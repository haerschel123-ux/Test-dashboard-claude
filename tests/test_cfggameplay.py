"""CFGGameplay Editor: Standarddateien, Pruefregeln, Presets und chirurgisches Schreiben der cfggameplay.json.

Die Referenz unter tests/fixtures/cfggameplay/referenz_seite.json sind echte Messwerte der Seite
doordiehub.com/CFGGameplayEditor (Standarddatei je Karte, Regeln, Hilfetexte, Presets), im Browser ausgelesen.
Abweichungen von der Seite sind bewusst und stehen hier in den Tests (lightingConfig bis 3, zwei Hilfetexte, Zahlenlisten fest).

    python3 -m pytest tests/test_cfggameplay.py -q
"""
import asyncio
import copy
import json
import os
import random
import sys
import time

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

with open(os.path.join(REPO, "tests", "fixtures", "cfggameplay", "referenz_seite.json"), encoding="utf-8") as _f:
    REF = json.load(_f)

KARTEN = {"Chernarus": "ChernarusPlus", "Livonia": "Livonia", "Sakhal": "Sakhal", "Nasdara": "Nasdara"}


def _stile(modell):
    """Dieselbe Datei in mehreren Schreibweisen: Einrueckung 2/4/Tab, einzeilig, mit Windows-Zeilenenden."""
    zwei = json.dumps(modell, indent=2, ensure_ascii=False)
    return {"zwei": zwei, "vier": json.dumps(modell, indent=4, ensure_ascii=False),
            "tab": json.dumps(modell, indent="\t", ensure_ascii=False),
            "kompakt": json.dumps(modell, separators=(",", ":"), ensure_ascii=False),
            "crlf": zwei.replace("\n", "\r\n")}


STILE = list(_stile({}).keys())


def _gleich(a, b):
    return bot._cg_gleich(a, b)


# ── Registrierung ──────────────────────────────────────────────────────────
def test_tool_ist_registriert():
    assert ("cfggameplay", "⚙️", "CFGGameplay Editor") in bot._TOOL_LISTE
    assert "tools.cfggameplay" in bot.FEATURE_MODULES
    with open(os.path.join(REPO, "bot.py"), encoding="utf-8") as f:
        src = f.read()
    assert 'r.add_get("/api/tools/cfggameplay", api_tools_cfggameplay_get)' in src
    assert 'r.add_post("/api/tools/cfggameplay", api_tools_cfggameplay_post)' in src


def test_tool_ist_im_frontend_verdrahtet():
    with open(os.path.join(REPO, "dashboard_web", "static", "app.js"), encoding="utf-8") as f:
        assert "cfggameplay: toolCfgGameplayModal" in f.read()


# ── Standarddateien: Zeichen fuer Zeichen wie auf der Seite ───────────────────
@pytest.mark.parametrize("name, karte", list(KARTEN.items()))
def test_standarddatei_entspricht_der_seite_auch_in_der_reihenfolge(name, karte):
    std, bekannt = bot._cg_standard(karte)
    assert bekannt is True
    assert json.dumps(std) == json.dumps(REF["standardwerte"][name])


def test_unbekannte_karte_bekommt_die_chernarus_werte_und_wird_gemeldet():
    for karte in (None, "Deerisle", ""):
        std, bekannt = bot._cg_standard(karte)
        assert bekannt is False and std == REF["standardwerte"]["Chernarus"]


def test_standarddatei_ist_eine_kopie():
    a, _ = bot._cg_standard("Sakhal")
    a["PlayerData"]["StaminaData"]["staminaMax"] = 1
    a["WorldsData"]["playerRestrictedAreaFiles"].append("x")
    b, _ = bot._cg_standard("Sakhal")
    assert b["PlayerData"]["StaminaData"]["staminaMax"] == 100 and b["WorldsData"]["playerRestrictedAreaFiles"] == ["pra/warheadstorage.json"]
    assert bot._CG_BASIS["PlayerData"]["StaminaData"]["staminaMax"] == 100


@pytest.mark.parametrize("name, karte", list(KARTEN.items()))
def test_standarddatei_hat_weder_fehler_noch_warnungen(name, karte):
    # auf der Seite wird der Nasdara-Standard (lightingConfig 3) von der eigenen Regel rot markiert - hier nicht
    assert bot._cg_pruefen(bot._cg_standard(karte)[0]) == ([], [])


# ── Regeln, Hilfetexte, Presets ────────────────────────────────────────────
def test_regeln_wie_auf_der_seite_bis_auf_drei_bewusste_abweichungen():
    """Weggelassen: der Zahlenbereich fuer den Schalter disableBaseDamage. Geaendert: lightingConfig bis 3 (die Seite bietet
    selbst 3 an und markiert es rot) und staminaKgToStaminaPercentPenalty ab 0 (ihr Preset 'Unlimited Stamina' setzt 0.01)."""
    erwartet = {p: (r["min"], r["max"]) for p, r in REF["regeln"].items() if p != "GeneralData.disableBaseDamage"}
    erwartet["WorldsData.lightingConfig"] = (0, 3)
    erwartet["PlayerData.StaminaData.staminaKgToStaminaPercentPenalty"] = (0, 10)
    assert bot._CG_REGELN == erwartet
    assert len(REF["regeln"]) == 26 and len(bot._CG_REGELN) == 25


