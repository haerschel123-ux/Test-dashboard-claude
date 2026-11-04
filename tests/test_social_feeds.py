"""Social-Benachrichtigungen (Dashboard -> Discord Management): YouTube, Instagram, TikTok, X ueber FEEDS.

Der Bot liest nur Atom/RSS-Feeds: bei YouTube den offiziellen Kanal-Feed, sonst eine Feed-Adresse, die der Kunde selbst eintraegt.
Testdaten: tests/fixtures/social/ (echte YouTube-Antwort, kuenstlicher Kunden-Feed). Der Netzzugriff ist in diesen Tests immer
ersetzt - es geht kein Test ins Internet.

    python3 -m pytest tests/test_social_feeds.py -q
"""
import asyncio
import json
import os
import sys
import time

import pytest
from aiohttp.test_utils import make_mocked_request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

FIXTURES = os.path.join(REPO, "tests", "fixtures", "social")
with open(os.path.join(FIXTURES, "youtube_feed.xml"), "rb") as _f:
    YT = _f.read()
with open(os.path.join(FIXTURES, "customer_rss.synthetic.xml"), "rb") as _f:
    KUNDEN_RSS = _f.read()

KANAL = "UCBR8-60-B28hp2BmDPdntcQ"
G1, G2 = 7101, 7102                  # Guild 1 und 2 gehoeren zu Server 1000, Guild 3 zu Server 2000
G3 = 7103
K1, K2, K3, K_FREMD = 1001, 1002, 1003, 9009
R1, R_FREMD = 501, 599


# ── Testdoubles ─────────────────────────────────────────────────────────────
class _Kanal:
    def __init__(self, id_, guild_id, fehler=None):
        self.id, self.guild, self.sent, self._fehler = id_, type("G", (), {"id": guild_id})(), [], fehler

    async def send(self, content=None, **kw):
        if self._fehler is not None:
            raise self._fehler
        self.sent.append({"content": content, "allowed": kw.get("allowed_mentions")})


class _Guild:
    def __init__(self, id_, kanaele, rollen):
        self.id, self._k, self._r = id_, {k.id: k for k in kanaele}, set(rollen)

    def get_channel(self, cid):
        return self._k.get(int(cid))

    def get_role(self, rid):
        return object() if int(rid) in self._r else None

    def get_member(self, _uid):
        return None


def _atom(videos, titel="Mein Kanal"):
    """YouTube-aehnlicher Feed: videos = [(videoId, titel)] (neueste zuerst)."""
    eintraege = "".join(
        f'<entry><id>yt:video:{v}</id><yt:videoId>{v}</yt:videoId><title>{t}</title>'
        f'<link rel="alternate" href="https://www.youtube.com/watch?v={v}"/><author><name>{titel}</name></author>'
        f'<published>2026-11-0{i + 1}T10:00:00+00:00</published></entry>' for i, (v, t) in enumerate(videos))
    return (f'<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">'
            f'<title>{titel}</title>{eintraege}</feed>').encode()


def _vid(n):
    return f"vid{n:08d}"                # genau 11 Zeichen


def _rss(posts):
    """RSS-Feed fuer Kunden-Adressen: posts = [(guid, titel, link)] (neueste zuerst)."""
    items = "".join(f"<item><guid>{g}</guid><title>{t}</title><link>{l}</link></item>" for g, t, l in posts)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Demo</title>{items}</channel></rss>'.encode()


@pytest.fixture
def welt(monkeypatch, tmp_path):
    """Server 1000 mit Guild 1 (+ Guild 2) und Server 2000 mit Guild 3; Discord-Doubles; Netzzugriff durch `abruf` ersetzt."""
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
    monkeypatch.setattr(bot, "_SOCIAL_CACHE", {})
    a, b = reg._conns["1000"], reg._conns["2000"]
    reg.add_guild("1000", G1)
    reg.add_guild("1000", G2)
    reg.add_guild("2000", G3)
    a.data["kunden_stufe"] = b.data["kunden_stufe"] = "premium"
    k = {n: _Kanal(n, g) for n, g in ((K1, G1), (K2, G1), (K3, G2), (K_FREMD, G3))}
    guilds = {G1: _Guild(G1, [k[K1], k[K2]], [R1]), G2: _Guild(G2, [k[K3]], []), G3: _Guild(G3, [k[K_FREMD]], [R_FREMD])}
    monkeypatch.setattr(bot.bot, "get_guild", lambda gid: guilds.get(int(gid)), raising=False)
    monkeypatch.setattr(bot.bot, "get_channel", lambda cid: k.get(int(cid)), raising=False)
    monkeypatch.setattr(bot.bot, "is_ready", lambda: True, raising=False)

    async def schnell(_s):
        return None
    monkeypatch.setattr(bot.asyncio, "sleep", schnell)
    welt_ = type("Welt", (), {})()
    welt_.a, welt_.b, welt_.k, welt_.abrufe, welt_.antworten = a, b, k, [], {}

    async def abruf(url):
        welt_.abrufe.append(url)
        antwort = welt_.antworten.get(url, welt_.antworten.get("*"))
        if isinstance(antwort, bot.SocialFehler):
            raise antwort
        if antwort is None:
            raise bot.SocialFehler("nicht_erreichbar")
        return antwort
    monkeypatch.setattr(bot, "_social_abrufen", abruf)
    return welt_


def _abo(plattform="youtube", quelle=KANAL, kanal=K1, rolle=None, name="Test", sprache="de", template="", enabled=True, **extra):
    d = {"platform": plattform, "source": quelle, "channel_id": str(kanal), "role_id": str(rolle) if rolle else None, "name": name,
         "language": sprache, "template": template, "enabled": enabled}
    d.update(extra)
    return d


def _api(monkeypatch, conn, handler, daten=None, methode="POST", admin=True, guild=None, match=None, gast=None):
    """Ruft einen Dashboard-Handler auf. gast=<Discord-ID>: weder Betreiber noch Besitzer (Rechte aus dashboard_perms)."""
    sid = str(time.time_ns())
    discord_id = gast or (sid if admin else str(conn.data["owner_discord_id"]))
    sess = {"token": "x", "discord": {"id": discord_id}, "is_admin": admin and not gast, "service_id": conn.service_id,
            "seen": time.time(), "admin_geprueft_ts": time.time()}
    if gast:
        sess["is_guest"] = True
    if guild:
        sess["guild_id"] = str(guild)
    bot._SESS_STORE[sid] = sess
    req = make_mocked_request(methode, "/api/discord-management/social", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"},
                              match_info=match or {})

    async def body(_r):
        return daten
    monkeypatch.setattr(bot, "body", body)
    bot._DASH_RATE_LIMIT_LAST.clear()
    r = asyncio.run(handler(req))
    return r.status, json.loads(r.body)


def _anlegen(monkeypatch, conn, **kw):
    daten = {"platform": "youtube", "source": KANAL, "channel_id": str(K1), "language": "de", "template": "", "enabled": True}
    daten.update(kw)
    return _api(monkeypatch, conn, bot.post_discord_mgmt_social, daten)


def _lauf(jetzt):
    return asyncio.run(bot._social_lauf(jetzt))


def _abos(conn, guild=G1):
    return bot._social_abos(bot.GuildSicht(conn, guild))


