"""Neuaufbau des Online-Zustands (Hydrierung) und "Post latest log lines now" bei Log-Lesen ueber die Nitrado-API.

Ausgangsbefund (Diagnose-Seite zweier Server, API-Betrieb): "FTP error while rebuilding", "Last successful read: -", "Online
state rebuilt: No". Der Poll wählt bei API-Betrieb den VOLLEN Pfad (/games/<ftp_user>/noftp/dayzps/config/...ADM) - den kennt der
FTP-Zugang nicht, der nur ein eigenes "Zuhause" sieht (/dayzps/config/...). Die Hydrierung und das Diagnose-Nachlesen schickten
genau diesen Pfad trotzdem per FTP los und scheiterten immer. Die Attrappe unten verhält sich wie dieser FTP-Zugang: sie kennt nur
die relativen Pfade. Die API-Attrappe verhält sich wie Nitrado: nur volle Pfade, höchstens 65535 Byte je seek.

    python3 -m pytest tests/test_hydrate_api.py -q
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
from log_parser import DayZLogParser  # noqa: E402

LOGDIR_REL = "/dayzps/config"
LOGDIR_API = "/games/ni1_3/noftp/dayzps/config"
NAME = "DayZServer_PS4_x64_2026-10-31_12-02-28.ADM"
REL, VOLL = f"{LOGDIR_REL}/{NAME}", f"{LOGDIR_API}/{NAME}"

KOPF = "AdminLog started on 2026-10-31 at 12:02:30\n"
BLOCK = ('12:20:00 | ##### PlayerList log: 1 players\n'
         '12:20:00 | Player "Tester" (id=abc123= pos=<1937.3, 15055.6, 445.6>)\n'
         '12:20:00 | #####\n')


def _fuellzeilen(n):
    return "".join(f"12:10:{i % 60:02d} | Fuellzeile Nummer {i} ohne Bedeutung fuer den Neuaufbau\n" for i in range(n))


class FtpStub:
    """Wie der echte FTP-Zugang: nur Pfade ab /dayzps/... existieren, der volle API-Pfad ergibt 550 (None)."""
    def __init__(self, dateien):
        self.dateien = dict(dateien)
        self.aufrufe = []

    def list_adm_files(self, d):
        self.aufrufe.append(("NLST", d))
        return sorted(p for p in self.dateien if p.rsplit("/", 1)[0] == d)

    def file_size_or_none(self, p):
        self.aufrufe.append(("SIZE", p))
        return len(self.dateien[p].encode()) if p in self.dateien else None

    def read_from_offset(self, p, off):
        self.aufrufe.append(("RETR", p, off))
        if p not in self.dateien:
            return None, off
        roh = self.dateien[p].encode()[off:]
        return roh.decode(), off + len(roh)


class ApiStub:
    """Wie Nitrado: list/seek verstehen nur volle Pfade, ein seek liefert hoechstens 65535 Byte."""
    consecutive_failures = 0

    def __init__(self, dateien):
        self.dateien = dict(dateien)
        self.seeks = []
        self.fehler_bei_aufruf = None          # Nummer des seek, der scheitern soll (1 = erster)

    async def list_files(self, d):
        return [{"name": p.rsplit("/", 1)[-1], "path": p, "size": len(t.encode()), "modified_at": i}
                for i, (p, t) in enumerate(sorted(self.dateien.items())) if p.rsplit("/", 1)[0] == d]

    async def seek_file(self, p, off, length=65535):
        self.seeks.append((p, off))
        if p not in self.dateien or self.fehler_bei_aufruf == len(self.seeks):
            return None
        return self.dateien[p].encode()[off:off + min(length, 65535)]


def _conn(monkeypatch, tmp_path, via_api, text):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "connections.json").write_text(json.dumps({"1000": {
        "service_id": "1000", "nitrado_token": "x", "owner_discord_id": "1", "map_name": "nasdara", "ftp_user": "ni1_3",
        "ftp_log_dir": LOGDIR_REL, "log_lesen_via_api": via_api}}))
    reg = bot.ConnectionRegistry()
    reg.load()
    monkeypatch.setattr(bot, "connections", reg)
    monkeypatch.setattr(bot.cfg, "config", {"service_id": "1000"})
    c = reg._conns["1000"]
    c.ftp, c.api, c.parser = FtpStub({REL: text}), ApiStub({VOLL: text}), DayZLogParser()
    return c


def _run(coro):
    return asyncio.run(coro)


def _hydrieren(c):
    async def lauf():
        adm = await bot._log_dateien(c, LOGDIR_REL, ".adm")
        return adm[-1], await bot.bot._hydrate_online_state(c, adm[-1], asyncio.get_running_loop())
    return _run(lauf())


# ── Der gemeldete Fehler ───────────────────────────────────────────────────
def test_api_betrieb_baut_den_online_zustand_auf_obwohl_der_ftp_nur_relative_pfade_kennt(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, True, KOPF + BLOCK)
    latest, ok = _hydrieren(c)
    assert latest == VOLL                                                  # der Poll waehlt den vollen Pfad ...
    assert ok is True and sorted(c.parser.player_positions) == ["Tester"]  # ... und die Hydrierung kommt damit klar
    assert c.parser_hydrated and c.hydrated_file == VOLL and c.roster_datei == VOLL
    assert c.last_read_status == "Online-Zustand aus PlayerList-Block aufgebaut"
    assert c.last_read_erfolg_ts > 0 and c.remote_size == len((KOPF + BLOCK).encode())
    assert c.ftp.aufrufe == []                                             # kein FTP-Zugriff mit dem falschen Pfad


def test_ftp_betrieb_funktioniert_unveraendert(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, False, KOPF + BLOCK)
    latest, ok = _hydrieren(c)
    assert latest == REL and ok is True and sorted(c.parser.player_positions) == ["Tester"]
    assert [a[0] for a in c.ftp.aufrufe] == ["NLST", "SIZE", "RETR"] and c.api.seeks == []


def test_ohne_den_fix_waere_es_gescheitert_der_alte_weg_ist_nachgestellt(monkeypatch, tmp_path):
    """Gegenprobe: der alte Weg (FTP-Zugriff mit dem API-Pfad) scheitert in dieser Attrappe tatsaechlich."""
    c = _conn(monkeypatch, tmp_path, True, KOPF + BLOCK)
    assert c.ftp.file_size_or_none(VOLL) is None
    assert c.ftp.read_from_offset(VOLL, 0) == (None, 0)


# ── API-Eigenheiten: Stuecke von hoechstens 65535 Byte ────────────────────────
def test_block_am_ende_einer_grossen_datei_wird_ueber_mehrere_seek_aufrufe_erreicht(monkeypatch, tmp_path):
    text = KOPF + _fuellzeilen(3000) + BLOCK                                # rund 200 KB, kleiner als die 512 KB-Grenze
    assert 150_000 < len(text.encode()) < 512 * 1024
    c = _conn(monkeypatch, tmp_path, True, text)
    _latest, ok = _hydrieren(c)
    assert ok is True and sorted(c.parser.player_positions) == ["Tester"]
    erwartet = -(-len(text.encode()) // 65535)                              # aufgerundet: genau so viele Stuecke, kein einziges mehr
    assert len(c.api.seeks) == erwartet and c.api.seeks[0] == (VOLL, 0)


def test_sehr_grosse_datei_liest_nur_das_ende_und_verwirft_die_angeschnittene_zeile(monkeypatch, tmp_path):
    text = KOPF + _fuellzeilen(9000) + BLOCK                                # rund 600 KB: nur das letzte Fenster (128 KB) wird gelesen
    assert len(text.encode()) > 512 * 1024
    c = _conn(monkeypatch, tmp_path, True, text)
    _latest, ok = _hydrieren(c)
    size = len(text.encode())
    assert ok is True and sorted(c.parser.player_positions) == ["Tester"]
    assert c.api.seeks[0][1] == size - 128 * 1024                           # ab dem Fenster, nicht ab 0
    assert len(c.api.seeks) <= 3


def test_fehler_in_einem_spaeteren_stueck_gilt_als_fehler_nicht_als_erfolg(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, True, KOPF + _fuellzeilen(3000) + BLOCK)
    c.api.fehler_bei_aufruf = 2
    _latest, ok = _hydrieren(c)
    assert ok is False and c.parser_hydrated is False and c.parser.player_positions == {}
    assert c.last_read_status == "FTP-Fehler beim Hydrieren"               # bleibt: die Oberflaeche uebersetzt genau diesen Text
    assert c.last_read_erfolg_ts == 0 and c.last_read_versuch_ts > 0


def test_fehler_beim_ersten_stueck(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, True, KOPF + BLOCK)
    c.api.fehler_bei_aufruf = 1
    _latest, ok = _hydrieren(c)
    assert ok is False and c.parser_hydrated is False


def test_unbekannte_groesse_liest_die_datei_trotzdem(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, True, KOPF + BLOCK)

    async def keine_groesse(_conn, _p):
        return None
    monkeypatch.setattr(bot, "_log_dateigroesse", keine_groesse)
    _latest, ok = _hydrieren(c)
    assert ok is True and sorted(c.parser.player_positions) == ["Tester"]


def test_ohne_playerlist_block_wird_nichts_erfunden(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, True, KOPF)
    _latest, ok = _hydrieren(c)
    assert ok is False and c.parser.player_positions == {}
    assert c.last_read_status == "kein vollständiger PlayerList-Block im Log gefunden" and c.last_read_erfolg_ts > 0


# ── _log_lesen_bereich ─────────────────────────────────────────────────────
def test_bereich_ftp_ein_aufruf_api_stuecke_bis_zum_ende(monkeypatch, tmp_path):
    text = _fuellzeilen(4000)                                              # rund 260 KB
    c = _conn(monkeypatch, tmp_path, False, text)
    inhalt, pos = _run(bot._log_lesen_bereich(c, REL, 0, len(text.encode())))
    assert inhalt == text and pos == len(text.encode()) and [a[0] for a in c.ftp.aufrufe] == ["RETR"]
    c = _conn(monkeypatch, tmp_path, False, text)                          # FTP, Ende unbekannt: trotzdem nur EIN Aufruf (kein zweiter am Dateiende)
    inhalt, pos = _run(bot._log_lesen_bereich(c, REL, 0))
    assert inhalt == text and [a[0] for a in c.ftp.aufrufe] == ["RETR"]
    c = _conn(monkeypatch, tmp_path, True, text)
    inhalt, pos = _run(bot._log_lesen_bereich(c, VOLL, 0, len(text.encode())))
    assert inhalt == text and pos == len(text.encode()) and len(c.api.seeks) == -(-len(text.encode()) // 65535)


def test_bereich_ab_der_mitte_und_ohne_bekanntes_ende(monkeypatch, tmp_path):
    text = _fuellzeilen(4000)
    c = _conn(monkeypatch, tmp_path, True, text)
    inhalt, pos = _run(bot._log_lesen_bereich(c, VOLL, 100_000))
    assert pos == len(text.encode()) and text.encode().endswith(inhalt.encode()) and len(inhalt) < len(text)


def test_bereich_bricht_nach_max_aufrufen_ab(monkeypatch, tmp_path):
    text = _fuellzeilen(8000)                                              # mehr als 5 Stuecke
    c = _conn(monkeypatch, tmp_path, True, text)
    inhalt, pos = _run(bot._log_lesen_bereich(c, VOLL, 0, len(text.encode()), max_aufrufe=3))
    assert len(c.api.seeks) == 3 and pos < len(text.encode()) and inhalt is not None


def test_bereich_leere_datei_und_ende_schon_erreicht(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, True, "")
    assert _run(bot._log_lesen_bereich(c, VOLL, 0, 0)) == ("", 0) and c.api.seeks == []


# ── Neuaufbau-Anstoss durch die Auto-Aufgabe ─────────────────────────────────
def test_rehydrate_nutzt_im_api_betrieb_die_api_liste(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, True, KOPF + BLOCK)

    def verboten(_d):
        raise AssertionError("im API-Betrieb darf die ADM-Liste nicht per FTP geholt werden")
    c.ftp.list_adm_files = verboten
    ok = _run(bot.bot._rehydrate_versuch(c))
    assert ok is True and sorted(c.parser.player_positions) == ["Tester"] and c.hydrated_file == VOLL


def test_rehydrate_im_ftp_betrieb(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, False, KOPF + BLOCK)
    assert _run(bot.bot._rehydrate_versuch(c)) is True and c.hydrated_file == REL


# ── "Post latest log lines now" (Diagnose) ───────────────────────────────────
def _diagnose(monkeypatch, c):
    c.log_state["current"] = {"file": VOLL if bot._log_lesen_via_api(c) else REL, "offset": 0}
    bot.connections.add_guild(c.service_id, 111)
    sid = str(time.time_ns())
    bot._SESS_STORE[sid] = {"token": "x", "discord": {"id": sid}, "is_admin": True, "service_id": c.service_id, "seen": time.time()}
    monkeypatch.setattr(bot, "_SESS_STORE", bot._SESS_STORE)
    req = make_mocked_request("POST", "/api/diagnose/nachlesen", headers={"Cookie": f"{bot._SESS_COOKIE}={sid}"})

    async def kein_dispatch(*_a, **_k):
        return None
    monkeypatch.setattr(bot.bot, "_dispatch", kein_dispatch)
    r = _run(bot.api_diagnose_nachlesen(req))
    return r.status, json.loads(r.body)


@pytest.mark.parametrize("via_api", [True, False])
def test_diagnose_nachlesen_funktioniert_in_beiden_betriebsarten(monkeypatch, tmp_path, via_api):
    text = KOPF + _fuellzeilen(1200) + BLOCK
    c = _conn(monkeypatch, tmp_path, via_api, text)
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    status, j = _diagnose(monkeypatch, c)
    assert status == 200, j
    d = j["data"]
    assert d["datei"] == (VOLL if via_api else REL) and 0 < d["zeilen_gelesen"] <= bot.DIAGNOSE_NACHLESEN_ZEILEN
    if via_api:
        assert c.ftp.aufrufe == []                                         # nichts per FTP mit dem API-Pfad versucht


def test_diagnose_nachlesen_liest_bei_groesserem_fenster_wirklich_bis_zum_dateiende(monkeypatch, tmp_path):
    """Die API liefert hoechstens 65535 Byte je Aufruf: bei einem groesseren Fenster muessen die Stuecke aneinandergehaengt werden."""
    monkeypatch.setattr(bot, "DIAGNOSE_NACHLESEN_BYTES", 150_000)
    c = _conn(monkeypatch, tmp_path, True, KOPF + _fuellzeilen(3000) + BLOCK)
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    gesehen = []
    echt = bot._historisch_parsen

    def merken(conn, text):
        gesehen.append(text)
        return echt(conn, text)
    monkeypatch.setattr(bot, "_historisch_parsen", merken)
    status, j = _diagnose(monkeypatch, c)
    assert status == 200 and gesehen and gesehen[0].splitlines()[-1] == "12:20:00 | #####"


def test_diagnose_nachlesen_meldet_einen_echten_lesefehler(monkeypatch, tmp_path):
    c = _conn(monkeypatch, tmp_path, True, KOPF + BLOCK)
    c.api.fehler_bei_aufruf = 1
    monkeypatch.setattr(bot, "_SESS_STORE", {})
    status, j = _diagnose(monkeypatch, c)
    assert status == 502 and "nicht lesbar" in j["error"]
