"""Sprüche: einstellbare Zufallssätze je Feed (Vorbild Vigil Killfeed) und für die
Economy-Befehle /work, /daily, /beg.

Geprüft werden Katalog und Einsetzen, die Auswahl (eigene Zeilen vor Standard, Feed-
Sprache, nur_eigene), die Speicherung über set_feed samt Rechten und Mandanten-
Trennung, die Bot-Meldungen (Shop-Kauf, Miete, Glücksrad-Gewinn) und die Economy-
Befehle. Kein Test geht ins Internet oder zu Discord.

    python3 -m pytest tests/test_sprueche.py -q
"""
import asyncio
import os
import sys
import time

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_spieler_dashboard import G_A, G_B, U, _session, api, welt  # noqa: F401 - Fixtures
from test_airstrike import servers  # noqa: F401 - Fixture (zwei Server 1000/2000)
from test_gluecksrad import _Inter, _admin, _lauf, slash  # noqa: F401 - Slash-Doppel

EV_KILL = {"type": "kill_pvp", "killer": "Ozzie", "victim": "Marek", "weapon": "M4-A1",
           "distance": "182", "killer_id": "K1", "victim_id": "V1"}


@pytest.fixture(autouse=True)
def _frisch(monkeypatch):
    monkeypatch.setattr(bot, "_audit_log", bot.deque(maxlen=100))


@pytest.fixture
def feeds_cfg(monkeypatch):
    c = bot.ConfigManager()
    monkeypatch.setattr(c, "save_guilds", lambda: None)
    return c


# ── Katalog und Einsetzen ───────────────────────────────────────────────────
def test_katalog_nur_echte_feed_typen_und_erlaubte_platzhalter():
    for k, s in bot._SPRUECHE.items():
        assert k in bot.FEED_TYPES, k
        assert s["de"] and s["en"], k
        for sprache in ("de", "en"):
            for z in s[sprache]:
                assert len(z) <= bot._SPRUECHE_MAX_LAENGE, (k, z)
                for m in bot._SPRUCH_MUSTER_RE.finditer(z):
                    assert m.group(1) in s["platzhalter"], (k, z, m.group(1))
    for feed_key, unter in bot._SPRUECHE_BOT.items():
        assert feed_key in bot.FEED_TYPES
        for u, s in unter.items():
            for sprache in ("de", "en"):
                for z in s[sprache]:
                    for m in bot._SPRUCH_MUSTER_RE.finditer(z):
                        assert m.group(1) in s["platzhalter"], (feed_key, u, z)


def test_zeile_eins_ist_der_bisherige_satz():
    """Zeile 1 jedes Standard-Satzes entspricht dem bisherigen Satz des Bots (Kill als Stichprobe)."""
    assert bot._SPRUECHE["kill"]["de"][0] == "**{killer}** hat **{victim}** getötet"
    assert bot._SPRUECHE["kill"]["en"][0] == "**{killer}** killed **{victim}**"
    assert bot._SPRUECHE["connect"]["de"][0] == "**{player}** hat sich mit dem Gameserver verbunden."
    assert bot._SPRUECHE_BOT["shop_log"]["kauf"]["de"][0] == "{player} hat **{menge}× {item}** gekauft"


def test_einsetzen_unbekannte_platzhalter_und_klammern_in_werten():
    assert bot._spruch_einsetzen("{killer} -> {victim}", {"killer": "A", "victim": "B"}) == "A -> B"
    assert bot._spruch_einsetzen("{killer} {nix}", {"killer": "A"}) == "A {nix}"
    # Geschweifte Klammern im eingesetzten Wert werden NICHT erneut ersetzt
    assert bot._spruch_einsetzen("{player}: {message}", {"player": "{message}", "message": "hi"}) == "{message}: hi"
    assert bot._spruch_einsetzen("ohne Platzhalter", {}) == "ohne Platzhalter"


