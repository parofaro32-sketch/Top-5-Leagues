from __future__ import annotations

import difflib
import re
import unicodedata
import zlib
from dataclasses import dataclass
from typing import Any

from engine.formations import SUPPORTED_FORMATIONS, get_formation_slots, is_position_compatible
from engine.models import Lineup, LineupEntry, Player


class LineupValidationError(ValueError):
    pass


POSITION_ALIASES = {
    "GK": "GK", "G": "GK", "GOALKEEPER": "GK", "KEEPER": "GK",
    "DEF": "DEF", "D": "DEF", "DEFENDER": "DEF", "CB": "DEF", "LB": "DEF", "RB": "DEF", "LWB": "DEF", "RWB": "DEF", "WB": "DEF", "FB": "DEF",
    "MID": "MID", "M": "MID", "MIDFIELDER": "MID", "CM": "MID", "DM": "MID", "AM": "MID", "CDM": "MID", "CAM": "MID", "LM": "MID", "RM": "MID",
    "FWD": "FWD", "F": "FWD", "ATT": "FWD", "ATTACKER": "FWD", "FORWARD": "FWD", "ST": "FWD", "CF": "FWD", "FW": "FWD", "LW": "FWD", "RW": "FWD", "STRIKER": "FWD",
}


def norm_pos(pos: str | None) -> str:
    p = (pos or "MID").upper().strip()
    return POSITION_ALIASES.get(p, p if p in {"GK", "DEF", "MID", "FWD"} else "MID")


def _strip_accents(s: str) -> str:
    try:
        return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    except Exception:
        return s


