"""Bunker Broadcast Builder: chirurgisches Schreiben der cfgbunkerbroadcast.json und des Schalters in der cfggameplay.json.

Die Testdateien unter tests/fixtures/bunker/ sind die offizielle Nasdara-Datei (Bohemia, DayZ-Central-Economy) und
Abwandlungen davon (Zusatzschluessel, Typen als Text, fehlende Abschnitte, doppelte Standorte, kaputtes JSON ...).

    python3 -m pytest tests/test_bunker_broadcast.py -q
"""
import asyncio
import difflib
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

FIX = os.path.join(REPO, "tests", "fixtures", "bunker")


def _datei(name):
    with open(os.path.join(FIX, name), encoding="utf-8", newline="") as f:
        return f.read()


BOHEMIA = _datei("bohemia_nasdara.json")
GAMEPLAY = _datei("cfggameplay_nasdara.json")
WELT = 16384


def _alles(text, karte="Nasdara"):
    """Eingabe der Oberflaeche, die den Dateistand unveraendert zurueckgibt."""
    m = bot._bb_modell(text, karte)
    felder = {p: e["wert"] for p, e in m["felder"].items() if e["da"] and e["wert"] is not None}
    return {"felder": felder,
            "perioden": ([{"src": e["src"], "start": e["start"], "ende": e["ende"]} for e in m["perioden"]]
                         if m["perioden_da"] else None),
            "proxies": ([{"src": e["src"], "behalten": True} if e["kaputt"]
                         else {"src": e["src"], "x": e["x"], "y": e["y"], "z": e["z"]} for e in m["proxies"]]
                        if m["proxies_da"] else None)}


def _diff(a, b, n=0):
    return list(difflib.unified_diff(a.splitlines(True), b.splitlines(True), n=n))


def _geaenderte_zeilen(a, b):
    return [z for z in _diff(a, b) if z[:1] in "+-" and z[:3] not in ("+++", "---")]


def _py(text):
    return bot._jt_parse(text)[0].py()


# ── Parser ─────────────────────────────────────────────────────────────────
def test_parser_liest_die_offizielle_datei_wie_json_loads():
    wurzel, doppelt = bot._jt_parse(BOHEMIA)
    assert doppelt == [] and wurzel.py() == json.loads(BOHEMIA)
    assert [g[0] for g in wurzel.glieder] == ["bunkerSettings", "schedulerSettings", "playbackSettings", "proxies"]
    # jeder Knoten kennt seine Textposition
    k = wurzel.hole("bunkerSettings").hole("numberOfActive")
    assert BOHEMIA[k.start:k.ende] == "3" and k.ganz is True
    pause = wurzel.hole("playbackSettings").hole("pauseBetweenBunkers")
    assert BOHEMIA[pause.start:pause.ende] == "6.0" and pause.ganz is False


@pytest.mark.parametrize("text, stichwort", [
    ("", "leer"), ("   \n", "leer"), ("{", "Schlüssel"), ('{"a":}', "Unerwartetes"), ("[1,]", "Unerwartetes"),
    ('{"a":1,}', "Schlüssel"), ('{"a":01}', "Zahl"), ('{"a":"\\x"}', "Text"), ('{"a":NaN}', "Unerwartetes"),
    ('{"a":Infinity}', "Unerwartetes"), ('{"a":1} x', "weiterer Inhalt"), ('{"a":tru}', "Unerwartetes"),
    ("{'a':1}", "Schlüssel"), ('{"a":1 "b":2}', "Komma"), ('{"a" 1}', "Doppelpunkt"), ('{"a":"x', "abgeschlossen"),
    ('{"a":1e}', "Zahl"), ('{"a":-}', "Zahl"), ('{"a":"\t"}', "Zeichen"), ("// c\n{}", "Unerwartetes"),
    ("[" * 100 + "]" * 100, "tief"),
])
def test_parser_lehnt_ungueltiges_json_mit_zeile_und_spalte_ab(text, stichwort):
    with pytest.raises(ValueError) as e:
        bot._jt_parse(text)
    assert stichwort in str(e.value)


def test_parser_meldet_fehlerposition():
    with pytest.raises(ValueError, match="Zeile 3, Spalte 8"):
        bot._jt_parse('{\n  "a": 1,\n  "b": x\n}')


def test_parser_erkennt_doppelte_schluessel_und_bom():
    _w, doppelt = bot._jt_parse('{\n"a": 1,\n"a": 2\n}')
    assert doppelt == [("a", 3)]
    w, _d = bot._jt_parse("﻿" + BOHEMIA)
    assert w.py() == json.loads(BOHEMIA)


# ── Unveraendert = Byte fuer Byte ──────────────────────────────────────────
@pytest.mark.parametrize("name", ["bohemia_nasdara.json", "zusatzschluessel.json", "proxy_zusatzfelder.json",
                                  "typen_als_text.json", "ohne_playback.json", "leere_perioden.json",
                                  "position_zwei_werte.json", "doppelter_standort.json", "grenzen_ausserhalb.json",
                                  "fenster_test.json"])
def test_unveraendert_zurueckgeben_ist_byteidentisch(name):
    text = _datei(name)
    neu, hinweise = bot._bb_anwenden(text, _alles(text))
    assert neu == text and hinweise == []


@pytest.mark.parametrize("umformung", [
    lambda t: t.replace("\n", "\r\n"),                       # Windows-Zeilenenden
    lambda t: t.rstrip("\n"),                                # ohne abschliessenden Zeilenumbruch
    lambda t: "﻿" + t,                                  # mit BOM
    lambda t: t.replace("\t", "    "),                       # Leerzeichen statt Tabs
    lambda t: json.dumps(json.loads(t), separators=(",", ":")),   # alles in einer Zeile
])
def test_format_bleibt_bei_unveraendertem_speichern_erhalten(umformung):
    text = umformung(BOHEMIA)
    neu, _h = bot._bb_anwenden(text, _alles(text))
    assert neu == text


def test_zahlenschreibweise_bleibt_erhalten():
    text = BOHEMIA.replace('"dotDuration": 0.5', '"dotDuration": 5E-1').replace('"batchLifetimeLength": 720', '"batchLifetimeLength": 720')
    assert "5E-1" in text
    eing = _alles(text)
    eing["felder"]["bunkerSettings.numberOfActive"] = 4                       # nur dieser Wert aendert sich
    neu, _h = bot._bb_anwenden(text, eing)
    assert "5E-1" in neu and '"pauseBetweenBunkers": 6.0' in neu
    assert len(_geaenderte_zeilen(text, neu)) == 2


