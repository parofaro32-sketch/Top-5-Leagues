from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from db.database import Database, utc_now_iso

logger = logging.getLogger(__name__)


def money_to_int(text: str | int | float | None) -> int | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    s = str(text).strip().lower().replace(",", "").replace("€", "").replace("$", "").replace("£", "")
    s = s.replace("per week", "").replace("/wk", "").replace("pw", "").replace("weekly", "").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmb])?", s)
    if not m:
        # try to find first number
        m2 = re.search(r"(\d+(?:\.\d+)?)\s*([kmb])?", s)
        if not m2:
            return None
        m = m2
    val = float(m.group(1))
    suf = (m.group(2) or "").lower()
    mult = 1
    if suf == "k":
        mult = 1_000
    elif suf == "m":
        mult = 1_000_000
    elif suf == "b":
        mult = 1_000_000_000
    return int(val * mult)


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


ROLE_TIERS = [
    "Crucial", "Important", "Rotation", "Sporadic", "Prospect"
]
ROLE_ALIASES = {
    "crucial": "Crucial", "key": "Crucial", "star": "Crucial",
    "important": "Important", "regular": "Important", "starter": "Important",
    "rotation": "Rotation", "squad": "Rotation",
    "sporadic": "Sporadic", "backup": "Sporadic", "fringe": "Sporadic",
    "prospect": "Prospect", "youngster": "Prospect", "future": "Prospect",
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
        q = (player_query or "").strip()
        if not q:
            return None
        rows = await self.db.fetchall(
            "SELECT * FROM players WHERE team_api_id=? AND season=?",
            (int(team_api_id), int(season_year)),
        )
        players = [dict(r) for r in rows]
        if not players:
            return None
        if q.isdigit():
            for p in players:
                if int(p.get("api_id") or 0) == int(q):
                    return p
                if int(p.get("number") or -1) == int(q):
                    return p
        ql = q.lower()
        # exact
        for p in players:
            if str(p.get("name") or "").lower().strip() == ql:
                return p
        # contains
        cands = [p for p in players if ql in str(p.get("name") or "").lower() or str(p.get("name") or "").lower() in ql]
        if len(cands) == 1:
            return cands[0]
        if cands:
            cands.sort(key=lambda p: len(str(p.get("name") or "")))
            return cands[0]
        # token overlap
        qt = set(ql.split())
        best = None
        best_sc = 0.0
        for p in players:
            pt = set(str(p.get("name") or "").lower().split())
            if not pt:
                continue
            sc = len(qt & pt) / max(1, len(qt | pt))
            if sc > best_sc:
                best_sc = sc
                best = p
        if best and best_sc >= 0.34:
            return best
        return None

    def value_player(self, player: dict[str, Any]) -> PlayerValuation:
        ovr = float(player.get("overall") or player.get("rating") or 65)
        pot = float(player.get("potential") or ovr)
        age = int(player.get("age") or 25)
        pos = str(player.get("position") or "MID").upper()
        # market value model (simple, fair)
        base = max(1_000_000, (ovr - 55) ** 2 * 180_000)
        pot_bonus = max(0.0, (pot - ovr)) * 900_000
        age_mult = 1.0
        if age <= 21:
            age_mult = 1.25
        elif age <= 24:
            age_mult = 1.15
        elif age <= 28:
            age_mult = 1.0
        elif age <= 31:
            age_mult = 0.85
        else:
            age_mult = 0.7
        pos_mult = 1.05 if pos in {"FWD", "ST", "LW", "RW"} else (1.03 if pos == "GK" else 1.0)
        market = int((base + pot_bonus) * age_mult * pos_mult)
        market = max(500_000, min(market, 180_000_000))
        asking = int(market * 1.18)
        fair = int(market * 1.02)
        min_accept = int(market * 0.9)
        # wage demand weekly
        wage = int(max(15_000, (ovr - 60) * 8_000 + max(0, pot - ovr) * 3_000))
        if age < 23 and pot >= ovr + 3:
            wage = int(wage * 0.9)
        if ovr >= 85:
            wage = int(wage * 1.3)
        signing_bonus = int(max(25_000, wage * 8))
        contract_years = 4 if age < 29 else 3
        if age >= 32:
            contract_years = 2
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

    def evaluate_contract_offer(self, demand: dict[str, Any], offered: dict[str, Any], player: dict[str, Any], round_no: int, pressure: int) -> dict[str, Any]:
        wage_d = int(demand.get("wage") or 0)
        role_d = str(demand.get("role") or "Rotation")
        bonus_d = int(demand.get("signing_bonus") or 0)
        years_d = int(demand.get("contract_years") or 3)

        wage_o = int(offered.get("wage") or 0)
        role_o = normalize_role(offered.get("role")) or role_d
        bonus_o = int(offered.get("signing_bonus") or 0)
        years_o = int(offered.get("contract_years") or years_d)

        role_rank = {r: i for i, r in enumerate(ROLE_TIERS)}  # Crucial best
        role_gap = role_rank.get(role_o, 2) - role_rank.get(role_d, 2)  # positive = worse role for player
        wage_ratio = wage_o / max(1, wage_d)
        bonus_ratio = bonus_o / max(1, bonus_d)
        years_ok = abs(years_o - years_d) <= 1

        score = 0.0
        score += (wage_ratio - 1.0) * 1.4
        score += (bonus_ratio - 1.0) * 0.4
        score += (-role_gap) * 0.25
        score += (0.05 if years_ok else -0.08)
        score -= pressure * 0.03
        score -= max(0, round_no - 2) * 0.03

        ovr = float(player.get("overall") or 65)
        pot = float(player.get("potential") or ovr)

        if score >= 0.0 and wage_ratio >= 0.95 and role_gap <= 0:
            return {
                "accepted": True,
                "message": (
                    f"Agreement reached. {player.get('name')} ({ovr:.0f} OVR, {pot:.0f} POT) accepts the terms: "
                    f"{fmt_money(wage_o)}/wk, {role_o} role, {fmt_money(bonus_o)} signing bonus, {years_o}-year contract."
                ),
                "counter": None,
            }

        # counter
        # make a concession toward player but not too much
        counter_wage = int(max(wage_o, wage_d * (0.96 - min(0.08, round_no * 0.01))))
        if wage_ratio >= 0.9:
            counter_wage = int(max(wage_o, wage_d * 0.97))
        counter_bonus = int(max(bonus_o, bonus_d * 0.95))
        counter_role = role_d if role_gap > 0 else role_o
        counter_years = years_d
        counter = {
            "wage": counter_wage,
            "role": counter_role,
            "signing_bonus": counter_bonus,
            "contract_years": counter_years,
        }

        reasons = []
        if wage_ratio < 0.95:
            reasons.append(f"wage offer {fmt_money(wage_o)}/wk is below the expected {fmt_money(wage_d)}/wk range")
        if role_gap > 0:
            reasons.append(f"requested role {role_o} is below the player's expectation of {role_d}")
        if bonus_ratio < 0.85:
            reasons.append(f"signing bonus {fmt_money(bonus_o)} is below expectation ({fmt_money(bonus_d)})")
        if not years_ok:
            reasons.append(f"contract length {years_o} years is not ideal (preferred {years_d})")
        if not reasons:
            reasons.append("overall package is slightly below the player's expectations")

        pressure_note = " Negotiation pressure is rising; the player wants clarity soon." if pressure >= 2 else ""
        return {
            "accepted": False,
            "message": (
                f"Counter-offer from {player.get('name')}'s camp: {fmt_money(counter_wage)}/wk, {counter_role} role, "
                f"{fmt_money(counter_bonus)} signing bonus, {counter_years} years. "
                f"Reason: {'; '.join(reasons)}.{pressure_note}"
            ),
            "counter": counter,
        }

    def parse_contract_reply(self, text: str) -> dict[str, Any]:
        t = (text or "").strip()
        out: dict[str, Any] = {}

        # years (look before money to avoid confusion)
        ym = re.search(r"(\d+)\s*(?:year|yr|years|yrs)\b", t, re.I)
        if ym:
            out["contract_years"] = int(ym.group(1))

        # role
        role = normalize_role(t)
        if role:
            out["role"] = role

        # signing bonus (before or after keyword)
        bonus_patterns = [
            r"(?:signing\s*bonus|sign-on\s*bonus|sign\s*on\s*bonus|bonus)\s*[:=]?\s*([€£$]?\s*\d[\d,.]*\s*[kmb]?)",
            r"([€£$]?\s*\d[\d,.]*\s*[kmb]?)\s*(?:signing\s*bonus|sign-on\s*bonus|sign\s*on\s*bonus|bonus)\b",
        ]
        for pat in bonus_patterns:
            m = re.search(pat, t, re.I)
            if m:
                val = money_to_int(m.group(1))
                if val is not None:
                    out["signing_bonus"] = val
                    break

        # wage
        wage_patterns = [
            r"wage\s*[:=]?\s*([€£$]?\s*\d[\d,.]*\s*[kmb]?)",
            r"salary\s*[:=]?\s*([€£$]?\s*\d[\d,.]*\s*[kmb]?)",
            r"(?:pay|offer|give)\s+(?:him\s+)?([€£$]?\s*\d[\d,.]*\s*[kmb]?)\s*/?\s*(?:wk|week|weekly|pw|per week)\b",
            r"([€£$]?\s*\d[\d,.]*\s*[kmb]?)\s*/?\s*(?:wk|week|weekly|pw|per week)\b",
            r"([€£$]?\s*\d[\d,.]*\s*[kmb]?)\s*(?:a|per)\s*week\b",
        ]
        for pat in wage_patterns:
            m = re.search(pat, t, re.I)
            if m:
                val = money_to_int(m.group(1))
                if val is not None:
                    out["wage"] = val
                    break

        # If no wage found, but there is a money token that is not the bonus and looks like wage (k/m small)
        if "wage" not in out:
            # try to pick a plausible wage number (typically in thousands per week)
            for m in re.finditer(r"([€£$]?\s*\d[\d,.]*\s*[kmb]?)", t, re.I):
                val = money_to_int(m.group(1))
                if val is None:
                    continue
                if val == out.get("signing_bonus"):
                    continue
                # heuristic: wages in this system are usually 5k-2M weekly
                if 5_000 <= val <= 2_000_000:
                    out["wage"] = val
                    break
            # if just a number and wage missing, treat as wage
            if "wage" not in out:
                m = re.fullmatch(r"[€£$]?\s*(\d[\d,.]*\s*[kmb]?)", t.strip(), re.I)
                if m:
                    val = money_to_int(m.group(1))
                    if val is not None:
                        out["wage"] = val
        return out
