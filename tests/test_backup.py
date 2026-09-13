"""Tests fuer das Backup der Server-Dateien (eigener Dashboard-Bereich).

Schwerpunkte: der rekursive Walk ueber den Mission-Ordner, dass sich der
Backup-Ordner nicht selbst mitsichert, dass Binaerdateien byteweise heil
bleiben, und dass ein manipuliertes Archiv nicht ausserhalb des
Mission-Ordners schreiben kann.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/ -v
"""
import io
import os
import sys
import zipfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

MISSION = "/dayzps_missions/dayzOffline.chernarusplus"

# Aufbau wie ein echter Mission-Ordner: mehrere Ebenen, eine Binaerdatei und
# eine Datei ohne Endung (die eine Namens-Heuristik faelschlich fuer einen
# Ordner halten wuerde).
_DATEIEN = {
    f"{MISSION}/cfggameplay.json": b'{"a": 1}',
    f"{MISSION}/init.c": b"void main() {}",
    f"{MISSION}/db/types.xml": b"<types/>",
    f"{MISSION}/db/events.xml": b"<events/>",
    f"{MISSION}/env/zombie_territories.xml": b"<territories/>",
    f"{MISSION}/custom/meins.json": b'{"Objects": []}',
    # Binaer: darf beim Sichern und Zurueckspielen nicht durch eine
    # UTF-8-Kodierung laufen.
    f"{MISSION}/storage_1/data.bin": bytes(range(256)),
    f"{MISSION}/storage_1/players.db": b"\x00\x01\x02binary",
    f"{MISSION}/LIESMICH": b"Datei ohne Endung",
}


class _FakeFTP:
    """Bildet die Teile von FTPManager nach, die das Backup benutzt."""

    def __init__(self, dateien=None, kann_mlsd=True):
        self.dateien = dict(dateien if dateien is not None else _DATEIEN)
        self.kann_mlsd = kann_mlsd
        self.geschrieben = []
        self.geloescht = []
        self.ordner = []

    # ── vom echten FTPManager nachgebildet ──
    def _kinder(self, directory):
        d = directory.rstrip("/")
        namen = {}
        for pfad in self.dateien:
            if not pfad.startswith(d + "/"):
                continue
            rest = pfad[len(d) + 1:]
            kopf = rest.split("/", 1)[0]
            ist_ordner = "/" in rest
            namen[kopf] = (ist_ordner, 0 if ist_ordner else len(self.dateien[pfad]))
        return namen

    def _eintraege_mit_typ(self, directory):
        if not self.kann_mlsd:
            return None
        return [(name, ist_ordner, groesse)
                for name, (ist_ordner, groesse) in self._kinder(directory).items()]

    def list_dir(self, directory):
        d = directory.rstrip("/")
        return [f"{d}/{name}" for name in self._kinder(directory)]

    def _ist_ordner(self, path):
        return any(p.startswith(path.rstrip("/") + "/") for p in self.dateien)

    def read_file_bytes(self, path):
        return self.dateien.get(path)

    def write_file_bytes(self, path, data):
        self.dateien[path] = data
        self.geschrieben.append(path)
        return True

    def mkdir(self, path):
        self.ordner.append(path)
        return True

    def delete_file(self, path):
        self.geloescht.append(path)
        self.dateien.pop(path, None)
        return True

    # walk kommt aus dem echten FTPManager - genau die Logik soll geprueft
    # werden, nicht eine Nachbildung.
    walk = bot.FTPManager.walk


# ── Walk ─────────────────────────────────────────────────────────────────
def test_walk_findet_alle_dateien_ueber_mehrere_ebenen():
    dateien, status = _FakeFTP().walk(MISSION)
    assert status == "ok"
    gefunden = {rel for rel, _ in dateien}
    assert gefunden == {
        "cfggameplay.json", "init.c", "db/types.xml", "db/events.xml",
        "env/zombie_territories.xml", "custom/meins.json",
        "storage_1/data.bin", "storage_1/players.db", "LIESMICH"}


