from __future__ import annotations

import re
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


def _norm_name(name: str | None) -> str:
    s = str(name or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
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
    pool = []
    for p in squad:
        d = _player_from_any(p)
        if not d.get("api_id"):
            continue
        pool.append(d)
    if len(pool) < 11:
        raise LineupValidationError(f"Not enough players in squad to build lineup ({len(pool)})")
    # score players by a rough overall if attributes present
    def score(pl: dict[str, Any], want: str) -> float:
        pos = norm_pos(pl.get("position"))
        base = 50.0
        if isinstance(p, dict):
            pass
        # try original for attributes
        orig = next((x for x in squad if int((x.get("api_id") or x.get("id") or 0)) == pl["api_id"]), None)
        attrs = (orig or {}).get("attributes") or {}
        if attrs:
            vals = list(attrs.values())
            nums = [float(v) for v in vals if isinstance(v, (int, float))]
            if nums:
                base = sum(nums) / len(nums)
        bonus = 0.0
        if pos == want: bonus += 6
        elif want == "DEF" and pos == "MID": bonus += 2
        elif want == "MID" and pos in {"DEF", "FWD"}: bonus += 2
        elif want == "FWD" and pos == "MID": bonus += 2
        elif want == "GK" and pos == "GK": bonus += 20
        elif want == "GK": bonus -= 30
        return base + bonus

    used = set()
    starters: list[dict[str, Any]] = []
    for sl in slots:
        cands = [p for p in pool if p["api_id"] not in used]
        if sl.group == "GK":
            cands_sorted = sorted(cands, key=lambda p: score(p, "GK"), reverse=True)
            # prefer actual GK
            gk = next((p for p in cands_sorted if norm_pos(p["position"]) == "GK"), cands_sorted[0])
            starters.append(gk); used.add(gk["api_id"])
        else:
            cands_sorted = sorted(cands, key=lambda p: score(p, sl.group), reverse=True)
            pick = cands_sorted[0]
            starters.append(pick); used.add(pick["api_id"])
    # bench: best remaining, ensure at least 1 GK if possible
    remaining = [p for p in pool if p["api_id"] not in used]
    remaining.sort(key=lambda p: score(p, norm_pos(p["position"])), reverse=True)
    bench: list[dict[str, Any]] = []
    gk_on_bench = any(norm_pos(p["position"]) == "GK" for p in remaining)
    bench_gk_added = False
    for p in remaining:
        if len(bench) >= 9:
            break
        if norm_pos(p["position"]) == "GK":
            if not bench_gk_added:
                bench.append(p); bench_gk_added = True
            continue
        bench.append(p)
    if not bench_gk_added and gk_on_bench and len(bench) == 9:
        # swap last outfield for GK
        gk = next(p for p in remaining if norm_pos(p["position"]) == "GK")
        bench[-1] = gk
    elif not bench_gk_added and gk_on_bench and len(bench) < 9:
        gk = next(p for p in remaining if norm_pos(p["position"]) == "GK")
        bench.append(gk)
    return validate_lineup(team_api_id=team_api_id, team_name=team_name, formation=formation, starters=starters, bench=bench, squad=squad)


def parse_lineup_text_block(text: str) -> list[str]:
    raw = (text or "").replace(",", "\n")
    out: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        # strip leading numbering like "1. " or "- "
        s = re.sub(r"^\s*(?:\d+[\).\-\:]\s*|[-*]\s*)", "", s).strip()
        if s:
            out.append(s)
    return out


def resolve_player_ref(ref: str, squad: list[dict[str, Any]]) -> dict[str, Any] | None:
    r = (ref or "").strip()
    if not r:
        return None
    # by shirt number
    if r.isdigit():
        num = int(r)
        for p in squad:
            try:
                if int(p.get("number") or -999) == num:
                    return p
            except Exception:
                pass
    rl = _norm_name(r)
    # exact name
    for p in squad:
        if _norm_name(p.get("name")) == rl:
            return p
    # first+last token match / contains
    for p in squad:
        pn = _norm_name(p.get("name"))
        if rl and (rl in pn or pn in rl):
            return p
    # match on last name token
    r_parts = rl.split()
    if r_parts:
        last = r_parts[-1]
        cands = [p for p in squad if _norm_name(p.get("name")).split()[-1:] == [last]]
        if len(cands) == 1:
            return cands[0]
    return None


def parse_lineup_submission(formation: str, starters_raw: str, bench_raw: str, squad: list[dict[str, Any]], team_api_id: int, team_name: str):
    starter_refs = parse_lineup_text_block(starters_raw)
    bench_refs = parse_lineup_text_block(bench_raw)
    if len(starter_refs) != 11:
        raise LineupValidationError(f"Starting XI must contain exactly 11 players (found {len(starter_refs)}).")
    starters: list[dict[str, Any]] = []
    missing: list[str] = []
    used: set[int] = set()
    for ref in starter_refs:
        p = resolve_player_ref(ref, squad)
        if not p:
            missing.append(ref)
            continue
        pid = int(p.get("api_id") or 0)
        if pid in used:
            raise LineupValidationError(f"Duplicate player in starters: {p.get('name')}")
        used.add(pid)
        starters.append(p)
    bench: list[dict[str, Any]] = []
    for ref in bench_refs:
        p = resolve_player_ref(ref, squad)
        if not p:
            missing.append(ref)
            continue
        pid = int(p.get("api_id") or 0)
        if pid in used:
            raise LineupValidationError(f"Duplicate player in lineup/bench: {p.get('name')}")
        used.add(pid)
        bench.append(p)
    if missing:
        raise LineupValidationError("Could not match players: " + ", ".join(missing[:8]) + ("..." if len(missing) > 8 else ""))
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
