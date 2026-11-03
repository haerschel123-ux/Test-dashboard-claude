"""Teleport-Objekte: Berechnung wie auf dayzfiletoolbox.com/teleports und der Katalog (Standard, Betreiber, Eigene).

tests/fixtures/teleports/referenz_export.json sind 30 echte Laeufe der Funktion exportAllObjects() der Seite (3 Objekte x 10 Lagen) samt
den Objektdaten der Seite. Abweichungen von der Seite sind bewusst: der Name von Land_Mil_GuardShed stimmt hier (die Seite exportiert
dort faelschlich die Toilette), Zahlen sind auf 6 Nachkommastellen gerundet (die Seite schreibt 1e-16-Reste wie 6.1e-17).

    python3 -m pytest tests/test_teleport_objekte.py -q
"""
import asyncio
import copy
import json
import math
import os
import sys
import time

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

with open(os.path.join(REPO, "tests", "fixtures", "teleports", "referenz_export.json"), encoding="utf-8") as _f:
    REF = json.load(_f)

NAME_ZUORDNUNG = {"Land_Misc_Toilet_Mobile": "Land_Misc_Toilet_Mobile", "Land_Mil_GuardShed": "Land_Mil_GuardShed",
                  "Skulls Teleport Stage": "Skulls_Teleport_Stage"}
GAMEPLAY = '{"version":123,"WorldsData":{"objectSpawnersArr":[]}}\n'


class FTP:
    def __init__(self):
        self.files = {"/mission/cfggameplay.json": GAMEPLAY}

    def list_dir(self, d):
        return [p for p in self.files if p.startswith(d + "/")]

    def read_file_ex(self, p):
        return (self.files[p], "ok") if p in self.files else (None, "missing")

    def write_file(self, p, c):
        self.files[p] = c
        return True

    def delete_file(self, p):
        self.files.pop(p, None)
        return True

    def mkdir(self, p):
        return True


@pytest.fixture
def servers(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    eintraege = {sid: {"service_id": sid, "nitrado_token": "x", "map_name": "Livonia", "ftp_mission_dir": "/mission", "owner_discord_id": sid}
                 for sid in ("1000", "2000")}
    (tmp_path / "connections.json").write_text(json.dumps(eintraege))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {})
    monkeypatch.setattr(bot, "_SCHADEN_LOCKS", {})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    monkeypatch.setattr(bot, "_audit_log", bot.deque(maxlen=50))
    for i, c in enumerate(reg._conns.values()):
        c.ftp = FTP()
        reg.add_guild(c.service_id, 111 + i)                  # freigeschaltet (sonst gilt der Kunde als "ohne Premium")
        c.data["kunden_stufe"] = "premium"
    return reg._conns["1000"], reg._conns["2000"]


def _aufruf(monkeypatch, conn, handler, daten=None, admin=True, methode="POST"):
    sid = str(time.time_ns())
    # Ein Nicht-Betreiber ist der Besitzer des Servers (sonst lehnt die Anmeldung ihn schon vorher ab)
    discord_id = sid if admin else str(conn.data["owner_discord_id"])
    bot._SESS_STORE[sid] = {"token": "x", "discord": {"id": discord_id}, "is_admin": admin, "service_id": conn.service_id, "seen": time.time()}
    req = make_mocked_request(methode, "/api/tools/teleports", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})

    async def body(_r):
        return daten
    monkeypatch.setattr(bot, "body", body)
    r = asyncio.run(handler(req))
    return r.status, json.loads(r.body)


def _get(monkeypatch, conn, admin=True):
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_teleports_get, None, admin, "GET")
    assert st == 200, j
    return j["data"]


def _add(monkeypatch, conn, daten, admin=True):
    return _aufruf(monkeypatch, conn, bot.api_tools_teleports_objekt_post, daten, admin)


def _del(monkeypatch, conn, daten, admin=True):
    return _aufruf(monkeypatch, conn, bot.api_tools_teleports_objekt_loeschen, daten, admin)


