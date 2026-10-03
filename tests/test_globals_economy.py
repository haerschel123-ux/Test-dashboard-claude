"""Globals/economy XML transactions with real flat tenant registry and stub FTP."""
import copy
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import pytest
from aiohttp.test_utils import make_mocked_request

from test_airstrike import bot, call, servers  # noqa: F401

GLOBAL_VALUES = {
    "AnimalMaxCount": 200, "CleanupAvoidance": 100,
    "CleanupLifetimeDeadAnimal": 1200, "CleanupLifetimeDeadInfected": 330,
    "CleanupLifetimeDeadPlayer": 3600, "CleanupLifetimeDefault": 45,
    "CleanupLifetimeLimit": 50, "CleanupLifetimeRuined": 330,
    "FlagRefreshFrequency": 432000, "FlagRefreshMaxDuration": 3456000,
    "FoodDecay": 1, "IdleModeCountdown": 60, "IdleModeStartup": 1,
    "InitialSpawn": 100, "LootDamageMax": .82, "LootDamageMin": 0.,
    "LootProxyPlacement": 1, "LootSpawnAvoidance": 100, "RespawnAttempt": 2,
    "RespawnLimit": 20, "RespawnTypes": 12, "RestartSpawn": 0,
    "SpawnInitial": 1200, "TimeHopping": 60, "TimeLogin": 15,
    "TimeLogout": 15, "TimePenalty": 20, "WorldWetTempUpdate": 1,
    "ZombieMaxCount": 1000, "ZoneSpawnDist": 300,
}
ECONOMY_VALUES = {
    "dynamic": dict(init=1, load=1, respawn=1, save=1),
    "animals": dict(init=1, load=0, respawn=1, save=0),
    "zombies": dict(init=1, load=0, respawn=1, save=0),
    "vehicles": dict(init=1, load=1, respawn=1, save=1),
    "randoms": dict(init=0, load=0, respawn=1, save=0),
    "custom": dict(init=0, load=0, respawn=0, save=0),
    "building": dict(init=1, load=1, respawn=0, save=1),
    "player": dict(init=1, load=1, respawn=1, save=1),
}
TOOLS = ("globals", "economy")


def vanilla(tool):
    return getattr(bot, f"_{tool}_vanilla")()


def xml(tool, values=None, original=None):
    return getattr(bot, f"_{tool}_xml")(values or vanilla(tool), original_raw=original, created_at="fixed")


def post(monkeypatch, conn, tool, values=None, preview=False, session=None, **extra):
    body = {"werte": vanilla(tool) if values is None else values, "vorschau": preview, **extra}
    return call(monkeypatch, conn, getattr(bot, f"api_tools_{tool}_post"), body, session)


def get(monkeypatch, conn, tool):
    return call(monkeypatch, conn, getattr(bot, f"api_tools_{tool}_get"))


