from __future__ import annotations

import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import info_embed, success_embed, error_embed
from db.database import utc_now_iso
from services.press_service import PressService

logger = logging.getLogger(__name__)


def _parse_press_questions(raw: Any) -> list[str]:
    if isinstance(raw, list):
        qs = [str(x).strip() for x in raw if str(x).strip()]
        return qs[:3] if qs else []
    s = str(raw or "").strip()
    if not s:
        return []
    try:
        data = json.loads(s)
        if isinstance(data, list):
            qs = [str(x).strip() for x in data if str(x).strip()]
            if qs:
                return qs[:3]
    except Exception:
        pass
    # fallback: split by numbered lines
    parts = [p.strip(" -") for p in re.split(r"\n\s*(?:\d+[\).]\s*)", s) if p.strip()]
    if len(parts) >= 2:
        return parts[:3]
    return [s]


class PressAnswerModal(discord.ui.Modal, title="Post-Match Press Conference"):
    a1 = discord.ui.TextInput(
        label="Answer 1",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=900,
        placeholder="Your response to question 1...",
    )
    a2 = discord.ui.TextInput(
        label="Answer 2",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=900,
        placeholder="Your response to question 2...",
    )
    a3 = discord.ui.TextInput(
        label="Answer 3",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=900,
        placeholder="Your response to question 3...",
    )

    def __init__(self, cog: "PressCog", press_id: int, questions: list[str]):
        super().__init__()
        self.cog = cog
        self.press_id = press_id
        self.questions = questions[:3]
        # Put question snippets into placeholders (Discord limit 100)
        for i, inp in enumerate((self.a1, self.a2, self.a3)):
            q = self.questions[i] if i < len(self.questions) else f"Question {i+1}"
            inp.placeholder = (q[:97] + "...") if len(q) > 100 else q

    async def on_submit(self, interaction: discord.Interaction):
        answers = [str(self.a1.value or "").strip(), str(self.a2.value or "").strip(), str(self.a3.value or "").strip()]
        await self.cog.handle_press_answer(interaction, self.press_id, answers)


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
                perms = interaction.user.guild_permissions if interaction.guild else None
                is_admin = bool(perms and (perms.administrator or perms.manage_guild))
                if not is_admin:
                    await interaction.response.send_message("Only the team manager can respond to this press conference.", ephemeral=True)
                    return
            questions = _parse_press_questions(pr.get("question"))
            if not questions:
                try:
                    ctx = json.loads(pr.get("context_json") or "{}")
                    questions = ctx.get("questions") or ([ctx.get("question")] if ctx.get("question") else [])
                except Exception:
                    questions = []
            while len(questions) < 3:
                questions.append("What is your message after this match and what must improve next?")
            await interaction.response.send_modal(PressAnswerModal(self, press_id, questions[:3]))
        except Exception:
            logger.exception("press interaction failed")

    async def handle_press_answer(self, interaction: discord.Interaction, press_id: int, answers: list[str]):
        await interaction.response.defer(ephemeral=True)
        row = await self.bot.db.fetchone("SELECT * FROM press_conferences WHERE id=?", (int(press_id),))
        if not row:
            await interaction.followup.send(embed=error_embed("Not Found", "Press conference not found."), ephemeral=True)
            return
        pr = dict(row)
        if pr.get("status") != "open":
            await interaction.followup.send(embed=error_embed("Closed", "This press conference is already closed."), ephemeral=True)
            return
        answers = [str(a or "").strip() for a in (answers or [])]
        if len(answers) < 3 or any(not a for a in answers[:3]):
            await interaction.followup.send(embed=error_embed("Incomplete Answers", "Please answer all 3 press questions."), ephemeral=True)
            return
        questions = _parse_press_questions(pr.get("question"))
        if len(questions) < 3:
            try:
                ctx = json.loads(pr.get("context_json") or "{}")
                questions = ctx.get("questions") or questions
            except Exception:
                pass
        while len(questions) < 3:
            questions.append("What is your message after this match?")

        payload = {"questions": questions[:3], "answers": answers[:3]}
        await self.bot.db.execute(
            "INSERT INTO press_answers(press_id,user_id,answer_text,created_at) VALUES(?,?,?,?)",
            (int(press_id), interaction.user.id, json.dumps(payload), utc_now_iso()),
        )
        await self.bot.db.execute(
            "UPDATE press_conferences SET status='answered', updated_at=? WHERE id=?",
            (utc_now_iso(), int(press_id)),
        )

        team = pr.get("team_name") or "Manager"
        lines = [f"**{team} manager (<@{interaction.user.id}>):**\n"]
        for i, (q, a) in enumerate(zip(questions[:3], answers[:3]), start=1):
            lines.append(f"**Q{i}.** {q}\n**A{i}.** {a}\n")
        embed = success_embed("Post-Match Press Conference", "\n".join(lines)[:3900])
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
        questions = _parse_press_questions(pr.get("question"))
        if not questions:
            try:
                ctx = json.loads(pr.get("context_json") or "{}")
                questions = ctx.get("questions") or [ctx.get("question")] or []
            except Exception:
                questions = []
        while len(questions) < 3:
            questions.append("What is your message after this match and what must improve next?")
        team = pr.get("team_name")
        who = f" for **{team}** manager" if team else ""
        target = pr.get("manager_user_id")
        mention = f"<@{int(target)}>" if target else ""
        qblock = "\n\n".join([f"**Q{i}.** {q}" for i, q in enumerate(questions[:3], start=1)])
        desc = f"**Post-match press conference{who}**\n\n{qblock}\n\nUse the **Respond** button to answer all 3 questions."
        embed = info_embed("Post-Match Press Conference", desc[:3900])
        view = PressRespondView(self, press_id)
        try:
            msg = await channel.send(content=mention or None, embed=embed, view=view)
            await self.bot.db.execute("UPDATE press_conferences SET message_id=?, channel_id=?, updated_at=? WHERE id=?", (msg.id, getattr(channel, "id", None), utc_now_iso(), press_id))
        except Exception:
            logger.exception("Failed to send press conference message")


async def setup(bot):
    await bot.add_cog(PressCog(bot))
