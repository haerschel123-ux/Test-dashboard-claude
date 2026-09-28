"""Day/Night Config: Registrierung als Tool und die Rechenformel in app.js.

Die Formel laeuft rein clientseitig - geprueft wird sie hier ueber node mit den
Werten, die das Referenz-Tool (doordiehub.com/DayNightCalculator) liefert.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

bot = pytest.importorskip("bot", reason="bot.py-Abhaengigkeiten nicht installiert")

APP_JS = os.path.join(REPO, "dashboard_web", "static", "app.js")


def test_tool_ist_registriert():
    assert ("daynight", "🌗", "Day/Night Config") in bot._TOOL_LISTE
    assert "tools.daynight" in bot.FEATURE_MODULES


def _berechne(faelle):
    if shutil.which("node") is None or not os.path.exists(APP_JS):
        pytest.skip("node oder dashboard_web/static/app.js nicht vorhanden")
    skript = (
        "const src=require('fs').readFileSync(process.argv[1],'utf8');"
        "const a=src.indexOf('var DAYNIGHT_MIN'),b=src.indexOf('function toolDayNightModal');"
        "eval(src.slice(a,b));"
        "const f=JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify(f.map(([d,n])=>{const r=dayNightBerechnen(d,n);"
        "return {tag:r.tag===undefined?null:r.tag.toFixed(2),"
        "nacht:r.nacht===undefined?null:r.nacht.toFixed(2),"
        "zyklus:r.zyklus===undefined?null:r.zyklus.toFixed(1),fehler:r.fehler||null};})));"
    )
    aus = subprocess.run(["node", "-e", skript, APP_JS, json.dumps(faelle)],
                         capture_output=True, text=True, check=True)
    return json.loads(aus.stdout)


@pytest.mark.parametrize("tag,nacht,erwartet", [
    (3, 30, ("4.00", "6.00", "210.0")),
    (2, 20, ("6.00", "6.00", "140.0")),
    (4, 45, ("3.00", "5.33", "285.0")),
    (1, 15, ("12.00", "4.00", "75.0")),
    (0.1875, 1, ("64.00", "11.25", "12.3")),
    (1.0666667, 1, ("11.25", "64.00", "65.0")),
])
def test_formel_stimmt_mit_referenz(tag, nacht, erwartet):
    r = _berechne([[tag, nacht]])[0]
    assert (r["tag"], r["nacht"], r["zyklus"]) == erwartet
    assert r["fehler"] is None


def test_ausserhalb_dayz_bereich_wird_abgelehnt():
    # Das Referenz-Tool gibt hier 120.00 aus - ungueltig fuer DayZ (0.1-64).
    r = _berechne([[0.1, 1]])[0]
    assert r["fehler"] and "120.00" in r["fehler"]


@pytest.mark.parametrize("tag,nacht", [(3, 0), (0, 30), (-1, 30)])
def test_null_und_negativ_werden_abgelehnt(tag, nacht):
    r = _berechne([[tag, nacht]])[0]
    assert r["fehler"] and r["tag"] is None