def _vorschau(monkeypatch, conn, objekt, name="Gate_A", boxen=None):
    box = {"x": 5000, "y": 220, "z": 6000, "width": 3, "height": 4, "depth": 5, "yaw": 90, "pitch": 0, "roll": 0, "objekt": objekt}
    return _aufruf(monkeypatch, conn, bot.api_tools_teleports_post,
                   {"name": name, "commit": False, "boxes": boxen or [box], "targets": [{"x": 7000, "y": 210, "z": 8000}]})


def _objekte_aus(antwort):
    return json.loads(next(g["content"] for g in antwort["data"]["generated"] if g["filename"].endswith("_objects.json")))["Objects"]


def _nah(a, b, tol=1.1e-4):                    # Zahlen unter 0,0001 schreibt der Bot als 0 (siehe _tpo_rund) - die Seite hat z. B. 1.26e-05
    return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))


# ── Berechnung: wie die Webseite ───────────────────────────────────────────
def test_standardobjekte_haben_die_daten_der_seite():
    std = bot._tpo_standard()
    assert set(std) == {"Land_Misc_Toilet_Mobile", "Land_Mil_GuardShed", "Skulls_Teleport_Stage"}
    gruppe = next(a for a in REF["assets"] if a["type"] == "group")
    assert std["Skulls_Teleport_Stage"]["objekte"] == [
        {"name": o["name"], "pos": [float(v) for v in o["pos"]], "ypr": [float(v) for v in o["ypr"]], "scale": float(o["scale"])}
        for o in gruppe["objects"]]
    assert len(gruppe["objects"]) == 21
    assert std["Land_Misc_Toilet_Mobile"]["y_versatz"] == 1.25 == bot._TPO_Y_VERSATZ


@pytest.mark.parametrize("fall", REF["faelle"], ids=lambda f: f"{f['asset']}|{f['orientierung']}")
def test_berechnung_entspricht_der_seite(fall):
    a = bot._tpo_standard()[NAME_ZUORDNUNG[fall["asset"]]]
    mein = bot._tpo_berechnen(a, list(fall["position"]), list(fall["orientierung"]))
    seite = fall["ergebnis"]["Objects"]
    assert len(mein) == len(seite)
    for m, s in zip(mein, seite):
        erwartet_name = "Land_Mil_GuardShed" if fall["asset"] == "Land_Mil_GuardShed" else s["name"]     # Fehler der Seite behoben
        assert m["name"] == erwartet_name
        assert _nah(m["pos"], s["pos"]) and _nah(m["ypr"], s["ypr"]) and abs(m["scale"] - s["scale"]) <= 1e-4
        assert m["enableCEPersistency"] == s["enableCEPersistency"] == 0 and m["customString"] == s["customString"] == ""
        assert list(m) == list(s)                                                              # gleiche Schluessel in gleicher Reihenfolge


def test_einzelobjekt_steht_125_ueber_der_box_und_dreht_mit_der_box():
    a = bot._tpo_standard()["Land_Mil_GuardShed"]
    o, = bot._tpo_berechnen(a, [100, 20, 200], [30, 5, 7])
    assert o["pos"] == [100, 21.25, 200] and o["ypr"] == [30, 5, 7] and o["scale"] == 1.0


def test_gruppe_anker_steht_auf_der_box_kinder_drehen_nur_mit_dem_yaw():
    a = bot._tpo_standard()["Skulls_Teleport_Stage"]
    ob = bot._tpo_berechnen(a, [100, 20, 200], [90, 30, 40])                   # Pitch und Roll der Box gehen nur an den Anker
    assert ob[0]["pos"] == [100, 20, 200] and ob[0]["ypr"] == [90, 30, 40]
    kind = a["objekte"][1]
    # bei 90 Grad: x' = z, z' = -x
    assert _nah(ob[1]["pos"], [100 + kind["pos"][2], 20 + kind["pos"][1], 200 - kind["pos"][0]])
    assert _nah(ob[1]["ypr"], [90 + kind["ypr"][0], kind["ypr"][1], kind["ypr"][2]])


