"""Tests fuer die Behebung der "verwaisten Ticket-Sperre": ein Nutzer galt
fuer immer als "hat noch ein offenes Ticket", wenn dessen Kanal ausserhalb
des Ticket Tools (von Hand) geloescht wurde - Brigarde hat das per
Screenshot gemeldet ("You already have an open ticket" trotz laengst
geloeschtem Kanal) und einen Admin-Befehl zum Aufraeumen verlangt.

Zwei Bausteine:
1. _ticket_erstellen archiviert einen solchen Eintrag jetzt automatisch
   selbst, sobald der Ersteller es erneut versucht (Selbstheilung).
2. /ticket clear_stale raeumt vorhandene Alt-Faelle fuer die ganze Guild
   auf einen Schlag auf (Admin-Werkzeug).

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_ticket_stale_lock.py -v
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


class _StubMember:
    def __init__(self, id_, roles=None):
        self.id = id_
        self.name = f"user{id_}"
        self.mention = f"<@{id_}>"
        self.roles = roles or []


class _StubResponse:
    def __init__(self):
        self.sent = []
        self.deferred = False

    def is_done(self):
        return self.deferred or bool(self.sent)

    async def send_message(self, content=None, ephemeral=False):
        self.sent.append({"content": content, "ephemeral": ephemeral})

    async def defer(self, ephemeral=False):
        self.deferred = True


class _StubFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, ephemeral=False):
        self.sent.append({"content": content, "ephemeral": ephemeral})


class _StubTextChannel:
    def __init__(self, id_, name):
        self.id = id_
        self.name = name
        self.mention = f"<#{id_}>"

    async def send(self, content=None, embed=None, view=None):
        pass


class _StubGuild:
    """Kennt nur NOCH VORHANDENE Kanaele - genau das bildet "Kanal von Hand
    geloescht" nach: get_channel liefert dann None."""

    def __init__(self, vorhandene_kanaele, me=None):
        self._kanaele = {c.id: c for c in vorhandene_kanaele}
        self.default_role = object()
        self.me = me or _StubMember(0)
        self.name = "Testguild"

    def get_channel(self, cid):
        return self._kanaele.get(cid)

    async def create_text_channel(self, name, overwrites=None, reason=None):
        neuer = _StubTextChannel(999, name)
        self._kanaele[999] = neuer
        return neuer


class _StubInteraction:
    def __init__(self, user, guild):
        self.user = user
        self.guild = guild
        self.locale = None
        self.response = _StubResponse()
        self.followup = _StubFollowup()


def _setup(monkeypatch, kanal_existiert):
    conn = bot_mod.connections.upsert("stale-service")
    bot_mod.connections.assign_guild("stale-service", 111)
    conn.data["ticket_categories"] = [{"id": 1, "label": "Kauf-Support", "role_ids": []}]
    conn.data["ticket_open"] = [{"id": 1, "channel_id": "555", "user_id": "42",
                                 "category_id": 1, "status": "open", "claimed_by": None}]
    vorhandene = [_StubTextChannel(555, "ticket-alt")] if kanal_existiert else []
    guild = _StubGuild(vorhandene)
    monkeypatch.setattr(discord, "PermissionOverwrite", lambda **kw: kw)
    return conn, guild


def test_erneuter_versuch_mit_geloeschtem_kanal_archiviert_alten_eintrag_und_erstellt_neu(monkeypatch):
    conn, guild = _setup(monkeypatch, kanal_existiert=False)
    interaction = _StubInteraction(_StubMember(42), guild)
    kategorie = conn.data["ticket_categories"][0]
    asyncio.run(bot_mod._ticket_erstellen(interaction, conn, kategorie))
    eintraege = conn.data["ticket_open"]
    assert eintraege[0]["status"] == "archived"
    assert any(e["status"] == "open" and e["channel_id"] == "999" for e in eintraege)
    assert not interaction.response.sent, "Es haette nicht mit der Sperr-Meldung geantwortet werden duerfen"


def test_vorhandener_kanal_blockiert_weiterhin_wie_bisher(monkeypatch):
    conn, guild = _setup(monkeypatch, kanal_existiert=True)
    interaction = _StubInteraction(_StubMember(42), guild)
    kategorie = conn.data["ticket_categories"][0]
    asyncio.run(bot_mod._ticket_erstellen(interaction, conn, kategorie))
    eintraege = conn.data["ticket_open"]
    assert eintraege[0]["status"] == "open"
    assert len(eintraege) == 1
    assert "bereits ein offenes Ticket" in interaction.response.sent[0]["content"]


class _StubInteractionAdmin:
    def __init__(self, guild):
        self.guild = guild
        self.guild_id = 111
        self.user = _StubMember(1)
        self.locale = None
        self.response = _StubResponse()
        self.followup = _StubFollowup()


def test_clear_stale_raeumt_nur_eintraege_mit_geloeschtem_kanal_auf(monkeypatch):
    conn = bot_mod.connections.upsert("stale-service-2")
    bot_mod.connections.assign_guild("stale-service-2", 222)
    conn.data["ticket_open"] = [
        {"id": 1, "channel_id": "1", "user_id": "10", "status": "open"},   # Kanal weg
        {"id": 2, "channel_id": "2", "user_id": "20", "status": "claimed"},  # Kanal noch da
        {"id": 3, "channel_id": "3", "user_id": "30", "status": "archived"},  # schon archiviert
    ]
    guild = _StubGuild([_StubTextChannel(2, "ticket-noch-da")])
    monkeypatch.setattr(bot_mod, "_conns_of", lambda interaction: [conn])
    monkeypatch.setattr(bot_mod, "_is_admin", lambda interaction: True)
    interaction = _StubInteractionAdmin(guild)
    asyncio.run(bot_mod.ticket_clear_stale.callback(interaction))
    eintraege = conn.data["ticket_open"]
    assert eintraege[0]["status"] == "archived"  # aufgeraeumt
    assert eintraege[1]["status"] == "claimed"   # unangetastet, Kanal existiert noch
    assert eintraege[2]["status"] == "archived"  # war schon so
    assert "1" in interaction.response.sent[0]["content"]


def test_clear_stale_verweigert_ohne_admin_oder_subcmd_recht(monkeypatch):
    conn = bot_mod.connections.upsert("stale-service-3")
    bot_mod.connections.assign_guild("stale-service-3", 333)
    conn.data["ticket_open"] = [{"id": 1, "channel_id": "1", "user_id": "10", "status": "open"}]
    guild = _StubGuild([])
    monkeypatch.setattr(bot_mod, "_conns_of", lambda interaction: [conn])
    monkeypatch.setattr(bot_mod, "_is_admin", lambda interaction: False)
    monkeypatch.setattr(bot_mod, "_subcmd_allowed", lambda interaction, key: False)
    interaction = _StubInteractionAdmin(guild)
    asyncio.run(bot_mod.ticket_clear_stale.callback(interaction))
    assert conn.data["ticket_open"][0]["status"] == "open", "Ohne Berechtigung darf nichts aufgeraeumt werden"
