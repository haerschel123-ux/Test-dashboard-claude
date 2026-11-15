"""Verifizierung (Discord Management) mit Zweitaccount-Erkennung über alle Kunden – Vorbild Double Counter.

Geprüft werden die Datenbank (Links, Konten, Signale, Gruppen, Status, Ereignisse, Suche, Datenschutz), die Einstellungen,
Hash/IP-Behandlung, die VPN-Abfrage (Netz immer gestubbt), die Prüf-Reihenfolge samt Aktionen, die öffentliche
Verifizier-Seite (Einmal-Link, Rate-Limit, Wettläufe), das Dashboard samt Mandanten-Trennung, Lens und die Slash-Befehle.
Kein Test geht ins Internet oder zu Discord.

    python3 -m pytest tests/test_verifizierung.py -q
"""
import asyncio
import json
import os
import re
import sys
import time

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
from test_spieler_dashboard import G_A, G_B, _session, api, welt  # noqa: F401 - Fixtures
from test_airstrike import servers  # noqa: F401 - Fixture (zwei Server 1000/2000)
from test_gluecksrad import _admin  # noqa: F401 - Dashboard-Anmeldung

TAG = 86400


def sf(tage_alt, n=0):
    """Discord-Snowflake eines Kontos, das ``tage_alt`` Tage alt ist."""
    ts = time.time() - tage_alt * TAG
    return ((int(ts * 1000) - 1420070400000) << 22) + n


HAUPT, ALT, DRITT, VIERT, NEU, ADMIN, STAFF = (sf(400, 1), sf(300, 2), sf(200, 3), sf(150, 4), sf(2, 5), sf(500, 6),
                                                sf(500, 7))
OWNER = sf(600, 8)


def ids(gid):
    b = 1000 if gid == G_A else 2000
    return {"verif": b + 1, "log": b + 2, "ver": b + 11, "unv": b + 12, "ping": b + 13, "staff": b + 14, "hoch": b + 15}


class _Antwort:
    status, reason = 403, "x"


def _forbidden():
    return discord.Forbidden(_Antwort(), "nein")


class VRolle:
    def __init__(self, id_, pos=1, managed=False):
        self.id, self.position, self.managed, self.name = id_, pos, managed, f"Rolle{id_}"
        self.mention = f"<@&{id_}>"

    def is_default(self):
        return False


class Rechte:
    def __init__(self, admin=False):
        self.administrator, self.manage_roles, self.view_audit_log = admin, True, True


class VKanal:
    def __init__(self, id_, kaputt=False):
        self.id, self.gesendet, self.kaputt, self.mention = id_, [], kaputt, f"<#{id_}>"

    async def send(self, content=None, embed=None, view=None, **kw):
        if self.kaputt:
            raise _forbidden()
        self.gesendet.append({"content": content, "embed": embed, "view": view, **kw})


class VMitglied:
    def __init__(self, uid, guild, rollen=(), admin=False, top=1, dms=True):
        self.id, self.guild, self.roles = uid, guild, list(rollen)
        self.guild_permissions, self.top_role, self.dms = Rechte(admin), VRolle(0, top), dms
        self.dm, self.bot, self.mention, self.name = [], False, f"<@{uid}>", f"Nutzer{uid}"

    def __str__(self):
        return self.name

    async def add_roles(self, *rollen, reason=None):
        self.roles.extend(rollen)

    async def remove_roles(self, *rollen, reason=None):
        weg = {r.id for r in rollen}
        self.roles = [r for r in self.roles if r.id not in weg]

    async def kick(self, reason=None):
        self.guild.gekickt.append(self.id)

    async def send(self, embed=None, view=None, **kw):
        if not self.dms:
            raise _forbidden()
        self.dm.append({"embed": embed, "view": view})


class VGuild:
    def __init__(self, gid, name=None):
        i = ids(gid)
        self.id, self.name, self.owner_id = gid, name or f"Guild {gid}", OWNER
        self.gekickt, self.gebannt = [], []
        self.roles = {i[k]: VRolle(i[k], 5) for k in ("ver", "unv", "ping", "staff")}
        self.roles[i["hoch"]] = VRolle(i["hoch"], 200)
        self.channels = {i["verif"]: VKanal(i["verif"]), i["log"]: VKanal(i["log"])}
        self.me = VMitglied(1, self, admin=False, top=100)
        self.members = {}
        for uid in (HAUPT, ALT, DRITT, VIERT, NEU, OWNER):
            self.members[uid] = VMitglied(uid, self)
        self.members[ADMIN] = VMitglied(ADMIN, self, admin=True)
        self.members[STAFF] = VMitglied(STAFF, self, rollen=[self.roles[i["staff"]]])

    def get_member(self, uid):
        return self.members.get(int(uid))

    async def fetch_member(self, uid):
        class _R:
            status, reason = 404, "nf"
        raise discord.NotFound(_R(), "Unknown Member")

    def get_role(self, rid):
        return self.roles.get(int(rid))

    def get_channel(self, cid):
        return self.channels.get(int(cid))

    async def ban(self, member, reason=None, delete_message_seconds=0):
        self.gebannt.append(member.id)


class VBot:
    def __init__(self, guilds):
        self._g = guilds
        self.guilds = list(guilds.values())

    def get_guild(self, gid):
        return self._g.get(int(gid))

    def is_ready(self):
        return True


class _KeinNetz:
    """Ersetzt aiohttp.ClientSession: jeder Versuch, ins Netz zu gehen, fällt auf."""
    def __init__(self, *a, **kw):
        raise AssertionError("Netzwerkzugriff im Test")


@pytest.fixture
def v(welt, monkeypatch):
    guilds = {G_A: VGuild(G_A), G_B: VGuild(G_B)}
    monkeypatch.setattr(bot, "bot", VBot(guilds))
    bot.cfg.config["verifizierung_geheimnis"] = "s" * 64
    bot.cfg.config["verifizierung_url"] = "https://verify.example"
    gespeichert = []
    monkeypatch.setattr(bot.cfg, "save_config", lambda *a, **kw: gespeichert.append(1))
    monkeypatch.setattr(bot, "_VERIF_LOCKS", {})
    monkeypatch.setattr(bot, "_VERIF_VPN_CACHE", {})
    monkeypatch.setattr(bot, "_VERIF_VPN_ZAEHLER", {"tag": "", "n": 0})
    monkeypatch.setattr(bot, "_VERIF_SEITE_LIMIT", {})
    monkeypatch.setattr(bot.aiohttp, "ClientSession", _KeinNetz)
    welt.update({"g": guilds, "gespeichert": gespeichert})
    return welt


def einstellen(v, gid=G_A, **kw):
    i = ids(gid)
    d = {"enabled": True, "modus": "panel", "channel_id": str(i["verif"]), "log_channel_id": str(i["log"]),
         "verified_role_ids": [str(i["ver"])], "unverified_role_id": str(i["unv"]), "ping_role_id": str(i["ping"]),
         "staff_role_ids": [str(i["staff"])], "aktion_alt": "blockieren", "aktion_vpn": "blockieren"}
    d.update(kw)
    sicht = bot.guild_sicht(v["a"] if gid == G_A else v["b"], gid)
    bot._conn_store(sicht, "verifizierung", d)
    return sicht


def pruefen(v, uid, ip="81.2.69.5", geraete=(), gid=G_A, jetzt=None):
    return asyncio.run(bot._verif_pruefen(v["g"][gid], gid, uid, f"Nutzer{uid}", ip, list(geraete), jetzt))


def status(gid, uid):
    s = bot.db.verif_status(gid, uid)
    return s["status"] if s else None


def hat_rolle(v, gid, uid, schluessel="ver"):
    return any(r.id == ids(gid)[schluessel] for r in v["g"][gid].members[uid].roles)


def log_kanal(v, gid=G_A):
    return v["g"][gid].channels[ids(gid)["log"]].gesendet


GERAET = "geraet_ABCDEFGHIJKLMNOP"


# ══ Datenbank ════════════════════════════════════════════════════════════════
@pytest.fixture
def db(tmp_path):
    return bot.EconomyDB(str(tmp_path / "v.db"))


def test_db_token_einmalig_und_ablauf(db):
    t = db.verif_token_neu(G_A, "1000", 5, 10)
    assert re.fullmatch(r"[A-Za-z0-9_-]{20,64}", t)
    zeile = db.verif_token_holen(t)
    assert (zeile["guild_id"], zeile["service_id"], zeile["user_id"], zeile["benutzt"]) == (G_A, "1000", 5, 0)
    assert zeile["ablauf"] - zeile["erstellt"] == pytest.approx(600)
    assert db.verif_token_holen("gibtsnicht") is None
    assert not db.verif_token_verbrauchen("gibtsnicht")
    assert not db.verif_token_verbrauchen(t, jetzt=time.time() + 601)      # abgelaufen
    assert db.verif_token_verbrauchen(t)
    assert not db.verif_token_verbrauchen(t)                                 # nur einmal
    assert db.verif_token_holen(t)["benutzt"] == 1


def test_db_token_neu_loescht_lange_abgelaufene(db):
    alt = db.verif_token_neu(G_A, "1000", 5, 2)
    db._conn.execute("UPDATE verif_token SET ablauf=? WHERE token=?", (time.time() - 3700, alt))
    db._conn.commit()
    neu = db.verif_token_neu(G_A, "1000", 5, 2)
    assert db.verif_token_holen(alt) is None and db.verif_token_holen(neu) is not None


