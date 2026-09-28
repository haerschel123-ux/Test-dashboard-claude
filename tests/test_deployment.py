"""NPC + Vehicle Deployment (erste Version: nur Fahrzeuge).

Kern: eigene Bloecke zwischen <!-- DAYZCODE:START id --> / END-Markern in
db/events.xml und cfgeventspawns.xml - alles ausserhalb bleibt Byte fuer Byte
erhalten, cfgspawnabletypes.xml wird nur gelesen, andere Tools duerfen die
markierten Bloecke nicht anfassen.

    python3 -m pytest tests/test_deployment.py -v
"""
import asyncio
import json
import os
import sys
import time
import xml.etree.ElementTree as ET

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

EVENTS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<events>\n'
          '    <!-- Vanilla-Kommentar ü -->\n'
          '    <event name="VehicleOffroadHatchback">\n'
          '        <nominal>8</nominal>\n'
          '        <children>\n'
          '            <child lootmax="0" lootmin="0" max="1" min="1" type="OffroadHatchback_Blue"/>\n'
          '        </children>\n'
          '    </event>\n'
          '</events>\n')
SPAWNS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<eventposdef>\n'
          '    <event name="VehicleOffroadHatchback">\n'
          '        <pos x="100" z="200" a="0"/>\n'
          '    </event>\n'
          '</eventposdef>\n')
TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
         '<spawnabletypes>\n'
         '    <type name="OffroadHatchback_Blue">\n'
         '        <attachments chance="1.00"><item name="HatchbackWheel" chance="1.00"/></attachments>\n'
         '    </type>\n'
         '</spawnabletypes>\n')


def _run(coro):
    return asyncio.run(coro)


# ── Reine Marker-Funktionen ──────────────────────────────────────────────
@pytest.mark.parametrize("zeilenende", ["\n", "\r\n"])
def test_einfuegen_laesst_rest_bytegleich_und_entfernen_stellt_original_her(zeilenende):
    original = EVENTS.replace("\n", zeilenende)
    block = bot._dayzcode_event_xml("VehicleOffroadHatchback_Blue_Test", "OffroadHatchback_Blue", 1)
    neu = bot._dayzcode_einfuegen(original, "events", "dv_abc", block)
    ET.fromstring(neu.encode("utf-8"))
    assert neu.count("<!-- DAYZCODE:START dv_abc -->") == 1
    assert "\r\n" not in neu or "\n" not in neu.replace("\r\n", "")  # keine gemischten Enden
    seg = bot._dayzcode_segmente(neu)
    assert [s["id"] for s in seg] == ["dv_abc"]
    zurueck, gefunden = bot._dayzcode_entfernen(neu, "dv_abc")
    assert gefunden and zurueck == original


def test_zweites_deployment_bleibt_beim_entfernen_unberuehrt():
    a = bot._dayzcode_einfuegen(EVENTS, "events", "dv_a",
                                bot._dayzcode_event_xml("E_A", "OffroadHatchback_Blue", 1))
    ab = bot._dayzcode_einfuegen(a, "events", "dv_b",
                                 bot._dayzcode_event_xml("E_B", "OffroadHatchback_Blue", 2))
    nur_b, _ = bot._dayzcode_entfernen(ab, "dv_a")
    assert "E_A" not in nur_b and "E_B" in nur_b
    zurueck, _ = bot._dayzcode_entfernen(nur_b, "dv_b")
    assert zurueck == EVENTS


@pytest.mark.parametrize("kaputt", [
    "<!-- DAYZCODE:START x -->\n<!-- DAYZCODE:START x -->\n",
    "<!-- DAYZCODE:END x -->\n",
    "<!-- DAYZCODE:START x -->\n",
])
def test_kaputte_marker_werfen_im_strengen_modus(kaputt):
    with pytest.raises(ValueError):
        bot._dayzcode_segmente(kaputt)
    bot._dayzcode_segmente(kaputt, streng=False)  # tolerant: kein Fehler


