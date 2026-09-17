"""Tests fuer den CE-Events-Feed (Heli-Crash, Convoy, Checkpoint, Abandoned
Train, Vehicle Event).

Brigarde wollte einen Feed fuer besondere Central-Economy-Events. Anhand
einer echten RPT-Datei wurde verifiziert: DayZ schreibt KEINE einzelne
"Event ist da"-Zeile, sondern alle 5 Sekunden einen kompletten
"[CE][DE] DynamicEvent Types (N):"-Zaehler-Dump mit ALLEN Event-Typen. Ein
neues Event erkennt man nur daran, dass die Zahl zwischen zwei Bloecken
steigt - das ist, was _ce_events_bloecke_extrahieren/_ce_events_zeilen_auswerten
reproduzieren.

Eine unabhaengige Recherche (siehe Anleitung_ce_events_position.md) hat
zusaetzlich bestaetigt: eine exakte Live-Position eines Events ist auf einem
Nitrado-PS4-Server technisch nicht ermittelbar. Statt einer erfundenen
Koordinate zeigt der Feed deshalb hoechstens die GROESSE des moeglichen
Spawnpools (Stufe A: echte cfgeventspawns.xml des Servers, Stufe B:
offizieller Bohemia-Vanilla-Datensatz, Stufe C: keine Ortsangabe).

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_ce_events_feed.py -v
"""
import asyncio
import os
import sys
import xml.etree.ElementTree as ET

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot_mod = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _run(coro):
    return asyncio.run(coro)


def _block_text(zaehler_zeilen, anzahl_typen=None, terminator="3:03:03.241 [CE][InitializeMap] :: init"):
    """Baut einen VOLLSTAENDIGEN RPT-Textblock: Header + Zaehler-Zeilen +
    eine abschliessende, nicht mehr passende Zeile (die den Block als
    beendet markiert - siehe _ce_events_bloecke_extrahieren)."""
    anzahl_typen = anzahl_typen if anzahl_typen is not None else len(zaehler_zeilen)
    header = f"3:03:03.231 [CE][DE] DynamicEvent Types ({anzahl_typen}):"
    zeilen = [header] + [f"  {z}" for z in zaehler_zeilen] + [terminator]
    return "\n".join(zeilen) + "\n"


# ── Reine Diff-Funktion (innerhalb eines Blocks), kein I/O ────────────────

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
    assert ergebnisse == [("StaticHeliCrash", "helicopter_crash", 1, 4)]
    assert counts["StaticHeliCrash"] == 4


def test_anstieg_um_mehrere_meldet_einen_zusammengefassten_treffer():
    # Anleitung 5.4.6: EIN zusammengefasster Eintrag je Typ und Block, kein
    # Eintrag pro Zaehler-Schritt (sonst wirken zwei Heli-Crashes im selben
    # Block wie viele einzelne, gleich unsichere Ereignisse).
    counts = {"StaticMilitaryConvoy": 50}
    zeilen = ["  3:03:08.231   StaticMilitaryConvoy (53)"]
    ergebnisse = bot_mod._ce_events_zeilen_auswerten(zeilen, counts)
    assert ergebnisse == [("StaticMilitaryConvoy", "convoy", 3, 53)]


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
    assert ergebnisse == [("VehicleTruck01", "vehicle_event", 1, 11)]


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
    assert ergebnisse == [("StaticHeliCrash", "helicopter_crash", 1, 3)]


# ── Block-Extraktion (Header-Bindung, Puffer bei unvollstaendigem Block) ──

def test_bloecke_extrahieren_vollstaendiger_block():
    text = _block_text(["StaticHeliCrash (3)", "StaticTrain (21)"])
    bloecke, rest = bot_mod._ce_events_bloecke_extrahieren(text)
    assert len(bloecke) == 1
    assert bloecke[0] == ["  StaticHeliCrash (3)", "  StaticTrain (21)"]
    assert rest == ""


def test_bloecke_extrahieren_unvollstaendiger_block_bleibt_im_puffer():
    # Kein Terminator am Ende - der Poll-Zyklus hat mitten im Dump abgeschnitten.
    header = "3:03:03.231 [CE][DE] DynamicEvent Types (1):"
    text = header + "\n  StaticHeliCrash (3)\n"
    bloecke, rest = bot_mod._ce_events_bloecke_extrahieren(text)
    assert bloecke == []
    assert rest == text


