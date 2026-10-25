"""Visual SpawnableTypes Configurator mit Brigardes echter Serverdatei (730 Eintraege) und dem Item-Katalog.

Die Datei hat Eigenheiten, an denen die Webseite doordiehub.com/VisualSpawnableTypes scheitert (Presets an <attachments>,
Chancen an Cargo-Items, Kommentare, verschachtelte Items) und 12 kaputte Zahlenwerte ("1 00", "1 o0"). Das Tool muss sie
unveraendert lassen, solange der Eintrag nicht angefasst wird.

    python3 -m pytest tests/test_spawnabletypes_echte_datei.py -q
"""
import asyncio
import json
import os
import re
import sys
import time

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

with open(os.path.join(REPO, "tests", "fixtures", "cfgspawnabletypes-brigarde-43.xml"), encoding="utf-8", newline="") as _f:
    TEXT = _f.read()
SCAN = bot._spt_scannen(TEXT)
TYPEN = SCAN["typen"]
KAPUTT = ["CivilianSedan", "CivilianSedan_Wine", "Hatchback_02_Blue", "Motorbike_01_Yellow", "OffroadHatchback_White",
          "Sedan_02", "Sedan_02_Red"]


def _eintrag(name):
    return next(t for t in TYPEN if t["name"] == name)


def _teile(name):
    return json.loads(json.dumps(_eintrag(name)["teile"]))


def _zeilen(text):
    return text.split("\n")


def _unterschiede(vorher, nachher):
    """Zeilennummern (1-basiert, im Original) die sich unterscheiden - plus Anzahl hinzugekommener/weggefallener Zeilen."""
    import difflib
    a, b = _zeilen(vorher), _zeilen(nachher)
    geaendert = set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            geaendert.update(range(i1 + 1, max(i2, i1 + 1) + 1))
    return sorted(geaendert), len(b) - len(a)


def _bereich(name):
    """Erste und letzte Zeile (1-basiert) des <type>-Blocks im Original."""
    zeilen = _zeilen(TEXT)
    start = next(i for i, z in enumerate(zeilen, 1) if re.match(r'\s*<type name="%s"[\s>]' % re.escape(name), z))
    ende = next(i for i in range(start, len(zeilen) + 1) if "</type>" in zeilen[i - 1] or zeilen[start - 1].rstrip().endswith("/>"))
    return start, ende


def _anwenden(geaendert=(), geloescht=(), neu=()):
    return bot._spt_anwenden(TEXT, list(geaendert), list(geloescht), list(neu), None)


# ── Lesen ─────────────────────────────────────────────────────────────────
def test_datei_wird_vollstaendig_gelesen():
    assert len(TYPEN) == 730 and SCAN["einheit"] == "\t"
    assert sorted(t["name"] for t in TYPEN if t["gesperrt"]) == ["MakarovIJ70", "StorageBox_Wooden"]
    praes_att = sum(1 for t in TYPEN for g in t["teile"] if g["art"] == "attachments" and g.get("preset"))
    praes_cargo = sum(1 for t in TYPEN for g in t["teile"] if g["art"] == "cargo" and g.get("preset"))
    cargo_chancen = sum(1 for t in TYPEN for g in t["teile"] if g["art"] == "cargo" for i in g["items"] if i["chance"] is not None)
    # 80 Cargo-Items mit Chance stehen in der Datei, 2 davon in den beiden schreibgeschuetzten Eintraegen (nicht gelesen)
    assert (praes_att, praes_cargo, cargo_chancen) == (604, 963, 78)


def test_presets_und_cargo_chancen_stehen_im_original_wortlaut(monkeypatch):
    t = _teile("ZmbM_PatrolNormal_Autumn")
    assert [(g["art"], g["preset"]) for g in t] == [("cargo", "foodArmy"), ("cargo", "ammoArmy"), ("attachments", "headtorches"),
                                                     ("attachments", "hatsArmy"), ("attachments", "bagsArmy"), ("attachments", "vestsArmy")]
    assert _teile("Bear_Beige")[0] == {"art": "cargo", "chance": "0.00", "preset": None,
                                       "items": [{"name": "Paper", "chance": "0.50"}, {"name": "Rag", "chance": "0.50"}]}


# ── Verlustfreiheit ───────────────────────────────────────────────────────
def test_ohne_aenderung_ist_die_ausgabe_zeichengleich():
    r = _anwenden()
    assert r["unveraendert"] is True and r["text"] == TEXT


def test_unveraenderter_eintrag_als_aenderung_gesendet_aendert_nichts():
    """Die Oberflaeche sendet nur wirklich geaenderte Eintraege - aber auch ein gleicher Eintrag darf nichts umschreiben."""
    e = _eintrag("ZmbM_PatrolNormal_Autumn")
    r = _anwenden([{"src": e["index"], "name": e["name"], "teile": _teile(e["name"])}])
    assert r["text"] == TEXT