def test_db_konto_merken_behaelt_erst_ts(db):
    db.verif_konto_merken(5, "Erst", jetzt=100.0)
    db.verif_konto_merken(5, "x" * 300, jetzt=200.0)
    k = db.verif_konto(5)
    assert (k["erst_ts"], k["letzt_ts"], len(k["name"])) == (100.0, 200.0, 100)


def test_db_gruppe_direkt_umweg_seit_grenze_und_zusatz(db):
    for u in (1, 2, 3, 4):
        db.verif_konto_merken(u, f"u{u}")
    db.verif_signal_merken(1, "ip", "ip1", jetzt=1000.0)
    db.verif_signal_merken(2, "ip", "ip1", jetzt=1000.0)
    db.verif_signal_merken(2, "geraet", "g1", jetzt=1000.0)
    db.verif_signal_merken(3, "geraet", "g1", jetzt=1000.0)
    db.verif_signal_merken(4, "ip", "ip9", jetzt=10.0)                    # alt
    assert db.verif_gruppe(1) == [1, 2, 3]                                   # A-B über IP, B-C über Gerät
    assert db.verif_gruppe(3) == [1, 2, 3]
    assert db.verif_gruppe(4) == [4]
    assert db.verif_gruppe(99) == [99]                                       # unbekannt: nur sich selbst
    assert db.verif_gruppe(99, zusatz=[("ip", "ip9")]) == [4, 99]            # noch nicht gespeicherte Signale
    assert db.verif_gruppe(99, zusatz=[("ip", "ip9")], seit=500.0) == [99]   # zu alt
    assert db.verif_gruppe(1, seit=2000.0) == [1]
    assert len(db.verif_gruppe(1, grenze=2)) == 2
    # Art zählt mit: gleicher Wert unter anderer Art verbindet nicht
    assert db.verif_gruppe(99, zusatz=[("geraet", "ip1")]) == [99]


def test_db_gruppe_grosse_kette_bleibt_unter_grenze(db):
    for u in range(1, 300):
        db.verif_signal_merken(u, "ip", f"k{u}")
        db.verif_signal_merken(u + 1, "ip", f"k{u}")
    g = db.verif_gruppe(1, grenze=50)
    assert len(g) == 50 and 1 in g


def test_db_status_liste_zaehlen(db):
    db.verif_status_setzen(G_A, 1, "offen", jetzt=100.0)
    db.verif_status_setzen(G_A, 2, "offen", jetzt=300.0)
    db.verif_status_setzen(G_A, 3, "verifiziert", jetzt=50.0)
    db.verif_status_setzen(G_B, 4, "offen", jetzt=10.0)
    db.verif_status_setzen(G_A, 1, "offen", "x" * 400, haupt_id=7, jetzt=110.0)   # Upsert
    assert [s["user_id"] for s in db.verif_status_liste(G_A, "offen")] == [1, 2]
    assert [s["user_id"] for s in db.verif_status_liste(G_A, "offen", vor=200.0)] == [1]
    assert db.verif_status_zaehlen(G_A) == {"offen": 2, "verifiziert": 1}
    s = db.verif_status(G_A, 1)
    assert (s["ts"], s["haupt_id"], len(s["grund"])) == (110.0, 7, 300)
    assert db.verif_status(G_B, 1) is None


def test_db_ereignisse_guild_filter_nur_eigene_und_geteilte(db):
    db.verif_ereignis_add(G_A, 1, "ban", teilen=False)
    db.verif_ereignis_add(G_B, 1, "ban", teilen=False)
    db.verif_ereignis_add(G_B, 1, "kick", teilen=True)
    db.verif_ereignis_add(G_A, 2, "alt", teilen=True)
    alle = db.verif_ereignisse([1])
    assert len(alle) == 3
    sicht_a = db.verif_ereignisse([1], guild_id=G_A)
    assert sorted((e["guild_id"], e["art"]) for e in sicht_a) == [(G_A, "ban"), (G_B, "kick")]
    assert db.verif_ereignisse([]) == []


def test_db_suche_nur_diese_guild_und_like_maskiert(db):
    for uid, name in ((1, "a%b"), (2, "a_b"), (3, "axb"), (4, "a\\b"), (5, "Zorro")):
        db.verif_konto_merken(uid, name)
        db.verif_status_setzen(G_A, uid, "verifiziert")
    db.verif_konto_merken(6, "a%b fremd")
    db.verif_status_setzen(G_B, 6, "verifiziert")
    db.verif_konto_merken(7, "ohne Status a%b")
    namen = lambda q: sorted(r["name"] for r in db.verif_suche(G_A, q))  # noqa: E731
    assert namen("%") == ["a%b"]
    assert namen("_") == ["a_b"]
    assert namen("\\") == ["a\\b"]
    assert namen("a") == ["a%b", "a\\b", "a_b", "axb"]
    assert namen("zor") == ["Zorro"]                       # LIKE ohne Groß-/Kleinschreibung
    assert len(db.verif_suche(G_A, "")) == 5
    assert [r["user_id"] for r in db.verif_suche(G_A, "2")] == [2]
    assert db.verif_suche(G_A, "6") == [] and db.verif_suche(G_A, "7") == []
    assert db.verif_suche(G_A, "'; DROP TABLE verif_status; --") == []
    assert len(db.verif_suche(G_A, "")) == 5
    assert len(db.verif_suche(G_A, "", limit=2)) == 2


def test_db_aufraeumen(db):
    db.verif_signal_merken(1, "ip", "a", jetzt=10.0)
    db.verif_signal_merken(2, "ip", "a", jetzt=1000.0)
    db.verif_status_setzen(G_A, 1, "alt", haupt_id=2)
    assert db.verif_aufraeumen(500.0) == 1
    assert db.verif_gruppe(2) == [2] and db.verif_status(G_A, 1)["haupt_id"] == 2


def test_db_lens_setzen_legt_an_und_behaelt_namen(db):
    db.verif_lens_setzen(5, True)
    assert db.verif_konto(5)["lens_aus"] == 1
    db.verif_konto_merken(5, "Name")
    db.verif_lens_setzen(5, False)
    k = db.verif_konto(5)
    assert (k["lens_aus"], k["name"]) == (0, "Name")


def test_db_export_ohne_hash_und_loeschen(db):
    db.verif_konto_merken(1, "Eins")
    db.verif_konto_merken(2, "Zwei")
    db.verif_signal_merken(1, "ip", "GEHEIMERHASH")
    db.verif_status_setzen(G_A, 1, "verifiziert")
    db.verif_status_setzen(G_A, 2, "alt", haupt_id=1)
    db.verif_ereignis_add(G_A, 2, "alt", haupt_id=1)
    db.verif_ereignis_add(G_A, 1, "verifiziert")
    db.verif_token_neu(G_A, "1000", 1, 10)
    exp = db.verif_export(1)
    assert "GEHEIMERHASH" not in json.dumps(exp) and exp["signale"][0]["art"] == "ip" and "wert" not in exp["signale"][0]
    assert exp["konto"]["name"] == "Eins" and len(exp["status"]) == 1 and len(exp["ereignisse"]) == 1
    db.verif_daten_loeschen(1)
    leer = db.verif_export(1)
    assert leer == {"konto": None, "signale": [], "status": [], "ereignisse": []}
    assert db.verif_status(G_A, 2)["haupt_id"] is None
    assert db.verif_ereignisse([2])[0]["haupt_id"] is None
    assert db._conn.execute("SELECT COUNT(*) FROM verif_token WHERE user_id=1").fetchone()[0] == 0


# ══ Einstellungen ════════════════════════════════════════════════════════════
class _Sicht(dict):
    pass


def test_einstellungen_vorgaben():
    for sicht in (None, _Sicht(), _Sicht(verifizierung="kaputt")):
        e = bot._verif_einstellungen(sicht)
        assert e["enabled"] is False and e["modus"] == "panel" and e["aktion_alt"] == "blockieren"
        assert e["aktion_vpn"] == "blockieren" and e["link_minuten"] == 10 and e["min_alter_tage"] == 0
        assert e["verified_role_ids"] == [] and e["channel_id"] is None and e["lens_teilen"] is True
        assert e["log_erfolg"] is True and e["proxycheck_key"] == "" and e["nachricht"] == ""


@pytest.mark.parametrize("feld,roh,erwartet", [
    ("modus", "x", "panel"), ("modus", "blind", "blind"), ("aktion_alt", "nuke", "blockieren"),
    ("aktion_alt", "bannen", "bannen"), ("aktion_vpn", "bannen", "blockieren"),
    ("min_alter_tage", -1, 0), ("min_alter_tage", 1.5, 0), ("min_alter_tage", True, 0), ("min_alter_tage", "5", 0),
    ("min_alter_tage", float("nan"), 0), ("min_alter_tage", 366, 0), ("min_alter_tage", 7.0, 7),
    ("link_minuten", 1, 10), ("link_minuten", 31, 10), ("link_minuten", 30, 30), ("kick_minuten", 1441, 0),
    ("channel_id", "abc", None), ("channel_id", 0, None), ("channel_id", True, None), ("channel_id", "-5", None),
    ("channel_id", " 123 ", "123"), ("channel_id", 123, "123"),
    ("verified_role_ids", ["1", "1", "x", 2, False], ["1", "2"]), ("verified_role_ids", "1", []),
    ("verified_role_ids", [str(i) for i in range(1, 20)], [str(i) for i in range(1, 11)]),
    ("nachricht", 5, ""), ("nachricht", "  hi  ", "hi"), ("nachricht", "x" * 2000, "x" * 1000),
    ("proxycheck_key", "k" * 100, "k" * 64), ("proxycheck_key", None, ""),
])
def test_einstellungen_kaputte_werte(feld, roh, erwartet):
    assert bot._verif_einstellungen(_Sicht(verifizierung={feld: roh}))[feld] == erwartet


