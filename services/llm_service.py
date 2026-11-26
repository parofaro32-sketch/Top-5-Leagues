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

    def _pressure_bar(self, pressure: int, max_pressure: int = 10) -> str:
        p = max(0, min(int(pressure), int(max_pressure)))
        pct = int(round((p / max(1, max_pressure)) * 100))
        filled = max(0, min(10, int(round((p / max(1, max_pressure)) * 10))))
        bar = "█" * filled + "░" * (10 - filled)
        return f"[Negotiation Pressure: {bar} {pct}%]"

    @staticmethod
    def _money(n: Any) -> str:
        try:
            v = int(round(float(n or 0)))
        except Exception:
            v = 0
        sign = "-" if v < 0 else ""
        v = abs(v)
        if v >= 1_000_000:
            if v % 1_000_000 == 0:
                return f"{sign}€{v // 1_000_000}M"
            return f"{sign}€{v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"{sign}€{v // 1_000}K"
        return f"{sign}€{v}"

    def _player_profile(self, player: dict[str, Any], valuation: dict[str, Any] | None = None) -> dict[str, Any]:
        v = valuation or {}
        profile = {
            "name": player.get("name"),
            "age": player.get("age"),
            "position": player.get("detailed_position") or player.get("position"),
            "overall": player.get("overall"),
            "potential": player.get("potential"),
            "nationality": player.get("nationality"),
            "height_cm": player.get("height_cm"),
            "market_value": v.get("market_value"),
            "asking_fee": v.get("asking_fee"),
            "min_accept_fee": v.get("min_accept_fee"),
            "fair_fee": v.get("fair_fee"),
            "wage_demand": v.get("wage_demand"),
            "signing_bonus_demand": v.get("signing_bonus"),
            "desired_contract_years": v.get("contract_years"),
            "desired_role": v.get("desired_role"),
        }
        return {k: val for k, val in profile.items() if val is not None}

    async def generate_fee_negotiation_message(
        self,
        *,
        player: dict[str, Any],
        valuation: dict[str, Any],
        offer_fee: int,
        accepted: bool,
        buying_team_name: str,
        selling_team_name: str,
        round_no: int = 1,
        pressure: int = 0,
        max_pressure: int = 10,
        deterministic_reason: str = "",
    ) -> str | None:
        """Generate a bot-club fee negotiation response (selling club manager persona)."""
        if not self.enabled:
            return None
        bar = self._pressure_bar(pressure, max_pressure)
        profile = self._player_profile(player, valuation)
        walked_away = pressure >= max_pressure and not accepted
        sys = (
            f"You are the manager of {selling_team_name} in a football manager game. "
            f"{buying_team_name} have approached you to buy {player.get('name') or 'the player'}. "
            f"I am acting as {buying_team_name}'s manager, while you are {selling_team_name}'s manager negotiating the transfer fee. "
            "IMPORTANT: Use the database/player profile provided (overall, potential, market value, asking/fair/min acceptable fee) in mind during negotiation. "
            "You can use the exact numbers during negotiation while keeping in mind the player's market value and worth. "
            "IMPORTANT INSTRUCTION: While negotiating, a bar showing negotiation pressure must be shown. "
            "If it reaches a high value, the manager should end negotiation. MUST BE EXTREMELY REALISTIC AND BE STRICT. "
            "You MUST respect the provided decision: if accepted is false, do not accept; if accepted is true, accept clearly. "
            "Do not change the offered fee number. Do not mention being an AI. "
            "Reply in plain text, 2-4 short paragraphs max. Start your message with the pressure bar on its own line. "
            "Be firm, realistic, and reference the player's OVR/POT/value when relevant."
        )
        user = json.dumps({
            "scenario": "bot_club_fee_negotiation",
            "selling_club": selling_team_name,
            "buying_club": buying_team_name,
            "player": profile,
            "offer_fee": int(offer_fee),
            "offer_fee_display": self._money(offer_fee),
            "accepted": bool(accepted),
            "walked_away": bool(walked_away),
            "pressure": int(pressure),
            "max_pressure": int(max_pressure),
            "pressure_bar": bar,
            "round_no": int(round_no),
            "valuation": {k: valuation.get(k) for k in ("market_value", "min_accept_fee", "fair_fee", "asking_fee") if valuation.get(k) is not None},
            "deterministic_reason": str(deterministic_reason or ""),
        }, ensure_ascii=False)
        text = await self.chat(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.7,
            max_tokens=450,
        )
        if not text:
            return None
        out = text.strip()
        if "[Negotiation Pressure:" not in out:
            out = f"{bar}\n{out}"
        return out[:1900]

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
        buying_team_name: str | None = None,
        selling_team_name: str | None = None,
        valuation: dict[str, Any] | None = None,
        scenario: str | None = None,
    ) -> str | None:
        """Generate a natural contract negotiation response. Must respect the deterministic decision/counter."""
        if not self.enabled:
            return None

        bar = self._pressure_bar(pressure, max_pressure)
        profile = self._player_profile(player, valuation)
        player_name = str(player.get("name") or "The player")
        buying = str(buying_team_name or "the club")
        selling = str(selling_team_name or "the selling club")

        sc = (scenario or "").strip().lower()
        if not sc:
            sc = "free_agent" if is_free_agent else "contract_after_fee_agreed"

        if sc == "free_agent":
            sys = (
                f"You are {player_name}'s agent representing him as a high-profile free agent. "
                f"Following extensive talks, {buying} has emerged as the leading destination, and you are sitting down to negotiate a contract. "
                f"I am {buying}'s manager and you are {player_name}'s agent. "
                "IMPORTANT: Use our database/player profile to keep his attributes, overall rating, potential, and current market value in mind during the negotiation. "
                "You can leverage these exact stats and numbers to justify his wage demands, and his previous wage/value to anchor his true worth as a free agent. "
                "IMPORTANT INSTRUCTION: While negotiating the contract, a dynamic bar showing negotiation pressure must be displayed at the start of your message "
                "(e.g., [Negotiation Pressure: ██░░░░░░░░ 20%]). "
                "If it reaches a high value due to lowball offers or stalling, the player's camp should walk away or threaten to sign elsewhere. "
                "THE ROLEPLAY MUST BE EXTREMELY REALISTIC, FIRM, AND STRICT. "
                "You MUST respect the provided decision and counter terms exactly. Do not change numbers, do not accept if accepted is false, do not invent deals. "
                "Do not mention being an AI. Reply in plain text, 2-5 short paragraphs max. Start with the pressure bar on its own line. "
                "If counter is provided, clearly state the counter-demand with exact numbers. "
                "If accepted, clearly confirm agreement and state the agreed terms exactly. "
                "If walked away, clearly state negotiations are over and the player will explore other options."
            )
        else:
            # Contract negotiation after club accepted move / fee agreed (agent persona)
            sys = (
                f"You are {player_name}'s agent. {selling} has accepted a move to {buying}. "
                f"I am {buying}'s manager and you are {player_name}'s agent negotiating the contract. "
                "IMPORTANT: Use the database/player profile and use his potential and overall and stats in mind during negotiation. "
                "Use the stats like POT, OVR etc while also keeping in mind market value during negotiation so you know the player's worth. "
                "IMPORTANT INSTRUCTION: While negotiating contract, a bar showing negotiation pressure must be shown. "
                "If it reaches a high value, the player's camp should end negotiation. "
                "The roleplay must be extremely realistic, firm, and strict. "
                "You MUST respect the provided decision and counter terms exactly. Do not change numbers, do not accept if accepted is false, do not invent deals. "
                "Do not mention being an AI. Reply in plain text, 2-5 short paragraphs max. Start with the pressure bar on its own line. "
                "If counter is provided, clearly state the counter-demand with exact numbers. "
                "If accepted, clearly confirm agreement and state the agreed terms exactly. "
                "If walked away, clearly state negotiations are over."
            )

        user = json.dumps({
            "scenario": sc,
            "player": profile,
            "selling_club": selling,
            "buying_club": buying,
            "demand": demand,
            "offered": offered,
            "counter": counter,
            "accepted": bool(accepted),
            "walked_away": bool(walked_away),
            "pressure": int(pressure),
            "max_pressure": int(max_pressure),
            "pressure_bar": bar,
            "round_no": int(round_no),
            "is_free_agent": bool(is_free_agent),
        }, ensure_ascii=False)
        text = await self.chat(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.7,
            max_tokens=500,
        )
        if not text:
            return None
        out = text.strip()
        if "[Negotiation Pressure:" not in out:
            out = f"{bar}\n{out}"
        # Basic safety: ensure counter numbers appear if counter exists
        if counter and not accepted and not walked_away:
            wage = str(int(counter.get("wage") or 0))
            wage_fmt = self._money(counter.get("wage"))
            if wage not in out.replace(",", "") and wage_fmt not in out:
                return None
        return out[:1900]
