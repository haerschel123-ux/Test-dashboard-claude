"""Tests fuer das Event-Vorlagen-System (/events add|list|remove).

Brigarde wollte eine neue, eigenstaendige Kategorie im Dashboard fuer
wiederverwendbare Event-Vorlagen - aehnlich dem Shop-Rental-System, aber
ohne Preis, ohne Neustart-Grenzen und ohne automatischen Ablauf: eine per
/events add hinzugefuegte Instanz bleibt dauerhaft bestehen, bis sie per
/events remove wieder entfernt wird. Nutzt dieselben generischen
XML-Werkzeuge wie das Rental-System (_tool_rental_event_umbenennen/
_einfuegen/_pos_schreiben).

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_event_templates.py -v
"""
import asyncio
import os
import sys
import xml.etree.ElementTree as ET

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

MISSION = "/dayzps_missions/dayzOffline.chernarusplus"

_EVENTS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<events>\n'
    '</events>\n'
)
_EVENTSPAWNS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<eventposdef>\n'
    '</eventposdef>\n'
)
_TEMPLATE_EVENT_XML = (
    '<event name="Helicrash"><nominal>1</nominal><min>1</min><max>1</max>'
    '<lifetime>3888000</lifetime><restock>0</restock><saferadius>1</saferadius>'
    '<distanceradius>1</distanceradius><cleanupradius>100</cleanupradius>'
    '<flags deletable="0" init_random="0" remove_damaged="1"/>'
    '<position>fixed</position><limit>child</limit><active>1</active>'
    '<children><child lootmax="0" lootmin="0" max="1" min="1" '
    'type="Wreck_UH1Y"/></children></event>'
)


class _FakeFTP:
    def __init__(self, ev=_EVENTS_XML, sp=_EVENTSPAWNS_XML):
        self.files = {
            f"{MISSION}/db/events.xml": ev,
            f"{MISSION}/cfgeventspawns.xml": sp,
        }

    def read_file_ex(self, pfad):
        if pfad in self.files:
            return self.files[pfad], "ok"
        return None, "missing"

    def write_file(self, pfad, inhalt):
        self.files[pfad] = inhalt
        return True

    def list_dir(self, d):
        d = d.rstrip("/")
        namen = set()
        for pfad in self.files:
            if pfad.startswith(d + "/"):
                namen.add(pfad[len(d) + 1:].split("/", 1)[0])
        return [f"{d}/{n}" for n in namen]


class _FakeConn:
    def __init__(self, ftp=None, service_id="1000", guild_id=111):
        self.service_id = service_id
        self.name = "Testserver"
        self.data = {}
        self._werte = {"ftp_mission_dir": MISSION, "map_name": "ChernarusPlus"}
        self.ftp = ftp or _FakeFTP()
        self.api = object()
        self.events_lock = asyncio.Lock()
        self._guild_id = guild_id
        self.event_catalog = bot.EventTemplateCatalog()

    def get(self, key, default=None):
        return self._werte.get(key, default)

    def set(self, key, value):
        self._werte[key] = value

    @property
    def guild_id(self):
        return self._guild_id


def _run(coro):
    return asyncio.run(coro)


class _StubResponse:
    def __init__(self):
        self.sent = []
        self.deferred = False

    def is_done(self):
        return self.deferred or bool(self.sent)

    async def defer(self, ephemeral=False):
        self.deferred = True

    async def send_message(self, *a, **k):
        self.sent.append((a, k))


class _StubFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *a, **k):
        self.sent.append((a, k))


class _StubUser:
    id = 555


class _StubInteraktion:
    def __init__(self, guild_id=111):
        self.guild_id = guild_id
        self.user = _StubUser()
        self.locale = None
        self.response = _StubResponse()
        self.followup = _StubFollowup()


def _vorlage(conn, **overrides):
    it = {"name": "Heli Crash", "event_xml": _TEMPLATE_EVENT_XML, "event_zone": "", "event_group": ""}
    it.update(overrides)
    conn.event_catalog.items.append(it)
    conn.event_catalog.rebuild_index()
    return it


# ── EventTemplateCatalog: eigener Katalog, kein classname-Filter ─────────

