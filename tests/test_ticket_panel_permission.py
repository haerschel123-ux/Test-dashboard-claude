"""Tests fuer die Standard-Berechtigung von /send ticket panel.

Die Befehlsbeschreibung sagt seit jeher "(Admin)", die Pruefung ging aber
ausschliesslich ueber die granulare Subcommand-Permission-Liste, in der
"send_ticket_panel" nie registriert war - dadurch konnte NUR der Guild-
Owner den Befehl nutzen, keine andere Administrator-Rolle. Jetzt gilt
zusaetzlich _is_admin() (Discord-Administrator ODER konfigurierte
admin_role_ids), und der Schluessel ist ueber die Permissions-Seite
zusaetzlich an einzelne Rollen/Personen vergebbar.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_ticket_panel_permission.py -v
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


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


class _StubInteraktion:
    def __init__(self, guild_id, user, owner_id=0):
        self.guild_id = guild_id
        self.guild = _StubGuild(owner_id)
        self.user = user


def test_send_ticket_panel_ist_im_permissions_katalog_registriert():
    assert "send_ticket_panel" in bot._SUBCMD_KEYS


def test_is_admin_true_bei_discord_administrator_rolle(monkeypatch):
    monkeypatch.setattr(bot, "_conns_of", lambda interaction: [])
    monkeypatch.setattr(bot.discord, "Member", _StubMember)
    member = _StubMember(1, administrator=True)
    interaction = _StubInteraktion(111, member)
    assert bot._is_admin(interaction) is True


def test_is_admin_false_ohne_administrator_und_ohne_admin_rolle(monkeypatch):
    monkeypatch.setattr(bot, "_conns_of", lambda interaction: [])
    monkeypatch.setattr(bot.discord, "Member", _StubMember)
    member = _StubMember(2, administrator=False)
    interaction = _StubInteraktion(111, member, owner_id=999)
    assert bot._is_admin(interaction) is False


def test_send_ticket_panel_erlaubt_jede_administrator_rolle_nicht_nur_owner(monkeypatch):
    # Die eigentliche Befehlsfreigabe: _is_admin ODER die granulare Liste -
    # ein Nicht-Owner mit Administrator-Rolle darf trotzdem, ohne dass
    # jemand ihn erst manuell in den Subcommand-Permissions eintragen muss.
    monkeypatch.setattr(bot, "_conns_of", lambda interaction: [])
    monkeypatch.setattr(bot.discord, "Member", _StubMember)
    member = _StubMember(3, administrator=True)
    interaction = _StubInteraktion(111, member, owner_id=999)
    erlaubt = bot._is_admin(interaction) or bot._subcmd_allowed(interaction, "send_ticket_panel")
    assert erlaubt is True
