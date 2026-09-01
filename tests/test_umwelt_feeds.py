"""Tests fuer die Feeds, die still leer blieben: Bury/Unbury und die
Umwelttode mit mehrwortiger Ursache (Explosion, Fahrzeug).

Warum es diese Datei gibt: beide Fehler waren im Code unsichtbar - die Muster
sahen plausibel aus, trafen aber kein echtes Log. Aufgefallen ist es erst, als
3,3 MB echte .ADM-Dateien (23.903 Zeilen, zwei Server, ueber die Nitrado-API
geholt) durch den echten Parser liefen:

* "Dug in"/"Dug out" kam vor, wurde aber von KEINEM Muster erkannt - der
  Zeiger ist <0x...> (das "x" faellt nicht unter [0-9a-fA-F]), zwischen
  Zeiger und Verb steht noch Klasse:ID, und die Position ist <x,y,z>.
* 19 Explosionstode standen im Log, keiner kam als Explosion an: das
  kill_env-Muster endete an einem "\\s" und schnitt die Ursache am ersten
  Leerzeichen ab ("EGD-5 Frag Grenade" -> "EGD-5").

Alle Zeilen unten sind ECHT aus diesen Dateien, nicht ausgedacht.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/ -v
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from log_parser import DayZLogParser  # noqa: E402

# ── Echte Zeilen ──────────────────────────────────────────────────────
DUG_OUT = (
    '11:14:39 | Player "Lcpl_1911" '
    '(id=kiZ3Ulkz8nuVjrn5mBwL_7jHB4RUJSOX-f5SnsSqWS0= '
    'pos=<4832.8, 858.1, 347.7>)Player SurvivorBase<0x00000244A8E78B10> '
    'SurvivorM_Oliver:105619 Dug out UndergroundStash<0x0000024330CBBAF0> '
    'UndergroundStash:0 at position <4833.35,347.93,857.259>'
)
DUG_IN = (
    '16:48:18 | Player "Brigarde_Erkelen" '
    '(id=pJ8wQQS1teCqTArq2aZoDMlVfy_LmtX05vC44dOIM6c= '
    'pos=<11142.4, 12311.2, 199.6>)Player SurvivorBase<0x00000179FAA175A0> '
    'SurvivorM_Cyril:74113 Dug in WoodenCrate<0x0000017D342EA020> '
    'WoodenCrate:73640 at position <11143.2,199.545,12312>'
)
TOD_GRANATE = (
    '07:06:30 | Player "thetruth554" (DEAD) '
    '(id=orj8JlpXe--yhhx7XFouPXSpEqbPl4Rfj614KsfQlJU= '
    'pos=<4025.9, 10143.3, 250.4>) killed by EGD-5 Frag Grenade'
)
TOD_SPRENGSTOFF = (
    '18:59:38 | Player "zues_marsh" (DEAD) '
    '(id=lbuDSIfpNPtbQFjm0p7iKqATHb2F8y7az10GXF6K67I= '
    'pos=<11903.4, 919.8, 331.6>) killed by Plastic Explosive'
)
TOD_ZOMBIE = (
    '20:28:47 | Player "B33-Ghost3D" (DEAD) '
    '(id=jwzPu1DI2MGbdHZwMkbPNCM3pcJYvwbKGfKU7-6fha0= '
    'pos=<4266.8, 10395.1, 239.7>) killed by ZmbM_Mummy'
)
TOD_SCHLICHT = (
    '06:28:33 | Player "thetruth554" (DEAD) '
    '(id=orj8JlpXe--yhhx7XFouPXSpEqbPl4Rfj614KsfQlJU= '
    'pos=<3268.5, 2050.6, 489.9>) died. Stats> Water: 598.997 '
    'Energy: 598.997 Bleed sources: 0'
)


# ── Parser: Vergraben/Ausgraben ───────────────────────────────────────
def test_dug_out_wird_erkannt():
    ev = DayZLogParser().parse_line(DUG_OUT)
    assert ev is not None, "echte Dug-out-Zeile blieb unerkannt"
    assert ev["type"] == "basebuild"
    assert ev["aktion"] == "dug out"
    assert ev["item"] == "UndergroundStash"
    assert ev["player"] == "Lcpl_1911"


def test_dug_in_wird_erkannt():
    ev = DayZLogParser().parse_line(DUG_IN)
    assert ev is not None, "echte Dug-in-Zeile blieb unerkannt"
    assert ev["aktion"] == "dug in"
    assert ev["item"] == "WoodenCrate"


# ── Parser: Todesursache vollstaendig ─────────────────────────────────
@pytest.mark.parametrize("zeile, ursache", [
    (TOD_GRANATE, "EGD-5 Frag Grenade"),
    (TOD_SPRENGSTOFF, "Plastic Explosive"),
    (TOD_ZOMBIE, "ZmbM_Mummy"),
])
def test_todesursache_nicht_abgeschnitten(zeile, ursache):
    ev = DayZLogParser().parse_line(zeile)
    assert ev is not None and ev["type"] == "kill_env"
    assert ev["cause"] == ursache


def test_schlichter_tod_bleibt_erkannt():
    """Die Muster-Korrektur darf die haeufigste Todeszeile nicht kaputtmachen."""
    ev = DayZLogParser().parse_line(TOD_SCHLICHT)
    assert ev is not None and ev["type"] == "kill_env"


# ── Feed-Zuordnung (braucht bot.py) ───────────────────────────────────
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


@pytest.mark.parametrize("zeile, feed", [
    (DUG_IN, "bury"),
    (DUG_OUT, "unbury"),
    (TOD_GRANATE, "explosion_death"),
    (TOD_SPRENGSTOFF, "explosion_death"),
    (TOD_ZOMBIE, "zombie_death"),
])
def test_feed_zuordnung(zeile, feed):
    ev = DayZLogParser().parse_line(zeile)
    assert ev is not None
    assert bot._feed_key(ev) == feed


def test_fahrzeugtod_ueber_klassennamen():
    """DayZ schreibt nie "vehicle" oder "car", sondern den Klassennamen."""
    for klasse in ("Hatchback_02_Blue", "Offroad_02", "CivilianSedan_Wine",
                   "Sedan_02"):
        ev = {"type": "kill_env", "cause": klasse, "raw": ""}
        assert bot._feed_key(ev) == "vehicle_death", klasse


def test_zombie_treffer_ist_kein_waehlbarer_feed():
    """DayZ protokolliert Zombie-TREFFER nicht (0 in 23.903 echten Zeilen) -
    der Feed waere immer leer. Der Zombie-TOD bleibt dagegen waehlbar."""
    assert "zombie_hit" not in bot.FEED_TYPES
    assert "zombie_death" in bot.FEED_TYPES