@pytest.mark.parametrize("roh,erwartet", [
    (None, None), ([], None), (["  "], None), (["A", "", "B"], ["A", "B"]),
    (["  viel   Platz  "], ["viel Platz"]), ("x", "fehler"), (5, "fehler"),
    ([5], "fehler"), ([None], "fehler"), (["a\x00b"], "fehler"),
    (["x" * 201], "fehler"), (["x"] * 21, "fehler"), (["x"] * 20, ["x"] * 20)])
def test_zeilen_pruefen(roh, erwartet):
    aus = bot._spruch_zeilen_pruefen(roh)
    if erwartet == "fehler":
        assert isinstance(aus, str)
    else:
        assert aus == erwartet


# ── Auswahl beim Posten ─────────────────────────────────────────────────────
def _embed(beschreibung="alt"):
    return discord.Embed(title="t", description=beschreibung)


def test_eigene_zeilen_gewinnen_und_standard_nach_feed_sprache(monkeypatch):
    e = _embed()
    bot._spruch_embed_anwenden(e, {"sprueche": ["Nur {killer} zählt ({distance})"]}, "kill", EV_KILL, "de")
    assert e.description == "Nur Ozzie zählt (182 m)"
    monkeypatch.setattr(bot.random, "choice", lambda liste: liste[0])
    for sprache, text in (("de", "**Ozzie** hat **Marek** getötet"), ("en", "**Ozzie** killed **Marek**")):
        e = _embed()
        bot._spruch_embed_anwenden(e, {}, "kill", EV_KILL, sprache)
        assert e.description == text
    # alle Standard-Zeilen sind erreichbar und vollständig gefüllt
    gesehen = set()
    monkeypatch.setattr(bot.random, "choice", lambda liste: liste[len(gesehen) % len(liste)])
    for _ in bot._SPRUECHE["kill"]["de"]:
        e = _embed()
        bot._spruch_embed_anwenden(e, None, "kill", EV_KILL, "de")
        assert "{" not in e.description and "Ozzie" in e.description or "Marek" in e.description
        gesehen.add(e.description)
    assert len(gesehen) == len(bot._SPRUECHE["kill"]["de"])


def test_unbekannter_feed_typ_und_kaputte_eigene_zeilen_aendern_nichts():
    for feed_key, feed in (("adm_download", {}), ("kill", {"sprueche": "kaputt"}),
                           ("kill", {"sprueche": [5]}), (None, {})):
        e = _embed()
        bot._spruch_embed_anwenden(e, feed, feed_key, EV_KILL, "de")
        if feed_key == "kill" and feed.get("sprueche") == "kaputt":
            continue        # String ist keine Liste -> Standard greift, Beschreibung wechselt
    e = _embed()
    bot._spruch_embed_anwenden(e, {"sprueche": [5]}, "kill", EV_KILL, "de")
    assert e.description != "alt"            # kaputte Liste -> Standard statt Absturz
    e = _embed()
    bot._spruch_embed_anwenden(e, {}, "adm_download", {"type": "x"}, "de")
    assert e.description == "alt"


def test_nur_eigene_emote_laesst_standard_unangetastet():
    ev = {"type": "emote", "player": "Ozzie", "emote": "wave"}
    e = _embed("**Ozzie** hat **Winken** gespielt (mit Gitarre)")
    bot._spruch_embed_anwenden(e, {}, "emote", ev, "de")
    assert "(mit Gitarre)" in e.description
    bot._spruch_embed_anwenden(e, {"sprueche": ["{player} tanzt: {emote}"]}, "emote", ev, "de")
    assert e.description == "Ozzie tanzt: " + bot._emote_label("wave")


def test_build_verben_bekommen_das_passende_verb():
    ev = {"type": "basebuild", "player": "Ozzie", "item": "Watchtower", "aktion": "dismantled"}
    e = _embed()
    bot._spruch_embed_anwenden(e, {}, "dismantle", ev, "de")
    assert e.description == "**Ozzie** hat **Watchtower** abgebaut"


