"""Regressionstest: ShopManager._parse_effect_area() muss "SafePositions"
immer garantieren - der Effekt-Generator befuellt dieses Feld nie selbst, und
brachte ein Kunde eine cfgEffectArea.json mit, die diesen Schluessel noch nie
hatte, fehlte er nach dem Speichern komplett statt wie erwartet
"SafePositions": [] zu sein (Brigarde hat das anhand eines Referenz-Formats
mit "SafePositions": [] gemeldet).

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_effect_area_safepositions.py -v
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def test_leere_datei_hat_safepositions():
    data, key = bot.ShopManager._parse_effect_area(None)
    assert data["SafePositions"] == []
    assert key == "Areas"


def test_vorhandene_datei_ohne_safepositions_bekommt_leere_liste():
    # Genau Brigardes Fall: eine echte cfgEffectArea.json, die "SafePositions"
    # noch nie hatte (von Hand gebaut oder mit einem anderen Tool erzeugt).
    raw = json.dumps({"Areas": [{"AreaName": "Zone 1"}]})
    data, key = bot.ShopManager._parse_effect_area(raw)
    assert data["SafePositions"] == []
    assert key == "Areas"


def test_vorhandene_safepositions_bleiben_unangetastet():
    raw = json.dumps({"Areas": [], "SafePositions": [[100.0, 200.0]]})
    data, _key = bot.ShopManager._parse_effect_area(raw)
    assert data["SafePositions"] == [[100.0, 200.0]]


def test_fallback_pfad_ohne_areas_key_bekommt_ebenfalls_safepositions():
    # Weder "areas" (Gross-/Kleinschreibung) noch eine Liste mit AreaName-
    # Eintraegen vorhanden - der dritte, generische Rueckfallpfad.
    raw = json.dumps({"SomethingElse": 1})
    data, key = bot.ShopManager._parse_effect_area(raw)
    assert data["SafePositions"] == []
    assert key == "Areas"
    assert data["Areas"] == []
