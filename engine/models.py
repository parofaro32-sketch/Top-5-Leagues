from __future__ import annotations

import copy
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Side = Literal["home", "away"]
MatchMode = Literal["bot_vs_bot", "manager_vs_manager"]
MatchStatus = Literal["setup", "running", "break", "halftime", "finished", "cancelled", "error"]


@dataclass(slots=True)
class PlayerAttributes:
    passing: float = 60
    shooting: float = 60
    finishing: float = 60
    dribbling: float = 60
    pace: float = 60
    acceleration: float = 60
    strength: float = 60
    stamina: float = 70
    positioning: float = 60
    tackling: float = 55
    interceptions: float = 55
    defensive_awareness: float = 55
    creativity: float = 55
    crossing: float = 55
    heading: float = 55
    goalkeeping: float = 10
    decision_making: float = 60
    composure: float = 60

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PlayerAttributes":
        base = cls()
        if not data:
            return base
        vals = {}
        for f in cls.__dataclass_fields__:
            vals[f] = max(1.0, min(99.0, float(data.get(f, getattr(base, f)))))
        return cls(**vals)

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    def overall(self, position: str | None = None) -> float:
        pos = (position or "MID").upper()
        if pos in {"G", "GK", "GOALKEEPER"}:
            vals = [self.goalkeeping*1.5, self.positioning, self.decision_making, self.composure, self.passing*0.6, self.strength]
        elif pos in {"D", "DEF", "DEFENDER", "CB", "LB", "RB", "WB", "LWB", "RWB"}:
            vals = [self.tackling, self.interceptions, self.defensive_awareness, self.strength, self.positioning, self.heading, self.pace*0.8, self.passing*0.8]
        elif pos in {"M", "MID", "MIDFIELDER", "CM", "DM", "AM", "CDM", "CAM"}:
            vals = [self.passing, self.decision_making, self.positioning, self.creativity, self.dribbling, self.stamina, self.tackling*0.7, self.shooting*0.6]
        else:
            vals = [self.finishing, self.shooting, self.dribbling, self.pace, self.acceleration, self.positioning, self.composure, self.heading*0.6, self.passing*0.7]
        return sum(vals) / len(vals)


@dataclass(slots=True)
class Player:
    api_id: int
    name: str
    team_api_id: int
    position: str = "MID"
    detailed_position: str | None = None
    number: int | None = None
    nationality: str | None = None
    age: int | None = None
    photo: str | None = None
    injured: bool = False
    attributes: PlayerAttributes = field(default_factory=PlayerAttributes)

    @property
    def pos_group(self) -> str:
        p = (self.position or "MID").upper()
        if "GK" in p or p == "G":
            return "GK"
        if "DEF" in p or p == "D":
            return "DEF"
        if "MID" in p or p == "M":
            return "MID"
        if "ATT" in p or "FWD" in p or "FOR" in p or p in {"F", "ST", "FW"}:
            return "FWD"
        return "MID"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["attributes"] = self.attributes.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Player":
        return cls(
            api_id=int(d["api_id"]), name=d.get("name", "Unknown"), team_api_id=int(d.get("team_api_id", 0)),
            position=d.get("position") or "MID", detailed_position=d.get("detailed_position"), number=d.get("number"),
            nationality=d.get("nationality"), age=d.get("age"), photo=d.get("photo"), injured=bool(d.get("injured", False)),
            attributes=PlayerAttributes.from_dict(d.get("attributes")),
        )


@dataclass(slots=True)
class Team:
    api_id: int
    name: str
    league_id: int | None = None
    league_name: str | None = None
    country: str | None = None
    logo: str | None = None
    short_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Team":
        return cls(api_id=int(d["api_id"]), name=d.get("name", "Unknown"), league_id=d.get("league_id"), league_name=d.get("league_name"), country=d.get("country"), logo=d.get("logo"), short_name=d.get("short_name"))


@dataclass(slots=True)
class LineupEntry:
    player_api_id: int
    name: str
    position: str
    slot: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LineupEntry":
        return cls(player_api_id=int(d["player_api_id"]), name=d.get("name", "Unknown"), position=d.get("position", "MID"), slot=d.get("slot"))


