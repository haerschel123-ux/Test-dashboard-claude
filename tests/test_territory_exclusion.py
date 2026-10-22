"""Territory Exclusion: Zonen per Kreis oder Rechteck aus env/*_territories.xml entfernen (echte Nasdara-Dateien).

    python3 -m pytest tests/test_territory_exclusion.py -q
"""
import asyncio
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

FIX = os.path.join(REPO, "tests", "fixtures", "territory")


def _datei(name):
    with open(os.path.join(FIX, name), encoding="utf-8", newline="") as f:
        return f.read()


WOLF = _datei("nasdara_wolf_territories.xml")
HEN = _datei("nasdara_hen_territories.xml")
WELT = 16384


def _zonen(text):
    return bot._terr_scannen(text)["zonen"]


def _kreis(x, z, r):
    return {"art": "kreis", "x": x, "z": z, "r": r}


def _rechteck(x1, z1, x2, z2):
    return {"art": "rechteck", "x1": x1, "z1": z1, "x2": x2, "z2": z2}


def _treffer(text, bereiche):
    b = bot._tex_bereiche(bereiche, WELT)
    return [z["index"] for z in _zonen(text) if any(bot._tex_trifft(f, z["x"], z["z"]) for f in b)]


def _zeilen(text):
    return text.split("\n")


# ── Flaechen pruefen ───────────────────────────────────────────────────────
def test_bereiche_werden_normalisiert():
    r = bot._tex_bereiche([_rechteck(900, 800, 100, 200), _kreis(5, 6, 7)], WELT)
    assert r == [{"art": "rechteck", "x1": 100.0, "z1": 200.0, "x2": 900.0, "z2": 800.0},
                 {"art": "kreis", "x": 5.0, "z": 6.0, "r": 7.0}]


@pytest.mark.parametrize("roh, stichwort", [
    (None, "Keine Flächenliste"), ("x", "Keine Flächenliste"), ({}, "Keine Flächenliste"), ([], "mindestens eine"),
    ([_kreis(1, 1, 1)] * 201, "Zu viele Flächen"),
    (["x"], "Unbekannte Form"), ([{"art": "dreieck"}], "Unbekannte Form"), ([{}], "Unbekannte Form"),
    ([_kreis(10, 10, 0)], "Radius größer als 0"), ([_kreis(10, 10, -5)], "Radius größer als 0"),
    ([_kreis(10, 10, WELT + 1)], "Radius größer als 0"),
    ([_kreis("10", 10, 5)], "Radius größer als 0"), ([_kreis(True, 10, 5)], "Radius größer als 0"),
    ([_kreis(float("nan"), 10, 5)], "Radius größer als 0"), ([_kreis(10, float("inf"), 5)], "Radius größer als 0"),
    ([{"art": "kreis", "x": 1, "z": 2}], "Radius größer als 0"),
    ([_rechteck(1, 2, 3, None)], "vier Eckwerte"), ([_rechteck(1, 2, 3, "4")], "vier Eckwerte"),
    ([{"art": "rechteck", "x1": 1}], "vier Eckwerte"),
    ([_rechteck(10, 10, 10, 50)], "Breite und eine Höhe"), ([_rechteck(10, 10, 50, 10)], "Breite und eine Höhe"),
    ([_kreis(-1, 10, 5)], "außerhalb der Karte"), ([_kreis(10, WELT + 1, 5)], "außerhalb der Karte"),
    ([_rechteck(-5, 0, 100, 100)], "außerhalb der Karte"), ([_rechteck(0, 0, WELT + 1, 100)], "außerhalb der Karte"),
])
def test_ungueltige_flaechen_werden_abgelehnt(roh, stichwort):
    with pytest.raises(ValueError) as e:
        bot._tex_bereiche(roh, WELT)
    assert stichwort in str(e.value)


def test_fehlermeldung_nennt_die_nummer_der_flaeche():
    with pytest.raises(ValueError, match="^Fläche 3: "):
        bot._tex_bereiche([_kreis(1, 1, 1), _rechteck(0, 0, 5, 5), _kreis(1, 1, 0)], WELT)


def test_ohne_kartengroesse_gilt_nur_nicht_negativ():
    assert bot._tex_bereiche([_kreis(99999, 99999, 50)], None)
    with pytest.raises(ValueError, match="nicht negativ"):
        bot._tex_bereiche([_kreis(-1, 5, 5)], None)


