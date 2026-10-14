"""Feed-Sprache je Nitrado-Server (feed_sprache): alle Posts folgen der Server-Einstellung, nicht der
Dashboard- oder Discord-Sprache; Slash-Befehlsantworten bleiben bei der Sprache des Nutzers."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import bot  # noqa: E402
from test_airstrike import servers  # noqa: E402,F401 - Fixture für pytest

# Deutsche Wörter, die in einer englischen Meldung nicht stehen dürfen (und umgekehrt).
DEUTSCH = ("hat ", "wurde", "Spieler", "Standort", "Verbunden", "Getrennt", "gestorben", "Waffe", "Distanz",
           "Opfer", "Ursache", "Befehl", "Nahe", "Neustart", "Gerade ist", "Grund", "Dauer", "Ping bei")
ENGLISCH = (" has ", " was ", "Player Location", "Connect", "Weapon", "Distance", "Victim", "Cause", "Command")


def _text(embed):
    teile = [embed.title or "", embed.description or "", (embed.footer.text if embed.footer else "") or ""]
    for f in embed.fields:
        teile += [f.name or "", f.value or ""]
    return " | ".join(teile)


def _frei_von(text, woerter):
    return [w for w in woerter if w in text]


EVENTS = [
    {"type": "kill_pvp", "killer": "Ann", "victim": "Bob", "weapon": "AKM", "distance": "50",
     "killer_id": "k1", "victim_id": "v1", "position": "100, 200, 300"},
    {"type": "suicide", "player": "Ann", "player_id": "p1", "position": "100, 200, 300"},
    {"type": "kill_env", "player": "Ann", "player_id": "p1", "cause": "Zombie", "position": "100, 200, 300"},
    {"type": "damage", "attacker": "Ann", "victim": "Bob", "damage": "10", "hit_zone": "Head", "weapon": "AKM",
     "distance": "5", "position": "100, 200, 300"},
    {"type": "connect", "player": "Ann", "player_id": "p1", "position": "100, 200, 300"},
    {"type": "disconnect", "player": "Ann", "player_id": "p1", "position": "100, 200, 300"},
    {"type": "connecting", "player": "Ann", "player_id": "p1"},
    {"type": "admin_action", "admin": "Boss", "admin_id": "a1", "command": "kick"},
    {"type": "basebuild", "player": "Ann", "player_id": "p1", "item": "Wall", "position": "100, 200, 300"},
    {"type": "combat_log", "player": "Ann", "player_id": "p1", "sekunden": 5, "gegner": "Bob",
     "position": "100, 200, 300"},
    {"type": "rage_quit", "player": "Ann", "player_id": "p1", "sekunden": 5, "killer": "Bob",
     "position": "100, 200, 300"},
    {"type": "killstreak", "player": "Ann", "player_id": "p1", "streak": 3, "victim": "Bob",
     "position": "100, 200, 300"},
    {"type": "server_restart", "gestartet": "01.01.2026 10:00:00", "datei": "x.RPT"},
    {"type": "unconscious", "player": "Ann", "player_id": "p1", "position": "100, 200, 300"},
    {"type": "conscious", "player": "Ann", "player_id": "p1", "position": "100, 200, 300"},
]


@pytest.mark.parametrize("ev", EVENTS, ids=[e["type"] for e in EVENTS])
def test_embed_builder_nur_in_der_gewaehlten_sprache(ev):
    en = bot.EmbedBuilder.build(dict(ev), None, None, sprache="en")
    de = bot.EmbedBuilder.build(dict(ev), None, None, sprache="de")
    assert en is not None and de is not None
    assert _frei_von(_text(en), DEUTSCH) == [], _text(en)
    assert _frei_von(_text(de), ENGLISCH) == [], _text(de)
    assert _text(en) != _text(de)


def test_standortfeld_traegt_die_sprache_und_der_standort_schalter_trifft_beide():
    ev = dict(EVENTS[4])
    en = bot.EmbedBuilder.build(dict(ev), None, {"location": False}, sprache="en")
    de = bot.EmbedBuilder.build(dict(ev), None, {"location": False}, sprache="de")
    assert not [f for f in en.fields if f.name.startswith(bot._LOCATION_PRAEFIX)]     # Schalter "Standort aus"
    assert not [f for f in de.fields if f.name.startswith(bot._LOCATION_PRAEFIX)]
    en = bot.EmbedBuilder.build(dict(ev), None, None, sprache="en")
    de = bot.EmbedBuilder.build(dict(ev), None, None, sprache="de")
    assert any("Player Location" in f.name for f in en.fields) and any("Spieler-Standort" in f.name for f in de.fields)


def test_einstellung_je_server_getrennt_und_standard_deutsch(servers):
    a, b = servers
    assert bot._feed_sprache(a) == "de" and bot._feed_sprache(b) == "de"
    a.data["feed_sprache"] = "en"
    assert bot._feed_sprache(a) == "en" and bot._feed_sprache(b) == "de"
    assert bot._ft(a, "Hallo", "Hello") == "Hello" and bot._ft(b, "Hallo", "Hello") == "Hallo"
    a.data["feed_sprache"] = "fr"                       # ungültig → Standard
    assert bot._feed_sprache(a) == "de"
    bot.cfg.config["feed_sprache"] = "en"               # Betreiber-Config wirkt NICHT auf Kunden
    assert bot._feed_sprache(b) == "de"


def test_guild_sicht_nutzt_die_sprache_des_servers(servers):
    a, _ = servers
    a.data["feed_sprache"] = "en"
    bot.connections.add_guild("1000", 8101)
    assert bot._feed_sprache(bot.GuildSicht(a, 8101)) == "en"


def test_neustart_countdown_in_server_sprache_ohne_knopf(servers):
    a, b = servers
    a.data["feed_sprache"] = "en"
    emb_en = bot.bot._restart_countdown_bauen(a, 1_900_000_000, 30)
    emb_de = bot.bot._restart_countdown_bauen(b, 1_900_000_000, 30)
    assert "until the next restart" in emb_en.title and "Scheduled restart" in emb_en.description
    assert "bis zum nächsten Neustart" in emb_de.title and "Geplanter Neustart" in emb_de.description


def test_uebersetzen_knoepfe_sind_entfernt():
    assert not hasattr(bot, "_FeedTranslateView") and not hasattr(bot, "_SimpleTranslateView")


def test_poll_grund_und_hook_hinweise_uebersetzt(servers):
    a, b = servers
    a.data["feed_sprache"] = "en"
    grund = "keine .ADM-Dateien in /logs gefunden (ADM-Logging auf dem Server aktiviert? FTP-Pfad richtig?)"
    assert bot._poll_grund_anzeige(a, grund).startswith("no .ADM files found in /logs")
    assert bot._poll_grund_anzeige(b, grund) == grund
    assert "assigned to a Discord server" in bot._poll_grund_anzeige(
        a, "keinem Discord-Server zugeordnet (Zuordnung fehlt im Dashboard unter „Serverliste“)")
    hinweis = "Rolle „Gast“ konnte nicht vergeben werden (Rechte/Hierarchie). Nickname: boom"
    assert bot._hook_hinweis_anzeige(a, hinweis) == "Role “Gast” could not be assigned (permissions/hierarchy). Nickname: boom"
    assert bot._hook_hinweis_anzeige(b, hinweis) == hinweis
    assert bot._poll_grund_anzeige(a, "ganz anderer Text") == "ganz anderer Text"       # Unbekanntes bleibt


def test_online_liste_server_sprache_und_befehl_nutzersprache(servers):
    a, _ = servers
    a.data["feed_sprache"] = "en"
    post = bot._online_liste_embed(a, [], "", 0)
    assert "Online list" in post.title and "Nobody is online" in post.description
    befehl = bot._online_liste_embed(a, [], "", 0, "de")          # /online: Sprache des Nutzers
    assert "Online-Liste" in befehl.title and "niemand online" in befehl.description


def test_status_embed_und_aufgaben_label(servers):
    a, b = servers
    a.data["feed_sprache"] = "en"
    off_en = bot.bot._build_status_embed(None, a)
    off_de = bot.bot._build_status_embed(None, b)
    assert "No response to the A2S query" in off_en.description and "Keine Antwort" in off_de.description
    assert bot._aufgabe_label(a, "online_list") == "Post online list"
    assert bot._aufgabe_label(b, "online_list") == "Online-Liste posten"
    assert bot._aufgabe_label(a, "gibt_es_nicht") == "gibt_es_nicht"


def test_whitelist_embed_und_giveaway_knopf_folgen_der_sprache():
    en = bot._whitelist_request_embed(1, "PSN", "en")
    de = bot._whitelist_request_embed(1, "PSN", "de")
    assert "New whitelist request" in en.title and "Neue Whitelist-Anfrage" in de.title
    assert bot.GiveawayEntryView("1000", 1, "en").children[0].label == "Enter"
    assert bot.GiveawayEntryView("1000", 1).children[0].label == "Teilnehmen"
    assert bot.WhitelistApprovalView("abc", "en").children[0].label == "Accept"


def test_abandoned_gruende_zweisprachig(servers):
    for schluessel, (de, en) in bot._ABANDONED_GRUND_TEXT.items():
        assert de and en and de != en


def test_modul_sprachen_folgen_dem_server(servers):
    a, _ = servers
    a.data["feed_sprache"] = "en"
    a.data["level_system"] = {"enabled": True, "language": "de"}          # alte Modulwahl wird ignoriert
    a.data["honeypot"] = {"enabled": True, "language": "de", "channel_id": "5"}
    a.data["ticket_language"] = "de"
    assert bot._level_einstellungen(a)["language"] == "en"
    assert bot._honeypot_einstellungen(a)["language"] == "en"
    assert bot._ticket_sprache(a) == "en"
    assert bot._ticket_sprache(None) == "de"


def test_befehlsantworten_bleiben_bei_der_sprache_des_nutzers(servers):
    a, _ = servers
    a.data["feed_sprache"] = "en"
    inter = SimpleNamespace(locale=None)
    assert bot._t(inter, "Hallo", "Hello") == "Hallo"             # Discord-Sprache des Nutzers, nicht der Server


def test_zonen_ping_und_auto_ban_in_server_sprache(servers, monkeypatch):
    import asyncio
    a, b = servers
    gesendet = []

    async def _fake_post(gid, log_type, embed, *args, **kw):
        gesendet.append(embed)
        return True, "ok"
    monkeypatch.setattr(bot, "_post_feed", _fake_post)
    zone = {"name": "Basis", "type": "circle", "x": 1, "z": 1, "radius": 10, "guild_id": 8101, "channel_id": 9,
            "verbose": True}
    a.data["feed_sprache"] = "en"
    bot.connections.add_guild("1000", 8101)
    bot.connections.add_guild("2000", 8102)
    zone_b = dict(zone, guild_id=8102)
    info = {"position": "1, 2, 3"}
    ev = {"weapon": "AKM", "distance": "5", "raw": "zeile"}
    asyncio.run(bot.bot._post_zone_ping(zone, "Ann", info, a, ev=ev))
    asyncio.run(bot.bot._post_zone_ping(zone_b, "Ann", info, b, ev=ev))
    en, de = gesendet
    assert "was located within the zone" in en.description and "Ping On Detection" in en.title
    assert _frei_von(_text(en), ("Waffe", "Distanz", "Logzeile", "wurde in der Zone")) == []
    assert "wurde in der Zone" in de.description and "Waffe" in _text(de) and "Logzeile" in _text(de)
