"""Tests fuer die ADM-/RPT-Log-Download-Feeds, die Brigarde als "postet seit
Wochen nichts mehr" gemeldet hat.

Zweck dieser Datei ist NICHT primaer, einen bereits gefundenen Bug zu belegen
(eine unabhaengige Code-Pruefung fand keinen), sondern echt zu beweisen, dass
_post_log_download bei einer korrekt eingerichteten Feed-Konfiguration
tatsaechlich sendet - inklusive Anhang. Damit laesst sich der Verdacht "Code
ist kaputt" von "liegt an Kunden-/Discord-Konfiguration" sauber trennen.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_log_download_feed.py -v
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot_mod = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _run(coro):
    return asyncio.run(coro)


class _StubFTP:
    def __init__(self, inhalt=b"ADM-Dateiinhalt" * 20):
        self.inhalt = inhalt
        self.gelesene_pfade = []

    def read_file_bytes(self, pfad):
        self.gelesene_pfade.append(pfad)
        return self.inhalt


class _StubChannel:
    def __init__(self, id_):
        self.id = id_
        self.gesendet = []

    async def send(self, content=None, embed=None, file=None, view=None):
        self.gesendet.append({"content": content, "embed": embed, "file": file})


class _StubLoop:
    async def run_in_executor(self, _executor, func, *args):
        return func(*args)


@pytest.fixture
def conn_mit_feeds(monkeypatch):
    bot_mod.connections.upsert("logdl-service")
    bot_mod.connections.assign_guild("logdl-service", 111)
    conn = bot_mod.connections.for_service("logdl-service")
    conn.ftp = _StubFTP()

    kanal_adm = _StubChannel(9001)
    kanal_rpt = _StubChannel(9002)
    kanaele = {9001: kanal_adm, 9002: kanal_rpt}

    async def fake_resolve_channel(ch_id):
        return kanaele.get(int(ch_id))
    monkeypatch.setattr(bot_mod.bot, "_resolve_channel", fake_resolve_channel)

    bot_mod.cfg.set_channel(111, "adm_download", 9001, service_id="logdl-service")
    bot_mod.cfg.set_channel(111, "rpt_download", 9002, service_id="logdl-service")
    return conn, kanal_adm, kanal_rpt


def test_post_log_download_sendet_adm_datei_mit_anhang(conn_mit_feeds):
    conn, kanal_adm, _ = conn_mit_feeds
    _run(bot_mod.bot._post_log_download(
        conn, "adm_download", "/games/x/config/DayZServer_alt.ADM", _StubLoop()))
    assert len(kanal_adm.gesendet) == 1
    eintrag = kanal_adm.gesendet[0]
    assert eintrag["file"] is not None
    assert "DayZServer_alt.ADM" in eintrag["embed"].description
    # Diagnose-Seite ("Letzte Ereignisse") muss den Erfolg sehen koennen.
    letzter = conn.dispatch_verlauf[-1]
    assert letzter["typ"] == "adm_download"
    assert letzter["ergebnis"] == "gepostet"


def test_post_log_download_sendet_rpt_datei_mit_anhang(conn_mit_feeds):
    conn, _, kanal_rpt = conn_mit_feeds
    _run(bot_mod.bot._post_log_download(
        conn, "rpt_download", "/games/x/config/DayZServer_alt.RPT", _StubLoop()))
    assert len(kanal_rpt.gesendet) == 1
    assert "DayZServer_alt.RPT" in kanal_rpt.gesendet[0]["embed"].description


def test_post_log_download_postet_nichts_ohne_konfigurierten_channel(monkeypatch):
    bot_mod.connections.upsert("logdl-service-2")
    bot_mod.connections.assign_guild("logdl-service-2", 222)
    conn = bot_mod.connections.for_service("logdl-service-2")
    conn.ftp = _StubFTP()
    aufrufe = []

    async def fake_resolve_channel(ch_id):
        aufrufe.append(ch_id)
        return None
    monkeypatch.setattr(bot_mod.bot, "_resolve_channel", fake_resolve_channel)

    _run(bot_mod.bot._post_log_download(
        conn, "adm_download", "/games/x/config/DayZServer_alt.ADM", _StubLoop()))
    assert aufrufe == []  # kein Channel konfiguriert -> gar nicht erst versucht
    assert conn.dispatch_verlauf[-1]["ergebnis"] == "kein Feed/Channel gesetzt"


def test_post_log_download_ueberlebt_leere_datei(conn_mit_feeds):
    conn, kanal_adm, _ = conn_mit_feeds
    conn.ftp.inhalt = b""
    _run(bot_mod.bot._post_log_download(
        conn, "adm_download", "/games/x/config/DayZServer_leer.ADM", _StubLoop()))
    assert kanal_adm.gesendet == []
    assert conn.dispatch_verlauf[-1]["ergebnis"] == "Datei leer oder nicht lesbar (FTP)"


def test_post_log_download_ueberlebt_ftp_exception(conn_mit_feeds):
    conn, kanal_adm, _ = conn_mit_feeds

    def kaputt(pfad):
        raise OSError("FTP weg")
    conn.ftp.read_file_bytes = kaputt
    # Darf NICHT werfen - der Poll-Zyklus soll trotz FTP-Fehler weiterlaufen.
    _run(bot_mod.bot._post_log_download(
        conn, "adm_download", "/games/x/config/DayZServer_x.ADM", _StubLoop()))
    assert kanal_adm.gesendet == []
    assert "Ausnahme" in conn.dispatch_verlauf[-1]["ergebnis"]


def test_post_log_download_disabled_feed_postet_nicht(conn_mit_feeds):
    conn, kanal_adm, _ = conn_mit_feeds
    # Wie im Dashboard ueber den Ein/Aus-Schalter pausiert.
    eintrag = bot_mod.cfg.feed_settings(111, "adm_download", "logdl-service")
    eintrag["enabled"] = False
    bot_mod.cfg.server_feeds(111, "logdl-service", anlegen=True)["adm_download"] = eintrag
    _run(bot_mod.bot._post_log_download(
        conn, "adm_download", "/games/x/config/DayZServer_alt.ADM", _StubLoop()))
    assert kanal_adm.gesendet == []