def _norm_name(name: str | None) -> str:
    s = str(name or "").strip().lower()
    s = _strip_accents(s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _player_from_any(p: Any) -> dict[str, Any]:
    if isinstance(p, str):
        name = p.strip() or "Unknown"
        return {"api_id": 0, "name": name, "position": "MID", "team_api_id": 0, "number": None}
    if isinstance(p, int):
        return {"api_id": int(p), "name": str(p), "position": "MID", "team_api_id": 0, "number": None}
    if isinstance(p, dict):
        name = p.get("name") or p.get("player_name") or p.get("full_name") or "Unknown"
        raw_id = p.get("api_id", p.get("id", p.get("player_id", 0)))
        try:
            api_id = int(raw_id) if raw_id not in (None, "") else 0
        except Exception:
            api_id = 0
        try:
            tid = int(p.get("team_api_id") or p.get("team_id") or 0)
        except Exception:
            tid = 0
        return {
            "api_id": api_id,
            "name": str(name),
            "position": norm_pos(p.get("position") or p.get("pos") or p.get("role") or "MID"),
            "team_api_id": tid,
            "number": p.get("number"),
        }
    # Player object
    return {
        "api_id": int(getattr(p, "api_id", 0) or 0),
        "name": str(getattr(p, "name", "Unknown") or "Unknown"),
        "position": norm_pos(getattr(p, "position", "MID")),
        "team_api_id": int(getattr(p, "team_api_id", 0) or 0),
        "number": getattr(p, "number", None),
    }


def _synthetic_api_id(team_api_id: int, name: str) -> int:
    base = f"custom:{int(team_api_id)}:{_norm_name(name)}"
    h = zlib.adler32(base.encode("utf-8")) & 0x7FFFFFFF
    # Negative IDs reserved for custom/synthetic players
    return -(h or 1)


def _build_squad_name_index(squad: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    for x in (squad or []):
        sp = _player_from_any(x)
        if sp.get("name"):
            idx[_norm_name(sp["name"])] = sp
        orig = x if isinstance(x, dict) else {}
        for k in ("short_name", "common_name"):
            if orig.get(k):
                idx[_norm_name(str(orig.get(k)))] = sp
    return idx


def resolve_lineup_player(raw: Any, squad_map: dict[int, dict[str, Any]], squad_name_idx: dict[str, dict[str, Any]], team_api_id: int) -> tuple[dict[str, Any], bool]:
    """Return (resolved_player_dict, is_custom). Resolves by id, then by name in squad, else creates custom player."""
    p = _player_from_any(raw)
    pid = int(p.get("api_id") or 0)
    if pid and pid in squad_map:
        sp = dict(squad_map[pid])
        # allow caller to override slot position intent? keep squad pos but preserve explicit if provided
        if isinstance(raw, dict) and raw.get("position"):
            sp["position"] = norm_pos(raw.get("position"))
        if isinstance(raw, dict) and raw.get("name"):
            sp["name"] = str(raw.get("name"))
        sp.setdefault("custom", False)
        return sp, False

    # Try name match in squad
    name_key = _norm_name(p.get("name"))
    if name_key and name_key in squad_name_idx:
        sp = dict(squad_name_idx[name_key])
        if isinstance(raw, dict) and raw.get("position"):
            sp["position"] = norm_pos(raw.get("position"))
        if isinstance(raw, dict) and raw.get("name"):
            sp["name"] = str(raw.get("name"))
        sp.setdefault("custom", False)
        return sp, False

    # Custom player
    custom_name = str(p.get("name") or "Custom Player").strip() or "Custom Player"
    custom_id = pid if pid < 0 else _synthetic_api_id(team_api_id, custom_name)
    # avoid collision with squad IDs (extremely unlikely with negative ids)
    pos = norm_pos(p.get("position") or "MID")
    cp = {
        "api_id": int(custom_id),
        "name": custom_name,
        "position": pos,
        "team_api_id": int(team_api_id),
        "number": p.get("number"),
        "custom": True,
        "attributes": _default_custom_attributes(pos),
    }
    return cp, True


def _default_custom_attributes(pos: str) -> dict[str, float]:
    pos = norm_pos(pos)
    base = 68.0
    attrs = {
        "pace": base, "acceleration": base, "stamina": 72.0, "strength": base,
        "shooting": base, "finishing": base, "passing": base, "crossing": base - 2,
        "dribbling": base, "tackling": base, "interceptions": base, "defensive_awareness": base,
        "positioning": base, "vision": base, "creativity": base, "decision_making": 68.0,
        "composure": 68.0, "heading": base, "goalkeeping": 10.0,
    }
    if pos == "GK":
        attrs.update({"goalkeeping": 72.0, "positioning": 70.0, "passing": 60.0, "tackling": 20.0, "finishing": 15.0, "shooting": 15.0, "dribbling": 35.0, "crossing": 30.0})
    elif pos == "DEF":
        attrs.update({"tackling": 72.0, "interceptions": 71.0, "defensive_awareness": 72.0, "heading": 71.0, "strength": 71.0, "finishing": 45.0, "shooting": 45.0})
    elif pos == "MID":
        attrs.update({"passing": 72.0, "vision": 71.0, "creativity": 70.0, "stamina": 74.0, "decision_making": 70.0})
    elif pos == "FWD":
        attrs.update({"finishing": 72.0, "shooting": 72.0, "pace": 71.0, "positioning": 72.0, "dribbling": 70.0, "tackling": 40.0, "defensive_awareness": 45.0, "interceptions": 40.0})
    return {k: float(max(5.0, min(95.0, v))) for k, v in attrs.items()}


def validate_lineup(*, team_api_id: int, team_name: str, formation: str, starters: list[Any], bench: list[Any], squad: list[Any] | None = None, captain_player_id: int | None = None, goalkeeper_player_id: int | None = None, return_custom: bool = False):
    if formation not in SUPPORTED_FORMATIONS:
        raise LineupValidationError(f"Unsupported formation '{formation}'. Supported: {', '.join(SUPPORTED_FORMATIONS)}")
    slots = get_formation_slots(formation)

    squad_map = {int(_player_from_any(x)["api_id"]): _player_from_any(x) for x in (squad or [])}
    squad_name_idx = _build_squad_name_index(squad)
    custom_players: list[dict[str, Any]] = []
    custom_seen: set[int] = set()

    def _resolve(raw: Any) -> dict[str, Any]:
        rp, is_custom = resolve_lineup_player(raw, squad_map, squad_name_idx, team_api_id)
        # keep explicitly provided position/name intent where sensible
        if isinstance(raw, dict):
            if raw.get("position"):
                rp["position"] = norm_pos(raw.get("position"))
            if raw.get("name") and is_custom:
                rp["name"] = str(raw.get("name"))
        if is_custom and rp["api_id"] not in custom_seen:
            custom_seen.add(int(rp["api_id"]))
            custom_players.append(rp)
        return rp

    s = [_resolve(x) for x in starters]
    b = [_resolve(x) for x in bench]
    if len(s) != 11:
        raise LineupValidationError(f"Starters must contain exactly 11 players (got {len(s)})")
    if len(b) > 9:
        raise LineupValidationError(f"Bench cannot exceed 9 players (got {len(b)})")

    # Ensure unique players by ID and normalized name (prevents repeated players in lineup)
    def _dedupe(players: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
        seen_ids: dict[int, int] = {}
        seen_names: set[str] = set()
        out = []
        dropped = 0
        for pl in players:
            pid = int(pl["api_id"])
            nkey = _norm_name(pl.get("name"))
            if pid in seen_ids or (nkey and nkey in seen_names):
                # duplicate player - skip to avoid repeated names/entries
                dropped += 1
                continue
            seen_ids[pid] = 1
            if nkey:
                seen_names.add(nkey)
            out.append(pl)
        if dropped:
            try:
                import logging as _logging
                _logging.getLogger(__name__).warning("LINEUP_DEDUPE label=%s team=%s dropped=%s in=%s out=%s", label, team_name, dropped, len(players), len(out))
            except Exception:
                pass
        return out

    s = _dedupe(s, "starters")
    b = _dedupe(b, "bench")
    # remove any bench player already in starters
    s_ids = {int(x["api_id"]) for x in s}
    s_names = {_norm_name(x.get("name")) for x in s}
    b = [x for x in b if int(x["api_id"]) not in s_ids and _norm_name(x.get("name")) not in s_names]

    if len(s) != 11:
        raise LineupValidationError(f"Starters must contain exactly 11 unique players (got {len(s)} after removing duplicates)")
    ids = [int(x["api_id"]) for x in s + b]
    if len(set(ids)) != len(ids):
        raise LineupValidationError("Duplicate players found in lineup/bench")

    # GK check
    gks = [x for x in s if norm_pos(x.get("position")) == "GK"]
    if len(gks) != 1:
        raise LineupValidationError(f"Starting XI must include exactly 1 goalkeeper (found {len(gks)})")
    if goalkeeper_player_id and gks[0]["api_id"] != int(goalkeeper_player_id):
        gk_match = next((x for x in s if int(x["api_id"]) == int(goalkeeper_player_id)), None)
        if not gk_match or norm_pos(gk_match.get("position")) != "GK":
            raise LineupValidationError("goalkeeper_player_id must refer to a starting goalkeeper")
    else:
        goalkeeper_player_id = int(gks[0]["api_id"])

    starters_entries: list[LineupEntry] = []
    assigned = set()
    gk_slot = next(sl for sl in slots if sl.group == "GK")
    starters_entries.append(LineupEntry(player_api_id=int(gks[0]["api_id"]), name=str(gks[0]["name"]), position="GK", slot=gk_slot.code))
    assigned.add(int(gks[0]["api_id"]))
    remaining_slots = [sl for sl in slots if sl.group != "GK"]
    remaining_players = [x for x in s if int(x["api_id"]) not in assigned]
    for sl in remaining_slots:
        pick_idx = None
        for i, pl in enumerate(remaining_players):
            pg = norm_pos(pl.get("position"))
            if is_position_compatible(pg, sl.group):
                pick_idx = i
                break
        if pick_idx is None:
            for i, pl in enumerate(remaining_players):
                if norm_pos(pl.get("position")) != "GK":
                    pick_idx = i
                    break
        if pick_idx is None:
            raise LineupValidationError(f"Cannot assign player to slot {sl.code}")
        pl = remaining_players.pop(pick_idx)
        starters_entries.append(LineupEntry(player_api_id=int(pl["api_id"]), name=str(pl["name"]), position=norm_pos(pl.get("position")), slot=sl.code))
    bench_entries = [LineupEntry(player_api_id=int(x["api_id"]), name=str(x["name"]), position=norm_pos(x.get("position")), slot=None) for x in b]
    if captain_player_id is None or int(captain_player_id) not in {e.player_api_id for e in starters_entries}:
        out = next((e for e in starters_entries if e.position != "GK"), starters_entries[0])
        captain_player_id = out.player_api_id
    lu = Lineup(team_api_id=team_api_id, team_name=team_name, formation=formation, starters=starters_entries, bench=bench_entries, captain_player_id=int(captain_player_id), goalkeeper_player_id=int(goalkeeper_player_id))
    try:
        import logging as _logging
        s_ids = [e.player_api_id for e in starters_entries]
        s_names = [_norm_name(e.name) for e in starters_entries]
        _logging.getLogger(__name__).info(
            "LINEUP_VALIDATED team=%s formation=%s starters=%s bench=%s unique_ids=%s unique_names=%s",
            team_name, formation, len(starters_entries), len(bench_entries), len(set(s_ids)), len(set(s_names)),
        )
    except Exception:
        pass
    if return_custom:
        return lu, custom_players
    return lu


def auto_build_lineup(team_api_id: int, team_name: str, squad: list[dict[str, Any]], formation: str = "4-3-3") -> Lineup:
    if formation not in SUPPORTED_FORMATIONS:
        formation = "4-3-3"
    slots = get_formation_slots(formation)

    pool: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for p in squad or []:
        d = _player_from_any(p)
        pid = int(d.get("api_id") or 0)
        if not pid or pid in seen_ids:
            continue
        if bool(p.get("injured")) or str(p.get("status") or "active").lower() in {"injured", "unavailable", "suspended"}:
            # Skip unavailable players for auto-build when alternatives exist.
            pass
        seen_ids.add(pid)
        pool.append(d)

    # Prefer available players; fall back to all if needed.
    available_pool = []
    for d in pool:
        orig = next((x for x in (squad or []) if int((x.get("api_id") or x.get("id") or 0)) == int(d["api_id"])), None)
        if orig and (bool(orig.get("injured")) or str(orig.get("status") or "active").lower() in {"injured", "unavailable", "suspended"}):
            continue
        available_pool.append(d)
    if len(available_pool) >= 11:
        pool = available_pool

    if len(pool) < 11:
        raise LineupValidationError(f"Not enough players in squad to build lineup ({len(pool)})")

    try:
        from engine.models import PlayerAttributes
    except Exception:
        PlayerAttributes = None  # type: ignore

    def _overall_for(pl: dict[str, Any], want_group: str) -> float:
        orig = next((x for x in (squad or []) if int((x.get("api_id") or x.get("id") or 0)) == int(pl["api_id"])), None) or {}
        attrs_raw = orig.get("attributes") or {}
        pos = norm_pos(pl.get("position"))
        base = None
        if PlayerAttributes is not None and attrs_raw:
            try:
                attrs = PlayerAttributes.from_dict(attrs_raw)
                base = float(attrs.overall(want_group if want_group in {"GK", "DEF", "MID", "FWD"} else pos))
            except Exception:
                base = None
        if base is None:
            for k in ("overall", "rating", "ovr"):
                v = orig.get(k, pl.get(k))
                if isinstance(v, (int, float)):
                    base = float(v)
                    break
        if base is None and attrs_raw:
            nums = [float(v) for v in attrs_raw.values() if isinstance(v, (int, float))]
            if nums:
                base = sum(nums) / len(nums)
        if base is None:
            base = 60.0

        fit = 0.0
        if want_group == "GK":
            fit += 25.0 if pos == "GK" else -40.0
        elif pos == want_group:
            fit += 8.0
        elif want_group == "DEF" and pos == "MID":
            fit += 2.5
        elif want_group == "MID" and pos in {"DEF", "FWD"}:
            fit += 2.0
        elif want_group == "FWD" and pos == "MID":
            fit += 2.5
        elif want_group in {"DEF", "MID", "FWD"} and pos == "GK":
            fit -= 50.0

        # Small bonus for exact natural-position matches in common slots
        nat = str(orig.get("detailed_position") or "").upper()
        if want_group == "DEF" and nat in {"CB", "LB", "RB", "LWB", "RWB"}:
            fit += 1.5
        if want_group == "MID" and nat in {"CDM", "CM", "CAM", "LM", "RM", "DM", "AM"}:
            fit += 1.5
        if want_group == "FWD" and nat in {"ST", "CF", "LW", "RW"}:
            fit += 1.5
        return base + fit

    used: set[int] = set()
    starters: list[dict[str, Any]] = []

    # Fill GK first.
    gk_slot = next((sl for sl in slots if sl.group == "GK"), None)
    non_gk_slots = [sl for sl in slots if sl.group != "GK"]
    if gk_slot:
        gk_cands = [p for p in pool if int(p["api_id"]) not in used]
        gk_cands.sort(key=lambda p: (_overall_for(p, "GK"), 1.0 if norm_pos(p.get("position")) == "GK" else 0.0), reverse=True)
        gk_pick = next((p for p in gk_cands if norm_pos(p.get("position")) == "GK"), gk_cands[0])
        starters.append(gk_pick)
        used.add(int(gk_pick["api_id"]))

    # Fill remaining slots by best fit.
    for sl in non_gk_slots:
        cands = [p for p in pool if int(p["api_id"]) not in used]
        cands.sort(key=lambda p: _overall_for(p, sl.group), reverse=True)
        pick = cands[0]
        starters.append(pick)
        used.add(int(pick["api_id"]))

    # Bench: best remaining, prefer at least one GK.
    remaining = [p for p in pool if int(p["api_id"]) not in used]
    remaining.sort(key=lambda p: _overall_for(p, norm_pos(p.get("position"))), reverse=True)
    bench: list[dict[str, Any]] = []
    bench_gk_added = False
    for p in remaining:
        if len(bench) >= 9:
            break
        if norm_pos(p.get("position")) == "GK":
            if not bench_gk_added:
                bench.append(p)
                bench_gk_added = True
            continue
        bench.append(p)
    if not bench_gk_added:
        gk = next((p for p in remaining if norm_pos(p.get("position")) == "GK"), None)
        if gk:
            if len(bench) < 9:
                bench.append(gk)
            elif bench:
                bench[-1] = gk

    return validate_lineup(team_api_id=team_api_id, team_name=team_name, formation=formation, starters=starters, bench=bench, squad=squad)


def parse_lineup_text_block(text: str) -> list[str]:
    raw = str(text or "").replace("\r", "\n").replace("\t", "\n")
    # Normalize common separators to newlines
    raw = raw.replace(";", "\n").replace("|", "\n").replace("/", "\n")
    # Split commas (usually list separators in lineups)
    raw = raw.replace(",", "\n")
    # Split mid-line numbered items: "1. Player 2. Player"
    raw = re.sub(r"(?<=\s)(\d+[\).\-\:])\s+(?=[A-Za-z#\[])", r"\n\1 ", raw)
    # Split on bullet markers mid-line
    raw = re.sub(r"(?<=\s)[•*]\s+", "\n", raw)
    # Split on double+ spaces as a soft separator (common in pasted tables)
    raw = re.sub(r" {2,}", "\n", raw)

    out: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        # skip pure headers/labels
        if re.match(r"^(formation|starters?|starting\s*xi|starting\s*11|line\s*up|lineup|bench|subs?|substitutes?|reserves?|xi)\s*[:\-]?\s*$", s, re.IGNORECASE):
            continue
        # remove "Formation: 4-3-3" inline
        m_form = re.match(r"^formation\s*[:\-]\s*([0-9]{1}-[0-9\-]{1,7})\s*$", s, re.IGNORECASE)
        if m_form:
            continue
        # strip leading numbering like "1. " or "- "
        s = re.sub(r"^\s*(?:\d+[\).\-\:]\s*|[-*•]\s*)", "", s).strip()
        # strip role/slot prefix like "GK:" "RB:" "ST -"
        s = re.sub(r"^(GK|CB|LB|RB|LWB|RWB|CDM|CM|CAM|LM|RM|LW|RW|ST|CF|DEF|MID|FWD)\s*[:\-]\s*", "", s, flags=re.IGNORECASE).strip()
        # strip trailing standalone role token
        s = re.sub(r"\s*[:\-]\s*(GK|CB|LB|RB|LWB|RWB|CDM|CM|CAM|LM|RM|LW|RW|ST|CF|DEF|MID|FWD)\s*$", "", s, flags=re.IGNORECASE).strip()
        if s:
            out.append(s)
    return out


def _clean_player_ref(ref: str) -> str:
    r = str(ref or "").strip()
    # remove leading bullets/numbers like "1." "1)" "- "
    r = re.sub(r"^\s*(?:\d+[\).\-\:]\s*|[-*•]\s*)", "", r).strip()
    # remove trailing/inline position hints like "(GK)" or "- MID"
    r = re.sub(r"\s*[\(\[]\s*(GK|DEF|MID|FWD|G|D|M|F|GOALKEEPER|DEFENDER|MIDFIELDER|FORWARD|ST|CF|LW|RW|CB|LB|RB|CDM|CAM|CM|RM|LM)\s*[\)\]]\s*$", "", r, flags=re.IGNORECASE)
    r = re.sub(r"\s*[-|:]\s*(GK|DEF|MID|FWD|G|D|M|F|GOALKEEPER|DEFENDER|MIDFIELDER|FORWARD|ST|CF|LW|RW|CB|LB|RB|CDM|CAM|CM|RM|LM)\s*$", "", r, flags=re.IGNORECASE)
    # remove leading "#10 " or "10 - " patterns
    r = re.sub(r"^\s*#\s*\d+\s*[-:.]?\s*", "", r)
    r = re.sub(r"^\s*\d+\s*[-:.]\s*", "", r)
    r = re.sub(r"(?i)^\s*no\.?\s*\d+\s*[-:.]?\s*", "", r)
    # remove trailing "#10" or "(10)"
    r = re.sub(r"\s*#\s*\d+\s*$", "", r)
    r = re.sub(r"\s*[\(\[]\s*#?\d{1,3}\s*[\)\]]\s*$", "", r)
    r = re.sub(r"\s+", " ", r).strip(" -:,.")
    return r.strip()


def _extract_ref_number(ref: str) -> int | None:
    r = str(ref or "").strip()
    if r.isdigit():
        try:
            return int(r)
        except Exception:
            return None
    m = re.match(r"^\s*#?\s*(\d{1,3})\s*(?:[-:.]|\s)\s*[A-Za-z].*", r)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    m = re.match(r"^.*\s#\s*(\d{1,3})\s*$", r)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    m = re.match(r"^.*[\(\[]\s*#?(\d{1,3})\s*[\)\]]\s*$", r)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None


def resolve_player_ref(ref: str, squad: list[dict[str, Any]]) -> dict[str, Any] | None:
    r = (ref or "").strip()
    if not r:
        return None
    # clean ref for name matching but also keep number extraction from original
    cleaned = _clean_player_ref(r)
    # pure number
    if cleaned.isdigit():
        num = int(cleaned)
        for p in squad:
            try:
                if int(p.get("number") or -999) == num:
                    return p
            except Exception:
                pass
    num = _extract_ref_number(r)
    rl = _norm_name(cleaned)
    if not rl and num is None:
        return None

    # exact normalized name
    if rl:
        for p in squad:
            if _norm_name(p.get("name")) == rl:
                return p

    # number + partial name: if number matches, prefer it; if also name is close, great
    if num is not None:
        num_cands = []
        for p in squad:
            try:
                if int(p.get("number") or -999) == num:
                    num_cands.append(p)
            except Exception:
                pass
        if len(num_cands) == 1:
            return num_cands[0]
        if len(num_cands) > 1 and rl:
            for p in num_cands:
                pn = _norm_name(p.get("name"))
                if rl in pn or pn in rl:
                    return p
            # fall through to name matching

    if not rl:
        return None

    # contains match
    contains = [p for p in squad if rl in _norm_name(p.get("name")) or _norm_name(p.get("name")) in rl]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        # prefer exact token-set overlap
        rl_tokens = set(rl.split())
        scored = []
        for p in contains:
            pn_tokens = set(_norm_name(p.get("name")).split())
            score = len(rl_tokens & pn_tokens) / max(1, len(rl_tokens | pn_tokens))
            scored.append((score, p))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored and scored[0][0] >= 0.5:
            return scored[0][1]

    # token overlap on full squad
    rl_tokens = set(rl.split())
    if rl_tokens:
        scored = []
        for p in squad:
            pn_tokens = set(_norm_name(p.get("name")).split())
            if not pn_tokens:
                continue
            inter = len(rl_tokens & pn_tokens)
            if inter == 0:
                continue
            score = inter / max(1, len(rl_tokens | pn_tokens))
            # bonus for last name match
            if rl.split()[-1:] == _norm_name(p.get("name")).split()[-1:]:
                score += 0.2
            scored.append((score, inter, p))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        if scored and scored[0][0] >= 0.45:
            return scored[0][2]

    # last-name unique match
    last = rl.split()[-1]
    last_cands = [p for p in squad if _norm_name(p.get("name")).split()[-1:] == [last]]
    if len(last_cands) == 1:
        return last_cands[0]

    # collapsed alnum match (handles N'Golo -> ngolo, hyphens, etc.)
    rl_col = re.sub(r"[^a-z0-9]+", "", rl)
    if len(rl_col) >= 3:
        col_cands = []
        for p in squad:
            pn_col = re.sub(r"[^a-z0-9]+", "", _norm_name(p.get("name")))
            if not pn_col:
                continue
            if rl_col == pn_col or (len(rl_col) >= 4 and (rl_col in pn_col or pn_col in rl_col)):
                col_cands.append(p)
        if len(col_cands) == 1:
            return col_cands[0]
        if col_cands:
            # prefer shortest name diff
            col_cands.sort(key=lambda p: abs(len(re.sub(r"[^a-z0-9]+", "", _norm_name(p.get("name")))) - len(rl_col)))
            return col_cands[0]

    # single-token unique match anywhere in name tokens
    rl_toks = rl.split()
    if len(rl_toks) == 1:
        tok = rl_toks[0]
        tok_cands = [p for p in squad if tok in _norm_name(p.get("name")).split()]
        if len(tok_cands) == 1:
            return tok_cands[0]
        # substring token match (e.g. "Garna" for Garnacho)
        if len(tok) >= 4:
            sub_cands = [p for p in squad if any(tok in t for t in _norm_name(p.get("name")).split())]
            if len(sub_cands) == 1:
                return sub_cands[0]
            if sub_cands:
                sub_cands.sort(key=lambda p: min((len(t) - len(tok) for t in _norm_name(p.get("name")).split() if tok in t), default=99))
                return sub_cands[0]

    # first name unique match
    if len(rl_toks) >= 1:
        first = rl_toks[0]
        if len(first) >= 3:
            first_cands = [p for p in squad if _norm_name(p.get("name")).split()[:1] == [first]]
            if len(first_cands) == 1:
                return first_cands[0]

    # Final fuzzy: best overall overlap if good enough
    if rl_toks:
        best = None
        best_sc = 0.0
        for p in squad:
            pn = _norm_name(p.get("name"))
            pt = set(pn.split())
            rt = set(rl_toks)
            if not pt or not rt:
                continue
            inter = len(rt & pt)
            union = len(rt | pt) or 1
            sc = inter / union
            if rl in pn or pn in rl:
                sc = max(sc, 0.72)
            pn_col = re.sub(r"[^a-z0-9]+", "", pn)
            if rl_col and pn_col and (rl_col in pn_col or pn_col in rl_col):
                sc = max(sc, 0.7)
            if sc > best_sc:
                best_sc = sc
                best = p
        if best and best_sc >= 0.4:
            return best

    # Final fallback: difflib similarity on normalized names (for typos / partial names)
    try:
        names = [_norm_name(p.get("name")) for p in squad]
        # compare against full name and also against last/first tokens
        candidates = []
        for i, pn in enumerate(names):
            if not pn:
                continue
            ratios = [difflib.SequenceMatcher(None, rl, pn).ratio()]
            for tok in pn.split():
                if len(tok) >= 3:
                    ratios.append(difflib.SequenceMatcher(None, rl, tok).ratio())
            if rl_col:
                pn_col = re.sub(r"[^a-z0-9]+", "", pn)
                if pn_col:
                    ratios.append(difflib.SequenceMatcher(None, rl_col, pn_col).ratio())
            sc = max(ratios) if ratios else 0.0
            if sc > 0:
                candidates.append((sc, i))
        candidates.sort(key=lambda x: x[0], reverse=True)
        if candidates:
            top_sc, top_i = candidates[0]
            second_sc = candidates[1][0] if len(candidates) > 1 else 0.0
            # Accept if clearly best or very strong
            if top_sc >= 0.72 or (top_sc >= 0.60 and top_sc - second_sc >= 0.08):
                return squad[top_i]
            # If only one candidate with reasonable score in small squad
            if top_sc >= 0.56 and len(candidates) == 1:
                return squad[top_i]
    except Exception:
        pass

    return None


def _extract_formation(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"\b([3-5]-[1-5]-[1-5](?:-[1-2])?|4-4-2|4-3-3|4-2-3-1|3-5-2|3-4-3|5-3-2|5-4-1|4-1-4-1|4-4-1-1|4-3-2-1)\b", str(text))
    return m.group(1) if m else None


def _split_starters_bench(text: str) -> tuple[str, str]:
    raw = str(text or "").replace("\r", "\n")
    # Split on Bench/Subs headers
    m = re.search(r"(?im)^\s*(bench|subs(?:titutes)?|reserves?)\s*[:\-]?\s*$", raw)
    if m:
        return raw[:m.start()], raw[m.end():]
    # Inline "Bench:" on same line
    m = re.search(r"(?i)\b(bench|subs(?:titutes)?|reserves?)\s*[:\-]\s*", raw)
    if m:
        return raw[:m.start()], raw[m.end():]
    return raw, ""


def _looks_like_json(text: str) -> bool:
    s = str(text or "").strip()
    return s.startswith("{") or s.startswith("[")


def _parse_json_lineup(text: str) -> dict[str, Any] | None:
    try:
        import json
        data = json.loads(text)
    except Exception:
        return None
    if isinstance(data, list):
        return {"starters": data, "bench": []}
    if isinstance(data, dict):
        # common shapes
        formation = data.get("formation") or data.get("shape")
        starters = data.get("starters") or data.get("starting_xi") or data.get("startingXI") or data.get("lineup") or data.get("xi")
        bench = data.get("bench") or data.get("subs") or data.get("substitutes") or data.get("reserves") or []
        if isinstance(starters, list):
            return {"formation": formation, "starters": starters, "bench": bench if isinstance(bench, list) else []}
        # maybe dict of positions
        if starters is None and any(k in data for k in ("GK","DEF","MID","FWD")):
            st = []
            for k in ("GK","DEF","MID","FWD"):
                v = data.get(k)
                if isinstance(v, list):
                    st.extend(v)
            if st:
                return {"formation": formation, "starters": st, "bench": bench if isinstance(bench, list) else []}
    return None


def parse_lineup_submission(formation: str, starters_raw: str, bench_raw: str, squad: list[dict[str, Any]], team_api_id: int, team_name: str):
    formation = (formation or "").strip()
    starters_raw = str(starters_raw or "")
    bench_raw = str(bench_raw or "")

    # JSON support
    json_obj = _parse_json_lineup(starters_raw) if _looks_like_json(starters_raw) else None
    if json_obj:
        if not formation:
            formation = str(json_obj.get("formation") or "").strip()
        starters_in = json_obj.get("starters") or []
        bench_in = json_obj.get("bench") or []
        starter_refs = [str(x).strip() if not isinstance(x, dict) else x for x in starters_in]
        bench_refs = [str(x).strip() if not isinstance(x, dict) else x for x in bench_in]
        # if items are dicts, resolve_player_ref/_player_from_any can handle them in validate path? easier to convert to names/ids
        starter_refs = [x.get("name") or x.get("player_name") or x.get("id") or x.get("api_id") or x.get("number") or str(x) if isinstance(x, dict) else x for x in starters_in]
        bench_refs = [x.get("name") or x.get("player_name") or x.get("id") or x.get("api_id") or x.get("number") or str(x) if isinstance(x, dict) else x for x in bench_in]
        starter_refs = [str(x).strip() for x in starter_refs if str(x).strip()]
        bench_refs = [str(x).strip() for x in bench_refs if str(x).strip()]
    else:
        # Split embedded bench from starters
        if not bench_raw.strip():
            s_part, b_part = _split_starters_bench(starters_raw)
            starters_raw, bench_raw = s_part, b_part
        # Detect formation from text if not provided
        if not formation:
            formation = _extract_formation(starters_raw) or _extract_formation(bench_raw) or "4-3-3"
        starter_refs = parse_lineup_text_block(starters_raw)
        bench_refs = parse_lineup_text_block(bench_raw)

    if not formation:
        formation = _extract_formation(" ".join([str(x) for x in starter_refs])) or "4-3-3"

    formation_norm = formation.strip()
    starter_refs = [r for r in starter_refs if str(r).strip().lower() != formation_norm.lower()]
    bench_refs = [r for r in bench_refs if str(r).strip().lower() != formation_norm.lower()]

    def _scan_refs_from_text(txt: str, squad_list: list[dict[str, Any]], exclude_ids: set[int] | None = None) -> list[str]:
        exclude_ids = exclude_ids or set()
        low = " " + re.sub(r"\s+", " ", str(txt or "").lower()) + " "
        found: list[tuple[int, str]] = []
        for p in squad_list:
            pid = int(p.get("api_id") or 0)
            if not pid or pid in exclude_ids:
                continue
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            nl = _norm_name(name)
            if not nl:
                continue
            # Match by full name or last name token boundary in text
            patterns = [re.escape(nl)]
            parts = nl.split()
            if len(parts) > 1:
                patterns.append(re.escape(parts[-1]))
            # also collapsed form
            col = re.sub(r"[^a-z0-9]+", "", nl)
            low_col = re.sub(r"[^a-z0-9 ]+", "", low)
            matched = False
            for pat in patterns:
                if re.search(r"(?<![a-z0-9])" + pat + r"(?![a-z0-9])", low):
                    matched = True
                    break
            if not matched and len(col) >= 5 and col in re.sub(r"[^a-z0-9]+", "", low):
                matched = True
            if not matched:
                # shirt number as token
                num = p.get("number")
                try:
                    if num is not None and re.search(r"(?<!\d)" + re.escape(str(int(num))) + r"(?!\d)", low):
                        # number alone is ambiguous; only accept if name fragment also present
                        for tok in parts:
                            if len(tok) >= 4 and tok in low:
                                matched = True
                                break
                except Exception:
                    pass
            if matched:
                # position in text for ordering
                pos = low.find(parts[-1]) if parts else low.find(nl)
                if pos < 0:
                    pos = 10**9
                found.append((pos, name))
        found.sort(key=lambda x: x[0])
        # dedupe by name
        seen = set()
        refs = []
        for _, nm in found:
            k = _norm_name(nm)
            if k in seen:
                continue
            seen.add(k)
            refs.append(nm)
        return refs

    # Natural-language intent handling
    combined_raw = f"{formation} {starters_raw} {bench_raw}".strip()
    combined_low = _norm_name(combined_raw)
    auto_intent = any(k in combined_low for k in [
        "strongest xi", "strongest 11", "strongest lineup", "strongest team", "best xi", "best 11",
        "best lineup", "best available", "best team", "best eleven", "strongest eleven",
        "auto pick", "auto lineup", "pick for me", "put out our best", "best possible lineup",
        "strongest possible", "optimal lineup", "optimal xi", "rest tired", "rest the tired",
        "fresh legs", "rotate the squad"
    ])
    fastest_intent = any(k in combined_low for k in ["fastest players", "quickest players", "most pace", "fastest xi", "fastest lineup"])
    youngest_intent = any(k in combined_low for k in ["young players", "youngest players", "youth", "academy players", "youngsters"])
    two_strikers = any(k in combined_low for k in ["two strikers", "2 strikers", "front two", "strike partnership", "pair of strikers"])
    width_intent = any(k in combined_low for k in ["need width", "with width", "make sure we have width", "play wide"])

    if two_strikers and formation_norm in {"4-3-3", "4-2-3-1", "3-4-3", "4-5-1"}:
        formation_norm = "4-4-2"
    if any(k in combined_low for k in ["4 3 3", "433"]):
        formation_norm = "4-3-3"
    if any(k in combined_low for k in ["4 2 3 1", "4231"]):
        formation_norm = "4-2-3-1"
    if any(k in combined_low for k in ["4 4 2", "442"]):
        formation_norm = "4-4-2"
    if any(k in combined_low for k in ["3 5 2", "352"]):
        formation_norm = "3-5-2"
    if width_intent and formation_norm in {"4-4-2", "3-5-2", "5-3-2", "5-4-1", "4-5-1"}:
        formation_norm = "4-3-3"

    if auto_intent or fastest_intent or youngest_intent:
        try:
            lu = auto_build_lineup(team_api_id=team_api_id, team_name=team_name, squad=squad, formation=formation_norm)
        except Exception:
            lu = None
        if lu is not None:
            # Apply attribute-based selection if requested
            if fastest_intent or youngest_intent:
                squad_by_id = {int(p.get("api_id") or 0): p for p in squad}
                def _pace(pl):
                    attrs = (pl.get("attributes") or {})
                    try: return float(attrs.get("pace") or attrs.get("speed") or pl.get("pace") or 0)
                    except Exception: return 0.0
                def _age(pl):
                    try: return float(pl.get("age") or 99)
                    except Exception: return 99.0
                def _ovr(pl):
                    try: return float(pl.get("overall") or pl.get("rating") or 0)
                    except Exception: return 0.0
                try:
                    slots = get_formation_slots(formation_norm)
                except Exception:
                    slots = get_formation_slots("4-3-3")
                pool = [dict(p) for p in squad if int(p.get("api_id") or 0)]
                avail = [p for p in pool if not (bool(p.get("injured")) or str(p.get("status") or "active").lower() in {"injured","unavailable","suspended"})]
                if len(avail) >= 11:
                    pool = avail
                used_ids = set()
                starters = []
                for sl in slots:
                    cands = [p for p in pool if int(p.get("api_id") or 0) not in used_ids]
                    if not cands:
                        continue
                    want = sl.group
                    def score(pl, want=want):
                        pos = norm_pos(pl.get("position"))
                        fit = 1.0 if pos == want else (0.5 if want != "GK" and pos != "GK" else 0.0)
                        if fastest_intent:
                            return (fit, _pace(pl), _ovr(pl))
                        return (fit, -_age(pl), _ovr(pl))
                    cands.sort(key=score, reverse=True)
                    pickp = cands[0]
                    starters.append(pickp)
                    used_ids.add(int(pickp.get("api_id") or 0))
                bench = [p for p in sorted(pool, key=lambda p: _ovr(p), reverse=True) if int(p.get("api_id") or 0) not in used_ids][:9]
                try:
                    lu = validate_lineup(team_api_id=team_api_id, team_name=team_name, formation=formation_norm, starters=starters, bench=bench, squad=squad)
                except Exception:
                    pass
            return lu

    # If parsing gave too few starters, try harder: scan raw text against squad
    if len(starter_refs) < 11:
        scanned = _scan_refs_from_text(starters_raw + "\n" + bench_raw, squad)
        if scanned:
            # If bench wasn't explicitly provided, split scanned list into starters/bench
            if not bench_refs:
                starter_refs = scanned[:11]
                bench_refs = scanned[11:20] if len(scanned) > 11 else []
            else:
                starter_refs = scanned[:11]
    # If we got more than 11 and no bench, split
    if len(starter_refs) > 11 and not bench_refs:
        bench_refs = starter_refs[11:20]
        starter_refs = starter_refs[:11]
    # Still short? pad from bench refs if we have extra there
    if len(starter_refs) < 11 and bench_refs:
        need = 11 - len(starter_refs)
        starter_refs = starter_refs + bench_refs[:need]
        bench_refs = bench_refs[need:]

    if len(starter_refs) != 11:
        # Final attempt: auto-fill from squad by best available not already named
        resolved_now = []
        used_ids_tmp = set()
        for r in starter_refs:
            p = resolve_player_ref(str(r), squad)
            if p:
                pid = int(p.get("api_id") or 0)
                if pid and pid not in used_ids_tmp:
                    used_ids_tmp.add(pid)
                    resolved_now.append(p)
        if resolved_now:
            # fill remaining slots with best available from squad
            from engine.formations import get_formation_slots
            slots = get_formation_slots(formation_norm)
            slot_groups = [s.group for s in slots]
            pos_order_fill = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
            remaining_pool = [p for p in squad if int(p.get("api_id") or 0) not in used_ids_tmp]
            # simple fill: prefer matching missing position groups
            have_groups = [norm_pos(p.get("position")) for p in resolved_now]
            need_groups = []
            for g in slot_groups:
                if g in have_groups:
                    have_groups.remove(g)
                else:
                    need_groups.append(g)
            def _fill_score(p, want_g):
                pg = norm_pos(p.get("position"))
                ovr = p.get("overall") or p.get("rating") or 0
                try: ovr = float(ovr)
                except Exception: ovr = 0.0
                return (1 if pg == want_g else 0, ovr)
            for g in need_groups:
                if len(resolved_now) >= 11:
                    break
                cands = [p for p in remaining_pool if int(p.get("api_id") or 0) not in used_ids_tmp]
                cands.sort(key=lambda p: _fill_score(p, g), reverse=True)
                if cands:
                    pick = cands[0]
                    used_ids_tmp.add(int(pick.get("api_id") or 0))
                    resolved_now.append(pick)
            if len(resolved_now) == 11:
                starter_refs = [str(p.get("name") or p.get("api_id")) for p in resolved_now]

    if len(starter_refs) != 11:
        raise LineupValidationError(f"Could not identify exactly 11 starters (found {len(starter_refs)}). Send player names or shirt numbers in any format, and I'll match them to your squad.")

    starters: list[dict[str, Any]] = []
    missing: list[str] = []
    used: set[int] = set()
    for ref in starter_refs:
        p = resolve_player_ref(str(ref), squad)
        if not p:
            missing.append(str(ref))
            continue
        pid = int(p.get("api_id") or 0)
        if pid in used:
            # skip duplicates silently? better to error clearly
            raise LineupValidationError(f"Duplicate player in starters: {p.get('name')}")
        used.add(pid)
        starters.append(p)
    bench: list[dict[str, Any]] = []
    for ref in bench_refs:
        p = resolve_player_ref(str(ref), squad)
        if not p:
            # be lenient on bench: skip unresolvable bench refs instead of failing whole lineup
            continue
        pid = int(p.get("api_id") or 0)
        if pid in used:
            continue
        used.add(pid)
        bench.append(p)
    if missing:
        # Try one more fuzzy pass: scan missing ref as substring across squad
        still_missing = []
        for ref in missing:
            p = resolve_player_ref(str(ref), squad)
            if p and int(p.get("api_id") or 0) not in used:
                pid = int(p.get("api_id") or 0)
                used.add(pid)
                starters.append(p)
                continue
            # last resort: pick best name-overlap candidate not used
            rl = _norm_name(_clean_player_ref(ref))
            best = None
            best_score = 0.0
            for cand in squad:
                cid = int(cand.get("api_id") or 0)
                if cid in used:
                    continue
                cn = _norm_name(cand.get("name"))
                if not rl or not cn:
                    continue
                rt = set(rl.split())
                ct = set(cn.split())
                inter = len(rt & ct)
                union = len(rt | ct) or 1
                sc = inter / union
                if rl in cn or cn in rl:
                    sc = max(sc, 0.75)
                if sc > best_score:
                    best_score = sc
                    best = cand
            if best and best_score >= 0.34:
                pid = int(best.get("api_id") or 0)
                used.add(pid)
                starters.append(best)
            else:
                still_missing.append(ref)
        missing = still_missing
    if missing:
        sample = ", ".join(missing[:8]) + ("..." if len(missing) > 8 else "")
        raise LineupValidationError(f"Could not match players: {sample}. Try using the exact player name or shirt number from your squad.")
    lu = validate_lineup(team_api_id=team_api_id, team_name=team_name, formation=formation.strip(), starters=starters, bench=bench, squad=squad)
    return lu


def format_squad_for_manager(squad: list[dict[str, Any]]) -> str:
    pos_order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    rows = []
    for p in squad:
        pos = norm_pos(p.get("position"))
        name = p.get("name") or "Unknown"
        num = p.get("number")
        ovr = p.get("overall")
        age = p.get("age")
        status = p.get("status") or "active"
        injured = p.get("injured")
        flag = " (injured)" if injured or str(status).lower() == "injured" else ""
        num_s = f"#{num}" if num is not None else "#-"
        ovr_s = f"{int(round(float(ovr)))}" if isinstance(ovr, (int, float)) else (f"{int(round(float(p.get('rating',0))))}" if p.get("rating") else "-")
        age_s = f"{age}" if age is not None else "-"
        rows.append((pos_order.get(pos, 9), pos, f"{num_s:>3} {name} [{pos}] OVR:{ovr_s} Age:{age_s}{flag}"))
    rows.sort(key=lambda x: (x[0], x[2]))
    lines = [r[2] for r in rows]
    # chunk to fit discord
    out = "\n".join(lines)
    if len(out) > 1900:
        out = "\n".join(lines[:40]) + f"\n... and {len(lines)-40} more"
    return out


def parse_break_subs_text(text: str, state: Any, side: str) -> list[dict[str, Any]]:
    """Parse break substitution text into apply_break_actions payloads.
    Accepts flexible formats: OFF -> ON, ON for OFF, OFF out ON in, names/numbers, etc.
    """
    raw = str(text or "").replace("\r", "\n").strip()
    if not raw:
        raise LineupValidationError("No substitutions provided.")

    team = state.team(side)

    def row_for(ps):
        p = state.players_index.get(state.player_key(side, ps.player_api_id))
        p_name = getattr(p, "name", None) if p else None
        ovr = 0.0
        pace = 0.0
        try:
            attrs = getattr(p, "attributes", None) if p else None
            if attrs is not None:
                ovr = float(getattr(attrs, "overall", 0) or 0)
                pace = float(getattr(attrs, "pace", 0) or 0)
        except Exception:
            pass
        if not ovr:
            try:
                ovr = float(getattr(p, "overall", 0) or 0)
            except Exception:
                ovr = 0.0
        return {
            "api_id": int(ps.player_api_id),
            "name": ps.name or p_name or str(ps.player_api_id),
            "number": getattr(p, "number", None) if p else None,
            "position": norm_pos(getattr(ps, "position", None) or getattr(p, "position", None)),
            "raw_position": str(getattr(ps, "position", None) or getattr(p, "position", "") or ""),
            "team_api_id": team.team.api_id,
            "on_pitch": bool(ps.on_pitch),
            "red_card": bool(ps.red_card),
            "injured": bool(getattr(ps, "injured", False)),
            "stamina": float(getattr(ps, "stamina", 100.0) or 100.0),
            "yellow_cards": int(getattr(ps, "yellow_cards", 0) or 0),
            "rating": float(getattr(ps, "rating", 6.5) or 6.5),
            "overall": float(ovr or 0.0),
            "pace": float(pace or 0.0),
            "is_goalkeeper": bool(getattr(ps, "is_goalkeeper", False) or norm_pos(getattr(ps, "position", None)) == "GK"),
        }

    all_rows = [row_for(ps) for ps in state.player_states.values() if ps.team_side == side]
    on_pitch = [r for r in all_rows if r["on_pitch"]]
    bench = [r for r in all_rows if (not r["on_pitch"]) and (not r["red_card"]) and (not r["injured"])]
    unavailable_bench = [r for r in all_rows if (not r["on_pitch"]) and (r["red_card"] or r["injured"])]

    def _squad_hint() -> str:
        op = ", ".join([f"{r['name']}" + (f" (#{r['number']})" if r.get('number') is not None else "") for r in on_pitch[:14]])
        bn = ", ".join([f"{r['name']}" + (f" (#{r['number']})" if r.get('number') is not None else "") for r in bench[:14]])
        parts = []
        if op:
            parts.append(f"On pitch: {op}")
        if bn:
            parts.append(f"Bench: {bn}")
        hint = " | ".join(parts)
        return hint[:600] if hint else ""

    def clean_ref(r: str) -> str:
        r = str(r or "").strip()
        r = re.sub(r"^\s*\d+[\).\-\:]\s*", "", r).strip()  # leading list number
        r = re.sub(r"^\s*[-*•]\s*", "", r).strip()
        r = re.sub(r"\b(bring\s+on|take\s+off|come\s+on|come\s+off|put\s+on|swap\s+in|swap\s+out|sub\s*out|sub\s*in|sub\s*off|sub\s*on)\b", " ", r, flags=re.IGNORECASE)
        r = re.sub(r"\b(out|off|in|on)\b", " ", r, flags=re.IGNORECASE)
        r = re.sub("[\"'\u2018\u2019\u201c\u201d]", "", r)
        r = re.sub(r"\b(please|pls|thanks|thank you|now)\b", " ", r, flags=re.IGNORECASE)
        r = re.sub(r"\s+", " ", r).strip(" -:>.,;/\\")
        return r.strip()

    def resolve_in_pool(ref: str, pool: list[dict[str, Any]]):
        if not ref:
            return None
        p = resolve_player_ref(ref, pool)
        if p:
            return p
        c = clean_ref(ref)
        if c and c != ref:
            p = resolve_player_ref(c, pool)
            if p:
                return p
        # try without numbers/punct noise
        c2 = re.sub(r"[^a-zA-Z0-9\s]", " ", c or ref)
        c2 = re.sub(r"\s+", " ", c2).strip()
        if c2 and c2 not in {ref, c}:
            p = resolve_player_ref(c2, pool)
            if p:
                return p
        return None

    def find_player_any(ref: str):
        ref = str(ref or "").strip()
        if not ref:
            return None, None
        p = resolve_in_pool(ref, on_pitch)
        if p:
            return p, "on_pitch"
        p = resolve_in_pool(ref, bench)
        if p:
            return p, "bench"
        p = resolve_in_pool(ref, unavailable_bench)
        if p:
            return p, "unavailable"
        p = resolve_in_pool(ref, all_rows)
        if p:
            is_on = any(r["api_id"] == p["api_id"] and r["on_pitch"] for r in all_rows)
            return p, ("on_pitch" if is_on else "bench")
        return None, None

    def split_chunks(text: str) -> list[str]:
        # Split by newlines and semicolons first
        base = []
        for line in str(text or "").replace(";", "\n").split("\n"):
            line = line.strip()
            if line:
                base.append(line)
        out = []
        for line in base:
            # If a line contains multiple substitution separators, split by comma.
            sep_count = len(re.findall(r"(->|=>|→|<->|\bfor\b|\bto\b|\breplaces?\b|\bwith\b|/|\||\s-\s)", line, flags=re.IGNORECASE))
            if sep_count > 1 and "," in line:
                parts = [p.strip() for p in line.split(",") if p.strip()]
                if len(parts) > 1:
                    out.extend(parts)
                    continue
            out.append(line)
        return out

    def try_parse_in_out(ln: str):
        # Prefix style: IN X OUT Y / OUT Y IN X
        m_in = re.search(r"\b(?:in|on)\b\s*[:=\-]?\s+(.+)", ln, flags=re.IGNORECASE)
        m_out = re.search(r"\b(?:out|off)\b\s*[:=\-]?\s+(.+)", ln, flags=re.IGNORECASE)
        if m_in and m_out:
            in_ref = re.split(r"\b(?:out|off)\b\s*[:=\-]?\s+", m_in.group(1), maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,.;:-")
            out_ref = re.split(r"\b(?:in|on)\b\s*[:=\-]?\s+", m_out.group(1), maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,.;:-")
            in_ref = re.sub(r"^\s*\d+[\).\-\:]\s*", "", in_ref).strip()
            out_ref = re.sub(r"^\s*\d+[\).\-\:]\s*", "", out_ref).strip()
            if in_ref and out_ref:
                return out_ref, in_ref

        # Suffix style: X out, Y in / X in, Y out
        m_out_s = re.search(r"(.+?)\s+\b(?:out|off)\b(?=$|\s*[,.;]|\s+\b(?:and|,|in|on)\b)", ln, flags=re.IGNORECASE)
        m_in_s = re.search(r"(.+?)\s+\b(?:in|on)\b(?=$|\s*[,.;]|\s+\b(?:and|,|out|off)\b)", ln, flags=re.IGNORECASE)
        if m_out_s and m_in_s:
            out_ref = m_out_s.group(1).strip(" ,.;:-")
            in_ref = m_in_s.group(1).strip(" ,.;:-")
            # clean up common leading words
            out_ref = re.sub(r"(?i)^\s*(?:and|then|,)+\s*", "", out_ref).strip()
            in_ref = re.sub(r"(?i)^\s*(?:and|then|,)+\s*", "", in_ref).strip()
            # if they captured the whole prefix including the other keyword, re-split
            if re.search(r"\b(?:in|on)\b", out_ref, flags=re.IGNORECASE):
                out_ref = re.split(r"\b(?:in|on)\b", out_ref, maxsplit=1, flags=re.IGNORECASE)[-1].strip(" ,.;:-")
            if re.search(r"\b(?:out|off)\b", in_ref, flags=re.IGNORECASE):
                in_ref = re.split(r"\b(?:out|off)\b", in_ref, maxsplit=1, flags=re.IGNORECASE)[-1].strip(" ,.;:-")
            out_ref = re.sub(r"^\s*\d+[\).\-\:]\s*", "", out_ref).strip()
            in_ref = re.sub(r"^\s*\d+[\).\-\:]\s*", "", in_ref).strip()
            if in_ref and out_ref and in_ref.lower() != out_ref.lower():
                return out_ref, in_ref
        return None

    def try_parse_separator(ln: str):
        seps = [
            r"\s*(?:->|=>|→)\s*",
            r"\s*(?:<->)\s*",
            r"\s+(?:for|to|with|replaces|replace)\s+",
            r"\s*/\s*",
            r"\s*\|\s*",
            r"\s+>\s+",
            r"\s+<\s+",
            r"\s+-\s+",
        ]
        for sep in seps:
            parts = re.split(sep, ln, maxsplit=1, flags=re.IGNORECASE)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return parts[0].strip(), parts[1].strip()
        # comma as separator if exactly two sides
        if "," in ln:
            parts = [p.strip() for p in ln.split(",", 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                return parts[0], parts[1]
        return None

    def try_smart_split(ln: str):
        toks = ln.split()
        if len(toks) < 2:
            return None
        candidates = []
        for i in range(1, len(toks)):
            left = " ".join(toks[:i]).strip()
            right = " ".join(toks[i:]).strip()
            if not left or not right:
                continue
            lp, lloc = find_player_any(left)
            rp, rloc = find_player_any(right)
            if not lp or not rp:
                continue
            if int(lp.get("api_id")) == int(rp.get("api_id")):
                continue
            score = 0
            if {lloc, rloc} == {"on_pitch", "bench"}:
                score += 100
            elif {lloc, rloc} == {"on_pitch", "unavailable"} or {lloc, rloc} == {"bench", "unavailable"}:
                score += 20
            # prefer splits that use cleaned refs less ambiguously (longer names)
            score += min(len(left), 30) * 0.1 + min(len(right), 30) * 0.1
            candidates.append((score, left, right, lp, lloc, rp, rloc))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        best = candidates[0]
        # accept best candidate - pick_off_on will validate/fallback
        if best[0] < 0:
            return None
        return best[1], best[2]

    def pick_off_on(a_p, a_loc, b_p, b_loc, a_ref, b_ref):
        if a_loc == "on_pitch" and b_loc == "bench":
            return a_p, b_p
        if a_loc == "bench" and b_loc == "on_pitch":
            return b_p, a_p
        if a_loc == "on_pitch" and b_loc == "on_pitch":
            raise LineupValidationError(f"Both players are on the pitch: {a_p.get('name')} and {b_p.get('name')}. Pick one player on the pitch and one on the bench.")
        if a_loc == "bench" and b_loc == "bench":
            raise LineupValidationError(f"Both players are on the bench: {a_p.get('name')} and {b_p.get('name')}. Pick one player on the pitch and one on the bench.")
        if a_loc == "unavailable" or b_loc == "unavailable":
            bad = a_p if a_loc == "unavailable" else b_p
            reason = "injured" if bad.get("injured") else ("sent off" if bad.get("red_card") else "unavailable")
            raise LineupValidationError(f"{bad.get('name')} is {reason} and cannot be substituted.")
        # fallback: assume first is off, second is on
        return a_p, b_p

    def _intent_subs(text: str):
        t = _norm_name(text)
        if not t:
            return None
        subs = []
        used_off = set()
        used_on = set()

        def _pos_group(r):
            return norm_pos(r.get("position"))
        def _is_gk(r):
            return _pos_group(r) == "GK" or bool(r.get("is_goalkeeper"))
        def _bench_same_group(off_r, prefer_group=None):
            g = prefer_group or _pos_group(off_r)
            cands = [r for r in bench if int(r.get("api_id") or 0) not in used_on and not _is_gk(r)]
            # prefer same group
            same = [r for r in cands if _pos_group(r) == g]
            pool = same if same else cands
            if not pool:
                return None
            pool.sort(key=lambda r: (float(r.get("overall") or 0), float(r.get("rating") or 0)), reverse=True)
            return pool[0]
        def _bench_by_group(group):
            cands = [r for r in bench if int(r.get("api_id") or 0) not in used_on and not _is_gk(r) and _pos_group(r) == group]
            cands.sort(key=lambda r: (float(r.get("overall") or 0), float(r.get("rating") or 0)), reverse=True)
            return cands[0] if cands else None
        def _bench_fastest(group=None):
            cands = [r for r in bench if int(r.get("api_id") or 0) not in used_on and not _is_gk(r)]
            if group:
                g = [r for r in cands if _pos_group(r) == group]
                if g: cands = g
            cands.sort(key=lambda r: (float(r.get("pace") or 0), float(r.get("overall") or 0)), reverse=True)
            return cands[0] if cands else None
        def _add(off_r, on_r, reason):
            if not off_r or not on_r:
                return False
            oid, nid = int(off_r.get("api_id") or 0), int(on_r.get("api_id") or 0)
            if not oid or not nid or oid == nid or oid in used_off or nid in used_on:
                return False
            if bool(off_r.get("red_card")) or bool(off_r.get("injured")) or not bool(off_r.get("on_pitch")):
                return False
            if bool(on_r.get("on_pitch")) or bool(on_r.get("red_card")) or bool(on_r.get("injured")):
                return False
            used_off.add(oid); used_on.add(nid)
            subs.append({"off_player_id": oid, "off_name": str(off_r.get("name") or oid), "on_player_id": nid, "on_name": str(on_r.get("name") or nid), "reason": reason})
            return True

        # tired / stamina / fresh legs
        tired_intent = any(k in t for k in ["tired", "fatigue", "fatigued", "fresh legs", "low stamina", "exhausted", "legs gone"])
        stamina_thresh = None
        m_st = re.search(r"below\s*(\d{1,3})\s*%?\s*stamina", t)
        if not m_st:
            m_st = re.search(r"stamina\s*(?:below|under|less than)\s*(\d{1,3})", t)
        if m_st:
            try: stamina_thresh = float(m_st.group(1))
            except Exception: stamina_thresh = 50.0
            tired_intent = True
        if tired_intent:
            thr = stamina_thresh if stamina_thresh is not None else 55.0
            cands = [r for r in on_pitch if not _is_gk(r) and float(r.get("stamina") or 100) < thr and int(r.get("api_id") or 0) not in used_off]
            cands.sort(key=lambda r: float(r.get("stamina") or 100))
            for off_r in cands[:3]:
                on_r = _bench_same_group(off_r)
                _add(off_r, on_r, "low stamina")

        # injured on-pitch (rare at break, but handle)
        if any(k in t for k in ["injured player", "replace the injured", "take off the injured"]):
            cands = [r for r in on_pitch if bool(r.get("injured")) and int(r.get("api_id") or 0) not in used_off]
            for off_r in cands[:2]:
                on_r = _bench_same_group(off_r)
                _add(off_r, on_r, "injured")

        # yellow-carded
        if "yellow" in t or "booked" in t:
            # try to find position mentioned
            yg = None
            if "midfielder" in t or "midfield" in t: yg = "MID"
            elif "defender" in t or "defence" in t or "defense" in t: yg = "DEF"
            elif "striker" in t or "forward" in t or "attacker" in t: yg = "FWD"
            elif "winger" in t: yg = None
            cands = [r for r in on_pitch if int(r.get("yellow_cards") or 0) > 0 and int(r.get("api_id") or 0) not in used_off and not _is_gk(r)]
            if yg:
                cands = [r for r in cands if _pos_group(r) == yg] or cands
            cands.sort(key=lambda r: (-int(r.get("yellow_cards") or 0), float(r.get("stamina") or 100)))
            for off_r in cands[:2]:
                on_r = _bench_same_group(off_r)
                _add(off_r, on_r, "yellow card risk")

        # defensive/protect lead
        if any(k in t for k in ["defender on", "get a defender on", "someone defensive on", "more defensive", "protect the lead", "protecting the lead", "shut up shop", "see it out"]):
            on_r = _bench_by_group("DEF")
            if on_r:
                # take off an attacker or lowest-rated non-GK
                cands = [r for r in on_pitch if int(r.get("api_id") or 0) not in used_off and not _is_gk(r)]
                fwd = [r for r in cands if _pos_group(r) == "FWD"]
                pool = fwd if fwd else cands
                pool.sort(key=lambda r: (float(r.get("overall") or 0), float(r.get("stamina") or 100)))
                if pool:
                    _add(pool[0], on_r, "defensive change")

        # attacking/more goals
        if any(k in t for k in ["attacking options", "more attacking", "need a goal", "go for it", "more forward", "attacker on", "striker on"]):
            on_r = _bench_by_group("FWD")
            if on_r:
                cands = [r for r in on_pitch if int(r.get("api_id") or 0) not in used_off and not _is_gk(r)]
                defs = [r for r in cands if _pos_group(r) == "DEF"]
                pool = defs if defs else cands
                pool.sort(key=lambda r: (float(r.get("overall") or 0), float(r.get("stamina") or 100)))
                if pool:
                    _add(pool[0], on_r, "attacking change")

        # midfielder to control game
        if any(k in t for k in ["midfielder to control", "control the game", "control midfield", "midfielder on"]):
            on_r = _bench_by_group("MID")
            if on_r:
                cands = [r for r in on_pitch if int(r.get("api_id") or 0) not in used_off and not _is_gk(r) and _pos_group(r) != "MID"]
                if not cands:
                    cands = [r for r in on_pitch if int(r.get("api_id") or 0) not in used_off and not _is_gk(r)]
                cands.sort(key=lambda r: (float(r.get("overall") or 0), float(r.get("stamina") or 100)))
                if cands:
                    _add(cands[0], on_r, "midfield control")

        # fastest winger
        if any(k in t for k in ["fastest winger", "quick winger", "pace on the wing", "fast winger"]):
            on_r = _bench_fastest("FWD")
            if on_r:
                cands = [r for r in on_pitch if int(r.get("api_id") or 0) not in used_off and _pos_group(r) == "FWD"]
                cands.sort(key=lambda r: (float(r.get("pace") or 0), float(r.get("stamina") or 100)))
                if cands:
                    _add(cands[0], on_r, "pace change")

        # two changes up front
        if any(k in t for k in ["two changes up front", "2 changes up front", "change both strikers", "change the front two", "two strikers off"]):
            on_fwds = [r for r in bench if int(r.get("api_id") or 0) not in used_on and _pos_group(r) == "FWD"]
            on_fwds.sort(key=lambda r: float(r.get("overall") or 0), reverse=True)
            off_fwds = [r for r in on_pitch if int(r.get("api_id") or 0) not in used_off and _pos_group(r) == "FWD"]
            off_fwds.sort(key=lambda r: (float(r.get("stamina") or 100), float(r.get("overall") or 0)))
            for off_r, on_r in zip(off_fwds[:2], on_fwds[:2]):
                _add(off_r, on_r, "front line change")

        return subs if subs else None

    # Try intent-based subs for natural language requests
    intent_subs = _intent_subs(raw)
    if intent_subs:
        # If the message is purely intent-based (no explicit pair patterns), return intent subs
        has_explicit_pair = False
        for _ln in str(raw).replace(";", "\n").split("\n"):
            _ln = _ln.strip()
            if not _ln:
                continue
            if try_parse_separator(_ln) or try_parse_in_out(_ln):
                has_explicit_pair = True
                break
        if not has_explicit_pair:
            # also check for simple "X for Y" name pairs by attempting smart split? skip to be safe
            return intent_subs

    chunks = split_chunks(raw)

    if not chunks:
        raise LineupValidationError("No substitutions provided.")

    # Pre-pass: pair halves and single-name lines
    def _has_in_kw(t: str) -> bool:
        return bool(re.search(r"\b(?:in|on)\b", t, flags=re.IGNORECASE))
    def _has_out_kw(t: str) -> bool:
        return bool(re.search(r"\b(?:out|off)\b", t, flags=re.IGNORECASE))
    def _looks_like_full_pair(text: str) -> bool:
        t = str(text or "").strip()
        if try_parse_separator(t):
            return True
        if try_parse_in_out(t):
            return True
        if try_smart_split(t):
            return True
        # has both in and out keywords - likely a pair
        if _has_in_kw(t) and _has_out_kw(t):
            return True
        return False
    def _is_half(text: str) -> str | None:
        t = str(text or "").strip()
        if _looks_like_full_pair(t):
            return None
        has_in = _has_in_kw(t)
        has_out = _has_out_kw(t)
        if has_out and not has_in:
            return "out"
        if has_in and not has_out:
            return "in"
        return None

    expanded: list[str] = []
    i = 0
    while i < len(chunks):
        cur = chunks[i].strip()
        if not cur:
            i += 1
            continue
        if _looks_like_full_pair(cur):
            expanded.append(cur)
            i += 1
            continue
        half = _is_half(cur)
        if half and i + 1 < len(chunks):
            nxt = chunks[i + 1].strip()
            nh = _is_half(nxt)
            if nh and nh != half:
                if half == "out":
                    expanded.append(cur + " | " + nxt)
                else:
                    expanded.append(nxt + " | " + cur)
                i += 2
                continue
        # single token/name line - try to pair with next single
        if i + 1 < len(chunks):
            nxt = chunks[i + 1].strip()
            if nxt and not _looks_like_full_pair(nxt) and _is_half(nxt) is None:
                expanded.append(cur + " -> " + nxt)
                i += 2
                continue
        expanded.append(cur)
        i += 1
    chunks = expanded

    out: list[dict[str, Any]] = []
    seen_off: set[int] = set()
    seen_on: set[int] = set()

    for ln in chunks:
        orig = ln
        ln = re.sub(r"(?i)^\s*(sub|substitution|change|subs?)\s*[:\-]\s*", "", ln).strip()
        ln = re.sub(r"(?i)^\s*(please\s+)?(make|do|apply)\s+(a\s+)?(sub|substitution|change)s?\s*[:\-]?\s*", "", ln).strip()
        if not ln:
            continue

        pair = try_parse_separator(ln)
        if not pair:
            pair = try_parse_in_out(ln)
        if not pair:
            pair = try_smart_split(ln)
        if not pair:
            hint = _squad_hint()
            msg = f"Could not parse sub: '{orig}'. Use 'OFF -> ON' (example: Rashford -> Garnacho)."
            if hint:
                msg += f"\n{hint}"
            raise LineupValidationError(msg)

        a_ref, b_ref = pair[0].strip(), pair[1].strip()
        a_ref = re.sub(r"^\s*\d+[\).\-\:]\s*", "", a_ref).strip(" -:>.,")
        b_ref = re.sub(r"^\s*\d+[\).\-\:]\s*", "", b_ref).strip(" -:>.,")

        a_p, a_loc = find_player_any(a_ref)
        b_p, b_loc = find_player_any(b_ref)
        if not a_p:
            c = clean_ref(a_ref)
            if c:
                a_p, a_loc = find_player_any(c)
        if not b_p:
            c = clean_ref(b_ref)
            if c:
                b_p, b_loc = find_player_any(c)

        if not a_p or not b_p:
            missing = []
            if not a_p:
                missing.append(a_ref)
            if not b_p:
                missing.append(b_ref)
            hint = _squad_hint()
            msg = f"Could not match player(s): {', '.join(missing)}. Use a name or shirt number from your squad."
            if hint:
                msg += f"\n{hint}"
            raise LineupValidationError(msg)

        off_p, on_p = pick_off_on(a_p, a_loc, b_p, b_loc, a_ref, b_ref)

        off_id = int(off_p["api_id"])
        on_id = int(on_p["api_id"])
        if off_id == on_id:
            raise LineupValidationError("Cannot substitute a player for themselves.")
        if off_id in seen_off or on_id in seen_on:
            raise LineupValidationError(f"Duplicate substitution: {off_p.get('name')} -> {on_p.get('name')}")
        seen_off.add(off_id)
        seen_on.add(on_id)
        out.append({
            "off_player_id": off_id,
            "off_name": str(off_p.get("name") or off_id),
            "on_player_id": on_id,
            "on_name": str(on_p.get("name") or on_id),
            "reason": "manager substitution",
        })

    if not out:
        raise LineupValidationError("No valid substitutions found.")
    return out