def test_ausgabe_ist_gerundet_und_ohne_minus_null():
    a = bot._tpo_standard()["Skulls_Teleport_Stage"]
    for yaw in (0, 90, 180, 270, 45, -45, 13.37):
        for o in bot._tpo_berechnen(a, [0, 0, 0], [yaw, 0, 0]):
            for v in o["pos"] + o["ypr"] + [o["scale"]]:
                assert v == round(v, 6) and not (v == 0 and math.copysign(1, v) < 0)
                assert "e-" not in repr(v)


# ── Box mit Objekt -> erzeugte Dateien ──────────────────────────────────────
def test_vorschau_mit_standardobjekten(monkeypatch, servers):
    conn, _ = servers
    st, j = _vorschau(monkeypatch, conn, {"quelle": "standard", "name": "Land_Mil_GuardShed"})
    assert st == 200, j
    assert _objekte_aus(j) == [{"name": "Land_Mil_GuardShed", "pos": [5000, 221.25, 6000], "ypr": [90, 0, 0], "scale": 1.0,
                                "enableCEPersistency": 0, "customString": ""}]
    st, j = _vorschau(monkeypatch, conn, {"quelle": "standard", "name": "Skulls_Teleport_Stage"})
    assert st == 200 and len(_objekte_aus(j)) == 21


def test_mehrere_boxen_ergeben_die_objekte_in_der_reihenfolge_der_boxen(monkeypatch, servers):
    conn, _ = servers
    def box(x, objekt):
        return {"x": x, "y": 20, "z": 30, "width": 2, "height": 2, "depth": 2, "yaw": 0, "pitch": 0, "roll": 0, "objekt": objekt}
    boxen = [box(100, {"quelle": "standard", "name": "Skulls_Teleport_Stage"}), box(200, None),
             box(300, {"quelle": "standard", "name": "Land_Misc_Toilet_Mobile"})]
    st, j = _vorschau(monkeypatch, conn, None, boxen=boxen)
    objekte = _objekte_aus(j)
    assert st == 200 and len(objekte) == 22 and objekte[0]["pos"][0] == 100 and objekte[-1]["pos"] == [300, 21.25, 30]
    assert len(json.loads(next(g["content"] for g in j["data"]["generated"] if "/pra/" in g["filename"]))["PRABoxes"]) == 3


@pytest.mark.parametrize("verweis", [{"quelle": "standard", "name": "Gibt_Es_Nicht"}, {"quelle": "premade", "name": "Nichts"},
                                     {"quelle": "eigen", "name": "Nichts"}, {"quelle": "egal", "name": "Land_Mil_GuardShed"},
                                     {"quelle": "standard"}, {"name": "Land_Mil_GuardShed"}, "Land_Mil_GuardShed", ["x"], 5])
def test_unbekanntes_objekt_ist_ein_fehler(monkeypatch, servers, verweis):
    conn, _ = servers
    st, j = _vorschau(monkeypatch, conn, verweis)
    assert st == 400 and ("gibt es nicht" in j["error"] or "Ungültiges Objekt" in j["error"]), j


def test_zu_viele_objekte_in_einer_datei(monkeypatch, servers):
    conn, _ = servers
    inhalt = json.dumps({"Objects": [{"name": f"Obj{i}", "pos": [i, 0, 0], "ypr": [0, 0, 0]} for i in range(300)]})
    assert _add(monkeypatch, conn, {"name": "Gross", "typ": "gruppe", "content": inhalt})[0] == 200
    box = {"x": 100, "y": 20, "z": 30, "width": 2, "height": 2, "depth": 2, "yaw": 0, "pitch": 0, "roll": 0,
           "objekt": {"quelle": "eigen", "name": "Gross"}}
    st, j = _vorschau(monkeypatch, conn, None, boxen=[dict(box) for _ in range(17)])        # 17 x 300 = 5100 > 5000
    assert st == 400 and "Zu viele Objekte" in j["error"]
    assert _vorschau(monkeypatch, conn, None, boxen=[dict(box) for _ in range(16)])[0] == 200


