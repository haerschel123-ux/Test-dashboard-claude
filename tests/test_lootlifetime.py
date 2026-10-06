"""Loot Lifetime Reducer (db/types.xml): Rechenlogik, Filter über echte
CE-Metadaten, nur <lifetime>-Zeilen ändern sich, Manifest/Reset byteidentisch,
Prüfsummen, Backup, Rollback, Mandantentrennung.

    python3 -m pytest tests/test_lootlifetime.py -q
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_airstrike import call, servers  # noqa: F401 - Fixture für pytest
from test_ignorelist_weaponblueprint import tool_files  # noqa: F401 - Stub-FTP

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "types")

SAMPLE = '''<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<types>
    <!-- Kommentar bleibt -->
    <type name="Ammo_Test">
        <nominal>10</nominal>
        <lifetime>10800</lifetime>
        <category name="tools"/>
        <usage name="Military"/>
        <tag name="shelves"/>
    </type>
    <type name="Food_Test">
        <lifetime>400</lifetime>
        <category name="food"/>
    </type>
    <type name="Low_Test">
        <lifetime>200</lifetime>
        <category name="food"/>
    </type>
    <type name="Base_Test">
        <lifetime>3888000</lifetime>
        <category name="containers"/>
    </type>
    <type name="Null_Test">
        <lifetime>0</lifetime>
    </type>
    <type name="NoLife">
        <category name="tools"/>
    </type>
</types>
'''


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def params(**updates):
    value = {"reduction": 50, "floor": 300, "mode": "all", "selected": [], "patterns": [], "standard_exclude": True}
    value.update(updates)
    return value


def _request(**updates):
    """POST-Body aus params(): Filter verschachtelt wie die Oberfläche."""
    p = params(**updates)
    return {"reduction": p["reduction"], "floor": p["floor"], "standard_exclude": p["standard_exclude"],
            "filter": {"mode": p["mode"], "selected": p["selected"], "patterns": p["patterns"]},
            "source_hash": updates.get("source_hash"), "commit": updates.get("commit", False)}


def _nur_lifetime_zeilen(vorher, nachher):
    a, b = vorher.splitlines(), nachher.splitlines()
    return len(a) == len(b) and all(x == y or ("<lifetime>" in x and "<lifetime>" in y) for x, y in zip(a, b))


# ── Rechenlogik ───────────────────────────────────────────────────────────
def test_math_floor_skip_protect_and_only_lifetime_lines_change():
    after, stat, original = bot._loot_lifetime_anwenden(SAMPLE, params())
    # 5 Einträge mit <lifetime>: Ammo 10800→5400, Food 400→300 (am Minimum),
    # Low 200 bleibt (schon unter Ziel), Base 3888000 und Null 0 geschützt.
    assert (stat["total"], stat["reduced"], stat["minimum"], stat["skipped"]) == (5, 2, 1, 4)
    assert original == {"Ammo_Test": 10800, "Food_Test": 400}
    assert stat["changes"] == [{"name": "Ammo_Test", "old": 10800, "new": 5400},
                               {"name": "Food_Test", "old": 400, "new": 300}]
    assert stat["average"] == 49  # (11200 − 5700) / 11200
    assert "<lifetime>5400</lifetime>" in after and "<lifetime>200</lifetime>" in after
    assert "<lifetime>3888000</lifetime>" in after and "<lifetime>0</lifetime>" in after
    assert _nur_lifetime_zeilen(SAMPLE, after) and "<!-- Kommentar bleibt -->" in after


def test_without_protection_long_lived_entries_are_reduced():
    after, stat, original = bot._loot_lifetime_anwenden(SAMPLE, params(standard_exclude=False))
    assert original["Base_Test"] == 3888000 and "<lifetime>1944000</lifetime>" in after
    assert "Null_Test" not in original  # 0 → max(0, 300) wäre länger, bleibt


def test_filters_by_category_usage_tag_and_pattern():
    _, stat, _ = bot._loot_lifetime_anwenden(SAMPLE, params(mode="include", selected=["tools"]))
    assert [c["name"] for c in stat["changes"]] == ["Ammo_Test"]
    _, stat, _ = bot._loot_lifetime_anwenden(SAMPLE, params(mode="include", selected=["Military"]))
    assert [c["name"] for c in stat["changes"]] == ["Ammo_Test"]
    _, stat, _ = bot._loot_lifetime_anwenden(SAMPLE, params(mode="include", selected=["shelves"]))
    assert [c["name"] for c in stat["changes"]] == ["Ammo_Test"]
    _, stat, _ = bot._loot_lifetime_anwenden(SAMPLE, params(mode="include", patterns=["food_"]))
    assert [c["name"] for c in stat["changes"]] == ["Food_Test"]
    _, stat, _ = bot._loot_lifetime_anwenden(SAMPLE, params(mode="exclude", selected=["tools"]))
    assert [c["name"] for c in stat["changes"]] == ["Food_Test"]


def test_restore_is_byte_identical_and_input_validation():
    after, _stat, originals = bot._loot_lifetime_anwenden(SAMPLE, params(standard_exclude=False))
    assert after != SAMPLE and bot._loot_lifetime_werte(after, originals) == SAMPLE
    for bad in ({"reduction": 96, "floor": 300}, {"reduction": 0, "floor": 300},
                {"reduction": 50, "floor": 30}, {"reduction": 50, "floor": 7260},
                {"reduction": 50, "floor": 301},
                {"reduction": 50, "floor": 300, "filter": {"mode": "include"}},
                {"reduction": 50, "floor": 300, "filter": {"mode": "bla"}},
                {"reduction": "x", "floor": 300}):
        with pytest.raises(ValueError):
            bot._loot_lifetime_eingabe(bad)


@pytest.mark.parametrize("karte", ["chernarusplus", "enoch", "sakhal"])
def test_vanilla_fixture_roundtrip_fast(karte):
    with open(os.path.join(FIX, f"types-{karte}.xml"), encoding="utf-8") as fh:
        raw = fh.read()
    rows, stats = bot._loot_lifetime_lesen(raw)
    assert len(rows) > 1900 and "weapons" in stats["categories"] and "Military" in stats["usages"]
    after, stat, originals = bot._loot_lifetime_anwenden(raw, params())
    assert stat["reduced"] > 1500 and _nur_lifetime_zeilen(raw, after)
    assert bot._loot_lifetime_werte(after, originals) == raw


# ── API ───────────────────────────────────────────────────────────────────
def test_api_preview_commit_manifest_second_apply_and_reset(monkeypatch, servers):
    a, b = servers
    a.ftp.files["/mission/db/types.xml"] = SAMPLE
    status, result = call(monkeypatch, a, bot.api_tools_lootlifetime_get)
    assert status == 200, result
    d = result["data"]
    assert d["hash"] == _sha(SAMPLE) and d["anzahl"] == 6 and d["manifest"] is None
    assert d["categories"] == {"tools": 2, "food": 2, "containers": 1} and d["usages"] == {"Military": 1}
    # Vorschau schreibt nichts
    status, result = call(monkeypatch, a, bot.api_tools_lootlifetime_post, _request(source_hash=d["hash"]))
    assert status == 200 and a.ftp.writes == [] and "loot_lifetime" not in a.data
    assert result["data"]["statistic"]["reduced"] == 2 and result["data"]["warnings"]
    # veralteter Hash → 409
    status, _ = call(monkeypatch, a, bot.api_tools_lootlifetime_post, _request(source_hash="alt", commit=True))
    assert status == 409 and a.ftp.writes == []
    # Anwenden: .bak vor Datei, nur lifetime-Zeilen, Manifest mit Originalen
    status, result = call(monkeypatch, a, bot.api_tools_lootlifetime_post, _request(source_hash=d["hash"], commit=True))
    assert status == 200, result
    assert a.ftp.writes == ["/mission/db/types.xml.bak", "/mission/db/types.xml"]
    assert a.ftp.files["/mission/db/types.xml.bak"] == SAMPLE
    einmal = a.ftp.files["/mission/db/types.xml"]
    assert _nur_lifetime_zeilen(SAMPLE, einmal) and "<lifetime>5400</lifetime>" in einmal
    assert a.data["loot_lifetime"]["originals"] == {"Ammo_Test": 10800, "Food_Test": 400}
    assert "commit" not in a.data["loot_lifetime"]["params"]
    assert result["data"]["hash"] == _sha(einmal)
    # Zweites Anwenden mit 25 % rechnet vom Original (10800 → 8100), nicht 5400 → 4050
    a.ftp.writes.clear()
    status, result = call(monkeypatch, a, bot.api_tools_lootlifetime_post,
                          _request(reduction=25, source_hash=_sha(einmal), commit=True))
    assert status == 200, result
    zweimal = a.ftp.files["/mission/db/types.xml"]
    assert "<lifetime>8100</lifetime>" in zweimal and "<lifetime>4050</lifetime>" not in zweimal
    assert a.data["loot_lifetime"]["originals"]["Ammo_Test"] == 10800
    # Reset: byteidentisch zum Original, Manifest leer, Mandant 2000 unberührt
    a.ftp.writes.clear()
    status, result = call(monkeypatch, a, bot.api_tools_lootlifetime_reset, {})
    assert status == 200, result
    assert result["data"]["restored"] == 2 and result["data"]["missing"] == []
    assert a.ftp.files["/mission/db/types.xml"] == SAMPLE
    assert a.ftp.writes == ["/mission/db/types.xml.bak", "/mission/db/types.xml"]
    assert not a.data.get("loot_lifetime", {}).get("originals")
    assert "loot_lifetime" not in b.data and b.ftp.writes == []
    status, _ = call(monkeypatch, b, bot.api_tools_lootlifetime_reset, {})
    assert status == 404


def test_api_reset_reports_missing_types_and_defect_file_is_never_written(monkeypatch, servers):
    a, _ = servers
    a.ftp.files["/mission/db/types.xml"] = SAMPLE
    status, _ = call(monkeypatch, a, bot.api_tools_lootlifetime_post, _request(source_hash=_sha(SAMPLE), commit=True))
    assert status == 200
    # Eintrag inzwischen von Hand entfernt → Reset meldet ihn, bricht nicht ab
    ohne = a.ftp.files["/mission/db/types.xml"].replace(
        '    <type name="Food_Test">\n        <lifetime>300</lifetime>\n        <category name="food"/>\n    </type>\n', "")
    assert ohne != a.ftp.files["/mission/db/types.xml"]
    a.ftp.files["/mission/db/types.xml"] = ohne
    status, result = call(monkeypatch, a, bot.api_tools_lootlifetime_reset, {})
    assert status == 200 and result["data"]["restored"] == 1 and result["data"]["missing"] == ["Food_Test"]
    assert "<lifetime>10800</lifetime>" in a.ftp.files["/mission/db/types.xml"]
    # defekte Datei: GET 409, POST 409, nichts geschrieben
    a.ftp.files["/mission/db/types.xml"] = "<kaputt>"
    a.ftp.writes.clear()
    status, _ = call(monkeypatch, a, bot.api_tools_lootlifetime_get)
    assert status == 409
    status, _ = call(monkeypatch, a, bot.api_tools_lootlifetime_post, _request(source_hash=_sha("<kaputt>"), commit=True))
    assert status == 409 and a.ftp.writes == []
    # Eingabefehler über die API → 400
    a.ftp.files["/mission/db/types.xml"] = SAMPLE
    status, _ = call(monkeypatch, a, bot.api_tools_lootlifetime_post, _request(floor=30, source_hash=_sha(SAMPLE)))
    assert status == 400


def test_api_rollback_when_main_write_fails(monkeypatch, servers):
    a, _ = servers
    a.ftp.files["/mission/db/types.xml"] = SAMPLE
    write_ok = bot._tools_datei_schreiben

    async def write(conn, name, content, _loop):
        if name == "db/types.xml":
            return False
        return await write_ok(conn, name, content, _loop)
    monkeypatch.setattr(bot, "_tools_datei_schreiben", write)
    status, _ = call(monkeypatch, a, bot.api_tools_lootlifetime_post, _request(source_hash=_sha(SAMPLE), commit=True))
    assert status == 502 and "loot_lifetime" not in a.data
    assert a.ftp.files["/mission/db/types.xml"] == SAMPLE