def test_jeder_hilfetext_der_seite_ist_da_deutsch_dabei_und_englisch_unveraendert():
    geaendert = {"lightingConfig", "hitDirectionOverrideEnabled"}
    for k, en in REF["tooltips"].items():
        de, en_neu = bot._CG_HILFE[k]
        assert de.strip() and de != en and not de.isascii() or de != en       # eine echte deutsche Fassung
        if k not in geaendert:
            assert en_neu == en, k
    assert "(default)" not in bot._CG_HILFE["lightingConfig"][1]
    assert set(bot._CG_HILFE) - set(REF["tooltips"]) == {"bunkerBroadcastEnabled"}


def test_jedes_feld_der_standarddateien_hat_einen_hilfetext_ausser_den_neuen_abschnitten():
    ohne = {"version", "ExternalLockData", "SandstormData"}

    def felder(d, vor=""):
        for k, v in d.items():
            if isinstance(v, dict):
                yield from felder(v, vor + k + ".")
            else:
                yield vor, k
    for karte in KARTEN.values():
        for vor, k in felder(bot._cg_standard(karte)[0]):
            if vor.split(".")[0] in ohne or k in ohne:
                continue
            assert k in bot._CG_HILFE, vor + k


def test_presets_wie_auf_der_seite():
    assert [g["pfad"] for g in bot._CG_PRESETS] == [g["path"] for g in REF["presets"]]
    for mein, ref in zip(bot._CG_PRESETS, REF["presets"]):
        assert [p["id"] for p in mein["eintraege"]] == [p["id"] for p in ref["presets"]]
        for a, b in zip(mein["eintraege"], ref["presets"]):
            assert (a["en"], a["text_en"], a["werte"]) == (b["label"], b["description"], b["values"])
            assert a["de"] and a["text_de"]
        # jedes Preset ist ein gueltiger Abschnitt
        for a in mein["eintraege"]:
            assert bot._cg_pruefen({"PlayerData": {mein["pfad"]: a["werte"]}}) == ([], [])


def test_beleuchtung_wie_auf_der_seite():
    assert [(b["wert"], b["en"]) for b in bot._CG_BELEUCHTUNG] == [
        (b["value"], b["label"]) for b in REF["beleuchtung"]] or [b["wert"] for b in bot._CG_BELEUCHTUNG] == [0, 1, 2, 3]
    assert all(b["de"] and b["text_de"] and b["text_en"] for b in bot._CG_BELEUCHTUNG)


# ── Pruefung ───────────────────────────────────────────────────────────────
def _m(**pfade):
    """Standarddatei Chernarus mit geaenderten Stellen ('A__B' -> A.B)."""
    d = copy.deepcopy(REF["standardwerte"]["Chernarus"])
    for p, v in pfade.items():
        ziel = d
        teile = p.split("__")
        for t in teile[:-1]:
            ziel = ziel[t]
        ziel[teile[-1]] = v
    return d


@pytest.mark.parametrize("modell, stichwort", [
    ([], "JSON-Objekt"), ("x", "JSON-Objekt"), (None, "JSON-Objekt"),
    (_m(GeneralData__disableBaseDamage=1), "true oder false"),
    (_m(GeneralData__disableBaseDamage="true"), "true oder false"),
    (_m(PlayerData__StaminaData__staminaMax="100"), "Zahl"),
    (_m(PlayerData__StaminaData__staminaMax=True), "Zahl"),
    (_m(PlayerData__StaminaData__staminaMax=None), "Zahl"),
    (_m(PlayerData__StaminaData__staminaMax=float("nan")), "Zahl"),
    (_m(PlayerData__StaminaData__staminaMax=float("inf")), "Zahl"),
    (_m(PlayerData__StaminaData="x"), "Abschnitt"),
    (_m(WorldsData=[]), "Abschnitt"),
    (_m(UIData__HitIndicationData__hitDirectionIndicatorColorStr=5), "Text"),
    (_m(WorldsData__environmentMinTemps=[1, 2, 3]), "genau 12"),
    (_m(WorldsData__environmentMinTemps=[1] * 11 + ["2"]), "genau 12"),
    (_m(WorldsData__environmentMaxTemps=[1] * 11 + [True]), "genau 12"),
    (_m(WorldsData__wetnessWeightModifiers=[1, 1, 1, 1]), "genau 5"),
    (_m(WorldsData__wetnessWeightModifiers="x"), "Liste"),
    (_m(WorldsData__objectSpawnersArr=["custom/a.json", ""]), "nicht-leere Texte"),
    (_m(WorldsData__objectSpawnersArr=["  "]), "nicht-leere Texte"),
    (_m(WorldsData__objectSpawnersArr=[3]), "nicht-leere Texte"),
    (_m(PlayerData__spawnGearPresetFiles=[None]), "nicht-leere Texte"),
    (_m(BaseBuildingData__HologramData__disallowedTypesInUnderground=["A", 1]), "nicht-leere Texte"),
    (_m(version="123"), "ganze Zahl"), (_m(version=123.5), "ganze Zahl"), (_m(version=True), "ganze Zahl"),
])
def test_pruefen_lehnt_falsche_typen_ab(modell, stichwort):
    fehler, _warn = bot._cg_pruefen(modell)
    assert fehler and any(stichwort in f for f in fehler), fehler


