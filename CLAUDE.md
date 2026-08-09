# Regeln für Claude

## Ansprache

Der Nutzer heißt **Brigarde**.

**Jede Abschluss-Zusammenfassung beginnt mit seinem Namen** – im Chat wie bei Code-Änderungen.
Das ist das Zeichen dafür, dass der Zusammenhang noch bekannt ist und nicht geraten wird.

## Vor jedem Commit

1. **Erst zeigen, was geändert wird.** Die Änderungen darstellen, bevor irgendetwas committet
   oder gepusht wird.
2. **Bei Funktionen mit Oberfläche zusätzlich einen Screenshot** der laufenden Funktion zeigen.
3. **Bestätigung abwarten.** Erst nach Brigardes ausdrücklichem „commit und push" wird
   committet und gepusht.

Sagt Brigarde „mach weiter" oder „mach einfach weiter", ist das **keine** Dauerfreigabe zum
Pushen – es bezieht sich auf die Arbeit, nicht auf das Veröffentlichen.

## Arbeitsweise

* **Bei Unklarheiten fragen** statt zu raten oder anzunehmen.
* **Agents zur Prüfung nur, wenn Brigarde es ausdrücklich verlangt.** Nicht von selbst
  vorschlagen oder starten, auch nicht bei sicherheitsrelevanten Änderungen.
* Behauptungen belegen: mit echten Testläufen, nicht durch Lesen des Codes. Was nicht
  ausgeführt wurde, gilt als ungeprüft.
* Fehler offen benennen, auch eigene.
* **So token-sparend wie möglich arbeiten, ohne an Gründlichkeit zu verlieren.** Keine
  überflüssigen Zwischenschritte, keine doppelten Testläufe, keine Erklärungen, die
  länger sind als nötig. **Ausgenommen: Screenshots bleiben Pflicht bei großen
  Änderungen an der Oberfläche (siehe „Vor jedem Commit") und immer, wenn Brigarde
  danach fragt** – daran spart nicht.

## Git

* Repo: `https://github.com/haerschel123-ux/Test-dashboard-claude`
* **Standard-Ziel für „commit und push": `claude/new-session-we1my2`.**
  Dorthin wird committet und gepusht, solange Brigarde nichts anderes sagt.
* Kein Pull Request ohne ausdrückliche Bitte.
* Ein anderer Branch nur auf ausdrückliche Nennung – dann gilt er für diese
  eine Aufgabe, nicht als neuer Standard.

---

# Das Projekt

Ein DayZ-Nitrado-Discord-Bot mit eingebettetem Web-Dashboard – **eine einzige Datei**
(`bot.py`, ca. 14.000 Zeilen, discord.py + aiohttp in einem Prozess). Läuft auf PebbleHost
unter `testdashboard.my.pebble.host:25590`.

Das Frontend (`index.html`, `app.js`, `styles.css`, `map.js`, Leaflet) steckt zlib+base64
in `_EMBEDDED_ASSETS` in derselben Datei und wird beim Start nach `dashboard_web/static/`
entpackt. Änderungen daran müssen wieder eingebettet werden, und die Prüfsumme der
Vorgängerfassung gehört in `_ASSET_KNOWN_HASHES` – sonst erreicht das Update bestehende
Installationen nicht.

## Das Ziel: Mehrkundenbetrieb

Aus dem Einzelserver-Bot ist einer geworden, den Brigarde für mehrere Kunden betreibt:

* Jeder Kunde verbindet seinen eigenen Nitrado-Server (`ServerConnection` in
  `connections.json`, Schlüssel = `service_id`).
* **Brigarde** ordnet ihm im Dashboard unter *Serverliste* eine Discord-Guild zu – das ist
  die Freischaltung. Kunden können sich **nicht** selbst freischalten; ihre Guild-ID wird
  nur als `guild_id_requested` vorgemerkt.
* Ohne Zuordnung antwortet jeder Slash-Befehl mit „du hast kein Premium" (`_premium_check`).
* Alles ist getrennt: Shop-Katalog, Kill-Statistik, Spielzeit, Ankündigungen, Neustart-Plan,
  Währung, Zonen, Zugangsdaten.

## Wie Code den richtigen Server findet

| Seite | Helfer |
|---|---|
| Discord-Befehle | `_conn_of(interaction)`, `_require_conn(...)`, `_require_catalog(...)` |
| Dashboard | `_session_conn(request)`, `_session_guilds(request)` |

`ServerConnection.get()` hat drei Stufen: Zugangsdaten und Serverkennungen haben **keine**
Rückfallebene (`_KEINE_RUECKFALL_SCHLUESSEL`), eigene Einstellungen fallen auf die
Auslieferungs-Vorgabe zurück (`_EIGENE_EINSTELLUNGEN`), alles Übrige auf `cfg.config`.

**Nie** `connections.primary()`, `bot.nitrado`, `bot.ftp` oder `cfg.config` benutzen, wo es
um einen bestimmten Kundenserver geht – genau daraus sind sämtliche Sicherheitslücken
entstanden, die in dieser Codebasis gefunden wurden.

## Vor jedem Commit prüfen

```bash
python3 -m pyflakes bot.py | grep -i undefined     # muss leer sein
python3 -m pylint --disable=all --enable=E bot.py  # muss 10.00 sein
```

`pyflakes` findet die Fehlerklasse, die hier zweimal zugeschlagen hat: eine Umbenennung
trifft eine Stelle, an der die lokale Variable anders heißt (`conn` statt `_conn`) – der
Befehl stürzt dann erst zur Laufzeit ab.

**Niemals per Regex oder Skript über mehrere Stellen ersetzen.** Das hat mehrfach
mehrzeilige Signaturen zerstört und Text in Nutzermeldungen verändert. Immer exakte,
einzelne Ersetzungen mit genug Kontext.

## Testen

Tests laufen als echte Prozesse in Wegwerf-Kopien: `bot.py` in einen Ordner kopieren,
`config.json` und `connections.json` daneben, `sys.path.insert` + `import bot` +
`cfg.load_all()`. Nitrado-API und FTP werden gestubbt, ein Discord-Login ist nicht nötig.
Für die Oberfläche Playwright mit dem vorinstallierten Chromium unter
`/opt/pw-browsers/`, mit `--no-sandbox`, bei 1280 px und 412 px.

**`connections.json` ist eine flache Zuordnung** `{"1000": {...}, "2000": {...}}` – nicht
verschachtelt. Ein falsches Testformat hat hier schon einmal eine Prüfung wertlos gemacht,
weil unbemerkt ein ganz anderer Code-Pfad lief.
