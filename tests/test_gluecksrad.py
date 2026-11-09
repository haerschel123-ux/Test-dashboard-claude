"""Glücksrad: Einstellungen im Dashboard (Discord Management), Drehen im Spielerbereich und per /wheel.

Geprüft werden die Preis-Prüfung, die gewichtete Ziehung, die Datenbank (Drehungen, Gratis-Zeit, Spielzeit-Gutschrift,
Verlauf), jede Art der Drehung samt Rückgabe bei Fehlern, die Dashboard- und Spieler-API samt Rechten, die
Mandanten-Trennung und die Slash-Befehle. Kein Test geht ins Internet oder zu Discord.

    python3 -m pytest tests/test_gluecksrad.py -q
"""
import asyncio
import json
import os
import sys
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_spieler_dashboard import G_A, G_B, U, ANDERER, ROLLE_VIP, _session, api, welt  # noqa: F401 - Fixtures
from test_airstrike import servers  # noqa: F401 - Fixture (zwei Server 1000/2000)

STUNDE = 3600


@pytest.fixture(autouse=True)
def _frisches_audit_log(monkeypatch):
    monkeypatch.setattr(bot, "_audit_log", bot.deque(maxlen=100))


@pytest.fixture
def db(tmp_path):
    return bot.EconomyDB(str(tmp_path / "r.db"))


def _preis(**kw):
    p = {"typ": "geld", "name": "Bargeld", "gewicht": 10, "betrag": 500}
    p.update(kw)
    return p


def _einstellen(welt, preise, gid=G_A, conn="a", **kw):
    """Speichert Einstellungen direkt (wie das Dashboard), setzt das Wallet von U auf 0 (die Datenbank vergibt ein Startguthaben)
    und gibt die Sicht zurück."""
    c = welt[conn]
    bot.db.set_wallet(gid, U, 0)
    daten = {"enabled": True, "kosten": 0, "gratis_stunden": 0, "spielzeit_stunden": 0, "max_gespeichert": 10,
             "preise": [dict(p, id=p.get("id") or f"{i:08x}") for i, p in enumerate(preise)]}
    daten.update(kw)
    sicht = bot.guild_sicht(c, gid)
    bot._conn_store(sicht, "gluecksrad", daten)
    return sicht


def _drehen(welt, sicht, quelle, uid=U, gid=G_A, conn="a"):
    return asyncio.run(bot._rad_drehen(welt[conn], sicht, welt["guilds"][gid], gid, uid, "User", quelle))


# ── Preis-Prüfung ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("roh", [
    {"typ": "geld", "name": "A", "gewicht": 1, "betrag": 1},
    {"typ": "geld", "name": "A", "gewicht": 1000, "betrag": 1_000_000_000},
    {"typ": "item", "name": "Kiste", "gewicht": 5, "item": "Kiste", "menge": 3},
    {"typ": "item", "name": "Kiste", "gewicht": 5, "item": "Kiste"},
    {"typ": "rolle", "name": "VIP", "gewicht": 5, "role_id": "123"},
    {"typ": "rolle", "name": "VIP", "gewicht": 5, "role_id": 123},
    {"typ": "drehung", "name": "Nochmal", "gewicht": 5, "drehungen": 2},
    {"typ": "drehung", "name": "Nochmal", "gewicht": 5},
    {"typ": "niete", "name": "Leider nichts", "gewicht": 5},
    {"typ": "geld", "name": "  Viel   Geld  ", "gewicht": 1.0, "betrag": 5.0}])
def test_preis_gueltig(roh):
    p = bot._rad_preis_pruefen(roh, id_erzeugen=True)
    assert bot._RAD_ID_RE.fullmatch(p["id"]) and p["typ"] == roh["typ"] and p["gewicht"] == int(roh["gewicht"])
    assert p["name"] == " ".join(roh["name"].split())


@pytest.mark.parametrize("roh", [
    None, [], "x", 5, {},
    {"typ": "unbekannt", "name": "A", "gewicht": 1},
    {"typ": "geld", "name": "", "gewicht": 1, "betrag": 1},
    {"typ": "geld", "name": "   ", "gewicht": 1, "betrag": 1},
    {"typ": "geld", "name": "x" * 41, "gewicht": 1, "betrag": 1},
    {"typ": "geld", "name": "a\x00b", "gewicht": 1, "betrag": 1},
    {"typ": "geld", "name": "a\nb‮c", "gewicht": 1, "betrag": 1},
    {"typ": "geld", "name": 5, "gewicht": 1, "betrag": 1},
    {"typ": "geld", "name": "A", "gewicht": 0, "betrag": 1},
    {"typ": "geld", "name": "A", "gewicht": 1001, "betrag": 1},
    {"typ": "geld", "name": "A", "gewicht": -1, "betrag": 1},
    {"typ": "geld", "name": "A", "gewicht": 1.5, "betrag": 1},
    {"typ": "geld", "name": "A", "gewicht": True, "betrag": 1},
    {"typ": "geld", "name": "A", "gewicht": "5", "betrag": 1},
    {"typ": "geld", "name": "A", "gewicht": 1},
    {"typ": "geld", "name": "A", "gewicht": 1, "betrag": 0},
    {"typ": "geld", "name": "A", "gewicht": 1, "betrag": -5},
    {"typ": "geld", "name": "A", "gewicht": 1, "betrag": 1_000_000_001},
    {"typ": "geld", "name": "A", "gewicht": 1, "betrag": "9"},
    {"typ": "item", "name": "A", "gewicht": 1},
    {"typ": "item", "name": "A", "gewicht": 1, "item": "  "},
    {"typ": "item", "name": "A", "gewicht": 1, "item": "x" * 81},
    {"typ": "item", "name": "A", "gewicht": 1, "item": "Kiste", "menge": 0},
    {"typ": "item", "name": "A", "gewicht": 1, "item": "Kiste", "menge": 51},
    {"typ": "rolle", "name": "A", "gewicht": 1},
    {"typ": "rolle", "name": "A", "gewicht": 1, "role_id": "abc"},
    {"typ": "rolle", "name": "A", "gewicht": 1, "role_id": "0"},
    {"typ": "rolle", "name": "A", "gewicht": 1, "role_id": "-5"},
    {"typ": "rolle", "name": "A", "gewicht": 1, "role_id": True},
    {"typ": "rolle", "name": "A", "gewicht": 1, "role_id": "1" * 21},
    {"typ": "drehung", "name": "A", "gewicht": 1, "drehungen": 0},
    {"typ": "drehung", "name": "A", "gewicht": 1, "drehungen": 11}])
def test_preis_ungueltig(roh):
    with pytest.raises(ValueError):
        bot._rad_preis_pruefen(roh, id_erzeugen=True)


def test_preis_kennung_pflicht_beim_laden_und_formatpruefung():
    with pytest.raises(ValueError):
        bot._rad_preis_pruefen(_preis())                              # Laden: ohne Kennung kaputt
    for schlecht in ("zu_kurz", "ABCDEF12", "0123456789", "../../x1", 5):
        with pytest.raises(ValueError):
            bot._rad_preis_pruefen(_preis(id=schlecht))
    assert bot._rad_preis_pruefen(_preis(id="0123abcd"))["id"] == "0123abcd"


def test_preis_ist_in_fester_form_und_fremde_felder_fallen_weg():
    p = bot._rad_preis_pruefen(_preis(id="0123abcd", boese="<script>", role_id="5", item="X"))
    assert set(p) == {"id", "name", "typ", "gewicht", "betrag"}


# ── Einstellungen laden ─────────────────────────────────────────────────────
class _Sicht(dict):
    pass


