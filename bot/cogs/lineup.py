from __future__ import annotations

import json
import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import error_embed, info_embed
from engine.models import Player, PlayerAttributes
from services.lineup_service import LineupValidationError, auto_build_lineup, validate_lineup
from services.tactics_service import normalize_tactics


class LineupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="lineup", description="Submit or auto-build a lineup for your team in a manager match")
    @app_commands.describe(
        match_id="Match ID",
        side="Which side you manage",
        formation="Formation (e.g. 4-3-3, 4-2-3-1)",
        auto="Auto-build lineup from squad",
        starters_json="11 starters: JSON array, or plain names separated by commas/new lines. For custom players add position like 'Name (GK/DEF/MID/FWD)'.",
        bench_json="Optional bench: JSON array or comma/newline-separated names.",
    )
    @app_commands.choices(side=[app_commands.Choice(name="home", value="home"), app_commands.Choice(name="away", value="away")])
    async def lineup(self, interaction: discord.Interaction, match_id: str, side: app_commands.Choice[str], formation: str = "4-3-3", auto: bool = True, starters_json: str | None = None, bench_json: str | None = None):
        await interaction.response.defer(ephemeral=True)
        session = self.bot.match_service.get(match_id) or await self.bot.match_service.load_from_db(match_id)
        if not session:
            await interaction.followup.send(embed=error_embed("Not Found", "Match not found."), ephemeral=True)
            return
        s = side.value
        mgr = session.home_manager_id if s == "home" else session.away_manager_id
        if mgr is None:
            await interaction.followup.send(embed=error_embed("Not Manager Mode", "This side is controlled by AI."), ephemeral=True)
            return
        if interaction.user.id != mgr:
            await interaction.followup.send(embed=error_embed("Not Allowed", "You are not the assigned manager for this side."), ephemeral=True)
            return
        team_state = session.state.team(s)
        squad = await self.bot.football_data.get_squad(team_state.team.api_id)
        custom_players: list[dict] = []

        def _parse_players_input(raw: str | None) -> list:
            if raw is None:
                return []
            text = raw.strip()
            if not text:
                return []
            # Try JSON first
            try:
                data = json.loads(text)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    for k in ("players", "starters", "lineup", "bench", "items"):
                        if isinstance(data.get(k), list):
                            return data[k]
                    return [data]
            except Exception:
                pass
            # Fallback: comma/newline separated names with optional position hints
            import re as _re
            items: list = []
            parts: list[str] = []
            for line in text.replace("\r", "\n").split("\n"):
                if "," in line:
                    parts.extend([p.strip() for p in line.split(",")])
                else:
                    parts.append(line.strip())
            for p in parts:
                if not p:
                    continue
                m = _re.match(r"^(.*?)\s*\((GK|DEF|MID|FWD|G|D|M|F|GOALKEEPER|DEFENDER|MIDFIELDER|FORWARD|ATT|ST)\)\s*$", p, _re.IGNORECASE)
                if not m:
                    m = _re.match(r"^(.*?)\s*[-|:]\s*(GK|DEF|MID|FWD|G|D|M|F|GOALKEEPER|DEFENDER|MIDFIELDER|FORWARD|ATT|ST)\s*$", p, _re.IGNORECASE)
                if m:
                    name = m.group(1).strip().strip('"').strip("'")
                    pos = m.group(2).strip().upper()
                    if name:
                        items.append({"name": name, "position": pos})
                else:
                    name = p.strip().strip('"').strip("'")
                    if name:
                        items.append({"name": name})
            return items

        try:
            if auto or (not starters_json):
                lu = auto_build_lineup(team_state.team.api_id, team_state.team.name, squad, formation=formation)
            else:
                starters = _parse_players_input(starters_json)
                bench = _parse_players_input(bench_json)
                if len(starters) != 11:
                    raise LineupValidationError(
                        f"Provide exactly 11 starters (got {len(starters)}). Use a JSON array or comma/newline-separated names. "
                        "You can include positions for custom players, e.g. 'Alisson (GK), Van Dijk (DEF), ...'"
                    )
                lu, custom_players = validate_lineup(
                    team_api_id=team_state.team.api_id,
                    team_name=team_state.team.name,
                    formation=formation,
                    starters=starters,
                    bench=bench,
                    squad=squad,
                    return_custom=True,
                )
        except (LineupValidationError, json.JSONDecodeError, Exception) as e:
            await interaction.followup.send(embed=error_embed("Lineup Error", f"Failed to set lineup: {e}"), ephemeral=True)
            return

        # Register any custom players into engine state so they work in simulation
        for cp in custom_players:
            key = session.state.player_key(s, int(cp["api_id"]))
            attrs = cp.get("attributes") or {}
            if key not in session.state.players_index:
                session.state.players_index[key] = Player(
                    api_id=int(cp["api_id"]),
                    name=cp.get("name") or "Custom Player",
                    team_api_id=team_state.team.api_id,
                    position=(cp.get("position") or "MID").upper(),
                    detailed_position=cp.get("position"),
                    number=cp.get("number"),
                    nationality=cp.get("nationality"),
                    attributes=PlayerAttributes.from_dict(attrs),
                )

        # If match not started yet, replace full lineup and reinitialize player states for side
        if session.state.status == "setup":
            try:
                session.engine.replace_lineup(s, lu)
            except Exception:
                # fallback: at least set lineup
                team_state.lineup = lu
                session.engine._init_ps(s, lu)
        else:
            # mid-match: store for reference; live XI changes handled via substitutions/break controls
            team_state.lineup = lu

        # Mark setup submission flags for manager mode
        if session.state.mode == "manager_vs_manager" and mgr is not None and session.state.status == "setup":
            session.state.submission[f"{s}_lineup_submitted"] = True
            session.state.submission[f"{s}_lineup_submitted_at"] = discord.utils.utcnow().isoformat()
        await session.save()
        # Nudge sim cog to check setup completion
        if session.state.mode == "manager_vs_manager" and session.state.status == "setup":
            sim_cog = self.bot.get_cog("SimCog")
            if sim_cog and hasattr(sim_cog, "_after_submission_update"):
                try:
                    await sim_cog._after_submission_update(session, s, None)
                except Exception:
                    pass
        names = ", ".join([p.name for p in lu.starters[:11]])
        extra = ""
        if custom_players:
            extra = f"\nCustom players added: {', '.join([p.get('name','?') for p in custom_players[:10]])}" + ("…" if len(custom_players) > 10 else "")
        note = "\nNote: if the match has already started, use tactical break substitutions to change the on-pitch XI." if session.state.status != "setup" else ""
        await interaction.followup.send(embed=info_embed("Lineup Updated", f"Formation **{lu.formation}** saved for **{team_state.team.name}**.\nStarters: {names}{extra}{note}"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(LineupCog(bot))
