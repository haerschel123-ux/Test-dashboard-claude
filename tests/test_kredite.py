"""Kredite (experimentell): Einstellungen, Vertrag mit Bestaetigung, Auszahlung, Rueckzahlung, Belohnung und die Strafen
(Pfaendung, Nitrado-Bann, Explosion am Pfand-Ort) samt Schnappschuss der Bedingungen, Rechten und Mandanten-Trennung.

Bann und Explosion laufen gegen Testdoubles (kein Nitrado, kein FTP, kein Discord).

    python3 -m pytest tests/test_kredite.py -q
"""
import asyncio
import json
import os
import sys
import time
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_spieler_dashboard import G_A, G_B, U, ANDERER, _session, api, welt  # noqa: F401 - Fixtures
from test_airstrike import servers, FTP  # noqa: F401 - Fixtures (zwei Server 1000/2000, FTP-Double)

TAG = 86400


@pytest.fixture(autouse=True)
def _frisch(monkeypatch):
    monkeypatch.setattr(bot, "_audit_log", bot.deque(maxlen=100))
    bot._KREDIT_EXPLOSION_VERSUCHE.clear()
    bot._KREDIT_LOCKS.clear()


@pytest.fixture
def db(tmp_path):
    return bot.EconomyDB(str(tmp_path / "k.db"))


def _einstellen(welt, conn="a", gid=G_A, **kw):
    """Speichert Einstellungen (wie das Dashboard) und setzt das Wallet von U auf 0 (die Datenbank vergibt ein Startguthaben)."""
    daten = {"enabled": True, "max_betrag": 10000, "zins_prozent": 10, "laufzeit_tage": 7, "gnade_tage": 1, "max_offen": 1,
             "belohnung_prozent": 0, "verzug_prozent": 5, "strafen": {"pfaendung": False, "nitrado_ban": False, "explosion": False},
             "explosion_klasse": ""}
    strafen = kw.pop("strafen", {})
    daten.update(kw)
    daten["strafen"] = dict(daten["strafen"], **strafen)
    bot.db.set_wallet(gid, U, 0)
    bot.db.deposit(gid, U, 0)
    sicht = bot.guild_sicht(welt[conn], gid)
    bot._conn_store(sicht, "kredite", daten)
    return sicht


def _bank_leeren(gid=G_A, uid=U):
    _w, b = bot.db.get_balance(gid, uid)
    if b:
        bot.db.withdraw(gid, uid, b)
        bot.db.set_wallet(gid, uid, 0)


class _Fake:
    def __init__(self, d):
        self.d = d

    def get(self, key, default=None):
        return self.d.get(key, default)


def _annehmen(welt, sicht, betrag=1000, ort=None, h=None, uid=U, gid=G_A, conn="a"):
    c = welt[conn]
    if h is None:
        v, f = bot._kredit_vertrag_pruefen(c, sicht, gid, uid, betrag, ort)
        assert f is None, f
        h = v["hash"]
    return asyncio.run(bot._kredit_annehmen(c, sicht, gid, uid, betrag, ort, h))


def _zahlen(welt, kid, betrag, uid=U, gid=G_A, conn="a"):
    return asyncio.run(bot._kredit_zahlen(welt[conn], gid, uid, kid, betrag))


# ── Einstellungen ───────────────────────────────────────────────────────────
def test_standard_ist_aus_ohne_strafen():
    e = bot._kredit_einstellungen(_Fake({}))
    assert e["enabled"] is False and e["strafen"] == {"pfaendung": False, "nitrado_ban": False, "explosion": False}
    assert (e["max_betrag"], e["zins_prozent"], e["laufzeit_tage"], e["gnade_tage"], e["max_offen"], e["belohnung_prozent"], e["verzug_prozent"]) == (10000, 10, 7, 1, 1, 0, 5)


@pytest.mark.parametrize("roh", [None, "x", 5, [], {"enabled": 1}, {"enabled": "true"}, {"enabled": "yes"}])
def test_nur_echtes_true_schaltet_ein(roh):
    assert bot._kredit_einstellungen(_Fake({"kredite": roh}))["enabled"] is False


def test_strafen_nur_bei_echtem_true():
    e = bot._kredit_einstellungen(_Fake({"kredite": {"strafen": {"pfaendung": 1, "nitrado_ban": "true", "explosion": True}, "explosion_klasse": ""}}))
    assert e["strafen"] == {"pfaendung": False, "nitrado_ban": False, "explosion": False}          # Explosion ohne Klasse ist aus
    e = bot._kredit_einstellungen(_Fake({"kredite": {"strafen": {"explosion": True}, "explosion_klasse": "ExplosionTest"}}))
    assert e["strafen"]["explosion"] is True and e["explosion_klasse"] == "ExplosionTest"
    assert bot._kredit_einstellungen(_Fake({"kredite": {"strafen": {"explosion": True}, "explosion_klasse": "a b"}}))["strafen"]["explosion"] is False


@pytest.mark.parametrize("key,wert,erwartet", [("max_betrag", 0, 10000), ("max_betrag", 2_000_000_000, 10000), ("zins_prozent", 101, 10), ("zins_prozent", -1, 10),
                                              ("laufzeit_tage", 0, 7), ("laufzeit_tage", 91, 7), ("gnade_tage", 31, 1), ("max_offen", 0, 1), ("max_offen", 6, 1),
                                              ("belohnung_prozent", 101, 0), ("verzug_prozent", 51, 5), ("zins_prozent", "5", 10), ("zins_prozent", True, 10),
                                              ("zins_prozent", 5.5, 10)])
def test_ungueltige_zahlen_fallen_auf_die_vorgabe(key, wert, erwartet):
    assert bot._kredit_einstellungen(_Fake({"kredite": {key: wert}}))[key] == erwartet


# ── Vertrag ─────────────────────────────────────────────────────────────────
def _e(**kw):
    e = bot._kredit_einstellungen(_Fake({"kredite": dict({"enabled": True}, **kw)}))
    return e


@pytest.mark.parametrize("betrag,prozent,zins", [(1000, 10, 100), (1001, 10, 101), (1, 10, 1), (1000, 0, 0), (999, 33, 330), (10, 100, 10)])
def test_zinsen_werden_aufgerundet(betrag, prozent, zins):
    assert bot._kredit_vertrag_bauen(_e(zins_prozent=prozent), betrag, None)["zins"] == zins


def test_vertrag_summen_belohnung_und_verzug():
    v = bot._kredit_vertrag_bauen(_e(zins_prozent=10, belohnung_prozent=50, verzug_prozent=5, strafen={"pfaendung": True}), 1000, None)
    assert (v["betrag"], v["zins"], v["gesamt"], v["belohnung"], v["verzug"]) == (1000, 100, 1100, 50, 50)
    v = bot._kredit_vertrag_bauen(_e(verzug_prozent=5), 1000, None)
    assert v["verzug"] == 0                                          # Verzugsgebuehr gibt es nur mit der Strafe Pfaendung


def test_vertrag_hash_haengt_von_allem_ab():
    h = bot._kredit_vertrag_bauen(_e(), 1000, None)["hash"]
    assert h == bot._kredit_vertrag_bauen(_e(), 1000, None)["hash"]
    for andere in (_e(zins_prozent=11), _e(laufzeit_tage=8), _e(gnade_tage=2), _e(belohnung_prozent=1), _e(strafen={"pfaendung": True}),
                   _e(strafen={"nitrado_ban": True})):
        assert bot._kredit_vertrag_bauen(andere, 1000, None)["hash"] != h
    assert bot._kredit_vertrag_bauen(_e(), 1001, None)["hash"] != h
    e = _e(strafen={"explosion": True}, explosion_klasse="ExplosionTest")
    o1, o2 = {"name": "Basis", "x": 1.0, "z": 2.0}, {"name": "Basis", "x": 1.0, "z": 3.0}
    assert bot._kredit_vertrag_bauen(e, 1000, o1)["hash"] != bot._kredit_vertrag_bauen(e, 1000, o2)["hash"]


