"""Spawn Point Generator: <group name="...">-Struktur der echten cfgplayerspawnpoints.xml.

Bohemias Dateien legen die Punkte in Gruppen (z. B. "WestCherno") unter
<generator_posbubbles> ab. Frueher las das Tool nur lose <pos>-Elemente und
zeigte deshalb "0 Spawn-Punkte" - und ein Speichern haette Gruppen zerlegt.

    python3 -m pytest tests/test_spawnpoint_gruppen.py -v
"""
import os
import sys
import xml.etree.ElementTree as ET

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

DATEI = '''<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<playerspawnpoints>
    <fresh>
        <spawn_params>
            <min_dist_infected>30</min_dist_infected>
        </spawn_params>
        <generator_params>
            <grid_density>4</grid_density>
            <allow_in_water>false</allow_in_water>
        </generator_params>
        <generator_posbubbles>
            <group name="WestCherno">
                <pos x="6063.018555" z="1931.907227" />
                <pos x="5933.964844" z="2171.072998" />
            </group>
            <group name="EastCherno">
                <pos x="8040.858398" z="3332.236328" />
            </group>
        </generator_posbubbles>
    </fresh>
    <hop></hop>
    <travel></travel>
</playerspawnpoints>
'''


def _lesen(text):
    return bot._tool_spawnpoint_positionen_lesen(ET.fromstring(text))


def test_punkte_in_gruppen_werden_gelesen():
    pts = _lesen(DATEI)
    assert len(pts) == 3
    assert [p.get("group") for p in pts] == ["WestCherno", "WestCherno", "EastCherno"]
    assert pts[0]["x"] == "6063.018555"


def test_lose_und_gruppierte_punkte_zusammen():
    text = DATEI.replace('<group name="EastCherno">', '<pos x="1" z="2" />\n            <group name="EastCherno">')
    pts = _lesen(text)
    assert len(pts) == 4 and {"x": "1", "z": "2"} in pts
    assert sum(1 for p in pts if p.get("group")) == 3


def test_speichern_behaelt_gruppen_und_rest_der_datei():
    pts = [{"x": float(p["x"]), "z": float(p["z"]), "group": p.get("group", "")} for p in _lesen(DATEI)]
    neu = bot._tool_spawnpoint_positionen_schreiben(DATEI, pts, "replace")
    ET.fromstring(neu)                                   # bleibt gueltiges XML
    assert [(p.get("group"), round(float(p["x"]))) for p in _lesen(neu)] == \
        [(p.get("group"), round(float(p["x"]))) for p in _lesen(DATEI)]
    assert neu.count("<group ") == 2 and "</group>" in neu
    for rest in ("<min_dist_infected>30</min_dist_infected>", "<grid_density>4</grid_density>",
                 "<hop></hop>", "<travel></travel>"):
        assert rest in neu


def test_punkt_loeschen_und_neuen_hinzufuegen():
    pts = [{"x": float(p["x"]), "z": float(p["z"]), "group": p.get("group", "")} for p in _lesen(DATEI)]
    pts = [p for p in pts if round(p["x"]) != 5934]       # einen alten Punkt loeschen
    pts.append({"x": 150.2, "z": 200.5})                    # neuen (losen) Punkt anlegen
    pts.append({"x": 300.0, "z": 400.0, "group": "Neu Gruppe"})
    neu = bot._tool_spawnpoint_positionen_schreiben(DATEI, pts, "replace")
    ET.fromstring(neu)
    erg = _lesen(neu)
    assert len(erg) == 4
    xs = {round(float(p["x"])) for p in erg}
    assert 5934 not in xs and {150, 300, 6063, 8041} <= xs
    assert any(p.get("group") == "Neu Gruppe" for p in erg)
    assert any("group" not in p and round(float(p["x"])) == 150 for p in erg)


def test_ungueltiger_gruppenname_wird_zu_losem_punkt():
    pts = [{"x": 1.0, "z": 2.0, "group": '"><evil/>'}]
    neu = bot._tool_spawnpoint_positionen_schreiben(DATEI, pts, "replace")
    ET.fromstring(neu)
    assert "evil" not in neu and len(_lesen(neu)) == 1 and "group" not in _lesen(neu)[0]


def test_leere_liste_leert_die_bubbles_ohne_reste():
    neu = bot._tool_spawnpoint_positionen_schreiben(DATEI, [], "replace")
    ET.fromstring(neu)
    assert _lesen(neu) == [] and "<group" not in neu