def test_werte_fuer_jeden_ereignistyp_mit_fehlenden_feldern():
    """Fehlende Ereignis-Felder ergeben '?', nie eine Ausnahme."""
    for t in ("kill_pvp", "suicide", "kill_env", "damage", "connect", "disconnect", "connecting",
              "unconscious", "conscious", "emote", "basebuild", "combat_log", "rage_quit",
              "killstreak", "admin_action"):
        w = bot._spruch_werte({"type": t}, "de")
        assert isinstance(w, dict) and all(isinstance(v, str) for v in w.values()), t
    assert bot._spruch_werte({"type": "chat"}, "de") is None
    assert bot._spruch_werte({}, "de") is None


def test_dispatch_nutzt_die_zeilen_des_jeweiligen_feeds(feeds_cfg, monkeypatch):
    """Zwei Guilds, derselbe Kill: jede bekommt ihre eigenen Sprüche (bzw. Standard)."""
    monkeypatch.setattr(bot.random, "choice", lambda liste: liste[0])
    feeds_cfg.server_feeds(111, "1000", anlegen=True)["kill"] = {
        "channel_id": 1, "sprueche": ["Eigener Satz: {killer} vs {victim}"]}
    feeds_cfg.server_feeds(222, "1000", anlegen=True)["kill"] = {"channel_id": 2}
    basis = bot.EmbedBuilder.build(dict(EV_KILL), sprache="de")
    aus = {}
    for gid in (111, 222):
        feed = feeds_cfg.feed_settings(gid, "kill", "1000")
        e = basis.copy()
        bot._spruch_embed_anwenden(e, feed, "kill", EV_KILL, "de")
        aus[gid] = e.description
    assert aus[111] == "Eigener Satz: Ozzie vs Marek"
    assert aus[222] == "**Ozzie** hat **Marek** getötet"
    assert basis.description == "**Ozzie** hat **Marek** getötet"        # Original unverändert
    # Titel und Felder bleiben in beiden Fällen unangetastet
    assert basis.title == "☠️ KILL" and len(basis.fields) >= 2


# ── Speichern über das Dashboard (set_feed) ────────────────────────────────
def _kanal_ok(monkeypatch):
    monkeypatch.setattr(bot, "_kanal_gehoert_guild", lambda gid, kanal, feld="Channel": None)


def _set_feed(welt, log_type, daten, gid=G_A, conn="a"):
    sid = _admin(welt, gid=gid, conn=conn, admin=False)        # Besitzer des Servers: sieht genau seine Guild
    return api("POST", f"/api/feeds/{gid}/{log_type}", sid, daten)


def test_set_feed_speichert_und_leert_sprueche(welt, monkeypatch):
    _kanal_ok(monkeypatch)
    st, d = _set_feed(welt, "kill", {"channel_id": "123", "sprueche": ["{killer} gegen {victim}", "  ", "Zweite  Zeile"]})
    assert st == 200, d
    s = bot.cfg.feed_settings(G_A, "kill", "1000")
    assert s["sprueche"] == ["{killer} gegen {victim}", "Zweite Zeile"]
    # Speichern OHNE das Feld lässt die Sprüche stehen (gleiche Regel wie enabled)
    assert _set_feed(welt, "kill", {"channel_id": "123", "note": "x"})[0] == 200
    assert bot.cfg.feed_settings(G_A, "kill", "1000")["sprueche"] == ["{killer} gegen {victim}", "Zweite Zeile"]
    # Der reine Ein/Aus-Schalter fasst sie ebenfalls nicht an
    assert _set_feed(welt, "kill", {"enabled": False})[0] == 200
    assert bot.cfg.feed_settings(G_A, "kill", "1000")["sprueche"] == ["{killer} gegen {victim}", "Zweite Zeile"]
    # sprueche: null (oder leere Liste) = zurück auf Standard
    assert _set_feed(welt, "kill", {"channel_id": "123", "sprueche": None})[0] == 200
    assert bot.cfg.feed_settings(G_A, "kill", "1000")["sprueche"] is None


@pytest.mark.parametrize("sprueche", ["x", 5, {"kauf": ["x"]}, [5], ["a\x00b"], ["x" * 201], ["x"] * 21])
def test_set_feed_lehnt_kaputte_sprueche_ab(welt, monkeypatch, sprueche):
    _kanal_ok(monkeypatch)
    st, _d = _set_feed(welt, "kill", {"channel_id": "123", "sprueche": sprueche})
    assert st == 400
    assert not bot.cfg.feed_settings(G_A, "kill", "1000")


