"""Glücksrad, zweiter Teil: Tageslimit, Gratis-Käufe im Shop (Gewinn „Shop-Item“) und offene Gewinne zum Einlösen
(Klassennamen / events.xml-Eintrag, /claim wheel prize und Spielerbereich).

Kein Test geht ins Internet oder zu Discord; FTP, Shop und Discord sind Test-Doppel.

    python3 -m pytest tests/test_gluecksrad_gewinne.py -q
"""
import asyncio
import json
import os
import sys

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
import test_shop_rentals as TR
from test_spieler_dashboard import G_A, G_B, U, ANDERER, ROLLE_VIP, _session, api, welt  # noqa: F401 - Fixtures
from test_airstrike import servers  # noqa: F401 - Fixture (zwei Server 1000/2000)
from test_gluecksrad import _Inter, _admin, _drehen, _einstellen, _lauf, _preis, slash  # noqa: F401 - Helfer und Fixture

TAG = 86400
MITTAG = 1_800_000_000 - 1_800_000_000 % TAG + 12 * 3600          # 12:00 UTC eines festen Tages


@pytest.fixture(autouse=True)
def _frisches_audit_log(monkeypatch):
    monkeypatch.setattr(bot, "_audit_log", bot.deque(maxlen=100))
    bot._RAD_LOCKS.clear()


@pytest.fixture
def uhr(monkeypatch):
    """Feste Uhr (12:00 UTC), damit kein Test um Mitternacht kippt. uhr["t"] verstellen = Zeit vergeht."""
    zeit = {"t": float(MITTAG)}
    monkeypatch.setattr(bot.time, "time", lambda: zeit["t"])
    return zeit


def _event_xml(klasse="CivilianSedan"):
    return bot._rental_vorlage_bauen(klasse)["event_xml"]


class _ShopDoppel:
    """Merkt sich jede Lieferung vollständig und kann sie scheitern lassen."""

    def __init__(self):
        self.geliefert, self.entfernt, self.ok = [], [], True

    async def add_purchase_entries(self, cls_list, amount, x, y, z):
        if not self.ok:
            return False, "FTP write failed", []
        namen = [f"area{len(self.geliefert)}_{i}" for i in range(amount * len(cls_list))]
        self.geliefert.append({"klassen": list(cls_list), "menge": amount, "x": x, "y": y, "z": z, "namen": namen})
        return True, "", namen

    async def remove_area_entries(self, names):
        self.entfernt.append(list(names))
        return True

    def schedule_auto_restart(self):
        pass


@pytest.fixture
def lager(welt):
    """Server A mit Shop-Doppel und events.xml/cfgeventspawns.xml im Speicher."""
    a = welt["a"]
    a.shop, a.ftp = _ShopDoppel(), TR._FakeFTP()
    a.data["ftp_mission_dir"] = TR.MISSION
    return welt


def _gewinn(welt, typ="klassen", uid=U, gid=G_A, sid="1000", name="Mosin", daten=None):
    if daten is None:
        daten = {"klassen": ["Mosin9130", "Ammo_762x54"], "menge": 2} if typ == "klassen" else {"event_xml": _event_xml()}
    return bot.db.rad_gewinn_add(gid, sid, uid, typ, name, daten)


def _einloesen(welt, gewinn_id, uid=U, gid=G_A, conn="a", ort=None, x=4640.0, z=10350.0, en=False):
    return asyncio.run(bot._rad_gewinn_einloesen(welt[conn], gid, uid, "User", gewinn_id, ort, x, z, en))


def _offen(uid=U, gid=G_A, sid="1000"):
    return [int(r["id"]) for r in bot.db.rad_gewinne_offen(gid, sid, uid)]


# ── Tageslimit ──────────────────────────────────────────────────────────────
def test_tageslimit_zaehlt_alle_quellen_und_sperrt_genau_ab_dem_limit(welt, uhr):
    sicht = _einstellen(welt, [_preis(betrag=5)], kosten=10, gratis_stunden=1, max_pro_tag=3)
    bot.db.add_wallet(G_A, U, 100)
    bot.db.rad_drehungen_aendern(G_A, U, 5, 10)
    assert _drehen(welt, sicht, "gratis")["ok"] and _drehen(welt, sicht, "bezahlt")["ok"] and _drehen(welt, sicht, "gespeichert")["ok"]
    wallet, gespeichert = bot.db.get_balance(G_A, U)[0], bot.db.rad_spieler(G_A, U)["drehungen"]
    uhr["t"] += 2 * 3600                                                # die Gratis-Drehung wäre wieder bereit
    for quelle in ("gespeichert", "bezahlt", "gratis"):
        erg = _drehen(welt, sicht, quelle)
        assert erg["ok"] is False and erg["code"] == "tageslimit" and "3×" in erg["de"] and "tomorrow" in erg["en"], (quelle, erg)
    # bei der Sperre wurde nichts verbraucht
    assert bot.db.get_balance(G_A, U)[0] == wallet and bot.db.rad_spieler(G_A, U)["drehungen"] == gespeichert
    assert bot.db.rad_spieler(G_A, U)["letzter_gratis"] == float(MITTAG) and len(bot.db.rad_log(G_A, U, 50)) == 3


def test_tageslimit_neuer_utc_tag_gibt_wieder_frei(welt, uhr):
    sicht = _einstellen(welt, [_preis()], max_pro_tag=1)
    bot.db.rad_drehungen_aendern(G_A, U, 5, 10)
    assert _drehen(welt, sicht, "gespeichert")["ok"] and _drehen(welt, sicht, "gespeichert")["code"] == "tageslimit"
    uhr["t"] = MITTAG + 12 * 3600 - 1                                   # 23:59:59 UTC: noch derselbe Tag
    assert _drehen(welt, sicht, "gespeichert")["code"] == "tageslimit"
    uhr["t"] = MITTAG + 12 * 3600                                       # 00:00:00 UTC: neuer Tag
    assert _drehen(welt, sicht, "gespeichert")["ok"] and _drehen(welt, sicht, "gespeichert")["code"] == "tageslimit"


def test_tageslimit_null_ist_unbegrenzt_und_gilt_je_spieler(welt, uhr):
    sicht = _einstellen(welt, [_preis()], max_pro_tag=0)
    bot.db.rad_drehungen_aendern(G_A, U, 10, 10)
    assert all(_drehen(welt, sicht, "gespeichert")["ok"] for _ in range(10))
    sicht = _einstellen(welt, [_preis()], max_pro_tag=1)
    assert _drehen(welt, sicht, "gespeichert")["code"] == "tageslimit"          # U hat heute schon 10
    bot.db.rad_drehungen_aendern(G_A, ANDERER, 1, 10)
    assert _drehen(welt, sicht, "gespeichert", uid=ANDERER)["ok"]               # ein anderer Spieler zählt eigen