# ═══ Parser ═════════════════════════════════════════════════════════════════
def test_echter_youtube_feed():
    titel, eintraege = bot._social_feed_parsen(YT, "youtube")
    assert titel == "YouTube" and len(eintraege) == 15
    e = eintraege[0]
    assert e["id"] == "yt:video:FFK9hSYmEu0" and e["link"] == "https://www.youtube.com/watch?v=FFK9hSYmEu0"
    assert e["titel"] == "leaf @dognamedstella alone, she has friends over" and e["autor"] == "YouTube"
    assert e["zeit"] == pytest.approx(1793721621)                      # 2026-11-03T16:00:21Z
    assert len({x["id"] for x in eintraege}) == 15 and all(x["link"].startswith("https://www.youtube.com/watch?v=") for x in eintraege)


def test_kunden_feed_synthetisch():
    titel, eintraege = bot._social_feed_parsen(KUNDEN_RSS, "instagram")
    assert titel == "Synthetic demo"
    assert eintraege == [{"id": "demo-post-1", "titel": "Demo post", "link": "https://example.org/post/1", "autor": "",
                          "zeit": pytest.approx(1793782800)}]          # Wed, 04 Nov 2026 09:00:00 GMT


@pytest.mark.parametrize("roh,art", [
    (b"", "kein_feed"),
    (b"<!DOCTYPE html><html><body>Bitte anmelden</body></html>", "kein_feed"),
    (b"<html><head></head></html>", "kein_feed"),
    (b"not xml at all", "kein_feed"),
    (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss version="2.0"><channel><title>&a;</title></channel></rss>', "kein_feed"),
    (b'<?xml version="1.0"?><foo><bar/></foo>', "kein_feed"),
    (b'<?xml version="1.0"?><rss version="2.0"></rss>', "kein_feed"),
    (b"<rss>" + b"x" * 1_600_000, "zu_gross"),
])
def test_kein_feed_ist_ein_fehler_nie_keine_neuen_beitraege(roh, art):
    with pytest.raises(bot.SocialFehler) as ex:
        bot._social_feed_parsen(roh, "x")
    assert ex.value.art == art


def test_gueltiger_leerer_feed_ist_kein_fehler():
    assert bot._social_feed_parsen(b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Leer</title></feed>', "x") == ("Leer", [])
    assert bot._social_feed_parsen(b'<rss version="2.0"><channel><title>Leer</title></channel></rss>', "x") == ("Leer", [])


def test_eintraege_ohne_brauchbaren_link_oder_video_werden_uebersprungen():
    rss = (b'<rss version="2.0"><channel><title>T</title>'
           b'<item><guid>1</guid><title>ohne Link</title></item>'
           b'<item><guid>2</guid><title>javascript</title><link>javascript:alert(1)</link></item>'
           b'<item><guid>3</guid><title>ok</title><link>https://example.org/ok</link></item></channel></rss>')
    assert [e["id"] for e in bot._social_feed_parsen(rss, "x")[1]] == ["3"]
    yt = (b'<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom"><title>T</title>'
          b'<entry><id>a</id><title>ohne videoId</title><link href="https://example.org/"/></entry>'
          b'<entry><id>b</id><yt:videoId>zu kurz</yt:videoId><title>x</title></entry></feed>')
    assert bot._social_feed_parsen(yt, "youtube")[1] == []


def test_atom_nimmt_den_alternate_link_und_rdf_wird_gelesen():
    atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><title>T</title><entry><id>e1</id><title>Eins</title>'
            b'<link rel="self" href="https://example.org/self"/><link rel="alternate" href="https://example.org/a"/>'
            b'<author><name>Ich</name></author></entry></feed>')
    e = bot._social_feed_parsen(atom, "x")[1][0]
    assert e["link"] == "https://example.org/a" and e["autor"] == "Ich"
    rdf = (b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">'
           b'<channel><title>RDF</title></channel><item><title>Eins</title><link>https://example.org/1</link><dc:creator>Dora</dc:creator></item></rdf:RDF>')
    t, es = bot._social_feed_parsen(rdf, "x")
    assert t == "RDF" and es[0]["id"] == "https://example.org/1" and es[0]["autor"] == "Dora"


def test_html_und_steuerzeichen_im_titel_werden_entfernt():
    rss = _rss([("1", "&lt;b&gt;Fett&lt;/b&gt; &amp;amp; \t Zeile\nzwei", "https://example.org/1")])
    # Der Titel steht als Text mit maskierten Tags im Feed; nach dem Lesen ist er einzeiliger Klartext
    assert bot._social_feed_parsen(rss, "x")[1][0]["titel"] == "Fett & Zeile zwei"
    assert bot._social_text("a\x01b\x7fc\n d\u200bx", 50) == "a b c d\u200bx".replace("\u200b", "\u200b")    # Steuerzeichen werden zu Leerzeichen
    lang = bot._social_text("a" * 500, 200)
    assert len(lang) == 200 and lang.endswith("…")


def test_hoechstens_30_eintraege():
    es = bot._social_feed_parsen(_rss([(str(i), f"T{i}", f"https://example.org/{i}") for i in range(80)]), "x")[1]
    assert len(es) == 30


# ═══ Kanal-ID und Adresse ═══════════════════════════════════════════════════
@pytest.mark.parametrize("text", [KANAL, f" {KANAL} ", f"https://www.youtube.com/channel/{KANAL}", f"https://youtube.com/channel/{KANAL}/",
                                  f"https://www.youtube.com/feeds/videos.xml?channel_id={KANAL}",
                                  f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={KANAL}", f"http://m.youtube.com/channel/{KANAL}"])
def test_kanal_id_wird_erkannt(text):
    assert bot._social_youtube_kanal_id(text) == KANAL


@pytest.mark.parametrize("text", ["", "@YouTube", "https://www.youtube.com/@YouTube", "https://www.youtube.com/c/YouTube",
                                  "https://www.youtube.com/user/YouTube", f"https://evil.example/channel/{KANAL}",
                                  f"https://www.youtube.com.evil.example/channel/{KANAL}", "UCBR8-60-B28hp2BmDPdnt",
                                  f"{KANAL}x", f"https://www.youtube.com/feeds/videos.xml?channel_id={KANAL}&channel_id={KANAL}",
                                  "https://www.youtube.com/feeds/videos.xml?channel_id=nope", f"ftp://www.youtube.com/channel/{KANAL}", None])
def test_handle_und_fremde_adressen_werden_nicht_als_kanal_id_genommen(text):
    assert bot._social_youtube_kanal_id(text) is None


@pytest.mark.parametrize("url,erwartet", [
    ("https://rss.example.com/instagram/user/nasa", "https://rss.example.com/instagram/user/nasa"),
    ("  HTTPS://RSS.Example.COM/feed.xml?key=abc  ", "https://rss.example.com/feed.xml?key=abc"),
    ("https://rss.example.com", "https://rss.example.com/"),
    ("https://rss.example.com:443/x", "https://rss.example.com/x"),
])
def test_feed_adresse_wird_normalisiert(url, erwartet):
    assert bot._social_url_pruefen(url) == erwartet


@pytest.mark.parametrize("url", ["", "   ", "http://rss.example.com/feed", "ftp://rss.example.com/feed", "//rss.example.com/feed",
                                 "https://user:pw@rss.example.com/feed", "https://rss.example.com@evil.example/feed",
                                 "https://127.0.0.1/feed", "https://[::1]/feed", "https://10.0.0.5/feed", "https://localhost/feed",
                                 "https://intern/feed", "https://rss.example.com:8443/feed", "https://rss.example.com:abc/feed",
                                 "https://rss.example.com/a b", "https://rss.exämple.com/feed", "https://rss.example.com/" + "a" * 600,
                                 "https://169.254.169.254/latest/meta-data", "javascript:alert(1)", "file:///etc/passwd"])
def test_unerlaubte_feed_adressen_werden_abgewiesen(url):
    with pytest.raises(ValueError):
        bot._social_url_pruefen(url)


def test_feed_url_je_plattform_und_ungueltige_quelle():
    assert bot._social_feed_url({"platform": "youtube", "source": KANAL}) == f"https://www.youtube.com/feeds/videos.xml?channel_id={KANAL}"
    assert bot._social_feed_url({"platform": "youtube", "source": "@handle"}) == ""
    assert bot._social_feed_url({"platform": "x", "source": "https://rss.example.com/x"}) == "https://rss.example.com/x"
    assert bot._social_feed_url({"platform": "x", "source": "http://127.0.0.1/x"}) == ""


def test_anzeige_der_quelle_ohne_parameter_der_adresse():
    abo = {"platform": "instagram", "source": "https://rss.example.com/instagram/user/nasa?key=GEHEIM123&x=1"}
    assert bot._social_quelle_anzeige(abo) == "rss.example.com/instagram/user/nasa?…"
    assert "GEHEIM" not in bot._social_quelle_anzeige(abo)
    assert bot._social_quelle_anzeige({"platform": "youtube", "source": KANAL}) == KANAL
    lang = {"platform": "x", "source": "https://rss.example.com/" + "p" * 80}
    assert bot._social_quelle_anzeige(lang).endswith("…") and len(bot._social_quelle_anzeige(lang)) < 70


# ═══ Abruf: Schutz vor internen Adressen, Weiterleitungen, Fehlercodes ═══════
def test_abruf_verbindet_sich_nie_mit_einem_internen_ziel():
    """Echter Lauf ohne Ersatz: Ein Name, der auf 127.0.0.1 zeigt (localhost), wird vom Resolver abgewiesen - der lokale Server
    sieht keine einzige Verbindung."""
    async def lauf():
        verbindungen = []

        async def annehmen(r, w):
            verbindungen.append(1)
            w.close()
        server = await asyncio.start_server(annehmen, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(bot.SocialFehler) as ex:
                await bot._social_abrufen(f"https://localhost:{port}/feed.xml")
        finally:
            server.close()
            await server.wait_closed()
        return ex.value.art, len(verbindungen)
    assert asyncio.run(lauf()) == ("nicht_erreichbar", 0)


class _Antwort:
    def __init__(self, status=200, headers=None, teile=(b"<rss/>",)):
        self.status, self.headers, self._teile = status, headers or {}, teile
        self.content = self

    async def iter_chunked(self, _n):
        for t in self._teile:
            yield t

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Sitzung:
    """Ersetzt aiohttp.ClientSession: liefert nacheinander die vorbereiteten Antworten bzw. wirft sie."""
    aufrufe = []
    antworten = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, headers=None, allow_redirects=True):
        _Sitzung.aufrufe.append({"url": url, "headers": headers, "redirects": allow_redirects})
        a = _Sitzung.antworten.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


@pytest.fixture
def netz(monkeypatch):
    _Sitzung.aufrufe, _Sitzung.antworten = [], []
    monkeypatch.setattr(bot.aiohttp, "ClientSession", _Sitzung)
    monkeypatch.setattr(bot.aiohttp, "TCPConnector", lambda *a, **kw: object())
    return _Sitzung


def _abrufen(url="https://rss.example.com/feed.xml"):
    return asyncio.run(bot._social_abrufen(url))


def test_abruf_ok_mit_ehrlichem_user_agent_ohne_automatische_weiterleitung(netz):
    netz.antworten = [_Antwort(200, teile=(b"<rss", b"/>"))]
    assert _abrufen() == b"<rss/>"
    anfrage = netz.aufrufe[0]
    assert anfrage["redirects"] is False and anfrage["headers"]["User-Agent"] == "DayZDashboardBot/1.0"
    assert set(anfrage["headers"]) == {"User-Agent", "Accept"}                 # keine Cookies, keine Browser-Kennung


def test_abruf_folgt_pruefbaren_weiterleitungen(netz):
    netz.antworten = [_Antwort(301, {"Location": "/neu.xml"}), _Antwort(302, {"Location": "https://cdn.example.net/f.xml"}), _Antwort(200)]
    assert _abrufen() == b"<rss/>"
    assert [a["url"] for a in netz.aufrufe] == ["https://rss.example.com/feed.xml", "https://rss.example.com/neu.xml", "https://cdn.example.net/f.xml"]


@pytest.mark.parametrize("ziel", ["http://rss.example.com/feed.xml", "https://127.0.0.1/feed.xml", "https://consent.youtube.com/m?continue=x",
                                  "https://www.google.com/sorry/index?continue=x", "https://rss.example.com:8443/x", "javascript:alert(1)"])
def test_weiterleitung_auf_unerlaubtes_ziel_oder_schutzseite_ist_blockiert(netz, ziel):
    netz.antworten = [_Antwort(302, {"Location": ziel})]
    with pytest.raises(bot.SocialFehler) as ex:
        _abrufen()
    assert ex.value.art == "blockiert" and len(netz.aufrufe) == 1                   # nichts weiter angefragt


def test_zu_viele_weiterleitungen(netz):
    netz.antworten = [_Antwort(302, {"Location": f"https://rss.example.com/{i}"}) for i in range(10)]
    with pytest.raises(bot.SocialFehler) as ex:
        _abrufen()
    assert ex.value.art == "blockiert" and len(netz.aufrufe) == 4                   # 1 + 3 Weiterleitungen


@pytest.mark.parametrize("status,art", [(401, "blockiert"), (403, "blockiert"), (429, "blockiert"), (404, "nicht_gefunden"),
                                        (410, "nicht_gefunden"), (500, "serverfehler"), (503, "serverfehler"), (204, "serverfehler")])
def test_status_wird_zu_fehlercode(netz, status, art):
    netz.antworten = [_Antwort(status)]
    with pytest.raises(bot.SocialFehler) as ex:
        _abrufen()
    assert ex.value.art == art


def test_retry_after_verlaengert_die_wartezeit(netz):
    netz.antworten = [_Antwort(429, {"Retry-After": "7200"})]
    with pytest.raises(bot.SocialFehler) as ex:
        _abrufen()
    assert ex.value.warten == 7200
    netz.antworten = [_Antwort(429, {"Retry-After": "999999999"})]
    with pytest.raises(bot.SocialFehler) as ex:
        _abrufen()
    assert ex.value.warten == 86400
    netz.antworten = [_Antwort(503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})]
    with pytest.raises(bot.SocialFehler) as ex:
        _abrufen()
    assert ex.value.warten == 0


def test_zu_grosse_antwort_wird_abgebrochen(netz):
    netz.antworten = [_Antwort(200, teile=(b"x" * 1_000_000, b"x" * 600_000))]
    with pytest.raises(bot.SocialFehler) as ex:
        _abrufen()
    assert ex.value.art == "zu_gross"


@pytest.mark.parametrize("fehler", [asyncio.TimeoutError(), OSError("kaputt"), bot.aiohttp.ClientError("x"), UnicodeError("u")])
def test_netzfehler_werden_zu_nicht_erreichbar(netz, fehler):
    netz.antworten = [fehler]
    with pytest.raises(bot.SocialFehler) as ex:
        _abrufen()
    assert ex.value.art == "nicht_erreichbar"


def test_abrufe_derselben_adresse_werden_geteilt(welt):
    welt.antworten["*"] = _atom([(_vid(1), "Eins")])
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + KANAL
    erste = asyncio.run(bot._social_feed_holen(url, "youtube", 1000.0))
    zweite = asyncio.run(bot._social_feed_holen(url, "youtube", 1500.0))               # innerhalb von 12 Minuten: aus dem Cache
    assert erste is zweite and len(welt.abrufe) == 1
    asyncio.run(bot._social_feed_holen(url, "youtube", 1000.0 + 721))                  # danach wieder ein echter Abruf
    assert len(welt.abrufe) == 2
    welt.antworten["*"] = bot.SocialFehler("blockiert", 60)
    c = asyncio.run(bot._social_feed_holen("https://rss.example.com/f", "x", 5000.0))
    assert c["fehler"] == "blockiert" and c["warten"] == 60 and c["eintraege"] == []
    asyncio.run(bot._social_feed_holen("https://rss.example.com/f", "x", 5100.0))
    assert len(welt.abrufe) == 3                                                       # auch der Fehler wird geteilt


def test_cache_bleibt_begrenzt(welt, monkeypatch):
    monkeypatch.setattr(bot, "_SOCIAL_CACHE_MAX", 5)
    welt.antworten["*"] = _rss([])
    for i in range(12):
        asyncio.run(bot._social_feed_holen(f"https://rss.example.com/{i}", "x", 100.0 + i))
    assert len(bot._SOCIAL_CACHE) == 5 and "https://rss.example.com/11" in bot._SOCIAL_CACHE


# ═══ Vorlage und Nachricht ══════════════════════════════════════════════════
@pytest.mark.parametrize("text", ["", "   ", "{link}", "Neu: {titel} von {autor} auf {plattform}: {link}", "Klammern { einzeln } ohne Name: {link} (ok)".replace("{ einzeln }", "(einzeln)")])
def test_gueltige_vorlagen(text):
    assert bot._social_vorlage_pruefen(text) is None


@pytest.mark.parametrize("text", ["nur Text", "{titel} ohne Link", "{link} {unbekannt}", "{link} {TITEL}", "{link} {__class__}", "{link} {0}", "{link} { titel }", "x" * 501])
def test_ungueltige_vorlagen(text):
    assert bot._social_vorlage_pruefen(text)


@pytest.mark.parametrize("plattform", ["youtube", "instagram", "tiktok", "x"])
@pytest.mark.parametrize("sprache", ["de", "en"])
def test_standardvorlagen_sind_gueltig_und_ersetzen_alles(plattform, sprache):
    vorlage = bot._SOCIAL_VORLAGEN[plattform][sprache]
    assert bot._social_vorlage_pruefen(vorlage) is None
    text = bot._social_text_bauen({"platform": plattform, "language": sprache, "name": "N"},
                                  {"titel": "Mein Titel", "autor": "Dora", "link": "https://example.org/1"})
    assert "{" not in text and "Mein Titel" in text and "Dora" in text and text.endswith("https://example.org/1")


def test_nachricht_ist_gegen_markdown_und_erwaehnungen_aus_fremden_feeds_gesichert():
    abo = {"platform": "x", "language": "de", "name": "Mein Abo", "template": "", "role_id": None}
    text = bot._social_text_bauen(abo, {"titel": "@everyone @here <@123> **fett** {link}", "autor": "", "link": "https://example.org/1"})
    assert "@everyone" not in text and "@here" not in text and "<@123>" not in text      # Erwaehnungen entschaerft
    assert "\\*\\*fett\\*\\*" in text                                                    # Markdown maskiert
    assert text.count("https://example.org/1") == 1 and "Mein Abo" in text               # '{link}' im Titel bleibt Text; Autor-Ersatz = Abo-Name


def test_rollenerwaehnung_steht_vorn_und_nachricht_bleibt_unter_dem_limit():
    abo = {"platform": "youtube", "language": "de", "name": "N", "template": ("{titel} " * 60).strip() + " {link}", "role_id": "501"}
    text = bot._social_text_bauen(abo, {"titel": "T" * 300, "autor": "A" * 300, "link": "https://example.org/" + "p" * 400})
    assert text.startswith("<@&501> ") and len(text) <= 1990


# ═══ Dashboard-API ══════════════════════════════════════════════════════════
def test_anlegen_und_lesen(monkeypatch, welt):
    st, j = _anlegen(monkeypatch, welt.a, name="Offizieller Kanal", role_id=str(R1), language="en", template="Neu: {titel} {link}")
    assert st == 200, j
    abo = j["data"]["abos"][0]
    assert abo["platform"] == "youtube" and abo["quelle"] == KANAL and abo["channel_id"] == str(K1) and abo["role_id"] == str(R1)
    assert abo["language"] == "en" and abo["template"] == "Neu: {titel} {link}" and abo["enabled"] is True and abo["name"] == "Offizieller Kanal"
    assert abo["status"]["baseline"] is False and abo["status"]["fehler"] == ""
    st, j = _api(monkeypatch, welt.a, bot.get_discord_mgmt_social, None, "GET")
    assert st == 200 and len(j["data"]["abos"]) == 1 and j["data"]["max_je_plattform"] == 5 and j["data"]["intervall_minuten"] == 15
    assert set(j["data"]["plattformen"]) == {"youtube", "instagram", "tiktok", "x"}
    assert [a["action"] for a in bot._audit_log] == ["Discord-Management aktualisiert"]


def test_standardname_je_plattform(monkeypatch, welt):
    assert _anlegen(monkeypatch, welt.a)[1]["data"]["abos"][0]["name"] == f"YouTube {KANAL[:12]}"
    st, j = _anlegen(monkeypatch, welt.a, platform="tiktok", source="https://rss.example.com/tiktok/user/@tiktok")
    assert st == 200 and j["data"]["abos"][1]["name"] == "TikTok rss.example.com"


def test_feed_adressen_mit_schluessel_verlassen_den_server_nicht(monkeypatch, welt, tmp_path):
    st, j = _anlegen(monkeypatch, welt.a, platform="instagram", source="https://rss.example.com/ig/nasa?key=GEHEIM123")
    assert st == 200
    st, g = _api(monkeypatch, welt.a, bot.get_discord_mgmt_social, None, "GET")
    assert "GEHEIM123" not in json.dumps(j) and "GEHEIM123" not in json.dumps(g)
    assert g["data"]["abos"][0]["quelle"] == "rss.example.com/ig/nasa?…"
    assert _abos(welt.a)[0]["source"].endswith("key=GEHEIM123")                   # auf dem Server bleibt sie vollstaendig
    assert "social_feeds" not in bot.cfg.config                                    # nie nach config.json gespiegelt
    st, j = _api(monkeypatch, welt.a, bot.get_discord_mgmt_social, None, "GET")
    assert "GEHEIM123" not in json.dumps(j)


@pytest.mark.parametrize("aenderung,text", [
    ({"source": "@YouTube"}, "Kanal-ID"), ({"source": "https://www.youtube.com/@YouTube"}, "Kanal-ID"), ({"source": ""}, "Kanal-ID"),
    ({"platform": "nope"}, "Plattform"), ({"language": "fr"}, "Sprache"), ({"template": "ohne link"}, "Vorlage"),
    ({"template": "{link} {x}"}, "Platzhalter"), ({"channel_id": ""}, "Channel"), ({"channel_id": "abc"}, "Ungültige"),
    ({"channel_id": str(K_FREMD)}, "gibt es in dem gewählten Discord-Server nicht"),
    ({"role_id": str(R_FREMD)}, "gibt es in dem gewählten Discord-Server nicht"),
])
def test_ungueltige_eingaben_werden_abgelehnt_und_nichts_gespeichert(monkeypatch, welt, aenderung, text):
    st, j = _anlegen(monkeypatch, welt.a, **aenderung)
    assert st in (400, 409) and text in j["error"], j
    assert _abos(welt.a) == []


@pytest.mark.parametrize("quelle", ["http://rss.example.com/f", "https://127.0.0.1/f", "https://localhost/f", "https://u:p@rss.example.com/f",
                                    "https://rss.example.com:9999/f", "", "kein url"])
def test_feed_plattformen_lehnen_unerlaubte_adressen_ab(monkeypatch, welt, quelle):
    st, j = _anlegen(monkeypatch, welt.a, platform="instagram", source=quelle)
    assert st == 400 and "Feed-Adresse" in j["error"] or "Domainnamen" in j["error"], j
    assert _abos(welt.a) == []


def test_kanal_id_aus_link_und_doppelte_quelle(monkeypatch, welt):
    assert _anlegen(monkeypatch, welt.a, source=f"https://www.youtube.com/channel/{KANAL}")[0] == 200
    assert _abos(welt.a)[0]["source"] == KANAL                                          # als reine ID gespeichert
    st, j = _anlegen(monkeypatch, welt.a, source=KANAL, channel_id=str(K2))
    assert st == 400 and "schon" in j["error"] and len(_abos(welt.a)) == 1
    assert _anlegen(monkeypatch, welt.a, platform="instagram", source="https://rss.example.com/a")[0] == 200   # andere Plattform ist frei


def test_hoechstens_fuenf_quellen_je_plattform(monkeypatch, welt):
    for i in range(5):
        assert _anlegen(monkeypatch, welt.a, platform="x", source=f"https://rss.example.com/x{i}")[0] == 200
    st, j = _anlegen(monkeypatch, welt.a, platform="x", source="https://rss.example.com/x5")
    assert st == 400 and "höchstens 5" in j["error"]
    assert _anlegen(monkeypatch, welt.a, platform="tiktok", source="https://rss.example.com/t")[0] == 200


def test_aendern_behaelt_plattform_und_quelle(monkeypatch, welt):
    abo_id = _anlegen(monkeypatch, welt.a)[1]["data"]["abos"][0]["id"]
    st, j = _api(monkeypatch, welt.a, bot.post_discord_mgmt_social,
                 {"id": abo_id, "platform": "x", "source": "https://evil.example/f", "channel_id": str(K2), "language": "en",
                  "template": "{link}", "name": "Neu", "enabled": True})
    assert st == 200, j
    abo = _abos(welt.a)[0]
    assert abo["platform"] == "youtube" and abo["source"] == KANAL and abo["channel_id"] == str(K2) and abo["name"] == "Neu"
    assert len(_abos(welt.a)) == 1
    assert bot._audit_log[-1]["detail"].startswith("social_youtube ·")                    # protokolliert wird die echte Plattform des Abos
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_social, {"id": "00000000", "channel_id": str(K1)})[0] == 404


def test_wieder_einschalten_beginnt_mit_frischem_ausgangsstand(monkeypatch, welt):
    abo_id = _anlegen(monkeypatch, welt.a)[1]["data"]["abos"][0]["id"]
    st = bot._social_stand(_abos(welt.a)[0])
    st.update({"baseline": True, "seen": ["a", "b"], "last_error": "blockiert", "errors": 3, "next_try": 9e9})
    def speichern(enabled):
        return _api(monkeypatch, welt.a, bot.post_discord_mgmt_social, {"id": abo_id, "channel_id": str(K1), "enabled": enabled})
    assert speichern(True)[0] == 200 and bot._social_stand(_abos(welt.a)[0])["seen"] == ["a", "b"]         # bleibt an: nichts zuruecksetzen
    assert speichern(False)[0] == 200 and _abos(welt.a)[0]["enabled"] is False
    assert speichern(True)[0] == 200
    st = bot._social_stand(_abos(welt.a)[0])
    assert st["baseline"] is False and st["seen"] == [] and st["next_try"] == 0 and st["errors"] == 0 and st["last_error"] == ""


def test_loeschen(monkeypatch, welt):
    abo_id = _anlegen(monkeypatch, welt.a)[1]["data"]["abos"][0]["id"]
    held = _abos(welt.a)[0]
    st, j = _api(monkeypatch, welt.a, bot.delete_discord_mgmt_social, None, "DELETE", match={"id": abo_id})
    assert st == 200 and j["data"]["abos"] == [] and _abos(welt.a) == []
    assert held["enabled"] is False                                                  # ein laufender Abruf postet danach nichts mehr
    assert _api(monkeypatch, welt.a, bot.delete_discord_mgmt_social, None, "DELETE", match={"id": abo_id})[0] == 404


def test_kachel_status_in_der_discord_management_uebersicht(monkeypatch, welt):
    sicht = bot.GuildSicht(welt.a, G1)
    assert all(bot._discord_mgmt_payload(sicht)[f"social_{p}"] == {"enabled": False} for p in ("youtube", "instagram", "tiktok", "x"))
    _anlegen(monkeypatch, welt.a)
    _anlegen(monkeypatch, welt.a, platform="x", source="https://rss.example.com/x", enabled=False)
    d = bot._discord_mgmt_payload(sicht)
    assert d["social_youtube"]["enabled"] is True and d["social_x"]["enabled"] is False and d["social_tiktok"]["enabled"] is False
    assert bot._discord_mgmt_payload(bot.GuildSicht(welt.a, G2))["social_youtube"]["enabled"] is False      # andere Guild: nichts aktiv


# ── Rechte und Mandantentrennung ────────────────────────────────────────────
def test_gast_nur_ansehen_darf_nichts_aendern(monkeypatch, welt):
    welt.a.data["dashboard_perms"] = {"user:900": {"discord_mgmt": ["view"]}}
    abo_id = _anlegen(monkeypatch, welt.a)[1]["data"]["abos"][0]["id"]
    assert _api(monkeypatch, welt.a, bot.get_discord_mgmt_social, None, "GET", gast="900")[0] == 200
    daten = {"platform": "youtube", "source": KANAL, "channel_id": str(K2)}
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_social, daten, gast="900")[0] == 403
    assert _api(monkeypatch, welt.a, bot.delete_discord_mgmt_social, None, "DELETE", gast="900", match={"id": abo_id})[0] == 403
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_social_pruefen, {"platform": "youtube", "source": KANAL}, gast="900")[0] == 403
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_social_test, None, gast="900", match={"id": abo_id})[0] == 403
    assert len(_abos(welt.a)) == 1


