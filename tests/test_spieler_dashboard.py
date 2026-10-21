"""Spielerbereich im Dashboard (/api/me/*): Serverwahl, Kauf, Lieferorte – und vor allem die Grenzen.

Ein Spieler ist weder Besitzer noch Admin noch Gast. Er darf NUR auf Servern handeln, auf denen er live Mitglied ist
und die dem Server freigeschaltet sind, nie auf fremden, und er kommt an keine Admin-Route. Die Anfragen laufen durch
die echte App (Middleware, Routen); Discord (Mitglieder/Rollen), Nitrado-FTP und der Feed sind gestubbt.

    python3 -m pytest tests/test_spieler_dashboard.py -q
"""
import asyncio
import os
import sys
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_airstrike import servers  # noqa: F401 - Fixture (zwei Server 1000/2000)

G_A, G_B, G_C = 144749039048694173, 155555555555555555, 166666666666666666
U, ANDERER, FREMDER = 555, 777, 999
ROLLE_VIP, ROLLE_ANDERE = 144700000000000010, 144700000000000011


class _Rolle:
    def __init__(self, id_, name="VIP"):
        self.id, self.name = id_, name


class _Mitglied:
    def __init__(self, uid, rollen=()):
        self.id, self.roles, self.display_name = uid, [_Rolle(r) for r in rollen], f"User{uid}"


class _Guild:
    def __init__(self, id_, mitglieder):
        self.id, self.name, self._m = id_, f"Guild {id_}", mitglieder

    def get_member(self, uid):
        return self._m.get(int(uid))

    async def fetch_member(self, uid):
        import discord

        class _Resp:
            status, reason = 404, "nf"
        raise discord.NotFound(_Resp(), "Unknown Member")

    def get_role(self, rid):
        return _Rolle(rid, "VIP") if rid == ROLLE_VIP else None


class _Bot:
    def __init__(self, guilds):
        self.guilds = guilds

    def get_guild(self, gid):
        return self.guilds.get(int(gid))


class _Katalog:
    def __init__(self):
        self.items = [
            {"name": "Kiste", "classname": "WoodenCrate", "price": 100, "enabled": True, "max_amount_per_buy": 3},
            {"name": "Vip-Kiste", "classname": "WoodenCrate", "price": 50, "enabled": True,
             "max_amount_per_buy": 1, "role_ids": [ROLLE_VIP]},
            {"name": "Aus", "classname": "WoodenCrate", "price": 1, "enabled": False, "max_amount_per_buy": 1},
        ]

    def find(self, name):
        return next((i for i in self.items if i["name"].lower() == str(name).lower()), None)


class _Shop:
    def __init__(self):
        self.aufrufe = []

    async def add_purchase_entries(self, cls_list, amount, x, y, z):
        self.aufrufe.append((x, y, z))
        return True, "", [f"area{len(self.aufrufe)}"]

    async def remove_area_entries(self, names):
        return True

    def schedule_auto_restart(self):
        pass


@pytest.fixture
def welt(monkeypatch, servers, tmp_path):  # noqa: F811
    a, b = servers
    bot.connections.assign_guild(a.service_id, G_A)
    bot.connections.assign_guild(b.service_id, G_B)
    a._catalog, a.shop, a.api, a.ftp = _Katalog(), _Shop(), object(), object()
    b._catalog, b.shop, b.api, b.ftp = _Katalog(), _Shop(), object(), object()
    guilds = {G_A: _Guild(G_A, {U: _Mitglied(U), ANDERER: _Mitglied(ANDERER, [ROLLE_VIP])}),
              G_B: _Guild(G_B, {U: _Mitglied(U)})}
    monkeypatch.setattr(bot, "bot", _Bot(guilds))
    monkeypatch.setattr(bot, "db", bot.EconomyDB(str(tmp_path / "e.db")))
    monkeypatch.setattr(bot.cfg, "guilds", {})
    feeds = []

    async def feed(gid, key, embed, **kw):
        feeds.append((gid, key, kw.get("service_id")))
    monkeypatch.setattr(bot, "_post_feed", feed)
    bot._DASH_RATE_LIMIT_LAST.clear()

    # Andere Tests tauschen bot.body dauerhaft aus (bot.body = ...) - hier immer die echte Fassung.
    async def echtes_body(request):
        try:
            d = await request.json()
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    monkeypatch.setattr(bot, "body", echtes_body)
    return {"a": a, "b": b, "guilds": guilds, "feeds": feeds}


