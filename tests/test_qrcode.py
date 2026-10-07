"""QR-Code Generator: eingebetteter Encoder (Nayuki, MIT), Objektplatzierung,
Gerüst, Object-Spawner-Datei + cfggameplay.json, Rollback, Mandantentrennung.

    python3 -m pytest tests/test_qrcode.py -q

Die Referenz-Hashes stammen aus demselben Encoder; die Korrektheit der Matrizen
wurde einmalig mit zxing-cpp (Decoder) belegt – alle 12 Fälle wurden zum
Ursprungstext zurückgelesen. Die Hashes sichern seither die Regression.
"""
import hashlib
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_airstrike import call, servers  # noqa: F401 - Fixture für pytest
from test_ignorelist_weaponblueprint import tool_files  # noqa: F401 - Stub-FTP

FIXTURES = [
    ("HELLO WORLD", "L", 21, "fd8287e68916269f20704cc74b77b4c3a4757c2cb7d5fe72f5b277871657bf96"),
    ("HELLO WORLD", "M", 21, "9f15327b648fdbb6e3878c9a72183cc1f76f880c1b3e4f53fd7859bb5b4f2421"),
    ("HELLO WORLD", "Q", 21, "98ee3af8dfb46e027fa18006e4cb5079826d9b1aeeec68886f356578867c468c"),
    ("HELLO WORLD", "H", 25, "a57bd7af72066bf8715ba6bf7c068d9c924f3f99c4591c8e99d1948f09b3ec1a"),
    ("ABC123", "M", 21, "75feed2b3290eb412f3c0ba26ef79fc20f0ee0f5c1f497326d09586eda81d7d6"),
    ("DAYZ 2026", "M", 21, "870ad41728f47c6cbd3fb3616d1a42630dc03a1af8d2d39102ad539dc8c47b87"),
    ("https://discord.gg/", "M", 25, "a3640fe56c427bbca50eda327debb40beaf062c331dd00587eb8b0387848575f"),
    ("https://example.com/a?b=1", "M", 25, "00ca3a0ce5a85c4e1f5eefcad23114bbadde204a53cb139acb7e909b56efdc0e"),
    ("Grüße aus DayZ", "M", 25, "16d4a17b8bb816f7a7ba94fd982690f154cdb3c266a96c03f544971e95e95cb2"),
    ("1234567890", "H", 21, "9dc81188ca77aba66648ebc7db0c6ad8b7db7d9d0c09a70f7e3ca806cef17a7b"),
    ("TEST $%*+-./:", "Q", 21, "16118ae0db8145a0a6ea248d8b8efd33c53ab5c9b7270509f11338c8f73911a7"),
    ("https://doordiehub.com/QrCodeMaker", "M", 29, "84c9fcc8bbc74319c229640d92e825e11cc57456d1338871ca3e98b75e68e004"),
]

GAMEPLAY = json.dumps({"version": 1, "GeneralData": {}, "PlayerData": {},
                       "WorldsData": {"objectSpawnersArr": ["./custom/vorhanden.json"]}}, indent=4) + "\n"


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def loeschen_stub(monkeypatch):
    async def loeschen(conn, name, _loop):
        return conn.ftp.files.pop("/mission/" + name, None) is not None
    monkeypatch.setattr(bot, "_tools_datei_loeschen", loeschen)


def _body(**updates):
    value = {"name": "qr_test", "text": "https://discord.gg/brigarde", "ecc": "M",
             "position": {"x": 5000, "y": 100, "z": 6000}, "yaw": 0, "scale": 0.05, "spacing": 0.0405,
             "tile": "StaticObj_Misc_BoxWooden", "include_structure": True, "commit": False}
    value.update(updates)
    return value


# ── Encoder ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,ecc,size,digest", FIXTURES)
def test_encoder_matches_reference_fixtures(text, ecc, size, digest):
    matrix, version = bot._qrcode_matrix(text, ecc)
    assert len(matrix) == size and all(len(row) == size for row in matrix)
    assert version == (size - 17) // 4
    flat = "".join("1" if m else "0" for row in matrix for m in row)
    assert hashlib.sha256(flat.encode("ascii")).hexdigest() == digest
    # Finder-Muster: 7×7 mit dunklem Rand in drei Ecken
    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
        assert all(matrix[r0][c0 + i] and matrix[r0 + 6][c0 + i] and matrix[r0 + i][c0] and matrix[r0 + i][c0 + 6]
                   for i in range(7))


