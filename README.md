# DayZ Nitrado Bot + Web-Dashboard — als **eine** Datei

Ein DayZ-Server-Verwaltungsbot für Discord (Nitrado-Konsolen-Server) **mit integriertem
Web-Dashboard**. Bot und Dashboard laufen in **einem** Prozess (`python bot.py`) und teilen
sich dieselbe Konfiguration und dieselben Live-Daten — alles, was per Slash-Command geht,
geht auch im Browser.

## Das Einzeldatei-Prinzip

Zum Betrieb wird **genau eine Datei** gebraucht: `bot.py`.

```bash
python bot.py
```

Beim ersten Start legt `bot.py` alles selbst an:

| Wird erzeugt | Inhalt |
|---|---|
| `config.json`, `guilds_config.json`, `banlist.json`, `log_state.json`, `whitelist_requests.json` | Konfiguration & Laufzeitdaten |
| `requirements.txt`, `README.txt` | Paketliste & Kurzanleitung |
| `dashboard_web/static/…` | Frontend (`index.html`, `app.js`, `styles.css`, `map.js`), Leaflet und die Ortslisten der drei Karten |
| `dashboard_web/dashboard_cert.pem`, `dashboard_key.pem` | Selbstsigniertes Zertifikat, damit das Dashboard auch `https://` annimmt |
| fehlende Python-Pakete | `discord.py`, `aiohttp`, `requests`, `tzdata` werden per pip nachinstalliert, `cryptography` optional für HTTPS |

Das komplette Frontend, die Leaflet-Bibliothek und die Ortslisten (201/60/60 Orte) stecken
komprimiert (zlib + base64) **in `bot.py`** und werden beim Start herausgeschrieben.

**Eigene Änderungen gehen nie verloren.** Beim Start vergleicht `bot.py` Prüfsummen:

- Datei fehlt → wird angelegt.
- Datei ist noch genau die, die eine frühere `bot.py` abgelegt hat → wird auf die neue
  Fassung **aktualisiert** (so kommen Fehlerbehebungen am Frontend an).
- Datei wurde **von dir** geändert → bleibt unangetastet, mit Hinweis im Log. Willst du doch
  die mitgelieferte Fassung, lösche die Datei und starte neu.

Dafür merkt sich `bot.py` die ausgelieferten Prüfsummen in `dashboard_web/.assets.json`.
Eigene Kartenbilder in `dashboard_web/static/maps/` sind davon gar nicht betroffen — die
werden nie angefasst. Ist das Verzeichnis nicht beschreibbar, liefert das Dashboard die
Assets direkt aus dem Speicher — es läuft in jedem Fall.

`dashboard_web/` und `requirements.txt` sind erzeugte Dateien und stehen deshalb in
`.gitignore`; sie gehören nicht ins Repo.

## Voraussetzungen

