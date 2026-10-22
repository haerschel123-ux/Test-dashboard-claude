"""Zahlen unten auf der Startseite: Discord-Communities, verbundene Spielserver, erfasste Spieler.

Drei Gesamtsummen, auch ohne Anmeldung abrufbar - nie etwas Einzelnes eines Kunden.

    python3 -m pytest tests/test_oeffentliche_zahlen.py -q
"""
import asyncio
import json
import os
import sys
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


class _Bot:
    def __init__(self, gilden, bereit=True):
        self.guilds = list(gilden)
        self._bereit = bereit

    def is_ready(self):
        return self._bereit


@pytest.fixture
def welt(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    flat = {"1000": {"service_id": "1000", "nitrado_token": "geheim-a", "owner_discord_id": "1", "name": "Kunde A"},
            "2000": {"service_id": "2000", "nitrado_token": "geheim-b", "owner_discord_id": "2", "name": "Kunde B"},
            "3000": {"service_id": "3000", "nitrado_token": "", "name": "Platzhalter ohne Token"}}
    (tmp_path / "connections.json").write_text(json.dumps(flat))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    monkeypatch.setattr(bot, "db", bot.EconomyDB(str(tmp_path / "e.db")))
    monkeypatch.setattr(bot, "bot", _Bot([object(), object(), object(), object()]))
    monkeypatch.setitem(bot._START_ZAHLEN, "ts", 0.0)
    monkeypatch.setitem(bot._START_ZAHLEN, "daten", None)
    return reg


def _holen(pfad="/api/public/stats", cookie=None):
    async def lauf():
        async with TestClient(TestServer(bot.build_app())) as c:
            r = await c.get(pfad, cookies=cookie)
            try:
                daten = await r.json()
            except Exception:  # noqa: BLE001
                daten = None
            return r.status, daten, r.headers
    return asyncio.run(lauf())


def _spieler(*zeilen):
    for gid, sid, name in zeilen:
        bot.db.roster_upsert_login(gid, sid, name)


def test_zahlen_ohne_anmeldung(welt):
    _spieler((1, "1000", "Anna"), (1, "1000", "Berta"), (2, "2000", "Carla"))
    st, j, h = _holen()
    assert st == 200 and j == {"ok": True, "data": {"communities": 4, "servers": 2, "players": 3}}
    assert "max-age=300" in h["Cache-Control"]


def test_nur_die_drei_summen_nichts_einzelnes(welt):
    _spieler((1, "1000", "Anna"))
    _st, j, _h = _holen()
    assert set(j["data"]) == {"communities", "servers", "players"}
    text = json.dumps(j)
    for geheim in ("geheim-a", "geheim-b", "Anna", "Kunde A", "Kunde B", "1000", "2000"):
        assert geheim not in text


def test_server_ohne_token_zaehlen_nicht(welt):
    assert len(welt.all()) == 3
    _st, j, _h = _holen()
    assert j["data"]["servers"] == 2


def test_derselbe_name_zaehlt_einmal(welt):
    # gleicher Name auf zwei Servern, bei zwei Guilds und in anderer Schreibweise
    _spieler((1, "1000", "Anna"), (1, "2000", "Anna"), (2, "1000", "Anna"), (1, "1000", "ANNA"), (1, "1000", "anna"),
             (1, "1000", "Berta"))
    _st, j, _h = _holen()
    assert j["data"]["players"] == 2


def test_ohne_spieler_ist_die_zahl_null(welt):
    _st, j, _h = _holen()
    assert j["data"]["players"] == 0 and j["data"]["servers"] == 2


def test_ergebnis_wird_fuenf_minuten_gemerkt(welt, monkeypatch):
    _spieler((1, "1000", "Anna"))
    assert _holen()[1]["data"]["players"] == 1
    _spieler((1, "1000", "Berta"), (1, "1000", "Carla"))
    assert _holen()[1]["data"]["players"] == 1                                  # noch der gemerkte Stand
    monkeypatch.setitem(bot._START_ZAHLEN, "ts", time.time() - bot._START_ZAHLEN_TTL - 1)
    assert _holen()[1]["data"]["players"] == 3                                  # abgelaufen: neu gezaehlt


def test_bot_noch_nicht_bereit_gibt_keine_zahl_und_merkt_nichts(welt, monkeypatch):
    monkeypatch.setattr(bot, "bot", _Bot([object()], bereit=False))
    st, j, _h = _holen()
    assert st == 503 and j["ok"] is False and bot._START_ZAHLEN["daten"] is None
    monkeypatch.setattr(bot, "bot", _Bot([object(), object()], bereit=True))
    st, j, _h = _holen()
    assert st == 200 and j["data"]["communities"] == 2                         # keine gemerkte Null


def test_nur_dieser_pfad_ist_oeffentlich(welt):
    assert bot._sess_is_public("/api/public/stats") is True
    assert bot._sess_is_public("/api/public/anderes") is False and bot._sess_is_public("/api/public/stats/x") is False
    for geschuetzt in ("/api/options", "/api/server/status", "/api/serverlist", "/api/tools/meta"):
        st, _j, _h = _holen(geschuetzt)
        assert st in (401, 404), geschuetzt


def test_zaehlung_fasst_die_datenbank_nur_lesend_an(welt):
    _spieler((1, "1000", "Anna"), (1, "1000", "Berta"))
    vorher = bot.db.roster_spieler_gesamt()
    _holen()
    assert bot.db.roster_spieler_gesamt() == vorher == 2
