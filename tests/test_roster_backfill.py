"""Tests fuer den Ruecklese-Abgleich alter ADM-Dateien in die Spieler-Liste
(player_roster) beim (Neu-)Start.

Brigarde: der Bot soll beim Einrichten/Neustart in die letzten ADM-Logs
schauen und unbekannte Spieler automatisch zur Liste hinzufuegen - bei einer
frisch eingerichteten Guild (noch keine Spieler-Liste) die letzten 5 Dateien,
bei einer bestehenden die letzten 3. Nur EINMAL je Prozesslauf, nicht bei
jedem Poll-Zyklus.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/test_roster_backfill.py -v
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


@pytest.fixture
def edb(tmp_path):
    return bot.EconomyDB(str(tmp_path / "economy_test.db"))


def _run(coro):
    return asyncio.run(coro)


class _StubFTP:
    def __init__(self, inhalte):
        self.inhalte = inhalte  # {(pfad, offset): text}

    def read_from_offset(self, pfad, offset):
        text = self.inhalte.get((pfad, offset), "")
        return text, offset + len(text.encode("utf-8"))


_zaehler = [0]


@pytest.fixture
def conn(monkeypatch, edb):
    monkeypatch.setattr(bot, "db", edb)
    _zaehler[0] += 1
    service_id = f"roster-backfill-{_zaehler[0]}"
    bot.connections.upsert(service_id)
    bot.connections.add_guild(service_id, 5000 + _zaehler[0])
    return bot.connections.for_service(service_id)


def _connect_zeile(name, id_="AAAA="):
    return f'01:00:00 | Player "{name}" (id={id_}) is connected'


def test_frisch_eingerichtet_durchsucht_fuenf_dateien(conn):
    dateien = [f"/logs/datei{i}.ADM" for i in range(7)]
    inhalte = {}
    for i, pfad in enumerate(dateien):
        inhalte[(pfad, 0)] = _connect_zeile(f"Spieler{i}") + "\n"
    conn.ftp = _StubFTP(inhalte)
    _run(bot.bot._roster_backfill_falls_noetig(conn, dateien))
    gid = conn.guild_ids[0]
    # nur die letzten 5 Dateien (Spieler2..Spieler6) durchsucht
    for i in range(2, 7):
        assert bot.db.roster_hat_namen(gid, conn.service_id, f"Spieler{i}")
    for i in range(0, 2):
        assert not bot.db.roster_hat_namen(gid, conn.service_id, f"Spieler{i}")


def test_bestehende_liste_durchsucht_nur_drei_dateien(conn):
    gid = conn.guild_ids[0]
    bot.db.roster_upsert_login(gid, conn.service_id, "SchonBekannt")
    dateien = [f"/logs/datei{i}.ADM" for i in range(5)]
    inhalte = {}
    for i, pfad in enumerate(dateien):
        inhalte[(pfad, 0)] = _connect_zeile(f"Spieler{i}") + "\n"
    conn.ftp = _StubFTP(inhalte)
    _run(bot.bot._roster_backfill_falls_noetig(conn, dateien))
    for i in range(2, 5):
        assert bot.db.roster_hat_namen(gid, conn.service_id, f"Spieler{i}")
    for i in range(0, 2):
        assert not bot.db.roster_hat_namen(gid, conn.service_id, f"Spieler{i}")


def test_laeuft_nur_einmal_je_prozesslauf(conn):
    dateien = ["/logs/datei0.ADM"]
    conn.ftp = _StubFTP({("/logs/datei0.ADM", 0): _connect_zeile("Erst") + "\n"})
    _run(bot.bot._roster_backfill_falls_noetig(conn, dateien))
    assert conn.roster_backfill_done
    # zweiter Aufruf mit neuem Inhalt darf NICHTS mehr aendern
    conn.ftp = _StubFTP({("/logs/datei0.ADM", 0): _connect_zeile("Zweiter") + "\n"})
    _run(bot.bot._roster_backfill_falls_noetig(conn, dateien))
    gid = conn.guild_ids[0]
    assert not bot.db.roster_hat_namen(gid, conn.service_id, "Zweiter")


def test_bereits_bekannter_name_wird_nicht_ueberschrieben(conn):
    gid = conn.guild_ids[0]
    bot.db.roster_upsert_login(gid, conn.service_id, "SpielerA")
    original = bot.db.roster_list(gid, conn.service_id)[0][0]
    dateien = ["/logs/datei0.ADM"]
    conn.ftp = _StubFTP({("/logs/datei0.ADM", 0): _connect_zeile("SpielerA") + "\n"})
    _run(bot.bot._roster_backfill_falls_noetig(conn, dateien))
    nachher = bot.db.roster_list(gid, conn.service_id)[0][0]
    assert nachher["first_seen"] == original["first_seen"]
    assert nachher["last_login"] == original["last_login"]


def test_ohne_guild_wird_nichts_getan(conn):
    conn.data["guild_ids"] = []
    dateien = ["/logs/datei0.ADM"]
    conn.ftp = _StubFTP({("/logs/datei0.ADM", 0): _connect_zeile("SpielerA") + "\n"})
    _run(bot.bot._roster_backfill_falls_noetig(conn, dateien))
    assert conn.roster_backfill_done


def test_log_kompletten_inhalt_liest_mehrere_haeppchen(conn):
    pfad = "/logs/gross.ADM"
    conn.ftp = _StubFTP({
        (pfad, 0): "erster teil\n",
        (pfad, len(b"erster teil\n")): "zweiter teil\n",
        (pfad, len(b"erster teil\nzweiter teil\n")): "",
    })
    inhalt = _run(bot._log_kompletten_inhalt(conn, pfad))
    assert inhalt == "erster teil\nzweiter teil\n"
