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
    """Parse free-text tactics into structured updates. Best-effort, deterministic, SAFE.
    Understands natural football language; never accepts outcome-manipulation instructions."""
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
    # normalize punctuation for matching
    compact = re.sub(r"[^a-z0-9+#\s-]", " ", low)
    compact = re.sub(r"\s+", " ", compact).strip()

    def has(*words: str) -> bool:
        return any(w in compact for w in words)

    def has_re(*patterns: str) -> bool:
        return any(re.search(p, compact) for p in patterns)

    def pick(patterns: list[tuple[str, str]], default: str | None = None) -> str | None:
        for pat, val in patterns:
            if re.search(pat, compact):
                return val
        return default

    # Formation
    fm = re.search(r"\b(3-4-3|3-5-2|3-4-1-2|4-1-4-1|4-2-3-1|4-2-2-2|4-3-1-2|4-3-3|4-4-1-1|4-4-2|4-5-1|5-3-2|5-4-1)\b", compact)
    if fm:
        t["formation"] = fm.group(1)
    else:
        # natural language formation hints
        if has_re(r"\bfour at the back\b", r"\bback four\b", r"\b4 at the back\b"):
            # don't override if a specific 4-x-x was implied; leave default unless other clues
            pass
        if has_re(r"\bthree at the back\b", r"\bback three\b", r"\b3 at the back\b", r"\b3 cb\b"):
            t["formation"] = "3-5-2"
        if has_re(r"\bfive at the back\b", r"\bback five\b", r"\b5 at the back\b"):
            t["formation"] = "5-3-2"

    # Mentality / intent
    ment = pick([
        (r"\bvery attacking\b|\ball out attack\b|\ball-out attack\b|\bgo all out\b|\bfull attack\b|\bthrow everything forward\b", "very_attacking"),
        (r"\battacking\b|\battack\b|\bgo forward\b|\bpush forward\b|\bplay on the front foot\b|\bfront foot\b", "attacking"),
        (r"\bpositive\b|\bproactive\b", "positive"),
        (r"\bcautious\b|\bcareful\b|\bdefensive mentality\b|\bplay safe\b|\bplay it safe\b|\bmore defensive\b|\bsit back\b", "cautious"),
        (r"\bbalanced\b|\bneutral\b", "balanced"),
    ])
    if ment:
        t["attacking"]["mentality"] = ment

    # Build-up / passing style
    build = pick([
        (r"\btiki taka\b|\btiki-taka\b|\bshort passing\b|\bshort passes\b|\bplay out from the back\b|\bbuild from the back\b|\bkeep possession\b|\bpossession football\b|\bpossession based\b|\bpass it around\b", "short"),
        (r"\bdirect\b|\blong ball\b|\blong balls\b|\bgo long\b|\bpump it long\b|\bhit long\b|\bplay direct\b|\bdirect football\b|\broute one\b", "direct"),
        (r"\bmixed build\b|\bmixed passing\b", "mixed"),
    ])
    if build:
        t["attacking"]["build_up"] = build
        t["attacking"]["passing_directness"] = build if build in {"short", "direct", "mixed"} else "mixed"
        if build == "short":
            t["attacking"]["possession_style"] = "patient"
        elif build == "direct":
            t["attacking"]["possession_style"] = "fast"

    # Possession/tempo
    poss = pick([
        (r"\bpatient\b|\bslow tempo\b|\bkeep the ball\b|\bcontrol the game\b|\bcontrol possession\b|\bslow it down\b", "patient"),
        (r"\bfast tempo\b|\bhigh tempo\b|\bquick tempo\b|\bplay fast\b|\bquick passing\b|\bmove the ball quickly\b", "fast"),
        (r"\bbalanced tempo\b", "balanced"),
    ])
    if poss:
        t["attacking"]["possession_style"] = poss

    # Width
    width = pick([
        (r"\bwide\b|\buse the wings\b|\buse wings\b|\bstretch\b|\bwing play\b|\bstay wide\b|\bget wide\b|\bhug the touchline\b", "wide"),
        (r"\bnarrow\b|\bthrough the middle\b|\bcentral\b|\bhalf spaces\b|\bplay narrow\b|\binside\b", "narrow"),
        (r"\bbalanced width\b", "balanced"),
    ])
    if width:
        t["attacking"]["attacking_width"] = width
        t["defensive"]["defensive_width"] = "balanced" if width == "narrow" else width

    # Transitions
    trans = pick([
        (r"\bfast transition\b|\bquick transition\b|\btransition quickly\b|\bbreak fast\b|\bfast breaks\b|\bhit on the break\b", "fast"),
        (r"\bslow transition\b|\bpatient transition\b|\bsettle back\b", "slow"),
    ])
    if trans:
        t["attacking"]["attacking_transitions"] = trans
        if trans == "fast":
            t["attacking"]["counterattacking"] = True

    # Counterattacking
    if has_re(r"\bcounterattack\b", r"\bcounter attack\b", r"\bcounter-attack\b", r"\bhit them on the break\b", r"\bbreak quickly\b", r"\bplay on the counter\b"):
        t["attacking"]["counterattacking"] = True
    if has_re(r"\bno counterattack\b", r"\bno counter attack\b", r"\bdon t counter\b", r"\bdo not counter\b"):
        t["attacking"]["counterattacking"] = False

    # Fullback/width movement
    if has_re(r"\boverlap\b", r"\boverlapping fullbacks\b", r"\boverlapping full backs\b", r"\bfullbacks push up\b"):
        t["attacking"]["overlapping_fullbacks"] = True
    if has_re(r"\bunderlap\b", r"\bunderlapping\b", r"\binverted fullbacks\b"):
        t["attacking"]["underlapping_movements"] = True

    # Crossing
    cross = pick([
        (r"\bcross a lot\b|\blots of crosses\b|\bwhip crosses\b|\bget crosses in\b|\bcross early\b|\bhigh crossing\b", "high"),
        (r"\blow crossing\b|\bfew crosses\b|\bdon t cross\b|\bavoid crosses\b", "low"),
    ])
    if cross:
        t["attacking"]["crossing"] = cross

    # Pressing
    press = pick([
        (r"\bgegenpress\b|\bgegenpressing\b|\bvery high press\b|\brelentless press\b|\bpress like crazy\b|\bpress everywhere\b", "very_high"),
        (r"\bhigh press\b|\bpress high\b|\bpress intensely\b|\bpress hard\b|\bpress them\b|\bclose down\b|\bpress aggressively\b", "high"),
        (r"\bmedium press\b|\bbalanced press\b|\bmid press\b", "medium"),
        (r"\blow press\b|\bno press\b|\bdon t press\b|\bdrop off\b|\bstand off\b|\bsit off\b|\bdo not press\b", "low"),
    ])
    if press:
        t["defensive"]["pressing_intensity"] = press
        if press in ("high", "very_high"):
            t["defensive"]["pressing_triggers"] = "aggressive"
            t["defensive"]["counterpressing"] = True
        elif press == "low":
            t["defensive"]["pressing_triggers"] = "conservative"

    # Defensive line / block
    line = pick([
        (r"\bvery high line\b|\bextremely high line\b", "very_high"),
        (r"\bhigh line\b|\bpush up\b|\bhold a high line\b", "high"),
        (r"\bdeep line\b|\blow block\b|\bdrop deep\b|\bsit deep\b|\bpark the bus\b|\bdefend deep\b|\blow defensive line\b", "deep"),
        (r"\bstandard line\b|\bnormal line\b|\bmid line\b", "standard"),
    ])
    if line:
        t["defensive"]["defensive_line"] = line
        if line == "deep":
            t["defensive"]["defensive_block"] = "low"
        elif line in ("high", "very_high"):
            t["defensive"]["defensive_block"] = "high"

    block = pick([
        (r"\blow block\b|\bpark the bus\b|\bsit deep\b|\bdeep block\b", "low"),
        (r"\bmid block\b|\bmiddle block\b", "mid"),
        (r"\bhigh block\b", "high"),
    ])
    if block:
        t["defensive"]["defensive_block"] = block

    # Compactness
    compactness = pick([
        (r"\bvery compact\b|\bstay compact\b|\bcompact\b|\bkeep it tight\b|\btight shape\b", "high"),
        (r"\bnot compact\b|\bloose shape\b|\bstretch defense\b|\bspread out\b", "low"),
    ])
    if compactness:
        t["defensive"]["compactness"] = compactness

    # Counterpress
    if has_re(r"\bcounterpress\b", r"\bcounter press\b", r"\bpress after loss\b", r"\bwin it back quickly\b"):
        t["defensive"]["counterpressing"] = True
    if has_re(r"\bno counterpress\b", r"\bdon t counterpress\b"):
        t["defensive"]["counterpressing"] = False

    # Marking / traps
    if has_re(r"\boffside trap\b"):
        t["defensive"]["offside_trap"] = True
    if has_re(r"\bman mark\b", r"\bman marking\b", r"\bmark man to man\b"):
        t["defensive"]["marking"] = "man"
    elif has_re(r"\bzonal\b", r"\bzone marking\b"):
        t["defensive"]["marking"] = "zonal"

    # Style presets / natural phrases
    if has_re(r"\bpark the bus\b", r"\bcatenaccio\b", r"\ball behind the ball\b"):
        t["attacking"]["mentality"] = "cautious"
        t["attacking"]["build_up"] = "direct"
        t["attacking"]["passing_directness"] = "direct"
        t["attacking"]["counterattacking"] = True
        t["attacking"]["attacking_transitions"] = "fast"
        t["defensive"]["defensive_line"] = "deep"
        t["defensive"]["defensive_block"] = "low"
        t["defensive"]["pressing_intensity"] = "low"
        t["defensive"]["compactness"] = "high"
    if has_re(r"\bgegenpress\b", r"\bheavy metal football\b"):
        t["attacking"]["mentality"] = "attacking"
        t["attacking"]["attacking_transitions"] = "fast"
        t["defensive"]["pressing_intensity"] = "very_high"
        t["defensive"]["defensive_line"] = "high"
        t["defensive"]["counterpressing"] = True
        t["defensive"]["compactness"] = "high"
    if has_re(r"\btiki taka\b", r"\btotal football\b", r"\bpossession football\b"):
        t["attacking"]["mentality"] = "positive"
        t["attacking"]["build_up"] = "short"
        t["attacking"]["passing_directness"] = "short"
        t["attacking"]["possession_style"] = "patient"
        t["attacking"]["attacking_transitions"] = "slow"
        t["defensive"]["pressing_intensity"] = "high"
        t["defensive"]["defensive_line"] = "high"

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
    """Natural language tactics parsing using the deterministic rule engine (no external LLM API)."""
    return parse_tactics_text(text)

def sanitize_tactics_summary(text: str | None) -> str:
    return _sanitize_text_summary(text)