def test_tageslimit_zwei_gleichzeitige_drehungen_passieren_nicht_beide(welt, uhr):
    sicht = _einstellen(welt, [_preis()], max_pro_tag=1)
    bot.db.rad_drehungen_aendern(G_A, U, 2, 10)

    async def beide():
        return await asyncio.gather(*(bot._rad_drehen(welt["a"], sicht, welt["guilds"][G_A], G_A, U, "User", "gespeichert") for _ in range(2)))
    erg = asyncio.run(beide())
    assert sorted(e["ok"] for e in erg) == [False, True] and bot.db.rad_spieler(G_A, U)["drehungen"] == 1


def test_tageslimit_fehlgeschlagene_auszahlung_zaehlt_nicht(welt, uhr):
    sicht = _einstellen(welt, [{"typ": "item", "name": "", "gewicht": 1, "item": "Gibt es nicht", "menge": 1}], max_pro_tag=1)
    bot.db.rad_drehungen_aendern(G_A, U, 2, 10)
    assert _drehen(welt, sicht, "gespeichert")["code"] == "auszahlung"
    assert _drehen(welt, sicht, "gespeichert")["code"] == "auszahlung" and bot.db.rad_spieler(G_A, U)["drehungen"] == 2


def test_tageslimit_in_status_und_api(welt, uhr):
    sicht = _einstellen(welt, [_preis()], max_pro_tag=2, name="Zivilisten-Rad")
    bot.db.rad_drehungen_aendern(G_A, U, 5, 10)
    s = _session(gid=G_A, sid_="1000")
    st, d = api("GET", "/api/me/rad", s)
    assert st == 200 and d["data"]["name"] == "Zivilisten-Rad"
    assert d["data"]["tag"] == {"max": 2, "heute": 0, "rest": 2, "neu_in_sekunden": 12 * 3600}
    assert api("POST", "/api/me/rad/drehen", s, {"quelle": "gespeichert"})[0] == 200
    assert api("POST", "/api/me/rad/drehen", s, {"quelle": "gespeichert"})[0] == 200
    st, d = api("POST", "/api/me/rad/drehen", s, {"quelle": "gespeichert"})
    assert st == 409 and d["code"] == "tageslimit"
    assert api("GET", "/api/me/rad", s)[1]["data"]["tag"]["rest"] == 0
    assert bot._rad_status(welt["a"], _einstellen(welt, [_preis()]), None, G_A, U, False)["tag"]["rest"] is None     # ohne Limit
    assert sicht is not None


# ── Gratis-Käufe: Datenbank ─────────────────────────────────────────────────
def test_db_gratis_verbrauchen_alles_oder_nichts_und_frueher_verfall_zuerst(welt, uhr):
    db = bot.db
    db.rad_gratis_add(G_A, "1000", U, "Kiste", 2, 0.0)                     # verfällt nie
    db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, MITTAG + 5 * TAG)
    db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, MITTAG + 2 * TAG)
    assert db.rad_gratis_stand(G_A, "1000", U)["kiste"] == {"name": "Kiste", "menge": 4, "ablauf": float(MITTAG + 2 * TAG)}
    assert db.rad_gratis_verbrauchen(G_A, "1000", U, "Kiste", 5) is None       # zu viele: nichts verändert
    assert db.rad_gratis_stand(G_A, "1000", U)["kiste"]["menge"] == 4
    genommen = db.rad_gratis_verbrauchen(G_A, "1000", U, "kiste", 3)           # Groß-/Kleinschreibung egal
    assert [(g["menge"], g["ablauf"]) for g in genommen] == [(1, MITTAG + 2 * TAG), (1, MITTAG + 5 * TAG), (1, 0.0)]
    assert db.rad_gratis_stand(G_A, "1000", U)["kiste"] == {"name": "Kiste", "menge": 1, "ablauf": 0.0}
    db.rad_gratis_zurueck(G_A, "1000", U, genommen)                            # auch die gelöschten Zeilen kommen wieder
    assert db.rad_gratis_stand(G_A, "1000", U)["kiste"] == {"name": "Kiste", "menge": 4, "ablauf": float(MITTAG + 2 * TAG)}
    assert db.rad_gratis_verbrauchen(G_A, "1000", U, "Kiste", 0) == [] and db.rad_gratis_verbrauchen(G_A, "1000", U, "Kiste", 4)
    assert db.rad_gratis_stand(G_A, "1000", U) == {}


def test_db_gratis_abgelaufene_zaehlen_nicht(welt, uhr):
    db = bot.db
    db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, MITTAG + TAG)
    uhr["t"] = MITTAG + TAG - 1
    assert db.rad_gratis_stand(G_A, "1000", U)["kiste"]["menge"] == 1
    uhr["t"] = MITTAG + TAG
    assert db.rad_gratis_stand(G_A, "1000", U) == {} and db.rad_gratis_verbrauchen(G_A, "1000", U, "Kiste", 1) is None


def test_db_gratis_getrennt_nach_spieler_guild_server_und_item(welt):
    db = bot.db
    db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, 0.0)
    for gid, sid, uid, item in ((G_A, "1000", ANDERER, "Kiste"), (G_B, "1000", U, "Kiste"), (G_A, "2000", U, "Kiste"), (G_A, "1000", U, "Vip-Kiste")):
        assert db.rad_gratis_verbrauchen(gid, sid, uid, item, 1) is None, (gid, sid, uid, item)
        assert db.rad_gratis_stand(gid, sid, uid).get(item.lower()) is None
    assert db.rad_gratis_stand(G_A, "1000", U)["kiste"]["menge"] == 1


def test_gewinn_item_mit_verfall(welt, uhr):
    sicht = _einstellen(welt, [{"typ": "item", "name": "", "gewicht": 1, "item": "kiste", "menge": 1}], gratis_stunden=1, gratis_kauf_tage=3)
    erg = _drehen(welt, sicht, "gratis")
    assert erg["ok"] and "Gültig 3 Tag(e)." in erg["de"] and "Valid for 3 day(s)." in erg["en"]
    assert bot.db.rad_gratis_stand(G_A, "1000", U)["kiste"] == {"name": "Kiste", "menge": 1, "ablauf": float(MITTAG + 3 * TAG)}


# ── Gratis-Käufe: Kauf ──────────────────────────────────────────────────────
def _pruefen(welt, menge, uid=U, rollen=None, gutschein=None, item="Kiste"):
    return bot._kauf_pruefen(welt["a"], G_A, uid, item, menge, rollen if rollen is not None else set(), None, 100.0, 200.0, None, False, gutschein)


