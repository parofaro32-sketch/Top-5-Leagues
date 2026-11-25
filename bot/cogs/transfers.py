from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import error_embed, info_embed, success_embed
from db.database import utc_now_iso
from services.transfer_market_service import TransferMarketService, fmt_money, money_to_int, normalize_role

logger = logging.getLogger(__name__)

LEAGUE_CHOICES = [
    app_commands.Choice(name="Premier League", value="premier_league"),
    app_commands.Choice(name="La Liga", value="la_liga"),
    app_commands.Choice(name="Bundesliga", value="bundesliga"),
    app_commands.Choice(name="Serie A", value="serie_a"),
    app_commands.Choice(name="Ligue 1", value="ligue_1"),
]


def _is_admin(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions if interaction.guild else None
    return bool(perms and (perms.administrator or perms.manage_guild))


class ContractOfferModal(discord.ui.Modal, title="Contract Offer (Natural Language)"):
    offer = discord.ui.TextInput(
        label="Your offer (natural language)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=300,
        placeholder="e.g. I offer 180k per week, important role, 2M signing bonus, 4 year contract",
    )

    def __init__(self, cog: "TransfersCog", offer_id: int):
        super().__init__()
        self.cog = cog
        self.offer_id = offer_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_contract_modal_submit(interaction, self)


class ContractNegView(discord.ui.View):
    def __init__(self, cog: "TransfersCog", offer_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.offer_id = int(offer_id)
        self.submit_offer.custom_id = f"transfer:contract:submit:{self.offer_id}"
        self.pull_out.custom_id = f"transfer:contract:pullout:{self.offer_id}"

    @discord.ui.button(label="Submit Contract Offer", style=discord.ButtonStyle.primary, custom_id="transfer:contract:submit:0")
    async def submit_offer(self, interaction: discord.Interaction, button: discord.ui.Button):
        return  # handled by on_interaction

    @discord.ui.button(label="Pull Out", style=discord.ButtonStyle.danger, custom_id="transfer:contract:pullout:0")
    async def pull_out(self, interaction: discord.Interaction, button: discord.ui.Button):
        return  # handled by on_interaction


class AskAboutView(discord.ui.View):
    def __init__(self, cog: "TransfersCog", offer_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.offer_id = int(offer_id)
        self.for_sale.custom_id = f"transfer:ask:forsale:{self.offer_id}"
        self.not_for_sale.custom_id = f"transfer:ask:notforsale:{self.offer_id}"

    @discord.ui.button(label="For Sale", style=discord.ButtonStyle.success, custom_id="transfer:ask:forsale:0")
    async def for_sale(self, interaction: discord.Interaction, button: discord.ui.Button):
        return  # handled by on_interaction

    @discord.ui.button(label="Not For Sale", style=discord.ButtonStyle.danger, custom_id="transfer:ask:notforsale:0")
    async def not_for_sale(self, interaction: discord.Interaction, button: discord.ui.Button):
        return  # handled by on_interaction


class TransfersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.market = TransferMarketService(bot.db)

    async def db_execute(self, sql: str, params: tuple = ()):
        await self.bot.db.execute(sql, params)

    async def get_offer(self, offer_id: int) -> dict[str, Any] | None:
        row = await self.bot.db.fetchone("SELECT * FROM transfer_offers WHERE id=?", (int(offer_id),))
        return dict(row) if row else None

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        try:
            if interaction.type != discord.InteractionType.component:
                return
            data = interaction.data or {}
            cid = str(data.get("custom_id") or "")
            if interaction.response.is_done():
                return
            if cid.startswith("transfer:contract:submit:"):
                offer_id = int(cid.rsplit(":", 1)[-1])
                await self.handle_contract_submit_button(interaction, offer_id)
                return
            if cid.startswith("transfer:contract:pullout:"):
                offer_id = int(cid.rsplit(":", 1)[-1])
                await self.handle_contract_pullout(interaction, offer_id)
                return
            if cid.startswith("transfer:ask:forsale:"):
                offer_id = int(cid.rsplit(":", 1)[-1])
                view = AskAboutView(self, offer_id)
                await self.handle_ask_response(interaction, offer_id, for_sale=True, view=view)
                return
            if cid.startswith("transfer:ask:notforsale:"):
                offer_id = int(cid.rsplit(":", 1)[-1])
                view = AskAboutView(self, offer_id)
                await self.handle_ask_response(interaction, offer_id, for_sale=False, view=view)
                return
        except Exception:
            logger.exception("transfer interaction handler failed")

    async def handle_contract_submit_button(self, interaction: discord.Interaction, offer_id: int):
        offer = await self.get_offer(offer_id)
        if not offer:
            if not interaction.response.is_done():
                await interaction.response.send_message("This negotiation is no longer available.", ephemeral=True)
            else:
                await interaction.followup.send("This negotiation is no longer available.", ephemeral=True)
            return
        if interaction.user.id != int(offer.get("buying_user_id") or 0):
            msg = "Only the buying manager can submit a contract offer."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return
        if offer.get("status") not in ("negotiating", "accepted", "countered"):
            msg = "This negotiation is no longer active."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return
        await interaction.response.send_modal(ContractOfferModal(self, int(offer_id)))

    async def handle_contract_pullout(self, interaction: discord.Interaction, offer_id: int):
        offer = await self.get_offer(offer_id)
        if not offer:
            if not interaction.response.is_done():
                await interaction.response.send_message("This negotiation is no longer available.", ephemeral=True)
            else:
                await interaction.followup.send("This negotiation is no longer available.", ephemeral=True)
            return
        if interaction.user.id != int(offer.get("buying_user_id") or 0):
            msg = "Only the buying manager can pull out."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return
        await self.db_execute(
            "UPDATE transfer_offers SET status='cancelled', updated_at=? WHERE id=?",
            (utc_now_iso(), int(offer_id)),
        )
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(content="Negotiation cancelled by buying manager.", view=None)
            else:
                await interaction.edit_original_response(content="Negotiation cancelled by buying manager.", view=None)
        except Exception:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Negotiation cancelled.", ephemeral=True)
            except Exception:
                pass

    async def _resolve_active_season(self, interaction: discord.Interaction, league_key: str | None):
        season = await self.bot.season_service.get_active_season(interaction.guild_id, league_key=league_key)
        return season

    async def _resolve_buying_team_for_user(self, interaction: discord.Interaction, league_key: str | None):
        team = await self.bot.season_service.get_user_team_in_active_season(interaction.guild_id, interaction.user.id, league_key=league_key)
        return team

    async def _resolve_buying_team_and_season(self, interaction: discord.Interaction, league_key: str | None):
        """Resolve the manager's claimed team and the season context. Falls back across leagues/seasons."""
        season = await self._resolve_active_season(interaction, league_key)
        buying_team = await self._resolve_buying_team_for_user(interaction, league_key=(season.get("league_key") if season else league_key))
        if not buying_team and league_key:
            buying_team = await self._resolve_buying_team_for_user(interaction, None)
        if buying_team:
            _sid = buying_team.get("season_id")
            team_season = buying_team.get("_season") or (await self.bot.season_service.get_season(str(_sid)) if _sid else None)
            if team_season:
                season = team_season
        return buying_team, season

    async def _find_season_team_by_name(self, season_id: str, team_name: str) -> dict[str, Any] | None:
        q = (team_name or "").strip().lower()
        if not q:
            return None
        rows = await self.bot.db.fetchall("SELECT * FROM season_teams WHERE season_id=?", (str(season_id),))
        cands = []
        for r in rows:
            d = dict(r)
            name = str(d.get("team_name") or "").lower()
            short = str(d.get("short_name") or "").lower()
            score = 0
            if q == name or q == short:
                score = 3
            elif name.startswith(q) or q in name:
                score = 2
            elif q in short:
                score = 1
            if score:
                d["_score"] = score
                cands.append(d)
        cands.sort(key=lambda x: (-x["_score"], x.get("team_name") or ""))
        return cands[0] if cands else None

    async def _find_season_team_by_name_any(self, guild_id: int | None, team_name: str, *, prefer_season_id: str | None = None, league_key: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Find a season_team by name, searching preferred season first then all active/any seasons. Returns (team, season)."""
        if prefer_season_id:
            t = await self._find_season_team_by_name(str(prefer_season_id), team_name)
            if t:
                s = await self.bot.season_service.get_season(str(prefer_season_id))
                return t, s
        seasons = await self.bot.season_service.list_seasons(guild_id=guild_id, status="active")
        if league_key:
            seasons = [s for s in seasons if str(s.get("league_key")) == str(league_key)]
        if not seasons and guild_id is not None:
            seasons = await self.bot.season_service.list_seasons(guild_id=None, status="active")
            if league_key:
                seasons = [s for s in seasons if str(s.get("league_key")) == str(league_key)]
        if not seasons:
            seasons = await self.bot.season_service.list_seasons(guild_id=guild_id, status=None)
            if league_key:
                seasons = [s for s in seasons if str(s.get("league_key")) == str(league_key)]
        for s in seasons:
            sid = str(s.get("id"))
            if prefer_season_id and sid == str(prefer_season_id):
                continue
            t = await self._find_season_team_by_name(sid, team_name)
            if t:
                return t, s
        return None, None

    def _parse_int_money(self, v: Any) -> int | None:
        if v is None:
            return None
        if isinstance(v, int):
            return v
        return money_to_int(str(v))

    @app_commands.command(name="botoffer", description="Make a transfer offer to a bot/unclaimed team (FC27-style)")
    @app_commands.describe(team_name="Bot team name (must be unclaimed)", player_name="Player name in that team", fee="Transfer fee (e.g. 25000000 or 25M)")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def botoffer(self, interaction: discord.Interaction, team_name: str, player_name: str, fee: str, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        buying_team, season = await self._resolve_buying_team_and_season(interaction, league_key)
        if not buying_team:
            await interaction.followup.send(embed=error_embed("No Claimed Team", "You must claim a team first with `/claim` to make bot offers."), ephemeral=True)
            return
        if not season:
            _sid = buying_team.get("season_id")
            season = buying_team.get("_season") or (await self.bot.season_service.get_season(str(_sid)) if _sid else None)
        if not season:
            await interaction.followup.send(embed=error_embed("No Active Season", "No active season found. Create one with `/season create`."), ephemeral=True)
            return
        season_id = str(season.get("id"))
        season_year = int(season.get("season_year") or 2026)
        buying_team_id = int(buying_team.get("team_api_id"))
        buying_team_name = str(buying_team.get("team_name"))

        selling_team = await self._find_season_team_by_name(season_id, team_name)
        selling_season = season
        if not selling_team:
            selling_team, selling_season = await self._find_season_team_by_name_any(interaction.guild_id, team_name, prefer_season_id=season_id, league_key=league_key)
        if not selling_team:
            await interaction.followup.send(embed=error_embed("Team Not Found", f"Could not find team `{team_name}`. Check the team name and try again."), ephemeral=True)
            return
        if selling_season:
            selling_sid = str(selling_season.get("id"))
            buying_sid = str((buying_team or {}).get("season_id") or season_id)
            # Prefer keeping buying team's season context when they differ (budget checks rely on it)
            if not buying_team or selling_sid == buying_sid:
                season = selling_season
                season_id = selling_sid
                season_year = int(season.get("season_year") or 2026)
            else:
                # keep buying team season; selling team came from another season - still usable but rare
                season_id = buying_sid
                season = (buying_team or {}).get("_season") or await self.bot.season_service.get_season(buying_sid) or season
        selling_team_id = int(selling_team.get("team_api_id"))
        selling_team_name = str(selling_team.get("team_name"))
        if selling_team.get("manager_user_id") is not None:
            await interaction.followup.send(embed=error_embed("Not A Bot Team", f"**{selling_team_name}** is claimed by a manager. `/botoffer` is only for unclaimed bot teams. Use `/askaboutplayer` or `/contractnego`."), ephemeral=True)
            return
        if selling_team_id == buying_team_id:
            await interaction.followup.send(embed=error_embed("Invalid Target", "You cannot make an offer to your own team."), ephemeral=True)
            return

        # bot transfer limit
        used = await self.market.get_bot_transfer_count(season_id, buying_team_id)
        if used >= 2:
            await interaction.followup.send(embed=error_embed("Bot Transfer Limit Reached", f"**{buying_team_name}** has used {used}/2 bot transfers this season. You hit max bot transfers available."), ephemeral=True)
            return

        player = await self.market.find_player_in_team(selling_team_id, player_name, season_year)
        if not player:
            await interaction.followup.send(embed=error_embed("Player Not Found", f"Could not find `{player_name}` in **{selling_team_name}**."), ephemeral=True)
            return

        offer_fee = self._parse_int_money(fee)
        if offer_fee is None or offer_fee <= 0:
            await interaction.followup.send(embed=error_embed("Invalid Fee", "Enter a valid fee like `25000000`, `25M`, or `€25M`."), ephemeral=True)
            return

        val = self.market.value_player(player)
        decision = self.market.evaluate_bot_offer(val, offer_fee, player)

        now = utc_now_iso()
        status = "negotiating" if decision["accept"] else "rejected"
        cur = await self.bot.db.execute(
            """INSERT INTO transfer_offers(season_id,guild_id,channel_id,kind,status,buying_user_id,buying_team_api_id,buying_team_name,selling_team_api_id,selling_team_name,selling_manager_user_id,player_api_id,player_name,fee,stage,round,pressure,meta_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?, 'club_offer',0,0,?, ?,?)""",
            (season_id, interaction.guild_id, interaction.channel_id, "bot_offer", status, interaction.user.id, buying_team_id, buying_team_name, selling_team_id, selling_team_name, int(player.get("api_id")), str(player.get("name")), int(offer_fee), json.dumps({"valuation": val.__dict__, "decision": decision}), now, now),
        )
        offer_id = cur.lastrowid

        if not decision["accept"]:
            await interaction.followup.send(embed=error_embed(
                "Offer Rejected",
                f"**{selling_team_name}** (bot manager) response for **{player.get('name')}**:\n\n{decision['reason']}\n\n"
                f"Your offer: {fmt_money(offer_fee)} • Club valuation: {fmt_money(val.market_value)} • Suggested fair fee: {fmt_money(val.fair_fee)}"
            ), ephemeral=True)
            return

        # accepted -> start contract negotiation
        demand = self.market.contract_demand(val, player)
        await self.bot.db.execute(
            "UPDATE transfer_offers SET status='negotiating', stage='contract', meta_json=?, updated_at=? WHERE id=?",
            (json.dumps({"valuation": val.__dict__, "decision": decision, "demand": demand, "contract_round": 0, "pressure": 0}), utc_now_iso(), int(offer_id)),
        )
        view = ContractNegView(self, int(offer_id))
        embed = success_embed(
            "Offer Accepted — Contract Negotiation",
            (
                f"**{selling_team_name}** (bot manager): {decision['reason']}\n\n"
                f"Fee agreed: **{fmt_money(offer_fee)}** for **{player.get('name')}** ({int(player.get('overall') or 0)} OVR / {int(player.get('potential') or 0)} POT).\n"
                f"Now starting **FC27-style contract negotiation** with the buying club.\n\n"
                f"**Player demands (opening):**\n"
                f"• Weekly wage: **{fmt_money(demand['wage'])}/wk**\n"
                f"• Role: **{demand['role']}**\n"
                f"• Signing bonus: **{fmt_money(demand['signing_bonus'])}**\n"
                f"• Contract length: **{demand['contract_years']} years**\n\n"
                f"Use **Submit Contract Offer** to respond. The negotiation includes pressure and counter-offers."
            ),
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def handle_contract_modal_submit(self, interaction: discord.Interaction, modal: ContractOfferModal):
        await interaction.response.defer(ephemeral=True)
        offer = await self.get_offer(modal.offer_id)
        if not offer:
            await interaction.followup.send(embed=error_embed("Not Found", "Negotiation not found."), ephemeral=True)
            return
        if interaction.user.id != int(offer.get("buying_user_id") or 0):
            await interaction.followup.send(embed=error_embed("Not Allowed", "Only the buying manager can submit this offer."), ephemeral=True)
            return
        if offer.get("status") not in ("negotiating", "accepted", "countered"):
            await interaction.followup.send(embed=error_embed("Inactive", "This negotiation is no longer active."), ephemeral=True)
            return

        meta = {}
        try:
            meta = json.loads(offer.get("meta_json") or "{}")
        except Exception:
            meta = {}
        demand = meta.get("demand") or {}
        valuation = meta.get("valuation") or {}

        raw_msg = str(getattr(modal, "offer", None).value if getattr(modal, "offer", None) else "").strip()
        parsed = self.market.parse_contract_reply(raw_msg) if raw_msg else {}

        wage = parsed.get("wage")
        bonus = parsed.get("signing_bonus")
        role = parsed.get("role")
        years = parsed.get("contract_years")

        missing = []
        if wage is None or int(wage) <= 0:
            missing.append("weekly wage (e.g. 150k/wk)")
        if role is None:
            missing.append("role (Crucial/Important/Rotation/Sporadic/Prospect)")
        if bonus is None:
            missing.append("signing bonus (e.g. 500k bonus)")
        if years is None:
            missing.append("contract years (e.g. 4 years)")
        if missing:
            await interaction.followup.send(
                embed=error_embed(
                    "Could Not Understand Offer",
                    "I couldn't fully understand your contract offer. Please include: " + ", ".join(missing) + ".\n\n"
                    f"Your message: `{raw_msg[:300]}`\n\n"
                    "Example: `I offer 180k per week, important role, 2M signing bonus, 4 year contract`"
                ),
                ephemeral=True,
            )
            return

        wage = int(wage)
        bonus = max(0, int(bonus))
        role = normalize_role(str(role)) or str(demand.get("role") or "Rotation")
        try:
            years = int(years)
        except Exception:
            years = int(demand.get("contract_years") or 3)
        years = max(1, min(5, years))

        round_no = int(meta.get("contract_round") or 0) + 1
        pressure = int(meta.get("pressure") or 0)
        # pressure increases on lowball or long negotiation
        if wage < int(demand.get("wage") or 0) * 0.85:
            pressure += 1
        if round_no >= 3:
            pressure += 1

        season_year = int((await self.bot.season_service.get_season(str(offer.get("season_id")) or "") or {}).get("season_year") or 2026)
        player = await self.market.get_player(int(offer.get("player_api_id") or 0), season_year) or {"name": offer.get("player_name"), "overall": valuation.get("market_value", 0) and 75, "potential": 75}
        # ensure ovr/pot
        if not player.get("overall"):
            p2 = await self.bot.db.fetchone("SELECT overall,potential,name,age,position FROM players WHERE api_id=? ORDER BY season DESC, updated_at DESC LIMIT 1", (int(offer.get("player_api_id") or 0),))
            if p2:
                d2 = dict(p2)
                player = {**player, **d2}

        offered = {"wage": wage, "role": role, "signing_bonus": bonus, "contract_years": years}
        result = self.market.evaluate_contract_offer(demand, offered, player, round_no, pressure)

        meta["contract_round"] = round_no
        meta["pressure"] = pressure
        meta["last_offer"] = offered
        meta["last_counter"] = result.get("counter")

        if result.get("accepted"):
            meta["agreed_contract"] = offered
            await self.bot.db.execute(
                "UPDATE transfer_offers SET status='agreed', stage='agreed', meta_json=?, updated_at=? WHERE id=?",
                (json.dumps(meta), utc_now_iso(), int(offer.get("id"))),
            )
            # finalize transfer
            fin = await self._finalize_agreed_offer(offer, offered)
            if fin.get("ok"):
                extra = fin.get("message", "")
                await interaction.followup.send(embed=success_embed(
                    "Contract Agreed — Transfer Completed",
                    result["message"] + "\n\n" + extra
                ), ephemeral=True)
            else:
                await interaction.followup.send(embed=error_embed(
                    "Contract Agreed But Finalization Failed",
                    result["message"] + f"\n\nTransfer finalization failed: {fin.get('reason','unknown')}. The transfer was agreed but could not be auto-finalized; please retry or check season/team state."
                ), ephemeral=True)
            return

        # counter
        await self.bot.db.execute(
            "UPDATE transfer_offers SET status='countered', stage='contract', meta_json=?, updated_at=? WHERE id=?",
            (json.dumps(meta), utc_now_iso(), int(offer.get("id"))),
        )
        counter = result.get("counter") or {}
        view = ContractNegView(self, int(offer.get("id")))
        await interaction.followup.send(embed=info_embed(
            "Contract Counter-Offer",
            result["message"] + "\n\n"
            f"**Your offer:** {fmt_money(wage)}/wk • {role} • bonus {fmt_money(bonus)} • {years} yrs\n"
            f"**Counter demand:** {fmt_money(counter.get('wage'))}/wk • {counter.get('role')} • bonus {fmt_money(counter.get('signing_bonus'))} • {counter.get('contract_years')} yrs\n"
            f"Round: {round_no} • Pressure: {pressure}/5\n"
            "Submit a new offer or pull out."
        ), view=view, ephemeral=True)

    async def _finalize_agreed_offer(self, offer: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
        season_id = offer.get("season_id")
        buying_team_id = int(offer.get("buying_team_api_id") or 0)
        selling_team_id = int(offer.get("selling_team_api_id") or 0)
        player_api_id = int(offer.get("player_api_id") or 0)
        fee = int(offer.get("fee") or 0)
        wage = int(contract.get("wage") or 0)
        bonus = int(contract.get("signing_bonus") or 0)
        years = int(contract.get("contract_years") or 0)
        role = contract.get("role")

        # budget check
        if season_id:
            try:
                await self.bot.season_service.ensure_season_budgets(str(season_id))
            except Exception:
                pass
            bt = await self.bot.db.fetchone("SELECT transfer_budget, wage_budget, team_name FROM season_teams WHERE season_id=? AND team_api_id=?", (str(season_id), buying_team_id))
            if bt:
                btd = dict(bt)
                tb = int(btd.get("transfer_budget") or 0)
                wb = int(btd.get("wage_budget") or 0)
                if tb == 0 and wb == 0:
                    try:
                        from services.season_service import default_season_budgets
                        _s = await self.bot.season_service.get_season(str(season_id))
                        _d = default_season_budgets((_s or {}).get("league_key"), btd.get("team_name"))
                        tb = int(_d.get("transfer_budget") or 0)
                        wb = int(_d.get("wage_budget") or 0)
                    except Exception:
                        pass
                if fee > tb:
                    return {"ok": False, "reason": f"Insufficient transfer budget ({fmt_money(tb)} available, {fmt_money(fee)} needed)."}
                if wage > wb:
                    return {"ok": False, "reason": f"Insufficient wage budget ({fmt_money(wb)}/wk available, {fmt_money(wage)}/wk needed)."}

        # bot transfer limit count - bot transfers against unclaimed/bot teams count
        is_free_agent_transfer = int(offer.get("selling_team_api_id") or 0) == 9000001 or str(offer.get("kind") or "") == "free_agent_contract"
        is_bot_transfer = (str(offer.get("kind")) == "bot_offer") or ((offer.get("selling_manager_user_id") is None) and not is_free_agent_transfer)
        if is_bot_transfer and season_id:
            used = await self.market.get_bot_transfer_count(str(season_id), buying_team_id)
            if used >= 2:
                return {"ok": False, "reason": "Bot transfer limit reached (2/2) before finalization."}

        # Ensure free agent is still available
        if is_free_agent_transfer:
            srow = await self.bot.season_service.get_season(str(season_id)) if season_id else None
            syear = int((srow or {}).get("season_year") or 2026)
            cur_team = await self.bot.db.fetchone(
                "SELECT team_api_id, team_name FROM players WHERE api_id=? AND season=? ORDER BY updated_at DESC LIMIT 1",
                (player_api_id, syear),
            )
            cur_tid = int(dict(cur_team).get("team_api_id") or 0) if cur_team else 0
            if cur_tid != 9000001:
                cur_name = dict(cur_team).get("team_name") if cur_team else None
                return {"ok": False, "reason": f"Player is no longer a free agent" + (f" (currently at {cur_name})." if cur_name else ".")}

        res = await self.bot.season_service.create_pending_transfer(
            season_id=str(season_id) if season_id else None,
            guild_id=offer.get("guild_id"),
            player_api_id=player_api_id,
            to_team_api_id=buying_team_id,
            requested_by=offer.get("buying_user_id"),
            note=f"Agreed via {offer.get('kind')} offer #{offer.get('id')}",
            transfer_type=str(offer.get("kind") or "negotiated"),
            fee=fee,
            wage=wage,
            signing_bonus=bonus,
            contract_years=years,
            role=role,
            meta={"source_offer_id": offer.get("id")},
        )
        if not res.get("ok"):
            return {"ok": False, "reason": str(res.get("reason") or "create_failed")}
        tr = res.get("transfer") or {}
        tid = tr.get("id")
        conf = await self.bot.season_service.confirm_transfer(int(tid), confirmed_by=offer.get("buying_user_id"))
        if not conf.get("ok"):
            return {"ok": False, "reason": str(conf.get("reason") or "confirm_failed")}

        # increment bot transfer usage
        is_free_agent_transfer = int(offer.get("selling_team_api_id") or 0) == 9000001 or str(offer.get("kind") or "") == "free_agent_contract"
        is_bot_transfer = (str(offer.get("kind")) == "bot_offer") or ((offer.get("selling_manager_user_id") is None) and not is_free_agent_transfer)
        if is_bot_transfer and season_id:
            used_after = await self.market.increment_bot_transfer_count(str(season_id), buying_team_id)
        else:
            used_after = None

        msg = (
            f"**{conf.get('player_name')}** moved from **{conf.get('from_team_name')}** to **{conf.get('to_team_name')}**.\n"
            f"Fee: {fmt_money(fee)} • Wage: {fmt_money(wage)}/wk • Bonus: {fmt_money(bonus)} • {years} years • Role: {role}\n"
            f"Transfer finalized (ID `{tid}`). Budgets auto-deducted from buying club."
        )
        if used_after is not None:
            msg += f"\nBot transfers used: **{used_after}/2** for {conf.get('to_team_name')}."
        return {"ok": True, "message": msg, "transfer": conf}

    @app_commands.command(name="askaboutplayer", description="Ask another manager if a player is for sale")
    @app_commands.describe(team_name="Team that owns the player", player_name="Player name")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def askaboutplayer(self, interaction: discord.Interaction, team_name: str, player_name: str, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        buying_team, season = await self._resolve_buying_team_and_season(interaction, league_key)
        if not season:
            # try any active season as fallback for selling team lookup
            season = await self.bot.season_service.get_active_season(interaction.guild_id, league_key=league_key)
        if not season:
            await interaction.followup.send(embed=error_embed("No Active Season", "No active season found. Create one with `/season create`."), ephemeral=True)
            return
        season_id = str(season.get("id"))
        season_year = int(season.get("season_year") or 2026)
        buying_team_name = str(buying_team.get("team_name")) if buying_team else "Your club"
        buying_team_id = int(buying_team.get("team_api_id")) if buying_team else None

        selling_team = await self._find_season_team_by_name(season_id, team_name)
        selling_season = season
        if not selling_team:
            selling_team, selling_season = await self._find_season_team_by_name_any(interaction.guild_id, team_name, prefer_season_id=season_id, league_key=league_key)
        if not selling_team:
            await interaction.followup.send(embed=error_embed("Team Not Found", f"Could not find team `{team_name}`. Check the team name and try again."), ephemeral=True)
            return
        # If found an unclaimed copy, look for a claimed copy in other seasons
        if selling_team.get("manager_user_id") is None:
            seasons_all = await self.bot.season_service.list_seasons(guild_id=interaction.guild_id, status=None)
            if not seasons_all and interaction.guild_id is not None:
                seasons_all = await self.bot.season_service.list_seasons(guild_id=None, status=None)
            for s2 in seasons_all:
                st2 = await self._find_season_team_by_name(str(s2.get("id")), team_name)
                if st2 and st2.get("manager_user_id") is not None:
                    selling_team, selling_season = st2, s2
                    break
        if selling_season:
            selling_sid = str(selling_season.get("id"))
            buying_sid = str((buying_team or {}).get("season_id") or season_id)
            # Prefer keeping buying team's season context when they differ (budget checks rely on it)
            if not buying_team or selling_sid == buying_sid:
                season = selling_season
                season_id = selling_sid
                season_year = int(season.get("season_year") or 2026)
            else:
                # keep buying team season; selling team came from another season - still usable but rare
                season_id = buying_sid
                season = (buying_team or {}).get("_season") or await self.bot.season_service.get_season(buying_sid) or season
        selling_team_name = str(selling_team.get("team_name"))
        selling_team_id = int(selling_team.get("team_api_id"))
        seller_manager_id = selling_team.get("manager_user_id")
        if seller_manager_id is None:
            await interaction.followup.send(embed=error_embed("Bot Team", f"**{selling_team_name}** is an unclaimed bot team. Use `/botoffer` for bot teams."), ephemeral=True)
            return
        if buying_team_id and selling_team_id == buying_team_id:
            await interaction.followup.send(embed=error_embed("Invalid Target", "You cannot enquire about your own team."), ephemeral=True)
            return

        player = await self.market.find_player_in_team(selling_team_id, player_name, season_year)
        if not player:
            await interaction.followup.send(embed=error_embed("Player Not Found", f"Could not find `{player_name}` in **{selling_team_name}**."), ephemeral=True)
            return

        now = utc_now_iso()
        cur = await self.bot.db.execute(
            """INSERT INTO transfer_offers(season_id,guild_id,channel_id,kind,status,buying_user_id,buying_team_api_id,buying_team_name,selling_team_api_id,selling_team_name,selling_manager_user_id,player_api_id,player_name,stage,meta_json,created_at,updated_at)
               VALUES(?,?,?,?, 'pending', ?,?,?,?,?,?,?,?,?, 'ask', ?, ?,?)""",
            (season_id, interaction.guild_id, interaction.channel_id, "ask_about", interaction.user.id, buying_team_id, buying_team_name, selling_team_id, selling_team_name, int(seller_manager_id), int(player.get("api_id")), str(player.get("name")), json.dumps({}), now, now),
        )
        offer_id = cur.lastrowid
        view = AskAboutView(self, int(offer_id))

        seller_mention = f"<@{int(seller_manager_id)}>"
        embed = info_embed(
            "Player Availability Enquiry",
            (
                f"{seller_mention}, **{interaction.user.display_name}** ({buying_team_name}) is asking if "
                f"**{player.get('name')}** ({selling_team_name}) is for sale.\n\n"
                f"Please respond using the buttons below."
            ),
        )
        msg = await interaction.channel.send(content=seller_mention, embed=embed, view=view) if interaction.channel else None
        if msg:
            await self.bot.db.execute("UPDATE transfer_offers SET message_id=?, updated_at=? WHERE id=?", (msg.id, utc_now_iso(), int(offer_id)))

        await interaction.followup.send(embed=success_embed("Enquiry Sent", f"Asked **{selling_team_name}** manager if **{player.get('name')}** is for sale."), ephemeral=True)

    async def handle_ask_response(self, interaction: discord.Interaction, offer_id: int, *, for_sale: bool, view: AskAboutView):
        offer = await self.get_offer(offer_id)
        if not offer:
            await interaction.response.send_message("This enquiry is no longer available.", ephemeral=True)
            return
        seller_id = int(offer.get("selling_manager_user_id") or 0)
        if interaction.user.id != seller_id and not _is_admin(interaction):
            await interaction.response.send_message("Only the selling manager can respond to this enquiry.", ephemeral=True)
            return
        if offer.get("status") != "pending":
            await interaction.response.send_message("This enquiry has already been answered.", ephemeral=True)
            return

        status = "accepted" if for_sale else "rejected"
        await self.bot.db.execute("UPDATE transfer_offers SET status=?, updated_at=? WHERE id=?", (status, utc_now_iso(), int(offer_id)))
        for child in view.children:
            child.disabled = True
        answer = "✅ **For Sale**" if for_sale else "❌ **Not For Sale**"
        await interaction.response.edit_message(
            embed=info_embed("Player Availability Response", f"**{offer.get('player_name')}** ({offer.get('selling_team_name')}): {answer}\nResponded by <@{interaction.user.id}>."),
            view=view,
        )

        # DM buying user
        buyer_id = int(offer.get("buying_user_id") or 0)
        if buyer_id:
            try:
                user = self.bot.get_user(buyer_id) or await self.bot.fetch_user(buyer_id)
                dm_text = (
                    f"Enquiry update: **{offer.get('selling_team_name')}** manager responded about **{offer.get('player_name')}**.\n"
                    f"Status: {'For sale' if for_sale else 'Not for sale'}."
                )
                await user.send(dm_text)
            except Exception:
                logger.exception("Failed to DM askaboutplayer buyer offer_id=%s", offer_id)

    @app_commands.command(name="contractnego", description="Approach a club/player with a fee and start FC27-style contract negotiation")
    @app_commands.describe(player_name="Player name", fee="Transfer fee offer (e.g. 20M)", selling_club="Club that owns the player", league="League (optional)")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def contractnego(self, interaction: discord.Interaction, player_name: str, fee: str, selling_club: str, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        buying_team, season = await self._resolve_buying_team_and_season(interaction, league_key)
        if not buying_team:
            await interaction.followup.send(embed=error_embed("No Claimed Team", "Claim a team first with `/claim`."), ephemeral=True)
            return
        if not season:
            _sid = buying_team.get("season_id")
            season = buying_team.get("_season") or (await self.bot.season_service.get_season(str(_sid)) if _sid else None)
        if not season:
            await interaction.followup.send(embed=error_embed("No Active Season", "No active season found."), ephemeral=True)
            return
        season_id = str(season.get("id"))
        season_year = int(season.get("season_year") or 2026)
        buying_team_id = int(buying_team.get("team_api_id"))
        buying_team_name = str(buying_team.get("team_name"))

        selling_team = await self._find_season_team_by_name(season_id, selling_club)
        selling_season = season
        if not selling_team:
            selling_team, selling_season = await self._find_season_team_by_name_any(interaction.guild_id, selling_club, prefer_season_id=season_id, league_key=league_key)
        if not selling_team:
            await interaction.followup.send(embed=error_embed("Team Not Found", f"Could not find selling club `{selling_club}`. Check the name and try again."), ephemeral=True)
            return
        if selling_season:
            selling_sid = str(selling_season.get("id"))
            buying_sid = str((buying_team or {}).get("season_id") or season_id)
            # Prefer keeping buying team's season context when they differ (budget checks rely on it)
            if not buying_team or selling_sid == buying_sid:
                season = selling_season
                season_id = selling_sid
                season_year = int(season.get("season_year") or 2026)
            else:
                # keep buying team season; selling team came from another season - still usable but rare
                season_id = buying_sid
                season = (buying_team or {}).get("_season") or await self.bot.season_service.get_season(buying_sid) or season
        selling_team_id = int(selling_team.get("team_api_id"))
        selling_team_name = str(selling_team.get("team_name"))
        if selling_team_id == buying_team_id:
            await interaction.followup.send(embed=error_embed("Invalid Target", "You cannot negotiate with your own club."), ephemeral=True)
            return

        player = await self.market.find_player_in_team(selling_team_id, player_name, season_year)
        if not player:
            await interaction.followup.send(embed=error_embed("Player Not Found", f"Could not find `{player_name}` in **{selling_team_name}**."), ephemeral=True)
            return

        offer_fee = self._parse_int_money(fee)
        if offer_fee is None or offer_fee <= 0:
            await interaction.followup.send(embed=error_embed("Invalid Fee", "Enter a valid fee (e.g. 20M)."), ephemeral=True)
            return

        # If selling team is bot/unclaimed, act as bot manager. If human-claimed, still allow bot-style negotiation for contract with player as requested (approach club to buy)
        is_bot = selling_team.get("manager_user_id") is None
        if is_bot:
            used = await self.market.get_bot_transfer_count(season_id, buying_team_id)
            if used >= 2:
                await interaction.followup.send(embed=error_embed("Bot Transfer Limit Reached", f"**{buying_team_name}** has used {used}/2 bot transfers this season. You hit max bot transfers available."), ephemeral=True)
                return
        val = self.market.value_player(player)
        decision = self.market.evaluate_bot_offer(val, offer_fee, player)

        now = utc_now_iso()
        status = "negotiating" if decision["accept"] else "rejected"
        cur = await self.bot.db.execute(
            """INSERT INTO transfer_offers(season_id,guild_id,channel_id,kind,status,buying_user_id,buying_team_api_id,buying_team_name,selling_team_api_id,selling_team_name,selling_manager_user_id,player_api_id,player_name,fee,stage,round,pressure,meta_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'club_offer',0,0,?,?,?)""",
            (season_id, interaction.guild_id, interaction.channel_id, "contract_nego", status, interaction.user.id, buying_team_id, buying_team_name, selling_team_id, selling_team_name, selling_team.get("manager_user_id"), int(player.get("api_id")), str(player.get("name")), int(offer_fee), json.dumps({"valuation": val.__dict__, "decision": decision, "selling_is_bot": is_bot}), now, now),
        )
        offer_id = cur.lastrowid

        if not decision["accept"]:
            await interaction.followup.send(embed=error_embed(
                "Club Offer Rejected",
                f"**{selling_team_name}** response for **{player.get('name')}**:\n\n{decision['reason']}\n\n"
                f"Your offer: {fmt_money(offer_fee)} • Valuation: {fmt_money(val.market_value)}"
            ), ephemeral=True)
            return

        demand = self.market.contract_demand(val, player)
        await self.bot.db.execute(
            "UPDATE transfer_offers SET status='negotiating', stage='contract', meta_json=?, updated_at=? WHERE id=?",
            (json.dumps({"valuation": val.__dict__, "decision": decision, "demand": demand, "contract_round": 0, "pressure": 0, "selling_is_bot": is_bot}), utc_now_iso(), int(offer_id)),
        )
        view = ContractNegView(self, int(offer_id))
        embed = success_embed(
            "Fee Accepted — Contract Negotiation",
            (
                f"**{selling_team_name}** {'(bot manager)' if is_bot else 'manager'} accepted the fee discussion for **{player.get('name')}**.\n"
                f"{decision['reason']}\n\n"
                f"Fee: **{fmt_money(offer_fee)}**\n"
                f"Player: **{player.get('name')}** ({int(player.get('overall') or 0)} OVR / {int(player.get('potential') or 0)} POT)\n\n"
                f"**Opening contract demands (FC27-style):**\n"
                f"• Weekly wage: **{fmt_money(demand['wage'])}/wk**\n"
                f"• Requested role: **{demand['role']}**\n"
                f"• Signing bonus: **{fmt_money(demand['signing_bonus'])}**\n"
                f"• Contract length: **{demand['contract_years']} years**\n\n"
                f"Submit your contract offer. The bot will respond as the negotiating side with counter-offers and pressure."
            ),
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


    @app_commands.command(name="freeagentcontract", description="Start a free-agent contract negotiation (natural language, auto-finalized on agreement)")
    @app_commands.describe(player_name="Free agent name to negotiate with", league="Your league (optional)")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def freeagentcontract(self, interaction: discord.Interaction, player_name: str, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        buying_team, season = await self._resolve_buying_team_and_season(interaction, league_key)
        if not buying_team:
            await interaction.followup.send(embed=error_embed("No Claimed Team", "Claim a team first with `/claim`."), ephemeral=True)
            return
        if not season:
            _sid = buying_team.get("season_id")
            season = buying_team.get("_season") or (await self.bot.season_service.get_season(str(_sid)) if _sid else None)
        if not season:
            await interaction.followup.send(embed=error_embed("No Active Season", "No active season found for your claim. You need an active season to sign a free agent."), ephemeral=True)
            return

        season_id = str(season.get("id"))
        season_year = int(season.get("season_year") or 2026)
        buying_team_id = int(buying_team.get("team_api_id"))
        buying_team_name = str(buying_team.get("team_name"))

        await self.bot.football_data.ensure_seeded()
        free_team_id = 9000001
        player = await self.market.find_player_in_team(free_team_id, player_name, season_year)
        if not player:
            # Helpful fallback: check if the player exists but is not a free agent
            any_player = await self.bot.db.fetchone(
                "SELECT name, team_api_id FROM players WHERE season=? AND LOWER(name) LIKE ? ORDER BY overall DESC LIMIT 1",
                (season_year, f"%{str(player_name).strip().lower()}%"),
            )
            if any_player:
                ap = dict(any_player)
                if int(ap.get("team_api_id") or 0) != free_team_id:
                    await interaction.followup.send(
                        embed=error_embed("Not A Free Agent", f"**{ap.get('name') or player_name}** is not currently a free agent. Use `/transfer` or negotiation commands for signed players."),
                        ephemeral=True,
                    )
                    return
            await interaction.followup.send(embed=error_embed("Free Agent Not Found", f"Could not find a free agent matching `{player_name}`. Use `/freeagents` to browse available free agents."), ephemeral=True)
            return

        # Ensure player is still a free agent
        if int(player.get("team_api_id") or 0) != free_team_id:
            await interaction.followup.send(embed=error_embed("Not A Free Agent", f"**{player.get('name')}** is not currently a free agent."), ephemeral=True)
            return

        val = self.market.value_player(player)
        demand = self.market.contract_demand(val, player)
        now = utc_now_iso()
        meta = {
            "valuation": val.__dict__,
            "demand": demand,
            "contract_round": 0,
            "pressure": 0,
            "is_free_agent": True,
            "selling_is_bot": True,
        }
        cur = await self.bot.db.execute(
            """INSERT INTO transfer_offers(season_id,guild_id,channel_id,kind,status,buying_user_id,buying_team_api_id,buying_team_name,selling_team_api_id,selling_team_name,selling_manager_user_id,player_api_id,player_name,fee,stage,round,pressure,meta_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'contract',0,0,?,?,?)""",
            (
                season_id, interaction.guild_id, interaction.channel_id, "free_agent_contract", "negotiating",
                interaction.user.id, buying_team_id, buying_team_name, free_team_id, "Free Agents", None,
                int(player.get("api_id")), str(player.get("name")), 0,
                json.dumps(meta), now, now,
            ),
        )
        offer_id = cur.lastrowid

        view = ContractNegView(self, int(offer_id))
        embed = success_embed(
            "Free Agent Contract Negotiation",
            (
                f"**{player.get('name')}** is available as a free agent (no transfer fee).\n"
                f"Player: **{player.get('name')}** ({int(player.get('overall') or 0)} OVR / {int(player.get('potential') or 0)} POT)\n"
                f"Position: {player.get('detailed_position') or player.get('position') or '?'} • Age: {player.get('age') or '?'} • Height: {int(player.get('height_cm') or 0) or '?'}cm\n"
                f"Market Value (Transfermarkt model): **{fmt_money(val.market_value)}**\n\n"
                f"**Opening contract demands (FC27-style, natural language negotiation):**\n"
                f"• Weekly wage: **{fmt_money(demand['wage'])}/wk**\n"
                f"• Requested role: **{demand['role']}**\n"
                f"• Signing bonus: **{fmt_money(demand['signing_bonus'])}**\n"
                f"• Contract length: **{demand['contract_years']} years**\n\n"
                f"Only <@{interaction.user.id}> (the buying manager) can negotiate this deal.\n"
                f"Click **Submit Contract Offer** and describe your offer in natural language (e.g. `180k per week, important role, 2M signing bonus, 4 years`).\n"
                f"When agreement is reached, the player is automatically added to **{buying_team_name}**."
            ),
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(TransfersCog(bot))