def user_call(monkeypatch, tool, service, user, *, values=None, guest=False):
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": user}, "is_admin": False,
                            "is_guest": guest, "service_id": service, "seen": time.time()}
    request = make_mocked_request("GET" if values is None else "POST", f"/api/tools/{tool}",
                                 headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    async def body(request):
        return {"werte": values, "vorschau": True}
    monkeypatch.setattr(bot, "body", body)
    handler = getattr(bot, f"api_tools_{tool}_{'get' if values is None else 'post'}")
    response = asyncio.run(handler(request))
    return response.status, json.loads(response.body)


@pytest.mark.parametrize("tool", TOOLS)
def test_missing_corrupt_and_read_error_fall_back_with_notice(monkeypatch, servers, tool):
    a, _ = servers
    path = f"/mission/db/{tool}.xml"
    for raw in (None, "<broken>", "<wrongroot/>"):
        if raw is None:
            a.ftp.files.pop(path, None)
        else:
            a.ftp.files[path] = raw
        status, response = get(monkeypatch, a, tool)
        data = response["data"]
        assert status == 200 and data["quelle"] == "vanilla"
        assert data["werte"] == vanilla(tool) and data["warnung"]
        assert data["vanilla"] == vanilla(tool) and data["presets"] and data["felder"]
    monkeypatch.setattr(a.ftp, "read_file_ex", lambda path: (None, "error"))
    assert get(monkeypatch, a, tool)[1]["data"]["warnung"]
    assert a.ftp.writes == []


def test_vanilla_exact_schema_order_and_branding():
    assert bot._globals_vanilla() == GLOBAL_VALUES
    assert bot._economy_vanilla() == ECONOMY_VALUES
    for tool, expected in (("globals", GLOBAL_VALUES), ("economy", ECONOMY_VALUES)):
        content = xml(tool)
        assert content.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
        assert "Brigarde Killfeed" in content
        assert not any(mark in content for mark in ("DAYZCODE", "dzbtools", "DoorDieHub"))
        root = ET.fromstring(content)
        assert root.tag == ("variables" if tool == "globals" else "economy")
        assert [child.get("name") if tool == "globals" else child.tag for child in root] == list(expected)
        if tool == "globals":
            for child in root:
                assert child.get("type") == ("1" if child.get("name") in ("LootDamageMin", "LootDamageMax") else "0")
        assert getattr(bot, f"_{tool}_parse")(content) == expected


@pytest.mark.parametrize("tool", TOOLS)
def test_server_roundtrip_preserves_mods_comments_and_attributes(monkeypatch, servers, tool):
    a, _ = servers
    path = f"/mission/db/{tool}.xml"
    raw = ('<variables version="mod"><!-- preserve ü --><var name="ModFoo" type="2" value="alpha &amp; beta" custom="yes"><extra/></var>'
           '<var name="AnimalMaxCount" type="0" value="123" custom="keep"/></variables>') if tool == "globals" else (
           '<economy version="mod"><!-- preserve ü --><modded init="7" mode="keep"><extra/></modded>'
           '<dynamic init="1" load="1" respawn="1" save="1" custom="keep"/></economy>')
    a.ftp.files[path] = raw
    status, response = get(monkeypatch, a, tool)
    assert status == 200 and response["data"]["quelle"] == "server"
    values = response["data"]["werte"]
    if tool == "globals":
        assert values["AnimalMaxCount"] == 123
        values["AnimalMaxCount"] = 456
    else:
        values["dynamic"]["respawn"] = 0
    # Client-supplied XML must never be used as a source of extras.
    status, response = post(monkeypatch, a, tool, values, True, original="<evil/>")
    assert status == 200 and a.ftp.writes == []
    content = response["data"]["generated"][0]["content"]
    assert "preserve ü" in content and "evil" not in content
    root = ET.fromstring(content)
    assert root.get("version") == "mod"
    node = root.find("var[@name='ModFoo']") if tool == "globals" else root.find("modded")
    assert node is not None and node.find("extra") is not None
    if tool == "globals":
        assert node.attrib == {"name": "ModFoo", "type": "2", "value": "alpha & beta", "custom": "yes"}
        assert root.find("var[@name='AnimalMaxCount']").get("custom") == "keep"
    else:
        assert node.attrib == {"init": "7", "mode": "keep"}
        assert root.find("dynamic").get("custom") == "keep"
    assert post(monkeypatch, a, tool, values)[0] == 200
    assert a.ftp.files[path + ".bak"] == raw
    assert "preserve ü" in a.ftp.files[path]


@pytest.mark.parametrize("field,value", [
    ("AnimalMaxCount", "1"), ("AnimalMaxCount", 1.1), ("AnimalMaxCount", True),
    ("AnimalMaxCount", -1), ("ZombieMaxCount", 5001), ("LootDamageMax", 1.1),
    ("LootDamageMin", -.1), ("LootDamageMin", True), ("LootDamageMin", "0.1"),
    ("LootDamageMin", float("nan")), ("LootDamageMax", float("inf")),
    ("ZombieMaxCount", 10 ** 1000), ("FoodDecay", 2), ("RestartSpawn", -1),
])
def test_invalid_globals_400_without_write(monkeypatch, servers, field, value):
    a, _ = servers
    values = vanilla("globals")
    values[field] = value
    assert post(monkeypatch, a, "globals", values)[0] == 400
    assert a.ftp.writes == []


def test_inverted_damage_and_unknown_fields_rejected(monkeypatch, servers):
    a, _ = servers
    values = vanilla("globals")
    values.update(LootDamageMin=.9, LootDamageMax=.1)
    assert post(monkeypatch, a, "globals", values)[0] == 400
    values = vanilla("globals")
    values["Injected"] = 1
    assert post(monkeypatch, a, "globals", values)[0] == 400
    assert a.ftp.writes == []


@pytest.mark.parametrize("value", [True, -1, 2, "1", .5, None, float("nan")])
def test_invalid_economy_400_without_write(monkeypatch, servers, value):
    a, _ = servers
    values = vanilla("economy")
    values["dynamic"]["load"] = value
    assert post(monkeypatch, a, "economy", values)[0] == 400
    assert a.ftp.writes == []


@pytest.mark.parametrize("tool", TOOLS)
def test_preview_no_writes_then_backup_before_single_target(monkeypatch, servers, tool):
    a, b = servers
    path = f"/mission/db/{tool}.xml"
    original = xml(tool)
    a.ftp.files[path] = original
    before_b = copy.deepcopy(b.ftp.files)
    values = vanilla(tool)
    events = []
    write = a.ftp.write_file
    def ordered_write(target, content):
        if target == path:
            assert a.ftp.files[path + ".bak"] == original
        events.append(target)
        return write(target, content)
    monkeypatch.setattr(a.ftp, "write_file", ordered_write)
    status, response = post(monkeypatch, a, tool, values, True)
    assert status == 200 and events == []
    assert response["data"]["generated"][0]["filename"] == f"db/{tool}.xml"
    assert post(monkeypatch, a, tool, values)[0] == 200
    assert events == [path + ".bak", path]
    assert b.ftp.files == before_b and b.ftp.writes == []


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("raises", [False, True])
def test_backup_failure_closed_without_target_write(monkeypatch, servers, tool, raises):
    a, _ = servers
    path = f"/mission/db/{tool}.xml"
    raw = xml(tool)
    a.ftp.files[path] = raw
    if raises:
        a.ftp.raise_at = 1
    else:
        a.ftp.fail_at = 1
    status, _ = post(monkeypatch, a, tool)
    assert status == 502 and a.ftp.writes == [path + ".bak"]
    assert a.ftp.files[path] == raw


@pytest.mark.parametrize("tool", TOOLS)
def test_tenant_reads_writes_rates_and_registry_no_leak(monkeypatch, servers, tmp_path, tool):
    a, b = servers
    path = f"/mission/db/{tool}.xml"
    values = vanilla(tool)
    if tool == "globals":
        values["AnimalMaxCount"] = 321
    else:
        values["dynamic"]["save"] = 0
    a.ftp.files[path] = xml(tool, values)
    raw_a = a.ftp.files[path]
    registry = (tmp_path / "connections.json").read_bytes()
    config = copy.deepcopy(bot.cfg.config)
    monkeypatch.setattr(bot.connections, "primary", lambda: pytest.fail("primary tenant fallback"))
    assert get(monkeypatch, b, tool)[1]["data"]["werte"] == vanilla(tool)
    assert post(monkeypatch, b, tool, session=f"rate-{tool}", service_id="1000",
                ftp_mission_dir="/victim", filename=f"/mission/db/{tool}.xml")[0] == 200
    assert post(monkeypatch, b, tool, session=f"rate-{tool}")[0] == 429
    assert b.ftp.writes == [path] and a.ftp.writes == [] and a.ftp.files[path] == raw_a
    assert bot.cfg.config == config and (tmp_path / "connections.json").read_bytes() == registry


@pytest.mark.parametrize("tool", TOOLS)
def test_missing_mission_preview_only_and_invalid_body(monkeypatch, servers, tool):
    a, _ = servers
    a.data["ftp_mission_dir"] = ""
    assert get(monkeypatch, a, tool)[1]["data"]["kein_mission_ordner"]
    assert post(monkeypatch, a, tool, preview=True)[0] == 200
    assert post(monkeypatch, a, tool)[0] == 409
    for value in ([], "bad", {"vorschau": 1, "werte": vanilla(tool)}):
        assert call(monkeypatch, a, getattr(bot, f"api_tools_{tool}_post"), value)[0] == 400
    assert a.ftp.writes == []


def test_globals_presets_exact_documented_changes_and_vanilla():
    changes = {
        "performance": {"AnimalMaxCount": 100, "ZombieMaxCount": 500,
                        "CleanupLifetimeDeadAnimal": 600, "CleanupLifetimeDeadInfected": 180,
                        "CleanupLifetimeDeadPlayer": 1800, "CleanupLifetimeDefault": 30,
                        "CleanupLifetimeRuined": 180},
        "base": {"FlagRefreshFrequency": 864000, "FlagRefreshMaxDuration": 5184000},
        "hardcore": {"CleanupLifetimeDeadAnimal": 2400, "CleanupLifetimeDeadInfected": 660,
                     "CleanupLifetimeDeadPlayer": 7200, "TimeLogin": 5, "ZombieMaxCount": 2000},
        "vanilla": {},
    }
    presets = bot._globals_presets()
    assert [p["key"] for p in presets] == list(changes)
    for preset in presets:
        assert preset["werte"] == {**GLOBAL_VALUES, **changes[preset["key"]]}
        assert bot._globals_validate(preset["werte"]) == preset["werte"]
    presets[0]["werte"]["FoodDecay"] = 0
    assert bot._globals_vanilla() == GLOBAL_VALUES


def test_economy_presets_composable_rows_and_vanilla():
    expected = {"soft": ["dynamic"], "loot": ["dynamic", "randoms"],
                "building": ["building"], "vehicle": ["vehicles"], "player": ["player"],
                "full": list(ECONOMY_VALUES), "vanilla": []}
    assert {p["key"]: p["zeilen"] for p in bot._economy_presets()} == expected
    values = vanilla("economy")
    values["animals"]["save"] = 1
    for key in ("vehicle", "building"):
        for row in expected[key]:
            values[row]["load"] = 0
    assert values["animals"]["save"] == 1
    assert values["vehicles"] == {**ECONOMY_VALUES["vehicles"], "load": 0}
    assert values["building"] == {**ECONOMY_VALUES["building"], "load": 0}
    assert bot._economy_validate(values) == values
    assert vanilla("economy") == ECONOMY_VALUES


@pytest.mark.parametrize("tool", TOOLS)
def test_unreadable_original_never_overwritten(monkeypatch, servers, tool):
    a, _ = servers
    path = f"/mission/db/{tool}.xml"
    a.ftp.files[path] = "<broken>"
    assert post(monkeypatch, a, tool)[0] == 409
    assert a.ftp.files[path] == "<broken>" and a.ftp.writes == []
    monkeypatch.setattr(a.ftp, "read_file_ex", lambda path: (None, "error"))
    assert post(monkeypatch, a, tool)[0] == 502
    assert a.ftp.writes == []


@pytest.mark.parametrize("tool", TOOLS)
def test_failed_target_keeps_original_backup(monkeypatch, servers, tool):
    a, _ = servers
    path = f"/mission/db/{tool}.xml"
    raw = xml(tool)
    a.ftp.files[path] = raw
    a.ftp.fail_at = 2
    assert post(monkeypatch, a, tool)[0] == 502
    assert a.ftp.writes == [path + ".bak", path]
    assert a.ftp.files[path + ".bak"] == raw


@pytest.mark.parametrize("tool,raw", [
    ("globals", '<variables><var name="AnimalMaxCount" type="1" value="3"/></variables>'),
    ("globals", '<variables><var name="AnimalMaxCount" type="0" value="3"/><var name="AnimalMaxCount" type="0" value="4"/></variables>'),
    ("economy", '<economy><dynamic load="1"/><dynamic load="0"/></economy>'),
    ("economy", '<economy><dynamic load="2"/></economy>'),
])
def test_bad_known_xml_falls_back_and_cannot_overwrite(monkeypatch, servers, tool, raw):
    a, _ = servers
    a.ftp.files[f"/mission/db/{tool}.xml"] = raw
    response = get(monkeypatch, a, tool)
    assert response[0] == 200 and response[1]["data"]["quelle"] == "vanilla"
    assert post(monkeypatch, a, tool)[0] == 409 and a.ftp.writes == []


@pytest.mark.parametrize("tool", TOOLS)
def test_forged_other_owner_session_and_permission_gates(monkeypatch, servers, tool):
    a, b = servers
    reads = []
    read = a.ftp.read_file_ex
    def tracked(path):
        reads.append(path)
        return read(path)
    monkeypatch.setattr(a.ftp, "read_file_ex", tracked)
    monkeypatch.setattr(bot, "_module_tier", lambda key: "public")
    for values in (None, vanilla(tool)):
        assert user_call(monkeypatch, tool, "1000", "2000", values=values)[0] == 403
    assert reads == [] and a.ftp.writes == [] and b.ftp.writes == []
    # A legitimate guest can view, but cannot generate/upload edited XML.
    a.data["dashboard_perms"] = {"user:guest": {"tools": ["view"]}}
    assert user_call(monkeypatch, tool, "1000", "guest", guest=True)[0] == 200
    assert user_call(monkeypatch, tool, "1000", "guest", values=vanilla(tool), guest=True)[0] == 403
    a.data["dashboard_perms"] = {}
    assert user_call(monkeypatch, tool, "1000", "guest", guest=True)[0] == 403
    # Owners also obey the module manager under-review gate.
    monkeypatch.setattr(bot, "_module_tier", lambda key: "under_review")
    assert user_call(monkeypatch, tool, "1000", "1000")[0] == 403
    assert user_call(monkeypatch, tool, "1000", "1000", values=vanilla(tool))[0] == 403
    assert a.ftp.writes == [] and b.ftp.writes == []


def test_restart_spawn_existing_percent_roundtrip(monkeypatch, servers):
    a, _ = servers
    values = vanilla("globals")
    values["RestartSpawn"] = 75
    a.ftp.files["/mission/db/globals.xml"] = xml("globals", values)
    status, response = get(monkeypatch, a, "globals")
    assert status == 200 and response["data"]["werte"]["RestartSpawn"] == 75
    assert post(monkeypatch, a, "globals", response["data"]["werte"])[0] == 200
    assert bot._globals_parse(a.ftp.files["/mission/db/globals.xml"])["RestartSpawn"] == 75


def test_endpoints_in_actual_isolated_process(tmp_path):
    """Fresh interpreter, copied modules and real cfg.load_all/flat connections."""
    root = Path(bot.__file__).resolve().parent
    for name in ("bot.py", "log_parser.py", "embedded_assets.py"):
        shutil.copy2(root / name, tmp_path / name)
    (tmp_path / "config.json").write_text(json.dumps({"service_id": "1000", "map_name": "Chernarus"}))
    flat = {sid: {"service_id": sid, "nitrado_token": "fake", "owner_discord_id": sid,
                  "ftp_mission_dir": f"/tenant-{sid}", "map_name": "Livonia"}
            for sid in ("1000", "2000")}
    (tmp_path / "connections.json").write_text(json.dumps(flat))
    script = r'''
import asyncio, copy, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import bot
from aiohttp.test_utils import make_mocked_request
bot.cfg.load_all()
bot.connections.load()
assert set(bot.connections._conns) == {"1000", "2000"}
class FTP:
    def __init__(self): self.files = {}; self.reads = []; self.writes = []
    def read_file_ex(self, path):
        self.reads.append(path)
        return (self.files[path], "ok") if path in self.files else (None, "missing")
    def write_file(self, path, content):
        self.writes.append(path); self.files[path] = content; return True
for conn in bot.connections._conns.values(): conn.ftp = FTP()
a, b = (bot.connections._conns[s] for s in ("1000", "2000"))
def forbidden(): raise AssertionError("primary fallback")
bot.connections.primary = forbidden
config = copy.deepcopy(bot.cfg.config)
registry = Path("connections.json").read_bytes()
async def run():
    for tool in ("globals", "economy"):
        values = getattr(bot, "_" + tool + "_vanilla")()
        path_a = "/tenant-1000/db/" + tool + ".xml"
        a.ftp.files[path_a] = getattr(bot, "_" + tool + "_xml")(values)
        raw_a = a.ftp.files[path_a]
        for preview in (True, False):
            sid = tool + str(preview)
            bot._SESS_STORE[sid] = {"token":"fake", "discord":{"id":sid}, "is_admin":True,
                                     "service_id":"2000", "seen":time.time()}
            req = make_mocked_request("POST", "/api/tools/" + tool,
                  headers={"Cookie":bot._SESS_COOKIE + "=" + sid})
            async def body(request): return {"werte":values, "vorschau":preview}
            bot.body = body
            response = await getattr(bot, "api_tools_" + tool + "_post")(req)
            assert response.status == 200, response.body
            if preview: assert b.ftp.writes == ([] if tool == "globals" else ["/tenant-2000/db/globals.xml"])
        assert a.ftp.files[path_a] == raw_a and not a.ftp.writes
    assert b.ftp.writes == ["/tenant-2000/db/globals.xml", "/tenant-2000/db/economy.xml"]
    assert all(p.startswith("/tenant-2000/") for p in b.ftp.reads + b.ftp.writes)
    assert bot.cfg.config == config and Path("connections.json").read_bytes() == registry
asyncio.run(run())
print("GLOBALS_ECONOMY_ISOLATED_TENANTS_OK")
'''
    (tmp_path / "isolated_check.py").write_text(script)
    result = subprocess.run([sys.executable, "isolated_check.py"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GLOBALS_ECONOMY_ISOLATED_TENANTS_OK" in result.stdout
