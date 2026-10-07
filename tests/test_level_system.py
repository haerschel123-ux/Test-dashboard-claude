"""Level-System (Dashboard → Discord Management): XP durch Nachrichten, Kurve,
Aufstieg mit Erwähnung und Rollen, /level, /levelrangliste, /levelset,
/levelreset und die Dashboard-API.

    python3 -m pytest tests/test_level_system.py -q
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_airstrike import call, servers  # noqa: F401 - Fixture für pytest

GID = 7001


# ── Stubs ─────────────────────────────────────────────────────────────────
class _Role:
    def __init__(self, id_, name):
        self.id, self.name = id_, name


class _Member:
    def __init__(self, id_, roles=None, name="Spieler", bot_=False):
        self.id, self.name, self.display_name, self.bot = id_, name, name, bot_
        self.mention = f"<@{id_}>"
        self.roles = list(roles or [])
        self.display_avatar = None
        self.added, self.removed = [], []

    async def add_roles(self, rolle, reason=None):
        self.roles.append(rolle); self.added.append(rolle.id)

    async def remove_roles(self, rolle, reason=None):
        self.roles = [r for r in self.roles if r.id != rolle.id]; self.removed.append(rolle.id)

    def __str__(self):
        return self.name


class _Guild:
    def __init__(self, id_=GID, roles=(), members=(), channels=()):
        self.id, self.owner_id = id_, 1
        self._roles = {r.id: r for r in roles}
        self._members = {m.id: m for m in members}
        self._channels = {c.id: c for c in channels}
        self.text_channels = list(channels)

    def get_role(self, rid):
        return self._roles.get(int(rid))

    def get_member(self, uid):
        return self._members.get(int(uid))

    def get_channel(self, cid):
        return self._channels.get(int(cid))


class _Channel:
    def __init__(self, id_, name="allgemein", parent=None):
        self.id, self.name, self.parent, self.category, self.sent = id_, name, parent, None, []

    async def send(self, content=None, embed=None, **_):
        self.sent.append({"content": content, "embed": embed})


class _Message:
    def __init__(self, author, guild, channel):
        self.author, self.guild, self.channel = author, guild, channel


class _Response:
    def __init__(self):
        self.sent = []

    def is_done(self):
        return bool(self.sent)

    async def send_message(self, content=None, embed=None, ephemeral=False, **_):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class _Interaction:
    def __init__(self, user, guild, channel=None, guild_id=GID):
        self.user, self.guild, self.channel = user, guild, channel
        self.guild_id, self.locale = guild_id, None
        self.response = _Response()
        self.followup = _Response()
        self.command = None


@pytest.fixture
def level_setup(monkeypatch, servers, tmp_path):
    """Mandant 1000 an Guild GID, frische economy.db, aktiviertes Level-System."""
    a, _b = servers
    bot.connections.assign_guild("1000", GID)
    monkeypatch.setattr(bot, "db", bot.EconomyDB(str(tmp_path / "e.db")))
    a.data["level_system"] = {"enabled": True, "language": "de", "xp_min": 10, "xp_max": 10,
                              "cooldown": 60, "xp_basis": 100, "steigerung": 0,
                              "announce": True, "announce_channel_id": None,
                              "role_rewards": [{"level": 2, "role_id": "501"}, {"level": 3, "role_id": "502"}],
                              "stack_roles": True, "ignored_channels": ["999"], "ignored_roles": ["777"]}
    rollen = [_Role(501, "Bronze"), _Role(502, "Silber"), _Role(777, "Bot-Frei")]
    kanal = _Channel(100)
    guild = _Guild(roles=rollen, channels=[kanal, _Channel(999, "spam"), _Channel(300, "ankuendigungen")])
    monkeypatch.setattr(bot.bot, "get_channel", lambda cid: guild.get_channel(cid), raising=False)
    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild if int(gid) == GID else None, raising=False)
    monkeypatch.setattr(bot.bot, "is_ready", lambda: True, raising=False)
    monkeypatch.setattr(bot.random, "randint", lambda lo, hi: hi)
    return a, guild, kanal


def _run(coro):
    return asyncio.run(coro)


# ── Kurve ─────────────────────────────────────────────────────────────────
def test_kurve_und_level_aus_xp():
    assert [bot._level_xp_noetig(l, 100, 15) for l in (1, 2, 5, 10)] == [100, 115, 175, 352]
    assert bot._level_xp_noetig(1, 100, 0) == 100 and bot._level_xp_noetig(40, 100, 0) == 100
    werte = [bot._level_xp_noetig(l, 100, 15) for l in range(1, 60)]
    assert werte == sorted(werte)
    assert bot._level_aus_xp(0, 100, 15) == (1, 0, 100)
    assert bot._level_aus_xp(99, 100, 15) == (1, 99, 100)
    assert bot._level_aus_xp(100, 100, 15) == (2, 0, 115)
    assert bot._level_aus_xp(314, 100, 15) == (3, 99, 132)
    assert bot._level_gesamt_xp(3, 100, 15) == 215
    assert bot._level_aus_xp(100 * 10_000, 100, 0)[0] == bot._LEVEL_MAX   # Deckel bei 500
    assert bot._level_balken(50, 100) == ("█" * 10 + "░" * 10, 50)
    assert bot._level_balken(0, 100)[1] == 0 and bot._level_balken(100, 100)[0] == "█" * 20


def test_einstellungen_vorgaben_und_typen():
    conn = bot.ServerConnection({"service_id": "x"})
    e = bot._level_einstellungen(conn)
    assert e["enabled"] is False and e["xp_min"] == 15 and e["xp_max"] == 25 and e["role_rewards"] == []
    conn.data["level_system"] = {"enabled": 1, "xp_min": "7", "role_rewards": [{"level": "5", "role_id": 42}, {"level": 2, "role_id": 41}],
                                 "announce_channel_id": 123, "language": "xx"}
    e = bot._level_einstellungen(conn)
    assert e["enabled"] is True and e["xp_min"] == 7 and e["language"] == "de" and e["announce_channel_id"] == "123"
    assert e["role_rewards"] == [{"level": 2, "role_id": "41"}, {"level": 5, "role_id": "42"}]


# ── XP durch Nachrichten ──────────────────────────────────────────────────
def test_nachricht_xp_cooldown_ausnahmen_und_aufstieg(monkeypatch, level_setup):
    a, guild, kanal = level_setup
    nutzer = _Member(42)
    # Bot-Nachrichten, DMs, ignorierter Channel, ignorierte Rolle: keine XP
    _run(bot._level_nachricht_verarbeiten(_Message(_Member(1, bot_=True), guild, kanal)))
    _run(bot._level_nachricht_verarbeiten(_Message(nutzer, None, kanal)))
    _run(bot._level_nachricht_verarbeiten(_Message(nutzer, guild, guild.get_channel(999))))
    _run(bot._level_nachricht_verarbeiten(_Message(_Member(43, roles=[_Role(777, "x")]), guild, kanal)))
    assert bot.db.level_count(GID) == 0
    zeit = [1000.0]
    monkeypatch.setattr(bot.time, "time", lambda: zeit[0])
    # Erste Nachricht: 10 XP; zweite innerhalb der Abklingzeit: nichts
    _run(bot._level_nachricht_verarbeiten(_Message(nutzer, guild, kanal)))
    _run(bot._level_nachricht_verarbeiten(_Message(nutzer, guild, kanal)))
    assert bot.db.level_get(GID, 42)["xp"] == 10 and bot.db.level_get(GID, 42)["messages"] == 1
    # Abklingzeit ablaufen lassen: 9 weitere Nachrichten → 100 XP → Level 2 → Rolle Bronze + Ankündigung im selben Channel
    for _ in range(9):
        zeit[0] += 61
        _run(bot._level_nachricht_verarbeiten(_Message(nutzer, guild, kanal)))
    assert bot.db.level_get(GID, 42)["xp"] == 100 and bot.db.level_get(GID, 42)["level"] == 2
    assert nutzer.added == [501] and len(kanal.sent) == 1
    post = kanal.sent[0]
    assert post["content"] == "<@42>" and "Level 2" in post["embed"].title and "<@42>" in post["embed"].description
    assert "Bronze" in post["embed"].description and "Glückwunsch" in post["embed"].description
    # Deaktiviert: nichts mehr
    a.data["level_system"]["enabled"] = False
    zeit[0] += 61
    _run(bot._level_nachricht_verarbeiten(_Message(nutzer, guild, kanal)))
    assert bot.db.level_get(GID, 42)["xp"] == 100


def test_aufstieg_fester_channel_englisch_und_rollen_nur_hoechste(monkeypatch, level_setup):
    a, guild, kanal = level_setup
    a.data["level_system"].update({"language": "en", "announce_channel_id": "300", "stack_roles": False})
    nutzer = _Member(42, roles=[guild.get_role(501)])
    bot.db.level_set(GID, 42, 199, 2)
    _run(bot._level_nachricht_verarbeiten(_Message(nutzer, guild, kanal)))   # +10 → 209 → Level 3
    ziel = guild.get_channel(300)
    assert not kanal.sent and len(ziel.sent) == 1 and "Level up" in ziel.sent[0]["embed"].title
    assert "Congratulations <@42>" in ziel.sent[0]["embed"].description
    assert nutzer.added == [502] and nutzer.removed == [501]   # nur die höchste bleibt
    # Ankündigung aus: Rollen trotzdem
    a.data["level_system"].update({"announce": False, "stack_roles": True})
    anderer = _Member(44)
    bot.db.level_set(GID, 44, 299, 3)
    _run(bot._level_nachricht_verarbeiten(_Message(anderer, guild, kanal)))
    assert len(ziel.sent) == 1 and sorted(anderer.added) == [501, 502]


# ── Befehle ───────────────────────────────────────────────────────────────
def test_level_befehl_embed_rang_und_fremder_nutzer(level_setup):
    a, guild, kanal = level_setup
    ich, anderer = _Member(42, name="Ich"), _Member(43, name="Du")
    bot.db.level_set(GID, 42, 150, 2)
    bot.db.level_set(GID, 43, 500, 5)
    inter = _Interaction(ich, guild, kanal)
    _run(bot.cmd_level.callback(inter))
    embed = inter.response.sent[0]["embed"]
    assert embed.title == "🏆 Level 2" and inter.response.sent[0]["ephemeral"] is False
    werte = {f.name: f.value for f in embed.fields}
    assert werte["🥇 Rang"] == "#2 / 2" and werte["✨ XP"] == "50 / 100"
    assert "██████████░░░░░░░░░░" in werte["Fortschritt zu Level 3"] and "50 %" in werte["Fortschritt zu Level 3"]
    assert embed.footer.text == "Gesamt-XP: 150"
    inter = _Interaction(ich, guild, kanal)
    _run(bot.cmd_level.callback(inter, user=anderer))
    assert inter.response.sent[0]["embed"].title == "🏆 Level 6"   # 500 XP bei 100 je Level
    # Englische Discord-Sprache → englische Beschriftung
    inter = _Interaction(ich, guild, kanal); inter.locale = bot.discord.Locale.british_english
    _run(bot.cmd_level.callback(inter))
    assert any(f.name == "🥇 Rank" for f in inter.response.sent[0]["embed"].fields)
    # Deaktiviert → Hinweis
    a.data["level_system"]["enabled"] = False
    inter = _Interaction(ich, guild, kanal)
    _run(bot.cmd_level.callback(inter))
    assert inter.response.sent[0]["ephemeral"] is True and "nicht aktiviert" in inter.response.sent[0]["content"]


def test_levelrangliste(level_setup):
    a, guild, kanal = level_setup
    for uid, xp in ((42, 150), (43, 500), (44, 20)):
        bot.db.level_set(GID, uid, xp, 1)
    guild._members = {42: _Member(42, name="Ich")}
    inter = _Interaction(_Member(42), guild, kanal)
    _run(bot.cmd_levelrangliste.callback(inter))
    text = inter.response.sent[0]["embed"].description
    zeilen = text.splitlines()
    assert zeilen[0].startswith("🥇 <@43>") and "Level **6**" in zeilen[0] and "500 XP" in zeilen[0]
    assert zeilen[1].startswith("🥈 <@42>") and zeilen[2].startswith("🥉 <@44>")
    assert inter.response.sent[0]["embed"].footer.text == "3 Teilnehmer"


def test_levelset_und_levelreset_mit_rechten(monkeypatch, level_setup):
    a, guild, kanal = level_setup
    admin, ziel = _Member(1, name="Admin"), _Member(42, name="Opfer")
    inter = _Interaction(admin, guild, kanal)
    _run(bot.cmd_levelset.callback(inter, user=ziel, xp=250))
    assert "Keine Berechtigung" in inter.response.sent[0]["content"] and bot.db.level_get(GID, 42)["xp"] == 0
    monkeypatch.setattr(bot, "_is_admin", lambda interaction: True)
    inter = _Interaction(admin, guild, kanal)
    _run(bot.cmd_levelset.callback(inter, user=ziel, xp=250))
    assert bot.db.level_get(GID, 42) == {"xp": 250, "level": 3, "messages": 0, "last_xp_at": 0.0}
    assert sorted(ziel.added) == [501, 502] and "250 XP" in inter.response.sent[0]["content"]
    inter = _Interaction(admin, guild, kanal)
    _run(bot.cmd_levelset.callback(inter, user=ziel, xp=-5))
    assert "zwischen 0 und" in inter.response.sent[0]["content"]
    inter = _Interaction(admin, guild, kanal)
    _run(bot.cmd_levelreset.callback(inter, user=ziel))
    assert bot.db.level_get(GID, 42)["xp"] == 0 and sorted(ziel.removed) == [501, 502]
    assert "zurückgesetzt" in inter.response.sent[0]["content"]


# ── Dashboard-API ─────────────────────────────────────────────────────────
def test_api_get_post_validierung_und_mandanten(monkeypatch, level_setup):
    a, guild, kanal = level_setup
    _, b = bot.connections._conns["1000"], bot.connections._conns["2000"]
    bot.db.level_set(GID, 42, 10, 1)
    status, result = call(monkeypatch, a, bot.get_discord_mgmt_level)
    assert status == 200 and result["data"]["enabled"] is True and result["data"]["teilnehmer"] == 1
    assert result["data"]["role_rewards"][0] == {"level": 2, "role_id": "501"}
    gut = {"enabled": True, "language": "en", "xp_min": 5, "xp_max": 9, "cooldown": 30, "xp_basis": 120, "steigerung": 10,
           "announce": True, "announce_channel_id": "300",
           "role_rewards": [{"level": 10, "role_id": "502"}, {"level": 4, "role_id": "501"}], "stack_roles": False,
           "ignored_channels": ["999"], "ignored_roles": ["777"]}
    status, result = call(monkeypatch, a, bot.post_discord_mgmt_level, gut)
    assert status == 200, result
    gespeichert = a.data["level_system"]
    assert gespeichert["role_rewards"] == [{"level": 4, "role_id": "501"}, {"level": 10, "role_id": "502"}]
    assert gespeichert["announce_channel_id"] == "300" and gespeichert["language"] == "en" and gespeichert["stack_roles"] is False
    assert result["data"]["xp_max"] == 9
    # Fehlerfälle → 400
    for bad in (dict(xp_min=20, xp_max=10), dict(xp_min=0), dict(cooldown=5000), dict(language="fr"),
                dict(role_rewards=[{"level": 2, "role_id": "501"}, {"level": 2, "role_id": "502"}]),
                dict(role_rewards=[{"level": 0, "role_id": "501"}]), dict(role_rewards=[{"level": 2, "role_id": "9"}]),
                dict(announce_channel_id="12345"), dict(ignored_roles=["9"]), dict(steigerung="abc")):
        status, _ = call(monkeypatch, a, bot.post_discord_mgmt_level, {**gut, **bad})
        assert status == 400, bad
    # Mandant ohne Guild: aktivieren → 409, deaktiviert speichern geht
    status, _ = call(monkeypatch, b, bot.post_discord_mgmt_level, {**gut, "announce_channel_id": None, "role_rewards": [], "ignored_channels": [], "ignored_roles": []})
    assert status == 409
    status, _ = call(monkeypatch, b, bot.post_discord_mgmt_level, {"enabled": False, "xp_min": 1, "xp_max": 2})
    assert status == 200 and b.data["level_system"]["enabled"] is False
    # Kachel-Payload zeigt den Status
    assert bot._discord_mgmt_payload(a)["level_system"] == {"enabled": True}
    assert bot._discord_mgmt_payload(b)["level_system"] == {"enabled": False}
