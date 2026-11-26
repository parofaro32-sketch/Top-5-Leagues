from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import error_embed, info_embed, success_embed
from db.database import utc_now_iso

logger = logging.getLogger(__name__)

TRAINING_TYPES: list[str] = [
    "Finishing", "Shot Power", "Long Shots", "Volleys", "Penalties",
    "Short Passing", "Long Passing", "Crossing", "Through Balls", "Ball Control",
    "Dribbling", "Close Control", "Agility", "Balance", "Reactions",
    "Sprint Speed", "Acceleration", "Stamina", "Strength", "Jumping",
    "Heading", "Marking", "Standing Tackles", "Sliding Tackles", "Interceptions",
    "Defensive Positioning", "Pressing", "Counter Pressing", "Zonal Defending", "Man Marking",
    "Goalkeeping Reflexes", "Goalkeeping Handling", "Goalkeeping Distribution", "Sweeper Keeper", "One on Ones",
    "Set Pieces", "Free Kicks", "Corners", "Throw-ins", "Penalty Kicks",
    "Attacking Movement", "Off the Ball Runs", "Hold-up Play", "Link-up Play", "Counter Attacking",
    "Build-up Play", "Possession Retention", "Tempo Control", "Switching Play", "Wide Play",
    "Team Shape", "Communication", "Leadership", "Composure", "Match Awareness",
]

TRAINING_TYPES = list(dict.fromkeys(TRAINING_TYPES))  # dedupe, preserve order
# ensure 50+
_extra = [
    "First Touch", "Weak Foot", "Shielding", "Aerial Duels", "Recovery Runs",
]
for _t in _extra:
    if _t not in TRAINING_TYPES:
        TRAINING_TYPES.append(_t)
TRAINING_TYPES = TRAINING_TYPES[:60]

LEAGUE_CHOICES = [
    app_commands.Choice(name="Premier League", value="premier_league"),
    app_commands.Choice(name="La Liga", value="la_liga"),
    app_commands.Choice(name="Bundesliga", value="bundesliga"),
    app_commands.Choice(name="Serie A", value="serie_a"),
    app_commands.Choice(name="Ligue 1", value="ligue_1"),
]


