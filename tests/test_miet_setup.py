"""Miet-Setup: Fahrzeug suchen und eine fertige Event-Vorlage bekommen (statt XML von Hand zu schreiben).

Geprueft werden der Baustein (gueltiges, kompaktes Event; keine Einschleusung ueber den Classnamen), die Suche (Vorlagen vor
Katalog, ohne Doppelte, je Server getrennt), die Dashboard-Routen samt Rechten und dass die Vorlage durch dieselbe Pruefung und
dasselbe Einfuegen laeuft wie ein von Hand geschriebenes Miet-Item. Es geht kein Test ins Internet.

    python3 -m pytest tests/test_miet_setup.py -q
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
from test_shop_rentals import _EVENTS_XML


# ── Baustein ────────────────────────────────────────────────────────────────
def test_vorlage_ist_gueltig_kompakt_und_hat_den_classnamen():
    v = bot._rental_vorlage_bauen("CivilianSedan")
    assert v["name"] == "VehicleCivilianSedan" and v["klasse"] == "CivilianSedan"
    xml, fehler = bot._tool_rental_xml_validieren(v["event_xml"], "event")
    assert fehler is None and xml == v["event_xml"] and len(xml) <= bot._RENTAL_MAX_EVENT_XML and "\n" not in xml
    el = ET.fromstring(xml)
    assert el.get("name") == "VehicleCivilianSedan"
    assert [c.get("type") for c in el.iter("child")] == ["CivilianSedan"]
    assert {k: el.findtext(k) for k in ("nominal", "min", "max", "restock", "active", "position", "limit", "lifetime")} == {
        "nominal": "1", "min": "1", "max": "1", "restock": "0", "active": "1", "position": "fixed", "limit": "child", "lifetime": "3888000"}
    assert el.find("flags").attrib == {"deletable": "0", "init_random": "0", "remove_damaged": "1"}


def test_name_beginnt_nie_mit_dem_miet_praefix():
    for klasse in ("RENT_Test", "Rent", "CivilianSedan"):
        assert not bot._rental_vorlage_bauen(klasse)["name"].startswith(bot._RENTAL_EVENT_PREFIX)


def test_events_datei_ist_eine_vollstaendige_events_xml():
    v = bot._rental_vorlage_bauen("Hatchback_02_Blue", 86400)
    assert v["events_datei"].startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<events>\n')
    assert v["events_datei"].endswith("</events>\n")
    wurzel = ET.fromstring(v["events_datei"].encode("utf-8"))
    events = list(wurzel)
    assert len(events) == 1 and events[0].get("name") == "VehicleHatchback02Blue" and events[0].findtext("lifetime") == "86400"
    assert ET.tostring(events[0], encoding="unicode") .count("<child ") == 1
    assert [c.get("type") for c in wurzel.iter("child")] == ["Hatchback_02_Blue"]


@pytest.mark.parametrize("klasse", ["", " ", "a b", "a<b", 'a"b', "a/b", "x" * 81, None, 5, ["A"], "ä", "Klasse;DROP", "a\nb", "a\tb", '"/><evil/>', "../x",
                                    "a&b", "a'b", "<x>", "a-b", "a.b"])
def test_ungueltiger_classname_wird_abgelehnt(klasse):
    with pytest.raises(ValueError):
        bot._rental_vorlage_bauen(klasse)


@pytest.mark.parametrize("klasse", ["A", "A_1", "x" * 80, "OffroadHatchback_White", " CivilianSedan "])
def test_gueltige_classnamen(klasse):
    v = bot._rental_vorlage_bauen(klasse)
    assert v["klasse"] == klasse.strip() and f'type="{klasse.strip()}"' in v["event_xml"]


@pytest.mark.parametrize("lifetime", [59, 0, -5, 31_536_001, "5000", True, False, 3.5, None, [60]])
def test_ungueltige_haltbarkeit(lifetime):
    with pytest.raises(ValueError):
        bot._rental_vorlage_bauen("CivilianSedan", lifetime)


@pytest.mark.parametrize("lifetime", [60, 3600, 31_536_000])
def test_gueltige_haltbarkeit(lifetime):
    assert f"<lifetime>{lifetime}</lifetime>" in bot._rental_vorlage_bauen("CivilianSedan", lifetime)["event_xml"]


def test_vorlage_laeuft_durch_das_echte_miet_einfuegen():
    v = bot._rental_vorlage_bauen("Sedan_02_Red")
    umbenannt = bot._tool_rental_event_umbenennen(v["event_xml"], "RENT_abc1234567")
    text = bot._tool_rental_event_einfuegen(_EVENTS_XML, "RENT_abc1234567", umbenannt)
    wurzel = ET.fromstring(text.encode("utf-8"))
    neu = [e for e in wurzel.iter("event") if e.get("name") == "RENT_abc1234567"]
    assert len(neu) == 1 and [c.get("type") for c in neu[0].iter("child")] == ["Sedan_02_Red"]
    assert not [e for e in wurzel.iter("event") if e.get("name") == "VehicleSedan02Red"]       # der Vorlagenname bleibt nicht zurueck


def test_vorlagen_namen_sind_sprachneutral():
    for name, _k in bot._RENTAL_VORLAGEN:
        assert name.isascii() and "(" not in name


def test_alle_mitgelieferten_vorlagen_sind_gueltig_und_ohne_doppelte():
    klassen = [k for _n, k in bot._RENTAL_VORLAGEN]
    assert len(klassen) == len({k.lower() for k in klassen}) == 15
    assert len({n for n, _k in bot._RENTAL_VORLAGEN}) == 15
    for _n, k in bot._RENTAL_VORLAGEN:
        v = bot._rental_vorlage_bauen(k)
        assert bot._tool_rental_xml_validieren(v["event_xml"], "event")[1] is None


# ── Suche ───────────────────────────────────────────────────────────────────
def test_suche_ohne_text_zeigt_alle_vorlagen(welt):
    s = bot._rental_vorlagen_suche(welt["a"], "")
    assert len(s) == 15 and all(x["vorlage"] for x in s)


def test_suche_vorlagen_vor_katalog_ohne_doppelte(welt):
    s = bot._rental_vorlagen_suche(welt["a"], "sedan")
    klassen = [x["classname"].lower() for x in s]
    assert len(klassen) == len(set(klassen))
    assert "civiliansedan" in klassen and "sedan_02" in klassen
    erste_katalog = next((i for i, x in enumerate(s) if not x["vorlage"]), len(s))
    assert all(x["vorlage"] for x in s[:erste_katalog]) and not any(x["vorlage"] for x in s[erste_katalog:])


def test_suche_nach_spielername_und_classname(welt):
    assert [x["classname"] for x in bot._rental_vorlagen_suche(welt["a"], "olga")] == ["CivilianSedan", "CivilianSedan_Black", "CivilianSedan_Wine"]
    assert {x["classname"] for x in bot._rental_vorlagen_suche(welt["a"], "TRUCK_01")} >= {"Truck_01_Covered", "Truck_01_Covered_Blue"}
    assert bot._rental_vorlagen_suche(welt["a"], "M3S")[0]["name"].startswith("M3S")


def test_suche_findet_katalog_classnamen_ohne_vorlage(welt):
    s = bot._rental_vorlagen_suche(welt["a"], "Offroad_02")
    assert [(x["classname"], x["vorlage"]) for x in s] == [("Offroad_02", False)]


def test_suche_nichts_gefunden_und_laenge_begrenzt(welt):
    assert bot._rental_vorlagen_suche(welt["a"], "gibtesnichtxyz") == []
    assert bot._rental_vorlagen_suche(welt["a"], "x" * 500) == []


def test_suche_hoechstens_30(welt):
    welt["a"].data["shop_autofill_classnames"] = [{"classname": f"Auto{i:03d}", "name": f"Auto {i}"} for i in range(80)]
    assert len(bot._rental_vorlagen_suche(welt["a"], "auto")) == 30


def test_suche_filtert_unzulaessige_classnamen_aus_dem_katalog(welt):
    welt["a"].data["shop_autofill_classnames"] = [{"classname": "zzBad Name"}, {"classname": 'zzx"y'}, {"classname": "zzGut_1", "name": "Gut"}]
    assert [x["classname"] for x in bot._rental_vorlagen_suche(welt["a"], "zz")] == ["zzGut_1"]
    assert bot._rental_vorlagen_suche(welt["a"], "zzbad name") == []


def test_suche_je_server_getrennt(welt):
    welt["a"].data["shop_autofill_classnames"] = [{"classname": "NurAufA", "name": "Nur A"}]
    assert [x["classname"] for x in bot._rental_vorlagen_suche(welt["a"], "nurauf")] == ["NurAufA"]
    assert bot._rental_vorlagen_suche(welt["b"], "nurauf") == []


# ── Dashboard-Routen ────────────────────────────────────────────────────────
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


def test_routen_ohne_login_und_als_spieler_gesperrt(welt):
    s = _session(gid=G_A, sid_="1000")
    for sid in (None, s):
        assert api("GET", "/api/shop/rentals/vorlagen", sid)[0] in (401, 403)
        assert api("POST", "/api/shop/rentals/vorlage", sid, {"classname": "CivilianSedan"})[0] in (401, 403)


def test_route_suche(welt):
    st, d = _call(welt, "GET", "/vorlagen?q=olga")
    assert st == 200 and [x["classname"] for x in d["data"]["vorlagen"]] == ["CivilianSedan", "CivilianSedan_Black", "CivilianSedan_Wine"]
    assert len(_call(welt, "GET", "/vorlagen")[1]["data"]["vorlagen"]) == 15


def test_route_vorlage_liefert_fertiges_event_und_speichert_nichts(welt):
    vorher = len(welt["a"].rentals_catalog.items)
    st, d = _call(welt, "POST", "/vorlage", {"classname": "CivilianSedan_Black"})
    assert st == 200, d
    v = d["data"]
    assert v["name"] == "VehicleCivilianSedanBlack" and v["vorschlag_name"] == "Olga 24 Black"
    assert bot._tool_rental_xml_validieren(v["event_xml"], "event")[1] is None and "<events>" in v["events_datei"]
    assert len(welt["a"].rentals_catalog.items) == vorher


def test_route_vorschlag_name_fuer_unbekannte_classnamen(welt):
    assert _call(welt, "POST", "/vorlage", {"classname": "Offroad_02"})[1]["data"]["vorschlag_name"] == "Offroad 02"
    assert _call(welt, "POST", "/vorlage", {"classname": "BoatFiberglass"})[1]["data"]["vorschlag_name"] == "Boat Fiberglass"


@pytest.mark.parametrize("daten,teil", [
    ({}, "Classname"), ({"classname": ""}, "Classname"), ({"classname": "a b"}, "Classname"), ({"classname": 5}, "Classname"),
    ({"classname": '"/><x/>'}, "Classname"), ({"classname": "x" * 81}, "Classname"),
    ({"classname": "CivilianSedan", "lifetime": 5}, "Haltbarkeit"), ({"classname": "CivilianSedan", "lifetime": "99999"}, "Haltbarkeit"),
    ({"classname": "CivilianSedan", "lifetime": True}, "Haltbarkeit")])
def test_route_vorlage_ablehnungen(welt, daten, teil):
    st, d = _call(welt, "POST", "/vorlage", daten)
    assert st == 400 and teil in d["error"]


def test_route_kein_json_objekt(welt):
    assert _call(welt, "POST", "/vorlage", [1, 2])[0] == 400


def test_route_gast_nur_ansehen_darf_suchen(welt):
    welt["a"].data["dashboard_perms"] = {"user:900": {"shop": ["view"]}}
    assert _call(welt, "GET", "/vorlagen?q=olga", gast="900")[0] == 200
    assert _call(welt, "POST", "/vorlage", {"classname": "CivilianSedan"}, gast="900")[0] == 200       # nur Berechnung, aendert nichts
    assert _call(welt, "GET", "/vorlagen?q=olga", gast="901")[0] == 403
    assert _call(welt, "POST", "/vorlage", {"classname": "CivilianSedan"}, gast="901")[0] == 403
    welt["a"].data["dashboard_perms"]["user:902"] = {"map": ["view"]}                    # Recht fuer ein anderes Modul reicht nicht
    assert _call(welt, "GET", "/vorlagen?q=olga", gast="902")[0] == 403
    assert _call(welt, "POST", "/vorlage", {"classname": "CivilianSedan"}, gast="902")[0] == 403


def test_route_rate_limit_der_vorlage(welt):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    sid = _admin(welt)

    async def lauf():
        async with TestClient(TestServer(bot.build_app())) as c:
            bot._DASH_RATE_LIMIT_LAST.clear()
            r1 = await c.post("/api/shop/rentals/vorlage", json={"classname": "CivilianSedan"}, cookies={bot._SESS_COOKIE: sid})
            r2 = await c.post("/api/shop/rentals/vorlage", json={"classname": "CivilianSedan"}, cookies={bot._SESS_COOKIE: sid})
            return r1.status, r2.status
    assert asyncio.run(lauf()) == (200, 429)


def test_vorlage_laesst_sich_als_miet_item_anlegen(welt):
    """Der volle Weg: suchen -> Vorlage -> als Miet-Item speichern; danach steht es im Katalog und die Namen sind eindeutig."""
    v = _call(welt, "POST", "/vorlage", {"classname": "Truck_01_Covered"})[1]["data"]
    st, d = _call(welt, "POST", "", {"name": v["vorschlag_name"], "price_per_restart": 300, "event_xml": v["event_xml"], "category": "Vehicle"})
    assert st == 200, d
    assert [i["name"] for i in welt["a"].rentals_catalog.items] == ["M3S Truck"]
    assert [i["name"] for i in welt["b"].rentals_catalog.items] == []
