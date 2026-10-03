"""Server-Seite „Utilities“/„Generell“ (Vorbild DayZ++): Schaden-Zustand,
Kartenlinks je Anbieter/Stil, Offline-Waechter, Verhaltens-Feeds (Combat-Log,
Rage-Quit, Killstreak), Heatmap-Datenhaltung, General-Validierung, Kopieren.

    python3 -m pytest tests/test_server_general.py -q
"""
import asyncio
import json
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")
from log_parser import DayZLogParser  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeApi:
    def __init__(self, status="stopped"):
        self.status = status
        self.restarts = 0

    async def get_info(self):
        return {"status": self.status}

    async def restart(self):
        self.restarts += 1
        return True, "ok"


class FakeFtp:
    def __init__(self, dateien):
        self.dateien = dateien

    def read_file_ex(self, pfad):
        if pfad in self.dateien:
            return self.dateien[pfad], "ok"
        return None, "missing"

    def write_file(self, pfad, inhalt):
        self.dateien[pfad] = inhalt
        return True


def _conn(sid="1000", gid=111, **data):
    d = {"service_id": sid, "guild_id": gid, "map_name": "ChernarusPlus", "ftp_mission_dir": "/mission"}
    d.update(data)
    c = bot.ServerConnection(d)
    c.parser = DayZLogParser()
    return c


@pytest.fixture(autouse=True)
def _ruhe(monkeypatch):
    monkeypatch.setattr(bot, "_audit_add", lambda *a, **k: None)
    monkeypatch.setattr(bot.connections, "save", lambda *a, **k: None)
    posts = []

    async def fake_post_feed(gid, typ, embed, **k):
        posts.append({"gid": gid, "typ": typ, "embed": embed})
        return True, "sent"
    monkeypatch.setattr(bot, "_post_feed", fake_post_feed)
    yield posts


# ── Kartenlinks ──────────────────────────────────────────────────────
def test_karten_link_izurvive_stile():
    c = _conn()
    assert bot._karten_link(100, 200, "ChernarusPlus", conn=c) == "https://www.izurvive.com/chernarusplussatmap/#location=100;200;0"
    c.data["map_link_style"] = "topo"
    assert "/chernarusplus/#" in bot._karten_link(100, 200, "ChernarusPlus", conn=c)
    c.data["map_link_style"] = "tourist"
    assert "/chernarusplushiking/#" in bot._karten_link(1, 2, "ChernarusPlus", conn=c)
    assert "/livoniaHiking/#" in bot._karten_link(1, 2, "Livonia", conn=c)
    # Sakhal hat keine Touristen-Karte -> topo
    assert "/sakhal/#" in bot._karten_link(1, 2, "Sakhal", conn=c)


def test_karten_link_xam():
    c = _conn(map_link_provider="xam", map_link_style="sat")
    assert bot._karten_link(4500.4, 9637.6, "ChernarusPlus", conn=c) == "https://dayz.xam.nu/chernarusplus/satmap#location=4500;9638;5"
    c.data["map_link_style"] = "topo"
    assert bot._karten_link(1, 2, "Livonia", conn=c).startswith("https://dayz.xam.nu/livonia/topographic#location=")
    c.data["map_link_provider"] = "quatsch"
    assert "izurvive.com" in bot._karten_link(1, 2, "Livonia", conn=c)


def test_tile_url_stile():
    assert "/topographic/" in bot._tile_url("ChernarusPlus")
    assert "/satellite/" in bot._tile_url("ChernarusPlus", "sat")
    assert bot._tile_url("Unbekannt") == ""


# ── Location Privacy ─────────────────────────────────────────────────
def test_position_privat_und_map_players_filter(monkeypatch):
    c = _conn(location_privacy_names=["Geheim"])
    assert bot._position_privat(c, "geheim")
    assert not bot._position_privat(c, "Offen")
    assert not bot._position_privat(None, "Geheim")
    ev = {"type": "connect", "player": "Geheim", "position": "1, 2, 3"}
    token = bot._AKTUELLER_SERVER.set(c)
    try:
        e = bot.discord.Embed(title="x")
        bot._add_location_field(e, ev, "player", None)
        assert not e.fields
        ev2 = {"type": "connect", "player": "Offen", "position": "1, 2, 3"}
        bot._add_location_field(e, ev2, "player", None)
        assert len(e.fields) == 1
    finally:
        bot._AKTUELLER_SERVER.reset(token)