def test_flaechen_am_kartenrand_sind_erlaubt():
    assert bot._tex_bereiche([_kreis(0, 0, 100), _kreis(WELT, WELT, 100), _rechteck(0, 0, WELT, WELT)], WELT)


# ── Trefferregel: Mittelpunkt in der Flaeche, Rand zaehlt ──────────────────
def test_kreis_rand_zaehlt_dazu():
    b = bot._tex_bereiche([_kreis(1000, 1000, 100)], WELT)[0]
    assert bot._tex_trifft(b, 1000, 1000) and bot._tex_trifft(b, 1100, 1000) and bot._tex_trifft(b, 1000, 900)
    assert not bot._tex_trifft(b, 1100.01, 1000) and not bot._tex_trifft(b, 1071, 1071)       # 100.4 m diagonal
    assert bot._tex_trifft(b, 1070, 1070)                                                    # 98.9 m diagonal


def test_rechteck_rand_zaehlt_dazu_und_ecken_beliebig():
    for ecken in ((100, 200, 300, 400), (300, 400, 100, 200), (100, 400, 300, 200)):
        b = bot._tex_bereiche([_rechteck(*ecken)], WELT)[0]
        assert bot._tex_trifft(b, 100, 200) and bot._tex_trifft(b, 300, 400) and bot._tex_trifft(b, 200, 300)
        assert not bot._tex_trifft(b, 99.99, 300) and not bot._tex_trifft(b, 200, 400.01)


def test_zonenradius_zaehlt_nicht_nur_der_mittelpunkt():
    """Eine grosse Zone, die die Flaeche nur ueberlappt, bleibt - entscheidend ist der Mittelpunkt."""
    text = ('<territory-type>\n  <territory color="1">\n'
            '    <zone name="Gross" smin="0" smax="0" dmin="0" dmax="0" x="1000" z="1000" r="500"/>\n'
            '    <zone name="Drin" smin="0" smax="0" dmin="0" dmax="0" x="1500" z="1500" r="10"/>\n'
            '  </territory>\n</territory-type>\n')
    assert _treffer(text, [_kreis(1600, 1600, 200)]) == [1]                       # Gross reicht in den Kreis, bleibt aber
    assert _treffer(text, [_rechteck(900, 900, 1100, 1100)]) == [0]


def test_mehrere_flaechen_vereinigen_sich():
    a = set(_treffer(WOLF, [_kreis(8901.07, 7709.6, 50)]))
    b = set(_treffer(WOLF, [_rechteck(0, 0, 4000, 11000)]))                       # die Zonen mit x < 4000
    ab = set(_treffer(WOLF, [_kreis(8901.07, 7709.6, 50), _rechteck(0, 0, 4000, 11000)]))
    assert a == {0} and b and not (a & b) and ab == a | b


def test_trefferzahl_stimmt_mit_nachrechnen_ueberein():
    flaechen = [_kreis(8000, 8000, 2500), _rechteck(2000, 2000, 6000, 9000)]
    erwartet = [z["index"] for z in _zonen(WOLF)
                if math.hypot(z["x"] - 8000, z["z"] - 8000) <= 2500 or (2000 <= z["x"] <= 6000 and 2000 <= z["z"] <= 9000)]
    assert _treffer(WOLF, flaechen) == erwartet and 0 < len(erwartet) < 52


# ── Chirurgisches Entfernen ────────────────────────────────────────────────
@pytest.mark.parametrize("text", [WOLF, HEN])
def test_nichts_entfernen_ist_byteidentisch(text):
    assert bot._tex_entfernen(text, bot._terr_scannen(text), set()) == text


def test_entfernen_streicht_genau_die_zeilen_dieser_zonen():
    scan = bot._terr_scannen(WOLF)
    weg = {3, 10, 11}
    neu = bot._tex_entfernen(WOLF, scan, weg)
    a, b = _zeilen(WOLF), _zeilen(neu)
    assert len(b) == len(a) - 3
    entfernt = [l for l in a if l not in b]
    assert len(entfernt) == 3
    for i in weg:
        zone = scan["zonen"][i]
        assert zone["roh"].strip() in "".join(entfernt)
    rest = [l for l in a if l in b]
    assert rest == b                                                              # alles uebrige unveraendert und in Reihenfolge
    neu_scan = bot._terr_scannen(neu)
    assert len(neu_scan["zonen"]) == 49 and len(neu_scan["bloecke"]) == len(scan["bloecke"])
    assert [bl["farbe"] for bl in neu_scan["bloecke"]] == [bl["farbe"] for bl in scan["bloecke"]]


