"""Spawn Point Generator: <group name="...">-Struktur der echten cfgplayerspawnpoints.xml.

Bohemias Dateien legen die Punkte in Gruppen (z. B. "WestCherno") unter
<generator_posbubbles> ab. Frueher las das Tool nur lose <pos>-Elemente und
zeigte deshalb "0 Spawn-Punkte" - und ein Speichern haette Gruppen zerlegt.

    python3 -m pytest tests/test_spawnpoint_gruppen.py -v
"""
import os
import sys
import xml.etree.ElementTree as ET

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

DATEI = '''<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<playerspawnpoints>
    <fresh>
        <spawn_params>
            <min_dist_infected>30</min_dist_infected>
        </spawn_params>
        <generator_params>
            <grid_density>4</grid_density>
            <allow_in_water>false</allow_in_water>
        </generator_params>
        <generator_posbubbles>
            <group name="WestCherno">
                <pos x="6063.018555" z="1931.907227" />
                <pos x="5933.964844" z="2171.072998" />
            </group>
            <group name="EastCherno">
                <pos x="8040.858398" z="3332.236328" />
            </group>
        </generator_posbubbles>
    </fresh>
    <hop></hop>
    <travel></travel>
</playerspawnpoints>
'''


def _lesen(text):
    return bot._tool_spawnpoint_positionen_lesen(ET.fromstring(text))


def test_punkte_in_gruppen_werden_gelesen():
    pts = _lesen(DATEI)
    assert len(pts) == 3
    assert [p.get("group") for p in pts] == ["WestCherno", "WestCherno", "EastCherno"]
    assert pts[0]["x"] == "6063.018555"


def test_lose_und_gruppierte_punkte_zusammen():
    text = DATEI.replace('<group name="EastCherno">', '<pos x="1" z="2" />\n            <group name="EastCherno">')
    pts = _lesen(text)
    assert len(pts) == 4 and {"x": "1", "z": "2"} in pts
    assert sum(1 for p in pts if p.get("group")) == 3


def test_speichern_behaelt_gruppen_und_rest_der_datei():
    pts = [{"x": float(p["x"]), "z": float(p["z"]), "group": p.get("group", "")} for p in _lesen(DATEI)]
    neu = bot._tool_spawnpoint_positionen_schreiben(DATEI, pts, "replace")
    ET.fromstring(neu)                                   # bleibt gueltiges XML
    assert [(p.get("group"), round(float(p["x"]))) for p in _lesen(neu)] == \
        [(p.get("group"), round(float(p["x"]))) for p in _lesen(DATEI)]
    assert neu.count("<group ") == 2 and "</group>" in neu
    for rest in ("<min_dist_infected>30</min_dist_infected>", "<grid_density>4</grid_density>",
                 "<hop></hop>", "<travel></travel>"):
        assert rest in neu


def test_punkt_loeschen_und_neuen_hinzufuegen():
    pts = [{"x": float(p["x"]), "z": float(p["z"]), "group": p.get("group", "")} for p in _lesen(DATEI)]
    pts = [p for p in pts if round(p["x"]) != 5934]       # einen alten Punkt loeschen
    pts.append({"x": 150.2, "z": 200.5})                    # neuen (losen) Punkt anlegen
    pts.append({"x": 300.0, "z": 400.0, "group": "Neu Gruppe"})
    neu = bot._tool_spawnpoint_positionen_schreiben(DATEI, pts, "replace")
    ET.fromstring(neu)
    erg = _lesen(neu)
    assert len(erg) == 4
    xs = {round(float(p["x"])) for p in erg}
    assert 5934 not in xs and {150, 300, 6063, 8041} <= xs
    assert any(p.get("group") == "Neu Gruppe" for p in erg)
    assert any("group" not in p and round(float(p["x"])) == 150 for p in erg)


def test_ungueltiger_gruppenname_wird_zu_losem_punkt():
    pts = [{"x": 1.0, "z": 2.0, "group": '"><evil/>'}]
    neu = bot._tool_spawnpoint_positionen_schreiben(DATEI, pts, "replace")
    ET.fromstring(neu)
    assert "evil" not in neu and len(_lesen(neu)) == 1 and "group" not in _lesen(neu)[0]