@pytest.mark.parametrize("name, aendere", [
    ("Barrel_Blue", lambda t: t.clear()),                                                       # Hoarder weg
    ("FirstAidKit", lambda t: [x.__setitem__("name", "floor") for x in t if x["art"] == "tag"]),
    ("PlateCarrierVest_Black", lambda t: [x.__setitem__("min", "0.2") for x in t if x["art"] == "damage"]),
    ("PlateCarrierVest_Black", lambda t: [x.__setitem__("chance", "0.90") for x in t if x["art"] == "attachments"]),
    ("ZmbM_PatrolNormal_Autumn", lambda t: [x.__setitem__("preset", "hatsPolice") for x in t if x["art"] == "attachments"][:1]),
    ("Bear_Beige", lambda t: t[0]["items"].append({"name": "Rag", "chance": "0.10"})),
    ("FirstAidKit", lambda t: t.append({"art": "cargo", "chance": "0.5", "preset": None, "items": [{"name": "Rag", "chance": None}]})),
])
def test_eine_aenderung_beruehrt_nur_diesen_eintrag(name, aendere):
    e = _eintrag(name)
    teile = _teile(name)
    aendere(teile)
    r = _anwenden([{"src": e["index"], "name": name, "teile": teile}])
    assert r["text"] != TEXT and r["geaendert"] == 1
    zeilen, _diff = _unterschiede(TEXT, r["text"])
    start, ende = _bereich(name)
    assert zeilen and all(start <= z <= ende for z in zeilen), (name, zeilen[:5], start, ende)
    # alle anderen Eintraege lesen sich danach genauso wie vorher (Presets, Chancen, Namen, Reihenfolge)
    neu = bot._spt_scannen(r["text"])["typen"]
    assert len(neu) == 730
    for a, b in zip(TYPEN, neu):
        if a["name"] != name:
            assert (a["name"], a["teile"], a["gesperrt"]) == (b["name"], b["teile"], b["gesperrt"])
    assert r["text"].count("<!--") == TEXT.count("<!--") == 23


def test_loeschen_neu_und_kopieren():
    e = _eintrag("SparkPlug")
    r = _anwenden(geloescht=[e["index"]])
    assert r["geloescht"] == 1 and '<type name="SparkPlug"' not in r["text"] and len(bot._spt_scannen(r["text"])["typen"]) == 729
    kopie = _teile("PlateCarrierVest_Black")
    r = _anwenden(neu=[{"name": "PlateCarrierVest_Test", "teile": kopie}])
    neu = bot._spt_scannen(r["text"])["typen"]
    assert len(neu) == 731 and neu[-1]["name"] == "PlateCarrierVest_Test" and neu[-1]["teile"] == kopie
    assert r["text"].startswith(TEXT[:TEXT.rindex("</spawnabletypes>")].rstrip("\n"))        # alles davor unberuehrt


def test_schreibgeschuetzte_eintraege_bleiben_unveraendert():
    for name in ("MakarovIJ70", "StorageBox_Wooden"):
        e = _eintrag(name)
        assert e["gesperrt"]
        with pytest.raises(ValueError):
            _anwenden([{"src": e["index"], "name": name, "teile": []}])
    # loeschen ist erlaubt
    r = _anwenden(geloescht=[_eintrag("StorageBox_Wooden")["index"]])
    assert '<type name="StorageBox_Wooden"' not in r["text"]


# ── Kaputte Zahlen (1 00 / 1 o0) ─────────────────────────────────────────
def _kaputte_werte():
    out = []
    for t in TYPEN:
        for g in t["teile"]:
            if g["art"] in ("attachments", "cargo"):
                for v in [g.get("chance")] + [i.get("chance") for i in g["items"]]:
                    if v is not None and not re.fullmatch(r"\d+(\.\d+)?", v):
                        out.append((t["name"], v))
            elif g["art"] == "damage":
                for v in (g["min"], g["max"]):
                    if v is not None and not re.fullmatch(r"\d+(\.\d+)?", v):
                        out.append((t["name"], v))
    return out


def test_zwoelf_kaputte_werte_in_sieben_fahrzeugen_werden_unveraendert_gelesen():
    kaputt = _kaputte_werte()
    assert len(kaputt) == 12 and sorted({n for n, _ in kaputt}) == sorted(KAPUTT)
    assert sorted(v for _n, v in kaputt) == ["1 00"] * 11 + ["1 o0"]


def test_aenderung_an_anderem_eintrag_fasst_kaputte_werte_nicht_an():
    e = _eintrag("PlateCarrierVest_Black")
    teile = _teile("PlateCarrierVest_Black")
    teile[0]["chance"] = "0.90"
    r = _anwenden([{"src": e["index"], "name": e["name"], "teile": teile}])
    assert r["text"].count('chance="1 00"') == TEXT.count('chance="1 00"') == 11 and 'chance="1 o0"' in r["text"]


