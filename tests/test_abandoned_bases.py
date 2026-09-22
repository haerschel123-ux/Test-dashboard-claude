"""Tests fuer den Feed "Verlassene Basen" (Abandoned Bases).

Schwerpunkte: das Clustering von Bauereignissen zu einem Standort
(abandoned_bases_record_build), die Flaggen-Zuordnung (record_flag - nur zu
einem bereits bestehenden Standort), die Regelpruefung
(abandoned_bases_kandidaten: jede der vier Regeln einzeln, Wiederholungssperre,
service_id-Trennung) sowie der Anschluss an den echten Log-Dispatch
(_dispatch) - nur wenn "abandoned_bases_enabled" fuer den jeweiligen Server
gesetzt ist, nie im Diagnose-Modus.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_abandoned_bases.py -v
"""
import asyncio
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


@pytest.fixture
def edb(tmp_path):
    return bot.EconomyDB(str(tmp_path / "economy_test.db"))


def _run(coro):
    return asyncio.run(coro)


# ── Clustering ────────────────────────────────────────────────────────────
def test_zwei_nahe_bauteile_werden_zu_einem_standort(edb):
    edb.abandoned_bases_record_build("1000", 100.0, 100.0, "ACC1", "Bauer1", "placed", radius_m=60)
    edb.abandoned_bases_record_build("1000", 110.0, 105.0, "ACC1", "Bauer1", "built", radius_m=60)
    kandidaten = edb.abandoned_bases_kandidaten("1000", {"min_parts": 1, "no_flag_enabled": False,
                                                         "builder_inactive_enabled": False,
                                                         "flag_lowered_enabled": False,
                                                         "no_activity_enabled": False})
    # Keine Regel aktiv -> keine Treffer, aber genau EIN Standort mit 2 Teilen
    assert kandidaten == []
    with edb._lock:
        rows = edb._conn.execute("SELECT * FROM base_sites WHERE service_id='1000'").fetchall()
    assert len(rows) == 1
    assert rows[0]["part_count"] == 2


def test_zwei_weit_entfernte_bauplaetze_werden_nie_verbunden(edb):
    edb.abandoned_bases_record_build("1000", 100.0, 100.0, "ACC1", "Bauer1", "placed", radius_m=60)
    edb.abandoned_bases_record_build("1000", 5000.0, 5000.0, "ACC2", "Bauer2", "placed", radius_m=60)
    with edb._lock:
        rows = edb._conn.execute("SELECT * FROM base_sites WHERE service_id='1000'").fetchall()
    assert len(rows) == 2


def test_reparatur_erhoeht_part_count_nicht_aber_aktualisiert_aktivitaet(edb):
    edb.abandoned_bases_record_build("1000", 0.0, 0.0, "ACC1", "Bauer1", "placed", radius_m=60)
    with edb._lock:
        vorher = edb._conn.execute("SELECT * FROM base_sites").fetchone()
    time.sleep(0.05)
    edb.abandoned_bases_record_build("1000", 1.0, 1.0, "ACC1", "Bauer1", "repaired", radius_m=60)
    with edb._lock:
        nachher = edb._conn.execute("SELECT * FROM base_sites").fetchone()
    assert nachher["part_count"] == 1  # unveraendert
    assert nachher["last_activity_at"] > vorher["last_activity_at"]


def test_service_id_trennt_standorte_strikt(edb):
    edb.abandoned_bases_record_build("1000", 0.0, 0.0, "ACC1", "Bauer1", "placed", radius_m=60)
    edb.abandoned_bases_record_build("2000", 0.0, 0.0, "ACC1", "Bauer1", "placed", radius_m=60)
    with edb._lock:
        rows_1000 = edb._conn.execute("SELECT * FROM base_sites WHERE service_id='1000'").fetchall()
        rows_2000 = edb._conn.execute("SELECT * FROM base_sites WHERE service_id='2000'").fetchall()
    assert len(rows_1000) == 1
    assert len(rows_2000) == 1


# ── Flaggen ───────────────────────────────────────────────────────────────
def test_flagge_ohne_nahen_standort_erzeugt_keinen_standort(edb):
    edb.abandoned_bases_record_flag("1000", 0.0, 0.0, raised=True, radius_m=60)
    with edb._lock:
        rows = edb._conn.execute("SELECT * FROM base_sites WHERE service_id='1000'").fetchall()
    assert rows == []