def test_einstellungen_vorgaben_und_kaputte_werte():
    assert bot._rad_einstellungen(_Fake({})) == {
        "enabled": False, "kosten": 0, "gratis_stunden": 0, "spielzeit_stunden": 0, "max_gespeichert": 10, "preise": []}
    kaputt = _Fake({"gluecksrad": {"enabled": 1, "kosten": -5, "gratis_stunden": "x", "spielzeit_stunden": 99999,
                                   "max_gespeichert": 0, "preise": [_preis(id="0123abcd"), {"typ": "x"}, 5, None]}})
    e = bot._rad_einstellungen(kaputt)
    assert e["enabled"] is True and e["kosten"] == 0 and e["gratis_stunden"] == 0 and e["spielzeit_stunden"] == 0
    assert e["max_gespeichert"] == 10 and [p["id"] for p in e["preise"]] == ["0123abcd"]
    assert bot._rad_einstellungen(_Fake({"gluecksrad": "kaputt"}))["preise"] == []
    assert bot._rad_einstellungen(_Fake({"gluecksrad": {"preise": "x"}}))["preise"] == []


def test_einstellungen_laden_hoechstens_20_preise():
    preise = [_preis(id=f"{i:08x}") for i in range(30)]
    assert len(bot._rad_einstellungen(_Fake({"gluecksrad": {"preise": preise}}))["preise"]) == 20


class _Fake:
    def __init__(self, d):
        self.d = d

    def get(self, key, default=None):
        return self.d.get(key, default)


# ── Chancen und Ziehung ─────────────────────────────────────────────────────
def test_chancen_summe_und_null():
    ps = [{"gewicht": 1}, {"gewicht": 3}, {"gewicht": 6}]
    assert bot._rad_chancen(ps) == [10.0, 30.0, 60.0]
    assert bot._rad_chancen([]) == []
    assert abs(sum(bot._rad_chancen([{"gewicht": 1}] * 3)) - 100) < 0.05


@pytest.mark.parametrize("wurf,erwartet", [(0, "a"), (1, "b"), (3, "b"), (4, "c"), (9, "c")])
def test_ziehung_grenzen(monkeypatch, wurf, erwartet):
    ps = [{"id": "a", "gewicht": 1}, {"id": "b", "gewicht": 3}, {"id": "c", "gewicht": 6}]
    monkeypatch.setattr(bot.secrets, "randbelow", lambda n: (wurf if n == 10 else pytest.fail(n)))
    assert bot._rad_ziehen(ps)["id"] == erwartet


def test_ziehung_verteilung_echter_zufall():
    ps = [{"id": "a", "gewicht": 1}, {"id": "b", "gewicht": 3}]
    n = 4000
    a = sum(1 for _ in range(n) if bot._rad_ziehen(ps)["id"] == "a")
    assert 800 < a < 1200            # erwartet 1000, Streuung ~27


def test_ziehung_gewicht_eins_trifft_immer():
    assert bot._rad_ziehen([{"id": "x", "gewicht": 1}])["id"] == "x"


# ── Datenbank ───────────────────────────────────────────────────────────────
def test_db_startwerte_und_trennung(db):
    assert db.rad_spieler(1, 5) == {"drehungen": 0, "letzter_gratis": 0.0, "spielzeit_basis": None}
    db.rad_drehungen_aendern(1, 5, 3, 10)
    assert db.rad_spieler(1, 5)["drehungen"] == 3
    assert db.rad_spieler(2, 5)["drehungen"] == 0 and db.rad_spieler(1, 6)["drehungen"] == 0


def test_db_drehungen_grenzen(db):
    assert db.rad_drehungen_aendern(1, 5, 4, 10) == 4
    assert db.rad_drehungen_aendern(1, 5, 100, 10) == 10          # Obergrenze
    assert db.rad_drehungen_aendern(1, 5, -3, 10) == 7
    assert db.rad_drehungen_aendern(1, 5, -100, 10) == 0          # nie unter 0
    db.rad_drehungen_aendern(1, 5, 5, 10)
    assert db.rad_drehungen_aendern(1, 5, 0, 3) == 3              # niedrigeres Maximum kappt


def test_db_verbrauchen_atomar(db):
    assert db.rad_verbrauche(1, 5) is False and db.rad_spieler(1, 5)["drehungen"] == 0
    db.rad_drehungen_aendern(1, 5, 2, 10)
    assert db.rad_verbrauche(1, 5) is True and db.rad_verbrauche(1, 5) is True and db.rad_verbrauche(1, 5) is False
    assert db.rad_spieler(1, 5)["drehungen"] == 0


def test_db_gratis_wartezeit(db):
    jetzt = 1_000_000.0
    assert db.rad_gratis_beanspruchen(1, 5, 2, jetzt) == 0.0                    # erste ist sofort bereit
    assert db.rad_gratis_beanspruchen(1, 5, 2, jetzt + 1) is None
    assert db.rad_gratis_beanspruchen(1, 5, 2, jetzt + 2 * STUNDE - 1) is None
    assert db.rad_gratis_beanspruchen(1, 5, 2, jetzt + 2 * STUNDE) == jetzt     # genau nach X Stunden wieder
    assert db.rad_spieler(1, 5)["letzter_gratis"] == jetzt + 2 * STUNDE


def test_db_gratis_zuruecksetzen_gibt_sie_zurueck(db):
    jetzt = 1_000_000.0
    vorher = db.rad_gratis_beanspruchen(1, 5, 24, jetzt)
    db.rad_gratis_zuruecksetzen(1, 5, vorher)
    assert db.rad_gratis_beanspruchen(1, 5, 24, jetzt + 1) == 0.0


def test_db_gratis_je_spieler_und_guild(db):
    jetzt = 1_000_000.0
    db.rad_gratis_beanspruchen(1, 5, 24, jetzt)
    assert db.rad_gratis_beanspruchen(1, 6, 24, jetzt) is not None
    assert db.rad_gratis_beanspruchen(2, 5, 24, jetzt) is not None


def test_db_spielzeit_erster_aufruf_legt_nur_den_ausgangsstand_fest(db):
    assert db.rad_spielzeit_gutschreiben(1, 5, 10 * STUNDE, 2, 10) == 0       # frueher gespielt zaehlt nicht
    s = db.rad_spieler(1, 5)
    assert s["drehungen"] == 0 and s["spielzeit_basis"] == 10 * STUNDE
    assert db.rad_spielzeit_gutschreiben(1, 5, 11 * STUNDE, 2, 10) == 0       # erst 1 h
    assert db.rad_spielzeit_gutschreiben(1, 5, 12 * STUNDE, 2, 10) == 1
    assert db.rad_spieler(1, 5) == {"drehungen": 1, "letzter_gratis": 0.0, "spielzeit_basis": 12 * STUNDE}


def test_db_spielzeit_rest_geht_nicht_verloren_und_mehrere_auf_einmal(db):
    db.rad_spielzeit_gutschreiben(1, 5, 0, 2, 10)
    assert db.rad_spielzeit_gutschreiben(1, 5, 5 * STUNDE + 100, 2, 10) == 2    # 5 h = 2 Schritte, 1 h + 100 s Rest
    assert db.rad_spieler(1, 5)["spielzeit_basis"] == 4 * STUNDE
    assert db.rad_spielzeit_gutschreiben(1, 5, 6 * STUNDE, 2, 10) == 1          # Rest zaehlt mit
    assert db.rad_spieler(1, 5)["drehungen"] == 3


def test_db_spielzeit_nicht_doppelt_gutgeschrieben(db):
    db.rad_spielzeit_gutschreiben(1, 5, 0, 1, 10)
    assert db.rad_spielzeit_gutschreiben(1, 5, 3 * STUNDE, 1, 10) == 3
    assert db.rad_spielzeit_gutschreiben(1, 5, 3 * STUNDE, 1, 10) == 0
    assert db.rad_spieler(1, 5)["drehungen"] == 3


