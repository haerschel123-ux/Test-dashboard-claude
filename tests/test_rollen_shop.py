"""Rollen-Shop: Discord-Rollen gegen Waehrung (dauerhaft oder fuer N Tage), /shop buy role und Dashboard.

Schwerpunkte: nichts bezahlen, was nicht vergeben werden kann; jeder Fehlschlag gibt Geld, Bestand und Gutschein
zurueck; zeitlich begrenzte Rollen laufen neustartfest ab; alles je Guild; Spieler kommen nicht an die Betreiber-Seite.

    python3 -m pytest tests/test_rollen_shop.py -q
"""
import asyncio
import os
import sys
import time

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
from test_airstrike import servers  # noqa: E402,F401 - Fixture (zwei Server 1000/2000)
from test_spieler_dashboard import api, _session  # noqa: E402

G_A, G_B = 144749039048694173, 155555555555555555
U, ANDERER = 555, 777
VIP, BOOSTER, MOD, MANAGED, GONE = 100, 200, 300, 400, 999
TAG = 86400


def _run(coro):
    return asyncio.run(coro)


class _Resp:
    status, reason = 403, "forbidden"


class _R:
    def __init__(self, id_, name, position=1, managed=False):
        self.id, self.name, self.position, self.managed = id_, name, position, managed
        self.mention = f"<@&{id_}>"

    def is_default(self):
        return False


class _M:
    def __init__(self, id_, guild, rollen=()):
        self.id, self.display_name, self.roles, self.guild = id_, f"User{id_}", [guild.rollen[r] for r in rollen], guild
        self.add_log, self.remove_log, self.dms = [], [], []

    async def add_roles(self, role, reason=None):
        await asyncio.sleep(0)                  # wie bei Discord: hier wartet der Bot auf das Netz, andere Aufgaben laufen weiter
        if self.guild.verweigern:
            raise discord.Forbidden(_Resp(), "Missing Permissions")
        self.roles.append(role)
        self.add_log.append((role.id, reason))

    async def send(self, text):
        if self.guild.dm_zu:
            raise discord.Forbidden(_Resp(), "Cannot send messages to this user")
        self.dms.append(text)

    async def remove_roles(self, role, reason=None):
        if self.guild.verweigern_entfernen:
            raise discord.Forbidden(_Resp(), "Missing Permissions")
        self.roles = [r for r in self.roles if r.id != role.id]
        self.remove_log.append((role.id, reason))


class _G:
    def __init__(self, id_):
        self.id, self.name = id_, f"Guild {id_}"
        self.rollen = {VIP: _R(VIP, "VIP", 1), BOOSTER: _R(BOOSTER, "Booster", 2), MOD: _R(MOD, "Moderator", 10),
                       MANAGED: _R(MANAGED, "Bot-Rolle", 1, managed=True), 500: _R(500, "Extra", 3)}
        self.me = type("Me", (), {"guild_permissions": type("P", (), {"manage_roles": True})(),
                                  "top_role": _R(1, "Bot", 5)})()
        self.mitglieder, self.verweigern, self.verweigern_entfernen = {}, False, False
        self.dm_zu = False

    @property
    def roles(self):
        return list(self.rollen.values())

    def get_role(self, rid):
        return self.rollen.get(int(rid))

    def get_member(self, uid):
        return self.mitglieder.get(int(uid))

    async def fetch_member(self, uid):
        raise discord.NotFound(_Resp(), "Unknown Member")


class _Bot:
    def __init__(self, guilds):
        self.guilds = guilds

    def get_guild(self, gid):
        return self.guilds.get(int(gid))


@pytest.fixture
def rwelt(monkeypatch, servers, tmp_path):  # noqa: F811
    a, b = servers
    bot.connections.assign_guild(a.service_id, G_A)
    bot.connections.assign_guild(b.service_id, G_B)
    ga, gb = _G(G_A), _G(G_B)
    ga.mitglieder = {U: _M(U, ga), ANDERER: _M(ANDERER, ga, [BOOSTER])}
    gb.mitglieder = {U: _M(U, gb)}
    monkeypatch.setattr(bot, "bot", _Bot({G_A: ga, G_B: gb}))
    monkeypatch.setattr(bot, "db", bot.EconomyDB(str(tmp_path / "e.db")))
    monkeypatch.setattr(bot.cfg, "guilds", {})
    feeds = []

    async def feed(gid, key, embed, **kw):
        feeds.append((gid, key, kw.get("service_id"), embed))
    monkeypatch.setattr(bot, "_post_feed", feed)

    async def echtes_body(request):
        try:
            d = await request.json()
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    monkeypatch.setattr(bot, "body", echtes_body)
    bot._DASH_RATE_LIMIT_LAST.clear()
    bot._GUTSCHEIN_FEHLVERSUCHE.clear()
    bot._ROLLEN_KAUF_LOCKS.clear()
    bot.db.add_wallet(G_A, U, 100000)
    bot.db.add_wallet(G_A, ANDERER, 100000)
    return {"a": a, "ga": ga, "gb": gb, "feeds": feeds, "u": ga.mitglieder[U], "anderer": ga.mitglieder[ANDERER],
            "start": bot.db.get_balance(G_A, U)[0]}             # Startguthaben haengt von der Konfiguration ab