def test_pruefen_meldet_zahlen_ausserhalb_des_bereichs_nur_als_warnung():
    fehler, warn = bot._cg_pruefen(_m(PlayerData__StaminaData__staminaMax=5000, VehicleData__boatDecayMultiplier=-1))
    assert fehler == []
    assert any(w.startswith("PlayerData.StaminaData.staminaMax: 5000 liegt außerhalb") and "50–200" in w for w in warn)
    assert any(w.startswith("VehicleData.boatDecayMultiplier: -1") for w in warn)
    # Grenzen gehoeren dazu
    assert bot._cg_pruefen(_m(PlayerData__StaminaData__staminaMax=50)) == ([], [])
    assert bot._cg_pruefen(_m(PlayerData__StaminaData__staminaMax=200)) == ([], [])
    assert bot._cg_pruefen(_m(PlayerData__StaminaData__staminaMax=200.01))[1]
    assert bot._cg_pruefen(_m(PlayerData__StaminaData__staminaMax=49.99))[1]


def test_pruefen_lighting_config_3_ist_erlaubt_4_ist_eine_warnung():
    assert bot._cg_pruefen(_m(WorldsData__lightingConfig=3)) == ([], [])
    assert bot._cg_pruefen(_m(WorldsData__lightingConfig=4))[1]
    assert bot._cg_pruefen(_m(WorldsData__lightingConfig=-1))[1]


def test_pruefen_doppelte_listeneintraege_sind_nur_eine_warnung():
    fehler, warn = bot._cg_pruefen(_m(WorldsData__objectSpawnersArr=["custom/a.json", "custom/a.json"]))
    assert fehler == [] and warn == ["WorldsData.objectSpawnersArr: Enthält doppelte Einträge."]


def test_pruefen_laesst_unbekannte_schluessel_in_ruhe():
    m = _m()
    m["ModData"] = {"x": [1, "a", None, {"y": True}], "z": "text"}
    m["PlayerData"]["EigenerWert"] = "abc"
    m["PlayerData"]["StaminaData"]["extra"] = [1, 2]
    assert bot._cg_pruefen(m) == ([], [])


def test_pruefen_fehlende_bekannte_felder_sind_erlaubt():
    assert bot._cg_pruefen({}) == ([], [])
    assert bot._cg_pruefen({"PlayerData": {"StaminaData": {"staminaMax": 90}}}) == ([], [])


# ── Aenderungsliste ────────────────────────────────────────────────────────
def test_aenderungen_nennt_pfad_vorher_nachher():
    alt = _m()
    neu = _m(GeneralData__disableBaseDamage=True, PlayerData__StaminaData__staminaMax=90)
    neu["PlayerData"]["spawnGearPresetFiles"] = ["custom/a.json"]
    del neu["VehicleData"]["boatDecayMultiplier"]
    neu["WorldsData"]["environmentMinTemps"][0] = 5
    a = {x["pfad"]: x for x in bot._cg_aenderungen(alt, neu)}
    assert a["GeneralData.disableBaseDamage"] == {"pfad": "GeneralData.disableBaseDamage", "vorher": False, "nachher": True, "art": "geaendert"}
    assert a["PlayerData.StaminaData.staminaMax"]["nachher"] == 90
    assert a["PlayerData.spawnGearPresetFiles"]["art"] == "neu"
    assert a["VehicleData.boatDecayMultiplier"]["art"] == "entfernt"
    assert a["WorldsData.environmentMinTemps"]["nachher"][0] == 5          # Listen zaehlen als eine Aenderung
    assert len(a) == 5


def test_aenderungen_true_ist_nicht_gleich_1_aber_1_ist_gleich_1_0():
    assert bot._cg_aenderungen({"a": True}, {"a": 1})
    assert bot._cg_aenderungen({"a": 1}, {"a": True})
    assert bot._cg_aenderungen({"a": 1}, {"a": 1.0}) == []
    assert bot._cg_aenderungen({"a": [1, True]}, {"a": [1, 1]})
    assert bot._cg_aenderungen({"a": {"b": 1}}, {"a": [1]})


# ── Chirurgisches Schreiben ────────────────────────────────────────────────
@pytest.mark.parametrize("stil", STILE)
def test_unveraenderte_eingabe_laesst_die_datei_zeichen_fuer_zeichen_stehen(stil):
    m = _m()
    text = _stile(m)[stil]
    assert bot._cg_anwenden(text, m)[0] == text


def test_datei_im_stil_der_bohemia_vorlage_bleibt_unveraendert():
    """4 Leerzeichen, Zahlen mit Nachkommastelle (1.0), mehrzeilige Liste - wie die offizielle Datei."""
    text = ('{\n    "version": 123,\n    "GeneralData": {\n        "disableBaseDamage": false,\n'
            '        "disableContainerDamage": false\n    },\n    "PlayerData": {\n        "StaminaData": {\n'
            '            "staminaMax": 100.0,\n            "sprintStaminaModifierErc": 1.0\n        }\n    },\n'
            '    "BaseBuildingData": {\n        "HologramData": {\n            "disallowedTypesInUnderground": [\n'
            '                "FenceKit",\n                "TerritoryFlagKit"\n            ]\n        }\n    }\n}\n')
    m = json.loads(text)
    assert bot._cg_anwenden(text, m)[0] == text
    m["PlayerData"]["StaminaData"]["staminaMax"] = 90
    neu = bot._cg_anwenden(text, m)[0]
    assert neu == text.replace('"staminaMax": 100.0', '"staminaMax": 90')