def test_db_spielzeit_obergrenze_ueberschuss_verfaellt(db):
    db.rad_spielzeit_gutschreiben(1, 5, 0, 1, 4)
    assert db.rad_spielzeit_gutschreiben(1, 5, 9 * STUNDE, 1, 4) == 4
    assert db.rad_spieler(1, 5)["drehungen"] == 4
    assert db.rad_spieler(1, 5)["spielzeit_basis"] == 9 * STUNDE                # Rest ueber dem Maximum verfaellt
    assert db.rad_spielzeit_gutschreiben(1, 5, 9 * STUNDE + 100, 1, 4) == 0


def test_db_spielzeit_voller_speicher_schreibt_nichts_gut(db):
    db.rad_spielzeit_gutschreiben(1, 5, 0, 1, 2)
    db.rad_drehungen_aendern(1, 5, 2, 2)
    assert db.rad_spielzeit_gutschreiben(1, 5, 3 * STUNDE, 1, 2) == 0
    assert db.rad_spieler(1, 5)["drehungen"] == 2


def test_db_spielzeit_gesamtzeit_sinkt_setzt_neuen_ausgangsstand(db):
    db.rad_spielzeit_gutschreiben(1, 5, 10 * STUNDE, 1, 10)
    assert db.rad_spielzeit_gutschreiben(1, 5, 2 * STUNDE, 1, 10) == 0         # anderer Name verknuepft
    assert db.rad_spieler(1, 5)["spielzeit_basis"] == 2 * STUNDE
    assert db.rad_spielzeit_gutschreiben(1, 5, 3 * STUNDE, 1, 10) == 1


@pytest.mark.parametrize("stunden", [0, -1])
def test_db_spielzeit_ohne_schritt_tut_nichts(db, stunden):
    assert db.rad_spielzeit_gutschreiben(1, 5, 5 * STUNDE, stunden, 10) == 0
    assert db.rad_spieler(1, 5)["spielzeit_basis"] is None


def test_db_verlauf_reihenfolge_und_trennung(db):
    for i in range(3):
        db.rad_log_add(1, 5, f"p{i}", f"Preis {i}", "geld", str(i), "gratis", 0)
    db.rad_log_add(1, 6, "x", "Fremd", "geld", "", "gratis", 0)
    db.rad_log_add(2, 5, "y", "Andere Guild", "geld", "", "gratis", 0)
    zeilen = db.rad_log(1, 5, 10)
    assert [z["preis_name"] for z in zeilen] == ["Preis 2", "Preis 1", "Preis 0"]
    assert [z["preis_name"] for z in db.rad_log(1, 5, 2)] == ["Preis 2", "Preis 1"]
    assert {"preis_name", "typ", "wert", "quelle", "kosten", "ts"} <= set(zeilen[0])


def test_db_verlauf_behaelt_nur_200_je_spieler(db):
    for i in range(210):
        db.rad_log_add(1, 5, "p", f"n{i}", "geld", "", "gratis", 0)
    db.rad_log_add(1, 6, "p", "anderer", "geld", "", "gratis", 0)
    zeilen = db.rad_log(1, 5, 500)
    assert len(zeilen) == 200 and zeilen[0]["preis_name"] == "n209" and zeilen[-1]["preis_name"] == "n10"
    assert len(db.rad_log(1, 6, 5)) == 1


def test_db_sql_kann_nicht_ueber_texte_eingeschleust_werden(db):
    db.rad_log_add(1, 5, "p", "x'); DROP TABLE rad_log;--", "geld", "' OR 1=1 --", "gratis", 0)
    assert db.rad_log(1, 5, 5)[0]["preis_name"] == "x'); DROP TABLE rad_log;--"


# ── Drehen ──────────────────────────────────────────────────────────────────
def test_aus_oder_ohne_preise_dreht_nicht(welt):
    sicht = _einstellen(welt, [_preis()], enabled=False, gratis_stunden=1)
    assert _drehen(welt, sicht, "gratis")["code"] == "aus"
    sicht = _einstellen(welt, [], enabled=True, gratis_stunden=1)
    assert _drehen(welt, sicht, "gratis")["code"] == "aus"
    assert bot.db.rad_log(G_A, U) == []


def test_ungueltige_quelle(welt):
    sicht = _einstellen(welt, [_preis()], gratis_stunden=1)
    for q in ("", "x", "GRATIS", None, "gratis; drop"):
        assert _drehen(welt, sicht, q)["code"] == "quelle"
    assert bot.db.rad_spieler(G_A, U)["letzter_gratis"] == 0.0


def test_gespeicherte_drehung_wird_verbraucht_und_geld_ausgezahlt(welt):
    sicht = _einstellen(welt, [_preis(betrag=500)])
    assert _drehen(welt, sicht, "gespeichert")["code"] == "keine"
    bot.db.rad_drehungen_aendern(G_A, U, 2, 10)
    vor = bot.db.get_balance(G_A, U)[0]
    erg = _drehen(welt, sicht, "gespeichert")
    assert erg["ok"] and erg["drehungen"] == 1 and erg["wallet"] == vor + 500
    assert erg["preis"]["typ"] == "geld" and bot.db.rad_spieler(G_A, U)["drehungen"] == 1
    z = bot.db.rad_log(G_A, U)[0]
    assert (z["preis_name"], z["typ"], z["wert"], z["quelle"], z["kosten"]) == ("Bargeld", "geld", "500", "gespeichert", 0)
    assert welt["feeds"] == [(G_A, "economy_log", "1000")]


def test_gratis_nicht_eingestellt_bereit_und_warten(welt):
    sicht = _einstellen(welt, [_preis()], gratis_stunden=0, kosten=100)
    assert _drehen(welt, sicht, "gratis")["code"] == "keine"
    sicht = _einstellen(welt, [_preis()], gratis_stunden=24)
    assert _drehen(welt, sicht, "gratis")["ok"] is True
    zweite = _drehen(welt, sicht, "gratis")
    assert zweite["ok"] is False and zweite["code"] == "warten"
    assert len(bot.db.rad_log(G_A, U)) == 1


def test_gratis_je_spieler_getrennt(welt):
    sicht = _einstellen(welt, [_preis()], gratis_stunden=24)
    assert _drehen(welt, sicht, "gratis", uid=U)["ok"] and _drehen(welt, sicht, "gratis", uid=ANDERER)["ok"]


def test_bezahlte_drehung_bucht_ab_und_zahlt_aus(welt):
    sicht = _einstellen(welt, [_preis(betrag=300)], kosten=100)
    assert _drehen(welt, sicht, "bezahlt")["code"] == "guthaben"
    assert bot.db.get_balance(G_A, U)[0] == 0 and bot.db.rad_log(G_A, U) == []
    bot.db.add_wallet(G_A, U, 250)
    erg = _drehen(welt, sicht, "bezahlt")
    assert erg["ok"] and erg["wallet"] == 250 - 100 + 300
    z = bot.db.rad_log(G_A, U)[0]
    assert z["quelle"] == "bezahlt" and z["kosten"] == 100


def test_bezahlte_drehung_ohne_kosten_eingestellt(welt):
    sicht = _einstellen(welt, [_preis()], kosten=0, gratis_stunden=1)
    bot.db.add_wallet(G_A, U, 1000)
    assert _drehen(welt, sicht, "bezahlt")["code"] == "keine"
    assert bot.db.get_balance(G_A, U)[0] == 1000


def test_niete_zahlt_nichts_und_postet_nichts(welt):
    sicht = _einstellen(welt, [{"typ": "niete", "name": "Leer", "gewicht": 1}], gratis_stunden=1)
    erg = _drehen(welt, sicht, "gratis")
    assert erg["ok"] and erg["preis"]["typ"] == "niete" and welt["feeds"] == []
    assert bot.db.rad_log(G_A, U)[0]["typ"] == "niete"