# ── Einzelne Aenderungen: kleinster Diff ───────────────────────────────────
def test_ein_wert_aendert_genau_eine_zeile():
    eing = _alles(BOHEMIA)
    eing["felder"]["bunkerSettings.numberOfActive"] = 4
    neu, _h = bot._bb_anwenden(BOHEMIA, eing)
    assert _geaenderte_zeilen(BOHEMIA, neu) == ['-\t\t"numberOfActive": 3,\n', '+\t\t"numberOfActive": 4,\n']


def test_float_feld_bleibt_float_und_ganzzahl_feld_bleibt_ganzzahl():
    eing = _alles(BOHEMIA)
    eing["felder"].update({"playbackSettings.pauseBetweenBunkers": 7, "playbackSettings.dotDuration": 0.75,
                           "bunkerSettings.lockCodeLength": 5})
    neu, _h = bot._bb_anwenden(BOHEMIA, eing)
    assert '"pauseBetweenBunkers": 7.0,' in neu and '"dotDuration": 0.75,' in neu and '"lockCodeLength": 5,' in neu


def test_unbekannte_schluessel_bleiben_beim_aendern_und_loeschen():
    text = _datei("proxy_zusatzfelder.json")
    eing = _alles(text)
    eing["proxies"][0]["y"] = 99.5                      # Eintrag mit "orientation" und "name"
    eing["proxies"].pop(1)
    neu, _h = bot._bb_anwenden(text, eing)
    py = _py(neu)
    assert py["proxies"][0]["name"] == "TEST" and py["proxies"][0]["orientation"] == [0, 90, 0]
    assert py["proxies"][0]["position"][1] == 99.5 and len(py["proxies"]) == len(_py(text)["proxies"]) - 1
    zusatz = _datei("zusatzschluessel.json")
    eing = _alles(zusatz)
    eing["felder"]["bunkerSettings.numberOfActive"] = 5
    assert _py(bot._bb_anwenden(zusatz, eing)[0])["bunkerSettings"]["extra"] == "behalten"


def test_zeitfenster_hinzufuegen_aendern_loeschen():
    eing = _alles(BOHEMIA)
    eing["perioden"][0]["start"] = "1800"
    eing["perioden"].append({"src": None, "start": "1200", "ende": "1200"})
    neu, _h = bot._bb_anwenden(BOHEMIA, eing)
    assert _py(neu)["schedulerSettings"]["periods"] == [{"startTime": "1800", "endTime": "0600"},
                                                         {"startTime": "1200", "endTime": "1200"}]
    assert '\t\t\t{\n\t\t\t\t"startTime": "1200",\n\t\t\t\t"endTime": "1200"\n\t\t\t}' in neu       # Format des Nachbarn
    eing = _alles(BOHEMIA)
    eing["perioden"] = []
    neu, _h = bot._bb_anwenden(BOHEMIA, eing)
    assert _py(neu)["schedulerSettings"]["periods"] == [] and '"periods": []' in neu


def test_standorte_hinzufuegen_verschieben_loeschen():
    eing = _alles(BOHEMIA)
    eing["proxies"].pop(1)
    eing["proxies"][1]["x"] = 11200.5
    eing["proxies"].append({"src": None, "x": 100.5, "y": 20.0, "z": 300.25})
    neu, _h = bot._bb_anwenden(BOHEMIA, eing)
    alt, neu_py = _py(BOHEMIA)["proxies"], _py(neu)["proxies"]
    assert len(neu_py) == 23
    assert neu_py[0] == alt[0] and neu_py[1]["position"] == [11200.5, 176.846359, 9662.226563] and neu_py[2] == alt[3]
    assert neu_py[-1]["position"] == [100.5, 20.0, 300.25]
    # unberuehrte Standorte stehen Zeichen fuer Zeichen unveraendert da (volle Genauigkeit)
    assert "7216.415039000014" in neu and "11945.569336" not in neu


def test_alle_standorte_loeschen_und_wieder_anlegen():
    eing = _alles(BOHEMIA)
    eing["proxies"] = []
    leer, _h = bot._bb_anwenden(BOHEMIA, eing)
    assert _py(leer)["proxies"] == [] and '"proxies": []' in leer
    neu, _h = bot._bb_anwenden(leer, {"proxies": [{"src": None, "x": 1.5, "y": 2.5, "z": 3.5}]})
    assert _py(neu)["proxies"] == [{"position": [1.5, 2.5, 3.5]}]


def test_letzten_standort_loeschen_laesst_kein_komma_zurueck():
    eing = _alles(BOHEMIA)
    eing["proxies"].pop()
    neu, _h = bot._bb_anwenden(BOHEMIA, eing)
    assert len(_py(neu)["proxies"]) == 22 and ",\n  }\n]" not in neu and ",\n]" not in neu


def test_fehlende_abschnitte_und_felder_werden_nur_auf_wunsch_ergaenzt():
    text = _datei("ohne_playback.json")
    assert "playbackSettings" not in _py(text)
    neu, _h = bot._bb_anwenden(text, _alles(text))
    assert neu == text                                                  # nichts wird still ergaenzt
    eing = _alles(text)
    eing["felder"].update({"playbackSettings.dotDuration": 0.5, "playbackSettings.dashDuration": 1.5})
    neu, _h = bot._bb_anwenden(text, eing)
    assert _py(neu)["playbackSettings"] == {"dotDuration": 0.5, "dashDuration": 1.5}
    neu, _h = bot._bb_anwenden(BOHEMIA, {"felder": {"bunkerSettings.lockCodeMode": 1}})
    assert _py(neu)["bunkerSettings"]["lockCodeMode"] == 1


def test_leere_periods_bleiben_leer_bis_ein_fenster_angelegt_wird():
    text = _datei("leere_perioden.json")
    eing = _alles(text)
    assert bot._bb_anwenden(text, eing)[0] == text
    eing["perioden"].append({"src": None, "start": "1745", "ende": "0600"})
    neu, _h = bot._bb_anwenden(text, eing)
    assert _py(neu)["schedulerSettings"]["periods"] == [{"startTime": "1745", "endTime": "0600"}]


