from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.views import BreakControlView, SimpleChoiceView, TeamSelect

logger = logging.getLogger(__name__)


def _clamp(text: str, limit: int = 1900) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…(truncated)"


def _format_lineup(state, side: str) -> str:
    team = state.team(side)
    lu = team.lineup
    starters = list(lu.starters)
    bench = list(lu.bench)
    # Order starters roughly GK, DEF, MID, FWD by slot/position for readability
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    starters_sorted = sorted(starters, key=lambda e: (order.get((e.position or "MID").upper(), 2), e.slot or ""))
    lines = [f"**{team.team.name}** — Formation: {lu.formation}"]
    lines.append("Starting XI:")
    for e in starters_sorted:
        tags = []
        if e.player_api_id == lu.captain_player_id:
            tags.append("C")
        if e.player_api_id == lu.goalkeeper_player_id or (e.position or "").upper() == "GK":
            tags.append("GK")
        tag_txt = f" ({', '.join(tags)})" if tags else ""
        slot = f"[{e.slot}] " if e.slot else ""
        lines.append(f"- {slot}{e.name} — {e.position}{tag_txt}")
    if bench:
        lines.append("Bench: " + ", ".join([f"{b.name} ({b.position})" for b in bench]))
    else:
        lines.append("Bench: none")
    # Tactics summary
    t = team.tactics or {}
    atk = t.get("attacking", {})
    dfn = t.get("defensive", {})
    tparts = []
    if atk.get("mentality"):
        tparts.append(f"mentality {atk.get('mentality')}")
    if dfn.get("pressing_intensity"):
        tparts.append(f"press {dfn.get('pressing_intensity')}")
    if dfn.get("defensive_line"):
        tparts.append(f"line {dfn.get('defensive_line')}")
    if atk.get("build_up"):
        tparts.append(f"build-up {atk.get('build_up')}")
    if atk.get("attacking_width"):
        tparts.append(f"width {atk.get('attacking_width')}")
    if tparts:
        lines.append("Tactics: " + ", ".join(tparts))
    return _clamp("\n".join(lines))


def _format_break_status(state: dict[str, Any]) -> str:
    score = state.get("score", {})
    stats = state.get("statistics", {})
    h = stats.get("home", {})
    a = stats.get("away", {})
    lines = [
        f"**Tactical Break — {score.get('scoreline','Match')}**",
        f"Minute: {state.get('minute',0)}' | Phase: {state.get('phase','break')}",
        "",
        f"Possession: {h.get('possession',0)}% - {a.get('possession',0)}%",
        f"Shots (on target): {h.get('shots',0)} ({h.get('shots_on_target',0)}) - {a.get('shots',0)} ({a.get('shots_on_target',0)})",
        f"xG: {h.get('xg',0)} - {a.get('xg',0)}",
        f"Corners: {h.get('corners',0)} - {a.get('corners',0)}",
        f"Fouls: {h.get('fouls',0)} - {a.get('fouls',0)}",
        f"Cards (Y/R): {h.get('yellow_cards',0)}/{h.get('red_cards',0)} - {a.get('yellow_cards',0)}/{a.get('red_cards',0)}",
    ]
    return _clamp("\n".join(lines))


