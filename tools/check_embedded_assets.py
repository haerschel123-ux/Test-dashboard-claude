"""Waechter gegen die Fehlerklasse aus CLAUDE.md: Frontend-Dateien unter
dashboard_web/static/ geaendert, aber vergessen, sie wieder in bot.py
einzubetten (siehe reembed.py) – dann erreicht das Update bestehende
Installationen nie, weil die dort mitgelieferten Assets stumm veraltet
bleiben. Reiner Lesezugriff, nichts wird geschrieben oder geloescht.

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
import re
import sys
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOTPY = os.path.join(REPO, "bot.py")
STATIC = os.path.join(REPO, "dashboard_web", "static")
DATEIEN = ["app.js", "styles.css", "map.js", "index.html"]


def eingebettete_fassung(lines, rel):
    embed_start = next(i for i, l in enumerate(lines) if l.startswith("_EMBEDDED_ASSETS"))
    embed_ende = next(i for i in range(embed_start, len(lines)) if lines[i] == "}")
    muster = r'^    "' + re.escape(rel) + r'": \($'
    start = None
    for i in range(embed_start, embed_ende):
        if re.match(muster, lines[i]):
            start = i
            break
    if start is None:
        raise SystemExit(f"[FEHLER] Schluessel nicht in bot.py gefunden: {rel}")
    ende = None
    for i in range(start + 1, embed_ende):
        if lines[i] == "    ),":
            ende = i
            break
    b64 = "".join(ln.strip().strip('"') for ln in lines[start + 1:ende])
    return zlib.decompress(base64.b64decode(b64))


def main():
    if not os.path.isdir(STATIC):
        print("dashboard_web/static/ existiert lokal nicht (frischer Checkout) "
              "- nichts zu pruefen.")
        return 0

    with open(BOTPY, "r", encoding="utf-8") as f:
        lines = f.read().split("\n")

    abweichungen = []
    for rel in DATEIEN:
        platte_pfad = os.path.join(STATIC, rel)
        if not os.path.exists(platte_pfad):
            print(f"  ?    {rel}: nicht auf der Platte, übersprungen")
            continue
        with open(platte_pfad, "rb") as f:
            platte = f.read()
        eingebettet = eingebettete_fassung(lines, rel)
        if hashlib.sha256(platte).digest() == hashlib.sha256(eingebettet).digest():
            print(f"  OK   {rel}: eingebettete Fassung stimmt mit der Platte überein")
        else:
            abweichungen.append(rel)
            print(f"  ABWEICHUNG {rel}: Platte und eingebettete Fassung in bot.py "
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
