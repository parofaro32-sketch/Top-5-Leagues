from __future__ import annotations

import json
import logging
import re
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

    def _candidate_models(self, model: str | None) -> list[str]:
        primary = str(model or self.settings.llm_model or "").strip()
        # Fallbacks for common Gemini OpenAI-compatible model IDs.
        fallbacks = [
            "gemini-3.8-flash",
            "gemini-3.8-flash-preview",
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ]
        out: list[str] = []
        for m in [primary, *fallbacks]:
            m = str(m or "").strip()
            if m and m not in out:
                out.append(m)
        return out

    @staticmethod
    def _is_model_error(status: int, body: str) -> bool:
        if status == 404:
            return True
        if status != 400:
            return False
        b = (body or "").lower()
        return any(k in b for k in ["model", "not found", "unsupported", "does not exist", "invalid model", "unknown model"])

    @staticmethod
    def _is_response_format_error(status: int, body: str) -> bool:
        if status not in {400, 422}:
            return False
        b = (body or "").lower()
        return any(k in b for k in ["response_format", "json_object", "json schema", "unsupported"])

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

        session = await self._get_session()
        url = self._chat_url()
        headers = self._headers()
        models = self._candidate_models(model)
        last_error = None

        for idx, m in enumerate(models):
            payload: dict[str, Any] = {
                "model": m,
                "messages": messages,
                "temperature": float(temperature),
                "max_tokens": int(max_tokens),
            }
            if response_format is not None:
                payload["response_format"] = response_format

            try:
                async with session.post(url, headers=headers, json=payload) as resp:
                    raw = await resp.text()
                    if resp.status >= 400:
                        last_error = f"status={resp.status} body={raw[:500]}"
                        # Retry without response_format if provider rejects it
                        if response_format is not None and self._is_response_format_error(resp.status, raw):
                            payload2 = dict(payload)
                            payload2.pop("response_format", None)
                            try:
                                async with session.post(url, headers=headers, json=payload2) as resp2:
                                    raw2 = await resp2.text()
                                    if resp2.status < 400:
                                        data = json.loads(raw2)
                                        text = self._extract_chat_text(data)
                                        if text:
                                            if m != (model or self.settings.llm_model):
                                                logger.info("LLM chat succeeded with fallback model=%s (response_format removed)", m)
                                            return text
                                    raw = raw2
                                    resp_status = resp2.status
                            except Exception:
                                resp_status = resp.status
                        else:
                            resp_status = resp.status

                        if self._is_model_error(resp_status, raw) and idx < len(models) - 1:
                            logger.warning("LLM model failed (%s), trying fallback model; model=%s body=%s", resp_status, m, raw[:300])
                            continue

                        logger.warning("LLM chat failed status=%s model=%s body=%s", resp_status, m, raw[:500])
                        # For auth/rate/other non-model errors, don't spam fallbacks
                        if resp_status in {401, 403, 429}:
                            return None
                        # For other errors, try next model only if it might be model related; otherwise stop
                        if not self._is_model_error(resp_status, raw):
                            return None
                        continue
                    data = json.loads(raw)
            except Exception as e:
                last_error = str(e)
                logger.exception("LLM chat request failed model=%s", m)
                continue

            text = self._extract_chat_text(data)
            if text:
                if idx > 0:
                    logger.info("LLM chat succeeded with fallback model=%s", m)
                return text
            logger.warning("LLM chat returned no text model=%s", m)

        if last_error:
            logger.warning("LLM chat exhausted candidates; last_error=%s", last_error)
        return None

    @staticmethod
    def _extract_chat_text(data: dict[str, Any]) -> str | None:
        try:
            choices = data.get("choices") or []
            if not choices:
                return None
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                text = content.strip()
                return text or None
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
            "ovr": player.get("overall"),
            "potential": player.get("potential"),
            "pot": player.get("potential"),
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

    @staticmethod
    def _coerce_int(value: Any, default: int = 0) -> int:
        try:
            if value is None:
                return int(default)
            if isinstance(value, str):
                s = value.strip().replace(",", "").replace("€", "").replace("£", "").replace("$", "")
                m = re.search(r"-?\d+(?:\.\d+)?", s)
                if not m:
                    return int(default)
                return int(round(float(m.group(0))))
            return int(round(float(value)))
        except Exception:
            return int(default)

    @staticmethod
    def _coerce_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"true", "yes", "y", "1", "accept", "accepted", "walk_away", "walkaway"}:
                return True
            if v in {"false", "no", "n", "0", "reject", "rejected"}:
                return False
        return bool(default)

    def _sanitize_contract_counter(self, counter: Any, *, fallback: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not isinstance(counter, dict):
            return None
        fb = fallback or {}
        from services.transfer_market_service import normalize_role

        wage = self._coerce_int(counter.get("wage"), self._coerce_int(fb.get("wage"), 0))
        bonus = self._coerce_int(counter.get("signing_bonus"), self._coerce_int(fb.get("signing_bonus"), 0))
        years = self._coerce_int(counter.get("contract_years"), self._coerce_int(fb.get("contract_years"), 3))
        role = normalize_role(str(counter.get("role") or fb.get("role") or "Rotation")) or "Rotation"

        if wage < 1_000 or wage > 10_000_000:
            return None
        bonus = max(0, min(bonus, 500_000_000))
        years = max(1, min(years, 5))
        return {"wage": int(wage), "role": role, "signing_bonus": int(bonus), "contract_years": int(years)}

    async def negotiate_fee(
        self,
        *,
        player: dict[str, Any],
        valuation: dict[str, Any],
        offer_fee: int,
        buying_team_name: str,
        selling_team_name: str,
        round_no: int = 1,
        previous_pressure: int = 0,
        max_pressure: int = 10,
    ) -> dict[str, Any] | None:
        """LLM-driven fee negotiation decision (prompt #1 persona). Returns validated dict or None."""
        if not self.enabled:
            return None
        profile = self._player_profile(player, valuation)
        prev_p = max(0, min(int(previous_pressure), int(max_pressure)))
        bar_hint = self._pressure_bar(prev_p, max_pressure)

        sys = (
            f"You are the manager of {selling_team_name} in a football manager game. "
            f"{buying_team_name} have approached you to buy {player.get('name') or 'the player'}. "
            f"I am acting as {buying_team_name}'s manager, while you are {selling_team_name}'s manager negotiating the transfer fee. "
            "IMPORTANT: Use the database/player profile provided (overall, potential, market value, asking/fair/min acceptable fee) in mind during negotiation. "
            "You can use the exact numbers during negotiation while keeping in mind the player's market value and worth. "
            "IMPORTANT INSTRUCTION: While negotiating, a bar showing negotiation pressure must be shown. "
            "If it reaches a high value, the manager should end negotiation. MUST BE EXTREMELY REALISTIC AND BE STRICT. "
            "You decide whether to accept or reject the fee. Do not mention being an AI. "
            "Return ONLY a JSON object with keys: "
            "accepted (boolean), walked_away (boolean), pressure (integer 0-10), message (string). "
            "Rules: pressure must be >= previous_pressure unless there is a strong reason, and never above 10. "
            "If pressure >= 10 or walked_away is true, accepted must be false and negotiations are over. "
            "If accepted is true, walked_away must be false and message must clearly accept the fee. "
            "If accepted is false, message must clearly reject and explain why realistically/strictly. "
            "message must start with the negotiation pressure bar in the format [Negotiation Pressure: ███░░░░░░░ XX%]. "
            "message should be 2-4 short paragraphs, firm and realistic, referencing OVR/POT/value."
        )
        user = json.dumps({
            "scenario": "bot_club_fee_negotiation",
            "selling_club": selling_team_name,
            "buying_club": buying_team_name,
            "player": profile,
            "offer_fee": int(offer_fee),
            "offer_fee_display": self._money(offer_fee),
            "previous_pressure": prev_p,
            "max_pressure": int(max_pressure),
            "previous_pressure_bar": bar_hint,
            "round_no": int(round_no),
        }, ensure_ascii=False)

        data = await self.chat_json(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.7,
            max_tokens=700,
        )
        if not isinstance(data, dict):
            return None

        def _pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
            for k in keys:
                if k in d and d.get(k) is not None:
                    return d.get(k)
            return default

        pressure = max(
            prev_p,
            min(int(max_pressure), self._coerce_int(_pick(data, "pressure", "pressure_level", "pressure_pct", default=prev_p), prev_p)),
        )
        walked_away = self._coerce_bool(_pick(data, "walked_away", "walkedAway", "walk_away", "walkaway", default=False), False)
        accepted = self._coerce_bool(_pick(data, "accepted", "accept", "is_accepted", "isAccepted", default=False), False)
        if pressure >= int(max_pressure):
            walked_away = True
            accepted = False
        if walked_away:
            accepted = False
        if accepted:
            walked_away = False

        msg = str(_pick(data, "message", "response", "reply", "text", default="") or "").strip()
        if not msg:
            return None
        bar = self._pressure_bar(pressure, max_pressure)
        if "[Negotiation Pressure:" not in msg:
            msg = f"{bar}\n{msg}"
        else:
            # normalize first-line bar to actual pressure to avoid inconsistencies
            lines = msg.splitlines()
            if lines and "[Negotiation Pressure:" in lines[0]:
                lines[0] = bar
                msg = "\n".join(lines)

        return {
            "accepted": bool(accepted),
            "walked_away": bool(walked_away),
            "pressure": int(pressure),
            "pressure_bar": bar,
            "message": msg[:1900],
        }

    async def negotiate_contract(
        self,
        *,
        player: dict[str, Any],
        demand: dict[str, Any],
        offered: dict[str, Any],
        previous_pressure: int = 0,
        max_pressure: int = 10,
        round_no: int = 1,
        is_free_agent: bool = False,
        buying_team_name: str | None = None,
        selling_team_name: str | None = None,
        valuation: dict[str, Any] | None = None,
        scenario: str | None = None,
    ) -> dict[str, Any] | None:
        """LLM-driven contract negotiation decision (prompts #2/#3). Returns validated dict or None."""
        if not self.enabled:
            return None

        profile = self._player_profile(player, valuation)
        player_name = str(player.get("name") or "The player")
        buying = str(buying_team_name or "the club")
        selling = str(selling_team_name or "the selling club")
        prev_p = max(0, min(int(previous_pressure), int(max_pressure)))
        bar_hint = self._pressure_bar(prev_p, max_pressure)

        sc = (scenario or "").strip().lower()
        if not sc:
            sc = "free_agent" if is_free_agent else "contract_after_fee_agreed"

        if sc == "free_agent":
            persona = (
                f"You are {player_name}'s agent representing him as a high-profile free agent. "
                f"Following extensive talks, {buying} has emerged as the leading destination, and you are sitting down to negotiate a contract. "
                f"I am {buying}'s manager and you are {player_name}'s agent. "
                "IMPORTANT: Use our database/player profile to keep his attributes, overall rating, potential, and current market value in mind during the negotiation. "
                "You can leverage these exact stats and numbers to justify his wage demands, and his previous wage/value to anchor his true worth as a free agent. "
                "IMPORTANT INSTRUCTION: While negotiating the contract, a dynamic bar showing negotiation pressure must be displayed at the start of your message "
                "(e.g., [Negotiation Pressure: ██░░░░░░░░ 20%]). "
                "If it reaches a high value due to lowball offers or stalling, the player's camp should walk away or threaten to sign elsewhere. "
                "THE ROLEPLAY MUST BE EXTREMELY REALISTIC, FIRM, AND STRICT."
            )
        else:
            persona = (
                f"You are {player_name}'s agent. {selling} has accepted a move to {buying}. "
                f"I am {buying}'s manager and you are {player_name}'s agent negotiating the contract. "
                "IMPORTANT: Use the database/player profile and use his potential and overall and stats in mind during negotiation. "
                "Use the stats like POT, OVR etc while also keeping in mind market value during negotiation so you know the player's worth. "
                "IMPORTANT INSTRUCTION: While negotiating contract, a bar showing negotiation pressure must be shown. "
                "If it reaches a high value, the player's camp should end negotiation. "
                "The roleplay must be extremely realistic, firm, and strict."
            )

        sys = (
            persona + " "
            "You decide the negotiation outcome. Do not mention being an AI. "
            "Return ONLY a JSON object with keys: "
            "accepted (boolean), walked_away (boolean), pressure (integer 0-10), "
            "counter (object|null with wage integer euros/week, role one of Crucial/Important/Rotation/Sporadic/Prospect, signing_bonus integer euros, contract_years integer 1-5), "
            "message (string). "
            "Rules: pressure must be >= previous_pressure unless there is strong reason, never above 10. "
            "If pressure >= 10 or walked_away is true, accepted must be false, counter must be null, and message must clearly end negotiations. "
            "If accepted is true, walked_away must be false, counter must be null, and message must clearly confirm agreement with the exact offered terms. "
            "If accepted is false and walked_away is false, counter must be present and message must clearly state the counter-demand with exact numbers. "
            "message must start with the negotiation pressure bar in the format [Negotiation Pressure: ███░░░░░░░ XX%]. "
            "message should be 2-5 short paragraphs. Be realistic and strict; use OVR/POT/market value in reasoning."
        )

        user = json.dumps({
            "scenario": sc,
            "player": profile,
            "selling_club": selling,
            "buying_club": buying,
            "demand": demand,
            "offered": offered,
            "previous_pressure": prev_p,
            "max_pressure": int(max_pressure),
            "previous_pressure_bar": bar_hint,
            "round_no": int(round_no),
            "is_free_agent": bool(is_free_agent),
        }, ensure_ascii=False)

        data = await self.chat_json(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.7,
            max_tokens=850,
        )
        if not isinstance(data, dict):
            return None

        def _pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
            for k in keys:
                if k in d and d.get(k) is not None:
                    return d.get(k)
            return default

        pressure = max(
            prev_p,
            min(int(max_pressure), self._coerce_int(_pick(data, "pressure", "pressure_level", "pressure_pct", default=prev_p), prev_p)),
        )
        walked_away = self._coerce_bool(_pick(data, "walked_away", "walkedAway", "walk_away", "walkaway", default=False), False)
        accepted = self._coerce_bool(_pick(data, "accepted", "accept", "is_accepted", "isAccepted", default=False), False)
        if pressure >= int(max_pressure):
            walked_away = True
            accepted = False
        if walked_away:
            accepted = False
        if accepted:
            walked_away = False

        counter = None
        if not accepted and not walked_away:
            raw_counter = _pick(data, "counter", "counter_offer", "counterOffer", "counter_demand", "counterDemand", default=None)
            counter = self._sanitize_contract_counter(raw_counter, fallback=demand)
            if counter is None:
                # Try to be lenient: if model gave partial counter, fill from demand
                rc = raw_counter if isinstance(raw_counter, dict) else {}
                merged = dict(demand or {})
                merged.update({k: v for k, v in rc.items() if v is not None})
                counter = self._sanitize_contract_counter(merged, fallback=demand)
            if counter is None:
                # Last resort: keep LLM's decision to counter, fill from demand
                counter = self._sanitize_contract_counter(demand, fallback={
                    "wage": 100000, "role": "Rotation", "signing_bonus": 0, "contract_years": 3
                })

        msg = str(_pick(data, "message", "response", "reply", "text", default="") or "").strip()
        if not msg:
            return None
        bar = self._pressure_bar(pressure, max_pressure)
        if "[Negotiation Pressure:" not in msg:
            msg = f"{bar}\n{msg}"
        else:
            lines = msg.splitlines()
            if lines and "[Negotiation Pressure:" in lines[0]:
                lines[0] = bar
                msg = "\n".join(lines)

        # If accepted, ensure the message reflects offered terms (handler finalizes offered terms)
        if accepted:
            offered_wage = self._money(offered.get("wage"))
            if offered_wage not in msg and str(self._coerce_int(offered.get("wage"), 0)) not in msg.replace(",", ""):
                # Append explicit agreed terms to prevent ambiguity
                msg += (
                    f"\n\n**Agreed terms:** {self._money(offered.get('wage'))}/wk, {offered.get('role')}, "
                    f"{self._money(offered.get('signing_bonus'))} signing bonus, {int(offered.get('contract_years') or 0)} years."
                )

        # If counter, ensure counter numbers appear
        if counter and not accepted and not walked_away:
            wage = str(int(counter.get("wage") or 0))
            wage_fmt = self._money(counter.get("wage"))
            if wage not in msg.replace(",", "") and wage_fmt not in msg:
                msg += (
                    f"\n\n**Counter-demand:** {self._money(counter.get('wage'))}/wk, {counter.get('role')}, "
                    f"{self._money(counter.get('signing_bonus'))} signing bonus, {int(counter.get('contract_years') or 0)} years."
                )

        return {
            "accepted": bool(accepted),
            "walked_away": bool(walked_away),
            "pressure": int(pressure),
            "pressure_bar": bar,
            "counter": counter,
            "message": msg[:1900],
        }

    # Backwards-compatible aliases used by older call sites
    async def generate_fee_negotiation_message(self, **kwargs):
        decision = await self.negotiate_fee(
            player=kwargs.get("player") or {},
            valuation=kwargs.get("valuation") or {},
            offer_fee=int(kwargs.get("offer_fee") or 0),
            buying_team_name=str(kwargs.get("buying_team_name") or ""),
            selling_team_name=str(kwargs.get("selling_team_name") or ""),
            round_no=int(kwargs.get("round_no") or 1),
            previous_pressure=int(kwargs.get("pressure") or 0),
            max_pressure=int(kwargs.get("max_pressure") or 10),
        )
        return decision.get("message") if isinstance(decision, dict) else None

    async def generate_negotiation_message(self, **kwargs):
        decision = await self.negotiate_contract(
            player=kwargs.get("player") or {},
            demand=kwargs.get("demand") or {},
            offered=kwargs.get("offered") or {},
            previous_pressure=int(kwargs.get("pressure") or 0),
            max_pressure=int(kwargs.get("max_pressure") or 10),
            round_no=int(kwargs.get("round_no") or 1),
            is_free_agent=bool(kwargs.get("is_free_agent")),
            buying_team_name=kwargs.get("buying_team_name"),
            selling_team_name=kwargs.get("selling_team_name"),
            valuation=kwargs.get("valuation"),
            scenario=kwargs.get("scenario"),
        )
        return decision.get("message") if isinstance(decision, dict) else None
