"""Tests fuer das Gewinnspiel-Feature (/gcreate, /gstart, /glist, /gdelete,
/greroll, /gsettings).

Kein echter Discord-Login noetig, siehe test_ticket_tool.py/
test_ticket_stale_lock.py fuer das etablierte Stub-Muster (Fake-Interaction,
Fake-Channel/-Message statt eines echten Discord-Gateways).

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_giveaways.py -v
"""
import asyncio
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot_mod = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")
discord = bot_mod.discord


def _run(coro):
    return asyncio.run(coro)


# ── Dauer-Parser (reine Funktion) ─────────────────────────────────────────

def test_dauer_parsen_minuten():
    assert bot_mod._dauer_parsen("10 minutes") == 600
    assert bot_mod._dauer_parsen("10 Minuten") == 600


def test_dauer_parsen_kombiniert():
    assert bot_mod._dauer_parsen("1h30m") == 5400


def test_dauer_parsen_tage_deutsch_und_englisch():
    assert bot_mod._dauer_parsen("2 Tage") == 172800
    assert bot_mod._dauer_parsen("2 days") == 172800


def test_dauer_parsen_leer_oder_unsinn_gibt_none():
    assert bot_mod._dauer_parsen("") is None
    assert bot_mod._dauer_parsen("bald") is None
    assert bot_mod._dauer_parsen("10 lichtjahre") is None  # unbekannte Einheit -> ganz ungueltig


def test_dauer_parsen_null_ist_ungueltig():
    assert bot_mod._dauer_parsen("0 minutes") is None


# ── ID-Vergabe (reine Funktion, analog _ensure_ticket_ids) ───────────────

def test_ensure_giveaway_ids_vergibt_fortlaufende_ids():
    eintraege = [{"id": None, "prize": "a"}, {"id": 5, "prize": "b"}, {"id": None, "prize": "c"}]
    geaendert = bot_mod._ensure_giveaway_ids(eintraege)
    assert geaendert is True
    ids = [e["id"] for e in eintraege]
    assert ids[1] == 5
    assert len(set(ids)) == 3


def test_giveaways_startet_leer_und_speichert_in_conn():
    conn = bot_mod.ServerConnection({"service_id": "9999", "guild_id": 111})
    assert bot_mod._giveaways(conn) == []
    eintraege = bot_mod._giveaways(conn)
    eintraege.append({"id": 1, "prize": "Olga"})
    assert conn.data["giveaways"] == eintraege


def test_giveaway_finden():
    conn = bot_mod.ServerConnection({"service_id": "9999", "guild_id": 111})
    conn.data["giveaways"] = [{"id": 1, "prize": "a"}, {"id": 2, "prize": "b"}]
    assert bot_mod._giveaway_finden(conn, 2)["prize"] == "b"
    assert bot_mod._giveaway_finden(conn, 99) is None


def test_giveaways_kein_rueckfall_auf_cfg_config():
    # Regressionstest fuer einen echten, hier gefundenen Fehler: "giveaways"
    # (und die beiden Server-Einstellungen) fehlten anfangs in
    # _KEINE_RUECKFALL_SCHLUESSEL - ein Server ohne eigene Gewinnspiele las
    # dann per cfg.config-Rueckfall die Teilnehmerliste/Einstellungen eines
    # FREMDEN Servers (siehe ServerConnection.get: alles ausserhalb von
    # _KEINE_RUECKFALL_SCHLUESSEL faellt sonst auf cfg.config zurueck, genau
    # die globale Struktur, in die _conn_store fuer den "primary"-Server
    # mitschreibt).
    bot_mod.cfg.config["giveaways"] = [{"id": 1, "prize": "Fremdes Gewinnspiel"}]
    bot_mod.cfg.config["giveaway_farbe"] = 0x123456
    bot_mod.cfg.config["giveaway_required_role_id"] = 999
    try:
        conn_b = bot_mod.ServerConnection({"service_id": "gw-leak-b", "guild_id": 222})
        assert bot_mod._giveaways(conn_b) == []
        assert conn_b.get("giveaway_farbe", bot_mod._GIVEAWAY_FARBE_DEFAULT) \
            == bot_mod._GIVEAWAY_FARBE_DEFAULT
        assert conn_b.get("giveaway_required_role_id") is None
    finally:
        for schluessel in ("giveaways", "giveaway_farbe", "giveaway_required_role_id"):
            bot_mod.cfg.config.pop(schluessel, None)


