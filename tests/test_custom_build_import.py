"""Tests fuer den Import/Entfernen-Teil des "Custom Build Mapping"-Tools:
eine Object-Spawner-JSON-Datei importieren (schreibt custom/<name>.json und
traegt sie in cfggameplay.json -> WorldsData.objectSpawnersArr ein) bzw.
wieder entfernen (Datei + Array-Eintrag).

Brigarde: "custom/file_name.json" und "./custom/file_name.json" muessen als
DERSELBE Eintrag erkannt werden - das ist der Kern der Normalisierung, die
hier geprueft wird.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_custom_build_import.py -v
"""
import asyncio
import json
import os
import sys
import time

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _run(coro):
    return asyncio.run(coro)


# ── Reine Funktion: Pfad-Normalisierung ──────────────────────────────────
def test_normalisierung_behandelt_fuehrendes_punkt_slash_gleich():
    a = bot._custom_spawner_pfad_normalisieren("custom/bunker.json")
    b = bot._custom_spawner_pfad_normalisieren("./custom/bunker.json")
    assert a == b == "custom/bunker.json"


def test_normalisierung_unterschiedliche_dateien_bleiben_verschieden():
    a = bot._custom_spawner_pfad_normalisieren("custom/bunker.json")
    b = bot._custom_spawner_pfad_normalisieren("custom/anders.json")
    assert a != b


# ── Integrationstests gegen die echten Endpunkte ─────────────────────────
class _StubFTP:
    def __init__(self, dateien=None):
        self.dateien = dateien or {}  # {pfad: inhalt}

    def list_dir(self, verzeichnis):
        praefix = verzeichnis.rstrip("/") + "/"
        return [p for p in self.dateien if p.startswith(praefix)]

    def read_file_ex(self, pfad):
        if pfad in self.dateien:
            return self.dateien[pfad], "ok"
        return None, "missing"

    def write_file(self, pfad, inhalt):
        self.dateien[pfad] = inhalt
        return True

    def delete_file(self, pfad):
        return self.dateien.pop(pfad, None) is not None

    def file_size_or_none(self, pfad):
        return len(self.dateien.get(pfad, "")) or None


_zaehler = [0]


