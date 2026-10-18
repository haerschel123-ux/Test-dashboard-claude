"""Bug-Report-Knopf: Meldung -> Embed (mit Screenshot) im Betreiber-Discord; Channel-Einstellung nur fuer Admins.

    python3 -m pytest tests/test_bugreport.py -v
"""
import asyncio
import base64
import json
import os
import sys
import time

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()
GUILD_ID, KANAL_ID, FREMD_KANAL_ID = 1534352039713439855, 777, 888
_zaehler = [0]


class _Rechte:
    def __init__(self, ok=True):
        self.send_messages = self.embed_links = self.attach_files = ok


class _Kanal:
    def __init__(self, cid, guild, name="bug-reports", ok=True):
        self.id, self.guild, self.name, self._ok = cid, guild, name, ok
        self.gesendet = []

    def permissions_for(self, _me):
        return _Rechte(self._ok)

    async def send(self, **kw):
        self.gesendet.append(kw)


class _Guild:
    name = "Betreiber-Discord"
    me = object()

    def __init__(self):
        self.kanal = _Kanal(KANAL_ID, self)
        self.text_channels = [self.kanal]

    def get_channel(self, cid):
        return self.kanal if cid == KANAL_ID else None


@pytest.fixture
def umgebung(monkeypatch):
    guild = _Guild()
    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guild if gid == GUILD_ID else None)
    monkeypatch.setitem(bot.cfg.config, "bug_report_guild_id", str(GUILD_ID))
    monkeypatch.setitem(bot.cfg.config, "bug_report_channel_id", str(KANAL_ID))
    monkeypatch.setattr(bot.cfg, "save_config", lambda *a, **k: None)
    bot._BUG_ZEITEN.clear()
    bot._DASH_RATE_LIMIT_LAST.clear()
    return guild


