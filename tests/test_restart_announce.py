"""Tests fuer die Neustart-Ankuendigungstexte (60/30/15/10/5/3 Minuten vorher).

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


def test_volle_stunde_wird_als_stunde_formatiert():
    assert bot._restart_dauer_text(60, "de") == "1 Stunde"
    assert bot._restart_dauer_text(60, "en") == "1 hour"


def test_minuten_bleiben_minuten():
    for mins in (30, 15, 10, 5, 3):
        assert bot._restart_dauer_text(mins, "de") == f"{mins} Minuten"
        assert bot._restart_dauer_text(mins, "en") == f"{mins} minutes"


def test_zwei_stunden_wuerden_als_stunden_erkannt():
    # Nicht Teil der aktuellen Ankuendigungs-Zeitpunkte, aber die Funktion
    # soll auch fuer andere volle Stunden korrekt pluralisieren.
    assert bot._restart_dauer_text(120, "de") == "2 Stunden"
    assert bot._restart_dauer_text(120, "en") == "2 hours"