def test_guild_sicht_nur_eingeschaltet(v):
    assert bot._verif_guild_sicht(G_A) == (None, None)
    einstellen(v, enabled=False)
    assert bot._verif_guild_sicht(G_A) == (None, None)
    einstellen(v)
    sicht, e = bot._verif_guild_sicht(G_A)
    assert sicht.guild_id == G_A and e["enabled"]
    assert bot._verif_guild_sicht(G_B) == (None, None)            # B hat nichts eingeschaltet
    assert bot._verif_guild_sicht(123) == (None, None)


# ══ Hash, IP, Kontoalter ═════════════════════════════════════════════════════
def test_geheimnis_wird_erzeugt_und_bleibt(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {})
    gespeichert = []
    monkeypatch.setattr(bot.cfg, "save_config", lambda *a, **kw: gespeichert.append(1))
    h1 = bot._verif_hash("ip", "1.2.3.4")
    schluessel = bot.cfg.config["verifizierung_geheimnis"]
    assert len(schluessel) >= 32 and gespeichert == [1]
    assert bot._verif_hash("ip", "1.2.3.4") == h1 and gespeichert == [1]
    assert bot._verif_hash("geraet", "1.2.3.4") != h1
    assert "1.2.3.4" not in h1 and len(h1) == 64
    monkeypatch.setattr(bot.cfg, "config", {"verifizierung_geheimnis": "zu-kurz"})
    bot._verif_hash("ip", "x")
    assert len(bot.cfg.config["verifizierung_geheimnis"]) >= 32


def test_geheimnis_speicherfehler_bricht_nicht_ab(monkeypatch):
    monkeypatch.setattr(bot.cfg, "config", {})

    def kaputt(*a, **kw):
        raise OSError("voll")
    monkeypatch.setattr(bot.cfg, "save_config", kaputt)
    assert len(bot._verif_hash("ip", "x")) == 64


@pytest.mark.parametrize("roh,erwartet", [
    ("1.2.3.4", "1.2.3.4"), (" 1.2.3.4 ", "1.2.3.4"), ("2a00:1450:1:2:3:4:5:6", "2a00:1450:1:2::/64"),
    ("2a00:1450:1:2:ffff::1", "2a00:1450:1:2::/64"), ("::ffff:1.2.3.4", "1.2.3.4"), ("unsinn", None), ("", None),
    ("1.2.3.4.5", None), ("999.1.1.1", None), ("1.2.3.4/24", None), (None, None),
])
def test_ip_norm(roh, erwartet):
    assert bot._verif_ip_norm(roh) == erwartet


class _Req:
    def __init__(self, remote, headers=None):
        self.remote, self.headers = remote, headers or {}


@pytest.mark.parametrize("remote,kopf,erwartet", [
    ("91.198.174.7", {"CF-Connecting-IP": "1.1.1.1", "X-Forwarded-For": "2.2.2.2"}, "91.198.174.7"),   # von außen: ignoriert
    ("10.0.0.5", {"X-Forwarded-For": "2.2.2.2"}, "10.0.0.5"),
    ("127.0.0.1", {"CF-Connecting-IP": "1.1.1.1", "X-Forwarded-For": "2.2.2.2"}, "1.1.1.1"),
    ("127.0.0.1", {"X-Forwarded-For": "2.2.2.2, 3.3.3.3"}, "2.2.2.2"),
    ("::1", {"X-Forwarded-For": "2a00:1450::1"}, "2a00:1450::1"),
    ("127.0.0.1", {"CF-Connecting-IP": "quatsch", "X-Forwarded-For": "4.4.4.4"}, "4.4.4.4"),
    ("127.0.0.1", {"CF-Connecting-IP": "quatsch"}, "127.0.0.1"),
    ("127.0.0.1", {}, "127.0.0.1"), (None, {"X-Forwarded-For": "2.2.2.2"}, None), ("", {}, None),
])
def test_client_ip_header_nur_von_loopback(remote, kopf, erwartet):
    assert bot._verif_client_ip(_Req(remote, kopf)) == erwartet


def test_konto_alter_aus_snowflake():
    assert bot._verif_konto_alter_tage(sf(30)) == pytest.approx(30, abs=0.01)
    jetzt = 1420070400.0 + 10 * TAG
    assert bot._verif_konto_alter_tage(0, jetzt) == pytest.approx(10)
    assert bot._verif_konto_alter_tage(sf(-5)) == 0.0              # Zukunft -> 0, nie negativ


# ══ VPN (proxycheck.io, Netz gestubbt) ════════════════════════════════════════
class _Netz:
    """Doppel für aiohttp.ClientSession: zeichnet URLs auf und liefert ``antwort`` (Exception = Fehler)."""
    def __init__(self, antwort):
        self.antwort, self.urls = antwort, []

    def __call__(self, *a, **kw):
        netz = self

        class _Resp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self, content_type=None):
                a = netz.antwort(netz.urls[-1]) if callable(netz.antwort) else netz.antwort
                if isinstance(a, Exception):
                    raise a
                return a

        class _Sess:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, url):
                netz.urls.append(url)
                return _Resp()
        return _Sess()


def _proxy(ja):
    return lambda url: {"status": "ok", url.split("/v2/")[1].split("?")[0]: {"proxy": "yes" if ja else "no"}}


def vpn(ip, key=""):
    return asyncio.run(bot._verif_vpn(ip, key))


def test_vpn_private_und_unsinn_ohne_netz(v):
    for ip in ("10.0.0.1", "192.168.1.1", "127.0.0.1", "::1", "fe80::1", "100.64.0.1"):
        assert vpn(ip) is False, ip
    assert vpn("unsinn") is None


def test_vpn_ja_nein_und_cache(v, monkeypatch):
    netz = _Netz(_proxy(True))
    monkeypatch.setattr(bot.aiohttp, "ClientSession", netz)
    assert vpn("8.8.8.8") is True
    assert vpn("8.8.8.8") is True and len(netz.urls) == 1               # Cache
    netz.antwort = _proxy(False)
    assert vpn("1.1.1.1") is False
    assert netz.urls[0] == "https://proxycheck.io/v2/8.8.8.8?vpn=1"
    # IPv6: abgefragt wird die Netzadresse des /64
    netz.antwort = _proxy(False)
    assert vpn("2606:4700:1:2:3:4:5:6") is False and "/v2/2606:4700:1:2::?" in netz.urls[-1]


@pytest.mark.parametrize("antwort", [
    {"status": "error", "8.8.8.8": {"proxy": "yes"}}, {"status": "ok"}, "kaputt", [], OSError("weg"),
    asyncio.TimeoutError(), {"status": "ok", "8.8.8.8": "yes"}])
def test_vpn_fehler_ergibt_none_und_wird_nicht_gecacht(v, monkeypatch, antwort):
    netz = _Netz(antwort)
    monkeypatch.setattr(bot.aiohttp, "ClientSession", netz)
    assert vpn("8.8.8.8") is None
    assert "8.8.8.8" not in bot._VERIF_VPN_CACHE


def test_vpn_tageslimit_ohne_schluessel(v, monkeypatch):
    netz = _Netz(_proxy(False))
    monkeypatch.setattr(bot.aiohttp, "ClientSession", netz)
    for i in range(100):
        assert vpn(f"8.8.{i // 250}.{i % 250 + 1}") is False
    assert vpn("9.9.9.9") is None and len(netz.urls) == 100          # Kontingent weg -> nicht blockieren
    # mit eigenem Schlüssel des Kunden weiter, Schlüssel in der URL
    assert vpn("9.9.9.9", "KUNDE123") is False and netz.urls[-1].endswith("&key=KUNDE123")
    # neuer Tag -> Zähler zurück
    bot._VERIF_VPN_ZAEHLER["tag"] = "1999-01-01"
    assert vpn("9.9.9.8") is False


def test_vpn_betreiber_schluessel(v, monkeypatch):
    netz = _Netz(_proxy(True))
    monkeypatch.setattr(bot.aiohttp, "ClientSession", netz)
    bot.cfg.config["verifizierung_proxycheck_key"] = "BETREIBER1"
    assert vpn("8.8.8.8") is True and netz.urls[-1].endswith("&key=BETREIBER1")
    assert bot._VERIF_VPN_ZAEHLER["n"] == 0
    assert vpn("8.8.4.4", "EIGEN1") is True and netz.urls[-1].endswith("&key=EIGEN1")


# ══ Prüfen ═══════════════════════════════════════════════════════════════════
def test_pruefen_aus(v):
    assert pruefen(v, HAUPT)["ergebnis"] == "aus"
    assert bot.db.verif_konto(HAUPT) is None


def test_pruefen_erstes_konto_verifiziert_rollen_und_log(v):
    einstellen(v)
    g = v["g"][G_A]
    g.members[HAUPT].roles.append(g.roles[ids(G_A)["unv"]])
    erg = pruefen(v, HAUPT, geraete=[GERAET])
    assert erg["ergebnis"] == "verifiziert" and erg["de"] and erg["en"]
    assert status(G_A, HAUPT) == "verifiziert"
    assert hat_rolle(v, G_A, HAUPT) and not hat_rolle(v, G_A, HAUPT, "unv")
    assert len(log_kanal(v)) == 1 and log_kanal(v)[0]["content"] is None          # Erfolg: kein Ping
    sig = bot.db._conn.execute("SELECT art, wert FROM verif_signal WHERE user_id=?", (HAUPT,)).fetchall()
    assert sorted(r["art"] for r in sig) == ["geraet", "ip"]
    assert all("81.2.69.5" not in r["wert"] and GERAET not in r["wert"] for r in sig)      # nur Hash


