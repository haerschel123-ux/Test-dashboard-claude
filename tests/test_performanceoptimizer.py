"""Performance Optimizer: Nachbau von doordiehub.com/PerformanceOptimizer.

Die Auswertung muss dieselben Reports liefern wie die Webseite. Die Soll-Dateien in tests/fixtures/performanceoptimizer/
sind die Report-Downloads der echten Seite (Brigardes types/events, die Vanilla-Events der Seite und 99 + 55 Grenzfaelle:
Schwellen auf der Kante, kaputte Zahlen, fehlende Tags, Rundung). Das Englisch der Befunde ist der Wortlaut der Seite.

    python3 -m pytest tests/test_performanceoptimizer.py -q
"""
import asyncio
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")
FIX = os.path.join(REPO, "tests", "fixtures", "performanceoptimizer")


def _lesen(name):
    with open(os.path.join(FIX, name), encoding="utf-8", newline="") as f:
        return f.read()


def _bericht(erg, datei):
    """Report-Text der Webseite (performance-report.txt), aus dem englischen Ergebnis gebaut."""
    z = erg["zusammenfassung"]
    y = "=== DayZ Server Performance Report ===\n"
    y += f"File: {datei} | Mode: {erg['modus']}\n"
    y += f"Score: {z['score']}/100 | Total: {z['total']} | Criticals: {z['criticals']} | Warnings: {z['warnings']}\n\n"
    y += "=== FLAGGED ITEMS ===\n"
    for e in erg["eintraege"]:
        if e["grad"] == "ok":
            continue
        y += f"\n[{e['grad'].upper()}] {e['name']}\n"
        for b in e["befunde"]:
            if b["grad"] != "ok":
                y += f"  - {b['meldung']}\n    FIX: {b['hinweis']}\n"
    return y


# (XML, Dateiname der Seite, Modus, Soll-Report, (score, kritisch, warnungen, ok, gesamt, custom), "Most flagged fields")
FAELLE = [
    ("types-brigarde-2026-10-26.xml", "types_-_2026-10-26T115651.831.xml", "types", "soll-types-brigarde.txt",
     (0, 192, 538, 1681, 2411, 471),
     ["624× count_in_map", "472× nominal", "160× min", "106× nominal (vs vanilla)", "70× lifetime"]),
    ("events-brigarde-39.xml", "events_39.xml", "events", "soll-events-brigarde.txt",
     (0, 36, 3, 26, 65, 15), ["33× lifetime", "32× nominal", "4× restock", "3× nominal (vs vanilla)"]),
    ("events-vanilla.xml", "events.xml", "events", "soll-events-vanilla.txt",
     (0, 43, 1, 15, 59, 0), ["42× lifetime", "38× nominal", "3× restock"]),
    ("types-grenzfaelle.xml", "edge_types.xml", "types", "soll-types-grenzfaelle.txt",
     (0, 37, 31, 31, 99, 67), ["28× nominal", "19× lifetime (vs vanilla)", "15× count_in_map", "14× nominal (vs vanilla)", "9× lifetime"]),
    ("events-grenzfaelle.xml", "edge_events.xml", "events", "soll-events-grenzfaelle.txt",
     (0, 30, 10, 15, 55, 37), ["26× nominal", "14× nominal (vs vanilla)", "9× lifetime", "9× distanceradius", "8× restock"]),
]


@pytest.mark.parametrize("xml,datei,modus,soll,zahlen,top", FAELLE, ids=[f[1] for f in FAELLE])
def test_report_ist_byteidentisch_zur_webseite(xml, datei, modus, soll, zahlen, top):
    erg = bot._po_pruefen(_lesen(xml), modus, "en")
    assert _bericht(erg, datei).encode("utf-8") == _lesen(soll).encode("utf-8")
    z = erg["zusammenfassung"]
    assert (z["score"], z["criticals"], z["warnings"], z["ok"], z["total"], z["custom"]) == zahlen
    assert z["topIssues"] == top


