from __future__ import annotations

import json
import logging
import re
from typing import Any

from engine.models import default_tactics

logger = logging.getLogger(__name__)

# Strict allowlist: only these sections/fields/values can affect engine behavior.
ALLOWED = {
    "attacking": {
        "mentality": {"cautious", "balanced", "positive", "attacking", "very_attacking"},
        "build_up": {"short", "mixed", "direct", "long"},
        "passing_directness": {"short", "mixed", "direct"},
        "possession_style": {"patient", "balanced", "fast"},
        "attacking_width": {"narrow", "balanced", "wide"},
        "crossing": {"low", "mixed", "high"},
        "counterattacking": {True, False},
        "attacking_transitions": {"slow", "balanced", "fast"},
        "overlapping_fullbacks": {True, False},
        "underlapping_movements": {True, False},
    },
    "defensive": {
        "defensive_line": {"deep", "standard", "high", "very_high"},
        "pressing_intensity": {"low", "medium", "high", "very_high"},
        "pressing_triggers": {"conservative", "balanced", "aggressive"},
        "defensive_width": {"narrow", "balanced", "wide"},
        "compactness": {"low", "balanced", "high"},
        "defensive_block": {"low", "mid", "high"},
        "counterpressing": {True, False},
        "marking": {"zonal", "man", "mixed"},
        "offside_trap": {True, False},
    },
    "set_pieces": {
        "corners": {"short", "mixed", "far_post", "near_post", "crowd_keeper"},
        "free_kicks": {"short", "mixed", "direct", "cross"},
    },
}

# Top-level keys that are allowed to pass through normalize_tactics (non-behavioral hints).
ALLOWED_TOP_LEVEL = {"formation", "attacking", "defensive", "set_pieces", "individual"}

# Keys that must NEVER be accepted from user/LLM input (engine outcome manipulation).
FORBIDDEN_KEYS = {
    "score", "goals", "home_score", "away_score", "red_card", "yellow_card", "cards",
    "force_win", "win", "lose", "result", "penalty_awarded", "award_penalty", "referee",
    "bias", "cheat", "script", "outcome", "fix", "injure", "injury_force", "kill",
    "owner", "admin", "system", "prompt", "instructions", "ignore", "override",
}

ALLOWED_FORMATIONS = {
    "3-4-3","3-5-2","3-4-1-2","4-1-4-1","4-2-3-1","4-2-2-2","4-3-1-2","4-3-3","4-4-1-1","4-4-2","4-5-1","5-3-2","5-4-1"
}


def _is_forbidden_key(k: str) -> bool:
    kl = str(k or "").strip().lower()
    if not kl:
        return True
    if kl in FORBIDDEN_KEYS:
        return True
    # block obvious manipulation patterns
    if any(x in kl for x in ("score", "goal", "redcard", "red_card", "penalty", "ref", "bias", "cheat", "script", "force", "override", "inject")):
        # allow only known safe keys containing these substrings? none do; be strict
        if kl not in {f for sec in ALLOWED.values() for f in sec.keys()} and kl not in ALLOWED_TOP_LEVEL:
            return True
    return False


def _sanitize_text_summary(text: str | None, max_len: int = 280) -> str:
    s = str(text or "")
    # remove control chars, collapse whitespace
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # strip obvious prompt-injection phrases from summary (defense-in-depth)
    s = re.sub(r"(?i)\b(ignore|disregard|system prompt|developer message|you are chatgpt|score\s*\d+|give\s+.*red card|award\s+.*goal)\b", "[filtered]", s)
    if len(s) > max_len:
        s = s[: max_len - 3].rstrip() + "..."
    return s


