"""NPC-Miete: ein NPC mit Outfit als Miet-Item (Vorlage, freie Koerper, Outfit auf den Server schreiben).

Geprueft werden Vorlage und Pruefungen, dass das Outfit nur in den eigenen Marker-Block und nur fuer einen freien Koerper
geschrieben wird (alles andere in der Datei bleibt Byte fuer Byte), Wiederholung, Fehlerfaelle, Rechte und Server-Trennung.
Es geht kein Test ins Internet und zu keinem FTP.

    python3 -m pytest tests/test_npc_miete.py -q
"""
import os
import sys
import time
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_spieler_dashboard import G_A, G_B, U, _session, api, welt  # noqa: F401 - Fixtures
from test_airstrike import servers  # noqa: F401 - Fixture (zwei Server 1000/2000)

TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<spawnabletypes>\n'
         '    <type name="Barrel_Blue">\n        <cargo chance="1.00"/>\n    </type>\n'
         '    <type name="SurvivorM_Mirek">\n        <attachments chance="1.00">\n            <item name="Shirt_BlueCheck" chance="1.00"/>\n        </attachments>\n    </type>\n'
         '</spawnabletypes>\n')


@pytest.fixture(autouse=True)
def _frisch(monkeypatch):
    monkeypatch.setattr(bot, "_audit_log", bot.deque(maxlen=100))


class _FTP:
    def __init__(self, text=TYPES):
        self.files = {"/mission/cfgspawnabletypes.xml": text} if text is not None else {}
        self.geschrieben, self.schreib_fehler, self.lese_fehler = [], False, False

    def read_file_ex(self, pfad):
        if self.lese_fehler:
            return None, "error"
        return (self.files[pfad], "ok") if pfad in self.files else (None, "missing")

    def write_file(self, pfad, inhalt):
        if self.schreib_fehler:
            return False
        self.files[pfad] = inhalt
        self.geschrieben.append(pfad)
        return True

    def list_dir(self, d):
        return [p for p in self.files if p.startswith(d.rstrip("/") + "/")]

    def datei(self):
        return self.files["/mission/cfgspawnabletypes.xml"]


@pytest.fixture
def npc(welt):
    welt["a"].ftp, welt["b"].ftp = _FTP(), _FTP()
    return welt


def _admin(welt, gid=G_A, conn="a", admin=True, gast=None):
    sid = str(time.time_ns())
    c = welt[conn]
    bot._SESS_STORE[sid] = {"token": "x", "discord": {"id": gast or (sid if admin else str(c.data["owner_discord_id"]))},
                            "is_admin": admin and not gast, "service_id": c.service_id, "seen": time.time(),
                            "admin_geprueft_ts": time.time(), "guild_id": str(gid), **({"is_guest": True} if gast else {})}
    c.data["kunden_stufe"] = "premium"
    return sid


def _call(welt, methode, pfad, daten=None, **kw):
    return api(methode, "/api/shop/rentals" + pfad, _admin(welt, **kw), daten)


def _eigener_npc(conn, key="MeinNpc", label="Mein NPC"):
    conn.data["eigene_npcs"] = {key: {"format": "dayz-npc-1", "name": key, "label": label,
                                       "items": [["attachments", "Shirt_BlueCheck"], ["cargo", "Hammer"]], "zufall": [["attachments", ["Jeans_Black", "Jeans_Grey"]]]}}


# ── Vorlage ─────────────────────────────────────────────────────────────────
def test_npc_vorlage_baustein_nutzt_den_npc_praefix():
    v = bot._rental_vorlage_bauen("SurvivorM_Boris", praefix="Npc")
    assert v["name"] == "NpcSurvivorMBoris" and not v["name"].startswith(bot._RENTAL_EVENT_PREFIX)
    assert [c.get("type") for c in ET.fromstring(v["event_xml"]).iter("child")] == ["SurvivorM_Boris"]


@pytest.mark.parametrize("praefix", ["", "npc", "Rent", "RENT_", None, "Vehicle ", "Npc;"])
def test_ungueltiger_praefix(praefix):
    with pytest.raises(ValueError):
        bot._rental_vorlage_bauen("SurvivorM_Boris", praefix=praefix)


def test_outfit_finden_eingebaut_und_eigene(npc):
    f = bot._npc_outfit_finden(npc["a"], "feuerwehr")
    assert f["label"] == "Feuerwehr" and f["eigener"] is False and ["attachments", "FirefightersHelmet_Red"] in f["items"]
    _eigener_npc(npc["a"])
    e = bot._npc_outfit_finden(npc["a"], "MeinNpc")
    assert e["label"] == "Mein NPC" and e["eigener"] is True and e["zufall"] == [["attachments", ["Jeans_Black", "Jeans_Grey"]]]