def _session(uid=U, mitglied=(G_A, G_B, G_C), gid=None, sid_=None):
    """Eingeloggter Spieler: weder Besitzer noch Admin, ohne Token und Verbindung."""
    gl = [{"id": str(g), "name": f"Guild {g}", "icon": None, "owner": False} for g in mitglied]
    s = bot._sess_create("", [], discord_user={"id": str(uid), "name": f"User{uid}"},
                         member_guilds=bot._mitglied_guilds(gl))
    if gid:
        bot._SESS_STORE[s]["player_guild_id"] = str(gid)
        bot._SESS_STORE[s]["player_service_id"] = str(sid_)
    return s


def api(method, path, sid=None, json=None):
    bot._DASH_RATE_LIMIT_LAST.clear()

    async def lauf():
        async with TestClient(TestServer(bot.build_app())) as c:
            r = await c.request(method, path, json=json, cookies=({bot._SESS_COOKIE: sid} if sid else None))
            try:
                daten = await r.json()
            except Exception:  # noqa: BLE001
                daten = None
            return r.status, daten
    return asyncio.run(lauf())


# ── Zugang ───────────────────────────────────────────────────────────────
def test_ohne_login_alles_gesperrt(welt):
    for pfad in ("/api/me/servers", "/api/me/overview", "/api/me/shop", "/api/me/places"):
        assert api("GET", pfad)[0] == 401


def test_ohne_gewaehlten_server_409(welt):
    assert api("GET", "/api/me/overview", _session())[0] == 409


def test_serverliste_nur_mitglied_bot_und_freigeschaltet(welt):
    welt["b"].data.pop("guild_ids", None)
    bot.connections.assign_guild(welt["b"].service_id, None)           # 2000: nicht freigeschaltet
    st, d = api("GET", "/api/me/servers", _session(mitglied=(G_A, G_B, G_C)))
    assert st == 200
    assert [(s["guild_id"], s["service_id"]) for s in d["data"]["servers"]] == [(str(G_A), "1000")]
    st, d = api("GET", "/api/me/servers", _session(mitglied=(G_B,)))     # nicht Mitglied von G_A
    assert d["data"]["servers"] == []


def test_waehlen_nur_eigene_freigeschaltete_server(welt):
    s = _session(mitglied=(G_A,))
    assert api("POST", "/api/me/servers/select", s, {"guild_id": str(G_B), "service_id": "2000"})[0] == 403   # kein Mitglied
    assert api("POST", "/api/me/servers/select", s, {"guild_id": str(G_A), "service_id": "2000"})[0] == 403   # falscher Server zur Guild
    assert api("POST", "/api/me/servers/select", s, {"guild_id": str(G_A), "service_id": "9999"})[0] == 403
    assert api("POST", "/api/me/servers/select", s, {"guild_id": str(G_A), "service_id": "1000"})[0] == 200
    assert api("GET", "/api/me/overview", s)[0] == 200


def test_nicht_mehr_mitglied_wird_live_erkannt(welt):
    s = _session(gid=G_A, sid_="1000")
    assert api("GET", "/api/me/overview", s)[0] == 200
    del welt["guilds"][G_A]._m[U]                                       # inzwischen ausgetreten
    assert api("GET", "/api/me/overview", s)[0] == 403
    assert api("POST", "/api/me/shop/buy", s, {"item": "Kiste", "amount": 1, "x": 100, "z": 100})[0] == 403


def test_manipulierte_session_guild_ausserhalb_der_mitgliederliste(welt):
    s = _session(mitglied=(G_C,), gid=G_A, sid_="1000")                 # G_A steht NICHT in member_guilds
    assert api("GET", "/api/me/overview", s)[0] == 403