def test_leere_liste_leert_die_bubbles_ohne_reste():
    neu = bot._tool_spawnpoint_positionen_schreiben(DATEI, [], "replace")
    ET.fromstring(neu)
    assert _lesen(neu) == [] and "<group" not in neu


def test_flache_ausgabe_wie_bisher_ohne_gruppen():
    flach = DATEI.replace('<group name="WestCherno">', "").replace('<group name="EastCherno">', "") \
        .replace("</group>", "")
    neu = bot._tool_spawnpoint_positionen_schreiben(
        flach, [{"x": 5.0, "z": 6.0}, {"x": 7.0, "z": 8.0}], "replace")
    assert neu.count("<pos ") == 2 and "<group" not in neu
    assert '            <pos x="5" z="6" />' in neu


# ── API: Vorschau schreibt nichts, Fehler werden benannt statt "HTTP 500" ──
import asyncio
import json
import re

sys.path.insert(0, os.path.join(REPO, "tests"))
import test_deployment as td  # noqa: E402  (nutzt dessen FTP-Attrappe)


class _MP:
    def setattr(self, o, n, v):
        setattr(o, n, v)


def _datei_mit_wasser():
    return DATEI.replace("<grid_density>4</grid_density>",
                         "<grid_density>4</grid_density>\n            <allow_in_water>false</allow_in_water>")


def _post(conn, daten):
    req = td._request(_MP(), conn, "POST", "/api/tools/spawnpoint", daten)
    r = asyncio.run(bot.api_tools_spawnpoint_post(req))
    return r.status, json.loads(r.body.decode("utf-8"))


def _punkte():
    return [{"x": float(p["x"]), "z": float(p["z"]), "group": p.get("group", "")}
            for p in _lesen(DATEI)]


def test_vorschau_schreibt_nichts_auf_den_server():
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser()
    vorher = conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"]
    status, j = _post(conn, {"positions": _punkte()[:2], "mode": "replace", "allow_in_water": False})
    assert status == 200 and j["data"]["total"] == 2
    assert conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] == vorher       # unveraendert


def test_erst_commit_schreibt():
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser()
    status, j = _post(conn, {"positions": _punkte()[:1], "mode": "replace", "allow_in_water": False,
                             "commit": True})
    assert status == 200
    assert conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"].count("<pos ") == 1


def test_unerwarteter_fehler_wird_benannt_statt_http_500(monkeypatch):
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser()

    def kaputt(*a, **k):
        raise RuntimeError("Testfehler")
    monkeypatch.setattr(bot, "_tool_spawnpoint_positionen_schreiben", kaputt)
    status, j = _post(conn, {"positions": _punkte(), "mode": "replace"})
    assert status == 500 and "RuntimeError: Testfehler" in j["error"] and "nichts wurde gespeichert" in j["error"]


def test_ftp_fehler_beim_lesen_und_schreiben_werden_benannt(monkeypatch):
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser()

    async def lesen_kaputt(*a, **k):
        raise OSError("Verbindung getrennt")
    monkeypatch.setattr(bot, "_tools_datei_lesen", lesen_kaputt)
    status, j = _post(conn, {"positions": _punkte(), "mode": "replace"})
    assert status == 502 and "OSError: Verbindung getrennt" in j["error"]


def test_vorschau_nennt_aenderung_gegenueber_dem_server():
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser()
    pts = _punkte()[:2] + [{"x": 111.0, "z": 222.0}]         # einen alten Punkt weglassen, einen neuen dazu
    status, j = _post(conn, {"positions": pts, "mode": "replace", "allow_in_water": False})
    assert status == 200
    assert j["data"]["aenderung"] == {"vorher": 3, "nachher": 3, "neu": 1, "entfernt": 1}


def test_unveraendert_speichern_aendert_die_datei_nicht():
    """Keine stille Rundung: Koordinaten behalten alle Nachkommastellen aus Bohemias Datei."""
    neu = bot._tool_spawnpoint_positionen_schreiben(DATEI, _punkte(), "replace")
    assert neu == DATEI