def test_neue_datei_wird_aus_den_eingaben_gebaut():
    neu, _h = bot._bb_anwenden(None, {"felder": {"bunkerSettings.numberOfActive": 2, "playbackSettings.dotDuration": 0.3},
                                      "perioden": [{"src": None, "start": "0000", "ende": "2359"}],
                                      "proxies": [{"src": None, "x": 1.0, "y": 2.0, "z": 3.0}]})
    py = _py(neu)
    assert py["bunkerSettings"] == {"numberOfActive": 2} and py["playbackSettings"] == {"dotDuration": 0.3}
    assert py["schedulerSettings"]["periods"] == [{"startTime": "0000", "endTime": "2359"}]
    assert py["proxies"] == [{"position": [1.0, 2.0, 3.0]}]


def test_typen_werden_nur_bewusst_korrigiert_und_gemeldet():
    text = _datei("typen_als_text.json")                                # "3" und "0.5" stehen als Text
    m = bot._bb_modell(text, "Nasdara")
    assert m["felder"]["bunkerSettings.numberOfActive"]["art"] == "text"
    fehler, _w = bot._bb_pruefen(_py(text), "Nasdara", WELT)
    assert any("Aktive Bunker: Der Wert ist keine Zahl." == f for f in fehler)
    eing = _alles(text)
    eing["felder"]["bunkerSettings.numberOfActive"] = 3
    neu, hinweise = bot._bb_anwenden(text, eing)
    assert '"numberOfActive": 3,' in neu and any("Aktive Bunker" in h for h in hinweise)
    zahl = _datei("startzeit_zahl.json")                               # Startzeit 1745 als Zahl
    neu, hinweise = bot._bb_anwenden(zahl, _alles(zahl))
    assert '"startTime": "1745"' in neu and any("Zeitfenster 1" in h for h in hinweise)


def test_kaputte_position_wird_nur_auf_ausdrueckliche_korrektur_ersetzt():
    text = _datei("position_zwei_werte.json")                          # [x, z] statt [x, y, z]
    m = bot._bb_modell(text, "Nasdara")
    assert m["proxies"][0]["kaputt"] is True
    fehler, _w = bot._bb_pruefen(_py(text), "Nasdara", WELT)
    assert any(f.startswith("Bunker 1:") for f in fehler)
    eing = _alles(text)
    eing["proxies"][0] = {"src": 0, "x": 7216.415, "y": 185.2, "z": 5706.9546}
    neu, _h = bot._bb_anwenden(text, eing)
    assert _py(neu)["proxies"][0]["position"] == [7216.415, 185.2, 5706.9546]
    assert bot._bb_pruefen(_py(neu), "Nasdara", WELT)[0] == []


def test_doppelte_schluessel_werden_nicht_automatisch_geaendert():
    text = '{\n  "bunkerSettings": {\n    "numberOfActive": 3,\n    "numberOfActive": 4\n  }\n}\n'
    assert bot._bb_modell(text, "Nasdara")["doppelt"] == [{"name": "numberOfActive", "zeile": 4}]
    with pytest.raises(ValueError, match="mehrfach"):
        bot._bb_anwenden(text, {"felder": {"bunkerSettings.numberOfActive": 5}})


@pytest.mark.parametrize("eingabe", [
    {"felder": {"bunkerSettings.numberOfActive": True}},
    {"felder": {"bunkerSettings.numberOfActive": "3"}},
    {"felder": {"bunkerSettings.numberOfActive": 2.5}},
    {"felder": {"playbackSettings.dotDuration": float("nan")}},
    {"felder": {"playbackSettings.dotDuration": float("inf")}},
    {"felder": []},
    {"perioden": "x"},
    {"perioden": [{"src": None, "start": 1745, "ende": "0600"}]},
    {"perioden": [{"src": None, "start": "1745"}]},
    {"proxies": [{"src": None, "x": 1, "y": 2}]},
    {"proxies": [{"src": None, "x": 1, "y": 2, "z": "3"}]},
    {"proxies": [{"src": None, "x": True, "y": 2, "z": 3}]},
    {"proxies": [{"src": None, "x": float("nan"), "y": 2, "z": 3}]},
    {"proxies": ["x"]},
    {"proxies": [{"src": 0, "behalten": True}]},
])
def test_ungueltige_eingaben_werden_abgelehnt_statt_still_korrigiert(eingabe):
    leer = "{}\n" if "proxies" in eingabe and eingabe["proxies"] == [{"src": 0, "behalten": True}] else None
    with pytest.raises(ValueError):
        bot._bb_anwenden(leer if leer else BOHEMIA if leer is None else leer, eingabe)


def test_defektes_zeitfenster_bleibt_unberuehrt_oder_wird_bewusst_ersetzt():
    text = BOHEMIA.replace('"periods": [', '"periods": [\n\t\t\t5,', 1)
    m = bot._bb_modell(text, "Nasdara")
    assert m["perioden"][0]["kaputt"] is True and m["perioden"][1]["start"] == "1745"
    eing = {"perioden": [{"src": 0, "behalten": True}, {"src": 1, "start": "1800", "ende": "0600"}]}
    neu, _h = bot._bb_anwenden(text, eing)
    assert _py(neu)["schedulerSettings"]["periods"] == [5, {"startTime": "1800", "endTime": "0600"}]
    assert bot._bb_pruefen(_py(neu), "Nasdara", WELT)[0] == ["Zeitfenster 1: Beginn und Ende müssen als Text (HHMM) angegeben sein."]
    ohne = {"perioden": [{"src": 1, "start": "1745", "ende": "0600"}]}                   # das defekte weglassen = loeschen
    assert _py(bot._bb_anwenden(text, ohne)[0])["schedulerSettings"]["periods"] == [{"startTime": "1745", "endTime": "0600"}]
    with pytest.raises(ValueError):
        bot._bb_anwenden('{"schedulerSettings": {}}', {"perioden": [{"src": 0, "behalten": True}]})


def test_wurzel_oder_abschnitt_kein_objekt():
    with pytest.raises(ValueError, match="Wurzel"):
        bot._bb_anwenden("[]", {})
    with pytest.raises(ValueError, match="kein Objekt"):
        bot._bb_anwenden('{"bunkerSettings": 5}', {"felder": {"bunkerSettings.numberOfActive": 3}})
    with pytest.raises(ValueError, match="kein Array"):
        bot._bb_anwenden('{"proxies": {}}', {"proxies": []})


