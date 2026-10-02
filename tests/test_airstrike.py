"""Airstrike XML transactions with real flat tenant persistence and fake FTP."""
import asyncio
import copy
import json
import os
import sys
import time
import xml.etree.ElementTree as ET

import pytest
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

EVENTS = '<?xml version="1.0"?>\n<events>\n    <!-- unverändert ü -->\n    <event name="StaticOriginal"><nominal>1</nominal></event>\n</events>\n'
SPAWNS = '<?xml version="1.0"?>\r\n<eventposdef>\r\n    <!-- Original -->\r\n</eventposdef>\r\n'
TYPES = '<types><type name="ExplosionTest"/><type name="Test_Custom"/></types>'


class FTP:
    def __init__(self):
        self.files = {"/mission/db/events.xml": EVENTS,
                      "/mission/cfgeventspawns.xml": SPAWNS, "/mission/db/types.xml": TYPES}
        self.writes = []
        self.fail_at = None
        self.raise_at = None

    def list_dir(self, directory):
        return [path for path in self.files if path.startswith(directory.rstrip("/") + "/")]

    def read_file_ex(self, path):
        return (self.files[path], "ok") if path in self.files else (None, "missing")

    def write_file(self, path, content):
        self.writes.append(path)
        # Even a failed FTP write may have partially changed the remote file.
        self.files[path] = content
        if len(self.writes) == self.raise_at:
            raise OSError("partial FTP write")
        return len(self.writes) != self.fail_at


