from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import error_embed, info_embed
from services.transfer_market_service import TransferMarketService, fmt_money

FREE_AGENT_TEAM_API_ID = 9000001


def _fmt_player(p: dict, market: TransferMarketService | None = None) -> str:
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

    height_cm = p.get("height_cm")
    mv = None
    try:
        if market is not None:
            mv = int(market.value_player(p).market_value)
    except Exception:
        mv = None

    bits = [f"{pos} ({coarse})"]
    if number is not None:
        bits.append(f"#{number}")
    if nat and nat != "?":
        bits.append(nat)
    if age is not None:
        bits.append(f"Age {age}")
    elif dob:
        bits.append(f"DOB {dob}")
    if height_cm:
        bits.append(f"{int(height_cm)}cm")
    if foot:
        bits.append(foot)
    if status and status.lower() != "active":
        bits.append(status.upper())

    lines = [f"**{name}**"]
    if lastname:
        lines.append(f"Last name: {lastname}")
    lines.append(f"{club}" + (f" — {league}" if league else ""))
    value_txt = f" • Market Value: {fmt_money(mv)}" if mv is not None else ""
    lines.append(f"{rating_txt}{value_txt}")
    lines.append(" • ".join(bits))
    return "\n".join(lines)


class FreeAgentsPagerView(discord.ui.View):
    def __init__(self, author_id: int, pages: list[discord.Embed], timeout: float = 180):
        super().__init__(timeout=timeout)
        self.author_id = int(author_id)
        self.pages = pages
        self.index = 0
        self._sync()

    def _sync(self):
        self.prev.disabled = self.index <= 0
        self.next.disabled = self.index >= len(self.pages) - 1
        for i, b in enumerate([self.prev, self.next]):
            b.label = "Previous" if i == 0 else "Next"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and int(interaction.user.id) == self.author_id:
            return True
        await interaction.response.send_message("Only the manager who opened this list can use these buttons.", ephemeral=True)
        return False

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)


class RatingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.market = TransferMarketService(bot.db)

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

        blocks = [_fmt_player(p, self.market) for p in results]
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
        em.set_footer(text=f"{len(results)} result(s) • OVR/POT/Market Value/Height")
        await interaction.followup.send(embed=em, ephemeral=True)

    @app_commands.command(name="freeagents", description="List free agents (outside top 5 leagues / unattached) with ratings, height, and market value")
    @app_commands.describe(position="Optional position filter (GK/DEF/MID/FWD)", min_ovr="Minimum overall rating", max_ovr="Maximum overall rating")
    async def freeagents(
        self,
        interaction: discord.Interaction,
        position: str | None = None,
        min_ovr: int | None = None,
        max_ovr: int | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        await self.bot.football_data.ensure_seeded()

        season = int(getattr(self.bot.football_data, "season", 2026))
        rows = await self.bot.db.fetchall(
            "SELECT * FROM players WHERE team_api_id=? AND season=? ORDER BY overall DESC, potential DESC, name ASC",
            (FREE_AGENT_TEAM_API_ID, season),
        )
        if not rows:
            await interaction.followup.send(embed=error_embed("No Free Agents", "No free agents found in the database."), ephemeral=True)
            return

        pos_filter = (position or "").strip().upper()
        if pos_filter in {"GOALKEEPER", "KEEPER", "G"}:
            pos_filter = "GK"
        elif pos_filter in {"DEFENDER", "D"}:
            pos_filter = "DEF"
        elif pos_filter in {"MIDFIELDER", "M"}:
            pos_filter = "MID"
        elif pos_filter in {"FORWARD", "ATTACKER", "ST", "F"}:
            pos_filter = "FWD"

        players = []
        for r in rows:
            p = dict(r)
            p_pos = str(p.get("position") or "").upper()
            ovr = int(p.get("overall") or 0)
            if pos_filter and p_pos != pos_filter:
                continue
            if min_ovr is not None and ovr < int(min_ovr):
                continue
            if max_ovr is not None and ovr > int(max_ovr):
                continue
            p.setdefault("team_name", "Free Agent")
            p.setdefault("league_name", "Outside Top 5 Leagues / Free Agent")
            players.append(p)

        if not players:
            await interaction.followup.send(embed=error_embed("No Free Agents", "No free agents matched your filters."), ephemeral=True)
            return

        per_page = 8
        pages: list[discord.Embed] = []
        total_pages = (len(players) + per_page - 1) // per_page
        for i in range(total_pages):
            chunk = players[i * per_page:(i + 1) * per_page]
            lines = []
            for p in chunk:
                name = p.get("name") or "Unknown"
                p_pos = p.get("detailed_position") or p.get("position") or "?"
                ovr = p.get("overall")
                pot = p.get("potential")
                age = p.get("age")
                nat = p.get("nationality") or "?"
                height_cm = p.get("height_cm")
                foot = p.get("preferred_foot")
                mv = None
                try:
                    mv = int(self.market.value_player(p).market_value)
                except Exception:
                    mv = None
                rating_txt = f"OVR {int(ovr)}" if ovr is not None else "OVR N/A"
                if pot is not None:
                    rating_txt += f" / POT {int(pot)}"
                bits = [p_pos]
                if age is not None:
                    bits.append(f"Age {age}")
                if nat and nat != "?":
                    bits.append(str(nat))
                if height_cm:
                    bits.append(f"{int(height_cm)}cm")
                if foot:
                    bits.append(str(foot))
                mv_txt = f" • MV {fmt_money(mv)}" if mv is not None else ""
                lines.append(f"**{name}**\n{rating_txt}{mv_txt}\n{' • '.join(bits)}")
            desc = "\n\n".join(lines)
            if len(desc) > 3900:
                desc = desc[:3890] + "…"
            title = "Free Agents — Ratings & Market Value"
            em = info_embed(title, desc)
            em.set_footer(text=f"Page {i+1}/{total_pages} • {len(players)} free agent(s) • OVR/POT/Height/Market Value")
            pages.append(em)

        view = FreeAgentsPagerView(interaction.user.id, pages) if len(pages) > 1 else None
        await interaction.followup.send(embed=pages[0], view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(RatingsCog(bot))