def test_bloecke_extrahieren_ignoriert_zeilen_ausserhalb_eines_blocks():
    # Eine Zeile im selben Format wie ein Zaehler-Eintrag, aber OHNE
    # vorangehenden Block-Header, darf nicht als CE-Event gewertet werden -
    # genau die im Bericht bemaengelte Luecke der alten, zeilenweisen Auswertung.
    text = "irgendein Log-Text\n  StaticHeliCrash (3)\nnoch mehr Text\n"
    bloecke, rest = bot_mod._ce_events_bloecke_extrahieren(text)
    assert bloecke == []
    assert rest == ""


def test_bloecke_extrahieren_mehrere_bloecke_in_einem_text():
    text = _block_text(["StaticTrain (21)"]) + _block_text(["StaticTrain (22)"])
    bloecke, rest = bot_mod._ce_events_bloecke_extrahieren(text)
    assert bloecke == [["  StaticTrain (21)"], ["  StaticTrain (22)"]]
    assert rest == ""


def test_bloecke_extrahieren_puffer_wird_beim_naechsten_aufruf_vervollstaendigt():
    header = "3:03:03.231 [CE][DE] DynamicEvent Types (1):"
    teil1 = header + "\n  StaticHeliCrash (3)\n"
    bloecke1, rest1 = bot_mod._ce_events_bloecke_extrahieren(teil1)
    assert bloecke1 == []
    teil2 = rest1 + "3:03:03.241 [CE][InitializeMap] :: init\n"
    bloecke2, rest2 = bot_mod._ce_events_bloecke_extrahieren(teil2)
    assert bloecke2 == [["  StaticHeliCrash (3)"]]
    assert rest2 == ""


# ── Positions-Kandidaten-Pool (Stufe A: echte cfgeventspawns.xml) ─────────

def test_pool_anzahl_aus_xml_zaehlt_pos_kinder():
    root = ET.fromstring(
        '<events><event name="StaticTrain">'
        '<pos x="1" z="2"/><pos x="3" z="4"/>'
        '</event></events>')
    assert bot_mod._ce_events_pool_anzahl_aus_xml(root, "StaticTrain") == 2


def test_pool_anzahl_aus_xml_unbekannter_event_gibt_none():
    root = ET.fromstring('<events><event name="StaticTrain"><pos x="1" z="2"/></event></events>')
    assert bot_mod._ce_events_pool_anzahl_aus_xml(root, "StaticHeliCrash") is None


def test_vanilla_pool_enthaelt_verifizierte_zahlen():
    # Gegengeprueft gegen die offizielle cfgeventspawns.xml (Bohemia-Commit
    # 9a21bb9f5fb9c62a7ce2761402196091588133e6) - siehe Anleitung Abschnitt 4.3.
    assert bot_mod._CE_VANILLA_SPAWN_COUNT["ChernarusPlus"] == {
        "StaticHeliCrash": 95, "StaticMilitaryConvoy": 23,
        "StaticPoliceSituation": 39, "StaticTrain": 21,
    }
    assert bot_mod._CE_VANILLA_SPAWN_COUNT["Livonia"] == {
        "StaticHeliCrash": 79, "StaticMilitaryConvoy": 14,
        "StaticPoliceSituation": 33, "StaticTrain": 14,
    }
    # Sakhal bewusst nicht enthalten - kein verifizierter Datensatz fuer alle
    # vier Typen (Convoy/Police/Train fehlen dort in der offiziellen Datei).
    assert "Sakhal" not in bot_mod._CE_VANILLA_SPAWN_COUNT


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


_ce_events_service_zaehler = [0]


@pytest.fixture
def conn_mit_ce_events(monkeypatch):
    # Jeder Test bekommt einen EIGENEN Service - bot_mod.connections.upsert()
    # gibt bei bekannter service_id die bestehende, bereits benutzte Verbindung
    # zurueck (kein Reset), gemeinsam genutzte IDs wuerden also RPT-Offset und
    # Zaehlerstand zwischen Tests verschleppen.
    _ce_events_service_zaehler[0] += 1
    service_id = f"ceevents-auto-{_ce_events_service_zaehler[0]}"
    bot_mod.connections.upsert(service_id)
    bot_mod.connections.assign_guild(service_id, 6000 + _ce_events_service_zaehler[0])
    conn = bot_mod.connections.for_service(service_id)

    kanal_gesendet = []

    async def fake_post_feed(guild_id, feed_key, embed, service_id=None, anhang=None):
        kanal_gesendet.append({"guild_id": guild_id, "feed_key": feed_key, "embed": embed})
        return True, "ok"
    monkeypatch.setattr(bot_mod, "_post_feed", fake_post_feed)
    # Kein Mission-Ordner konfiguriert -> Positions-Pool faellt auf Stufe
    # B/C zurueck, keine echten FTP-Aufrufe fuer cfgeventspawns.xml noetig.
    return conn, kanal_gesendet


