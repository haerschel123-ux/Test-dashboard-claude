"""Live-Karte für Spieler: wer die Positionen sehen darf (Team / Rollen / alle), Verzögerung, Privatsphäre, Mandanten-Trennung.

Der Betreiber stellt sie im Dashboard (Discord Management) ein; Standard ist "nur Team" - dann sieht ein Spieler nichts.
Es geht kein Test ins Internet oder zu Discord.

    python3 -m pytest tests/test_livekarte.py -q
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from log_parser import DayZLogParser
from test_spieler_dashboard import G_A, G_B, U, ANDERER, ROLLE_VIP, ROLLE_ANDERE, _session, api, welt  # noqa: F401 - Fixtures
from test_airstrike import servers  # noqa: F401 - Fixture (zwei Server 1000/2000)


@pytest.fixture(autouse=True)
def _frisch(monkeypatch):
    monkeypatch.setattr(bot, "_audit_log", bot.deque(maxlen=100))


@pytest.fixture
def lk(welt):
    welt["a"].parser, welt["b"].parser = DayZLogParser(), DayZLogParser()
    return welt


def _pos(conn, name, x, z, verlauf=()):
    """Setzt eine aktuelle Position und optional einen Verlauf [(Sekunden zurueck, x, z), ...]."""
    conn.parser.player_positions[name] = {"id": "i", "position": f"{x}, {z}, 190.0", "last_seen": "2026-11-07T16:00:00+00:00", "quelle": "position"}
    from collections import deque
    jetzt = time.time()
    conn.parser.positions_verlauf[name] = deque([(jetzt - s, vx, vz) for s, vx, vz in verlauf] or [(jetzt, x, z)], maxlen=240)


def _einstellen(welt, modus="alle", conn="a", gid=G_A, **kw):
    daten = {"modus": modus, "role_ids": [], "verzoegerung": 0, "nur_verknuepft": False}
    daten.update(kw)
    bot._conn_store(bot.guild_sicht(welt[conn], gid), "live_karte", daten)


def _s():
    return _session(gid=G_A, sid_="1000")


# ── Einstellungen laden ─────────────────────────────────────────────────────
class _Fake:
    def __init__(self, d):
        self.d = d

    def get(self, key, default=None):
        return self.d.get(key, default)


def test_standard_ist_nur_team():
    assert bot._livekarte_einstellungen(_Fake({})) == {"modus": "team", "role_ids": [], "verzoegerung": 0, "nur_verknuepft": True}


@pytest.mark.parametrize("roh", [None, "x", 5, [], {"modus": "alle "}, {"modus": "ALLE"}, {"modus": 1}, {"modus": None}, {"modus": ["alle"]}])
def test_kaputter_oder_unbekannter_modus_faellt_auf_team(roh):
    assert bot._livekarte_einstellungen(_Fake({"live_karte": roh}))["modus"] == "team"


def test_einstellungen_bereinigen():
    e = bot._livekarte_einstellungen(_Fake({"live_karte": {"modus": "rollen", "role_ids": ["5", 5, "x", "-3", True, None, "0", "7" * 21, " 9 "],
                                                          "verzoegerung": 60, "nur_verknuepft": 0}}))
    assert e == {"modus": "rollen", "role_ids": ["5", "9"], "verzoegerung": 60, "nur_verknuepft": False}


@pytest.mark.parametrize("verz", [-1, 1, 45, 301, 3600, "60", 60.0, True, None])
def test_ungueltige_verzoegerung_wird_zu_null(verz):
    assert bot._livekarte_einstellungen(_Fake({"live_karte": {"modus": "alle", "verzoegerung": verz}}))["verzoegerung"] == 0


@pytest.mark.parametrize("verz", [0, 30, 60, 120, 300])
def test_gueltige_verzoegerungen(verz):
    assert bot._livekarte_einstellungen(_Fake({"live_karte": {"modus": "alle", "verzoegerung": verz}}))["verzoegerung"] == verz


def test_hoechstens_25_rollen_beim_laden():
    e = bot._livekarte_einstellungen(_Fake({"live_karte": {"modus": "rollen", "role_ids": [str(i + 1) for i in range(40)]}}))
    assert len(e["role_ids"]) == 25


# ── Wer darf sehen ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("modus,rollen,verknuepft,nur_v,erwartet", [
    ("team", {5}, True, False, (False, "team")),
    ("team", set(), False, True, (False, "team")),
    ("alle", set(), False, False, (True, "")),
    ("alle", set(), True, True, (True, "")),
    ("alle", set(), False, True, (False, "verknuepfung")),
    ("rollen", {5}, True, True, (True, "")),
    ("rollen", {5}, False, False, (True, "")),
    ("rollen", {6}, True, False, (False, "rolle")),
    ("rollen", set(), True, False, (False, "rolle")),
    ("rollen", {5}, False, True, (False, "verknuepfung"))])
def test_erlaubt_matrix(welt, modus, rollen, verknuepft, nur_v, erwartet):
    if verknuepft:
        bot.db.link_user(G_A, U, "Spieler1")
    e = {"modus": modus, "role_ids": ["5"], "verzoegerung": 0, "nur_verknuepft": nur_v}
    assert bot._livekarte_erlaubt(e, G_A, U, rollen) == erwartet


def test_verknuepfung_gilt_je_guild(welt):
    bot.db.link_user(G_B, U, "Spieler1")
    e = {"modus": "alle", "role_ids": [], "verzoegerung": 0, "nur_verknuepft": True}
    assert bot._livekarte_erlaubt(e, G_A, U, set()) == (False, "verknuepfung")
    assert bot._livekarte_erlaubt(e, G_B, U, set()) == (True, "")


# ── Spielerliste ────────────────────────────────────────────────────────────
def test_spieler_ohne_stats_und_spur(lk):
    _pos(lk["a"], "Anna", 4640.0, 10350.0)
    s = bot._livekarte_spieler(lk["a"], 0, set(), time.time())
    assert len(s) == 1 and set(s[0]) == {"name", "x", "z", "near", "ich"}
    assert (s[0]["name"], s[0]["x"], s[0]["z"], s[0]["ich"]) == ("Anna", 4640.0, 10350.0, False)


def test_location_privacy_versteckt_bei_anderen_aber_nicht_bei_sich_selbst(lk):
    _pos(lk["a"], "Geheim", 100.0, 200.0)
    _pos(lk["a"], "Offen", 300.0, 400.0)
    lk["a"].data["location_privacy_names"] = ["geheim"]
    assert [p["name"] for p in bot._livekarte_spieler(lk["a"], 0, set(), time.time())] == ["Offen"]
    mit = bot._livekarte_spieler(lk["a"], 0, {"geheim"}, time.time())
    assert [(p["name"], p["ich"]) for p in mit] == [("Geheim", True), ("Offen", False)]


def test_eigene_zuerst_dann_alphabetisch(lk):
    for n in ("zed", "Berta", "anton", "Mich"):
        _pos(lk["a"], n, 1.0, 2.0)
    assert [p["name"] for p in bot._livekarte_spieler(lk["a"], 0, {"mich"}, time.time())] == ["Mich", "anton", "Berta", "zed"]


def test_verzoegerung_zeigt_die_position_von_vor_x_sekunden(lk):
    _pos(lk["a"], "Anna", 900.0, 900.0, verlauf=[(200, 100.0, 100.0), (90, 500.0, 500.0), (10, 900.0, 900.0)])
    assert bot._livekarte_spieler(lk["a"], 0, set(), time.time())[0]["x"] == 900.0
    assert bot._livekarte_spieler(lk["a"], 30, set(), time.time())[0]["x"] == 500.0
    assert bot._livekarte_spieler(lk["a"], 60, set(), time.time())[0]["x"] == 500.0
    assert bot._livekarte_spieler(lk["a"], 120, set(), time.time())[0]["x"] == 100.0
    assert bot._livekarte_spieler(lk["a"], 300, set(), time.time()) == []            # aelteste bekannte Position ist erst 200 s alt


def test_verzoegerung_neuer_spieler_noch_unsichtbar_eigene_position_sofort(lk):
    _pos(lk["a"], "Neu", 5.0, 6.0, verlauf=[(5, 5.0, 6.0)])
    assert bot._livekarte_spieler(lk["a"], 60, set(), time.time()) == []
    assert bot._livekarte_spieler(lk["a"], 60, {"neu"}, time.time())[0]["x"] == 5.0


def test_verzoegerung_ohne_verlauf_zeigt_nichts(lk):
    _pos(lk["a"], "Anna", 1.0, 2.0)
    lk["a"].parser.positions_verlauf.clear()
    assert bot._livekarte_spieler(lk["a"], 30, set(), time.time()) == []
    assert len(bot._livekarte_spieler(lk["a"], 0, set(), time.time())) == 1


def test_verzoegerung_steht_der_spieler_still_gilt_der_alte_punkt(lk):
    _pos(lk["a"], "Statue", 7.0, 8.0, verlauf=[(4000, 7.0, 8.0)])          # seit ueber einer Stunde unveraendert
    assert bot._livekarte_spieler(lk["a"], 300, set(), time.time())[0]["x"] == 7.0


def test_kaputte_eintraege_werden_uebersprungen(lk):
    lk["a"].parser.player_positions["Kaputt"] = {"position": "keine zahlen"}
    lk["a"].parser.player_positions["Leer"] = "kein dict"
    lk["a"].parser.player_positions["Eine"] = {"position": "12.5"}
    _pos(lk["a"], "Gut", 1.0, 2.0)
    assert [p["name"] for p in bot._livekarte_spieler(lk["a"], 0, set(), time.time())] == ["Gut"]


def test_hoechstens_300_spieler(lk):
    for i in range(320):
        _pos(lk["a"], f"P{i:03d}", 1.0, 2.0)
    assert len(bot._livekarte_spieler(lk["a"], 0, set(), time.time())) == 300


def test_ohne_parser_keine_spieler(lk):
    lk["a"].parser = None
    assert bot._livekarte_spieler(lk["a"], 0, set(), time.time()) == []


# ── Spieler-API ─────────────────────────────────────────────────────────────
def test_api_ohne_login_und_ohne_server(lk):
    assert api("GET", "/api/me/livekarte")[0] in (401, 403)
    assert api("GET", "/api/me/livekarte", _session())[0] == 409


def test_api_standard_team_zeigt_nichts_und_verraet_keine_spieler(lk):
    _pos(lk["a"], "Anna", 4640.0, 10350.0)
    st, d = api("GET", "/api/me/livekarte", _s())
    assert st == 200 and d["data"] == {"erlaubt": False, "grund": "team"} and "Anna" not in json.dumps(d)


def test_api_alle_zeigt_die_spieler(lk):
    _einstellen(lk, "alle")
    _pos(lk["a"], "Anna", 4640.0, 10350.0)
    st, d = api("GET", "/api/me/livekarte", _s())
    assert st == 200 and d["data"]["erlaubt"] is True and d["data"]["map_name"] == "Livonia" and d["data"]["verzoegerung"] == 0
    assert [(p["name"], p["x"], p["z"]) for p in d["data"]["players"]] == [("Anna", 4640.0, 10350.0)]


def test_api_nur_verknuepfte_und_eigene_markierung(lk):
    _einstellen(lk, "alle", nur_verknuepft=True)
    _pos(lk["a"], "Anna", 1.0, 2.0)
    _pos(lk["a"], "Spieler1", 3.0, 4.0)
    d = api("GET", "/api/me/livekarte", _s())[1]["data"]
    assert d == {"erlaubt": False, "grund": "verknuepfung"}
    bot.db.link_user(G_A, U, "SPIELER1")                                            # Schreibweise wie im Log egal
    d = api("GET", "/api/me/livekarte", _s())[1]["data"]
    assert [(p["name"], p["ich"]) for p in d["players"]] == [("Spieler1", True), ("Anna", False)]


def test_api_rollen_modus(lk):
    _einstellen(lk, "rollen", role_ids=[str(ROLLE_VIP)])
    _pos(lk["a"], "Anna", 1.0, 2.0)
    assert api("GET", "/api/me/livekarte", _s())[1]["data"] == {"erlaubt": False, "grund": "rolle"}      # U hat die Rolle nicht
    anderer = _session(uid=ANDERER, gid=G_A, sid_="1000")                                                 # ANDERER hat ROLLE_VIP
    d = api("GET", "/api/me/livekarte", anderer)[1]["data"]
    assert d["erlaubt"] is True and [p["name"] for p in d["players"]] == ["Anna"]


def test_api_andere_rolle_reicht_nicht(lk):
    _einstellen(lk, "rollen", role_ids=[str(ROLLE_ANDERE)])
    assert api("GET", "/api/me/livekarte", _session(uid=ANDERER, gid=G_A, sid_="1000"))[1]["data"]["grund"] == "rolle"


def test_api_verzoegerung_und_privatsphaere(lk):
    _einstellen(lk, "alle", verzoegerung=60)
    _pos(lk["a"], "Alt", 11.0, 12.0, verlauf=[(500, 11.0, 12.0)])
    _pos(lk["a"], "Neu", 21.0, 22.0, verlauf=[(5, 21.0, 22.0)])
    _pos(lk["a"], "Privat", 31.0, 32.0, verlauf=[(500, 31.0, 32.0)])
    lk["a"].data["location_privacy_names"] = ["Privat"]
    d = api("GET", "/api/me/livekarte", _s())[1]["data"]
    assert d["verzoegerung"] == 60 and [p["name"] for p in d["players"]] == ["Alt"]


def test_api_server_getrennt(lk):
    """Ein Spieler von Guild B sieht nie die Positionen von Server A - auch nicht, wenn A 'alle' erlaubt."""
    _einstellen(lk, "alle")
    _pos(lk["a"], "NurAufA", 1.0, 2.0)
    _pos(lk["b"], "NurAufB", 3.0, 4.0)
    sb = _session(gid=G_B, sid_="2000")
    assert api("GET", "/api/me/livekarte", sb)[1]["data"] == {"erlaubt": False, "grund": "team"}           # B hat nichts freigegeben
    _einstellen(lk, "alle", conn="b", gid=G_B)
    d = api("GET", "/api/me/livekarte", sb)[1]["data"]
    assert [p["name"] for p in d["players"]] == ["NurAufB"]
    assert [p["name"] for p in api("GET", "/api/me/livekarte", _s())[1]["data"]["players"]] == ["NurAufA"]


def test_api_manipulierte_sitzung_fremder_server(lk):
    """Session zeigt auf Guild A, aber den Server B: wird abgelehnt statt B's Positionen zu liefern."""
    _einstellen(lk, "alle", conn="b", gid=G_B)
    _pos(lk["b"], "NurAufB", 3.0, 4.0)
    st, d = api("GET", "/api/me/livekarte", _session(gid=G_A, sid_="2000"))
    assert st in (403, 409) and "NurAufB" not in json.dumps(d)


