"""Discord Management je Guild: ein Nitrado-Server, mehrere Discord-Server.

Willkommen/Verlassen, Reaction Roles, Tickets, Level-System und Honeypot
enthalten Channel- und Rollen-IDs EINER Guild. Haengt ein Nitrado-Server an
zwei Guilds, darf die Einstellung der einen nie in der anderen wirken.

    python3 -m pytest tests/test_guild_trennung.py -q
"""
import asyncio
import json
import os
import sys
import time

from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_airstrike import servers  # noqa: F401 - Fixture für pytest

G_A = 144749039048694173   # z. B. „After The Outbreak“
G_B = 155555555555555555   # z. B. „Bot test 2“
K_A, K_B = 144700000000000001, 155500000000000001   # je ein Channel
R_B = 155500000000000099                            # Rolle in Guild B


class _Kanal:
    def __init__(self, id_, guild):
        self.id, self.guild, self.sent = id_, guild, []

    async def send(self, content=None, embed=None, **_):
        self.sent.append(embed)


class _Guild:
    def __init__(self, id_, rollen=()):
        self.id, self.name, self._rollen = id_, f"Guild {id_}", set(rollen)

    def get_role(self, rid):
        return object() if rid in self._rollen else None


class _Bot:
    def __init__(self, bereit=True):
        self.guilds = {G_A: _Guild(G_A), G_B: _Guild(G_B, [R_B])}
        self.kanaele = {K_A: _Kanal(K_A, self.guilds[G_A]), K_B: _Kanal(K_B, self.guilds[G_B])}
        self._bereit = bereit

    def get_channel(self, cid):
        return self.kanaele.get(int(cid))

    def get_guild(self, gid):
        return self.guilds.get(int(gid))

    def is_ready(self):
        return self._bereit


def _zwei_guilds(conn):
    bot.connections.add_guild(conn.service_id, G_A)
    bot.connections.add_guild(conn.service_id, G_B)
    assert conn.guild_ids == [G_A, G_B]


def test_einzelne_guild_speichert_wie_bisher(servers):
    a, _ = servers
    bot.connections.add_guild(a.service_id, G_A)
    sicht = bot.GuildSicht(a, G_A)
    sicht.set("honeypot", {"enabled": True})
    # Format der connections.json unverändert: direkt am Server, kein guild_daten
    assert a.data["honeypot"] == {"enabled": True} and "guild_daten" not in a.data
    assert sicht.get("honeypot") == {"enabled": True}
    # Nicht-Guild-Schlüssel gehen unverändert an den Server
    assert sicht.service_id == a.service_id and sicht.guild_ids == [G_A]


def test_zwei_guilds_getrennt_und_altdaten_nach_channel_verteilt(monkeypatch, servers):
    a, _ = servers
    monkeypatch.setattr(bot, "bot", _Bot(bereit=True))
    # Alte, gemeinsame Einstellungen: Willkommen zeigt in Guild B, Honeypot in A,
    # Tickets über eine Rolle in B, Level ohne IDs.
    a.data["welcome_message"] = {"enabled": True, "channel_id": str(K_B), "language": "de"}
    a.data["honeypot"] = {"enabled": True, "channel_id": str(K_A)}
    a.data["ticket_categories"] = [{"id": 1, "label": "Support", "role_ids": [str(R_B)]}]
    a.data["ticket_language"] = "en"
    a.data["level_system"] = {"enabled": True}
    _zwei_guilds(a)
    sa, sb = bot.GuildSicht(a, G_A), bot.GuildSicht(a, G_B)
    assert sb.get("welcome_message")["channel_id"] == str(K_B) and sa.get("welcome_message") is None
    assert sa.get("honeypot")["channel_id"] == str(K_A) and sb.get("honeypot") is None
    # Ticket-Schlüssel wandern gemeinsam (Rolle gehört zu B)
    assert sb.get("ticket_language") == "en" and sb.get("ticket_categories") and sa.get("ticket_categories") is None
    # Ohne ID: erste Guild
    assert sa.get("level_system") == {"enabled": True} and sb.get("level_system") is None
    assert not any(k in a.data for k in bot._GUILD_SCHLUESSEL)
    # Ab jetzt getrennt speichern
    sa.set("honeypot", {"enabled": False})
    sb.set("honeypot", {"enabled": True, "channel_id": str(K_B)})
    assert sa.get("honeypot") == {"enabled": False} and sb.get("honeypot")["channel_id"] == str(K_B)
    gespeichert = json.loads(json.dumps(a.data["guild_daten"]))
    assert set(gespeichert) == {str(G_A), str(G_B)}


