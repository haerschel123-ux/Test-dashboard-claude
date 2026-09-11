"""Tests fuer die Ticket-Tool-Bausteine (Discord Management).

Kein echter Discord-Login noetig: die reinen Datenhelfer (_ticket_categories,
_ensure_ticket_category_ids, _ticket_transkript_bauen) sind ohne Discord-API
testbar. _ticket_support_rollen braucht ein Guild-Objekt - dafuer wird das
globale bot.bot kurz durch einen Stub-Client ersetzt (siehe CLAUDE.md: kein
echter Discord-Login im Test moeglich).

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/ -v
"""
import asyncio
import datetime
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot_mod = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def test_ticket_categories_startet_leer_und_speichert_in_conn():
    conn = bot_mod.ServerConnection({"service_id": "9999", "guild_id": 111})
    assert bot_mod._ticket_categories(conn) == []
    kategorien = bot_mod._ticket_categories(conn)
    kategorien.append({"id": None, "label": "Kauf-Support", "role_ids": ["42"]})
    assert conn.data["ticket_categories"] == kategorien


def test_ensure_ticket_category_ids_vergibt_fortlaufende_ids():
    eintraege = [{"id": None, "label": "a"}, {"id": 5, "label": "b"}, {"id": None, "label": "c"}]
    geaendert = bot_mod._ensure_ticket_category_ids(eintraege)
    assert geaendert is True
    ids = [e["id"] for e in eintraege]
    assert ids[1] == 5
    assert ids[0] not in (None,) and ids[2] not in (None,)
    assert len(set(ids)) == 3  # alle eindeutig


def test_ensure_ticket_ids_vergibt_fortlaufende_ids():
    eintraege = [{"id": None, "channel_id": "1"}, {"id": None, "channel_id": "2"}]
    geaendert = bot_mod._ensure_ticket_ids(eintraege)
    assert geaendert is True
    assert eintraege[0]["id"] == 1
    assert eintraege[1]["id"] == 2


class _StubAuthor:
    def __init__(self, display_name):
        self.display_name = display_name
        self.name = display_name


class _StubAttachment:
    def __init__(self, url):
        self.url = url


class _StubMessage:
    def __init__(self, author, content, created_at, attachments=None):
        self.author = author
        self.content = content
        self.created_at = created_at
        self.attachments = attachments or []


def test_ticket_transkript_bauen_enthaelt_alle_nachrichten_und_anhaenge():
    zeit = datetime.datetime(2026, 1, 1, 12, 30, tzinfo=datetime.timezone.utc)
    nachrichten = [
        _StubMessage(_StubAuthor("Spieler1"), "Hallo, ich brauche Hilfe.", zeit),
        _StubMessage(_StubAuthor("Support1"), "Klar, worum geht's?", zeit,
                    attachments=[_StubAttachment("https://cdn.example/bild.png")]),
    ]
    transkript = bot_mod._ticket_transkript_bauen(nachrichten).decode("utf-8")
    assert "Spieler1: Hallo, ich brauche Hilfe." in transkript
    assert "Support1: Klar, worum geht's?" in transkript
    assert "https://cdn.example/bild.png" in transkript
    assert "2026-01-01 12:30" in transkript


def test_ticket_transkript_bauen_leere_liste():
    transkript = bot_mod._ticket_transkript_bauen([]).decode("utf-8")
    assert "keine Nachrichten" in transkript


class _StubRole:
    def __init__(self, id_, name="Rolle"):
        self.id = id_
        self.name = name


class _StubGuild:
    def __init__(self, rollen):
        self._rollen = {r.id: r for r in rollen}

    def get_role(self, role_id):
        return self._rollen.get(role_id)


class _StubClient:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, gid):
        return self._guild


def test_ticket_support_rollen_loest_vorhandene_rollen_auf_und_ueberspringt_fehlende():
    conn = bot_mod.ServerConnection({"service_id": "9999", "guild_id": 111})
    rolle_a = _StubRole(1, "Support")
    guild = _StubGuild([rolle_a])
    original_bot = bot_mod.bot
    bot_mod.bot = _StubClient(guild)
    try:
        kategorie = {"id": 1, "label": "Kauf-Support", "role_ids": ["1", "999"]}
        rollen = bot_mod._ticket_support_rollen(conn, kategorie)
    finally:
        bot_mod.bot = original_bot
    assert [r.id for r in rollen] == [1]


class _StubMember:
    def __init__(self, id_, roles=None):
        self.id = id_
        self.mention = f"<@{id_}>"
        self.roles = roles or []


class _StubChannel:
    def __init__(self, id_):
        self.id = id_
        self.permission_calls = []

    async def set_permissions(self, target, **kwargs):
        self.permission_calls.append((target, kwargs))


class _StubResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, ephemeral=False):
        self.sent.append({"content": content, "ephemeral": ephemeral})


