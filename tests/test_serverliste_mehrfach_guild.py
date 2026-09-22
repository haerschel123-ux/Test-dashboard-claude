"""Regressionstest fuer die Serverliste-Endpunkte nach dem Mehrfach-Guild-
Umbau: post_admin_server_guild HAENGT jetzt an (statt zu ersetzen),
post_admin_server_guild_remove entfernt gezielt EINE Guild.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_serverliste_mehrfach_guild.py -v
"""
import asyncio
import os
import sys

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _run(coro):
    return asyncio.run(coro)


async def _kein_admin_check(request):
    return None


async def _antwort_fuer(request, daten):
    return daten


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(bot, "_require_admin", _kein_admin_check)

    async def _keine_rollen(owner_id):
        return []
    monkeypatch.setattr(bot, "_rollen_fuer_kunden_stufe", _keine_rollen)

    async def _keine_commands(gid):
        return {}
    monkeypatch.setattr(bot, "_register_guild_commands", _keine_commands)

    async def _kein_aufraeumen(gid):
        return {}
    monkeypatch.setattr(bot, "_guild_aufraeumen", _kein_aufraeumen)


def _request_mit_body(pfad, daten, match_info=None):
    req = make_mocked_request("POST", pfad, match_info=match_info or {})
    bot.body = lambda request: _antwort_fuer(request, daten)
    return req


def test_post_admin_server_guild_haengt_an_statt_zu_ersetzen(monkeypatch):
    service_id = "sl-mfg-1"
    bot.connections.upsert(service_id, nitrado_token="fake", owner_discord_id="1")
    req = _request_mit_body("/api/admin/servers/sl-mfg-1/guild",
                            {"guild_id": "151597221870882418"},
                            {"service_id": service_id})
    _run(bot.post_admin_server_guild(req))
    req2 = _request_mit_body("/api/admin/servers/sl-mfg-1/guild",
                             {"guild_id": "144749039048694173"},
                             {"service_id": service_id})
    _run(bot.post_admin_server_guild(req2))

    conn = bot.connections.for_service(service_id)
    assert set(conn.guild_ids) == {151597221870882418, 144749039048694173}


def test_post_admin_server_guild_remove_entfernt_nur_eine(monkeypatch):
    service_id = "sl-mfg-2"
    bot.connections.upsert(service_id, nitrado_token="fake", owner_discord_id="2")
    bot.connections.add_guild(service_id, 151597221870882418)
    bot.connections.add_guild(service_id, 144749039048694173)

    req = make_mocked_request(
        "DELETE", "/api/admin/servers/sl-mfg-2/guild/151597221870882418",
        match_info={"service_id": service_id, "guild_id": "151597221870882418"})
    _run(bot.post_admin_server_guild_remove(req))

    conn = bot.connections.for_service(service_id)
    assert conn.guild_ids == [144749039048694173]
