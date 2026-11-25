from __future__ import annotations

import asyncio
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
from services.season_service import LEAGUE_DISPLAY, LEAGUE_KEYS

logger = logging.getLogger(__name__)


def _league_choices() -> list[app_commands.Choice[str]]:
    # show top 5
    choices = []
    for k in LEAGUE_KEYS:
        name = LEAGUE_DISPLAY.get(k) or LEAGUE_DISPLAY_NAMES.get(k, k)
        choices.append(app_commands.Choice(name=name, value=k))
    return choices


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

    @app_commands.command(name="advance", description="Quick-sim remaining fixtures in current matchday and advance to next matchday when complete")
    @app_commands.describe(league="League (optional)")
    @app_commands.choices(league=_league_choices())
    async def advance(self, interaction: discord.Interaction, league: app_commands.Choice[str] | None = None):
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
        # quick-sim remaining pending fixtures
        simmed = []
        for fx in pending:
            try:
                res = await ss.quick_sim_fixture(season_id=sid, fixture_id=int(fx["id"]), guild_id=interaction.guild_id, channel_id=interaction.channel_id)
                simmed.append(res)
            except Exception as e:
                logger.exception("Failed to quick-sim fixture %s", fx.get("id"))
                simmed.append({"ok": False, "fixture_id": fx.get("id"), "reason": str(e)})

        # advance
        adv = await ss.advance_matchday_if_complete(sid)
        season2 = await ss.get_season(sid)
        played_lines = []
        fixtures2 = await ss.get_fixtures(sid, matchday=md)
        for fx in fixtures2[:12]:
            hs = fx.get("home_score"); as_ = fx.get("away_score")
            score = f"{hs}-{as_}" if hs is not None and as_ is not None else "vs"
            played_lines.append(f"**{fx.get('home_team_name')}** {score} **{fx.get('away_team_name')}**")
        msg = f"**{season.get('name')} — Matchday {md}**\n" + "\n".join(played_lines)
        if adv.get("advanced"):
            msg += f"\n\n✅ Advanced to **Matchday {adv.get('to_matchday')}**."
        elif adv.get("reason") == "season_completed":
            msg += "\n\n🏁 **Season completed.**"
        else:
            msg += f"\n\nMatchday status: {adv.get('reason')}."
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

    @app_commands.command(name="transfer", description="Create a pending transfer request (confirm with /finalizetransfer)")
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
                await interaction.followup.send(embed=error_embed("Transfer Create Failed", str(res.get("reason") or "unknown")), ephemeral=True)
                return
            tr = res.get("transfer") or {}
            await interaction.followup.send(embed=success_embed(
                "Transfer Pending",
                f"**{tr.get('player_name')}**: **{tr.get('from_team_name')}** → **{tr.get('to_team_name')}**\n"
                f"Transfer ID: `{tr.get('id')}`\n"
                f"Confirm with: `/finalizetransfer transfer_id:{tr.get('id')} confirm:CONFIRM`"
            ), ephemeral=True)
        except Exception as e:
            logger.exception("transfer create failed")
            await interaction.followup.send(embed=error_embed("Transfer Create Failed", str(e)), ephemeral=True)

    @app_commands.command(name="finalizetransfer", description="Confirm and apply a pending transfer (admin/manager flow)")
    @app_commands.describe(transfer_id="Pending transfer ID", confirm="Type CONFIRM to apply the transfer")
    async def finalizetransfer(self, interaction: discord.Interaction, transfer_id: int, confirm: str):
        await interaction.response.defer(ephemeral=True)
        if str(confirm).strip().upper() != "CONFIRM":
            await interaction.followup.send(embed=error_embed("Confirmation Required", "Type `CONFIRM` in the confirm field to apply this transfer."), ephemeral=True)
            return
        try:
            res = await self.bot.season_service.confirm_transfer(int(transfer_id), confirmed_by=interaction.user.id if interaction.user else None)
            if not res.get("ok"):
                await interaction.followup.send(embed=error_embed("Transfer Failed", str(res.get("reason") or "unknown error")), ephemeral=True)
                return
            await interaction.followup.send(embed=success_embed(
                "Transfer Finalized",
                f"**{res.get('player_name')}** moved from **{res.get('from_team_name')}** to **{res.get('to_team_name')}**.\n"
                f"Transfer ID: `{transfer_id}` | Status: confirmed"
            ), ephemeral=True)
        except Exception as e:
            logger.exception("finalizetransfer failed")
            await interaction.followup.send(embed=error_embed("Transfer Failed", str(e)), ephemeral=True)


    @app_commands.command(name="claim", description="Claim a team in the active season")
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
                await interaction.followup.send(embed=error_embed("Team Not Found", f"No active-season team found for `{team}`. Create a season first with `/season create`."), ephemeral=True)
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
        if res.get("already_claimed_by_you"):
            await interaction.followup.send(embed=info_embed("Already Claimed", f"You already manage **{team_name}** in **{season.get('name', 'this season')}**."), ephemeral=True)
            return
        await interaction.followup.send(embed=success_embed("Team Claimed", f"{interaction.user.mention} is now the manager of **{team_name}** in **{season.get('name', 'the active season')}**."), ephemeral=True)

    @app_commands.command(name="claimed", description="Show claimed teams and their managers in active seasons")
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
            await interaction.followup.send(embed=info_embed("Claimed Teams", "No teams have been claimed yet in active seasons."), ephemeral=True)
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
            season_name = r.get("season_name") or LEAGUE_DISPLAY.get(r.get("season_league_key"), r.get("season_league_key"))
            lines.append(f"• **{r.get('team_name')}** — {manager_txt} _{season_name}_")
        await interaction.followup.send(embed=info_embed("Claimed Teams", "\n".join(lines)[:3900]), ephemeral=True)


async def setup(bot):
    await bot.add_cog(SeasonCog(bot))
