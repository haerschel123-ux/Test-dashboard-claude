"""Tests fuer den Erweiterten Types Manager (Einzelbearbeitung der types.xml).

Der Schwerpunkt liegt auf der chirurgischen Bearbeitung: die Vorlage aus dem
Vorgaengerprojekt hat die komplette XML neu serialisiert und dabei Kommentare
und Formatierung verloren. Genau das darf hier nicht passieren.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/ -v
"""
import os
import sys
import xml.etree.ElementTree as ET

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


# Aufbau wie eine echte types.xml: Kommentar des Kunden, gemischte
# Tag-Reihenfolge, ein Item ohne die meisten Tags.
XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<types>
    <!-- Von Brigarde von Hand angepasst - dieser Kommentar muss bleiben -->
    <type name="AKM">
        <nominal>10</nominal>
        <lifetime>7200</lifetime>
        <restock>1800</restock>
        <min>5</min>
        <quantmin>-1</quantmin>
        <quantmax>-1</quantmax>
        <cost>100</cost>
        <flags count_in_cargo="0" count_in_hoarder="0" count_in_map="1" count_in_player="0" crafted="0" deloot="0"/>
        <category name="weapons"/>
        <usage name="Military"/>
        <usage name="Police"/>
        <value name="Tier3"/>
        <value name="Tier4"/>
    </type>
    <type name="TestItem">
        <nominal>1</nominal>
    </type>