# ── Schaden ──────────────────────────────────────────────────────────
def test_schaden_zustand_lesen_invertiert():
    c = _conn()
    c.ftp = FakeFtp({"/mission/cfggameplay.json": json.dumps(
        {"GeneralData": {"disableBaseDamage": True, "disableContainerDamage": False}})})
    z = _run(bot._schaden_zustand_lesen(c))
    assert z["code"] == "ok" and z["base"] is False and z["container"] is True
    c.ftp = FakeFtp({"/mission/cfggameplay.json": json.dumps({"GeneralData": {}})})
    z = _run(bot._schaden_zustand_lesen(c))
    assert z["base"] is True and z["container"] is True     # nicht gesetzt = DayZ-Vorgabe an
    c.ftp = FakeFtp({})
    assert _run(bot._schaden_zustand_lesen(c))["code"] == "fehlt"
    c.ftp = None
    assert _run(bot._schaden_zustand_lesen(c))["code"] == "kein_ftp"


# ── Offline-Waechter ─────────────────────────────────────────────────
def test_waechter_startet_nur_mit_schutz(monkeypatch):
    c = _conn(keep_server_running=True)
    c.api = FakeApi("stopped")
    jetzt = [1_000_000.0]
    monkeypatch.setattr(bot.time, "time", lambda: jetzt[0])
    # 1. Offline erkannt -> noch kein Start
    _run(bot.bot._offline_waechter(c, None))
    assert c.api.restarts == 0 and c.offline_seit == jetzt[0]
    # 2. 5 Minuten spaeter: zu frueh
    jetzt[0] += 300
    _run(bot.bot._offline_waechter(c, None))
    assert c.api.restarts == 0
    # 3. 11 Minuten: Start
    jetzt[0] += 400
    _run(bot.bot._offline_waechter(c, None))
    assert c.api.restarts == 1
    assert c.data["watchdog_letzter_start_ts"] == jetzt[0]
    # 4. Weiter offline, 20 Minuten spaeter: Abstand 30 Min nicht erreicht
    jetzt[0] += 1200
    c.offline_seit = jetzt[0] - 1200
    _run(bot.bot._offline_waechter(c, None))
    assert c.api.restarts == 1
    # 5. nach 31 Minuten erneut
    jetzt[0] += 700
    _run(bot.bot._offline_waechter(c, None))
    assert c.api.restarts == 2


def test_waechter_respektiert_stopp_flag_schonfrist_und_status(monkeypatch):
    jetzt = [2_000_000.0]
    monkeypatch.setattr(bot.time, "time", lambda: jetzt[0])
    c = _conn(keep_server_running=True)
    c.api = FakeApi("stopped")
    c.offline_seit = jetzt[0] - 3600
    c.stopp_markieren("test")
    assert c.offline_seit is None
    c.offline_seit = jetzt[0] - 3600
    _run(bot.bot._offline_waechter(c, None))
    assert c.api.restarts == 0            # absichtlich gestoppt
    c.neustart_markieren()                # loescht das Flag, setzt Schonfrist
    assert "server_absichtlich_gestoppt" not in c.data
    c.offline_seit = jetzt[0] - 3600
    _run(bot.bot._offline_waechter(c, None))
    assert c.api.restarts == 0            # Schonfrist
    jetzt[0] += 1000
    c.api.status = "restarting"
    _run(bot.bot._offline_waechter(c, None))
    assert c.api.restarts == 0            # Uebergang
    c.api.status = "started"
    _run(bot.bot._offline_waechter(c, None))
    assert c.api.restarts == 0            # Haenger-Stufe aus
    c.data["keep_server_running_haenger"] = True
    _run(bot.bot._offline_waechter(c, None))
    assert c.api.restarts == 1
    # Online -> Flag und offline_seit weg
    c.stopp_markieren("x")
    _run(bot.bot._offline_waechter(c, {"players": 1}))
    assert c.offline_seit is None and "server_absichtlich_gestoppt" not in c.data


