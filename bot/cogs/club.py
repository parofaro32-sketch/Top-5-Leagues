from __future__ import annotations

import asyncio
import json
import logging
import random
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

    @app_commands.command(name="lockerroom", description="Open your team's locker room (claimed team in active season)")
    @app_commands.describe(league="League (optional)")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def lockerroom(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        ss = self.bot.season_service
        team = await self._get_manager_team(interaction, league_key=league_key)
        if not team:
            await interaction.followup.send(embed=error_embed("No Claimed Team", "You don't manage a claimed team in an active season. Use `/claim` first."), ephemeral=True)
            return
        season = team.get("_season") or await ss.get_season(str(team.get("season_id")))
        if not season:
            await interaction.followup.send(embed=error_embed("Season Not Found", "Could not load the active season for your team."), ephemeral=True)
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
        tb = team.get("transfer_budget", 0)
        wb = team.get("wage_budget", 0)

        lines = []
        lines.append(f"**Team:** {team.get('team_name')} ({season.get('name')})")
        lines.append(f"**Manager:** <@{interaction.user.id}>")
        lines.append(f"**Table:** #{pos} • {pts} pts • GD {gd:+} • {rec} • Form {form}")
        lines.append(f"**Budgets:** Transfer {_money(tb)} • Wage {_money(wb)}/wk")
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
        season = team.get("_season") or await self.bot.season_service.get_season(str(team.get("season_id")))
        if not season:
            await interaction.followup.send(embed=error_embed("Season Not Found", "Active season not found."), ephemeral=True)
            return
        team_id = int(team.get("team_api_id"))
        season_year = int(season.get("season_year") or 2026)
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
        cur = await self.bot.db.execute(
            """INSERT INTO training_sessions(season_id,guild_id,team_api_id,team_name,user_id,status,starts_at,completes_at,assignments_json,result_json,created_at,updated_at)
               VALUES(?,?,?,?,?, 'running', ?, ?, ?, ?, ?, ?)""",
            (season_id, interaction.guild_id, team_id, team.get("team_name"), interaction.user.id, now.isoformat(), completes.isoformat(), json.dumps(assignments), json.dumps({}), utc_now_iso(), utc_now_iso()),
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

    @app_commands.command(name="requestfunds", description="Request transfer funds from the board (board meeting)")
    @app_commands.describe(amount="Amount requested (e.g. 50000000)", reason="Why you need the funds")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def requestfunds(self, interaction: discord.Interaction, amount: int, reason: str, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        await self._handle_board_request(interaction, kind="funds", amount=amount, reason=reason, league_key=league.value if league else None)

    @app_commands.command(name="requestwage", description="Request wage budget increase from the board (board meeting)")
    @app_commands.describe(amount="Weekly wage amount requested (e.g. 250000)", reason="Why you need the wage increase")
    @app_commands.choices(league=LEAGUE_CHOICES)
    async def requestwage(self, interaction: discord.Interaction, amount: int, reason: str, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        await self._handle_board_request(interaction, kind="wage", amount=amount, reason=reason, league_key=league.value if league else None)

    async def _handle_board_request(self, interaction: discord.Interaction, *, kind: str, amount: int, reason: str, league_key: str | None):
        team = await self._get_manager_team(interaction, league_key=league_key)
        if not team:
            await interaction.followup.send(embed=error_embed("No Claimed Team", "Claim a team first with `/claim`."), ephemeral=True)
            return
        amount = int(amount)
        if amount <= 0:
            await interaction.followup.send(embed=error_embed("Invalid Amount", "Amount must be greater than 0."), ephemeral=True)
            return
        reason = (reason or "").strip()
        if len(reason) < 10:
            await interaction.followup.send(embed=error_embed("Reason Required", "Please provide a valid detailed reason (at least 10 characters)."), ephemeral=True)
            return

        season = team.get("_season") or await self.bot.season_service.get_season(str(team.get("season_id")))
        season_id = str((season or {}).get("id") or team.get("season_id"))
        team_id = int(team.get("team_api_id"))
        team_name = str(team.get("team_name"))
        current_transfer = int(team.get("transfer_budget") or 0)
        current_wage = int(team.get("wage_budget") or 0)

        # Board evaluation - fair, detailed
        rng = random.Random(f"{season_id}:{team_id}:{kind}:{amount}:{reason}:{utc_now_iso()[:10]}")
        # reason quality heuristic
        reason_score = min(1.0, len(reason) / 120.0) + (0.15 if any(w in reason.lower() for w in ["injur", "depth", "compete", "objective", "relegation", "title", "europe", "squad", "develop", "project", "sales", "performance"]) else 0.0)
        reason_score = max(0.0, min(1.3, reason_score))
        if kind == "funds":
            baseline = 40_000_000
            ratio = amount / max(1, baseline)
            base_chance = 0.68 - max(0.0, ratio - 0.5) * 0.35
            budget_factor = 0.05 if current_transfer < 60_000_000 else -0.05
        else:
            baseline = 250_000
            ratio = amount / max(1, baseline)
            base_chance = 0.65 - max(0.0, ratio - 0.5) * 0.35
            budget_factor = 0.05 if current_wage < 1_800_000 else -0.05
        chance = max(0.08, min(0.92, base_chance + (reason_score - 0.6) * 0.35 + budget_factor))
        approved = rng.random() < chance

        # store request
        meta = {
            "chance": chance,
            "reason_score": reason_score,
            "current_transfer_budget": current_transfer,
            "current_wage_budget": current_wage,
        }
        status = "approved" if approved else "rejected"
        await self.bot.db.execute(
            """INSERT INTO board_requests(season_id,guild_id,channel_id,user_id,team_api_id,team_name,kind,amount,reason,status,meta_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,?)""",
            (season_id, interaction.guild_id, interaction.channel_id, interaction.user.id, team_id, team_name, kind, int(amount), reason, status, json.dumps(meta), utc_now_iso(), utc_now_iso()),
        )

        board_lines = []
        board_lines.append(f"**Board Meeting — {team_name}**")
        board_lines.append(f"Manager: <@{interaction.user.id}>")
        board_lines.append(f"Request: {'Transfer funds' if kind=='funds' else 'Weekly wage budget increase'} of **{_money(amount)}**")
        board_lines.append(f"Reason provided: {reason}")
        board_lines.append("")
        board_lines.append("**Board discussion**")
        if kind == "funds":
            board_lines.append(f"- Current transfer budget: {_money(current_transfer)}")
            board_lines.append(f"- Requested increase: {_money(amount)}")
            board_lines.append("- The board assessed squad need, financial risk, and the justification you provided.")
        else:
            board_lines.append(f"- Current wage budget: {_money(current_wage)}/wk")
            board_lines.append(f"- Requested increase: {_money(amount)}/wk")
            board_lines.append("- The board assessed wage structure, long-term sustainability, and the justification you provided.")

        if approved:
            if kind == "funds":
                await self.bot.db.execute(
                    "UPDATE season_teams SET transfer_budget=COALESCE(transfer_budget,0)+?, updated_at=? WHERE season_id=? AND team_api_id=?",
                    (int(amount), utc_now_iso(), season_id, team_id),
                )
                new_val = current_transfer + int(amount)
                board_lines.append("")
                board_lines.append("**Decision: APPROVED ✅**")
                board_lines.append(f"The board approved your request. Transfer budget updated: {_money(current_transfer)} → **{_money(new_val)}**.")
            else:
                await self.bot.db.execute(
                    "UPDATE season_teams SET wage_budget=COALESCE(wage_budget,0)+?, updated_at=? WHERE season_id=? AND team_api_id=?",
                    (int(amount), utc_now_iso(), season_id, team_id),
                )
                new_val = current_wage + int(amount)
                board_lines.append("")
                board_lines.append("**Decision: APPROVED ✅**")
                board_lines.append(f"The board approved your request. Wage budget updated: {_money(current_wage)}/wk → **{_money(new_val)}/wk.")
        else:
            board_lines.append("")
            board_lines.append("**Decision: REJECTED ❌**")
            # detailed reason
            if ratio > 1.5:
                board_lines.append(f"- The requested amount is too high relative to a realistic {'transfer' if kind=='funds' else 'wage'} adjustment at this stage ({_money(amount)} requested).")
            if reason_score < 0.75:
                board_lines.append("- The justification was not detailed enough. The board wants clearer footballing and financial reasoning.")
            if kind == "funds" and current_transfer > 80_000_000:
                board_lines.append(f"- The club already has a healthy transfer budget ({_money(current_transfer)}), so the board sees no urgent need for extra funds.")
            if kind == "wage" and current_wage > 2_500_000:
                board_lines.append(f"- The current wage structure is already strong ({_money(current_wage)}/wk), and the board is cautious about increasing wage commitments.")
            if len([l for l in board_lines if l.startswith("- The")]) == 0:
                board_lines.append("- The board felt the timing and risk profile of this request did not justify approval. Try a smaller request or a stronger football case.")

        embed = success_embed("Board Meeting Result", "\n".join(board_lines)[:3900]) if approved else error_embed("Board Meeting Result", "\n".join(board_lines)[:3900])
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ClubCog(bot))