def _artikel(gid=G_A, role=VIP, name="VIP-Rolle", preis=1000, **kw):
    f = dict(role_id=role, name=name, beschreibung="", preis=preis, hidden=False, permanent=True, dauer_tage=None,
             bestand=None, benoetigt=[], alle_noetig=False, verfuegbar_ab=None, verfuegbar_bis=None, enabled=True)
    f.update(kw)
    erg, aid = bot.db.rollen_artikel_speichern(gid, None, f)
    assert erg == "ok"
    return aid


def _kaufen(w, uid=U, name="VIP-Rolle", gutschein=None, ids=None, gid=G_A, quelle="discord", en=False):
    guild = w["ga"] if gid == G_A else w["gb"]
    m = guild.mitglieder[uid]
    ids = {r.id for r in m.roles} if ids is None else ids
    plan = bot._rolle_kauf_pruefen(guild, gid, uid, ids, name, gutschein, en)
    if isinstance(plan, bot._KaufFehler):
        return plan
    return _run(bot._rolle_kauf_ausfuehren(w["a"], guild, gid, uid, "U", plan, quelle))


def _admin(gid=G_A, sid_="1000", uid="1000"):
    return bot._sess_create("fake", [], discord_user={"id": uid, "name": "Owner"}, service_id=sid_,
                            owned_guilds=[{"id": str(gid), "name": "G"}], guild_id=str(gid))


def _feld(**kw):
    f = {"role_id": str(VIP), "name": "Neu", "beschreibung": "x", "preis": 500, "permanent": True}
    f.update(kw)
    return f


# ── DB ───────────────────────────────────────────────────────────────────
def test_artikel_anlegen_aendern_loeschen(rwelt):
    aid = _artikel(bestand=3)
    f = dict(role_id=VIP, name="vip-rolle", beschreibung="", preis=1, hidden=False, permanent=True, dauer_tage=None,
             bestand=None, benoetigt=[], alle_noetig=False, verfuegbar_ab=None, verfuegbar_bis=None, enabled=True)
    assert bot.db.rollen_artikel_speichern(G_A, None, f) == ("name_vorhanden", None)             # Gross/klein egal
    bot.db.rollen_bestand_reservieren(aid)
    assert bot.db.rollen_artikel_speichern(G_A, aid, dict(f, name="Umbenannt", preis=7)) == ("ok", aid)
    row = bot.db.rollen_artikel_holen(G_A, aid)
    assert (row["name"], row["preis"], row["verkauft"]) == ("Umbenannt", 7, 1)                    # Verkaufszaehler bleibt
    assert bot.db.rollen_artikel_speichern(G_A, 9999, f) == ("nicht_gefunden", None)
    assert bot.db.rollen_artikel_speichern(G_B, None, f)[0] == "ok"                              # andere Guild: gleicher Name erlaubt
    assert bot.db.rollen_artikel_loeschen(G_B, aid) is False and bot.db.rollen_artikel_loeschen(G_A, aid) is True


def test_artikel_limit(rwelt):
    for i in range(3):
        _artikel(name=f"A{i}")
    f = dict(role_id=VIP, name="zuviel", beschreibung="", preis=1, hidden=False, permanent=True, dauer_tage=None,
             bestand=None, benoetigt=[], alle_noetig=False, verfuegbar_ab=None, verfuegbar_bis=None, enabled=True)
    assert bot.db.rollen_artikel_speichern(G_A, None, f, max_artikel=3) == ("limit", None)


def test_bestand_atomar(rwelt):
    aid = _artikel(bestand=2)
    assert [bot.db.rollen_bestand_reservieren(aid) for _ in range(3)] == [True, True, False]
    bot.db.rollen_bestand_freigeben(aid)
    assert bot.db.rollen_bestand_reservieren(aid) is True
    frei = _artikel(name="unbegrenzt")
    assert all(bot.db.rollen_bestand_reservieren(frei) for _ in range(5))


# ── Kauf ─────────────────────────────────────────────────────────────────
def test_dauerhafte_rolle_kaufen(rwelt):
    _artikel(preis=1000)
    vor = bot.db.get_balance(G_A, U)[0]
    r = _kaufen(rwelt)
    assert r.ok and r.ende is None and not r.verlaengert
    assert bot.db.get_balance(G_A, U)[0] == vor - 1000
    assert rwelt["u"].add_log == [(VIP, "Rollen-Shop (discord)")] and any(x.id == VIP for x in rwelt["u"].roles)
    k = bot.db.rollen_kauf_aktiv(G_A, U, VIP)
    assert (k["preis_bezahlt"], k["expires_at"], k["status"], k["quelle"]) == (1000, None, "active", "discord")
    assert rwelt["feeds"][-1][:3] == (G_A, "shop_log", "1000")
    assert bot.db.rollen_artikel_nach_name(G_A, "VIP-Rolle")["verkauft"] == 1