@pytest.mark.parametrize("stil", STILE)
def test_ein_geaenderter_wert_aendert_nur_diesen_wert(stil):
    m = _m()
    text = _stile(m)[stil]
    m["PlayerData"]["StaminaData"]["staminaMax"] = 150
    m["GeneralData"]["disableBaseDamage"] = True
    m["UIData"]["HitIndicationData"]["hitDirectionIndicatorColorStr"] = "0xff00ff00"
    neu = bot._cg_anwenden(text, m)[0]
    erwartet = (text.replace('"staminaMax": 100', '"staminaMax": 150').replace('"staminaMax":100', '"staminaMax":150')
                .replace('"disableBaseDamage": false', '"disableBaseDamage": true').replace('"disableBaseDamage":false', '"disableBaseDamage":true')
                .replace("0xffbb0a1e", "0xff00ff00"))
    assert neu == erwartet and _gleich(json.loads(neu), m)


def test_zahlenschreibweise_der_unberuehrten_werte_bleibt():
    text = '{\n  "PlayerData": {\n    "StaminaData": {\n      "staminaMax": 1e2,\n      "staminaMinCap": 5.0,\n      "meleeStaminaModifier": 1\n    }\n  }\n}'
    m = json.loads(text)
    m["PlayerData"]["StaminaData"]["meleeStaminaModifier"] = 2
    neu = bot._cg_anwenden(text, m)[0]
    assert neu == text.replace('"meleeStaminaModifier": 1', '"meleeStaminaModifier": 2') and "1e2" in neu and "5.0" in neu


@pytest.mark.parametrize("stil", STILE)
@pytest.mark.parametrize("weg", [["version"], ["GeneralData"], ["VehicleData"], ["MapData", "VehicleData"],
                                 ["PlayerData", "UIData"], ["version", "GeneralData", "PlayerData"]])
def test_schluessel_loeschen_an_jeder_stelle(stil, weg):
    m = _m()
    text = _stile(m)[stil]
    for k in weg:
        del m[k]
    neu = bot._cg_anwenden(text, m)[0]
    assert _gleich(json.loads(neu), m)
    assert bot._cg_anwenden(neu, m)[0] == neu                       # nochmal anwenden: nichts mehr zu tun


@pytest.mark.parametrize("stil, einzug", [("zwei", 2), ("vier", 4), ("tab", "\t")])
@pytest.mark.parametrize("weg", [("version",), ("GeneralData", "disableBaseDamage"), ("PlayerData", "StaminaData", "staminaMax"),
                                 ("MapData", "ignoreMapOwnership"), ("MapData", "displayNavInfo"), ("VehicleData",),
                                 ("WorldsData", "environmentMinTemps"), ("WorldsData", "lightingConfig")])
def test_geloeschte_schluessel_hinterlassen_keine_luecken_und_keine_komma_reste(stil, einzug, weg):
    """Die Datei wurde mit json.dumps geschrieben - nach dem Loeschen muss sie genau so aussehen wie die frisch geschriebene."""
    m = _m()
    text = _stile(m)[stil]
    ziel = m
    for k in weg[:-1]:
        ziel = ziel[k]
    del ziel[weg[-1]]
    assert bot._cg_anwenden(text, m)[0] == json.dumps(m, indent=einzug, ensure_ascii=False)


@pytest.mark.parametrize("stil, einzug", [("zwei", 2), ("vier", 4), ("tab", "\t")])
def test_mehrere_nebeneinanderliegende_schluessel_loeschen(stil, einzug):
    m = _m()
    text = _stile(m)[stil]
    for k in ("disableRespawnDialog", "disableContainerDamage", "disableRespawnInUnconsciousness"):
        del m["GeneralData"][k]
    assert bot._cg_anwenden(text, m)[0] == json.dumps(m, indent=einzug, ensure_ascii=False)
    for k in ("UIData", "MapData", "VehicleData"):
        del m[k]
    assert bot._cg_anwenden(text, m)[0] == json.dumps(m, indent=einzug, ensure_ascii=False)


@pytest.mark.parametrize("stil", STILE)
def test_alle_schluessel_eines_abschnitts_loeschen_laesst_ein_leeres_objekt(stil):
    m = _m()
    text = _stile(m)[stil]
    m["GeneralData"] = {}
    neu = bot._cg_anwenden(text, m)[0]
    assert json.loads(neu)["GeneralData"] == {} and _gleich(json.loads(neu), m)


def test_loeschen_laesst_die_zeilen_der_uebrigen_schluessel_stehen():
    m = _m()
    text = _stile(m)["vier"]
    m["PlayerData"]["disablePersonalLight"] = False
    m2 = copy.deepcopy(m)
    del m2["MapData"]["ignoreMapOwnership"]
    del m2["MapData"]["displayNavInfo"]                            # letzter Schluessel: das Komma davor faellt weg
    neu = bot._cg_anwenden(text, m2)[0]
    zeilen_alt, zeilen_neu = text.splitlines(), neu.splitlines()
    assert len(zeilen_alt) - len(zeilen_neu) == 2
    gestrichen = [z for z in zeilen_alt if z not in zeilen_neu]
    assert any("ignoreMapOwnership" in z for z in gestrichen) and any("displayNavInfo" in z for z in gestrichen)
    assert _gleich(json.loads(neu), m2)


@pytest.mark.parametrize("stil", STILE)
def test_neuer_schluessel_wird_angehaengt(stil):
    m = _m()
    text = _stile(m)[stil]
    m["PlayerData"]["spawnGearPresetFiles"] = ["custom/loadout1.json"]
    m["EigenerAbschnitt"] = {"a": 1, "b": [1, 2], "c": {"d": "x"}}
    neu = bot._cg_anwenden(text, m)[0]
    assert _gleich(json.loads(neu), m)
    assert list(json.loads(neu)["PlayerData"])[-1] == "spawnGearPresetFiles"
    assert bot._cg_anwenden(neu, m)[0] == neu