def test_deutsche_ausgabe_hat_dieselben_befunde_nur_anderer_text():
    xml = _lesen("types-grenzfaelle.xml")
    de, en = bot._po_pruefen(xml, "types", "de"), bot._po_pruefen(xml, "types", "en")
    assert [(e["name"], e["grad"], [(b["feld"], b["grad"]) for b in e["befunde"]]) for e in de["eintraege"]] == \
           [(e["name"], e["grad"], [(b["feld"], b["grad"]) for b in e["befunde"]]) for e in en["eintraege"]]
    assert de["zusammenfassung"] == en["zusammenfassung"]
    erster = next(e for e in de["eintraege"] if e["befunde"])["befunde"][0]
    assert "is " not in erster["meldung"].split("=")[0] and re.search(r"[äöüÄÖÜß]|ist |sind |liegt ", erster["meldung"] + erster["hinweis"])


def test_alle_meldungen_haben_in_beiden_sprachen_dieselben_platzhalter():
    feld = re.compile(r"\{(\w+)\}")
    for code, (de_m, de_h, en_m, en_h) in bot._PO_TEXTE.items():
        assert set(feld.findall(de_m + de_h)) == set(feld.findall(en_m + en_h)), code
        assert de_m and de_h and en_m and en_h


def _types(*eintraege):
    return "<types>" + "".join(eintraege) + "</types>"


def _t(name, nominal="3", lifetime="14400", restock="0", mn="1", flags='count_in_map="0"'):
    f = f"<flags {flags}/>" if flags is not None else ""
    return (f'<type name="{name}"><nominal>{nominal}</nominal><lifetime>{lifetime}</lifetime>'
            f"<restock>{restock}</restock><min>{mn}</min>{f}</type>")


def _e(name, nominal="3", lifetime="3600", restock="0", dist="500", aktiv="1"):
    a = f"<active>{aktiv}</active>" if aktiv is not None else ""
    return (f'<event name="{name}"><nominal>{nominal}</nominal><lifetime>{lifetime}</lifetime>'
            f"<restock>{restock}</restock><distanceradius>{dist}</distanceradius>{a}</event>")


def _befunde(erg, name=None):
    e = erg["eintraege"][0] if name is None else next(x for x in erg["eintraege"] if x["name"] == name)
    return [(b["feld"], b["grad"]) for b in e["befunde"]]


@pytest.mark.parametrize("text,erwartet", [
    (" 30 ", 30), ("12abc", 12), ("1e3", 1), ("3.9", 3), ("+25", 25), ("-5", -5), ("0x10", 16), ("00041", 41),
    ("", 0), ("abc", None), ("٣٠", None)])
def test_zahlen_werden_wie_javascript_parseint_gelesen(text, erwartet):
    wert = bot._po_zahl(ET.fromstring(f"<t><nominal>{text}</nominal></t>"), "nominal")
    assert (wert != wert) if erwartet is None else wert == erwartet           # None = NaN
    assert bot._po_zahl(ET.fromstring("<t/>"), "nominal") == 0                # fehlendes Tag = 0


def test_rundung_und_toFixed_wie_javascript():
    assert [bot._po_runden(x) for x in (0.5, 1.5, 9.5, 10.5)] == [1, 2, 10, 11]      # Python wuerde 0, 2, 10, 10 liefern
    assert [bot._po_eins(x) for x in (51 / 20, 53 / 20, 57 / 20, 99 / 20, 2.5)] == ["2.5", "2.6", "2.9", "5.0", "2.5"]


def test_fehlende_oder_leere_count_in_map_gilt_als_eins():
    for flags, warnt in (('count_in_map="1"', True), ('count_in_map="0"', False), ('count_in_map=""', True),
                         ('count_in_map="2"', False), ('crafted="0"', True), (None, True)):
        erg = bot._po_pruefen(_types(_t("Neu_X", nominal="16", flags=flags)), "types", "en")
        assert (("count_in_map", "warning") in _befunde(erg)) is warnt, flags