def _format_report(report: dict[str, Any]) -> list[str]:
    fs = report.get("final_score", {})
    stats = report.get("statistics", {})
    h = stats.get("home", {})
    a = stats.get("away", {})
    msgs: list[str] = []

    lines = [f"**Full-Time: {fs.get('scoreline','')}**", ""]
    goals = report.get("goalscorers", [])
    if goals:
        lines.append("**Goalscorers**")
        for g in goals:
            m = f"{g.get('minute',0)}{('+'+str(g.get('extra_minute'))) if g.get('extra_minute') else ''}'"
            who = g.get('scorer') or 'Unknown'
            team = g.get('team') or ''
            ast = f" (assist: {g.get('assist')})" if g.get('assist') else ''
            lines.append(f"- {m} {who} — {team}{ast}")
    else:
        lines.append("**Goalscorers:** None")
    lines.append("")
    lines.append("**Match Statistics**")
    lines.append(f"Possession: {h.get('possession',0)}% - {a.get('possession',0)}%")
    lines.append(f"Shots (on target): {h.get('shots',0)} ({h.get('shots_on_target',0)}) - {a.get('shots',0)} ({a.get('shots_on_target',0)})")
    lines.append(f"xG: {h.get('xg',0)} - {a.get('xg',0)}")
    lines.append(f"Corners: {h.get('corners',0)} - {a.get('corners',0)}")
    lines.append(f"Fouls: {h.get('fouls',0)} - {a.get('fouls',0)}")
    lines.append(f"Cards (Y/R): {h.get('yellow_cards',0)}/{h.get('red_cards',0)} - {a.get('yellow_cards',0)}/{a.get('red_cards',0)}")
    lines.append(f"Pass accuracy: {h.get('pass_accuracy',0)}% - {a.get('pass_accuracy',0)}%")
    motm = report.get("man_of_the_match")
    if motm:
        lines.append("")
        lines.append(f"**Man of the Match:** {motm.get('name')} ({motm.get('team')}) — {motm.get('rating')}★")
    msgs.append(_clamp("\n".join(lines)))

    # Ratings summary per team
    def fmt_team(side_key: str, label: str) -> str:
        players = (report.get('player_ratings') or {}).get(side_key, [])
        parts = [f"**{label} — Player Ratings**"]
        shown = 0
        for p in players:
            if p.get('minutes_played', 0) <= 0 and not p.get('on_pitch'):
                continue
            tag = []
            if p.get('goals'): tag.append(f"{p['goals']}G")
            if p.get('assists'): tag.append(f"{p['assists']}A")
            if p.get('red_card'): tag.append("RC")
            elif p.get('yellow_cards'): tag.append(f"{p['yellow_cards']}Y")
            if p.get('injured'): tag.append("INJ")
            extra = f" ({', '.join(tag)})" if tag else ""
            parts.append(f"- {p.get('name')} [{p.get('position')}] {p.get('rating')}★ | {p.get('minutes_played')}' | STA {p.get('stamina')}{extra}")
            shown += 1
            if shown >= 18:
                break
        if shown == 0:
            parts.append("- No participants recorded")
        return _clamp("\n".join(parts))

    msgs.append(fmt_team('home', fs.get('home_team', 'Home')))
    msgs.append(fmt_team('away', fs.get('away_team', 'Away')))

    # Notes/analysis
    extra_lines = []
    ta = report.get('tactical_analysis')
    if ta:
        extra_lines.append("**Tactical Analysis**")
        extra_lines.append(str(ta))
    disc = report.get('discipline') or []
    if disc:
        extra_lines.append("")
        extra_lines.append("**Discipline**")
        for x in disc[:15]:
            extra_lines.append(f"- {x.get('minute')}' {x.get('player')} ({x.get('team')}) — {str(x.get('type','')).replace('_',' ')}")
    inj = report.get('injuries') or []
    if inj:
        extra_lines.append("")
        extra_lines.append("**Injuries**")
        for x in inj[:12]:
            status = 'continued' if x.get('continued') else 'substituted off' if x.get('substituted') else 'forced off'
            extra_lines.append(f"- {x.get('minute')}' {x.get('player')} ({x.get('team')}) — {x.get('severity')} ({status})")
    subs = report.get('substitutions') or []
    if subs:
        extra_lines.append("")
        extra_lines.append("**Substitutions**")
        for x in subs[:20]:
            reason = f" ({x.get('reason')})" if x.get('reason') else ''
            extra_lines.append(f"- {x.get('minute')}' {x.get('team')}: {x.get('on_name')} for {x.get('off_name')}{reason}")
    if extra_lines:
        msgs.append(_clamp("\n".join(extra_lines)))

    return [m for m in msgs if m.strip()]


class SimCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _send_events_live(self, channel: discord.abc.Messageable, events: list[dict[str, Any]], delay: float = 3.0):
        for ev in events:
            text = ev.get("commentary")
            if not text:
                continue
            try:
                await channel.send(_clamp(str(text), 1900))
            except Exception:
                logger.exception("Failed to send match event message")
            if delay > 0:
                await asyncio.sleep(delay)

    async def _handle_break(self, channel: discord.abc.Messageable, session) -> None:
        # Auto-resolve AI sides immediately
        await session.auto_resolve_ai_sides_if_ready()
        await session.save()

        bs = session.break_status()
        has_human = (session.home_manager_id is not None) or (session.away_manager_id is not None)

        if not has_human:
            # Bot vs Bot: brief message and continue smoothly
            await channel.send(_format_break_status(bs))
            await channel.send(_clamp(f"Tactical break: both AI managers make adjustments. {session.state.scoreline()} — play will resume shortly."))
            await asyncio.sleep(1)
            return

        # Manager mode: show break status and controls, wait for readiness
        view = BreakControlView(session.state.id, session)
        try:
            await channel.send(_format_break_status(bs) + "\n\nManagers: confirm ready or apply AI-suggested adjustments for your side.", view=view)
        except Exception:
            await channel.send(_format_break_status(bs))

        waited = 0
        while session.state.pending_break and session.engine.is_waiting_for_break_decisions():
            await session.auto_resolve_ai_sides_if_ready()
            await session.save()
            if session.is_ready_to_resume():
                break
            await asyncio.sleep(2)
            waited += 2
            if waited >= 300:
                # timeout: auto-apply for remaining human sides
                for side in ("home", "away"):
                    mgr = session.home_manager_id if side == "home" else session.away_manager_id
                    done = session.state.home_break_decision_done if side == "home" else session.state.away_break_decision_done
                    if mgr is not None and not done:
                        try:
                            from engine.ai_manager import decide_ai_break_actions
                            actions = decide_ai_break_actions(session.state, side)
                            await session.apply_break_actions(side, substitutions=actions.get("substitutions", []), tactics_updates=actions.get("tactics_updates", {}), summary="Auto-applied (timeout)")
                        except Exception:
                            logger.exception("Failed to auto-apply break actions on timeout for %s", side)
                await session.save()
                break

    async def _run_match_flow(self, interaction: discord.Interaction, session):
        channel = interaction.channel
        if channel is None:
            return

        st = session.state
        await channel.send(_clamp(
            f"**Match Starting**\n{st.home.team.name} vs {st.away.team.name}\n"
            f"Match ID: `{st.id}` | Mode: {st.mode.replace('_', ' ').title()}"
        ))

        # Starting lineups
        try:
            await channel.send(_format_lineup(st, "home"))
            await channel.send(_format_lineup(st, "away"))
        except Exception:
            logger.exception("Failed to send starting lineups")

        # Main loop
        while True:
            if session.state.status == "finished":
                break

            if session.state.status == "setup":
                evs = await session.start_and_simulate_until_break()
            elif session.state.pending_break and session.engine.is_waiting_for_break_decisions():
                # Should not happen normally, but handle safely
                await self._handle_break(channel, session)
                if session.is_ready_to_resume() and session.state.pending_break:
                    evs = await session.continue_after_break()
                else:
                    await asyncio.sleep(1)
                    continue
            else:
                # Fallback continue if somehow not finished and not in handled break state
                evs = await session.continue_after_break()

            # Stream commentary live (1 event per 3 seconds)
            await self._send_events_live(channel, evs, delay=3.0)

            if session.state.status == "finished":
                break

            # Handle tactical/halftime break
            if session.state.pending_break and session.engine.is_waiting_for_break_decisions():
                await self._handle_break(channel, session)
                # Resume when ready (continue_after_break resumes and simulates next segment)
                if session.state.pending_break and session.engine.is_waiting_for_break_decisions():
                    # still waiting (should be rare), loop back to wait handling
                    await asyncio.sleep(1)
                    continue
                # If ready, resume and simulate next segment
                if session.is_ready_to_resume() and session.state.pending_break:
                    # mark decisions done for any AI sides again defensively
                    await session.auto_resolve_ai_sides_if_ready()
                if session.state.pending_break:
                    evs = await session.continue_after_break()
                    await channel.send(_clamp(f"▶️ Play resumes. {session.state.scoreline()}"))
                    await self._send_events_live(channel, evs, delay=3.0)
                    if session.state.status == "finished":
                        break
                    # loop to check for next break/finish
                    continue

            await asyncio.sleep(0)

        # Full-time report
        try:
            rep = session.report()
            for msg in _format_report(rep):
                await channel.send(msg)
                await asyncio.sleep(0.5)
        except Exception:
            logger.exception("Failed to send full-time report")
            try:
                await channel.send(_clamp(f"Full-time: {session.state.scoreline()}"))
            except Exception:
                pass

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
                await interaction.followup.send("Could not load enough teams for selection.", ephemeral=True)
                return

            selected: dict[str, Any] = {"home": home, "away": away}

            async def on_away_pick(inter: discord.Interaction, team_id: int):
                t = await fd.get_team(team_id)
                if selected.get("home") and int(t.get("api_id", 0)) == int(selected["home"].get("api_id", 0)):
                    await inter.response.send_message("Away team must be different from home team.", ephemeral=True)
                    return
                selected["away"] = t
                await inter.response.edit_message(content=f"Match setup: **{selected['home'].get('name')}** vs **{t.get('name')}**\nStarting simulation...", view=None)
                await self._launch_with_selection(interaction, mode.value, selected["home"], selected["away"], home_manager, away_manager)

            async def on_home_pick_chain(inter: discord.Interaction, team_id: int):
                t = await fd.get_team(team_id)
                selected["home"] = t
                away_opts = [x for x in teams if int(x["api_id"]) != team_id]
                view2_local = SimpleChoiceView(interaction.user.id)
                view2_local.add_item(TeamSelect(away_opts, "Select AWAY team", on_away_pick))
                await inter.response.edit_message(content=f"Home team: **{t.get('name')}**\nSelect away team.", view=view2_local)

            view = SimpleChoiceView(interaction.user.id)
            view.add_item(TeamSelect(teams, "Select HOME team", on_home_pick_chain))
            league_name = next((l['name'] for l in fd.list_leagues() if l['key'] == league_key), league_key)
            await interaction.followup.send(f"**Match Setup** — {league_name}\nSelect the home team, then away team.", view=view, ephemeral=True)
            return

        await self._launch_with_selection(interaction, mode.value, home, away, home_manager, away_manager)

    async def _launch_with_selection(self, interaction: discord.Interaction, mode: str, home: dict[str, Any], away: dict[str, Any], home_manager: discord.User | None, away_manager: discord.User | None):
        fd = self.bot.football_data
        ms = self.bot.match_service
        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("No channel available to run the match.", ephemeral=True)
            return

        if int(home["api_id"]) == int(away["api_id"]):
            await interaction.followup.send("Home and away teams must be different.", ephemeral=True)
            return

        hm_id = None
        am_id = None
        if mode == "manager_vs_manager":
            hm_id = home_manager.id if home_manager else interaction.user.id
            am_id = away_manager.id if away_manager else None
            if am_id is None:
                await interaction.followup.send("Manager vs Manager mode requires `away_manager` (and optionally `home_manager`).", ephemeral=True)
                return
            if hm_id == am_id:
                await interaction.followup.send("Home and away managers must be different users.", ephemeral=True)
                return

        try:
            home_squad = await fd.get_squad(int(home["api_id"]))
            away_squad = await fd.get_squad(int(away["api_id"]))
        except Exception as e:
            logger.exception("Failed to load squads")
            await interaction.followup.send(f"Failed to load team squads: {e}", ephemeral=True)
            return

        if len(home_squad) < 11 or len(away_squad) < 11:
            await interaction.followup.send("One or both teams do not have enough players in bundled data.", ephemeral=True)
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
            await interaction.followup.send(f"Failed to create match: {e}", ephemeral=True)
            return

        msg = f"Match created: **{home.get('name')}** vs **{away.get('name')}**\nMatch ID: `{session.state.id}`\nMode: {mode.replace('_',' ').title()}"
        if mode == "manager_vs_manager":
            msg += f"\nHome manager: <@{hm_id}>\nAway manager: <@{am_id}>"
        try:
            await interaction.followup.send(_clamp(msg), ephemeral=True)
        except Exception:
            pass

        # Run full flow in background task
        asyncio.create_task(self._run_match_flow(interaction, session))


async def setup(bot):
    await bot.add_cog(SimCog(bot))


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
