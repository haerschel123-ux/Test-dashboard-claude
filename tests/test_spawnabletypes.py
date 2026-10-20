"""Visual SpawnableTypes Configurator: chirurgisches Schreiben der cfgspawnabletypes.xml (echte Bohemia-nahe Datei)."""
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

with open(os.path.join(REPO, "tests", "fixtures", "cfgspawnabletypes-chernarusplus.xml"), encoding="utf-8", newline="") as _f:
    TEXT = _f.read()
SCAN = bot._spt_scannen(TEXT)
TYPEN = SCAN["typen"]


def _idx(name, nr=0):
    return [t["index"] for t in TYPEN if t["name"] == name][nr]


def _teile(name, nr=0):
    return json.loads(json.dumps(TYPEN[_idx(name, nr)]["teile"]))


def _zeilen(t):
    return t.split("\n")


# ── Scannen ────────────────────────────────────────────────────────────────
def test_scan_zaehlt_aktive_eintraege_ohne_auskommentierte():
    assert len(TYPEN) == 584 and SCAN["einheit"] == "\t" and SCAN["selbst"] == " />"
    gesperrt = [t["name"] for t in TYPEN if t["gesperrt"]]
    assert gesperrt == ["MakarovIJ70"] and "Kommentar" in TYPEN[_idx("MakarovIJ70")]["gesperrt"]


def test_preset_gruppen_und_chancen_werden_im_original_wortlaut_gelesen():
    t = _teile("AssaultBag_Black")
    assert t[0] == {"art": "cargo", "chance": None, "preset": "mixArmy", "items": []} and len(t) == 3
    teile = _teile("MiniDress_Pink")
    assert teile[0]["chance"] == "0.10" and teile[0]["items"][0] == {"name": "Ammo_9x19", "chance": "0.20"}
    assert teile[1]["items"] == [{"name": "Derringer_Pink", "chance": None}]


DUP = ('<?xml version="1.0"?>\n<spawnabletypes>\n\t<type name="Scout">\n\t\t<hoarder />\n\t</type>\n'
       '\t<type name="Scout">\n\t\t<tag name="shelves" />\n\t</type>\n</spawnabletypes>\n')


def test_doppelte_namen_bleiben_getrennte_eintraege():
    s = bot._spt_scannen(DUP)["typen"]
    assert [t["name"] for t in s] == ["Scout", "Scout"] and s[0]["teile"] != s[1]["teile"]
    # das zweite wird geaendert, das erste bleibt unberuehrt
    r = bot._spt_anwenden(DUP, [{"src": 1, "name": "Scout", "teile": [{"art": "tag", "name": "floor"}]}], [], [])
    neu = bot._spt_scannen(r["text"])["typen"]
    assert neu[0]["teile"] == [{"art": "hoarder"}] and neu[1]["teile"] == [{"art": "tag", "name": "floor"}]
    assert r["text"].startswith(DUP[:DUP.index('<type name="Scout">', 30)])
    # einen Namen auf einen zweiten Doppelten umbenennen ist nicht erlaubt, ein bestehendes Duplikat zu belassen schon
    r2 = bot._spt_anwenden(DUP, [{"src": 0, "name": "Scout", "teile": [{"art": "hoarder"}, {"art": "tag", "name": "floor"}]}], [], [])
    assert r2["geaendert"] == 1


@pytest.mark.parametrize("kaputt", ["nicht xml", "<anders/>", "<spawnabletypes><type name='x'><unbekannt/></type></spawnabletypes><x"])
def test_unbrauchbare_dateien_werden_abgelehnt(kaputt):
    with pytest.raises(ValueError):
        bot._spt_scannen(kaputt)


def test_unbekanntes_und_verschachteltes_macht_den_eintrag_schreibgeschuetzt():
    xml = ('<spawnabletypes>\n\t<type name="A">\n\t\t<cargo chance="1">\n\t\t\t<cargo chance="1"><item name="X"/></cargo>\n\t\t</cargo>\n\t</type>\n'
           '\t<type name="B" extra="1">\n\t\t<unique />\n\t</type>\n\t<type name="C">\n\t\t<attachments chance="1"><item name="X" quantity="3"/></attachments>\n\t</type>\n'
           '\t<type name="D">\n\t\t<hoarder />\n\t</type>\n</spawnabletypes>\n')
    s = bot._spt_scannen(xml)["typen"]
    assert [bool(t["gesperrt"]) for t in s] == [True, True, True, False]


# ── Chirurgisches Schreiben ────────────────────────────────────────────────
def test_ohne_aenderung_bleibt_alles_identisch():
    r = bot._spt_anwenden(TEXT, [], [], [])
    assert r["text"] == TEXT and r["unveraendert"] is True


