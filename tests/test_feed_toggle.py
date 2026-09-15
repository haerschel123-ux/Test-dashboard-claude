"""Tests fuer den Feed-Ein/Aus-Schalter (enabled), der Kanal/Farbe/Notiz
unangetastet laesst - siehe ConfigManager.feed_settings/get_channel und die
Rueckfallketten in _dispatch/_post_feed.

Kernidee: ein deaktivierter Feed liefert bei feed_settings() weiterhin sein
volles Dict (mit enabled=False), bei get_channel() dagegen None - und in den
beiden Rueckfallketten bricht die Kandidatensuche beim ersten Treffer ab,
statt bei "deaktiviert" auf den naechsten Kandidaten (z. B. catch_all)
auszuweichen.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_feed_toggle.py -v
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cfg(monkeypatch):
    c = bot.ConfigManager()
    monkeypatch.setattr(c, "save_guilds", lambda: None)
    return c


GID = 111
SID = "1000"


# ── feed_settings() / get_channel(): Kernverhalten des Schalters ─────────
def test_feed_settings_legacy_ohne_enabled_gilt_als_aktiv(cfg):
    cfg.server_feeds(GID, SID, anlegen=True)["kill"] = {"channel_id": 555, "colour": 100}
    s = cfg.feed_settings(GID, "kill", SID)
    assert s is not None
    assert s["enabled"] is True


def test_feed_settings_bare_channel_id_gilt_als_aktiv(cfg):
    # Bot-Feeds koennen historisch als blosse Zahl gespeichert sein.
    cfg.server_feeds(GID, SID, anlegen=True)["shop_log"] = 777
    s = cfg.feed_settings(GID, "shop_log", SID)
    assert s["enabled"] is True
    assert s["channel_id"] == 777


def test_feed_settings_zeigt_deaktivierten_feed_weiterhin_mit_channel(cfg):
    cfg.server_feeds(GID, SID, anlegen=True)["kill"] = {"channel_id": 555, "enabled": False}
    s = cfg.feed_settings(GID, "kill", SID)
    assert s is not None
    assert s["enabled"] is False
    assert s["channel_id"] == 555


def test_get_channel_liefert_none_fuer_deaktivierten_feed(cfg):
    cfg.server_feeds(GID, SID, anlegen=True)["kill"] = {"channel_id": 555, "enabled": False}
    assert cfg.get_channel(GID, "kill", SID) is None


def test_get_channel_liefert_channel_wenn_aktiv(cfg):
    cfg.server_feeds(GID, SID, anlegen=True)["kill"] = {"channel_id": 555, "enabled": True}
    assert cfg.get_channel(GID, "kill", SID) == 555


def test_feed_settings_ohne_channel_bleibt_none_auch_ohne_enabled_key(cfg):
    assert cfg.feed_settings(GID, "kill", SID) is None
    assert cfg.get_channel(GID, "kill", SID) is None


# ── _post_feed(): kein Ausweichen auf catch_all bei deaktiviertem Feed ───
@pytest.fixture
def fake_resolve(monkeypatch):
    """Ersetzt bot._resolve_channel durch einen Stub, der Zielkanaele als
    einfache Recorder-Objekte liefert - kein echter Discord-Login noetig."""
    gesendet = []

    class _FakeChannel:
        def __init__(self, cid):
            self.id = cid

        async def send(self, **kwargs):
            gesendet.append((self.id, kwargs))

    async def _resolve(cid):
        return _FakeChannel(cid)
    monkeypatch.setattr(bot.bot, "_resolve_channel", _resolve)
    return gesendet


def test_post_feed_disabled_postet_nicht_und_weicht_nicht_auf_catch_all_aus(
        monkeypatch, cfg, fake_resolve):
    monkeypatch.setattr(bot, "cfg", cfg)
    cfg.server_feeds(GID, SID, anlegen=True)["shop_log"] = {"channel_id": 111, "enabled": False}
    cfg.server_feeds(GID, SID, anlegen=True)["catch_all"] = {"channel_id": 999, "enabled": True}
    embed = bot.discord.Embed(title="Test")
    ok, grund = _run(bot._post_feed(GID, "shop_log", embed, service_id=SID))
    assert ok is False
    assert grund == "feed_disabled"
    assert fake_resolve == []  # nichts wurde je an einen Kanal gesendet


def test_post_feed_ohne_konfiguration_weicht_weiterhin_auf_catch_all_aus(
        monkeypatch, cfg, fake_resolve):
    monkeypatch.setattr(bot, "cfg", cfg)
    cfg.server_feeds(GID, SID, anlegen=True)["catch_all"] = {"channel_id": 999, "enabled": True}
    embed = bot.discord.Embed(title="Test")
    ok, grund = _run(bot._post_feed(GID, "shop_log", embed, service_id=SID))
    assert ok is True
    assert grund == "sent"
    assert fake_resolve and fake_resolve[0][0] == 999


def test_post_feed_aktiver_feed_postet_normal(monkeypatch, cfg, fake_resolve):
    monkeypatch.setattr(bot, "cfg", cfg)
    cfg.server_feeds(GID, SID, anlegen=True)["shop_log"] = {"channel_id": 111, "enabled": True}
    embed = bot.discord.Embed(title="Test")
    ok, grund = _run(bot._post_feed(GID, "shop_log", embed, service_id=SID))
    assert ok is True
    assert grund == "sent"
    assert fake_resolve and fake_resolve[0][0] == 111
