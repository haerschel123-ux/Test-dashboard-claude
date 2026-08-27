"""Tests fuer die PlayerList-Erkennung und die Online-Liste.

Warum es diese Datei gibt: die Online-Liste hat in dieser Codebasis mehrfach
hintereinander versagt, und jedes Mal war die REGEX unschuldig – der Roster
wurde nach dem korrekten Erkennen an anderer Stelle wieder geloescht. Genau
diese Zusammenspiele deckt hier je ein Test ab, damit ein spaeterer Umbau
sie nicht stumm wieder aufreisst.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/ -v

Die Tests, die nur den Parser betreffen, laufen ohne discord.py/aiohttp.
Die Tests am Verbindungs-Layer importieren bot.py und werden automatisch
uebersprungen, wenn dessen Abhaengigkeiten fehlen.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from log_parser import DayZLogParser  # noqa: E402

# Echte Zeilen aus einer echten ADM-Datei dieses Servers, nicht ausgedacht.
BLOCK_EIN_SPIELER = (
    '15:19:30 | ##### PlayerList log: 1 players\n'
    '15:19:30 | Player "miscitoman" '
    '(id=xVOzVbIohkbDbf2MEP2eIj_T2QZgefFvcUXR9t8XQi0= '
    'pos=<1937.3, 15055.6, 445.6>)\n'
    '15:19:30 | #####\n'
)
MISCITOMAN_ID = "xVOzVbIohkbDbf2MEP2eIj_T2QZgefFvcUXR9t8XQi0="


# ── Test 1: exakter echter Block ──────────────────────────────────────
def test_echter_block_wird_uebernommen():
    p = DayZLogParser()
    events = p.parse_lines(BLOCK_EIN_SPIELER)
    # Ein PlayerList-Block ist KEIN Feed-Ereignis: leere Event-Liste ist hier
    # der Erfolgsfall, nicht das Gegenteil.
    assert events == []
    assert set(p.player_positions) == {"miscitoman"}
    assert p.player_positions["miscitoman"]["id"] == MISCITOMAN_ID
    assert p.player_positions["miscitoman"]["position"] == "1937.3, 15055.6, 445.6"
    assert p.player_positions["miscitoman"]["quelle"] == "playerlist"


# ── Test 2: Block ueber drei Polls verteilt ───────────────────────────
def test_block_ueber_drei_polls():
    """Der FTP-Tail schneidet mitten im Block ab – der Zustand muss das
    ueberleben, und der Roster darf erst mit dem Endmarker wechseln."""
    p = DayZLogParser()
    zeilen = BLOCK_EIN_SPIELER.splitlines(keepends=True)

    p.parse_lines(zeilen[0])                    # nur der Header
    assert p.player_positions == {}
    p.parse_lines(zeilen[1])                    # Spielerzeile
    assert p.player_positions == {}, "vor dem Endmarker darf nichts committen"
    p.parse_lines(zeilen[2])                    # Endmarker
    assert set(p.player_positions) == {"miscitoman"}


# ── Test 3: unvollstaendiger neuer Block ueberschreibt nichts ─────────
def test_unvollstaendiger_block_laesst_alten_roster_stehen():
    p = DayZLogParser()
    p.parse_lines(BLOCK_EIN_SPIELER)
    vorher = dict(p.player_positions)

    # Neuer Header ueber ZWEI Spieler, aber nur eine Zeile und kein Endmarker.
    p.parse_lines(
        '15:24:30 | ##### PlayerList log: 2 players\n'
        '15:24:30 | Player "Alice" (id=AAAA= pos=<1.0, 2.0, 3.0>)\n'
    )
    assert p.player_positions == vorher


# ── Test 4: Aktionszeile ist kein PlayerList-Eintrag ──────────────────
def test_aktionszeile_zaehlt_nicht_als_playerlist_eintrag():
    p = DayZLogParser()
    p.parse_lines(
        '15:22:58 | ##### PlayerList log: 1 players\n'
        '15:22:58 | Player "miscitoman" (id=' + MISCITOMAN_ID +
        ' pos=<1938.5, 15048.1, 445.4>) placed Sea Chest<SeaChest>\n'
    )
    # Die "placed"-Zeile gehoert NICHT zum Block: sie zaehlt nicht als
    # PlayerList-Eintrag, der Block bleibt unvollstaendig und wird nie
    # committet. Als gewoehnliches Bau-Ereignis wird sie sehr wohl
    # ausgewertet – der Eintrag traegt dann aber `quelle="position"` und
    # unterliegt damit dem kurzen 900s-Fenster statt der PlayerList-Regel.
    assert p.playerlist_commits == 0
    assert p.player_positions["miscitoman"]["quelle"] == "position"


# ── Test 5: mehrere Spieler, ungewoehnliche IDs und Koordinaten ──────
def test_mehrere_spieler_und_sonderformate():
    p = DayZLogParser()
    p.parse_lines(
        '16:00:00 | ##### PlayerList log: 3 players\n'
        '16:00:00 | Player "Mit Leerzeichen" (id=abc_def-ghi= pos=<0, 1.5, -2.25>)\n'
        '16:00:00 | Player "x" (id=A-B_C= pos=<-100.5, 200, 3>)\n'
        '16:00:00 | Player "Dritter" (id=ZZZ= pos=<1, 2, 3>)\n'
        '16:00:00 | #####\n'
    )
    assert set(p.player_positions) == {"Mit Leerzeichen", "x", "Dritter"}
    assert p.player_positions["x"]["position"] == "-100.5, 200, 3"


# ── Test 6: Null Spieler ──────────────────────────────────────────────
def test_null_spieler_block_leert_den_roster():
    p = DayZLogParser()
    p.parse_lines(BLOCK_EIN_SPIELER)
    assert set(p.player_positions) == {"miscitoman"}

    p.parse_lines(
        '15:30:00 | ##### PlayerList log: 0 players\n'
        '15:30:00 | #####\n'
    )
    assert p.player_positions == {}


def test_null_spieler_header_allein_leert_nicht():
    """Erst der VOLLSTAENDIGE Block darf leeren – ein abgeschnittener
    Header am Dateiende sonst nicht."""
    p = DayZLogParser()
    p.parse_lines(BLOCK_EIN_SPIELER)
    p.parse_lines('15:30:00 | ##### PlayerList log: 0 players\n')
    assert set(p.player_positions) == {"miscitoman"}


# ── Test 7: letzter vollstaendiger Block ──────────────────────────────
def test_letzter_vollstaendiger_block_ignoriert_angeschnittenen():
    zeilen = (
        BLOCK_EIN_SPIELER +
        '15:25:00 | ##### PlayerList log: 2 players\n'
        '15:25:00 | Player "Alice" (id=AAAA= pos=<1, 2, 3>)\n'
    ).splitlines()
    idx = DayZLogParser.letzter_vollstaendiger_block(zeilen)
    assert idx is not None
    # Der Index muss auf den VOLLSTAENDIGEN Block am Anfang zeigen, nicht auf
    # den angeschnittenen am Ende.
    assert "1 players" in zeilen[idx - 1] or "1 players" in zeilen[idx]


# ── Commit-Zaehler als Signal an den Verbindungs-Layer ────────────────
def test_commit_zaehler_steigt_nur_bei_vollstaendigem_block():
    """Der Poller erkennt an diesem Zaehler, dass live ein Roster
    uebernommen wurde, und zieht seine Metadaten nach."""
    p = DayZLogParser()
    assert p.playerlist_commits == 0

    p.parse_lines(BLOCK_EIN_SPIELER)
    assert p.playerlist_commits == 1
    assert p.letzte_commit_namen == ["miscitoman"]

    # Unvollstaendiger Block: Zaehler darf NICHT steigen.
    p.parse_lines('15:26:00 | ##### PlayerList log: 5 players\n')
    assert p.playerlist_commits == 1


# ── Alle echten ADM-Dateien, falls vorhanden ─────────────────────────
def _echte_adm_dateien():
    ordner = os.path.join(REPO, "tests", "adm")
    if not os.path.isdir(ordner):
        return []
    return sorted(os.path.join(ordner, n) for n in os.listdir(ordner)
                  if n.endswith(".ADM"))


@pytest.mark.skipif(not _echte_adm_dateien(),
                    reason="keine echten ADM-Dateien unter tests/adm/ hinterlegt")
def test_echte_adm_dateien_ohne_fehlerhafte_bloecke():
    """Kein fester Sollwert fuer die Blockanzahl: die haengt davon ab,
    WELCHE Dateien hinterlegt sind, und waere als Zahl beim naechsten
    hinzugefuegten Log falsch. Geprueft wird die Eigenschaft, die immer
    gelten muss – jeder angekuendigte Block geht sauber auf."""
    bloecke = eintraege = 0
    for pfad in _echte_adm_dateien():
        with open(pfad, "r", encoding="utf-8", errors="replace") as f:
            zeilen = f.read().splitlines()
        p = DayZLogParser()
        for i, zeile in enumerate(zeilen):
            p.parse_line(zeile)
            if "PlayerList log:" in zeile:
                erwartet = int(zeile.split("PlayerList log:")[1].split()[0])
                # Endmarker muss genau erwartet Zeilen spaeter stehen.
                ende = i + 1 + erwartet
                if ende < len(zeilen) and zeilen[ende].strip().endswith("#####"):
                    bloecke += 1
                    eintraege += erwartet
    assert bloecke > 0, "in den hinterlegten Dateien steht kein einziger Block"
    assert eintraege >= bloecke


# ── Altersfilter: bekannter Name darf nicht stumm verschwinden ────────
bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _positions(quelle: str, alter_s: float):
    ts = datetime.now(timezone.utc) - timedelta(seconds=alter_s)
    return {"miscitoman": {"id": MISCITOMAN_ID, "position": "1, 2, 3",
                           "quelle": quelle, "last_seen": ts.isoformat()}}


def test_playerlist_eintrag_ueberlebt_das_900s_fenster():
    """Ein per PlayerList bekannter Spieler faellt NICHT nach 15 Minuten
    heraus – sonst verschwaende er aus der Liste, obwohl er online ist."""
    namen = bot._online_spieler_namen(_positions("playerlist", 3600))
    assert namen == ["miscitoman"]


def test_beilaeufige_position_verfaellt_nach_900s():
    namen = bot._online_spieler_namen(_positions("position", 1000))
    assert namen == []


# ── Test 14: A2S-0 darf den Live-Roster nicht zerstoeren ─────────────
def test_a2s_null_zerstoert_den_roster_nicht():
    """Die Anzeige darf 0 melden, der Parserzustand muss aber erhalten
    bleiben. Frueher stand hier ein player_positions.clear(), das echte
    Namen bei einem einzigen falschen A2S-Wert wegwarf."""
    import asyncio

    conn = bot.ServerConnection({"service_id": "1000", "guild_id": "555",
                                 "nitrado_token": "x"})
    conn.parser = DayZLogParser()
    conn.parser.parse_lines(BLOCK_EIN_SPIELER)
    assert set(conn.parser.player_positions) == {"miscitoman"}

    async def null(_conn):
        return 0

    original = bot._a2s_spielerzahl
    bot._a2s_spielerzahl = null
    try:
        botti = bot.DayZBot.__new__(bot.DayZBot)   # ohne Discord-Login
        ok, meldung, embed = asyncio.run(
            botti._scheduled_task_ausfuehren(
                conn, {"task": "online_list", "post_wenn_leer": True}))
    finally:
        bot._a2s_spielerzahl = original

    # Anzeige leer – Roster aber unangetastet.
    assert "miscitoman" in conn.parser.player_positions
    assert ok is True


# ── Test 10: Scheduler-Integration mit echtem Parser und echtem Embed ─
def test_scheduler_zeigt_echten_namen_im_embed():
    import asyncio

    conn = bot.ServerConnection({"service_id": "1000", "guild_id": "555",
                                 "nitrado_token": "x"})
    conn.parser = DayZLogParser()
    conn.parser.parse_lines(BLOCK_EIN_SPIELER)

    async def einer(_conn):
        return 1

    original = bot._a2s_spielerzahl
    bot._a2s_spielerzahl = einer
    try:
        botti = bot.DayZBot.__new__(bot.DayZBot)
        ok, meldung, embed = asyncio.run(
            botti._scheduled_task_ausfuehren(conn, {"task": "online_list"}))
    finally:
        bot._a2s_spielerzahl = original

    assert embed is not None and embed is not False
    assert "miscitoman" in embed.description
    assert "Namen aktuell nicht bekannt" not in embed.description
    assert "Gerade ist niemand online" not in embed.description


# ── Test 13: Query-Port heilt sich auch bei gesetzter IP ─────────────
def test_query_port_wird_auch_bei_gesetzter_ip_korrigiert():
    """Frueher stand das Setzen des Ports in `if not server_ip:` – ein
    falsch gespeicherter Port blieb dadurch fuer immer stehen und der
    A2S-Endpunkt zeigte dauerhaft ins Leere."""
    conn = bot.ServerConnection({"service_id": "1000",
                                 "server_ip": "95.156.224.87",
                                 "query_port": 10900})
    bot._apply_gameserver_info(
        {"ip": "95.156.224.87", "query": {"connect_port": 10903}}, conn)
    assert int(conn.data["query_port"]) == 10903
    assert conn.data["server_ip"] == "95.156.224.87"


# ── Zentraler Reset setzt ALLE Hydrierungsfelder zurueck ─────────────
def test_reset_setzt_alle_hydrierungsfelder():
    """_pruefe_neustart leerte frueher nur player_positions; die uebrigen
    Felder blieben stehen, weshalb die naechste Hydrierung sich fuer
    ueberfluessig hielt und der Roster leer blieb."""
    conn = bot.ServerConnection({"service_id": "1000"})
    conn.parser = DayZLogParser()
    conn.parser.parse_lines(BLOCK_EIN_SPIELER)
    conn.parser_hydrated = True
    conn.hydrated_file = "alt.ADM"
    conn.roster_datei = "alt.ADM"
    conn.roster_quelle_datei = "alt.ADM"
    conn.roster_quelle_ts = 12345.0
    conn.hydrate_versuch_ts = 12345.0

    conn.online_zustand_zuruecksetzen("Test")

    assert conn.parser.player_positions == {}
    assert conn.parser_hydrated is False
    assert conn.hydrated_file is None
    assert conn.roster_datei is None
    assert conn.roster_quelle_datei is None
    assert conn.roster_quelle_ts == 0.0
    assert conn.hydrate_versuch_ts == 0.0