# ── Stubs (Muster: test_ticket_stale_lock.py) ────────────────────────────

class _StubMember:
    def __init__(self, id_, roles=None, name="Spieler"):
        self.id = id_
        self.name = name
        self.mention = f"<@{id_}>"
        self.roles = roles or []

    def __str__(self):
        return self.name


class _StubResponse:
    def __init__(self):
        self.sent = []
        self.deferred = False
        self.modal = None

    def is_done(self):
        return self.deferred or bool(self.sent)

    async def send_message(self, content=None, embed=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})

    async def send_modal(self, modal):
        self.modal = modal


class _StubFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class _StubMessage:
    def __init__(self, id_):
        self.id = id_
        self.edits = []
        self.deleted = False

    async def edit(self, embed=None, view=None):
        self.edits.append({"embed": embed, "view": view})

    async def delete(self):
        self.deleted = True


class _StubChannel:
    def __init__(self, id_=555):
        self.id = id_
        self.sent = []
        self._nachrichten = {}

    async def send(self, content=None, embed=None, view=None):
        msg = _StubMessage(len(self._nachrichten) + 1000)
        self._nachrichten[msg.id] = msg
        self.sent.append({"content": content, "embed": embed, "view": view, "message": msg})
        return msg

    async def fetch_message(self, mid):
        nachricht = self._nachrichten.get(int(mid))
        if nachricht is None:
            raise discord.NotFound(type("R", (), {"status": 404, "reason": ""})(), "not found")
        return nachricht


class _StubInteraction:
    def __init__(self, user, channel, guild_id=111):
        self.user = user
        self.channel = channel
        self.channel_id = channel.id if channel else None
        self.guild_id = guild_id
        self.locale = None
        self.response = _StubResponse()
        self.followup = _StubFollowup()
        self.message = None


_gw_guild_zaehler = [8000]


def _giveaway_conn(service_id="gw-service"):
    # Jeder Aufruf bekommt eine EIGENE Guild-ID - sonst sieht _conn_waehlen
    # mehrere Server an derselben Guild haengen (alle Tests teilen sich sonst
    # Guild 111) und fragt nach einem "server:"-Parameter statt den Server
    # eindeutig aufzuloesen (gleiches Muster wie der Service-ID-Zaehler in
    # test_ce_events_feed.py).
    _gw_guild_zaehler[0] += 1
    bot_mod.connections.upsert(service_id)
    bot_mod.connections.assign_guild(service_id, _gw_guild_zaehler[0])
    return bot_mod.connections.for_service(service_id)


# ── /gcreate + /gstart (gemeinsamer Erstell-Pfad) ────────────────────────

def test_giveaway_erstellen_postet_embed_und_legt_datensatz_an():
    conn = _giveaway_conn("gw-erstellen")
    kanal = _StubChannel()
    interaction = _StubInteraction(_StubMember(1), kanal)
    _run(bot_mod._giveaway_erstellen(
        interaction, conn, prize="Olga Black", winners_count=2, ends_in_seconds=600,
        description="Testbeschreibung", sprache="de"))
    eintraege = bot_mod._giveaways(conn)
    assert len(eintraege) == 1
    eintrag = eintraege[0]
    assert eintrag["prize"] == "Olga Black"
    assert eintrag["winners_count"] == 2
    assert eintrag["status"] == "running"
    assert eintrag["message_id"] == kanal.sent[0]["message"].id
    assert interaction.response.sent[0]["ephemeral"] is True
    assert isinstance(kanal.sent[0]["view"], bot_mod.GiveawayEntryView)


def test_giveaway_erstellen_ohne_preis_lehnt_ab():
    conn = _giveaway_conn("gw-ohne-preis")
    kanal = _StubChannel()
    interaction = _StubInteraction(_StubMember(1), kanal)
    _run(bot_mod._giveaway_erstellen(
        interaction, conn, prize="", winners_count=1, ends_in_seconds=600,
        description="", sprache="de"))
    assert bot_mod._giveaways(conn) == []
    assert "Preis" in interaction.response.sent[0]["content"]