def test_encoder_limits_and_speed():
    with pytest.raises(ValueError):
        bot._qrcode_matrix("x" * 400, "H")          # passt nicht in Version 10
    started = time.perf_counter()
    matrix, version = bot._qrcode_matrix("x" * 200, "L")
    assert version <= 10 and len(matrix) == 17 + 4 * version
    assert time.perf_counter() - started < 1.5   # real ~0,07 s; Luft für parallele Testläufe


# ── Objektplatzierung ─────────────────────────────────────────────────────
def test_objects_centre_width_rotation_and_structure():
    matrix, _ = bot._qrcode_matrix("https://discord.gg/brigarde", "M")
    dark = sum(1 for row in matrix for m in row if m)
    base = (5000.0, 100.0, 6000.0, 0.0, 0.05, 0.0405, "StaticObj_Misc_BoxWooden", False)
    objs = bot._qrcode_objects(matrix, base)
    assert len(objs) == dark and {o["name"] for o in objs} == {"StaticObj_Misc_BoxWooden"}
    assert all(o["scale"] == 0.05 and o["enableCEPersistency"] == 0 and o["customString"] == "" for o in objs)
    xs = [o["pos"][0] for o in objs]; ys = [o["pos"][1] for o in objs]; zs = [o["pos"][2] for o in objs]
    assert round((max(xs) + min(xs)) / 2, 3) == 5000.0 and round((max(zs) + min(zs)) / 2, 3) == 6000.0
    assert round((max(ys) + min(ys)) / 2, 3) == 100.0
    breite = (len(matrix) - 1) * 0.0405
    assert abs(max(ys) - min(ys) - breite) < 1e-3
    assert abs(((max(xs) - min(xs)) ** 2 + (max(zs) - min(zs)) ** 2) ** 0.5 - breite) < 1e-3
    # Drehung um 90°: Ausdehnung wandert zwischen den Achsen, Mitte bleibt
    gedreht = bot._qrcode_objects(matrix, base[:3] + (90.0,) + base[4:])
    gx = [o["pos"][0] for o in gedreht]; gz = [o["pos"][2] for o in gedreht]
    assert abs((max(gx) - min(gx)) - (max(zs) - min(zs))) < 1e-2 and abs((max(gz) - min(gz)) - (max(xs) - min(xs))) < 1e-2
    assert all(o["ypr"][0] == 90.0 for o in gedreht)
    # Gerüst: 28 zusätzliche Objekte mit eigenen Skalierungen, um dieselbe Mitte
    mit = bot._qrcode_objects(matrix, base[:7] + (True,))
    assert len(mit) == dark + len(bot._QR_GERUEST) == dark + 28
    geruest = mit[dark:]
    assert {o["name"] for o in geruest} == {n for n, *_ in bot._QR_GERUEST} and "DoorTestCamera" not in {o["name"] for o in geruest}
    assert all(abs(o["pos"][0] - 5000) < 1.5 and abs(o["pos"][2] - 6000) < 1.5 for o in geruest)
    assert all(len(str(v).split(".")[-1]) <= 4 for o in mit for v in o["pos"])


def test_too_many_objects_is_rejected():
    big = [[True] * 57 for _ in range(57)]            # 3249 → über der Grenze
    with pytest.raises(ValueError):
        bot._qrcode_objects(big, (5000.0, 100.0, 6000.0, 0.0, 0.05, 0.0405, "StaticObj_Misc_BoxWooden", True))
    matrix, version = bot._qrcode_matrix("x" * 200, "L")   # groß, aber erlaubt
    assert version <= 10 and len(bot._qrcode_objects(matrix, (5000.0, 100.0, 6000.0, 0.0, 0.05, 0.0405, "StaticObj_Misc_BoxWooden", True))) <= bot._QR_MAX_OBJEKTE


