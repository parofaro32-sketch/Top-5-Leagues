from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import error_embed, info_embed


class DashboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="dashboard", description="Show status of a match simulation")
    @app_commands.describe(match_id="Match ID (leave empty to list active matches in this channel)")
    async def dashboard(self, interaction: discord.Interaction, match_id: str | None = None):
        await interaction.response.defer(ephemeral=True)
        ms = self.bot.match_service
        if match_id:
            session = ms.get(match_id) or await ms.load_from_db(match_id)
            if not session:
                await interaction.followup.send(embed=error_embed("Not Found", f"No match found with ID `{match_id}`."), ephemeral=True)
                return
            st = session.state
            desc = (
                f"**{st.home.team.name} {st.home.score}-{st.away.score} {st.away.team.name}**\n"
                f"Status: {st.status} | Phase: {st.phase} | Minute: {st.minute}'\n"
                f"Mode: {st.mode.replace('_',' ').title()}\n"
                f"Pending break: {st.pending_break or 'none'}\n"
                f"Home ready: {st.home_break_decision_done} | Away ready: {st.away_break_decision_done}"
            )
            await interaction.followup.send(embed=info_embed(f"Match Dashboard — {st.id}", desc), ephemeral=True)
            return

        # list active in memory
        sessions = list(ms.sessions.values())
        if not sessions:
            await interaction.followup.send(embed=info_embed("Dashboard", "No active matches in memory."), ephemeral=True)
            return
        lines = []
        for s in sessions[:15]:
            st = s.state
            lines.append(f"`{st.id}` — {st.home.team.name} {st.home.score}-{st.away.score} {st.away.team.name} ({st.status}, {st.minute}')")
        await interaction.followup.send(embed=info_embed("Active Matches", "\n".join(lines)), ephemeral=True)


async def setup(bot):
    await bot.add_cog(DashboardCog(bot))
