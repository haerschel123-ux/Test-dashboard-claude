"""Wortfilter (Dashboard -> Discord Management): Regeln mit Woertern, Geltung (alle/Rollen/Mitglieder) und Folge.

Geprueft wird die reine Erkennung, die Regelpruefung, die Entscheidung (strengste Folge, Eskalation), die Ausfuehrung gegen
Discord-Doubles (loeschen, verwarnen, Timeout, Kick, Bann, Fehler), die Ausnahmen, die Dashboard-API samt Rechten und die
Trennung der Mandanten. Es geht kein Test ins Internet und zu Discord.

    python3 -m pytest tests/test_wortfilter.py -q
"""
import asyncio
import json
import os
import sys
import time
from datetime import timedelta

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")
discord = bot.discord

G1, G2, G3 = 7201, 7202, 7203          # Guild 1 und 2 gehoeren zu Server 1000, Guild 3 zu Server 2000
K1, K2, K_FREMD = 1101, 1102, 1109
R1, R2, R_FREMD = 601, 602, 699
OWNER = 5


# ── Testdoubles ─────────────────────────────────────────────────────────────
class _Rolle:
    def __init__(self, id_):
        self.id = id_


class _Rechte:
    def __init__(self, **kw):
        self.administrator = kw.get("administrator", False)
        self.manage_guild = kw.get("manage_guild", False)
        self.manage_messages = kw.get("manage_messages", False)


class _Mitglied:
    def __init__(self, id_, rollen=(), name=None, **rechte):
        self.id, self.roles, self.guild_permissions = id_, [_Rolle(r) for r in rollen], _Rechte(**rechte)
        self.mention, self.display_name, self.bot = f"<@{id_}>", name or f"Spieler{id_}", False
        self.dms, self.timeouts, self.gekickt = [], [], 0
        self.dm_fehler = self.aktions_fehler = None

    async def send(self, content=None, **kw):
        if self.dm_fehler:
            raise self.dm_fehler
        self.dms.append(kw.get("embed") or content)

    async def timeout(self, dauer, reason=None):
        if self.aktions_fehler:
            raise self.aktions_fehler
        self.timeouts.append((dauer, reason))

    async def kick(self, reason=None):
        if self.aktions_fehler:
            raise self.aktions_fehler
        self.gekickt += 1

    def __str__(self):
        return self.display_name


class _Kanal:
    def __init__(self, id_, guild_id):
        self.id, self.guild, self.sent = id_, type("G", (), {"id": guild_id})(), []
        self.mention, self.name = f"<#{id_}>", f"kanal{id_}"

    async def send(self, content=None, **kw):
        self.sent.append({"content": content, **kw})


class _Nachricht:
    def __init__(self, autor, guild, kanal, inhalt, webhook_id=None):
        self.author, self.guild, self.channel, self.content, self.webhook_id = autor, guild, kanal, inhalt, webhook_id
        self.geloescht, self.delete_fehler = False, None

    async def delete(self):
        if self.delete_fehler:
            raise self.delete_fehler
        self.geloescht = True


def _nicht_gefunden():
    return discord.NotFound(type("R", (), {"status": 404, "reason": "x"})(), "unbekannt")


class _Guild:
    def __init__(self, id_, kanaele, rollen, owner=OWNER, name="Testserver"):
        self.id, self._k, self._r, self.owner_id, self.name = id_, {k.id: k for k in kanaele}, set(rollen), owner, name
        self.mitglieder, self.bans, self.ban_fehler = {}, [], None
        self.fetches = []

    def get_channel(self, cid):
        return self._k.get(int(cid))

    def get_role(self, rid):
        return object() if int(rid) in self._r else None

    def get_member(self, uid):
        return self.mitglieder.get(int(uid))

    async def fetch_member(self, uid):
        self.fetches.append(int(uid))
        if int(uid) in self.mitglieder:
            return self.mitglieder[int(uid)]
        raise _nicht_gefunden()

    async def ban(self, nutzer, reason=None, delete_message_seconds=0):
        if self.ban_fehler:
            raise self.ban_fehler
        self.bans.append((nutzer, reason, delete_message_seconds))


@pytest.fixture
def welt(monkeypatch, tmp_path):
    """Server 1000 mit Guild 1 (+ Guild 2) und Server 2000 mit Guild 3; Discord-Doubles."""
    monkeypatch.chdir(tmp_path)
    flat = {sid: {"service_id": sid, "nitrado_token": "x", "owner_discord_id": sid, "map_name": "Livonia"} for sid in ("1000", "2000")}
    (tmp_path / "connections.json").write_text(json.dumps(flat))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_DASH_RATE_LIMIT_LAST", {})
    monkeypatch.setattr(bot, "_audit_log", bot.deque(maxlen=100))
    monkeypatch.setattr(bot, "_WORTFILTER_STRIKES", {})
    a, b = reg._conns["1000"], reg._conns["2000"]
    reg.add_guild("1000", G1)
    reg.add_guild("1000", G2)
    reg.add_guild("2000", G3)
    a.data["kunden_stufe"] = b.data["kunden_stufe"] = "premium"
    k = {n: _Kanal(n, g) for n, g in ((K1, G1), (K2, G1), (K_FREMD, G3))}
    guilds = {G1: _Guild(G1, [k[K1], k[K2]], [R1, R2]), G2: _Guild(G2, [], []), G3: _Guild(G3, [k[K_FREMD]], [R_FREMD])}
    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guilds.get(int(gid)), raising=False)
    monkeypatch.setattr(bot.bot, "get_channel", lambda cid: k.get(int(cid)), raising=False)
    monkeypatch.setattr(bot.bot, "is_ready", lambda: True, raising=False)
    w = type("Welt", (), {})()
    w.a, w.b, w.k, w.guilds = a, b, k, guilds
    return w