@pytest.fixture
def servers(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    flat = {sid: {"service_id": sid, "nitrado_token": "fake", "owner_discord_id": sid,
                  "ftp_mission_dir": "/mission", "map_name": "Livonia"}
            for sid in ("1000", "2000")}
    (tmp_path / "connections.json").write_text(json.dumps(flat))
    registry = bot.ConnectionRegistry()
    registry.load()
    monkeypatch.setattr(bot, "connections", registry)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    for conn in registry._conns.values():
        conn.ftp = FTP()
    return registry._conns["1000"], registry._conns["2000"]


def payload(**changes):
    value = {"suffix": "Test", "klasse": "ExplosionTest", "commit": False,
             "positions": [{"x": 4000, "z": 5000}, {"x": 4100, "z": 5100}]}
    value.update(changes)
    return value


def call(monkeypatch, conn, handler, value=None, session=None):
    sid = session or str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": True,
                            "service_id": conn.service_id, "seen": time.time()}
    request = make_mocked_request("GET" if value is None else "POST", "/api/tools/airstrike",
                                 headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    async def body(_request):
        return value
    monkeypatch.setattr(bot, "body", body)
    response = asyncio.run(handler(request))
    return response.status, json.loads(response.body)


def deploy(monkeypatch, conn, **changes):
    return call(monkeypatch, conn, bot.api_tools_airstrike_deploy, payload(**changes))


def test_preview_has_two_files_and_no_writes(monkeypatch, servers, tmp_path):
    a, _b = servers
    disk = (tmp_path / "connections.json").read_bytes()
    original = copy.deepcopy(a.ftp.files)
    status, result = deploy(monkeypatch, a)
    assert status == 200, result
    assert result["data"]["event_name"] == "Airstrike_Test"
    assert [f["filename"] for f in result["data"]["generated"]] == ["db/events.xml", "cfgeventspawns.xml"]
    assert a.ftp.files == original and a.ftp.writes == [] and "airstrikes" not in a.data
    assert (tmp_path / "connections.json").read_bytes() == disk


def test_commit_exact_parameters_and_original_bytes(monkeypatch, servers):
    a, _b = servers
    parameters = {"nominal": 3, "min": 2, "max": 4, "lifetime": 71, "restock": 333,
                  "saferadius": 12, "distanceradius": 23, "cleanupradius": 34,
                  "child_min": 2, "child_max": 3, "lootmin": 4, "lootmax": 5,
                  "flags": {"deletable": 1, "init_random": 0, "remove_damaged": 0}}
    status, result = deploy(monkeypatch, a, commit=True, parameters=parameters)
    assert status == 200, result
    dep_id = result["data"]["id"]
    assert a.ftp.writes == ["/mission/db/events.xml", "/mission/cfgeventspawns.xml"]
    assert a.ftp.files["/mission/db/types.xml"] == TYPES
    for path, before in (("/mission/db/events.xml", EVENTS), ("/mission/cfgeventspawns.xml", SPAWNS)):
        raw = a.ftp.files[path]
        ET.fromstring(raw)
        assert len(bot._dayzcode_segmente(raw)) == 1
        restored, found = bot._dayzcode_entfernen(raw, dep_id)
        assert found and restored.encode() == before.encode()
    event = ET.fromstring(a.ftp.files["/mission/db/events.xml"]).find("event[@name='Airstrike_Test']")
    for key in ("nominal", "min", "max", "lifetime", "restock", "saferadius", "distanceradius", "cleanupradius"):
        assert event.findtext(key) == str(parameters[key])
    assert event.find("flags").attrib == {k: str(v) for k, v in parameters["flags"].items()}
    assert event.find("children/child").attrib == {"min": "2", "max": "3", "lootmin": "4", "lootmax": "5", "type": "ExplosionTest"}
    assert event.findtext("position") == "fixed"
    spawn = ET.fromstring(a.ftp.files["/mission/cfgeventspawns.xml"]).find("event")
    assert [(float(p.get("x")), float(p.get("z")), p.get("a")) for p in spawn] == [(4000, 5000, "0"), (4100, 5100, "0")]
    assert a.data["airstrikes"][0]["parameters"] == parameters


@pytest.mark.parametrize("changes", [
    {"suffix": "<evil>"}, {"suffix": "A" * 25}, {"suffix": ""}, {"suffix": 1},
    {"klasse": "Missing_Class"}, {"klasse": "<evil>"}, {"klasse": "A" * 65}, {"klasse": ""},
    {"commit": 1}, {"commit": "false"}, {"positions": []},
    {"positions": [{"x": 1, "z": 2}] * 51}, {"positions": {}},
    {"positions": [{"x": -1, "z": 0}]}, {"positions": [{"x": 12801, "z": 0}]},
    {"positions": [{"x": 1, "z": 12801}]}, {"positions": [{"x": True, "z": 0}]},
    {"positions": [{"x": float("nan"), "z": 0}]}, {"positions": [{"x": 0, "z": float("inf")}]},
    {"positions": [{"x": "1", "z": 0}]}, {"positions": [None]},
    {"positions": [{"x": 10 ** 1000, "z": 0}]},
    {"parameters": {"min": 4, "max": 2}}, {"parameters": {"nominal": 3, "max": 2}},
    {"parameters": {"lifetime": 0}}, {"parameters": {"lifetime": 86401}},
    {"parameters": {"restock": 86401}}, {"parameters": {"restock": -1}},
    {"parameters": {"saferadius": 0.5}}, {"parameters": {"nominal": True}},
    {"parameters": {"cleanupradius": 2147483648}}, {"parameters": {"child_min": 2, "child_max": 1}},
    {"parameters": {"lootmin": 2, "lootmax": 1}}, {"parameters": {"unknown": 1}},
    {"parameters": None}, {"parameters": {"flags": {"deletable": 2}}},
    {"parameters": {"flags": {"init_random": True}}}, {"parameters": {"flags": {"unknown": 0}}},
    {"parameters": {"flags": []}},
])
def test_invalid_input_is_400_without_writes(monkeypatch, servers, changes):
    a, _b = servers
    status, result = deploy(monkeypatch, a, **changes)
    assert status == 400, result
    assert a.ftp.writes == [] and "airstrikes" not in a.data


@pytest.mark.parametrize("filename", ["db/events.xml", "cfgeventspawns.xml"])
def test_collision_with_unmanaged_event_is_409(monkeypatch, servers, filename):
    a, _b = servers
    path = "/mission/" + filename
    tag = "events" if filename.startswith("db/") else "eventposdef"
    a.ftp.files[path] = f'<{tag}><event name="Airstrike_Test"/></{tag}>'
    status, result = deploy(monkeypatch, a)
    assert status == 409, result
    assert a.ftp.writes == []


@pytest.mark.parametrize("failure,operation", [(1, "false"), (2, "false"), (2, "raise")])
def test_partial_write_is_rolled_back(monkeypatch, servers, failure, operation):
    a, _b = servers
    original = copy.deepcopy(a.ftp.files)
    if operation == "false":
        a.ftp.fail_at = failure
    else:
        a.ftp.raise_at = failure
    status, result = deploy(monkeypatch, a, commit=True)
    assert status == 502, result
    assert a.ftp.files == original and "airstrikes" not in a.data


def test_manifest_failure_after_disk_save_is_rolled_back(monkeypatch, servers, tmp_path):
    a, _b = servers
    before = json.loads((tmp_path / "connections.json").read_text())
    original_save = bot.connections.save
    calls = []
    def save(*, strict=False):
        calls.append(strict)
        original_save(strict=strict)
        if len(calls) == 1:
            raise OSError("failure after save")
    monkeypatch.setattr(bot.connections, "save", save)
    status, result = deploy(monkeypatch, a, commit=True)
    assert status == 502, result
    assert calls == [True, True]
    assert a.ftp.files["/mission/db/events.xml"] == EVENTS and a.ftp.files["/mission/cfgeventspawns.xml"] == SPAWNS
    assert "airstrikes" not in a.data
    assert json.loads((tmp_path / "connections.json").read_text()) == before


def test_tenants_persist_reload_and_remove_byte_exact(monkeypatch, servers, tmp_path):
    a, b = servers
    status, result = deploy(monkeypatch, a, commit=True)
    assert status == 200, result
    dep_id = result["data"]["id"]
    disk = json.loads((tmp_path / "connections.json").read_text())
    assert len(disk["1000"]["airstrikes"]) == 1 and "airstrikes" not in disk["2000"]
    assert "airstrikes" not in bot.cfg.config
    reloaded = bot.ConnectionRegistry()
    reloaded.load()
    assert bot._airstrikes(reloaded._conns["1000"])[0]["id"] == dep_id
    assert bot._airstrikes(reloaded._conns["2000"]) == []
    assert call(monkeypatch, b, bot.api_tools_airstrike_get)[1]["data"]["deployments"] == []
    assert call(monkeypatch, b, bot.api_tools_airstrike_remove, {"id": dep_id})[0] == 404
    own = call(monkeypatch, a, bot.api_tools_airstrike_get)[1]["data"]["deployments"][0]
    assert own["status"] == "ok"
    assert call(monkeypatch, a, bot.api_tools_airstrike_remove, {"id": dep_id})[0] == 200
    assert a.ftp.files["/mission/db/events.xml"].encode() == EVENTS.encode()
    assert a.ftp.files["/mission/cfgeventspawns.xml"].encode() == SPAWNS.encode()
    assert a.data["airstrikes"] == [] and b.ftp.writes == []


def test_markers_protect_other_tools_even_without_manifest(monkeypatch, servers):
    a, _b = servers
    assert deploy(monkeypatch, a, commit=True)[0] == 200
    assert "Airstrike_Test" in bot._deployment_eventnamen(a)
    root = ET.fromstring(a.ftp.files["/mission/db/events.xml"])
    assert "Airstrike_Test" not in bot._tool_events_liste(root, ausblenden=bot._deployment_eventnamen(a))
    a.data.pop("airstrikes")
    with pytest.raises(ValueError, match="Airstrike Generator"):
        bot._tool_delete_event(a.ftp.files["/mission/db/events.xml"], "Airstrike_Test")


@pytest.mark.parametrize("missing,status", [("cfgeventspawns.xml", "teilweise"), ("both", "fehlt")])
def test_status_comes_from_both_marker_segments(monkeypatch, servers, missing, status):
    a, _b = servers
    assert deploy(monkeypatch, a, commit=True)[0] == 200
    a.ftp.files["/mission/cfgeventspawns.xml"] = SPAWNS
    if missing == "both":
        a.ftp.files["/mission/db/events.xml"] = EVENTS
    result = call(monkeypatch, a, bot.api_tools_airstrike_get)
    assert result[0] == 200 and result[1]["data"]["deployments"][0]["status"] == status


def test_map_uses_tenant_data_and_canonical_boundary(monkeypatch, servers):
    a, _b = servers
    bot.cfg.config["map_name"] = "Chernarus"
    a.data["map_name"] = "enoch"
    assert deploy(monkeypatch, a, positions=[{"x": 12800, "z": 12800}])[0] == 200
    assert deploy(monkeypatch, a, positions=[{"x": 12800.1, "z": 0}])[0] == 400
    a.data.pop("map_name")
    assert deploy(monkeypatch, a)[0] == 400


def test_same_session_mutations_share_rate_limit(monkeypatch, servers):
    a, _b = servers
    status, result = call(monkeypatch, a, bot.api_tools_airstrike_deploy, payload(commit=True), session="rate-airstrike")
    assert status == 200, result
    assert call(monkeypatch, a, bot.api_tools_airstrike_remove, {"id": result["data"]["id"]}, session="rate-airstrike")[0] == 429


@pytest.mark.parametrize("value", [[], "oops", 1, {"id": "dv_bad"}])
def test_bad_remove_body_is_400(monkeypatch, servers, value):
    assert call(monkeypatch, servers[0], bot.api_tools_airstrike_remove, value)[0] == 400


def test_legacy_xml_bytes_are_unchanged():
    expected = ('<event name="VehicleTest">\n'
                '    <nominal>2</nominal>\n    <min>2</min>\n    <max>2</max>\n'
                '    <lifetime>300</lifetime>\n    <restock>0</restock>\n'
                '    <saferadius>500</saferadius>\n    <distanceradius>500</distanceradius>\n'
                '    <cleanupradius>200</cleanupradius>\n'
                '    <flags deletable="0" init_random="0" remove_damaged="1"/>\n'
                '    <position>fixed</position>\n    <limit>mixed</limit>\n    <active>1</active>\n'
                '    <children>\n'
                '        <child lootmax="0" lootmin="0" max="2" min="2" type="OffroadHatchback_Blue"/>\n'
                '    </children>\n</event>')
    assert bot._dayzcode_event_xml("VehicleTest", "OffroadHatchback_Blue", 2).encode() == expected.encode()


@pytest.mark.parametrize("raw", ['<events></events>', '<events><event name="Original"/></events>', '<events>  </events>'])
def test_compact_original_is_restored_byte_exact(raw):
    updated = bot._dayzcode_einfuegen(raw, "events", "as_inline", bot._dayzcode_event_xml("Airstrike_Inline", "ExplosionTest", 1))
    ET.fromstring(updated)
    restored, found = bot._dayzcode_entfernen(updated, "as_inline")
    assert found and restored.encode() == raw.encode()