def test_flagge_gesenkt_ohne_erneutes_heissen_loest_regel_aus(edb):
    edb.abandoned_bases_record_build("1000", 0.0, 0.0, "ACC1", "Bauer1", "placed", radius_m=60)
    edb.abandoned_bases_record_flag("1000", 1.0, 1.0, raised=True, radius_m=60)
    time.sleep(0.01)
    edb.abandoned_bases_record_flag("1000", 1.0, 1.0, raised=False, radius_m=60)
    treffer = edb.abandoned_bases_kandidaten("1000", {
        "min_parts": 1, "flag_lowered_enabled": True,
        "builder_inactive_enabled": False, "no_flag_enabled": False, "no_activity_enabled": False})
    assert len(treffer) == 1
    assert treffer[0]["gruende"][0][0] == "flag_lowered"


def test_flagge_erneut_gehisst_nach_senken_entfernt_den_treffer(edb):
    edb.abandoned_bases_record_build("1000", 0.0, 0.0, "ACC1", "Bauer1", "placed", radius_m=60)
    edb.abandoned_bases_record_flag("1000", 1.0, 1.0, raised=True, radius_m=60)
    time.sleep(0.01)
    edb.abandoned_bases_record_flag("1000", 1.0, 1.0, raised=False, radius_m=60)
    time.sleep(0.01)
    edb.abandoned_bases_record_flag("1000", 1.0, 1.0, raised=True, radius_m=60)
    treffer = edb.abandoned_bases_kandidaten("1000", {
        "min_parts": 1, "flag_lowered_enabled": True,
        "builder_inactive_enabled": False, "no_flag_enabled": False, "no_activity_enabled": False})
    assert treffer == []


# ── Regeln ────────────────────────────────────────────────────────────────
def test_erbauer_inaktiv_regel_greift_erst_nach_grenzwert(edb):
    edb.abandoned_bases_record_build("1000", 0.0, 0.0, "ACC1", "Bauer1", "placed", radius_m=60)
    edb.player_seen_now("1000", "ACC1")
    treffer = edb.abandoned_bases_kandidaten("1000", {
        "min_parts": 1, "builder_inactive_enabled": True, "builder_inactive_days": 14,
        "no_flag_enabled": False, "flag_lowered_enabled": False, "no_activity_enabled": False})
    assert treffer == []  # gerade erst online gewesen
    # Kuenstlich "vor 20 Tagen" gesehen setzen.
    with edb._lock:
        edb._conn.execute("UPDATE player_last_seen SET last_seen=? WHERE account_id='ACC1'",
                          (time.time() - 20 * 86400,))
        edb._conn.commit()
    treffer = edb.abandoned_bases_kandidaten("1000", {
        "min_parts": 1, "builder_inactive_enabled": True, "builder_inactive_days": 14,
        "no_flag_enabled": False, "flag_lowered_enabled": False, "no_activity_enabled": False})
    assert len(treffer) == 1
    assert treffer[0]["gruende"][0][0] == "builder_inactive"


def test_aktiver_erbauer_verhindert_nur_diese_regel_nicht_andere(edb):
    with edb._lock:
        edb._conn.execute(
            "INSERT INTO base_sites (service_id, site_id, center_x, center_z, part_count, "
            "first_activity_at, last_activity_at) VALUES ('1000','s1',0,0,5,?,?)",
            (time.time() - 30 * 86400, time.time() - 30 * 86400))
        edb._conn.commit()
    edb.player_seen_now("1000", "ACC1")  # gerade eben online
    with edb._lock:
        edb._conn.execute(
            "INSERT INTO base_site_builders (service_id, site_id, account_id, gamertag, "
            "last_build_at) VALUES ('1000','s1','ACC1','Bauer1',?)", (time.time() - 30 * 86400,))
        edb._conn.commit()
    treffer = edb.abandoned_bases_kandidaten("1000", {
        "min_parts": 1, "builder_inactive_enabled": True, "builder_inactive_days": 14,
        "no_flag_enabled": False, "flag_lowered_enabled": False,
        "no_activity_enabled": True, "no_activity_days": 21})
    assert len(treffer) == 1
    gruende = [g[0] for g in treffer[0]["gruende"]]
    assert "builder_inactive" not in gruende
    assert "no_activity" in gruende


def test_keine_flagge_regel_greift_nach_grenzwert_ohne_je_eine_flagge(edb):
    with edb._lock:
        edb._conn.execute(
            "INSERT INTO base_sites (service_id, site_id, center_x, center_z, part_count, "
            "first_activity_at, last_activity_at) VALUES ('1000','s1',0,0,5,?,?)",
            (time.time() - 3 * 86400, time.time()))
        edb._conn.commit()
    treffer = edb.abandoned_bases_kandidaten("1000", {
        "min_parts": 1, "no_flag_enabled": True, "no_flag_hours": 48,
        "builder_inactive_enabled": False, "flag_lowered_enabled": False, "no_activity_enabled": False})
    assert len(treffer) == 1
    assert treffer[0]["gruende"][0][0] == "no_flag"