def test_alle_zonen_entfernen_laesst_bloecke_und_farben_stehen():
    scan = bot._terr_scannen(HEN)
    neu = bot._tex_entfernen(HEN, scan, {z["index"] for z in scan["zonen"]})
    leer = bot._terr_scannen(neu)
    assert leer["zonen"] == [] and [b["farbe"] for b in leer["bloecke"]] == [b["farbe"] for b in scan["bloecke"]]


def test_kommentare_und_andere_bloecke_bleiben():
    text = ('<?xml version="1.0"?>\n<territory-type>\n  <!-- <zone name="X" smin="0" smax="0" dmin="0" dmax="0" x="5" z="5" r="5"/> -->\n'
            '  <territory color="1">\n    <zone name="A" smin="0" smax="0" dmin="0" dmax="0" x="100" z="100" r="50"/>\n'
            '    <!-- Notiz -->\n    <zone name="B" smin="0" smax="0" dmin="0" dmax="0" x="900" z="900" r="50"/>\n  </territory>\n'
            '  <territory color="2">\n    <zone name="C" smin="0" smax="0" dmin="0" dmax="0" x="110" z="110" r="50"/>\n  </territory>\n</territory-type>\n')
    neu = bot._tex_entfernen(text, bot._terr_scannen(text), set(_treffer(text, [_kreis(105, 105, 20)])))
    assert 'name="A"' not in neu and 'name="C"' not in neu and 'name="B"' in neu
    assert '<!-- <zone name="X"' in neu and "<!-- Notiz -->" in neu and neu.count("<territory ") == 2


def test_zone_in_derselben_zeile_verliert_nur_ihr_tag():
    text = ('<territory-type>\n  <territory color="1"><zone name="A" smin="0" smax="0" dmin="0" dmax="0" x="1" z="1" r="1"/>'
            '<zone name="B" smin="0" smax="0" dmin="0" dmax="0" x="500" z="500" r="1"/></territory>\n</territory-type>\n')
    neu = bot._tex_entfernen(text, bot._terr_scannen(text), {0})
    assert neu == ('<territory-type>\n  <territory color="1"><zone name="B" smin="0" smax="0" dmin="0" dmax="0" x="500" z="500" r="1"/>'
                   '</territory>\n</territory-type>\n')


def test_crlf_bleibt_erhalten():
    text = WOLF.replace("\n", "\r\n")
    neu = bot._tex_entfernen(text, bot._terr_scannen(text), {0, 1})
    assert "\r\n" in neu and "\n" not in neu.replace("\r\n", "") and len(bot._terr_scannen(neu)["zonen"]) == 50


def test_ungewoehnliche_werte_in_unberuehrten_zonen_blockieren_nichts():
    """Der Zone Creator prueft alle Zonen neu; hier werden nur Zeilen gestrichen."""
    text = ('<territory-type>\n  <territory color="1">\n'
            '    <zone name="Komisch" smin="5" smax="2" dmin="9" dmax="1" x="999999" z="5" r="-3"/>\n'
            '    <zone name="Weg" smin="0" smax="0" dmin="0" dmax="0" x="10" z="10" r="5"/>\n  </territory>\n</territory-type>\n')
    neu = bot._tex_entfernen(text, bot._terr_scannen(text), {1})
    assert 'name="Komisch"' in neu and 'name="Weg"' not in neu


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
    c.ftp = _FTP({"/mission/env/wolf_territories.xml": WOLF, "/mission/env/hen_territories.xml": HEN,
                  "/mission/cfgenvironment.xml": '<env><territories><file path="env/hen_territories.xml"/></territories></env>'})
    return c


def _aufruf(monkeypatch, c, handler, methode="GET", daten=None, query=""):
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": True, "service_id": c.service_id,
                            "seen": time.time()}
    req = make_mocked_request(methode, "/api/tools/territoryexclusion" + query, headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})

    async def body(_r):
        return daten
    monkeypatch.setattr(bot, "body", body)
    r = asyncio.run(handler(req))
    return r.status, json.loads(r.body)


def _post(monkeypatch, c, **daten):
    return _aufruf(monkeypatch, c, bot.api_tools_territoryexclusion_post, "POST", daten)