def test_dauerhaft_nicht_doppelt_kaufbar(rwelt):
    _artikel()
    assert _kaufen(rwelt).ok
    vor = bot.db.get_balance(G_A, U)[0]
    f = _kaufen(rwelt)
    assert isinstance(f, bot._KaufFehler) and f.code == "besitzt"
    assert bot.db.get_balance(G_A, U)[0] == vor


def test_zeitrolle_laeuft_ab_und_wird_verlaengert(rwelt):
    _artikel(name="Woche", role=BOOSTER, permanent=False, dauer_tage=7, preis=100)
    jetzt = time.time()
    r1 = _kaufen(rwelt, name="Woche")
    assert r1.ok and abs(r1.ende - (jetzt + 7 * TAG)) < 5
    r2 = _kaufen(rwelt, name="Woche")                                      # Verlaengerung: ab dem bisherigen Ende
    assert r2.ok and r2.verlaengert and abs(r2.ende - (r1.ende + 7 * TAG)) < 5
    aktiv = [k for k in bot.db.rollen_kaeufe_von(G_A, U, 10) if k["status"] == "active"]
    assert len(aktiv) == 1 and abs(aktiv[0]["expires_at"] - r2.ende) < 1                                # nur EIN aktiver Kauf
    assert [k["status"] for k in bot.db.rollen_kaeufe_von(G_A, U, 10)].count("extended") == 1
    assert len([x for x in rwelt["u"].roles if x.id == BOOSTER]) == 1                                     # Rolle nicht doppelt


def test_zeitrolle_nicht_ueber_den_shop_erhalten_ist_nicht_kaufbar(rwelt):
    _artikel(name="Woche", role=BOOSTER, permanent=False, dauer_tage=7)
    f = _kaufen(rwelt, uid=ANDERER, name="Woche")                         # ANDERER hat Booster, aber nicht aus dem Shop
    assert isinstance(f, bot._KaufFehler) and f.code == "besitzt"


def test_aufwertung_zeitlich_zu_dauerhaft(rwelt):
    _artikel(name="Woche", role=BOOSTER, permanent=False, dauer_tage=7, preis=10)
    _artikel(name="Fuer-immer", role=BOOSTER, permanent=True, preis=50)
    assert _kaufen(rwelt, name="Woche").ok
    r = _kaufen(rwelt, name="Fuer-immer")
    assert r.ok and r.ende is None
    aktiv = [k for k in bot.db.rollen_kaeufe_von(G_A, U, 10) if k["status"] == "active"]
    assert len(aktiv) == 1 and aktiv[0]["expires_at"] is None
    f = _kaufen(rwelt, name="Woche")                                       # dauerhaft vorhanden: keine Zeitrolle mehr
    assert isinstance(f, bot._KaufFehler) and f.code == "besitzt"


@pytest.mark.parametrize("kw,code", [
    (dict(enabled=False), "nicht_verfuegbar"), (dict(verfuegbar_ab=time.time() + 3600), "zeitraum"),
    (dict(verfuegbar_bis=time.time() - 3600), "zeitraum")])
def test_nicht_verfuegbar(rwelt, kw, code):
    _artikel(**kw)
    f = _kaufen(rwelt)
    assert isinstance(f, bot._KaufFehler) and f.code == code
    assert bot.db.get_balance(G_A, U)[0] == rwelt["start"]


def test_benoetigte_rollen_eine_oder_alle(rwelt):
    _artikel(name="Eine", role=500, benoetigt=[BOOSTER, VIP])                 # eine von beiden genuegt
    _artikel(name="Alle", role=VIP, benoetigt=[BOOSTER, 500], alle_noetig=True)   # beide noetig
    assert _kaufen(rwelt, name="Eine").code == "rolle_noetig"                 # U hat keine
    assert "Booster" in _kaufen(rwelt, name="Eine").text(False)
    assert _kaufen(rwelt, uid=ANDERER, name="Alle").code == "rolle_noetig"    # ANDERER hat nur Booster, braucht auch Extra
    assert _kaufen(rwelt, uid=ANDERER, name="Eine").ok                        # Booster genuegt -> bekommt Extra
    assert _kaufen(rwelt, uid=ANDERER, name="Alle").ok                        # jetzt hat er beide


def test_ausverkauft_und_bestand_sinkt(rwelt):
    _artikel(name="Limit", role=500, bestand=1)
    assert _kaufen(rwelt, name="Limit").ok
    f = _kaufen(rwelt, uid=ANDERER, name="Limit")
    assert isinstance(f, bot._KaufFehler) and f.code == "ausverkauft"


