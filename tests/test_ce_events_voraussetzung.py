"""CE-Events-Feed: Voraussetzung in der cfgeconomycore.xml (log_ce_dynamicevent + log_ce_vehicle).

Der Bot liest die beiden Werte selbst, zeigt sie im Dialog an und laesst den Feed erst zu, wenn beide true sind. Dazu ein
Beleg aus der RPT, ob der Server den Zaehler-Dump wirklich schreibt (nach einer Aenderung der Datei braucht es einen Neustart).

    python3 -m pytest tests/test_ce_events_voraussetzung.py -q
"""
import asyncio
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from unittest import mock

import pytest
from aiohttp.test_utils import make_mocked_request
from aiohttp.web_urldispatcher import UrlMappingMatchInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot
import test_deployment as td

MISSION = "/mission"
DATEI = f"{MISSION}/cfgeconomycore.xml"
GID = 4711


def _core(dyn="true", veh="true", extra=""):
    """Eine cfgeconomycore.xml wie auf dem Server (Auszug der echten Datei)."""
    zeilen = []
    if dyn is not None:
        zeilen.append(f'<default name="log_ce_dynamicevent" value="{dyn}"/>')
    if veh is not None:
        zeilen.append(f'<default name="log_ce_vehicle" value="{veh}"/>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<economycore>\n    <classes>\n'
            '        <rootclass name="DefaultWeapon" behaviour="CfgWeapons"/>\n    </classes>\n    <defaults>\n'
            '        <default name="dyn_radius" value="30" />\n        <default name="log_ce_loop" value="false"/>\n        '
            + "\n        ".join(zeilen) + extra + '\n    </defaults>\n</economycore>\n')


# ── Die beiden Werte aus der Datei lesen ─────────────────────────────────
def _werte(xml):
    return {k: (v["zustand"], v["wert"]) for k, v in bot._ce_log_werte_lesen(ET.fromstring(xml)).items()}


def test_beide_true():
    assert _werte(_core()) == {"log_ce_dynamicevent": ("an", True), "log_ce_vehicle": ("an", True)}


def test_einer_false():
    assert _werte(_core(veh="false"))["log_ce_vehicle"] == ("aus", False)
    assert _werte(_core(dyn="false"))["log_ce_dynamicevent"] == ("aus", False)


def test_schluessel_fehlt_ist_nicht_an():
    w = _werte(_core(veh=None))
    assert w["log_ce_vehicle"] == ("fehlt", None) and w["log_ce_dynamicevent"] == ("an", True)


@pytest.mark.parametrize("roh", ["True", "1", "yes", "", "TRUE", " true x"])
def test_nur_exakt_true_und_false_gelten(roh):
    assert _werte(_core(dyn=roh))["log_ce_dynamicevent"][0] == "ungueltig"


def test_leerraum_um_den_wert_ist_erlaubt():
    assert _werte(_core(dyn=" true "))["log_ce_dynamicevent"] == ("an", True)


def test_mehrfach_gilt_der_letzte_wert():
    xml = _core(dyn="true", extra='\n        <default name="log_ce_dynamicevent" value="false"/>')
    assert _werte(xml)["log_ce_dynamicevent"] == ("aus", False)


def test_aehnliche_schluessel_werden_nicht_verwechselt():
    xml = _core(dyn=None, extra='\n        <default name="log_ce_dynamicevent_x" value="true"/>')
    assert _werte(xml)["log_ce_dynamicevent"] == ("fehlt", None)


# ── Live-Pruefung per FTP ────────────────────────────────────────────────
def _conn(dateien=None, mission=True):
    conn = td._server()
    conn.data["ftp_mission_dir"] = MISSION if mission else ""
    conn.ftp = td._StubFTP(dateien if dateien is not None else {DATEI: _core()})
    return conn


def _status(conn):
    return asyncio.run(bot._ce_log_status(conn))