def test_katalog_verwirft_keinen_eintrag_ohne_classname(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kat = bot.EventTemplateCatalog("test-events-1")
    kat.items.append({"name": "Heli Crash", "event_xml": _TEMPLATE_EVENT_XML, "event_zone": "",
                      "event_group": ""})
    kat.rebuild_index()
    assert kat.save() is True
    kat2 = bot.EventTemplateCatalog("test-events-1")
    kat2.load()
    assert len(kat2.items) == 1
    assert kat2.find("heli crash")["name"] == "Heli Crash"


# ── Zufallsname ───────────────────────────────────────────────────────────

def test_zufallsname_hat_erwartetes_format():
    name = bot._event_katalog_zufallsname(_EVENTS_XML)
    assert name.startswith("item")
    assert name[4:].isdigit()
    assert 1000 <= int(name[4:]) <= 9999


def test_zufallsname_vermeidet_bereits_vergebenen_namen(monkeypatch):
    rufe = {"n": 0}
    orig = bot.random.randint

    def gefaelscht(a, b):
        rufe["n"] += 1
        return 1234 if rufe["n"] == 1 else 5678
    monkeypatch.setattr(bot.random, "randint", gefaelscht)
    text = _EVENTS_XML.replace("</events>", '<event name="item1234"></event></events>')
    name = bot._event_katalog_zufallsname(text)
    assert name == "item5678"
    monkeypatch.setattr(bot.random, "randint", orig)


# ── /events add ───────────────────────────────────────────────────────────

def test_events_add_mit_zufallsnamen_schreibt_beide_dateien(monkeypatch):
    ftp = _FakeFTP()
    conn = _FakeConn(ftp=ftp)
    _vorlage(conn)
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda interaction, key: True)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda interaction, server=None: (conn, None))
    interaction = _StubInteraktion()

    _run(bot.events_add.callback(interaction, name="Heli Crash", x=1410.0, z=4299.0,
                                 y=None, event_name=None, server=None))

    assert not interaction.response.sent
    assert interaction.followup.sent, "kein followup.send - /events add ist nicht durchgelaufen"
    ev_root = ET.fromstring(ftp.files[f"{MISSION}/db/events.xml"])
    namen = [e.get("name") for e in ev_root.findall("event")]
    assert len(namen) == 1
    assert namen[0].startswith("item")
    sp_text = ftp.files[f"{MISSION}/cfgeventspawns.xml"]
    block = bot._tool_finde_benannten_block(sp_text, "event", namen[0])
    assert block is not None
    assert 'x="1410' in block["block"] and 'z="4299' in block["block"]
    instanzen = bot._event_instances(conn)
    assert len(instanzen) == 1
    assert instanzen[0]["event_name"] == namen[0]
    assert instanzen[0]["template"] == "Heli Crash"


def test_events_add_mit_eigenem_namen_verwendet_ihn(monkeypatch):
    ftp = _FakeFTP()
    conn = _FakeConn(ftp=ftp)
    _vorlage(conn)
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda interaction, key: True)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda interaction, server=None: (conn, None))
    interaction = _StubInteraktion()

    _run(bot.events_add.callback(interaction, name="Heli Crash", x=100.0, z=200.0,
                                 y=None, event_name="mein_heli_1", server=None))

    ev_root = ET.fromstring(ftp.files[f"{MISSION}/db/events.xml"])
    assert [e.get("name") for e in ev_root.findall("event")] == ["mein_heli_1"]


def test_events_add_lehnt_ungueltigen_eigenen_namen_ab(monkeypatch):
    conn = _FakeConn()
    _vorlage(conn)
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda interaction, key: True)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda interaction, server=None: (conn, None))
    interaction = _StubInteraktion()

    _run(bot.events_add.callback(interaction, name="Heli Crash", x=100.0, z=200.0,
                                 y=None, event_name="kaputt<>name", server=None))

    assert interaction.response.sent
    assert bot._event_instances(conn) == []


def test_events_add_lehnt_unbekannte_vorlage_ab(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda interaction, key: True)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda interaction, server=None: (conn, None))
    interaction = _StubInteraktion()

    _run(bot.events_add.callback(interaction, name="Nicht Vorhanden", x=100.0, z=200.0,
                                 y=None, event_name=None, server=None))

    assert interaction.response.sent
    assert "keine event-vorlage" in interaction.response.sent[0][0][0].lower()


def test_events_add_lehnt_koordinaten_ausserhalb_der_map_ab(monkeypatch):
    conn = _FakeConn()
    _vorlage(conn)
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda interaction, key: True)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda interaction, server=None: (conn, None))
    interaction = _StubInteraktion()

    _run(bot.events_add.callback(interaction, name="Heli Crash", x=99999.0, z=200.0,
                                 y=None, event_name=None, server=None))

    assert interaction.response.sent
    assert bot._event_instances(conn) == []


def test_events_add_ohne_berechtigung_wird_abgelehnt(monkeypatch):
    conn = _FakeConn()
    _vorlage(conn)
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda interaction, key: False)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda interaction, server=None: (conn, None))
    interaction = _StubInteraktion()

    _run(bot.events_add.callback(interaction, name="Heli Crash", x=100.0, z=200.0,
                                 y=None, event_name=None, server=None))

    assert bot._event_instances(conn) == []