def test_preis_drehung_schreibt_zusaetzliche_drehungen_gut_mit_obergrenze(welt):
    sicht = _einstellen(welt, [{"typ": "drehung", "name": "+3", "gewicht": 1, "drehungen": 3}], gratis_stunden=1, max_gespeichert=2)
    erg = _drehen(welt, sicht, "gratis")
    assert erg["ok"] and erg["drehungen"] == 2                                 # auf Obergrenze gekappt


def test_preis_rolle(welt, monkeypatch):
    aufrufe = []

    async def geben(guild, uid, rid, grund):
        aufrufe.append((guild.id, uid, rid, grund))
        return None
    monkeypatch.setattr(bot, "_rolle_geben", geben)
    sicht = _einstellen(welt, [{"typ": "rolle", "name": "Gewinner", "gewicht": 1, "role_id": str(ROLLE_VIP)}], gratis_stunden=1)
    erg = _drehen(welt, sicht, "gratis")
    assert erg["ok"] and aufrufe == [(G_A, U, ROLLE_VIP, "Glücksrad")]


def test_preis_rolle_fehlschlag_gibt_die_drehung_zurueck(welt, monkeypatch):
    async def geben(guild, uid, rid, grund):
        return ("Die Rolle liegt über der Bot-Rolle.", "The role is above the bot role.")
    monkeypatch.setattr(bot, "_rolle_geben", geben)
    sicht = _einstellen(welt, [{"typ": "rolle", "name": "G", "gewicht": 1, "role_id": str(ROLLE_VIP)}], gratis_stunden=24, kosten=50)
    # gratis
    erg = _drehen(welt, sicht, "gratis")
    assert erg["ok"] is False and erg["code"] == "auszahlung" and "Bot-Rolle" in erg["de"] and "returned" in erg["en"]
    assert bot.db.rad_spieler(G_A, U)["letzter_gratis"] == 0.0 and _drehen(welt, sicht, "gratis")["code"] == "auszahlung"
    # gespeichert
    bot.db.rad_drehungen_aendern(G_A, U, 1, 10)
    assert _drehen(welt, sicht, "gespeichert")["ok"] is False and bot.db.rad_spieler(G_A, U)["drehungen"] == 1
    # bezahlt
    bot.db.add_wallet(G_A, U, 200)
    assert _drehen(welt, sicht, "bezahlt")["ok"] is False and bot.db.get_balance(G_A, U)[0] == 200
    assert bot.db.rad_log(G_A, U) == [] and welt["feeds"] == []


