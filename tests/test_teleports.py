"""PRA uploads against real handlers, tenant sessions and a failure-injectable FTP."""
import asyncio
import copy
import json
import os
import sys
import time

import pytest
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

GAMEPLAY = '{"version":123,"WorldsData":{"playerRestrictedAreaFiles":["pra/warheadstorage.json"],"objectSpawnersArr":["./custom/base.json"],"lightingConfig":1},"PlayerData":{"other":[1,true,"ü"]}}\n'


class FTP:
    def __init__(self):
        self.files = {"/mission/cfggameplay.json": GAMEPLAY}
        self.mutations = []
        self.fail = None
        self.fail_delete = None

    def list_dir(self, directory):
        return [p for p in self.files if p.startswith(directory + "/")]

    def read_file_ex(self, path):
        return (self.files[path], "ok") if path in self.files else (None, "missing")

    def write_file(self, path, content):
        self.mutations.append(("write", path))
        self.files[path] = content
        if path == self.fail:
            self.fail = None  # A partial write: rollback must include the failing file.
            return False
        return True

    def delete_file(self, path):
        self.mutations.append(("delete", path))
        self.files.pop(path, None)
        if path == self.fail_delete:
            self.fail_delete = None
            return False
        return True

    def mkdir(self, path):
        self.mutations.append(("mkdir", path))
        return True


