from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Settings
from data.bundled import get_team, get_team_players, list_all_teams, list_league_keys, resolve_league_key, search_teams, LEAGUE_DISPLAY_NAMES
from db.database import Database, utc_now_iso

logger = logging.getLogger(__name__)

# Approx mapping for optional live API (API-Football league IDs)
LEAGUE_API_IDS = {
    "premier_league": 39,
    "la_liga": 140,
    "bundesliga": 78,
    "serie_a": 135,
    "ligue_1": 61,
}


def _derive_attributes(player: dict[str, Any]) -> dict[str, float]:
    # Deterministic-ish attributes from player id/name for consistent simulation
    seed = int(player.get("api_id") or 0) or sum(ord(c) for c in (player.get("name") or "x"))
    rng = random.Random(seed)
    pos = (player.get("position") or "MID").upper()
    def r(base, spread=12):
        return max(35.0, min(95.0, base + rng.uniform(-spread, spread)))
    # role-tuned baselines
    if pos == "GK":
        base = {"goalkeeping": r(78,10), "positioning": r(72,10), "decision_making": r(70,10), "composure": r(70,10), "passing": r(60,12), "strength": r(70,10)}
        for k in ["shooting","finishing","dribbling","pace","acceleration","tackling","interceptions","defensive_awareness","creativity","crossing","heading","stamina"]:
            base.setdefault(k, r(35,8) if k in {"shooting","finishing","dribbling","crossing","creativity"} else r(55,10))
    elif pos == "DEF":
        base = {"tackling": r(74,10), "interceptions": r(73,10), "defensive_awareness": r(75,10), "strength": r(74,10), "positioning": r(72,10), "heading": r(72,10), "pace": r(68,12), "passing": r(66,12)}
        for k in ["shooting","finishing","dribbling","acceleration","stamina","creativity","crossing","goalkeeping","decision_making","composure"]:
            base.setdefault(k, r(50,12) if k not in {"goalkeeping"} else r(12,5))
    elif pos == "FWD":
        base = {"finishing": r(76,10), "shooting": r(75,10), "dribbling": r(73,10), "pace": r(74,12), "acceleration": r(74,12), "positioning": r(74,10), "composure": r(72,10), "heading": r(68,12)}
        for k in ["passing","strength","stamina","tackling","interceptions","defensive_awareness","creativity","crossing","goalkeeping","decision_making"]:
            base.setdefault(k, r(55,12) if k not in {"goalkeeping"} else r(10,4))
    else:  # MID
        base = {"passing": r(75,10), "decision_making": r(73,10), "positioning": r(72,10), "creativity": r(72,12), "dribbling": r(71,10), "stamina": r(74,10), "tackling": r(66,12), "shooting": r(66,12)}
        for k in ["finishing","pace","acceleration","strength","interceptions","defensive_awareness","crossing","heading","goalkeeping","composure"]:
            base.setdefault(k, r(60,12) if k not in {"goalkeeping"} else r(10,4))
    base["stamina"] = max(base.get("stamina", 70), r(72,8))
    base["decision_making"] = max(base.get("decision_making", 65), r(68,8))
    base["composure"] = max(base.get("composure", 65), r(68,8))
    # keep within 40-95
    for k, v in list(base.items()):
        base[k] = float(max(40.0, min(95.0, v))) if k != "goalkeeping" or pos == "GK" else float(max(5.0, min(20.0, v)))
    if pos == "GK":
        base["goalkeeping"] = float(max(65.0, min(95.0, base.get("goalkeeping", 78))))
    return base


def _split_name(name: str) -> tuple[str | None, str | None]:
    parts = [x for x in str(name or "").strip().split() if x]
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], parts[0]
    return " ".join(parts[:-1]), parts[-1]


def _estimate_overall_potential(player: dict[str, Any]) -> tuple[float, float]:
    attrs = player.get("attributes") or {}
    pos = (player.get("position") or "MID").upper()
    vals = [float(v) for v in attrs.values() if isinstance(v, (int, float))]
    if vals:
        overall = sum(vals) / len(vals)
    else:
        overall = 68.0
    # Position-weighted nudge from key attrs
    key_map = {
        "GK": ["goalkeeping", "positioning", "composure", "decision_making"],
        "DEF": ["tackling", "interceptions", "defensive_awareness", "strength", "positioning"],
        "MID": ["passing", "decision_making", "creativity", "stamina", "dribbling"],
        "FWD": ["finishing", "shooting", "pace", "positioning", "dribbling", "composure"],
    }
    keys = key_map.get(pos, key_map["MID"])
    key_vals = [float(attrs[k]) for k in keys if isinstance(attrs.get(k), (int, float))]
    if key_vals:
        overall = overall * 0.45 + (sum(key_vals) / len(key_vals)) * 0.55
    overall = max(55.0, min(95.0, overall))
    age = player.get("age")
    try:
        age_i = int(age) if age is not None else 25
    except Exception:
        age_i = 25
    # Potential: younger players can grow, older players near/at overall
    if age_i <= 21:
        ceiling = min(95.0, overall + 12 + max(0, 22 - age_i) * 0.6)
    elif age_i <= 24:
        ceiling = min(94.0, overall + 7)
    elif age_i <= 28:
        ceiling = min(93.0, overall + 3)
    elif age_i <= 31:
        ceiling = min(92.0, overall + 1)
    else:
        ceiling = overall
    potential = max(overall, min(95.0, ceiling))
    return round(overall, 1), round(potential, 1)