def _anfrage(monkeypatch, methode, pfad, daten=None, admin=False, discord_id=None):
    _zaehler[0] += 1
    sid = f"bug-sid-{time.time_ns()}"
    uid = discord_id or f"acct-{_zaehler[0]}"
    bot._SESS_STORE[sid] = {"token": "fake", "discord": {"id": uid, "name": "Tester"}, "is_admin": admin,
                            "admin_geprueft_ts": time.time(), "seen": time.time()}
    req = make_mocked_request(methode, pfad, headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    if daten is not None:
        async def fake_body(_r):
            return daten
        monkeypatch.setattr(bot, "body", fake_body)
    return req


def _lauf(coro):
    r = asyncio.run(coro)
    return r.status, json.loads(r.body.decode("utf-8"))


def _melden(monkeypatch, daten, **kw):
    return _lauf(bot.api_bugreport_post(_anfrage(monkeypatch, "POST", "/api/bugreport", daten, **kw)))


GUT = {"titel": "Karte lädt nicht", "beschreibung": "Beim Öffnen des Tools bleibt die Karte leer.",
       "kontext": {"seite": "tools", "sprache": "de", "bildschirm": "412×900", "browser": "Chrome Mobile", "version": "abc123"}}


def test_ohne_eingerichteten_channel_wird_nichts_angenommen(monkeypatch, umgebung):
    monkeypatch.setitem(bot.cfg.config, "bug_report_channel_id", "")
    status, j = _melden(monkeypatch, GUT)
    assert status == 503 and "noch nicht eingerichtet" in j["error"]
    status, j = _lauf(bot.api_bugreport_status(_anfrage(monkeypatch, "GET", "/api/bugreport/status")))
    assert j["data"]["aktiv"] is False


def test_meldung_mit_screenshot_wird_als_embed_gepostet(monkeypatch, umgebung):
    status, j = _melden(monkeypatch, dict(GUT, screenshot="data:image/png;base64," + PNG))
    assert status == 200 and j["data"]["gesendet"] is True
    (kw,) = umgebung.kanal.gesendet
    emb = kw["embed"]
    assert emb.title == "🐞 Karte lädt nicht" and "Karte leer" in emb.description
    felder = {f.name: f.value for f in emb.fields}
    assert "Tester" in felder["Von"] and felder["Bereich"] == "tools" and felder["Bildschirm"] == "412×900"
    assert emb.image.url == "attachment://screenshot.png" and len(kw["files"]) == 1
    assert kw["files"][0].filename == "screenshot.png"
    assert kw["allowed_mentions"].everyone is False and kw["allowed_mentions"].users is False   # keine Pings


def test_meldung_ohne_screenshot_und_mit_boesen_zeichen(monkeypatch, umgebung):
    status, _ = _melden(monkeypatch, dict(GUT, titel="@everyone Hilfe", beschreibung="x" * 5000))
    assert status == 200
    emb = umgebung.kanal.gesendet[0]["embed"]
    assert len(emb.description) == 1500 and umgebung.kanal.gesendet[0]["files"] == []
    assert emb.footer.text == "ohne Screenshot"


def test_eingaben_werden_geprueft(monkeypatch, umgebung):
    assert _melden(monkeypatch, dict(GUT, titel="ab"))[0] == 400
    assert _melden(monkeypatch, dict(GUT, beschreibung="kurz"))[0] == 400
    for kaputt in ("data:image/png;base64,AAAA",                      # kein PNG-Kopf
                   "data:text/html;base64," + PNG,                    # falscher Typ
                   "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (7 * 1024 * 1024)).decode()):
        status, j = _melden(monkeypatch, dict(GUT, screenshot=kaputt))
        assert status == 400 and "Screenshot" in j["error"]
    assert umgebung.kanal.gesendet == []


def test_spam_schutz_pro_konto(monkeypatch, umgebung):
    assert _melden(monkeypatch, GUT, discord_id="konto-1")[0] == 200
    status, j = _melden(monkeypatch, GUT, discord_id="konto-1")           # sofort nochmal: Abstand
    assert status == 429
    bot._DASH_RATE_LIMIT_LAST.clear()
    for _ in range(4):
        bot._DASH_RATE_LIMIT_LAST.clear()
        _melden(monkeypatch, GUT, discord_id="konto-1")
    bot._DASH_RATE_LIMIT_LAST.clear()
    status, j = _melden(monkeypatch, GUT, discord_id="konto-1")
    assert status == 429 and "in einer Stunde" in j["error"]
    assert len(umgebung.kanal.gesendet) == 5
    assert _melden(monkeypatch, GUT, discord_id="konto-2")[0] == 200         # anderes Konto ist nicht betroffen


def test_channel_muss_zum_betreiber_discord_gehoeren(monkeypatch, umgebung):
    monkeypatch.setitem(bot.cfg.config, "bug_report_channel_id", str(FREMD_KANAL_ID))   # gibt es dort nicht
    assert bot._bug_kanal() is None
    assert _melden(monkeypatch, GUT)[0] == 503


def test_admin_einstellung_nur_fuer_admins_und_nur_betreiber_channel(monkeypatch, umgebung):
    req = _anfrage(monkeypatch, "GET", "/api/admin/bugreport", admin=False)
    assert _lauf(bot.api_admin_bugreport_get(req))[0] == 403
    status, j = _lauf(bot.api_admin_bugreport_get(_anfrage(monkeypatch, "GET", "/api/admin/bugreport", admin=True)))
    assert status == 200 and j["data"]["bot_in_guild"] and j["data"]["channels"] == [
        {"id": str(KANAL_ID), "name": "bug-reports", "bot_darf": True}]
    # Fremden Channel ablehnen
    status, j = _lauf(bot.api_admin_bugreport_post(_anfrage(
        monkeypatch, "POST", "/api/admin/bugreport", {"channel_id": str(FREMD_KANAL_ID)}, admin=True)))
    assert status == 400 and "Betreiber-Discord" in j["error"]
    # Nicht-Admin darf nicht speichern
    assert _lauf(bot.api_admin_bugreport_post(_anfrage(
        monkeypatch, "POST", "/api/admin/bugreport", {"channel_id": str(KANAL_ID)}, admin=False)))[0] == 403
    # Admin speichert + Test
    monkeypatch.setitem(bot.cfg.config, "bug_report_channel_id", "")
    status, j = _lauf(bot.api_admin_bugreport_post(_anfrage(
        monkeypatch, "POST", "/api/admin/bugreport", {"channel_id": str(KANAL_ID), "test": True}, admin=True)))
    assert status == 200 and bot.cfg.config["bug_report_channel_id"] == str(KANAL_ID)
    assert umgebung.kanal.gesendet and "Test" in umgebung.kanal.gesendet[-1]["embed"].title
    # Ausschalten
    _lauf(bot.api_admin_bugreport_post(_anfrage(monkeypatch, "POST", "/api/admin/bugreport", {"channel_id": ""}, admin=True)))
    assert bot.cfg.config["bug_report_channel_id"] == ""


def test_kanal_ohne_rechte_wird_abgelehnt(monkeypatch, umgebung):
    umgebung.kanal._ok = False
    status, j = _lauf(bot.api_admin_bugreport_post(_anfrage(
        monkeypatch, "POST", "/api/admin/bugreport", {"channel_id": str(KANAL_ID)}, admin=True)))
    assert status == 409 and "Rechte" in j["error"]