# ── Katalog: eigene und Betreiber ───────────────────────────────────────────
def test_get_liefert_den_katalog_und_die_rolle(monkeypatch, servers):
    conn, _ = servers
    d = _get(monkeypatch, conn)
    assert [o["name"] for o in d["objekte"]["standard"]] == ["Land_Mil_GuardShed", "Land_Misc_Toilet_Mobile", "Skulls_Teleport_Stage"]
    assert d["objekte"]["premade"] == [] and d["objekte"]["eigen"] == [] and d["ist_betreiber"] is True
    assert _get(monkeypatch, conn, admin=False)["ist_betreiber"] is False
    skulls = next(o for o in d["objekte"]["standard"] if o["typ"] == "gruppe")
    assert skulls["anzahl"] == 21 and skulls["anker"] == "StaticObj_Furniture_yellow_star" and skulls["quelle"] == "standard"


def test_eigenes_einzelobjekt_hinzufuegen_benutzen_und_loeschen(monkeypatch, servers):
    conn, _ = servers
    st, j = _add(monkeypatch, conn, {"name": "Mein_Schild", "typ": "einzel", "classname": "Land_Sign_Warning", "scale": 2, "y_versatz": 0.5})
    assert st == 200 and j["data"]["gespeichert"] is True
    assert j["data"]["objekt"] == {"quelle": "eigen", "name": "Mein_Schild", "typ": "einzel", "classname": "Land_Sign_Warning",
                                   "scale": 2.0, "y_versatz": 0.5, "anzahl": 1}
    assert [o["name"] for o in _get(monkeypatch, conn)["objekte"]["eigen"]] == ["Mein_Schild"]
    st, v = _vorschau(monkeypatch, conn, {"quelle": "eigen", "name": "Mein_Schild"})
    assert st == 200 and _objekte_aus(v) == [{"name": "Land_Sign_Warning", "pos": [5000, 220.5, 6000], "ypr": [90, 0, 0], "scale": 2.0,
                                              "enableCEPersistency": 0, "customString": ""}]
    st, j = _del(monkeypatch, conn, {"name": "Mein_Schild"})
    assert st == 200 and j["data"]["objekte"]["eigen"] == []
    assert _vorschau(monkeypatch, conn, {"quelle": "eigen", "name": "Mein_Schild"})[0] == 400


def test_einzelobjekt_vorgaben(monkeypatch, servers):
    conn, _ = servers
    st, j = _add(monkeypatch, conn, {"name": "Kiste", "typ": "einzel", "classname": "Land_Crate"})
    assert st == 200 and j["data"]["objekt"]["scale"] == 1.0 and j["data"]["objekt"]["y_versatz"] == 1.25


def test_eigene_objekte_sind_je_server_getrennt_betreiberobjekte_gelten_fuer_alle(monkeypatch, servers):
    a, b = servers
    assert _add(monkeypatch, a, {"name": "Nur_A", "typ": "einzel", "classname": "Land_A"})[0] == 200
    assert _add(monkeypatch, a, {"name": "Fuer_Alle", "typ": "einzel", "classname": "Land_Alle", "global": True})[0] == 200
    da, db = _get(monkeypatch, a), _get(monkeypatch, b)
    assert [o["name"] for o in da["objekte"]["eigen"]] == ["Nur_A"] and db["objekte"]["eigen"] == []
    assert [o["name"] for o in da["objekte"]["premade"]] == ["Fuer_Alle"] == [o["name"] for o in db["objekte"]["premade"]]
    # Server B darf Nur_A weder benutzen noch loeschen
    assert _vorschau(monkeypatch, b, {"quelle": "eigen", "name": "Nur_A"})[0] == 400
    assert _del(monkeypatch, b, {"name": "Nur_A"})[0] == 404
    assert _vorschau(monkeypatch, b, {"quelle": "premade", "name": "Fuer_Alle"})[0] == 200
    assert [o["name"] for o in _get(monkeypatch, a)["objekte"]["eigen"]] == ["Nur_A"]