</types>
'''


def _anwenden(text, name, aenderung):
    treffer = bot._tool_finde_benannten_block(text, "type", name)
    assert treffer, f"Block {name} nicht gefunden"
    block = bot._tm_block_anwenden(treffer["block"], aenderung)
    return text[:treffer["start"]] + block + text[treffer["end"]:]


def _type(text, name):
    for el in ET.fromstring(text).findall("type"):
        if el.get("name") == name:
            return el
    raise AssertionError(f"{name} fehlt")


# ── Lesen ────────────────────────────────────────────────────────────────
def test_alle_werte_werden_gelesen():
    eintrag = bot._tm_type_lesen(_type(XML, "AKM"))
    assert eintrag["nominal"] == 10
    assert eintrag["lifetime"] == 7200
    assert eintrag["quantmin"] == -1
    assert eintrag["category"] == "weapons"
    assert eintrag["usage"] == ["Military", "Police"]
    assert eintrag["value"] == ["Tier3", "Tier4"]
    assert eintrag["flags"]["count_in_map"] == 1
    assert eintrag["flags"]["deloot"] == 0


def test_fehlende_werte_sind_none_statt_null():
    """Ein fehlendes Tag ist etwas anderes als eine Null - sonst wuerde das
    Dashboard eine Vorgabe anzeigen, die gar nicht in der Datei steht."""
    eintrag = bot._tm_type_lesen(_type(XML, "TestItem"))
    assert eintrag["nominal"] == 1
    assert eintrag["lifetime"] is None
    assert eintrag["cost"] is None


def test_kaputter_zahlenwert_kippt_nicht_die_liste():
    kaputt = XML.replace("<nominal>10</nominal>", "<nominal>viele</nominal>")
    eintrag = bot._tm_type_lesen(_type(kaputt, "AKM"))
    assert eintrag["nominal"] is None


def test_bekannte_werte_kommen_aus_der_datei():
    """Auswahllisten aus der Datei statt fest verdrahtetem Vanilla - sonst
    fehlen auf einem Server mit Mods die eigenen Kategorien."""
    daten = bot._tm_datei_lesen(ET.fromstring(XML))
    assert daten["bekannt"]["category"] == ["weapons"]
    assert daten["bekannt"]["usage"] == ["Military", "Police"]
    assert daten["bekannt"]["value"] == ["Tier3", "Tier4"]


def test_doppelte_items_werden_gemeldet():
    """Die Vorlage nimmt bei doppelten Namen still den letzten Block - das ist
    stiller Datenverlust."""
    doppelt = XML.replace("</types>",
                          '    <type name="AKM"><nominal>99</nominal></type>\n</types>')
    daten = bot._tm_datei_lesen(ET.fromstring(doppelt))
    assert daten["doppelt"] == ["AKM"]
    assert len([t for t in daten["types"] if t["name"] == "AKM"]) == 1


# ── Chirurgisches Schreiben ──────────────────────────────────────────────
def test_kommentar_und_fremde_items_bleiben_erhalten():
    neu = _anwenden(XML, "AKM", {"nominal": 25})
    assert "dieser Kommentar muss bleiben" in neu
    assert '<type name="TestItem">\n        <nominal>1</nominal>' in neu


def test_xml_kopf_bleibt_erhalten():
    neu = _anwenden(XML, "AKM", {"nominal": 25})
    assert neu.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')


def test_zahlen_werden_gesetzt():
    neu = _anwenden(XML, "AKM", {"nominal": 25, "min": 10, "restock": 900})
    el = _type(neu, "AKM")
    assert el.findtext("nominal") == "25"
    assert el.findtext("min") == "10"
    assert el.findtext("restock") == "900"


def test_unberuehrte_tags_bleiben_stehen():
    neu = _anwenden(XML, "AKM", {"nominal": 25})
    el = _type(neu, "AKM")
    assert el.findtext("lifetime") == "7200"
    assert el.find("category").get("name") == "weapons"
    assert [u.get("name") for u in el.findall("usage")] == ["Military", "Police"]


def test_fehlendes_tag_wird_angelegt():
    neu = _anwenden(XML, "TestItem", {"restock": 600, "lifetime": 3600})
    el = _type(neu, "TestItem")
    assert el.findtext("restock") == "600"
    assert el.findtext("lifetime") == "3600"


def test_leerer_wert_entfernt_das_tag():
    """Ausdruecklich geleert heisst: der Server soll seine eigene Vorgabe
    benutzen - nicht die Null, die die Vorlage hineingeschrieben haette."""
    neu = _anwenden(XML, "AKM", {"cost": None})
    assert _type(neu, "AKM").find("cost") is None


def test_listen_werden_vollstaendig_ersetzt():
    neu = _anwenden(XML, "AKM", {"usage": ["Farm"], "value": ["Tier1", "Tier2"]})
    el = _type(neu, "AKM")
    assert [u.get("name") for u in el.findall("usage")] == ["Farm"]
    assert [v.get("name") for v in el.findall("value")] == ["Tier1", "Tier2"]


def test_leere_liste_entfernt_alle_eintraege():
    neu = _anwenden(XML, "AKM", {"usage": []})
    assert _type(neu, "AKM").findall("usage") == []


def test_flags_werden_vollstaendig_geschrieben():
    neu = _anwenden(XML, "AKM", {"flags": {"count_in_cargo": 1, "count_in_map": 1}})
    flags = _type(neu, "AKM").find("flags")
    assert flags.get("count_in_cargo") == "1"
    assert flags.get("count_in_map") == "1"
    assert flags.get("deloot") == "0"


def test_kategorie_wird_gesetzt_und_entfernt():
    assert _type(_anwenden(XML, "AKM", {"category": "tools"}), "AKM") \
        .find("category").get("name") == "tools"
    assert _type(_anwenden(XML, "AKM", {"category": ""}), "AKM").find("category") is None


def test_ergebnis_bleibt_gueltiges_xml():
    neu = _anwenden(XML, "AKM", {"nominal": 25, "usage": ["Farm"],
                                 "flags": {"deloot": 1}, "category": "tools"})
    ET.fromstring(neu)


# ── Prüfung der Eingaben ─────────────────────────────────────────────────
def _bekannt():
    daten = bot._tm_datei_lesen(ET.fromstring(XML))
    return {k: set(v) for k, v in daten["bekannt"].items()}


def test_zahl_ausserhalb_des_bereichs_wird_abgelehnt():
    _, fehler = bot._tm_aenderung_pruefen("AKM", {"cost": 500}, _bekannt())
    assert fehler and "cost" in fehler


def test_keine_zahl_wird_abgelehnt():
    """Die Vorlage macht aus einer ungueltigen Eingabe still eine 0 und
    schreibt sie in die Kundendatei."""
    _, fehler = bot._tm_aenderung_pruefen("AKM", {"nominal": "viele"}, _bekannt())
    assert fehler and "keine ganze Zahl" in fehler


def test_min_darf_nicht_ueber_nominal_liegen():
    _, fehler = bot._tm_aenderung_pruefen("AKM", {"nominal": 10, "min": 20}, _bekannt())
    assert fehler and "min" in fehler


def test_quantmin_darf_minus_eins_sein():
    sauber, fehler = bot._tm_aenderung_pruefen("AKM", {"quantmin": -1}, _bekannt())
    assert fehler is None and sauber["quantmin"] == -1


def test_negatives_nominal_wird_abgelehnt():
    _, fehler = bot._tm_aenderung_pruefen("AKM", {"nominal": -5}, _bekannt())
    assert fehler is not None


def test_unbekannte_kategorie_wird_abgelehnt():
    _, fehler = bot._tm_aenderung_pruefen("AKM", {"category": "gibtsnicht"}, _bekannt())
    assert fehler and "Kategorie" in fehler


def test_unbekannte_flags_werden_abgelehnt():
    _, fehler = bot._tm_aenderung_pruefen("AKM", {"flags": {"boese": 1}}, _bekannt())
    assert fehler and "flags" in fehler


def test_doppelte_listenwerte_werden_entdoppelt():
    sauber, fehler = bot._tm_aenderung_pruefen(
        "AKM", {"usage": ["Military", "Military", "Police"]}, _bekannt())
    assert fehler is None
    assert sauber["usage"] == ["Military", "Police"]


def test_jedes_zahlenfeld_hat_eine_erklaerung():
    """Brigardes Vorgabe: jeder aenderbare Wert bekommt ein Info-Zeichen mit
    Erklaerung - die Texte kommen aus diesen Tabellen."""
    for tag, mini, maxi, hinweis in bot._TM_ZAHLENFELDER:
        assert hinweis.strip(), tag
        assert mini <= maxi
    for tag, hinweis in bot._TM_LISTENFELDER:
        assert hinweis.strip(), tag
    for name, hinweis in bot._TM_FLAGS:
        assert hinweis.strip(), name
