"""Airdrops JSON (Dashboard → Tools → „Airdrops JSON“, /airdrop add|list|remove, Scheduler).

Geprüft werden: Datei-Prüfung (`_adj_pruefen`), Verschieben/Karten-Grenzen, der Dateispeicher
(Premade global, Eigene je Server, Mandantentrennung, Pfad-Injektion), die Server-Transaktion
(custom/adj_<id>.json + cfggameplay.json → objectSpawnersArr, Rollback, Aufräumen), der
Neustart-Zähler samt Scheduler-Rotation (mit simulierten Neustarts, ohne A2S), die Slash-Befehle,
die Dashboard-API, das Backup und die Registrierung.

Es gibt weder Netzwerk noch Discord-Login: FTP und Interaktionen sind Fakes, die Testdaten sind
SYNTHETISCH (12 Objekte mit absoluten Chernarus-Koordinaten). Alles läuft im Wegwerf-Verzeichnis
(`airdrop_json/` entsteht relativ dazu, nie im Repo).

    python3 -m pytest tests/test_airdrop_json.py -q
"""
import asyncio
import collections
import copy
import json
import logging
import os
import sys
import time
import zipfile
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_airstrike import servers  # noqa: F401 - Fixture für pytest

discord = bot.discord
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GID = 8101            # Guild des Mandanten 1000
GID_B = 8102          # Guild des Mandanten 2000
MISSION = "/mission"
GAMEPLAY_PFAD = f"{MISSION}/cfggameplay.json"


def run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════
#  Synthetische Testdaten
# ══════════════════════════════════════════════════════════════════════════
# 12 Objekte, absolute Chernarus-Koordinaten: x 3005–3057, z 12304–12365, y 275–287.
# Mitte des Rahmens: x 3031, z 12334.5; tiefster Punkt y 275.
_ROH = [
    ("Land_Container_1Moh_DE", [3005.0, 275.0, 12304.0], [90.0, 0.0, 0.0], 1.0),
    ("Land_Container_1Moh_DE", [3011.5, 275.5, 12310.25], [0.0, 0.0, 0.0], 1.0),
    ("StaticObj_Wall_Concrete_4m", [3018.0, 276.0, 12316.5], [45.0, 0.0, 0.0], 1.0),
    ("StaticObj_Wall_Concrete_4m", [3024.5, 277.25, 12322.75], [45.0, 5.0, 0.0], 1.5),
    ("Land_Wreck_Truck", [3031.0, 278.0, 12329.0], [10.0, 0.0, 0.0], 1.0),
    ("Land_Wreck_Truck", [3037.5, 279.5, 12335.25], [20.0, 0.0, 0.0], 1.0),
    ("Barrel_Blue", [3044.0, 281.0, 12341.5], [0.0, 0.0, 0.0], 1.0),
    ("Barrel_Green", [3050.5, 283.0, 12347.75], [0.0, 0.0, 0.0], 1.0),
    ("Barrel_Red", [3057.0, 285.0, 12354.0], [0.0, 0.0, 0.0], 1.0),
    ("Land_Tent_Big", [3020.0, 286.0, 12360.0], [180.0, 0.0, 0.0], 1.0),
    ("Land_Tent_Big", [3030.0, 287.0, 12365.0], [270.0, 0.0, 0.0], 1.0),
    ("Wooden_Crate", [3040.0, 284.0, 12330.0], [0.0, 0.0, 0.0], 0.5),
]


def _roh_objekte():
    return [{"name": n, "pos": list(p), "ypr": list(ypr), "scale": s,
             "enableCEPersistency": 0, "customString": ""} for n, p, ypr, s in _ROH]


def _text(objekte=None):
    return json.dumps({"Objects": _roh_objekte() if objekte is None else objekte}, indent=2)


def _objekte():
    """Normalisierte Objekte der Fixture (so wie sie der Speicher kennt)."""
    objekte, fehler = bot._adj_pruefen(_text())
    assert fehler is None, fehler
    return objekte


def _viele(n):
    return [{"name": f"Barrel_{i}", "pos": [100.0 + i, 10.0, 200.0 + i], "ypr": [0, 0, 0], "scale": 1} for i in range(n)]


# ══════════════════════════════════════════════════════════════════════════
#  Fake-FTP und Fixtures
# ══════════════════════════════════════════════════════════════════════════
GAMEPLAY = {"version": 123, "WorldsData": {"objectSpawnersArr": ["custom/alt.json"], "andere": {"a": 1}}}


class FTP:
    """Mission-Ordner im Speicher. `write_fehler`: Teilstrings von Pfaden, deren Schreiben scheitert."""

    def __init__(self, gameplay=GAMEPLAY):
        self.files = {}
        if gameplay is not None:
            self.files[GAMEPLAY_PFAD] = json.dumps(gameplay, indent=4)
        self.files[f"{MISSION}/custom/alt.json"] = '{"Objects": []}'
        self.protokoll = []
        self.write_fehler = set()
        self.mkdir_ok = True
        self.delete_ok = True

    def list_dir(self, directory):
        d = directory.rstrip("/") + "/"
        return sorted({d + p[len(d):].split("/", 1)[0] for p in self.files if p.startswith(d)})

    def read_file_ex(self, pfad):
        return (self.files[pfad], "ok") if pfad in self.files else (None, "missing")

    def write_file(self, pfad, inhalt):
        self.protokoll.append(("write", pfad))
        if any(s in pfad for s in self.write_fehler):
            return False
        self.files[pfad] = inhalt
        return True

    def delete_file(self, pfad):
        self.protokoll.append(("delete", pfad))
        if not self.delete_ok:
            return False
        return self.files.pop(pfad, None) is not None

    def mkdir(self, pfad):
        self.protokoll.append(("mkdir", pfad))
        return self.mkdir_ok

    # ── Prüf-Helfer ──
    def gameplay(self):
        return json.loads(self.files[GAMEPLAY_PFAD])

    def eintraege(self):
        return self.gameplay()["WorldsData"]["objectSpawnersArr"]

    def adj_dateien(self):
        return sorted(p.rsplit("/", 1)[1] for p in self.files if "/custom/adj_" in p)

    def objekte(self, inst):
        return json.loads(self.files[f"{MISSION}/{inst['datei']}"])["Objects"]

    def schreibvorgaenge(self):
        return [p for art, p in self.protokoll if art in ("write", "delete")]


@pytest.fixture(autouse=True)
def _adj_sauber(monkeypatch, tmp_path, caplog):
    """Wegwerf-Arbeitsverzeichnis, frische Zähler/Caches. Nach dem Test: keine Datei im Repo."""
    vorher = {n for n in ("airdrop_json", "backups", "economy.db") if os.path.exists(os.path.join(REPO, n))}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    monkeypatch.setattr(bot, "_audit_log", collections.deque(maxlen=bot._AUDIT_MAX))
    monkeypatch.setattr(bot, "_ADJ_TASKS", {})
    monkeypatch.setattr(bot, "_ADJ_RETRY_AB", {})
    monkeypatch.setattr(bot.cfg, "config", {})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    caplog.set_level(logging.DEBUG)
    yield
    for n in ("airdrop_json", "backups"):
        if n not in vorher:
            assert not os.path.exists(os.path.join(REPO, n)), f"{n}/ ist im Repo entstanden"


@pytest.fixture
def env(monkeypatch, servers, tmp_path):
    """Mandanten 1000 (a) und 2000 (b) auf Chernarus, je eigener Fake-FTP, a an GID, b an GID_B."""
    a, b = servers
    for c in (a, b):
        c.data["map_name"] = "ChernarusPlus"
        c.ftp = FTP()
    bot.connections.add_guild("1000", GID)
    bot.connections.add_guild("2000", GID_B)
    monkeypatch.setattr(bot, "db", bot.EconomyDB(str(tmp_path / "e.db")))
    return SimpleNamespace(a=a, b=b)


def _premade(name="drop1", objekte=None):
    return bot._adj_speichern("premade", "", name, objekte or _objekte(), "tester")


def _eigen(conn, name="mein1", objekte=None):
    return bot._adj_speichern("eigen", conn.service_id, name, objekte or _objekte(), "tester")


def _platziere(conn, name="drop1", quelle="premade", x=5000, y=300, z=6000, restarts=1, von="befehl"):
    inst, fehler = run(bot._adj_platzieren(conn, quelle, name, x, y, z, restarts, von, "42"))
    assert fehler is None, fehler
    return inst


def _platte(service_id="1000"):
    """connections.json frisch von der Platte (flache Zuordnung)."""
    with open("connections.json", encoding="utf-8") as f:
        roh = json.load(f)
    assert service_id in roh and "1000" in roh          # flaches Format, nicht verschachtelt
    return roh[service_id]


def _ids(conn, von=None):
    return sorted(i["id"] for i in bot._adj_zustand(conn)["instanzen"] if von is None or i["von"] == von)


# ══════════════════════════════════════════════════════════════════════════
#  _adj_pruefen
# ══════════════════════════════════════════════════════════════════════════
def test_pruefen_gueltige_datei_wird_normalisiert():
    objekte, fehler = bot._adj_pruefen(_text())
    assert fehler is None and len(objekte) == 12
    assert objekte[0] == {"name": "Land_Container_1Moh_DE", "pos": [3005.0, 275.0, 12304.0],
                          "ypr": [90.0, 0.0, 0.0], "scale": 1.0, "enableCEPersistency": 0, "customString": ""}
    assert all(set(o) == {"name", "pos", "ypr", "scale", "enableCEPersistency", "customString"} for o in objekte)


def test_pruefen_fehlende_felder_bekommen_vorgaben_und_fremde_felder_fallen_weg():
    objekte, fehler = bot._adj_pruefen(json.dumps({"Objects": [{"name": "A", "pos": [1, 2, 3], "geheim": "x"}], "extra": 1}))
    assert fehler is None
    assert objekte == [{"name": "A", "pos": [1.0, 2.0, 3.0], "ypr": [0.0, 0.0, 0.0], "scale": 1.0,
                        "enableCEPersistency": 0, "customString": ""}]


@pytest.mark.parametrize("roh,erwartet", [(1, 1), (True, 1), (0, 0), (False, 0), (None, 0)])
def test_pruefen_enable_ce_persistency_wird_zu_null_oder_eins(roh, erwartet):
    objekte, fehler = bot._adj_pruefen(json.dumps({"Objects": [{"name": "A", "pos": [1, 2, 3], "enableCEPersistency": roh}]}))
    assert fehler is None and objekte[0]["enableCEPersistency"] == erwartet


@pytest.mark.parametrize("roh,erwartet", [("hallo", "hallo"), ("", ""), (5, ""), (None, ""), (["x"], ""), ({"a": 1}, "")])
def test_pruefen_custom_string_nur_text(roh, erwartet):
    objekte, fehler = bot._adj_pruefen(json.dumps({"Objects": [{"name": "A", "pos": [1, 2, 3], "customString": roh}]}))
    assert fehler is None and objekte[0]["customString"] == erwartet


def _eins(**felder):
    objekt = {"name": "A", "pos": [1, 2, 3]}
    objekt.update(felder)
    return json.dumps({"Objects": [objekt]})


@pytest.mark.parametrize("inhalt", [
    "", "kein json", "{", "[1, 2", "null", "42", '"text"', "[]", '[{"name":"A","pos":[1,2,3]}]',
    "[" * 200000,                                            # Rekursions-Bombe
    '{"a":' * 100000,
    '{"Objects": []}', '{"Objects": {}}', '{"Objects": "x"}', '{"Objects": null}', '{"objects": [{"name":"A","pos":[1,2,3]}]}',
    '{"Containers": [{"Items": []}]}', '{"Loot": []}', '{"AirdropSettings": {"Containers": [], "Loot": []}}',
    '{"Objects": [5]}', '{"Objects": ["A"]}', '{"Objects": [null]}', '{"Objects": [[]]}',
])
def test_pruefen_lehnt_ungueltige_dateien_ab(inhalt):
    objekte, fehler = bot._adj_pruefen(inhalt)
    assert objekte is None and isinstance(fehler, str) and fehler


def test_pruefen_expansion_einstellung_hat_eigene_meldung():
    _objekte_, fehler = bot._adj_pruefen('{"Containers": [], "Loot": []}')
    assert "Expansion" in fehler
    # Wer ein Feld „Objects“ mitbringt, ist kein Expansion-Format
    objekte, fehler = bot._adj_pruefen('{"Containers": [], "Objects": [{"name":"A","pos":[1,2,3]}]}')
    assert fehler is None and len(objekte) == 1


@pytest.mark.parametrize("name", [
    "", None, 5, ["A"], True, "a/b", "../x", "/abs", "a\\b", "C:\\x", "mit\nZeilenumbruch", "tab\tulator", "nul\x00", "x" * 201,
])
def test_pruefen_lehnt_ungueltige_namen_ab(name):
    objekte, fehler = bot._adj_pruefen(json.dumps({"Objects": [{"name": name, "pos": [1, 2, 3]}]}))
    assert objekte is None and "Namen" in fehler


def test_pruefen_name_ohne_feld_und_grenzlaenge():
    assert bot._adj_pruefen(json.dumps({"Objects": [{"pos": [1, 2, 3]}]}))[0] is None
    assert bot._adj_pruefen(_eins(name="x" * 200))[1] is None
    assert bot._adj_pruefen(_eins(name="x" * 201))[0] is None
    assert bot._adj_pruefen(_eins(name="Land_Ä_ü"))[1] is None            # Umlaute sind keine Steuerzeichen


@pytest.mark.parametrize("pos_text", [
    "[1, 2]", "[1, 2, 3, 4]", "[]", "1", '"1 2 3"', "null", "{}", '["1", "2", "3"]', "[true, 2, 3]", "[1, false, 3]",
    "[1, 2, null]", "[NaN, 2, 3]", "[1, Infinity, 3]", "[1, 2, -Infinity]", "[1e999, 2, 3]", "[1, 2, [3]]",
    "[10000000, 2, 3]", "[1, -10000000, 3]", "[1, 2, 99999999999]",
])
def test_pruefen_lehnt_ungueltige_positionen_ab(pos_text):
    objekte, fehler = bot._adj_pruefen('{"Objects": [{"name": "A", "pos": %s}]}' % pos_text)
    assert objekte is None and "pos" in fehler


def test_pruefen_position_fehlt_und_grenzwerte():
    assert bot._adj_pruefen('{"Objects": [{"name": "A"}]}')[0] is None
    objekte, fehler = bot._adj_pruefen('{"Objects": [{"name": "A", "pos": [9999999, -9999999, 0.5]}]}')
    assert fehler is None and objekte[0]["pos"] == [9999999.0, -9999999.0, 0.5]


def test_pruefen_riesige_ganzzahl_ist_ein_pruef_fehler_kein_absturz():
    """Eine Ganzzahl über Float-Bereich (aber unter Pythons Ziffern-Grenze) darf nicht als
    OverflowError durchschlagen, sondern muss wie jede andere ungültige Zahl abgelehnt werden."""
    gross = "1" + "0" * 400
    for feld in ('"pos": [%s, 2, 3]' % gross, '"pos": [1, 2, 3], "ypr": [%s, 0, 0]' % gross,
                 '"pos": [1, 2, 3], "scale": %s' % gross):
        objekte, fehler = bot._adj_pruefen('{"Objects": [{"name": "A", %s}]}' % feld)
        assert objekte is None and isinstance(fehler, str), feld


@pytest.mark.parametrize("ypr_text", ["[1, 2]", "[1, 2, 3, 4]", "[]", "5", '"x"', "{}", "[true, 0, 0]", '["1", 0, 0]', "[NaN, 0, 0]", "null"])
def test_pruefen_lehnt_ungueltiges_ypr_ab(ypr_text):
    objekte, fehler = bot._adj_pruefen('{"Objects": [{"name": "A", "pos": [1,2,3], "ypr": %s}]}' % ypr_text)
    assert objekte is None and "ypr" in fehler


@pytest.mark.parametrize("scale_text", ["0", "-1", "-0.5", "1000.5", "1001", '"1"', "true", "false", "null", "NaN", "[1]", "{}"])
def test_pruefen_lehnt_ungueltigen_scale_ab(scale_text):
    objekte, fehler = bot._adj_pruefen('{"Objects": [{"name": "A", "pos": [1,2,3], "scale": %s}]}' % scale_text)
    assert objekte is None and "scale" in fehler


def test_pruefen_scale_grenzen_und_vorgabe():
    assert bot._adj_pruefen(_eins(scale=1000))[0][0]["scale"] == 1000.0
    assert bot._adj_pruefen(_eins(scale=0.001))[0][0]["scale"] == 0.001
    assert bot._adj_pruefen(_eins())[0][0]["scale"] == 1.0


def test_pruefen_objektanzahl_grenzen():
    objekte, fehler = bot._adj_pruefen(_text(_viele(5000)))
    assert fehler is None and len(objekte) == 5000
    objekte, fehler = bot._adj_pruefen(_text(_viele(5001)))
    assert objekte is None and "5000" in fehler


def test_pruefen_dateigroesse_grenze_ist_5_megabyte():
    kern = _text(_viele(1))
    exakt = kern + " " * (5_000_000 - len(kern.encode()))
    assert len(exakt.encode()) == 5_000_000
    assert bot._adj_pruefen(exakt)[1] is None
    objekte, fehler = bot._adj_pruefen(exakt + " ")
    assert objekte is None and "5 MB" in fehler
    # Mehrbyte-Zeichen zählen als Bytes, nicht als Zeichen
    objekte, fehler = bot._adj_pruefen(kern + "ä" * 2_600_000)
    assert objekte is None and "5 MB" in fehler


def test_pruefen_warnung_ab_200_objekten_ist_kein_fehler():
    for n, warnung in ((199, False), (200, True), (250, True)):
        objekte, fehler = bot._adj_pruefen(_text(_viele(n)))
        assert fehler is None and len(objekte) == n
        assert bot._adj_meta("x", objekte, 10, "u")["warnung"] is warnung


def test_pruefen_veraendert_die_eingabe_nicht_und_ist_wiederholbar():
    text = _text()
    erst = bot._adj_pruefen(text)
    assert bot._adj_pruefen(text) == erst
    # normalisierte Ausgabe ist selbst wieder gültig und stabil
    assert bot._adj_pruefen(json.dumps({"Objects": erst[0]}))[0] == erst[0]


# ══════════════════════════════════════════════════════════════════════════
#  Verschieben
# ══════════════════════════════════════════════════════════════════════════
def _rahmen(objekte):
    xs, ys, zs = ([o["pos"][i] for o in objekte] for i in range(3))
    return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)


def test_verschieben_mitte_und_tiefster_punkt_liegen_auf_dem_ziel():
    alt = _objekte()
    neu = bot._adj_verschieben(alt, 5000.0, 300.0, 6000.0)
    x0, x1, y0, _y1, z0, z1 = _rahmen(neu)
    assert (x0 + x1) / 2 == 5000.0 and (z0 + z1) / 2 == 6000.0 and y0 == 300.0
    assert (x0, x1, z0, z1) == (4974.0, 5026.0, 5969.5, 6030.5)


def test_verschieben_abstaende_hoehen_ypr_scale_bleiben():
    alt = _objekte()
    neu = bot._adj_verschieben(alt, 5000.0, 300.0, 6000.0)
    for a, n in zip(alt, neu):
        assert n["pos"][0] - a["pos"][0] == pytest.approx(5000.0 - 3031.0, abs=1e-3)
        assert n["pos"][1] - a["pos"][1] == pytest.approx(300.0 - 275.0, abs=1e-3)
        assert n["pos"][2] - a["pos"][2] == pytest.approx(6000.0 - 12334.5, abs=1e-3)
        for k in ("name", "ypr", "scale", "enableCEPersistency", "customString"):
            assert n[k] == a[k]
    # Höhenunterschiede untereinander unverändert
    assert [n["pos"][1] - neu[0]["pos"][1] for n in neu] == [a["pos"][1] - alt[0]["pos"][1] for a in alt]