def test_auszahlung_wirft_ausnahme_nichts_geht_verloren(welt, monkeypatch):
    async def kaputt(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(bot, "_rad_preis_auszahlen", kaputt)
    sicht = _einstellen(welt, [_preis()], kosten=100)
    bot.db.add_wallet(G_A, U, 100)
    erg = _drehen(welt, sicht, "bezahlt")
    assert erg["ok"] is False and erg["code"] == "auszahlung" and bot.db.get_balance(G_A, U)[0] == 100


def _item_preis(**kw):
    return dict({"typ": "item", "name": "Kiste", "gewicht": 1, "item": "Kiste", "menge": 2}, **kw)


def test_preis_item_wird_wie_ein_gratis_kauf_am_letzten_ort_geliefert(welt, monkeypatch):
    monkeypatch.setattr(bot, "_ort_letzter_bekannter", lambda c, g, u: {"x": 4640.0, "z": 10350.0, "ts": time.time()})
    sicht = _einstellen(welt, [_item_preis()], gratis_stunden=24)
    bot.db.add_wallet(G_A, U, 70)
    erg = _drehen(welt, sicht, "gratis")
    assert erg["ok"], erg
    assert welt["a"].shop.aufrufe == [(4640.0, 0.0, 10350.0)] and welt["b"].shop.aufrufe == []
    assert bot.db.get_balance(G_A, U)[0] == 70                                  # nichts abgebucht
    kauf = bot.db.purchases_of_user("1000", G_A, U, 5)[0]
    assert (kauf["item_name"], kauf["amount"], kauf["total_price"], kauf["status"]) == ("Kiste", 2, 0, "pending")


def test_preis_item_ohne_ort_gibt_den_geldwert(welt, monkeypatch):
    monkeypatch.setattr(bot, "_ort_letzter_bekannter", lambda c, g, u: None)
    sicht = _einstellen(welt, [_item_preis()], gratis_stunden=24)
    erg = _drehen(welt, sicht, "gratis")
    assert erg["ok"] and bot.db.get_balance(G_A, U)[0] == 200 and welt["a"].shop.aufrufe == []
    assert bot.db.rad_log(G_A, U)[0]["wert"] == "ersatz:200"


def test_preis_item_ohne_ort_und_ohne_wert_scheitert_und_gibt_zurueck(welt, monkeypatch):
    monkeypatch.setattr(bot, "_ort_letzter_bekannter", lambda c, g, u: None)
    welt["a"]._catalog.items.append({"name": "Gratis", "classname": "WoodenCrate", "price": 0, "enabled": True})
    sicht = _einstellen(welt, [_item_preis(item="Gratis", name="Gratis")], gratis_stunden=24)
    erg = _drehen(welt, sicht, "gratis")
    assert erg["ok"] is False and "/link" in erg["de"] and bot.db.rad_spieler(G_A, U)["letzter_gratis"] == 0.0


def test_preis_item_nicht_mehr_im_katalog(welt, monkeypatch):
    monkeypatch.setattr(bot, "_ort_letzter_bekannter", lambda c, g, u: {"x": 1.0, "z": 2.0, "ts": 0})
    sicht = _einstellen(welt, [_item_preis(item="Gibt es nicht")], gratis_stunden=24)
    erg = _drehen(welt, sicht, "gratis")
    assert erg["ok"] is False and "Katalog" in erg["de"] and welt["a"].shop.aufrufe == []
    assert bot.db.rad_spieler(G_A, U)["letzter_gratis"] == 0.0


def test_preis_item_lieferung_scheitert_nichts_verbraucht(welt, monkeypatch):
    monkeypatch.setattr(bot, "_ort_letzter_bekannter", lambda c, g, u: {"x": 1.0, "z": 2.0, "ts": 0})
    welt["a"].ftp = None                                                         # ohne FTP kann nichts geliefert werden
    sicht = _einstellen(welt, [_item_preis()], kosten=100)
    bot.db.add_wallet(G_A, U, 100)
    erg = _drehen(welt, sicht, "bezahlt")
    assert erg["ok"] is False and bot.db.get_balance(G_A, U)[0] == 100 and bot.db.purchases_of_user("1000", G_A, U, 5) == []


def test_zwei_gleichzeitige_drehungen_mit_einer_gespeicherten(welt):
    sicht = _einstellen(welt, [_preis()])
    bot.db.rad_drehungen_aendern(G_A, U, 1, 10)

    async def beide():
        g = welt["guilds"][G_A]
        return await asyncio.gather(*(bot._rad_drehen(welt["a"], sicht, g, G_A, U, "User", "gespeichert") for _ in range(5)))
    ergebnisse = asyncio.run(beide())
    assert sum(1 for e in ergebnisse if e["ok"]) == 1 and len(bot.db.rad_log(G_A, U)) == 1
    assert bot.db.get_balance(G_A, U)[0] == 500


def test_gleichzeitige_bezahlte_drehungen_mit_fehlschlagender_auszahlung_bleiben_nacheinander(welt, monkeypatch):
    """Zwei Klicks gleichzeitig, Guthaben nur fuer eine Drehung, Auszahlung scheitert nach einer Pause: ohne die Sperre je
    Spieler sähe die zweite Drehung das Geld in der Schwebe und meldete faelschlich 'zu wenig Guthaben'."""
    async def geben(guild, uid, rid, grund):
        await asyncio.sleep(0.01)
        return ("Rolle nicht vergebbar.", "Role not assignable.")
    monkeypatch.setattr(bot, "_rolle_geben", geben)
    sicht = _einstellen(welt, [{"typ": "rolle", "name": "G", "gewicht": 1, "role_id": str(ROLLE_VIP)}], kosten=100)
    bot.db.add_wallet(G_A, U, 100)

    async def beide():
        g = welt["guilds"][G_A]
        return await asyncio.gather(*(bot._rad_drehen(welt["a"], sicht, g, G_A, U, "User", "bezahlt") for _ in range(2)))
    ergebnisse = asyncio.run(beide())
    assert [e["code"] for e in ergebnisse] == ["auszahlung", "auszahlung"] and bot.db.get_balance(G_A, U)[0] == 100


def test_zwei_gleichzeitige_gratis_drehungen(welt):
    sicht = _einstellen(welt, [_preis()], gratis_stunden=24)

    async def beide():
        g = welt["guilds"][G_A]
        return await asyncio.gather(*(bot._rad_drehen(welt["a"], sicht, g, G_A, U, "User", "gratis") for _ in range(5)))
    assert sum(1 for e in asyncio.run(beide()) if e["ok"]) == 1


def test_spielzeit_gutschrift_beim_drehen_und_status(welt):
    bot.db.link_user(G_A, U, "Spieler1")
    bot.db.roster_add_playtime(G_A, "1000", "Spieler1", 50 * STUNDE)               # frueher gespielt: zaehlt nicht
    sicht = _einstellen(welt, [_preis()], spielzeit_stunden=2)
    s = bot._rad_status(welt["a"], sicht, welt["guilds"][G_A], G_A, U, False)
    assert s["drehungen"] == 0 and s["spielzeit"] == {"stunden": 2, "fortschritt": 0, "verknuepft": True}
    assert _drehen(welt, sicht, "gespeichert")["code"] == "keine"
    bot.db.roster_add_playtime(G_A, "1000", "Spieler1", 5 * STUNDE + 600)           # 2 Schritte + 1 h 10 min Rest
    s = bot._rad_status(welt["a"], sicht, welt["guilds"][G_A], G_A, U, False)
    assert s["drehungen"] == 2 and s["spielzeit"]["fortschritt"] == STUNDE + 600
    assert _drehen(welt, sicht, "gespeichert")["ok"] and _drehen(welt, sicht, "gespeichert")["ok"]
    assert _drehen(welt, sicht, "gespeichert")["code"] == "keine"


def test_spielzeit_gutschrift_dreht_auch_ohne_vorherigen_status_aufruf(welt):
    bot.db.link_user(G_A, U, "Spieler1")
    sicht = _einstellen(welt, [_preis()], spielzeit_stunden=1)
    _drehen(welt, sicht, "gespeichert")                                            # legt den Ausgangsstand fest
    bot.db.roster_add_playtime(G_A, "1000", "Spieler1", 3 * STUNDE)
    assert _drehen(welt, sicht, "gespeichert")["ok"]
    assert bot.db.rad_spieler(G_A, U)["drehungen"] == 2


def test_spielzeit_ohne_verknuepfung_und_anderer_server_zaehlt_nicht(welt):
    sicht = _einstellen(welt, [_preis()], spielzeit_stunden=1)
    s = bot._rad_status(welt["a"], sicht, welt["guilds"][G_A], G_A, U, False)
    assert s["spielzeit"] == {"stunden": 1, "fortschritt": None, "verknuepft": False}
    bot.db.link_user(G_A, U, "Spieler1")
    bot._rad_status(welt["a"], sicht, welt["guilds"][G_A], G_A, U, False)
    bot.db.roster_add_playtime(G_A, "2000", "Spieler1", 9 * STUNDE)               # Zeit auf einem anderen Server
    assert bot._rad_status(welt["a"], sicht, welt["guilds"][G_A], G_A, U, False)["drehungen"] == 0


# ── Status ──────────────────────────────────────────────────────────────────
def test_status_zeigt_nur_oeffentliche_angaben(welt):
    sicht = _einstellen(welt, [_preis(), _item_preis(), {"typ": "niete", "name": "Leer", "gewicht": 88}], gratis_stunden=6, kosten=100)
    s = bot._rad_status(welt["a"], sicht, welt["guilds"][G_A], G_A, U, False, jetzt=1_000_000.0)
    assert s["aktiv"] is True and s["drehungen"] == 0 and s["gratis"] == {"stunden": 6, "bereit": True, "in_sekunden": 0}
    assert s["kosten"] == 100 and [p["name"] for p in s["preise"]] == ["Bargeld", "Kiste", "Leer"]
    assert sum(p["chance"] for p in s["preise"]) == pytest.approx(100, abs=0.05)
    assert all(set(p) == {"id", "name", "typ", "chance", "text"} for p in s["preise"])
    assert "gewicht" not in json.dumps(s) and "betrag" not in json.dumps(s)


def test_status_wartezeit_laeuft_runter(welt):
    sicht = _einstellen(welt, [_preis()], gratis_stunden=2)
    bot.db.rad_gratis_beanspruchen(G_A, U, 2, 1_000_000.0)
    s = bot._rad_status(welt["a"], sicht, welt["guilds"][G_A], G_A, U, False, jetzt=1_000_000.0 + 1800)
    assert s["gratis"]["bereit"] is False and s["gratis"]["in_sekunden"] == 2 * STUNDE - 1800
    s = bot._rad_status(welt["a"], sicht, welt["guilds"][G_A], G_A, U, False, jetzt=1_000_000.0 + 3 * STUNDE)
    assert s["gratis"]["bereit"] is True and s["gratis"]["in_sekunden"] == 0


def test_status_aus_oder_ohne_preise_ist_inaktiv(welt):
    assert bot._rad_status(welt["a"], _einstellen(welt, [_preis()], enabled=False), welt["guilds"][G_A], G_A, U, False)["aktiv"] is False
    assert bot._rad_status(welt["a"], _einstellen(welt, []), welt["guilds"][G_A], G_A, U, False)["aktiv"] is False


def test_preis_texte_deutsch_und_englisch(welt):
    c = welt["a"]
    assert "💰" in bot._rad_preis_text({"typ": "geld", "betrag": 5}, c, False)
    assert bot._rad_preis_text({"typ": "item", "item": "Kiste", "menge": 3}, c, False) == "🎁 3× Kiste"
    assert bot._rad_preis_text({"typ": "drehung", "drehungen": 2}, c, False) == "🔄 +2 Drehung(en)"
    assert bot._rad_preis_text({"typ": "drehung", "drehungen": 2}, c, True) == "🔄 +2 spin(s)"
    assert bot._rad_preis_text({"typ": "niete"}, c, False) == "💨 Niete" and bot._rad_preis_text({"typ": "niete"}, c, True) == "💨 Nothing"
    assert bot._rad_preis_text({"typ": "rolle", "role_id": str(ROLLE_VIP)}, c, True, welt["guilds"][G_A]) == "🎭 Role VIP"
    assert bot._rad_preis_text({"typ": "rolle", "role_id": "999"}, c, False, welt["guilds"][G_A]) == "🎭 Rolle 999"


@pytest.mark.parametrize("sek,en,erwartet", [(30, False, "1 Min."), (30, True, "1 min"), (125 * 60, False, "2 Std. 5 Min."),
                                              (125 * 60, True, "2 h 5 min"), (3600, False, "1 Std. 0 Min."), (59 * 60, True, "59 min")])
def test_dauer_text(sek, en, erwartet):
    assert bot._rad_dauer(sek, en) == erwartet


# ── Spieler-API ─────────────────────────────────────────────────────────────
def _s():
    return _session(gid=G_A, sid_="1000")


def test_api_ohne_login_gesperrt(welt):
    assert api("GET", "/api/me/rad")[0] in (401, 403)
    assert api("POST", "/api/me/rad/drehen", None, {"quelle": "gratis"})[0] in (401, 403)


def test_api_ohne_gewaehlten_server(welt):
    assert api("GET", "/api/me/rad", _session())[0] == 409


def test_api_status_und_drehen(welt):
    _einstellen(welt, [_preis(betrag=400)], gratis_stunden=24, kosten=100)
    st, d = api("GET", "/api/me/rad", _s())
    assert st == 200 and d["data"]["aktiv"] and d["data"]["gratis"]["bereit"] and d["data"]["verlauf"] == []
    st, d = api("POST", "/api/me/rad/drehen", _s(), {"quelle": "gratis"})
    assert st == 200 and d["data"]["ok"] and d["data"]["preis"]["typ"] == "geld" and d["data"]["wallet"] == 400
    assert d["data"]["status"]["gratis"]["bereit"] is False and "400" in d["data"]["text"] and "💰" in d["data"]["preis"]["text_de"]
    st, d = api("POST", "/api/me/rad/drehen", _s(), {"quelle": "gratis"})
    assert st == 409 and d["code"] == "warten"
    st, d = api("GET", "/api/me/rad", _s())
    assert [v["quelle"] for v in d["data"]["verlauf"]] == ["gratis"]


def test_api_fehlercodes(welt):
    _einstellen(welt, [_preis()], gratis_stunden=24, kosten=100)
    assert api("POST", "/api/me/rad/drehen", _s(), {"quelle": "x"})[0] == 400
    assert api("POST", "/api/me/rad/drehen", _s(), {})[0] == 400
    assert api("POST", "/api/me/rad/drehen", _s(), {"quelle": "gespeichert"})[1]["code"] == "keine"
    st, d = api("POST", "/api/me/rad/drehen", _s(), {"quelle": "bezahlt"})
    assert st == 409 and d["code"] == "guthaben"
    assert api("POST", "/api/me/rad/drehen", _s(), [1, 2])[0] == 400


def test_api_aus_ist_409(welt):
    _einstellen(welt, [_preis()], enabled=False, gratis_stunden=24)
    assert api("POST", "/api/me/rad/drehen", _s(), {"quelle": "gratis"})[1]["code"] == "aus"


def test_api_englisch(welt):
    _einstellen(welt, [_preis()], gratis_stunden=24)
    s = _s()
    bot._SESS_STORE[s]["lang"] = "en"
    api("POST", "/api/me/rad/drehen", s, {"quelle": "gratis"})
    st, d = api("POST", "/api/me/rad/drehen", s, {"quelle": "gratis"})
    assert st == 409 and "not ready" in d["error"]


def test_api_rate_limit_greift(welt):
    _einstellen(welt, [_preis()], gratis_stunden=24)
    s = _s()

    async def lauf():
        async with TestClient(TestServer(bot.build_app())) as c:
            bot._DASH_RATE_LIMIT_LAST.clear()
            r1 = await c.post("/api/me/rad/drehen", json={"quelle": "gratis"}, cookies={bot._SESS_COOKIE: s})
            r2 = await c.post("/api/me/rad/drehen", json={"quelle": "gratis"}, cookies={bot._SESS_COOKIE: s})
            return r1.status, r2.status
    assert asyncio.run(lauf())[1] == 429


def test_mandanten_getrennt_einstellungen_drehungen_guthaben(welt):
    _einstellen(welt, [_preis(betrag=111)], gratis_stunden=24)                       # nur Guild A
    bot.db.rad_drehungen_aendern(G_A, U, 3, 10)
    sb = _session(gid=G_B, sid_="2000")
    st, d = api("GET", "/api/me/rad", sb)
    assert st == 200 and d["data"]["aktiv"] is False and d["data"]["preise"] == [] and d["data"]["drehungen"] == 0
    assert api("POST", "/api/me/rad/drehen", sb, {"quelle": "gespeichert"})[1]["code"] in ("aus", "keine")
    assert bot.db.rad_spieler(G_A, U)["drehungen"] == 3 and bot.db.get_balance(G_A, U)[0] == 0


def test_spieler_a_sieht_nie_den_verlauf_von_spieler_b(welt):
    _einstellen(welt, [_preis()], gratis_stunden=24)
    bot.db.rad_log_add(G_A, ANDERER, "p", "Geheim", "geld", "5", "gratis", 0)
    st, d = api("GET", "/api/me/rad", _s())
    assert st == 200 and "Geheim" not in json.dumps(d)


# ── Dashboard-API (Discord Management) ──────────────────────────────────────
def _admin(welt, gid=G_A, conn="a", admin=True, gast=None):
    """Dashboard-Anmeldung: Admin, Kunde (admin=False = Besitzer des Servers) oder Gast (gast = Discord-ID)."""
    sid = str(time.time_ns())
    c = welt[conn]
    bot._SESS_STORE[sid] = {"token": "x", "discord": {"id": gast or (sid if admin else str(c.data["owner_discord_id"]))},
                            "is_admin": admin and not gast, "service_id": c.service_id, "seen": time.time(),
                            "admin_geprueft_ts": time.time(), "guild_id": str(gid), **({"is_guest": True} if gast else {})}
    c.data["kunden_stufe"] = "premium"
    return sid


def _mgmt(welt, methode, pfad, daten=None, **kw):
    sid = _admin(welt, **kw)
    return api(methode, "/api/discord-management/gluecksrad" + pfad, sid, daten)


def _speichern(welt, preise, **kw):
    daten = {"enabled": True, "kosten": 0, "gratis_stunden": 24, "spielzeit_stunden": 0, "max_gespeichert": 5, "preise": preise}
    daten.update(kw)
    return _mgmt(welt, "POST", "", daten)


def test_mgmt_ohne_login_und_als_spieler_gesperrt(welt):
    assert api("GET", "/api/discord-management/gluecksrad")[0] in (401, 403)
    assert api("POST", "/api/discord-management/gluecksrad", None, {})[0] in (401, 403)
    s = _session(gid=G_A, sid_="1000")
    for methode, pfad in (("GET", ""), ("POST", ""), ("GET", "/items?q=ki"), ("POST", "/geben")):
        st, _d = api(methode, "/api/discord-management/gluecksrad" + pfad, s, {})
        assert st in (401, 403), (methode, pfad, st)


def test_mgmt_gast_nur_ansehen_darf_nichts_aendern(welt):
    welt["a"].data["dashboard_perms"] = {"user:900": {"discord_mgmt": ["view"]}}
    assert _mgmt(welt, "GET", "", gast="900")[0] == 200
    assert _mgmt(welt, "GET", "/items?q=ki", gast="900")[0] == 200
    assert _mgmt(welt, "POST", "", {"enabled": True, "preise": [_preis()], "gratis_stunden": 1}, gast="900")[0] == 403
    assert _mgmt(welt, "POST", "/geben", {"user_id": str(U), "anzahl": 1}, gast="900")[0] == 403
    assert bot._rad_einstellungen(bot.guild_sicht(welt["a"], G_A))["preise"] == [] and bot.db.rad_spieler(G_A, U)["drehungen"] == 0


def test_mgmt_gast_ohne_eintrag_sieht_nichts(welt):
    for methode, pfad in (("GET", ""), ("GET", "/items?q=ki"), ("POST", ""), ("POST", "/geben")):
        assert _mgmt(welt, methode, pfad, {"enabled": False, "preise": []}, gast="900")[0] == 403


def test_mgmt_kunde_besitzer_darf_auf_seinem_server(welt):
    st, d = _mgmt(welt, "POST", "", {"enabled": True, "kosten": 10, "preise": [_preis()]}, admin=False)
    assert st == 200, d


def test_mgmt_lesen_leer_mit_grenzen(welt):
    st, d = _mgmt(welt, "GET", "")
    assert st == 200
    e = d["data"]
    assert e["enabled"] is False and e["preise"] == [] and e["max_gespeichert"] == 10
    assert e["limits"]["preise"] == 20 and e["limits"]["gewicht"] == 1000


def test_mgmt_speichern_und_lesen(welt):
    preise = [_preis(name="Klein", gewicht=90, betrag=100), {"typ": "niete", "name": "Leer", "gewicht": 10}]
    st, d = _speichern(welt, preise, kosten=250)
    assert st == 200, d
    e = d["data"]
    assert e["enabled"] and e["kosten"] == 250 and [p["chance"] for p in e["preise"]] == [90.0, 10.0]
    assert all(bot._RAD_ID_RE.fullmatch(p["id"]) for p in e["preise"])
    assert [p["name"] for p in bot._rad_einstellungen(bot.guild_sicht(welt["a"], G_A))["preise"]] == ["Klein", "Leer"]
    assert [z["detail"] for z in bot._audit_log if z["action"] == "Discord-Management aktualisiert"] == ["gluecksrad · 2 Preis(e) · Server 1000"]


def test_mgmt_kennung_bleibt_beim_erneuten_speichern(welt):
    st, d = _speichern(welt, [_preis()])
    pid = d["data"]["preise"][0]["id"]
    st, d = _speichern(welt, [_preis(id=pid, betrag=900)])
    assert st == 200 and d["data"]["preise"][0]["id"] == pid and d["data"]["preise"][0]["betrag"] == 900


@pytest.mark.parametrize("daten,teil", [
    ({"preise": "x"}, "Höchstens"),
    ({"preise": [_preis()] * 21}, "Höchstens"),
    ({"preise": [_preis(gewicht=0)]}, "Zahlenwert"),
    ({"preise": [_preis(betrag=0)]}, "Zahlenwert"),
    ({"preise": [_preis(typ="boese")]}, "Preis-Typ"),
    ({"preise": [_preis(name="")]}, "Name"),
    ({"preise": [_preis()], "kosten": -1}, "Zahlenwert"),
    ({"preise": [_preis()], "kosten": "5"}, "Zahlenwert"),
    ({"preise": [_preis()], "gratis_stunden": 721}, "Zahlenwert"),
    ({"preise": [_preis()], "spielzeit_stunden": -2}, "Zahlenwert"),
    ({"preise": [_preis()], "max_gespeichert": 0}, "Zahlenwert"),
    ({"preise": [_preis()], "max_gespeichert": 101}, "Zahlenwert"),
    ({"preise": [], "enabled": True}, "mindestens einen Preis"),
    ({"preise": [_preis()], "enabled": True, "gratis_stunden": 0, "kosten": 0, "spielzeit_stunden": 0}, "Weg"),
    ({"preise": [_preis(id="0123abcd"), _preis(id="0123abcd")]}, "Doppelte"),
    ({"preise": [_preis(typ="item", item="Gibt es nicht", menge=1)]}, "Katalog")])
def test_mgmt_ablehnungen_speichern_nichts(welt, daten, teil):
    basis = {"enabled": True, "kosten": 0, "gratis_stunden": 24, "spielzeit_stunden": 0, "max_gespeichert": 5}
    basis.update(daten)
    st, d = _mgmt(welt, "POST", "", basis)
    assert st == 400 and teil in d["error"], d
    assert bot._rad_einstellungen(bot.guild_sicht(welt["a"], G_A))["preise"] == []


def test_mgmt_kein_json_objekt(welt):
    assert _mgmt(welt, "POST", "", [1, 2])[0] == 400


def test_mgmt_ausschalten_ohne_preise_erlaubt(welt):
    st, d = _mgmt(welt, "POST", "", {"enabled": False, "preise": [], "max_gespeichert": 3})
    assert st == 200 and d["data"]["enabled"] is False and d["data"]["max_gespeichert"] == 3


def test_mgmt_item_aus_dem_katalog_ok(welt):
    st, d = _speichern(welt, [_preis(typ="item", name="Kiste", item="Kiste", menge=2)])
    assert st == 200 and d["data"]["preise"][0]["item"] == "Kiste" and d["data"]["preise"][0]["menge"] == 2


def test_mgmt_rolle_pruefungen(welt, monkeypatch):
    monkeypatch.setattr(bot, "_rolle_vergebbar", lambda guild, rolle: None)
    st, d = _speichern(welt, [{"typ": "rolle", "name": "VIP", "gewicht": 1, "role_id": str(ROLLE_VIP)}])
    assert st == 200, d
    # Rolle gehoert nicht zur Guild
    st, d = _speichern(welt, [{"typ": "rolle", "name": "Fremd", "gewicht": 1, "role_id": "999"}])
    assert st == 400 and "gibt es in dem gewählten Discord-Server nicht" in d["error"]
    # Rolle nicht vergebbar
    monkeypatch.setattr(bot, "_rolle_vergebbar", lambda guild, rolle: ("Die Rolle liegt über der Bot-Rolle.", "x"))
    st, d = _speichern(welt, [{"typ": "rolle", "name": "VIP", "gewicht": 1, "role_id": str(ROLLE_VIP)}])
    assert st == 400 and "Bot-Rolle" in d["error"]


def test_mgmt_rolle_einer_fremden_guild_wird_abgelehnt(welt, monkeypatch):
    monkeypatch.setattr(bot, "_rolle_vergebbar", lambda guild, rolle: None)
    gesehen = []

    def pruefen(gid, rid, feld="Rolle"):
        gesehen.append((gid, rid))
        return bot.err("Diese Rolle gehört nicht zu diesem Discord-Server.")
    monkeypatch.setattr(bot, "_rolle_gehoert_guild", pruefen)
    st, d = _speichern(welt, [{"typ": "rolle", "name": "VIP", "gewicht": 1, "role_id": str(ROLLE_VIP)}])
    assert st == 400 and gesehen == [(G_A, ROLLE_VIP)]
    assert bot._rad_einstellungen(bot.guild_sicht(welt["a"], G_A))["preise"] == []


def test_mgmt_items_suche(welt):
    st, d = _mgmt(welt, "GET", "/items?q=ki")
    assert st == 200 and d["data"]["items"] == ["Kiste", "Vip-Kiste"]            # "Aus" ist deaktiviert, nicht gesucht
    assert _mgmt(welt, "GET", "/items?q=k")[1]["data"]["items"] == []            # zu kurz
    assert _mgmt(welt, "GET", "/items")[1]["data"]["items"] == []
    assert _mgmt(welt, "GET", "/items?q=" + "k" * 500)[1]["data"]["items"] == []
    assert _mgmt(welt, "GET", "/items?q=Aus")[1]["data"]["items"] == []          # deaktivierte Items nicht anbieten


def test_mgmt_items_hoechstens_zwanzig(welt):
    welt["a"]._catalog.items += [{"name": f"Ding{i:02d}", "classname": "X", "price": 1, "enabled": True} for i in range(40)]
    assert len(_mgmt(welt, "GET", "/items?q=ding")[1]["data"]["items"]) == 20


def test_mgmt_geben(welt):
    st, d = _mgmt(welt, "POST", "/geben", {"user_id": str(U), "anzahl": 3})
    assert st == 200 and d["data"]["drehungen"] == 3 and bot.db.rad_spieler(G_A, U)["drehungen"] == 3
    st, d = _mgmt(welt, "POST", "/geben", {"user_id": str(U), "anzahl": 50})
    assert st == 200 and d["data"]["drehungen"] == 10                           # Standard-Obergrenze 10
    eintraege = [z for z in bot._audit_log if z["action"] == "Glücksrad: Drehungen verschenkt"]
    assert [z["detail"] for z in eintraege] == [f"3× an {U} · Server 1000", f"50× an {U} · Server 1000"]


@pytest.mark.parametrize("daten,status", [
    ({"user_id": "abc", "anzahl": 1}, 400), ({"user_id": "", "anzahl": 1}, 400), ({"user_id": "0", "anzahl": 1}, 400),
    ({"user_id": "-5", "anzahl": 1}, 400), ({"user_id": "5" * 21, "anzahl": 1}, 400), ({"anzahl": 1}, 400),
    ({"user_id": str(U), "anzahl": 0}, 400), ({"user_id": str(U), "anzahl": 101}, 400), ({"user_id": str(U), "anzahl": "2"}, 400),
    ({"user_id": str(U), "anzahl": True}, 400), ({"user_id": str(U), "anzahl": 1.5}, 400), ({"user_id": str(U)}, 400),
    ({"user_id": "424242", "anzahl": 1}, 400)])
def test_mgmt_geben_ablehnungen(welt, daten, status):
    assert _mgmt(welt, "POST", "/geben", daten)[0] == status
    assert bot.db.rad_spieler(G_A, U)["drehungen"] == 0


def test_mgmt_geben_nur_an_mitglieder_dieser_guild(welt):
    # ANDERER ist nur in Guild A; Guild B kennt ihn nicht
    st, d = _mgmt(welt, "POST", "/geben", {"user_id": str(ANDERER), "anzahl": 1}, gid=G_B, conn="b")
    assert st == 400 and bot.db.rad_spieler(G_B, ANDERER)["drehungen"] == 0 and bot.db.rad_spieler(G_A, ANDERER)["drehungen"] == 0


def test_mgmt_mandanten_getrennt(welt):
    _speichern(welt, [_preis(name="Nur A")])
    st, d = _mgmt(welt, "GET", "", gid=G_B, conn="b")
    assert st == 200 and d["data"]["preise"] == [] and d["data"]["enabled"] is False


def test_mgmt_nicht_in_config_json_gespiegelt(welt):
    _speichern(welt, [_preis()])
    assert "gluecksrad" not in bot.cfg.config
    # Auch beim Hauptserver selbst (nicht nur ueber die Guild-Sicht) darf nichts nach config.json wandern - Gegenprobe mit
    # einem Schluessel, der gespiegelt wird.
    assert bot.connections.primary() is welt["a"]
    bot._conn_store(welt["a"], "gluecksrad", {"enabled": True, "preise": []})
    assert "gluecksrad" not in bot.cfg.config
    bot._conn_store(welt["a"], "irgendein_serverwert", 5)
    assert bot.cfg.config.get("irgendein_serverwert") == 5


def test_gluecksrad_status_in_der_kartenuebersicht(welt):
    _speichern(welt, [_preis()])
    assert bot._discord_mgmt_payload(bot.guild_sicht(welt["a"], G_A))["gluecksrad"] == {"enabled": True}
    assert bot._discord_mgmt_payload(bot.guild_sicht(welt["a"], G_A))["wortfilter"] == {"enabled": False}


# ── Slash-Befehle ───────────────────────────────────────────────────────────
class _Resp:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, embed=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed})

    async def defer(self, ephemeral=False):
        pass


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed})


