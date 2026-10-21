"""Tests fuer die Kauf-Orte: /buy ort:..., /location save|list|delete.

Spieler verlinken sich (/link); loest ein Platzier-Ereignis aus, merkt sich der Bot den Ort
(bis zu 5 automatisch, Platzieren = "placed"/"deployed", nicht "attached"). Per /location save
werden bis zu 15 Orte unter einem Namen abgelegt. /buy bietet "ort" mit diesen Vorschlaegen an.
Alle Orte sind je Guild + Server + User getrennt.

    python3 -m pytest tests/test_kauf_orte.py -v
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

G, U, SID = 111, 42, "1000"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def edb(tmp_path, monkeypatch):
    d = bot.EconomyDB(str(tmp_path / "economy_test.db"))
    monkeypatch.setattr(bot, "db", d)
    return d


class _Parser:
    def __init__(self):
        self.player_positions = {}


class _Conn:
    def __init__(self, service_id=SID, guilds=(G,), map_name="ChernarusPlus"):
        self.service_id = service_id
        self.guild_ids = list(guilds)
        self.parser = _Parser()
        self._w = {"map_name": map_name, "default_pos_y": 0.0}
        self.catalog = None
        self.api = object()
        self.ftp = object()
        self.shop = None
        self.dispatch_verlauf = []

    def get(self, key, default=None):
        return self._w.get(key, default)


class _Resp:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, embed=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed})

    async def defer(self, ephemeral=False):
        pass


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed})


class _Inter:
    def __init__(self, uid=U, gid=G):
        self.user = type("U", (), {"id": uid, "mention": f"<@{uid}>", "__str__": lambda s: "User"})()
        self.guild_id = gid
        self.locale = None
        self.response = _Resp()
        self.followup = _Followup()

    def text(self):
        s = self.response.sent + self.followup.sent
        return " ".join(str(m["content"] or "") + (str(m["embed"].to_dict()) if m["embed"] else "") for m in s)


def _use(monkeypatch, conn):
    monkeypatch.setattr(bot, "_conn_waehlen", lambda inter, server=None: (conn, None))


# ── DB: automatische Orte ────────────────────────────────────────────────
def test_auto_nahe_platzierungen_werden_zusammengefasst(edb):
    edb.orte_auto_merken(G, SID, U, 5000, 5000, "Zaun")
    edb.orte_auto_merken(G, SID, U, 5010, 5010, "Kiste")        # ~14 m
    liste = edb.orte_liste(G, SID, U, "auto")
    assert len(liste) == 1 and liste[0]["x"] == 5010


def test_auto_rollierend_maximal_fuenf_neueste_bleiben(edb):
    for i in range(8):
        edb.orte_auto_merken(G, SID, U, 1000 + i * 500, 2000, f"i{i}", max_auto=5)
    liste = edb.orte_liste(G, SID, U, "auto")
    assert len(liste) == 5
    assert [r["x"] for r in liste] == [4500, 4000, 3500, 3000, 2500]       # neueste zuerst


def test_auto_getrennt_nach_user_server_guild(edb):
    edb.orte_auto_merken(G, SID, U, 100, 100)
    assert edb.orte_liste(G, SID, U + 1, "auto") == []
    assert edb.orte_liste(G, "2000", U, "auto") == []
    assert edb.orte_liste(G + 1, SID, U, "auto") == []
    assert len(edb.orte_liste(G, SID, U, "auto")) == 1


# ── DB: gespeicherte Orte ────────────────────────────────────────────────
def test_speichern_name_eindeutig_ohne_gross_klein(edb):
    assert edb.ort_speichern(G, SID, U, "Base", 1, 2) == "ok"
    assert edb.ort_speichern(G, SID, U, "base", 3, 4) == "name_vorhanden"
    assert edb.ort_speichern(G, SID, U + 1, "Base", 3, 4) == "ok"          # anderer User darf


def test_speichern_limit_fuenfzehn(edb):
    for i in range(15):
        assert edb.ort_speichern(G, SID, U, f"o{i}", i, i, max_gespeichert=15) == "ok"
    assert edb.ort_speichern(G, SID, U, "zu viel", 1, 1, max_gespeichert=15) == "limit"
    assert edb.ort_loeschen(G, SID, U, "O3") is True
    assert edb.ort_speichern(G, SID, U, "zu viel", 1, 1, max_gespeichert=15) == "ok"


def test_ort_holen_nur_eigener_ort_auf_diesem_server(edb):
    edb.ort_speichern(G, SID, U, "Base", 1, 2)
    oid = edb.ort_nach_name(G, SID, U, "base")["id"]
    assert edb.ort_holen(G, SID, U, oid, "gespeichert") is not None
    assert edb.ort_holen(G, SID, U + 1, oid, "gespeichert") is None        # fremder User
    assert edb.ort_holen(G, "2000", U, oid, "gespeichert") is None         # fremder Server
    assert edb.ort_holen(G + 1, SID, U, oid, "gespeichert") is None        # fremde Guild
    assert edb.ort_holen(G, SID, U, oid, "auto") is None                   # falsche Art


# ── Ereignis -> Orte ─────────────────────────────────────────────────────
def test_ereignis_platzieren_verlinkter_spieler(edb):
    edb.link_user(G, U, "Rasant")
    conn = _Conn()
    bot._orte_aus_ereignis(conn, "rasant", (4000.0, 6000.0), "Fence Kit")
    a = edb.orte_liste(G, SID, U, "auto")
    assert len(a) == 1 and (a[0]["x"], a[0]["z"]) == (4000.0, 6000.0)
    assert edb.letzter_ort(G, SID, U)["x"] == 4000.0


def test_ereignis_ohne_platzieren_setzt_nur_letzten_ort(edb):
    edb.link_user(G, U, "Rasant")
    bot._orte_aus_ereignis(_Conn(), "Rasant", (4000.0, 6000.0), None)
    assert edb.orte_liste(G, SID, U, "auto") == []
    assert edb.letzter_ort(G, SID, U) is not None


def test_ereignis_unverlinkter_spieler_wird_ignoriert(edb):
    bot._orte_aus_ereignis(_Conn(), "Fremder", (4000.0, 6000.0), "Zaun")
    assert edb.orte_liste(G, SID, U, "auto") == [] and edb.letzter_ort(G, SID, U) is None


def test_ereignis_andere_guild_bekommt_nichts(edb):
    edb.link_user(999, U, "Rasant")                                       # verlinkt in einer Guild, die nicht zum Server gehoert
    bot._orte_aus_ereignis(_Conn(guilds=(G,)), "Rasant", (4000.0, 6000.0), "Zaun")
    assert edb.orte_liste(999, SID, U, "auto") == [] and edb.letzter_ort(999, SID, U) is None


def test_ereignis_ungueltige_position_ignoriert(edb):
    edb.link_user(G, U, "Rasant")
    for xz in ((0.0, 0.0), (-5.0, 100.0), (50000.0, 10.0)):
        bot._orte_aus_ereignis(_Conn(), "Rasant", xz, "Zaun")
    assert edb.orte_liste(G, SID, U, "auto") == []


def _bau(aktion, x, z, item="Fence Kit"):
    return {"type": "basebuild", "timestamp": "00:00:00", "player": "Rasant", "player_id": "ACC1",
            "aktion": aktion, "item": item, "position": f"{x}, {z}, 0", "raw": ""}


@pytest.fixture
def dispatch_umgebung(monkeypatch, edb):
    monkeypatch.setattr(bot.cfg, "guilds", {})
    edb.link_user(G, U, "Rasant")
    return _Conn()


def test_dispatch_placed_merkt_attached_nicht(edb, dispatch_umgebung):
    conn = dispatch_umgebung
    _run(bot.bot._dispatch(_bau("placed", 8678.1, 12813.9), conn))
    _run(bot.bot._dispatch(_bau("attached", 1000.0, 2000.0, "Wall"), conn))
    a = edb.orte_liste(G, SID, U, "auto")
    assert len(a) == 1 and round(a[0]["x"]) == 8678          # attached erzeugt keinen Vorschlag
    assert round(edb.letzter_ort(G, SID, U)["x"]) == 1000     # zaehlt aber als letzter bekannter Ort


def test_dispatch_diagnose_modus_ohne_nebenwirkungen(edb, dispatch_umgebung):
    _run(bot.bot._dispatch(_bau("placed", 8678.1, 12813.9), dispatch_umgebung, nebenwirkungen=False))
    assert edb.orte_liste(G, SID, U, "auto") == [] and edb.letzter_ort(G, SID, U) is None


# ── Letzter bekannter Ort ────────────────────────────────────────────────
def test_letzter_ort_live_position_schlaegt_alten_eintrag(edb):
    edb.link_user(G, U, "Rasant")
    conn = _Conn()
    edb.letzter_ort_setzen(G, SID, U, 100.0, 100.0)
    time.sleep(0.01)
    conn.parser.player_positions["Rasant"] = {"position": "<7000.0, 8000.0, 12.0>",
                                              "last_seen": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + 5))}
    o = bot._ort_letzter_bekannter(conn, G, U)
    assert (o["x"], o["z"]) == (7000.0, 8000.0)


def test_letzter_ort_nach_disconnect_bleibt_erhalten(edb):
    edb.link_user(G, U, "Rasant")
    edb.letzter_ort_setzen(G, SID, U, 100.0, 200.0)
    o = bot._ort_letzter_bekannter(_Conn(), G, U)
    assert (o["x"], o["z"]) == (100.0, 200.0)


def test_letzter_ort_fremder_spieler_wird_nicht_genommen(edb):
    edb.link_user(G, U, "Rasant")
    conn = _Conn()
    conn.parser.player_positions["Jemand Anderes"] = {"position": "<7000.0, 8000.0, 12.0>",
                                                      "last_seen": "2999-01-01T00:00:00"}
    assert bot._ort_letzter_bekannter(conn, G, U) is None


# ── Aufloesen / Vorschlaege ──────────────────────────────────────────────
def test_aufloesen_varianten(edb):
    edb.link_user(G, U, "Rasant")
    conn = _Conn()
    edb.letzter_ort_setzen(G, SID, U, 11.0, 22.0)
    edb.orte_auto_merken(G, SID, U, 3000, 4000)
    edb.ort_speichern(G, SID, U, "Base", 5000, 6000)
    aid = edb.orte_liste(G, SID, U, "auto")[0]["id"]
    sid_ = edb.ort_nach_name(G, SID, U, "Base")["id"]

    assert bot._ort_aufloesen(conn, G, U, "last", False)[0][:2] == (11.0, 22.0)
    assert bot._ort_aufloesen(conn, G, U, f"a:{aid}", False)[0][:2] == (3000.0, 4000.0)
    assert bot._ort_aufloesen(conn, G, U, f"s:{sid_}", False)[0][:2] == (5000.0, 6000.0)
    assert bot._ort_aufloesen(conn, G, U, "base", False)[0][:2] == (5000.0, 6000.0)   # frei getippter Name
    # falsche Art / fremder User / Unsinn
    assert bot._ort_aufloesen(conn, G, U, f"s:{aid}", False)[0] is None
    assert bot._ort_aufloesen(conn, G, U + 1, f"s:{sid_}", False)[0] is None
    assert bot._ort_aufloesen(conn, G, U + 1, "last", False)[0] is None
    assert bot._ort_aufloesen(conn, G, U, "gibtsnicht", False)[1].startswith("❌")
    assert bot._ort_aufloesen(conn, G, U, "gibtsnicht", True)[1].startswith("❌ Location")


def test_vorschlaege_reihenfolge(edb):
    edb.link_user(G, U, "Rasant")
    conn = _Conn()
    edb.letzter_ort_setzen(G, SID, U, 11.0, 22.0)
    edb.orte_auto_merken(G, SID, U, 3000, 4000)
    edb.ort_speichern(G, SID, U, "Base", 5000, 6000)
    werte = [w for _l, w in bot._ort_vorschlaege(conn, G, U, False)]
    assert werte[0] == "last" and werte[1].startswith("a:") and werte[2].startswith("s:")


# ── /location ────────────────────────────────────────────────────────────
def test_places_save_standard_ist_neuester_ort(monkeypatch, edb):
    edb.link_user(G, U, "Rasant")
    conn = _Conn()
    _use(monkeypatch, conn)
    edb.orte_auto_merken(G, SID, U, 3000, 4000)
    i = _Inter()
    _run(bot.places_save.callback(i, "Base"))
    r = edb.ort_nach_name(G, SID, U, "Base")
    assert r is not None and (r["x"], r["z"]) == (3000.0, 4000.0), i.text()


def test_places_save_xz_und_grenzen(monkeypatch, edb):
    _use(monkeypatch, _Conn())
    i = _Inter()
    _run(bot.places_save.callback(i, "Punkt", None, 4640.0, 10350.0))
    assert edb.ort_nach_name(G, SID, U, "Punkt") is not None
    for kw in ((None, 99999.0, 10.0), (None, -1.0, 10.0), (None, float("nan"), 10.0), (None, 10.0, None)):
        j = _Inter()
        _run(bot.places_save.callback(j, "Bad", *kw))
        assert edb.ort_nach_name(G, SID, U, "Bad") is None and "❌" in j.text()


def test_places_save_ort_und_xz_zusammen_abgelehnt(monkeypatch, edb):
    _use(monkeypatch, _Conn())
    i = _Inter()
    _run(bot.places_save.callback(i, "X", "last", 10.0, 10.0))
    assert edb.ort_nach_name(G, SID, U, "X") is None and "❌" in i.text()


def test_places_save_ohne_ort_fehlermeldung(monkeypatch, edb):
    _use(monkeypatch, _Conn())
    i = _Inter()
    _run(bot.places_save.callback(i, "X"))
    assert edb.ort_nach_name(G, SID, U, "X") is None and "Noch kein Ort" in i.text()


def test_places_save_name_validierung_doppelt_und_limit(monkeypatch, edb):
    _use(monkeypatch, _Conn())
    i = _Inter()
    _run(bot.places_save.callback(i, "Ba<se>", None, 1.0, 1.0))
    assert "Ungültiger Name" in i.text()
    _run(bot.places_save.callback(_Inter(), "Base", None, 1.0, 1.0))
    j = _Inter()
    _run(bot.places_save.callback(j, "base", None, 2.0, 2.0))
    assert "schon einen Ort" in j.text()
    for n in range(14):
        _run(bot.places_save.callback(_Inter(), f"p{n}", None, 1.0, 1.0))
    k = _Inter()
    _run(bot.places_save.callback(k, "zuviel", None, 1.0, 1.0))
    assert "15 gespeicherte" in k.text() and edb.ort_nach_name(G, SID, U, "zuviel") is None


def test_places_list_und_delete(monkeypatch, edb):
    edb.link_user(G, U, "Rasant")
    _use(monkeypatch, _Conn())
    edb.orte_auto_merken(G, SID, U, 3000, 4000)
    edb.ort_speichern(G, SID, U, "Base", 5000, 6000)
    i = _Inter()
    _run(bot.places_list.callback(i))
    t = i.text()
    assert "(1/5)" in t and "(1/15)" in t and "Base" in t
    d = _Inter()
    _run(bot.places_delete.callback(d, "base"))
    assert edb.ort_nach_name(G, SID, U, "Base") is None
    d2 = _Inter()
    _run(bot.places_delete.callback(d2, "base"))
    assert "keinen gespeicherten Ort" in d2.text()
    # ein anderer User kann fremde Orte nicht loeschen
    edb.ort_speichern(G, SID, U, "Mein", 1, 1)
    _run(bot.places_delete.callback(_Inter(uid=U + 1), "Mein"))
    assert edb.ort_nach_name(G, SID, U, "Mein") is not None


# ── /buy mit ort ─────────────────────────────────────────────────────────
class _Katalog:
    def find(self, name):
        return {"name": "Kiste", "price": 100, "enabled": True, "max_amount_per_buy": 5,
                "classnames": ["WoodenCrate"]}


class _Shop:
    def __init__(self):
        self.aufrufe = []

    async def add_purchase_entries(self, cls_list, amount, x, y, z):
        self.aufrufe.append((x, y, z))
        return True, "", ["A1"]

    def schedule_auto_restart(self):
        pass


@pytest.fixture
def kauf(monkeypatch, edb):
    edb.link_user(G, U, "Rasant")
    conn = _Conn()
    conn.catalog = _Katalog()
    conn.shop = _Shop()
    _use(monkeypatch, conn)
    edb.add_wallet(G, U, 1000)
    conn.start_guthaben = edb.get_balance(G, U)[0]       # Startguthaben haengt von der Konfiguration ab

    async def kein_feed(*a, **k):
        return None
    monkeypatch.setattr(bot, "_post_feed", kein_feed)
    monkeypatch.setattr(bot, "_item_classnames", lambda it: list(it["classnames"]))
    monkeypatch.setattr(bot, "_item_role_ids", lambda it: [])
    return conn, edb


def test_buy_mit_gespeichertem_ort(kauf):
    conn, edb = kauf
    edb.ort_speichern(G, SID, U, "Base", 5000, 6000)
    i = _Inter()
    _run(bot.cmd_buy.callback(i, "Kiste", 1, "Base"))
    assert conn.shop.aufrufe == [(5000.0, 0.0, 6000.0)], i.text()
    assert "Base" in i.text()
    assert edb.get_balance(G, U)[0] == conn.start_guthaben - 100


def test_buy_mit_letztem_bekannten_ort(kauf):
    conn, edb = kauf
    edb.letzter_ort_setzen(G, SID, U, 1234.0, 4321.0)
    _run(bot.cmd_buy.callback(_Inter(), "Kiste", 1, "last"))
    assert conn.shop.aufrufe == [(1234.0, 0.0, 4321.0)]


def test_buy_mit_xz_funktioniert_weiter(kauf):
    conn, _edb = kauf
    _run(bot.cmd_buy.callback(_Inter(), "Kiste", 1, None, 4640.0, 10350.0))
    assert conn.shop.aufrufe == [(4640.0, 0.0, 10350.0)]


def test_buy_ort_und_xz_zusammen_abgelehnt_nichts_abgebucht(kauf):
    conn, edb = kauf
    i = _Inter()
    _run(bot.cmd_buy.callback(i, "Kiste", 1, "last", 10.0, 10.0))
    assert conn.shop.aufrufe == [] and edb.get_balance(G, U)[0] == conn.start_guthaben and "nicht beides" in i.text()


def test_buy_ohne_ort_und_ohne_xz_abgelehnt(kauf):
    conn, edb = kauf
    i = _Inter()
    _run(bot.cmd_buy.callback(i, "Kiste", 1))
    assert conn.shop.aufrufe == [] and edb.get_balance(G, U)[0] == conn.start_guthaben and "/places save" in i.text()


def test_buy_fremder_ort_wird_abgelehnt(kauf):
    conn, edb = kauf
    edb.ort_speichern(G, SID, U + 1, "Fremd", 5000, 6000)
    fid = edb.ort_nach_name(G, SID, U + 1, "Fremd")["id"]
    for wert in (f"s:{fid}", "Fremd"):
        i = _Inter()
        _run(bot.cmd_buy.callback(i, "Kiste", 1, wert))
        assert "nicht gefunden" in i.text()
    assert conn.shop.aufrufe == [] and edb.get_balance(G, U)[0] == conn.start_guthaben


def test_buy_ort_von_anderem_server_wird_abgelehnt(kauf):
    conn, edb = kauf
    edb.ort_speichern(G, "2000", U, "Andere", 5000, 6000)
    oid = edb.ort_nach_name(G, "2000", U, "Andere")["id"]
    i = _Inter()
    _run(bot.cmd_buy.callback(i, "Kiste", 1, f"s:{oid}"))
    assert conn.shop.aufrufe == [] and "nicht gefunden" in i.text()