@pytest.mark.parametrize("key", [None, 5, "", "gibtesnicht", ["feuerwehr"], "FEUERWEHR", "b:feuerwehr"])
def test_outfit_unbekannt(npc, key):
    assert bot._npc_outfit_finden(npc["a"], key) is None


def test_eigene_outfits_sind_je_server_getrennt(npc):
    _eigener_npc(npc["a"])
    assert bot._npc_outfit_finden(npc["b"], "MeinNpc") is None


def test_koerper_belegung_durch_miet_items(npc):
    npc["a"].rentals_catalog.items.append({"name": "NPC Test", "event_xml": '<event name="x"><children><child type="SurvivorM_Boris" min="1"/></children></event>'})
    npc["a"].rentals_catalog.items.append({"name": "Auto", "event_xml": '<event name="y"><children><child type="CivilianSedan"/></children></event>'})
    assert bot._rental_koerper_belegung(npc["a"]) == {"SurvivorM_Boris": "NPC Test"}
    assert bot._rental_koerper_belegung(npc["b"]) == {}


# ── GET /npc ────────────────────────────────────────────────────────────────
def test_routen_ohne_login_und_als_spieler_gesperrt(npc):
    s = _session(gid=G_A, sid_="1000")
    for sid in (None, s):
        assert api("GET", "/api/shop/rentals/npc", sid)[0] in (401, 403)
        assert api("POST", "/api/shop/rentals/npc-vorlage", sid, {"preset": "feuerwehr", "koerper": "SurvivorM_Boris"})[0] in (401, 403)
        assert api("POST", "/api/shop/rentals/npc-outfit", sid, {"preset": "feuerwehr", "koerper": "SurvivorM_Boris"})[0] in (401, 403)


def test_liste_outfits_und_freie_koerper(npc):
    _eigener_npc(npc["a"])
    npc["a"].rentals_catalog.items.append({"name": "Mieter", "event_xml": '<event name="x"><children><child type="SurvivorM_Cyril"/></children></event>'})
    st, d = _call(npc, "GET", "/npc")
    assert st == 200
    liste = d["data"]
    assert liste["lesbar"] is True
    keys = [o["key"] for o in liste["outfits"]]
    assert keys == list(bot.TOOL_DEPLOY_NPCS) + ["MeinNpc"] and [o["eigener"] for o in liste["outfits"]][-1] is True
    assert [k["name"] for k in liste["koerper"]] == list(bot.TOOL_DEPLOY_SURVIVORS)
    k = {x["name"]: x for x in liste["koerper"]}
    assert k["SurvivorM_Mirek"]["frei"] is False                       # hat schon einen <type>
    assert k["SurvivorM_Boris"]["frei"] is True and k["SurvivorM_Boris"]["miet_item"] is None
    assert k["SurvivorM_Cyril"]["miet_item"] == "Mieter"


def test_liste_ohne_lesbare_datei(npc):
    npc["a"].ftp.lese_fehler = True
    d = _call(npc, "GET", "/npc")[1]["data"]
    assert d["lesbar"] is False and all(k["frei"] is None for k in d["koerper"])
    assert len(d["outfits"]) == len(bot.TOOL_DEPLOY_NPCS)


def test_liste_je_server_getrennt(npc):
    _eigener_npc(npc["a"])
    assert "MeinNpc" not in [o["key"] for o in _call(npc, "GET", "/npc", gid=G_B, conn="b")[1]["data"]["outfits"]]


def test_liste_belegt_durch_npc_einsatz(npc):
    npc["a"].data["deployments"] = [{"id": "dv_1", "art": "npc", "klasse": "SurvivorM_Boris"}]
    k = {x["name"]: x for x in _call(npc, "GET", "/npc")[1]["data"]["koerper"]}
    assert k["SurvivorM_Boris"]["frei"] is False


# ── POST /npc-vorlage ───────────────────────────────────────────────────────
def test_vorlage_route(npc):
    st, d = _call(npc, "POST", "/npc-vorlage", {"preset": "feuerwehr", "koerper": "SurvivorM_Boris"})
    assert st == 200, d
    v = d["data"]
    assert v["name"] == "NpcSurvivorMBoris" and v["vorschlag_name"] == "NPC Feuerwehr" and v["klasse"] == "SurvivorM_Boris"
    assert bot._tool_rental_xml_validieren(v["event_xml"], "event")[1] is None
    block = ET.fromstring(v["outfit_xml"])
    assert block.tag == "type" and block.get("name") == "SurvivorM_Boris"
    assert [i.get("name") for i in block.iter("item")][:2] == ["FirefightersHelmet_Red", "FirefighterJacket_Black"]
    assert npc["a"].ftp.geschrieben == [] and npc["a"].rentals_catalog.items == []           # speichert nichts