# ── Dashboard-API ───────────────────────────────────────────────────────────
def _admin(welt, gid=G_A, conn="a", admin=True, gast=None):
    sid = str(time.time_ns())
    c = welt[conn]
    bot._SESS_STORE[sid] = {"token": "x", "discord": {"id": gast or (sid if admin else str(c.data["owner_discord_id"]))},
                            "is_admin": admin and not gast, "service_id": c.service_id, "seen": time.time(),
                            "admin_geprueft_ts": time.time(), "guild_id": str(gid), **({"is_guest": True} if gast else {})}
    c.data["kunden_stufe"] = "premium"
    return sid


def _mgmt(welt, methode, daten=None, **kw):
    return api(methode, "/api/discord-management/livekarte", _admin(welt, **kw), daten)


def _speichern(welt, **kw):
    daten = {"modus": "alle", "role_ids": [], "verzoegerung": 0, "nur_verknuepft": True}
    daten.update(kw)
    return _mgmt(welt, "POST", daten)


def test_mgmt_ohne_login_und_als_spieler_gesperrt(lk):
    assert api("GET", "/api/discord-management/livekarte")[0] in (401, 403)
    for methode in ("GET", "POST"):
        assert api(methode, "/api/discord-management/livekarte", _s(), {"modus": "alle"})[0] in (401, 403)


