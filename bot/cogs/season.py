from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import error_embed, info_embed
try:
    from bot.ui.embeds import success_embed
except Exception:  # pragma: no cover - fallback for partial deploys
    success_embed = info_embed
from data.football_data import list_league_keys, LEAGUE_DISPLAY_NAMES
from services.season_service import LEAGUE_DISPLAY, LEAGUE_KEYS, EUROPEAN_COMPETITION_KEYS

logger = logging.getLogger(__name__)


def _league_choices() -> list[app_commands.Choice[str]]:
    # show top 5
    choices = []
    for k in LEAGUE_KEYS:
        name = LEAGUE_DISPLAY.get(k) or LEAGUE_DISPLAY_NAMES.get(k, k)
        choices.append(app_commands.Choice(name=name, value=k))
    return choices


def _euro_choices() -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name="Champions League", value="champions_league"),
        app_commands.Choice(name="Europa League", value="europa_league"),
        app_commands.Choice(name="Conference League", value="conference_league"),
    ]


def _fmt_standings(rows: list[dict[str, Any]], limit: int = 20) -> str:
    lines = [
        "`Pos  Team                     P   W  D  L  GF  GA  GD  Pts  Form `",
        "`---  ----------------------- --- -- -- -- --- --- --- ---- -----`",
    ]
    for r in rows[:limit]:
        rank = int(r.get("rank", 0))
        name = str(r.get("team_name", "Team"))[:23]
        mp = int(r.get("played", 0)); w = int(r.get("won", 0)); d = int(r.get("drawn", 0)); l = int(r.get("lost", 0))
        gf = int(r.get("goals_for", 0)); ga = int(r.get("goals_against", 0)); gd = int(r.get("goal_difference", 0))
        pts = int(r.get("points", 0)); form = str(r.get("form", ""))[-5:]
        lines.append(f"`{rank:>2}.  {name:<23} {mp:>3} {w:>2} {d:>2} {l:>2} {gf:>3} {ga:>3} {gd:>+3} {pts:>4}  {form:<5}`")
    legend = (
        "\n**Legend:** Pos = Position • P = Played • W = Won • D = Draw • L = Lost • "
        "GF = Goals For • GA = Goals Against • GD = Goal Difference (GF−GA) • Pts = Points (W=3, D=1, L=0) • Form = Last 5"
    )
    return "\n".join(lines) + legend


def _fmt_fixtures(rows: list[dict[str, Any]], limit: int = 20) -> str:
    lines = []
    for r in rows[:limit]:
        fid = r.get("id")
        md = r.get("matchday")
        hs = r.get("home_score")
        a_s = r.get("away_score")
        status = r.get("status")
        home = r.get("home_team_name")
        away = r.get("away_team_name")
        if hs is None or a_s is None:
            score = "vs"
        else:
            score = f"{hs}-{a_s}"
        tag = "✅" if status == "played" else ("🟢" if status == "live" else "🕒")
        lines.append(f"{tag} `MD{md:>2}` **{home}** {score} **{away}**  _(id:{fid}, {status})_")
    return "\n".join(lines) if lines else "No fixtures found."


