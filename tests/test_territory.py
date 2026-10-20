"""Territory Zone Creator: chirurgisches Schreiben der env/*_territories.xml (echte Bohemia-Dateien von Nasdara)."""
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

FIX = os.path.join(REPO, "tests", "fixtures", "territory")


def _datei(name):
    with open(os.path.join(FIX, name), encoding="utf-8", newline="") as f:
        return f.read()


WOLF = _datei("nasdara_wolf_territories.xml")
HEN = _datei("nasdara_hen_territories.xml")
WELT = 16384


def _ui(text):
    return bot._terr_zonen_fuer_oberflaeche(bot._terr_scannen(text))


def _zeilen(text):
    return text.split("\n")


# ── Scannen ────────────────────────────────────────────────────────────────
def test_scan_echte_datei_bloecke_zonen_und_farben():
    s = bot._terr_scannen(WOLF)
    assert len(s["bloecke"]) == 8 and len(s["zonen"]) == 52
    assert s["bloecke"][0]["farbe"] == "2729642808" and len(s["bloecke"][0]["zonen"]) >= 5
    z = s["zonen"][0]
    assert (z["name"], z["x"], z["z"], z["r"]) == ("HuntingGround", 8901.07, 7709.6, 210.0)
    assert sum(len(b["zonen"]) for b in s["bloecke"]) == 52


def test_kommentare_werden_nicht_gescannt_und_bleiben_erhalten():
    text = ('<?xml version="1.0"?>\n<territory-type>\n  <!-- <territory color="9"> <zone name="X" smin="0" smax="0" dmin="0" dmax="0" x="1" z="1" r="1"/> -->\n'
            '  <territory color="5">\n    <zone name="Graze" smin="0" smax="0" dmin="0" dmax="0" x="100" z="200" r="50"/>\n  </territory>\n</territory-type>\n')
    s = bot._terr_scannen(text)
    assert len(s["bloecke"]) == 1 and len(s["zonen"]) == 1
    ui = bot._terr_zonen_fuer_oberflaeche(s)
    ui[0]["r"] = 60
    neu = bot._terr_anwenden(text, ui, WELT)["text"]
    assert "<!-- <territory color=\"9\">" in neu and 'r="60"' in neu


@pytest.mark.parametrize("kaputt", [
    "<territory-type><territory color='1'><zone name='a' x='1' z='1' r='1' smin='0' smax='0' dmin='0' dmax='0'></zone></territory></territory-type>",
    "<anders></anders>",
    "<territory-type><zone name='a' smin='0' smax='0' dmin='0' dmax='0' x='1' z='1' r='1'/></territory-type>",
    "<territory-type><territory color='1'><zone name='a' x='1'/></territory></territory-type>",
    "nicht xml",
])
def test_unbrauchbare_dateien_werden_abgelehnt(kaputt):
    with pytest.raises(ValueError):
        bot._terr_scannen(kaputt)


# ── Chirurgisches Schreiben ────────────────────────────────────────────────
@pytest.mark.parametrize("text", [WOLF, HEN])
def test_unveraendert_gespeichert_ist_zeichen_fuer_zeichen_identisch(text):
    r = bot._terr_anwenden(text, _ui(text), WELT)
    assert r["text"] == text and r["unveraendert"] is True
    assert (r["neu"], r["geaendert"], r["entfernt"]) == (0, 0, 0)


def test_eine_zone_aendern_beruehrt_nur_diese_zeile():
    ui = _ui(WOLF)
    ui[3]["r"] = 123.5
    ui[3]["x"] = 9000
    r = bot._terr_anwenden(WOLF, ui, WELT)
    a, b = _zeilen(WOLF), _zeilen(r["text"])
    assert len(a) == len(b)
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    assert len(diff) == 1 and 'x="9000"' in b[diff[0]] and 'r="123.5"' in b[diff[0]]
    assert 'name="Rest"' in b[diff[0]] and r["geaendert"] == 1


def test_zone_loeschen_entfernt_genau_eine_zeile():
    ui = _ui(WOLF)
    weg = ui.pop(10)
    r = bot._terr_anwenden(WOLF, ui, WELT)
    a, b = _zeilen(WOLF), _zeilen(r["text"])
    assert len(b) == len(a) - 1 and r["entfernt"] == 1
    gesucht = f'x="{bot._terr_zahl(weg["x"])}" z="{bot._terr_zahl(weg["z"])}"'
    fehlend = [l for l in a if l not in b]
    assert len(fehlend) == 1 and gesucht in fehlend[0]