def _kaufen(welt, plan, uid=U):
    return asyncio.run(bot._kauf_ausfuehren(welt["a"], G_A, uid, "User", plan))


def test_kauf_zwei_stueck_mit_einem_gratis_kauf_zahlt_genau_eins(lager):
    bot.db.set_wallet(G_A, U, 100)
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, 0.0)
    plan = _pruefen(lager, 2)
    assert isinstance(plan, bot._KaufPlan) and (plan.gratis, plan.total, plan.preis.liste, plan.preis.gratis_wert) == (1, 100, 200, 100)
    zeile = bot._preis_zeile(plan.preis, lager["a"], False)
    assert "🎡 1× gratis (Glücksrad)" in zeile and "free (wheel of fortune)" in bot._preis_zeile(plan.preis, lager["a"], True)
    res = _kaufen(lager, plan)
    assert res.ok and bot.db.get_balance(G_A, U)[0] == 0 and bot.db.rad_gratis_stand(G_A, "1000", U) == {}
    assert lager["a"].shop.geliefert[0]["menge"] == 2
    # der nächste Kauf kostet wieder den vollen Preis
    bot.db.set_wallet(G_A, U, 100)
    plan2 = _pruefen(lager, 1)
    assert (plan2.gratis, plan2.total) == (0, 100) and bot._preis_zeile(plan2.preis, lager["a"], False) is None


def test_kauf_komplett_gratis_geht_mit_leerem_wallet_und_laesst_den_gutschein_in_ruhe(lager):
    bot.db.set_wallet(G_A, U, 0)
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 2, 0.0)
    bot.db.gutschein_speichern(G_A, "FEST50", "fixed", 50, True, 1, None, None, True)
    vorher = dict(bot.db.gutschein_holen(G_A, "FEST50"))
    plan = _pruefen(lager, 2, gutschein="FEST50")
    assert isinstance(plan, bot._KaufPlan) and (plan.gratis, plan.total, plan.preis.gutschein) == (2, 0, None)
    assert _kaufen(lager, plan).ok and bot.db.rad_gratis_stand(G_A, "1000", U) == {}
    assert dict(bot.db.gutschein_holen(G_A, "FEST50")) == vorher           # nicht eingelöst
    # bleibt ein Rest zu zahlen, gilt der Gutschein für den Rest
    bot.db.set_wallet(G_A, U, 100)
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, 0.0)
    plan = _pruefen(lager, 2, gutschein="FEST50")
    assert (plan.gratis, plan.total, plan.preis.gutschein, plan.preis.gutschein_abzug) == (1, 50, "FEST50", 50)


def test_kauf_rabatt_gilt_nur_fuer_den_rest(lager):
    bot.db.set_wallet(G_A, U, 1000)
    bot.db.rabatt_ersetzen(G_A, [(ROLLE_VIP, "percent", 50)])
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, 0.0)
    plan = _pruefen(lager, 3, rollen={ROLLE_VIP})
    assert (plan.preis.liste, plan.preis.gratis_wert, plan.preis.rabatt, plan.total) == (300, 100, 100, 100)
    ohne = bot._preis_berechnen(G_A, {ROLLE_VIP}, 300, None, False, U)      # ohne Gratis-Kauf unverändert
    assert (ohne.rabatt, ohne.total, ohne.gratis_menge) == (150, 150, 0)


def test_kauf_gratis_haelt_sich_an_hoechstmenge_und_rolle(lager):
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 9, 0.0)
    bot.db.rad_gratis_add(G_A, "1000", U, "Vip-Kiste", 1, 0.0)
    assert _pruefen(lager, 4).code == "menge"                              # max_amount_per_buy = 3
    assert _pruefen(lager, 1, item="Vip-Kiste").code == "rolle"            # ohne die Rolle auch gratis nicht
    assert _pruefen(lager, 3).gratis == 3


def test_kauf_scheitert_am_wallet_dann_kommt_der_gratis_kauf_zurueck(lager):
    bot.db.set_wallet(G_A, U, 100)
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, 0.0)
    plan = _pruefen(lager, 2)
    bot.db.set_wallet(G_A, U, 0)                                           # Geld ist zwischen Prüfen und Ausführen weg
    res = _kaufen(lager, plan)
    assert res.ok is False and res.code == "guthaben"
    assert bot.db.rad_gratis_stand(G_A, "1000", U)["kiste"]["menge"] == 1   # zurückgegeben
    assert lager["a"].shop.entfernt == [lager["a"].shop.geliefert[0]["namen"]] and bot.db.purchases_of_user("1000", G_A, U, 5) == []


def test_kauf_scheitert_an_der_lieferung_dann_bleibt_der_gratis_kauf(lager):
    bot.db.set_wallet(G_A, U, 0)
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, 0.0)
    plan = _pruefen(lager, 1)
    lager["a"].shop.ok = False
    res = _kaufen(lager, plan)
    assert res.ok is False and res.code == "lieferung" and bot.db.rad_gratis_stand(G_A, "1000", U)["kiste"]["menge"] == 1


def test_kauf_wettlauf_gratis_kauf_ist_weg_nichts_wird_abgebucht(lager):
    """Zwei Käufe planen mit demselben einen Gratis-Kauf: nur der erste bekommt ihn, der zweite bricht ab statt voll zu zahlen."""
    bot.db.set_wallet(G_A, U, 500)
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, 0.0)
    plan1, plan2 = _pruefen(lager, 1), _pruefen(lager, 1)
    assert plan1.total == plan2.total == 0
    assert _kaufen(lager, plan1).ok
    res = _kaufen(lager, plan2)
    assert res.ok is False and res.code == "gratis" and "nichts wurde abgebucht" in res.de and "nothing was charged" in res.en
    assert bot.db.get_balance(G_A, U)[0] == 500 and len(bot.db.purchases_of_user("1000", G_A, U, 5)) == 1
    assert lager["a"].shop.entfernt == [lager["a"].shop.geliefert[1]["namen"]]          # Area-Einträge des zweiten zurückgerollt


def test_kauf_gratis_nur_fuer_den_gewinner(lager):
    bot.db.set_wallet(G_A, ANDERER, 100)
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, 0.0)
    plan = _pruefen(lager, 1, uid=ANDERER)
    assert (plan.gratis, plan.total) == (0, 100)
    assert _kaufen(lager, plan, uid=ANDERER).ok and bot.db.rad_gratis_stand(G_A, "1000", U)["kiste"]["menge"] == 1