@dataclass(slots=True)
class Lineup:
    team_api_id: int
    team_name: str
    formation: str
    starters: list[LineupEntry]
    bench: list[LineupEntry]
    captain_player_id: int | None = None
    goalkeeper_player_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"team_api_id": self.team_api_id, "team_name": self.team_name, "formation": self.formation,
                "starters": [s.to_dict() for s in self.starters], "bench": [b.to_dict() for b in self.bench],
                "captain_player_id": self.captain_player_id, "goalkeeper_player_id": self.goalkeeper_player_id}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Lineup":
        return cls(team_api_id=int(d["team_api_id"]), team_name=d.get("team_name", ""), formation=d.get("formation", "4-3-3"),
                   starters=[LineupEntry.from_dict(x) for x in d.get("starters", [])], bench=[LineupEntry.from_dict(x) for x in d.get("bench", [])],
                   captain_player_id=d.get("captain_player_id"), goalkeeper_player_id=d.get("goalkeeper_player_id"))


def default_tactics() -> dict[str, Any]:
    return {
        "attacking": {"mentality": "balanced", "build_up": "mixed", "passing_directness": "mixed", "possession_style": "balanced",
                      "attacking_width": "balanced", "crossing": "mixed", "counterattacking": False, "attacking_transitions": "balanced",
                      "overlapping_fullbacks": True, "underlapping_movements": False},
        "defensive": {"defensive_line": "standard", "pressing_intensity": "medium", "pressing_triggers": "balanced",
                      "defensive_width": "balanced", "compactness": "balanced", "defensive_block": "mid",
                      "counterpressing": False, "marking": "zonal", "offside_trap": False},
        "individual": {"roles": {}, "movement": {}, "positioning": {}, "pressing_responsibilities": {}, "defensive_responsibilities": {}, "attacking_responsibilities": {}},
        "set_pieces": {"corners": "mixed", "free_kicks": "mixed", "penalties": {}},
    }


@dataclass(slots=True)
class PlayerMatchState:
    player_api_id: int
    name: str
    team_side: Side
    position: str
    slot: str | None = None
    on_pitch: bool = False
    is_starter: bool = False
    is_goalkeeper: bool = False
    is_captain: bool = False
    minutes_played: int = 0
    stamina: float = 100.0
    rating: float = 6.5
    rating_points: float = 6.5
    goals: int = 0
    assists: int = 0
    shots: int = 0
    shots_on_target: int = 0
    key_passes: int = 0
    tackles: int = 0
    interceptions: int = 0
    saves: int = 0
    fouls_committed: int = 0
    fouls_won: int = 0
    yellow_cards: int = 0
    red_card: bool = False
    injured: bool = False
    injury_minute: int | None = None
    injury_severity: str | None = None
    injury_continued: bool = False
    injury_substituted: bool = False
    substituted_in_minute: int | None = None
    substituted_out_minute: int | None = None
    contributions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlayerMatchState":
        obj = cls(player_api_id=int(d.get("player_api_id", 0)), name=d.get("name", ""), team_side=d.get("team_side", "home"), position=d.get("position", "MID"))
        for k in cls.__dataclass_fields__:
            if k in d:
                setattr(obj, k, d[k])
        return obj


@dataclass(slots=True)
class TeamMatchState:
    side: Side
    team: Team
    lineup: Lineup
    tactics: dict[str, Any]
    score: int = 0
    possession_share: float = 50.0
    shots: int = 0
    shots_on_target: int = 0
    xg: float = 0.0
    corners: int = 0
    fouls: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    offsides: int = 0
    saves: int = 0
    passes_completed: int = 0
    passes_attempted: int = 0
    substitutions_made: int = 0
    substitution_events: list[dict[str, Any]] = field(default_factory=list)
    tactical_changes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def pass_accuracy(self) -> float:
        return (self.passes_completed / self.passes_attempted * 100.0) if self.passes_attempted else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["team"] = self.team.to_dict()
        d["lineup"] = self.lineup.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TeamMatchState":
        return cls(side=d["side"], team=Team.from_dict(d["team"]), lineup=Lineup.from_dict(d["lineup"]), tactics=copy.deepcopy(d.get("tactics") or default_tactics()),
                   score=int(d.get("score", 0)), possession_share=float(d.get("possession_share", 50)), shots=int(d.get("shots", 0)),
                   shots_on_target=int(d.get("shots_on_target", 0)), xg=float(d.get("xg", 0)), corners=int(d.get("corners", 0)), fouls=int(d.get("fouls", 0)),
                   yellow_cards=int(d.get("yellow_cards", 0)), red_cards=int(d.get("red_cards", 0)), offsides=int(d.get("offsides", 0)), saves=int(d.get("saves", 0)),
                   passes_completed=int(d.get("passes_completed", 0)), passes_attempted=int(d.get("passes_attempted", 0)), substitutions_made=int(d.get("substitutions_made", 0)),
                   substitution_events=list(d.get("substitution_events", [])), tactical_changes=list(d.get("tactical_changes", [])))


