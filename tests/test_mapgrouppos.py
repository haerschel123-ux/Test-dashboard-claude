"""Object to MapGroupPos: Registrierung als Tool und die Umwandlung in app.js.

Die Umwandlung laeuft rein clientseitig - geprueft wird sie hier ueber node gegen die Ausgaben der Referenzseite
(doordiehub.com/ObjectToMapGroupPos). tests/fixtures/mapgrouppos/referenz_seite.json sind echte Laeufe der Seite im Browser:
Eingabe, Format und das, was die Seite daraus gemacht hat (Ausgabe, Fehlermeldung oder - bei einem JavaScript-Fehler im
Browser - nichts). basis_auszug.* ist ein Ausschnitt einer echten Basis-Datei samt Ausgabe der Seite, Zeile fuer Zeile.

    python3 -m pytest tests/test_mapgrouppos.py -q
"""
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

APP_JS = os.path.join(REPO, "dashboard_web", "static", "app.js")
FIX = os.path.join(REPO, "tests", "fixtures", "mapgrouppos")

# Die Fehlermeldungen der Seite (englisch) und ihre deutsche Entsprechung im Dashboard.
FEHLER = {
    "No content to convert.": "Kein Inhalt zum Umwandeln.",
    "Failed to parse JSON input.": "Die JSON-Eingabe konnte nicht gelesen werden.",
    "No objects found in the input.": "In der Eingabe wurden keine Objekte gefunden.",
}


def _lies(name):
    with open(os.path.join(FIX, name), encoding="utf-8", newline="") as f:
        return f.read()


def _konvertiere(faelle):
    """faelle: [[text, modus], ...] -> Ergebnisse der JS-Funktion mapGroupPosKonvertieren."""
    if shutil.which("node") is None or not os.path.exists(APP_JS):
        pytest.skip("node oder dashboard_web/static/app.js nicht vorhanden")
    skript = (
        "const src=require('fs').readFileSync(process.argv[1],'utf8');"
        "const a=src.indexOf('var MGP_SKALIERUNG_TOLERANZ'),b=src.indexOf('function toolMapGroupPosModal');"
        "eval(src.slice(a,b));"
        "const f=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "console.log(JSON.stringify(f.map(([t,m])=>mapGroupPosKonvertieren(t,m))));"
    )
    aus = subprocess.run(["node", "-e", skript, APP_JS], input=json.dumps(faelle), capture_output=True, text=True, check=True)
    return json.loads(aus.stdout)


REFERENZ = json.loads(_lies("referenz_seite.json"))["faelle"]


def test_tool_ist_registriert():
    assert ("objecttomapgrouppos", "🏗️", "Object to MapGroupPos") in bot._TOOL_LISTE
    assert "tools.objecttomapgrouppos" in bot.FEATURE_MODULES


def test_tool_ist_im_frontend_verdrahtet():
    with open(APP_JS, encoding="utf-8") as f:
        src = f.read()
    assert "objecttomapgrouppos: toolMapGroupPosModal" in src


# ── Gleiche Ausgabe wie die Seite ────────────────────────────────────────
@pytest.mark.parametrize("fall", [f for f in REFERENZ if f["ergebnis"]["art"] == "ausgabe"], ids=lambda f: f["name"])
def test_ausgabe_ist_byte_gleich_zur_seite(fall):
    r = _konvertiere([[fall["eingabe"], fall["modus"]]])[0]
    erwartet = fall["ergebnis"]["seite"]
    if fall["name"] == "json_sonderzeichen_name":
        # Einzige gewollte Abweichung bei gueltiger Eingabe: Die Seite schreibt den Namen roh (kaputtes XML), wir escapen.
        erwartet = erwartet.replace('name="A"B<C&D>E"', 'name="A&quot;B&lt;C&amp;D&gt;E"')
    assert "fehler" not in r, r
    assert r["xml"] == erwartet


def test_basis_auszug_ist_zeile_fuer_zeile_wie_die_seite():
    r = _konvertiere([[_lies("basis_auszug.json"), "json"]])[0]
    assert r["xml"] == _lies("basis_auszug.xml")
    assert r["anzahl"] == len(json.loads(_lies("basis_auszug.json"))["Objects"])


def test_datei_endet_ohne_zeilenumbruch_wie_die_seite():
    r = _konvertiere([['SpawnObject("A", "1 2 3", "4 5 6");', "spawnobject"]])[0]
    assert r["xml"].endswith("</map>") and not r["xml"].endswith("\n")
    assert r["xml"].startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<map>\n    <group ')


def test_drehung_wird_wie_die_seite_umsortiert():
    """ypr = [yaw, pitch, roll] -> rpy = "pitch roll yaw", a = -yaw."""
    r = _konvertiere([['{"Objects":[{"name":"A","pos":[1,2,3],"ypr":[10,20,30]}]}', "json"]])[0]
    assert 'rpy="20.000000 30.000000 10.000000" a="-10.000000"' in r["xml"]


# ── Gleiche Fehlermeldungen wie die Seite ────────────────────────────────
@pytest.mark.parametrize("fall", [f for f in REFERENZ if f["ergebnis"]["art"] == "fehler"], ids=lambda f: f["name"])
def test_fehlermeldung_entspricht_der_seite(fall):
    r = _konvertiere([[fall["eingabe"], fall["modus"]]])[0]
    assert r.get("fehler") == FEHLER[fall["ergebnis"]["seite"]]
    assert "xml" not in r


@pytest.mark.parametrize("fall", [f for f in REFERENZ if f["ergebnis"]["art"] == "stumm"], ids=lambda f: f["name"])
def test_wo_die_seite_stumm_scheitert_kommt_hier_eine_klare_meldung(fall):
    r = _konvertiere([[fall["eingabe"], fall["modus"]]])[0]
    assert "xml" not in r and r["fehler"].startswith("Objekt Nr. 1: „")