def test_verschieben_veraendert_die_eingabe_nicht_und_teilt_keine_listen():
    alt = _objekte()
    kopie = copy.deepcopy(alt)
    neu = bot._adj_verschieben(alt, 1.0, 2.0, 3.0)
    assert alt == kopie
    neu[0]["ypr"][0] = 999.0
    neu[0]["pos"][0] = -5.0
    assert alt == kopie


def test_verschieben_rundet_auf_vier_stellen():
    objekte = [{"name": "A", "pos": [0.0, 0.0, 0.0], "ypr": [0.0, 0.0, 0.0], "scale": 1.0, "enableCEPersistency": 0, "customString": ""},
               {"name": "B", "pos": [1.0, 1.0, 1.0], "ypr": [0.0, 0.0, 0.0], "scale": 1.0, "enableCEPersistency": 0, "customString": ""}]
    neu = bot._adj_verschieben(objekte, 100.123456789, 7.777777777, 55.55555555)
    for o in neu:
        assert all(round(v, 4) == v for v in o["pos"]), o["pos"]
    assert neu[0]["pos"][1] == 7.7778


def test_verschieben_einzelnes_objekt_liegt_genau_auf_dem_ziel():
    objekte = [{"name": "A", "pos": [10.0, 20.0, 30.0], "ypr": [1.0, 2.0, 3.0], "scale": 2.0, "enableCEPersistency": 1, "customString": "x"}]
    assert bot._adj_verschieben(objekte, 500.0, 40.0, 600.0)[0]["pos"] == [500.0, 40.0, 600.0]


def test_verschieben_ist_umkehrbar_ohne_drift():
    alt = _objekte()
    hin = bot._adj_verschieben(alt, 7000.0, 120.0, 8000.0)
    zurueck = bot._adj_verschieben(hin, 3031.0, 275.0, 12334.5)
    assert [o["pos"] for o in zurueck] == [o["pos"] for o in alt]


# ══════════════════════════════════════════════════════════════════════════
#  _adj_bauen (Ziel und Kartengrenzen)
# ══════════════════════════════════════════════════════════════════════════
def test_bauen_gueltiges_ziel(env):
    neu, fehler = bot._adj_bauen(env.a, _objekte(), 5000, 300, 6000)
    assert fehler is None and len(neu) == 12
    assert _rahmen(neu)[2] == 300.0


@pytest.mark.parametrize("x,y,z", [("5000", 300, 6000), (5000, None, 6000), (5000, 300, True), (float("nan"), 300, 6000),
                                   (5000, float("inf"), 6000), ([1], 300, 6000), (5000, 300, "abc")])
def test_bauen_koordinaten_muessen_zahlen_sein(env, x, y, z):
    neu, fehler = bot._adj_bauen(env.a, _objekte(), x, y, z)
    assert neu is None and "Zahlen" in fehler


@pytest.mark.parametrize("x,z", [(-1, 6000), (15361, 6000), (5000, -0.5), (5000, 15360.5), (1e6, 6000)])
def test_bauen_ziel_ausserhalb_der_karte(env, x, z):
    neu, fehler = bot._adj_bauen(env.a, _objekte(), x, 300, z)
    assert neu is None and "außerhalb der Karte" in fehler


@pytest.mark.parametrize("y,gueltig", [(-1000, True), (10000, True), (-1000.1, False), (10000.1, False), (-5000, False)])
def test_bauen_hoehengrenzen(env, y, gueltig):
    neu, fehler = bot._adj_bauen(env.a, _objekte(), 5000, y, 6000)
    assert (fehler is None) == gueltig
    if not gueltig:
        assert "Höhe" in fehler


def test_bauen_kein_objekt_darf_ausserhalb_der_karte_landen(env):
    # Ziel liegt in der Karte, aber die Anlage (Breite 52 / Tiefe 61) ragt über den Rand
    for x, z in ((10, 6000), (5000, 10), (15350, 6000), (5000, 15350)):
        neu, fehler = bot._adj_bauen(env.a, _objekte(), x, 300, z)
        assert neu is None and "Teilen außerhalb" in fehler, (x, z)
    # knapp genug innen geht
    assert bot._adj_bauen(env.a, _objekte(), 26, 300, 31)[1] is None
    assert bot._adj_bauen(env.a, _objekte(), 25.9, 300, 31)[1] is not None


def test_bauen_kartengroesse_je_karte(env):
    assert bot._adj_kartengroesse(env.a) == 15360
    env.a.data["map_name"] = "Livonia"
    assert bot._adj_kartengroesse(env.a) == 12800
    assert bot._adj_bauen(env.a, _objekte(), 13000, 300, 6000)[0] is None
    assert bot._adj_bauen(env.a, _objekte(), 12000, 300, 6000)[1] is None
    env.a.data["map_name"] = "Unbekannte_Karte"
    assert bot._adj_kartengroesse(env.a) == 20480
    assert bot._adj_bauen(env.a, _objekte(), 20000, 300, 6000)[1] is None
    env.a.data["map_name"] = ""
    assert bot._adj_kartengroesse(env.a) == 20480


# ══════════════════════════════════════════════════════════════════════════
#  Dateispeicher
# ══════════════════════════════════════════════════════════════════════════
def test_speichern_und_laden_rundreise(env):
    meta = _premade("drop1")
    assert meta["name"] == "drop1" and meta["objekte"] == 12 and meta["bytes"] > 100
    assert (meta["breite_x"], meta["breite_z"], meta["hoehe"]) == (52.0, 61.0, 12.0)
    assert meta["warnung"] is False and meta["von"] == "tester" and meta["hochgeladen"]
    assert bot._adj_laden("premade", "", "drop1") == _objekte()
    assert os.path.isfile(os.path.join("airdrop_json", "premade", "drop1.json"))
    assert json.load(open(os.path.join("airdrop_json", "premade", ".index.json")))["drop1"]["objekte"] == 12


def test_speichern_schreibt_gueltiges_object_spawner_json_und_haelt_keine_tmp_dateien(env):
    _premade("drop1")
    _eigen(env.a, "mein1")
    roh = json.load(open(os.path.join("airdrop_json", "premade", "drop1.json")))
    assert list(roh) == ["Objects"] and bot._adj_pruefen(json.dumps(roh))[1] is None
    for wurzel, _d, namen in os.walk("airdrop_json"):
        assert not [n for n in namen if n.endswith(".tmp")], namen


def test_liste_ist_nach_namen_ohne_gross_klein_sortiert(env):
    for n in ("zeta", "Alpha", "beta", "Gamma_2"):
        _premade(n)
    assert [m["name"] for m in bot._adj_liste("premade")] == ["Alpha", "beta", "Gamma_2", "zeta"]
    assert bot._adj_liste("eigen", "1000") == []


def test_liste_zeigt_nur_dateien_die_wirklich_existieren(env):
    _premade("da")
    _premade("weg")
    os.remove(os.path.join("airdrop_json", "premade", "weg.json"))
    with open(os.path.join("airdrop_json", "premade", "ohneindex.json"), "w") as f:
        f.write(_text())
    assert [m["name"] for m in bot._adj_liste("premade")] == ["da"]


def test_kaputter_oder_fremder_index_wird_ignoriert_und_repariert(env):
    _premade("a1")
    index = os.path.join("airdrop_json", "premade", ".index.json")
    for inhalt in ("{kaputt", "[]", "null", '"x"'):
        with open(index, "w") as f:
            f.write(inhalt)
        assert bot._adj_liste("premade") == []
    _premade("a2")
    assert [m["name"] for m in bot._adj_liste("premade")] == ["a2"]
    with open(index, "w") as f:                                # Einträge mit ungültigem Namen/Typ fliegen raus
        json.dump({"../evil": {"name": "x"}, "ok": "kein dict", "a2": {"name": "a2"}}, f)
    assert [m["name"] for m in bot._adj_liste("premade")] == ["a2"]


def test_ueberschreiben_ersetzt_inhalt_und_meta(env):
    _premade("drop1")
    kleiner = _objekte()[:3]
    meta = _premade("drop1", kleiner)
    assert meta["objekte"] == 3 and len(bot._adj_laden("premade", "", "drop1")) == 3
    assert [m["objekte"] for m in bot._adj_liste("premade")] == [3]


@pytest.mark.parametrize("name", ["", " ", "a b", "ä", "a.b", "a/b", "../x", "a\\b", "x" * 41, ".", "..", "a:b"])
def test_speichern_lehnt_ungueltige_namen_ab(env, name):
    with pytest.raises(ValueError):
        bot._adj_speichern("premade", "", name, _objekte(), "t")
    assert not os.path.exists("airdrop_json")


@pytest.mark.parametrize("name", ["a\n", "drop1\n", "\n", "a\r\n"])
def test_namen_mit_zeilenumbruch_am_ende_sind_ungueltig(env, name):
    """`^…$` lässt vor einem abschließenden Zeilenumbruch noch einen Treffer zu - der Name
    „a\\n“ ist aber kein Name aus [A-Za-z0-9_-]."""
    assert bot._ADJ_NAME_RE.fullmatch(name) is None
    with pytest.raises(ValueError):
        bot._adj_speichern("premade", "", name, _objekte(), "t")
    assert not os.path.exists("airdrop_json")
    assert bot._adj_laden("premade", "", name) is None
    ok, _ = bot._adj_scheduler_validieren({"anzahl": 1, "alle_neustarts": 1, "airdrops": [{"quelle": "premade", "name": name}]})
    assert ok is None


def test_namen_grenzen_40_zeichen_und_erlaubte_zeichen(env):
    for n in ("x" * 40, "A-b_9", "0"):
        _premade(n)
        assert bot._adj_laden("premade", "", n) is not None


@pytest.mark.parametrize("name", ["../x", "a/b", "..", ".", "", "a b", "x" * 41, "..\\x", "drop1.json", None])
def test_laden_und_loeschen_lehnen_pfad_injektion_ab(env, name):
    _premade("drop1")
    _eigen(env.a, "mein1")
    with open("geheim.json", "w") as f:
        f.write(_text())                                       # liegt AUSSERHALB des Speichers
    os.makedirs("airdrop_json/eigene/1000/sub", exist_ok=True)
    assert bot._adj_laden("premade", "", name) is None
    assert bot._adj_laden("eigen", "1000", name) is None
    assert bot._adj_loeschen("premade", "", name) is False
    assert bot._adj_loeschen("eigen", "1000", name) is False
    assert os.path.exists("geheim.json") and bot._adj_laden("premade", "", "drop1") is not None
    assert bot._adj_laden("eigen", "1000", "mein1") is not None


def test_pfad_injektion_ueber_die_service_id_bleibt_im_speicher(env):
    objekte = _objekte()
    with open("geheim.json", "w") as f:
        f.write(_text())
    for sid in ("../..", "../../x", "a/b", "..\\x", "/etc", "", "x" * 100):
        bot._adj_speichern("eigen", sid, "gefaengnis", objekte, "t")
    for wurzel, _d, namen in os.walk("."):
        for n in namen:
            if n.startswith("gefaengnis") or n == ".index.json":
                assert os.path.abspath(wurzel).startswith(os.path.abspath("airdrop_json")), wurzel
    assert not os.path.exists("gefaengnis.json") and not os.path.exists(os.path.join("..", "gefaengnis.json"))
    assert open("geheim.json").read() == _text()


def test_laden_ungueltige_quelle_und_kaputte_oder_fehlende_datei(env):
    _premade("drop1")
    assert bot._adj_laden("sonstwas", "", "drop1") is None
    assert bot._adj_laden("premade", "", "gibtsnicht") is None
    with open(os.path.join("airdrop_json", "premade", "kaputt.json"), "w") as f:
        f.write("{kaputt")
    with open(os.path.join("airdrop_json", "premade", "leer.json"), "w") as f:
        f.write('{"Objects": []}')
    assert bot._adj_laden("premade", "", "kaputt") is None
    assert bot._adj_laden("premade", "", "leer") is None
    assert bot._adj_loeschen("sonstwas", "", "drop1") is False
    assert bot._adj_laden("premade", "", "drop1") is not None


def test_loeschen_entfernt_datei_und_index_eintrag(env):
    _premade("drop1")
    _premade("drop2")
    assert bot._adj_loeschen("premade", "", "drop1") is True
    assert bot._adj_laden("premade", "", "drop1") is None and [m["name"] for m in bot._adj_liste("premade")] == ["drop2"]
    assert "drop1" not in json.load(open(os.path.join("airdrop_json", "premade", ".index.json")))
    assert bot._adj_loeschen("premade", "", "drop1") is False             # zweites Mal: gibt es nicht mehr


def test_mandantentrennung_eigene_dateien_sind_pro_server(env):
    _eigen(env.a, "geheim")
    _eigen(env.a, "nurba")
    _eigen(env.b, "bsache")
    assert [m["name"] for m in bot._adj_liste("eigen", "1000")] == ["geheim", "nurba"]
    assert [m["name"] for m in bot._adj_liste("eigen", "2000")] == ["bsache"]
    assert bot._adj_laden("eigen", "1000", "geheim") is not None
    assert bot._adj_laden("eigen", "2000", "geheim") is None
    assert bot._adj_loeschen("eigen", "2000", "geheim") is False
    assert bot._adj_laden("eigen", "1000", "geheim") is not None          # blieb unangetastet
    assert bot._adj_laden("premade", "", "geheim") is None                # eigene sind nie Premade
    assert os.path.dirname(bot._adj_verz("eigen", "1000")) == os.path.dirname(bot._adj_verz("eigen", "2000"))
    assert bot._adj_verz("eigen", "1000") != bot._adj_verz("eigen", "2000")


def test_premade_ist_global_und_gleicher_name_in_beiden_quellen_ist_getrennt(env):
    _premade("gleich", _objekte()[:2])
    _eigen(env.a, "gleich", _objekte()[:5])
    assert len(bot._adj_laden("premade", "", "gleich")) == 2
    assert len(bot._adj_laden("eigen", "1000", "gleich")) == 5
    assert bot._adj_laden("eigen", "2000", "gleich") is None
    assert [m["name"] for m in bot._adj_liste("premade", "2000")] == ["gleich"]   # Premade sieht jeder Server


# ══════════════════════════════════════════════════════════════════════════
#  Zustand je Server
# ══════════════════════════════════════════════════════════════════════════
def test_zustand_vorgaben(env):
    z = bot._adj_zustand(env.a)
    assert z["instanzen"] == [] and z["neustarts_offen"] == 0
    assert z["scheduler"] == {"aktiv": False, "anzahl": 1, "alle_neustarts": 1, "zaehler": 0, "airdrops": [], "positionen": []}
    assert env.a.data["airdrop_json"] is z


def test_zustand_repariert_kaputte_werte(env):
    env.a.data["airdrop_json"] = {"instanzen": "x", "scheduler": [1], "neustarts_offen": -3}
    z = bot._adj_zustand(env.a)
    assert z["instanzen"] == [] and z["scheduler"]["aktiv"] is False and z["neustarts_offen"] == 0
    env.a.data["airdrop_json"] = {"scheduler": {"airdrops": None, "positionen": "x", "aktiv": True}, "neustarts_offen": None}
    z = bot._adj_zustand(env.a)
    assert z["scheduler"]["airdrops"] == [] and z["scheduler"]["positionen"] == [] and z["neustarts_offen"] == 0
    env.a.data["airdrop_json"] = "kaputt"
    assert bot._adj_zustand(env.a)["instanzen"] == []


def test_zustand_hat_keine_rueckfallebene_auf_die_globale_config(env):
    assert "airdrop_json" in bot.ServerConnection._KEINE_RUECKFALL_SCHLUESSEL
    fremd = {"instanzen": [{"id": "abcdef", "name": "fremd"}], "scheduler": {"aktiv": True}}
    bot.cfg.config["airdrop_json"] = fremd                   # Betreiber-Config enthält etwas
    assert env.b.get("airdrop_json") is None
    z = bot._adj_zustand(env.b)
    assert z["instanzen"] == [] and z["scheduler"]["aktiv"] is False
    assert _ids(env.a) == []


def test_zustand_wird_beim_hauptserver_nicht_nach_config_gespiegelt(env):
    assert bot.connections.primary() is env.a              # 1000 ist der Hauptserver
    _premade()
    _platziere(env.a, restarts=2)
    assert "airdrop_json" not in bot.cfg.config
    assert not os.path.exists("config.json") or "airdrop_json" not in open("config.json").read()
    bot._conn_store(env.a, "airdrop_json", {"instanzen": [], "scheduler": {}})
    assert "airdrop_json" not in bot.cfg.config
    assert not os.path.exists("config.json") or "airdrop_json" not in open("config.json").read()


def test_zustand_wird_in_connections_json_gespeichert_und_ueberlebt_neuladen(env):
    _premade()
    inst = _platziere(env.a, restarts=3)
    gespeichert = _platte("1000")["airdrop_json"]
    assert [i["id"] for i in gespeichert["instanzen"]] == [inst["id"]]
    assert "airdrop_json" not in _platte("2000")
    neu = bot.ConnectionRegistry()
    neu.load()
    z = bot._adj_zustand(neu._conns["1000"])
    assert z["instanzen"][0]["name"] == "drop1" and z["instanzen"][0]["restarts"] == 3
    assert bot._adj_zustand(neu._conns["2000"])["instanzen"] == []


# ══════════════════════════════════════════════════════════════════════════
#  Auf den Server schreiben: Platzieren / Entfernen / Transaktion
# ══════════════════════════════════════════════════════════════════════════
def test_platzieren_schreibt_datei_und_gameplay_eintrag_und_zustand(env):
    _premade()
    inst = _platziere(env.a, x=5000, y=300, z=6000, restarts=2)
    assert len(inst["id"]) == 6 and all(c in "0123456789abcdef" for c in inst["id"])
    assert inst["datei"] == f"custom/adj_{inst['id']}.json"
    assert (inst["name"], inst["quelle"], inst["x"], inst["y"], inst["z"]) == ("drop1", "premade", 5000.0, 300.0, 6000.0)
    assert (inst["restarts"], inst["gesehen"], inst["von"], inst["objekte"]) == (2, 0, "befehl", 12)
    # Datei auf dem Server: verschobene Objekte, Mitte auf (x,z), tiefster Punkt auf y
    objekte = env.a.ftp.objekte(inst)
    x0, x1, y0, _y1, z0, z1 = _rahmen(objekte)
    assert ((x0 + x1) / 2, (z0 + z1) / 2, y0) == (5000.0, 6000.0, 300.0)
    assert [o["name"] for o in objekte] == [o["name"] for o in _objekte()]
    # cfggameplay.json: neuer Eintrag, fremder Eintrag und übrige Einstellungen bleiben
    gp = env.a.ftp.gameplay()
    assert gp["WorldsData"]["objectSpawnersArr"] == ["custom/alt.json", inst["datei"]]
    assert gp["version"] == 123 and gp["WorldsData"]["andere"] == {"a": 1}
    # Zustand im Speicher und auf der Platte
    assert bot._adj_zustand(env.a)["instanzen"] == [inst] and _platte("1000")["airdrop_json"]["instanzen"] == [inst]
    # Der andere Server wurde nicht angefasst
    assert env.b.ftp.protokoll == [] and bot._adj_zustand(env.b)["instanzen"] == []


def test_platzieren_legt_den_ordner_custom_an_und_veraendert_die_quelldatei_nicht(env):
    _premade()
    vorher = open(os.path.join("airdrop_json", "premade", "drop1.json")).read()
    _platziere(env.a)
    assert ("mkdir", f"{MISSION}/custom") in env.a.ftp.protokoll
    assert open(os.path.join("airdrop_json", "premade", "drop1.json")).read() == vorher