# ── Modell fuer die Oberflaeche ────────────────────────────────────────────
def test_modell_der_offiziellen_datei():
    m = bot._bb_modell(BOHEMIA, "Nasdara")
    assert m["felder"]["bunkerSettings.numberOfActive"] == {"da": True, "wert": 3, "text": "3", "art": "zahl"}
    assert m["felder"]["playbackSettings.pauseBetweenBunkers"]["text"] == "6.0"
    assert m["perioden"] == [{"src": 0, "start": "1745", "ende": "0600", "kaputt": False}]
    assert len(m["proxies"]) == 23 and all(p["nr"] == i for i, p in enumerate(m["proxies"], start=1))
    assert m["unbekannt"] == [] and m["doppelt"] == []
    # bei einer anderen Karte gehoert kein Standort zu einem bekannten Bunker
    assert all(p["nr"] is None for p in bot._bb_modell(BOHEMIA, "Livonia")["proxies"])


def test_modell_meldet_unbekanntes_und_fehlendes():
    m = bot._bb_modell(_datei("zusatzschluessel.json"), "Nasdara")
    assert m["unbekannt"] == ["bunkerSettings.extra"]
    m = bot._bb_modell(_datei("proxy_zusatzfelder.json"), "Nasdara")
    assert m["unbekannt"] == ["proxies[0].orientation", "proxies[0].name"] and m["proxies"][0]["weitere"] is True
    m = bot._bb_modell(_datei("ohne_playback.json"), "Nasdara")
    assert m["felder"]["playbackSettings.dotDuration"]["da"] is False
    assert bot._bb_modell(None, "Nasdara")["felder"] == {} and bot._bb_modell(None, "Nasdara")["proxies_da"] is False
    with pytest.raises(ValueError):
        bot._bb_modell(_datei("kaputt.json"), "Nasdara")


def test_katalog_der_offiziellen_nasdara_bunker():
    katalog = bot._BB_KATALOG["Nasdara"]
    assert len(katalog) == 23
    original = [tuple(p["position"]) for p in json.loads(BOHEMIA)["proxies"]]
    assert list(katalog) == original                                    # volle Genauigkeit, gleiche Reihenfolge
    assert bot._bb_katalog_nr("Nasdara", *katalog[4]) == 5
    assert bot._bb_katalog_nr("Nasdara", katalog[4][0] + 0.9, katalog[4][1], katalog[4][2]) == 5      # innerhalb 1 m
    assert bot._bb_katalog_nr("Nasdara", katalog[4][0] + 1.1, katalog[4][1], katalog[4][2]) is None
    assert bot._bb_katalog_nr("Nasdara", katalog[4][0], katalog[4][1] + 5, katalog[4][2]) is None    # Hoehe zaehlt (3D)
    assert bot._bb_katalog_nr("Livonia", *katalog[4]) is None and bot._bb_katalog_nr(None, *katalog[4]) is None


# ── Pruefung ───────────────────────────────────────────────────────────────
def test_offizielle_datei_ist_gueltig_und_ohne_hinweise_ausser_schalter():
    fehler, warn = bot._bb_pruefen(json.loads(BOHEMIA), "Nasdara", WELT)
    assert fehler == [] and warn == []


@pytest.mark.parametrize("abschnitt, schluessel, ok_werte, schlechte_werte", [
    ("bunkerSettings", "numberOfActive", [1, 30], [0, 31]),
    ("bunkerSettings", "lockCodeLength", [3, 6], [2, 7]),
    ("bunkerSettings", "lockCodeMode", [0, 1], [-1, 2]),
    ("schedulerSettings", "batchLifetimeLength", [1, 1000], [0, 1001]),
    ("schedulerSettings", "reportInterval", [1, 120], [0, 121]),
    ("playbackSettings", "dotDuration", [0.1, 5, 5.0], [0.09, 5.01]),
    ("playbackSettings", "dashDuration", [0.1, 5], [0.05, 6]),
    ("playbackSettings", "symbolGapDuration", [0.1, 5], [0, 5.5]),
    ("playbackSettings", "wordGapDuration", [0.1, 5], [0.0, 6]),
    ("playbackSettings", "pauseBetweenBunkers", [0.5, 10], [0.4, 10.5]),
    ("playbackSettings", "bunkerRepetitions", [0, 3], [-1, 4]),
    ("playbackSettings", "messageRepetitions", [0, 5], [-1, 6]),
])
def test_spannen_sind_inklusive_und_werden_nicht_begrenzt(abschnitt, schluessel, ok_werte, schlechte_werte):
    for wert in ok_werte:
        daten = json.loads(BOHEMIA)
        daten[abschnitt][schluessel] = wert
        assert bot._bb_pruefen(daten, "Nasdara", WELT)[0] == [], wert
    for wert in schlechte_werte:
        daten = json.loads(BOHEMIA)
        daten[abschnitt][schluessel] = wert
        fehler, _w = bot._bb_pruefen(daten, "Nasdara", WELT)
        assert len(fehler) == 1 and "liegt außerhalb von" in fehler[0], (wert, fehler)


@pytest.mark.parametrize("wert", ["3", None, True, [3], {"a": 1}, 2.5])
def test_falsche_typen_sind_fehler(wert):
    daten = json.loads(BOHEMIA)
    daten["bunkerSettings"]["numberOfActive"] = wert
    fehler, _w = bot._bb_pruefen(daten, "Nasdara", WELT)
    assert len(fehler) == 1 and fehler[0].startswith("Aktive Bunker:")


def test_ganzzahl_als_gleitkomma_ist_nur_eine_warnung():
    daten = json.loads(BOHEMIA.replace('"numberOfActive": 3', '"numberOfActive": 3.0'))
    fehler, warn = bot._bb_pruefen(daten, "Nasdara", WELT)
    assert fehler == [] and any("Aktive Bunker" in w and "nicht belegt" in w for w in warn)


def test_alle_elf_bereichsfehler_der_codex_datei():
    fehler, _w = bot._bb_pruefen(json.loads(_datei("grenzen_ausserhalb.json")), "Nasdara", WELT)
    assert len(fehler) == 11 and "Aktive Bunker: 31 liegt außerhalb von 1–30." in fehler
    assert "Punktdauer: 6 liegt außerhalb von 0.1–5." in fehler