def _money(n: int | float | None) -> str:
    try:
        v = int(n or 0)
    except Exception:
        v = 0
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}€{v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"{sign}€{v/1_000:.0f}K"
    return f"{sign}€{v}"


class TrainingSelect(discord.ui.Select):
    def __init__(self, parent_view: "TrainingSetupView"):
        opts = [discord.SelectOption(label=t, value=t) for t in TRAINING_TYPES[:25]]
        super().__init__(placeholder="Select training type (page 1/3)", min_values=1, max_values=1, options=opts, custom_id="training:type:1")
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_type = self.values[0]
        await interaction.response.send_message(f"Training type selected: **{self.values[0]}**", ephemeral=True)


class TrainingSelect2(discord.ui.Select):
    def __init__(self, parent_view: "TrainingSetupView"):
        opts = [discord.SelectOption(label=t, value=t) for t in TRAINING_TYPES[25:50]]
        super().__init__(placeholder="Select training type (page 2/3)", min_values=1, max_values=1, options=opts, custom_id="training:type:2")
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_type = self.values[0]
        await interaction.response.send_message(f"Training type selected: **{self.values[0]}**", ephemeral=True)


class TrainingSelect3(discord.ui.Select):
    def __init__(self, parent_view: "TrainingSetupView"):
        opts = [discord.SelectOption(label=t, value=t) for t in TRAINING_TYPES[50:]]
        super().__init__(placeholder="Select training type (page 3/3)", min_values=1, max_values=1, options=opts, custom_id="training:type:3")
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_type = self.values[0]
        await interaction.response.send_message(f"Training type selected: **{self.values[0]}**", ephemeral=True)


class TrainingPlayerSelect(discord.ui.Select):
    def __init__(self, parent_view: "TrainingSetupView", players: list[dict[str, Any]]):
        opts = []
        for p in players[:25]:
            pid = str(p.get("api_id"))
            name = str(p.get("name") or pid)
            pos = str(p.get("position") or "")
            opts.append(discord.SelectOption(label=name[:100], value=pid, description=f"{pos}"[:100]))
        super().__init__(placeholder="Select players to assign selected training (up to 25)", min_values=1, max_values=min(25, len(opts)) if opts else 1, options=opts or [discord.SelectOption(label="No players", value="none")], custom_id="training:players")
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        if not self.parent_view.selected_type:
            await interaction.response.send_message("Select a training type first.", ephemeral=True)
            return
        for pid in self.values:
            if pid == "none":
                continue
            self.parent_view.assignments[pid] = self.parent_view.selected_type
        await interaction.response.send_message(
            f"Assigned **{self.parent_view.selected_type}** to {len([v for v in self.values if v!='none'])} player(s).",
            ephemeral=True,
        )


class TrainingSetupView(discord.ui.View):
    def __init__(self, cog: "ClubCog", *, user_id: int, team: dict[str, Any], season: dict[str, Any], players: list[dict[str, Any]]):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.team = team
        self.season = season
        self.players = players
        self.selected_type: str | None = None
        self.assignments: dict[str, str] = {}
        self.add_item(TrainingSelect(self))
        self.add_item(TrainingSelect2(self))
        self.add_item(TrainingSelect3(self))
        self.add_item(TrainingPlayerSelect(self, players))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("Only the manager who opened this training session can use it.", ephemeral=True)
        return False

    @discord.ui.button(label="Start 1-min Training", style=discord.ButtonStyle.success, row=4)
    async def start_training(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.assignments:
            await interaction.response.send_message("Assign at least one player to a training type first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog._start_training(interaction, self)


class ClubCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _get_manager_team(self, interaction: discord.Interaction, league_key: str | None = None):
        ss = self.bot.season_service
        team = await ss.get_user_team_in_active_season(interaction.guild_id, interaction.user.id, league_key=league_key)
        return team

    @app_commands.command(name="lockerroom", description="Open your team's locker room (claimed team)")
    @app_commands.describe(league="League (optional)")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def lockerroom(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        ss = self.bot.season_service
        team = await self._get_manager_team(interaction, league_key=league_key)
        if not team:
            await interaction.followup.send(embed=error_embed("No Claimed Team", "You don't manage a claimed team. Use `/claim` first."), ephemeral=True)
            return
        season = team.get("_season") or (await ss.get_season(str(team.get("season_id"))) if team.get("season_id") else None)
        if not season:
            lk = team.get("league_key") or team.get("season_league_key")
            league_name = LEAGUE_DISPLAY_NAMES.get(lk, str(lk or "League")) if 'LEAGUE_DISPLAY_NAMES' in globals() else str(lk or "League")
            try:
                from services.season_service import LEAGUE_DISPLAY as _LD, default_season_budgets as _dsb
                league_name = _LD.get(lk, league_name)
                defaults = _dsb(lk, team.get("team_name"))
                tb = defaults.get("transfer_budget", 0)
                wb = defaults.get("wage_budget", 0)
            except Exception:
                tb = 0
                wb = 0
            squad_value = 0
            total_wage = 0
            try:
                from services.transfer_market_service import TransferMarketService
                _ms = TransferMarketService(self.bot.db)
                _tid = int(team.get("team_api_id") or 0)
                _sy = int(team.get("season_year") or 2026)
                prow = await self.bot.db.fetchall("SELECT api_id, name, age, position, overall, potential, market_value, wage, team_api_id FROM players WHERE team_api_id=? AND season=?", (_tid, _sy))
                for pr in prow:
                    pd = dict(pr)
                    try:
                        squad_value += int(_ms.value_player(pd).market_value)
                    except Exception:
                        pass
                    try:
                        w = pd.get("wage")
                        if w is None or int(w) <= 0:
                            w = _ms.fair_wage_weekly(pd)
                        total_wage += int(w or 0)
                    except Exception:
                        pass
            except Exception:
                squad_value = 0
                total_wage = 0
            lines = [
                f"**Team:** {team.get('team_name')}",
                f"**Manager:** <@{interaction.user.id}>",
                f"**League:** {league_name}",
                f"**Budgets (default):** Transfer {_money(tb)} • Wage {_money(wb)}/wk",
                f"**Squad Value:** {_money(squad_value)}",
                f"**Total Squad Wage:** {_money(total_wage)}/wk",
                "",
                "No season is set up yet for this team. Your claim is saved — once a season is created, your team will be ready.",
            ]
            embed = info_embed(f"Locker Room — {team.get('team_name')}", "\n".join(lines)[:3900])
            embed.set_footer(text="Your claim is active even without a season. Use /claim to manage it.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        season_id = str(season.get("id"))
        team_id = int(team.get("team_api_id"))
        standings = await ss.get_standings(season_id)
        rank_row = next((r for r in standings if int(r.get("team_api_id") or 0) == team_id), None)
        fixtures = await ss.get_fixtures(season_id, team_api_id=team_id)
        played = [f for f in fixtures if f.get("status") == "played"]
        upcoming = [f for f in fixtures if f.get("status") in ("pending", "live")]
        played.sort(key=lambda f: int(f.get("matchday") or 0), reverse=True)
        upcoming.sort(key=lambda f: int(f.get("matchday") or 0))
        next_fx = upcoming[0] if upcoming else None

        form = str((rank_row or {}).get("form") or "")[-5:] or "-"
        pos = rank_row.get("rank") if rank_row else "-"
        pts = rank_row.get("points") if rank_row else 0
        gd = rank_row.get("goal_difference") if rank_row else 0
        rec = f"{(rank_row or {}).get('won',0)}W {(rank_row or {}).get('drawn',0)}D {(rank_row or {}).get('lost',0)}L"

        # training
        tr_row = await self.bot.db.fetchone(
            "SELECT * FROM training_sessions WHERE season_id=? AND team_api_id=? ORDER BY created_at DESC LIMIT 1",
            (season_id, team_id),
        )
        training_txt = "No recent training session."
        if tr_row:
            tr = dict(tr_row)
            training_txt = f"{tr.get('status','?').title()} • {str(tr.get('starts_at') or '')[:16].replace('T',' ')}"

        # budgets
        tb = int(team.get("transfer_budget") or 0)
        wb = int(team.get("wage_budget") or 0)
        if tb == 0 and wb == 0:
            try:
                await ss.ensure_season_budgets(season_id)
                refreshed = await ss.get_season_team(season_id, team_id)
                if refreshed:
                    team = refreshed
                    tb = int(team.get("transfer_budget") or 0)
                    wb = int(team.get("wage_budget") or 0)
            except Exception:
                pass
        if tb == 0 and wb == 0:
            try:
                from services.season_service import default_season_budgets
                defaults = default_season_budgets(season.get("league_key"), team.get("team_name"))
                tb = int(defaults.get("transfer_budget") or 0)
                wb = int(defaults.get("wage_budget") or 0)
            except Exception:
                pass

        # total squad value and total squad wage
        squad_value = 0
        total_wage = 0
        try:
            from services.transfer_market_service import TransferMarketService
            _ms = TransferMarketService(self.bot.db)
            season_year = int(season.get("season_year") or 2026)
            prow = await self.bot.db.fetchall(
                "SELECT api_id, name, age, position, overall, potential, market_value, wage, team_api_id FROM players WHERE team_api_id=? AND season=?",
                (team_id, season_year),
            )
            for pr in prow:
                pd = dict(pr)
                try:
                    squad_value += int(_ms.value_player(pd).market_value)
                except Exception:
                    pass
                try:
                    w = pd.get("wage")
                    if w is None or int(w) <= 0:
                        w = _ms.fair_wage_weekly(pd)
                    total_wage += int(w or 0)
                except Exception:
                    pass
        except Exception:
            squad_value = 0
            total_wage = 0

        lines = []
        lines.append(f"**Team:** {team.get('team_name')} ({season.get('name')})")
        lines.append(f"**Manager:** <@{interaction.user.id}>")
        lines.append(f"**Table:** #{pos} • {pts} pts • GD {gd:+} • {rec} • Form {form}")
        lines.append(f"**Budgets:** Transfer {_money(tb)} • Wage {_money(wb)}/wk")
        lines.append(f"**Squad Value:** {_money(squad_value)}")
        lines.append(f"**Total Squad Wage:** {_money(total_wage)}/wk")
        lines.append(f"**Training:** {training_txt}")
        if next_fx:
            opp = next_fx.get("away_team_name") if int(next_fx.get("home_team_api_id") or 0) == team_id else next_fx.get("home_team_name")
            ha = "Home" if int(next_fx.get("home_team_api_id") or 0) == team_id else "Away"
            lines.append(f"**Next Match:** MD{next_fx.get('matchday')} vs {opp} ({ha}) • {next_fx.get('status')}")
        else:
            lines.append("**Next Match:** No upcoming fixture.")
        if played:
            last = played[0]
            hs = last.get("home_score"); aws = last.get("away_score")
            lines.append(f"**Last Result:** {last.get('home_team_name')} {hs}-{aws} {last.get('away_team_name')} (MD{last.get('matchday')})")

        # recent results
        recent_lines = []
        for fx in played[:5]:
            is_home = int(fx.get("home_team_api_id") or 0) == team_id
            gf = int(fx.get("home_score") or 0) if is_home else int(fx.get("away_score") or 0)
            ga = int(fx.get("away_score") or 0) if is_home else int(fx.get("home_score") or 0)
            opp = fx.get("away_team_name") if is_home else fx.get("home_team_name")
            res = "W" if gf > ga else ("D" if gf == ga else "L")
            recent_lines.append(f"{res} {gf}-{ga} vs {opp} (MD{fx.get('matchday')})")
        if recent_lines:
            lines.append("\n**Recent Results**\n" + "\n".join(recent_lines))

        morale = "Confident" if str(form).count("W") >= 3 else ("Steady" if "L" not in str(form)[-3:] else "Needs response")
        lines.append(f"\n**Locker Room Mood:** {morale}")

        embed = info_embed(f"Locker Room — {team.get('team_name')}", "\n".join(lines)[:3900])
        embed.set_footer(text="Training, funds, and wage requests are available via /training, /requestfunds, /requestwage")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="training", description="Set team training (50+ types, per-player assignments, 1 minute completion)")
    @app_commands.describe(league="League (optional)")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def training(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        team = await self._get_manager_team(interaction, league_key=league_key)
        if not team:
            await interaction.followup.send(embed=error_embed("No Claimed Team", "Claim a team first with `/claim`."), ephemeral=True)
            return
        _sid = team.get("season_id")
        season = team.get("_season") or (await self.bot.season_service.get_season(str(_sid)) if _sid else None)
        if not season:
            await interaction.followup.send(embed=error_embed("No Season Set", "Your claim is saved, but no season is set up yet. A season is needed for training."), ephemeral=True)
            return
        season_id = str(season.get("id"))
        team_id = int(team.get("team_api_id"))
        season_year = int(season.get("season_year") or 2026)
        current_md = int(season.get("current_matchday") or 1)
        existing_tr = await self.bot.db.fetchone(
            "SELECT id, status, matchday, created_at FROM training_sessions WHERE season_id=? AND team_api_id=? AND matchday=? AND status IN ('running','completed','scheduled') ORDER BY created_at DESC LIMIT 1",
            (season_id, team_id, current_md),
        )
        if existing_tr:
            tr = dict(existing_tr)
            await interaction.followup.send(
                embed=error_embed("Training Limit Reached", f"**{team.get('team_name')}** has already trained for matchday {current_md} (session #{tr.get('id')}, {tr.get('status')}). Only **1 training per matchday** is allowed."),
                ephemeral=True,
            )
            return
        # load players
        rows = await self.bot.db.fetchall(
            "SELECT api_id, name, position, overall, potential, injured, status FROM players WHERE team_api_id=? AND season=? ORDER BY overall DESC LIMIT 25",
            (team_id, season_year),
        )
        players = [dict(r) for r in rows]
        if not players:
            await interaction.followup.send(embed=error_embed("No Players", "No players found for your team squad."), ephemeral=True)
            return
        view = TrainingSetupView(self, user_id=interaction.user.id, team=team, season=season, players=players)
        desc = (
            f"**Team:** {team.get('team_name')}\n"
            "Select a training type, then select players to assign it. Repeat for different groups.\n"
            "Each player can have one training type per session. Training completes in **1 minute**.\n"
            "Injury chance is very low and fair. Training provides small, fair familiarity benefits (no overpowered advantage)."
        )
        await interaction.followup.send(embed=info_embed("Training Setup", desc), view=view, ephemeral=True)

    async def _start_training(self, interaction: discord.Interaction, view: TrainingSetupView):
        season = view.season
        team = view.team
        season_id = str(season.get("id"))
        team_id = int(team.get("team_api_id"))
        now = datetime.now(timezone.utc)
        completes = now + timedelta(minutes=1)
        assignments = {}
        # normalize assignments to player name + type
        for pid, ttype in view.assignments.items():
            p = next((x for x in view.players if str(x.get("api_id")) == str(pid)), None)
            assignments[str(pid)] = {
                "player_api_id": int(pid),
                "player_name": (p or {}).get("name") or str(pid),
                "training_type": str(ttype),
            }
        current_md = int(season.get("current_matchday") or 1)
        existing_tr = await self.bot.db.fetchone(
            "SELECT id FROM training_sessions WHERE season_id=? AND team_api_id=? AND matchday=? AND status IN ('running','completed','scheduled') LIMIT 1",
            (season_id, team_id, current_md),
        )
        if existing_tr:
            await interaction.followup.send(embed=error_embed("Training Limit Reached", f"Only **1 training per matchday** is allowed. {team.get('team_name')} has already trained for matchday {current_md}."), ephemeral=True)
            return
        cur = await self.bot.db.execute(
            """INSERT INTO training_sessions(season_id,guild_id,team_api_id,team_name,user_id,matchday,status,starts_at,completes_at,assignments_json,result_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?, 'running', ?, ?, ?, ?, ?, ?)""",
            (season_id, interaction.guild_id, team_id, team.get("team_name"), interaction.user.id, current_md, now.isoformat(), completes.isoformat(), json.dumps(assignments), json.dumps({}), utc_now_iso(), utc_now_iso()),
        )
        session_id = cur.lastrowid
        # disable view buttons/selects best-effort
        for child in view.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(view=view)
        except Exception:
            pass
        await interaction.followup.send(embed=success_embed(
            "Training Started",
            f"**{team.get('team_name')}** training session started.\n"
            f"Assignments: {len(assignments)} player(s)\n"
            f"Completes: <t:{int(completes.timestamp())}:R>"
        ), ephemeral=True)

        async def _finish():
            await asyncio.sleep(60)
            try:
                await self._complete_training(session_id)
                ch = interaction.channel
                if ch:
                    # summarize
                    row = await self.bot.db.fetchone("SELECT * FROM training_sessions WHERE id=?", (int(session_id),))
                    if row:
                        tr = dict(row)
                        result = {}
                        try:
                            result = json.loads(tr.get("result_json") or "{}")
                        except Exception:
                            result = {}
                        injuries = result.get("injuries") or []
                        lines = [f"Training complete for **{tr.get('team_name')}**.", f"Players trained: {len((json.loads(tr.get('assignments_json') or '{}') or {}))}"]
                        if injuries:
                            lines.append("Injuries (very rare): " + ", ".join(injuries[:5]))
                        else:
                            lines.append("No injuries reported.")
                        lines.append("Training focuses recorded. Small, fair familiarity gains applied for upcoming matches.")
                        await ch.send(embed=success_embed("Training Complete", "\n".join(lines)[:3900]))
            except Exception:
                logger.exception("Training completion failed session_id=%s", session_id)

        asyncio.create_task(_finish())

    async def _complete_training(self, session_id: int):
        row = await self.bot.db.fetchone("SELECT * FROM training_sessions WHERE id=?", (int(session_id),))
        if not row:
            return
        tr = dict(row)
        if tr.get("status") == "completed":
            return
        assignments = {}
        try:
            assignments = json.loads(tr.get("assignments_json") or "{}") or {}
        except Exception:
            assignments = {}
        injuries = []
        trained = []
        rng = random.Random(session_id)
        for pid, a in assignments.items():
            p_name = str((a or {}).get("player_name") or pid)
            ttype = str((a or {}).get("training_type") or "Training")
            # very low injury chance, fair
            if rng.random() < 0.01:  # 1% per trained player, very low overall
                injuries.append(f"{p_name} ({ttype})")
            trained.append({"player_api_id": int((a or {}).get("player_api_id") or pid), "player_name": p_name, "training_type": ttype})
        result = {
            "trained": trained,
            "injuries": injuries,
            "completed_at": utc_now_iso(),
            "note": "Small fair familiarity gains recorded; no overpower advantage.",
        }
        await self.bot.db.execute(
            "UPDATE training_sessions SET status='completed', result_json=?, updated_at=? WHERE id=?",
            (json.dumps(result), utc_now_iso(), int(session_id)),
        )

    @app_commands.command(name="requestfunds", description="Request transfer funds from the board (reply to the board's message)")
    async def requestfunds(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        try:
            await self._start_board_request(interaction, kind="funds")
        except Exception:
            logger.exception("requestfunds command failed")
            try:
                await interaction.followup.send(embed=error_embed("Request Failed", "I couldn't start your transfer funds request. Please try again."), ephemeral=True)
            except Exception:
                pass

    @app_commands.command(name="requestwage", description="Request a wage budget increase from the board (reply to the board's message)")
    async def requestwage(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        try:
            await self._start_board_request(interaction, kind="wage")
        except Exception:
            logger.exception("requestwage command failed")
            try:
                await interaction.followup.send(embed=error_embed("Request Failed", "I couldn't start your wage budget request. Please try again."), ephemeral=True)
            except Exception:
                pass

    async def _start_board_request(self, interaction: discord.Interaction, *, kind: str):
        team = await self._get_manager_team(interaction, league_key=None)
        if not team:
            await interaction.followup.send(embed=error_embed("No Claimed Team", "Claim a team first with `/claim`."), ephemeral=True)
            return
        _sid = team.get("season_id")
        season = team.get("_season") or (await self.bot.season_service.get_season(str(_sid)) if _sid else None)
        if not season:
            await interaction.followup.send(embed=error_embed("No Season Set", "Your claim is saved, but no season is set up yet. A season is needed for board requests."), ephemeral=True)
            return
        season_id = str(season.get("id"))
        team_id = int(team.get("team_api_id"))
        team_name = str(team.get("team_name"))

        # Request limits: 2 per kind per team per season (approved+rejected count)
        req_row = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS c FROM board_requests WHERE season_id=? AND team_api_id=? AND kind=? AND status IN ('approved','rejected')",
            (season_id, team_id, kind),
        )
        used_requests = int((req_row["c"] if req_row else 0) or 0)
        limit = 2
        kind_label = "transfer budget" if kind == "funds" else "wage budget"
        if used_requests >= limit:
            await interaction.followup.send(
                embed=error_embed("Request Limit Reached", f"You hit max requests for {kind_label}. Limit: **{limit}/{limit}** used for {team_name} this season."),
                ephemeral=True,
            )
            return

        # Cancel any previous awaiting_reply for same user/team/kind
        await self.bot.db.execute(
            "UPDATE board_requests SET status='cancelled', updated_at=? WHERE season_id=? AND team_api_id=? AND user_id=? AND kind=? AND status='awaiting_reply'",
            (utc_now_iso(), season_id, team_id, interaction.user.id, kind),
        )

        current_transfer = int(team.get("transfer_budget") or 0)
        current_wage = int(team.get("wage_budget") or 0)
        if current_transfer == 0 and current_wage == 0:
            try:
                from services.season_service import default_season_budgets
                defaults = default_season_budgets(season.get("league_key"), team_name)
                current_transfer = int(defaults.get("transfer_budget") or 0)
                current_wage = int(defaults.get("wage_budget") or 0)
            except Exception:
                pass

        if kind == "funds":
            ask = f"**Transfer Funds Request — {team_name}**"
            detail = (
                f"Manager: <@{interaction.user.id}>\n"
                f"Current transfer budget: **{_money(current_transfer)}**\n"
                f"Request usage: **{used_requests}/{limit}** (transfer requests used this season)\n\n"
                "You are now in a **board meeting**. The board expects a clear, professional case. "
                "Reply to **this message** with your request and include all of the following:\n\n"
                "1. **Amount requested** (e.g. `€50M`, `75,000,000`)\n"
                "2. **Footballing need** - which positions/roles you need, squad depth issues, injuries, tactical reasons\n"
                "3. **Competition objectives** - what you are competing for this season\n"
                "4. **Commitment/track record** - your promise, targets, or achievements (e.g. win league/cup, double, Champions League qualification, strong season)\n"
                "5. **Plan** - how the funds will be used responsibly\n\n"
                "The board is strict and needs-based. Vague requests or unrealistic amounts will be rejected. You can explain your case in natural language. "
                "Do not send a one-liner - make your case properly."
            )
        else:
            ask = f"**Wage Budget Request — {team_name}**"
            detail = (
                f"Manager: <@{interaction.user.id}>\n"
                f"Current wage budget: **{_money(current_wage)}/wk**\n"
                f"Request usage: **{used_requests}/{limit}** (wage requests used this season)\n\n"
                "You are now in a **board meeting**. The board expects a clear, professional case. "
                "Reply to **this message** with your request and include all of the following:\n\n"
                "1. **Weekly wage increase requested** (e.g. `€200k/wk`, `250000`)\n"
                "2. **Why it is needed** - contract renewals, new signings, squad role balance, retention risks\n"
                "3. **Competition objectives** - what you are competing for this season\n"
                "4. **Commitment/track record** - your promise, targets, or achievements (e.g. win league/cup, double, Champions League qualification, strong season)\n"
                "5. **Sustainability** - how this wage increase fits the club plan\n\n"
                "The board is strict and needs-based. Vague requests or unrealistic amounts will be rejected. You can explain your case in natural language. "
                "Do not send a one-liner - make your case properly."
            )

        embed = info_embed(ask, detail[:3900])
        embed.set_footer(text="Reply to this message with your detailed case. The board will respond after reviewing it.")
        sent = await interaction.followup.send(embed=embed, wait=True)
        msg_id = getattr(sent, "id", None)

        await self.bot.db.execute(
            """INSERT INTO board_requests(season_id,guild_id,channel_id,message_id,user_id,team_api_id,team_name,kind,amount,reason,status,meta_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (season_id, interaction.guild_id, interaction.channel_id, int(msg_id) if msg_id else None, interaction.user.id, team_id, team_name, kind, None, None, "awaiting_reply", json.dumps({"request_count_before": used_requests, "request_limit": limit}), utc_now_iso(), utc_now_iso()),
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Handle replies to board request prompts
        try:
            if message.author.bot:
                return
            is_dm = message.guild is None
            req = None
            ref = getattr(message, "reference", None)
            if ref and getattr(ref, "message_id", None):
                reply_to_id = int(ref.message_id)
                row = await self.bot.db.fetchone(
                    "SELECT * FROM board_requests WHERE message_id=? ORDER BY id DESC LIMIT 1",
                    (reply_to_id,),
                )
                if row:
                    req = dict(row)
            # Fallback: if user didn't use Discord reply, handle their latest awaiting request
            if req is None:
                content = (message.content or "").strip()
                if not content or content.startswith("/"):
                    return
                row = None
                if not is_dm:
                    channel_ids: list[int] = [int(message.channel.id)]
                    parent_id = getattr(getattr(message, "channel", None), "parent_id", None)
                    if parent_id:
                        channel_ids.append(int(parent_id))
                    for cid in channel_ids:
                        row = await self.bot.db.fetchone(
                            """SELECT * FROM board_requests
                               WHERE guild_id=? AND channel_id=? AND user_id=? AND status='awaiting_reply'
                               ORDER BY id DESC LIMIT 1""",
                            (int(message.guild.id), int(cid), int(message.author.id)),
                        )
                        if row:
                            break
                    # Last resort: latest awaiting request for this user in this guild
                    if not row:
                        row = await self.bot.db.fetchone(
                            """SELECT * FROM board_requests
                               WHERE guild_id=? AND user_id=? AND status='awaiting_reply'
                               ORDER BY id DESC LIMIT 1""",
                            (int(message.guild.id), int(message.author.id)),
                        )
                else:
                    # DM: latest awaiting request for this user across guilds
                    row = await self.bot.db.fetchone(
                        """SELECT * FROM board_requests
                           WHERE user_id=? AND status='awaiting_reply'
                           ORDER BY id DESC LIMIT 1""",
                        (int(message.author.id),),
                    )
                if not row:
                    return
                req = dict(row)
            # Only the requesting manager can reply
            if int(req.get("user_id") or 0) != int(message.author.id):
                return
            # Basic channel/guild sanity
            if not is_dm and req.get("guild_id") and int(req.get("guild_id")) != int(message.guild.id):
                return
            status_now = str(req.get("status") or "")
            if status_now != "awaiting_reply":
                try:
                    await message.reply(embed=info_embed("Board Request Already Handled", f"This board request is currently **{status_now}**. Run `/requestfunds` or `/requestwage` to start a new request if needed."))
                except Exception:
                    pass
                return

            reason = (message.content or "").strip()
            if not reason:
                await message.reply(embed=error_embed("Empty Reply", "Please reply with your detailed board request (amount, need, and league+cup commitment)."))
                return

            kind = str(req.get("kind") or "funds")
            team_id = int(req.get("team_api_id") or 0)
            season_id = str(req.get("season_id") or "")
            team_name = str(req.get("team_name") or "Your club")
            meta = {}
            try:
                meta = json.loads(req.get("meta_json") or "{}")
            except Exception:
                meta = {}
            used_requests = int(meta.get("request_count_before") or 0)
            limit = int(meta.get("request_limit") or 2)

            # Load team/season for budgets
            season = await self.bot.season_service.get_season(season_id) if season_id else None
            team = await self.bot.season_service.get_season_team(season_id, team_id) if season_id else None
            current_transfer = int((team or {}).get("transfer_budget") or 0)
            current_wage = int((team or {}).get("wage_budget") or 0)
            if team and current_transfer == 0 and current_wage == 0:
                try:
                    from services.season_service import default_season_budgets
                    d = default_season_budgets((season or {}).get("league_key"), team_name)
                    current_transfer = int(d.get("transfer_budget") or 0)
                    current_wage = int(d.get("wage_budget") or 0)
                except Exception:
                    pass

            # Parse amount from reply
            from services.transfer_market_service import money_to_int
            amount = money_to_int(reason)
            # If multiple numbers, try to pick one that looks like a budget request (largest plausible)
            nums = []
            for m in re.finditer(r"[€£$]?\s*\d[\d,\.]*\s*[kmb]?", reason, re.I):
                v = money_to_int(m.group(0))
                if v is not None:
                    nums.append(v)
            if nums:
                # pick the largest plausible amount for the request kind
                plausible = [v for v in nums if (v >= 10000 if kind == "funds" else v >= 5000)]
                amount = max(plausible) if plausible else max(nums)

            if amount is None or amount <= 0:
                await self.bot.db.execute("UPDATE board_requests SET status='rejected', reason=?, updated_at=? WHERE id=?", (reason, utc_now_iso(), int(req["id"])))
                await message.reply(embed=error_embed("Board Meeting Result", (
                    f"**Board Meeting — {team_name}**\n"
                    f"Manager: <@{message.author.id}>\n\n"
                    "**Decision: REJECTED ❌**\n"
                    "- No clear requested amount was found in your message.\n"
                    "- Reply to the board request with a clear amount (e.g. `€50M` for transfer funds or `€200k/wk` for wage budget) and a detailed case."
                )))
                return

            # Natural-language board reasoning (local, no LLM)
            from services.nlu_service import board_argument_strength
            analysis = board_argument_strength(reason, kind=kind, amount=amount, current_budget=(current_transfer if kind == "funds" else current_wage))
            achievements = analysis.get("achievements") or {}
            commit = analysis.get("commitment") or {}
            need_hits = list(analysis.get("need_hits") or [])

            # Season evidence for verification when available
            season_evidence = {}
            try:
                # Use standings (not season_teams) for position/points
                try:
                    standings = await self.bot.season_service.get_standings(season_id)
                except Exception:
                    standings = []
                my_st = next((r for r in standings if int(r.get("team_api_id") or 0) == team_id), None)
                my_position = int(my_st.get("rank") or 0) if my_st and my_st.get("rank") is not None else None
                if my_position is None and my_st is not None:
                    # fallback: compute rank index
                    for i, r in enumerate(standings, start=1):
                        if int(r.get("team_api_id") or 0) == team_id:
                            my_position = i
                            break

                played_rows = await self.bot.db.fetchall("SELECT status, home_team_api_id, away_team_api_id, league_key, meta_json FROM fixtures WHERE season_id=?", (season_id,))
                played_rows = [dict(r) for r in played_rows]
                total_fixtures = len(played_rows)
                played_fixtures = sum(1 for r in played_rows if str(r.get("status") or "") in {"played", "completed", "finalized", "ft"})

                # squad context
                squad_size = 0
                injured_count = 0
                avg_overall = None
                try:
                    s_year = int((season or {}).get("season_year") or 2026)
                    prow = await self.bot.db.fetchall(
                        "SELECT overall, injured, status FROM players WHERE team_api_id=? AND season=?",
                        (team_id, s_year),
                    )
                    prow = [dict(r) for r in (prow or [])]
                    squad_size = len(prow)
                    injured_count = sum(1 for p in prow if int(p.get("injured") or 0) == 1 or str(p.get("status") or "").lower() in {"injured", "out"})
                    ovs = [float(p.get("overall") or 0) for p in prow if float(p.get("overall") or 0) > 0]
                    avg_overall = (sum(ovs) / len(ovs)) if ovs else None
                except Exception:
                    pass

                # multi-competition workload (other active seasons/fixtures for same teams?)
                comp_count = 1
                try:
                    other_fixt = await self.bot.db.fetchall(
                        "SELECT DISTINCT league_key FROM fixtures WHERE (home_team_api_id=? OR away_team_api_id=?) AND status IN ('pending','live')",
                        (team_id, team_id),
                    )
                    comp_count = max(1, len({str(dict(r).get("league_key") or "") for r in (other_fixt or []) if str(dict(r).get("league_key") or "")}))
                except Exception:
                    pass

                season_evidence = {
                    "total_teams": len(standings),
                    "my_position": my_position,
                    "my_points": int(my_st.get("points") or 0) if my_st else None,
                    "my_played": int(my_st.get("played") or 0) if my_st else None,
                    "my_won": int(my_st.get("won") or 0) if my_st else None,
                    "my_drawn": int(my_st.get("drawn") or 0) if my_st else None,
                    "my_lost": int(my_st.get("lost") or 0) if my_st else None,
                    "my_gd": int(my_st.get("goal_difference") or 0) if my_st else None,
                    "total_fixtures": total_fixtures,
                    "played_fixtures": played_fixtures,
                    "squad_size": squad_size,
                    "injured_count": injured_count,
                    "avg_overall": round(avg_overall, 1) if avg_overall is not None else None,
                    "competition_count": comp_count,
                }
            except Exception:
                logger.exception("Failed to collect season evidence for board request")

            # Interpret achievements/commitments flexibly
            text_l = reason.lower()
            double_claim = bool(achievements.get("double"))
            treble_claim = bool(achievements.get("treble"))
            league_claim = bool(achievements.get("league_title") or double_claim or treble_claim)
            cup_claim = bool(achievements.get("domestic_cup") or double_claim or treble_claim)
            continental_claim = bool(achievements.get("continental") or treble_claim)
            strong_claim = bool(achievements.get("strong_season") or achievements.get("qualification") or achievements.get("promotion") or achievements.get("survival"))

            # Commitment/insurance is satisfied by explicit promise OR strong achievement-based argument
            explicit_insurance = bool(commit.get("league_commitment") and commit.get("cup_commitment"))
            achievement_insurance = bool(double_claim or treble_claim or (league_claim and cup_claim))
            competing_many = bool(re.search(r"\b(4|four|three|3|multiple)\s+competitions?\b", text_l) or "champions league" in text_l or "europe" in text_l)
            insurance_ok = explicit_insurance or achievement_insurance

            # Need/argument quality
            need_score = float(analysis.get("need_score") or 0.0)
            reason_quality = float(analysis.get("reason_quality") or 0.0)
            clear_need = reason_quality >= 0.45 and len(reason.strip()) >= 20

            # Amount limits
            if kind == "funds":
                max_reasonable = 120_000_000
                soft_cap = 60_000_000
                current = current_transfer
            else:
                max_reasonable = 750_000
                soft_cap = 350_000
                current = current_wage

            amount_reasonable = amount <= max_reasonable
            amount_soft = amount <= soft_cap

            # Slightly relax strictness when user provides strong natural context (double/treble/CL etc.)
            if (double_claim or treble_claim or continental_claim or competing_many) and reason_quality < 0.45:
                clear_need = len(reason.strip()) >= 15
            if (achievement_insurance or explicit_insurance) and not clear_need and len(reason.strip()) >= 30:
                clear_need = True

            # Human-readable achievement summary
            ach_labels = []
            if treble_claim: ach_labels.append("treble claim")
            elif double_claim: ach_labels.append("double/league+cup claim")
            else:
                if league_claim: ach_labels.append("league title claim")
                if cup_claim: ach_labels.append("cup claim")
                if continental_claim: ach_labels.append("continental/Champions League claim")
            if achievements.get("qualification"): ach_labels.append("qualification claim")
            if achievements.get("promotion"): ach_labels.append("promotion claim")
            if achievements.get("survival"): ach_labels.append("survival/relegation battle claim")
            if achievements.get("strong_season"): ach_labels.append("strong season claim")
            if competing_many: ach_labels.append("multiple competitions / squad demand")

            # Build a flexible, context-aware board decision (no hard keyword gate)
            reasons_out = []
            approved = False
            approved_amount = int(amount)

            pos = season_evidence.get("my_position")
            total_teams = int(season_evidence.get("total_teams") or 0)
            squad_size = int(season_evidence.get("squad_size") or 0)
            injured_count = int(season_evidence.get("injured_count") or 0)
            comp_count = int(season_evidence.get("competition_count") or 1)
            avg_ovr = season_evidence.get("avg_overall")

            # Signal extraction from natural language
            t = text_l
            wants_rebuild = bool(re.search(r"\brebuild|restructur|refresh|reset|new project\b", t))
            wants_retention = bool(re.search(r"\bkeep (our|my|the) (best|key|top) players|retain|contract renewal|renewals|keep hold of\b", t))
            wants_compete = bool(re.search(r"\bcompete|challenge|push for|go for|aim for|fight for\b", t))
            finance_healthy = bool(re.search(r"\bfinances? (are )?(healthy|good|strong|stable)|healthy finances?|financially (healthy|strong|stable)|money in the bank\b", t))
            struggling = bool(re.search(r"\bstruggl(e|ing)|hard to compete|can't compete|cannot compete|behind (the )?(big|bigger) clubs\b", t))
            injuries_need = bool(re.search(r"\binjur(y|ies|ed)|fitness crisis\b", t))
            small_squad = bool(re.search(r"\bsmall squad|squad is (too )?small|thin squad|lack of depth|not enough depth|need depth\b", t))
            low_wage_budget = bool(re.search(r"\bwage budget (is )?(too )?low|can't afford wages|cannot afford wages|wages? too high\b", t))
            european_next = bool(re.search(r"\bchampions league next season|europa league next season|conference league next season|playing in europe next season|european football next season\b", t))
            promotion_push = bool(re.search(r"\bpromotion|promoted|go up\b", t))
            relegation_fight = bool(re.search(r"\bavoid relegation|relegation battle|stay up|fight relegation\b", t))

            # Contextual boosts
            context_boost = 0.0
            if injuries_need or injured_count >= 3:
                context_boost += 0.08
                if injured_count >= 3:
                    reasons_out.append(f"Squad context: {injured_count} injured/unavailable players noted.")
            if small_squad or (0 < squad_size < 22):
                context_boost += 0.08
                if 0 < squad_size < 22:
                    reasons_out.append(f"Squad context: squad size is {squad_size}, which is limited for a full season.")
            if comp_count >= 2 or competing_many or european_next:
                context_boost += 0.10
                if comp_count >= 2:
                    reasons_out.append(f"Competition load: {comp_count} active competitions detected for this club.")
                elif european_next:
                    reasons_out.append("Competition context: European football next season was considered.")
            if wants_rebuild:
                context_boost += 0.06
            if wants_retention or low_wage_budget:
                context_boost += 0.07
            if finance_healthy:
                context_boost += 0.03
            if struggling or relegation_fight:
                context_boost += 0.05
            if promotion_push or achievements.get("promotion"):
                context_boost += 0.07
            if strong_claim or (isinstance(pos, int) and pos > 0 and total_teams > 0 and pos <= max(2, total_teams // 4)):
                context_boost += 0.08
                if isinstance(pos, int) and pos > 0 and pos <= max(2, total_teams // 4):
                    reasons_out.append(f"Performance context: current league position is {pos}/{total_teams}.")
            if wants_compete:
                context_boost += 0.03

            # Achievement / commitment context
            achievement_boost = 0.0
            if achievement_insurance or explicit_insurance:
                achievement_boost += 0.18
            if treble_claim:
                achievement_boost += 0.10
            elif double_claim:
                achievement_boost += 0.08
            elif league_claim or cup_claim or continental_claim:
                achievement_boost += 0.05
            if achievements.get("qualification"):
                achievement_boost += 0.05

            # Amount realism relative to current budget
            current = current_transfer if kind == "funds" else current_wage
            ratio = (float(amount) / float(current)) if current > 0 else (1.0 if amount > 0 else 0.0)
            amount_pressure = 0.0
            if amount_reasonable:
                if amount <= soft_cap:
                    amount_pressure += 0.10
                elif ratio <= 0.50:
                    amount_pressure += 0.06
                elif ratio <= 1.00:
                    amount_pressure += 0.02
                else:
                    amount_pressure -= 0.03
            else:
                amount_pressure -= 0.20

            # Clear need: be flexible for natural language
            detail_ok = len(reason.strip()) >= 12
            has_any_reason = bool(need_hits) or any([wants_rebuild, wants_retention, wants_compete, finance_healthy, struggling, injuries_need, small_squad, low_wage_budget, european_next, promotion_push, relegation_fight, competing_many, strong_claim, league_claim, cup_claim, continental_claim])
            clear_need = detail_ok and (reason_quality >= 0.35 or has_any_reason)

            # Final case score
            case_score = (reason_quality * 0.55) + (need_score * 0.20) + context_boost + achievement_boost + amount_pressure
            # small bonus for explicit, well-formed request
            if amount > 0 and has_any_reason:
                case_score += 0.05

            # Hard cap on unreasonable amounts
            if not amount_reasonable:
                reasons_out.append(f"Requested amount ({_money(amount)}) is above the reasonable single-request board limit ({_money(max_reasonable)}).")

            # Decision thresholds (strict but fair)
            approve_threshold = 0.52
            partial_threshold = 0.42

            if amount_reasonable and (case_score >= approve_threshold or (clear_need and case_score >= partial_threshold)):
                # decide full vs partial
                if case_score >= 0.82 and amount <= max_reasonable:
                    approved_amount = int(amount)
                elif case_score >= 0.68:
                    # allow up to request if within soft cap, else partial
                    if amount <= soft_cap:
                        approved_amount = int(amount)
                    else:
                        approved_amount = int(soft_cap + (amount - soft_cap) * 0.75)
                elif case_score >= 0.55:
                    approved_amount = int(min(amount, soft_cap if amount > soft_cap else amount))
                    if amount > soft_cap:
                        approved_amount = int(soft_cap + (amount - soft_cap) * 0.50)
                else:
                    # partial approval for borderline but reasonable case
                    approved_amount = int(min(amount, max(1, soft_cap)))
                    if amount > soft_cap:
                        approved_amount = int(soft_cap + (amount - soft_cap) * 0.35)

                approved_amount = max(1, min(int(approved_amount), int(amount), int(max_reasonable)))
                if approved_amount < int(amount):
                    reasons_out.append(f"Board adjusted the amount after reviewing case strength and club context: {_money(amount)} → {_money(approved_amount)}.")
                else:
                    reasons_out.append("Board accepted the case based on club context, needs, and current situation.")
                approved = True
            else:
                approved = False
                if not clear_need:
                    reasons_out.append("The board needs a clearer footballing case. Explain what you need, why you need it, and how it fits the club's situation.")
                elif amount_reasonable:
                    reasons_out.append("The board was not convinced the request is justified strongly enough at this time. Provide more concrete context (squad needs, injuries, competition load, performance, finances, or objectives).")

            status = "approved" if approved else "rejected"
            meta_out = {
                **meta,
                "need_score": round(need_score, 3),
                "reason_quality": round(reason_quality, 3),
                "case_score": round(case_score, 3),
                "need_hits": need_hits[:20],
                "insurance_ok": insurance_ok,
                "explicit_insurance": explicit_insurance,
                "achievement_insurance": achievement_insurance,
                "achievements": achievements,
                "commitment": commit,
                "season_evidence": season_evidence,
                "amount_reasonable": amount_reasonable,
                "amount_soft": amount_soft,
                "approved_amount": approved_amount if approved else 0,
                "natural_language_eval": True,
            }
            await self.bot.db.execute(
                "UPDATE board_requests SET amount=?, reason=?, status=?, meta_json=?, updated_at=? WHERE id=?",
                (int(amount), reason, status, json.dumps(meta_out), utc_now_iso(), int(req["id"])),
            )

            board_lines = []
            board_lines.append(f"**Board Meeting — {team_name}**")
            board_lines.append(f"Manager: <@{message.author.id}>")
            board_lines.append(f"Request: {'Transfer funds' if kind=='funds' else 'Weekly wage budget increase'} of **{_money(amount)}**")
            board_lines.append(f"Request usage: **{used_requests + 1}/{limit}** ({'transfer' if kind=='funds' else 'wage'} requests this season)")
            board_lines.append("")
            board_lines.append("**Board assessment (strict, needs-based):**")
            if kind == "funds":
                board_lines.append(f"- Current transfer budget: {_money(current_transfer)}")
            else:
                board_lines.append(f"- Current wage budget: {_money(current_wage)}/wk")
            board_lines.append(f"- Case understood: {', '.join(ach_labels[:6]) if ach_labels else 'general club/season context considered'}")
            board_lines.append(f"- Identified needs: {', '.join(need_hits[:8]) if need_hits else 'context reviewed from your message'}")
            board_lines.append(f"- Commitment/achievement case: {'✅ strong' if insurance_ok else '➖ considered in overall case'}")
            if season_evidence.get("my_position"):
                board_lines.append(f"- Season context: {team_name} are currently {season_evidence.get('my_position')}th with {season_evidence.get('my_points', 0)} pts ({season_evidence.get('my_played', 0)} played).")

            if approved:
                final_amount = int(approved_amount)
                if kind == "funds":
                    await self.bot.db.execute(
                        "UPDATE season_teams SET transfer_budget=COALESCE(transfer_budget,0)+?, updated_at=? WHERE season_id=? AND team_api_id=?",
                        (final_amount, utc_now_iso(), season_id, team_id),
                    )
                    new_val = current_transfer + final_amount
                    board_lines.append("")
                    board_lines.append("**Decision: APPROVED ✅**")
                    for r in reasons_out:
                        board_lines.append(f"- {r}")
                    board_lines.append(f"- Approved transfer funds: **{_money(final_amount)}**")
                    board_lines.append(f"- Transfer budget updated: {_money(current_transfer)} → **{_money(new_val)}**")
                else:
                    await self.bot.db.execute(
                        "UPDATE season_teams SET wage_budget=COALESCE(wage_budget,0)+?, updated_at=? WHERE season_id=? AND team_api_id=?",
                        (final_amount, utc_now_iso(), season_id, team_id),
                    )
                    new_val = current_wage + final_amount
                    board_lines.append("")
                    board_lines.append("**Decision: APPROVED ✅**")
                    for r in reasons_out:
                        board_lines.append(f"- {r}")
                    board_lines.append(f"- Approved wage increase: **{_money(final_amount)}/wk**")
                    board_lines.append(f"- Wage budget updated: {_money(current_wage)}/wk → **{_money(new_val)}/wk**")
                board_lines.append("- Board note: approval is based on the case presented and the club's competitive expectations.")
                result_embed = success_embed("Board Meeting Result", "\n".join(board_lines)[:3900])
                try:
                    await message.reply(embed=result_embed)
                except Exception:
                    try:
                        await message.channel.send(content=f"<@{message.author.id}>", embed=result_embed)
                    except Exception:
                        pass
                try:
                    await message.author.send(embed=result_embed)
                except Exception:
                    pass
            else:
                board_lines.append("")
                board_lines.append("**Decision: REJECTED ❌**")
                for r in reasons_out:
                    board_lines.append(f"- {r}")
                board_lines.append("- Reply with a stronger, detailed case (achievements, competition demands, squad need, and your plan). Natural language is fine.")
                result_embed = error_embed("Board Meeting Result", "\n".join(board_lines)[:3900])
                try:
                    await message.reply(embed=result_embed)
                except Exception:
                    try:
                        await message.channel.send(content=f"<@{message.author.id}>", embed=result_embed)
                    except Exception:
                        pass
                try:
                    await message.author.send(embed=result_embed)
                except Exception:
                    pass
        except Exception:
            logger.exception("Failed to process board request reply")
            err = error_embed("Board Request Error", "I couldn't process your board request reply. Please try again with a clear amount and a detailed case in natural language (what you need, why, and your objectives/achievements).")
            try:
                await message.reply(embed=err)
            except Exception:
                try:
                    await message.channel.send(content=f"<@{message.author.id}>", embed=err)
                except Exception:
                    try:
                        await message.author.send(embed=err)
                    except Exception:
                        pass

async def setup(bot):
    await bot.add_cog(ClubCog(bot))
