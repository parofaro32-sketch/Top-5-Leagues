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


def _money_span_flags(raw: str) -> dict[str, bool]:
    r = str(raw or "").strip().lower()
    has_currency = bool(re.search(r"[€£$]", r))
    has_suffix = bool(re.search(r"\b([kmb]|mil(?:lion)?s?|bn|billion|thousand|grand)\b", r, re.I) or re.search(r"(?<=\d)\s*[kmb]\b", r, re.I))
    plain_numeric = bool(re.fullmatch(r"[€£$]?\s*[\d,.]+", r.strip()))
    return {"has_currency": has_currency, "has_suffix": has_suffix, "plain_numeric": plain_numeric}


def _is_duration_like_span(text: str, span: dict[str, Any], years: int | None) -> bool:
    raw = str(span.get("raw") or "")
    val = int(span.get("value") or 0)
    flags = span.get("flags") or _money_span_flags(raw)
    if flags.get("has_currency") or flags.get("has_suffix"):
        return False
    if not (1 <= val <= 7):
        return False
    s = str(text or "")
    start = int(span.get("start") or 0)
    end = int(span.get("end") or start)
    around = s[max(0, start - 12):min(len(s), end + 20)].lower()
    if re.search(r"\b(years?|yrs?|seasons?)\b", around):
        return True
    if years is not None and val == int(years):
        # "contract 3" / "for 3" without explicit year word but equals parsed duration
        if re.search(r"\b(contract|deal|length|duration|for|term)\b", around):
            return True
    return False


def extract_money_spans(text: str | None) -> list[dict[str, Any]]:
    s = str(text or "")
    out = []
    for m in re.finditer(r"([€£$]?\s*\d[\d,]*(?:\.\d+)?\s*(?:[kmb]|mil(?:lion)?s?|bn|billion|thousand|grand)?)", s, re.I):
        raw = m.group(1).strip()
        val = parse_money(raw)
        if val is None:
            continue
        start, end = m.span(1)
        ctx = s[max(0, start - 60):min(len(s), end + 60)].lower()
        flags = _money_span_flags(raw)
        out.append({
            "raw": raw,
            "value": int(val),
            "start": start,
            "end": end,
            "context": ctx,
            "flags": flags,
        })
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


def _cue_distance(text: str, span: dict[str, Any], cues: list[str], *, window: int = 48) -> int | None:
    """Return min char distance between span and any cue occurrence within window. None if not found."""
    s = str(text or "").lower()
    start = int(span.get("start") or 0)
    end = int(span.get("end") or start)
    lo = max(0, start - window)
    hi = min(len(s), end + window)
    region = s[lo:hi]
    best: int | None = None
    for cue in cues:
        c = str(cue).lower()
        if not c:
            continue
        for m in re.finditer(re.escape(c), region):
            c_start = lo + m.start()
            c_end = lo + m.end()
            if c_end <= start:
                dist = start - c_end
            elif c_start >= end:
                dist = c_start - end
            else:
                dist = 0
            if best is None or dist < best:
                best = dist
    return best


def _span_kind(text: str, span: dict[str, Any], years: int | None) -> str:
    """Classify a money span as 'wage', 'bonus', 'duration', or 'unknown' using local cue proximity."""
    if _is_duration_like_span(text, span, years):
        return "duration"
    raw = str(span.get("raw") or "")
    flags = span.get("flags") or _money_span_flags(raw)
    val = int(span.get("value") or 0)

    # Plain small integers with no currency/suffix are very likely durations or noise in contract text
    if flags.get("plain_numeric") and not flags.get("has_currency") and not flags.get("has_suffix"):
        if 1 <= val <= 7:
            return "duration"
        # bare year-like numbers (2026 etc) should not become wage/bonus accidentally
        if 1900 <= val <= 2100:
            return "duration"

    wage_dist = _cue_distance(text, span, WAGE_CUES, window=50)
    bonus_dist = _cue_distance(text, span, BONUS_CUES, window=50)

    # Strong wage cues
    strong_wage = _cue_distance(text, span, ["/wk", "per week", "weekly", "p/w", "pw", "a week", "per wk", "salary", "wages", "wage"], window=30)
    strong_bonus = _cue_distance(text, span, ["signing bonus", "sign-on bonus", "sign on bonus", "signing fee", "sign-on fee", "upfront", "sign on"], window=40)

    if strong_bonus is not None and (strong_wage is None or strong_bonus <= strong_wage):
        return "bonus"
    if strong_wage is not None and (strong_bonus is None or strong_wage < strong_bonus):
        return "wage"
    if bonus_dist is not None and (wage_dist is None or bonus_dist < wage_dist):
        return "bonus"
    if wage_dist is not None and (bonus_dist is None or wage_dist < bonus_dist):
        return "wage"
    if bonus_dist is not None and wage_dist is not None and bonus_dist == wage_dist:
        # tie - prefer explicit bonus cue "bonus" word if very close, else unknown
        if strong_bonus is not None:
            return "bonus"
        if strong_wage is not None:
            return "wage"
        return "unknown"
    return "unknown"