def test_status_beide_true_ist_aktivierbar():
    s = _status(_conn())
    assert s["lesbar"] and s["beide_an"] and s["fehler"] is None
    assert [(b["key"], b["zustand"]) for b in s["bedingungen"]] == [("log_ce_dynamicevent", "an"), ("log_ce_vehicle", "an")]


@pytest.mark.parametrize("dyn,veh", [("true", "false"), ("false", "true"), ("false", "false"), ("true", None), ("true", "x")])
def test_status_ein_fehlender_schalter_sperrt(dyn, veh):
    s = _status(_conn({DATEI: _core(dyn, veh)}))
    assert s["lesbar"] and not s["beide_an"]


def test_status_nennt_die_gelesene_datei():
    assert _status(_conn())["pfad"] == DATEI


def test_beleg_startlisting_der_event_typen_ist_kein_zaehler_dump(monkeypatch):
    """Das einmalige Start-Listing "[CE][DynEvent] :: 57 types / [28] StaticHeliCrash" steht in jedem RPT - der periodische Dump
    "DynamicEvent Types (N):" mit "StaticHeliCrash (3)" dagegen nur, wenn log_ce_dynamicevent wirksam ist (Tester-Server)."""
    conn = _conn()
    conn.data["ftp_log_dir"] = "/log"
    text = ("21:48:26.105 [CE][DynEvent] :: 57 types\n21:48:26.105 [28] StaticHeliCrash\n"
            "21:48:26.105   Primary Spawner: \"Static\", active: yes, repeat: 1\n"
            "21:48:26.105 [30] StaticMilitaryConvoy\n21:48:26.105   Primary Spawner: \"Static\", active: yes, repeat: 1\n"
            "21:48:26.105 !!! [CE][DE] DynamicEvent \"StaticTrain\" setup is invalid, event will be disabled.\n"
            "22:02:35.61  [CE][SpawnRandomLoot] (StaticMilitaryConvoy) :: Type: X, lootmin: 2\n")
    _RptWelt(monkeypatch, ["/log/a.RPT"], text)
    assert _beleg(conn)["zustand"] == "kein_dump"


def test_status_liest_jedes_mal_frisch():
    conn = _conn()
    assert _status(conn)["beide_an"]
    conn.ftp.dateien[DATEI] = _core(veh="false")                    # der Tester aendert die Datei
    assert not _status(conn)["beide_an"]
    conn.ftp.dateien[DATEI] = _core()
    assert _status(conn)["beide_an"]


@pytest.mark.parametrize("fall,erwartet", [
    ("ohne_datei", "nicht gefunden"), ("kaputt", "kein gültiges XML"), ("ohne_ordner", "Mission-Ordner"),
    ("ohne_ftp", "FTP-Zugang")])
def test_status_nicht_lesbar_bleibt_gesperrt_mit_klarem_grund(fall, erwartet):
    conn = _conn({DATEI: "<economycore><defaults>"} if fall == "kaputt" else ({} if fall == "ohne_datei" else None),
                 mission=fall != "ohne_ordner")
    if fall == "ohne_ftp":
        conn.ftp = None
    s = _status(conn)
    assert not s["lesbar"] and not s["beide_an"] and erwartet in s["fehler"]


def test_status_ftp_ausnahme_wird_zur_meldung_nicht_zum_absturz():
    conn = _conn()

    def kaputt(_pfad):
        raise OSError("Verbindung getrennt")
    conn.ftp.read_file_ex = kaputt
    s = _status(conn)
    assert not s["lesbar"] and "per FTP nicht gelesen" in s["fehler"]


def test_status_mit_bom_am_dateianfang():
    assert _status(_conn({DATEI: "﻿" + _core()}))["beide_an"]


def test_aktivierungstext_nennt_beide_werte_und_den_neustart():
    t = bot._ce_aktivierung_text(_status(_conn({DATEI: _core(veh="false")})))
    assert "log_ce_dynamicevent = true" in t and "log_ce_vehicle = false" in t and "Server neu starten" in t
    t = bot._ce_aktivierung_text(_status(_conn({DATEI: _core(veh=None)})))
    assert "log_ce_vehicle = fehlt" in t