def _regel(woerter=("poo",), **kw):
    r = {"words": list(woerter), "match": "word", "scope": "all", "action": "warn", "name": "Regel"}
    r.update(kw)
    return r


def _api(monkeypatch, conn, handler, daten=None, methode="POST", admin=True, guild=None, gast=None):
    sid = str(time.time_ns())
    discord_id = gast or (sid if admin else str(conn.data["owner_discord_id"]))
    sess = {"token": "x", "discord": {"id": discord_id}, "is_admin": admin and not gast, "service_id": conn.service_id,
            "seen": time.time(), "admin_geprueft_ts": time.time()}
    if gast:
        sess["is_guest"] = True
    if guild:
        sess["guild_id"] = str(guild)
    bot._SESS_STORE[sid] = sess
    req = make_mocked_request(methode, "/api/discord-management/wortfilter", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})

    async def body(_r):
        return daten
    monkeypatch.setattr(bot, "body", body)
    bot._DASH_RATE_LIMIT_LAST.clear()
    r = asyncio.run(handler(req))
    return r.status, json.loads(r.body)


def _speichern(monkeypatch, conn, regeln, guild=G1, **extra):
    daten = {"enabled": True, "rules": regeln, "exempt_roles": [], "log_channel_id": None}
    daten.update(extra)
    st, j = _api(monkeypatch, conn, bot.post_discord_mgmt_wortfilter, daten, guild=guild)
    assert st == 200, j
    return j["data"]


def _verarbeiten(welt, nachricht):
    return asyncio.run(bot._wortfilter_nachricht_verarbeiten(nachricht))


def _nachricht(welt, inhalt, autor=None, guild=G1, kanal=K1, **kw):
    autor = autor or _Mitglied(100)
    return _Nachricht(autor, welt.guilds[guild], welt.k[kanal], inhalt, **kw)


# ── Erkennung ───────────────────────────────────────────────────────────────
def _trifft(woerter, text, modus="word"):
    regel = {"words": list(woerter), "match": modus}
    return bot._wortfilter_finde(regel, bot._wortfilter_normal(text)) is not None


@pytest.mark.parametrize("text", ["poo", "Poo", "POO", "du poo!", "Das ist Poo.", "p00", "P0o", "pooooo", "Pöö", "Pôô", "p​oo",
                                  "ＰＯＯ", "ein poo, ein anderes poo", "poo\nzweite zeile"])
def test_wort_wird_trotz_schreibtricks_erkannt(text):
    assert _trifft(["poo"], text)


@pytest.mark.parametrize("text", ["spoon", "poop", "pool", "po", "p o o", "zoo", "", "nichts hier", "poofter", "apoo"])
def test_ganzes_wort_trifft_keine_teile_anderer_woerter(text):
    assert not _trifft(["poo"], text)


def test_teil_eines_wortes_trifft_auch_innerhalb_anderer_woerter():
    assert _trifft(["poo"], "spoon", "part") and _trifft(["poo"], "poop", "part") and _trifft(["poo"], "pool", "part")
    assert not _trifft(["poo"], "po op", "part") and not _trifft(["poo"], "pxo", "part")


def test_mehrere_woerter_und_wortgruppen():
    assert _trifft(["foo", "poo"], "ein POO") and _trifft(["foo", "poo"], "foo")
    assert _trifft(["du depp"], "du   depp") and _trifft(["du depp"], "Du DEPP") and not _trifft(["du depp"], "du, depp")
    assert not _trifft(["du depp"], "depp du")


def test_sonderzeichen_im_wort_sind_wirklich_woertlich():
    assert _trifft(["f*ck"], "oh f*ck") and not _trifft(["f*ck"], "fuck") and not _trifft(["a.b"], "axb")


def test_lange_eingaben_bleiben_schnell():
    """Das Muster hat keine verschachtelten Quantifizierer: auch boesartiger Text braucht keine Sekunden."""
    muster = {"words": ["a" * 40, "poo", "ab" * 20], "match": "word"}
    for text in ("a" * 4000, "ab" * 2000, ("a" * 39 + " ") * 100, "p" * 4000 + "x"):
        start = time.perf_counter()
        bot._wortfilter_finde(muster, bot._wortfilter_normal(text))
        assert time.perf_counter() - start < 0.5


# ── Regelpruefung ───────────────────────────────────────────────────────────
def test_gueltige_regel_bekommt_feste_form_und_kennung():
    r = bot._wortfilter_regel_pruefen({"words": ["  Poo ", "poo", "POO", "Foo"], "name": " Meine   Regel "}, id_erzeugen=True)
    assert r["words"] == ["Poo", "Foo"] and r["name"] == "Meine Regel" and r["match"] == "word" and r["scope"] == "all"
    assert len(r["id"]) == 8 and r["action"] == "warn" and r["timeout_minutes"] == 10 and r["escalation"] is None and r["enabled"] is True