def test_spieler_kommt_an_keine_admin_route(welt):
    s = _session(gid=G_A, sid_="1000")
    for methode, pfad in (("GET", "/api/shop/items"), ("GET", "/api/economy/balances"),
                          ("POST", "/api/economy/money"), ("GET", "/api/map/meta"), ("GET", "/api/map/players"),
                          ("GET", "/api/players"), ("GET", "/api/bans"), ("POST", "/api/shop/items")):
        st, d = api(methode, pfad, s, {} if methode == "POST" else None)
        assert st in (401, 403, 409), (pfad, st)
        assert not (d or {}).get("ok"), pfad


# ── Übersicht, Katalog ───────────────────────────────────────────────────
def test_uebersicht_zeigt_nur_eigene_daten(welt):
    bot.db.add_wallet(G_A, U, 500)
    bot.db.add_wallet(G_A, ANDERER, 7777)
    st, d = api("GET", "/api/me/overview", _session(gid=G_A, sid_="1000"))
    assert st == 200 and d["data"]["wallet"] == bot.db.get_balance(G_A, U)[0]
    assert d["data"]["wallet"] != bot.db.get_balance(G_A, ANDERER)[0]
    assert d["data"]["server_name"] == welt["a"].name


def test_katalog_nur_aktive_und_rollen_markierung(welt):
    st, d = api("GET", "/api/me/shop", _session(gid=G_A, sid_="1000"))
    items = {i["name"]: i for i in d["data"]["items"]}
    assert set(items) == {"Kiste", "Vip-Kiste"} and items["Kiste"]["kaufbar"] and not items["Vip-Kiste"]["kaufbar"]
    st, d = api("GET", "/api/me/shop", _session(uid=ANDERER, gid=G_A, sid_="1000"))
    assert {i["name"]: i for i in d["data"]["items"]}["Vip-Kiste"]["kaufbar"]


# ── Kauf ─────────────────────────────────────────────────────────────────
def _kauf(s, **kw):
    return api("POST", "/api/me/shop/buy", s, dict({"item": "Kiste", "amount": 1}, **kw))


def test_kauf_per_koordinaten(welt):
    start = bot.db.get_balance(G_A, U)[0]
    bot.db.add_wallet(G_A, U, 1000)
    vor = bot.db.get_balance(G_A, U)[0]
    st, d = _kauf(_session(gid=G_A, sid_="1000"), x=4640, z=10350)
    assert st == 200, d
    assert welt["a"].shop.aufrufe == [(4640.0, 0.0, 10350.0)] and welt["b"].shop.aufrufe == []
    assert bot.db.get_balance(G_A, U)[0] == vor - 100
    zeile = bot.db.purchases_of_user("1000", G_A, U, 5)[0]
    assert (zeile["item_name"], zeile["amount"], zeile["total_price"], zeile["status"]) == ("Kiste", 1, 100, "pending")
    assert welt["feeds"] == [(G_A, "shop_log", "1000")]
    assert start is not None


def test_kauf_wie_slash_buy_gleiche_buchung(welt):
    """Dashboard und /buy laufen durch dieselbe Logik: gleiche Buchung, gleicher Eintrag."""
    bot.db.add_wallet(G_A, U, 1000)
    vor = bot.db.get_balance(G_A, U)[0]
    plan = bot._kauf_pruefen(welt["a"], G_A, U, "Kiste", 2, set(), None, 4640.0, 10350.0, None, False)
    res = asyncio.run(bot._kauf_ausfuehren(welt["a"], G_A, U, "User", plan))
    slash = bot.db.purchases_of_user("1000", G_A, U, 5)[0]
    bot._DASH_RATE_LIMIT_LAST.clear()
    st, _d = _kauf(_session(gid=G_A, sid_="1000"), amount=2, x=4640, z=10350)
    dash = bot.db.purchases_of_user("1000", G_A, U, 5)[0]
    assert res.ok and st == 200
    assert (slash["item_name"], slash["amount"], slash["total_price"], slash["x"], slash["z"]) == \
           (dash["item_name"], dash["amount"], dash["total_price"], dash["x"], dash["z"])
    assert bot.db.get_balance(G_A, U)[0] == vor - 400


