from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import info_embed, success_embed, error_embed
from db.database import utc_now_iso
from services.press_service import PressService

logger = logging.getLogger(__name__)


class PressAnswerModal(discord.ui.Modal, title="Press Conference Response"):
    answer = discord.ui.TextInput(
        label="Your answer",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
        placeholder="Type your response to the press question...",
    )

    def __init__(self, cog: "PressCog", press_id: int):
        super().__init__()
        self.cog = cog
        self.press_id = press_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_press_answer(interaction, self.press_id, str(self.answer.value or "").strip())


class PressRespondView(discord.ui.View):
    def __init__(self, cog: "PressCog", press_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.press_id = int(press_id)
        self.respond.custom_id = f"press:respond:{self.press_id}"

    @discord.ui.button(label="Respond", style=discord.ButtonStyle.primary, custom_id="press:respond:0")
    async def respond(self, interaction: discord.Interaction, button: discord.ui.Button):
        return  # handled by on_interaction


class PressCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.service = PressService(bot.db)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        try:
            if interaction.type != discord.InteractionType.component:
                return
            cid = str((interaction.data or {}).get("custom_id") or "")
            if not cid.startswith("press:respond:"):
                return
            if interaction.response.is_done():
                return
            press_id = int(cid.rsplit(":", 1)[-1])
            row = await self.bot.db.fetchone("SELECT * FROM press_conferences WHERE id=?", (press_id,))
            if not row:
                await interaction.response.send_message("Press conference not found.", ephemeral=True)
                return
            pr = dict(row)
            if pr.get("status") != "open":
                await interaction.response.send_message("This press conference is already closed.", ephemeral=True)
                return
            target = pr.get("manager_user_id")
            if target is not None and int(target) != interaction.user.id:
                # allow admins? keep it simple - only target manager
                perms = interaction.user.guild_permissions if interaction.guild else None
                is_admin = bool(perms and (perms.administrator or perms.manage_guild))
                if not is_admin:
                    await interaction.response.send_message("Only the team manager can respond to this press conference.", ephemeral=True)
                    return
            await interaction.response.send_modal(PressAnswerModal(self, press_id))
        except Exception:
            logger.exception("press interaction failed")

    async def handle_press_answer(self, interaction: discord.Interaction, press_id: int, answer: str):
        await interaction.response.defer(ephemeral=True)
        row = await self.bot.db.fetchone("SELECT * FROM press_conferences WHERE id=?", (int(press_id),))
        if not row:
            await interaction.followup.send(embed=error_embed("Not Found", "Press conference not found."), ephemeral=True)
            return
        pr = dict(row)
        if pr.get("status") != "open":
            await interaction.followup.send(embed=error_embed("Closed", "This press conference is already closed."), ephemeral=True)
            return
        if not answer:
            await interaction.followup.send(embed=error_embed("Empty Answer", "Please provide an answer."), ephemeral=True)
            return

        await self.bot.db.execute(
            "INSERT INTO press_answers(press_id,user_id,answer_text,created_at) VALUES(?,?,?,?)",
            (int(press_id), interaction.user.id, answer, utc_now_iso()),
        )
        await self.bot.db.execute(
            "UPDATE press_conferences SET status='answered', updated_at=? WHERE id=?",
            (utc_now_iso(), int(press_id)),
        )

        # Update the original message to display the answer
        q = pr.get("question") or "Press question"
        team = pr.get("team_name") or "Manager"
        embed = success_embed(
            "Press Conference",
            f"**Question:** {q}\n\n**{team} manager (<@{interaction.user.id}>):**\n{answer}"
        )
        try:
            if interaction.message:
                await interaction.message.edit(embed=embed, view=None)
        except Exception:
            logger.exception("Failed to edit press message")
        try:
            await interaction.followup.send("Press response submitted.", ephemeral=True)
        except Exception:
            pass

    async def send_post_match_press(self, channel: discord.abc.Messageable, *, state: Any, report: dict[str, Any] | None = None):
        pr = await self.service.create_for_match(
            state=state, report=report, channel_id=getattr(channel, "id", None), guild_id=getattr(getattr(channel, "guild", None), "id", None)
        )
        if not pr:
            return
        press_id = int(pr.get("id"))
        q = pr.get("question") or "What are your thoughts on the match?"
        team = pr.get("team_name")
        who = f" for **{team}** manager" if team else ""
        target = pr.get("manager_user_id")
        mention = f"<@{int(target)}>" if target else ""
        desc = f"**Question{who}:**\n{q}\n\nPress respond with your answer using the button below."
        embed = info_embed("Post-Match Press Conference", desc)
        view = PressRespondView(self, press_id)
        try:
            msg = await channel.send(content=mention or None, embed=embed, view=view)
            await self.bot.db.execute("UPDATE press_conferences SET message_id=?, channel_id=?, updated_at=? WHERE id=?", (msg.id, getattr(channel, "id", None), utc_now_iso(), press_id))
        except Exception:
            logger.exception("Failed to send press conference message")


async def setup(bot):
    await bot.add_cog(PressCog(bot))
