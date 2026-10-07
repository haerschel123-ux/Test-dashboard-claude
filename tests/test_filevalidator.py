"""Datei-Validator: lineare Regeln, Fixes und Vanilla-Regressionen."""
import pathlib
import time

import bot


ROOT = pathlib.Path(__file__).parent / "fixtures"


def _errors(result):
    return [issue for issue in result["issues"] if issue["severity"] == "error"]


def _fixture_files(map_name):
    names = {
        "db/types.xml": ROOT / "types" / ("types-" + map_name + ".xml"),
        "db/events.xml": ROOT / "ce" / map_name / "events.xml",
        "cfgeconomycore.xml": ROOT / "ce" / map_name / "cfgeconomycore.xml",
        "cfglimitsdefinition.xml": ROOT / "ce" / map_name / "cfglimitsdefinition.xml",
        "db/globals.xml": ROOT / "ce" / map_name / "globals.xml",
        "cfgeventspawns.xml": ROOT / "ce" / map_name / "cfgeventspawns.xml",
        "cfggameplay.json": ROOT / "ce" / map_name / "cfggameplay.json",
    }
    return {name: path.read_text(encoding="utf-8") for name, path in names.items()}


def test_vanilla_files_are_clean_and_fast():
    maps = (("chernarusplus", "ChernarusPlus"), ("enoch", "Livonia"), ("sakhal", "Sakhal"))
    for fixture, world in maps:
        started = time.perf_counter()
        results = bot._fv_validate_all(_fixture_files(fixture), world)
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, f"{fixture}: {elapsed:.3f}s"
        assert not [issue for result in results for issue in _errors(result)]
        assert not [issue for result in results for issue in result["issues"] if issue["severity"] == "warning"]


def test_xml_syntax_and_textual_fixes_keep_other_lines():
    broken = """\ufeff<types>
<type name=\"Bad\"><nominal>2</nominal><min>7</min><lifetime>-4</lifetime><quantmin>9</quantmin><quantmax>2</quantmax></type>
</types></types>\n"""
    result = bot._fv_validate_one("db/types.xml", broken, "ChernarusPlus")
    assert any(issue["fix"] == "root_tail" for issue in result["issues"])
    fixed = bot._fv_fix_text("db/types.xml", broken)
    assert fixed.startswith("<types>")
    assert "<min>2</min>" in fixed and "<lifetime>0</lifetime>" in fixed and "<quantmin>-1</quantmin>" in fixed
    assert bot._fv_xml(fixed)[0] is not None


def test_json_line_and_livonia_border_and_cross_references():
    json_result = bot._fv_validate_one("cfggameplay.json", "{\n  bad\n}", "Livonia")
    assert _errors(json_result)[0]["line"] == 2
    files = {
        "db/types.xml": "<types><type name=\"Known\"><nominal>1</nominal><min>1</min></type></types>",
        "db/events.xml": "<events><event name=\"A\"><children><child type=\"Missing\"/></children></event></events>",
        "cfgeventspawns.xml": "<eventposdef><event name=\"A\"><pos x=\"15000\" z=\"1\"/></event></eventposdef>",
        "cfgeconomycore.xml": "<economycore><ce folder=\"db\"><file name=\"custom.xml\" type=\"types\"/></ce></economycore>",
    }
    results = {row["name"]: row for row in bot._fv_validate_all(files, "Livonia")}
    assert any("Missing" in issue["message"] for issue in _errors(results["db/events.xml"]))
    assert any("fehlende Datei" in issue["message"] for issue in _errors(results["cfgeconomycore.xml"]))
    assert any("12800" in issue["message"] for issue in results["cfgeventspawns.xml"]["issues"])


def test_detect_name_and_chance_fix():
    assert bot._fv_detect_name("text", "<types></types>") == "types.xml"
    assert bot._fv_detect_name("text", '{"Areas": []}') == "cfgeffectarea.json"
    source = '<spawnabletypes><type name="A"><cargo chance="1.5"/></type></spawnabletypes>'
    fixed = bot._fv_fix_text("cfgspawnabletypes.xml", source)
    assert 'chance="1"' in fixed


def test_duplicate_events_undefined_flags_and_spawns_without_event():
    files = {
        "db/types.xml": '<types><type name="A"><nominal>1</nominal><min>1</min><usage name="Nirgendwo"/><category name="tools"/></type></types>',
        "cfglimitsdefinition.xml": '<lists><categories><category name="tools"/></categories><usageflags><usage name="Town"/></usageflags><valueflags/><tags/></lists>',
        "db/events.xml": '<events><event name="E"><children/></event><event name="E"><children><child/></children></event></events>',
        "cfgeventspawns.xml": '<eventposdef><event name="E"><pos x="1" z="1"/></event><event name="Fremd"><pos x="1" z="1"/></event></eventposdef>',
    }
    results = {row["name"]: row for row in bot._fv_validate_all(files, "ChernarusPlus")}
    events = [i["message"] for i in results["db/events.xml"]["issues"]]
    assert any("Doppeltes <event name=\"E\">" in m for m in events) and any("kein type-Attribut" in m for m in events)
    warn = [i for i in results["db/types.xml"]["issues"] if i["severity"] == "warning"]
    assert len(warn) == 1 and "Nirgendwo" in warn[0]["message"] and "cfglimitsdefinition.xml" in warn[0]["message"]
    info = [i for i in results["cfgeventspawns.xml"]["issues"] if i["severity"] == "info"]
    assert len(info) == 1 and "Fremd" in info[0]["message"]
    # Ohne cfglimitsdefinition.xml keine Flag-Warnung (Datei nicht mitgeprüft)
    ohne = {k: v for k, v in files.items() if k != "cfglimitsdefinition.xml"}
    assert not [i for i in bot._fv_validate_all(ohne, "ChernarusPlus")[0]["issues"] if i["severity"] == "warning"]