@pytest.mark.parametrize("gameplay,erwartet", [
    ({"version": 1}, {"version": 1, "WorldsData": {"objectSpawnersArr": ["NEU"]}}),
    ({"WorldsData": {}}, {"WorldsData": {"objectSpawnersArr": ["NEU"]}}),
    ({"WorldsData": {"x": 1}}, {"WorldsData": {"x": 1, "objectSpawnersArr": ["NEU"]}}),
    ({"WorldsData": {"objectSpawnersArr": []}}, {"WorldsData": {"objectSpawnersArr": ["NEU"]}}),
    ({"WorldsData": {"objectSpawnersArr": None}}, {"WorldsData": {"objectSpawnersArr": ["NEU"]}}),
])
def test_platzieren_legt_fehlende_objectSpawnersArr_an(env, gameplay, erwartet):
    env.a.ftp = FTP(gameplay)
    _premade()
    inst = _platziere(env.a)
    assert env.a.ftp.gameplay() == json.loads(json.dumps(erwartet).replace("NEU", inst["datei"]))


def test_platzieren_mehrere_airdrops_behalten_alle_fremden_eintraege(env):
    env.a.ftp = FTP({"WorldsData": {"objectSpawnersArr": ["custom/alt.json", "./custom/andere.json", 7, "custom/adj_x.json"]}})
    _premade()
    a1 = _platziere(env.a, x=4000, z=4000)
    a2 = _platziere(env.a, x=9000, z=9000)
    eintraege = env.a.ftp.eintraege()
    assert eintraege[:4] == ["custom/alt.json", "./custom/andere.json", 7, "custom/adj_x.json"]     # fremde bleiben unverändert (auch Nicht-Texte)
    assert eintraege[4:] == [a1["datei"], a2["datei"]]
    assert env.a.ftp.adj_dateien() == sorted([a1["datei"].split("/")[1], a2["datei"].split("/")[1]])


def test_platzieren_ohne_ftp_oder_mission_ordner_meldet_ftp_fehler(env):
    _premade()
    env.a.ftp = None
    inst, fehler = run(bot._adj_platzieren(env.a, "premade", "drop1", 5000, 300, 6000, 1, "befehl", "42"))
    assert inst is None and fehler == bot._ADJ_FEHLER_FTP and bot._adj_zustand(env.a)["instanzen"] == []
    env.a.ftp = FTP()
    env.a.data["ftp_mission_dir"] = ""
    inst, fehler = run(bot._adj_platzieren(env.a, "premade", "drop1", 5000, 300, 6000, 1, "befehl", "42"))
    assert inst is None and fehler == bot._ADJ_FEHLER_FTP and env.a.ftp.protokoll == []


@pytest.mark.parametrize("name,x,y,z,restarts,teil", [
    ("gibtsnicht", 5000, 300, 6000, 1, "gibt es nicht"),
    ("drop1", -5, 300, 6000, 1, "außerhalb der Karte"),
    ("drop1", 20000, 300, 6000, 1, "außerhalb der Karte"),
    ("drop1", 10, 300, 6000, 1, "Teilen außerhalb"),
    ("drop1", 5000, 20000, 6000, 1, "Höhe"),
    ("drop1", 5000, 300, 6000, 0, "1 und 100"),
    ("drop1", 5000, 300, 6000, 101, "1 und 100"),
    ("drop1", "abc", 300, 6000, 1, "Zahlen"),
])
def test_platzieren_fehlerfaelle_schreiben_nichts(env, name, x, y, z, restarts, teil):
    _premade()
    inst, fehler = run(bot._adj_platzieren(env.a, "premade", name, x, y, z, restarts, "befehl", "42"))
    assert inst is None and teil in fehler
    assert env.a.ftp.protokoll == [] and bot._adj_zustand(env.a)["instanzen"] == []


def test_platzieren_hoechstens_20_instanzen(env):
    _premade()
    for i in range(20):
        _platziere(env.a, x=1000 + 100 * i, z=1000)
    assert len(bot._adj_zustand(env.a)["instanzen"]) == 20
    schreib = len(env.a.ftp.protokoll)
    inst, fehler = run(bot._adj_platzieren(env.a, "premade", "drop1", 9000, 300, 9000, 1, "befehl", "42"))
    assert inst is None and "höchstens 20" in fehler and len(env.a.ftp.protokoll) == schreib
    assert len(env.a.ftp.eintraege()) == 21 and len(env.a.ftp.adj_dateien()) == 20
    assert len(set(_ids(env.a))) == 20


def test_platzieren_quelle_eigen_nimmt_nur_dateien_des_eigenen_servers(env):
    _eigen(env.b, "nurb")
    _premade("gemeinsam")
    inst, fehler = run(bot._adj_platzieren(env.a, "eigen", "nurb", 5000, 300, 6000, 1, "befehl", "42"))
    assert inst is None and "gibt es nicht" in fehler
    assert _platziere(env.a, name="gemeinsam")["quelle"] == "premade"
    assert _platziere(env.b, name="nurb", quelle="eigen")["quelle"] == "eigen"


def test_platzieren_schreibfehler_der_datei_rollt_zurueck(env):
    _premade()
    env.a.ftp.write_fehler = {"custom/adj_"}
    inst, fehler = run(bot._adj_platzieren(env.a, "premade", "drop1", 5000, 300, 6000, 1, "befehl", "42"))
    assert inst is None and "fehlgeschlagen" in fehler and "nichts wurde geändert" in fehler
    assert env.a.ftp.adj_dateien() == [] and env.a.ftp.eintraege() == ["custom/alt.json"]
    assert bot._adj_zustand(env.a)["instanzen"] == []


def test_platzieren_schreibfehler_der_cfggameplay_loescht_die_neue_datei_wieder(env):
    _premade()
    env.a.ftp.write_fehler = {"cfggameplay.json"}
    vorher = env.a.ftp.files[GAMEPLAY_PFAD]
    inst, fehler = run(bot._adj_platzieren(env.a, "premade", "drop1", 5000, 300, 6000, 1, "befehl", "42"))
    assert inst is None and "fehlgeschlagen" in fehler
    assert env.a.ftp.adj_dateien() == []                       # Rollback der neuen Datei
    assert env.a.ftp.files[GAMEPLAY_PFAD] == vorher
    assert bot._adj_zustand(env.a)["instanzen"] == [] and _platte("1000").get("airdrop_json", {}).get("instanzen", []) == []
    # nach der Reparatur klappt es wieder
    env.a.ftp.write_fehler = set()
    assert _platziere(env.a)["id"] in env.a.ftp.eintraege()[1]


@pytest.mark.parametrize("gameplay_text", ["{kaputt", "[1, 2]", '"x"'])
def test_platzieren_kaputte_oder_fremde_cfggameplay_bricht_ab(env, gameplay_text):
    _premade()
    env.a.ftp.files[GAMEPLAY_PFAD] = gameplay_text
    inst, fehler = run(bot._adj_platzieren(env.a, "premade", "drop1", 5000, 300, 6000, 1, "befehl", "42"))
    assert inst is None and "fehlgeschlagen" in fehler
    assert env.a.ftp.files[GAMEPLAY_PFAD] == gameplay_text and env.a.ftp.adj_dateien() == []


def test_platzieren_fehlende_cfggameplay_bricht_ab_und_raeumt_auf(env):
    _premade()
    del env.a.ftp.files[GAMEPLAY_PFAD]
    inst, fehler = run(bot._adj_platzieren(env.a, "premade", "drop1", 5000, 300, 6000, 1, "befehl", "42"))
    assert inst is None and "fehlgeschlagen" in fehler and env.a.ftp.adj_dateien() == []


def test_platzieren_ordner_custom_nicht_anlegbar(env):
    _premade()
    env.a.ftp.mkdir_ok = False
    inst, fehler = run(bot._adj_platzieren(env.a, "premade", "drop1", 5000, 300, 6000, 1, "befehl", "42"))
    assert inst is None and "Ordner custom" in fehler and env.a.ftp.schreibvorgaenge() == []


def test_entfernen_traegt_erst_aus_und_loescht_dann_die_datei(env):
    _premade()
    inst = _platziere(env.a)
    env.a.ftp.protokoll.clear()
    assert run(bot._adj_entfernen(env.a, [inst["id"]])) is None
    assert env.a.ftp.eintraege() == ["custom/alt.json"] and env.a.ftp.adj_dateien() == []
    assert env.a.ftp.protokoll == [("write", GAMEPLAY_PFAD), ("delete", f"{MISSION}/{inst['datei']}")]
    assert bot._adj_zustand(env.a)["instanzen"] == [] and _platte("1000")["airdrop_json"]["instanzen"] == []
    assert f"{MISSION}/custom/alt.json" in env.a.ftp.files                         # fremde Datei bleibt


def test_entfernen_gameplay_schreibfehler_laesst_alles_wie_es_war(env):
    _premade()
    inst = _platziere(env.a)
    env.a.ftp.write_fehler = {"cfggameplay.json"}
    fehler = run(bot._adj_entfernen(env.a, [inst["id"]]))
    assert fehler and "fehlgeschlagen" in fehler
    assert env.a.ftp.eintraege() == ["custom/alt.json", inst["datei"]]
    assert env.a.ftp.adj_dateien() == [inst["datei"].split("/")[1]]                # Datei NICHT gelöscht, solange der Eintrag steht
    assert _ids(env.a) == [inst["id"]]


def test_entfernen_ist_erfolgreich_auch_wenn_das_loeschen_der_datei_scheitert(env):
    _premade()
    inst = _platziere(env.a)
    env.a.ftp.delete_ok = False
    assert run(bot._adj_entfernen(env.a, [inst["id"]])) is None
    assert env.a.ftp.eintraege() == ["custom/alt.json"] and _ids(env.a) == []


def test_entfernen_unbekannte_ids_tun_nichts(env):
    _premade()
    inst = _platziere(env.a)
    env.a.ftp.protokoll.clear()
    assert run(bot._adj_entfernen(env.a, ["ffffff", "000000"])) is None and run(bot._adj_entfernen(env.a, [])) is None
    assert env.a.ftp.protokoll == [] and _ids(env.a) == [inst["id"]]


def test_entfernen_findet_auch_eintraege_mit_punkt_slash_schreibweise(env):
    _premade()
    inst = _platziere(env.a)
    gp = env.a.ftp.gameplay()
    gp["WorldsData"]["objectSpawnersArr"] = ["./custom/alt.json", "./" + inst["datei"]]
    env.a.ftp.files[GAMEPLAY_PFAD] = json.dumps(gp)
    assert run(bot._adj_entfernen(env.a, [inst["id"]])) is None
    assert env.a.ftp.eintraege() == ["./custom/alt.json"]


def test_entfernen_mehrerer_instanzen_aendert_die_cfggameplay_nur_einmal(env):
    _premade()
    a1, a2, a3 = (_platziere(env.a, x=3000 + 1000 * i) for i in range(3))
    env.a.ftp.protokoll.clear()
    assert run(bot._adj_entfernen(env.a, [a1["id"], a3["id"]])) is None
    assert env.a.ftp.protokoll.count(("write", GAMEPLAY_PFAD)) == 1
    assert env.a.ftp.eintraege() == ["custom/alt.json", a2["datei"]] and _ids(env.a) == [a2["id"]]


def test_transaktion_raeumt_verwaiste_adj_eintraege_mit_auf(env):
    """adj_*-Einträge, die zu keiner bekannten Instanz mehr gehören (Absturz, manuell kopiert),
    verschwinden bei der nächsten Änderung; fremde Einträge und Instanzen der Liste bleiben."""
    _premade()
    behalten = _platziere(env.a, x=3000)
    gp = env.a.ftp.gameplay()
    gp["WorldsData"]["objectSpawnersArr"] += ["custom/adj_aaaaaa.json", "./custom/adj_bbbbbb.json", "custom/adj_nichthex.json",
                                              "custom/adj_ab12.json", "custom/adj_aaaaaa.json.bak", "custom/fremd.json"]
    env.a.ftp.files[GAMEPLAY_PFAD] = json.dumps(gp)
    neu = _platziere(env.a, x=9000)
    assert env.a.ftp.eintraege() == ["custom/alt.json", behalten["datei"], "custom/adj_nichthex.json", "custom/adj_ab12.json",
                                     "custom/adj_aaaaaa.json.bak", "custom/fremd.json", neu["datei"]]


def test_transaktion_bleibende_ids_schuetzen_instanzen_vor_dem_aufraeumen(env):
    _premade()
    a1 = _platziere(env.a, x=3000)
    a2 = _platziere(env.a, x=9000)
    assert run(bot._adj_entfernen(env.a, [a1["id"]])) is None
    assert env.a.ftp.eintraege() == ["custom/alt.json", a2["datei"]]


def test_transaktion_ohne_aenderung_schreibt_die_cfggameplay_nicht(env):
    run(bot._adj_transaktion(env.a, [], [], set()))
    assert ("write", GAMEPLAY_PFAD) not in env.a.ftp.protokoll
    assert env.a.ftp.eintraege() == ["custom/alt.json"]


def test_transaktion_direkt_mit_mehreren_neuen_dateien_rollt_alle_zurueck(env):
    objekte = _objekte()
    env.a.ftp.write_fehler = {"cfggameplay.json"}
    fehler = run(bot._adj_transaktion(env.a, [("aaaaaa", objekte), ("bbbbbb", objekte)], [], {"aaaaaa", "bbbbbb"}))
    assert fehler and env.a.ftp.adj_dateien() == []
    env.a.ftp.write_fehler = {"adj_bbbbbb"}
    fehler = run(bot._adj_transaktion(env.a, [("aaaaaa", objekte), ("bbbbbb", objekte)], [], {"aaaaaa", "bbbbbb"}))
    assert fehler and env.a.ftp.adj_dateien() == [] and env.a.ftp.eintraege() == ["custom/alt.json"]
    env.a.ftp.write_fehler = set()
    assert run(bot._adj_transaktion(env.a, [("aaaaaa", objekte), ("bbbbbb", objekte)], [], {"aaaaaa", "bbbbbb"})) is None
    assert env.a.ftp.eintraege() == ["custom/alt.json", "custom/adj_aaaaaa.json", "custom/adj_bbbbbb.json"]
    # Wiederholung desselben Eintrags erzeugt keine Dublette
    assert run(bot._adj_transaktion(env.a, [("aaaaaa", objekte)], [], {"aaaaaa", "bbbbbb"})) is None
    assert env.a.ftp.eintraege().count("custom/adj_aaaaaa.json") == 1


def test_beschreibt_nur_den_eigenen_server_bei_zwei_servern_mit_gleichen_namen(env):
    _premade()
    ia = _platziere(env.a, x=4000)
    ib = _platziere(env.b, x=8000)
    assert env.a.ftp.adj_dateien() == [f"adj_{ia['id']}.json"] and env.b.ftp.adj_dateien() == [f"adj_{ib['id']}.json"]
    assert run(bot._adj_entfernen(env.a, [ib["id"]])) is None                    # fremde ID: nichts passiert
    assert _ids(env.b) == [ib["id"]] and env.b.ftp.adj_dateien() == [f"adj_{ib['id']}.json"]


# ══════════════════════════════════════════════════════════════════════════
#  Scheduler-Validierung
# ══════════════════════════════════════════════════════════════════════════
def _sch(**changes):
    wert = {"aktiv": True, "anzahl": 2, "alle_neustarts": 3,
            "airdrops": [{"quelle": "premade", "name": "drop1"}, {"quelle": "eigen", "name": "mein1"}],
            "positionen": [{"x": 1000, "y": 200, "z": 2000}, {"x": 3000, "y": 210, "z": 4000}, {"x": 5000, "y": 220, "z": 6000}]}
    wert.update(changes)
    return wert


def test_scheduler_validieren_gueltig_und_bereinigt():
    sauber, fehler = bot._adj_scheduler_validieren(_sch())
    assert fehler is None
    assert sauber == {"aktiv": True, "anzahl": 2, "alle_neustarts": 3,
                      "airdrops": [{"quelle": "premade", "name": "drop1"}, {"quelle": "eigen", "name": "mein1"}],
                      "positionen": [{"x": 1000.0, "y": 200.0, "z": 2000.0}, {"x": 3000.0, "y": 210.0, "z": 4000.0},
                                     {"x": 5000.0, "y": 220.0, "z": 6000.0}]}


def test_scheduler_validieren_zahlen_aus_texten_und_rundung():
    sauber, fehler = bot._adj_scheduler_validieren(_sch(anzahl="2", alle_neustarts="4",
                                                        positionen=[{"x": 1.234567, "y": 2.0, "z": 3.0}, {"x": 9, "y": 9, "z": 9}]))
    assert fehler is None and sauber["anzahl"] == 2 and sauber["alle_neustarts"] == 4
    assert sauber["positionen"][0] == {"x": 1.23, "y": 2.0, "z": 3.0}


@pytest.mark.parametrize("anzahl,gueltig", [(0, False), (1, True), (10, True), (11, False), (-1, False), ("x", False), (None, False),
                                            ([], False), (float("inf"), False), (float("nan"), False), (10 ** 30, False)])
def test_scheduler_validieren_anzahl_1_bis_10(anzahl, gueltig):
    pos = [{"x": i, "y": 1, "z": i} for i in range(1, 12)]
    sauber, fehler = bot._adj_scheduler_validieren(_sch(anzahl=anzahl, positionen=pos))
    assert (fehler is None) == gueltig and (sauber is not None) == gueltig
    if not gueltig:
        assert isinstance(fehler, str) and fehler


@pytest.mark.parametrize("alle,gueltig", [(0, False), (1, True), (100, True), (101, False), (-5, False), ("x", False), (None, False), (float("inf"), False)])
def test_scheduler_validieren_alle_neustarts_1_bis_100(alle, gueltig):
    sauber, fehler = bot._adj_scheduler_validieren(_sch(alle_neustarts=alle))
    assert (fehler is None) == gueltig


@pytest.mark.parametrize("eintrag", [
    {"quelle": "sonst", "name": "a"}, {"quelle": "premade", "name": ""}, {"quelle": "premade", "name": "a/b"},
    {"quelle": "premade", "name": "../x"}, {"quelle": "premade", "name": "x" * 41}, {"quelle": "premade"},
    {"name": "a"}, "text", 5, None, ["premade", "a"], {"quelle": "PREMADE", "name": "a"},
])
def test_scheduler_validieren_lehnt_ungueltige_auswahl_ab(eintrag):
    sauber, fehler = bot._adj_scheduler_validieren(_sch(airdrops=[{"quelle": "premade", "name": "ok"}, eintrag]))
    assert sauber is None and "Airdrop-Auswahl" in fehler


def test_scheduler_validieren_auswahl_dubletten_limit_und_typ():
    sauber, _ = bot._adj_scheduler_validieren(_sch(airdrops=[{"quelle": "premade", "name": "a"}] * 3 + [{"quelle": "eigen", "name": "a"}]))
    assert sauber["airdrops"] == [{"quelle": "premade", "name": "a"}, {"quelle": "eigen", "name": "a"}]
    hundert = [{"quelle": "premade", "name": f"a{i}"} for i in range(100)]
    assert bot._adj_scheduler_validieren(_sch(airdrops=hundert))[1] is None
    sauber, fehler = bot._adj_scheduler_validieren(_sch(airdrops=hundert + [{"quelle": "premade", "name": "zu_viel"}]))
    assert sauber is None and "Airdrop-Auswahl" in fehler
    for kaputt in ("text", {"a": 1}, 5):
        assert bot._adj_scheduler_validieren(_sch(airdrops=kaputt))[0] is None


