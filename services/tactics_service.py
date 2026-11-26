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
    compact = re.sub(r"[^a-z0-9+#\s-]", " ", low)
    compact = re.sub(r"\s+", " ", compact).strip()

    NEG = r"(?:don\'?t|do not|dont|never|no|avoid|stop|without|not|isn\'?t|aren\'?t|can\'?t|won\'?t)"
    def _window_has_neg(m_start: int, window: int = 40) -> bool:
        pre = compact[max(0, m_start - window):m_start]
        return bool(re.search(r"\b" + NEG + r"\b", pre))

    def has_re(*patterns: str) -> bool:
        for p in patterns:
            for m in re.finditer(p, compact):
                if not _window_has_neg(m.start()):
                    return True
        return False

    def has_neg_re(*patterns: str) -> bool:
        for p in patterns:
            for m in re.finditer(p, compact):
                if _window_has_neg(m.start()):
                    return True
        return False

    def pick(patterns: list[tuple[str, str]], default: str | None = None) -> str | None:
        for pat, val in patterns:
            for m in re.finditer(pat, compact):
                if not _window_has_neg(m.start()):
                    return val
        return default

    # Formation
    fm = re.search(r"\b(3-4-3|3-5-2|3-4-1-2|4-1-4-1|4-2-3-1|4-2-2-2|4-3-1-2|4-3-3|4-4-1-1|4-4-2|4-5-1|5-3-2|5-4-1)\b", compact)
    if fm:
        t["formation"] = fm.group(1)
    else:
        if has_re(r"\bthree at the back\b", r"\bback three\b", r"\b3 at the back\b", r"\b3 cb\b", r"\bthree centre backs\b"):
            t["formation"] = "3-5-2"
        if has_re(r"\bfive at the back\b", r"\bback five\b", r"\b5 at the back\b"):
            t["formation"] = "5-3-2"
        if has_re(r"\b4 3 3\b", r"\b433\b"):
            t["formation"] = "4-3-3"
        if has_re(r"\b4 2 3 1\b", r"\b4231\b"):
            t["formation"] = "4-2-3-1"
        if has_re(r"\b4 4 2\b", r"\b442\b"):
            t["formation"] = "4-4-2"

    # Mentality / intent
    ment = pick([
        (r"\bvery attacking\b|\ball out attack\b|\ball-out attack\b|\bgo all out\b|\bfull attack\b|\bthrow everything forward\b|\bgo for it\b|\bwe need a goal\b|\bchase the game\b|\bpush for a goal\b", "very_attacking"),
        (r"\battacking\b|\battack\b|\bgo forward\b|\bpush forward\b|\bplay on the front foot\b|\bfront foot\b|\bmore attacking\b|\bbe aggressive\b|\btake risks\b", "attacking"),
        (r"\bpositive\b|\bproactive\b", "positive"),
        (r"\bultra defensive\b|\bpark the bus\b|\ball behind the ball\b", "cautious"),
        (r"\bcautious\b|\bcareful\b|\bdefensive mentality\b|\bplay safe\b|\bplay it safe\b|\bmore defensive\b|\bsit back\b|\bprotect the lead\b|\bprotecting the lead\b|\bhold on\b|\bsee it out\b|\bkeep it tight\b|\bstay organized\b|\bstay organised\b|\bno risks\b|\bdon\'?t take risks\b|\bstop taking risks\b", "cautious"),
        (r"\bbalanced\b|\bneutral\b", "balanced"),
    ])
    if ment:
        t["attacking"]["mentality"] = ment

    # Build-up / passing style / possession control
    if has_re(r"\bkeep possession\b", r"\bcontrol possession\b", r"\bpossession football\b", r"\bpossession based\b", r"\bpass it around\b", r"\bcontrol the game\b", r"\bdominate possession\b", r"\btiki taka\b", r"\btiki-taka\b", r"\bplay out from the back\b", r"\bbuild from the back\b", r"\bshort passing\b", r"\bshort passes\b"):
        t["attacking"]["build_up"] = "short"
        t["attacking"]["passing_directness"] = "short"
        t["attacking"]["possession_style"] = "patient"
    if has_re(r"\bdirect\b", r"\blong ball\b", r"\blong balls\b", r"\bgo long\b", r"\bpump it long\b", r"\bhit long\b", r"\bplay direct\b", r"\bdirect football\b", r"\broute one\b", r"\bget the ball forward quickly\b", r"\bforward quickly\b", r"\bquickly forward\b"):
        # don't override a clear possession instruction unless direct is explicit
        if not has_re(r"\bkeep possession\b", r"\bcontrol possession\b", r"\btiki taka\b"):
            t["attacking"]["build_up"] = "direct"
            t["attacking"]["passing_directness"] = "direct"
            t["attacking"]["possession_style"] = "fast"

    # Possession/tempo
    if has_re(r"\bpatient\b", r"\bslow tempo\b", r"\bslow it down\b", r"\bslow things down\b", r"\bkeep the ball\b", r"\bcalm it down\b", r"\bcontrol the tempo\b"):
        t["attacking"]["possession_style"] = "patient"
        if t["attacking"]["build_up"] == "mixed":
            t["attacking"]["build_up"] = "short"
            t["attacking"]["passing_directness"] = "short"
    if has_re(r"\bfast tempo\b", r"\bhigh tempo\b", r"\bquick tempo\b", r"\bplay fast\b", r"\bquick passing\b", r"\bmove the ball quickly\b", r"\bup the tempo\b", r"\bspeed it up\b"):
        t["attacking"]["possession_style"] = "fast"

    # Width
    width = pick([
        (r"\bwide\b|\buse the wings\b|\buse wings\b|\bstretch\b|\bwing play\b|\bstay wide\b|\bget wide\b|\bhug the touchline\b|\bkeep the width\b|\bstretch their defense\b|\bstretch the defense\b", "wide"),
        (r"\bnarrow\b|\bthrough the middle\b|\bplay through the middle\b|\bcentral\b|\bhalf spaces\b|\bplay narrow\b|\binside\b|\bblock the middle\b", "narrow"),
        (r"\bbalanced width\b", "balanced"),
    ])
    if width:
        t["attacking"]["attacking_width"] = width
        if width == "narrow":
            t["defensive"]["defensive_width"] = "narrow"
        elif width == "wide":
            t["defensive"]["defensive_width"] = "wide"
    if re.search(r"\b(don\'?t let them|stop them|prevent them from)\s+play(?:ing)?\s+through the middle\b", compact) or re.search(r"\bblock the middle\b", compact):
        t["attacking"]["attacking_width"] = "narrow"
        t["defensive"]["defensive_width"] = "narrow"
        t["defensive"]["compactness"] = "high"

    # Compactness / shape
    if has_re(r"\bcompact\b", r"\bmore compact\b", r"\bkeep it tight\b", r"\btight shape\b", r"\bclose the gaps\b", r"\bstay organized\b", r"\bstay organised\b", r"\bharder to break down\b", r"\bdifficult to break down\b", r"\bmake us solid\b", r"\bsolid shape\b"):
        t["defensive"]["compactness"] = "high"
    if has_neg_re(r"\bcompact\b") or has_re(r"\bloose shape\b", r"\bspread out\b"):
        t["defensive"]["compactness"] = "low"

    # Transitions / counters
    if has_re(r"\bcounterattack\b", r"\bcounter attack\b", r"\bcounter-attack\b", r"\bhit them on the break\b", r"\bbreak quickly\b", r"\bplay on the counter\b", r"\bfast breaks\b", r"\bcounter attacking\b"):
        t["attacking"]["counterattacking"] = True
        t["attacking"]["attacking_transitions"] = "fast"
    if has_re(r"\bfast transition\b", r"\bquick transition\b", r"\btransition quickly\b", r"\bbreak fast\b", r"\bget forward quickly\b", r"\battack quickly\b"):
        t["attacking"]["attacking_transitions"] = "fast"
    if has_re(r"\bslow transition\b", r"\bpatient transition\b", r"\bsettle back\b", r"\bno risks\b"):
        t["attacking"]["attacking_transitions"] = "slow"
    if has_neg_re(r"\bcounterattack\b", r"\bcounter attack\b", r"\bcounter-attack\b") or has_re(r"\bno counterattack\b", r"\bdon\'?t counter\b", r"\bdo not counter\b"):
        t["attacking"]["counterattacking"] = False

    # Stop counterattacks (opponent) - be conservative: keep shape, slower transitions, possible counterpress
    if has_re(r"\bstop their counterattacks\b", r"\bprevent counterattacks\b", r"\bstop counters\b", r"\bno counterattacks\b", r"\bwatch the counter\b"):
        t["attacking"]["attacking_transitions"] = "slow"
        t["defensive"]["counterpressing"] = True
        t["defensive"]["compactness"] = "high"

    # Fullbacks / movement
    if has_re(r"\boverlap\b", r"\boverlapping fullbacks\b", r"\boverlapping full backs\b", r"\bfullbacks push up\b", r"\bfull backs push up\b"):
        t["attacking"]["overlapping_fullbacks"] = True
    if has_neg_re(r"\boverlap\b"):
        t["attacking"]["overlapping_fullbacks"] = False
    if has_re(r"\bunderlap\b", r"\bunderlapping\b", r"\binverted fullbacks\b"):
        t["attacking"]["underlapping_movements"] = True
    if has_neg_re(r"\bunderlap\b"):
        t["attacking"]["underlapping_movements"] = False

    # Crossing
    cross = pick([
        (r"\bcross a lot\b|\blots of crosses\b|\bwhip crosses\b|\bget crosses in\b|\bcross early\b|\bhigh crossing\b|\bflood the box\b", "high"),
        (r"\blow crossing\b|\bfew crosses\b|\bdon\'?t cross\b|\bavoid crosses\b|\bwork it into the box\b", "low"),
        (r"\bmixed crossing\b", "mixed"),
    ])
    if cross:
        t["attacking"]["crossing"] = cross

    # Pressing / out of possession
    if has_re(r"\bgegenpress\b", r"\bgegenpressing\b", r"\bpress after losing possession\b", r"\bpress after loss\b", r"\bwin it back quickly\b", r"\bcounterpress\b", r"\bcounter press\b"):
        t["defensive"]["pressing_intensity"] = "high" if t["defensive"]["pressing_intensity"] == "medium" else t["defensive"]["pressing_intensity"]
        t["defensive"]["counterpressing"] = True
        t["defensive"]["pressing_triggers"] = "aggressive"
    if has_re(r"\bhigh press\b", r"\bpress high\b", r"\bpress intensely\b", r"\bpress hard\b", r"\bpress them constantly\b", r"\bpress them\b", r"\bclose down\b", r"\bpress aggressively\b", r"\bconstant press\b"):
        t["defensive"]["pressing_intensity"] = "high"
        t["defensive"]["pressing_triggers"] = "aggressive"
        t["defensive"]["counterpressing"] = True
    if has_re(r"\bvery high press\b", r"\brelentless press\b", r"\bpress everywhere\b", r"\bpress like crazy\b"):
        t["defensive"]["pressing_intensity"] = "very_high"
        t["defensive"]["pressing_triggers"] = "aggressive"
        t["defensive"]["counterpressing"] = True
    if has_re(r"\bmedium press\b", r"\bbalanced press\b", r"\bmid press\b"):
        t["defensive"]["pressing_intensity"] = "medium"
        t["defensive"]["pressing_triggers"] = "balanced"
    if has_re(r"\blow press\b", r"\bdrop off\b", r"\bstand off\b", r"\bsit off\b", r"\bdon\'?t press\b", r"\bdo not press\b", r"\bno press\b"):
        t["defensive"]["pressing_intensity"] = "low"
        t["defensive"]["pressing_triggers"] = "conservative"
        t["defensive"]["counterpressing"] = False

    # Defensive line / block
    if has_re(r"\bvery high line\b", r"\bextremely high line\b"):
        t["defensive"]["defensive_line"] = "very_high"
        t["defensive"]["defensive_block"] = "high"
    if has_re(r"\bhigh line\b", r"\bkeep the defensive line high\b", r"\bhold a high line\b", r"\bpush up\b"):
        # "push up" can mean attack, but in defensive context it's line; treat as high line
        t["defensive"]["defensive_line"] = "high"
        t["defensive"]["defensive_block"] = "high"
    if has_re(r"\bdeep line\b", r"\bdrop the line\b", r"\bdrop deep\b", r"\bsit deep\b", r"\bdefend deep\b", r"\blow defensive line\b", r"\bdeeper line\b", r"\bthey have pace\b"):
        t["defensive"]["defensive_line"] = "deep"
        t["defensive"]["defensive_block"] = "low"
    if has_re(r"\blow block\b", r"\bpark the bus\b", r"\bdeep block\b"):
        t["defensive"]["defensive_block"] = "low"
        t["defensive"]["defensive_line"] = "deep"
        t["attacking"]["mentality"] = "cautious"
        t["attacking"]["counterattacking"] = True
    if has_re(r"\bmid block\b", r"\bmiddle block\b"):
        t["defensive"]["defensive_block"] = "mid"
    if has_re(r"\bhigh block\b"):
        t["defensive"]["defensive_block"] = "high"

    # Marking / traps
    if has_re(r"\boffside trap\b"):
        t["defensive"]["offside_trap"] = True
    if has_neg_re(r"\boffside trap\b"):
        t["defensive"]["offside_trap"] = False
    if has_re(r"\bman mark\b", r"\bman marking\b", r"\bmark man to man\b"):
        t["defensive"]["marking"] = "man"
    elif has_re(r"\bzonal\b", r"\bzone marking\b"):
        t["defensive"]["marking"] = "zonal"

    # Exploit space behind / pace warnings
    if has_re(r"\bspace behind\b", r"\bin behind\b", r"\bbehind their fullbacks\b", r"\bbehind their defence\b", r"\bbehind their defense\b"):
        t["attacking"]["attacking_transitions"] = "fast"
        t["attacking"]["counterattacking"] = True
        t["attacking"]["passing_directness"] = "direct" if t["attacking"]["passing_directness"] != "short" else t["attacking"]["passing_directness"]

    # Protect lead / game state phrases
    if has_re(r"\bprotect the lead\b", r"\bprotecting the lead\b", r"\bsee out the game\b", r"\bgame management\b", r"\bhold onto the lead\b"):
        t["attacking"]["mentality"] = "cautious"
        t["attacking"]["possession_style"] = "patient"
        t["attacking"]["attacking_transitions"] = "slow"
        t["defensive"]["compactness"] = "high"
    if has_re(r"\bneed a goal\b", r"\bchasing the game\b", r"\bgo for broke\b", r"\ball out\b"):
        t["attacking"]["mentality"] = "very_attacking"
        t["attacking"]["attacking_transitions"] = "fast"
        t["defensive"]["defensive_line"] = "high"

    # Safety: do not allow manipulative instructions to affect tactics
    # (normalize_tactics and allowed schema already enforce this)

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