def test_waechter_aus_ohne_schalter(monkeypatch):
    jetzt = [3_000_000.0]
    monkeypatch.setattr(bot.time, "time", lambda: jetzt[0])
    c = _conn()
    c.api = FakeApi("stopped")
    c.offline_seit = jetzt[0] - 7200
    _run(bot.bot._offline_waechter(c, None))
    assert c.api.restarts == 0


# ── Verhaltens-Feeds ─────────────────────────────────────────────────
def _hit(ts, opfer, taeter):
    return {"type": "damage", "timestamp": ts, "victim": opfer, "attacker": taeter, "attacker_id": "t"}


def _kill(ts, opfer, killer):
    return {"type": "kill_pvp", "timestamp": ts, "victim": opfer, "killer": killer, "killer_id": "k",
            "killer_position": "1, 2, 3", "raw": "r"}


def _dc(ts, spieler):
    return {"type": "disconnect", "timestamp": ts, "player": spieler, "player_id": "p", "position": "4, 5, 6"}


def test_combat_log_an_der_schwelle_und_mitternacht():
    c = _conn(combat_log_seconds=60)
    assert bot._verhalten_auswerten(_hit("23:59:30", "A", "B"), c) == []
    out = bot._verhalten_auswerten(_dc("00:00:20", "A"), c)       # 50 s ueber Mitternacht
    assert len(out) == 1 and out[0]["type"] == "combat_log" and out[0]["sekunden"] == 50
    assert out[0]["gegner"] == "B"
    # B (Angreifer) ebenfalls im Fenster
    out = bot._verhalten_auswerten(_dc("00:00:30", "B"), c)
    assert out and out[0]["type"] == "combat_log" and out[0]["gegner"] == "A"
    # Ausserhalb der Schwelle: nichts
    bot._verhalten_auswerten(_hit("10:00:00", "C", "D"), c)
    assert bot._verhalten_auswerten(_dc("10:01:01", "C"), c) == []
    # Umwelt-Treffer zaehlt nicht
    bot._verhalten_auswerten({"type": "damage", "timestamp": "11:00:00", "victim": "E",
                              "attacker": "Umgebung", "attacker_id": "Umgebung"}, c)
    assert bot._verhalten_auswerten(_dc("11:00:05", "E"), c) == []


def test_rage_quit_vor_combat_log():
    c = _conn(rage_quit_seconds=30, combat_log_seconds=60)
    bot._verhalten_auswerten(_hit("12:00:00", "A", "B"), c)
    bot._verhalten_auswerten(_kill("12:00:10", "A", "B"), c)
    out = bot._verhalten_auswerten(_dc("12:00:25", "A"), c)
    assert len(out) == 1 and out[0]["type"] == "rage_quit" and out[0]["killer"] == "B"
    assert out[0]["sekunden"] == 15
    # 40 s nach Tod: kein Rage-Quit, aber noch Combat-Log (Treffer 55 s her)
    bot._verhalten_auswerten(_hit("13:00:00", "C", "D"), c)
    bot._verhalten_auswerten(_kill("13:00:15", "C", "D"), c)
    out = bot._verhalten_auswerten(_dc("13:00:55", "C"), c)
    assert len(out) == 1 and out[0]["type"] == "combat_log"


def test_killstreak_schritte_und_reset():
    c = _conn(killstreak_min=3, killstreak_step=2)
    arten = []
    for i in range(6):
        for ev in bot._verhalten_auswerten(_kill("14:00:0%d" % i, "V%d" % i, "K"), c):
            arten.append((ev["type"], ev["streak"]))
    assert arten == [("killstreak", 3), ("killstreak", 5)]
    assert c.killstreaks["k"] == 6 and c.data["killstreak_stand"] == {"k": 6}
    # Tod des Killers setzt zurueck
    bot._verhalten_auswerten(_kill("14:01:00", "K", "X"), c)
    assert "k" not in c.killstreaks and c.killstreaks["x"] == 1
    # Disconnect setzt zurueck
    bot._verhalten_auswerten(_dc("14:02:00", "X"), c)
    assert "x" not in c.killstreaks
    # Server-Neustart leert alles
    c.killstreaks["y"] = 4
    c.kampf_zustand["y"] = {"treffer_ts": "14:03:00"}
    c.online_zustand_zuruecksetzen("test")
    assert not c.killstreaks and not c.kampf_zustand