@pytest.mark.parametrize("pos", [
    {"x": 1, "y": 2}, {"x": 1, "y": 2, "z": "3"}, {"x": True, "y": 2, "z": 3}, {"x": float("nan"), "y": 2, "z": 3},
    {"x": 1, "y": float("inf"), "z": 3}, {"x": 1, "y": 2, "z": None}, {"x": 10 ** 8, "y": 2, "z": 3}, {}, "text", None, [1, 2, 3],
])
def test_scheduler_validieren_lehnt_ungueltige_positionen_ab(pos):
    sauber, fehler = bot._adj_scheduler_validieren(_sch(positionen=[{"x": 1, "y": 2, "z": 3}, {"x": 4, "y": 5, "z": 6}, pos]))
    assert sauber is None and "Position" in fehler


def test_scheduler_validieren_position_mit_riesiger_ganzzahl_ist_ein_fehler_kein_absturz():
    sauber, fehler = bot._adj_scheduler_validieren(_sch(positionen=[{"x": 10 ** 400, "y": 2, "z": 3}]))
    assert sauber is None and isinstance(fehler, str)


def test_scheduler_validieren_positionen_dubletten_und_limit():
    pos = [{"x": 1, "y": 2, "z": 3}, {"x": 1.001, "y": 2, "z": 3}, {"x": 4, "y": 5, "z": 6}, {"x": 4.0, "y": 5.0, "z": 6.0}]
    sauber, fehler = bot._adj_scheduler_validieren(_sch(positionen=pos))
    assert fehler is None and sauber["positionen"] == [{"x": 1.0, "y": 2.0, "z": 3.0}, {"x": 4.0, "y": 5.0, "z": 6.0}]
    fuenfzig = [{"x": i, "y": 1, "z": i} for i in range(50)]
    assert bot._adj_scheduler_validieren(_sch(positionen=fuenfzig))[1] is None
    sauber, fehler = bot._adj_scheduler_validieren(_sch(positionen=fuenfzig + [{"x": 999, "y": 1, "z": 999}]))
    assert sauber is None and "50" in fehler


def test_scheduler_validieren_aktiv_braucht_airdrop_und_genug_positionen():
    sauber, fehler = bot._adj_scheduler_validieren(_sch(airdrops=[]))
    assert sauber is None and "mindestens einen Airdrop" in fehler
    sauber, fehler = bot._adj_scheduler_validieren(_sch(anzahl=4))
    assert sauber is None and "Positionen" in fehler
    sauber, fehler = bot._adj_scheduler_validieren(_sch(anzahl=3))
    assert fehler is None
    # Dubletten zählen nicht als Position
    sauber, fehler = bot._adj_scheduler_validieren(_sch(anzahl=2, positionen=[{"x": 1, "y": 2, "z": 3}, {"x": 1, "y": 2, "z": 3}]))
    assert sauber is None
    # ausgeschaltet darf alles leer sein
    sauber, fehler = bot._adj_scheduler_validieren({"aktiv": False, "anzahl": 5, "alle_neustarts": 1})
    assert fehler is None and sauber == {"aktiv": False, "anzahl": 5, "alle_neustarts": 1, "airdrops": [], "positionen": []}


def test_scheduler_validieren_aktiv_ist_ein_wahrheitswert_und_fehlend_ist_aus():
    assert bot._adj_scheduler_validieren({"anzahl": 1, "alle_neustarts": 1})[0]["aktiv"] is False
    for aus in (False, 0, None, ""):
        assert bot._adj_scheduler_validieren(_sch(aktiv=aus))[0]["aktiv"] is False


# ══════════════════════════════════════════════════════════════════════════
#  Neustart-Zähler (simulierte Neustarts: _adj_einen_neustart direkt)
# ══════════════════════════════════════════════════════════════════════════
def neustart(conn, anzahl=1):
    """Simuliert erkannte Neustarts wie der Poll: vormerken, dann einzeln verbuchen."""
    z = bot._adj_zustand(conn)
    fehler = None
    for _ in range(anzahl):
        z["neustarts_offen"] += 1
        fehler = run(bot._adj_einen_neustart(conn))
        if fehler:
            return fehler
    return fehler


def test_neustart_restarts_eins_wird_beim_ersten_neustart_ausgetragen(env):
    _premade()
    inst = _platziere(env.a, restarts=1)
    assert neustart(env.a) is None
    assert _ids(env.a) == [] and env.a.ftp.adj_dateien() == [] and env.a.ftp.eintraege() == ["custom/alt.json"]
    assert bot._adj_zustand(env.a)["neustarts_offen"] == 0
    assert f"{MISSION}/{inst['datei']}" not in env.a.ftp.files


@pytest.mark.parametrize("n", [2, 3, 5])
def test_neustart_restarts_n_wird_beim_n_ten_neustart_ausgetragen(env, n):
    _premade()
    inst = _platziere(env.a, restarts=n)
    for k in range(1, n):
        assert neustart(env.a) is None
        assert _ids(env.a) == [inst["id"]], f"nach Neustart {k} muss er noch da sein"
        assert bot._adj_zustand(env.a)["instanzen"][0]["gesehen"] == k
        assert inst["datei"] in env.a.ftp.eintraege()
    assert neustart(env.a) is None                                  # der n-te
    assert _ids(env.a) == [] and inst["datei"] not in env.a.ftp.eintraege() and env.a.ftp.adj_dateien() == []


def test_neustart_mehrere_instanzen_laufen_unabhaengig_ab(env):
    _premade()
    k1 = _platziere(env.a, x=3000, restarts=1)
    k2 = _platziere(env.a, x=6000, restarts=2)
    k3 = _platziere(env.a, x=9000, restarts=3)
    neustart(env.a)
    assert _ids(env.a) == sorted([k2["id"], k3["id"]])
    neustart(env.a)
    assert _ids(env.a) == [k3["id"]]
    neustart(env.a)
    assert _ids(env.a) == [] and env.a.ftp.eintraege() == ["custom/alt.json"]
    assert k1["id"] not in " ".join(map(str, env.a.ftp.eintraege()))


def test_neustart_gleichzeitig_ablaufende_instanzen_ergeben_eine_gameplay_schreibung(env):
    _premade()
    for i in range(3):
        _platziere(env.a, x=3000 + 1000 * i, restarts=1)
    env.a.ftp.protokoll.clear()
    neustart(env.a)
    assert env.a.ftp.protokoll.count(("write", GAMEPLAY_PFAD)) == 1 and _ids(env.a) == []


def test_neustart_zustand_wird_gespeichert(env):
    _premade()
    _platziere(env.a, restarts=3)
    neustart(env.a)
    assert _platte("1000")["airdrop_json"]["instanzen"][0]["gesehen"] == 1
    assert _platte("1000")["airdrop_json"]["neustarts_offen"] == 0


def test_neustart_ohne_instanzen_und_ohne_scheduler_aendert_nichts(env):
    bot._adj_zustand(env.a)
    assert neustart(env.a) is None
    assert env.a.ftp.protokoll == [] and bot._adj_zustand(env.a)["scheduler"]["zaehler"] == 0


def test_neustart_schreibfehler_laesst_die_zaehlung_stehen_und_wiederholung_ist_idempotent(env):
    _premade()
    inst = _platziere(env.a, restarts=1)
    z = bot._adj_zustand(env.a)
    z["neustarts_offen"] = 1
    env.a.ftp.write_fehler = {"cfggameplay.json"}
    fehler = run(bot._adj_einen_neustart(env.a))
    assert fehler and "fehlgeschlagen" in fehler
    assert z["neustarts_offen"] == 1 and _ids(env.a) == [inst["id"]] and z["instanzen"][0]["gesehen"] == 0
    assert inst["datei"] in env.a.ftp.eintraege() and env.a.ftp.adj_dateien()
    run(bot._adj_einen_neustart(env.a))                              # erneut scheitern ändert nichts
    assert z["neustarts_offen"] == 1 and z["instanzen"][0]["gesehen"] == 0
    env.a.ftp.write_fehler = set()
    assert run(bot._adj_einen_neustart(env.a)) is None
    assert z["neustarts_offen"] == 0 and _ids(env.a) == [] and env.a.ftp.adj_dateien() == []


def test_neustart_fehler_beim_ablauf_zaehlt_die_anderen_nicht_doppelt(env):
    """Ein Neustart, dessen Ablauf-Schreibung scheitert, darf `gesehen` der übrigen nicht erhöhen -
    sonst zählte die Wiederholung denselben Neustart ein zweites Mal."""
    _premade()
    ablaufend = _platziere(env.a, x=3000, restarts=1)
    bleibt = _platziere(env.a, x=9000, restarts=5)
    z = bot._adj_zustand(env.a)
    z["neustarts_offen"] = 1
    env.a.ftp.write_fehler = {"cfggameplay.json"}
    assert run(bot._adj_einen_neustart(env.a))
    assert [i["gesehen"] for i in z["instanzen"]] == [0, 0] and z["neustarts_offen"] == 1
    env.a.ftp.write_fehler = set()
    assert run(bot._adj_einen_neustart(env.a)) is None
    assert _ids(env.a) == [bleibt["id"]] and z["instanzen"][0]["gesehen"] == 1 and z["neustarts_offen"] == 0
    assert ablaufend["id"] not in _ids(env.a)


def test_neustart_ohne_ftp_meldet_fehler_und_behaelt_alles(env):
    _premade()
    inst = _platziere(env.a, restarts=1)
    env.a.ftp = None
    z = bot._adj_zustand(env.a)
    z["neustarts_offen"] = 1
    assert run(bot._adj_einen_neustart(env.a)) == bot._ADJ_FEHLER_FTP
    assert z["neustarts_offen"] == 1 and _ids(env.a) == [inst["id"]]


# ── Scheduler ─────────────────────────────────────────────────────────────
POSITIONEN = [{"x": 1000 + 1000 * i, "y": 200 + i, "z": 2000 + 500 * i} for i in range(6)]


def scheduler_an(conn, anzahl=2, alle=3, airdrops=("drop1", "drop2", "drop3"), positionen=POSITIONEN):
    for n in airdrops:
        if bot._adj_laden("premade", "", n) is None:
            _premade(n)
    sch = bot._adj_zustand(conn)["scheduler"]
    sch.update({"aktiv": True, "anzahl": anzahl, "alle_neustarts": alle, "zaehler": 0,
                "airdrops": [{"quelle": "premade", "name": n} for n in airdrops], "positionen": copy.deepcopy(list(positionen))})
    assert run(bot._adj_scheduler_anwenden(conn)) is None
    return bot._adj_zustand(conn)


def _pos(z, von="scheduler"):
    return {(i["x"], i["z"]) for i in z["instanzen"] if i["von"] == von}


def test_scheduler_anwenden_setzt_anzahl_instanzen_auf_unterschiedlichen_positionen(env):
    z = scheduler_an(env.a, anzahl=3)
    sch_inst = [i for i in z["instanzen"] if i["von"] == "scheduler"]
    assert len(sch_inst) == 3 and len(_pos(z)) == 3
    assert {(p["x"], p["z"]) for p in POSITIONEN} >= _pos(z)
    for i in sch_inst:
        assert (i["restarts"], i["gesehen"], i["user"], i["quelle"]) == (3, 0, "scheduler", "premade")
        objekte = env.a.ftp.objekte(i)
        x0, x1, y0, _y1, z0, z1 = _rahmen(objekte)
        assert ((x0 + x1) / 2, (z0 + z1) / 2, y0) == (i["x"], i["z"], i["y"])
    assert env.a.ftp.eintraege() == ["custom/alt.json"] + [i["datei"] for i in sch_inst]
    assert z["scheduler"]["zaehler"] == 0


def test_scheduler_anwenden_aus_traegt_nur_scheduler_instanzen_aus(env):
    _premade()
    befehl = _platziere(env.a, x=15000, z=15000, restarts=9)
    z = scheduler_an(env.a)
    assert len([i for i in z["instanzen"] if i["von"] == "scheduler"]) == 2
    z["scheduler"]["aktiv"] = False
    assert run(bot._adj_scheduler_anwenden(env.a)) is None
    assert _ids(env.a) == [befehl["id"]] and env.a.ftp.eintraege() == ["custom/alt.json", befehl["datei"]]
    assert env.a.ftp.adj_dateien() == [f"adj_{befehl['id']}.json"]


def test_scheduler_instanzen_verfallen_nicht_einzeln_nur_per_rotation(env):
    z = scheduler_an(env.a, anzahl=2, alle=3)
    ids_vorher = _ids(env.a, "scheduler")
    for i in z["instanzen"]:
        i["restarts"] = 1                                           # wäre als Befehls-Airdrop sofort fällig
    neustart(env.a)
    assert _ids(env.a, "scheduler") == ids_vorher and z["scheduler"]["zaehler"] == 1
    neustart(env.a)
    assert _ids(env.a, "scheduler") == ids_vorher and z["scheduler"]["zaehler"] == 2
    neustart(env.a)                                                 # dritter Neustart: Rotation
    neu = _ids(env.a, "scheduler")
    assert len(neu) == 2 and not set(neu) & set(ids_vorher) and z["scheduler"]["zaehler"] == 0
    assert all(i["gesehen"] == 0 for i in z["instanzen"])
    assert len(env.a.ftp.eintraege()) == 3 and len(env.a.ftp.adj_dateien()) == 2


def test_scheduler_rotation_alle_einen_neustart(env):
    z = scheduler_an(env.a, anzahl=1, alle=1)
    vorher = _ids(env.a)
    neustart(env.a)
    assert len(_ids(env.a)) == 1 and _ids(env.a) != vorher and z["scheduler"]["zaehler"] == 0


def test_scheduler_rotation_waehlt_nie_dieselben_positionen_wie_zuvor_und_nie_doppelt(env, monkeypatch):
    import random
    for seed in range(15):
        monkeypatch.setattr(bot, "random", random.Random(seed))
        z = scheduler_an(env.a, anzahl=2, alle=1)
        for _ in range(8):
            alt = _pos(z)
            neustart(env.a)
            neu = _pos(z)
            assert len(neu) == 2, "Positionen dürfen nie doppelt sein"
            assert not (alt & neu), f"Seed {seed}: {alt} -> {neu}"          # 6 Positionen, 2 davon frei → immer andere


def test_scheduler_rotation_ohne_ausweichmoeglichkeit_nimmt_trotzdem_die_gleichen(env):
    pool = POSITIONEN[:2]
    z = scheduler_an(env.a, anzahl=2, alle=1, positionen=pool)
    for _ in range(4):
        neustart(env.a)
        assert _pos(z) == {(p["x"], p["z"]) for p in pool} and len(z["instanzen"]) == 2
    # Pool nur teilweise frei: erst die frischen, Rest aus dem Gesamtpool
    pool3 = POSITIONEN[:3]
    z = scheduler_an(env.a, anzahl=2, alle=1, positionen=pool3)
    for _ in range(6):
        neustart(env.a)
        assert len(_pos(z)) == 2 and _pos(z) <= {(p["x"], p["z"]) for p in pool3}


def test_scheduler_rotation_meidet_per_befehl_gesetzte_positionen(env, monkeypatch):
    import random
    _premade()
    p0 = POSITIONEN[0]
    for seed in range(20):
        monkeypatch.setattr(bot, "random", random.Random(seed))
        befehl = _platziere(env.a, x=p0["x"] + 0.5, y=50, z=p0["z"] - 0.5, restarts=100)        # liegt (≤1 m) auf Pool-Position 0
        z = scheduler_an(env.a, anzahl=2, alle=1)
        for _ in range(5):
            assert (p0["x"], p0["z"]) not in _pos(z) and len(_pos(z)) == 2
            neustart(env.a)
        assert befehl["id"] in _ids(env.a, "befehl")
        run(bot._adj_entfernen(env.a, [befehl["id"]]))


def test_scheduler_anzahl_wird_auf_freie_positionen_begrenzt(env):
    _premade()
    for p in POSITIONEN[:5]:
        _platziere(env.a, x=p["x"], y=50, z=p["z"], restarts=100)                             # 5 von 6 Positionen belegt
    z = scheduler_an(env.a, anzahl=4, alle=1)
    assert _pos(z) == {(POSITIONEN[5]["x"], POSITIONEN[5]["z"])}


def test_scheduler_wechselt_die_airdrops_und_nutzt_nur_vorhandene_dateien(env, monkeypatch):
    import random
    monkeypatch.setattr(bot, "random", random.Random(3))
    z = scheduler_an(env.a, anzahl=2, alle=1, airdrops=("drop1", "drop2", "drop3"))
    gesehen = set()
    for _ in range(10):
        namen = [i["name"] for i in z["instanzen"] if i["von"] == "scheduler"]
        assert len(namen) == len(set(namen)) == 2                          # genug Airdrops → keine Wiederholung
        gesehen |= set(namen)
        neustart(env.a)
    assert gesehen == {"drop1", "drop2", "drop3"}
    bot._adj_loeschen("premade", "", "drop2")
    bot._adj_loeschen("premade", "", "drop3")
    neustart(env.a)                                                        # nur noch drop1 vorhanden → mehrfach erlaubt
    assert {i["name"] for i in z["instanzen"]} == {"drop1"} and len(z["instanzen"]) == 2


def test_scheduler_ohne_vorhandene_dateien_oder_positionen_setzt_nichts(env):
    z = scheduler_an(env.a, anzahl=2, alle=1)
    assert len(z["instanzen"]) == 2
    bot._adj_loeschen("premade", "", "drop1")
    bot._adj_loeschen("premade", "", "drop2")
    bot._adj_loeschen("premade", "", "drop3")
    neustart(env.a)                                                        # alte raus, nichts Neues möglich
    assert z["instanzen"] == [] and env.a.ftp.eintraege() == ["custom/alt.json"] and z["scheduler"]["zaehler"] == 0


def test_scheduler_rotation_laesst_befehls_airdrops_unberuehrt(env):
    _premade()
    befehl = _platziere(env.a, x=15000, z=15000, restarts=4)
    z = scheduler_an(env.a, anzahl=2, alle=2)
    neustart(env.a, 2)                                                     # Rotation beim zweiten
    b = next(i for i in z["instanzen"] if i["id"] == befehl["id"])
    assert b["gesehen"] == 2 and befehl["datei"] in env.a.ftp.eintraege()
    neustart(env.a, 2)                                                     # Befehls-Airdrop läuft beim 4. ab
    assert befehl["id"] not in _ids(env.a) and len(_ids(env.a, "scheduler")) == 2


def test_scheduler_rotation_schreibfehler_nimmt_die_zaehlung_zurueck_und_wiederholt_sauber(env):
    _premade()
    befehl = _platziere(env.a, x=15000, z=15000, restarts=10)
    z = scheduler_an(env.a, anzahl=2, alle=1)
    alt_ids = _ids(env.a, "scheduler")
    alt_eintraege = list(env.a.ftp.eintraege())
    z["neustarts_offen"] = 1
    env.a.ftp.write_fehler = {"cfggameplay.json"}
    fehler = run(bot._adj_einen_neustart(env.a))
    assert fehler and "fehlgeschlagen" in fehler
    assert z["neustarts_offen"] == 1 and _ids(env.a, "scheduler") == alt_ids and env.a.ftp.eintraege() == alt_eintraege
    assert next(i for i in z["instanzen"] if i["id"] == befehl["id"])["gesehen"] == 0       # Zählung zurückgenommen
    assert len(env.a.ftp.adj_dateien()) == 3                                              # keine verwaiste neue Datei
    env.a.ftp.write_fehler = set()
    assert run(bot._adj_einen_neustart(env.a)) is None
    assert z["neustarts_offen"] == 0 and not set(_ids(env.a, "scheduler")) & set(alt_ids) and len(_ids(env.a, "scheduler")) == 2
    assert next(i for i in z["instanzen"] if i["id"] == befehl["id"])["gesehen"] == 1       # genau einmal gezählt
    assert len(env.a.ftp.adj_dateien()) == 3