def test_versteckt_nur_per_exaktem_namen_kaufbar(rwelt):
    _artikel(name="Geheim", hidden=True)
    assert [a["name"] for a in map(bot._rollen_artikel_dict, bot.db.rollen_artikel_liste(G_A)) if not a["hidden"]] == []
    assert _kaufen(rwelt, name="geheim").ok


@pytest.mark.parametrize("aenderung,text", [
    (lambda g: g.rollen[VIP].__setattr__("position", 9), "höchsten Rolle"),               # ueber der Bot-Rolle
    (lambda g: g.rollen[VIP].__setattr__("managed", True), "Integration"),
    (lambda g: g.me.guild_permissions.__setattr__("manage_roles", False), "Rollen verwalten"),
    (lambda g: g.rollen.pop(VIP), "nicht (mehr)")])
def test_nicht_vergebbare_rolle_wird_gar_nicht_erst_bezahlt(rwelt, aenderung, text):
    _artikel()
    aenderung(rwelt["ga"])
    f = _kaufen(rwelt)
    assert isinstance(f, bot._KaufFehler) and f.code in ("nicht_vergebbar", "item") and (text in f.text(False) or f.code == "item")
    assert bot.db.get_balance(G_A, U)[0] == rwelt["start"] and bot.db.rollen_artikel_nach_name(G_A, "VIP-Rolle")["verkauft"] == 0


def test_discord_verweigert_die_rolle_alles_wird_zurueckgegeben(rwelt):
    _artikel(bestand=2)
    bot.db.gutschein_speichern(G_A, "MINUS100", "fixed", 100, True, 1, None, None, True)
    rwelt["ga"].verweigern = True
    vor = bot.db.get_balance(G_A, U)[0]
    r = _kaufen(rwelt, gutschein="MINUS100")
    assert not r.ok and r.code == "rolle" and "nicht abgebucht" in r.de
    assert bot.db.get_balance(G_A, U)[0] == vor
    assert bot.db.rollen_artikel_nach_name(G_A, "VIP-Rolle")["verkauft"] == 0
    assert bot.db.gutschein_holen(G_A, "MINUS100")["uses"] == 0
    assert bot.db.rollen_kaeufe_von(G_A, U, 5) == []
    rwelt["ga"].verweigern = False
    assert _kaufen(rwelt, gutschein="MINUS100").ok                          # danach klappt derselbe Gutschein


def test_rabatt_und_gutschein_senken_den_preis(rwelt):
    _artikel(preis=1000, role=VIP)
    bot.db.rabatt_ersetzen(G_A, [(BOOSTER, "percent", 10)])
    bot.db.gutschein_speichern(G_A, "FEST50", "fixed", 50, True, None, None, None, True)
    vor = bot.db.get_balance(G_A, ANDERER)[0]
    r = _kaufen(rwelt, uid=ANDERER, gutschein="fest50")                     # 1000 - 100 (10 %) - 50
    assert r.ok and bot.db.get_balance(G_A, ANDERER)[0] == vor - 850
    k = bot.db.rollen_kauf_aktiv(G_A, ANDERER, VIP)
    assert (k["preis_liste"], k["preis_bezahlt"], k["gutschein"]) == (1000, 850, "FEST50")
    assert bot.db.gutschein_holen(G_A, "FEST50")["uses"] == 1


def test_zu_wenig_guthaben(rwelt):
    _artikel(preis=10 ** 6)
    f = _kaufen(rwelt)
    assert isinstance(f, bot._KaufFehler) and f.code == "guthaben" and f.total == 10 ** 6
    assert not any(x.id == VIP for x in rwelt["u"].roles)


def test_zwei_gleichzeitige_kaeufe_zahlen_nur_einmal(rwelt):
    _artikel(preis=1000)
    guild, m = rwelt["ga"], rwelt["u"]
    plan = bot._rolle_kauf_pruefen(guild, G_A, U, set(), "VIP-Rolle", None, False)
    assert not isinstance(plan, bot._KaufFehler)

    async def beide():
        return await asyncio.gather(bot._rolle_kauf_ausfuehren(rwelt["a"], guild, G_A, U, "U", plan, "dashboard"),
                                    bot._rolle_kauf_ausfuehren(rwelt["a"], guild, G_A, U, "U", plan, "dashboard"))
    r = _run(beide())
    assert sorted(x.ok for x in r) == [False, True] and [x.code for x in r if not x.ok] == ["besitzt"]
    assert bot.db.get_balance(G_A, U)[0] == rwelt["start"] - 1000 and len(m.add_log) == 1


def test_mitglied_nicht_mehr_da(rwelt):
    _artikel()
    guild = rwelt["ga"]
    plan = bot._rolle_kauf_pruefen(guild, G_A, U, set(), "VIP-Rolle", None, False)
    del guild.mitglieder[U]
    r = _run(bot._rolle_kauf_ausfuehren(rwelt["a"], guild, G_A, U, "U", plan, "dashboard"))
    assert not r.ok and r.code == "mitglied" and bot.db.get_balance(G_A, U)[0] == rwelt["start"]


