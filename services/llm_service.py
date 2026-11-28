from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import deque
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
        self.last_error: str | None = None
        self._cooldown_until: float = 0.0
        self._request_times: deque[float] = deque(maxlen=60)
        self._key_index: int = 0
        # Per-provider state: {name: {"key_index": int, "cooldown_until": float}}
        self._provider_state: dict[str, dict[str, Any]] = {}
        # Small response cache to limit token usage on identical low-temp prompts.
        self._cache: dict[str, tuple[float, str]] = {}
        self._cache_ttl_s: float = 120.0
        self._cache_max: int = 64

    @property
    def enabled(self) -> bool:
        return bool(self._providers())

    @property
    def _api_keys(self) -> list[str]:
        keys = list(getattr(self.settings, "llm_api_keys", None) or [])
        if not keys and self.settings.llm_api_key:
            keys = [self.settings.llm_api_key]
        # de-dupe while preserving order
        out: list[str] = []
        for k in keys:
            k = str(k or "").strip()
            if k and k not in out:
                out.append(k)
        return out

    def _current_api_key(self) -> str:
        keys = self._api_keys
        if not keys:
            return ""
        self._key_index = self._key_index % len(keys)
        return keys[self._key_index]

    def _rotate_api_key(self) -> bool:
        keys = self._api_keys
        if len(keys) <= 1:
            return False
        self._key_index = (self._key_index + 1) % len(keys)
        logger.info("LLM rotated to API key index %s/%s", self._key_index + 1, len(keys))
        return True

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=max(20, int(self.settings.llm_timeout or 45)))
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._current_api_key()}",
            "Content-Type": "application/json",
        }

    def _chat_url(self) -> str:
        base = (self.settings.llm_base_url or "").rstrip("/")
        # Accept both .../v1 and .../v1beta/openai style bases
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    # ------------------------------------------------------------------
    # Multi-provider support (Gemini primary + OpenRouter/Groq fallbacks)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_base_url(base: str) -> str:
        b = str(base or "").strip().rstrip("/")
        if not b:
            return b
        if b.endswith("/chat/completions"):
            return b
        return f"{b}/chat/completions"

    @staticmethod
    def _dedupe_strs(values: list[str] | None) -> list[str]:
        out: list[str] = []
        for v in values or []:
            s = str(v or "").strip()
            if s and s not in out:
                out.append(s)
        return out

    def _provider_state_for(self, name: str) -> dict[str, Any]:
        st = self._provider_state.get(name)
        if not isinstance(st, dict):
            st = {"key_index": 0, "cooldown_until": 0.0}
            self._provider_state[name] = st
        st.setdefault("key_index", 0)
        st.setdefault("cooldown_until", 0.0)
        return st

    def _providers(self) -> list[dict[str, Any]]:
        """Build provider chain: primary Gemini first, then OpenRouter, then Groq."""
        providers: list[dict[str, Any]] = []

        primary_keys = self._dedupe_strs(getattr(self.settings, "llm_api_keys", None) or ([self.settings.llm_api_key] if self.settings.llm_api_key else []))
        if primary_keys:
            primary_models = self._candidate_models_for_provider(
                primary=str(self.settings.llm_model or "").strip(),
                configured=list(getattr(self.settings, "llm_models", None) or []),
                defaults=[
                    "gemini-3.8-flash",
                    "gemini-3.8-flash-preview",
                    "gemini-3-flash-preview",
                    "gemini-2.0-flash",
                    "gemini-2.0-flash-lite",
                ],
            )
            providers.append({
                "name": "primary",
                "base_url": self._normalize_base_url(self.settings.llm_base_url),
                "api_keys": primary_keys,
                "models": primary_models,
                "extra_headers": {},
            })

        or_keys = self._dedupe_strs(getattr(self.settings, "openrouter_api_keys", None) or [])
        if or_keys:
            or_models = self._candidate_models_for_provider(
                primary="",
                configured=list(getattr(self.settings, "openrouter_models", None) or []),
                defaults=[
                    "nvidia/nemotron-3.5-lightning",
                    "nvidia/nemotron-3.5-super-vl",
                    "nvidia/nemotron-3.5-lightning:free",
                    "nvidia/nemotron-3.5-super-vl:free",
                ],
            )
            providers.append({
                "name": "openrouter",
                "base_url": self._normalize_base_url(getattr(self.settings, "openrouter_base_url", "https://openrouter.ai/api/v1")),
                "api_keys": or_keys,
                "models": or_models,
                "extra_headers": {
                    "HTTP-Referer": "https://github.com/parofaro32-sketch/Top-5-Leagues",
                    "X-Title": "Football Manager Bot",
                },
            })

        groq_keys = self._dedupe_strs(getattr(self.settings, "groq_api_keys", None) or [])
        if groq_keys:
            groq_models = self._candidate_models_for_provider(
                primary="",
                configured=list(getattr(self.settings, "groq_models", None) or []),
                defaults=[
                    "llama-3.1-8b-instant",
                    "llama-3.3-70b-versatile",
                    "gemma2-9b-it",
                ],
            )
            providers.append({
                "name": "groq",
                "base_url": self._normalize_base_url(getattr(self.settings, "groq_base_url", "https://api.groq.com/openai/v1")),
                "api_keys": groq_keys,
                "models": groq_models,
                "extra_headers": {},
            })

        return providers

    @staticmethod
    def _candidate_models_for_provider(*, primary: str, configured: list[str], defaults: list[str]) -> list[str]:
        out: list[str] = []
        for m in [primary, *configured, *defaults]:
            s = str(m or "").strip()
            if s and s not in out:
                out.append(s)
        return out

    def _candidate_models(self, model: str | None) -> list[str]:
        primary = str(model or self.settings.llm_model or "").strip()
        configured = [str(m or "").strip() for m in (getattr(self.settings, "llm_models", None) or []) if str(m or "").strip()]
        # Built-in fallbacks for common Gemini OpenAI-compatible model IDs.
        fallbacks = [
            "gemini-3.8-flash",
            "gemini-3.8-flash-preview",
            "gemini-3-flash-preview",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ]
        out: list[str] = []
        for m in [primary, *configured, *fallbacks]:
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
            self.last_error = "llm_disabled"
            return None

        self.last_error = None
        use_cache = float(temperature) <= 0.2
        cache_key = ""
        if use_cache:
            try:
                cache_payload = {
                    "messages": messages,
                    "temperature": float(temperature),
                    "max_tokens": int(max_tokens),
                    "model": str(model or ""),
                    "response_format": response_format,
                }
                cache_key = hashlib.sha256(json.dumps(cache_payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
                now_ts = time.time()
                hit = self._cache.get(cache_key)
                if hit and (now_ts - float(hit[0])) <= self._cache_ttl_s:
                    return hit[1]
            except Exception:
                use_cache = False
                cache_key = ""

        session = await self._get_session()
        providers = self._providers()
        if not providers:
            self.last_error = "llm_disabled"
            return None

        # If caller requests a specific model, try it first on the primary provider.
        requested_model = str(model or "").strip()
        last_error: str | None = None
        _timeout_s = int(getattr(self.settings, "llm_timeout", 45) or 45)
        overall_deadline = time.monotonic() + float(max(20, min(_timeout_s * 2, 60)))

        def _deadline_exceeded() -> bool:
            return time.monotonic() >= overall_deadline

        def _log_failure(status: int, prov: str, mdl: str, raw: str) -> None:
            logger.warning("LLM chat failed status=%s provider=%s model=%s body=%s", status, prov, mdl, (raw or "")[:500])

        def _is_quota(status: int, raw: str) -> bool:
            if status != 429:
                return False
            body_l = (raw or "").lower()
            return any(k in body_l for k in ["quota", "resource_exhausted", "rate limit exceeded", "too many requests"])

        for prov_idx, prov in enumerate(providers):
            if _deadline_exceeded():
                last_error = last_error or "overall_deadline_exceeded"
                logger.warning("LLM chat overall deadline exceeded before provider=%s", prov.get("name"))
                break
            prov_name = str(prov.get("name") or f"provider_{prov_idx}")
            prov_state = self._provider_state_for(prov_name)
            base_url = str(prov.get("base_url") or "")
            api_keys = self._dedupe_strs(list(prov.get("api_keys") or []))
            if not base_url or not api_keys:
                continue

            # Respect per-provider cooldown
            now_m = time.monotonic()
            cd = float(prov_state.get("cooldown_until") or 0.0)
            if now_m < cd:
                wait_s = min(10.0, cd - now_m)
                if wait_s > 0.5:
                    logger.info("LLM provider %s cooldown %.1fs, skipping to next provider", prov_name, wait_s)
                    continue
                if wait_s > 0:
                    await asyncio.sleep(wait_s)

            models = list(prov.get("models") or [])
            if requested_model:
                # Try requested model first on primary provider; on other providers keep their defaults.
                if prov_idx == 0:
                    models = self._candidate_models_for_provider(primary=requested_model, configured=list(getattr(self.settings, "llm_models", None) or []), defaults=models)
                elif requested_model not in models:
                    # Only prepend cross-provider if model id is clearly compatible (e.g. OpenRouter-style provider/model).
                    if "/" in requested_model:
                        models = [requested_model, *models]

            i = 0
            rotations = 0
            provider_dead = False
            while i < len(models):
                if _deadline_exceeded():
                    last_error = last_error or "overall_deadline_exceeded"
                    logger.warning("LLM chat overall deadline exceeded provider=%s model=%s", prov_name, models[i] if i < len(models) else "?")
                    provider_dead = True
                    break
                m = models[i]
                prov_state["key_index"] = int(prov_state.get("key_index") or 0) % max(1, len(api_keys))
                key_idx = int(prov_state.get("key_index") or 0)
                current_key = api_keys[key_idx]
                headers = {"Authorization": f"Bearer {current_key}", "Content-Type": "application/json"}
                headers.update(dict(prov.get("extra_headers") or {}))

                payload: dict[str, Any] = {
                    "model": m,
                    "messages": messages,
                    "temperature": float(temperature),
                    "max_tokens": int(max_tokens),
                }
                if response_format is not None:
                    payload["response_format"] = response_format

                resp_status: int | None = None
                raw = ""
                data: dict[str, Any] | None = None
                retry_after: float | None = None
                try:
                    async with session.post(base_url, headers=headers, json=payload) as resp:
                        raw = await resp.text()
                        resp_status = resp.status
                        ra = resp.headers.get("Retry-After")
                        if ra:
                            try:
                                retry_after = float(ra)
                            except Exception:
                                retry_after = None
                        if resp.status >= 400:
                            last_error = f"status={resp.status} provider={prov_name} body={raw[:500]}"
                            # Retry without response_format if provider rejects it
                            if response_format is not None and self._is_response_format_error(resp.status, raw):
                                payload2 = dict(payload)
                                payload2.pop("response_format", None)
                                try:
                                    async with session.post(base_url, headers=headers, json=payload2) as resp2:
                                        raw2 = await resp2.text()
                                        ra2 = resp2.headers.get("Retry-After")
                                        if ra2:
                                            try:
                                                retry_after = float(ra2)
                                            except Exception:
                                                pass
                                        if resp2.status < 400:
                                            try:
                                                data2 = json.loads(raw2)
                                            except Exception:
                                                data2 = None
                                            if isinstance(data2, dict):
                                                text2 = self._extract_chat_text(data2)
                                                if text2:
                                                    if prov_idx > 0 or m != (requested_model or self.settings.llm_model):
                                                        logger.info("LLM chat succeeded provider=%s model=%s (response_format removed)", prov_name, m)
                                                    self.last_error = None
                                                    if use_cache and cache_key:
                                                        try:
                                                            if len(self._cache) >= self._cache_max:
                                                                # drop oldest
                                                                oldest_k = min(self._cache.items(), key=lambda kv: kv[1][0])[0]
                                                                self._cache.pop(oldest_k, None)
                                                            self._cache[cache_key] = (time.time(), text2)
                                                        except Exception:
                                                            pass
                                                    return text2
                                            logger.warning("LLM chat returned no text after response_format retry provider=%s model=%s raw=%s", prov_name, m, raw2[:500])
                                            last_error = f"status=200 no_text provider={prov_name} body={raw2[:300]}"
                                            data = None
                                            raw = raw2
                                            resp_status = 200
                                            raise RuntimeError("_no_text_after_rf_retry")
                                        raw = raw2
                                        resp_status = resp2.status
                                        last_error = f"status={resp2.status} provider={prov_name} body={raw2[:500]}"
                                except RuntimeError as rexc:
                                    if str(rexc) == "_no_text_after_rf_retry":
                                        if i < len(models) - 1:
                                            i += 1
                                            continue
                                        provider_dead = True
                                        break
                                    raise
                                except Exception:
                                    pass

                            if resp_status in {401, 403}:
                                _log_failure(resp_status, prov_name, m, raw)
                                # Try rotating key; if no more keys, provider is dead.
                                if len(api_keys) > 1 and rotations < max(1, len(api_keys)):
                                    prov_state["key_index"] = (key_idx + 1) % len(api_keys)
                                    rotations += 1
                                    logger.info("LLM rotated %s key index %s/%s after auth failure", prov_name, prov_state["key_index"] + 1, len(api_keys))
                                    continue
                                provider_dead = True
                                break

                            if resp_status == 429:
                                _log_failure(resp_status, prov_name, m, raw)
                                quota_hit = _is_quota(resp_status, raw)
                                wait_s = float(retry_after) if retry_after is not None else (15.0 if quota_hit else 2.0)
                                wait_s = max(1.0, min(wait_s, 30.0))
                                if quota_hit:
                                    if len(api_keys) > 1 and rotations < max(1, len(api_keys)):
                                        prov_state["key_index"] = (key_idx + 1) % len(api_keys)
                                        rotations += 1
                                        prov_state["cooldown_until"] = max(float(prov_state.get("cooldown_until") or 0.0), time.monotonic() + 1.0)
                                        logger.info("LLM rotated %s key index %s/%s after quota", prov_name, prov_state["key_index"] + 1, len(api_keys))
                                        await asyncio.sleep(1.0)
                                        continue
                                    prov_state["cooldown_until"] = max(float(prov_state.get("cooldown_until") or 0.0), time.monotonic() + wait_s)
                                    last_error = last_error or f"status=429 quota provider={prov_name} body={raw[:200]}"
                                    provider_dead = True
                                    break
                                prov_state["cooldown_until"] = max(float(prov_state.get("cooldown_until") or 0.0), time.monotonic() + wait_s)
                                if i < len(models) - 1:
                                    i += 1
                                    await asyncio.sleep(min(wait_s, 5.0))
                                    continue
                                provider_dead = True
                                break

                            if resp_status >= 500:
                                _log_failure(resp_status, prov_name, m, raw)
                                if i < len(models) - 1:
                                    i += 1
                                    await asyncio.sleep(0.4)
                                    continue
                                provider_dead = True
                                break

                            if self._is_model_error(resp_status or 0, raw):
                                _log_failure(resp_status or 0, prov_name, m, raw)
                                if i < len(models) - 1:
                                    i += 1
                                    continue
                                provider_dead = True
                                break

                            _log_failure(resp_status or 0, prov_name, m, raw)
                            if i < len(models) - 1:
                                i += 1
                                continue
                            provider_dead = True
                            break

                        try:
                            data = json.loads(raw)
                        except Exception:
                            last_error = f"status=200 invalid_json provider={prov_name} body={raw[:300]}"
                            logger.warning("LLM chat 200 but invalid JSON provider=%s model=%s body=%s", prov_name, m, raw[:500])
                            if i < len(models) - 1:
                                i += 1
                                continue
                            provider_dead = True
                            break
                except Exception as e:
                    last_error = str(e)
                    logger.exception("LLM chat request failed provider=%s model=%s", prov_name, m)
                    if i < len(models) - 1:
                        i += 1
                        continue
                    provider_dead = True
                    break

                if not isinstance(data, dict):
                    if i < len(models) - 1:
                        i += 1
                        continue
                    provider_dead = True
                    break

                text = self._extract_chat_text(data)
                if text:
                    if prov_idx > 0 or i > 0 or rotations > 0:
                        logger.info("LLM chat succeeded provider=%s model=%s", prov_name, m)
                    self.last_error = None
                    if use_cache and cache_key:
                        try:
                            if len(self._cache) >= self._cache_max:
                                oldest_k = min(self._cache.items(), key=lambda kv: kv[1][0])[0]
                                self._cache.pop(oldest_k, None)
                            self._cache[cache_key] = (time.time(), text)
                        except Exception:
                            pass
                    return text

                finish_reason = None
                try:
                    choices = data.get("choices") or []
                    if choices:
                        finish_reason = (choices[0] or {}).get("finish_reason")
                except Exception:
                    finish_reason = None
                logger.warning("LLM chat returned no text provider=%s model=%s finish_reason=%s raw=%s", prov_name, m, finish_reason, str(raw)[:500])
                last_error = f"status=200 no_text provider={prov_name} finish_reason={finish_reason} body={str(raw)[:300]}"
                if i < len(models) - 1:
                    i += 1
                    continue
                provider_dead = True
                break

            if not provider_dead:
                # exhausted models loop without explicit provider_dead; move to next provider
                pass
            # try next provider

        if last_error:
            logger.warning("LLM chat exhausted all providers/models; last_error=%s", last_error)
        self.last_error = last_error
        return None

    @staticmethod
    def _extract_chat_text(data: dict[str, Any]) -> str | None:
        try:
            choices = data.get("choices") or []
            if choices:
                choice = choices[0] or {}
                msg = choice.get("message") or choice.get("delta") or {}
                content = msg.get("content")

                if isinstance(content, str):
                    text = content.strip()
                    if text:
                        return text

                if isinstance(content, list):
                    parts: list[str] = []
                    for p in content:
                        if isinstance(p, str):
                            parts.append(p)
                        elif isinstance(p, dict):
                            ptype = str(p.get("type") or "").lower()
                            if ptype in {"thinking", "thought", "reasoning", "analysis", "internal"}:
                                continue
                            # OpenAI-compatible content parts
                            for key in ("text", "output_text", "content"):
                                v = p.get(key)
                                if isinstance(v, str) and v:
                                    parts.append(v)
                                    break
                    text = "".join(parts).strip()
                    if text:
                        return text

                # Some providers put text in other fields. Never return raw reasoning/thinking.
                for k in ("text", "output_text"):
                    v = msg.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                for rk in ("reasoning_content", "reasoning", "thinking", "thought"):
                    rc = msg.get(rk)
                    if not isinstance(rc, str) or not rc.strip():
                        continue
                    rct = rc.strip()
                    # Only extract a clean JSON object from reasoning; never return thinking traces.
                    if "{" in rct:
                        start = rct.find("{")
                        end = rct.rfind("}")
                        if start != -1 and end > start:
                            candidate = rct[start:end + 1]
                            try:
                                obj = json.loads(candidate)
                                if isinstance(obj, dict):
                                    return json.dumps(obj, ensure_ascii=False)
                            except Exception:
                                repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
                                try:
                                    obj = json.loads(repaired)
                                    if isinstance(obj, dict):
                                        return json.dumps(obj, ensure_ascii=False)
                                except Exception:
                                    pass
                    # If reasoning contains a pressure-bar message, extract from bar onward only if it looks like a final message.
                    mbar = re.search(r"\[Negotiation Pressure:[^\]]*\]", rct)
                    if mbar:
                        tail = rct[mbar.start():].strip()
                        tail_head = tail[:500]
                        if not re.search(
                            r"(?i)\b(i should|i need to|i'll|i will|let me|we should|i must consider|the user wants|the prompt says|i'm going to|i am going to|my response should|here's how)\b",
                            tail_head,
                        ):
                            return tail[:4000]
                    # Otherwise ignore reasoning content.

            # Native Gemini response fallback (just in case)
            candidates = data.get("candidates") or []
            if candidates:
                cand = candidates[0] or {}
                content = cand.get("content") or {}
                parts = content.get("parts") or []
                out = []
                for p in parts:
                    if isinstance(p, dict) and isinstance(p.get("text"), str):
                        out.append(p["text"])
                text = "".join(out).strip()
                if text:
                    return text
        except Exception:
            logger.exception("LLM chat response parse failed")
        return None

    def _pressure_bar(self, pressure: int, max_pressure: int = 10) -> str:
        p = max(0, min(int(pressure), int(max_pressure)))
        pct = int(round((p / max(1, max_pressure)) * 100))
        filled = max(0, min(10, int(round((p / max(1, max_pressure)) * 10))))
        bar = "█" * filled + "░" * (10 - filled)
        return f"[Negotiation Pressure: {bar} {pct}%]"

    @staticmethod
    def _looks_like_reasoning_leak(text: Any) -> bool:
        """Detect internal planning/reasoning mistakenly emitted as user-facing text."""
        s = str(text or "").strip()
        if not s:
            return False
        # Real negotiation outputs almost always include a pressure bar or JSON structure.
        if "[Negotiation Pressure:" in s:
            return False
        if re.search(r"^\s*\{[\s\S]*\}\s*$", s):
            return False
        head = s[:700].lower()
        reasoning_markers = [
            r"\bi should consider\b", r"\bi need to evaluate\b", r"\bi need to consider\b",
            r"\blet me think\b", r"\bi will formulate\b", r"\bi'll formulate\b",
            r"\bi should respond\b", r"\bi need to respond\b", r"\bhow i should\b",
            r"\bthe prompt says\b", r"\bthe user wants\b", r"\bmy response should\b",
            r"\bi am going to\b", r"\bi'm going to\b", r"\bstep by step\b",
            r"\bchain of thought\b", r"\binternal reasoning\b", r"\bas an ai\b",
        ]
        hits = sum(1 for p in reasoning_markers if re.search(p, head))
        if hits >= 2:
            return True
        if hits >= 1 and not re.search(r"(?i)\b(wage|salary|bonus|contract|years|deal|offer|counter|accept|reject|role|signing)\b", head):
            return True
        return False

    @staticmethod
    def _clean_user_message_text(text: Any) -> str:
        """Remove JSON/code-fence artifacts from user-facing LLM messages.

        Never return an empty string when there is recoverable human-readable content.
        """
        if text is None:
            return ""
        s = str(text).strip()
        if not s:
            return ""
        # Strip reasoning/thinking blocks that some models emit in content.
        # Closed blocks: remove entirely.
        s = re.sub(
            r"(?is)<(?:think|thinking|thought|reasoning|internal_monologue|reflection)>[\s\S]*?</(?:think|thinking|thought|reasoning|internal_monologue|reflection)>",
            "",
            s,
        ).strip()
        # Unclosed thinking block at start: drop everything before a real negotiation message.
        m_unclosed = re.match(r"(?is)^\s*<(?:think|thinking|thought|reasoning|internal_monologue|reflection)>\b", s)
        if m_unclosed:
            # If there's a pressure bar after, keep from pressure bar onward.
            mbar = re.search(r"\[Negotiation Pressure:[^\]]*\]", s)
            if mbar:
                candidate = s[mbar.start():].strip()
                candidate = re.sub(
                    r"(?is)</?(?:think|thinking|thought|reasoning|internal_monologue|reflection)>",
                    "",
                    candidate,
                ).strip()
                s = candidate
            else:
                # No visible answer marker; discard as leaked thinking.
                s = ""
        # Scrub stray closing/opening think tags
        s = re.sub(r"(?is)</?(?:think|thinking|thought|reasoning|internal_monologue|reflection)>", "", s).strip()
        if not s:
            return ""
        # Scrub internal prompt/format markers that should never reach users
        s = re.sub(r"(?im)^\s*(RESPONSE FORMAT\s*\(system\)\s*:|NEGOTIATION CONTEXT\s*\(JSON\)\s*:|MANAGER MESSAGE\s*:)\s*", "", s)
        # Strip markdown fences
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
            s = re.sub(r"\s*```$", "", s).strip()

        def _try_parse(candidate: str) -> Any:
            candidate = (candidate or "").strip()
            if not candidate:
                return None
            try:
                return json.loads(candidate)
            except Exception:
                pass
            try:
                repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
                return json.loads(repaired)
            except Exception:
                return None

        def _extract(obj: Any) -> str | None:
            if isinstance(obj, dict):
                # Prefer explicit text fields
                for k in ("message", "response", "reply", "text", "content", "reason"):
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        inner = LLMService._clean_user_message_text(v)
                        if inner:
                            return inner
                # Readable fallback from structured negotiation fields (no raw JSON)
                parts: list[str] = []
                accepted = obj.get("accepted")
                walked = obj.get("walked_away") if "walked_away" in obj else obj.get("walkedAway")
                counter = obj.get("counter") if isinstance(obj.get("counter"), dict) else None
                agreed = obj.get("agreed_terms") if isinstance(obj.get("agreed_terms"), dict) else None
                if walked is True:
                    parts.append("The player's camp has walked away from negotiations.")
                elif accepted is True:
                    parts.append("The offer has been accepted.")
                elif counter:
                    parts.append("The player's camp has made a counter-offer.")
                if counter:
                    parts.append(
                        f"Counter: {LLMService._money(counter.get('wage') or 0)}/wk, {counter.get('role') or 'Rotation'} role, "
                        f"{int(counter.get('contract_years') or 0)} years, {LLMService._money(counter.get('signing_bonus') or 0)} signing bonus."
                    )
                if agreed:
                    parts.append(
                        f"Agreed terms: {LLMService._money(agreed.get('wage') or 0)}/wk, {agreed.get('role') or 'Rotation'} role, "
                        f"{int(agreed.get('contract_years') or 0)} years, {LLMService._money(agreed.get('signing_bonus') or 0)} signing bonus."
                    )
                if parts:
                    return "\n".join(parts)
                return None
            if isinstance(obj, list):
                for it in obj:
                    got = _extract(it)
                    if got:
                        return got
            return None

        parsed = _try_parse(s)
        if parsed is not None:
            extracted = _extract(parsed)
            if extracted:
                return extracted
            # Last resort: don't leak raw JSON; return a compact human-readable summary
            if isinstance(parsed, dict):
                keys = ", ".join(list(parsed.keys())[:8])
                return f"Negotiation update received ({keys})." if keys else "Negotiation update received."
            return "Negotiation update received."

        # If embedded JSON object exists, try extracting message from it
        if "{" in s and "}" in s:
            m = re.search(r"\{[\s\S]*\}", s)
            if m:
                obj = _try_parse(m.group(0))
                if isinstance(obj, dict):
                    extracted = _extract(obj)
                    if extracted:
                        prefix = s[:m.start()].strip()
                        suffix = s[m.end():].strip()
                        if prefix and not prefix.startswith(("{", "[")):
                            return (prefix + "\n\n" + extracted).strip()
                        if suffix and not suffix.startswith(("{", "[")):
                            return (extracted + "\n\n" + suffix).strip()
                        return extracted
                # JSON blob present but unreadable as message - strip the blob and keep surrounding text
                prefix = s[:m.start()].strip()
                suffix = s[m.end():].strip()
                surrounding = "\n\n".join([p for p in [prefix, suffix] if p and not p.startswith(("{", "["))]).strip()
                if surrounding:
                    return surrounding

        # If it still looks like raw JSON, avoid leaking it
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            return "Negotiation update received."
        if LLMService._looks_like_reasoning_leak(s):
            return ""
        return s

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

        def _int_or_none(*keys: str) -> int | None:
            for k in keys:
                val = player.get(k)
                if val is None or str(val).strip() == "":
                    continue
                try:
                    return int(round(float(val)))
                except Exception:
                    continue
            return None

        overall = _int_or_none("overall", "ovr", "OVR", "rating", "overall_rating")
        potential = _int_or_none("potential", "pot", "POT", "potential_rating")

        profile = {
            "name": player.get("name"),
            "age": player.get("age"),
            "position": player.get("detailed_position") or player.get("position"),
            "overall": overall if overall is not None else 0,
            "ovr": overall if overall is not None else 0,
            "potential": potential if potential is not None else 0,
            "pot": potential if potential is not None else 0,
            "nationality": player.get("nationality"),
            "height_cm": player.get("height_cm"),
            "club_status": "free_agent" if int(player.get("team_api_id") or 0) == 9000001 else ("signed" if player.get("team_api_id") else None),
            "current_wage": player.get("wage"),
            "player_status": player.get("status"),
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

    @staticmethod
    def _extract_contract_terms_from_text(text: str, *, player_age: int | None = None) -> dict[str, Any]:
        """Extract contract terms from natural language using existing NLU parser."""
        try:
            from services.nlu_service import extract_contract_terms
            out = dict(extract_contract_terms(text) or {})
        except Exception:
            out = {}
        # Guard: never mistake player age for contract years
        if player_age is not None and "contract_years" in out:
            try:
                yrs = int(out.get("contract_years") or 0)
                if yrs == int(player_age) and yrs > 5:
                    out.pop("contract_years", None)
            except Exception:
                pass
        return out

    @staticmethod
    def _detect_manager_end_intent(text: str | None) -> bool:
        t = str(text or "").strip().lower()
        if not t:
            return False
        # Explicit end/cancel/decline phrases
        patterns = [
            r"\b(no deal|not interested|walk away|i'?m done|im done|end (?:the )?(?:negotiation|talks|deal)|stop (?:the )?(?:negotiation|talks|deal)|cancel (?:the )?(?:negotiation|talks|deal)|let'?s stop|leave it|forget it|pull out)\b",
            r"\b(i )?(?:don'?t|do not|dont) want to (?:continue|proceed|negotiate|go on)\b",
            r"\b(?:decline|reject) (?:the )?(?:deal|offer|terms)\b",
        ]
        return any(re.search(p, t) for p in patterns)

    @staticmethod
    def _detect_acceptance_intent(text: str | None) -> bool:
        t = str(text or "").strip().lower()
        if not t:
            return False
        # Strong acceptance cues
        accept_patterns = [
            r"\b(we (?:have )?a deal|deal\b|agreed|agreement reached|we accept|i accept|we(?:'re| are) happy to accept|accepted|let'?s do it|done deal|shake on it)\b",
            r"\b(?:accept|agree(?:d)? to) (?:the|your|this) (?:offer|terms|deal|proposal)\b",
        ]
        if any(re.search(p, t) for p in accept_patterns):
            # Avoid false positives like "if you accept..." "would you accept..." "can't accept"
            if re.search(r"\b(if|would|could|can you|will you|please|unless|before we accept|can'?t accept|cannot accept|won'?t accept|not accept)\b", t):
                # Allow "we accept..." / "I accept..." strong forms
                if re.search(r"\b(we accept|i accept|we(?:'re| are) happy to accept|deal done|we have a deal|agreed)\b", t):
                    return True
                return False
            return True
        return False

    @staticmethod
    def _detect_walkaway_intent(text: str | None) -> bool:
        t = str(text or "").strip().lower()
        if not t:
            return False
        patterns = [
            r"\b(walk(?:ing)? away|talks? (?:are )?over|negotiations? (?:are )?over|deal is off|end(?:ing)? negotiations?|explore other options|sign elsewhere|move on|no longer interested)\b",
        ]
        return any(re.search(p, t) for p in patterns)

    @staticmethod
    def _detect_reduction_request(text: str | None) -> bool:
        t = str(text or "").strip().lower()
        if not t:
            return False
        return bool(re.search(
            r"\b(lower|reduce|decrease|cut|drop|bring down|go down|cheaper|less|more reasonable|more affordable|ease|soften)\b",
            t,
        ))

    @staticmethod
    def _detect_increase_request(text: str | None) -> bool:
        t = str(text or "").strip().lower()
        if not t:
            return False
        return bool(re.search(r"\b(increase|raise|improve|higher|bump up|go up|more|boost)\b", t))

    @classmethod
    def _interpret_nl_contract_decision(
        cls,
        *,
        message: str,
        player: dict[str, Any] | None = None,
        demand: dict[str, Any] | None = None,
        offered: dict[str, Any] | None = None,
        previous_pressure: int = 0,
        max_pressure: int = 10,
        manager_message: str | None = None,
        previous_counter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Interpret a natural-language negotiation message into structured decision fields."""
        msg = str(message or "").strip()
        demand = demand or {}
        offered = offered or {}
        player = player or {}
        prev_p = max(0, min(int(previous_pressure), int(max_pressure)))

        pressure = cls._infer_pressure_from_text(msg, previous=prev_p, max_pressure=max_pressure)
        if pressure is None:
            pressure = prev_p

        manager_ended = cls._detect_manager_end_intent(manager_message)
        # Agent-side signals
        agent_walkaway = cls._detect_walkaway_intent(msg)
        agent_accepted = cls._detect_acceptance_intent(msg)

        accepted = bool(agent_accepted) and not manager_ended
        walked_away = bool(agent_walkaway) and not accepted and not manager_ended

        if pressure >= int(max_pressure):
            walked_away = True
            accepted = False

        if manager_ended:
            accepted = False
            walked_away = False

        # Extract terms from agent message
        player_age = None
        try:
            player_age = int(player.get("age")) if player.get("age") is not None else None
        except Exception:
            player_age = None
        extracted = cls._extract_contract_terms_from_text(msg, player_age=player_age)

        # Build counter/terms
        counter = None
        agreed_terms = None
        if accepted:
            base = dict(offered) if offered else dict(demand)
            base.update({k: v for k, v in extracted.items() if v is not None})
            agreed_terms = cls._sanitize_contract_counter(base, fallback=demand or offered)
        elif not walked_away and not manager_ended:
            base = dict(previous_counter) if isinstance(previous_counter, dict) else dict(demand)
            base.update({k: v for k, v in extracted.items() if v is not None})
            counter = cls._sanitize_contract_counter(base, fallback=demand)

            # Section 4: if manager asked to lower/reduce and counter is unchanged, nudge conservatively
            if counter and isinstance(previous_counter, dict) and cls._detect_reduction_request(manager_message):
                same = all(int(counter.get(k) or 0) == int(previous_counter.get(k) or 0) for k in ("wage", "signing_bonus", "contract_years")) and str(counter.get("role")) == str(previous_counter.get("role"))
                if same:
                    nudged = dict(counter)
                    # Conservative reductions
                    if re.search(r"\b(wage|salary|week|weekly|pay)\b", str(manager_message or "").lower()):
                        nudged["wage"] = max(1000, int(round(int(nudged.get("wage") or 0) * 0.95)))
                    elif re.search(r"\b(bonus|signing)\b", str(manager_message or "").lower()):
                        nudged["signing_bonus"] = max(0, int(round(int(nudged.get("signing_bonus") or 0) * 0.90)))
                    elif re.search(r"\b(year|contract length|duration|deal)\b", str(manager_message or "").lower()):
                        nudged["contract_years"] = max(1, int(nudged.get("contract_years") or 1) - 1)
                    else:
                        # Generic "lower it down" - reduce wage a bit
                        nudged["wage"] = max(1000, int(round(int(nudged.get("wage") or 0) * 0.97)))
                        nudged["signing_bonus"] = max(0, int(round(int(nudged.get("signing_bonus") or 0) * 0.95)))
                    nudged = cls._sanitize_contract_counter(nudged, fallback=demand)
                    if nudged:
                        counter = nudged
            # If increase requested and counter unchanged, bump conservatively
            if counter and isinstance(previous_counter, dict) and cls._detect_increase_request(manager_message):
                same = all(int(counter.get(k) or 0) == int(previous_counter.get(k) or 0) for k in ("wage", "signing_bonus", "contract_years")) and str(counter.get("role")) == str(previous_counter.get("role"))
                if same:
                    nudged = dict(counter)
                    if re.search(r"\b(wage|salary|week|weekly|pay)\b", str(manager_message or "").lower()):
                        nudged["wage"] = max(1000, int(round(int(nudged.get("wage") or 0) * 1.03)))
                    elif re.search(r"\b(bonus|signing)\b", str(manager_message or "").lower()):
                        nudged["signing_bonus"] = max(0, int(round(int(nudged.get("signing_bonus") or 0) * 1.05)))
                    else:
                        nudged["wage"] = max(1000, int(round(int(nudged.get("wage") or 0) * 1.02)))
                    nudged = cls._sanitize_contract_counter(nudged, fallback=demand)
                    if nudged:
                        counter = nudged

            if counter is None:
                counter = cls._sanitize_contract_counter(demand, fallback={"wage": 100000, "role": "Rotation", "signing_bonus": 0, "contract_years": 3})

        return {
            "accepted": bool(accepted),
            "walked_away": bool(walked_away),
            "manager_ended": bool(manager_ended),
            "pressure": int(pressure),
            "counter": counter,
            "agreed_terms": agreed_terms,
        }

    @staticmethod
    def _sanitize_contract_counter(counter: Any, *, fallback: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not isinstance(counter, dict):
            return None
        fb = fallback or {}
        from services.transfer_market_service import normalize_role

        wage = LLMService._coerce_int(counter.get("wage"), LLMService._coerce_int(fb.get("wage"), 0))
        bonus = LLMService._coerce_int(counter.get("signing_bonus"), LLMService._coerce_int(fb.get("signing_bonus"), 0))
        years = LLMService._coerce_int(counter.get("contract_years"), LLMService._coerce_int(fb.get("contract_years"), 3))
        role = normalize_role(str(counter.get("role") or fb.get("role") or "Rotation")) or "Rotation"

        if wage < 1_000 or wage > 10_000_000:
            return None
        bonus = max(0, min(bonus, 500_000_000))
        years = max(1, min(years, 5))
        return {"wage": int(wage), "role": role, "signing_bonus": int(bonus), "contract_years": int(years)}

    @staticmethod
    def _infer_pressure_from_text(text: str, *, previous: int = 0, max_pressure: int = 10) -> int | None:
        t = str(text or "")
        m = re.search(r"\[Negotiation Pressure:\s*[█░\#\-\s]*\s*(\d{1,3})%\s*\]", t, re.IGNORECASE)
        if m:
            try:
                pct = max(0, min(100, int(m.group(1))))
                return max(0, min(int(max_pressure), int(round((pct / 100.0) * int(max_pressure)))))
            except Exception:
                pass
        # "pressure: 7/10", "pressure 70%"
        m2 = re.search(r"pressure\D{0,12}(\d{1,2})\s*/\s*(\d{1,2})", t, re.IGNORECASE)
        if m2:
            try:
                p = int(m2.group(1)); mx = int(m2.group(2) or 10)
                if mx > 0:
                    return max(0, min(int(max_pressure), int(round((p / mx) * int(max_pressure)))))
            except Exception:
                pass
        m3 = re.search(r"pressure\D{0,12}(\d{1,3})\s*%", t, re.IGNORECASE)
        if m3:
            try:
                pct = max(0, min(100, int(m3.group(1))))
                return max(0, min(int(max_pressure), int(round((pct / 100.0) * int(max_pressure)))))
            except Exception:
                pass
        return None

    # Text-inference decision helpers removed (pure LLM decisions).
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

        # === Original persona prompt (recovered from git commit 1d41750). Keep verbatim. ===
        persona = (
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

        # No JSON-only negotiation protocol. Use the original persona prompt and natural language.
        context = {
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
        }
        user_content = (
            "NEGOTIATION CONTEXT (JSON data for reference; reply in natural language, not JSON):\n"
            + json.dumps(context, ensure_ascii=False)
        )

        msgs = [
            {"role": "system", "content": persona},
            {"role": "user", "content": user_content},
        ]

        raw_reply = await self.chat(msgs, temperature=0.7, max_tokens=800)
        _le = str(self.last_error or "").lower()
        if not isinstance(raw_reply, str) or not raw_reply.strip():
            if any(k in _le for k in ["overall_deadline_exceeded", "exhausted all providers", "llm_disabled"]):
                return None
            raw_reply = await self.chat(msgs, temperature=0.5, max_tokens=800)

        msg = self._clean_user_message_text(raw_reply or "")
        if self._looks_like_reasoning_leak(msg):
            msg = ""
        if not msg:
            return None

        # Interpret natural language fee decision
        pressure = self._infer_pressure_from_text(msg, previous=prev_p, max_pressure=max_pressure)
        if pressure is None:
            pressure = prev_p
        pressure = max(prev_p, min(int(max_pressure), int(pressure)))

        low = msg.lower()
        walked_away = bool(self._detect_walkaway_intent(msg))
        # Acceptance signals
        accepted = bool(re.search(r"\b(accept|accepted|deal|agreement reached|we have a deal|happy to accept)\b", low))
        # Avoid false positives
        if re.search(r"\b(can'?t accept|cannot accept|won'?t accept|not accept|unable to accept|reject|decline|refuse)\b", low):
            # but allow explicit "we accept" strong form
            if not re.search(r"\b(we accept|i accept|we(?:'re| are) happy to accept|we have a deal)\b", low):
                accepted = False
        if re.search(r"\b(reject|decline|refuse|not acceptable|too low|insufficient|unacceptable)\b", low) and not re.search(r"\b(we accept|we have a deal)\b", low):
            accepted = False

        if pressure >= int(max_pressure):
            walked_away = True
            accepted = False
        if walked_away:
            accepted = False
        if accepted:
            walked_away = False

        bar = self._pressure_bar(pressure, max_pressure)
        bar_idx = msg.find("[Negotiation Pressure:")
        if bar_idx == -1:
            msg = f"{bar}\n{msg}"
        else:
            if bar_idx > 0:
                # Drop any preamble before the pressure bar (reasoning/planning leak guard).
                msg = msg[bar_idx:].lstrip()
            msg = re.sub(r"^\[Negotiation Pressure:[^\]]*\]", bar, msg, count=1, flags=re.MULTILINE)

        return {
            "accepted": bool(accepted),
            "walked_away": bool(walked_away),
            "pressure": int(pressure),
            "pressure_bar": bar,
            "message": msg[:3500],
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
        user_message: str | None = None,
        offered_is_explicit: bool = True,
        history: list[dict[str, Any]] | None = None,
        previous_counter: dict[str, Any] | None = None,
        previous_offer: dict[str, Any] | None = None,
        negotiation_stage: str | None = None,
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

        # === Original persona prompts (recovered from git commit 1d41750). Keep verbatim. ===
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
                "THE ROLEPLAY MUST BE EXTREMELY REALISTIC, FIRM, AND STRICT. "
                "You MUST respect the provided decision and counter terms exactly. Do not change numbers, do not accept if accepted is false, do not invent deals. "
                "Do not mention being an AI. Reply in plain text, 2-5 short paragraphs max. Start with the pressure bar on its own line. "
                "If counter is provided, clearly state the counter-demand with exact numbers. "
                "If accepted, clearly confirm agreement and state the agreed terms exactly. "
                "If walked away, clearly state negotiations are over and the player will explore other options."
            )
        else:
            persona = (
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

        # No JSON-only negotiation protocol. Use the original persona prompt and natural language.
        context = {
            "scenario": sc,
            "player": profile,
            "selling_club": selling,
            "buying_club": buying,
            "demand": demand,
            "offered": offered,
            "previous_manager_offer": previous_offer if isinstance(previous_offer, dict) else None,
            "previous_agent_counteroffer": previous_counter if isinstance(previous_counter, dict) else None,
            "offered_is_explicit": bool(offered_is_explicit),
            "previous_pressure": prev_p,
            "max_pressure": int(max_pressure),
            "previous_pressure_bar": bar_hint,
            "round_no": int(round_no),
            "is_free_agent": bool(is_free_agent),
            "negotiation_stage": str(negotiation_stage or ("free_agent" if is_free_agent else "contract_after_fee_agreed")),
        }
        current_user_msg = str(user_message or "").strip()
        user_content = (
            "NEGOTIATION CONTEXT (JSON data for reference; reply in natural language, not JSON):\n"
            + json.dumps(context, ensure_ascii=False)
            + "\n\nMANAGER MESSAGE:\n"
            + (current_user_msg if current_user_msg else "(no message provided)")
        )

        msgs: list[dict[str, str]] = [{"role": "system", "content": persona}]
        if isinstance(history, list):
            for h in history[-12:]:
                if not isinstance(h, dict):
                    continue
                role = str(h.get("role") or "").strip().lower()
                content = str(h.get("content") or "").strip()
                if role in ("user", "assistant") and content:
                    msgs.append({"role": role, "content": content[:3000]})
        msgs.append({"role": "user", "content": user_content})

        raw_reply = await self.chat(msgs, temperature=0.7, max_tokens=900)
        _le = str(self.last_error or "").lower()
        if not isinstance(raw_reply, str) or not raw_reply.strip():
            if any(k in _le for k in ["overall_deadline_exceeded", "exhausted all providers", "llm_disabled"]):
                return None
            # one retry
            raw_reply = await self.chat(msgs, temperature=0.5, max_tokens=900)

        msg = self._clean_user_message_text(raw_reply or "")
        if self._looks_like_reasoning_leak(msg):
            msg = ""
        if not msg:
            return None

        # Interpret natural language decision
        decision = self._interpret_nl_contract_decision(
            message=msg,
            player=player,
            demand=demand,
            offered=offered,
            previous_pressure=prev_p,
            max_pressure=int(max_pressure),
            manager_message=current_user_msg,
            previous_counter=previous_counter if isinstance(previous_counter, dict) else None,
        )

        pressure = int(decision.get("pressure") or prev_p)
        pressure = max(prev_p, min(int(max_pressure), pressure))
        accepted = bool(decision.get("accepted"))
        walked_away = bool(decision.get("walked_away"))
        manager_ended = bool(decision.get("manager_ended"))
        counter = decision.get("counter") if isinstance(decision.get("counter"), dict) else None
        agreed_terms = decision.get("agreed_terms") if isinstance(decision.get("agreed_terms"), dict) else None

        # Guardrails
        if manager_ended:
            accepted = False
            walked_away = False
            counter = None
            agreed_terms = None
        else:
            if pressure >= int(max_pressure):
                walked_away = True
                accepted = False
            if walked_away and pressure < int(max_pressure):
                # User rule: allow contract negotiation to continue until pressure reaches 100%.
                walked_away = False
                pressure = min(max(int(pressure), int(prev_p)), int(max_pressure) - 1)
                accepted = False
            if walked_away:
                accepted = False
                counter = None
                agreed_terms = None
            if accepted:
                walked_away = False
                manager_ended = False
                counter = None

        if not accepted and not walked_away and not manager_ended:
            if not isinstance(counter, dict):
                base = dict(previous_counter) if isinstance(previous_counter, dict) else dict(demand)
                # Try extracting from message again
                extracted = self._extract_contract_terms_from_text(msg, player_age=(int(player.get("age")) if str(player.get("age") or "").isdigit() else None))
                base.update({k: v for k, v in extracted.items() if v is not None})
                counter = self._sanitize_contract_counter(base, fallback=demand)
            if not isinstance(counter, dict):
                counter = self._sanitize_contract_counter(demand, fallback={"wage": 100000, "role": "Rotation", "signing_bonus": 0, "contract_years": 3})

        if accepted and not isinstance(agreed_terms, dict):
            base = dict(offered) if offered else dict(demand)
            extracted = self._extract_contract_terms_from_text(msg, player_age=(int(player.get("age")) if str(player.get("age") or "").isdigit() else None))
            base.update({k: v for k, v in extracted.items() if v is not None})
            agreed_terms = self._sanitize_contract_counter(base, fallback=offered or demand)

        # Ensure pressure bar present and canonical
        bar = self._pressure_bar(pressure, max_pressure)
        bar_idx = msg.find("[Negotiation Pressure:")
        if bar_idx == -1:
            msg = f"{bar}\n{msg}"
        else:
            if bar_idx > 0:
                # Drop any preamble before the pressure bar (reasoning/planning leak guard).
                msg = msg[bar_idx:].lstrip()
            msg = re.sub(r"^\[Negotiation Pressure:[^\]]*\]", bar, msg, count=1, flags=re.MULTILINE)

        # Ensure key terms visible on acceptance/counter
        if accepted and isinstance(agreed_terms, dict):
            wage_fmt = self._money(agreed_terms.get("wage"))
            if wage_fmt not in msg and str(int(agreed_terms.get("wage") or 0)) not in msg.replace(",", ""):
                msg += (
                    f"\n\n**Agreed terms:** {self._money(agreed_terms.get('wage'))}/wk, {agreed_terms.get('role')}, "
                    f"{int(agreed_terms.get('contract_years') or 0)} years, {self._money(agreed_terms.get('signing_bonus'))} signing bonus."
                )
        if counter and not accepted and not walked_away and not manager_ended:
            wage_fmt = self._money(counter.get("wage"))
            if wage_fmt not in msg and str(int(counter.get("wage") or 0)) not in msg.replace(",", ""):
                msg += (
                    f"\n\n**Counter-demand:** {self._money(counter.get('wage'))}/wk, {counter.get('role')}, "
                    f"{int(counter.get('contract_years') or 0)} years, {self._money(counter.get('signing_bonus'))} signing bonus."
                )

        return {
            "accepted": bool(accepted),
            "walked_away": bool(walked_away),
            "manager_ended": bool(manager_ended),
            "pressure": int(pressure),
            "pressure_bar": bar,
            "counter": counter,
            "agreed_terms": agreed_terms,
            "message": msg[:3500],
        }

    async def generate_contract_opening(
        self,
        *,
        player: dict[str, Any],
        demand: dict[str, Any],
        valuation: dict[str, Any] | None = None,
        buying_team_name: str | None = None,
        selling_team_name: str | None = None,
        is_free_agent: bool = False,
        scenario: str | None = None,
    ) -> str | None:
        """LLM-generated opening contract negotiation message (uses prompt #2 or #3 persona)."""
        if not self.enabled:
            return None

        profile = self._player_profile(player, valuation)
        player_name = str(player.get("name") or "The player")
        buying = str(buying_team_name or "the club")
        selling = str(selling_team_name or "the selling club")
        bar = self._pressure_bar(0, 10)

        sc = (scenario or "").strip().lower()
        if not sc:
            sc = "free_agent" if is_free_agent else "contract_after_fee_agreed"

        # === Original persona prompts (recovered from git commit 1d41750). Keep verbatim. ===
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
                "THE ROLEPLAY MUST BE EXTREMELY REALISTIC, FIRM, AND STRICT. "
                "You MUST respect the provided decision and counter terms exactly. Do not change numbers, do not accept if accepted is false, do not invent deals. "
                "Do not mention being an AI. Reply in plain text, 2-5 short paragraphs max. Start with the pressure bar on its own line. "
                "If counter is provided, clearly state the counter-demand with exact numbers. "
                "If accepted, clearly confirm agreement and state the agreed terms exactly. "
                "If walked away, clearly state negotiations are over and the player will explore other options."
            )
        else:
            persona = (
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

        # No JSON-only negotiation protocol. Use the original persona prompt and natural language.
        context = {
            "scenario": sc,
            "player": profile,
            "selling_club": selling,
            "buying_club": buying,
            "demand": demand,
            "pressure": 0,
            "max_pressure": 10,
            "pressure_bar": bar,
            "is_free_agent": bool(is_free_agent),
        }
        user_content = (
            "NEGOTIATION CONTEXT (JSON data for reference; reply in natural language, not JSON):\n"
            + json.dumps(context, ensure_ascii=False)
            + "\n\nThis is the OPENING message of the negotiation. State the opening demands clearly with exact numbers."
        )

        msgs = [
            {"role": "system", "content": persona},
            {"role": "user", "content": user_content},
        ]

        raw_reply = await self.chat(msgs, temperature=0.7, max_tokens=800)
        _le = str(self.last_error or "").lower()
        if not isinstance(raw_reply, str) or not raw_reply.strip():
            if any(k in _le for k in ["overall_deadline_exceeded", "exhausted all providers", "llm_disabled"]):
                return None
            raw_reply = await self.chat(msgs, temperature=0.5, max_tokens=800)

        msg = self._clean_user_message_text(raw_reply or "")
        if self._looks_like_reasoning_leak(msg):
            msg = ""
        if not msg:
            return None

        bar_idx = msg.find("[Negotiation Pressure:")
        if bar_idx == -1:
            msg = f"{bar}\n{msg}"
        else:
            if bar_idx > 0:
                # Drop any preamble before the pressure bar (reasoning/planning leak guard).
                msg = msg[bar_idx:].lstrip()
            msg = re.sub(r"^\[Negotiation Pressure:[^\]]*\]", bar, msg, count=1, flags=re.MULTILINE)

        # Ensure key demand numbers appear
        wage = self._coerce_int(demand.get("wage"), 0)
        bonus = self._coerce_int(demand.get("signing_bonus"), 0)
        years = self._coerce_int(demand.get("contract_years"), 0)
        wage_fmt = self._money(wage)
        bonus_fmt = self._money(bonus)
        missing_bits = []
        if wage > 0 and str(wage) not in msg.replace(",", "") and wage_fmt not in msg:
            missing_bits.append(f"{wage_fmt}/wk")
        if bonus > 0 and str(bonus) not in msg.replace(",", "") and bonus_fmt not in msg:
            missing_bits.append(f"{bonus_fmt} signing bonus")
        if years > 0 and str(years) not in msg:
            missing_bits.append(f"{years} years")
        if missing_bits or (demand.get("role") and str(demand.get("role")) not in msg):
            msg += (
                f"\n\n**Opening demands:** {self._money(wage)}/wk • {demand.get('role')} role • "
                f"{int(years)} years • {self._money(bonus)} signing bonus."
            )
        return msg[:3500]

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