class _StubInteraction:
    def __init__(self, guild_id, user):
        self.guild_id = guild_id
        self.user = user
        self.locale = None
        self.response = _StubResponse()


def _ticket_setup(monkeypatch):
    conn = bot_mod.ServerConnection({"service_id": "9999", "guild_id": 111})
    conn.data["ticket_categories"] = [{"id": 1, "label": "Kauf-Support", "role_ids": ["42"]}]
    conn.data["ticket_open"] = [{"id": 1, "channel_id": "777", "user_id": "555",
                                 "category_id": 1, "status": "open", "claimed_by": None}]
    rolle = type("R", (), {"id": 42, "name": "Support"})()
    guild = type("G", (), {"get_role": lambda self, rid: rolle if rid == 42 else None})()
    original_bot = bot_mod.bot
    bot_mod.bot = type("C", (), {"get_guild": lambda self, gid: guild})()
    monkeypatch.setattr(bot_mod, "_conns_of", lambda interaction: [conn])
    monkeypatch.setattr(bot_mod.discord, "Member", _StubMember)
    return conn, rolle, original_bot


def test_ticket_add_erlaubt_fuer_support_rolle(monkeypatch):
    conn, rolle, original_bot = _ticket_setup(monkeypatch)
    try:
        interaction = _StubInteraction(111, _StubMember(1, roles=[rolle]))
        kanal = _StubChannel(777)
        asyncio.run(bot_mod.ticket_add_member.callback(interaction, kanal, _StubMember(999)))
    finally:
        bot_mod.bot = original_bot
    assert kanal.permission_calls, "set_permissions haette aufgerufen werden muessen"
    ziel, kwargs = kanal.permission_calls[0]
    assert ziel.id == 999
    assert kwargs["view_channel"] is True
    assert interaction.response.sent[0]["ephemeral"] is False


def test_ticket_add_verweigert_ohne_support_rolle(monkeypatch):
    conn, rolle, original_bot = _ticket_setup(monkeypatch)
    try:
        interaction = _StubInteraction(111, _StubMember(1, roles=[]))
        kanal = _StubChannel(777)
        asyncio.run(bot_mod.ticket_add_member.callback(interaction, kanal, _StubMember(999)))
    finally:
        bot_mod.bot = original_bot
    assert not kanal.permission_calls
    assert interaction.response.sent[0]["ephemeral"] is True


def test_ticket_add_kein_ticket_kanal(monkeypatch):
    conn, rolle, original_bot = _ticket_setup(monkeypatch)
    try:
        interaction = _StubInteraction(111, _StubMember(1, roles=[rolle]))
        kanal = _StubChannel(999999)  # kein Ticket-Kanal
        asyncio.run(bot_mod.ticket_add_member.callback(interaction, kanal, _StubMember(999)))
    finally:
        bot_mod.bot = original_bot
    assert not kanal.permission_calls
    assert "kein Ticket-Kanal" in interaction.response.sent[0]["content"]


def test_ticket_remove_erlaubt_fuer_support_rolle(monkeypatch):
    conn, rolle, original_bot = _ticket_setup(monkeypatch)
    try:
        interaction = _StubInteraction(111, _StubMember(1, roles=[rolle]))
        kanal = _StubChannel(777)
        asyncio.run(bot_mod.ticket_remove_member.callback(interaction, kanal, _StubMember(999)))
    finally:
        bot_mod.bot = original_bot
    assert kanal.permission_calls
    ziel, kwargs = kanal.permission_calls[0]
    assert ziel.id == 999
    assert kwargs["overwrite"] is None


def test_ticket_remove_schuetzt_ersteller(monkeypatch):
    conn, rolle, original_bot = _ticket_setup(monkeypatch)
    try:
        interaction = _StubInteraction(111, _StubMember(1, roles=[rolle]))
        kanal = _StubChannel(777)
        ersteller = _StubMember(555)  # user_id des Tickets
        asyncio.run(bot_mod.ticket_remove_member.callback(interaction, kanal, ersteller))
    finally:
        bot_mod.bot = original_bot
    assert not kanal.permission_calls, "Ersteller darf nicht entfernt werden"
    assert interaction.response.sent[0]["ephemeral"] is True


def test_ticket_category_payload_loest_rollennamen_auf():
    conn = bot_mod.ServerConnection({"service_id": "9999", "guild_id": 111})
    rolle_a = _StubRole(1, "Support")
    guild = _StubGuild([rolle_a])
    original_bot = bot_mod.bot
    bot_mod.bot = _StubClient(guild)
    try:
        payload = bot_mod._ticket_category_payload(
            conn, {"id": 1, "label": "Kauf-Support", "role_ids": ["1", "999"]})
    finally:
        bot_mod.bot = original_bot
    assert payload["label"] == "Kauf-Support"
    assert payload["roles"][0]["name"] == "Support"
    assert payload["roles"][1]["name"] is None
