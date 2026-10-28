"""Gelieferte Shop-Items: 2 Minuten nach einem erkannten Neustart verschwinden die SHOP_-Eintraege aus der cfgEffectArea.json.

Fehlerbild: Die Eintraege blieben nach einem Neustart in der Datei. Der Shop wartete auf eine A2S-Antwort des Spielservers -
Konsolen-Server (PS4/Xbox) antworten oft nie, dann erst nach 45 Minuten Wartezeit plus 10 Minuten festem Delay. Und war die
alte ADM-Datei nach der Rotation nicht lesbar (FTP 550), kam der Neustart gar nicht erst beim Shop an.

    python3 -m pytest tests/test_shop_neustart.py -q
"""
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402

MISSION = "/dayzps/mpmissions/dayzOffline.nasdara"
DATEI = f"{MISSION}/cfgEffectArea.json"
GILDE = 777


class _Ftp:
    def __init__(self, dateien):
        self.dateien = dict(dateien)

    def read_file_ex(self, path):
        return (self.dateien[path], "ok") if path in self.dateien else (None, "missing")

    def write_file(self, path, content):
        self.dateien[path] = content
        return True

    def delete_file(self, path):
        return self.dateien.pop(path, None) is not None


def _vanilla():
    return json.dumps({"Areas": [{"AreaName": "Vanilla", "Type": "ContaminatedArea_Static",
                                  "Data": {"Pos": [1.0, 0.0, 2.0], "Radius": 50}}], "SafePositions": []})


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    flat = {"1000": {"service_id": "1000", "nitrado_token": "fake", "owner_discord_id": "1", "map_name": "Nasdara",
                     "guild_ids": [GILDE], "ftp_mission_dir": MISSION, "cfg_effect_area_path": DATEI}}
    (tmp_path / "connections.json").write_text(json.dumps(flat))
    registry = bot.ConnectionRegistry()
    registry.load()
    monkeypatch.setattr(bot, "connections", registry)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "9999"})
    monkeypatch.setattr(bot, "db", bot.EconomyDB(str(tmp_path / "e.db")))

    async def kein_feed(*a, **k):
        return True, ""
    monkeypatch.setattr(bot, "_post_feed", kein_feed)

    def keine_a2s(*a, **k):
        raise AssertionError("A2S darf der Shop nach einem Neustart nicht mehr abfragen")
    monkeypatch.setattr(bot, "a2s_query", keine_a2s)
    conn = registry._conns["1000"]
    conn.ftp = _Ftp({DATEI: _vanilla()})
    conn.shop = bot.ShopManager(object(), conn)
    return conn


def _kauf(conn, alter=1000.0, user=5, gilde=GILDE):
    """Ein Kauf samt SHOP_-Eintraegen in der Datei; ``alter`` Sekunden alt."""
    ok, _fehler, namen = asyncio.run(conn.shop.add_purchase_entries(["Wooden_Crate"], 1, 7679.2, 0.0, 7719.6))
    assert ok
    kid = bot.db.create_purchase("1000", gilde, user, "Anna", "Kiste", "Wooden_Crate", 1, 10, 7679.2, 0.0, 7719.6, namen)
    with bot.db._lock:
        bot.db._conn.execute("UPDATE purchases SET created_at=? WHERE id=?", (time.time() - alter, kid))
        bot.db._conn.commit()
    return kid, namen


def _namen_in_datei(conn):
    return [a["AreaName"] for a in json.loads(conn.ftp.dateien[DATEI])["Areas"]]


def _status(kid):
    with bot.db._lock:
        return bot.db._conn.execute("SELECT status FROM purchases WHERE id=?", (kid,)).fetchone()["status"]


def _kurze_frist(monkeypatch, sekunden):
    monkeypatch.setattr(bot.ShopManager, "CLEANUP_NACH_NEUSTART_SEKUNDEN", sekunden)


# ── Die Frist ────────────────────────────────────────────────────────────
def test_die_frist_ist_zwei_minuten():
    assert bot.ShopManager.CLEANUP_NACH_NEUSTART_SEKUNDEN == 120


