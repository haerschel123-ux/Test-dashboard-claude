"""Username Hooks: Schalter fuer das Zurueckdrehen beim /unlink
(link_revert_added_roles / _removed_roles / _nickname) und das Limit der
Zusatz-Accounts ueber die General-Karte.

    python3 -m pytest tests/test_link_hooks_revert.py -q
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeRole:
    def __init__(self, rid):
        self.id, self.name = rid, f"Rolle{rid}"


class FakeMember:
    def __init__(self):
        self.nick = "Ingame"
        self.added, self.removed, self.edits = [], [], []

    async def add_roles(self, rolle, reason=None):
        self.added.append(rolle.id)

    async def remove_roles(self, rolle, reason=None):
        self.removed.append(rolle.id)

    async def edit(self, nick=None, reason=None):
        self.edits.append(nick)


class FakeGuild:
    def __init__(self, member):
        self.member = member
        self.owner_id = 1

    def get_member(self, uid):
        return self.member

    def get_role(self, rid):
        return FakeRole(rid)


@pytest.fixture
def umgebung(monkeypatch):
    member = FakeMember()
    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: FakeGuild(member))
    zustand = {"added": ["10"], "removed": ["20"], "alter_nick": "Alt", "nick_gesetzt": True}
    monkeypatch.setattr(bot.db, "link_hook_get", lambda g, u: dict(zustand))
    geloescht = []
    monkeypatch.setattr(bot.db, "link_hook_delete", lambda g, u: geloescht.append((g, u)))
    conn = bot.ServerConnection({"service_id": "1000", "guild_id": 111, "map_name": "ChernarusPlus"})
    monkeypatch.setattr(bot.connections, "for_guild", lambda gid: conn)
    return member, conn, geloescht


def test_unlink_dreht_standardmaessig_alles_zurueck(umgebung):
    member, _conn, geloescht = umgebung
    assert _run(bot._link_hooks_zuruecknehmen(111, 5)) == ""
    assert member.removed == [10] and member.added == [20] and member.edits == ["Alt"]
    assert geloescht == [(111, 5)]


def test_unlink_schalter_aus(umgebung):
    member, conn, geloescht = umgebung
    conn.data["link_revert_added_roles"] = False
    conn.data["link_revert_nickname"] = False
    _run(bot._link_hooks_zuruecknehmen(111, 5))
    assert member.removed == [] and member.edits == [] and member.added == [20]
    conn.data["link_revert_removed_roles"] = False
    member.added.clear()
    _run(bot._link_hooks_zuruecknehmen(111, 5))
    assert member.added == [] and len(geloescht) == 2


def test_general_karte_und_validierung():
    keys = bot._GENERAL_KARTEN_SCHLUESSEL["username_hooks"]
    for k in ("max_linked_accounts", "link_revert_added_roles", "link_revert_removed_roles", "link_revert_nickname"):
        assert k in keys
    assert bot._general_wert_pruefen("max_linked_accounts", 6) == (6, None)
    assert bot._general_wert_pruefen("max_linked_accounts", 0)[1]
    assert bot._general_wert_pruefen("max_linked_accounts", 101)[1]
    assert bot._general_wert_pruefen("link_revert_nickname", False) == (False, None)
    assert bot._general_wert_pruefen("link_revert_nickname", "nein")[1]
    conn = bot.ServerConnection({"service_id": "1000", "guild_id": 111})
    # Vorgabe: alles zurueckdrehen, Limit 1 (nur Hauptaccount)
    assert conn.get("link_revert_added_roles") is True and conn.get("max_linked_accounts") == 1
    payload = bot._general_payload(conn)
    assert payload["link_revert_nickname"] is True and payload["max_linked_accounts"] == 1
