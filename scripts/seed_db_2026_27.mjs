#!/usr/bin/env node
/**
 * Idempotent seeder for the 2026/27 top-5 league bundled database.
 * Usage:
 *   node scripts/seed_db_2026_27.mjs [--db ./football_bot.sqlite3] [--season 2026]
 *
 * Reads data/bundled/leagues/*.json and upserts teams/players into SQLite.
 * Safe to run multiple times.
 */
import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const args = process.argv.slice(2);
function getArg(name, def) {
  const i = args.indexOf(name);
  if (i >= 0 && args[i + 1]) return args[i + 1];
  return def;
}
const DB_PATH = getArg('--db', process.env.DATABASE_PATH || './football_bot.sqlite3');
const SEASON = parseInt(getArg('--season', process.env.FOOTBALL_API_SEASON || '2026'), 10);
const ROOT = process.cwd();
const LEAGUES_DIR = path.join(ROOT, 'data', 'bundled', 'leagues');

function nowIso() { return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'); }

function ensureSchema(db) {
  db.exec(`
    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS teams (
      api_id INTEGER NOT NULL, season INTEGER NOT NULL, league_id INTEGER, league_name TEXT,
      country TEXT, name TEXT NOT NULL, short_name TEXT, code TEXT, logo TEXT, venue_name TEXT,
      raw_json TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now')),
      PRIMARY KEY (api_id, season)
    );
    CREATE TABLE IF NOT EXISTS players (
      api_id INTEGER NOT NULL, season INTEGER NOT NULL, team_api_id INTEGER NOT NULL,
      name TEXT NOT NULL, firstname TEXT, lastname TEXT, age INTEGER, dob TEXT, nationality TEXT,
      photo TEXT, position TEXT, detailed_position TEXT, number INTEGER, injured INTEGER NOT NULL DEFAULT 0,
      height_cm INTEGER, weight_kg INTEGER, preferred_foot TEXT, overall REAL, potential REAL,
      status TEXT DEFAULT 'active', attributes_json TEXT, statistics_json TEXT, raw_json TEXT,
      updated_at TEXT NOT NULL DEFAULT (datetime('now')),
      PRIMARY KEY (api_id, team_api_id, season)
    );
    CREATE INDEX IF NOT EXISTS idx_players_team_season ON players(team_api_id, season);
    CREATE INDEX IF NOT EXISTS idx_players_api_season ON players(api_id, season);
    CREATE TABLE IF NOT EXISTS player_career_stats (
      player_api_id INTEGER PRIMARY KEY, name TEXT, current_team_api_id INTEGER,
      matches_played INTEGER NOT NULL DEFAULT 0, starts INTEGER NOT NULL DEFAULT 0, minutes_played INTEGER NOT NULL DEFAULT 0,
      goals INTEGER NOT NULL DEFAULT 0, assists INTEGER NOT NULL DEFAULT 0, yellow_cards INTEGER NOT NULL DEFAULT 0,
      red_cards INTEGER NOT NULL DEFAULT 0, clean_sheets INTEGER NOT NULL DEFAULT 0, saves INTEGER NOT NULL DEFAULT 0,
      rating_sum REAL NOT NULL DEFAULT 0, rating_count INTEGER NOT NULL DEFAULT 0, avg_rating REAL NOT NULL DEFAULT 0,
      potm_awards INTEGER NOT NULL DEFAULT 0, trophies INTEGER NOT NULL DEFAULT 0,
      current_ovr REAL, current_pot REAL, previous_ovr REAL,
      rating_progression_json TEXT, career_history_json TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS api_cache_meta (
      cache_key TEXT PRIMARY KEY, value_json TEXT, expires_at TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
  `);
}

function asInt(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}
function asReal(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function main() {
  if (!fs.existsSync(LEAGUES_DIR)) {
    console.error('Leagues dir not found:', LEAGUES_DIR);
    process.exit(1);
  }
  const files = fs.readdirSync(LEAGUES_DIR).filter(f => f.endsWith('.json')).sort();
  if (files.length === 0) { console.error('No league json files found'); process.exit(1); }

  const db = new DatabaseSync(DB_PATH);
  ensureSchema(db);

  const upsertTeam = db.prepare(`
    INSERT INTO teams(api_id,season,league_id,league_name,country,name,short_name,code,logo,venue_name,raw_json,updated_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(api_id,season) DO UPDATE SET
      league_id=excluded.league_id,league_name=excluded.league_name,country=excluded.country,name=excluded.name,
      short_name=excluded.short_name,code=excluded.code,logo=excluded.logo,venue_name=excluded.venue_name,
      raw_json=excluded.raw_json,updated_at=excluded.updated_at
  `);
  const upsertPlayer = db.prepare(`
    INSERT INTO players(api_id,season,team_api_id,name,firstname,lastname,age,dob,nationality,photo,position,detailed_position,number,injured,height_cm,weight_kg,preferred_foot,overall,potential,status,attributes_json,statistics_json,raw_json,updated_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(api_id,team_api_id,season) DO UPDATE SET
      name=excluded.name,firstname=excluded.firstname,lastname=excluded.lastname,age=excluded.age,dob=excluded.dob,nationality=excluded.nationality,
      photo=excluded.photo,position=excluded.position,detailed_position=excluded.detailed_position,number=excluded.number,
      injured=excluded.injured,height_cm=excluded.height_cm,weight_kg=excluded.weight_kg,preferred_foot=excluded.preferred_foot,
      overall=excluded.overall,potential=excluded.potential,status=excluded.status,
      attributes_json=excluded.attributes_json,statistics_json=excluded.statistics_json,raw_json=excluded.raw_json,updated_at=excluded.updated_at
  `);
  const upsertCareer = db.prepare(`
    INSERT INTO player_career_stats(player_api_id,name,current_team_api_id,current_ovr,current_pot,previous_ovr,updated_at)
    VALUES(?,?,?,?,?,?,?)
    ON CONFLICT(player_api_id) DO UPDATE SET
      name=excluded.name,
      current_team_api_id=excluded.current_team_api_id,
      current_ovr=COALESCE(player_career_stats.current_ovr, excluded.current_ovr),
      current_pot=COALESCE(player_career_stats.current_pot, excluded.current_pot),
      previous_ovr=COALESCE(player_career_stats.previous_ovr, excluded.previous_ovr),
      updated_at=excluded.updated_at
  `);
  const setMeta = db.prepare(`
    INSERT INTO api_cache_meta(cache_key,value_json,expires_at,updated_at) VALUES(?,?,?,?)
    ON CONFLICT(cache_key) DO UPDATE SET value_json=excluded.value_json,expires_at=excluded.expires_at,updated_at=excluded.updated_at
  `);

  let teamCount = 0, playerCount = 0;
  const now = nowIso();

  db.exec('BEGIN IMMEDIATE');
  try {
    for (const file of files) {
      const data = JSON.parse(fs.readFileSync(path.join(LEAGUES_DIR, file), 'utf8'));
      const leagueName = data.league_name || data.name || null;
      const leagueId = asInt(data.league_id);
      const country = data.country || null;
      for (const t of (data.teams || [])) {
        const teamId = asInt(t.api_id);
        if (!teamId) continue;
        upsertTeam.run(
          teamId, SEASON, leagueId, leagueName, country,
          t.name || 'Unknown', t.short_name || t.code || null, t.code || t.short_name || null,
          t.logo || null, t.venue_name || t.venue || null, JSON.stringify(t), now
        );
        teamCount++;
        for (const p of (t.players || [])) {
          const pid = asInt(p.api_id);
          if (!pid) continue;
          const name = p.name || 'Unknown';
          const firstname = p.firstname || null;
          const lastname = p.lastname || null;
          upsertPlayer.run(
            pid, SEASON, teamId, name, firstname, lastname,
            asInt(p.age), p.dob || null, p.nationality || null, p.photo || null,
            p.position || null, p.detailed_position || p.position || null, asInt(p.number),
            p.injured ? 1 : 0, asInt(p.height_cm), asInt(p.weight_kg), p.preferred_foot || null,
            asReal(p.overall), asReal(p.potential), p.status || 'active',
            JSON.stringify(p.attributes || {}), JSON.stringify(p.statistics || {}), JSON.stringify(p), now
          );
          upsertCareer.run(pid, name, teamId, asReal(p.overall), asReal(p.potential), asReal(p.previous_ovr), now);
          playerCount++;
        }
      }
    }
    setMeta.run('bundled_seed_v7_2026_27_upgrade', JSON.stringify({ ok: true, season: SEASON, seeded_at: now, source: 'scripts/seed_db_2026_27.mjs' }), null, now);
    db.exec('COMMIT');
  } catch (err) {
    try { db.exec('ROLLBACK'); } catch {}
    db.close();
    throw err;
  }
  db.close();

  console.log(JSON.stringify({ ok: true, db: DB_PATH, season: SEASON, teams: teamCount, players: playerCount, files }, null, 2));
}

main();
