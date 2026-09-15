"""Tests fuer das Transkript-Posting beim Schliessen eines Tickets.

Brigarde will beim Schliessen zusaetzlich zur bestehenden DM an den
Ersteller ein Embed (Typ/Ersteller/Schliesser) samt Transkript-Anhang in
einen im Dashboard gewaehlten Channel posten - wie im mitgeschickten
Discord-Beispiel ("Ticket #0001 closed"). Kein echter Discord-Login noetig,
siehe test_ticket_tool.py fuer das etablierte Stub-Muster.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_ticket_transcript_channel.py -v
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot_mod = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")
discord = bot_mod.discord


def test_ticket_transkript_kanal_none_ohne_conn():
    assert bot_mod._ticket_transkript_kanal(None) is None


def test_ticket_transkript_kanal_liest_gespeicherten_wert():
    conn = bot_mod.ServerConnection({"service_id": "9999", "guild_id": 111})
    assert bot_mod._ticket_transkript_kanal(conn) is None
    conn.data["ticket_transcript_channel"] = "12345"
    assert bot_mod._ticket_transkript_kanal(conn) == 12345
    conn.data["ticket_transcript_channel"] = "kaputt"
    assert bot_mod._ticket_transkript_kanal(conn) is None


class _StubMember:
    def __init__(self, id_, roles=None):
        self.id = id_
        self.mention = f"<@{id_}>"
        self.roles = roles or []
        self.dms = []

    async def send(self, content=None, file=None):
        self.dms.append({"content": content, "file": file})


class _StubTextChannel:
    """Simuliert sowohl den Ticket-Kanal (history/set_permissions/edit) als
    auch den separaten Protokoll-Kanal (send mit embed+file)."""

    def __init__(self, name, nachrichten=None):
        self.name = name
        self._nachrichten = nachrichten or []
        self.sent = []

    async def history(self, limit=None, oldest_first=True):
        for m in self._nachrichten:
            yield m

    async def set_permissions(self, target, **kwargs):
        pass

    async def edit(self, name=None, **kwargs):
        self.name = name

    async def send(self, content=None, embed=None, file=None, view=None):
        self.sent.append({"content": content, "embed": embed, "file": file, "view": view})


class _StubGuild:
    def __init__(self, members, channels):
        self._members = {m.id: m for m in members}
        self._channels = {c_id: c for c_id, c in channels.items()}
        self.name = "Testguild"

    def get_member(self, uid):
        return self._members.get(uid)

    def get_channel(self, cid):
        return self._channels.get(cid)


class _StubResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self):
        self.deferred = True


class _StubFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, view=None):
        self.sent.append({"content": content, "embed": embed, "view": view})


class _StubInteraction:
    def __init__(self, user, guild, channel):
        self.user = user
        self.guild = guild
        self.channel = channel
        self.response = _StubResponse()
        self.followup = _StubFollowup()


def _close_setup(monkeypatch, transcript_channel_id=None):
    conn = bot_mod.connections.upsert("tt-service")
    bot_mod.connections.assign_guild("tt-service", 111)
    conn.data["ticket_categories"] = [{"id": 1, "label": "Kauf-Support", "role_ids": ["42"]}]
    conn.data["ticket_open"] = [{"id": 1, "channel_id": "777", "user_id": "555",
                                 "category_id": 1, "status": "open", "claimed_by": None}]
    if transcript_channel_id is not None:
        conn.data["ticket_transcript_channel"] = transcript_channel_id
    rolle = type("R", (), {"id": 42, "name": "Support"})()
    guild_stub_fuer_rollen = type("G", (), {"get_role": lambda self, rid: rolle if rid == 42 else None})()
    original_bot = bot_mod.bot
    bot_mod.bot = type("C", (), {"get_guild": lambda self, gid: guild_stub_fuer_rollen})()
    monkeypatch.setattr(discord, "Member", _StubMember)
    return conn, rolle, original_bot


def test_close_postet_transkript_in_konfigurierten_channel(monkeypatch):
    conn, rolle, original_bot = _close_setup(monkeypatch, transcript_channel_id=999)
    try:
        view = bot_mod.TicketChannelView("tt-service", 1)
        ersteller = _StubMember(555)
        protokoll_kanal = _StubTextChannel("ticket-log")
        monkeypatch.setattr(discord, "TextChannel", _StubTextChannel)
        guild = _StubGuild([ersteller], {999: protokoll_kanal})
        ticket_kanal = _StubTextChannel("ticket-spieler1")
        interaction = _StubInteraction(_StubMember(1, roles=[rolle]), guild, ticket_kanal)
        asyncio.run(view._close(interaction))
    finally:
        bot_mod.bot = original_bot
    assert protokoll_kanal.sent, "Es haette in den Protokoll-Channel gepostet werden muessen"
    eintrag = protokoll_kanal.sent[0]
    assert eintrag["embed"].title == "Ticket #1 geschlossen"
    felder = {f.name: f.value for f in eintrag["embed"].fields}
    assert felder["Typ"] == "Kauf-Support"
    assert felder["Erstellt von"] == "<@555>"
    assert felder["Geschlossen von"] == "<@1>"
    assert eintrag["file"] is not None
    assert conn.data["ticket_open"][0]["status"] == "archived"


def test_close_postet_nichts_ohne_konfigurierten_channel(monkeypatch):
    conn, rolle, original_bot = _close_setup(monkeypatch, transcript_channel_id=None)
    try:
        view = bot_mod.TicketChannelView("tt-service", 1)
        ersteller = _StubMember(555)
        monkeypatch.setattr(discord, "TextChannel", _StubTextChannel)
        guild = _StubGuild([ersteller], {})
        ticket_kanal = _StubTextChannel("ticket-spieler1")
        interaction = _StubInteraction(_StubMember(1, roles=[rolle]), guild, ticket_kanal)
        asyncio.run(view._close(interaction))
    finally:
        bot_mod.bot = original_bot
    assert conn.data["ticket_open"][0]["status"] == "archived"
