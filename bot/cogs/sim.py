from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import break_embed, commentary_chunk_embed, error_embed, info_embed, report_embeds, setup_embed
from bot.ui.views import BreakControlView, SimpleChoiceView, TeamSelect

logger = logging.getLogger(__name__)


class SimCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _send_chunked_commentary(self, channel: discord.abc.Messageable, events: list[dict[str, Any]], title: str, scoreline: str):
        # Post minute-by-minute commentary in manageable chunks (Discord embed desc limit ~4096)
        lines = [ev.get("commentary") for ev in events if ev.get("commentary")]
        if not lines:
            return
        chunk: list[str] = []
        size = 0
        idx = 1
        for line in lines:
            add = len(line) + 1
            if size + add > 3400 and chunk:
                await channel.send(embed=commentary_chunk_embed(f"{title} (Part {idx})", chunk, scoreline))
                idx += 1
                chunk = [line]
                size = add
            else:
                chunk.append(line)
                size += add
        if chunk:
            suffix = f" (Part {idx})" if idx > 1 else ""
            await channel.send(embed=commentary_chunk_embed(f"{title}{suffix}", chunk, scoreline))

    async def _run_match_flow(self, interaction: discord.Interaction, session):
        channel = interaction.channel
        if channel is None:
            return
        await channel.send(embed=info_embed("Match Starting", f"**{session.state.home.team.name}** vs **{session.state.away.team.name}**\nMatch ID: `{session.state.id}`\nMode: {session.state.mode.replace('_',' ').title()}"))

        async def tick(events: list[dict[str, Any]]):
            if not events:
                return
            # show progress in chunks occasionally? we send per segment after breaks/end

        # simulate to first break/end
        while True:
            if session.state.status == "setup":
                evs = await session.start_and_simulate_until_break()
            elif session.state.pending_break and session.engine.is_waiting_for_break_decisions():
                evs = []
            elif session.state.status == "finished":
                break
            else:
                evs = await session.continue_after_break()

            if evs:
                # find a readable title by phase
                scoreline = session.state.scoreline()
                title = "Live Commentary"
                if session.state.status == "finished" or session.state.phase == "finished":
                    title = "Commentary — Full Match"
                elif session.state.pending_break == "halftime" or session.state.phase == "halftime":
                    title = "Commentary — First Half"
                elif session.state.pending_break == "break_60":
                    title = "Commentary — 46' to 60'"
                elif session.state.pending_break == "break_75":
                    title = "Commentary — 61' to 75'"
                else:
                    title = f"Commentary — Up to {session.state.minute}'"
                await self._send_chunked_commentary(channel, evs, title, scoreline)

            if session.state.status == "finished":
                rep = session.report()
                for em in report_embeds(rep):
                    await channel.send(embed=em)
                break

            if session.state.pending_break and session.engine.is_waiting_for_break_decisions():
                # auto-resolve AI sides
                await session.auto_resolve_ai_sides_if_ready()
                await session.save()
                bs = session.break_status()
                view = BreakControlView(session.state.id, session)
                await channel.send(embed=break_embed(bs), view=view)
                # wait for managers / AI readiness
                waited = 0
                while session.state.pending_break and session.engine.is_waiting_for_break_decisions():
                    await session.auto_resolve_ai_sides_if_ready()
                    await session.save()
                    if session.is_ready_to_resume():
                        break
                    await asyncio.sleep(2)
                    waited += 2
                    # auto-continue after timeout (5 minutes) applying AI suggestions for any remaining human side
                    if waited >= 300:
                        for side in ("home", "away"):
                            mgr = session.home_manager_id if side == "home" else session.away_manager_id
                            done = session.state.home_break_decision_done if side == "home" else session.state.away_break_decision_done
                            if mgr is not None and not done:
                                from engine.ai_manager import decide_ai_break_actions
                                actions = decide_ai_break_actions(session.state, side)
                                await session.apply_break_actions(side, substitutions=actions.get("substitutions", []), tactics_updates=actions.get("tactics_updates", {}), summary="Auto-applied (timeout)")
                        break
                await channel.send(embed=info_embed("Break Resolved", f"Resuming match {session.state.scoreline()}..."))
                continue

            # safety
            await asyncio.sleep(0)

    @app_commands.command(name="sim", description="Start a football match simulation (Bot vs Bot or Manager vs Manager)")
    @app_commands.describe(
        mode="Match mode",
        league="League to choose teams from",
        home_team="Home team name (search)",
        away_team="Away team name (search)",
        home_manager="Home human manager (Manager vs Manager)",
        away_manager="Away human manager (Manager vs Manager)",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Bot vs Bot", value="bot_vs_bot"),
        app_commands.Choice(name="Manager vs Manager", value="manager_vs_manager"),
    ])
    async def sim(self, interaction: discord.Interaction,
                  mode: app_commands.Choice[str],
                  league: str | None = None,
                  home_team: str | None = None,
                  away_team: str | None = None,
                  home_manager: discord.User | None = None,
                  away_manager: discord.User | None = None):
        await interaction.response.defer(thinking=True)
        fd = self.bot.football_data
        ms = self.bot.match_service

        # Resolve teams
        async def resolve_team(q: str | None, label: str) -> dict[str, Any] | None:
            if not q:
                return None
            matches = await fd.list_teams(query=q, league=league, limit=5)
            if not matches:
                return None
            # exact-ish first
            ql = q.lower().strip()
            for m in matches:
                if (m.get("name") or "").lower() == ql or (m.get("short_name") or "").lower() == ql:
                    return m
            return matches[0]

        home = await resolve_team(home_team, "home")
        away = await resolve_team(away_team, "away")

        # If teams not provided or not found, fall back to interactive selection for the given/first league
        if home is None or away is None or (home and away and home.get("api_id") == away.get("api_id")):
            leagues = fd.list_leagues()
            league_key = None
            from data.bundled import resolve_league_key
            if league:
                league_key = resolve_league_key(league)
            if not league_key:
                league_key = leagues[0]["key"] if leagues else "premier_league"
            teams = await fd.list_teams(league=league_key, limit=25)
            if len(teams) < 2:
                await interaction.followup.send(embed=error_embed("Team Data Missing", "Could not load enough teams for selection."), ephemeral=True)
                return

            view = SimpleChoiceView(interaction.user.id)
            selected: dict[str, Any] = {"home": home, "away": away}

            async def on_home_pick(inter: discord.Interaction, team_id: int):
                t = await fd.get_team(team_id)
                selected["home"] = t
                await inter.response.edit_message(embed=setup_embed("Match Setup", f"Home team selected: **{t.get('name')}**\nNow select away team."), view=view2)

            async def on_away_pick(inter: discord.Interaction, team_id: int):
                t = await fd.get_team(team_id)
                if selected.get("home") and int(t.get("api_id", 0)) == int(selected["home"].get("api_id", 0)):
                    await inter.response.send_message("Away team must be different from home team.", ephemeral=True)
                    return
                selected["away"] = t
                await inter.response.edit_message(embed=setup_embed("Match Setup", f"Home: **{selected['home'].get('name')}**\nAway: **{t.get('name')}**\nStarting simulation..."), view=None)
                await self._launch_with_selection(interaction, mode.value, selected["home"], selected["away"], home_manager, away_manager)

            view.add_item(TeamSelect(teams, "Select HOME team", on_home_pick))
            view2 = SimpleChoiceView(interaction.user.id)
            # away options exclude selected home later; initial full list ok
            async def on_home_pick_chain(inter: discord.Interaction, team_id: int):
                t = await fd.get_team(team_id)
                selected["home"] = t
                away_opts = [x for x in teams if int(x["api_id"]) != team_id]
                view2_local = SimpleChoiceView(interaction.user.id)
                view2_local.add_item(TeamSelect(away_opts, "Select AWAY team", on_away_pick))
                await inter.response.edit_message(embed=setup_embed("Match Setup", f"Home team: **{t.get('name')}**\nSelect away team."), view=view2_local)

            # rebuild view with chained handler
            view = SimpleChoiceView(interaction.user.id)
            view.add_item(TeamSelect(teams, "Select HOME team", on_home_pick_chain))
            await interaction.followup.send(embed=setup_embed("Match Setup", f"League: **{next((l['name'] for l in fd.list_leagues() if l['key']==league_key), league_key)}**\nSelect the home team, then away team."), view=view, ephemeral=True)
            return

        await self._launch_with_selection(interaction, mode.value, home, away, home_manager, away_manager)

    async def _launch_with_selection(self, interaction: discord.Interaction, mode: str, home: dict[str, Any], away: dict[str, Any], home_manager: discord.User | None, away_manager: discord.User | None):
        fd = self.bot.football_data
        ms = self.bot.match_service
        channel = interaction.channel
        if channel is None:
            await interaction.followup.send(embed=error_embed("Error", "No channel available to run the match."), ephemeral=True)
            return

        if int(home["api_id"]) == int(away["api_id"]):
            await interaction.followup.send(embed=error_embed("Invalid Selection", "Home and away teams must be different."), ephemeral=True)
            return

        hm_id = None
        am_id = None
        if mode == "manager_vs_manager":
            hm_id = home_manager.id if home_manager else interaction.user.id
            am_id = away_manager.id if away_manager else None
            if am_id is None:
                await interaction.followup.send(embed=error_embed("Manager Required", "Manager vs Manager mode requires `away_manager` to be specified (and optionally `home_manager`)."), ephemeral=True)
                return
            if hm_id == am_id:
                await interaction.followup.send(embed=error_embed("Invalid Managers", "Home and away managers must be different users."), ephemeral=True)
                return

        try:
            home_squad = await fd.get_squad(int(home["api_id"]))
            away_squad = await fd.get_squad(int(away["api_id"]))
        except Exception as e:
            logger.exception("Failed to load squads")
            await interaction.followup.send(embed=error_embed("Data Error", f"Failed to load team squads: {e}"), ephemeral=True)
            return

        if len(home_squad) < 11 or len(away_squad) < 11:
            await interaction.followup.send(embed=error_embed("Squad Error", "One or both teams do not have enough players in bundled data."), ephemeral=True)
            return

        try:
            session = await ms.create_session(
                guild_id=interaction.guild_id,
                channel_id=channel.id if hasattr(channel, 'id') else None,
                mode=mode,
                home_team=home, away_team=away,
                home_squad=home_squad, away_squad=away_squad,
                home_manager_id=hm_id, away_manager_id=am_id,
            )
        except Exception as e:
            logger.exception("Failed to create match session")
            await interaction.followup.send(embed=error_embed("Simulation Error", f"Failed to create match: {e}"), ephemeral=True)
            return

        msg = f"Match created: **{home.get('name')}** vs **{away.get('name')}**\nMatch ID: `{session.state.id}`\nMode: {mode.replace('_',' ').title()}"
        if mode == "manager_vs_manager":
            msg += f"\nHome manager: <@{hm_id}>\nAway manager: <@{am_id}>\nDefault lineups/tactics auto-assigned; managers can use break controls and `/lineup`/`tactics` features for adjustments (current build auto-applies AI suggestions on ready/timeout)."
        try:
            await interaction.followup.send(embed=info_embed("Simulation Ready", msg), ephemeral=True)
        except Exception:
            pass

        # Run full flow in background task
        asyncio.create_task(self._run_match_flow(interaction, session))


async def setup(bot):
    await bot.add_cog(SimCog(bot))