def test_set_feed_bot_feed_je_meldungsart(welt, monkeypatch):
    _kanal_ok(monkeypatch)
    st, d = _set_feed(welt, "shop_log", {"channel_id": "9", "sprueche": {"kauf": ["{player} kauft {menge}× {item}"], "miete": None}})
    assert st == 200, d
    assert bot.cfg.feed_settings(G_A, "shop_log", "1000")["sprueche"] == {"kauf": ["{player} kauft {menge}× {item}"]}
    assert _set_feed(welt, "shop_log", {"channel_id": "9", "sprueche": {"unbekannt": ["x"]}})[0] == 400
    assert _set_feed(welt, "shop_log", {"channel_id": "9", "sprueche": ["flache Liste"]})[0] == 400
    # Feed ohne Sprüche lehnt das Feld ab
    assert _set_feed(welt, "adm_download", {"channel_id": "9", "sprueche": ["x"]})[0] == 400


def test_set_feed_sprueche_trennen_server_und_guild(welt, monkeypatch):
    _kanal_ok(monkeypatch)
    assert _set_feed(welt, "kill", {"channel_id": "1", "sprueche": ["A-Zeile {killer}"]})[0] == 200
    assert _set_feed(welt, "kill", {"channel_id": "2", "sprueche": ["B-Zeile {killer}"]}, gid=G_B, conn="b")[0] == 200
    assert bot.cfg.feed_settings(G_A, "kill", "1000")["sprueche"] == ["A-Zeile {killer}"]
    assert bot.cfg.feed_settings(G_B, "kill", "2000")["sprueche"] == ["B-Zeile {killer}"]
    assert bot.cfg.feed_settings(G_A, "kill", "2000") is None


def test_set_feed_ohne_rechte(welt):
    assert api("POST", f"/api/feeds/{G_A}/kill", None, {"channel_id": "1", "sprueche": ["x"]})[0] in (401, 403)
    s = _session(gid=G_A, sid_="1000")
    assert api("POST", f"/api/feeds/{G_A}/kill", s, {"channel_id": "1", "sprueche": ["x"]})[0] in (401, 403)


def test_get_feeds_liefert_definitionen_und_gespeicherte_zeilen(welt, monkeypatch):
    _kanal_ok(monkeypatch)
    _set_feed(welt, "kill", {"channel_id": "123", "sprueche": ["Meine {killer}-Zeile"]})
    sid = _admin(welt, admin=False)
    st, d = api("GET", "/api/feeds", sid)
    assert st == 200
    typen = {t["key"]: t for t in d["data"]["feed_types"]}
    assert typen["kill"]["sprueche_def"]["platzhalter"] == ["killer", "victim", "weapon", "distance"]
    assert typen["kill"]["sprueche_def"]["de"][0] == "**{killer}** hat **{victim}** getötet"
    assert "unter" in typen["shop_log"]["sprueche_def"] and "kauf" in typen["shop_log"]["sprueche_def"]["unter"]
    assert "sprueche_def" not in typen["adm_download"]
    assert d["data"]["sprueche_limits"] == {"zeilen": 20, "laenge": 200}
    assert d["data"]["feed_sprache"] in ("de", "en")
    feeds = {f["key"]: f for f in d["data"]["guilds"][0]["feeds"]}
    assert feeds["kill"]["sprueche"] == ["Meine {killer}-Zeile"]


