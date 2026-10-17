"""Neue Karte Nasdara (DayZ 1.30 Badlands, Mission-Ordner dayzOffline.nasdara).

Weltgroesse, Orte und Kachelquelle sind inoffiziell (Bohemia hat nichts
veroeffentlicht) - siehe Kommentare in bot.py.

    python3 -m pytest tests/test_map_nasdara.py -v
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


@pytest.mark.parametrize("roh", ["dayzOffline.nasdara", "nasdara", "Nasdara", "DAYZOFFLINE.NASDARA"])
def test_kanonischer_name(roh):
    assert bot._canonical_map_name(roh) == "Nasdara"


def test_mission_ordner_und_erkennung_aus_nitrado_daten():
    assert bot._mission_folder_for_map("Nasdara") == "dayzOffline.nasdara"
    info = {"query": {"map": "dayzOffline.nasdara"}}
    assert bot.NitradoAPI.extract_map(info) == "Nasdara"
    info = {"query": {}, "settings": {"config": {"mission": "dayzOffline.nasdara"}}}
    assert bot.NitradoAPI.extract_map(info) == "Nasdara"


def test_bestehende_karten_unveraendert():
    assert bot._canonical_map_name("dayzOffline.sakhal") == "Sakhal"
    assert bot._canonical_map_name("dayzOffline.enoch") == "Livonia"
    assert bot._canonical_map_name("dayzOffline.chernarusplus") == "ChernarusPlus"
    assert bot._mission_folder_for_map("Sakhal") == "dayzOffline.sakhal"


def test_weltgroesse(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {})
    assert bot._world_size("Nasdara") == 16384
    monkeypatch.setattr(bot.cfg, "config", {"dashboard_map_sizes": {"Nasdara": 20480}})
    assert bot._world_size("Nasdara") == 20480          # Korrektur per config.json bleibt moeglich


def test_orte_liegen_in_der_welt(monkeypatch):
    monkeypatch.setattr(bot, "_locations_cache", {})
    orte = bot._locations("Nasdara")
    assert len(orte) == 101
    assert all(0 <= o["x"] <= 16384 and 0 <= o["z"] <= 16384 for o in orte)
    assert {o["t"] for o in orte} >= {"capital", "city", "village", "local", "camp"}
    # Eine bekannte Stadt: Dashtabad (nasdaramap.com)
    d = next(o for o in orte if o["name"] == "Dashtabad")
    assert (round(d["x"]), round(d["z"])) == (10441, 10669)
    assert next(o for o in orte if o["name"] == "Nasdara")["t"] == "capital"


def test_naechster_ort_fuer_feed_texte():
    assert bot._nearest_location(10441, 10669, "Nasdara") == "Dashtabad"
    assert bot._nearest_location(100, 100, "Nasdara") is None       # weiter als 1500 m von jeder Stadt


def test_kacheln_satellit_und_papierkarte(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {})
    sat = bot._tile_url("Nasdara", "sat")
    topo = bot._tile_url("Nasdara", "topo")
    assert sat != topo and "nasdara_paper" in topo and "nasdara_paper" not in sat
    for url in (sat, topo):
        assert url.startswith("https://") and all(p in url for p in ("{z}", "{x}", "{y}"))
    assert bot._MAP_MAX_NATIVE_ZOOM["Nasdara"] == {"sat": 6, "topo": 4}
    # Faction-Map (Zoom 2) baut daraus genau die Kachel-Adressen
    assert "/2/1/3.webp" in sat.format(z=2, x=1, y=3)
    # andere Karten behalten die xam-Kacheln
    assert "static.xam.nu/dayz/maps/sakhal/" in bot._tile_url("Sakhal", "topo")


def test_kachel_override_gewinnt(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {"dashboard_map_tiles": {"Nasdara": "https://example.invalid/{z}/{x}/{y}.png"}})
    assert bot._tile_url("Nasdara", "topo") == "https://example.invalid/{z}/{x}/{y}.png"


def test_kartenlink_xam(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {"map_link_provider": "xam"})
    url = bot._karten_link(10655, 10792, "Nasdara")
    assert url.startswith("https://dayz.xam.nu/nasdara/") and "#location=10655;10792" in url


@pytest.mark.parametrize("stil", ["sat", "topo", "tourist"])
def test_kartenlink_izurvive_hat_nasdara_nur_als_satellit(monkeypatch, stil):
    monkeypatch.setattr(bot.cfg, "config", {"map_link_provider": "izurvive", "map_link_style": stil})
    assert bot._karten_link(10655, 10792, "Nasdara") == \
        "https://www.izurvive.com/nasdaraSat/#location=10655;10792;0"


def test_izurvive_fuer_andere_karten_unveraendert(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {"map_link_provider": "izurvive"})
    assert bot._karten_link(1, 2, "Sakhal").startswith("https://www.izurvive.com/sakhalSat/")


# ── Erkennung und Anzeige durchgehend (Nitrado-Daten -> Verbindung -> Dashboard) ──
import asyncio
import json
import time

from aiohttp.test_utils import make_mocked_request


def _conn_mit_session(monkeypatch, sid_name):
    conn = bot.connections.upsert(sid_name, nitrado_token="fake", owner_discord_id="1")
    sid = f"nasdara-sid-{time.time_ns()}"
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": "acct-nasdara"}, "is_admin": True,
                            "service_id": conn.service_id, "seen": time.time()}
    req = make_mocked_request("GET", "/api/map/meta", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    return conn, req


def test_erkennung_aus_nitrado_infos_bis_zur_dashboard_karte(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {})
    conn, req = _conn_mit_session(monkeypatch, "nasdara-e2e")
    conn.data.pop("map_name", None)
    info = {"query": {"map": "dayzOffline.nasdara", "server_name": "Test"}, "ip": "1.2.3.4"}
    bot._apply_gameserver_info(info, conn)
    assert conn.data["map_name"] == "Nasdara"                   # Anzeige in Kopfzeile/Uebersicht
    resp = asyncio.run(bot.api_map_meta(req))
    assert resp.status == 200
    d = json.loads(resp.body.decode("utf-8"))["data"]
    assert d["map_name"] == "Nasdara" and d["world_size"] == 16384
    assert d["tile_url"].startswith("https://nasdaramap.com/tiles/nasdara") and d["tile_max_native_zoom"] == 6
    assert len(d["locations"]) == 101
    assert d["izurvive"] == "https://www.izurvive.com/nasdaraSat/"


def test_erkennung_ueber_mission_einstellung_und_ordnersuche(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {})
    conn, _req = _conn_mit_session(monkeypatch, "nasdara-e2e-2")
    conn.data.pop("map_name", None)
    info = {"query": {}, "settings": {"config": {"mission": "dayzOffline.nasdara"}}}   # Server offline
    bot._apply_gameserver_info(info, conn)
    assert conn.data["map_name"] == "Nasdara"
    assert bot._mission_folder_for_map(conn.data["map_name"]) == "dayzOffline.nasdara"


# ── Kartenwechsel nach dem Auswaehlen (Livonia -> Nasdara) ──────────────
def test_kartenwechsel_wird_nachgezogen_inklusive_mission_ordner(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {})
    conn, _req = _conn_mit_session(monkeypatch, "nasdara-wechsel")
    conn.data["map_name"] = "Livonia"
    conn.data["ftp_mission_dir"] = "/dayzps/mpmissions/dayzOffline.enoch"
    assert bot._karte_nachziehen(conn, "dayzOffline.nasdara") is True
    assert conn.data["map_name"] == "Nasdara"
    assert conn.data["ftp_mission_dir"] == "/dayzps/mpmissions/dayzOffline.nasdara"
    assert bot._karte_nachziehen(conn, "dayzOffline.nasdara") is False       # nichts mehr zu tun


def test_kartenwechsel_laesst_eigene_pfade_und_unbekanntes_in_ruhe(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {})
    conn, _req = _conn_mit_session(monkeypatch, "nasdara-wechsel-2")
    conn.data["map_name"] = "Livonia"
    conn.data["ftp_mission_dir"] = "/eigener/ordner/mission"
    assert bot._karte_nachziehen(conn, "dayzOffline.nasdara") is True
    assert conn.data["ftp_mission_dir"] == "/eigener/ordner/mission"
    assert bot._karte_nachziehen(conn, "irgendwas-unbekanntes") is False
    assert bot._karte_nachziehen(conn, "") is False
    assert bot._karte_nachziehen(None, "nasdara") is False
    assert conn.data["map_name"] == "Nasdara"


def test_status_abfrage_zieht_karte_nach(monkeypatch):
    """Die Kopfzeile fragt /api/server/status regelmaessig - dort wird die Karte korrigiert."""
    monkeypatch.setattr(bot.cfg, "config", {})
    conn, _req = _conn_mit_session(monkeypatch, "nasdara-status")
    conn.data["map_name"] = "Livonia"
    conn.data["ftp_mission_dir"] = "/m/dayzOffline.enoch"

    class _Api:
        service_id = "1"

        async def get_info(self):
            return {"status": "started", "query": {"map": "dayzOffline.nasdara", "player_current": 3,
                                                   "player_max": 20}}
    conn.api = _Api()
    req = make_mocked_request("GET", "/api/server/status",
                              headers={"Cookie": _req.headers["Cookie"]})
    resp = asyncio.run(bot.api_server_status(req))
    d = json.loads(resp.body.decode("utf-8"))["data"]
    assert d["map_name"] == "Nasdara"
    assert conn.data["ftp_mission_dir"] == "/m/dayzOffline.nasdara"


def test_hintergrundabfrage_ueber_nitrado_ist_gedrosselt(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {})
    conn, _req = _conn_mit_session(monkeypatch, "nasdara-hintergrund")
    conn.data["map_name"] = "Livonia"
    aufrufe = []

    class _Api:
        service_id = "1"

        async def get_info(self):
            aufrufe.append(1)
            return {"query": {"map": "dayzOffline.nasdara"}}
    conn.api = _Api()
    monkeypatch.setattr(bot, "_KARTE_NITRADO_GEPRUEFT", {})
    asyncio.run(bot._karte_ueber_nitrado(conn))
    assert conn.data["map_name"] == "Nasdara" and len(aufrufe) == 1
    conn.data["map_name"] = "Livonia"
    asyncio.run(bot._karte_ueber_nitrado(conn))          # innerhalb der Drosselung: keine zweite Abfrage
    assert len(aufrufe) == 1 and conn.data["map_name"] == "Livonia"