def test_flache_ausgabe_wie_bisher_ohne_gruppen():
    flach = DATEI.replace('<group name="WestCherno">', "").replace('<group name="EastCherno">', "") \
        .replace("</group>", "")
    neu = bot._tool_spawnpoint_positionen_schreiben(
        flach, [{"x": 5.0, "z": 6.0}, {"x": 7.0, "z": 8.0}], "replace")
    assert neu.count("<pos ") == 2 and "<group" not in neu
    assert '            <pos x="5" z="6" />' in neu


# ── API: Vorschau schreibt nichts, Fehler werden benannt statt "HTTP 500" ──
import asyncio
import json
import re

sys.path.insert(0, os.path.join(REPO, "tests"))
import test_deployment as td  # noqa: E402  (nutzt dessen FTP-Attrappe)


class _MP:
    def setattr(self, o, n, v):
        setattr(o, n, v)


def _datei_mit_wasser():
    return DATEI.replace("<grid_density>4</grid_density>",
                         "<grid_density>4</grid_density>\n            <allow_in_water>false</allow_in_water>")


def _post(conn, daten):
    if daten.get("commit") and "erwartet_hash" not in daten:        # wie das Dashboard: Hash der Vorschau mitschicken
        daten = dict(daten, erwartet_hash=bot._spawn_datei_hash(conn.ftp.dateien.get("/mission/cfgplayerspawnpoints.xml")))
    req = td._request(_MP(), conn, "POST", "/api/tools/spawnpoint", daten)
    r = asyncio.run(bot.api_tools_spawnpoint_post(req))
    return r.status, json.loads(r.body.decode("utf-8"))


def _punkte():
    return [{"x": float(p["x"]), "z": float(p["z"]), "group": p.get("group", "")}
            for p in _lesen(DATEI)]


def test_vorschau_schreibt_nichts_auf_den_server():
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser()
    vorher = conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"]
    status, j = _post(conn, {"positions": _punkte()[:2], "mode": "replace", "allow_in_water": False})
    assert status == 200 and j["data"]["total"] == 2
    assert conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] == vorher       # unveraendert


def test_erst_commit_schreibt():
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser()
    status, j = _post(conn, {"positions": _punkte()[:1], "mode": "replace", "allow_in_water": False,
                             "commit": True})
    assert status == 200
    assert conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"].count("<pos ") == 1


def test_unerwarteter_fehler_wird_benannt_statt_http_500(monkeypatch):
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser()

    def kaputt(*a, **k):
        raise RuntimeError("Testfehler")
    monkeypatch.setattr(bot, "_tool_spawnpoint_positionen_schreiben", kaputt)
    status, j = _post(conn, {"positions": _punkte(), "mode": "replace"})
    assert status == 500 and "RuntimeError: Testfehler" in j["error"] and "nichts wurde gespeichert" in j["error"]


def test_ftp_fehler_beim_lesen_und_schreiben_werden_benannt(monkeypatch):
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser()

    async def lesen_kaputt(*a, **k):
        raise OSError("Verbindung getrennt")
    monkeypatch.setattr(bot, "_tools_datei_lesen", lesen_kaputt)
    status, j = _post(conn, {"positions": _punkte(), "mode": "replace"})
    assert status == 502 and "OSError: Verbindung getrennt" in j["error"]


def test_vorschau_nennt_aenderung_gegenueber_dem_server():
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser()
    pts = _punkte()[:2] + [{"x": 111.0, "z": 222.0}]         # einen alten Punkt weglassen, einen neuen dazu
    status, j = _post(conn, {"positions": pts, "mode": "replace", "allow_in_water": False})
    assert status == 200
    assert j["data"]["aenderung"] == {"vorher": 3, "nachher": 3, "neu": 1, "entfernt": 1}


def test_unveraendert_speichern_aendert_die_datei_nicht():
    """Keine stille Rundung: Koordinaten behalten alle Nachkommastellen aus Bohemias Datei."""
    neu = bot._tool_spawnpoint_positionen_schreiben(DATEI, _punkte(), "replace")
    assert neu == DATEI


