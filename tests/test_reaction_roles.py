"""Tests fuer die Reaction-Roles-Bausteine (Discord Management).

Kein echter Discord-Login noetig: der Link-Parser ist eine reine Funktion,
und der Emoji-Vergleich in _reaction_role_anwenden laeuft ueber einen
simplen String-Vergleich mit str(payload.emoji) - beides mit einem Stub
pruefbar (siehe CLAUDE.md: kein echter Discord-Login im Test moeglich).

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/ -v
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def test_link_parsen_gueltiger_link():
    ergebnis = bot._discord_message_link_parsen(
        "https://discord.com/channels/111/222/333")
    assert ergebnis == (111, 222, 333)


def test_link_parsen_canary_und_ptb_subdomains():
    assert bot._discord_message_link_parsen(
        "https://canary.discord.com/channels/1/2/3") == (1, 2, 3)
    assert bot._discord_message_link_parsen(
        "https://ptb.discordapp.com/channels/1/2/3") == (1, 2, 3)


def test_link_parsen_ungueltiger_link():
    assert bot._discord_message_link_parsen("") is None
    assert bot._discord_message_link_parsen("https://example.com/nix") is None
    assert bot._discord_message_link_parsen("nur ein Text ohne Link") is None


def test_ensure_reaction_role_ids_vergibt_fortlaufende_ids():
    eintraege = [{"id": None, "emoji": "a"}, {"id": 5, "emoji": "b"}, {"id": None, "emoji": "c"}]
    geaendert = bot._ensure_reaction_role_ids(eintraege)
    assert geaendert is True
    ids = [e["id"] for e in eintraege]
    assert ids[1] == 5
    assert ids[0] not in (None,) and ids[2] not in (None,)
    assert len(set(ids)) == 3  # alle eindeutig


class _StubEmoji:
    """Duck-Typing-Stub fuer discord.PartialEmoji - str() ist das einzige,
    was _reaction_role_anwenden davon benutzt."""
    def __init__(self, text):
        self._text = text

    def __str__(self):
        return self._text


class _StubRole:
    def __init__(self, id_, name="Rolle"):
        self.id = id_
        self.name = name


class _StubMember:
    def __init__(self):
        self.added = []
        self.removed = []

    async def add_roles(self, role, reason=None):
        self.added.append(role.id)

    async def remove_roles(self, role, reason=None):
        self.removed.append(role.id)


class _StubGuild:
    def __init__(self, role):
        self._role = role
        self._member = _StubMember()

    def get_role(self, role_id):
        return self._role if role_id == self._role.id else None

    def get_member(self, user_id):
        return self._member


class _StubClient:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, gid):
        return self._guild


class _StubPayload:
    def __init__(self, message_id, emoji, guild_id, user_id, member=None):
        self.message_id = message_id
        self.emoji = emoji
        self.guild_id = guild_id
        self.user_id = user_id
        self.member = member


def test_reaction_role_anwenden_vergibt_rolle_bei_passendem_emoji():
    conn = bot.ServerConnection({"service_id": "9999", "guild_id": 111})
    conn.data["reaction_roles"] = [
        {"id": 1, "channel_id": "1", "message_id": "500", "emoji": "🎉", "role_id": "42"}]
    role = _StubRole(42)
    guild = _StubGuild(role)
    client = _StubClient(guild)
    payload = _StubPayload(message_id=500, emoji=_StubEmoji("🎉"), guild_id=111,
                           user_id=999, member=guild._member)

    asyncio.run(bot._reaction_role_anwenden(client, payload, [conn], vergeben=True))
    assert guild._member.added == [42]


def test_reaction_role_anwenden_ignoriert_falsches_emoji():
    conn = bot.ServerConnection({"service_id": "9999", "guild_id": 111})
    conn.data["reaction_roles"] = [
        {"id": 1, "channel_id": "1", "message_id": "500", "emoji": "🎉", "role_id": "42"}]
    role = _StubRole(42)
    guild = _StubGuild(role)
    client = _StubClient(guild)
    payload = _StubPayload(message_id=500, emoji=_StubEmoji("👍"), guild_id=111,
                           user_id=999, member=guild._member)

    asyncio.run(bot._reaction_role_anwenden(client, payload, [conn], vergeben=True))
    assert guild._member.added == []


def test_reaction_role_anwenden_entzieht_rolle_bei_entfernter_reaktion():
    conn = bot.ServerConnection({"service_id": "9999", "guild_id": 111})
    conn.data["reaction_roles"] = [
        {"id": 1, "channel_id": "1", "message_id": "500", "emoji": "🎉", "role_id": "42"}]
    role = _StubRole(42)
    guild = _StubGuild(role)
    client = _StubClient(guild)
    payload = _StubPayload(message_id=500, emoji=_StubEmoji("🎉"), guild_id=111, user_id=999)

    asyncio.run(bot._reaction_role_anwenden(client, payload, [conn], vergeben=False))
    assert guild._member.removed == [42]
