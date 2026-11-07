"""Karte (Dashboard -> Karte): Metadaten an Ereignissen, Positionsverlauf (Spur), Spielerliste mit Statistik, Spur-Endpunkt
und die Kachelquellen fuer den Stil-Umschalter.

Die Mandantentrennung ist der Kern: Positionen, Ereignisse und Spuren eines Servers duerfen nie bei einem anderen ankommen,
und Spieler mit "Location Privacy" tauchen nirgends auf.

    python3 -m pytest tests/test_karte.py -q
"""
import asyncio
import json
import os
import sys
import time
from collections import deque

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")
from log_parser import DayZLogParser  # noqa: E402


@pytest.fixture
def welt(monkeypatch, tmp_path):
    """Server 1000 und Server 2000 mit eigenem Parser, eigener Datenbank und leerem Ereignispuffer."""
    monkeypatch.chdir(tmp_path)
    flat = {sid: {"service_id": sid, "nitrado_token": "x", "owner_discord_id": sid, "map_name": "Livonia"} for sid in ("1000", "2000")}
    (tmp_path / "connections.json").write_text(json.dumps(flat))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    monkeypatch.setattr(bot, "_KARTE_STATS_CACHE", {})
    monkeypatch.setattr(bot, "_EV_BUF", deque(maxlen=bot.EV_MAX_EVENTS))
    monkeypatch.setattr(bot, "_ev_next_id", 1)
    monkeypatch.setattr(bot, "db", bot.EconomyDB(str(tmp_path / "eco.db")))
    a, b = reg._conns["1000"], reg._conns["2000"]
    reg.add_guild("1000", 7301)
    reg.add_guild("2000", 7302)
    a.data["kunden_stufe"] = b.data["kunden_stufe"] = "premium"
    a.parser, b.parser = DayZLogParser(), DayZLogParser()
    w = type("Welt", (), {})()
    w.a, w.b = a, b
    return w