class _Inter:
    def __init__(self, welt, uid=U, gid=G_A, locale=None):
        self.user = type("Nutzer", (), {"id": uid, "mention": f"<@{uid}>", "__str__": lambda s: "User"})()
        self.guild_id, self.guild, self.locale = gid, welt["guilds"][gid], locale
        self.response, self.followup = _Resp(), _Followup()

    def text(self):
        s = self.response.sent + self.followup.sent
        return " ".join(str(m["content"] or "") + (str(m["embed"].to_dict()) if m["embed"] else "") for m in s)


@pytest.fixture
def slash(welt, monkeypatch):
    async def ja(interaction):
        return True
    monkeypatch.setattr(bot, "_require_guild", ja)
    monkeypatch.setattr(bot, "_require_economy_enabled", ja)
    monkeypatch.setattr(bot, "_conn_of", lambda inter: welt["a"] if inter.guild_id == G_A else welt["b"])
    return welt


def _lauf(befehl, inter, **kw):
    asyncio.run(befehl.callback(inter, **kw))
    return inter.text()


def test_slash_info_aus(slash):
    _einstellen(slash, [_preis()], enabled=False)
    assert "nicht aktiv" in _lauf(bot.wheel_info, _Inter(slash))


def test_slash_info_zeigt_preise_und_wege(slash):
    _einstellen(slash, [_preis(betrag=500, gewicht=1), {"typ": "niete", "name": "Leer", "gewicht": 3}],
                gratis_stunden=12, kosten=100, spielzeit_stunden=5)
    text = _lauf(bot.wheel_info, _Inter(slash))
    assert "25" in text and "75" in text and "Gratis" in text and "pay:True" in text and "5 Std." in text