def test_mgmt_lesen_standard(lk):
    st, d = _mgmt(lk, "GET")
    assert st == 200 and d["data"]["modus"] == "team" and d["data"]["limits"]["verzoegerungen"] == [0, 30, 60, 120, 300]


def test_mgmt_speichern_und_lesen(lk):
    st, d = _speichern(lk, modus="alle", verzoegerung=60, nur_verknuepft=False)
    assert st == 200 and (d["data"]["modus"], d["data"]["verzoegerung"], d["data"]["nur_verknuepft"]) == ("alle", 60, False)
    e = bot._livekarte_einstellungen(bot.guild_sicht(lk["a"], G_A))
    assert (e["modus"], e["verzoegerung"], e["nur_verknuepft"]) == ("alle", 60, False)
    assert [z["detail"] for z in bot._audit_log if z["action"] == "Discord-Management aktualisiert"] == ["live_karte · alle · Server 1000"]


def test_mgmt_rollen_werden_nur_im_rollenmodus_gespeichert(lk, monkeypatch):
    monkeypatch.setattr(bot, "_rolle_gehoert_guild", lambda gid, rid, feld="Rolle": None)
    st, d = _speichern(lk, modus="rollen", role_ids=[str(ROLLE_VIP), str(ROLLE_VIP)])
    assert st == 200 and d["data"]["role_ids"] == [str(ROLLE_VIP)]
    st, d = _speichern(lk, modus="alle", role_ids=[str(ROLLE_VIP)])
    assert st == 200 and d["data"]["role_ids"] == []
    st, d = _speichern(lk, modus="team", role_ids=[str(ROLLE_VIP)])
    assert st == 200 and d["data"]["role_ids"] == [] and d["data"]["modus"] == "team"


