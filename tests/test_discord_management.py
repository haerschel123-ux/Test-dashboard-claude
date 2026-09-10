"""Tests fuer die Discord-Management-Embeds (Willkommen/Verlassen).

Kein echter discord.Member/-Guild noetig: _welcome_leave_embed greift nur auf
display_name, mention, display_avatar.url, guild.name und guild.member_count
zu - ein einfaches Duck-Typing-Stub reicht, ein echter Discord-Login ist im
Test nicht moeglich (siehe CLAUDE.md).

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/ -v
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


class _StubAvatar:
    def __init__(self, url):
        self.url = url


class _StubGuild:
    def __init__(self, name, member_count):
        self.name = name
        self.member_count = member_count


class _StubMember:
    def __init__(self, display_name, user_id=555000111,
                 avatar_url="https://cdn.example/avatar.png"):
        self.display_name = display_name
        self.mention = f"<@{user_id}>"
        self.display_avatar = _StubAvatar(avatar_url)

    def __str__(self):
        return self.display_name


def test_willkommen_embed_de_mit_avatar():
    member = _StubMember("Alex")
    guild = _StubGuild("Test-Server", 94)
    embed = bot._welcome_leave_embed(member, guild, "de", True, True)
    assert "Willkommen" in embed.title
    assert "Alex" in embed.title
    # Titel bleibt reiner Text (Discord rendert dort keine Erwaehnungen) -
    # die echte "<@id>"-Erwaehnung gehoert in die Beschreibung.
    assert member.mention not in embed.title
    assert member.mention in embed.description
    assert "#94" in embed.description
    assert "Test-Server" in embed.description
    assert embed.colour.value == 0x2ECC71
    assert embed.thumbnail.url == "https://cdn.example/avatar.png"


def test_willkommen_embed_en_ohne_avatar():
    member = _StubMember("Alex")
    guild = _StubGuild("Test-Server", 94)
    embed = bot._welcome_leave_embed(member, guild, "en", True, False)
    assert "Welcome" in embed.title
    assert member.mention in embed.description
    assert "#94" in embed.description
    assert embed.thumbnail.url is None


def test_verlassen_embed_de():
    member = _StubMember("Alex")
    guild = _StubGuild("Test-Server", 93)
    embed = bot._welcome_leave_embed(member, guild, "de", False, True)
    assert "Auf Wiedersehen" in embed.title
    assert member.mention in embed.description
    assert "verlassen" in embed.description
    assert "93" not in embed.description
    assert embed.colour.value == 0xE74C3C


def test_verlassen_embed_en():
    member = _StubMember("Alex")
    guild = _StubGuild("Test-Server", 93)
    embed = bot._welcome_leave_embed(member, guild, "en", False, True)
    assert "Goodbye" in embed.title
    assert member.mention in embed.description
    assert "left" in embed.description