def test_giveaway_create_modal_lehnt_ungueltige_dauer_ab():
    modal = bot_mod.GiveawayCreateModal("gw-service", "de")
    modal.dauer_in._value = "unsinn"
    modal.sieger_in._value = "1"
    modal.preis_in._value = "Preis"
    modal.beschreibung_in._value = ""
    interaction = _StubInteraction(_StubMember(1), _StubChannel())
    _run(modal.on_submit(interaction))
    assert "Ungültige Dauer" in interaction.response.sent[0]["content"]


def test_giveaway_create_modal_lehnt_null_sieger_ab():
    modal = bot_mod.GiveawayCreateModal("gw-service", "de")
    modal.dauer_in._value = "10 minutes"
    modal.sieger_in._value = "0"
    modal.preis_in._value = "Preis"
    modal.beschreibung_in._value = ""
    interaction = _StubInteraction(_StubMember(1), _StubChannel())
    _run(modal.on_submit(interaction))
    assert "Sieger" in interaction.response.sent[0]["content"]


# ── Teilnahme-Button ──────────────────────────────────────────────────────

def test_teilnehmen_fuegt_user_hinzu_und_aktualisiert_embed():
    conn = _giveaway_conn("gw-teilnahme")
    eintrag = {"id": 1, "message_id": 1000, "channel_id": 555, "prize": "x",
              "winners_count": 1, "ends_at": time.time() + 600, "sprache": "de",
              "required_role_id": None, "entrants": [], "winners": [], "status": "running"}
    conn.data["giveaways"] = [eintrag]
    view = bot_mod.GiveawayEntryView(conn.service_id, 1)
    kanal = _StubChannel()
    nachricht = _StubMessage(1000)
    interaction = _StubInteraction(_StubMember(42), kanal)
    interaction.message = nachricht
    _run(view._teilnehmen(interaction))
    assert eintrag["entrants"] == [42]
    assert nachricht.edits  # Embed wurde aktualisiert
    assert interaction.response.sent[0]["ephemeral"] is True


def test_teilnehmen_doppelt_wird_ignoriert():
    conn = _giveaway_conn("gw-doppelt")
    eintrag = {"id": 1, "message_id": 1000, "channel_id": 555, "prize": "x",
              "winners_count": 1, "ends_at": time.time() + 600, "sprache": "de",
              "required_role_id": None, "entrants": [42], "winners": [], "status": "running"}
    conn.data["giveaways"] = [eintrag]
    view = bot_mod.GiveawayEntryView(conn.service_id, 1)
    interaction = _StubInteraction(_StubMember(42), _StubChannel())
    interaction.message = _StubMessage(1000)
    _run(view._teilnehmen(interaction))
    assert eintrag["entrants"] == [42]
    assert "bereits" in interaction.response.sent[0]["content"]


def test_teilnehmen_ohne_pflichtrolle_lehnt_ab():
    conn = _giveaway_conn("gw-rolle")
    eintrag = {"id": 1, "message_id": 1000, "channel_id": 555, "prize": "x",
              "winners_count": 1, "ends_at": time.time() + 600, "sprache": "de",
              "required_role_id": 999, "entrants": [], "winners": [], "status": "running"}
    conn.data["giveaways"] = [eintrag]
    view = bot_mod.GiveawayEntryView(conn.service_id, 1)
    interaction = _StubInteraction(_StubMember(42, roles=[]), _StubChannel())
    interaction.message = _StubMessage(1000)
    _run(view._teilnehmen(interaction))
    assert eintrag["entrants"] == []
    assert "Rolle" in interaction.response.sent[0]["content"]