@pytest.mark.parametrize("zeit, gueltig", [
    ("0000", True), ("2359", True), ("1745", True), ("0600", True), ("2400", False), ("2360", False),
    ("2500", False), ("1760", False), ("745", False), ("17450", False), ("17:45", False), ("", False), ("abcd", False),
])
def test_zeitfenster_nur_als_echte_hhmm(zeit, gueltig):
    daten = json.loads(BOHEMIA)
    daten["schedulerSettings"]["periods"] = [{"startTime": zeit, "endTime": "0600"}]
    fehler, _w = bot._bb_pruefen(daten, "Nasdara", WELT)
    assert (fehler == []) is gueltig
    if not gueltig:
        assert fehler[0].startswith("Zeitfenster 1:")


def test_codex_fenstertest_viertes_fenster_ist_ungueltig():
    fehler, _w = bot._bb_pruefen(json.loads(_datei("fenster_test.json")), "Nasdara", WELT)
    assert len(fehler) == 1 and fehler[0].startswith("Zeitfenster 4:")


@pytest.mark.parametrize("position", [[1, 2], [1, 2, 3, 4], [1, 2, "3"], [1, None, 3], "x", None, [True, 2, 3]])
def test_positionen_brauchen_genau_drei_zahlen(position):
    daten = json.loads(BOHEMIA)
    daten["proxies"][0] = {"position": position}
    fehler, _w = bot._bb_pruefen(daten, "Nasdara", WELT)
    assert fehler == ["Bunker 1: Die Position braucht genau drei endliche Zahlen [x, y, z]."]


def test_warnungen():
    daten = json.loads(BOHEMIA)
    daten["bunkerSettings"]["numberOfActive"] = 30
    daten["schedulerSettings"]["periods"] = []
    daten["proxies"].append({"position": [100.0, 0, 200.0]})
    daten["proxies"].append({"position": [100.0, 0, 200.0]})
    daten["proxies"].append({"position": [99999.0, 5, 200.0]})
    fehler, warn = bot._bb_pruefen(daten, "Nasdara", WELT)
    assert fehler == []
    texte = "\n".join(warn)
    assert "Kein Zeitfenster" in texte and "mehr als vorhandene Standorte (26)" in texte
    assert "1 Standort(e) kommen mehrfach vor" in texte and "3 von 26 Standorten gehören zu keinem bekannten Bunker" in texte
    assert "2 Standort(e) haben die Höhe 0" in texte and "1 Standort(e) liegen außerhalb der Karte" in texte
    _f, warn = bot._bb_pruefen(json.loads(BOHEMIA), "Sakhal", 15360)
    assert any("keinen belegten Bunkerkatalog" in w for w in warn)
    assert any("mehrfach" in w for w in bot._bb_pruefen(json.loads(_datei("doppelter_standort.json")), "Nasdara", WELT)[1])


def test_datei_validator_nutzt_dieselben_regeln():
    ok_ = bot._fv_validate_one("mission/cfgbunkerbroadcast.json", BOHEMIA, "Nasdara")
    assert ok_["valid"] is True and ok_["issues"] == []
    schlecht = bot._fv_validate_one("cfgbunkerbroadcast.json", _datei("grenzen_ausserhalb.json"), "Nasdara")
    assert schlecht["valid"] is False and schlecht["summary"] == "11 Fehler, 0 Warnungen"
    assert {"severity": "error", "message": "Aktive Bunker: 31 liegt außerhalb von 1–30.", "line": None} in schlecht["issues"]
    doppelt = bot._fv_validate_one("cfgbunkerbroadcast.json", _datei("doppelter_standort.json"), "Nasdara")
    assert doppelt["valid"] is True and doppelt["summary"] == "0 Fehler, 1 Warnungen"
    kaputt = bot._fv_validate_one("cfgbunkerbroadcast.json", _datei("kaputt.json"), "Nasdara")
    assert kaputt["valid"] is False and kaputt["issues"][0]["message"].startswith("JSON-Parsefehler")
    assert bot._fv_validate_one("cfgbunkerbroadcast.json", "[]", "Nasdara")["valid"] is False


def test_wurzel_und_abschnitte_pruefen():
    assert bot._bb_pruefen([], "Nasdara", WELT)[0] == ["Die Wurzel der Datei ist kein JSON-Objekt."]
    fehler, _w = bot._bb_pruefen({"bunkerSettings": 3, "schedulerSettings": {"periods": 5}, "proxies": 7}, "Nasdara", WELT)
    assert "„bunkerSettings“ ist kein Objekt." in fehler and "„schedulerSettings.periods“ ist kein Array." in fehler
    assert "„proxies“ ist kein Array." in fehler


# ── Schalter in der cfggameplay.json ───────────────────────────────────────
def test_schalter_lesen():
    assert bot._bb_schalter_lesen(GAMEPLAY)["wert"] is False
    assert bot._bb_schalter_lesen(GAMEPLAY.replace("false", "true"))["wert"] is True
    ohne = '{"WorldsData": {"lightingConfig": 1}}'
    assert bot._bb_schalter_lesen(ohne) == {"vorhanden": True, "lesbar": True, "wert": None, "ungueltig": False,
                                            "abschnitt": True, "fehler": None}
    assert bot._bb_schalter_lesen('{"version": 1}')["abschnitt"] is False
    assert bot._bb_schalter_lesen('{"WorldsData": {"bunkerBroadcastEnabled": "ja"}}')["ungueltig"] is True
    assert bot._bb_schalter_lesen("kaputt")["lesbar"] is False and bot._bb_schalter_lesen(None)["vorhanden"] is False


def test_schalter_setzen_aendert_nur_den_einen_wert():
    neu = bot._bb_schalter_setzen(GAMEPLAY, True)
    assert _geaenderte_zeilen(GAMEPLAY, neu) == ['-        "bunkerBroadcastEnabled": false\n',
                                                 '+        "bunkerBroadcastEnabled": true\n']
    assert bot._bb_schalter_setzen(neu, True) == neu and bot._bb_schalter_setzen(GAMEPLAY, False) == GAMEPLAY


def test_schalter_wird_im_stil_der_datei_ergaenzt():
    ohne = GAMEPLAY.replace(',\n        "bunkerBroadcastEnabled": false', "")
    assert "bunkerBroadcastEnabled" not in ohne
    neu = bot._bb_schalter_setzen(ohne, True)
    assert _geaenderte_zeilen(ohne, neu) == ['-        "objectSpawnersArr": []\n',
                                             '+        "objectSpawnersArr": [],\n',
                                             '+        "bunkerBroadcastEnabled": true\n']
    assert _py(neu)["WorldsData"]["bunkerBroadcastEnabled"] is True
    ganz_ohne = bot._bb_schalter_setzen('{\n    "version": 1\n}\n', True)
    assert _py(ganz_ohne) == {"version": 1, "WorldsData": {"bunkerBroadcastEnabled": True}}