def test_kauf_ablehnungen_buchen_nichts_ab(welt):
    bot.db.add_wallet(G_A, U, 1000)
    vor = bot.db.get_balance(G_A, U)[0]
    s = _session(gid=G_A, sid_="1000")
    faelle = [
        dict(item="Gibt es nicht", x=100, z=100),
        dict(item="Aus", x=100, z=100),
        dict(amount=99, x=100, z=100),                       # mehr als max_amount_per_buy
        dict(x=100, z=100, ort="last"),                      # ort + x/z
        dict(),                                              # weder ort noch x/z
        dict(x=99999, z=100),                                # ausserhalb der Karte
        dict(x="abc", z=100),
        dict(x=float("nan"), z=100),
        dict(x=100, z=100, y=5000),
        dict(ort="last"),                                    # noch kein letzter Ort
        dict(item="Vip-Kiste", x=100, z=100),                # Rolle fehlt
        dict(amount=0, x=100, z=100),
    ]
    for kw in faelle:
        st, d = _kauf(s, **kw)
        assert st in (400, 402) and not d["ok"], kw
    assert bot.db.get_balance(G_A, U)[0] == vor and welt["a"].shop.aufrufe == []
    assert bot.db.purchases_of_user("1000", G_A, U, 5) == []


def test_kauf_zu_wenig_guthaben_402_ohne_lieferung(welt):
    s = _session(gid=G_A, sid_="1000")
    bot.db.try_spend_wallet(G_A, U, bot.db.get_balance(G_A, U)[0])       # leeren
    st, d = _kauf(s, x=100, z=100)
    assert st == 402 and d["code"] == "guthaben" and welt["a"].shop.aufrufe == []


def test_rollen_item_mit_rolle_ok(welt):
    bot.db.add_wallet(G_A, ANDERER, 1000)
    st, d = _kauf(_session(uid=ANDERER, gid=G_A, sid_="1000"), item="Vip-Kiste", x=100, z=100)
    assert st == 200, d


def test_rollen_hinweis_nennt_rollennamen(welt):
    st, d = _kauf(_session(gid=G_A, sid_="1000"), item="Vip-Kiste", x=100, z=100, lang="de")
    assert st == 400 and "VIP" in d["error"] and "<@&" not in d["error"]


def test_kauf_ohne_ftp_503_nichts_abgebucht(welt):
    welt["a"].ftp = None
    bot.db.add_wallet(G_A, U, 1000)
    vor = bot.db.get_balance(G_A, U)[0]
    st, d = _kauf(_session(gid=G_A, sid_="1000"), x=100, z=100)
    assert st == 503 and bot.db.get_balance(G_A, U)[0] == vor


def test_kauf_geht_immer_an_den_gewaehlten_server(welt):
    bot.db.add_wallet(G_B, U, 1000)
    st, _d = _kauf(_session(gid=G_B, sid_="2000"), x=100, z=100)
    assert st == 200 and welt["b"].shop.aufrufe and welt["a"].shop.aufrufe == []
    assert bot.db.purchases_of_user("2000", G_B, U, 5) and bot.db.purchases_of_user("1000", G_A, U, 5) == []


# ── Lieferorte ───────────────────────────────────────────────────────────
def test_orte_speichern_listen_loeschen(welt):
    s = _session(gid=G_A, sid_="1000")
    assert api("POST", "/api/me/places", s, {"name": "Basis", "x": 4640, "z": 10350})[0] == 200
    st, d = api("GET", "/api/me/places", s)
    assert [o["name"] for o in d["data"]["gespeichert"]] == ["Basis"] and d["data"]["max_gespeichert"] == 15
    assert api("DELETE", "/api/me/places?name=basis", s)[0] == 200            # Gross/klein egal
    assert api("DELETE", "/api/me/places?name=Basis", s)[0] == 404
    assert api("GET", "/api/me/places", s)[1]["data"]["gespeichert"] == []