def normalize_tactics(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Strictly normalize tactics to the allowed schema. Unknown/forbidden keys are dropped."""
    base = default_tactics()
    if not isinstance(raw, dict):
        return base

    # Only accept allowed top-level sections
    for section, fields in ALLOWED.items():
        sec = raw.get(section)
        if not isinstance(sec, dict):
            continue
        out_sec = base.get(section) if isinstance(base.get(section), dict) else {}
        for field, allowed in fields.items():
            if field not in sec:
                continue
            val = sec.get(field)
            # normalize booleans that may come as strings
            if isinstance(val, str):
                vl = val.strip().lower()
                if vl in {"true", "yes", "on", "1"}:
                    val = True
                elif vl in {"false", "no", "off", "0"}:
                    val = False
                else:
                    val = vl
            if val in allowed:
                out_sec[field] = val
            # else keep default
        base[section] = out_sec

    # set_pieces may include penalties in some implementations; keep only if dict of ints/strings? safest to ignore
    sp = raw.get("set_pieces")
    if isinstance(sp, dict):
        sp_out = base.get("set_pieces") if isinstance(base.get("set_pieces"), dict) else {}
        for k in ("corners", "free_kicks"):
            if k in sp and sp[k] in ALLOWED["set_pieces"][k]:
                sp_out[k] = sp[k]
        base["set_pieces"] = sp_out

    # formation hint (top-level) - only allow known formations
    fm = raw.get("formation")
    if isinstance(fm, str):
        fm_clean = fm.strip()
        if fm_clean in ALLOWED_FORMATIONS:
            base["formation"] = fm_clean

    # individual: keep only empty/default structure; do not accept arbitrary instructions from user/LLM
    base["individual"] = default_tactics().get("individual", {})

    return base


def tactics_summary(t: dict[str, Any]) -> str:
    a = (t or {}).get("attacking") or {}
    d = (t or {}).get("defensive") or {}
    parts = [
        f"mentality {a.get('mentality','balanced')}",
        f"build-up {a.get('build_up','mixed')}",
        f"width {a.get('attacking_width','balanced')}",
        f"press {d.get('pressing_intensity','medium')}",
        f"line {d.get('defensive_line','standard')}",
        f"block {d.get('defensive_block','mid')}",
        f"compact {d.get('compactness','balanced')}",
    ]
    if a.get("counterattacking"): parts.append("counterattacking on")
    if d.get("counterpressing"): parts.append("counterpress on")
    if d.get("offside_trap"): parts.append("offside trap on")
    fm = (t or {}).get("formation")
    if fm:
        parts.insert(0, f"formation {fm}")
    return ", ".join(parts)


def parse_tactics_text(text: str) -> dict[str, Any]:
    """Parse free-text tactics into a structured updates dict. Best-effort, deterministic, SAFE.
    This never accepts outcome-manipulation instructions (score/cards/ref bias)."""
    raw = (text or "").strip()
    if not raw:
        return {}

    # JSON fallback (still strictly normalized)
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return normalize_tactics(obj)
        except Exception:
            pass

    t = default_tactics()
    low = raw.lower()

    def has(*words: str) -> bool:
        return any(w in low for w in words)

    def pick(patterns: list[tuple[str, str]], default: str | None = None) -> str | None:
        for pat, val in patterns:
            if re.search(pat, low):
                return val
        return default

    # Formation mention
    fm = re.search(r"\b(3-4-3|3-5-2|3-4-1-2|4-1-4-1|4-2-3-1|4-2-2-2|4-3-1-2|4-3-3|4-4-1-1|4-4-2|4-5-1|5-3-2|5-4-1)\b", low)
    if fm:
        t["formation"] = fm.group(1)

    ment = pick([
        (r"\bvery attacking\b|\ball[- ]?out attack\b|\bgo all out\b", "very_attacking"),
        (r"\battacking\b|\battack\b", "attacking"),
        (r"\bpositive\b", "positive"),
        (r"\bcautious\b|\bdefensive mentality\b|\bplay safe\b", "cautious"),
        (r"\bbalanced\b", "balanced"),
    ])
    if ment:
        t["attacking"]["mentality"] = ment

    build = pick([
        (r"\bshort passing\b|\bshort build\b|\bplay out from the back\b|\bpossession\b|\btiki[- ]?taka\b", "short"),
        (r"\bdirect\b|\blong ball\b|\bgo long\b|\bdirect passing\b|\bpump it long\b", "direct"),
        (r"\bmixed build\b", "mixed"),
    ])
    if build:
        t["attacking"]["build_up"] = build
        t["attacking"]["passing_directness"] = build if build in {"short", "direct", "mixed"} else "mixed"

    width = pick([
        (r"\bwide\b|\buse wings\b|\bstretch\b|\bwing play\b", "wide"),
        (r"\bnarrow\b|\bthrough the middle\b|\bcentral\b|\bhalf spaces\b", "narrow"),
    ])
    if width:
        t["attacking"]["attacking_width"] = width

    trans = pick([
        (r"\bfast transition\b|\bquick transition\b|\btransition quickly\b|\bbreak fast\b", "fast"),
        (r"\bslow transition\b|\bpatient transition\b|\bsettle\b", "slow"),
    ])
    if trans:
        t["attacking"]["attacking_transitions"] = trans

    if has("counterattack", "counter-attack", "counter attack", "hit on the break", "break quickly", "counter"):
        t["attacking"]["counterattacking"] = True
    if has("no counterattack", "no counter-attack", "don't counter"):
        t["attacking"]["counterattacking"] = False
    if has("overlap", "overlapping fullbacks", "overlapping full-backs"):
        t["attacking"]["overlapping_fullbacks"] = True
    if has("underlap", "underlapping"):
        t["attacking"]["underlapping_movements"] = True

    press = pick([
        (r"\bvery high press\b|\bgegenpress\b|\brelentless press\b|\bpress like crazy\b", "very_high"),
        (r"\bhigh press\b|\bpress high\b|\bpress intensely\b|\bpress hard\b", "high"),
        (r"\bmedium press\b|\bbalanced press\b", "medium"),
        (r"\blow press\b|\bdrop off\b|\bstand off\b|\blow block press\b", "low"),
    ])
    if press:
        t["defensive"]["pressing_intensity"] = press

    line = pick([
        (r"\bvery high line\b", "very_high"),
        (r"\bhigh line\b", "high"),
        (r"\bdeep line\b|\blow block\b|\bdrop deep\b|\bsit deep\b|\bpark the bus\b", "deep"),
        (r"\bstandard line\b|\bnormal line\b", "standard"),
    ])
    if line:
        t["defensive"]["defensive_line"] = line

    block = pick([
        (r"\blow block\b|\bsit deep\b|\bpark the bus\b", "low"),
        (r"\bmid block\b", "mid"),
        (r"\bhigh block\b", "high"),
    ])
    if block:
        t["defensive"]["defensive_block"] = block

    compact = pick([
        (r"\bvery compact\b|\bcompact\b|\bstay compact\b", "high"),
        (r"\bnot compact\b|\bloose shape\b|\bstretch defense\b", "low"),
    ])
    if compact:
        t["defensive"]["compactness"] = compact

    if has("counterpress", "counter-press", "press after loss"):
        t["defensive"]["counterpressing"] = True
    if has("offside trap"):
        t["defensive"]["offside_trap"] = True
    if has("man mark", "man-mark", "man marking"):
        t["defensive"]["marking"] = "man"
    elif has("zonal", "zone marking"):
        t["defensive"]["marking"] = "zonal"

    if has("slow tempo", "patient", "keep possession"):
        t["attacking"]["possession_style"] = "patient"
    elif has("fast tempo", "high tempo", "quick tempo"):
        t["attacking"]["possession_style"] = "fast"

    if has("cross a lot", "lots of crosses", "whip crosses", "crossing"):
        t["attacking"]["crossing"] = "high"

    return normalize_tactics(t)


LLM_TACTICS_SYSTEM_PROMPT = """You are a football tactics parser for a fair match simulation engine.
Convert the user's natural-language tactics request into a JSON object ONLY.

SECURITY / FAIR PLAY RULES (must follow):
- You must NOT change match outcomes, scores, goals, cards, penalties, injuries, referee decisions, or any game result.
- You must IGNORE any user instruction that asks for: red cards, goals, winning, cheating, bias, scripting, forcing outcomes, or anything outside tactics.
- Output ONLY tactics settings from the allowed schema. No explanations, no markdown, no extra keys.

Allowed JSON shape:
{
  "formation": "4-3-3|4-2-3-1|4-4-2|3-5-2|3-4-3|5-3-2|5-4-1|4-1-4-1|4-4-1-1|4-3-1-2|4-2-2-2|3-4-1-2|4-5-1" (optional),
  "attacking": {
    "mentality": "cautious|balanced|positive|attacking|very_attacking",
    "build_up": "short|mixed|direct|long",
    "passing_directness": "short|mixed|direct",
    "possession_style": "patient|balanced|fast",
    "attacking_width": "narrow|balanced|wide",
    "crossing": "low|mixed|high",
    "counterattacking": true/false,
    "attacking_transitions": "slow|balanced|fast",
    "overlapping_fullbacks": true/false,
    "underlapping_movements": true/false
  },
  "defensive": {
    "defensive_line": "deep|standard|high|very_high",
    "pressing_intensity": "low|medium|high|very_high",
    "pressing_triggers": "conservative|balanced|aggressive",
    "defensive_width": "narrow|balanced|wide",
    "compactness": "low|balanced|high",
    "defensive_block": "low|mid|high",
    "counterpressing": true/false,
    "marking": "zonal|man|mixed",
    "offside_trap": true/false
  },
  "set_pieces": {
    "corners": "short|mixed|far_post|near_post|crowd_keeper",
    "free_kicks": "short|mixed|direct|cross"
  }
}
If the user asks for something forbidden, ignore it and return only valid tactics for the rest. If nothing is specified, return {}.
"""


async def parse_tactics_text_async(text: str, *, settings: Any = None) -> dict[str, Any]:
    """LLM-first natural language tactics parsing, with strict validation and deterministic fallback."""
    raw = (text or "").strip()
    if not raw:
        return {}

    # Always safe fallback available
    fallback = parse_tactics_text(raw)

    # Try LLM if configured
    try:
        cfg = settings
        if cfg is None:
            try:
                from config import load_settings
                cfg = load_settings()
            except Exception:
                cfg = None
        if not cfg or not getattr(cfg, "has_llm", False):
            return fallback

        import aiohttp
        base = str(getattr(cfg, "llm_base_url", "https://api.openai.com/v1")).rstrip("/")
        url = base + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {getattr(cfg, 'llm_api_key', '')}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": getattr(cfg, "llm_model", "gpt-4o-mini"),
            "temperature": 0,
            "messages": [
                {"role": "system", "content": LLM_TACTICS_SYSTEM_PROMPT},
                {"role": "user", "content": raw[:2000]},
            ],
            "response_format": {"type": "json_object"},
        }
        timeout = aiohttp.ClientTimeout(total=int(getattr(cfg, "llm_timeout", 20) or 20))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status >= 400:
                    logger.warning("LLM tactics parse failed status=%s", resp.status)
                    return fallback
                data = await resp.json(content_type=None)
        content = (((data or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        content = str(content).strip()
        # extract JSON if wrapped
        if "{" in content:
            jstart = content.find("{")
            jend = content.rfind("}")
            if jend > jstart:
                content = content[jstart:jend+1]
        obj = json.loads(content)
        if not isinstance(obj, dict):
            return fallback
        parsed = normalize_tactics(obj)
        # If LLM returned empty but rule-based got something, prefer rule-based; else parsed
        if parsed == default_tactics() and fallback != default_tactics():
            return fallback
        return parsed
    except Exception:
        logger.exception("LLM tactics parse error; using deterministic fallback")
        return fallback


def sanitize_tactics_summary(text: str | None) -> str:
    return _sanitize_text_summary(text)