def _request(monkeypatch, methode, pfad, body_daten=None, match_info=None):
    _zaehler[0] += 1
    service_id = f"custombuild-{_zaehler[0]}"
    conn = bot.connections.upsert(service_id, nitrado_token="fake")
    conn.data["ftp_mission_dir"] = "/mission"
    conn.ftp = _StubFTP({
        "/mission/cfggameplay.json": json.dumps(
            {"WorldsData": {"objectSpawnersArr": []}}),
    })
    sid = f"test-sid-{time.time_ns()}"
    # Eigene Discord-ID je Aufruf, sonst wuerde der Rate-Limiter
    # (kontoweit, ueber alle Sessions) den zweiten Testaufruf blockieren.
    sess = {"token": "fake", "discord": {"id": f"acct-{_zaehler[0]}"}, "is_admin": True,
           "service_id": service_id, "seen": time.time()}
    bot._SESS_STORE[sid] = sess
    req = make_mocked_request(methode, pfad,
                              headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    if match_info:
        req.match_info.update(match_info)
    if body_daten is not None:
        async def fake_body(_request):
            return body_daten
        monkeypatch.setattr(bot, "body", fake_body)
    return req, conn


def _antwort_json(resp):
    return json.loads(resp.body.decode("utf-8"))


def test_import_schreibt_datei_und_traegt_ein(monkeypatch):
    req, conn = _request(monkeypatch, "POST", "/api/tools/custombuildmap/import",
                         body_daten={"filename": "bunker.json",
                                     "content": json.dumps({"Objects": [
                                         {"name": "Land_Bunker", "pos": [1, 2, 3]}]})})
    res = _run(bot.api_tools_custombuildmap_import(req))
    daten = _antwort_json(res)
    assert daten["ok"] is True
    assert daten["data"]["geschrieben"] is True
    assert daten["data"]["eingetragen"] is True
    assert "/mission/custom/bunker.json" in conn.ftp.dateien
    gameplay = json.loads(conn.ftp.dateien["/mission/cfggameplay.json"])
    assert gameplay["WorldsData"]["objectSpawnersArr"] == ["custom/bunker.json"]


def test_import_lehnt_ungueltigen_dateinamen_ab(monkeypatch):
    req, _conn = _request(monkeypatch, "POST", "/api/tools/custombuildmap/import",
                          body_daten={"filename": "../../etc/passwd",
                                      "content": '{"Objects": []}'})
    res = _run(bot.api_tools_custombuildmap_import(req))
    daten = _antwort_json(res)
    assert daten["ok"] is False


def test_import_lehnt_ungueltiges_json_ab(monkeypatch):
    req, _conn = _request(monkeypatch, "POST", "/api/tools/custombuildmap/import",
                          body_daten={"filename": "bunker.json", "content": "kein json{"})
    res = _run(bot.api_tools_custombuildmap_import(req))
    daten = _antwort_json(res)
    assert daten["ok"] is False


def test_import_verlangt_objects_feld(monkeypatch):
    req, _conn = _request(monkeypatch, "POST", "/api/tools/custombuildmap/import",
                          body_daten={"filename": "bunker.json", "content": "{}"})
    res = _run(bot.api_tools_custombuildmap_import(req))
    daten = _antwort_json(res)
    assert daten["ok"] is False


def test_import_meldet_konflikt_bei_bestehender_datei_ohne_overwrite(monkeypatch):
    req, conn = _request(monkeypatch, "POST", "/api/tools/custombuildmap/import",
                         body_daten={"filename": "bunker.json",
                                     "content": '{"Objects": []}'})
    conn.ftp.dateien["/mission/custom/bunker.json"] = '{"Objects": [{"name": "alt"}]}'
    res = _run(bot.api_tools_custombuildmap_import(req))
    daten = _antwort_json(res)
    assert daten["ok"] is True
    assert daten["data"]["konflikt"] is True
    # Nicht ueberschrieben:
    assert "alt" in conn.ftp.dateien["/mission/custom/bunker.json"]


def test_import_ueberschreibt_mit_overwrite_flag(monkeypatch):
    req, conn = _request(monkeypatch, "POST", "/api/tools/custombuildmap/import",
                         body_daten={"filename": "bunker.json",
                                     "content": '{"Objects": [{"name": "neu"}]}',
                                     "overwrite": True})
    conn.ftp.dateien["/mission/custom/bunker.json"] = '{"Objects": [{"name": "alt"}]}'
    res = _run(bot.api_tools_custombuildmap_import(req))
    daten = _antwort_json(res)
    assert daten["ok"] is True
    assert "neu" in conn.ftp.dateien["/mission/custom/bunker.json"]


def test_import_erkennt_bereits_eingetragenen_pfad_mit_punkt_slash_praefix(monkeypatch):
    req, conn = _request(monkeypatch, "POST", "/api/tools/custombuildmap/import",
                         body_daten={"filename": "bunker.json",
                                     "content": '{"Objects": []}',
                                     "overwrite": True})
    conn.ftp.dateien["/mission/cfggameplay.json"] = json.dumps(
        {"WorldsData": {"objectSpawnersArr": ["./custom/bunker.json"]}})
    res = _run(bot.api_tools_custombuildmap_import(req))
    daten = _antwort_json(res)
    assert daten["ok"] is True
    gameplay = json.loads(conn.ftp.dateien["/mission/cfggameplay.json"])
    # Kein zweiter Eintrag - der bestehende "./custom/bunker.json" zaehlt schon.
    assert gameplay["WorldsData"]["objectSpawnersArr"] == ["./custom/bunker.json"]


def test_entfernen_loescht_datei_und_array_eintrag(monkeypatch):
    req, conn = _request(monkeypatch, "DELETE", "/api/tools/custombuildmap/bunker.json",
                         match_info={"filename": "bunker.json"})
    conn.ftp.dateien["/mission/custom/bunker.json"] = '{"Objects": []}'
    conn.ftp.dateien["/mission/cfggameplay.json"] = json.dumps(
        {"WorldsData": {"objectSpawnersArr": ["custom/bunker.json"]}})
    res = _run(bot.api_tools_custombuildmap_remove(req))
    daten = _antwort_json(res)
    assert daten["ok"] is True
    assert daten["data"]["entfernt"] is True
    assert daten["data"]["datei_geloescht"] is True
    assert "/mission/custom/bunker.json" not in conn.ftp.dateien
    gameplay = json.loads(conn.ftp.dateien["/mission/cfggameplay.json"])
    assert gameplay["WorldsData"]["objectSpawnersArr"] == []


def test_entfernen_erkennt_punkt_slash_praefix_variante(monkeypatch):
    req, conn = _request(monkeypatch, "DELETE", "/api/tools/custombuildmap/bunker.json",
                         match_info={"filename": "bunker.json"})
    conn.ftp.dateien["/mission/cfggameplay.json"] = json.dumps(
        {"WorldsData": {"objectSpawnersArr": ["./custom/bunker.json", "custom/andere.json"]}})
    res = _run(bot.api_tools_custombuildmap_remove(req))
    daten = _antwort_json(res)
    assert daten["ok"] is True
    gameplay = json.loads(conn.ftp.dateien["/mission/cfggameplay.json"])
    assert gameplay["WorldsData"]["objectSpawnersArr"] == ["custom/andere.json"]


def test_entfernen_unbekannter_eintrag_liefert_fehler(monkeypatch):
    req, conn = _request(monkeypatch, "DELETE", "/api/tools/custombuildmap/nichtda.json",
                         match_info={"filename": "nichtda.json"})
    res = _run(bot.api_tools_custombuildmap_remove(req))
    daten = _antwort_json(res)
    assert daten["ok"] is False
