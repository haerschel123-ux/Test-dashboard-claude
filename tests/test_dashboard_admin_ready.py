"""Regressionstest fuer die "ADMIN-Kategorie verschwindet nach Bot-Neustart"-
Meldung: _discord_user_is_admin() behandelte einen noch leeren Guild-Cache
(bot.get_guild() -> None kurz nach dem Start) wie "keine Rolle" und schrieb
faelschlich is_admin=False fest in die neue Session. Der Fix wartet kurz auf
bot.wait_until_ready(), bevor er die Guilds durchgeht.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_dashboard_admin_ready.py -v
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot_mod = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _run(coro):
    return asyncio.run(coro)


class _StubRole:
    def __init__(self, id_):
        self.id = id_


class _StubMember:
    def __init__(self, roles):
        self.roles = roles


class _StubGuild:
    def __init__(self, member):
        self._member = member

    def get_member(self, user_id):
        return self._member


class _StubBot:
    """Simuliert den Zustand kurz nach dem Start: is_ready() liefert False,
    bis wait_until_ready() "durchgelaufen" ist - danach liefert get_guild()
    die Guild mit der erwarteten Rolle."""

    def __init__(self, guild_id, role_id):
        self.user = object()
        self._guild_id = guild_id
        self._guild = _StubGuild(_StubMember([_StubRole(role_id)]))
        self._bereit = False

    def is_ready(self):
        return self._bereit

    async def wait_until_ready(self):
        self._bereit = True

    def get_guild(self, gid):
        if not self._bereit:
            return None  # Cache noch leer - genau der gemeldete Fall
        return self._guild if gid == self._guild_id else None


def test_wartet_auf_bereitschaft_statt_sofort_false_zu_liefern(monkeypatch):
    rolle_id = 555
    guild_id = 111
    monkeypatch.setattr(bot_mod.cfg, "config", {"dashboard_admin_role_id": str(rolle_id)})
    monkeypatch.setattr(bot_mod, "_configured_guild_ids", lambda: [guild_id])
    monkeypatch.setattr(bot_mod, "bot", _StubBot(guild_id, rolle_id))
    ergebnis = _run(bot_mod._discord_user_is_admin(42))
    assert ergebnis is True


def test_bereits_bereiter_bot_wartet_nicht_unnoetig(monkeypatch):
    rolle_id = 555
    guild_id = 111
    stub = _StubBot(guild_id, rolle_id)
    stub._bereit = True  # kein Neustart-Fall - Cache schon warm
    monkeypatch.setattr(bot_mod.cfg, "config", {"dashboard_admin_role_id": str(rolle_id)})
    monkeypatch.setattr(bot_mod, "_configured_guild_ids", lambda: [guild_id])
    monkeypatch.setattr(bot_mod, "bot", stub)
    ergebnis = _run(bot_mod._discord_user_is_admin(42))
    assert ergebnis is True


def test_ohne_passende_rolle_bleibt_es_bei_false(monkeypatch):
    guild_id = 111
    stub = _StubBot(guild_id, role_id=555)
    stub._bereit = True
    monkeypatch.setattr(bot_mod.cfg, "config", {"dashboard_admin_role_id": "999"})
    monkeypatch.setattr(bot_mod, "_configured_guild_ids", lambda: [guild_id])
    monkeypatch.setattr(bot_mod, "bot", stub)
    ergebnis = _run(bot_mod._discord_user_is_admin(42))
    assert ergebnis is False