- **Python 3.9+**
- Ein **Discord-Bot-Token** (https://discord.com/developers/applications) → in `config.json`
  bei `bot_token` eintragen, dazu die `guild_ids`.
- Ein **Nitrado Long-Life-Token** (Nitrado → Benutzereinstellungen → API-Schlüssel).
  Der wird **nicht** in eine Datei eingetragen, sondern zur Laufzeit im Dashboard
  (Onboarding) oder im Discord per `/setup token` eingegeben. FTP-Zugang und Karte werden
  daraus automatisch erkannt.

## Schnellstart

```bash
python bot.py                  # Bot + Dashboard
python bot.py --dashboard-only # nur das Dashboard, ohne Discord-Login (lokale Vorschau)
```

Dann im Browser öffnen und durch die Schritte gehen (Schritt 0 nur, wenn der
[Discord-Login](#discord-login-und-die-admin-kategorien) eingerichtet ist):

0. **Mit Discord anmelden**
1. **Nitrado-Token** eingeben
2. **Server auswählen** — Karte, FTP-Zugang & Co. werden automatisch erkannt
3. **Discord-Server-ID** eingeben — der Bot registriert daraufhin **sofort** alle
   Slash-Befehle für diesen Server. Ohne diesen Schritt registriert Discord global,
   und das dauert bis zu 24 Stunden. Die ID kommt aus Discord → Einstellungen →
   Erweitert → Entwicklermodus, dann Rechtsklick auf den Server → *Server-ID kopieren*.
   Sie landet in `config.json` unter `guild_ids`; vorhandene Einträge bleiben erhalten.
4. **„Hast du den Bot bereits auf deinem Server?"** — *Nein* öffnet den Einladen-Link
   (die Client-ID stammt von der laufenden Discord-App, nicht fest eingetragen).
   *Ja* prüft nach: Lassen sich die Befehle registrieren, geht es ins Dashboard.
   Ist der Bot noch nicht auf dem Server, sagt das Dashboard es, statt dich mit still
   fehlenden Befehlen weiterzuschicken.

Die Schritte 3 und 4 erscheinen nur, solange in `guild_ids` noch kein echter Discord-Server
steht — wer schon eingerichtet ist, geht direkt ins Dashboard. Danach sind alle 11 Kategorien
freigeschaltet: Übersicht · Feeds · Zones · Auto-Aufgaben · Shop · Karte · Bans · Whitelist ·
Economy · Ankündigungen · Server.

## Dashboard aufrufen

### Auf PebbleHost

Startbefehl im Panel: `python bot.py`

Die Allocation dieses Servers ist **`45.143.198.35:25590`**, die Subdomain zeigt auf dieselbe
IP. Das Dashboard ist deshalb hier erreichbar:

```
http://testdashboard.my.pebble.host:25590
```

Diese Adresse steht beim Start auch direkt im Log:

```
[DASHBOARD] ✅ Dashboard läuft:  http://testdashboard.my.pebble.host:25590
[DASHBOARD]    …oder verschlüsselt: https://testdashboard.my.pebble.host:25590
[DASHBOARD]    (Das Zertifikat ist selbstsigniert – der Browser warnt einmalig: "Erweitert" → "Weiter".)
[DASHBOARD]    (lokal auf diesem Rechner: http://127.0.0.1:25590)
```

Sie kommt aus dem Feld **`dashboard_public_host`** in der `config.json` (Standard:
`testdashboard.my.pebble.host`); die Umgebungsvariable `DASHBOARD_PUBLIC_HOST` hat Vorrang.
Ohne Portangabe wird der tatsächliche Port automatisch angehängt. Trägst du dort einen Port
ein oder eine `https://…`-Adresse, wird der Wert unverändert übernommen — so stimmt der Link
auch hinter einem Reverse-Proxy. Das Feld beeinflusst **nur die Anzeige**; gebunden wird
weiterhin an `dashboard_host` (`0.0.0.0`).

**Der Port gehört dazu.** Die nackte Domain ohne Port funktioniert **nicht**: Port 80 auf
`45.143.198.35` wird bereits von PebbleHosts eigenem nginx bedient (dort erscheint die Seite
„This server is powered by PebbleHost!"). Das lässt sich nicht im Bot ändern — der Bot kann
nur den Port belegen, den ihm der Host zuweist (`SERVER_PORT`), und das ist `25590`, nicht 80.

Wenn du die Adresse **ohne** Port brauchst, gibt es genau zwei Wege:

1. **PebbleHost-Support** nach einer Allocation auf Port 80/443 für diesen Server fragen bzw.
   nach einem HTTP-Proxy für die Subdomain. Bekommst du eine, zeigt die nackte Domain direkt
   aufs Dashboard.
2. **Reverse-Proxy davorsetzen**, z. B. ein Cloudflare Tunnel auf `127.0.0.1:25590`. Dann
   trägst du in `dashboard_public_host` die dortige `https://…`-Adresse ein und rufst das
   Dashboard ohne Port auf.

Ändert sich die Allocation, steht der aktuelle Port im Panel unter **Network / Allocations**.
Trage ihn **nicht** als `dashboard_port` in `config.json` ein — PebbleHost setzt
`SERVER_PORT`, und die wird automatisch und mit Vorrang benutzt. So bleibt die Konfiguration
portunabhängig.

Der Web-Port wird in dieser Reihenfolge bestimmt:
**`SERVER_PORT` → `PORT` → `config.json` (`dashboard_port`) → `8080`**

### `http://` oder `https://`?

**Beides funktioniert – auf demselben Port.** Das ist kein Komfort-Detail, sondern
behebt einen Fehler: Browser stufen getippte Adressen zunehmend von selbst auf
`https://` hoch. Traf so ein TLS-Handshake früher auf das Dashboard, das nur
Klartext-HTTP sprach, antwortete es mit HTTP-Text — der Browser konnte daraus kein
Zertifikat lesen und brach ab:

```
Diese Website kann keine sichere Verbindung bereitstellen
testdashboard.my.pebble.host hat eine ungültige Antwort gesendet.
ERR_SSL_PROTOCOL_ERROR
```

Die Seite war damit schlicht nicht erreichbar, obwohl der Server lief — und zwar
sporadisch: über ein Lesezeichen mit `http://` ging es, neu getippt nicht.

Jetzt entscheidet der Server **pro Verbindung**. Das erste Byte eines
TLS-ClientHello ist immer `0x16`; steht es an, wird die Verbindung mit TLS
bedient, sonst wie bisher als Klartext-HTTP. Gelesen wird das Byte mit `MSG_PEEK`,
es bleibt also im Puffer und ist danach noch Teil des Handshakes. Ein zweiter Port
kommt nicht in Frage — PebbleHost weist genau einen zu.

Das Zertifikat erzeugt `bot.py` beim Start selbst (`dashboard_web/dashboard_cert.pem`
und `dashboard_key.pem`, beide gitignored, der Schlüssel mit `0600`). Es gilt für
`dashboard_public_host`, `server_ip`, `localhost` und `127.0.0.1`, läuft nach
825 Tagen ab und wird 30 Tage vorher — oder wenn sich die Adresse ändert —
automatisch erneuert.

Weil es **selbstsigniert** ist, warnt der Browser bei `https://` einmalig
(„Erweitert" → „Weiter zur Seite"). Das ist der Unterschied zu vorher: aus einer
Sackgasse wird ein Klick. Willst du die Warnung ganz los, brauchst du ein echtes
Zertifikat — dafür ist der Cloudflare Tunnel weiter unten der Weg.

| Aufruf | Ergebnis |
|---|---|
| `http://…:25590` | lädt direkt, unverändert wie bisher |
| `https://…:25590` | lädt nach einmaliger Zertifikatswarnung, Verbindung verschlüsselt |
| `https://…` ohne Port | geht nicht — Port 80/443 gehören PebbleHosts nginx |

Abschalten lässt sich das mit `"dashboard_https": false` in der `config.json`; dann
läuft alles wie früher über reines HTTP. Fehlt das Paket `cryptography` (es wird
beim Start automatisch nachinstalliert, ein Fehlschlag ist nicht schlimm) oder ist
`dashboard_web/` nicht beschreibbar, passiert dasselbe — mit einem Hinweis im Log.
Der Bot startet in jedem Fall.

### Was **nicht** in die Browserzeile gehört

`0.0.0.0` und `127.0.0.1` sind **lokale** Adressen. Der Bot bindet bewusst an `0.0.0.0`
(alle Interfaces) — eine öffentliche IP lässt sich im Container nicht direkt binden
(„cannot assign requested address"). In der Startmeldung steht `http://127.0.0.1:<Port>`;
das ist der Link für den **lokalen** Betrieb. Läuft der Bot auf PebbleHost, nimm die
Subdomain bzw. die Server-IP mit dem zugewiesenen Port.

## Discord-Login und die Admin-Kategorien

Optional lässt sich dem Dashboard eine **Discord-Anmeldung** voranstellen: Erst nach dem
Login kommt man überhaupt zum Nitrado-Token. Zusätzlich schaltet eine Discord-Rolle zwei
weitere Kategorien frei.

### Einrichten

Solange kein `discord_client_secret` in der `config.json` steht, ist der Login **aus** und
das Dashboard verhält sich exakt wie vorher — ein Update sperrt also keine laufende
Installation aus. Zum Aktivieren im
[Discord Developer Portal](https://discord.com/developers/applications) → deine App → OAuth2:

1. **Client Secret** kopieren → `discord_client_secret` in der `config.json`
2. Unter **Redirects** die Rücksprungadresse eintragen:
   `http://deine-adresse:PORT/api/auth/discord/callback`
   Rufst du das Dashboard auch über `https://` auf, trage **beide** Varianten ein — die
   Adresse wird aus dem tatsächlichen Aufruf gebildet und muss exakt passen.

Nach dem Login steht oben rechts in der Kopfzeile das Discord-Profilbild, darunter der Name
und darunter der Abmelden-Button. Lädt das Bild nicht (kein Zugriff auf Discords CDN im
Browser), bleibt nur der Name stehen.

Abgefragt wird nur der Scope `identify` — also wer du bist. Der Zugriffstoken bleibt auf dem
Server und erreicht den Browser nie. Welche Rollen jemand hat, fragt der Bot selbst über die
verbundenen Guilds ab (`fetch_member`, ein REST-Aufruf ohne privilegierte Intent).

### Die zwei Kategorien

Wer die Rolle aus `dashboard_admin_role_id` (Standard: `1530653925575753838`) in einem der
verbundenen Discord-Server hat, sieht zusätzlich:

| Kategorie | Inhalt |
|---|---|
| **📜 Logs** | Wer hat was mit dem Bot gemacht: Slash-Befehle im Discord (mit Argumenten und Server) und ändernde Zugriffe im Dashboard. Die letzten 500 Einträge, in `bot_audit.json` (gitignored). |
| **🆔 Guild IDs** | Alle verbundenen Discord-Server mit Namen, Mitgliederzahl und ob der Bot dort tatsächlich drauf ist — plus Button zum Hinzufügen weiterer IDs. |

Ohne die Rolle sind beide Kategorien nicht nur ausgeblendet: `/api/audit` und
`/api/admin/guilds` antworten mit `403`. Wer die Rolle in Discord verliert, verliert den
Zugriff beim nächsten Login — die Rolle wird beim Anmelden geprüft, nicht bei jedem Klick.

Protokolliert werden nur **ändernde** Zugriffe (POST/PUT/DELETE), aufgezeichnet werden
Methode, Pfad und Ergebnis — nie der Inhalt der Anfrage, in dem sonst der Nitrado-Token
stünde.

## Mehrere Nitrado-Server (Premium-Zuordnung)

Der Bot kann mehrere Nitrado-Server bedienen. Jeder verbundene Server steht in
`connections.json` (gitignored, enthält Tokens) und wird über die Dashboard-Kategorie
**Serverliste** genau einer Discord-Guild zugeordnet — das ist die Freischaltung.

| Kategorie | Wer sieht sie | Inhalt |
|---|---|---|
| **🗄️ Serverliste** | nur Admin-Rolle | Alle verbundenen Nitrado-Server mit Name, Service-ID, Karte und zugeordneter Guild. Der Stift-Button setzt die Guild-ID; beim Speichern werden die Slash-Befehle sofort dort registriert. Leer lassen entfernt die Zuordnung. |
| **⚙️ Optionen** | jeder angemeldete Nutzer | Der eigene Nitrado-Token maskiert (`••••••••1111`), Auge-Button zum Einblenden, Stift zum Ändern. Darunter die zugeordnete Guild — oder der Hinweis, dass der Betreiber noch freischalten muss. |

Eine Guild kann immer nur **einen** Nitrado-Server verwalten; der Versuch, sie ein zweites
Mal zu vergeben, wird mit Nennung des bisherigen Servers abgelehnt.

### Was pro Server getrennt läuft

Die Trennung ist vollständig — jeder verbundene Server hat eigene:

| | |
|---|---|
| Nitrado-Verbindung und FTP-Sitzung | `/neustart`, `/stoppen`, `/serverstatus`, `/ban`, `/whitelist` wirken auf den Server der aufrufenden Guild |
| Log-Abfrage, Lese-Position und Parser | Kills, Builds und Positionen aus Server A erscheinen **nur** im Discord von Server A |
| Shop-Auslieferung (`cfgEffectArea.json`) | Käufe landen in der Mission des jeweiligen Servers |
| Zonen, Karte, Server-IP und Query-Port | pro Server gepflegt, nicht global |

Die Feeds folgen der Zuordnung: Ein Ereignis geht ausschließlich in die Guild, der sein
Server zugeordnet ist. Economy und Feed-Channels waren schon vorher pro Guild getrennt.

Im Dashboard richtet sich alles nach der Anmeldung: Wer sich mit seinem Nitrado-Token
anmeldet, sieht Status, Karte, Zonen und Steuerung **seines** Servers.

### „Du hast kein Premium"

Ist ein Discord-Server keinem Nitrado-Server zugeordnet, antwortet **jeder** Slash-Befehl
dort mit dieser Meldung. Ausgenommen sind nur `/setup` und `/hilfe` — sonst käme man aus der
Sperre nicht mehr heraus. Die Prüfung hängt als `interaction_check` am gesamten
Befehlsbaum, greift also auch bei später hinzugefügten Befehlen.

Beim Ändern des Tokens in den Optionen wird geprüft, ob der neue Token denselben Server
kennt — ein Token vom falschen Nitrado-Konto wird abgelehnt, statt die Verbindung zu
zerstören. Danach richtet sich der Bot sofort neu ein, ohne Neustart.

**Bestehende Installationen** werden beim ersten Start automatisch übernommen: Aus der
`config.json` entsteht eine Verbindung, die der ersten echten Guild-ID zugeordnet wird —
inklusive der bisherigen Log-Position, damit keine alten Ereignisse erneut gepostet werden.

## Die Karte

Die Karte zeigt automatisch die erkannte Karte des Servers (Chernarus, Livonia oder Sakhal)
mit Live-Spielerpositionen, den letzten Events als Marker und einem ein-/ausklappbaren
Filter-Panel (jeder der 14 Event-Typen einzeln an/aus). Zonen lassen sich direkt auf der
Karte zeichnen (Kreis ziehen → x/z/Radius werden übernommen).

Die **Kartenkacheln lädt der Browser** direkt von `static.xam.nu`, nicht der Server.
Dreistufige Fallback-Kette, damit die Karte nie leer ist:

1. echte Kacheln (bzw. eine eigene Kachel-URL je Karte in `config.json` →
   `dashboard_map_tiles`),
2. ein eigenes Bild unter `dashboard_web/static/maps/<Karte>.jpg` — ein quadratisches
   Top-Down-Bild passt am besten; es wird über die gesamte Weltgröße gelegt
   (Nordwesten = oben links). Weltgrößen: Chernarus 15360 m, Livonia 12800 m, Sakhal 15360 m,
3. eine schematische Karte mit Gitter und Ortsnamen.

Leaflet liegt lokal (eingebettet, kein CDN) — die Oberfläche funktioniert also auch offline;
nur die Kacheln brauchen Internet im Browser.

> `.ADM`-Logs enthalten keine Koordinaten für Heli- und Zug-Crashes. Diese Event-Typen sind
> als Filter vorhanden, Marker erscheinen aber nur, wenn Koordinaten ableitbar sind.

## Sicherheit

- **Das Zugangs-Gate ist allein der Nitrado-Token** — es gibt bewusst kein zweites Passwort.
  Ein gültiger Token erzeugt eine **serverseitige** Session; der Browser bekommt nur eine
  zufällige Session-ID als Cookie (`dz_sess`, httponly, 12 h). Der Token selbst verlässt den
  Server nie.
- **Über `http://` geht der Token im Klartext durchs Netz.** Rufst du das Dashboard über
  `https://` auf (derselbe Port, siehe oben), ist die Verbindung verschlüsselt — gegen
  Mitlesen unterwegs. Ein selbstsigniertes Zertifikat schützt allerdings nicht gegen einen
  Angreifer, der sich aktiv dazwischenschaltet; dagegen hilft nur ein echtes Zertifikat
  (Cloudflare Tunnel).
- **Das Dashboard kann den Server neu starten und stoppen.** Halte die URL (samt Port)
  deshalb **privat**.
- **Niemals committen:** `config.json`, Nitrado-Token, Discord-Bot-Token, FTP-Zugangsdaten,
  `economy.db`. Die mitgelieferte `.gitignore` deckt das ab.

## Funktionsumfang des Bots

- **16 Feed-Typen:** killfeed, damagefeed, joinleave, suicide, chat, adminlog, envdeath,
  vehiclecrash, basebuild, loot, connecting, shop_log, economy_log, status, restart, zone
- **Setup/Nitrado:** `/setup token`, `/setup feeds`, `/show_feeds`, `/ftp_scan`,
  `/ftp_status`, `/log_status`, `/raw_log`
- **Zonen:** `/zone create|edit|remove|list` — überwachte Gebiete mit Rollen-Ping (inkl. Allowlist)
- **Automatik:** `/auto restart|off|status` — geplante Neustarts mit Vorankündigungen
- **Shop & Lieferung:** `/shop list|setprice|enable|removeitem`, `/add shopitem`, `/buy` —
  Items und Bundles, Katalog aus `types.xml` generierbar (~1700 Items)
- **Economy & Casino:** `/balance`, `/daily`, `/work`, `/beg`, `/pay`, `/deposit`,
  `/withdraw`, `/leaderboard`, `/blackjack`, `/roulette`, `/slots`, `/bounty`,
  Admin: `/addmoney`, `/removemoney`, `/setbalance`
- **Spielerverwaltung:** `/link`, `/unlink`, `/forcelink`, `/spieler_suche`, `/stats`,
  `/admin_position`, Whitelist-Panel mit Freigabe-Buttons
- **Moderation:** `/ban`, `/ban_entfernen`, `/banlist`, `/hackban`
- **Server-Steuerung:** `/neustart`, `/stoppen`, `/serverstatus` (A2S + Nitrado-API)
- **Ankündigungen:** `/erstellen`, `/liste`, `/löschen` — wiederkehrend (wöchentlich bis monatlich)

## Architektur

Ein **einziger Python-Prozess**. Das Dashboard ist eine aiohttp-Web-App im **selben
Event-Loop** wie der Discord-Bot und greift **direkt** auf dessen Live-Objekte zu (`cfg`,
`db`, `catalog`, `bot.nitrado`, `bot.ftp`, `bot.parser.player_positions`). Es gibt keinen
zweiten Prozess und keine doppelte Datenhaltung — **jede Änderung im Dashboard wirkt sofort
im laufenden Bot**, ohne Neustart.

### Dashboard-API

`/api/health` · `/api/session` · `/api/auth/{token,select-server,guild,logout}` ·
`/api/auth/discord/{start,callback}` · `/api/audit` · `/api/admin/guilds` · `/api/feeds` ·
`/api/zones` (+ `/allowlist`) · `/api/guild/{id}/{roles,channels}` · `/api/auto-restart` ·
`/api/shop/{items,categories,classnames}` · `/api/map/{meta,players}` · `/api/events` ·
`/api/events/types` · `/api/bans` · `/api/whitelist` ·
`/api/economy/{balances,money,config}` · `/api/announcements` ·
`/api/server/{status,restart,stop}`

Alle `/api/`-Pfade außer `/api/health`, `/api/session` und `/api/auth/*` brauchen eine
gültige Session und antworten sonst mit `401`.