# ── Beleg aus der RPT ────────────────────────────────────────────────────
class _RptWelt:
    """Gestubbte Log-Funktionen: eine RPT-Liste, eine Groesse und der Text am Ende der Datei."""

    def __init__(self, monkeypatch, dateien, text, groesse=None):
        self.gelesen = []

        async def liste(conn, verzeichnis, endung):
            return list(dateien) if endung == ".rpt" else []

        async def groesse_(conn, pfad):
            return groesse if groesse is not None else (None if text is None else len(text) + 5000)

        async def lesen(conn, pfad, offset):
            self.gelesen.append((pfad, offset))
            return text, offset + len(text or "")
        monkeypatch.setattr(bot, "_log_dateien", liste)
        monkeypatch.setattr(bot, "_log_dateigroesse", groesse_)
        monkeypatch.setattr(bot, "_log_lesen_ab_offset", lesen)


def _dump(zaehler, vorspann="12:00:00.000 [CE][DE] DynamicEvent Types (%d):\n"):
    return (vorspann % len(zaehler)) + "".join(f"  {typ} ({n})\n" for typ, n in zaehler) + "naechste Zeile ohne Zaehler\n"


def _beleg(conn):
    return asyncio.run(bot._ce_rpt_beleg(conn))


def test_beleg_dump_gefunden_und_nur_bekannte_typen_gelistet(monkeypatch):
    conn = _conn()
    conn.data["ftp_log_dir"] = "/log"
    text = "irgendwas\n" + _dump([("StaticHeliCrash", 2), ("StaticMilitaryConvoy", 1), ("Unbekannt", 9), ("VehicleBoat", 4)])
    _RptWelt(monkeypatch, ["/log/DayZServer_x_20261029_100000.RPT"], text)
    b = _beleg(conn)
    assert b["zustand"] == "dump" and b["datei"] == "DayZServer_x_20261029_100000.RPT" and b["typen"] == 4
    assert [(k["typ"], k["anzahl"]) for k in b["bekannt"]] == [("StaticHeliCrash", 2), ("StaticMilitaryConvoy", 1), ("VehicleBoat", 4)]
    assert {k["schalter"] for k in b["bekannt"]} == {"helicopter_crash", "convoy", "vehicle_event"}


def test_beleg_nimmt_den_letzten_vollstaendigen_dump(monkeypatch):
    conn = _conn()
    conn.data["ftp_log_dir"] = "/log"
    text = _dump([("StaticHeliCrash", 1)]) + _dump([("StaticHeliCrash", 3)])
    _RptWelt(monkeypatch, ["/log/a.RPT"], text)
    assert _beleg(conn)["bekannt"][0]["anzahl"] == 3


def test_beleg_unvollstaendiger_letzter_dump_zaehlt_nicht(monkeypatch):
    conn = _conn()
    conn.data["ftp_log_dir"] = "/log"
    text = _dump([("StaticHeliCrash", 1)]) + "12:00:05.000 [CE][DE] DynamicEvent Types (1):\n  StaticHeliCrash (9)\n"
    _RptWelt(monkeypatch, ["/log/a.RPT"], text)
    assert _beleg(conn)["bekannt"][0]["anzahl"] == 1


def test_beleg_ohne_dump_im_log(monkeypatch):
    conn = _conn()
    conn.data["ftp_log_dir"] = "/log"
    _RptWelt(monkeypatch, ["/log/a.RPT"], "viel anderer Text\nohne Zaehler\n")
    assert _beleg(conn)["zustand"] == "kein_dump"


def test_beleg_ohne_rpt_ohne_logordner_und_bei_lesefehler(monkeypatch):
    conn = _conn()
    assert _beleg(conn)["zustand"] == "unbekannt"                   # kein Log-Verzeichnis bekannt
    conn.data["ftp_log_dir"] = "/log"
    _RptWelt(monkeypatch, [], "")
    assert _beleg(conn)["zustand"] == "keine_rpt"
    _RptWelt(monkeypatch, ["/log/a.RPT"], None)
    assert _beleg(conn)["zustand"] == "nicht_lesbar"


