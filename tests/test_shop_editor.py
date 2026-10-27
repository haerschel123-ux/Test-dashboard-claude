"""Shop-Item-Editor wie bei DayZ++: Menge je Inhalt, Kauf-Limit (mit Ausnahmen), Rabatte je Item, versteckt, alle Rollen noetig.

Geprueft wird vor allem, was schiefgehen kann: ein Limit, das sich umgehen laesst (mehrere Stueck in einem Kauf, zwei
gleichzeitige Kaeufe), ein Limit, das ueber Server hinweg zaehlt, und Rollen-/Rabattregeln, die jemand ohne die Rolle nutzt.
Admin-Aufrufe laufen durch die echten Handler, Kaeufe durch die echte App (wie in test_spieler_dashboard).

    python3 -m pytest tests/test_shop_editor.py -q
"""
import asyncio
import json
import os
import sys
import time
from unittest import mock

import pytest
from aiohttp.test_utils import make_mocked_request
from aiohttp.web_urldispatcher import UrlMappingMatchInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot
from test_spieler_dashboard import (  # noqa: F401 - Fixtures und Helfer der Spielertests
    ANDERER, G_A, G_B, ROLLE_ANDERE, ROLLE_VIP, U, _Mitglied, _Rolle, _session, api, servers, welt)


class _Lieferung:
    """Shop-Attrappe, die mitschreibt, WAS ausgeliefert wurde (Classnames mit Wiederholung)."""

    def __init__(self):
        self.aufrufe = []

    async def add_purchase_entries(self, cls_list, amount, x, y, z):
        self.aufrufe.append((list(cls_list), amount))
        return True, "", [f"area{len(self.aufrufe)}"]

    async def remove_area_entries(self, names):
        return True

    def schedule_auto_restart(self):
        pass