def normalize_player(player: dict[str, Any], team_api_id: int) -> dict[str, Any]:
    p = dict(player)
    p["api_id"] = int(p.get("api_id") or p.get("id") or 0)
    p["team_api_id"] = int(team_api_id)
    p["name"] = str(p.get("name") or "Unknown").strip()
    # clean wiki disambiguation like "Name (footballer)"
    import re as _re
    p["name"] = _re.sub(r"\s*\([^)]*footballer[^)]*\)\s*", "", p["name"], flags=_re.IGNORECASE).strip()
    p["name"] = _re.sub(r"\s*\([^)]*born\s+\d{4}[^)]*\)\s*", "", p["name"], flags=_re.IGNORECASE).strip()
    p["name"] = _re.sub(r"\s+", " ", p["name"]).strip() or "Unknown"
    p["position"] = (p.get("position") or "MID").upper()
    if p["position"] not in {"GK", "DEF", "MID", "FWD"}:
        pos = (p.get("position") or "").lower()
        if "goal" in pos or pos in {"g", "gk", "keeper"}: p["position"] = "GK"
        elif "def" in pos or "back" in pos or pos in {"cb", "lb", "rb", "lwb", "rwb"}: p["position"] = "DEF"
        elif "att" in pos or "forw" in pos or "strik" in pos or "wing" in pos or pos in {"f", "st", "fw", "cf", "lw", "rw"}: p["position"] = "FWD"
        else: p["position"] = "MID"
    fn, ln = _split_name(p["name"])
    p.setdefault("firstname", p.get("firstname") or fn)
    p.setdefault("lastname", p.get("lastname") or ln)
    p["attributes"] = p.get("attributes") or _derive_attributes(p)
    ovr, pot = _estimate_overall_potential(p)
    if p.get("overall") is None:
        p["overall"] = ovr
    if p.get("potential") is None:
        p["potential"] = pot
    p.setdefault("status", "active")
    p.setdefault("injured", False)
    mv = p.get("market_value")
    try:
        p["market_value"] = int(mv) if mv is not None and str(mv).strip() != "" else None
    except Exception:
        p["market_value"] = None
    wg = p.get("wage")
    try:
        p["wage"] = int(wg) if wg is not None and str(wg).strip() != "" else None
    except Exception:
        p["wage"] = None
    # Compute fair wage if missing
    if p["wage"] is None:
        try:
            from services.transfer_market_service import TransferMarketService
            p["wage"] = TransferMarketService.fair_wage_weekly(p)
        except Exception:
            ovr_f = float(p.get("overall") or 65)
            p["wage"] = max(5000, int(12000 + (max(0, ovr_f - 58) ** 2) * 420))
    # Compute fair market value if missing
    if p["market_value"] is None:
        try:
            from services.transfer_market_service import TransferMarketService
            p["market_value"] = TransferMarketService.fair_market_value(p)
        except Exception:
            pass
    return p


