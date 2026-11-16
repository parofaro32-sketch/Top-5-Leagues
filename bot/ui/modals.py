from __future__ import annotations

import json
import discord


class TacticsJsonModal(discord.ui.Modal, title="Submit Tactics JSON"):
    tactics_json = discord.ui.TextInput(label="Tactics JSON (partial or full)", style=discord.TextStyle.paragraph, required=True, max_length=4000, placeholder='{"attacking":{"mentality":"positive"},"defensive":{"pressing_intensity":"high"}}')

    def __init__(self, on_submit_data):
        super().__init__()
        self._on_submit_data = on_submit_data

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            data = json.loads(str(self.tactics_json.value))
            if not isinstance(data, dict):
                raise ValueError("JSON must be an object")
        except Exception as e:
            await interaction.response.send_message(f"Invalid tactics JSON: {e}", ephemeral=True)
            return
        await self._on_submit_data(interaction, data)
