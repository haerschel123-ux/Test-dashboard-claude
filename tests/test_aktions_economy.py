"""Tests fuer die Aktions-Economy (_AKTIONS_ECONOMY_SCHLUESSEL,
_aktion_auszahlen) und die neue Spielzeit-Verguetung pro Stunde
(_spielzeit_auszahlen), beide eingehaengt in _process_event_rewards.

Deckt: Auszahlung nur bei verlinktem Spieler, Betrag 0 tut nichts, negative
Betraege ziehen ab (add_wallet deckelt selbst bei 0), kill_pvp zahlt weiter
nur ueber den bestehenden kill_reward-Pfad (kein Doppel-Hook), und die
Spielzeit-Schwelle (< 1 ganze Einheit wird nicht ausgezahlt, kein Uebertrag
zwischen Sitzungen).

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_aktions_economy.py -v
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


class _FakeConn:
    def __init__(self, guild_ids=(111,), action_economy=None, kill_reward=0):
        self.service_id = "1000"
        self.guild_ids = list(guild_ids)
        self._werte = {"action_economy": action_economy or {}, "kill_reward": kill_reward}

    def get(self, key, default=None):
        return self._werte.get(key, default)


def _verlinke(edb, guild_id, user_id, name):
    edb.roster_upsert_login(guild_id, "1000", name)
    edb.link_user(guild_id, user_id, name)


# ── Einzelne Aktionen ─────────────────────────────────────────────────────
def test_sturz_tod_zieht_ab(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    _verlinke(edb, 111, 42, "Opfer")
    edb.add_wallet(111, 42, 100)
    conn = _FakeConn(action_economy={"fall_death": -50})
    _run(bot.bot._process_event_rewards(
        {"type": "kill_env", "player": "Opfer", "cause": "fall damage"}, conn))
    wallet, _bank = edb.get_balance(111, 42)
    assert wallet == 50


def test_connect_schreibt_gut(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    _verlinke(edb, 111, 42, "SpielerA")
    conn = _FakeConn(action_economy={"connect": 25})
    _run(bot.bot._process_event_rewards({"type": "connect", "player": "SpielerA", "player_id": "ACC1"}, conn))
    wallet, _bank = edb.get_balance(111, 42)
    assert wallet == 25


def test_bauen_schreibt_gut(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    _verlinke(edb, 111, 42, "Bauer")
    conn = _FakeConn(action_economy={"build": 10})
    _run(bot.bot._process_event_rewards(
        {"type": "basebuild", "player": "Bauer", "aktion": "built", "item": "wall"}, conn))
    wallet, _bank = edb.get_balance(111, 42)
    assert wallet == 10


def test_flagge_hissen_schreibt_gut(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    _verlinke(edb, 111, 42, "Bauer")
    conn = _FakeConn(action_economy={"flag_raise": 15})
    _run(bot.bot._process_event_rewards(
        {"type": "basebuild", "player": "Bauer", "aktion": "raised", "item": "flag"}, conn))
    wallet, _bank = edb.get_balance(111, 42)
    assert wallet == 15


def test_bewusstlosigkeit_zieht_ab(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    _verlinke(edb, 111, 42, "SpielerA")
    edb.add_wallet(111, 42, 100)
    conn = _FakeConn(action_economy={"unconscious": -20})
    _run(bot.bot._process_event_rewards({"type": "unconscious", "player": "SpielerA"}, conn))
    wallet, _bank = edb.get_balance(111, 42)
    assert wallet == 80


def test_treffer_zieht_ab_beim_opfer(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    _verlinke(edb, 111, 42, "Opfer")
    edb.add_wallet(111, 42, 100)
    conn = _FakeConn(action_economy={"player_hit": -5})
    _run(bot.bot._process_event_rewards(
        {"type": "damage", "victim": "Opfer", "attacker": "Angreifer", "weapon": "AKM", "ammo": None}, conn))
    wallet, _bank = edb.get_balance(111, 42)
    assert wallet == 95


def test_emote_schreibt_gut(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    _verlinke(edb, 111, 42, "SpielerA")
    conn = _FakeConn(action_economy={"emote": 3})
    _run(bot.bot._process_event_rewards({"type": "emote", "player": "SpielerA"}, conn))
    wallet, _bank = edb.get_balance(111, 42)
    assert wallet == 3


def test_betrag_null_tut_nichts(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    _verlinke(edb, 111, 42, "SpielerA")
    conn = _FakeConn(action_economy={"connect": 0})
    _run(bot.bot._process_event_rewards({"type": "connect", "player": "SpielerA", "player_id": "ACC1"}, conn))
    wallet, _bank = edb.get_balance(111, 42)
    assert wallet == 0


def test_unverlinkter_spieler_bekommt_nichts(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    edb.roster_upsert_login(111, "1000", "SpielerA")
    conn = _FakeConn(action_economy={"connect": 50})
    _run(bot.bot._process_event_rewards({"type": "connect", "player": "SpielerA", "player_id": "ACC1"}, conn))
    assert edb.get_links_by_user(111, 42) == []  # niemand verlinkt, kein Wallet-Ziel


def test_kill_pvp_zahlt_nur_ueber_kill_reward_kein_doppel_hook(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    _verlinke(edb, 111, 42, "Killer")
    _verlinke(edb, 111, 43, "Opfer")
    # Auch wenn "kill" als Aktions-Schluessel existieren wuerde, gibt es ihn
    # bewusst nicht in _AKTIONS_ECONOMY_SCHLUESSEL - kill_reward ist der
    # einzige Pfad fuer PvP-Kills.
    conn = _FakeConn(kill_reward=100)
    _run(bot.bot._process_event_rewards(
        {"type": "kill_pvp", "killer": "Killer", "victim": "Opfer",
         "killer_id": "K1", "victim_id": "V1", "weapon": "AKM", "distance": 50}, conn))
    wallet, _bank = edb.get_balance(111, 42)
    assert wallet == 100


# ── Spielzeit pro Stunde ──────────────────────────────────────────────────
def test_spielzeit_ueber_schwelle_wird_ausgezahlt(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    monkeypatch.setattr(edb, "close_session", lambda *a, **k: 1800.0)
    _verlinke(edb, 111, 42, "SpielerA")
    conn = _FakeConn(action_economy={"playtime_per_hour": 10})
    _run(bot.bot._process_event_rewards({"type": "disconnect", "player": "SpielerA"}, conn))
    wallet, _bank = edb.get_balance(111, 42)
    assert wallet == 5  # 1800s * 10/3600 = 5


def test_spielzeit_unter_schwelle_wird_nicht_ausgezahlt(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    monkeypatch.setattr(edb, "close_session", lambda *a, **k: 30.0)
    _verlinke(edb, 111, 42, "SpielerA")
    conn = _FakeConn(action_economy={"playtime_per_hour": 10})
    _run(bot.bot._process_event_rewards({"type": "disconnect", "player": "SpielerA"}, conn))
    wallet, _bank = edb.get_balance(111, 42)
    assert wallet == 0  # 30s * 10/3600 = 0.08 -> floor 0, kein Uebertrag


def test_spielzeit_kein_uebertrag_zwischen_sitzungen(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    monkeypatch.setattr(edb, "close_session", lambda *a, **k: 30.0)
    _verlinke(edb, 111, 42, "SpielerA")
    conn = _FakeConn(action_economy={"playtime_per_hour": 10})
    for _ in range(2):
        _run(bot.bot._process_event_rewards({"type": "disconnect", "player": "SpielerA"}, conn))
    wallet, _bank = edb.get_balance(111, 42)
    assert wallet == 0  # zwei kurze Sitzungen unter der Schwelle ergeben zusammen trotzdem 0