@dataclass
class FootballDataService:
    settings: Settings
    db: Database

    @property
    def season(self) -> int:
        return int(self.settings.football_api_season)

    async def ensure_seeded(self) -> None:
        # Seed bundled teams/players into DB for fast lookup and persistence
        seed_key = "bundled_seed_v13_fair_values"
        meta = await self.db.get_cache_meta(seed_key)
        if not meta:
            for league_key in list_league_keys():
                teams = list_all_teams(league_key)
                await self.db.upsert_teams(teams, self.season)
                for t in teams:
                    players = [normalize_player(p, t["api_id"]) for p in get_team_players(t["api_id"], league_key)]
                    await self.db.upsert_players(players, t["api_id"], self.season)
                    # Remove stale DB rows no longer present in bundled FC27 roster
                    keep_ids = [int(p["api_id"]) for p in players if p.get("api_id") is not None]
                    try:
                        await self.db.delete_stale_players_for_team(int(t["api_id"]), self.season, keep_ids)
                    except Exception:
                        logger.exception("Failed to delete stale players for team_api_id=%s", t.get("api_id"))
            # Seed free agents (outside top 5 leagues / unattached) as a pseudo-team
            try:
                import json as _json
                from pathlib import Path as _Path
                fa_path = _Path(__file__).resolve().parent / "bundled" / "free_agents.json"
                if fa_path.exists():
                    with fa_path.open("r", encoding="utf-8") as f:
                        fa_data = _json.load(f)
                    for t in fa_data.get("teams", []):
                        team = {
                            "api_id": int(t["api_id"]),
                            "name": t.get("name"),
                            "short_name": t.get("short_name"),
                            "code": t.get("code") or t.get("short_name"),
                            "logo": t.get("logo"),
                            "league_id": fa_data.get("league_id"),
                            "league_name": fa_data.get("league_name") or "Free Agents",
                            "country": fa_data.get("country") or "World",
                            "venue_name": t.get("venue_name"),
                        }
                        await self.db.upsert_teams([team], self.season)
                        raw_players = t.get("players", [])
                        players = [normalize_player(p, int(t["api_id"])) for p in raw_players]
                        await self.db.upsert_players(players, int(t["api_id"]), self.season)
                        keep_ids = [int(p["api_id"]) for p in players if p.get("api_id") is not None]
                        await self.db.delete_stale_players_for_team(int(t["api_id"]), self.season, keep_ids)
            except Exception:
                logger.exception("Failed to seed free agents")
            await self.db.set_cache_meta(seed_key, {"ok": True, "season": self.season}, None)

        # Always-on migration: ensure fair wages/market values/heights/potentials for all players
        await self._ensure_fair_player_values()

    async def _ensure_fair_player_values(self) -> None:
        """One-time migration to apply fair wage/market_value/height to all players in DB."""
        mig_key = "fair_player_values_v1"
        if await self.db.get_cache_meta(mig_key):
            return
        try:
            from services.transfer_market_service import TransferMarketService
        except Exception:
            return

        # Build lookup from bundled data for canonical heights/wages/market values
        lookup: dict[tuple[int, int], dict[str, Any]] = {}
        def _add_from_bundled(p: dict[str, Any], team_api_id: int, season: int):
            key = (int(p.get("api_id") or 0), int(season))
            if key[0] <= 0:
                return
            lookup[key] = dict(p)

        for league_key in list_league_keys():
            for t in list_all_teams(league_key):
                for p in get_team_players(t["api_id"], league_key):
                    pp = normalize_player(dict(p), int(t["api_id"]))
                    _add_from_bundled(pp, int(t["api_id"]), self.season)
        try:
            import json as _json
            from pathlib import Path as _Path
            fa_path = _Path(__file__).resolve().parent / "bundled" / "free_agents.json"
            if fa_path.exists():
                with fa_path.open("r", encoding="utf-8") as f:
                    fa_data = _json.load(f)
                for t in fa_data.get("teams", []):
                    for p in t.get("players", []):
                        pp = normalize_player(dict(p), int(t.get("api_id") or 9000001))
                        _add_from_bundled(pp, int(t.get("api_id") or 9000001), self.season)
        except Exception:
            pass

        rows = await self.db.fetchall("SELECT api_id, season, team_api_id, name, age, position, detailed_position, overall, potential, height_cm, market_value, wage, status FROM players")
        updates = []
        for r in rows:
            d = dict(r)
            api_id = int(d.get("api_id") or 0)
            season = int(d.get("season") or self.season)
            team_id = int(d.get("team_api_id") or 0)
            key = (api_id, season)
            src = lookup.get(key) or lookup.get((api_id, self.season))
            is_fa = team_id == 9000001
            ovr = float(d.get("overall") or 65)
            pot = float(d.get("potential") or ovr)
            # Reduce FA potential a bit if needed (keep >= overall)
            if is_fa and pot > ovr + 1:
                pot = max(ovr, pot - 2)
            # Height: prefer src (bundled), else existing, else position default
            h = d.get("height_cm")
            try:
                h = int(h) if h is not None and int(h) > 0 else None
            except Exception:
                h = None
            if src and src.get("height_cm"):
                try:
                    h2 = int(src.get("height_cm"))
                    if h2 > 0:
                        h = h2
                except Exception:
                    pass
            if not h:
                h = {"GK": 190, "DEF": 185, "MID": 180, "FWD": 180}.get(str(d.get("position") or "MID").upper(), 181)

            # Wage & market value: compute fair values; prefer bundled if present
            base_player = {**d, "team_api_id": team_id, "overall": ovr, "potential": pot}
            if src:
                base_player["height_cm"] = h
                if src.get("wage") is not None:
                    base_player["wage"] = src.get("wage")
                if src.get("market_value") is not None:
                    base_player["market_value"] = src.get("market_value")
            fair_wage = TransferMarketService.fair_wage_weekly(base_player)
            fair_mv = TransferMarketService.fair_market_value({**base_player, "market_value": None})
            # If bundled src provides explicit values, use them
            if src and src.get("wage") is not None:
                try: fair_wage = int(src.get("wage"))
                except Exception: pass
            if src and src.get("market_value") is not None:
                try: fair_mv = int(src.get("market_value"))
                except Exception: pass
            if is_fa:
                fair_wage = int(TransferMarketService.fair_wage_weekly({**base_player, "market_value": None, "wage": None, "team_api_id": 9000001}))
                fair_mv = int(TransferMarketService.fair_market_value({**base_player, "market_value": None, "team_api_id": 9000001}))
            updates.append((h, fair_mv, fair_wage, pot, api_id, team_id, season))

        for i in range(0, len(updates), 300):
            chunk = updates[i:i+300]
            await self.db.executemany(
                "UPDATE players SET height_cm=?, market_value=?, wage=?, potential=?, updated_at=? WHERE api_id=? AND team_api_id=? AND season=?",
                [(h, mv, wg, pot, utc_now_iso(), api, tid, s) for (h, mv, wg, pot, api, tid, s) in chunk],
            )
        await self.db.set_cache_meta(mig_key, {"ok": True, "count": len(updates)}, None)

    async def list_teams(self, query: str | None = None, league: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        league_key = resolve_league_key(league) if league else None
        if league and not league_key:
            # treat as text query fallback
            query = query or league
            league_key = None
        await self.ensure_seeded()
        if query:
            rows = await self.db.get_teams(self.season, query=query, limit=limit)
            if rows:
                return [dict(r) for r in rows]
            return search_teams(query, league_key, limit)
        if league_key:
            return list_all_teams(league_key)[:limit]
        rows = await self.db.get_teams(self.season, query=None, limit=limit)
        if rows:
            return [dict(r) for r in rows]
        return list_all_teams(None)[:limit]

    async def get_team(self, team_api_id: int) -> dict[str, Any] | None:
        await self.ensure_seeded()
        row = await self.db.get_team(team_api_id, self.season)
        if row:
            return dict(row)
        t = get_team(team_api_id)
        if not t:
            return None
        return {
            "api_id": int(t["api_id"]), "name": t.get("name"), "short_name": t.get("short_name"), "code": t.get("short_name"),
            "logo": t.get("logo"), "league_id": t.get("league_id"), "league_name": t.get("league_name"), "country": t.get("country"),
        }

    async def get_squad(self, team_api_id: int) -> list[dict[str, Any]]:
        await self.ensure_seeded()
        rows = await self.db.get_players_for_team(team_api_id, self.season)
        if rows:
            out = []
            for r in rows:
                d = dict(r)
                d["attributes"] = self.db.loads(d.get("attributes_json")) or {}
                out.append(d)
            return out
        players = [normalize_player(p, team_api_id) for p in get_team_players(team_api_id)]
        return players

    async def search_players(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        await self.ensure_seeded()
        q = (query or "").strip()
        if not q:
            return []
        rows = await self.db.search_players(q, self.season, limit=limit)
        if rows:
            out = []
            for r in rows:
                d = dict(r)
                d["attributes"] = self.db.loads(d.get("attributes_json")) or {}
                out.append(d)
            return out
        # Fallback: search bundled data
        ql = q.lower()
        results = []
        for league_key in list_league_keys():
            for t in list_all_teams(league_key):
                for p in get_team_players(t["api_id"], league_key):
                    name = str(p.get("name") or "")
                    fn = str(p.get("firstname") or "")
                    ln = str(p.get("lastname") or "")
                    if ql in name.lower() or ql in fn.lower() or ql in ln.lower():
                        d = normalize_player(p, t["api_id"])
                        d["team_name"] = t.get("name")
                        d["league_name"] = t.get("league_name")
                        results.append(d)
                        if len(results) >= limit:
                            break
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break
        results.sort(key=lambda x: (-(x.get("overall") or 0), x.get("name") or ""))
        return results[:limit]

    def list_leagues(self) -> list[dict[str, str]]:
        return [{"key": k, "name": LEAGUE_DISPLAY_NAMES.get(k, k)} for k in list_league_keys()]