def test_eintraege_bleiben_bis_zur_frist_und_sind_dann_weg(server, monkeypatch):
    _kurze_frist(monkeypatch, 1.2)
    kid, namen = _kauf(server)
    assert set(namen) <= set(_namen_in_datei(server))

    async def lauf():
        t0 = time.time()
        server.shop.neustart_erkannt("DayZServer_PS4_x64_2026-10-22_12-00-00.ADM")
        await asyncio.sleep(0.5)
        davor = (_namen_in_datei(server), _status(kid))            # Frist noch nicht um
        await server.shop._cleanup_task
        return davor, time.time() - t0
    (namen_davor, status_davor), dauer = asyncio.run(lauf())
    assert set(namen) <= set(namen_davor) and status_davor == "pending"
    assert dauer >= 1.2
    assert _namen_in_datei(server) == ["Vanilla"] and _status(kid) == "delivered"


def test_kein_warten_auf_die_a2s_antwort(server, monkeypatch):
    """Der Server hat IP und Query-Port, antwortet aber nie (Konsole): trotzdem nach der Frist weg, ohne A2S-Abfrage."""
    _kurze_frist(monkeypatch, 0.3)
    server.data["server_ip"] = "10.0.0.1"
    server.data["query_port"] = 27016
    kid, _ = _kauf(server)

    async def lauf():
        server.shop.neustart_erkannt("A.ADM")
        await asyncio.wait_for(server.shop._cleanup_task, timeout=5)
    asyncio.run(lauf())                                           # die A2S-Attrappe in der Fixture wirft bei jedem Aufruf
    assert _status(kid) == "delivered" and _namen_in_datei(server) == ["Vanilla"]


def test_alte_einstellung_aendert_die_frist_nicht(server, monkeypatch):
    """Auch ein aus alter Konfiguration uebernommener Wert (frueher 600) darf die Frist nicht verlaengern."""
    _kurze_frist(monkeypatch, 0.3)
    server.data["delivery_cleanup_delay_seconds"] = 600
    server.data["delivery_online_wait_max_seconds"] = 2700
    kid, _ = _kauf(server)

    async def lauf():
        server.shop.neustart_erkannt("A.ADM")
        await asyncio.wait_for(server.shop._cleanup_task, timeout=5)
    asyncio.run(lauf())
    assert _status(kid) == "delivered"


# ── Kauefe nach der Erkennung bleiben stehen ─────────────────────────────
def test_kauf_nach_der_erkennung_wird_nicht_als_geliefert_verbucht(server, monkeypatch):
    _kurze_frist(monkeypatch, 0.6)
    alt, namen_alt = _kauf(server)

    async def lauf():
        server.shop.neustart_erkannt("A.ADM")
        await asyncio.sleep(0.2)
        # Kauf NACH der Erkennung: der Server hat die Datei beim Hochfahren schon gelesen - dieser Eintrag spawnt erst beim naechsten Neustart
        ok, _f, namen = await server.shop.add_purchase_entries(["Barrel_Blue"], 1, 1.0, 0.0, 2.0)
        neu = bot.db.create_purchase("1000", GILDE, 6, "Berta", "Fass", "Barrel_Blue", 1, 5, 1.0, 0.0, 2.0, namen)
        await server.shop._cleanup_task
        return neu, namen
    neu, namen_neu = asyncio.run(lauf())
    assert _status(alt) == "delivered" and not set(namen_alt) & set(_namen_in_datei(server))
    assert _status(neu) == "pending" and set(namen_neu) <= set(_namen_in_datei(server))


def test_ohne_offene_kaeufe_passiert_nichts(server, monkeypatch):
    _kurze_frist(monkeypatch, 0.2)

    async def lauf():
        server.shop.neustart_erkannt("A.ADM")
        await server.shop._cleanup_task
    asyncio.run(lauf())
    assert _namen_in_datei(server) == ["Vanilla"]


# ── Mehrere Guilds an einem Server ───────────────────────────────────────
ZWEITE_GILDE = 888


def test_kauf_aus_der_zweiten_guild_wird_nach_dem_neustart_entfernt(server, monkeypatch):
    """Ein Server darf mehrere Guilds bedienen. Der Kauf aus der ZWEITEN Guild blieb mit seinen SHOP_-Eintraegen ewig in der
    Datei, weil das Aufraeumen nur die erste Guild des Servers kannte."""
    server.data["guild_ids"] = [GILDE, ZWEITE_GILDE]
    _kurze_frist(monkeypatch, 0.2)
    kid, namen = _kauf(server, gilde=ZWEITE_GILDE)

    async def lauf():
        server.shop.neustart_erkannt("A.ADM")
        await server.shop._cleanup_task
    asyncio.run(lauf())
    assert _status(kid) == "delivered" and not set(namen) & set(_namen_in_datei(server))