class ResignConfirmView(discord.ui.View):
    def __init__(self, *, bot, user_id: int, guild_id: int | None, team_label: str, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.user_id = int(user_id)
        self.guild_id = guild_id
        self.team_label = team_label
        self.resolved = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != int(self.user_id):
            await interaction.response.send_message("Only the requesting manager can confirm this resignation.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Resign", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message("This resignation has already been handled.", ephemeral=True)
            return
        self.resolved = True
        for child in self.children:
            child.disabled = True
        try:
            res = await self.bot.season_service.resign_manager(guild_id=self.guild_id, user_id=self.user_id)
        except Exception:
            logger.exception("resign_manager failed for user_id=%s guild_id=%s", self.user_id, self.guild_id)
            await interaction.response.edit_message(content=None, embed=error_embed("Resign Failed", "I couldn't complete the resignation. Please try again."), view=self)
            return

        if not res.get("ok"):
            reason = str(res.get("reason") or "unknown")
            if reason == "no_claimed_team":
                msg = "You do not currently manage a claimed team."
            else:
                msg = f"Resignation could not be completed ({reason})."
            await interaction.response.edit_message(content=None, embed=error_embed("Resign Failed", msg), view=self)
            return

        released = res.get("released") or []
        if released:
            lines = []
            for r in released[:10]:
                tname = r.get("team_name") or f"Team {r.get('team_api_id')}"
                lk = r.get("league_key")
                league_name = LEAGUE_DISPLAY.get(str(lk), str(lk or "League"))
                lines.append(f"- **{tname}** ({league_name})")
            detail = "You have resigned from:\n" + "\n".join(lines)
        else:
            detail = f"You have resigned from **{self.team_label}**."
        detail += "\n\nThe club is now vacant. You can claim another available team with `/claim` when you're ready."
        await interaction.response.edit_message(content=None, embed=success_embed("Resignation Confirmed", detail[:3900]), view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message("This resignation has already been handled.", ephemeral=True)
            return
        self.resolved = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=None, embed=info_embed("Resignation Cancelled", f"You remain manager of **{self.team_label}**."), view=self)

    async def on_timeout(self):
        if self.resolved:
            return
        self.resolved = True
        for child in self.children:
            child.disabled = True


class SeasonGroup(app_commands.Group):
    def __init__(self, cog: "SeasonCog"):
        super().__init__(name="season", description="Season mode: fixtures, standings, matchday progression")
        self.cog = cog

    @app_commands.command(name="create", description="Create a new league season (generates fixtures and standings)")
    @app_commands.describe(league="League to create season for", season_year="Start year (e.g. 2026 for 2026/27)", name="Optional season name")
    @app_commands.choices(league=_league_choices())
    async def create(self, interaction: discord.Interaction, league: app_commands.Choice[str], season_year: int = 2026, name: str | None = None):
        await interaction.response.defer(ephemeral=True)
        try:
            season = await self.cog.bot.season_service.create_season(
                guild_id=interaction.guild_id,
                league_key=league.value,
                season_year=int(season_year),
                name=name,
            )
            await interaction.followup.send(embed=success_embed(
                "Season Created",
                f"**{season.get('name')}** (`{season.get('id')}`)\n"
                f"League: {LEAGUE_DISPLAY.get(season.get('league_key'), season.get('league_key'))}\n"
                f"Matchdays: {season.get('total_matchdays')}\n"
                f"Current matchday: {season.get('current_matchday')}\n\n"
                f"Use `/season fixtures`, `/season table`, `/season advance`, and `/season play`."
            ), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=error_embed("Season Create Failed", str(e)), ephemeral=True)

    @app_commands.command(name="info", description="Show active season info")
    @app_commands.describe(league="League (optional)")
    @app_commands.choices(league=_league_choices())
    async def info(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        season = await self.cog.bot.season_service.get_active_season(interaction.guild_id, league_key=league_key)
        if not season:
            await interaction.followup.send(embed=error_embed("No Season", "No active season found. Create one with `/season create`."), ephemeral=True)
            return
        sid = season["id"]
        standings = await self.cog.bot.season_service.get_standings(sid)
        fixtures = await self.cog.bot.season_service.get_fixtures(sid, matchday=int(season.get("current_matchday", 1)))
        played = sum(1 for f in fixtures if f.get("status") == "played")
        await interaction.followup.send(embed=info_embed(
            f"Season Info — {season.get('name')}",
            f"ID: `{sid}`\n"
            f"League: {LEAGUE_DISPLAY.get(season.get('league_key'), season.get('league_key'))}\n"
            f"Status: {season.get('status')}\n"
            f"Matchday: {season.get('current_matchday')}/{season.get('total_matchdays')}\n"
            f"Current matchday fixtures played: {played}/{len(fixtures)}\n"
            f"Teams: {len(standings)}"
        ), ephemeral=True)

    @app_commands.command(name="table", description="Show season standings/table")
    @app_commands.describe(league="League (optional)")
    @app_commands.choices(league=_league_choices())
    async def table(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        season = await self.cog.bot.season_service.get_active_season(interaction.guild_id, league_key=league_key)
        if not season:
            await interaction.followup.send(embed=error_embed("No Season", "No active season found."), ephemeral=True)
            return
        rows = await self.cog.bot.season_service.get_standings(season["id"])
        txt = _fmt_standings(rows, limit=20)
        await interaction.followup.send(embed=info_embed(f"Table — {season.get('name')}", txt[:3900]), ephemeral=True)

    @app_commands.command(name="fixtures", description="Show season fixtures for a matchday")
    @app_commands.describe(league="League (optional)", matchday="Matchday number (default: current)")
    @app_commands.choices(league=_league_choices())
    async def fixtures(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None, matchday: int | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        season = await self.cog.bot.season_service.get_active_season(interaction.guild_id, league_key=league_key)
        if not season:
            await interaction.followup.send(embed=error_embed("No Season", "No active season found."), ephemeral=True)
            return
        md = int(matchday) if matchday is not None else int(season.get("current_matchday", 1))
        rows = await self.cog.bot.season_service.get_fixtures(season["id"], matchday=md)
        txt = _fmt_fixtures(rows, limit=25)
        await interaction.followup.send(embed=info_embed(f"Fixtures — {season.get('name')} (MD {md})", txt[:3900]), ephemeral=True)

    @app_commands.command(name="advance", description="Quick-sim up to 5 remaining fixtures in current matchday, then advance if complete")
    @app_commands.describe(league="League (optional)", count="How many matches to sim now (1-5, default 5)")
    @app_commands.choices(league=_league_choices())
    async def advance(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None, count: app_commands.Range[int, 1, 5] = 5):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        ss = self.cog.bot.season_service
        season = await ss.get_active_season(interaction.guild_id, league_key=league_key)
        if not season:
            await interaction.followup.send(embed=error_embed("No Season", "No active season found."), ephemeral=True)
            return
        sid = season["id"]
        md = int(season.get("current_matchday", 1))
        fixtures = await ss.get_fixtures(sid, matchday=md)
        pending = [f for f in fixtures if f.get("status") not in ("played", "cancelled") and int(f.get("finalized") or 0) == 0]
        live = [f for f in pending if f.get("status") == "live"]
        if live:
            await interaction.followup.send(embed=error_embed("Matchday Has Live Fixture", f"Matchday {md} has {len(live)} live fixture(s). Finish them before advancing."), ephemeral=True)
            return
        # quick-sim up to `count` pending fixtures (batch sim, max 5 at a time)
        to_sim = pending[: int(count)]
        simmed = []
        for fx in to_sim:
            try:
                res = await ss.quick_sim_fixture(season_id=sid, fixture_id=int(fx["id"]), guild_id=interaction.guild_id, channel_id=interaction.channel_id)
                simmed.append(res)
            except Exception as e:
                logger.exception("Failed to quick-sim fixture %s", fx.get("id"))
                simmed.append({"ok": False, "fixture_id": fx.get("id"), "reason": str(e)})

        # advance if complete
        adv = await ss.advance_matchday_if_complete(sid)
        season2 = await ss.get_season(sid)
        played_lines = []
        fixtures2 = await ss.get_fixtures(sid, matchday=md)
        for fx in fixtures2[:12]:
            hs = fx.get("home_score"); as_ = fx.get("away_score")
            score = f"{hs}-{as_}" if hs is not None and as_ is not None else "vs"
            played_lines.append(f"**{fx.get('home_team_name')}** {score} **{fx.get('away_team_name')}**")
        remaining_after = [f for f in fixtures2 if f.get("status") not in ("played", "cancelled") and int(f.get("finalized") or 0) == 0]
        msg = f"**{(season2 or season).get('name')} — Matchday {md}**\nSimmed now: **{len(simmed)}** (max 5 per run)\n\n" + "\n".join(played_lines)
        if adv.get("advanced"):
            msg += f"\n\n✅ Advanced to **Matchday {adv.get('to_matchday')}**."
        elif adv.get("reason") == "season_completed":
            msg += "\n\n🏁 **Season completed.**"
        else:
            msg += f"\n\nRemaining this matchday: **{len(remaining_after)}**. Run `/season advance` again to sim the next batch."
        await interaction.followup.send(embed=success_embed("Season Advanced", msg[:3900]), ephemeral=True)

    @app_commands.command(name="play", description="Play next pending season fixture in live sim (bot vs bot by default)")
    @app_commands.describe(league="League (optional)", fixture_id="Fixture ID (optional; defaults to next pending in current matchday)")
    @app_commands.choices(league=_league_choices())
    async def play(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None, fixture_id: int | None = None):
        await interaction.response.defer(ephemeral=False)
        league_key = league.value if league else None
        ss = self.cog.bot.season_service
        season = await ss.get_active_season(interaction.guild_id, league_key=league_key)
        if not season:
            await interaction.followup.send(embed=error_embed("No Season", "No active season found."), ephemeral=True)
            return
        sid = season["id"]
        md = int(season.get("current_matchday", 1))
        fx = None
        if fixture_id is not None:
            fx = await ss.get_fixture(int(fixture_id))
            if not fx or str(fx.get("season_id")) != str(sid):
                await interaction.followup.send(embed=error_embed("Fixture Not Found", "Fixture not found in this season."), ephemeral=True)
                return
        else:
            rows = await ss.get_fixtures(sid, matchday=md, status="pending")
            rows = [r for r in rows if int(r.get("finalized") or 0) == 0]
            fx = rows[0] if rows else None
        if not fx:
            await interaction.followup.send(embed=error_embed("No Pending Fixture", "No pending fixture found for current matchday. Use `/season advance` if matchday is complete."), ephemeral=True)
            return
        if fx.get("status") == "live":
            await interaction.followup.send(embed=error_embed("Fixture Live", f"Fixture is already live (match `{fx.get('match_id')}`)."), ephemeral=True)
            return
        if fx.get("status") == "played" or int(fx.get("finalized") or 0) == 1:
            await interaction.followup.send(embed=error_embed("Fixture Played", "This fixture is already played."), ephemeral=True)
            return

        # Build match from fixture
        from data.football_data import get_team, get_team_players
        home = get_team(int(fx["home_team_api_id"]), fx.get("league_key"))
        away = get_team(int(fx["away_team_api_id"]), fx.get("league_key"))
        if not home or not away:
            await interaction.followup.send(embed=error_embed("Team Not Found", "Could not load teams for fixture."), ephemeral=True)
            return
        home_squad = get_team_players(int(fx["home_team_api_id"]), fx.get("league_key"))
        away_squad = get_team_players(int(fx["away_team_api_id"]), fx.get("league_key"))
        if not home_squad or not away_squad:
            await interaction.followup.send(embed=error_embed("Squad Not Found", "Could not load squads for fixture."), ephemeral=True)
            return

        # Detect claimed managers for this fixture
        managers = await ss.get_fixture_managers(sid, fx)
        hm_id = managers.get("home_manager_id")
        am_id = managers.get("away_manager_id")
        if hm_id is not None and am_id is not None and int(hm_id) == int(am_id):
            await interaction.followup.send(embed=error_embed("Invalid Managers", "Home and away teams are claimed by the same user."), ephemeral=True)
            return

        if hm_id and am_id:
            mode = "manager_vs_manager"
        elif hm_id or am_id:
            mode = "manager_vs_bot"
        else:
            mode = "bot_vs_bot"

        ms = self.cog.bot.match_service
        try:
            session = await ms.create_session(
                guild_id=interaction.guild_id, channel_id=interaction.channel_id, mode=mode,
                home_team=home, away_team=away, home_squad=home_squad, away_squad=away_squad,
                home_manager_id=hm_id, away_manager_id=am_id,
                season_id=sid, fixture_id=int(fx["id"]), league_key=fx.get("league_key"), competition_type="league",
            )
        except Exception as e:
            logger.exception("Failed to create season match session")
            await interaction.followup.send(embed=error_embed("Match Create Failed", str(e)), ephemeral=True)
            return

        await ss.mark_fixture_live(int(fx["id"]), session.state.id)
        session.state.submission["competition"] = LEAGUE_DISPLAY.get(fx.get("league_key"), "League")
        session.state.submission["match_type"] = "League"
        await session.save()

        sim_cog = self.cog.bot.get_cog("SimCog")
        if sim_cog is None:
            await interaction.followup.send(embed=error_embed("Sim Unavailable", "Sim cog not loaded; match created but not started."), ephemeral=True)
            return

        channel = interaction.channel
        if channel is None and session.state.channel_id:
            try:
                channel = await self.cog.bot.fetch_channel(session.state.channel_id)
            except Exception:
                channel = None
        if channel is None:
            await interaction.followup.send(embed=error_embed("Channel Error", "Could not resolve channel to start match flow."), ephemeral=True)
            return

        manager_ids = [uid for uid in (hm_id, am_id) if uid is not None]
        if manager_ids:
            # Manager setup flow (same as /sim manager flow)
            from datetime import datetime, timezone, timedelta
            from bot.cogs.sim import ManagerSetupView, build_match_setup_embed, _clamp

            now = datetime.now(timezone.utc)
            deadline = now + timedelta(seconds=int(session.state.submission.get("initial_deadline_seconds") or 1200))
            session.state.submission["deadline_at"] = deadline.isoformat()
            session.state.submission["match_started"] = False
            await session.save()

            view = ManagerSetupView(sim_cog, session, timeout=None)
            try:
                setup_msg = await channel.send(embed=build_match_setup_embed(session), view=view)
                view.message = setup_msg
                session.state.message_id = setup_msg.id
                await session.save()
            except Exception:
                logger.exception("Failed to send season manager setup embed")
                await interaction.followup.send(embed=error_embed("Setup Error", "Failed to post match setup embed in channel."), ephemeral=True)
                return

            deadline_ts = int(deadline.timestamp())
            title = "Manager vs Manager — Submissions Open" if len(manager_ids) > 1 else "Manager vs Bot — Submission Open"
            who_line = "Both managers have 20 minutes to submit their starting lineup and tactics." if len(manager_ids) > 1 else "You have 20 minutes to submit your starting lineup and tactics."
            notice = (
                f"**{title}**\n"
                f"{home.get('name')} vs {away.get('name')}\n"
                f"Season: {season.get('name')} | Matchday {fx.get('matchday')} | Fixture `{fx.get('id')}`\n"
                f"{who_line}\n"
                f"Deadline: <t:{deadline_ts}:R> (<t:{deadline_ts}:T>)\n"
                f"Please select your starting XI, choose your bench and submit your tactics before the deadline."
            )
            for uid in manager_ids:
                try:
                    user = self.cog.bot.get_user(uid) or await self.cog.bot.fetch_user(uid)
                    await user.send(notice + f"\nMatch ID: `{session.state.id}`")
                except Exception:
                    pass
            try:
                mentions = " ".join(f"<@{uid}>" for uid in manager_ids)
                prefix = (mentions + " ") if mentions else ""
                await channel.send(_clamp(prefix + notice, 1900))
            except Exception:
                pass

            try:
                await interaction.followup.send(embed=success_embed(
                    "Season Match Setup",
                    f"**{home.get('name')}** vs **{away.get('name')}**\n"
                    f"Season: {season.get('name')} | Matchday {fx.get('matchday')} | Fixture `{fx.get('id')}`\n"
                    f"Mode: {mode.replace('_', ' ').title()} | Match ID: `{session.state.id}`\n"
                    f"Managers: {', '.join(f'<@{uid}>' for uid in manager_ids)}"
                ), ephemeral=False)
            except Exception:
                pass

            sim_cog._start_deadline_watcher(session, view)
            return

        # Bot vs bot
        await interaction.followup.send(embed=success_embed(
            "Season Match Starting",
            f"**{home.get('name')}** vs **{away.get('name')}**\n"
            f"Season: {season.get('name')} | Matchday {fx.get('matchday')} | Fixture `{fx.get('id')}`\n"
            f"Match ID: `{session.state.id}`"
        ))

        class _Shim:
            channel = interaction.channel
        shim = _Shim()
        if shim.channel is None:
            try:
                shim.channel = await self.cog.bot.fetch_channel(session.state.channel_id) if session.state.channel_id else None
            except Exception:
                shim.channel = None
        if shim.channel is not None:
            asyncio.create_task(sim_cog._run_match_flow(shim, session))
        else:
            await interaction.followup.send(embed=error_embed("Channel Error", "Could not resolve channel to start match flow."), ephemeral=True)

    @app_commands.command(name="scorers", description="Show season top scorers")
    @app_commands.describe(league="League (optional)")
    @app_commands.choices(league=_league_choices())
    async def scorers(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        ss = self.cog.bot.season_service
        season = await ss.get_active_season(interaction.guild_id, league_key=league_key)
        if not season:
            await interaction.followup.send(embed=error_embed("No Season", "No active season found."), ephemeral=True)
            return
        rows = await ss.get_top_scorers(season["id"], limit=15)
        if not rows:
            await interaction.followup.send(embed=info_embed(f"Top Scorers — {season.get('name')}", "No goals recorded yet."), ephemeral=True)
            return
        lines = []
        for i, r in enumerate(rows, start=1):
            lines.append(f"`{i:>2}.` **{r.get('player_name','?')}** ({r.get('team_name','?')}) — {int(r.get('goals',0))}G {int(r.get('assists',0))}A, {float(r.get('avg_rating',0.0)):.2f} avg")
        await interaction.followup.send(embed=info_embed(f"Top Scorers — {season.get('name')}", "\n".join(lines)[:3900]), ephemeral=True)

    @app_commands.command(name="assisters", description="Show season top assisters")
    @app_commands.describe(league="League (optional)")
    @app_commands.choices(league=_league_choices())
    async def assisters(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        ss = self.cog.bot.season_service
        season = await ss.get_active_season(interaction.guild_id, league_key=league_key)
        if not season:
            await interaction.followup.send(embed=error_embed("No Season", "No active season found."), ephemeral=True)
            return
        rows = await ss.get_top_assisters(season["id"], limit=15)
        if not rows:
            await interaction.followup.send(embed=info_embed(f"Top Assisters — {season.get('name')}", "No assists recorded yet."), ephemeral=True)
            return
        lines = []
        for i, r in enumerate(rows, start=1):
            lines.append(f"`{i:>2}.` **{r.get('player_name','?')}** ({r.get('team_name','?')}) — {int(r.get('assists',0))}A {int(r.get('goals',0))}G, {float(r.get('avg_rating',0.0)):.2f} avg")
        await interaction.followup.send(embed=info_embed(f"Top Assisters — {season.get('name')}", "\n".join(lines)[:3900]), ephemeral=True)


    async def _get_or_create_euro_season(self, interaction: discord.Interaction, competition: str, season_year: int = 2026):
        ss = self.cog.bot.season_service
        season = await ss.get_active_season(interaction.guild_id, league_key=competition)
        if season:
            return season, False
        season = await ss.create_season(guild_id=interaction.guild_id, league_key=competition, season_year=int(season_year))
        return season, True

    @app_commands.command(name="europots", description="Generate or view European competition pots (UCL/UEL/UECL)")
    @app_commands.describe(competition="European competition", season_year="Season start year (used if creating)")
    @app_commands.choices(competition=_euro_choices())
    async def europots(self, interaction: discord.Interaction, competition: app_commands.Choice[str], season_year: int = 2026):
        await interaction.response.defer(ephemeral=True)
        ss = self.cog.bot.season_service
        try:
            season, created = await self._get_or_create_euro_season(interaction, competition.value, season_year)
            sid = str(season["id"])
            # If pots already exist, show them; else generate
            existing = await ss.get_european_pots(sid, competition.value)
            pots = existing.get("pots") or []
            has_pots = any(len(p) > 0 for p in pots)
            if not has_pots:
                res = await ss.generate_european_pots(season_id=sid, competition_key=competition.value, season_year=int(season.get("season_year") or season_year), guild_id=interaction.guild_id)
                if not res.get("ok"):
                    reason = res.get("reason", "unknown")
                    if reason == "insufficient_teams":
                        await interaction.followup.send(embed=error_embed("Not Enough Teams", f"Found {res.get('found',0)}/{res.get('needed',0)} eligible teams for {competition.name}. Check that teams exist in the database."), ephemeral=True)
                        return
                    await interaction.followup.send(embed=error_embed("Pot Generation Failed", str(reason)), ephemeral=True)
                    return
                pots = res.get("pots") or []
            lines = [f"**{competition.name} — Pots**", f"Season: {season.get('name')} (`{sid}`)"]
            if created:
                lines.append("_Season created automatically._")
            for i, pot in enumerate(pots, start=1):
                lines.append(f"\n**Pot {i}**")
                for t in pot:
                    dom = LEAGUE_DISPLAY.get(str(t.get('domestic_league_key') or ''), t.get('domestic_league_key') or '?')
                    rating = t.get('seed_rating')
                    rating_txt = f" • {float(rating):.1f}" if rating is not None else ""
                    lines.append(f"- {t.get('team_name')} ({dom}{rating_txt})")
            await interaction.followup.send(embed=info_embed(f"{competition.name} Pots", "\n".join(lines)[:3900]), ephemeral=True)
        except Exception as e:
            logger.exception("europots failed")
            await interaction.followup.send(embed=error_embed("Pot Generation Failed", str(e)), ephemeral=True)

    @app_commands.command(name="eurodraw", description="Draw European competition groups from pots")
    @app_commands.describe(competition="European competition", season_year="Season start year (used if creating)")
    @app_commands.choices(competition=_euro_choices())
    async def eurodraw(self, interaction: discord.Interaction, competition: app_commands.Choice[str], season_year: int = 2026):
        await interaction.response.defer(ephemeral=True)
        ss = self.cog.bot.season_service
        try:
            season, _ = await self._get_or_create_euro_season(interaction, competition.value, season_year)
            sid = str(season["id"])
            # Ensure pots exist
            pot_res = await ss.get_european_pots(sid, competition.value)
            pots = pot_res.get("pots") or []
            if not any(len(p) > 0 for p in pots):
                gen = await ss.generate_european_pots(season_id=sid, competition_key=competition.value, season_year=int(season.get("season_year") or season_year), guild_id=interaction.guild_id)
                if not gen.get("ok"):
                    await interaction.followup.send(embed=error_embed("Draw Failed", f"Could not generate pots: {gen.get('reason','unknown')}"), ephemeral=True)
                    return
            res = await ss.draw_european_groups(season_id=sid, competition_key=competition.value)
            if not res.get("ok"):
                await interaction.followup.send(embed=error_embed("Draw Failed", str(res.get("detail") or res.get("reason") or "unknown")), ephemeral=True)
                return
            groups = res.get("groups") or []
            lines = [f"**{competition.name} — Group Draw**", f"Season: {season.get('name')} (`{sid}`)"]
            for g in groups:
                lines.append(f"\n**Group {g.get('group_name')}**")
                for t in g.get("teams") or []:
                    dom = LEAGUE_DISPLAY.get(str(t.get('domestic_league_key') or ''), t.get('domestic_league_key') or '?')
                    pot = t.get('pot')
                    lines.append(f"- Pot {pot}: {t.get('team_name')} ({dom})")
            await interaction.followup.send(embed=info_embed(f"{competition.name} Group Draw", "\n".join(lines)[:3900]), ephemeral=True)
        except Exception as e:
            logger.exception("eurodraw failed")
            await interaction.followup.send(embed=error_embed("Draw Failed", str(e)), ephemeral=True)

    @app_commands.command(name="eurofixtures", description="Generate European group-stage fixtures from drawn groups")
    @app_commands.describe(competition="European competition", season_year="Season start year (used if creating)", regenerate="Replace existing unplayed fixtures")
    @app_commands.choices(competition=_euro_choices())
    async def eurofixtures(self, interaction: discord.Interaction, competition: app_commands.Choice[str], season_year: int = 2026, regenerate: bool = False):
        await interaction.response.defer(ephemeral=True)
        ss = self.cog.bot.season_service
        try:
            season, _ = await self._get_or_create_euro_season(interaction, competition.value, season_year)
            sid = str(season["id"])
            # Ensure pots + groups exist
            pot_res = await ss.get_european_pots(sid, competition.value)
            pots = pot_res.get("pots") or []
            if not any(len(p) > 0 for p in pots):
                gen = await ss.generate_european_pots(season_id=sid, competition_key=competition.value, season_year=int(season.get("season_year") or season_year), guild_id=interaction.guild_id)
                if not gen.get("ok"):
                    await interaction.followup.send(embed=error_embed("Fixture Generation Failed", f"Could not generate pots: {gen.get('reason','unknown')}"), ephemeral=True)
                    return
            groups_res = await ss.get_european_groups(sid, competition.value)
            if not (groups_res.get("groups") or []):
                draw = await ss.draw_european_groups(season_id=sid, competition_key=competition.value)
                if not draw.get("ok"):
                    await interaction.followup.send(embed=error_embed("Fixture Generation Failed", f"Could not draw groups: {draw.get('detail') or draw.get('reason','unknown')}"), ephemeral=True)
                    return
            res = await ss.generate_european_group_stage_fixtures(season_id=sid, competition_key=competition.value, regenerate=bool(regenerate))
            if not res.get("ok"):
                reason = str(res.get("reason") or "unknown")
                detail = str(res.get("detail") or reason)
                await interaction.followup.send(embed=error_embed("Fixture Generation Failed", detail), ephemeral=True)
                return
            await interaction.followup.send(embed=success_embed(
                f"{competition.name} Fixtures Generated",
                f"Season: {season.get('name')} (`{sid}`)\n"
                f"Groups: {res.get('groups')}\n"
                f"Matchdays: {res.get('matchdays')}\n"
                f"Fixtures created: {res.get('fixtures_created')}\n\n"
                f"Use `/season euroshow competition:{competition.name}` to inspect and `/season eurovalidate` to validate."
            ), ephemeral=True)
        except Exception as e:
            logger.exception("eurofixtures failed")
            await interaction.followup.send(embed=error_embed("Fixture Generation Failed", str(e)), ephemeral=True)

    @app_commands.command(name="euroshow", description="Show European competition pots, groups, or fixtures")
    @app_commands.describe(competition="European competition", view="What to show", matchday="Matchday (for fixtures view)")
    @app_commands.choices(competition=_euro_choices(), view=[
        app_commands.Choice(name="Pots", value="pots"),
        app_commands.Choice(name="Groups", value="groups"),
        app_commands.Choice(name="Fixtures", value="fixtures"),
    ])
    async def euroshow(self, interaction: discord.Interaction, competition: app_commands.Choice[str], view: app_commands.Choice[str], matchday: int | None = None):
        await interaction.response.defer(ephemeral=True)
        ss = self.cog.bot.season_service
        season = await ss.get_active_season(interaction.guild_id, league_key=competition.value)
        if not season:
            await interaction.followup.send(embed=error_embed("No Competition Season", f"No active {competition.name} season found. Run `/season europots` first."), ephemeral=True)
            return
        sid = str(season["id"])
        try:
            if view.value == "pots":
                res = await ss.get_european_pots(sid, competition.value)
                pots = res.get("pots") or []
                if not any(len(p) > 0 for p in pots):
                    await interaction.followup.send(embed=info_embed(f"{competition.name} Pots", "No pots generated yet. Use `/season europots`."), ephemeral=True)
                    return
                lines = [f"Season: {season.get('name')} (`{sid}`)"]
                for i, pot in enumerate(pots, start=1):
                    lines.append(f"\n**Pot {i}**")
                    for t in pot:
                        dom = LEAGUE_DISPLAY.get(str(t.get('domestic_league_key') or ''), t.get('domestic_league_key') or '?')
                        lines.append(f"- {t.get('team_name')} ({dom})")
                await interaction.followup.send(embed=info_embed(f"{competition.name} Pots", "\n".join(lines)[:3900]), ephemeral=True)
                return
            if view.value == "groups":
                res = await ss.get_european_groups(sid, competition.value)
                groups = res.get("groups") or []
                if not groups:
                    await interaction.followup.send(embed=info_embed(f"{competition.name} Groups", "No groups drawn yet. Use `/season eurodraw`."), ephemeral=True)
                    return
                lines = [f"Season: {season.get('name')} (`{sid}`)"]
                for g in groups:
                    lines.append(f"\n**Group {g.get('group_name')}**")
                    for t in g.get("teams") or []:
                        dom = LEAGUE_DISPLAY.get(str(t.get('domestic_league_key') or ''), t.get('domestic_league_key') or '?')
                        pot = t.get('pot')
                        lines.append(f"- Pot {pot}: {t.get('team_name')} ({dom})")
                await interaction.followup.send(embed=info_embed(f"{competition.name} Groups", "\n".join(lines)[:3900]), ephemeral=True)
                return
            # fixtures
            md = int(matchday) if matchday is not None else None
            rows = await ss.get_fixtures(sid, matchday=md)
            if not rows:
                await interaction.followup.send(embed=info_embed(f"{competition.name} Fixtures", "No fixtures found. Use `/season eurofixtures`."), ephemeral=True)
                return
            lines = [f"Season: {season.get('name')} (`{sid}`)"]
            if md is not None:
                lines.append(f"Matchday: {md}")
            for r in rows[:40]:
                hs = r.get("home_score")
                as_ = r.get("away_score")
                score = f"{hs}-{as_}" if hs is not None and as_ is not None else "vs"
                meta = {}
                try:
                    meta = json.loads(r.get("meta_json") or "{}")
                except Exception:
                    meta = {}
                gname = meta.get("group") or "?"
                tag = "✅" if r.get("status") == "played" else ("🟢" if r.get("status") == "live" else "🕒")
                lines.append(f"{tag} `MD{int(r.get('matchday') or 0):>2}` G{gname} **{r.get('home_team_name')}** {score} **{r.get('away_team_name')}** _(id:{r.get('id')})_")
            await interaction.followup.send(embed=info_embed(f"{competition.name} Fixtures", "\n".join(lines)[:3900]), ephemeral=True)
        except Exception as e:
            logger.exception("euroshow failed")
            await interaction.followup.send(embed=error_embed("Show Failed", str(e)), ephemeral=True)

    @app_commands.command(name="eurovalidate", description="Validate European competition pots, groups, and fixtures")
    @app_commands.describe(competition="European competition")
    @app_commands.choices(competition=_euro_choices())
    async def eurovalidate(self, interaction: discord.Interaction, competition: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        ss = self.cog.bot.season_service
        season = await ss.get_active_season(interaction.guild_id, league_key=competition.value)
        if not season:
            await interaction.followup.send(embed=error_embed("No Competition Season", f"No active {competition.name} season found."), ephemeral=True)
            return
        sid = str(season["id"])
        try:
            res = await ss.validate_european_competition(sid, competition.value)
            counts = res.get("counts") or {}
            lines = [
                f"Season: {season.get('name')} (`{sid}`)",
                f"Entries: {counts.get('entries',0)} | Groups: {counts.get('groups',0)} | Fixtures: {counts.get('fixtures',0)}",
            ]
            issues = res.get("issues") or []
            warnings = res.get("warnings") or []
            if issues:
                lines.append("\n**Issues**")
                for it in issues[:20]:
                    lines.append(f"- {it}")
            if warnings:
                lines.append("\n**Warnings**")
                for it in warnings[:10]:
                    lines.append(f"- {it}")
            if not issues and not warnings:
                lines.append("\nNo issues found. Pots, groups, and fixtures look valid.")
            embed = success_embed(f"{competition.name} Validation", "\n".join(lines)[:3900]) if res.get("ok") else error_embed(f"{competition.name} Validation", "\n".join(lines)[:3900])
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.exception("eurovalidate failed")
            await interaction.followup.send(embed=error_embed("Validation Failed", str(e)), ephemeral=True)

class SeasonCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.season_group = SeasonGroup(self)
        try:
            self.bot.tree.add_command(self.season_group)
        except discord.app_commands.CommandAlreadyRegistered:
            # Reload/partial state: replace existing group so new code takes effect.
            try:
                self.bot.tree.remove_command(self.season_group.name, type=self.season_group.type)
            except Exception:
                pass
            self.bot.tree.add_command(self.season_group)

    async def cog_unload(self) -> None:
        try:
            self.bot.tree.remove_command(self.season_group.name, type=self.season_group.type)
        except Exception:
            pass

    @app_commands.command(name="topassisters", description="Show top assisters in the active season")
    @app_commands.describe(league="League (optional)")
    @app_commands.choices(league=_league_choices())
    async def topassisters(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        ss = self.bot.season_service
        season = await ss.get_active_season(interaction.guild_id, league_key=league_key)
        if not season:
            await interaction.followup.send(embed=error_embed("No Season", "No active season found."), ephemeral=True)
            return
        rows = await ss.get_top_assisters(season["id"], limit=15)
        if not rows:
            await interaction.followup.send(embed=info_embed(f"Top Assisters — {season.get('name')}", "No assists recorded yet."), ephemeral=True)
            return
        lines = []
        for i, r in enumerate(rows, start=1):
            lines.append(f"`{i:>2}.` **{r.get('player_name','?')}** ({r.get('team_name','?')}) — {int(r.get('assists',0))}A {int(r.get('goals',0))}G, {float(r.get('avg_rating',0.0)):.2f} avg")
        await interaction.followup.send(embed=info_embed(f"Top Assisters — {season.get('name')}", "\n".join(lines)[:3900]), ephemeral=True)

    @app_commands.command(name="resign", description="Resign from your currently claimed club/manager position")
    async def resign(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ss = self.bot.season_service
        try:
            team = await ss.get_user_team_in_active_season(interaction.guild_id, interaction.user.id)
        except Exception:
            logger.exception("resign: failed to resolve current team")
            await interaction.followup.send(embed=error_embed("Resign Failed", "I couldn't check your current manager position. Please try again."), ephemeral=True)
            return

        if not team:
            # Also check claim-only across guild-less context in case of DM/guild mismatch
            try:
                claim = await ss._get_user_team_claim(interaction.guild_id, interaction.user.id)
                if claim:
                    team = {
                        "team_name": claim.get("team_name"),
                        "team_api_id": claim.get("team_api_id"),
                        "league_key": claim.get("league_key"),
                    }
            except Exception:
                pass

        if not team:
            await interaction.followup.send(
                embed=error_embed("No Claimed Team", "You don't currently manage a claimed club. Use `/claim` to take charge of an available team."),
                ephemeral=True,
            )
            return

        team_name = str(team.get("team_name") or "your club")
        league_key = team.get("league_key") or team.get("season_league_key")
        league_name = LEAGUE_DISPLAY.get(str(league_key), str(league_key or "League"))
        team_label = f"{team_name} ({league_name})" if league_key else team_name

        view = ResignConfirmView(
            bot=self.bot,
            user_id=int(interaction.user.id),
            guild_id=interaction.guild_id,
            team_label=team_label,
        )
        await interaction.followup.send(
            embed=info_embed(
                "Confirm Resignation",
                f"You are about to resign as manager of **{team_label}**.\n\n"
                "This will remove your claim/manager assignment for that club and make it vacant. "
                "This action cannot be undone automatically.\n\n"
                "Press **Confirm Resign** to proceed, or **Cancel** to keep your position."
            ),
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="archive", description="Archive the current active season for a league (admin only)")
    @app_commands.describe(league="League season to archive (optional; uses current active season if omitted)")
    @app_commands.choices(league=_league_choices())
    async def archive(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        perms = interaction.user.guild_permissions if interaction.guild else None
        is_admin = bool(perms and (perms.administrator or perms.manage_guild))
        if not is_admin:
            await interaction.followup.send(embed=error_embed("Permission Denied", "Only server administrators can archive a season."), ephemeral=True)
            return

        league_key = league.value if league else None
        ss = self.bot.season_service
        season = await ss.get_active_season(interaction.guild_id, league_key=league_key)
        if not season:
            msg = "No active season found for that league." if league_key else "No active season found."
            await interaction.followup.send(embed=error_embed("No Active Season", msg), ephemeral=True)
            return

        try:
            archived = await ss.archive_season(str(season["id"]))
        except Exception as e:
            logger.exception("archive season failed")
            await interaction.followup.send(embed=error_embed("Archive Failed", str(e)), ephemeral=True)
            return

        if not archived:
            await interaction.followup.send(embed=error_embed("Archive Failed", "Season could not be archived."), ephemeral=True)
            return

        name = archived.get("name") or LEAGUE_DISPLAY.get(archived.get("league_key"), archived.get("league_key"))
        await interaction.followup.send(
            embed=success_embed(
                "Season Archived",
                f"Archived **{name}** (`{archived.get('id')}`).\n"
                f"League: {LEAGUE_DISPLAY.get(archived.get('league_key'), archived.get('league_key'))}\n"
                f"This season is no longer active and will not appear as the current selected league season."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="transfer", description="Transfer a player to a team (auto-finalized)")
    @app_commands.describe(player="Player name or ID", to_team="Destination team name", league="League for team search (optional)", note="Optional note")
    @app_commands.choices(league=_league_choices())
    async def transfer(self, interaction: discord.Interaction, player: str, to_team: str, league: app_commands.Choice[str] | None = None, note: str | None = None):
        await interaction.response.defer(ephemeral=True)
        try:
            fd = self.bot.football_data
            league_key = league.value if league else None
            # resolve player
            p_row = None
            q = (player or "").strip()
            if q.isdigit():
                # try DB lookup across seasons
                row = await self.bot.db.fetchone("SELECT * FROM players WHERE api_id=? ORDER BY season DESC, updated_at DESC LIMIT 1", (int(q),))
                p_row = dict(row) if row else None
            if p_row is None:
                players = await fd.search_players(q, limit=10)
                if players:
                    # prefer exact normalized name
                    ql = q.lower().strip()
                    exact = [p for p in players if str(p.get("name","")).lower().strip() == ql]
                    p_row = exact[0] if exact else players[0]
            if not p_row:
                await interaction.followup.send(embed=error_embed("Player Not Found", f"No player found for `{player}`."), ephemeral=True)
                return

            # resolve destination team
            teams = await fd.list_teams(query=to_team, league=league_key, limit=10)
            if not teams and league_key:
                teams = await fd.list_teams(query=to_team, league=None, limit=10)
            if not teams:
                await interaction.followup.send(embed=error_embed("Team Not Found", f"No team found for `{to_team}`."), ephemeral=True)
                return
            t = teams[0]

            # figure active season for guild if any
            season = await self.bot.season_service.get_active_season(interaction.guild_id, league_key=league_key)
            sid = season["id"] if season else None

            res = await self.bot.season_service.create_pending_transfer(
                season_id=sid,
                guild_id=interaction.guild_id,
                player_api_id=int(p_row.get("api_id")),
                to_team_api_id=int(t.get("api_id")),
                requested_by=interaction.user.id if interaction.user else None,
                note=note,
            )
            if not res.get("ok"):
                await interaction.followup.send(embed=error_embed("Transfer Failed", str(res.get("reason") or "unknown")), ephemeral=True)
                return
            tr = res.get("transfer") or {}
            # auto-finalize
            conf = await self.bot.season_service.confirm_transfer(int(tr.get("id")), confirmed_by=interaction.user.id if interaction.user else None)
            if not conf.get("ok"):
                reason = str(conf.get("reason") or "auto-finalize failed")
                if reason.startswith("active_match_blocks_transfer"):
                    await interaction.followup.send(embed=info_embed(
                        "Transfer Pending",
                        f"Transfer recorded (ID `{tr.get('id')}`) for **{p_row.get('name','?')}** to **{t.get('name','?')}**, but finalization is pending because an active match is blocking it ({reason.split(':',1)[1]}).\n\n"
                        f"The transfer has not been completed yet. Confirm it again once the match is no longer active."
                    ), ephemeral=True)
                    return
                await interaction.followup.send(embed=error_embed("Transfer Failed", reason), ephemeral=True)
                return
            await interaction.followup.send(embed=success_embed(
                "Transfer Completed",
                f"**{conf.get('player_name')}** moved from **{conf.get('from_team_name')}** to **{conf.get('to_team_name')}**.\n"
                f"Transfer ID: `{tr.get('id')}` | Status: confirmed (auto-finalized)"
            ), ephemeral=True)
        except Exception as e:
            logger.exception("transfer create failed")
            await interaction.followup.send(embed=error_embed("Transfer Failed", str(e)), ephemeral=True)

    @app_commands.command(name="claim", description="Claim a team (works even if no season is open)")
    @app_commands.describe(team="Team name (e.g. Liverpool)", league="League (optional)")
    @app_commands.choices(league=_league_choices())
    async def claim(self, interaction: discord.Interaction, team: str, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        ss = self.bot.season_service
        try:
            res = await ss.claim_team(guild_id=interaction.guild_id, user_id=interaction.user.id, team_query=team, league_key=league_key)
        except Exception as e:
            logger.exception("claim failed")
            await interaction.followup.send(embed=error_embed("Claim Failed", str(e)), ephemeral=True)
            return

        if not res.get("ok"):
            reason = str(res.get("reason") or "unknown")
            if reason == "team_not_found":
                await interaction.followup.send(embed=error_embed("Team Not Found", f"Could not find team `{team}`. Check the spelling, or provide a league."), ephemeral=True)
                return
            if reason == "already_claimed":
                owner = res.get("owner_user_id")
                t = res.get("team") or {}
                owner_txt = f"<@{owner}>" if owner else "another manager"
                await interaction.followup.send(embed=error_embed("Team Claimed", f"**{t.get('team_name', team)}** is already claimed by {owner_txt}."), ephemeral=True)
                return
            if reason == "user_already_has_team":
                cur = res.get("current_team") or {}
                await interaction.followup.send(embed=error_embed("Already Managing A Team", f"You already manage **{cur.get('team_name','a team')}** in this season. Release it first before claiming another."), ephemeral=True)
                return
            if reason == "ambiguous_team":
                cands = res.get("candidates") or []
                lines = [f"- **{c.get('team_name')}** ({c.get('season_name')})" for c in cands[:10]]
                await interaction.followup.send(embed=error_embed("Ambiguous Team", "Multiple teams matched. Be more specific:\n" + "\n".join(lines)), ephemeral=True)
                return
            await interaction.followup.send(embed=error_embed("Claim Failed", reason), ephemeral=True)
            return

        t = res.get("team") or {}
        season = res.get("season") or {}
        team_name = t.get("team_name") or team
        season_name = season.get("name") if isinstance(season, dict) else None
        if not season_name:
            lk = t.get("league_key") or t.get("season_league_key") or league_key
            try:
                from services.season_service import LEAGUE_DISPLAY as _LD
                league_name = _LD.get(str(lk), str(lk or "this league"))
            except Exception:
                league_name = str(lk or "this league")
            season_name = f"{league_name} (no season set)"
        if res.get("already_claimed_by_you"):
            await interaction.followup.send(embed=info_embed("Already Claimed", f"You already manage **{team_name}** in **{season_name}**."), ephemeral=True)
            return
        await interaction.followup.send(embed=success_embed("Team Claimed", f"{interaction.user.mention} is now the manager of **{team_name}** in **{season_name}**."), ephemeral=True)

    @app_commands.command(name="claimed", description="Show claimed teams and their managers")
    @app_commands.describe(league="League (optional)")
    @app_commands.choices(league=_league_choices())
    async def claimed(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        league_key = league.value if league else None
        ss = self.bot.season_service
        try:
            rows = await ss.list_claimed_teams(interaction.guild_id, league_key=league_key)
        except Exception as e:
            logger.exception("claimed failed")
            await interaction.followup.send(embed=error_embed("Claimed Failed", str(e)), ephemeral=True)
            return
        if not rows:
            await interaction.followup.send(embed=info_embed("Claimed Teams", "No teams have been claimed yet."), ephemeral=True)
            return
        lines = []
        for r in rows[:40]:
            uid = r.get("manager_user_id")
            manager_txt = f"<@{uid}>"
            try:
                m = interaction.guild.get_member(int(uid)) if interaction.guild and uid is not None else None
                if m:
                    manager_txt = f"{m.display_name} ({m.mention})"
            except Exception:
                pass
            league_label = LEAGUE_DISPLAY.get(r.get("season_league_key") or r.get("league_key"), r.get("season_league_key") or r.get("league_key"))
            if r.get("season_name"):
                season_name = r.get("season_name")
            elif r.get("claim_only") or not r.get("season_id"):
                season_name = f"{league_label} (no season set)"
            else:
                season_name = league_label
            lines.append(f"• **{r.get('team_name')}** — {manager_txt} _{season_name}_")
        await interaction.followup.send(embed=info_embed("Claimed Teams", "\n".join(lines)[:3900]), ephemeral=True)


async def setup(bot):
    await bot.add_cog(SeasonCog(bot))
