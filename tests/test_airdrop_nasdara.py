"""Airdrop-Konfigurator: Nasdara ist freigegeben (SupplyBoxen laut Bohemias CE-Dateien belegt)."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402


def _conn(karte):
    return SimpleNamespace(data={"map_name": karte}, service_id="1000")


@pytest.mark.parametrize("karte,name", [("ChernarusPlus", "ChernarusPlus"), ("dayzOffline.livonia", "Livonia"),
                                        ("dayzOffline.sakhal", "Sakhal"), ("dayzOffline.nasdara", "Nasdara"),
                                        ("Nasdara", "Nasdara")])
def test_bekannte_karten_sind_freigegeben(karte, name):
    st = bot._airdrop_status(_conn(karte))
    assert st["freigegeben"] is True and st["karte"] == name and st["dekor"] and st["secondary"]


def test_nasdara_hat_weltgroesse_und_hinweis():
    st = bot._airdrop_status(_conn("dayzOffline.nasdara"))
    assert st["world_size"] == 16384
    assert "Nasdara" in st["hinweis"] and "Gaszone" in st["hinweis"]


@pytest.mark.parametrize("karte", ["", "Unbekannte_Karte"])
def test_unbekannte_karte_bleibt_gesperrt(karte):
    st = bot._airdrop_status(_conn(karte))
    assert st["freigegeben"] is False and "unbekannt" in st["hinweis"]


def test_eingabe_auf_nasdara_wird_angenommen():
    c = _conn("dayzOffline.nasdara")
    suffix, name, punkte, werte = bot._airdrop_input(
        {"suffix": "winter", "vorschau": True, "positionen": [{"x": 10114, "z": 4222}], "werte": {}}, c)
    assert suffix == "winter" and punkte and werte["kisten"] == 3


def test_nasdara_position_ausserhalb_der_karte_wird_abgelehnt():
    c = _conn("dayzOffline.nasdara")
    with pytest.raises(ValueError):
        bot._airdrop_input({"suffix": "w", "vorschau": True, "positionen": [{"x": 16500, "z": 100}], "werte": {}}, c)
    assert bot._airdrop_input({"suffix": "w", "vorschau": True, "positionen": [{"x": 16300, "z": 100}], "werte": {}}, c)
