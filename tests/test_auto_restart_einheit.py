"""Ein Neustart-Plan - egal ob per /auto restart oder im Dashboard angelegt: loescht man ihn
(/auto off oder Dashboard), ist er ueberall weg und wird nicht mehr ausgefuehrt."""
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


class _Api:
    def __init__(self):
        self.restarts = 0

    async def restart(self):
        self.restarts += 1
        return True, "ok"


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "connections.json").write_text(json.dumps(
        {"1000": {"service_id": "1000", "nitrado_token": "fake", "owner_discord_id": "1", "map_name": "Livonia"}}))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    c = reg._conns["1000"]
    c.api = _Api()
    return c


def _plaene(c):
    return [t for t in bot._scheduled_tasks(c) if t.get("task") == "restart_server"]


def _alt(c, an=True, first="04:00", h=4):
    c.set("auto_restart_schedule", {"enabled": an, "first_time": first, "interval_hours": h})


def _dash(monkeypatch, c, handler, methode="GET", daten=None, id_=None):
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": True,
                            "service_id": c.service_id, "seen": time.time()}
    req = make_mocked_request(methode, "/api/scheduled-tasks", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"},
                              match_info={"id": str(id_)} if id_ is not None else {})

    async def body(_r):
        return daten
    monkeypatch.setattr(bot, "body", body)
    r = asyncio.run(handler(req))
    return r.status, json.loads(r.body)


def test_befehl_legt_eine_auto_aufgabe_an_und_ersetzt_den_alten_plan(conn):
    nxt = bot._auto_restart_task_setzen(conn, "04:00", 2)
    (t,) = _plaene(conn)
    assert t["quelle"] == "auto_command" and t["interval_seconds"] == 7200 and t["next_execution"] == nxt
    assert t["restart_feed"] is True and t["id"] == 1
    bot._auto_restart_task_setzen(conn, "05:30", 3)
    (t,) = _plaene(conn)
    assert t["interval_seconds"] == 10800 and t["id"] == 1
    assert bot.bot._next_scheduled_restart(conn) == t["next_execution"]


def test_alt_plan_wird_in_aufgabe_umgezogen_und_ist_danach_aus(conn):
    _alt(conn, True, "06:00", 2)
    bot._auto_restart_migrieren(conn)
    (t,) = _plaene(conn)
    assert t["interval_seconds"] == 7200 and conn.get("auto_restart_schedule")["enabled"] is False
    bot._auto_restart_migrieren(conn)
    assert len(_plaene(conn)) == 1


def test_kaputter_alt_plan_wird_abgeschaltet(conn):
    _alt(conn, True, "kaputt", 2)
    bot._auto_restart_migrieren(conn)
    assert _plaene(conn) == [] and conn.get("auto_restart_schedule")["enabled"] is False


def test_auto_off_loescht_befehls_und_dashboard_plaene_und_alt_plan(conn):
    bot._auto_restart_task_setzen(conn, "04:00", 2)
    bot._scheduled_tasks(conn).append({"id": 9, "task": "restart_server", "next_execution": time.time() + 99,
                                       "initial_execution": time.time(), "interval_seconds": 600,
                                       "channel_ignore": True, "channel_id": None})
    bot._scheduled_tasks(conn).append({"id": 10, "task": "base_damage_on", "next_execution": time.time() + 99,
                                       "interval_seconds": 600, "channel_ignore": True})
    _alt(conn, True)
    entfernt, alt = bot._auto_restart_alle_loeschen(conn)
    assert entfernt == 2 and alt is True
    assert _plaene(conn) == [] and conn.get("auto_restart_schedule")["enabled"] is False
    assert [t["task"] for t in bot._scheduled_tasks(conn)] == ["base_damage_on"]   # andere Aufgaben bleiben
    assert bot.bot._next_scheduled_restart(conn) is None
    assert bot._auto_restart_alle_loeschen(conn) == (0, False)
    # steht auch auf der Platte
    roh = json.load(open("connections.json", encoding="utf-8"))["1000"]
    assert [t["task"] for t in roh["scheduled_tasks"]] == ["base_damage_on"]


def test_dashboard_loeschen_beendet_auch_einen_noch_aktiven_alt_plan(monkeypatch, conn):
    bot._auto_restart_task_setzen(conn, "04:00", 2)
    _alt(conn, True)                      # Alt-Plan, der (z. B. nach Neustart des Bots) noch aktiv ist
    tid = _plaene(conn)[0]["id"]
    status, j = _dash(monkeypatch, conn, bot.delete_scheduled_task, "DELETE", id_=tid)
    assert status == 200, j
    assert _plaene(conn) == [] and conn.get("auto_restart_schedule")["enabled"] is False
    assert bot.bot._next_scheduled_restart(conn) is None