def test_pruefen_ohne_erfolgs_log(v):
    einstellen(v, log_erfolg=False)
    assert pruefen(v, HAUPT)["ergebnis"] == "verifiziert" and log_kanal(v) == []


def test_pruefen_rolle_fehlt_meldet_fehler(v):
    einstellen(v)
    del v["g"][G_A].roles[ids(G_A)["ver"]]
    erg = pruefen(v, HAUPT)
    assert erg["ergebnis"] == "fehler" and status(G_A, HAUPT) == "verifiziert"
    assert "Rolle" in json.dumps(log_kanal(v)[0]["embed"].to_dict(), ensure_ascii=False)


def test_pruefen_log_channel_fehlt_oder_kaputt_kein_absturz(v):
    einstellen(v, log_channel_id="424242")
    assert pruefen(v, HAUPT)["ergebnis"] == "verifiziert"
    einstellen(v)
    v["g"][G_A].channels[ids(G_A)["log"]].kaputt = True
    assert pruefen(v, ALT, ip="81.2.69.99")["ergebnis"] == "verifiziert"


def test_pruefen_zu_jung_und_manuell_ueberspringt(v):
    einstellen(v, min_alter_tage=7)
    erg = pruefen(v, NEU)
    assert erg["ergebnis"] == "zu_jung" and "5 Tag" in erg["de"] and status(G_A, NEU) == "zu_jung"
    assert not hat_rolle(v, G_A, NEU)
    assert bot.db._conn.execute("SELECT COUNT(*) FROM verif_signal WHERE user_id=?", (NEU,)).fetchone()[0] == 0
    bot.db.verif_status_setzen(G_A, NEU, "manuell")
    assert pruefen(v, NEU)["ergebnis"] == "verifiziert" and status(G_A, NEU) == "manuell"
    assert pruefen(v, HAUPT, ip="91.198.174.1")["ergebnis"] == "verifiziert"     # alt genug


def test_pruefen_zweitaccount_blockieren(v):
    einstellen(v)
    assert pruefen(v, HAUPT)["ergebnis"] == "verifiziert"
    erg = pruefen(v, ALT)
    assert erg["ergebnis"] == "alt"
    s = bot.db.verif_status(G_A, ALT)
    assert (s["status"], s["haupt_id"]) == ("alt", HAUPT)
    assert not hat_rolle(v, G_A, ALT) and v["g"][G_A].gekickt == [] and v["g"][G_A].gebannt == []
    letzte = log_kanal(v)[-1]
    assert letzte["content"] == f"<@&{ids(G_A)['ping']}>"                          # Ping nur bei Zweitaccount
    assert str(HAUPT) in json.dumps(letzte["embed"].to_dict())
    assert [e["art"] for e in bot.db.verif_ereignisse([ALT])] == ["alt"]
    # erneuter Versuch bleibt Zweitaccount
    assert pruefen(v, ALT)["ergebnis"] == "alt"


def test_pruefen_ping_ohne_ping_rolle(v):
    einstellen(v, ping_role_id=None)
    pruefen(v, HAUPT)
    pruefen(v, ALT)
    assert log_kanal(v)[-1]["content"] is None


def test_pruefen_zweitaccount_keine_aktion_verifiziert_aber_markiert(v):
    einstellen(v, aktion_alt="keine")
    pruefen(v, HAUPT)
    erg = pruefen(v, ALT)
    s = bot.db.verif_status(G_A, ALT)
    assert erg["ergebnis"] == "verifiziert" and (s["status"], s["haupt_id"]) == ("verifiziert", HAUPT)
    assert hat_rolle(v, G_A, ALT) and log_kanal(v)[-1]["content"] == f"<@&{ids(G_A)['ping']}>"
    assert "Zweitaccount" in log_kanal(v)[-1]["embed"].title


@pytest.mark.parametrize("aktion,liste", [("kicken", "gekickt"), ("bannen", "gebannt")])
def test_pruefen_zweitaccount_kicken_bannen(v, aktion, liste):
    einstellen(v, aktion_alt=aktion)
    pruefen(v, HAUPT)
    assert pruefen(v, ALT)["ergebnis"] == "alt"
    assert getattr(v["g"][G_A], liste) == [ALT]
    assert aktion in json.dumps(log_kanal(v)[-1]["embed"].to_dict())


def test_strafe_nie_gegen_admin_oder_rolle_ueber_bot(v):
    einstellen(v, aktion_alt="bannen")
    g = v["g"][G_A]
    pruefen(v, HAUPT)
    assert pruefen(v, ADMIN)["ergebnis"] == "alt" and g.gebannt == []
    g.members[DRITT].top_role = VRolle(0, 100)                           # gleich hoch wie der Bot
    assert pruefen(v, DRITT)["ergebnis"] == "alt" and g.gebannt == []
    assert "ausgenommen" in json.dumps(log_kanal(v)[-1]["embed"].to_dict(), ensure_ascii=False)
    assert asyncio.run(bot._verif_strafe(g, 424242, "bannen", "x")) == "nicht mehr Mitglied"
    assert asyncio.run(bot._verif_strafe(g, ALT, "blockieren", "x")) == ""

    async def verboten(**kw):
        raise _forbidden()
    g.members[ALT].kick = verboten
    assert asyncio.run(bot._verif_strafe(g, ALT, "kicken", "x")) == "keine Berechtigung"


def test_pruefen_global_ueber_guilds_und_kunden(v):
    einstellen(v, G_A)
    einstellen(v, G_B)
    assert pruefen(v, HAUPT, gid=G_A)["ergebnis"] == "verifiziert"
    assert pruefen(v, ALT, gid=G_B)["ergebnis"] == "alt"
    assert bot.db.verif_status(G_B, ALT)["haupt_id"] == HAUPT
    # Der Log in B nennt den Hauptaccount, aber nicht die fremde Guild
    text = json.dumps(log_kanal(v, G_B)[-1]["embed"].to_dict(), ensure_ascii=False)
    assert str(HAUPT) in text and str(G_A) not in text and "Guild" not in text
    assert log_kanal(v, G_A)[-1]["embed"].title.startswith("✅")             # A bekommt nichts vom Zweitaccount in B


def test_pruefen_ueber_umweg_geraet(v):
    einstellen(v)
    pruefen(v, HAUPT, ip="81.2.69.1", geraete=[GERAET])
    pruefen(v, DRITT, ip="81.2.69.2", geraete=[])                     # unabhängig
    assert status(G_A, DRITT) == "verifiziert"
    assert pruefen(v, ALT, ip="81.2.69.3", geraete=[GERAET])["ergebnis"] == "alt"
    # VIERT teilt nur die IP von ALT -> über ALT mit HAUPT verbunden
    assert pruefen(v, VIERT, ip="81.2.69.3")["ergebnis"] == "alt"
    assert bot.db.verif_status(G_A, VIERT)["haupt_id"] == HAUPT


def test_pruefen_ipv6_gleiches_64_netz_ist_verbunden(v):
    einstellen(v)
    pruefen(v, HAUPT, ip="2a00:1450:1:2::1")
    assert pruefen(v, ALT, ip="2a00:1450:1:2:aaaa::9")["ergebnis"] == "alt"
    assert pruefen(v, DRITT, ip="2a00:1450:1:3::1")["ergebnis"] == "verifiziert"


def test_pruefen_ungueltige_geraete_werden_ignoriert(v):
    einstellen(v)
    pruefen(v, HAUPT, ip=None, geraete=["kurz", "x" * 65, "mit leerzeichen 123456789", 5, None])
    assert bot.db._conn.execute("SELECT COUNT(*) FROM verif_signal").fetchone()[0] == 0
    assert status(G_A, HAUPT) == "verifiziert"
    assert pruefen(v, ALT, ip=None, geraete=["kurz"])["ergebnis"] == "verifiziert"   # ohne Signal keine Verbindung


def test_pruefen_hauptaccount_bleibt_verifiziert(v):
    einstellen(v)
    pruefen(v, HAUPT)
    pruefen(v, ALT)
    assert pruefen(v, HAUPT)["ergebnis"] == "verifiziert"
    einstellen(v, G_B)
    assert pruefen(v, HAUPT, gid=G_B)["ergebnis"] == "verifiziert"


def test_pruefen_nur_vpn_schaltet_alt_erkennung_ab(v):
    einstellen(v, nur_vpn=True)
    pruefen(v, HAUPT)
    assert pruefen(v, ALT)["ergebnis"] == "verifiziert"


def test_pruefen_manuell_ueberspringt_alt_und_vpn(v, monkeypatch):
    einstellen(v, vpn_pruefen=True)
    aufrufe = []

    async def ist_vpn(ip, key=""):
        aufrufe.append(ip)
        return True
    monkeypatch.setattr(bot, "_verif_vpn", ist_vpn)
    bot.db.verif_signal_merken(HAUPT, "ip", bot._verif_hash("ip", "81.2.69.5"))
    bot.db.verif_konto_merken(HAUPT, "H", jetzt=1.0)
    bot.db.verif_status_setzen(G_A, ALT, "manuell")
    assert pruefen(v, ALT)["ergebnis"] == "verifiziert"
    assert status(G_A, ALT) == "manuell" and aufrufe == [] and hat_rolle(v, G_A, ALT)