@dataclass(slots=True)
class MatchEvent:
    seq: int
    minute: int
    extra_minute: int
    phase: str
    event_type: str
    team_side: Side | None = None
    player_api_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    commentary: str = ""
    public_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MatchEvent":
        return cls(seq=int(d["seq"]), minute=int(d.get("minute", 0)), extra_minute=int(d.get("extra_minute", 0)), phase=d.get("phase", ""),
                   event_type=d.get("event_type", "note"), team_side=d.get("team_side"), player_api_id=d.get("player_api_id"),
                   payload=dict(d.get("payload") or {}), commentary=d.get("commentary", ""), public_payload=dict(d.get("public_payload") or d.get("payload") or {}))


@dataclass(slots=True)
class MatchState:
    id: str
    guild_id: int | None
    channel_id: int | None
    mode: MatchMode
    home: TeamMatchState
    away: TeamMatchState
    status: MatchStatus = "setup"
    minute: int = 0
    extra_minute: int = 0
    phase: str = "pre_kickoff"
    stoppage_time_1: int = 1
    stoppage_time_2: int = 3
    possession_side: Side = "home"
    pending_break: str | None = None
    home_break_decision_done: bool = False
    away_break_decision_done: bool = False
    events: list[MatchEvent] = field(default_factory=list)
    next_event_seq: int = 1
    player_states: dict[str, PlayerMatchState] = field(default_factory=dict)
    players_index: dict[str, Player] = field(default_factory=dict)
    rng_seed: int = 0
    message_id: int | None = None
    public_stats: dict[str, Any] = field(default_factory=dict)
    submission: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def player_key(side: Side, player_api_id: int) -> str:
        return f"{side}:{int(player_api_id)}"

    def team(self, side: Side) -> TeamMatchState:
        return self.home if side == "home" else self.away

    def opp_side(self, side: Side) -> Side:
        return "away" if side == "home" else "home"

    def scoreline(self) -> str:
        return f"{self.home.team.name} {self.home.score}-{self.away.score} {self.away.team.name}"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "guild_id": self.guild_id, "channel_id": self.channel_id, "mode": self.mode,
                "home": self.home.to_dict(), "away": self.away.to_dict(), "status": self.status, "minute": self.minute,
                "extra_minute": self.extra_minute, "phase": self.phase, "stoppage_time_1": self.stoppage_time_1, "stoppage_time_2": self.stoppage_time_2,
                "possession_side": self.possession_side, "pending_break": self.pending_break,
                "home_break_decision_done": self.home_break_decision_done, "away_break_decision_done": self.away_break_decision_done,
                "events": [e.to_dict() for e in self.events], "next_event_seq": self.next_event_seq,
                "player_states": {k: v.to_dict() for k, v in self.player_states.items()},
                "players_index": {k: v.to_dict() for k, v in self.players_index.items()}, "rng_seed": self.rng_seed,
                "message_id": self.message_id, "public_stats": self.public_stats, "submission": self.submission}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MatchState":
        return cls(id=d["id"], guild_id=d.get("guild_id"), channel_id=d.get("channel_id"), mode=d.get("mode", "bot_vs_bot"),
                   home=TeamMatchState.from_dict(d["home"]), away=TeamMatchState.from_dict(d["away"]), status=d.get("status", "setup"),
                   minute=int(d.get("minute", 0)), extra_minute=int(d.get("extra_minute", 0)), phase=d.get("phase", "pre_kickoff"),
                   stoppage_time_1=int(d.get("stoppage_time_1", 1)), stoppage_time_2=int(d.get("stoppage_time_2", 3)),
                   possession_side=d.get("possession_side", "home"), pending_break=d.get("pending_break"),
                   home_break_decision_done=bool(d.get("home_break_decision_done", False)), away_break_decision_done=bool(d.get("away_break_decision_done", False)),
                   events=[MatchEvent.from_dict(e) for e in d.get("events", [])], next_event_seq=int(d.get("next_event_seq", 1)),
                   player_states={k: PlayerMatchState.from_dict(v) for k, v in (d.get("player_states") or {}).items()},
                   players_index={k: Player.from_dict(v) for k, v in (d.get("players_index") or {}).items()}, rng_seed=int(d.get("rng_seed", 0)),
                   message_id=d.get("message_id"), public_stats=dict(d.get("public_stats") or {}),
                   submission=dict(d.get("submission") or {}))


def new_match_id() -> str:
    return uuid.uuid4().hex[:12]
