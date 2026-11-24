from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import error_embed


def _fmt_age(p: dict) -> str:
    age = p.get("age")
    return str(age) if age is not None else "-"


def _fmt_num(p: dict) -> str:
    n = p.get("number")
    return f"#{n}" if n is not None else "#-"


def _fmt_rating(p: dict) -> str:
    ovr = p.get("overall")
    pot = p.get("potential")
    if ovr is None and pot is None:
        return "OVR - / POT -"
    ovr_s = str(ovr) if ovr is not None else "-"
    pot_s = str(pot) if pot is not None else "-"
    return f"OVR {ovr_s} / POT {pot_s}"


def _pos_order(pos: str | None) -> int:
    p = (pos or "").upper()
    return {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}.get(p, 9)


class SquadCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="squad", description="Show all players for a team")
    @app_commands.describe(team="Team name (e.g. Manchester United)")
    async def squad(self, interaction: discord.Interaction, team: str):
        await interaction.response.defer()
        fd = self.bot.football_data
        try:
            matches = await fd.list_teams(query=team, limit=10)
        except Exception:
            matches = []
        if not matches:
            await interaction.followup.send(embed=error_embed("Team Not Found", f"No team found for '{team}'."), ephemeral=True)
            return

        # best match: exact, then startswith, else first
        ql = team.lower().strip()
        picked = None
        for m in matches:
            if (m.get("name") or "").lower() == ql or (m.get("short_name") or "").lower() == ql:
                picked = m
                break
        if picked is None:
            for m in matches:
                if (m.get("name") or "").lower().startswith(ql):
                    picked = m
                    break
        if picked is None:
            picked = matches[0]

        try:
            squad = await fd.get_squad(int(picked.get("api_id") or 0))
        except Exception:
            squad = []

        if not squad:
            await interaction.followup.send(embed=error_embed("No Players", f"No players found for {picked.get('name','team')}."), ephemeral=True)
            return

        # sort by position then number/name
        squad = sorted(squad, key=lambda p: (_pos_order(p.get("position")), (p.get("number") if p.get("number") is not None else 999), (p.get("name") or "")))

        header = f"**{picked.get('name','Team')}**"
        league = picked.get("league_name")
        if league:
            header += f" — {league}"
        header += f"\nPlayers: {len(squad)}"

        lines = []
        for p in squad:
            pos = (p.get("detailed_position") or p.get("position") or "?")
            coarse = p.get("position") or "?"
            pos_txt = pos if pos == coarse else f"{pos}/{coarse}"
            nat = p.get("nationality") or ""
            nat_txt = f" • {nat}" if nat else ""
            status = str(p.get("status") or "active").lower()
            inj = " • INJURED" if p.get("injured") or status == "injured" else (" • " + status.upper() if status not in ("active", "") else "")
            lines.append(f"{_fmt_num(p):>3} **{p.get('name','Unknown')}** — {pos_txt} • {_fmt_rating(p)} • Age {_fmt_age(p)}{nat_txt}{inj}")

        # chunk into messages under 1900 chars
        chunks = []
        cur = header + "\n\n"
        for ln in lines:
            if len(cur) + len(ln) + 1 > 1850:
                chunks.append(cur.rstrip())
                cur = ""
            cur += ln + "\n"
        if cur.strip():
            chunks.append(cur.rstrip())

        for i, ch in enumerate(chunks[:6]):
            if i == 0:
                await interaction.followup.send(ch)
            else:
                await interaction.followup.send(ch)


async def setup(bot):
    await bot.add_cog(SquadCog(bot))