def test_unveraendert_zurueckgeschickte_eintraege_aendern_nichts():
    alle = [{"src": t["index"], "name": t["name"], "teile": t["teile"]} for t in TYPEN if not t["gesperrt"]]
    r = bot._spt_anwenden(TEXT, alle, [], [])
    assert r["text"] == TEXT and r["geaendert"] == 0


def test_eine_chance_aendern_beruehrt_nur_diesen_eintrag():
    teile = _teile("MiniDress_Pink")
    teile[0]["items"][0]["chance"] = "0.75"
    r = bot._spt_anwenden(TEXT, [{"src": _idx("MiniDress_Pink"), "name": "MiniDress_Pink", "teile": teile}], [], [])
    a, b = _zeilen(TEXT), _zeilen(r["text"])
    unterschied = [(x, y) for x, y in zip(a, b) if x != y]
    assert r["geaendert"] == 1 and len(a) + 2 == len(b)          # die Einzeiler-Gruppe wird sauber auf drei Zeilen ausgeschrieben
    assert any('chance="0.75"' in y for _x, y in unterschied)
    # ausserhalb des Eintrags: Anfang und Ende der Datei Byte fuer Byte gleich
    i = TEXT.index('<type name="MiniDress_Pink">')
    j = TEXT.index("</type>", i) + len("</type>")
    assert r["text"].startswith(TEXT[:i]) and r["text"].endswith(TEXT[j:])
    assert ET.fromstring(r["text"]) is not None


def test_geaenderter_eintrag_behaelt_preset_und_weggelassene_attribute():
    teile = _teile("AssaultBag_Black")
    teile.append({"art": "hoarder"})
    r = bot._spt_anwenden(TEXT, [{"src": _idx("AssaultBag_Black"), "name": "AssaultBag_Black", "teile": teile}], [], [])
    neu = bot._spt_scannen(r["text"])["typen"][_idx("AssaultBag_Black")]
    assert neu["teile"][0] == {"art": "cargo", "chance": None, "preset": "mixArmy", "items": []}
    assert '<cargo preset="mixArmy" />' in neu["roh"] and "<hoarder />" in neu["roh"]


def test_eintrag_loeschen_entfernt_nur_seinen_block():
    ziel = TYPEN[_idx("SparkPlug")]
    r = bot._spt_anwenden(TEXT, [], [ziel["index"]], [])
    assert r["geloescht"] == 1 and r["nachher"] == len(TYPEN) - 1
    assert r["text"] == TEXT[:ziel["zeile_start"]] + TEXT[ziel["zeile_ende"]:]


def test_neuer_eintrag_kommt_ans_ende_im_stil_der_datei():
    neu = {"name": "Test_Rucksack", "teile": [{"art": "hoarder"}, {"art": "damage", "min": "0.1", "max": "0.5"},
                                              {"art": "cargo", "chance": "0.5", "preset": None,
                                               "items": [{"name": "Rag", "chance": "0.3"}, {"name": "Apple", "chance": None}]}]}
    r = bot._spt_anwenden(TEXT, [], [], [neu])
    assert r["neu"] == 1 and r["nachher"] == len(TYPEN) + 1
    ende = r["text"].rstrip().rsplit("</spawnabletypes>", 1)[0]
    assert ende.endswith('\t<type name="Test_Rucksack">\n\t\t<hoarder />\n\t\t<damage min="0.1" max="0.5" />\n'
                         '\t\t<cargo chance="0.5">\n\t\t\t<item name="Rag" chance="0.3" />\n\t\t\t<item name="Apple" />\n\t\t</cargo>\n\t</type>\n')
    assert r["text"].startswith(TEXT[:TEXT.rindex("</spawnabletypes>")])


def test_leerer_neuer_eintrag_und_crlf():
    text = TEXT.replace("\n", "\r\n")
    r = bot._spt_anwenden(text, [], [], [{"name": "Leer_Ding", "teile": []}])
    assert "\r\n" in r["text"] and "\n" not in r["text"].replace("\r\n", "")
    assert '<type name="Leer_Ding">\r\n\t</type>' in r["text"]


def test_fehlende_datei_wird_angelegt():
    r = bot._spt_anwenden(None, [], [], [{"name": "A_B", "teile": [{"art": "hoarder"}]}])
    assert r["vorher"] == 0 and r["nachher"] == 1 and ET.fromstring(r["text"]).find("type/hoarder") is not None


def test_schreibgeschuetzter_eintrag_laesst_sich_nur_loeschen():
    t = TYPEN[_idx("MakarovIJ70")]
    with pytest.raises(ValueError, match="schreibgeschützt"):
        bot._spt_anwenden(TEXT, [{"src": t["index"], "name": t["name"], "teile": t["teile"] + [{"art": "hoarder"}]}], [], [])
    assert bot._spt_anwenden(TEXT, [], [t["index"]], [])["geloescht"] == 1