def test_vertrag_pruefen_ablehnungen(welt):
    sicht = _einstellen(welt, enabled=False)
    assert bot._kredit_vertrag_pruefen(welt["a"], sicht, G_A, U, 100, None)[1]["code"] == "aus"
    sicht = _einstellen(welt, max_betrag=500)
    for betrag in (0, -5, 501, 5.0, "100", None, True, [1]):
        assert bot._kredit_vertrag_pruefen(welt["a"], sicht, G_A, U, betrag, None)[1]["code"] == "betrag", betrag
    assert bot._kredit_vertrag_pruefen(welt["a"], sicht, G_A, U, 500, None)[1] is None


def test_vertrag_verlangt_verknuepfung_nur_bei_bann_oder_explosion(welt):
    sicht = _einstellen(welt, strafen={"pfaendung": True})
    assert bot._kredit_vertrag_pruefen(welt["a"], sicht, G_A, U, 100, None)[1] is None
    sicht = _einstellen(welt, strafen={"nitrado_ban": True})
    assert bot._kredit_vertrag_pruefen(welt["a"], sicht, G_A, U, 100, None)[1]["code"] == "verknuepfung"
    bot.db.link_user(G_A, U, "Spieler1")
    assert bot._kredit_vertrag_pruefen(welt["a"], sicht, G_A, U, 100, None)[1] is None


def test_vertrag_explosion_verlangt_einen_gespeicherten_pfand_ort(welt):
    bot.db.link_user(G_A, U, "Spieler1")
    sicht = _einstellen(welt, strafen={"explosion": True}, explosion_klasse="ExplosionTest")
    for ort in (None, "", "Gibt es nicht", 5):
        assert bot._kredit_vertrag_pruefen(welt["a"], sicht, G_A, U, 100, ort)[1]["code"] == "ort"
    bot.db.ort_speichern(G_A, "1000", U, "Basis", 4000.5, 5000.5)
    v, f = bot._kredit_vertrag_pruefen(welt["a"], sicht, G_A, U, 100, "basis")
    assert f is None and v["explosion"] == {"klasse": "ExplosionTest", "ort": "Basis", "x": 4000.5, "z": 5000.5} and v["strafen"]["explosion"] is True


def test_pfand_ort_eines_anderen_oder_anderen_servers_zaehlt_nicht(welt):
    bot.db.link_user(G_A, U, "Spieler1")
    sicht = _einstellen(welt, strafen={"explosion": True}, explosion_klasse="ExplosionTest")
    bot.db.ort_speichern(G_A, "1000", ANDERER, "Fremd", 1.0, 2.0)
    bot.db.ort_speichern(G_A, "2000", U, "AndererServer", 1.0, 2.0)
    bot.db.ort_speichern(G_B, "1000", U, "AndereGuild", 1.0, 2.0)
    for ort in ("Fremd", "AndererServer", "AndereGuild"):
        assert bot._kredit_vertrag_pruefen(welt["a"], sicht, G_A, U, 100, ort)[1]["code"] == "ort"


# ── Annehmen / Auszahlen ────────────────────────────────────────────────────
def test_annehmen_zahlt_aus_und_speichert_den_schnappschuss(welt):
    sicht = _einstellen(welt, belohnung_prozent=50, strafen={"pfaendung": True})
    erg = _annehmen(welt, sicht, 1000)
    assert erg["ok"] and erg["wallet"] == 1000 and bot.db.get_balance(G_A, U)[0] == 1000
    k = bot.db.kredit_holen(erg["kredit"]["id"])
    assert (k["betrag"], k["zins"], k["bezahlt"], k["rest"], k["status"], k["user_id"], k["guild_id"], k["service_id"]) == (1000, 100, 0, 1100, "offen", U, G_A, "1000")
    assert k["bedingungen"] == {"strafen": {"pfaendung": True, "nitrado_ban": False, "explosion": False}, "belohnung": 50, "verzug": 50,
                                "explosion": None, "laufzeit_tage": 7, "gnade_tage": 1}
    assert abs(k["faellig"] - (k["erstellt"] + 7 * TAG)) < 1 and abs(k["gnade_bis"] - (k["faellig"] + TAG)) < 1
    assert welt["feeds"][-1] == (G_A, "economy_log", "1000")


def test_annehmen_mit_geaenderten_bedingungen_wird_abgelehnt(welt):
    sicht = _einstellen(welt)
    v, _f = bot._kredit_vertrag_pruefen(welt["a"], sicht, G_A, U, 1000, None)
    _einstellen(welt, zins_prozent=50)                                                       # Betreiber aendert, waehrend der Spieler liest
    erg = _annehmen(welt, bot.guild_sicht(welt["a"], G_A), 1000, h=v["hash"])
    assert erg["ok"] is False and erg["code"] == "geaendert" and bot.db.kredit_liste(G_A, U) == [] and bot.db.get_balance(G_A, U)[0] == 0


@pytest.mark.parametrize("h", [None, "", "abc", "0" * 16, 5, ["x"]])
def test_annehmen_ohne_gueltige_bestaetigung(welt, h):
    sicht = _einstellen(welt)
    erg = asyncio.run(bot._kredit_annehmen(welt["a"], sicht, G_A, U, 1000, None, h))
    assert erg["ok"] is False and erg["code"] == "geaendert" and bot.db.kredit_liste(G_A, U) == []


def test_annehmen_hoechstens_max_offen(welt):
    sicht = _einstellen(welt, max_offen=2)
    assert _annehmen(welt, sicht, 100)["ok"] and _annehmen(welt, sicht, 100)["ok"]
    v = bot._kredit_vertrag_pruefen(welt["a"], sicht, G_A, U, 100, None)
    assert v[1]["code"] == "offen" and len(bot.db.kredit_liste(G_A, U, nur_offen=True)) == 2


def test_gleichzeitiges_annehmen_legt_nur_einen_kredit_an(welt):
    sicht = _einstellen(welt, max_offen=1)
    v, _f = bot._kredit_vertrag_pruefen(welt["a"], sicht, G_A, U, 500, None)

    async def beide():
        return await asyncio.gather(*(bot._kredit_annehmen(welt["a"], sicht, G_A, U, 500, None, v["hash"]) for _ in range(5)))
    ergebnisse = asyncio.run(beide())
    assert sum(1 for e in ergebnisse if e["ok"]) == 1 and len(bot.db.kredit_liste(G_A, U)) == 1 and bot.db.get_balance(G_A, U)[0] == 500


def test_annehmen_wallet_obergrenze_zahlt_nichts_halbes_aus(welt, monkeypatch):
    sicht = _einstellen(welt)
    bot.db.set_wallet(G_A, U, 0)
    monkeypatch.setattr(bot.db, "_max_balances", lambda gid: (300, 10 ** 9))
    erg = _annehmen(welt, sicht, 1000)
    assert erg["ok"] is False and erg["code"] == "limit" and bot.db.get_balance(G_A, U)[0] == 0 and bot.db.kredit_liste(G_A, U) == []


def test_annehmen_je_guild_getrennt(welt):
    sicht = _einstellen(welt)
    _annehmen(welt, sicht, 100)
    assert bot.db.kredit_liste(G_B, U) == [] and len(bot.db.kredit_liste(G_A, U)) == 1


