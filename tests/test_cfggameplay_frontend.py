"""CFGGameplay Editor: die reinen Rechenfunktionen der Oberflaeche (app.js), mit node ausgefuehrt.

Geprueft werden Zahlen-Eingabe, Preset-Erkennung, der Schalter fuer die Start-Ausruestung, Listeneintraege und die Reihenfolge der
Kategorien - die Stellen, an denen die Webseite doordiehub.com/CFGGameplayEditor Fehler hatte (stilles 0, Duplikate, kaputte Presets).

    python3 -m pytest tests/test_cfggameplay_frontend.py -q
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

APP_JS = os.path.join(REPO, "dashboard_web", "static", "app.js")


def _js(ausdruck, daten=None):
    """Wertet einen JS-Ausdruck mit den Funktionen aus app.js aus (Block 'var CG_KATEGORIEN' bis 'async function toolCfgGameplayModal')."""
    if shutil.which("node") is None or not os.path.exists(APP_JS):
        pytest.skip("node oder dashboard_web/static/app.js nicht vorhanden")
    skript = (
        "const src=require('fs').readFileSync(process.argv[1],'utf8');"
        "const a=src.indexOf('var CG_KATEGORIEN'),b=src.indexOf('async function toolCfgGameplayModal');"
        "if(a<0||b<0)throw new Error('Block nicht gefunden');"
        "eval(src.slice(a,b));"
        "const D=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "console.log(JSON.stringify(" + ausdruck + "));"
    )
    aus = subprocess.run(["node", "-e", skript, APP_JS], input=json.dumps(daten), capture_output=True, text=True, check=True)
    return json.loads(aus.stdout)


@pytest.mark.parametrize("eingabe, erwartet", [
    ("7", 7), ("-5", -5), ("+5", 5), ("1,5", 1.5), (" 2.25 ", 2.25), (".5", 0.5), ("5.", 5), ("1e3", 1000), ("1E-2", 0.01), ("0", 0),
    ("-0.01", -0.01), ("007", 7), ("0.001", 0.001),
    ("", None), ("  ", None), ("abc", None), ("-", None), ("+", None), ("1.2.3", None), ("1,2,3", None), ("1e", None), ("e5", None),
    ("5 5", None), ("0x10", None), ("Infinity", None), ("NaN", None), ("1e999", None), ("--5", None), ("١٢", None),
])
def test_cgzahl_nimmt_nur_echte_zahlen(eingabe, erwartet):
    assert _js("cgZahl(D)", eingabe) == erwartet


def test_cgzahl_gibt_null_nicht_still_null_zurueck():
    """Die Webseite machte aus jeder Eingabe, die keine Zahl war, still 0 (parseFloat(...) || 0)."""
    assert _js("[cgZahl('abc'), cgZahl('0')]") == [None, 0]


@pytest.mark.parametrize("a, b, gleich", [
    (1, 1, True), (True, 1, False), (1, True, False), (None, None, True), ("a", "a", True), ("1", 1, False),
    ([1, 2], [1, 2], True), ([1, 2], [2, 1], False), ([1], [1, 2], False), ([True], [1], False),
    ({"a": 1, "b": 2}, {"b": 2, "a": 1}, True), ({"a": 1}, {"a": 1, "b": 2}, False), ({"a": 1}, {"b": 1}, False),
    ({"a": [1, {"b": None}]}, {"a": [1, {"b": None}]}, True), ({"a": []}, {"a": {}}, False), ([], {}, False), (None, {}, False),
])
def test_cggleich_ist_streng_im_typ(a, b, gleich):
    assert _js("cgGleich(D[0], D[1])", [a, b]) is gleich


def test_presets_werden_erkannt_und_ueberschreiben_nur_ihre_werte():
    for gruppe in bot._CG_PRESETS:
        for p in gruppe["eintraege"]:
            abschnitt = {"eigener": "bleibt", **{k: 999 for k in p["werte"]}}
            # nicht aktiv, solange die Werte abweichen
            assert _js("cgPresetAktiv(D[0], D[1])", [abschnitt, p["werte"]]) is False
            neu = _js("cgPresetAnwenden(D[0], D[1])", [abschnitt, p["werte"]])
            assert neu["eigener"] == "bleibt"                              # Zusatzschluessel (z. B. von Mods) bleiben
            assert {k: neu[k] for k in p["werte"]} == p["werte"]
            assert _js("cgPresetAktiv(D[0], D[1])", [neu, p["werte"]]) is True


def test_preset_ist_nach_einer_aenderung_nicht_mehr_aktiv():
    p = bot._CG_PRESETS[0]["eintraege"][0]["werte"]
    assert _js("cgPresetAktiv(D[0], D[1])", [dict(p), p]) is True
    geaendert = dict(p, staminaMax=p["staminaMax"] + 1)
    assert _js("cgPresetAktiv(D[0], D[1])", [geaendert, p]) is False
    assert _js("cgPresetAktiv(D[0], D[1])", [None, p]) is False
    assert _js("cgPresetAktiv(D[0], D[1])", [{}, p]) is False
    # true ist nicht 1
    assert _js("cgPresetAktiv(D[0], D[1])", [{"allowRefillSpeedModifier": 1}, {"allowRefillSpeedModifier": True}]) is False


def test_preset_anwenden_legt_einen_fehlenden_abschnitt_an_und_teilt_keine_listen():
    assert _js("cgPresetAnwenden(undefined, D)", {"a": 1}) == {"a": 1}
    assert _js("(function(){var w={l:[1,2]};var o=cgPresetAnwenden({}, w);o.l.push(3);return w.l;})()") == [1, 2]


def test_spawngear_schalter_an_setzt_den_schluessel_hinter_disablepersonallight():
    spieler = {"disablePersonalLight": False, "StaminaData": {"staminaMax": 100}, "Anderes": 1}
    neu = _js("cgSpawnGear(D, true)", spieler)
    assert list(neu) == ["disablePersonalLight", "spawnGearPresetFiles", "StaminaData", "Anderes"] and neu["spawnGearPresetFiles"] == []
    # ohne disablePersonalLight: am Ende
    assert list(_js("cgSpawnGear(D, true)", {"StaminaData": {}})) == ["StaminaData", "spawnGearPresetFiles"]
    # schon da: nichts aendern (auch nicht die Liste leeren)
    vorhanden = {"disablePersonalLight": True, "spawnGearPresetFiles": ["custom/a.json"]}
    assert _js("cgSpawnGear(D, true)", vorhanden) == vorhanden
    # aus: Schluessel weg, Rest bleibt in der Reihenfolge
    assert _js("cgSpawnGear(D, false)", neu) == spieler and list(_js("cgSpawnGear(D, false)", neu)) == list(spieler)
    assert _js("cgSpawnGear(D, false)", spieler) == spieler


def test_spawngear_schalter_veraendert_das_original_nicht():
    assert _js("(function(){var s={disablePersonalLight:false};cgSpawnGear(s,true);return s;})()") == {"disablePersonalLight": False}


@pytest.mark.parametrize("text, liste, praefix, erwartet", [
    ("custom/a.json", [], "custom/", {"wert": "custom/a.json"}),
    ("  custom/a.json  ", [], "custom/", {"wert": "custom/a.json"}),
    ("custom/", [], "custom/", {"fehler": "leer"}),
    ("", [], "custom/", {"fehler": "leer"}),
    ("   ", [], "", {"fehler": "leer"}),
    (None, [], "", {"fehler": "leer"}),
    ("custom/a.json", ["custom/a.json"], "custom/", {"fehler": "doppelt"}),
    ("custom/a.json ", ["custom/a.json"], "custom/", {"fehler": "doppelt"}),
    ("FenceKit", ["TerritoryFlagKit"], "", {"wert": "FenceKit"}),
    ("custom/", [], "", {"wert": "custom/"}),                       # ohne Praefix ist "custom/" ein gewoehnlicher Eintrag
    ("Custom/a.json", ["custom/a.json"], "custom/", {"wert": "Custom/a.json"}),
])
def test_listeneintrag(text, liste, praefix, erwartet):
    assert _js("cgListeEintrag(D[0], D[1], D[2])", [text, liste, praefix]) == erwartet


def test_kategorien_bekannte_zuerst_in_fester_reihenfolge_dann_weitere_abschnitte():
    modell = {"version": 1, "VehicleData": {}, "ModData": {"x": 1}, "GeneralData": {}, "Skalar": 5, "Liste": [1], "PlayerData": {},
              "Leer": None, "NochEiner": {}}
    assert _js("cgKategorien(D)", modell) == ["GeneralData", "PlayerData", "VehicleData", "ModData", "NochEiner"]
    assert _js("cgKategorien(D)", {}) == []
    assert _js("cgKategorien(D)", {"version": 1, "PlayerData": []}) == []


def test_alle_bekannten_kategorien_der_standarddateien_haben_einen_namen():
    namen = _js("Object.keys(CG_KATEGORIEN)")
    for karte in ("ChernarusPlus", "Livonia", "Sakhal", "Nasdara"):
        for k, v in bot._cg_standard(karte)[0].items():
            if isinstance(v, dict):
                assert k in namen, k
    kat = _js("CG_KATEGORIEN")
    assert all(len(v) == 3 and all(v) for v in kat.values())


def test_monate_und_naessestufen_passen_zu_den_laengen_der_zahlenlisten():
    assert len(_js("CG_MONATE")) == 12 and len(_js("CG_NAESSE")) == 5
    assert set(bot._CG_ZAHLENLISTEN.values()) == {12, 5}
    assert all(len(x) == 2 and all(x) for x in _js("CG_MONATE") + _js("CG_NAESSE"))
