"""Sanfte Drosselung der Nitrado-Log-Abfragen nach HTTP 429/5xx.

    python3 -m pytest tests/test_nitrado_drossel.py -q
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot


class _Antwort:
    def __init__(self, status, daten=None, header=None, inhalt=b""):
        self.status, self._daten, self.headers, self._inhalt = status, daten or {}, header or {}, inhalt

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self._daten

    async def read(self):
        return self._inhalt


class _Session:
    def __init__(self, antworten):
        self.antworten, self.aufrufe = list(antworten), []

    def get(self, url, **kw):
        self.aufrufe.append(url)
        return self.antworten.pop(0)


def _liste(status=200, header=None):
    return _Antwort(status, {"data": {"entries": [{"name": "a.ADM", "path": "/p/a.ADM"}]}}, header)


@pytest.fixture
def api(monkeypatch):
    bot._NITRADO_PAUSE_BIS.clear(); bot._NITRADO_FEHLSERIE.clear()
    schlaf = []

    async def fake_sleep(sek):
        schlaf.append(round(sek, 1))
    monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)
    # monotonic einfrieren: Pausen laufen im Test nicht von selbst ab
    monkeypatch.setattr(bot.time, "monotonic", lambda: 1000.0)

    def neu(antworten, token="tokA", service="1"):
        a = bot.NitradoAPI(token, service)
        a._session = _Session(antworten)
        a._s = lambda: _async_value(a._session)
        return a
    neu.schlaf = schlaf
    return neu


async def _async_value(v):
    return v


def _run(c):
    return asyncio.run(c)


def test_5xx_loest_kurze_pause_aus_und_wird_nicht_uebersprungen(api):
    a = api([_liste(500), _liste(500), _liste(200)])
    assert _run(a.list_files("/x")) is None            # 1. Fehler: noch keine Wartezeit
    assert api.schlaf == []
    assert _run(a.list_files("/x")) is None            # wartet 1 s, fragt trotzdem an
    assert api.schlaf == [1.0]
    assert len(a._session.aufrufe) == 2
    assert _run(a.list_files("/x"))                    # wartet 2 s (2. Fehler in Folge), dann Erfolg
    assert api.schlaf == [1.0, 2.0] and len(a._session.aufrufe) == 3
    assert bot._NITRADO_PAUSE_BIS == {} and bot._NITRADO_FEHLSERIE == {}   # Erfolg hebt alles auf
    assert a.consecutive_failures == 0


def test_nach_erfolg_keine_wartezeit_mehr(api):
    a = api([_liste(500), _liste(200), _liste(200)])
    _run(a.list_files("/x")); _run(a.list_files("/x")); _run(a.list_files("/x"))
    assert api.schlaf == [1.0]   # nur der Aufruf direkt nach dem Fehler wartete


def test_429_mit_und_ohne_retry_after_und_obergrenze(api):
    a = api([_liste(429, {"Retry-After": "2"})]); _run(a.list_files("/x"))
    assert bot._NITRADO_PAUSE_BIS["tokA"] == 1002.0
    a = api([_liste(429, {"Retry-After": "120"})]); _run(a.list_files("/x"))
    assert bot._NITRADO_PAUSE_BIS["tokA"] == 1005.0    # hart auf 5 s begrenzt
    bot._NITRADO_PAUSE_BIS.clear(); bot._NITRADO_FEHLSERIE.clear()
    a = api([_liste(429)]); _run(a.list_files("/x"))
    assert bot._NITRADO_PAUSE_BIS["tokA"] == 1003.0    # Standard 3 s
    a = api([_liste(429, {"Retry-After": "abc"})]); _run(a.list_files("/x"))
    assert bot._NITRADO_PAUSE_BIS["tokA"] == 1003.0    # ungueltiger Header → Standard


def test_pause_gilt_je_token_nicht_je_server(api):
    a1 = api([_liste(429)], token="tokA", service="1"); _run(a1.list_files("/x"))
    a2 = api([_liste(200)], token="tokA", service="3"); _run(a2.list_files("/x"))
    assert api.schlaf == [3.0]                          # anderer Server, gleiches Konto: wartet
    api.schlaf.clear()
    b = api([_liste(429)], token="tokA", service="1"); _run(b.list_files("/x"))
    api.schlaf.clear()
    c = api([_liste(200)], token="tokB", service="9"); _run(c.list_files("/x"))
    assert api.schlaf == []                             # fremdes Konto: unberuehrt


def test_andere_fehler_und_404_loesen_keine_pause_aus(api):
    for status in (200, 400, 403, 404):
        a = api([_Antwort(status, {})]); _run(a.list_files("/x"))
    assert bot._NITRADO_PAUSE_BIS == {} and api.schlaf == []


def test_alte_fehler_zaehlen_nicht_ewig(api, monkeypatch):
    a = api([_liste(500), _liste(500)])
    _run(a.list_files("/x"))
    jetzt = [1000.0 + bot._NITRADO_SERIE_FENSTER + 5]
    monkeypatch.setattr(bot.time, "monotonic", lambda: jetzt[0])
    _run(a.list_files("/x"))
    assert bot._NITRADO_FEHLSERIE["tokA"][0] == 1      # Serie begann neu → wieder 1 s statt 2 s
    assert bot._NITRADO_PAUSE_BIS["tokA"] == jetzt[0] + 1.0


def test_seek_file_drosselt_und_erfolg_hebt_auf(api):
    a = api([_Antwort(503), _Antwort(200, {"data": {"token": {"url": "https://x/y"}}}), _Antwort(200, inhalt=b"abc")])
    assert _run(a.seek_file("/p", 0)) is None
    assert bot._NITRADO_PAUSE_BIS["tokA"] == 1001.0
    assert _run(a.seek_file("/p", 0)) == b"abc"
    assert api.schlaf == [1.0] and bot._NITRADO_PAUSE_BIS == {}


def test_neustart_und_stopp_werden_nie_gedrosselt(api):
    a = api([_liste(500)]); _run(a.list_files("/x"))

    class _Post:
        def post(self, url, **kw):
            return _Antwort(200, {"message": "ok"})
    a._session = _Post()
    api.schlaf.clear()
    assert _run(a.restart()) == (True, "ok") and api.schlaf == []
