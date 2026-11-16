from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Slot:
    code: str
    group: str
    label: str


FORMATION_TEMPLATES: dict[str, list[Slot]] = {
    "4-3-3": [Slot("GK","GK","Goalkeeper"), Slot("LB","DEF","Left Back"), Slot("LCB","DEF","LCB"), Slot("RCB","DEF","RCB"), Slot("RB","DEF","Right Back"),
              Slot("LCM","MID","LCM"), Slot("CM","MID","CM"), Slot("RCM","MID","RCM"), Slot("LW","FWD","LW"), Slot("ST","FWD","ST"), Slot("RW","FWD","RW")],
    "4-2-3-1": [Slot("GK","GK","Goalkeeper"), Slot("LB","DEF","Left Back"), Slot("LCB","DEF","LCB"), Slot("RCB","DEF","RCB"), Slot("RB","DEF","Right Back"),
                Slot("LDM","MID","LDM"), Slot("RDM","MID","RDM"), Slot("LAM","MID","LAM"), Slot("CAM","MID","CAM"), Slot("RAM","MID","RAM"), Slot("ST","FWD","ST")],
    "4-4-2": [Slot("GK","GK","Goalkeeper"), Slot("LB","DEF","Left Back"), Slot("LCB","DEF","LCB"), Slot("RCB","DEF","RCB"), Slot("RB","DEF","Right Back"),
              Slot("LM","MID","LM"), Slot("LCM","MID","LCM"), Slot("RCM","MID","RCM"), Slot("RM","MID","RM"), Slot("LS","FWD","LS"), Slot("RS","FWD","RS")],
    "3-5-2": [Slot("GK","GK","Goalkeeper"), Slot("LCB","DEF","LCB"), Slot("CB","DEF","CB"), Slot("RCB","DEF","RCB"),
              Slot("LWB","MID","LWB"), Slot("LCM","MID","LCM"), Slot("CDM","MID","CDM"), Slot("RCM","MID","RCM"), Slot("RWB","MID","RWB"), Slot("LS","FWD","LS"), Slot("RS","FWD","RS")],
    "3-4-3": [Slot("GK","GK","Goalkeeper"), Slot("LCB","DEF","LCB"), Slot("CB","DEF","CB"), Slot("RCB","DEF","RCB"),
              Slot("LM","MID","LM"), Slot("LCM","MID","LCM"), Slot("RCM","MID","RCM"), Slot("RM","MID","RM"), Slot("LW","FWD","LW"), Slot("ST","FWD","ST"), Slot("RW","FWD","RW")],
    "4-1-4-1": [Slot("GK","GK","Goalkeeper"), Slot("LB","DEF","Left Back"), Slot("LCB","DEF","LCB"), Slot("RCB","DEF","RCB"), Slot("RB","DEF","Right Back"),
                Slot("CDM","MID","CDM"), Slot("LM","MID","LM"), Slot("LCM","MID","LCM"), Slot("RCM","MID","RCM"), Slot("RM","MID","RM"), Slot("ST","FWD","ST")],
    "4-3-1-2": [Slot("GK","GK","Goalkeeper"), Slot("LB","DEF","Left Back"), Slot("LCB","DEF","LCB"), Slot("RCB","DEF","RCB"), Slot("RB","DEF","Right Back"),
                Slot("LCM","MID","LCM"), Slot("CM","MID","CM"), Slot("RCM","MID","RCM"), Slot("CAM","MID","CAM"), Slot("LS","FWD","LS"), Slot("RS","FWD","RS")],
    "4-2-2-2": [Slot("GK","GK","Goalkeeper"), Slot("LB","DEF","Left Back"), Slot("LCB","DEF","LCB"), Slot("RCB","DEF","RCB"), Slot("RB","DEF","Right Back"),
                Slot("LDM","MID","LDM"), Slot("RDM","MID","RDM"), Slot("LAM","MID","LAM"), Slot("RAM","MID","RAM"), Slot("LS","FWD","LS"), Slot("RS","FWD","RS")],
    "4-5-1": [Slot("GK","GK","Goalkeeper"), Slot("LB","DEF","Left Back"), Slot("LCB","DEF","LCB"), Slot("RCB","DEF","RCB"), Slot("RB","DEF","Right Back"),
              Slot("LM","MID","LM"), Slot("LCM","MID","LCM"), Slot("CM","MID","CM"), Slot("RCM","MID","RCM"), Slot("RM","MID","RM"), Slot("ST","FWD","ST")],
    "5-3-2": [Slot("GK","GK","Goalkeeper"), Slot("LWB","DEF","LWB"), Slot("LCB","DEF","LCB"), Slot("CB","DEF","CB"), Slot("RCB","DEF","RCB"), Slot("RWB","DEF","RWB"),
              Slot("LCM","MID","LCM"), Slot("CM","MID","CM"), Slot("RCM","MID","RCM"), Slot("LS","FWD","LS"), Slot("RS","FWD","RS")],
    "5-4-1": [Slot("GK","GK","Goalkeeper"), Slot("LWB","DEF","LWB"), Slot("LCB","DEF","LCB"), Slot("CB","DEF","CB"), Slot("RCB","DEF","RCB"), Slot("RWB","DEF","RWB"),
              Slot("LM","MID","LM"), Slot("LCM","MID","LCM"), Slot("RCM","MID","RCM"), Slot("RM","MID","RM"), Slot("ST","FWD","ST")],
    "3-4-1-2": [Slot("GK","GK","Goalkeeper"), Slot("LCB","DEF","LCB"), Slot("CB","DEF","CB"), Slot("RCB","DEF","RCB"),
                Slot("LM","MID","LM"), Slot("LCM","MID","LCM"), Slot("RCM","MID","RCM"), Slot("RM","MID","RM"), Slot("CAM","MID","CAM"), Slot("LS","FWD","LS"), Slot("RS","FWD","RS")],
    "4-4-1-1": [Slot("GK","GK","Goalkeeper"), Slot("LB","DEF","Left Back"), Slot("LCB","DEF","LCB"), Slot("RCB","DEF","RCB"), Slot("RB","DEF","Right Back"),
                Slot("LM","MID","LM"), Slot("LCM","MID","LCM"), Slot("RCM","MID","RCM"), Slot("RM","MID","RM"), Slot("CF","FWD","CF"), Slot("ST","FWD","ST")],
}

SUPPORTED_FORMATIONS = sorted(FORMATION_TEMPLATES.keys())


def get_formation_slots(formation: str) -> list[Slot]:
    if formation not in FORMATION_TEMPLATES:
        raise ValueError(f"Unsupported formation: {formation}. Supported: {', '.join(SUPPORTED_FORMATIONS)}")
    return FORMATION_TEMPLATES[formation]


def is_position_compatible(player_group: str, slot_group: str) -> bool:
    pg = (player_group or "MID").upper()
    sg = (slot_group or "MID").upper()
    if sg == "GK":
        return pg == "GK"
    if sg == "DEF":
        return pg in {"DEF", "MID"}
    if sg == "MID":
        return pg in {"MID", "DEF", "FWD"}
    if sg == "FWD":
        return pg in {"FWD", "MID"}
    return False