def test_gast_ohne_eintrag_sieht_nichts(monkeypatch, welt):
    assert _api(monkeypatch, welt.a, bot.get_discord_mgmt_social, None, "GET", gast="900")[0] == 403


def test_kunde_besitzer_darf_alles_auf_seinem_server(monkeypatch, welt):
    st, j = _api(monkeypatch, welt.a, bot.post_discord_mgmt_social,
                 {"platform": "youtube", "source": KANAL, "channel_id": str(K1)}, admin=False)
    assert st == 200, j


def test_server_sehen_nie_die_abos_eines_anderen(monkeypatch, welt):
    _anlegen(monkeypatch, welt.a)
    st, j = _api(monkeypatch, welt.b, bot.get_discord_mgmt_social, None, "GET")
    assert st == 200 and j["data"]["abos"] == []
    # ein Channel der fremden Guild ist fuer Server 1000 tabu - und umgekehrt
    assert _anlegen(monkeypatch, welt.a, channel_id=str(K_FREMD))[0] == 400
    st, j = _api(monkeypatch, welt.b, bot.post_discord_mgmt_social, {"platform": "youtube", "source": KANAL, "channel_id": str(K1)})
    assert st == 400 and _abos(welt.b, G3) == []
    # dasselbe Abo darf bei Server 2000 unabhaengig existieren
    st, j = _api(monkeypatch, welt.b, bot.post_discord_mgmt_social, {"platform": "youtube", "source": KANAL, "channel_id": str(K_FREMD)})
    assert st == 200 and len(_abos(welt.b, G3)) == 1 and len(_abos(welt.a)) == 1


