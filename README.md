# Discord Football Manager & Match Simulation Bot

A professional Discord bot for managing football teams and simulating realistic matches across the **top 5 European leagues**:

- English Premier League
- Spanish La Liga
- German Bundesliga
- Italian Serie A
- French Ligue 1

The bot includes bundled real club names and real player squads (no invented players in bundled data), an event-driven match engine, tactical breaks, and full-time reports.

## Features

- `/sim` — start a simulation with interactive team selection across all top-5 leagues
- Modes:
  - **Bot vs Bot** — AI controls both sides
  - **Manager vs Manager** — two human managers (with AI fallback/auto-assist at breaks)
- Real team and player data bundled in `data/bundled/` (no API key required)
- Lineup/tactics support:
  - default/auto lineups generated from squad
  - `/lineup` and `/tactics` commands for manager submissions/updates
- Realistic simulation systems:
  - formations and tactical profiles
  - stamina, ratings, statistics (shots, xG, possession, cards, saves, etc.)
  - event-driven commentary (goals, chances, fouls, cards, VAR, injuries, subs, stoppage time)
  - mandatory tactical breaks at **HT (45')**, **60'**, **75'**
  - up to 5 substitutions per side
- Full-time report with scoreline, goalscorers, stats, player ratings/stamina, discipline, injuries, substitutions, and tactical analysis
- SQLite persistence for matches, lineups, tactics, and cached data

## Requirements

- Python **3.11+**
- Discord bot token (Discord Developer Portal)
- No football API key required (bundled data is the default)

## Quick Start (Local)

1. **Clone the repository**
   ```bash
   git clone https://github.com/parofaro32-sketch/Top-5-Leagues.git
   cd Top-5-Leagues
   ```

2. **Create and activate a virtual environment** (recommended)
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Linux/macOS
   # .venv\Scripts\activate   # Windows
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**

   Copy `.env.example` to `.env` and set your Discord bot token:
   ```bash
   cp .env.example .env
   ```
   Edit `.env`:
   ```env
   DISCORD_BOT_TOKEN=your_discord_bot_token_here
   FOOTBALL_DATA_SOURCE=bundled
   ```
   > The Discord bot token is a secret. Do not commit `.env` to git.

5. **Run the bot**
   ```bash
   python main.py
   ```

6. **Invite the bot to your server** with scopes `bot` and `applications.commands`.

## Deploy on Railway

1. Push this repository to GitHub (Railway deploys from git commits).
2. In Railway, create a new project from the GitHub repo.
3. Set **Variables** (Settings → Variables):
   - `DISCORD_BOT_TOKEN` = your Discord bot token
   - `FOOTBALL_DATA_SOURCE` = `bundled` (recommended; no API key needed)
   - `FOOTBALL_API_KEY` = *(optional, leave empty)*
   - `DATABASE_PATH` = `./football_bot.sqlite3` *(optional, default)*
   - `LOG_LEVEL` = `INFO` *(optional)*
4. Start command / process:
   - This repo includes a `Procfile` with: `worker: python main.py`
   - Railway can run it as a worker/service process.
5. Deploy/redeploy and check logs for successful login and command sync.

> **Important:** Railway needs an actual git commit on the branch it deploys. If you see "cannot find latest commit", make sure your changes are committed and pushed to the configured branch.

## Slash Commands

- `/sim` — start a match simulation
  - choose mode (Bot vs Bot / Manager vs Manager)
  - optionally specify league and team names, or use interactive selectors
  - in Manager vs Manager, provide `away_manager` (and optionally `home_manager`)
- `/dashboard [match_id]` — view match status
- `/lineup` — submit/auto-build a lineup for a managed side in a match
- `/tactics` — submit tactical settings for a managed side in a match

## Match Flow

1. Match setup and team selection
2. Lineups/tactics initialized (auto-built by default; managers can adjust)
3. Simulation runs with minute-by-minute commentary
4. Tactical breaks pause play at:
   - Half-time (45')
   - 60'
   - 75'
5. At breaks, managers can confirm readiness / apply AI-assisted adjustments; AI sides auto-decide
6. Full-time report is posted with statistics, ratings, and analysis

## Football Data

- **Default:** `FOOTBALL_DATA_SOURCE=bundled`
  - Uses bundled real teams and real player names from `data/bundled/leagues/*.json`
  - Covers all clubs in the 5 leagues included in the bundled dataset
  - No external football API key required
- **Optional live API mode:** `FOOTBALL_DATA_SOURCE=api` (or `auto`) with `FOOTBALL_API_KEY`
  - Intended for API-Football (api-sports.io) integration where configured
  - Bundled mode remains the recommended default for zero-setup deployment

## Project Structure

- `main.py` — bot entry point
- `config.py` — environment/settings loading
- `bot/` — Discord client, cogs, UI components
- `engine/` — simulation engine, tactics, stamina, ratings, commentary, reports
- `services/` — lineup/tactics/match orchestration services
- `data/` — bundled football data and data access layer
- `db/` — SQLite schema and persistence

## Notes

- This environment-agnostic project is designed to run as a long-lived bot process (e.g. Railway worker).
- The simulation is a game simulation engine intended for interactive entertainment and analysis, not an official real-world match feed.
- Regenerate and rotate your Discord bot token if it has ever been exposed publicly.