FLAECHEN = [_kreis(8000, 8000, 2500), _rechteck(2000, 2000, 6000, 9000)]


def test_get_dateiliste_und_zonen(monkeypatch, conn):
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_territoryexclusion_get)
    d = {t["typ"]: t for t in j["data"]["typen"]}
    assert st == 200 and j["data"]["welt"] == 16384 and j["data"]["karte"] == "Nasdara"
    assert d["hen"]["aktiv"] is True and d["wolf"]["aktiv"] is False and d["wolf"]["vorhanden"] is True
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_territoryexclusion_get, query="?typ=wolf")
    assert st == 200 and len(j["data"]["zonen"]) == 52 and len(j["data"]["bloecke"]) == 8
    assert j["data"]["hash"] == bot._spawn_datei_hash(WOLF) and j["data"]["zonen"][0]["src"] == 0
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_territoryexclusion_get, query="?typ=../../etc")
    assert st == 400
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_territoryexclusion_get, query="?typ=bear")
    assert st == 200 and j["data"]["vorhanden"] is False and j["data"]["zonen"] == []


def test_vorschau_schreibt_nichts_und_zaehlt(monkeypatch, conn):
    erwartet = _treffer(WOLF, FLAECHEN)
    st, j = _post(monkeypatch, conn, typ="wolf", bereiche=FLAECHEN)
    d = j["data"]
    assert st == 200 and conn.ftp.writes == [] and d["backup"] is None and d["unveraendert"] is False
    assert d["aenderung"] == {"vorher": 52, "nachher": 52 - len(erwartet), "entfernt": len(erwartet),
                              "treffer": len(erwartet), "behalten": 0, "flaechen": 2}
    assert len(bot._terr_scannen(d["generated"][0]["content"])["zonen"]) == 52 - len(erwartet)
    assert d["generated"][0]["filename"] == "env/wolf_territories.xml" and sum(d["namen"].values()) == len(erwartet)
    assert d["datei_hash"] == bot._spawn_datei_hash(WOLF)


def test_commit_braucht_den_hash_sichert_und_schreibt(monkeypatch, conn):
    st, _j = _post(monkeypatch, conn, typ="wolf", bereiche=FLAECHEN, commit=True)
    assert st == 409 and conn.ftp.writes == []
    st, _j = _post(monkeypatch, conn, typ="wolf", bereiche=FLAECHEN, commit=True, erwartet_hash="falsch")
    assert st == 409 and conn.ftp.writes == []
    st, j = _post(monkeypatch, conn, typ="wolf", bereiche=FLAECHEN, commit=True, erwartet_hash=bot._spawn_datei_hash(WOLF))
    assert st == 200 and j["data"]["backup"] == "env/wolf_territories.xml.bak"
    assert conn.ftp.writes == ["/mission/env/wolf_territories.xml.bak", "/mission/env/wolf_territories.xml"]
    assert conn.ftp.files["/mission/env/wolf_territories.xml.bak"] == WOLF
    assert conn.ftp.files["/mission/env/wolf_territories.xml"] == j["data"]["generated"][0]["content"]
    assert conn.ftp.files["/mission/env/hen_territories.xml"] == HEN                         # andere Datei unberuehrt
    assert any(e["action"] == "Tool: Territory Exclusion gespeichert" for e in bot._audit_log)


def test_datei_zwischenzeitlich_geaendert(monkeypatch, conn):
    conn.ftp.files["/mission/env/wolf_territories.xml"] = WOLF + " "
    st, _j = _post(monkeypatch, conn, typ="wolf", bereiche=FLAECHEN, commit=True, erwartet_hash=bot._spawn_datei_hash(WOLF))
    assert st == 409 and conn.ftp.writes == []


def test_ohne_treffer_wird_nichts_geschrieben(monkeypatch, conn):
    st, j = _post(monkeypatch, conn, typ="wolf", bereiche=[_kreis(10, 10, 5)], commit=True, erwartet_hash=bot._spawn_datei_hash(WOLF))
    assert st == 200 and j["data"]["unveraendert"] is True and conn.ftp.writes == [] and j["data"]["backup"] is None
    assert j["data"]["generated"][0]["content"] == WOLF
    assert any("Keine Zone" in w for w in j["data"]["warnungen"])