def test_dashboard_erstellt_auto_off_loescht(monkeypatch, conn):
    daten = {"task": "restart_server", "initial_execution": time.time() + 3600, "interval_seconds": 7200,
             "channel_ignore": True, "note": "x"}
    status, j = _dash(monkeypatch, conn, bot.create_scheduled_task, "POST", daten)
    assert status == 200, j
    assert len(_plaene(conn)) == 1 and bot.bot._next_scheduled_restart(conn) is not None
    bot._auto_restart_alle_loeschen(conn)
    assert _plaene(conn) == []


def test_befehls_plan_laesst_sich_im_dashboard_bearbeiten(monkeypatch, conn):
    bot._auto_restart_task_setzen(conn, "04:00", 2)
    t = _plaene(conn)[0]
    daten = {"task": "restart_server", "initial_execution": t["initial_execution"], "interval_seconds": 3600,
             "channel_ignore": False, "channel_id": "", "note": "n"}
    status, j = _dash(monkeypatch, conn, bot.update_scheduled_task, "PUT", daten, id_=t["id"])
    assert status == 200, j
    t = _plaene(conn)[0]
    assert t["interval_seconds"] == 3600 and t["restart_feed"] is True and t["channel_id"] is None


def test_scheduler_startet_genau_einmal_und_postet_in_den_restart_feed(monkeypatch, conn):
    posts = []

    async def feed(embed, c=None, view=None):
        posts.append(embed.title)
    monkeypatch.setattr(bot.bot, "_post_restart_feed", feed)
    bot._auto_restart_task_setzen(conn, "04:00", 2)
    t = _plaene(conn)[0]
    t["next_execution"] = time.time() - 5
    asyncio.run(bot.bot._restart_scheduler_conn(conn))        # Alt-Schleife darf NICHT mehr neu starten
    assert conn.api.restarts == 0
    asyncio.run(bot.bot._scheduled_tasks_conn(conn))
    assert conn.api.restarts == 1 and posts == ["🔄 Server wird jetzt neu gestartet"]
    assert t["next_execution"] > time.time()
    asyncio.run(bot.bot._scheduled_tasks_conn(conn))
    assert conn.api.restarts == 1


def test_geloeschter_plan_startet_nicht_mehr(monkeypatch, conn):
    bot._auto_restart_task_setzen(conn, "04:00", 2)
    _plaene(conn)[0]["next_execution"] = time.time() - 5
    _alt(conn, True)
    bot._auto_restart_alle_loeschen(conn)
    asyncio.run(bot.bot._restart_scheduler_conn(conn))
    asyncio.run(bot.bot._scheduled_tasks_conn(conn))
    assert conn.api.restarts == 0


def test_auto_off_befehl_und_status(monkeypatch, conn):
    antworten = []

    async def senden(*a, **k):
        antworten.append((a, k))
    itx = SimpleNamespace(response=SimpleNamespace(send_message=senden), locale="de", guild_id=1,
                          user=SimpleNamespace(id=1), guild=None)
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda *a, **k: True)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda i, s: (conn, None))
    bot._auto_restart_task_setzen(conn, "04:00", 2)
    asyncio.run(bot.auto_status.callback(itx, None))
    emb = antworten[-1][1]["embed"]
    assert "alle 2 Stunde(n)" in emb.description
    asyncio.run(bot.auto_off.callback(itx, None))
    assert "deaktiviert" in antworten[-1][0][0] and _plaene(conn) == []
    asyncio.run(bot.auto_status.callback(itx, None))
    assert "Keine geplanten Neustarts" in antworten[-1][0][0]
    asyncio.run(bot.auto_off.callback(itx, None))
    assert "keine geplanten Neustarts" in antworten[-1][0][0]


def test_auto_restart_bestaetigen_legt_aufgabe_an(monkeypatch, conn):
    gesendet = []

    async def edit(*a, **k):
        gesendet.append(k)
    itx = SimpleNamespace(response=SimpleNamespace(edit_message=edit), locale="de", guild_id=None,
                          user=SimpleNamespace(id=5), guild=None)
    monkeypatch.setattr(bot, "_is_admin", lambda i: True)
    _alt(conn, True, "09:00", 6)                       # alter Plan wird dabei ersetzt, nicht doppelt

    async def lauf():
        view = bot.AutoRestartView(itx, 3, conn.service_id)
        view.hour, view.minute = 4, 30
        await view.confirm.callback(itx)
    asyncio.run(lauf())
    (t,) = _plaene(conn)
    assert t["interval_seconds"] == 10800 and t["quelle"] == "auto_command"
    assert conn.get("auto_restart_schedule")["enabled"] is False
    assert "04:30" in gesendet[0]["embed"].description
