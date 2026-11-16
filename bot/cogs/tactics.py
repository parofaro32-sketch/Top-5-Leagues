from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import error_embed, info_embed
from bot.ui.modals import TacticsJsonModal
from services.tactics_service import normalize_tactics, tactics_summary


class TacticsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="tactics", description="Submit tactical settings for your team in a manager match")
    @app_commands.describe(
        match_id="Match ID",
        side="Which side you manage",
        mentality="Attacking mentality",
        pressing="Pressing intensity",
        defensive_line="Defensive line height",
    )
    @app_commands.choices(
        side=[app_commands.Choice(name="home", value="home"), app_commands.Choice(name="away", value="away")],
        mentality=[app_commands.Choice(name=v, value=v) for v in ["cautious","balanced","positive","attacking","very_attacking"]],
        pressing=[app_commands.Choice(name=v, value=v) for v in ["low","medium","high","very_high"]],
        defensive_line=[app_commands.Choice(name=v, value=v) for v in ["deep","standard","high","very_high"]],
    )
    async def tactics(self, interaction: discord.Interaction, match_id: str, side: app_commands.Choice[str],
                      mentality: app_commands.Choice[str] | None = None,
                      pressing: app_commands.Choice[str] | None = None,
                      defensive_line: app_commands.Choice[str] | None = None,
                      advanced_json: bool = False):
        session = self.bot.match_service.get(match_id) or await self.bot.match_service.load_from_db(match_id)
        if not session:
            await interaction.response.send_message(embed=error_embed("Not Found", "Match not found."), ephemeral=True)
            return
        s = side.value
        mgr = session.home_manager_id if s == "home" else session.away_manager_id
        if mgr is None:
            await interaction.response.send_message(embed=error_embed("Not Manager Mode", "This side is controlled by AI."), ephemeral=True)
            return
        if interaction.user.id != mgr:
            await interaction.response.send_message(embed=error_embed("Not Allowed", "You are not the assigned manager for this side."), ephemeral=True)
            return

        async def apply(inter: discord.Interaction, updates: dict):
            team_state = session.state.team(s)
            team_state.tactics = normalize_tactics({**(team_state.tactics or {}), **updates})
            await session.save()
            await inter.response.send_message(embed=info_embed("Tactics Updated", f"**{team_state.team.name}** tactics updated.\n{tactics_summary(team_state.tactics)}"), ephemeral=True)

        if advanced_json:
            async def on_modal(inter: discord.Interaction, data: dict):
                await apply(inter, data)
            await interaction.response.send_modal(TacticsJsonModal(on_modal))
            return

        updates = {"attacking": {}, "defensive": {}}
        if mentality: updates["attacking"]["mentality"] = mentality.value
        if pressing: updates["defensive"]["pressing_intensity"] = pressing.value
        if defensive_line: updates["defensive"]["defensive_line"] = defensive_line.value
        if not updates["attacking"]: updates.pop("attacking")
        if not updates["defensive"]: updates.pop("defensive")
        if not updates:
            # show current
            team_state = session.state.team(s)
            await interaction.response.send_message(embed=info_embed("Current Tactics", f"**{team_state.team.name}**\n{tactics_summary(team_state.tactics or {})}"), ephemeral=True)
            return
        await apply(interaction, updates)


async def setup(bot):
    await bot.add_cog(TacticsCog(bot))