def test_kaeufe_beider_guilds_werden_im_selben_lauf_geliefert(server, monkeypatch):
    server.data["guild_ids"] = [GILDE, ZWEITE_GILDE]
    _kurze_frist(monkeypatch, 0.2)
    k1, n1 = _kauf(server, gilde=GILDE)
    k2, n2 = _kauf(server, gilde=ZWEITE_GILDE)

    async def lauf():
        server.shop.neustart_erkannt("A.ADM")
        await server.shop._cleanup_task
    asyncio.run(lauf())
    assert _status(k1) == "delivered" and _status(k2) == "delivered"
    assert _namen_in_datei(server) == ["Vanilla"]


def test_fremde_guild_ohne_zuordnung_wird_nicht_beliefert(server, monkeypatch):
    """Eine Guild, die dem Server NICHT zugeordnet ist, bleibt draussen (Mandantentrennung)."""
    _kurze_frist(monkeypatch, 0.2)
    kid, namen = _kauf(server, gilde=ZWEITE_GILDE)          # ZWEITE_GILDE gehoert hier nicht zum Server

    async def lauf():
        server.shop.neustart_erkannt("A.ADM")
        await server.shop._cleanup_task
    asyncio.run(lauf())
    assert _status(kid) == "pending" and set(namen) <= set(_namen_in_datei(server))


def test_shop_cleanup_haelt_offene_kaeufe_der_zweiten_guild_fuer_nicht_verwaist(server):
    """sweep_orphans (/shop cleanup) darf bezahlte, noch nicht gelieferte Eintraege der zweiten Guild nicht loeschen."""
    server.data["guild_ids"] = [GILDE, ZWEITE_GILDE]
    kid, namen = _kauf(server, gilde=ZWEITE_GILDE)
    entfernt = asyncio.run(server.shop.sweep_orphans())
    assert entfernt == 0 and set(namen) <= set(_namen_in_datei(server)) and _status(kid) == "pending"


def test_selbstheilung_stellt_fehlende_eintraege_der_zweiten_guild_wieder_her(server):
    server.data["guild_ids"] = [GILDE, ZWEITE_GILDE]
    kid, namen = _kauf(server, gilde=ZWEITE_GILDE)
    server.ftp.dateien[DATEI] = _vanilla()                   # Datei extern zurueckgesetzt
    bericht = asyncio.run(server.shop.check_and_heal())
    assert bericht["healed_purchases"] == [kid] and set(namen) <= set(_namen_in_datei(server))


# ── Zweite Absicherung: die .RPT ─────────────────────────────────────────
def test_rpt_allein_startet_die_frist_und_raeumt_auf(server, monkeypatch):
    """Meldet die ADM-Erkennung nichts (z. B. Rotation nicht bemerkt), reicht die neue .RPT als Neustart-Signal."""
    _kurze_frist(monkeypatch, 0.3)
    kid, namen = _kauf(server)

    async def lauf():
        server.shop.neustart_erkannt("DayZServer_PS4_x64_20261027_221000.RPT", zusatzquelle=True)
        await server.shop._cleanup_task
    asyncio.run(lauf())
    assert _status(kid) == "delivered" and _namen_in_datei(server) == ["Vanilla"]


def test_rpt_nach_der_adm_erkennung_zaehlt_den_neustart_nicht_doppelt(server, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))
    server.shop.neustart_erkannt("A.ADM")
    erste = server.shop._last_restart_at
    time.sleep(0.05)
    server.shop.neustart_erkannt("A.RPT", zusatzquelle=True)       # derselbe Neustart, 60 s spaeter ueber die RPT gesehen
    assert aufrufe == [True] and server.shop._last_restart_at == erste


def test_rpt_vor_der_adm_dann_adm_startet_keinen_zweiten_task(server, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))
    server.shop.neustart_erkannt("A.RPT", zusatzquelle=True)
    server.shop.neustart_erkannt("A.ADM")
    assert aufrufe == [True, True]                                  # spawn_cleanup selbst startet nie einen zweiten Task


def test_dieselbe_rpt_wird_nur_einmal_gemeldet(server, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))
    server.shop.neustart_erkannt("A.RPT", zusatzquelle=True)
    erste = server.shop._last_restart_at
    time.sleep(0.05)
    server.shop.neustart_erkannt("A.RPT", zusatzquelle=True)
    assert aufrufe == [True] and server.shop._last_restart_at == erste