def test_guilds_desselben_servers_sind_getrennt(monkeypatch, welt):
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_social, {"platform": "youtube", "source": KANAL, "channel_id": str(K1)}, guild=G1)[0] == 200
    st, j = _api(monkeypatch, welt.a, bot.get_discord_mgmt_social, None, "GET", guild=G2)
    assert st == 200 and j["data"]["abos"] == []                                       # Guild 2 sieht die Abos von Guild 1 nicht
    st, j = _api(monkeypatch, welt.a, bot.post_discord_mgmt_social, {"platform": "youtube", "source": KANAL, "channel_id": str(K1)}, guild=G2)
    assert st == 400                                                                     # Channel von Guild 1 in Guild 2: abgelehnt
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_social, {"platform": "youtube", "source": KANAL, "channel_id": str(K3)}, guild=G2)[0] == 200
    assert len(_abos(welt.a, G1)) == 1 and len(_abos(welt.a, G2)) == 1


def test_feed_adressen_werden_nie_nach_config_json_gespiegelt(welt):
    """_conn_store spiegelt Werte des Hauptservers sonst nach config.json - Feed-Adressen koennen Zugangsschluessel enthalten."""
    assert bot.connections.primary() is welt.a
    bot._conn_store(welt.a, "social_feeds", [{"id": "aaaaaaaa", "source": "https://rss.example.com/f?key=GEHEIM123"}])
    assert "social_feeds" not in bot.cfg.config and "GEHEIM123" not in json.dumps(bot.cfg.config)
    bot._conn_store(welt.a, "welcome_message", {"enabled": True})                 # Gegenprobe: normale Werte werden weiterhin gespiegelt
    assert bot.cfg.config.get("welcome_message") == {"enabled": True}


