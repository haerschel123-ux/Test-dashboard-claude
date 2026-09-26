"""Regressionstest: ein Kunde, dessen Nitrado-Server bereits einer Guild
zugeordnet ist, muss mit demselben Token trotzdem eine ANDERE Discord-Guild
anfragen koennen (z. B. weil er dort ebenfalls Mitglied/Eigentuemer ist).

Brigarde meldete: das ging nicht - die Anfrage tauchte nie in der
Serverliste auf. Ursache: post_select_server() (/api/auth/select-server)
schrieb conn.data["guild_id_requested"] nur, wenn der Server noch GAR
KEINER Guild zugeordnet war ("not conn.guild_id") - war irgendeine Guild
schon zugeordnet, wurde eine Anfrage fuer eine ANDERE Guild stillschweigend
verworfen. post_setup_guild() (die manuelle Guild-ID-Eingabe) hatte diese
Einschraenkung nie und schrieb schon immer unconditional - post_select_server
ist jetzt konsistent dazu.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_guild_wechsel_anfrage.py -v
"""
import asyncio
import os
import sys
import time

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _run(coro):
    return asyncio.run(coro)


class _FakeNitradoAPI:
    def __init__(self, token=None, service_id=None, base=None):
        pass

    async def get_info(self):
        return {"data": {"gameserver": {}}}

    async def close(self):
        pass