def test_vor_bot_bereit_keine_verteilung_erste_guild_liest_altdaten(monkeypatch, servers):
    a, _ = servers
    monkeypatch.setattr(bot, "bot", _Bot(bereit=False))
    a.data["honeypot"] = {"enabled": True, "channel_id": str(K_B)}
    _zwei_guilds(a)
    assert bot.GuildSicht(a, G_A).get("honeypot") == {"enabled": True, "channel_id": str(K_B)}
    assert bot.GuildSicht(a, G_B).get("honeypot") is None
    assert "honeypot" in a.data   # noch nicht verteilt
    assert bot.guild_sicht(a, 999) is None   # fremde Guild: keine Sicht


def test_willkommen_postet_nur_in_der_eigenen_guild(monkeypatch, servers):
    a, _ = servers
    fake = _Bot(bereit=True)
    monkeypatch.setattr(bot, "bot", fake)
    _zwei_guilds(a)
    bot.GuildSicht(a, G_B).set("welcome_message", {"enabled": True, "channel_id": str(K_B), "language": "de"})
    mitglied = type("M", (), {"guild": fake.guilds[G_A], "id": 1, "display_name": "X", "mention": "@X",
                              "display_avatar": None, "avatar": None, "name": "X"})()
    monkeypatch.setattr(bot, "_welcome_leave_embed", lambda *a_, **k: "EMBED")
    asyncio.run(bot._welcome_leave_posten(mitglied, [a], True))
    assert fake.kanaele[K_B].sent == []          # Beitritt in A postet NICHT in B
    mitglied.guild = fake.guilds[G_B]
    asyncio.run(bot._welcome_leave_posten(mitglied, [a], True))
    assert fake.kanaele[K_B].sent == ["EMBED"]


def _call(monkeypatch, conn, handler, value=None, guild_id=None):
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": True,
                            "service_id": conn.service_id, "seen": time.time(),
                            "guild_id": str(guild_id) if guild_id else None}
    request = make_mocked_request("GET" if value is None else "POST", "/api/discord-management",
                                 headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})

    async def body(_request):
        return value
    monkeypatch.setattr(bot, "body", body)
    response = asyncio.run(handler(request))
    return response.status, json.loads(response.body)


def test_dashboard_nutzt_die_gewaehlte_guild(monkeypatch, servers):
    a, _ = servers
    monkeypatch.setattr(bot, "bot", _Bot(bereit=True))
    _zwei_guilds(a)
    bot.GuildSicht(a, G_B).set("level_system", {"enabled": True})
    # Gewählt ist Guild A → Level-System dort aus, obwohl B es nutzt
    status, result = _call(monkeypatch, a, bot.get_discord_mgmt_level, guild_id=G_A)
    assert status == 200 and result["data"]["enabled"] is False
    status, result = _call(monkeypatch, a, bot.get_discord_mgmt_level, guild_id=G_B)
    assert status == 200 and result["data"]["enabled"] is True
    # Ohne gewählte Guild: erste zugeordnete
    status, result = _call(monkeypatch, a, bot.get_discord_mgmt_level)
    assert status == 200 and result["data"]["enabled"] is False
    # Kachel-Übersicht ebenso
    status, result = _call(monkeypatch, a, bot.get_discord_mgmt, guild_id=G_B)
    assert status == 200 and result["data"]["level_system"]["enabled"] is True