def test_spawngear_schalter_an_und_wieder_aus_stellt_die_datei_wieder_her():
    m = _m()
    text = _stile(m)["zwei"]
    an = copy.deepcopy(m)
    an["PlayerData"]["spawnGearPresetFiles"] = []
    t_an = bot._cg_anwenden(text, an)[0]
    assert '"spawnGearPresetFiles": []' in t_an
    assert bot._cg_anwenden(t_an, m)[0] == text


@pytest.mark.parametrize("stil", STILE)
def test_liste_aendern_bewahrt_ihr_layout(stil):
    m = _m()
    text = _stile(m)[stil]
    m["BaseBuildingData"]["HologramData"]["disallowedTypesInUnderground"] = ["FenceKit", "WatchtowerKit", "Neu1", "Neu2"]
    m["WorldsData"]["objectSpawnersArr"] = ["custom/a.json"]
    neu = bot._cg_anwenden(text, m)[0]
    assert _gleich(json.loads(neu), m)
    if stil in ("zwei", "vier", "tab", "crlf"):
        zeile = [z for z in neu.splitlines() if "WatchtowerKit" in z]
        assert zeile and zeile[0].strip().rstrip(",") == '"WatchtowerKit"'      # jeder Eintrag steht weiter auf einer eigenen Zeile
    assert bot._cg_anwenden(neu, m)[0] == neu


def test_zahlenliste_aendert_nur_den_geaenderten_monat():
    m = _m()
    text = _stile(m)["zwei"]
    m["WorldsData"]["environmentMinTemps"][5] = 15.5
    neu = bot._cg_anwenden(text, m)[0]
    zeilen_alt, zeilen_neu = text.splitlines(), neu.splitlines()
    assert len(zeilen_alt) == len(zeilen_neu)
    assert [z for z in zeilen_neu if z not in zeilen_alt] == ["      15.5,"]
    assert _gleich(json.loads(neu), m)


def test_abschnitt_wird_zum_wert_und_wert_zum_abschnitt():
    m = _m()
    text = _stile(m)["zwei"]
    m["MapData"] = "weg"
    m["GeneralData"] = {"neu": {"tief": [1, {"x": None}]}}
    neu = bot._cg_anwenden(text, m)[0]
    assert _gleich(json.loads(neu), m)


def test_crlf_bleibt_erhalten_und_unbekanntes_bleibt():
    m = _m()
    m["ModData"] = {"ding": "ä ö ü \"quote\"", "n": 1.50}
    text = _stile(m)["crlf"]
    m["GeneralData"]["disableContainerDamage"] = True
    m["PlayerData"]["spawnGearPresetFiles"] = ["custom/x.json"]
    neu = bot._cg_anwenden(text, m)[0]
    assert "\n" not in neu.replace("\r\n", "") and "ä ö ü" in neu and "1.5" in neu
    assert _gleich(json.loads(neu), m)


def test_ohne_datei_wird_neu_geschrieben_wie_auf_der_webseite():
    m = bot._cg_standard("Nasdara")[0]
    text, alt = bot._cg_anwenden(None, m)
    assert text == json.dumps(m, indent=2) and not text.endswith("\n") and alt == {}


@pytest.mark.parametrize("text, stichwort", [
    ('{"a": 1, "a": 2}', "doppelt"), ('{"a": {"b": 1, "b": 2}}', "doppelt"), ("[1,2]", "Objekt"), ("[]", "Objekt"),
    ("{ kaputt", "Schlüssel"), ("", "leer"), ('{"a": 1,}', "Schlüssel"),
])
def test_dateien_die_nicht_automatisch_geaendert_werden_koennen(text, stichwort):
    with pytest.raises(ValueError, match=stichwort):
        bot._cg_anwenden(text, {"a": 1})


# ── Zufallstest: jede Aenderung ergibt gueltiges JSON mit genau dem gewuenschten Inhalt ───────
def _alle_objekte(d, vor=()):
    yield vor, d
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _alle_objekte(v, vor + (k,))


def _mutiere(rng, modell):
    m = copy.deepcopy(modell)
    for _ in range(rng.randint(1, 8)):
        objekte = list(_alle_objekte(m))
        pfad, obj = rng.choice(objekte)
        if not obj and rng.random() < 0.5:
            continue
        aktion = rng.choice(["bool", "zahl", "text", "liste_plus", "liste_minus", "loeschen", "neu", "neu_objekt", "typ", "alles_weg"])
        schluessel = list(obj)
        if aktion == "neu":
            obj["Neu%d" % rng.randint(0, 999)] = rng.choice([True, 3, 2.5, "x\"y", [], [1, 2], ["a"], None])
        elif aktion == "neu_objekt":
            obj["Obj%d" % rng.randint(0, 999)] = {"a": 1, "b": {"c": [True]}}
        elif aktion == "alles_weg" and pfad:
            obj.clear()
        elif schluessel:
            k = rng.choice(schluessel)
            v = obj[k]
            if aktion == "loeschen":
                del obj[k]
            elif aktion == "bool" and isinstance(v, bool):
                obj[k] = not v
            elif aktion == "zahl" and isinstance(v, (int, float)) and not isinstance(v, bool):
                obj[k] = rng.choice([0, 1, -3, 7.25, 1000, 0.001])
            elif aktion == "text" and isinstance(v, str):
                obj[k] = rng.choice(["", "äö", "a\"b", "0xff00ff00"])
            elif aktion == "liste_plus" and isinstance(v, list):
                v.insert(rng.randint(0, len(v)), rng.choice(["neu", 5, 1.5, True]))
            elif aktion == "liste_minus" and isinstance(v, list) and v:
                del v[rng.randrange(len(v))]
            elif aktion == "typ":
                obj[k] = rng.choice([[1, 2], {"x": 1}, "text", 12, False, None])
    return m