def test_schalter_verweigert_mehrdeutige_oder_kaputte_dateien():
    with pytest.raises(ValueError, match="mehrfach"):
        bot._bb_schalter_setzen('{"WorldsData": {"bunkerBroadcastEnabled": false, "bunkerBroadcastEnabled": true}}', True)
    with pytest.raises(ValueError, match="kein Objekt"):
        bot._bb_schalter_setzen('{"WorldsData": 3}', True)
    with pytest.raises(ValueError):
        bot._bb_schalter_setzen("{ kaputt", True)


# ── Endpunkte ──────────────────────────────────────────────────────────────
class _FTP:
    def __init__(self, dateien):
        self.files = dict(dateien)
        self.writes = []
        self.deletes = []
        self.fehlschlag = set()                       # Pfade, deren Schreiben scheitert
        self.verzoegerung = 0.0                       # Sekunden je Lesen/Schreiben (fuer Gleichzeitigkeitstests)

    def list_dir(self, d):
        d = d.rstrip("/") + "/"
        return sorted(p for p in self.files if p.startswith(d))

    def read_file_ex(self, p):
        time.sleep(self.verzoegerung)
        return (self.files[p], "ok") if p in self.files else (None, "missing")

    def write_file(self, p, inhalt):
        time.sleep(self.verzoegerung)
        if p in self.fehlschlag:
            return False
        self.writes.append(p)
        self.files[p] = inhalt
        return True

    def delete_file(self, p):
        self.deletes.append(p)
        return self.files.pop(p, None) is not None


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "connections.json").write_text(json.dumps({
        "1000": {"service_id": "1000", "nitrado_token": "fake", "owner_discord_id": "1", "map_name": "dayzOffline.nasdara",
                 "ftp_mission_dir": "/mission"}}))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    monkeypatch.setattr(bot, "_audit_log", bot.deque(maxlen=50))
    c = reg._conns["1000"]
    c.ftp = _FTP({"/mission/cfgbunkerbroadcast.json": BOHEMIA, "/mission/cfggameplay.json": GAMEPLAY})
    return c


def _aufruf(monkeypatch, c, handler, methode="GET", daten=None):
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": True, "service_id": c.service_id,
                            "seen": time.time()}
    req = make_mocked_request(methode, "/api/tools/bunkerbroadcast", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})

    async def body(_r):
        return daten
    monkeypatch.setattr(bot, "body", body)
    r = asyncio.run(handler(req))
    return r.status, json.loads(r.body)


def _get(monkeypatch, c):
    st, j = _aufruf(monkeypatch, c, bot.api_tools_bunkerbroadcast_get)
    assert st == 200
    return j["data"]


def _post(monkeypatch, c, daten):
    return _aufruf(monkeypatch, c, bot.api_tools_bunkerbroadcast_post, "POST", daten)


def test_get_liefert_datei_regeln_katalog_und_schalter(monkeypatch, conn):
    d = _get(monkeypatch, conn)
    assert d["karte"] == "Nasdara" and d["welt"] == 16384 and d["vorhanden"] is True and d["kaputt"] is None
    assert d["hash"] == bot._spawn_datei_hash(BOHEMIA) and d["gameplay_hash"] == bot._spawn_datei_hash(GAMEPLAY)
    assert len(d["katalog"]) == 23 and d["katalog"][0]["nr"] == 1 and d["vorlage"]["perioden"] == [{"start": "1745", "ende": "0600"}]
    assert len(d["vorlage"]["proxies"]) == 23 and len(d["regeln"]) == 12
    assert {"pfad": "bunkerSettings.numberOfActive", "abschnitt": "bunkerSettings", "schluessel": "numberOfActive",
            "typ": "integer", "min": 1, "max": 30, "vorgabe": 2, "nasdara": 3, "einheit": "",
            "name": "Aktive Bunker"} in d["regeln"]
    assert d["schalter"]["wert"] is False and d["fehler"] == [] and len(d["modell"]["proxies"]) == 23
    assert d["stand"]["spielbuild"] == "1.30.164200"


def test_get_ohne_datei_und_mit_kaputter_datei(monkeypatch, conn):
    del conn.ftp.files["/mission/cfgbunkerbroadcast.json"]
    d = _get(monkeypatch, conn)
    assert d["vorhanden"] is False and d["modell"]["felder"] == {} and d["hash"] == bot._spawn_datei_hash("")
    conn.ftp.files["/mission/cfgbunkerbroadcast.json"] = _datei("kaputt.json")
    d = _get(monkeypatch, conn)
    assert d["vorhanden"] is True and d["modell"] is None and "Zeile 1" in d["kaputt"]


def test_get_andere_karte_hat_keinen_katalog(monkeypatch, conn):
    conn.data["map_name"] = "Sakhal"
    d = _get(monkeypatch, conn)
    assert d["karte"] == "Sakhal" and d["katalog"] == [] and d["vorlage"] is None and d["welt"] == 15360
    assert any("keinen belegten Bunkerkatalog" in w for w in d["warnungen"])


def test_vorschau_schreibt_nichts(monkeypatch, conn):
    eing = _alles(BOHEMIA)
    eing["felder"]["bunkerSettings.numberOfActive"] = 4
    st, j = _post(monkeypatch, conn, eing)
    assert st == 200 and conn.ftp.writes == []
    d = j["data"]
    assert d["unveraendert"] is False and d["aenderung"]["felder"] == 1 and d["backup"] == []
    assert [g["filename"] for g in d["generated"]] == ["cfgbunkerbroadcast.json"]
    assert '"numberOfActive": 4' in d["generated"][0]["content"]
    assert any("nicht aktiviert" in w for w in d["warnungen"])                 # Schalter steht auf false


def test_commit_braucht_den_hash_der_vorschau(monkeypatch, conn):
    eing = _alles(BOHEMIA)
    eing["felder"]["bunkerSettings.numberOfActive"] = 4
    st, _j = _post(monkeypatch, conn, dict(eing, commit=True))
    assert st == 409 and conn.ftp.writes == []
    st, _j = _post(monkeypatch, conn, dict(eing, commit=True, erwartet_hash="falsch"))
    assert st == 409 and conn.ftp.writes == []
    conn.ftp.files["/mission/cfgbunkerbroadcast.json"] = BOHEMIA + " "          # jemand hat die Datei inzwischen geaendert
    st, _j = _post(monkeypatch, conn, dict(eing, commit=True, erwartet_hash=bot._spawn_datei_hash(BOHEMIA)))
    assert st == 409 and conn.ftp.writes == []


