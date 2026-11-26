from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

from bot.ui.embeds import error_embed, info_embed
from bot.ui.modals import TacticsJsonModal
from services.tactics_service import normalize_tactics, parse_tactics_text, tactics_summary


class TacticsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="tactics", description="Submit tactical settings for your team (natural language or structured options)")
    @app_commands.describe(
        match_id="Match ID",
        side="Which side you manage",
        instructions="Natural language tactics (e.g. 'high press, play out from the back, 4-3-3, stay compact')",
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
                      instructions: str | None = None,
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
            try:
                if not inter.response.is_done():
                    await inter.response.defer(ephemeral=True)
            except Exception:
                pass
            try:
                team_state = session.state.team(s)
                team_state.tactics = normalize_tactics({**(team_state.tactics or {}), **updates})
                if session.state.mode in ("manager_vs_manager", "manager_vs_bot") and mgr is not None and session.state.status == "setup":
                    session.state.submission[f"{s}_tactics_submitted"] = True
                    session.state.submission[f"{s}_tactics_submitted_at"] = discord.utils.utcnow().isoformat()
                await session.save()
                if session.state.mode in ("manager_vs_manager", "manager_vs_bot") and session.state.status == "setup":
                    sim_cog = self.bot.get_cog("SimCog")
                    if sim_cog and hasattr(sim_cog, "_after_submission_update"):
                        try:
                            await sim_cog._after_submission_update(session, s, None)
                        except Exception:
                            pass
                await inter.followup.send(embed=info_embed("Tactics Updated", f"**{team_state.team.name}** tactics submitted privately. Confirmation: received."), ephemeral=True)
            except Exception:
                logger.exception("Failed to apply tactics command update")
                try:
                    await inter.followup.send(embed=error_embed("Tactics Error", "Failed to save tactics. Please try again."), ephemeral=True)
                except Exception:
                    pass

        if advanced_json:
            async def on_modal(inter: discord.Interaction, data: dict):
                await apply(inter, data)
            await interaction.response.send_modal(TacticsJsonModal(on_modal))
            return

        updates: dict = {}
        # Natural language instructions first
        if instructions and str(instructions).strip():
            try:
                parsed = parse_tactics_text(str(instructions))
                if isinstance(parsed, dict):
                    updates = parsed
            except Exception:
                logger.exception("Failed to parse tactics instructions")
                updates = {}

        # Explicit choices override parsed NL
        if mentality or pressing or defensive_line:
            updates.setdefault("attacking", {})
            updates.setdefault("defensive", {})
            if mentality:
                updates["attacking"]["mentality"] = mentality.value
            if pressing:
                updates["defensive"]["pressing_intensity"] = pressing.value
            if defensive_line:
                updates["defensive"]["defensive_line"] = defensive_line.value

        # Clean empty sections
        if isinstance(updates.get("attacking"), dict) and not updates["attacking"]:
            updates.pop("attacking", None)
        if isinstance(updates.get("defensive"), dict) and not updates["defensive"]:
            updates.pop("defensive", None)
        if isinstance(updates.get("set_pieces"), dict) and not updates["set_pieces"]:
            updates.pop("set_pieces", None)

        if not updates:
            # show current
            team_state = session.state.team(s)
            await interaction.response.send_message(embed=info_embed("Current Tactics", f"**{team_state.team.name}**\n{tactics_summary(team_state.tactics or {})}\n\nTip: you can also pass `instructions` in natural language, e.g. 'high press, 4-3-3, play out from the back'."), ephemeral=True)
            return
        await apply(interaction, updates)


async def setup(bot):
    await bot.add_cog(TacticsCog(bot))