def test_andere_tools_duerfen_markierten_block_nicht_aendern():
    neu = bot._dayzcode_einfuegen(EVENTS, "events", "dv_x",
                                  bot._dayzcode_event_xml("Geschuetzt", "OffroadHatchback_Blue", 1))
    with pytest.raises(ValueError, match="NPC \\+ Vehicle Deployment"):
        bot._tool_delete_event(neu, "Geschuetzt")
    with pytest.raises(ValueError):
        bot._tool_upsert_event(neu, {"name": "Geschuetzt", "nominal": 1, "min": 1, "max": 1,
                                     "lifetime": 1, "restock": 0, "saferadius": 1,
                                     "distanceradius": 1, "cleanupradius": 1, "children": []})
    # Fremde, nicht markierte Events bleiben bearbeitbar.
    _, gefunden = bot._tool_delete_event(neu, "VehicleOffroadHatchback")
    assert gefunden


def test_events_liste_blendet_eingesetzte_namen_aus():
    root = ET.fromstring(bot._dayzcode_einfuegen(
        EVENTS, "events", "dv_x",
        bot._dayzcode_event_xml("Eingesetzt_1", "OffroadHatchback_Blue", 1)))
    assert "Eingesetzt_1" in bot._tool_events_liste(root)
    assert "Eingesetzt_1" not in bot._tool_events_liste(root, ausblenden={"Eingesetzt_1"})


# ── Endpunkte ────────────────────────────────────────────────────────────
class _StubFTP:
    def __init__(self, dateien, fehler_bei=None):
        self.dateien = dict(dateien)
        self.fehler_bei = fehler_bei

    def list_dir(self, verzeichnis):
        praefix = verzeichnis.rstrip("/") + "/"
        return [p for p in self.dateien if p.startswith(praefix)]

    def read_file_ex(self, pfad):
        if pfad in self.dateien:
            return self.dateien[pfad], "ok"
        return None, "missing"

    def write_file(self, pfad, inhalt):
        if self.fehler_bei and pfad.endswith(self.fehler_bei):
            return False
        self.dateien[pfad] = inhalt
        return True


_zaehler = [0]


def _server(map_name="Livonia", types=TYPES, fehler_bei=None):
    _zaehler[0] += 1
    sid = f"deploy-{_zaehler[0]}"
    conn = bot.connections.upsert(sid, nitrado_token="fake", owner_discord_id="1")
    conn.data["ftp_mission_dir"] = "/mission"
    conn.data["map_name"] = map_name
    conn.data["deployments"] = []
    conn.ftp = _StubFTP({"/mission/db/events.xml": EVENTS,
                         "/mission/cfgeventspawns.xml": SPAWNS,
                         "/mission/cfgspawnabletypes.xml": types}, fehler_bei)
    return conn