def test_name_ohne_angabe_ist_das_erste_wort():
    assert bot._wortfilter_regel_pruefen({"words": ["Poo"]}, True)["name"] == "Poo"


def test_geltung_raeumt_die_nicht_gewaehlten_listen_auf():
    r = bot._wortfilter_regel_pruefen({"words": ["poo"], "scope": "roles", "role_ids": [str(R1)], "member_ids": ["9"]}, True)
    assert r["role_ids"] == [str(R1)] and r["member_ids"] == []
    r = bot._wortfilter_regel_pruefen({"words": ["poo"], "scope": "all", "role_ids": [str(R1)], "member_ids": ["9"]}, True)
    assert r["role_ids"] == [] and r["member_ids"] == []


@pytest.mark.parametrize("aenderung", [
    {"words": []}, {"words": "poo"}, {"words": [123]}, {"words": [""]}, {"words": ["   "]}, {"words": ["x" * 41]},
    {"words": ["a"]}, {"words": ["!!!"]}, {"words": ["po\x00o"]}, {"words": ["po​o​"], "match": "part"},
    {"words": [f"w{i}x" for i in range(41)]}, {"words": ["po"], "match": "part"}, {"match": "regex"}, {"match": None},
    {"scope": "jeder"}, {"scope": "roles"}, {"scope": "members"}, {"scope": "roles", "role_ids": []},
    {"role_ids": "5", "scope": "roles"}, {"scope": "roles", "role_ids": ["abc"]}, {"scope": "roles", "role_ids": [True]},
    {"scope": "roles", "role_ids": [str(i + 1) for i in range(16)]}, {"scope": "members", "member_ids": ["-5"]},
    {"scope": "members", "member_ids": ["0"]}, {"scope": "members", "member_ids": ["1" * 21]},
    {"action": "pranger"}, {"action": None}, {"timeout_minutes": 0}, {"timeout_minutes": 40321}, {"timeout_minutes": True},
    {"timeout_minutes": "10"}, {"timeout_minutes": 1.5}, {"name": 5}, {"name": "a\x07b"}, {"id": "xyz"}, {"id": 12},
    {"escalation": "ja"}, {"escalation": {"after": 3}}, {"escalation": {"after": 1, "action": "kick"}},
    {"escalation": {"after": 11, "action": "kick"}}, {"escalation": {"after": 3, "action": "warn"}},
    {"escalation": {"after": 3, "action": "delete"}}, {"escalation": {"after": 3, "action": "kick", "timeout_minutes": 0}},
    {"action": "ban", "escalation": {"after": 3, "action": "kick"}},
    {"action": "kick", "escalation": {"after": 3, "action": "timeout"}},
])
def test_ungueltige_regeln_werden_abgelehnt(aenderung):
    roh = {"words": ["poo"]}
    roh.update(aenderung)
    with pytest.raises(ValueError):
        bot._wortfilter_regel_pruefen(roh, id_erzeugen=True)


def test_regel_ohne_kennung_wird_beim_laden_abgelehnt_aber_beim_speichern_erzeugt():
    with pytest.raises(ValueError):
        bot._wortfilter_regel_pruefen({"words": ["poo"]})
    assert bot._wortfilter_regel_pruefen({"words": ["poo"]}, True)["id"]
    assert bot._wortfilter_regel_pruefen({"words": ["poo"], "id": "0a1b2c3d"})["id"] == "0a1b2c3d"


def test_eskalation_darf_gleich_streng_sein_und_hat_standardminuten():
    r = bot._wortfilter_regel_pruefen({"words": ["poo"], "action": "timeout", "timeout_minutes": 5,
                                       "escalation": {"after": 3, "action": "timeout"}}, True)
    assert r["escalation"] == {"after": 3, "action": "timeout", "timeout_minutes": 10} and r["timeout_minutes"] == 5


def test_kaputte_gespeicherte_regeln_werden_beim_laden_uebersprungen(welt):
    gut = bot._wortfilter_regel_pruefen({"words": ["poo"]}, True)
    welt.a.data["wortfilter"] = {"enabled": True, "rules": [gut, {"words": []}, "kaputt", None, {"id": "zz", "words": ["x"]}],
                                 "exempt_roles": "falsch"}
    e = bot._wortfilter_einstellungen(welt.a)
    assert [r["id"] for r in e["rules"]] == [gut["id"]] and e["exempt_roles"] == [] and e["enabled"] is True
    welt.a.data["wortfilter"] = "kaputt"
    assert bot._wortfilter_einstellungen(welt.a)["rules"] == []
    welt.a.data["wortfilter"] = {"rules": "kaputt"}
    assert bot._wortfilter_einstellungen(welt.a)["rules"] == []


# ── Entscheidung: Geltung, strengste Folge, Eskalation ──────────────────────
def _e(*regeln, ausnahmen=()):
    return {"enabled": True, "language": "de", "log_channel_id": None, "exempt_roles": [str(x) for x in ausnahmen],
            "rules": [bot._wortfilter_regel_pruefen(dict(r, id=f"{i:08x}"), True) for i, r in enumerate(regeln)]}


