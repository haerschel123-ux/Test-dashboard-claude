"""Tests fuer die neue Spieler-Seite (player_roster) und die verschaerfte
/link-Pruefung.

Deckt: automatisches Anlegen/Aktualisieren eines Roster-Eintrags bei jedem
Connect, Kumulieren der Spielzeit bei Disconnect, Loeschen inkl. Entlinken,
erneuter Connect nach dem Loeschen legt den Namen unverlinkt neu an, und
den Anschluss an den echten Event-Dispatch (_process_event_rewards).

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_spieler_seite.py -v
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


# ── Reine DB-Ebene (player_roster) ───────────────────────────────────────
def test_erster_login_setzt_first_seen_und_last_login(edb):
    edb.roster_upsert_login(111, "1000", "SpielerA")
    rows, gesamt = edb.roster_list(111, "1000")
    assert gesamt == 1
    assert rows[0]["ingame_name"] == "SpielerA"
    assert rows[0]["first_seen"] is not None
    assert rows[0]["last_login"] is not None


def test_zweiter_login_aendert_nur_last_login_nicht_first_seen(edb):
    edb.roster_upsert_login(111, "1000", "SpielerA")
    erster = edb.roster_list(111, "1000")[0][0]["first_seen"]
    edb.roster_upsert_login(111, "1000", "SpielerA")
    zweiter = edb.roster_list(111, "1000")[0][0]
    assert zweiter["first_seen"] == erster


def test_roster_add_playtime_kumuliert(edb):
    edb.roster_upsert_login(111, "1000", "SpielerA")
    edb.roster_add_playtime(111, "1000", "SpielerA", 100)
    edb.roster_add_playtime(111, "1000", "SpielerA", 50)
    rows, _ = edb.roster_list(111, "1000")
    assert rows[0]["total_playtime_seconds"] == 150


def test_roster_hat_namen_case_insensitiv(edb):
    edb.roster_upsert_login(111, "1000", "SpielerA")
    assert edb.roster_hat_namen(111, "1000", "spielera")
    assert not edb.roster_hat_namen(111, "1000", "SpielerB")


def test_roster_getrennt_nach_guild_und_server(edb):
    edb.roster_upsert_login(111, "1000", "SpielerA")
    assert not edb.roster_hat_namen(222, "1000", "SpielerA")
    assert not edb.roster_hat_namen(111, "2000", "SpielerA")


def test_roster_delete_entfernt_zeile(edb):
    edb.roster_upsert_login(111, "1000", "SpielerA")
    assert edb.roster_delete(111, "1000", "SpielerA")
    assert not edb.roster_hat_namen(111, "1000", "SpielerA")


def test_roster_delete_entlinkt_falls_verlinkt(edb):
    edb.roster_upsert_login(111, "1000", "SpielerA")
    edb.link_user(111, 999, "SpielerA")
    assert edb.get_links_by_user(111, 999) != []
    edb.roster_delete(111, "1000", "SpielerA")
    assert edb.get_links_by_user(111, 999) == []


def test_roster_delete_unbekannter_name_liefert_false(edb):
    assert not edb.roster_delete(111, "1000", "Nichtvorhanden")


def test_erneuter_connect_nach_loeschen_legt_unverlinkt_neu_an(edb):
    edb.roster_upsert_login(111, "1000", "SpielerA")
    edb.link_user(111, 999, "SpielerA")
    edb.roster_delete(111, "1000", "SpielerA")
    edb.roster_upsert_login(111, "1000", "SpielerA")
    rows, _ = edb.roster_list(111, "1000")
    assert rows[0]["user_id"] is None


def test_roster_list_suche_filtert(edb):
    edb.roster_upsert_login(111, "1000", "SpielerA")
    edb.roster_upsert_login(111, "1000", "AndererName")
    rows, gesamt = edb.roster_list(111, "1000", suche="Spieler")
    assert gesamt == 1
    assert rows[0]["ingame_name"] == "SpielerA"


def test_roster_list_online_status(edb):
    edb.roster_upsert_login(111, "1000", "SpielerA")
    edb.open_session("1000", "SpielerA", None)
    rows, _ = edb.roster_list(111, "1000")
    assert rows[0]["online"]


# ── Anschluss an den echten Event-Dispatch ───────────────────────────────
class _FakeConn:
    def __init__(self, guild_ids=(111,), action_economy=None, kill_reward=0):
        self.service_id = "1000"
        self.guild_ids = list(guild_ids)
        self._werte = {"action_economy": action_economy or {}, "kill_reward": kill_reward}

    def get(self, key, default=None):
        return self._werte.get(key, default)


def _connect_event(player, player_id="ACC1"):
    return {"type": "connect", "player": player, "player_id": player_id}


def _disconnect_event(player):
    return {"type": "disconnect", "player": player}


def test_connect_event_traegt_spieler_in_roster_ein(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    conn = _FakeConn()
    _run(bot.bot._process_event_rewards(_connect_event("SpielerA"), conn))
    assert edb.roster_hat_namen(111, "1000", "SpielerA")


def test_link_lehnt_unbekannten_namen_ab(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    assert not edb.roster_hat_namen(111, "1000", "Unbekannt")


def test_link_erlaubt_bekannten_namen(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    conn = _FakeConn()
    _run(bot.bot._process_event_rewards(_connect_event("SpielerA"), conn))
    assert edb.roster_hat_namen(111, "1000", "SpielerA")
    ok, _why = edb.link_user(111, 42, "SpielerA")
    assert ok
