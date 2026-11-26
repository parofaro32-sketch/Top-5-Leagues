from __future__ import annotations

import difflib
import json
import logging
import random
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from db.database import Database, utc_now_iso

logger = logging.getLogger(__name__)


def money_to_int(text: str | int | float | None) -> int | None:
    """Canonical money parser. Understands 1m, 1.5m, 500k, 2 million, 750 thousand, commas, currency."""
    try:
        from services.nlu_service import parse_money as _parse_money
        return _parse_money(text)
    except Exception:
        # Fallback: minimal safe parsing
        if text is None:
            return None
        if isinstance(text, (int, float)):
            try:
                return int(round(float(text)))
            except Exception:
                return None
        s = str(text).strip().lower().replace("€", "").replace("$", "").replace("£", "")
        s = re.sub(r"(?<=\d)\s*(millions?|mil)\b", "m", s)
        s = re.sub(r"\b(millions?|mil)\b", "m", s)
        s = re.sub(r"(?<=\d)\s*(thousands?|thou|grand|k)\b", "k", s)
        s = re.sub(r"\b(thousands?|thou|grand)\b", "k", s)
        s = s.replace(",", "")
        m = re.search(r"(\d+(?:\.\d+)?)\s*([kmb])?", s)
        if not m:
            return None
        val = float(m.group(1))
        suf = (m.group(2) or "").lower()
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suf, 1)
        return int(round(val * mult))


