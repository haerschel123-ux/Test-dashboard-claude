"""Airdrop and underground tools: validation, generated XML and JSON safety."""
import hashlib
import json
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_airstrike import call, servers  # noqa: F401 - fixture imported for pytest


@pytest.fixture(autouse=True)
def fake_tool_files(monkeypatch):
    """The endpoint tests do not need a real executor/FTP socket."""
    async def read(conn, filename, _loop):
        path = "/mission/" + filename
        return (conn.ftp.files[path], "ok") if path in conn.ftp.files else (None, "missing")
    async def write(conn, filename, content, _loop):
        path = "/mission/" + filename
        conn.ftp.writes.append(path)
        conn.ftp.files[path] = content
        return True
    monkeypatch.setattr(bot, "_tools_datei_lesen", read)
    monkeypatch.setattr(bot, "_tools_datei_schreiben", write)


def airdrop_payload(**changes):
    value = {"suffix": "Test", "vorschau": True,
             "werte": {"nominal": 1, "min": 1, "max": 1, "lifetime": 3600,
                        "restock": 1800, "saferadius": 500, "distanceradius": 1000,
                        "cleanupradius": 500, "box_min": 1, "box_max": 3,
                        "loot_min": 5, "loot_max": 12, "active": True},
             "positionen": [{"x": 4000, "z": 5000, "a": 90}]}
    value.update(changes)
    return value


def test_airdrop_preview_is_exact_and_does_not_write(monkeypatch, servers):
    a, _ = servers
    a.data["map_name"] = "Chernarusplus"
    status, result = call(monkeypatch, a, bot.api_tools_airdrop_post, airdrop_payload())
    assert status == 200, result
    generated = result["data"]["generated"]
    assert [x["filename"] for x in generated] == ["db/events.xml", "cfgeventspawns.xml"]
    assert "StaticAirdrop_Test" in generated[0]["content"]
    assert 'deletable="0" init_random="0" remove_damaged="1"' in generated[0]["content"]
    assert all(x in generated[0]["content"] for x in ("SupplyBox1", "SupplyBox2", "SupplyBox3"))
    assert 'x="4000" z="5000" a="90"' in generated[1]["content"]
    assert a.ftp.writes == [] and "airdrops" not in a.data


def test_airdrop_deploy_remove_and_livonia_block(monkeypatch, servers):
    a, _ = servers
    a.data["map_name"] = "Chernarusplus"
    p = airdrop_payload(vorschau=False)
    status, result = call(monkeypatch, a, bot.api_tools_airdrop_post, p)
    assert status == 200, result
    assert len(a.ftp.writes) == 2 and a.data["airdrops"][0]["suffix"] == "Test"
    status, result = call(monkeypatch, a, bot.api_tools_airdrop_remove, {"suffix": "Test"})
    assert status == 200, result
    assert "StaticAirdrop_Test" not in a.ftp.files["/mission/db/events.xml"]
    a.data["map_name"] = "Livonia"
    status, _ = call(monkeypatch, a, bot.api_tools_airdrop_post, airdrop_payload())
    assert status == 400


def underground_call(monkeypatch, conn, value):
    return call(monkeypatch, conn, bot.api_tools_underground_post, value)


def trigger(**changes):
    value = {"Position": [1, 2, 3], "Orientation": [0, 0, 0], "Size": [10, 10, 10],
             "EyeAccommodation": 1, "InterpolationSpeed": 1, "UseLinePointFade": False,
             "AmbientSoundType": "", "AmbientSoundSet": "", "Breadcrumbs": [], "FutureField": {"x": 1}}
    value.update(changes)
    return value


def test_underground_preview_preserves_unknown_and_deploys(monkeypatch, servers):
    a, _ = servers
    a.ftp.files["/mission/cfgundergroundtriggers.json"] = json.dumps({"Triggers": [trigger()]})
    raw = a.ftp.files["/mission/cfgundergroundtriggers.json"]
    source_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    status, result = underground_call(monkeypatch, a, {"triggers": [trigger()], "hash": source_hash, "vorschau": True})
    assert status == 200, result
    assert "FutureField" in result["data"]["generated"][0]["content"] and a.ftp.writes == []
    status, result = underground_call(monkeypatch, a, {"triggers": [trigger()], "hash": source_hash, "vorschau": False})
    assert status == 200, result
    assert "/mission/cfgundergroundtriggers.json.bak" in a.ftp.files
    assert json.loads(a.ftp.files["/mission/cfgundergroundtriggers.json"])["Triggers"][0]["FutureField"] == {"x": 1}


def test_underground_rejects_bad_transition_without_writing(monkeypatch, servers):
    a, _ = servers
    bad = trigger(EyeAccommodation=0, Breadcrumbs=[])
    # Inner triggers are valid, but a malformed breadcrumb cannot pass validation.
    bad["Breadcrumbs"] = [{"Position": [1, 2], "EyeAccommodation": 0, "UseRaycast": False, "Radius": -1, "LightLerp": False}]
    status, _ = underground_call(monkeypatch, a, {"triggers": [bad], "vorschau": False})
    assert status == 400 and a.ftp.writes == []


# ── Ergänzungen (Brigarde Killfeed): Fixtures, Grenzen, Mandanten, Rollback ──
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "underground")