def test_artikel_zwischendurch_geaendert_oder_geloescht(rwelt):
    aid = _artikel()
    guild = rwelt["ga"]
    plan = bot._rolle_kauf_pruefen(guild, G_A, U, set(), "VIP-Rolle", None, False)
    bot.db.rollen_artikel_loeschen(G_A, aid)
    r = _run(bot._rolle_kauf_ausfuehren(rwelt["a"], guild, G_A, U, "U", plan, "dashboard"))
    assert not r.ok and bot.db.get_balance(G_A, U)[0] == rwelt["start"] and not rwelt["u"].add_log


def test_artikel_je_guild_getrennt(rwelt):
    _artikel(gid=G_A, name="Nur-A")
    f = _kaufen(rwelt, name="Nur-A", gid=G_B)
    assert isinstance(f, bot._KaufFehler) and f.code == "item"
    assert bot._rollen_artikel_finden(G_B, "Nur-A") is None
    assert bot._rollen_artikel_finden(G_A, str(bot.db.rollen_artikel_nach_name(G_A, "Nur-A")["id"])) is not None   # per Nummer


# ── Ablauf ───────────────────────────────────────────────────────────────
def _faellig(w, uid=U, role=BOOSTER, ende=None, gid=G_A):
    ende = time.time() - 10 if ende is None else ende
    return bot.db.rollen_kauf_anlegen(gid, uid, None, "Test", role, 100, 100, None, "t", ende)


def test_ablauf_entfernt_nur_abgelaufene_rollen(rwelt):
    rwelt["u"].roles.append(rwelt["ga"].rollen[BOOSTER])
    rwelt["anderer"].roles = [rwelt["ga"].rollen[BOOSTER]]
    alt = _faellig(rwelt, uid=U)
    noch = _faellig(rwelt, uid=ANDERER, ende=time.time() + 3600)
    forever = bot.db.rollen_kauf_anlegen(G_A, U, None, "P", VIP, 1, 1, None, "t", None)
    assert _run(bot._rollen_ablauf_pruefen()) == 1
    assert not any(r.id == BOOSTER for r in rwelt["u"].roles) and rwelt["u"].remove_log[0][1] == "Rollen-Shop (abgelaufen)"
    assert any(r.id == BOOSTER for r in rwelt["anderer"].roles)
    status = {k["id"]: k["status"] for k in bot.db.rollen_kaeufe_liste(G_A)}
    assert (status[alt], status[noch], status[forever]) == ("expired", "active", "active")
    assert _run(bot._rollen_ablauf_pruefen()) == 0                           # zweiter Lauf: nichts mehr


def test_ablauf_nachholen_nach_pause_und_fehlerfaelle(rwelt):
    guild = rwelt["ga"]
    # Rolle ist laengst weg / Mitglied ausgetreten -> trotzdem als abgelaufen markieren
    a = _faellig(rwelt, uid=U)                                               # U hat die Rolle gar nicht (mehr)
    b = _faellig(rwelt, uid=12345)                                           # unbekanntes Mitglied
    c = _faellig(rwelt, uid=U, role=GONE)                                    # Rolle geloescht
    assert _run(bot._rollen_ablauf_pruefen()) == 3
    assert {k["id"]: k["status"] for k in bot.db.rollen_kaeufe_liste(G_A)} == {a: "expired", b: "expired", c: "expired"}
    # Discord verweigert das Entfernen: bleibt aktiv, es wird weiter versucht
    rwelt["u"].roles.append(guild.rollen[BOOSTER])
    d = _faellig(rwelt, uid=U)
    guild.verweigern_entfernen = True
    assert _run(bot._rollen_ablauf_pruefen()) == 0 and bot.db.rollen_kauf_aktiv(G_A, U, BOOSTER)["id"] == d
    guild.verweigern_entfernen = False
    assert _run(bot._rollen_ablauf_pruefen()) == 1 and bot.db.rollen_kauf_aktiv(G_A, U, BOOSTER) is None


def test_ablauf_ueberspringt_unbekannte_guild(rwelt):
    k = _faellig(rwelt, gid=424242)                                          # Bot ist dort nicht
    assert _run(bot._rollen_ablauf_pruefen()) == 0
    assert [x["status"] for x in bot.db.rollen_kaeufe_liste(424242)] == ["active"] and k


# ── Slash-Befehl ─────────────────────────────────────────────────────────
class _SResp:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, embed=None, ephemeral=False):
        self.sent.append((content, embed))

    async def defer(self, ephemeral=False):
        pass


class _SFollow:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, ephemeral=False):
        self.sent.append((content, embed))


class _SInter:
    def __init__(self, guild, uid=U):
        self.guild, self.guild_id = guild, guild.id
        self.user = type("U", (), {"id": uid, "mention": f"<@{uid}>", "__str__": lambda s: "User"})()
        self.locale, self.response, self.followup = None, _SResp(), _SFollow()
        self.namespace = type("N", (), {})()

    def text(self):
        alle = self.response.sent + self.followup.sent
        return " ".join(str(c or "") + (str(e.to_dict()) if e else "") for c, e in alle)