def test_vorlage_eigenes_outfit_mit_zufallsgruppe(npc):
    _eigener_npc(npc["a"])
    v = _call(npc, "POST", "/npc-vorlage", {"preset": "MeinNpc", "koerper": "SurvivorF_Eva"})[1]["data"]
    assert v["vorschlag_name"] == "NPC Mein NPC"
    assert {i.get("name") for i in ET.fromstring(v["outfit_xml"]).iter("item")} == {"Shirt_BlueCheck", "Hammer", "Jeans_Black", "Jeans_Grey"}


@pytest.mark.parametrize("daten,teil", [
    ({}, "Outfit"), ({"preset": "nope", "koerper": "SurvivorM_Boris"}, "Outfit"), ({"preset": 5, "koerper": "SurvivorM_Boris"}, "Outfit"),
    ({"preset": "feuerwehr"}, "Körper"), ({"preset": "feuerwehr", "koerper": "CivilianSedan"}, "Körper"),
    ({"preset": "feuerwehr", "koerper": "SurvivorM_Nope"}, "Körper"), ({"preset": "feuerwehr", "koerper": 5}, "Körper"),
    ({"preset": "feuerwehr", "koerper": '"/><x/>'}, "Körper")])
def test_vorlage_ablehnungen(npc, daten, teil):
    st, d = _call(npc, "POST", "/npc-vorlage", daten)
    assert st == 400 and teil in d["error"]


def test_vorlage_gast_nur_ansehen(npc):
    npc["a"].data["dashboard_perms"] = {"user:900": {"shop": ["view"]}}
    assert _call(npc, "POST", "/npc-vorlage", {"preset": "feuerwehr", "koerper": "SurvivorM_Boris"}, gast="900")[0] == 200
    assert _call(npc, "GET", "/npc", gast="900")[0] == 200
    assert _call(npc, "GET", "/npc", gast="901")[0] == 403


# ── POST /npc-outfit ────────────────────────────────────────────────────────
def _outfit(npc, preset="feuerwehr", koerper="SurvivorM_Boris", **kw):
    return _call(npc, "POST", "/npc-outfit", {"preset": preset, "koerper": koerper}, **kw)


def test_outfit_wird_in_den_eigenen_block_geschrieben(npc):
    st, d = _outfit(npc)
    assert st == 200 and d["data"] == {"geschrieben": True, "koerper": "SurvivorM_Boris", "bereits": False}
    neu = npc["a"].ftp.datei()
    ET.fromstring(neu)
    assert bot._tool_finde_benannten_block(neu, "type", "SurvivorM_Boris") is not None
    assert [s["id"] for s in bot._dayzcode_segmente(neu)] == ["rn_SurvivorM_Boris"]
    assert bot._dayzcode_entfernen(neu, "rn_SurvivorM_Boris")[0] == TYPES                 # alles andere Byte fuer Byte wie vorher
    assert npc["a"].ftp.geschrieben == ["/mission/cfgspawnabletypes.xml"] and npc["b"].ftp.geschrieben == []
    assert npc["b"].ftp.datei() == TYPES
    assert [z["detail"] for z in bot._audit_log if z["action"] == "NPC-Miete: Outfit geschrieben"] == ["Feuerwehr → SurvivorM_Boris · Server 1000"]


def test_outfit_wiederholung_ist_unschaedlich(npc):
    _outfit(npc)
    nach_erstem = npc["a"].ftp.datei()
    st, d = _outfit(npc)
    assert st == 200 and d["data"]["bereits"] is True and d["data"]["geschrieben"] is False
    assert npc["a"].ftp.datei() == nach_erstem and len(npc["a"].ftp.geschrieben) == 1


def test_anderes_outfit_auf_demselben_koerper_wird_abgelehnt(npc):
    _outfit(npc, "feuerwehr")
    nach_erstem = npc["a"].ftp.datei()
    st, d = _outfit(npc, "polizei")
    assert st == 409 and "anderes Outfit" in d["error"] and npc["a"].ftp.datei() == nach_erstem


def test_zwei_koerper_zwei_outfits(npc):
    assert _outfit(npc, "feuerwehr", "SurvivorM_Boris")[0] == 200
    assert _outfit(npc, "polizei", "SurvivorM_Cyril")[0] == 200
    assert [s["id"] for s in bot._dayzcode_segmente(npc["a"].ftp.datei())] == ["rn_SurvivorM_Boris", "rn_SurvivorM_Cyril"]
    ET.fromstring(npc["a"].ftp.datei())