# ── Rueckzahlen / Belohnung ─────────────────────────────────────────────────
def test_teilzahlung_und_komplettzahlung_mit_belohnung(welt):
    sicht = _einstellen(welt, belohnung_prozent=50)
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]                    # 1000 + 100 Zinsen, Belohnung 50
    erg = _zahlen(welt, kid, 400)
    assert erg["ok"] and erg["bezahlt"] == 400 and erg["rest"] == 700 and erg["komplett"] is False and bot.db.get_balance(G_A, U)[0] == 600
    bot.db.add_wallet(G_A, U, 200)                                         # Wallet 800
    erg = _zahlen(welt, kid, 5000)                                         # mehr als noetig: nur der Rest (700) wird genommen
    assert erg["ok"] and erg["bezahlt"] == 700 and erg["komplett"] and erg["puenktlich"] and erg["rest"] == 0
    assert bot.db.get_balance(G_A, U)[0] == 800 - 700 + 50                 # Belohnung 50 aufs Wallet
    k = bot.db.kredit_holen(kid)
    assert k["status"] == "bezahlt" and k["bezahlt"] == 1100 and k["puenktlich"] == 1 and k["geschlossen"] is not None
    assert "Belohnung" in erg["notiz"]


def test_keine_belohnung_bei_verspaeteter_zahlung(welt):
    sicht = _einstellen(welt, belohnung_prozent=100)
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    with bot.db._lock:
        bot.db._conn.execute("UPDATE kredite SET faellig=? WHERE id=?", (time.time() - 10, kid))
        bot.db._conn.commit()
    bot.db.add_wallet(G_A, U, 500)
    erg = _zahlen(welt, kid, 1100)
    assert erg["komplett"] and erg["puenktlich"] is False and bot.db.get_balance(G_A, U)[0] == 1500 - 1100 and erg["notiz"] == ""


def test_zahlen_ohne_genug_guthaben_aendert_nichts(welt):
    sicht = _einstellen(welt)
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    bot.db.set_wallet(G_A, U, 100)
    erg = _zahlen(welt, kid, 500)
    assert erg["ok"] is False and erg["code"] == "guthaben" and bot.db.get_balance(G_A, U)[0] == 100 and bot.db.kredit_holen(kid)["bezahlt"] == 0


@pytest.mark.parametrize("betrag", [0, -5, 1.5, "100", None, True, [1]])
def test_zahlen_ungueltiger_betrag(welt, betrag):
    sicht = _einstellen(welt)
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    erg = _zahlen(welt, kid, betrag)
    assert erg["ok"] is False and erg["code"] == "betrag" and bot.db.get_balance(G_A, U)[0] == 1000


@pytest.mark.parametrize("kid", [None, 0, -1, 99999, "1", True, 1.0, [1]])
def test_zahlen_unbekannter_kredit(welt, kid):
    sicht = _einstellen(welt)
    _annehmen(welt, sicht, 1000)
    assert _zahlen(welt, kid, 10)["code"] == "kredit"


def test_zahlen_nur_auf_den_eigenen_kredit_dieser_guild_und_dieses_servers(welt):
    sicht = _einstellen(welt)
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    bot.db.set_wallet(G_A, ANDERER, 5000)
    assert _zahlen(welt, kid, 100, uid=ANDERER)["code"] == "kredit" and bot.db.get_balance(G_A, ANDERER)[0] == 5000
    bot.db.set_wallet(G_B, U, 5000)
    assert _zahlen(welt, kid, 100, gid=G_B, conn="b")["code"] == "kredit" and bot.db.get_balance(G_B, U)[0] == 5000
    assert _zahlen(welt, kid, 100, conn="b")["code"] == "kredit"                 # richtige Guild, falscher Server
    assert bot.db.kredit_holen(kid)["bezahlt"] == 0


def test_zahlen_nicht_ueber_eine_andere_guild_desselben_servers(welt):
    bot.connections.add_guild("1000", 4242)                                       # zweite Guild am selben Nitrado-Server
    sicht = _einstellen(welt)
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    bot.db.set_wallet(4242, U, 5000)
    assert _zahlen(welt, kid, 100, gid=4242)["code"] == "kredit" and bot.db.get_balance(4242, U)[0] == 5000 and bot.db.kredit_holen(kid)["bezahlt"] == 0


def test_zahlen_gibt_geld_zurueck_wenn_die_buchung_weniger_anwendet(welt, monkeypatch):
    """Wurde der Kredit zwischen Abbuchung und Buchung veraendert (z. B. erlassen), bekommt der Spieler das Geld zurueck."""
    sicht = _einstellen(welt)
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    monkeypatch.setattr(bot.db, "kredit_zahlung", lambda k, b, j: {"angewendet": 0, "rest": 0, "komplett": False, "puenktlich": False})
    erg = _zahlen(welt, kid, 400)
    assert erg["ok"] and bot.db.get_balance(G_A, U)[0] == 1000


def test_zahlen_auf_geschlossenen_kredit(welt):
    sicht = _einstellen(welt)
    kid = _annehmen(welt, sicht, 100)["kredit"]["id"]
    bot.db.add_wallet(G_A, U, 100)
    assert _zahlen(welt, kid, 1000)["komplett"] is True
    assert _zahlen(welt, kid, 10)["code"] == "geschlossen"


def test_gleichzeitiges_zahlen_bucht_nie_mehr_als_der_rest(welt):
    sicht = _einstellen(welt)
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    bot.db.set_wallet(G_A, U, 100000)

    async def alle():
        return await asyncio.gather(*(bot._kredit_zahlen(welt["a"], G_A, U, kid, 1100) for _ in range(5)))
    erg = asyncio.run(alle())
    assert sum(1 for e in erg if e["ok"]) == 1 and bot.db.get_balance(G_A, U)[0] == 100000 - 1100 and bot.db.kredit_holen(kid)["bezahlt"] == 1100


# ── DB ──────────────────────────────────────────────────────────────────────
def _anlegen(db, uid=5, gid=1, sid="S", betrag=1000, zins=100, faellig=1000.0, gnade=2000.0, max_offen=1):
    return db.kredit_anlegen(gid, uid, sid, betrag, zins, faellig, gnade, {"strafen": {}}, max_offen, 10.0)


def test_db_max_offen_atomar_und_je_nutzer(db):
    assert _anlegen(db) is not None and _anlegen(db) is None
    assert _anlegen(db, uid=6) is not None and _anlegen(db, gid=2) is not None
    assert _anlegen(db, max_offen=2) is not None


def test_db_zahlung_regeln(db):
    kid = _anlegen(db)
    assert db.kredit_zahlung(kid, 0, 500.0)["angewendet"] == 0
    r = db.kredit_zahlung(kid, 300, 500.0)
    assert r == {"angewendet": 300, "rest": 800, "komplett": False, "puenktlich": False}
    r = db.kredit_zahlung(kid, 10 ** 6, 500.0)
    assert r == {"angewendet": 800, "rest": 0, "komplett": True, "puenktlich": True}
    assert db.kredit_zahlung(kid, 5, 500.0) == {"angewendet": 0, "rest": 0, "komplett": False, "puenktlich": False}
    assert db.kredit_holen(kid)["status"] == "bezahlt"


def test_db_puenktlichkeit_grenze(db):
    a, b = _anlegen(db, uid=1), _anlegen(db, uid=2)
    assert db.kredit_zahlung(a, 1100, 1000.0)["puenktlich"] is True              # genau am Faelligkeitszeitpunkt
    assert db.kredit_zahlung(b, 1100, 1000.1)["puenktlich"] is False


def test_db_stufen_nur_einmal_und_nur_offen(db):
    kid = _anlegen(db)
    assert db.kredit_stufe_markieren(kid, "ban") is True and db.kredit_stufe_markieren(kid, "ban") is False
    assert db.kredit_stufe_markieren(kid, "explosion") is True and db.kredit_holen(kid)["stufen"] == ["ban", "explosion"]
    db.kredit_zahlung(kid, 10 ** 6, 1.0)
    assert db.kredit_stufe_markieren(kid, "verzug") is False