def test_slash_info_englisch(slash):
    _einstellen(slash, [_preis()], gratis_stunden=12)
    text = _lauf(bot.wheel_info, _Inter(slash, locale=bot.discord.Locale.american_english))
    assert "Wheel of fortune" in text and "Free" in text and "Glücksrad" not in text and "gespeicherten" not in text


def test_slash_spin_ohne_drehung(slash):
    _einstellen(slash, [_preis()], gratis_stunden=24, kosten=100)
    bot.db.rad_gratis_beanspruchen(G_A, U, 24, time.time())
    text = _lauf(bot.wheel_spin, _Inter(slash), pay=False)
    assert "keine Drehung" in text and bot.db.rad_log(G_A, U) == []


def test_slash_spin_reihenfolge_gespeichert_vor_gratis(slash):
    _einstellen(slash, [_preis()], gratis_stunden=24)
    bot.db.rad_drehungen_aendern(G_A, U, 1, 10)
    _lauf(bot.wheel_spin, _Inter(slash), pay=False)
    assert bot.db.rad_log(G_A, U)[0]["quelle"] == "gespeichert" and bot.db.rad_spieler(G_A, U)["letzter_gratis"] == 0.0
    _lauf(bot.wheel_spin, _Inter(slash), pay=False)
    assert bot.db.rad_log(G_A, U)[0]["quelle"] == "gratis"


