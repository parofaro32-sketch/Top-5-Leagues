from __future__ import annotations

from typing import Any

from engine.models import default_tactics


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


def _merge(base: dict, updates: dict) -> dict:
    out = {k: (v.copy() if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in (updates or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def normalize_tactics(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = default_tactics()
    if not raw:
        return base
    merged = _merge(base, raw)
    # validate known enums/bools softly
    for section, fields in ALLOWED.items():
        sec = merged.get(section) or {}
        for field, allowed in fields.items():
            if field in sec and sec[field] not in allowed:
                sec[field] = base[section][field]
        merged[section] = sec
    # keep individual and set_pieces.penalties if provided
    if not isinstance(merged.get("individual"), dict):
        merged["individual"] = base["individual"]
    if not isinstance(merged.get("set_pieces"), dict):
        merged["set_pieces"] = base["set_pieces"]
    return merged


def tactics_summary(t: dict[str, Any]) -> str:
    a = t.get("attacking") or {}
    d = t.get("defensive") or {}
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
    return ", ".join(parts)


def parse_tactics_text(text: str) -> dict[str, Any]:
    """Parse free-text tactics into a structured updates dict. Best-effort, private."""
    import json as _json
    import re as _re

    raw = (text or "").strip()
    if not raw:
        return {}

    # JSON fallback
    if raw.startswith("{"):
        try:
            obj = _json.loads(raw)
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
            if _re.search(pat, low):
                return val
        return default

    # Formation mention (stored as top-level hint; engine handles it in tactics update)
    fm = _re.search(r"\b(3-4-3|3-5-2|3-4-1-2|4-1-4-1|4-2-3-1|4-2-2-2|4-3-1-2|4-3-3|4-4-1-1|4-4-2|4-5-1|5-3-2|5-4-1)\b", low)
    if fm:
        t["formation"] = fm.group(1)

    ment = pick([
        (r"\bvery attacking\b|\ball[- ]?out attack\b", "very_attacking"),
        (r"\battacking\b", "attacking"),
        (r"\bpositive\b", "positive"),
        (r"\bcautious\b|\bdefensive mentality\b", "cautious"),
        (r"\bbalanced\b", "balanced"),
    ])
    if ment:
        t["attacking"]["mentality"] = ment

    build = pick([
        (r"\bshort passing\b|\bshort build\b|\bplay out from the back\b|\bpossession\b", "short"),
        (r"\bdirect\b|\blong ball\b|\bgo long\b|\bdirect passing\b", "direct"),
        (r"\bmixed build\b", "mixed"),
    ])
    if build:
        t["attacking"]["build_up"] = build
        t["attacking"]["passing_directness"] = build if build in {"short", "direct", "mixed"} else "mixed"

    width = pick([
        (r"\bwide\b|\buse wings\b|\bstretch\b", "wide"),
        (r"\bnarrow\b|\bthrough the middle\b|\bcentral\b", "narrow"),
    ])
    if width:
        t["attacking"]["attacking_width"] = width

    trans = pick([
        (r"\bfast transition\b|\bquick transition\b|\btransition quickly\b", "fast"),
        (r"\bslow transition\b|\bpatient transition\b", "slow"),
    ])
    if trans:
        t["attacking"]["attacking_transitions"] = trans

    if has("counterattack", "counter-attack", "counter attack", "hit on the break", "break quickly"):
        t["attacking"]["counterattacking"] = True
    if has("no counterattack", "no counter-attack"):
        t["attacking"]["counterattacking"] = False
    if has("overlap", "overlapping fullbacks", "overlapping full-backs"):
        t["attacking"]["overlapping_fullbacks"] = True
    if has("underlap", "underlapping"):
        t["attacking"]["underlapping_movements"] = True

    press = pick([
        (r"\bvery high press\b|\bgegenpress\b|\brelentless press\b", "very_high"),
        (r"\bhigh press\b|\bpress high\b|\bpress intensely\b", "high"),
        (r"\bmedium press\b|\bbalanced press\b", "medium"),
        (r"\blow press\b|\bdrop off\b|\bstand off\b", "low"),
    ])
    if press:
        t["defensive"]["pressing_intensity"] = press

    line = pick([
        (r"\bvery high line\b", "very_high"),
        (r"\bhigh line\b", "high"),
        (r"\bdeep line\b|\blow block\b|\bdrop deep\b|\bsit deep\b", "deep"),
        (r"\bstandard line\b|\bnormal line\b", "standard"),
    ])
    if line:
        t["defensive"]["defensive_line"] = line

    block = pick([
        (r"\blow block\b|\bsit deep\b", "low"),
        (r"\bmid block\b", "mid"),
        (r"\bhigh block\b", "high"),
    ])
    if block:
        t["defensive"]["defensive_block"] = block

    compact = pick([
        (r"\bvery compact\b|\bcompact\b|\bstay compact\b", "high"),
        (r"\bnot compact\b|\bloose shape\b", "low"),
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

    # tempo/possession hints
    if has("slow tempo", "patient", "keep possession"):
        t["attacking"]["possession_style"] = "patient"
    elif has("fast tempo", "high tempo", "quick tempo"):
        t["attacking"]["possession_style"] = "fast"

    if has("cross a lot", "lots of crosses", "whip crosses", "crossing"):
        t["attacking"]["crossing"] = "high"

    return normalize_tactics(t)