# ── Bot-Meldungen: Shop-Kauf, Miete, Glücksrad-Gewinn ──────────────────────
def test_bot_zeile_standard_und_eigene(welt, monkeypatch):
    monkeypatch.setattr(bot.random, "choice", lambda liste: liste[0])
    werte = {"player": "<@20>", "menge": "2", "item": "Holzkiste"}
    assert bot._spruch_bot_zeile(G_A, welt["a"], "shop_log", "kauf", werte) == "<@20> hat **2× Holzkiste** gekauft"
    bot.cfg.server_feeds(G_A, "1000", anlegen=True)["shop_log"] = {
        "channel_id": 5, "sprueche": {"kauf": ["{player} schleppt {menge}× {item} nach Hause"]}}
    assert bot._spruch_bot_zeile(G_A, welt["a"], "shop_log", "kauf", werte) == "<@20> schleppt 2× Holzkiste nach Hause"
    # Miete hat keine eigenen Zeilen -> Standard; Feed-Sprache en
    welt["a"].data["feed_sprache"] = "en"
    assert bot._spruch_bot_zeile(G_A, welt["a"], "shop_log", "miete",
                                 {"player": "<@20>", "item": "Olga", "neustarts": "3"}) == "<@20> rented **Olga** for **3 restart(s)**"
    # Ein anderer Server derselben Guild sieht die eigenen Zeilen nicht
    assert bot._spruch_bot_zeile(G_A, welt["b"], "shop_log", "kauf", werte).endswith("gekauft")


def test_rad_gewinn_feed_nutzt_eigene_zeile(welt, monkeypatch):
    monkeypatch.setattr(bot.random, "choice", lambda liste: liste[-1])
    bot.cfg.server_feeds(G_A, "1000", anlegen=True)["economy_log"] = {
        "channel_id": 5, "sprueche": {"rad": ["{player} räumt ab: {gewinn} ({inhalt})"]}}
    sicht = bot.guild_sicht(welt["a"], G_A)
    bot._conn_store(sicht, "gluecksrad", {"enabled": True, "kosten": 0, "gratis_stunden": 24, "spielzeit_stunden": 0,
                                          "max_gespeichert": 10, "preise": [
                                              {"id": "aaaaaaa1", "typ": "geld", "name": "Jackpot", "gewicht": 1, "betrag": 500}]})
    bot.db.set_wallet(G_A, U, 0)
    erg = asyncio.run(bot._rad_drehen(welt["a"], sicht, welt["guilds"][G_A], G_A, U, "User", "gratis"))
    assert erg["ok"], erg
    feeds = [f for f in welt["feeds"] if f[1] == "economy_log"]
    assert feeds, welt["feeds"]


# ── Economy-Befehle /work, /daily, /beg ────────────────────────────────────
def test_economy_spruch_eigene_und_standard(monkeypatch):
    monkeypatch.setattr(bot.random, "choice", lambda liste: liste[0])
    assert bot._economy_spruch({}, "zeilen", bot.WORK_FLAVOR, "**250**") == bot.WORK_FLAVOR[0].format(amount="**250**")
    conf = {"zeilen": ["Du hast {amount} verdient – {unbekannt} bleibt stehen"]}
    assert bot._economy_spruch(conf, "zeilen", bot.WORK_FLAVOR, "**250**") == "Du hast **250** verdient – {unbekannt} bleibt stehen"
    assert bot._economy_spruch({"zeilen": "kaputt"}, "zeilen", ["Std {amount}"], "X") == "Std X"
    assert bot._economy_spruch({"zeilen": [5]}, "zeilen", ["Std {amount}"], "X") == "Std X"


def test_slash_work_und_beg_nutzen_eigene_zeilen(slash, monkeypatch):
    monkeypatch.setattr(bot.random, "choice", lambda liste: liste[0])
    monkeypatch.setattr(bot.random, "randint", lambda a, b: 100)
    monkeypatch.setattr(bot.random, "random", lambda: 1.0)        # beg schlaegt nie fehl
    slash["a"].data["economy"] = {"work": {"zeilen": ["Schicht fertig: {amount}"]},
                                  "beg": {"zeilen": ["Jemand gab dir {amount}"], "fehl_zeilen": ["Nichts."]}}
    text = _lauf(bot.cmd_work, _Inter(slash, locale=discord.Locale.german))
    assert "Schicht fertig: **" in text and "100" in text
    text = _lauf(bot.cmd_beg, _Inter(slash, uid=31, locale=discord.Locale.german))
    assert "Jemand gab dir **" in text
    monkeypatch.setattr(bot.random, "random", lambda: 0.0)        # beg schlaegt immer fehl
    text = _lauf(bot.cmd_beg, _Inter(slash, uid=32, locale=discord.Locale.german))
    assert "Nichts." in text