def test_eigentuemerwechsel_loescht_alle_feed_adressen(monkeypatch, welt):
    _api(monkeypatch, welt.a, bot.post_discord_mgmt_social, {"platform": "instagram", "source": "https://rss.example.com/a?key=X", "channel_id": str(K1)}, guild=G1)
    _api(monkeypatch, welt.a, bot.post_discord_mgmt_social, {"platform": "x", "source": "https://rss.example.com/b?key=Y", "channel_id": str(K3)}, guild=G2)
    assert "GEHEIM" not in json.dumps(welt.a.data) and "key=X" in json.dumps(welt.a.data) and "key=Y" in json.dumps(welt.a.data)
    bot._social_feeds_verwerfen(welt.a)
    assert "key=X" not in json.dumps(welt.a.data) and "key=Y" not in json.dumps(welt.a.data)
    assert _abos(welt.a, G1) == [] and _abos(welt.a, G2) == []


# ── "Feed prüfen" und Testnachricht ─────────────────────────────────────────
def test_feed_pruefen_zeigt_die_neuesten_und_veraendert_nichts(monkeypatch, welt):
    welt.antworten["*"] = YT
    st, j = _api(monkeypatch, welt.a, bot.post_discord_mgmt_social_pruefen, {"platform": "youtube", "source": f"https://www.youtube.com/channel/{KANAL}"})
    assert st == 200 and j["data"]["ok"] is True and j["data"]["anzahl"] == 15 and len(j["data"]["eintraege"]) == 3
    assert j["data"]["feed_titel"] == "YouTube" and j["data"]["eintraege"][0]["link"].startswith("https://www.youtube.com/watch?v=")
    assert welt.abrufe == [f"https://www.youtube.com/feeds/videos.xml?channel_id={KANAL}"]
    assert _abos(welt.a) == [] and all(k.sent == [] for k in welt.k.values())