def test_betreiberkatalog_nur_fuer_den_betreiber(monkeypatch, servers):
    conn, _ = servers
    st, j = _add(monkeypatch, conn, {"name": "Fuer_Alle", "typ": "einzel", "classname": "Land_X", "global": True}, admin=False)
    assert st == 403
    assert _get(monkeypatch, conn)["objekte"]["premade"] == []
    assert _add(monkeypatch, conn, {"name": "Fuer_Alle", "typ": "einzel", "classname": "Land_X", "global": True})[0] == 200
    assert _del(monkeypatch, conn, {"name": "Fuer_Alle", "global": True}, admin=False)[0] == 403
    assert [o["name"] for o in _get(monkeypatch, conn)["objekte"]["premade"]] == ["Fuer_Alle"]
    assert _del(monkeypatch, conn, {"name": "Fuer_Alle", "global": True})[0] == 200
    assert _get(monkeypatch, conn)["objekte"]["premade"] == []


def test_global_ist_kein_trick_fuer_eigene_objekte(monkeypatch, servers):
    """Wer 'global' nicht als true schickt (z. B. "true" als Text), landet im eigenen Katalog - nie im globalen."""
    conn, _ = servers
    assert _add(monkeypatch, conn, {"name": "X1", "typ": "einzel", "classname": "Land_X", "global": "true"}, admin=False)[0] == 200
    d = _get(monkeypatch, conn)
    assert [o["name"] for o in d["objekte"]["eigen"]] == ["X1"] and d["objekte"]["premade"] == []


def test_ueberschreiben_braucht_eine_bestaetigung(monkeypatch, servers):
    conn, _ = servers
    daten = {"name": "Mein_Obj", "typ": "einzel", "classname": "Land_Eins"}
    assert _add(monkeypatch, conn, daten)[0] == 200
    st, j = _add(monkeypatch, conn, dict(daten, classname="Land_Zwei"))
    assert st == 200 and j["data"] == {"konflikt": True, "name": "Mein_Obj"}
    assert _get(monkeypatch, conn)["objekte"]["eigen"][0]["classname"] == "Land_Eins"          # nichts geaendert
    st, j = _add(monkeypatch, conn, dict(daten, classname="Land_Zwei", overwrite=True))
    assert st == 200 and j["data"]["gespeichert"] is True
    assert _get(monkeypatch, conn)["objekte"]["eigen"][0]["classname"] == "Land_Zwei"
    st, _j = _add(monkeypatch, conn, dict(daten, classname="Land_Drei", overwrite="ja"))        # nur true zaehlt
    assert _j["data"].get("konflikt") is True


@pytest.mark.parametrize("daten, stichwort", [
    ({"name": "", "typ": "einzel", "classname": "Land_X"}, "Name"),
    ({"name": "a b", "typ": "einzel", "classname": "Land_X"}, "Name"),
    ({"name": "../x", "typ": "einzel", "classname": "Land_X"}, "Name"),
    ({"name": "A" * 41, "typ": "einzel", "classname": "Land_X"}, "Name"),
    ({"name": "Ok", "typ": "einzel", "classname": ""}, "Classname"),
    ({"name": "Ok", "typ": "einzel", "classname": "Land X"}, "Classname"),
    ({"name": "Ok", "typ": "einzel", "classname": "Land/X"}, "Classname"),
    ({"name": "Ok", "typ": "einzel", "classname": "L" * 129}, "Classname"),
    ({"name": "Ok", "typ": "einzel", "classname": "Land_X", "scale": 0}, "Skalierung"),
    ({"name": "Ok", "typ": "einzel", "classname": "Land_X", "scale": -1}, "Skalierung"),
    ({"name": "Ok", "typ": "einzel", "classname": "Land_X", "scale": 1001}, "Skalierung"),
    ({"name": "Ok", "typ": "einzel", "classname": "Land_X", "scale": "2"}, "Skalierung"),
    ({"name": "Ok", "typ": "einzel", "classname": "Land_X", "scale": True}, "Skalierung"),
    ({"name": "Ok", "typ": "einzel", "classname": "Land_X", "y_versatz": 1001}, "Höhe"),
    ({"name": "Ok", "typ": "einzel", "classname": "Land_X", "y_versatz": float("nan")}, "Höhe"),
    ({"name": "Ok", "typ": "unbekannt", "classname": "Land_X"}, "Einzelobjekt oder Gruppe"),
    ({"name": "Ok", "classname": "Land_X"}, "Einzelobjekt oder Gruppe"),
    ({"name": "Ok", "typ": "gruppe"}, "JSON-Inhalt"),
    ({"name": "Ok", "typ": "gruppe", "content": "kein json"}, "kein gültiges JSON"),
    ({"name": "Ok", "typ": "gruppe", "content": "{}"}, "Objects"),
    ({"name": "Ok", "typ": "gruppe", "content": '{"Objects": []}'}, "keine Objekte"),
    ({"name": "Ok", "typ": "gruppe", "content": '{"Objects": [{"name": "A", "pos": [1, 2, 3]}]}'}, "mindestens 2"),
    ({"name": "Ok", "typ": "gruppe", "content": '{"Objects": [{"name": "A", "pos": [1, 2, 3]}, {"name": "B/C", "pos": [1, 2, 3]}]}'}, "Namen"),
    ({"name": "Ok", "typ": "gruppe", "content": '{"Objects": [{"name": "A", "pos": [1, 2]}, {"name": "B", "pos": [1, 2, 3]}]}'}, "Position"),
    ({"name": "Ok", "typ": "gruppe", "content": '{"Objects": [{"name": "A", "pos": [1, 2, 3]}, {"name": "B C", "pos": [1, 2, 3]}]}'}, "ungültige Namen"),
])
def test_ungueltige_eingaben_werden_abgelehnt(monkeypatch, servers, daten, stichwort):
    conn, _ = servers
    st, j = _add(monkeypatch, conn, daten)
    assert st == 400 and stichwort in j["error"], j
    assert _get(monkeypatch, conn)["objekte"]["eigen"] == []