def test_events_add_rollt_event_zurueck_wenn_position_nicht_speicherbar(monkeypatch):
    ftp = _FakeFTP()
    conn = _FakeConn(ftp=ftp)
    _vorlage(conn)
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda interaction, key: True)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda interaction, server=None: (conn, None))

    orig_schreiben = bot._tool_datei_schreiben_sync

    def kaputt(c, dateiname, inhalt):
        if dateiname == "cfgeventspawns.xml":
            return False
        return orig_schreiben(c, dateiname, inhalt)
    monkeypatch.setattr(bot, "_tool_datei_schreiben_sync", kaputt)

    interaction = _StubInteraktion()
    _run(bot.events_add.callback(interaction, name="Heli Crash", x=100.0, z=200.0,
                                 y=None, event_name="rollback_test", server=None))

    ev_root = ET.fromstring(ftp.files[f"{MISSION}/db/events.xml"])
    assert ev_root.findall("event") == []  # zurueckgerollt
    assert bot._event_instances(conn) == []


# ── /events list, /events remove ─────────────────────────────────────────

def test_events_list_zeigt_hinzugefuegte_instanzen(monkeypatch):
    conn = _FakeConn()
    bot._event_instances(conn).append(
        {"template": "Heli Crash", "event_name": "item1234", "x": 1.0, "y": None, "z": 2.0,
         "created_at": 0, "created_by": 1})
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda interaction, key: True)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda interaction, server=None: (conn, None))
    interaction = _StubInteraktion()

    _run(bot.events_list_cmd.callback(interaction, server=None))

    assert interaction.response.sent
    embed = interaction.response.sent[0][1].get("embed")
    assert "item1234" in embed.description


def test_events_list_ohne_instanzen_meldet_leer(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda interaction, key: True)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda interaction, server=None: (conn, None))
    interaction = _StubInteraktion()

    _run(bot.events_list_cmd.callback(interaction, server=None))

    assert "keine event-instanzen" in interaction.response.sent[0][0][0].lower()


def test_events_remove_entfernt_datei_bloecke_und_instanz(monkeypatch):
    ftp = _FakeFTP(
        ev=_EVENTS_XML.replace("</events>", '<event name="item9999"></event></events>'),
        sp=_EVENTSPAWNS_XML.replace(
            "</eventposdef>", '<event name="item9999"><pos x="1" z="2" a="0"/></event></eventposdef>'))
    conn = _FakeConn(ftp=ftp)
    bot._event_instances(conn).append(
        {"template": "Heli Crash", "event_name": "item9999", "x": 1.0, "y": None, "z": 2.0,
         "created_at": 0, "created_by": 1})
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda interaction, key: True)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda interaction, server=None: (conn, None))
    interaction = _StubInteraktion()

    _run(bot.events_remove_cmd.callback(interaction, event_name="item9999", server=None))

    assert interaction.followup.sent
    ev_root = ET.fromstring(ftp.files[f"{MISSION}/db/events.xml"])
    assert ev_root.findall("event") == []
    assert bot._event_instances(conn) == []


def test_events_remove_unbekannter_name_meldet_fehler(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda interaction, key: True)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda interaction, server=None: (conn, None))
    interaction = _StubInteraktion()

    _run(bot.events_remove_cmd.callback(interaction, event_name="nichtvorhanden", server=None))

    assert interaction.response.sent
    assert not interaction.followup.sent


def test_events_remove_ohne_berechtigung_wird_abgelehnt(monkeypatch):
    conn = _FakeConn()
    bot._event_instances(conn).append(
        {"template": "Heli Crash", "event_name": "item9999", "x": 1.0, "y": None, "z": 2.0,
         "created_at": 0, "created_by": 1})
    monkeypatch.setattr(bot, "_subcmd_allowed", lambda interaction, key: False)
    monkeypatch.setattr(bot, "_conn_waehlen", lambda interaction, server=None: (conn, None))
    interaction = _StubInteraktion()

    _run(bot.events_remove_cmd.callback(interaction, event_name="item9999", server=None))

    assert len(bot._event_instances(conn)) == 1  # unangetastet


# ── Registrierung im Subcommand-Permission-System ────────────────────────

def test_events_befehle_im_subcommand_permission_system_registriert():
    for key in ("events_add", "events_list", "events_remove"):
        assert key in bot._SUBCMD_KEYS


def test_events_dashboard_kategorie_registriert():
    assert "events" in bot._DASH_PERM_CAT_KEYS
    assert "event_catalog" in bot.FEATURE_MODULES
