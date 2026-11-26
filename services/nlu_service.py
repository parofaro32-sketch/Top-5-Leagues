from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable


# -------------------------
# Text normalization
# -------------------------

def norm_text(text: str | None) -> str:
    s = str(text or "").strip().lower()
    try:
        s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    except Exception:
        pass
    s = s.replace("’", "'").replace("`", "'")
    s = re.sub(r"[\s_]+", " ", s)
    return s.strip()


def norm_token(text: str | None) -> str:
    s = norm_text(text)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokens(text: str | None) -> list[str]:
    return [t for t in norm_token(text).split() if t]


def fuzzy_ratio(a: str | None, b: str | None) -> float:
    aa, bb = norm_token(a), norm_token(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    r = difflib.SequenceMatcher(None, aa, bb).ratio()
    at, bt = set(aa.split()), set(bb.split())
    if at and bt:
        r = max(r, len(at & bt) / max(1, len(at | bt)))
    return r


# -------------------------
# Money / numbers / duration
# -------------------------

_MONEY_TOKEN = r"[€£$]?\s*\d[\d,]*(?:\.\d+)?\s*(?:[kmb]|mil(?:lion)?s?|bn|billion|thousand|grand)?"
_MONEY_RE = re.compile(_MONEY_TOKEN, re.I)


def _normalize_number_str(num_raw: str) -> str:
    s = num_raw.strip().replace("€", "").replace("$", "").replace("£", "").replace(" ", "")
    if "," in s and "." in s:
        # 1.234,56 or 1,234.56
        if re.fullmatch(r"\d{1,3}(\.\d{3})*,\d{1,2}", s):
            return s.replace(".", "").replace(",", ".")
        if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):
            return s.replace(",", "")
    if "," in s:
        if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):
            return s.replace(",", "")
        if re.fullmatch(r"\d+,\d{1,2}", s):
            return s.replace(",", ".")
        return s.replace(",", "")
    return s


