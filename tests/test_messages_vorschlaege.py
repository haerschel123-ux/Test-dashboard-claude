"""Messages Generator: Vorschlaege (fertige Nachrichten zum Anklicken).

Jeder Vorschlag muss von der normalen Pruefung des Generators (_tool_messages_eintrag_aus_payload) akzeptiert werden - in
Deutsch UND Englisch - und ein gueltiges db/messages.xml ergeben. Dazu: GET /api/tools/messages liefert sie in jedem Zustand.

    python3 -m pytest tests/test_messages_vorschlaege.py -q
"""
import asyncio
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

V = bot._tool_messages_vorschlaege()
VORSCHLAEGE = V["vorschlaege"]
KATEGORIEN = {k["id"] for k in V["kategorien"]}


def _payload(v, sprache):
    return {"text": v["text"][sprache], "onconnect": v["onconnect"], "delay": v["delay"], "repeat": v["repeat"],
            "deadline": v["deadline"], "shutdown": v["shutdown"]}


def test_es_gibt_vorschlaege_in_allen_kategorien():
    assert len(VORSCHLAEGE) >= 15
    assert {v["kategorie"] for v in VORSCHLAEGE} == KATEGORIEN
    for k in V["kategorien"]:
        assert k["de"] and k["en"]


def test_ids_sind_eindeutig_und_titel_in_beiden_sprachen_da():
    ids = [v["id"] for v in VORSCHLAEGE]
    assert len(ids) == len(set(ids))
    for v in VORSCHLAEGE:
        assert v["titel"]["de"].strip() and v["titel"]["en"].strip()


@pytest.mark.parametrize("sprache", ["de", "en"])
@pytest.mark.parametrize("v", VORSCHLAEGE, ids=lambda v: v["id"])
def test_jeder_vorschlag_besteht_die_pruefung_des_generators(v, sprache):
    e = bot._tool_messages_eintrag_aus_payload(_payload(v, sprache), 0)
    assert e["text"] == v["text"][sprache].strip() and 0 < len(e["text"]) <= bot._TOOL_MESSAGES_MAX_TEXT


@pytest.mark.parametrize("v", VORSCHLAEGE, ids=lambda v: v["id"])
def test_deutsch_und_englisch_sind_verschieden_und_nutzen_dieselben_platzhalter(v):
    de, en = v["text"]["de"], v["text"]["en"]
    assert de != en
    assert sorted(re.findall(r"#\w+", de)) == sorted(re.findall(r"#\w+", en))        # gleiche Platzhalter in beiden Sprachen
    assert set(re.findall(r"#\w+", de)) <= {"#name", "#port", "#tmin"}
    assert ("discord.gg/deinserver" in de) == ("discord.gg/yourserver" in en)         # der Link-Platzhalter passt zur Sprache


@pytest.mark.parametrize("v", VORSCHLAEGE, ids=lambda v: v["id"])
def test_tmin_nur_mit_countdown_und_shutdown_ist_im_titel_gekennzeichnet(v):
    assert ("#tmin" in v["text"]["de"]) == (v["deadline"] is not None)
    if v["deadline"] is not None:
        titel_de, titel_en = v["titel"]["de"], v["titel"]["en"]
        if v["shutdown"]:
            assert "stoppt den Server" in titel_de and "stops the server" in titel_en          # wer den Server stoppt, soll es vorher wissen
        else:
            assert "nicht" in titel_de and "not" in titel_en
    else:
        assert not v["shutdown"]


def test_alle_vorschlaege_ergeben_ein_gueltiges_messages_xml():
    for sprache in ("de", "en"):
        eintraege = [bot._tool_messages_eintrag_aus_payload(_payload(v, sprache), i) for i, v in enumerate(VORSCHLAEGE)]
        xml = bot._tool_messages_xml_bauen(eintraege)
        wurzel = ET.fromstring(xml.encode("utf-8"))
        assert len(wurzel.findall("message")) == len(VORSCHLAEGE)
        assert bot._tool_messages_lesen(wurzel) == eintraege                                   # Hin und zurueck ohne Verlust
        assert len(xml.encode("utf-8")) < 20_000


def test_die_liste_ist_unabhaengig_vom_server_und_aenderbar_ohne_wirkung():
    a, b = bot._tool_messages_vorschlaege(), bot._tool_messages_vorschlaege()
    assert a == b
    a["vorschlaege"][0]["text"]["de"] = "geaendert"
    assert bot._tool_messages_vorschlaege()["vorschlaege"][0]["text"]["de"] != "geaendert"


# ── GET /api/tools/messages liefert die Vorschlaege in jedem Zustand ─────────────────────────
@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "connections.json").write_text(json.dumps({"1000": {
        "service_id": "1000", "nitrado_token": "x", "map_name": "Livonia", "ftp_mission_dir": "/mission", "owner_discord_id": "1000"}}))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    return reg._conns["1000"]


def _get(monkeypatch, conn, datei, mission=True):
    async def lesen(c, name, loop):
        return datei
    monkeypatch.setattr(bot, "_tools_datei_lesen", lesen)
    monkeypatch.setattr(bot, "_mission_dir_of", lambda c: "/mission" if mission else "")
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "x", "discord": {"id": sid}, "is_admin": True, "service_id": conn.service_id, "seen": time.time()}
    req = make_mocked_request("GET", "/api/tools/messages", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    r = asyncio.run(bot.api_tools_messages_get(req))
    return r.status, json.loads(r.body)


@pytest.mark.parametrize("zustand", ["vorhanden", "fehlt", "kein_mission_ordner"])
def test_get_enthaelt_die_vorschlaege(monkeypatch, conn, zustand):
    if zustand == "vorhanden":
        st, j = _get(monkeypatch, conn, ("<messages><message><repeat>5</repeat><text>Hi</text></message></messages>", "ok"))
    elif zustand == "fehlt":
        st, j = _get(monkeypatch, conn, (None, "missing"))
    else:
        st, j = _get(monkeypatch, conn, (None, "missing"), mission=False)
    assert st == 200, j
    d = j["data"]
    assert d["vorschlaege"] == V["vorschlaege"] and d["kategorien"] == V["kategorien"]
    if zustand == "vorhanden":
        assert [e["text"] for e in d["eintraege"]] == ["Hi"]
    else:
        assert d["eintraege"] == []


def test_vorschlaege_aendern_die_datei_nicht(monkeypatch, conn):
    """Die Vorschlaege sind reine Anzeige: ohne Speichern (POST mit commit) wird nie geschrieben."""
    geschrieben = []

    async def schreiben(commit, c, name, text, loop):
        geschrieben.append((commit, name))
        return True
    monkeypatch.setattr(bot, "_tools_datei_schreiben_wenn", schreiben)
    _get(monkeypatch, conn, (None, "missing"))
    assert geschrieben == []
