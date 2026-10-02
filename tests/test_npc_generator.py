"""NPC exchange files, durable tenant storage and the three-file deployment."""
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

EVENTS = '<events>\n    <!-- original ü -->\n</events>\n'
SPAWNS = '<eventposdef>\n    <!-- original -->\n</eventposdef>\n'
TYPES = '<spawnabletypes>\n    <!-- original -->\n</spawnabletypes>\n'


class FTP:
    def __init__(self):
        self.files = {"/mission/db/events.xml": EVENTS, "/mission/cfgeventspawns.xml": SPAWNS,
                      "/mission/cfgspawnabletypes.xml": TYPES}
        self.writes = []

    def list_dir(self, directory):
        return [path for path in self.files if path.startswith(directory + "/")]

    def read_file_ex(self, path):
        return (self.files[path], "ok") if path in self.files else (None, "missing")

    def write_file(self, path, content):
        self.writes.append(path)
        self.files[path] = content
        return True


@pytest.fixture
def servers(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    entries = {sid: {"service_id": sid, "nitrado_token": "fake", "owner_discord_id": sid,
                     "ftp_mission_dir": "/mission", "map_name": "Livonia"}
               for sid in ("1000", "2000")}
    (tmp_path / "connections.json").write_text(json.dumps(entries))
    registry = bot.ConnectionRegistry()
    registry.load()
    monkeypatch.setattr(bot, "connections", registry)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    for conn in registry._conns.values():
        conn.ftp = FTP()
    return registry._conns["1000"], registry._conns["2000"]


def npc(**changes):
    data = {"format": "dayz-npc-1", "name": "Haendler", "label": "Händler am Markt",
            "koerper": "SurvivorM_Boris", "items": [["attachments", "ConstructionHelmet_Red"],
              ["cargo", "BandageDressing"], ["cargo", "BandageDressing"]],
            "zufall": [["attachments", ["Mp133Shotgun", "M4A1"]]]}
    data.update(changes)
    return data


def call(monkeypatch, conn, handler, data=None, *, session=None):
    sid = session or str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": sid}, "is_admin": True,
                            "service_id": conn.service_id, "seen": time.time()}
    request = make_mocked_request("GET" if data is None else "POST", "/api/tools/deployment",
                                 headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    async def body(_request):
        return data
    monkeypatch.setattr(bot, "body", body)
    response = asyncio.run(handler(request))
    return response.status, json.loads(response.body)


def test_import_persists_and_get_returns_outfit(monkeypatch, servers, tmp_path):
    a, _b = servers
    status, result = call(monkeypatch, a, bot.api_tools_deployment_npc_import, npc())
    assert status == 200, result
    assert a.data["eigene_npcs"]["Haendler"] == npc()
    raw = json.loads((tmp_path / "connections.json").read_text())
    assert raw["1000"]["eigene_npcs"]["Haendler"] == npc()
    registry = bot.ConnectionRegistry()
    registry.load()
    assert bot._eigene_npcs(registry._conns["1000"])["Haendler"] == npc()
    status, result = call(monkeypatch, a, bot.api_tools_deployment_get)
    assert status == 200
    own = result["data"]["eigene_npcs"][0]
    assert own["label"] == "Händler am Markt"
    assert own["slots"] == npc()["items"] and own["zufall"] == npc()["zufall"]
    assert own["event"] == "NpcHaendler"
    assert not a.ftp.writes


@pytest.mark.parametrize("changes", [
    {"format": "other"}, {"format": None}, {"name": ""}, {"name": "A" * 33},
    {"name": "<evil>"}, {"name": 123}, {"label": ""}, {"label": "ü" * 65}, {"label": []},
    {"koerper": "SurvivorM_Invalid"}, {"items": None}, {"items": {}},
    {"items": [["attachments", "MissingItem"]]}, {"items": [["head", "M4A1"]]},
    {"items": [["cargo", {"classname": "M4A1"}]]}, {"items": [["cargo"]]},
    {"items": [["cargo", "M4A1", "extra"]]}, {"zufall": None},
    {"items": [["cargo", "BandageDressing"]] * 41},
    {"zufall": [["attachments", ["M4A1"]]] * 9},
    {"zufall": [["attachments", ["M4A1"] * 11]]},
    {"zufall": [["attachments", []]]}, {"zufall": [["attachments", "M4A1"]]},
    {"zufall": [["invalid", ["M4A1"]]]}, {"zufall": [["cargo", ["MissingItem"]]]},
    {"items": [["cargo", "BandageDressing"]] * 39},
])
def test_invalid_import_is_400_and_saves_nothing(monkeypatch, servers, tmp_path, changes):
    a, _b = servers
    before = (tmp_path / "connections.json").read_bytes()
    status, result = call(monkeypatch, a, bot.api_tools_deployment_npc_import, npc(**changes))
    assert status == 400, result
    assert "eigene_npcs" not in a.data
    assert (tmp_path / "connections.json").read_bytes() == before


def test_duplicate_requires_explicit_boolean_overwrite(monkeypatch, servers):
    a, _b = servers
    assert call(monkeypatch, a, bot.api_tools_deployment_npc_import, npc())[0] == 200
    for overwrite in (None, False, "true", 1):
        status, _ = call(monkeypatch, a, bot.api_tools_deployment_npc_import,
                         npc(label="Neu", overwrite=overwrite))
        assert status == 409
    assert call(monkeypatch, a, bot.api_tools_deployment_npc_import,
                npc(label="Neu", overwrite=True))[0] == 200
    assert a.data["eigene_npcs"]["Haendler"]["label"] == "Neu"
    assert "overwrite" not in a.data["eigene_npcs"]["Haendler"]


def test_customer_b_cannot_see_deploy_or_delete_customer_a(monkeypatch, servers):
    a, b = servers
    assert call(monkeypatch, a, bot.api_tools_deployment_npc_import, npc())[0] == 200
    # Even contaminated legacy configuration must not become B's fallback.
    assert "eigene_npcs" not in bot.cfg.config
    bot.cfg.config["eigene_npcs"] = copy.deepcopy(a.data["eigene_npcs"])
    assert "eigene_npcs" not in b.data
    assert bot._eigene_npcs(b) == {}
    assert call(monkeypatch, b, bot.api_tools_deployment_get)[1]["data"]["eigene_npcs"] == []
    status, _ = call(monkeypatch, b, bot.api_tools_deployment_deploy,
                     {"art": "npc", "preset": "Haendler", "koerper": "SurvivorM_Boris",
                      "positions": [{"x": 100, "z": 200}], "commit": True})
    assert status == 422 and not b.ftp.writes
    assert call(monkeypatch, b, bot.api_tools_deployment_npc_delete, {"name": "Haendler"})[0] == 404
    assert call(monkeypatch, b, bot.api_tools_deployment_npc_import, npc(label="Kunde B"))[0] == 200
    assert bot._eigene_npcs(a)["Haendler"]["label"] == "Händler am Markt"


def test_custom_deploy_three_segments_random_group_and_remove(monkeypatch, servers):
    a, _b = servers
    original = copy.deepcopy(a.ftp.files)
    assert call(monkeypatch, a, bot.api_tools_deployment_npc_import, npc())[0] == 200
    payload = {"art": "npc", "preset": "Haendler", "koerper": "SurvivorM_Boris",
               "suffix": "Markt", "positions": [{"x": 100, "z": 200, "a": 45}], "commit": False}
    status, result = call(monkeypatch, a, bot.api_tools_deployment_deploy, payload)
    assert status == 200, result
    assert len(result["data"]["generated"]) == 3 and a.ftp.files == original
    status, result = call(monkeypatch, a, bot.api_tools_deployment_deploy, dict(payload, commit=True))
    assert status == 200, result
    assert result["data"]["event_name"] == "NpcHaendler_Markt"
    dep_id = result["data"]["id"]
    for content in a.ftp.files.values():
        assert f"DAYZCODE:START {dep_id}" in content
        ET.fromstring(content)
    root = ET.fromstring(a.ftp.files["/mission/cfgspawnabletypes.xml"])
    random = next(group for group in root.findall("./type/attachments")
                  if group.find("item[@name='M4A1']") is not None)
    assert random.attrib["chance"] == "1.00"
    assert [item.attrib for item in random] == [{"name": "Mp133Shotgun", "chance": "0.50"},
                                                {"name": "M4A1", "chance": "0.50"}]
    assert len(root.findall("./type/cargo/item[@name='BandageDressing']")) == 2
    assert call(monkeypatch, a, bot.api_tools_deployment_npc_delete, {"name": "Haendler"})[0] == 409
    assert call(monkeypatch, a, bot.api_tools_deployment_remove, {"id": dep_id})[0] == 200
    assert a.ftp.files == original
    assert call(monkeypatch, a, bot.api_tools_deployment_npc_delete, {"name": "Haendler"})[0] == 200
    assert bot._eigene_npcs(a) == {}


@pytest.mark.parametrize("count,chance", [(2, "0.50"), (3, "0.33"), (6, "0.17"), (8, "0.12"), (10, "0.10")])
def test_xml_alternatives_follow_requested_two_decimal_format(count, chance):
    root = ET.fromstring(bot._dayzcode_type_xml("SurvivorM_Boris", [], [("cargo", ["BandageDressing"] * count)]))
    assert len(root.findall("cargo")) == 1
    assert [item.attrib["chance"] for item in root.findall("cargo/item")] == [chance] * count


def test_generator_and_import_without_ftp_or_mission(monkeypatch, servers):
    a, _b = servers
    a.ftp = None
    a.data.pop("ftp_mission_dir")
    assert call(monkeypatch, a, bot.api_tools_npcgenerator_get)[0] == 200
    status, result = call(monkeypatch, a, bot.api_tools_npcgenerator_preview, npc())
    assert status == 200 and result["data"]["xml"] == bot._dayzcode_type_xml(
        "SurvivorM_Boris", npc()["items"], npc()["zufall"])
    assert call(monkeypatch, a, bot.api_tools_deployment_npc_import, npc())[0] == 200
    assert call(monkeypatch, a, bot.api_tools_deployment_get)[1]["data"]["eigene_npcs"][0]["name"] == "Haendler"


def test_storage_failure_returns_500_and_restores_memory(monkeypatch, servers):
    a, _b = servers
    def fail_save(*, strict=False):
        assert strict
        raise OSError("disk full")
    monkeypatch.setattr(bot.connections, "save", fail_save)
    assert call(monkeypatch, a, bot.api_tools_deployment_npc_import, npc())[0] == 500
    assert "eigene_npcs" not in a.data


def test_builtin_name_is_reserved(monkeypatch, servers):
    a, _b = servers
    assert call(monkeypatch, a, bot.api_tools_deployment_npc_import,
                npc(name="bauarbeiter", overwrite=True))[0] == 409
    assert "eigene_npcs" not in a.data


def test_generator_inherits_deployment_tier(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {"module_tiers": {"tools.deployment": "beta", "tools": "public",
                                                            "tools.npcgenerator": "public"}})
    assert bot._module_tier("tools.npcgenerator") == "beta"


def test_new_npc_storage_never_depends_on_legacy_config_write(monkeypatch, servers):
    a, _b = servers
    def fail_config():
        raise OSError("legacy config is read-only")
    monkeypatch.setattr(bot.cfg, "save_config", fail_config)
    assert call(monkeypatch, a, bot.api_tools_deployment_npc_import, npc())[0] == 200
    assert "eigene_npcs" not in bot.cfg.config


def test_import_rate_limit_is_enforced(monkeypatch, servers):
    a, _b = servers
    session = str(time.time_ns())
    assert call(monkeypatch, a, bot.api_tools_deployment_npc_import, npc(), session=session)[0] == 200
    assert call(monkeypatch, a, bot.api_tools_deployment_npc_import,
                npc(name="Second"), session=session)[0] == 429
    assert "Second" not in bot._eigene_npcs(a)


def test_import_requires_authenticated_session():
    request = make_mocked_request("POST", "/api/tools/deployment/npc-import")
    response = asyncio.run(bot.api_tools_deployment_npc_import(request))
    assert response.status == 401
