"""Tests fuer den CE-Events-Feed (Heli-Crash, Convoy, Checkpoint, Abandoned
Train, Vehicle Event).

Brigarde wollte einen Feed fuer besondere Central-Economy-Events. Anhand
einer echten RPT-Datei wurde verifiziert: DayZ schreibt KEINE einzelne
"Event ist da"-Zeile, sondern alle 5 Sekunden einen Zaehler-Dump mit ALLEN
Event-Typen. Ein neues Event erkennt man nur daran, dass die Zahl zwischen
zwei Dumps steigt - das ist, was _ce_events_zeilen_auswerten reproduziert.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_ce_events_feed.py -v
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


# ── Reine Diff-Funktion, kein I/O ────────────────────────────────────────

def test_erster_dump_meldet_nichts_nur_baseline():
    counts = {}
    zeilen = ["  3:03:03.231   StaticHeliCrash (3)"]
    ergebnisse = bot_mod._ce_events_zeilen_auswerten(zeilen, counts)
    assert ergebnisse == []
    assert counts["StaticHeliCrash"] == 3


def test_anstieg_um_eins_meldet_einen_treffer():
    counts = {"StaticHeliCrash": 3}
    zeilen = ["  3:03:08.231   StaticHeliCrash (4)"]
    ergebnisse = bot_mod._ce_events_zeilen_auswerten(zeilen, counts)
    assert ergebnisse == [("StaticHeliCrash", "helicopter_crash", 4)]
    assert counts["StaticHeliCrash"] == 4


def test_anstieg_um_mehrere_meldet_je_einen_treffer_pro_schritt():
    counts = {"StaticMilitaryConvoy": 50}
    zeilen = ["  3:03:08.231   StaticMilitaryConvoy (53)"]
    ergebnisse = bot_mod._ce_events_zeilen_auswerten(zeilen, counts)
    assert ergebnisse == [
        ("StaticMilitaryConvoy", "convoy", 51),
        ("StaticMilitaryConvoy", "convoy", 52),
        ("StaticMilitaryConvoy", "convoy", 53),
    ]


def test_rueckgang_oder_gleichstand_meldet_nichts():
    counts = {"StaticTrain": 44}
    zeilen = ["  3:03:08.231   StaticTrain (44)", "  3:03:13.231   StaticTrain (40)"]
    ergebnisse = bot_mod._ce_events_zeilen_auswerten(zeilen, counts)
    assert ergebnisse == []
    assert counts["StaticTrain"] == 40


def test_alle_vier_bekannten_typen_richtig_zugeordnet():
    counts = {"StaticHeliCrash": 0, "StaticMilitaryConvoy": 0,
              "StaticPoliceSituation": 0, "StaticTrain": 0}
    zeilen = [
        "  StaticHeliCrash (1)",
        "  StaticMilitaryConvoy (1)",
        "  StaticPoliceSituation (1)",
        "  StaticTrain (1)",
    ]
    ergebnisse = bot_mod._ce_events_zeilen_auswerten(zeilen, counts)
    schalter = {e[1] for e in ergebnisse}
    assert schalter == {"helicopter_crash", "convoy", "checkpoint", "abandoned_train"}


def test_vehicle_stern_typen_fallen_unter_einen_schalter_mit_klarnamen():
    counts = {"VehicleTruck01": 10}
    zeilen = ["  VehicleTruck01 (11)"]
    ergebnisse = bot_mod._ce_events_zeilen_auswerten(zeilen, counts)
    assert ergebnisse == [("VehicleTruck01", "vehicle_event", 11)]


def test_unbekannte_typen_werden_ignoriert():
    counts = {}
    zeilen = ["  ItemPlanks (50)", "  AnimalWolf (3)", "  Loot (0)"]
    ergebnisse = bot_mod._ce_events_zeilen_auswerten(zeilen, counts)
    assert ergebnisse == []
    assert counts == {}


def test_zeitstempel_mit_einer_nachkommastelle_wird_trotzdem_erkannt():
    # Echte Falle aus Brigardes RPT-Datei: "3:04:41.16" statt "3:04:41.160".
    counts = {"StaticHeliCrash": 2}
    zeilen = [" 3:04:41.16   StaticHeliCrash (3)"]
    ergebnisse = bot_mod._ce_events_zeilen_auswerten(zeilen, counts)
    assert ergebnisse == [("StaticHeliCrash", "helicopter_crash", 3)]


def test_nicht_passende_zeilen_werden_uebersprungen():
    counts = {}
    zeilen = ["[CE][DE] DynamicEvent Types (56):", "beliebiger anderer RPT-Text",
              "!!! [CE][DE][SPAWNS] :: [WARNING] :: Skipping entry ..."]
    ergebnisse = bot_mod._ce_events_zeilen_auswerten(zeilen, counts)
    assert ergebnisse == []


# ── Sub-Toggle-Einstellungen (Speichern/Laden) ───────────────────────────

def test_ce_events_payload_default_alle_aktiv():
    bot_mod.connections.upsert("ceevents-service-1")
    bot_mod.connections.assign_guild("ceevents-service-1", 5001)
    conn = bot_mod.connections.for_service("ceevents-service-1")
    payload = bot_mod._ce_events_payload(conn)
    assert payload == {
        "helicopter_crash": True, "convoy": True, "checkpoint": True,
        "abandoned_train": True, "vehicle_event": True,
    }


def test_ce_events_payload_persistiert_deaktivierten_schalter():
    bot_mod.connections.upsert("ceevents-service-2")
    bot_mod.connections.assign_guild("ceevents-service-2", 5002)
    conn = bot_mod.connections.for_service("ceevents-service-2")
    bot_mod._conn_store(conn, "ce_events_convoy_enabled", False)
    payload = bot_mod._ce_events_payload(conn)
    assert payload["convoy"] is False
    assert payload["helicopter_crash"] is True


# ── FEED_TYPES-Registrierung ──────────────────────────────────────────────

def test_feed_types_ce_events_registriert():
    assert "ce_events" in bot_mod.FEED_TYPES
    assert bot_mod.FEED_TYPES["ce_events"]["label"] == "CE-Events"


# ── _lese_ce_events: Integrationstest mit gestubbter FTP ─────────────────

class _StubFTP:
    def __init__(self, dateien, groessen, inhalte):
        self.dateien = dateien
        self.groessen = groessen
        self.inhalte = inhalte  # {(pfad, offset): text}

    def list_rpt_files(self, directory):
        return self.dateien

    def file_size_or_none(self, pfad):
        return self.groessen.get(pfad)

    def read_from_offset(self, pfad, offset):
        text = self.inhalte.get((pfad, offset), "")
        return text, offset + len(text.encode("utf-8"))


class _StubLoop:
    async def run_in_executor(self, _executor, func, *args):
        return func(*args)


@pytest.fixture
def conn_mit_ce_events(monkeypatch):
    bot_mod.connections.upsert("ceevents-service-3")
    bot_mod.connections.assign_guild("ceevents-service-3", 5003)
    conn = bot_mod.connections.for_service("ceevents-service-3")

    kanal_gesendet = []

    async def fake_post_feed(guild_id, feed_key, embed, service_id=None, anhang=None):
        kanal_gesendet.append({"guild_id": guild_id, "feed_key": feed_key, "embed": embed})
        return True, "ok"
    monkeypatch.setattr(bot_mod, "_post_feed", fake_post_feed)
    return conn, kanal_gesendet


def test_lese_ce_events_erster_zyklus_setzt_nur_cursor_ohne_post(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", _StubLoop()))
    assert gesendet == []
    assert conn.log_state["ce_events"] == {"file": pfad, "offset": 500}


def test_lese_ce_events_postet_bei_anstieg(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    # Erster Zyklus: Baseline setzen.
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 2
    # Zweiter Zyklus: neue Zeile mit gestiegenem Zaehler ab dem gemerkten Offset.
    neuer_text = "  3:03:08.231   StaticHeliCrash (3)\n"
    conn.ftp.inhalte[(pfad, 500)] = neuer_text
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 1
    assert gesendet[0]["feed_key"] == "ce_events"
    assert "3 aktiv" in gesendet[0]["embed"].description


def test_lese_ce_events_deaktivierter_schalter_postet_nicht(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    bot_mod._conn_store(conn, "ce_events_helicopter_crash_enabled", False)
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 2
    conn.ftp.inhalte[(pfad, 500)] = "  StaticHeliCrash (3)\n"
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert gesendet == []


def test_lese_ce_events_rotation_setzt_baseline_zurueck(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    alter_pfad = "/games/x/config/DayZServer_alt.RPT"
    neuer_pfad = "/games/x/config/DayZServer_neu.RPT"
    conn.ftp = _StubFTP([alter_pfad], {alter_pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 3
    # Rotation: neue Datei, DayZ-Zaehler faengt bei 0 wieder an.
    conn.ftp.dateien = [neuer_pfad]
    conn.ftp.groessen[neuer_pfad] = 0
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert conn.ce_event_counts == {}
    assert conn.log_state["ce_events"] == {"file": neuer_pfad, "offset": 0}
    assert gesendet == []
