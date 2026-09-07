"""Tests fuer die Loot-Ausschlusszonen (mapgrouppos.xml).

Es gibt keine echte mapgrouppos.xml eines Kunden als Testdatei - die
synthetische Datei unten folgt der bekannten Struktur
(<map><group name="..." pos="x y z" .../></map>), damit der Parser und das
chirurgische Entfernen wenigstens gegen ein plausibles Format geprueft sind,
bevor Kunden das Tool live nutzen.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/ -v
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

MAPGROUPPOS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<map>\n'
    '    <group name="Land_Village_Small_A" pos="7500.000000 300.000000 7500.000000" a0="0" a1="0" a2="0"/>\n'
    '    <group name="Land_Shed_Small" pos="7550.000000 300.000000 7520.000000" a0="0" a1="0" a2="0"/>\n'
    '    <group name="Land_Silo_Metal" pos="500.000000 200.000000 500.000000" a0="0" a1="0" a2="0"/>\n'
    '</map>\n'
)


def test_lootzone_gruppen_findet_alle_eintraege():
    gruppen = bot._lootzone_gruppen(MAPGROUPPOS_XML)
    assert len(gruppen) == 3
    assert gruppen[0]["x"] == 7500.0 and gruppen[0]["z"] == 7500.0
    assert gruppen[2]["x"] == 500.0 and gruppen[2]["z"] == 500.0


def test_lootzone_treffer_erkennt_nur_gruppen_in_der_zone():
    gruppen = bot._lootzone_gruppen(MAPGROUPPOS_XML)
    zonen = [{"name": "Zone 1", "x": 7500.0, "z": 7500.0, "radius": 100.0}]
    entfernen, je_zone = bot._lootzone_treffer(gruppen, zonen)
    # Beide Village-Eintraege (7500/7500 und 7550/7520) liegen im Radius,
    # der weit entfernte Silo (500/500) nicht.
    assert entfernen == {0, 1}
    assert je_zone == [2]


def test_lootzone_entfernen_laesst_uebrige_zeilen_byte_fuer_byte_unveraendert():
    gruppen = bot._lootzone_gruppen(MAPGROUPPOS_XML)
    entfernen, _ = bot._lootzone_treffer(
        gruppen, [{"name": "Zone 1", "x": 7500.0, "z": 7500.0, "radius": 100.0}])
    neu = bot._lootzone_entfernen(MAPGROUPPOS_XML, gruppen, entfernen)
    assert "Land_Village_Small_A" not in neu
    assert "Land_Shed_Small" not in neu
    assert "Land_Silo_Metal" in neu
    assert neu.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<map>\n')
    assert neu.endswith("</map>\n")
    assert len(bot._lootzone_gruppen(neu)) == 1