def test_api_shop_quote_und_kauf_zeigen_den_gratis_kauf(lager, uhr):
    bot.db.set_wallet(G_A, U, 100)
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, MITTAG + TAG)
    s = _session(gid=G_A, sid_="1000")
    items = {i["name"]: i for i in api("GET", "/api/me/shop", s)[1]["data"]["items"]}
    assert items["Kiste"]["gratis"] == {"menge": 1, "ablauf": float(MITTAG + TAG)} and "gratis" not in items["Vip-Kiste"]
    st, d = api("POST", "/api/me/quote", s, {"art": "shop", "item": "Kiste", "amount": 2})
    assert st == 200 and d["data"]["gratis_menge"] == 1 and "🎡 1× " in d["data"]["zeile"] and "100" in d["data"]["total_text"]
    st, d = api("POST", "/api/me/shop/buy", s, {"item": "Kiste", "amount": 2, "x": 100, "z": 200})
    assert st == 200 and "🎡 1× " in d["data"]["preis_zeile"] and bot.db.get_balance(G_A, U)[0] == 0
    assert "gratis" not in {i["name"]: i for i in api("GET", "/api/me/shop", s)[1]["data"]["items"]}["Kiste"]
    # ein anderer Spieler sieht davon nichts
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, 0.0)
    fremd = {i["name"]: i for i in api("GET", "/api/me/shop", _session(uid=ANDERER, gid=G_A, sid_="1000"))[1]["data"]["items"]}
    assert "gratis" not in fremd["Kiste"]
    assert api("POST", "/api/me/quote", _session(uid=ANDERER, gid=G_A, sid_="1000"), {"art": "shop", "item": "Kiste", "amount": 1})[1]["data"]["gratis_menge"] == 0


# ── Offene Gewinne: Gewinnen ────────────────────────────────────────────────
def test_gewinn_klassen_und_event_legen_offene_gewinne_an(lager):
    sicht = _einstellen(lager, [{"typ": "klassen", "name": "", "gewicht": 1, "klassen": ["Mosin9130", "Ammo_762x54"], "menge": 2}], gratis_stunden=1)
    erg = _drehen(lager, sicht, "gratis")
    assert erg["ok"] and "/claim wheel prize" in erg["de"] and "/claim wheel prize" in erg["en"]
    assert lager["a"].shop.geliefert == []                                  # beim Gewinn wird noch nichts geliefert
    sicht = _einstellen(lager, [{"typ": "event", "name": "Olga", "gewicht": 1, "event_xml": _event_xml()}], gratis_stunden=1)
    bot.db.rad_gratis_zuruecksetzen(G_A, U, 0.0)
    assert _drehen(lager, sicht, "gratis")["ok"]
    offen = bot.db.rad_gewinne_offen(G_A, "1000", U)
    assert [(r["typ"], r["name"]) for r in offen] == [("klassen", "2× Mosin9130 +1"), ("event", "Olga")]
    assert json.loads(offen[0]["daten"]) == {"klassen": ["Mosin9130", "Ammo_762x54"], "menge": 2}
    st, d = api("GET", "/api/me/rad", _session(gid=G_A, sid_="1000"))
    gewinne = d["data"]["gewinne"]
    assert [(g["typ"], g["name"], g["inhalt"]) for g in gewinne] == [
        ("klassen", "2× Mosin9130 +1", "2× Mosin9130 + Ammo_762x54"), ("event", "Olga", "Permanent spawn: CivilianSedan")]   # API-Sitzung: Englisch
    assert "<event" not in json.dumps(d) and set(gewinne[0]) == {"id", "typ", "name", "inhalt", "ts"}      # nie das rohe XML
    assert api("GET", "/api/me/rad", _session(uid=ANDERER, gid=G_A, sid_="1000"))[1]["data"]["gewinne"] == []


def test_gewinn_obergrenze_gibt_die_drehung_zurueck(lager):
    for _ in range(bot._RAD_MAX_OFFEN):
        _gewinn(lager)
    sicht = _einstellen(lager, [{"typ": "klassen", "name": "", "gewicht": 1, "klassen": ["Mosin9130"], "menge": 1}], kosten=10)
    bot.db.add_wallet(G_A, U, 10)
    erg = _drehen(lager, sicht, "bezahlt")
    assert erg["ok"] is False and erg["code"] == "auszahlung" and "offene Gewinne" in erg["de"] and bot.db.get_balance(G_A, U)[0] == 10
    assert bot.db.rad_gewinne_offen_anzahl(G_A, "1000", U) == bot._RAD_MAX_OFFEN
    bot.db.add_wallet(G_A, ANDERER, 10)
    assert _drehen(lager, sicht, "bezahlt", uid=ANDERER)["ok"]              # die Grenze gilt je Spieler


# ── Offene Gewinne: Einlösen ────────────────────────────────────────────────
def test_einloesen_klassen_liefert_am_gewaehlten_ort_und_legt_bestellung_an(lager):
    g = _gewinn(lager)
    erg = _einloesen(lager, g, x=4640.0, z=10350.0)
    assert erg["ok"] and (erg["x"], erg["z"], erg["typ"], erg["name"]) == (4640.0, 10350.0, "klassen", "Mosin")
    assert "eingelöst" in erg["de"] and "claimed" in erg["en"]
    lief = lager["a"].shop.geliefert
    assert len(lief) == 1 and (lief[0]["klassen"], lief[0]["menge"], lief[0]["x"], lief[0]["z"]) == (["Mosin9130", "Ammo_762x54"], 2, 4640.0, 10350.0)
    kauf = bot.db.purchases_of_user("1000", G_A, U, 5)
    assert len(kauf) == 1 and (kauf[0]["item_name"], kauf[0]["amount"], kauf[0]["total_price"], kauf[0]["status"]) == ("🎡 Mosin", 2, 0, "pending")
    assert _offen() == [] and ("shop_log" in [f[1] for f in lager["feeds"]])
    again = _einloesen(lager, g)                                            # zweites Einlösen: gibt es nicht mehr
    assert again["ok"] is False and again["code"] == "gewinn" and len(lager["a"].shop.geliefert) == 1