def test_scheduler_rotation_schreibfehler_nach_ablauf_zaehlt_den_neustart_nicht_doppelt(env):
    _premade()
    ablaufend = _platziere(env.a, x=15000, z=15000, restarts=1)
    bleibt = _platziere(env.a, x=14000, z=14000, restarts=6)
    z = scheduler_an(env.a, anzahl=1, alle=1)
    z["neustarts_offen"] = 1
    # Ablauf-Schreibung klappt (1. Schreibvorgang), die Rotation scheitert (2.)
    schreibungen = []
    original = env.a.ftp.write_file

    def write(pfad, inhalt):
        if pfad == GAMEPLAY_PFAD:
            schreibungen.append(pfad)
            if len(schreibungen) == 2:
                env.a.ftp.protokoll.append(("write", pfad))
                return False
        return original(pfad, inhalt)
    env.a.ftp.write_file = write
    assert run(bot._adj_einen_neustart(env.a))
    assert ablaufend["id"] not in _ids(env.a) and z["neustarts_offen"] == 1
    assert next(i for i in z["instanzen"] if i["id"] == bleibt["id"])["gesehen"] == 0
    env.a.ftp.write_file = original
    assert run(bot._adj_einen_neustart(env.a)) is None
    assert next(i for i in z["instanzen"] if i["id"] == bleibt["id"])["gesehen"] == 1 and z["neustarts_offen"] == 0


def test_scheduler_ausgeschaltet_zaehlt_nicht(env):
    z = scheduler_an(env.a, anzahl=1, alle=5)
    z["scheduler"]["aktiv"] = False
    neustart(env.a, 3)
    assert z["scheduler"]["zaehler"] == 0


def test_scheduler_ist_je_server_getrennt(env):
    scheduler_an(env.a, anzahl=2, alle=1)
    assert bot._adj_zustand(env.b)["instanzen"] == [] and bot._adj_zustand(env.b)["scheduler"]["aktiv"] is False
    assert env.b.ftp.protokoll == []


# ── Poll-Hook ─────────────────────────────────────────────────────────────
def _poll_lauf(conn, *, restart=True, warten=None):
    """Ruft `_adj_poll` in einer Ereignisschleife auf und wartet auf die gestartete Aufgabe."""
    async def lauf():
        bot._adj_poll(conn, restart)
        aufgabe = bot._ADJ_TASKS.get(conn.service_id)
        if aufgabe is not None:
            await aufgabe
        return aufgabe
    return run(lauf())


def test_poll_ohne_airdrop_zustand_kehrt_sofort_zurueck(env):
    assert "airdrop_json" not in env.a.data
    for restart in (True, False):
        assert _poll_lauf(env.a, restart=restart) is None
    assert "airdrop_json" not in env.a.data and bot._ADJ_TASKS == {} and not os.path.exists("airdrop_json")
    assert env.a.ftp.protokoll == []


def test_poll_leerer_zustand_ohne_arbeit_zaehlt_keine_neustarts(env):
    bot._adj_zustand(env.a)
    assert _poll_lauf(env.a, restart=True) is None
    assert bot._adj_zustand(env.a)["neustarts_offen"] == 0 and env.a.ftp.protokoll == []


def test_poll_ohne_neustart_startet_nichts(env):
    _premade()
    inst = _platziere(env.a, restarts=1)
    assert _poll_lauf(env.a, restart=False) is None
    assert _ids(env.a) == [inst["id"]] and bot._adj_zustand(env.a)["neustarts_offen"] == 0


def test_poll_neustart_wird_verbucht_und_der_airdrop_ausgetragen(env):
    _premade()
    inst = _platziere(env.a, restarts=2)
    assert _poll_lauf(env.a, restart=True) is not None
    z = bot._adj_zustand(env.a)
    assert z["neustarts_offen"] == 0 and z["instanzen"][0]["gesehen"] == 1 and _ids(env.a) == [inst["id"]]
    _poll_lauf(env.a, restart=True)
    assert _ids(env.a) == [] and env.a.ftp.adj_dateien() == []


def test_poll_merkt_neustart_persistent_vor_bevor_die_aufgabe_laeuft(env):
    _premade()
    _platziere(env.a, restarts=5)

    async def lauf():
        bot._adj_poll(env.a, True)                                   # noch KEIN await: die Aufgabe hat nicht gearbeitet
        vorgemerkt = _platte("1000")["airdrop_json"]["neustarts_offen"]
        await bot._ADJ_TASKS["1000"]
        return vorgemerkt
    assert run(lauf()) == 1
    assert _platte("1000")["airdrop_json"]["neustarts_offen"] == 0


def test_poll_offene_neustarts_aus_einem_frueheren_lauf_werden_nachgeholt(env):
    _premade()
    inst = _platziere(env.a, restarts=2)
    bot._adj_zustand(env.a)["neustarts_offen"] = 2                    # z. B. Bot-Absturz nach Vormerken
    _poll_lauf(env.a, restart=False)
    assert _ids(env.a) == [] and bot._adj_zustand(env.a)["neustarts_offen"] == 0
    assert inst["datei"] not in env.a.ftp.eintraege()


def test_poll_startet_hoechstens_eine_aufgabe_je_server(env, monkeypatch):
    _premade()
    _platziere(env.a, restarts=2)
    _platziere(env.b, restarts=1)

    async def lauf():
        schranke = asyncio.Event()
        aufrufe = []

        async def warten(conn):
            aufrufe.append(conn.service_id)
            await schranke.wait()
        monkeypatch.setattr(bot, "_adj_warten_bis_online", warten)
        bot._adj_poll(env.a, True)
        erste = bot._ADJ_TASKS["1000"]
        await asyncio.sleep(0)
        bot._adj_poll(env.a, True)                                   # zweiter Neustart, solange die Aufgabe noch wartet
        bot._adj_poll(env.a, False)
        assert bot._ADJ_TASKS["1000"] is erste and aufrufe == ["1000"]
        assert bot._adj_zustand(env.a)["neustarts_offen"] == 2
        bot._adj_poll(env.b, True)                                   # anderer Server bekommt seine eigene Aufgabe
        assert bot._ADJ_TASKS["2000"] is not erste
        schranke.set()
        await asyncio.gather(erste, bot._ADJ_TASKS["2000"])
    run(lauf())
    assert bot._adj_zustand(env.a)["neustarts_offen"] == 0 and _ids(env.a) == []        # beide Neustarts verbucht
    assert _ids(env.b) == []


def test_poll_wiederholung_nach_fehler_erst_nach_der_wartezeit(env):
    _premade()
    inst = _platziere(env.a, restarts=1)
    env.a.ftp.write_fehler = {"cfggameplay.json"}
    erste = _poll_lauf(env.a, restart=True)
    z = bot._adj_zustand(env.a)
    assert z["neustarts_offen"] == 1 and _ids(env.a) == [inst["id"]]
    rest = bot._ADJ_RETRY_AB["1000"] - time.time()
    assert bot._ADJ_RETRY_SEKUNDEN == 90 and 80 < rest <= 90
    env.a.ftp.write_fehler = set()
    assert _poll_lauf(env.a, restart=False) is erste                   # Wartezeit läuft: keine neue Aufgabe (dieselbe, fertige)
    assert z["neustarts_offen"] == 1 and _ids(env.a) == [inst["id"]]
    bot._ADJ_RETRY_AB["1000"] = time.time() - 1                         # Wartezeit vorbei
    zweite = _poll_lauf(env.a, restart=False)
    assert zweite is not erste and z["neustarts_offen"] == 0 and _ids(env.a) == []


def test_poll_fehler_in_der_aufgabe_stoert_den_poll_nicht(env, monkeypatch):
    _premade()
    _platziere(env.a, restarts=1)

    async def kaputt(conn):
        raise RuntimeError("Absturz in der Verarbeitung")
    monkeypatch.setattr(bot, "_adj_einen_neustart", kaputt)
    _poll_lauf(env.a, restart=True)                                     # darf nicht werfen
    assert bot._adj_zustand(env.a)["neustarts_offen"] == 1 and "1000" in bot._ADJ_RETRY_AB


def test_poll_wartet_bis_der_server_online_ist(env, monkeypatch):
    _premade()
    _platziere(env.a, restarts=1)
    env.a.data["server_ip"] = "10.0.0.1:2302"
    env.a.data["query_port"] = 2303
    antworten = [False, False, True]
    abfragen = []
    monkeypatch.setattr(bot, "a2s_query", lambda ip, port: abfragen.append((ip, port)) or antworten.pop(0))

    async def kein_schlaf(_s):
        return None
    monkeypatch.setattr(bot.asyncio, "sleep", kein_schlaf)
    _poll_lauf(env.a, restart=True)
    assert abfragen == [("10.0.0.1", 2303)] * 3 and _ids(env.a) == []


# ══════════════════════════════════════════════════════════════════════════
#  Slash-Befehle /airdrop add | list | remove
# ══════════════════════════════════════════════════════════════════════════
class _Rolle:
    def __init__(self, id_):
        self.id = id_


class _Member:
    def __init__(self, id_, rollen=()):
        self.id, self.roles, self.mention = id_, [_Rolle(r) for r in rollen], f"<@{id_}>"
        self.guild_permissions = SimpleNamespace(administrator=False)

    def __str__(self):
        return f"Nutzer{self.id}"


class _Antwort:
    def __init__(self):
        self.gesendet, self.aufgeschoben = [], None

    def is_done(self):
        return bool(self.gesendet) or self.aufgeschoben is not None

    async def send_message(self, content=None, embed=None, ephemeral=False, **_):
        self.gesendet.append({"content": content, "embed": embed, "ephemeral": ephemeral})

    send = send_message

    async def defer(self, ephemeral=False, **_):
        self.aufgeschoben = {"ephemeral": ephemeral}

    @property
    def text(self):
        return "\n".join((g["content"] or "") + ((g["embed"].title or "") + "\n" + (g["embed"].description or "") if g["embed"] else "")
                         for g in self.gesendet)


class _Interaktion:
    OWNER = 1

    def __init__(self, user=None, guild_id=GID, locale=None, server=None, befehl="airdrop add"):
        self.user = user or _Member(self.OWNER)
        self.guild_id, self.locale = guild_id, locale
        self.guild = SimpleNamespace(id=guild_id, owner_id=self.OWNER)
        self.response, self.followup = _Antwort(), _Antwort()
        self.namespace = SimpleNamespace(server=server)
        self.command = SimpleNamespace(qualified_name=befehl)

    @property
    def alle(self):
        """Alles, was der Nutzer als Antwort bekam (sofort + nachträglich)."""
        return self.response.text + "\n" + self.followup.text

    @property
    def ephemer(self):
        return all(g["ephemeral"] for g in self.response.gesendet + self.followup.gesendet) and bool(self.response.gesendet + self.followup.gesendet)


EN = discord.Locale.american_english


@pytest.fixture
def slash(monkeypatch, env):
    monkeypatch.setattr(discord, "Member", _Member)
    _premade()
    return env


def add(name="premade:drop1", x=5000.0, y=300.0, z=6000.0, restarts=2, server=None, **kw):
    inter = _Interaktion(**kw)
    run(bot.airdrop_add.callback(inter, name, x, y, z, restarts, server))
    return inter


def liste(art=None, server=None, **kw):
    inter = _Interaktion(befehl="airdrop list", **kw)
    run(bot.airdrop_list.callback(inter, discord.app_commands.Choice(name=art, value=art) if art else None, server))
    return inter


def entfernen(name, server=None, **kw):
    inter = _Interaktion(befehl="airdrop remove", **kw)
    run(bot.airdrop_remove.callback(inter, name, server))
    return inter


def test_add_erfolg_schreibt_datei_gameplay_instanz_und_audit(slash):
    a = slash.a
    inter = add(restarts=2)
    assert inter.response.aufgeschoben == {"ephemeral": True} and inter.ephemer
    antwort = inter.followup.gesendet[0]["content"]
    assert antwort.startswith("✅") and "drop1" in antwort and "5000.0, 300.0, 6000.0" in antwort
    assert "nächsten Neustart" in antwort and "**2** Neustart(s)" in antwort and "12 Objekte" in antwort
    inst = bot._adj_zustand(a)["instanzen"][0]
    assert f"`{inst['id']}`" in antwort
    assert (inst["name"], inst["quelle"], inst["von"], inst["user"], inst["restarts"], inst["gesehen"]) == ("drop1", "premade", "befehl", "1", 2, 0)
    assert a.ftp.adj_dateien() == [f"adj_{inst['id']}.json"] and a.ftp.eintraege() == ["custom/alt.json", inst["datei"]]
    x0, x1, y0, _y1, z0, z1 = _rahmen(a.ftp.objekte(inst))
    assert ((x0 + x1) / 2, (z0 + z1) / 2, y0) == (5000.0, 6000.0, 300.0)
    eintrag = list(bot._audit_log)[-1]
    assert (eintrag["source"], eintrag["action"], eintrag["ok"]) == ("discord", "/airdrop add", True)
    assert "drop1" in eintrag["detail"] and "5000.0" in eintrag["detail"] and "x2" in eintrag["detail"]
    assert slash.b.ftp.protokoll == [] and bot._adj_zustand(slash.b)["instanzen"] == []


def test_add_englische_antwort_nach_discord_sprache(slash):
    inter = add(locale=EN)
    antwort = inter.followup.gesendet[0]["content"]
    assert "becomes active from the **next restart**" in antwort and "objects" in antwort and "nächsten" not in antwort
    assert "No airdrop named" in add(name="gibtsnicht", locale=EN).response.gesendet[0]["content"]
    assert "does not exist" in add(name="premade:gibtsnicht", locale=EN).followup.gesendet[0]["content"]


@pytest.mark.parametrize("wert", ["premade:drop1", "drop1"])
def test_add_nimmt_premade_per_vorschlagswert_und_blossem_namen(slash, wert):
    add(name=wert)
    assert [(i["quelle"], i["name"]) for i in bot._adj_zustand(slash.a)["instanzen"]] == [("premade", "drop1")]


def test_add_eigene_datei_per_vorschlagswert_und_eigene_vor_premade_beim_blossen_namen(slash):
    _eigen(slash.a, "gleich", _objekte()[:4])
    _premade("gleich", _objekte()[:9])
    add(name="eigen:gleich")
    add(name="premade:gleich", x=9000.0)
    add(name="gleich", x=12000.0)                                # bloßer Name: die eigene gewinnt
    z = bot._adj_zustand(slash.a)["instanzen"]
    assert [(i["quelle"], i["objekte"]) for i in z] == [("eigen", 4), ("premade", 9), ("eigen", 4)]


def test_add_eigene_datei_eines_anderen_servers_ist_nicht_waehlbar(slash):
    _eigen(slash.b, "nurb")
    assert "Kein Airdrop" in add(name="nurb").response.gesendet[0]["content"]
    assert "gibt es nicht" in add(name="eigen:nurb").followup.gesendet[0]["content"]
    assert bot._adj_zustand(slash.a)["instanzen"] == [] and slash.a.ftp.protokoll == []


@pytest.mark.parametrize("name", ["gibtsnicht", "../x", "", "  ", "a/b", "drop1.json"])
def test_add_unbekannter_name(slash, name):
    inter = add(name=name)
    text = inter.response.gesendet[0]["content"]
    assert text.startswith("❌") and "Kein Airdrop" in text and inter.response.aufgeschoben is None and inter.ephemer
    assert bot._adj_zustand(slash.a)["instanzen"] == [] and slash.a.ftp.protokoll == []
    assert not [e for e in bot._audit_log]


@pytest.mark.parametrize("name", ["premade:gibtsnicht", "eigen:drop1", "premade:../../etc", "eigen:../x", "premade:a/b"])
def test_add_unbekannter_name_mit_quellen_vorsatz(slash, name):
    inter = add(name=name)
    text = inter.followup.gesendet[0]["content"]
    assert text.startswith("❌") and "gibt es nicht" in text and inter.ephemer
    assert bot._adj_zustand(slash.a)["instanzen"] == [] and slash.a.ftp.protokoll == [] and not list(bot._audit_log)


@pytest.mark.parametrize("x,z", [(-5.0, 6000.0), (99999.0, 6000.0), (5000.0, -1.0)])
def test_add_koordinate_ausserhalb_der_karte(slash, x, z):
    inter = add(x=x, z=z)
    assert inter.followup.gesendet[0]["content"].startswith("❌") and "außerhalb der Karte" in inter.followup.gesendet[0]["content"]
    assert slash.a.ftp.protokoll == [] and bot._adj_zustand(slash.a)["instanzen"] == [] and not list(bot._audit_log)
    assert "outside the map" in add(x=x, z=z, locale=EN).followup.gesendet[0]["content"]


def test_add_hoehe_und_anlage_ueber_dem_kartenrand(slash):
    assert "Höhe" in add(y=99999.0).followup.gesendet[0]["content"]
    assert "Teilen außerhalb" in add(x=10.0).followup.gesendet[0]["content"]
    assert "outside the map" in add(x=10.0, locale=EN).followup.gesendet[0]["content"]


def test_add_ohne_ftp_zugang(slash):
    slash.a.ftp = None
    inter = add()
    assert "FTP" in inter.followup.gesendet[0]["content"] and inter.followup.gesendet[0]["content"].startswith("❌")
    assert "missing FTP access" in add(locale=EN).followup.gesendet[0]["content"]
    assert bot._adj_zustand(slash.a)["instanzen"] == [] and not list(bot._audit_log)


def test_add_schreibfehler_wird_gemeldet_und_nichts_bleibt_zurueck(slash):
    slash.a.ftp.write_fehler = {"cfggameplay.json"}
    inter = add()
    assert "Speichern auf dem Server fehlgeschlagen" in inter.followup.gesendet[0]["content"]
    assert "Saving on the server failed" in add(locale=EN).followup.gesendet[0]["content"]
    assert slash.a.ftp.adj_dateien() == [] and bot._adj_zustand(slash.a)["instanzen"] == [] and not list(bot._audit_log)


def test_add_zu_viele_instanzen(slash):
    for i in range(20):
        _platziere(slash.a, x=1000 + 100 * i, restarts=5)
    inter = add(x=9000.0)
    assert "höchstens 20" in inter.followup.gesendet[0]["content"]
    assert "maximum 20" in add(x=9000.0, locale=EN).followup.gesendet[0]["content"]


def test_befehle_ohne_recht_werden_abgewiesen(slash):
    fremd = _Member(77)
    inter = add(user=fremd)
    assert "Keine Berechtigung" in inter.response.gesendet[0]["content"] and inter.ephemer and inter.response.aufgeschoben is None
    assert "No permission" in add(user=fremd, locale=EN).response.gesendet[0]["content"]
    assert "Keine Berechtigung" in liste(user=fremd).response.gesendet[0]["content"]
    assert "Keine Berechtigung" in entfernen("irgendwas", user=fremd).response.gesendet[0]["content"]
    assert slash.a.ftp.protokoll == [] and bot._adj_zustand(slash.a)["instanzen"] == []


def test_befehle_recht_pro_unterbefehl_per_person_oder_rolle(slash):
    a = slash.a
    a.data["subcommand_perms"] = {"user:77": ["airdrop_add"], "role:9001": ["airdrop_list", "airdrop_remove"]}
    assert add(user=_Member(77)).followup.gesendet[0]["content"].startswith("✅")
    assert "Keine Berechtigung" in liste(user=_Member(77)).response.gesendet[0]["content"]     # list nicht erlaubt
    assert "Keine Berechtigung" in add(user=_Member(78, rollen=[9001])).response.gesendet[0]["content"]
    assert liste(user=_Member(78, rollen=[9001])).response.gesendet[0]["embed"] is not None
    inst = bot._adj_zustand(a)["instanzen"][0]
    assert "Keine Berechtigung" in entfernen(inst["id"], user=_Member(77)).response.gesendet[0]["content"]
    assert entfernen(inst["id"], user=_Member(78, rollen=[9001])).followup.gesendet[0]["content"].startswith("✅")
    assert bot._adj_zustand(a)["instanzen"] == []