def test_belegter_koerper_wird_nicht_ueberschrieben(npc):
    st, d = _outfit(npc, "feuerwehr", "SurvivorM_Mirek")                  # hat schon einen eigenen <type>
    assert st == 409 and "belegt" in d["error"] and npc["a"].ftp.datei() == TYPES and npc["a"].ftp.geschrieben == []


def test_koerper_eines_npc_einsatzes_wird_nicht_genommen(npc):
    npc["a"].data["deployments"] = [{"id": "dv_1", "art": "npc", "klasse": "SurvivorM_Boris"}]
    assert _outfit(npc)[0] == 409 and npc["a"].ftp.geschrieben == []


@pytest.mark.parametrize("daten,teil", [
    ({}, "Outfit"), ({"preset": "nope", "koerper": "SurvivorM_Boris"}, "Outfit"), ({"preset": "feuerwehr"}, "Körper"),
    ({"preset": "feuerwehr", "koerper": "SurvivorM_Nope"}, "Körper"), ({"preset": "feuerwehr", "koerper": "../x"}, "Körper")])
def test_outfit_ablehnungen_schreiben_nichts(npc, daten, teil):
    st, d = _call(npc, "POST", "/npc-outfit", daten)
    assert st == 400 and teil in d["error"] and npc["a"].ftp.geschrieben == []


def test_outfit_datei_nicht_lesbar_und_schreibfehler(npc):
    npc["a"].ftp.lese_fehler = True
    assert _outfit(npc)[0] == 502
    npc["a"].ftp.lese_fehler = False
    npc["a"].ftp.schreib_fehler = True
    st, d = _outfit(npc)
    assert st == 502 and npc["a"].ftp.datei() == TYPES and not [z for z in bot._audit_log if z["action"] == "NPC-Miete: Outfit geschrieben"]


def test_outfit_ohne_mission_ordner(npc):
    npc["a"].data["ftp_mission_dir"] = ""
    npc["a"].set("ftp_mission_dir", "")
    st, d = _outfit(npc)
    assert st == 409 and npc["a"].ftp.geschrieben == []


def test_outfit_kaputte_marker_werden_nicht_angefasst(npc):
    kaputt = TYPES.replace("</spawnabletypes>", "<!-- BRIGARDE-KILLFEED:START x1 -->\n</spawnabletypes>")
    npc["a"].ftp = _FTP(kaputt)
    st, d = _outfit(npc)
    assert st in (400, 409) and npc["a"].ftp.datei() == kaputt


def test_outfit_rechte_gast_nur_shop_darf_nicht_schreiben(npc):
    npc["a"].data["dashboard_perms"] = {"user:900": {"shop": ["view", "create", "edit"]}}
    assert _call(npc, "POST", "/npc-outfit", {"preset": "feuerwehr", "koerper": "SurvivorM_Boris"}, gast="900")[0] == 403
    npc["a"].data["dashboard_perms"] = {"user:901": {"tools": ["view"]}}
    assert _call(npc, "POST", "/npc-outfit", {"preset": "feuerwehr", "koerper": "SurvivorM_Boris"}, gast="901")[0] == 403
    assert npc["a"].ftp.geschrieben == []


def test_outfit_rate_limit(npc):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    sid = _admin(npc)

    async def lauf():
        async with TestClient(TestServer(bot.build_app())) as c:
            bot._DASH_RATE_LIMIT_LAST.clear()
            r1 = await c.post("/api/shop/rentals/npc-outfit", json={"preset": "feuerwehr", "koerper": "SurvivorM_Boris"}, cookies={bot._SESS_COOKIE: sid})
            r2 = await c.post("/api/shop/rentals/npc-outfit", json={"preset": "polizei", "koerper": "SurvivorM_Cyril"}, cookies={bot._SESS_COOKIE: sid})
            return r1.status, r2.status
    assert asyncio.run(lauf()) == (200, 429)


def test_voller_weg_vorlage_outfit_und_miet_item(npc):
    v = _call(npc, "POST", "/npc-vorlage", {"preset": "sanitaeter", "koerper": "SurvivorF_Eva"})[1]["data"]
    assert _outfit(npc, "sanitaeter", "SurvivorF_Eva")[0] == 200
    st, d = _call(npc, "POST", "", {"name": v["vorschlag_name"], "price_per_restart": 400, "event_xml": v["event_xml"], "category": "NPC"})
    assert st == 200, d
    assert [i["name"] for i in npc["a"].rentals_catalog.items] == ["NPC Sanitäter"]
    k = {x["name"]: x for x in _call(npc, "GET", "/npc")[1]["data"]["koerper"]}
    assert k["SurvivorF_Eva"]["frei"] is False and k["SurvivorF_Eva"]["miet_item"] == "NPC Sanitäter"
    assert [i["name"] for i in npc["b"].rentals_catalog.items] == []
