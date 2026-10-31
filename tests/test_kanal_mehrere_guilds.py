"""Channel-Pruefung bei Servern mit MEHREREN Discord-Guilds (Auto-Aufgaben, Ankuendigungen).

Ausgangsbefund: "Diese Channel-ID gibt es in dem gewaehlten Discord-Server nicht", obwohl der Channel (#online) existiert und der
Bot dort alle Rechte hat. Die Auswahlliste im Dashboard zeigt die Channels der gewaehlten Guild, die Pruefung nahm aber nur die
ERSTE zugeordnete Guild des Servers (conn.guild_id). Ein Channel der zweiten Guild wurde abgelehnt.

    python3 -m pytest tests/test_kanal_mehrere_guilds.py -q
"""
import asyncio
import json
import os
import sys
import time

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

G1, G2, G3 = 111, 222, 333                    # G1 + G2 gehoeren zum Server, G3 einem fremden Kunden
K1, K2, K3 = 1001, 2002, 3003                 # je ein Channel pro Guild


class FakeGuild:
    def __init__(self, kanaele):
        self._k = set(kanaele)

    def get_channel(self, kid):
        return object() if int(kid) in self._k else None


def _guilds(monkeypatch, vorhanden):
    """vorhanden: {guild_id: [channel_ids]} - alle anderen Guilds erreicht der Bot nicht."""
    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: FakeGuild(vorhanden[int(gid)]) if int(gid) in vorhanden else None)
    monkeypatch.setattr(bot.bot, "is_ready", lambda: True)


def _status(antwort):
    return None if antwort is None else (antwort.status, json.loads(antwort.body)["error"])


# ── Die Pruefung selbst ─────────────────────────────────────────────────────
def test_channel_der_zweiten_guild_wird_akzeptiert(monkeypatch):
    _guilds(monkeypatch, {G1: [K1], G2: [K2]})
    assert bot._kanal_gehoert_guilds([G1, G2], K2) is None             # vorher: abgelehnt
    assert bot._kanal_gehoert_guilds([G1, G2], K1) is None


def test_channel_einer_fremden_guild_wird_weiter_abgelehnt(monkeypatch):
    _guilds(monkeypatch, {G1: [K1], G2: [K2], G3: [K3]})              # der Bot ist auch in G3 - die gehoert aber nicht zu diesem Server
    st, text = _status(bot._kanal_gehoert_guilds([G1, G2], K3))
    assert st == 400 and "gibt es in dem gewählten Discord-Server nicht" in text
    st, text = _status(bot._kanal_gehoert_guilds([G1, G2], 999999))
    assert st == 400


def test_eine_unerreichbare_guild_verhindert_den_treffer_in_der_anderen_nicht(monkeypatch):
    _guilds(monkeypatch, {G2: [K2]})                                    # G1 erreicht der Bot gerade nicht
    assert bot._kanal_gehoert_guilds([G1, G2], K2) is None


def test_ist_keine_guild_erreichbar_kommt_die_klare_meldung(monkeypatch):
    _guilds(monkeypatch, {})
    st, text = _status(bot._kanal_gehoert_guilds([G1, G2], K2))
    assert st == 409 and "erreicht diesen Discord-Server nicht" in text
    monkeypatch.setattr(bot.bot, "is_ready", lambda: False)
    st, text = _status(bot._kanal_gehoert_guilds([G1, G2], K2))
    assert st == 409 and "nicht bei Discord angemeldet" in text


def test_eine_guild_verhaelt_sich_wie_zuvor(monkeypatch):
    _guilds(monkeypatch, {G1: [K1], G3: [K3]})
    assert bot._kanal_gehoert_guilds([G1], K1) is None
    assert _status(bot._kanal_gehoert_guilds([G1], K3))[0] == 400       # Channel einer fremden Guild
    _guilds(monkeypatch, {})
    assert _status(bot._kanal_gehoert_guilds([G1], K1))[0] == 409
    assert bot._kanal_gehoert_guilds([], K1) is None                    # ohne Guild gar nichts zu pruefen (Aufrufer fangen das ab)