def test_slash_daily_standard_und_eigene(slash, monkeypatch):
    monkeypatch.setattr(bot.random, "choice", lambda liste: liste[0])
    text = _lauf(bot.cmd_daily, _Inter(slash, locale=discord.Locale.german))
    assert "Tagesbonus" in text and "abgeholt" in text
    slash["a"].data["economy"] = {"daily": {"zeilen": ["Bonus kassiert: {amount}!"]}}
    text = _lauf(bot.cmd_daily, _Inter(slash, uid=33, locale=discord.Locale.american_english))
    assert "Bonus kassiert: **" in text


def test_api_economy_config_sprueche(welt):
    sid = _admin(welt)
    st, d = api("POST", "/api/economy/config", sid, {"economy": {"work": {"zeilen": ["Zeile {amount}", "  "]}}})
    assert st == 200, d
    assert welt["a"].data["economy"]["work"]["zeilen"] == ["Zeile {amount}"]
    # Zahlenfelder bleiben beim Sprüche-Speichern erhalten und umgekehrt
    assert api("POST", "/api/economy/config", sid, {"economy": {"work": {"min": 10, "max": 20}}})[0] == 200
    assert welt["a"].data["economy"]["work"]["zeilen"] == ["Zeile {amount}"]
    assert api("POST", "/api/economy/config", sid, {"economy": {"beg": {"fehl_zeilen": ["Pech gehabt"]}}})[0] == 200
    assert welt["a"].data["economy"]["beg"]["fehl_zeilen"] == ["Pech gehabt"]
    # leer/None = zurück auf Standard
    assert api("POST", "/api/economy/config", sid, {"economy": {"work": {"zeilen": None}}})[0] == 200
    assert "zeilen" not in welt["a"].data["economy"]["work"]
    # kaputte Zeilen werden abgelehnt, nichts gespeichert
    st, d = api("POST", "/api/economy/config", sid, {"economy": {"work": {"zeilen": ["x" * 300]}}})
    assert st == 400
    st, d = api("GET", "/api/economy/config", sid)
    assert st == 200 and d["data"]["sprueche_standard"]["work"] == bot.WORK_FLAVOR
    assert d["data"]["sprueche_standard"]["beg_fehl"] == bot.BEG_FAIL


def test_api_economy_config_sprueche_mandanten_getrennt(welt):
    sid_a, sid_b = _admin(welt), _admin(welt, gid=G_B, conn="b")
    assert api("POST", "/api/economy/config", sid_a, {"economy": {"work": {"zeilen": ["A"]}}})[0] == 200
    assert api("POST", "/api/economy/config", sid_b, {"economy": {"work": {"zeilen": ["B"]}}})[0] == 200
    assert welt["a"].data["economy"]["work"]["zeilen"] == ["A"]
    assert welt["b"].data["economy"]["work"]["zeilen"] == ["B"]