def test_slash_spin_bezahlt(slash):
    _einstellen(slash, [_preis(betrag=1)], kosten=100)
    bot.db.add_wallet(G_A, U, 100)
    text = _lauf(bot.wheel_spin, _Inter(slash), pay=True)
    assert bot.db.rad_log(G_A, U)[0]["quelle"] == "bezahlt" and bot.db.get_balance(G_A, U)[0] == 1 and "💰" in text


def test_slash_spin_bezahlt_zu_wenig_geld(slash):
    _einstellen(slash, [_preis()], kosten=100)
    text = _lauf(bot.wheel_spin, _Inter(slash), pay=True)
    assert "Wallet" in text and bot.db.rad_log(G_A, U) == []


def test_slash_ohne_premium(slash, monkeypatch):
    monkeypatch.setattr(bot, "_conn_of", lambda inter: None)
    assert "Premium" in _lauf(bot.wheel_info, _Inter(slash)) and "Premium" in _lauf(bot.wheel_spin, _Inter(slash), pay=False)


def test_slash_give_nur_fuer_admins(slash, monkeypatch):
    ziel = type("M", (), {"id": ANDERER, "mention": "<@777>"})()
    async def verweigert(interaction):
        interaction.response.sent.append({"content": "verweigert", "embed": None})
    monkeypatch.setattr(bot, "_is_economy_admin", lambda inter: False)
    monkeypatch.setattr(bot, "_deny", verweigert)
    text = _lauf(bot.wheel_give, _Inter(slash), user=ziel, amount=3)
    assert "verweigert" in text and bot.db.rad_spieler(G_A, ANDERER)["drehungen"] == 0
    monkeypatch.setattr(bot, "_is_economy_admin", lambda inter: True)
    text = _lauf(bot.wheel_give, _Inter(slash), user=ziel, amount=3)
    assert "3" in text and bot.db.rad_spieler(G_A, ANDERER)["drehungen"] == 3


def test_slash_gruppe_ist_registriert():
    assert "wheel" in {c.name for c in bot.bot.tree.get_commands()}
    assert {c.name for c in bot.wheel_group.commands} == {"info", "spin", "give"}