def test_ohne_treffer_keine_entscheidung(welt):
    assert bot._wortfilter_entscheiden(_e(_regel()), _Mitglied(1), G1, "alles gut") is None


def test_strengste_folge_gewinnt_bei_mehreren_treffern(welt):
    e = _e(_regel(["poo"], action="warn", name="A"), _regel(["poo"], action="kick", name="B"), _regel(["poo"], action="timeout", name="C"))
    d = bot._wortfilter_entscheiden(e, _Mitglied(1), G1, "poo")
    assert d["action"] == "kick" and d["regel"]["name"] == "B"


def test_ausgeschaltete_regel_greift_nicht(welt):
    assert bot._wortfilter_entscheiden(_e(_regel(enabled=False)), _Mitglied(1), G1, "poo") is None


def test_geltung_rollen_und_mitglieder(welt):
    e = _e(_regel(scope="roles", role_ids=[str(R1)]))
    assert bot._wortfilter_entscheiden(e, _Mitglied(1, rollen=[R1]), G1, "poo") is not None
    assert bot._wortfilter_entscheiden(e, _Mitglied(1, rollen=[R2]), G1, "poo") is None
    assert bot._wortfilter_entscheiden(e, _Mitglied(1), G1, "poo") is None
    e = _e(_regel(scope="members", member_ids=["42"]))
    assert bot._wortfilter_entscheiden(e, _Mitglied(42), G1, "poo") is not None
    assert bot._wortfilter_entscheiden(e, _Mitglied(43), G1, "poo") is None


def test_eskalation_nach_n_verstoessen_im_24_stunden_fenster(welt):
    e = _e(_regel(action="warn", escalation={"after": 3, "action": "timeout", "timeout_minutes": 60}))
    m = _Mitglied(1)
    t0 = 1_000_000.0
    folgen = [bot._wortfilter_entscheiden(e, m, G1, "poo", t0 + i)["action"] for i in range(4)]
    assert folgen == ["warn", "warn", "timeout", "timeout"]
    d = bot._wortfilter_entscheiden(e, m, G1, "poo", t0 + 10)
    assert d["minutes"] == 60 and d["eskaliert"] is True and d["anzahl"] == 5
    # nach 24 Stunden Ruhe zaehlt es von vorn
    d = bot._wortfilter_entscheiden(e, m, G1, "poo", t0 + 10 + 86400.5)
    assert d["action"] == "warn" and d["anzahl"] == 1 and d["eskaliert"] is False


def test_verstoesse_zaehlen_je_regel_nutzer_und_guild(welt):
    e = _e(_regel(["poo"], escalation={"after": 2, "action": "kick"}, name="A"))
    a, b = _Mitglied(1), _Mitglied(2)
    assert bot._wortfilter_entscheiden(e, a, G1, "poo", 1.0)["anzahl"] == 1
    assert bot._wortfilter_entscheiden(e, b, G1, "poo", 2.0)["anzahl"] == 1            # anderer Nutzer
    assert bot._wortfilter_entscheiden(e, a, G2, "poo", 3.0)["anzahl"] == 1            # andere Guild
    assert bot._wortfilter_entscheiden(e, a, G1, "poo", 4.0)["anzahl"] == 2


def test_zaehler_bleiben_begrenzt(welt, monkeypatch):
    monkeypatch.setattr(bot, "_WORTFILTER_STRIKES_MAX", 40)
    for i in range(200):
        bot._wortfilter_verstoss_zaehlen(G1, "aaaaaaaa", i, float(i))
    assert len(bot._WORTFILTER_STRIKES) <= 41
    assert (G1, "aaaaaaaa", 199) in bot._WORTFILTER_STRIKES                          # die neuesten bleiben


def test_nur_die_ersten_4000_zeichen_werden_geprueft(welt):
    assert bot._wortfilter_entscheiden(_e(_regel()), _Mitglied(1), G1, "x " * 2100 + "poo") is None


# ── Ausnahmen ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mitglied,frei", [
    (_Mitglied(OWNER), True), (_Mitglied(1, administrator=True), True), (_Mitglied(1, manage_guild=True), True),
    (_Mitglied(1, manage_messages=True), True), (_Mitglied(1, rollen=[R2]), True), (_Mitglied(1, rollen=[R1]), False),
    (_Mitglied(1), False)])
def test_ausnahmen(welt, mitglied, frei):
    assert bot._wortfilter_ausgenommen(mitglied, welt.guilds[G1], _e(_regel(), ausnahmen=[R2])) is frei


# ── Ausfuehrung ─────────────────────────────────────────────────────────────
def test_modul_aus_oder_ohne_regeln_tut_nichts(monkeypatch, welt):
    n = _nachricht(welt, "poo")
    assert _verarbeiten(welt, n) is False and not n.geloescht
    _speichern(monkeypatch, welt.a, [_regel()], enabled=False)
    assert _verarbeiten(welt, n) is False and not n.geloescht
    _speichern(monkeypatch, welt.a, [], enabled=True)
    assert _verarbeiten(welt, n) is False and not n.geloescht


