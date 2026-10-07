"""Honeypot (Dashboard → Discord Management): Köder-Channel, DM + Kick/Bann,
Ausnahmen, Log, Warnung posten, Dashboard-API.

    python3 -m pytest tests/test_honeypot.py -q
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_airstrike import call, servers  # noqa: F401 - Fixture für pytest

GID = 7101


class _Perms:
    def __init__(self, administrator=False, manage_guild=False):
        self.administrator, self.manage_guild = administrator, manage_guild


class _Role:
    def __init__(self, id_, name):
        self.id, self.name = id_, name


class _Member:
    def __init__(self, id_, roles=None, name="Spieler", bot_=False, perms=None, dm_ok=True):
        self.id, self.name, self.display_name, self.bot = id_, name, name, bot_
        self.mention = f"<@{id_}>"
        self.roles = list(roles or [])
        self.guild_permissions = perms or _Perms()
        self.dms, self.kicked, self._dm_ok = [], False, dm_ok

    async def send(self, embed=None, **_):
        if not self._dm_ok:
            raise RuntimeError("Cannot send messages to this user")
        self.dms.append(embed)

    async def kick(self, reason=None):
        self.kicked = reason

    def __str__(self):
        return self.name


class _Msg:
    def __init__(self, id_=5000):
        self.id, self.deleted = id_, False

    async def delete(self):
        self.deleted = True


class _Channel:
    def __init__(self, id_, name="honeypot"):
        self.id, self.name, self.category, self.mention = id_, name, None, f"<#{id_}>"
        self.sent, self._msgs, self._next = [], {}, 9000

    async def send(self, content=None, embed=None, **_):
        self._next += 1
        m = _Msg(self._next); self._msgs[m.id] = m
        self.sent.append({"content": content, "embed": embed, "message": m})
        return m

    async def fetch_message(self, mid):
        if int(mid) not in self._msgs:
            raise RuntimeError("not found")
        return self._msgs[int(mid)]


class _Guild:
    def __init__(self, channels, roles=(), owner_id=1):
        self.id, self.name, self.owner_id = GID, "Testguild", owner_id
        self._channels = {c.id: c for c in channels}; self.text_channels = list(channels)
        self._roles = {r.id: r for r in roles}
        self.bans = []

    def get_channel(self, cid):
        return self._channels.get(int(cid))

    def get_role(self, rid):
        return self._roles.get(int(rid))

    async def ban(self, user, reason=None, delete_message_seconds=None, delete_message_days=None):
        self.bans.append({"user": user.id, "reason": reason, "seconds": delete_message_seconds})


class _Message:
    def __init__(self, author, guild, channel):
        self.author, self.guild, self.channel, self.deleted = author, guild, channel, False

    async def delete(self):
        self.deleted = True


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def honeypot_setup(monkeypatch, servers):
    a, _b = servers
    bot.connections.assign_guild("1000", GID)
    honig, log_kanal, normal = _Channel(100, "honeypot"), _Channel(200, "log"), _Channel(300, "allgemein")
    guild = _Guild([honig, log_kanal, normal], roles=[_Role(501, "Mod"), _Role(502, "VIP")])
    a.data["honeypot"] = {"enabled": True, "channel_id": "100", "language": "de", "action": "ban",
                          "delete_messages": True, "exempt_roles": ["502"], "log_channel_id": "200",
                          "warning_message_id": None, "custom_text": "Einspruch per Ticket."}
    monkeypatch.setattr(bot.bot, "get_channel", lambda cid: guild.get_channel(cid), raising=False)
    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild if int(gid) == GID else None, raising=False)
    monkeypatch.setattr(bot.bot, "is_ready", lambda: True, raising=False)
    return a, guild, honig, log_kanal, normal


def test_einstellungen_und_embeds():
    conn = bot.ServerConnection({"service_id": "x"})
    e = bot._honeypot_einstellungen(conn)
    assert e["enabled"] is False and e["action"] == "ban" and e["delete_messages"] is True
    conn.data["honeypot"] = {"action": "kick", "channel_id": 123, "exempt_roles": [1, None], "custom_text": "x" * 400, "language": "en"}
    e = bot._honeypot_einstellungen(conn)
    assert e["action"] == "kick" and e["channel_id"] == "123" and e["exempt_roles"] == ["1"] and len(e["custom_text"]) == 300
    w = bot._honeypot_warnung_embed("de", "ban", "Zusatz")
    assert "Hier NICHT schreiben" in w.title and "gebannt" in w.description and "Zusatz" in w.description
    w = bot._honeypot_warnung_embed("en", "kick", "")
    assert "Do NOT write" in w.title and "kicked" in w.description
    d = bot._honeypot_dm_embed("Guild", "en", "ban", "")
    assert "banned from Guild" in d.title and "honeypot channel" in d.description


def test_treffer_bann_mit_dm_log_und_loeschen(honeypot_setup):
    a, guild, honig, log_kanal, normal = honeypot_setup
    bot_user = _Member(2, bot_=True)
    assert _run(bot._honeypot_nachricht_verarbeiten(_Message(bot_user, guild, honig))) is False
    assert _run(bot._honeypot_nachricht_verarbeiten(_Message(_Member(3), None, honig))) is False
    assert _run(bot._honeypot_nachricht_verarbeiten(_Message(_Member(3), guild, normal))) is False
    # Ausnahmen: Admin, Server verwalten, Eigentümer, ausgenommene Rolle
    for ausgenommen in (_Member(4, perms=_Perms(administrator=True)), _Member(5, perms=_Perms(manage_guild=True)),
                        _Member(1), _Member(6, roles=[_Role(502, "VIP")])):
        assert _run(bot._honeypot_nachricht_verarbeiten(_Message(ausgenommen, guild, honig))) is False
    assert guild.bans == [] and honig.sent == []
    # Treffer
    spam = _Member(42, name="SpamBot")
    nachricht = _Message(spam, guild, honig)
    assert _run(bot._honeypot_nachricht_verarbeiten(nachricht)) is True
    assert nachricht.deleted and len(spam.dms) == 1
    assert "gebannt" in spam.dms[0].title and "Einspruch per Ticket." in spam.dms[0].description
    assert guild.bans == [{"user": 42, "reason": "Honeypot: Nachricht im Köder-Channel (Bot-Erkennung)", "seconds": 86400}]
    assert len(log_kanal.sent) == 1 and "Honeypot-Treffer – gebannt" in log_kanal.sent[0]["embed"].title
    felder = {f.name: f.value for f in log_kanal.sent[0]["embed"].fields}
    assert "<@42>" in felder["Nutzer"] and felder["DM zugestellt"] == "ja"


def test_treffer_kick_englisch_ohne_loeschen_und_dm_geschlossen(honeypot_setup):
    a, guild, honig, log_kanal, normal = honeypot_setup
    a.data["honeypot"].update({"action": "kick", "language": "en", "delete_messages": False, "log_channel_id": None})
    spam = _Member(43, name="Spam2", dm_ok=False)
    assert _run(bot._honeypot_nachricht_verarbeiten(_Message(spam, guild, honig))) is True
    assert spam.kicked and guild.bans == [] and spam.dms == [] and log_kanal.sent == []
    # Bann ohne Löschen → 0 Sekunden
    a.data["honeypot"].update({"action": "ban"})
    spam = _Member(44)
    assert _run(bot._honeypot_nachricht_verarbeiten(_Message(spam, guild, honig))) is True
    assert guild.bans[-1]["seconds"] == 0
    # Deaktiviert → nichts
    a.data["honeypot"]["enabled"] = False
    spam = _Member(45)
    assert _run(bot._honeypot_nachricht_verarbeiten(_Message(spam, guild, honig))) is False and spam.kicked is False


def test_on_message_reihenfolge_treffer_bekommt_keine_xp(monkeypatch, honeypot_setup, tmp_path):
    a, guild, honig, log_kanal, normal = honeypot_setup
    monkeypatch.setattr(bot, "db", bot.EconomyDB(str(tmp_path / "e.db")))
    a.data["level_system"] = {"enabled": True, "xp_min": 10, "xp_max": 10, "cooldown": 0}
    spam = _Member(46)
    _run(bot.bot.on_message(_Message(spam, guild, honig)))
    assert guild.bans and bot.db.level_get(GID, 46)["xp"] == 0
    _run(bot.bot.on_message(_Message(_Member(47), guild, normal)))
    assert bot.db.level_get(GID, 47)["xp"] == 10


def test_api_get_post_warnung_repost_und_validierung(monkeypatch, honeypot_setup):
    a, guild, honig, log_kanal, normal = honeypot_setup
    _, b = bot.connections._conns["1000"], bot.connections._conns["2000"]
    status, result = call(monkeypatch, a, bot.get_discord_mgmt_honeypot)
    assert status == 200 and result["data"]["channel_id"] == "100" and result["data"]["warning_posted"] is False
    gut = {"enabled": True, "channel_id": "100", "language": "en", "action": "kick", "delete_messages": False,
           "exempt_roles": ["501"], "log_channel_id": "200", "custom_text": "Appeal via ticket."}
    status, result = call(monkeypatch, a, bot.post_discord_mgmt_honeypot, gut)
    assert status == 200, result
    assert result["data"]["hinweis"] is None and result["data"]["warning_posted"] is True
    assert len(honig.sent) == 1 and "Do NOT write" in honig.sent[0]["embed"].title and "kicked" in honig.sent[0]["embed"].description
    assert a.data["honeypot"]["warning_message_id"] == str(honig.sent[0]["message"].id)
    assert a.data["honeypot"]["exempt_roles"] == ["501"] and a.data["honeypot"]["action"] == "kick"
    # Unverändert speichern → keine neue Warnung
    status, _ = call(monkeypatch, a, bot.post_discord_mgmt_honeypot, gut)
    assert status == 200 and len(honig.sent) == 1
    # Sprache ändern → alte Warnung gelöscht, neue gepostet
    status, _ = call(monkeypatch, a, bot.post_discord_mgmt_honeypot, {**gut, "language": "de"})
    assert status == 200 and len(honig.sent) == 2 and honig.sent[0]["message"].deleted
    # Repost
    status, _ = call(monkeypatch, a, bot.post_discord_mgmt_honeypot_repost, {})
    assert status == 200 and len(honig.sent) == 3 and honig.sent[1]["message"].deleted
    # Fehlerfälle
    for bad in (dict(action="mute"), dict(language="fr"), dict(channel_id="999"), dict(log_channel_id="100"),
                dict(exempt_roles=["9"]), dict(custom_text="x" * 301), dict(channel_id=None)):
        status, _ = call(monkeypatch, a, bot.post_discord_mgmt_honeypot, {**gut, **bad})
        assert status == 400, bad
    # Mandant ohne Guild: Channel → 409; deaktiviert ohne Channel geht
    status, _ = call(monkeypatch, b, bot.post_discord_mgmt_honeypot, {"enabled": False, "channel_id": "100"})
    assert status == 409
    status, _ = call(monkeypatch, b, bot.post_discord_mgmt_honeypot, {"enabled": False})
    assert status == 200 and b.data["honeypot"]["enabled"] is False
    assert bot._discord_mgmt_payload(a)["honeypot"] == {"enabled": True}