def test_slash_kauf_und_fehler(rwelt):
    _artikel(preis=1000)
    i = _SInter(rwelt["ga"])
    _run(bot.shop_buy_role.callback(i, "VIP-Rolle"))
    assert "Rolle erhalten" in i.text() and any(x.id == VIP for x in rwelt["u"].roles)
    j = _SInter(rwelt["ga"])
    _run(bot.shop_buy_role.callback(j, "Gibt es nicht"))
    assert "gibt es nicht" in j.text()
    k = _SInter(rwelt["ga"])
    _run(bot.shop_buy_role.callback(k, "VIP-Rolle"))                          # schon im Besitz (rollen via ids: user ist kein Member -> None)
    assert k.response.sent or k.followup.sent


def test_slash_guthaben_und_gutschein_fehler(rwelt):
    _artikel(preis=10 ** 7, role=500, name="Teuer")
    i = _SInter(rwelt["ga"])
    _run(bot.shop_buy_role.callback(i, "Teuer"))
    assert "Insufficient funds" in i.text() and not rwelt["u"].add_log
    _artikel(preis=10, role=BOOSTER, name="Billig")
    j = _SInter(rwelt["ga"])
    _run(bot.shop_buy_role.callback(j, "Billig", "NIXDA"))
    assert "gibt es nicht" in j.text() and not rwelt["u"].add_log


def test_autocomplete_zeigt_nur_sichtbare_artikel_der_eigenen_guild(rwelt):
    _artikel(name="Sichtbar", preis=5)
    _artikel(name="Versteckt", hidden=True, role=BOOSTER)
    _artikel(name="Aus", enabled=False, role=500)
    _artikel(gid=G_B, name="Fremd")
    i = _SInter(rwelt["ga"])
    namen = [c.value for c in _run(bot._rolle_autocomplete(i, ""))]
    assert namen == ["Sichtbar"]
    assert [c.value for c in _run(bot._rolle_autocomplete(i, "sicht"))] == ["Sichtbar"] and _run(bot._rolle_autocomplete(i, "xyz")) == []


# ── Dashboard: Spieler ───────────────────────────────────────────────────
def test_spieler_liste_und_kauf(rwelt):
    _artikel(name="Dauerhaft", role=VIP, preis=1000, beschreibung="Schick")
    _artikel(name="Woche", role=BOOSTER, permanent=False, dauer_tage=7, preis=100)
    _artikel(name="Versteckt", role=500, hidden=True)
    _artikel(name="Gesperrt", role=MOD)                                      # ueber der Bot-Rolle
    s = _session(gid=G_A, sid_="1000", mitglied=(G_A, G_B))
    st, d = api("GET", "/api/me/roles?lang=de", s)
    namen = {i["name"]: i for i in d["data"]["items"]}
    assert st == 200 and set(namen) == {"Dauerhaft", "Woche", "Gesperrt"}
    assert namen["Dauerhaft"]["kaufbar"] and namen["Dauerhaft"]["permanent"] and namen["Dauerhaft"]["role_name"] == "VIP"
    assert not namen["Gesperrt"]["kaufbar"] and namen["Gesperrt"]["grund"]["code"] == "nicht_vergebbar"
    assert namen["Woche"]["dauer_tage"] == 7
    st, d = api("POST", "/api/me/roles/buy", s, {"item": "Dauerhaft", "lang": "de"})
    assert st == 200 and d["data"]["role_name"] == "VIP" and d["data"]["ende"] is None, d
    assert any(x.id == VIP for x in rwelt["u"].roles) and rwelt["u"].add_log[0][1] == "Rollen-Shop (dashboard)"
    d = api("GET", "/api/me/roles", s)[1]["data"]
    dauerhaft = {i["name"]: i for i in d["items"]}["Dauerhaft"]
    assert dauerhaft["besitzt"] and not dauerhaft["kaufbar"] and dauerhaft["grund"]["code"] == "besitzt"
    assert d["meine"][0]["artikel"] == "Dauerhaft" and d["meine"][0]["status"] == "active"


def test_spieler_kauf_fehlercodes(rwelt):
    _artikel(name="Teuer", preis=10 ** 7, role=500)
    _artikel(name="Gesperrt", role=MOD)
    _artikel(name="Aus", role=BOOSTER, enabled=False)
    s = _session(gid=G_A, sid_="1000")
    for item, status, code in (("Gibt es nicht", 404, "item"), ("Teuer", 402, "guthaben"), ("Gesperrt", 400, "nicht_vergebbar"),
                               ("Aus", 400, "nicht_verfuegbar")):
        bot._DASH_RATE_LIMIT_LAST.clear()
        st, d = api("POST", "/api/me/roles/buy", s, {"item": item})
        assert (st, d["code"]) == (status, code), (item, st, d)
    assert api("POST", "/api/me/roles/buy", None, {"item": "x"})[0] == 401
    assert api("POST", "/api/me/roles/buy", _session(), {"item": "x"})[0] == 409
    assert bot.db.get_balance(G_A, U)[0] == rwelt["start"] and not rwelt["u"].add_log