# ── Echte Dateien erklaeren ihre Tags in Kommentaren (Sakhal, Nasdara) ──
MIT_KOMMENTAREN = '''<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<playerspawnpoints>
    <!--Spawn points settings for newly created player characters-->
    <fresh>
        <!--
            Spawn points are generated around <pos> child elements under the <generator_posbubbles> element.
            Example: <group name="Beispiel"><pos x="1" z="2" /></group>
            Water: <allow_in_water>true</allow_in_water>
        -->
        <generator_params>
            <grid_density>4</grid_density>
            <!--Spawns in water (<allow_in_water>true</allow_in_water> allows it)-->
            <allow_in_water>false</allow_in_water>
        </generator_params>
        <!--Centers for grids of spawn points-->
        <generator_posbubbles>
            <!--
                Each <pos> is a bubble, optionally inside a <group name="X">...</group>.
            -->
            <group name="AllSpawnpoints">
                <pos x="14069.808594" z="13083.109375" />
                <pos x="13571.811523" z="12821.112305" />
            </group>
        </generator_posbubbles>
    </fresh>
    <!--Spawn points settings for server hoppers-->
    <hop>
        <generator_posbubbles>
            <pos x="1" z="1" />
        </generator_posbubbles>
    </hop>
    <travel></travel>
</playerspawnpoints>
'''


def test_tags_in_kommentaren_werden_nicht_mit_echten_verwechselt():
    pts = [{"x": float(p["x"]), "z": float(p["z"]), "group": p.get("group", "")}
           for p in _lesen(MIT_KOMMENTAREN)]
    assert len(pts) == 2
    neu = bot._tool_spawnpoint_positionen_schreiben(MIT_KOMMENTAREN, pts, "replace")
    neu = bot._tool_spawnpoint_allow_in_water_schreiben(neu, False)
    ET.fromstring(neu)                                   # frueher: "not well-formed" (Zeile 141)
    assert neu == MIT_KOMMENTAREN                         # unveraendert gespeichert = identisch


def test_kommentare_und_hop_bleiben_bei_aenderungen_erhalten():
    pts = [{"x": 14069.808594, "z": 13083.109375, "group": "AllSpawnpoints"},
           {"x": 5.0, "z": 6.0, "group": ""}]
    neu = bot._tool_spawnpoint_positionen_schreiben(MIT_KOMMENTAREN, pts, "replace")
    neu = bot._tool_spawnpoint_allow_in_water_schreiben(neu, True)
    ET.fromstring(neu)
    assert neu.count("<!--") == MIT_KOMMENTAREN.count("<!--")
    assert '<pos x="1" z="1" />' in neu                     # <hop> bleibt unangetastet
    assert "<allow_in_water>true</allow_in_water>" in neu      # echte Zeile geaendert ...
    assert "Water: <allow_in_water>true</allow_in_water>" in neu  # ... Kommentar bleibt wie er war
    assert len(_lesen(neu)) == 2


def test_selbstschliessende_posbubbles_werden_gefuellt():
    text = MIT_KOMMENTAREN.replace(
        '<generator_posbubbles>\n            <!--\n                Each <pos> is a bubble, optionally inside a '
        '<group name="X">...</group>.\n            -->\n            <group name="AllSpawnpoints">\n'
        '                <pos x="14069.808594" z="13083.109375" />\n                <pos x="13571.811523" '
        'z="12821.112305" />\n            </group>\n        </generator_posbubbles>',
        '<generator_posbubbles />')
    assert "<generator_posbubbles />" in text
    neu = bot._tool_spawnpoint_positionen_schreiben(text, [{"x": 5.0, "z": 6.0}], "replace")
    ET.fromstring(neu)
    assert len(_lesen(neu)) == 1


def test_gruppen_attribute_wie_lifetime_bleiben_erhalten():
    text = DATEI.replace('<group name="WestCherno">', '<group name="WestCherno" lifetime="0">')
    assert 'lifetime="0"' in text
    punkte = _lesen(text)
    assert bot._tool_spawnpoint_positionen_schreiben(text, punkte, "replace") == text
    neu = bot._tool_spawnpoint_positionen_schreiben(text, punkte[1:], "replace")
    assert '<group name="WestCherno" lifetime="0">' in neu
    ET.fromstring(neu)


def test_punkte_ausserhalb_der_karte_werden_abgelehnt_und_nichts_geschrieben():
    conn = td._server(map_name="Nasdara")                       # Weltgroesse 16384
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser()
    vorher = conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"]
    for x, z in ((-3008.0, 100.0), (100.0, -1.0), (16385.0, 100.0), (100.0, 20000.0)):
        status, j = _post(conn, {"positions": _punkte()[:1] + [{"x": x, "z": z}],
                                 "mode": "replace", "commit": True})
        assert status == 400 and "ausserhalb der Karte" in j["error"] and "Nichts wurde gespeichert" in j["error"]
    assert conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] == vorher
    status, _ = _post(conn, {"positions": [{"x": 0.0, "z": 16384.0}], "mode": "replace"})   # Rand ist erlaubt
    assert status == 200