def _api(monkeypatch, conn, handler, query="", admin=True, gast=None):
    sid = str(time.time_ns())
    discord_id = gast or (sid if admin else str(conn.data["owner_discord_id"]))
    sess = {"token": "x", "discord": {"id": discord_id}, "is_admin": admin and not gast, "service_id": conn.service_id,
            "seen": time.time(), "admin_geprueft_ts": time.time()}
    if gast:
        sess["is_guest"] = True
    bot._SESS_STORE[sid] = sess
    req = make_mocked_request("GET", "/api/map/x" + query, headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    r = asyncio.run(handler(req))
    return r.status, json.loads(r.body)


def _pos(parser, name, x, z, alter=0.0):
    parser.player_positions[name] = {"id": "i", "position": f"{x}, {z}, 190.0", "last_seen": "2026-11-07T16:00:00+00:00", "quelle": "position"}


def _event(sid, typ, player, x, z, ts, **extra):
    ev = {"type": typ, "timestamp": "12:00:00", "player": player, "pos": f"<{x}, {z}, 190.0>", "raw": "zeile"}
    ev.update(extra)
    bot._ev_record(ev, None, sid)
    bot._EV_BUF[-1]["ts"] = ts


# ── Metadaten an Ereignissen ────────────────────────────────────────────────
def test_meta_enthaelt_einfache_felder_aber_nicht_die_rohzeile():
    meta = bot._ev_meta({"type": "basebuild", "player": "A", "item": "door", "raw": "ROH", "n": 3, "f": 1.5, "ok": True, "leer": None})
    assert meta == {"type": "basebuild", "player": "A", "item": "door", "n": 3, "f": 1.5, "ok": True, "leer": None}


def test_meta_kuerzt_texte_und_ignoriert_verschachteltes_und_zu_viele_felder():
    meta = bot._ev_meta({"lang": "x" * 500, "liste": [1, 2], "dict": {"a": 1}, **{f"k{i}": i for i in range(40)}})
    assert len(meta["lang"]) == 200 and "liste" not in meta and "dict" not in meta
    assert len(meta) == bot._EV_META_MAX_FELDER
    assert bot._ev_meta({"": 1, "k" * 41: 2, 5: 3, "ok": 4}) == {"ok": 4}


@pytest.mark.parametrize("pos,erwartet", [
    ("<11314.7, 12149.0, 188.4>", {"x": 11314.7, "y": 188.4, "z": 12149.0}),
    ("11314.7, 12149, 188.4", {"x": 11314.7, "y": 188.4, "z": 12149.0}),
    ("<11314.7, 188.4>", None), ("", None), (None, None), ("kaputt", None)])
def test_meta_hoehe_aus_der_position(pos, erwartet):
    meta = bot._ev_meta({"type": "connect", "pos": pos})
    assert meta.get("player_location") == erwartet


def test_ereignis_speichert_meta_und_bleibt_json_tauglich(welt):
    _event("1000", "basebuild", "ChrisKYF", 11314.7, 12149.0, time.time(), item="window10_wood", tool="Hatchet")
    rec = bot._EV_BUF[-1]
    assert rec["meta"]["item"] == "window10_wood" and rec["meta"]["player_location"] == {"x": 11314.7, "y": 190.0, "z": 12149.0}
    assert (rec["x"], rec["z"]) == (11314.7, 12149.0)                                  # Karte und Metadaten stimmen ueberein
    assert "raw" not in rec["meta"] and rec["raw"] == "zeile"
    json.dumps(list(bot._EV_BUF))                       # wird so auch auf die Platte geschrieben


# ── Positionsverlauf im Parser ──────────────────────────────────────────────
def test_verlauf_merkt_positionen_und_keine_doppelten():
    p = DayZLogParser()
    p._set_position("A", "i", "100.0, 200.0, 10.0")
    p._set_position("A", "i", "100.0, 200.0, 10.0")                # gleicher Ort wie zuletzt: nicht doppelt
    p._set_position("A", "i", "110.5, 205.5, 10.0")
    assert [(x, z) for _t, x, z in p.positions_verlauf["A"]] == [(100.0, 200.0), (110.5, 205.5)]
    assert all(isinstance(t, float) for t, _x, _z in p.positions_verlauf["A"])


@pytest.mark.parametrize("pos", ["", "abc", "12"])
def test_verlauf_ignoriert_unbrauchbare_positionen(pos):
    p = DayZLogParser()
    p._set_position("A", "i", pos)
    assert "A" not in p.positions_verlauf or not p.positions_verlauf["A"]


def test_verlauf_ist_je_spieler_begrenzt():
    p = DayZLogParser()
    for i in range(DayZLogParser.POSITIONS_VERLAUF_MAX + 50):
        p._set_position("A", "i", f"{i}.0, 1.0, {i}.0")
    assert len(p.positions_verlauf["A"]) == DayZLogParser.POSITIONS_VERLAUF_MAX
    assert p.positions_verlauf["A"][-1][1] == DayZLogParser.POSITIONS_VERLAUF_MAX + 49.0      # die neuesten bleiben


def test_verlauf_begrenzt_die_zahl_der_spieler_und_wirft_den_aeltesten_raus(monkeypatch):
    monkeypatch.setattr(DayZLogParser, "POSITIONS_VERLAUF_SPIELER_MAX", 5)
    p = DayZLogParser()
    for i in range(5):
        p._set_position(f"S{i}", "i", f"{i}.0, 1.0, 1.0")
        p.positions_verlauf[f"S{i}"][-1] = (1000.0 + i, float(i), 1.0)                      # S0 ist der aelteste
    p._set_position("NEU", "i", "9.0, 1.0, 9.0")
    assert "S0" not in p.positions_verlauf and "NEU" in p.positions_verlauf and len(p.positions_verlauf) == 5


def test_zwei_parser_teilen_keinen_verlauf():
    a, b = DayZLogParser(), DayZLogParser()
    a._set_position("A", "i", "1.0, 1.0, 1.0")
    assert b.positions_verlauf == {} and a.positions_verlauf is not b.positions_verlauf


def test_adm_zeile_fuellt_den_verlauf():
    p = DayZLogParser()
    p.parse_line('12:00:00 | Player "ChrisKYF" (id=abc pos=<9400.5, 7700.2, 190.0>) built door5_metal on Wall with Hatchet')
    assert [(x, z) for _t, x, z in p.positions_verlauf["ChrisKYF"]] == [(9400.5, 7700.2)]


# ── Spielerliste ────────────────────────────────────────────────────────────
def test_spielerliste_hat_statistik_und_verlaufspunkte(monkeypatch, welt):
    _pos(welt.a.parser, "ChrisKYF", 9400.0, 7700.0)
    welt.a.parser._set_position("ChrisKYF", "i", "9400.0, 7700.0, 190.0")
    for k in range(3):
        bot.db.record_kill("1000", "ChrisKYF", "i1", f"Opfer{k}", "i2", "M4", 100)
    bot.db.record_kill("1000", "Anderer", "i3", "ChrisKYF", "i1", "Axe", 5)
    st, j = _api(monkeypatch, welt.a, bot.api_map_players)
    assert st == 200
    p = j["data"]["players"][0]
    assert p["name"] == "ChrisKYF" and (p["kills"], p["deaths"], p["kd"]) == (3, 1, 3.0) and p["trail_points"] == 1


def test_spielerliste_ohne_kills_zeigt_nullen_und_ohne_spieler_bleibt_leer(monkeypatch, welt):
    assert _api(monkeypatch, welt.a, bot.api_map_players)[1]["data"]["players"] == []
    _pos(welt.a.parser, "Neu", 1.0, 2.0)
    p = _api(monkeypatch, welt.a, bot.api_map_players)[1]["data"]["players"][0]
    assert (p["kills"], p["deaths"], p["kd"], p["trail_points"]) == (0, 0, 0.0, 0)


def test_statistik_kommt_nur_vom_eigenen_server(monkeypatch, welt):
    _pos(welt.a.parser, "ChrisKYF", 1.0, 2.0)
    bot.db.record_kill("2000", "ChrisKYF", "i1", "X", "i2", "M4", 1)                     # Kills auf dem anderen Server zaehlen nicht
    assert _api(monkeypatch, welt.a, bot.api_map_players)[1]["data"]["players"][0]["kills"] == 0


def test_statistik_wird_kurz_zwischengespeichert(monkeypatch, welt):
    _pos(welt.a.parser, "ChrisKYF", 1.0, 2.0)
    assert _api(monkeypatch, welt.a, bot.api_map_players)[1]["data"]["players"][0]["kills"] == 0
    bot.db.record_kill("1000", "ChrisKYF", "i1", "X", "i2", "M4", 1)
    assert _api(monkeypatch, welt.a, bot.api_map_players)[1]["data"]["players"][0]["kills"] == 0      # noch aus dem Zwischenspeicher
    monkeypatch.setattr(bot, "_KARTE_STATS_TTL", 0.0)
    assert _api(monkeypatch, welt.a, bot.api_map_players)[1]["data"]["players"][0]["kills"] == 1


def test_statistik_zwischenspeicher_bleibt_begrenzt(monkeypatch, welt):
    monkeypatch.setattr(bot, "_KARTE_STATS_MAX", 10)
    bot._karten_spielerstats("1000", [f"S{i}" for i in range(50)])
    assert len(bot._KARTE_STATS_CACHE) <= 11


def test_spielerliste_zeigt_nie_positionen_eines_anderen_servers(monkeypatch, welt):
    _pos(welt.a.parser, "AlleinAufA", 1.0, 2.0)
    _pos(welt.b.parser, "AlleinAufB", 3.0, 4.0)
    assert [p["name"] for p in _api(monkeypatch, welt.a, bot.api_map_players)[1]["data"]["players"]] == ["AlleinAufA"]
    assert [p["name"] for p in _api(monkeypatch, welt.b, bot.api_map_players)[1]["data"]["players"]] == ["AlleinAufB"]


def test_location_privacy_gilt_fuer_die_liste(monkeypatch, welt):
    _pos(welt.a.parser, "Geheim", 1.0, 2.0)
    _pos(welt.a.parser, "Offen", 3.0, 4.0)
    welt.a.data["location_privacy_names"] = ["geheim"]
    assert [p["name"] for p in _api(monkeypatch, welt.a, bot.api_map_players)[1]["data"]["players"]] == ["Offen"]


# ── Spur ────────────────────────────────────────────────────────────────────
def _verlauf(parser, name, punkte):
    parser.positions_verlauf[name] = deque(punkte, maxlen=240)


def test_spur_verbindet_verlauf_und_ereignisse_zeitlich_geordnet_ohne_doppelte(monkeypatch, welt):
    jetzt = time.time()
    _verlauf(welt.a.parser, "ChrisKYF", [(jetzt - 300, 100.0, 100.0), (jetzt - 100, 120.0, 120.0)])
    _event("1000", "basebuild", "ChrisKYF", 110.0, 110.0, jetzt - 200)
    _event("1000", "basebuild", "ChrisKYF", 120.0, 120.0, jetzt - 50)                 # gleicher Ort wie der Verlaufspunkt davor
    st, j = _api(monkeypatch, welt.a, bot.api_map_trail, "?player=ChrisKYF")
    assert st == 200
    assert [(p["x"], p["z"]) for p in j["data"]["points"]] == [(100.0, 100.0), (110.0, 110.0), (120.0, 120.0)]
    assert [p["t"] for p in j["data"]["points"]] == sorted(p["t"] for p in j["data"]["points"])


def test_spur_name_ist_unabhaengig_von_gross_und_kleinschreibung(monkeypatch, welt):
    _verlauf(welt.a.parser, "ChrisKYF", [(time.time() - 10, 1.0, 2.0)])
    assert len(_api(monkeypatch, welt.a, bot.api_map_trail, "?player=chriskyf")[1]["data"]["points"]) == 1


def test_spur_zeitfenster_und_grenzen(monkeypatch, welt):
    jetzt = time.time()
    _verlauf(welt.a.parser, "A", [(jetzt - 8 * 3600, 1.0, 1.0), (jetzt - 600, 2.0, 2.0), (jetzt - 30, 3.0, 3.0)])
    def punkte(q):
        return [(p["x"]) for p in _api(monkeypatch, welt.a, bot.api_map_trail, "?player=A" + q)[1]["data"]["points"]]
    assert punkte("&minutes=5") == [3.0]
    assert punkte("&minutes=60") == [2.0, 3.0]
    assert punkte("&minutes=1440") == [1.0, 2.0, 3.0]
    assert punkte("") == [2.0, 3.0]                                                  # Standard: 6 Stunden
    assert punkte("&minutes=abc") == [2.0, 3.0]
    assert _api(monkeypatch, welt.a, bot.api_map_trail, "?player=A&minutes=99999")[1]["data"]["minutes"] == 1440
    assert _api(monkeypatch, welt.a, bot.api_map_trail, "?player=A&minutes=1")[1]["data"]["minutes"] == 5


def test_spur_nimmt_ereignisse_ausserhalb_des_zeitfensters_nicht_mit(monkeypatch, welt):
    jetzt = time.time()
    _event("1000", "basebuild", "A", 1.0, 1.0, jetzt - 8 * 3600)
    _event("1000", "basebuild", "A", 2.0, 2.0, jetzt - 60)
    assert [p["x"] for p in _api(monkeypatch, welt.a, bot.api_map_trail, "?player=A")[1]["data"]["points"]] == [2.0]


@pytest.mark.parametrize("query", ["", "?player=", "?player=%20%20", "?player=" + "x" * 65])
def test_spur_ohne_gueltigen_namen_ist_400(monkeypatch, welt, query):
    assert _api(monkeypatch, welt.a, bot.api_map_trail, query)[0] == 400


def test_spur_ist_auf_500_punkte_begrenzt_und_behaelt_die_neuesten(monkeypatch, welt):
    jetzt = time.time()
    _verlauf(welt.a.parser, "A", [(jetzt - 3000 + i, float(i), float(i)) for i in range(240)])
    for i in range(400):
        _event("1000", "basebuild", "A", 1000.0 + i, 2000.0 + i, jetzt - 2000 + i)
    punkte = _api(monkeypatch, welt.a, bot.api_map_trail, "?player=A")[1]["data"]["points"]
    assert len(punkte) == bot._KARTE_SPUR_MAX_PUNKTE and punkte[-1]["x"] == 1399.0


def test_spur_nie_vom_anderen_server(monkeypatch, welt):
    jetzt = time.time()
    _verlauf(welt.b.parser, "ChrisKYF", [(jetzt - 10, 9.0, 9.0)])
    _event("2000", "basebuild", "ChrisKYF", 8.0, 8.0, jetzt - 20)
    j = _api(monkeypatch, welt.a, bot.api_map_trail, "?player=ChrisKYF")[1]["data"]
    assert j["points"] == []
    assert len(_api(monkeypatch, welt.b, bot.api_map_trail, "?player=ChrisKYF")[1]["data"]["points"]) == 2


def test_location_privacy_gilt_auch_fuer_die_spur(monkeypatch, welt):
    _verlauf(welt.a.parser, "Geheim", [(time.time() - 10, 1.0, 2.0)])
    _event("1000", "basebuild", "Geheim", 5.0, 6.0, time.time() - 5)
    welt.a.data["location_privacy_names"] = ["GEHEIM"]
    assert _api(monkeypatch, welt.a, bot.api_map_trail, "?player=Geheim")[1]["data"]["points"] == []


def test_spur_ohne_parser_oder_verlauf_ist_leer(monkeypatch, welt):
    welt.a.parser = None
    assert _api(monkeypatch, welt.a, bot.api_map_trail, "?player=A")[1]["data"]["points"] == []


# ── Rechte ──────────────────────────────────────────────────────────────────
def test_gast_braucht_das_kartenrecht(monkeypatch, welt):
    for handler, q in ((bot.api_map_players, ""), (bot.api_map_trail, "?player=A")):
        assert _api(monkeypatch, welt.a, handler, q, gast="900")[0] == 403
    welt.a.data["dashboard_perms"] = {"user:900": {"map": ["view"]}}
    for handler, q in ((bot.api_map_players, ""), (bot.api_map_trail, "?player=A")):
        assert _api(monkeypatch, welt.a, handler, q, gast="900")[0] == 200


def test_kunde_besitzer_darf_die_karte_seines_servers_sehen(monkeypatch, welt):
    assert _api(monkeypatch, welt.a, bot.api_map_trail, "?player=A", admin=False)[0] == 200


# ── Kachelquellen ───────────────────────────────────────────────────────────
def test_meta_nennt_beide_kachelstile_mit_echter_zoomstufe(welt):
    welt.a.data["map_name"] = "Nasdara"
    meta = bot._map_meta_daten(welt.a)
    v = meta["tile_varianten"]
    assert set(v) == {"sat", "topo"} and v["sat"]["url"] != v["topo"]["url"]
    assert v["sat"]["max_native"] == 6 and v["topo"]["max_native"] == 4
    assert meta["tile_style"] in v and meta["tile_url"] == v[meta["tile_style"]]["url"]


def test_meta_normale_karte_hat_beide_stile_auf_den_xam_adressen(welt):
    v = bot._map_meta_daten(welt.a)["tile_varianten"]
    assert "/satellite/" in v["sat"]["url"] and "/topographic/" in v["topo"]["url"]
    assert v["sat"]["max_native"] == v["topo"]["max_native"] == bot.TILE_MAX_NATIVE_ZOOM


def test_eigene_kachel_adresse_ersetzt_beide_stile(monkeypatch, welt):
    monkeypatch.setitem(bot.cfg.config, "dashboard_map_tiles", {"Livonia": "https://tiles.example.com/{z}/{x}/{y}.png"})
    v = bot._map_meta_daten(welt.a)["tile_varianten"]
    assert v["sat"]["url"] == v["topo"]["url"] == "https://tiles.example.com/{z}/{x}/{y}.png"      # gleiche Adresse: kein Umschalter