def test_spieler_kauf_discord_verweigert(rwelt):
    _artikel(name="Normal", preis=500)
    rwelt["ga"].verweigern = True
    st, d = api("POST", "/api/me/roles/buy", _session(gid=G_A, sid_="1000"), {"item": "Normal", "lang": "en"})
    assert st == 500 and d["code"] == "rolle" and "not charged" in d["error"]
    assert bot.db.get_balance(G_A, U)[0] == rwelt["start"]


def test_spieler_quote_rolle_mit_rabatt_und_gutschein(rwelt):
    _artikel(name="Normal", preis=1000)
    bot.db.rabatt_ersetzen(G_A, [(BOOSTER, "percent", 10)])
    bot.db.gutschein_speichern(G_A, "FEST50", "fixed", 50, True, None, None, None, True)
    s = _session(uid=ANDERER, gid=G_A, sid_="1000")
    st, d = api("POST", "/api/me/quote", s, {"art": "role", "item": "Normal", "gutschein": "fest50"})
    assert st == 200 and d["data"]["total_text"].startswith("850") and d["data"]["rabatt"] == 100
    assert api("POST", "/api/me/quote", s, {"art": "role", "item": "Nix"})[0] == 404


def test_spieler_ohne_mitgliedschaft_und_fremde_guild(rwelt):
    _artikel(name="Normal")
    assert api("GET", "/api/me/roles", _session(mitglied=(G_B,), gid=G_A, sid_="1000"))[0] == 403
    assert api("POST", "/api/me/roles/buy", _session(mitglied=(G_B,), gid=G_A, sid_="1000"), {"item": "Normal"})[0] == 403
    _artikel(gid=G_B, name="Nur-B", role=VIP)
    st, d = api("GET", "/api/me/roles", _session(gid=G_A, sid_="1000"))
    assert [i["name"] for i in d["data"]["items"]] == ["Normal"]


# ── Dashboard: Betreiber ─────────────────────────────────────────────────
def test_admin_artikel_crud_und_bestellungen(rwelt):
    s = _admin()
    st, d = api("POST", "/api/roleshop/items", s, _feld(name="Booster-Rolle", role_id=str(BOOSTER), permanent=False, dauer_tage=30,
                                                       bestand=5, benoetigt=[str(VIP)], hidden=True))
    assert st == 200, d
    aid = d["data"]["id"]
    st, d = api("GET", "/api/roleshop/items", s)
    it = d["data"]["items"][0]
    assert st == 200 and (it["name"], it["role_name"], it["vergebbar"], it["permanent"], it["dauer_tage"], it["bestand"], it["hidden"],
                          it["benoetigt"]) == ("Booster-Rolle", "Booster", True, False, 30, 5, True, [str(VIP)])
    bot._DASH_RATE_LIMIT_LAST.clear()
    assert api("PUT", f"/api/roleshop/items/{aid}", s, _feld(name="Umbenannt", preis=900))[0] == 200
    assert api("GET", "/api/roleshop/items", s)[1]["data"]["items"][0]["name"] == "Umbenannt"
    assert api("PUT", "/api/roleshop/items/9999", s, _feld())[0] == 404
    bot.db.rollen_kauf_anlegen(G_A, U, aid, "Umbenannt", BOOSTER, 900, 900, None, "discord", None)
    st, d = api("GET", "/api/roleshop/orders", s)
    assert st == 200 and d["data"]["orders"][0]["artikel"] == "Umbenannt" and d["data"]["orders"][0]["user_name"] == "User555"
    assert api("DELETE", f"/api/roleshop/items/{aid}", s)[0] == 200
    assert api("DELETE", f"/api/roleshop/items/{aid}", s)[0] == 404


def test_admin_validierung(rwelt):
    s = _admin()
    schlecht = [_feld(role_id="abc"), _feld(role_id=str(GONE)), _feld(role_id=str(MOD)), _feld(role_id=str(MANAGED)),
                _feld(name=""), _feld(name="x" * 61), _feld(beschreibung="x" * 101), _feld(preis=0), _feld(preis="x"),
                _feld(permanent=False), _feld(permanent=False, dauer_tage=0), _feld(permanent=False, dauer_tage=99999),
                _feld(bestand=0), _feld(bestand="x"), _feld(benoetigt=[str(VIP)] * 6 + ["1"] * 0 + [str(BOOSTER)]),
                _feld(benoetigt=[str(GONE)]), _feld(benoetigt="x"),
                _feld(verfuegbar_ab=2000000000, verfuegbar_bis=1900000000), _feld(verfuegbar_ab="abc")]
    for f in schlecht:
        bot._DASH_RATE_LIMIT_LAST.clear()
        st, d = api("POST", "/api/roleshop/items", s, f)
        assert st in (400, 409) and not d["ok"], f
    assert bot.db.rollen_artikel_liste(G_A) == []