def test_vorschau_nennt_unveraenderte_hop_und_travel_punkte():
    """Bohemias Dateien fuehren dieselben Punkte auch in hop/travel; nur fresh wird geaendert."""
    conn = td._server(map_name="Nasdara")
    hop = '<hop><generator_posbubbles><group name="G"><pos x="1" z="2"/><pos x="3" z="4"/></group></generator_posbubbles></hop>'
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser().replace("<hop></hop>", hop)
    status, j = _post(conn, {"positions": [{"x": 5.0, "z": 6.0}], "mode": "replace", "allow_in_water": False})
    assert status == 200 and j["data"]["unveraendert"] == {"hop": 2}
    assert j["data"]["total"] == 1
    assert j["data"]["generated"][0]["content"].count("<pos ") == 3        # 1 fresh + 2 hop bleiben


# ── Schalter "auch hop und travel" und "allow_in_water ergaenzen" (beides nur auf Wunsch) ──
def _drei_abschnitte():
    """Wie Bohemias Dateien: fresh, hop und travel mit je eigenem generator_params/posbubbles."""
    teil = ('<generator_params>\n<allow_in_water>false</allow_in_water>\n</generator_params>\n'
            '<generator_posbubbles>\n<!-- <pos> und <group> im Kommentar -->\n'
            '<group name="AllSpawnpoints" lifetime="0"><pos x="1" z="2"/><pos x="3" z="4"/></group>\n'
            '</generator_posbubbles>\n')
    return ('<?xml version="1.0"?>\n<playerspawnpoints>\n<fresh>\n' + teil + '</fresh>\n<hop>\n' + teil
            + '</hop>\n<travel>\n' + teil + '</travel>\n</playerspawnpoints>\n')


def _anzahl(text, abschnitt):
    return len(ET.fromstring(text).findall(f"{abschnitt}/generator_posbubbles//pos"))


def test_ohne_schalter_bleiben_hop_und_travel_unveraendert():
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _drei_abschnitte()
    status, j = _post(conn, {"positions": [{"x": 9.0, "z": 9.0}], "mode": "replace", "allow_in_water": True})
    assert status == 200
    neu = j["data"]["generated"][0]["content"]
    assert (_anzahl(neu, "fresh"), _anzahl(neu, "hop"), _anzahl(neu, "travel")) == (1, 2, 2)
    assert j["data"]["unveraendert"] == {"hop": 2, "travel": 2}
    root = ET.fromstring(neu)
    assert [root.findtext(f"{a}/generator_params/allow_in_water") for a in ("fresh", "hop", "travel")] == \
        ["true", "false", "false"]


def test_mit_schalter_bekommen_alle_drei_abschnitte_dieselben_punkte():
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _drei_abschnitte()
    pts = [{"x": 9.0, "z": 9.0}, {"x": 10.5, "z": 11.0, "group": "Nord"}]
    status, j = _post(conn, {"positions": pts, "mode": "replace", "allow_in_water": True,
                             "auch_hop_travel": True, "commit": True})
    assert status == 200 and j["data"]["unveraendert"] == {}
    neu = conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"]
    root = ET.fromstring(neu)
    for a in ("fresh", "hop", "travel"):
        assert _anzahl(neu, a) == 2 and root.findtext(f"{a}/generator_params/allow_in_water") == "true"
        assert root.find(f"{a}/generator_posbubbles/group").get("name") == "Nord"
    assert neu.count("<!-- <pos> und <group> im Kommentar -->") == 3        # Kommentare bleiben


def test_schalter_ohne_hop_abschnitt_meldet_klaren_fehler_und_schreibt_nichts():
    conn = td._server(map_name="Nasdara")
    vorher = _datei_mit_wasser()
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = vorher
    status, j = _post(conn, {"positions": [{"x": 1.0, "z": 1.0}], "mode": "replace",
                             "auch_hop_travel": True, "commit": True})
    assert status == 400 and "keinen Abschnitt" in j["error"]
    assert conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] == vorher


def test_fehlendes_allow_in_water_wird_nur_auf_wunsch_ergaenzt():
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser().replace(
        "<allow_in_water>false</allow_in_water>", "")
    status, j = _post(conn, {"positions": [{"x": 1.0, "z": 1.0}], "mode": "replace", "allow_in_water": True})
    assert status == 400 and "ergänzen" in j["error"]                   # ohne aktive Wahl: wie bisher Fehler
    status, j = _post(conn, {"positions": [{"x": 1.0, "z": 1.0}], "mode": "replace", "allow_in_water": True,
                             "allow_in_water_ergaenzen": True})
    assert status == 200
    root = ET.fromstring(j["data"]["generated"][0]["content"])
    assert root.findtext("fresh/generator_params/allow_in_water") == "true"


