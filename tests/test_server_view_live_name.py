"""Regressionstest: die Server-Auswahl beim Token-Eintragen soll den ECHTEN
DayZ-Servernamen zeigen (z. B. "After The Outbreak PvP/PvE"), nicht nur
Nitrados generisches Service-Label ("Gameserver - 10 Slots").

Brigarde meldete: in der Auswahl beim Verbinden eines Servers standen zwei
Eintraege mit demselben Namen "Gameserver - 10 Slots (active)" statt der
erwarteten echten Servernamen. Ursache: _server_view() nutzte ausschliesslich
svc["details"]["name"] - das von Nitrado selbst vergebene Service-Label, das
den echten, im Spiel sichtbaren Servernamen nicht kennt. Der steckt erst in
der Live-A2S-Antwort des laufenden Servers.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_server_view_live_name.py -v
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _run(coro):
    return asyncio.run(coro)


class _FakeNitradoAPI:
    def __init__(self, token=None, service_id=None, base=None):
        self.service_id = service_id

    async def get_info(self):
        return {"ip": "1.2.3.4", "query": {"connect_port": 2302}}

    async def close(self):
        pass


def test_aktiver_server_bekommt_echten_namen_von_a2s(monkeypatch):
    monkeypatch.setattr(bot, "NitradoAPI", _FakeNitradoAPI)

    def fake_a2s(ip, port, timeout=3.0):
        assert ip == "1.2.3.4"
        assert port == 2302
        return {"name": "After The Outbreak PvP/PvE"}
    monkeypatch.setattr(bot, "a2s_query", fake_a2s)

    svc = {"id": "111", "status": "active",
           "details": {"name": "Gameserver - 10 Slots"}}
    view = _run(bot._server_view_mit_echtem_namen("tok", "https://api.nitrado.net", svc))
    assert view["name"] == "After The Outbreak PvP/PvE"
    assert view["id"] == "111"


def test_inaktiver_server_behaelt_generischen_namen(monkeypatch):
    aufgerufen = []
    monkeypatch.setattr(bot, "NitradoAPI",
                        lambda **kw: aufgerufen.append(kw) or _FakeNitradoAPI(**kw))

    svc = {"id": "222", "status": "stopped",
           "details": {"name": "Gameserver - 10 Slots"}}
    view = _run(bot._server_view_mit_echtem_namen("tok", "https://api.nitrado.net", svc))
    assert view["name"] == "Gameserver - 10 Slots"
    # Fuer nicht laufende Server wird kein API-Aufruf mehr gemacht.
    assert aufgerufen == []


def test_a2s_ohne_antwort_behaelt_generischen_namen(monkeypatch):
    monkeypatch.setattr(bot, "NitradoAPI", _FakeNitradoAPI)
    monkeypatch.setattr(bot, "a2s_query", lambda ip, port, timeout=3.0: None)

    svc = {"id": "333", "status": "started",
           "details": {"name": "Gameserver - 10 Slots"}}
    view = _run(bot._server_view_mit_echtem_namen("tok", "https://api.nitrado.net", svc))
    assert view["name"] == "Gameserver - 10 Slots"


def test_get_info_fehler_wird_abgefangen(monkeypatch):
    class _KaputteAPI:
        def __init__(self, **kw):
            pass

        async def get_info(self):
            raise RuntimeError("Verbindungsfehler")

        async def close(self):
            pass
    monkeypatch.setattr(bot, "NitradoAPI", _KaputteAPI)

    svc = {"id": "444", "status": "active",
           "details": {"name": "Gameserver - 10 Slots"}}
    view = _run(bot._server_view_mit_echtem_namen("tok", "https://api.nitrado.net", svc))
    assert view["name"] == "Gameserver - 10 Slots"
