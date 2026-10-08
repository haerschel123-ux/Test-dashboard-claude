"""Dashboard-Status „Online/Offline“: Spielabfrage (A2S) ODER Nitrado „gestartet“.

    python3 -m pytest tests/test_server_status.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_airstrike import call, servers  # noqa: F401 - Fixture für pytest


class _Nitrado:
    def __init__(self, status, spieler=1):
        self.service_id, self._status, self._spieler = "1000", status, spieler

    async def get_info(self):
        if self._status is None:
            raise RuntimeError("Nitrado nicht erreichbar")
        return {"status": self._status, "query": {"player_current": self._spieler, "player_max": 60, "map": "enoch"}}


@pytest.mark.parametrize("a2s,nitrado,erwartet_online,erwartet_a2s", [
    ({"players": 3, "max_players": 60, "map": "enoch"}, "started", True, True),    # beides ok
    (None, "started", True, False),                      # Query-Port tot, Nitrado sagt „läuft“ → online
    ({"players": 3, "max_players": 60, "map": "enoch"}, "stopped", True, True),   # A2S hat Vorrang
    (None, "stopped", False, False),                     # wirklich aus
    (None, "restarting", False, False),                  # Zwischenzustand: die Anzeige übernimmt das Frontend
    (None, None, False, False),                          # Nitrado nicht erreichbar, keine A2S → offline
])
def test_online_aus_a2s_oder_nitrado(monkeypatch, servers, a2s, nitrado, erwartet_online, erwartet_a2s):
    a, _ = servers
    a.data["server_ip"] = "10.0.0.1"; a.data["query_port"] = 27016
    a.api = _Nitrado(nitrado)
    monkeypatch.setattr(bot, "a2s_query", lambda ip, port, timeout=3.0: a2s)
    status, result = call(monkeypatch, a, bot.api_server_status)
    assert status == 200, result
    d = result["data"]
    assert d["online"] is erwartet_online and d["a2s_erreichbar"] is erwartet_a2s
    if nitrado:
        assert d["nitrado"]["state"] == nitrado