def test_beleg_liest_nur_das_ende_unter_der_api_grenze(monkeypatch):
    conn = _conn()
    conn.data["ftp_log_dir"] = "/log"
    welt = _RptWelt(monkeypatch, ["/log/a.RPT"], _dump([("StaticHeliCrash", 1)]), groesse=5_000_000)
    _beleg(conn)
    pfad, offset = welt.gelesen[0]
    assert 5_000_000 - offset == bot._CE_RPT_BELEG_BYTES < 65535


def test_beleg_kleine_datei_wird_ab_null_gelesen(monkeypatch):
    conn = _conn()
    conn.data["ftp_log_dir"] = "/log"
    welt = _RptWelt(monkeypatch, ["/log/a.RPT"], _dump([("StaticHeliCrash", 1)]), groesse=2000)
    _beleg(conn)
    assert welt.gelesen[0][1] == 0


def test_beleg_ausnahme_wird_zu_nicht_lesbar(monkeypatch):
    conn = _conn()
    conn.data["ftp_log_dir"] = "/log"

    async def kaputt(*a, **k):
        raise OSError("weg")
    monkeypatch.setattr(bot, "_log_dateien", kaputt)
    assert _beleg(conn)["zustand"] == "nicht_lesbar"


# ── Endpunkte ────────────────────────────────────────────────────────────
@pytest.fixture
def welt(monkeypatch):
    bot._DASH_RATE_LIMIT_LAST.clear()
    monkeypatch.setattr(bot.cfg, "save_guilds", lambda: None)
    monkeypatch.setattr(bot, "_session_guilds", lambda request: [GID])
    monkeypatch.setattr(bot, "_kanal_gehoert_guild", lambda gid, kanal: None)
    conn = _conn()
    bot.connections.add_guild(conn.service_id, GID)
    conn.data["ftp_log_dir"] = "/log"

    async def keine_rpt(c, v, e):
        return []
    monkeypatch.setattr(bot, "_log_dateien", keine_rpt)
    feeds = bot.cfg.server_feeds(GID, conn.service_id, anlegen=True)
    feeds.clear()
    return {"conn": conn, "mp": monkeypatch, "feeds": feeds}


def _aufruf(welt, handler, methode, pfad, daten=None, match=None):
    bot._DASH_RATE_LIMIT_LAST.clear()
    conn = welt["conn"]
    sid = f"ce-{time.time_ns()}"
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": "acct-ce"}, "is_admin": True,
                            "service_id": conn.service_id, "seen": time.time()}
    req = make_mocked_request(methode, pfad, headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"},
                              match_info=UrlMappingMatchInfo(match or {}, mock.Mock()))

    async def fake_body(_r):
        return daten or {}
    welt["mp"].setattr(bot, "body", fake_body)
    resp = asyncio.run(handler(req))
    return resp.status, json.loads(resp.body.decode("utf-8"))


def _setze_feed(welt, daten):
    return _aufruf(welt, bot.set_feed, "POST", f"/api/feeds/{GID}/ce_events", daten, {"guild_id": str(GID), "log_type": "ce_events"})


def test_status_endpunkt_zeigt_bedingungen_und_beleg(welt):
    st, d = _aufruf(welt, bot.api_ce_events_status, "GET", "/api/feeds/ce-events/status")
    assert st == 200 and d["data"]["aktivierbar"] is True and d["data"]["rpt"]["zustand"] == "keine_rpt"
    assert [b["zustand"] for b in d["data"]["bedingungen"]] == ["an", "an"]
    welt["conn"].ftp.dateien[DATEI] = _core(dyn="false")
    st, d = _aufruf(welt, bot.api_ce_events_status, "GET", "/api/feeds/ce-events/status")
    assert d["data"]["aktivierbar"] is False and d["data"]["bedingungen"][0]["zustand"] == "aus"