@pytest.mark.parametrize("stil", STILE)
def test_zufallsaenderungen_ergeben_immer_gueltiges_json_mit_dem_gewuenschten_inhalt(stil):
    rng = random.Random(20261030)
    basis = {k: _m() for k in ("Chernarus",)}
    basis["Nasdara"] = bot._cg_standard("Nasdara")[0]
    basis["Sakhal"] = bot._cg_standard("Sakhal")[0]
    for i in range(150):
        quelle = basis[rng.choice(list(basis))]
        text = _stile(quelle)[stil]
        ziel = _mutiere(rng, quelle)
        neu = bot._cg_anwenden(text, ziel)[0]
        assert _gleich(json.loads(neu), ziel), (stil, i)
        assert bot._cg_anwenden(neu, ziel)[0] == neu, (stil, i)          # zweites Anwenden aendert nichts mehr


# ── Endpunkte ──────────────────────────────────────────────────────────────
class _FTP:
    def __init__(self, dateien):
        self.files = dict(dateien)
        self.writes = []
        self.fehlschlag = set()

    def list_dir(self, d):
        d = d.rstrip("/") + "/"
        return sorted(p for p in self.files if p.startswith(d))

    def read_file_ex(self, p):
        return (self.files[p], "ok") if p in self.files else (None, "missing")

    def write_file(self, p, inhalt):
        if p in self.fehlschlag:
            return False
        self.writes.append(p)
        self.files[p] = inhalt
        return True


def _conn(monkeypatch, tmp_path, karte="dayzOffline.sakhal", mission="/mission", dateien=None):
    monkeypatch.chdir(tmp_path)
    eintrag = {"service_id": "1000", "nitrado_token": "fake", "owner_discord_id": "1", "map_name": karte}
    if mission:
        eintrag["ftp_mission_dir"] = mission
    (tmp_path / "connections.json").write_text(json.dumps({"1000": eintrag}))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    monkeypatch.setattr(bot, "_audit_log", bot.deque(maxlen=50))
    c = reg._conns["1000"]
    c.ftp = _FTP(dateien if dateien is not None else {})
    return c


@pytest.fixture
def conn(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path)
    c.ftp.files["/mission/cfggameplay.json"] = _stile(bot._cg_standard("Sakhal")[0])["vier"]
    return c


def _aufruf(monkeypatch, c, handler, methode="GET", daten=None):
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": True, "service_id": c.service_id,
                            "seen": time.time()}
    req = make_mocked_request(methode, "/api/tools/cfggameplay", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})

    async def body(_r):
        return daten
    monkeypatch.setattr(bot, "body", body)
    r = asyncio.run(handler(req))
    return r.status, json.loads(r.body)


def _get(monkeypatch, c):
    st, j = _aufruf(monkeypatch, c, bot.api_tools_cfggameplay_get)
    assert st == 200
    return j["data"]


def _post(monkeypatch, c, daten):
    return _aufruf(monkeypatch, c, bot.api_tools_cfggameplay_post, "POST", daten)


def _vorschau_und_commit(monkeypatch, c, modell):
    st, v = _post(monkeypatch, c, {"modell": modell})
    assert st == 200, v
    st, j = _post(monkeypatch, c, {"modell": modell, "commit": True, "erwartet_hash": v["data"]["datei_hash"]})
    return st, j, v["data"]


def test_get_liefert_datei_standard_regeln_hilfe_und_presets(monkeypatch, conn):
    d = _get(monkeypatch, conn)
    assert d["karte"] == "Sakhal" and d["karte_bekannt"] is True and d["welt"] == 15360 and d["vorhanden"] is True
    assert d["modell"] == bot._cg_standard("Sakhal")[0] and d["standard"] == d["modell"]
    assert d["hash"] == bot._spawn_datei_hash(conn.ftp.files["/mission/cfggameplay.json"])
    assert d["regeln"]["PlayerData.StaminaData.staminaMax"] == [50, 200] and d["zahlenlisten"]["WorldsData.environmentMinTemps"] == 12
    assert d["hilfe"]["staminaMax"][0] and d["hilfe"]["staminaMax"][1] and len(d["presets"]) == 3 and len(d["beleuchtung"]) == 4
    assert d["kaputt"] is None and d["doppelt"] == [] and d["kein_mission_ordner"] is False


def test_get_zeigt_den_stand_der_datei_nicht_den_standard(monkeypatch, conn):
    m = bot._cg_standard("Sakhal")[0]
    m["PlayerData"]["StaminaData"]["staminaMax"] = 130
    m["ModData"] = {"x": 1}
    conn.ftp.files["/mission/cfggameplay.json"] = json.dumps(m)
    d = _get(monkeypatch, conn)
    assert d["modell"]["PlayerData"]["StaminaData"]["staminaMax"] == 130 and d["modell"]["ModData"] == {"x": 1}
    assert d["standard"]["PlayerData"]["StaminaData"]["staminaMax"] == 100


