"""Tests fuer das Custom-Build-Mapping-Tool (Object-Spawner-Dateien).

Kein echter FTP-Zugang noetig: ein einfacher Stub mit list_dir/read_file_ex
reicht, dieselbe Idee wie das FakeFTP aus den echten Wegwerf-Prozess-Tests.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/ -v
"""
import asyncio
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


class _FakeFTP:
    def __init__(self, files):
        self.files = files

    def list_dir(self, directory):
        d = directory.rstrip("/")
        return [p for p in self.files if p.rsplit("/", 1)[0] == d]

    def read_file_ex(self, path):
        if path in self.files:
            return self.files[path], "ok"
        return None, "missing"


# Verkuerztes, aber strukturgleiches Beispiel der echten AUTOFRIEDHOF.json,
# die Brigarde geschickt hat - inklusive eines absichtlich kaputten Eintrags
# (kein "pos"), der uebersprungen werden muss statt die ganze Antwort zu
# zerstoeren.
_AUTOFRIEDHOF = json.dumps({
    "Objects": [
        {"name": "Land_Wreck_Lada_Green",
         "pos": [10933.228515625, 6.926835536956787, 2883.7998046875],
         "ypr": [-104.94, -0.00002, 0.000003], "scale": 0.9999933242797852,
         "enableCEPersistency": 0, "customString": ""},
        {"name": "StaticObj_Wreck_BRDM_DE",
         "pos": [11438.6083984375, 8.718642234802246, 3369.126953125],
         "ypr": [77.63, 0.0, -15.22], "scale": 0.9999809265136719,
         "enableCEPersistency": 0, "customString": ""},
        {"name": "Kaputter_Eintrag_ohne_position"},
    ],
})

_CFGGAMEPLAY = json.dumps({
    "GeneralData": {"disableBaseDamage": False},
    "WorldsData": {
        "lightingConfig": 0,
        "objectSpawnersArr": ["custom/quarantine krasno.json", "custom/AUTOFRIEDHOF.json"],
    },
})


def _conn_mit_dateien(dateien):
    conn = bot.ServerConnection({"service_id": "9999", "ftp_mission_dir": "/mission"})
    conn.ftp = _FakeFTP(dateien)
    return conn


async def _lauf(conn):
    loop = asyncio.get_running_loop()
    dateien, status = await bot._custom_build_dateien(conn, loop)
    objekte_je_datei = {}
    for name in dateien:
        objekte_je_datei[name] = await bot._custom_build_objekte(conn, name, loop)
    return dateien, status, objekte_je_datei


def test_objectspawnersarr_wird_gefunden():
    conn = _conn_mit_dateien({
        "/mission/cfggameplay.json": _CFGGAMEPLAY,
        "/mission/custom/quarantine krasno.json": json.dumps({"Objects": []}),
        "/mission/custom/AUTOFRIEDHOF.json": _AUTOFRIEDHOF,
    })
    dateien, status, objekte_je_datei = asyncio.run(_lauf(conn))
    assert status == "ok"
    assert dateien == ["custom/quarantine krasno.json", "custom/AUTOFRIEDHOF.json"]
    assert objekte_je_datei["custom/quarantine krasno.json"] == []


def test_objekte_werden_mit_x_z_extrahiert_und_kaputte_eintraege_uebersprungen():
    conn = _conn_mit_dateien({
        "/mission/cfggameplay.json": _CFGGAMEPLAY,
        "/mission/custom/quarantine krasno.json": json.dumps({"Objects": []}),
        "/mission/custom/AUTOFRIEDHOF.json": _AUTOFRIEDHOF,
    })
    _, _, objekte_je_datei = asyncio.run(_lauf(conn))
    objekte = objekte_je_datei["custom/AUTOFRIEDHOF.json"]
    # Nur 2 von 3 Eintraegen - der ohne "pos" wird uebersprungen.
    assert len(objekte) == 2
    assert objekte[0]["name"] == "Land_Wreck_Lada_Green"
    # pos[0] -> x, pos[2] -> z (pos[1] ist die Hoehe und wird ignoriert).
    assert objekte[0]["x"] == pytest.approx(10933.228515625)
    assert objekte[0]["z"] == pytest.approx(2883.7998046875)


def test_kein_objectspawnersarr_liefert_leere_liste_ohne_fehler():
    conn = _conn_mit_dateien({
        "/mission/cfggameplay.json": json.dumps({"GeneralData": {"disableBaseDamage": False}}),
    })
    dateien, status, _ = asyncio.run(_lauf(conn))
    assert dateien == []
    assert status == "kein_eintrag"


def test_fehlende_cfggameplay_liefert_leere_liste_mit_status():
    conn = _conn_mit_dateien({})
    dateien, status, _ = asyncio.run(_lauf(conn))
    assert dateien == []
    assert status == "missing"