def test_nur_active_null_schaltet_ein_event_ab():
    for aktiv, aus in (("0", True), (" 0 ", True), ("1", False), ("false", False), ("00", False), (None, False)):
        erg = bot._po_pruefen("<events>" + _e("Neu_E", nominal="50", lifetime="1", aktiv=aktiv) + "</events>", "events", "en")
        e = erg["eintraege"][0]
        assert (e["grad"] == "ok" and _befunde(erg) == [("active", "ok")]) is aus, aktiv
    erg = bot._po_pruefen("<events>" + _e("A", aktiv="0") + "</events>", "events", "en")
    assert erg["zusammenfassung"]["ok"] == 1 and erg["zusammenfassung"]["topIssues"] == []   # "Event is disabled" zaehlt nie als Befund


def test_schwellen_liegen_auf_der_kante():
    def grad(nominal, **kw):
        return bot._po_pruefen(_types(_t("Neu_X", nominal=nominal, **kw)), "types", "en")["eintraege"][0]["grad"]
    assert [grad(n, flags='count_in_map="0"') for n in (20, 21, 40, 41)] == ["ok", "warning", "warning", "critical"]
    assert [grad("3", lifetime=l) for l in (599, 600, 1799, 1800)] == ["critical", "warning", "warning", "ok"]
    assert [grad("3", restock=r) for r in (59, 60, 299, 300)] == ["critical", "warning", "warning", "ok"]
    assert [grad("20", mn=m, flags='count_in_map="0"') for m in (17, 18, 21)] == ["ok", "warning", "critical"]


def test_namen_wie_toString_gelten_nicht_als_vanilla():
    erg = bot._po_pruefen(_types(*(_t(n) for n in ("toString", "constructor", "hasOwnProperty", "__proto__"))), "types", "en")
    assert [e["custom"] for e in erg["eintraege"]] == [True] * 4      # die Webseite haelt sie wegen einer JS-Eigenheit fuer Vanilla


def test_name_fehlt_und_doppelte_namen():
    erg = bot._po_pruefen(_types("<type><nominal>50</nominal></type>", _t("Doppelt", nominal="45"), _t("Doppelt", nominal="45")), "types", "en")
    assert [e["name"] for e in erg["eintraege"]] == ["Unknown", "Doppelt", "Doppelt"]


@pytest.mark.parametrize("text", ["", "   ", '<types><type name="a"></types>', "kein xml", '<types><type name="a">&</type></types>'])
def test_kaputtes_xml_ergibt_none(text):
    assert bot._po_pruefen(text, "types", "en") is None


def test_leere_wurzel_ist_gesund_und_hat_keine_eintraege():
    erg = bot._po_pruefen("<types></types>", "types", "de")
    assert erg["eintraege"] == [] and erg["zusammenfassung"]["score"] == 100 and erg["zusammenfassung"]["total"] == 0
    assert bot._po_pruefen(_types(_t("A")), "events", "de")["eintraege"] == []      # Types-Inhalt im Events-Modus: nichts gefunden


def test_referenz_ist_die_der_webseite():
    ref = bot._po_referenz()
    assert (len(ref["types"]), len(ref["events"])) == (1941, 59)
    assert ref["types"]["ACOGOptic"] == [10, 14400, 1800, 6] and ref["events"]["InfectedArmy"] == [50, 3, 0, 25]


def test_modul_und_werkzeugliste_sind_eingetragen():
    assert "tools.performanceoptimizer" in bot.FEATURE_MODULES
    assert any(k == "performanceoptimizer" for k, _e, _l in bot._TOOL_LISTE)
    assert bot._module_tier("tools.performanceoptimizer") == "premium"