def test_neue_rpt_lange_nach_dem_letzten_neustart_zaehlt_neu(server, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))
    server.shop.neustart_erkannt("A.RPT", zusatzquelle=True)
    server.shop._last_restart_at -= bot.ShopManager.ZUSATZQUELLE_DOPPELT_SEKUNDEN + 1     # der erste Neustart ist lange her
    server.shop.neustart_erkannt("B.RPT", zusatzquelle=True)
    assert aufrufe == [True, True]


class _Rpt:
    """Gestubbter Poller-Zyklus nur fuer _pruefe_neustart: liefert die jeweils aktuelle RPT-Liste."""

    def __init__(self, monkeypatch, conn):
        self.dateien, self.heruntergeladen, self.events = [], [], []
        outer = self

        async def liste(c, log_dir, endung):
            return list(outer.dateien) if endung == ".rpt" else []

        async def download(c, key, pfad, loop):
            outer.heruntergeladen.append(pfad)

        async def dispatch(ev, c):
            outer.events.append(ev["type"])
        monkeypatch.setattr(bot, "_log_dateien", liste)
        self.stub = SimpleNamespace(_post_log_download=download, _dispatch=dispatch)
        self.conn = conn

    def zyklus(self):
        self.conn.rpt_geprueft_ts = 0.0                             # die 60-s-Sperre ueberspringen
        asyncio.run(bot.DayZBot._pruefe_neustart(self.stub, self.conn, "/log", asyncio.new_event_loop()))


def test_pruefe_neustart_meldet_neue_rpt_dem_shop_aber_nicht_beim_ersten_durchlauf(server, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))
    rpt = _Rpt(monkeypatch, server)
    rpt.dateien = ["/log/DayZServer_PS4_x64_20261027_200000.RPT"]
    rpt.zyklus()
    assert aufrufe == [] and rpt.events == []                       # erster Durchlauf: nur merken, kein Neustart
    rpt.zyklus()
    assert aufrufe == [] and rpt.events == []                       # unveraendert
    rpt.dateien = ["/log/DayZServer_PS4_x64_20261027_221000.RPT"]   # der Server wurde neu gestartet
    rpt.zyklus()
    assert aufrufe == [True] and rpt.events == ["server_restart"]
    assert server.shop._gemeldete_zusatz == "DayZServer_PS4_x64_20261027_221000.RPT"


# ── Dritte Absicherung: der Nitrado-Status (wie die Anzeige oben im Dashboard) ──
class _Nitrado:
    """Gestubbte Nitrado-API: liefert nacheinander die vorgegebenen Zustaende (None = API-Fehler, Exception = Ausnahme)."""

    def __init__(self, zustaende):
        self.zustaende, self.abfragen = list(zustaende), 0

    async def get_info(self):
        self.abfragen += 1
        z = self.zustaende.pop(0) if self.zustaende else "started"
        if isinstance(z, Exception):
            raise z
        return None if z is None else {"status": z}


def _nitrado_pruefen(server, anzahl=1):
    for _ in range(anzahl):
        server.shop._nitrado_geprueft_ts = 0.0                      # die 30-s-Sperre ueberspringen
        asyncio.run(server.shop.nitrado_zustand_pruefen())


def test_nitrado_ohne_offene_lieferungen_wird_nicht_gefragt(server):
    server.api = _Nitrado(["restarting", "started"])
    _nitrado_pruefen(server, 2)
    assert server.api.abfragen == 0


def test_nitrado_neustart_wird_beim_zurueck_auf_gestartet_erkannt(server, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))
    _kauf(server)
    server.api = _Nitrado(["started", "started", "restarting", "restarting", "started", "started"])
    _nitrado_pruefen(server, 2)
    assert aufrufe == []                                            # laeuft normal: kein Neustart
    _nitrado_pruefen(server, 2)
    assert aufrufe == []                                            # mitten im Neustart: noch nichts
    _nitrado_pruefen(server, 1)
    assert aufrufe == [True]                                        # wieder gestartet
    _nitrado_pruefen(server, 1)
    assert aufrufe == [True]                                        # bleibt gestartet: nicht noch einmal


@pytest.mark.parametrize("weg", [["stopping", "stopped", "starting", "started"], ["stopped", "started"],
                                 ["starting", "started"], ["restarting", "RESTARTING", "Started"]])
def test_nitrado_jeder_uebergang_zaehlt_als_neustart(server, monkeypatch, weg):
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))
    _kauf(server)
    server.api = _Nitrado(weg)
    _nitrado_pruefen(server, len(weg))
    assert aufrufe == [True]