def test_befehle_recht_einer_anderen_guild_zaehlt_nicht(slash):
    slash.b.data["subcommand_perms"] = {"user:77": ["airdrop_add"]}          # Rechte des Servers 2000
    assert "Keine Berechtigung" in add(user=_Member(77)).response.gesendet[0]["content"]


def test_befehle_guild_ohne_zugeordneten_server(slash):
    for inter in (add(guild_id=555), liste(guild_id=555), entfernen("x", guild_id=555)):
        assert inter.ephemer and inter.response.gesendet and "Premium" in inter.response.gesendet[0]["content"]
    assert slash.a.ftp.protokoll == []


def test_befehle_mehrere_server_in_einer_guild_brauchen_die_server_angabe(slash):
    bot.connections.add_guild("2000", GID)
    _premade()
    inter = add()
    text = inter.response.gesendet[0]["content"]
    assert "mehrere Nitrado-Server" in text and "server:" in text and "1000" in text and "2000" in text and inter.response.aufgeschoben is None
    assert "manages multiple Nitrado servers" in add(locale=EN).response.gesendet[0]["content"]
    assert slash.a.ftp.protokoll == [] and slash.b.ftp.protokoll == []
    # mit server: genau dieser
    assert add(server="2000").followup.gesendet[0]["content"].startswith("✅")
    assert bot._adj_zustand(slash.a)["instanzen"] == [] and len(bot._adj_zustand(slash.b)["instanzen"]) == 1
    assert slash.a.ftp.protokoll == [] and slash.b.ftp.adj_dateien()
    assert "Kein Server namens" in add(server="9999").response.gesendet[0]["content"]
    assert "mehrere Nitrado-Server" in liste().response.gesendet[0]["content"]


def test_befehle_server_einer_fremden_guild_ist_nicht_waehlbar(slash):
    inter = add(server="2000")                                                # 2000 gehört zu GID_B, nicht zu GID
    assert "Kein Server namens" in inter.response.gesendet[0]["content"]
    assert slash.b.ftp.protokoll == [] and bot._adj_zustand(slash.b)["instanzen"] == []
    inter = entfernen("x", server="2000")
    assert "Kein Server namens" in inter.response.gesendet[0]["content"]


def test_list_platziert_zeigt_name_koordinaten_und_status(slash):
    a1 = _platziere(slash.a, x=4000, y=250, z=5000, restarts=3)
    a2 = _platziere(slash.a, name="drop1", x=8000, y=260, z=9000, restarts=1)
    inter = liste()
    emb = inter.response.gesendet[0]["embed"]
    assert inter.ephemer and "platziert" in emb.title
    zeilen = emb.description.split("\n")
    assert len(zeilen) == 2 and "drop1" in zeilen[0] and "[4000.0, 250.0, 5000.0]" in zeilen[0] and f"`{a1['id']}`" in zeilen[0]
    assert "ab dem nächsten Neustart aktiv" in zeilen[0] and "3 Neustart(s)" in zeilen[0] and "12 obj." in zeilen[0] and "[Premade]" in zeilen[0]
    assert "izurvive" in zeilen[0].lower() and f"`{a2['id']}`" in zeilen[1]
    neustart(slash.a)                                                         # a2 (1x) läuft ab, a1 wurde einmal gesehen
    zeilen = liste().response.gesendet[0]["embed"].description.split("\n")
    assert len(zeilen) == 1 and "aktiv · noch 2 Neustart(s)" in zeilen[0] and "ab dem nächsten" not in zeilen[0]


def test_list_englisch_und_leer(slash):
    assert liste(locale=EN).response.gesendet[0]["embed"].description == "No active airdrops."
    assert liste().response.gesendet[0]["embed"].description == "Keine aktiven Airdrops."
    _eigen(slash.a, "mein1")
    _platziere(slash.a, name="mein1", quelle="eigen", restarts=2)
    zeile = liste(locale=EN).response.gesendet[0]["embed"].description
    assert "active from the next restart" in zeile and "[Own]" in zeile and "2 restart(s)" in zeile


def test_list_scheduler_und_platziert_sind_getrennte_ansichten(slash):
    befehl = _platziere(slash.a, x=15000, z=15000, restarts=4)
    z = scheduler_an(slash.a, anzahl=2, alle=3)
    sch_ids = _ids(slash.a, "scheduler")
    platziert = liste().response.gesendet[0]["embed"].description
    assert befehl["id"] in platziert and not any(i in platziert for i in sch_ids)
    emb = liste(art="scheduler").response.gesendet[0]["embed"]
    assert "Scheduler" in emb.title and all(i in emb.description for i in sch_ids) and befehl["id"] not in emb.description
    assert "**Scheduler:** an · 2 gleichzeitig · Wechsel alle 3 Neustarts · nächster Wechsel in 3 Neustart(s) · 3 Airdrops · 6 Positionen" in emb.description
    neustart(slash.a, 2)
    assert "nächster Wechsel in 1 Neustart(s)" in liste(art="scheduler").response.gesendet[0]["embed"].description
    en = liste(art="scheduler", locale=EN).response.gesendet[0]["embed"].description
    assert "**Scheduler:** on · 2 at a time · rotation every 3 restarts · next rotation in 1 restart(s)" in en
    z["scheduler"]["aktiv"] = False
    assert "**Scheduler:** aus" in liste(art="scheduler").response.gesendet[0]["embed"].description


def test_list_zeigt_nur_instanzen_des_eigenen_servers(slash):
    _platziere(slash.b, restarts=2)
    assert liste().response.gesendet[0]["embed"].description == "Keine aktiven Airdrops."
    bot.connections.add_guild("2000", GID)
    assert len(liste(server="2000").response.gesendet[0]["embed"].description.split("\n")) == 1
    assert liste(server="1000").response.gesendet[0]["embed"].description == "Keine aktiven Airdrops."


def test_remove_per_id_und_per_name_mit_audit(slash):
    i1 = _platziere(slash.a, x=4000, restarts=3)
    i2 = _platziere(slash.a, x=8000, restarts=3)
    inter = entfernen(i1["id"])
    text = inter.followup.gesendet[0]["content"]
    assert inter.response.aufgeschoben == {"ephemeral": True} and text.startswith("✅") and "drop1" in text and "nächsten Neustart" in text
    assert _ids(slash.a) == [i2["id"]] and slash.a.ftp.eintraege() == ["custom/alt.json", i2["datei"]]
    assert slash.a.ftp.adj_dateien() == [f"adj_{i2['id']}.json"]
    eintrag = list(bot._audit_log)[-1]
    assert (eintrag["action"], eintrag["ok"]) == ("/airdrop remove", True) and i1["id"] in eintrag["detail"]
    assert entfernen(" DROP1 ").followup.gesendet[0]["content"].startswith("✅")          # Name ohne Groß/Klein, Leerzeichen egal
    assert _ids(slash.a) == [] and slash.a.ftp.eintraege() == ["custom/alt.json"]
    _platziere(slash.a)
    en = entfernen("drop1", locale=EN).followup.gesendet[0]["content"]
    assert "was removed and disappears with the **next restart**" in en


@pytest.mark.parametrize("name", ["gibtsnicht", "ffffff", "", "  "])
def test_remove_unbekannt(slash, name):
    _platziere(slash.a)
    inter = entfernen(name)
    assert "Keinen passenden platzierten Airdrop" in inter.response.gesendet[0]["content"] and inter.response.aufgeschoben is None
    assert "No matching placed airdrop" in entfernen(name, locale=EN).response.gesendet[0]["content"]
    assert len(_ids(slash.a)) == 1 and not list(bot._audit_log)


def test_remove_scheduler_instanz_ist_nicht_per_befehl_entfernbar(slash):
    z = scheduler_an(slash.a, anzahl=2, alle=3)
    vorher = _ids(slash.a)
    for inst in list(z["instanzen"]):
        assert "Keinen passenden" in entfernen(inst["id"]).response.gesendet[0]["content"]
        assert "Keinen passenden" in entfernen(inst["name"]).response.gesendet[0]["content"]
    assert _ids(slash.a) == vorher and not list(bot._audit_log)


def test_remove_fremde_instanz_eines_anderen_servers(slash):
    fremd = _platziere(slash.b, restarts=2)
    assert "Keinen passenden" in entfernen(fremd["id"]).response.gesendet[0]["content"]
    assert _ids(slash.b) == [fremd["id"]] and slash.b.ftp.adj_dateien()
    bot.connections.add_guild("2000", GID)                                   # jetzt gehören beide zur Guild
    assert "Keinen passenden" in entfernen(fremd["id"], server="1000").response.gesendet[0]["content"]
    assert entfernen(fremd["id"]).followup.gesendet[0]["content"].startswith("✅")              # ohne server: sucht auf allen
    assert _ids(slash.b) == []


def test_remove_schreibfehler_laesst_den_airdrop_stehen(slash):
    inst = _platziere(slash.a, restarts=2)
    slash.a.ftp.write_fehler = {"cfggameplay.json"}
    inter = entfernen(inst["id"])
    assert inter.followup.gesendet[0]["content"].startswith("❌") and "fehlgeschlagen" in inter.followup.gesendet[0]["content"]
    assert _ids(slash.a) == [inst["id"]] and not list(bot._audit_log)


# ── Autocomplete ──────────────────────────────────────────────────────────
def test_autocomplete_name_zeigt_premade_und_eigene_mit_label(slash):
    _eigen(slash.a, "mein1", _objekte()[:5])
    _eigen(slash.b, "fremd")
    _premade("drop2")
    antworten = run(bot._adj_name_autocomplete(_Interaktion(), ""))
    assert [(c.name, c.value) for c in antworten] == [
        ("[Premade] drop1 · 12 obj.", "premade:drop1"), ("[Premade] drop2 · 12 obj.", "premade:drop2"),
        ("[Eigen] mein1 · 5 obj.", "eigen:mein1")]


def test_autocomplete_name_substring_filter_ohne_gross_klein(slash):
    _premade("Wald_Airdrop")
    _eigen(slash.a, "waldhuette")
    assert [c.value for c in run(bot._adj_name_autocomplete(_Interaktion(), "WALD"))] == ["premade:Wald_Airdrop", "eigen:waldhuette"]
    assert [c.value for c in run(bot._adj_name_autocomplete(_Interaktion(), "ldh"))] == ["eigen:waldhuette"]
    assert [c.value for c in run(bot._adj_name_autocomplete(_Interaktion(), "  drop1  "))] == ["premade:drop1"]
    assert run(bot._adj_name_autocomplete(_Interaktion(), "nichts_passt")) == []


def test_autocomplete_name_hoechstens_25(slash):
    for i in range(30):
        _premade(f"p{i:02d}")
    antworten = run(bot._adj_name_autocomplete(_Interaktion(), ""))
    assert len(antworten) == 25 and all(len(c.name) <= 100 and len(c.value) <= 100 for c in antworten)


def test_autocomplete_name_nur_server_der_guild_und_gewaehlter_server(slash):
    _eigen(slash.a, "nur_a")
    _eigen(slash.b, "nur_b")
    assert [c.value for c in run(bot._adj_name_autocomplete(_Interaktion(guild_id=GID_B), ""))] == ["premade:drop1", "eigen:nur_b"]
    assert [c.value for c in run(bot._adj_name_autocomplete(_Interaktion(guild_id=555), ""))] == ["premade:drop1"]
    bot.connections.add_guild("2000", GID)
    alle = [c.value for c in run(bot._adj_name_autocomplete(_Interaktion(), ""))]
    assert alle == ["premade:drop1", "eigen:nur_a", "eigen:nur_b"]
    nur_b = [c.value for c in run(bot._adj_name_autocomplete(_Interaktion(server="2000"), ""))]
    assert nur_b == ["premade:drop1", "eigen:nur_b"]


def test_autocomplete_instanzen_nur_befehls_airdrops_mit_rest(slash):
    befehl = _platziere(slash.a, x=4000, y=250, z=5000, restarts=3)
    scheduler_an(slash.a, anzahl=1, alle=2)
    _platziere(slash.b, restarts=1)
    antworten = run(bot._adj_instanz_autocomplete(_Interaktion(), ""))
    assert [(c.value) for c in antworten] == [befehl["id"]]
    assert antworten[0].name == "drop1 · 4000.0, 250.0, 5000.0 · 3x"
    neustart(slash.a)
    assert run(bot._adj_instanz_autocomplete(_Interaktion(), "DROP"))[0].name.endswith("· 2x")
    assert run(bot._adj_instanz_autocomplete(_Interaktion(), "xyz")) == []
    assert run(bot._adj_instanz_autocomplete(_Interaktion(guild_id=GID_B), ""))[0].value != befehl["id"]


# ── Registrierung ─────────────────────────────────────────────────────────
def test_befehlsgruppe_ist_registriert():
    gruppe = bot.bot.tree.get_command("airdrop")
    assert gruppe is bot.airdrop_group
    assert sorted(c.name for c in gruppe.commands) == ["add", "list", "remove"]
    add_cmd = gruppe.get_command("add")
    assert [p.name for p in add_cmd.parameters] == ["name", "x", "y", "z", "restarts", "server"]
    restarts = next(p for p in add_cmd.parameters if p.name == "restarts")
    assert (restarts.min_value, restarts.max_value) == (1, 100)
    assert [c.value for c in next(p for p in gruppe.get_command("list").parameters if p.name == "art").choices] == ["platziert", "scheduler"]
    assert add_cmd.get_parameter("name").autocomplete and gruppe.get_command("remove").get_parameter("name").autocomplete


def test_unterbefehl_rechte_und_modul_zuordnung_sind_registriert():
    for key in ("airdrop_add", "airdrop_list", "airdrop_remove"):
        assert key in bot._SUBCMD_KEYS
    assert {k for k, kd, *_ in bot._SUBCMD_DEFS if kd == "Airdrops"} == {"airdrop_add", "airdrop_list", "airdrop_remove"}
    assert bot._DISCORD_MODUL_MAP["airdrop"] == "tools.airdropjson"
    assert "tools.airdropjson" in bot.FEATURE_MODULES and bot.FEATURE_MODULES["tools.airdropjson"]["gruppe"] == "Tools"


def test_premium_und_modulstufe_gelten_fuer_airdrop(slash):
    def pruefen(guild_id=GID):
        inter = _Interaktion(guild_id=guild_id)
        return run(bot._premium_check(inter)), inter
    assert pruefen()[0] is True
    erlaubt, inter = pruefen(555)                                           # Guild ohne Server
    assert erlaubt is False and "Premium" in inter.response.gesendet[0]["content"]
    bot.cfg.config["module_tiers"] = {"tools.airdropjson": "under_review"}
    erlaubt, inter = pruefen()                                              # "In Prüfung" sperrt auch Premium-Kunden
    assert erlaubt is False and bot.UNTER_PRUEFUNG_TEXT in inter.response.gesendet[0]["content"]
    bot.cfg.config["module_tiers"] = {"tools.airdropjson": "public"}
    assert pruefen(555)[0] is True
    bot.cfg.config["module_tiers"] = {"tools": "under_review"}              # Kategorie vererbt die Stufe
    assert pruefen()[0] is False


@pytest.mark.parametrize("locale,erwartet", [(None, "Tool: Dashboard → „Airdrops JSON“."), (EN, "Tool: dashboard → “Airdrops JSON”.")])
def test_hilfe_nennt_den_airdrop_befehl(slash, locale, erwartet):
    inter = _Interaktion(locale=locale, befehl="hilfe")
    run(bot.cmd_hilfe.callback(inter))
    text = " ".join(f.value for f in inter.response.gesendet[0]["embed"].fields)
    assert "/airdrop add" in text and "/airdrop list" in text and "/airdrop remove" in text and erwartet in text


# ══════════════════════════════════════════════════════════════════════════
#  Dashboard-API
# ══════════════════════════════════════════════════════════════════════════
PFAD = "/api/tools/airdropjson"


def dash(monkeypatch, conn, handler, wert=None, *, methode=None, match=None, admin=True, discord_id=None, gast=False,
         ohne_sitzung=False, limit_behalten=False):
    """Ruft einen Dashboard-Handler mit einer Sitzung für `conn` auf (wert=None → GET, sonst POST)."""
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": discord_id or sid}, "is_admin": admin,
                            "service_id": conn.service_id, "seen": time.time(), "admin_geprueft_ts": time.time(),
                            **({"is_guest": True} if gast else {})}
    kopf = {} if ohne_sitzung else {"Cookie": f"{bot._SESS_COOKIE}={sid}"}
    request = make_mocked_request(methode or ("GET" if wert is None else "POST"), PFAD, headers=kopf, match_info=match or {})

    async def body(_request):
        return wert
    monkeypatch.setattr(bot, "body", body)
    if not limit_behalten:
        bot._DASH_RATE_LIMIT_LAST.clear()
    antwort = asyncio.run(handler(request))
    return antwort.status, json.loads(antwort.body)


def besitzer(monkeypatch, conn, handler, wert=None, **kw):
    """Der Server-Eigentümer ohne Betreiber-Rolle (Kunde)."""
    return dash(monkeypatch, conn, handler, wert, admin=False, discord_id=conn.service_id, **kw)


def gast(monkeypatch, conn, handler, wert=None, **kw):
    return dash(monkeypatch, conn, handler, wert, admin=False, discord_id="900", gast=True, **kw)


def hochladen(monkeypatch, conn, handler, name, objekte=None, **extra):
    return dash(monkeypatch, conn, handler, {"name": name, "content": _text(objekte), **extra})


def _audit_aktionen():
    return [e["action"] for e in bot._audit_log]


# ── GET ───────────────────────────────────────────────────────────────────
def test_api_get_liefert_alles_fuer_die_seite(monkeypatch, env):
    _premade("drop1")
    _eigen(env.a, "mein1", _objekte()[:3])
    _eigen(env.b, "fremd")
    inst = _platziere(env.a, restarts=2)
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_get)
    assert status == 200 and r["ok"] is True
    d = r["data"]
    assert set(d) == {"premade", "eigene", "instanzen", "scheduler", "ist_betreiber", "karte", "warn_objekte", "ftp", "kann_edit"}
    assert [m["name"] for m in d["premade"]] == ["drop1"] and d["premade"][0]["objekte"] == 12
    assert [(m["name"], m["objekte"]) for m in d["eigene"]] == [("mein1", 3)]            # nichts von Server 2000
    assert d["ist_betreiber"] is True and d["karte"] == 15360 and d["warn_objekte"] == 200 and d["ftp"] is True
    assert d["scheduler"] == {"aktiv": False, "anzahl": 1, "alle_neustarts": 1, "airdrops": [], "positionen": [], "zaehler": 0}
    assert [{k: i[k] for k in ("id", "name", "quelle", "x", "y", "z", "restarts", "gesehen", "von", "objekte")} for i in d["instanzen"]] == [
        {"id": inst["id"], "name": "drop1", "quelle": "premade", "x": 5000.0, "y": 300.0, "z": 6000.0, "restarts": 2, "gesehen": 0,
         "von": "befehl", "objekte": 12}]
    assert set(d["instanzen"][0]) == {"id", "name", "quelle", "x", "y", "z", "restarts", "gesehen", "von", "objekte", "erstellt"}   # kein user/datei


def test_api_get_kunde_sieht_premade_aber_ist_kein_betreiber(monkeypatch, env):
    _premade("drop1")
    d = besitzer(monkeypatch, env.a, bot.api_tools_airdropjson_get)[1]["data"]
    assert d["ist_betreiber"] is False and [m["name"] for m in d["premade"]] == ["drop1"]


def test_api_get_zeigt_nur_den_eigenen_server_auch_mit_admin_sitzung_auf_dem_anderen(monkeypatch, env):
    _premade()
    _eigen(env.a, "nur_a")
    inst_a = _platziere(env.a)
    d = dash(monkeypatch, env.b, bot.api_tools_airdropjson_get)[1]["data"]
    assert d["eigene"] == [] and d["instanzen"] == [] and [m["name"] for m in d["premade"]] == ["drop1"]
    d = dash(monkeypatch, env.a, bot.api_tools_airdropjson_get)[1]["data"]
    assert [i["id"] for i in d["instanzen"]] == [inst_a["id"]] and [m["name"] for m in d["eigene"]] == ["nur_a"]