def test_db_pfaendung_wallet_zuerst_dann_bank(db):
    db.set_wallet(1, 5, 300)
    db.deposit(1, 5, 0)
    with db._lock:
        db._conn.execute("UPDATE balances SET bank=1000 WHERE guild_id=1 AND user_id=5")
        db._conn.commit()
    assert db.kredit_pfaenden(1, 5, 200) == 200 and db.get_balance(1, 5) == (100, 1000)
    assert db.kredit_pfaenden(1, 5, 600) == 600 and db.get_balance(1, 5) == (0, 500)
    assert db.kredit_pfaenden(1, 5, 10 ** 6) == 500 and db.get_balance(1, 5) == (0, 0)
    assert db.kredit_pfaenden(1, 5, 10) == 0
    assert db.kredit_pfaenden(2, 5, 10) == 0                                      # andere Guild: eigenes Guthaben


def test_db_erlassen_und_loeschen(db):
    kid = _anlegen(db)
    assert db.kredit_erlassen(kid, 5.0) is True and db.kredit_erlassen(kid, 5.0) is False and db.kredit_holen(kid)["status"] == "erlassen"
    db.kredit_loeschen(kid)
    assert db.kredit_holen(kid) is None


def test_db_listen_getrennt(db):
    a, b = _anlegen(db, uid=1), _anlegen(db, uid=2, gid=2)
    db.kredit_zahlung(a, 10 ** 6, 1.0)
    assert [k["id"] for k in db.kredit_liste(1, 1)] == [a] and db.kredit_liste(1, 1, nur_offen=True) == []
    assert [k["id"] for k in db.kredit_alle_offen()] == [b]
    assert db.kredit_liste(3, 1) == [] and [k["id"] for k in db.kredit_liste(1)] == [a]


def test_db_kaputtes_json_wird_toleriert(db):
    kid = _anlegen(db)
    with db._lock:
        db._conn.execute("UPDATE kredite SET bedingungen='{kaputt', stufen='x', ban_namen='' WHERE id=?", (kid,))
        db._conn.commit()
    k = db.kredit_holen(kid)
    assert k["bedingungen"] == {} and k["stufen"] == [] and k["ban_namen"] == []


# ── Strafen (nach der Gnadenfrist) ──────────────────────────────────────────
@pytest.fixture
def ban(monkeypatch):
    log = {"bans": [], "unbans": [], "fehler": None}

    async def hinzu(conn, namen, grund, von, **kw):
        if log["fehler"]:
            return [], [], log["fehler"]
        log["bans"].append((conn.service_id, list(namen), grund, von))
        return list(namen), [], None

    async def weg(conn, namen, *, von="System", grund=""):
        log["unbans"].append((conn.service_id, list(namen), grund))
        return list(namen), [], None
    monkeypatch.setattr(bot, "_ban_namen_hinzufuegen", hinzu)
    monkeypatch.setattr(bot, "_ban_namen_entfernen", weg)
    return log


def _lauf(jetzt):
    return asyncio.run(bot._kredit_lauf(jetzt))


def _nach_gnade(kid, sekunden=10):
    return bot.db.kredit_holen(kid)["gnade_bis"] + sekunden


def test_vor_ende_der_gnadenfrist_passiert_nichts(welt, ban):
    sicht = _einstellen(welt, strafen={"pfaendung": True, "nitrado_ban": True})
    bot.db.link_user(G_A, U, "Spieler1")
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    k = bot.db.kredit_holen(kid)
    assert _lauf(k["faellig"] + 10) == 0 and _lauf(k["gnade_bis"]) == 0
    assert bot.db.kredit_holen(kid)["stufen"] == [] and ban["bans"] == [] and bot.db.get_balance(G_A, U)[0] == 1000


def test_ohne_strafen_im_vertrag_passiert_nach_der_frist_nichts(welt, ban):
    sicht = _einstellen(welt)                                                 # keine Strafe gewaehlt
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    _lauf(_nach_gnade(kid, 10 * TAG))
    k = bot.db.kredit_holen(kid)
    assert k["status"] == "offen" and k["bezahlt"] == 0 and k["stufen"] == [] and ban["bans"] == [] and bot.db.get_balance(G_A, U)[0] == 1000


def test_pfaendung_verzugsgebuehr_einmal_dann_laufend(welt, ban):
    sicht = _einstellen(welt, verzug_prozent=5, strafen={"pfaendung": True})
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]                       # Schuld 1100, Verzug 50
    bot.db.set_wallet(G_A, U, 300)                                            # Wallet 300 (Auszahlung ausgegeben)
    jetzt = _nach_gnade(kid)
    assert _lauf(jetzt) == 1
    k = bot.db.kredit_holen(kid)
    assert k["zins"] == 150 and k["bezahlt"] == 300 and k["rest"] == 850 and k["stufen"] == ["verzug"] and bot.db.get_balance(G_A, U) == (0, 0)
    assert _lauf(jetzt + 600) == 1
    assert bot.db.kredit_holen(kid)["zins"] == 150                           # Verzugsgebuehr nicht noch einmal
    bot.db.add_wallet(G_A, U, 100)                                           # neues Geld wird beim naechsten Lauf gepfaendet
    _lauf(jetzt + 1200)
    assert bot.db.kredit_holen(kid)["bezahlt"] == 400 and bot.db.get_balance(G_A, U) == (0, 0)


def test_pfaendung_nimmt_auch_von_der_bank_und_schliesst_den_kredit(welt, ban):
    sicht = _einstellen(welt, belohnung_prozent=100, strafen={"pfaendung": True})
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    bot.db.set_wallet(G_A, U, 100)
    with bot.db._lock:
        bot.db._conn.execute("UPDATE balances SET bank=5000 WHERE guild_id=? AND user_id=?", (G_A, U))
        bot.db._conn.commit()
    _lauf(_nach_gnade(kid))
    k = bot.db.kredit_holen(kid)
    assert k["status"] == "bezahlt" and k["bezahlt"] == 1150 and k["puenktlich"] == 0
    assert bot.db.get_balance(G_A, U) == (0, 5000 - 1050)                      # erst Wallet (100), dann Bank (1050)


def test_pfaendung_ohne_guthaben_nimmt_nichts_und_bleibt_offen(welt, ban):
    sicht = _einstellen(welt, strafen={"pfaendung": True})
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    bot.db.set_wallet(G_A, U, 0)
    _lauf(_nach_gnade(kid))
    k = bot.db.kredit_holen(kid)
    assert k["status"] == "offen" and k["bezahlt"] == 0 and k["rest"] == 1150


def test_pfaendung_trifft_nur_den_schuldner_in_dieser_guild(welt, ban):
    sicht = _einstellen(welt, strafen={"pfaendung": True})
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    bot.db.set_wallet(G_A, ANDERER, 777)
    bot.db.set_wallet(G_B, U, 888)
    bot.db.set_wallet(G_A, U, 0)
    _lauf(_nach_gnade(kid))
    assert bot.db.get_balance(G_A, ANDERER)[0] == 777 and bot.db.get_balance(G_B, U)[0] == 888


def test_bann_bei_verzug_und_aufheben_nach_rueckzahlung(welt, ban):
    sicht = _einstellen(welt, strafen={"nitrado_ban": True})
    bot.db.link_user(G_A, U, "Spieler1")
    bot.db.link_user(G_A, U, "Zweitkonto")
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    _lauf(_nach_gnade(kid))
    assert [(s, sorted(n), g) for s, n, g, _v in ban["bans"]] == [("1000", ["Spieler1", "Zweitkonto"], "Kredit überfällig")]
    k = bot.db.kredit_holen(kid)
    assert k["stufen"] == ["ban"] and sorted(k["ban_namen"]) == ["Spieler1", "Zweitkonto"]
    _lauf(_nach_gnade(kid, 600))
    assert len(ban["bans"]) == 1                                              # nicht noch einmal
    bot.db.add_wallet(G_A, U, 500)
    erg = _zahlen(welt, kid, 1100)
    assert erg["komplett"] and [(s, sorted(n)) for s, n, _g in ban["unbans"]] == [("1000", ["Spieler1", "Zweitkonto"])] and "Bann aufgehoben" in erg["notiz"]