def test_lese_ce_events_erster_zyklus_setzt_nur_cursor_ohne_post(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", _StubLoop()))
    assert gesendet == []
    assert conn.log_state["ce_events"] == {"file": pfad, "offset": 500}


def test_lese_ce_events_postet_bei_anstieg_mit_vanilla_pool(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    bot_mod._conn_store(conn, "map_name", "ChernarusPlus")
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    # Erster Zyklus: Baseline setzen.
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 2
    # Zweiter Zyklus: vollstaendiger Block mit gestiegenem Zaehler ab dem
    # gemerkten Offset.
    conn.ftp.inhalte[(pfad, 500)] = _block_text(["StaticHeliCrash (3)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 1
    assert gesendet[0]["feed_key"] == "ce_events"
    beschreibung = gesendet[0]["embed"].description
    assert "jetzt 3 aktiv" in beschreibung
    assert "Ort: nicht live bestimmbar" in beschreibung
    assert "einer von 95 Vanilla Punkten auf Chernarus" in beschreibung
    assert "Custom-Spawnpunkte" in beschreibung


def test_lese_ce_events_unvollstaendiger_block_wird_nicht_gepostet(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 2
    header = "3:03:03.231 [CE][DE] DynamicEvent Types (1):"
    conn.ftp.inhalte[(pfad, 500)] = header + "\n  StaticHeliCrash (3)\n"
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert gesendet == []  # Block noch nicht abgeschlossen - wartet im Puffer
    assert conn.ce_events_puffer != ""


def test_lese_ce_events_ohne_pool_meldet_ort_unbekannt(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    # Keine Karte mit verifiziertem Vanilla-Pool -> Stufe C.
    bot_mod._conn_store(conn, "map_name", "Sakhal")
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticTrain"] = 0
    conn.ftp.inhalte[(pfad, 500)] = _block_text(["StaticTrain (1)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 1
    assert "Ort unbekannt" in gesendet[0]["embed"].description


def test_lese_ce_events_mehrfacher_anstieg_im_selben_block_wird_zusammengefasst(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticMilitaryConvoy"] = 50
    conn.ftp.inhalte[(pfad, 500)] = _block_text(["StaticMilitaryConvoy (53)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 1
    assert "3× neu aufgetaucht" in gesendet[0]["embed"].description
    assert "jetzt 53 aktiv" in gesendet[0]["embed"].description


def test_lese_ce_events_verwendet_echten_server_pool_wenn_erreichbar(conn_mit_ce_events, monkeypatch):
    conn, gesendet = conn_mit_ce_events
    bot_mod._conn_store(conn, "ftp_mission_dir", "/mission")
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()

    async def fake_xml_lesen(_conn, _dateiname, _loop):
        return ET.fromstring(
            '<events><event name="StaticTrain">'
            '<pos x="1" z="2"/><pos x="3" z="4"/><pos x="5" z="6"/>'
            '</event></events>'), "ok"
    monkeypatch.setattr(bot_mod, "_tools_xml_lesen", fake_xml_lesen)

    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticTrain"] = 0
    conn.ftp.inhalte[(pfad, 500)] = _block_text(["StaticTrain (1)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 1
    beschreibung = gesendet[0]["embed"].description
    assert "einer von 3 serverkonfigurierten Punkten" in beschreibung
    assert "Quelle: aktuelle Server-Mission" in beschreibung


def test_lese_ce_events_deaktivierter_schalter_postet_nicht(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    bot_mod._conn_store(conn, "ce_events_helicopter_crash_enabled", False)
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 2
    conn.ftp.inhalte[(pfad, 500)] = _block_text(["StaticHeliCrash (3)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert gesendet == []


def test_lese_ce_events_rotation_setzt_baseline_und_puffer_zurueck(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    alter_pfad = "/games/x/config/DayZServer_alt.RPT"
    neuer_pfad = "/games/x/config/DayZServer_neu.RPT"
    conn.ftp = _StubFTP([alter_pfad], {alter_pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 3
    conn.ce_events_puffer = "3:03:03.231 [CE][DE] DynamicEvent Types (1):\n  StaticHeliCrash (5)\n"
    # Rotation: neue Datei, DayZ-Zaehler faengt bei 0 wieder an.
    conn.ftp.dateien = [neuer_pfad]
    conn.ftp.groessen[neuer_pfad] = 0
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert conn.ce_event_counts == {}
    assert conn.ce_events_puffer == ""
    assert conn.log_state["ce_events"] == {"file": neuer_pfad, "offset": 0}
    assert gesendet == []