def test_killstreak_stand_wird_geladen():
    c = _conn(killstreak_stand={"k": 4})
    out = bot._verhalten_auswerten(_kill("15:00:00", "V", "K"), c)
    assert out and out[0]["streak"] == 5


def test_feed_erweiterungen_postet_ohne_nebenwirkungen(monkeypatch):
    c = _conn()
    gesehen = []

    async def fake_dispatch(ev, conn=None, nebenwirkungen=True):
        gesehen.append((ev["type"], nebenwirkungen))
    monkeypatch.setattr(bot.bot, "_dispatch", fake_dispatch)
    bot._verhalten_auswerten(_hit("16:00:00", "A", "B"), c)
    _run(bot.bot._feed_erweiterungen(_dc("16:00:10", "A"), c))
    assert gesehen == [("combat_log", False)]
    assert bot.EmbedBuilder.build({"type": "combat_log", "player": "A", "gegner": "B", "sekunden": 10,
                                   "player_id": "p"}) is not None
    assert bot.EmbedBuilder.build({"type": "rage_quit", "player": "A", "killer": "B", "sekunden": 5,
                                   "player_id": "p"}) is not None
    assert bot.EmbedBuilder.build({"type": "killstreak", "player": "K", "victim": "V", "streak": 3,
                                   "player_id": "p"}) is not None
    for t in ("combat_log", "rage_quit", "killstreak"):
        assert t in bot.FEED_TYPES and bot._feed_key({"type": t}) == t


# ── Heatmaps ─────────────────────────────────────────────────────────
@pytest.fixture
def edb(tmp_path):
    return bot.EconomyDB(str(tmp_path / "heat.db"))


def test_heatmap_limit_reset_und_trennung(edb):
    for i in range(7):
        edb.heatmap_add("1000", "pvp_kill", i, i, "P", limit=5)
    edb.heatmap_add("2000", "pvp_kill", 99, 99, "Q", limit=5)
    pts = edb.heatmap_points("1000", "pvp_kill")
    assert len(pts) == 5 and (6.0, 6.0) in pts and (0.0, 0.0) not in pts
    assert edb.heatmap_counts("1000") == {"pvp_kill": 5}
    assert edb.heatmap_points("2000", "pvp_kill") == [(99.0, 99.0)]
    edb.heatmap_trim("1000", "pvp_kill", 2)
    assert len(edb.heatmap_points("1000", "pvp_kill")) == 2
    edb.heatmap_add("1000", "build", 1, 1, "P", limit=50)
    edb.heatmap_reset("1000", "pvp_kill")
    assert edb.heatmap_counts("1000") == {"build": 1}
    edb.heatmap_reset("1000")
    assert edb.heatmap_counts("1000") == {}
    assert edb.heatmap_counts("2000") == {"pvp_kill": 1}


def test_heatmap_typ_zuordnung():
    p = DayZLogParser()
    assert bot._heatmap_typ({"type": "kill_pvp"}) == "pvp_kill"
    assert bot._heatmap_typ({"type": "damage", "attacker": "X", "attacker_id": "a"}) == "pvp_hit"
    assert bot._heatmap_typ({"type": "damage", "attacker": "Umgebung", "attacker_id": "Umgebung"}) is None
    assert bot._heatmap_typ({"type": "kill_env", "cause": "ZmbM_Hermit", "raw": ""}) == "zombie_death"
    assert bot._heatmap_typ({"type": "kill_env", "cause": "Animal_UrsusArctos", "raw": ""}) == "animal_death"
    assert bot._heatmap_typ({"type": "kill_env", "cause": "FallDamage", "raw": ""}) == "env_death"
    bau = p.parse_line('23:08:31 | Player "C" (id=z pos=<1, 2, 3>) placed Fence Kit<FenceKit>')
    assert bot._heatmap_typ(bau) == "place"
    assert bot._heatmap_typ({"type": "connect"}) == "connect"
    assert bot._heatmap_typ({"type": "chat"}) is None
    c = _conn(heatmap_limits={"pvp_kill": 7})
    assert bot._heatmap_limit(c, "pvp_kill") == 7 and bot._heatmap_limit(c, "build") == 50