def test_teilnehmen_mit_pflichtrolle_erlaubt(monkeypatch):
    conn = _giveaway_conn("gw-rolle-ok")
    eintrag = {"id": 1, "message_id": 1000, "channel_id": 555, "prize": "x",
              "winners_count": 1, "ends_at": time.time() + 600, "sprache": "de",
              "required_role_id": 999, "entrants": [], "winners": [], "status": "running"}
    conn.data["giveaways"] = [eintrag]
    view = bot_mod.GiveawayEntryView(conn.service_id, 1)
    rolle = type("R", (), {"id": 999})()
    monkeypatch.setattr(discord, "Member", _StubMember)  # isinstance-Pruefung im Callback
    interaction = _StubInteraction(_StubMember(42, roles=[rolle]), _StubChannel())
    interaction.message = _StubMessage(1000)
    _run(view._teilnehmen(interaction))
    assert eintrag["entrants"] == [42]


# ── Automatisches Beenden (Hintergrund-Schleife) ─────────────────────────

def test_giveaway_beenden_zieht_sieger_aus_teilnehmern(monkeypatch):
    conn = _giveaway_conn("gw-beenden")
    eintrag = {"id": 1, "message_id": 1000, "channel_id": 555, "prize": "Olga",
              "winners_count": 2, "ends_at": time.time() - 1, "sprache": "de",
              "required_role_id": None, "entrants": [1, 2, 3], "winners": [], "status": "running"}
    kanal = _StubChannel()
    kanal._nachrichten[1000] = _StubMessage(1000)  # message_id existiert bereits im Kanal

    async def fake_resolve_channel(cid):
        return kanal
    monkeypatch.setattr(bot_mod.bot, "_resolve_channel", fake_resolve_channel)

    _run(bot_mod.bot._giveaway_beenden(conn, eintrag))
    assert eintrag["status"] == "ended"
    assert len(eintrag["winners"]) == 2
    assert set(eintrag["winners"]).issubset({1, 2, 3})
    assert len(set(eintrag["winners"])) == 2  # keine doppelten Sieger
    assert kanal._nachrichten[1000].edits  # Original-Embed aktualisiert
    assert len(kanal.sent) == 1  # Sieger-Ankuendigung


def test_giveaway_beenden_ohne_teilnehmer():
    conn = _giveaway_conn("gw-leer")
    eintrag = {"id": 1, "message_id": None, "channel_id": 0, "prize": "Olga",
              "winners_count": 2, "ends_at": time.time() - 1, "sprache": "de",
              "required_role_id": None, "entrants": [], "winners": [], "status": "running"}
    _run(bot_mod.bot._giveaway_beenden(conn, eintrag))
    assert eintrag["status"] == "ended"
    assert eintrag["winners"] == []


def test_giveaway_beenden_mehr_sieger_als_teilnehmer():
    conn = _giveaway_conn("gw-wenig")
    eintrag = {"id": 1, "message_id": None, "channel_id": 0, "prize": "Olga",
              "winners_count": 5, "ends_at": time.time() - 1, "sprache": "de",
              "required_role_id": None, "entrants": [1, 2], "winners": [], "status": "running"}
    _run(bot_mod.bot._giveaway_beenden(conn, eintrag))
    assert set(eintrag["winners"]) == {1, 2}


def test_giveaways_conn_beendet_nur_faellige_und_speichert(monkeypatch):
    conn = _giveaway_conn("gw-conn")
    laeuft_noch = {"id": 1, "message_id": None, "channel_id": 0, "prize": "a",
                   "winners_count": 1, "ends_at": time.time() + 600, "sprache": "de",
                   "required_role_id": None, "entrants": [], "winners": [], "status": "running"}
    ist_faellig = {"id": 2, "message_id": None, "channel_id": 0, "prize": "b",
                  "winners_count": 1, "ends_at": time.time() - 1, "sprache": "de",
                  "required_role_id": None, "entrants": [], "winners": [], "status": "running"}
    conn.data["giveaways"] = [laeuft_noch, ist_faellig]
    _run(bot_mod.bot._giveaways_conn(conn))
    assert laeuft_noch["status"] == "running"
    assert ist_faellig["status"] == "ended"


# ── /gdelete, /greroll ────────────────────────────────────────────────────