def test_feed_pruefen_meldet_fehler_als_code(monkeypatch, welt):
    welt.antworten["*"] = b"<html><body>Login</body></html>"
    st, j = _api(monkeypatch, welt.a, bot.post_discord_mgmt_social_pruefen, {"platform": "tiktok", "source": "https://rss.example.com/t"})
    assert st == 200 and j["data"] == {"ok": False, "fehler": "kein_feed", "eintraege": []}
    welt.antworten["*"] = bot.SocialFehler("blockiert")
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_social_pruefen, {"platform": "x", "source": "https://rss.example.com/t"})[1]["data"]["fehler"] == "blockiert"
    for falsch in ({"platform": "youtube", "source": "@YouTube"}, {"platform": "x", "source": "http://127.0.0.1/f"}, {"platform": "nope", "source": "x"}):
        assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_social_pruefen, falsch)[0] == 400
    assert len(welt.abrufe) == 2                                                          # unerlaubte Adressen werden gar nicht erst abgerufen


def test_feed_pruefen_gespeichertes_abo_ohne_adresse_im_klartext(monkeypatch, welt):
    welt.antworten["*"] = KUNDEN_RSS
    _anlegen(monkeypatch, welt.a, platform="instagram", source="https://rss.example.com/ig?key=GEHEIM123")
    abo_id = _abos(welt.a)[0]["id"]
    st, j = _api(monkeypatch, welt.a, bot.post_discord_mgmt_social_pruefen, {"platform": "instagram", "id": abo_id})
    assert st == 200 and j["data"]["ok"] is True and "GEHEIM123" not in json.dumps(j)
    assert welt.abrufe == ["https://rss.example.com/ig?key=GEHEIM123"]


def test_testnachricht(monkeypatch, welt):
    welt.antworten["*"] = _atom([(_vid(1), "Neues Video")], "Mein Kanal")
    abo_id = _anlegen(monkeypatch, welt.a, role_id=str(R1))[1]["data"]["abos"][0]["id"]
    st, j = _api(monkeypatch, welt.a, bot.post_discord_mgmt_social_test, None, match={"id": abo_id})
    assert st == 200 and j["data"] == {"ok": True, "fehler": ""}
    gesendet = welt.k[K1].sent[0]
    assert gesendet["content"].startswith("🧪 <@&501> ") and "Neues Video" in gesendet["content"] and f"watch?v={_vid(1)}" in gesendet["content"]
    assert gesendet["allowed"].everyone is False and gesendet["allowed"].users is False and [r.id for r in gesendet["allowed"].roles] == [R1]
    assert bot._social_stand(_abos(welt.a)[0]).get("seen") in (None, []) and not bot._social_stand(_abos(welt.a)[0]).get("baseline")