def test_einloesen_event_schreibt_eintrag_und_position_und_events_remove_kennt_ihn(lager):
    g = _gewinn(lager, "event", name="Olga 24")
    erg = _einloesen(lager, g, x=6500.5, z=2500.0)
    assert erg["ok"], erg
    ftp = lager["a"].ftp
    ev, sp = ftp.files[f"{TR.MISSION}/db/events.xml"], ftp.files[f"{TR.MISSION}/cfgeventspawns.xml"]
    inst = bot._event_instances(lager["a"])
    assert len(inst) == 1 and inst[0]["template"] == "🎡 Olga 24" and inst[0]["created_by"] == U and (inst[0]["x"], inst[0]["z"]) == (6500.5, 2500.0)
    name = inst[0]["event_name"]
    assert bot.re.fullmatch(r"VehicleCivilianSedan_W[0-9a-f]{8}", name) and bot._EVENT_NAME_RE.match(name)
    assert f'<event name="{name}">' in ev and 'type="CivilianSedan"' in ev
    block = bot._tool_finde_benannten_block(sp, "event", name)
    assert block is not None and 'x="6500.5"' in block["block"] and 'z="2500"' in block["block"]
    assert bot.ET.fromstring(ev) is not None and bot.ET.fromstring(sp) is not None          # beide Dateien bleiben gültiges XML
    assert ev.count(TR._EVENTS_XML.strip().splitlines()[1].strip()) >= 1                   # der alte Inhalt steht noch da
    # genau so räumt /events remove auf
    ev2, weg_ev = bot._tool_delete_event(ev, name)
    sp2, weg_sp = bot._tool_delete_eventspawns(sp, name)
    assert weg_ev and weg_sp and name not in ev2 and name not in sp2
    assert _offen() == [] and bot.db.purchases_of_user("1000", G_A, U, 5) == []
    # zwei Gewinne desselben Segments bekommen verschiedene Namen
    g2 = _gewinn(lager, "event", name="Olga 24")
    assert _einloesen(lager, g2)["ok"] and len({i["event_name"] for i in bot._event_instances(lager["a"])}) == 2


class _FtpKaputt(TR._FakeFTP):
    def __init__(self, kaputt):
        super().__init__()
        self.kaputt = kaputt

    def write_file(self, pfad, inhalt):
        if pfad.endswith(self.kaputt) and not getattr(self, "repariert", False):
            if self.kaputt == "cfgeventspawns.xml":
                self.repariert_nach = True
            return False
        return super().write_file(pfad, inhalt)


@pytest.mark.parametrize("kaputt", ["db/events.xml", "cfgeventspawns.xml"])
def test_einloesen_event_schreibfehler_laesst_den_gewinn_offen_und_rollt_zurueck(lager, kaputt):
    lager["a"].ftp = _FtpKaputt(kaputt)
    g = _gewinn(lager, "event")
    erg = _einloesen(lager, g)
    assert erg["ok"] is False and erg["code"] == "lieferung" and "bleibt offen" in erg["de"] and "stays unclaimed" in erg["en"]
    assert _offen() == [g] and bot._event_instances(lager["a"]) == []
    assert "_W" not in lager["a"].ftp.files[f"{TR.MISSION}/db/events.xml"]            # kein Event ohne Position bleibt zurück
    assert lager["a"].ftp.files[f"{TR.MISSION}/cfgeventspawns.xml"] == TR._EVENTSPAWNS_XML
    lager["a"].ftp.repariert = True
    assert _einloesen(lager, g)["ok"] and _offen() == []                              # danach klappt es


def test_einloesen_klassen_lieferfehler_und_ausnahme_lassen_den_gewinn_offen(lager, monkeypatch):
    g = _gewinn(lager)
    lager["a"].shop.ok = False
    erg = _einloesen(lager, g)
    assert erg["ok"] is False and erg["code"] == "lieferung" and _offen() == [g] and bot.db.purchases_of_user("1000", G_A, U, 5) == []

    async def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(bot, "_rad_klassen_liefern", boom)
    assert _einloesen(lager, g)["code"] == "lieferung" and _offen() == [g]


def test_einloesen_ohne_ftp_shop_oder_mission(lager):
    g_k, g_e = _gewinn(lager), _gewinn(lager, "event")
    a = lager["a"]
    ftp, shop = a.ftp, a.shop
    a.ftp = None
    assert _einloesen(lager, g_k)["code"] == "kein_ftp" and _einloesen(lager, g_e)["code"] == "kein_ftp"
    a.ftp, a.shop = ftp, None
    assert _einloesen(lager, g_k)["code"] == "shop_start"
    a.shop = shop
    a.data["ftp_mission_dir"] = ""
    if not bot._mission_dir_of(a):
        assert _einloesen(lager, g_e)["code"] == "shop_start"
    assert _offen() == [g_k, g_e]


@pytest.mark.parametrize("gewinn_id", [0, -1, True, "1", 1.5, None, 999999])
def test_einloesen_ungueltige_kennung(lager, gewinn_id):
    _gewinn(lager)
    erg = _einloesen(lager, gewinn_id)
    assert erg["ok"] is False and erg["code"] == "gewinn" and lager["a"].shop.geliefert == [] and len(_offen()) == 1


def test_einloesen_fremder_spieler_andere_guild_anderer_server(lager):
    g = _gewinn(lager)
    lager["b"].shop, lager["b"].ftp = _ShopDoppel(), TR._FakeFTP()
    assert _einloesen(lager, g, uid=ANDERER)["code"] == "gewinn"                       # anderer Spieler
    assert _einloesen(lager, g, gid=G_B, conn="b")["code"] == "gewinn"                 # andere Guild + Server
    assert _einloesen(lager, g, gid=G_B)["code"] == "gewinn"                           # andere Guild
    assert _einloesen(lager, g, conn="b")["code"] == "gewinn"                          # anderer Server
    assert lager["a"].shop.geliefert == [] and lager["b"].shop.geliefert == [] and _offen() == [g]
    assert _einloesen(lager, g)["ok"]


@pytest.mark.parametrize("ort,x,z", [("last", 1.0, 2.0), (None, None, None), (None, 5.0, None), (None, -1.0, 5.0), (None, 5.0, 20000.5),
                                     (None, float("nan"), 5.0), (None, float("inf"), 5.0), ("s:999999", None, None)])
def test_einloesen_ort_fehler_laesst_den_gewinn_offen(lager, ort, x, z):
    g = _gewinn(lager)
    erg = _einloesen(lager, g, ort=ort, x=x, z=z)
    assert erg["ok"] is False and erg["code"] == "ort" and _offen() == [g] and lager["a"].shop.geliefert == []


def test_einloesen_mit_gespeichertem_ort(lager):
    bot.db.ort_speichern(G_A, "1000", U, "Basis", 4010.0, 11080.0)
    ort_id = bot.db.orte_liste(G_A, "1000", U, "gespeichert")[0]["id"]
    erg = _einloesen(lager, _gewinn(lager), ort=f"s:{ort_id}", x=None, z=None)
    assert erg["ok"] and (erg["x"], erg["z"]) == (4010.0, 11080.0) and "Basis" in str(erg["ort_label"])