# ── Pruefung ───────────────────────────────────────────────────────────────
def _mit(teile_neu, name="X_Test"):
    return bot._spt_anwenden(TEXT, [], [], [{"name": name, "teile": teile_neu}])


@pytest.mark.parametrize("teile,stichwort", [
    ([{"art": "cargo", "chance": "5", "preset": None, "items": []}], "zwischen 0 und 1"),
    ([{"art": "cargo", "chance": "abc", "preset": None, "items": []}], "keine Zahl"),
    ([{"art": "cargo", "chance": "nan", "preset": None, "items": []}], "zwischen 0 und 1"),
    ([{"art": "attachments", "chance": None, "preset": None, "items": [{"name": "A", "chance": "-0.1"}]}], "zwischen 0 und 1"),
    ([{"art": "damage", "min": "0.9", "max": "0.1"}], "nicht größer"),
    ([{"art": "damage", "min": "0.1", "max": None}], "min und max"),
    ([{"art": "cargo", "chance": "1", "preset": None, "items": [{"name": "", "chance": None}]}], "leer oder ungültig"),
    ([{"art": "cargo", "chance": "1", "preset": None, "items": [{"name": 'a"b', "chance": None}]}], "leer oder ungültig"),
    ([{"art": "cargo", "chance": "1", "preset": 'x y', "items": []}], "Preset"),
    ([{"art": "tag", "name": "a b"}], "Tag"),
    ([{"art": "hoarder"}, {"art": "hoarder"}], "nur einmal"),
    ([{"art": "gibtsnicht"}], "unbekannter Teil"),
])
def test_ungueltige_eingaben_werden_abgelehnt(teile, stichwort):
    with pytest.raises(ValueError, match=stichwort):
        _mit(teile)


@pytest.mark.parametrize("name", ["", "  ", "a b", 'a"b', "<x>", "x" * 200])
def test_ungueltige_namen(name):
    with pytest.raises(ValueError, match="ungültig"):
        _mit([], name)


def test_bestehender_name_bei_neuem_eintrag_wird_abgelehnt():
    with pytest.raises(ValueError, match="gibt es schon"):
        _mit([], "SparkPlug")
    teile = _teile("SparkPlug")
    with pytest.raises(ValueError, match="gibt es schon"):
        bot._spt_anwenden(TEXT, [{"src": _idx("SparkPlug"), "name": "Glowplug", "teile": teile}], [], [])


def test_liste_passt_nicht_zur_datei():
    for src in (-1, 99999, True, None):
        with pytest.raises(ValueError, match="neu laden"):
            bot._spt_anwenden(TEXT, [{"src": src, "name": "x", "teile": []}], [], [])
    with pytest.raises(ValueError, match="mehrfach"):
        bot._spt_anwenden(TEXT, [], [3, 3], [])


def test_preset_wird_gegen_die_cfgrandompresets_geprueft():
    praesets = {"attachments": {"vorhanden"}, "cargo": {"mixArmy"}}
    teile = [{"art": "cargo", "chance": None, "preset": "nichtda", "items": []}]
    with pytest.raises(ValueError, match="gibt es in der cfgrandompresets.xml"):
        bot._spt_anwenden(TEXT, [], [], [{"name": "N1", "teile": teile}], praesets)
    ok = [{"art": "cargo", "chance": None, "preset": "mixArmy", "items": []}]
    assert bot._spt_anwenden(TEXT, [], [], [{"name": "N2", "teile": ok}], praesets)["neu"] == 1
    # bereits vorhandene, aber nicht definierte Verweise blockieren nicht, werden aber gemeldet
    r = bot._spt_anwenden(TEXT, [{"src": _idx("AssaultBag_Black"), "name": "AssaultBag_Black",
                                  "teile": _teile("AssaultBag_Black") + [{"art": "hoarder"}]}], [], [], {"attachments": set(), "cargo": set()})
    assert r["fehlende_presets"] == ["mixArmy"]


def test_warnungen():
    teile = [("X", [{"art": "tag", "name": "shelf"}, {"art": "cargo", "chance": None, "preset": None, "items": []},
                    {"art": "cargo", "chance": "1", "preset": "p", "items": [{"name": "A", "chance": None}]}])]
    w = " ".join(bot._spt_warnungen(teile, ["floor", "ground", "shelves"], ["p"]))
    assert "weder Items noch ein Preset" in w and "shelf" in w and "shelves" in w and "Preset UND eigene Items" in w