def extract_contract_terms(text: str | None) -> dict[str, Any]:
    """Extract wage, signing bonus, role, contract years from natural language.
    Returns dict with keys present only when confidently found (bonus may be 0 when explicitly none).
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

    # Explicit no bonus
    if re.search(r"\b(no|without|zero|none)\b\s*(signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|bonus|sign[- ]?on|upfront)\b", s) or \
       re.search(r"\b(signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|bonus|sign[- ]?on|upfront)\b\s*[:=]?\s*(no|none|zero|0)\b", s):
        out["signing_bonus"] = 0

    explicit_no_bonus = out.get("signing_bonus") == 0

    # Classify spans
    classified: list[dict[str, Any]] = []
    for i, sp in enumerate(money_spans):
        kind = _span_kind(raw, sp, years)
        classified.append({"i": i, "span": sp, "kind": kind, "value": int(sp.get("value") or 0)})

    # Pick best bonus and wage from explicit cues (proximity-based)
    wage_pick: dict[str, Any] | None = None
    bonus_pick: dict[str, Any] | None = None

    def cue_score(span: dict[str, Any], cues: list[str], window: int = 50) -> int:
        d = _cue_distance(raw, span, cues, window=window)
        return 10_000 if d is None else d

    for c in classified:
        if c["kind"] == "duration":
            continue
        sp = c["span"]
        val = c["value"]
        if c["kind"] == "wage":
            score = cue_score(sp, WAGE_CUES, 50)
            # prefer closer cue; break ties by plausible wage range
            cand = {"i": c["i"], "value": val, "score": score}
            if wage_pick is None or cand["score"] < wage_pick["score"] or (cand["score"] == wage_pick["score"] and abs(val - 150_000) < abs(int(wage_pick["value"]) - 150_000)):
                wage_pick = cand
        elif c["kind"] == "bonus" and not explicit_no_bonus:
            score = cue_score(sp, BONUS_CUES, 50)
            cand = {"i": c["i"], "value": val, "score": score}
            if bonus_pick is None or cand["score"] < bonus_pick["score"] or (cand["score"] == bonus_pick["score"] and val > int(bonus_pick["value"])):
                bonus_pick = cand

    used_idxs = set()
    if wage_pick:
        out["wage"] = int(wage_pick["value"])
        used_idxs.add(wage_pick["i"])
    if bonus_pick:
        out["signing_bonus"] = int(max(0, bonus_pick["value"]))
        used_idxs.add(bonus_pick["i"])

    # Fallback inference for remaining unknown spans
    remaining = [c for c in classified if c["i"] not in used_idxs and c["kind"] not in ("duration",)]
    # Remove obvious year-like/garbage plain numbers
    filtered_remaining = []
    for c in remaining:
        sp = c["span"]
        flags = sp.get("flags") or _money_span_flags(sp.get("raw", ""))
        val = int(c["value"])
        if flags.get("plain_numeric") and not flags.get("has_currency") and not flags.get("has_suffix"):
            # allow big plain numbers like 100000, 1000000 as possible money; skip tiny
            if val < 1000:
                continue
        filtered_remaining.append(c)
    remaining = filtered_remaining

    if ("wage" not in out) or ("signing_bonus" not in out and not explicit_no_bonus):
        if len(remaining) == 1:
            c = remaining[0]
            val = int(c["value"])
            if "wage" not in out and 1_000 <= val <= 5_000_000:
                out["wage"] = val
                used_idxs.add(c["i"])
            elif "wage" in out and "signing_bonus" not in out and not explicit_no_bonus and val >= 1_000:
                out["signing_bonus"] = val
                used_idxs.add(c["i"])
            elif "wage" not in out:
                out["wage"] = val
                used_idxs.add(c["i"])
        elif len(remaining) >= 2:
            vals = sorted([(int(c["value"]), c) for c in remaining], key=lambda x: x[0])
            low_v, low_c = vals[0]
            high_v, high_c = vals[-1]
            # Common contract case: one wage-like, one bonus-like.
            if "wage" not in out and "signing_bonus" not in out and not explicit_no_bonus:
                if 1_000 <= low_v <= 2_000_000 and high_v >= 50_000 and high_v >= low_v:
                    out["wage"] = int(low_v)
                    out["signing_bonus"] = int(high_v)
                    used_idxs.add(low_c["i"])
                    used_idxs.add(high_c["i"])
                else:
                    # default: assign largest plausible wage to wage, second to bonus if larger
                    wage_cand = None
                    for v, c in reversed(vals):
                        if 1_000 <= v <= 5_000_000:
                            wage_cand = (v, c)
                            break
                    if wage_cand:
                        out["wage"] = int(wage_cand[0])
                        used_idxs.add(wage_cand[1]["i"])
                        rem2 = [c for c in remaining if c["i"] not in used_idxs]
                        if rem2 and not explicit_no_bonus:
                            out["signing_bonus"] = int(max(int(x["value"]) for x in rem2))
                            # mark the max one used
                            mx = max(rem2, key=lambda x: int(x["value"]))
                            used_idxs.add(mx["i"])
            elif "wage" not in out:
                for v, c in vals:
                    if 1_000 <= v <= 5_000_000:
                        out["wage"] = int(v)
                        used_idxs.add(c["i"])
                        break
            elif "signing_bonus" not in out and not explicit_no_bonus:
                rem2 = [c for c in remaining if c["i"] not in used_idxs and int(c["value"]) != int(out.get("wage") or -1)]
                if rem2:
                    mx = max(rem2, key=lambda x: int(x["value"]))
                    if int(mx["value"]) >= 1_000:
                        out["signing_bonus"] = int(mx["value"])
                        used_idxs.add(mx["i"])

    # Final sanity: don't let year value leak as wage/bonus
    if years is not None:
        if int(out.get("wage") or 0) == int(years) and int(years) <= 7:
            out.pop("wage", None)
        if int(out.get("signing_bonus") or -1) == int(years) and int(years) <= 7:
            if not explicit_no_bonus:
                out.pop("signing_bonus", None)

    # If wage and bonus ended up identical with no clear bonus cue, drop bonus
    if out.get("wage") and out.get("signing_bonus") and out["wage"] == out["signing_bonus"] and not explicit_no_bonus:
        bonus_dist_any = None
        wage_dist_any = None
        # check if any cue exists in whole string
        if not re.search(r"\b(signing\s*bonus|sign[- ]?on|upfront|bonus)\b", s):
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
# General intent / entity helpers
# -------------------------

INTENT_SYNONYMS: dict[str, list[str]] = {
    "yes": ["yes", "yeah", "yep", "yup", "sure", "ok", "okay", "fine", "alright", "all right", "do it", "go ahead", "confirm", "accept", "agree", "deal", "sounds good", "works for me", "let's do it", "lets do it"],
    "no": ["no", "nope", "nah", "never", "don't", "dont", "do not", "cancel", "stop", "decline", "reject", "refuse", "not now", "no thanks", "no way"],
    "help": ["help", "how", "what can you do", "what do i do", "commands", "guide", "explain"],
    "status": ["status", "how are we", "where are we", "what's happening", "whats happening", "update", "progress"],
}


def normalize_free_text(text: str | None) -> str:
    """Normalize messy user text: lowercase, strip accents, collapse whitespace, normalize punctuation."""
    s = str(text or "").strip()
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    try:
        s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    except Exception:
        pass
    s = re.sub(r"\s+", " ", s).strip()
    return s


def fuzzy_contains(text: str | None, phrase: str, threshold: float = 0.84) -> bool:
    """True if phrase appears exactly or as a close fuzzy match in text."""
    t = norm_token(text)
    p = norm_token(phrase)
    if not t or not p:
        return False
    if p in t:
        return True
    # token-window fuzzy match
    t_toks = t.split()
    p_toks = p.split()
    if not p_toks:
        return False
    n = len(p_toks)
    if len(t_toks) < n:
        return fuzzy_ratio(t, p) >= threshold
    for i in range(0, len(t_toks) - n + 1):
        window = " ".join(t_toks[i:i + n])
        if fuzzy_ratio(window, p) >= threshold:
            return True
    # also try whole-text ratio for short phrases
    if len(p_toks) <= 3:
        return fuzzy_ratio(t, p) >= (threshold - 0.05)
    return False


def detect_yes_no(text: str | None) -> bool | None:
    """Detect clear yes/no intent in short replies. Returns True/False or None if ambiguous."""
    s = norm_text(text)
    if not s:
        return None
    # explicit negation-first
    if any_regex(s, [r"^\s*(no|nope|nah|never|cancel|stop|decline|reject)\b", r"\bdon'?t\b", r"\bdo not\b", r"\bnot now\b"]):
        # but avoid "not bad" style false positives in very short replies - rare here
        if any_regex(s, [r"^\s*(yes|yeah|yep|sure|ok|okay|fine|alright|do it|go ahead|confirm|accept|agree)\b"]):
            return None
        return False
    if any_regex(s, [r"^\s*(yes|yeah|yep|yup|sure|ok|okay|fine|alright|all right|do it|go ahead|confirm|accept|agree|deal)\b", r"\bsounds good\b", r"\bworks for me\b", r"\blet'?s do it\b"]):
        return True
    # fuzzy for short messages
    if len(s.split()) <= 4:
        y_score = max((fuzzy_ratio(s, p) for p in INTENT_SYNONYMS["yes"]), default=0.0)
        n_score = max((fuzzy_ratio(s, p) for p in INTENT_SYNONYMS["no"]), default=0.0)
        if y_score >= 0.88 and y_score > n_score + 0.08:
            return True
        if n_score >= 0.88 and n_score > y_score + 0.08:
            return False
    return None


def extract_number(text: str | None) -> int | None:
    """Extract a plain integer from text (for shirt numbers, counts, etc.)."""
    s = str(text or "").strip()
    m = re.search(r"\b(\d{1,4})\b", s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def extract_player_mentions(text: str | None) -> list[str]:
    """Extract likely player name mentions from free text. Heuristic: capitalized words, quoted names, or #numbers."""
    raw = str(text or "")
    out: list[str] = []
    # quoted
    for m in re.finditer(r"[\"']([^\"']{2,40})[\"']", raw):
        v = m.group(1).strip()
        if v:
            out.append(v)
    # #number
    for m in re.finditer(r"#\s*(\d{1,3})\b", raw):
        out.append(f"#{m.group(1)}")
    # capitalized sequences (2-4 words) - keep conservative
    for m in re.finditer(r"\b([A-Z][a-zA-ZÀ-ÿ\.\-']+(?:\s+[A-Z][a-zA-ZÀ-ÿ\.\-']+){0,3})\b", raw):
        v = m.group(1).strip()
        if len(v) < 3:
            continue
        if norm_token(v) in {"i", "we", "my", "our", "the", "a", "please", "can", "could", "would", "should", "start", "bench", "sub", "off", "on"}:
            continue
        out.append(v)
    # dedupe preserve order
    seen = set()
    deduped = []
    for v in out:
        k = norm_token(v)
        if not k or k in seen:
            continue
        seen.add(k)
        deduped.append(v)
    return deduped


