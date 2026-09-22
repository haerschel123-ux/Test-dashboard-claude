"""Regressionstest fuer das eigentliche Kernproblem, das Brigarde meldete:
"nitrado server 1 nicht sowohl auf discord server 1 als auch 2 freigeschaltet
werden kann [...] sowas wie /neustart funktioniert nur auf dem zuletzt
registrierten". Ursache war, dass ServerConnection.guild_id ein SKALAR war -
eine zweite Zuordnung (assign_guild) ersetzte die erste stillschweigend.

Seit dem Mehrfach-Guild-Umbau ist guild_ids eine Liste: add_guild() fuegt
hinzu statt zu ersetzen, remove_guild() entfernt gezielt eine einzelne Guild.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_mehrfach_guild.py -v
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def test_zweite_guild_ersetzt_nicht_die_erste():
    service_id = "mfg-server-1"
    bot.connections.upsert(service_id, nitrado_token="fake", owner_discord_id="1")
    okay1, _ = bot.connections.add_guild(service_id, 111)
    okay2, _ = bot.connections.add_guild(service_id, 222)
    conn = bot.connections.for_service(service_id)
    assert okay1 and okay2
    assert set(conn.guild_ids) == {111, 222}


def test_all_for_guild_findet_server_ueber_beide_guilds():
    service_id = "mfg-server-2"
    bot.connections.upsert(service_id, nitrado_token="fake", owner_discord_id="2")
    bot.connections.add_guild(service_id, 333)
    bot.connections.add_guild(service_id, 444)
    conn = bot.connections.for_service(service_id)
    assert conn in bot.connections.all_for_guild(333)
    assert conn in bot.connections.all_for_guild(444)
    assert conn not in bot.connections.all_for_guild(555)


def test_remove_guild_entfernt_gezielt_nur_eine():
    service_id = "mfg-server-3"
    bot.connections.upsert(service_id, nitrado_token="fake", owner_discord_id="3")
    bot.connections.add_guild(service_id, 666)
    bot.connections.add_guild(service_id, 777)
    okay, _ = bot.connections.remove_guild(service_id, 666)
    conn = bot.connections.for_service(service_id)
    assert okay
    assert conn.guild_ids == [777]


def test_add_guild_ist_idempotent():
    service_id = "mfg-server-4"
    bot.connections.upsert(service_id, nitrado_token="fake", owner_discord_id="4")
    bot.connections.add_guild(service_id, 888)
    bot.connections.add_guild(service_id, 888)
    conn = bot.connections.for_service(service_id)
    assert conn.guild_ids == [888]


def test_migration_von_alter_einzelner_guild_id():
    # Alte connections.json-Zeile von vor dem Umbau: nur "guild_id", kein
    # "guild_ids" - muss weiterhin lesend funktionieren.
    conn = bot.ServerConnection({"service_id": "mfg-alt", "guild_id": 999})
    assert conn.guild_ids == [999]
    assert conn.guild_id == 999


def test_sicherheitspruefung_lehnt_dritte_fremde_guild_ab():
    service_id = "mfg-server-5"
    bot.connections.upsert(service_id, nitrado_token="fake", owner_discord_id="5")
    bot.connections.add_guild(service_id, 111)
    bot.connections.add_guild(service_id, 222)
    conn = bot.connections.for_service(service_id)
    assert 111 in conn.guild_ids and 222 in conn.guild_ids
    assert 333 not in conn.guild_ids


def test_guild_ids_requested_erlaubt_mehrere_gleichzeitige_anfragen():
    conn = bot.ServerConnection({"service_id": "mfg-req"})
    conn.data["guild_ids_requested"] = [111]
    offen = conn.guild_ids_requested
    if 222 not in offen:
        conn.data["guild_ids_requested"] = offen + [222]
    assert set(conn.guild_ids_requested) == {111, 222}


def test_zonen_ziel_waehlt_erste_guild_ohne_angabe():
    service_id = "mfg-zone-1"
    bot.connections.upsert(service_id, nitrado_token="fake", owner_discord_id="6")
    bot.connections.add_guild(service_id, 111)
    bot.connections.add_guild(service_id, 222)
    conn = bot.connections.for_service(service_id)
    ziel, denied = bot._zonen_ziel(None, conn, {})
    assert denied is None
    assert ziel["guild_id"] == 111


def test_zonen_ziel_akzeptiert_gezielte_zweite_guild():
    service_id = "mfg-zone-2"
    bot.connections.upsert(service_id, nitrado_token="fake", owner_discord_id="7")
    bot.connections.add_guild(service_id, 111)
    bot.connections.add_guild(service_id, 222)
    conn = bot.connections.for_service(service_id)
    ziel, denied = bot._zonen_ziel(None, conn, {"guild_id": 222})
    assert denied is None
    assert ziel["guild_id"] == 222


def test_zonen_ziel_lehnt_fremde_guild_ab():
    service_id = "mfg-zone-3"
    bot.connections.upsert(service_id, nitrado_token="fake", owner_discord_id="8")
    bot.connections.add_guild(service_id, 111)
    conn = bot.connections.for_service(service_id)
    ziel, denied = bot._zonen_ziel(None, conn, {"guild_id": 999})
    assert ziel is None
    assert denied is not None
    assert denied.status == 403
