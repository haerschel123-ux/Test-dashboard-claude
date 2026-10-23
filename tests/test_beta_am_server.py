"""Beta haengt am Server: Admins eines Beta-Servers duerfen dort die Beta-Module nutzen - ohne eigene Beta-Rolle.

Vorher verlangten Beta-Module die Beta-Rolle der einzelnen Person im Haupt-Discord. Ein Admin (Gast mit Dashboard Permissions)
auf dem Server eines Beta-Kunden kam nicht hinein, obwohl der Server selbst Beta hat. Premium hing schon am Server.

Ein Server gilt als Beta-Server, wenn er freigeschaltet ist (Guild zugeordnet) UND die Stufe beta/premium_beta hat ODER sein
Eigentuemer die Beta-Rolle traegt. Gilt nur auf diesem Server, fuer Dashboard und Discord-Befehle.

    python3 -m pytest tests/test_beta_am_server.py -q
"""
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

HAUPT_GUILD = 555
BESITZER_BETA, BESITZER_PREMIUM, BESITZER_FREI = "10", "11", "13"
GAST_A, GAST_B, GAST_C = "20", "21", "23"
PERMS = {"tools": ["view", "edit"]}


def _conn(sid, besitzer, guilds, stufe, gaeste=()):
    return {"service_id": sid, "nitrado_token": "geheim-" + sid, "owner_discord_id": besitzer, "name": "Server " + sid,
            "guild_ids": list(guilds), "kunden_stufe": stufe,
            "dashboard_perms": {f"user:{g}": PERMS for g in gaeste}}


@pytest.fixture
def welt(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    flat = {
        "1000": _conn("1000", BESITZER_BETA, [777], "beta", [GAST_A]),                 # Stufe Beta
        "2000": _conn("2000", BESITZER_PREMIUM, [888], "premium", [GAST_B]),           # Premium, Besitzer ohne Beta-Rolle
        "3000": _conn("3000", "12", [], "beta"),                                       # Beta-Stufe, aber nicht freigeschaltet
        "4000": _conn("4000", BESITZER_FREI, [999], "premium", [GAST_C]),              # Premium, ABER Besitzer traegt die Beta-Rolle
        "5000": _conn("5000", "14", [666], "premium_beta", [GAST_A]),                  # Premium + Beta
    }
    (tmp_path / "connections.json").write_text(json.dumps(flat))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {
        "service_id": "9999", "premium_role_guild_id": str(HAUPT_GUILD),
        "module_tiers": {"tools.horde": "beta", "tools.vehicle": "premium", "tools.weather": "under_review",
                         "tools.teleports": "public", "shop.buy": "beta"}})
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    monkeypatch.setattr(bot, "_BETA_BESITZER_CACHE", {})
    rollen = SimpleNamespace(haben={(int(BESITZER_FREI), bot.BETA_ROLLE_ID)}, abfragen=[])

    async def hat_rolle(guild_id, user_id, role_id):
        rollen.abfragen.append((guild_id, user_id, role_id))
        return guild_id == HAUPT_GUILD and (int(user_id), int(role_id)) in rollen.haben
    monkeypatch.setattr(bot, "_user_hat_rolle", hat_rolle)
    rollen.reg = reg
    return rollen


def _sess(user, service, *, admin=False, gast=False):
    return {"token": "", "discord": {"id": user}, "is_admin": admin, "is_guest": gast, "service_id": service,
            "seen": time.time()}


def _erlaubt(key, sess):
    return asyncio.run(bot._module_erlaubt(key, sess, bot.connections.for_service(sess["service_id"])))


# ── Dashboard ───────────────────────────────────────────────────────────
def test_gast_auf_beta_server_darf_beta_module_ohne_eigene_rolle(welt):
    assert _erlaubt("tools.horde", _sess(GAST_A, "1000", gast=True)) is True


def test_besitzer_eines_beta_servers_darf_auch_ohne_rolle(welt):
    assert _erlaubt("tools.horde", _sess(BESITZER_BETA, "1000")) is True


def test_gast_auf_server_ohne_beta_bleibt_gesperrt(welt):
    assert _erlaubt("tools.horde", _sess(GAST_B, "2000", gast=True)) is False
    assert _erlaubt("tools.horde", _sess(BESITZER_PREMIUM, "2000")) is False


def test_besitzer_mit_beta_rolle_macht_den_server_zum_beta_server(welt):
    """Stufe nur Premium - aber der Eigentuemer ist Beta-Tester: seine Admins duerfen Beta nutzen."""
    assert _erlaubt("tools.horde", _sess(GAST_C, "4000", gast=True)) is True
    assert _erlaubt("tools.horde", _sess(BESITZER_FREI, "4000")) is True