def test_gdelete_entfernt_datensatz_unabhaengig_vom_status(monkeypatch):
    conn = _giveaway_conn("gw-delete")
    kanal = _StubChannel()
    nachricht = _run(kanal.send())
    conn.data["giveaways"] = [{"id": 1, "message_id": nachricht.id, "channel_id": kanal.id,
                               "prize": "x", "winners_count": 1, "ends_at": 0, "sprache": "de",
                               "required_role_id": None, "entrants": [], "winners": [],
                               "status": "running"}]

    async def fake_resolve_channel(cid):
        return kanal
    monkeypatch.setattr(bot_mod.bot, "_resolve_channel", fake_resolve_channel)
    monkeypatch.setattr(bot_mod, "_subcmd_allowed", lambda interaction, key: True)
    interaction = _StubInteraction(_StubMember(1), kanal, guild_id=conn.guild_id)
    _run(bot_mod.cmd_gdelete.callback(interaction, 1, None))
    assert bot_mod._giveaways(conn) == []
    assert nachricht.deleted is True


def test_greroll_zieht_neue_sieger_ohne_vorherige(monkeypatch):
    conn = _giveaway_conn("gw-reroll")
    eintrag = {"id": 1, "message_id": None, "channel_id": 0, "prize": "x", "winners_count": 1,
              "ends_at": 0, "sprache": "de", "required_role_id": None,
              "entrants": [1, 2, 3], "winners": [1], "status": "ended"}
    conn.data["giveaways"] = [eintrag]
    monkeypatch.setattr(bot_mod, "_subcmd_allowed", lambda interaction, key: True)
    interaction = _StubInteraction(_StubMember(1), _StubChannel(), guild_id=conn.guild_id)
    _run(bot_mod.cmd_greroll.callback(interaction, 1, 1, None))
    assert len(eintrag["winners"]) == 2
    assert 1 in eintrag["winners"]
    neuer = [w for w in eintrag["winners"] if w != 1][0]
    assert neuer in (2, 3)


def test_greroll_zu_wenig_teilnehmer_nimmt_alle_uebrigen(monkeypatch):
    conn = _giveaway_conn("gw-reroll-wenig")
    eintrag = {"id": 1, "message_id": None, "channel_id": 0, "prize": "x", "winners_count": 1,
              "ends_at": 0, "sprache": "de", "required_role_id": None,
              "entrants": [1, 2], "winners": [1], "status": "ended"}
    conn.data["giveaways"] = [eintrag]
    monkeypatch.setattr(bot_mod, "_subcmd_allowed", lambda interaction, key: True)
    interaction = _StubInteraction(_StubMember(1), _StubChannel(), guild_id=conn.guild_id)
    _run(bot_mod.cmd_greroll.callback(interaction, 1, 5, None))
    assert set(eintrag["winners"]) == {1, 2}
    assert "nicht genug" in interaction.response.sent[0]["content"]


def test_greroll_keine_teilnehmer_uebrig_meldet_fehler(monkeypatch):
    conn = _giveaway_conn("gw-reroll-leer")
    eintrag = {"id": 1, "message_id": None, "channel_id": 0, "prize": "x", "winners_count": 1,
              "ends_at": 0, "sprache": "de", "required_role_id": None,
              "entrants": [1], "winners": [1], "status": "ended"}
    conn.data["giveaways"] = [eintrag]
    monkeypatch.setattr(bot_mod, "_subcmd_allowed", lambda interaction, key: True)
    interaction = _StubInteraction(_StubMember(1), _StubChannel(), guild_id=conn.guild_id)
    _run(bot_mod.cmd_greroll.callback(interaction, 1, 1, None))
    assert eintrag["winners"] == [1]
    assert "Keine weiteren" in interaction.response.sent[0]["content"]


# ── /hilfe: Phantom-Befehle verschwunden, neue Gruppen vorhanden ─────────

def test_hilfe_enthaelt_keine_phantom_befehle_mehr():
    interaction = _StubInteraction(_StubMember(1), _StubChannel())
    _run(bot_mod.cmd_hilfe.callback(interaction))
    embed = interaction.response.sent[0]["embed"]
    text = " ".join(f.value for f in embed.fields)
    for phantom in ("/add shopitem", "/bundle add", "/shop setprice",
                    "/shop removeitem", "/edit shopitem"):
        assert phantom not in text
    for echt in ("/gcreate", "/faction info", "/ticket add", "/send ticket panel"):
        assert echt in text