def test_fehlermeldung_nennt_das_richtige_objekt_und_feld():
    ok = '{"name":"A","pos":[1,2,3],"ypr":[1,2,3]}'
    r = _konvertiere([['{"Objects":[%s,%s,{"name":"C","pos":["1",2,3],"ypr":[1,2,3]}]}' % (ok, ok), "json"],
                      ['{"Objects":[%s,{"name":"B","pos":[1,2,3],"ypr":[1,2]}]}' % ok, "json"]])
    assert r[0]["fehler"] == "Objekt Nr. 3: „pos“ darf nur Zahlen enthalten."
    assert r[1]["fehler"] == "Objekt Nr. 2: „ypr“ muss drei Zahlen enthalten."


# ── Was bewusst anders ist als auf der Seite ─────────────────────────────
def test_ausgabe_ist_immer_gueltiges_xml_auch_mit_sonderzeichen_im_namen():
    r = _konvertiere([['{"Objects":[{"name":"A\\"B<C&D>E","pos":[1,2,3],"ypr":[0,0,0]}]}', "json"]])[0]
    wurzel = ET.fromstring(r["xml"])
    assert [g.attrib["name"] for g in wurzel.findall("group")] == ['A"B<C&D>E']


def test_skalierung_wird_gemeldet_nicht_uebernommen():
    daten = {"Objects": [{"name": "A", "pos": [1, 2, 3], "ypr": [0, 0, 0], "scale": 1.5},
                         {"name": "B", "pos": [1, 2, 3], "ypr": [0, 0, 0], "scale": 0.9999285},   # praktisch 1
                         {"name": "C", "pos": [1, 2, 3], "ypr": [0, 0, 0], "scale": 0.5}]}
    r = _konvertiere([[json.dumps(daten), "json"]])[0]
    assert any(w.startswith("2 Objekt(e) haben eine Skalierung") for w in r["warnungen"])
    assert "scale" not in r["xml"].lower()


def test_skalierung_aus_dem_spawnobject_wird_gemeldet():
    r = _konvertiere([['SpawnObject("A", "1 2 3", "0 0 0", 2.5);\nSpawnObject("B", "1 2 3", "0 0 0", 1);', "spawnobject"]])[0]
    assert r["anzahl"] == 2 and len(r["warnungen"]) == 1 and r["warnungen"][0].startswith("1 Objekt(e)")


def test_basis_auszug_meldet_seine_skalierten_mauern():
    r = _konvertiere([[_lies("basis_auszug.json"), "json"]])[0]
    erwartet = sum(1 for o in json.loads(_lies("basis_auszug.json"))["Objects"] if abs(o["scale"] - 1) > 0.001)
    assert erwartet > 0
    assert r["warnungen"] == ["%d Objekt(e) haben eine Skalierung ungleich 1 – die mapgrouppos.xml kennt keine Skalierung, "
                              "sie wird nicht übernommen." % erwartet]


def test_ungueltige_zahlen_aus_spawnobject_werden_gemeldet_wie_die_seite_sie_als_nan_schreibt():
    r = _konvertiere([['SpawnObject("Land_F", "abc 2 3", "0 0 0", 1);', "spawnobject"]])[0]
    assert 'pos="NaN 2.000000 3.000000"' in r["xml"]                 # wie die Seite ...
    assert any("NaN" in w for w in r["warnungen"])                    # ... aber nicht mehr unbemerkt


def test_objekt_ohne_namen_wird_gemeldet():
    r = _konvertiere([['{"Objects":[{"pos":[1,2,3],"ypr":[1,2,3]}]}', "json"]])[0]
    assert 'name="undefined"' in r["xml"] and any("ohne Namen" in w for w in r["warnungen"])


def test_saubere_eingabe_hat_keine_warnungen():
    r = _konvertiere([['{"Objects":[{"name":"A","pos":[1,2,3],"ypr":[1,2,3],"scale":1}]}', "json"]])[0]
    assert r["warnungen"] == [] and r["anzahl"] == 1


def test_altes_ergebnis_kommt_nicht_zusammen_mit_einem_fehler():
    """Die Funktion liefert entweder xml oder fehler - nie beides (die Oberflaeche leert die Ausgabe bei einem Fehler)."""
    for r in _konvertiere([[f["eingabe"], f["modus"]] for f in REFERENZ]):
        assert ("xml" in r) != ("fehler" in r)


def test_nur_json_modus_liest_json_und_nur_spawnobject_modus_liest_aufrufe():
    json_text = '{"Objects":[{"name":"A","pos":[1,2,3],"ypr":[0,0,0]}]}'
    so_text = 'SpawnObject("A", "1 2 3", "0 0 0", 1);'
    r = _konvertiere([[json_text, "spawnobject"], [so_text, "json"], [json_text, "json"], [so_text, "spawnobject"]])
    assert r[0]["fehler"] == FEHLER["No objects found in the input."]
    assert r[1]["fehler"] == FEHLER["Failed to parse JSON input."]
    assert r[2]["xml"] == r[3]["xml"]


def test_grosse_datei_bleibt_zeilenweise_und_vollstaendig():
    objekte = [{"name": "Obj%d" % i, "pos": [i, i / 3, -i], "ypr": [i % 360, 0, 0], "scale": 1} for i in range(5000)]
    r = _konvertiere([[json.dumps({"Objects": objekte}), "json"]])[0]
    assert r["anzahl"] == 5000 and r["xml"].count("<group ") == 5000
    assert len(ET.fromstring(r["xml"]).findall("group")) == 5000