def test_walk_erkennt_datei_ohne_endung_als_datei():
    """Eine Namens-Heuristik („kein Punkt = Ordner") wuerde LIESMICH
    verschlucken."""
    dateien, _ = _FakeFTP().walk(MISSION)
    assert "LIESMICH" in {rel for rel, _ in dateien}


def test_walk_ohne_mlsd_liefert_dasselbe():
    mit, _ = _FakeFTP(kann_mlsd=True).walk(MISSION)
    ohne, _ = _FakeFTP(kann_mlsd=False).walk(MISSION)
    assert {r for r, _ in mit} == {r for r, _ in ohne}


def test_walk_ueberspringt_den_backup_ordner():
    """Sonst wuerde jede neue Sicherung alle vorherigen mitsichern."""
    dateien = dict(_DATEIEN)
    dateien[f"{MISSION}/{bot._BACKUP_ORDNER_NAME}/alt.zip"] = b"PK\x03\x04alt"
    gefunden, _ = _FakeFTP(dateien).walk(
        MISSION, ueberspringen={bot._BACKUP_ORDNER_NAME})
    assert not any(r.startswith(bot._BACKUP_ORDNER_NAME) for r, _ in gefunden)


def test_walk_bricht_bei_zu_vielen_dateien_ab():
    viele = {f"{MISSION}/datei_{i}.xml": b"x" for i in range(20)}
    gefunden, status = _FakeFTP(viele).walk(MISSION, max_dateien=5)
    assert status == "zu_viele_dateien"
    # Ausdruecklich NICHTS zurueckgeben - eine halbe Sicherung waere schlimmer
    # als keine.
    assert gefunden == []


def test_walk_bricht_bei_zu_vielen_bytes_ab():
    gross = {f"{MISSION}/gross.bin": b"x" * 5000}
    gefunden, status = _FakeFTP(gross).walk(MISSION, max_bytes=100)
    assert status == "zu_gross"
    assert gefunden == []


def test_walk_meldet_leeren_ordner():
    assert _FakeFTP({}).walk(MISSION)[1] == "leer"


# ── Backup-Ordner liegt neben dem Mission-Ordner ─────────────────────────
class _FakeConn:
    def __init__(self, mission=MISSION, service_id="1000"):
        self.service_id = service_id
        self.name = "Testserver"
        self.data = {}
        self._werte = {"ftp_mission_dir": mission, "map_name": "ChernarusPlus"}
        self.ftp = _FakeFTP()
        self.api = None

    def get(self, key, default=None):
        return self._werte.get(key, default)

    def set(self, key, value):
        self._werte[key] = value


def test_backup_ordner_liegt_neben_dem_mission_ordner():
    """Laege er darin, wuerde sich jede Sicherung selbst mitsichern."""
    ordner = bot._backup_ordner(_FakeConn())
    assert ordner == f"/dayzps_missions/{bot._BACKUP_ORDNER_NAME}"
    assert not ordner.startswith(MISSION)


def test_ohne_mission_ordner_kein_backup_ordner():
    assert bot._backup_ordner(_FakeConn(mission="")) is None


# ── Pfadpruefung beim Zurueckspielen ─────────────────────────────────────
@pytest.mark.parametrize("pfad", [
    "../ausserhalb.xml", "db/../../weg.xml", "/absolut.xml", "\\windows.xml",
    "C:/laufwerk.xml", "", "./x.xml",
])
def test_ausbrechende_pfade_werden_abgelehnt(pfad):
    assert bot._backup_zielpfad_pruefen(pfad) is False


@pytest.mark.parametrize("pfad", [
    "types.xml", "db/types.xml", "storage_1/data.bin", "env/tief/datei.xml",
])
def test_normale_pfade_sind_erlaubt(pfad):
    assert bot._backup_zielpfad_pruefen(pfad) is True


# ── Aufraeumen ───────────────────────────────────────────────────────────
def _eintrag(nr, umbenannt=False):
    return {"id": f"id{nr:02d}", "name": f"S{nr}", "umbenannt": umbenannt,
            "erstellt": f"2026-09-{nr:02d}T10:00:00", "datei": f"id{nr:02d}.zip"}