def test_orte_validierung_limit_und_doppelter_name(welt):
    s = _session(gid=G_A, sid_="1000")
    assert api("POST", "/api/me/places", s, {"name": "A<b>", "x": 1, "z": 1})[0] == 400
    assert api("POST", "/api/me/places", s, {"name": "Punkt", "x": 99999, "z": 1})[0] == 400
    assert api("POST", "/api/me/places", s, {"name": "Punkt", "x": "x", "z": 1})[0] == 400
    assert api("POST", "/api/me/places", s, {"name": "Punkt", "x": 1})[0] == 400
    assert api("POST", "/api/me/places", s, {"name": "Punkt", "x": 1, "z": 1})[0] == 200
    assert api("POST", "/api/me/places", s, {"name": "punkt", "x": 2, "z": 2})[0] == 409
    for n in range(14):
        assert api("POST", "/api/me/places", s, {"name": f"p{n}", "x": 1, "z": 1})[0] == 200
    st, d = api("POST", "/api/me/places", s, {"name": "zuviel", "x": 1, "z": 1})
    assert st == 409 and d["code"] == "limit"


def test_orte_getrennt_nach_user_und_server(welt):
    s1 = _session(gid=G_A, sid_="1000")
    api("POST", "/api/me/places", s1, {"name": "Meins", "x": 5, "z": 5})
    # anderer User auf demselben Server: sieht nichts, kann nichts loeschen
    s2 = _session(uid=ANDERER, gid=G_A, sid_="1000")
    assert api("GET", "/api/me/places", s2)[1]["data"]["gespeichert"] == []
    assert api("DELETE", "/api/me/places?name=Meins", s2)[0] == 404
    # derselbe User auf dem anderen Server: eigene, leere Liste
    s3 = _session(gid=G_B, sid_="2000")
    assert api("GET", "/api/me/places", s3)[1]["data"]["gespeichert"] == []
    assert [o["name"] for o in api("GET", "/api/me/places", s1)[1]["data"]["gespeichert"]] == ["Meins"]


def test_kauf_mit_gespeichertem_ort_und_ort_speichern(welt):
    bot.db.add_wallet(G_A, U, 1000)
    s = _session(gid=G_A, sid_="1000")
    api("POST", "/api/me/places", s, {"name": "Basis", "x": 4640, "z": 10350})
    st, d = _kauf(s, ort="Basis")
    assert st == 200 and welt["a"].shop.aufrufe[-1] == (4640.0, 0.0, 10350.0) and "Basis" in d["data"]["ort_label"]
    st, d = _kauf(s, x=1000, z=2000, speichern_als="Neu")
    assert st == 200 and d["data"]["ort_gespeichert"] == "ok"
    assert {o["name"] for o in api("GET", "/api/me/places", s)[1]["data"]["gespeichert"]} == {"Basis", "Neu"}


def test_kauf_mit_fremdem_ort_abgelehnt(welt):
    bot.db.add_wallet(G_A, U, 1000)
    api("POST", "/api/me/places", _session(uid=ANDERER, gid=G_A, sid_="1000"), {"name": "Fremd", "x": 5, "z": 5})
    fid = bot.db.ort_nach_name(G_A, "1000", ANDERER, "Fremd")["id"]
    s = _session(gid=G_A, sid_="1000")
    for wert in ("Fremd", f"s:{fid}"):
        st, _d = _kauf(s, ort=wert)
        assert st == 400
    assert welt["a"].shop.aufrufe == []


def test_karte_des_spieler_servers(welt):
    s = _session(gid=G_A, sid_="1000")
    st, d = api("GET", "/api/me/map/meta", s)
    assert st == 200 and d["data"]["world_size"] == 12800 and "heatmap_typen" in d["data"]   # Livonia


def test_mitglied_guilds_und_session_felder():
    gl = bot._mitglied_guilds([{"id": "1", "name": "A", "icon": "x", "owner": True}, {"id": "abc"}, "kaputt",
                               {"id": "2", "name": "B", "owner": False}])
    assert [g["id"] for g in gl] == ["1", "2"] and gl[0]["owner"] and not gl[1]["owner"]
    assert "cdn.discordapp.com/icons/1/x.png" in gl[0]["icon_url"] and gl[1]["icon_url"] is None
    sid = bot._sess_create("", [], discord_user={"id": "1"}, member_guilds=gl)
    assert bot._SESS_STORE[sid]["player_guild_id"] is None and len(bot._SESS_STORE[sid]["member_guilds"]) == 2