@pytest.mark.parametrize("aktion,erwartet,gekickt", [("blockieren", "vpn", []), ("kicken", "vpn", True),
                                                      ("keine", "verifiziert", [])])
def test_pruefen_vpn(v, monkeypatch, aktion, erwartet, gekickt):
    einstellen(v, vpn_pruefen=True, aktion_vpn=aktion, proxycheck_key="EIGEN1")
    gesehen = []

    async def ist_vpn(ip, key=""):
        gesehen.append((ip, key))
        return True
    monkeypatch.setattr(bot, "_verif_vpn", ist_vpn)
    assert pruefen(v, HAUPT)["ergebnis"] == erwartet
    assert gesehen == [("81.2.69.5", "EIGEN1")]
    assert status(G_A, HAUPT) == ("vpn" if erwartet == "vpn" else "verifiziert")
    assert v["g"][G_A].gekickt == ([HAUPT] if gekickt is True else [])
    assert "vpn" in [e["art"] for e in bot.db.verif_ereignisse([HAUPT])]
    assert any("VPN" in (m["embed"].title or "") for m in log_kanal(v))
    assert all(m["content"] is None for m in log_kanal(v))                    # VPN: kein Ping


def test_pruefen_vpn_unbekannt_blockiert_nicht(v, monkeypatch):
    einstellen(v, vpn_pruefen=True)

    async def unbekannt(ip, key=""):
        return None
    monkeypatch.setattr(bot, "_verif_vpn", unbekannt)
    assert pruefen(v, HAUPT)["ergebnis"] == "verifiziert"


def test_pruefen_reihenfolge_alter_vor_vpn_vor_alt(v, monkeypatch):
    einstellen(v, vpn_pruefen=True, min_alter_tage=7)
    aufrufe = []

    async def ist_vpn(ip, key=""):
        aufrufe.append(ip)
        return True
    monkeypatch.setattr(bot, "_verif_vpn", ist_vpn)
    assert pruefen(v, NEU)["ergebnis"] == "zu_jung" and aufrufe == []
    pruefen(v, HAUPT, ip="91.198.174.1")                                    # VPN
    monkeypatch.setattr(bot, "_verif_vpn", lambda ip, key="": asyncio.sleep(0, False))
    pruefen(v, DRITT)                                                     # normal, hinterlässt IP
    assert pruefen(v, ALT)["ergebnis"] == "alt"


def test_pruefen_alte_signale_ausserhalb_speicherdauer_verbinden_nicht(v):
    einstellen(v)
    bot.cfg.config["verifizierung_speicher_monate"] = 1
    alt = time.time() - 40 * TAG
    pruefen(v, HAUPT, jetzt=alt)
    assert pruefen(v, ALT)["ergebnis"] == "verifiziert"


def test_pruefen_gleichzeitig_dieselbe_person(v):
    einstellen(v)

    async def beide():
        g = v["g"][G_A]
        return await asyncio.gather(*(bot._verif_pruefen(g, G_A, HAUPT, "H", "81.2.69.5", [GERAET]) for _ in range(5)))
    erg = asyncio.run(beide())
    assert {e["ergebnis"] for e in erg} == {"verifiziert"} and status(G_A, HAUPT) == "verifiziert"


def test_pruefen_gleichzeitig_zwei_konten_gleiche_ip(v, monkeypatch):
    einstellen(v, vpn_pruefen=True)

    async def langsam(ip, key=""):
        await asyncio.sleep(0.05)
        return False
    monkeypatch.setattr(bot, "_verif_vpn", langsam)

    async def beide():
        g = v["g"][G_A]
        return await asyncio.gather(bot._verif_pruefen(g, G_A, HAUPT, "H", "81.2.69.5", []),
                                    bot._verif_pruefen(g, G_A, ALT, "A", "81.2.69.5", []))
    erg = asyncio.run(beide())
    assert sorted(e["ergebnis"] for e in erg) == ["alt", "verifiziert"]


# Behobener Fehler (vorher als xfail festgehalten).
def test_pruefen_frueher_beigetretener_zweitaccount_wird_erkannt(v):
    einstellen(v)
    asyncio.run(bot._verif_beitritt(v["g"][G_A].members[ALT]))               # ALT tritt zuerst bei, verifiziert nicht
    time.sleep(0.01)
    assert pruefen(v, HAUPT)["ergebnis"] == "verifiziert"                    # HAUPT verifiziert zuerst
    assert pruefen(v, ALT)["ergebnis"] == "alt"


# Behobener Fehler (vorher als xfail festgehalten).
def test_pruefen_loopback_ip_ist_kein_signal(v):
    einstellen(v)
    pruefen(v, HAUPT, ip="127.0.0.1")
    assert pruefen(v, ALT, ip="127.0.0.1")["ergebnis"] == "verifiziert"


# ══ Verifizier-Seite ═════════════════════════════════════════════════════════
def _token(v, uid=HAUPT, gid=G_A, minuten=10):
    return bot.db.verif_token_neu(gid, "1000" if gid == G_A else "2000", uid, minuten)


def seite(anfragen):
    """anfragen: Liste (methode, pfad, kwargs); laufen nacheinander in einem Client. Rückgabe [(status, text, cookies)]."""
    bot._VERIF_SEITE_LIMIT.clear()

    async def lauf():
        aus = []
        async with TestClient(TestServer(bot.build_app())) as c:
            for methode, pfad, kw in anfragen:
                bot._VERIF_SEITE_LIMIT.clear()
                r = await c.request(methode, pfad, **kw)
                aus.append((r.status, await r.text(), r.headers.getall("Set-Cookie", [])))
        return aus
    return asyncio.run(lauf())


def post(token, ip="81.2.69.5", geraet=GERAET, cookie=None, lang=None):
    h = {"X-Forwarded-For": ip}
    if lang:
        h["Accept-Language"] = lang
    if cookie:
        h["Cookie"] = f"bkf_geraet={cookie}"
    return ("POST", f"/api/verify/{token}", {"json": {"geraet": geraet}, "headers": h})


def test_seite_get_ungueltig_abgelaufen_benutzt(v):
    einstellen(v)
    benutzt = _token(v)
    bot.db.verif_token_verbrauchen(benutzt)
    abgelaufen = _token(v)
    bot.db._conn.execute("UPDATE verif_token SET ablauf=? WHERE token=?", (time.time() - 5, abgelaufen))
    bot.db._conn.commit()
    for t in ("x", "a" * 20, "<script>alert(1)</script>", benutzt, abgelaufen, "a" * 65):
        (st, text, _c), = seite([("GET", f"/verify/{t}", {})])
        assert st in (200, 404) and ("Link abgelaufen" in text or st == 404), t
        assert "<script>alert" not in text
    (st, text, _c), = seite([("GET", f"/verify/{benutzt}", {"headers": {"Accept-Language": "en-US"}})])
    assert "Link expired" in text


def test_seite_get_gueltig_escaped_und_header(v):
    einstellen(v)
    v["g"][G_A].name = "<b>Böse</b>"
    t = _token(v)

    async def lauf():
        async with TestClient(TestServer(bot.build_app())) as c:
            r = await c.get(f"/verify/{t}")
            return r.status, await r.text(), r.headers
    st, text, h = asyncio.run(lauf())
    assert st == 200 and "&lt;b&gt;Böse&lt;/b&gt;" in text and "<b>Böse</b>" not in text
    assert h["Cache-Control"] == "no-store" and h["X-Frame-Options"] == "DENY" and h["Referrer-Policy"] == "no-referrer"
    assert "noindex" in text and bot.db.verif_token_holen(t)["benutzt"] == 0          # GET verbraucht nichts


def test_seite_post_verifiziert_setzt_cookie_und_nur_einmal(v):
    einstellen(v)
    t = _token(v)
    (st, text, cookies), (st2, text2, _c) = seite([post(t), post(t, ip="81.2.69.77")])
    d = json.loads(text)
    assert st == 200 and d["ok"] and d["ergebnis"] == "verifiziert"
    c = "; ".join(cookies)
    assert f"bkf_geraet={GERAET}" in c and "HttpOnly" in c and "SameSite=Lax" in c
    assert st2 == 410 and status(G_A, HAUPT) == "verifiziert"
    assert [e["art"] for e in bot.db.verif_ereignisse([HAUPT])] == ["verifiziert"]


def test_seite_post_englisch_und_unbekannt(v):
    einstellen(v)
    (st, text, _c), = seite([post("a" * 30, lang="en")])
    assert st == 410 and "no longer valid" in text
    (st, text, _c), = seite([post(_token(v), lang="en")])
    assert st == 200 and "You are verified" in text


def test_seite_post_rate_limit_je_ip(v):
    einstellen(v)
    t1, t2, t3 = _token(v), _token(v, uid=ALT), _token(v, uid=DRITT)
    bot._VERIF_SEITE_LIMIT.clear()

    async def lauf():
        async with TestClient(TestServer(bot.build_app())) as c:
            out = []
            for t, ip in ((t1, "91.198.174.1"), (t2, "91.198.174.1"), (t3, "91.198.174.2")):
                m, p, kw = post(t, ip=ip)
                r = await c.request(m, p, **kw)
                out.append(r.status)
            return out
    assert asyncio.run(lauf()) == [200, 429, 200]
    assert bot.db.verif_token_holen(t2)["benutzt"] == 0                        # 429 kostet den Link nicht


