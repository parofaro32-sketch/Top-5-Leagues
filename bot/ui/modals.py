from __future__ import annotations

import json
import discord

from services.tactics_service import normalize_tactics, parse_tactics_text_async, sanitize_tactics_summary, tactics_summary


class TacticsJsonModal(discord.ui.Modal, title="Submit Tactics JSON"):
    tactics_json = discord.ui.TextInput(
        label="Tactics JSON (partial or full)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
        placeholder='{"attacking":{"mentality":"positive"},"defensive":{"pressing_intensity":"high"}}',
    )

    def __init__(self, on_submit_data):
        super().__init__()
        self._on_submit_data = on_submit_data

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            data = json.loads(str(self.tactics_json.value))
            if not isinstance(data, dict):
                raise ValueError("JSON must be an object")
            data = normalize_tactics(data)
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Invalid tactics JSON: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"Invalid tactics JSON: {e}", ephemeral=True)
            return
        try:
            await self._on_submit_data(interaction, data)
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Failed to save tactics: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"Failed to save tactics: {e}", ephemeral=True)


class TacticsTextModal(discord.ui.Modal, title="Confirm Tactics"):
    tactics_text = discord.ui.TextInput(
        label="Tactical instructions",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
        placeholder="e.g. 4-3-3, high press, high line, short build-up, fast transitions",
    )

    def __init__(self, on_submit_data):
        super().__init__()
        self._on_submit_data = on_submit_data

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.tactics_text.value or "").strip()
        try:
            settings = getattr(getattr(interaction, "client", None), "settings", None)
            updates = await parse_tactics_text_async(raw, settings=settings)
            updates = normalize_tactics(updates)
            summary = sanitize_tactics_summary(tactics_summary(updates))
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Could not parse tactics: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"Could not parse tactics: {e}", ephemeral=True)
            return
        try:
            await self._on_submit_data(interaction, updates, summary)
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Failed to save tactics: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"Failed to save tactics: {e}", ephemeral=True)


class LineupSubmitModal(discord.ui.Modal, title="Submit Lineup"):
    formation = discord.ui.TextInput(
        label="Formation",
        style=discord.TextStyle.short,
        required=True,
        max_length=20,
        placeholder="e.g. 4-3-3",
    )
    starters = discord.ui.TextInput(
        label="Starting XI (any format)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
        placeholder="Paste 11 players in any format: names, numbers, commas, lines, etc.",
    )
    bench = discord.ui.TextInput(
        label="Bench (optional, any format)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1500,
        placeholder="Bench players in any format (optional)",
    )

    def __init__(self, on_submit_data):
        super().__init__()
        self._on_submit_data = on_submit_data

    async def on_submit(self, interaction: discord.Interaction) -> None:
        payload = {
            "formation": str(self.formation.value or "").strip(),
            "starters_raw": str(self.starters.value or "").strip(),
            "bench_raw": str(self.bench.value or "").strip(),
        }
        try:
            await self._on_submit_data(interaction, payload)
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Failed to submit lineup: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"Failed to submit lineup: {e}", ephemeral=True)


class SubsTextModal(discord.ui.Modal, title="Submit Substitutions"):
    subs_text = discord.ui.TextInput(
        label="Substitutions (any format)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
        placeholder="Any format: Saka -> Martinelli, 7 for 11, OUT: X IN: Y, etc.",
    )

    def __init__(self, on_submit_data):
        super().__init__()
        self._on_submit_data = on_submit_data

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.subs_text.value or "").strip()
        try:
            await self._on_submit_data(interaction, raw)
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Failed to submit substitutions: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"Failed to submit substitutions: {e}", ephemeral=True)
