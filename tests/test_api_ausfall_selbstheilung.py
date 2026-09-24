"""Tests fuer die Selbstheilung bei dauerhaftem Nitrado-API-Ausfall.

Ausgangsbefund (live mit Brigardes Nitrado-Token verifiziert): ein Server,
dessen Nitrado-Unteraccount sich geaendert hat (z.B. nach Neuanlage des
Servers), bekam dauerhaft HTTP 500/429 auf list_files/seek_file, weil der
gespeicherte "ftp_user" veraltet war. _check_ftp_health beobachtete bisher
NUR FTPManager.consecutive_failures - bei einem Server, der Logs ueber die
API statt FTP liest (_log_lesen_via_api), blieb dieser Zaehler ewig bei 0,
die Selbstheilung (frische Zugangsdaten ueber den Token holen) griff nie.

Deckt: NitradoAPI.list_files/seek_file zaehlen Fehlschlaege und setzen sie
bei Erfolg zurueck; _check_ftp_health beobachtet jetzt BEIDE Zaehler
(FTP und API) und stoesst die Selbstheilung auch bei einem reinen
API-Ausfall an.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_api_ausfall_selbstheilung.py -v
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


# ── NitradoAPI: Fehlschlaege zaehlen/zuruecksetzen ───────────────────────
class _FakeResp:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload or {}

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, antworten):
        self._antworten = list(antworten)  # Liste von _FakeResp, der Reihe nach

    def get(self, url, params=None):
        return self._antworten.pop(0)


def _api_mit_antworten(monkeypatch, *antworten):
    api = bot.NitradoAPI(token="fake", service_id="123")
    session = _FakeSession(antworten)

    async def fake_s():
        return session
    monkeypatch.setattr(api, "_s", fake_s)
    api._session_stub = session  # fuer Tests, die die Antwortliste nachtraeglich aendern
    return api


def test_list_files_zaehlt_fehlschlag_bei_http_500(monkeypatch):
    api = _api_mit_antworten(monkeypatch, _FakeResp(500))
    ergebnis = _run(api.list_files("/dayzps/config"))
    assert ergebnis is None
    assert api.consecutive_failures == 1
    assert "500" in api.last_error


def test_list_files_setzt_bei_erfolg_zurueck(monkeypatch):
    api = _api_mit_antworten(monkeypatch, _FakeResp(500), _FakeResp(200, {"data": {"entries": []}}))
    _run(api.list_files("/dayzps/config"))
    assert api.consecutive_failures == 1
    api._session_stub._antworten = [_FakeResp(200, {"data": {"entries": [{"name": "x.ADM"}]}})]
    ergebnis = _run(api.list_files("/dayzps/config"))
    assert ergebnis == [{"name": "x.ADM"}]
    assert api.consecutive_failures == 0
    assert api.last_error == ""


def test_seek_file_zaehlt_fehlschlag_bei_http_429(monkeypatch):
    api = _api_mit_antworten(monkeypatch, _FakeResp(429))
    ergebnis = _run(api.seek_file("/dayzps/config/x.ADM", 0))
    assert ergebnis is None
    assert api.consecutive_failures == 1
    assert "429" in api.last_error


def test_seek_file_setzt_bei_erfolg_zurueck(monkeypatch):
    api = bot.NitradoAPI(token="fake", service_id="123")
    api.consecutive_failures = 3
    session = _FakeSession([
        _FakeResp(200, {"data": {"token": {"url": "https://x/y"}}}),
    ])
    # Der zweite GET (Abruf-URL) liefert die eigentlichen Bytes.
    orig_pop = session._antworten.pop

    class _Abruf(_FakeResp):
        async def read(self):
            return b"inhalt"
    session._antworten = [
        _FakeResp(200, {"data": {"token": {"url": "https://x/y"}}}),
        _Abruf(200),
    ]

    async def fake_s():
        return session
    monkeypatch.setattr(api, "_s", fake_s)
    ergebnis = _run(api.seek_file("/dayzps/config/x.ADM", 0))
    assert ergebnis == b"inhalt"
    assert api.consecutive_failures == 0


# ── _check_ftp_health beobachtet auch conn.api ───────────────────────────
class _StubFTPManager:
    def __init__(self):
        self.consecutive_failures = 0
        self.last_error = ""


class _StubNitradoAPIFuerHealth:
    def __init__(self, consecutive_failures=0, last_error=""):
        self.consecutive_failures = consecutive_failures
        self.last_error = last_error

    async def get_info(self):
        return None  # _try_refresh_ftp_credentials bricht dann sauber ab


_zaehler = [0]


@pytest.fixture
def conn():
    _zaehler[0] += 1
    service_id = f"api-health-{_zaehler[0]}"
    bot.connections.upsert(service_id)
    bot.connections.add_guild(service_id, 7000 + _zaehler[0])
    c = bot.connections.for_service(service_id)
    c.ftp = _StubFTPManager()
    c.api = None
    return c


def test_reiner_api_ausfall_loest_selbstheilungsversuch_aus(monkeypatch, conn):
    conn.api = _StubNitradoAPIFuerHealth(consecutive_failures=10, last_error="HTTP 500 bei list_files(...)")
    versucht = []

    async def fake_refresh(c):
        versucht.append(c)
        return False
    monkeypatch.setattr(bot.bot, "_try_refresh_ftp_credentials", fake_refresh)
    _run(bot.bot._check_ftp_health(conn))
    assert versucht == [conn]
    assert conn.ftp_warn_active


def test_reiner_ftp_ausfall_loest_weiterhin_aus(monkeypatch, conn):
    conn.ftp.consecutive_failures = 10
    versucht = []

    async def fake_refresh(c):
        versucht.append(c)
        return False
    monkeypatch.setattr(bot.bot, "_try_refresh_ftp_credentials", fake_refresh)
    _run(bot.bot._check_ftp_health(conn))
    assert versucht == [conn]


def test_kein_ausfall_loest_nichts_aus(monkeypatch, conn):
    conn.api = _StubNitradoAPIFuerHealth(consecutive_failures=0)
    versucht = []

    async def fake_refresh(c):
        versucht.append(c)
        return False
    monkeypatch.setattr(bot.bot, "_try_refresh_ftp_credentials", fake_refresh)
    _run(bot.bot._check_ftp_health(conn))
    assert versucht == []