def test_bann_fehlgeschlagen_wird_beim_naechsten_lauf_wiederholt(welt, ban):
    sicht = _einstellen(welt, strafen={"nitrado_ban": True})
    bot.db.link_user(G_A, U, "Spieler1")
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    ban["fehler"] = "Nitrado nicht erreichbar"
    _lauf(_nach_gnade(kid))
    assert bot.db.kredit_holen(kid)["stufen"] == [] and ban["bans"] == []
    ban["fehler"] = None
    _lauf(_nach_gnade(kid, 600))
    assert bot.db.kredit_holen(kid)["stufen"] == ["ban"] and len(ban["bans"]) == 1


def test_bann_nur_wenn_im_vertrag_vereinbart_auch_wenn_inzwischen_eingeschaltet(welt, ban):
    sicht = _einstellen(welt)                                                 # beim Vertrag: keine Strafe
    bot.db.link_user(G_A, U, "Spieler1")
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    _einstellen(welt, strafen={"nitrado_ban": True, "pfaendung": True})        # Betreiber schaltet spaeter Strafen ein
    _lauf(_nach_gnade(kid))
    assert ban["bans"] == [] and bot.db.kredit_holen(kid)["stufen"] == []


def test_bann_ohne_verknuepften_namen_und_ohne_api(welt, ban):
    sicht = _einstellen(welt, strafen={"nitrado_ban": True})
    bot.db.link_user(G_A, U, "Spieler1")
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    welt["a"].api = None
    _lauf(_nach_gnade(kid))
    assert ban["bans"] == [] and bot.db.kredit_holen(kid)["stufen"] == []


def test_erlassen_hebt_bann_auf(welt, ban):
    sicht = _einstellen(welt, strafen={"nitrado_ban": True})
    bot.db.link_user(G_A, U, "Spieler1")
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    _lauf(_nach_gnade(kid))
    st, d = _mgmt(welt, "POST", "/erlassen", {"id": kid})
    assert st == 200 and bot.db.kredit_holen(kid)["status"] == "erlassen" and ban["unbans"] == [("1000", ["Spieler1"], "Kredit erlassen")]
    assert _lauf(_nach_gnade(kid, 5000)) == 0


@pytest.fixture
def expl(monkeypatch):
    log = {"aufrufe": [], "ergebnis": (True, "Airstrike_Kredit1")}

    async def f(conn, klasse, x, z, kid):
        log["aufrufe"].append((conn.service_id, klasse, x, z, kid))
        return log["ergebnis"]
    monkeypatch.setattr(bot, "_kredit_explosion", f)
    return log


def _expl_kredit(welt, **kw):
    bot.db.link_user(G_A, U, "Spieler1")
    bot.db.ort_speichern(G_A, "1000", U, "Basis", 4000.5, 5000.5)
    sicht = _einstellen(welt, strafen={"explosion": True}, explosion_klasse="ExplosionTest", **kw)
    return _annehmen(welt, sicht, 1000, ort="Basis")["kredit"]["id"]


def test_explosion_am_pfand_ort_einmal(welt, ban, expl):
    kid = _expl_kredit(welt)
    _lauf(_nach_gnade(kid))
    assert expl["aufrufe"] == [("1000", "ExplosionTest", 4000.5, 5000.5, kid)] and bot.db.kredit_holen(kid)["stufen"] == ["explosion"]
    _lauf(_nach_gnade(kid, 600))
    assert len(expl["aufrufe"]) == 1


def test_explosion_hoechstens_drei_versuche_bei_fehlern(welt, ban, expl):
    kid = _expl_kredit(welt)
    expl["ergebnis"] = (False, "Schreiben fehlgeschlagen")
    for i in range(6):
        _lauf(_nach_gnade(kid, 600 * (i + 1)))
    assert len(expl["aufrufe"]) == 3 and bot.db.kredit_holen(kid)["stufen"] == []


def test_explosion_nicht_nach_rueckzahlung_und_nicht_nach_erlass(welt, ban, expl):
    kid = _expl_kredit(welt)
    bot.db.add_wallet(G_A, U, 500)
    assert _zahlen(welt, kid, 1100)["komplett"]
    _lauf(_nach_gnade(kid, 5000))
    assert expl["aufrufe"] == []


def test_explosion_vorher_nichts_und_ohne_vereinbarung_nichts(welt, ban, expl):
    kid = _expl_kredit(welt)
    _lauf(bot.db.kredit_holen(kid)["faellig"] + 1)
    assert expl["aufrufe"] == []
    sicht = _einstellen(welt, strafen={"pfaendung": True})                       # ohne Explosion im Vertrag
    bot.db.kredit_erlassen(kid, 1.0)
    kid2 = _annehmen(welt, sicht, 500)["kredit"]["id"]
    _lauf(_nach_gnade(kid2))
    assert expl["aufrufe"] == []


def test_ein_kaputter_kredit_haelt_die_anderen_nicht_auf(welt, ban, monkeypatch):
    sicht = _einstellen(welt, strafen={"pfaendung": True})
    bot.db.set_wallet(G_A, ANDERER, 0)
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    bot.db.set_wallet(G_A, U, 100)
    echt = bot._kredit_strafen
    aufrufe = []

    async def kaputt(conn, k, jetzt):
        aufrufe.append(k["id"])
        raise RuntimeError("boom")
    monkeypatch.setattr(bot, "_kredit_strafen", kaputt)
    assert _lauf(_nach_gnade(kid)) == 0 and aufrufe == [kid]
    monkeypatch.setattr(bot, "_kredit_strafen", echt)
    assert _lauf(_nach_gnade(kid)) == 1


def test_lauf_ueberspringt_kredite_unbekannter_server(welt, ban):
    sicht = _einstellen(welt, strafen={"pfaendung": True})
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    with bot.db._lock:
        bot.db._conn.execute("UPDATE kredite SET service_id='9999' WHERE id=?", (kid,))
        bot.db._conn.commit()
    assert _lauf(_nach_gnade(kid)) == 0


# ── Explosion selbst (gegen das FTP-Double des Airstrike-Werkzeugs) ─────────
@pytest.fixture
def mission(welt):
    welt["a"].ftp = FTP()
    welt["a"].data["map_name"] = "Livonia"
    return welt


def _expl(mission, klasse="ExplosionTest", x=4000.5, z=5000.5, kid=7):
    return asyncio.run(bot._kredit_explosion(mission["a"], klasse, x, z, kid))


def test_explosion_schreibt_ein_airstrike_event(mission):
    ftp = mission["a"].ftp
    ok_, name = _expl(mission)
    assert ok_ and name == "Airstrike_Kredit7"
    ev, sp = ftp.files["/mission/db/events.xml"], ftp.files["/mission/cfgeventspawns.xml"]
    ET.fromstring(ev), ET.fromstring(sp)
    assert 'name="Airstrike_Kredit7"' in ev and 'type="ExplosionTest"' in ev and 'x="4000.5"' in sp and 'z="5000.5"' in sp
    assert "StaticOriginal" in ev and [e["event_name"] for e in mission["a"].data["airstrikes"]] == ["Airstrike_Kredit7"]


def test_explosion_ist_idempotent(mission):
    _expl(mission)
    schreibvorgaenge = len(mission["a"].ftp.writes)
    ok_, name = _expl(mission)
    assert ok_ and name == "Airstrike_Kredit7" and len(mission["a"].ftp.writes) == schreibvorgaenge


