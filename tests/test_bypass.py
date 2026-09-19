"""Tests fuer /bypass guildid und /bypass list - Brigardes Notschalter, um
eine Discord-Guild ohne Nitrado-Token freizuschalten (fuer Befehle, die
keinen echten Serverzugriff brauchen), bis sie dort einen echten Server
verbindet und zuordnet.

Muster: test_ticket_panel_permission.py (Discord-Owner-Stubs fuer
_ist_bot_eigentuemer) + test_giveaways.py (Stub-Interaction/-Member).

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_bypass.py -v
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


def _run(coro):
    return asyncio.run(coro)


class _StubPerms:
    def __init__(self, administrator=False):
        self.administrator = administrator


class _StubMember:
    def __init__(self, id_, administrator=False, roles=None):
        self.id = id_
        self.roles = roles or []
        self.guild_permissions = _StubPerms(administrator)


class _StubGuild:
    def __init__(self, owner_id):
        self.owner_id = owner_id


class _StubResponse:
    def __init__(self):
        self.sent = []
        self.deferred = False

    def is_done(self):
        return self.deferred or bool(self.sent)

    async def send_message(self, content=None, embed=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})

    async def defer(self, ephemeral=False):
        self.deferred = True


class _StubFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class _StubInteraction:
    def __init__(self, user, guild_id, owner_id=0):
        self.user = user
        self.guild_id = guild_id
        self.guild = _StubGuild(owner_id)
        self.locale = None
        self.response = _StubResponse()
        self.followup = _StubFollowup()


@pytest.fixture(autouse=True)
def _eigentuemer_cache_zuruecksetzen(monkeypatch):
    # _EIGENTUEMER_IDS wird beim ersten Aufruf befuellt und dann gecacht -
    # zwischen Tests zuruecksetzen, sonst haengt vom vorherigen Test ab, wer
    # als Eigentuemer gilt.
    monkeypatch.setattr(bot_mod, "_EIGENTUEMER_IDS", set())
    monkeypatch.setattr(bot_mod.discord, "Member", _StubMember)


def _als_eigentuemer(monkeypatch, user_id):
    monkeypatch.setattr(bot_mod, "_EIGENTUEMER_IDS", {user_id})


def test_bypass_erlaubt_bot_eigentuemer_in_der_haupt_guild(monkeypatch):
    _als_eigentuemer(monkeypatch, 42)
    zielguild = 900001
    interaction = _StubInteraction(_StubMember(42), guild_id=bot_mod._BYPASS_HAUPT_GUILD_ID)
    _run(bot_mod.bypass_guildid.callback(interaction, str(zielguild)))
    assert interaction.followup.sent[0]["ephemeral"] is True
    assert "freigeschaltet" in interaction.followup.sent[0]["content"]
    conn = bot_mod.connections.for_service(bot_mod._bypass_platzhalter_service_id(zielguild))
    assert conn is not None
    assert conn.guild_id == zielguild
    assert conn.data["bypass"] is True


def test_bypass_lehnt_ausserhalb_der_haupt_guild_ab(monkeypatch):
    _als_eigentuemer(monkeypatch, 42)
    zielguild = 900002
    fremde_guild = 111
    interaction = _StubInteraction(_StubMember(42), guild_id=fremde_guild)
    _run(bot_mod.bypass_guildid.callback(interaction, str(zielguild)))
    assert "Haupt-Discord" in interaction.followup.sent[0]["content"]
    assert bot_mod.connections.for_service(
        bot_mod._bypass_platzhalter_service_id(zielguild)) is None


def test_bypass_lehnt_nicht_eigentuemer_ab(monkeypatch):
    _als_eigentuemer(monkeypatch, 999)  # jemand anderes ist Eigentuemer
    zielguild = 900003
    interaction = _StubInteraction(_StubMember(42), guild_id=bot_mod._BYPASS_HAUPT_GUILD_ID)
    _run(bot_mod.bypass_guildid.callback(interaction, str(zielguild)))
    assert "Nur der Bot-Eigentümer" in interaction.followup.sent[0]["content"]
    assert bot_mod.connections.for_service(
        bot_mod._bypass_platzhalter_service_id(zielguild)) is None


def test_bypass_lehnt_ungueltige_guild_id_ab(monkeypatch):
    _als_eigentuemer(monkeypatch, 42)
    interaction = _StubInteraction(_StubMember(42), guild_id=bot_mod._BYPASS_HAUPT_GUILD_ID)
    _run(bot_mod.bypass_guildid.callback(interaction, "keine-zahl"))
    assert "Ungültige Guild-ID" in interaction.followup.sent[0]["content"]


def test_bypass_verweigert_bei_bereits_echtem_server(monkeypatch):
    _als_eigentuemer(monkeypatch, 42)
    zielguild = 900004
    bot_mod.connections.upsert("echt-9000", nitrado_token="x")
    bot_mod.connections.assign_guild("echt-9000", zielguild)
    interaction = _StubInteraction(_StubMember(42), guild_id=bot_mod._BYPASS_HAUPT_GUILD_ID)
    _run(bot_mod.bypass_guildid.callback(interaction, str(zielguild)))
    assert "bereits einen echten" in interaction.followup.sent[0]["content"]
    assert bot_mod.connections.for_service(
        bot_mod._bypass_platzhalter_service_id(zielguild)) is None


def test_premium_check_laesst_bypass_guild_durch(monkeypatch):
    # Kern der Anforderung: nach /bypass guildid gilt die Guild fuer
    # _premium_check als premium - ohne dass ein Token existiert.
    _als_eigentuemer(monkeypatch, 42)
    zielguild = 900005
    interaction = _StubInteraction(_StubMember(42), guild_id=bot_mod._BYPASS_HAUPT_GUILD_ID)
    _run(bot_mod.bypass_guildid.callback(interaction, str(zielguild)))
    conns = bot_mod.connections.all_for_guild(zielguild)
    assert len(conns) == 1
    assert any(bot_mod._kunden_stufe(c) in ("premium", "premium_beta") for c in conns)
    # Aber Befehle mit echtem Serverzugriff bleiben gesperrt: kein conn.api.
    assert conns[0].api is None


def test_echte_zuweisung_entfernt_bypass_platzhalter_automatisch(monkeypatch):
    _als_eigentuemer(monkeypatch, 42)
    zielguild = 900006
    interaction = _StubInteraction(_StubMember(42), guild_id=bot_mod._BYPASS_HAUPT_GUILD_ID)
    _run(bot_mod.bypass_guildid.callback(interaction, str(zielguild)))
    platzhalter_sid = bot_mod._bypass_platzhalter_service_id(zielguild)
    assert bot_mod.connections.for_service(platzhalter_sid) is not None

    bot_mod.connections.upsert("echt-9001", nitrado_token="x")
    ok, _ = bot_mod.connections.assign_guild("echt-9001", zielguild)
    assert ok is True
    assert bot_mod.connections.for_service(platzhalter_sid) is None
    verbleibend = bot_mod.connections.all_for_guild(zielguild)
    assert len(verbleibend) == 1
    assert verbleibend[0].service_id == "echt-9001"


def test_bypass_list_zeigt_nur_aktive_bypasses(monkeypatch):
    _als_eigentuemer(monkeypatch, 42)
    zielguild = 900007
    interaction = _StubInteraction(_StubMember(42), guild_id=bot_mod._BYPASS_HAUPT_GUILD_ID)
    _run(bot_mod.bypass_guildid.callback(interaction, str(zielguild)))
    liste_interaction = _StubInteraction(_StubMember(42), guild_id=bot_mod._BYPASS_HAUPT_GUILD_ID)
    _run(bot_mod.bypass_list.callback(liste_interaction))
    embed = liste_interaction.followup.sent[0]["embed"]
    assert str(zielguild) in embed.description


def test_bypass_list_ohne_aktive_bypasses(monkeypatch):
    # Andere Tests in dieser Datei legen bewusst dauerhafte Bypass-Eintraege
    # an (kein Auto-Cleanup ohne echte Zuweisung) - fuer den Leerfall alle
    # vorher entfernen, sonst haengt das Ergebnis von der Testreihenfolge ab.
    for conn in list(bot_mod.connections.all()):
        if conn.data.get("bypass"):
            bot_mod.connections.remove(conn.service_id)
    _als_eigentuemer(monkeypatch, 42)
    interaction = _StubInteraction(_StubMember(42), guild_id=bot_mod._BYPASS_HAUPT_GUILD_ID)
    _run(bot_mod.bypass_list.callback(interaction))
    assert "Keine aktiven" in interaction.followup.sent[0]["content"]


# ── Regression: /bypass darf nicht selbst an der Premium-Sperre scheitern ──

class _StubCommand:
    def __init__(self, qualified_name):
        self.qualified_name = qualified_name


def test_premium_check_laesst_bypass_immer_durch(monkeypatch):
    # Brigarde meldete per Screenshot: /bypass guildid antwortete in ihrer
    # EIGENEN, noch nicht zugeordneten Guild mit "Du hast kein Premium" -
    # der globale _premium_check lief VOR dem Befehl selbst und blockierte
    # damit genau das Werkzeug, das eine Guild ueberhaupt erst freischalten
    # soll (Henne-Ei-Problem). "bypass" muss deshalb wie "setup"/"hilfe"/
    # "ping" in _PREMIUM_FREE_COMMANDS stehen.
    unbekannte_guild = 987654321
    assert bot_mod.connections.all_for_guild(unbekannte_guild) == []
    interaction = _StubInteraction(_StubMember(42), guild_id=unbekannte_guild)
    interaction.command = _StubCommand("bypass guildid")
    erlaubt = _run(bot_mod._premium_check(interaction))
    assert erlaubt is True
