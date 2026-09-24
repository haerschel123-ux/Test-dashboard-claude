"""Tests fuer Mehrfach-Accounts pro Discord-Nutzer (Haupt-/Alt-Verknuepfungen).

Brigarde: "Wie viele Accounts man mit seinem discord verbinden kann - der
erste Account wird bei /username list als main angezeigt, weitere unter
alt - wenn das eingestellte limit erreicht ist kann ein Spieler keine
weiteren Accounts verbinden - das dient dazu das man selbst dann geld
bekommt wenn man nicht auf dem hauptaccount ist."

Deckt: DB-Ebene (link_user vergibt is_main automatisch, unlink_specific
ruecktw den naechsten Alt-Account zum Hauptaccount nach, ein Name bleibt
weiterhin nur EINEM User zugeordnet), und die tatsaechlichen Slash-Commands
/link (Limit-Durchsetzung) und /unlink (Name-Pflicht bei mehreren Accounts).

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_mehrfach_accounts.py -v
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


@pytest.fixture
def edb(tmp_path):
    return bot.EconomyDB(str(tmp_path / "economy_test.db"))


def _run(coro):
    return asyncio.run(coro)


# ── Reine DB-Ebene ────────────────────────────────────────────────────────
def test_erster_link_wird_hauptaccount(edb):
    edb.link_user(111, 42, "Haupt")
    rows = edb.get_links_by_user(111, 42)
    assert len(rows) == 1
    assert rows[0]["is_main"] == 1


def test_zweiter_link_wird_alt_account(edb):
    edb.link_user(111, 42, "Haupt")
    edb.link_user(111, 42, "Alt1")
    rows = edb.get_links_by_user(111, 42)
    assert len(rows) == 2
    haupt = [r for r in rows if r["is_main"]]
    alt = [r for r in rows if not r["is_main"]]
    assert len(haupt) == 1 and haupt[0]["ingame_name"] == "Haupt"
    assert len(alt) == 1 and alt[0]["ingame_name"] == "Alt1"


def test_get_links_by_user_hauptaccount_zuerst(edb):
    edb.link_user(111, 42, "Haupt")
    edb.link_user(111, 42, "Alt1")
    edb.link_user(111, 42, "Alt2")
    rows = edb.get_links_by_user(111, 42)
    assert rows[0]["ingame_name"] == "Haupt"


def test_get_main_link_by_user_liefert_nur_hauptaccount(edb):
    edb.link_user(111, 42, "Haupt")
    edb.link_user(111, 42, "Alt1")
    row = edb.get_main_link_by_user(111, 42)
    assert row["ingame_name"] == "Haupt"


def test_count_links_of_user(edb):
    assert edb.count_links_of_user(111, 42) == 0
    edb.link_user(111, 42, "Haupt")
    edb.link_user(111, 42, "Alt1")
    assert edb.count_links_of_user(111, 42) == 2


def test_name_bleibt_pro_guild_einem_user_zugeordnet(edb):
    edb.link_user(111, 42, "Umkaempft")
    ok, why = edb.link_user(111, 43, "Umkaempft")
    assert not ok and why == "name_taken"


def test_gleicher_name_in_anderer_guild_ist_frei(edb):
    edb.link_user(111, 42, "Name")
    ok, _why = edb.link_user(222, 43, "Name")
    assert ok


def test_unlink_specific_entfernt_nur_diesen_namen(edb):
    edb.link_user(111, 42, "Haupt")
    edb.link_user(111, 42, "Alt1")
    assert edb.unlink_specific(111, 42, "Alt1")
    rows = edb.get_links_by_user(111, 42)
    assert len(rows) == 1 and rows[0]["ingame_name"] == "Haupt"


def test_unlink_specific_unbekannter_name_liefert_false(edb):
    edb.link_user(111, 42, "Haupt")
    assert not edb.unlink_specific(111, 42, "Nichtvorhanden")


def test_unlink_hauptaccount_befoerdert_alt_zum_hauptaccount(edb):
    edb.link_user(111, 42, "Haupt")
    edb.link_user(111, 42, "Alt1")
    edb.unlink_specific(111, 42, "Haupt")
    rows = edb.get_links_by_user(111, 42)
    assert len(rows) == 1
    assert rows[0]["ingame_name"] == "Alt1"
    assert rows[0]["is_main"] == 1


def test_unlink_letzten_account_laesst_keinen_hauptaccount_uebrig(edb):
    edb.link_user(111, 42, "Haupt")
    edb.unlink_specific(111, 42, "Haupt")
    assert edb.get_links_by_user(111, 42) == []


# ── Slash-Commands (/link, /unlink) ──────────────────────────────────────
class _StubResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, embed=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class _StubUser:
    def __init__(self, id_):
        self.id = id_
        self.mention = f"<@{id_}>"

    def __str__(self):
        return f"User{self.id}"


class _StubInteraction:
    def __init__(self, user_id, guild_id=111):
        self.user = _StubUser(user_id)
        self.guild_id = guild_id
        self.locale = None
        self.response = _StubResponse()


class _FakeConnLink:
    def __init__(self, max_linked_accounts=1):
        self.service_id = "1000"
        self.parser = None
        self._werte = {"max_linked_accounts": max_linked_accounts,
                       "kill_reward": 0, "action_economy": {}}

    def get(self, key, default=None):
        return self._werte.get(key, default)


@pytest.fixture
def gepatchtes_link(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)

    async def fake_notify(*a, **k):
        return None
    monkeypatch.setattr(bot, "_notify_link_change", fake_notify)
    return edb


def _conn_stub(monkeypatch, max_linked_accounts=1):
    conn = _FakeConnLink(max_linked_accounts)
    monkeypatch.setattr(bot, "_conn_of", lambda interaction: conn)
    return conn


def test_link_erster_account_wird_hauptaccount(monkeypatch, gepatchtes_link):
    _conn_stub(monkeypatch, max_linked_accounts=2)
    gepatchtes_link.roster_upsert_login(111, "1000", "Haupt")
    interaction = _StubInteraction(42)
    _run(bot.cmd_link.callback(interaction, "Haupt"))
    rows = gepatchtes_link.get_links_by_user(111, 42)
    assert len(rows) == 1 and rows[0]["is_main"] == 1


def test_link_zweiter_account_wird_alt_wenn_limit_erlaubt(monkeypatch, gepatchtes_link):
    _conn_stub(monkeypatch, max_linked_accounts=2)
    gepatchtes_link.roster_upsert_login(111, "1000", "Haupt")
    gepatchtes_link.roster_upsert_login(111, "1000", "Alt1")
    _run(bot.cmd_link.callback(_StubInteraction(42), "Haupt"))
    _run(bot.cmd_link.callback(_StubInteraction(42), "Alt1"))
    rows = gepatchtes_link.get_links_by_user(111, 42)
    assert len(rows) == 2


def test_link_lehnt_ab_wenn_limit_erreicht(monkeypatch, gepatchtes_link):
    _conn_stub(monkeypatch, max_linked_accounts=1)
    gepatchtes_link.roster_upsert_login(111, "1000", "Haupt")
    gepatchtes_link.roster_upsert_login(111, "1000", "Alt1")
    _run(bot.cmd_link.callback(_StubInteraction(42), "Haupt"))
    interaction = _StubInteraction(42)
    _run(bot.cmd_link.callback(interaction, "Alt1"))
    assert gepatchtes_link.count_links_of_user(111, 42) == 1
    assert "❌" in interaction.response.sent[0]["content"]


def test_unlink_ohne_namen_bei_nur_einem_account(monkeypatch, gepatchtes_link):
    _conn_stub(monkeypatch, max_linked_accounts=2)
    gepatchtes_link.link_user(111, 42, "Haupt")
    interaction = _StubInteraction(42)
    _run(bot.cmd_unlink.callback(interaction, None))
    assert gepatchtes_link.get_links_by_user(111, 42) == []


def test_unlink_ohne_namen_bei_mehreren_accounts_wird_abgelehnt(monkeypatch, gepatchtes_link):
    gepatchtes_link.link_user(111, 42, "Haupt")
    gepatchtes_link.link_user(111, 42, "Alt1")
    interaction = _StubInteraction(42)
    _run(bot.cmd_unlink.callback(interaction, None))
    assert len(gepatchtes_link.get_links_by_user(111, 42)) == 2
    assert "❌" in interaction.response.sent[0]["content"]


def test_unlink_mit_namen_entfernt_gezielt_einen_account(monkeypatch, gepatchtes_link):
    gepatchtes_link.link_user(111, 42, "Haupt")
    gepatchtes_link.link_user(111, 42, "Alt1")
    interaction = _StubInteraction(42)
    _run(bot.cmd_unlink.callback(interaction, "Alt1"))
    rows = gepatchtes_link.get_links_by_user(111, 42)
    assert len(rows) == 1 and rows[0]["ingame_name"] == "Haupt"