@pytest.fixture
def servers(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    entries = {sid: {"service_id": sid, "nitrado_token": "fake", "map_name": "Livonia",
                      "ftp_mission_dir": "/mission", "owner_discord_id": sid}
               for sid in ("1000", "2000")}
    (tmp_path / "connections.json").write_text(json.dumps(entries))
    registry = bot.ConnectionRegistry()
    registry.load()
    monkeypatch.setattr(bot, "connections", registry)
    monkeypatch.setattr(bot.cfg, "config", {})
    monkeypatch.setattr(bot, "_SCHADEN_LOCKS", {})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    for conn in registry._conns.values():
        conn.ftp = FTP()
    return registry._conns["1000"], registry._conns["2000"]


def call(monkeypatch, conn, payload=None, remove=False, get=False):
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": True,
                            "service_id": conn.service_id, "seen": time.time()}
    req = make_mocked_request("GET" if get else "POST", "/api/tools/teleports",
                             headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    async def body(_request):
        return payload
    monkeypatch.setattr(bot, "body", body)
    handler = bot.api_tools_teleports_get if get else (
        bot.api_tools_teleports_remove if remove else bot.api_tools_teleports_post)
    result = asyncio.run(handler(req))
    return result.status, json.loads(result.body)


def payload(name="Gate_A", commit=True):
    return {"name": name, "commit": commit,
            "boxes": [{"x": 5000, "y": 220, "z": 6000, "width": 3, "height": 4,
                       "depth": 5, "yaw": 90, "pitch": 0, "roll": 0,
                       "object": "Land_Sign_Warning", "objectYaw": 180}],
            "targets": [{"x": 7000, "y": 210, "z": 8000}]}


def deploy(monkeypatch, conn, data=None):
    status, response = call(monkeypatch, conn, data or payload())
    assert status == 200, response
    return response["data"]


def test_preview_has_no_writes(monkeypatch, servers):
    conn, _ = servers
    before = copy.deepcopy(conn.data)
    data = deploy(monkeypatch, conn, payload(commit=False))
    assert len(data["generated"]) == 3
    assert not conn.ftp.mutations
    assert conn.data == before
    assert conn.ftp.files == {"/mission/cfggameplay.json": GAMEPLAY}


def test_commit_format_registration_and_persistence(monkeypatch, servers):
    conn, _ = servers
    result = deploy(monkeypatch, conn)
    pra = json.loads(conn.ftp.files["/mission/custom/pra/Gate_A.json"])
    assert pra == {"areaName": "Gate_A", "PRABoxes": [[[3, 4, 5], [90, 0, 0], [5000, 220, 6000]]],
                   "safePositions3D": [[7000, 210, 8000]]}
    objects = json.loads(conn.ftp.files["/mission/custom/Gate_A_objects.json"])
    assert objects["Objects"][0]["ypr"] == [180, 0, 0]
    gameplay = json.loads(conn.ftp.files["/mission/cfggameplay.json"])
    assert gameplay["WorldsData"]["playerRestrictedAreaFiles"].pop() == "custom/pra/Gate_A.json"
    assert gameplay["WorldsData"]["objectSpawnersArr"].pop() == "custom/Gate_A_objects.json"
    assert gameplay == json.loads(GAMEPLAY)
    with open("connections.json", encoding="utf-8") as stream:
        saved = json.load(stream)
    assert saved["1000"]["teleports"][0]["id"] == result["id"]
    assert "teleports" not in saved["2000"]


def test_duplicate_and_normalized_registration_conflict(monkeypatch, servers):
    conn, _ = servers
    deploy(monkeypatch, conn)
    assert call(monkeypatch, conn, payload())[0] == 409
    assert call(monkeypatch, conn, payload("gate_a"))[0] == 409
    conn.ftp.files["/mission/cfggameplay.json"] = '{"WorldsData":{"playerRestrictedAreaFiles":["./custom/pra/Other.json"]}}'
    assert call(monkeypatch, conn, payload("Other"))[0] == 409


@pytest.mark.parametrize("field,value", [("name", "../Bad"), ("name", ""), ("name", "a-b"),
    ("x", 12801), ("z", -1), ("y", None), ("x", float("nan")), ("x", True), ("width", 0), ("yaw", 361)])
def test_invalid_input_is_400(monkeypatch, servers, field, value):
    conn, _ = servers
    data = payload()
    (data if field == "name" else data["boxes"][0])[field] = value
    assert call(monkeypatch, conn, data)[0] == 400
    assert not conn.ftp.mutations


@pytest.mark.parametrize("filename", ["custom/pra/Gate_A.json", "custom/Gate_A_objects.json", "cfggameplay.json"])
def test_partial_write_rolls_back_all_files(monkeypatch, servers, filename):
    conn, _ = servers
    conn.ftp.fail = "/mission/" + filename
    assert call(monkeypatch, conn, payload())[0] == 502
    assert conn.ftp.files == {"/mission/cfggameplay.json": GAMEPLAY}
    assert not conn.data.get("teleports")


def test_remove_restores_original_and_tenant_b_is_denied(monkeypatch, servers):
    a, b = servers
    item = deploy(monkeypatch, a)
    bot.cfg.config["teleports"] = a.data["teleports"]  # Regression: no conn.get fallback.
    assert call(monkeypatch, b, get=True)[1]["data"]["teleports"] == []
    assert call(monkeypatch, b, {"id": item["id"]}, remove=True)[0] == 404
    assert not b.ftp.mutations
    assert call(monkeypatch, a, {"id": item["id"]}, remove=True)[0] == 200
    assert a.ftp.files == {"/mission/cfggameplay.json": GAMEPLAY}
    assert not a.data["teleports"]


def test_failed_remove_restores_files_and_manifest(monkeypatch, servers):
    conn, _ = servers
    item = deploy(monkeypatch, conn)
    before = dict(conn.ftp.files)
    conn.ftp.fail_delete = "/mission/custom/Gate_A_objects.json"
    assert call(monkeypatch, conn, {"id": item["id"]}, remove=True)[0] == 502
    assert conn.ftp.files == before
    assert conn.data["teleports"][0]["id"] == item["id"]


@pytest.mark.parametrize("first", [0, 1])
def test_remove_out_of_order_preserves_other_changes(monkeypatch, servers, first):
    conn, _ = servers
    conn.ftp.files["/mission/cfggameplay.json"] = '{"version":123}'
    items = [deploy(monkeypatch, conn, payload(name)) for name in ("A", "B")]
    changed = json.loads(conn.ftp.files["/mission/cfggameplay.json"])
    changed["OtherTool"] = {"keep": True}
    conn.ftp.files["/mission/cfggameplay.json"] = json.dumps(changed)
    assert call(monkeypatch, conn, {"id": items[first]["id"]}, remove=True)[0] == 200
    assert call(monkeypatch, conn, {"id": items[1-first]["id"]}, remove=True)[0] == 200
    assert json.loads(conn.ftp.files["/mission/cfggameplay.json"]) == {"version":123, "OtherTool":{"keep":True}}
    assert len(conn.ftp.files) == 1


def test_no_objects_no_spawner_changes(monkeypatch, servers):
    conn, _ = servers
    data = payload()
    data["boxes"][0]["object"] = ""
    item = deploy(monkeypatch, conn, data)
    assert len(item["generated"]) == 2
    assert "/mission/custom/Gate_A_objects.json" not in conn.ftp.files


def test_bad_gameplay_does_not_write(monkeypatch, servers):
    conn, _ = servers
    conn.ftp.files["/mission/cfggameplay.json"] = '{"WorldsData": []}'
    assert call(monkeypatch, conn, payload())[0] == 502
    assert not conn.ftp.mutations


def test_manifest_disk_failure_rolls_back_ftp(monkeypatch, servers):
    _, conn = servers  # Nonprimary customer: no global config fallback can hide the failure.
    real_replace = bot.os.replace
    def fail_connections(source, target):
        if target == bot.CONNECTIONS_FILE:
            raise OSError("disk unavailable")
        return real_replace(source, target)
    monkeypatch.setattr(bot.os, "replace", fail_connections)
    status, result = call(monkeypatch, conn, payload())
    assert status == 502, result
    assert conn.ftp.files == {"/mission/cfggameplay.json": GAMEPLAY}
    assert not conn.data.get("teleports")
    with open("connections.json", encoding="utf-8") as stream:
        assert "teleports" not in json.load(stream)["2000"]


def test_concurrent_custom_import_preserves_both_registrations(monkeypatch, servers):
    conn, _ = servers
    requests = []
    for suffix in ("teleport", "custom"):
        sid = suffix + str(time.time_ns())
        bot._SESS_STORE[sid] = {"token":"fake", "discord":{"id":sid}, "is_admin":True,
                                "service_id":conn.service_id, "seen":time.time()}
        requests.append(make_mocked_request("POST", "/api/tools/" + suffix,
                        headers={"Cookie":f"{bot._SESS_COOKIE}={sid}"}))
    async def request_body(request):
        return payload() if request is requests[0] else {"filename":"other.json", "content":'{"Objects": []}'}
    monkeypatch.setattr(bot, "body", request_body)
    original_read = bot._tools_datei_lesen
    async def delayed_read(connection, filename, loop):
        result = await original_read(connection, filename, loop)
        if filename == "cfggameplay.json":
            await asyncio.sleep(0.01)
        return result
    monkeypatch.setattr(bot, "_tools_datei_lesen", delayed_read)
    async def run():
        return await asyncio.gather(bot.api_tools_teleports_post(requests[0]),
                                    bot.api_tools_custombuildmap_import(requests[1]))
    results = asyncio.run(run())
    assert [r.status for r in results] == [200, 200]
    worlds = json.loads(conn.ftp.files["/mission/cfggameplay.json"])["WorldsData"]
    assert "custom/pra/Gate_A.json" in worlds["playerRestrictedAreaFiles"]
    assert set(worlds["objectSpawnersArr"]) == {"./custom/base.json", "custom/Gate_A_objects.json", "custom/other.json"}