def test_get_ohne_datei_beginnt_mit_dem_standard_der_karte(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, karte="dayzOffline.nasdara")
    d = _get(monkeypatch, c)
    assert d["vorhanden"] is False and d["karte"] == "Nasdara" and d["modell"] == bot._cg_standard("Nasdara")[0]
    assert d["hash"] == bot._spawn_datei_hash("")


def test_get_unbekannte_karte_wird_gemeldet(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, karte="dayzOffline.deerisle")
    d = _get(monkeypatch, c)
    assert d["karte"] is None and d["karte_bekannt"] is False and d["modell"] == bot._cg_standard(None)[0]


@pytest.mark.parametrize("inhalt, stichwort", [("{ kaputt", "Schlüssel"), ("[1,2]", "kein JSON-Objekt"), ("", "leer")])
def test_get_kaputte_datei_wird_gemeldet_und_nicht_geladen(monkeypatch, conn, inhalt, stichwort):
    conn.ftp.files["/mission/cfggameplay.json"] = inhalt
    d = _get(monkeypatch, conn)
    assert stichwort in d["kaputt"] and d["modell"] is None


def test_get_doppelte_schluessel_werden_gemeldet(monkeypatch, conn):
    conn.ftp.files["/mission/cfggameplay.json"] = '{"version": 123, "version": 130}'
    d = _get(monkeypatch, conn)
    assert d["doppelt"] and d["doppelt"][0]["name"] == "version"


def test_get_ohne_mission_ordner(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, mission=None)
    d = _get(monkeypatch, c)
    assert d["kein_mission_ordner"] is True and d["modell"] is None
    st, j = _post(monkeypatch, c, {"modell": {}})
    assert st == 409


def test_get_lesefehler_und_zu_grosse_datei(monkeypatch, conn):
    conn.ftp.read_file_ex = lambda p: (None, "error")
    st, _j = _aufruf(monkeypatch, conn, bot.api_tools_cfggameplay_get)
    assert st == 502
    conn.ftp.read_file_ex = lambda p: ("{" + " " * (3 * 1024 * 1024) + "}", "ok")
    st, _j = _aufruf(monkeypatch, conn, bot.api_tools_cfggameplay_get)
    assert st == 413


def test_vorschau_schreibt_nichts_und_zeigt_aenderungen(monkeypatch, conn):
    m = bot._cg_standard("Sakhal")[0]
    m["PlayerData"]["StaminaData"]["staminaMax"] = 150
    m["PlayerData"]["spawnGearPresetFiles"] = ["custom/a.json"]
    st, j = _post(monkeypatch, conn, {"modell": m})
    assert st == 200 and conn.ftp.writes == []
    d = j["data"]
    assert d["unveraendert"] is False and d["anzahl"] == 2 and d["backup"] == [] and d["neu_angelegt"] is False
    assert {a["pfad"] for a in d["aenderungen"]} == {"PlayerData.StaminaData.staminaMax", "PlayerData.spawnGearPresetFiles"}
    assert d["generated"][0]["filename"] == "cfggameplay.json" and '"staminaMax": 150' in d["generated"][0]["content"]


def test_unveraendert_wird_nichts_geschrieben(monkeypatch, conn):
    st, j, v = _vorschau_und_commit(monkeypatch, conn, bot._cg_standard("Sakhal")[0])
    assert st == 200 and v["unveraendert"] is True and v["anzahl"] == 0
    assert conn.ftp.writes == [] and j["data"]["backup"] == []


def test_commit_braucht_den_hash_der_vorschau(monkeypatch, conn):
    m = bot._cg_standard("Sakhal")[0]
    m["GeneralData"]["disableBaseDamage"] = True
    for extra in ({}, {"erwartet_hash": "falsch"}):
        st, j = _post(monkeypatch, conn, {"modell": m, "commit": True, **extra})
        assert st == 409 and "seit der Vorschau" in j["error"]
    assert conn.ftp.writes == []


def test_commit_sichert_zuerst_und_schreibt_dann_nur_die_geaenderte_stelle(monkeypatch, conn):
    vorher = conn.ftp.files["/mission/cfggameplay.json"]
    m = bot._cg_standard("Sakhal")[0]
    m["GeneralData"]["disableBaseDamage"] = True
    st, j, _v = _vorschau_und_commit(monkeypatch, conn, m)
    assert st == 200
    assert conn.ftp.writes == ["/mission/cfggameplay.json.bak", "/mission/cfggameplay.json"]
    assert conn.ftp.files["/mission/cfggameplay.json.bak"] == vorher
    assert conn.ftp.files["/mission/cfggameplay.json"] == vorher.replace('"disableBaseDamage": false', '"disableBaseDamage": true')
    assert j["data"]["backup"] == ["cfggameplay.json.bak"]
    assert any("CFGGameplay" in str(e) for e in bot._audit_log)


def test_commit_ohne_vorhandene_datei_legt_sie_neu_an_ohne_sicherung(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, karte="dayzOffline.nasdara")
    m = bot._cg_standard("Nasdara")[0]
    m["PlayerData"]["StaminaData"]["staminaMax"] = 120
    st, j, v = _vorschau_und_commit(monkeypatch, c, m)
    assert st == 200 and v["neu_angelegt"] is True and any("enableCfgGameplayFile" in h for h in v["hinweise"])
    assert c.ftp.writes == ["/mission/cfggameplay.json"] and json.loads(c.ftp.files["/mission/cfggameplay.json"]) == m