@pytest.mark.parametrize("daten,teil", [
    ({"modus": "alle ", "nur_verknuepft": True}, "Sichtbarkeit"), ({"modus": None}, "Sichtbarkeit"), ({"modus": 1}, "Sichtbarkeit"),
    ({"modus": ["alle"]}, "Sichtbarkeit"), ({}, "Sichtbarkeit"),
    ({"modus": "alle", "verzoegerung": 45}, "Verzögerung"), ({"modus": "alle", "verzoegerung": "60"}, "Verzögerung"),
    ({"modus": "alle", "verzoegerung": True}, "Verzögerung"), ({"modus": "alle", "verzoegerung": False}, "Verzögerung"), ({"modus": "alle", "verzoegerung": -1}, "Verzögerung"),
    ({"modus": "alle", "role_ids": "x"}, "Höchstens"), ({"modus": "alle", "role_ids": ["1"] * 26}, "Höchstens"),
    ({"modus": "rollen", "role_ids": ["abc"]}, "Rollen-ID"), ({"modus": "rollen", "role_ids": [0]}, "Rollen-ID"),
    ({"modus": "rollen", "role_ids": [True]}, "Rollen-ID"), ({"modus": "rollen", "role_ids": ["-4"]}, "Rollen-ID"),
    ({"modus": "rollen", "role_ids": []}, "mindestens eine Rolle")])