def test_aufraeumen_behaelt_die_neuesten_zehn():
    liste = [_eintrag(i) for i in range(1, 15)]
    bleiben, weg = bot._backup_aufraeumen(liste)
    assert len(bleiben) == bot._BACKUP_MAX
    assert len(weg) == 4
    # Die aeltesten fallen weg.
    assert {e["id"] for e in weg} == {"id01", "id02", "id03", "id04"}


def test_benannte_sicherungen_bleiben_auch_als_aelteste():
    """Wer eine Sicherung bewusst benannt hat, soll sie nicht durch
    Weiterarbeiten verlieren."""
    liste = [_eintrag(1, umbenannt=True)] + [_eintrag(i) for i in range(2, 15)]
    bleiben, weg = bot._backup_aufraeumen(liste)
    assert "id01" in {e["id"] for e in bleiben}
    assert "id01" not in {e["id"] for e in weg}


def test_unter_der_grenze_wird_nichts_weggeworfen():
    liste = [_eintrag(i) for i in range(1, 6)]
    bleiben, weg = bot._backup_aufraeumen(liste)
    assert weg == [] and len(bleiben) == 5


# ── Namenspruefung ───────────────────────────────────────────────────────
def test_leerer_name_wird_abgelehnt():
    assert bot._backup_name_pruefen("   ")[1] is not None


def test_zu_langer_name_wird_abgelehnt():
    assert bot._backup_name_pruefen("x" * 61)[1] is not None


def test_steuerzeichen_werden_abgelehnt():
    assert bot._backup_name_pruefen("vor\x00nach")[1] is not None


def test_name_wird_von_ueberfluessigen_leerzeichen_befreit():
    name, fehler = bot._backup_name_pruefen("  Vor   dem   Update  ")
    assert fehler is None and name == "Vor dem Update"


# ── ZIP-Umlauf mit Binaerdaten ───────────────────────────────────────────
def test_binaerdatei_ueberlebt_den_zip_umlauf():
    """Der Kern der Sicherung: write_file (Text) wuerde storage_1/data.bin
    beschaedigen, deshalb laeuft alles ueber Bytes."""
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
        for pfad, roh in _DATEIEN.items():
            z.writestr(pfad[len(MISSION) + 1:], roh)
    archiv = zipfile.ZipFile(io.BytesIO(puffer.getvalue()))
    for pfad, roh in _DATEIEN.items():
        assert archiv.read(pfad[len(MISSION) + 1:]) == roh


# ── Ganzer Durchlauf: sichern und zurueckspielen ─────────────────────────
class _FakeNitrado:
    """Schreibt mit, bei wie vielen Schreibzugriffen gestoppt/gestartet wurde -
    damit sich die Reihenfolge beweisen laesst."""

    def __init__(self, ftp, stoppt=True):
        self.ftp = ftp
        self.stoppt = stoppt
        self.aufrufe = []
        self.laeuft = True

    async def stop(self):
        self.aufrufe.append(("stop", len(self.ftp.geschrieben)))
        if self.stoppt:
            self.laeuft = False
        return True, "ok"

    async def restart(self):
        self.aufrufe.append(("restart", len(self.ftp.geschrieben)))
        self.laeuft = True
        return True, "ok"

    async def get_info(self):
        return {"status": "started" if self.laeuft else "stopped"}


def _durchlauf(conn):
    """Sichern, dann alle Dateien zerstoeren, dann zurueckspielen."""
    import asyncio

    async def lauf():
        bot._BACKUP_JOBS[str(conn.service_id)] = {"art": "erstellen", "fertig": False}
        await bot._backup_erstellen_worker(conn)
        assert not bot._backup_job(conn).get("fehler"), bot._backup_job(conn)
        eintrag = bot._backup_liste(conn)[0]
        # Alles kaputtmachen, was gesichert wurde.
        for pfad in list(conn.ftp.dateien):
            if pfad.startswith(MISSION + "/"):
                conn.ftp.dateien[pfad] = b"KAPUTT"
        bot._BACKUP_JOBS[str(conn.service_id)] = {"art": "wiederherstellen",
                                                  "fertig": False}
        await bot._backup_wiederherstellen_worker(conn, eintrag)
        return eintrag

    return asyncio.run(lauf())