def test_api_get_ohne_sitzung_401_und_ohne_ftp_flag_aus(monkeypatch, env):
    assert dash(monkeypatch, env.a, bot.api_tools_airdropjson_get, ohne_sitzung=True)[0] == 401
    env.a.ftp = None
    assert dash(monkeypatch, env.a, bot.api_tools_airdropjson_get)[1]["data"]["ftp"] is False
    env.a.ftp = FTP()
    env.a.data["ftp_mission_dir"] = ""
    assert dash(monkeypatch, env.a, bot.api_tools_airdropjson_get)[1]["data"]["ftp"] is False


def test_api_modulstufe_sperrt_kunden_aber_nicht_den_betreiber(monkeypatch, env):
    bot.cfg.config["module_tiers"] = {"tools.airdropjson": "under_review"}
    status, r = besitzer(monkeypatch, env.a, bot.api_tools_airdropjson_get)
    assert status == 403 and r.get("code") == "under_review"
    assert dash(monkeypatch, env.a, bot.api_tools_airdropjson_get)[0] == 200
    bot.cfg.config["module_tiers"] = {"tools.airdropjson": "premium"}
    env.a.data["guild_ids"] = []                       # nicht freigeschaltet
    bot.connections.remove_guild("1000", None)
    status, r = besitzer(monkeypatch, env.a, bot.api_tools_airdropjson_get)
    assert status == 403
    assert besitzer(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, {"name": "x", "content": _text()})[0] == 403


def test_api_gast_rechte_view_und_edit(monkeypatch, env):
    _premade("drop1")
    _eigen(env.a, "mein1")
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_get)[0] == 403                    # kein Eintrag
    env.a.data["dashboard_perms"] = {"user:900": {"tools": ["view"]}}
    status, r = gast(monkeypatch, env.a, bot.api_tools_airdropjson_get)
    assert status == 200 and r["data"]["ist_betreiber"] is False
    # nur ansehen: nichts davon darf schreiben
    eigene = {"name": "x", "content": _text()}
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, eigene)[0] == 403
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_delete, methode="DELETE", match={"name": "mein1"})[0] == 403
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_scheduler_post, {"anzahl": 1, "alle_neustarts": 1})[0] == 403
    inst = _platziere(env.a)
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_instanz_entfernen, methode="POST", match={"id": inst["id"]})[0] == 403
    assert bot._adj_laden("eigen", "1000", "x") is None and bot._adj_laden("eigen", "1000", "mein1") is not None
    assert _ids(env.a) == [inst["id"]]
    # mit Bearbeiten-Recht darf er Eigene schreiben/löschen, aber nie Premade
    env.a.data["dashboard_perms"] = {"user:900": {"tools": ["view", "edit"]}}
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, eigene)[0] == 200
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_delete, methode="DELETE", match={"name": "x"})[0] == 200
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, eigene)[0] == 403
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_premade_delete, methode="DELETE", match={"name": "drop1"})[0] == 403
    assert bot._adj_laden("premade", "", "drop1") is not None and bot._adj_laden("premade", "", "x") is None
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_instanz_entfernen, methode="POST", match={"id": inst["id"]})[0] == 200


# ── Premade ───────────────────────────────────────────────────────────────
def test_api_premade_speichern_nur_fuer_den_betreiber(monkeypatch, env):
    status, r = hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "Neuer_Drop")
    assert status == 200 and r["data"]["gespeichert"] is True and r["data"]["warnung"] is False
    assert r["data"]["meta"]["name"] == "Neuer_Drop" and r["data"]["meta"]["objekte"] == 12
    assert bot._adj_laden("premade", "", "Neuer_Drop") == _objekte()
    assert "Tool: Premade-Airdrop gespeichert" in _audit_aktionen()
    # Kunde (Eigentümer ohne Betreiber-Rolle) und Gast: 403 und nichts geschrieben
    wert = {"name": "Kunde", "content": _text()}
    assert besitzer(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, wert)[0] == 403
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, wert)[0] == 403
    assert dash(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, wert, ohne_sitzung=True)[0] == 401
    assert [m["name"] for m in bot._adj_liste("premade")] == ["Neuer_Drop"]


def test_api_premade_ist_fuer_alle_server_sichtbar(monkeypatch, env):
    hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "global")
    d = besitzer(monkeypatch, env.b, bot.api_tools_airdropjson_get)[1]["data"]
    assert [m["name"] for m in d["premade"]] == ["global"]


def test_api_premade_endung_json_wird_entfernt_und_text_getrimmt(monkeypatch, env):
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "  Mein_Drop.JSON  ")[0] == 200
    assert [m["name"] for m in bot._adj_liste("premade")] == ["Mein_Drop"]


@pytest.mark.parametrize("name", ["", "   ", None, "a b", "ä", "a/b", "../x", "..", "a\\b", "x" * 41, "a.b", ".json", "a:b", "a\x00b"])
def test_api_premade_ungueltige_namen(monkeypatch, env, name):
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, {"name": name, "content": _text()})
    assert status == 400 and "Der Name darf nur" in r["error"]
    assert not os.path.exists("airdrop_json")


@pytest.mark.parametrize("content,teil", [
    (None, "JSON-Inhalt"), ("", "JSON-Inhalt"), ("   \n ", "JSON-Inhalt"), (123, "JSON-Inhalt"), ({"Objects": []}, "JSON-Inhalt"), (["x"], "JSON-Inhalt"),
    ("kein json", "kein gültiges JSON"), ("{}", "Objects"), ('{"Objects": []}', "keine Objekte"),
    ('{"Containers": []}', "Expansion"), ('{"Objects": [{"name": "a/b", "pos": [1,2,3]}]}', "Namen"),
    ('{"Objects": [{"name": "A", "pos": [1,2]}]}', "pos"), ('{"Objects": [{"name": "A", "pos": [1,2,3], "scale": 0}]}', "scale"),
    ('{"Objects": [{"name": "A", "pos": [1,2,3], "ypr": [1]}]}', "ypr"),
])
def test_api_premade_ungueltige_dateien_haben_klare_fehlertexte(monkeypatch, env, content, teil):
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, {"name": "x", "content": content})
    assert status == 400 and r["ok"] is False and teil in r["error"]
    assert not os.path.exists("airdrop_json")


def test_api_premade_zu_grosse_datei(monkeypatch, env):
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, {"name": "x", "content": _text(_viele(1)) + " " * 5_000_100})
    assert status == 400 and "5 MB" in r["error"]


def test_api_premade_riesige_zahl_in_der_datei_ist_ein_400_kein_500(monkeypatch, env):
    inhalt = '{"Objects": [{"name": "A", "pos": [1%s, 2, 3]}]}' % ("0" * 400)
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, {"name": "x", "content": inhalt})
    assert status == 400 and r["ok"] is False


def test_api_premade_ueberschreib_konflikt(monkeypatch, env):
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "d1", _roh_objekte()[:3])[0] == 200
    status, r = hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "d1", _roh_objekte()[:5])
    assert status == 200 and r["data"] == {"konflikt": True, "name": "d1"}
    assert len(bot._adj_laden("premade", "", "d1")) == 3                                  # nichts überschrieben
    status, r = hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "d1", _roh_objekte()[:5], overwrite=False)
    assert r["data"].get("konflikt") is True
    status, r = hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "d1", _roh_objekte()[:5], overwrite=True)
    assert status == 200 and r["data"]["gespeichert"] is True and len(bot._adj_laden("premade", "", "d1")) == 5
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "D1", _roh_objekte()[:2])[1]["data"].get("gespeichert")   # anderer Name (Großschreibung)


def test_api_premade_limit_100(monkeypatch, env):
    for i in range(100):
        bot._adj_speichern("premade", "", f"p{i}", _objekte()[:1], "t")
    status, r = hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "p_neu")
    assert status == 400 and "höchstens 100" in r["error"] and len(bot._adj_liste("premade")) == 100
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "p5")[1]["data"] == {"konflikt": True, "name": "p5"}
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "p5", overwrite=True)[0] == 200
    assert len(bot._adj_liste("premade")) == 100
    assert dash(monkeypatch, env.a, bot.api_tools_airdropjson_premade_delete, methode="DELETE", match={"name": "p0"})[0] == 200
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "p_neu")[0] == 200


def test_api_premade_warnung_ab_200_objekten(monkeypatch, env):
    status, r = hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "gross", _viele(250))
    assert status == 200 and r["data"]["warnung"] is True and r["data"]["meta"]["objekte"] == 250
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, "klein", _viele(199))[1]["data"]["warnung"] is False


def test_api_premade_schreiben_wird_gedrosselt(monkeypatch, env):
    wert = {"name": "x", "content": _text()}
    assert dash(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, wert, discord_id="77")[0] == 200
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_premade_post, wert, discord_id="77", limit_behalten=True)
    assert status == 429 and r["code"] == "rate_limit"


def test_api_premade_loeschen(monkeypatch, env):
    _premade("weg")
    _premade("bleibt")
    wert = dict(methode="DELETE")
    assert besitzer(monkeypatch, env.a, bot.api_tools_airdropjson_premade_delete, match={"name": "weg"}, **wert)[0] == 403
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_premade_delete, match={"name": "weg"}, **wert)[0] == 403
    assert bot._adj_laden("premade", "", "weg") is not None
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_premade_delete, match={"name": "weg"}, **wert)
    assert status == 200 and r["data"] == {"geloescht": True} and [m["name"] for m in bot._adj_liste("premade")] == ["bleibt"]
    assert "Tool: Premade-Airdrop gelöscht" in _audit_aktionen()
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_premade_delete, match={"name": "weg"}, **wert)
    assert status == 404 and "gibt es nicht" in r["error"]
    for name in ("../bleibt", "a/b", "", "x" * 41, "bleibt.json"):
        status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_premade_delete, match={"name": name}, **wert)
        assert status == 400 and "Ungültiger Name" in r["error"], name
    assert bot._adj_laden("premade", "", "bleibt") is not None


def test_api_premade_loeschen_beruehrt_keine_eigenen_dateien(monkeypatch, env):
    _eigen(env.a, "mein1")
    status, _ = dash(monkeypatch, env.a, bot.api_tools_airdropjson_premade_delete, methode="DELETE", match={"name": "mein1"})
    assert status == 404 and bot._adj_laden("eigen", "1000", "mein1") is not None


# ── Eigene ────────────────────────────────────────────────────────────────
def test_api_eigene_speichern_und_loeschen_als_kunde(monkeypatch, env):
    status, r = hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, "mein1")
    assert status == 200 and r["data"]["gespeichert"] is True
    kunde = {"name": "kunde1", "content": _text()}
    assert besitzer(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, kunde)[0] == 200
    assert [m["name"] for m in bot._adj_liste("eigen", "1000")] == ["kunde1", "mein1"]
    assert bot._adj_liste("eigen", "2000") == [] and bot._adj_liste("premade") == []
    assert "Tool: eigener Airdrop gespeichert" in _audit_aktionen()
    status, r = besitzer(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_delete, methode="DELETE", match={"name": "kunde1"})
    assert status == 200 and r["data"] == {"geloescht": True} and [m["name"] for m in bot._adj_liste("eigen", "1000")] == ["mein1"]
    assert "Tool: eigener Airdrop gelöscht" in _audit_aktionen()


def test_api_eigene_gehoeren_nur_dem_eigenen_server(monkeypatch, env):
    _eigen(env.a, "von_a")
    _eigen(env.b, "von_b")
    # Server 2000 kann die Datei von 1000 weder sehen noch löschen
    assert [m["name"] for m in dash(monkeypatch, env.b, bot.api_tools_airdropjson_get)[1]["data"]["eigene"]] == ["von_b"]
    status, r = besitzer(monkeypatch, env.b, bot.api_tools_airdropjson_eigene_delete, methode="DELETE", match={"name": "von_a"})
    assert status == 404 and bot._adj_laden("eigen", "1000", "von_a") is not None
    # Gleicher Name auf beiden Servern ist erlaubt und kein Konflikt
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, "gleich")[1]["data"]["gespeichert"]
    assert hochladen(monkeypatch, env.b, bot.api_tools_airdropjson_eigene_post, "gleich")[1]["data"]["gespeichert"]
    # Ein Kunde ohne Rechte auf Server 1000 kommt nicht an dessen Dateien
    status, _ = dash(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, {"name": "x", "content": _text()}, admin=False, discord_id="2000")
    assert status == 403 and bot._adj_laden("eigen", "1000", "x") is None


def test_api_eigene_loeschen_trifft_nie_premade_oder_ungueltige_namen(monkeypatch, env):
    _premade("drop1")
    for name, erwartet in (("drop1", 404), ("gibtsnicht", 404), ("../x", 400), ("a/b", 400), ("", 400)):
        status, _ = dash(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_delete, methode="DELETE", match={"name": name})
        assert status == erwartet, name
    assert bot._adj_laden("premade", "", "drop1") is not None


def test_api_eigene_konflikt_und_limit_20(monkeypatch, env):
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, "e1", _roh_objekte()[:3])[0] == 200
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, "e1")[1]["data"] == {"konflikt": True, "name": "e1"}
    assert len(bot._adj_laden("eigen", "1000", "e1")) == 3
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, "e1", overwrite=True)[0] == 200
    for i in range(2, 21):
        bot._adj_speichern("eigen", "1000", f"e{i}", _objekte()[:1], "t")
    assert len(bot._adj_liste("eigen", "1000")) == 20
    status, r = hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, "e_neu")
    assert status == 400 and "höchstens 20" in r["error"] and len(bot._adj_liste("eigen", "1000")) == 20
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, "e5", overwrite=True)[0] == 200
    assert hochladen(monkeypatch, env.b, bot.api_tools_airdropjson_eigene_post, "e_neu")[0] == 200            # Limit gilt je Server


@pytest.mark.parametrize("name", ["", "a/b", "../x", "x" * 41, "ä"])
def test_api_eigene_ungueltige_namen(monkeypatch, env, name):
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, {"name": name, "content": _text()})
    assert status == 400 and "Der Name darf nur" in r["error"] and not os.path.exists("airdrop_json")


def test_api_eigene_ungueltige_datei(monkeypatch, env):
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, {"name": "x", "content": '{"Containers": []}'})
    assert status == 400 and "Expansion" in r["error"]


def test_api_eigene_speichern_wird_gedrosselt(monkeypatch, env):
    wert = {"name": "x", "content": _text()}
    assert besitzer(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, wert)[0] == 200
    assert besitzer(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, wert, limit_behalten=True)[0] == 429


# ── Scheduler ─────────────────────────────────────────────────────────────
def sch_wert(**changes):
    wert = {"aktiv": True, "anzahl": 2, "alle_neustarts": 3,
            "airdrops": [{"quelle": "premade", "name": "drop1"}, {"quelle": "premade", "name": "drop2"}],
            "positionen": [dict(p) for p in POSITIONEN[:4]]}
    wert.update(changes)
    return wert


@pytest.fixture
def sch(monkeypatch, env):
    _premade("drop1")
    _premade("drop2")
    return env


def test_api_scheduler_speichert_und_wendet_sofort_an(monkeypatch, sch):
    a = sch.a
    befehl = _platziere(a, x=15000, z=15000, restarts=9)
    status, r = dash(monkeypatch, a, bot.api_tools_airdropjson_scheduler_post, sch_wert())
    assert status == 200 and r["ok"] is True
    d = r["data"]
    assert d["scheduler"]["aktiv"] is True and d["scheduler"]["anzahl"] == 2 and d["scheduler"]["alle_neustarts"] == 3
    assert d["scheduler"]["zaehler"] == 0 and len(d["scheduler"]["airdrops"]) == 2 and len(d["scheduler"]["positionen"]) == 4
    von = collections.Counter(i["von"] for i in d["instanzen"])
    assert von == {"befehl": 1, "scheduler": 2}
    sched = [i for i in d["instanzen"] if i["von"] == "scheduler"]
    assert len({(i["x"], i["z"]) for i in sched}) == 2 and all(i["restarts"] == 3 and i["gesehen"] == 0 for i in sched)
    assert a.ftp.eintraege() == ["custom/alt.json", befehl["datei"]] + [f"custom/adj_{i['id']}.json" for i in sched]
    assert len(a.ftp.adj_dateien()) == 3
    assert _platte("1000")["airdrop_json"]["scheduler"]["aktiv"] is True and len(_platte("1000")["airdrop_json"]["instanzen"]) == 3
    assert any(a_.startswith("Tool: Airdrop-Scheduler gespeichert") for a_ in _audit_aktionen())
    assert "an · 2 · alle 3" in list(bot._audit_log)[-1]["detail"]
    assert bot._adj_zustand(sch.b)["instanzen"] == [] and sch.b.ftp.protokoll == []


def test_api_scheduler_aenderung_ersetzt_die_belegung_und_setzt_den_zaehler_zurueck(monkeypatch, sch):
    dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert())
    alt = _ids(sch.a, "scheduler")
    neustart(sch.a)
    assert bot._adj_zustand(sch.a)["scheduler"]["zaehler"] == 1
    status, r = dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert(anzahl=1, alle_neustarts=5))
    assert status == 200 and len(r["data"]["instanzen"]) == 1 and r["data"]["scheduler"]["zaehler"] == 0
    assert not set(alt) & set(_ids(sch.a)) and len(sch.a.ftp.adj_dateien()) == 1 and len(sch.a.ftp.eintraege()) == 2


def test_api_scheduler_aus_traegt_die_scheduler_instanzen_aus_und_behaelt_die_einstellungen(monkeypatch, sch):
    befehl = _platziere(sch.a, x=15000, z=15000, restarts=9)
    dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert())
    status, r = dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert(aktiv=False))
    assert status == 200 and [i["id"] for i in r["data"]["instanzen"]] == [befehl["id"]]
    assert r["data"]["scheduler"]["aktiv"] is False and r["data"]["scheduler"]["anzahl"] == 2 and len(r["data"]["scheduler"]["airdrops"]) == 2
    assert sch.a.ftp.eintraege() == ["custom/alt.json", befehl["datei"]] and len(sch.a.ftp.adj_dateien()) == 1
    neustart(sch.a, 3)                                                  # ausgeschaltet: kein Neustart bringt neue Scheduler-Airdrops
    assert _ids(sch.a, "scheduler") == []


def test_api_scheduler_ftp_fehler_ergibt_502_und_setzt_die_einstellung_zurueck(monkeypatch, sch):
    sch.a.ftp.write_fehler = {"cfggameplay.json"}
    status, r = dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert())
    assert status == 502 and "fehlgeschlagen" in r["error"]
    z = bot._adj_zustand(sch.a)
    assert z["scheduler"] == {"aktiv": False, "anzahl": 1, "alle_neustarts": 1, "zaehler": 0, "airdrops": [], "positionen": []}
    assert z["instanzen"] == [] and sch.a.ftp.adj_dateien() == [] and sch.a.ftp.eintraege() == ["custom/alt.json"]
    assert not any(a_.startswith("Tool: Airdrop-Scheduler") for a_ in _audit_aktionen())


def test_api_scheduler_ftp_fehler_behaelt_die_vorige_aktive_einstellung_und_belegung(monkeypatch, sch):
    dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert())
    vorher = copy.deepcopy(bot._adj_zustand(sch.a))
    eintraege = list(sch.a.ftp.eintraege())
    sch.a.ftp.write_fehler = {"cfggameplay.json"}
    status, _ = dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert(anzahl=1, alle_neustarts=9))
    assert status == 502
    assert bot._adj_zustand(sch.a) == vorher and sch.a.ftp.eintraege() == eintraege and len(sch.a.ftp.adj_dateien()) == 2