def parse_money(text: str | int | float | None) -> int | None:
    """Parse football money amounts: 1m, 1.5m, 500k, 2 million, 750 thousand, plain numbers, currency/commas."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        try:
            return int(round(float(text)))
        except Exception:
            return None
    raw = str(text).strip().lower()
    if not raw:
        return None
    raw = raw.replace("€", " ").replace("$", " ").replace("£", " ").replace("\u00a3", " ")
    # remove wage/time suffixes that shouldn't be interpreted as multipliers
    raw = re.sub(r"\bper\s*week\b", " ", raw)
    raw = re.sub(r"\bper\s*wk\b", " ", raw)
    raw = re.sub(r"\bp\s*/\s*w\b", " ", raw)
    raw = raw.replace("/wk", " ").replace("p/w", " ").replace("p.w", " ").replace("weekly", " ").replace("pw", " ")
    # normalize multipliers
    raw = re.sub(r"(?<=\d)\s*(millions?|mil)\b", " m ", raw)
    raw = re.sub(r"\b(millions?|mil)\b", " m ", raw)
    raw = re.sub(r"(?<=\d)\s*(billions?|bn)\b", " b ", raw)
    raw = re.sub(r"\b(billions?|bn)\b", " b ", raw)
    raw = re.sub(r"(?<=\d)\s*(thousands?|thou|grand|k)\b", " k ", raw)
    raw = re.sub(r"\b(thousands?|thou|grand)\b", " k ", raw)
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([kmb])?\b", raw, re.I)
    if not m:
        # last resort: strip commas/spaces and try plain float
        cleaned = re.sub(r"[^0-9.]", "", raw.replace(",", ""))
        if not cleaned:
            return None
        try:
            return int(round(float(cleaned)))
        except Exception:
            return None
    num_raw = _normalize_number_str(m.group(1))
    try:
        val = float(num_raw)
    except Exception:
        cleaned = re.sub(r"[^0-9.]", "", num_raw)
        if not cleaned:
            return None
        try:
            val = float(cleaned)
        except Exception:
            return None
    suf = (m.group(2) or "").lower()
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suf, 1)
    return int(round(val * mult))


def extract_money_spans(text: str | None) -> list[dict[str, Any]]:
    s = str(text or "")
    out = []
    for m in re.finditer(r"([€£$]?\s*\d[\d,]*(?:\.\d+)?\s*(?:[kmb]|mil(?:lion)?s?|bn|billion|thousand|grand)?)", s, re.I):
        raw = m.group(1).strip()
        val = parse_money(raw)
        if val is None:
            continue
        start, end = m.span(1)
        ctx = s[max(0, start - 35):min(len(s), end + 35)].lower()
        out.append({"raw": raw, "value": int(val), "start": start, "end": end, "context": ctx})
    return out


def parse_duration_years(text: str | None) -> int | None:
    s = norm_text(text)
    m = re.search(r"\b(\d{1,2})\s*(?:year|years|yr|yrs|season|seasons)\b", s)
    if m:
        try:
            return max(1, min(7, int(m.group(1))))
        except Exception:
            return None
    m = re.search(r"\b(?:contract|deal|length|duration|for)\s*[:=]?\s*(\d{1,2})\b", s)
    if m:
        try:
            return max(1, min(7, int(m.group(1))))
        except Exception:
            return None
    return None


# -------------------------
# Contract extraction
# -------------------------

ROLE_ALIASES = {
    "crucial": "Crucial", "key player": "Crucial", "key": "Crucial", "star": "Crucial", "essential": "Crucial", "main man": "Crucial",
    "important": "Important", "regular": "Important", "starter": "Important", "first team": "Important", "first-team": "Important",
    "rotation": "Rotation", "squad player": "Rotation", "rotational": "Rotation", "squad": "Rotation",
    "sporadic": "Sporadic", "backup": "Sporadic", "fringe": "Sporadic", "bench": "Sporadic", "reserve": "Sporadic", "bit part": "Sporadic",
    "prospect": "Prospect", "youngster": "Prospect", "future": "Prospect", "youth": "Prospect", "development": "Prospect",
}

WAGE_CUES = ["wage", "wages", "salary", "per week", "/wk", "p/w", "pw", "weekly", "a week", "week", "per wk"]
BONUS_CUES = ["signing bonus", "sign-on bonus", "sign on bonus", "signing fee", "sign-on fee", "upfront", "bonus", "sign on"]
ROLE_CUES = ["role", "crucial", "important", "rotation", "sporadic", "prospect", "starter", "key player", "squad player", "backup", "fringe", "regular"]


def extract_role(text: str | None) -> str | None:
    s = norm_text(text)
    # check multi-word first
    for k in sorted(ROLE_ALIASES.keys(), key=len, reverse=True):
        if re.search(rf"\b{re.escape(k)}\b", s):
            return ROLE_ALIASES[k]
    return None


def extract_contract_terms(text: str | None) -> dict[str, Any]:
    """Extract wage, signing bonus, role, contract years from natural language.
    Returns dict with keys present only when confidently found (bonus may be 0 when explicitly none).
    Also returns _confidence hints and _ambiguous flags.
    """
    raw = str(text or "").strip()
    s = norm_text(raw)
    out: dict[str, Any] = {}
    if not s:
        return out

    # Years
    years = parse_duration_years(raw)
    if years is not None:
        out["contract_years"] = years

    # Role
    role = extract_role(raw)
    if role:
        out["role"] = role

    money_spans = extract_money_spans(raw)

    def has_any(hay: str, cues: list[str]) -> bool:
        return any(c in hay for c in cues)

    def ctx_has(ctx: str, cues: list[str]) -> bool:
        return any(c in ctx for c in cues)

    # Mark explicit no bonus
    if re.search(r"\b(no|without|zero|none)\b\s*(signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|bonus|sign[- ]?on|upfront)\b", s) or \
       re.search(r"\b(signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|bonus|sign[- ]?on|upfront)\b\s*[:=]?\s*(no|none|zero|0)\b", s):
        out["signing_bonus"] = 0

    # Assign money spans by cues
    used = set()
    wage_val = None
    bonus_val = out.get("signing_bonus")

    # Pass 1: explicit cues
    for i, sp in enumerate(money_spans):
        ctx = sp["context"]
        val = int(sp["value"])
        is_wage = ctx_has(ctx, WAGE_CUES) or bool(re.search(r"/\s*w(k)?\b|p\s*/\s*w\b|per\s*w(eek|k)\b|\bw(k|eekly)\b", ctx))
        is_bonus = ctx_has(ctx, BONUS_CUES)
        # avoid misclassifying "4 years" as money
        around = raw[max(0, sp["start"]-1):min(len(raw), sp["end"]+8)].lower()
        if re.search(r"\b(year|yr|season)\b", around) and val <= 10:
            used.add(i)
            continue
        if is_bonus and not is_wage:
            if bonus_val is None or bonus_val == 0:
                bonus_val = val
            used.add(i)
        elif is_wage and not is_bonus:
            if wage_val is None:
                wage_val = val
            used.add(i)

    # Pass 2: if two unassigned amounts remain, infer by magnitude / typical ranges
    remaining = [(i, money_spans[i]) for i in range(len(money_spans)) if i not in used]
    if wage_val is None or (bonus_val is None and "signing_bonus" not in out):
        if len(remaining) == 1:
            val = int(remaining[0][1]["value"])
            # If it looks like a weekly wage, treat as wage; if it's a big lump sum and wage exists, bonus
            if wage_val is None and 1_000 <= val <= 5_000_000 and not ctx_has(remaining[0][1]["context"], BONUS_CUES):
                wage_val = val
                used.add(remaining[0][0])
            elif wage_val is not None and bonus_val is None and val >= 1_000:
                bonus_val = val
                used.add(remaining[0][0])
            elif wage_val is None:
                wage_val = val
                used.add(remaining[0][0])
        elif len(remaining) >= 2:
            vals = [(i, int(sp["value"]), sp) for i, sp in remaining]
            # Heuristic: weekly wage typically 5k-2M, signing bonus typically 50k-100M; when ambiguous, use cues/magnitude
            wage_cand = None
            bonus_cand = None
            for i, val, sp in vals:
                ctx = sp["context"]
                if ctx_has(ctx, BONUS_CUES) and bonus_cand is None:
                    bonus_cand = (i, val)
                elif ctx_has(ctx, WAGE_CUES) and wage_cand is None:
                    wage_cand = (i, val)
            if wage_cand is None or bonus_cand is None:
                # magnitude-based: if one is clearly wage-like and other larger lump-sum-like
                by_val = sorted(vals, key=lambda x: x[1])
                low_i, low_v, low_sp = by_val[0]
                high_i, high_v, high_sp = by_val[-1]
                # If the lower value is in wage range and high is much larger, low=wage, high=bonus
                if wage_cand is None and bonus_cand is None:
                    if 1_000 <= low_v <= 2_000_000 and high_v >= low_v * 2 and high_v >= 50_000:
                        wage_cand, bonus_cand = (low_i, low_v), (high_i, high_v)
                    elif 1_000 <= high_v <= 5_000_000 and 1_000 <= low_v <= 200_000_000:
                        # both could be wage-like; prefer the one closer to typical wage as wage if explicitly marked, else leave ambiguous
                        # but if only two numbers, assume smaller is wage, larger is bonus (common case)
                        wage_cand, bonus_cand = (low_i, low_v), (high_i, high_v)
            if wage_cand and wage_val is None:
                wage_val = wage_cand[1]
                used.add(wage_cand[0])
            if bonus_cand and bonus_val is None:
                bonus_val = bonus_cand[1]
                used.add(bonus_cand[0])

    # Pass 3: leftover amounts
    for i, sp in enumerate(money_spans):
        if i in used:
            continue
        val = int(sp["value"])
        if val <= 10:
            continue
        if wage_val is None and 1_000 <= val <= 5_000_000:
            wage_val = val
            used.add(i)
        elif bonus_val is None and val >= 1_000:
            bonus_val = val
            used.add(i)

    if wage_val is not None and wage_val > 0:
        out["wage"] = int(wage_val)
    if bonus_val is not None:
        out["signing_bonus"] = int(max(0, bonus_val))
    elif "signing_bonus" not in out:
        # if bonus not mentioned at all, don't force - caller can decide; but keep absent
        pass

    # If wage and bonus ended up equal and no clear cue, treat as wage (ambiguous)
    if out.get("wage") and out.get("signing_bonus") and out["wage"] == out["signing_bonus"]:
        # keep wage, drop bonus unless bonus cue present
        if not has_any(s, BONUS_CUES):
            out.pop("signing_bonus", None)

    return out


# -------------------------
# Negation / intent helpers
# -------------------------

_NEGATION_TOKENS = {
    "no", "not", "never", "dont", "don't", "do not", "didnt", "didn't", "did not",
    "cant", "can't", "cannot", "won't", "wont", "without", "isn't", "isnt", "arent", "aren't",
    "none", "neither", "nor"
}


def has_negation_near(text: str | None, keyword_pattern: str, window: int = 48) -> bool:
    s = norm_text(text)
    for m in re.finditer(keyword_pattern, s, re.I):
        start = max(0, m.start() - window)
        pre = s[start:m.start()]
        if any(re.search(rf"\b{re.escape(n)}\b", pre) for n in _NEGATION_TOKENS):
            return True
    return False


def any_phrase(text: str | None, phrases: Iterable[str]) -> bool:
    s = norm_text(text)
    return any(p in s for p in phrases)


def any_regex(text: str | None, patterns: Iterable[str]) -> bool:
    s = norm_text(text)
    return any(re.search(p, s, re.I) for p in patterns)


# -------------------------
# Board / budget request analysis
# -------------------------

ACHIEVEMENT_PATTERNS = {
    "league_title": [r"\bwon the league\b", r"\bwin the league\b", r"\bleague title\b", r"\bleague champions\b", r"\bchampions of the league\b", r"\bwon .* league\b", r"\bdomestic league\b", r"\bliga\b", r"\bpremier league\b", r"\bserie a\b", r"\bbundesliga\b", r"\bligue 1\b", r"\bla liga\b"],
    "domestic_cup": [r"\bwon the cup\b", r"\bwin the cup\b", r"\bdomestic cup\b", r"\bcup title\b", r"\bfa cup\b", r"\bcopa del rey\b", r"\bdfb pokal\b", r"\bcoppa italia\b", r"\bcoupe de france\b", r"\bwon .* cup\b", r"\bcup winners\b"],
    "continental": [r"\bchampions league\b", r"\bucl\b", r"\beuropa league\b", r"\bconference league\b", r"\bcontinental\b", r"\beuropean cup\b", r"\buefa\b"],
    "double": [r"\bdouble\b", r"\bwon both\b", r"\bboth trophies\b", r"\btwo trophies\b", r"\bleague and cup\b", r"\bcup and league\b", r"\bleague and the cup\b", r"\bcup and the league\b"],
    "treble": [r"\btreble\b", r"\bwon everything\b", r"\bwon all three\b", r"\ball three trophies\b", r"\bleague,? cup,? (and )?champions\b"],
    "qualification": [r"\bqualified for\b", r"\bqualification\b", r"\bchampions league football\b", r"\beuropean football\b", r"\bqualified\b"],
    "promotion": [r"\bpromoted\b", r"\bpromotion\b", r"\bwent up\b"],
    "survival": [r"\bsurvived\b", r"\bavoided relegation\b", r"\bstayed up\b"],
    "strong_season": [r"\bgreat season\b", r"\bamazing season\b", r"\bincredible season\b", r"\bexcellent season\b", r"\bstrong season\b", r"\bfantastic season\b", r"\bbrilliant season\b", r"\boverachieved\b", r"\bexceeded expectations\b"],
}

COMPETITION_NEED_PATTERNS = [
    r"\bcompeting in \d+ competitions?\b", r"\b4 competitions?\b", r"\bfour competitions?\b", r"\bmultiple competitions?\b",
    r"\bchampions league\b", r"\beurope\b", r"\bdomestic cup\b", r"\bcup run\b", r"\btitle race\b", r"\brelegation battle\b",
    r"\bfixture congestion\b", r"\bbigger squad\b", r"\bmore depth\b", r"\bsquad depth\b",
]


def extract_achievements(text: str | None) -> dict[str, Any]:
    s = norm_text(text)
    found: dict[str, bool] = {}
    for k, pats in ACHIEVEMENT_PATTERNS.items():
        hit = any(re.search(p, s, re.I) for p in pats)
        if hit and has_negation_near(s, "|".join(pats)):
            # if negated, ignore unless there is also a positive claim elsewhere (simple heuristic)
            # check if any non-negated match
            pos = False
            for p in pats:
                for m in re.finditer(p, s, re.I):
                    pre = s[max(0, m.start()-48):m.start()]
                    if not any(re.search(rf"\b{re.escape(n)}\b", pre) for n in _NEGATION_TOKENS):
                        pos = True
                        break
                if pos:
                    break
            hit = pos
        found[k] = bool(hit)
    # implications
    if found.get("double"):
        found["league_title"] = True
        found["domestic_cup"] = True
    if found.get("treble"):
        found["league_title"] = True
        found["domestic_cup"] = True
        found["continental"] = True
    return found


def detect_commitment(text: str | None) -> dict[str, bool]:
    s = norm_text(text)
    commit_words = [r"\bpromise\b", r"\bguarantee\b", r"\bcommit\b", r"\bdeliver\b", r"\bsecure\b", r"\bbring home\b", r"\blift\b", r"\bensure\b", r"\bwin\b", r"\baim\b", r"\bpush for\b", r"\bgoing for\b", r"\btarget\b"]
    has_commit = any(re.search(p, s, re.I) for p in commit_words)
    league = any(re.search(p, s, re.I) for p in [r"\bleague\b", r"\bliga\b", r"\bpremier league\b", r"\bserie a\b", r"\bbundesliga\b", r"\bligue 1\b", r"\btitle\b"])
    cup = any(re.search(p, s, re.I) for p in [r"\bcup\b", r"\bcopa\b", r"\bpokal\b", r"\bcoppa\b", r"\bcoupe\b", r"\btrophy\b", r"\btrophies\b"])
    continental = any(re.search(p, s, re.I) for p in [r"\bchampions league\b", r"\beuropa\b", r"\bcontinental\b", r"\bucl\b", r"\buefa\b"])
    both = any(re.search(p, s, re.I) for p in [r"\bleague and cup\b", r"\bcup and league\b", r"\bdouble\b", r"\bboth\b", r"\bdomestic double\b"])
    treble = any(re.search(p, s, re.I) for p in [r"\btreble\b", r"\beverything\b", r"\ball trophies\b"])
    win_verb = any(re.search(p, s, re.I) for p in [r"\bwin\b", r"\blift\b", r"\bbring home\b", r"\bsecure\b", r"\bdeliver\b", r"\bguarantee\b", r"\bpromise\b"])
    # interpret "won..." as stated achievement; "will win / promise to win" as commitment.
    future_commit = bool(re.search(r"\b(will|going to|gonna|plan to|aim to|promise to|guarantee|commit to|deliver|ensure we)\b", s))
    league_commit = bool(has_commit and league and (win_verb or future_commit or both or treble))
    cup_commit = bool(has_commit and cup and (win_verb or future_commit or both or treble))
    if both and (win_verb or future_commit or has_commit):
        league_commit = True
        cup_commit = True
    if treble and (win_verb or future_commit or has_commit):
        league_commit = True
        cup_commit = True
        continental = True
    # "We won the league and cup" - user stated as achievement; board request may be arguing based on it.
    # Treat explicit "won league and cup" as a strong claim (achievement) and also as a confidence/commitment signal in context.
    won_league = bool(re.search(r"\bwon\b.{0,40}\bleague\b", s) or re.search(r"\bleague\b.{0,40}\bwon\b", s))
    won_cup = bool(re.search(r"\bwon\b.{0,40}\bcup\b", s) or re.search(r"\bcup\b.{0,40}\bwon\b", s))
    if both and (won_league or won_cup):
        league_commit = True
        cup_commit = True
    return {
        "has_commitment_language": has_commit,
        "league_commitment": league_commit,
        "cup_commitment": cup_commit,
        "continental_commitment": bool(continental and (win_verb or future_commit or treble)),
        "combined": bool(both or treble),
    }


def board_argument_strength(text: str | None, *, kind: str, amount: int | None = None, current_budget: int | None = None) -> dict[str, Any]:
    s = str(text or "")
    sl = norm_text(s)
    achievements = extract_achievements(s)
    commit = detect_commitment(s)
    need_hits = []
    for p in COMPETITION_NEED_PATTERNS:
        if re.search(p, sl, re.I):
            need_hits.append(p)
    # generic need signals
    generic = ["injur", "depth", "squad", "sign", "transfer", "reinforc", "replace", "rebuild", "compete",
               "window", "striker", "midfield", "defend", "goalkeeper", "winger", "fullback", "centre-back",
               "center-back", "contract", "renew", "wage", "salary", "upgrade", "cover", "short", "thin",
               "promotion", "relegation", "europe", "title", "cup", "season objective", "project", "finance",
               "budget", "fund", "cash", "sell", "loan", "youth", "academy", "qualif", "champions league"]
    for g in generic:
        if g in sl:
            need_hits.append(g)
    need_hits = sorted(set(need_hits))
    need_score = min(1.0, 0.20 + 0.08 * len(need_hits) + min(0.25, len(s) / 500.0))
    # achievement boost
    ach_score = 0.0
    if achievements.get("treble"):
        ach_score += 0.45
    elif achievements.get("double"):
        ach_score += 0.35
    else:
        if achievements.get("league_title"):
            ach_score += 0.18
        if achievements.get("domestic_cup"):
            ach_score += 0.12
        if achievements.get("continental"):
            ach_score += 0.18
    if achievements.get("qualification"):
        ach_score += 0.10
    if achievements.get("strong_season"):
        ach_score += 0.08
    if achievements.get("promotion"):
        ach_score += 0.12
    if achievements.get("survival"):
        ach_score += 0.06
    ach_score = min(0.6, ach_score)

    # commitment boost - allow natural achievement-based arguments to count as strong commitment context
    commit_score = 0.0
    if commit.get("league_commitment") and commit.get("cup_commitment"):
        commit_score = 0.30
    elif commit.get("league_commitment") or commit.get("cup_commitment"):
        commit_score = 0.15
    elif commit.get("has_commitment_language"):
        commit_score = 0.08
    # if they claim they already won league+cup/double, treat as strong case
    if achievements.get("double") or achievements.get("treble"):
        commit_score = max(commit_score, 0.25)

    reason_quality = min(1.0, need_score * 0.55 + ach_score + commit_score)
    return {
        "need_score": round(need_score, 3),
        "achievement_score": round(ach_score, 3),
        "commitment_score": round(commit_score, 3),
        "reason_quality": round(reason_quality, 3),
        "achievements": achievements,
        "commitment": commit,
        "need_hits": need_hits[:25],
    }
