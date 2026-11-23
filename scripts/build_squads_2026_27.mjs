#!/usr/bin/env node
/**
 * Build 2026/27 bundled squad data for top 5 European leagues.
 * Sources: Wikipedia current squad pages (via MediaWiki API) + existing bundled data fallback.
 * Output: data/bundled/leagues/*.json
 *
 * Usage: node scripts/build_squads_2026_27.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const OUT_DIR = path.join(ROOT, 'data/bundled/leagues');
const EXISTING_DIR = path.join(ROOT, 'data/bundled/leagues');

const SEASON = '2026-27';

const LEAGUES = {
  premier_league: { league_id: 4328, league_name: 'English Premier League', country: 'England' },
  la_liga:        { league_id: 4335, league_name: 'Spanish La Liga',        country: 'Spain' },
  serie_a:        { league_id: 4332, league_name: 'Italian Serie A',        country: 'Italy' },
  bundesliga:     { league_id: 4331, league_name: 'German Bundesliga',      country: 'Germany' },
  ligue_1:        { league_id: 4334, league_name: 'French Ligue 1',         country: 'France' },
};

// Target 2026/27 clubs. api_id preserves existing IDs where known; new clubs get stable IDs.
const TARGET = {
  premier_league: [
    { api_id: 133604, name: 'Arsenal', wiki: 'Arsenal_F.C.' },
    { api_id: 133601, name: 'Aston Villa', wiki: 'Aston_Villa_F.C.' },
    { api_id: 134301, name: 'AFC Bournemouth', wiki: 'AFC_Bournemouth' },
    { api_id: 134355, name: 'Brentford', wiki: 'Brentford_F.C.' },
    { api_id: 133619, name: 'Brighton & Hove Albion', wiki: 'Brighton_%26_Hove_Albion_F.C.' },
    { api_id: 133610, name: 'Chelsea', wiki: 'Chelsea_F.C.' },
    { api_id: 900201, name: 'Coventry City', wiki: 'Coventry_City_F.C.' },
    { api_id: 133632, name: 'Crystal Palace', wiki: 'Crystal_Palace_F.C.' },
    { api_id: 133615, name: 'Everton', wiki: 'Everton_F.C.' },
    { api_id: 133600, name: 'Fulham', wiki: 'Fulham_F.C.' },
    { api_id: 900202, name: 'Hull City', wiki: 'Hull_City_A.F.C.' },
    { api_id: 900203, name: 'Ipswich Town', wiki: 'Ipswich_Town_F.C.' },
    { api_id: 198340, name: 'Leeds United', wiki: 'Leeds_United_F.C.' },
    { api_id: 133602, name: 'Liverpool', wiki: 'Liverpool_F.C.' },
    { api_id: 133613, name: 'Manchester City', wiki: 'Manchester_City_F.C.' },
    { api_id: 133612, name: 'Manchester United', wiki: 'Manchester_United_F.C.' },
    { api_id: 133636, name: 'Newcastle United', wiki: 'Newcastle_United_F.C.' },
    { api_id: 133631, name: 'Nottingham Forest', wiki: 'Nottingham_Forest_F.C.' },
    { api_id: 198244, name: 'Sunderland', wiki: 'Sunderland_A.F.C.' },
    { api_id: 133616, name: 'Tottenham Hotspur', wiki: 'Tottenham_Hotspur_F.C.' },
  ],
  la_liga: [
    { api_id: 134221, name: 'Deportivo Alavés', wiki: 'Deportivo_Alav%C3%A9s' },
    { api_id: 133727, name: 'Athletic Bilbao', wiki: 'Athletic_Bilbao' },
    { api_id: 133729, name: 'Atlético Madrid', wiki: 'Atlético_Madrid' },
    { api_id: 133739, name: 'FC Barcelona', wiki: 'FC_Barcelona' },
    { api_id: 133937, name: 'Celta Vigo', wiki: 'RC_Celta_de_Vigo' },
    { api_id: 900301, name: 'Deportivo La Coruña', wiki: 'Deportivo_de_A_Coruña' },
    { api_id: 900302, name: 'Elche CF', wiki: 'Elche_CF' },
    { api_id: 133741, name: 'RCD Espanyol', wiki: 'RCD_Espanyol' },
    { api_id: 133731, name: 'Getafe CF', wiki: 'Getafe_CF' },
    { api_id: 900303, name: 'Levante UD', wiki: 'Levante_UD' },
    { api_id: 900304, name: 'Málaga CF', wiki: 'Málaga_CF' },
    { api_id: 133730, name: 'CA Osasuna', wiki: 'CA_Osasuna' },
    { api_id: 900305, name: 'Racing Santander', wiki: 'Racing_de_Santander' },
    { api_id: 133732, name: 'Rayo Vallecano', wiki: 'Rayo_Vallecano' },
    { api_id: 133757, name: 'Real Betis', wiki: 'Real_Betis' },
    { api_id: 133738, name: 'Real Madrid', wiki: 'Real_Madrid_CF' },
    { api_id: 133740, name: 'Real Sociedad', wiki: 'Real_Sociedad' },
    { api_id: 133743, name: 'Sevilla FC', wiki: 'Sevilla_FC' },
    { api_id: 133750, name: 'Valencia CF', wiki: 'Valencia_CF' },
    { api_id: 133746, name: 'Villarreal CF', wiki: 'Villarreal_CF' },
  ],
  serie_a: [
    { api_id: 134782, name: 'Atalanta', wiki: 'Atalanta_BC' },
    { api_id: 134781, name: 'Bologna', wiki: 'Bologna_FC_1909' },
    { api_id: 134783, name: 'Cagliari', wiki: 'Cagliari_Calcio' },
    { api_id: 134243, name: 'Como', wiki: 'Como_1907' },
    { api_id: 134675, name: 'Fiorentina', wiki: 'ACF_Fiorentina' },
    { api_id: 900401, name: 'Frosinone', wiki: 'Frosinone_Calcio' },
    { api_id: 133671, name: 'Genoa', wiki: 'Genoa_CFC' },
    { api_id: 133681, name: 'Inter Milan', wiki: 'Inter_Milan' },
    { api_id: 133676, name: 'Juventus', wiki: 'Juventus_FC' },
    { api_id: 133674, name: 'Lazio', wiki: 'SS_Lazio' },
    { api_id: 133677, name: 'Lecce', wiki: 'US_Lecce' },
    { api_id: 133667, name: 'AC Milan', wiki: 'AC_Milan' },
    { api_id: 133685, name: 'Monza', wiki: 'AC_Monza' },
    { api_id: 133670, name: 'Napoli', wiki: 'SSC_Napoli' },
    { api_id: 133680, name: 'Parma', wiki: 'Parma_Calcio_1913' },
    { api_id: 133668, name: 'Roma', wiki: 'AS_Roma' },
    { api_id: 900402, name: 'Sassuolo', wiki: 'US_Sassuolo_Calcio' },
    { api_id: 133673, name: 'Torino', wiki: 'Torino_FC' },
    { api_id: 133679, name: 'Udinese', wiki: 'Udinese_Calcio' },
    { api_id: 133684, name: 'Venezia', wiki: 'Venezia_FC' },
  ],
  bundesliga: [
    { api_id: 133652, name: 'FC Augsburg', wiki: 'FC_Augsburg' },
    { api_id: 133661, name: 'Union Berlin', wiki: '1._FC_Union_Berlin' },
    { api_id: 133655, name: 'Werder Bremen', wiki: 'SV_Werder_Bremen' },
    { api_id: 133650, name: 'Borussia Dortmund', wiki: 'Borussia_Dortmund' },
    { api_id: 900501, name: 'SV Elversberg', wiki: 'SV_Elversberg' },
    { api_id: 133814, name: 'Eintracht Frankfurt', wiki: 'Eintracht_Frankfurt' },
    { api_id: 133653, name: 'SC Freiburg', wiki: 'SC_Freiburg' },
    { api_id: 900502, name: 'Hamburger SV', wiki: 'Hamburger_SV' },
    { api_id: 133657, name: 'TSG Hoffenheim', wiki: 'TSG_1899_Hoffenheim' },
    { api_id: 900503, name: '1. FC Köln', wiki: '1._FC_Köln' },
    { api_id: 133654, name: 'RB Leipzig', wiki: 'RB_Leipzig' },
    { api_id: 133666, name: 'Bayer Leverkusen', wiki: 'Bayer_04_Leverkusen' },
    { api_id: 133656, name: 'Mainz 05', wiki: '1._FSV_Mainz_05' },
    { api_id: 133659, name: 'Borussia Mönchengladbach', wiki: 'Borussia_M%C3%B6nchengladbach' },
    { api_id: 133664, name: 'Bayern Munich', wiki: 'FC_Bayern_Munich' },
    { api_id: 900504, name: 'SC Paderborn', wiki: 'SC_Paderborn_07' },
    { api_id: 900505, name: 'Schalke 04', wiki: 'FC_Schalke_04' },
    { api_id: 133660, name: 'VfB Stuttgart', wiki: 'VfB_Stuttgart' },
  ],
  ligue_1: [
    { api_id: 133705, name: 'Angers SCO', wiki: 'Angers_SCO' },
    { api_id: 133717, name: 'AJ Auxerre', wiki: 'AJ_Auxerre' },
    { api_id: 133704, name: 'Stade Brestois 29', wiki: 'Stade_Brestois_29' },
    { api_id: 900601, name: 'Le Havre AC', wiki: 'Le_Havre_AC' },
    { api_id: 900602, name: 'Le Mans FC', wiki: 'Le_Mans_FC' },
    { api_id: 133716, name: 'RC Lens', wiki: 'RC_Lens' },
    { api_id: 133712, name: 'Lille OSC', wiki: 'Lille_OSC' },
    { api_id: 900603, name: 'FC Lorient', wiki: 'FC_Lorient' },
    { api_id: 133713, name: 'Olympique Lyonnais', wiki: 'Olympique_Lyonnais' },
    { api_id: 133707, name: 'Olympique Marseille', wiki: 'Olympique_de_Marseille' },
    { api_id: 133711, name: 'AS Monaco', wiki: 'AS_Monaco_FC' },
    { api_id: 133715, name: 'OGC Nice', wiki: 'OGC_Nice' },
    { api_id: 900604, name: 'Paris FC', wiki: 'Paris_FC' },
    { api_id: 133714, name: 'Paris Saint-Germain', wiki: 'Paris_Saint-Germain_FC' },
    { api_id: 133719, name: 'Stade Rennais FC', wiki: 'Stade_Rennais_FC' },
    { api_id: 133720, name: 'RC Strasbourg Alsace', wiki: 'RC_Strasbourg_Alsace' },
    { api_id: 133718, name: 'Toulouse FC', wiki: 'Toulouse_FC' },
    { api_id: 900605, name: 'ES Troyes AC', wiki: 'ES_Troyes_AC' },
  ],
};

const DETAILED_POS = new Set(['GK','CB','LB','RB','CDM','CM','CAM','LM','RM','LW','RW','ST']);
const POS_MAP = {
  GK:'GK', G:'GK', GOALKEEPER:'GK', KEEPER:'GK',
  CB:'CB', 'CENTRE-BACK':'CB', 'CENTER-BACK':'CB', DEFENDER:'CB', DF:'CB', D:'CB', DEF:'CB',
  LB:'LB', 'LEFT-BACK':'LB', LWB:'LB',
  RB:'RB', 'RIGHT-BACK':'RB', RWB:'RB',
  CDM:'CDM', DM:'CDM', 'DEFENSIVE MIDFIELD':'CDM', 'DEFENSIVE MIDFIELDER':'CDM',
  CM:'CM', MIDFIELDER:'CM', 'CENTRAL MIDFIELD':'CM', MF:'CM', M:'CM', MID:'CM',
  CAM:'CAM', AM:'CAM', 'ATTACKING MIDFIELD':'CAM', 'ATTACKING MIDFIELDER':'CAM',
  LM:'LM', 'LEFT MIDFIELD':'LM', LW:'LW', 'LEFT WINGER':'LW',
  RM:'RM', 'RIGHT MIDFIELD':'RM', RW:'RW', 'RIGHT WINGER':'RW',
  ST:'ST', CF:'ST', FW:'ST', FWD:'ST', F:'ST', FORWARD:'ST', STRIKER:'ST', 'CENTRE-FORWARD':'ST', 'CENTER-FORWARD':'ST', ATT:'ST',
};
function mapPos(raw) {
  const p = String(raw||'').trim().toUpperCase().replace(/_/g,' ').replace(/\s+/g,' ');
  if (DETAILED_POS.has(p)) return p;
  if (POS_MAP[p]) return POS_MAP[p];
  if (/GOAL/.test(p)) return 'GK';
  if (/LEFT.?BACK|LWB/.test(p)) return 'LB';
  if (/RIGHT.?BACK|RWB/.test(p)) return 'RB';
  if (/CENTRE.?BACK|CENTER.?BACK|\bCB\b|DEFEND/.test(p)) return 'CB';
  if (/DEFENSIVE MID|\bCDM\b|\bDM\b/.test(p)) return 'CDM';
  if (/ATTACKING MID|\bCAM\b|\bAM\b/.test(p)) return 'CAM';
  if (/LEFT WING|\bLW\b/.test(p)) return 'LW';
  if (/RIGHT WING|\bRW\b/.test(p)) return 'RW';
  if (/LEFT MID|\bLM\b/.test(p)) return 'LM';
  if (/RIGHT MID|\bRM\b/.test(p)) return 'RM';
  if (/STRIK|FORWARD|CENTRE.?FORWARD|CENTER.?FORWARD|\bCF\b|\bST\b|\bFW\b|\bFWD\b|\bATT\b/.test(p)) return 'ST';
  if (/MID|\bMF\b|\bCM\b/.test(p)) return 'CM';
  if (/WING/.test(p)) return 'RW';
  if (/\bDF\b|\bDEF\b/.test(p)) return 'CB';
  return 'CM';
}
function coarse(pos){ return ({GK:'GK',CB:'DEF',LB:'DEF',RB:'DEF',CDM:'MID',CM:'MID',CAM:'MID',LM:'MID',RM:'MID',LW:'FWD',RW:'FWD',ST:'FWD'})[pos]||'MID'; }

const NAT_MAP = {
  ESP:'Spain',ENG:'England',FRA:'France',GER:'Germany',ITA:'Italy',POR:'Portugal',NED:'Netherlands',
  BRA:'Brazil',ARG:'Argentina',BEL:'Belgium',CRO:'Croatia',URU:'Uruguay',COL:'Colombia',USA:'United States',
  MAR:'Morocco',SEN:'Senegal',NGA:'Nigeria',CIV:'Ivory Coast',GHA:'Ghana',CMR:'Cameroon',EGY:'Egypt',
  ALG:'Algeria',TUN:'Tunisia',JPN:'Japan',KOR:'South Korea',AUS:'Australia',CAN:'Canada',MEX:'Mexico',
  SUI:'Switzerland',AUT:'Austria',DEN:'Denmark',SWE:'Sweden',NOR:'Norway',POL:'Poland',CZE:'Czech Republic',
  SRB:'Serbia',UKR:'Ukraine',TUR:'Turkey',GRE:'Greece',SCO:'Scotland',WAL:'Wales',IRL:'Republic of Ireland',
  NIR:'Northern Ireland',ROU:'Romania',HUN:'Hungary',SVK:'Slovakia',SVN:'Slovenia',BUL:'Bulgaria',
  ECU:'Ecuador',CHI:'Chile',PAR:'Paraguay',PER:'Peru',VEN:'Venezuela',CRC:'Costa Rica',JAM:'Jamaica',
  MLI:'Mali',GUI:'Guinea',BFA:'Burkina Faso',COD:'DR Congo',ZAM:'Zambia',RSA:'South Africa',
  NGA:'Nigeria',GAB:'Gabon',ANG:'Angola',MOZ:'Mozambique',CPV:'Cape Verde',
  ISR:'Israel',GEO:'Georgia',ALB:'Albania',MKD:'North Macedonia',BIH:'Bosnia and Herzegovina',
  ISL:'Iceland',FIN:'Finland',KOS:'Kosovo',MNE:'Montenegro',ARM:'Armenia',AZE:'Azerbaijan',
  IRN:'Iran',UZB:'Uzbekistan',KSA:'Saudi Arabia',QAT:'Qatar',UAE:'United Arab Emirates',
  CHN:'China',JAM:'Jamaica',HON:'Honduras',PAN:'Panama',LUX:'Luxembourg',MLT:'Malta',
  CYP:'Cyprus',LTU:'Lithuania',LVA:'Latvia',EST:'Estonia',ZIM:'Zimbabwe',BEN:'Benin',
  TOG:'Togo',CGO:'Congo',GAM:'Gambia',SLE:'Sierra Leone',GNB:'Guinea-Bissau',
  COM:'Comoros',MAD:'Madagascar',KEN:'Kenya',TAN:'Tanzania',UGA:'Uganda',
  SUR:'Suriname',GUY:'Guyana',HAI:'Haiti',DOM:'Dominican Republic',CUB:'Cuba',
  BOL:'Bolivia',SLV:'El Salvador',GUA:'Guatemala',PHI:'Philippines',IDN:'Indonesia',
  NZL:'New Zealand',LBR:'Liberia',CTA:'Central African Republic',EQG:'Equatorial Guinea',
  NCL:'New Caledonia',GRN:'Grenada',MSR:'Montserrat',TRI:'Trinidad and Tobago',
};

function natName(code){ return NAT_MAP[String(code||'').toUpperCase()] || code || null; }

function hashStr(s){
  let h=2166136261;
  for(let i=0;i<s.length;i++){ h^=s.charCodeAt(i); h=Math.imul(h,16777619); }
  return h>>>0;
}

function makePlayerId(name, teamId, shirt){
  // stable deterministic ID
  const base = hashStr(`${teamId}|${name.toLowerCase().replace(/\s+/g,' ').trim()}`);
  return 700000000 + (base % 200000000) + (Number(shirt)||0);
}

function estimateRatings(pos, age, seed){
  const rng = mulberry32(seed);
  const posBase = {GK:74,CB:73,LB:72,RB:72,CDM:73,CM:73,CAM:74,LM:72,RM:72,LW:75,RW:75,ST:75}[pos]||73;
  let overall = Math.round(posBase + (rng()-0.45)*14);
  overall = Math.max(58, Math.min(91, overall));
  const a = Number(age)||26;
  let pot = overall;
  if (a <= 20) pot = overall + 8 + Math.floor(rng()*10);
  else if (a <= 23) pot = overall + 4 + Math.floor(rng()*8);
  else if (a <= 27) pot = overall + Math.floor(rng()*5);
  else if (a <= 30) pot = overall + Math.floor(rng()*2);
  else pot = overall;
  pot = Math.max(overall, Math.min(94, pot));
  return { overall, potential: pot };
}

function mulberry32(a){ return function(){ let t=a+=0x6D2B79F5; t=Math.imul(t^t>>>15,t|1); t^=t+Math.imul(t^t>>>7,t|61); return ((t^t>>>14)>>>0)/4294967296; }; }

function footFor(seed){ return (seed%2===0)?'Right':'Left'; }

async function fetchJSON(url, tries = 4){
  let lastErr;
  for(let i=0;i<tries;i++){
    try {
      const r = await fetch(url, { headers: { 'user-agent': 'KiloSquadBuilder/1.0 (https://github.com/kilo-code)' } });
      if(r.status === 429 || r.status >= 500){
        lastErr = new Error(`HTTP ${r.status} ${url}`);
        await new Promise(res => setTimeout(res, 800 * (i + 1)));
        continue;
      }
      if(!r.ok) throw new Error(`HTTP ${r.status} ${url}`);
      return r.json();
    } catch(e){
      lastErr = e;
      await new Promise(res => setTimeout(res, 500 * (i + 1)));
    }
  }
  throw lastErr;
}

function parseFsPlayers(wikitext){
  const players = [];
  // Find the first-team/current squad block - stop at Out on loan / Reserves / Youth / Academy / Other players
  const lines = wikitext.split('\n');
  let inSquad = false;
  let seenPlayer = false;
  for(const line of lines){
    if(/^\{\{\s*(Fs start|Football squad start|Squad start)/i.test(line)){ inSquad = true; continue; }
    if(/^\{\{\s*(Fs mid|Football squad mid)/i.test(line)){ inSquad = true; continue; }
    if(/^\{\{\s*(Fs end|Football squad end|Squad end)/i.test(line)){ if(seenPlayer) break; continue; }
    if(/==+\s*(Out on loan|On loan|Reserves|Youth|Academy|Other players|Players out on loan|Notable players)/i.test(line)){
      if(seenPlayer) break;
    }
    if(!inSquad && /\{\{\s*(Fs player|fs player)/i.test(line)) inSquad = true;
    if(!inSquad) continue;
    const m = line.match(/\{\{\s*(?:Fs player|fs player|Fs2 player)\s*\|([^}]*)\}\}/i);
    if(!m) continue;
    seenPlayer = true;
    const params = {};
    // split by | but be careful with nested templates/links
    const parts = m[1].split('|');
    for(const part of parts){
      const eq = part.indexOf('=');
      if(eq===-1) continue;
      const k = part.slice(0,eq).trim().toLowerCase();
      const v = part.slice(eq+1).trim();
      params[k]=v;
    }
    const no = parseInt(params.no || params.number || params.num || '', 10);
    const nat = (params.nat || params.nation || '').toUpperCase().replace(/[^A-Z]/g,'').slice(0,3);
    const posRaw = params.pos || params.position || '';
    const nameRaw = params.name || '';
    // extract name from [[link|display]] or [[link]]
    let name = nameRaw.replace(/\{\{[^}]*\}\}/g,'').replace(/<!--.*?-->/g,'').trim();
    const linkMatch = name.match(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/);
    if(linkMatch) name = (linkMatch[2]||linkMatch[1]).trim();
    name = name.replace(/\[\[|\]\]/g,'').replace(/''+/g,'').replace(/\s+/g,' ').trim();
    if(!name || name.length < 2) continue;
    // skip obvious non-players
    if(/^(flagicon|fbicon)/i.test(name)) continue;
    const other = (params.other || '').toLowerCase();
    // exclude loaned out if they appear in this block with "on loan to/at" and we are past main squad
    players.push({ no: Number.isFinite(no)?no:null, nat, pos: mapPos(posRaw), name, other });
  }
  // dedupe by name
  const seen = new Set();
  return players.filter(p=>{ const k=p.name.toLowerCase(); if(seen.has(k)) return false; seen.add(k); return true; });
}

async function fetchWikipediaSquad(wikiTitle){
  try {
    const page = wikiTitle.includes('%') ? decodeURIComponent(wikiTitle) : wikiTitle;
    const secData = await fetchJSON(`https://en.wikipedia.org/w/api.php?action=parse&page=${encodeURIComponent(page)}&prop=sections&format=json&origin=*`);
    const sections = secData.parse?.sections || [];
    // prefer "Current squad" / "First-team squad" / "Players"
    let sec = sections.find(s=>/^(current squad|first-team squad|first team squad)$/i.test(s.line.trim()));
    if(!sec) sec = sections.find(s=>/current squad|first-team squad/i.test(s.line) && !/women|youth|reserve/i.test(s.line));
    if(!sec) sec = sections.find(s=>/^players$/i.test(s.line.trim()));
    if(!sec) sec = sections.find(s=>/squad|players/i.test(s.line) && !/women|youth|reserve|former|notable|records/i.test(s.line));
    if(!sec) return { players:[], source:'no-section' };
    const wtData = await fetchJSON(`https://en.wikipedia.org/w/api.php?action=parse&page=${encodeURIComponent(page)}&prop=wikitext&section=${sec.index}&format=json&origin=*`);
    const wt = wtData.parse?.wikitext?.['*'] || '';
    const players = parseFsPlayers(wt);
    return { players, source:'wikipedia', section: sec.line };
  } catch(e) {
    return { players:[], source:'error', error: String(e.message||e) };
  }
}

function loadExisting() {
  const byLeague = {};
  const playerClubMap = new Map(); // normalized name -> {team_api_id, team_name, league}
  for (const [league, meta] of Object.entries(LEAGUES)) {
    const p = path.join(EXISTING_DIR, `${league}.json`);
    if (!fs.existsSync(p)) continue;
    const d = JSON.parse(fs.readFileSync(p, 'utf8'));
    byLeague[league] = d;
    for (const t of d.teams || []) {
      for (const pl of t.players || []) {
        const k = normName(pl.name);
        if (!k) continue;
        // keep first seen (or overwrite) - doesn't matter much
        playerClubMap.set(k, { team_api_id: t.api_id, team_name: t.name, league });
      }
    }
  }
  return { byLeague, playerClubMap };
}

function normName(s){
  return String(s||'').toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9\s]/g,'').replace(/\s+/g,' ').trim();
}

function findExistingTeam(byLeague, league, api_id, name){
  const d = byLeague[league];
  if(!d) return null;
  return (d.teams||[]).find(t => t.api_id === api_id) || (d.teams||[]).find(t => normName(t.name) === normName(name)) || null;
}

function buildPlayerRecord(pl, team, leagueKey, existingPlayerMap){
  const pos = mapPos(pl.pos || pl.position || 'CM');
  const name = pl.name;
  const seed = hashStr(`${team.api_id}|${name}`);
  const age = pl.age != null ? Number(pl.age) : (existingPlayerMap.get(normName(name))?.age ?? null);
  const { overall, potential } = (pl.overall != null && pl.potential != null)
    ? { overall: Number(pl.overall), potential: Number(pl.potential) }
    : estimateRatings(pos, age, seed);
  const ex = existingPlayerMap.get(normName(name)) || {};
  const id = pl.api_id ? Number(pl.api_id) : (ex.api_id ? Number(ex.api_id) : makePlayerId(name, team.api_id, pl.no));
  return {
    api_id: id,
    name,
    firstname: pl.firstname || ex.firstname || name.split(' ').slice(0,-1).join(' ') || null,
    lastname: pl.lastname || ex.lastname || name.split(' ').slice(-1)[0] || name,
    age: age ?? null,
    dob: pl.dob || ex.dob || null,
    nationality: natName(pl.nat || pl.nationality) || ex.nationality || null,
    photo: pl.photo || ex.photo || null,
    position: coarse(pos),
    detailed_position: pos,
    number: pl.no ?? ex.number ?? null,
    height_cm: pl.height_cm || ex.height_cm || null,
    weight_kg: pl.weight_kg || ex.weight_kg || null,
    preferred_foot: pl.preferred_foot || ex.preferred_foot || footFor(seed),
    overall,
    potential,
    status: 'active',
    injured: false,
  };
}

async function main(){
  const { byLeague, playerClubMap } = loadExisting();
  // Build existing player details lookup by normalized name
  const existingPlayerDetails = new Map();
  for (const d of Object.values(byLeague)) {
    for (const t of d.teams||[]) {
      for (const p of t.players||[]) {
        const k = normName(p.name);
        if(k) existingPlayerDetails.set(k, p);
      }
    }
  }

  const report = { leagues: {}, totals: { clubs:0, players:0 }, warnings: [] };
  fs.mkdirSync(OUT_DIR, { recursive: true });

  for (const [leagueKey, meta] of Object.entries(LEAGUES)) {
    const targets = TARGET[leagueKey] || [];
    const outTeams = [];
    let leaguePlayers = 0;

    for (const target of targets) {
      process.stderr.write(`Fetching ${leagueKey} / ${target.name}...\n`);
      const wiki = await fetchWikipediaSquad(target.wiki);
      let players = wiki.players;

      // Sanity filter: remove players who are in existing bundled data for a DIFFERENT target club
      const targetIds = new Set((TARGET[leagueKey]||[]).map(t=>t.api_id));
      const allTargetIds = new Set(Object.values(TARGET).flat().map(t=>t.api_id));
      const filtered = [];
      for (const p of players) {
        const k = normName(p.name);
        const ex = playerClubMap.get(k);
        if (ex && !allTargetIds.has(ex.team_api_id)) {
          // existed in old data but that club is not in our 96 - could be relegated, allow
          filtered.push(p);
        } else if (ex && ex.team_api_id !== target.api_id) {
          // player belongs to another top-5 club in existing data - likely vandalism or outdated wiki
          // but allow if it's the same league move? be conservative: skip
          report.warnings.push(`SKIP suspected-misplaced: ${p.name} at ${target.name} (existing: ${ex.team_name})`);
          continue;
        } else {
          filtered.push(p);
        }
      }
      players = filtered;

      // Fallback: if Wikipedia gave too few players, merge with existing data
      const existingTeam = findExistingTeam(byLeague, leagueKey, target.api_id, target.name);
      if (players.length < 14 && existingTeam) {
        report.warnings.push(`FALLBACK merge: ${target.name} had ${players.length} wiki players, merging existing`);
        const seen = new Set(players.map(p=>normName(p.name)));
        for (const ep of (existingTeam.players||[])) {
          const k = normName(ep.name);
          if(seen.has(k)) continue;
          // don't bring in players now at another target club
          const ex = playerClubMap.get(k);
          if (ex && ex.team_api_id !== target.api_id && allTargetIds.has(ex.team_api_id)) continue;
          players.push({ no: ep.number, nat: ep.nationality, pos: ep.detailed_position || ep.position, name: ep.name, age: ep.age, dob: ep.dob, api_id: ep.api_id, firstname: ep.firstname, lastname: ep.lastname, photo: ep.photo, height_cm: ep.height_cm, weight_kg: ep.weight_kg, preferred_foot: ep.preferred_foot, overall: ep.overall, potential: ep.potential, number: ep.number });
          seen.add(k);
        }
      }

      // If still empty, use existing entirely
      if (players.length === 0 && existingTeam) {
        report.warnings.push(`FALLBACK full: ${target.name} using existing data`);
        for (const ep of (existingTeam.players||[])) {
          players.push({ no: ep.number, nat: ep.nationality, pos: ep.detailed_position || ep.position, name: ep.name, age: ep.age, dob: ep.dob, api_id: ep.api_id, firstname: ep.firstname, lastname: ep.lastname, photo: ep.photo, height_cm: ep.height_cm, weight_kg: ep.weight_kg, preferred_foot: ep.preferred_foot, overall: ep.overall, potential: ep.potential, number: ep.number });
        }
      }

      // Dedupe within squad
      const seenNames = new Set();
      players = players.filter(p => { const k=normName(p.name); if(!k || seenNames.has(k)) return false; seenNames.add(k); return true; });

      const records = players.map(p => buildPlayerRecord(p, target, leagueKey, existingPlayerDetails));
      // Ensure at least some GK/DEF/MID/FWD
      const counts = {GK:0,DEF:0,MID:0,FWD:0};
      for(const r of records) counts[r.position]=(counts[r.position]||0)+1;

      outTeams.push({
        api_id: target.api_id,
        name: target.name,
        short_name: target.short_name || null,
        logo: existingTeam?.logo || null,
        players: records,
      });
      leaguePlayers += records.length;
      // be nice to Wikipedia
      await new Promise(r => setTimeout(r, 350));
    }

    const out = {
      league_id: meta.league_id,
      league_name: meta.league_name,
      country: meta.country,
      season: SEASON,
      teams: outTeams,
    };
    fs.writeFileSync(path.join(OUT_DIR, `${leagueKey}.json`), JSON.stringify(out, null, 2) + '\n');
    report.leagues[leagueKey] = { clubs: outTeams.length, players: leaguePlayers };
    report.totals.clubs += outTeams.length;
    report.totals.players += leaguePlayers;
  }

  // duplicate check across all leagues
  const seenIds = new Map();
  const dupes = [];
  for(const [lk, meta] of Object.entries(LEAGUES)){
    const d = JSON.parse(fs.readFileSync(path.join(OUT_DIR, `${lk}.json`),'utf8'));
    for(const t of d.teams){
      for(const p of t.players){
        if(seenIds.has(p.api_id)) dupes.push({id:p.api_id,name:p.name,first:seenIds.get(p.api_id),dup:`${lk}/${t.name}`});
        else seenIds.set(p.api_id, `${lk}/${t.name}`);
      }
    }
  }
  report.duplicates = dupes;

  fs.writeFileSync(path.join(ROOT, 'data/bundled/build_report_2026_27.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report.totals));
  console.log('Leagues:', Object.entries(report.leagues).map(([k,v])=>`${k}:${v.clubs}c/${v.players}p`).join(', '));
  console.log('Duplicates:', dupes.length);
  if(report.warnings.length) console.log('Warnings:', report.warnings.length);
}

main().catch(e=>{ console.error(e); process.exit(1); });