def test_heatmap_aufzeichnen_nur_mit_position(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    c = _conn()
    ev = c.parser.parse_line('00:31:17 | Player "A" (id=x pos=<100.0, 200.0, 3>) hit by Player "B" '
                             '(id=y pos=<101.0, 201.0, 3>) into Torso(7) for 4 damage (MeleeFist_Heavy)')
    _run(bot.bot._heatmap_aufzeichnen(ev, c))
    assert edb.heatmap_points("1000", "pvp_hit") == [(100.0, 200.0)]
    _run(bot.bot._heatmap_aufzeichnen({"type": "connect", "player": "Niemand"}, c))
    assert edb.heatmap_counts("1000") == {"pvp_hit": 1}


def test_draw_heatmap_smoke():
    PIL = pytest.importorskip("PIL")
    base = PIL.Image.new("RGB", (256, 256), (10, 10, 10))
    img = bot._draw_heatmap(base, 15360, [(1000, 2000), (1010, 2010), (14000, 500)], "Test")
    assert img.size == (256, 256)
    assert img.getpixel((16, 222)) != (10, 10, 10)     # (1000, 2000) -> Punkt eingefaerbt


# ── General-Validierung + Kopieren ───────────────────────────────────
def test_general_wert_pruefen():
    assert bot._general_wert_pruefen("keep_server_running", True) == (True, None)
    assert bot._general_wert_pruefen("keep_server_running", "ja")[1]
    assert bot._general_wert_pruefen("combat_log_seconds", 4)[1]
    assert bot._general_wert_pruefen("combat_log_seconds", "90") == (90, None)
    assert bot._general_wert_pruefen("map_link_provider", "xam") == ("xam", None)
    assert bot._general_wert_pruefen("map_link_style", "3d")[1]
    assert bot._general_wert_pruefen("ban_immune_role_ids", ["1", 2, "1"]) == (["1", "2"], None)
    assert bot._general_wert_pruefen("ban_immune_role_ids", list(range(6)))[1]
    assert bot._general_wert_pruefen("location_privacy_names", [" A ", "a", "B"]) == (["A", "B"], None)
    assert bot._general_wert_pruefen("heatmap_limits", {"pvp_kill": 10}) == ({"pvp_kill": 10}, None)
    assert bot._general_wert_pruefen("heatmap_limits", {"quatsch": 10})[1]
    assert bot._general_wert_pruefen("heatmap_limits", {"pvp_kill": 99999})[1]
    assert bot._general_wert_pruefen("unbekannt", 1)[1]


def test_general_payload_und_andere_server(monkeypatch):
    a = _conn("1000", 111, owner_discord_id="7", combat_log_seconds=90, ban_immune_role_ids=["5"])
    b = _conn("2000", 111, owner_discord_id="7")
    d = _conn("3000", 222, owner_discord_id="7")
    fremd = _conn("4000", 333, owner_discord_id="8")
    monkeypatch.setattr(bot.connections, "for_owner",
                        lambda uid: [c for c in (a, b, d, fremd) if c.data.get("owner_discord_id") == str(uid)])
    g = bot._general_payload(a)
    assert g["combat_log_seconds"] == 90 and g["ban_immune_role_ids"] == ["5"]
    assert g["long_range_kill_meter"] == 300 and g["heatmap_limits"]["pvp_kill"] == 50
    andere = bot._general_andere_server(a)
    assert [(x["service_id"], x["gleiche_guild"]) for x in andere] == [("2000", True), ("3000", False)]


def test_eigene_einstellungen_fallen_nicht_auf_betreiber_zurueck(monkeypatch):
    monkeypatch.setitem(bot.cfg.config, "long_range_kill_meter", 999)
    monkeypatch.setitem(bot.cfg.config, "keep_server_running", True)
    c = _conn()
    assert c.get("long_range_kill_meter") == 300
    assert c.get("keep_server_running") is False
    assert c.get("ban_immune_role_ids", []) == []
