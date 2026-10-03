"""Positionen aus der Ereigniszeile selbst (Konsolen-ADM).

Das Konsolen-Format schreibt ``pos=<x, y, z>`` INNERHALB der id-Klammer
jedes genannten Spielers. Die Haupt-Muster kill_pvp/damage/connect fingen
aber nur das alte ``at pos=<...>`` am Zeilenende, deshalb war ``position``
bei jeder Konsolenzeile None, und die Zonen-Pruefung musste den Positions-
Cache nehmen, den eine spaetere Zeile desselben Lesedurchgangs schon
ueberschrieben haben konnte. Seit dieser Aenderung traegt jedes Ereignis die
Position aus GENAU seiner Zeile - beim Treffer auch die des Angreifers.

Alle Zeilen mit "echt" sind aus tests/adm (PS4-Server); die PvP-Kill-Zeile
ist KONSTRUIERT (in den Testdaten gibt es keinen PvP-Kill), folgt aber dem
belegten Format der Umwelttod-Zeilen (Player "X" (DEAD) (id=... pos=<...>)).

    python3 -m pytest tests/test_event_positionen.py -q
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from log_parser import DayZLogParser  # noqa: E402

# echt
TREFFER = (
    '00:31:17 | Player "ICH_MAG_HUNDE_xD" (id=3UsM1LZavR0zCtKZxuwoF6B801b8m7_u-EN2vY9VHxA= '
    'pos=<8698.0, 12833.5, 104.5>)[HP: 95.725] hit by Player "SIDNIK29F95" '
    '(id=qu9u_5Ia6GlRon-PlZNtqZl_60hcxMQlLP6HOJvdlCY= pos=<8697.3, 12834.4, 104.5>) '
    'into Torso(7) for 4.275 damage (MeleeFist_Heavy)'
)
# echt
BAU = (
    '23:08:31 | Player "rasant_Mucke" (id=4CV73HWlJhWudbxIqJBrRa6QfMs9joTmakVKx4OVRXc= '
    'pos=<8678.1, 12813.9, 114.9>) placed Fence Kit<FenceKit>'
)
# echt
CONNECT = (
    '21:08:29 | Player "Louis_107Boss" (id=FdswdgqQV72L9_UbX49jkysnU7DfjsaAQhE9Qxw3SiI= '
    'pos=<8675.3, 12807.4, 115.0>) is connected'
)
# echt
DISCONNECT = (
    '21:44:32 | Player "Louis_107Boss" (id=FdswdgqQV72L9_UbX49jkysnU7DfjsaAQhE9Qxw3SiI= '
    'pos=<8654.5, 12903.6, 113.4>) has been disconnected'
)
# echt
EMOTE = (
    '00:13:55 | Player "SIDNIK29F95" (id=qu9u_5Ia6GlRon-PlZNtqZl_60hcxMQlLP6HOJvdlCY= '
    'pos=<8645.2, 12822.3, 108.6>) performed EmoteSitA with RefrigeratorMinsk'
)
# echt - ohne Position
CONNECTING = (
    '21:07:58 | Player "Louis_107Boss" (id=FdswdgqQV72L9_UbX49jkysnU7DfjsaAQhE9Qxw3SiI=) '
    'is connecting'
)
# KONSTRUIERT nach dem Konsolen-Format
KILL_KONSOLE = (
    '12:00:01 | Player "Opfer" (DEAD) (id=AAA= pos=<1000.0, 2000.0, 10.0>) '
    'killed by Player "Killer" (id=BBB= pos=<1300.0, 2400.0, 12.0>) with M4A1 from 500 meters'
)
# Altes PC-Format (Regression)
KILL_LEGACY = (
    '12:00:02 | Player "Opfer" (id=AAA) killed by Player "Killer" (id=BBB) '
    'with M4A1 from 500 meters at pos=<1000.0, 2000.0, 10.0>'
)


@pytest.fixture
def parser():
    return DayZLogParser()


def test_treffer_traegt_beide_positionen(parser):
    ev = parser.parse_line(TREFFER)
    assert ev["type"] == "damage"
    assert ev["position"] == "8698.0, 12833.5, 104.5"
    assert ev["attacker_position"] == "8697.3, 12834.4, 104.5"
    assert ev["weapon"] == "Unbekannt" or ev["weapon"]  # Waffe bleibt wie bisher


def test_treffer_position_ist_die_des_opfers_nicht_die_letzte_der_zeile(parser):
    ev = parser.parse_line(TREFFER)
    # Nicht einfach "irgendein pos= der Zeile": Opfer und Angreifer getrennt.
    assert ev["position"] != ev["attacker_position"]


def test_kill_konsole_beide_positionen(parser):
    ev = parser.parse_line(KILL_KONSOLE)
    assert ev["type"] == "kill_pvp"
    assert ev["victim"] == "Opfer" and ev["killer"] == "Killer"
    assert ev["position"] == "1000.0, 2000.0, 10.0"
    assert ev["killer_position"] == "1300.0, 2400.0, 12.0"
    assert ev["weapon"] == "M4A1"
    assert ev["distance"] == "500"


def test_kill_legacy_format_unveraendert(parser):
    ev = parser.parse_line(KILL_LEGACY)
    assert ev["type"] == "kill_pvp"
    assert ev["position"] == "1000.0, 2000.0, 10.0"
    assert ev["killer_position"] is None
    assert ev["weapon"] == "M4A1"


def test_connect_traegt_position_und_bleibt_online(parser):
    ev = parser.parse_line(CONNECT)
    assert ev["type"] == "connect"
    assert ev["position"] == "8675.3, 12807.4, 115.0"
    # Der Spieler steht weiter im Tracking (Online-Liste).
    assert "Louis_107Boss" in parser.player_positions


def test_disconnect_traegt_position_und_entfernt_aus_tracking(parser):
    parser.parse_line(CONNECT)
    ev = parser.parse_line(DISCONNECT)
    assert ev["type"] == "disconnect"
    assert ev["position"] == "8654.5, 12903.6, 113.4"
    assert "Louis_107Boss" not in parser.player_positions


def test_bau_und_emote_positionen(parser):
    ev = parser.parse_line(BAU)
    assert ev["type"] == "basebuild"
    assert ev["position"] == "8678.1, 12813.9, 114.9"
    ev = parser.parse_line(EMOTE)
    assert ev["type"] == "emote"
    assert ev["position"] == "8645.2, 12822.3, 108.6"


def test_connecting_ohne_position(parser):
    ev = parser.parse_line(CONNECTING)
    assert ev["type"] == "connecting"
    assert ev["position"] is None


def test_notfall_rueckfall_kill_mit_positionen(parser):
    # Zeile, die das strenge kill_pvp-Muster nicht trifft (Zusatztext am Ende),
    # aber den Notfall-Rueckfall _generic_kill_event.
    zeile = KILL_KONSOLE + " (headshot)"
    ev = parser.parse_line(zeile)
    assert ev["type"] == "kill_pvp"
    assert ev["position"] == "1000.0, 2000.0, 10.0"
    assert ev["killer_position"] == "1300.0, 2400.0, 12.0"


# ── bot.py-Helfer ────────────────────────────────────────────────────
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def test_ev_position_xz_bevorzugt_ereigniszeile(parser):
    ev = parser.parse_line(TREFFER)
    # Cache absichtlich "weitergewandert":
    parser.player_positions["ICH_MAG_HUNDE_xD"]["position"] = "1.0, 2.0, 3.0"
    assert bot._ev_position_xz(ev, parser) == (8698.0, 12833.5)
    assert bot._ev_position_xz(ev, parser, "attacker") == (8697.3, 12834.4)


def test_ev_position_xz_rueckfall_cache(parser):
    parser.parse_line(CONNECT)
    ev = {"type": "connect", "player": "Louis_107Boss", "position": None}
    assert bot._ev_position_xz(ev, parser) == (8675.3, 12807.4)
    assert bot._ev_position_xz(ev, None) is None
    assert bot._ev_position_xz({"type": "connect", "player": "X"}, parser, "attacker") is None


def test_zone_enthaelt_kreis_und_polygon():
    kreis = {"x": 1000, "z": 2000, "radius": 50}
    assert bot._zone_enthaelt(kreis, 1030, 2040)
    assert not bot._zone_enthaelt(kreis, 1051, 2000)
    poly = {"type": "polygon", "points": [{"x": 0, "z": 0}, {"x": 100, "z": 0},
                                          {"x": 100, "z": 100}, {"x": 0, "z": 100}]}
    assert bot._zone_enthaelt(poly, 50, 50)
    assert not bot._zone_enthaelt(poly, 150, 50)
    assert not bot._zone_enthaelt({"x": "abc", "z": 0, "radius": 5}, 0, 0)
