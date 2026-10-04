"""Battle Royale Builder (feste Gas-Ringe + Spawn-Blasen) und Locked Container
Builder (Vanilla-Schiffscontainer): Vorschau, Anwenden, Entfernen, Grenzen."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_airstrike import call, servers  # noqa: F401 - Fixture für pytest

SPAWN_VANILLA = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<playerspawnpoints>
    <fresh>
        <spawn_params>
            <min_dist_infected>20</min_dist_infected>
        </spawn_params>
        <generator_posbubbles>
            <pos x="4000" z="2000"/>
            <pos x="4100" z="2100"/>
        </generator_posbubbles>
    </fresh>
    <hop>
        <generator_posbubbles>
            <pos x="1" z="1"/>
        </generator_posbubbles>
    </hop>
</playerspawnpoints>
"""
TYPES_CHERNARUS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<types>
    <type name="M4A1">
        <nominal>5</nominal>
    </type>
    <type name="ShippingContainerKeys_Blue">
        <nominal>0</nominal>
        <lifetime>1800</lifetime>
        <restock>0</restock>
        <min>0</min>
    </type>
</types>
"""


@pytest.fixture(autouse=True)
def tool_files(monkeypatch):
    async def read(conn, name, _loop):
        path = "/mission/" + name
        return (conn.ftp.files[path], "ok") if path in conn.ftp.files else (None, "missing")

    async def write(conn, name, content, _loop):
        path = "/mission/" + name
        conn.ftp.writes.append(path)
        conn.ftp.files[path] = content
        return True

    async def delete(conn, name, _loop):
        conn.ftp.files.pop("/mission/" + name, None)
        return True

    monkeypatch.setattr(bot, "_tools_datei_lesen", read)
    monkeypatch.setattr(bot, "_tools_datei_schreiben", write)
    monkeypatch.setattr(bot, "_tools_datei_loeschen", delete)


def br_payload(**changes):
    value = {"suffix": "Runde1", "vorschau": True, "mittelpunkt": {"x": 7000, "z": 7000},
             "radius": 1000, "zonenradius": 80, "abstand": 500,
             "spawnpunkte": [{"x": 7100, "z": 7100}, {"x": 6900, "z": 6900}]}
    value.update(changes)
    return value


def locked_payload(**changes):
    value = {"suffix": "Blau1", "vorschau": True, "farbe": "blue",
             "position": {"x": 7000, "z": 7000, "a": 90},
             "loot_min": 5, "loot_max": 9, "schluessel_nominal": 0, "schluessel_min": 0,
             "inhalt": [{"item": "M4A1"}]}
    value.update(changes)
    return value


def _br_setup(conn):
    conn.data["map_name"] = "Chernarusplus"
    conn.ftp.files["/mission/cfgEffectArea.json"] = json.dumps(
        {"Areas": [{"AreaName": "Andere"}], "SafePositions": [[1, 2]], "Zukunft": 1})
    conn.ftp.files["/mission/cfgplayerspawnpoints.xml"] = SPAWN_VANILLA


def test_battle_royale_preview_apply_remove(monkeypatch, servers):
    conn, _ = servers
    _br_setup(conn)
    original = dict(conn.ftp.files)
    status, result = call(monkeypatch, conn, bot.api_tools_battleroyale_post, br_payload())
    assert status == 200, result
    assert result["data"]["zonen"] == 13 and conn.ftp.writes == [] and "battleroyale" not in conn.data
    status, result = call(monkeypatch, conn, bot.api_tools_battleroyale_post, br_payload(vorschau=False))
    assert status == 200, result
    effect = json.loads(conn.ftp.files["/mission/cfgEffectArea.json"])
    assert effect["Zukunft"] == 1 and effect["SafePositions"] == [[1, 2]]
    namen = [a["AreaName"] for a in effect["Areas"]]
    assert namen[0] == "Andere" and namen[1] == "BrigardeBR_Runde1_001" and len(namen) == 14
    assert effect["Areas"][1]["Type"] == "ContaminatedArea_Static"
    spawn = conn.ftp.files["/mission/cfgplayerspawnpoints.xml"]
    # Blasen liegen in der vorhandenen <generator_posbubbles> der fresh-Gruppe
    fresh = spawn[spawn.index("<fresh>"):spawn.index("</fresh>")]
    assert fresh.count("<generator_posbubbles>") == 1 and 'x="7100" z="7100"' in fresh
    assert "BRIGARDE-KILLFEED:START battleroyale_Runde1" in fresh
    assert conn.data["battleroyale"][0]["suffix"] == "Runde1"
    # Kollision
    status, _ = call(monkeypatch, conn, bot.api_tools_battleroyale_post, br_payload(vorschau=False))
    assert status == 409
    status, result = call(monkeypatch, conn, bot.api_tools_battleroyale_remove, {"suffix": "Runde1"})
    assert status == 200, result
    assert conn.ftp.files["/mission/cfgplayerspawnpoints.xml"] == original["/mission/cfgplayerspawnpoints.xml"]
    assert json.loads(conn.ftp.files["/mission/cfgEffectArea.json"]) == json.loads(original["/mission/cfgEffectArea.json"])
    assert conn.data["battleroyale"] == []


def test_battle_royale_missing_files_and_limits(monkeypatch, servers):
    conn, _ = servers
    conn.data["map_name"] = "Chernarusplus"
    # ohne Dateien: Grundstruktur wird angelegt
    status, result = call(monkeypatch, conn, bot.api_tools_battleroyale_post, br_payload(vorschau=False))
    assert status == 200, result
    assert "<generator_posbubbles>" in conn.ftp.files["/mission/cfgplayerspawnpoints.xml"]
    assert json.loads(conn.ftp.files["/mission/cfgEffectArea.json"])["Areas"]
    for changes in ({"radius": 10}, {"radius": 9000}, {"mittelpunkt": {"x": 99999, "z": 1}},
                    {"abstand": 50, "radius": 7000}, {"suffix": "böse"},
                    {"spawnpunkte": [{"x": 1, "z": 1}] * 101}):
        payload = br_payload(suffix="X")
        payload.update(changes)
        status, _ = call(monkeypatch, conn, bot.api_tools_battleroyale_post, payload)
        assert status == 400, changes


def _locked_setup(conn, karte="Chernarusplus", proto=False):
    conn.data["map_name"] = karte
    conn.ftp.files["/mission/mapgroupproto.xml"] = (
        '<?xml version="1.0"?>\n<mapgroupproto>\n'
        + ('    <group name="Land_ContainerLocked_Blue_DE" lootmax="9">\n    </group>\n' if proto else "")
        + '</mapgroupproto>\n')
    conn.ftp.files["/mission/db/types.xml"] = TYPES_CHERNARUS


def test_locked_container_chernarus_creates_proto_and_types(monkeypatch, servers):
    conn, _ = servers
    _locked_setup(conn)
    vorher = dict(conn.ftp.files)
    status, result = call(monkeypatch, conn, bot.api_tools_lockedcontainer_post, locked_payload())
    assert status == 200, result
    gen = {g["filename"]: g["content"] for g in result["data"]["generated"]}
    assert list(gen) == ["db/events.xml", "cfgeventspawns.xml", "mapgroupproto.xml", "db/types.xml"]
    assert "<lifetime>2400</lifetime>" in gen["db/events.xml"] and 'lootmax="9" lootmin="5" max="1" min="1"' in gen["db/events.xml"]
    assert '<proxy type="M4A1"' in gen["mapgroupproto.xml"] and gen["mapgroupproto.xml"].count("<point ") == 9
    assert '<type name="Land_ContainerLocked_Blue_DE">' in gen["db/types.xml"]
    assert "ShippingContainerKeys" not in gen["db/types.xml"]      # Blau-Schlüssel ist vorhanden
    assert result["data"]["schluessel"] == "ShippingContainerKeys_Blue" and conn.ftp.writes == []
    status, result = call(monkeypatch, conn, bot.api_tools_lockedcontainer_post, locked_payload(vorschau=False))
    assert status == 200, result
    assert sorted(conn.ftp.writes) == sorted(["/mission/db/events.xml", "/mission/cfgeventspawns.xml",
                                              "/mission/mapgroupproto.xml", "/mission/db/types.xml"])
    status, result = call(monkeypatch, conn, bot.api_tools_lockedcontainer_remove, {"suffix": "Blau1"})
    assert status == 200, result
    for pfad, inhalt in vorher.items():
        assert conn.ftp.files[pfad] == inhalt, pfad


def test_locked_container_red_key_and_key_nominal_restore(monkeypatch, servers):
    conn, _ = servers
    _locked_setup(conn)
    vorher = conn.ftp.files["/mission/db/types.xml"]
    # Rot: Schlüssel-Typ fehlt auf Chernarus -> wird mit nominal angelegt
    status, result = call(monkeypatch, conn, bot.api_tools_lockedcontainer_post,
                          locked_payload(farbe="red", schluessel_nominal=3, schluessel_min=1, inhalt=[]))
    assert status == 200, result
    gen = {g["filename"]: g["content"] for g in result["data"]["generated"]}
    assert '<type name="ShippingContainerKeys_Red">' in gen["db/types.xml"] and "<nominal>3</nominal>" in gen["db/types.xml"]
    # Blau: Schlüssel vorhanden -> nominal/min im Vanilla-Block geändert, Rest gleich
    status, result = call(monkeypatch, conn, bot.api_tools_lockedcontainer_post,
                          locked_payload(vorschau=False, schluessel_nominal=4, schluessel_min=2, inhalt=[]))
    assert status == 200, result
    assert result["data"]["schluessel_geaendert"] is True
    block = bot._tool_finde_benannten_block(conn.ftp.files["/mission/db/types.xml"], "type", "ShippingContainerKeys_Blue")["block"]
    assert "<nominal>4</nominal>" in block and "<min>2</min>" in block and "<lifetime>1800</lifetime>" in block
    status, result = call(monkeypatch, conn, bot.api_tools_lockedcontainer_remove, {"suffix": "Blau1"})
    assert status == 200, result
    assert conn.ftp.files["/mission/db/types.xml"] == vorher


def test_locked_container_sakhal_keeps_vanilla_proto(monkeypatch, servers):
    conn, _ = servers
    _locked_setup(conn, karte="Sakhal", proto=True)
    # feste Items nicht erlaubt, wenn der Proto-Block schon existiert
    status, result = call(monkeypatch, conn, bot.api_tools_lockedcontainer_post, locked_payload())
    assert status == 400 and "Proto-Block" in result["error"]
    status, result = call(monkeypatch, conn, bot.api_tools_lockedcontainer_post, locked_payload(vorschau=False, inhalt=[]))
    assert status == 200, result
    assert result["data"]["proto_angelegt"] is False
    assert conn.ftp.files["/mission/mapgroupproto.xml"].count("Land_ContainerLocked_Blue_DE") == 1
    assert "/mission/mapgroupproto.xml" not in conn.ftp.writes


def test_locked_container_validation(monkeypatch, servers):
    conn, _ = servers
    _locked_setup(conn)
    for changes in ({"farbe": "mod_locked"}, {"position": {"x": 99999, "z": 1}},
                    {"loot_min": 9, "loot_max": 5}, {"schluessel_nominal": 1, "schluessel_min": 2},
                    {"inhalt": [{"item": "GibtEsNicht"}]}, {"inhalt": [{"item": "M4A1"}] * 10},
                    {"inhalt": [{"item": "böse klasse"}]}):
        status, _ = call(monkeypatch, conn, bot.api_tools_lockedcontainer_post, locked_payload(vorschau=False, **changes))
        assert status == 400, changes
    assert conn.ftp.writes == []


def test_locked_container_rollback_and_tenants(monkeypatch, servers):
    a, b = servers
    _locked_setup(a)
    _locked_setup(b)
    vorher = dict(a.ftp.files)
    write_ok = bot._tools_datei_schreiben

    async def write(conn, name, content, _loop):
        if name == "db/types.xml":
            return False
        path = "/mission/" + name
        conn.ftp.writes.append(path)
        conn.ftp.files[path] = content
        return True
    monkeypatch.setattr(bot, "_tools_datei_schreiben", write)
    status, _ = call(monkeypatch, a, bot.api_tools_lockedcontainer_post, locked_payload(vorschau=False))
    assert status == 502
    assert a.ftp.files == vorher and "lockedcontainers" not in a.data
    monkeypatch.setattr(bot, "_tools_datei_schreiben", write_ok)
    # Mandanten getrennt
    status, _ = call(monkeypatch, a, bot.api_tools_lockedcontainer_post, locked_payload(vorschau=False))
    assert status == 200
    status, result = call(monkeypatch, b, bot.api_tools_lockedcontainer_get)
    assert status == 200 and result["data"]["eintraege"] == []
    status, _ = call(monkeypatch, b, bot.api_tools_lockedcontainer_remove, {"suffix": "Blau1"})
    assert status == 404
    assert "StaticLocked_Blau1" not in b.ftp.files["/mission/db/events.xml"]
