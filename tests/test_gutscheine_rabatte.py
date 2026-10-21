"""Gutscheine und dauerhafte Rabatte pro Rolle – Item-Shop, Mieten, Dashboard (Spieler und Betreiber).

Reihenfolge der Preisberechnung: bester Rollen-Rabatt, dann der Gutschein auf den reduzierten Preis; ein fester
Betrag gilt je Kauf; der Endpreis ist nie negativ. Alles je Guild. Die Einloesung eines Gutscheins ist atomar und
wird bei jedem gescheiterten Kauf zurueckgenommen.

    python3 -m pytest tests/test_gutscheine_rabatte.py -q
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
from test_airstrike import servers  # noqa: E402,F401 - Fixture (zwei Server 1000/2000)
from test_spieler_dashboard import (  # noqa: E402,F401 - Fixtures und Helfer des Spielerbereichs
    welt, miete, api, _session, _kauf, _mieten, _miet_events, G_A, G_B, U, ANDERER, ROLLE_VIP, ROLLE_ANDERE)

SID = "1000"


def _run(coro):
    return asyncio.run(coro)


def _gut(gid=G_A, code="SPAR10", typ="percent", betrag=10, enabled=True, max_uses=None, von=None, bis=None):
    assert bot.db.gutschein_speichern(gid, code, typ, betrag, enabled, max_uses, von, bis, True) == "ok"


def _admin(gid=G_A, sid_="1000", uid="1000"):
    """Besitzer des Servers (kein Bot-Betreiber) mit gewaehlter Guild."""
    return bot._sess_create("fake", [], discord_user={"id": uid, "name": "Owner"}, service_id=sid_,
                            owned_guilds=[{"id": str(gid), "name": "G"}], guild_id=str(gid))


# ── Rechnen ──────────────────────────────────────────────────────────────
def test_abzug_fest_prozent_und_deckel():
    assert bot._abzug("fixed", 100, 1000) == 100
    assert bot._abzug("fixed", 5000, 1000) == 1000          # nie mehr als der Preis
    assert bot._abzug("percent", 10, 1000) == 100
    assert bot._abzug("percent", 150, 1000) == 1000         # Prozent wird bei 100 gedeckelt
    assert bot._abzug("percent", 10, 0) == 0 and bot._abzug("fixed", 0, 1000) == 0


def test_gutschein_code_format():
    assert bot._gutschein_code(" spar-10 ") == "SPAR-10"
    for schlecht in ("ab", "a" * 33, "mit leerzeichen", "ä€", "", None):
        assert bot._gutschein_code(schlecht) is None


def test_bester_rabatt_gewinnt_und_gutschein_kommt_obendrauf(welt):
    bot.db.rabatt_ersetzen(G_A, [(ROLLE_VIP, "percent", 10), (ROLLE_ANDERE, "fixed", 150)])
    p = bot._preis_berechnen(G_A, {ROLLE_VIP, ROLLE_ANDERE}, 1000, None, False)
    assert (p.rabatt, p.total) == (150, 850)                # 150 fest schlaegt 10 % (=100), nie beide
    _gut(code="MINUS50", typ="fixed", betrag=50)
    p = bot._preis_berechnen(G_A, {ROLLE_VIP, ROLLE_ANDERE}, 1000, "minus50", False)
    assert (p.rabatt, p.gutschein_abzug, p.total) == (150, 50, 800)
    _gut(code="HALB", typ="percent", betrag=50)
    p = bot._preis_berechnen(G_A, {ROLLE_VIP}, 1000, "HALB", False)
    assert (p.rabatt, p.gutschein_abzug, p.total) == (100, 450, 450)     # 50 % von 900


def test_preis_nie_negativ_und_ohne_rolle_kein_rabatt(welt):
    bot.db.rabatt_ersetzen(G_A, [(ROLLE_VIP, "fixed", 5000)])
    assert bot._preis_berechnen(G_A, {ROLLE_VIP}, 1000, None, False).total == 0
    assert bot._preis_berechnen(G_A, set(), 1000, None, False).total == 1000
    assert bot._preis_berechnen(G_A, None, 1000, None, False).total == 1000


def test_rabatte_und_gutscheine_gelten_nur_in_ihrer_guild(welt):
    bot.db.rabatt_ersetzen(G_A, [(ROLLE_VIP, "percent", 50)])
    _gut(G_A, "NURA")
    assert bot._preis_berechnen(G_B, {ROLLE_VIP}, 1000, None, False).total == 1000
    fehler = bot._preis_berechnen(G_B, set(), 1000, "NURA", False)
    assert isinstance(fehler, bot._KaufFehler) and fehler.code == "gutschein"


@pytest.mark.parametrize("kw,text", [
    (dict(enabled=False), "nicht aktiv"), (dict(bis=time.time() - 60), "abgelaufen"),
    (dict(von=time.time() + 3600), "noch nicht"), (dict(max_uses=1), "zu oft")])
def test_gutschein_wird_geprueft(welt, kw, text):
    _gut(code="PRUEF", **kw)
    if kw.get("max_uses"):
        assert bot.db.gutschein_einloesen(G_A, "PRUEF", U, "t")
    f = bot._preis_berechnen(G_A, set(), 1000, "PRUEF", False)
    assert isinstance(f, bot._KaufFehler) and text in f.text(False)
    assert "Gutschein" in bot._preis_berechnen(G_A, set(), 1000, "!!", False).text(False)
    assert "does not exist" in bot._preis_berechnen(G_A, set(), 1000, "NIX123", True).text(True)


# ── Einloesen (atomar) ───────────────────────────────────────────────────
def test_einloesen_begrenzt_und_freigeben(welt):
    _gut(code="ZWEIMAL", max_uses=2)
    assert bot.db.gutschein_einloesen(G_A, "ZWEIMAL", U, "t") and bot.db.gutschein_einloesen(G_A, "ZWEIMAL", U, "t")
    assert not bot.db.gutschein_einloesen(G_A, "ZWEIMAL", U, "t")
    assert bot.db.gutschein_holen(G_A, "ZWEIMAL")["uses"] == 2
    bot.db.gutschein_freigeben(G_A, "ZWEIMAL", U)
    assert bot.db.gutschein_holen(G_A, "ZWEIMAL")["uses"] == 1
    assert bot.db.gutschein_einloesen(G_A, "ZWEIMAL", U, "t")
    bot.db.gutschein_freigeben(G_A, "ZWEIMAL", U)
    bot.db.gutschein_freigeben(G_A, "ZWEIMAL", U)
    bot.db.gutschein_freigeben(G_A, "ZWEIMAL", U)
    assert bot.db.gutschein_holen(G_A, "ZWEIMAL")["uses"] == 0          # nie unter 0


def test_einloesen_unbekannt_inaktiv_zeitraum(welt):
    assert not bot.db.gutschein_einloesen(G_A, "GIBTSNICHT", U, "t")
    _gut(code="AUS", enabled=False)
    _gut(code="ALT", bis=time.time() - 1)
    _gut(code="SPAET", von=time.time() + 600)
    for c in ("AUS", "ALT", "SPAET"):
        assert not bot.db.gutschein_einloesen(G_A, c, U, "t")
        assert bot.db.gutschein_holen(G_A, c)["uses"] == 0
    assert not bot.db.gutschein_einloesen(G_B, "AUS", U, "t")            # andere Guild


def test_aendern_behaelt_nutzungszaehler(welt):
    _gut(code="BLEIBT", max_uses=5)
    bot.db.gutschein_einloesen(G_A, "BLEIBT", U, "t")
    assert bot.db.gutschein_speichern(G_A, "BLEIBT", "fixed", 7, True, 9, None, None, False) == "ok"
    row = bot.db.gutschein_holen(G_A, "BLEIBT")
    assert (row["uses"], row["typ"], row["betrag"], row["max_uses"]) == (1, "fixed", 7, 9)
    assert bot.db.gutschein_speichern(G_A, "BLEIBT", "fixed", 7, True, 9, None, None, True) == "existiert"
    assert bot.db.gutschein_speichern(G_A, "NIX", "fixed", 7, True, 9, None, None, False) == "nicht_gefunden"


# ── Item-Shop: _kauf_pruefen / _kauf_ausfuehren ──────────────────────────
def test_kauf_mit_rollenrabatt_und_gutschein_bucht_den_endpreis(welt):
    bot.db.rabatt_ersetzen(G_A, [(ROLLE_VIP, "percent", 10)])
    _gut(code="MINUS50", typ="fixed", betrag=50, max_uses=3)
    bot.db.add_wallet(G_A, ANDERER, 1000)
    vor = bot.db.get_balance(G_A, ANDERER)[0]
    plan = bot._kauf_pruefen(welt["a"], G_A, ANDERER, "Kiste", 2, {ROLLE_VIP}, None, 100.0, 200.0, None, False, "minus50")
    assert (plan.preis.liste, plan.preis.rabatt, plan.preis.gutschein_abzug, plan.total) == (200, 20, 50, 130)
    res = _run(bot._kauf_ausfuehren(welt["a"], G_A, ANDERER, "A", plan))
    assert res.ok and bot.db.get_balance(G_A, ANDERER)[0] == vor - 130
    assert bot.db.gutschein_holen(G_A, "MINUS50")["uses"] == 1
    assert bot.db.purchases_of_user(SID, G_A, ANDERER, 1)[0]["total_price"] == 130


def test_kauf_scheitert_der_gutschein_wird_nicht_verbraucht(welt):
    _gut(code="EINMAL", max_uses=1)
    bot.db.add_wallet(G_A, U, 1000)
    plan = bot._kauf_pruefen(welt["a"], G_A, U, "Kiste", 1, set(), None, 100.0, 200.0, None, False, "EINMAL")
    welt["a"].ftp = None                                     # Lieferung nicht moeglich
    res = _run(bot._kauf_ausfuehren(welt["a"], G_A, U, "U", plan))
    assert not res.ok and bot.db.gutschein_holen(G_A, "EINMAL")["uses"] == 0


def test_gutschein_zwischendurch_aufgebraucht_rollt_zurueck(welt):
    """Zwischen Pruefen und Bezahlen nimmt jemand die letzte Nutzung: nichts wird abgebucht, die Lieferung zurueckgerollt."""
    _gut(code="LETZTER", max_uses=1)
    bot.db.add_wallet(G_A, U, 1000)
    vor = bot.db.get_balance(G_A, U)[0]
    plan = bot._kauf_pruefen(welt["a"], G_A, U, "Kiste", 1, set(), None, 100.0, 200.0, None, False, "LETZTER")
    assert not isinstance(plan, bot._KaufFehler)
    assert bot.db.gutschein_einloesen(G_A, "LETZTER", ANDERER, "t")           # der andere war schneller
    entfernt = []

    async def entfernen(namen):
        entfernt.extend(namen)
        return True
    welt["a"].shop.remove_area_entries = entfernen
    res = _run(bot._kauf_ausfuehren(welt["a"], G_A, U, "U", plan))
    assert not res.ok and res.code == "gutschein" and entfernt
    assert bot.db.get_balance(G_A, U)[0] == vor and bot.db.gutschein_holen(G_A, "LETZTER")["uses"] == 1
    assert bot.db.purchases_of_user(SID, G_A, U, 5) == []


def test_zu_wenig_geld_gibt_den_gutschein_wieder_frei(welt):
    _gut(code="FREI", typ="fixed", betrag=1)
    bot.db.try_spend_wallet(G_A, U, bot.db.get_balance(G_A, U)[0])
    bot.db.add_wallet(G_A, U, 100)
    plan = bot._kauf_pruefen(welt["a"], G_A, U, "Kiste", 1, set(), None, 100.0, 200.0, None, False, None)
    assert plan.total == 100                                  # reicht genau
    bot.db.try_spend_wallet(G_A, U, 100)                      # Geld weg, bevor bezahlt wird
    plan.preis = bot._preis_berechnen(G_A, set(), 100, "FREI", False)
    res = _run(bot._kauf_ausfuehren(welt["a"], G_A, U, "U", plan))
    assert not res.ok and res.code == "guthaben" and bot.db.gutschein_holen(G_A, "FREI")["uses"] == 0


def test_ungueltiger_gutschein_bricht_den_kauf_ab(welt):
    f = bot._kauf_pruefen(welt["a"], G_A, U, "Kiste", 1, set(), None, 1.0, 1.0, None, False, "GIBTSNICHT")
    assert isinstance(f, bot._KaufFehler) and f.code == "gutschein"


# ── Mieten ───────────────────────────────────────────────────────────────
def test_miete_mit_rabatt_und_gutschein(welt, miete):
    bot.db.rabatt_ersetzen(G_A, [(ROLLE_VIP, "percent", 50)])
    _gut(code="MIETE20", typ="fixed", betrag=20)
    bot.db.add_wallet(G_A, ANDERER, 1000)
    vor = bot.db.get_balance(G_A, ANDERER)[0]
    st, d = _mieten(_session(uid=ANDERER, gid=G_A, sid_="1000"), x=100, z=100, gutschein="mi ete20".replace(" ", ""))
    assert st == 200, d
    assert bot.db.get_balance(G_A, ANDERER)[0] == vor - 80                 # 200 -> 100 (50 %) -> 80
    assert "Listenpreis" in d["data"]["preis_zeile"] or "List price" in d["data"]["preis_zeile"]
    assert bot.db.gutschein_holen(G_A, "MIETE20")["uses"] == 1


def test_miete_ftp_fehler_gutschein_bleibt_frei(welt, miete):
    from test_spieler_dashboard import _SchreibFehlerFTP
    miete.ftp = _SchreibFehlerFTP()
    _gut(code="MIETE5", typ="fixed", betrag=5, max_uses=1)
    bot.db.add_wallet(G_A, U, 1000)
    st, _d = _mieten(_session(gid=G_A, sid_="1000"), x=1, z=1, gutschein="MIETE5")
    assert st == 500 and bot.db.gutschein_holen(G_A, "MIETE5")["uses"] == 0


# ── Spieler-API ──────────────────────────────────────────────────────────
def _quote(s, **kw):
    return api("POST", "/api/me/quote", s, kw)


def test_quote_shop_und_miete(welt, miete):
    bot.db.rabatt_ersetzen(G_A, [(ROLLE_VIP, "percent", 10)])
    _gut(code="MINUS50", typ="fixed", betrag=50)
    s = _session(uid=ANDERER, gid=G_A, sid_="1000")
    st, d = _quote(s, art="shop", item="Kiste", amount=2, gutschein="minus50")
    assert st == 200 and (d["data"]["rabatt"], d["data"]["gutschein_abzug"]) == (20, 50)
    assert d["data"]["total_text"].startswith("130")
    st, d = _quote(s, art="rental", item="Olga", restarts=3)
    assert st == 200 and d["data"]["rabatt"] == 30 and d["data"]["total_text"].startswith("270")
    # ohne Rolle kein Rabatt
    st, d = _quote(_session(gid=G_A, sid_="1000"), art="shop", item="Kiste", amount=2)
    assert d["data"]["rabatt"] == 0 and d["data"]["total_text"].startswith("200")


def test_quote_fehler(welt, miete):
    s = _session(gid=G_A, sid_="1000")
    assert _quote(s, art="shop", item="Kiste", amount=1, gutschein="NIX123")[0] == 400
    assert _quote(s, art="shop", item="Gibt es nicht", amount=1)[0] == 404
    assert _quote(s, art="shop", item="Aus", amount=1)[0] == 404
    assert _quote(s, art="shop", item="Kiste", amount="x")[0] == 400
    assert _quote(s, art="shop", item="Kiste", amount=0)[0] == 400
    assert _quote(s, art="unsinn", item="Kiste")[0] == 400
    assert _quote(_session(), art="shop", item="Kiste")[0] == 409                 # kein Server gewaehlt
    assert api("POST", "/api/me/quote", None, {})[0] == 401


def test_kauf_per_dashboard_mit_gutschein(welt):
    _gut(code="MINUS50", typ="fixed", betrag=50, max_uses=1)
    bot.db.add_wallet(G_A, U, 1000)
    vor = bot.db.get_balance(G_A, U)[0]
    s = _session(gid=G_A, sid_="1000")
    st, d = _kauf(s, x=100, z=100, gutschein="MINUS50")
    assert st == 200 and bot.db.get_balance(G_A, U)[0] == vor - 50 and "MINUS50" in d["data"]["preis_zeile"]
    st, d = _kauf(s, x=100, z=100, gutschein="MINUS50")                             # aufgebraucht
    assert st == 400 and d["code"] == "gutschein" and bot.db.get_balance(G_A, U)[0] == vor - 50


def test_shop_und_miet_listen_zeigen_die_rabatte_der_rollen(welt, miete):
    bot.db.rabatt_ersetzen(G_A, [(ROLLE_VIP, "percent", 10), (ROLLE_ANDERE, "fixed", 5)])
    vip = _session(uid=ANDERER, gid=G_A, sid_="1000")
    d = api("GET", "/api/me/shop?lang=de", vip)[1]["data"]
    assert [r["text"] for r in d["rabatte"]] == ["10 %"]
    assert api("GET", "/api/me/rentals?lang=de", vip)[1]["data"]["rabatte"][0]["typ"] == "percent"
    assert api("GET", "/api/me/shop", _session(gid=G_A, sid_="1000"))[1]["data"]["rabatte"] == []


# ── Betreiber-API ────────────────────────────────────────────────────────
def test_admin_gutscheine_anlegen_listen_aendern_loeschen(welt):
    sid = _admin()
    st, d = api("POST", "/api/coupons", sid, {"code": "neu-10", "typ": "percent", "betrag": 10, "max_uses": 5})
    assert st == 200 and d["data"]["code"] == "NEU-10", d
    assert api("POST", "/api/coupons", sid, {"code": "NEU-10", "typ": "fixed", "betrag": 5})[0] == 409
    st, d = api("GET", "/api/coupons", sid)
    c = d["data"]["coupons"]
    assert st == 200 and len(c) == 1 and (c[0]["status"], c[0]["uses"], c[0]["max_uses"]) == ("active", 0, 5)
    assert api("PUT", "/api/coupons/neu-10", sid, {"typ": "fixed", "betrag": 25, "enabled": False})[0] == 200
    c = api("GET", "/api/coupons", sid)[1]["data"]["coupons"][0]
    assert (c["typ"], c["betrag"], c["status"]) == ("fixed", 25, "disabled")
    assert api("PUT", "/api/coupons/gibtsnicht", sid, {"typ": "fixed", "betrag": 5})[0] == 404
    assert api("DELETE", "/api/coupons/NEU-10", sid)[0] == 200
    assert api("DELETE", "/api/coupons/NEU-10", sid)[0] == 404
    assert api("GET", "/api/coupons", sid)[1]["data"]["coupons"] == []


def test_admin_gutschein_validierung(welt):
    sid = _admin()
    bad = [{"code": "ab", "typ": "fixed", "betrag": 5}, {"code": "OK1", "typ": "unsinn", "betrag": 5},
           {"code": "OK1", "typ": "percent", "betrag": 101}, {"code": "OK1", "typ": "percent", "betrag": 0},
           {"code": "OK1", "typ": "fixed", "betrag": "x"}, {"code": "OK1", "typ": "fixed", "betrag": 5, "max_uses": 0},
           {"code": "OK1", "typ": "fixed", "betrag": 5, "valid_from": 2000000000, "valid_until": 1900000000},
           {"code": "OK1", "typ": "fixed", "betrag": 5, "valid_from": "abc"}]
    for b in bad:
        st, d = api("POST", "/api/coupons", sid, b)
        assert st == 400 and not d["ok"], b
    assert bot.db.gutschein_liste(G_A) == []


def test_admin_rabatt_regeln(welt):
    sid = _admin()
    regeln = [{"role_id": str(ROLLE_VIP), "typ": "percent", "betrag": 10}]
    assert api("PUT", "/api/discounts", sid, {"rules": regeln})[0] == 200
    st, d = api("GET", "/api/coupons", sid)
    assert d["data"]["rabatte"][0]["role_name"] == "VIP" and d["data"]["rabatte"][0]["betrag"] == 10
    bad = [{"rules": regeln + regeln},                                              # doppelte Rolle
           {"rules": [{"role_id": "999", "typ": "percent", "betrag": 10}]},         # Rolle gibt es nicht
           {"rules": [{"role_id": str(ROLLE_VIP), "typ": "percent", "betrag": 200}]},
           {"rules": [{"role_id": "abc", "typ": "fixed", "betrag": 5}]}, {"rules": "x"}, {"rules": ["x"]},
           {"rules": [{"role_id": str(ROLLE_VIP), "typ": "fixed", "betrag": 5}] * 30}]
    for b in bad:
        assert api("PUT", "/api/discounts", sid, b)[0] == 400, b
    assert len(bot.db.rabatt_liste(G_A)) == 1                                       # nichts davon hat die Liste veraendert
    assert api("PUT", "/api/discounts", sid, {"rules": []})[0] == 200 and bot.db.rabatt_liste(G_A) == []


def test_admin_sicht_ist_je_guild_getrennt(welt):
    _gut(G_B, "NURB")
    assert api("GET", "/api/coupons", _admin())[1]["data"]["coupons"] == []         # Server 1000 / Guild A sieht Guild B nicht
    assert api("DELETE", "/api/coupons/NURB", _admin())[0] == 404
    assert bot.db.gutschein_holen(G_B, "NURB") is not None


def test_spieler_und_fremde_kommen_nicht_an_die_admin_routen(welt):
    spieler = _session(gid=G_A, sid_="1000")
    for methode, pfad, body in (("GET", "/api/coupons", None), ("POST", "/api/coupons", {"code": "XYZ", "typ": "fixed", "betrag": 5}),
                                ("PUT", "/api/discounts", {"rules": []}), ("DELETE", "/api/coupons/XYZ", None)):
        st, d = api(methode, pfad, spieler, body)
        assert st in (401, 403, 409) and not (d or {}).get("ok"), (pfad, st)
        assert api(methode, pfad, None, body)[0] == 401
    assert bot.db.gutschein_liste(G_A) == [] and bot.db.rabatt_liste(G_A) == []
    # ein fremder Besitzer (anderer Server 2000) kann den Gutschein-Bestand von Server 1000/Guild A nicht anfassen
    fremd = _admin(gid=G_B, sid_="2000", uid="2000")
    assert api("POST", "/api/coupons", fremd, {"code": "FREMD", "typ": "fixed", "betrag": 5})[0] == 200
    assert bot.db.gutschein_holen(G_B, "FREMD") is not None and bot.db.gutschein_holen(G_A, "FREMD") is None


def test_ohne_zugeordnete_guild_gesperrt(welt):
    """Ohne Freischaltung (keine Guild) greift schon die Premium-Sperre des Dashboards - kein Zugriff."""
    bot.connections.assign_guild(welt["a"].service_id, None)
    st, d = api("GET", "/api/coupons", _admin())
    assert st in (403, 409) and not d["ok"]


# ── Schutz vor dem Durchprobieren von Codes ──────────────────────────────
@pytest.fixture(autouse=True)
def _fehlversuche_leeren():
    bot._GUTSCHEIN_FEHLVERSUCHE.clear()
    yield
    bot._GUTSCHEIN_FEHLVERSUCHE.clear()


def test_zu_viele_falsche_codes_sperren_die_eingabe(welt):
    _gut(code="ECHT10")
    for i in range(bot._GUTSCHEIN_FEHLVERSUCHE_MAX):
        f = bot._preis_berechnen(G_A, set(), 100, f"FALSCH{i}", False, U)
        assert f.code == "gutschein" and "gibt es nicht" in f.text(False)
    f = bot._preis_berechnen(G_A, set(), 100, "ECHT10", False, U)            # jetzt auch der echte Code: gesperrt
    assert isinstance(f, bot._KaufFehler) and "Zu viele" in f.text(False)
    assert not isinstance(bot._preis_berechnen(G_A, set(), 100, "ECHT10", False, ANDERER), bot._KaufFehler)   # anderes Konto frei
    assert not isinstance(bot._preis_berechnen(G_A, set(), 100, None, False, U), bot._KaufFehler)             # ohne Code immer moeglich
    # nach dem Zeitfenster geht es wieder
    bot._GUTSCHEIN_FEHLVERSUCHE[U] = [time.time() - bot._GUTSCHEIN_FEHLVERSUCHE_FENSTER - 5] * 8
    assert not isinstance(bot._preis_berechnen(G_A, set(), 100, "ECHT10", False, U), bot._KaufFehler)


def test_abgelaufene_oder_aufgebrauchte_codes_zaehlen_nicht_als_fehlversuch(welt):
    _gut(code="ALT", bis=time.time() - 5)
    for _ in range(20):
        bot._preis_berechnen(G_A, set(), 100, "ALT", False, U)
    assert U not in bot._GUTSCHEIN_FEHLVERSUCHE


def test_dashboard_quote_wird_ebenfalls_gesperrt(welt, miete):
    s = _session(gid=G_A, sid_="1000")
    for i in range(bot._GUTSCHEIN_FEHLVERSUCHE_MAX):
        bot._DASH_RATE_LIMIT_LAST.clear()
        assert _quote(s, art="shop", item="Kiste", amount=1, gutschein=f"NIX{i}000")[0] == 400
    bot._DASH_RATE_LIMIT_LAST.clear()
    st, d = _quote(s, art="shop", item="Kiste", amount=1, gutschein="NOCHMAL")
    assert st == 400 and "Too many" in d["error"] or "Zu viele" in d["error"]
