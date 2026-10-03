"""/stats: eigene Statistik ohne Angabe, username ohne Verknuepfung, user nur
mit /link, beides zusammen = Fehler; erweiterte Kennzahlen (Zeitraeume,
Streaks, zuletzt getoetet, Spielzeit).

    python3 -m pytest tests/test_stats.py -q
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


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, embed=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class _User:
    def __init__(self, id_):
        self.id = id_
        self.mention = f"<@{id_}>"


class _Inter:
    def __init__(self, user_id, guild_id=111):
        self.user = _User(user_id)
        self.guild_id = guild_id
        self.locale = None
        self.response = _Resp()


class _Conn:
    service_id = "1000"
    name = "Testserver"

    def get(self, key, default=None):
        return {"long_range_kill_meter": 300}.get(key, default)


@pytest.fixture
def edb(tmp_path, monkeypatch):
    db = bot.EconomyDB(str(tmp_path / "stats.db"))
    monkeypatch.setattr(bot, "db", db)
    monkeypatch.setattr(bot, "_conn_of", lambda i: _Conn())
    return db


def _kill(db, killer, victim, weapon="M4A1", dist=50, alter=0):
    db.record_kill("1000", killer, "k", victim, "v", weapon, dist)
    if alter:
        db._conn.execute("UPDATE kills SET created_at=? WHERE id=(SELECT MAX(id) FROM kills)",
                         (time.time() - alter,))
        db._conn.commit()


def test_erweiterte_statistik(edb):
    _kill(edb, "A", "X", "Crossbow", 28, alter=40 * 86400)   # aelter als ein Monat
    _kill(edb, "A", "Y", "Crossbow", 400, alter=3 * 86400)   # diese Woche, Long Range
    _kill(edb, "Z", "A", "AKM", 10, alter=2 * 86400)         # Tod
    _kill(edb, "A", "Q", "M4A1", 60)                         # heute
    edb.roster_add_playtime(111, "1000", "A", 7530)
    st = edb.player_stats_erweitert("1000", "a", 111)
    assert (st["kills"], st["kills_tag"], st["kills_woche"], st["kills_monat"]) == (3, 1, 2, 2)
    assert st["deaths"] == 1 and abs(st["kd"] - 3.0) < 1e-9
    assert st["fav_weapon"] == "Crossbow" and st["fav_weapon_kills"] == 2
    assert st["longest"] == 400 and st["longest_weapon"] == "Crossbow"
    assert st["last_killed"] == "Q" and st["last_killed_by"] == "Z"
    assert (st["kill_streak"], st["best_kill_streak"]) == (1, 2)
    assert (st["death_streak"], st["worst_death_streak"]) == (0, 1)
    assert st["playtime_seconds"] == 7530
    assert edb.player_stats_erweitert("1000", "Niemand", 111) is None
    # nur Spielzeit, keine Kills -> trotzdem Daten
    edb.roster_add_playtime(111, "1000", "NurOnline", 60)
    assert edb.player_stats_erweitert("1000", "NurOnline", 111)["kills"] == 0


def test_dauer_text():
    assert bot._dauer_text(7530) == "2 Std 5 Min"
    assert bot._dauer_text(7530, "en") == "2h 5m"
    assert bot._dauer_text(0) == "0 Min"
    assert bot._dauer_text(8 * 86400 + 3600, "en") == "1w 1d 1h"


def test_stats_beides_angegeben_fehler(edb):
    i = _Inter(42)
    _run(bot.cmd_stats.callback(i, "A", _User(7)))
    assert i.response.sent[0]["ephemeral"] and "nicht beides" in i.response.sent[0]["content"]


def test_stats_ohne_angabe_eigene_verknuepfung(edb):
    _kill(edb, "Haupt", "X")
    i = _Inter(42)
    _run(bot.cmd_stats.callback(i, None, None))
    assert "/link" in i.response.sent[0]["content"]       # nicht verknuepft
    edb.link_user(111, 42, "Haupt")
    i = _Inter(42)
    _run(bot.cmd_stats.callback(i, None, None))
    e = i.response.sent[0]["embed"]
    assert e is not None and "Haupt" in e.title


def test_stats_username_ohne_verknuepfung_und_user_mit(edb):
    _kill(edb, "Fremd", "X", "AKM", 350)
    i = _Inter(42)
    _run(bot.cmd_stats.callback(i, "fremd", None))
    e = i.response.sent[0]["embed"]
    assert e is not None and "fremd" in e.title
    assert any("Long Range" in f.value and "1" in f.value for f in e.fields)
    # user ohne Verknuepfung -> Fehler
    i = _Inter(42)
    _run(bot.cmd_stats.callback(i, None, _User(7)))
    assert "verknüpft" in i.response.sent[0]["content"]
    edb.link_user(111, 7, "Fremd")
    i = _Inter(42)
    _run(bot.cmd_stats.callback(i, None, _User(7)))
    e = i.response.sent[0]["embed"]
    assert e is not None and any("<@7>" in f.value for f in e.fields)


def test_stats_unbekannter_name(edb):
    i = _Inter(42)
    _run(bot.cmd_stats.callback(i, "Niemand", None))
    assert "Keine Daten" in i.response.sent[0]["content"]