# ── API ───────────────────────────────────────────────────────────────────
def test_api_preview_commit_remove_and_tenants(monkeypatch, servers):
    a, b = servers
    for conn in (a, b):
        conn.ftp.files["/mission/cfggameplay.json"] = GAMEPLAY
        conn.ftp.mkdir = lambda path: True
    status, result = call(monkeypatch, a, bot.api_tools_qrcode_get)
    assert status == 200 and result["data"]["qrcodes"] == [] and result["data"]["kein_mission_ordner"] is False
    # Vorschau: Matrix als Zeilen-Strings, Zusammenfassung, nichts geschrieben
    status, result = call(monkeypatch, a, bot.api_tools_qrcode_post, _body())
    assert status == 200, result
    d = result["data"]
    assert d["grid"] == 29 and len(d["matrix"]) == 29 and set("".join(d["matrix"])) <= {"0", "1"}
    assert d["objekte_geruest"] == 28 and d["count"] == d["objekte_qr"] + 28 and d["breite_m"] == round(29 * 0.0405, 3)
    assert d["generated"][0]["filename"] == "custom/qrcode/qr_test.json" and a.ftp.writes == []
    assert json.loads(d["generated"][0]["content"])["Objects"][0]["name"] == "StaticObj_Misc_BoxWooden"
    # Eingabefehler → 400
    for bad in (dict(name="böse name"), dict(text=""), dict(ecc="X"), dict(position={"x": 99999, "y": 1, "z": 1}),
                dict(tile="Gibt_Es_Nicht"), dict(text="x" * 513), dict(scale=5)):
        status, _ = call(monkeypatch, a, bot.api_tools_qrcode_post, _body(**bad))
        assert status == 400, bad
    # Commit: Datei + Eintrag, Rest der cfggameplay.json unverändert
    status, result = call(monkeypatch, a, bot.api_tools_qrcode_post, _body(commit=True))
    assert status == 200, result
    assert a.ftp.writes == ["/mission/custom/qrcode/qr_test.json", "/mission/cfggameplay.json"]
    gameplay = json.loads(a.ftp.files["/mission/cfggameplay.json"])
    assert gameplay["WorldsData"]["objectSpawnersArr"] == ["./custom/vorhanden.json", "custom/qrcode/qr_test.json"]
    assert gameplay["GeneralData"] == {} and gameplay["version"] == 1
    datei = json.loads(a.ftp.files["/mission/custom/qrcode/qr_test.json"])
    assert len(datei["Objects"]) == d["count"]
    assert len(a.data["qrcodes"]) == 1 and a.data["qrcodes"][0]["name"] == "qr_test"
    # gleicher Name → 409; Mandant B sieht nichts und kann nicht entfernen
    status, _ = call(monkeypatch, a, bot.api_tools_qrcode_post, _body(commit=True))
    assert status == 409
    status, result = call(monkeypatch, b, bot.api_tools_qrcode_get)
    assert status == 200 and result["data"]["qrcodes"] == []
    status, _ = call(monkeypatch, b, bot.api_tools_qrcode_remove, {"id": a.data["qrcodes"][0]["id"]})
    assert status == 404 and b.ftp.writes == []
    # Entfernen stellt cfggameplay.json byteidentisch her, Datei weg, Manifest leer
    a.ftp.writes.clear()
    status, _ = call(monkeypatch, a, bot.api_tools_qrcode_remove, {"id": a.data["qrcodes"][0]["id"]})
    assert status == 200
    assert a.ftp.files["/mission/cfggameplay.json"] == GAMEPLAY
    assert "/mission/custom/qrcode/qr_test.json" not in a.ftp.files and a.data["qrcodes"] == []


def test_api_rollback_when_gameplay_write_fails(monkeypatch, servers):
    a, _ = servers
    a.ftp.files["/mission/cfggameplay.json"] = GAMEPLAY
    a.ftp.mkdir = lambda path: True
    write_ok = bot._tools_datei_schreiben

    async def write(conn, name, content, _loop):
        if name == "cfggameplay.json":
            return False
        return await write_ok(conn, name, content, _loop)
    monkeypatch.setattr(bot, "_tools_datei_schreiben", write)
    status, _ = call(monkeypatch, a, bot.api_tools_qrcode_post, _body(commit=True))
    assert status == 502
    assert "/mission/custom/qrcode/qr_test.json" not in a.ftp.files
    assert a.ftp.files["/mission/cfggameplay.json"] == GAMEPLAY and not a.data.get("qrcodes")