def test_ganzer_durchlauf_stellt_alle_dateien_byteweise_wieder_her():
    conn = _FakeConn()
    conn.api = _FakeNitrado(conn.ftp)
    bot._BACKUP_STOP_INTERVALL = 0
    _durchlauf(conn)
    job = bot._backup_job(conn)
    assert not job.get("fehler"), job
    for pfad, roh in _DATEIEN.items():
        assert conn.ftp.dateien[pfad] == roh, pfad
    bot._BACKUP_JOBS.clear()


def test_beim_zurueckspielen_wird_erst_gestoppt_dann_geschrieben():
    """Schreibt man in den laufenden Server, ueberschreibt DayZ die Dateien
    beim naechsten eigenen Speichern wieder."""
    conn = _FakeConn()
    conn.api = _FakeNitrado(conn.ftp)
    bot._BACKUP_STOP_INTERVALL = 0
    _durchlauf(conn)
    arten = [a for a, _ in conn.api.aufrufe]
    assert arten == ["stop", "restart"]
    stop_bei = conn.api.aufrufe[0][1]
    restart_bei = conn.api.aufrufe[1][1]
    # Beim Stoppen war erst die ZIP-Datei der Sicherung geschrieben.
    assert stop_bei == 1
    # Danach alle Dateien der Sicherung.
    assert restart_bei == 1 + len(_DATEIEN)
    bot._BACKUP_JOBS.clear()


def test_ohne_erfolgreiches_stoppen_wird_nichts_geschrieben():
    """Der gefaehrlichste Fall: der Server laesst sich nicht stoppen. Dann darf
    keine einzige Datei angefasst werden."""
    import asyncio

    conn = _FakeConn()
    conn.api = _FakeNitrado(conn.ftp, stoppt=False)
    bot._BACKUP_STOP_INTERVALL = 0
    bot._BACKUP_STOP_TIMEOUT = 0
    bot._BACKUP_JOBS[str(conn.service_id)] = {"art": "erstellen", "fertig": False}
    asyncio.run(bot._backup_erstellen_worker(conn))
    eintrag = bot._backup_liste(conn)[0]
    vorher = dict(conn.ftp.dateien)
    bot._BACKUP_JOBS[str(conn.service_id)] = {"art": "wiederherstellen",
                                              "fertig": False}
    asyncio.run(bot._backup_wiederherstellen_worker(conn, eintrag))
    assert bot._backup_job(conn).get("fehler")
    assert conn.ftp.dateien == vorher
    assert "restart" not in [a for a, _ in conn.api.aufrufe]
    bot._BACKUP_STOP_TIMEOUT = 120
    bot._BACKUP_JOBS.clear()


def test_sicherung_landet_nicht_im_mission_ordner():
    conn = _FakeConn()
    conn.api = _FakeNitrado(conn.ftp)
    bot._BACKUP_STOP_INTERVALL = 0
    import asyncio
    bot._BACKUP_JOBS[str(conn.service_id)] = {"art": "erstellen", "fertig": False}
    asyncio.run(bot._backup_erstellen_worker(conn))
    zips = [p for p in conn.ftp.geschrieben if p.endswith(".zip")]
    assert len(zips) == 1
    assert not zips[0].startswith(MISSION + "/")
    bot._BACKUP_JOBS.clear()


# ── Trennung der Kunden ──────────────────────────────────────────────────
def test_backups_fallen_nicht_auf_einen_fremden_server_zurueck():
    assert "backups" in bot.ServerConnection._KEINE_RUECKFALL_SCHLUESSEL


def test_job_gilt_je_server_getrennt():
    a, b = _FakeConn(service_id="1000"), _FakeConn(service_id="2000")
    bot._BACKUP_JOBS.clear()
    bot._backup_job_setzen(a, art="erstellen", fertig=False)
    assert bot._backup_job_laeuft(a) is True
    assert bot._backup_job_laeuft(b) is False
    bot._BACKUP_JOBS.clear()