def test_spieler_kann_die_sprache_wechseln(welt):
    """Ein Spieler hat nie einen Token - der Sprachschalter darf trotzdem nicht an der Token-Sperre scheitern."""
    s = _session()
    assert api("POST", "/api/sprache", s, {"lang": "de"})[0] == 200
    assert bot.cfg.sprache.get("555") == "de"
    assert api("POST", "/api/sprache", None, {"lang": "de"})[0] == 401


# ── Mieten (/api/me/rentals) ─────────────────────────────────────────────
import xml.etree.ElementTree as ET  # noqa: E402
from test_shop_rentals import MISSION, _EVENTS_XML, _RENTAL_EVENT_XML, _FakeFTP  # noqa: E402


class _SchreibFehlerFTP(_FakeFTP):
    def write_file(self, pfad, inhalt):
        return False


@pytest.fixture
def miete(welt):
    a = welt["a"]
    a.ftp, a.rentals = _FakeFTP(), True
    a.data["ftp_mission_dir"] = MISSION
    kat = bot.RentalCatalog()
    for name, preis, rollen, an in (("Olga", 100, [], True), ("Vip-Olga", 50, [ROLLE_VIP], True), ("Aus", 1, [], False)):
        kat.items.append({"name": name, "price_per_restart": preis, "event_xml": _RENTAL_EVENT_XML, "event_zone": "",
                          "category": "Vehicle", "event_group": "", "enabled": an, "min_restarts": 1,
                          "max_restarts": 5, "role_ids": rollen})
    kat.rebuild_index()
    a._rentals_catalog = kat
    return a


def _miet_events(conn):
    root = ET.fromstring(conn.ftp.files[f"{MISSION}/db/events.xml"])
    return [e.get("name") for e in root.findall("event") if (e.get("name") or "").startswith(bot._RENTAL_EVENT_PREFIX)]


def _mieten(s, **kw):
    return api("POST", "/api/me/rentals/buy", s, dict({"item": "Olga", "restarts": 2}, **kw))


def test_miet_katalog_und_rollen(welt, miete):
    st, d = api("GET", "/api/me/rentals", _session(gid=G_A, sid_="1000"))
    items = {i["name"]: i for i in d["data"]["items"]}
    assert st == 200 and set(items) == {"Olga", "Vip-Olga"} and items["Olga"]["kaufbar"] and not items["Vip-Olga"]["kaufbar"]
    assert d["data"]["mieten"] == []
    st, d = api("GET", "/api/me/rentals", _session(uid=ANDERER, gid=G_A, sid_="1000"))
    assert {i["name"]: i for i in d["data"]["items"]}["Vip-Olga"]["kaufbar"]


def test_miete_per_koordinaten(welt, miete):
    bot.db.add_wallet(G_A, U, 1000)
    vor = bot.db.get_balance(G_A, U)[0]
    st, d = _mieten(_session(gid=G_A, sid_="1000"), x=4640, z=10350)
    assert st == 200, d
    assert len(_miet_events(miete)) == 1 and bot.db.get_balance(G_A, U)[0] == vor - 200
    assert welt["feeds"][-1] == (G_A, "shop_log", "1000")
    st, d = api("GET", "/api/me/rentals", _session(gid=G_A, sid_="1000"))
    m = d["data"]["mieten"]
    assert len(m) == 1 and (m[0]["item"], m[0]["total"], m[0]["remaining"], m[0]["status"]) == ("Olga", 2, 2, "active")