# ── API ───────────────────────────────────────────────────────────────────
import hashlib  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_airstrike import call, servers  # noqa: E402,F401 - Fixture für pytest
from test_ignorelist_weaponblueprint import tool_files  # noqa: E402,F401 - Stub-FTP

KAPUTT = '<?xml version="1.0"?>\n<types>\n    <type name="Bad">\n        <nominal>2</nominal>\n        <min>7</min>\n        <lifetime>-4</lifetime>\n    </type>\n</types>\n'


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_api_check_fix_hash_backup_rollback_tenants(monkeypatch, servers):
    a, b = servers
    a.ftp.files["/mission/db/types.xml"] = KAPUTT
    status, result = call(monkeypatch, a, bot.api_tools_filevalidator_check, {"files": ["db/types.xml"]})
    assert status == 200, result
    datei = result["data"]["files"][0]
    assert datei["source_hash"] == _sha(KAPUTT) and not datei["valid"] and result["data"]["querbezuege"]
    assert {i["fix"] for i in datei["issues"] if i.get("fix")} == {"negative", "min_nominal"}
    # Eingefügter Text: ohne Querbezüge, Grenze 2 MB
    status, result = call(monkeypatch, a, bot.api_tools_filevalidator_check, {"pasted": {"name": "text", "content": "{\n bad\n}"}})
    assert status == 200 and result["data"]["hinweis"] and result["data"]["files"][0]["issues"][0]["line"] == 2
    status, _ = call(monkeypatch, a, bot.api_tools_filevalidator_check, {"pasted": {"name": "x.xml", "content": "x" * (2 * 1024 * 1024 + 1)}})
    assert status == 400
    status, _ = call(monkeypatch, a, bot.api_tools_filevalidator_check, {"files": ["gibt/es/nicht.xml"]})
    assert status == 404
    # Fix: Vorschau schreibt nichts, veralteter Hash 409, Anwenden .bak + nur betroffene Zeilen
    status, result = call(monkeypatch, a, bot.api_tools_filevalidator_fix, {"name": "db/types.xml", "source_hash": _sha(KAPUTT), "commit": False})
    assert status == 200 and result["data"]["changed"] and a.ftp.writes == []
    status, _ = call(monkeypatch, a, bot.api_tools_filevalidator_fix, {"name": "db/types.xml", "source_hash": "alt", "commit": True})
    assert status == 409 and a.ftp.writes == []
    status, result = call(monkeypatch, a, bot.api_tools_filevalidator_fix, {"name": "db/types.xml", "source_hash": _sha(KAPUTT), "commit": True})
    assert status == 200, result
    assert a.ftp.writes == ["/mission/db/types.xml.bak", "/mission/db/types.xml"]
    neu = a.ftp.files["/mission/db/types.xml"]
    assert a.ftp.files["/mission/db/types.xml.bak"] == KAPUTT and result["data"]["hash"] == _sha(neu)
    alt_z, neu_z = KAPUTT.splitlines(), neu.splitlines()
    assert len(alt_z) == len(neu_z) and [x for x, y in zip(alt_z, neu_z) if x != y] == ["        <min>7</min>", "        <lifetime>-4</lifetime>"]
    assert "<min>2</min>" in neu and "<lifetime>0</lifetime>" in neu
    assert b.ftp.writes == []
    # Rollback: Hauptdatei nicht schreibbar → 502, Original bleibt
    a.ftp.files["/mission/db/types.xml"] = KAPUTT; a.ftp.writes.clear()
    write_ok = bot._tools_datei_schreiben

    async def write(conn, name, content, _loop):
        if name == "db/types.xml":
            return False
        return await write_ok(conn, name, content, _loop)
    monkeypatch.setattr(bot, "_tools_datei_schreiben", write)
    status, _ = call(monkeypatch, a, bot.api_tools_filevalidator_fix, {"name": "db/types.xml", "source_hash": _sha(KAPUTT), "commit": True})
    assert status == 502 and a.ftp.files["/mission/db/types.xml"] == KAPUTT


def test_api_fix_refuses_result_that_does_not_parse(monkeypatch, servers):
    a, _ = servers
    # Doppeltes Schließtag und zusätzlich ein echter Parsefehler davor: der Fix entfernt nur den Schwanz
    text = "<types>\n<type name=\"A\"><nominal>1</nominal><min>2\n</types></types>\n"
    a.ftp.files["/mission/db/types.xml"] = text
    status, _ = call(monkeypatch, a, bot.api_tools_filevalidator_fix, {"name": "db/types.xml", "source_hash": _sha(text), "commit": True})
    assert status == 409 and a.ftp.writes == []
