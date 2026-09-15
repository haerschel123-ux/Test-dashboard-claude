"""Bettet eine oder mehrere Dateien aus dashboard_web/static/ neu in
embedded_assets.py ein (siehe CLAUDE.md: "Aendert sich das Frontend, muss es
wieder eingebettet werden"). Ersetzt NUR den Block der genannten Datei(en) in
_EMBEDDED_ASSETS - alle anderen Eintraege bleiben byte-identisch stehen.

Aufruf:
    python3 tools/reembed.py app.js
    python3 tools/reembed.py app.js styles.css map.js index.html

Nach dem Lauf: python3 tools/check_embedded_assets.py (muss Exit 0 liefern).
Die Pruefsumme der VORHERIGEN Fassung gehoert danach noch von Hand in
_ASSET_KNOWN_HASHES (bot.py) - das macht dieses Skript bewusst nicht, das ist
eine inhaltliche Entscheidung (bestehende Installationen aktualisieren).
"""
import base64
import hashlib
import os
import re
import sys
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "dashboard_web", "static")
ASSETS_PY = os.path.join(REPO, "embedded_assets.py")
ZEILENBREITE = 110  # Zeichen Base64 je Zeile - deckt sich mit den vorhandenen Eintraegen


def de_tausender(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def baue_block(dateiname: str, roh: bytes) -> str:
    komprimiert = zlib.compress(roh, 9)
    b64 = base64.b64encode(komprimiert).decode("ascii")
    zeilen = [b64[i:i + ZEILENBREITE] for i in range(0, len(b64), ZEILENBREITE)] or [""]
    quoted = "\n".join(f'        "{z}"' for z in zeilen)
    kommentar = (f"    # {dateiname}  ({de_tausender(len(roh))} Bytes roh → "
                 f"{de_tausender(len(b64))} Bytes base64)")
    return f'{kommentar}\n    "{dateiname}": (\n{quoted}\n    ),'


def ersetze_eintrag(text: str, dateiname: str, neuer_block: str) -> str:
    muster = re.compile(
        r'[ \t]*#[^\n]*\n[ \t]*"' + re.escape(dateiname) + r'":\s*\(\n(?:[ \t]*"[^"\n]*"\n)*[ \t]*\),',
    )
    treffer = list(muster.finditer(text))
    if len(treffer) != 1:
        raise SystemExit(f"[FEHLER] {dateiname}: {len(treffer)} Treffer statt genau 1 - "
                          "von Hand pruefen, nichts geschrieben.")
    m = treffer[0]
    return text[:m.start()] + neuer_block + text[m.end():]


def main(argv):
    if not argv:
        raise SystemExit("Nutzung: python3 tools/reembed.py <datei> [<datei> ...]")
    text = open(ASSETS_PY, "r", encoding="utf-8").read()
    for dateiname in argv:
        pfad = os.path.join(STATIC, dateiname)
        if not os.path.exists(pfad):
            raise SystemExit(f"[FEHLER] nicht gefunden: {pfad}")
        with open(pfad, "rb") as f:
            roh = f.read()
        block = baue_block(dateiname, roh)
        text = ersetze_eintrag(text, dateiname, block)
        print(f"  {dateiname}: {len(roh)} Bytes roh, sha256={hashlib.sha256(roh).hexdigest()[:12]}…")
    with open(ASSETS_PY, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"\n{len(argv)} Datei(en) neu eingebettet in {ASSETS_PY}.")
    print("Jetzt: python3 tools/check_embedded_assets.py")


if __name__ == "__main__":
    main(sys.argv[1:])