def test_mgmt_ablehnungen_speichern_nichts(lk, daten, teil):
    st, d = _mgmt(lk, "POST", daten)
    assert st == 400 and teil in d["error"], d
    assert bot._livekarte_einstellungen(bot.guild_sicht(lk["a"], G_A))["modus"] == "team"


def test_mgmt_kein_json_objekt(lk):
    assert _mgmt(lk, "POST", [1, 2])[0] == 400


def test_mgmt_rolle_einer_fremden_guild_abgelehnt(lk):
    st, d = _speichern(lk, modus="rollen", role_ids=["999"])
    assert st == 400 and "gibt es in dem gewählten Discord-Server nicht" in d["error"]
    assert bot._livekarte_einstellungen(bot.guild_sicht(lk["a"], G_A))["modus"] == "team"


def test_mgmt_gast_nur_ansehen(lk):
    lk["a"].data["dashboard_perms"] = {"user:900": {"discord_mgmt": ["view"]}}
    assert _mgmt(lk, "GET", gast="900")[0] == 200
    assert _mgmt(lk, "POST", {"modus": "alle"}, gast="900")[0] == 403
    assert _mgmt(lk, "GET", gast="901")[0] == 403 and _mgmt(lk, "POST", {"modus": "alle"}, gast="901")[0] == 403
    assert bot._livekarte_einstellungen(bot.guild_sicht(lk["a"], G_A))["modus"] == "team"


def test_mgmt_kunde_besitzer_darf(lk):
    assert _mgmt(lk, "POST", {"modus": "alle", "nur_verknuepft": True}, admin=False)[0] == 200


def test_mgmt_guilds_getrennt_und_nicht_nach_config_gespiegelt(lk):
    _speichern(lk)
    st, d = _mgmt(lk, "GET", gid=G_B, conn="b")
    assert st == 200 and d["data"]["modus"] == "team"
    assert "live_karte" not in bot.cfg.config
    assert bot.connections.primary() is lk["a"]
    bot._conn_store(lk["a"], "live_karte", {"modus": "alle"})
    assert "live_karte" not in bot.cfg.config


def test_mgmt_rate_limit(lk):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    sid = _admin(lk)

    async def lauf():
        async with TestClient(TestServer(bot.build_app())) as c:
            bot._DASH_RATE_LIMIT_LAST.clear()
            r1 = await c.post("/api/discord-management/livekarte", json={"modus": "alle"}, cookies={bot._SESS_COOKIE: sid})
            r2 = await c.post("/api/discord-management/livekarte", json={"modus": "team"}, cookies={bot._SESS_COOKIE: sid})
            return r1.status, r2.status
    assert asyncio.run(lauf()) == (200, 429)


def test_kachel_status_in_der_uebersicht(lk):
    assert bot._discord_mgmt_payload(bot.guild_sicht(lk["a"], G_A))["livekarte"] == {"enabled": False}
    _einstellen(lk, "alle")
    assert bot._discord_mgmt_payload(bot.guild_sicht(lk["a"], G_A))["livekarte"] == {"enabled": True}