def test_testnachricht_ohne_feed_nimmt_ein_beispiel_und_meldet_channel_fehler(monkeypatch, welt):
    abo_id = _anlegen(monkeypatch, welt.a)[1]["data"]["abos"][0]["id"]                    # Feed nicht erreichbar (keine Antwort vorbereitet)
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_social_test, None, match={"id": abo_id})[1]["data"]["ok"] is True
    assert "Beispiel-Beitrag" in welt.k[K1].sent[0]["content"]
    _abos(welt.a)[0]["channel_id"] = "424242"                                              # Channel gibt es nicht mehr
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_social_test, None, match={"id": abo_id})[1]["data"] == {"ok": False, "fehler": "kanal_fehlt"}
    assert _api(monkeypatch, welt.a, bot.post_discord_mgmt_social_test, None, match={"id": "00000000"})[0] == 404


# ═══ Der Lauf ═══════════════════════════════════════════════════════════════
def _mit_abo(welt, **kw):
    welt.a.data.setdefault("guild_daten", {})
    abo = {"id": kw.pop("id", "aaaaaaaa"), "created": 0, "state": {}}
    abo.update(_abo(**kw))
    sicht = bot.GuildSicht(welt.a, G1)
    sicht.set("social_feeds", _abos(welt.a) + [abo])
    return abo


URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={KANAL}"


def test_erste_pruefung_merkt_nur_den_ausgangsstand_und_postet_nichts(welt):
    abo = _mit_abo(welt)
    welt.antworten[URL] = _atom([(_vid(3), "C"), (_vid(2), "B"), (_vid(1), "A")])
    assert _lauf(1000.0) == 1
    assert welt.k[K1].sent == [] and abo["state"]["baseline"] is True and abo["state"]["seen"] == [f"yt:video:{_vid(n)}" for n in (3, 2, 1)]
    assert _lauf(1000.0 + 5) == 0                                                          # noch nicht faellig
    _lauf(1000.0 + 2000)
    assert welt.k[K1].sent == []                                                           # unveraendert: nichts neues


def test_neue_beitraege_werden_in_zeitlicher_reihenfolge_gepostet_und_nie_doppelt(welt):
    abo = _mit_abo(welt, rolle=R1)
    welt.antworten[URL] = _atom([(_vid(1), "Alt")])
    _lauf(1000.0)
    welt.antworten[URL] = _atom([(_vid(3), "Dritter"), (_vid(2), "Zweiter"), (_vid(1), "Alt")])
    _lauf(3000.0)
    texte = [m["content"] for m in welt.k[K1].sent]
    assert len(texte) == 2 and "Zweiter" in texte[0] and "Dritter" in texte[1]            # aeltester zuerst
    assert all(t.startswith("<@&501> ") for t in texte) and abo["state"]["last_title"] == "Dritter" and abo["state"]["last_post"] == 3000.0
    _lauf(6000.0)
    assert len(welt.k[K1].sent) == 2                                                       # nichts doppelt
    assert welt.k[K1].sent[0]["allowed"].everyone is False and welt.k[K1].sent[0]["allowed"].users is False


def test_bei_vielen_neuen_beitraegen_nur_die_neuesten_drei(welt):
    abo = _mit_abo(welt)
    welt.antworten[URL] = _atom([(_vid(1), "Alt")])
    _lauf(1000.0)
    welt.antworten[URL] = _atom([(_vid(n), f"V{n}") for n in (6, 5, 4, 3, 2, 1)])
    _lauf(3000.0)
    assert [m["content"].split("**")[3] for m in welt.k[K1].sent] == ["V4", "V5", "V6"]
    assert f"yt:video:{_vid(2)}" in abo["state"]["seen"] and f"yt:video:{_vid(3)}" in abo["state"]["seen"]   # uebersprungene gelten als gesehen
    _lauf(6000.0)
    assert len(welt.k[K1].sent) == 3


def test_kunden_feed_mit_vorlage(welt):
    abo = _mit_abo(welt, plattform="instagram", quelle="https://rss.example.com/ig", template="NEU {autor}/{plattform}: {titel} -> {link}", name="Mein IG")
    welt.antworten["https://rss.example.com/ig"] = _rss([("g1", "Eins", "https://example.org/1")])
    _lauf(1000.0)
    welt.antworten["https://rss.example.com/ig"] = _rss([("g2", "Zwei", "https://example.org/2"), ("g1", "Eins", "https://example.org/1")])
    _lauf(3000.0)
    assert [m["content"] for m in welt.k[K1].sent] == ["NEU Mein IG/Instagram: Zwei -> https://example.org/2"] and abo["state"]["seen"][0] == "g2"


@pytest.mark.parametrize("kanal_fehler,code", [(None, "kanal_fehlt"), ("fremd", "kanal_fremd"), ("forbidden", "kein_zugriff"), ("http", "senden_fehlgeschlagen")])
def test_postfehler_werden_gemeldet_und_beitraege_bleiben_ungesehen(welt, kanal_fehler, code):
    import discord
    abo = _mit_abo(welt)
    welt.antworten[URL] = _atom([(_vid(1), "Alt")])
    _lauf(1000.0)
    if kanal_fehler is None:
        abo["channel_id"] = "424242"                                                       # Channel geloescht
    elif kanal_fehler == "fremd":
        abo["channel_id"] = str(K_FREMD)                                                   # gehoert zu einer fremden Guild
    elif kanal_fehler == "forbidden":
        welt.k[K1]._fehler = discord.Forbidden(type("R", (), {"status": 403, "reason": "x"})(), "x")
    else:
        welt.k[K1]._fehler = discord.HTTPException(type("R", (), {"status": 500, "reason": "x"})(), "x")
    welt.antworten[URL] = _atom([(_vid(2), "Neu"), (_vid(1), "Alt")])
    _lauf(3000.0)
    st = abo["state"]
    assert st["last_error"] == code and f"yt:video:{_vid(2)}" not in st["seen"] and st["next_try"] > 3000.0 + 900
    assert welt.k[K1].sent == [] and welt.k[K_FREMD].sent == []                            # nirgends gepostet - schon gar nicht in der fremden Guild
    # Fehler behoben: beim naechsten Versuch kommt der Beitrag doch noch
    abo["channel_id"] = str(K1)
    welt.k[K1]._fehler = None
    _lauf(3000.0 + 30000)
    assert len(welt.k[K1].sent) == 1 and abo["state"]["last_error"] == "" and f"yt:video:{_vid(2)}" in abo["state"]["seen"]


def test_nach_einem_postfehler_in_der_mitte_gelten_nur_die_gesendeten_als_gesehen(welt):
    abo = _mit_abo(welt)
    welt.antworten[URL] = _atom([(_vid(1), "Alt")])
    _lauf(1000.0)
    kanal = welt.k[K1]
    original = kanal.send
    zaehler = {"n": 0}

    async def zweiter_scheitert(content=None, **kw):
        zaehler["n"] += 1
        if zaehler["n"] == 2:
            import discord
            raise discord.Forbidden(type("R", (), {"status": 403, "reason": "x"})(), "x")
        return await original(content, **kw)
    kanal.send = zweiter_scheitert
    welt.antworten[URL] = _atom([(_vid(3), "Drei"), (_vid(2), "Zwei"), (_vid(1), "Alt")])
    _lauf(3000.0)
    seen = abo["state"]["seen"]
    assert f"yt:video:{_vid(2)}" in seen and f"yt:video:{_vid(3)}" not in seen and abo["state"]["last_error"] == "kein_zugriff"
    kanal.send = original
    _lauf(3000.0 + 30000)
    assert [m["content"].split("**")[3] for m in kanal.sent] == ["Zwei", "Drei"]