# ── Routen ───────────────────────────────────────────────────────────────
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
        return True


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    flat = {"1000": {"service_id": "1000", "nitrado_token": "fake", "owner_discord_id": "1", "map_name": "Nasdara", "ftp_mission_dir": "/mission"},
            "2000": {"service_id": "2000", "nitrado_token": "fake", "owner_discord_id": "2", "map_name": "Livonia", "ftp_mission_dir": "/andere"},
            "3000": {"service_id": "3000", "nitrado_token": "fake", "owner_discord_id": "3", "map_name": "Nasdara"}}
    (tmp_path / "connections.json").write_text(json.dumps(flat))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    reg._conns["1000"].ftp = _FTP({"/mission/db/types.xml": _types(_t("AK74", nominal="30"), _t("Nur_Nasdara", nominal="2")),
                                   "/mission/db/events.xml": "<events>" + _e("InfectedArmy", nominal="50", lifetime="3") + "</events>"})
    reg._conns["2000"].ftp = _FTP({"/andere/db/types.xml": _types(_t("NurLivonia", nominal="99"))})
    reg._conns["3000"].ftp = _FTP({})
    return reg


def _sitzung(conn, admin=True):
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": admin, "service_id": conn.service_id, "seen": time.time()}
    return {"Cookie": f"{bot._SESS_COOKIE}={sid}"}


def _get(conn, query="", cookies=True):
    req = make_mocked_request("GET", "/api/tools/performanceoptimizer" + query, headers=_sitzung(conn) if cookies else {})
    r = asyncio.run(bot.api_tools_performanceoptimizer_get(req))
    return r.status, json.loads(r.body)


def _post(monkeypatch, conn, daten):
    async def _body(_request):
        return daten
    monkeypatch.setattr(bot, "body", _body)
    req = make_mocked_request("POST", "/api/tools/performanceoptimizer", headers=_sitzung(conn))
    r = asyncio.run(bot.api_tools_performanceoptimizer_post(req))
    return r.status, json.loads(r.body)


def test_route_ohne_anmeldung_wird_abgewiesen(server):
    st, _j = _get(server._conns["1000"], cookies=False)
    assert st in (401, 403)


def test_route_meta_nennt_referenz_und_karte_des_servers(server):
    st, j = _get(server._conns["1000"])
    d = j["data"]
    assert st == 200 and d["referenz"] == {"types": 1941, "events": 59, "karte": "Chernarus"}
    assert d["server_karte"] == "Nasdara" and d["kein_mission_ordner"] is False and "eintraege" not in d


def test_route_liest_types_und_events_des_eigenen_servers(server):
    st, j = _get(server._conns["1000"], "?datei=types&sprache=en")
    d = j["data"]
    assert st == 200 and d["datei"] == "db/types.xml" and d["modus"] == "types"
    assert [e["name"] for e in d["eintraege"]] == ["AK74", "Nur_Nasdara"] and d["eintraege"][1]["custom"] is True
    st, j = _get(server._conns["1000"], "?datei=events&sprache=en")
    assert [e["name"] for e in j["data"]["eintraege"]] == ["InfectedArmy"] and j["data"]["eintraege"][0]["grad"] == "critical"
    st, j = _get(server._conns["2000"], "?datei=types")
    assert [e["name"] for e in j["data"]["eintraege"]] == ["NurLivonia"]                       # nie die Datei eines anderen Kunden


def test_route_sprache_steuert_den_text(server):
    c = server._conns["1000"]
    server_events = "<events>" + _e("AnimalSheep", nominal="20") + "</events>"          # Vanilla 5 -> das 4-Fache
    c.ftp.files["/mission/db/events.xml"] = server_events
    en = _get(c, "?datei=events&sprache=en")[1]["data"]["eintraege"][0]["befunde"][0]
    assert en["meldung"] == "nominal=20 is 4.0× vanilla value of 5"                    # Englisch: Wortlaut der Seite
    bot._DASH_RATE_LIMIT_LAST.clear()
    de = _get(c, "?datei=events&sprache=de")[1]["data"]["eintraege"][0]["befunde"][0]
    assert de["meldung"] == "nominal=20 ist 4.0× so hoch wie der Vanilla-Wert 5" and de["feld"] == en["feld"]