def test_einloesen_gleichzeitig_liefert_nur_einmal(lager):
    g = _gewinn(lager)

    async def beide():
        return await asyncio.gather(*(bot._rad_gewinn_einloesen(lager["a"], G_A, U, "User", g, None, 100.0, 200.0, False) for _ in range(3)))
    erg = asyncio.run(beide())
    assert sorted(e["ok"] for e in erg) == [False, False, True] and len(lager["a"].shop.geliefert) == 1
    assert len(bot.db.purchases_of_user("1000", G_A, U, 5)) == 1


def test_einloesen_beschaedigter_gewinn(lager):
    kaputt = [_gewinn(lager, daten={"klassen": ["böse klasse!"], "menge": 1}), _gewinn(lager, daten={"klassen": ["Mosin9130"], "menge": 999}),
              _gewinn(lager, "event", daten={"event_xml": "<event name='x'><!-- c --></event>"}),
              _gewinn(lager, "event", daten={"event_xml": "<!DOCTYPE x><event/>"})]
    for g in kaputt:
        assert _einloesen(lager, g)["code"] == "lieferung"
    assert _offen() == kaputt and lager["a"].shop.geliefert == [] and lager["a"].ftp.geschrieben == []


def test_db_gewinn_beanspruchen_und_freigeben(lager):
    g = _gewinn(lager)
    assert bot.db.rad_gewinn_beanspruchen(g, G_A, "1000", ANDERER, 1, 2) is False
    assert bot.db.rad_gewinn_beanspruchen(g, G_A, "1000", U, 1, 2) is True
    assert bot.db.rad_gewinn_beanspruchen(g, G_A, "1000", U, 1, 2) is False and _offen() == []
    bot.db.rad_gewinn_freigeben(g, G_A, "1000", ANDERER)                    # ein Fremder kann nichts freigeben
    assert _offen() == []
    bot.db.rad_gewinn_freigeben(g, G_A, "1000", U)
    assert _offen() == [g]


# ── API ─────────────────────────────────────────────────────────────────────
def test_api_einloesen(lager):
    g = _gewinn(lager)
    assert api("POST", "/api/me/rad/einloesen", None, {"id": g, "x": 1, "z": 2})[0] == 401
    assert api("POST", "/api/me/rad/einloesen", _session(), {"id": g, "x": 1, "z": 2})[0] == 409          # kein Server gewählt
    s = _session(gid=G_A, sid_="1000")
    for daten, status, code in (({"id": g}, 400, "ort"), ({"id": g, "x": "abc", "z": 2}, 400, None), ({"id": g, "x": 1, "z": 2, "ort": "last"}, 400, "ort"),
                                ({"id": 999999, "x": 1, "z": 2}, 404, "gewinn"), ({"id": str(g), "x": 1, "z": 2}, 404, "gewinn"), ({"x": 1, "z": 2}, 404, "gewinn")):
        st, d = api("POST", "/api/me/rad/einloesen", s, daten)
        assert st == status and (code is None or d["code"] == code), (daten, st, d)
    assert api("POST", "/api/me/rad/einloesen", _session(uid=ANDERER, gid=G_A, sid_="1000"), {"id": g, "x": 1, "z": 2})[0] == 404
    assert _offen() == [g] and lager["a"].shop.geliefert == []
    st, d = api("POST", "/api/me/rad/einloesen", s, {"id": g, "x": 4640, "z": 10350})
    assert st == 200 and d["data"]["name"] == "Mosin" and d["data"]["gewinne"] == [] and d["data"]["x"] == 4640.0
    assert "claimed" in d["data"]["text"] and api("POST", "/api/me/rad/einloesen", s, {"id": g, "x": 1, "z": 2})[0] == 404


def test_api_einloesen_lieferfehler_502_und_ohne_ftp_503(lager):
    g = _gewinn(lager)
    s = _session(gid=G_A, sid_="1000")
    lager["a"].shop.ok = False
    st, d = api("POST", "/api/me/rad/einloesen", s, {"id": g, "x": 1, "z": 2})
    assert st == 502 and d["code"] == "lieferung"
    lager["a"].ftp = None
    assert api("POST", "/api/me/rad/einloesen", s, {"id": g, "x": 1, "z": 2})[0] == 503 and _offen() == [g]


def _mgmt(welt, methode, pfad, daten=None, **kw):
    return api(methode, "/api/discord-management/gluecksrad" + pfad, _admin(welt, **kw), daten)


def test_mgmt_klassen_suche_und_event_vorlage(welt):
    welt["a"].data["shop_autofill_classnames"] = [{"classname": "Mosin9130", "name": "Mosin 91/30"}, {"classname": "Mosin_Bayonet", "name": "Bajonett"},
                                                  {"classname": "böse klasse", "name": "Mosin kaputt"}]
    welt["b"].data["shop_autofill_classnames"] = [{"classname": "MosinNurB", "name": "nur auf B"}]
    st, d = _mgmt(welt, "GET", "/klassen?q=mosin")
    namen = [k["classname"] for k in d["data"]["klassen"]]
    assert st == 200 and namen[:2] == ["Mosin9130", "Mosin_Bayonet"] and "MosinNurB" not in namen and "böse klasse" not in namen
    assert len(namen) <= 20 and _mgmt(welt, "GET", "/klassen?q=m")[1]["data"]["klassen"] == []
    st, d = _mgmt(welt, "POST", "/event-vorlage", {"classname": "CivilianSedan"})
    assert st == 200 and d["data"]["klasse"] == "CivilianSedan"
    assert bot._rad_preis_pruefen({"typ": "event", "gewicht": 1, "event_xml": d["data"]["event_xml"]}, id_erzeugen=True)["typ"] == "event"
    for schlecht in ("", "böse klasse", "a" * 81, None, 5, "x\"><script>"):
        assert _mgmt(welt, "POST", "/event-vorlage", {"classname": schlecht})[0] == 400, schlecht


def test_mgmt_neue_endpunkte_ohne_login_und_als_spieler_gesperrt(welt):
    s = _session(gid=G_A, sid_="1000")
    for methode, pfad in (("GET", "/klassen?q=mosin"), ("POST", "/event-vorlage")):
        assert api(methode, "/api/discord-management/gluecksrad" + pfad, None, {"classname": "CivilianSedan"})[0] in (401, 403)
        assert api(methode, "/api/discord-management/gluecksrad" + pfad, s, {"classname": "CivilianSedan"})[0] in (401, 403)