@pytest.mark.parametrize("x,z", [(-1.0, 100.0), (100.0, -5.0), (99999.0, 100.0), (100.0, 99999.0)])
def test_explosion_ausserhalb_der_karte(mission, x, z):
    ok_, info = _expl(mission, x=x, z=z)
    assert ok_ is False and "Karte" in info and mission["a"].ftp.writes == []


def test_explosion_klasse_muss_in_der_types_xml_stehen(mission):
    ok_, info = _expl(mission, klasse="GibtEsNicht")
    assert ok_ is False and "types.xml" in info and mission["a"].ftp.writes == []


def test_explosion_ohne_mission_ordner_oder_lesbare_dateien(mission):
    del mission["a"].ftp.files["/mission/cfgeventspawns.xml"]
    assert _expl(mission)[0] is False
    mission["a"].data["ftp_mission_dir"] = ""
    mission["a"].set("ftp_mission_dir", "")
    assert _expl(mission) == (False, "Mission-Ordner unbekannt")


def test_explosion_schreibfehler_wird_zurueckgerollt(mission):
    ftp = mission["a"].ftp
    vorher = dict(ftp.files)
    ftp.fail_at = 2                                                                  # erste Datei ok, zweite scheitert
    ok_, info = _expl(mission)
    assert ok_ is False and ftp.files == vorher and "airstrikes" not in mission["a"].data


def test_explosion_unbekannte_karte(mission):
    mission["a"].data["map_name"] = "GibtEsNicht"
    assert _expl(mission)[0] is False and mission["a"].ftp.writes == []


# ── Dashboard-API ───────────────────────────────────────────────────────────
def _admin(welt, gid=G_A, conn="a", admin=True, gast=None):
    sid = str(time.time_ns())
    c = welt[conn]
    bot._SESS_STORE[sid] = {"token": "x", "discord": {"id": gast or (sid if admin else str(c.data["owner_discord_id"]))},
                            "is_admin": admin and not gast, "service_id": c.service_id, "seen": time.time(),
                            "admin_geprueft_ts": time.time(), "guild_id": str(gid), **({"is_guest": True} if gast else {})}
    c.data["kunden_stufe"] = "premium"
    return sid


def _mgmt(welt, methode, pfad="", daten=None, **kw):
    return api(methode, "/api/discord-management/kredite" + pfad, _admin(welt, **kw), daten)


def _speichern(welt, **kw):
    daten = {"enabled": True, "max_betrag": 5000, "zins_prozent": 12, "laufzeit_tage": 14, "gnade_tage": 2, "max_offen": 2,
             "belohnung_prozent": 25, "verzug_prozent": 8, "strafen": {"pfaendung": True, "nitrado_ban": False, "explosion": False}, "explosion_klasse": ""}
    daten.update(kw)
    return _mgmt(welt, "POST", "", daten)


def test_mgmt_ohne_login_und_als_spieler_gesperrt(welt):
    s = _session(gid=G_A, sid_="1000")
    for sid in (None, s):
        for methode, pfad in (("GET", ""), ("POST", ""), ("POST", "/erlassen")):
            assert api(methode, "/api/discord-management/kredite" + pfad, sid, {})[0] in (401, 403)


def test_mgmt_lesen_standard(welt):
    st, d = _mgmt(welt, "GET")
    assert st == 200 and d["data"]["enabled"] is False and d["data"]["strafen"] == {"pfaendung": False, "nitrado_ban": False, "explosion": False} and d["data"]["kredite"] == []


def test_mgmt_speichern_und_lesen(welt):
    st, d = _speichern(welt)
    assert st == 200, d
    e = bot._kredit_einstellungen(bot.guild_sicht(welt["a"], G_A))
    assert e["enabled"] and (e["max_betrag"], e["zins_prozent"], e["laufzeit_tage"], e["gnade_tage"], e["max_offen"], e["belohnung_prozent"], e["verzug_prozent"]) == (5000, 12, 14, 2, 2, 25, 8)
    assert e["strafen"] == {"pfaendung": True, "nitrado_ban": False, "explosion": False}
    assert [z["detail"] for z in bot._audit_log if z["action"] == "Discord-Management aktualisiert"] == ["kredite · Server 1000"]
    assert bot._discord_mgmt_payload(bot.guild_sicht(welt["a"], G_A))["kredite"] == {"enabled": True}
    assert "kredite" not in bot.cfg.config
    assert bot.connections.primary() is welt["a"]
    bot._conn_store(welt["a"], "kredite", {"enabled": True})                  # auch beim Hauptserver selbst nie nach config.json
    assert "kredite" not in bot.cfg.config
    bot._conn_store(welt["a"], "irgendein_serverwert", 5)
    assert bot.cfg.config.get("irgendein_serverwert") == 5


def test_mgmt_explosion_braucht_klasse(welt):
    st, d = _speichern(welt, strafen={"pfaendung": False, "nitrado_ban": False, "explosion": True}, explosion_klasse="")
    assert st == 400 and "Explosionsklasse" in d["error"]
    st, d = _speichern(welt, strafen={"pfaendung": False, "nitrado_ban": False, "explosion": True}, explosion_klasse="ExplosionTest")
    assert st == 200 and bot._kredit_einstellungen(bot.guild_sicht(welt["a"], G_A))["strafen"]["explosion"] is True


@pytest.mark.parametrize("aenderung,teil", [
    ({"max_betrag": 0}, "Zahlenwert"), ({"max_betrag": "5"}, "Zahlenwert"), ({"zins_prozent": 101}, "Zahlenwert"), ({"laufzeit_tage": 0}, "Zahlenwert"),
    ({"laufzeit_tage": 91}, "Zahlenwert"), ({"gnade_tage": -1}, "Zahlenwert"), ({"max_offen": 6}, "Zahlenwert"), ({"belohnung_prozent": True}, "Zahlenwert"),
    ({"verzug_prozent": 51}, "Zahlenwert"), ({"zins_prozent": 1.5}, "Zahlenwert"),
    ({"strafen": "x"}, "Strafen"), ({"strafen": {"pfaendung": True}}, "Strafen"), ({"strafen": {"pfaendung": 1, "nitrado_ban": 0, "explosion": 0}}, "Strafen"),
    ({"strafen": {"pfaendung": True, "nitrado_ban": False, "explosion": False, "extra": True}}, "Strafen"),
    ({"explosion_klasse": "a b"}, "Explosionsklasse"), ({"explosion_klasse": "x" * 65}, "Explosionsklasse")])
def test_mgmt_ablehnungen_speichern_nichts(welt, aenderung, teil):
    st, d = _speichern(welt, **aenderung)
    assert st == 400 and teil in d["error"], d
    assert bot._kredit_einstellungen(bot.guild_sicht(welt["a"], G_A))["enabled"] is False


def test_mgmt_enabled_nur_bei_echtem_true(welt):
    st, d = _speichern(welt, enabled="true")
    assert st == 200 and bot._kredit_einstellungen(bot.guild_sicht(welt["a"], G_A))["enabled"] is False


def test_mgmt_gast_nur_ansehen(welt):
    welt["a"].data["dashboard_perms"] = {"user:900": {"discord_mgmt": ["view"]}}
    assert _mgmt(welt, "GET", gast="900")[0] == 200
    assert _mgmt(welt, "POST", "", {"enabled": True}, gast="900")[0] == 403
    assert _mgmt(welt, "POST", "/erlassen", {"id": 1}, gast="900")[0] == 403
    assert _mgmt(welt, "GET", gast="901")[0] == 403