def test_kaputter_eintrag_wird_vom_server_abgelehnt_aber_nicht_still_veraendert():
    e = _eintrag("CivilianSedan")
    teile = _teile("CivilianSedan")
    with pytest.raises(ValueError) as fehler:
        bot._spt_teile_pruefen("CivilianSedan", teile)
    assert "keine Zahl" in str(fehler.value)
    assert e["name"] == "CivilianSedan"


def test_korrigierter_wert_wird_gespeichert_und_nur_er():
    e = _eintrag("CivilianSedan")
    teile = _teile("CivilianSedan")
    for g in teile:
        if g["art"] in ("attachments", "cargo"):
            if g.get("chance") == "1 00":
                g["chance"] = "1.00"
            for i in g["items"]:
                if i.get("chance") in ("1 00", "1 o0"):
                    i["chance"] = "1.00"
    bot._spt_teile_pruefen("CivilianSedan", teile)                                            # jetzt gueltig
    r = _anwenden([{"src": e["index"], "name": "CivilianSedan", "teile": teile}])
    zeilen, _ = _unterschiede(TEXT, r["text"])
    start, ende = _bereich("CivilianSedan")
    assert zeilen and all(start <= z <= ende for z in zeilen)
    block = "\n".join(_zeilen(r["text"])[start - 1:ende])
    assert "1 00" not in block and "1 o0" not in block


# ── Item-Katalog ─────────────────────────────────────────────────────────
TYPES = ('<types>\n<type name="Apple"><category name="food"/></type>\n<type name="AKM"><category name="weapons"/></type>\n'
         '<type name="Mag_AKM_30Rnd"><category name="weapons"/></type>\n<type name="ZmbM_X"></type>\n'
         '<type name="bad name!"><category name="food"/></type>\n<type name="Banana"><category name="food"/></type></types>')


def test_katalog_gruppiert_nach_kategorie_und_sortiert():
    k = bot._spt_katalog_lesen(TYPES)
    assert [g["name"] for g in k] == ["food", "weapons", ""]                                   # ohne Kategorie zuletzt
    assert k[0]["items"] == ["Apple", "Banana"] and k[1]["items"] == ["AKM", "Mag_AKM_30Rnd"] and k[2]["items"] == ["ZmbM_X"]


@pytest.mark.parametrize("text", [None, "", "<types><type", "<anderes/>"])
def test_katalog_ohne_lesbare_datei_ist_none(text):
    assert bot._spt_katalog_lesen(text) is None


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
def server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    flat = {"1000": {"service_id": "1000", "nitrado_token": "fake", "owner_discord_id": "1", "map_name": "Nasdara", "ftp_mission_dir": "/mission"},
            "2000": {"service_id": "2000", "nitrado_token": "fake", "owner_discord_id": "2", "map_name": "Livonia", "ftp_mission_dir": "/andere"}}
    (tmp_path / "connections.json").write_text(json.dumps(flat))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    reg._conns["1000"].ftp = _FTP({"/mission/cfgspawnabletypes.xml": TEXT, "/mission/db/types.xml": TYPES})
    reg._conns["2000"].ftp = _FTP({"/andere/db/types.xml": '<types><type name="NurLivonia"><category name="tools"/></type></types>'})
    return reg


def _get(monkeypatch, conn, query=""):
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": True, "service_id": conn.service_id, "seen": time.time()}
    req = make_mocked_request("GET", "/api/tools/spawnabletypes" + query, headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    r = asyncio.run(bot.api_tools_spawnabletypes_get(req))
    return r.status, json.loads(r.body)


def test_endpunkt_liefert_den_katalog_des_eigenen_servers(monkeypatch, server):
    st, j = _get(monkeypatch, server._conns["1000"], "?katalog=1")
    d = j["data"]["katalog"]
    assert st == 200 and d["gelesen"] is True and [g["name"] for g in d["kategorien"]] == ["food", "weapons", ""]
    assert "eintraege" not in j["data"]                                                       # liest dabei nicht die grosse Datei
    st, j = _get(monkeypatch, server._conns["2000"], "?katalog=1")
    assert [g["items"] for g in j["data"]["katalog"]["kategorien"]] == [["NurLivonia"]]       # nie die Items eines anderen Kunden


def test_endpunkt_ohne_types_xml_meldet_nicht_gelesen(monkeypatch, server):
    del server._conns["1000"].ftp.files["/mission/db/types.xml"]
    st, j = _get(monkeypatch, server._conns["1000"], "?katalog=1")
    assert st == 200 and j["data"]["katalog"] == {"gelesen": False, "kategorien": []}


def test_normaler_aufruf_bleibt_unveraendert(monkeypatch, server):
    st, j = _get(monkeypatch, server._conns["1000"])
    assert st == 200 and len(j["data"]["eintraege"]) == 730 and "katalog" not in j["data"]
