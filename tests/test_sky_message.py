"""Tests fuer den Sky Message Generator (Object-Spawner-Dateien im Himmel).

Die Geometrie ist gegen echte Ausgaben der Vorlage doordiehub.com/SkyMessenger
geprueft; die bewusst korrigierten Abweichungen sind hier einzeln festgehalten,
damit eine spaetere Aenderung auffaellt.

Aufruf aus dem Repository-Wurzelverzeichnis:

    python3 -m pytest tests/ -v
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")


def _bauen(text, muster="compact", groesse="small", x=7500.0, z=7500.0,
           hoehe=500.0, scale=1.0, yaw=0.0, pitch=0.0, roll=0.0, winkel=0.0,
           classname="M18SmokeGrenade_White", wand=None):
    return bot._sky_objekte_bauen(text, muster, groesse, x, z, hoehe, scale,
                                  yaw, pitch, roll, winkel, classname, wand)


# ── Textbereinigung ──────────────────────────────────────────────────────
def test_kleinbuchstaben_werden_gross():
    assert bot._sky_text_saeubern("survive")[0] == "SURVIVE"


def test_scharfes_s_wird_zu_doppel_s():
    # str.upper() macht aus "ß" zwei "S" - wie in der Vorlage.
    assert bot._sky_text_saeubern("straße")[0] == "STRASSE"


def test_umlaute_und_sonderzeichen_werden_gemeldet():
    text, entfernt = bot._sky_text_saeubern("ab äöüß @?: 1")
    assert text == "AB SS  1"
    # Die Vorlage entfernt kommentarlos; hier wird gemeldet, WAS wegfiel.
    assert entfernt == ["Ä", "Ö", "Ü", "@", "?", ":"]


def test_fragezeichen_erzeugt_keine_luecke():
    assert bot._sky_text_saeubern("A?B")[0] == "AB"


# ── Geometrie gegen die Vorlage ──────────────────────────────────────────
def test_einzelner_punkt_sitzt_auf_dem_mittelpunkt():
    # Belegtes Originalbeispiel: "." in Compact/Small bei 7500/500/7500.
    objekte, _, _, _ = _bauen(".")
    assert len(objekte) == 1
    assert objekte[0]["pos"] == [7500, 498, 7500]


def test_ganze_zahlen_ohne_nachkommastelle():
    # Die Vorlage laeuft in JavaScript und schreibt 7500 statt 7500.0.
    objekte, _, _, _ = _bauen(".")
    roh = json.dumps({"Objects": objekte})
    assert "7500.0" not in roh and "7500" in roh


def test_textwinkel_dreht_um_die_hochachse():
    # Bei 90 Grad laeuft der Text entlang Z statt entlang X.
    objekte, _, _, _ = _bauen("..", winkel=90.0)
    assert [o["pos"][0] for o in objekte] == [7500, 7500]
    assert [o["pos"][2] for o in objekte] == [7498, 7502]


def test_wandmodus_rechnet_auf_die_wandflaeche():
    objekte, abstand, breite, zeilen = _bauen("..", wand=(5.0, 8.0))
    assert abstand == pytest.approx(min(5.0 / breite, 8.0 / zeilen))
    assert len(objekte) == 2


def test_buchstabengroesse_bestimmt_den_punktabstand():
    _, klein, _, _ = _bauen("A", groesse="small")
    _, gross, _, _ = _bauen("A", groesse="huge")
    assert klein == pytest.approx(5.0 / 5)
    assert gross == pytest.approx(40.0 / 5)


def test_objektgroesse_aendert_nur_scale_nicht_die_position():
    ohne, _, _, _ = _bauen("A", scale=1.0)
    mit, _, _, _ = _bauen("A", scale=0.25)
    assert [o["pos"] for o in ohne] == [o["pos"] for o in mit]
    assert all(o["scale"] == 0.25 for o in mit)


# ── Bewusst korrigierte Fehler der Vorlage ───────────────────────────────
def test_ypr_reihenfolge_ist_yaw_pitch_roll():
    # Die Vorlage schreibt [Pitch, Yaw, Roll] und dreht die Objekte damit
    # falsch. Korrigiert auf die Reihenfolge des offiziellen Formats.
    objekte, _, _, _ = _bauen(".", yaw=10.0, pitch=20.0, roll=30.0)
    assert objekte[0]["ypr"] == [10, 20, 30]


def test_raute_wird_ueber_ihre_echte_breite_zentriert():
    # "#" ist im Compact-Muster vier statt drei Spalten breit. Die Vorlage
    # rechnet trotzdem mit drei und setzt das Zeichen dadurch versetzt.
    objekte, abstand, breite, _ = _bauen("#")
    assert breite == 4
    xs = [o["pos"][0] for o in objekte]
    # Die Glyphe reicht von Spalte 0 bis 3 und liegt damit mittig auf 7500.
    # Die Vorlage schoebe sie um eine halbe Spalte nach rechts.
    assert min(xs) == pytest.approx(7500 - 1.5 * abstand)
    assert max(xs) == pytest.approx(7500 + 1.5 * abstand)
    assert (min(xs) + max(xs)) / 2 == pytest.approx(7500)


def test_nachfolgendes_zeichen_ruecht_um_die_echte_breite_nach():
    # Bei "#." liegt der Punkt in der Vorlage zu weit links, weil der
    # Vorschub die vierte Spalte ignoriert.
    objekte, _, _, _ = _bauen("#.")
    assert objekte[-1]["pos"] == [7502.5, 498, 7500]


def test_komma_hat_in_jedem_muster_eine_glyphe():
    # Die Vorlage nennt das Komma als unterstuetzt, liefert aber in allen
    # drei Mustern ein leeres Raster.
    for muster in ("compact", "standard", "detailed"):
        objekte, _, _, _ = _bauen(",", muster=muster)
        assert objekte, f"Komma ist im Muster {muster} leer"


# ── Dateiname und Serialisierung ─────────────────────────────────────────
def test_dateiname_liegt_im_custom_ordner():
    assert bot._sky_dateiname("SURVIVE") == "custom/skymessage_survive.json"


def test_dateiname_faengt_leeren_text_ab():
    assert bot._sky_dateiname("!!!") == "custom/skymessage_nachricht.json"


def test_objekte_haben_das_object_spawner_format():
    objekte, _, _, _ = _bauen("A")
    for o in objekte:
        assert list(o) == ["name", "pos", "ypr", "scale",
                           "enableCEPersistency", "customString"]
        assert o["enableCEPersistency"] == 0
        assert o["customString"] == ""
        assert len(o["pos"]) == 3


def test_alle_muster_kennen_dieselben_zeichen():
    zeichen = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ./!#-_=,")
    for _, _, schrift in bot._SKY_MUSTER.values():
        assert set(schrift) == zeichen


def test_jede_schrift_hat_gleich_viele_zeilen_je_zeichen():
    for spalten, zeilen, schrift in bot._SKY_MUSTER.values():
        for zeichen, raster in schrift.items():
            teile = raster.split("/")
            assert len(teile) == zeilen, f"{zeichen}: {len(teile)} statt {zeilen}"
            breiten = {len(t) for t in teile}
            assert len(breiten) == 1, f"{zeichen}: uneinheitliche Breite"
            # "#" darf im Compact-Muster breiter sein als der Nennwert.
            assert breiten.pop() >= spalten or zeichen == "#"


def _item_katalog():
    import base64
    import zlib

    import embedded_assets

    roh = zlib.decompress(base64.b64decode(
        embedded_assets._EMBEDDED_ASSETS["loadout-catalog.js"].replace("\n", "")))
    return roh.decode("utf8").lower()


def test_inventar_objekte_stehen_im_item_katalog():
    """Die Inventar-Gegenstaende muessen es wirklich geben - ein erfundener
    Classname erzeugt eine gueltige Datei, im Spiel erscheint aber nichts.

    Statische Objekte (Container, Wracks, Militaer, Steine, Feuer) stehen
    naturgemaess nicht im Loadout-Katalog; sie sind gegen echte Class-Dumps
    des Spiels geprueft, siehe Kommentar an _SKY_OBJEKTE.
    """
    katalog = _item_katalog()
    inventar = [c for _, c, _ in bot._SKY_OBJEKTE
                if not c.startswith(("StaticObj_", "Land_", "BarrelHoles_"))
                and c not in ("Bonfire", "Fireplace", "FireplaceIndoor")]
    fehlend = [c for c in inventar if f'"{c.lower()}"' not in katalog]
    assert fehlend == [], fehlend
    assert len(inventar) >= 19


def test_kein_classname_ist_ein_modellpfad():
    """Die Vorlage bietet zwei Steine als "rock_apart1.p3d" an - das ist ein
    Modellpfad, kein Classname, und der Object Spawner kann damit nichts
    anfangen. So etwas darf nicht in die Auswahl geraten."""
    for _, classname, _ in bot._SKY_OBJEKTE:
        assert not classname.endswith(".p3d"), classname


def test_alle_classnames_passieren_die_eingabepruefung():
    """Die Auswahl muss durch dieselbe Pruefung kommen wie eine freie
    Eingabe - sonst bietet das Dashboard etwas an, das der eigene Handler
    anschliessend ablehnt."""
    import re as _re
    for _, classname, _ in bot._SKY_OBJEKTE:
        assert _re.fullmatch(r"[A-Za-z0-9_]{1,64}", classname), classname


def test_objektliste_ist_eindeutig():
    namen = [c for _, c, _ in bot._SKY_OBJEKTE]
    assert len(namen) == len(set(namen))
    label = [l for _, _, l in bot._SKY_OBJEKTE]
    assert len(label) == len(set(label))