def test_mgmt_liste_nur_offene_dieser_guild_und_erlassen(welt):
    sicht = _einstellen(welt)
    kid = _annehmen(welt, sicht, 1000)["kredit"]["id"]
    sicht_b = _einstellen(welt, conn="b", gid=G_B)
    bot.db.set_wallet(G_B, U, 0)
    kid_b = _annehmen(welt, sicht_b, 500, gid=G_B, conn="b")["kredit"]["id"]
    liste = _mgmt(welt, "GET")[1]["data"]["kredite"]
    assert [k["id"] for k in liste] == [kid] and liste[0]["user_id"] == str(U) and liste[0]["rest"] == 1100
    assert _mgmt(welt, "POST", "/erlassen", {"id": kid_b})[0] == 404                      # Kredit einer fremden Guild
    assert bot.db.kredit_holen(kid_b)["status"] == "offen"
    st, d = _mgmt(welt, "POST", "/erlassen", {"id": kid})
    assert st == 200 and d["data"]["kredite"] == [] and bot.db.kredit_holen(kid)["status"] == "erlassen"
    assert _mgmt(welt, "POST", "/erlassen", {"id": kid})[0] in (409, 429)


@pytest.mark.parametrize("kid", [None, 0, "1", True, 99999, [1]])
def test_mgmt_erlassen_ungueltige_id(welt, kid):
    assert _mgmt(welt, "POST", "/erlassen", {"id": kid})[0] == 404


# ── Spieler-API ─────────────────────────────────────────────────────────────
def _s():
    return _session(gid=G_A, sid_="1000")


def test_spieler_api_ohne_login_und_server(welt):
    assert api("GET", "/api/me/kredit")[0] in (401, 403)
    assert api("GET", "/api/me/kredit", _session())[0] == 409
    for pfad in ("/vertrag", "/annehmen", "/zahlen"):
        assert api("POST", "/api/me/kredit" + pfad, None, {})[0] in (401, 403)


def test_spieler_status_zeigt_bedingungen_und_eigene_kredite(welt):
    sicht = _einstellen(welt, strafen={"pfaendung": True}, belohnung_prozent=10)
    bot.db.ort_speichern(G_A, "1000", U, "Basis", 1.0, 2.0)
    _annehmen(welt, sicht, 1000)
    bot.db.set_wallet(G_A, ANDERER, 0)
    sicht2 = bot.guild_sicht(welt["a"], G_A)
    _annehmen(welt, sicht2, 100, uid=ANDERER) if False else None
    st, d = api("GET", "/api/me/kredit", _s())
    s = d["data"]
    assert st == 200 and s["aktiv"] is True and s["strafen"]["pfaendung"] is True and s["max_betrag"] == 10000 and s["orte"] == [{"name": "Basis", "x": 1.0, "z": 2.0}]
    assert [k["rest"] for k in s["kredite"]] == [1100] and s["kredite"][0]["status"] == "offen" and "user_id" not in s["kredite"][0]


def test_spieler_vertrag_vorschau_legt_nichts_an(welt):
    _einstellen(welt, strafen={"pfaendung": True})
    st, d = api("POST", "/api/me/kredit/vertrag", _s(), {"betrag": 1000})
    assert st == 200 and d["data"]["gesamt"] == 1100 and d["data"]["verzug"] == 50 and len(d["data"]["hash"]) == 16 and d["data"]["gesamt_text"]
    assert bot.db.kredit_liste(G_A, U) == [] and bot.db.get_balance(G_A, U)[0] == 0


@pytest.mark.parametrize("daten,status,code", [({"betrag": 0}, 400, "betrag"), ({"betrag": "10"}, 400, "betrag"), ({"betrag": 10 ** 12}, 400, "betrag"), ({}, 400, "betrag")])
def test_spieler_vertrag_ablehnungen(welt, daten, status, code):
    _einstellen(welt)
    st, d = api("POST", "/api/me/kredit/vertrag", _s(), daten)
    assert st == status and d["code"] == code


def test_spieler_aus_ist_409(welt):
    _einstellen(welt, enabled=False)
    st, d = api("POST", "/api/me/kredit/vertrag", _s(), {"betrag": 100})
    assert st == 409 and d["code"] == "aus"
    assert api("POST", "/api/me/kredit/annehmen", _s(), {"betrag": 100, "hash": "x"})[0] == 409


def test_spieler_voller_weg_annehmen_und_zahlen(welt):
    _einstellen(welt, belohnung_prozent=100)
    v = api("POST", "/api/me/kredit/vertrag", _s(), {"betrag": 1000})[1]["data"]
    st, d = api("POST", "/api/me/kredit/annehmen", _s(), {"betrag": 1000, "hash": v["hash"]})
    assert st == 200 and d["data"]["wallet"] == 1000 and d["data"]["status"]["kredite"][0]["rest"] == 1100
    kid = d["data"]["kredit"]["id"]
    assert api("POST", "/api/me/kredit/zahlen", _s(), {"id": kid, "betrag": 100})[0] == 200
    bot.db.add_wallet(G_A, U, 500)
    st, d = api("POST", "/api/me/kredit/zahlen", _s(), {"id": kid, "betrag": 100000})
    assert st == 200 and d["data"]["komplett"] is True and d["data"]["status"]["kredite"][0]["status"] == "bezahlt"
    assert bot.db.get_balance(G_A, U)[0] == 1000 - 100 + 500 - 1000 + 100                  # + Belohnung (100 % der 100 Zinsen)


def test_spieler_annehmen_mit_falschem_oder_fehlendem_hash(welt):
    _einstellen(welt)
    for daten in ({"betrag": 1000}, {"betrag": 1000, "hash": "0" * 16}, {"betrag": 1000, "hash": 5}):
        st, d = api("POST", "/api/me/kredit/annehmen", _s(), daten)
        assert st == 409 and d["code"] == "geaendert"
    assert bot.db.kredit_liste(G_A, U) == []


def test_spieler_zahlen_fehler(welt):
    _einstellen(welt)
    kid = api("POST", "/api/me/kredit/annehmen", _s(), {"betrag": 1000, "hash": api("POST", "/api/me/kredit/vertrag", _s(), {"betrag": 1000})[1]["data"]["hash"]})[1]["data"]["kredit"]["id"]
    bot.db.set_wallet(G_A, U, 5)
    st, d = api("POST", "/api/me/kredit/zahlen", _s(), {"id": kid, "betrag": 100})
    assert st == 402 and d["code"] == "guthaben"
    assert api("POST", "/api/me/kredit/zahlen", _s(), {"id": 9999, "betrag": 1})[1]["code"] == "kredit"
    assert api("POST", "/api/me/kredit/zahlen", _s(), {"id": kid, "betrag": 0})[1]["code"] == "betrag"


def test_spieler_englische_fehler(welt):
    _einstellen(welt, enabled=False)
    s = _s()
    bot._SESS_STORE[s]["lang"] = "en"
    st, d = api("POST", "/api/me/kredit/vertrag", s, {"betrag": 100})
    assert st == 409 and "not active" in d["error"]


def test_spieler_andere_guild_sieht_nichts_von_fremden_krediten(welt):
    sicht = _einstellen(welt)
    _annehmen(welt, sicht, 1000)
    sb = _session(gid=G_B, sid_="2000")
    d = api("GET", "/api/me/kredit", sb)[1]["data"]
    assert d["kredite"] == [] and d["aktiv"] is False


def test_spieler_rate_limit_beim_annehmen(welt):
    from aiohttp.test_utils import TestClient, TestServer
    _einstellen(welt)
    s = _s()
    h = api("POST", "/api/me/kredit/vertrag", s, {"betrag": 100})[1]["data"]["hash"]

    async def lauf():
        async with TestClient(TestServer(bot.build_app())) as c:
            bot._DASH_RATE_LIMIT_LAST.clear()
            r1 = await c.post("/api/me/kredit/annehmen", json={"betrag": 100, "hash": h}, cookies={bot._SESS_COOKIE: s})
            r2 = await c.post("/api/me/kredit/annehmen", json={"betrag": 100, "hash": h}, cookies={bot._SESS_COOKIE: s})
            return r1.status, r2.status
    assert asyncio.run(lauf()) == (200, 429)