def test_hat_sich_die_datei_nach_der_vorschau_geaendert_wird_nichts_geschrieben(monkeypatch, conn):
    m = bot._cg_standard("Sakhal")[0]
    m["GeneralData"]["disableBaseDamage"] = True
    st, v = _post(monkeypatch, conn, {"modell": m})
    conn.ftp.files["/mission/cfggameplay.json"] += "\n"                       # jemand anderes hat gespeichert
    st, j = _post(monkeypatch, conn, {"modell": m, "commit": True, "erwartet_hash": v["data"]["datei_hash"]})
    assert st == 409 and conn.ftp.writes == []


@pytest.mark.parametrize("aenderung, stichwort", [
    (lambda m: m["GeneralData"].__setitem__("disableBaseDamage", "ja"), "true oder false"),
    (lambda m: m["WorldsData"].__setitem__("environmentMinTemps", [1, 2]), "genau 12"),
    (lambda m: m["PlayerData"].__setitem__("StaminaData", 5), "Abschnitt"),
])
def test_typfehler_blockieren_sogar_die_vorschau(monkeypatch, conn, aenderung, stichwort):
    m = bot._cg_standard("Sakhal")[0]
    aenderung(m)
    st, j = _post(monkeypatch, conn, {"modell": m})
    assert st == 422 and stichwort in j["error"] and j["fehler"] and conn.ftp.writes == []


def test_bereichswarnung_blockiert_nicht(monkeypatch, conn):
    m = bot._cg_standard("Sakhal")[0]
    m["PlayerData"]["StaminaData"]["staminaMax"] = 5000
    st, j, v = _vorschau_und_commit(monkeypatch, conn, m)
    assert st == 200 and any("staminaMax" in w for w in v["warnungen"]) and '"staminaMax": 5000' in conn.ftp.files["/mission/cfggameplay.json"]


def test_mehrdeutige_oder_kaputte_datei_wird_nicht_ueberschrieben(monkeypatch, conn):
    m = bot._cg_standard("Sakhal")[0]
    for inhalt in ('{"version": 123, "version": 130}', "{ kaputt", "[1]"):
        conn.ftp.files["/mission/cfggameplay.json"] = inhalt
        st, j = _post(monkeypatch, conn, {"modell": m})
        assert st == 400 and conn.ftp.writes == [], inhalt


def test_scheitert_die_sicherung_wird_die_datei_nicht_angefasst(monkeypatch, conn):
    conn.ftp.fehlschlag.add("/mission/cfggameplay.json.bak")
    m = bot._cg_standard("Sakhal")[0]
    m["GeneralData"]["disableBaseDamage"] = True
    vorher = conn.ftp.files["/mission/cfggameplay.json"]
    st, j, _v = _vorschau_und_commit(monkeypatch, conn, m)
    assert st == 502 and "Sichern" in j["error"] and conn.ftp.files["/mission/cfggameplay.json"] == vorher


def test_scheitert_das_schreiben_kommt_ein_fehler(monkeypatch, conn):
    conn.ftp.fehlschlag.add("/mission/cfggameplay.json")
    m = bot._cg_standard("Sakhal")[0]
    m["GeneralData"]["disableBaseDamage"] = True
    st, j, _v = _vorschau_und_commit(monkeypatch, conn, m)
    assert st == 502 and "konnte nicht gespeichert werden" in j["error"]


def test_gleichzeitiges_speichern_und_schaden_schalter_teilen_sich_den_lock(monkeypatch, conn):
    assert bot._schaden_lock("1000") is bot._schaden_lock("1000")
    with open(os.path.join(REPO, "bot.py"), encoding="utf-8") as f:
        src = f.read()
    a = src.index("async def api_tools_cfggameplay_post")
    assert "async with _schaden_lock(conn.service_id)" in src[a:a + 3000]


def test_unbekannte_schluessel_der_datei_bleiben_beim_speichern(monkeypatch, conn):
    m = bot._cg_standard("Sakhal")[0]
    m["ModData"] = {"x": [1, 2, 3], "y": "z"}
    m["PlayerData"]["MeinSchalter"] = True
    conn.ftp.files["/mission/cfggameplay.json"] = json.dumps(m, indent=4)
    st, d = _aufruf(monkeypatch, conn, bot.api_tools_cfggameplay_get)
    modell = d["data"]["modell"]
    modell["PlayerData"]["StaminaData"]["staminaMax"] = 111
    st, j, _v = _vorschau_und_commit(monkeypatch, conn, modell)
    assert st == 200
    gespeichert = json.loads(conn.ftp.files["/mission/cfggameplay.json"])
    assert gespeichert["ModData"] == {"x": [1, 2, 3], "y": "z"} and gespeichert["PlayerData"]["MeinSchalter"] is True
    assert gespeichert["PlayerData"]["StaminaData"]["staminaMax"] == 111


def test_commit_wird_rate_limitiert(monkeypatch, conn):
    m = bot._cg_standard("Sakhal")[0]
    m["GeneralData"]["disableBaseDamage"] = True
    st, v = _post(monkeypatch, conn, {"modell": m})
    daten = {"modell": m, "commit": True, "erwartet_hash": v["data"]["datei_hash"]}
    assert _post(monkeypatch, conn, daten)[0] == 200
    st, j = _post(monkeypatch, conn, daten)
    assert st == 429 or st == 409          # direkt danach: Sperre bzw. Datei hat sich geaendert (Hash)