def test_neuer_feed_laesst_sich_nur_mit_beiden_schaltern_aktivieren(welt):
    welt["conn"].ftp.dateien[DATEI] = _core(veh="false")
    st, d = _setze_feed(welt, {"channel_id": "555", "enabled": True})
    assert st == 409 and d["code"] == "ce_log_schalter" and "log_ce_vehicle = false" in d["error"]
    assert "ce_events" not in welt["feeds"]                         # nichts gespeichert
    welt["conn"].ftp.dateien[DATEI] = _core()
    st, d = _setze_feed(welt, {"channel_id": "555", "enabled": True})
    assert st == 200 and welt["feeds"]["ce_events"]["enabled"] is True


def test_feed_darf_ausgeschaltet_angelegt_werden_auch_wenn_die_schalter_fehlen(welt):
    welt["conn"].ftp.dateien[DATEI] = _core(dyn="false", veh="false")
    st, _d = _setze_feed(welt, {"channel_id": "555", "enabled": False})
    assert st == 200 and welt["feeds"]["ce_events"]["enabled"] is False


def test_schnellschalter_gesperrt_bis_beide_true(welt):
    _setze_feed(welt, {"channel_id": "555", "enabled": False})
    welt["conn"].ftp.dateien[DATEI] = _core(dyn=None, veh=None)
    st, d = _setze_feed(welt, {"enabled": True})
    assert st == 409 and welt["feeds"]["ce_events"]["enabled"] is False
    welt["conn"].ftp.dateien[DATEI] = _core()
    st, _d = _setze_feed(welt, {"enabled": True})
    assert st == 200 and welt["feeds"]["ce_events"]["enabled"] is True


def test_ausschalten_geht_immer(welt):
    _setze_feed(welt, {"channel_id": "555", "enabled": True})
    welt["conn"].ftp.dateien[DATEI] = _core(dyn="false", veh="false")
    st, _d = _setze_feed(welt, {"enabled": False})
    assert st == 200 and welt["feeds"]["ce_events"]["enabled"] is False


def test_schon_aktiver_feed_laesst_sich_weiter_speichern(welt):
    """Farbe/Channel aendern darf nicht an der Datei scheitern, solange der Feed aktiv bleibt."""
    _setze_feed(welt, {"channel_id": "555", "enabled": True})
    welt["conn"].ftp.dateien[DATEI] = _core(dyn="false")
    st, _d = _setze_feed(welt, {"channel_id": "556", "colour": "#112233", "enabled": True})
    assert st == 200 and welt["feeds"]["ce_events"]["channel_id"] == 556 and welt["feeds"]["ce_events"]["enabled"] is True


def test_speichern_ohne_enabled_behaelt_den_pausierten_zustand(welt):
    _setze_feed(welt, {"channel_id": "555", "enabled": False})
    welt["conn"].ftp.dateien[DATEI] = _core(dyn="false")
    st, _d = _setze_feed(welt, {"channel_id": "556"})
    assert st == 200 and welt["feeds"]["ce_events"]["enabled"] is False


def test_nicht_lesbare_datei_sperrt_die_aktivierung(welt):
    welt["conn"].ftp.dateien.clear()
    st, d = _setze_feed(welt, {"channel_id": "555", "enabled": True})
    assert st == 409 and "prüfen kann" in d["error"] and "nicht gefunden" in d["error"]


def test_andere_feeds_sind_von_der_sperre_nicht_betroffen(welt):
    welt["conn"].ftp.dateien[DATEI] = _core(dyn="false", veh="false")
    st, _d = _aufruf(welt, bot.set_feed, "POST", f"/api/feeds/{GID}/kill", {"channel_id": "555", "enabled": True},
                     {"guild_id": str(GID), "log_type": "kill"})
    assert st == 200 and welt["feeds"]["kill"]["enabled"] is True