def test_admin_doppelter_name_und_limit(rwelt, monkeypatch):
    s = _admin()
    assert api("POST", "/api/roleshop/items", s, _feld(name="Doppelt"))[0] == 200
    bot._DASH_RATE_LIMIT_LAST.clear()
    assert api("POST", "/api/roleshop/items", s, _feld(name="doppelt"))[0] == 409
    monkeypatch.setattr(bot, "_ROLLEN_ARTIKEL_MAX", 1)
    bot._DASH_RATE_LIMIT_LAST.clear()
    st, d = api("POST", "/api/roleshop/items", s, _feld(name="Zweiter"))
    assert st == 409 and "Höchstens" in d["error"]


def test_admin_nur_eigene_guild_und_nicht_fuer_spieler(rwelt):
    _artikel(gid=G_B, name="Fremd")
    assert api("GET", "/api/roleshop/items", _admin())[1]["data"]["items"] == []
    assert api("DELETE", f"/api/roleshop/items/{bot.db.rollen_artikel_nach_name(G_B, 'Fremd')['id']}", _admin())[0] == 404
    spieler = _session(gid=G_A, sid_="1000")
    for methode, pfad, body in (("GET", "/api/roleshop/items", None), ("POST", "/api/roleshop/items", _feld()),
                                ("PUT", "/api/roleshop/items/1", _feld()), ("DELETE", "/api/roleshop/items/1", None),
                                ("GET", "/api/roleshop/orders", None)):
        st, d = api(methode, pfad, spieler, body)
        assert st in (401, 403, 409) and not (d or {}).get("ok"), pfad
        assert api(methode, pfad, None, body)[0] == 401
    assert bot.db.rollen_artikel_liste(G_A) == []
    assert bot.db.rollen_artikel_nach_name(G_B, "Fremd") is not None


def test_rollen_endpunkt_meldet_vergebbarkeit(rwelt):
    st, d = api("GET", f"/api/guild/{G_A}/roles", _admin())
    r = {x["name"]: x for x in d["data"]["roles"]}
    assert st == 200 and r["VIP"]["vergebbar"] and not r["Moderator"]["vergebbar"] and not r["Bot-Rolle"]["vergebbar"]
    assert r["Moderator"]["position"] == 10 and r["Bot-Rolle"]["managed"] is True


def test_ablauf_schickt_eine_nachricht_nur_wenn_die_rolle_entfernt_wurde(rwelt, monkeypatch):
    monkeypatch.setattr(bot.cfg, "sprache", {})
    guild = rwelt["ga"]
    rwelt["u"].roles.append(guild.rollen[BOOSTER])
    bot.db.rollen_kauf_anlegen(G_A, U, None, "Booster-Woche", BOOSTER, 100, 100, None, "t", time.time() - 10)
    assert _run(bot._rollen_ablauf_pruefen()) == 1
    assert len(rwelt["u"].dms) == 1 and "Booster-Woche" in rwelt["u"].dms[0] and "abgelaufen" in rwelt["u"].dms[0]
    # Rolle war schon von Hand entfernt: abgelaufen markieren, aber keine Nachricht
    bot.db.rollen_kauf_anlegen(G_A, U, None, "Nochmal", BOOSTER, 100, 100, None, "t", time.time() - 10)
    assert _run(bot._rollen_ablauf_pruefen()) == 1 and len(rwelt["u"].dms) == 1
    # Sprache des Kontos: Englisch
    bot.cfg.sprache["555"] = "en"
    rwelt["u"].roles.append(guild.rollen[BOOSTER])
    bot.db.rollen_kauf_anlegen(G_A, U, None, "Booster-Week", BOOSTER, 100, 100, None, "t", time.time() - 10)
    _run(bot._rollen_ablauf_pruefen())
    assert "has expired" in rwelt["u"].dms[-1]


def test_geschlossene_dms_stoeren_den_ablauf_nicht(rwelt):
    guild = rwelt["ga"]
    guild.dm_zu = True
    rwelt["u"].roles.append(guild.rollen[BOOSTER])
    k = bot.db.rollen_kauf_anlegen(G_A, U, None, "Booster-Woche", BOOSTER, 100, 100, None, "t", time.time() - 10)
    assert _run(bot._rollen_ablauf_pruefen()) == 1
    assert not any(r.id == BOOSTER for r in rwelt["u"].roles)
    assert {x["id"]: x["status"] for x in bot.db.rollen_kaeufe_liste(G_A)}[k] == "expired"
    # Mitglied ausgetreten: keine Nachricht, kein Fehler
    del guild.mitglieder[U]
    bot.db.rollen_kauf_anlegen(G_A, U, None, "X", BOOSTER, 1, 1, None, "t", time.time() - 10)
    assert _run(bot._rollen_ablauf_pruefen()) == 1