def test_commit_sichert_und_schreibt_nur_die_geaenderte_datei(monkeypatch, conn):
    eing = _alles(BOHEMIA)
    eing["felder"]["bunkerSettings.numberOfActive"] = 4
    st, j = _post(monkeypatch, conn, dict(eing, commit=True, erwartet_hash=bot._spawn_datei_hash(BOHEMIA)))
    assert st == 200 and j["data"]["backup"] == ["cfgbunkerbroadcast.json.bak"]
    assert j["data"]["ergebnis"] == {"bunker": "geschrieben", "schalter": "unveraendert"}
    assert conn.ftp.writes == ["/mission/cfgbunkerbroadcast.json.bak", "/mission/cfgbunkerbroadcast.json"]
    assert conn.ftp.files["/mission/cfgbunkerbroadcast.json.bak"] == BOHEMIA
    assert _geaenderte_zeilen(BOHEMIA, conn.ftp.files["/mission/cfgbunkerbroadcast.json"]) == [
        '-\t\t"numberOfActive": 3,\n', '+\t\t"numberOfActive": 4,\n']
    assert conn.ftp.files["/mission/cfggameplay.json"] == GAMEPLAY               # Schalter nicht angefasst
    assert any(e["action"] == "Tool: Bunker Broadcast gespeichert" for e in bot._audit_log)


def test_gleichzeitige_speichervorgaenge_ueberschreiben_sich_nicht(monkeypatch, conn):
    """Zwei Speichern-Klicks mit demselben Vorschau-Hash: nur einer darf durch, der andere sieht die geaenderte Datei."""
    conn.ftp.verzoegerung = 0.05
    daten = {}
    for wert in (4, 5):
        eing = _alles(BOHEMIA)
        eing["felder"]["bunkerSettings.numberOfActive"] = wert
        daten[f"sid{wert}"] = dict(eing, commit=True, erwartet_hash=bot._spawn_datei_hash(BOHEMIA))

    async def body(request):
        return daten[request.headers["Cookie"].split("=", 1)[1]]
    monkeypatch.setattr(bot, "body", body)

    async def lauf():
        antworten = []
        for sid in daten:
            bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": True,
                                    "service_id": conn.service_id, "seen": time.time()}
            antworten.append(bot.api_tools_bunkerbroadcast_post(
                make_mocked_request("POST", "/api/tools/bunkerbroadcast", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})))
        return await asyncio.gather(*antworten)
    antworten = asyncio.run(lauf())
    assert sorted(a.status for a in antworten) == [200, 409]
    assert conn.ftp.writes.count("/mission/cfgbunkerbroadcast.json") == 1


def test_commit_ohne_aenderung_schreibt_nichts(monkeypatch, conn):
    st, j = _post(monkeypatch, conn, dict(_alles(BOHEMIA), commit=True, erwartet_hash=bot._spawn_datei_hash(BOHEMIA)))
    assert st == 200 and j["data"]["unveraendert"] is True and conn.ftp.writes == []


def test_schalter_und_bunkerdatei_zusammen(monkeypatch, conn):
    eing = _alles(BOHEMIA)
    eing["felder"]["bunkerSettings.numberOfActive"] = 5
    eing["schalter"] = True
    st, j = _post(monkeypatch, conn, eing)
    assert st == 200 and [g["filename"] for g in j["data"]["generated"]] == ["cfgbunkerbroadcast.json", "cfggameplay.json"]
    assert not any("nicht aktiviert" in w for w in j["data"]["warnungen"])         # mit dem Schalter ist alles aktiv
    assert j["data"]["aenderung"]["schalter_vorher"] is False and j["data"]["aenderung"]["schalter_nachher"] is True
    eing.update(commit=True, erwartet_hash=bot._spawn_datei_hash(BOHEMIA), erwartet_hash_gameplay=bot._spawn_datei_hash(GAMEPLAY))
    st, j = _post(monkeypatch, conn, eing)
    assert st == 200 and j["data"]["ergebnis"] == {"bunker": "geschrieben", "schalter": "geschrieben"}
    assert j["data"]["backup"] == ["cfgbunkerbroadcast.json.bak", "cfggameplay.json.bak"]
    assert conn.ftp.writes == ["/mission/cfgbunkerbroadcast.json.bak", "/mission/cfggameplay.json.bak",
                               "/mission/cfgbunkerbroadcast.json", "/mission/cfggameplay.json"]
    assert conn.ftp.files["/mission/cfggameplay.json.bak"] == GAMEPLAY
    assert _geaenderte_zeilen(GAMEPLAY, conn.ftp.files["/mission/cfggameplay.json"]) == [
        '-        "bunkerBroadcastEnabled": false\n', '+        "bunkerBroadcastEnabled": true\n']


def test_nur_der_schalter(monkeypatch, conn):
    eing = dict(_alles(BOHEMIA), schalter=True, commit=True, erwartet_hash=bot._spawn_datei_hash(BOHEMIA),
                erwartet_hash_gameplay=bot._spawn_datei_hash(GAMEPLAY))
    st, j = _post(monkeypatch, conn, eing)
    assert st == 200 and j["data"]["ergebnis"] == {"bunker": "unveraendert", "schalter": "geschrieben"}
    assert conn.ftp.writes == ["/mission/cfggameplay.json.bak", "/mission/cfggameplay.json"]


def test_schalter_hash_wird_geprueft(monkeypatch, conn):
    eing = dict(_alles(BOHEMIA), schalter=True, commit=True, erwartet_hash=bot._spawn_datei_hash(BOHEMIA))
    st, _j = _post(monkeypatch, conn, eing)                                  # Hash der cfggameplay.json fehlt
    assert st == 409 and conn.ftp.writes == []


def test_schalter_ohne_cfggameplay_wird_abgelehnt(monkeypatch, conn):
    del conn.ftp.files["/mission/cfggameplay.json"]
    st, j = _post(monkeypatch, conn, dict(_alles(BOHEMIA), schalter=True))
    assert st == 409 and "cfggameplay.json" in j["error"]
    st, _j = _post(monkeypatch, conn, dict(_alles(BOHEMIA), schalter="ja"))
    assert st == 400


