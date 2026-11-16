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