def test_seite_post_guild_oder_mitglied_fehlt_verbraucht_link_nicht(v):
    einstellen(v)
    t = _token(v, uid=424242)
    (st, _t, _c), = seite([post(t)])
    assert st == 403 and bot.db.verif_token_holen(t)["benutzt"] == 0
    t2 = _token(v)
    weg = bot.bot._g.pop(G_A)
    (st, _t, _c), = seite([post(t2)])
    assert st == 409 and bot.db.verif_token_holen(t2)["benutzt"] == 0
    bot.bot._g[G_A] = weg
    (st, _t, _c), = seite([post(t2)])
    assert st == 200


def test_seite_post_gleichzeitig_gleicher_token_nur_einer(v, monkeypatch):
    einstellen(v)
    t = _token(v)
    echt = bot._verif_pruefen
    aufrufe = []

    async def gezaehlt(*a, **kw):
        aufrufe.append(1)
        return await echt(*a, **kw)
    monkeypatch.setattr(bot, "_verif_pruefen", gezaehlt)
    bot._VERIF_SEITE_LIMIT.clear()

    async def lauf():
        async with TestClient(TestServer(bot.build_app())) as c:
            async def eins(ip):
                m, p, kw = post(t, ip=ip)
                r = await c.request(m, p, **kw)
                return r.status
            return await asyncio.gather(*(eins(f"198.51.100.{i}") for i in range(1, 9)))
    st = sorted(asyncio.run(lauf()))
    assert st.count(200) == 1 and st.count(410) == 7 and len(aufrufe) == 1


def test_seite_geraete_cookie_verbindet_konten(v):
    einstellen(v)
    seite([post(_token(v), ip="91.198.174.1", geraet="")])
    keks = bot.db._conn.execute("SELECT COUNT(*) FROM verif_signal WHERE user_id=? AND art='geraet'", (HAUPT,)).fetchone()[0]
    assert keks == 0                                     # kein gültiges Gerät -> neuer Keks, aber kein Signal
    (st, text, cookies), = seite([post(_token(v, uid=DRITT), ip="91.198.174.2")])
    assert json.loads(text)["ergebnis"] == "verifiziert"
    # andere IP, anderes localStorage, aber derselbe Keks -> Zweitaccount
    (st, text, _c), = seite([post(_token(v, uid=ALT), ip="91.198.174.3", geraet="anderesGeraet123456", cookie=GERAET)])
    assert json.loads(text)["ergebnis"] == "alt" and bot.db.verif_status(G_A, ALT)["haupt_id"] == DRITT


def test_seite_token_gehoert_der_guild_aus_dem_token(v):
    einstellen(v, G_A)
    einstellen(v, G_B)
    t = _token(v, uid=HAUPT, gid=G_B)
    seite([post(t)])
    assert status(G_B, HAUPT) == "verifiziert" and status(G_A, HAUPT) is None


# ══ Dashboard ════════════════════════════════════════════════════════════════
PFAD = "/api/discord-management/verifizierung"


def daten(gid=G_A, **kw):
    i = ids(gid)
    d = {"enabled": True, "modus": "panel", "aktion_alt": "blockieren", "aktion_vpn": "blockieren", "min_alter_tage": 0,
         "kick_minuten": 0, "link_minuten": 10, "channel_id": str(i["verif"]), "log_channel_id": str(i["log"]),
         "verified_role_ids": [str(i["ver"])], "staff_role_ids": [], "unverified_role_id": str(i["unv"]),
         "ping_role_id": "", "staff_user_ids": [], "nachricht": ""}
    d.update(kw)
    return d


def mgmt(v, methode, pfad="", d=None, gid=G_A, conn="a", admin=True, **kw):
    return api(methode, PFAD + pfad, _admin(v, gid=gid, conn=conn, admin=admin, **kw), d)


def test_dashboard_ohne_login_und_spieler_gesperrt(v):
    for m, p in (("GET", ""), ("POST", ""), ("GET", "/suche?q=a"), ("GET", f"/konto/{HAUPT}"), ("POST", "/manuell")):
        assert api(m, PFAD + p, None, {})[0] in (401, 403)
        assert api(m, PFAD + p, _session(gid=G_A, sid_="1000"), {})[0] in (401, 403)


def test_dashboard_speichern_lesen_und_schluessel_geheim(v):
    st, d = mgmt(v, "POST", "", daten(proxycheck_key="GEHEIM-123"))
    assert st == 200, d
    assert "GEHEIM-123" not in json.dumps(d) and d["data"]["proxycheck_key_gesetzt"] is True
    st, d = mgmt(v, "GET")
    assert st == 200 and "proxycheck_key" not in d["data"] and "GEHEIM" not in json.dumps(d)
    assert d["data"]["enabled"] and d["data"]["seite"].startswith("https://verify.example/verify/")
    # Feld fehlt -> Schlüssel bleibt
    assert mgmt(v, "POST", "", daten())[0] == 200
    assert bot._verif_einstellungen(bot.guild_sicht(v["a"], G_A))["proxycheck_key"] == "GEHEIM-123"
    # "" löscht
    st, d = mgmt(v, "POST", "", daten(proxycheck_key=""))
    assert d["data"]["proxycheck_key_gesetzt"] is False
    for kaputt in ("a b c d e", "<script>", 5, "x" * 65, "kurz"):
        assert mgmt(v, "POST", "", daten(proxycheck_key=kaputt))[0] == 400, kaputt


@pytest.mark.parametrize("aenderung", [
    {"modus": "x"}, {"aktion_alt": "sprengen"}, {"aktion_vpn": "bannen"}, {"min_alter_tage": -1},
    {"min_alter_tage": 1.5}, {"min_alter_tage": True}, {"link_minuten": 1}, {"kick_minuten": 1441},
    {"min_alter_tage": "3"}, {"nachricht": "x" * 1001}, {"nachricht": 5},
    {"channel_id": "abc"}, {"channel_id": "999999"}, {"channel_id": str(ids(G_B)["verif"])},
    {"log_channel_id": str(ids(G_B)["log"])}, {"verified_role_ids": [str(ids(G_B)["ver"])]},
    {"verified_role_ids": ["x"]}, {"verified_role_ids": "1"}, {"verified_role_ids": [str(i) for i in range(1, 12)]},
    {"staff_role_ids": [str(ids(G_B)["staff"])]}, {"ping_role_id": str(ids(G_B)["ping"])},
    {"unverified_role_id": str(ids(G_A)["ver"])}, {"verified_role_ids": [str(ids(G_A)["hoch"])]},
    {"verified_role_ids": []}, {"channel_id": ""}, {"staff_user_ids": ["x"]}, {"staff_user_ids": "1"},
    {"staff_user_ids": [str(i) for i in range(1, 27)]},
])
def test_dashboard_ungueltig_und_fremde_ids(v, aenderung):
    st, d = mgmt(v, "POST", "", daten(**aenderung))
    assert st == 400, (aenderung, d)
    assert bot._verif_einstellungen(bot.guild_sicht(v["a"], G_A))["enabled"] is False


def test_dashboard_blind_ohne_rolle_und_aus_ohne_channel_erlaubt(v):
    assert mgmt(v, "POST", "", daten(modus="blind", verified_role_ids=[]))[0] == 200
    assert mgmt(v, "POST", "", daten(enabled=False, channel_id="", verified_role_ids=[]))[0] == 200


def test_dashboard_speicherdauer_nur_betreiber(v):
    assert mgmt(v, "POST", "", daten(speicher_monate=3), admin=False)[0] == 200
    assert bot._verif_speicher_monate() == 24
    assert mgmt(v, "POST", "", daten(speicher_monate=3))[0] == 200
    assert bot._verif_speicher_monate() == 3 and v["gespeichert"]
    for kaputt in (0, 61, "5", 2.5, True):
        assert mgmt(v, "POST", "", daten(speicher_monate=kaputt))[0] == 400, kaputt
    st, d = mgmt(v, "GET", admin=False)
    assert d["data"]["betreiber"] is False and d["data"]["speicher_monate"] == 3


def test_dashboard_kunde_b_sieht_und_aendert_nichts_von_a(v):
    einstellen(v, G_A)
    einstellen(v, G_B)
    pruefen(v, HAUPT, gid=G_A)
    pruefen(v, ALT, gid=G_A, ip="91.198.174.9")
    st, d = mgmt(v, "GET", gid=G_B, conn="b", admin=False)
    assert st == 200 and d["data"]["zaehler"] == {}
    st, d = mgmt(v, "GET", gid=G_A, conn="a", admin=False)
    assert d["data"]["zaehler"] == {"verifiziert": 2}
    assert mgmt(v, "GET", "/suche?q=", gid=G_B, conn="b", admin=False)[1]["data"]["treffer"] == []
    assert mgmt(v, "GET", f"/suche?q={HAUPT}", gid=G_B, conn="b", admin=False)[1]["data"]["treffer"] == []
    assert mgmt(v, "GET", f"/konto/{HAUPT}", gid=G_B, conn="b", admin=False)[0] == 404
    assert mgmt(v, "GET", f"/konto/{ALT}", gid=G_B, conn="b", admin=False)[0] == 404
    # Kunde B speichert eigene Einstellungen – A bleibt unverändert
    assert mgmt(v, "POST", "", daten(G_B, modus="blind"), gid=G_B, conn="b", admin=False)[0] == 200
    assert bot._verif_einstellungen(bot.guild_sicht(v["a"], G_A))["modus"] == "panel"
    # B kann nicht über die Session-Guild auf A umschalten
    assert mgmt(v, "POST", "", daten(G_A), gid=G_A, conn="b", admin=False)[0] in (400, 403, 409)