def test_neue_zone_landet_am_ende_des_gewaehlten_blocks_mit_gleichem_einzug():
    ui = _ui(WOLF)
    ui.append({"src": None, "block": 2, "name": "HuntingGround", "x": 5000.25, "z": 6000, "r": 150,
               "smin": 0, "smax": 0, "dmin": 0, "dmax": 0})
    r = bot._terr_anwenden(WOLF, ui, WELT)
    assert r["neu"] == 1
    neu = bot._terr_scannen(r["text"])
    assert len(neu["bloecke"]) == 8 and len(neu["bloecke"][2]["zonen"]) == len(bot._terr_scannen(WOLF)["bloecke"][2]["zonen"]) + 1
    letzte = neu["zonen"][neu["bloecke"][2]["zonen"][-1]]
    assert (letzte["x"], letzte["z"], letzte["r"]) == (5000.25, 6000.0, 150.0) and letzte["einzug"] == "        "
    # alles andere unveraendert: ohne die eine neue Zeile ist der Text derselbe
    zeilen_neu = [l for l in _zeilen(r["text"]) if 'x="5000.25"' not in l]
    assert zeilen_neu == _zeilen(WOLF)


def test_nummern_werden_wie_bei_bohemia_formatiert():
    assert bot._terr_zahl(4473.14) == "4473.14" and bot._terr_zahl(80.0) == "80" and bot._terr_zahl(1.5) == "1.5"
    assert bot._terr_zahl(100.126) == "100.13"


def test_crlf_bleibt_erhalten():
    text = WOLF.replace("\n", "\r\n")
    ui = _ui(text)
    ui.append({"src": None, "block": 0, "name": "Rest", "x": 1, "z": 2, "r": 50, "smin": 0, "smax": 0, "dmin": 0, "dmax": 0})
    neu = bot._terr_anwenden(text, ui, WELT)["text"]
    assert "\r\n" in neu and "\n" not in neu.replace("\r\n", "")


def test_fehlende_datei_wird_neu_angelegt():
    z = {"src": None, "name": "InfectedVillage", "x": 100, "z": 200, "r": 60, "smin": 0, "smax": 0, "dmin": 4, "dmax": 6}
    r = bot._terr_anwenden(None, [z], WELT)
    assert r["vorher"] == 0 and r["nachher"] == 1
    root = ET.fromstring(r["text"])
    assert root.find("territory/zone").get("dmax") == "6"


def test_tag_aendern_behaelt_unbekannte_attribute_und_reihenfolge():
    roh = '<zone  name="A"   foo="bar" x="1" z="2" r="3" smin="0" smax="0" dmin="0" dmax="0" />'
    a = {"name": "A", "x": 1, "z": 2, "r": 3, "smin": 0, "smax": 0, "dmin": 0, "dmax": 0}
    n = dict(a, r=9, name="B")
    out = bot._terr_tag_aendern(roh, n, a)
    assert out == '<zone  name="B"   foo="bar" x="1" z="2" r="9" smin="0" smax="0" dmin="0" dmax="0" />'


# ── Pruefung ───────────────────────────────────────────────────────────────
BASIS = {"src": None, "name": "Graze", "x": 100, "z": 100, "r": 100, "smin": 0, "smax": 0, "dmin": 0, "dmax": 0}


@pytest.mark.parametrize("aenderung,stichwort", [
    ({"dmin": 9, "dmax": 2}, "Minimum"),
    ({"smin": 3, "smax": 1}, "Minimum"),
    ({"r": 0}, "Radius"), ({"r": -5}, "Radius"), ({"r": 99999}, "Radius"),
    ({"x": -1}, "außerhalb"), ({"z": WELT + 1}, "außerhalb"),
    ({"x": float("inf")}, "gültige Zahl"), ({"x": "abc"}, "keine Zahl"),
    ({"name": ""}, "ungültig"), ({"name": 'a"b'}, "ungültig"), ({"name": "<x>"}, "ungültig"),
    ({"dmax": 2.5}, "ganze Zahl"), ({"dmax": 5000}, "ganze Zahl"),
])
def test_ungueltige_zonen_werden_abgelehnt_statt_still_korrigiert(aenderung, stichwort):
    with pytest.raises(ValueError, match=stichwort):
        bot._terr_anwenden(WOLF, _ui(WOLF) + [dict(BASIS, **aenderung)], WELT)


def test_liste_passt_nicht_zur_datei():
    ui = _ui(WOLF)
    ui[0]["src"] = 999
    with pytest.raises(ValueError, match="neu laden"):
        bot._terr_anwenden(WOLF, ui, WELT)
    ui = _ui(WOLF)
    ui[1]["src"] = ui[0]["src"]
    with pytest.raises(ValueError, match="neu laden"):
        bot._terr_anwenden(WOLF, ui, WELT)


def test_warnungen():
    zonen = [dict(BASIS, name="Tippfehler", r=900), dict(BASIS), dict(BASIS)]
    w = bot._terr_warnungen(zonen, {"Graze"}, None)
    text = " ".join(w)
    assert "Tippfehler" in text and "500 m" in text and "übereinander" in text
    assert bot._terr_warnungen([dict(BASIS)], {"Graze"}, None) == []


def test_typen_aus_cfgenvironment_und_ordner():
    cfg = '<env><!-- <file path="env/wolf_territories.xml"/> --><territories><file path="env/zombie_territories.xml"/><file path="env/goat_territories.xml"/></territories></env>'
    t = bot._terr_typen_aus_env(cfg, ["/m/env/zombie_territories.xml", "/m/env/wolf_territories.xml", "/m/env/readme.txt"])
    d = {x["typ"]: x for x in t}
    assert list(d) == ["zombie", "goat", "wolf"]
    assert d["zombie"]["aktiv"] is True and d["goat"]["aktiv"] is True and d["goat"]["vorhanden"] is False
    assert d["wolf"]["aktiv"] is False and d["wolf"]["vorhanden"] is True       # Datei da, aber nicht eingebunden
    assert d["goat"]["de"] == "Ziegen"