def test_mgmt_speichern_und_laden_der_neuen_felder(welt):
    segmente = [{"typ": "klassen", "name": "", "gewicht": 4, "farbe": 2, "klassen": ["BakedBeansCan"], "menge": 1},
                {"typ": "geld", "name": "Jackpot", "gewicht": 1, "farbe": 11, "betrag": 250},
                {"typ": "event", "gewicht": 1, "event_xml": _event_xml()},
                {"typ": "item", "name": "", "gewicht": 1, "item": "Kiste", "menge": 2}]
    daten = {"enabled": True, "name": "  Nifty's   Civilian Wheel ", "kosten": 10, "max_pro_tag": 2, "gratis_stunden": 0, "spielzeit_stunden": 0,
             "max_gespeichert": 5, "gratis_kauf_tage": 14, "preise": segmente}
    st, d = _mgmt(welt, "POST", "", daten)
    assert st == 200, d
    e = _mgmt(welt, "GET", "")[1]["data"]
    assert (e["name"], e["kosten"], e["max_pro_tag"], e["gratis_kauf_tage"]) == ("Nifty's Civilian Wheel", 10, 2, 14)
    assert [(p["typ"], p["name"], p["farbe"]) for p in e["preise"]] == [("klassen", "", 2), ("geld", "Jackpot", 11), ("event", "", 2), ("item", "", 3)]
    assert e["limits"]["preise"] == 12 and e["limits"]["min_preise"] == 2 and e["limits"]["farben"] == 12
    # Server B bleibt unberührt
    assert bot._rad_einstellungen(bot.guild_sicht(welt["b"], G_B))["preise"] == []
    for feld, wert in (("max_pro_tag", 101), ("max_pro_tag", -1), ("max_pro_tag", True), ("gratis_kauf_tage", 366), ("gratis_kauf_tage", "7"),
                       ("name", "x" * 41), ("name", 5), ("name", "a\x00b")):
        assert _mgmt(welt, "POST", "", dict(daten, **{feld: wert}))[0] == 400, (feld, wert)
    assert _mgmt(welt, "POST", "", dict(daten, preise=segmente[:1]))[0] == 400                    # aktiv mit nur 1 Segment
    assert _mgmt(welt, "POST", "", dict(daten, enabled=False, preise=segmente[:1]))[0] == 200     # abgeschaltet darf weniger haben
    assert _mgmt(welt, "POST", "", dict(daten, preise=[_preis() for _ in range(13)]))[0] == 400   # mehr als 12


# ── Slash: /claim wheel prize und /wheel info ───────────────────────────────
def test_slash_claim_befehl_ist_registriert():
    assert bot.claim_group.name == "claim" and bot.claim_wheel_group.name == "wheel" and bot.claim_wheel_group.parent is bot.claim_group
    assert bot.claim_wheel_prize.name == "prize" and bot.claim_wheel_prize.parent is bot.claim_wheel_group
    assert [p.name for p in bot.claim_wheel_prize.parameters] == ["prize", "ort", "x", "z", "server"]


def test_slash_claim_loest_ein_in_beiden_sprachen(slash):
    slash["a"].shop, slash["a"].ftp = _ShopDoppel(), TR._FakeFTP()
    slash["a"].data["ftp_mission_dir"] = TR.MISSION
    g1, g2 = _gewinn(slash), _gewinn(slash, "event", name="Olga")
    text = _lauf(bot.claim_wheel_prize, _Inter(slash, locale=discord.Locale.german), prize=str(g1), x=4640.0, z=10350.0)
    assert "Gewinn eingelöst" in text and "Mosin" in text and "4640.0 / 10350.0" in text and "Prize claimed" not in text
    text = _lauf(bot.claim_wheel_prize, _Inter(slash, locale=discord.Locale.american_english), prize=f"#{g2}", x=1.0, z=2.0)
    assert "Prize claimed" in text and "claimed" in text and "eingelöst" not in text
    assert _offen() == [] and len(slash["a"].shop.geliefert) == 1 and len(bot._event_instances(slash["a"])) == 1


def test_slash_claim_fehler(slash):
    slash["a"].shop, slash["a"].ftp = _ShopDoppel(), TR._FakeFTP()
    g = _gewinn(slash)
    for prize in ("abc", "", "1 OR 1=1", "-5"):
        assert "aus der Liste" in _lauf(bot.claim_wheel_prize, _Inter(slash, locale=discord.Locale.german), prize=prize, x=1.0, z=2.0)
    assert "does not exist" in _lauf(bot.claim_wheel_prize, _Inter(slash, uid=ANDERER, locale=discord.Locale.american_english), prize=str(g), x=1.0, z=2.0)
    assert "nicht beides" in _lauf(bot.claim_wheel_prize, _Inter(slash, locale=discord.Locale.german), prize=str(g), ort="last", x=1.0, z=2.0)
    assert "Ort wählen" in _lauf(bot.claim_wheel_prize, _Inter(slash, locale=discord.Locale.german), prize=str(g))
    assert _offen() == [g] and slash["a"].shop.geliefert == []


def test_slash_claim_autocomplete_zeigt_nur_eigene_offene_gewinne(slash):
    g1, g2 = _gewinn(slash), _gewinn(slash, "event", name="Olga")
    _gewinn(slash, uid=ANDERER, name="Fremd")
    _gewinn(slash, gid=G_B, sid="2000", name="AndereGuild")
    wahl = asyncio.run(bot._rad_gewinn_autocomplete(_Inter(slash, locale=discord.Locale.german), ""))
    assert [(c.value, c.name) for c in wahl] == [(str(g1), f"#{g1} Mosin – 2× Mosin9130 + Ammo_762x54"), (str(g2), f"#{g2} Olga – Dauer-Spawn: CivilianSedan")]
    assert [c.value for c in asyncio.run(bot._rad_gewinn_autocomplete(_Inter(slash, locale=discord.Locale.american_english), "olga"))] == [str(g2)]
    assert "Permanent spawn" in asyncio.run(bot._rad_gewinn_autocomplete(_Inter(slash, locale=discord.Locale.american_english), "olga"))[0].name
    assert [c.name for c in asyncio.run(bot._rad_gewinn_autocomplete(_Inter(slash, uid=ANDERER), ""))][0].endswith("Fremd – 2× Mosin9130 + Ammo_762x54")


def test_slash_info_zeigt_tageszaehler_offene_gewinne_und_gratis_kaeufe(slash, uhr):
    _einstellen(slash, [_preis()], max_pro_tag=4, name="Zivilisten-Rad", gratis_stunden=1)
    _gewinn(slash)
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 2, MITTAG + TAG)
    text = _lauf(bot.wheel_info, _Inter(slash, locale=discord.Locale.german))
    assert "Zivilisten-Rad" in text and "0/4" in text and "Offene Gewinne" in text and "Mosin" in text and "2× Kiste" in text
    text = _lauf(bot.wheel_info, _Inter(slash, uid=ANDERER, locale=discord.Locale.american_english))
    assert "Unclaimed" not in text and "Kiste" not in text and "Spins today" in text


