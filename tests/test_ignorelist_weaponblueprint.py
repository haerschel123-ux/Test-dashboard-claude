"""Ignore List Generator (cfgignorelist.xml) und Weapon Blueprint Generator
(cfgspawnabletypes.xml): Vanilla-Fixtures, Prüfsummen, Backup, Reset.

    python3 -m pytest tests/test_ignorelist_weaponblueprint.py -q
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_airstrike import call, servers  # noqa: F401 - Fixture für pytest

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
TYPES = ('<?xml version="1.0"?>\n<types>\n'
         + "".join(f'    <type name="{k}"><nominal>1</nominal><category name="weapons"/></type>\n' for k in
                   ("M4A1", "AKM", "BUISOptic", "ACOGOptic"))
         + "".join(f'    <type name="{k}"><nominal>1</nominal></type>\n' for k in
                   ("Bandage", "EasterEgg", "M4_OEBttstck", "PlateCarrierVest_Camo"))
         + "</types>\n")


def _lies(*teile):
    with open(os.path.join(FIX, *teile), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(autouse=True)
def tool_files(monkeypatch):
    async def read(conn, name, _loop):
        for path in conn.ftp.files:
            if path.lower() == ("/mission/" + name).lower():
                return conn.ftp.files[path], "ok"
        return None, "missing"

    async def write(conn, name, content, _loop):
        path = "/mission/" + name
        conn.ftp.writes.append(path)
        conn.ftp.files[path] = content
        return True
    monkeypatch.setattr(bot, "_tools_datei_lesen", read)
    monkeypatch.setattr(bot, "_tools_datei_schreiben", write)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Ignore List ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("datei", ["ignorelist/cfgIgnoreList-chernarusplus.xml",
                                   "ignorelist/cfgignorelist-enoch.xml", "ignorelist/cfgignorelist-sakhal.xml"])
def test_ignorelist_vanilla_fixture_roundtrip(monkeypatch, servers, datei):
    a, _ = servers
    raw = _lies(datei)
    a.ftp.files["/mission/" + os.path.basename(datei).split("-")[0] + ".xml"] = raw   # echte Groß-/Kleinschreibung
    a.ftp.files["/mission/db/types.xml"] = TYPES
    status, result = call(monkeypatch, a, bot.api_tools_ignorelist_get)
    assert status == 200, result
    d = result["data"]
    assert d["entries"] == bot._IGNORELIST_VANILLA and d["vorhanden"] and d["hash"] == _sha(raw)
    assert "Bandage" in d["classnames"]
    # Vorschau ohne Änderung: gleiche Einträge, Vanilla-Namen ohne types-Eintrag lösen keine Warnung aus
    status, result = call(monkeypatch, a, bot.api_tools_ignorelist_post,
                          {"entries": d["entries"], "source_hash": d["hash"], "commit": False})
    assert status == 200, result
    assert result["data"]["warnungen"] == [] and a.ftp.writes == []
    assert bot._ignorelist_read(result["data"]["generated"][0]["content"]) == bot._IGNORELIST_VANILLA
    assert '\t<type name="Bandage"></type>' in result["data"]["generated"][0]["content"]


def test_ignorelist_missing_file_add_warn_commit_backup(monkeypatch, servers):
    a, _ = servers
    a.ftp.files["/mission/db/types.xml"] = TYPES
    status, result = call(monkeypatch, a, bot.api_tools_ignorelist_get)
    assert status == 200 and result["data"]["vorhanden"] is False and result["data"]["hash"] is None
    # fehlende Datei: Vorschau ohne 409, unbekannte Klasse nur Warnung
    status, result = call(monkeypatch, a, bot.api_tools_ignorelist_post,
                          {"entries": ["Bandage", "GibtEsNicht"], "source_hash": None, "commit": False})
    assert status == 200, result
    assert result["data"]["warnungen"] and "GibtEsNicht" in result["data"]["warnungen"][0]
    # falsches Format → 400
    status, _ = call(monkeypatch, a, bot.api_tools_ignorelist_post, {"entries": ["böse name"], "source_hash": None, "commit": False})
    assert status == 400
    # Anlegen ohne Original: keine .bak, Datei neu
    status, result = call(monkeypatch, a, bot.api_tools_ignorelist_post,
                          {"entries": ["Bandage"], "source_hash": None, "commit": True})
    assert status == 200, result
    assert a.ftp.writes == ["/mission/cfgignorelist.xml"]
    neu = a.ftp.files["/mission/cfgignorelist.xml"]
    assert result["data"]["hash"] == _sha(neu)
    # veralteter Hash → 409; passender Hash → .bak vor Hauptdatei
    status, _ = call(monkeypatch, a, bot.api_tools_ignorelist_post, {"entries": ["EasterEgg"], "source_hash": "alt", "commit": True})
    assert status == 409
    a.ftp.writes.clear()
    status, _ = call(monkeypatch, a, bot.api_tools_ignorelist_post, {"entries": ["Bandage", "EasterEgg"], "source_hash": _sha(neu), "commit": True})
    assert status == 200
    assert a.ftp.writes == ["/mission/cfgignorelist.xml.bak", "/mission/cfgignorelist.xml"]
    assert a.ftp.files["/mission/cfgignorelist.xml.bak"] == neu


def test_ignorelist_duplicates_from_server_file_are_warning_not_error(monkeypatch, servers):
    """Echte Server-Datei mit `Flaregun` und `flaregun`: unveränderte Liste darf
    die Vorschau nicht blockieren; Doppelte werden auf das erste Vorkommen reduziert."""
    a, _ = servers
    raw = bot._ignorelist_xml(["Bandage", "Flaregun", "flaregun", "Bandage"])
    a.ftp.files["/mission/cfgignorelist.xml"] = raw
    a.ftp.files["/mission/db/types.xml"] = TYPES
    status, result = call(monkeypatch, a, bot.api_tools_ignorelist_get)
    assert status == 200 and result["data"]["entries"] == ["Bandage", "Flaregun", "flaregun", "Bandage"]
    status, result = call(monkeypatch, a, bot.api_tools_ignorelist_post,
                          {"entries": result["data"]["entries"], "source_hash": result["data"]["hash"], "commit": False})
    assert status == 200, result
    warn = [w for w in result["data"]["warnungen"] if w.startswith("Doppelte")]
    assert len(warn) == 1 and "flaregun (= Flaregun)" in warn[0] and "Bandage" in warn[0]
    assert bot._ignorelist_read(result["data"]["generated"][0]["content"]) == ["Bandage", "Flaregun"]
    status, result = call(monkeypatch, a, bot.api_tools_ignorelist_post,
                          {"entries": ["Bandage", "Flaregun", "flaregun", "Bandage"], "source_hash": _sha(raw), "commit": True})
    assert status == 200, result
    assert bot._ignorelist_read(a.ftp.files["/mission/cfgignorelist.xml"]) == ["Bandage", "Flaregun"]


def test_ignorelist_defect_file_never_overwritten(monkeypatch, servers):
    a, _ = servers
    a.ftp.files["/mission/cfgignorelist.xml"] = "<kaputt>"
    status, _ = call(monkeypatch, a, bot.api_tools_ignorelist_get)
    assert status == 502
    status, _ = call(monkeypatch, a, bot.api_tools_ignorelist_post, {"entries": ["Bandage"], "source_hash": None, "commit": True})
    assert status == 409 and a.ftp.writes == []


# ── Weapon Blueprint ─────────────────────────────────────────────────────
def _spawnable(conn):
    raw = _lies("cfgspawnabletypes-chernarusplus.xml")
    conn.ftp.files["/mission/cfgspawnabletypes.xml"] = raw
    conn.ftp.files["/mission/db/types.xml"] = TYPES
    return raw


def test_blueprint_catalog_from_vanilla(monkeypatch, servers):
    a, _ = servers
    raw = _spawnable(a)
    status, result = call(monkeypatch, a, bot.api_tools_weaponblueprint_get)
    assert status == 200, result
    w = result["data"]["weapons"]
    assert "M4A1" in w and "AKM" in w and not any(k.startswith("Zmb") for k in w)
    # nur Kategorie "weapons": Westen/Fahrzeuge landen in "sonstige"
    assert result["data"]["gefiltert"] is True
    assert "PlateCarrierVest_Camo" not in w and "CivilianSedan" not in w
    assert "PlateCarrierVest_Camo" in result["data"]["sonstige"] and "CivilianSedan" in result["data"]["sonstige"]
    # ohne types.xml: ungefiltert
    del a.ftp.files["/mission/db/types.xml"]
    status, result = call(monkeypatch, a, bot.api_tools_weaponblueprint_get)
    assert status == 200 and result["data"]["gefiltert"] is False and "PlateCarrierVest_Camo" in result["data"]["weapons"]
    a.ftp.files["/mission/db/types.xml"] = TYPES
    m4 = w["M4A1"]
    assert len(m4["slots"]) == 4 and m4["slots"][2]["items"][0]["item"] == "BUISOptic"
    assert m4["slots"][3]["chance"] == 0.1 and m4["rest"] == ['<damage min="0.45" max="0.85" />']
    assert result["data"]["hash"] == _sha(raw)


def test_blueprint_replace_keeps_damage_and_resets_byte_identical(monkeypatch, servers):
    a, _ = servers
    raw = _spawnable(a)
    original = bot._tool_finde_benannten_block(raw, "type", "M4A1")["block"]
    payload = {"weapon": "M4A1", "source_hash": _sha(raw), "commit": False, "slots": [
        {"chance": 1, "items": [{"item": "M4_OEBttstck", "chance": 1}]},
        {"chance": 0.5, "items": [{"item": "BUISOptic", "chance": 0.3}, {"item": "ACOGOptic", "chance": 0.7}]}]}
    status, result = call(monkeypatch, a, bot.api_tools_weaponblueprint_post, payload)
    assert status == 200, result
    block = result["data"]["generated"][0]["content"]
    assert '<damage min="0.45" max="0.85" />' in block and block.count("<attachments") == 2
    assert 'name="ACOGOptic" chance="0.70"' in block
    assert any("ACOGOptic" in w for w in result["data"]["warnungen"])      # nicht aus dem Vanilla-Block
    assert a.ftp.writes == []
    payload["commit"] = True
    status, result = call(monkeypatch, a, bot.api_tools_weaponblueprint_post, payload)
    assert status == 200, result
    assert a.ftp.writes == ["/mission/cfgspawnabletypes.xml.bak", "/mission/cfgspawnabletypes.xml"]
    neu = a.ftp.files["/mission/cfgspawnabletypes.xml"]
    assert neu.count('<type name="M4A1">') == 1 and neu.count("<type ") == raw.count("<type ")
    assert a.data["weapon_blueprints"]["M4A1"] == original
    # zweite Änderung behält das ERSTE Original
    payload["source_hash"] = _sha(neu); payload["slots"] = payload["slots"][:1]
    status, _ = call(monkeypatch, a, bot.api_tools_weaponblueprint_post, payload)
    assert status == 200 and a.data["weapon_blueprints"]["M4A1"] == original
    status, result = call(monkeypatch, a, bot.api_tools_weaponblueprint_reset, {"weapon": "M4A1"})
    assert status == 200, result
    assert a.ftp.files["/mission/cfgspawnabletypes.xml"] == raw
    assert a.data["weapon_blueprints"] == {}


def test_blueprint_validation_and_conflicts(monkeypatch, servers):
    a, _ = servers
    raw = _spawnable(a)
    basis = {"weapon": "M4A1", "source_hash": _sha(raw), "commit": True}
    faelle = (
        {"slots": [{"chance": 1, "items": [{"item": "GibtEsNicht", "chance": 1}]}]},      # nicht in types.xml
        {"slots": [{"chance": 1.5, "items": [{"item": "BUISOptic", "chance": 1}]}]},      # Slot-Chance
        {"slots": [{"chance": 1, "items": [{"item": "BUISOptic", "chance": 0}]}]},        # Item-Chance
        {"slots": []},
        {"weapon": "GibtEsNicht", "slots": [{"chance": 1, "items": [{"item": "BUISOptic", "chance": 1}]}]},
        {"source_hash": "alt", "slots": [{"chance": 1, "items": [{"item": "BUISOptic", "chance": 1}]}]},
    )
    for f in faelle:
        p = dict(basis); p.update(f)
        status, result = call(monkeypatch, a, bot.api_tools_weaponblueprint_post, p)
        assert status in (400, 409), (f, result)
    assert a.ftp.writes == []
    status, _ = call(monkeypatch, a, bot.api_tools_weaponblueprint_reset, {"weapon": "M4A1"})
    assert status == 404


def test_blueprint_rollback_and_tenants(monkeypatch, servers):
    a, b = servers
    raw = _spawnable(a); _spawnable(b)
    write_ok = bot._tools_datei_schreiben

    async def write(conn, name, content, _loop):
        if name == "cfgspawnabletypes.xml":
            return False
        return await write_ok(conn, name, content, _loop)
    monkeypatch.setattr(bot, "_tools_datei_schreiben", write)
    p = {"weapon": "M4A1", "source_hash": _sha(raw), "commit": True,
         "slots": [{"chance": 1, "items": [{"item": "BUISOptic", "chance": 1}]}]}
    status, _ = call(monkeypatch, a, bot.api_tools_weaponblueprint_post, p)
    assert status == 502 and "weapon_blueprints" not in a.data
    assert a.ftp.files["/mission/cfgspawnabletypes.xml"] == raw
    monkeypatch.setattr(bot, "_tools_datei_schreiben", write_ok)
    status, _ = call(monkeypatch, a, bot.api_tools_weaponblueprint_post, p)
    assert status == 200
    status, result = call(monkeypatch, b, bot.api_tools_weaponblueprint_get)
    assert status == 200 and result["data"]["blueprints"] == []
    status, _ = call(monkeypatch, b, bot.api_tools_weaponblueprint_reset, {"weapon": "M4A1"})
    assert status == 404
    assert b.ftp.files["/mission/cfgspawnabletypes.xml"] == raw
