"""Server → Logging: Ban-Meldungen (zeitlich / permanent / Entbannung) in frei
gewaehlte Channels – Auswahl des Channels, Inhalt, alle Quellen, Validierung
und Kopieren der General-Karte „logging“.

    python3 -m pytest tests/test_logging_channels.py -q
"""
import asyncio
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeApi:
    """Nitrado-Stub wie in test_zonen_editor: Banliste in den Settings."""
    def __init__(self, bans=""):
        self.settings = {"general": {"bans": bans}}
        self.written = []

    async def get_settings(self):
        return self.settings

    async def set_setting(self, cat, key, val):
        self.settings[cat][key] = val
        self.written.append(val)
        return True, "ok"

    @property
    def bans(self):
        return self.settings["general"]["bans"]


def _conn(sid="1000", gid=111, **data):
    d = {"service_id": sid, "guild_id": gid, "map_name": "ChernarusPlus"}
    d.update(data)
    c = bot.ServerConnection(d)
    c.api = FakeApi()
    return c


@pytest.fixture(autouse=True)
def posts(monkeypatch):
    monkeypatch.setattr(bot.cfg, "save_bans", lambda: None)
    monkeypatch.setattr(bot.cfg, "bans", {})
    monkeypatch.setattr(bot, "_audit_add", lambda *a, **k: None)
    gesendet = []

    async def fake_post_feed(gid, typ, embed, content=None, channel_id=None, service_id=None, **k):
        gesendet.append({"gid": gid, "typ": typ, "embed": embed, "channel_id": channel_id,
                         "service_id": service_id})
        return True, "sent"
    monkeypatch.setattr(bot, "_post_feed", fake_post_feed)
    yield gesendet


def _feld(embed, name):
    return next((f.value for f in embed.fields if f.name == name), None)


def test_log_kanal_je_art(posts):
    c = _conn(log_ban_temp_channel_id="11", log_ban_perm_channel_id="22", log_unban_channel_id="33")
    assert _run(bot._ban_log_senden(c, "temp", ["Max"], "Bauen in Zone", "Zone Basis", expires_at=time.time() + 3600,
                                    zone="Basis", ereignis="basebuild"))
    assert _run(bot._ban_log_senden(c, "perm", ["Moe"], "Cheater", "Admin#1"))
    assert _run(bot._ban_log_senden(c, "unban", ["Max"], "", "Zeitlicher Ban abgelaufen"))
    assert [p["channel_id"] for p in posts] == [11, 22, 33]
    assert all(p["gid"] == 111 and p["service_id"] == "1000" for p in posts)
    e = posts[0]["embed"]
    assert e.title.endswith("Zeitlicher Ban") and "`Max`" in _feld(e, "Spieler")
    assert _feld(e, "Grund") == "Bauen in Zone" and _feld(e, "Zone") == "Basis"
    assert _feld(e, "Ereignis") == "basebuild" and _feld(e, "Endet").startswith("<t:")
    assert posts[1]["embed"].title.endswith("Permanenter Ban") and _feld(posts[1]["embed"], "Von") == "Admin#1"
    assert posts[2]["embed"].title.endswith("Entbannt")


def test_ohne_kanal_keine_meldung(posts):
    c = _conn(log_ban_perm_channel_id="22")           # nur permanent gesetzt
    assert not _run(bot._ban_log_senden(c, "temp", ["Max"], "x", "y", expires_at=time.time() + 60))
    assert not _run(bot._ban_log_senden(c, "unban", ["Max"], "", "y"))
    assert posts == []
    # kaputter Channel-Wert darf den Aufrufer nicht stoppen
    c2 = _conn(log_ban_perm_channel_id="abc")
    assert not _run(bot._ban_log_senden(c2, "perm", ["Max"], "x", "y"))


def test_ban_kern_meldet_alle_quellen(posts):
    c = _conn(log_ban_temp_channel_id="11", log_ban_perm_channel_id="22", log_unban_channel_id="33")
    hinzu, schon, fehler = _run(bot._ban_namen_hinzufuegen(c, ["Max", "Moe"], "Cheater", "Admin#1"))
    assert fehler is None and hinzu == ["Max", "Moe"]
    assert posts[-1]["channel_id"] == 22 and "`Moe`" in _feld(posts[-1]["embed"], "Spieler")
    # zeitlich (Zonen-Auto-Ban) -> temp-Channel
    hinzu, _schon, fehler = _run(bot._ban_namen_hinzufuegen(
        c, ["Tom"], "Bauen in Zone", "Zone Basis", expires_at=time.time() + 7200, zone="Basis", ereignis="basebuild"))
    assert fehler is None and posts[-1]["channel_id"] == 11 and _feld(posts[-1]["embed"], "Zone") == "Basis"
    # bereits gebannt -> keine zweite Meldung
    n = len(posts)
    _run(bot._ban_namen_hinzufuegen(c, ["Max"], "nochmal", "Admin#1"))
    assert len(posts) == n
    # Entbannung mit Quelle
    entfernt, _nf, fehler = _run(bot._ban_namen_entfernen(c, ["Max"], von="Admin#2", grund="Einspruch"))
    assert fehler is None and entfernt == ["Max"]
    assert posts[-1]["channel_id"] == 33 and _feld(posts[-1]["embed"], "Von") == "Admin#2"
    assert _feld(posts[-1]["embed"], "Grund") == "Einspruch"


def test_ban_kern_ohne_kanal_unveraendert(posts):
    c = _conn()
    hinzu, _schon, fehler = _run(bot._ban_namen_hinzufuegen(c, ["Max"], "Cheater", "Admin#1"))
    assert fehler is None and hinzu == ["Max"] and posts == []
    assert "Max" in c.api.bans


def test_general_validierung_und_kopieren(monkeypatch):
    pruefen = bot._general_wert_pruefen
    assert pruefen("log_ban_temp_channel_id", "123456789") == ("123456789", None)
    assert pruefen("log_ban_temp_channel_id", 987) == ("987", None)
    assert pruefen("log_ban_perm_channel_id", "") == ("", None)
    assert pruefen("log_unban_channel_id", None) == ("", None)
    assert pruefen("log_unban_channel_id", "kanal")[1]
    assert set(bot._GENERAL_KARTEN_SCHLUESSEL["logging"]) <= bot._GENERAL_GUILD_GEBUNDEN
    assert all(k in bot.ServerConnection._KEINE_RUECKFALL_SCHLUESSEL for k in bot._GENERAL_KARTEN_SCHLUESSEL["logging"])
    # ohne eigenen Wert kein Rueckfall auf cfg.config
    monkeypatch.setitem(bot.cfg.config, "log_ban_perm_channel_id", "999")
    assert _conn().get("log_ban_perm_channel_id") in (None, "")
