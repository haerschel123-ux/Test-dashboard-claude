"""Discord Management: jede Kachel einzeln im Modul Manager sperrbar (discord_mgmt.<kachel>)."""
import pytest

import bot
from test_spieler_dashboard import api, welt  # noqa: F401 - Fixtures
from test_airstrike import servers  # noqa: F401 - Fixture
from test_gluecksrad import _admin


@pytest.fixture(autouse=True)
def _stufen(monkeypatch):
    monkeypatch.setitem(bot.cfg.config, "module_tiers", {})


def test_jede_kachel_hat_ein_modul_und_erbt_die_kategorie():
    kacheln = [k for k in bot.FEATURE_MODULES if k.startswith("discord_mgmt.")]
    assert len(kacheln) == 18 and "discord_mgmt.verifizierung" in kacheln
    bot.cfg.config["module_tiers"] = {"discord_mgmt": "beta"}
    assert all(bot._module_tier(k) == "beta" for k in kacheln)
    bot.cfg.config["module_tiers"] = {"discord_mgmt": "beta", "discord_mgmt.honeypot": "public"}
    assert bot._module_tier("discord_mgmt.honeypot") == "public"


def test_befehle_haengen_an_ihrer_kachel():
    m = bot._DISCORD_MODUL_MAP
    assert m["level"] == m["levelset"] == "discord_mgmt.level_system"
    assert m["verify"] == m["manverify"] == m["verification"] == "discord_mgmt.verifizierung"
    assert m["wheel"] == m["claim"] == "discord_mgmt.gluecksrad" and m["loan"] == "discord_mgmt.kredite"
    assert "privacy" not in m                       # Datenauskunft/-löschung bleibt immer erreichbar


@pytest.mark.parametrize("pfad,kachel", [
    ("/api/discord-management/gluecksrad", "gluecksrad"),
    ("/api/discord-management/honeypot", "honeypot"),
    ("/api/discord-management/verifizierung", "verifizierung"),
    ("/api/discord-management/tickets/categories", "ticket_tool"),
    ("/api/discord-management/reaction-roles", "reaction_roles"),
    ("/api/discord-management/ki-helfer", "ki_helfer"),
])
def test_under_review_sperrt_nur_diese_kachel_fuer_kunden(welt, pfad, kachel):
    bot.cfg.config["module_tiers"] = {"discord_mgmt." + kachel: "under_review"}
    kunde = _admin(welt, admin=False)
    st, d = api("GET", pfad, kunde)
    assert st == 403 and d.get("code") == "under_review", (st, d)
    assert api("GET", pfad, _admin(welt))[0] != 403            # Betreiber kommt durch (Stub-Guild kann 500 liefern)
    assert api("GET", "/api/discord-management/wortfilter", kunde)[0] == 200   # andere Kachel frei
    st, d = api("GET", "/api/discord-management", kunde)
    assert st == 200 and d["data"]["kachel_sperren"][kachel] == "under_review"
    assert d["data"]["kachel_sperren"]["wortfilter"] is None
    assert d["data"]["kachel_stufen"][kachel] == "under_review"


def test_beta_kachel_ohne_beta_gesperrt(welt):
    bot.cfg.config["module_tiers"] = {"discord_mgmt.kredite": "beta"}
    st, d = api("POST", "/api/discord-management/kredite", _admin(welt, admin=False), {})
    assert st == 403 and d.get("code") == "beta"


def test_social_je_plattform(welt):
    bot.cfg.config["module_tiers"] = {"discord_mgmt.social_youtube": "under_review"}
    kunde = _admin(welt, admin=False)
    st, d = api("POST", "/api/discord-management/social", kunde, {"platform": "youtube", "source": "x"})
    assert st == 403 and d.get("code") == "under_review"
    st, d = api("POST", "/api/discord-management/social/pruefen", kunde, {"platform": "youtube", "source": "x"})
    assert st == 403 and d.get("code") == "under_review"
    st, d = api("POST", "/api/discord-management/social", kunde, {"platform": "tiktok", "source": ""})
    assert d.get("code") != "under_review"
    assert api("GET", "/api/discord-management/social", kunde)[0] == 200