def test_premium_plus_beta_stufe_zaehlt(welt):
    assert _erlaubt("tools.horde", _sess(GAST_A, "5000", gast=True)) is True


def test_nicht_freigeschalteter_server_gibt_kein_beta(welt):
    """Stufe Beta, aber keine Guild zugeordnet: Kunden schalten sich nicht selbst frei."""
    assert _erlaubt("tools.horde", _sess("12", "3000")) is False


def test_beta_gilt_nur_auf_dem_eigenen_server(welt):
    """Gast von Server 1000 (Beta) darf auf 2000 (kein Beta) nichts - auch nicht, wenn sich die Sitzung auf 2000 richtet."""
    assert _erlaubt("tools.horde", _sess(GAST_A, "2000", gast=True)) is False
    assert _erlaubt("tools.horde", _sess(BESITZER_BETA, "2000")) is False


def test_fremder_ohne_eintrag_auf_beta_server_bekommt_nichts(welt):
    assert _erlaubt("tools.horde", _sess("99", "1000")) is False
    assert _erlaubt("tools.horde", _sess("99", "1000", gast=True)) is False            # Gast ohne Dashboard Permission
    assert _erlaubt("tools.horde", {"token": "", "discord": None, "is_admin": False, "service_id": "1000"}) is False


def test_entzogene_dashboard_permission_nimmt_auch_beta_weg(welt):
    sess = _sess(GAST_A, "1000", gast=True)
    assert _erlaubt("tools.horde", sess) is True
    welt.reg.for_service("1000").data["dashboard_perms"] = {}
    assert _erlaubt("tools.horde", sess) is False


def test_persoenliche_beta_rolle_gilt_weiter(welt):
    welt.haben.add((int("77"), bot.BETA_ROLLE_ID))
    assert _erlaubt("tools.horde", _sess("77", "2000", gast=True)) is True                # kein Beta-Server, aber eigene Rolle


def test_andere_stufen_bleiben_wie_sie_waren(welt):
    gast = _sess(GAST_A, "1000", gast=True)
    assert _erlaubt("tools.teleports", gast) is True                                      # public: jeder
    assert _erlaubt("tools.weather", gast) is False                                       # under_review: nur Betreiber
    assert _erlaubt("tools.vehicle", gast) is False                                       # premium: Stufe "beta" gibt KEIN Premium
    assert _erlaubt("tools.vehicle", _sess(GAST_B, "2000", gast=True)) is True            # Premium-Server wie bisher
    assert _erlaubt("tools.weather", _sess("1", "1000", admin=True)) is True              # Betreiber immer


# ── HTTP-Antwort der Modul-Pruefung ──────────────────────────────────────
def _pruefen(key, sess):
    sid = "s" + str(time.time_ns())
    bot._SESS_STORE[sid] = sess
    request = make_mocked_request("GET", "/api/tools/x", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    return asyncio.run(bot._modul_pruefen(key, request, bot.connections.for_service(sess["service_id"])))


def test_modul_pruefung_laesst_gast_auf_beta_server_durch_und_sperrt_sonst(welt):
    assert _pruefen("tools.horde", _sess(GAST_A, "1000", gast=True)) is None
    antwort = _pruefen("tools.horde", _sess(GAST_B, "2000", gast=True))
    assert antwort.status == 403 and json.loads(antwort.body)["code"] == "beta"


# ── Status-Abzeichen ────────────────────────────────────────────────────
def _session_antwort(sess):
    sid = "s" + str(time.time_ns())
    bot._SESS_STORE[sid] = sess
    request = make_mocked_request("GET", "/api/session", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})
    return json.loads(asyncio.run(bot.api_get_session(request)).body)["data"]


def test_abzeichen_beta_folgt_dem_server(welt, monkeypatch):
    monkeypatch.setattr(bot, "_discord_login_enabled", lambda: True)
    assert _session_antwort(_sess(GAST_A, "1000", gast=True))["beta"] is True
    assert _session_antwort(_sess(GAST_B, "2000", gast=True))["beta"] is False


# ── Discord-Befehle ──────────────────────────────────────────────────────
def _interaktion(guild_id, user_id, befehl="shop"):
    gesendet = []

    async def senden(text, ephemeral=False):
        gesendet.append(text)
    return SimpleNamespace(guild_id=guild_id, user=SimpleNamespace(id=int(user_id)), gesendet=gesendet,
                           command=SimpleNamespace(qualified_name=befehl),
                           response=SimpleNamespace(is_done=lambda: False, send_message=senden),
                           followup=SimpleNamespace(send=senden))


def test_discord_befehl_beta_modul_geht_auf_der_guild_eines_beta_servers(welt):
    i = _interaktion(777, GAST_A)
    assert asyncio.run(bot._premium_check(i)) is True and i.gesendet == []