# ── Sicherheit: Datei-Check zwischen Vorschau und Upload, Backup, Warnungen ──
def test_upload_ohne_oder_mit_veraltetem_hash_wird_abgelehnt_und_schreibt_nichts():
    conn = td._server(map_name="Nasdara")
    pfad = "/mission/cfgplayerspawnpoints.xml"
    conn.ftp.dateien[pfad] = _datei_mit_wasser()
    vorher = conn.ftp.dateien[pfad]
    for hash_ in ("", "veraltet", None):
        status, j = _post(conn, {"positions": _punkte()[:1], "mode": "replace", "commit": True,
                                 "erwartet_hash": hash_})
        assert status == 409 and "seit der Vorschau geändert" in j["error"]
    assert conn.ftp.dateien[pfad] == vorher and pfad + ".bak" not in conn.ftp.dateien


def test_vorschau_liefert_hash_und_geaenderte_datei_verlangt_neue_vorschau():
    conn = td._server(map_name="Nasdara")
    pfad = "/mission/cfgplayerspawnpoints.xml"
    conn.ftp.dateien[pfad] = _datei_mit_wasser()
    status, j = _post(conn, {"positions": _punkte()[:1], "mode": "replace"})
    h = j["data"]["datei_hash"]
    assert status == 200 and h == bot._spawn_datei_hash(conn.ftp.dateien[pfad])
    conn.ftp.dateien[pfad] = conn.ftp.dateien[pfad].replace("<hop></hop>", "<hop></hop><!-- fremd -->")   # jemand aendert die Datei
    status, j = _post(conn, {"positions": _punkte()[:1], "mode": "replace", "commit": True, "erwartet_hash": h})
    assert status == 409 and "<!-- fremd -->" in conn.ftp.dateien[pfad]
    assert pfad + ".bak" not in conn.ftp.dateien


def test_upload_sichert_vorher_die_alte_datei_als_bak():
    conn = td._server(map_name="Nasdara")
    pfad = "/mission/cfgplayerspawnpoints.xml"
    alt = conn.ftp.dateien[pfad] = _datei_mit_wasser()
    status, j = _post(conn, {"positions": _punkte()[:1], "mode": "replace", "commit": True})
    assert status == 200 and j["data"]["backup"] == "cfgplayerspawnpoints.xml.bak"
    assert conn.ftp.dateien[pfad + ".bak"] == alt and conn.ftp.dateien[pfad] != alt
    # Vorschau (commit=False) legt kein Backup an
    conn2 = td._server(map_name="Nasdara"); conn2.ftp.dateien[pfad] = _datei_mit_wasser()
    status, j = _post(conn2, {"positions": _punkte()[:1], "mode": "replace"})
    assert status == 200 and pfad + ".bak" not in conn2.ftp.dateien and j["data"]["backup"] is None


def test_warnungen_doppelte_nahe_und_rechtecke_ueber_dem_kartenrand():
    # Datei mit grid_width/height 200 -> Punkt bei x=50 ragt ueber den linken Rand (50-100 < 0)
    text = _datei_mit_wasser().replace("<grid_density>4</grid_density>",
        "<grid_density>4</grid_density><grid_width>200</grid_width><grid_height>200</grid_height>")
    pts = [{"x": 5000.0, "z": 5000.0}, {"x": 5000.0, "z": 5000.2},          # doppelt
           {"x": 6000.0, "z": 6000.0}, {"x": 6006.0, "z": 6000.0},          # nahe (6 m)
           {"x": 50.0, "z": 8000.0}, {"x": 8000.0, "z": 16380.0}]            # beide ragen ueber den Rand
    w = bot._spawn_warnungen(pts, text, 16384)
    assert any("exakt übereinander" in x and x.startswith("1 ") for x in w)
    assert any("weniger als 10 m" in x and x.startswith("1 ") for x in w)
    assert any("Kartenrand" in x and x.startswith("2 ") for x in w)
    assert bot._spawn_warnungen([{"x": 5000.0, "z": 5000.0}, {"x": 9000.0, "z": 9000.0}], text, 16384) == []


def test_vorschau_antwort_enthaelt_warnungen():
    conn = td._server(map_name="Nasdara")
    conn.ftp.dateien["/mission/cfgplayerspawnpoints.xml"] = _datei_mit_wasser()
    status, j = _post(conn, {"positions": [{"x": 100.0, "z": 100.0}, {"x": 101.0, "z": 100.0}], "mode": "replace"})
    assert status == 200 and any("weniger als 10 m" in x for x in j["data"]["warnungen"])
