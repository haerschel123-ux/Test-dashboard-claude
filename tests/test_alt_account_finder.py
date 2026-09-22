"""Tests fuer den Alt Account Finder (Feed-Modul unter Tools).

Schwerpunkte: die DB-Kernlogik alt_account_seen (erster Name kein Alarm,
bekannter Name kein Alarm, neuer Name bei bekannter Account-ID = Alarm,
Trennung nach service_id, "Unbekannt"/leere Werte werden verworfen), die
Aggregation alt_account_mehrfach fuer die Dashboard-Anzeige, und der
Anschluss an den echten Log-Dispatch (_dispatch) - nur im normalen
Event-Pfad (nebenwirkungen=True), nie im Diagnose-Modus.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_alt_account_finder.py -v
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


@pytest.fixture
def edb(tmp_path):
    return bot.EconomyDB(str(tmp_path / "economy_test.db"))


def _run(coro):
    return asyncio.run(coro)


# ── Kernlogik: alt_account_seen ──────────────────────────────────────────
def test_erster_gesehener_name_loest_keinen_alarm_aus(edb):
    ergebnis = edb.alt_account_seen("1000", "ACC123", "SpielerA")
    assert ergebnis is None


def test_bekannter_name_derselben_account_id_loest_keinen_alarm_aus(edb):
    edb.alt_account_seen("1000", "ACC123", "SpielerA")
    ergebnis = edb.alt_account_seen("1000", "ACC123", "SpielerA")
    assert ergebnis is None


def test_bekannter_name_ist_case_insensitiv(edb):
    edb.alt_account_seen("1000", "ACC123", "SpielerA")
    ergebnis = edb.alt_account_seen("1000", "ACC123", "spielera")
    assert ergebnis is None


def test_neuer_name_bekannter_account_id_loest_alarm_mit_altem_namen_aus(edb):
    edb.alt_account_seen("1000", "ACC123", "SpielerA")
    ergebnis = edb.alt_account_seen("1000", "ACC123", "SpielerB")
    assert ergebnis == "SpielerA"
    # Danach ist "SpielerB" selbst bekannt - ein wiederholter Connect mit
    # demselben (neuen) Namen darf keinen zweiten Alarm mehr ausloesen.
    assert edb.alt_account_seen("1000", "ACC123", "SpielerB") is None


def test_service_id_trennt_dieselbe_account_id_vollstaendig(edb):
    edb.alt_account_seen("1000", "ACC123", "SpielerA")
    # Dieselbe Account-ID auf einem ANDEREN Server: eigener, erster Name,
    # kein Alarm - die beiden Server duerfen sich nie vermischen.
    ergebnis = edb.alt_account_seen("2000", "ACC123", "SpielerB")
    assert ergebnis is None


@pytest.mark.parametrize("account_id,gamertag", [
    ("", "SpielerA"), ("Unbekannt", "SpielerA"), ("unbekannt", "SpielerA"),
    ("ACC123", ""), (None, "SpielerA"), ("ACC123", None),
])
def test_unbekannte_oder_leere_werte_werden_nie_gespeichert(edb, account_id, gamertag):
    assert edb.alt_account_seen("1000", account_id, gamertag) is None
    assert edb.alt_account_mehrfach("1000") == []


# ── Aggregation fuer die Dashboard-Anzeige ───────────────────────────────
def test_mehrfach_zeigt_nur_accounts_mit_mehr_als_einem_namen(edb):
    edb.alt_account_seen("1000", "ACC1", "Solo")
    edb.alt_account_seen("1000", "ACC2", "Erst")
    edb.alt_account_seen("1000", "ACC2", "Zweit")
    ergebnis = edb.alt_account_mehrfach("1000")
    assert len(ergebnis) == 1
    assert ergebnis[0]["account_id"] == "ACC2"
    assert set(ergebnis[0]["gamertags"]) == {"Erst", "Zweit"}
    assert ergebnis[0]["anzahl"] == 2


def test_mehrfach_ist_je_server_getrennt(edb):
    edb.alt_account_seen("1000", "ACC1", "Erst")
    edb.alt_account_seen("1000", "ACC1", "Zweit")
    assert edb.alt_account_mehrfach("2000") == []
    assert len(edb.alt_account_mehrfach("1000")) == 1


# ── Anschluss an den echten Log-Dispatch ─────────────────────────────────
class _FakeConnDispatch:
    def __init__(self, enabled=True):
        self.service_id = "1000"
        self.guild_id = 111
        self.guild_ids = [111]
        self.name = "Testserver"
        self.parser = None
        self.dispatch_verlauf = []
        self._werte = {"alt_account_enabled": enabled}

    def get(self, key, default=None):
        return self._werte.get(key, default)


@pytest.fixture
def gepatchter_dispatch(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    posts = []

    async def fake_post_feed(*a, **k):
        posts.append((a, k))
        return True, "sent"
    monkeypatch.setattr(bot, "_post_feed", fake_post_feed)
    monkeypatch.setattr(bot.cfg, "guilds", {})
    return posts


def _connect_event(player, player_id):
    return {"type": "connect", "timestamp": "00:00:00", "player": player,
           "player_id": player_id, "position": None, "raw": ""}


def test_dispatch_postet_keinen_alarm_beim_ersten_connect(gepatchter_dispatch):
    conn = _FakeConnDispatch()
    _run(bot.bot._dispatch(_connect_event("SpielerA", "ACC123"), conn))
    alt_account_posts = [p for p in gepatchter_dispatch if p[1].get("service_id") == "1000"
                         and p[0][1] == "alt_account"]
    assert alt_account_posts == []


def test_dispatch_postet_alarm_bei_neuem_gamertag_derselben_account_id(gepatchter_dispatch):
    conn = _FakeConnDispatch()
    _run(bot.bot._dispatch(_connect_event("SpielerA", "ACC123"), conn))
    _run(bot.bot._dispatch(_connect_event("SpielerB", "ACC123"), conn))
    alt_account_posts = [p for p in gepatchter_dispatch if p[0][1] == "alt_account"]
    assert len(alt_account_posts) == 1
    embed = alt_account_posts[0][0][2]
    assert "SpielerB" in embed.description
    assert "SpielerA" in embed.description


def test_dispatch_ohne_aktivierten_schalter_postet_nichts(gepatchter_dispatch):
    conn = _FakeConnDispatch(enabled=False)
    _run(bot.bot._dispatch(_connect_event("SpielerA", "ACC123"), conn))
    _run(bot.bot._dispatch(_connect_event("SpielerB", "ACC123"), conn))
    alt_account_posts = [p for p in gepatchter_dispatch if p[0][1] == "alt_account"]
    assert alt_account_posts == []
    # Ohne den Schalter darf auch keine Historie entstehen.
    assert bot.db.alt_account_mehrfach("1000") == []


def test_diagnose_modus_erzeugt_keine_historie_und_keinen_alarm(gepatchter_dispatch):
    conn = _FakeConnDispatch()
    _run(bot.bot._dispatch(_connect_event("SpielerA", "ACC123"), conn, nebenwirkungen=False))
    _run(bot.bot._dispatch(_connect_event("SpielerB", "ACC123"), conn, nebenwirkungen=False))
    assert gepatchter_dispatch == []
    assert bot.db.alt_account_mehrfach("1000") == []
