"""Tests fuer den CE-Events-Feed (Heli-Crash, Convoy, Checkpoint, Abandoned
Train, Vehicle Event).

Brigarde wollte einen Feed fuer besondere Central-Economy-Events. Anhand
einer echten RPT-Datei wurde verifiziert: DayZ schreibt KEINE einzelne
"Event ist da"-Zeile, sondern alle 5 Sekunden einen kompletten
"[CE][DE] DynamicEvent Types (N):"-Zaehler-Dump mit ALLEN Event-Typen. Ein
neues Event erkennt man nur daran, dass die Zahl zwischen zwei Bloecken
steigt - das ist, was _ce_events_bloecke_extrahieren/_ce_events_zeilen_auswerten
reproduzieren.

Eine anschliessende, forensische Auswertung mehrerer echter RPT-Dateien hat
zusaetzlich bestaetigt: DayZ schreibt sehr wohl echte Positionen ins Log -
nicht als eigene "Event hier"-Zeile, aber als "  Adding <Item> at [x,z]"
zum Zeitpunkt, in dem die Ladung eines Events entsteht (siehe
_ce_events_adding_cluster_erkennen). Mehrere solcher Zeilen in enger
zeitlicher/raeumlicher Naehe markieren OFT den Spawn-Moment eines Events -
koennen aber auch ein ganz normaler, zufaellig zeitgleicher Loot-Nachschub in
der Naehe sein (live beobachtet: fuehrte zu falschen Ortsangaben). Ein
erkannter Cluster wird deshalb zusaetzlich gegen die ECHTEN <pos>-Punkte des
gemeldeten Event-Typs in der cfgeventspawns.xml DIESES Servers geprueft
(siehe _ce_events_position_zuordnen/_ce_events_pool_laden) - nur ein
Treffer nah an einem bekannten Spawnpunkt GENAU dieses Typs gilt als
verifiziert und wird angezeigt (wie beim bestehenden Killfeed: iZurvive-Link
+ _nearest_location). Ohne erreichbare cfgeventspawns.xml oder ohne
passenden Treffer bleibt die Ortszeile schlicht weg - keine Vermutung.

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


# ── "Adding"-Zeilen-Cluster-Erkennung (echte, verifizierte Positionen) ────

def test_adding_cluster_erkennt_eng_gruppierte_zeilen():
    zeilen = [
        "12:46:25.995 Adding Ammo_9x39 at [6200,8300]",
        "12:46:25.996 Adding Ammo_9x39 at [6205,8303]",
        "12:46:25.998 Adding Wreck_UH1Y at [6202,8306]",
    ]
    cluster = bot_mod._ce_events_adding_cluster_erkennen(zeilen)
    assert len(cluster) == 1
    x, z, anzahl = cluster[0]
    assert anzahl == 3
    assert round(x, 2) == round((6200 + 6205 + 6202) / 3, 2)
    assert round(z, 2) == round((8300 + 8303 + 8306) / 3, 2)


def test_adding_cluster_ignoriert_zu_kleine_gruppe():
    # Unter _CE_ADDING_CLUSTER_MIN_ITEMS (3) zaehlt es nicht als Cluster -
    # sonst waere jede einzelne, gewoehnliche Loot-Nachschub-Zeile ein
    # "verifiziertes" Event.
    zeilen = [
        "12:46:25.995 Adding Ammo_9x39 at [100,100]",
        "12:46:25.996 Adding Ammo_9x39 at [101,101]",
    ]
    assert bot_mod._ce_events_adding_cluster_erkennen(zeilen) == []


def test_adding_cluster_trennt_bei_grosser_zeitluecke():
    zeilen = [
        "12:00:00.000 Adding A at [0,0]",
        "12:00:00.100 Adding A at [1,1]",
        "12:00:00.200 Adding A at [2,2]",
        "12:00:05.000 Adding A at [500,500]",
        "12:00:05.100 Adding A at [501,501]",
        "12:00:05.200 Adding A at [502,502]",
    ]
    cluster = bot_mod._ce_events_adding_cluster_erkennen(zeilen)
    assert len(cluster) == 2


def test_adding_cluster_trennt_bei_grosser_raeumlicher_distanz():
    zeilen = [
        "12:00:00.000 Adding A at [0,0]",
        "12:00:00.100 Adding A at [1,1]",
        "12:00:00.200 Adding A at [2,2]",
        "12:00:00.300 Adding A at [5000,5000]",
        "12:00:00.400 Adding A at [5001,5001]",
        "12:00:00.500 Adding A at [5002,5002]",
    ]
    cluster = bot_mod._ce_events_adding_cluster_erkennen(zeilen)
    assert len(cluster) == 2


def test_adding_cluster_ignoriert_nicht_passende_zeilen():
    zeilen = [
        "irgendein RPT-Text",
        "12:00:00.000 Adding A at [10,10]",
        "3:03:03.231 [CE][DE] DynamicEvent Types (56):",
        "12:00:00.100 Adding A at [11,11]",
        "12:00:00.200 Adding A at [12,12]",
    ]
    cluster = bot_mod._ce_events_adding_cluster_erkennen(zeilen)
    assert len(cluster) == 1


# ── Pool-Abgleich gegen die echte cfgeventspawns.xml ──────────────────────

def test_positionen_aus_xml_liest_x_z():
    root = bot_mod.ET.fromstring(
        '<eventposdef><event name="StaticTrain">'
        '<pos x="1" z="2"/><pos x="3" z="4"/>'
        '</event></eventposdef>')
    assert bot_mod._ce_events_positionen_aus_xml(root, "StaticTrain") == [(1.0, 2.0), (3.0, 4.0)]


def test_positionen_aus_xml_unbekannter_typ_gibt_none():
    root = bot_mod.ET.fromstring(
        '<eventposdef><event name="StaticTrain"><pos x="1" z="2"/></event></eventposdef>')
    assert bot_mod._ce_events_positionen_aus_xml(root, "StaticHeliCrash") is None


def test_position_zuordnen_ohne_pool_gibt_none():
    conn = bot_mod.connections.upsert("ceevents-pool-1")
    conn.ce_pending_positionen = [(6202.0, 8303.0)]
    assert bot_mod._ce_events_position_zuordnen(conn, "StaticHeliCrash", None) is None
    # Kandidat bleibt erhalten - kein Pool heisst nicht "verworfen".
    assert conn.ce_pending_positionen == [(6202.0, 8303.0)]


def test_position_zuordnen_findet_nahen_bekannten_punkt():
    conn = bot_mod.connections.upsert("ceevents-pool-2")
    # Echte Koordinate aus Brigardes cfgeventspawns.xml (StaticHeliCrash).
    root = bot_mod.ET.fromstring(
        '<eventposdef><event name="StaticHeliCrash">'
        '<pos x="6204.925293" z="8301.290039" a="-1"/>'
        '</event></eventposdef>')
    conn.ce_pending_positionen = [(6202.0, 8303.0)]
    treffer = bot_mod._ce_events_position_zuordnen(conn, "StaticHeliCrash", root)
    assert treffer == (6202.0, 8303.0)
    assert conn.ce_pending_positionen == []  # nur der Treffer wird entfernt


def test_position_zuordnen_lehnt_zu_weit_entfernten_kandidaten_ab():
    # Simuliert genau den gemeldeten Fehler: ein Cluster, das zufaellig
    # zeitgleich mit dem Zaehler-Anstieg auftrat, aber tatsaechlich ein
    # normaler Loot-Nachschub weit weg vom naechsten echten Spawnpunkt war.
    conn = bot_mod.connections.upsert("ceevents-pool-3")
    root = bot_mod.ET.fromstring(
        '<eventposdef><event name="StaticHeliCrash">'
        '<pos x="6204.925293" z="8301.290039" a="-1"/>'
        '</event></eventposdef>')
    conn.ce_pending_positionen = [(2000.0, 2000.0)]
    treffer = bot_mod._ce_events_position_zuordnen(conn, "StaticHeliCrash", root)
    assert treffer is None
    # Der abgelehnte Kandidat bleibt in der Warteschlange - er koennte noch
    # zu einem anderen, spaeter gemeldeten Typ passen.
    assert conn.ce_pending_positionen == [(2000.0, 2000.0)]


def test_position_zuordnen_ueberspringt_nicht_passende_und_nimmt_naechsten():
    conn = bot_mod.connections.upsert("ceevents-pool-4")
    root = bot_mod.ET.fromstring(
        '<eventposdef><event name="StaticTrain">'
        '<pos x="5587.47" z="2063.35" a="0"/>'
        '</event></eventposdef>')
    conn.ce_pending_positionen = [(9999.0, 9999.0), (5588.0, 2064.0)]
    treffer = bot_mod._ce_events_position_zuordnen(conn, "StaticTrain", root)
    assert treffer == (5588.0, 2064.0)
    assert conn.ce_pending_positionen == [(9999.0, 9999.0)]


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
    return conn, kanal_gesendet


def test_lese_ce_events_erster_zyklus_setzt_nur_cursor_ohne_post(conn_mit_ce_events):
    conn, gesendet = conn_mit_ce_events
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", _StubLoop()))
    assert gesendet == []
    assert conn.log_state["ce_events"] == {"file": pfad, "offset": 500}


def _pool_bereitstellen(conn, monkeypatch, xml_text):
    """Simuliert eine erreichbare, echte cfgeventspawns.xml dieses Servers -
    ohne diesen Aufruf bleibt conn.ce_eventspawns_root False (kein
    Mission-Ordner konfiguriert) und JEDE Ortsangabe faellt weg, egal wie gut
    ein Cluster passen wuerde."""
    bot_mod._conn_store(conn, "ftp_mission_dir", "/mission")

    async def fake_xml_lesen(_conn, _dateiname, _loop):
        return bot_mod.ET.fromstring(xml_text), "ok"
    monkeypatch.setattr(bot_mod, "_tools_xml_lesen", fake_xml_lesen)


def test_lese_ce_events_nutzt_echten_adding_cluster_der_zum_pool_passt(conn_mit_ce_events, monkeypatch):
    conn, gesendet = conn_mit_ce_events
    bot_mod._conn_store(conn, "map_name", "ChernarusPlus")
    _pool_bereitstellen(conn, monkeypatch, (
        '<eventposdef><event name="StaticHeliCrash">'
        '<pos x="6204.925293" z="8301.290039" a="-1"/>'
        '</event></eventposdef>'))
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 2
    adding_zeilen = (
        "12:46:25.995 Adding Ammo_9x39 at [6200,8300]\n"
        "12:46:25.996 Adding Ammo_9x39 at [6205,8303]\n"
        "12:46:25.998 Adding Wreck_UH1Y at [6202,8306]\n"
    )
    conn.ftp.inhalte[(pfad, 500)] = adding_zeilen + _block_text(["StaticHeliCrash (3)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 1
    beschreibung = gesendet[0]["embed"].description
    assert "jetzt 3 aktiv" in beschreibung
    assert "📍 [6202 / 8303]" in beschreibung
    assert "izurvive.com" in beschreibung
    assert conn.ce_pending_positionen == []  # Treffer verbraucht


def test_lese_ce_events_ohne_adding_cluster_zeigt_keinen_ort(conn_mit_ce_events):
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


def test_lese_ce_events_cluster_ohne_erreichbaren_pool_zeigt_keinen_ort(conn_mit_ce_events):
    # Kein ftp_mission_dir konfiguriert -> conn.ce_eventspawns_root bleibt
    # False, obwohl ein plausibler Cluster vorliegt. Ohne echte Datei keine
    # Ortsangabe - kein Rateraten.
    conn, gesendet = conn_mit_ce_events
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 2
    adding_zeilen = ("12:46:25.995 Adding A at [6200,8300]\n"
                     "12:46:25.996 Adding A at [6205,8303]\n"
                     "12:46:25.998 Adding A at [6202,8306]\n")
    conn.ftp.inhalte[(pfad, 500)] = adding_zeilen + _block_text(["StaticHeliCrash (3)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 1
    assert "📍" not in gesendet[0]["embed"].description
    # Der Kandidat bleibt fuer einen spaeteren Poll-Zyklus (Pool koennte dann
    # erreichbar sein) in der Warteschlange stehen.
    assert len(conn.ce_pending_positionen) == 1
    x, z = conn.ce_pending_positionen[0]
    assert round(x, 2) == round((6200 + 6205 + 6202) / 3, 2)
    assert z == 8303.0


def test_lese_ce_events_cluster_weit_weg_von_bekanntem_typ_zeigt_keinen_ort(conn_mit_ce_events, monkeypatch):
    # Genau der gemeldete Fehler: ein Cluster faellt rein zeitlich mit dem
    # Zaehler-Anstieg zusammen, liegt aber weit weg vom einzigen bekannten
    # Spawnpunkt dieses Typs - also vermutlich normaler Loot-Nachschub, kein
    # echter Event-Ort.
    conn, gesendet = conn_mit_ce_events
    _pool_bereitstellen(conn, monkeypatch, (
        '<eventposdef><event name="StaticHeliCrash">'
        '<pos x="6204.925293" z="8301.290039" a="-1"/>'
        '</event></eventposdef>'))
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 2
    adding_zeilen = ("12:46:25.995 Adding Ammo_9x39 at [100,100]\n"
                     "12:46:25.996 Adding Ammo_9x39 at [101,101]\n"
                     "12:46:25.998 Adding Ammo_9x39 at [102,102]\n")
    conn.ftp.inhalte[(pfad, 500)] = adding_zeilen + _block_text(["StaticHeliCrash (3)"])
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 1
    assert "📍" not in gesendet[0]["embed"].description
    assert "jetzt 3 aktiv" in gesendet[0]["embed"].description


def test_lese_ce_events_mehrere_cluster_richtigem_typ_zugeordnet(conn_mit_ce_events, monkeypatch):
    # Zwei Events im selben Zyklus, zwei Cluster - der Pool-Abgleich (nicht
    # die zeitliche Reihenfolge) entscheidet, welcher Cluster zu welchem Typ
    # gehoert.
    conn, gesendet = conn_mit_ce_events
    _pool_bereitstellen(conn, monkeypatch, (
        '<eventposdef>'
        '<event name="StaticHeliCrash"><pos x="101" z="101" a="-1"/></event>'
        '<event name="StaticTrain"><pos x="901" z="901" a="0"/></event>'
        '</eventposdef>'))
    pfad = "/games/x/config/DayZServer_x.RPT"
    conn.ftp = _StubFTP([pfad], {pfad: 500}, {})
    loop = _StubLoop()
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    conn.ce_event_counts["StaticHeliCrash"] = 0
    conn.ce_event_counts["StaticTrain"] = 0
    cluster_a = ("1:00:00.000 Adding A at [100,100]\n"
                "1:00:00.100 Adding A at [101,101]\n"
                "1:00:00.200 Adding A at [102,102]\n")
    cluster_b = ("1:00:10.000 Adding B at [900,900]\n"
                "1:00:10.100 Adding B at [901,901]\n"
                "1:00:10.200 Adding B at [902,902]\n")
    text = (cluster_a + _block_text(["StaticHeliCrash (1)"])
           + cluster_b + _block_text(["StaticTrain (1)"]))
    conn.ftp.inhalte[(pfad, 500)] = text
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert len(gesendet) == 2
    assert "📍 [101 / 101]" in gesendet[0]["embed"].description
    assert "📍 [901 / 901]" in gesendet[1]["embed"].description
    assert conn.ce_pending_positionen == []


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
    conn.ce_pending_positionen = [(1.0, 2.0)]
    # Rotation: neue Datei, DayZ-Zaehler faengt bei 0 wieder an.
    conn.ftp.dateien = [neuer_pfad]
    conn.ftp.groessen[neuer_pfad] = 0
    _run(bot_mod.bot._lese_ce_events(conn, "/games/x/config", loop))
    assert conn.ce_event_counts == {}
    assert conn.ce_events_puffer == ""
    assert conn.ce_pending_positionen == []
    assert conn.log_state["ce_events"] == {"file": neuer_pfad, "offset": 0}
    assert gesendet == []
