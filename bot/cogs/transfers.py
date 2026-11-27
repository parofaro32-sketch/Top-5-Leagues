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
from services.transfer_market_service import TransferMarketService, fmt_money, money_to_int, negotiation_pressure_bar, normalize_role

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
        try:
            await self.cog.handle_contract_modal_submit(interaction, self)
        except Exception:
            logger.exception("ContractOfferModal.on_submit failed offer_id=%s", self.offer_id)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=error_embed("Negotiation Error", "I couldn't process that offer. Please try again."), ephemeral=True)
                else:
                    await interaction.followup.send(embed=error_embed("Negotiation Error", "I couldn't process that offer. Please try again."), ephemeral=True)
            except Exception:
                pass


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


def _clean_llm_user_text(text: Any, *, max_len: int = 3500) -> str:
    """Sanitize LLM output before showing to users.

    Removes code fences/JSON blobs and extracts a readable message when the model
    returns JSON or JSON-like text.
    """
    if text is None:
        return ""
    s = str(text).strip()
    if not s:
        return ""

    # Strip markdown fences
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
        s = s.strip()

    def _try_parse_json(candidate: str) -> Any:
        candidate = (candidate or "").strip()
        if not candidate:
            return None
        try:
            return json.loads(candidate)
        except Exception:
            pass
        # trailing comma repair
        try:
            repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
            return json.loads(repaired)
        except Exception:
            return None

    def _extract_from_obj(obj: Any) -> str | None:
        if isinstance(obj, dict):
            for k in ("message", "response", "reply", "text", "content", "reason"):
                v = obj.get(k)
                if isinstance(v, str) and v.strip():
                    inner = _clean_llm_user_text(v, max_len=max_len)
                    if inner:
                        return inner
            # Build a readable fallback from known fields (no raw JSON)
            parts: list[str] = []
            counter = obj.get("counter") if isinstance(obj.get("counter"), dict) else None
            agreed = obj.get("agreed_terms") if isinstance(obj.get("agreed_terms"), dict) else None
            accepted = obj.get("accepted")
            walked = obj.get("walked_away") or obj.get("walkedAway")
            if walked is True:
                parts.append("The player's camp has walked away from negotiations.")
            elif accepted is True:
                parts.append("The offer has been accepted.")
            elif counter:
                parts.append("The player's camp has made a counter-offer.")
            if counter:
                parts.append(
                    f"Counter: {fmt_money(counter.get('wage') or 0)}/wk, {counter.get('role') or 'Rotation'} role, "
                    f"{fmt_money(counter.get('signing_bonus') or 0)} signing bonus, {int(counter.get('contract_years') or 0)} years."
                )
            if agreed:
                parts.append(
                    f"Agreed terms: {fmt_money(agreed.get('wage') or 0)}/wk, {agreed.get('role') or 'Rotation'} role, "
                    f"{fmt_money(agreed.get('signing_bonus') or 0)} signing bonus, {int(agreed.get('contract_years') or 0)} years."
                )
            if parts:
                return "\n".join(parts)
            return None
        if isinstance(obj, list):
            for item in obj:
                got = _extract_from_obj(item)
                if got:
                    return got
        return None

    # If entire string is JSON (or contains a JSON object), extract readable message
    parsed = _try_parse_json(s)
    if parsed is not None:
        extracted = _extract_from_obj(parsed)
        if extracted:
            s = extracted
        else:
            # Don't dump raw JSON
            s = ""
    else:
        # If message contains an embedded JSON object, try to extract from it
        if "{" in s and "}" in s:
            m = re.search(r"\{[\s\S]*\}", s)
            if m:
                obj = _try_parse_json(m.group(0))
                if isinstance(obj, dict):
                    extracted = _extract_from_obj(obj)
                    if extracted:
                        # Keep any non-JSON prefix/suffix if useful
                        prefix = s[:m.start()].strip()
                        suffix = s[m.end():].strip()
                        # Only keep prefix/suffix if they look like human text, not JSON fragments
                        if prefix and not prefix.startswith("{") and not prefix.startswith("["):
                            s = (prefix + "\n\n" + extracted).strip()
                        elif suffix and not suffix.startswith("{") and not suffix.startswith("["):
                            s = (extracted + "\n\n" + suffix).strip()
                        else:
                            s = extracted

    # Remove stray JSON-like lines/blocks that still slipped through
    s = re.sub(r"```(?:json)?\s*[\s\S]*?```", "", s, flags=re.IGNORECASE).strip()
    # If the remaining text is basically a JSON object, drop it
    if s.startswith("{") and s.endswith("}"):
        maybe = _try_parse_json(s)
        if isinstance(maybe, (dict, list)):
            extracted = _extract_from_obj(maybe)
            s = extracted or ""

    # Collapse excessive blank lines
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 3].rstrip() + "..."
    return s


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
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=error_embed("Interaction Failed", "Something didn't go through. Please try again."), ephemeral=True)
                else:
                    await interaction.followup.send(embed=error_embed("Interaction Failed", "Something didn't go through. Please try again."), ephemeral=True)
            except Exception:
                pass

    async def handle_contract_submit_button(self, interaction: discord.Interaction, offer_id: int):
        offer = await self.get_offer(offer_id)
        if not offer:
            msg = error_embed("Not Found", "This negotiation is no longer available.")
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=msg, ephemeral=True)
            else:
                await interaction.followup.send(embed=msg, ephemeral=True)
            return
        if interaction.user.id != int(offer.get("buying_user_id") or 0):
            msg = error_embed("Not Allowed", "Only the buying manager can submit a contract offer.")
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=msg, ephemeral=True)
            else:
                await interaction.followup.send(embed=msg, ephemeral=True)
            return
        if offer.get("status") not in ("negotiating", "accepted", "countered"):
            msg = error_embed("Inactive", "This negotiation is no longer active.")
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=msg, ephemeral=True)
            else:
                await interaction.followup.send(embed=msg, ephemeral=True)
            return
        await interaction.response.send_modal(ContractOfferModal(self, int(offer_id)))

    async def handle_contract_pullout(self, interaction: discord.Interaction, offer_id: int):
        offer = await self.get_offer(offer_id)
        if not offer:
            msg = error_embed("Not Found", "This negotiation is no longer available.")
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=msg, ephemeral=True)
            else:
                await interaction.followup.send(embed=msg, ephemeral=True)
            return
        if interaction.user.id != int(offer.get("buying_user_id") or 0):
            msg = error_embed("Not Allowed", "Only the buying manager can pull out.")
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=msg, ephemeral=True)
            else:
                await interaction.followup.send(embed=msg, ephemeral=True)
            return
        await self.db_execute(
            "UPDATE transfer_offers SET status='cancelled', updated_at=? WHERE id=?",
            (utc_now_iso(), int(offer_id)),
        )
        cancel_embed = info_embed("Negotiation Cancelled", "Negotiation cancelled by the buying manager.")
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=cancel_embed, view=None)
            else:
                await interaction.edit_original_response(embed=cancel_embed, view=None)
        except Exception:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=cancel_embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=cancel_embed, ephemeral=True)
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
        try:
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

            decision = None
            llm = getattr(self.bot, "llm_service", None)
            if not (llm and getattr(llm, "enabled", False)):
                await interaction.followup.send(
                    embed=error_embed(
                        "LLM Required",
                        "Bot fee negotiation is configured to use LLM only, but the LLM is not enabled on this bot. Set `LLM_API_KEY` on the host and restart the bot.",
                    ),
                    ephemeral=True,
                )
                return

            last_err = None
            for _attempt in range(1):
                try:
                    llm_decision = await llm.negotiate_fee(
                        player=player,
                        valuation=val.__dict__,
                        offer_fee=int(offer_fee),
                        buying_team_name=str(buying_team_name),
                        selling_team_name=str(selling_team_name),
                        round_no=1,
                        previous_pressure=0,
                        max_pressure=10,
                    )
                    if isinstance(llm_decision, dict) and str(llm_decision.get("message") or "").strip():
                        decision = {
                            "accept": bool(llm_decision.get("accepted")),
                            "walked_away": bool(llm_decision.get("walked_away")),
                            "reason": str(llm_decision.get("message") or "").strip(),
                            "pressure": int(llm_decision.get("pressure") or 0),
                            "llm": True,
                        }
                        if decision["pressure"] >= 10:
                            decision["walked_away"] = True
                            decision["accept"] = False
                        if decision["walked_away"]:
                            decision["accept"] = False
                        break
                    last_err = "empty_or_invalid_decision"
                except Exception as e:
                    last_err = str(e)
                    logger.exception("LLM fee negotiation decision failed (botoffer) attempt=%s player=%s", _attempt + 1, player.get("name"))

            if not isinstance(decision, dict):
                # Retry with the same Prompt #1 method (no ad-hoc prompt). negotiate_fee already has internal fallbacks.
                logger.warning("LLM fee negotiation returned no structured result; retrying negotiate_fee (botoffer) player=%s err=%s", player.get("name"), last_err)
                try:
                    llm_decision = await llm.negotiate_fee(
                        player=player,
                        valuation=val.__dict__,
                        offer_fee=int(offer_fee),
                        buying_team_name=str(buying_team_name),
                        selling_team_name=str(selling_team_name),
                        round_no=1,
                        previous_pressure=0,
                        max_pressure=10,
                    )
                    if isinstance(llm_decision, dict) and str(llm_decision.get("message") or "").strip():
                        decision = {
                            "accept": bool(llm_decision.get("accepted")),
                            "walked_away": bool(llm_decision.get("walked_away")),
                            "reason": str(llm_decision.get("message") or "").strip(),
                            "pressure": int(llm_decision.get("pressure") or 0),
                            "llm": True,
                        }
                        if decision["pressure"] >= 10:
                            decision["walked_away"] = True
                            decision["accept"] = False
                        if decision["walked_away"]:
                            decision["accept"] = False
                except Exception as e:
                    last_err = str(e)
                    logger.exception("LLM fee negotiation retry failed (botoffer) player=%s", player.get("name"))

            if not isinstance(decision, dict):
                last_err = last_err or getattr(llm, "last_error", None) or "empty_or_invalid_decision"
                logger.warning("LLM fee negotiation failed after all fallbacks (botoffer) player=%s err=%s", player.get("name"), last_err)
                await interaction.followup.send(
                    embed=error_embed(
                        "LLM Negotiation Failed",
                        f"I couldn't get a valid LLM negotiation decision right now. Please retry your offer. ({str(last_err)[:160]})",
                    ),
                    ephemeral=True,
                )
                return

            _fee_ratio = float(offer_fee) / max(1, int(val.min_accept_fee or val.market_value or 1))
            if "pressure" in decision:
                _fee_pressure = max(0, min(10, int(decision.get("pressure") or 0)))
            else:
                if decision.get("accept"):
                    _fee_pressure = 0 if _fee_ratio >= 1.15 else 1
                elif _fee_ratio < 0.50:
                    _fee_pressure = 10
                elif _fee_ratio < 0.70:
                    _fee_pressure = 8
                elif _fee_ratio < 0.85:
                    _fee_pressure = 6
                elif _fee_ratio < 1.00:
                    _fee_pressure = 4
                else:
                    _fee_pressure = 2
            _fee_pressure = max(0, min(10, int(_fee_pressure)))
            _fee_bar = negotiation_pressure_bar(_fee_pressure, 10)
            decision["pressure"] = _fee_pressure
            decision["pressure_bar"] = _fee_bar

            # /botoffer must always proceed to contract negotiation (no fee-stage refusal that ends the flow).
            # We still use the LLM bot-nego (Prompt #1) message for realism, but the flow continues.
            if not decision.get("accept") or decision.get("walked_away"):
                decision["forced_proceed_to_contract"] = True
            decision["accept"] = True
            decision["walked_away"] = False

            now = utc_now_iso()
            status = "negotiating"
            cur = await self.bot.db.execute(
                """INSERT INTO transfer_offers(season_id,guild_id,channel_id,kind,status,buying_user_id,buying_team_api_id,buying_team_name,selling_team_api_id,selling_team_name,selling_manager_user_id,player_api_id,player_name,fee,stage,round,pressure,meta_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?,?, 'club_offer',0,0,?, ?,?)""",
                (season_id, interaction.guild_id, interaction.channel_id, "bot_offer", status, interaction.user.id, buying_team_id, buying_team_name, selling_team_id, selling_team_name, int(player.get("api_id")), str(player.get("name")), int(offer_fee), json.dumps({"valuation": val.__dict__, "decision": decision, "fee_pressure": _fee_pressure}), now, now),
            )
            offer_id = cur.lastrowid

            llm_fee_msg = _clean_llm_user_text(decision.get("reason") or "") or None
            if decision.get("forced_proceed_to_contract"):
                proceed_note = "Despite the fee concerns, the club is willing to move forward to contract negotiations."
                if llm_fee_msg:
                    if "contract negotiation" not in llm_fee_msg.lower():
                        llm_fee_msg = f"{llm_fee_msg}\n\n{proceed_note}"
                else:
                    llm_fee_msg = f"{_fee_bar}\n\n{proceed_note}"

            # accepted -> start contract negotiation
            demand = self.market.contract_demand(val, player)
            await self.bot.db.execute(
                "UPDATE transfer_offers SET status='negotiating', stage='contract', meta_json=?, updated_at=? WHERE id=?",
                (json.dumps({"valuation": val.__dict__, "decision": decision, "demand": demand, "contract_round": 0, "pressure": 0, "fee_pressure": _fee_pressure}), utc_now_iso(), int(offer_id)),
            )
            view = ContractNegView(self, int(offer_id))
            p = player
            contract_opening = None
            last_open_err = None
            for _attempt in range(2):
                try:
                    contract_opening = await llm.generate_contract_opening(
                        player=player,
                        demand=demand,
                        valuation=val.__dict__,
                        buying_team_name=str(buying_team_name),
                        selling_team_name=str(selling_team_name),
                        is_free_agent=False,
                        scenario="contract_after_fee_agreed",
                    )
                    if contract_opening:
                        break
                    last_open_err = "empty_opening"
                except Exception as e:
                    last_open_err = str(e)
                    logger.exception("LLM contract opening failed (botoffer) attempt=%s offer_id=%s", _attempt + 1, offer_id)

            if not contract_opening:
                # Retry using the same Prompt #3 path (no ad-hoc prompt).
                for _attempt in range(2):
                    try:
                        contract_opening = await llm.generate_contract_opening(
                            player=player,
                            demand=demand,
                            valuation=val.__dict__,
                            buying_team_name=str(buying_team_name),
                            selling_team_name=str(selling_team_name),
                            is_free_agent=False,
                            scenario="contract_after_fee_agreed",
                        )
                        if contract_opening:
                            break
                        last_open_err = last_open_err or "empty_opening"
                    except Exception as e:
                        last_open_err = str(e)
                        logger.exception("LLM contract opening retry failed (botoffer) attempt=%s offer_id=%s", _attempt + 1, offer_id)
                if not contract_opening:
                    await self.bot.db.execute(
                        "UPDATE transfer_offers SET status='cancelled', stage='ended', updated_at=? WHERE id=?",
                        (utc_now_iso(), int(offer_id)),
                    )
                    logger.warning("LLM contract opening failed (botoffer, no deterministic fallback) offer_id=%s err=%s", offer_id, last_open_err)

            contract_opening = _clean_llm_user_text(contract_opening) if contract_opening else None
            if not contract_opening:
                _oe = last_open_err or getattr(llm, "last_error", None) or "empty_opening"
                await interaction.followup.send(
                    embed=error_embed(
                        "LLM Negotiation Failed",
                        f"I couldn't generate the contract opening message right now. Please try again. ({str(_oe)[:160]})",
                    ),
                    ephemeral=True,
                )
                return

            parts = []
            if llm_fee_msg:
                parts.append(llm_fee_msg)
                parts.append(f"**Fee agreed:** {fmt_money(offer_fee)}")
            parts.append(_clean_llm_user_text(contract_opening))
            parts.append("Submit your contract offer using the button below.")
            accept_desc = "\n\n".join(parts)
            embed = success_embed("Offer Accepted — Contract Negotiation", accept_desc[:3900])
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception:
            logger.exception("botoffer command failed")
            try:
                await interaction.followup.send(embed=error_embed("Command Failed", "I couldn't complete that bot offer. Please try again - if it keeps failing, check the team/player names and fee format."), ephemeral=True)
            except Exception:
                pass

    async def handle_contract_modal_submit(self, interaction: discord.Interaction, modal: ContractOfferModal):
        try:
            if not interaction.response.is_done():
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
            # NLU-first canonical parse (shared across contract flows)
            parsed = self.market.parse_contract_reply(raw_msg) if raw_msg else {}
            wage = parsed.get("wage")
            bonus = parsed.get("signing_bonus")
            role = parsed.get("role")
            years = parsed.get("contract_years")

            # Agreement intent: "we agree", "deal", "accept your offer/counter/terms" etc.
            _raw_low = raw_msg.lower().strip()
            _negated_agree = bool(re.search(r"\b(don't|dont|do not|can't|cant|cannot|won't|wont|never|not)\b.{0,20}\bagree\b", _raw_low))
            _agreement_phrases = bool(re.search(
                r"\b(we agree|i agree|agreed|we have a deal|you got a deal|you have a deal|let'?s do it|lets do it|"
                r"accept your (offer|counter|terms|demand)|accept the (offer|counter|terms|demand)|accept those terms|"
                r"i'?ll take it|ill take it|that works for me|works for me|sounds good|fine by me|ok deal|okay deal|go ahead|done deal)\b",
                _raw_low,
            ))
            _plain_accept = bool(re.fullmatch(r"(ok|okay|yes|yep|yeah|sure|fine|alright|all right|agree|agreed|deal|accept|yup)\W*", _raw_low))
            _explicit_accept_ref = bool(re.search(r"\b(accept|agree to|take)\b.{0,30}\b(your|the|that|those)\b.{0,20}\b(offer|counter|terms|demand|deal)\b", _raw_low))
            # Treat as agreement only when it's clearly accepting bot terms (no conflicting specific contract numbers)
            _has_specific_contract_terms = bool(
                re.search(r"(\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|billion|bn)?\s*(?:/wk|wk|week|weekly|per week|pw|p/w))", _raw_low)
                or re.search(r"\b\d+\s*(?:year|yr|years|yrs|season|seasons)\b", _raw_low)
                or re.search(r"\b(signing bonus|sign-on|bonus)\b\s*[:=]?\s*\d", _raw_low)
            )
            _is_agreement = (not _negated_agree) and (_agreement_phrases or _plain_accept or _explicit_accept_ref) and not _has_specific_contract_terms
            if _is_agreement:
                _agree_terms = meta.get("last_counter") or demand or {}
                wage = _agree_terms.get("wage", wage)
                role = _agree_terms.get("role", role)
                bonus = _agree_terms.get("signing_bonus", bonus)
                years = _agree_terms.get("contract_years", years)
                meta["_agreement_accepted"] = True

            missing: list[str] = []

            _bonus_mentioned = bool(re.search(r"\b(signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|sign[- ]?on|upfront|bonus)\b", raw_msg, re.I))
            if bonus is None:
                # If user mentioned bonus but we couldn't parse a value, ask for clarification.
                if _bonus_mentioned and not re.search(r"\b(no|without|zero|none)\b", raw_msg, re.I):
                    _b2 = None
                    for pat in (
                        r"(?:signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|sign[- ]?on|upfront|bonus)\s*[:=]?\s*([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|billion|bn)?)",
                        r"([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|billion|bn)?)\s*(?:signing\s*bonus|sign[- ]?on\s*bonus|signing\s*fee|sign[- ]?on|upfront|bonus)\b",
                    ):
                        for _m in re.finditer(pat, raw_msg, re.I):
                            _raw_tok = str(_m.group(1) or "").strip()
                            _v = money_to_int(_raw_tok)
                            if _v is None:
                                continue
                            # Guard: never let duration-like plain small ints (e.g. "3 years") become bonus
                            if not re.search(r"[€£$kmb]|million|mil|thousand|billion|bn", _raw_tok, re.I):
                                if 1 <= int(_v) <= 7:
                                    continue
                                if years is not None and int(_v) == int(years):
                                    continue
                                if 1900 <= int(_v) <= 2100:
                                    continue
                            if years is not None and int(_v) == int(years) and int(_v) <= 7:
                                continue
                            _b2 = int(_v)
                            break
                        if _b2 is not None:
                            break
                    if _b2 is not None:
                        bonus = _b2
                    else:
                        missing.append("signing bonus amount (e.g. 1m signing bonus, or say 'no bonus')")
                else:
                    # No bonus mentioned - treat as no signing bonus
                    bonus = 0

            # If wage missing, try to infer from remaining money tokens not equal to bonus/years
            if wage is None or int(wage) <= 0:
                _nums: list[int] = []
                for _m in re.finditer(r"([€£$]?\s*\d[\d,.]*\s*(?:[kmb]|million|mil|thousand|billion|bn)?)", raw_msg, re.I):
                    _raw_tok = str(_m.group(1) or "").strip()
                    _v = money_to_int(_raw_tok)
                    if _v is None or _v <= 0:
                        continue
                    # Skip duration-like plain small ints and year-like plain numbers
                    if not re.search(r"[€£$kmb]|million|mil|thousand|billion|bn", _raw_tok, re.I):
                        if 1 <= int(_v) <= 7:
                            continue
                        if years is not None and int(_v) == int(years):
                            continue
                        if 1900 <= int(_v) <= 2100:
                            continue
                    if years is not None and int(_v) == int(years) and int(_v) <= 7:
                        continue
                    _nums.append(int(_v))
                if bonus is not None:
                    _nums = [v for v in _nums if int(v) != int(bonus)]
                wage_cands = [v for v in _nums if 1_000 <= v <= 5_000_000]
                if wage_cands:
                    wage = int(min(wage_cands))
                elif _nums:
                    # Prefer plausible wage-like value, never a small duration-like number
                    _fallback = [v for v in _nums if v >= 1_000]
                    if _fallback:
                        wage = int(min(_fallback))
            if wage is None or int(wage) <= 0:
                missing.append("weekly wage (e.g. 200k/wk, 300k per week)")

            if role is None:
                missing.append("role (Crucial/Important/Rotation/Sporadic/Prospect)")
            if years is None:
                missing.append("contract length (e.g. 4 years)")

            # LLM fallback for missing fields (only fills gaps; deterministic parse takes precedence)
            if missing and not meta.get("_agreement_accepted"):
                try:
                    llm = getattr(self.bot, "llm_service", None)
                    if llm and getattr(llm, "enabled", False):
                        llm_terms = await llm.parse_contract_terms_llm(
                            raw_msg,
                            context={
                                "demand": demand,
                                "known": {"wage": wage, "role": role, "signing_bonus": bonus, "contract_years": years},
                                "missing": list(missing),
                            },
                        )
                        if isinstance(llm_terms, dict):
                            if (wage is None or int(wage) <= 0) and llm_terms.get("wage") is not None:
                                wage = int(llm_terms["wage"])
                            if role is None and llm_terms.get("role"):
                                role = str(llm_terms["role"])
                            if bonus is None and llm_terms.get("signing_bonus") is not None:
                                bonus = int(llm_terms["signing_bonus"])
                            if years is None and llm_terms.get("contract_years") is not None:
                                years = int(llm_terms["contract_years"])
                            # Recompute missing
                            missing = []
                            if wage is None or int(wage) <= 0:
                                missing.append("weekly wage (e.g. 200k/wk, 300k per week)")
                            if role is None:
                                missing.append("role (Crucial/Important/Rotation/Sporadic/Prospect)")
                            if years is None:
                                missing.append("contract length (e.g. 4 years)")
                except Exception:
                    logger.exception("LLM contract term fallback failed")

            # Never block on parsing - LLM handles natural language understanding.
            # Fill any missing fields from last counter / demand so we can always proceed.
            if missing and not meta.get("_agreement_accepted"):
                _fill = meta.get("last_counter") or demand or {}
                if wage is None or int(wage) <= 0:
                    wage = int(_fill.get("wage") or demand.get("wage") or 100_000)
                if role is None:
                    role = str(_fill.get("role") or demand.get("role") or "Rotation")
                if bonus is None:
                    bonus = int(_fill.get("signing_bonus") or demand.get("signing_bonus") or 0)
                if years is None:
                    years = int(_fill.get("contract_years") or demand.get("contract_years") or 3)
                meta["_llm_infer_terms"] = True

            # Safety for agreement-accepted path
            if wage is None or int(wage) <= 0:
                wage = int(demand.get("wage") or 50_000)
            if role is None:
                role = normalize_role(str(demand.get("role") or "Rotation")) or "Rotation"
            if bonus is None:
                bonus = int(demand.get("signing_bonus") or 0)
            if years is None:
                years = int(demand.get("contract_years") or 3)

            wage = int(wage)
            bonus = max(0, int(bonus))
            role = (
                normalize_role(str(role))
                or normalize_role(str(demand.get("role") or ""))
                or "Rotation"
            )
            try:
                years = int(years)
            except Exception:
                years = int(demand.get("contract_years") or 3)
            years = max(1, min(5, years))

            round_no = int(meta.get("contract_round") or 0) + 1
            previous_pressure = max(0, min(10, int(meta.get("pressure") or 0)))
            is_free_agent = bool(meta.get("is_free_agent")) or str(offer.get("kind") or "") == "free_agent_contract" or int(offer.get("selling_team_api_id") or 0) == 9000001

            season_year = int((await self.bot.season_service.get_season(str(offer.get("season_id")) or "") or {}).get("season_year") or 2026)
            player_api_id = int(offer.get("player_api_id") or 0)
            player = await self.market.get_player(player_api_id, season_year) or {}
            # Merge any DB row for this player (any season) to ensure real OVR/POT
            try:
                p2 = await self.bot.db.fetchone(
                    "SELECT api_id,name,age,position,detailed_position,overall,potential,height_cm,weight_kg,nationality,market_value,wage,team_api_id,season FROM players WHERE api_id=? ORDER BY season DESC, updated_at DESC LIMIT 1",
                    (player_api_id,),
                )
                if p2:
                    p2d = dict(p2)
                    player = {**p2d, **{k: v for k, v in (player or {}).items() if v is not None}}
            except Exception:
                logger.exception("Failed to load player DB row for negotiation player_api_id=%s", player_api_id)
            # Last resort: keep name/id, but never fake OVR/POT
            if not player:
                player = {"api_id": player_api_id, "name": offer.get("player_name")}
            player.setdefault("name", offer.get("player_name"))
            player.setdefault("api_id", player_api_id)
            # Normalize OVR/POT to ints when present
            for k in ("overall", "potential"):
                try:
                    if player.get(k) is not None and str(player.get(k)).strip() != "":
                        player[k] = int(round(float(player.get(k))))
                except Exception:
                    pass

            offered = {"wage": wage, "role": role, "signing_bonus": bonus, "contract_years": years}

            result = None
            llm = getattr(self.bot, "llm_service", None)
            if not (llm and getattr(llm, "enabled", False)):
                await interaction.followup.send(
                    embed=error_embed(
                        "LLM Required",
                        "Contract negotiation is configured to use LLM only, but the LLM is not enabled on this bot. Set `LLM_API_KEY` on the host and restart the bot.",
                    ),
                    ephemeral=True,
                )
                return

            last_err = None
            for _attempt in range(1):
                try:
                    llm_decision = await llm.negotiate_contract(
                        player=player,
                        demand=demand,
                        offered=offered,
                        previous_pressure=previous_pressure,
                        max_pressure=10,
                        round_no=int(round_no),
                        is_free_agent=bool(is_free_agent),
                        buying_team_name=str(offer.get("buying_team_name") or ""),
                        selling_team_name=str(offer.get("selling_team_name") or ""),
                        valuation=valuation if isinstance(valuation, dict) else None,
                        scenario="free_agent" if is_free_agent else "contract_after_fee_agreed",
                        user_message=raw_msg,
                    )
                    if isinstance(llm_decision, dict) and str(llm_decision.get("message") or "").strip():
                        result = dict(llm_decision)
                        break
                    last_err = "empty_or_invalid_decision"
                except Exception as e:
                    last_err = str(e)
                    logger.exception("LLM contract negotiation decision failed attempt=%s offer_id=%s", _attempt + 1, offer.get("id"))

            if not isinstance(result, dict):
                # Retry with the same Prompt #2/#3 method (no ad-hoc prompt).
                logger.warning("LLM contract negotiation returned no structured result; retrying negotiate_contract offer_id=%s err=%s", offer.get("id"), last_err)
                for _retry in range(2):
                    try:
                        llm_decision = await llm.negotiate_contract(
                            player=player,
                            demand=demand,
                            offered=offered,
                            previous_pressure=previous_pressure,
                            max_pressure=10,
                            round_no=int(round_no),
                            is_free_agent=bool(is_free_agent),
                            buying_team_name=str(offer.get("buying_team_name") or ""),
                            selling_team_name=str(offer.get("selling_team_name") or ""),
                            valuation=valuation if isinstance(valuation, dict) else None,
                            scenario="free_agent" if is_free_agent else "contract_after_fee_agreed",
                            user_message=raw_msg,
                        )
                        if isinstance(llm_decision, dict) and str(llm_decision.get("message") or "").strip():
                            result = dict(llm_decision)
                            break
                        last_err = "empty_or_invalid_decision"
                    except Exception as e:
                        last_err = str(e)
                        logger.exception("LLM contract negotiation retry failed attempt=%s offer_id=%s", _retry + 1, offer.get("id"))

            if not isinstance(result, dict):
                last_err = last_err or getattr(llm, "last_error", None) or "empty_or_invalid_decision"
                logger.warning("LLM contract negotiation failed after all fallbacks offer_id=%s err=%s", offer.get("id"), last_err)
                await interaction.followup.send(
                    embed=error_embed(
                        "LLM Negotiation Failed",
                        f"I couldn't get a valid LLM negotiation decision right now. Please retry your offer. ({str(last_err)[:160]})",
                    ),
                    ephemeral=True,
                )
                return

            pressure = max(0, min(10, int(result.get("pressure", previous_pressure) or 0)))
            # Only allow walkaway at 100% pressure.
            if pressure >= 10:
                result["walked_away"] = True
                result["accepted"] = False
                result["counter"] = None
                pressure = 10
            if bool(result.get("walked_away")) and pressure < 10:
                result["walked_away"] = False
                result["accepted"] = False
                _mw = str(result.get("message") or "")
                if re.search(r"(?i)\b(walk(?:ing)? away|negotiations? (?:are )?over|talks? (?:are )?over|deal is off)\b", _mw):
                    _mw = re.sub(r"(?i)\b(walk(?:ing)? away|negotiations? (?:are )?over|talks? (?:are )?over|deal is off)\b[^.!\n]*[.!]?", "talks are close to breaking point, but they are still willing to hear an improved offer.", _mw)
                    result["message"] = _mw
            if bool(result.get("accepted")):
                result["walked_away"] = False
                result["counter"] = None
            # Ensure counter is sane when it's a counter offer
            if not result.get("accepted") and not result.get("walked_away"):
                raw_counter = result.get("counter")
                sanitized = None
                try:
                    if llm and hasattr(llm, "_sanitize_contract_counter"):
                        sanitized = llm._sanitize_contract_counter(raw_counter, fallback=demand)
                        if sanitized is None and isinstance(raw_counter, dict):
                            merged = dict(demand or {})
                            merged.update({k: v for k, v in raw_counter.items() if v is not None})
                            sanitized = llm._sanitize_contract_counter(merged, fallback=demand)
                    if sanitized is None and isinstance(raw_counter, dict):
                        # minimal inline sanitize
                        _w = int(raw_counter.get("wage") or demand.get("wage") or 100000)
                        _b = int(raw_counter.get("signing_bonus") or demand.get("signing_bonus") or 0)
                        _y = int(raw_counter.get("contract_years") or demand.get("contract_years") or 3)
                        _r = normalize_role(str(raw_counter.get("role") or demand.get("role") or "Rotation")) or "Rotation"
                        sanitized = {"wage": max(1000, _w), "role": _r, "signing_bonus": max(0, _b), "contract_years": max(1, min(5, _y))}
                except Exception:
                    sanitized = None
                if sanitized is None:
                    sanitized = {
                        "wage": int(demand.get("wage") or 100000),
                        "role": normalize_role(str(demand.get("role") or "Rotation")) or "Rotation",
                        "signing_bonus": int(demand.get("signing_bonus") or 0),
                        "contract_years": int(demand.get("contract_years") or 3),
                    }
                result["counter"] = sanitized
            result["pressure"] = pressure
            bar = negotiation_pressure_bar(pressure, 10)
            result["pressure_bar"] = bar
            raw_msg_out = str(result.get("message") or "").strip()
            cleaned_msg = _clean_llm_user_text(raw_msg_out)
            # Strip any leading pressure bar line from cleaned text; we'll re-add the canonical bar
            cleaned_msg = re.sub(r"^\[Negotiation Pressure:[^\]]*\]\s*", "", cleaned_msg, flags=re.IGNORECASE).strip()
            if not cleaned_msg:
                cleaned_msg = "Negotiation response unavailable."
            msg_out = f"{bar}\n{cleaned_msg}"
            result["message"] = msg_out[:1900]

            meta["contract_round"] = round_no
            meta["pressure"] = pressure
            meta["last_offer"] = offered
            meta["last_counter"] = result.get("counter")
            bar = result.get("pressure_bar") or negotiation_pressure_bar(pressure, 10)

            _walked = bool(result.get("walked_away"))
            _p_now = int(result.get("pressure") or pressure or 0)
            if _walked and _p_now < 10:
                # User rule: contract negotiations continue until pressure reaches 100%.
                result["walked_away"] = False
                result["accepted"] = False
                _p_now = min(max(int(_p_now), int(meta.get("pressure") or 0)), 9)
                result["pressure"] = _p_now
                pressure = _p_now
                result["pressure_bar"] = negotiation_pressure_bar(_p_now, 10)
                if not isinstance(result.get("counter"), dict):
                    result["counter"] = dict(demand or {})
                _msg_now = str(result.get("message") or "").strip()
                _msg_now = re.sub(r"^\[Negotiation Pressure:[^\]]*\]\s*", "", _msg_now, flags=re.IGNORECASE).strip()
                if not _msg_now:
                    _msg_now = "The player's camp is frustrated, but negotiations remain open. Improve your terms."
                if "walk away" in _msg_now.lower() or "negotiations are over" in _msg_now.lower():
                    _msg_now = re.sub(r"(?i)\b(walk(?:ing)? away|negotiations? (?:are )?over|talks? (?:are )?over|deal is off)\b[^.!\n]*[.!]?", "talks are close to breaking point, but they are still willing to hear an improved offer.", _msg_now)
                result["message"] = f"{result['pressure_bar']}\n{_msg_now}"[:1900]
                bar = result["pressure_bar"]
                meta["pressure"] = _p_now
                meta["last_counter"] = result.get("counter")

            if result.get("walked_away"):
                meta["ended_reason"] = "pressure_walkaway"
                await self.bot.db.execute(
                    "UPDATE transfer_offers SET status='cancelled', stage='ended', meta_json=?, updated_at=? WHERE id=?",
                    (json.dumps(meta), utc_now_iso(), int(offer.get("id"))),
                )
                await interaction.followup.send(embed=error_embed(
                    "Negotiation Ended",
                    result["message"]
                ), ephemeral=True)
                return

            if result.get("accepted"):
                agreed = result.get("agreed_terms") if isinstance(result.get("agreed_terms"), dict) else None
                if not agreed:
                    agreed = offered
                # Ensure required keys
                for _k, _fallback in (("wage", offered.get("wage")), ("role", offered.get("role")), ("signing_bonus", offered.get("signing_bonus")), ("contract_years", offered.get("contract_years"))):
                    if agreed.get(_k) is None:
                        agreed[_k] = _fallback
                meta["agreed_contract"] = agreed
                # finalize transfer before marking completed
                fin = await self._finalize_agreed_offer(offer, agreed)
                if fin.get("ok"):
                    meta["finalized_transfer"] = fin.get("transfer")
                    await self.bot.db.execute(
                        "UPDATE transfer_offers SET status='agreed', stage='completed', meta_json=?, updated_at=? WHERE id=?",
                        (json.dumps(meta), utc_now_iso(), int(offer.get("id"))),
                    )
                    extra = fin.get("message", "")
                    await interaction.followup.send(embed=success_embed(
                        "Contract Agreed — Transfer Completed",
                        result["message"] + "\n\n" + extra
                    ), ephemeral=True)
                else:
                    meta["finalization_error"] = fin.get("reason")
                    await self.bot.db.execute(
                        "UPDATE transfer_offers SET status='agreed_pending_finalization', stage='agreed_pending_finalization', meta_json=?, updated_at=? WHERE id=?",
                        (json.dumps(meta), utc_now_iso(), int(offer.get("id"))),
                    )
                    await interaction.followup.send(embed=error_embed(
                        "Contract Agreed — Finalization Pending",
                        result["message"] + f"\n\nThe contract is agreed, but the transfer could not be finalized automatically: {fin.get('reason','unknown')}."
                        + "\n\nNo player move or budget change was completed by this failed finalization step. Please resolve the issue and retry confirmation."
                    ), ephemeral=True)
                return

            # counter
            await self.bot.db.execute(
                "UPDATE transfer_offers SET status='countered', stage='contract', meta_json=?, updated_at=? WHERE id=?",
                (json.dumps(meta), utc_now_iso(), int(offer.get("id"))),
            )
            counter = result.get("counter") or {}
            view = ContractNegView(self, int(offer.get("id")))
            counter_line = ""
            if counter:
                _bar_in_msg = "[Negotiation Pressure:" in str(result.get("message") or "")
                _bar_part = "" if _bar_in_msg else f" • {bar}"
                counter_line = (
                    f"\n\n**Your offer:** {fmt_money(wage)}/wk • {role} • bonus {fmt_money(bonus)} • {years} yrs\n"
                    f"**Counter-demand:** {fmt_money(counter.get('wage'))}/wk • {counter.get('role')} • bonus {fmt_money(counter.get('signing_bonus'))} • {counter.get('contract_years')} yrs\n"
                    f"Round: {round_no}{_bar_part}\n"
                    "Submit a revised offer or pull out. Lowballing or stalling will raise pressure."
                )
            await interaction.followup.send(embed=info_embed(
                "Contract Counter-Offer",
                (result["message"] + counter_line)[:3900]
            ), view=view, ephemeral=True)

        except Exception:
            logger.exception("handle_contract_modal_submit failed offer_id=%s", getattr(modal, "offer_id", None))
            try:
                err_embed = error_embed("Negotiation Error", "I couldn't process that contract offer. Please try again with a clear offer like `200k/wk, important role, 2M signing bonus, 4 years`.")
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=err_embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=err_embed, ephemeral=True)
            except Exception:
                logger.exception("Failed to send negotiation error followup offer_id=%s", getattr(modal, "offer_id", None))

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
                total_transfer_cost = int(fee) + int(bonus)
                if total_transfer_cost > tb:
                    return {"ok": False, "reason": f"Insufficient transfer budget ({fmt_money(tb)} available, {fmt_money(total_transfer_cost)} needed for fee+signing bonus)."}
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
        try:
            conf = await self.bot.season_service.confirm_transfer(int(tid), confirmed_by=offer.get("buying_user_id"))
        except Exception as e:
            logger.exception("confirm_transfer raised for transfer_id=%s", tid)
            raw = str(e) if isinstance(e, RuntimeError) and str(e) else f"confirm_exception:{e.__class__.__name__}"
            reason = raw
            if raw.startswith("insufficient_transfer_budget:"):
                parts = raw.split(":")
                if len(parts) == 3:
                    reason = f"Insufficient transfer budget ({fmt_money(int(parts[1]))} available, {fmt_money(int(parts[2]))} needed)."
            elif raw.startswith("insufficient_wage_budget:"):
                parts = raw.split(":")
                if len(parts) == 3:
                    reason = f"Insufficient wage budget ({fmt_money(int(parts[1]))}/wk available, {fmt_money(int(parts[2]))}/wk needed)."
            elif raw == "buying_team_not_in_season":
                reason = "Buying team is not registered in this season."
            try:
                await self.bot.db.execute("UPDATE season_transfers SET status='failed', updated_at=? WHERE id=? AND status='pending'", (utc_now_iso(), int(tid)))
            except Exception:
                pass
            return {"ok": False, "reason": reason, "transfer_id": tid}
        if not conf.get("ok"):
            reason = str(conf.get("reason") or "confirm_failed")
            try:
                await self.bot.db.execute("UPDATE season_transfers SET status='failed', updated_at=? WHERE id=? AND status='pending'", (utc_now_iso(), int(tid)))
            except Exception:
                pass
            return {"ok": False, "reason": reason, "transfer_id": tid}

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
        try:
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
                   VALUES(?,?,?,?, 'pending', ?,?,?,?,?,?,?,?, 'ask', ?, ?,?)""",
                (season_id, interaction.guild_id, interaction.channel_id, "ask_about", interaction.user.id, buying_team_id, buying_team_name, selling_team_id, selling_team_name, int(seller_manager_id), int(player.get("api_id")), str(player.get("name")), json.dumps({}), now, now),
            )
            offer_id = cur.lastrowid
            view = AskAboutView(self, int(offer_id))

            dm_embed = info_embed(
                "Player Availability Enquiry",
                (
                    f"**{interaction.user.display_name}** ({buying_team_name}) has asked whether "
                    f"**{player.get('name')}** ({selling_team_name}) is for sale.\n\n"
                    f"Please respond using the buttons below. This enquiry was sent privately (no public pings)."
                ),
            )

            msg = None
            dm_failed = False
            send_err = None
            # DM-first delivery (no public mentions/pings)
            try:
                seller_user = self.bot.get_user(int(seller_manager_id)) or await self.bot.fetch_user(int(seller_manager_id))
                msg = await seller_user.send(embed=dm_embed, view=view)
            except discord.Forbidden:
                dm_failed = True
                send_err = "seller_dms_disabled"
                logger.info("askaboutplayer seller DMs disabled offer_id=%s seller=%s", offer_id, seller_manager_id)
            except Exception as e:
                send_err = e
                logger.exception("Failed to DM askaboutplayer seller offer_id=%s", offer_id)

            if msg:
                try:
                    await self.bot.db.execute("UPDATE transfer_offers SET message_id=?, channel_id=?, updated_at=? WHERE id=?", (int(msg.id), int(getattr(msg.channel, "id", 0) or 0) or interaction.channel_id, utc_now_iso(), int(offer_id)))
                except Exception:
                    logger.exception("Failed to store askaboutplayer message_id offer_id=%s", offer_id)
                await interaction.followup.send(
                    embed=success_embed(
                        "Enquiry Sent (DM)",
                        f"Your enquiry about **{player.get('name')}** ({selling_team_name}) was sent privately to the club manager's DMs. "
                        f"They can respond with **For Sale / Not For Sale** buttons.\n\n"
                        f"You'll receive a DM when they respond."
                    ),
                    ephemeral=True,
                )
            elif dm_failed:
                await interaction.followup.send(
                    embed=error_embed(
                        "DM Delivery Failed",
                        f"I couldn't DM **{selling_team_name}**'s manager because their DMs are disabled or unavailable.\n\n"
                        f"Your enquiry for **{player.get('name')}** was recorded (ID: {offer_id}), but it was **not** delivered publicly to avoid exposing negotiations. "
                        f"Please ask them to enable DMs from server members/bots, or contact them directly to check their availability."
                    ),
                    ephemeral=True,
                )
            else:
                logger.error("askaboutplayer could not deliver message offer_id=%s err=%s", offer_id, send_err)
                await interaction.followup.send(
                    embed=error_embed(
                        "Enquiry Not Delivered",
                        f"Your enquiry for **{player.get('name')}** ({selling_team_name}) was created (ID: {offer_id}), but I couldn't deliver it to the manager's DMs right now. "
                        f"No public message was sent. Please try again shortly."
                    ),
                    ephemeral=True,
                )
        except Exception:
            logger.exception("askaboutplayer command failed")
            try:
                await interaction.followup.send(embed=error_embed("Command Failed", "I couldn't complete that enquiry. Please try again - if it keeps failing, check the team/player names."), ephemeral=True)
            except Exception:
                pass

    async def handle_ask_response(self, interaction: discord.Interaction, offer_id: int, *, for_sale: bool, view: AskAboutView):
        offer = await self.get_offer(offer_id)
        if not offer:
            msg = "This enquiry is no longer available."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return
        seller_id = int(offer.get("selling_manager_user_id") or 0)
        if interaction.user.id != seller_id and not _is_admin(interaction):
            msg = "Only the selling manager can respond to this enquiry."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return
        if offer.get("status") != "pending":
            msg = "This enquiry has already been answered."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return

        status = "accepted" if for_sale else "rejected"
        await self.bot.db.execute("UPDATE transfer_offers SET status=?, updated_at=? WHERE id=?", (status, utc_now_iso(), int(offer_id)))
        for child in view.children:
            child.disabled = True

        player_name = str(offer.get("player_name") or "Player")
        selling_team_name = str(offer.get("selling_team_name") or "Selling club")
        buying_team_name = str(offer.get("buying_team_name") or "Buying club")
        answer = "For Sale" if for_sale else "Not For Sale"
        seller_embed = success_embed(
            "Response Recorded",
            f"You marked **{player_name}** ({selling_team_name}) as **{answer}**.\n\nThe enquiring manager has been notified by DM."
        ) if for_sale else info_embed(
            "Response Recorded",
            f"You marked **{player_name}** ({selling_team_name}) as **{answer}**.\n\nThe enquiring manager has been notified by DM."
        )
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=seller_embed, view=view)
            else:
                await interaction.edit_original_response(embed=seller_embed, view=view)
        except Exception:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=seller_embed, ephemeral=True)
                else:
                    await interaction.followup.send(embed=seller_embed, ephemeral=True)
            except Exception:
                pass

        # DM buying user with relevant info
        buyer_id = int(offer.get("buying_user_id") or 0)
        if buyer_id:
            try:
                user = self.bot.get_user(buyer_id) or await self.bot.fetch_user(buyer_id)
                if for_sale:
                    dm_embed = success_embed(
                        "Player For Sale",
                        (
                            f"**{player_name}** ({selling_team_name}) is **available for sale**.\n\n"
                            f"Enquiry from: **{buying_team_name}**\n"
                            f"Selling club: **{selling_team_name}**\n\n"
                            f"Next step: you can approach them with `/contractnego` (manager-to-manager) or use the appropriate transfer command for your situation."
                        )
                    )
                else:
                    dm_embed = info_embed(
                        "Player Not For Sale",
                        (
                            f"**{player_name}** ({selling_team_name}) is **not for sale** at this time.\n\n"
                            f"Enquiry from: **{buying_team_name}**\n"
                            f"Selling club: **{selling_team_name}**"
                        )
                    )
                await user.send(embed=dm_embed)
            except discord.Forbidden:
                logger.info("Could not DM buyer for askaboutplayer offer_id=%s (DMs disabled)", offer_id)
            except Exception:
                logger.exception("Failed to DM askaboutplayer buyer offer_id=%s", offer_id)

    @app_commands.command(name="contractnego", description="Approach a club/player with a fee and start FC27-style contract negotiation")
    @app_commands.describe(player_name="Player name", fee="Transfer fee offer (e.g. 20M)", selling_club="Club that owns the player", league="League (optional)")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def contractnego(self, interaction: discord.Interaction, player_name: str, fee: str, selling_club: str, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        try:
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
            # If found an unclaimed copy, look for a claimed copy in other seasons
            if selling_team.get("manager_user_id") is None:
                seasons_all = await self.bot.season_service.list_seasons(guild_id=interaction.guild_id, status=None)
                if not seasons_all and interaction.guild_id is not None:
                    seasons_all = await self.bot.season_service.list_seasons(guild_id=None, status=None)
                for s2 in seasons_all:
                    st2 = await self._find_season_team_by_name(str(s2.get("id")), selling_club)
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

            # If selling team is bot/unclaimed, bot manager evaluates the fee realistically.
            # If selling team is human-managed, the managers have agreed the fee between themselves -
            # go straight to contract negotiation with NO fee refusal.
            is_bot = selling_team.get("manager_user_id") is None
            llm = getattr(self.bot, "llm_service", None)
            if not (llm and getattr(llm, "enabled", False)):
                await interaction.followup.send(
                    embed=error_embed(
                        "LLM Required",
                        "Contract negotiation is configured to use LLM only, but the LLM is not enabled on this bot. Set `LLM_API_KEY` on the host and restart the bot.",
                    ),
                    ephemeral=True,
                )
                return
            if is_bot:
                used = await self.market.get_bot_transfer_count(season_id, buying_team_id)
                if used >= 2:
                    await interaction.followup.send(embed=error_embed("Bot Transfer Limit Reached", f"**{buying_team_name}** has used {used}/2 bot transfers this season. You hit max bot transfers available."), ephemeral=True)
                    return
            val = self.market.value_player(player)

            # /contractnego is fee-agreed: go straight to contract negotiation.
            # For human-managed clubs this is manager-to-manager agreed; for bot clubs we still
            # move directly into contract talks (fee stage is not a rejection gate here).
            decision = {
                "accept": True,
                "reason": (
                    f"Fee agreed at {fmt_money(offer_fee)} for {player.get('name')} "
                    f"({int(player.get('overall') or 0)} OVR, {int(player.get('potential') or 0)} POT). "
                    "Moving straight to contract negotiation."
                ),
                "manager_agreed": not is_bot,
                "llm": False,
                "pressure": 0,
            }

            _fee_ratio = float(offer_fee) / max(1, int(val.min_accept_fee or val.market_value or 1))
            if "pressure" in decision:
                _fee_pressure = max(0, min(10, int(decision.get("pressure") or 0)))
            else:
                if decision.get("accept"):
                    _fee_pressure = 0 if _fee_ratio >= 1.15 else 1
                elif _fee_ratio < 0.50:
                    _fee_pressure = 10
                elif _fee_ratio < 0.70:
                    _fee_pressure = 8
                elif _fee_ratio < 0.85:
                    _fee_pressure = 6
                elif _fee_ratio < 1.00:
                    _fee_pressure = 4
                else:
                    _fee_pressure = 2
            _fee_bar = negotiation_pressure_bar(_fee_pressure, 10)
            decision["pressure"] = _fee_pressure
            decision["pressure_bar"] = _fee_bar

            now = utc_now_iso()
            status = "negotiating" if decision.get("accept") else "rejected"
            cur = await self.bot.db.execute(
                """INSERT INTO transfer_offers(season_id,guild_id,channel_id,kind,status,buying_user_id,buying_team_api_id,buying_team_name,selling_team_api_id,selling_team_name,selling_manager_user_id,player_api_id,player_name,fee,stage,round,pressure,meta_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'club_offer',0,0,?,?,?)""",
                (season_id, interaction.guild_id, interaction.channel_id, "contract_nego", status, interaction.user.id, buying_team_id, buying_team_name, selling_team_id, selling_team_name, selling_team.get("manager_user_id"), int(player.get("api_id")), str(player.get("name")), int(offer_fee), json.dumps({"valuation": val.__dict__, "decision": decision, "selling_is_bot": is_bot, "fee_pressure": _fee_pressure}), now, now),
            )
            offer_id = cur.lastrowid

            llm_fee_msg = _clean_llm_user_text(decision.get("reason") or "") or None

            if not decision["accept"]:
                _reason_clean = llm_fee_msg or _clean_llm_user_text(decision.get("reason") or "") or "The club rejected the offer."
                reason_text = (
                    f"{_fee_bar}\n\n"
                    f"**{selling_team_name}** response for **{player.get('name')}** ({int(player.get('overall') or 0)} OVR, {int(player.get('potential') or 0)} POT):\n\n{_reason_clean}\n\n"
                    f"Your offer: {fmt_money(offer_fee)} • Valuation: {fmt_money(val.market_value)}"
                )
                await interaction.followup.send(embed=error_embed("Club Offer Rejected", reason_text), ephemeral=True)
                return

            demand = self.market.contract_demand(val, player)
            await self.bot.db.execute(
                "UPDATE transfer_offers SET status='negotiating', stage='contract', meta_json=?, updated_at=? WHERE id=?",
                (json.dumps({"valuation": val.__dict__, "decision": decision, "demand": demand, "contract_round": 0, "pressure": 0, "selling_is_bot": is_bot, "fee_pressure": _fee_pressure}), utc_now_iso(), int(offer_id)),
            )
            view = ContractNegView(self, int(offer_id))
            p = player
            contract_opening = None
            last_open_err = None
            for _attempt in range(2):
                try:
                    contract_opening = await llm.generate_contract_opening(
                        player=player,
                        demand=demand,
                        valuation=val.__dict__,
                        buying_team_name=str(buying_team_name),
                        selling_team_name=str(selling_team_name),
                        is_free_agent=False,
                        scenario="contract_after_fee_agreed",
                    )
                    if contract_opening:
                        break
                    last_open_err = "empty_opening"
                except Exception as e:
                    last_open_err = str(e)
                    logger.exception("LLM contract opening failed (contractnego) attempt=%s offer_id=%s", _attempt + 1, offer_id)

            if not contract_opening:
                # Retry using the same Prompt #3 path (no ad-hoc prompt).
                for _attempt in range(2):
                    try:
                        contract_opening = await llm.generate_contract_opening(
                            player=player,
                            demand=demand,
                            valuation=val.__dict__,
                            buying_team_name=str(buying_team_name),
                            selling_team_name=str(selling_team_name),
                            is_free_agent=False,
                            scenario="contract_after_fee_agreed",
                        )
                        if contract_opening:
                            break
                        last_open_err = last_open_err or "empty_opening"
                    except Exception as e:
                        last_open_err = str(e)
                        logger.exception("LLM contract opening retry failed (contractnego) attempt=%s offer_id=%s", _attempt + 1, offer_id)
                if not contract_opening:
                    await self.bot.db.execute(
                        "UPDATE transfer_offers SET status='cancelled', stage='ended', updated_at=? WHERE id=?",
                        (utc_now_iso(), int(offer_id)),
                    )
                    logger.warning("LLM contract opening failed (contractnego, no deterministic fallback) offer_id=%s err=%s", offer_id, last_open_err)

            contract_opening = _clean_llm_user_text(contract_opening) if contract_opening else None
            if not contract_opening:
                _oe = last_open_err or getattr(llm, "last_error", None) or "empty_opening"
                await interaction.followup.send(
                    embed=error_embed(
                        "LLM Negotiation Failed",
                        f"I couldn't generate the contract opening message right now. Please try again. ({str(_oe)[:160]})",
                    ),
                    ephemeral=True,
                )
                return

            parts = []
            if llm_fee_msg:
                parts.append(llm_fee_msg)
                parts.append(f"**Fee agreed:** {fmt_money(offer_fee)}")
            elif decision.get("manager_agreed"):
                parts.append(f"Fee agreed: **{fmt_money(offer_fee)}** for **{p.get('name')}** ({int(p.get('overall') or 0)} OVR / {int(p.get('potential') or 0)} POT).")
            parts.append(_clean_llm_user_text(contract_opening))
            parts.append("Submit your contract offer using the button below.")
            accept_desc = "\n\n".join(parts)
            embed = success_embed("Fee Accepted — Contract Negotiation", accept_desc[:3900])
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception:
            logger.exception("contractnego command failed")
            try:
                await interaction.followup.send(embed=error_embed("Command Failed", "I couldn't start that contract negotiation. Please try again - if it keeps failing, check the player/club/fee details."), ephemeral=True)
            except Exception:
                pass


    @app_commands.command(name="freeagentcontract", description="Start a free-agent contract negotiation (natural language, auto-finalized on agreement)")
    @app_commands.describe(player_name="Free agent name to negotiate with", league="Your league (optional)")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def freeagentcontract(self, interaction: discord.Interaction, player_name: str, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        try:
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

            llm = getattr(self.bot, "llm_service", None)
            if not (llm and getattr(llm, "enabled", False)):
                await interaction.followup.send(
                    embed=error_embed(
                        "LLM Required",
                        "Free agent contract negotiation is configured to use LLM only, but the LLM is not enabled on this bot. Set `LLM_API_KEY` on the host and restart the bot.",
                    ),
                    ephemeral=True,
                )
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

            opening_msg = None
            last_err = None
            for _attempt in range(1):
                try:
                    opening_msg = await llm.generate_contract_opening(
                        player=player,
                        demand=demand,
                        valuation=val.__dict__,
                        buying_team_name=str(buying_team_name),
                        selling_team_name="Free Agents",
                        is_free_agent=True,
                        scenario="free_agent",
                    )
                    if opening_msg:
                        break
                    last_err = "empty_opening"
                except Exception as e:
                    last_err = str(e)
                    logger.exception("LLM free agent opening failed attempt=%s player=%s", _attempt + 1, player.get("name"))

            if not opening_msg:
                await self.bot.db.execute(
                    "UPDATE transfer_offers SET status='cancelled', stage='ended', updated_at=? WHERE id=?",
                    (utc_now_iso(), int(offer_id)),
                )
                last_err = last_err or getattr(llm, "last_error", None) or "empty_opening"
                logger.warning("LLM free agent opening failed (no deterministic fallback) player=%s err=%s", player.get("name"), last_err)
                await interaction.followup.send(
                    embed=error_embed(
                        "LLM Negotiation Failed",
                        f"I couldn't get a valid LLM opening message right now. Please try the command again. ({str(last_err)[:160]})",
                    ),
                    ephemeral=True,
                )
                return

            view = ContractNegView(self, int(offer_id))
            p = player
            embed = success_embed(
                "Free Agent Contract Negotiation",
                (
                    f"{opening_msg}\n\n"
                    f"Only <@{interaction.user.id}> may negotiate this deal. Submit your contract offer using the button below."
                )[:3900],
            )
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception:
            logger.exception("freeagentcontract command failed")
            try:
                await interaction.followup.send(embed=error_embed("Command Failed", "I couldn't start that free-agent negotiation. Please try again - if it keeps failing, check the player name."), ephemeral=True)
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(TransfersCog(bot))
