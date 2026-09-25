"""Tests fuer die Spielzeit-Berechnung aus den ADM-Log-Zeitstempeln statt
der Wanduhr des Bots.

Brigarde meldete: ein Spieler (Brigarde_Erkelen) war laut ADM-Log klar
laenger online, das Dashboard zeigte aber "0h 0m" Spielzeit. Ursache
(verifiziert mit ihren echten hochgeladenen ADM-Dateien): EconomyDB.
close_session berechnete die Sitzungsdauer bisher aus
``time.time() - connect_ts`` - der WANDUHRZEIT des Bots zwischen den beiden
Verarbeitungszeitpunkten. Verarbeitet der Bot Connect und Disconnect im
selben Poll-Zyklus oder beim Aufholen eines Rueckstands nach Downtime,
liegen beide fuer die Wanduhr nur Millisekunden auseinander, obwohl im Log
echte Minuten oder Stunden dazwischen liegen.

Fix: die Dauer wird jetzt aus den ADM-Zeitstempeln (HH:MM:SS) von Connect
und Disconnect berechnet (_session_dauer_aus_log_zeitstempeln), mit der
Wanduhr nur noch als Rueckfall bei unlesbaren/fehlenden Zeitstempeln.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_spielzeit_log_zeitstempel.py -v
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")
log_parser = pytest.importorskip("log_parser", reason="log_parser.py-Abhaengigkeiten nicht installiert")


def _run(coro):
    return asyncio.run(coro)


# ── Reine Funktion _session_dauer_aus_log_zeitstempeln ───────────────────
def test_normale_sitzung_am_selben_tag():
    dauer = bot._session_dauer_aus_log_zeitstempeln("17:56:56", "17:58:02")
    assert dauer == 66.0


def test_mehrstuendige_sitzung_am_selben_tag():
    dauer = bot._session_dauer_aus_log_zeitstempeln("08:00:00", "14:30:00")
    assert dauer == 6.5 * 3600


def test_sitzung_ueber_mitternacht_hinweg():
    dauer = bot._session_dauer_aus_log_zeitstempeln("23:50:00", "00:10:00")
    assert dauer == 20 * 60


def test_unlesbarer_connect_zeitstempel_liefert_none():
    assert bot._session_dauer_aus_log_zeitstempeln(None, "17:58:02") is None
    assert bot._session_dauer_aus_log_zeitstempeln("", "17:58:02") is None
    assert bot._session_dauer_aus_log_zeitstempeln("kaputt", "17:58:02") is None


def test_unlesbarer_disconnect_zeitstempel_liefert_none():
    assert bot._session_dauer_aus_log_zeitstempeln("17:56:56", None) is None


# ── EconomyDB.open_session/close_session ─────────────────────────────────
@pytest.fixture
def edb(tmp_path):
    return bot.EconomyDB(str(tmp_path / "economy_test.db"))


def test_close_session_nutzt_log_zeitstempel_statt_wanduhr(edb):
    # Kern-Regressionstest: Connect UND Disconnect werden RUECKARTIG
    # (Wanduhr vergeht nur Millisekunden) verarbeitet - genau das Szenario,
    # das den gemeldeten Fehler ausgeloest hat.
    edb.open_session("1000", "SpielerA", None, connect_log_ts="17:56:56")
    dauer = edb.close_session("1000", "SpielerA", disconnect_log_ts="17:58:02")
    assert dauer == 66.0


def test_close_session_faellt_ohne_log_zeitstempel_auf_wanduhr_zurueck(edb):
    edb.open_session("1000", "SpielerA", None)  # kein connect_log_ts
    dauer = edb.close_session("1000", "SpielerA", disconnect_log_ts="17:58:02")
    assert 0.0 <= dauer < 1.0  # Wanduhr: kaum Zeit vergangen zwischen den Aufrufen


def test_close_session_ohne_offene_sitzung_liefert_null(edb):
    assert edb.close_session("1000", "Nichtvorhanden", disconnect_log_ts="17:58:02") == 0.0


# ── Ende-zu-Ende mit Brigardes echter ADM-Datei ──────────────────────────
_ECHTE_ADM_ZEILEN = """
******************************************************************************
AdminLog started on 2026-09-24 at 17:16:28
17:56:49 | Player "Brigarde_Erkelen" (id=pJ8wQQS1teCqTArq2aZoDMlVfy_LmtX05vC44dOIM6c=) is connecting
17:56:56 | Player "Brigarde_Erkelen" (id=pJ8wQQS1teCqTArq2aZoDMlVfy_LmtX05vC44dOIM6c= pos=<7800.9, 10098.7, 342.1>) is connected
17:58:00 | Player "Brigarde_Erkelen" (id=pJ8wQQS1teCqTArq2aZoDMlVfy_LmtX05vC44dOIM6c= pos=<7803.9, 10081.3, 340.2>) performed EmoteSitA
17:58:02 | Player "Brigarde_Erkelen" (id=pJ8wQQS1teCqTArq2aZoDMlVfy_LmtX05vC44dOIM6c= pos=<7804.0, 10081.3, 340.3>) has been disconnected
"""


class _FakeConn:
    def __init__(self, guild_ids=(999999,)):
        self.service_id = "spielzeit-check"
        self.guild_ids = list(guild_ids)
        self.parser = None
        self._werte = {"action_economy": {}, "kill_reward": 0}

    def get(self, key, default=None):
        return self._werte.get(key, default)


def test_echte_adm_datei_ergibt_korrekte_gesamtspielzeit(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    parser = log_parser.DayZLogParser()
    events = parser.parse_lines(_ECHTE_ADM_ZEILEN)
    conn = _FakeConn()
    for ev in events:
        if ev.get("player") == "Brigarde_Erkelen":
            # Direkt nacheinander verarbeitet, wie es bei einem Rueckstand
            # (Aufholen mehrerer Zeilen in einem Batch) tatsaechlich passiert -
            # die Wanduhr zwischen diesen beiden Aufrufen vergeht nur
            # Millisekunden, im Log liegen aber 66 echte Sekunden dazwischen.
            _run(bot.bot._process_event_rewards(ev, conn))
    rows, _ = edb.roster_list(999999, "spielzeit-check")
    zeile = [r for r in rows if r["ingame_name"] == "Brigarde_Erkelen"][0]
    assert zeile["total_playtime_seconds"] == 66


def test_gesamtspielzeit_kumuliert_ueber_mehrere_tage(edb):
    # Brigardes Vorgabe: "spiele ich heute 2 Stunden und morgen nochmal 2,
    # soll insgesamt 4 angezeigt werden" - roster_add_playtime (bereits
    # bestehende Kumulierlogik) deckt das ab, hier als Regressionstest fest-
    # gehalten, damit es nicht versehentlich durch den Zeitstempel-Fix bricht.
    edb.roster_upsert_login(999999, "spielzeit-check", "SpielerA")
    edb.roster_add_playtime(999999, "spielzeit-check", "SpielerA", 2 * 3600)
    edb.roster_add_playtime(999999, "spielzeit-check", "SpielerA", 2 * 3600)
    rows, _ = edb.roster_list(999999, "spielzeit-check")
    assert rows[0]["total_playtime_seconds"] == 4 * 3600