def test_feed_fehler_ziehen_wartezeit_hoch_und_heilen_von_selbst(welt):
    abo = _mit_abo(welt)
    welt.antworten[URL] = bot.SocialFehler("blockiert", 7200)
    _lauf(1000.0)
    st = abo["state"]
    assert st["last_error"] == "blockiert" and st["errors"] == 1 and st["next_try"] >= 1000.0 + 7200
    welt.antworten[URL] = bot.SocialFehler("nicht_erreichbar")
    _lauf(st["next_try"] + 1)
    assert st["errors"] == 2 and st["last_error"] == "nicht_erreichbar"
    for i in range(10):                                                                   # Wartezeit waechst, bleibt aber unter 6 Std. + Zufall
        _lauf(st["next_try"] + 1)
    assert st["errors"] == 12 and st["next_try"] - (st["last_check"]) <= 21600 + 120
    welt.antworten[URL] = _atom([(_vid(1), "A")])
    _lauf(st["next_try"] + 1)
    assert st["errors"] == 0 and st["last_error"] == "" and st["baseline"] is True


def test_wartezeit_nach_fehlern_verdoppelt_sich_bis_sechs_stunden():
    st = {}
    erwartet = [900, 1800, 3600, 7200, 14400, 21600, 21600]
    for n, basis in enumerate(erwartet, start=1):
        bot._social_backoff(st, 1000.0)
        assert st["errors"] == n and 1000.0 + basis <= st["next_try"] <= 1000.0 + basis + 120, (n, st["next_try"] - 1000.0)
    bot._social_backoff({}, 1000.0, 50000)                                                  # ein Retry-After der Quelle geht vor
    st2 = {}
    bot._social_backoff(st2, 1000.0, 50000)
    assert st2["next_try"] >= 1000.0 + 50000


def test_eine_seite_statt_eines_feeds_postet_nichts(welt):
    abo = _mit_abo(welt)
    welt.antworten[URL] = _atom([(_vid(1), "A")])
    _lauf(1000.0)
    welt.antworten[URL] = b"<!DOCTYPE html><html><body>Bitte anmelden</body></html>"
    _lauf(3000.0)
    assert welt.k[K1].sent == [] and abo["state"]["last_error"] == "kein_feed" and len(abo["state"]["seen"]) == 1     # Ausgangsstand bleibt


def test_ausgeschaltete_und_nicht_faellige_abos_werden_nicht_abgerufen(welt):
    _mit_abo(welt, enabled=False)
    assert _lauf(1000.0) == 0 and welt.abrufe == []
    abo = _mit_abo(welt, id="bbbbbbbb", quelle="UCaaaaaaaaaaaaaaaaaaaaaa")
    abo["state"]["next_try"] = 5000.0
    assert _lauf(1000.0) == 0 and welt.abrufe == []
    welt.antworten["*"] = _atom([])
    assert _lauf(5000.0) == 1 and len(welt.abrufe) == 1


def test_abonnenten_derselben_adresse_teilen_sich_einen_abruf(welt):
    welt.b.data["kunden_stufe"] = "premium"
    _mit_abo(welt)
    abo2 = {"id": "cccccccc", "created": 0, "state": {}, **_abo(kanal=K_FREMD)}
    bot.GuildSicht(welt.b, G3).set("social_feeds", [abo2])
    welt.antworten[URL] = _atom([(_vid(1), "A")])
    assert _lauf(1000.0) == 2
    assert welt.abrufe == [URL]                                                            # zwei Kunden, ein Abruf


def test_ein_defektes_abo_stoppt_die_anderen_nicht(welt, monkeypatch):
    kaputt = _mit_abo(welt)
    gut = _mit_abo(welt, id="dddddddd", quelle="UCbbbbbbbbbbbbbbbbbbbbbb")
    welt.antworten["*"] = _atom([(_vid(1), "A")])
    echt = bot._social_abo_pruefen

    async def mit_fehler(gid, abo, jetzt):
        if abo is kaputt:
            raise RuntimeError("kaputt")
        return await echt(gid, abo, jetzt)
    monkeypatch.setattr(bot, "_social_abo_pruefen", mit_fehler)
    assert _lauf(1000.0) == 2
    assert gut["state"]["baseline"] is True and kaputt["state"]["errors"] == 1


def test_hoechstens_25_pruefungen_je_lauf(welt, monkeypatch):
    for i in range(30):
        _mit_abo(welt, id=f"{i:08x}", quelle=f"UC{i:022d}")
    welt.antworten["*"] = _atom([])
    assert _lauf(1000.0) == 25
    assert _lauf(1001.0) == 5                                                              # der Rest folgt im naechsten Lauf


def test_wird_waehrend_des_abrufs_ausgeschaltet_wird_nichts_gepostet(welt, monkeypatch):
    abo = _mit_abo(welt)
    welt.antworten[URL] = _atom([(_vid(1), "Alt")])
    _lauf(1000.0)
    echt = bot._social_feed_holen

    async def mit_loeschung(url, plattform, jetzt, *a, **kw):
        c = await echt(url, plattform, jetzt, *a, **kw)
        abo["enabled"] = False                                                             # Kunde loescht das Abo mitten im Abruf
        return c
    monkeypatch.setattr(bot, "_social_feed_holen", mit_loeschung)
    welt.antworten[URL] = _atom([(_vid(2), "Neu"), (_vid(1), "Alt")])
    _lauf(3000.0)
    assert welt.k[K1].sent == []


def test_bot_nicht_bereit_pruefen_nichts(welt, monkeypatch):
    _mit_abo(welt)
    monkeypatch.setattr(bot.bot, "is_ready", lambda: False, raising=False)
    assert _lauf(1000.0) == 0 and welt.abrufe == []


def test_ungueltige_quelle_im_abo_bleibt_ein_fehler_ohne_netzzugriff(welt):
    abo = _mit_abo(welt, quelle="@handle")
    assert _lauf(1000.0) == 1
    assert abo["state"]["last_error"] == "ungueltig_url" and welt.abrufe == [] and abo["state"]["next_try"] > 1000.0 + 20000


def test_zustand_wird_gespeichert_und_ueberlebt_einen_neustart(welt, tmp_path):
    _mit_abo(welt)
    welt.antworten[URL] = _atom([(_vid(1), "A")])
    _lauf(1000.0)
    neu = bot.ConnectionRegistry()
    neu.load()
    abo = bot._social_abos(bot.GuildSicht(neu._conns["1000"], G1))[0]
    assert abo["state"]["baseline"] is True and abo["state"]["seen"] == [f"yt:video:{_vid(1)}"] and abo["source"] == KANAL


def test_status_im_dashboard_zeigt_fehlercodes_aber_nie_rohtext(monkeypatch, welt):
    abo = _mit_abo(welt)
    abo["state"]["last_error"] = "blockiert"
    st, j = _api(monkeypatch, welt.a, bot.get_discord_mgmt_social, None, "GET")
    assert j["data"]["abos"][0]["status"]["fehler"] == "blockiert"
    abo["state"]["last_error"] = "<script>alert(1)</script>"
    st, j = _api(monkeypatch, welt.a, bot.get_discord_mgmt_social, None, "GET")
    assert j["data"]["abos"][0]["status"]["fehler"] == "serverfehler" and "<script>" not in json.dumps(j)