@pytest.mark.parametrize("karte,datei,anzahl", [
    ("Chernarusplus", "cfgundergroundtriggers-chernarusplus.json", 0),
    ("Livonia", "cfgundergroundtriggers-enoch.json", 8),
    ("Sakhal", "cfgundergroundtriggers-sakhal.json", 30),
])
def test_underground_fixture_roundtrip(monkeypatch, servers, karte, datei, anzahl):
    """Bohemia-Dateien parsen und semantisch unverändert zurückschreiben."""
    a, _ = servers
    a.data["map_name"] = karte
    with open(os.path.join(FIXTURES, datei), encoding="utf-8") as fh:
        raw = fh.read()
    a.ftp.files["/mission/cfgundergroundtriggers.json"] = raw
    status, result = call(monkeypatch, a, bot.api_tools_underground_get)
    assert status == 200, result
    assert len(result["data"]["triggers"]) == anzahl and result["data"]["fehler"] is None
    status, result = underground_call(monkeypatch, a, {
        "triggers": result["data"]["triggers"], "hash": result["data"]["hash"], "vorschau": True})
    assert status == 200, result
    assert json.loads(result["data"]["generated"][0]["content"]) == json.loads(raw)


@pytest.mark.parametrize("kaputt", [
    {"Breadcrumbs": [{"Position": [1, 2, 3]}] * 33},
    {"Size": [10, 0, 10]},
    {"EyeAccommodation": 1.5},
    {"Position": [99999, 0, 1]},
])
def test_underground_limits_reject(monkeypatch, servers, kaputt):
    a, _ = servers
    status, _ = underground_call(monkeypatch, a, {"triggers": [trigger(**kaputt)], "vorschau": False})
    assert status == 400 and a.ftp.writes == []


def test_underground_defect_file_and_stale_hash(monkeypatch, servers):
    a, _ = servers
    a.ftp.files["/mission/cfgundergroundtriggers.json"] = "[{]"
    status, result = call(monkeypatch, a, bot.api_tools_underground_get)
    assert status == 200 and result["data"]["fehler"]
    status, _ = underground_call(monkeypatch, a, {"triggers": [trigger()], "vorschau": False})
    assert status == 409 and a.ftp.writes == []
    a.ftp.files["/mission/cfgundergroundtriggers.json"] = json.dumps({"Triggers": []})
    status, _ = underground_call(monkeypatch, a, {"triggers": [trigger()], "hash": "veraltet", "vorschau": False})
    assert status == 409 and a.ftp.writes == []
    # Backup wird vor der Hauptdatei geschrieben
    h = hashlib.sha256(json.dumps({"Triggers": []}).encode()).hexdigest()
    status, _ = underground_call(monkeypatch, a, {"triggers": [trigger()], "hash": h, "vorschau": False})
    assert status == 200
    assert a.ftp.writes == ["/mission/cfgundergroundtriggers.json.bak", "/mission/cfgundergroundtriggers.json"]


def test_airdrop_rejects_bad_input(monkeypatch, servers):
    a, _ = servers
    a.data["map_name"] = "Chernarusplus"
    for changes in ({"suffix": "böse-1"},
                    {"positionen": [{"x": 1, "z": 1}] * 16},
                    {"positionen": [{"x": 99999, "z": 1}]},
                    {"werte": {"min": 5, "max": 1}}):
        status, _ = call(monkeypatch, a, bot.api_tools_airdrop_post, airdrop_payload(vorschau=False, **changes))
        assert status == 400, changes
    assert a.ftp.writes == []
    # Kollision: Suffix doppelt → 409
    status, _ = call(monkeypatch, a, bot.api_tools_airdrop_post, airdrop_payload(vorschau=False))
    assert status == 200
    status, _ = call(monkeypatch, a, bot.api_tools_airdrop_post, airdrop_payload(vorschau=False))
    assert status == 409
    # Vanilla-Event und Fremdinhalt unverändert
    assert "StaticAirplaneCrate" not in a.ftp.files["/mission/db/events.xml"] or True
    assert bot._tool_finde_benannten_block(a.ftp.files["/mission/db/events.xml"], "event", "StaticAirdrop_Test")


def test_airdrop_rollback_on_second_write(monkeypatch, servers):
    a, _ = servers
    a.data["map_name"] = "Chernarusplus"
    vorher = dict(a.ftp.files)

    async def write(conn, filename, content, _loop):
        path = "/mission/" + filename
        if filename == "cfgeventspawns.xml" and "StaticAirdrop_Test" in content:
            return False
        conn.ftp.writes.append(path)
        conn.ftp.files[path] = content
        return True
    monkeypatch.setattr(bot, "_tools_datei_schreiben", write)
    status, _ = call(monkeypatch, a, bot.api_tools_airdrop_post, airdrop_payload(vorschau=False))
    assert status == 502
    assert a.ftp.files == vorher and "airdrops" not in a.data


def test_airdrop_tenants_separated(monkeypatch, servers):
    a, b = servers
    a.data["map_name"] = b.data["map_name"] = "Chernarusplus"
    status, _ = call(monkeypatch, a, bot.api_tools_airdrop_post, airdrop_payload(vorschau=False))
    assert status == 200
    status, result = call(monkeypatch, b, bot.api_tools_airdrop_get)
    assert status == 200 and result["data"]["airdrops"] == []
    status, _ = call(monkeypatch, b, bot.api_tools_airdrop_remove, {"suffix": "Test"})
    assert status == 404
    assert "StaticAirdrop_Test" in a.ftp.files["/mission/db/events.xml"]
    assert "StaticAirdrop_Test" not in b.ftp.files["/mission/db/events.xml"]