def test_grenzen_der_anzahl(monkeypatch, servers):
    conn, _ = servers
    for i in range(bot._TPO_MAX_EIGENE):
        assert _add(monkeypatch, conn, {"name": f"O{i}", "typ": "einzel", "classname": "Land_X"})[0] == 200
    st, j = _add(monkeypatch, conn, {"name": "Zuviel", "typ": "einzel", "classname": "Land_X"})
    assert st == 400 and "höchstens 30" in j["error"]
    assert _add(monkeypatch, conn, {"name": "O0", "typ": "einzel", "classname": "Land_Y", "overwrite": True})[0] == 200     # Ueberschreiben geht immer
    assert _del(monkeypatch, conn, {"name": "O1"})[0] == 200
    assert _add(monkeypatch, conn, {"name": "Zuviel", "typ": "einzel", "classname": "Land_X"})[0] == 200


def test_zu_grosse_gruppe_wird_abgelehnt(monkeypatch, servers):
    conn, _ = servers
    inhalt = json.dumps({"Objects": [{"name": f"Obj{i}", "pos": [i, 0, 0]} for i in range(bot._TPO_MAX_OBJEKTE + 1)]})
    st, j = _add(monkeypatch, conn, {"name": "Riesig", "typ": "gruppe", "content": inhalt})
    assert st == 400 and "höchstens 500" in j["error"]


def test_loeschen_eines_unbekannten_objekts(monkeypatch, servers):
    conn, _ = servers
    assert _del(monkeypatch, conn, {"name": "Gibt_Es_Nicht"})[0] == 404
    assert _del(monkeypatch, conn, {"name": "../x"})[0] == 400
    assert _del(monkeypatch, conn, {"name": "Land_Mil_GuardShed"})[0] == 404         # Standardobjekte lassen sich nicht loeschen


def test_standardobjekte_sind_nicht_ueberschreibbar_aber_namensgleiche_eigene_erlaubt(monkeypatch, servers):
    conn, _ = servers
    assert _add(monkeypatch, conn, {"name": "Land_Mil_GuardShed", "typ": "einzel", "classname": "Land_Anders"})[0] == 200
    assert _vorschau(monkeypatch, conn, {"quelle": "standard", "name": "Land_Mil_GuardShed"})[0] == 200
    st, v = _vorschau(monkeypatch, conn, {"quelle": "standard", "name": "Land_Mil_GuardShed"})
    assert _objekte_aus(v)[0]["name"] == "Land_Mil_GuardShed"                         # Standard bleibt Standard
    st, v = _vorschau(monkeypatch, conn, {"quelle": "eigen", "name": "Land_Mil_GuardShed"})
    assert _objekte_aus(v)[0]["name"] == "Land_Anders"