@pytest.fixture
def shop(welt, monkeypatch, tmp_path):
    """Zwei Server mit echtem ShopCatalog (eigene Datei je Server) und Admin-Zugriff auf beide."""
    a, b = welt["a"], welt["b"]
    for conn in (a, b):
        conn._catalog = bot.ShopCatalog(conn.service_id, str(tmp_path / f"shop_{conn.service_id}.json"))
        conn.shop = _Lieferung()
        bot.db.add_wallet({"1000": G_A, "2000": G_B}[conn.service_id], U, 100000)
    bot.db.add_wallet(G_A, ANDERER, 100000)
    welt["guilds"][G_A]._m[ANDERER].roles = [_Rolle(ROLLE_VIP)]
    welt["guilds"][G_A]._m[555 + 1] = _Mitglied(556, [ROLLE_VIP, ROLLE_ANDERE])
    bot.db.add_wallet(G_A, 556, 100000)

    echtes_body = bot.body                                  # von ``welt`` gesetzt; admin() ersetzt es nur fuer den Aufruf

    def admin(conn, methode, pfad, daten=None, name=None, handler=None):
        bot._DASH_RATE_LIMIT_LAST.clear()
        sid = f"shopedit-{time.time_ns()}"
        bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": "acct-1"}, "is_admin": True,
                                "service_id": conn.service_id, "seen": time.time()}
        mi = UrlMappingMatchInfo({"name": name} if name else {}, mock.Mock())
        req = make_mocked_request(methode, pfad, headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"}, match_info=mi)

        async def fake_body(_r):
            return daten or {}
        monkeypatch.setattr(bot, "body", fake_body)
        try:
            resp = asyncio.run(handler(req))
        finally:
            monkeypatch.setattr(bot, "body", echtes_body)
        return resp.status, json.loads(resp.body.decode("utf-8"))

    welt["admin"] = admin
    welt["neu"] = lambda conn, daten: admin(conn, "POST", "/api/shop/items", daten, handler=bot.create_item)
    welt["aendern"] = lambda conn, name, daten: admin(conn, "PUT", f"/api/shop/items/{name}", daten, name=name,
                                                      handler=bot.update_item)
    return welt


def _kauf(sess, item="Kiste", amount=1, **kw):
    return api("POST", "/api/me/shop/buy", sess, dict({"item": item, "amount": amount, "x": 4640, "z": 10350}, **kw))


def _a(uid=U):
    return _session(uid=uid, gid=G_A, sid_="1000")


def _stueck(conn, uid=None):
    return sum(r["amount"] for r in bot.db.purchases_of_user(conn.service_id, G_A, uid or U, 100))


# ── Inhalt mit Menge ─────────────────────────────────────────────────────
def test_inhalt_mit_menge_wird_als_wiederholter_classname_gespeichert(shop):
    st, d = shop["neu"](shop["a"], {"name": "Set", "price": 10, "category": "Kisten",
                                    "contents": [{"classname": "Canteen", "qty": 3}, {"classname": "Apple", "qty": 1},
                                                 {"classname": "canteen", "qty": 1}, {"classname": "  ", "qty": 1}]})
    assert st == 200, d
    it = shop["a"].catalog.find("Set")
    assert it["classnames"] == ["Canteen"] * 4 + ["Apple"]                  # gleiche Namen zaehlen zusammen
    v = d["data"]["item"]
    assert v["contents"] == [{"classname": "Canteen", "qty": 4}, {"classname": "Apple", "qty": 1}]
    assert v["is_bundle"] is True


def test_einzelitem_mit_menge_ist_kein_bundle(shop):
    st, d = shop["neu"](shop["a"], {"name": "Wasser", "price": 5, "contents": [{"classname": "Canteen", "qty": 3}]})
    assert st == 200, d
    assert d["data"]["item"]["is_bundle"] is False and d["data"]["item"]["classnames"] == ["Canteen"] * 3
    assert d["data"]["item"]["category"] == "Custom"                          # nicht „Bundles“
    kat = shop["a"].catalog
    assert kat.find("Canteen") is kat.find("Wasser")                          # Classname bleibt als Schluessel
    assert "×3" in bot._shop_line(kat.find("Wasser"))


def test_lieferung_schreibt_je_stueck_einen_eintrag(shop):
    shop["neu"](shop["a"], {"name": "Set", "price": 10, "contents": [{"classname": "Canteen", "qty": 3},
                                                                      {"classname": "Apple", "qty": 1}],
                            "max_amount_per_buy": 5})
    st, d = _kauf(_a(), "Set", 2)
    assert st == 200, d
    assert shop["a"].shop.aufrufe == [(["Canteen"] * 3 + ["Apple"], 2)]       # 2 Kaeufe x (3 Canteen + 1 Apple)


@pytest.mark.parametrize("inhalt", [
    [{"classname": "X", "qty": 0}], [{"classname": "X", "qty": 51}], [{"classname": "X", "qty": "viel"}],
    [{"classname": "Mit Leerzeichen", "qty": 1}], [{"classname": "A,B", "qty": 1}], [], "kein Liste",
    [{"classname": "A", "qty": 30}, {"classname": "B", "qty": 30}], [1, 2]])
def test_unbrauchbarer_inhalt_wird_abgelehnt(shop, inhalt):
    st, d = shop["neu"](shop["a"], {"name": "Kaputt", "price": 1, "contents": inhalt})
    assert st == 400 and not d["ok"]
    assert shop["a"].catalog.find("Kaputt") is None


def test_aendern_mit_fehler_laesst_das_item_unveraendert(shop):
    shop["neu"](shop["a"], {"name": "Set", "price": 10, "contents": [{"classname": "Apple", "qty": 2}], "hidden": False})
    vorher = json.dumps(shop["a"].catalog.find("Set"), sort_keys=True)
    for kaputt in ({"hidden": True, "purchase_limit": {"max": 0, "per_seconds": 60}},
                   {"hidden": True, "contents": [{"classname": "X", "qty": 99}]},
                   {"hidden": True, "discounts": [{"role_id": "abc", "typ": "percent", "betrag": 10}]}):
        st, d = shop["aendern"](shop["a"], "Set", kaputt)
        assert st == 400, d
        assert json.dumps(shop["a"].catalog.find("Set"), sort_keys=True) == vorher


def test_aendern_ersetzt_den_inhalt_und_entfernt_leere_zusaetze(shop):
    shop["neu"](shop["a"], {"name": "Set", "price": 10, "contents": [{"classname": "Apple", "qty": 2}],
                            "hidden": True, "purchase_limit": {"max": 1, "per_seconds": 60}})
    st, d = shop["aendern"](shop["a"], "Set", {"contents": [{"classname": "Apple", "qty": 1}], "hidden": False,
                                               "purchase_limit": None, "discounts": [], "require_all_roles": False})
    assert st == 200, d
    it = shop["a"].catalog.find("Set")
    assert it["classname"] == "Apple" and "classnames" not in it
    assert not any(k in it for k in ("hidden", "purchase_limit", "discounts", "require_all_roles"))


# ── Kauf-Limit ───────────────────────────────────────────────────────────
def _mit_limit(shop, limit, name="Kiste", **extra):
    d = {"name": name, "price": 10, "contents": [{"classname": "WoodenCrate", "qty": 1}], "max_amount_per_buy": 5,
         "purchase_limit": limit}
    d.update(extra)
    st, r = shop["neu"](shop["a"], d)
    assert st == 200, r


def test_limit_je_spieler_zaehlt_stuecke_nicht_kaeufe(shop):
    _mit_limit(shop, {"max": 3, "per_seconds": 3600})
    assert _kauf(_a(), amount=2)[0] == 200
    st, d = _kauf(_a(), amount=2)                                              # 2 + 2 > 3: nur noch 1 uebrig
    assert st == 400 and d["code"] == "limit" and "1×" in d["error"], d
    assert _kauf(_a(), amount=1)[0] == 200                                     # genau der Rest geht
    st, d = _kauf(_a(), amount=1)
    assert st == 400 and d["code"] == "limit" and ("erreicht" in d["error"] or "reached" in d["error"])
    assert _stueck(shop["a"]) == 3
    assert _kauf(_a(ANDERER), amount=3)[0] == 200                              # ein anderer Spieler hat sein eigenes Limit


def test_limit_ein_kauf_ueber_dem_limit_wird_nicht_ausgeliefert(shop):
    _mit_limit(shop, {"max": 2, "per_seconds": 3600})
    guthaben = bot.db.get_balance(G_A, U)[0]
    st, d = _kauf(_a(), amount=4)
    assert st == 400 and d["code"] == "limit"
    assert bot.db.get_balance(G_A, U)[0] == guthaben and shop["a"].shop.aufrufe == []


def test_limit_gemeinsam_fuer_alle_spieler(shop):
    _mit_limit(shop, {"max": 2, "per_seconds": 3600, "shared": True})
    assert _kauf(_a(U), amount=2)[0] == 200
    st, d = _kauf(_a(ANDERER), amount=1)
    assert st == 400 and d["code"] == "limit"
    assert "alle Spieler" in d["error"] or "all players" in d["error"]


def test_limit_zeitraum_laeuft_ab(shop):
    _mit_limit(shop, {"max": 1, "per_seconds": 3600})
    assert _kauf(_a())[0] == 200
    it = shop["a"].catalog.find("Kiste")
    assert bot._kauf_limit_pruefen("1000", U, it, set(), 1) is not None
    assert bot._kauf_limit_pruefen("1000", U, it, set(), 1, jetzt=time.time() + 3599) is not None
    assert bot._kauf_limit_pruefen("1000", U, it, set(), 1, jetzt=time.time() + 3601) is None


def test_limit_meldung_nennt_die_wartezeit(shop):
    _mit_limit(shop, {"max": 1, "per_seconds": 7200})
    _kauf(_a())
    st, d = _kauf(_a())
    assert st == 400 and d["code"] == "limit"
    assert "1 h 59 min" in d["error"] or "2 h" in d["error"], d["error"]            # Wartezeit bis der Kauf aus dem Zeitraum faellt


def test_limit_ausnahmen_grosszuegigste_gilt(shop):
    ausn = [{"kind": "role", "id": str(ROLLE_VIP), "max": 4}, {"kind": "role", "id": str(ROLLE_ANDERE), "max": 1},
            {"kind": "user", "id": "556", "max": 0}]
    _mit_limit(shop, {"max": 2, "per_seconds": 3600, "exceptions": ausn})
    # U hat keine Rolle: Grundwert 2
    assert _kauf(_a(U), amount=2)[0] == 200 and _kauf(_a(U))[0] == 400
    # ANDERER hat VIP: 4
    assert _kauf(_a(ANDERER), amount=4)[0] == 200 and _kauf(_a(ANDERER))[0] == 400
    # 556 hat VIP + ANDERE und eine Spieler-Ausnahme „unbegrenzt“ - die gewinnt
    assert _kauf(_a(556), amount=5)[0] == 200 and _kauf(_a(556), amount=5)[0] == 200


def test_limit_ausnahme_kann_nicht_verschaerfen(shop):
    _mit_limit(shop, {"max": 3, "per_seconds": 3600, "exceptions": [{"kind": "role", "id": str(ROLLE_VIP), "max": 1}]})
    assert _kauf(_a(ANDERER), amount=3)[0] == 200                              # VIP bekommt nicht weniger als alle


def test_limit_zaehlt_nur_diesen_server_und_diesen_artikel(shop):
    _mit_limit(shop, {"max": 1, "per_seconds": 3600})
    shop["neu"](shop["b"], {"name": "Kiste", "price": 10, "contents": [{"classname": "WoodenCrate", "qty": 1}],
                            "purchase_limit": {"max": 1, "per_seconds": 3600}})
    shop["neu"](shop["a"], {"name": "Andere Kiste", "price": 10, "contents": [{"classname": "WoodenCrate", "qty": 1}],
                            "purchase_limit": {"max": 1, "per_seconds": 3600}})
    assert _kauf(_a())[0] == 200
    assert _kauf(_a())[0] == 400
    # gleicher Name auf dem Server eines anderen Kunden: eigener Zaehler
    assert _kauf(_session(gid=G_B, sid_="2000"))[0] == 200
    # anderer Artikel auf demselben Server: eigener Zaehler
    assert _kauf(_a(), "Andere Kiste")[0] == 200


def test_zwei_gleichzeitige_kaeufe_umgehen_das_limit_nicht(shop):
    _mit_limit(shop, {"max": 1, "per_seconds": 3600})
    conn = shop["a"]
    plan1 = bot._kauf_pruefen(conn, G_A, U, "Kiste", 1, set(), None, 100.0, 100.0, None, False)
    plan2 = bot._kauf_pruefen(conn, G_A, U, "Kiste", 1, set(), None, 100.0, 100.0, None, False)
    assert not isinstance(plan1, bot._KaufFehler) and not isinstance(plan2, bot._KaufFehler)    # beide bestehen die Vorpruefung

    async def beide():
        return await asyncio.gather(bot._kauf_ausfuehren(conn, G_A, U, "U", plan1),
                                    bot._kauf_ausfuehren(conn, G_A, U, "U", plan2))
    r1, r2 = asyncio.run(beide())
    assert sorted([r1.ok, r2.ok]) == [False, True]
    assert _stueck(conn) == 1


@pytest.mark.parametrize("limit", [
    {"max": 0, "per_seconds": 60}, {"max": 1, "per_seconds": 0}, {"max": "x", "per_seconds": 60},
    {"max": 1, "per_seconds": 366 * 86400 + 1}, {"max": 1, "per_seconds": 60, "exceptions": [{"kind": "x", "id": "1", "max": 1}]},
    {"max": 1, "per_seconds": 60, "exceptions": [{"kind": "role", "id": "abc", "max": 1}]},
    {"max": 1, "per_seconds": 60, "exceptions": [{"kind": "role", "id": "5", "max": -1}]},
    {"max": 1, "per_seconds": 60, "exceptions": [{"kind": "role", "id": "5", "max": 1}, {"kind": "role", "id": "5", "max": 2}]},
    {"max": 1, "per_seconds": 60, "exceptions": [{"kind": "role", "id": str(i + 1), "max": 1} for i in range(21)]}, 5])
def test_ungueltiges_limit_wird_abgelehnt(shop, limit):
    st, d = shop["neu"](shop["a"], {"name": "X", "price": 1, "classname": "A", "purchase_limit": limit})
    assert st == 400 and not d["ok"]
    assert shop["a"].catalog.find("X") is None


def test_limit_aus_schaltet_es_ab(shop):
    _mit_limit(shop, {"max": 1, "per_seconds": 60})
    st, _d = shop["aendern"](shop["a"], "Kiste", {"purchase_limit": {"enabled": False, "max": 1, "per_seconds": 60}})
    assert st == 200 and "purchase_limit" not in shop["a"].catalog.find("Kiste")
    assert _kauf(_a())[0] == 200 and _kauf(_a())[0] == 200


def test_kaputtes_limit_im_katalog_blockiert_nicht_den_kauf(shop):
    shop["a"].catalog.items.append({"name": "Alt", "classname": "A", "price": 1, "max_amount_per_buy": 5,
                                    "purchase_limit": {"max": "kaputt", "per_seconds": None}})
    shop["a"].catalog.rebuild_index()
    assert _kauf(_a(), "Alt")[0] == 200


def test_limit_ids_kommen_als_zeichenketten_zurueck(shop):
    _mit_limit(shop, {"max": 1, "per_seconds": 60, "exceptions": [{"kind": "role", "id": "144700000000000010", "max": 2}]})
    st, d = shop["admin"](shop["a"], "GET", "/api/shop/items", handler=bot.list_items)
    ausn = d["data"]["items"][0]["purchase_limit"]["exceptions"]
    assert ausn == [{"kind": "role", "id": "144700000000000010", "max": 2}]


# ── Alle Rollen noetig, Rabatt je Item, versteckt ───────────────────────
def test_jede_rolle_noetig_statt_irgendeine(shop):
    shop["neu"](shop["a"], {"name": "Beide", "price": 10, "classname": "A", "role_ids": [str(ROLLE_VIP), str(ROLLE_ANDERE)],
                            "require_all_roles": True})
    shop["neu"](shop["a"], {"name": "Eine", "price": 10, "classname": "A", "role_ids": [str(ROLLE_VIP), str(ROLLE_ANDERE)]})
    st, d = _kauf(_a(ANDERER), "Beide")                                        # nur VIP
    assert st in (400, 403) and d["code"] == "rolle"
    assert _kauf(_a(ANDERER), "Eine")[0] == 200                                # irgendeine reicht
    assert _kauf(_a(556), "Beide")[0] == 200                                   # beide vorhanden
    assert _kauf(_a(U), "Eine")[0] in (400, 403)                               # keine Rolle: nie


def test_jede_rolle_noetig_zeigt_sich_in_der_spielerliste(shop):
    shop["neu"](shop["a"], {"name": "Beide", "price": 10, "classname": "A", "role_ids": [str(ROLLE_VIP), str(ROLLE_ANDERE)],
                            "require_all_roles": True})
    st, d = api("GET", "/api/me/shop", _a(ANDERER))
    z = next(i for i in d["data"]["items"] if i["name"] == "Beide")
    assert z["kaufbar"] is False and z["alle_rollen"] is True
    st, d = api("GET", "/api/me/shop", _a(556))
    assert next(i for i in d["data"]["items"] if i["name"] == "Beide")["kaufbar"] is True


def test_rabatt_je_item_nur_fuer_die_rolle(shop):
    shop["neu"](shop["a"], {"name": "Rab", "price": 100, "classname": "A", "max_amount_per_buy": 2,
                            "discounts": [{"role_id": str(ROLLE_VIP), "typ": "percent", "betrag": 50}]})
    vor_u, vor_v = bot.db.get_balance(G_A, U)[0], bot.db.get_balance(G_A, ANDERER)[0]
    assert _kauf(_a(U), "Rab")[0] == 200
    assert _kauf(_a(ANDERER), "Rab", amount=2)[0] == 200
    assert vor_u - bot.db.get_balance(G_A, U)[0] == 100                         # ohne Rolle voller Preis
    assert vor_v - bot.db.get_balance(G_A, ANDERER)[0] == 100                   # VIP: 2 x 50
    st, d = api("GET", "/api/me/shop", _a(ANDERER))
    z = next(i for i in d["data"]["items"] if i["name"] == "Rab")
    assert z["rabatt_preis_text"] and "50" in z["rabatt_preis_text"]
    st, d = api("GET", "/api/me/shop", _a(U))
    assert next(i for i in d["data"]["items"] if i["name"] == "Rab")["rabatt_preis_text"] is None


def test_rabatt_je_item_und_der_guild_zusammen_gilt_nur_der_groesste(shop):
    bot.db.rabatt_ersetzen(G_A, [(ROLLE_VIP, "percent", 20)])
    shop["neu"](shop["a"], {"name": "Rab", "price": 100, "classname": "A",
                            "discounts": [{"role_id": str(ROLLE_VIP), "typ": "fixed", "betrag": 30}]})
    vor = bot.db.get_balance(G_A, ANDERER)[0]
    assert _kauf(_a(ANDERER), "Rab")[0] == 200
    assert vor - bot.db.get_balance(G_A, ANDERER)[0] == 70                      # 30 schlagen 20 %, nicht 50 zusammen


def test_preisvorschau_stimmt_mit_dem_kauf_ueberein(shop):
    shop["neu"](shop["a"], {"name": "Rab", "price": 100, "classname": "A",
                            "discounts": [{"role_id": str(ROLLE_VIP), "typ": "percent", "betrag": 25}]})
    st, d = api("POST", "/api/me/quote", _a(ANDERER), {"art": "shop", "item": "Rab", "amount": 1})
    assert st == 200 and "75" in d["data"]["total_text"]


@pytest.mark.parametrize("rab", [
    [{"role_id": "abc", "typ": "percent", "betrag": 10}], [{"role_id": "5", "typ": "percent", "betrag": 101}],
    [{"role_id": "5", "typ": "percent", "betrag": 0}], [{"role_id": "5", "typ": "bonus", "betrag": 5}],
    [{"role_id": "5", "typ": "fixed", "betrag": 5}, {"role_id": "5", "typ": "fixed", "betrag": 6}],
    [{"role_id": str(i + 1), "typ": "fixed", "betrag": 5} for i in range(16)], "kein Liste"])
def test_ungueltige_rabatte_werden_abgelehnt(shop, rab):
    st, d = shop["neu"](shop["a"], {"name": "X", "price": 1, "classname": "A", "discounts": rab})
    assert st == 400 and not d["ok"]


def test_verstecktes_item_nicht_in_listen_aber_kaufbar(shop):
    shop["neu"](shop["a"], {"name": "Geheim", "price": 10, "classname": "A", "hidden": True})
    shop["neu"](shop["a"], {"name": "Offen", "price": 10, "classname": "A"})
    shop["neu"](shop["a"], {"name": "Aus", "price": 10, "classname": "A", "enabled": False})
    st, d = api("GET", "/api/me/shop", _a())
    assert {i["name"] for i in d["data"]["items"]} == {"Offen"}
    assert _kauf(_a(), "Geheim")[0] == 200                                     # mit genauem Namen kaufbar
    assert _kauf(_a(), "Aus")[0] in (400, 404)                                 # abgeschaltet nie
    index = {wert: sichtbar for _s, _l, wert, sichtbar in shop["a"].catalog._ac_index}
    assert index == {"Geheim": False, "Offen": True, "Aus": False}             # /buy schlaegt Geheim nicht vor
    assert [i["name"] for i in shop["a"].catalog.items if bot._item_sichtbar(i)] == ["Offen"]


def test_verstecktes_item_bleibt_im_admin_katalog_sichtbar(shop):
    shop["neu"](shop["a"], {"name": "Geheim", "price": 10, "classname": "A", "hidden": True})
    st, d = shop["admin"](shop["a"], "GET", "/api/shop/items", handler=bot.list_items)
    assert [(i["name"], i["hidden"]) for i in d["data"]["items"]] == [("Geheim", True)]


# ── Export / Import ──────────────────────────────────────────────────────
def test_export_laesst_rollen_und_ids_weg_aber_behaelt_menge_und_limit(shop):
    shop["neu"](shop["a"], {"name": "Set", "price": 10, "contents": [{"classname": "Canteen", "qty": 3}],
                            "role_ids": [str(ROLLE_VIP)], "require_all_roles": True, "hidden": True,
                            "discounts": [{"role_id": str(ROLLE_VIP), "typ": "percent", "betrag": 10}],
                            "purchase_limit": {"max": 2, "per_seconds": 600,
                                               "exceptions": [{"kind": "role", "id": str(ROLLE_VIP), "max": 5}]}})
    st, d = shop["admin"](shop["a"], "GET", "/api/shop/export", handler=bot.api_shop_export)
    assert st == 200
    roh = json.dumps(d)
    assert str(ROLLE_VIP) not in roh and "discounts" not in roh and "require_all_roles" not in roh and "role_ids" not in roh
    z = d["data"]["items"][0]
    assert z["contents"] == [{"classname": "Canteen", "qty": 3}] and z["hidden"] is True
    assert z["purchase_limit"] == {"max": 2, "per_seconds": 600, "shared": False, "exceptions": []}


def test_import_uebernimmt_menge_versteckt_und_limit_ohne_ausnahmen(shop):
    shop["neu"](shop["a"], {"name": "Set", "price": 10, "contents": [{"classname": "Canteen", "qty": 3},
                                                                      {"classname": "Apple", "qty": 1}],
                            "hidden": True, "purchase_limit": {"max": 2, "per_seconds": 600, "shared": True}})
    _st, ex = shop["admin"](shop["a"], "GET", "/api/shop/export", handler=bot.api_shop_export)
    st, d = shop["admin"](shop["b"], "POST", "/api/shop/import", ex["data"], handler=bot.api_shop_import)
    assert st == 200 and d["data"]["hinzugefuegt"] == 1, d
    neu = shop["b"].catalog.find("Set")
    assert neu["classnames"] == ["Canteen"] * 3 + ["Apple"] and neu["hidden"] is True
    assert neu["purchase_limit"] == {"max": 2, "per_seconds": 600, "shared": True, "exceptions": []}
    assert shop["a"].catalog.find("Set")["classnames"] == ["Canteen"] * 3 + ["Apple"]       # Quelle unveraendert


def test_import_roher_katalog_mit_wiederholten_classnames_behaelt_die_menge(shop):
    st, d = shop["admin"](shop["b"], "POST", "/api/shop/import",
                          {"items": [{"name": "Zwei", "price": 5, "classnames": ["Apple", "Apple"]},
                                     {"name": "Kaputt", "price": 5, "contents": [{"classname": "X", "qty": 0}]}]},
                          handler=bot.api_shop_import)
    assert st == 200 and d["data"]["hinzugefuegt"] == 1 and d["data"]["ungueltig"] == 1
    assert shop["b"].catalog.find("Zwei")["classnames"] == ["Apple", "Apple"]


def test_katalog_ueberlebt_speichern_und_neu_laden(shop, tmp_path):
    shop["neu"](shop["a"], {"name": "Set", "price": 10, "contents": [{"classname": "Canteen", "qty": 3}],
                            "hidden": True, "purchase_limit": {"max": 2, "per_seconds": 600}})
    neu = bot.ShopCatalog("1000", str(tmp_path / "shop_1000.json"))
    neu.load()
    it = neu.find("Set")
    assert it["classnames"] == ["Canteen"] * 3 and it["hidden"] is True and it["purchase_limit"]["max"] == 2
