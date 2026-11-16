from __future__ import annotations

import json
import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import error_embed, info_embed
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
        starters_json="Optional JSON array of starter objects/ids (advanced)",
        bench_json="Optional JSON array of bench objects/ids (advanced)",
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
        try:
            if auto or (not starters_json):
                lu = auto_build_lineup(team_state.team.api_id, team_state.team.name, squad, formation=formation)
            else:
                starters = json.loads(starters_json)
                bench = json.loads(bench_json) if bench_json else []
                # allow simple id lists
                def map_item(x):
                    if isinstance(x, int):
                        sp = next((p for p in squad if int(p.get('api_id') or p.get('id') or 0) == x), None)
                        if not sp:
                            return {"api_id": x, "name": str(x), "position": "MID"}
                        return sp
                    return x
                starters = [map_item(x) for x in starters]
                bench = [map_item(x) for x in bench]
                lu = validate_lineup(team_api_id=team_state.team.api_id, team_name=team_state.team.name, formation=formation, starters=starters, bench=bench, squad=squad)
        except (LineupValidationError, json.JSONDecodeError, Exception) as e:
            await interaction.followup.send(embed=error_embed("Lineup Error", f"Failed to set lineup: {e}"), ephemeral=True)
            return

        # Apply to team state for future breaks/setup context (mid-match live XI changes beyond subs are limited, but we store/update lineup)
        team_state.lineup = lu
        await session.save()
        names = ", ".join([p.name for p in lu.starters[:11]])
        await interaction.followup.send(embed=info_embed("Lineup Updated", f"Formation **{lu.formation}** saved for **{team_state.team.name}**.\nStarters: {names}"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(LineupCog(bot))