def test_schreibt_der_schalter_nicht_wird_die_bunkerdatei_zurueckgenommen(monkeypatch, conn):
    conn.ftp.fehlschlag = {"/mission/cfggameplay.json"}
    eing = _alles(BOHEMIA)
    eing["felder"]["bunkerSettings.numberOfActive"] = 5
    eing.update(schalter=True, commit=True, erwartet_hash=bot._spawn_datei_hash(BOHEMIA),
                erwartet_hash_gameplay=bot._spawn_datei_hash(GAMEPLAY))
    st, j = _post(monkeypatch, conn, eing)
    assert st == 502 and j["ergebnis"] == {"bunker": "zurueckgenommen", "schalter": "fehlgeschlagen"}
    assert "zurückgenommen" in j["error"]
    assert conn.ftp.files["/mission/cfgbunkerbroadcast.json"] == BOHEMIA          # Zustand wie vorher
    assert conn.ftp.files["/mission/cfggameplay.json"] == GAMEPLAY


def test_rueknahme_einer_neu_angelegten_datei_loescht_sie_wieder(monkeypatch, conn):
    del conn.ftp.files["/mission/cfgbunkerbroadcast.json"]
    conn.ftp.fehlschlag = {"/mission/cfggameplay.json"}
    eing = {"felder": {"bunkerSettings.numberOfActive": 2}, "schalter": True, "commit": True,
            "erwartet_hash": bot._spawn_datei_hash(""), "erwartet_hash_gameplay": bot._spawn_datei_hash(GAMEPLAY)}
    st, j = _post(monkeypatch, conn, eing)
    assert st == 502 and j["ergebnis"]["bunker"] == "zurueckgenommen"
    assert "/mission/cfgbunkerbroadcast.json" not in conn.ftp.files and conn.ftp.deletes == ["/mission/cfgbunkerbroadcast.json"]


def test_scheitert_die_sicherung_wird_nichts_geschrieben(monkeypatch, conn):
    conn.ftp.fehlschlag = {"/mission/cfgbunkerbroadcast.json.bak"}
    eing = dict(_alles(BOHEMIA), commit=True, erwartet_hash=bot._spawn_datei_hash(BOHEMIA))
    eing["felder"]["bunkerSettings.numberOfActive"] = 4
    st, j = _post(monkeypatch, conn, eing)
    assert st == 502 and "Sichern" in j["error"] and conn.ftp.writes == []
    assert conn.ftp.files["/mission/cfgbunkerbroadcast.json"] == BOHEMIA


def test_ungueltiges_ergebnis_wird_mit_allen_fehlern_abgelehnt(monkeypatch, conn):
    eing = _alles(BOHEMIA)
    eing["felder"]["bunkerSettings.numberOfActive"] = 31
    eing["felder"]["playbackSettings.dotDuration"] = 6
    eing.update(commit=True, erwartet_hash=bot._spawn_datei_hash(BOHEMIA))
    st, j = _post(monkeypatch, conn, eing)
    assert st == 422 and j["fehler"] == ["Aktive Bunker: 31 liegt außerhalb von 1–30.",
                                         "Punktdauer: 6 liegt außerhalb von 0.1–5."]
    assert j["error"] == j["fehler"][0] and conn.ftp.writes == []


def test_neue_datei_wird_angelegt(monkeypatch, conn):
    del conn.ftp.files["/mission/cfgbunkerbroadcast.json"]
    eing = {"felder": {"bunkerSettings.numberOfActive": 3}, "perioden": [{"src": None, "start": "1745", "ende": "0600"}],
            "proxies": [{"src": None, "x": 7216.415039000014, "y": 185.243851, "z": 5706.95459}],
            "commit": True, "erwartet_hash": bot._spawn_datei_hash("")}
    st, j = _post(monkeypatch, conn, eing)
    assert st == 200 and j["data"]["backup"] == [] and j["data"]["ergebnis"]["bunker"] == "geschrieben"
    assert conn.ftp.writes == ["/mission/cfgbunkerbroadcast.json"]                # nichts zu sichern
    assert _py(conn.ftp.files["/mission/cfgbunkerbroadcast.json"])["proxies"][0]["position"][0] == 7216.415039000014


def test_ohne_mission_ordner(monkeypatch, conn):
    conn.data["ftp_mission_dir"] = ""
    d = _get(monkeypatch, conn)
    assert d["kein_mission_ordner"] is True and d["modell"] is None
    st, _j = _post(monkeypatch, conn, {})
    assert st == 409


def test_zu_grosse_datei_wird_nicht_bearbeitet(monkeypatch, conn):
    conn.ftp.files["/mission/cfgbunkerbroadcast.json"] = '{"x": "' + "a" * (2 * 1024 * 1024) + '"}'
    st, _j = _aufruf(monkeypatch, conn, bot.api_tools_bunkerbroadcast_get)
    assert st == 413
    st, _j = _post(monkeypatch, conn, {})
    assert st == 413


def test_zweiter_server_sieht_nicht_die_dateien_des_ersten(monkeypatch, conn, tmp_path):
    flat = json.loads((tmp_path / "connections.json").read_text())
    flat["2000"] = {"service_id": "2000", "nitrado_token": "fake", "owner_discord_id": "2", "map_name": "Livonia",
                    "ftp_mission_dir": "/andere"}
    (tmp_path / "connections.json").write_text(json.dumps(flat))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    reg._conns["1000"].ftp = conn.ftp
    reg._conns["2000"].ftp = _FTP({})
    d = _get(monkeypatch, reg._conns["2000"])
    assert d["vorhanden"] is False and d["karte"] == "Livonia" and d["katalog"] == [] and d["welt"] == 12800
    assert d["schalter"]["vorhanden"] is False
    st, _j = _post(monkeypatch, reg._conns["2000"], dict(_alles(BOHEMIA), commit=True, erwartet_hash=bot._spawn_datei_hash("")))
    assert st == 200 and conn.ftp.writes == [] and "/mission/cfgbunkerbroadcast.json" in conn.ftp.files
    assert reg._conns["2000"].ftp.writes == ["/andere/cfgbunkerbroadcast.json"]


def test_modul_ist_registriert():
    assert bot.FEATURE_MODULES["tools.bunkerbroadcast"]["label"] == "Bunker Broadcast Builder"
    assert ("bunkerbroadcast", "📻", "Bunker Broadcast Builder") in bot._TOOL_LISTE