def test_presets_und_tags_lesen():
    pr = bot._spt_praesets_lesen('<randompresets><cargo name="a"/><attachments name="b"/><cargo name="c"/></randompresets>')
    assert pr == {"attachments": {"b"}, "cargo": {"a", "c"}} and bot._spt_praesets_lesen("kaputt") is None and bot._spt_praesets_lesen(None) is None
    assert bot._spt_tags_lesen('<lists><tags><tag name="floor"/><tag name="shelves"/></tags></lists>') == ["floor", "shelves"]


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
        "1000": {"service_id": "1000", "nitrado_token": "fake", "owner_discord_id": "1", "map_name": "Nasdara",
                 "ftp_mission_dir": "/mission"}}))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    c = reg._conns["1000"]
    c.ftp = _FTP({"/mission/cfgspawnabletypes.xml": TEXT,
                  "/mission/cfgrandompresets.xml": '<randompresets><cargo name="mixArmy"/><attachments name="optics"/></randompresets>',
                  "/mission/cfglimitsdefinition.xml": '<lists><tags><tag name="floor"/><tag name="shelves"/><tag name="ground"/></tags></lists>'})
    return c


def _aufruf(monkeypatch, c, handler, methode="GET", daten=None):
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": True, "service_id": c.service_id, "seen": time.time()}
    req = make_mocked_request(methode, "/api/tools/spawnabletypes", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})

    async def body(_r):
        return daten
    monkeypatch.setattr(bot, "body", body)
    r = asyncio.run(handler(req))
    return r.status, json.loads(r.body)


def test_get_liefert_eintraege_presets_tags(monkeypatch, conn):
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_spawnabletypes_get)
    d = j["data"]
    assert st == 200 and len(d["eintraege"]) == 584 and d["praesets_gelesen"] is True and d["hash"]
    assert d["praesets"] == {"attachments": ["optics"], "cargo": ["mixArmy"]} and d["tags"] == ["floor", "ground", "shelves"]
    assert sum(1 for e in d["eintraege"] if e["gesperrt"]) == 1


def test_vorschau_commit_hash_backup(monkeypatch, conn):
    _, j = _aufruf(monkeypatch, conn, bot.api_tools_spawnabletypes_get)
    h = j["data"]["hash"]
    eintrag = next(e for e in j["data"]["eintraege"] if e["name"] == "MiniDress_Pink")
    eintrag["teile"][0]["chance"] = "0.9"
    nutz = {"geaendert": [{"src": eintrag["src"], "name": eintrag["name"], "teile": eintrag["teile"]}]}
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_spawnabletypes_post, "POST", nutz)
    assert st == 200 and j["data"]["aenderung"]["geaendert"] == 1 and conn.ftp.writes == [] and j["data"]["backup"] is None
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_spawnabletypes_post, "POST", dict(nutz, commit=True))
    assert st == 409 and conn.ftp.writes == []
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_spawnabletypes_post, "POST", dict(nutz, commit=True, erwartet_hash=h))
    assert st == 200 and j["data"]["backup"] == "cfgspawnabletypes.xml.bak"
    assert conn.ftp.writes == ["/mission/cfgspawnabletypes.xml.bak", "/mission/cfgspawnabletypes.xml"]
    assert conn.ftp.files["/mission/cfgspawnabletypes.xml.bak"] == TEXT and 'chance="0.9"' in conn.ftp.files["/mission/cfgspawnabletypes.xml"]


def test_fehler_und_warnung(monkeypatch, conn):
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_spawnabletypes_post, "POST",
                    {"neu": [{"name": "N", "teile": [{"art": "damage", "min": "2", "max": "3"}]}]})
    assert st == 400 and "zwischen 0 und 1" in j["error"]
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_spawnabletypes_post, "POST",
                    {"neu": [{"name": "N", "teile": [{"art": "tag", "name": "shelf"}]}]})
    assert st == 200 and any("shelf" in w for w in j["data"]["warnungen"])
    st, j = _aufruf(monkeypatch, conn, bot.api_tools_spawnabletypes_post, "POST", {"geaendert": "x"})
    assert st == 400


def test_zweiter_server_sieht_nicht_die_datei_des_ersten(monkeypatch, conn, tmp_path):
    flat = json.loads((tmp_path / "connections.json").read_text())
    flat["2000"] = {"service_id": "2000", "nitrado_token": "fake", "owner_discord_id": "2", "map_name": "Livonia", "ftp_mission_dir": "/andere"}
    (tmp_path / "connections.json").write_text(json.dumps(flat))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    reg._conns["1000"].ftp = conn.ftp
    reg._conns["2000"].ftp = _FTP({})
    st, j = _aufruf(monkeypatch, reg._conns["2000"], bot.api_tools_spawnabletypes_get)
    assert st == 200 and j["data"]["vorhanden"] is False and j["data"]["eintraege"] == []
