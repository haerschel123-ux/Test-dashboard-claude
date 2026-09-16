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


class _StubNitradoAPI:
    def __init__(self, inhalt=b"ADM-Dateiinhalt-per-API" * 20):
        self.inhalt = inhalt
        self.angefragte_pfade = []

    async def download_file(self, pfad):
        self.angefragte_pfade.append(pfad)
        return self.inhalt


class _VergiftetesFTP:
    """Wuerde bei einem Aufruf beweisen, dass faelschlich der FTP-Pfad
    genommen wurde, obwohl die Verbindung ueber die Nitrado-API liest."""

    def read_file_bytes(self, pfad):
        raise AssertionError("FTP haette hier NICHT aufgerufen werden duerfen (Server liest via API)")


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


@pytest.fixture
def conn_mit_feeds_via_api(monkeypatch):
    """Wie conn_mit_feeds, aber fuer einen Server, der Logs ueber die
    Nitrado-API liest statt per FTP - der Standardfall (log_lesen_via_api
    ist per Vorgabe True, sobald conn.api gesetzt ist)."""
    bot_mod.connections.upsert("logdl-service-api")
    bot_mod.connections.assign_guild("logdl-service-api", 333)
    conn = bot_mod.connections.for_service("logdl-service-api")
    conn.api = _StubNitradoAPI()
    conn.ftp = _VergiftetesFTP()

    kanal_adm = _StubChannel(9101)
    kanaele = {9101: kanal_adm}

    async def fake_resolve_channel(ch_id):
        return kanaele.get(int(ch_id))
    monkeypatch.setattr(bot_mod.bot, "_resolve_channel", fake_resolve_channel)

    bot_mod.cfg.set_channel(333, "adm_download", 9101, service_id="logdl-service-api")
    return conn, kanal_adm


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


# ── Regression: Server, die ueber die Nitrado-API statt per FTP lesen ─────
# (log_lesen_via_api ist per Vorgabe True, sobald conn.api gesetzt ist -
# das betrifft neue Verbindungen standardmaessig). _post_log_download rief
# bisher IMMER conn.ftp.read_file_bytes auf, auch wenn der Server ueber die
# API liest - dort schlug das leise fehl ("Datei leer/nicht lesbar"), ohne
# Ausnahme und ohne dass irgendwo sichtbar wurde, warum. Betraf nur diese
# zwei Feeds, weil jeder andere Codepfad (_log_dateien, _log_lesen_ab_offset,
# _log_dateigroesse) schon immer zwischen FTP und API unterschied.
def test_post_log_download_liest_ueber_nitrado_api_wenn_konfiguriert(conn_mit_feeds_via_api):
    conn, kanal_adm = conn_mit_feeds_via_api
    _run(bot_mod.bot._post_log_download(
        conn, "adm_download",
        "/games/ni11769331_2/noftp/dayzps/config/DayZServer_alt.ADM", _StubLoop()))
    assert len(kanal_adm.gesendet) == 1
    assert conn.api.angefragte_pfade == [
        "/games/ni11769331_2/noftp/dayzps/config/DayZServer_alt.ADM"]
    assert conn.dispatch_verlauf[-1]["ergebnis"] == "gepostet"


def test_post_log_download_ueber_api_ueberlebt_leere_antwort(conn_mit_feeds_via_api):
    conn, kanal_adm = conn_mit_feeds_via_api
    conn.api.inhalt = None
    _run(bot_mod.bot._post_log_download(
        conn, "adm_download", "/games/x/noftp/config/DayZServer_leer.ADM", _StubLoop()))
    assert kanal_adm.gesendet == []
    assert conn.dispatch_verlauf[-1]["ergebnis"] == "Datei leer oder nicht lesbar (API)"


def test_post_log_download_respektiert_expliziten_ftp_vorzug(monkeypatch):
    """log_lesen_via_api kann pro Server auf False gesetzt werden (manueller
    Rueckfall auf FTP) - dann muss weiterhin FTP genutzt werden, obwohl
    conn.api gesetzt ist."""
    bot_mod.connections.upsert("logdl-service-ftp-vorzug")
    bot_mod.connections.assign_guild("logdl-service-ftp-vorzug", 444)
    conn = bot_mod.connections.for_service("logdl-service-ftp-vorzug")
    conn.data["log_lesen_via_api"] = False
    conn.api = _StubNitradoAPI()
    conn.ftp = _StubFTP()
    kanal = _StubChannel(9201)

    async def fake_resolve_channel(ch_id):
        return kanal if int(ch_id) == 9201 else None
    monkeypatch.setattr(bot_mod.bot, "_resolve_channel", fake_resolve_channel)
    bot_mod.cfg.set_channel(444, "adm_download", 9201, service_id="logdl-service-ftp-vorzug")

    _run(bot_mod.bot._post_log_download(
        conn, "adm_download", "/dayzps/config/DayZServer_alt.ADM", _StubLoop()))
    assert len(kanal.gesendet) == 1
    assert conn.ftp.gelesene_pfade == ["/dayzps/config/DayZServer_alt.ADM"]
    assert conn.api.angefragte_pfade == []
