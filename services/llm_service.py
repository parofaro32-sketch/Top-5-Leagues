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
            timeout = aiohttp.ClientTimeout(total=max(5, int(self.settings.llm_timeout or 20)))
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

        def _log_failure(status: int, prov: str, mdl: str, raw: str) -> None:
            logger.warning("LLM chat failed status=%s provider=%s model=%s body=%s", status, prov, mdl, (raw or "")[:500])

        def _is_quota(status: int, raw: str) -> bool:
            if status != 429:
                return False
            body_l = (raw or "").lower()
            return any(k in body_l for k in ["quota", "resource_exhausted", "rate limit exceeded", "too many requests"])

        for prov_idx, prov in enumerate(providers):
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
                    # Also try requested model on fallback providers (some are cross-compatible, e.g. OpenRouter).
                    models = [requested_model, *models]

            i = 0
            rotations = 0
            provider_dead = False
            while i < len(models):
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
                            # OpenAI-compatible content parts
                            for key in ("text", "output_text", "content"):
                                v = p.get(key)
                                if isinstance(v, str) and v:
                                    parts.append(v)
                                    break
                    text = "".join(parts).strip()
                    if text:
                        return text

                # Some providers put text in other fields. Avoid reasoning_content unless it
                # clearly contains JSON/message (thinking traces poison JSON parsing).
                for k in ("text", "output_text"):
                    v = msg.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                rc = msg.get("reasoning_content")
                if isinstance(rc, str) and rc.strip():
                    rct = rc.strip()
                    if "{" in rct or "[Negotiation Pressure:" in rct:
                        return rct

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

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 900,
        model: str | None = None,
    ) -> dict[str, Any] | None:
        """Request a JSON object response. Returns parsed dict or None.

        Robustness: tries response_format then plain. If the model returns plain text that
        is not JSON, salvages it as {"message": text} so callers can still use the LLM reply.
        """
        def _strip_fences(t: str) -> str:
            t = str(t or "").strip()
            if t.startswith("```"):
                t = t.strip("`")
                nl = t.find("\n")
                if nl != -1 and t[:nl].strip().lower() in {"json", "javascript", "js"}:
                    t = t[nl + 1 :].strip()
            return t.strip()

        def _try_parse(s: str) -> dict[str, Any] | None:
            if not s:
                return None
            s = s.strip()
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, dict) else None
            except Exception:
                pass
            # Repair trailing commas
            try:
                repaired = re.sub(r",\s*([}\]])", r"\1", s)
                obj = json.loads(repaired)
                return obj if isinstance(obj, dict) else None
            except Exception:
                pass
            # Repair truncated JSON by closing open brackets/braces
            try:
                repaired = re.sub(r",\s*([}\]])", r"\1", s)
                # Trim trailing incomplete string/value
                repaired2 = re.sub(r',\s*"[^"]*"?\s*:?\s*"?[^"{}\[\]]*$', '', repaired)
                opens = repaired2.count("{") - repaired2.count("}")
                open_br = repaired2.count("[") - repaired2.count("]")
                if opens > 0 or open_br > 0:
                    repaired2 = repaired2.rstrip().rstrip(",")
                    repaired2 += "]" * max(0, open_br) + "}" * max(0, opens)
                    obj = json.loads(repaired2)
                    return obj if isinstance(obj, dict) else None
            except Exception:
                pass
            return None

        def _parse_text(raw_text: str | None) -> dict[str, Any] | None:
            t = _strip_fences(str(raw_text or ""))
            if not t:
                return None
            parsed = _try_parse(t)
            if parsed is not None:
                return parsed
            start = t.find("{")
            end = t.rfind("}")
            if start != -1 and end != -1 and end > start:
                parsed = _try_parse(t[start : end + 1])
                if parsed is not None:
                    return parsed
            return None

        def _salvage_plain(raw_text: str | None) -> dict[str, Any] | None:
            t = _strip_fences(str(raw_text or ""))
            if not t:
                return None
            # If it looks like JSON-ish but failed to parse, try to extract a "message" field
            m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', t, re.DOTALL | re.IGNORECASE)
            if m:
                try:
                    msg = json.loads('"' + m.group(1) + '"')
                except Exception:
                    msg = m.group(1).replace('\\"', '"').replace('\\n', '\n')
                if isinstance(msg, str) and msg.strip():
                    d: dict[str, Any] = {"message": msg.strip()}
                    # try to pull other fields
                    for k in ("accepted", "walked_away", "pressure"):
                        km = re.search(rf'"{k}"\s*:\s*(true|false|null|-?\d+)', t, re.IGNORECASE)
                        if km:
                            val = km.group(1).lower()
                            if val == "true":
                                d[k] = True
                            elif val == "false":
                                d[k] = False
                            elif val == "null":
                                d[k] = None
                            else:
                                try:
                                    d[k] = int(val)
                                except Exception:
                                    pass
                    return d
            # Plain text response - use it as the message. The caller can infer defaults.
            # Strip any leading/trailing markdown fences leftover
            clean = LLMService._clean_user_message_text(t) or t.strip()
            if len(clean) >= 10:
                return {"message": clean[:4000], "_salvaged_plain": True}
            return None

        text1 = await self.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            response_format={"type": "json_object"},
        )
        parsed = _parse_text(text1)
        if parsed is not None:
            return parsed
        salvaged1 = _salvage_plain(text1)
        if isinstance(salvaged1, dict) and salvaged1.get("message"):
            logger.info("LLM chat_json salvaged plain-text response from first call (len=%s)", len(str(salvaged1.get("message") or "")))
            return salvaged1

        text2 = await self.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
        )
        parsed = _parse_text(text2)
        if parsed is not None:
            return parsed
        salvaged2 = _salvage_plain(text2)
        if isinstance(salvaged2, dict) and salvaged2.get("message"):
            logger.info("LLM chat_json salvaged plain-text response from second call (len=%s)", len(str(salvaged2.get("message") or "")))
            return salvaged2

        last_t = str(text2 or text1 or "").strip()
        logger.warning("LLM chat_json could not parse or salvage: %s", last_t[:300])
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
    def _clean_user_message_text(text: Any) -> str:
        """Remove JSON/code-fence artifacts from user-facing LLM messages.

        Never return an empty string when there is recoverable human-readable content.
        """
        if text is None:
            return ""
        s = str(text).strip()
        if not s:
            return ""
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
                        f"{LLMService._money(counter.get('signing_bonus') or 0)} signing bonus, {int(counter.get('contract_years') or 0)} years."
                    )
                if agreed:
                    parts.append(
                        f"Agreed terms: {LLMService._money(agreed.get('wage') or 0)}/wk, {agreed.get('role') or 'Rotation'} role, "
                        f"{LLMService._money(agreed.get('signing_bonus') or 0)} signing bonus, {int(agreed.get('contract_years') or 0)} years."
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

    @staticmethod
    def _infer_walkaway_from_text(text: str) -> bool:
        t = str(text or "").lower()
        patterns = [
            r"\bwalk(?:ing)? away\b", r"\bend(?:ing)? (?:the )?negotiat", r"\btalks? (?:are )?over\b",
            r"\bdeal is off\b", r"\bno deal\b", r"\bpull(?:ing)? out\b", r"\bleav(?:e|ing) the table\b",
            r"\bnot negotiating any further\b", r"\bnegotiations? (?:have )?collapsed\b",
        ]
        return any(re.search(p, t) for p in patterns)

    @staticmethod
    def _infer_accept_from_text(text: str) -> bool:
        t = str(text or "").lower()
        # Avoid false positives like "cannot accept"
        if re.search(r"\b(can(?:not|'t)|won't|will not|unable to|refuse to|do not|don't)\b.{0,20}\baccept\b", t):
            return False
        patterns = [
            r"\bwe (?:can )?accept\b", r"\bi (?:can )?accept\b", r"\bwe(?:'re| are) happy to accept\b",
            r"\bdeal(?: is)? accepted\b", r"\baccepted? (?:the|your) (?:offer|fee|bid|terms)\b",
            r"\bwe have a deal\b", r"\bagree(?:d)? to (?:the|your) (?:offer|fee|bid|terms)\b",
            r"\bfee is accepted\b", r"\boffer is accepted\b",
        ]
        return any(re.search(p, t) for p in patterns)

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
            max_tokens=1300,
        )
        if not isinstance(data, dict):
            data = await self.chat_json(
                [
                    {"role": "system", "content": sys + " Return ONLY valid JSON. No markdown, no extra text."},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                max_tokens=1300,
            )
        if not isinstance(data, dict):
            # Last resort: plain chat and salvage text as message
            plain = await self.chat(
                [
                    {"role": "system", "content": sys.replace("Return ONLY a JSON object with keys: accepted (boolean), walked_away (boolean), pressure (integer 0-10), message (string). ", "")},
                    {"role": "user", "content": user},
                ],
                temperature=0.5,
                max_tokens=800,
            )
            if isinstance(plain, str) and plain.strip():
                cleaned_plain = self._clean_user_message_text(plain.strip()) or plain.strip()
                data = {"message": cleaned_plain, "_salvaged_plain": True}
        if not isinstance(data, dict):
            return None

        # Unnest common wrappers
        if not any(k in data for k in ("accepted", "accept", "message", "response", "walked_away", "walkedAway")):
            for k in ("decision", "result", "negotiation", "output", "data"):
                v = data.get(k)
                if isinstance(v, dict):
                    data = v
                    break

        def _pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
            for k in keys:
                if k in d and d.get(k) is not None:
                    return d.get(k)
            return default

        msg = self._clean_user_message_text(_pick(data, "message", "response", "reply", "text", default="") or "")

        # Infer from message when fields are missing or salvaged
        has_accepted_key = any(k in data for k in ("accepted", "accept", "is_accepted", "isAccepted"))
        has_walked_key = any(k in data for k in ("walked_away", "walkedAway", "walk_away", "walkaway"))
        has_pressure_key = any(k in data for k in ("pressure", "pressure_level", "pressure_pct"))

        inferred_pressure = self._infer_pressure_from_text(msg, previous=prev_p, max_pressure=max_pressure)
        if not has_pressure_key and inferred_pressure is not None:
            pressure_raw = inferred_pressure
        else:
            pressure_raw = self._coerce_int(_pick(data, "pressure", "pressure_level", "pressure_pct", default=prev_p), prev_p)
        pressure = max(prev_p, min(int(max_pressure), int(pressure_raw)))

        if has_walked_key:
            walked_away = self._coerce_bool(_pick(data, "walked_away", "walkedAway", "walk_away", "walkaway", default=False), False)
        else:
            walked_away = self._infer_walkaway_from_text(msg)

        if has_accepted_key:
            accepted = self._coerce_bool(_pick(data, "accepted", "accept", "is_accepted", "isAccepted", default=False), False)
        else:
            accepted = self._infer_accept_from_text(msg) if not walked_away else False

        if pressure >= int(max_pressure):
            walked_away = True
            accepted = False
        if walked_away:
            accepted = False
        if accepted:
            walked_away = False

        if not msg:
            # Build a minimal message if the model gave us a decision but no text
            bar_tmp = self._pressure_bar(pressure, max_pressure)
            if accepted:
                msg = f"{bar_tmp}\nWe accept your offer of {self._money(offer_fee)} for {player.get('name') or 'the player'}."
            elif walked_away:
                msg = f"{bar_tmp}\nNegotiations are over. We are walking away from this deal."
            else:
                msg = f"{bar_tmp}\nWe reject your offer of {self._money(offer_fee)}. It does not meet our valuation."

        bar = self._pressure_bar(pressure, max_pressure)
        if "[Negotiation Pressure:" not in msg:
            msg = f"{bar}\n{msg}"
        else:
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
        user_message: str | None = None,
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
            "agreed_terms (object|null with wage, role, signing_bonus, contract_years - set when accepted is true, otherwise null), "
            "message (string). "
            "Rules: pressure must be >= previous_pressure unless there is strong reason, never above 10. "
            "If pressure >= 10 or walked_away is true, accepted must be false, counter must be null, agreed_terms must be null, and message must clearly end negotiations. "
            "If accepted is true, walked_away must be false, counter must be null, agreed_terms must contain the exact agreed terms, and message must clearly confirm agreement. "
            "If accepted is false and walked_away is false, counter must be present and message must clearly state the counter-demand with exact numbers. "
            "message must start with the negotiation pressure bar in the format [Negotiation Pressure: ███░░░░░░░ XX%]. "
            "message should be 2-5 short paragraphs. Be realistic and strict; use OVR/POT/market value in reasoning. "
            "IMPORTANT: The user's message is provided as user_message. Interpret it as natural language, even if informal, partial, conversational, or vague "
            "(e.g. 'can we lower salary to like 100k', 'what about 150k', 'make it 3 years', 'throw in a bigger bonus', 'ok deal'). "
            "Use user_message together with the offered fields to understand what the manager is proposing. "
            "If the message is a question, a vague proposal, or missing details, respond in character as the agent, explain what you need or make a firm counter - "
            "but still return a valid JSON decision (counter or walked_away as appropriate). Never refuse to understand; always reply in character."
        )

        user = json.dumps({
            "scenario": sc,
            "player": profile,
            "selling_club": selling,
            "buying_club": buying,
            "demand": demand,
            "offered": offered,
            "user_message": str(user_message or "").strip(),
            "previous_pressure": prev_p,
            "max_pressure": int(max_pressure),
            "previous_pressure_bar": bar_hint,
            "round_no": int(round_no),
            "is_free_agent": bool(is_free_agent),
        }, ensure_ascii=False)

        data = await self.chat_json(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.7,
            max_tokens=1300,
        )
        if not isinstance(data, dict):
            data = await self.chat_json(
                [
                    {"role": "system", "content": sys + " Return ONLY valid JSON. No markdown, no extra text."},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                max_tokens=1300,
            )
        if not isinstance(data, dict):
            # Last resort: plain chat and salvage
            plain = await self.chat(
                [
                    {"role": "system", "content": sys.replace("Return ONLY a JSON object with keys: accepted (boolean), walked_away (boolean), pressure (integer 0-10), counter (object|null with wage integer euros/week, role one of Crucial/Important/Rotation/Sporadic/Prospect, signing_bonus integer euros, contract_years integer 1-5), message (string). ", "")},
                    {"role": "user", "content": user},
                ],
                temperature=0.5,
                max_tokens=800,
            )
            if isinstance(plain, str) and plain.strip():
                cleaned_plain = self._clean_user_message_text(plain.strip()) or plain.strip()
                data = {"message": cleaned_plain, "_salvaged_plain": True}

        if not isinstance(data, dict):
            return None
        if not any(k in data for k in ("accepted", "accept", "message", "response", "walked_away", "walkedAway")):
            for k in ("decision", "result", "negotiation", "output", "data"):
                v = data.get(k)
                if isinstance(v, dict):
                    data = v
                    break

        def _pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
            for k in keys:
                if k in d and d.get(k) is not None:
                    return d.get(k)
            return default

        msg = self._clean_user_message_text(_pick(data, "message", "response", "reply", "text", default="") or "")

        has_accepted_key = any(k in data for k in ("accepted", "accept", "is_accepted", "isAccepted"))
        has_walked_key = any(k in data for k in ("walked_away", "walkedAway", "walk_away", "walkaway"))
        has_pressure_key = any(k in data for k in ("pressure", "pressure_level", "pressure_pct"))

        inferred_pressure = self._infer_pressure_from_text(msg, previous=prev_p, max_pressure=max_pressure)
        if not has_pressure_key and inferred_pressure is not None:
            pressure_raw = inferred_pressure
        else:
            pressure_raw = self._coerce_int(_pick(data, "pressure", "pressure_level", "pressure_pct", default=prev_p), prev_p)
        pressure = max(prev_p, min(int(max_pressure), int(pressure_raw)))

        if has_walked_key:
            walked_away = self._coerce_bool(_pick(data, "walked_away", "walkedAway", "walk_away", "walkaway", default=False), False)
        else:
            walked_away = self._infer_walkaway_from_text(msg)

        if has_accepted_key:
            accepted = self._coerce_bool(_pick(data, "accepted", "accept", "is_accepted", "isAccepted", default=False), False)
        else:
            accepted = self._infer_accept_from_text(msg) if not walked_away else False

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
                rc = raw_counter if isinstance(raw_counter, dict) else {}
                merged = dict(demand or {})
                merged.update({k: v for k, v in rc.items() if v is not None})
                counter = self._sanitize_contract_counter(merged, fallback=demand)
            if counter is None:
                # Last resort: keep LLM's decision to counter, fill from demand
                counter = self._sanitize_contract_counter(demand, fallback={
                    "wage": 100000, "role": "Rotation", "signing_bonus": 0, "contract_years": 3
                })

        if not msg:
            bar_tmp = self._pressure_bar(pressure, max_pressure)
            if accepted:
                msg = (
                    f"{bar_tmp}\nWe accept your offer: {self._money(offered.get('wage'))}/wk, "
                    f"{offered.get('role')}, {self._money(offered.get('signing_bonus'))} signing bonus, "
                    f"{int(offered.get('contract_years') or 0)} years."
                )
            elif walked_away:
                msg = f"{bar_tmp}\nNegotiations are over. The player's camp is walking away."
            elif counter:
                msg = (
                    f"{bar_tmp}\nYour offer is not acceptable. Our counter-demand is "
                    f"{self._money(counter.get('wage'))}/wk, {counter.get('role')}, "
                    f"{self._money(counter.get('signing_bonus'))} signing bonus, {int(counter.get('contract_years') or 0)} years."
                )
            else:
                msg = f"{bar_tmp}\nYour offer is not acceptable. We need improved terms."

        bar = self._pressure_bar(pressure, max_pressure)
        if "[Negotiation Pressure:" not in msg:
            msg = f"{bar}\n{msg}"
        else:
            lines = msg.splitlines()
            if lines and "[Negotiation Pressure:" in lines[0]:
                lines[0] = bar
                msg = "\n".join(lines)

        # Parse agreed terms if LLM accepted
        agreed_terms = None
        if accepted:
            raw_agreed = _pick(data, "agreed_terms", "agreedTerms", "agreed", "accepted_terms", "final_terms", default=None)
            agreed_terms = self._sanitize_contract_counter(raw_agreed, fallback=offered)
            if agreed_terms is None:
                agreed_terms = self._sanitize_contract_counter(offered, fallback=demand)
            offered_wage = self._money((agreed_terms or offered).get("wage"))
            if offered_wage not in msg and str(self._coerce_int((agreed_terms or offered).get("wage"), 0)) not in msg.replace(",", ""):
                _at = agreed_terms or offered
                msg += (
                    f"\n\n**Agreed terms:** {self._money(_at.get('wage'))}/wk, {_at.get('role')}, "
                    f"{self._money(_at.get('signing_bonus'))} signing bonus, {int(_at.get('contract_years') or 0)} years."
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
            "agreed_terms": agreed_terms,
            "message": msg[:1900],
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
            "This is the OPENING message of the contract negotiation. Do not mention being an AI. "
            "Return ONLY a JSON object with keys: message (string). "
            "The message must start with the negotiation pressure bar [Negotiation Pressure: ░░░░░░░░░░ 0%]. "
            "Clearly state the opening demands with exact numbers: weekly wage, role, signing bonus, contract years. "
            "Use the provided demand values exactly. Reference the player's OVR/POT/market value. "
            "Be realistic, firm, and strict. 2-5 short paragraphs."
        )
        user = json.dumps({
            "scenario": sc,
            "player": profile,
            "selling_club": selling,
            "buying_club": buying,
            "demand": demand,
            "pressure": 0,
            "max_pressure": 10,
            "pressure_bar": bar,
            "is_free_agent": bool(is_free_agent),
        }, ensure_ascii=False)

        data = await self.chat_json(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.7,
            max_tokens=1300,
        )
        if not isinstance(data, dict):
            data = await self.chat_json(
                [
                    {"role": "system", "content": sys + " Return ONLY valid JSON. No markdown, no extra text."},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                max_tokens=1300,
            )

        msg = ""
        if isinstance(data, dict):
            def _pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
                for k in keys:
                    if k in d and d.get(k) is not None:
                        return d.get(k)
                return default
            msg = self._clean_user_message_text(_pick(data, "message", "response", "reply", "text", default="") or "")

        if not msg:
            # Last resort: ask for plain text opening
            plain = await self.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            persona + " "
                            "This is the OPENING message of the contract negotiation. Do not mention being an AI. "
                            f"Start with the negotiation pressure bar [Negotiation Pressure: ░░░░░░░░░░ 0%]. "
                            "Clearly state opening demands with exact numbers: weekly wage, role, signing bonus, contract years. "
                            "Use the provided demand values exactly. Reference the player's OVR/POT/market value. "
                            "Be realistic, firm, and strict. 2-5 short paragraphs. Return only the message text."
                        ),
                    },
                    {"role": "user", "content": user},
                ],
                temperature=0.6,
                max_tokens=1000,
            )
            if isinstance(plain, str) and plain.strip():
                msg = self._clean_user_message_text(plain.strip()) or plain.strip()

        if not msg:
            return None

        if "[Negotiation Pressure:" not in msg:
            msg = f"{bar}\n{msg}"
        else:
            lines = msg.splitlines()
            if lines and "[Negotiation Pressure:" in lines[0]:
                lines[0] = bar
                msg = "\n".join(lines)

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
                f"{self._money(bonus)} signing bonus • {int(years)} years."
            )
        return msg[:1900]

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