def test_dashboard_suche_und_konto(v):
    einstellen(v)
    pruefen(v, HAUPT)
    pruefen(v, ALT)
    st, d = mgmt(v, "GET", "/suche?q=Nutzer")
    tr = {t["user_id"]: t for t in d["data"]["treffer"]}
    assert set(tr) == {str(HAUPT), str(ALT)} and tr[str(ALT)]["haupt_id"] == str(HAUPT) and tr[str(ALT)]["konten"] == 2
    assert mgmt(v, "GET", "/suche?q=" + "%25")[1]["data"]["treffer"] == []
    st, d = mgmt(v, "GET", f"/konto/{ALT}")
    assert st == 200 and d["data"]["hauptaccount"] == str(HAUPT)
    assert [k["user_id"] for k in d["data"]["konten"]] == [str(HAUPT), str(ALT)]
    for kaputt in ("abc", "1" * 21, "-1", str(DRITT)):
        assert mgmt(v, "GET", f"/konto/{kaputt}")[0] == 404, kaputt


def test_dashboard_konto_ueber_guilds_ohne_fremde_guild_daten(v):
    v["g"][G_A].name = "GeheimeGuildA"
    einstellen(v, G_A)
    einstellen(v, G_B)
    pruefen(v, HAUPT, gid=G_A)
    pruefen(v, ALT, gid=G_B)
    bot.db.verif_ereignis_add(G_A, HAUPT, "ban", teilen=True)
    st, d = mgmt(v, "GET", f"/konto/{ALT}", gid=G_B, conn="b", admin=False)
    assert st == 200
    text = json.dumps(d)
    assert str(G_A) not in text and "GeheimeGuildA" not in text and "1000" not in text.replace(str(HAUPT), "")
    kh = next(k for k in d["data"]["konten"] if k["user_id"] == str(HAUPT))
    assert kh["hauptaccount"] and kh["status"] is None                    # Status nur in DIESER Guild
    assert d["data"]["lens"]["bans_andere_server"] == 1
    assert all(e["user_id"] == str(ALT) for e in d["data"]["verlauf"])     # nur Verlauf dieser Guild
    st, d = mgmt(v, "GET", f"/suche?q={HAUPT}", gid=G_B, conn="b", admin=False)
    assert d["data"]["treffer"] == []                                      # Haupt hat in B keinen Status


def test_dashboard_manuell(v):
    einstellen(v)
    pruefen(v, HAUPT)
    pruefen(v, ALT)
    st, d = mgmt(v, "POST", "/manuell", {"user_id": str(ALT)})
    assert st == 200 and status(G_A, ALT) == "manuell" and hat_rolle(v, G_A, ALT)
    assert bot.db.verif_ereignisse([ALT])[0]["teilen"] == 0
    assert "Manuell" in log_kanal(v)[-1]["embed"].title
    assert mgmt(v, "POST", "/manuell", {"user_id": "abc"})[0] == 400
    assert mgmt(v, "POST", "/manuell", {"user_id": "424242"})[0] == 400
    assert mgmt(v, "POST", "/manuell", {"user_id": str(DRITT)}, gid=G_B, conn="b", admin=False)[0] == 200
    assert status(G_B, DRITT) == "manuell" and status(G_A, DRITT) is None
    v["a"].data["dashboard_perms"] = {"user:900": {"discord_mgmt": ["view"]}}
    assert mgmt(v, "POST", "/manuell", {"user_id": str(NEU)}, gast="900")[0] == 403
    assert mgmt(v, "POST", "", daten(), gast="900")[0] == 403


# ══ Lens ═════════════════════════════════════════════════════════════════════
def test_lens_nur_fremde_geteilte_bans_kicks_ueber_die_gruppe(v):
    einstellen(v, G_A)
    einstellen(v, G_B)
    pruefen(v, HAUPT, gid=G_A)
    pruefen(v, ALT, gid=G_B)
    db = bot.db
    db.verif_ereignis_add(G_B, HAUPT, "ban", teilen=True)           # andere Guild, geteilt -> zählt (über die Gruppe)
    db.verif_ereignis_add(G_B, HAUPT, "ban", teilen=True)           # gleiche Guild doppelt -> 1 Server
    db.verif_ereignis_add(G_A, ALT, "ban", teilen=True)             # eigene Guild -> zählt nicht
    db.verif_ereignis_add(166666666666666666, ALT, "kick", teilen=False)   # nicht geteilt -> zählt nicht
    db.verif_ereignis_add(177777777777777777, ALT, "kick", teilen=True)
    lens = bot._verif_lens(ALT, G_A)
    assert lens["bans_andere_server"] == 1 and lens["kicks_andere_server"] == 1 and lens["konten"] == 2
    assert lens["zweitaccount_versuche"] == 1 and lens["server_verifiziert"] == 1
    assert 0 <= lens["punkte"] <= 100
    assert str(G_B) not in json.dumps(lens)
    db.verif_lens_setzen(ALT, True)
    assert set(bot._verif_lens(ALT, G_A)) == {"lens_aus", "konto_alter_tage"}
    assert bot._verif_gruppe_view(ALT, G_A)["lens"]["lens_aus"] is True


def test_lens_punkte_untergrenze(v):
    for i in range(10):
        bot.db.verif_ereignis_add(1000 + i, NEU, "ban", teilen=True)
    lens = bot._verif_lens(NEU, G_A)
    assert lens["bans_andere_server"] == 10 and lens["punkte"] == 0


# Behobener Fehler (vorher als xfail festgehalten).
def test_gruppe_view_verlauf_trotz_vieler_fremder_ereignisse(v):
    bot.db.verif_status_setzen(G_A, HAUPT, "verifiziert")
    bot.db.verif_ereignis_add(G_A, HAUPT, "verifiziert")
    for _ in range(120):
        bot.db.verif_ereignis_add(G_B, HAUPT, "kick", teilen=True)
    assert len(bot._verif_gruppe_view(HAUPT, G_A)["verlauf"]) == 1


def test_ban_kick_merken_nur_mit_verifizierung_und_teilen(v):
    asyncio.run(bot._verif_ban_kick_merken(v["g"][G_A], ALT, "ban"))
    assert bot.db.verif_ereignisse([ALT]) == []
    einstellen(v, lens_teilen=False)
    asyncio.run(bot._verif_ban_kick_merken(v["g"][G_A], ALT, "kick"))
    assert [(e["art"], e["teilen"]) for e in bot.db.verif_ereignisse([ALT])] == [("kick", 0)]
    einstellen(v)
    asyncio.run(bot._verif_ban_kick_merken(v["g"][G_A], ALT, "ban"))
    assert bot.db.verif_ereignisse([ALT])[0]["teilen"] == 1


# ══ Discord-Befehle ══════════════════════════════════════════════════════════
class _Resp:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, embed=None, view=None, ephemeral=False, file=None, **kw):
        self.sent.append({"content": content, "embed": embed, "view": view, "ephemeral": ephemeral, "file": file})


class VInter:
    def __init__(self, v, uid=HAUPT, gid=G_A, locale=None):
        self.guild = v["g"].get(gid) if gid else None
        self.user = self.guild.members[uid] if self.guild else VMitglied(uid, None)
        self.guild_id, self.locale, self.response = gid, locale, _Resp()
        self.channel = VKanal(4711)

    def text(self):
        return " ".join(str(m["content"] or "") + (json.dumps(m["embed"].to_dict(), ensure_ascii=False) if m["embed"] else "")
                        for m in self.response.sent)


def lauf(befehl, inter, **kw):
    asyncio.run(befehl.callback(inter, **kw))
    return inter.text()


@pytest.mark.parametrize("uid,erlaubt", [(HAUPT, False), (ADMIN, True), (OWNER, True), (STAFF, True), (DRITT, True)])
def test_manverify_rechte(v, uid, erlaubt):
    einstellen(v, staff_user_ids=[str(DRITT)])
    i = VInter(v, uid=uid)
    text = lauf(bot.cmd_manverify, i, user=v["g"][G_A].members[ALT])
    assert (status(G_A, ALT) == "manuell") is erlaubt, text
    assert hat_rolle(v, G_A, ALT) is erlaubt
    assert i.response.sent[0]["ephemeral"]
    if not erlaubt:
        assert "Nur Owner" in text


def test_befehle_ohne_aktive_verifizierung_und_ohne_guild(v):
    for befehl, kw in ((bot.cmd_manverify, {"user": v["g"][G_A].members[ALT]}), (bot.verif_status_cmd, {}),
                       (bot.verif_lookup, {"user": v["g"][G_A].members[ALT]}), (bot.cmd_verify, {})):
        assert "nicht aktiv" in lauf(befehl, VInter(v, uid=ADMIN), **kw)
    assert "Only on a Discord server" in lauf(bot.cmd_verify, VInter(v, gid=None))


def test_verify_link_ephemer_mit_token(v):
    einstellen(v, link_minuten=5)
    i = VInter(v)
    lauf(bot.cmd_verify, i)
    m = i.response.sent[0]
    assert m["ephemeral"] and m["view"] is not None
    url = m["view"].children[0].url
    assert url.startswith("https://verify.example/verify/")
    t = bot.db.verif_token_holen(url.rsplit("/", 1)[1])
    assert (t["user_id"], t["guild_id"], t["service_id"]) == (HAUPT, G_A, "1000")
    assert t["ablauf"] - t["erstellt"] == pytest.approx(300)
    assert "5 Minuten" in m["embed"].description