# ── Endpunkte ──────────────────────────────────────────────────────────────
class _FTP:
    def __init__(self, dateien):
        self.files = dict(dateien)
        self.writes = []

    def list_dir(self, d):
        d = d.rstrip("/") + "/"
        return sorted(p for p in self.files if p.startswith(d))

    def read_file_ex(self, p):
        return (self.files[p], "ok") if p in self.files else (None, "missing")

    def write_file(self, p, inhalt):
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
    c = reg._conns["1000"]
    c.ftp = _FTP({"/mission/env/wolf_territories.xml": WOLF, "/mission/env/hen_territories.xml": HEN,
                  "/mission/cfgenvironment.xml": '<env><territories><file path="env/hen_territories.xml"/></territories></env>'})
    return c


def _aufruf(monkeypatch, c, handler, methode="GET", daten=None, query=""):
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": True, "service_id": c.service_id,
                            "seen": time.time()}
    req = make_mocked_request(methode, "/api/tools/territory" + query, headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})

    async def body(_r):
        return daten
    monkeypatch.setattr(bot, "body", body)
    r = asyncio.run(handler(req))
    return r.status, json.loads(r.body)


def test_get_typen_und_zonen(monkeypatch, conn):
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_territory_get)
    assert st == 200 and j["data"]["welt"] == 16384 and j["data"]["karte"] == "Nasdara"
    d = {t["typ"]: t for t in j["data"]["typen"]}
    assert d["hen"]["aktiv"] is True and d["wolf"]["aktiv"] is False and d["wolf"]["vorhanden"] is True
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_territory_get, query="?typ=wolf")
    assert st == 200 and len(j["data"]["zonen"]) == 52 and len(j["data"]["bloecke"]) == 8
    assert j["data"]["zonen"][0]["src"] == 0 and j["data"]["hash"]
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_territory_get, query="?typ=../../etc")
    assert st == 400


def test_vorschau_schreibt_nichts_und_commit_braucht_den_hash(monkeypatch, conn):
    _, j = _aufruf(monkeypatch, conn, bot.api_tools_territory_get, query="?typ=wolf")
    zonen = j["data"]["zonen"]
    h = j["data"]["hash"]
    zonen[0]["r"] = 77
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_territory_post, "POST", {"typ": "wolf", "zonen": zonen})
    assert st == 200 and j["data"]["aenderung"]["geaendert"] == 1 and conn.ftp.writes == []
    assert 'r="77"' in j["data"]["generated"][0]["content"] and j["data"]["backup"] is None
    # commit ohne/mit falschem Hash
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_territory_post, "POST", {"typ": "wolf", "zonen": zonen, "commit": True})
    assert st == 409 and conn.ftp.writes == []
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_territory_post, "POST",
                    {"typ": "wolf", "zonen": zonen, "commit": True, "erwartet_hash": h})
    assert st == 200 and j["data"]["backup"] == "env/wolf_territories.xml.bak"
    assert conn.ftp.writes == ["/mission/env/wolf_territories.xml.bak", "/mission/env/wolf_territories.xml"]
    assert conn.ftp.files["/mission/env/wolf_territories.xml.bak"] == WOLF
    assert conn.ftp.files["/mission/env/wolf_territories.xml"].count('r="77"') == 1


def test_commit_ohne_aenderung_schreibt_nichts(monkeypatch, conn):
    _, j = _aufruf(monkeypatch, conn, bot.api_tools_territory_get, query="?typ=hen")
    st, j2 = _aufruf(monkeypatch, conn, bot.api_tools_territory_post, "POST",
                     {"typ": "hen", "zonen": j["data"]["zonen"], "commit": True, "erwartet_hash": j["data"]["hash"]})
    assert st == 200 and j2["data"]["unveraendert"] is True and conn.ftp.writes == []


def test_fehler_wird_ohne_schreiben_gemeldet(monkeypatch, conn):
    _, j = _aufruf(monkeypatch, conn, bot.api_tools_territory_get, query="?typ=wolf")
    zonen = j["data"]["zonen"]
    zonen[0]["dmin"], zonen[0]["dmax"] = 9, 2
    st, j2 = _aufruf(monkeypatch, conn, bot.api_tools_territory_post, "POST",
                     {"typ": "wolf", "zonen": zonen, "commit": True, "erwartet_hash": j["data"]["hash"]})
    assert st == 400 and "Minimum" in j2["error"] and conn.ftp.writes == []


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
    st, j = _aufruf(monkeypatch, reg._conns["2000"], bot.api_tools_territory_get, query="?typ=wolf")
    assert st == 200 and j["data"]["vorhanden"] is False and j["data"]["zonen"] == [] and j["data"]["welt"] == 12800
