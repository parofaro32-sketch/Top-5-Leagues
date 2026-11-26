from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from config import Settings

logger = logging.getLogger(__name__)


class LLMService:
    """OpenAI-compatible chat completions client with safe fallbacks.

    Works with OpenAI-compatible endpoints, including Gemini's OpenAI-compatible API.
    All methods return None on failure so callers can fall back to deterministic logic.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.has_llm)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=max(5, int(self.settings.llm_timeout or 20)))
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }

    def _chat_url(self) -> str:
        base = (self.settings.llm_base_url or "").rstrip("/")
        # Accept both .../v1 and .../v1beta/openai style bases
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.4,
        max_tokens: int = 700,
        model: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        payload: dict[str, Any] = {
            "model": model or self.settings.llm_model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        if response_format is not None:
            payload["response_format"] = response_format
        try:
            session = await self._get_session()
            async with session.post(self._chat_url(), headers=self._headers(), json=payload) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    logger.warning("LLM chat failed status=%s body=%s", resp.status, raw[:500])
                    return None
                data = json.loads(raw)
        except Exception:
            logger.exception("LLM chat request failed")
            return None

        try:
            choices = data.get("choices") or []
            if not choices:
                return None
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                text = content.strip()
                return text or None
            # Some providers return content parts
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and isinstance(p.get("text"), str):
                        parts.append(p["text"])
                    elif isinstance(p, str):
                        parts.append(p)
                text = "".join(parts).strip()
                return text or None
        except Exception:
            logger.exception("LLM chat response parse failed")
        return None

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 900,
        model: str | None = None,
    ) -> dict[str, Any] | None:
        """Request a JSON object response. Returns parsed dict or None."""
        # Try with response_format first; fallback to plain if provider rejects it
        text = await self.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            response_format={"type": "json_object"},
        )
        if text is None:
            text = await self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                model=model,
            )
        if not text:
            return None
        # Strip code fences if present
        t = text.strip()
        if t.startswith("```"):
            t = t.strip("`")
            # remove optional language tag
            nl = t.find("\n")
            if nl != -1 and t[:nl].strip().lower() in {"json", "javascript", "js"}:
                t = t[nl + 1 :].strip()
        # Find JSON object
        try:
            return json.loads(t)
        except Exception:
            pass
        # Try to extract first {...}
        try:
            start = t.find("{")
            end = t.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(t[start : end + 1])
        except Exception:
            pass
        logger.warning("LLM chat_json could not parse JSON: %s", t[:300])
        return None

    async def parse_contract_terms_llm(self, text: str, *, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """LLM fallback for contract term extraction. Returns dict with wage/role/signing_bonus/contract_years or None."""
        if not self.enabled:
            return None
        ctx = context or {}
        sys = (
            "You extract football contract terms from natural language. "
            "Return ONLY a JSON object with keys: wage, signing_bonus, contract_years, role. "
            "wage and signing_bonus must be integers in euros (weekly wage, signing bonus total). "
            "contract_years must be an integer 1-5. role must be one of: Crucial, Important, Rotation, Sporadic, Prospect. "
            "Use null for unknown fields. Never invent terms the user did not state. "
            "Understand k/m/thousand/million and currency symbols. 100k=100000, 1.5m=1500000. "
            "Do not confuse contract years with money."
        )
        user = json.dumps({"message": str(text or ""), "context": ctx}, ensure_ascii=False)
        data = await self.chat_json(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.1,
            max_tokens=300,
        )
        if not isinstance(data, dict):
            return None
        out: dict[str, Any] = {}
        for k in ("wage", "signing_bonus", "contract_years"):
            v = data.get(k)
            if v is None:
                continue
            try:
                iv = int(float(v))
            except Exception:
                continue
            if k == "contract_years":
                if 1 <= iv <= 7:
                    out[k] = iv
            elif k == "wage":
                if 1_000 <= iv <= 10_000_000:
                    out[k] = iv
            elif k == "signing_bonus":
                if 0 <= iv <= 500_000_000:
                    out[k] = iv
        role = data.get("role")
        if isinstance(role, str) and role.strip():
            from services.transfer_market_service import normalize_role
            r = normalize_role(role.strip())
            if r:
                out["role"] = r
        return out or None

    async def generate_negotiation_message(
        self,
        *,
        player: dict[str, Any],
        demand: dict[str, Any],
        offered: dict[str, Any],
        counter: dict[str, Any] | None,
        accepted: bool,
        walked_away: bool,
        pressure: int,
        max_pressure: int,
        round_no: int,
        is_free_agent: bool,
        deterministic_message: str,
    ) -> str | None:
        """Generate a natural negotiation response. Must respect the deterministic decision/counter."""
        if not self.enabled:
            return None
        sys = (
            "You are a professional football agent/club negotiator in a football manager game. "
            "Write a concise, realistic negotiation message in plain text. "
            "You MUST respect the provided decision and counter terms exactly. "
            "Do not change numbers, do not accept if accepted is false, do not invent deals. "
            "Do not mention being an AI. Keep it human, firm but fair, 2-5 short paragraphs max. "
            "Include the pressure bar as given. If counter is provided, clearly state the counter-demand. "
            "If accepted, clearly confirm agreement and state the agreed terms exactly. "
            "If walked away, clearly state negotiations are over."
        )
        user = json.dumps({
            "player": {
                "name": player.get("name"),
                "overall": player.get("overall"),
                "potential": player.get("potential"),
                "age": player.get("age"),
                "position": player.get("detailed_position") or player.get("position"),
            },
            "demand": demand,
            "offered": offered,
            "counter": counter,
            "accepted": accepted,
            "walked_away": walked_away,
            "pressure": pressure,
            "max_pressure": max_pressure,
            "round_no": round_no,
            "is_free_agent": is_free_agent,
            "pressure_bar": deterministic_message.splitlines()[0] if deterministic_message else "",
        }, ensure_ascii=False)
        text = await self.chat(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.7,
            max_tokens=450,
        )
        if not text:
            return None
        # Basic safety: ensure counter numbers appear if counter exists
        if counter and not accepted and not walked_away:
            wage = str(int(counter.get("wage") or 0))
            if wage and wage not in text.replace(",", ""):
                # If LLM omitted the key numbers, fall back to deterministic
                return None
        return text.strip()[:1900]