def test_echter_dispatch_postet_den_spruch_je_guild(monkeypatch):
    """Der echte Versandweg (LogDispatcher._dispatch): zwei Guilds desselben Servers bekommen
    denselben Kill mit ihren jeweiligen Zeilen - Felder und Titel bleiben erhalten."""
    import tempfile
    monkeypatch.setattr(bot, "db", bot.EconomyDB(os.path.join(tempfile.mkdtemp(), "d.db")))
    monkeypatch.setattr(bot.cfg, "guilds", {})
    monkeypatch.setattr(bot.cfg, "save_guilds", lambda: None)
    monkeypatch.setattr(bot.random, "choice", lambda liste: liste[0])
    bot.cfg.server_feeds(111, "1000", anlegen=True)["kill"] = {"channel_id": 71, "sprueche": ["{killer} zerlegt {victim}"]}
    bot.cfg.server_feeds(222, "1000", anlegen=True)["kill"] = {"channel_id": 72}

    class _Ch:
        def __init__(self):
            self.gesendet = []

        async def send(self, embed=None, **kw):
            self.gesendet.append(embed)
    kanaele = {71: _Ch(), 72: _Ch()}

    class _Conn:
        service_id, guild_ids, name, parser = "1000", [111, 222], "Test", None

        def __init__(self):
            self.dispatch_verlauf = []

        def get(self, key, default=None):
            return {"feed_sprache": "de"}.get(key, default)
    monkeypatch.setattr(bot.bot, "_resolve_channel", lambda ch_id: _als_coro(kanaele.get(int(ch_id))))
    ev = dict(EV_KILL, position=None, raw="", timestamp="00:00:00")
    asyncio.run(bot.bot._dispatch(ev, _Conn(), nebenwirkungen=False))
    assert len(kanaele[71].gesendet) == 1 and len(kanaele[72].gesendet) == 1
    assert kanaele[71].gesendet[0].description == "Ozzie zerlegt Marek"
    assert kanaele[72].gesendet[0].description == "**Ozzie** hat **Marek** getötet"
    for e in (kanaele[71].gesendet[0], kanaele[72].gesendet[0]):
        assert e.title == "☠️ KILL" and any(f.name in ("Waffe", "Weapon") for f in e.fields)


async def _als_coro(wert):
    return wert


# ── Befunde der unabhängigen Prüfung ────────────────────────────────────────
def test_set_feed_nur_schalter_mit_sprueche_speichert_sie_mit(welt, monkeypatch):
    """Früher: enabled+sprueche ohne channel_id verwarf die Sprüche stillschweigend."""
    _kanal_ok(monkeypatch)
    assert _set_feed(welt, "kill", {"channel_id": "123"})[0] == 200
    assert _set_feed(welt, "kill", {"enabled": True, "sprueche": ["Nur {killer}"]})[0] == 200
    assert bot.cfg.feed_settings(G_A, "kill", "1000")["sprueche"] == ["Nur {killer}"]
    assert _set_feed(welt, "kill", {"enabled": False, "sprueche": None})[0] == 200
    s = bot.cfg.feed_settings(G_A, "kill", "1000")
    assert s["sprueche"] is None and s["enabled"] is False
    assert _set_feed(welt, "kill", {"enabled": True, "sprueche": ["x" * 300]})[0] == 400


def test_beschreibung_wird_auf_discord_grenze_gekuerzt(monkeypatch):
    monkeypatch.setattr(bot.random, "choice", lambda liste: liste[0])
    ev = dict(EV_KILL, killer="K" * 250)
    zeile = "{killer} " * 20 + "tötet {victim}"            # unter 200 Zeichen Vorlage, Werte vervielfacht
    assert len(zeile.rstrip()) <= 200
    e = _embed()
    bot._spruch_embed_anwenden(e, {"sprueche": [zeile]}, "kill", ev, "de")
    assert len(e.description) == 4096
    feed = {"channel_id": 5, "sprueche": {"kauf": [zeile.replace("{victim}", "{item}")]}}
    bot.cfg.server_feeds(777, "1000", anlegen=True)["shop_log"] = feed

    class _Conn:
        service_id = "1000"

        def get(self, key, default=None):
            return default
    text = bot._spruch_bot_zeile(777, _Conn(), "shop_log", "kauf", {"player": "P" * 300, "item": "Kiste", "menge": "1"})
    assert len(text) <= 4096


def test_kill_standardzeile_zwei_liest_sich_auch_im_nahkampf(monkeypatch):
    monkeypatch.setattr(bot.random, "choice", lambda liste: liste[1])
    e = _embed()
    bot._spruch_embed_anwenden(e, {}, "kill", dict(EV_KILL, distance="?"), "de")
    assert e.description == "**Ozzie** erwischt **Marek** mit der **M4-A1** (**Nahkampf**)"
    e = _embed()
    bot._spruch_embed_anwenden(e, {}, "kill", EV_KILL, "en")
    assert e.description == "**Ozzie** got **Marek** with a **M4-A1** (**182 m**)"