def test_route_fehler_bei_unbekannter_datei_fehlender_datei_und_ohne_mission_ordner(server):
    assert _get(server._conns["1000"], "?datei=globals")[0] == 400
    del server._conns["1000"].ftp.files["/mission/db/events.xml"]
    st, j = _get(server._conns["1000"], "?datei=events")
    assert st == 502 and "db/events.xml" in j["error"]
    st, j = _get(server._conns["3000"], "?datei=types")
    assert st == 200 and j["data"]["kein_mission_ordner"] is True and "eintraege" not in j["data"]


def test_route_kaputtes_xml_auf_dem_server(server):
    server._conns["1000"].ftp.files["/mission/db/types.xml"] = "<types><type name=\"a\"></types>"
    st, j = _get(server._conns["1000"], "?datei=types")
    assert st == 422 and j["code"] == "xml_kaputt"


def test_route_abfragen_haben_einen_spam_schutz(server):
    assert _get(server._conns["1000"], "?datei=types")[0] == 200
    # gleicher Kontoschluessel nur mit derselben Sitzung: zweite Abfrage mit eigener Sitzung ist ein anderes Konto
    cookies = _sitzung(server._conns["1000"])
    req = lambda: make_mocked_request("GET", "/api/tools/performanceoptimizer?datei=events", headers=cookies)
    assert asyncio.run(bot.api_tools_performanceoptimizer_get(req())).status == 200
    assert asyncio.run(bot.api_tools_performanceoptimizer_get(req())).status == 429


def test_upload_erkennt_den_modus_am_dateinamen(monkeypatch, server):
    c = server._conns["1000"]
    st, j = _post(monkeypatch, c, {"datei": "types_neu.xml", "xml": _types(_t("A", nominal="45")), "sprache": "en"})
    assert st == 200 and j["data"]["modus"] == "types" and j["data"]["datei"] == "types_neu.xml"
    bot._DASH_RATE_LIMIT_LAST.clear()                                   # sonst greift der Spam-Schutz zwischen zwei Uploads
    st, j = _post(monkeypatch, c, {"datei": "Meine_EVENT_liste.xml", "xml": "<events>" + _e("A", nominal="50") + "</events>"})
    assert st == 200 and j["data"]["modus"] == "events" and j["data"]["eintraege"][0]["grad"] == "critical"


def test_upload_fehlerfaelle(monkeypatch, server):
    c = server._conns["1000"]
    st, j = _post(monkeypatch, c, {"datei": "notizen.txt", "xml": "hallo"})
    assert st == 400 and j["code"] == "keine_xml"
    st, j = _post(monkeypatch, c, {"datei": "Types_GROSS.XML", "xml": _types(_t("A"))})
    assert st == 400 and j["code"] == "keine_xml"                       # die Endung zaehlt klein geschrieben, wie auf der Seite
    st, j = _post(monkeypatch, c, {"datei": "types.xml"})
    assert st == 400
    st, j = _post(monkeypatch, c, {"datei": "types.xml", "xml": "x" * (6 * 1024 * 1024 + 1)})
    assert st == 413
    st, j = _post(monkeypatch, c, {"datei": "kaputt_types.xml", "xml": "<types><type name=\"a\"></types>"})
    assert st == 422 and j["code"] == "xml_kaputt" and j["datei"] == "kaputt_types.xml"
    st, j = _post(monkeypatch, c, {"datei": "leer_types.xml", "xml": ""})
    assert st == 422


def test_ein_grosser_upload_liefert_die_ganze_datei(monkeypatch, server):
    st, j = _post(monkeypatch, server._conns["1000"], {"datei": "types.xml", "xml": _lesen("types-brigarde-2026-10-26.xml"), "sprache": "en"})
    z = j["data"]["zusammenfassung"]
    assert st == 200 and (z["criticals"], z["warnings"], z["ok"], z["total"], z["custom"]) == (192, 538, 1681, 2411, 471)