def test_nitrado_api_fehler_loest_nichts_aus_und_verliert_den_uebergang_nicht(server, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))
    _kauf(server)
    server.api = _Nitrado(["restarting", None, RuntimeError("API weg"), {}, "started"])
    server.api.zustaende[3] = None
    _nitrado_pruefen(server, 5)
    assert aufrufe == [True]                                        # der Fehler dazwischen hat den Uebergang nicht vergessen


def test_nitrado_wird_hoechstens_alle_30_sekunden_gefragt(server):
    _kauf(server)
    server.api = _Nitrado(["started"] * 5)
    for _ in range(5):
        asyncio.run(server.shop.nitrado_zustand_pruefen())          # ohne die Sperre zurueckzusetzen
    assert server.api.abfragen == 1


def test_nitrado_ohne_api_verbindung_passiert_nichts(server):
    _kauf(server)
    server.api = None
    _nitrado_pruefen(server)                                        # darf nicht abstuerzen


def test_nitrado_nach_adm_erkennung_zaehlt_den_neustart_nicht_doppelt(server, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))
    _kauf(server)
    server.shop.neustart_erkannt("A.ADM")
    erste = server.shop._last_restart_at
    server.api = _Nitrado(["restarting", "started"])
    _nitrado_pruefen(server, 2)
    assert aufrufe == [True] and server.shop._last_restart_at == erste


def test_nitrado_neustart_raeumt_auf_auch_ohne_logdateien(server, monkeypatch):
    """Der ganze Weg: Kauf, Neustart-Anzeige wechselt, Frist, SHOP_-Eintrag weg - ohne dass ADM oder RPT etwas melden."""
    _kurze_frist(monkeypatch, 0.3)
    kid, namen = _kauf(server)
    server.api = _Nitrado(["restarting", "started"])

    async def lauf():
        for _ in range(2):
            server.shop._nitrado_geprueft_ts = 0.0
            await server.shop.nitrado_zustand_pruefen()
        await server.shop._cleanup_task                              # der Task muss im selben Event-Loop weiterlaufen
    asyncio.run(lauf())
    assert _status(kid) == "delivered" and _namen_in_datei(server) == ["Vanilla"]


def test_poller_fragt_den_nitrado_status_auch_wenn_der_zyklus_spaeter_abbricht(server, poller, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(server.shop, "nitrado_zustand_pruefen", lambda: _gezaehlt(aufrufe))
    poller.dateien = []                                              # keine ADM-Dateien: der Zyklus bricht dort ab
    poller.zyklus()
    assert aufrufe == [1]


async def _gezaehlt(liste):
    liste.append(1)


# ── Entprellen ───────────────────────────────────────────────────────────
def test_derselbe_neustart_startet_die_frist_nicht_neu(server, monkeypatch):
    _kurze_frist(monkeypatch, 5)
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))
    server.shop.neustart_erkannt("A.ADM")
    erste = server.shop._last_restart_at
    time.sleep(0.05)
    server.shop.neustart_erkannt("A.ADM")                          # Rotation wird wiederholt: gleiche Datei
    server.shop.neustart_erkannt("A.ADM")
    assert aufrufe == [True] and server.shop._last_restart_at == erste


def test_ein_anderer_neustart_zaehlt_neu(server, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))
    server.shop.neustart_erkannt("A.ADM")
    erste = server.shop._last_restart_at
    time.sleep(0.05)
    server.shop.neustart_erkannt("B.ADM")
    assert aufrufe == [True, True] and server.shop._last_restart_at > erste


def test_ohne_kennung_zaehlt_jeder_aufruf(server, monkeypatch):
    """Leerung der ADM (Truncation) hat keinen Dateinamen: jeder Aufruf ist ein eigener Neustart."""
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))
    server.shop.neustart_erkannt()
    server.shop.neustart_erkannt()
    assert aufrufe == [True, True]


def test_neuer_neustart_waehrend_der_wartezeit_verschiebt_die_frist(server, monkeypatch):
    _kurze_frist(monkeypatch, 1.0)
    kid, _ = _kauf(server)

    async def lauf():
        t0 = time.time()
        server.shop.neustart_erkannt("A.ADM")
        await asyncio.sleep(0.6)
        server.shop.neustart_erkannt("B.ADM")                       # zweiter Neustart mitten in der Wartezeit
        await asyncio.sleep(0.7)                                    # t0+1.3: erste Frist waere um, die zweite nicht
        status_mitten = _status(kid)
        await server.shop._cleanup_task
        return status_mitten, time.time() - t0
    status_mitten, dauer = asyncio.run(lauf())
    assert status_mitten == "pending" and dauer >= 1.6 and _status(kid) == "delivered"


