from __future__ import annotations

import discord
from typing import Any


def color_primary() -> discord.Color:
    return discord.Color.blurple()


def color_success() -> discord.Color:
    return discord.Color.green()


def color_warn() -> discord.Color:
    return discord.Color.gold()


def color_error() -> discord.Color:
    return discord.Color.red()


def clamp_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…(truncated)"


def setup_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color_primary())


def info_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color_primary())


def success_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color_success())


def error_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color_error())


def match_header_embed(match_id: str, home: str, away: str, mode: str) -> discord.Embed:
    em = discord.Embed(title=f"Match Setup: {home} vs {away}", color=color_primary())
    em.add_field(name="Match ID", value=f"`{match_id}`", inline=True)
    em.add_field(name="Mode", value=mode.replace("_", " ").title(), inline=True)
    return em


def commentary_chunk_embed(title: str, lines: list[str], scoreline: str | None = None) -> discord.Embed:
    desc = "\n".join(lines) if lines else "No events."
    em = discord.Embed(title=title, description=clamp_text(desc, 3900), color=color_primary())
    if scoreline:
        em.set_footer(text=scoreline)
    return em


def break_embed(state: dict[str, Any]) -> discord.Embed:
    score = state.get("score", {})
    stats = state.get("statistics", {})
    h = stats.get("home", {})
    a = stats.get("away", {})
    title = f"Tactical Break — {score.get('scoreline','Match')}"
    desc = [f"Minute: **{state.get('minute',0)}'**", f"Phase: **{state.get('phase','break')}**", ""]
    desc.append("**Statistics**")
    desc.append(f"Possession: {h.get('possession',0)}% - {a.get('possession',0)}%")
    desc.append(f"Shots (on target): {h.get('shots',0)} ({h.get('shots_on_target',0)}) - {a.get('shots',0)} ({a.get('shots_on_target',0)})")
    desc.append(f"xG: {h.get('xg',0)} - {a.get('xg',0)}")
    desc.append(f"Corners: {h.get('corners',0)} - {a.get('corners',0)}")
    desc.append(f"Fouls: {h.get('fouls',0)} - {a.get('fouls',0)}")
    desc.append(f"Cards (Y/R): {h.get('yellow_cards',0)}/{h.get('red_cards',0)} - {a.get('yellow_cards',0)}/{a.get('red_cards',0)}")
    em = discord.Embed(title=title, description="\n".join(desc), color=color_warn())
    em.set_footer(text="Managers can make substitutions/tactical changes, then confirm ready. AI sides auto-decide.")
    return em


def report_embeds(report: dict[str, Any]) -> list[discord.Embed]:
    fs = report.get("final_score", {})
    stats = report.get("statistics", {})
    h = stats.get("home", {})
    a = stats.get("away", {})
    main = discord.Embed(title=f"Full-Time: {fs.get('scoreline','')}", color=color_success())
    lines = []
    goals = report.get("goalscorers", [])
    if goals:
        gtxt = []
        for g in goals:
            m = f"{g.get('minute',0)}{('+'+str(g.get('extra_minute'))) if g.get('extra_minute') else ''}'"
            who = g.get('scorer') or 'Unknown'
            team = g.get('team') or ''
            ast = f" (assist: {g.get('assist')})" if g.get('assist') else ''
            gtxt.append(f"• {m} {who} — {team}{ast}")
        lines.append("**Goalscorers**\n" + "\n".join(gtxt))
    else:
        lines.append("**Goalscorers**\n• None")
    lines.append("\n**Match Statistics**")
    lines.append(f"Possession: {h.get('possession',0)}% - {a.get('possession',0)}%")
    lines.append(f"Shots (on target): {h.get('shots',0)} ({h.get('shots_on_target',0)}) - {a.get('shots',0)} ({a.get('shots_on_target',0)})")
    lines.append(f"xG: {h.get('xg',0)} - {a.get('xg',0)}")
    lines.append(f"Corners: {h.get('corners',0)} - {a.get('corners',0)}")
    lines.append(f"Fouls: {h.get('fouls',0)} - {a.get('fouls',0)}")
    lines.append(f"Cards (Y/R): {h.get('yellow_cards',0)}/{h.get('red_cards',0)} - {a.get('yellow_cards',0)}/{a.get('red_cards',0)}")
    lines.append(f"Pass accuracy: {h.get('pass_accuracy',0)}% - {a.get('pass_accuracy',0)}%")
    motm = report.get("man_of_the_match")
    if motm:
        lines.append(f"\n**Man of the Match**: {motm.get('name')} ({motm.get('team')}) — {motm.get('rating')}")
    main.description = clamp_text("\n".join(lines), 3900)

    # Player ratings embed (top contributors + all by team, summarized)
    def fmt_team(side_key: str, label: str) -> str:
        players = (report.get('player_ratings') or {}).get(side_key, [])
        parts = []
        for p in players:
            if p.get('minutes_played', 0) <= 0 and not p.get('on_pitch'):
                continue
            tag = []
            if p.get('goals'): tag.append(f"{p['goals']}G")
            if p.get('assists'): tag.append(f"{p['assists']}A")
            if p.get('red_card'): tag.append("RC")
            elif p.get('yellow_cards'): tag.append(f"{p['yellow_cards']}Y")
            if p.get('injured'): tag.append("INJ")
            extra = f" ({', '.join(tag)})" if tag else ""
            parts.append(f"{p.get('name')} [{p.get('position')}] — {p.get('rating')}★, {p.get('minutes_played')}' , STA {p.get('stamina')}{extra}")
        if not parts:
            return f"**{label}**\nNo participants"
        return f"**{label}**\n" + "\n".join(parts[:20]) + ("\n…" if len(parts) > 20 else "")

    ratings = discord.Embed(title="Player Ratings & Participation", color=color_primary())
    ratings.description = clamp_text(fmt_team('home', fs.get('home_team','Home')) + "\n\n" + fmt_team('away', fs.get('away_team','Away')), 3900)

    # Discipline, injuries, subs (no tactical analysis - tactics are private)
    extra = discord.Embed(title="Match Notes", color=color_primary())
    blocks = []
    disc = report.get('discipline') or []
    if disc:
        d = [f"• {x.get('minute')}' {x.get('player')} ({x.get('team')}) — {x.get('type').replace('_',' ')}" for x in disc[:12]]
        blocks.append("**Discipline**\n" + "\n".join(d))
    inj = report.get('injuries') or []
    if inj:
        il = [f"• {x.get('minute')}' {x.get('player')} ({x.get('team')}) — {x.get('severity')} ({'continued' if x.get('continued') else 'substituted' if x.get('substituted') else 'off'})" for x in inj[:12]]
        blocks.append("**Injuries**\n" + "\n".join(il))
    subs = report.get('substitutions') or []
    if subs:
        sl = [f"• {x.get('minute')}' {x.get('team')}: {x.get('on_name')} for {x.get('off_name')}" + (f" ({x.get('reason')})" if x.get('reason') else '') for x in subs[:15]]
        blocks.append("**Substitutions**\n" + "\n".join(sl))
    extra.description = clamp_text("\n\n".join(blocks) if blocks else "No additional notes.", 3900)
    return [main, ratings, extra]
