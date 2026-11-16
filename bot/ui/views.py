from __future__ import annotations

import discord
from typing import Any, Callable, Awaitable


class LeagueSelect(discord.ui.Select):
    def __init__(self, leagues: list[dict[str, str]], on_select: Callable[[discord.Interaction, str], Awaitable[None]]):
        options = [discord.SelectOption(label=l["name"], value=l["key"]) for l in leagues[:25]]
        super().__init__(placeholder="Select a league", min_values=1, max_values=1, options=options)
        self._on_select = on_select

    async def callback(self, interaction: discord.Interaction):
        await self._on_select(interaction, self.values[0])


class TeamSelect(discord.ui.Select):
    def __init__(self, teams: list[dict[str, Any]], placeholder: str, on_select: Callable[[discord.Interaction, int], Awaitable[None]]):
        opts = []
        for t in teams[:25]:
            label = t.get("name") or str(t.get("api_id"))
            desc = (t.get("league_name") or "")[:100] or None
            opts.append(discord.SelectOption(label=label[:100], value=str(t["api_id"]), description=desc))
        if not opts:
            opts = [discord.SelectOption(label="No teams found", value="0")]
        super().__init__(placeholder=placeholder[:100], min_values=1, max_values=1, options=opts)
        self._on_select = on_select

    async def callback(self, interaction: discord.Interaction):
        val = int(self.values[0])
        if val == 0:
            await interaction.response.send_message("No team available in that selection.", ephemeral=True)
            return
        await self._on_select(interaction, val)


class SimpleChoiceView(discord.ui.View):
    def __init__(self, user_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("This match setup belongs to another user.", ephemeral=True)
        return False


class BreakControlView(discord.ui.View):
    def __init__(self, match_id: str, session, *, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.match_id = match_id
        self.session = session

    async def _resolve_side(self, interaction: discord.Interaction) -> str | None:
        uid = interaction.user.id if interaction.user else None
        if uid is not None and uid == self.session.home_manager_id:
            return "home"
        if uid is not None and uid == self.session.away_manager_id:
            return "away"
        return None

    @discord.ui.button(label="Confirm Ready (No Changes)", style=discord.ButtonStyle.secondary)
    async def confirm_ready(self, interaction: discord.Interaction, button: discord.ui.Button):
        side = await self._resolve_side(interaction)
        if not side:
            await interaction.response.send_message("Only assigned human managers can confirm for their side in manager mode.", ephemeral=True)
            return
        await self.session.apply_break_actions(side, substitutions=[], tactics_updates={}, summary=None)
        await interaction.response.send_message(f"{side.title()} side marked ready.", ephemeral=True)

    @discord.ui.button(label="AI Suggest & Ready (My Side)", style=discord.ButtonStyle.primary)
    async def ai_ready(self, interaction: discord.Interaction, button: discord.ui.Button):
        side = await self._resolve_side(interaction)
        if not side:
            await interaction.response.send_message("Only assigned human managers can use this for their side.", ephemeral=True)
            return
        from engine.ai_manager import decide_ai_break_actions
        actions = decide_ai_break_actions(self.session.state, side)
        await self.session.apply_break_actions(side, substitutions=actions.get("substitutions", []), tactics_updates=actions.get("tactics_updates", {}), summary="Manager accepted AI adjustment")
        subs = actions.get("substitutions", [])
        txt = "AI adjustments applied and side marked ready."
        if subs:
            txt += " Subs: " + ", ".join([f"{s.get('on_name')} for {s.get('off_name')}" for s in subs])
        await interaction.response.send_message(txt[:1900], ephemeral=True)
