#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const DB_PATH = process.env.DATABASE_PATH || path.join(ROOT, 'football_bot.sqlite3');
const SEASON = 2026;
const SEED_KEY = 'bundled_seed_v6_2026_27';

function nowIso() { return new Date().toISOString().replace(/\.\d{3}Z$/, '+00:00'); }

function loadSchema() {
  const py = fs.readFileSync(path.join(ROOT, 'db/database.py'), 'utf8');
  const m = py.match(/SCHEMA = """([\s\S]*?)"""/);
  if (!m) throw new Error('SCHEMA not found in db/database.py');
  return m[1];
}

function loadLeagues() {
  const dir = path.join(ROOT, 'data/bundled/leagues');
  return fs.readdirSync(dir).filter(f => f.endsWith('.json')).sort().map(f => JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')));
}

function normalizePlayer(p, teamApiId) {
  const pos = String(p.position || 'MID').toUpperCase();
  const coarse = ({GK:'GK',CB:'DEF',LB:'DEF',RB:'DEF',LWB:'DEF',RWB:'DEF',CDM:'MID',CM:'MID',CAM:'MID',LM:'MID',RM:'MID',LW:'FWD',RW:'FWD',ST:'FWD',CF:'FWD'})[pos] || (['GK','DEF','MID','FWD'].includes(pos) ? pos : 'MID');
  return {
    ...p,
    api_id: Number(p.api_id),
    team_api_id: Number(teamApiId),
    name: p.name || 'Unknown',
    position: coarse,
    detailed_position: String(p.detailed_position || p.position || 'CM').toUpperCase(),
    status: p.status || 'active',
  };
}

const db = new DatabaseSync(DB_PATH);
db.exec('PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;');
db.exec(loadSchema());

// migrations
const migrations = [
  'ALTER TABLE players ADD COLUMN dob TEXT',
  'ALTER TABLE players ADD COLUMN height_cm INTEGER',
  'ALTER TABLE players ADD COLUMN weight_kg INTEGER',
  'ALTER TABLE players ADD COLUMN preferred_foot TEXT',
  'ALTER TABLE players ADD COLUMN overall INTEGER',
  'ALTER TABLE players ADD COLUMN potential INTEGER',
  "ALTER TABLE players ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
];
for (const sql of migrations) { try { db.exec(sql); } catch {} }

const upsertTeam = db.prepare(`INSERT INTO teams(api_id,season,league_id,league_name,country,name,short_name,code,logo,venue_name,raw_json,updated_at)
  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
  ON CONFLICT(api_id,season) DO UPDATE SET
    league_id=excluded.league_id,league_name=excluded.league_name,country=excluded.country,name=excluded.name,
    short_name=excluded.short_name,code=excluded.code,logo=excluded.logo,venue_name=excluded.venue_name,
    raw_json=excluded.raw_json,updated_at=excluded.updated_at`);

const upsertPlayer = db.prepare(`INSERT INTO players(api_id,season,team_api_id,name,firstname,lastname,age,dob,nationality,photo,position,detailed_position,number,height_cm,weight_kg,preferred_foot,overall,potential,status,injured,attributes_json,statistics_json,raw_json,updated_at)
  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  ON CONFLICT(api_id,team_api_id,season) DO UPDATE SET
    name=excluded.name,firstname=excluded.firstname,lastname=excluded.lastname,age=excluded.age,dob=excluded.dob,
    nationality=excluded.nationality,photo=excluded.photo,position=excluded.position,detailed_position=excluded.detailed_position,
    number=excluded.number,height_cm=excluded.height_cm,weight_kg=excluded.weight_kg,preferred_foot=excluded.preferred_foot,
    overall=excluded.overall,potential=excluded.potential,status=excluded.status,injured=excluded.injured,
    attributes_json=excluded.attributes_json,statistics_json=excluded.statistics_json,raw_json=excluded.raw_json,updated_at=excluded.updated_at`);

const setCache = db.prepare(`INSERT INTO api_cache_meta(cache_key,value_json,expires_at,updated_at) VALUES(?,?,?,?)
  ON CONFLICT(cache_key) DO UPDATE SET value_json=excluded.value_json,expires_at=excluded.expires_at,updated_at=excluded.updated_at`);

const leagues = loadLeagues();
let totalTeams=0, totalPlayers=0;
const now = nowIso();

db.exec('BEGIN IMMEDIATE');
try {
  for (const league of leagues) {
    for (const t of league.teams || []) {
      upsertTeam.run(
        Number(t.api_id), SEASON, league.league_id ?? null, league.league_name ?? null, league.country ?? null,
        t.name ?? null, t.short_name ?? null, t.code ?? t.short_name ?? null, t.logo ?? null, t.venue_name ?? null,
        JSON.stringify(t), now
      );
      totalTeams++;
      for (const rp of t.players || []) {
        const p = normalizePlayer(rp, t.api_id);
        upsertPlayer.run(
          Number(p.api_id), SEASON, Number(t.api_id), p.name ?? '', p.firstname ?? null, p.lastname ?? null,
          p.age ?? null, p.dob ?? null, p.nationality ?? null, p.photo ?? null, p.position ?? null, p.detailed_position ?? null,
          p.number ?? null, p.height_cm ?? null, p.weight_kg ?? null, p.preferred_foot ?? null,
          p.overall ?? null, p.potential ?? null, p.status ?? 'active',
          p.injured ? 1 : 0, JSON.stringify(p.attributes || {}), JSON.stringify(p.statistics || {}), JSON.stringify(rp), now
        );
        totalPlayers++;
      }
    }
  }
  setCache.run(SEED_KEY, JSON.stringify({ok:true, season:SEASON, seeded_at:now}), null, now);
  db.exec('COMMIT');
} catch (e) {
  try { db.exec('ROLLBACK'); } catch {}
  throw e;
}

console.log(JSON.stringify({db:DB_PATH, season:SEASON, teams:totalTeams, players:totalPlayers}));
db.close();
