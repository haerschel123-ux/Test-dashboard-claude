"""Tests fuer das Shop-Rental-System (Miet-Items mit Neustart-Ablauf).

Schwerpunkte: Validierung der vom Admin eingegebenen Event-XML/Zone-Vorlagen,
das Einfuegen/Umbenennen/Entfernen von Event- und Positions-Bloecken in
db/events.xml + cfgeventspawns.xml, der RENT_-Filter gegen Event-Vorlagen/
Vehicle-Builder, der RentalCatalog-Laden/Speichern-Zyklus (der NICHT ueber
ShopCatalog laufen darf), und die zweistufige Ablauf-Entfernung nach
simulierten Server-Neustarts.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/ -v
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
    '    <event name="VehicleCivilianSedan">\n'
    '        <nominal>10</nominal>\n'
    '        <min>10</min>\n'
    '        <max>10</max>\n'
    '        <lifetime>300</lifetime>\n'
    '        <restock>0</restock>\n'
    '        <saferadius>500</saferadius>\n'
    '        <distanceradius>500</distanceradius>\n'
    '        <cleanupradius>200</cleanupradius>\n'
    '        <flags deletable="0" init_random="0" remove_damaged="1"/>\n'
    '        <position>fixed</position>\n'
    '        <limit>mixed</limit>\n'
    '        <active>1</active>\n'
    '        <children>\n'
    '            <child lootmax="0" lootmin="0" max="5" min="3" type="CivilianSedan"/>\n'
    '        </children>\n'
    '    </event>\n'
    '</events>\n'
)

_EVENTSPAWNS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<eventposdef>\n'
    '    <event name="VehicleCivilianSedan">\n'
    '        <pos x="12071.933594" z="9129.989258" a="317.953339"/>\n'
    '    </event>\n'
    '</eventposdef>\n'
)

_RENTAL_EVENT_XML = (
    '<event name="Vehicle"><nominal>1</nominal><min>1</min><max>1</max>'
    '<lifetime>3888000</lifetime><restock>0</restock><saferadius>1</saferadius>'
    '<distanceradius>1</distanceradius><cleanupradius>100</cleanupradius>'
    '<flags deletable="0" init_random="0" remove_damaged="1"/>'
    '<position>fixed</position><limit>child</limit><active>1</active>'
    '<children><child lootmax="0" lootmin="0" max="1" min="1" '
    'type="CivilianSedan_Black"/></children></event>'
)


class _FakeFTP:
    """Haelt db/events.xml + cfgeventspawns.xml im Speicher; liefert
    read_file_ex/write_file/list_dir wie der echte FTPManager."""

    def __init__(self, ev=_EVENTS_XML, sp=_EVENTSPAWNS_XML):
        self.files = {
            f"{MISSION}/db/events.xml": ev,
            f"{MISSION}/cfgeventspawns.xml": sp,
        }
        self.geschrieben = []

    def read_file_ex(self, pfad):
        if pfad in self.files:
            return self.files[pfad], "ok"
        return None, "missing"

    def write_file(self, pfad, inhalt):
        self.files[pfad] = inhalt
        self.geschrieben.append(pfad)
        return True

    def list_dir(self, d):
        d = d.rstrip("/")
        namen = set()
        for pfad in self.files:
            if not pfad.startswith(d + "/"):
                continue
            namen.add(pfad[len(d) + 1:].split("/", 1)[0])
        return [f"{d}/{n}" for n in namen]


class _FakeConn:
    def __init__(self, ftp=None, service_id="1000", guild_id=111):
        self.service_id = service_id
        self.name = "Testserver"
        self.data = {}
        self._werte = {"ftp_mission_dir": MISSION, "map_name": "ChernarusPlus"}
        self.ftp = ftp or _FakeFTP()
        self.api = None
        self.shop = None
        self.events_lock = asyncio.Lock()
        self._guild_id = guild_id

    def get(self, key, default=None):
        return self._werte.get(key, default)

    def set(self, key, value):
        self._werte[key] = value

    @property
    def guild_id(self):
        return self._guild_id


def _run(coro):
    return asyncio.run(coro)


# ── Validierung der Admin-Vorlagen ───────────────────────────────────────
def test_gueltiges_event_xml_wird_akzeptiert():
    text, fehler = bot._tool_rental_xml_validieren(_RENTAL_EVENT_XML, "event")
    assert fehler is None
    assert text.startswith("<event")


@pytest.mark.parametrize("kaputt", [
    "", "   ", "<event", "<zone/>", "<event/><event/>",
    "<!DOCTYPE x><event name=\"x\"/>", "<event name=\"x\"><!--x--></event>",
    "<event name=\"x\"><![CDATA[x]]></event>", "<?xml version=\"1.0\"?><event name=\"x\"/>",
])
def test_ungueltiges_event_xml_wird_abgelehnt(kaputt):
    text, fehler = bot._tool_rental_xml_validieren(kaputt, "event")
    assert text is None and fehler


def test_zu_langes_event_xml_wird_abgelehnt():
    lang = '<event name="x">' + "a" * bot._RENTAL_MAX_EVENT_XML + "</event>"
    text, fehler = bot._tool_rental_xml_validieren(lang, "event")
    assert text is None and fehler


def test_zone_xml_verlangt_zone_als_wurzel():
    text, fehler = bot._tool_rental_xml_validieren('<event name="x"/>', "zone")
    assert text is None and fehler
    text, fehler = bot._tool_rental_xml_validieren('<zone smin="0" smax="0"/>', "zone")
    assert fehler is None and text.startswith("<zone")


# ── Event-Vorlage umbenennen + einfuegen ─────────────────────────────────
def test_event_wird_auf_instanz_id_umbenannt():
    umbenannt = bot._tool_rental_event_umbenennen(_RENTAL_EVENT_XML, "RENT_abc123")
    assert 'name="RENT_abc123"' in umbenannt
    assert 'name="Vehicle"' not in umbenannt


def test_event_einfuegen_und_wiederfinden():
    umbenannt = bot._tool_rental_event_umbenennen(_RENTAL_EVENT_XML, "RENT_xyz")
    neu = bot._tool_rental_event_einfuegen(_EVENTS_XML, "RENT_xyz", umbenannt)
    gefunden = bot._tool_finde_benannten_block(neu, "event", "RENT_xyz")
    assert gefunden is not None
    # Der Rest der Datei (bestehendes Vanilla-Event) bleibt unangetastet.
    assert "VehicleCivilianSedan" in neu
    # Gueltiges XML nach dem Einfuegen (in eine Huelle gepackt, da das
    # Fragment selbst kein Wurzel-Element von "events" hat).
    ET.fromstring(neu)


def test_event_einfuegen_ohne_events_tag_wirft_valueerror():
    kaputt = "<nicht_events></nicht_events>"
    with pytest.raises(ValueError):
        bot._tool_rental_event_einfuegen(kaputt, "RENT_x", _RENTAL_EVENT_XML)


# ── Positions-Schreibfunktion (x/y/z/a/group) ────────────────────────────
def test_rental_pos_schreibt_pflichtfelder():
    neu = bot._tool_rental_pos_schreiben(_EVENTSPAWNS_XML, "RENT_abc", x=100.0, z=200.0)
    block = bot._tool_finde_benannten_block(neu, "event", "RENT_abc")
    assert block is not None
    assert 'x="100"' in block["block"] and 'z="200"' in block["block"]
    assert 'a="0"' in block["block"]           # a immer geschrieben, Default 0
    assert "y=" not in block["block"]           # y nicht gesetzt -> nicht geschrieben
    assert "group=" not in block["block"]


def test_rental_pos_schreibt_optionale_felder_nur_wenn_gesetzt():
    neu = bot._tool_rental_pos_schreiben(
        _EVENTSPAWNS_XML, "RENT_def", x=1.0, z=2.0, y=140.0, a=90.0, gruppe="MyGroup")
    block = bot._tool_finde_benannten_block(neu, "event", "RENT_def")
    assert 'y="140"' in block["block"]
    assert 'a="90"' in block["block"]
    assert 'group="MyGroup"' in block["block"]


def test_rental_pos_mit_zone_xml_haengt_zone_anhaengen():
    neu = bot._tool_rental_pos_schreiben(
        _EVENTSPAWNS_XML, "RENT_ghi", x=1.0, z=2.0, zone_xml='<zone smin="0" smax="1"/>')
    block = bot._tool_finde_benannten_block(neu, "event", "RENT_ghi")
    assert "<zone" in block["block"]


def test_bestehende_vanilla_position_bleibt_unangetastet():
    neu = bot._tool_rental_pos_schreiben(_EVENTSPAWNS_XML, "RENT_neu", x=1.0, z=2.0)
    assert "12071.933594" in neu   # die vorhandene VehicleCivilianSedan-Position


# ── Regression: bestehende Event-Rebuild-Schleifen behalten y/group NICHT
#    absichtlich unveraendert bei (siehe Plan Abschnitt 4) - dieser Test
#    dokumentiert/beweist genau die Entscheidung, _tool_spawn_positionen
#    NICHT um y/group zu erweitern: die generischen Tools lesen weiterhin
#    nur x/z/a und wuerden bei einem Speichern y/group verlieren. Rentals
#    laufen deshalb nie durch diese Tools (siehe RENT_-Filter unten).
def test_generische_positions_lesefunktion_kennt_kein_y_oder_group():
    sp_mit_y = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<eventposdef><event name="X"><pos x="1" y="140" z="2" a="0" '
        'group="G"/></event></eventposdef>\n')
    root = ET.fromstring(sp_mit_y)
    positions = bot._tool_spawn_positionen(root, "X")
    assert positions == [{"x": "1", "z": "2", "a": "0"}]


# ── RENT_-Filter gegen Event-Vorlagen/Vehicle-Builder ────────────────────
def test_rent_events_werden_aus_der_liste_gefiltert():
    text = _EVENTS_XML.replace(
        "</events>",
        '    <event name="RENT_abc123"><children><child lootmax="0" lootmin="0" '
        'max="1" min="1" type="CivilianSedan_Black"/></children></event>\n</events>')
    root = ET.fromstring(text)
    namen = bot._tool_events_liste(root)
    assert "VehicleCivilianSedan" in namen
    assert "RENT_abc123" not in namen


# ── Zweistufige Lifetime/Deletable-Aenderung (Stufe 1 des Ablaufs) ───────
def test_lifetime_kuerzen_setzt_lifetime_und_deletable():
    umbenannt = bot._tool_rental_event_umbenennen(_RENTAL_EVENT_XML, "RENT_stufe1")
    mit_event = bot._tool_rental_event_einfuegen(_EVENTS_XML, "RENT_stufe1", umbenannt)
    neu = bot._tool_rental_lifetime_kuerzen(mit_event, "RENT_stufe1", sekunden=30)
    block = bot._tool_finde_benannten_block(neu, "event", "RENT_stufe1")
    assert "<lifetime>30</lifetime>" in block["block"]
    assert 'deletable="1"' in block["block"]


def test_lifetime_kuerzen_ohne_block_wirft_valueerror():
    with pytest.raises(ValueError):
        bot._tool_rental_lifetime_kuerzen(_EVENTS_XML, "RENT_nicht_da", 30)


# ── RentalCatalog: eigener Lade-/Speicher-Zyklus, KEIN ShopCatalog-Reuse ──
def test_rental_catalog_laedt_item_ohne_classname(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    katalog = bot.RentalCatalog("9999")
    katalog.items = [{
        "name": "Olga black", "price_per_restart": 5000,
        "event_xml": _RENTAL_EVENT_XML, "event_zone": "",
        "category": "Vehicle", "enabled": True,
        "min_restarts": 1, "max_restarts": 16,
    }]
    assert katalog.save() is True

    neu = bot.RentalCatalog("9999")
    neu.load()
    assert len(neu.items) == 1
    assert neu.find("Olga black") is not None
    # Ein Shop-Katalog haette dasselbe Item verworfen (kein classname/classnames).
    assert neu.find("Olga black").get("event_xml") == _RENTAL_EVENT_XML


def test_rental_catalog_leere_datei_ergibt_leere_liste(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    katalog = bot.RentalCatalog("keine_datei")
    katalog.load()
    assert katalog.items == []


# ── Neustart-Simulation: zweistufiger Ablauf ─────────────────────────────
class _FakeDB:
    """Minimaler Ersatz fuer die rentals-Tabelle - haelt Zeilen im
    Speicher statt in SQLite, mit derselben Methodensignatur wie EconomyDB."""

    def __init__(self):
        self.rows = {}
        self._next_id = 1
        self.feed_posts = []
        self.wallet = 10_000_000

    def get_balance(self, guild_id, user_id):
        return self.wallet, 0

    def try_spend_wallet(self, guild_id, user_id, amount):
        if amount > self.wallet:
            return False
        self.wallet -= amount
        return True

    def create_rental(self, service_id, guild_id, user_id, user_name, item_name,
                      event_name, x, y, z, a, pos_group, restarts, expires_at):
        rid = self._next_id
        self._next_id += 1
        self.rows[rid] = {
            "id": rid, "service_id": service_id, "guild_id": guild_id,
            "user_id": user_id, "user_name": user_name, "item_name": item_name,
            "event_name": event_name, "x": x, "y": y, "z": z, "a": a,
            "pos_group": pos_group, "restarts_total": restarts,
            "restarts_remaining": restarts, "status": "active",
            "expires_at": expires_at, "removed_at": None,
        }
        return rid

    def active_rentals(self, guild_id, service_id):
        return [dict(r) for r in self.rows.values()
                if r["status"] in ("active", "expiring")
                and r["guild_id"] == guild_id and r["service_id"] == service_id]

    def rental_decrement(self, rental_id, restarts_remaining):
        self.rows[rental_id]["restarts_remaining"] = restarts_remaining

    def rental_set_expiring(self, rental_id):
        self.rows[rental_id]["status"] = "expiring"
        self.rows[rental_id]["restarts_remaining"] = 0

    def rental_set_removed(self, rental_id):
        self.rows[rental_id]["status"] = "removed"

    def rental_delete(self, rental_id):
        self.rows.pop(rental_id, None)


@pytest.fixture
def fake_db(monkeypatch):
    fdb = _FakeDB()
    monkeypatch.setattr(bot, "db", fdb)
    monkeypatch.setattr(bot, "_post_feed",
                        lambda *a, **k: fdb.feed_posts.append((a, k)) or _afertig())
    return fdb


async def _afertig():
    return None


def _kaufen(fdb, ftp, conn, restarts):
    """Simuliert den Schreib-Teil von /shop buy rental (ohne Discord/Wallet) -
    dieselben Bausteine wie der echte Befehl."""
    event_name = bot._tool_rental_event_name()
    ev_text = ftp.files[f"{MISSION}/db/events.xml"]
    sp_text = ftp.files[f"{MISSION}/cfgeventspawns.xml"]
    umbenannt = bot._tool_rental_event_umbenennen(_RENTAL_EVENT_XML, event_name)
    neu_ev = bot._tool_rental_event_einfuegen(ev_text, event_name, umbenannt)
    neu_sp = bot._tool_rental_pos_schreiben(sp_text, event_name, x=100.0, z=200.0)
    ftp.files[f"{MISSION}/db/events.xml"] = neu_ev
    ftp.files[f"{MISSION}/cfgeventspawns.xml"] = neu_sp
    rid = fdb.create_rental(conn.service_id, conn.guild_id, 1, "Käufer", "Olga black",
                            event_name, 100.0, None, 200.0, None, None, restarts, None)
    return rid, event_name


def test_zweistufiger_ablauf_entfernt_event_und_position(fake_db):
    ftp = _FakeFTP()
    conn = _FakeConn(ftp=ftp)
    conn.rentals = bot.RentalManager(bot, conn)
    rid, event_name = _kaufen(fake_db, ftp, conn, restarts=2)

    # Neustart 1: Zaehler 2 -> 1, Event bleibt vollstaendig erhalten.
    _run(conn.rentals.on_restart_detected(delayed=False))
    assert fake_db.rows[rid]["status"] == "active"
    assert fake_db.rows[rid]["restarts_remaining"] == 1
    assert bot._tool_finde_benannten_block(
        ftp.files[f"{MISSION}/db/events.xml"], "event", event_name) is not None

    # Neustart 2: Zaehler erreicht 0 -> Stufe 1 (lifetime kurz, deletable=1,
    # status 'expiring'), Event bleibt in der Datei stehen.
    _run(conn.rentals.on_restart_detected(delayed=False))
    assert fake_db.rows[rid]["status"] == "expiring"
    block = bot._tool_finde_benannten_block(
        ftp.files[f"{MISSION}/db/events.xml"], "event", event_name)
    assert block is not None
    assert f"<lifetime>{bot.RentalManager.LIFETIME_KURZ_SEKUNDEN}</lifetime>" in block["block"]
    assert 'deletable="1"' in block["block"]
    # Position ist zu diesem Zeitpunkt noch da.
    assert bot._tool_finde_benannten_block(
        ftp.files[f"{MISSION}/cfgeventspawns.xml"], "event", event_name) is not None

    # Neustart 3: Stufe 2 - Event UND Position werden entfernt.
    _run(conn.rentals.on_restart_detected(delayed=False))
    assert fake_db.rows[rid]["status"] == "removed"
    assert bot._tool_finde_benannten_block(
        ftp.files[f"{MISSION}/db/events.xml"], "event", event_name) is None
    assert bot._tool_finde_benannten_block(
        ftp.files[f"{MISSION}/cfgeventspawns.xml"], "event", event_name) is None
    # Das Vanilla-Event daneben ist unberuehrt.
    assert bot._tool_finde_benannten_block(
        ftp.files[f"{MISSION}/db/events.xml"], "event", "VehicleCivilianSedan") is not None


def test_andere_mieten_bleiben_beim_ablauf_einer_miete_unberuehrt(fake_db):
    ftp = _FakeFTP()
    conn = _FakeConn(ftp=ftp)
    conn.rentals = bot.RentalManager(bot, conn)
    rid_kurz, name_kurz = _kaufen(fake_db, ftp, conn, restarts=1)
    rid_lang, name_lang = _kaufen(fake_db, ftp, conn, restarts=5)

    _run(conn.rentals.on_restart_detected(delayed=False))   # kurz: 1 -> 0, Stufe 1
    assert fake_db.rows[rid_kurz]["status"] == "expiring"
    assert fake_db.rows[rid_lang]["status"] == "active"
    assert fake_db.rows[rid_lang]["restarts_remaining"] == 4
    assert bot._tool_finde_benannten_block(
        ftp.files[f"{MISSION}/db/events.xml"], "event", name_lang) is not None


def test_fehlgeschlagener_schreibvorgang_setzt_retry_flag(fake_db, monkeypatch):
    ftp = _FakeFTP()
    conn = _FakeConn(ftp=ftp)
    conn.rentals = bot.RentalManager(bot, conn)
    _kaufen(fake_db, ftp, conn, restarts=1)

    def kaputtes_schreiben(_conn, _name, _inhalt):
        return False
    monkeypatch.setattr(bot, "_tool_datei_schreiben_sync", kaputtes_schreiben)

    _run(conn.rentals.on_restart_detected(delayed=False))
    assert conn.rentals.cleanup_retry_needed is True
    # Zeile darf nicht als erledigt gelten, wenn das Schreiben scheiterte.
    row = next(iter(fake_db.rows.values()))
    assert row["status"] == "active"


def test_kein_mission_ordner_setzt_retry_flag(fake_db):
    conn = _FakeConn()
    conn.set("ftp_mission_dir", "")
    conn.rentals = bot.RentalManager(bot, conn)
    _kaufen(fake_db, conn.ftp, conn, restarts=1)
    _run(conn.rentals.on_restart_detected(delayed=False))
    assert conn.rentals.cleanup_retry_needed is True


# ── conn.events_lock ist lazy (kein asyncio.Lock() ausserhalb eines
#    laufenden Event-Loops - siehe hydrate_lock-Vorbild) ─────────────────
def test_events_lock_ist_lazy():
    daten = {"service_id": "1", "name": "X"}
    conn = bot.ServerConnection(daten)
    assert conn._events_lock is None
    lock = conn.events_lock
    assert lock is conn.events_lock   # zweiter Zugriff liefert dasselbe Objekt


# ── /shop buy rental Ende-zu-Ende: event_group aus dem Katalog-Eintrag muss
#    tatsaechlich als group="..." in cfgeventspawns.xml landen - das ist die
#    Verdrahtung, die urspruenglich fehlte (_tool_rental_pos_schreiben kennt
#    "gruppe" laengst, aber shop_buy_rental hat es nie befuellt). Ruft die
#    ECHTE Kommando-Funktion auf (.callback), nicht nur ihre Bausteine, damit
#    genau dieser Anschluss mitgeprueft wird - Vorbild: tests/test_ticket_tool.py.
class _StubResponse:
    def __init__(self):
        self.sent = []

    async def defer(self, ephemeral=False):
        pass

    async def send_message(self, *a, **k):
        self.sent.append((a, k))


class _StubFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *a, **k):
        self.sent.append((a, k))


class _StubUser:
    id = 555
    mention = "<@555>"

    def __str__(self):
        return "Käufer"


class _StubInteraktion:
    def __init__(self, guild_id=111):
        self.guild_id = guild_id
        self.user = _StubUser()
        self.locale = None
        self.response = _StubResponse()
        self.followup = _StubFollowup()


def test_kauf_schreibt_event_group_aus_dem_katalog_als_pos_group(fake_db, monkeypatch):
    ftp = _FakeFTP()
    conn = _FakeConn(ftp=ftp)
    conn.api = object()          # nur auf None geprueft
    conn.rentals = True          # nur auf Wahrheitswert geprueft
    katalog = bot.RentalCatalog()
    katalog.items.append({
        "name": "Olga black", "price_per_restart": 0, "event_xml": _RENTAL_EVENT_XML,
        "event_zone": "", "category": "Vehicle", "event_group": "MyRentalGroup",
        "enabled": True, "min_restarts": 1, "max_restarts": 16, "role_ids": [],
    })
    katalog.rebuild_index()
    conn.rentals_catalog = katalog

    monkeypatch.setattr(bot, "_conns_of", lambda interaction: [conn])
    interaction = _StubInteraktion()

    _run(bot.shop_buy_rental.callback(interaction, item="Olga black", restarts=3,
                                      x=100.0, z=200.0, y=None, a=None, server=None))

    assert not interaction.response.sent, interaction.response.sent
    assert interaction.followup.sent, "kein followup.send aufgerufen - Kauf ist nicht durchgelaufen"
    sp_text = ftp.files[f"{MISSION}/cfgeventspawns.xml"]
    ev_root = ET.fromstring(ftp.files[f"{MISSION}/db/events.xml"])
    neue_events = [ev.get("name") for ev in ev_root.findall("event")
                  if (ev.get("name") or "").startswith(bot._RENTAL_EVENT_PREFIX)]
    assert len(neue_events) == 1
    block = bot._tool_finde_benannten_block(sp_text, "event", neue_events[0])
    assert block is not None
    assert 'group="MyRentalGroup"' in block["block"]
    assert len(fake_db.rows) == 1
    row = next(iter(fake_db.rows.values()))
    assert row["pos_group"] == "MyRentalGroup"