def _request_mit_session(sess_daten, body_daten):
    sid = f"test-sid-{time.time_ns()}"
    bot._SESS_STORE[sid] = dict(sess_daten, seen=time.time())
    req = make_mocked_request("POST", "/api/auth/select-server",
                              headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    return req, sid


def _basis_session(service_id, guild_id_neu, owned_guilds):
    return {
        "token": "fake-token", "discord": {"id": "555"},
        "owned_guilds": owned_guilds,
        "guild_id": str(guild_id_neu),
        "gameservers": [{"id": service_id, "status": "started"}],
    }


def test_zweite_guild_anfrage_wird_trotz_bestehender_zuordnung_gespeichert(monkeypatch):
    alte_guild, neue_guild = 111, 222
    service_id = "server-multi-1"
    conn = bot.connections.upsert(service_id, nitrado_token="fake-token", owner_discord_id="555")
    bot.connections.assign_guild(service_id, alte_guild)
    assert conn.guild_id == alte_guild

    monkeypatch.setattr(bot, "NitradoAPI", _FakeNitradoAPI)
    monkeypatch.setattr(bot, "_apply_gameserver_info", lambda info, c=None: None)
    monkeypatch.setattr(bot, "_server_view", lambda s: {"name": "Testserver"})

    async def kein_alarm(*a, **k):
        pass
    monkeypatch.setattr(bot, "_betreiber_alarm", kein_alarm)

    async def kein_init(*a, **k):
        pass
    monkeypatch.setattr(bot.bot, "init_nitrado", kein_init)
    monkeypatch.setattr(bot, "_panel_view_registrieren", lambda c: None)

    sess = _basis_session(service_id, neue_guild, [{"id": str(neue_guild)}])
    req, sid = _request_mit_session(sess, {"service_id": service_id})
    monkeypatch.setattr(bot, "body", lambda request: _antwort_fuer(request, {"service_id": service_id}))

    _run(bot.post_select_server(req))

    # Kernpruefung: die NEUE Anfrage ist vermerkt ...
    assert neue_guild in conn.guild_ids_requested
    # ... und die BESTEHENDE Freischaltung bleibt unangetastet.
    assert conn.guild_id == alte_guild
    bot._SESS_STORE.pop(sid, None)


async def _antwort_fuer(request, daten):
    return daten


def test_gleiche_guild_erneut_ausgewaehlt_loest_keine_unnoetige_anfrage_aus(monkeypatch):
    guild = 333
    service_id = "server-multi-2"
    conn = bot.connections.upsert(service_id, nitrado_token="fake-token", owner_discord_id="555")
    bot.connections.assign_guild(service_id, guild)

    monkeypatch.setattr(bot, "NitradoAPI", _FakeNitradoAPI)
    monkeypatch.setattr(bot, "_apply_gameserver_info", lambda info, c=None: None)
    monkeypatch.setattr(bot, "_server_view", lambda s: {"name": "Testserver"})
    alarme = []

    async def alarm_zaehlen(text, farbe=0xE67E22):
        alarme.append(text)
    monkeypatch.setattr(bot, "_betreiber_alarm", alarm_zaehlen)

    async def kein_init(*a, **k):
        pass
    monkeypatch.setattr(bot.bot, "init_nitrado", kein_init)
    monkeypatch.setattr(bot, "_panel_view_registrieren", lambda c: None)

    sess = _basis_session(service_id, guild, [{"id": str(guild)}])
    req, sid = _request_mit_session(sess, {"service_id": service_id})
    monkeypatch.setattr(bot, "body", lambda request: _antwort_fuer(request, {"service_id": service_id}))

    _run(bot.post_select_server(req))

    assert "Neue Premium-Anfrage" not in " ".join(alarme)
    assert conn.guild_id == guild
    bot._SESS_STORE.pop(sid, None)


def test_login_vorauswahl_eigentuemer_zeigt_immer_auswahl():
    # Brigardes ausdrueckliche Vorgabe: Eigentuemer-Konten sehen bei JEDEM
    # Login die Kachel-Auswahl, auch wenn nur eine eigene Guild vorhanden und
    # der Server bereits zugeordnet ist - kein automatisches Ueberspringen
    # mehr, egal wie eindeutig der Fall ist.
    conn = bot.connections.upsert("srv-login-1", nitrado_token="fake", owner_discord_id="777")
    bot.connections.assign_guild("srv-login-1", 111)
    eigene = [{"id": "111"}]
    assert bot._login_guild_vorauswahl("777", eigene, conn, False) is None


def test_login_vorauswahl_gast_ueberspringt_auswahl_wenn_eindeutig():
    # Gast-Zugaenge haben keine eigene Server-Auswahl - fuer sie bleibt die
    # bisherige automatische Vorbelegung bestehen, wenn es nichts zu
    # entscheiden gibt.
    conn = bot.connections.upsert("srv-login-1g", nitrado_token="fake", owner_discord_id="777g")
    bot.connections.assign_guild("srv-login-1g", 111)
    eigene = [{"id": "111"}]
    assert bot._login_guild_vorauswahl("777g", eigene, conn, True) == "111"


def test_login_vorauswahl_gast_bei_weiterer_unverbundener_guild_zeigt_auswahl():
    # Brigardes gemeldeter Fall (fuer Gast-Zugaenge weiterhin relevant):
    # derselbe Nitrado Server 1 ist bereits mit Discord Server 1 verbunden,
    # das Konto besitzt zusaetzlich Discord Server 2, an dem noch KEIN
    # eigener Server haengt - hier darf die Guild-Auswahl beim Login nicht
    # automatisch uebersprungen werden, sonst kommt der Kunde nie mehr bis
    # zur Anfrage fuer Discord Server 2.
    conn = bot.connections.upsert("srv-login-2", nitrado_token="fake", owner_discord_id="778")
    bot.connections.assign_guild("srv-login-2", 111)
    eigene = [{"id": "111"}, {"id": "222"}]
    assert bot._login_guild_vorauswahl("778", eigene, conn, True) is None


def test_login_vorauswahl_ohne_verbindung_zeigt_auswahl():
    assert bot._login_guild_vorauswahl("779", [{"id": "111"}], None, False) is None
    assert bot._login_guild_vorauswahl("779", [{"id": "111"}], None, True) is None


def test_login_vorauswahl_ohne_zugeordnete_guild_zeigt_auswahl():
    conn = bot.connections.upsert("srv-login-3", nitrado_token="fake", owner_discord_id="780")
    assert conn.guild_id is None
    assert bot._login_guild_vorauswahl("780", [{"id": "111"}], conn, False) is None
    assert bot._login_guild_vorauswahl("780", [{"id": "111"}], conn, True) is None