def test_kaputte_oder_manipulierte_katalogdatei_wird_ignoriert(monkeypatch, servers):
    conn, _ = servers
    assert _add(monkeypatch, conn, {"name": "Gut", "typ": "einzel", "classname": "Land_X"})[0] == 200
    pfad = bot._tpo_datei("eigen", conn.service_id)
    with open(pfad, "w", encoding="utf-8") as f:
        f.write(json.dumps({"Gut": {"name": "Gut", "typ": "einzel", "classname": "Land_X"},
                            "Falsch": {"name": "Anders", "typ": "einzel", "classname": "Land_X"},
                            "Boese": {"name": "Boese", "typ": "einzel", "classname": "../../etc"},
                            "Leer": {"name": "Leer", "typ": "gruppe", "objekte": []}}))
    assert [o["name"] for o in _get(monkeypatch, conn)["objekte"]["eigen"]] == ["Gut"]
    with open(pfad, "w", encoding="utf-8") as f:
        f.write("{ kaputt")
    assert _get(monkeypatch, conn)["objekte"]["eigen"] == []
    assert _add(monkeypatch, conn, {"name": "Neu", "typ": "einzel", "classname": "Land_X"})[0] == 200


def test_dateien_liegen_je_server_und_nie_ausserhalb_des_ordners(monkeypatch, servers):
    a, _ = servers
    assert _add(monkeypatch, a, {"name": "A1", "typ": "einzel", "classname": "Land_X"})[0] == 200
    assert os.path.exists(os.path.join("teleport_objekte", "eigene", "1000.json"))
    assert not os.path.exists(os.path.join("teleport_objekte", "eigene", "2000.json"))
    boese = bot._tpo_datei("eigen", "../../etc/passwd")
    assert os.path.abspath(boese).startswith(os.path.abspath("teleport_objekte"))


def test_gespeichert_wird_atomar_und_gueltig(monkeypatch, servers):
    conn, _ = servers
    assert _add(monkeypatch, conn, {"name": "A1", "typ": "einzel", "classname": "Land_X"})[0] == 200
    daten = json.load(open(bot._tpo_datei("eigen", conn.service_id), encoding="utf-8"))
    assert daten == {"A1": {"name": "A1", "typ": "einzel", "classname": "Land_X", "scale": 1.0, "y_versatz": 1.25}}
    assert [f for f in os.listdir(os.path.join("teleport_objekte", "eigene")) if f.endswith(".tmp")] == []


def test_audit_eintraege(monkeypatch, servers):
    conn, _ = servers
    _add(monkeypatch, conn, {"name": "A1", "typ": "einzel", "classname": "Land_X"})
    _del(monkeypatch, conn, {"name": "A1"})
    texte = " ".join(str(e) for e in bot._audit_log)
    assert "Teleport-Objekt gespeichert" in texte and "Teleport-Objekt gelöscht" in texte


# ── Gruppe aus eigenen Objekten: der Bot rechnet um ─────────────────────────────
def _absolut(box_pos, yaw):
    """Gruppe der Seite als absolute Object-Spawner-Datei, so wie sie in der Welt steht (Lage der Box)."""
    return bot._tpo_berechnen(bot._tpo_standard()["Skulls_Teleport_Stage"], box_pos, [yaw, 0, 0])


@pytest.mark.parametrize("yaw", [0, 37, 90, 180, -75, 250])
def test_gruppe_importieren_und_mit_gleicher_drehung_ausgeben_ergibt_dieselbe_lage(monkeypatch, servers, yaw):
    conn, _ = servers
    original = _absolut([5000.5, 33.25, 6000.75], yaw)
    assert _add(monkeypatch, conn, {"name": "Meine_Gruppe", "typ": "gruppe", "content": json.dumps({"Objects": original})})[0] == 200
    a = bot._tpo_finden("eigen", "Meine_Gruppe", conn.service_id)
    neu = bot._tpo_berechnen(a, [5000.5, 33.25, 6000.75], [yaw, 0, 0])
    assert len(neu) == len(original)
    for n, o in zip(neu, original):
        assert n["name"] == o["name"] and _nah(n["pos"], o["pos"], 2e-5) and _nah(n["ypr"], o["ypr"], 2e-5)