def test_miete_mit_gespeichertem_ort_und_ort_speichern(welt, miete):
    bot.db.add_wallet(G_A, U, 1000)
    s = _session(gid=G_A, sid_="1000")
    api("POST", "/api/me/places", s, {"name": "Basis", "x": 4640, "z": 10350})
    st, d = _mieten(s, ort="Basis")
    assert st == 200 and "Basis" in d["data"]["ort_label"], d
    bot._DASH_RATE_LIMIT_LAST.clear()
    st, d = _mieten(s, x=1000, z=2000, speichern_als="Garage")
    assert st == 200 and d["data"]["ort_gespeichert"] == "ok"
    assert {o["name"] for o in api("GET", "/api/me/places", s)[1]["data"]["gespeichert"]} == {"Basis", "Garage"}


def test_miete_ablehnungen_veraendern_nichts(welt, miete):
    bot.db.add_wallet(G_A, U, 1000)
    vor = bot.db.get_balance(G_A, U)[0]
    s = _session(gid=G_A, sid_="1000")
    for kw in (dict(item="Gibt es nicht", x=1, z=1), dict(item="Aus", x=1, z=1), dict(restarts=99, x=1, z=1),
               dict(restarts=0, x=1, z=1), dict(restarts="abc", x=1, z=1), dict(x=1, z=1, ort="last"), dict(),
               dict(x=99999, z=1), dict(x="abc", z=1), dict(x=1, z=1, speichern_als="a<b>"),
               dict(item="Vip-Olga", x=1, z=1), dict(ort="last")):
        st, d = _mieten(s, **kw)
        assert st in (400, 402) and not d["ok"], kw
    assert bot.db.get_balance(G_A, U)[0] == vor and _miet_events(miete) == []
    assert bot.db.rentals_of_user("1000", G_A, U, 5) == []


def test_miete_zu_wenig_guthaben_402(welt, miete):
    s = _session(gid=G_A, sid_="1000")
    bot.db.try_spend_wallet(G_A, U, bot.db.get_balance(G_A, U)[0])
    st, d = _mieten(s, x=1, z=1)
    assert st == 402 and d["code"] == "guthaben" and _miet_events(miete) == []


def test_miete_ftp_schreibfehler_nichts_abgebucht(welt, miete):
    miete.ftp = _SchreibFehlerFTP()
    bot.db.add_wallet(G_A, U, 1000)
    vor = bot.db.get_balance(G_A, U)[0]
    st, d = _mieten(_session(gid=G_A, sid_="1000"), x=1, z=1)
    assert st == 500 and bot.db.get_balance(G_A, U)[0] == vor and bot.db.rentals_of_user("1000", G_A, U, 5) == []


def test_miete_rollen_item_mit_rolle(welt, miete):
    bot.db.add_wallet(G_A, ANDERER, 1000)
    st, d = _mieten(_session(uid=ANDERER, gid=G_A, sid_="1000"), item="Vip-Olga", x=1, z=1)
    assert st == 200, d


def test_miete_fremder_ort_und_fremde_mieten(welt, miete):
    bot.db.add_wallet(G_A, U, 1000)
    bot.db.add_wallet(G_A, ANDERER, 1000)
    api("POST", "/api/me/places", _session(uid=ANDERER, gid=G_A, sid_="1000"), {"name": "Fremd", "x": 5, "z": 5})
    s = _session(gid=G_A, sid_="1000")
    assert _mieten(s, ort="Fremd")[0] == 400
    assert _mieten(_session(uid=ANDERER, gid=G_A, sid_="1000"), x=1, z=1)[0] == 200
    assert api("GET", "/api/me/rentals", s)[1]["data"]["mieten"] == []        # die Miete des anderen sieht er nicht
    assert len(api("GET", "/api/me/rentals", _session(uid=ANDERER, gid=G_A, sid_="1000"))[1]["data"]["mieten"]) == 1


def test_miete_nur_auf_dem_gewaehlten_server_und_nur_fuer_mitglieder(welt, miete):
    bot.db.add_wallet(G_A, U, 1000)
    assert _mieten(_session(mitglied=(G_B,), gid=G_A, sid_="1000"), x=1, z=1)[0] == 403       # Mitgliedschaft fehlt
    assert api("GET", "/api/me/rentals", _session())[0] == 409                                # kein Server gewaehlt
    assert _miet_events(miete) == []
