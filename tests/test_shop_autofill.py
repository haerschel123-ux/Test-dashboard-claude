"""Shop: types.xml fuers Autofill direkt vom Server (oder aus Datei) und alle neuen 1.30-Items vorschlagbar.

    python3 -m pytest tests/test_shop_autofill.py -v
"""
import asyncio
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for pfad in (REPO, os.path.join(REPO, "tests")):
    if pfad not in sys.path:
        sys.path.insert(0, pfad)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")
import test_deployment as td  # noqa: E402  (FTP-Attrappe und Anfrage-Helfer)

TYPES_XML = """<?xml version="1.0"?>
<types>
  <type name="Zzz_NurAufDemServer"><nominal>1</nominal></type>
  <type name="HoloOptic"><nominal>2</nominal></type>
</types>
"""


class _MP:
    def setattr(self, o, n, v):
        setattr(o, n, v)


def _lauf(coro):
    return asyncio.run(coro)


def _post(conn, daten):
    req = td._request(_MP(), conn, "POST", "/api/shop/upload-types", daten)
    r = _lauf(bot.api_shop_upload_types(req))
    return r.status, json.loads(r.body.decode("utf-8"))


def _suche(conn, q, limit=25):
    req = td._request(_MP(), conn, "GET", f"/api/shop/classnames?q={q}&limit={limit}")
    r = _lauf(bot.api_shop_classnames(req))
    return json.loads(r.body.decode("utf-8"))["data"]["classnames"]


def _conn(types=None):
    conn = td._server(map_name="Nasdara")
    conn.data.pop("shop_autofill_classnames", None)
    conn.data.pop("shop_autofill_quelle", None)
    if types is not None:
        conn.ftp.dateien["/mission/db/types.xml"] = types
    return conn


def test_types_xml_direkt_vom_server_holen_und_ohne_katalog_anzulegen():
    conn = _conn(TYPES_XML)
    status, j = _post(conn, {"vom_server": True})
    assert status == 200 and j["data"]["items"] == 2 and j["data"]["quelle"] == "server"
    assert "von deinem Server" in j["data"]["message"]
    assert {e["classname"] for e in conn.data["shop_autofill_classnames"]} == {"Zzz_NurAufDemServer", "HoloOptic"}
    assert conn.data["shop_autofill_quelle"]["quelle"] == "server"
    assert not conn.catalog.items                      # legt nie von sich aus Shop-Items an


def test_vom_server_ohne_types_xml_oder_ohne_mission_ordner_gibt_klare_meldung():
    conn = _conn()                                      # keine db/types.xml
    status, j = _post(conn, {"vom_server": True})
    assert status == 404 and "keine db/types.xml" in j["error"]
    conn2 = _conn(TYPES_XML)
    conn2.data["ftp_mission_dir"] = ""
    status, j = _post(conn2, {"vom_server": True})
    assert status == 409 and "Mission-Ordner" in j["error"] and "Datei hochladen" in j["error"]
    assert "shop_autofill_classnames" not in conn.data


def test_upload_aus_datei_funktioniert_weiter_und_ersetzt_die_liste():
    conn = _conn(TYPES_XML)
    _post(conn, {"vom_server": True})
    status, j = _post(conn, {"xml": '<types><type name="Aus_Datei_1"/></types>'})
    assert status == 200 and j["data"]["quelle"] == "datei" and "aus der Datei" in j["data"]["message"]
    assert [e["classname"] for e in conn.data["shop_autofill_classnames"]] == ["Aus_Datei_1"]
    status, j = _post(conn, {"xml": "kein xml"})
    assert status == 400


def test_autofill_kennt_auch_ohne_types_xml_alle_neuen_1_30_items():
    """Der mitgelieferte Katalog schlaegt die neuen Badlands-Items vor (ohne hinterlegte types.xml)."""
    conn = _conn()
    eingebaut = {e["classname"].lower() for e in bot._shop_eingebaute_classnames()}
    assert len(eingebaut) > 1700
    for cn in ("HoloOptic", "PlateCarrierVest_Desert", "Shrimp", "TurbanHat_Gold", "Mag_AKM_30Rnd_Gold",
               "KameezShirt_Black", "Mosin9130_Desert"):
        assert cn.lower() in eingebaut
    assert any(e["classname"] == "Shrimp" for e in _suche(conn, "Shrimp"))


def test_suche_findet_auch_teile_im_namen_anfang_zuerst_und_server_liste_geht_vor():
    conn = _conn(TYPES_XML)
    _post(conn, {"vom_server": True})
    treffer = _suche(conn, "optic", limit=50)
    namen = [e["classname"] for e in treffer]
    assert "HoloOptic" in namen and "ACOGOptic" in namen        # "optic" mitten im Classnamen
    assert namen.count("HoloOptic") == 1
    # Anfang des Classnames vor Teiltreffern
    treffer = _suche(conn, "holo")
    assert treffer[0]["classname"].lower().startswith("holo")
    # Item nur auf dem Server (nicht im Katalog) wird gefunden
    assert _suche(conn, "zzz_nur")[0]["classname"] == "Zzz_NurAufDemServer"
    # Server-Liste hat Vorrang vor dem eingebauten Katalog (Kategorie der Server-Eintraege)
    assert [e for e in _suche(conn, "HoloOptic") if e["classname"] == "HoloOptic"][0]["category"] != "DayZ 1.30 (Katalog)"


def test_items_antwort_nennt_autofill_stand():
    conn = _conn(TYPES_XML)
    _post(conn, {"vom_server": True})
    req = td._request(_MP(), conn, "GET", "/api/shop/items?page_size=10")
    d = json.loads(_lauf(bot.list_items(req)).body.decode("utf-8"))["data"]
    assert d["autofill"]["anzahl"] == 2 and d["autofill"]["quelle"] == "server" and d["autofill"]["eingebaut"] > 1700