def test_api_scheduler_ohne_ftp_aktivieren_ist_409(monkeypatch, sch):
    sch.a.ftp = None
    status, r = dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert())
    assert status == 409 and r["error"] == bot._ADJ_FEHLER_FTP
    assert bot._adj_zustand(sch.a)["scheduler"]["aktiv"] is False
    sch.a.ftp = FTP()
    sch.a.data["ftp_mission_dir"] = ""
    assert dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert())[0] == 409


def test_api_scheduler_aus_ohne_ftp_braucht_keinen_ftp_zugang_wenn_nie_aktiv(monkeypatch, sch):
    sch.a.ftp = None
    status, r = dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert(aktiv=False, anzahl=4))
    assert status == 200 and r["data"]["scheduler"]["aktiv"] is False and r["data"]["scheduler"]["anzahl"] == 4
    assert r["data"]["ftp"] is False and _platte("1000")["airdrop_json"]["scheduler"]["anzahl"] == 4


def test_api_scheduler_aus_ohne_ftp_aber_mit_scheduler_instanzen_ist_409(monkeypatch, sch):
    dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert())
    sch.a.ftp = None
    status, r = dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert(aktiv=False))
    assert status == 409 and bot._adj_zustand(sch.a)["scheduler"]["aktiv"] is True and len(_ids(sch.a, "scheduler")) == 2


@pytest.mark.parametrize("aenderung,teil", [
    ({"anzahl": 0}, "1 und 10"), ({"anzahl": 11}, "1 und 10"), ({"anzahl": "x"}, "ganze Zahlen"), ({"alle_neustarts": 0}, "1 und 100"),
    ({"alle_neustarts": 101}, "1 und 100"), ({"airdrops": []}, "mindestens einen Airdrop"),
    ({"airdrops": [{"quelle": "x", "name": "a"}]}, "Airdrop-Auswahl"), ({"airdrops": "text"}, "Airdrop-Auswahl"),
    ({"positionen": [{"x": 1, "y": 2, "z": 3}]}, "mindestens so viele Positionen"), ({"positionen": [{"x": "a", "y": 2, "z": 3}] * 2}, "x, y und z"),
    ({"positionen": [{"x": i, "y": 1, "z": i} for i in range(51)]}, "50"),
])
def test_api_scheduler_validierungsfehler_sind_400_und_aendern_nichts(monkeypatch, sch, aenderung, teil):
    status, r = dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert(**aenderung))
    assert status == 400 and teil in r["error"]
    z = bot._adj_zustand(sch.a)
    assert z["scheduler"]["aktiv"] is False and z["instanzen"] == [] and sch.a.ftp.protokoll == []


def test_api_scheduler_position_mit_riesiger_zahl_ist_400_kein_500(monkeypatch, sch):
    status, r = dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post,
                     sch_wert(positionen=[{"x": 10 ** 400, "y": 2, "z": 3}, {"x": 1, "y": 2, "z": 3}]))
    assert status == 400 and r["ok"] is False


def test_api_scheduler_nur_mit_bearbeiten_recht_und_je_server(monkeypatch, sch):
    assert gast(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert())[0] == 403
    assert dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert(), ohne_sitzung=True)[0] == 401
    assert besitzer(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert())[0] == 200
    assert bot._adj_zustand(sch.b)["scheduler"]["aktiv"] is False and sch.b.ftp.protokoll == []


def test_api_scheduler_wird_gedrosselt(monkeypatch, sch):
    assert dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert(), discord_id="5")[0] == 200
    assert dash(monkeypatch, sch.a, bot.api_tools_airdropjson_scheduler_post, sch_wert(), discord_id="5", limit_behalten=True)[0] == 429


# ── Platzierte Airdrops entfernen ─────────────────────────────────────────
def test_api_instanz_entfernen(monkeypatch, env):
    _premade()
    i1 = _platziere(env.a, x=4000, restarts=3)
    i2 = _platziere(env.a, x=8000, restarts=3)
    status, r = besitzer(monkeypatch, env.a, bot.api_tools_airdropjson_instanz_entfernen, methode="POST", match={"id": i1["id"]})
    assert status == 200 and [i["id"] for i in r["data"]["instanzen"]] == [i2["id"]]
    assert env.a.ftp.eintraege() == ["custom/alt.json", i2["datei"]] and env.a.ftp.adj_dateien() == [f"adj_{i2['id']}.json"]
    assert "Tool: Airdrop entfernt" in _audit_aktionen() and i1["id"] in list(bot._audit_log)[-1]["detail"]
    assert _platte("1000")["airdrop_json"]["instanzen"][0]["id"] == i2["id"]


def test_api_instanz_entfernen_fremde_und_ungueltige_ids_sind_404(monkeypatch, env):
    _premade()
    meine = _platziere(env.a)
    fremd = _platziere(env.b)
    for ident in (fremd["id"], "ffffff", "000000", "", "../x", "ABCDEF", "abcde", "abcdef0", "abcdeg", " ", meine["id"].upper() + "0"):
        status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_instanz_entfernen, methode="POST", match={"id": ident})
        assert status == 404 and "gibt es nicht" in r["error"], ident
    assert _ids(env.a) == [meine["id"]] and _ids(env.b) == [fremd["id"]] and env.b.ftp.adj_dateien()


def test_api_instanz_entfernen_nur_mit_bearbeiten_recht(monkeypatch, env):
    _premade()
    inst = _platziere(env.a)
    assert gast(monkeypatch, env.a, bot.api_tools_airdropjson_instanz_entfernen, methode="POST", match={"id": inst["id"]})[0] == 403
    assert dash(monkeypatch, env.a, bot.api_tools_airdropjson_instanz_entfernen, methode="POST", match={"id": inst["id"]}, ohne_sitzung=True)[0] == 401
    assert _ids(env.a) == [inst["id"]]


def test_api_instanz_entfernen_ftp_fehler_ist_502_und_laesst_den_airdrop_stehen(monkeypatch, env):
    _premade()
    inst = _platziere(env.a)
    env.a.ftp.write_fehler = {"cfggameplay.json"}
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_instanz_entfernen, methode="POST", match={"id": inst["id"]})
    assert status == 502 and "fehlgeschlagen" in r["error"] and _ids(env.a) == [inst["id"]]
    env.a.ftp = None
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_instanz_entfernen, methode="POST", match={"id": inst["id"]})
    assert status == 502 and r["error"] == bot._ADJ_FEHLER_FTP and _ids(env.a) == [inst["id"]]


def test_api_instanz_entfernen_wird_gedrosselt(monkeypatch, env):
    _premade()
    i1, i2 = _platziere(env.a, x=3000), _platziere(env.a, x=9000)
    kw = dict(methode="POST", discord_id="5")
    assert dash(monkeypatch, env.a, bot.api_tools_airdropjson_instanz_entfernen, match={"id": i1["id"]}, **kw)[0] == 200
    assert dash(monkeypatch, env.a, bot.api_tools_airdropjson_instanz_entfernen, match={"id": i2["id"]}, limit_behalten=True, **kw)[0] == 429
    assert _ids(env.a) == [i2["id"]]


# ── Ende zu Ende: Dashboard → Befehl → Neustart ───────────────────────────
def test_ende_zu_ende_hochladen_platzieren_neustart_entfernen(monkeypatch, env):
    monkeypatch.setattr(discord, "Member", _Member)
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, "mein_drop", _roh_objekte())[0] == 200
    inter = add(name="eigen:mein_drop", restarts=2)
    assert inter.followup.gesendet[0]["content"].startswith("✅")
    inst = bot._adj_zustand(env.a)["instanzen"][0]
    assert dash(monkeypatch, env.a, bot.api_tools_airdropjson_get)[1]["data"]["instanzen"][0]["id"] == inst["id"]
    neustart(env.a)
    assert "aktiv · noch 1 Neustart(s)" in liste().response.gesendet[0]["embed"].description
    neustart(env.a)
    assert liste().response.gesendet[0]["embed"].description == "Keine aktiven Airdrops."
    assert env.a.ftp.adj_dateien() == [] and env.a.ftp.eintraege() == ["custom/alt.json"]
    assert [m["name"] for m in bot._adj_liste("eigen", "1000")] == ["mein_drop"]            # die Datei im Speicher bleibt


# ── Backup, Meta, Routen ──────────────────────────────────────────────────
def test_backup_enthaelt_premade_und_eigene_dateien_aller_server(env):
    _premade("drop1")
    _eigen(env.a, "mein_a")
    _eigen(env.b, "mein_b", _objekte()[:2])
    pfad = bot._voll_backup_erstellen()
    assert pfad and os.path.isfile(pfad)
    with zipfile.ZipFile(pfad) as z:
        namen = set(z.namelist())
        assert {"airdrop_json/premade/drop1.json", "airdrop_json/premade/.index.json",
                "airdrop_json/eigene/1000/mein_a.json", "airdrop_json/eigene/1000/.index.json",
                "airdrop_json/eigene/2000/mein_b.json", "airdrop_json/eigene/2000/.index.json"} <= namen
        assert not [n for n in namen if "\\" in n or n.endswith(".tmp")]
        assert z.read("airdrop_json/premade/drop1.json") == open(os.path.join("airdrop_json", "premade", "drop1.json"), "rb").read()
        assert json.loads(z.read("airdrop_json/eigene/2000/mein_b.json"))["Objects"][0]["name"] == _objekte()[0]["name"]
        assert "connections.json" in namen                                                 # der übrige Inhalt bleibt


def test_backup_ohne_airdrop_dateien_funktioniert_weiter(env):
    pfad = bot._voll_backup_erstellen()
    with zipfile.ZipFile(pfad) as z:
        assert "connections.json" in z.namelist() and not [n for n in z.namelist() if n.startswith("airdrop_json")]


def test_backup_wiederherstellbar_die_gesicherte_datei_ist_wieder_ladbar(env, tmp_path):
    _premade("drop1")
    pfad = bot._voll_backup_erstellen()
    with zipfile.ZipFile(pfad) as z:
        roh = z.read("airdrop_json/premade/drop1.json").decode("utf-8")
    assert bot._adj_pruefen(roh)[0] == _objekte()


def test_tool_ist_in_der_tool_liste_und_den_modulen(monkeypatch, env):
    status, r = dash(monkeypatch, env.a, bot.api_tools_meta)
    assert status == 200
    eintrag = next(t for t in r["data"]["tools"] if t["key"] == "airdropjson")
    assert eintrag["label"] == "Airdrops JSON" and eintrag["selectable"] is True
    assert ("airdropjson", "📦", "Airdrops JSON") in bot._TOOL_LISTE
    assert "tools.airdropjson" in bot.FEATURE_MODULES
    # Kunde mit Premium darf es wählen; "in Prüfung" blendet es für Kunden aus (sichtbar, aber gesperrt)
    kunde = besitzer(monkeypatch, env.a, bot.api_tools_meta)[1]["data"]["tools"]
    assert next(t for t in kunde if t["key"] == "airdropjson")["selectable"] is True
    bot.cfg.config["module_tiers"] = {"tools.airdropjson": "under_review"}
    kunde = besitzer(monkeypatch, env.a, bot.api_tools_meta)[1]["data"]["tools"]
    gesperrt = next(t for t in kunde if t["key"] == "airdropjson")
    assert gesperrt["selectable"] is False and gesperrt["tier"] == "under_review"


def test_routen_sind_registriert():
    app = bot.build_app()
    routen = {(r.method, r.resource.canonical) for r in app.router.routes()}
    erwartet = {("GET", PFAD), ("POST", PFAD + "/premade"), ("DELETE", PFAD + "/premade/{name}"), ("POST", PFAD + "/eigene"),
                ("DELETE", PFAD + "/eigene/{name}"), ("POST", PFAD + "/scheduler"), ("POST", PFAD + "/platziert/{id}/entfernen")}
    assert erwartet <= routen


# ══════════════════════════════════════════════════════════════════════════
#  Nachbesserungen aus dem Sicherheits-Review
# ══════════════════════════════════════════════════════════════════════════
def test_datei_namens_index_zerstoert_den_index_nicht(monkeypatch, env):
    """Der Index heißt .index.json (Punkt: nie ein gültiger Name) - eine Datei „index“ darf ihn nicht überschreiben."""
    for n in ("a1", "a2", "index"):
        assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, n)[0] == 200
    namen = [m["name"] for m in bot._adj_liste("eigen", "1000")]
    assert namen == ["a1", "a2", "index"]
    assert os.path.exists(os.path.join("airdrop_json", "eigene", "1000", ".index.json"))


def test_speicherkontingent_je_server(monkeypatch, env):
    monkeypatch.setattr(bot, "_ADJ_QUOTA_EIGENE", 100_000)
    gross = _viele(900)                                          # ~180 KB
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, "a1")[0] == 200     # ~4 KB
    status, r = hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, "gross", gross)
    assert status == 400 and "kontingent" in r["error"]
    monkeypatch.setattr(bot, "_ADJ_QUOTA_EIGENE", 1_000_000)     # anderes Kontingent -> Upload klappt
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, "gross", gross)[0] == 200


def test_uebersicht_enthaelt_keine_discord_ids_der_hochlader(monkeypatch, env):
    assert hochladen(monkeypatch, env.a, bot.api_tools_airdropjson_eigene_post, "a1")[0] == 200
    r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_get)[1]["data"]
    assert all("von" not in m and "hochgeladen" not in m for m in r["eigene"] + r["premade"])
    assert set(r["eigene"][0]) == {"name", "objekte", "bytes", "breite_x", "breite_z", "hoehe", "warnung"}


def test_custom_string_ueber_grenze_wird_verworfen():
    lang = "x" * (bot._ADJ_CUSTOMSTRING_MAX + 1)
    objekte, fehler = bot._adj_pruefen(json.dumps({"Objects": [{"name": "A", "pos": [1, 2, 3], "customString": lang}]}))
    assert fehler is None and objekte[0]["customString"] == ""
    ok_, _ = bot._adj_pruefen(json.dumps({"Objects": [{"name": "A", "pos": [1, 2, 3], "customString": "x" * 1000}]}))
    assert ok_[0]["customString"] == "x" * 1000


def test_speichern_lehnt_ab_wenn_die_normalisierte_datei_ueber_dem_limit_liegt(monkeypatch):
    objekte = _objekte()
    monkeypatch.setattr(bot, "_ADJ_MAX_BYTES", 500)
    with pytest.raises(ValueError):
        bot._adj_speichern("premade", "", "gross", objekte, "t")
    assert bot._adj_liste("premade") == []


def test_parallele_platzierungen_behalten_beide_eintraege(env):
    _premade()
    bot._adj_zustand(env.a)

    async def beide():
        return await asyncio.gather(bot._adj_platzieren(env.a, "premade", "drop1", 5000, 300, 6000, 2, "befehl", "1"),
                                    bot._adj_platzieren(env.a, "premade", "drop1", 8000, 300, 7000, 2, "befehl", "2"))
    (i1, f1), (i2, f2) = asyncio.run(beide())
    assert f1 is None and f2 is None
    assert sorted(env.a.ftp.eintraege()) == sorted(["custom/alt.json", i1["datei"], i2["datei"]])
    assert _ids(env.a) == sorted([i1["id"], i2["id"]]) and len(env.a.ftp.adj_dateien()) == 2


def test_airdrop_nach_dem_serverstart_verliert_keinen_lauf(env):
    """Neustart erkannt (Server hat die Config schon gelesen), aber noch nicht verbucht: ein JETZT gesetzter
    Airdrop war in diesem Lauf nicht sichtbar und darf dort nicht mitgezählt werden."""
    _premade()
    z = bot._adj_zustand(env.a)
    bot._adj_poll(env.a, False)                      # nichts zu tun, kein Zustand mit Arbeit
    z["instanzen"].append({"id": "aaaaaa", "name": "x", "quelle": "premade", "datei": "custom/adj_aaaaaa.json", "x": 1, "y": 1,
                           "z": 1, "restarts": 1, "gesehen": 0, "von": "befehl", "ab_lauf": 1, "objekte": 1})
    bot._adj_poll(env.a, True)                       # Neustart 1 erkannt: laeufe=1, offen=1
    assert z["laeufe"] == 1 and z["neustarts_offen"] == 1
    inst = _platziere(env.a, restarts=1)             # danach gesetzt -> sichtbar erst ab Lauf 2
    assert inst["ab_lauf"] == 2
    assert run(bot._adj_einen_neustart(env.a)) is None
    ids = _ids(env.a)
    assert inst["id"] in ids and "aaaaaa" not in ids                 # der alte war sichtbar und läuft ab, der neue bleibt
    z["neustarts_offen"] += 1
    z["laeufe"] += 1
    assert run(bot._adj_einen_neustart(env.a)) is None               # Neustart 2: jetzt zählt der neue (restarts=1) und läuft ab
    assert inst["id"] not in _ids(env.a)


def test_poll_zaehlt_nur_mit_restart_flag_und_nur_einmal(env):
    _premade()
    _platziere(env.a, restarts=5)
    z = bot._adj_zustand(env.a)
    bot._ADJ_RETRY_AB[env.a.service_id] = time.time() + 1000             # keine Aufgabe starten, nur zählen
    for _ in range(3):
        bot._adj_poll(env.a, False)
    assert z["neustarts_offen"] == 0 and z["laeufe"] == 0
    bot._adj_poll(env.a, True)
    assert z["neustarts_offen"] == 1 and z["laeufe"] == 1


def test_poll_haengt_den_zaehler_nach_dem_cursor_ein():
    """Reihenfolge im Log-Poll: _adj_poll(…, True) erst NACH dem Speichern des Log-Cursors (sonst Doppelzählung)."""
    quelle = open(os.path.join(REPO, "bot.py"), encoding="utf-8").read()
    ort = quelle.index("async def _poll_connection")
    teil = quelle[ort:ort + 40000]
    erst = teil.index("_adj_poll(conn, False)")
    zweimal = [i for i in range(len(teil)) if teil.startswith("_adj_poll(conn, restart_detected)", i)]
    assert len(zweimal) == 2 and all(i > erst for i in zweimal)
    for i in zweimal:
        assert "connections.save()" in teil[max(0, i - 200):i]


def test_kaputter_zustand_stoppt_den_poll_nicht(env):
    env.a.data["airdrop_json"] = {"instanzen": [], "neustarts_offen": "x", "laeufe": [], "scheduler": {"zaehler": None, "anzahl": "?"}}
    bot._adj_poll(env.a, True)                       # darf nicht werfen
    z = bot._adj_zustand(env.a)
    assert z["neustarts_offen"] == 0 and z["laeufe"] == 0 and z["scheduler"]["anzahl"] == 1


def test_autocomplete_zeigt_ohne_recht_nichts(monkeypatch, env):
    _premade()
    inst = _platziere(env.a, restarts=3)
    inter = SimpleNamespace(guild_id=GID, namespace=SimpleNamespace(server=None), user=SimpleNamespace(id=1))
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda i, k: False)
    assert asyncio.run(bot._adj_name_autocomplete(inter, "")) == []
    assert asyncio.run(bot._adj_instanz_autocomplete(inter, "")) == []
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda i, k: True)
    assert [c.value for c in asyncio.run(bot._adj_instanz_autocomplete(inter, ""))] == [inst["id"]]
    assert [c.value for c in asyncio.run(bot._adj_name_autocomplete(inter, ""))] == ["premade:drop1"]


def test_scheduler_position_ausserhalb_der_karte_wird_abgelehnt(monkeypatch, env):
    _premade()
    wert = {"aktiv": True, "anzahl": 1, "alle_neustarts": 1, "airdrops": [{"quelle": "premade", "name": "drop1"}],
            "positionen": [{"x": 99999, "y": 200, "z": 5000}]}
    status, r = dash(monkeypatch, env.a, bot.api_tools_airdropjson_scheduler_post, wert)
    assert status == 400 and "außerhalb der Karte" in r["error"]