def test_discord_befehl_beta_modul_bleibt_auf_anderer_guild_gesperrt(welt):
    i = _interaktion(888, GAST_B)                                    # Guild von Server 2000: Premium, aber kein Beta
    assert asyncio.run(bot._premium_check(i)) is False
    assert i.gesendet == [bot.BETA_GESPERRT_TEXT]


def test_discord_befehl_beta_ueber_besitzer_rolle(welt):
    assert asyncio.run(bot._premium_check(_interaktion(999, GAST_C))) is True


def test_discord_befehl_unbekannte_guild_bleibt_gesperrt(welt):
    assert asyncio.run(bot._premium_check(_interaktion(123456, GAST_A))) is False


def test_discord_befehl_persoenliche_beta_rolle_gilt_weiter(welt):
    welt.haben.add((77, bot.BETA_ROLLE_ID))
    assert asyncio.run(bot._premium_check(_interaktion(888, "77"))) is True


# ── Rollenabfrage ────────────────────────────────────────────────────────
def test_besitzer_rolle_wird_eine_minute_gemerkt(welt):
    conn = welt.reg.for_service("4000")
    for _ in range(5):
        assert asyncio.run(bot._server_hat_beta(conn)) is True
    assert sum(1 for a in welt.abfragen if a[1] == int(BESITZER_FREI)) == 1
    bot._BETA_BESITZER_CACHE[BESITZER_FREI] = (time.time() - bot._BETA_BESITZER_TTL - 1, True)
    welt.haben.clear()                                                # Rolle inzwischen entzogen
    assert asyncio.run(bot._server_hat_beta(conn)) is False


def test_ohne_haupt_discord_in_der_konfiguration_gibt_es_nur_die_stufe(welt, monkeypatch):
    monkeypatch.setitem(bot.cfg.config, "premium_role_guild_id", "")
    assert asyncio.run(bot._server_hat_beta(welt.reg.for_service("4000"))) is False       # nur Besitzer-Rolle, nicht pruefbar
    assert asyncio.run(bot._server_hat_beta(welt.reg.for_service("1000"))) is True        # Stufe reicht


# ── Echte Route (Middleware, Sitzung, Gast-Pruefung, Modul-Gate) ──────────
class _Ftp:
    """Minimaler FTP fuer den Territory-Exclusion-Lesezugriff."""

    def __init__(self, dateien):
        self.files = dict(dateien)

    def list_dir(self, d):
        d = d.rstrip("/") + "/"
        return sorted(p for p in self.files if p.startswith(d))

    def read_file_ex(self, p):
        return (self.files[p], "ok") if p in self.files else (None, "missing")


def _echte_route(welt, monkeypatch, user, service, *, gast):
    from aiohttp.test_utils import TestClient, TestServer
    fix = os.path.join(REPO, "tests", "fixtures", "territory", "nasdara_wolf_territories.xml")
    with open(fix, encoding="utf-8", newline="") as f:
        wolf = f.read()
    monkeypatch.setitem(bot.cfg.config["module_tiers"], "tools.territoryexclusion", "beta")
    monkeypatch.setitem(bot.cfg.config, "discord_client_id", "123")
    monkeypatch.setitem(bot.cfg.config, "discord_client_secret", "x")
    monkeypatch.setattr(bot.cfg, "consent", {user: {"version": bot._CONSENT_VERSION, "ts": 0}})
    conn = welt.reg.for_service(service)
    conn.ftp, conn.api = _Ftp({"/mission/env/wolf_territories.xml": wolf}), object()
    conn.data["ftp_mission_dir"] = "/mission"
    sid = "s" + str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "", "discord": {"id": user, "name": "Admin"}, "is_admin": False, "is_guest": gast,
                            "service_id": service, "guild_id": None, "seen": time.time(), "owned_guilds": [], "member_guilds": []}

    async def lauf():
        async with TestClient(TestServer(bot.build_app())) as c:
            r = await c.get("/api/tools/territoryexclusion", cookies={bot._SESS_COOKIE: sid})
            try:
                return r.status, await r.json()
            except Exception:  # noqa: BLE001
                return r.status, None
    return asyncio.run(lauf())


def test_echte_route_gast_auf_beta_server_kommt_ins_beta_tool(welt, monkeypatch):
    status, antwort = _echte_route(welt, monkeypatch, GAST_A, "1000", gast=True)
    assert status == 200, (status, antwort)
    assert antwort["ok"] is True


def test_echte_route_gast_auf_server_ohne_beta_wird_abgewiesen(welt, monkeypatch):
    status, antwort = _echte_route(welt, monkeypatch, GAST_B, "2000", gast=True)
    assert status == 403 and antwort.get("code") == "beta"
