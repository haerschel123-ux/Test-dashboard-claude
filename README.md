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
| fehlende Python-Pakete | `discord.py`, `aiohttp`, `requests`, `tzdata` werden per pip nachinstalliert |

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

Dann im Browser öffnen, **Nitrado-Token** eingeben → **Server auswählen** → Karte/FTP werden
automatisch erkannt. Danach sind alle 11 Kategorien freigeschaltet: Übersicht · Feeds ·
Zones · Auto-Aufgaben · Shop · Karte · Bans · Whitelist · Economy · Ankündigungen · Server.

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

### Was **nicht** in die Browserzeile gehört

`0.0.0.0` und `127.0.0.1` sind **lokale** Adressen. Der Bot bindet bewusst an `0.0.0.0`
(alle Interfaces) — eine öffentliche IP lässt sich im Container nicht direkt binden
(„cannot assign requested address"). In der Startmeldung steht `http://127.0.0.1:<Port>`;
das ist der Link für den **lokalen** Betrieb. Läuft der Bot auf PebbleHost, nimm die
Subdomain bzw. die Server-IP mit dem zugewiesenen Port.

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

`/api/health` · `/api/session` · `/api/auth/{token,select-server,logout}` · `/api/feeds` ·
`/api/zones` (+ `/allowlist`) · `/api/guild/{id}/{roles,channels}` · `/api/auto-restart` ·
`/api/shop/{items,categories,classnames}` · `/api/map/{meta,players}` · `/api/events` ·
`/api/events/types` · `/api/bans` · `/api/whitelist` ·
`/api/economy/{balances,money,config}` · `/api/announcements` ·
`/api/server/{status,restart,stop}`

Alle `/api/`-Pfade außer `/api/health`, `/api/session` und `/api/auth/*` brauchen eine
gültige Session und antworten sonst mit `401`.
