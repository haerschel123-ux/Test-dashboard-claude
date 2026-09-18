"""Tests fuer den CE-Events-Feed (Heli-Crash, Convoy, Checkpoint, Abandoned
Train, Vehicle Event).

Brigarde wollte einen Feed fuer besondere Central-Economy-Events. Anhand
einer echten RPT-Datei wurde verifiziert: DayZ schreibt KEINE einzelne
"Event ist da"-Zeile, sondern alle 5 Sekunden einen kompletten
"[CE][DE] DynamicEvent Types (N):"-Zaehler-Dump mit ALLEN Event-Typen. Ein
neues Event erkennt man nur daran, dass die Zahl zwischen zwei Bloecken
steigt - das ist, was _ce_events_bloecke_extrahieren/_ce_events_zeilen_auswerten
reproduzieren.

Brigarde meldete danach: "Vehicle Event"-Postings zeigen NIE einen Ort,
obwohl "Helicopter Crash" einen zeigt. Forensische Auswertung einer echten
RPT-Datei (im Zeitfenster eines VehicleBoat-Zaehleranstiegs) hat gezeigt: der
zuvor genutzte "Adding <Item> at [x,z]"-Zeilen-Cluster-Ansatz kann Fahrzeuge
strukturell nicht erfassen (alle "Adding"-Zeilen in diesem Fenster waren
normaler, ueber die ganze Karte verstreuter Loot-Nachschub). Stattdessen gibt
es eine universelle, bisher unbekannte Zeile fuer ALLE CE-Event-Typen
(Static* UND Vehicle*), die die tatsaechlich verwendete Spawn-Position exakt
tragt:

    [CE][DE] [<Typ>] Spawning: EventID:[N] CurrentID:[M] at [x,y,z] a: <winkel>
    	(child) Spawned <Klasse> EventID:[N] CurrentID:[M] at [x,y,z] a: <winkel>

(bei manchen Static-Events "(group) Spawned" statt "(child) Spawned"). Ein
Spawn-Versuch kann mit "spawn refused... too close to another one." abgelehnt
und mit neuer Koordinate unter derselben CurrentID wiederholt werden - die
Bestaetigungszeile traegt immer die tatsaechlich verwendete, finale Position.
Siehe _ce_events_spawn_positionen_erkennen. Das ersetzt den vorherigen
Adding-Cluster-/cfgeventspawns.xml-Abgleich-Mechanismus komplett.

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


# ── Spawn-Positions-Erkennung ueber "Spawning:"/"Spawned"-Zeilenpaare ─────

def test_spawn_positionen_erkennen_grundfall():
    zeilen = [
        "18:42:55.572 [CE][DE] [StaticHeliCrash] Spawning: EventID:[35] CurrentID:[55370] at [1410.0,-1.0,4299.2] a: -1.000",
        "18:42:55.573 	(child) Spawned Wreck_UH1Y EventID:[35] CurrentID:[55370] at [1410.0,-1.0,4299.2] a: -1.000",
    ]
    ergebnisse = bot_mod._ce_events_spawn_positionen_erkennen(zeilen)
    assert ergebnisse == [("StaticHeliCrash", 1410.0, 4299.2)]


def test_spawn_positionen_erkennen_nimmt_finale_position_nach_wiederholungen():
    # "spawn refused"-Wiederholungen aendern die CurrentID nicht - nur die
    # LETZTE "Spawning:"-Zeile vor dem Erfolg traegt die tatsaechlich
    # verwendete Koordinate (die Bestaetigungszeile selbst ist massgeblich).
    zeilen = [
        "18:43:25.571 [CE][DE] [VehicleBoat] Spawning: EventID:[46] CurrentID:[55375] at [6160.1,-1.0,2011.1] a: -1.000",
        "18:43:25.571 	spawn refused... too close to another one.",
        "18:43:25.571 [CE][DE] [VehicleBoat] Spawning: EventID:[46] CurrentID:[55375] at [6158.0,-1.0,2053.3] a: -1.000",
        "18:43:25.572 	(child) Spawned Boat_01_Blue EventID:[46] CurrentID:[55375] at [6158.0,-1.0,2053.3] a: 10.000",
    ]
    ergebnisse = bot_mod._ce_events_spawn_positionen_erkennen(zeilen)
    assert ergebnisse == [("VehicleBoat", 6158.0, 2053.3)]


def test_spawn_positionen_erkennen_zwei_typen_gleichzeitig_nicht_vertauscht():
    zeilen = [
        "18:43:25.567 [CE][DE] [VehicleBoat] Spawning: EventID:[46] CurrentID:[55371] at [13359.4,-1.0,10709.6] a: -1.000",
        "18:42:55.572 [CE][DE] [StaticHeliCrash] Spawning: EventID:[35] CurrentID:[55370] at [1410.0,-1.0,4299.2] a: -1.000",
        "18:43:25.568 	(child) Spawned Boat_01_Orange EventID:[46] CurrentID:[55371] at [13359.5,-0.0,10709.4] a: 10.585",
        "18:42:55.573 	(child) Spawned Wreck_UH1Y EventID:[35] CurrentID:[55370] at [1410.0,-1.0,4299.2] a: -1.000",
    ]
    ergebnisse = bot_mod._ce_events_spawn_positionen_erkennen(zeilen)
    assert ergebnisse == [
        ("VehicleBoat", 13359.5, 10709.4),
        ("StaticHeliCrash", 1410.0, 4299.2),
    ]


def test_spawn_positionen_erkennen_spawned_ohne_vorherige_spawning_wird_ignoriert():
    # Simuliert einen Poll-Zyklus, der mitten in einem Zeilen-Paar
    # abgeschnitten hat - die "Spawning:"-Zeile fehlt im gesehenen Puffer.
    zeilen = [
        "18:42:55.573 	(child) Spawned Wreck_UH1Y EventID:[35] CurrentID:[55370] at [1410.0,-1.0,4299.2] a: -1.000",
    ]
    assert bot_mod._ce_events_spawn_positionen_erkennen(zeilen) == []


def test_spawn_positionen_erkennen_group_spawned_wird_ebenfalls_erkannt():
    # Manche Static-Events (z. B. StaticPoliceSituation) bestaetigen mit
    # "(group) Spawned" statt "(child) Spawned".
    zeilen = [
        "19:00:00.000 [CE][DE] [StaticPoliceSituation] Spawning: EventID:[10] CurrentID:[1] at [100.0,-1.0,200.0] a: 0.000",
        "19:00:00.001 	(group) Spawned CivilianSedan EventID:[10] CurrentID:[1] at [100.0,-1.0,200.0] a: 0.000",
    ]
    ergebnisse = bot_mod._ce_events_spawn_positionen_erkennen(zeilen)
    assert ergebnisse == [("StaticPoliceSituation", 100.0, 200.0)]


# ── Die drei zusaetzlichen, serverseitig noch inaktiven Event-Typen ───────

def test_neue_typen_sind_eigenen_schaltern_zugeordnet():
    assert bot_mod._CE_EVENT_TYP_ZU_SCHALTER["StaticContaminatedArea"] == "contaminated_zone"
    assert bot_mod._CE_EVENT_TYP_ZU_SCHALTER["StaticAirplaneCrate"] == "airplane_crate"
    assert bot_mod._CE_EVENT_TYP_ZU_SCHALTER["StaticSantaCrash"] == "santa_crash"
    for schalter in ("contaminated_zone", "airplane_crate", "santa_crash"):
        emoji, name = bot_mod._CE_EVENT_SCHALTER_LABEL[schalter]
        assert emoji and name


def test_neue_typen_werden_im_zaehler_dump_erkannt():
    counts = {"StaticContaminatedArea": 0, "StaticAirplaneCrate": 1, "StaticSantaCrash": 0}
    zeilen = [
        "  StaticContaminatedArea (2)",
        "  StaticAirplaneCrate (3)",
        "  StaticSantaCrash (1)",
    ]
    ergebnisse = bot_mod._ce_events_zeilen_auswerten(zeilen, counts)
    assert ergebnisse == [
        ("StaticContaminatedArea", "contaminated_zone", 2, 2),
        ("StaticAirplaneCrate", "airplane_crate", 2, 3),
        ("StaticSantaCrash", "santa_crash", 1, 1),
    ]




# ── Sub-Toggle-Einstellungen (Speichern/Laden) ───────────────────────────

def test_ce_events_payload_default_alle_aktiv():
    bot_mod.connections.upsert("ceevents-service-1")
    bot_mod.connections.assign_guild("ceevents-service-1", 5001)
    conn = bot_mod.connections.for_service("ceevents-service-1")
    payload = bot_mod._ce_events_payload(conn)
    assert payload == {
        "helicopter_crash": True, "convoy": True, "checkpoint": True,
        "abandoned_train": True, "vehicle_event": True,
        "contaminated_zone": True, "airplane_crate": True, "santa_crash": True,
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
    return conn, kanal_gesendet


def test_lese_ce_events_erster_zyklus_setzt_nur_cursor_ohne_post(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", _StubLoop()))
    assert gesendet == []
    assert conn.log_state["ce_events"] == {"file": pfad, "offset": 500}


def test_lese_ce_events_static_helicrash_zeigt_exakte_geloggte_position(conn_mit_ce_events):
    # Echte Zeilen aus Brigardes RPT-Datei (Zeile 36706 des Originals).
    conn, gesendet = conn_mit_ce_events
    bot_mod._conn_store(conn, "map_name", "ChernarusPlus")
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 2
    spawn_zeilen = (
        "18:42:55.572 [CE][DE] [StaticHeliCrash] Spawning: EventID:[35] CurrentID:[55370] at [1410.0,-1.0,4299.2] a: -1.000\n"
        "18:42:55.573 \t(child) Spawned Wreck_UH1Y EventID:[35] CurrentID:[55370] at [1410.0,-1.0,4299.2] a: -1.000\n"
    )
    conn.ftp.inhalte[(pfad, 500)] = spawn_zeilen + _block_text(["StaticHeliCrash (3)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 1
    beschreibung = gesendet[0]["embed"].description
    assert "jetzt 3 aktiv" in beschreibung
    assert "📍 [1410 / 4299]" in beschreibung
    assert "izurvive.com" in beschreibung
    assert conn.ce_pending_positionen["StaticHeliCrash"] == []  # Treffer verbraucht


def test_lese_ce_events_vehicleboat_zeigt_jetzt_ebenfalls_eine_position(conn_mit_ce_events):
    # Genau das gemeldete Problem: Vehicle-Events zeigten NIE einen Ort. Echte
    # Zeilen aus Brigardes RPT-Datei (CurrentID:[55371], Zeile 37080/37081).
    conn, gesendet = conn_mit_ce_events
    bot_mod._conn_store(conn, "map_name", "ChernarusPlus")
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["VehicleBoat"] = 0
    spawn_zeilen = (
        "18:43:25.567 [CE][DE] [VehicleBoat] Spawning: EventID:[46] CurrentID:[55371] at [13359.4,-1.0,10709.6] a: -1.000\n"
        "18:43:25.568 \t(child) Spawned Boat_01_Orange EventID:[46] CurrentID:[55371] at [13359.5,-0.0,10709.4] a: 10.585\n"
    )
    conn.ftp.inhalte[(pfad, 500)] = spawn_zeilen + _block_text(["VehicleBoat (1)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 1
    beschreibung = gesendet[0]["embed"].description
    assert "jetzt 1 aktiv" in beschreibung
    assert "📍 [13360 / 10709]" in beschreibung  # x/z gerundet, KEIN Mittelwert
    assert "izurvive.com" in beschreibung
    assert conn.ce_pending_positionen["VehicleBoat"] == []


def test_lese_ce_events_ohne_spawn_zeile_zeigt_keinen_ort(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticTrain"] = 0
    conn.ftp.inhalte[(pfad, 500)] = _block_text(["StaticTrain (1)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 1
    beschreibung = gesendet[0]["embed"].description
    assert "📍" not in beschreibung
    assert "jetzt 1 aktiv" in beschreibung


def test_lese_ce_events_zwei_gleichzeitige_typen_nicht_vertauscht(conn_mit_ce_events):
    # Zwei Events verschiedener Typen im selben Poll-Zyklus - die Zuordnung
    # ueber CurrentID/Typname darf sie nicht vertauschen.
    conn, gesendet = conn_mit_ce_events
    bot_mod._conn_store(conn, "map_name", "ChernarusPlus")
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 0
    conn.ce_event_counts["VehicleBoat"] = 0
    spawn_zeilen = (
        "18:42:55.572 [CE][DE] [StaticHeliCrash] Spawning: EventID:[35] CurrentID:[55370] at [1410.0,-1.0,4299.2] a: -1.000\n"
        "18:42:55.573 \t(child) Spawned Wreck_UH1Y EventID:[35] CurrentID:[55370] at [1410.0,-1.0,4299.2] a: -1.000\n"
        "18:43:25.567 [CE][DE] [VehicleBoat] Spawning: EventID:[46] CurrentID:[55371] at [13359.4,-1.0,10709.6] a: -1.000\n"
        "18:43:25.568 \t(child) Spawned Boat_01_Orange EventID:[46] CurrentID:[55371] at [13359.5,-0.0,10709.4] a: 10.585\n"
    )
    text = spawn_zeilen + _block_text(["StaticHeliCrash (1)", "VehicleBoat (1)"])
    conn.ftp.inhalte[(pfad, 500)] = text
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 2
    assert "📍 [1410 / 4299]" in gesendet[0]["embed"].description
    assert "📍 [13360 / 10709]" in gesendet[1]["embed"].description
    assert conn.ce_pending_positionen["StaticHeliCrash"] == []
    assert conn.ce_pending_positionen["VehicleBoat"] == []


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


def test_lese_ce_events_rotation_setzt_baseline_puffer_und_positionen_zurueck(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    alter_pfad = "/games/x/config/DayZServer_alt.RPT"
    neuer_pfad = "/games/x/config/DayZServer_neu.RPT"
    conn.ftp = _StubFTP([alter_pfad], {alter_pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 3
    conn.ce_events_puffer = "3:03:03.231 [CE][DE] DynamicEvent Types (1):\n  StaticHeliCrash (5)\n"
    conn.ce_pending_positionen = {"StaticHeliCrash": [(1.0, 2.0)]}
    # Rotation: neue Datei, DayZ-Zaehler faengt bei 0 wieder an.
    conn.ftp.dateien = [neuer_pfad]
    conn.ftp.groessen[neuer_pfad] = 0
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert conn.ce_event_counts == {}
    assert conn.ce_events_puffer == ""
    assert conn.ce_pending_positionen == {}
    assert conn.log_state["ce_events"] == {"file": neuer_pfad, "offset": 0}
    assert gesendet == []


def test_lese_ce_events_inaktiver_typ_ohne_zaehlerzeile_bleibt_stumm(conn_mit_ce_events):
    # Genau Brigardes Fall: die drei neuen Typen sind auf ihrem Server (noch)
    # nicht aktiviert und tauchen deshalb im Dump gar nicht auf - der Feed
    # darf dadurch weder etwas melden noch anderweitig stolpern.
    conn, gesendet = conn_mit_ce_events
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 3
    conn.ftp.inhalte[(pfad, 500)] = _block_text(["StaticHeliCrash (3)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert gesendet == []
    assert "StaticSantaCrash" not in conn.ce_event_counts


def test_lese_ce_events_neuer_typ_wird_nach_aktivierung_gemeldet(conn_mit_ce_events):
    # Sobald Brigarde den Typ in der events.xml aktiviert, taucht er im Dump
    # auf und der zugehoerige Schalter meldet ihn - ohne Code-Aenderung.
    conn, gesendet = conn_mit_ce_events
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticContaminatedArea"] = 0
    conn.ftp.inhalte[(pfad, 500)] = _block_text(["StaticContaminatedArea (1)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 1
    assert "Contaminated Zone" in gesendet[0]["embed"].title
    assert "jetzt 1 aktiv" in gesendet[0]["embed"].description


def test_lese_ce_events_deaktivierter_neuer_schalter_postet_nicht(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    bot_mod._conn_store(conn, "ce_events_santa_crash_enabled", False)
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticSantaCrash"] = 0
    conn.ftp.inhalte[(pfad, 500)] = _block_text(["StaticSantaCrash (1)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert gesendet == []