def test_importierte_gruppe_wird_mit_der_box_gedreht_und_verschoben(monkeypatch, servers):
    conn, _ = servers
    original = _absolut([100, 20, 200], 30)                                      # Anker steht bei Yaw 30
    assert _add(monkeypatch, conn, {"name": "G", "typ": "gruppe", "content": json.dumps({"Objects": original})})[0] == 200
    a = bot._tpo_finden("eigen", "G", conn.service_id)
    # dieselbe Gruppe an anderer Stelle mit Yaw 120: Abstaende zum Anker bleiben, alles dreht um 90 Grad weiter
    neu = bot._tpo_berechnen(a, [300, 25, 400], [120, 0, 0])
    anker_alt, anker_neu = original[0]["pos"], neu[0]["pos"]
    for n, o in zip(neu[1:], original[1:]):
        d_alt = [o["pos"][0] - anker_alt[0], o["pos"][2] - anker_alt[2]]
        d_neu = [n["pos"][0] - anker_neu[0], n["pos"][2] - anker_neu[2]]
        w = math.radians(90)
        erwartet = [d_alt[0] * math.cos(w) + d_alt[1] * math.sin(w), -d_alt[0] * math.sin(w) + d_alt[1] * math.cos(w)]
        assert _nah(d_neu, erwartet, 3e-5)
        assert abs((n["pos"][1] - anker_neu[1]) - (o["pos"][1] - anker_alt[1])) <= 1e-5       # Hoehenunterschied bleibt
        assert abs((n["ypr"][0] - 120) - (o["ypr"][0] - 30)) <= 1e-5                         # Yaw relativ zum Anker bleibt


def test_gruppe_anker_ist_das_erste_objekt_und_steht_nach_dem_import_im_ursprung(monkeypatch, servers):
    conn, _ = servers
    objekte = [{"name": "Anker", "pos": [1000, 50, 2000], "ypr": [0, 0, 0], "scale": 2},
               {"name": "Kind", "pos": [1003, 51, 2004], "ypr": [10, 5, 6], "scale": 0.5}]
    assert _add(monkeypatch, conn, {"name": "Zwei", "typ": "gruppe", "content": json.dumps({"Objects": objekte})})[0] == 200
    a = bot._tpo_finden("eigen", "Zwei", conn.service_id)
    assert a["objekte"][0]["pos"] == [0.0, 0.0, 0.0] and a["objekte"][0]["scale"] == 2.0
    assert a["objekte"][1] == {"name": "Kind", "pos": [3.0, 1.0, 4.0], "ypr": [10.0, 5.0, 6.0], "scale": 0.5}


def test_import_akzeptiert_dze_dateien_mit_zusatzfeldern(monkeypatch, servers):
    conn, _ = servers
    objekte = [{"name": "A", "pos": [1, 2, 3], "ypr": [0, 0, 0], "scale": 1.0, "enableCEPersistency": 1, "customString": "x", "extra": [1]},
               {"name": "B", "pos": [2, 2, 3]}]
    assert _add(monkeypatch, conn, {"name": "Dze", "typ": "gruppe", "content": json.dumps({"Objects": objekte})})[0] == 200
    assert bot._tpo_finden("eigen", "Dze", conn.service_id)["objekte"][1]["ypr"] == [0.0, 0.0, 0.0]


def test_expansion_airdrop_datei_wird_mit_klarer_meldung_abgelehnt(monkeypatch, servers):
    conn, _ = servers
    st, j = _add(monkeypatch, conn, {"name": "Ex", "typ": "gruppe", "content": '{"Containers": []}'})
    assert st == 400 and "Expansion" in j["error"]