def test_standorte_unter_mindest_bauteilen_werden_ignoriert(edb):
    edb.abandoned_bases_record_build("1000", 0.0, 0.0, "ACC1", "Bauer1", "placed", radius_m=60)
    treffer = edb.abandoned_bases_kandidaten("1000", {
        "min_parts": 10, "no_flag_enabled": True, "no_flag_hours": 0,
        "builder_inactive_enabled": False, "flag_lowered_enabled": False, "no_activity_enabled": False})
    assert treffer == []


def test_wiederholungssperre_verhindert_erneutes_melden(edb):
    with edb._lock:
        edb._conn.execute(
            "INSERT INTO base_sites (service_id, site_id, center_x, center_z, part_count, "
            "first_activity_at, last_activity_at, last_reported_at) "
            "VALUES ('1000','s1',0,0,5,?,?,?)",
            (time.time() - 3 * 86400, time.time(), time.time() - 1 * 86400))
        edb._conn.commit()
    treffer = edb.abandoned_bases_kandidaten("1000", {
        "min_parts": 1, "no_flag_enabled": True, "no_flag_hours": 1, "repeat_after_days": 7,
        "builder_inactive_enabled": False, "flag_lowered_enabled": False, "no_activity_enabled": False})
    assert treffer == []  # erst vor einem Tag gemeldet, Sperre ist 7 Tage


def test_als_gemeldet_markieren_setzt_last_reported_at(edb):
    edb.abandoned_bases_record_build("1000", 0.0, 0.0, "ACC1", "Bauer1", "placed", radius_m=60)
    with edb._lock:
        site_id = edb._conn.execute("SELECT site_id FROM base_sites").fetchone()["site_id"]
    edb.abandoned_bases_als_gemeldet_markieren("1000", [site_id])
    with edb._lock:
        row = edb._conn.execute("SELECT last_reported_at FROM base_sites").fetchone()
    assert row["last_reported_at"] is not None


# ── Anschluss an den echten Log-Dispatch ─────────────────────────────────
class _FakeConnDispatch:
    def __init__(self, enabled=True):
        self.service_id = "1000"
        self.guild_id = 111
        self.guild_ids = [111]
        self.name = "Testserver"
        self.parser = None
        self.dispatch_verlauf = []
        self._werte = {"abandoned_bases_enabled": enabled, "abandoned_bases_cluster_radius_m": 60}

    def get(self, key, default=None):
        return self._werte.get(key, default)


@pytest.fixture
def gepatchter_dispatch(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    monkeypatch.setattr(bot.cfg, "guilds", {})
    return edb


def _build_event(x, z, player="Bauer1", player_id="ACC1", aktion="placed"):
    return {"type": "basebuild", "timestamp": "00:00:00", "player": player, "player_id": player_id,
           "aktion": aktion, "item": "Wall Kit", "position": f"{x}, {z}, 0", "raw": ""}


def test_dispatch_zeichnet_bauereignis_bei_aktiviertem_modul_auf(gepatchter_dispatch):
    conn = _FakeConnDispatch(enabled=True)
    _run(bot.bot._dispatch(_build_event(0, 0), conn))
    with gepatchter_dispatch._lock:
        rows = gepatchter_dispatch._conn.execute(
            "SELECT * FROM base_sites WHERE service_id='1000'").fetchall()
    assert len(rows) == 1


def test_dispatch_ohne_aktiviertes_modul_zeichnet_nichts_auf(gepatchter_dispatch):
    conn = _FakeConnDispatch(enabled=False)
    _run(bot.bot._dispatch(_build_event(0, 0), conn))
    with gepatchter_dispatch._lock:
        rows = gepatchter_dispatch._conn.execute(
            "SELECT * FROM base_sites WHERE service_id='1000'").fetchall()
    assert rows == []


def test_dispatch_im_diagnose_modus_zeichnet_nichts_auf(gepatchter_dispatch):
    conn = _FakeConnDispatch(enabled=True)
    _run(bot.bot._dispatch(_build_event(0, 0), conn, nebenwirkungen=False))
    with gepatchter_dispatch._lock:
        rows = gepatchter_dispatch._conn.execute(
            "SELECT * FROM base_sites WHERE service_id='1000'").fetchall()
    assert rows == []


def test_dispatch_pflegt_letzte_onlinezeit_bei_jedem_connect(gepatchter_dispatch):
    conn = _FakeConnDispatch()
    connect_ev = {"type": "connect", "timestamp": "00:00:00", "player": "SpielerA",
                 "player_id": "ACC9", "position": None, "raw": ""}
    _run(bot.bot._dispatch(connect_ev, conn))
    with gepatchter_dispatch._lock:
        row = gepatchter_dispatch._conn.execute(
            "SELECT * FROM player_last_seen WHERE service_id='1000' AND account_id='ACC9'").fetchone()
    assert row is not None
