"""DayZ-ADM-Log-Parser.

Ausgelagert aus bot.py: DayZLogParser haengt ausschliesslich von
Standardbibliothek (re, datetime) ab, keine Discord-/Dashboard-/
Konfigurations-Globalen - deshalb risikoarm eigenstaendig verschiebbar.
Quelle: Nitrado DayZ Konsolen-Server .ADM Logs.
"""
import re
from collections import deque
from datetime import datetime, timezone
from typing import Optional, List, Dict, Deque


class DayZLogParser:
    """Parst die ADM-Logzeilen EINES Servers.

    Die Spieler-Positionen liegen bewusst auf der Instanz: als
    Klassen-Attribut teilten sich alle verbundenen Server dasselbe Dict,
    wodurch jeder Kunde die Positionen aller anderen sah und Zonen-Pings im
    falschen Discord ausgeloest wurden.
    """

    def __init__(self):
        self.player_positions: Dict[str, Dict] = {}
        # Diagnose (reiner Arbeitsspeicher, nichts davon wird gespeichert):
        # wie viele Zeilen kamen an, wie viele wurden zu einem Ereignis, und
        # die letzten paar, die KEIN Muster getroffen haben – im Original,
        # damit sich das Log-Format des Servers ohne Raten nachziehen laesst.
        # Vorher gab es dafuer ueberhaupt keine Sichtbarkeit: eine Zeile, die
        # an keinem Muster haengen blieb, verschwand kommentarlos.
        self.zeilen_gelesen: int = 0
        self.zeilen_erkannt: int = 0
        self.unerkannte_zeilen: Deque[str] = deque(maxlen=50)
        # NUR die seit dem letzten Poll-Zyklus neu hinzugekommenen unerkannten
        # Zeilen (der Ringpuffer oben verliert alte Eintraege beim Auffuellen,
        # eignet sich also nicht als "was ist neu" – siehe _poll_connection,
        # das dies nach jedem Zyklus wieder leert).
        self.frisch_unerkannt: List[str] = []
        # Zaehlt JEDEN uebernommenen vollstaendigen PlayerList-Block. Der
        # Verbindungs-Layer vergleicht den Stand vor und nach dem Parsen und
        # weiss dadurch, ob live ein Roster committet wurde (siehe
        # _playerlist_abschliessen). "Generation" statt Zeitstempel, weil ein
        # Zaehler nicht von der Systemuhr abhaengt und nie rueckwaerts laeuft.
        self.playerlist_commits: int = 0
        # Namen des zuletzt uebernommenen Blocks – nur fuer Diagnose/Logging.
        self.letzte_commit_namen: List[str] = []
        # "##### PlayerList log: N players" – manche Server-Konfigurationen
        # schreiben periodisch eine vollstaendige Momentaufnahme, wer gerade
        # verbunden ist. Verlaesslicher als das last_seen-Zeitfenster: wer
        # dort NICHT auftaucht, ist nicht mehr online, selbst wenn eine
        # aeltere Position noch "frisch genug" waere (siehe parse_line unten).
        self._playerlist_offen: bool = False
        self._playerlist_erwartet: int = 0
        self._playerlist_gesehen: set = set()
        # Ein PlayerList-Block ist erst mit seiner eigenen Abschlusszeile
        # "#####" autoritativ. Bis dahin bleiben die Eintraege getrennt vom
        # Live-Zustand, damit ein am Dateiende nur halb geschriebener Block
        # weder Namen hinzufuegt noch den bisherigen Roster ersetzt.
        self._playerlist_pending: Dict[str, Dict] = {}
        # Letzter nicht-toedlicher Umwelt-Treffer je Spieler (Angreifer/Waffe
        # + Zeitpunkt). Grund: DayZ schreibt in die Todeszeile selbst nur bei
        # ausdruecklich benannten Ursachen "killed by X"/"due to X" - Sturz,
        # Feuer und einige andere Umwelt-Tode schreiben NUR ein nacktes
        # "died." OHNE jede Ursache (in der echten Spiel-Quelle bestaetigt).
        # Ohne diese Kopplung landet z. B. jeder Sturztod unter "Unknown
        # Death" statt "Fall Death", weil die Todeszeile allein keine Angabe
        # dazu enthaelt, WORAN der Spieler gestorben ist - siehe
        # _generic_env_death_event.
        self._letzte_umwelt_treffer: Dict[str, Dict[str, str]] = {}

    # Spieler-Muster: Name + optionale Steam-ID.
    # Tolerant gegenüber dem echten Nitrado-Konsolen-ADM-Format:
    #   Player "Name" (DEAD) (id=ABC123 pos=<7500.0, 8500.0, 12.3>)[HP: 85.6]
    # - "(DEAD)" zwischen Name und id-Klammer
    # - kein oder mehrere Leerzeichen vor "(id=..."
    # - pos=<...> INNERHALB der id-Klammer (id wird ohne pos-Anteil erfasst)
    # - "[HP: ...]" direkt hinter der Klammer
    PLAYER = (r'Player\s*"([^"]+)"'
              r'(?:\s*\(DEAD\))?'
              r'(?:\s*\(id=([^)\s]+)[^)]*\))?'
              r'(?:\s*\[HP:[^\]]*\])?')

    # Lockeres Ersatz-Muster NUR fuer die Notfall-Rueckfaelle unten (nie fuer
    # die praezisen Haupt-Muster in P): auch einfache Anfuehrungszeichen und
    # "Player" darf fehlen, wenn direkt eine id=-Klammer folgt. Fängt Format-
    # Abweichungen ab, die sonst eine Zeile komplett spurlos verwerfen würden.
    PLAYER_LOSE = (r'(?:Player\s*)?[\'"]([^\'"]{1,64})[\'"]'
                   r'(?:\s*\(id=([^)\s]+)[^)]*\))?')

    # ── Regex-Muster ──────────────────────────────────────────
    P = {
        # Kopfzeile einer vollstaendigen Spielerliste, z. B.
        # "##### PlayerList log: 4 players" - danach folgen N Zeilen im
        # normalen Positions-Format (siehe "position" unten), aber OHNE
        # weitere Aktion (kein "killed by", "connected" usw.).
        "playerlist_start": re.compile(
            r'#####\s*PlayerList log:\s*(\d+)\s*players?', re.IGNORECASE
        ),
        # Nitrado-Konsole schliesst den Block mit einer eigenen Zeile, die
        # NUR aus "#####" besteht (kein "PlayerList log" mehr davor/danach -
        # das unterscheidet sie von playerlist_start). Ohne diese eigene
        # Erkennung landete sie als "unerkannte Zeile" auf der Diagnose-
        # Seite, obwohl sie zum Block gehoert und nichts Unerwartetes ist.
        "playerlist_end": re.compile(
            r'^\s*(?:\d{2}:\d{2}:\d{2}\s*\|?\s*)?#####\s*$'
        ),
        # Eine EINZELNE Zeile aus so einem Block: Zeitstempel, Spieler,
        # Position – und sonst NICHTS. Bewusst als vollstaendige Zeile
        # (^...$) geprueft: nur so laesst sich "gehoert zum Block" von
        # "Spieler tut etwas" sicher unterscheiden. Deckt beide Formate ab
        # (pos INNERHALB der id-Klammer wie auf Konsole, pos dahinter wie im
        # alten PC-Format).
        "playerlist_entry": re.compile(
            r'^(?:\d{2}:\d{2}:\d{2}\s*\|?\s*)?'
            r'Player\s*"([^"]+)"(?:\s*\(DEAD\))?\s*'
            r'(?:\(id=([^)\s]+)(?:\s+pos\s*=\s*<([\d., \-]+)>)?[^)]*\))?'
            r'(?:\s*pos\s*=\s*<([\d., \-]+)>)?'
            r'(?:\s*\[HP:[^\]]*\])?\s*$', re.IGNORECASE
        ),
        # Konsolen-Format: pos INNERHALB der id-Klammer, pro Spieler
        "position": re.compile(
            r'Player\s*"([^"]+)"(?:\s*\(DEAD\))?\s*\(id=([^)\s]+)\s+pos\s*=\s*<([\d., \-]+)>[^)]*\)',
            re.IGNORECASE
        ),
        # Altes PC-Format: pos irgendwo hinter dem Spieler
        "position_legacy": re.compile(
            r'Player "([^"]+)"(?:\s+\(id=([^)]+)\))?.*?(?:pos|position)=<([\d., \-]+)>',
            re.IGNORECASE
        ),
        # PvP-Kill – tolerant gegenüber fehlender ID oder Zusatztext
        "kill_pvp": re.compile(
            PLAYER + r'\s*(?:was\s+)?(?:killed|murdered)\s+by\s+' + PLAYER +
            r'(?:\s+with\s+(.+?))?(?:\s+from\s+([\d.,]+)\s*m(?:eters?)?)?(?:\s+at\s+pos=<([\d., \-]+)>)?\s*$',
            re.IGNORECASE
        ),
        # Umwelt-Tod (kein PvP) – verschiedene Formulierungen. "killed" (ohne
        # "was") MUSS hier stehen, sonst laufen alle Zombie-/Tier-Tode nur
        # noch über die Notfall-Rückfälle statt über dieses präzise Muster –
        # kill_pvp steht in der Prüfreihenfolge davor und verlangt für den
        # zweiten Teil zwingend ein zweites "Player "..."", ein Klassenname
        # wie ZmbM_Hermit erfüllt das nicht, also bleibt PvP hier sauber.
        "kill_env": re.compile(
            PLAYER + r'\s*(?:died|was killed|killed|has died|perished|bled out'
                     r'|starved|dehydrated|drowned|suffocated|froze to death)'
            r'[.!]?'
            r'(?:\s+by\s+(.+?))?(?:\s+at\s+pos=<([\d., \-]+)>)?(?:\s+due to\s+(.+?))?(?:[.!]|\s|$)',
            re.IGNORECASE
        ),
        # Suicide
        "suicide": re.compile(
            PLAYER + r'\s*(?:committed suicide|killed themselves|blew themselves up|ended their life)',
            re.IGNORECASE
        ),
        # Damage/Hit – tolerant bei Bodypart / Waffe / Munition / Distanz
        "damage": re.compile(
            PLAYER + r'\s*(?:was\s+)?hit by\s+' + PLAYER +
            r'(?:\s+into\s+(.+?))?'
            r'(?:\s+for\s+([\d.,]+)\s+damage)?'
            r'(?:\s*\([^)]*\))?'
            r'(?:\s+with\s+(.+?))?'
            r'(?:\s+from\s+([\d.,]+)\s*m(?:eters?)?)?'
            r'(?:\s+at\s+pos=<([\d., \-]+)>)?\s*$',
            re.IGNORECASE
        ),
        # Connect – Konsole schreibt "is connected"
        "connect": re.compile(
            PLAYER + r'\s+(?:is\s+)?connected\b(?:\s+from\s+.+?)?(?:\s+at\s+pos=<([\d., \-]+)>)?',
            re.IGNORECASE
        ),
        # Disconnect – Konsole schreibt "has been disconnected"
        "disconnect": re.compile(
            PLAYER + r'\s+(?:has\s+been\s+)?disconnected\b(?:\s+from\s+.+?)?(?:\s+at\s+pos=<([\d., \-]+)>)?',
            re.IGNORECASE
        ),
        # Is Connecting
        "connecting": re.compile(
            PLAYER + r'\s+is connecting(?:\s+from\s+.+?)?(?:\s+at\s+pos=<([\d., \-]+)>)?',
            re.IGNORECASE
        ),
        # Chat (Side / Direct / Vehicle / Megaphone / Radio)
        "chat": re.compile(
            r'\((Side|Direct|Vehicle|Megaphone|Radio|GlobalBanMessage|Unknown)\) ([^:]+): (.+)',
            re.IGNORECASE
        ),
        # Chat – Konsolen-Format: Chat("Name"(id=...)): Nachricht
        "chat_console": re.compile(
            r'Chat\s*\(\s*"([^"]+)"[^)]*\)\s*\)?\s*:\s*(.+)',
            re.IGNORECASE
        ),
        # Admin-Aktion
        "admin_action": re.compile(
            r'Admin "([^"]+)"(?:\s+\(id=([^)]+)\))?(?: issued command:? (.+))?',
            re.IGNORECASE
        ),
        # Basis-Bau
        "basebuild": re.compile(
            # Das Verb steht in einer eigenen Fanggruppe: daran haengt die
            # Aufteilung in Build/Dismantle/Place/Pack/Fold/... (_feed_key).
            PLAYER + r'\s+(placed|built|constructed|dismantled|repaired|attached|removed'
                     r'|folded|packed|deployed|mounted|unmounted'
                     r'|raised|lowered)\s+([^\n]+)',
            re.IGNORECASE
        ),
        # Emote (z. B. "performed EmoteSitA with CableReel", "performed
        # EmoteSurrender" ohne Gegenstand). "with <Gegenstand>" ist optional.
        "emote": re.compile(
            PLAYER + r'\s+performed\s+(\w+)(?:\s+with\s+([^\n]+))?',
            re.IGNORECASE
        ),
        # Vergraben/Ausgraben eines Verstecks – VOELLIG anderes Format als
        # die uebrigen Bau-Aktionen (belegt im DayZ-Quellcode, ActionDigIn/
        # OutStash): kein Leerzeichen nach der Spieler-Klammer, ein zweiter,
        # unzitierter "Player <Klasse><Zeiger>"-Block, dann "Dug in"/"Dug out"
        # <Objekt><Zeiger> "at position" <Hex-Adresse> {<x,y,z>}. Die Verben
        # "buried"/"unburied" kommen in echten Logs gar nicht vor.
        "dig_stash": re.compile(
            PLAYER + r'Player\s+\w+<[0-9a-fA-F]+>\s+Dug\s+(in|out)\s+'
                     r'(\w+)<[0-9a-fA-F]+>\s+at\s+position\s+0x[0-9a-fA-F]+',
            re.IGNORECASE
        ),
        # Fahrzeug
        "vehicle": re.compile(
            r'(Vehicle|Car|Truck|Heli|Helicopter|Boat|UH[\w-]+)\s+'
            r'(?:crashed|exploded|was damaged|was destroyed|burned|flipped)',
            re.IGNORECASE
        ),
        # Loot-Spawn
        "loot": re.compile(
            r'(Loot|Item)\s+"([^"]+)"\s+(spawned|despawned|created|deleted|moved)',
            re.IGNORECASE
        ),
        # Bewusstlos / wieder bei Bewusstsein. Welche Formulierung DayZ genau
        # schreibt, haengt von Version und Mods ab – deshalb mehrere Varianten.
        "unconscious": re.compile(
            PLAYER + r'\s+(?:is\s+)?(?:unconscious|knocked\s+out|lost\s+consciousness)\b',
            re.IGNORECASE
        ),
        "conscious": re.compile(
            PLAYER + r'\s+(?:is\s+)?(?:conscious|regained\s+consciousness|woke\s+up)\b',
            re.IGNORECASE
        ),
    }

    # Event-Typ → Log-Typ (für Channel-Routing)
    EVENT_TO_LOG = {
        "kill_pvp":     "killfeed",
        "suicide":      "suicide",
        "kill_env":     "envdeath",
        "damage":       "damagefeed",
        "connect":      "joinleave",
        "disconnect":   "joinleave",
        "connecting":   "connecting",
        "chat":         "chat",
        "admin_action": "adminlog",
        "basebuild":    "basebuild",
        "vehicle":      "vehiclecrash",
        "loot":         "loot",
        # Zustandswechsel gehen in den Umwelttod-Sammelkanal, solange jemand
        # noch die alten groben Kategorien benutzt. Die feine Zuordnung macht
        # _feed_key.
        "unconscious":  "envdeath",
        "conscious":    "envdeath",
    }

    def _players_found(self, line: str) -> List[Dict[str, Optional[str]]]:
        """Spieler-Nennungen fuer die Notfall-Rueckfaelle unten.

        Bewusst GROSS-/KLEINSCHREIBUNG-UNABHAENGIG (frueher ohne IGNORECASE –
        eine Konsole, die "player" statt "Player" schreibt, fand dadurch NIE
        einen Rückfall-Treffer, egal wie tolerant die Schluesselwort-Suche
        darum herum war). Schlaegt das strenge PLAYER-Muster fehl, zaehlt
        zusaetzlich das lockere PLAYER_LOSE (einfache Anfuehrungszeichen,
        "Player" auch ganz weggelassen).
        """
        treffer = re.findall(self.PLAYER, line, re.IGNORECASE)
        if not treffer:
            treffer = re.findall(self.PLAYER_LOSE, line, re.IGNORECASE)
        return [{"name": name, "id": pid or None} for name, pid in treffer]

    def _set_position(self, name: str, player_id: Optional[str], pos: str,
                      quelle: str = "position"):
        self.player_positions[name] = {
            "id": player_id,
            "position": pos.strip(),
            "last_seen": datetime.now(timezone.utc).isoformat(),
            # Herkunft des Eintrags, siehe _set_gesehen und
            # _online_spieler_namen: eine beilaeufige Position (aus einem
            # Kill/Treffer/Bau-Ereignis) veraltet nach 900s, eine Connect-
            # Meldung oder ein vollstaendiger PlayerList-Block gilt dagegen
            # bis zum Disconnect bzw. bis der naechste Block widerspricht.
            "quelle": quelle,
        }

    def _pos_aus_zeile(self, line: str, name: str,
                       player_id: Optional[str]) -> Optional[str]:
        """``pos=<x, y, z>`` aus der Zeile holen, den Positions-Cache damit
        auffrischen und die Position zurueckgeben (sonst ``None``).

        PLAYER erfasst die id-Klammer bewusst OHNE den pos-Anteil (siehe
        Kommentar dort), deshalb muss jeder Zweig, dessen Embed ein Orts-Feld
        zeigt, extra danach suchen - genau das fehlte bei den Bau-Ereignissen,
        wodurch der iZurvive-Link dort verschwand, sobald nicht zufaellig eine
        ANDERE Zeile desselben Spielers den Cache aktuell hielt.

        Zweite Form ``{<x, y, z>}``: so schreibt die Konsole die Position beim
        Vergraben/Ausgraben (dig_stash), dort gibt es kein ``pos=``.
        """
        m = re.search(r'pos\s*=\s*<([\d., \-]+)>', line, re.IGNORECASE)
        if not m:
            m = re.search(r'\{<([\d., \-]+)>\}', line)
        if not m:
            return None
        pos = m.group(1).strip()
        self._set_position(name, player_id, pos)
        return pos

    def _set_gesehen(self, name: str, player_id: Optional[str]):
        """Der Spieler ist NACHWEISLICH online, aber die Zeile nennt keine
        Position (die Konsole schreibt ``Player "X"(id=...) is connected``
        ohne ``pos=``).

        Ohne diesen Weg landete so ein Spieler NIE im Tracking: _set_position
        lief nur bei Zeilen mit ``pos=``, und wenn der Server die periodischen
        Positions-Zeilen nicht schreibt (adminLogPlayerList = 0), blieb die
        Online-Liste dauerhaft leer, egal wer verbunden war.

        Eine bereits bekannte Position bleibt erhalten – sie ist besser als
        gar keine, etwa fuer die Zonen-Pruefung und die Ereignis-Karte.
        """
        alt = self.player_positions.get(name) or {}
        self.player_positions[name] = {
            "id": player_id or alt.get("id"),
            "position": alt.get("position"),
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "quelle": "connect",
        }

    def _extract_ts(self, line: str) -> str:
        ts_m = re.match(r'^(\d{2}:\d{2}:\d{2})\s*\|?\s*', line)
        return ts_m.group(1) if ts_m else ""

    @staticmethod
    def _ts_diff_sekunden(a: str, b: str) -> float:
        """Abstand zweier ``HH:MM:SS``-Zeitstempel in Sekunden, ueber
        Mitternacht hinweg korrekt. Unlesbare Zeitstempel gelten als "weit
        auseinander" (kein falsches Zusammenfuehren durch Zufall)."""
        m1 = re.match(r'^(\d{2}):(\d{2}):(\d{2})$', a or "")
        m2 = re.match(r'^(\d{2}):(\d{2}):(\d{2})$', b or "")
        if not m1 or not m2:
            return 999.0
        s1 = int(m1.group(1)) * 3600 + int(m1.group(2)) * 60 + int(m1.group(3))
        s2 = int(m2.group(1)) * 3600 + int(m2.group(2)) * 60 + int(m2.group(3))
        diff = abs(s1 - s2)
        return min(diff, 86400 - diff)

    def _generic_kill_event(self, line: str, ts: str) -> Optional[Dict]:
        players = self._players_found(line)
        if len(players) < 2 or "killed by" not in line.lower():
            return None

        victim = players[0]
        killer = players[1]

        weapon = "Unbekannt"
        distance = "?"
        # Sehr tolerante Fallbacks für Zusatzinformationen
        m_weapon = re.search(r'\b(?:with|using)\s+(.+?)(?=\s+from\s+[\d.,]+\s*m|\s+at\s+pos=<|$)', line, re.IGNORECASE)
        if m_weapon:
            weapon = m_weapon.group(1).strip()
        m_dist = re.search(r'\bfrom\s+([\d.,]+)\s*m(?:eters?)?', line, re.IGNORECASE)
        if m_dist:
            distance = m_dist.group(1)

        pos_m = re.search(r'pos=<([\d., \-]+)>', line, re.IGNORECASE)
        if pos_m:
            self._set_position(victim["name"], victim["id"], pos_m.group(1))

        return {
            "type": "kill_pvp",
            "timestamp": ts,
            "victim": victim["name"],
            "victim_id": victim["id"] or "Unbekannt",
            "killer": killer["name"],
            "killer_id": killer["id"] or "Unbekannt",
            "weapon": weapon,
            "distance": distance,
            "raw": line,
        }

    def _generic_damage_event(self, line: str, ts: str) -> Optional[Dict]:
        players = self._players_found(line)
        if len(players) < 2 or "hit by" not in line.lower():
            return None

        victim = players[0]
        attacker = players[1]

        hit_zone = "Unbekannt"
        damage = "?"
        weapon = "Unbekannt"
        distance = "?"

        m_zone = re.search(r'\bhit by\b.*?\binto\s+(.+?)(?=\s+for\s+[\d.,]+\s+damage|\s+with\s+|$)', line, re.IGNORECASE)
        if m_zone:
            hit_zone = m_zone.group(1).strip()

        m_damage = re.search(r'\bfor\s+([\d.,]+)\s+damage\b', line, re.IGNORECASE)
        if m_damage:
            damage = m_damage.group(1)

        m_weapon = re.search(r'\bwith\s+(.+?)(?=\s+from\s+[\d.,]+\s*m|\s+at\s+pos=<|$)', line, re.IGNORECASE)
        if m_weapon:
            weapon = m_weapon.group(1).strip()

        m_dist = re.search(r'\bfrom\s+([\d.,]+)\s*m(?:eters?)?', line, re.IGNORECASE)
        if m_dist:
            distance = m_dist.group(1)

        pos_m = re.search(r'pos=<([\d., \-]+)>', line, re.IGNORECASE)
        if pos_m:
            self._set_position(victim["name"], victim["id"], pos_m.group(1))

        return {
            "type": "damage",
            "timestamp": ts,
            "victim": victim["name"],
            "victim_id": victim["id"] or "Unbekannt",
            "attacker": attacker["name"],
            "attacker_id": attacker["id"] or "Unbekannt",
            "hit_zone": hit_zone,
            "damage": damage,
            "weapon": weapon,
            "distance": distance,
            "raw": line,
        }

    def _generic_env_death_event(self, line: str, ts: str) -> Optional[Dict]:
        """Tod ohne zweiten Spieler: Zombie, Explosion, Verbluten, Sturz usw."""
        players = self._players_found(line)
        if not players:
            return None
        p = players[0]

        cause = "Umgebung"
        m = re.search(r'\b(?:killed\s+by|died\s+(?:by|from|of)|due\s+to)\s+(.+?)(?=\s+at\s+pos=<|\s+with\s+|\s*[.!]?\s*$)',
                      line, re.IGNORECASE)
        if m:
            cause = m.group(1).strip()
        else:
            for kw, txt in (("bled out", "Verblutet"), ("starved", "Verhungert"),
                            ("dehydrated", "Verdurstet"), ("drowned", "Ertrunken"),
                            ("suffocated", "Erstickt"), ("froze", "Erfroren"),
                            ("fall", "Sturzschaden")):
                if kw in line.lower():
                    cause = txt
                    break
            if cause == "Umgebung":
                # Immer noch keine Ursache in der Todeszeile selbst: DayZ
                # schreibt bei vielen Umwelt-Toden (Sturz, Feuer, ...) nur ein
                # nacktes "died." ohne jede Angabe - belegt in echten .ADM-
                # Dateien. Der letzte nicht-toedliche Treffer DESSELBEN
                # Spielers kurz zuvor (siehe _generic_env_damage_event) traegt
                # die Ursache stattdessen; ohne das landen z. B. alle
                # Sturztode unter "Unknown Death". Nur innerhalb weniger
                # Sekunden verwendet, damit ein laengst verheilter alter
                # Treffer nicht faelschlich einem spaeteren Tod zugeschrieben
                # wird (z. B. Verhungern Minuten nach einem ueberlebten
                # Wolfsangriff).
                letzter = self._letzte_umwelt_treffer.pop(p["name"], None)
                if letzter is not None and self._ts_diff_sekunden(ts, letzter.get("ts", "")) <= 5:
                    kombiniert = " ".join(
                        x for x in (letzter.get("ammo"), letzter.get("attacker"),
                                   letzter.get("weapon"))
                        if x and x != "Unbekannt")
                    if kombiniert:
                        cause = kombiniert

        pos_m = re.search(r'pos\s*=\s*<([\d., \-]+)>', line, re.IGNORECASE)
        if pos_m:
            self._set_position(p["name"], p["id"], pos_m.group(1))

        return {
            "type": "kill_env",
            "timestamp": ts,
            "player": p["name"],
            "player_id": p["id"] or "Unbekannt",
            "cause": cause,
            "position": pos_m.group(1).strip() if pos_m else None,
            "raw": line,
        }

    def _generic_env_damage_event(self, line: str, ts: str) -> Optional[Dict]:
        """Treffer ohne zweiten Spieler: Zombie, FallDamage, Explosion, Tier usw."""
        players = self._players_found(line)
        if not players or "hit by" not in line.lower():
            return None
        victim = players[0]

        attacker = "Umgebung"
        m_att = re.search(r'\bhit by\s+(.+?)(?=\s+into\s+|\s+for\s+[\d.,]+\s+damage|\s+with\s+|\s+from\s+[\d.,]+\s*m|\s+at\s+pos=<|\s*$)',
                          line, re.IGNORECASE)
        if m_att:
            attacker = m_att.group(1).strip()

        hit_zone = "Unbekannt"
        m_zone = re.search(r'\binto\s+(.+?)(?=\s+for\s+[\d.,]+\s+damage|\s+with\s+|\s*$)', line, re.IGNORECASE)
        if m_zone:
            hit_zone = m_zone.group(1).strip()

        damage = "?"
        m_damage = re.search(r'\bfor\s+([\d.,]+)\s+damage\b', line, re.IGNORECASE)
        if m_damage:
            damage = m_damage.group(1)

        weapon = "Unbekannt"
        m_weapon = re.search(r'\bwith\s+(.+?)(?=\s+from\s+[\d.,]+\s*m|\s+at\s+pos=<|\s*$)', line, re.IGNORECASE)
        if m_weapon:
            weapon = m_weapon.group(1).strip()

        # Munitions-/Schadensklasse in der Klammer am Zeilenende, z.B.
        # "... for 9 damage (MeleeZombie)". Ein waehlbarer Server-Konstante
        # aus dem Spielcode, nicht lokalisiert - verlaesslicher als der
        # Anzeigename (der lokalisiert ist und Leerzeichen enthalten kann,
        # z. B. "Brown Bear") oder eine Stichwortsuche darin (z. B. enthaelt
        # "TripwireTrap" zufaellig "wire" und traefe sonst faelschlich
        # Stacheldraht statt Falle). Siehe _AMMO_TREFFER.
        ammo = None
        m_ammo = re.search(r'\(([A-Za-z0-9_]+)\)\s*$', line)
        if m_ammo:
            ammo = m_ammo.group(1)

        pos_m = re.search(r'pos\s*=\s*<([\d., \-]+)>', line, re.IGNORECASE)
        if pos_m:
            self._set_position(victim["name"], victim["id"], pos_m.group(1))

        # Fuer eine spaetere ursachenlose Todeszeile merken (siehe
        # _generic_env_death_event) - nur nicht-toedliche Treffer sind hier
        # relevant, ein Tod raeumt den Merker selbst wieder ab.
        self._letzte_umwelt_treffer[victim["name"]] = {
            "attacker": attacker, "weapon": weapon, "ammo": ammo or "", "ts": ts,
        }

        return {
            "type": "damage",
            "timestamp": ts,
            "victim": victim["name"],
            "victim_id": victim["id"] or "Unbekannt",
            "attacker": attacker,
            "attacker_id": "Umgebung",
            "hit_zone": hit_zone,
            "damage": damage,
            "weapon": weapon,
            "ammo": ammo,
            "distance": "?",
            "position": pos_m.group(1).strip() if pos_m else None,
            "raw": line,
        }

    def _playerlist_zeile(self, line: str) -> bool:
        """Gehoert ``line`` zum gerade offenen PlayerList-Block?

        ``True`` heisst: verarbeitet, der Aufrufer ist fertig mit der Zeile.
        ``False`` verwirft den noch nicht abgeschlossenen Block, weil eine
        unvollstaendige Momentaufnahme nichts beweist.

        Die alte Fassung zaehlte stattdessen einen Zaehler herunter, sobald
        IRGENDEINE Zeile bis ans Ende von parse_line durchlief. Blieb auch nur
        eine Zeile des Blocks vorher haengen, zaehlten spaeter voellig fremde
        Zeilen mit, und beim Nulldurchgang loeschte das Aufraeumen jeden
        Spieler, der nicht in der (unvollstaendigen) Momentaufnahme stand –
        im schlimmsten Fall alle. Genau daher kam die dauerhaft leere
        Online-Liste.
        """
        m = self.P["playerlist_entry"].match(line)
        if not m:
            self._playerlist_offen = False
            self._playerlist_erwartet = 0
            self._playerlist_gesehen = set()
            self._playerlist_pending = {}
            return False
        name, pid = m.group(1), m.group(2)
        pos = m.group(3) or m.group(4)
        self._playerlist_gesehen.add(name)
        # quelle="playerlist" statt "position": diese Zeile stammt aus DayZs
        # eigener, vollstaendiger Spielerliste, nicht aus einer beilaeufigen
        # Position bei irgendeinem Ereignis. Sie soll nicht nach 900s
        # verfallen, sondern wie ein Connect bis zum naechsten (widerspre-
        # chenden) Block bzw. Disconnect gelten - sonst faellt ein Spieler,
        # der schon vor dem Bot-Start online war und nie eine "is connected"-
        # Zeile erzeugt hat, zwischen zwei Block-Durchlaeufen aus der Online-
        # Liste, obwohl er die ganze Zeit da ist (das war der Grund fuer
        # "3 Spieler, 0 Namen bekannt"). Erst NACH der eigenen Abschlusszeile
        # "#####" gilt der Block als bestaetigt (siehe parse_line) - bis
        # dahin liegt er nur in _playerlist_pending, nicht in player_positions.
        alt = self.player_positions.get(name) or {}
        if pos:
            position = pos.strip()
        else:
            position = alt.get("position")
        self._playerlist_pending[name] = {
            "id": pid or alt.get("id"),
            "position": position,
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "quelle": "playerlist",
        }
        return True

    @classmethod
    def letzter_vollstaendiger_block(cls, zeilen: List[str]) -> Optional[int]:
        """Zeilen-Index der Kopfzeile des JÜNGSTEN VOLLSTÄNDIGEN
        PlayerList-Blocks, sonst ``None``.

        Vollständig heißt: auf ``##### PlayerList log: N players`` folgen
        wirklich exakt N Spielerzeilen und danach die eigene Abschlusszeile
        ``#####``. Ein angeschnittener Block ist NICHT autoritativ.
        """
        for i in range(len(zeilen) - 1, -1, -1):
            m = cls.P["playerlist_start"].search(zeilen[i])
            if not m:
                continue
            try:
                erwartet = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if erwartet < 0:
                continue
            ende_idx = i + 1 + erwartet
            if ende_idx >= len(zeilen):
                continue
            eintraege = zeilen[i + 1:ende_idx]
            if len(eintraege) != erwartet:
                continue
            if not all(cls.P["playerlist_entry"].match(z) for z in eintraege):
                continue
            if cls.P["playerlist_end"].match(zeilen[ende_idx]):
                return i
        return None

    def _playerlist_abschliessen(self):
        """Ein VOLLSTAENDIGER Block gilt als Wahrheit: wer darin fehlt, ist
        nicht mehr online – auch wenn seine letzte Position noch frisch waere
        oder er per Connect vermerkt ist (Absturz ohne Disconnect-Zeile)."""
        self.player_positions.clear()
        self.player_positions.update(self._playerlist_pending)
        # Zaehler HOCH, bevor der Zwischenspeicher geleert wird: der
        # Verbindungs-Layer (DayZBot._poll_connection) vergleicht ihn vor und
        # nach dem Parsen und erkennt daran, dass hier LIVE ein vollstaendiger
        # Block uebernommen wurde. Ohne dieses Signal glaubten roster_datei
        # und Diagnose weiterhin, der Zustand stamme aus einer alten Datei oder
        # sei gar nicht hydriert - und die Verwerfen-Zweige loeschten einen
        # gerade erst korrekt gelesenen Roster wieder.
        self.playerlist_commits += 1
        self.letzte_commit_namen = sorted(self._playerlist_pending)
        self._playerlist_offen = False
        self._playerlist_erwartet = 0
        self._playerlist_gesehen = set()
        self._playerlist_pending = {}

    def parse_line(self, line: str):
        line = line.strip()
        if not line:
            return None

        ts = self._extract_ts(line)

        # Kopfzeile einer vollstaendigen Spielerliste: die naechsten N Zeilen
        # gehoeren dazu (siehe _playerlist_zeile direkt darunter).
        m_pl = self.P["playerlist_start"].search(line)
        if m_pl:
            self._playerlist_erwartet = int(m_pl.group(1))
            self._playerlist_gesehen = set()
            self._playerlist_pending = {}
            # Auch "0 players" wird erst durch die nachfolgende #####-Zeile
            # bestaetigt; ein nur halb geschriebener Header leert nichts.
            self._playerlist_offen = True
            return None

        # Abschluss-Zeile der Konsole ("23:59:26 | #####", ohne "PlayerList
        # log" davor). Nur exakt N zuvor erkannte Spieler machen den Block
        # autoritativ; andernfalls wird die Teilaufnahme verworfen.
        if self.P["playerlist_end"].match(line):
            if (self._playerlist_offen
                    and len(self._playerlist_pending) == self._playerlist_erwartet):
                self._playerlist_abschliessen()
            else:
                self._playerlist_offen = False
                self._playerlist_erwartet = 0
                self._playerlist_gesehen = set()
                self._playerlist_pending = {}
            return None

        # Laeuft gerade ein Block? Dann gehoert diese Zeile entweder dazu
        # (reine Spieler+Positions-Zeile) oder der unvollstaendige Block wird
        # verworfen und die Zeile danach normal weiter ausgewertet.
        if self._playerlist_offen and self._playerlist_zeile(line):
            return None

        # Positionen immer tracken – Konsolen-Format zuerst (pro Spieler in
        # der eigenen id-Klammer), sonst altes Format als Fallback
        tracked = False
        for pm in self.P["position"].finditer(line):
            self._set_position(pm.group(1), pm.group(2), pm.group(3))
            tracked = True
        if not tracked:
            pm = self.P["position_legacy"].search(line)
            if pm:
                self._set_position(pm.group(1), pm.group(2), pm.group(3))
                # Auch das alte PC-Format zaehlt als getrackt: sonst landete
                # eine reine Positions-Zeile in diesem Format unten als
                # "unerkannt", obwohl sie sauber verarbeitet wurde.
                tracked = True

        # Reihenfolge ist wichtig:
        # 1) Kill
        m = self.P["kill_pvp"].search(line)
        if m:
            pos = m.group(7)
            if pos:
                self._set_position(m.group(1), m.group(2), pos)
            return {
                "type": "kill_pvp",
                "timestamp": ts,
                "victim": m.group(1),
                "victim_id": m.group(2) or "Unbekannt",
                "killer": m.group(3),
                "killer_id": m.group(4) or "Unbekannt",
                "weapon": (m.group(5) or "Unbekannt").strip(),
                "distance": (m.group(6) or "?").strip(),
                "position": pos.strip() if pos else None,
                "raw": line,
            }

        # 2) Suicide
        m = self.P["suicide"].search(line)
        if m:
            return {
                "type": "suicide",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                # Das suicide-Muster faengt kein pos= (Verb allein reicht zum
                # Erkennen) - separat danach suchen, sonst bleibt das Orts-Feld
                # im Embed leer, obwohl die Zeile die Position enthaelt.
                "position": self._pos_aus_zeile(line, m.group(1), m.group(2) or "Unbekannt"),
                "raw": line,
            }

        # 2b) Bewusstsein – VOR dem Umwelttod, sonst verschluckt dessen
        # tolerantes Muster ein "is unconscious" als Todesmeldung.
        for _zustand in ("unconscious", "conscious"):
            m = self.P[_zustand].search(line)
            if m:
                return {
                    "type": _zustand,
                    "timestamp": ts,
                    "player": m.group(1),
                    "player_id": m.group(2) or "Unbekannt",
                    "position": self._pos_aus_zeile(line, m.group(1), m.group(2) or "Unbekannt"),
                    "raw": line,
                }

        # 3) Environment death
        m = self.P["kill_env"].search(line)
        if m and "killed by player" not in line.lower():
            # Gruppe 3 = "by <Ursache>", Gruppe 4 = pos, Gruppe 5 = "due to <Ursache>"
            cause = m.group(3) or m.group(5)
            env_pos = m.group(4)
            if not cause:
                ev = self._generic_env_death_event(line, ts)
                if ev:
                    return ev
                cause = "Unbekannte Ursache"
            if env_pos:
                self._set_position(m.group(1), m.group(2), env_pos)
            return {
                "type": "kill_env",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                "cause": cause.strip() if isinstance(cause, str) else "Unbekannte Ursache",
                "position": env_pos.strip() if env_pos else None,
                "raw": line,
            }

        # 4) Damage
        m = self.P["damage"].search(line)
        if m:
            pos = m.group(9)
            if pos:
                self._set_position(m.group(1), m.group(2), pos)
            return {
                "type": "damage",
                "timestamp": ts,
                "victim": m.group(1),
                "victim_id": m.group(2) or "Unbekannt",
                "attacker": m.group(3),
                "attacker_id": m.group(4) or "Unbekannt",
                "hit_zone": (m.group(5) or "Unbekannt").strip(),
                "damage": (m.group(6) or "?").strip(),
                "weapon": (m.group(7) or "Unbekannt").strip(),
                "distance": (m.group(8) or "?").strip(),
                "position": pos.strip() if pos else None,
                "raw": line,
            }

        # 5) Verbindungen
        m = self.P["connect"].search(line)
        if m:
            pos = m.group(3)
            if pos:
                self._set_position(m.group(1), m.group(2), pos)
            else:
                # Ohne pos= trotzdem ins Tracking – sonst fehlt jeder Spieler
                # in der Online-Liste, dessen Server keine Positions-Zeilen
                # schreibt (siehe _set_gesehen).
                self._set_gesehen(m.group(1), m.group(2))
            return {
                "type": "connect",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                "position": pos.strip() if pos else None,
                "raw": line,
            }

        m = self.P["disconnect"].search(line)
        if m:
            pos = m.group(3)
            if not pos:
                # Das disconnect-Muster faengt nur die alte "at pos=<...>"-Form
                # am Zeilenende - das heutige Konsolen-Format schreibt pos=<...>
                # dagegen INNERHALB der id-Klammer, die PLAYER bewusst ohne den
                # pos-Anteil erfasst (siehe Kommentar bei PLAYER). Rein lesend
                # nachsehen, NICHT ueber _pos_aus_zeile/_set_position: der
                # Cache-Eintrag wird gleich absichtlich geloescht (naechster
                # Kommentar), sonst gaelte der Spieler faelschlich weiter online.
                pos_m = re.search(r'pos\s*=\s*<([\d., \-]+)>', line, re.IGNORECASE)
                pos = pos_m.group(1) if pos_m else None
            # BEWUSST kein _set_position: das wuerde last_seen auf "jetzt"
            # setzen und den Spieler in der Online-Liste (_online_spieler_
            # namen, 15-Minuten-Fenster) bis zu 15 Minuten laenger als
            # online zeigen, obwohl er gerade die Verbindung getrennt hat.
            # Stattdessen wird er sofort aus dem Tracking entfernt.
            self.player_positions.pop(m.group(1), None)
            return {
                "type": "disconnect",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                "position": pos.strip() if pos else None,
                "raw": line,
            }

        m = self.P["connecting"].search(line)
        if m:
            pos = m.group(3)
            if pos:
                self._set_position(m.group(1), m.group(2), pos)
            return {
                "type": "connecting",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                "position": pos.strip() if pos else None,
                "raw": line,
            }

        # 6) Chat
        m = self.P["chat"].search(line)
        if m:
            return {
                "type": "chat",
                "timestamp": ts,
                "channel": m.group(1),
                "player": m.group(2).strip(),
                "message": m.group(3).strip(),
                "raw": line,
            }

        # 6b) Chat im Konsolen-Format: Chat("Name"(id=...)): Nachricht
        m = self.P["chat_console"].search(line)
        if m:
            return {
                "type": "chat",
                "timestamp": ts,
                "channel": "Side",
                "player": m.group(1).strip(),
                "message": m.group(2).strip().strip('"'),
                "raw": line,
            }

        # 7) Admin-Aktion
        m = self.P["admin_action"].search(line)
        if m:
            return {
                "type": "admin_action",
                "timestamp": ts,
                "admin": m.group(1),
                "admin_id": m.group(2) or "Unbekannt",
                "command": m.group(3) or "",
                "raw": line,
            }

        # 8) Vergraben/Ausgraben – eigenes Format, siehe "dig_stash" oben.
        # Vor dem generischen Bau-Muster, weil dessen ehemalige "buried"/
        # "unburied"-Verben in echten Logs nie vorkommen und dieses Muster
        # sonst gar nicht zum Zug kaeme.
        m = self.P["dig_stash"].search(line)
        if m:
            return {
                "type": "basebuild",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                "aktion": "dug in" if m.group(3).lower() == "in" else "dug out",
                "item": m.group(4),
                "position": self._pos_aus_zeile(line, m.group(1),
                                                m.group(2) or "Unbekannt"),
                "raw": line,
            }

        # 8b) Basis-Bau
        m = self.P["basebuild"].search(line)
        if m:
            return {
                "type": "basebuild",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                # Gruppe 3 ist das Verb (PLAYER belegt 1 und 2), Gruppe 4 der
                # Gegenstand. Ohne "aktion" liesse sich Bau nicht aufteilen.
                "aktion": (m.group(3) or "").lower(),
                "item": m.group(4).strip(),
                "position": self._pos_aus_zeile(line, m.group(1),
                                                m.group(2) or "Unbekannt"),
                "raw": line,
            }

        # 8c) Emote
        m = self.P["emote"].search(line)
        if m:
            # pos=<...> steckt in der id-Klammer, die PLAYER bewusst ohne den
            # pos-Anteil erfasst (siehe Kommentar bei PLAYER) - deshalb wie bei
            # Kills/Toden/Treffern extra danach suchen. Ohne das blieb das
            # Positions-Feld im Embed leer, sobald keine ANDERE Zeile desselben
            # Spielers kurz zuvor den Positions-Cache aktuell gehalten hatte.
            pos_m = re.search(r'pos\s*=\s*<([\d., \-]+)>', line, re.IGNORECASE)
            if pos_m:
                self._set_position(m.group(1), m.group(2) or "Unbekannt", pos_m.group(1))
            return {
                "type": "emote",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                "emote": m.group(3),
                "item": (m.group(4) or "").strip() or None,
                "position": pos_m.group(1).strip() if pos_m else None,
                "raw": line,
            }

        # 9) Fahrzeug
        m = self.P["vehicle"].search(line)
        if m:
            return {
                "type": "vehicle",
                "timestamp": ts,
                "raw": line,
            }

        # 10) Loot
        m = self.P["loot"].search(line)
        if m:
            return {
                "type": "loot",
                "timestamp": ts,
                "item": m.group(2),
                "action": m.group(3),
                "raw": line,
            }

        # 11) Fallbacks – garantieren, dass jedes Feed-Ereignis gepostet wird,
        #     auch wenn die Zeile vom erwarteten Muster abweicht
        low = line.lower()

        # Suizid (z.B. mit "(DEAD)" oder Zusatztext zwischen Name und Schlüsselwort)
        if any(kw in low for kw in ("committed suicide", "killed themselves",
                                    "blew themselves up", "ended their life", "suicide")):
            players = self._players_found(line)
            if players:
                return {
                    "type": "suicide",
                    "timestamp": ts,
                    "player": players[0]["name"],
                    "player_id": players[0]["id"] or "Unbekannt",
                    "raw": line,
                }

        # Kill: erst PvP (2 Spieler), sonst Umwelttod (Zombie, Explosion, ...)
        if "killed by" in low:
            ev = self._generic_kill_event(line, ts)
            if ev:
                return ev
            ev = self._generic_env_death_event(line, ts)
            if ev:
                return ev

        # Treffer: erst PvP (2 Spieler), sonst Umwelt (Zombie, FallDamage, ...)
        if "hit by" in low:
            ev = self._generic_damage_event(line, ts)
            if ev:
                return ev
            ev = self._generic_env_damage_event(line, ts)
            if ev:
                return ev

        # Sonstige Todesarten ohne "killed by"
        if any(kw in low for kw in (" died", "bled out", "perished", "starved",
                                    "dehydrated", "drowned", "suffocated", "froze to death")):
            ev = self._generic_env_death_event(line, ts)
            if ev:
                return ev

        # Join/Leave/Connecting – falls das Format erneut abweicht
        if "connect" in low:
            players = self._players_found(line)
            if players:
                p = players[0]
                if "disconnect" in low:
                    ctype = "disconnect"
                elif "connecting" in low:
                    ctype = "connecting"
                elif "connected" in low:
                    ctype = "connect"
                else:
                    ctype = None
                if ctype:
                    pos_m = re.search(r'pos\s*=\s*<([\d., \-]+)>', line, re.IGNORECASE)
                    if ctype == "disconnect":
                        # Siehe Haupt-Disconnect-Zweig oben: kein _set_position,
                        # sonst gilt der Spieler bis zu 15 Minuten faelschlich
                        # weiter als online.
                        self.player_positions.pop(p["name"], None)
                    elif pos_m:
                        self._set_position(p["name"], p["id"], pos_m.group(1))
                    elif ctype == "connect":
                        # Wie im Haupt-Zweig oben: ohne pos= trotzdem als
                        # online vermerken. "connecting" bewusst nicht – der
                        # Verbindungsversuch kann noch scheitern.
                        self._set_gesehen(p["name"], p["id"])
                    return {
                        "type": ctype,
                        "timestamp": ts,
                        "player": p["name"],
                        "player_id": p["id"] or "Unbekannt",
                        "position": pos_m.group(1).strip() if pos_m else None,
                        "raw": line,
                    }

        # Basis-Bau – falls Verb/Format abweicht
        m_build = re.search(r'\b(placed|built|constructed|dismantled|repaired|attached'
                            r'|removed|folded|packed|deployed|mounted|unmounted'
                            r'|buried|unburied|raised|lowered)\s+(.+)$',
                            line, re.IGNORECASE)
        if m_build:
            players = self._players_found(line)
            if players:
                return {
                    "type": "basebuild",
                    "timestamp": ts,
                    "player": players[0]["name"],
                    "player_id": players[0]["id"] or "Unbekannt",
                    "aktion": (m_build.group(1) or "").lower(),
                    "item": m_build.group(2).strip(),
                    "position": self._pos_aus_zeile(line, players[0]["name"],
                                                    players[0]["id"] or "Unbekannt"),
                    "raw": line,
                }

        # Eine reine "Player ... pos=..."-Zeile ohne weitere Aktion – auch
        # ausserhalb eines PlayerList-Blocks (manche Server schreiben sie
        # periodisch einzeln). Die Position ist oben schon getrackt; sie zaehlt
        # als erkannt statt als unerkannte Zeile.
        if tracked and self.P["playerlist_entry"].match(line):
            return None

        # Nichts hat gegriffen: die Roh-Zeile fuer die Diagnose merken statt sie
        # spurlos zu verwerfen. Aus Diskretion nur, wenn ueberhaupt Buchstaben
        # drin sind – reine Trennzeilen o.ae. wuerden nur Rauschen erzeugen.
        if any(c.isalnum() for c in line):
            self.unerkannte_zeilen.append(line[:300])
            self.frisch_unerkannt.append(line[:300])
        return None

    def parse_lines(self, content: str) -> List[Dict]:
        events = []
        for line in content.splitlines():
            if not line.strip():
                continue
            self.zeilen_gelesen += 1
            ev = self.parse_line(line)
            if ev:
                self.zeilen_erkannt += 1
                events.append(ev)
        return events
