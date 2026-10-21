"""Shop-Lieferung nach einem Kartenwechsel: Kaeufe muessen in die cfgEffectArea.json der AKTUELLEN Mission.

Fehlerbild: Der Betreiber wechselte bei Nitrado die Karte (Livonia -> Nasdara). ``ftp_mission_dir`` wurde nachgezogen,
der einmal gefundene ``cfg_effect_area_path`` blieb auf ``dayzOffline.enoch``. Der Shop schrieb in die Datei der alten
Mission, die der Server nie liest - der Kauf blieb ausstehend und nichts spawnte.

    python3 -m pytest tests/test_shop_effect_pfad.py -q
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402

BASIS = "/dayzps/mpmissions"
ALT = f"{BASIS}/dayzOffline.enoch"
NEU = f"{BASIS}/dayzOffline.nasdara"
DATEI = "cfgEffectArea.json"


class _Ftp:
    """FTP im Speicher: Pfad -> Text. Merkt sich alle Schreibzugriffe."""

    def __init__(self, dateien=None):
        self.dateien = dict(dateien or {})
        self.geschrieben = []

    def read_file_ex(self, path):
        if path in self.dateien:
            return self.dateien[path], "ok"
        return None, "missing"

    def write_file(self, path, content):
        self.dateien[path] = content
        self.geschrieben.append(path)
        return True

    def delete_file(self, path):
        self.dateien.pop(path, None)
        return True


def _vanilla(name="Vanilla Zone"):
    return json.dumps({"Areas": [{"AreaName": name, "Type": "ContaminatedArea_Static",
                                  "Data": {"Pos": [1.0, 0.0, 2.0], "Radius": 50}}],
                       "SafePositions": []})


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    flat = {"1000": {"service_id": "1000", "nitrado_token": "fake", "owner_discord_id": "1000",
                     "map_name": "Livonia", "ftp_mission_dir": ALT,
                     "cfg_effect_area_path": f"{ALT}/{DATEI}"}}
    (tmp_path / "connections.json").write_text(json.dumps(flat))
    registry = bot.ConnectionRegistry()
    registry.load()
    monkeypatch.setattr(bot, "connections", registry)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "9999"})
    conn = registry._conns["1000"]
    conn.ftp = _Ftp({f"{ALT}/{DATEI}": _vanilla("Livonia Zone"), f"{NEU}/{DATEI}": _vanilla("Nasdara Zone")})
    conn.shop = bot.ShopManager(object(), conn)
    return conn


def _kauf(conn):
    return asyncio.run(conn.shop.add_purchase_entries(["Wooden_Crate"], 2, 7679.2, 0.0, 7719.6))


# ── Reine Pfad-Berechnung ────────────────────────────────────────────────
@pytest.mark.parametrize("effekt, mission, erwartet", [
    # Karte gewechselt: gleicher Ordner darueber, anderer dayzOffline-Ordner -> auf die Mission ziehen
    (f"{ALT}/{DATEI}", NEU, f"{NEU}/{DATEI}"),
    (f"{ALT}/{DATEI}", NEU + "/", f"{NEU}/{DATEI}"),                       # abschliessender Schraegstrich
    (f"{BASIS}/DAYZOFFLINE.ENOCH/{DATEI}", NEU, f"{NEU}/{DATEI}"),          # Gross-/Kleinschreibung
    (f"{ALT}/eigene_effekte.json", NEU, f"{NEU}/eigene_effekte.json"),      # Dateiname bleibt
    # schon richtig -> unveraendert
    (f"{NEU}/{DATEI}", NEU, f"{NEU}/{DATEI}"),
    # eigene Pfade bleiben in Ruhe
    ("/eigener/ordner/cfgEffectArea.json", NEU, "/eigener/ordner/cfgEffectArea.json"),
    (f"{ALT}/{DATEI}", "/eigener/ordner/mission", f"{ALT}/{DATEI}"),
    (f"{ALT}/{DATEI}", "/anderes/verzeichnis/dayzOffline.nasdara", f"{ALT}/{DATEI}"),   # anderes Verzeichnis darueber
    # leer / unvollstaendig
    ("", NEU, ""),
    (f"{ALT}/{DATEI}", "", f"{ALT}/{DATEI}"),
    (None, None, ""),
    (DATEI, NEU, DATEI),
])
def test_effect_pfad_zur_mission(effekt, mission, erwartet):
    assert bot._effect_pfad_zur_mission(effekt, mission) == erwartet


# ── Kartenwechsel zieht den Pfad mit ─────────────────────────────────────
def test_kartenwechsel_zieht_effect_pfad_mit(server):
    assert bot._karte_nachziehen(server, "dayzOffline.nasdara") is True
    assert server.data["ftp_mission_dir"] == NEU
    assert server.data["cfg_effect_area_path"] == f"{NEU}/{DATEI}"
    # und er ist gespeichert, nicht nur im Speicher
    gespeichert = json.loads(open("connections.json", encoding="utf-8").read())
    assert gespeichert["1000"]["cfg_effect_area_path"] == f"{NEU}/{DATEI}"


def test_kartenwechsel_laesst_eigenen_effect_pfad_in_ruhe(server):
    server.data["cfg_effect_area_path"] = "/eigener/ordner/cfgEffectArea.json"
    assert bot._karte_nachziehen(server, "dayzOffline.nasdara") is True
    assert server.data["cfg_effect_area_path"] == "/eigener/ordner/cfgEffectArea.json"


def test_kartenwechsel_ohne_effect_pfad_legt_keinen_an(server):
    server.data["cfg_effect_area_path"] = ""
    assert bot._karte_nachziehen(server, "dayzOffline.nasdara") is True
    assert not server.data.get("cfg_effect_area_path")


# ── Selbstheilung: Pfad ist schon veraltet (Kartenwechsel lief vor der Korrektur) ──
def test_veralteter_pfad_wird_beim_shop_zugriff_korrigiert(server):
    server.data["map_name"] = "Nasdara"
    server.data["ftp_mission_dir"] = NEU                    # Mission-Ordner war schon nachgezogen ...
    # ... der Effekt-Pfad steht noch auf der alten Karte
    assert server.data["cfg_effect_area_path"] == f"{ALT}/{DATEI}"
    assert server.shop.effect_area_path() == f"{NEU}/{DATEI}"
    assert server.data["cfg_effect_area_path"] == f"{NEU}/{DATEI}"          # dauerhaft gespeichert
    assert server.shop.effect_area_path() == f"{NEU}/{DATEI}"               # zweiter Aufruf: stabil


def test_eigener_pfad_bleibt_beim_shop_zugriff(server):
    server.data["ftp_mission_dir"] = NEU
    server.data["cfg_effect_area_path"] = "/eigener/ordner/cfgEffectArea.json"
    assert server.shop.effect_area_path() == "/eigener/ordner/cfgEffectArea.json"
    assert server.data["cfg_effect_area_path"] == "/eigener/ordner/cfgEffectArea.json"


def test_ohne_effect_pfad_gilt_der_mission_ordner(server):
    server.data["ftp_mission_dir"] = NEU
    server.data["cfg_effect_area_path"] = ""
    assert server.shop.effect_area_path() == f"{NEU}/{DATEI}"
    server.data["ftp_mission_dir"] = ""
    assert server.shop.effect_area_path() is None


# ── Ende zu Ende: der Kauf landet in der Datei der aktuellen Mission ─────
def test_kauf_nach_kartenwechsel_landet_in_der_neuen_datei(server):
    assert bot._karte_nachziehen(server, "dayzOffline.nasdara") is True
    ok, fehler, namen = _kauf(server)
    assert ok, fehler
    assert len(namen) == 2
    assert server.ftp.geschrieben == [f"{NEU}/{DATEI}"]                       # nur die Nasdara-Datei
    neu = json.loads(server.ftp.dateien[f"{NEU}/{DATEI}"])
    assert [a["AreaName"] for a in neu["Areas"]][0] == "Nasdara Zone"          # Vanilla-Zonen bleiben
    assert sorted(a["AreaName"] for a in neu["Areas"] if a["AreaName"].startswith("SHOP_")) == sorted(namen)
    assert all(a["Data"]["Pos"] == [7679.2, 0.0, 7719.6] for a in neu["Areas"] if a["AreaName"] in namen)
    assert server.ftp.dateien[f"{ALT}/{DATEI}"] == _vanilla("Livonia Zone")   # alte Datei unberuehrt


def test_kauf_mit_schon_veraltetem_pfad_landet_ebenfalls_richtig(server):
    server.data["ftp_mission_dir"] = NEU
    server.data["map_name"] = "Nasdara"                      # nur der Effekt-Pfad ist noch alt
    ok, fehler, namen = _kauf(server)
    assert ok, fehler
    assert server.ftp.geschrieben == [f"{NEU}/{DATEI}"]
    assert f"{ALT}/{DATEI}" not in server.ftp.geschrieben


def test_lieferung_entfernt_die_eintraege_aus_der_neuen_datei(server):
    server.data["ftp_mission_dir"] = NEU
    ok, _fehler, namen = _kauf(server)
    assert ok
    assert asyncio.run(server.shop.remove_area_entries(namen)) is True
    areas = json.loads(server.ftp.dateien[f"{NEU}/{DATEI}"])["Areas"]
    assert [a["AreaName"] for a in areas] == ["Nasdara Zone"]


def test_check_and_heal_meldet_den_korrigierten_pfad(server):
    server.data["ftp_mission_dir"] = NEU
    bericht = asyncio.run(server.shop.check_and_heal())
    assert bericht["path"] == f"{NEU}/{DATEI}"
    assert bericht["status"] == "ok"