def test_einzelne_zonen_lassen_sich_behalten(monkeypatch, conn):
    treffer = _treffer(WOLF, FLAECHEN)
    behalten = treffer[:2]
    st, j = _post(monkeypatch, conn, typ="wolf", bereiche=FLAECHEN, behalten=behalten)
    a = j["data"]["aenderung"]
    assert st == 200 and a["entfernt"] == len(treffer) - 2 and a["behalten"] == 2 and a["treffer"] == len(treffer)
    verbleibend = bot._terr_scannen(j["data"]["generated"][0]["content"])["zonen"]
    roh_alt = [z["roh"] for z in _zonen(WOLF)]
    for i in behalten:
        assert roh_alt[i] in [z["roh"] for z in verbleibend]
    # Behalten einer Zone ausserhalb der Treffer ist wirkungslos
    st, j2 = _post(monkeypatch, conn, typ="wolf", bereiche=FLAECHEN, behalten=[0] if 0 not in treffer else [51])
    assert j2["data"]["aenderung"]["entfernt"] == len(treffer)


def test_alle_zonen_entfernen_warnt(monkeypatch, conn):
    st, j = _post(monkeypatch, conn, typ="hen", bereiche=[_rechteck(0, 0, WELT, WELT)])
    assert st == 200 and any("ALLE Zonen" in w for w in j["data"]["warnungen"])
    assert bot._terr_scannen(j["data"]["generated"][0]["content"])["zonen"] == []


@pytest.mark.parametrize("daten", [
    {"typ": "wolf"}, {"typ": "wolf", "bereiche": []}, {"typ": "wolf", "bereiche": [_kreis(1, 1, 0)]},
    {"typ": "wolf", "bereiche": [_kreis(-5, 1, 5)]}, {"typ": "../x", "bereiche": [_kreis(1, 1, 5)]},
    {"typ": "wolf", "bereiche": FLAECHEN, "behalten": "x"}, {"typ": "wolf", "bereiche": FLAECHEN, "behalten": [True]},
    {"typ": "wolf", "bereiche": FLAECHEN, "behalten": ["1"]},
])
def test_ungueltige_eingaben_ohne_schreiben(monkeypatch, conn, daten):
    st, j = _post(monkeypatch, conn, **daten)
    assert st == 400 and conn.ftp.writes == []


def test_fehlende_datei_und_kaputte_datei(monkeypatch, conn):
    st, j = _post(monkeypatch, conn, typ="bear", bereiche=FLAECHEN)
    assert st == 404 and "nichts zu entfernen" in j["error"]
    conn.ftp.files["/mission/env/wolf_territories.xml"] = "<territory-type><territory color='1'>"
    st, j = _post(monkeypatch, conn, typ="wolf", bereiche=FLAECHEN)
    assert st == 422 and conn.ftp.writes == []


def test_scheitert_das_schreiben_bleibt_die_datei_wie_sie_war(monkeypatch, conn):
    conn.ftp.fehlschlag = {"/mission/env/wolf_territories.xml"}
    st, j = _post(monkeypatch, conn, typ="wolf", bereiche=FLAECHEN, commit=True, erwartet_hash=bot._spawn_datei_hash(WOLF))
    assert st == 502 and conn.ftp.files["/mission/env/wolf_territories.xml"] == WOLF


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
    st, j = _aufruf(monkeypatch, reg._conns["2000"], bot.api_tools_territoryexclusion_get, query="?typ=wolf")
    assert st == 200 and j["data"]["vorhanden"] is False and j["data"]["zonen"] == [] and j["data"]["welt"] == 12800
    st, j = _post(monkeypatch, reg._conns["2000"], typ="wolf", bereiche=FLAECHEN, commit=True, erwartet_hash=bot._spawn_datei_hash(""))
    assert st == 404 and conn.ftp.writes == [] and reg._conns["2000"].ftp.writes == []
    # Livonia ist kleiner: eine Flaeche ausserhalb von 12800 m wird dort abgelehnt, auf Nasdara nicht
    st, _j = _post(monkeypatch, reg._conns["2000"], typ="wolf", bereiche=[_kreis(14000, 14000, 50)])
    assert st == 400
    st, _j = _post(monkeypatch, conn, typ="wolf", bereiche=[_kreis(14000, 14000, 50)])
    assert st == 200


def test_modul_ist_registriert():
    assert bot.FEATURE_MODULES["tools.territoryexclusion"]["label"] == "Territory Exclusion"
    assert ("territoryexclusion", "✂️", "Territory Exclusion") in bot._TOOL_LISTE