# ── Slash-Befehle ───────────────────────────────────────────────────────────
class _Resp:
    def __init__(self):
        self.sent = []
        self.view = None

    async def send_message(self, content=None, embed=None, ephemeral=False, view=None):
        self.sent.append({"content": content, "embed": embed})
        self.view = view

    async def edit_message(self, content=None, embed=None, view=None):
        self.sent.append({"content": content, "embed": embed})


class _Inter:
    def __init__(self, welt, uid=U, gid=G_A, locale=None):
        self.user = type("Nutzer", (), {"id": uid, "mention": f"<@{uid}>", "__str__": lambda s: "User"})()
        self.guild_id, self.guild, self.locale = gid, welt["guilds"][gid], locale
        self.response = _Resp()

    def text(self):
        return " ".join(str(m["content"] or "") + (str(m["embed"].to_dict()) if m["embed"] else "") for m in self.response.sent)


@pytest.fixture
def slash(welt, monkeypatch):
    async def ja(interaction):
        return True
    monkeypatch.setattr(bot, "_require_guild", ja)
    monkeypatch.setattr(bot, "_require_economy_enabled", ja)
    monkeypatch.setattr(bot, "_conn_of", lambda inter: welt["a"] if inter.guild_id == G_A else welt["b"])
    return welt


def _lauf_cmd(befehl, inter, **kw):
    asyncio.run(befehl.callback(inter, **kw))
    return inter.text()


def test_slash_request_zeigt_vertrag_mit_knoepfen_und_legt_nichts_an(slash):
    _einstellen(slash, belohnung_prozent=50, strafen={"pfaendung": True})
    inter = _Inter(slash)

    async def lauf():
        await bot.loan_request.callback(inter, amount=1000, collateral=None)
    asyncio.run(lauf())
    t = inter.text()
    assert "Kreditvertrag" in t and "1,100" in t and "Pfändung" in t and "Belohnung" in t and inter.response.view is not None
    assert bot.db.kredit_liste(G_A, U) == [] and bot.db.get_balance(G_A, U)[0] == 0


def test_slash_request_ablehnungen(slash):
    _einstellen(slash, enabled=False)
    assert "nicht aktiv" in _lauf_cmd(bot.loan_request, _Inter(slash), amount=100, collateral=None)
    _einstellen(slash, max_betrag=50)
    assert "1 bis 50" in _lauf_cmd(bot.loan_request, _Inter(slash), amount=100, collateral=None)


def test_slash_knopf_annehmen_zahlt_aus_und_nur_der_antragsteller_darf(slash):
    _einstellen(slash)
    inter = _Inter(slash)

    async def lauf():
        await bot.loan_request.callback(inter, amount=1000, collateral=None)
        view = inter.response.view
        fremd = _Inter(slash, uid=ANDERER)
        assert await view.interaction_check(fremd) is False and bot.db.kredit_liste(G_A, U) == []
        eigen = _Inter(slash)
        assert await view.interaction_check(eigen) is True
        await view.annehmen.callback(eigen)
        return eigen
    eigen = asyncio.run(lauf())
    assert "ausgezahlt" in eigen.text() and bot.db.get_balance(G_A, U)[0] == 1000 and len(bot.db.kredit_liste(G_A, U)) == 1


def test_slash_knopf_ablehnen_bucht_nichts(slash):
    _einstellen(slash)
    inter = _Inter(slash)

    async def lauf():
        await bot.loan_request.callback(inter, amount=1000, collateral=None)
        eigen = _Inter(slash)
        await inter.response.view.ablehnen.callback(eigen)
        return eigen
    eigen = asyncio.run(lauf())
    assert "Abgelehnt" in eigen.text() and bot.db.kredit_liste(G_A, U) == [] and bot.db.get_balance(G_A, U)[0] == 0


def test_slash_knopf_nur_einmal(slash):
    _einstellen(slash, max_offen=3)
    inter = _Inter(slash)

    async def lauf():
        await bot.loan_request.callback(inter, amount=100, collateral=None)
        view = inter.response.view
        await view.annehmen.callback(_Inter(slash))
        zweit = _Inter(slash)
        await view.annehmen.callback(zweit)
    asyncio.run(lauf())
    assert len(bot.db.kredit_liste(G_A, U)) == 1


def test_slash_knopf_mit_geaenderten_bedingungen(slash):
    _einstellen(slash)
    inter = _Inter(slash)

    async def lauf():
        await bot.loan_request.callback(inter, amount=1000, collateral=None)
        _einstellen(slash, zins_prozent=90)
        eigen = _Inter(slash)
        await inter.response.view.annehmen.callback(eigen)
        return eigen
    eigen = asyncio.run(lauf())
    assert "geändert" in eigen.text() and bot.db.kredit_liste(G_A, U) == []


def test_slash_englisch(slash):
    _einstellen(slash, strafen={"pfaendung": True})
    inter = _Inter(slash, locale=bot.discord.Locale.american_english)
    asyncio.run(bot.loan_request.callback(inter, amount=1000, collateral=None))
    t = inter.text()
    assert "Loan contract" in t and "seizure" in t and "Kreditvertrag" not in t and "Pfändung" not in t


def test_slash_pay_status_und_forgive(slash):
    sicht = _einstellen(slash)
    assert "keinen offenen" in _lauf_cmd(bot.loan_pay, _Inter(slash), amount=10)
    assert "keine Kredite" in _lauf_cmd(bot.loan_status, _Inter(slash))
    kid = _annehmen(slash, sicht, 1000)["kredit"]["id"]
    assert "zurückgezahlt" in _lauf_cmd(bot.loan_pay, _Inter(slash), amount=300) and bot.db.kredit_holen(kid)["bezahlt"] == 300
    assert f"#{kid}" in _lauf_cmd(bot.loan_status, _Inter(slash)) and "800" in _lauf_cmd(bot.loan_status, _Inter(slash))
    bot.db.set_wallet(G_A, U, 1)
    assert "Wallet" in _lauf_cmd(bot.loan_pay, _Inter(slash), amount=500) and bot.db.kredit_holen(kid)["bezahlt"] == 300


def test_slash_forgive_nur_fuer_admins(slash, monkeypatch):
    sicht = _einstellen(slash)
    kid = _annehmen(slash, sicht, 1000)["kredit"]["id"]
    ziel = type("M", (), {"id": U, "mention": "<@555>"})()

    async def verweigert(interaction):
        interaction.response.sent.append({"content": "verweigert", "embed": None})
    monkeypatch.setattr(bot, "_is_economy_admin", lambda inter: False)
    monkeypatch.setattr(bot, "_deny", verweigert)
    assert "verweigert" in _lauf_cmd(bot.loan_forgive, _Inter(slash, uid=ANDERER), user=ziel) and bot.db.kredit_holen(kid)["status"] == "offen"
    monkeypatch.setattr(bot, "_is_economy_admin", lambda inter: True)
    assert "1 Kredit" in _lauf_cmd(bot.loan_forgive, _Inter(slash, uid=ANDERER), user=ziel) and bot.db.kredit_holen(kid)["status"] == "erlassen"


def test_slash_ohne_premium(slash, monkeypatch):
    monkeypatch.setattr(bot, "_conn_of", lambda inter: None)
    for befehl, kw in ((bot.loan_request, {"amount": 1, "collateral": None}), (bot.loan_pay, {"amount": 1}), (bot.loan_status, {})):
        assert "Premium" in _lauf_cmd(befehl, _Inter(slash), **kw)


def test_slash_gruppe_registriert():
    assert "loan" in {c.name for c in bot.bot.tree.get_commands()}
    assert {c.name for c in bot.loan_group.commands} == {"request", "pay", "status", "forgive"}
