from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from engine.formations import SUPPORTED_FORMATIONS, get_formation_slots, is_position_compatible
from engine.models import Lineup, LineupEntry, Player


class LineupValidationError(ValueError):
    pass


POSITION_ALIASES = {
    "GK": "GK", "G": "GK", "GOALKEEPER": "GK",
    "DEF": "DEF", "D": "DEF", "DEFENDER": "DEF", "CB": "DEF", "LB": "DEF", "RB": "DEF", "LWB": "DEF", "RWB": "DEF", "WB": "DEF",
    "MID": "MID", "M": "MID", "MIDFIELDER": "MID", "CM": "MID", "DM": "MID", "AM": "MID", "CDM": "MID", "CAM": "MID", "LM": "MID", "RM": "MID",
    "FWD": "FWD", "F": "FWD", "ATT": "FWD", "ATTACKER": "FWD", "FORWARD": "FWD", "ST": "FWD", "CF": "FWD", "FW": "FWD", "LW": "FWD", "RW": "FWD",
}


def norm_pos(pos: str | None) -> str:
    p = (pos or "MID").upper().strip()
    return POSITION_ALIASES.get(p, p if p in {"GK", "DEF", "MID", "FWD"} else "MID")


def _player_from_any(p: Any) -> dict[str, Any]:
    if isinstance(p, dict):
        return {
            "api_id": int(p.get("api_id") or p.get("id") or 0),
            "name": p.get("name") or p.get("player_name") or "Unknown",
            "position": norm_pos(p.get("position") or p.get("pos") or "MID"),
            "team_api_id": int(p.get("team_api_id") or p.get("team_id") or 0),
            "number": p.get("number"),
        }
    # Player object
    return {"api_id": int(getattr(p, "api_id", 0)), "name": getattr(p, "name", "Unknown"), "position": norm_pos(getattr(p, "position", "MID")), "team_api_id": int(getattr(p, "team_api_id", 0)), "number": getattr(p, "number", None)}


def validate_lineup(*, team_api_id: int, team_name: str, formation: str, starters: list[Any], bench: list[Any], squad: list[Any] | None = None, captain_player_id: int | None = None, goalkeeper_player_id: int | None = None) -> Lineup:
    if formation not in SUPPORTED_FORMATIONS:
        raise LineupValidationError(f"Unsupported formation '{formation}'. Supported: {', '.join(SUPPORTED_FORMATIONS)}")
    slots = get_formation_slots(formation)
    s = [_player_from_any(x) for x in starters]
    b = [_player_from_any(x) for x in bench]
    if len(s) != 11:
        raise LineupValidationError(f"Starters must contain exactly 11 players (got {len(s)})")
    if len(b) > 9:
        raise LineupValidationError(f"Bench cannot exceed 9 players (got {len(b)})")
    ids = [x["api_id"] for x in s + b]
    if len(set(ids)) != len(ids):
        raise LineupValidationError("Duplicate players found in lineup/bench")
    squad_map = {int(_player_from_any(x)["api_id"]): _player_from_any(x) for x in (squad or [])}
    if squad_map:
        for pid in ids:
            if pid not in squad_map:
                raise LineupValidationError(f"Player {pid} is not in the selected squad")
    # GK check
    gks = [x for x in s if norm_pos(x.get("position")) == "GK"]
    if len(gks) != 1:
        raise LineupValidationError(f"Starting XI must include exactly 1 goalkeeper (found {len(gks)})")
    if goalkeeper_player_id and gks[0]["api_id"] != int(goalkeeper_player_id):
        # allow but normalize
        gk_match = next((x for x in s if x["api_id"] == int(goalkeeper_player_id)), None)
        if not gk_match or norm_pos(gk_match.get("position")) != "GK":
            raise LineupValidationError("goalkeeper_player_id must refer to a starting goalkeeper")
    else:
        goalkeeper_player_id = gks[0]["api_id"]
    # formation compatibility (relaxed): ensure counts by group feasible
    starters_entries: list[LineupEntry] = []
    assigned = set()
    # assign GK slot first
    gk_slot = next(sl for sl in slots if sl.group == "GK")
    starters_entries.append(LineupEntry(player_api_id=gks[0]["api_id"], name=gks[0]["name"], position="GK", slot=gk_slot.code))
    assigned.add(gks[0]["api_id"])
    remaining_slots = [sl for sl in slots if sl.group != "GK"]
    remaining_players = [x for x in s if x["api_id"] not in assigned]
    # greedy compatible assignment
    for sl in remaining_slots:
        pick_idx = None
        for i, pl in enumerate(remaining_players):
            pg = norm_pos(pl.get("position"))
            if is_position_compatible(pg, sl.group):
                pick_idx = i
                break
        if pick_idx is None:
            # allow flexible: take any outfield
            for i, pl in enumerate(remaining_players):
                if norm_pos(pl.get("position")) != "GK":
                    pick_idx = i
                    break
        if pick_idx is None:
            raise LineupValidationError(f"Cannot assign player to slot {sl.code}")
        pl = remaining_players.pop(pick_idx)
        starters_entries.append(LineupEntry(player_api_id=pl["api_id"], name=pl["name"], position=norm_pos(pl.get("position")), slot=sl.code))
    bench_entries = [LineupEntry(player_api_id=x["api_id"], name=x["name"], position=norm_pos(x.get("position")), slot=None) for x in b]
    if captain_player_id is None or int(captain_player_id) not in {e.player_api_id for e in starters_entries}:
        # default captain: first outfield starter
        out = next((e for e in starters_entries if e.position != "GK"), starters_entries[0])
        captain_player_id = out.player_api_id
    return Lineup(team_api_id=team_api_id, team_name=team_name, formation=formation, starters=starters_entries, bench=bench_entries, captain_player_id=int(captain_player_id), goalkeeper_player_id=int(goalkeeper_player_id))


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
