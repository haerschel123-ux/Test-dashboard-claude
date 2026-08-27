"""Waechter gegen die Fehlerklasse aus CLAUDE.md: Frontend-Dateien unter
dashboard_web/static/ geaendert, aber vergessen, sie wieder einzubetten
(siehe reembed.py) – dann erreicht das Update bestehende Installationen nie,
weil die dort mitgelieferten Assets stumm veraltet bleiben. Reiner
Lesezugriff, nichts wird geschrieben oder geloescht.

Seit der Auslagerung liegt ``_EMBEDDED_ASSETS`` in ``embedded_assets.py``,
nicht mehr in ``bot.py``. Das Dict wird hier direkt importiert statt wie
frueher zeilenweise aus dem Quelltext geschnitten: der alte Weg brach
wortlos mit ``StopIteration`` ab, sobald sich Datei oder Formatierung
aenderten – ein Waechter, der bei einer Umstrukturierung selbst ausfaellt,
ist schlimmer als keiner.

Aufruf vor jedem Commit, der dashboard_web/static/* aendert:
    python3 tools/check_embedded_assets.py

Exit 0: eingebettete Fassung deckt sich mit der Platte (oder es gibt lokal
        gar keine dashboard_web/static/ – frischer Checkout, nichts zu pruefen).
Exit 1: mindestens eine Datei weicht ab -> vor dem Commit reembed.py laufen
        lassen.
"""
import base64
import hashlib
import os
import sys
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "dashboard_web", "static")
DATEIEN = ["app.js", "styles.css", "map.js", "index.html"]


def lade_eingebettete_assets():
    """``_EMBEDDED_ASSETS`` aus embedded_assets.py holen.

    Bewusst NICHT ueber ``import bot``: bot.py zieht discord.py, aiohttp und
    einen ganzen Konfigurations-Startvorgang mit sich. Der Waechter soll auch
    in einem nackten Checkout ohne installierte Abhaengigkeiten laufen.
    """
    pfad = os.path.join(REPO, "embedded_assets.py")
    if not os.path.exists(pfad):
        raise SystemExit(f"[FEHLER] embedded_assets.py nicht gefunden: {pfad}")
    namensraum = {}
    with open(pfad, "r", encoding="utf-8") as f:
        exec(compile(f.read(), pfad, "exec"), namensraum)  # noqa: S102
    assets = namensraum.get("_EMBEDDED_ASSETS")
    if not isinstance(assets, dict):
        raise SystemExit("[FEHLER] _EMBEDDED_ASSETS fehlt in embedded_assets.py")
    return assets


def eingebettete_fassung(assets, rel):
    blob = assets.get(rel)
    if blob is None:
        raise SystemExit(f"[FEHLER] Schluessel nicht eingebettet: {rel}")
    return zlib.decompress(base64.b64decode(blob))


def main():
    if not os.path.isdir(STATIC):
        print("dashboard_web/static/ existiert lokal nicht (frischer Checkout) "
              "- nichts zu pruefen.")
        return 0

    assets = lade_eingebettete_assets()

    abweichungen = []
    for rel in DATEIEN:
        platte_pfad = os.path.join(STATIC, rel)
        if not os.path.exists(platte_pfad):
            print(f"  ?    {rel}: nicht auf der Platte, übersprungen")
            continue
        with open(platte_pfad, "rb") as f:
            platte = f.read()
        eingebettet = eingebettete_fassung(assets, rel)
        if hashlib.sha256(platte).digest() == hashlib.sha256(eingebettet).digest():
            print(f"  OK   {rel}: eingebettete Fassung stimmt mit der Platte überein")
        else:
            abweichungen.append(rel)
            print(f"  ABWEICHUNG {rel}: Platte und eingebettete Fassung in embedded_assets.py "
                  f"unterscheiden sich ({len(platte)} vs. {len(eingebettet)} Bytes roh)")

    if abweichungen:
        print(f"\n{len(abweichungen)} Datei(en) weichen ab: {', '.join(abweichungen)}")
        print("Vor dem Commit reembed.py laufen lassen, sonst erreicht das Update "
              "bestehende Installationen nicht (siehe CLAUDE.md).")
        return 1
    print("\nAlles eingebettet - passt zur Platte.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
