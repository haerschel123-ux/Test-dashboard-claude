"""Tests fuer die Neustart-Ankuendigungstexte (60/30/15/10/5/3 Minuten vorher)
und den Countdown der Auto-Aufgabe "Server neu starten".

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/ -v
"""
import asyncio
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


class _FakeConn:
    service_id = "1000"
    guild_id = None

    def get(self, k, default=None):  # noqa: ARG002 - Signatur wie ServerConnection.get
        return default


def test_volle_stunde_wird_als_stunde_formatiert():
    assert bot._restart_dauer_text(60, "de") == "1 Stunde"
    assert bot._restart_dauer_text(60, "en") == "1 hour"


def test_minuten_bleiben_minuten():
    for mins in (30, 15, 10, 5, 3):
        assert bot._restart_dauer_text(mins, "de") == f"{mins} Minuten"
        assert bot._restart_dauer_text(mins, "en") == f"{mins} minutes"


def test_zwei_stunden_wuerden_als_stunden_erkannt():
    # Nicht Teil der aktuellen Ankuendigungs-Zeitpunkte, aber die Funktion
    # soll auch fuer andere volle Stunden korrekt pluralisieren.
    assert bot._restart_dauer_text(120, "de") == "2 Stunden"
    assert bot._restart_dauer_text(120, "en") == "2 hours"


class _FakeChannel:
    def __init__(self, sink):
        self.sink = sink

    async def send(self, embed=None, view=None):  # noqa: ARG002
        self.sink.append(embed.title)


def test_restart_task_countdown_feuert_nur_im_fenster():
    """Postet in den fuer DIE AUFGABE konfigurierten Channel (channel_id),
    nicht in den restart-Feed - genau das war der gemeldete Fehler: die
    Ankuendigung landete im falschen Channel und blieb dort unsichtbar."""
    b = bot.DayZBot()
    calls = []

    async def fake_resolve(ch_id):  # noqa: ARG001
        return _FakeChannel(calls)

    b._resolve_channel = fake_resolve
    conn = _FakeConn()
    jetzt = time.time()

    # 45 Minuten entfernt: trifft keines der 60/30/15/10/5/3-Fenster
    ausserhalb = {"id": 1, "task": "restart_server", "channel_id": 123,
                  "next_execution": jetzt + 45 * 60}
    asyncio.run(b._restart_task_countdown(conn, ausserhalb, jetzt))
    assert calls == []

    # 5 Minuten entfernt: muss genau einmal feuern, auch bei zweitem Aufruf
    innerhalb = {"id": 2, "task": "restart_server", "channel_id": 123,
                 "next_execution": jetzt + 5 * 60 - 5}
    asyncio.run(b._restart_task_countdown(conn, innerhalb, jetzt))
    asyncio.run(b._restart_task_countdown(conn, innerhalb, jetzt))
    assert calls == ["🔄 Noch 5 Minuten bis zum nächsten Neustart!"]


def test_restart_task_countdown_ohne_channel_bleibt_stumm():
    b = bot.DayZBot()
    aufgerufen = []

    async def fake_resolve(ch_id):  # noqa: ARG001
        aufgerufen.append(ch_id)
        return _FakeChannel([])

    b._resolve_channel = fake_resolve
    conn = _FakeConn()
    jetzt = time.time()

    kein_channel = {"id": 3, "task": "restart_server", "next_execution": jetzt + 5 * 60 - 5}
    asyncio.run(b._restart_task_countdown(conn, kein_channel, jetzt))
    assert aufgerufen == []

    ignoriert = {"id": 4, "task": "restart_server", "channel_id": 123, "channel_ignore": True,
                "next_execution": jetzt + 5 * 60 - 5}
    asyncio.run(b._restart_task_countdown(conn, ignoriert, jetzt))
    assert aufgerufen == []