def test_nur_loeschen_ist_still(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(action="delete")])
    n = _nachricht(welt, "poo")
    assert _verarbeiten(welt, n) is True and n.geloescht and welt.k[K1].sent == [] and n.author.dms == []


def test_verwarnen_loescht_und_sagt_es_kurz_im_channel_ohne_sammelerwaehnung(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(action="warn")])
    n = _nachricht(welt, "du Poo")
    assert _verarbeiten(welt, n) is True and n.geloescht
    gesendet = welt.k[K1].sent
    assert len(gesendet) == 1 and "<@100>" in gesendet[0]["content"] and gesendet[0]["delete_after"] == 15
    am = gesendet[0]["allowed_mentions"]
    assert am.everyone is False and am.roles is False and am.users == [n.author]
    assert "poo" not in gesendet[0]["content"].lower()                                    # das Wort steht nie in der Warnung


def test_verwarnung_auf_englisch_bei_englischem_server(monkeypatch, welt):
    welt.a.data["feed_sprache"] = "en"
    _speichern(monkeypatch, welt.a, [_regel()])
    _verarbeiten(welt, _nachricht(welt, "poo"))
    assert "watch your language" in welt.k[K1].sent[0]["content"]


def test_timeout_mit_dauer_und_dm(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(action="timeout", timeout_minutes=45, name="Poo-Regel")])
    n = _nachricht(welt, "poo")
    assert _verarbeiten(welt, n) is True and n.geloescht
    assert n.author.timeouts == [(timedelta(minutes=45), "Wortfilter: Regel „Poo-Regel“")]
    assert len(n.author.dms) == 1 and "45" in n.author.dms[0].description


def test_kick_schickt_die_dm_vor_dem_kick(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(action="kick")])
    n = _nachricht(welt, "poo")
    reihenfolge = []
    orig_send, orig_kick = n.author.send, n.author.kick

    async def send(*a, **kw):
        reihenfolge.append("dm")
        return await orig_send(*a, **kw)

    async def kick(*a, **kw):
        reihenfolge.append("kick")
        return await orig_kick(*a, **kw)
    n.author.send, n.author.kick = send, kick
    assert _verarbeiten(welt, n) is True
    assert reihenfolge == ["dm", "kick"] and n.author.gekickt == 1


def test_bann_ohne_nachrichten_loeschen_und_mit_grund(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(action="ban", name="Hart")])
    n = _nachricht(welt, "poo")
    assert _verarbeiten(welt, n) is True and n.geloescht
    assert welt.guilds[G1].bans == [(n.author, "Wortfilter: Regel „Hart“", 0)]


def test_geschlossene_dms_stoeren_die_strafe_nicht(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(action="timeout")])
    n = _nachricht(welt, "poo")
    n.author.dm_fehler = RuntimeError("DMs geschlossen")
    assert _verarbeiten(welt, n) is True and len(n.author.timeouts) == 1


@pytest.mark.parametrize("aktion", ["timeout", "kick", "ban"])
def test_fehlendes_recht_ist_ein_logeintrag_kein_absturz(monkeypatch, welt, aktion):
    _speichern(monkeypatch, welt.a, [_regel(action=aktion)], log_channel_id=str(K2))
    n = _nachricht(welt, "poo")
    n.author.aktions_fehler = RuntimeError("403 Forbidden")
    welt.guilds[G1].ban_fehler = RuntimeError("403 Forbidden")
    assert _verarbeiten(welt, n) is True and n.geloescht
    log = welt.k[K2].sent
    assert len(log) == 1 and "Problem" in [f.name for f in log[0]["embed"].fields]
    assert welt.guilds[G1].bans == [] and n.author.timeouts == [] and n.author.gekickt == 0
    assert not [z for z in bot._audit_log if "Wortfilter" in str(z)]                      # nichts passiert -> kein Audit-Eintrag


def test_nachricht_loeschen_schlaegt_fehl_strafe_gilt_trotzdem_und_log_sagt_es(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(action="timeout")], log_channel_id=str(K2))
    n = _nachricht(welt, "poo")
    n.delete_fehler = RuntimeError("403")
    assert _verarbeiten(welt, n) is True and not n.geloescht and len(n.author.timeouts) == 1
    felder = {f.name: f.value for f in welt.k[K2].sent[0]["embed"].fields}
    assert "Nachrichten verwalten" in felder["Problem"]


def test_log_zeigt_regel_nutzer_und_nachricht_ohne_ping_oder_markdown(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(name="Poo-Regel", action="delete")], log_channel_id=str(K2))
    n = _nachricht(welt, "poo @everyone <@&601> **fett**", autor=_Mitglied(100, name="Hans"))
    _verarbeiten(welt, n)
    eintrag = welt.k[K2].sent[0]
    assert eintrag["allowed_mentions"].everyone is False and eintrag["allowed_mentions"].users is False
    felder = {f.name: f.value for f in eintrag["embed"].fields}
    assert felder["Regel"] == "Poo-Regel" and "<@100>" in felder["Nutzer"] and "@everyone" not in felder["Nachricht"].replace("@​everyone", "")
    assert "\\*\\*fett" in felder["Nachricht"]
    assert "Nachricht gelöscht" in eintrag["embed"].title