# ── Befunde der unabhängigen Prüfung ────────────────────────────────────────
def test_einloesen_klassen_bestellung_nicht_speicherbar_rollt_die_lieferung_zurueck(lager, monkeypatch):
    """Früher: Area-Einträge blieben stehen und der Gewinn war wieder offen - zweimal einlösbar."""
    g = _gewinn(lager)

    def kaputt(*a, **k):
        raise RuntimeError("db kaputt")
    monkeypatch.setattr(bot.db, "create_purchase", kaputt)
    erg = _einloesen(lager, g)
    shop = lager["a"].shop
    assert erg["ok"] is False and erg["code"] == "lieferung" and _offen() == [g]
    assert shop.entfernt == [shop.geliefert[0]["namen"]]                    # nichts bleibt in der Datei zurück
    monkeypatch.undo()


def test_einloesen_event_buchfuehrung_kaputt_bleibt_trotzdem_eingeloest(lager, monkeypatch):
    """Der Eintrag steht schon auf dem Server: der Gewinn darf dann nicht wieder offen sein (sonst zweiter Dauer-Spawn)."""
    g = _gewinn(lager, "event")
    echt = bot._conn_store

    def kaputt(conn, key, wert, *a, **k):
        if key == "event_instances":
            raise RuntimeError("platte voll")
        return echt(conn, key, wert, *a, **k)
    monkeypatch.setattr(bot, "_conn_store", kaputt)
    erg = _einloesen(lager, g)
    assert erg["ok"] is True and _offen() == []
    assert lager["a"].ftp.files[f"{TR.MISSION}/db/events.xml"].count("VehicleCivilianSedan_W") == 1
    assert _einloesen(lager, g)["code"] == "gewinn"


@pytest.mark.parametrize("xml", [
    '<event name="VehicleCivilianSedan" note="x name=\'y\'"><children><child type="CivilianSedan"/></children></event>',
    '<event name="Vehicle Sedan"><children><child type="CivilianSedan"/></children></event>',
    '<event name="VehicleX"><child type="CivilianSedan"/></event>',
    '<event name="VehicleX"><children><child/></children></event>'])
def test_event_name_muss_das_echte_name_attribut_sein(xml):
    with pytest.raises(ValueError):
        bot._rad_preis_pruefen({"typ": "event", "gewicht": 1, "event_xml": xml}, id_erzeugen=True)


@pytest.mark.parametrize("wert", [float("inf"), float("-inf"), float("nan"), 1e999])
def test_zahl_unendlich_ist_ein_eingabefehler_kein_absturz(wert):
    with pytest.raises(ValueError):
        bot._rad_zahl(wert, 0, 100)
    with pytest.raises(ValueError):
        bot._wortfilter_zahl(wert, 0, 100)


def test_mgmt_unendlich_im_json_ergibt_400(welt):
    async def lauf():
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(bot.build_app())) as c:
            roh = '{"enabled": false, "max_pro_tag": Infinity, "preise": []}'
            r = await c.post("/api/discord-management/gluecksrad", data=roh, headers={"Content-Type": "application/json"},
                             cookies={bot._SESS_COOKIE: _admin(welt)})
            return r.status
    bot._DASH_RATE_LIMIT_LAST.clear()
    assert asyncio.run(lauf()) == 400


def test_gratis_kauf_wird_nicht_verschenkt_wenn_der_kauf_ohnehin_nichts_kostet(lager):
    bot.db.set_wallet(G_A, U, 0)
    bot.db.rabatt_ersetzen(G_A, [(ROLLE_VIP, "percent", 100)])
    bot.db.rad_gratis_add(G_A, "1000", U, "Kiste", 1, 0.0)
    plan = _pruefen(lager, 1, rollen={ROLLE_VIP})
    assert (plan.gratis, plan.total) == (0, 0) and _kaufen(lager, plan).ok
    assert bot.db.rad_gratis_stand(G_A, "1000", U)["kiste"]["menge"] == 1    # der Gratis-Kauf ist noch da
    lager["a"]._catalog.items.append({"name": "Umsonst", "classname": "Rag", "price": 0, "enabled": True, "max_amount_per_buy": 5})
    bot.db.rad_gratis_add(G_A, "1000", U, "Umsonst", 2, 0.0)
    plan = _pruefen(lager, 2, item="Umsonst")
    assert plan.gratis == 0 and _kaufen(lager, plan).ok and bot.db.rad_gratis_stand(G_A, "1000", U)["umsonst"]["menge"] == 2


def test_slash_claim_ortsfehler_ohne_doppeltes_kreuz_und_lieferfehler_mit_satzzeichen(slash):
    slash["a"].shop, slash["a"].ftp = _ShopDoppel(), TR._FakeFTP()
    g = _gewinn(slash)
    text = _lauf(bot.claim_wheel_prize, _Inter(slash, locale=discord.Locale.german), prize=str(g), ort="s:999999")
    assert text.count("❌") == 1 and "Ort nicht gefunden" in text
    slash["a"].shop.ok = False
    text = _lauf(bot.claim_wheel_prize, _Inter(slash, locale=discord.Locale.american_english), prize=str(g), x=1.0, z=2.0)
    assert "The delivery via FTP failed. Your prize stays unclaimed." in text


def test_slash_claim_mit_server_angabe(slash, monkeypatch):
    """Im Spielerbereich auf dem zweiten Server der Guild gewonnen: /claim findet den Gewinn mit `server`."""
    monkeypatch.setattr(bot, "_conns_of", lambda inter: [slash["a"], slash["b"]])
    bot.connections.assign_guild(slash["b"].service_id, G_A)
    slash["b"].shop, slash["b"].ftp = _ShopDoppel(), TR._FakeFTP()
    g = _gewinn(slash, sid="2000")
    text = _lauf(bot.claim_wheel_prize, _Inter(slash, locale=discord.Locale.german), prize=str(g), x=1.0, z=2.0)
    assert "gibt es nicht" in text and "mehrere Server" in text and slash["b"].shop.geliefert == []
    text = _lauf(bot.claim_wheel_prize, _Inter(slash, locale=discord.Locale.german), prize=str(g), x=1.0, z=2.0, server="2000")
    assert "Gewinn eingelöst" in text and len(slash["b"].shop.geliefert) == 1 and slash["a"].shop.aufrufe == []
    assert "Kein Server" in _lauf(bot.claim_wheel_prize, _Inter(slash, locale=discord.Locale.german), prize=str(g), x=1.0, z=2.0, server="gibtsnicht")