def parse_flexible_intent(text: str | None) -> dict[str, Any]:
    """General-purpose intent extraction for football manager chat messages."""
    raw = normalize_free_text(text)
    s = norm_text(raw)
    intents: list[str] = []
    flags: dict[str, bool] = {}

    def add(name: str, cond: bool):
        if cond:
            intents.append(name)
            flags[name] = True

    add("agree", bool(detect_yes_no(raw) is True) or any_regex(s, [r"\bwe agree\b", r"\bi agree\b", r"\bdeal\b", r"\baccept\b"]))
    add("reject", bool(detect_yes_no(raw) is False) or any_regex(s, [r"\breject\b", r"\bdecline\b", r"\bno thanks\b"]))
    add("negotiate", any_regex(s, [r"\bcounter\b", r"\bnegotiate\b", r"\bhow about\b", r"\bwhat about\b", r"\bcan you do\b", r"\bmeet me\b", r"\bmiddle ground\b"]))
    add("ask_info", any_regex(s, [r"\bwhat\b", r"\bhow much\b", r"\bwhy\b", r"\bexplain\b", r"\btell me\b", r"\bdetails\b", r"\bstatus\b"]))
    add("urgent", any_regex(s, [r"\burgent\b", r"\basap\b", r"\bnow\b", r"\bquickly\b", r"\bimmediately\b"]))
    add("insult_or_frustration", any_regex(s, [r"\bridiculous\b", r"\bjoke\b", r"\bunacceptable\b", r"\bdisrespect\b", r"\binsult\b", r"\bangry\b"]))

    return {"text": raw, "norm": s, "intents": intents, "flags": flags}


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

    need_hits: list[str] = []
    for p in COMPETITION_NEED_PATTERNS:
        if re.search(p, sl, re.I):
            need_hits.append(p)

    # Generic need signals (broad, football-aware)
    generic = [
        "injur", "depth", "squad", "sign", "transfer", "reinforc", "replace", "rebuild", "compete",
        "window", "striker", "midfield", "defend", "goalkeeper", "winger", "fullback", "centre-back",
        "center-back", "contract", "renew", "wage", "salary", "upgrade", "cover", "short", "thin",
        "promotion", "relegation", "europe", "title", "cup", "season objective", "project", "finance",
        "budget", "fund", "cash", "sell", "loan", "youth", "academy", "qualif", "champions league",
        "keep our best", "retain", "retention", "too small", "above expectations", "bigger clubs",
        "avoid relegation", "stay up", "rebuild", "restructure", "next season", "qualified for europe",
        "playing champions league", "playing europa", "wage budget is too low", "squad is too small",
    ]
    for g in generic:
        if g in sl:
            need_hits.append(g)

    # Specific contextual flags (for board reasoning)
    flags = {
        "injuries": bool(re.search(r"\binjur(y|ies|ed)\b", sl)),
        "small_squad": bool(re.search(r"\b(small|thin) squad\b|lack of depth|not enough depth|need depth\b", sl)),
        "competition_load": bool(re.search(r"\b(multiple|too many|3|three|4|four)\s+competitions?\b|fixture congestion\b|champions league next season|european football next season\b", sl)),
        "rebuild": bool(re.search(r"\brebuild|restructur|refresh|reset\b", sl)),
        "retention": bool(re.search(r"\bkeep (our|my|the) (best|key|top) players\b|retain|retention|renew(al|als)?\b", sl)),
        "promotion": bool(achievements.get("promotion") or re.search(r"\bpromoted\b|promotion\b", sl)),
        "relegation": bool(re.search(r"\bavoid relegation\b|relegation battle\b|stay up\b", sl)),
        "finances_healthy": bool(re.search(r"\bfinances? (are )?(healthy|good|strong|stable)\b|financially (healthy|strong|stable)\b", sl)),
        "struggling_compete": bool(re.search(r"\bstruggl(e|ing)\b|can't compete\b|cannot compete\b|bigger clubs\b", sl)),
        "above_expectations": bool(re.search(r"\babove expectations\b|overachiev(ed|ing)\b|exceeded expectations\b", sl)),
        "qualified_europe": bool(achievements.get("qualification") or re.search(r"\bqualified for europe\b|european football\b|champions league football\b", sl)),
    }
    for k, v in flags.items():
        if v:
            need_hits.append(f"flag:{k}")

    need_hits = sorted(set(need_hits))
    # Need score: give more weight to concrete football reasons
    need_score = min(1.0, 0.18 + 0.07 * len(need_hits) + min(0.20, len(s) / 600.0))
    if any(flags[k] for k in ["injuries", "small_squad", "competition_load", "rebuild", "retention", "relegation", "promotion"]):
        need_score = min(1.0, need_score + 0.08)

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
    if flags.get("above_expectations"):
        ach_score += 0.08
    ach_score = min(0.6, ach_score)

    # commitment boost - allow natural achievement-based arguments to count as strong commitment context
    commit_score = 0.0
    if commit.get("league_commitment") and commit.get("cup_commitment"):
        commit_score = 0.30
    elif commit.get("league_commitment") or commit.get("cup_commitment"):
        commit_score = 0.15
    elif commit.get("has_commitment_language"):
        commit_score = 0.08
    if achievements.get("double") or achievements.get("treble"):
        commit_score = max(commit_score, 0.25)

    reason_quality = min(1.0, need_score * 0.50 + ach_score + commit_score)
    return {
        "need_score": round(need_score, 3),
        "achievement_score": round(ach_score, 3),
        "commitment_score": round(commit_score, 3),
        "reason_quality": round(reason_quality, 3),
        "achievements": achievements,
        "commitment": commit,
        "need_hits": need_hits[:30],
        "flags": flags,
    }