def test_audit_nur_bei_wirksamer_strafe(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(action="kick")])
    _verarbeiten(welt, _nachricht(welt, "poo"))
    assert any("Wortfilter" in str(z) for z in bot._audit_log)


def test_bots_webhooks_und_leere_nachrichten_werden_ignoriert(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel()])
    robot = _Mitglied(100)
    robot.bot = True
    assert _verarbeiten(welt, _nachricht(welt, "poo", autor=robot)) is False
    assert _verarbeiten(welt, _nachricht(welt, "poo", webhook_id=123)) is False
    assert _verarbeiten(welt, _nachricht(welt, "   ")) is False
    assert _verarbeiten(welt, _nachricht(welt, None)) is False
    dm = _nachricht(welt, "poo")
    dm.guild = None
    assert _verarbeiten(welt, dm) is False
    nutzer = _nachricht(welt, "poo")
    nutzer.author = type("U", (), {"id": 1, "bot": False})()                                # kein Mitglied (keine Rollen)
    assert _verarbeiten(welt, nutzer) is False and welt.k[K1].sent == []


def test_ausgenommene_werden_nie_angefasst(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(action="ban")], exempt_roles=[str(R2)])
    for autor in (_Mitglied(OWNER), _Mitglied(7, administrator=True), _Mitglied(8, manage_messages=True), _Mitglied(9, rollen=[R2])):
        n = _nachricht(welt, "poo", autor=autor)
        assert _verarbeiten(welt, n) is False and not n.geloescht
    assert welt.guilds[G1].bans == []


def test_regeln_gelten_nur_in_ihrer_guild(monkeypatch, welt):
    """Guild 3 gehoert einem anderen Server und hat eigene (keine) Regeln - Guild 1 und Guild 2 desselben Servers sind getrennt."""
    _speichern(monkeypatch, welt.a, [_regel(action="delete")], guild=G1)
    assert _verarbeiten(welt, _nachricht(welt, "poo", guild=G1)) is True
    assert _verarbeiten(welt, _nachricht(welt, "poo", guild=G2, kanal=K1)) is False             # Guild 2 desselben Servers
    n = _nachricht(welt, "poo", guild=G3, kanal=K_FREMD)
    assert _verarbeiten(welt, n) is False and not n.geloescht                                    # Server 2000


def test_eskalation_wirkt_in_der_ausfuehrung(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(action="warn", escalation={"after": 2, "action": "kick"})])
    autor = _Mitglied(100)
    _verarbeiten(welt, _nachricht(welt, "poo", autor=autor))
    assert autor.gekickt == 0
    _verarbeiten(welt, _nachricht(welt, "poo", autor=autor))
    assert autor.gekickt == 1


def test_on_message_ueberspringt_xp_und_ki_bei_einem_verstoss(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(action="delete")])
    aufrufe = []

    async def level(_m):
        aufrufe.append("level")

    async def ki(_m):
        aufrufe.append("ki")

    async def honigtopf(_m):
        return False
    monkeypatch.setattr(bot, "_level_nachricht_verarbeiten", level)
    monkeypatch.setattr(bot, "_ticket_ki_nachricht", ki)
    monkeypatch.setattr(bot, "_honeypot_nachricht_verarbeiten", honigtopf)
    asyncio.run(bot.bot.on_message(_nachricht(welt, "poo")))
    assert aufrufe == []
    asyncio.run(bot.bot.on_message(_nachricht(welt, "harmlos")))
    assert aufrufe == ["level", "ki"]


def test_on_message_edit_prueft_nur_geaenderten_text(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(action="delete")])
    vorher, nachher = _nachricht(welt, "harmlos"), _nachricht(welt, "jetzt poo")
    asyncio.run(bot.bot.on_message_edit(vorher, nachher))
    assert nachher.geloescht
    gleich_a, gleich_b = _nachricht(welt, "poo"), _nachricht(welt, "poo")
    asyncio.run(bot.bot.on_message_edit(gleich_a, gleich_b))                                   # z. B. nur Vorschaubild nachgeladen
    assert not gleich_b.geloescht


def test_fehler_im_filter_bricht_on_message_nicht_ab(monkeypatch, welt):
    async def kaputt(_m):
        raise RuntimeError("boom")
    monkeypatch.setattr(bot, "_wortfilter_nachricht_verarbeiten", kaputt)
    aufrufe = []

    async def level(_m):
        aufrufe.append("level")

    async def honigtopf(_m):
        return False

    async def ki(_m):
        return None
    monkeypatch.setattr(bot, "_level_nachricht_verarbeiten", level)
    monkeypatch.setattr(bot, "_honeypot_nachricht_verarbeiten", honigtopf)
    monkeypatch.setattr(bot, "_ticket_ki_nachricht", ki)
    asyncio.run(bot.bot.on_message(_nachricht(welt, "poo")))
    assert aufrufe == ["level"]


