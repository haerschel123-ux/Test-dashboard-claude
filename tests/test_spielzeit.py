"""Spielzeit im Spielerbereich (/api/me/playtime): Tagesverlauf, Summen, Rang und die Grenzen.

Ein Spieler sieht NUR seine eigenen, per /link verknuepften Namen - nie die Zeit anderer und nie die eines anderen Servers.

    python3 -m pytest tests/test_spielzeit.py -q
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_spieler_dashboard import G_A, G_B, U, ANDERER, _session, api, welt  # noqa: F401 - Fixtures
from test_airstrike import servers  # noqa: F401 - Fixture (zwei Server 1000/2000)

TAG = 86400


@pytest.fixture(autouse=True)
def _mittag_utc(monkeypatch):
    """Eingefrorene Uhr: 12:00 UTC. Ohne das fielen Sitzungen kurz nach Mitternacht (UTC) teilweise auf den Vortag und "heute" waere kleiner."""
    monkeypatch.setattr(bot.time, "time", lambda: 100 * 86400 + 43200.0)


@pytest.fixture(autouse=True)
def _mittag_utc(monkeypatch):
    """Eingefrorene Uhr: 12:00 UTC. Ohne das fielen Sitzungen kurz nach Mitternacht (UTC) teilweise auf den Vortag und "heute" waere kleiner."""
    monkeypatch.setattr(bot.time, "time", lambda: 100 * 86400 + 43200.0)


@pytest.fixture
def db(tmp_path):
    return bot.EconomyDB(str(tmp_path / "z.db"))


def _tag(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


# ── Auf Tage verteilen ──────────────────────────────────────────────────────
@pytest.mark.parametrize("sekunden,ende,erwartet", [
    (3600, 20 * TAG + 7200, [("1970-01-21", 3600)]),                                  # ganz im selben Tag
    (90000, 20 * TAG + 3600, [("1970-01-21", 3600), ("1970-01-20", TAG)]),            # ueber Mitternacht
    (7200, 20 * TAG, [("1970-01-20", 7200)]),                                         # endet genau um Mitternacht: gehoert zum Vortag
    (TAG, 20 * TAG, [("1970-01-20", TAG)]),
    (1, 20 * TAG + 1, [("1970-01-21", 1)]),
    (0, 20 * TAG + 5, []), (-5, 20 * TAG + 5, [])])
def test_tage_aufteilen(sekunden, ende, erwartet):
    assert bot.EconomyDB._tage_aufteilen(sekunden, ende) == erwartet


def test_tage_aufteilen_summe_stimmt_immer():
    for sek in (1, 59, 3599, 86399, 86400, 86401, 200000, 1_000_000):
        for ende in (TAG * 100, TAG * 100 + 1, TAG * 100 + 43200, TAG * 100 + 86399):
            teile = bot.EconomyDB._tage_aufteilen(sek, ende)
            assert sum(s for _t, s in teile) == min(sek, 15 * TAG) or len(teile) == 15
            assert all(s > 0 for _t, s in teile)


def test_tage_aufteilen_ist_auf_15_tage_begrenzt():
    assert len(bot.EconomyDB._tage_aufteilen(100 * TAG, 200 * TAG)) == 15


# ── Buchung ─────────────────────────────────────────────────────────────────
def test_sitzung_landet_in_gesamt_und_heute_namen_ohne_gross_klein(db):
    db.roster_add_playtime(1, "S", "ChrisKYF", 3600)
    db.roster_add_playtime(1, "S", "chriskyf", 600)
    z = db.playtime_summary(1, "S", ["CHRISKYF"])
    assert z["total"] == 4200 and z["heute"] == 4200 and z["woche"] == 4200 and z["monat"] == 4200


def test_sitzungen_ohne_dauer_werden_nicht_gebucht(db):
    db.roster_add_playtime(1, "S", "A", 0)
    db.roster_add_playtime(1, "S", "A", -10)
    assert db.playtime_summary(1, "S", ["A"])["total"] == 0


def test_mehrere_verknuepfte_namen_zaehlen_zusammen(db):
    db.roster_add_playtime(1, "S", "Haupt", 1000)
    db.roster_add_playtime(1, "S", "Alt", 500)
    db.roster_add_playtime(1, "S", "Fremd", 9999)
    z = db.playtime_summary(1, "S", ["Haupt", "Alt"])
    assert z["total"] == 1500 and z["heute"] == 1500


def test_guild_und_server_sind_getrennt(db):
    db.roster_add_playtime(1, "S", "A", 100)
    db.roster_add_playtime(2, "S", "A", 200)
    db.roster_add_playtime(1, "T", "A", 400)
    assert db.playtime_summary(1, "S", ["A"])["total"] == 100
    assert db.playtime_summary(2, "S", ["A"])["total"] == 200
    assert db.playtime_summary(1, "T", ["A"])["total"] == 400


# ── Zusammenfassung ─────────────────────────────────────────────────────────
def _tagzeile(db, name, ts, sek, guild=1, sid="S"):
    with db._lock:
        db._conn.execute("INSERT INTO playtime_days (guild_id, service_id, ingame_name, day, seconds) VALUES (?,?,?,?,?)",
                         (guild, sid, name, _tag(ts), sek))
        db._conn.execute("INSERT INTO player_roster (guild_id, service_id, ingame_name, total_playtime_seconds) VALUES (?,?,?,?) "
                         "ON CONFLICT(guild_id, service_id, ingame_name) DO UPDATE SET total_playtime_seconds = total_playtime_seconds + excluded.total_playtime_seconds",
                         (guild, sid, name, sek))
        db._conn.commit()


def test_zeitfenster_heute_woche_monat(db):
    jetzt = 100 * TAG + 36000                                                          # 10:00 UTC
    for tage_zurueck, sek in ((0, 100), (6, 200), (7, 400), (29, 800), (30, 1600), (45, 3200)):
        _tagzeile(db, "A", jetzt - tage_zurueck * TAG, sek)
    z = db.playtime_summary(1, "S", ["A"], jetzt)
    assert z["heute"] == 100
    assert z["woche"] == 300                     # heute + 6 Tage zurueck = 7 Kalendertage
    assert z["monat"] == 1500                    # 30 Kalendertage
    assert z["total"] == 6300                    # Gesamt kennt auch aeltere Zeit


def test_verlauf_hat_14_tage_aelteste_zuerst_und_fuellt_luecken(db):
    jetzt = 100 * TAG + 36000
    _tagzeile(db, "A", jetzt, 50)
    _tagzeile(db, "A", jetzt - 13 * TAG, 70)
    _tagzeile(db, "A", jetzt - 14 * TAG, 90)
    v = db.playtime_summary(1, "S", ["A"], jetzt)["tage"]
    assert len(v) == 14 and v[-1] == {"day": _tag(jetzt), "seconds": 50} and v[0] == {"day": _tag(jetzt - 13 * TAG), "seconds": 70}
    assert [x["day"] for x in v] == sorted(x["day"] for x in v) and sum(1 for x in v if x["seconds"] == 0) == 12


def _sitzung(db, name, connect, zuletzt, sid="S"):
    with db._lock:
        db._conn.execute("INSERT INTO sessions (service_id, ingame_name, connect_ts, last_seen_ts) VALUES (?,?,?,?)", (sid, name, connect, zuletzt))
        db._conn.commit()


def test_laufende_sitzung_zaehlt_mit_wenn_sie_plausibel_ist(db):
    jetzt = 100 * TAG + 36000
    db.roster_add_playtime(1, "S", "A", 1000)
    _sitzung(db, "A", jetzt - 600, jetzt - 30)
    z = db.playtime_summary(1, "S", ["A"], jetzt)
    assert z["total"] == 1600 and z["heute"] == 1600 and z["woche"] == 1600 and z["monat"] == 1600 and z["online_seit"] == jetzt - 600


def test_alte_haengende_sitzung_wird_nicht_mitgezaehlt(db):
    jetzt = 100 * TAG + 36000
    db.roster_add_playtime(1, "S", "A", 1000)
    _sitzung(db, "A", jetzt - 5000, jetzt - 4000)                     # seit ueber 15 Minuten nicht bestaetigt
    z = db.playtime_summary(1, "S", ["A"], jetzt)
    assert z["total"] == 1000 and z["online_seit"] is None
    db2 = db
    _sitzung(db2, "B", jetzt - 3 * TAG, jetzt - 10)                   # laenger als ein Tag: unplausibel
    assert db.playtime_summary(1, "S", ["B"], jetzt)["total"] == 0


def test_sitzung_eines_anderen_servers_zaehlt_nicht(db):
    jetzt = 100 * TAG + 36000
    _sitzung(db, "A", jetzt - 600, jetzt - 30, sid="T")
    assert db.playtime_summary(1, "S", ["A"], jetzt)["total"] == 0 and db.playtime_summary(1, "T", ["A"], jetzt)["total"] == 570 + 30


def test_rang(db):
    for name, sek in (("Ich", 500), ("Spitze", 900), ("Zweiter", 700), ("Gleich", 500), ("Letzter", 100)):
        db.roster_add_playtime(1, "S", name, sek)
    db.roster_add_playtime(1, "T", "FremdServer", 99999)                # anderer Server
    db.roster_add_playtime(2, "S", "FremdGuild", 99999)                 # andere Guild
    z = db.playtime_summary(1, "S", ["Ich"])
    assert z["rang"] == 3 and z["spieler"] == 5


def test_gleiche_spielzeit_teilt_sich_den_rang(db):
    db.roster_add_playtime(1, "S", "A", 500)
    db.roster_add_playtime(1, "S", "B", 500)
    db.roster_add_playtime(1, "S", "C", 900)
    assert db.playtime_summary(1, "S", ["A"])["rang"] == 2 and db.playtime_summary(1, "S", ["B"])["rang"] == 2


def test_rang_mehrerer_namen_zaehlt_die_summe_und_nie_gegen_sich_selbst(db):
    db.roster_add_playtime(1, "S", "Haupt", 300)
    db.roster_add_playtime(1, "S", "Alt", 300)
    db.roster_add_playtime(1, "S", "Anderer", 500)
    z = db.playtime_summary(1, "S", ["Haupt", "Alt"])
    assert z["total"] == 600 and z["rang"] == 1 and z["spieler"] == 2


def test_ohne_namen_oder_ohne_zeit_ist_alles_leer(db):
    for namen in ([], ["", ""], ["Nobody"]):
        z = db.playtime_summary(1, "S", namen)
        assert (z["total"], z["heute"], z["woche"], z["monat"], z["online_seit"], z["rang"]) == (0, 0, 0, 0, None, None)
        assert len(z["tage"]) == 14 and all(t["seconds"] == 0 for t in z["tage"])


def test_alte_tage_werden_beim_buchen_aufgeraeumt(db, monkeypatch):
    monkeypatch.setattr(bot, "_SPIELZEIT_TAGE_BEHALTEN", 5)
    jetzt = bot.time.time()
    for tage_zurueck in (3, 4, 6, 40):
        _tagzeile(db, "A", jetzt - tage_zurueck * TAG, 100)
    db.roster_add_playtime(1, "S", "A", 60)
    with db._lock:
        uebrig = sorted(r["day"] for r in db._conn.execute("SELECT day FROM playtime_days").fetchall())
    assert uebrig == sorted({_tag(jetzt - 3 * TAG), _tag(jetzt - 4 * TAG), _tag(jetzt)})


def test_sql_kann_nicht_ueber_namen_eingeschleust_werden(db):
    db.roster_add_playtime(1, "S", "Echt", 777)
    boese = ["A'); DROP TABLE player_roster; --", 'x" OR 1=1 --', "%", "_", "Echt' OR '1'='1"]
    z = db.playtime_summary(1, "S", boese)
    assert z["total"] == 0 and db.playtime_summary(1, "S", ["Echt"])["total"] == 777


def test_alle_namen_nur_hoechstens_zwanzig_und_doppelte_zaehlen_einmal(db):
    for i in range(25):
        db.roster_add_playtime(1, "S", f"N{i:02d}", 10)
    db.roster_add_playtime(1, "S", "Dop", 100)
    assert db.playtime_summary(1, "S", [f"N{i:02d}" for i in range(25)])["total"] == 200
    assert db.playtime_summary(1, "S", ["Dop", "dop", "DOP"])["total"] == 100


# ── API ─────────────────────────────────────────────────────────────────────
def _s():
    return _session(gid=G_A, sid_="1000")


def test_api_ohne_login_und_ohne_server(welt):  # noqa: F811
    assert api("GET", "/api/me/playtime")[0] in (401, 403)
    assert api("GET", "/api/me/playtime", _session())[0] == 409


def test_api_ohne_verknuepfung(welt):  # noqa: F811
    st, d = api("GET", "/api/me/playtime", _s())
    assert st == 200 and d["data"] == {"verknuepft": False, "namen": []}


def test_api_zeigt_nur_die_eigenen_namen(welt):  # noqa: F811
    bot.db.link_user(G_A, U, "MeinName")
    bot.db.roster_add_playtime(G_A, "1000", "MeinName", 3600)
    bot.db.roster_add_playtime(G_A, "1000", "FremderName", 7200)
    st, d = api("GET", "/api/me/playtime", _s())
    assert st == 200 and d["data"]["verknuepft"] is True and d["data"]["namen"] == ["MeinName"]
    assert d["data"]["total"] == 3600 and d["data"]["heute"] == 3600 and d["data"]["rang"] == 2 and "FremderName" not in str(d)


def test_api_andere_guild_und_server_sind_getrennt(welt):  # noqa: F811
    bot.db.link_user(G_A, U, "MeinName")
    bot.db.roster_add_playtime(G_A, "2000", "MeinName", 5000)           # Zeit auf einem anderen Server zaehlt nicht
    bot.db.roster_add_playtime(G_B, "1000", "MeinName", 5000)           # und auch nicht die einer anderen Guild
    assert api("GET", "/api/me/playtime", _s())[1]["data"]["total"] == 0
    sb = _session(gid=G_B, sid_="2000")
    assert api("GET", "/api/me/playtime", sb)[1]["data"]["verknuepft"] is False


def test_api_laufende_sitzung_und_tagesverlauf(welt):  # noqa: F811
    bot.db.link_user(G_A, U, "MeinName")
    jetzt = bot.time.time()
    bot.db.roster_add_playtime(G_A, "1000", "MeinName", 1800)
    _sitzung(bot.db, "MeinName", jetzt - 900, jetzt - 20, sid="1000")
    d = api("GET", "/api/me/playtime", _s())[1]["data"]
    assert d["total"] == 2700 and d["heute"] == 2700 and d["online_seit"] == jetzt - 900
    assert len(d["tage"]) == 14 and d["tage"][-1] == {"day": _tag(jetzt), "seconds": 2700}
