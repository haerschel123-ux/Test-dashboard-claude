"""Zonen-Editor (Vorbild DayZ++): Datenmodell-Vorgaben, Validierung,
Ereignis-Zuordnung, Filterlisten, Entry/Leave, Auto-Bans innerhalb und
ausserhalb der Zonen, Ban-Immunitaet, Temp-Unban, Mandantentrennung.

    python3 -m pytest tests/test_zonen_editor.py -q
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
from log_parser import DayZLogParser  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeApi:
    def __init__(self, bans=""):
        self.settings = {"general": {"bans": bans}}
        self.written = []

    async def get_settings(self):
        return self.settings

    async def set_setting(self, cat, key, val):
        self.settings[cat][key] = val
        self.written.append(val)
        return True, "ok"


def _conn(sid="1000", gid=111, bans=""):
    c = bot.ServerConnection({"service_id": sid, "guild_id": gid, "map_name": "ChernarusPlus"})
    c.api = FakeApi(bans)
    c.parser = DayZLogParser()
    return c


@pytest.fixture(autouse=True)
def _ruhe(monkeypatch):
    monkeypatch.setattr(bot.cfg, "save_bans", lambda: None)
    monkeypatch.setattr(bot.cfg, "bans", {})
    monkeypatch.setattr(bot, "_audit_add", lambda *a, **k: None)
    posts = []

    async def fake_post_feed(gid, typ, embed, content=None, channel_id=None, service_id=None, **k):
        posts.append({"gid": gid, "typ": typ, "embed": embed, "channel_id": channel_id,
                      "service_id": service_id})
        return True, "sent"
    monkeypatch.setattr(bot, "_post_feed", fake_post_feed)
    monkeypatch.setattr(bot, "_offenes_kopfgeld", lambda gid, name: 0)
    bot.bot._zone_drin.clear()
    bot.bot._zone_ban_zuletzt.clear()
    bot.bot._zone_ev_ping.clear()
    bot.bot._zone_last_ping.clear()
    yield posts


# ── Datenmodell ───────────────────────────────────────────────────────
def test_altzone_bekommt_vorgaben_ohne_umschreiben():
    alt = {"name": "Alt", "x": 1, "z": 2, "radius": 50, "role_id": 5}
    p = bot._zone_payload(alt)
    assert p["active"] is True and p["color"] == "9B59B6" and p["ping_on_detect"] is True
    assert p["ban_events"] == [] and p["lists"] == [] and p["manager_ids"] == []
    assert p["temp_ban_hours"] == 24 and p["ping_role_ids"] == ["5"]
    assert "active" not in alt     # gespeicherter Eintrag unveraendert


def test_felder_uebernehmen_validiert():
    z = {"name": "A"}
    assert bot._zone_felder_uebernehmen(z, {"active": "ja"}) is not None
    assert bot._zone_felder_uebernehmen(z, {"color": "rot"}) is not None
    assert bot._zone_felder_uebernehmen(z, {"temp_ban_hours": 0}) is not None
    assert bot._zone_felder_uebernehmen(z, {"ban_events": ["gibtsnicht"]}) is not None
    assert bot._zone_felder_uebernehmen(z, {"manager_ids": list(range(6))}) is not None
    assert bot._zone_felder_uebernehmen(z, {"lists": [{"mode": "x", "field": "player", "values": []}]}) is not None
    assert bot._zone_felder_uebernehmen(z, {
        "active": False, "color": "#ff00aa", "ping_payout": 50, "temp_ban": True,
        "temp_ban_hours": 2, "ban_events": ["entry", "entry", "kill"],
        "manager_ids": ["42", 43],
        "lists": [{"name": "Waffen", "mode": "exclude", "field": "weapon", "values": ["M4A1", " m4a1 "]}],
    }) is None
    assert z["color"] == "FF00AA" and z["ban_events"] == ["entry", "kill"]
    assert z["manager_ids"] == ["42", "43"] and z["lists"][0]["values"] == ["M4A1"]


def test_geometrie_grenzen():
    assert bot._validate_zone_geometry(100, 100, 1) is None
    assert bot._validate_zone_geometry(100, 100, 15000) is None
    assert bot._validate_zone_geometry(100, 100, 0.5) is not None
    assert bot._validate_zone_geometry(100, 100, 15001) is not None
    assert bot._validate_zone_geometry(15361, 100, 50, welt=15360) is not None
    assert bot._validate_zone_geometry(15360, 100, 50, welt=15360) is None


def test_allowlist_bis_5000():
    namen = [f"n{i}" for i in range(6000)]
    assert len(bot._allowlist_aus_anfrage({"allowlist": namen})) == 5000


# ── Ereignis-Zuordnung + Filter ───────────────────────────────────────
def test_ereignis_schluessel():
    p = DayZLogParser()
    hit = p.parse_line('00:31:17 | Player "A" (id=x pos=<1, 2, 3>) hit by Player "B" (id=y pos=<1, 2, 3>) into Torso(7) for 4 damage (MeleeFist_Heavy)')
    assert bot._zone_ereignis_schluessel(hit) == {"hit"}
    kill = {"type": "kill_pvp", "distance": "10"}
    assert bot._zone_ereignis_schluessel(kill) == {"kill", "kill_ignore_bounty"}
    assert bot._zone_ereignis_schluessel(kill, kopfgeld_offen=True) == {"kill_ignore_bounty"}
    assert bot._zone_ereignis_schluessel({"type": "connect"}) == {"login", "connect"}
    bau = p.parse_line('23:08:31 | Player "C" (id=z pos=<1, 2, 3>) placed Fence Kit<FenceKit>')
    assert bot._zone_ereignis_schluessel(bau) == {"place"}
    umwelt = {"type": "damage", "attacker": "Umgebung", "attacker_id": "Umgebung"}
    assert bot._zone_ereignis_schluessel(umwelt) == set()
    assert "explosion_suicide_death" in bot._zone_ereignis_schluessel(
        {"type": "suicide", "raw": 'Player "X" blew themselves up'})


def test_filter_ignoriert_erlaubt_und_listen():
    z = {"name": "Z", "ignored_events": ["hit"], "allowed_events": [],
         "lists": [{"mode": "exclude", "field": "player", "values": ["Admin"]},
                   {"mode": "include", "field": "weapon", "values": ["M4A1"]}]}
    assert not bot._zone_ereignis_erlaubt(z, "hit", "Bob", {"weapon": "M4A1"})
    assert not bot._zone_ereignis_erlaubt(z, "kill", "admin", {"weapon": "M4A1"})
    assert not bot._zone_ereignis_erlaubt(z, "kill", "Bob", {"weapon": "AKM"})
    assert bot._zone_ereignis_erlaubt(z, "kill", "Bob", {"weapon": "M4A1"})
    z2 = {"name": "Z", "allowed_events": ["entry"]}
    assert bot._zone_ereignis_erlaubt(z2, "entry", "Bob", None)
    assert not bot._zone_ereignis_erlaubt(z2, "leave", "Bob", None)


# ── Entry/Leave ───────────────────────────────────────────────────────
def test_entry_und_leave_ueber_positionen(_ruhe):
    posts = _ruhe
    c = _conn()
    c.data["zones"] = [{"name": "Base", "x": 1000, "z": 1000, "radius": 100, "guild_id": 111,
                        "channel_id": 5, "verbose": True}]
    c.parser._set_position("Bob", "id1", "1010, 1010, 5")
    _run(bot.bot._check_zones(c))              # hydriert -> noch nicht bewertet
    c.parser._set_position("Bob", "id1", "1010, 1010, 5")
    _run(bot.bot._check_zones(c))
    assert ("1000", "base", "Bob") in bot.bot._zone_drin
    assert any("Betreten" in str(p["embed"].title) or "Detection" in str(p["embed"].title) for p in posts)
    # Entry-Ping setzt den Cooldown -> kein zweiter Ping im selben Durchlauf
    assert len(posts) == 1
    posts.clear()
    c.parser._set_position("Bob", "id1", "2000, 2000, 5")
    _run(bot.bot._check_zones(c))
    assert ("1000", "base", "Bob") not in bot.bot._zone_drin
    assert posts and "Verlassen" in str(posts[0]["embed"].title)
    posts.clear()
    # Verschwinden aus dem Tracking = still, kein Leave
    c.parser._set_position("Bob", "id1", "1010, 1010, 5")
    _run(bot.bot._check_zones(c))
    posts.clear()
    c.parser.player_positions.pop("Bob")
    _run(bot.bot._check_zones(c))
    assert not posts and ("1000", "base", "Bob") not in bot.bot._zone_drin


def test_inaktive_zone_schweigt(_ruhe):
    c = _conn()
    c.data["zones"] = [{"name": "Aus", "x": 1000, "z": 1000, "radius": 100, "guild_id": 111,
                        "channel_id": 5, "active": False}]
    for _ in range(2):
        c.parser._set_position("Bob", "id1", "1010, 1010, 5")
        _run(bot.bot._check_zones(c))
    assert not _ruhe and not bot.bot._zone_drin


# ── Auto-Bans ─────────────────────────────────────────────────────────
HIT = ('00:31:17 | Player "Opfer" (id=o pos=<5000.0, 5000.0, 10>) hit by Player "Taeter" '
       '(id=t pos=<5005.0, 5005.0, 10>) into Torso(7) for 4.275 damage (MeleeFist_Heavy)')


def test_ban_in_zone_bei_hit_mit_temp_ban(_ruhe):
    c = _conn(bans="Alt")
    c.data["zones"] = [{"name": "Safe", "x": 5000, "z": 5000, "radius": 50, "guild_id": 111,
                        "channel_id": 5, "ban_events": ["hit"], "temp_ban": True, "temp_ban_hours": 3}]
    ev = c.parser.parse_line(HIT)
    _run(bot.bot._zonen_ereignis(ev, c))
    assert c.api.written and c.api.written[-1] == "Alt\r\nTaeter"
    meta = bot._bans_of(c)["Taeter"]
    assert meta["zone"] == "Safe" and meta["ereignis"] == "hit"
    assert 2.9 * 3600 < meta["expires_at"] - time.time() <= 3 * 3600
    titel = [str(p["embed"].title) for p in _ruhe]
    assert any("Auto-Ban" in t for t in titel)
    # Dedupe: zweiter Treffer bannt nicht erneut
    n = len(c.api.written)
    _run(bot.bot._zonen_ereignis(ev, c))
    assert len(c.api.written) == n


def test_hit_zone_bannt_ausserhalb_ausser_whitelist(_ruhe):
    c = _conn()
    c.data["zones"] = [{"name": "PvP", "x": 100, "z": 100, "radius": 50, "guild_id": 111,
                        "channel_id": 5, "hit_outside_ban": True, "allowlist": ["Erlaubt"]}]
    ev = c.parser.parse_line(HIT)          # Taeter steht bei 5005/5005 -> ausserhalb
    _run(bot.bot._zonen_ereignis(ev, c))
    assert "Taeter" in bot._bans_of(c)
    assert "Treffer außerhalb" in bot._bans_of(c)["Taeter"]["reason"]
    # Whitelist-Spieler wird nicht gebannt
    ev2 = c.parser.parse_line(HIT.replace('"Taeter"', '"Erlaubt"'))
    _run(bot.bot._zonen_ereignis(ev2, c))
    assert "Erlaubt" not in bot._bans_of(c)
    # Innerhalb einer Zone: kein Outside-Ban
    c.data["zones"].append({"name": "Hier", "x": 5000, "z": 5000, "radius": 100, "guild_id": 111,
                            "channel_id": 5})
    ev3 = c.parser.parse_line(HIT.replace('"Taeter"', '"Drinnen"'))
    _run(bot.bot._zonen_ereignis(ev3, c))
    assert "Drinnen" not in bot._bans_of(c)


def test_kein_ban_bei_umwelt_treffer(_ruhe):
    c = _conn()
    c.data["zones"] = [{"name": "PvP", "x": 100, "z": 100, "radius": 50, "guild_id": 111,
                        "channel_id": 5, "hit_outside_ban": True}]
    ev = {"type": "damage", "victim": "Opfer", "attacker": "Umgebung", "attacker_id": "Umgebung",
          "position": "5000, 5000, 1", "attacker_position": None}
    _run(bot.bot._zonen_ereignis(ev, c))
    assert not bot._bans_of(c)


def test_ban_immunitaet(_ruhe, monkeypatch):
    c = _conn()
    c.data["ban_immune_role_ids"] = ["777"]
    c.data["zones"] = [{"name": "Safe", "x": 5000, "z": 5000, "radius": 50, "guild_id": 111,
                        "channel_id": 5, "ban_events": ["hit"]}]
    monkeypatch.setattr(bot.db, "links_for_name", lambda name, gid=None: [{"user_id": 9}])

    async def rolle(gid, uid, rid):
        return rid == 777
    monkeypatch.setattr(bot, "_user_hat_rolle", rolle)
    ev = c.parser.parse_line(HIT)
    _run(bot.bot._zonen_ereignis(ev, c))
    assert not c.api.written and not bot._bans_of(c)


def test_temp_unban_hebt_nur_abgelaufene_auf(_ruhe, monkeypatch):
    c = _conn(bans="Alt\r\nNeu\r\nDauer")
    eimer = bot._bans_of(c)
    eimer["Alt"] = {"name": "Alt", "expires_at": time.time() - 10}
    eimer["Neu"] = {"name": "Neu", "expires_at": time.time() + 3600}
    eimer["Dauer"] = {"name": "Dauer"}
    monkeypatch.setattr(bot.connections, "all", lambda: [c])
    _run(bot.bot._temp_unban_once())
    assert c.api.written[-1] == "Neu\r\nDauer"
    assert "Alt" not in eimer and "Neu" in eimer and "Dauer" in eimer
    assert any("abgelaufen" in str(p["embed"].title) for p in _ruhe)


def test_ban_kerne_lesen_vor_schreiben():
    c = _conn()

    async def kaputt():
        return None
    c.api.get_settings = kaputt
    hinzu, schon, fehler = _run(bot._ban_namen_hinzufuegen(c, ["X"], "g", "v"))
    assert fehler and not hinzu and not c.api.written
    entfernt, nf, fehler = _run(bot._ban_namen_entfernen(c, ["X"]))
    assert fehler and not entfernt


# ── Mandantentrennung ─────────────────────────────────────────────────
def test_zonen_von_kunde_a_nicht_bei_kunde_b(_ruhe):
    a = _conn("1000", 111)
    b = _conn("2000", 222)
    a.data["zones"] = [{"name": "NurA", "x": 5000, "z": 5000, "radius": 50, "guild_id": 111,
                        "channel_id": 5, "ban_events": ["hit"]}]
    ev = b.parser.parse_line(HIT)
    _run(bot.bot._zonen_ereignis(ev, b))
    assert not b.api.written and not bot._bans_of(b)
    assert bot._zones(b) == []


def test_payload_liefert_discord_ids_als_strings():
    """Snowflakes > 2^53 wuerden in JavaScript gerundet (…000) – die API gibt
    Channel-, Guild- und Rollen-IDs deshalb als Strings aus."""
    z = {"name": "Groß", "channel_id": 1521459827031675123, "guild_id": 1398765432109876543,
         "ping_role_ids": [1521459827031675999, "7"], "manage_role_ids": [1521459827031676001],
         "role_id": 1521459827031675555}
    p = bot._zone_payload(z)
    assert p["channel_id"] == "1521459827031675123" and p["guild_id"] == "1398765432109876543"
    assert p["ping_role_ids"] == ["1521459827031675999", "7"]
    assert p["manage_role_ids"] == ["1521459827031676001"] and p["role_id"] == "1521459827031675555"
    # intern bleibt die Zone unveraendert (int)
    assert z["channel_id"] == 1521459827031675123
    # Altbestand ohne Channel bleibt ohne Channel
    assert "channel_id" not in bot._zone_payload({"name": "Alt"})