# ── Dashboard-API ───────────────────────────────────────────────────────────
def test_speichern_und_lesen_mit_stabilen_kennungen(monkeypatch, welt):
    d = _speichern(monkeypatch, welt.a, [_regel(["Poo", "foo"], name="Erste"), _regel(["x1y"], name="Zweite", action="timeout", timeout_minutes=30)],
                   log_channel_id=str(K2), exempt_roles=[str(R2)])
    assert d["enabled"] is True and d["log_channel_id"] == str(K2) and d["exempt_roles"] == [str(R2)]
    assert [r["name"] for r in d["rules"]] == ["Erste", "Zweite"] and all(len(r["id"]) == 8 for r in d["rules"])
    assert d["inhalt_ok"] in (True, False) and d["limits"]["regeln"] == 40
    st, j = _api(monkeypatch, welt.a, bot.get_discord_mgmt_wortfilter, None, "GET", guild=G1)
    assert st == 200 and j["data"]["rules"] == d["rules"]
    # erneutes Speichern mit den vorhandenen Kennungen aendert sie nicht
    d2 = _speichern(monkeypatch, welt.a, d["rules"])
    assert [r["id"] for r in d2["rules"]] == [r["id"] for r in d["rules"]]


def test_speichern_ersetzt_alles_und_leere_liste_loescht_alle_regeln(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel()])
    d = _speichern(monkeypatch, welt.a, [], enabled=False)
    assert d["rules"] == [] and d["enabled"] is False


@pytest.mark.parametrize("daten", [
    None, [], "x", {"rules": "nein"}, {"rules": [{"words": []}]}, {"rules": [_regel()] * 41},
    {"rules": [dict(_regel(), id="aaaaaaaa"), dict(_regel(), id="aaaaaaaa")]},
    {"rules": [], "exempt_roles": "x"}, {"rules": [], "exempt_roles": [str(i + 1) for i in range(26)]},
    {"rules": [], "log_channel_id": "abc"}, {"rules": [], "log_channel_id": [1]},
    {"rules": [_regel(scope="members", member_ids=[str(i + 100) for i in range(15)])] * 5}])
def test_ungueltige_anfragen_werden_abgelehnt_und_nichts_gespeichert(monkeypatch, welt, daten):
    st, _ = _api(monkeypatch, welt.a, bot.post_discord_mgmt_wortfilter, daten, guild=G1)
    assert st == 400 and "wortfilter" not in welt.a.data


def test_fremde_oder_unbekannte_channel_und_rollen_werden_abgelehnt(monkeypatch, welt):
    for extra in ({"log_channel_id": str(K_FREMD)}, {"log_channel_id": "424242"}, {"exempt_roles": [str(R_FREMD)]}, {"exempt_roles": ["424242"]}):
        st, _ = _api(monkeypatch, welt.a, bot.post_discord_mgmt_wortfilter, {"enabled": True, "rules": [_regel()], **extra}, guild=G1)
        assert st == 400 and "wortfilter" not in welt.a.data
    st, _ = _api(monkeypatch, welt.a, bot.post_discord_mgmt_wortfilter,
                 {"enabled": True, "rules": [_regel(scope="roles", role_ids=[str(R_FREMD)])]}, guild=G1)
    assert st == 400 and "wortfilter" not in welt.a.data


def test_mitglieder_werden_geprueft_und_namen_angezeigt(monkeypatch, welt):
    welt.guilds[G1].mitglieder[42] = _Mitglied(42, name="Hans")
    d = _speichern(monkeypatch, welt.a, [_regel(scope="members", member_ids=["42"])])
    assert d["member_names"] == {"42": "Hans"} and welt.guilds[G1].fetches == []              # aus dem Cache, kein Discord-Abruf
    st, j = _api(monkeypatch, welt.a, bot.post_discord_mgmt_wortfilter,
                 {"enabled": True, "rules": [_regel(scope="members", member_ids=["777"])]}, guild=G1)
    assert st == 400 and "777" in j["error"] and welt.guilds[G1].fetches == [777]
    assert bot._wortfilter_einstellungen(bot.GuildSicht(welt.a, G1))["rules"][0]["member_ids"] == ["42"]   # alter Stand blieb


def test_mitglied_nur_per_abruf_bekannt_wird_akzeptiert(monkeypatch, welt):
    m = _Mitglied(55, name="Neu")
    welt.guilds[G1].fetch_member = lambda uid: _bereit(m)
    d = _speichern(monkeypatch, welt.a, [_regel(scope="members", member_ids=["55"])])
    assert d["rules"][0]["member_ids"] == ["55"] and d["member_names"] == {"55": None}


async def _bereit(wert):
    return wert


def test_discord_fehler_bei_der_mitgliederpruefung_ist_ein_502(monkeypatch, welt):
    async def kaputt(_uid):
        raise discord.HTTPException(type("R", (), {"status": 500, "reason": "x"})(), "boom")
    welt.guilds[G1].fetch_member = kaputt
    st, _ = _api(monkeypatch, welt.a, bot.post_discord_mgmt_wortfilter,
                 {"enabled": True, "rules": [_regel(scope="members", member_ids=["9"])]}, guild=G1)
    assert st == 502 and "wortfilter" not in welt.a.data


def test_modul_gespeichert_pro_guild_und_nie_nach_config_json(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel()], guild=G1)
    st, j = _api(monkeypatch, welt.a, bot.get_discord_mgmt_wortfilter, None, "GET", guild=G2)
    assert st == 200 and j["data"]["rules"] == [] and j["data"]["enabled"] is False            # Guild 2 sieht Guild 1 nicht
    assert "wortfilter" not in bot.cfg.config
    bot._conn_store(welt.a, "wortfilter", {"enabled": True, "rules": [], "exempt_roles": [], "log_channel_id": None})
    assert "wortfilter" not in bot.cfg.config and "wortfilter" not in json.dumps(bot.cfg.config)