def fmt_money(n: int | float | None) -> str:
    try:
        v = int(n or 0)
    except Exception:
        v = 0
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        if v % 1_000_000 == 0:
            return f"{sign}€{v//1_000_000}M"
        return f"{sign}€{v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"{sign}€{v//1_000}K"
    return f"{sign}€{v}"


def negotiation_pressure_bar(pressure: int, max_pressure: int = 10) -> str:
    p = max(0, min(int(pressure), int(max_pressure)))
    pct = int(round((p / max(1, max_pressure)) * 100))
    filled = int(round((p / max(1, max_pressure)) * 10))
    filled = max(0, min(10, filled))
    bar = "█" * filled + "░" * (10 - filled)
    return f"[Negotiation Pressure: {bar} {pct}%]"


ROLE_TIERS = [
    "Crucial", "Important", "Rotation", "Sporadic", "Prospect"
]
ROLE_ALIASES = {
    "crucial": "Crucial", "key": "Crucial", "star": "Crucial", "essential": "Crucial",
    "important": "Important", "regular": "Important", "starter": "Important", "first team": "Important", "first-team": "Important",
    "rotation": "Rotation", "squad": "Rotation", "rotational": "Rotation",
    "sporadic": "Sporadic", "backup": "Sporadic", "fringe": "Sporadic", "bench": "Sporadic", "reserve": "Sporadic",
    "prospect": "Prospect", "youngster": "Prospect", "future": "Prospect", "youth": "Prospect",
}


def normalize_role(text: str | None) -> str | None:
    if not text:
        return None
    s = str(text).strip().lower()
    if s in ROLE_ALIASES:
        return ROLE_ALIASES[s]
    # Check longer aliases first to avoid "star" matching inside "starter"
    for k, v in sorted(ROLE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(k)}\b", s):
            return v
    for r in ROLE_TIERS:
        if re.search(rf"\b{re.escape(r.lower())}\b", s):
            return r
    return None


@dataclass
class PlayerValuation:
    market_value: int
    min_accept_fee: int
    fair_fee: int
    asking_fee: int
    wage_demand: int
    signing_bonus: int
    contract_years: int
    desired_role: str


class TransferMarketService:
    def __init__(self, db: Database):
        self.db = db

    async def get_player(self, player_api_id: int, season_year: int = 2026) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM players WHERE api_id=? AND season=? ORDER BY updated_at DESC LIMIT 1",
            (int(player_api_id), int(season_year)),
        )
        if row:
            return dict(row)
        row = await self.db.fetchone(
            "SELECT * FROM players WHERE api_id=? ORDER BY season DESC, updated_at DESC LIMIT 1",
            (int(player_api_id),),
        )
        return dict(row) if row else None

    async def find_team_by_name(self, team_name: str, season_year: int = 2026) -> dict[str, Any] | None:
        q = (team_name or "").strip()
        if not q:
            return None
        row = await self.db.fetchone(
            "SELECT * FROM teams WHERE season=? AND lower(name)=lower(?) LIMIT 1",
            (int(season_year), q),
        )
        if row:
            return dict(row)
        row = await self.db.fetchone(
            "SELECT * FROM teams WHERE season=? AND lower(name) LIKE ? ORDER BY name ASC LIMIT 1",
            (int(season_year), f"%{q.lower()}%"),
        )
        return dict(row) if row else None

    async def find_player_in_team(self, team_api_id: int, player_query: str, season_year: int = 2026) -> dict[str, Any] | None:
        q_raw = (player_query or "").strip()
        if not q_raw:
            return None

        def _norm(s: str | None) -> str:
            s = str(s or "").strip().lower()
            try:
                s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
            except Exception:
                pass
            s = re.sub(r"[^a-z0-9\s]", " ", s)
            s = re.sub(r"\s+", " ", s).strip()
            return s

        qn = _norm(q_raw)
        rows = await self.db.fetchall(
            "SELECT * FROM players WHERE team_api_id=? AND season=?",
            (int(team_api_id), int(season_year)),
        )
        players = [dict(r) for r in rows]
        if not players:
            # Fallback: try any season for this team (handles season mismatch)
            rows = await self.db.fetchall(
                "SELECT * FROM players WHERE team_api_id=? ORDER BY season DESC",
                (int(team_api_id),),
            )
            players = [dict(r) for r in rows]
        if not players:
            return None

        # Numeric lookup (api_id or shirt number)
        if q_raw.isdigit():
            qi = int(q_raw)
            for p in players:
                if int(p.get("api_id") or 0) == qi:
                    return p
            for p in players:
                try:
                    if int(p.get("number") or -1) == qi:
                        return p
                except Exception:
                    pass

        # Exact normalized name
        for p in players:
            if _norm(p.get("name")) == qn and qn:
                return p

        # Contains / contained
        contains: list[dict[str, Any]] = []
        if qn:
            for p in players:
                pn = _norm(p.get("name"))
                if not pn:
                    continue
                if qn in pn or pn in qn:
                    contains.append(p)
        if len(contains) == 1:
            return contains[0]
        if contains:
            # Prefer best token overlap among contains candidates
            qt = set(qn.split())
            scored: list[tuple[float, int, dict[str, Any]]] = []
            for p in contains:
                pn = _norm(p.get("name"))
                pt = set(pn.split())
                sc = len(qt & pt) / max(1, len(qt | pt))
                scored.append((sc, -len(pn), p))
            scored.sort(reverse=True)
            return scored[0][2]

        # Token overlap
        qt = set(qn.split())
        best: dict[str, Any] | None = None
        best_sc = 0.0
        if qt:
            for p in players:
                pn = _norm(p.get("name"))
                pt = set(pn.split())
                if not pt:
                    continue
                inter = len(qt & pt)
                if inter == 0:
                    continue
                sc = inter / max(1, len(qt | pt))
                # bonus for last-name match
                if qn.split() and pn.split() and qn.split()[-1] == pn.split()[-1]:
                    sc += 0.15
                if sc > best_sc:
                    best_sc = sc
                    best = p
        if best is not None and best_sc >= 0.34:
            return best

        # Difflib fallback on normalized names
        if qn:
            names = [_norm(p.get("name")) for p in players]
            candidates: list[tuple[float, int]] = []
            for i, pn in enumerate(names):
                if not pn:
                    continue
                ratios = [difflib.SequenceMatcher(None, qn, pn).ratio()]
                for tok in pn.split():
                    if len(tok) >= 3:
                        ratios.append(difflib.SequenceMatcher(None, qn, tok).ratio())
                qcol = re.sub(r"[^a-z0-9]+", "", qn)
                pcol = re.sub(r"[^a-z0-9]+", "", pn)
                if qcol and pcol:
                    ratios.append(difflib.SequenceMatcher(None, qcol, pcol).ratio())
                sc = max(ratios) if ratios else 0.0
                if sc > 0:
                    candidates.append((sc, i))
            candidates.sort(reverse=True)
            if candidates:
                top_sc, top_i = candidates[0]
                second_sc = candidates[1][0] if len(candidates) > 1 else 0.0
                if top_sc >= 0.72 or (top_sc >= 0.58 and (top_sc - second_sc) >= 0.08):
                    return players[top_i]
                if top_sc >= 0.62 and len(candidates) == 1:
                    return players[top_i]
        return None

    @staticmethod
    def _is_free_agent_player(player: dict[str, Any]) -> bool:
        tid = int(player.get("team_api_id") or 0)
        if tid == 9000001:
            return True
        tname = str(player.get("team_name") or "").strip().lower()
        return tname in {"free agents", "free agent"}

    @staticmethod
    def fair_wage_weekly(player: dict[str, Any]) -> int:
        ovr = float(player.get("overall") or player.get("rating") or 65)
        pot = float(player.get("potential") or ovr)
        age = int(player.get("age") or 25)
        pos = str(player.get("position") or "MID").upper()
        is_fa = TransferMarketService._is_free_agent_player(player)
        wage = 12000 + (max(0.0, ovr - 58) ** 2) * 420
        wage += max(0.0, pot - ovr) * 800
        if age <= 20:
            age_mult = 0.82
        elif age <= 23:
            age_mult = 0.92
        elif age <= 25:
            age_mult = 0.97
        elif age <= 29:
            age_mult = 1.0
        elif age <= 32:
            age_mult = 0.95
        elif age <= 34:
            age_mult = 0.88
        else:
            age_mult = 0.78
        wage *= age_mult
        if pos == "GK":
            wage *= 0.95
        if is_fa:
            wage *= 0.45
        wage = max(5000, min(600000, wage))
        if wage >= 100000:
            step = 5000
        elif wage >= 50000:
            step = 2500
        elif wage >= 20000:
            step = 1000
        else:
            step = 500
        return int(max(3000, round(wage / step) * step))

    @staticmethod
    def fair_market_value(player: dict[str, Any]) -> int:
        ovr = float(player.get("overall") or player.get("rating") or 65)
        pot = float(player.get("potential") or ovr)
        age = int(player.get("age") or 25)
        pos = str(player.get("position") or "MID").upper()
        is_fa = TransferMarketService._is_free_agent_player(player)
        # Honor explicit stored market_value if present (trusted DB/bundled value)
        mv = player.get("market_value")
        if mv is not None and str(mv).strip() != "":
            try:
                mv_i = int(mv)
                if mv_i > 0:
                    return mv_i
            except Exception:
                pass
        value = 500000 + (max(0.0, ovr - 55) ** 2.15) * 85000
        upside = max(0.0, pot - ovr)
        value += (upside * upside * 180000) + (upside * 900000)
        if age <= 20:
            age_mult = 1.18 + max(0, 21 - age) * 0.03
        elif age <= 23:
            age_mult = 1.12
        elif age <= 26:
            age_mult = 1.05
        elif age <= 29:
            age_mult = 1.0
        elif age <= 31:
            age_mult = 0.86
        elif age <= 33:
            age_mult = 0.72
        else:
            age_mult = 0.58
        value *= age_mult
        if pos in {"FWD", "ST", "LW", "RW", "CF"}:
            value *= 1.03
        elif pos == "GK":
            value *= 0.97
        elif pos in {"DEF", "CB", "LB", "RB"}:
            value *= 0.99
        if is_fa:
            value *= 0.45
        value = max(200000, min(200000000, value))
        if value >= 50000000:
            step = 500000
        elif value >= 10000000:
            step = 250000
        elif value >= 1000000:
            step = 100000
        else:
            step = 50000
        return int(max(150000, round(value / step) * step))

    def value_player(self, player: dict[str, Any]) -> PlayerValuation:
        ovr = float(player.get("overall") or player.get("rating") or 65)
        pot = float(player.get("potential") or ovr)
        age = int(player.get("age") or 25)
        is_fa = self._is_free_agent_player(player)
        # Use stored market value if present; otherwise compute fair value
        stored_mv = player.get("market_value")
        if stored_mv is not None and str(stored_mv).strip() != "":
            try:
                market = int(stored_mv)
                if market <= 0:
                    market = self.fair_market_value(player)
            except Exception:
                market = self.fair_market_value(player)
        else:
            market = self.fair_market_value(player)
        if is_fa:
            # ensure free agents remain low even if stale stored value exists
            market = min(market, self.fair_market_value({**player, "market_value": None, "team_api_id": 9000001}))

        asking = int(market * 1.15)
        fair = int(market * 1.02)
        min_accept = int(market * 0.90)

        # wage demand weekly - use stored fair wage as baseline
        stored_wage = player.get("wage")
        if stored_wage is not None and str(stored_wage).strip() != "":
            try:
                wage = int(stored_wage)
                if wage <= 0:
                    wage = self.fair_wage_weekly(player)
            except Exception:
                wage = self.fair_wage_weekly(player)
        else:
            wage = self.fair_wage_weekly(player)
        if is_fa:
            wage = min(wage, self.fair_wage_weekly({**player, "team_api_id": 9000001}))

        signing_bonus = int(max(25_000, wage * 8))
        contract_years = 4 if age < 29 else 3
        if age >= 32:
            contract_years = 2
        if is_fa:
            # Free agents often accept shorter/flexible deals
            contract_years = 2 if age >= 30 else 3

        # desired role
        if ovr >= 86:
            desired_role = "Crucial"
        elif ovr >= 82:
            desired_role = "Important"
        elif ovr >= 77:
            desired_role = "Rotation"
        elif age <= 22 and pot >= ovr + 4:
            desired_role = "Prospect"
        else:
            desired_role = "Sporadic"

        return PlayerValuation(market, min_accept, fair, asking, wage, signing_bonus, contract_years, desired_role)

    async def get_season_team(self, season_id: str, team_api_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM season_teams WHERE season_id=? AND team_api_id=?",
            (str(season_id), int(team_api_id)),
        )
        return dict(row) if row else None

    async def is_bot_team(self, season_id: str, team_api_id: int) -> bool:
        t = await self.get_season_team(season_id, team_api_id)
        if not t:
            # if not in season teams, treat as bot/unclaimed
            return True
        return t.get("manager_user_id") is None

    async def get_bot_transfer_count(self, season_id: str, buying_team_api_id: int) -> int:
        row = await self.db.fetchone(
            "SELECT used_count FROM bot_transfer_usage WHERE season_id=? AND team_api_id=?",
            (str(season_id), int(buying_team_api_id)),
        )
        if not row:
            return 0
        return int(dict(row).get("used_count") or 0)

    async def increment_bot_transfer_count(self, season_id: str, buying_team_api_id: int) -> int:
        now = utc_now_iso()
        await self.db.execute(
            """INSERT INTO bot_transfer_usage(season_id,team_api_id,used_count,updated_at)
               VALUES(?,?,1,?)
               ON CONFLICT(season_id,team_api_id) DO UPDATE SET used_count=used_count+1, updated_at=excluded.updated_at""",
            (str(season_id), int(buying_team_api_id), now),
        )
        return await self.get_bot_transfer_count(season_id, buying_team_api_id)

    def evaluate_bot_offer(self, val: PlayerValuation, offer_fee: int, player: dict[str, Any]) -> dict[str, Any]:
        ovr = float(player.get("overall") or 65)
        pot = float(player.get("potential") or ovr)
        ratio = offer_fee / max(1, val.min_accept_fee)
        # decision
        if offer_fee <= 0:
            return {"accept": False, "reason": "The offer is invalid. A transfer fee must be greater than €0."}
        if offer_fee < int(val.market_value * 0.5):
            return {
                "accept": False,
                "reason": (
                    f"This offer is far below the player's value. {player.get('name')} is rated {ovr:.0f} OVR with {pot:.0f} POT, "
                    f"and our valuation is around {fmt_money(val.market_value)}. An offer of {fmt_money(offer_fee)} is not serious enough to consider."
                ),
            }
        if ratio < 0.85:
            return {
                "accept": False,
                "reason": (
                    f"We've reviewed your bid of {fmt_money(offer_fee)} for {player.get('name')} ({ovr:.0f} OVR, {pot:.0f} POT). "
                    f"At this stage, it's under our minimum acceptable range. We value the player around {fmt_money(val.market_value)} "
                    f"and would need a much stronger fee to enter negotiations."
                ),
            }
        if ratio < 1.0:
            return {
                "accept": False,
                "reason": (
                    f"Your offer of {fmt_money(offer_fee)} for {player.get('name')} ({ovr:.0f} OVR, {pot:.0f} POT) is close, but still short. "
                    f"We'd consider a fee around {fmt_money(val.fair_fee)} to {fmt_money(val.asking_fee)} depending on structure."
                ),
            }
        # accept
        note = "fair and acceptable" if ratio < 1.15 else "very strong"
        return {
            "accept": True,
            "reason": (
                f"We consider your offer of {fmt_money(offer_fee)} for {player.get('name')} ({ovr:.0f} OVR, {pot:.0f} POT) {note}. "
                f"The fee meets our valuation."
            ),
        }

    def contract_demand(self, val: PlayerValuation, player: dict[str, Any]) -> dict[str, Any]:
        return {
            "wage": int(val.wage_demand),
            "role": val.desired_role,
            "signing_bonus": int(val.signing_bonus),
            "contract_years": int(val.contract_years),
        }

    def evaluate_contract_offer(self, demand: dict[str, Any], offered: dict[str, Any], player: dict[str, Any], round_no: int, pressure: int, *, is_free_agent: bool = False) -> dict[str, Any]:
        wage_d = int(demand.get("wage") or 0)
        role_d = str(demand.get("role") or "Rotation")
        bonus_d = int(demand.get("signing_bonus") or 0)
        years_d = int(demand.get("contract_years") or 3)

        wage_o = int(offered.get("wage") or 0)
        role_o = normalize_role(offered.get("role")) or role_d
        bonus_o = int(offered.get("signing_bonus") or 0)
        years_o = int(offered.get("contract_years") or years_d)

        role_rank = {r: i for i, r in enumerate(ROLE_TIERS)}  # Crucial best (0)
        role_gap = role_rank.get(role_o, 2) - role_rank.get(role_d, 2)  # positive = worse role for player
        wage_ratio = wage_o / max(1, wage_d)
        bonus_ratio = bonus_o / max(1, bonus_d)
        years_ok = abs(years_o - years_d) <= 1

        ovr = float(player.get("overall") or 65)
        pot = float(player.get("potential") or ovr)
        name = str(player.get("name") or "The player")
        max_pressure = 10
        bar = negotiation_pressure_bar(pressure, max_pressure)

        # Hard walk-away / negotiation ended at high pressure
        if pressure >= max_pressure:
            end_msg = (
                f"{bar}\n"
                f"**Negotiation ended.** {name}'s camp has walked away from the table. "
                + ("As a free agent, he will explore other options and is no longer considering this offer." if is_free_agent else "The player's representatives have ended discussions due to the lack of progress and respect for the player's value.")
                + f"\n\nPlayer profile considered: **{ovr:.0f} OVR / {pot:.0f} POT**."
            )
            return {"accepted": False, "walked_away": True, "message": end_msg, "counter": None, "pressure_bar": bar}

        score = 0.0
        score += (wage_ratio - 1.0) * 1.6
        score += (bonus_ratio - 1.0) * 0.35
        score += (-role_gap) * 0.30
        score += (0.05 if years_ok else -0.10)
        score -= pressure * 0.035
        score -= max(0, round_no - 1) * 0.025
        # High-profile players are stricter
        if ovr >= 85:
            score -= 0.05
        if ovr >= 88:
            score -= 0.04

        acceptable = score >= 0.0 and wage_ratio >= 0.97 and role_gap <= 0 and bonus_ratio >= 0.80
        # Very close deals can be accepted
        if not acceptable and score >= -0.02 and wage_ratio >= 0.99 and role_gap <= 0 and bonus_ratio >= 0.90 and pressure <= 5:
            acceptable = True

        if acceptable:
            return {
                "accepted": True,
                "walked_away": False,
                "message": (
                    f"{bar}\n"
                    f"**Agreement reached.** {name} ({ovr:.0f} OVR, {pot:.0f} POT) accepts the contract: "
                    f"**{fmt_money(wage_o)}/wk**, {role_o} role, **{fmt_money(bonus_o)}** signing bonus, **{years_o} years**.\n"
                    f"The terms reflect the player's quality and current standing. Finalizing paperwork."
                ),
                "counter": None,
                "pressure_bar": bar,
            }

        # Counter - firm, realistic, small concessions
        # Don't chase bad offers; hold close to demand
        counter_wage = int(max(wage_o, wage_d * (0.985 - min(0.03, round_no * 0.005))))
        if wage_ratio >= 0.93:
            counter_wage = int(max(wage_o, wage_d * 0.995))
        if wage_ratio >= 0.97:
            counter_wage = int(max(wage_o, wage_d))
        counter_bonus = int(max(bonus_o, bonus_d * 0.97))
        if bonus_ratio >= 0.95:
            counter_bonus = int(max(bonus_o, bonus_d))
        counter_role = role_d if role_gap > 0 else role_o
        # Keep preferred years; only flex one year at high rounds
        counter_years = years_d
        if years_o == years_d:
            counter_years = years_d
        elif round_no >= 3 and abs(years_o - years_d) == 1:
            counter_years = years_o
        counter = {
            "wage": counter_wage,
            "role": counter_role,
            "signing_bonus": counter_bonus,
            "contract_years": counter_years,
        }

        reasons = []
        if wage_ratio < 0.97:
            reasons.append(f"weekly wage of {fmt_money(wage_o)}/wk is below the market for a {ovr:.0f} OVR / {pot:.0f} POT player (demand {fmt_money(wage_d)}/wk)")
        if role_gap > 0:
            reasons.append(f"the proposed role ({role_o}) does not match the player's standing; {name} expects {role_d}")
        if bonus_ratio < 0.85:
            reasons.append(f"signing bonus {fmt_money(bonus_o)} is well short of the expected {fmt_money(bonus_d)}")
        if not years_ok:
            reasons.append(f"contract length of {years_o} year(s) is not acceptable; preference is {years_d} years")
        if not reasons:
            reasons.append("the overall package still falls short of the player's value")

        # Pressure / walk-away warning
        warning = ""
        if pressure >= 8:
            warning = " **This is close to breaking point - one more poor offer and talks will end.**"
        elif pressure >= 6:
            warning = " Pressure is high; the camp is losing patience."
        elif pressure >= 4:
            warning = " Negotiation pressure is building - be realistic."

        stance = "firm" if ovr >= 82 else "clear"
        agent_line = (
            f"{bar}\n"
            f"**{name}'s camp ({stance} response):** We have reviewed your offer carefully. "
            f"{' '.join(reasons).strip().capitalize() if reasons else 'The package is not there yet.'}{warning}\n\n"
            f"**Counter-demand:** **{fmt_money(counter_wage)}/wk**, {counter_role} role, **{fmt_money(counter_bonus)}** signing bonus, **{counter_years} years**.\n"
            f"Player profile: **{ovr:.0f} OVR / {pot:.0f} POT**. This negotiation is being handled professionally and strictly - respect the player's value."
        )

        return {
            "accepted": False,
            "walked_away": False,
            "message": agent_line,
            "counter": counter,
            "pressure_bar": bar,
        }

    def parse_contract_reply(self, text: str) -> dict[str, Any]:
        """Parse natural-language contract terms (wage, role, bonus, years) in any order."""
        t = (text or "").strip()
        if not t:
            return {}
        try:
            from services.nlu_service import extract_contract_terms
            out = dict(extract_contract_terms(t) or {})
        except Exception:
            out = {}

        # Compatibility / fallback pass
        if "contract_years" not in out:
            ym = re.search(r"(\d{1,2})\s*(?:year|yr|years|yrs|season|seasons)\b", t, re.I)
            if not ym:
                ym = re.search(r"(?:contract|deal|length|duration)\s*[:=]?\s*(\d{1,2})\b", t, re.I)
            if ym:
                try:
                    out["contract_years"] = max(1, min(7, int(ym.group(1))))
                except Exception:
                    pass

        if "role" not in out:
            role = normalize_role(t)
            if role:
                out["role"] = role

        if "signing_bonus" not in out:
            if re.search(r"\b(no|without|zero|none)\b\s*(?:signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|bonus|sign[- ]?on|upfront)\b", t, re.I) or \
               re.search(r"\b(?:signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|bonus|sign[- ]?on|upfront)\b\s*[:=]?\s*(?:no|none|zero|0)\b", t, re.I):
                out["signing_bonus"] = 0
            else:
                bonus_patterns = [
                    r"(?:signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|bonus|sign[- ]?on|upfront)\s*[:=]?\s*([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|k|m|bn|billion)?)",
                    r"([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|k|m|bn|billion)?)\s*(?:signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|bonus|sign[- ]?on|upfront)\b",
                ]
                for pat in bonus_patterns:
                    m = re.search(pat, t, re.I)
                    if m:
                        val = money_to_int(m.group(1))
                        if val is not None:
                            out["signing_bonus"] = val
                            break

        if "wage" not in out:
            wage_patterns = [
                r"wages?\s*[:=]?\s*([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|k|m)?)",
                r"salary\s*[:=]?\s*([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|k|m)?)",
                r"(?:pay|offer|give)\s+(?:him\s+)?([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|k|m)?)\s*/?\s*(?:wk|week|weekly|pw|p/w|p\.w|per\s*week|per\s*wk|w)\b",
                r"([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|k|m)?)\s*/?\s*(?:wk|week|weekly|pw|p/w|p\.w|per\s*week|per\s*wk)\b",
                r"([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|k|m)?)\s*(?:a|per)\s*(?:week|wk)\b",
            ]
            for pat in wage_patterns:
                m = re.search(pat, t, re.I)
                if m:
                    val = money_to_int(m.group(1))
                    if val is not None:
                        out["wage"] = val
                        break

        # If no wage found, but there is a money token that is not the bonus and looks like wage
        if "wage" not in out:
            for m in re.finditer(r"([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|k|m|bn|billion)?)", t, re.I):
                val = money_to_int(m.group(1))
                if val is None:
                    continue
                if "signing_bonus" in out and val == int(out.get("signing_bonus") or 0):
                    continue
                if 1_000 <= val <= 5_000_000:
                    out["wage"] = val
                    break
            if "wage" not in out:
                m = re.fullmatch(r"[€£$]?\s*(\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|k|m)?)", t.strip(), re.I)
                if m:
                    val = money_to_int(m.group(1))
                    if val is not None:
                        out["wage"] = val

        # If two money values and bonus missing, infer bonus as the other one
        if "wage" in out and "signing_bonus" not in out:
            nums = []
            for m in re.finditer(r"([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|k|m|bn|billion)?)", t, re.I):
                v = money_to_int(m.group(1))
                if v is not None and v > 0:
                    nums.append(int(v))
            others = [v for v in nums if v != int(out["wage"])]
            if others:
                # pick a plausible bonus
                cand = max(others)
                if cand >= 1_000 and cand != int(out["wage"]):
                    out["signing_bonus"] = cand

        return out
