from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import error_embed, info_embed


def _fmt_player(p: dict) -> str:
    name = p.get("name") or "Unknown"
    firstname = (p.get("firstname") or "").strip()
    lastname = (p.get("lastname") or "").strip()
    if not lastname:
        parts = name.split()
        lastname = parts[-1] if parts else name
        if not firstname and len(parts) > 1:
            firstname = " ".join(parts[:-1])

    club = p.get("team_name") or "Free Agent"
    league = p.get("league_name") or ""
    pos = p.get("detailed_position") or p.get("position") or "?"
    coarse = p.get("position") or "?"
    ovr = p.get("overall")
    pot = p.get("potential")
    nat = p.get("nationality") or "?"
    age = p.get("age")
    dob = p.get("dob")
    number = p.get("number")
    foot = p.get("preferred_foot")
    status = p.get("status") or "active"

    rating_txt = f"**OVR {ovr}**" if ovr is not None else "**OVR N/A**"
    if pot is not None:
        rating_txt += f" / POT {pot}"

    bits = [f"{pos} ({coarse})"]
    if number is not None:
        bits.append(f"#{number}")
    if nat and nat != "?":
        bits.append(nat)
    if age is not None:
        bits.append(f"Age {age}")
    elif dob:
        bits.append(f"DOB {dob}")
    if foot:
        bits.append(foot)
    if status and status.lower() != "active":
        bits.append(status.upper())

    lines = [f"**{name}**"]
    if lastname:
        lines.append(f"Last name: {lastname}")
    lines.append(f"{club}" + (f" — {league}" if league else ""))
    lines.append(f"{rating_txt} • " + " • ".join(bits))
    return "\n".join(lines)


class RatingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ratings", description="Search player ratings by name")
    @app_commands.describe(
        name="Player name to search for (full or partial)",
        limit="Max results to show (1-15, default 10)",
    )
    async def ratings(self, interaction: discord.Interaction, name: str, limit: app_commands.Range[int, 1, 15] = 10):
        await interaction.response.defer(ephemeral=True)
        query = (name or "").strip()
        if len(query) < 2:
            await interaction.followup.send(embed=error_embed("Invalid Query", "Please enter at least 2 characters."), ephemeral=True)
            return

        try:
            results = await self.bot.football_data.search_players(query=query, limit=int(limit))
        except Exception as e:
            await interaction.followup.send(embed=error_embed("Search Error", f"Could not search players: {e}"), ephemeral=True)
            return

        if not results:
            await interaction.followup.send(embed=info_embed("No Results", f"No players found for `{query}`."), ephemeral=True)
            return

        blocks = [_fmt_player(p) for p in results]
        desc = "\n\n".join(blocks)
        # Discord embed description limit is 4096
        if len(desc) > 3900:
            trimmed = []
            total = 0
            for b in blocks:
                add = len(b) + (2 if trimmed else 0)
                if total + add > 3800:
                    break
                trimmed.append(b)
                total += add
            remaining = len(blocks) - len(trimmed)
            desc = "\n\n".join(trimmed)
            if remaining > 0:
                desc += f"\n\n… and {remaining} more. Narrow your search."

        title = f"Player Ratings — \"{query}\""
        em = info_embed(title, desc)
        em.set_footer(text=f"{len(results)} result(s) • OVR/POT from 2026-27 dataset")
        await interaction.followup.send(embed=em, ephemeral=True)


async def setup(bot):
    await bot.add_cog(RatingsCog(bot))