# ── Anbindung an den Log-Poller ──────────────────────────────────────────
ALT_ADM = "/games/x/noftp/dayzps/config/DayZServer_PS4_x64_2026-10-22_09-02-19.ADM"
NEU_ADM = "/games/x/noftp/dayzps/config/DayZServer_PS4_x64_2026-10-22_12-02-30.ADM"


@pytest.fixture
def poller(server, monkeypatch):
    """Ein Poll-Zyklus von ``DayZBot._poll_connection`` mit gestubbtem Log-Zugriff."""
    server.data["ftp_log_dir"] = "/dayzps/config"
    server.log_state["current"] = {"file": ALT_ADM, "offset": 100}
    server.log_state["last_poll_ts"] = time.time()
    ctx = SimpleNamespace(dateien=[ALT_ADM, NEU_ADM], alter_rest="lesbar", groesse=50, lesungen=[])

    async def leer(*a, **k):
        return None

    async def dateien(conn, verzeichnis, endung):
        return list(ctx.dateien) if endung == ".adm" else []

    async def groesse(conn, pfad):
        return ctx.groesse

    async def lesen(conn, pfad, offset):
        ctx.lesungen.append(pfad)
        if pfad == ALT_ADM and ctx.alter_rest == "unlesbar":
            return None, offset                                      # FTP 550
        return "", offset

    stub = SimpleNamespace(_pruefe_neustart=leer, _lese_ce_events=leer, _roster_backfill_falls_noetig=leer,
                           _hydrate_online_state=leer, _check_ftp_health=leer, _post_log_download=leer)
    monkeypatch.setattr(bot, "_log_dateien", dateien)
    monkeypatch.setattr(bot, "_log_dateigroesse", groesse)
    monkeypatch.setattr(bot, "_log_lesen_ab_offset", lesen)
    monkeypatch.setattr(bot, "_poll_zustand_melden", leer)
    monkeypatch.setattr(bot, "_adj_poll", lambda *a, **k: None)
    aufrufe = []
    monkeypatch.setattr(server.shop, "spawn_cleanup", lambda delayed=False: aufrufe.append(delayed))

    def zyklus():
        asyncio.run(bot.DayZBot._poll_connection(stub, server))
    ctx.zyklus, ctx.aufrufe = zyklus, aufrufe
    return ctx


def test_poller_meldet_die_rotation_dem_shop(server, poller):
    poller.zyklus()
    assert poller.aufrufe == [True] and server.shop._gemeldete_kennung == NEU_ADM


def test_poller_meldet_auch_wenn_die_alte_datei_nicht_lesbar_ist(server, poller):
    """Der Zyklus bricht dann ab und die Rotation wird wiederholt - der Neustart muss trotzdem sofort beim Shop sein."""
    poller.alter_rest = "unlesbar"
    poller.zyklus()
    assert ALT_ADM in poller.lesungen                              # der Rest der alten Datei wurde versucht ...
    assert server.log_state["current"]["file"] == ALT_ADM          # ... die Rotation ist NICHT vollzogen ...
    assert poller.aufrufe == [True]                                # ... und der Shop weiss es trotzdem


def test_wiederholte_rotation_startet_die_frist_nicht_jeden_zyklus_neu(server, poller):
    poller.alter_rest = "unlesbar"
    erste = None
    for _ in range(4):
        poller.zyklus()
        erste = erste or server.shop._last_restart_at
    assert poller.aufrufe == [True] and server.shop._last_restart_at == erste


def test_poller_ohne_neustart_meldet_nichts(server, poller):
    poller.dateien = [ALT_ADM]
    poller.groesse = 500                                            # Datei ist nur gewachsen
    poller.zyklus()
    assert poller.aufrufe == [] and server.shop._gemeldete_kennung == ""


def test_poller_meldet_die_leerung_der_adm(server, poller):
    poller.dateien = [ALT_ADM]
    poller.groesse = 10                                             # kleiner als der Cursor (100): Server hat sie geleert
    poller.zyklus()
    assert poller.aufrufe == [True]


def test_poller_wiederholt_nur_das_entfernen_nach_ftp_fehler(server, poller):
    poller.dateien = [ALT_ADM]
    poller.groesse = 500
    server.shop.cleanup_retry_needed = True
    poller.zyklus()
    assert poller.aufrufe == [False]                                # sofort, ohne neue Frist