def test_verify_englisch(v):
    einstellen(v)
    i = VInter(v, locale=discord.Locale.american_english)
    lauf(bot.cmd_verify, i)
    assert i.response.sent[0]["view"].children[0].label == "Verify now"
    assert "Click the button" in i.response.sent[0]["embed"].description


def test_verify_schon_verifiziert_und_rolle_verloren(v):
    einstellen(v)
    pruefen(v, HAUPT)
    assert "bereits verifiziert" in lauf(bot.cmd_verify, VInter(v))
    g = v["g"][G_A]
    g.members[HAUPT].roles = []                                   # Rolle weg -> neuer Link
    i = VInter(v)
    lauf(bot.cmd_verify, i)
    assert i.response.sent[0]["view"] is not None


def test_verify_blind_gibt_rolle_direkt(v):
    einstellen(v, modus="blind")
    text = lauf(bot.cmd_verify, VInter(v))
    assert "verifiziert" in text and status(G_A, HAUPT) == "verifiziert" and hat_rolle(v, G_A, HAUPT)
    assert bot.db._conn.execute("SELECT COUNT(*) FROM verif_token").fetchone()[0] == 0


def test_verif_knopf_panel_view(v):
    einstellen(v)
    i = VInter(v)
    asyncio.run(bot._verif_knopf(i))
    assert i.response.sent[0]["view"] is not None
    assert bot.VerifPanelView().children[0].custom_id == bot._VERIF_PANEL_ID


def test_force_dm_und_dm_zu(v):
    einstellen(v)
    alt = v["g"][G_A].members[ALT]
    assert "Link an" in lauf(bot.verif_force, VInter(v, uid=ADMIN), user=alt)
    assert len(alt.dm) == 1 and alt.dm[0]["view"].children[0].url.startswith("https://verify.example/verify/")
    alt.dms = False
    assert "DMs geschlossen" in lauf(bot.verif_force, VInter(v, uid=ADMIN), user=alt)
    assert "Nur Owner" in lauf(bot.verif_force, VInter(v, uid=HAUPT), user=alt)


def test_panel_postet_button(v):
    einstellen(v, nachricht="Bitte klicken")
    k = v["g"][G_A].channels[ids(G_A)["verif"]]
    assert "gepostet" in lauf(bot.verif_panel, VInter(v, uid=ADMIN), channel=None)
    assert k.gesendet[0]["embed"].description == "Bitte klicken" and isinstance(k.gesendet[0]["view"], bot.VerifPanelView)
    k.kaputt = True
    assert "nicht schreiben" in lauf(bot.verif_panel, VInter(v, uid=ADMIN), channel=None)
    assert "Nur Owner" in lauf(bot.verif_panel, VInter(v, uid=HAUPT), channel=None)


def test_status_und_lookup(v):
    einstellen(v)
    pruefen(v, HAUPT)
    pruefen(v, ALT)
    text = lauf(bot.verif_status_cmd, VInter(v, uid=ADMIN))
    assert "Verifiziert: **1**" in text and "Zweitaccount: **1**" in text
    text = lauf(bot.verif_lookup, VInter(v, uid=ADMIN), user=v["g"][G_A].members[ALT])
    assert f"👑 Nutzer{HAUPT}" in text and str(ALT) in text and "Punkte" in text
    assert "Nur Owner" in lauf(bot.verif_lookup, VInter(v, uid=HAUPT), user=v["g"][G_A].members[ALT])
    bot.db.verif_lens_setzen(ALT, True)
    assert "Lens abgeschaltet" in lauf(bot.verif_lookup, VInter(v, uid=ADMIN), user=v["g"][G_A].members[ALT])


def test_privacy_export_loeschen_lens(v):
    einstellen(v)
    pruefen(v, HAUPT, geraete=[GERAET])
    i = VInter(v)
    lauf(bot.privacy_export, i)
    datei = i.response.sent[0]["file"]
    inhalt = datei.fp.read().decode("utf-8")
    d = json.loads(inhalt)
    hashes = [r["wert"] for r in bot.db._conn.execute("SELECT wert FROM verif_signal").fetchall()]
    assert d["konto"]["user_id"] == HAUPT and all(h not in inhalt for h in hashes) and "81.2.69.5" not in inhalt
    assert i.response.sent[0]["ephemeral"]
    lauf(bot.privacy_lens, VInter(v), on=False)
    assert bot.db.verif_konto(HAUPT)["lens_aus"] == 1
    lauf(bot.privacy_lens, VInter(v), on=True)
    assert bot.db.verif_konto(HAUPT)["lens_aus"] == 0
    lauf(bot.privacy_delete, VInter(v))
    assert bot.db.verif_export(HAUPT) == {"konto": None, "signale": [], "status": [], "ereignisse": []}
    assert pruefen(v, ALT)["ergebnis"] == "verifiziert"                  # Erkennung beginnt neu


# ══ Beitritt, Auto-Kick ══════════════════════════════════════════════════════
def test_beitritt_panel_unverifiziert_rolle_und_status(v):
    einstellen(v)
    m = v["g"][G_A].members[NEU]
    asyncio.run(bot._verif_beitritt(m))
    assert status(G_A, NEU) == "offen" and hat_rolle(v, G_A, NEU, "unv") and m.dm == []


def test_beitritt_aus_oder_bot_tut_nichts(v):
    m = v["g"][G_A].members[NEU]
    asyncio.run(bot._verif_beitritt(m))
    assert status(G_A, NEU) is None
    einstellen(v)
    m.bot = True
    asyncio.run(bot._verif_beitritt(m))
    assert status(G_A, NEU) is None and bot.db.verif_konto(NEU) is None


def test_beitritt_auto_dm_und_dm_zu(v):
    einstellen(v, modus="auto_dm")
    m = v["g"][G_A].members[NEU]
    asyncio.run(bot._verif_beitritt(m))
    assert len(m.dm) == 1 and "/verify/" in m.dm[0]["view"].children[0].url
    m2 = v["g"][G_A].members[DRITT]
    m2.dms = False
    asyncio.run(bot._verif_beitritt(m2))
    k = v["g"][G_A].channels[ids(G_A)["verif"]]
    assert len(k.gesendet) == 1 and m2.mention in k.gesendet[0]["content"] and k.gesendet[0]["delete_after"] == 20
    einstellen(v, modus="auto_dm", dm_hinweis=False)
    m3 = v["g"][G_A].members[VIERT]
    m3.dms = False
    asyncio.run(bot._verif_beitritt(m3))
    assert len(k.gesendet) == 1


def test_beitritt_blind_und_schon_verifiziert(v):
    einstellen(v, modus="blind")
    asyncio.run(bot._verif_beitritt(v["g"][G_A].members[NEU]))
    assert status(G_A, NEU) == "verifiziert" and hat_rolle(v, G_A, NEU)
    einstellen(v)
    bot.db.verif_status_setzen(G_A, HAUPT, "manuell")
    asyncio.run(bot._verif_beitritt(v["g"][G_A].members[HAUPT]))
    assert status(G_A, HAUPT) == "manuell"


def test_kick_lauf_nur_offene_aelter_als_frist(v):
    einstellen(v, kick_minuten=10)
    einstellen(v, G_B, kick_minuten=0)
    jetzt = time.time()
    bot.db.verif_status_setzen(G_A, NEU, "offen", jetzt=jetzt - 11 * 60)
    bot.db.verif_status_setzen(G_A, DRITT, "offen", jetzt=jetzt - 5 * 60)
    bot.db.verif_status_setzen(G_A, HAUPT, "verifiziert", jetzt=jetzt - 99 * 60)
    bot.db.verif_status_setzen(G_A, 424242, "offen", jetzt=jetzt - 99 * 60)     # schon weg
    bot.db.verif_status_setzen(G_B, NEU, "offen", jetzt=jetzt - 99 * 60)        # B: Auto-Kick aus
    asyncio.run(bot._verif_kick_lauf())
    assert v["g"][G_A].gekickt == [NEU] and v["g"][G_B].gekickt == []
    assert status(G_A, NEU) == "abgelaufen" and status(G_A, DRITT) == "offen" and status(G_A, HAUPT) == "verifiziert"
    assert status(G_A, 424242) == "abgelaufen" and status(G_B, NEU) == "offen"
    assert [(e["art"], e["teilen"]) for e in bot.db.verif_ereignisse([NEU])] == [("timeout", 0)]


def test_kick_lauf_blind_kickt_nicht(v):
    einstellen(v, kick_minuten=10, modus="blind")
    bot.db.verif_status_setzen(G_A, NEU, "offen", jetzt=time.time() - 3600)
    asyncio.run(bot._verif_kick_lauf())
    assert v["g"][G_A].gekickt == []


def test_aufraeumen_nutzt_speicherdauer(v):
    bot.cfg.config["verifizierung_speicher_monate"] = 1
    bot.db.verif_signal_merken(HAUPT, "ip", "x", jetzt=time.time() - 31 * TAG)
    bot.db.verif_signal_merken(ALT, "ip", "y", jetzt=time.time() - 29 * TAG)
    bot._verif_aufraeumen()
    assert [r["user_id"] for r in bot.db._conn.execute("SELECT user_id FROM verif_signal")] == [ALT]