def test_server_sehen_nie_die_regeln_eines_anderen(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(["geheim"])])
    st, j = _api(monkeypatch, welt.b, bot.get_discord_mgmt_wortfilter, None, "GET")
    assert st == 200 and j["data"]["rules"] == [] and "geheim" not in json.dumps(j)
    st, _ = _api(monkeypatch, welt.b, bot.post_discord_mgmt_wortfilter, {"enabled": True, "rules": [_regel()], "log_channel_id": str(K1)})
    assert st == 400                                                                            # Channel von Guild 1 ist fuer Server 2000 tabu
    st, _ = _api(monkeypatch, welt.b, bot.post_discord_mgmt_wortfilter, {"enabled": True, "rules": [_regel()], "log_channel_id": str(K_FREMD)})
    assert st == 200
    assert bot._wortfilter_einstellungen(bot.GuildSicht(welt.a, G1))["rules"][0]["words"] == ["geheim"]


def test_gast_nur_ansehen_darf_nichts_aendern(monkeypatch, welt):
    welt.a.data["dashboard_perms"] = {"user:900": {"discord_mgmt": ["view"]}}
    assert _api(monkeypatch, welt.a, bot.get_discord_mgmt_wortfilter, None, "GET", gast="900")[0] == 200
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_wortfilter, {"enabled": True, "rules": [_regel()]}, gast="900")[0] == 403
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_wortfilter_test, {"text": "poo"}, gast="900")[0] == 200
    assert "wortfilter" not in welt.a.data


def test_gast_ohne_eintrag_sieht_nichts(monkeypatch, welt):
    assert _api(monkeypatch, welt.a, bot.get_discord_mgmt_wortfilter, None, "GET", gast="900")[0] == 403
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_wortfilter_test, {"text": "poo"}, gast="900")[0] == 403


def test_kunde_besitzer_darf_alles_auf_seinem_server(monkeypatch, welt):
    st, j = _api(monkeypatch, welt.a, bot.post_discord_mgmt_wortfilter, {"enabled": True, "rules": [_regel()]}, admin=False)
    assert st == 200, j


def test_speichern_hat_eine_rate_begrenzung(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel()])
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "x", "discord": {"id": sid}, "is_admin": True, "service_id": "1000", "seen": time.time(),
                            "admin_geprueft_ts": time.time(), "guild_id": str(G1)}
    req = make_mocked_request("POST", "/x", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})

    async def body(_r):
        return {"enabled": True, "rules": [_regel()]}
    monkeypatch.setattr(bot, "body", body)
    bot._DASH_RATE_LIMIT_LAST.clear()
    assert asyncio.run(bot.post_discord_mgmt_wortfilter(req)).status == 200
    assert asyncio.run(bot.post_discord_mgmt_wortfilter(req)).status == 429


def test_kachel_status_in_der_discord_management_uebersicht(monkeypatch, welt):
    sicht = bot.GuildSicht(welt.a, G1)
    assert bot._discord_mgmt_payload(sicht)["wortfilter"] == {"enabled": False}
    _speichern(monkeypatch, welt.a, [_regel()])
    assert bot._discord_mgmt_payload(sicht)["wortfilter"] == {"enabled": True}
    assert bot._discord_mgmt_payload(bot.GuildSicht(welt.a, G2))["wortfilter"] == {"enabled": False}


def test_testfunktion_zeigt_treffer_ohne_etwas_zu_bestrafen(monkeypatch, welt):
    _speichern(monkeypatch, welt.a, [_regel(["poo"], name="A"), _regel(["foo"], name="B", enabled=False), _regel(["bar"], scope="roles", role_ids=[str(R1)])])
    st, j = _api(monkeypatch, welt.a, bot.post_discord_mgmt_wortfilter_test, {"text": "Das P00 und foo"}, guild=G1)
    assert st == 200
    treffer = {t["name"]: t for t in j["data"]["treffer"]}
    assert set(treffer) == {"A", "B"} and treffer["A"]["treffer"] == "poo" and treffer["B"]["enabled"] is False
    assert bot._WORTFILTER_STRIKES == {}
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_wortfilter_test, {"text": "harmlos"}, guild=G1)[1]["data"]["treffer"] == []


@pytest.mark.parametrize("daten", [None, {}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": "x" * 501}])
def test_testfunktion_lehnt_ungueltigen_text_ab(monkeypatch, welt, daten):
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_wortfilter_test, daten, guild=G1)[0] == 400


def test_get_ohne_kaputte_daten_stuerzt_nicht_ab(monkeypatch, welt):
    welt.a.data["wortfilter"] = {"enabled": True, "rules": [{"id": "aaaaaaaa", "words": ["x"], "scope": "members", "member_ids": []}]}
    st, j = _api(monkeypatch, welt.a, bot.get_discord_mgmt_wortfilter, None, "GET", guild=G1)
    assert st == 200 and j["data"]["rules"] == []