def _request(monkeypatch, conn, methode, pfad, body_daten=None):
    _zaehler[0] += 1
    sid = f"test-sid-{time.time_ns()}"
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": f"acct-{_zaehler[0]}"},
                            "is_admin": True, "service_id": conn.service_id, "seen": time.time()}
    req = make_mocked_request(methode, pfad, headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    if body_daten is not None:
        async def fake_body(_request):
            return body_daten
        monkeypatch.setattr(bot, "body", fake_body)
    return req


def _json(resp):
    return resp.status, json.loads(resp.body.decode("utf-8"))


def _deploy(monkeypatch, conn, commit, suffix="MilitaryADA", positions=None, preset="ada_blue"):
    req = _request(monkeypatch, conn, "POST", "/api/tools/deployment/deploy",
                   {"preset": preset, "suffix": suffix, "commit": commit,
                    "positions": positions or [{"x": 6290, "z": 10405, "a": 0}]})
    return _json(_run(bot.api_tools_deployment_deploy(req)))


def test_vorschau_schreibt_nichts(monkeypatch):
    conn = _server()
    status, j = _deploy(monkeypatch, conn, commit=False)
    assert status == 200, j
    assert j["data"]["event_name"] == "VehicleOffroadHatchback_Blue_MilitaryADA"
    assert conn.ftp.dateien["/mission/db/events.xml"] == EVENTS
    assert conn.ftp.dateien["/mission/cfgeventspawns.xml"] == SPAWNS
    assert conn.data["deployments"] == []


def test_commit_schreibt_zwei_dateien_und_laesst_types_unberuehrt(monkeypatch):
    conn = _server()
    status, j = _deploy(monkeypatch, conn, commit=True,
                        positions=[{"x": 6290, "z": 10405, "a": 0}, {"x": 6300, "z": 10410, "a": 90}])
    assert status == 200, j
    ev = conn.ftp.dateien["/mission/db/events.xml"]
    sp = conn.ftp.dateien["/mission/cfgeventspawns.xml"]
    assert "<nominal>2</nominal>" in ev and 'type="OffroadHatchback_Blue"' in ev
    assert sp.count("<pos ") == 3 and 'a="90"' in sp
    assert conn.ftp.dateien["/mission/cfgspawnabletypes.xml"] == TYPES
    dep = conn.data["deployments"][0]
    assert dep["event_name"] == "VehicleOffroadHatchback_Blue_MilitaryADA"
    ET.fromstring(ev.encode("utf-8"))
    ET.fromstring(sp.encode("utf-8"))


def test_fehlender_type_bricht_ab(monkeypatch):
    conn = _server(types=TYPES.replace("OffroadHatchback_Blue", "Anders"))
    status, _ = _deploy(monkeypatch, conn, commit=True)
    assert status == 422
    assert conn.ftp.dateien["/mission/db/events.xml"] == EVENTS


def test_namenskollision_ergibt_409(monkeypatch):
    conn = _server()
    assert _deploy(monkeypatch, conn, commit=True)[0] == 200
    status, _ = _deploy(monkeypatch, conn, commit=True)
    assert status == 409
    assert len(conn.data["deployments"]) == 1


def test_fehler_beim_zweiten_schreiben_setzt_events_zurueck(monkeypatch):
    conn = _server(fehler_bei="cfgeventspawns.xml")
    status, _ = _deploy(monkeypatch, conn, commit=True)
    assert status == 502
    assert conn.ftp.dateien["/mission/db/events.xml"] == EVENTS
    assert conn.data["deployments"] == []


def test_entfernen_stellt_originaldateien_wieder_her(monkeypatch):
    conn = _server()
    _deploy(monkeypatch, conn, commit=True)
    dep_id = conn.data["deployments"][0]["id"]
    req = _request(monkeypatch, conn, "POST", "/api/tools/deployment/remove", {"id": dep_id})
    status, j = _json(_run(bot.api_tools_deployment_remove(req)))
    assert status == 200, j
    assert conn.ftp.dateien["/mission/db/events.xml"] == EVENTS
    assert conn.ftp.dateien["/mission/cfgeventspawns.xml"] == SPAWNS
    assert conn.data["deployments"] == []


def test_ungueltige_eingaben(monkeypatch):
    conn = _server()
    assert _deploy(monkeypatch, conn, commit=False, suffix="böse<name>")[0] == 400
    assert _deploy(monkeypatch, conn, commit=False,
                   positions=[{"x": 30000, "z": 1, "a": 0}])[0] == 400
    assert _deploy(monkeypatch, conn, commit=False,
                   positions=[{"x": 1, "z": 1, "a": 400}])[0] == 400
    assert _deploy(monkeypatch, conn, commit=False, preset="gibtsnicht")[0] == 422


def test_humvee_nicht_auf_sakhal(monkeypatch):
    conn = _server(map_name="Sakhal")
    req = _request(monkeypatch, conn, "GET", "/api/tools/deployment")
    status, j = _json(_run(bot.api_tools_deployment_get(req)))
    assert status == 200
    keys = {p["key"] for p in j["data"]["presets"]}
    assert "humvee" not in keys and "ada_blue" in keys
    assert _deploy(monkeypatch, conn, commit=False, preset="humvee")[0] == 422


def test_kunde_b_kann_einsatz_von_a_nicht_entfernen(monkeypatch):
    a, b = _server(), _server()
    _deploy(monkeypatch, a, commit=True)
    dep_id = a.data["deployments"][0]["id"]
    req = _request(monkeypatch, b, "POST", "/api/tools/deployment/remove", {"id": dep_id})
    status, _ = _json(_run(bot.api_tools_deployment_remove(req)))
    assert status == 404
    assert len(a.data["deployments"]) == 1