def test_feldname_erscheint_in_der_meldung(monkeypatch):
    _guilds(monkeypatch, {G1: [K1], G2: [K2]})
    assert "Log-Channel-ID" in _status(bot._kanal_gehoert_guilds([G1, G2], 5, "Log-Channel"))[1]


# ── Die Endpunkte ───────────────────────────────────────────────────────────
@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "connections.json").write_text(json.dumps({"1000": {
        "service_id": "1000", "nitrado_token": "x", "owner_discord_id": "1", "map_name": "nasdara", "guild_ids": [G1, G2]}}))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    monkeypatch.setattr(bot, "_audit_log", bot.deque(maxlen=50))
    monkeypatch.setattr(bot, "ann_data", {"announcements": []})
    monkeypatch.setattr(bot, "save_announcements", lambda: None)
    _guilds(monkeypatch, {G1: [K1], G2: [K2], G3: [K3]})
    return reg._conns["1000"]


def _aufruf(monkeypatch, c, handler, daten, methode="POST", **match):
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "x", "discord": {"id": sid}, "is_admin": True, "service_id": c.service_id, "seen": time.time()}
    req = make_mocked_request(methode, "/api/x", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"}, match_info=match)

    async def body(_r):
        return daten
    monkeypatch.setattr(bot, "body", body)
    r = asyncio.run(handler(req))
    return r.status, json.loads(r.body)


def _aufgabe(kanal):
    return {"task": "online_list", "initial_execution": time.time() + 3600, "interval_seconds": 900, "note": "",
            "channel_ignore": False, "channel_id": str(kanal), "post_wenn_leer": True, "nachricht_ersetzen": False}


def test_aufgabe_anlegen_mit_channel_der_zweiten_guild(monkeypatch, conn):
    st, j = _aufruf(monkeypatch, conn, bot.create_scheduled_task, _aufgabe(K2))
    assert st == 200, j
    assert int(j["data"]["channel_id"]) == K2 and len(bot._scheduled_tasks(conn)) == 1


def test_aufgabe_anlegen_mit_channel_der_ersten_guild_geht_weiter(monkeypatch, conn):
    assert _aufruf(monkeypatch, conn, bot.create_scheduled_task, _aufgabe(K1))[0] == 200


def test_aufgabe_anlegen_mit_fremdem_channel_wird_abgelehnt_und_nichts_gespeichert(monkeypatch, conn):
    st, j = _aufruf(monkeypatch, conn, bot.create_scheduled_task, _aufgabe(K3))
    assert st == 400 and "gibt es in dem gewählten Discord-Server nicht" in j["error"]
    assert bot._scheduled_tasks(conn) == []


def test_aufgabe_aendern_auf_channel_der_zweiten_guild(monkeypatch, conn):
    st, j = _aufruf(monkeypatch, conn, bot.create_scheduled_task, _aufgabe(K1))
    tid = j["data"]["id"]
    st, j = _aufruf(monkeypatch, conn, bot.update_scheduled_task, _aufgabe(K2), "PUT", id=str(tid))
    assert st == 200, j
    assert int(bot._find_scheduled_task(conn, tid)["channel_id"]) == K2
    st, j = _aufruf(monkeypatch, conn, bot.update_scheduled_task, _aufgabe(K3), "PUT", id=str(tid))
    assert st == 400 and int(bot._find_scheduled_task(conn, tid)["channel_id"]) == K2      # fremder Channel: Aufgabe bleibt unveraendert


def test_ankuendigung_mit_channel_der_zweiten_guild(monkeypatch, conn):
    daten = {"day": "monday", "time": "18:00", "message": "Hallo", "repeat": "weekly", "channel_id": str(K2)}
    st, j = _aufruf(monkeypatch, conn, bot.create_announcement, daten)
    assert st == 200, j
    assert int(j["data"]["channel_id"]) == K2
    st, j = _aufruf(monkeypatch, conn, bot.create_announcement, dict(daten, channel_id=str(K3)))
    assert st == 400 and "gibt es in dem gewählten Discord-Server nicht" in j["error"]
