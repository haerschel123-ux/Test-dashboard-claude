#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║          DayZ Discord Bot – Vollständige Server-Verwaltung       ║
║  Nitrado Konsolen-Server | Multi-Guild | FTP-Auto-Discovery      ║
║  Starte mit: python dayz_bot.py                                  ║
╚══════════════════════════════════════════════════════════════════╝

Beim ersten Start werden automatisch erstellt:
  - config.json        (Hauptkonfiguration – nur bot_token und guild_ids
                        eintragen; den Nitrado-Token setzt du im Discord per
                        /setup token mit Server-Auswahl im Dropdown – FTP-Zugang
                        und aktive Karte werden dann automatisch erkannt)
  - guilds_config.json (Channel-Einstellungen pro Discord-Server)
  - banlist.json       (Lokale Ban-Datenbank)
  - log_state.json     (Log-Lese-Position)
  - requirements.txt   (Benötigte Pakete)
  - README.txt         (Kurzanleitung)
"""

import os
import sys
import json
import re
import asyncio
import ftplib
import io
import time
import logging
import platform
import sqlite3
import uuid
import random
import threading
import functools
import contextvars
import copy
import glob
import zipfile
import base64
import zlib
import secrets
import mimetypes
import hashlib
import ssl
import ipaddress
import urllib.parse
from collections import deque
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Dict, List, Tuple, Any, Deque
from zoneinfo import ZoneInfo


def _berlin_tz():
    """Zeitzone Europe/Berlin – mit Fallback, falls die IANA-Zeitzonendaten
    fehlen (z. B. Windows ohne installiertes tzdata-Paket). Verhindert, dass
    der Ankündigungs-Scheduler mit ZoneInfoNotFoundError abstürzt."""
    try:
        return ZoneInfo("Europe/Berlin")
    except Exception:
        # Notfall-Fallback ohne IANA-DB: lokale Systemzeitzone, sonst UTC.
        return datetime.now().astimezone().tzinfo or timezone.utc


# ══════════════════════════════════════════════════════════════
#  SCHRITT 1 – Automatische Abhängigkeits-Installation
# ══════════════════════════════════════════════════════════════
def _install_deps():
    required = {
        "discord": "discord.py>=2.3.0",
        "aiohttp":  "aiohttp>=3.9.0",
        "requests": "requests>=2.31.0",
        # Windows (und andere Systeme ohne IANA-Zeitzonen-DB) brauchen tzdata,
        # sonst schlägt ZoneInfo("Europe/Berlin") fehl (Ankündigungen crashen).
        "tzdata":   "tzdata>=2024.1",
    }
    missing = [pip for mod, pip in required.items() if not _can_import(mod)]
    if missing:
        print(f"\n[SETUP] Installiere fehlende Pakete: {', '.join(missing)}")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + missing)
        print("[SETUP] Fertig – starte Bot neu...\n")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    _install_optional_deps()

def _can_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _x509_available() -> bool:
    """Prüft, ob wirklich Zertifikate erzeugt werden können.

    Bewusst breit abgesichert: eine kaputte cryptography-Installation meldet
    sich nicht nur mit ImportError, sondern z. B. auch mit einem Panic aus dem
    Rust-Backend, wenn das cffi-Backend fehlt. Das darf den Bot nicht umwerfen.
    """
    try:
        __import__("cryptography.x509")
        return True
    except Exception:  # noqa: BLE001 – siehe Docstring
        return False


def _install_optional_deps():
    """Optionale Pakete nachziehen – ein Fehlschlag darf NIE den Start blockieren.

    ``cryptography`` wird nur für das selbstsignierte Zertifikat des Dashboards
    gebraucht (HTTPS auf demselben Port). Ohne das Paket läuft alles wie
    bisher, nur eben ausschließlich über http://.
    """
    if _x509_available():
        return
    import subprocess
    print("[SETUP] Installiere optionales Paket für HTTPS im Dashboard: cryptography")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                        "cryptography>=41.0"], check=True, timeout=300)
    except Exception as e:  # noqa: BLE001 – optional, darf fehlschlagen
        print(f"[SETUP] cryptography nicht installierbar ({e}).")
        print("[SETUP] Das Dashboard läuft dann nur über http:// – siehe README.")
        return
    # Frisch installierte Pakete findet der Import-Mechanismus erst nach dem
    # Leeren der Pfad-Caches; ohne execv-Neustart ist das nötig.
    import importlib
    importlib.invalidate_caches()
    if _x509_available():
        print("[SETUP] cryptography installiert – das Dashboard nimmt jetzt auch https:// an.")
    else:
        print("[SETUP] cryptography weiterhin nicht nutzbar – Dashboard läuft nur über http://.")

_install_deps()

import discord
from discord import app_commands
from discord.ext import tasks
import aiohttp
from aiohttp import web



# ══════════════════════════════════════════════════════════════
#  SCHRITT 2 – Logging
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[
        logging.FileHandler("dayz_bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("DayZBot")


# ══════════════════════════════════════════════════════════════
#  SCHRITT 3 – Standard-Konfiguration (wird auto-erstellt)
# ══════════════════════════════════════════════════════════════
CONFIG_FILE       = "config.json"
GUILDS_FILE       = "guilds_config.json"
BANLIST_FILE      = "banlist.json"
LOG_STATE_FILE    = "log_state.json"
WHITELIST_REQ_FILE = "whitelist_requests.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "_anleitung": [
        "1) Nur DIESE 2 Angaben sind nötig – alles andere richtet der Bot selbst ein:",
        "2) bot_token: Discord Entwicklerportal → deine App → Bot → Token kopieren",
        "3) guild_ids: Discord → Einstellungen → Erweitert → Entwicklermodus aktivieren,",
        "   dann Rechtsklick auf deinen Server → ID kopieren. Mehrere IDs möglich!",
        "   (Geht auch im Dashboard: dort ist es Schritt 3 der Einrichtung – die Befehle",
        "    werden dann sofort registriert, ohne Neustart.)",
        "4) admin_role_name: Name der Rolle, die Bot-Befehle nutzen darf (z.B. 'DayZ Admin')",
        "5) Bot starten und im Discord /setup token <dein Nitrado-Token> ausführen:",
        "   Es öffnet sich ein Dropdown mit deinen Nitrado-Servern – Server auswählen,",
        "   bestätigen, fertig. FTP-Zugang und die aktuelle Karte erkennt der Bot",
        "   automatisch und speichert alles hier als Cache.",
        "   (Nitrado-Token: Nitrado → Benutzereinstellungen → API-Schlüssel,",
        "   Long-Life-Token. Er kann auch weiterhin direkt hier eingetragen werden.)"
    ],
    "bot_token":         "HIER_DEIN_DISCORD_BOT_TOKEN_EINTRAGEN",
    "nitrado_token":     "",
    "service_id":        "",
    "nitrado_api_base":  "https://api.nitrado.net",
    "ftp_host":          "",
    "ftp_port":          21,
    "ftp_user":          "",
    "ftp_password":      "",

    "ftp_log_dir":       "",
    "ftp_ban_file":      "",
    "ftp_profile_dir":   "",

    "_anleitung_banliste": [
        "Die Banliste läuft über die NITRADO-SERVEREINSTELLUNGEN (dasselbe Feld wie im",
        "Webinterface bei den allgemeinen Einstellungen, 1 Name pro Zeile) – nicht mehr",
        "über eine ban.txt. /ban, /ban_entfernen und /banlist nutzen die Nitrado-API.",
        "nitrado_ban_category/nitrado_ban_key: Nur setzen, falls die Auto-Erkennung",
        "  (Settings-Key 'bans') das falsche Feld findet – sonst leer lassen."
    ],
    "nitrado_ban_category": "",
    "nitrado_ban_key":      "",

    "_anleitung_whitelist": [
        "Die Whitelist läuft wie die Banliste über die NITRADO-SERVEREINSTELLUNGEN",
        "(1 Name pro Zeile). /whitelist add|remove|show und das Whitelist-Panel",
        "(/send whitelist panel) nutzen die Nitrado-API.",
        "nitrado_whitelist_category/nitrado_whitelist_key: Nur setzen, falls die",
        "  Auto-Erkennung (Settings-Key 'whitelist') das falsche Feld findet – sonst leer."
    ],
    "nitrado_whitelist_category": "",
    "nitrado_whitelist_key":      "",

    "guild_ids": [111111111111111111],

    "server_ip":   "",
    "query_port":  2302,
    "rcon_port":   2310,

    "admin_role_name":           "DayZ Admin",
    # 5s statt 10s: die Server werden gleichzeitig abgefragt (siehe log_poll),
    # ein Zyklus dauert deshalb nur noch so lange wie der langsamste Kunde.
    # Damit steht eine neue Log-Zeile im Schnitt nach ~2,5s im Feed.
    "log_poll_interval_seconds": 5,
    "max_embed_fields":          25,
    "map_name":                  "ChernarusPlus",

    "_anleitung_neue_features": [
        "─────────── FEED-SCHUTZ / BELOHNUNGEN / AUTO-RESTART ───────────",
        "max_backlog_minutes: War der Bot länger als X Minuten offline, werden alte",
        "  Log-Events NICHT nachgepostet (verhindert Embed-Fluten nach Ausfällen).",
        "max_events_per_cycle: Höchstens so viele Events pro Poll-Zyklus posten.",
        "ftp_fail_warn_cycles: Nach so vielen FTP-Fehlzyklen in Folge postet der Bot",
        "  eine Warnung in den Adminlog-Channel (/setup feeds adminlog).",
        "kill_reward: Betrag, den ein per /link verknüpfter Spieler pro PvP-Kill erhält.",
        "playtime_reward: amount pro interval_minutes Spielzeit (500 pro 30 Min = 1000/Std)",
        "  – wird nur verlinkten Spielern gutgeschrieben.",
        "status_update_interval_seconds: Aktualisierungs-Intervall des Auto-Status-Embeds",
        "  (/setup feeds status #channel).",
        "auto_restart_schedule: Wird über /auto restart im Discord gesetzt (Startzeit +",
        "  Intervall in Stunden). Ankündigungen 15/5/1 Min vorher im /setup feeds restart Channel.",
        "economy_backup_keep: So viele tägliche economy.db-Backups werden aufbewahrt.",
        "delivery_cleanup_delay_seconds: FALLBACK-Delay. Nach einem Server-Neustart wartet",
        "  der Bot, bis der Server wieder ONLINE ist (A2S-Antwort), und entfernt die",
        "  SHOP_-Einträge dann SOFORT aus der cfgEffectArea.json. Nur wenn der Online-",
        "  Status nicht prüfbar ist (server_ip/query_port fehlt oder A2S-Timeout),",
        "  wartet er stattdessen diesen festen Delay, bevor er sie entfernt.",
        "delivery_online_wait_max_seconds: Maximal so lange auf die A2S-Antwort warten;",
        "  danach (oder ohne server_ip/query_port) greift der feste Delay als Fallback.",
        "zones: Überwachte Zonen (/zone create|remove|list|edit|allowlist im Discord verwalten).",
        "  Pro Zone: name, x/z (iZurvive: x=Ost, z=Nord), radius (Meter), role_id",
        "  (optional, Ping-@), channel_id (optional, eigener Warn-Channel), allowlist",
        "  (ignorierte Spieler), guild_id. Ohne channel_id gehen Pings in den zone-Feed",
        "  (/setup feeds zone, Fallback adminlog) – wiederholt alle",
        "  zone_ping_cooldown_seconds, solange der Spieler in der Zone bleibt.",
        "zone_ping_cooldown_seconds: Wiederhol-Intervall zwischen zwei Pings für",
        "  denselben Spieler in derselben Zone (Default 300 = 5 Minuten)."
    ],
    "delivery_cleanup_delay_seconds": 600,
    "delivery_online_wait_max_seconds": 2700,
    "zones": [],
    "zone_ping_cooldown_seconds": 300,
    "max_backlog_minutes":            10,
    "max_events_per_cycle":           30,
    "ftp_fail_warn_cycles":           10,
    "kill_reward":                    100,
    "playtime_reward":                {"amount": 500, "interval_minutes": 30},
    "status_update_interval_seconds": 180,
    "auto_restart_schedule":          {"enabled": False, "first_time": "04:00", "interval_hours": 4},
    "economy_backup_keep":            7,
    # Taegliches Zip mit ALLEN Kundendaten (Zugangsdaten, Zuordnungen, Feeds,
    # Zonen, Shop-Kataloge, Ankuendigungen, Economy) – nicht zu verwechseln
    # mit economy_backup_keep oben, das sichert nur die Geld-Datenbank.
    "betreiber_backup_keep":          7,

    "_anleitung_shop_economy": [
        "─────────── SHOP / ECONOMY / CASINO ───────────",
        "admin_role_ids: Liste von Discord-Rollen-IDs (Zahlen!), die ALLE Admin-Befehle nutzen",
        "  dürfen. Rechtsklick auf Rolle → ID kopieren (Entwicklermodus). admin_role_name bleibt",
        "  als Fallback aktiv, Discord-Administratoren dürfen immer.",
        "economy_admin_role_ids: Rollen-IDs, die NUR Geld verwalten dürfen (/addmoney,",
        "  /removemoney, /setbalance). Leer lassen = nur admin_role_ids.",
        "auto_restart_after_purchase: true = Server startet nach einem Kauf automatisch neu.",
        "  restart_cooldown_seconds = Wartezeit vor dem Auto-Restart, damit mehrere Käufe",
        "  gesammelt werden. false = Items spawnen erst beim nächsten regulären Neustart.",
        "delivery_grace_seconds: Käufe, die jünger sind, werden bei Neustart-Erkennung noch",
        "  NICHT als geliefert markiert (Server hatte die Datei evtl. noch nicht gelesen).",
        "default_pos_y: Standard-Höhe (Pos[1]) für Spawns, meist 0.0. default_radius: Radius.",
        "ftp_mission_dir / cfg_effect_area_path: werden per /ftp_scan automatisch gefunden,",
        "  können aber auch manuell gesetzt werden, z.B.",
        "  /dayzps/mpmissions/dayzOffline.chernarusplus/cfgEffectArea.json",
        "currency_name / currency_symbol / starting_balance: Währung & Startguthaben.",
        "economy: Beträge und Cooldowns für /work, /daily, /beg.",
        "bounty: min_amount/max_amount (Grenzen pro Kopfgeld) und cooldown_seconds pro Nutzer.",
        "hilfe_cooldown_seconds: Spam-Schutz für /hilfe (Sekunden pro Nutzer).",
        "casino: min_bet/max_bet, Auszahlungs-Multiplikatoren und Cooldowns pro Spiel.",
        "shop_items: Katalog. Pro Item: name (Anzeigename), classname (exakter DayZ-Type!),",
        "  price, category, enabled (true/false), max_amount_per_buy.",
        "shop_items_file: generierter Groß-Katalog. Wird beim Bot-Start automatisch aus",
        "  einer types.xml im Bot-Ordner erzeugt, falls die Datei noch fehlt. Existiert",
        "  sie, hat sie Vorrang vor shop_items. Preise pro Kategorie: shop_category_prices",
        "  (Datei löschen + Neustart = neu generieren) oder einzeln per /shop setprice",
        "  bzw. /edit shopitem. Items hinzufügen/ändern: /add shopitem, /edit shopitem.",
        "Nach Änderungen an dieser Datei: /economy_reload im Discord (kein Neustart nötig)."
    ],

    "admin_role_ids":              [],
    "economy_admin_role_ids":      [],

    "auto_restart_after_purchase": False,
    "restart_cooldown_seconds":    300,
    "delivery_grace_seconds":      90,
    "default_pos_y":               0.0,
    "default_radius":              1,
    "ftp_mission_dir":             "",
    "cfg_effect_area_path":        "",
    "economy_db_path":             "economy.db",

    "currency_name":    "Rubles",
    "currency_symbol":  "₽",
    "starting_balance": 5000,

    "economy": {
        "work":  {"min": 50, "max": 150, "cooldown_seconds": 3600},
        "daily": {"amount": 300, "cooldown_seconds": 86400},
        "beg":   {"min": 5, "max": 50, "fail_chance": 0.35, "cooldown_seconds": 300}
    },

    # Kopfgelder: Betragsgrenzen + Cooldown pro Nutzer (Spam-/Missbrauchsschutz)
    "bounty": {"min_amount": 100, "max_amount": 10000, "cooldown_seconds": 300},
    # /hilfe-Spam-Schutz (Sekunden pro Nutzer)
    "hilfe_cooldown_seconds": 30,

    "casino": {
        "blackjack": {"min_bet": 10, "max_bet": 1000, "blackjack_payout": 1.5, "cooldown_seconds": 30},
        "roulette":  {"min_bet": 10, "max_bet": 1000, "cooldown_seconds": 5,
                      "payout_number": 36.0, "payout_color": 2.0,
                      "payout_evenodd": 2.0, "payout_highlow": 2.0},
        "slots":     {"min_bet": 10, "max_bet": 500, "cooldown_seconds": 10,
                      "symbols":  ["🍒", "🍋", "🍉", "🔔", "💎", "7️⃣"],
                      "weights":  [30, 25, 20, 12, 8, 5],
                      "payout_three": {"🍒": 3, "🍋": 4, "🍉": 5, "🔔": 8, "💎": 15, "7️⃣": 30},
                      "payout_two": 1.5}
    },

    # Generierter Groß-Katalog: hat Vorrang vor shop_items (Fallback), wenn die Datei existiert
    "shop_items_file":    "shop_items.json",
    "shop_default_price": 100,
    "shop_category_prices": {
        "Armbands": 50, "Ammo": 150, "Attachments": 400, "Bags": 600,
        "Belts": 150, "Clothing": 100, "Clothing Improvised": 80, "Feet": 120,
        "Firearms": 2500, "Flags": 300, "Food": 60, "Gas Gear": 800,
        "Gas Masks": 700, "Ghillies": 1200, "Gloves": 100, "Hats": 80,
        "Helmets": 400, "Lights": 150, "Magazines": 300, "Masks": 150,
        "Medical Items": 200, "Melee Items": 250, "Misc Items": 100,
        "Nades & Traps": 900, "Navigation Items": 250, "Pelts": 150,
        "Plants": 40, "Optics": 600, "Seeds": 30, "Storage Items": 700,
        "Supplies": 120, "Suppressors": 800, "Tacticals": 500, "Tools": 300,
        "Vests": 800, "Vehicle Parts": 350, "Vehicles": 15000
    },

    "shop_items": [
        {"name": "CZ75",           "classname": "cz75",                    "price": 500,  "category": "Weapons", "enabled": True, "max_amount_per_buy": 5},
        {"name": "Mlock-91",       "classname": "Mlock91",                 "price": 600,  "category": "Weapons", "enabled": True, "max_amount_per_buy": 5},
        {"name": "M4-A1",          "classname": "M4A1",                    "price": 2500, "category": "Weapons", "enabled": True, "max_amount_per_buy": 2},
        {"name": "KA-M",           "classname": "AKM",                     "price": 2500, "category": "Weapons", "enabled": True, "max_amount_per_buy": 2},
        {"name": "Combat Knife",   "classname": "CombatKnife",             "price": 150,  "category": "Gear",    "enabled": True, "max_amount_per_buy": 10},
        {"name": "Field Backpack", "classname": "AliceBag_Camo",           "price": 800,  "category": "Gear",    "enabled": True, "max_amount_per_buy": 3},
        {"name": "Tetracycline",   "classname": "TetracyclineAntibiotics", "price": 200,  "category": "Medical", "enabled": True, "max_amount_per_buy": 10},
        {"name": "Saline Bag IV",  "classname": "SalineBagIV",             "price": 350,  "category": "Medical", "enabled": True, "max_amount_per_buy": 5},
        {"name": "Canned Bacon",   "classname": "CannedBacon",             "price": 60,   "category": "Food",    "enabled": True, "max_amount_per_buy": 20}
    ],

    # ─────────── DASHBOARD (Web-Oberfläche) ───────────
    # Das Dashboard läuft im selben Prozess wie der Bot (aiohttp) und wird über
    # denselben Nitrado-Token abgesichert. Port ggf. an den vom Host (z. B.
    # PebbleHost) zugewiesenen Port anpassen – SERVER_PORT/PORT haben Vorrang.
    "dashboard_enabled":     True,
    "dashboard_host":        "0.0.0.0",
    "dashboard_port":        8080,
    # Öffentlich erreichbare Adresse – wird NUR für die Startmeldung im Log
    # benutzt. Gebunden wird immer an "dashboard_host" (0.0.0.0). Ohne Port
    # angeben, dann wird der tatsächliche Port automatisch angehängt; mit
    # Port oder als https://… wird der Wert genau so übernommen (z. B. hinter
    # einem Reverse-Proxy). Leer lassen = nur lokaler Hinweis.
    # Die Umgebungsvariable DASHBOARD_PUBLIC_HOST hat Vorrang.
    "dashboard_public_host": "brigardekillfeed.my.pebble.host",
    # HTTPS: Das Dashboard nimmt auf DEMSELBEN Port zusätzlich TLS an, weil
    # Browser getippte Adressen oft von sich aus auf https:// hochstufen – ohne
    # TLS endet das in ERR_SSL_PROTOCOL_ERROR und die Seite ist gar nicht
    # erreichbar. Das Zertifikat ist selbstsigniert und wird beim Start
    # automatisch erzeugt; der Browser zeigt deshalb einmalig eine Warnung
    # ("Erweitert" → "Weiter"). Ganz ohne Warnung geht nur ein echtes
    # Zertifikat, z. B. über einen Cloudflare Tunnel (siehe README).
    # false = wie früher ausschließlich HTTP.
    "dashboard_https":       True,

    # Ab dieser Distanz (Meter) zaehlt ein PvP-Kill als "Long Range Kill" und
    # geht in dessen eigenen Feed statt in den gewoehnlichen Kill-Feed.
    "long_range_kill_meter": 300,

    # ─────────── CLOUDFLARE TUNNEL (eigene Domain ohne Port) ───────────
    # Leer = Feature aus, alles läuft wie bisher nur über dashboard_https.
    # Einrichten (einmalig, im Cloudflare-Konto):
    #   1. Zero Trust → Networks → Tunnels → "Create a tunnel" → Cloudflared
    #   2. Den angezeigten Connector-Token hier eintragen (NICHT den
    #      OS-spezifischen Installationsbefehl - nur die lange Zeichenkette
    #      nach "service install" bzw. "run --token").
    #   3. Im selben Assistenten unter "Public Hostname": die eigene Domain
    #      eintragen, als Ziel "HTTP" + "localhost:<Port>" (der Port steht im
    #      PebbleHost-Panel unter Network/Allocations, siehe README).
    # Der Bot lädt cloudflared bei Bedarf selbst herunter (nach
    # dashboard_web/, wie das Zertifikat) und hält die Verbindung am Leben -
    # es ist keine Installation auf dem Server nötig.
    "cloudflare_tunnel_token": "",

    # ─────────── DISCORD-LOGIN FÜRS DASHBOARD ───────────
    # Solange discord_client_secret leer ist, bleibt der Login AUS und das
    # Dashboard verhält sich wie bisher. Das ist Absicht: ein Update darf ein
    # laufendes Dashboard nicht aussperren.
    # Einrichten: Discord Developer Portal → deine App → OAuth2 →
    #   1. "Client Secret" kopieren und hier eintragen
    #   2. unter "Redirects" die Dashboard-Adresse + /api/auth/discord/callback
    #      eintragen, z. B. http://brigardekillfeed.my.pebble.host:25590/api/auth/discord/callback
    #      (am besten zusätzlich die https://-Variante)
    # Danach muss sich jeder erst mit Discord anmelden, bevor er den
    # Nitrado-Token eingeben kann.
    "discord_client_id":     "",   # leer = Application-ID des laufenden Bots
    "discord_client_secret": "",
    "discord_redirect_uri":  "",   # leer = automatisch aus dem Aufruf gebildet
    # Wer diese Rolle in einem der verbundenen Discord-Server hat, sieht im
    # Dashboard zusätzlich die Kategorien "Logs" und "Guild IDs".
    # Leer = niemand sieht sie.
    "dashboard_admin_role_id": "1530653925575753838",
    # Channel im EIGENEN Discord des Bot-Betreibers, in den der Bot meldet,
    # wenn bei einem Kunden etwas klemmt (kein FTP, Token abgelehnt, …) oder
    # wieder normal laeuft, sowie bei neuen Premium-Anfragen. Wird per
    # /betreiber alarm_channel gesetzt (nur der Bot-Eigentuemer darf das).
    # Leer = keine Meldungen.
    "betreiber_alarm_channel_id": "",
    # Premium-Rolle: Wird einem Kundenserver eine Discord-Guild zugeordnet
    # (= Freischaltung), bekommt der Kunde diese Rolle im Betreiber-Discord.
    # Wird die Freischaltung zurueckgenommen oder der Server entfernt, geht sie
    # wieder ab – es sei denn, der Kunde hat noch einen anderen freien Server.
    # Beide Felder leer = die Funktion ist aus.
    "premium_role_guild_id": "1534352039713439855",
    "premium_role_id":       "1534356139758588097",
    # Optionale Leaflet-Kachel-URLs je Karte, z. B.
    #   {"ChernarusPlus": "https://.../{z}/{x}/{y}.png"}
    "dashboard_map_tiles":   {},
    # Optionale abweichende Welt-Kantenlängen je Karte (Meter).
    "dashboard_map_sizes":   {},
    # Eigene Shop-Kategorien, die im Dashboard angelegt wurden.
    "shop_categories_custom": []
}

# ══════════════════════════════════════════════════════════════
#  Log-Typen (Auswahl im /setup feeds Dropdown)
# ══════════════════════════════════════════════════════════════
LOG_TYPES: Dict[str, str] = {
    "killfeed":     "☠️  PvP-Kills zwischen Spielern",
    "damagefeed":   "🩸 Treffer / Damage an Spielern",
    "joinleave":    "🟢 Spieler betritt / verlässt den Server",
    "suicide":      "💀 Selbstmord / Freitod",
    "chat":         "💬 In-Game Chat-Nachrichten",
    "adminlog":     "🛡️  Admin-Aktionen & Befehle",
    "envdeath":     "☠️  Umwelttode (Zombies, Bleed, Hunger usw.)",
    "vehiclecrash": "🚗 Fahrzeug-Ereignisse & Crashes",
    "basebuild":    "🏗️  Basis-Bau Ereignisse",
    "loot":         "🎒 Loot-Spawn/-Despawn Ereignisse",
    "connecting":   "🔌 Verbindungsversuche (is connecting)",
    "shop_log":     "🛒 Shop-Käufe (wer hat was gekauft)",
    "economy_log":  "💰 Economy-Admin-Aktionen (add/remove money)",
    "status":       "📊 Auto-Status-Embed (online/offline, Spielerzahl)",
    "restart":      "🔄 Restart-Ankündigungen (geplante Neustarts)",
    "zone":         "🛡️ Zonen-Pings (Spieler in überwachten Zonen, /zone create)",
}


# ══════════════════════════════════════════════════════════════
#  FEED-TYPEN – die feine Aufteilung der Log-Ereignisse
#
#  LOG_TYPES oben sind die groben Sammelkategorien; sie bleiben fuer die
#  Bot-eigenen Feeds (Shop, Economy, Status, Restart, Zone) und fuer alle
#  Stellen im Code, die feste Schluessel benutzen (/setup feeds, Auto-Restart).
#
#  FEED_TYPES ist die Liste, aus der im Dashboard gewaehlt wird: statt
#  "Umwelttode" gibt es Zombie Death, Wolf Death, Fall Death - jeder mit
#  eigenem Kanal und eigener Farbe. Welcher Typ ein Ereignis ist, entscheidet
#  _feed_key() anhand der geparsten Ursache bzw. des Bau-Verbs.
#
#  "gruppe" ordnet nur die Anzeige im Dropdown, "farbe" ist die Vorgabe, die
#  der Kunde je Feed ueberschreiben kann.
# ══════════════════════════════════════════════════════════════
FEED_TYPES: Dict[str, Dict[str, Any]] = {
    # ── Kills ────────────────────────────────────────────────
    "kill":               {"label": "Kill",                "gruppe": "Kills",
                           "emoji": "☠️", "farbe": 0xE74C3C},
    "long_range_kill":    {"label": "Long Range Kill",     "gruppe": "Kills",
                           "emoji": "🎯", "farbe": 0xC0392B},
    # ── Tode ─────────────────────────────────────────────────
    "suicide_death":      {"label": "Suicide Death",       "gruppe": "Tode",
                           "emoji": "💀", "farbe": 0x7F8C8D},
    "zombie_death":       {"label": "Zombie Death",        "gruppe": "Tode",
                           "emoji": "🧟", "farbe": 0x27AE60},
    "wolf_death":         {"label": "Wolf Death",          "gruppe": "Tode",
                           "emoji": "🐺", "farbe": 0x95A5A6},
    "bear_death":         {"label": "Bear Death",          "gruppe": "Tode",
                           "emoji": "🐻", "farbe": 0x795548},
    "fall_death":         {"label": "Fall Death",          "gruppe": "Tode",
                           "emoji": "🪂", "farbe": 0x34495E},
    "fire_death":         {"label": "Fire Death",          "gruppe": "Tode",
                           "emoji": "🔥", "farbe": 0xE67E22},
    "explosion_death":    {"label": "Explosion Death",     "gruppe": "Tode",
                           "emoji": "💥", "farbe": 0xD35400},
    "trap_death":         {"label": "Trap Death",          "gruppe": "Tode",
                           "emoji": "🪤", "farbe": 0x8E44AD},
    "barbed_wire_death":  {"label": "Barbed Wire Death",   "gruppe": "Tode",
                           "emoji": "🚧", "farbe": 0x616161},
    "vehicle_death":      {"label": "Vehicle Death",       "gruppe": "Tode",
                           "emoji": "🚗", "farbe": 0x16A085},
    "bleed_out_death":    {"label": "Bleed Out Death",     "gruppe": "Tode",
                           "emoji": "🩸", "farbe": 0xB03A2E},
    "unknown_death":      {"label": "Unknown Death",       "gruppe": "Tode",
                           "emoji": "❓", "farbe": 0x607D8B},
    # ── Treffer ──────────────────────────────────────────────
    "player_hit":         {"label": "Player Hit",          "gruppe": "Treffer",
                           "emoji": "🩸", "farbe": 0xFF6B35},
    "zombie_hit":         {"label": "Zombie Hit",          "gruppe": "Treffer",
                           "emoji": "🧟", "farbe": 0x2ECC71},
    "animal_hit":         {"label": "Animal Hit",          "gruppe": "Treffer",
                           "emoji": "🐾", "farbe": 0xA0522D},
    "trap_hit":           {"label": "Trap Hit",            "gruppe": "Treffer",
                           "emoji": "🪤", "farbe": 0x9B59B6},
    "fire_hit":           {"label": "Fire Hit",            "gruppe": "Treffer",
                           "emoji": "🔥", "farbe": 0xF39C12},
    "explosion_hit":      {"label": "Explosion Hit",       "gruppe": "Treffer",
                           "emoji": "💥", "farbe": 0xE8580C},
    "fall_damage_hit":    {"label": "Fall Damage Hit",     "gruppe": "Treffer",
                           "emoji": "🪂", "farbe": 0x546E7A},
    "vehicle_hit":        {"label": "Vehicle Hit",         "gruppe": "Treffer",
                           "emoji": "🚗", "farbe": 0x1ABC9C},
    "barbed_wire_hit":    {"label": "Barbed Wire Hit",     "gruppe": "Treffer",
                           "emoji": "🚧", "farbe": 0x757575},
    # ── Verbindung ───────────────────────────────────────────
    "connect":            {"label": "Connect",             "gruppe": "Verbindung",
                           "emoji": "🟢", "farbe": 0x2ECC71},
    "disconnect":         {"label": "Disconnect",          "gruppe": "Verbindung",
                           "emoji": "🔴", "farbe": 0xE74C3C},
    "connection_attempt": {"label": "Connection Attempt",  "gruppe": "Verbindung",
                           "emoji": "🔌", "farbe": 0x3498DB},
    # ── Zustand ──────────────────────────────────────────────
    "unconscious":        {"label": "Unconscious",         "gruppe": "Zustand",
                           "emoji": "😵", "farbe": 0x8E44AD},
    "conscious":          {"label": "Conscious",           "gruppe": "Zustand",
                           "emoji": "🙂", "farbe": 0x9B59B6},
    # ── Bau ──────────────────────────────────────────────────
    "build":              {"label": "Build",               "gruppe": "Bau",
                           "emoji": "🏗️", "farbe": 0xF1C40F},
    "dismantle":          {"label": "Dismantle",           "gruppe": "Bau",
                           "emoji": "🔨", "farbe": 0xE67E22},
    "place":              {"label": "Place",               "gruppe": "Bau",
                           "emoji": "📦", "farbe": 0xF39C12},
    "pack":               {"label": "Pack",                "gruppe": "Bau",
                           "emoji": "🎒", "farbe": 0xD68910},
    "fold":               {"label": "Fold",                "gruppe": "Bau",
                           "emoji": "📐", "farbe": 0xCA8A04},
    "repair":             {"label": "Repair",              "gruppe": "Bau",
                           "emoji": "🛠️", "farbe": 0x27AE60},
    "mount":              {"label": "Mount",               "gruppe": "Bau",
                           "emoji": "⬆️", "farbe": 0x2980B9},
    "unmount":            {"label": "Unmount",             "gruppe": "Bau",
                           "emoji": "⬇️", "farbe": 0x21618C},
    "bury":               {"label": "Bury",                "gruppe": "Bau",
                           "emoji": "⛏️", "farbe": 0x6D4C41},
    "unbury":             {"label": "Unbury",              "gruppe": "Bau",
                           "emoji": "🪙", "farbe": 0x8D6E63},
    "flag_raise":         {"label": "Flag Raise",          "gruppe": "Bau",
                           "emoji": "🚩", "farbe": 0x2ECC71},
    "flag_lower":         {"label": "Flag Lower",          "gruppe": "Bau",
                           "emoji": "🏳️", "farbe": 0x95A5A6},
    # ── Sonstiges ────────────────────────────────────────────
    "chat":               {"label": "Chat",                "gruppe": "Sonstiges",
                           "emoji": "💬", "farbe": 0x3498DB},
    "admin_action":       {"label": "Admin Action",        "gruppe": "Sonstiges",
                           "emoji": "🛡️", "farbe": 0x9B59B6},
    "loot":               {"label": "Loot",                "gruppe": "Sonstiges",
                           "emoji": "🎒", "farbe": 0x27AE60},
    # Rückfall-Feeds: garantieren, dass nichts mehr unbemerkt verschwindet.
    # catch_all fängt jedes ERKANNTE Ereignis auf, für das kein eigener Feed
    # gesetzt ist (Rückfallkette in _dispatch: fein → grob → catch_all).
    # unparsed sammelt Roh-Log-Zeilen, die an KEINEM Muster hängen blieben –
    # gedrosselt und entdoppelt gepostet (siehe _unparsed_posten), damit eine
    # rauschige ADM-Datei die Feeds nicht flutet. Ungefiltert und vollständig
    # stehen dieselben Zeilen auf der Diagnose-Seite im Dashboard.
    "catch_all":          {"label": "Alles Übrige",        "gruppe": "Sonstiges",
                           "emoji": "🧺", "farbe": 0x5D6D7E},
    "unparsed":           {"label": "Unerkannte Log-Zeilen", "gruppe": "Sonstiges",
                           "emoji": "❓", "farbe": 0x34495E},
    # ── Bot-eigene Feeds (keine Log-Ereignisse) ──────────────
    "shop_log":           {"label": "Shop-Log",            "gruppe": "Bot",
                           "emoji": "🛒", "farbe": 0x1ABC9C},
    "economy_log":        {"label": "Economy-Log",         "gruppe": "Bot",
                           "emoji": "💰", "farbe": 0xF1C40F},
    "status":             {"label": "Status-Embed",        "gruppe": "Bot",
                           "emoji": "📊", "farbe": 0x3498DB},
    "restart":            {"label": "Restart-Ankündigung", "gruppe": "Bot",
                           "emoji": "🔄", "farbe": 0xE67E22},
    "zone":               {"label": "Zonen-Ping",          "gruppe": "Bot",
                           "emoji": "🛡️", "farbe": 0xE74C3C},
    # Aus der .RPT erkannt: jeder Serverstart legt eine neue an. Meldet auch
    # Neustarts, die NICHT vom eigenen Zeitplan kommen (Absturz, Nitrado).
    "server_restart":     {"label": "Server Restart (erkannt)", "gruppe": "Bot",
                           "emoji": "♻️", "farbe": 0x2ECC71},
}

# Die alten Sammelkategorien, die durch FEED_TYPES ersetzt wurden.
_ALTE_EREIGNIS_FEEDS = ("killfeed", "damagefeed", "joinleave", "suicide", "chat",
                        "adminlog", "envdeath", "vehiclecrash", "basebuild",
                        "loot", "connecting")

# Alter Schlüssel → heutiger Feed-Typ. Betriebsmeldungen im Code posten teils
# noch auf die entfernten Sammelkategorien; ohne diese Übersetzung landen sie
# nirgends, weil der Schlüssel in FEED_TYPES nicht mehr existiert und im
# Dashboard nicht anlegbar ist.
_FEED_ALIASSE = {
    "adminlog": "admin_action",
    "killfeed": "kill",
    "joinleave": "connect",
    "chat": "chat",
    "restart": "server_restart",
}

# Stichwort → Feed-Typ. Zuerst passender Treffer gewinnt, deshalb stehen die
# spezielleren Begriffe vorn (z. B. "barbed" vor "wire").
_URSACHE_TODE = (
    ("barbed", "barbed_wire_death"), ("wire", "barbed_wire_death"),
    ("zmb", "zombie_death"), ("zombie", "zombie_death"), ("infected", "zombie_death"),
    # DayZ schreibt die Klassennamen, nicht die Tiernamen: Animal_CanisLupus,
    # Animal_UrsusArctos. Beide Schreibweisen stehen drin.
    # "trap" MUSS vor "bear" stehen: eine BearTrap ist eine Falle, kein Baer.
    ("trap", "trap_death"), ("snare", "trap_death"),
    ("wolf", "wolf_death"), ("canislupus", "wolf_death"), ("lupus", "wolf_death"),
    ("bear", "bear_death"), ("ursus", "bear_death"),
    ("fall", "fall_death"),
    ("fire", "fire_death"), ("burn", "fire_death"), ("flame", "fire_death"),
    ("explos", "explosion_death"), ("grenade", "explosion_death"),
    ("mine", "explosion_death"), ("c4", "explosion_death"),
    ("vehicle", "vehicle_death"), ("car", "vehicle_death"),
    ("truck", "vehicle_death"), ("transport", "vehicle_death"),
    ("bled", "bleed_out_death"), ("bleed", "bleed_out_death"),
)
# Todesarten, die DayZ als VERB schreibt ("bled out") statt als Ursache hinter
# "by". Sie stehen deshalb nie im cause-Feld und werden in der Rohzeile
# gesucht – bewusst nur mehrwortige, eindeutige Wendungen, damit ein Spieler
# namens "Bleeder" nicht versehentlich trifft.
_TODESVERBEN = (
    ("bled out", "bleed_out_death"),
)
_URSACHE_TREFFER = (
    ("barbed", "barbed_wire_hit"), ("wire", "barbed_wire_hit"),
    ("zmb", "zombie_hit"), ("zombie", "zombie_hit"), ("infected", "zombie_hit"),
    # "trap" vor den Tieren – siehe _URSACHE_TODE (BearTrap).
    ("trap", "trap_hit"), ("snare", "trap_hit"),
    ("wolf", "animal_hit"), ("canislupus", "animal_hit"), ("lupus", "animal_hit"),
    ("bear", "animal_hit"), ("ursus", "animal_hit"), ("animal", "animal_hit"),
    ("boar", "animal_hit"), ("cow", "animal_hit"), ("deer", "animal_hit"),
    ("fall", "fall_damage_hit"),
    ("fire", "fire_hit"), ("burn", "fire_hit"), ("flame", "fire_hit"),
    ("explos", "explosion_hit"), ("grenade", "explosion_hit"),
    ("mine", "explosion_hit"), ("c4", "explosion_hit"),
    ("vehicle", "vehicle_hit"), ("car", "vehicle_hit"),
    ("truck", "vehicle_hit"), ("transport", "vehicle_hit"),
)
# Bau-Verb → Feed-Typ. Die Verben stammen aus dem basebuild-Muster.
_BAU_VERBEN = {
    "built": "build", "constructed": "build",
    "dismantled": "dismantle", "removed": "dismantle",
    "placed": "place", "deployed": "place", "attached": "place",
    "packed": "pack", "folded": "fold", "repaired": "repair",
    "mounted": "mount", "unmounted": "unmount",
    "buried": "bury", "unburied": "unbury",
    "raised": "flag_raise", "lowered": "flag_lower",
}


def _stichwort(text: Any, tabelle) -> Optional[str]:
    """Erster passender Eintrag aus einer Stichwort-Tabelle. None ohne Treffer."""
    t = str(text or "").lower()
    if not t:
        return None
    for wort, key in tabelle:
        if wort in t:
            return key
    return None


def _feed_key(ev: Dict[str, Any]) -> Optional[str]:
    """Welchen Feed-Typ hat dieses Log-Ereignis?

    Die feine Aufteilung entsteht hier und nicht schon im Parser: der Parser
    liest die Zeile, diese Funktion deutet sie. So bleibt die Zuordnung an
    EINER Stelle aenderbar, wenn DayZ oder eine Mod anders formuliert.

    Wichtig: Unbekanntes wird NIE verschluckt. Ein Tod ohne erkannte Ursache
    wird ``unknown_death``, ein Treffer ohne erkannte Quelle ``player_hit`` –
    sonst verschwaenden Ereignisse lautlos.
    """
    t = ev.get("type")

    if t == "kill_pvp":
        # Die Distanz steht als Text im Ereignis ("142.3" oder "?").
        try:
            weit = float(str(ev.get("distance", "")).replace(",", ".").strip())
        except (TypeError, ValueError):
            weit = 0.0
        grenze = _long_range_grenze()
        return "long_range_kill" if weit >= grenze else "kill"

    if t == "suicide":
        return "suicide_death"

    if t == "kill_env":
        return (_stichwort(ev.get("cause"), _URSACHE_TODE)
                or _stichwort(ev.get("raw"), _TODESVERBEN)
                or "unknown_death")

    if t == "damage":
        # Waffe und Angreifer zusammen betrachten: mal steht die Quelle im
        # einen, mal im anderen Feld.
        treffer = (_stichwort(ev.get("weapon"), _URSACHE_TREFFER)
                   or _stichwort(ev.get("attacker"), _URSACHE_TREFFER))
        return treffer or "player_hit"

    if t == "basebuild":
        aktion = str(ev.get("aktion") or "").lower()
        key = _BAU_VERBEN.get(aktion)
        # "raised"/"lowered" gelten nur der Flagge; an anderem Gegenstand ist
        # es ein gewoehnliches Bau-Ereignis.
        if key in ("flag_raise", "flag_lower") and "flag" not in str(
                ev.get("item") or "").lower():
            return "build"
        return key or "build"

    if t == "vehicle":
        return "vehicle_death"

    if t == "connecting":
        return "connection_attempt"

    # connect, disconnect, chat, admin_action, loot, unconscious, conscious
    return t if t in FEED_TYPES else None


def _long_range_grenze() -> float:
    """Ab welcher Distanz ein Kill als Long Range Kill gilt (Meter).

    Wie ``_cur_symbol`` am gerade behandelten Server: sonst entschiede der
    Betreiberwert fuer alle Kunden, ob ein Kill im normalen oder im
    Long-Range-Feed landet, obwohl die Grenze pro Server einstellbar ist.
    """
    conn = _AKTUELLER_SERVER.get()
    quelle = conn.get("long_range_kill_meter", 300) if conn is not None \
        else cfg.config.get("long_range_kill_meter", 300)
    try:
        return float(quelle)
    except (TypeError, ValueError, AttributeError):
        return 300.0


# ══════════════════════════════════════════════════════════════
#  Map-Name → Mission-Ordner unter mpmissions/
# ══════════════════════════════════════════════════════════════
MISSION_FOLDERS: Dict[str, str] = {
    "chernarusplus": "dayzOffline.chernarusplus",
    "livonia":       "dayzOffline.enoch",
    "enoch":         "dayzOffline.enoch",
    "sakhal":        "dayzOffline.sakhal",
}

def _mission_folder_for_map(map_name: str) -> str:
    """Ermittelt den mpmissions-Ordnernamen für die konfigurierte Map."""
    return MISSION_FOLDERS.get((map_name or "").strip().lower(), "dayzOffline.chernarusplus")

# Kanonische Map-Namen (Schlüssel von _MAP_LOCATIONS bzw. iZurvive-URL)
_CANONICAL_MAPS: Dict[str, str] = {
    "chernarusplus": "ChernarusPlus",
    "chernarus":     "ChernarusPlus",
    "enoch":         "Livonia",
    "livonia":       "Livonia",
    "sakhal":        "Sakhal",
}

def _canonical_map_name(raw: str) -> Optional[str]:
    """Normalisiert einen Map-Namen aus Nitrado-Daten (z.B. query.map oder
    Mission-Ordner 'dayzOffline.sakhal') auf den kanonischen Namen.
    None, wenn keine bekannte Karte erkannt wird."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    for key, canon in _CANONICAL_MAPS.items():
        if key in s:
            return canon
    return None


# ══════════════════════════════════════════════════════════════
#  Hilfsdateien automatisch erstellen
# ══════════════════════════════════════════════════════════════
def _create_helper_files():
    req = ("discord.py>=2.3.0\naiohttp>=3.9.0\nrequests>=2.31.0\n"
           "# optional – nur für HTTPS im Dashboard (selbstsigniertes Zertifikat)\n"
           "cryptography>=41.0\n")
    if not os.path.exists("requirements.txt"):
        with open("requirements.txt", "w", encoding="utf-8") as f:
            f.write(req)
        print("[SETUP] requirements.txt erstellt.")

    readme = """╔══════════════════════════════════════════════════════════════╗
║              DayZ Bot – Kurzanleitung                        ║
╚══════════════════════════════════════════════════════════════╝

ERSTE SCHRITTE
──────────────
1. Öffne config.json und trage NUR diese 2 Angaben ein:
   bot_token und guild_ids.
2. Starte den Bot: python dayz_bot.py
3. Öffne das Web-Dashboard (Adresse steht beim Start im Log) und
   trage unter „Optionen“ deinen Nitrado-Token ein. Server im
   Dropdown auswählen – FTP-Zugang, die aktive Karte und die
   Log-Verzeichnisse erkennt der Bot dann automatisch.
4. Feeds, Zonen, Shop und Ankündigungen werden ebenfalls im
   Dashboard eingerichtet. Die früheren Befehle /setup token,
   /setup feeds, /setup uebersicht, /edit_feeds und
   /zone create|edit|remove gibt es nicht mehr.

BEFEHLE (alle nur für Admins mit der konfigurierten Rolle)
──────────────────────────────────────────────────────────
/show_feeds                     → Zeigt, welche Feeds für diesen Server
                                   eingerichtet sind (Dashboard: Seite „Feeds“)
/test [zeilen]                  → Liest die letzten Log-Zeilen und schickt je
                                   Feed-Typ ein Beispiel-Event zur Kontrolle

/neustart                       → Server neu starten
/stoppen                        → Server stoppen
/serverstatus                   → Aktuellen Status abrufen

/ban <spieler> [grund]          → Name(n) auf die Nitrado-Banliste setzen (Komma = mehrere)
/ban_entfernen <spieler>        → Name(n) von der Nitrado-Banliste entfernen
/banlist                        → Nitrado-Banliste anzeigen (Servereinstellungen)

/whitelist add <spieler>        → Name(n) auf die Nitrado-Whitelist setzen (Komma = mehrere)
/whitelist remove <spieler>     → Name(n) von der Nitrado-Whitelist entfernen
/whitelist show                 → Nitrado-Whitelist anzeigen (Servereinstellungen)
/send whitelist panel <panel_channel> <admin_channel>
                                → Whitelist-Anmelde-Panel senden. Spieler tragen per
                                  Button ihren PSN-Namen ein, Admins geben die Anfrage
                                  im admin_channel per Button frei/ab; bei Freigabe wird
                                  der Name automatisch zur Nitrado-Whitelist hinzugefügt.

/admin_position                 → Letzte bekannte Positionen aller Spieler
/spieler_suche <name>           → Spieler in den Logs suchen
/log_status                     → Log-Polling Status anzeigen
/ftp_scan                       → FTP-Verzeichnisse neu scannen
/hilfe                          → Diese Hilfe

SHOP & ECONOMY & CASINO
───────────────────────
Spieler-Befehle (kein Admin nötig):
/balance [@user]                → Wallet & Bank anzeigen
/deposit [menge]                → Wallet → Bank (ohne Menge = alles)
/withdraw [menge]               → Bank → Wallet (ohne Menge = alles)
/work  /daily  /beg             → Geld verdienen (Cooldowns in config.json)
/blackjack <einsatz>            → Blackjack mit Hit/Stand-Buttons
/roulette <einsatz> <wette>     → red/black/even/odd/low/high oder Zahl 0-36
/slots <einsatz>                → Slot-Maschine
/shop list [kategorie]          → Item-Katalog (ohne Kategorie: Übersicht)
/buy <item> <menge> <x> <z> [y] → Item kaufen & an Koordinate spawnen lassen
   x = iZurvive X (Ost, 1. Zahl) | z = iZurvive Y (Nord, 2. Zahl)
   y = Höhe (optional, Standard: default_pos_y aus config.json)

Economy-Admin-Befehle (admin_role_ids / economy_admin_role_ids):
/addmoney <@user> <betrag>      → Guthaben hinzufügen
/removemoney <@user> <betrag>   → Guthaben abziehen (nie unter 0)
/setbalance <@user> <betrag>    → Wallet exakt setzen
/shop pending                   → Offene (noch nicht gespawnte) Käufe
/shop check                     → Delivery-Diagnose: prüft cfgEffectArea.json und
   trägt fehlende Einträge offener Käufe automatisch wieder ein
/shop cleanup                   → Lieferungen abschließen + verwaiste SHOP_-Einträge entfernen
/shop setprice <item> <preis>   → Item-Preis ändern
/shop enable <item> <true/false>→ Item im Shop (de)aktivieren
/add shopitem <classnames> <preis> → Item/Bundle in den Katalog aufnehmen
   Anzeigename = Classname; mehrere Classnames (Komma getrennt) = Bundle,
   dann spawnen alle Items zusammen an der Kauf-Koordinate
/bundle add                     → Bundle per Formular anlegen: Kategorie im
   Dropdown wählen, dann Items zeilenweise als "2xClassname" (mit Menge),
   Name, Preis und Max-Kauf-Limit eingeben
/edit shopitem <item> [...]     → Classnames, Preis, Name, Kategorie oder
   Max-Menge eines vorhandenen Items/Bundles ändern
/shop removeitem <item>         → Item/Bundle aus dem Katalog löschen
/economy_reload                 → config.json + Katalog neu laden (ohne Bot-Neustart)

ITEM-KATALOG (shop_items.json)
──────────────────────────────
Der große Katalog wird beim Bot-Start AUTOMATISCH aus deiner types.xml
erzeugt, falls shop_items.json noch fehlt (types.xml einfach in den
Bot-Ordner legen – der Generator steckt im Bot selbst, keine 2. Datei).
Neu generieren: shop_items.json löschen und den Bot neu starten;
per /add shopitem angelegte Items bleiben dabei erhalten.
Preise pro Kategorie: shop_category_prices in config.json; einzeln per
/shop setprice oder /edit shopitem. Existiert shop_items.json, hat sie
Vorrang vor der kleinen shop_items-Liste in config.json.

ITEM-AUSLIEFERUNG (cfgEffectArea.json)
──────────────────────────────────────
Käufe werden als Einträge in mpmissions/<mission>/cfgEffectArea.json
geschrieben (Type = Item-Classname). Die Items spawnen beim NÄCHSTEN
Server-Neustart an der angegebenen Koordinate.
- auto_restart_after_purchase=true → der Bot startet den Server nach
  einem Kauf automatisch neu (gesammelt über restart_cooldown_seconds).
- Nach dem Neustart entfernt der Bot die Einträge automatisch wieder,
  sonst würden die Items bei JEDEM Neustart erneut spawnen.
- Vor jedem Schreiben wird ein Backup (cfgEffectArea.json.bak) angelegt.
- Guthaben liegt in economy.db (SQLite) – Datei sichern = Economy sichern.

EINRICHTUNG IM DASHBOARD (4 Schritte)
─────────────────────────────────────
Nitrado-Token → Server auswählen → Discord-Server-ID → "Hast du den Bot
bereits auf deinem Server?".
Schritt 3 registriert alle /-Befehle SOFORT für diesen Discord-Server
(ohne Server-ID registriert Discord global, das dauert bis zu 24 Stunden)
und trägt die ID in config.json unter guild_ids ein – vorhandene Einträge
bleiben erhalten. Server-ID: Discord → Einstellungen → Erweitert →
Entwicklermodus, dann Rechtsklick auf den Server → Server-ID kopieren.
Schritt 4 bietet den Einladen-Link an, falls der Bot noch nicht auf dem
Discord-Server ist. Beide Schritte erscheinen nur, solange in guild_ids
noch kein echter Server steht.

WEB-DASHBOARD IM BROWSER ÖFFNEN
────────────────────────────────
Die Adresse steht beim Start im Log ([DASHBOARD] ✅ ...). Gebunden wird an
0.0.0.0 – 0.0.0.0 und 127.0.0.1 sind nur LOKALE Adressen und gehören nicht
in die Browserzeile; nimm die Adresse deines Servers samt Port.

Der Port gehört dazu, z. B. http://dein-server.example:25590
Trage deine Adresse als "dashboard_public_host" in die config.json ein,
dann steht im Log direkt der fertige Link.

http:// ODER https:// ?
Beides geht – derselbe Port nimmt an, was der Browser schickt. Wichtig,
weil Browser getippte Adressen oft von allein auf https:// hochstufen;
früher endete das in ERR_SSL_PROTOCOL_ERROR und die Seite lud gar nicht.
Das Zertifikat erzeugt der Bot selbst (selbstsigniert), deshalb warnt der
Browser bei https:// einmalig – "Erweitert" → "Weiter zur Seite".
Ohne Warnung geht es nur mit einem echten Zertifikat, z. B. über einen
Cloudflare Tunnel auf 127.0.0.1:<Port>.
Abschalten: "dashboard_https": false in der config.json (dann nur http://).

MEHRERE DISCORD-SERVER (Guilds)
────────────────────────────────
Trage in config.json unter "guild_ids" mehrere IDs ein:
  "guild_ids": [111111111111111111, 222222222222222222]
Jeder Server hat seine eigene Channel-Konfiguration.

NITRADO KONSOLEN-SERVER – LOG-PFADE
────────────────────────────────────
Der Bot sucht beim Start automatisch in folgenden Pfaden:
  /games/<user>/noftp/dayz/config
  /games/<user>/noftp/dayz/profiles
  /dayz/config
  /dayz/profiles
  ...und weiteren typischen Nitrado-Pfaden.
"""
    # README neu schreiben, wenn sie fehlt oder noch eine alte Version ist
    needs_readme = True
    if os.path.exists("README.txt"):
        try:
            with open("README.txt", "r", encoding="utf-8") as f:
                # Marker mitziehen, wenn oben etwas ergänzt wird – sonst
                # bekommen bestehende Installationen die neue Fassung nie.
                needs_readme = "EINRICHTUNG IM DASHBOARD" not in f.read()
        except Exception:
            needs_readme = True
    if needs_readme:
        with open("README.txt", "w", encoding="utf-8") as f:
            f.write(readme)
        print("[SETUP] README.txt erstellt/aktualisiert.")


# ══════════════════════════════════════════════════════════════
#  Konfigurations-Manager
# ══════════════════════════════════════════════════════════════
class ConfigManager:
    def __init__(self):
        self.config:    Dict = {}
        self.guilds:    Dict = {}
        self.bans:      Dict = {}
        self.log_state: Dict = {}
        self.whitelist_reqs: Dict = {}

    def load_all(self):
        _create_helper_files()
        self.config    = self._load_or_create(CONFIG_FILE,    DEFAULT_CONFIG)
        self.guilds    = self._load_or_create(GUILDS_FILE,    {})
        self.bans      = self._load_or_create(BANLIST_FILE,   {})
        self.log_state = self._load_or_create(LOG_STATE_FILE, {})
        self.whitelist_reqs = self._load_or_create(WHITELIST_REQ_FILE, {})
        # Fehlende neue Felder (Shop/Economy/Casino) in bestehende config.json ergänzen
        if self._merge_defaults(self.config, DEFAULT_CONFIG):
            self.save_config()
            log.info("[CONFIG] config.json um neue Standard-Felder ergänzt.")
        # Verbundene Nitrado-Server laden. Beim ersten Start nach dem Update
        # wird daraus eine bestehende Einzelserver-Einrichtung uebernommen.
        connections.load()
        _migriere_eigene_einstellungen()
        _migriere_ban_metadaten()
        _migriere_feed_typen()

    def _merge_defaults(self, target: Dict, defaults: Dict) -> bool:
        """Ergänzt fehlende Keys rekursiv, ohne vorhandene Werte zu überschreiben."""
        changed = False
        for key, val in defaults.items():
            if key not in target:
                target[key] = val
                changed = True
            elif isinstance(val, dict) and isinstance(target.get(key), dict):
                if self._merge_defaults(target[key], val):
                    changed = True
        return changed

    def reload_config(self) -> bool:
        """Lädt config.json zur Laufzeit neu (für /economy_reload)."""
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                self.config = json.load(f)
            if self._merge_defaults(self.config, DEFAULT_CONFIG):
                self.save_config()
            return True
        except Exception as e:
            log.error(f"[CONFIG] Reload fehlgeschlagen: {e}")
            return False

    def _load_or_create(self, path: str, default: Any) -> Any:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=2)
            log.info(f"[CONFIG] '{path}' wurde neu erstellt.")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, path: str, data: Any):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def save_config(self):   self.save(CONFIG_FILE,    self.config)
    def save_guilds(self):   self.save(GUILDS_FILE,    self.guilds)
    def save_bans(self):     self.save(BANLIST_FILE,   self.bans)
    def save_log_state(self):self.save(LOG_STATE_FILE, self.log_state)
    def save_whitelist_reqs(self): self.save(WHITELIST_REQ_FILE, self.whitelist_reqs)

    def server_feeds(self, guild_id: int, service_id: str,
                     anlegen: bool = False) -> Dict[str, Any]:
        """Die Feed-Ebene EINES Nitrado-Servers innerhalb einer Guild.

        Aufbau: ``guilds[gid]["servers"][service_id][log_type] = channel_id``.
        Die alten flachen Werte auf Guild-Ebene bleiben als Rueckfall stehen,
        solange die Guild nur einen Server hat (siehe ``get_channel``).
        """
        gid = str(guild_id)
        if anlegen:
            eimer = self.guilds.setdefault(gid, {}).setdefault("servers", {})
            return eimer.setdefault(str(service_id), {})
        eimer = (self.guilds.get(gid) or {}).get("servers") or {}
        wert = eimer.get(str(service_id))
        return wert if isinstance(wert, dict) else {}

    def _guild_hat_mehrere_server(self, guild_id: int) -> bool:
        try:
            return len(connections.all_for_guild(guild_id)) > 1
        except Exception:  # noqa: BLE001 – Registry evtl. noch nicht geladen
            return False

    SEEN_PLAYERS_MAX = 1000

    def seen_players(self, guild_id: int,
                     service_id: Optional[str] = None) -> List[str]:
        """Ingame-Namen, die auf DIESEM Server schon gesehen wurden – Grundlage
        für die Allowlist-Autofill bei Zonen.

        Mit ``service_id`` genau die Namen dieses Servers; ohne (Altbestand,
        Guild-Ebene) die gemeinsam gemerkten. Vorher lagen alle Server einer
        Guild in einem Topf: das Zonenformular von Server B schlug Namen vor,
        die nur auf A gesehen wurden, und eine Auswahl konnte auf B einen
        gleichnamigen Spieler unbeabsichtigt von den Alarmen ausnehmen.
        """
        eintrag = self.guilds.get(str(guild_id)) or {}
        if service_id:
            je_server = eintrag.get("seen_players_je_server")
            if isinstance(je_server, dict):
                werte = je_server.get(str(service_id))
                if isinstance(werte, list):
                    return werte
            # Noch nichts server-eigenes gemerkt: der gemeinsame Altbestand
            # ist besser als eine leere Liste.
        werte = eintrag.get("seen_players")
        return werte if isinstance(werte, list) else []

    def record_seen_player(self, guild_id: int, name: str,
                           service_id: Optional[str] = None) -> None:
        """Merkt sich einen Ingame-Namen (case-insensitiv dedupliziert, auf die
        letzten SEEN_PLAYERS_MAX gedeckelt) – je Server UND auf Guild-Ebene.

        Die Guild-Ebene bleibt fuer den Altbestand und fuer Guilds mit genau
        einem Server erhalten; die Aufteilung je Server verhindert, dass sich
        die Spielerlisten mehrerer Server vermischen.
        """
        name = (name or "").strip()
        if not name:
            return
        eintrag = self.guilds.setdefault(str(guild_id), {})
        geaendert = False

        def _merken(liste: Optional[list]) -> Optional[list]:
            seen = liste if isinstance(liste, list) else []
            if name.lower() in {str(n).lower() for n in seen}:
                return None
            seen = seen + [name]
            if len(seen) > self.SEEN_PLAYERS_MAX:
                seen = seen[-self.SEEN_PLAYERS_MAX:]
            return seen

        neu = _merken(eintrag.get("seen_players"))
        if neu is not None:
            eintrag["seen_players"] = neu
            geaendert = True
        if service_id:
            je_server = eintrag.get("seen_players_je_server")
            if not isinstance(je_server, dict):
                je_server = {}
            neu_srv = _merken(je_server.get(str(service_id)))
            if neu_srv is not None:
                je_server[str(service_id)] = neu_srv
                eintrag["seen_players_je_server"] = je_server
                geaendert = True
        if geaendert:
            self.save_guilds()

    def get_channel(self, guild_id: int, log_type: str,
                    service_id: Optional[str] = None) -> Optional[int]:
        """Feed-Channel eines Log-Typs – bevorzugt der des genannten Servers.

        Der Rueckfall auf die Guild-Ebene gilt nur, solange die Guild GENAU
        EINEN Server hat. Ab zwei Servern wuerde er beide in denselben Channel
        posten – genau das soll die Trennung verhindern. Der Bestandsserver
        behaelt seine Channels durch die Migration in ``uebernimm_guild_feeds``.

        Seit den Feed-Einstellungen kann der gespeicherte Wert ZWEI Formen
        haben: die blosse Kanal-ID (Bot-Feeds wie shop_log, /setup feeds) oder
        ein Dict mit Farbe, Location und Notiz (die feinen Feed-Typen aus dem
        Dashboard). ``_kanal_aus`` nimmt beides an, damit kein Aufrufer etwas
        von dem Unterschied wissen muss.
        """
        if service_id:
            eigen = self._kanal_aus(self.server_feeds(guild_id, service_id).get(log_type))
            if eigen:
                return eigen
            if self._guild_hat_mehrere_server(guild_id):
                return None
        return self._kanal_aus((self.guilds.get(str(guild_id)) or {}).get(log_type))

    @staticmethod
    def _kanal_aus(wert: Any) -> Optional[int]:
        """Kanal-ID aus einem Feed-Eintrag – egal ob Zahl oder Einstellungs-Dict."""
        if isinstance(wert, dict):
            wert = wert.get("channel_id")
        if wert in (None, "", 0, "0"):
            return None
        try:
            return int(wert)
        except (TypeError, ValueError):
            return None

    # Vorgabewerte eines Feeds, solange der Kunde nichts anderes gesetzt hat.
    def feed_settings(self, guild_id: int, feed_key: str,
                      service_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Alle Einstellungen eines Feeds oder None, wenn er nicht gesetzt ist."""
        roh = self.server_feeds(guild_id, service_id).get(feed_key) if service_id else None
        if roh is None:
            if service_id and self._guild_hat_mehrere_server(guild_id):
                return None
            roh = (self.guilds.get(str(guild_id)) or {}).get(feed_key)
        kanal = self._kanal_aus(roh)
        if not kanal:
            return None
        meta = FEED_TYPES.get(feed_key) or {}
        if not isinstance(roh, dict):
            roh = {}
        return {
            "channel_id": kanal,
            "colour": roh.get("colour", meta.get("farbe", 0x5865F2)),
            "location": bool(roh.get("location", True)),
            "footer_ts": bool(roh.get("footer_ts", False)),
            "note": str(roh.get("note") or ""),
        }

    def set_channel(self, guild_id: int, log_type: str, channel_id: int,
                    service_id: Optional[str] = None):
        gid = str(guild_id)
        if service_id:
            self.server_feeds(guild_id, service_id, anlegen=True)[log_type] = channel_id
        else:
            self.guilds.setdefault(gid, {})[log_type] = channel_id
        self.save_guilds()

    def uebernimm_guild_feeds(self, guild_id: int, service_id: str) -> bool:
        """Die flachen Guild-Feeds diesem Server zuschreiben.

        Wird aufgerufen, sobald eine Guild ihren ZWEITEN Server bekommt: der
        bisherige behaelt alles, was bis dahin guild-weit eingestellt war, und
        der neue startet mit leeren Feeds statt in dieselben Channels zu posten.
        """
        gid = str(guild_id)
        oben = self.guilds.get(gid) or {}
        umzug = {k: v for k, v in oben.items()
                 if k in LOG_TYPES or k in ("whitelist_request", "status_message_id")}
        if not umzug:
            return False
        eigen = self.server_feeds(guild_id, service_id, anlegen=True)
        for k, v in umzug.items():
            eigen.setdefault(k, v)
        self.save_guilds()
        log.info(f"[FEED] Guild {gid}: {len(umzug)} Feed-Einstellungen an Server "
                 f"{service_id} uebergeben.")
        return True

    def is_valid(self) -> Tuple[bool, List[str]]:
        # Nur der Discord-Bot-Token ist Pflicht. Der Nitrado-Token wird per
        # /setup token im Discord gesetzt (oder optional hier eingetragen);
        # service_id + FTP-Zugang erkennt der Bot dann automatisch.
        errors = []
        placeholders = ["HIER", "TOKEN", "EINTRAGEN", "1111111", "2222222"]
        for key in ["bot_token"]:
            val = str(self.config.get(key, ""))
            if not val or any(p in val for p in placeholders):
                errors.append(key)
        return len(errors) == 0, errors

    def has_nitrado_token(self) -> bool:
        """True, wenn ein echter Nitrado-Token gesetzt ist (kein Platzhalter)."""
        val = str(self.config.get("nitrado_token") or "").strip()
        return bool(val) and "HIER" not in val and "EINTRAGEN" not in val

cfg = ConfigManager()


# ══════════════════════════════════════════════════════════════
#  Nitrado API-Client
# ══════════════════════════════════════════════════════════════
class NitradoAPI:
    def __init__(self, token: str, service_id: str, base: str = "https://api.nitrado.net"):
        self.token      = token
        self.service_id = service_id
        self.base       = base.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    def _headers(self) -> Dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def _s(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers())
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, endpoint: str) -> Tuple[bool, str]:
        try:
            s = await self._s()
            url = f"{self.base}/services/{self.service_id}/gameservers{endpoint}"
            async with s.post(url) as r:
                data = await r.json()
                if r.status in (200, 201, 204):
                    return True, data.get("message", "Erfolgreich")
                return False, data.get("message", f"HTTP {r.status}")
        except Exception as e:
            return False, f"Verbindungsfehler: {e}"

    async def restart(self) -> Tuple[bool, str]:
        return await self._post("/restart")

    async def stop(self) -> Tuple[bool, str]:
        return await self._post("/stop")

    async def get_info(self) -> Optional[Dict]:
        try:
            s = await self._s()
            url = f"{self.base}/services/{self.service_id}/gameservers"
            async with s.get(url) as r:
                if r.status == 200:
                    data = await r.json()
                    return data.get("data", {}).get("gameserver")
        except Exception as e:
            log.error(f"[NITRADO] get_info: {e}")
        return None

    async def get_settings(self) -> Optional[Dict]:
        """Gameserver-Settings (Nitrado-Webinterface 'Einstellungen') als Dict
        {kategorie: {key: value}}. None bei API-Fehler."""
        info = await self.get_info()
        if not info:
            return None
        settings = info.get("settings")
        return settings if isinstance(settings, dict) else None

    async def set_setting(self, category: str, key: str, value: str) -> Tuple[bool, str]:
        """Setzt ein einzelnes Gameserver-Setting (z. B. die Banliste) über
        POST /gameservers/settings."""
        try:
            s = await self._s()
            url = f"{self.base}/services/{self.service_id}/gameservers/settings"
            payload = {"category": category, "key": key, "value": value}
            async with s.post(url, json=payload) as r:
                try:
                    data = await r.json()
                except Exception:
                    data = {}
                if r.status in (200, 201, 204):
                    return True, data.get("message", "Erfolgreich")
                return False, data.get("message", f"HTTP {r.status}")
        except Exception as e:
            return False, f"Verbindungsfehler: {e}"

    async def download_file(self, path: str) -> Optional[bytes]:
        try:
            s = await self._s()
            url = f"{self.base}/services/{self.service_id}/gameservers/file_server/download"
            async with s.get(url, params={"file": path}) as r:
                if r.status == 200:
                    data = await r.json()
                    dl_url = data.get("data", {}).get("token", {}).get("url")
                    if dl_url:
                        async with s.get(dl_url) as dr:
                            return await dr.read()
        except Exception as e:
            log.error(f"[NITRADO] download_file: {e}")
        return None

    # ── Auto-Erkennung (Service-ID, FTP-Zugang, Karte) ────────────
    async def list_services(self) -> List[Dict]:
        """GET /services – alle Services des Tokens. Leere Liste bei Fehler."""
        try:
            s = await self._s()
            async with s.get(f"{self.base}/services") as r:
                if r.status == 200:
                    data = await r.json()
                    services = data.get("data", {}).get("services", [])
                    return services if isinstance(services, list) else []
                log.error(f"[NITRADO] list_services: HTTP {r.status}")
        except Exception as e:
            log.error(f"[NITRADO] list_services: {e}")
        return []

    async def detect_service(self) -> Optional[str]:
        """Sucht den DayZ-Gameserver unter allen Services des Tokens und setzt
        self.service_id. Bevorzugt Services mit 'dayz' im Spiel-/Detailnamen,
        bei mehreren Kandidaten den ersten aktiven. None, wenn nichts gefunden."""
        services = await self.list_services()
        gameservers = [s for s in services
                       if str(s.get("type", "")).lower() == "gameserver"]

        def _is_dayz(svc: Dict) -> bool:
            details = svc.get("details") or {}
            text = " ".join(str(v) for v in (details.get("game"),
                                             details.get("name"),
                                             details.get("folder_short"))
                            if v).lower()
            return "dayz" in text

        candidates = [s for s in gameservers if _is_dayz(s)] or gameservers
        if not candidates:
            log.error("[NITRADO] Kein Gameserver-Service unter diesem Token gefunden.")
            return None
        active = [s for s in candidates
                  if str(s.get("status", "")).lower() == "active"]
        chosen = (active or candidates)[0]
        if len(candidates) > 1:
            overview = ", ".join(
                f"{s.get('id')} ({(s.get('details') or {}).get('name') or (s.get('details') or {}).get('game') or '?'})"
                for s in candidates)
            log.warning(f"[NITRADO] Mehrere Gameserver gefunden: {overview} – "
                        f"nutze Service {chosen.get('id')}. Zum Überschreiben "
                        f"service_id in config.json setzen.")
        self.service_id = str(chosen.get("id"))
        log.info(f"[NITRADO] ✅ Service-ID automatisch erkannt: {self.service_id}")
        return self.service_id

    @staticmethod
    def extract_ftp_credentials(info: Dict) -> Optional[Dict[str, Any]]:
        """Liest die FTP-Zugangsdaten aus den Gameserver-Infos (credentials.ftp)."""
        ftp = ((info or {}).get("credentials") or {}).get("ftp") or {}
        host     = ftp.get("hostname") or ftp.get("host")
        user     = ftp.get("username") or ftp.get("user")
        password = ftp.get("password")
        if not (host and user and password):
            return None
        try:
            port = int(ftp.get("port") or 21)
        except (TypeError, ValueError):
            port = 21
        return {"host": str(host), "port": port,
                "user": str(user), "password": str(password)}

    @staticmethod
    def extract_map(info: Dict) -> Optional[str]:
        """Ermittelt die aktuell laufende Karte aus den Gameserver-Infos:
        zuerst query.map (Server online), sonst Mission/Map aus den Settings."""
        info = info or {}
        m = _canonical_map_name(str((info.get("query") or {}).get("map") or ""))
        if m:
            return m
        settings = info.get("settings")
        if isinstance(settings, dict):
            for cat in settings.values():
                if not isinstance(cat, dict):
                    continue
                for key in ("mission", "map", "current_map", "mapname"):
                    m = _canonical_map_name(str(cat.get(key) or ""))
                    if m:
                        return m
        return None


# ══════════════════════════════════════════════════════════════
#  A2S UDP-Ping (Valve Server Query – kein extra Paket nötig)
#  Liefert Echtzeit-Spielerzahl, Servername und Map direkt
#  vom Spielserver – unabhängig von der Nitrado-API.
# ══════════════════════════════════════════════════════════════
import socket
import struct

def a2s_query(ip: str, port: int, timeout: float = 3.0) -> Optional[Dict]:
    """
    Sendet eine A2S_INFO Anfrage an den DayZ-Server und gibt
    ein Dict mit Serverinfos zurück, oder None bei Fehler.
    Funktioniert nur wenn der Server online ist.
    """
    if not ip:
        return None
    try:
        # A2S_INFO Request Payload
        payload = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(timeout)
            sock.sendto(payload, (ip, int(port)))
            data, _ = sock.recvfrom(4096)
        finally:
            sock.close()   # Socket auch bei Timeout/Fehler schließen

        if len(data) < 6:
            return None

        # Challenge-Response Handling (neuere Server)
        if data[4:5] == b"\x41":
            challenge = data[5:9]
            payload2 = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00" + challenge
            sock2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock2.settimeout(timeout)
                sock2.sendto(payload2, (ip, int(port)))
                data, _ = sock2.recvfrom(4096)
            finally:
                sock2.close()

        # A2S_INFO Response parsen (0x49 = 'I')
        if data[4:5] != b"\x49":
            return None

        offset = 6  # Header (4) + Typ (1) + Protocol (1)

        def read_str(d: bytes, pos: int):
            end = d.index(b"\x00", pos)
            return d[pos:end].decode("utf-8", errors="replace"), end + 1

        name,    offset = read_str(data, offset)
        mapname, offset = read_str(data, offset)
        folder,  offset = read_str(data, offset)
        game,    offset = read_str(data, offset)
        offset += 2  # App-ID (short)

        players     = data[offset];     offset += 1
        max_players = data[offset];     offset += 1
        bots        = data[offset];     offset += 1
        server_type = chr(data[offset]); offset += 1
        environment = chr(data[offset]); offset += 1
        visibility  = data[offset];     offset += 1

        return {
            "name":        name,
            "map":         mapname,
            "players":     players,
            "max_players": max_players,
            "bots":        bots,
            "type":        server_type,
            "password":    visibility == 1,
            "game":        game,
        }
    except (socket.timeout, socket.gaierror):
        return None
    except Exception as e:
        log.debug(f"[A2S] Query Fehler ({ip}:{port}): {e}")
        return None


# So oft darf die Dateigröße in Folge nicht ermittelbar sein (z.B. FTP-Server
# antwortet im ASCII-Modus mit einem SIZE-Fehler), bevor _poll_connection den
# Offline-Ueberspring-Zweig verlaesst und einfach weiterliest, statt fuer
# immer im selben Zyklus haengen zu bleiben.
SIZE_FEHLER_GRENZE = 3

# Wie oft je Server auf eine neue .RPT (= Serverneustart) geprueft wird.
# Nicht in jedem Poll-Zyklus: die Abfrage kostet eine eigene FTP-Runde, aber
# Neustarts kommen nur ein paar Mal am Tag vor. Siehe _pruefe_neustart.
_RPT_PRUEF_ABSTAND = 60.0


# ══════════════════════════════════════════════════════════════
#  FTP-Manager  (sync, läuft in ThreadPoolExecutor)
# ══════════════════════════════════════════════════════════════
class FTPManager:
    # Typische Nitrado-Pfade für DayZ Konsolen-Server
    NITRADO_SEARCH_PATHS = [
        "/games/{user}/noftp/dayz/config",
        "/games/{user}/noftp/dayz/profiles",
        "/games/{user}/noftp/dayz",
        "/noftp/dayz/config",
        "/noftp/dayz/profiles",
        "/dayz/config",
        "/dayz/profiles",
        "/dayz",
        "/",
    ]

    # Typische Orte des mpmissions-Ordners (Konsole: dayzps = PS4/PS5, dayzxb = Xbox)
    MPMISSIONS_SEARCH_PATHS = [
        "/dayzps_missions",
        "/dayzxb_missions",
        "/games/{user}/noftp/dayz/mpmissions",
        "/noftp/dayz/mpmissions",
        "/dayzps/mpmissions",
        "/dayzxb/mpmissions",
        "/dayz/mpmissions",
        "/mpmissions",
    ]

    def __init__(self, host: str, port: int, user: str, password: str):
        self.host     = host
        self.port     = int(port)
        self.user     = user
        self.password = password
        # Eine gehaltene FTP-Verbindung mit Reconnect-Fallback statt 2–3 neuer
        # Verbindungen pro Poll-Zyklus – schneller und schont den Nitrado-Server.
        self._ftp: Optional[ftplib.FTP] = None
        self._ftp_lock = threading.Lock()   # Methoden laufen in Executor-Threads
        self.consecutive_failures = 0       # Zähler für die Adminlog-Ausfall-Warnung
        self.last_error: str = ""

    def _connect(self) -> ftplib.FTP:
        ftp = ftplib.FTP()
        ftp.connect(self.host, self.port, timeout=30)
        ftp.login(self.user, self.password)
        ftp.encoding = "utf-8"
        # Binaermodus SOFORT erzwingen, nicht erst beim ersten retrbinary()
        # (das setzt TYPE I selbst, aber eben erst dann): direkt nach dem
        # Login steht die Verbindung im FTP-Standard ASCII (TYPE A). Ein
        # SIZE-Aufruf VOR dem ersten Lesen (siehe file_size_or_none, laeuft
        # in jedem Zyklus VOR read_from_offset) laeuft dann noch im
        # ASCII-Modus, waehrend der gespeicherte offset aus binaeren
        # read_from_offset()-Aufrufen stammt. Uebersetzt der Server dabei
        # \r\n -> \n (ASCII-Modus-Definition), meldet SIZE weniger Bytes als
        # tatsaechlich gelesen wurden -> offset > current_size wird faelschlich
        # als "Datei geschrumpft" (Neustart) gedeutet, die ganze ADM-Datei
        # nochmal von vorne gelesen und JEDES Ereignis erneut gepostet. Tritt
        # nach jedem Reconnect auf (Verbindung schlaeft ein, NOOP schlaegt
        # fehl) - nicht nur beim eigentlichen Bot-Start.
        try:
            ftp.voidcmd("TYPE I")
        except Exception as e:
            log.warning(f"[FTP] TYPE I fehlgeschlagen ({self.host}): {e}")
        return ftp

    def _drop_conn(self):
        if self._ftp is not None:
            try:
                self._ftp.close()
            except Exception:
                pass
            self._ftp = None

    def _with_conn(self, op):
        """Führt op(ftp) auf der gehaltenen Verbindung aus. Ist der Socket tot
        (Timeout, Server-Trennung), wird genau einmal neu verbunden und wiederholt.
        error_perm (Pfad/Rechte) gilt nicht als Verbindungsfehler."""
        with self._ftp_lock:
            for attempt in (1, 2):
                try:
                    if self._ftp is None:
                        self._ftp = self._connect()
                    else:
                        self._ftp.voidcmd("NOOP")   # lebt die Verbindung noch?
                    result = op(self._ftp)
                    self.consecutive_failures = 0
                    self.last_error = ""
                    return result
                except ftplib.error_perm:
                    # Server hat geantwortet → Verbindung ok, nur Pfad/Rechte-Problem
                    self.consecutive_failures = 0
                    raise
                except Exception as e:
                    self._drop_conn()
                    if attempt == 2:
                        self.consecutive_failures += 1
                        self.last_error = str(e)
                        raise

    def _effect_area_file_in(self, mission_dir: str) -> str:
        """Realer Pfad der cfgEffectArea.json im Mission-Ordner. Ein bereits
        vorhandenes File (beliebige Schreibweise) hat Vorrang – FTP kann
        case-sensitiv sein und der Server liest nur die Original-Datei."""
        for entry in self.list_dir(mission_dir):
            if entry.split("/")[-1].lower() == "cfgeffectarea.json":
                return entry
        return f"{mission_dir.rstrip('/')}/cfgEffectArea.json"

    @staticmethod
    def _full_path(directory: str, name: str) -> str:
        """Gibt den vollständigen Pfad zurück – egal ob NLST absolute oder relative Namen liefert."""
        if name.startswith("/"):
            return name
        return f"{directory.rstrip('/')}/{name}"

    def list_adm_files(self, directory: str) -> List[str]:
        def op(ftp):
            raw: List[str] = []
            ftp.cwd(directory)
            ftp.retrlines("NLST", raw.append)
            return raw
        try:
            raw = self._with_conn(op)
        except ftplib.error_perm:
            return []
        except Exception as e:
            log.debug(f"[FTP] list_adm_files({directory}): {e}")
            return []
        # NLST gibt nach cwd() oft nur Dateinamen ohne Pfad zurück →
        # immer den vollständigen Pfad zusammenbauen
        return sorted([
            self._full_path(directory, f)
            for f in raw
            if f.split("/")[-1].lower().endswith(".adm")
        ])

    def list_rpt_files(self, directory: str) -> List[str]:
        """Die .RPT-Dateien im Log-Ordner.

        Jeder Serverstart legt eine neue an, der Name traegt Datum und
        Uhrzeit (DayZServer_..._20260808_142104.RPT). Mehr wird fuer die
        Neustart-Erkennung nicht gebraucht – die Datei selbst (mehrere MB,
        fast nur Motor-Meldungen) wird bewusst NICHT geladen.
        """
        def op(ftp):
            raw: List[str] = []
            ftp.cwd(directory)
            ftp.retrlines("NLST", raw.append)
            return raw
        try:
            raw = self._with_conn(op)
        except ftplib.error_perm:
            return []
        except Exception as e:  # noqa: BLE001
            log.debug(f"[FTP] list_rpt_files({directory}): {e}")
            return []
        return sorted([
            self._full_path(directory, f)
            for f in raw
            if f.split("/")[-1].lower().endswith(".rpt")
        ])

    def list_dir(self, directory: str) -> List[str]:
        def op(ftp):
            raw: List[str] = []
            ftp.cwd(directory)
            ftp.retrlines("NLST", raw.append)
            return raw
        try:
            raw = self._with_conn(op)
        except ftplib.error_perm:
            return []
        except Exception as e:
            log.debug(f"[FTP] list_dir({directory}): {e}")
            return []
        # Vollständige Pfade zurückgeben
        return [self._full_path(directory, e) for e in raw]

    def read_file(self, path: str) -> Optional[str]:
        def op(ftp):
            buf = io.BytesIO()
            ftp.retrbinary(f"RETR {path}", buf.write)
            return buf.getvalue()
        try:
            return self._with_conn(op).decode("utf-8", errors="replace")
        except Exception as e:
            log.debug(f"[FTP] read_file({path}): {e}")
            return None

    def read_file_ex(self, path: str) -> Tuple[Optional[str], str]:
        """Wie read_file, unterscheidet aber 'Datei fehlt' von echten Fehlern.
        Status: 'ok' (Inhalt gelesen), 'missing' (550 – Datei existiert nicht),
        'error' (Verbindung/Timeout/Rechte – Inhalt UNBEKANNT; die Datei darf
        dann NICHT als leer behandelt/überschrieben werden!)."""
        def op(ftp):
            buf = io.BytesIO()
            ftp.retrbinary(f"RETR {path}", buf.write)
            return buf.getvalue()
        try:
            return self._with_conn(op).decode("utf-8", errors="replace"), "ok"
        except ftplib.error_perm as e:
            if str(e).lstrip().startswith("550"):
                return None, "missing"
            log.warning(f"[FTP] read_file_ex({path}): {e}")
            return None, "error"
        except Exception as e:
            log.warning(f"[FTP] read_file_ex({path}): {e}")
            return None, "error"

    def read_from_offset(self, path: str, offset: int) -> Tuple[Optional[str], int]:
        """Liest ab ``offset`` weiter.

        Rueckgabe ``(inhalt, neuer_offset)``. **``inhalt is None`` heisst
        FEHLER**, ``""`` heisst "nichts Neues". Frueher lieferten beide Faelle
        ``""`` – ein kaputter FTP-Zugang war damit von einem stillen Server
        nicht zu unterscheiden, und der Betreiber sah nur "es wird nichts
        gepostet", ohne jede Fehlermeldung.
        """
        def op(ftp):
            buf = io.BytesIO()
            ftp.retrbinary(f"RETR {path}", buf.write, rest=offset if offset > 0 else None)
            return buf.getvalue()
        try:
            raw = self._with_conn(op)
        except Exception as e:
            log.warning(f"[FTP] Log-Datei nicht lesbar – {path} ab Offset {offset}: {e}")
            return None, offset
        # Der Server schreibt die ADM-Datei live – die letzte Zeile kann noch
        # unvollständig sein. Nur bis zum letzten Zeilenumbruch verarbeiten,
        # der Rest wird beim nächsten Poll (dann vollständig) gelesen.
        # Sonst ginge das Event der halben Zeile für immer verloren.
        if raw and not raw.endswith(b"\n"):
            cut = raw.rfind(b"\n")
            if cut == -1:
                return "", offset
            raw = raw[:cut + 1]
        return raw.decode("utf-8", errors="replace"), offset + len(raw)

    def file_size_or_none(self, path: str) -> Optional[int]:
        """Dateigroesse, oder ``None`` wenn sie sich nicht ermitteln laesst.

        Getrennt von ``file_size``, weil dort ein Fehler als ``0`` erscheint –
        und 0 ist eine gueltige Groesse. Manche FTP-Server beantworten ``SIZE``
        im ASCII-Modus mit einem Fehler; das darf der Aufrufer nicht als
        "Datei ist leer" missverstehen.

        TYPE I wird HIER nochmal erzwungen, nicht nur einmal in ``_connect``:
        ``list_adm_files`` (NLST) laeuft ueber ``ftp.retrlines()``, und die
        schickt laut ftplib-Quellcode **unbedingt** ``TYPE A`` vor jeder
        Zeilen-Uebertragung – das hebt den Binaermodus aus ``_connect`` bei
        JEDEM Poll-Zyklus wieder auf, noch bevor diese Methode drankommt.
        Ohne das hier waere die Verbindung beim SIZE-Aufruf wieder im
        ASCII-Modus (siehe Docstring oben) und manche Server (bestaetigt:
        "550 SIZE not allowed in ASCII mode") lehnen SIZE dort komplett ab.
        """
        def op(ftp):
            ftp.voidcmd("TYPE I")
            return ftp.size(path)
        try:
            wert = self._with_conn(op)
        except Exception as e:
            log.warning(f"[FTP] Dateigröße nicht ermittelbar – {path}: {e}")
            return None
        return int(wert) if wert is not None else None

    def write_file(self, path: str, content: str) -> bool:
        # Atomar: erst als .tmp hochladen, dann umbenennen – bricht die Verbindung
        # mitten im Upload ab, bleibt die Zieldatei unversehrt
        tmp = path + ".tmp"
        def op(ftp):
            buf = io.BytesIO(content.encode("utf-8"))
            ftp.storbinary(f"STOR {tmp}", buf)
            try:
                ftp.rename(tmp, path)
            except ftplib.error_perm:
                # RNTO überschreibt nicht auf jedem FTP-Server → Ziel löschen,
                # aber erst hier (Zieldatei so kurz wie möglich weg)
                ftp.delete(path)
                ftp.rename(tmp, path)
            return True
        try:
            return bool(self._with_conn(op))
        except Exception as e:
            log.error(f"[FTP] write_file({path}): {e}")
            return False

    def delete_file(self, path: str) -> bool:
        """Löscht eine Datei; False wenn nicht vorhanden/kein Zugriff."""
        def op(ftp):
            ftp.delete(path)
            return True
        try:
            return bool(self._with_conn(op))
        except ftplib.error_perm:
            return False      # Datei existiert nicht → nichts zu tun
        except Exception as e:
            log.debug(f"[FTP] delete_file({path}): {e}")
            return False

    def discover_paths(self, map_name: str = "ChernarusPlus") -> Dict[str, str]:
        """Durchsucht alle bekannten Nitrado-Pfade und gibt gefundene Verzeichnisse zurück.
        Findet zusätzlich mpmissions/<mission>/cfgEffectArea.json für die Shop-Auslieferung."""
        found: Dict[str, str] = {}

        # FTP-Benutzernamen für Pfad-Templates extrahieren
        user_part = self.user.split("_")[0] if "_" in self.user else self.user

        search_paths = []
        for p in self.NITRADO_SEARCH_PATHS:
            search_paths.append(p.replace("{user}", user_part))

        for path in search_paths:
            adm = self.list_adm_files(path)
            if adm:
                log.info(f"[FTP] ✅ ADM-Logs gefunden: {path} ({len(adm)} Dateien)")
                found["log_dir"] = path
                # ban.txt suchen – list_dir gibt jetzt vollständige Pfade zurück
                entries = self.list_dir(path)
                ban_entries = [e for e in entries if "ban" in e.split("/")[-1].lower()]
                if ban_entries:
                    found["ban_file"] = ban_entries[0]
                else:
                    found["ban_file"] = f"{path.rstrip('/')}/ban.txt"
                break

        if "log_dir" not in found:
            log.warning("[FTP] ⚠️  Keine ADM-Log-Dateien gefunden. "
                        "Prüfe FTP-Zugangsdaten und Pfade in config.json.")

        # ── mpmissions/<mission>/cfgEffectArea.json suchen ────────
        mission_candidates: List[str] = []
        # Aus gefundenem Log-Verzeichnis ableiten (z.B. /dayzps/config → /dayzps/mpmissions)
        if "log_dir" in found:
            base = found["log_dir"].rstrip("/")
            for suffix in ("/config", "/profiles"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            if base:
                mission_candidates.append(f"{base}/mpmissions")
        for p in self.MPMISSIONS_SEARCH_PATHS:
            cand = p.replace("{user}", user_part)
            if cand not in mission_candidates:
                mission_candidates.append(cand)

        wanted = _mission_folder_for_map(map_name).lower()
        for mp_dir in mission_candidates:
            entries = self.list_dir(mp_dir)
            missions = [e for e in entries if "dayzoffline" in e.split("/")[-1].lower()]
            if not missions:
                continue
            # Bevorzugt den Ordner der konfigurierten Map, sonst den ersten Treffer
            mission = next((m for m in missions
                            if m.split("/")[-1].lower() == wanted), missions[0])
            found["mission_dir"]     = mission
            found["cfg_effect_area"] = self._effect_area_file_in(mission)
            log.info(f"[FTP] ✅ Mission-Ordner gefunden: {mission}")
            break

        if "cfg_effect_area" not in found:
            # Fallback: begrenzte Breitensuche über den FTP-Baum (Idee aus Referenzbot)
            hit = self._find_mission_bfs()
            if hit:
                found["mission_dir"]     = hit
                found["cfg_effect_area"] = self._effect_area_file_in(hit)
                log.info(f"[FTP] ✅ Mission-Ordner per Breitensuche gefunden: {hit}")

        if "cfg_effect_area" not in found:
            log.warning("[FTP] ⚠️  Kein mpmissions-Ordner gefunden – für die Shop-Auslieferung "
                        "muss cfg_effect_area_path in config.json manuell gesetzt werden.")

        return found

    def _find_mission_bfs(self, max_depth: int = 4, max_dirs: int = 200) -> Optional[str]:
        """Begrenzte Breitensuche nach einem dayzOffline.*-Mission-Ordner –
        Fallback, wenn die festen Kandidatenpfade nichts liefern."""
        queue: List[Tuple[str, int]] = [("/", 0)]
        seen = 0
        while queue and seen < max_dirs:
            current, depth = queue.pop(0)
            seen += 1
            for e in self.list_dir(current):
                name = e.split("/")[-1].lower()
                if name.startswith("dayzoffline"):
                    return e
                # Nur vermutliche Verzeichnisse (ohne Punkt) weiterverfolgen
                if depth + 1 < max_depth and "." not in name:
                    queue.append((e, depth + 1))
        return None


# ══════════════════════════════════════════════════════════════
#  Standort-Datenbank & iZurvive-Hilfsfunktionen
#  Koordinaten: X=Ost, Y=Nord, Z=Höhe  (DayZ pos=<X,Y,Z>)
# ══════════════════════════════════════════════════════════════
_MAP_LOCATIONS: Dict[str, List[Tuple[str, float, float]]] = {
    "ChernarusPlus": [
        ("Chernogorsk",          2946, 7693),
        ("Elektrozavodsk",       4452, 9839),
        ("Novodmitrovsk",        6730, 14350),
        ("Severograd",           4455, 13900),
        ("Krasnostav",           8990, 12700),
        ("Svetlojarsk",          9870, 12200),
        ("Berezino",             8481, 9150),
        ("Solnichny",            7510, 12700),
        ("Zelenogorsk",          2730, 5110),
        ("Pavlovo",              2880, 3480),
        ("Vybor",                3960, 5980),
        ("Green Mountain",       2560, 5760),
        ("Kabanino",             4470, 6560),
        ("Stary Sobor",          5110, 7240),
        ("Novy Sobor",           5520, 7850),
        ("Grishino",             4050, 7890),
        ("Guglovo",              6900, 9300),
        ("Polana",               6600, 10600),
        ("Mogilevka",            5890, 8720),
        ("Pusta",                6100, 8100),
        ("Vavilovo",             7370, 8880),
        ("Nadezhdino",           2800, 9400),
        ("Kamyshovo",            6190, 11300),
        ("Kamenka",              1010, 6070),
        ("Balota",               1790, 6970),
        ("Komarovo",             1600, 8800),
        ("Prigorodki",           2760, 7860),
        ("Krasnoe",              2130, 11150),
        ("Sinystok",             2000, 12100),
        ("Novaya Petrovka",      3610, 13500),
        ("Tisy Military Base",   1050, 13800),
        ("Kumyrna",              3800, 11600),
        ("Lopatino",             3780, 9970),
        ("NWAF",                 4640, 10350),
        ("NE Airfield",          8180, 13200),
        ("Karer Krasnaya Zarya", 8658, 12823),
        ("Dubrovka",             6910, 9900),
        ("Vyshnoye",             7340, 10900),
        ("Gorka",                6610, 9640),
        ("Vysotovo",             5680, 9190),
        ("Rogovo",               3760, 6920),
    ],
    "Livonia": [
        ("Nadbor",       6000, 5000),
        ("Sitnik",       4000, 2500),
        ("Topolin",      1800, 1800),
        ("Radacz",       3200, 4200),
        ("Bialy Brzeg",  8000, 3000),
        ("Grabin",       9000, 7000),
        ("Flintstone",   11000, 6000),
        ("Lukow",        2000, 8000),
        ("Puszcza",      4500, 7000),
        ("Polana",       6500, 8500),
        ("Dabrowa",      8500, 9000),
        ("Losino",       10000, 9500),
        ("Tarnow",       5500, 11000),
    ],
    "Sakhal": [
        ("Klen",        5500, 5500),
        ("Kvoshnino",   3000, 7000),
        ("Tikhaya Bay", 8000, 6000),
        ("Rikhov",      6000, 9000),
        ("Sever",       7500, 10000),
        ("Tulga",       9000, 8000),
        ("Podgorsk",    4000, 10000),
        ("Volcanka",    2500, 4000),
    ],
}

def _nearest_location(x: float, y: float, map_name: str = "ChernarusPlus") -> Optional[str]:
    """Gibt den Namen des nächsten Orts zurück (max. 1500 m Radius)."""
    locs = _MAP_LOCATIONS.get(map_name, _MAP_LOCATIONS["ChernarusPlus"])
    if not locs:
        return None
    nearest = min(locs, key=lambda l: (l[1] - x) ** 2 + (l[2] - y) ** 2)
    dist = ((nearest[1] - x) ** 2 + (nearest[2] - y) ** 2) ** 0.5
    return nearest[0] if dist <= 1500 else None

# Kanonischer Map-Name -> Pfad-Baustein der iZurvive-Adresse.
_IZURVIVE_MAP_SLUG = {
    "ChernarusPlus": "chernarusplus",
    "Livonia": "livonia",
    "Sakhal": "sakhal",
}


def _izurvive_url(x: float, y: float, map_name: str = "ChernarusPlus",
                  z: float = 0.0) -> str:
    """Deep-Link MIT Markierung an der Stelle (https://www.izurvive.com/
    <karte>/#location=X;Y;Z). Das alte Format (?m=<karte>#l=X;Y) zentrierte
    iZurvive nur auf die Koordinate, setzte aber keine Markierung – man
    musste die Stelle auf der Karte erst wieder suchen."""
    slug = _IZURVIVE_MAP_SLUG.get(map_name, map_name.lower().replace(" ", ""))
    return f"https://www.izurvive.com/{slug}/#location={x:.0f};{y:.0f};{z:.0f}"

def _location_field_value(pos_str: Optional[str]) -> Optional[str]:
    """
    Parst 'X, Y, Z' aus pos_str (DayZ pos=<X,Y,Z>: X=Ost, Y=Nord, Z=Hoehe)
    und gibt einen Discord-Markdown-String mit klickbarem iZurvive-Link zurueck.
    """
    if not pos_str:
        return None
    parts = [p.strip() for p in pos_str.split(",")]
    if len(parts) < 3:
        return None
    try:
        x = float(parts[0])
        y = float(parts[1])
        z = float(parts[2])
    except ValueError:
        return None
    map_name = _aktuelle_karte()
    url  = _izurvive_url(x, y, map_name, z)
    loc  = _nearest_location(x, y, map_name)
    near = f"\n*(Near {loc})*" if loc else ""
    return f"[{x:.1f}, {y:.1f}, {z:.1f}]({url}){near}"


# ══════════════════════════════════════════════════════════════
#  DayZ Log-Parser
#  Quelle: Nitrado DayZ Konsolen-Server .ADM Logs
# ══════════════════════════════════════════════════════════════

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
                     r'|folded|packed|deployed|mounted|unmounted|buried|unburied'
                     r'|raised|lowered)\s+([^\n]+)',
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

    def _set_position(self, name: str, player_id: Optional[str], pos: str):
        self.player_positions[name] = {
            "id": player_id,
            "position": pos.strip(),
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }

    def _extract_ts(self, line: str) -> str:
        ts_m = re.match(r'^(\d{2}:\d{2}:\d{2})\s*\|?\s*', line)
        return ts_m.group(1) if ts_m else ""

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

        pos_m = re.search(r'pos\s*=\s*<([\d., \-]+)>', line, re.IGNORECASE)
        if pos_m:
            self._set_position(p["name"], p["id"], pos_m.group(1))

        return {
            "type": "kill_env",
            "timestamp": ts,
            "player": p["name"],
            "player_id": p["id"] or "Unbekannt",
            "cause": cause,
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

        pos_m = re.search(r'pos\s*=\s*<([\d., \-]+)>', line, re.IGNORECASE)
        if pos_m:
            self._set_position(victim["name"], victim["id"], pos_m.group(1))

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
            "distance": "?",
            "raw": line,
        }

    def parse_line(self, line: str):
        line = line.strip()
        if not line:
            return None

        ts = self._extract_ts(line)

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
                    "raw": line,
                }

        # 3) Environment death
        m = self.P["kill_env"].search(line)
        if m and "killed by player" not in line.lower():
            # Gruppe 3 = "by <Ursache>", Gruppe 5 = "due to <Ursache>"
            cause = m.group(3) or m.group(5)
            if not cause:
                ev = self._generic_env_death_event(line, ts)
                if ev:
                    return ev
                cause = "Unbekannte Ursache"
            return {
                "type": "kill_env",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                "cause": cause.strip() if isinstance(cause, str) else "Unbekannte Ursache",
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
                "raw": line,
            }

        # 5) Verbindungen
        m = self.P["connect"].search(line)
        if m:
            pos = m.group(3)
            if pos:
                self._set_position(m.group(1), m.group(2), pos)
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
            if pos:
                self._set_position(m.group(1), m.group(2), pos)
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

        # 8) Basis-Bau
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
                    if pos_m:
                        self._set_position(p["name"], p["id"], pos_m.group(1))
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
                    "raw": line,
                }

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


# ══════════════════════════════════════════════════════════════
#  Discord Embed-Builder
# ══════════════════════════════════════════════════════════════
def _footer(ev: Dict) -> str:
    return f"🕐 {ev['timestamp']}" if ev.get("timestamp") else ""

def _dist(d: str) -> str:
    return f"{d} m" if d != "?" else "Nah­kampf"

def _add_location_field(e: discord.Embed, ev: Dict, player_key: str,
                        positions: Optional[Dict[str, Dict]] = None):
    """Fügt das '📍 • Player Location'-Feld hinzu (gleiches Aussehen wie bei
    Connect/Disconnect): Position aus dem Event selbst oder die zuletzt
    getrackte Position des Spielers, als klickbarer iZurvive-Link."""
    name = ev.get(player_key) or ""
    pos = ev.get("position") or (positions or {}).get(name, {}).get("position")
    loc_val = _location_field_value(pos)
    if loc_val:
        e.add_field(name="📍 • Player Location", value=loc_val, inline=False)


_LOCATION_FELD = "📍 • Player Location"


def _feed_anwenden(e: discord.Embed,
                   feed: Optional[Dict[str, Any]]) -> discord.Embed:
    """Die Einstellungen eines Feeds auf das fertige Embed legen.

    Bewusst als Nachbearbeitung und nicht in den dreizehn Zweigen von
    ``build``: so bleibt jeder Zweig unveraendert, und ohne ``feed`` kommt
    exakt das heraus, was der Bot bisher verschickt hat.
    """
    if not feed:
        return e
    try:
        if feed.get("colour") is not None:
            e.colour = discord.Colour(int(feed["colour"]))
        if not feed.get("location", True):
            for i, f in enumerate(list(e.fields)):
                if f.name == _LOCATION_FELD:
                    e.remove_field(i)
                    break
        if feed.get("footer_ts"):
            e.timestamp = datetime.now(timezone.utc)
    except Exception:  # noqa: BLE001 – Darstellung darf den Versand nie kippen
        pass
    return e


class EmbedBuilder:
    @staticmethod
    def build(ev: Dict,
              positions: Optional[Dict[str, Dict]] = None,
              feed: Optional[Dict[str, Any]] = None) -> Optional[discord.Embed]:
        t = ev["type"]
        if t == "kill_pvp":
            e = discord.Embed(
                title="☠️ KILL",
                description=f"**{ev['killer']}** hat **{ev['victim']}** getötet",
                color=0xE74C3C
            )
            e.add_field(name="Waffe",       value=ev["weapon"],         inline=True)
            e.add_field(name="Distanz",     value=_dist(ev["distance"]),inline=True)
            _add_location_field(e, ev, "victim", positions)
            e.add_field(name="Killer ID",   value=f"`{ev['killer_id']}`",  inline=False)
            e.add_field(name="Opfer ID",    value=f"`{ev['victim_id']}`",  inline=False)

        elif t == "suicide":
            e = discord.Embed(
                title="💀 SELBSTMORD",
                description=f"**{ev['player']}** hat sein Leben beendet",
                color=0x7F8C8D
            )
            _add_location_field(e, ev, "player", positions)
            e.add_field(name="Steam-ID", value=f"`{ev['player_id']}`", inline=False)

        elif t == "kill_env":
            e = discord.Embed(
                title="☠️ TOD",
                description=f"**{ev['player']}** ist gestorben",
                color=0xE67E22
            )
            e.add_field(name="Ursache",  value=ev["cause"],              inline=True)
            _add_location_field(e, ev, "player", positions)
            e.add_field(name="Steam-ID", value=f"`{ev['player_id']}`",   inline=False)

        elif t == "damage":
            e = discord.Embed(
                title="🩸 TREFFER",
                description=f"**{ev['attacker']}** trifft **{ev['victim']}**",
                color=0xFF6B35
            )
            e.add_field(name="Schaden",     value=f"{ev['damage']} HP",  inline=True)
            e.add_field(name="Körperteil",  value=ev["hit_zone"],         inline=True)
            e.add_field(name="Waffe",       value=ev["weapon"],           inline=True)
            e.add_field(name="Distanz",     value=_dist(ev["distance"]),  inline=True)
            _add_location_field(e, ev, "victim", positions)

        elif t == "connect":
            e = discord.Embed(
                title=f"→ • Connect • {ev.get('timestamp', '')}",
                description=f"**{ev['player']}** connected to the game server.",
                color=0x5865F2
            )
            _add_location_field(e, ev, "player", positions)
            e.add_field(name="Steam-ID", value=f"`{ev['player_id']}`", inline=False)

        elif t == "disconnect":
            e = discord.Embed(
                title=f"← • Disconnect • {ev.get('timestamp', '')}",
                description=f"**{ev['player']}** left the game server.",
                color=0xE74C3C
            )
            _add_location_field(e, ev, "player", positions)
            e.add_field(name="Steam-ID", value=f"`{ev['player_id']}`", inline=False)

        elif t == "connecting":
            e = discord.Embed(
                title=f"↔ • Connecting • {ev.get('timestamp', '')}",
                description=f"**{ev['player']}** verbindet sich...",
                color=0x3498DB
            )
            _add_location_field(e, ev, "player", positions)
            e.add_field(name="Steam-ID", value=f"`{ev['player_id']}`", inline=False)

        elif t == "chat":
            icons = {"side":"📢","direct":"💬","vehicle":"🚗","megaphone":"📣","radio":"📻"}
            icon = icons.get(ev["channel"].lower(), "💬")
            e = discord.Embed(
                title=f"{icon} CHAT [{ev['channel'].upper()}]",
                description=f"**{ev['player']}**: {ev['message']}",
                color=0x5865F2
            )

        elif t == "admin_action":
            e = discord.Embed(
                title="🛡️ ADMIN AKTION",
                description=f"**{ev['admin']}** hat einen Befehl ausgeführt",
                color=0xF1C40F
            )
            if ev.get("command"):
                e.add_field(name="Befehl", value=f"`{ev['command']}`", inline=False)
            e.add_field(name="Admin-ID", value=f"`{ev['admin_id']}`", inline=False)

        elif t == "basebuild":
            e = discord.Embed(
                title="🏗️ BASIS-BAU",
                description=f"**{ev['player']}** hat gebaut: **{ev['item']}**",
                color=0x8B4513
            )
            _add_location_field(e, ev, "player", positions)
            e.add_field(name="Steam-ID", value=f"`{ev['player_id']}`", inline=False)

        elif t == "vehicle":
            e = discord.Embed(
                title="🚗 FAHRZEUG-EREIGNIS",
                description=ev["raw"],
                color=0xFF8C00
            )

        elif t == "loot":
            action_icons = {"spawned":"✅", "despawned":"❌", "created":"✅", "deleted":"❌"}
            icon = action_icons.get(ev.get("action","").lower(), "📦")
            e = discord.Embed(
                title=f"{icon} LOOT",
                description=f"**{ev.get('item','Unbekannt')}** → {ev.get('action','?')}",
                color=0x9B59B6
            )
        elif t == "server_restart":
            e = discord.Embed(
                title="♻️ SERVER NEU GESTARTET",
                description="Der Gameserver wurde neu gestartet.",
                color=0x2ECC71
            )
            if ev.get("gestartet"):
                e.add_field(name="Startzeit", value=ev["gestartet"], inline=True)
            e.add_field(name="Logdatei", value=f"`{ev.get('datei', '?')}`", inline=False)

        elif t in ("unconscious", "conscious"):
            bewusstlos = t == "unconscious"
            e = discord.Embed(
                title="😵 BEWUSSTLOS" if bewusstlos else "🙂 WIEDER BEI BEWUSSTSEIN",
                description=(f"**{ev['player']}** ist bewusstlos" if bewusstlos
                             else f"**{ev['player']}** ist wieder bei Bewusstsein"),
                color=0x8E44AD if bewusstlos else 0x9B59B6
            )
            _add_location_field(e, ev, "player", positions)
            e.add_field(name="Steam-ID", value=f"`{ev['player_id']}`", inline=False)

        else:
            return None

        if t in ("connect", "disconnect", "connecting"):
            if ev.get("timestamp"):
                e.set_footer(text=f"Server Time: {ev['timestamp']}")
        elif _footer(ev):
            e.set_footer(text=_footer(ev))
        return _feed_anwenden(e, feed)


# ══════════════════════════════════════════════════════════════
#  Verbundene Nitrado-Server (Mehrmandanten-Betrieb)
# ══════════════════════════════════════════════════════════════
#  Frueher gab es genau einen Nitrado-Server: Token, Service-ID, FTP-Zugang
#  und Karte standen global in der config.json. Fuer mehrere Kunden braucht
#  jeder Server seinen eigenen Satz dieser Werte plus die Discord-Guild, die
#  ihn verwalten darf. Beides liegt jetzt in connections.json, geschluesselt
#  nach Service-ID.
#
#  Die config.json bleibt als Rueckfallebene bestehen: Werte, die eine
#  Verbindung nicht selbst kennt, werden weiterhin von dort gelesen. So
#  funktioniert eine bestehende Installation unveraendert weiter.

CONNECTIONS_FILE = "connections.json"

# Werte, die nur einen bestimmten Server betreffen und deshalb aus der
# config.json in die jeweilige Verbindung wandern.
_CONN_SERVER_FIELDS = (
    "ftp_host", "ftp_port", "ftp_user", "ftp_password",
    "ftp_log_dir", "ftp_ban_file", "ftp_profile_dir", "ftp_mission_dir",
    "cfg_effect_area_path", "map_name", "server_ip", "query_port", "rcon_port",
    "zones", "auto_restart_schedule",
    "nitrado_ban_category", "nitrado_ban_key",
    "nitrado_whitelist_category", "nitrado_whitelist_key",
)


class ServerConnection:
    """Ein verbundener Nitrado-Server mit allem, was nur ihn betrifft.

    Haelt neben den Stammdaten auch die Laufzeit-Objekte (NitradoAPI, FTP,
    Log-Parser), damit spaeter jeder Server unabhaengig gepollt werden kann.
    """

    def __init__(self, data: Dict[str, Any]):
        self.data: Dict[str, Any] = data
        self.api: Optional[NitradoAPI] = None
        self.ftp: Optional[FTPManager] = None
        self.parser: Optional[DayZLogParser] = None
        self.shop: Optional[Any] = None
        self._catalog: Optional[Any] = None
        # Lief die FTP-Auto-Erkennung fuer DIESEN Server schon?
        self.discovered: bool = False
        # FTP-Warnzustand je Server (sonst verschluckt ein Kunde die Warnung
        # eines anderen) und Zeitpunkt, seit dem er als online gilt
        self.ftp_warned_ts: float = 0.0
        self.ftp_warn_active: bool = False
        self.online_since: Optional[float] = None
        # Zeitpunkt des letzten Discovery-Versuchs (Wiederholsperre je Server)
        self.discover_retry_ts: float = 0.0
        # Wann zuletzt auf eine neue .RPT (Serverneustart) geprueft wurde.
        # Die Abfrage kostet eine eigene FTP-Runde und muss deshalb nicht in
        # jedem 10s-Zyklus laufen – siehe _pruefe_neustart.
        self.rpt_geprueft_ts: float = 0.0
        # Grund, aus dem der Poll-Zyklus diesen Server gerade uebergeht
        # (None = laeuft normal). Nur bei einer AENDERUNG geloggt (siehe
        # _poll_zustand_melden) – sonst waere das Terminal bei einem
        # dauerhaft kaputten FTP-Zugang alle 10s vollgeschrieben.
        self.poll_zustand: Optional[str] = None
        # Wie oft die Dateigroesse in Folge nicht zu ermitteln war. Ab
        # SIZE_FEHLER_GRENZE wird trotzdem weitergelesen, statt ewig im
        # Ueberspring-Zweig zu haengen (siehe _poll_connection).
        self.size_fehler: int = 0
        # Diagnose: die letzten Dispatch-Entscheidungen dieses Servers (reiner
        # Arbeitsspeicher). Beantwortet "kommt es an, und wenn nicht warum"
        # ohne dass der Betreiber das Terminal mitlesen muss.
        self.dispatch_verlauf: Deque[Dict[str, Any]] = deque(maxlen=30)

    # ── Stammdaten ──
    @property
    def service_id(self) -> str:
        return str(self.data.get("service_id") or "")

    @property
    def name(self) -> str:
        return str(self.data.get("name") or f"Server {self.service_id}")

    @property
    def token(self) -> str:
        return str(self.data.get("nitrado_token") or "")

    @property
    def guild_id(self) -> Optional[int]:
        try:
            gid = int(self.data.get("guild_id") or 0)
        except (TypeError, ValueError):
            return None
        return gid or None

    @property
    def log_state(self) -> Dict[str, Any]:
        state = self.data.get("log_state")
        if not isinstance(state, dict):
            state = {}
            self.data["log_state"] = state
        return state

    # Werte, die NIEMALS aus der globalen config nachgeladen werden duerfen.
    # Sonst liest die Verbindung eines Kunden die Zugangsdaten eines anderen,
    # sobald dessen Daten in der config.json stehen.
    _KEINE_RUECKFALL_SCHLUESSEL = frozenset({
        "nitrado_token", "service_id", "ftp_host", "ftp_port", "ftp_user",
        "ftp_password", "ftp_log_dir", "ftp_ban_file", "ftp_profile_dir",
        "ftp_mission_dir", "cfg_effect_area_path", "server_ip", "query_port",
        "rcon_port", "zones", "shop_items_file", "types_xml_path",
        # Karte und Neustart-Zeitplan beschreiben ebenfalls genau einen Server –
        # geerbt wuerde sonst der Zeitplan des Betreibers auf fremden Servern
        # Neustarts ausloesen.
        "map_name", "auto_restart_schedule", "auto_restart_after_purchase",
        # Ban-/Whitelist-Feld auf dem Nitrado-Server: erbt ein Kunde hier die
        # Kategorie oder den Settings-Key eines anderen, liest und beschreibt
        # der Bot auf SEINEM Server das falsche Einstellungsfeld.
        "nitrado_ban_category", "nitrado_ban_key",
        "nitrado_whitelist_category", "nitrado_whitelist_key",
    })

    # Einstellungen, die jeder Kunde selbst festlegt. Rueckfallebene ist hier
    # NICHT die config.json des Betreibers, sondern die mitgelieferte Vorgabe –
    # sonst wuerde jede Aenderung des Betreibers (Waehrung, Belohnungen,
    # Casino-Einsaetze, Liefer-Parameter) bei allen Kunden mitwandern, die den
    # Wert noch nicht selbst gesetzt haben.
    _EIGENE_EINSTELLUNGEN = frozenset({
        "economy", "casino", "bounty",
        "currency_name", "currency_symbol", "starting_balance",
        "kill_reward", "playtime_reward",
        "shop_default_price", "shop_category_prices", "shop_categories_custom",
        "default_radius", "default_pos_y",
        "delivery_grace_seconds", "delivery_cleanup_delay_seconds",
        "delivery_online_wait_max_seconds",
        "restart_cooldown_seconds", "zone_ping_cooldown_seconds",
        "log_poll_interval_seconds", "ftp_fail_warn_cycles",
        "admin_role_ids", "admin_role_name", "economy_admin_role_ids",
    })

    def get(self, key: str, default: Any = None) -> Any:
        """Serverspezifischer Wert.

        Fuer unkritische Einstellungen gilt die globale config.json als
        Rueckfallebene. Fuer Zugangsdaten und Serverkennungen NICHT – dort
        wuerde der Rueckfall Daten fremder Kunden liefern.
        """
        if key in self.data:
            return self.data[key]
        if key in self._KEINE_RUECKFALL_SCHLUESSEL:
            return default
        if key in self._EIGENE_EINSTELLUNGEN:
            # Auslieferungs-Vorgabe statt der Einstellung des Betreibers
            return copy.deepcopy(DEFAULT_CONFIG.get(key, default))
        return cfg.config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    @property
    def catalog(self) -> "ShopCatalog":
        """Eigener Item-Katalog dieses Servers (beim ersten Zugriff geladen).

        Ohne diese Trennung teilten sich alle Kunden eine Item-Liste – Preise
        und Bundles des einen Servers landeten im Shop des anderen.
        """
        if self._catalog is None:
            self._catalog = ShopCatalog(self.service_id,
                                        path=str(self.data.get("shop_items_file") or ""))
            self._catalog.load()
        return self._catalog

    def masked_token(self) -> str:
        """Token fuer die Anzeige: nur die letzten vier Zeichen bleiben lesbar."""
        tok = self.token
        if not tok:
            return ""
        return "•" * max(8, len(tok) - 4) + tok[-4:]

    # ── Laufzeit-Objekte ──
    async def ensure_clients(self, force: bool = False) -> None:
        """NitradoAPI/FTP fuer diese Verbindung anlegen (idempotent)."""
        if force and self.api is not None:
            try:
                await self.api.close()
            except Exception:  # noqa: BLE001
                pass
            self.api = None
        if force:
            self.ftp = None

        if self.api is None and self.token and self.service_id:
            self.api = NitradoAPI(
                token=self.token,
                service_id=self.service_id,
                base=self.get("nitrado_api_base", "https://api.nitrado.net"),
            )
        if self.ftp is None and all(str(self.get(k) or "").strip()
                                    for k in ("ftp_host", "ftp_user", "ftp_password")):
            self.ftp = FTPManager(
                host=self.get("ftp_host"),
                port=self.get("ftp_port", 21),
                user=self.get("ftp_user"),
                password=self.get("ftp_password"),
            )
        # Eigener Parser je Server: er merkt sich Spielerpositionen, die sonst
        # zwischen den Servern durcheinandergeraten wuerden.
        if self.parser is None:
            self.parser = DayZLogParser()

    async def close(self) -> None:
        if self.api is not None:
            try:
                await self.api.close()
            except Exception:  # noqa: BLE001
                pass
            self.api = None
        self.ftp = None

    def view(self, with_token: bool = False) -> Dict[str, Any]:
        """Darstellung fuers Dashboard – der Token nur auf ausdrueckliche Bitte."""
        out = {
            "service_id": self.service_id,
            "name": self.name,
            "guild_id": (str(self.guild_id) if self.guild_id else None),
            "map_name": self.get("map_name"),
            "server_ip": self.get("server_ip") or None,
            "ftp_host": self.get("ftp_host") or None,
            "has_ftp": bool(self.get("ftp_host") and self.get("ftp_user")),
            "token_masked": self.masked_token(),
            # Vom Kunden im Onboarding genannte Guild – freigeschaltet wird sie
            # erst, wenn der Betreiber sie in der Serverliste zuordnet.
            "guild_id_requested": (str(self.data.get("guild_id_requested"))
                                   if self.data.get("guild_id_requested") else None),
        }
        if with_token:
            out["token"] = self.token
        return out


class ConnectionRegistry:
    """Alle verbundenen Nitrado-Server, geschluesselt nach Service-ID."""

    def __init__(self):
        self._conns: Dict[str, ServerConnection] = {}

    # ── Laden/Speichern ──
    def load(self) -> None:
        raw: Dict[str, Any] = {}
        if os.path.exists(CONNECTIONS_FILE):
            try:
                with open(CONNECTIONS_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, ValueError) as e:
                log.error(f"[CONN] {CONNECTIONS_FILE} nicht lesbar ({e}) – "
                          f"starte mit leerer Liste.")
                raw = {}
        self._conns = {str(sid): ServerConnection(data)
                       for sid, data in (raw or {}).items()
                       if isinstance(data, dict)}
        if not self._conns and self._migrate_from_config():
            self.save()

    def save(self) -> None:
        try:
            with open(CONNECTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump({sid: c.data for sid, c in self._conns.items()},
                          f, ensure_ascii=False, indent=2)
        except OSError as e:
            log.error(f"[CONN] {CONNECTIONS_FILE} nicht schreibbar: {e}")

    def _migrate_from_config(self) -> bool:
        """Bestehende Einzelserver-Installation uebernehmen.

        Ohne diesen Schritt stuende eine laufende Installation nach dem Update
        ohne Verbindung da. Die Werte bleiben zusaetzlich in der config.json –
        dort schadet die Kopie nicht und dient als Rueckfallebene.
        """
        service_id = str(cfg.config.get("service_id") or "").strip()
        if not service_id or not cfg.has_nitrado_token():
            return False

        guild_id = None
        for raw in (cfg.config.get("guild_ids") or []):
            try:
                gid = int(raw)
            except (TypeError, ValueError):
                continue
            if gid and gid not in _PLACEHOLDER_GUILD_IDS:
                guild_id = gid
                break

        data: Dict[str, Any] = {
            "service_id": service_id,
            "name": str(cfg.config.get("server_name") or "").strip()
                    or f"Server {service_id}",
            "nitrado_token": str(cfg.config.get("nitrado_token") or "").strip(),
            "guild_id": guild_id,
            "owner_discord_id": None,
            # Die bisherige Lese-Position wandert mit, sonst wuerden nach dem
            # Update alte Log-Zeilen erneut als Ereignisse gepostet.
            "log_state": dict(cfg.log_state or {}),
        }
        for key in _CONN_SERVER_FIELDS:
            if key in cfg.config:
                data[key] = cfg.config[key]

        self._conns[service_id] = ServerConnection(data)
        log.info(f"[CONN] Bestehende Einrichtung uebernommen: Service {service_id}"
                 + (f" → Guild {guild_id}" if guild_id else " (noch keiner Guild zugeordnet)"))
        return True

    # ── Zugriff ──
    def all(self) -> List[ServerConnection]:
        return list(self._conns.values())

    def for_service(self, service_id: Any) -> Optional[ServerConnection]:
        return self._conns.get(str(service_id or ""))

    def all_for_guild(self, guild_id: Any) -> List[ServerConnection]:
        """ALLE Server, die diese Discord-Guild verwalten darf.

        Eine Guild kann mehrere Nitrado-Server betreuen. Die Reihenfolge ist
        stabil: der Leitserver zuerst, danach alphabetisch. Ohne diese feste
        Reihenfolge haenge die Frage „welcher Server gilt, wenn keiner genannt
        wurde?" von der Reihenfolge in der connections.json ab.
        """
        try:
            gid = int(guild_id or 0)
        except (TypeError, ValueError):
            return []
        if not gid:
            return []
        treffer = [c for c in self._conns.values() if c.guild_id == gid]
        treffer.sort(key=lambda c: (not bool(c.data.get("guild_primary")),
                                    c.name.lower()))
        return treffer

    def for_guild(self, guild_id: Any) -> Optional[ServerConnection]:
        """Der **Leitserver** dieser Discord-Guild.

        Guild-weite Dinge – Waehrung, Startguthaben, Admin-Rollen, Economy –
        haengen an ihm, denn ``balances`` und ``links`` gehoeren der Guild, nicht
        dem einzelnen Server. Fuer serverbezogene Befehle NICHT verwenden:
        dort loest ``_conn_waehlen`` ueber den ``server``-Parameter auf.
        """
        treffer = self.all_for_guild(guild_id)
        return treffer[0] if treffer else None

    def set_guild_primary(self, service_id: Any) -> bool:
        """Diesen Server zum Leitserver seiner Guild machen."""
        conn = self.for_service(service_id)
        if conn is None or conn.guild_id is None:
            return False
        for other in self.all_for_guild(conn.guild_id):
            other.data.pop("guild_primary", None)
        conn.data["guild_primary"] = True
        self.save()
        return True

    def for_owner(self, discord_id: Any) -> List[ServerConnection]:
        """Alle Server, die diesem Discord-Konto gehoeren.

        Damit erkennt die Anmeldung einen Rueckkehrer wieder und muss den
        Nitrado-Token nicht erneut abfragen.
        """
        uid = str(discord_id or "").strip()
        if not uid:
            return []
        return [c for c in self._conns.values()
                if str(c.data.get("owner_discord_id") or "") == uid]

    def adopt_ownerless(self, discord_id: Any) -> List[ServerConnection]:
        """Herrenlose Verbindungen diesem Konto zuschreiben.

        Betrifft Server aus der Migration und aus ``/setup token`` – dort ist
        kein Discord-Konto bekannt. Wird bewusst nur fuer Konten mit der
        Dashboard-Admin-Rolle aufgerufen, sonst koennte sich der erste
        beliebige Anmelder den Server des Betreibers aneignen.
        """
        uid = str(discord_id or "").strip()
        if not uid:
            return []
        taken = [c for c in self._conns.values()
                 if not str(c.data.get("owner_discord_id") or "").strip()]
        for conn in taken:
            conn.data["owner_discord_id"] = uid
        if taken:
            self.save()
        return taken

    def primary(self) -> Optional[ServerConnection]:
        """Die Verbindung des Hauptservers – die aus der config.json, sonst die erste.

        Uebergangsloesung, solange Log-Abruf und Shop noch nicht pro Server
        laufen (Stufe 4 des Umbaus).

        Ist in der config.json eine ``service_id`` **festgeschrieben**, gilt sie
        allein: findet sich dazu keine Verbindung, gibt es keinen Hauptserver.
        Ohne diese Klammer rueckte beim Entfernen eines Servers der naechste
        nach – und erbte still den gesamten Altbestand ohne ``service_id``
        (Ankuendigungen, Ereignisse, shop_items.json, Ban-Angaben). Steht dort
        nichts, bleibt es beim bisherigen Rueckfall auf die erste Verbindung.
        """
        festgeschrieben = str(cfg.config.get("service_id") or "").strip()
        conn = self.for_service(festgeschrieben)
        if conn is not None:
            return conn
        if festgeschrieben:
            return None
        return next(iter(self._conns.values()), None)

    # ── Aendern ──
    def upsert(self, service_id: Any, **fields: Any) -> ServerConnection:
        sid = str(service_id)
        conn = self._conns.get(sid)
        if conn is None:
            conn = ServerConnection({"service_id": sid, "log_state": {}})
            self._conns[sid] = conn
        for key, value in fields.items():
            if value is not None:
                conn.data[key] = value
        self.save()
        return conn

    def assign_guild(self, service_id: Any, guild_id: Optional[int]) -> Tuple[bool, str]:
        """Guild einem Server zuordnen.

        Eine Guild darf MEHRERE Nitrado-Server verwalten. Wer das darf, pruefen
        die Aufrufer: der Betreiber frei, ein Kunde nur in einer Guild, in der
        ihm schon ein Server gehoert – sonst koennte er sich per zweitem Server
        in einen fremden Discord einklinken.
        """
        conn = self.for_service(service_id)
        if conn is None:
            return False, "Dieser Server ist nicht (mehr) verbunden."

        neu_in_guild = False
        bestehende: List[ServerConnection] = []
        if guild_id:
            bestehende = [c for c in self.all_for_guild(guild_id)
                          if c.service_id != conn.service_id]
            neu_in_guild = conn.guild_id != int(guild_id)

        conn.data["guild_id"] = int(guild_id) if guild_id else None
        self.save()

        # Bekommt die Guild damit ihren ZWEITEN Server, gehen die bisher
        # guildweiten Feed-Einstellungen an den Bestandsserver ueber. Sonst
        # wuerde der neue Server ueber den Rueckfall in dieselben Channels
        # posten – genau das soll die Trennung verhindern.
        if guild_id and neu_in_guild and len(bestehende) == 1:
            try:
                cfg.uebernimm_guild_feeds(int(guild_id), bestehende[0].service_id)
            except Exception as e:  # noqa: BLE001
                log.warning(f"[FEED] Uebergabe an {bestehende[0].service_id} "
                            f"fehlgeschlagen: {e}")

        if guild_id and len(bestehende) >= 1:
            return True, (f"Zuordnung gespeichert – diese Guild verwaltet jetzt "
                          f"{len(bestehende) + 1} Server.")
        return True, ("Zuordnung gespeichert." if guild_id else "Zuordnung entfernt.")

    def remove(self, service_id: Any) -> bool:
        """Eine Verbindung entfernen und die connections.json zurueckschreiben.

        Ohne das ``save()`` waere der Server nach dem naechsten Neustart wieder
        da – wie bei ``upsert`` und ``assign_guild`` gehoert das Speichern hierher.
        """
        entfernt = self._conns.pop(str(service_id), None) is not None
        if entfernt:
            self.save()
        return entfernt


connections = ConnectionRegistry()


# Befehle, die auch ohne zugeordneten Server laufen müssen – sonst käme man
# aus der Premium-Sperre nicht mehr heraus bzw. bekäme keine Hilfe mehr.
_PREMIUM_FREE_COMMANDS = ("setup", "hilfe")

PREMIUM_MISSING_TEXT = (
    "❌ **Du hast kein Premium**\n"
    "Dieser Discord-Server ist noch keinem Nitrado-Server zugeordnet. "
    "Der Bot-Betreiber schaltet ihn im Dashboard unter **Serverliste** frei."
)


# Der Server, um den es im gerade laufenden Befehl bzw. Poll-Zyklus geht.
# Damit lesen _cur_symbol/_fmt_money die Waehrung DIESES Kunden, ohne dass
# jede der ueber 60 Aufrufstellen eine Verbindung durchreichen muesste.
# contextvars sind pro asyncio-Task getrennt – ein Befehl beeinflusst also
# nie die Anzeige eines gleichzeitig laufenden Befehls einer anderen Guild.
_AKTUELLER_SERVER: "contextvars.ContextVar[Optional[ServerConnection]]" = \
    contextvars.ContextVar("aktueller_server", default=None)


def _setze_aktuellen_server(conn: Optional[ServerConnection]) -> None:
    try:
        _AKTUELLER_SERVER.set(conn)
    except Exception:  # noqa: BLE001
        pass


def _aktuelle_karte(conn: Optional[ServerConnection] = None) -> str:
    """Die Karte des gerade behandelten Servers – wie ``_cur_symbol`` bei der
    Waehrung. Vorher stand hier immer ``cfg.config["map_name"]``: ein
    Livonia- oder Sakhal-Kunde bekam dadurch Chernarus-iZurvive-Links und
    Ortsnamen, die auf seiner Karte gar nicht existieren."""
    conn = conn if conn is not None else _AKTUELLER_SERVER.get()
    if conn is not None:
        name = str(conn.get("map_name") or "").strip()
        if name:
            return name
    return cfg.config.get("map_name", "ChernarusPlus")


async def _betreiber_alarm(text: str, farbe: int = 0xE67E22) -> None:
    """Schickt eine kurze Meldung in den Alarm-Channel des Bot-BETREIBERS
    (siehe /betreiber alarm_channel) – nicht zu verwechseln mit irgendeinem
    Kunden-Channel. Ohne eingerichteten Channel passiert nichts, damit ein
    frischer Bot ohne Konfiguration nicht mit Fehlern im Log auffaellt."""
    cid = str(cfg.config.get("betreiber_alarm_channel_id") or "").strip()
    if not cid:
        return
    try:
        kanal = bot.get_channel(int(cid))
        if kanal is None:
            return
        await kanal.send(embed=discord.Embed(
            description=text, color=farbe, timestamp=datetime.now(timezone.utc)))
    except Exception as e:  # noqa: BLE001
        log.error(f"[BETREIBER-ALARM] Senden fehlgeschlagen: {e}")


async def _poll_zustand_melden(conn: ServerConnection, grund: Optional[str]) -> None:
    """Loggt, WARUM der Poll-Zyklus diesen Server gerade uebergeht oder wieder
    normal laeuft – aber nur bei einer AENDERUNG des Grundes, nicht bei jedem
    10s-Zyklus. Vorher liefen mehrere solcher Stellen komplett stumm (kein FTP
    aufgebaut, keine Guild zugeordnet, keine ADM-Dateien gefunden, dauerhaft
    haengender Offline-Ueberspring-Zweig) – das sah dann fuer den Betreiber
    exakt so aus wie "es passiert nichts auf dem Server", obwohl der Bot den
    Server gar nicht erst abgefragt hat. Meldet die AENDERUNG zusaetzlich in
    den Betreiber-Alarm-Channel (nicht nur ins fuer PebbleHost meist
    unerreichbare Terminal-Log) UND, falls eingerichtet, in den eigenen
    „Admin Action“-Feed des betroffenen Kunden – der erfaehrt sonst erst beim
    naechsten Dashboard-Besuch, dass z. B. sein Nitrado-Token abgelehnt wird."""
    if conn.poll_zustand == grund:
        return
    if grund:
        log.warning(f"[POLL] {conn.name}: übersprungen – {grund}")
        await _betreiber_alarm(f"⚠️ **{conn.name}**: {grund}", farbe=0xE74C3C)
        if conn.guild_id is not None:
            warnung = discord.Embed(
                title="⚠️ Automatische Betriebswarnung", description=grund,
                color=0xE74C3C, timestamp=datetime.now(timezone.utc))
            await _post_feed(conn.guild_id, "adminlog", warnung, service_id=conn.service_id)
    elif conn.poll_zustand:
        log.info(f"[POLL] {conn.name}: läuft wieder normal "
                 f"(vorheriger Grund war: {conn.poll_zustand}).")
        await _betreiber_alarm(f"✅ **{conn.name}** läuft wieder normal.", farbe=0x2ECC71)
        if conn.guild_id is not None:
            erholt = discord.Embed(
                title="✅ Läuft wieder normal",
                description=f"Vorheriger Grund: {conn.poll_zustand}",
                color=0x2ECC71, timestamp=datetime.now(timezone.utc))
            await _post_feed(conn.guild_id, "adminlog", erholt, service_id=conn.service_id)
    conn.poll_zustand = grund


def _historisch_parsen(conn: ServerConnection, text: str) -> Tuple[List[Dict],
                                                                  "DayZLogParser"]:
    """Alte Log-Zeilen parsen, **ohne** den Live-Parser zu verändern.

    ``/test`` und die Diagnose-Seite lesen bereits verarbeitete Zeilen ein
    zweites Mal. Mit ``conn.parser`` schrieben sie dabei historische
    Positionen ueber die aktuellen und setzten ``last_seen`` auf jetzt – der
    naechste Zonenlauf hielt eine laengst verlassene Position fuer frisch und
    konnte einen Fehlalarm ausloesen. Deshalb eine eigene Instanz.

    Die unerkannten Zeilen werden anschliessend bewusst in den Live-Parser
    uebernommen: genau die will der Betreiber auf der Diagnose-Seite sehen,
    und sie veraendern keinen Zustand, an dem etwas haengt.
    """
    eigen = DayZLogParser()
    events = eigen.parse_lines(text)
    if conn is not None and conn.parser is not None:
        for zeile in eigen.unerkannte_zeilen:
            if zeile not in conn.parser.unerkannte_zeilen:
                conn.parser.unerkannte_zeilen.append(zeile)
    return events, eigen


async def _premium_check(interaction: discord.Interaction) -> bool:
    """Laeuft vor jedem Slash-Befehl (CommandTree.interaction_check).

    Ohne zugeordneten Nitrado-Server ist der Discord-Server nicht
    freigeschaltet. Die Pruefung darf niemals eine Ausnahme nach oben
    durchlassen – sonst waeren im Zweifel alle Befehle tot.
    Nebenbei wird hier der Server des Befehls hinterlegt, damit Betraege in
    der Waehrung dieses Kunden angezeigt werden.
    """
    try:
        # Vorbelegung fuer die Waehrungsanzeige. Verwaltet die Guild MEHRERE
        # Server, waere jede Wahl hier geraten – dann gilt der Leitserver, und
        # _conn_waehlen setzt spaeter den tatsaechlich gemeinten Server.
        _setze_aktuellen_server(
            connections.for_guild(interaction.guild_id)
            if interaction.guild_id is not None else connections.primary())
        name = str(getattr(interaction.command, "qualified_name", "") or "")
        if name.split(" ")[0] in _PREMIUM_FREE_COMMANDS:
            return True
        if interaction.guild_id is None:
            return True                      # Direktnachricht: nichts zu sperren
        # Freigeschaltet ist die Guild, sobald ihr MINDESTENS ein Server gehoert.
        if connections.all_for_guild(interaction.guild_id):
            return True
    except Exception:  # noqa: BLE001
        return True

    try:
        if interaction.response.is_done():
            await interaction.followup.send(PREMIUM_MISSING_TEXT, ephemeral=True)
        else:
            await interaction.response.send_message(PREMIUM_MISSING_TEXT, ephemeral=True)
    except Exception:  # noqa: BLE001
        pass
    return False


# ══════════════════════════════════════════════════════════════
#  Befehlsnamen auf Englisch – fuer Mitglieder, deren EIGENE Discord-App
#  auf Englisch steht (Discords eingebaute Befehls-Lokalisierung, siehe
#  https://discord.com/developers/docs/interactions/application-commands#localization).
#  Hat nichts mit dem Sprache-Schalter im Web-Dashboard zu tun – der ist
#  eine reine Browser-Einstellung ohne Bezug zu Discords Befehlsliste, die
#  fuer den ganzen Server gleich ist. Nur echte deutsche Namen stehen hier;
#  ohnehin englische (z. B. "ban", "hackban", "stats") bleiben unveraendert.
# ══════════════════════════════════════════════════════════════
_BEFEHL_NAMEN_EN = {
    "neustart": "restart",
    "stoppen": "stop",
    "ban_entfernen": "ban_remove",
    "spieler_suche": "player_search",
    "hilfe": "help",
    "erstellen": "create",
    "liste": "list",
    "löschen": "delete",
    "ankuendigung": "announcement",
}


class _BefehlsUebersetzer(app_commands.Translator):
    """Uebersetzt nur die neun deutschen Befehlsnamen oben ins Englische,
    wenn Discord fuer einen Nutzer eine englische Anzeige anfragt (dessen
    eigene App-Sprache, nicht unser Dashboard). Alles andere (Beschreibungen,
    Parameter, bereits englische Namen) bekommt keinen locale_str und laeuft
    deshalb nie durch diese Funktion – so kann hier nichts aus Versehen
    uebersetzt werden, das gar nicht dafuer vorgesehen ist."""

    async def translate(self, string: app_commands.locale_str, locale: discord.Locale,
                        context: app_commands.TranslationContextTypes) -> Optional[str]:
        if locale not in (discord.Locale.american_english, discord.Locale.british_english):
            return None
        return _BEFEHL_NAMEN_EN.get(string.message)


# ══════════════════════════════════════════════════════════════
#  Bot-Klasse
# ══════════════════════════════════════════════════════════════
class DayZBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree    = app_commands.CommandTree(self)
        # Premium-Sperre vor JEDEN Slash-Befehl haengen. Als Instanz-Attribut
        # gesetzt, weil discord.py die Pruefung als self.interaction_check(itx)
        # aufruft – eine gewoehnliche Funktion passt dort genau hinein.
        self.tree.interaction_check = _premium_check
        self.nitrado: Optional[NitradoAPI] = None
        self.ftp:     Optional[FTPManager]  = None
        self.parser   = DayZLogParser()
        self.shop:    Optional["ShopManager"] = None   # wird in on_ready initialisiert
        # FTP-Warnzustand und "online seit" haengen jetzt an der jeweiligen
        # Verbindung (ServerConnection.ftp_warned_ts / .online_since) – als
        # Bot-Attribut haetten sich die Kunden gegenseitig ueberschrieben.
        self._restart_announced: set = set()  # (restart_ts, minuten) bereits angekündigt
        # Zonen-Pings (/zone create): wiederholte Pings im Cooldown-Intervall
        # Schluessel jeweils MIT service_id – sonst greifen gleichnamige Zonen
        # bzw. gleichnamige Spieler zweier Kunden ineinander.
        self._zone_last_ping: Dict[Tuple[str, str, str], float] = {}  # (Server, Zone, Spieler)
        self._zone_pos_seen: Dict[Tuple[str, str], str] = {}          # (Server, Spieler)
        # Der Discovery-Retry haengt jetzt an der jeweiligen Verbindung
        # (ServerConnection.discover_retry_ts), nicht mehr am Bot.

    async def setup_hook(self):
        # Vor dem Registrieren setzen, damit die uebersetzten Namen gleich
        # mit hochgeladen werden (siehe _BefehlsUebersetzer oben).
        await self.tree.set_translator(_BefehlsUebersetzer())

        # Web-Dashboard im selben Prozess/Loop starten (aiohttp). Fehler hier
        # dürfen den Bot-Start nicht verhindern.
        try:
            await start_dashboard(self)
        except Exception as e:
            log.error(f"[DASHBOARD] Start fehlgeschlagen: {e}")

        # Persistente Views registrieren, damit Panel-/Freigabe-Buttons einen
        # Bot-Neustart überleben (timeout=None + feste custom_ids)
        try:
            # Alt-Panels ohne Server-Endung weiter bedienen …
            self.add_view(WhitelistPanelView())
            # … und je verbundenem Server eine eigene, damit die Anfrage weiss,
            # fuer welchen Nitrado-Server sie gilt.
            for _c in connections.all():
                if _c.service_id:
                    self.add_view(WhitelistPanelView(_c.service_id))
            for reqid in list(cfg.whitelist_reqs.keys()):
                self.add_view(WhitelistApprovalView(reqid))
            if cfg.whitelist_reqs:
                log.info(f"[BOT] {len(cfg.whitelist_reqs)} offene Whitelist-Anfrage(n) "
                         f"wiederhergestellt.")
        except Exception as e:
            log.error(f"[BOT] Persistente Whitelist-Views konnten nicht registriert werden: {e}")

        guild_ids = cfg.config.get("guild_ids", [])
        if not guild_ids and connections.all():
            # Verbindungen da, aber keine davon freigeschaltet – etwa nachdem
            # die letzte Zuordnung zurueckgenommen wurde. Global registrieren
            # wuerde die Befehle in JEDER Guild anzeigen, in der der Bot
            # Mitglied ist; gesperrt sind sie zwar, sichtbar aber trotzdem.
            log.warning("[BOT] Keine Guild freigeschaltet – es werden keine "
                        "Slash-Befehle registriert. Ordne im Dashboard unter "
                        "Serverliste einem Server eine Discord-Guild zu.")
        elif not guild_ids:
            log.warning("[BOT] Keine guild_ids konfiguriert – Befehle werden global registriert (24h Verzögerung).")
            await self.tree.sync()
        else:
            for gid in guild_ids:
                g = discord.Object(id=int(gid))
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
                log.info(f"[BOT] Slash-Befehle für Guild {gid} registriert.")

    async def on_interaction(self, interaction: discord.Interaction):
        """Jeden Slash-Befehl ins Aktions-Protokoll schreiben.

        Nur mitschreiben, nicht eingreifen: die Befehle selbst laufen über den
        CommandTree, der unabhängig von diesem Event ausgelöst wird.
        """
        try:
            if interaction.type is not discord.InteractionType.application_command:
                return
            name = getattr(interaction.command, "qualified_name", None) or "?"
            opts = []
            for opt in ((interaction.data or {}).get("options") or []):
                # Unterbefehle (z. B. /whitelist add) verschachteln ihre Optionen
                for sub in (opt.get("options") or [opt]):
                    if sub.get("value") is not None:
                        opts.append(f"{sub.get('name')}={sub.get('value')}")
            where = getattr(interaction.guild, "name", None) or "Direktnachricht"
            _audit_add("discord", f"{interaction.user} ({interaction.user.id})",
                       f"/{name}", " ".join(opts) + f" · {where}")
        except Exception:  # noqa: BLE001 – Protokoll darf keinen Befehl stören
            pass

    async def on_ready(self):
        log.info(f"[BOT] ✅ Eingeloggt als {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="DayZ Server Logs 🎮")
        )
        # on_ready feuert auch bei jedem Discord-Reconnect → nur einmal initialisieren
        # (sonst leakt die alte aiohttp-Session und FTP wird unnötig neu gescannt)
        #
        # WICHTIG: Der Gate hier darf sich NICHT auf die globale config.json
        # stuetzen (cfg.has_nitrado_token()/config["service_id"]) – im
        # Mehrkundenbetrieb liegen die Server ausschliesslich in
        # connections.json, die globalen Felder bleiben leer. Mit dem alten
        # Gate lief init_nitrado() dann NIE, jeder Server blieb ohne FTP-
        # Verbindung (conn.ftp bleibt None) und log_poll ueberspringt ihn
        # lautlos bei jedem Zyklus – Feeds posten nichts, ohne dass irgendwo
        # eine Fehlermeldung auftaucht. Massgeblich ist einzig, ob ueberhaupt
        # eine Verbindung in der Registry steht.
        if connections.all():
            await self.init_nitrado()
        else:
            log.warning("[BOT] ⚠️ Noch kein Nitrado-Server eingerichtet – "
                        "im Dashboard unter „Optionen“ den Token eintragen.")
        # Nahezu-Echtzeit: höchstens 10s zwischen den Polls, mindestens 5s
        # (schont den FTP-Server). Größere Werte aus alten Configs werden
        # automatisch begrenzt, damit Feeds sofort nach Erscheinen posten.
        # Eine gemeinsame Schleife bedient alle Kunden. Damit niemand langsamer
        # abgefragt wird, als er eingestellt hat, gilt der KLEINSTE gewuenschte
        # Wert – vorher zaehlte ausschliesslich der Betreiberwert, und die je
        # Server gespeicherte Einstellung (die /log_status auch anzeigt) war
        # wirkungslos.
        wuensche = [int(cfg.config.get("log_poll_interval_seconds", 10) or 10)]
        for _c in connections.all():
            try:
                wuensche.append(int(_c.get("log_poll_interval_seconds", 10) or 10))
            except (TypeError, ValueError):
                pass
        interval = min(wuensche)
        if interval > 10:
            log.info(f"[POLL] log_poll_interval_seconds={interval} wird auf 10s begrenzt (Echtzeit-Feeds)")
            interval = 10
        # Untergrenze 3s statt 5s: seit die Server gleichzeitig abgefragt
        # werden (siehe log_poll), dauert ein Zyklus nur noch so lange wie der
        # LANGSAMSTE Kunde – ein schnellerer Takt ist damit bezahlbar und die
        # Zeile steht entsprechend frueher im Feed.
        interval = max(3, interval)
        self.log_poll.change_interval(seconds=interval)
        if not self.log_poll.is_running():
            self.log_poll.start()
        if not self.economy_backup.is_running():
            self.economy_backup.start()
        if not self.betreiber_backup.is_running():
            self.betreiber_backup.start()
        status_iv = max(60, int(cfg.config.get("status_update_interval_seconds", 180)))
        self.status_update.change_interval(seconds=status_iv)
        if not self.status_update.is_running():
            self.status_update.start()
        if not self.restart_scheduler.is_running():
            self.restart_scheduler.start()
        if not announcement_scheduler.is_running():
            announcement_scheduler.start()

    async def init_nitrado(self, force: bool = False,
                           only: Optional[ServerConnection] = None):
        """Baut die Laufzeit-Objekte aller verbundenen Server auf.

        Die Stammdaten kommen aus der Registry (connections.json), nicht mehr
        direkt aus der config.json. ``self.nitrado``/``self.ftp`` zeigen
        weiterhin auf den Hauptserver, damit Befehle und Log-Abruf unveraendert
        arbeiten, solange sie noch nicht pro Guild aufloesen.
        force=True (fuer /setup token) ersetzt bestehende Instanzen – die alte
        aiohttp-Session wird dabei sauber geschlossen."""
        # ``only`` begrenzt das Neuaufsetzen auf einen Server. Ohne das wuerde
        # ein /setup token bei einem Kunden die API- und FTP-Objekte ALLER
        # anderen Kunden schliessen und deren Discovery erneut anstossen.
        ziele = [only] if only is not None else list(connections.all())
        for conn in ziele:
            try:
                await conn.ensure_clients(force=force)
            except Exception as e:  # noqa: BLE001 – ein Server darf die anderen nicht kippen
                log.error(f"[CONN] {conn.name}: Verbindung nicht aufbaubar: {e}")

        primary = connections.primary()
        if primary is None:
            log.warning("[CONN] Keine verbundenen Server – Nitrado bleibt uneingerichtet.")
            if force:
                self.nitrado, self.ftp = None, None
            return

        self.nitrado = primary.api
        self.ftp = primary.ftp
        # Shop-/Delivery-Manager je Verbindung (braucht FTP + Nitrado)
        for verbindung in ziele:
            if verbindung.shop is None and verbindung.ftp is not None:
                verbindung.shop = ShopManager(self, verbindung)
        if only is None or only is primary:
            self.shop = primary.shop

        # Auto-Discovery fuer JEDEN Server einzeln. Log-Verzeichnis,
        # Mission-Ordner und Shop-Katalog gehoeren jeweils zu genau einem
        # Server – frueher lief das nur einmal und schrieb die gefundenen
        # Pfade in die zuletzt durchlaufene Verbindung.
        for verbindung in ziele:
            if verbindung.ftp is None or (verbindung.discovered and not force):
                continue
            verbindung.discovered = True
            try:
                await self._auto_discover(verbindung)
            except Exception as e:  # noqa: BLE001
                # FTP gerade nicht erreichbar → Init nicht abbrechen;
                # Discovery kann später per /ftp_scan nachgeholt werden
                verbindung.discovered = False
                log.warning(f"[FTP] {verbindung.name}: Auto-Discovery fehlgeschlagen: {e}")

    async def _auto_discover(self, conn: Optional[ServerConnection] = None):
        """Sucht automatisch nach DayZ-Log-Verzeichnissen via FTP.

        Die gefundenen Pfade gehoeren zu genau einem Server und landen deshalb
        in dessen Verbindung. Ohne Angabe gilt der Hauptserver.
        """
        conn = conn or connections.primary()
        if conn is None or conn.ftp is None:
            return
        log.info(f"[FTP] Starte Auto-Discovery der Log-Verzeichnisse ({conn.name})...")
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(
            None,
            functools.partial(conn.ftp.discover_paths,
                              conn.get("map_name", "ChernarusPlus"))
        )

        for key, found_key in (("ftp_log_dir", "log_dir"),
                               ("ftp_ban_file", "ban_file"),
                               ("ftp_mission_dir", "mission_dir"),
                               ("cfg_effect_area_path", "cfg_effect_area")):
            if found.get(found_key) and not conn.get(key):
                _conn_store(conn, key, found[found_key])
                log.info(f"[FTP] 💾 {conn.name}: {key}={found[found_key]}")

        # Selbstheilung: Der konfigurierte cfgEffectArea-Pfad zeigt auf einen Ordner,
        # den es auf dem FTP gar nicht gibt (z.B. Chernarus-Pfad, obwohl der Server
        # Sakhal läuft) → Shop-Käufe landen sonst in einer Datei, die der Server nie
        # liest, und spawnen nie. Auf den tatsächlich gefundenen Ordner korrigieren.
        configured   = str(conn.get("cfg_effect_area_path") or "")
        found_effect = found.get("cfg_effect_area")
        if configured and found_effect and configured != found_effect:
            parent  = configured.rsplit("/", 1)[0] or "/"
            entries = await loop.run_in_executor(None, conn.ftp.list_dir, parent)
            if not entries:
                log.warning(f"[FTP] ⚠️ Konfigurierter cfg_effect_area_path existiert nicht "
                            f"auf dem FTP ({configured}) – korrigiert auf {found_effect}")
                _conn_store(conn, "cfg_effect_area_path", found_effect)
                if found.get("mission_dir"):
                    _conn_store(conn, "ftp_mission_dir", found["mission_dir"])

        # Shop-Katalog dieses Servers: ist er noch leer, die types.xml direkt
        # vom Server holen. So bekommt jeder Kunde genau seine eigenen Items,
        # ohne den Katalog von Hand pflegen zu muessen.
        if not conn.catalog.items:
            n, meldung = await katalog_von_server_holen(conn)
            if n:
                log.info(f"[SHOP] {conn.name}: {meldung}")
            else:
                log.info(f"[SHOP] {conn.name}: Katalog bleibt leer – {meldung}")

    @tasks.loop(seconds=10)
    async def log_poll(self):
        """Alle verbundenen Server abfragen – **gleichzeitig, nicht nacheinander**.

        Jede Verbindung hat eigene FTP-Sitzung, eigenen Log-Zustand und einen
        eigenen Parser – die Ereignisse eines Servers erreichen dadurch nur
        noch dessen Discord-Guild.

        Warum parallel: nacheinander war die Dauer eines Zyklus die SUMME aller
        Kunden. Bei ~1s FTP-Antwortzeit und drei Kunden waren das ueber 12s –
        mehr als der 10s-Takt. Der Bot kam damit dauerhaft nicht mehr
        hinterher, der Rueckstand wuchs immer weiter, und Ereignisse landeten
        erst Stunden spaeter (oder wurden als "Rueckstand" ganz uebersprungen)
        im Feed. Gleichzeitig ist die Dauer nur noch die des LANGSAMSTEN
        Kunden, und das traege FTP eines Kunden bremst die anderen nicht mehr.

        Nebenwirkung, die hier ausdruecklich erwuenscht ist: ``asyncio`` gibt
        jeder Aufgabe eine eigene Kopie des Kontexts. Das ``_AKTUELLER_SERVER``
        aus ``_setze_aktuellen_server`` kann dadurch nicht mehr von einem
        Kunden zum naechsten durchschlagen – parallel ist hier also auch die
        sauberere Mandanten-Trennung.
        """
        aufgaben = []
        for conn in connections.all():
            if conn.ftp is None:
                await _poll_zustand_melden(conn, "kein FTP aufgebaut (Zugangsdaten "
                                           "unvollständig oder Verbindung noch nicht eingerichtet)")
                continue
            if conn.guild_id is None:
                # Ohne zugeordnete Guild gibt es keinen Discord-Server, der
                # diesen Nitrado-Server verwaltet ("kein Premium"). Wuerde er
                # trotzdem gepollt, gingen seine Ereignisse mangels Ziel an
                # ALLE konfigurierten Guilds – also an fremde Kunden.
                await _poll_zustand_melden(conn, "keinem Discord-Server zugeordnet "
                                           "(Zuordnung fehlt im Dashboard unter „Serverliste“)")
                continue
            aufgaben.append(self._poll_connection_sicher(conn))
        if aufgaben:
            await asyncio.gather(*aufgaben)

    async def _poll_connection_sicher(self, conn: ServerConnection):
        """Ein Server – ein Fehler darf die anderen nie mitreissen. Frueher
        stand dieses try/except in der Schleife; mit ``gather`` braucht es
        eine eigene Ebene, sonst wuerde eine Ausnahme die ganze Sammlung
        abbrechen."""
        try:
            await self._poll_connection(conn)
        except Exception as e:  # noqa: BLE001 – ein Server darf die anderen nicht stoppen
            log.error(f"[POLL] {conn.name}: {e}")
            await self._check_ftp_health(conn)

    async def _poll_connection(self, conn: ServerConnection):
        # Waehrung/Anzeige gehoeren zu DIESEM Server (siehe _cur_symbol)
        _setze_aktuellen_server(conn)
        log_dir = conn.get("ftp_log_dir")
        if not log_dir:
            # Discovery beim Start fehlgeschlagen oder noch nicht gelaufen →
            # automatisch erneut versuchen (alle 120s), sonst würden nie
            # Kills/Builds/Damage gepostet, bis jemand /ftp_scan ausführt
            # Wiederholsperre je Server: ein Server mit kaputtem FTP darf die
            # Discovery der anderen nicht ausbremsen.
            now = time.time()
            if now - conn.discover_retry_ts < 120:
                await _poll_zustand_melden(conn, "noch kein Log-Verzeichnis gefunden "
                                           "(Auto-Erkennung versucht es alle 120s erneut)")
                return
            conn.discover_retry_ts = now
            try:
                await self._auto_discover(conn)
            except Exception as e:
                log.warning(f"[FTP] {conn.name}: Auto-Discovery-Retry fehlgeschlagen: {e}")
            log_dir = conn.get("ftp_log_dir")
            if not log_dir:
                await _poll_zustand_melden(conn, "noch kein Log-Verzeichnis gefunden "
                                           "(Auto-Erkennung versucht es alle 120s erneut)")
                return
        try:
            loop = asyncio.get_running_loop()
            await self._pruefe_neustart(conn, log_dir, loop)
            adm_files = await loop.run_in_executor(None, conn.ftp.list_adm_files, log_dir)
            if not adm_files:
                await _poll_zustand_melden(conn, f"keine .ADM-Dateien in {log_dir} gefunden "
                                           "(ADM-Logging auf dem Server aktiviert? FTP-Pfad richtig?)")
                await self._check_ftp_health(conn)
                return

            latest = adm_files[-1]
            state = conn.log_state.get("current")
            if state is None:
                # Erststart ohne gespeicherten Offset: Alt-Events NICHT in die
                # Feeds nachposten, sondern ab dem aktuellen Dateiende weiterlesen
                size_now = await loop.run_in_executor(None, conn.ftp.file_size_or_none, latest)
                conn.log_state["current"] = {"file": latest, "offset": int(size_now or 0)}
                conn.log_state["last_poll_ts"] = time.time()
                connections.save()
                log.info(f"[POLL] Erststart – überspringe Alt-Events, Offset={int(size_now or 0)} ({latest})")
                await _poll_zustand_melden(conn, None)
                return

            # Offline-Lücke erkennen: War der Bot (oder das FTP) länger weg als
            # max_backlog_minutes, die aufgelaufenen Alt-Events NICHT nachposten –
            # sonst flutet der Bot die Feeds mit stundenalten Embeds.
            # last_poll_ts fehlt bei Updates von älteren Versionen → Lücke unbekannt
            # → sicherheitshalber ebenfalls überspringen (verliert max. 1 Poll-Zyklus).
            now = time.time()
            last_poll = float(conn.log_state.get("last_poll_ts") or 0)
            backlog_limit = max(1, int(conn.get("max_backlog_minutes", 10))) * 60
            gap = (now - last_poll) if last_poll else -1.0
            skip_backlog = (not last_poll) or gap > backlog_limit

            events: List[Dict] = []

            restart_detected = False
            if state["file"] != latest:
                # Neue ADM-Datei = Server wurde neu gestartet.
                # Den ungelesenen Rest der ALTEN Datei noch auslesen, damit
                # zwischen letztem Poll und Rotation keine Events verloren gehen.
                restart_detected = bool(state.get("file"))
                old_file = state.get("file")
                if old_file and old_file in adm_files and not skip_backlog:
                    old_tail, _ = await loop.run_in_executor(
                        None, conn.ftp.read_from_offset, old_file, state.get("offset", 0)
                    )
                    if old_tail:
                        events.extend(conn.parser.parse_lines(old_tail))
                        log.info(f"[POLL] {len(events)} Events aus dem Rest der alten Datei {old_file}")
                state = {"file": latest, "offset": 0}

            current_size = await loop.run_in_executor(None, conn.ftp.file_size_or_none, latest)
            if current_size is not None:
                conn.size_fehler = 0
            # "is not None" statt Wahrheitswert: eine frisch rotierte, noch
            # leere ADM-Datei hat current_size == 0 – das ist ein gueltiger
            # Wert und darf nicht wie ein FTP-Fehler behandelt werden.
            if current_size is not None and state.get("offset", 0) > current_size:
                # Gleiche Datei, aber geschrumpft: Server hat die ADM beim Neustart geleert
                log.info(f"[POLL] Offset {state.get('offset', 0)} > Dateigröße {current_size} – Neustart (Truncation) erkannt")
                restart_detected = True
                state = {"file": latest, "offset": 0}

            if conn.shop and (restart_detected or conn.shop.cleanup_retry_needed):
                # Offene Käufe ausliefern; nach FTP-Fehler automatisch erneut versuchen.
                # Bei frisch erkanntem Neustart bleiben die Einträge in der Datei,
                # bis der Server per A2S wieder online ist (Mission-Load fertig),
                # und werden dann sofort entfernt
                conn.shop.spawn_cleanup(delayed=restart_detected)

            if restart_detected:
                # Server-Neustart wirft alle Spieler → offene Spielzeit-Sitzungen
                # DIESES Servers beenden (nicht die der anderen Kunden)
                await loop.run_in_executor(None, db.close_all_sessions, conn.service_id)

            if skip_backlog:
                # Fast-Forward ans aktuelle Dateiende – nichts nachposten
                size_now = (current_size if current_size is not None
                           else await loop.run_in_executor(None, conn.ftp.file_size_or_none, latest))
                if size_now is None:
                    # Größe nicht ermittelbar (FTP-Fehler?). Ein paar Zyklen
                    # abwarten (last_poll_ts NICHT aktualisieren, Skip greift
                    # erneut) – aber NICHT fuer immer: manche FTP-Server
                    # beantworten SIZE dauerhaft mit einem Fehler (z.B. im
                    # ASCII-Modus), und ohne Ausweg blieb der Bot dann fuer
                    # alle Zeit in diesem Zweig haengen, ohne je wieder etwas
                    # zu posten. Ab SIZE_FEHLER_GRENZE wird stattdessen ganz
                    # normal ab dem zuletzt bekannten Offset weitergelesen –
                    # der aufgelaufene Rueckstand kommt dann einmalig nach,
                    # was besser ist als dauerhafte Stille.
                    conn.size_fehler += 1
                    if conn.size_fehler < SIZE_FEHLER_GRENZE:
                        await _poll_zustand_melden(conn, "Dateigröße nicht ermittelbar "
                                                   "(FTP-Fehler beim SIZE-Kommando?) – Zyklus wird wiederholt")
                        await self._check_ftp_health(conn)
                        return
                    log.warning(f"[POLL] {conn.name}: Dateigröße seit {conn.size_fehler} Zyklen "
                                f"nicht ermittelbar – lese trotzdem ab Offset {state.get('offset', 0)} "
                                f"weiter, statt weiter zu warten.")
                    skip_backlog = False
                else:
                    conn.size_fehler = 0
                    state = {"file": latest, "offset": int(size_now)}
                    conn.log_state["current"] = state
                    conn.log_state["last_poll_ts"] = now
                    connections.save()
                    await loop.run_in_executor(None, db.close_all_sessions, conn.service_id)
                    mins = int(gap // 60) if gap >= 0 else 0
                    log.info(f"[POLL] Bot war {mins} Min offline – überspringe Alt-Events, Offset={state['offset']} ({latest})")
                    if gap >= 0:
                        info = discord.Embed(
                            title="⏭️ Alte Log-Events übersprungen",
                            description=(f"Der Bot war ca. **{mins} Minuten** offline. Log-Events aus "
                                         f"dieser Zeit werden nicht nachgepostet, um die Feeds nicht zu "
                                         f"fluten (Grenze: `max_backlog_minutes` in config.json)."),
                            color=0x95A5A6)
                        await _post_feed(conn.guild_id, "adminlog", info,
                                         service_id=conn.service_id)
                    await self._check_ftp_health(conn)
                    await _poll_zustand_melden(conn, None)
                    return

            content, new_offset = await loop.run_in_executor(
                None, conn.ftp.read_from_offset, latest, state["offset"]
            )
            if content is None:
                # Echter Lesefehler, nicht bloss "nichts Neues" (das waere "").
                # last_poll_ts trotzdem fortschreiben – sonst wuerde ein
                # dauerhaft lesefehlerhaftes FTP zusaetzlich noch die
                # Offline-Lücken-Erkennung (skip_backlog) auslösen.
                await _poll_zustand_melden(conn, f"Log-Datei {latest} nicht lesbar "
                                           "(FTP-Fehler beim Lesen ab Offset)")
                conn.log_state["last_poll_ts"] = now
                connections.save()
                await self._check_ftp_health(conn)
                return
            if content:
                state["offset"] = new_offset
                events.extend(conn.parser.parse_lines(content))

            # Zustand auch bei reiner Rotation (ohne neuen Inhalt) speichern
            conn.log_state["current"] = state
            conn.log_state["last_poll_ts"] = now
            connections.save()

            if events:
                log.info(f"[POLL] {len(events)} neue Events aus {latest}")
                # Rate-Limit-Schutz: pro Zyklus höchstens N Events posten
                cap = max(1, int(conn.get("max_events_per_cycle", 30)))
                if len(events) > cap:
                    log.warning(f"[POLL] {len(events)} Events in einem Zyklus – "
                                f"poste nur die neuesten {cap} (max_events_per_cycle)")
                    events = events[-cap:]
                for ev in events:
                    await self._dispatch(ev, conn)

            # Unerkannte Zeilen dieses Zyklus (falls der Betreiber einen
            # "unparsed"-Feed eingerichtet hat) gedrosselt/entdoppelt posten.
            await self._post_unparsed_zeilen(conn)

            # Zonen-Pings: frisch getrackte Positionen gegen /zone-Zonen prüfen
            await self._check_zones(conn)
            # Spielzeit-Belohnung für offene Sitzungen gutschreiben
            await self._credit_playtime(conn)
            await self._check_ftp_health(conn)
            await _poll_zustand_melden(conn, None)
        except Exception as e:
            log.error(f"[POLL] {conn.name}: {e}")
            await self._check_ftp_health(conn)

    UNPARSED_MAX_JE_ZYKLUS = 5

    async def _post_unparsed_zeilen(self, conn: ServerConnection,
                                    parser: Optional["DayZLogParser"] = None) -> None:
        """Postet neue unerkannte Log-Zeilen in den "unparsed"-Feed – NUR,
        wenn der Betreiber ihm einen Channel gegeben hat, sonst bleiben sie
        rein auf der Diagnose-Seite sichtbar. Entdoppelt und auf
        UNPARSED_MAX_JE_ZYKLUS gedeckelt, sonst würde eine rauschige ADM-Datei
        (z. B. ein Mod, der pro Tick eine Zeile schreibt) die Feeds fluten.
        """
        parser = parser or (conn.parser if conn is not None else self.parser)
        if parser is None or not parser.frisch_unerkannt:
            return
        zeilen = parser.frisch_unerkannt
        parser.frisch_unerkannt = []
        if conn is None or conn.guild_id is None:
            return
        feed = cfg.feed_settings(int(conn.guild_id), "unparsed", conn.service_id)
        if not feed:
            return  # nicht eingerichtet – Zeilen bleiben nur in der Diagnose sichtbar
        # Entdoppeln (Reihenfolge behalten), dann deckeln.
        eindeutig: List[str] = []
        gesehen: set = set()
        for z in zeilen:
            if z not in gesehen:
                gesehen.add(z)
                eindeutig.append(z)
        ueberschuss = len(eindeutig) - self.UNPARSED_MAX_JE_ZYKLUS
        gezeigt = eindeutig[:self.UNPARSED_MAX_JE_ZYKLUS]
        text = "\n".join(gezeigt)
        if len(text) > 1000:
            text = text[:997] + "…"
        embed = discord.Embed(
            title="❓ Unerkannte Log-Zeilen",
            description=f"```\n{text}\n```",
            color=FEED_TYPES["unparsed"]["farbe"])
        if ueberschuss > 0:
            embed.set_footer(text=f"… und {ueberschuss} weitere in diesem Zyklus "
                                  f"(vollständig auf der Diagnose-Seite).")
        send_embed = _feed_anwenden(embed, feed)
        ch = await self._resolve_channel(int(feed["channel_id"]))
        if ch:
            try:
                await ch.send(embed=send_embed)
            except Exception as e:  # noqa: BLE001 – darf den Poll-Zyklus nicht stoppen
                log.warning(f"[POLL] {conn.name}: unparsed-Feed nicht postbar: {e}")

    @log_poll.before_loop
    async def _before_poll(self):
        await self.wait_until_ready()

    @tasks.loop(hours=24)
    async def economy_backup(self):
        keep = max(1, int(cfg.config.get("economy_backup_keep", 7)))
        loop = asyncio.get_running_loop()
        dest = await loop.run_in_executor(None, db.backup, keep)
        if dest:
            log.info(f"[ECON] Tages-Backup erstellt: {dest}")

    @economy_backup.before_loop
    async def _before_backup(self):
        await self.wait_until_ready()

    # ── Taegliches Komplett-Backup (alle Kundendaten, nicht nur economy.db) ──
    @tasks.loop(hours=24)
    async def betreiber_backup(self):
        keep = max(1, int(cfg.config.get("betreiber_backup_keep", 7)))
        loop = asyncio.get_running_loop()
        dest = await loop.run_in_executor(None, _voll_backup_erstellen, keep)
        if dest:
            log.info(f"[BACKUP] Taegliches Komplett-Backup erstellt: {dest}")

    @betreiber_backup.before_loop
    async def _before_betreiber_backup(self):
        await self.wait_until_ready()

    # ── Auto-Status-Embed (eine Nachricht pro Guild, wird editiert) ──
    @tasks.loop(seconds=180)
    async def status_update(self):
        # tasks.loop stoppt bei unbehandelten Exceptions dauerhaft → alles fangen
        try:
            await self._status_update_once()
        except Exception as e:
            log.error(f"[STATUS] Fehler: {e}")

    async def _status_update_once(self):
        """Status-Embed je verbundenem Server in dessen Guild aktualisieren."""
        for conn in connections.all():
            await self._status_update_for(conn)

    async def _status_update_for(self, conn: ServerConnection):
        if conn.guild_id is None:
            return          # keine Guild → nichts anzuzeigen, also auch nicht abfragen
        ip = str(conn.get("server_ip") or "").split(":")[0].strip()
        qport = int(conn.get("query_port", 0) or 0)
        if not ip or not qport:
            return
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, a2s_query, ip, qport)
        if info:
            if conn.online_since is None:
                conn.online_since = time.time()
        else:
            conn.online_since = None
        embed = self._build_status_embed(info, conn)
        # Nur die Guild dieses Servers – sonst saehe jeder Discord-Server den
        # Status aller Kunden.
        for gid_str in [str(conn.guild_id)]:
            ch_id = cfg.get_channel(int(gid_str), "status", conn.service_id)
            if not ch_id:
                continue
            ch = await self._resolve_channel(int(ch_id))
            if not ch:
                continue
            # Die Status-Nachricht gehoert dem Server, nicht der Guild – sonst
            # editieren zwei Server derselben Guild im Minutentakt dieselbe
            # Nachricht und die Anzeige springt zwischen ihnen hin und her.
            eigen = cfg.server_feeds(int(gid_str), conn.service_id, anlegen=True)
            msg = None
            msg_id = eigen.get("status_message_id")
            if msg_id:
                try:
                    msg = await ch.fetch_message(int(msg_id))
                except Exception:
                    msg = None   # Nachricht gelöscht → neu senden
            try:
                if msg:
                    await msg.edit(embed=embed)
                else:
                    msg = await ch.send(embed=embed)
                    eigen["status_message_id"] = msg.id
                    cfg.save_guilds()
            except Exception as e:
                log.error(f"[STATUS] Guild {gid_str}: {e}")

    @status_update.before_loop
    async def _before_status(self):
        await self.wait_until_ready()

    def _build_status_embed(self, info: Optional[Dict],
                            conn: Optional[ServerConnection] = None) -> discord.Embed:
        if info:
            e = discord.Embed(title="🟢 Server ONLINE", color=0x2ECC71)
            e.add_field(name="Server", value=str(info.get("name") or "?"), inline=False)
            e.add_field(name="👥 Spieler",
                        value=f"{info.get('players', '?')} / {info.get('max_players', '?')}",
                        inline=True)
            e.add_field(name="🗺️ Map", value=str(info.get("map") or "?"), inline=True)
            if conn is not None and conn.online_since:
                h, m = divmod(int((time.time() - conn.online_since) // 60), 60)
                e.add_field(name="⏱️ Online seit (Bot-Sicht)",
                            value=f"{h} Std {m} Min", inline=True)
        else:
            e = discord.Embed(
                title="🔴 Server OFFLINE",
                description=("Keine Antwort auf die A2S-Abfrage – Server ist aus, "
                             "startet gerade oder der Query-Port stimmt nicht."),
                color=0xE74C3C)
        nxt = self._next_scheduled_restart(conn)
        if nxt:
            e.add_field(name="⏰ Nächster Auto-Restart", value=f"<t:{int(nxt)}:R>", inline=True)
        e.set_footer(text="Auto-Status · aktualisiert sich automatisch")
        e.timestamp = datetime.now(timezone.utc)
        return e

    # ── Zonen-Pings (/zone create): Spieler in der Zone ───────
    async def _check_zones(self, conn: Optional[ServerConnection] = None):
        """Bewertet frisch getrackte Spieler-Positionen gegen die konfigurierten
        Zonen und pingt WIEDERHOLT (alle zone_ping_cooldown_seconds, Default 5 Min),
        solange sich ein Spieler in der Zone befindet – auch mehrfach für denselben
        Spieler. Allowlist-Spieler werden nie gemeldet.
        Wird pro Poll-Zyklus aufgerufen; fängt eigene Fehler selbst ab, damit
        der Poll-Zyklus (Spielzeit-Gutschrift etc.) nie daran scheitert."""
        try:
            _src = conn if conn is not None else connections.primary()
            zones = [z for z in (_zones(_src) if _src else (cfg.config.get("zones") or []))
                     if isinstance(z, dict) and z.get("name")]
            if not zones:
                return
            # Zustände entfernter Zonen entsorgen – aber NUR die dieses
            # Servers. Frueher loeschte jeder Poll-Durchlauf die Cooldowns der
            # anderen Kunden, deren Zonen dann alle 10 s erneut pingten.
            _sid = _src.service_id if _src is not None else ""
            zone_keys = {str(z["name"]).strip().lower() for z in zones}
            self._zone_last_ping = {k: v for k, v in self._zone_last_ping.items()
                                    if k[0] != _sid or k[1] in zone_keys}
            cooldown = max(0, int((_src.get("zone_ping_cooldown_seconds", 300) if _src
                                   else cfg.config.get("zone_ping_cooldown_seconds", 300))))
            now = time.time()
            _p = (conn.parser if conn is not None
                  else (_src.parser if _src is not None else self.parser))
            for pname, info in list((_p.player_positions if _p else {}).items()):
                # Nur NEU eingetroffene Positions-Samples bewerten – alte Daten
                # dürfen nach Zonen-Änderungen keine nachträglichen Pings auslösen
                last_seen = str(info.get("last_seen") or "")
                if self._zone_pos_seen.get((_sid, pname)) == last_seen:
                    continue
                self._zone_pos_seen[(_sid, pname)] = last_seen
                parts = [p.strip() for p in str(info.get("position") or "").split(",")]
                if len(parts) < 2:
                    continue
                try:
                    px, pz = float(parts[0]), float(parts[1])   # ADM pos = <Ost, Nord, Höhe>
                except ValueError:
                    continue
                for zone in zones:
                    if zone.get("type") == "polygon":
                        pts = zone.get("points")
                        inside = isinstance(pts, list) and _point_in_polygon(px, pz, pts)
                    else:
                        try:
                            zx = float(zone.get("x", 0.0))
                            zz = float(zone.get("z", 0.0))
                            zr = float(zone.get("radius", 0.0))
                        except (TypeError, ValueError):
                            continue
                        inside = (px - zx) ** 2 + (pz - zz) ** 2 <= zr * zr
                    if not inside:
                        continue
                    zkey = (_sid, str(zone["name"]).strip().lower(), pname)
                    if _player_in_allowlist(zone, pname):
                        continue     # Allowlist: nie pingen
                    if now - self._zone_last_ping.get(zkey, 0.0) < cooldown:
                        continue     # Wiederhol-Intervall noch nicht abgelaufen
                    self._zone_last_ping[zkey] = now
                    await self._post_zone_ping(zone, pname, info, _src)
        except Exception as e:
            log.error(f"[ZONE] Zonen-Prüfung fehlgeschlagen: {e}")

    async def _post_zone_ping(self, zone: Dict, player: str, info: Dict,
                              conn: Optional[ServerConnection] = None):
        # Damit der iZurvive-Link die Karte DIESES Servers benutzt und nicht
        # die global eingestellte (siehe _aktuelle_karte).
        _setze_aktuellen_server(conn)
        e = discord.Embed(
            title="🛡️ • Ping On Detection",
            description=f"**{player}** was located within the zone **{zone['name']}**.",
            color=0x9B59B6)
        loc = _location_field_value(info.get("position"))
        if loc:
            e.add_field(name="📍 • Player Location", value=loc, inline=False)
        if zone.get("type") == "polygon":
            zone_val = f"Polygon · {len(zone.get('points') or [])} Punkte"
        else:
            zone_val = f"`{zone.get('x')}, {zone.get('z')}` · Radius **{zone.get('radius')} m**"
        e.add_field(name="🎯 Zone", value=zone_val, inline=False)
        e.set_footer(text=f"Zone: {zone['name']}")
        e.timestamp = datetime.now(timezone.utc)
        role_ids = _zone_ping_role_ids(zone)
        content = " ".join(f"<@&{r}>" for r in role_ids) or None
        gid = int(zone["guild_id"]) if zone.get("guild_id") else None
        if gid is None:
            # Ohne Guild ginge der Alarm samt Spielername und Koordinaten an
            # ALLE konfigurierten Discord-Server.
            log.warning(f"[ZONE] {zone.get('name')}: keine guild_id – Ping unterdrueckt.")
            return
        # Die Zone traegt ihre Guild seit dem Anlegen. Wird der Server spaeter
        # einem anderen Kunden zugeordnet, zeigt sie weiter auf den alten
        # Discord – Spielernamen und exakte Koordinaten des NEUEN Betreibers
        # gingen dann an den frueheren. Passt die gespeicherte Guild nicht mehr
        # zur Verbindung, wird nicht gepostet.
        if conn is not None and conn.guild_id is not None and gid != int(conn.guild_id):
            log.warning(f"[ZONE] {zone.get('name')}: gespeicherte Guild {gid} gehört "
                        f"nicht mehr zu {conn.name} (jetzt {conn.guild_id}) – Ping "
                        f"unterdrückt. Zone im Dashboard neu speichern.")
            return
        sid = conn.service_id if conn is not None else None
        zone_ch = zone.get("channel_id")
        if zone_ch:
            await _post_feed(gid, "zone", e, content=content,
                             channel_id=int(zone_ch), service_id=sid)
        elif cfg.get_channel(gid, "zone", sid):
            await _post_feed(gid, "zone", e, content=content, service_id=sid)
        else:
            await _post_feed(gid, "adminlog", e, content=content, service_id=sid)

    # ── Geplante Neustarts (/auto restart) ────────────────────
    def _next_scheduled_restart(self,
                                conn: Optional[ServerConnection] = None) -> Optional[float]:
        """Nächster geplanter Restart-Zeitpunkt EINES Servers (lokale Botzeit).

        Ohne Angabe gilt der Hauptserver – jeder Kunde hat seinen eigenen
        Zeitplan, sonst wuerde der Plan des Betreibers fremde Server neu starten.
        """
        _c = conn if conn is not None else connections.primary()
        sched = ((_c.get("auto_restart_schedule") if _c
                  else cfg.config.get("auto_restart_schedule")) or {})
        if not sched.get("enabled"):
            return None
        try:
            hh, mm = str(sched.get("first_time", "04:00")).split(":")
            step = timedelta(hours=max(1, int(sched.get("interval_hours", 4))))
            anchor = datetime.now().replace(hour=int(hh), minute=int(mm),
                                            second=0, microsecond=0)
        except Exception:
            return None
        now = datetime.now()
        while anchor > now:
            anchor -= step
        while anchor <= now:
            anchor += step
        return anchor.timestamp()

    @tasks.loop(seconds=30)
    async def restart_scheduler(self):
        # tasks.loop stoppt bei unbehandelten Exceptions dauerhaft → alles fangen
        try:
            await self._restart_scheduler_once()
        except Exception as e:
            log.error(f"[AUTO-RESTART] Fehler: {e}")

    async def _restart_scheduler_once(self):
        """Jeden verbundenen Server nach seinem eigenen Zeitplan neu starten."""
        for conn in connections.all():
            try:
                await self._restart_scheduler_conn(conn)
            except Exception as e:  # noqa: BLE001 – ein Server darf die anderen nicht stoppen
                log.error(f"[AUTO-RESTART] {conn.name}: {e}")
        # Alte Ankündigungs-Marker aufräumen
        cutoff = time.time() - 3600
        self._restart_announced = {k for k in self._restart_announced if k[1] > cutoff}

    async def _restart_scheduler_conn(self, conn: ServerConnection):
        sid = conn.service_id
        nxt = self._next_scheduled_restart(conn)
        if nxt is None:
            übrig = {k for k in self._restart_announced if k[0] != sid}
            if übrig != self._restart_announced:
                self._restart_announced = übrig
            return
        remaining = nxt - time.time()
        # Ankündigungen 15/5/1 Minuten vorher (45s-Fenster > 30s-Loop-Takt)
        for mins in (15, 5, 1):
            key = (sid, int(nxt), mins)
            if (mins * 60 - 45) < remaining <= mins * 60 and key not in self._restart_announced:
                self._restart_announced.add(key)
                e = discord.Embed(
                    title=f"🔄 Server-Neustart in {mins} Minute{'n' if mins != 1 else ''}!",
                    description=(f"Geplanter Neustart um <t:{int(nxt)}:t> Uhr – "
                                 f"bitte sichere Position und Loot."),
                    color=0xE67E22 if mins <= 5 else 0xF1C40F)
                await self._post_restart_feed(e, conn)
        # Restart auslösen
        key0 = (sid, int(nxt), 0)
        if remaining <= 30 and key0 not in self._restart_announced:
            self._restart_announced.add(key0)
            if conn.api is None:
                log.warning(f"[AUTO-RESTART] {conn.name}: keine Nitrado-Verbindung.")
                return
            try:
                ok, msg = await conn.api.restart()
            except Exception as ex:  # noqa: BLE001
                ok, msg = False, str(ex)
            log.info(f"[AUTO-RESTART] {conn.name}: Neustart ausgelöst: ok={ok} – {msg}")
            e = discord.Embed(
                title="🔄 Server wird jetzt neu gestartet" if ok
                      else "❌ Geplanter Neustart fehlgeschlagen",
                description=("Der geplante Neustart wurde über die Nitrado-API ausgelöst."
                             if ok else f"Nitrado-API-Fehler: {msg}"),
                color=0x2ECC71 if ok else 0xE74C3C)
            await self._post_restart_feed(e, conn)

    @restart_scheduler.before_loop
    async def _before_restart_scheduler(self):
        await self.wait_until_ready()

    async def _post_restart_feed(self, embed: discord.Embed,
                                 conn: Optional[ServerConnection] = None):
        """Postet in den restart-Feed; ohne konfigurierten Channel → adminlog.

        Mit Verbindung nur in deren Guild – ein Neustart-Hinweis eines Servers
        hat in fremden Discord-Servern nichts zu suchen.
        """
        ziele = ([str(conn.guild_id)] if conn is not None and conn.guild_id
                 else ([] if conn is not None else list(cfg.guilds)))
        for gid_str in ziele:
            gid = int(gid_str)
            _sid = conn.service_id if conn is not None else None
            lt = ("restart" if cfg.get_channel(gid, "restart", _sid) else "adminlog")
            await _post_feed(gid, lt, embed, service_id=_sid)

    async def _try_refresh_ftp_credentials(self, conn: ServerConnection) -> bool:
        """Selbstheilung bei FTP-Dauerausfall: Zugangsdaten fuer DIESEN Server
        frisch ueber seinen Nitrado-Token holen und den FTPManager ersetzen,
        falls Nitrado sie geaendert hat (z.B. Passwort-Rotation).
        True = neue Daten uebernommen.

        Frueher lief das immer ueber ``self.nitrado`` – also ueber den Token des
        Hauptservers – und schrieb dessen Zugangsdaten in die globale config.json.
        Ein FTP-Ausfall bei einem Kunden hat damit die Verbindung des Betreibers
        umgebaut, statt die des Kunden zu reparieren.
        """
        if conn is None or conn.api is None:
            return False
        try:
            info = await conn.api.get_info()
        except Exception:  # noqa: BLE001
            return False
        if not info:
            return False
        creds = NitradoAPI.extract_ftp_credentials(info)
        if not creds:
            return False
        changed = (creds["host"] != conn.get("ftp_host")
                   or creds["user"] != conn.get("ftp_user")
                   or creds["password"] != conn.get("ftp_password")
                   or int(creds["port"]) != int(conn.get("ftp_port") or 21))
        if not changed:
            return False
        for schluessel, wert in (("ftp_host", creds["host"]), ("ftp_port", creds["port"]),
                                 ("ftp_user", creds["user"]),
                                 ("ftp_password", creds["password"])):
            _conn_store(conn, schluessel, wert)
        conn.ftp = FTPManager(host=creds["host"], port=creds["port"],
                              user=creds["user"], password=creds["password"])
        if connections.primary() is conn:
            self.ftp = conn.ftp
        log.info(f"[NITRADO] 🔄 {conn.name}: FTP-Zugangsdaten über die API erneuert.")
        return True

    async def _pruefe_neustart(self, conn: ServerConnection, log_dir: str, loop):
        """Neue .RPT-Datei erkannt = der Gameserver wurde neu gestartet.

        Jeder Serverstart legt eine neue RPT an, deren Name Datum und Uhrzeit
        traegt (``DayZServer_..._20260808_142104.RPT``). Erkannt wird allein
        am Dateinamen – die Datei selbst wird NIE geladen: sie ist mehrere
        Megabyte gross und enthaelt fuer die Feeds nichts, was nicht schon in
        der .ADM steht.

        Das ergaenzt den eigenen Neustart-Zeitplan um die Faelle, die er nicht
        kennt: Abstuerze und Neustarts von Nitrado-Seite.

        Laeuft bewusst NICHT in jedem 10s-Zyklus: Serverneustarts kommen ein
        paar Mal am Tag vor, die Abfrage kostete aber jedes Mal eine eigene
        FTP-Runde – ein Viertel der Zeit eines Zyklus fuer eine Information,
        die sich stundenlang nicht aendert. Die neue ADM-Datei faellt ohnehin
        im selben Zyklus auf (``state["file"] != latest``), das hier ist nur
        die zusaetzliche Meldung.
        """
        jetzt = time.time()
        if jetzt - conn.rpt_geprueft_ts < _RPT_PRUEF_ABSTAND:
            return
        conn.rpt_geprueft_ts = jetzt
        try:
            rpt = await loop.run_in_executor(None, conn.ftp.list_rpt_files, log_dir)
        except Exception as e:  # noqa: BLE001 – Zugabe, darf den Poll nie kippen
            log.debug(f"[POLL] RPT-Liste ({conn.name}): {e}")
            return
        if not rpt:
            return
        neueste = rpt[-1].split("/")[-1]
        bekannt = conn.log_state.get("rpt_neueste")
        if bekannt == neueste:
            return
        conn.log_state["rpt_neueste"] = neueste
        connections.save()
        if not bekannt:
            # Erster Durchlauf: nur merken. Sonst meldete jeder Bot-Start
            # einen Server-Neustart, den es gar nicht gab.
            log.info(f"[POLL] {conn.name}: RPT-Stand gemerkt ({neueste}).")
            return
        # Aus DayZServer_PS4_x64_20260808_142104.RPT wird 08.08.2026 14:21:04
        gestartet = ""
        m = re.search(r"(\d{8})_(\d{6})", neueste)
        if m:
            d, u = m.group(1), m.group(2)
            gestartet = f"{d[6:8]}.{d[4:6]}.{d[0:4]} {u[0:2]}:{u[2:4]}:{u[4:6]}"
        log.info(f"[POLL] {conn.name}: Server-Neustart erkannt ({neueste}).")
        await self._dispatch({"type": "server_restart",
                              "timestamp": gestartet,
                              "gestartet": gestartet,
                              "datei": neueste,
                              "raw": neueste}, conn)

    async def _check_ftp_health(self, conn: Optional[ServerConnection] = None):
        """Warnt im Adminlog-Feed, wenn das FTP-Polling dauerhaft fehlschlägt
        (Passwort geändert, Nitrado-Wartung), und meldet die Erholung.
        Versucht vorher, die FTP-Zugangsdaten über den Nitrado-Token zu erneuern.

        Warn-Zustand und Sperrzeit haengen an der Verbindung – sonst wuerde die
        Warnung eines Kunden die eines anderen 30 Minuten lang verschlucken.
        """
        conn = conn or connections.primary()
        if conn is None or conn.ftp is None:
            return
        ftp = conn.ftp
        fails     = ftp.consecutive_failures
        threshold = max(1, int(conn.get("ftp_fail_warn_cycles", 10) or 10))
        now = time.time()
        if fails >= threshold:
            if now - conn.ftp_warned_ts >= 1800:   # höchstens alle 30 Min erneut warnen
                conn.ftp_warned_ts = now
                if await self._try_refresh_ftp_credentials(conn):
                    # Zugangsdaten waren veraltet → mit den neuen weitermachen,
                    # keine Ausfall-Warnung nötig
                    conn.ftp_warn_active = False
                    embed = discord.Embed(
                        title="🔄 FTP-Zugang automatisch erneuert",
                        description=("Die FTP-Zugriffe schlugen wiederholt fehl – der Bot "
                                     "hat die Zugangsdaten über den Nitrado-Token neu "
                                     "geholt und die Verbindung neu aufgebaut."),
                        color=0x2ECC71)
                    await _post_feed(conn.guild_id, "adminlog", embed,
                                     service_id=conn.service_id)
                    return
                conn.ftp_warn_active = True
                embed = discord.Embed(
                    title="🚨 FTP-Verbindung gestört",
                    description=(f"**{fails} FTP-Zugriffe in Folge fehlgeschlagen** "
                                 f"(Host `{conn.get('ftp_host') or '–'}`).\n"
                                 f"Log-Feeds und Shop-Lieferungen sind unterbrochen!\n"
                                 f"Mögliche Ursachen: FTP-Passwort geändert, Nitrado-Wartung.\n"
                                 f"Letzter Fehler: `{ftp.last_error or 'unbekannt'}`"),
                    color=0xE74C3C)
                await _post_feed(conn.guild_id, "adminlog", embed,
                                 service_id=conn.service_id)
        elif fails == 0 and conn.ftp_warn_active:
            conn.ftp_warn_active = False
            conn.ftp_warned_ts   = 0.0
            embed = discord.Embed(
                title="✅ FTP-Verbindung wiederhergestellt",
                description="Der FTP-Zugriff funktioniert wieder – die Feeds laufen normal weiter.",
                color=0x2ECC71)
            await _post_feed(conn.guild_id, "adminlog", embed,
                             service_id=conn.service_id)

    async def _resolve_channel(self, channel_id: int):
        ch = self.get_channel(channel_id)
        if ch is not None:
            return ch
        try:
            return await self.fetch_channel(channel_id)
        except Exception as e:
            log.debug(f"[DISPATCH] fetch_channel({channel_id}) fehlgeschlagen: {e}")
            return None

    @staticmethod
    def _dispatch_merken(conn: Optional[ServerConnection], ev: Dict,
                         ergebnis: str, **extra) -> None:
        """Fürs Diagnose-Dashboard: die letzten Dispatch-Entscheidungen dieses
        Servers, im Klartext WARUM ein Ereignis gepostet wurde oder nicht."""
        if conn is None:
            return
        eintrag = {"zeit": time.time(), "typ": ev.get("type"), "ergebnis": ergebnis}
        eintrag.update(extra)
        conn.dispatch_verlauf.append(eintrag)

    async def _dispatch(self, ev: Dict, conn: Optional[ServerConnection] = None,
                        nebenwirkungen: bool = True):
        """Ein Log-Ereignis in die Feeds posten.

        Mit Verbindung geht es nur in deren Guild – die Ereignisse eines
        Servers haben in fremden Discord-Servern nichts zu suchen.

        ``nebenwirkungen=False`` postet **nur** das Embed und ueberspringt
        alles Buchende: Kill-Statistik, Kill-Belohnung, Kopfgelder,
        Spielzeit-Sitzungen, die Dashboard-Ereignisliste und das Merken
        gesehener Spieler. Das braucht die Diagnose-Seite, die bereits
        gelesene Log-Zeilen ein zweites Mal durchschickt: ohne diesen
        Schalter zaehlte jeder Klick denselben alten Kill erneut und zahlte
        das Kopfgeld erneut aus. Der normale Poll-Zyklus laesst den Schalter
        auf True – dort kommt jede Zeile genau einmal vorbei.
        """
        _setze_aktuellen_server(conn)
        if conn is not None and conn.guild_id is None:
            # Ohne zugeordnete Guild gibt es kein Ziel. Frueher fiel der
            # Versand hier auf ALLE konfigurierten Guilds zurueck – Kills,
            # Chat und Positionen eines Servers landeten dann bei fremden
            # Kunden, und Belohnungen wurden guilduebergreifend ausgezahlt.
            return
        # Fuer die Allowlist-Autofill der Zonen: unabhaengig davon, ob fuer
        # "connect" ueberhaupt ein Feed eingerichtet ist, sonst wuerden nur
        # Namen von Servern mit aktivem Connect-Feed gemerkt.
        if nebenwirkungen and ev.get("type") == "connect" and ev.get("player"):
            for gid_str in ([str(conn.guild_id)] if conn is not None else list(cfg.guilds)):
                cfg.record_seen_player(int(gid_str), ev["player"],
                                       conn.service_id if conn is not None else None)
        # Rückfallkette statt einem einzelnen Schlüssel: erst die feine
        # Zuordnung (Zombie Death statt "Umwelttod"), dann die grobe
        # Sammelkategorie (macht EVENT_TO_LOG wieder nutzbar – vorher toter
        # Code, weil _feed_key für jedes echte Ereignis bereits einen feinen
        # Treffer lieferte, und altbestand unter den groben Schlüsseln nie
        # wieder erreicht wurde), zuletzt "catch_all". Je Discord-Server
        # gewinnt der erste Kandidat, für den dort ein Channel gesetzt ist –
        # so geht ein erkanntes Ereignis nur noch verloren, wenn wirklich
        # nirgends ein Feed dafür eingerichtet ist.
        fein = _feed_key(ev)
        grob = DayZLogParser.EVENT_TO_LOG.get(ev["type"])
        kandidaten: List[str] = []
        for k in (fein, grob, "catch_all"):
            if k and k not in kandidaten:
                kandidaten.append(k)
        if not kandidaten:
            self._dispatch_merken(conn, ev, "kein Feed-Typ zugeordnet")
            return
        # Für die Dashboard-Karte/Event-Liste festhalten (mit Koordinaten aus
        # dem Event bzw. der letzten bekannten Spielerposition). Fehler hier
        # dürfen den Log-Dispatch niemals stören.
        rewards: Dict[int, str] = {}
        if nebenwirkungen:
            try:
                _ev_record(ev, (conn.parser if conn is not None else self.parser).player_positions,
                           service_id=(conn.service_id if conn is not None else None))
            except Exception:
                pass
            # Kill-Statistik, Sessions, Kill-Belohnung & Bounties verarbeiten
            rewards = await self._process_event_rewards(ev, conn)
        _p = conn.parser if conn is not None else self.parser
        embed = EmbedBuilder.build(ev, _p.player_positions if _p else None)
        if not embed:
            self._dispatch_merken(conn, ev, "kein Embed erzeugt", kandidaten=kandidaten)
            return
        targets = ([str(conn.guild_id)] if conn is not None
                   else list(cfg.guilds))
        _sid = conn.service_id if conn is not None else None
        for gid_str in targets:
            # Farbe, Location und Zeitstempel haengen am einzelnen Feed –
            # deshalb je Ziel eine eigene Kopie des Embeds. Die Kandidaten
            # der Reihe nach versuchen, der erste mit gesetztem Channel gilt.
            feed = None
            treffer_schluessel = None
            for kand in kandidaten:
                feed = cfg.feed_settings(int(gid_str), kand, _sid)
                if feed:
                    treffer_schluessel = kand
                    break
            if not feed:
                self._dispatch_merken(conn, ev, "kein Feed/Channel gesetzt",
                                     kandidaten=kandidaten, guild=gid_str)
                continue
            ch_id = feed["channel_id"]
            send_embed = _feed_anwenden(embed.copy(), feed)
            reward_line = rewards.get(int(gid_str))
            if reward_line:
                send_embed.add_field(name="💰 Belohnung", value=reward_line, inline=False)
            ch = await self._resolve_channel(int(ch_id))
            if ch:
                try:
                    await ch.send(embed=send_embed)
                    self._dispatch_merken(conn, ev, "gepostet",
                                         feed_typ=treffer_schluessel, channel=str(ch_id))
                except discord.Forbidden:
                    log.warning(f"[DISPATCH] Keine Rechte in Channel {ch_id} (Guild {gid_str})")
                    self._dispatch_merken(conn, ev, "keine Rechte im Channel",
                                         feed_typ=treffer_schluessel, channel=str(ch_id))
                except Exception as e:
                    log.error(f"[DISPATCH] Fehler in Guild {gid_str}: {e}")
                    self._dispatch_merken(conn, ev, f"Fehler beim Senden: {e}",
                                         feed_typ=treffer_schluessel, channel=str(ch_id))
            else:
                log.warning(f"[DISPATCH] Channel {ch_id} in Guild {gid_str} nicht gefunden")
                self._dispatch_merken(conn, ev, "Channel nicht gefunden (ID veraltet?)",
                                     feed_typ=treffer_schluessel, channel=str(ch_id))

    async def _process_event_rewards(self, ev: Dict,
                                     conn: Optional[ServerConnection] = None) -> Dict[int, str]:
        """Nebenwirkungen eines Log-Events: Kill-Statistik schreiben, Spielzeit-
        Sitzungen öffnen/schließen, Kill-Belohnung und Kopfgelder an verlinkte
        Spieler auszahlen. Gibt pro Guild eine Belohnungszeile fürs Embed zurück.

        Alles haengt am Server, von dem das Ereignis stammt: Statistik und
        Sitzungen bekommen dessen service_id, Belohnungen gehen nur an
        Verknuepfungen in dessen Guild.
        """
        out: Dict[int, str] = {}
        loop = asyncio.get_running_loop()
        t = ev["type"]
        sid = conn.service_id if conn is not None else ""
        gid_ev = conn.guild_id if conn is not None else None
        try:
            if t == "connect":
                pid = ev.get("player_id")
                pid = pid if pid and pid != "Unbekannt" else None
                await loop.run_in_executor(None, db.open_session, sid, ev["player"], pid)
                if pid:
                    await loop.run_in_executor(None, db.update_link_id,
                                               ev["player"], pid, gid_ev)

            elif t == "disconnect":
                await loop.run_in_executor(None, db.close_session, sid, ev["player"])

            elif t == "kill_pvp":
                killer = ev.get("killer") or ""
                victim = ev.get("victim") or ""
                await loop.run_in_executor(
                    None, db.record_kill, sid, killer, ev.get("killer_id"),
                    victim, ev.get("victim_id"), ev.get("weapon"), ev.get("distance"))
                for nm, key in ((killer, "killer_id"), (victim, "victim_id")):
                    pid = ev.get(key)
                    if nm and pid and pid != "Unbekannt":
                        await loop.run_in_executor(None, db.update_link_id, nm, pid, gid_ev)
                if killer and victim and killer.lower() != victim.lower():
                    reward = max(0, int((conn.get("kill_reward", 0) if conn is not None
                                         else cfg.config.get("kill_reward", 0)) or 0))
                    links = await loop.run_in_executor(None, db.links_for_name,
                                                       killer, gid_ev)
                    for lk in links:
                        gid, uid = int(lk["guild_id"]), int(lk["user_id"])
                        parts: List[str] = []
                        total = 0
                        if reward > 0:
                            total += reward
                            parts.append(f"+{_fmt_money(reward)} Kill-Belohnung")
                        bounty = await loop.run_in_executor(
                            None, db.claim_bounties, gid, victim, uid)
                        if bounty > 0:
                            total += bounty
                            parts.append(f"+{_fmt_money(bounty)} Kopfgeld 🎯")
                        if total > 0:
                            await loop.run_in_executor(None, db.add_wallet, gid, uid, total)
                            out[gid] = f"{' · '.join(parts)} → <@{uid}>"
        except Exception as e:
            log.error(f"[REWARD] Event-Verarbeitung fehlgeschlagen: {e}")
        return out

    async def _credit_playtime(self, conn: Optional[ServerConnection] = None):
        """Schreibt verlinkten Spielern volle Spielzeit-Blöcke gut
        (playtime_reward: amount pro interval_minutes, z.B. 500 pro 30 Min)."""
        if conn is not None and conn.guild_id is None:
            return          # kein Discord-Server → keine Auszahlung
        conf = ((conn.get("playtime_reward") if conn is not None
                 else cfg.config.get("playtime_reward")) or {})
        amount = max(0, int(conf.get("amount", 0)))
        if amount <= 0:
            return
        interval = max(1, int(conf.get("interval_minutes", 30))) * 60
        loop = asyncio.get_running_loop()
        try:
            # Verpasste Connect-Events abfangen: verlinkte Spieler, die laut Log
            # gerade aktiv sind, aber keine offene Sitzung haben → Sitzung öffnen
            positions = dict((conn.parser or self.parser).player_positions
                             if conn is not None else self.parser.player_positions)
            sid = conn.service_id if conn is not None else ""
            gid_conn = conn.guild_id if conn is not None else None
            await loop.run_in_executor(None, db.sync_sessions_from_positions,
                                       sid, positions, 300, gid_conn)
            due = await loop.run_in_executor(None, db.playtime_credits_due, sid, interval)
            for entry in due:
                links = await loop.run_in_executor(None, db.links_for_name,
                                                   entry["name"], gid_conn)
                for lk in links:
                    gid, uid = int(lk["guild_id"]), int(lk["user_id"])
                    credit = amount * int(entry["blocks"])
                    await loop.run_in_executor(None, db.add_wallet, gid, uid, credit)
                    log.info(f"[PLAYTIME] {entry['name']}: +{credit} für <@{uid}> (Guild {gid})")
        except Exception as e:
            log.error(f"[PLAYTIME] Gutschrift fehlgeschlagen: {e}")


bot = DayZBot()


# ══════════════════════════════════════════════════════════════
#  Berechtigungs-Prüfung
# ══════════════════════════════════════════════════════════════
def _member_has_role_ids(member: discord.Member, role_ids: List) -> bool:
    """Prüft ob ein Member mindestens eine der konfigurierten Rollen-IDs besitzt."""
    if not role_ids:
        return False
    try:
        wanted = {int(r) for r in role_ids}
    except (TypeError, ValueError):
        return False
    return any(r.id in wanted for r in member.roles)

def _is_admin(interaction: discord.Interaction) -> bool:
    """Admin = Rolle aus admin_role_ids ODER Discord-Administrator.
    admin_role_name bleibt als Fallback erhalten (Abwärtskompatibilität)."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    # Rollen kommen von den Servern dieser Guild – so kann jeder Kunde seine
    # eigenen Admin-Rollen festlegen statt die des Betreibers zu erben. Ohne
    # zugeordneten Server gibt es auch keine Admin-Rolle: sonst koennte eine
    # fremde Guild ohne Premium ueber einen Rollennamen wie "DayZ Admin"
    # (die Auslieferungs-Vorgabe) /setup token als Admin ausfuehren.
    #
    # Bei mehreren Servern an derselben Guild zaehlt die VEREINIGUNG ihrer
    # Rollen. Nur den ersten zu fragen waere von der Reihenfolge in der
    # connections.json abhaengig – und diese Pruefung laeuft VOR der
    # Server-Auswahl des Befehls, kann den `server`-Parameter also gar nicht
    # kennen.
    for _c in _conns_of(interaction):
        if _member_has_role_ids(interaction.user, _c.get("admin_role_ids", [])):
            return True
        role_name = _c.get("admin_role_name", "")
        if role_name and any(r.name == role_name for r in interaction.user.roles):
            return True
    return False

def _is_economy_admin(interaction: discord.Interaction) -> bool:
    """Economy-Admin = economy_admin_role_ids ODER voller Admin."""
    if _is_admin(interaction):
        return True
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    # Wie bei _is_admin: Vereinigung ueber alle Server dieser Guild.
    return any(_member_has_role_ids(interaction.user,
                                    _c.get("economy_admin_role_ids", []))
               for _c in _conns_of(interaction))

async def _deny(interaction: discord.Interaction):
    msg = ("❌ No permission. You need one of the configured admin roles "
           "(`admin_role_ids` in config.json) or Administrator rights.")
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def _conn_of(interaction: discord.Interaction) -> Optional[ServerConnection]:
    """Der Nitrado-Server, den diese Guild verwalten darf.

    In Direktnachrichten gibt es keine Guild – dort gilt der Hauptserver,
    damit Befehle wie frueher auch per DM funktionieren.
    """
    if interaction.guild_id is None:
        # In einer Direktnachricht gibt es keine Guild. Frueher galt hier der
        # Hauptserver – damit haette jeder per DM die Daten des Betreibers
        # abrufen koennen. Jetzt zaehlt der Server, der diesem Discord-Konto
        # gehoert; wer keinen hat, bekommt "kein Premium".
        eigene = connections.for_owner(interaction.user.id)
        return eigene[0] if eigene else None
    return connections.for_guild(interaction.guild_id)


def _conns_of(interaction: discord.Interaction) -> List[ServerConnection]:
    """ALLE Server, die diese Guild (bzw. dieses Konto per DM) verwalten darf."""
    if interaction.guild_id is None:
        return connections.for_owner(interaction.user.id)
    return connections.all_for_guild(interaction.guild_id)


def _conn_waehlen(interaction: discord.Interaction, server: Optional[str] = None
                  ) -> Tuple[Optional[ServerConnection], Optional[str]]:
    """Den gemeinten Nitrado-Server bestimmen: ``(Verbindung, Fehlermeldung)``.

    Verwaltet eine Guild nur einen Server, bleibt alles wie bisher – der
    ``server``-Parameter ist dann entbehrlich. Ab zwei Servern ist er Pflicht:
    ein stiller Standard koennte sonst ``/stoppen`` auf dem falschen Server
    ausloesen.
    """
    verfuegbar = _conns_of(interaction)
    if not verfuegbar:
        return None, PREMIUM_MISSING_TEXT

    wahl = (server or "").strip()
    if wahl:
        gesucht = wahl.lower()
        treffer = next((c for c in verfuegbar if c.service_id == wahl), None)
        if treffer is None:
            treffer = next((c for c in verfuegbar if c.name.lower() == gesucht), None)
        if treffer is None:
            passend = [c for c in verfuegbar if gesucht in c.name.lower()]
            treffer = passend[0] if len(passend) == 1 else None
        if treffer is None:
            return None, (f"❌ Kein Server namens `{wahl}` an diesem Discord-Server.\n"
                          + _server_liste(verfuegbar))
        _setze_aktuellen_server(treffer)
        return treffer, None

    if len(verfuegbar) == 1:
        _setze_aktuellen_server(verfuegbar[0])
        return verfuegbar[0], None

    return None, ("❌ Dieser Discord-Server verwaltet mehrere Nitrado-Server – "
                  "gib mit `server:` an, welchen du meinst.\n"
                  + _server_liste(verfuegbar))


def _server_liste(conns: List[ServerConnection]) -> str:
    """Aufzaehlung fuer Fehlermeldungen: Name und Service-ID je Zeile."""
    return "\n".join(f"• **{c.name}** (`{c.service_id}`)" for c in conns[:10])


async def _server_autocomplete(interaction: discord.Interaction,
                               current: str) -> List[app_commands.Choice[str]]:
    """Vorschlaege fuer den ``server``-Parameter: die Server dieser Guild."""
    cur = (current or "").strip().lower()
    out: List[app_commands.Choice] = []
    for conn in _conns_of(interaction):
        if cur and cur not in conn.name.lower() and cur not in conn.service_id:
            continue
        out.append(app_commands.Choice(name=f"{conn.name} ({conn.service_id})"[:100],
                                       value=conn.service_id))
        if len(out) >= 25:
            break
    return out


def _gewaehlter_server(interaction: discord.Interaction) -> Optional[str]:
    """Der bereits eingetippte ``server``-Wert – fuer die anderen Autocompletes.

    Discord stellt die schon ausgefuellten Optionen unter ``namespace`` bereit.
    Damit zeigt z. B. die Zonen-Vervollstaendigung die Zonen des gewaehlten
    Servers statt die des Leitservers.
    """
    try:
        return getattr(interaction.namespace, "server", None)
    except Exception:  # noqa: BLE001
        return None


def _ac_conns(interaction: discord.Interaction) -> List[ServerConnection]:
    """Aus welchen Servern zieht eine Vervollstaendigung ihre Vorschlaege?

    Ist ``server`` schon ausgefuellt, genau dieser eine. Sonst alle Server der
    Guild – der Nutzer tippt oft zuerst den Gegenstand und erst danach den
    Server, und dann darf die Liste nicht auf den Leitserver eingeengt sein.
    """
    gid = interaction.guild_id or 0
    alle = connections.all_for_guild(gid)
    wahl = _gewaehlter_server(interaction)
    if wahl:
        treffer = [c for c in alle if c.service_id == str(wahl)]
        if treffer:
            return treffer
    return alle


def _conn_store(conn: ServerConnection, key: str, value: Any) -> None:
    """Serverspezifischen Wert in der Verbindung ablegen.

    Beim Hauptserver zusaetzlich in der config.json, solange Log-Abruf und
    Dashboard dort noch mitlesen – sonst liefen beide Seiten auseinander.
    """
    conn.set(key, value)
    connections.save()
    if connections.primary() is conn:
        cfg.config[key] = value
        cfg.save_config()


def _bans_of(conn: Optional[ServerConnection]) -> Dict[str, Dict]:
    """Lokale Ban-Metadaten (Grund/Datum/von) EINES Servers.

    Gebannt wird bei Nitrado – diese Datei haelt nur die Zusatzangaben fuer
    ``/banlist``. Ohne die Trennung sähe ein Kunde Grund und Admin-Namen des
    Bans eines anderen, sobald derselbe Spielername dort gesperrt ist.
    """
    sid = conn.service_id if conn is not None else ""
    eimer = cfg.bans.get(sid)
    if not isinstance(eimer, dict):
        eimer = {}
        cfg.bans[sid] = eimer
    return eimer


def _migriere_eigene_einstellungen() -> None:
    """Einstellungen des Betreibers einmalig in die Verbindung seines Servers holen.

    Fuer die Schluessel in ``_EIGENE_EINSTELLUNGEN`` ist die config.json seit der
    Mandantentrennung keine Rueckfallebene mehr – sonst wanderte jede Aenderung
    des Betreibers bei allen Kunden mit. Ohne diese Uebernahme faende der
    Hauptserver nach einem Update seine EIGENEN Werte nicht mehr: Waehrung,
    Startguthaben, Belohnungen und vor allem ``admin_role_ids`` staenden dann
    auf der Auslieferungs-Vorgabe.

    Laeuft genau einmal, auch wenn die connections.json schon existiert; das
    Merkmal dafuer steht in der Verbindung selbst. So bleibt ein spaeter vom
    Betreiber bewusst geloeschter Wert geloescht.
    """
    # Bewusst NICHT connections.primary(): das faellt ohne service_id in der
    # config.json auf die erste beliebige Verbindung zurueck – das waere dann
    # ein Kunde, und die Werte des Betreibers laegen dauerhaft bei ihm.
    haupt = connections.for_service(cfg.config.get("service_id"))
    if haupt is None or haupt.data.get("_eigene_uebernommen"):
        return
    uebernommen = []
    for key in sorted(ServerConnection._EIGENE_EINSTELLUNGEN):
        if key not in cfg.config or key in haupt.data:
            continue
        # Nur was der Betreiber wirklich angepasst hat. Bei einer Neuinstallation
        # entspricht alles der Vorgabe – dann bleibt die Verbindung schlank und
        # spaetere Aenderungen an DEFAULT_CONFIG erreichen sie weiterhin.
        if cfg.config[key] == DEFAULT_CONFIG.get(key):
            continue
        haupt.data[key] = copy.deepcopy(cfg.config[key])
        uebernommen.append(key)
    haupt.data["_eigene_uebernommen"] = True
    connections.save()
    if uebernommen:  # noqa: SIM102 – Logzeile nur bei tatsaechlicher Uebernahme
        log.info(f"[CONN] Eigene Einstellungen des Hauptservers uebernommen: "
                 f"{', '.join(uebernommen)}")


def _migriere_feed_typen() -> None:
    """Die alten Sammel-Feeds einmalig entfernen (Umstellung auf FEED_TYPES).

    Aus "Umwelttode" sind Zombie Death, Wolf Death, Fall Death ... geworden.
    Eine automatische Umdeutung waere geraten, deshalb wird neu angefangen –
    aber erst NACH einer Sicherungskopie, damit nichts unwiederbringlich weg
    ist. Die fuenf Bot-Feeds (Shop, Economy, Status, Restart, Zone) bilden 1:1
    ab und bleiben unangetastet.
    """
    # Die Marke gehoert in die config.json und NICHT in cfg.guilds: dort sind
    # die Schluessel Guild-IDs, und mehrere Stellen machen int(gid) darauf.
    marke = "feedtypen_umgestellt"
    if cfg.config.get(marke):
        return
    betroffen = []
    for gid, gdata in cfg.guilds.items():
        if not isinstance(gdata, dict):
            continue
        eimer = [gdata] + [v for v in (gdata.get("servers") or {}).values()
                           if isinstance(v, dict)]
        for e in eimer:
            for alt in _ALTE_EREIGNIS_FEEDS:
                if e.pop(alt, None) is not None:
                    betroffen.append(f"{gid}/{alt}")
    if betroffen:
        try:
            ziel = f"{GUILDS_FILE}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            with open(GUILDS_FILE, "r", encoding="utf-8") as f:
                inhalt = f.read()
            with open(ziel, "w", encoding="utf-8") as f:
                f.write(inhalt)
            log.info(f"[FEEDS] Sicherung der bisherigen Feeds: {ziel}")
        except OSError as e:  # noqa: BLE001 – Sicherung ist Zugabe
            log.warning(f"[FEEDS] Sicherung nicht moeglich ({e}) – fahre fort.")
        log.info(f"[FEEDS] Umstellung auf die feinen Feed-Typen: "
                 f"{len(betroffen)} alte Sammel-Feeds entfernt. "
                 f"Bitte im Dashboard unter „Feeds“ neu einrichten.")
        cfg.save_guilds()
    cfg.config[marke] = True
    cfg.save_config()


def _migriere_ban_metadaten() -> None:
    """Alte flache banlist.json (Name → Angaben) dem Hauptserver zuordnen."""
    daten = cfg.bans
    if not isinstance(daten, dict) or not daten:
        return
    flach = {k: v for k, v in daten.items()
             if isinstance(v, dict) and ("reason" in v or "banned_at" in v)}
    if not flach:
        return
    haupt = connections.primary()
    sid = haupt.service_id if haupt is not None else ""
    eimer = daten.get(sid) if isinstance(daten.get(sid), dict) else {}
    eimer.update(flach)
    for k in flach:
        daten.pop(k, None)
    daten[sid] = eimer
    cfg.save_bans()
    log.info(f"[BAN] {len(flach)} lokale Ban-Angaben dem Server {sid or '-'} zugeordnet.")


async def _require_conn(interaction: discord.Interaction,
                        need_ftp: bool = False,
                        server: Optional[str] = None) -> Optional[ServerConnection]:
    """Die einsatzbereite Verbindung dieses Befehls – sonst None.

    Ersetzt das fruehere _require_nitrado: statt einer globalen Verbindung
    loest jeder Befehl jetzt den Server auf, der zu seiner Guild gehoert.
    ``server`` waehlt bei mehreren Servern derselben Guild den gemeinten aus.
    Bei None hat der Aufrufer bereits eine Antwort bekommen und bricht mit
    `return` ab.
    """
    conn, auswahlfehler = _conn_waehlen(interaction, server)
    if conn is not None and conn.api is not None and (not need_ftp or conn.ftp is not None):
        return conn

    if conn is None:
        msg = auswahlfehler or PREMIUM_MISSING_TEXT
    elif conn.api is None:
        msg = ("❌ Nitrado ist für diesen Server noch nicht eingerichtet.\n"
               "Führe `/setup token <dein-nitrado-token>` aus und wähle "
               "deinen Server im Dropdown aus.")
    else:
        msg = ("❌ Für diesen Server fehlt der FTP-Zugang – ohne ihn sind "
               "Logs und Spielerpositionen nicht lesbar.\n"
               "`/ftp_scan` versucht die Erkennung erneut.")
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return None


# ══════════════════════════════════════════════════════════════
#  /setup – Alle Log-Channels konfigurieren
# ══════════════════════════════════════════════════════════════
def _panel_view_registrieren(conn: "ServerConnection") -> None:
    """Persistente Whitelist-Panel-View fuer diesen Server anmelden.

    Noetig fuer Server, die zur Laufzeit dazukommen – ohne das reagiert ihr
    Panel-Knopf erst nach dem naechsten Bot-Neustart.
    """
    try:
        if conn.service_id and bot is not None and getattr(bot, "user", None):
            bot.add_view(WhitelistPanelView(conn.service_id))
    except Exception as e:  # noqa: BLE001 – doppelte Anmeldung ist harmlos
        log.debug(f"[WL] Panel-View {conn.service_id}: {e}")




# ══════════════════════════════════════════════════════════════
#  /show_feeds – Alle Feed-Channels auf einen Blick
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="show_feeds", description="📡 Zeigt alle Feed-Channels und ihren Status")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_show_feeds(interaction: discord.Interaction, server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    _conn_show, _fehler = _conn_waehlen(interaction, server)
    if _conn_show is None:
        return await interaction.response.send_message(
            _fehler or PREMIUM_MISSING_TEXT, ephemeral=True)
    _sid_show = _conn_show.service_id
    # FEED_TYPES statt LOG_TYPES: die feinen Feeds (kill, connect, zombie_death
    # …) sind es, was das Dashboard tatsaechlich schreibt. Mit LOG_TYPES
    # (killfeed, joinleave, …) meldete dieser Befehl vorher IMMER "0 Feeds
    # aktiv", selbst wenn im Dashboard alles korrekt eingerichtet war – die
    # beiden Schluesselmengen ueberschneiden sich kaum.
    active = {}
    for ft in FEED_TYPES:
        # Im Owner-DM-Pfad ist interaction.guild_id None – dann die Guild des
        # Servers nehmen, sonst meldet der Befehl dort faelschlich "0 Feeds".
        feed = cfg.feed_settings(interaction.guild_id or _conn_show.guild_id,
                                 ft, _sid_show)
        if feed:
            active[ft] = feed["channel_id"]
    inactive = [ft for ft in FEED_TYPES if ft not in active]

    embed = discord.Embed(
        title=f"📡 Feed-Channel Übersicht – {_conn_show.name}",
        description=(
            f"**{len(active)}** Feed{'s' if len(active) != 1 else ''} aktiv  •  "
            f"**{len(inactive)}** nicht konfiguriert"
        ),
        color=0x2ECC71 if active else 0x95A5A6,
    )

    def _feld_text(schluessel: List[str], anzeige) -> str:
        # FEED_TYPES hat gut 50 Eintraege – ungekuerzt sprengt das locker das
        # 1024-Zeichen-Limit eines Embed-Feldes.
        lines = [anzeige(k) for k in schluessel[:20]]
        text = "\n".join(lines)
        rest = len(schluessel) - 20
        if rest > 0:
            text += f"\n… und {rest} weitere"
        return text[:1024]

    # ── Aktive Feeds ──────────────────────────────────────────
    if active:
        def _aktiv_zeile(ft):
            ch_id = active[ft]
            ch = interaction.guild.get_channel(int(ch_id)) if interaction.guild else None
            ch_mention = ch.mention if ch else f"<#{ch_id}> *(Channel nicht gefunden)*"
            return f"{FEED_TYPES[ft]['emoji']} {FEED_TYPES[ft]['label']} → {ch_mention}"
        embed.add_field(name="✅ Aktive Feeds",
                        value=_feld_text(list(active), _aktiv_zeile), inline=False)

    # ── Inaktive Feeds ────────────────────────────────────────
    if inactive:
        embed.add_field(
            name="⚪ Nicht konfiguriert",
            value=_feld_text(inactive, lambda ft: f"❌ {FEED_TYPES[ft]['label']}"),
            inline=False)

    embed.set_footer(text="Feeds werden im Dashboard unter „Feeds“ geändert")
    await interaction.response.send_message(embed=embed, ephemeral=True)


cmd_show_feeds.autocomplete("server")(_server_autocomplete)


# ══════════════════════════════════════════════════════════════
#  /neustart – Server Neustart
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name=app_commands.locale_str("neustart"),
                  description="🔄 Startet den DayZ Server neu")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_neustart(interaction: discord.Interaction,
                       server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, server=server)
    if conn is None:
        return
    await interaction.response.defer()
    ok, msg = await conn.api.restart()
    embed = discord.Embed(
        title="🔄 Server Neustart",
        description=msg,
        color=0x2ECC71 if ok else 0xE74C3C
    )
    embed.set_footer(text=f"Ausgeführt von {interaction.user.display_name}")
    await interaction.followup.send(embed=embed)


# ══════════════════════════════════════════════════════════════
#  /stoppen – Server stoppen
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name=app_commands.locale_str("stoppen"),
                  description="⏹️ Stoppt den DayZ Server")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_stoppen(interaction: discord.Interaction,
                      server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, server=server)
    if conn is None:
        return
    await interaction.response.defer()
    ok, msg = await conn.api.stop()
    embed = discord.Embed(
        title="⏹️ Server gestoppt",
        description=msg,
        color=0xF39C12 if ok else 0xE74C3C
    )
    embed.set_footer(text=f"Ausgeführt von {interaction.user.display_name}")
    await interaction.followup.send(embed=embed)


# ══════════════════════════════════════════════════════════════
#  /serverstatus – Status abrufen (Nitrado API + direkter A2S-Ping)
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="serverstatus", description="📊 Zeigt den aktuellen Server-Status")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_status(interaction: discord.Interaction,
                     server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, server=server)
    if conn is None:
        return
    await interaction.response.defer()

    # ── 1. Nitrado API (parallel zum A2S-Ping) ────────────────
    loop       = asyncio.get_running_loop()
    nitrado_task = loop.run_in_executor(None, lambda: None)   # Placeholder
    info = await conn.api.get_info()

    # ── 2. Direkter A2S UDP-Ping ─────────────────────────────
    srv_ip    = conn.get("server_ip",  "")
    qport     = int(conn.get("query_port",  2302))
    rcon_port = int(conn.get("rcon_port", 2310) or 2310)

    a2s: Optional[Dict] = None
    a2s_ping_ms = -1
    if srv_ip:
        import time as _t
        t0 = _t.monotonic()
        a2s = await loop.run_in_executor(None, a2s_query, srv_ip, qport)
        a2s_ping_ms = int((_t.monotonic() - t0) * 1000)

    # ── 3. Status aus den Quellen zusammenbauen ───────────────
    # Nitrado-Daten
    n_status = ""
    n_players, n_max = "?", "?"
    n_game, n_ip, n_port = "DayZ", srv_ip or "–", "–"
    if info:
        n_status  = info.get("status", "")
        q         = info.get("query", {})
        n_players = str(q.get("player_current", "?"))
        n_max     = str(q.get("player_max", "?"))
        n_game    = info.get("game_human", "DayZ")
        n_ip      = info.get("ip", srv_ip or "–")
        n_port    = str(info.get("port", "–"))

    # Echtzeit-Daten bevorzugen wenn A2S antwortet
    if a2s:
        players_str = f"{a2s['players']}/{a2s['max_players']}"
        is_up       = True
        ping_str    = f"{a2s_ping_ms} ms"
        mapname     = a2s.get("map", "–")
        srv_name    = a2s.get("name", "–")
    else:
        players_str = f"{n_players}/{n_max}"
        is_up       = "started" in n_status.lower() or "running" in n_status.lower()
        ping_str    = "Timeout (offline?)" if srv_ip else "Keine IP konfiguriert"
        mapname     = "–"
        srv_name    = "–"

    color   = 0x2ECC71 if is_up else 0xE74C3C
    st_icon = "🟢" if is_up else "🔴"
    n_st    = n_status.upper() if n_status else ("ONLINE" if is_up else "OFFLINE")

    embed = discord.Embed(title="📊 Server Status", color=color)

    # Zeile 1: Status, Spieler, Ping
    embed.add_field(name="Status",    value=f"{st_icon} {n_st}",  inline=True)
    embed.add_field(name="Spieler",   value=players_str,           inline=True)
    embed.add_field(name="Ping",      value=ping_str,              inline=True)

    # Zeile 2: IP/Port, Query-Port, RCON-Port
    display_ip = n_ip if n_ip not in ("–", "") else (srv_ip or "–")
    embed.add_field(name="Server-IP", value=f"`{display_ip}`",           inline=True)
    embed.add_field(name="Game-Port / Query",
                    value=f"`{n_port}` / `{qport}`",                     inline=True)
    embed.add_field(name="RCON-Port", value=f"`{rcon_port}`",            inline=True)

    # Zeile 3: Map + Servername (nur wenn A2S geantwortet hat)
    if a2s:
        embed.add_field(name="Map",          value=mapname,  inline=True)
        embed.add_field(name="Servername",   value=srv_name[:50], inline=True)
        lock = "🔒 Passwort" if a2s.get("password") else "🔓 Offen"
        embed.add_field(name="Zugang",       value=lock,     inline=True)

    src = []
    if info:   src.append("Nitrado API")
    if a2s:    src.append("Direkter Ping (A2S)")
    embed.set_footer(text=f"Quellen: {', '.join(src) or '–'} | "
                          f"Service ID: {conn.service_id or '–'}")
    await interaction.followup.send(embed=embed)


# ══════════════════════════════════════════════════════════════
#  /betreiber – Einstellungen, die nur den Bot-BETREIBER angehen, nicht
#  irgendeinen Kunden. Gesperrt auf den Eigentuemer der Bot-ANWENDUNG,
#  NICHT auf eine Guild-Admin-Rolle – eine Admin-Rolle in Kunde XYs Discord
#  soll hier keinen Zugriff geben.
# ══════════════════════════════════════════════════════════════
_EIGENTUEMER_IDS: set = set()


async def _ist_bot_eigentuemer(user: discord.abc.User) -> bool:
    """Gehoert die Bot-Anwendung diesem Discord-Konto?

    ``discord.Client`` hat – anders als ``commands.Bot`` – **kein**
    ``is_owner()``; der Aufruf lief hier in einen AttributeError und der
    Befehl antwortete nie ("Die Anwendung reagiert nicht"). Die Auskunft
    kommt deshalb direkt aus den Anwendungsdaten und wird gemerkt, damit
    nicht jeder Aufruf eine HTTP-Runde zu Discord kostet.

    Beruecksichtigt auch Team-Anwendungen: gehoert der Bot einem Discord-Team,
    zaehlt jedes Team-Mitglied als Eigentuemer.
    """
    if not _EIGENTUEMER_IDS:
        info = bot.application or await bot.application_info()
        if info is None:
            return False
        if getattr(info, "team", None):
            _EIGENTUEMER_IDS.update(int(m.id) for m in info.team.members)
        elif getattr(info, "owner", None):
            _EIGENTUEMER_IDS.add(int(info.owner.id))
    return int(user.id) in _EIGENTUEMER_IDS


betreiber_group = app_commands.Group(
    name="betreiber", description="🛠️ Einstellungen für den Bot-Betreiber")


@betreiber_group.command(
    name="alarm_channel",
    description="🛠️ (Nur Bot-Eigentümer) Legt fest, wohin Ausfall-/Anfrage-Meldungen gehen")
@app_commands.describe(channel="Channel in DEINEM eigenen Discord für Betriebsmeldungen")
async def betreiber_alarm_channel(interaction: discord.Interaction,
                                  channel: discord.TextChannel):
    # ZUERST bestaetigen, dann erst pruefen: die Eigentuemer-Abfrage holt beim
    # ersten Aufruf die Anwendungsdaten von Discord. Dauert das (oder etwas
    # danach) laenger als 3s, verfaellt die Interaktion und Discord zeigt nur
    # noch "Die Anwendung reagiert nicht" – ohne dass man je erfaehrt, woran
    # es lag.
    await interaction.response.defer(ephemeral=True)
    try:
        ist_eigentuemer = await _ist_bot_eigentuemer(interaction.user)
    except Exception as e:  # noqa: BLE001
        log.error(f"[BETREIBER] Eigentümer-Prüfung fehlgeschlagen: {e}")
        return await interaction.followup.send(
            f"❌ Eigentümer-Prüfung bei Discord fehlgeschlagen: `{e}`", ephemeral=True)
    if not ist_eigentuemer:
        return await interaction.followup.send(
            "❌ Nur der Bot-Eigentümer darf das einstellen.", ephemeral=True)
    cfg.config["betreiber_alarm_channel_id"] = str(channel.id)
    cfg.save_config()
    await interaction.followup.send(
        f"✅ Betriebsmeldungen (Ausfälle, Erholung, neue Premium-Anfragen) gehen "
        f"jetzt an {channel.mention}.", ephemeral=True)


bot.tree.add_command(betreiber_group)


# ══════════════════════════════════════════════════════════════
#  /auto – Geplante automatische Server-Neustarts
# ══════════════════════════════════════════════════════════════
auto_group = app_commands.Group(name="auto", description="⏰ Automatische Server-Neustarts planen")


class AutoRestartView(discord.ui.View):
    """Uhrzeit-Auswahl für /auto restart: Stunde (0–23) + Minute (:00/:30).
    Zwei Dropdowns, weil Discord max. 25 Optionen pro Select erlaubt."""

    def __init__(self, interaction: discord.Interaction, interval_hours: int,
                 service_id: Optional[str] = None):
        super().__init__(timeout=180)
        self.author_id      = interaction.user.id
        self.interval_hours = interval_hours
        # Der Server wird beim Aufruf festgehalten: zwischen Dropdown und
        # Bestaetigen koennte sich die Zuordnung sonst geaendert haben.
        self.service_id     = str(service_id or "")
        self.hour:   Optional[int] = None
        self.minute: Optional[int] = None

    async def interaction_check(self, itx: discord.Interaction) -> bool:
        if itx.user.id != self.author_id:
            await itx.response.send_message(
                "❌ Nur wer den Befehl aufgerufen hat, kann hier auswählen.", ephemeral=True)
            return False
        return True

    @discord.ui.select(placeholder="🕐 Stunde der ersten Ausführung (0–23 Uhr)",
                       options=[discord.SelectOption(label=f"{h:02d} Uhr", value=str(h))
                                for h in range(24)])
    async def sel_hour(self, itx: discord.Interaction, select: discord.ui.Select):
        self.hour = int(select.values[0])
        await itx.response.defer()

    @discord.ui.select(placeholder="⏱️ Minute (:00 oder :30)",
                       options=[discord.SelectOption(label=":00", value="0"),
                                discord.SelectOption(label=":30", value="30")])
    async def sel_minute(self, itx: discord.Interaction, select: discord.ui.Select):
        self.minute = int(select.values[0])
        await itx.response.defer()

    @discord.ui.button(label="✅ Aktivieren", style=discord.ButtonStyle.success)
    async def confirm(self, itx: discord.Interaction, button: discord.ui.Button):
        if self.hour is None or self.minute is None:
            return await itx.response.send_message(
                "❌ Bitte zuerst Stunde und Minute auswählen.", ephemeral=True)
        conn = (connections.for_service(self.service_id) if self.service_id
                else _conn_of(itx))
        if conn is None:
            return await itx.response.send_message(PREMIUM_MISSING_TEXT, ephemeral=True)
        # Zwischen Aufruf und Bestaetigen liegen bis zu 180 Sekunden. In der
        # Zeit kann der Server einem anderen Discord-Server zugeordnet worden
        # sein oder die Person ihre Adminrolle verloren haben – sonst liesse
        # sich hier noch ein Neustartplan fuer einen fremden Server setzen.
        if conn.guild_id is not None and itx.guild_id \
                and int(conn.guild_id) != int(itx.guild_id):
            return await itx.response.send_message(
                "❌ Dieser Server gehört inzwischen zu einem anderen Discord-Server. "
                "Bitte `/auto restart` neu aufrufen.", ephemeral=True)
        if not _is_admin(itx):
            return await itx.response.send_message(
                "❌ Dir fehlt inzwischen die nötige Rolle.", ephemeral=True)
        first = f"{self.hour:02d}:{self.minute:02d}"
        _conn_store(conn, "auto_restart_schedule", {
            "enabled": True, "first_time": first, "interval_hours": self.interval_hours})
        bot._restart_announced = {k for k in bot._restart_announced
                                  if k[0] != conn.service_id}
        nxt = bot._next_scheduled_restart(conn)
        for child in self.children:
            child.disabled = True
        e = discord.Embed(
            title="⏰ Auto-Restart aktiviert",
            description=(f"Erste Ausführung: **{first} Uhr** · "
                         f"Intervall: **alle {self.interval_hours} Stunde(n)**\n"
                         f"Nächster Neustart: <t:{int(nxt)}:F> (<t:{int(nxt)}:R>)\n"
                         f"Ankündigungen **15/5/1 Min** vorher im `restart`-Feed-Channel "
                         f"(`/setup feeds restart`, Fallback: Adminlog)."),
            color=0x2ECC71)
        await itx.response.edit_message(embed=e, view=self)
        self.stop()


@auto_group.command(name="restart",
                    description="⏰ Plant automatische Neustarts (Startzeit per Dropdown + Intervall)")
@app_commands.describe(intervall="Abstand in Stunden, z.B. 2 = alle 2 Stunden (1–24)",
                       server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def auto_restart(interaction: discord.Interaction,
                       intervall: app_commands.Range[int, 1, 24],
                       server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    _conn_ar, _fehler = _conn_waehlen(interaction, server)
    if _conn_ar is None:
        return await interaction.response.send_message(
            _fehler or PREMIUM_MISSING_TEXT, ephemeral=True)
    view = AutoRestartView(interaction, int(intervall), _conn_ar.service_id)
    e = discord.Embed(
        title="⏰ Auto-Restart einrichten",
        description=(f"Intervall: **alle {int(intervall)} Stunde(n)**\n\n"
                     f"Wähle unten die Uhrzeit der **ersten Ausführung** "
                     f"(danach immer im gewählten Intervall) und bestätige."),
        color=0x5865F2)
    await interaction.response.send_message(embed=e, view=view, ephemeral=True)


@auto_group.command(name="off", description="⏹️ Deaktiviert die geplanten Neustarts")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def auto_off(interaction: discord.Interaction,
                   server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn, _fehler = _conn_waehlen(interaction, server)
    if conn is None:
        return await interaction.response.send_message(
            _fehler or PREMIUM_MISSING_TEXT, ephemeral=True)
    sched = dict(conn.get("auto_restart_schedule") or {})
    was_on = bool(sched.get("enabled"))
    sched["enabled"] = False
    _conn_store(conn, "auto_restart_schedule", sched)
    bot._restart_announced = {k for k in bot._restart_announced
                              if k[0] != conn.service_id}
    await interaction.response.send_message(
        "⏹️ Geplante Neustarts deaktiviert." if was_on
        else "ℹ️ Es waren keine geplanten Neustarts aktiv.", ephemeral=True)


@auto_group.command(name="status", description="📋 Zeigt den aktuellen Restart-Zeitplan")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def auto_status(interaction: discord.Interaction,
                      server: Optional[str] = None):
    conn, _fehler = _conn_waehlen(interaction, server)
    if conn is None:
        return await interaction.response.send_message(
            _fehler or PREMIUM_MISSING_TEXT, ephemeral=True)
    sched = conn.get("auto_restart_schedule") or {}
    if not sched.get("enabled"):
        return await interaction.response.send_message(
            "ℹ️ Keine geplanten Neustarts aktiv. Einrichten: `/auto restart`.", ephemeral=True)
    nxt = bot._next_scheduled_restart(conn)
    e = discord.Embed(
        title="⏰ Auto-Restart Zeitplan",
        description=(f"Startzeit: **{sched.get('first_time', '?')} Uhr** · "
                     f"Intervall: **alle {sched.get('interval_hours', '?')} Stunde(n)**\n"
                     f"Nächster Neustart: <t:{int(nxt)}:F> (<t:{int(nxt)}:R>)"),
        color=0x5865F2)
    await interaction.response.send_message(embed=e, ephemeral=True)


bot.tree.add_command(auto_group)

# Server-Auswahl fuer die Server-Verwaltung (nur noetig ab zwei Servern)
cmd_neustart.autocomplete("server")(_server_autocomplete)
cmd_stoppen.autocomplete("server")(_server_autocomplete)
cmd_status.autocomplete("server")(_server_autocomplete)
auto_restart.autocomplete("server")(_server_autocomplete)
auto_off.autocomplete("server")(_server_autocomplete)
auto_status.autocomplete("server")(_server_autocomplete)




# ══════════════════════════════════════════════════════════════
#  /zone – Überwachte Zonen: wiederholter Ping (alle 5 Min),
#  solange ein Spieler in der Zone steht (außer Allowlist)
#  (Positionen kommen aus den ADM-Logs, Prüfung in _check_zones)
# ══════════════════════════════════════════════════════════════
zone_group = app_commands.Group(name="zone",
                                description="🛡️ Zonen-Pings verwalten (Admin)")

def _zones(conn: Optional[ServerConnection] = None) -> List[Dict]:
    """Die Zonen eines Servers. Ohne Angabe die des Hauptservers."""
    src = conn or connections.primary()
    if src is None:
        zs = cfg.config.get("zones")
        if not isinstance(zs, list):
            zs = []
            cfg.config["zones"] = zs
        return zs
    # Bewusst src.data statt src.get: der Rueckfall auf die globale config
    # wuerde sonst allen Servern DIESELBE Zonenliste geben. Migrierte
    # Verbindungen haben ihre Zonen ohnehin bereits in den eigenen Daten.
    zs = src.data.get("zones")
    if not isinstance(zs, list):
        zs = []
        src.set("zones", zs)
    return zs


def _zones_save(conn: Optional[ServerConnection] = None) -> None:
    """Zonen sichern – beim Hauptserver zusaetzlich in der config.json."""
    src = conn or connections.primary()
    if src is not None:
        _conn_store(src, "zones", _zones(src))
    else:
        cfg.save_config()

def _find_zone(name: str, conn: Optional[ServerConnection] = None) -> Optional[Dict]:
    key = name.strip().lower()
    for z in _zones(conn):
        if isinstance(z, dict) and str(z.get("name", "")).strip().lower() == key:
            return z
    return None

def _zone_allowlist(zone: Dict) -> List[str]:
    """Liefert die Ignorier-Liste einer Zone (legt sie bei Bedarf an)."""
    al = zone.get("allowlist")
    if not isinstance(al, list):
        al = []
        zone["allowlist"] = al
    return al

def _allowlist_aus_anfrage(data: Dict) -> Optional[List[str]]:
    """Die im Zonen-Formular gewählten Spielernamen säubern.

    ``None`` heisst "nicht mitgeschickt, vorhandene Liste behalten". Ohne
    diese Auswertung nahmen ``create_zone``/``update_zone`` das Feld
    stillschweigend nicht an: das Dashboard meldete Erfolg, die Namen waren
    aber nie gespeichert und loesten weiter Zonenalarme aus.
    """
    roh = data.get("allowlist")
    if roh is None:
        return None
    if not isinstance(roh, list):
        return []
    namen: List[str] = []
    gesehen: set = set()
    for n in roh:
        name = str(n).strip()[:64]
        if not name:
            continue
        if name.lower() in gesehen:
            continue
        gesehen.add(name.lower())
        namen.append(name)
        if len(namen) >= 200:
            break
    return namen


def _player_in_allowlist(zone: Dict, pname: str) -> bool:
    """True, wenn der Spieler in dieser Zone ignoriert werden soll (case-insensitiv)."""
    key = (pname or "").strip().lower()
    return any(str(n).strip().lower() == key for n in _zone_allowlist(zone))

def _reset_zone_state(zone_name: str, conn: Optional[ServerConnection] = None):
    """Ping-Cooldowns einer Zone verwerfen (nach remove/edit),
    damit die nächste frische Position sauber neu bewertet wird.

    Mit Verbindung nur die Zone DIESES Servers – sonst setzt eine geloeschte
    Zone „Airfield“ die Cooldowns gleichnamiger Zonen aller Kunden zurueck.
    """
    zk = zone_name.strip().lower()
    sid = conn.service_id if conn is not None else None
    bot._zone_last_ping = {
        k: v for k, v in bot._zone_last_ping.items()
        if not (k[1] == zk and (sid is None or k[0] == sid))}

def _zone_ping_role_ids(zone: Dict) -> List[int]:
    """Rollen, die beim Ping markiert werden – Liste `ping_role_ids`, sonst
    (Altbestand) das frühere einzelne `role_id`."""
    ids = zone.get("ping_role_ids")
    if isinstance(ids, list):
        out = []
        for r in ids:
            try:
                out.append(int(r))
            except (TypeError, ValueError):
                continue
        return out
    legacy = zone.get("role_id")
    return [int(legacy)] if legacy else []


def _zone_payload(z: Dict) -> Dict:
    """Liest eine Zone rückwärtskompatibel: Altbestand ohne `type` gilt als
    `circular`, ein einzelnes altes `role_id` erscheint zusätzlich als Liste
    `ping_role_ids`. Der gespeicherte Eintrag wird dabei NICHT umgeschrieben."""
    out = dict(z)
    out["type"] = z.get("type") or "circular"
    out["ping_role_ids"] = _zone_ping_role_ids(z)
    manage = z.get("manage_role_ids")
    out["manage_role_ids"] = manage if isinstance(manage, list) else []
    out["allowlist"] = _zone_allowlist(z)
    if out["type"] == "polygon":
        pts = z.get("points")
        out["points"] = pts if isinstance(pts, list) else []
    return out


def _ensure_zone_ids(zones: List[Dict]) -> bool:
    """Vergibt fortlaufende `id`-Felder an Zonen, die noch keins haben (reiner
    Anzeigewert, die Ansprache über die API läuft weiter über `name`).
    Gibt True zurück, wenn etwas ergänzt wurde (dann muss gespeichert werden)."""
    changed = False
    next_id = 1 + max([int(z.get("id", 0)) for z in zones if isinstance(z, dict)] or [0])
    for z in zones:
        if isinstance(z, dict) and not z.get("id"):
            z["id"] = next_id
            next_id += 1
            changed = True
    return changed


def _point_in_polygon(px: float, pz: float, points: List[Dict]) -> bool:
    """Ray-Casting-Test: True, wenn (px, pz) innerhalb des Polygons liegt."""
    n = len(points)
    if n < 3:
        return False
    inside = False
    try:
        x1, z1 = float(points[-1]["x"]), float(points[-1]["z"])
        for p in points:
            x2, z2 = float(p["x"]), float(p["z"])
            if (z1 > pz) != (z2 > pz):
                x_schnitt = (x2 - x1) * (pz - z1) / (z2 - z1) + x1
                if px < x_schnitt:
                    inside = not inside
            x1, z1 = x2, z2
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        return False
    return inside


def _validate_zone_points(points: Any) -> Optional[str]:
    """Gibt eine Fehlermeldung zurück oder None, wenn die Polygon-Punkte ok sind."""
    if not isinstance(points, list) or len(points) < 3:
        return "❌ Ein Polygon braucht mindestens 3 Koordinatenpunkte."
    for p in points:
        if not isinstance(p, dict):
            return "❌ Jeder Polygon-Punkt braucht `x` und `z`."
        try:
            px, pz = float(p.get("x")), float(p.get("z"))
        except (TypeError, ValueError):
            return "❌ Jeder Polygon-Punkt braucht Zahlen für `x` und `z`."
        if not (0.0 <= px <= 20000.0 and 0.0 <= pz <= 20000.0):
            return "❌ Ein Polygon-Punkt liegt außerhalb der Map."
    return None


def _zone_summary(z: Dict) -> str:
    roles = _zone_ping_role_ids(z)
    role = f" · Ping: {' '.join(f'<@&{r}>' for r in roles)}" if roles else ""
    chan = f" · Channel: <#{int(z['channel_id'])}>" if z.get("channel_id") else ""
    if z.get("type") == "polygon":
        pts = z.get("points") or []
        return f"Polygon mit {len(pts)} Punkten{role}{chan}"
    return (f"Zentrum `{z.get('x')}, {z.get('z')}` (x=Ost, z=Nord) · "
            f"Radius **{z.get('radius')} m**{role}{chan}")

async def _zone_name_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    conns = _ac_conns(interaction)
    mehrere = len(conns) > 1
    out: List[app_commands.Choice] = []
    for c in conns:
        for z in _zones(c):
            if not (isinstance(z, dict) and z.get("name")):
                continue
            nm = str(z["name"])
            if cur not in nm.lower():
                continue
            out.append(app_commands.Choice(
                name=f"{nm} – {c.name}"[:100] if mehrere else nm, value=nm))
    return out[:25]

def _validate_zone_geometry(x: float, z: float, radius: float) -> Optional[str]:
    """Gibt eine Fehlermeldung zurück oder None, wenn alles ok ist."""
    if not (0.0 <= x <= 20000.0 and 0.0 <= z <= 20000.0):
        return ("❌ Koordinaten außerhalb der Map. Gib die beiden iZurvive-Zahlen "
                "als `x` (Ost) und `z` (Nord) an, z. B. `x: 4522` `z: 9638`.")
    if not (10.0 <= radius <= 10000.0):
        return "❌ Radius muss zwischen **10** und **10000** Metern liegen."
    return None


@zone_group.command(name="list", description="📋 Alle aktiven Zonen anzeigen (Admin)")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def zone_list(interaction: discord.Interaction, server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    _c, _fehler = _conn_waehlen(interaction, server)
    if _c is None:
        return await interaction.response.send_message(_fehler, ephemeral=True)
    zones = [z for z in _zones(_c) if isinstance(z, dict) and z.get("name")]
    if not zones:
        return await interaction.response.send_message(
            "ℹ️ Keine Zonen angelegt. Im Dashboard unter „Zonen“ eine Zone einrichten.",
            ephemeral=True)
    e = discord.Embed(title=f"🛡️ Aktive Zonen ({len(zones)})", color=0x3498DB)
    for z in zones[:25]:
        e.add_field(name=f"📍 {z['name']}", value=_zone_summary(z), inline=False)
    if len(zones) > 25:
        e.set_footer(text=f"… und {len(zones) - 25} weitere (Embed-Limit)")
    await interaction.response.send_message(embed=e, ephemeral=True)


# ── /zone allowlist – Spieler in einer Zone ignorieren (Admin) ──
allowlist_group = app_commands.Group(
    name="allowlist",
    description="🙈 Spieler in einer Zone ignorieren (Admin)",
    parent=zone_group)


@allowlist_group.command(
    name="add",
    description="🙈 Spieler zur Ignorier-Liste einer Zone hinzufügen (Admin)")
@app_commands.describe(
    zone="Name der Zone (Autocomplete)",
    spieler="PlayStation-/Ingame-Name, der nicht mehr gemeldet werden soll",
    server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def zone_allowlist_add(interaction: discord.Interaction, zone: str, spieler: str,
                             server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    _c, _fehler = _conn_waehlen(interaction, server)
    if _c is None:
        return await interaction.response.send_message(_fehler, ephemeral=True)
    z = _find_zone(zone, _c)
    if not z:
        return await interaction.response.send_message(
            f"❌ Keine Zone namens **{zone.strip()}** gefunden – `/zone list` zeigt alle.",
            ephemeral=True)
    spieler = spieler.strip()
    if not spieler:
        return await interaction.response.send_message(
            "❌ Kein Spielername angegeben.", ephemeral=True)
    if _player_in_allowlist(z, spieler):
        return await interaction.response.send_message(
            f"ℹ️ **{spieler}** steht bereits auf der Ignorier-Liste von **{z['name']}**.",
            ephemeral=True)
    _zone_allowlist(z).append(spieler)
    _zones_save(_c)
    await interaction.response.send_message(
        f"🙈 **{spieler}** wird in Zone **{z['name']}** ab sofort **nicht** mehr gemeldet.",
        ephemeral=True)

zone_allowlist_add.autocomplete("zone")(_zone_name_autocomplete)


@allowlist_group.command(
    name="remove",
    description="🔔 Spieler wieder melden – von der Ignorier-Liste entfernen (Admin)")
@app_commands.describe(
    zone="Name der Zone (Autocomplete)",
    spieler="Name, der wieder gemeldet werden soll",
    server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def zone_allowlist_remove(interaction: discord.Interaction, zone: str, spieler: str,
                                server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    _c, _fehler = _conn_waehlen(interaction, server)
    if _c is None:
        return await interaction.response.send_message(_fehler, ephemeral=True)
    z = _find_zone(zone, _c)
    if not z:
        return await interaction.response.send_message(
            f"❌ Keine Zone namens **{zone.strip()}** gefunden – `/zone list` zeigt alle.",
            ephemeral=True)
    key = spieler.strip().lower()
    al = _zone_allowlist(z)
    matches = [n for n in al if str(n).strip().lower() == key]
    if not matches:
        return await interaction.response.send_message(
            f"ℹ️ **{spieler.strip()}** steht nicht auf der Ignorier-Liste von **{z['name']}**.",
            ephemeral=True)
    z["allowlist"] = [n for n in al if str(n).strip().lower() != key]
    _zones_save(_c)
    await interaction.response.send_message(
        f"🔔 **{matches[0]}** wird in Zone **{z['name']}** wieder gemeldet.",
        ephemeral=True)

zone_allowlist_remove.autocomplete("zone")(_zone_name_autocomplete)


@allowlist_group.command(
    name="show",
    description="📋 Ignorierte Spieler einer Zone anzeigen (Admin)")
@app_commands.describe(zone="Name der Zone (Autocomplete)",
                       server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def zone_allowlist_show(interaction: discord.Interaction, zone: str,
                              server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    _c, _fehler = _conn_waehlen(interaction, server)
    if _c is None:
        return await interaction.response.send_message(_fehler, ephemeral=True)
    z = _find_zone(zone, _c)
    if not z:
        return await interaction.response.send_message(
            f"❌ Keine Zone namens **{zone.strip()}** gefunden – `/zone list` zeigt alle.",
            ephemeral=True)
    al = _zone_allowlist(z)
    if not al:
        return await interaction.response.send_message(
            f"ℹ️ Für Zone **{z['name']}** werden aktuell keine Spieler ignoriert.",
            ephemeral=True)
    listing = "\n".join(f"• {n}" for n in al[:50])
    if len(al) > 50:
        listing += f"\n… und {len(al) - 50} weitere"
    e = discord.Embed(
        title=f"🙈 Ignorierte Spieler – {z['name']} ({len(al)})",
        description=listing,
        color=0x95A5A6)
    await interaction.response.send_message(embed=e, ephemeral=True)

zone_allowlist_show.autocomplete("zone")(_zone_name_autocomplete)

zone_list.autocomplete("server")(_server_autocomplete)
zone_allowlist_add.autocomplete("server")(_server_autocomplete)
zone_allowlist_remove.autocomplete("server")(_server_autocomplete)
zone_allowlist_show.autocomplete("server")(_server_autocomplete)


bot.tree.add_command(zone_group)


# ══════════════════════════════════════════════════════════════
#  Ban-Hilfsfunktionen (Banliste in den Nitrado-Servereinstellungen –
#  dasselbe Settings-Feld wie im Webinterface, 1 Name pro Zeile)
# ══════════════════════════════════════════════════════════════
def _find_ban_setting(conn: ServerConnection, settings: Dict) -> Tuple[str, str, str]:
    """Sucht das Banlisten-Setting in den Nitrado-Settings.
    Reihenfolge: Config-Override (nitrado_ban_category/nitrado_ban_key) →
    Auto-Erkennung (Key 'bans', Kategorie egal) → Fallback ('general', 'bans').
    Gibt (category, key, aktueller_wert) zurück."""
    ov_cat = str(conn.get("nitrado_ban_category") or "").strip()
    ov_key = str(conn.get("nitrado_ban_key") or "").strip()
    if ov_cat and ov_key:
        val = ((settings.get(ov_cat) or {}).get(ov_key)
               if isinstance(settings.get(ov_cat), dict) else None)
        return ov_cat, ov_key, str(val or "")
    for category, keys in settings.items():
        if not isinstance(keys, dict):
            continue
        for key, val in keys.items():
            if str(key).lower() == "bans":
                return str(category), str(key), str(val or "")
    return "general", "bans", ""

async def _read_banlist(conn: ServerConnection) -> Tuple[List[str], str, str]:
    """Liest die Banliste aus den Nitrado-Servereinstellungen.
    Gibt (namen, category, key) zurück. Wirft RuntimeError bei API-Fehler –
    Aufrufer dürfen dann NICHT schreiben (sonst würde die Liste überschrieben)."""
    settings = await conn.api.get_settings()
    if settings is None:
        raise RuntimeError("Nitrado-API nicht erreichbar (Settings konnten nicht gelesen werden)")
    category, key, raw = _find_ban_setting(conn, settings)
    names = [l.strip() for l in raw.splitlines() if l.strip()]
    return names, category, key

async def _write_banlist(conn: ServerConnection, names: List[str],
                         category: str, key: str) -> Tuple[bool, str]:
    """Schreibt die Banliste in die Nitrado-Servereinstellungen (1 Name pro Zeile)."""
    return await conn.api.set_setting(category, key, "\r\n".join(names))

def _split_names(raw: str) -> List[str]:
    """Zerlegt die Eingabe in einzelne Namen (Komma-getrennt), Duplikate raus."""
    out: List[str] = []
    seen: set = set()
    for part in raw.split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


# ══════════════════════════════════════════════════════════════
#  /ban – Spieler bannen
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="ban",
                  description="🔨 Fügt Spieler zur Banliste in den Nitrado-Servereinstellungen hinzu")
@app_commands.describe(
    spieler="Name(n) – mehrere per Komma getrennt",
    grund="Grund für den Ban (optional)",
    server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)"
)
async def cmd_ban(interaction: discord.Interaction, spieler: str,
                  grund: str = "Kein Grund angegeben", server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, server=server)
    if conn is None:
        return
    await interaction.response.defer()

    names = _split_names(spieler)
    if not names:
        return await interaction.followup.send("❌ Keinen gültigen Namen angegeben.")

    # Erst lesen – bei API-Fehler NICHT schreiben, sonst würde die
    # bestehende Nitrado-Banliste überschrieben/geleert
    try:
        current, category, key = await _read_banlist(conn)
    except Exception as e:
        return await interaction.followup.send(
            f"❌ Nitrado-Banliste konnte nicht gelesen werden – nichts geändert.\n`{e}`")

    existing_lower = {n.lower() for n in current}
    added   = [n for n in names if n.lower() not in existing_lower]
    already = [n for n in names if n.lower() in existing_lower]

    sv = "ℹ️ Alle Namen standen bereits auf der Banliste"
    if added:
        ok, msg = await _write_banlist(conn, current + added, category, key)
        if not ok:
            return await interaction.followup.send(
                f"❌ Nitrado-Banliste konnte nicht gespeichert werden – nichts geändert.\n`{msg}`")
        sv = "✅ In der Nitrado-Banliste gespeichert"

    # Lokale Metadaten (nur für die Anzeige in /banlist)
    now = datetime.now(timezone.utc).isoformat()
    _eimer = _bans_of(conn)
    for n in added:
        _eimer[n] = {"name": n, "reason": grund,
                     "banned_by": str(interaction.user), "banned_at": now}
    if added:
        cfg.save_bans()

    embed = discord.Embed(title="🔨 Spieler gebannt", color=0xE74C3C)
    embed.add_field(name="Hinzugefügt",
                    value="\n".join(f"`{n}`" for n in added) or "–", inline=True)
    if already:
        embed.add_field(name="Bereits gebannt",
                        value="\n".join(f"`{n}`" for n in already), inline=True)
    embed.add_field(name="Grund",       value=grund,                 inline=True)
    embed.add_field(name="Gebannt von", value=str(interaction.user), inline=True)
    embed.add_field(name="Nitrado",     value=sv,                    inline=False)
    embed.set_footer(text="Änderung greift ggf. erst nach einem Server-Neustart.")
    await interaction.followup.send(embed=embed)


# ══════════════════════════════════════════════════════════════
#  /ban_entfernen – Ban aufheben
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name=app_commands.locale_str("ban_entfernen"),
                  description="✅ Entfernt Spieler von der Banliste in den Nitrado-Servereinstellungen")
@app_commands.describe(spieler="Name(n) – mehrere per Komma getrennt",
                       server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_unban(interaction: discord.Interaction, spieler: str,
                    server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, server=server)
    if conn is None:
        return
    await interaction.response.defer()

    names = _split_names(spieler)
    if not names:
        return await interaction.followup.send("❌ Keinen gültigen Namen angegeben.")

    try:
        current, category, key = await _read_banlist(conn)
    except Exception as e:
        return await interaction.followup.send(
            f"❌ Nitrado-Banliste konnte nicht gelesen werden – nichts geändert.\n`{e}`")

    wanted_lower = {n.lower() for n in names}
    new_list  = [n for n in current if n.lower() not in wanted_lower]
    removed   = [n for n in current if n.lower() in wanted_lower]
    not_found = [n for n in names if n.lower() not in {r.lower() for r in removed}]

    sv = "ℹ️ Keiner der Namen stand auf der Banliste"
    if removed:
        ok, msg = await _write_banlist(conn, new_list, category, key)
        if not ok:
            return await interaction.followup.send(
                f"❌ Nitrado-Banliste konnte nicht gespeichert werden – nichts geändert.\n`{msg}`")
        sv = "✅ Von der Nitrado-Banliste entfernt"
        # Lokale Metadaten aufräumen (case-insensitive)
        _eimer = _bans_of(conn)
        for local_key in [k for k in _eimer if k.lower() in wanted_lower]:
            _eimer.pop(local_key, None)
        cfg.save_bans()

    embed = discord.Embed(title="✅ Ban aufgehoben", color=0x2ECC71)
    embed.add_field(name="Entfernt",
                    value="\n".join(f"`{n}`" for n in removed) or "–", inline=True)
    if not_found:
        embed.add_field(name="Nicht auf der Liste",
                        value="\n".join(f"`{n}`" for n in not_found), inline=True)
    embed.add_field(name="Nitrado", value=sv, inline=False)
    embed.set_footer(text="Änderung greift ggf. erst nach einem Server-Neustart.")
    await interaction.followup.send(embed=embed)


# ══════════════════════════════════════════════════════════════
#  /banlist – Alle gesperrten Spieler
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="banlist",
                  description="📋 Zeigt die Banliste aus den Nitrado-Servereinstellungen")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_banlist(interaction: discord.Interaction, server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, server=server)
    if conn is None:
        return
    await interaction.response.defer(ephemeral=True)

    try:
        all_bans, _category, _key = await _read_banlist(conn)
    except Exception as e:
        return await interaction.followup.send(
            f"❌ Nitrado-Banliste konnte nicht gelesen werden.\n`{e}`", ephemeral=True)

    if not all_bans:
        return await interaction.followup.send("✅ Keine gesperrten Spieler.", ephemeral=True)

    embed = discord.Embed(
        title=f"🚫 Banliste – {len(all_bans)} Spieler gesperrt",
        color=0xE74C3C
    )
    # Metadaten (Grund/Datum/von) kommen aus der lokalen banlist.json, falls
    # der Ban über /ban gesetzt wurde – Einträge direkt aus dem Nitrado-
    # Webinterface haben keine Metadaten (case-insensitives Matching)
    local = {k.lower(): v for k, v in _bans_of(conn).items()}
    lines = []
    for entry in sorted(all_bans, key=str.lower):
        info = local.get(entry.lower())
        if info:
            grund = info.get("reason", "–")
            datum = (info.get("banned_at", "")[:10]) if info.get("banned_at") else "–"
            von   = info.get("banned_by", "–")
            lines.append(f"• `{entry}` — {grund} | {datum} | von {von}")
        else:
            lines.append(f"• `{entry}`")

    # Aufteilen bei > 1000 Zeichen
    chunks, chunk = [], []
    for line in lines:
        if len("\n".join(chunk + [line])) > 1000:
            chunks.append("\n".join(chunk))
            chunk = [line]
        else:
            chunk.append(line)
    if chunk:
        chunks.append("\n".join(chunk))

    for i, c in enumerate(chunks[:25]):
        embed.add_field(name=f"Spieler {i+1}" if len(chunks) > 1 else "Spieler",
                        value=c, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════
#  Whitelist – Hilfsfunktionen (Whitelist in den Nitrado-
#  Servereinstellungen, 1 Name pro Zeile – analog zur Banliste)
# ══════════════════════════════════════════════════════════════
def _find_whitelist_setting(conn: ServerConnection, settings: Dict) -> Tuple[str, str, str]:
    """Sucht das Whitelist-Setting in den Nitrado-Settings.
    Reihenfolge: Config-Override (nitrado_whitelist_category/-key) →
    Auto-Erkennung (Key 'whitelist') → Fallback ('general', 'whitelist').
    Gibt (category, key, aktueller_wert) zurück."""
    ov_cat = str(conn.get("nitrado_whitelist_category") or "").strip()
    ov_key = str(conn.get("nitrado_whitelist_key") or "").strip()
    if ov_cat and ov_key:
        val = ((settings.get(ov_cat) or {}).get(ov_key)
               if isinstance(settings.get(ov_cat), dict) else None)
        return ov_cat, ov_key, str(val or "")
    for category, keys in settings.items():
        if not isinstance(keys, dict):
            continue
        for key, val in keys.items():
            if str(key).lower() == "whitelist":
                return str(category), str(key), str(val or "")
    return "general", "whitelist", ""

async def _read_whitelist(conn: ServerConnection) -> Tuple[List[str], str, str]:
    """Liest die Whitelist aus den Nitrado-Servereinstellungen.
    Gibt (namen, category, key) zurück. Wirft RuntimeError bei API-Fehler –
    Aufrufer dürfen dann NICHT schreiben (sonst würde die Liste überschrieben)."""
    settings = await conn.api.get_settings()
    if settings is None:
        raise RuntimeError("Nitrado-API nicht erreichbar (Settings konnten nicht gelesen werden)")
    category, key, raw = _find_whitelist_setting(conn, settings)
    names = [l.strip() for l in raw.splitlines() if l.strip()]
    return names, category, key

async def _write_whitelist(conn: ServerConnection, names: List[str],
                           category: str, key: str) -> Tuple[bool, str]:
    """Schreibt die Whitelist in die Nitrado-Servereinstellungen (1 Name pro Zeile)."""
    return await conn.api.set_setting(category, key, "\r\n".join(names))


# ══════════════════════════════════════════════════════════════
#  /whitelist add|remove|show – Whitelist verwalten (Admin)
# ══════════════════════════════════════════════════════════════
whitelist_group = app_commands.Group(
    name="whitelist",
    description="✅ Whitelist in den Nitrado-Servereinstellungen verwalten (Admin)")


@whitelist_group.command(
    name="add",
    description="✅ Spieler zur Whitelist hinzufügen (mehrere per Komma/Zeile)")
@app_commands.describe(spieler="PlayStation-Name(n) – mehrere per Komma getrennt",
                       server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def whitelist_add(interaction: discord.Interaction, spieler: str,
                        server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, server=server)
    if conn is None:
        return
    await interaction.response.defer()

    names = _split_names(spieler.replace("\n", ","))
    if not names:
        return await interaction.followup.send("❌ Keinen gültigen Namen angegeben.")

    try:
        current, category, key = await _read_whitelist(conn)
    except Exception as e:
        return await interaction.followup.send(
            f"❌ Nitrado-Whitelist konnte nicht gelesen werden – nichts geändert.\n`{e}`")

    existing_lower = {n.lower() for n in current}
    added   = [n for n in names if n.lower() not in existing_lower]
    already = [n for n in names if n.lower() in existing_lower]

    sv = "ℹ️ Alle Namen standen bereits auf der Whitelist"
    if added:
        ok, msg = await _write_whitelist(conn, current + added, category, key)
        if not ok:
            return await interaction.followup.send(
                f"❌ Nitrado-Whitelist konnte nicht gespeichert werden – nichts geändert.\n`{msg}`")
        sv = "✅ In der Nitrado-Whitelist gespeichert"

    embed = discord.Embed(title="✅ Whitelist aktualisiert", color=0x2ECC71)
    embed.add_field(name="Hinzugefügt",
                    value="\n".join(f"`{n}`" for n in added) or "–", inline=True)
    if already:
        embed.add_field(name="Bereits auf der Whitelist",
                        value="\n".join(f"`{n}`" for n in already), inline=True)
    embed.add_field(name="Hinzugefügt von", value=str(interaction.user), inline=True)
    embed.add_field(name="Nitrado",         value=sv,                    inline=False)
    embed.set_footer(text="Änderung greift ggf. erst nach einem Server-Neustart.")
    await interaction.followup.send(embed=embed)


@whitelist_group.command(
    name="remove",
    description="🗑️ Spieler von der Whitelist entfernen (mehrere per Komma/Zeile)")
@app_commands.describe(spieler="PlayStation-Name(n) – mehrere per Komma getrennt",
                       server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def whitelist_remove(interaction: discord.Interaction, spieler: str,
                           server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, server=server)
    if conn is None:
        return
    await interaction.response.defer()

    names = _split_names(spieler.replace("\n", ","))
    if not names:
        return await interaction.followup.send("❌ Keinen gültigen Namen angegeben.")

    try:
        current, category, key = await _read_whitelist(conn)
    except Exception as e:
        return await interaction.followup.send(
            f"❌ Nitrado-Whitelist konnte nicht gelesen werden – nichts geändert.\n`{e}`")

    wanted_lower = {n.lower() for n in names}
    new_list  = [n for n in current if n.lower() not in wanted_lower]
    removed   = [n for n in current if n.lower() in wanted_lower]
    not_found = [n for n in names if n.lower() not in {r.lower() for r in removed}]

    sv = "ℹ️ Keiner der Namen stand auf der Whitelist"
    if removed:
        ok, msg = await _write_whitelist(conn, new_list, category, key)
        if not ok:
            return await interaction.followup.send(
                f"❌ Nitrado-Whitelist konnte nicht gespeichert werden – nichts geändert.\n`{msg}`")
        sv = "✅ Von der Nitrado-Whitelist entfernt"

    embed = discord.Embed(title="🗑️ Whitelist aktualisiert", color=0xE67E22)
    embed.add_field(name="Entfernt",
                    value="\n".join(f"`{n}`" for n in removed) or "–", inline=True)
    if not_found:
        embed.add_field(name="Nicht auf der Liste",
                        value="\n".join(f"`{n}`" for n in not_found), inline=True)
    embed.add_field(name="Nitrado", value=sv, inline=False)
    embed.set_footer(text="Änderung greift ggf. erst nach einem Server-Neustart.")
    await interaction.followup.send(embed=embed)


@whitelist_group.command(
    name="show",
    description="📋 Zeigt die aktuellen Spieler auf der Whitelist (Admin)")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def whitelist_show(interaction: discord.Interaction, server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, server=server)
    if conn is None:
        return
    await interaction.response.defer(ephemeral=True)

    try:
        names, _category, _key = await _read_whitelist(conn)
    except Exception as e:
        return await interaction.followup.send(
            f"❌ Nitrado-Whitelist konnte nicht gelesen werden.\n`{e}`", ephemeral=True)

    if not names:
        return await interaction.followup.send(
            "ℹ️ Es stehen keine Spieler auf der Whitelist.", ephemeral=True)

    embed = discord.Embed(
        title=f"✅ Whitelist – {len(names)} Spieler",
        color=0x2ECC71)
    lines = [f"• `{n}`" for n in sorted(names, key=str.lower)]
    chunks, chunk = [], []
    for line in lines:
        if len("\n".join(chunk + [line])) > 1000:
            chunks.append("\n".join(chunk))
            chunk = [line]
        else:
            chunk.append(line)
    if chunk:
        chunks.append("\n".join(chunk))
    for i, c in enumerate(chunks[:25]):
        embed.add_field(name=f"Spieler {i+1}" if len(chunks) > 1 else "Spieler",
                        value=c, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


bot.tree.add_command(whitelist_group)


# ══════════════════════════════════════════════════════════════
#  Whitelist-Anfrage-Panel (Spieler reichen ihren PSN-Namen ein,
#  Admins geben per Button frei/ab). Persistente Views (timeout=None
#  + feste custom_ids) → überleben einen Bot-Neustart.
# ══════════════════════════════════════════════════════════════
WHITELIST_PANEL_TEXT = ("Klick auf den Button und trage deinen PlayStation Namen ein "
                        "um zur whitelist hinzugefügt werden zu können")


def _whitelist_request_embed(requester_id: int, psn: str) -> discord.Embed:
    embed = discord.Embed(
        title="🎮 Neue Whitelist-Anfrage",
        description="Ein Admin muss diese Anfrage prüfen.",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Angefragt von", value=f"<@{requester_id}>", inline=True)
    embed.add_field(name="PlayStation-Name", value=f"`{psn}`", inline=True)
    return embed


class WhitelistRequestModal(discord.ui.Modal, title="🎮 PSN Name eintragen"):
    """Formular, in das der Spieler seinen PlayStation-Namen einträgt."""

    def __init__(self, service_id: Optional[str] = None):
        super().__init__()
        self.service_id = str(service_id or "")
        self.psn_in = discord.ui.TextInput(
            label="Dein PlayStation Name",
            placeholder="z.B. DeinPSNName",
            required=True, max_length=32)
        self.add_item(self.psn_in)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.psn_in.value or "")
        psn = (raw.splitlines()[0].strip() if raw.strip() else "")
        if not psn:
            return await interaction.response.send_message(
                "❌ Kein Name eingegeben.", ephemeral=True)
        if "," in psn:
            return await interaction.response.send_message(
                "❌ Bitte nur **einen** Namen eintragen (ohne Komma).", ephemeral=True)

        gid = interaction.guild_id
        # Der Server steckt im Panel-Knopf; fehlt er (Alt-Panel), gilt der
        # einzige Server der Guild – bei mehreren bricht die Anfrage ab.
        sid = self.service_id
        if not sid:
            eigene = connections.all_for_guild(gid)
            if len(eigene) == 1:
                sid = eigene[0].service_id
            elif len(eigene) > 1:
                return await interaction.response.send_message(
                    "❌ Dieser Discord-Server verwaltet mehrere Nitrado-Server. "
                    "Ein Admin muss das Whitelist-Panel mit `/send whitelist panel` "
                    "neu senden, damit klar ist, für welchen Server es gilt.",
                    ephemeral=True)
        admin_ch_id = cfg.get_channel(gid, "whitelist_request", sid or None)
        if not admin_ch_id:
            return await interaction.response.send_message(
                "❌ Das Whitelist-System ist noch nicht eingerichtet. "
                "Bitte wende dich an einen Admin.", ephemeral=True)
        admin_ch = bot.get_channel(int(admin_ch_id))
        if admin_ch is None:
            return await interaction.response.send_message(
                "❌ Der Anfrage-Channel wurde nicht gefunden. "
                "Bitte wende dich an einen Admin.", ephemeral=True)

        # Doppelte Anfrage für denselben PSN-Namen abwehren
        for r in cfg.whitelist_reqs.values():
            if (str(r.get("guild_id")) == str(gid)
                    and str(r.get("psn", "")).lower() == psn.lower()):
                return await interaction.response.send_message(
                    f"ℹ️ Für **{psn}** läuft bereits eine Anfrage. "
                    "Bitte warte auf die Freigabe.", ephemeral=True)

        reqid = uuid.uuid4().hex[:12]
        req = {
            "requester_id":     interaction.user.id,
            "requester_name":   str(interaction.user),
            "psn":              psn,
            "guild_id":         gid,
            "service_id":       sid,
            "admin_channel_id": int(admin_ch_id),
            "message_id":       None,
            "created_at":       datetime.now(timezone.utc).isoformat(),
        }
        try:
            msg = await admin_ch.send(
                embed=_whitelist_request_embed(interaction.user.id, psn),
                view=WhitelistApprovalView(reqid))
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ Der Bot darf im Anfrage-Channel nicht schreiben. "
                "Bitte informiere einen Admin.", ephemeral=True)
        req["message_id"] = msg.id
        cfg.whitelist_reqs[reqid] = req
        cfg.save_whitelist_reqs()

        await interaction.response.send_message(
            f"✅ Deine Anfrage für den PSN-Namen **{psn}** wurde eingereicht. "
            "Ein Admin prüft sie in Kürze.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error(f"[WHITELIST] Anfrage-Modal-Fehler: {error}")
        msg = "❌ Etwas ist schiefgelaufen. Bitte versuche es erneut."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


def _whitelist_conn(req: Dict[str, Any],
                    interaction: discord.Interaction) -> Optional[ServerConnection]:
    """Der Nitrado-Server, zu dem eine Whitelist-Anfrage gehoert.

    Bevorzugt die in der Anfrage vermerkte Service-ID. Alt-Anfragen ohne das
    Feld fallen auf den einzigen Server ihrer Guild zurueck; gibt es dort
    mehrere, ist die Zuordnung nicht mehr rekonstruierbar und die Freigabe
    wird abgelehnt, statt auf gut Glueck den falschen Server zu treffen.
    """
    sid = str((req or {}).get("service_id") or "")
    if sid:
        conn = connections.for_service(sid)
        # Panels und offene Anfragen ueberdauern eine Neuzuordnung. Gehoert der
        # Server inzwischen einem anderen Discord-Server, darf ein Admin aus
        # dem alten hier nicht weiter dessen Nitrado-Whitelist aendern.
        if conn is not None and conn.guild_id is not None and interaction.guild_id \
                and int(conn.guild_id) != int(interaction.guild_id):
            log.warning(f"[WHITELIST] Panel in Guild {interaction.guild_id} zeigt auf "
                        f"{conn.name}, der inzwischen zu Guild {conn.guild_id} gehört – "
                        f"abgelehnt.")
            return None
        return conn
    eigene = connections.all_for_guild((req or {}).get("guild_id")
                                       or interaction.guild_id)
    return eigene[0] if len(eigene) == 1 else None


class WhitelistPanelView(discord.ui.View):
    """Persistentes Panel mit dem Button, der das PSN-Eingabe-Modal öffnet.

    Die ``custom_id`` traegt die Service-ID, damit eine Anfrage auch nach einem
    Bot-Neustart noch weiss, fuer WELCHEN Nitrado-Server sie gilt. Panels aus
    der Zeit davor haben keine Endung und werden weiter bedient.
    """

    def __init__(self, service_id: Optional[str] = None):
        super().__init__(timeout=None)
        self.service_id = str(service_id or "")
        cid = f"wl_panel_open:{self.service_id}" if self.service_id else "wl_panel_open"
        knopf = discord.ui.Button(label="PSN Name eintragen", emoji="🎮",
                                  style=discord.ButtonStyle.primary, custom_id=cid)
        knopf.callback = self._open_modal
        self.add_item(knopf)

    async def _open_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            WhitelistRequestModal(self.service_id or None))


class WhitelistApprovalView(discord.ui.View):
    """Persistente Freigabe-Buttons (Akzeptieren/Ablehnen) für eine Anfrage.
    custom_ids tragen die reqid, damit sie einen Neustart überleben."""

    def __init__(self, reqid: str):
        super().__init__(timeout=None)
        self.reqid = reqid
        approve = discord.ui.Button(
            label="Akzeptieren", emoji="✅",
            style=discord.ButtonStyle.success, custom_id=f"wl_approve:{reqid}")
        reject = discord.ui.Button(
            label="Ablehnen", emoji="❌",
            style=discord.ButtonStyle.danger, custom_id=f"wl_reject:{reqid}")
        approve.callback = self._approve
        reject.callback  = self._reject
        self.add_item(approve)
        self.add_item(reject)

    async def _approve(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await _deny(interaction)
        req = cfg.whitelist_reqs.pop(self.reqid, None)
        cfg.save_whitelist_reqs()
        if not req:
            return await interaction.response.send_message(
                "ℹ️ Diese Anfrage wurde bereits bearbeitet.", ephemeral=True)

        # Der Server steht in der Anfrage – die Buttons ueberleben Neustarts,
        # und bei mehreren Servern derselben Guild waere _conn_of geraten.
        conn = _whitelist_conn(req, interaction)
        if conn is None or conn.api is None:
            cfg.whitelist_reqs[self.reqid] = req
            cfg.save_whitelist_reqs()
            return await interaction.response.send_message(
                "❌ Für diese Anfrage ist kein Nitrado-Server eingerichtet – "
                "sie bleibt offen. Ein Admin kann das Whitelist-Panel mit "
                "`/send whitelist panel` neu senden.", ephemeral=True)

        await interaction.response.defer()
        try:
            current, category, key = await _read_whitelist(conn)
        except Exception as e:
            cfg.whitelist_reqs[self.reqid] = req
            cfg.save_whitelist_reqs()
            return await interaction.followup.send(
                f"❌ Whitelist konnte nicht gelesen werden – nichts geändert. "
                f"Anfrage bleibt offen.\n`{e}`", ephemeral=True)

        psn = req["psn"]
        if psn.lower() not in {n.lower() for n in current}:
            ok, msg = await _write_whitelist(conn, current + [psn], category, key)
            if not ok:
                cfg.whitelist_reqs[self.reqid] = req
                cfg.save_whitelist_reqs()
                return await interaction.followup.send(
                    f"❌ Whitelist konnte nicht gespeichert werden – nichts geändert. "
                    f"Anfrage bleibt offen.\n`{msg}`", ephemeral=True)
            nitrado_note = "✅ Zur Nitrado-Whitelist hinzugefügt"
        else:
            nitrado_note = "ℹ️ Stand bereits auf der Whitelist"

        embed = discord.Embed(
            title="✅ Whitelist-Anfrage angenommen",
            color=0x2ECC71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Spieler", value=f"<@{req['requester_id']}>", inline=True)
        embed.add_field(name="PlayStation-Name", value=f"`{psn}`", inline=True)
        embed.add_field(name="Status", value=nitrado_note, inline=False)
        embed.add_field(name="Bearbeitet von",
                        value=interaction.user.mention, inline=False)
        embed.set_footer(text="Änderung greift ggf. erst nach einem Server-Neustart.")
        await interaction.edit_original_response(embed=embed, view=None)

    async def _reject(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await _deny(interaction)
        req = cfg.whitelist_reqs.pop(self.reqid, None)
        cfg.save_whitelist_reqs()
        if not req:
            return await interaction.response.send_message(
                "ℹ️ Diese Anfrage wurde bereits bearbeitet.", ephemeral=True)

        embed = discord.Embed(
            title="❌ Whitelist-Anfrage abgelehnt",
            color=0xE74C3C, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Spieler", value=f"<@{req['requester_id']}>", inline=True)
        embed.add_field(name="PlayStation-Name", value=f"`{req['psn']}`", inline=True)
        embed.add_field(name="Status",
                        value="❌ Nicht zur Whitelist hinzugefügt", inline=False)
        embed.add_field(name="Bearbeitet von",
                        value=interaction.user.mention, inline=False)
        await interaction.response.edit_message(embed=embed, view=None)


# ── /send whitelist panel – Panel in einen Channel senden (Admin) ──
send_group = app_commands.Group(name="send", description="📨 Panels/Embeds senden (Admin)")
send_whitelist_group = app_commands.Group(
    name="whitelist", description="✅ Whitelist-Panel senden", parent=send_group)


@send_whitelist_group.command(
    name="panel",
    description="📩 Whitelist-Anfrage-Panel in einen Channel senden (Admin)")
@app_commands.describe(
    panel_channel="Channel, in dem das Panel für die Spieler erscheint",
    admin_channel="Staff-Channel, in dem die Anfragen zur Freigabe landen",
    server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def send_whitelist_panel(interaction: discord.Interaction,
                               panel_channel: discord.TextChannel,
                               admin_channel: discord.TextChannel,
                               server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)

    _conn, _fehler = _conn_waehlen(interaction, server)
    if _conn is None:
        return await interaction.response.send_message(
            _fehler or PREMIUM_MISSING_TEXT, ephemeral=True)
    # Anfrage-Channel je Server merken (das Modal liest ihn beim Absenden aus)
    cfg.set_channel(interaction.guild_id, "whitelist_request", admin_channel.id,
                    service_id=_conn.service_id)

    panel_embed = discord.Embed(
        title="✅ Whitelist-Anmeldung",
        description=WHITELIST_PANEL_TEXT,
        color=0x5865F2)
    try:
        await panel_channel.send(embed=panel_embed,
                                 view=WhitelistPanelView(_conn.service_id))
    except discord.Forbidden:
        return await interaction.response.send_message(
            f"❌ Ich darf in {panel_channel.mention} nicht schreiben. "
            "Bitte Kanal-Rechte prüfen.", ephemeral=True)

    await interaction.response.send_message(
        f"✅ Whitelist-Panel in {panel_channel.mention} gesendet.\n"
        f"Anfragen zur Freigabe erscheinen in {admin_channel.mention}.",
        ephemeral=True)


bot.tree.add_command(send_group)


# ══════════════════════════════════════════════════════════════
#  /admin_position – Letzte bekannte Spieler-Positionen
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="admin_position",
                  description="📍 Letzte bekannte Positionen aller Spieler aus den Logs")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_positions(interaction: discord.Interaction, server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)

    _conn = await _require_conn(interaction, server=server)
    if _conn is None:
        return
    positions = (_conn.parser.player_positions if _conn.parser else {})
    if not positions:
        return await interaction.response.send_message(
            "⚠️ Noch keine Positions-Daten verfügbar.\n"
            "Positionen werden aus Kill/Death-Events automatisch gesammelt. "
            "Warte bis der erste Log-Zyklus gelaufen ist.",
            ephemeral=True
        )

    embed = discord.Embed(
        title=f"📍 Spieler-Positionen ({len(positions)} bekannt)",
        description="Letzte bekannte Koordinaten aus Server-Logs (nicht live)",
        color=0x3498DB
    )

    lines = []
    for name, data in sorted(positions.items()):
        ts = data.get("last_seen", "")
        ts_fmt = ts[:16].replace("T", " ") if ts else "?"
        lines.append(f"**{name}** → `{data['position']}` *(zuletzt: {ts_fmt} UTC)*")

    chunk, fc = [], 0
    for line in lines:
        if len("\n".join(chunk + [line])) > 1000:
            embed.add_field(name="Spieler", value="\n".join(chunk), inline=False)
            chunk = [line]
            fc += 1
            if fc >= 24:
                chunk.append(f"... und {len(lines)-fc*10} weitere")
                break
        else:
            chunk.append(line)
    if chunk:
        embed.add_field(name="Spieler", value="\n".join(chunk), inline=False)

    embed.set_footer(text="⚠️ Positionen stammen aus Log-Events – nicht live in Echtzeit")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════
#  /spieler_suche – Spieler in Logs suchen
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name=app_commands.locale_str("spieler_suche"),
                  description="🔍 Sucht einen Spieler in den aktuellen Logs")
@app_commands.describe(name="Ingame-Name oder Steam64-ID",
                       server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_search(interaction: discord.Interaction, name: str,
                     server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, need_ftp=True, server=server)
    if conn is None:
        return
    await interaction.response.defer(ephemeral=True)

    log_dir = conn.get("ftp_log_dir")
    if not log_dir:
        return await interaction.followup.send(
            "❌ Log-Verzeichnis nicht konfiguriert. Starte den Bot neu oder nutze `/ftp_scan`.",
            ephemeral=True
        )

    loop = asyncio.get_running_loop()
    adm_files = await loop.run_in_executor(None, conn.ftp.list_adm_files, log_dir)
    if not adm_files:
        return await interaction.followup.send("❌ Keine Log-Dateien gefunden.", ephemeral=True)

    content = await loop.run_in_executor(None, conn.ftp.read_file, adm_files[-1])
    if not content:
        return await interaction.followup.send("❌ Log-Datei konnte nicht gelesen werden.", ephemeral=True)

    hits = [l.strip() for l in content.splitlines() if name.lower() in l.lower()][:25]
    if not hits:
        return await interaction.followup.send(f"❌ Keine Einträge für **{name}** gefunden.", ephemeral=True)

    result = "\n".join(f"`{h[:120]}`" for h in hits)
    if len(result) > 3900:
        result = result[:3900] + "\n..."

    embed = discord.Embed(title=f"🔍 Suche: {name}", description=result, color=0x5865F2)
    embed.set_footer(text=f"Datei: {adm_files[-1]} | {len(hits)} Treffer (max. 25)")
    await interaction.followup.send(embed=embed, ephemeral=True)


cmd_ban.autocomplete("server")(_server_autocomplete)
cmd_unban.autocomplete("server")(_server_autocomplete)
cmd_banlist.autocomplete("server")(_server_autocomplete)
whitelist_add.autocomplete("server")(_server_autocomplete)
whitelist_remove.autocomplete("server")(_server_autocomplete)
whitelist_show.autocomplete("server")(_server_autocomplete)
send_whitelist_panel.autocomplete("server")(_server_autocomplete)
cmd_positions.autocomplete("server")(_server_autocomplete)
cmd_search.autocomplete("server")(_server_autocomplete)


# ══════════════════════════════════════════════════════════════
#  /ftp_scan – FTP-Verzeichnisse neu scannen
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="ftp_scan", description="🔎 Scannt FTP-Server erneut nach Log-Verzeichnissen")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_ftp_scan(interaction: discord.Interaction, server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, need_ftp=True, server=server)
    if conn is None:
        return
    await interaction.response.defer(ephemeral=True)

    # Pfade zurücksetzen damit discover_paths nicht überspringt
    for key in ("ftp_log_dir", "ftp_ban_file", "ftp_mission_dir",
                "cfg_effect_area_path"):
        _conn_store(conn, key, "")
    conn.data["log_state"] = {}
    connections.save()
    if connections.primary() is conn:
        cfg.log_state = {}
        cfg.save_log_state()

    await bot._auto_discover(conn)

    log_dir  = conn.get("ftp_log_dir")          or "Nicht gefunden"
    ban_file = conn.get("ftp_ban_file")         or "Nicht gefunden"
    mission  = conn.get("ftp_mission_dir")      or "Nicht gefunden"
    effect   = conn.get("cfg_effect_area_path") or "Nicht gefunden"

    embed = discord.Embed(title="🔎 FTP-Scan abgeschlossen", color=0x2ECC71)
    embed.add_field(name="Log-Verzeichnis", value=f"`{log_dir}`",  inline=False)
    embed.add_field(name="Ban-Datei",       value=f"`{ban_file}`", inline=False)
    embed.add_field(name="Mission-Ordner",  value=f"`{mission}`",  inline=False)
    embed.add_field(name="cfgEffectArea",   value=f"`{effect}`",   inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════
#  /raw_log – Letzte N Zeilen des ADM-Logs anzeigen (Debug)
#  Hilft herauszufinden warum manche Events (damage, loot)
#  nicht gepostet werden – zeigt das exakte Log-Format.
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="raw_log", description="🔍 Zeigt die letzten Zeilen des ADM-Logs (Debug)")
@app_commands.describe(zeilen="Anzahl der Zeilen (Standard: 20, max. 40)",
                       server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_raw_log(interaction: discord.Interaction, zeilen: int = 20,
                      server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, need_ftp=True, server=server)
    if conn is None:
        return
    await interaction.response.defer(ephemeral=True)

    log_dir = conn.get("ftp_log_dir")
    if not log_dir:
        return await interaction.followup.send(
            "❌ Log-Verzeichnis nicht konfiguriert. Nutze `/ftp_scan`.", ephemeral=True
        )

    loop = asyncio.get_running_loop()
    adm_files = await loop.run_in_executor(None, conn.ftp.list_adm_files, log_dir)
    if not adm_files:
        return await interaction.followup.send("❌ Keine ADM-Dateien gefunden.", ephemeral=True)

    content = await loop.run_in_executor(None, conn.ftp.read_file, adm_files[-1])
    if not content:
        return await interaction.followup.send("❌ Log-Datei konnte nicht gelesen werden.", ephemeral=True)

    zeilen = max(5, min(zeilen, 40))
    lines  = [l for l in content.splitlines() if l.strip()][-zeilen:]
    result = "\n".join(f"`{l[:110]}`" for l in lines)
    if len(result) > 3900:
        result = result[:3900] + "\n..."

    embed = discord.Embed(
        title=f"🔍 Raw Log – letzte {len(lines)} Zeilen",
        description=result,
        color=0x7F8C8D
    )
    embed.set_footer(text=f"Datei: {adm_files[-1].split('/')[-1]}  •  "
                         f"Tipp: Damage/Loot erscheinen nur wenn der Server diese Events loggt")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════
#  /test – Letztes Log-Event pro Typ in die jeweiligen Channels
# ══════════════════════════════════════════════════════════════
@bot.tree.command(
    name="test",
    description="🧪 Postet das letzte Log-Event jedes Typs in die jeweiligen Channels"
)
@app_commands.describe(zeilen="Zu durchsuchende Log-Zeilen (Standard: 500, max: 2000)",
                       server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_test(interaction: discord.Interaction, zeilen: int = 500,
                   server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    conn = await _require_conn(interaction, need_ftp=True, server=server)
    if conn is None:
        return
    await interaction.response.defer(ephemeral=True)

    # ── 1. Log-Datei lesen ────────────────────────────────────
    log_dir = conn.get("ftp_log_dir")
    if not log_dir:
        return await interaction.followup.send(
            "❌ Log-Verzeichnis nicht konfiguriert. Nutze `/ftp_scan`.", ephemeral=True
        )

    loop = asyncio.get_running_loop()
    adm_files = await loop.run_in_executor(None, conn.ftp.list_adm_files, log_dir)
    if not adm_files:
        return await interaction.followup.send("❌ Keine ADM-Dateien gefunden.", ephemeral=True)

    content = await loop.run_in_executor(None, conn.ftp.read_file, adm_files[-1])
    if not content:
        return await interaction.followup.send("❌ Log-Datei konnte nicht gelesen werden.", ephemeral=True)

    # ── 2. Letzten N Zeilen parsen ────────────────────────────
    zeilen = max(50, min(zeilen, 2000))
    recent_lines = "\n".join(content.splitlines()[-zeilen:])
    # Eigener Parser: mit conn.parser schrieb /test historische Positionen
    # ueber die aktuellen und liess den naechsten Zonenlauf eine laengst
    # verlassene Position fuer frisch halten.
    events, _eigen = _historisch_parsen(conn, recent_lines)

    # ── 3. Pro Feed-Typ das neueste Event merken ───────────────
    # FEED_TYPES (fein: kill, connect, zombie_death, …) statt LOG_TYPES (grob:
    # killfeed, joinleave, …) – genau das ist der Schluessel, den _dispatch
    # tatsaechlich verwendet und den das Dashboard schreibt. Mit LOG_TYPES
    # meldete /test hier fuer JEDES Event "kein Channel konfiguriert", selbst
    # wenn im Dashboard alles korrekt eingerichtet war.
    # Events kommen in Lesereihenfolge → letztes überschreibt → neuestes bleibt
    latest_by_logtype: Dict[str, Dict] = {}
    for ev in events:
        # Dieselbe Rueckfallkette wie _dispatch: fein → grob → catch_all.
        # Ohne den groben und den catch_all-Schluessel meldete /test bei einem
        # Kunden, der NUR "Alles Übrige" gesetzt hat, faelschlich "kein
        # Ereignis" bzw. "kein Channel" – obwohl der Poller genau dorthin
        # postet.
        for lt in (_feed_key(ev), DayZLogParser.EVENT_TO_LOG.get(ev["type"]),
                   "catch_all"):
            if lt:
                latest_by_logtype[lt] = ev

    # ── 4. Pro Feed-Typ in konfigurierten Channel posten ──────
    sent:     List[Tuple[str, str]] = []  # (feed_type, channel_mention)
    no_event: List[str]             = []  # Feed-Typ ohne Event im gescannten Bereich
    no_ch:    List[str]             = []  # Feed-Typ mit Event aber ohne Channel
    errors:   List[Tuple[str, str]] = []  # (feed_type, Fehlermeldung)

    for lt in FEED_TYPES:
        ev   = latest_by_logtype.get(lt)
        feed = cfg.feed_settings(interaction.guild_id or conn.guild_id,
                                 lt, conn.service_id)
        ch_id = feed["channel_id"] if feed else None

        if not ev:
            no_event.append(lt)
            continue
        if not ch_id:
            no_ch.append(lt)
            continue

        ch = bot.get_channel(int(ch_id))
        if not ch:
            errors.append((lt, "Channel nicht gefunden (ID veraltet?)"))
            continue

        embed = EmbedBuilder.build(ev)
        if not embed:
            errors.append((lt, "Embed konnte nicht erstellt werden"))
            continue

        # Test-Kennung in den Embed-Titel & Author einbauen
        embed.title = f"🧪 [TEST] {embed.title or lt}"
        embed.set_author(
            name=f"Testpost via /test · {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else None
        )

        try:
            await ch.send(embed=embed)
            sent.append((lt, ch.mention))
        except discord.Forbidden:
            errors.append((lt, f"Keine Schreibrechte in {ch.mention}"))
        except Exception as ex:
            errors.append((lt, str(ex)[:80]))

    # ── 5. Ergebnis-Embed senden ──────────────────────────────
    ok_count = len(sent)
    color = 0x2ECC71 if ok_count > 0 else 0xE74C3C

    summary = discord.Embed(
        title="🧪 Test-Ergebnis",
        description=(
            f"Gescannt: letzte **{zeilen}** Zeilen aus `{adm_files[-1].split('/')[-1]}`\n"
            f"Events gefunden: **{len(events)}** · Gepostet: **{ok_count}**"
        ),
        color=color,
    )

    def _gekuerzt(zeilen_liste: List[str], trenner: str = "\n") -> str:
        # FEED_TYPES hat gut 50 Eintraege – ungekuerzt sprengt das locker
        # das 1024-Zeichen-Limit eines Embed-Feldes.
        text = trenner.join(zeilen_liste[:20])
        rest = len(zeilen_liste) - 20
        if rest > 0:
            text += f"{trenner}… und {rest} weitere"
        return text[:1024]

    if sent:
        lines = [f"✅ `{lt}` → {ch}" for lt, ch in sent]
        summary.add_field(
            name=f"✅ Erfolgreich gepostet ({len(sent)})",
            value=_gekuerzt(lines),
            inline=False
        )
    if no_ch:
        lines = [f"⚪ `{lt}`" for lt in no_ch]
        summary.add_field(
            name=f"⚪ Kein Channel konfiguriert ({len(no_ch)})",
            value=_gekuerzt(lines, "  "),
            inline=False
        )
    if no_event:
        lines = [f"🔍 `{lt}`" for lt in no_event]
        summary.add_field(
            name=f"🔍 Kein Event in den letzten {zeilen} Zeilen ({len(no_event)})",
            value=_gekuerzt(lines, "  "),
            inline=False
        )
    if errors:
        lines = [f"❌ `{lt}` — {msg}" for lt, msg in errors]
        summary.add_field(
            name=f"❌ Fehler ({len(errors)})",
            value=_gekuerzt(lines),
            inline=False
        )

    summary.set_footer(
        text="🔍-Typen = diese Events kommen in deinen Logs nicht vor "
             "(z.B. Damage/Loot brauchen Server-Mods). "
             "⚪-Typen → im Dashboard unter „Feeds“ einrichten"
    )
    await interaction.followup.send(embed=summary, ephemeral=True)


@bot.tree.command(name="ftp_status", description="🔌 Testet die FTP-Verbindung zum Nitrado-Server")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_ftp_status(interaction: discord.Interaction, server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    await interaction.response.defer(ephemeral=True)
    conn = await _require_conn(interaction, need_ftp=True, server=server)
    if conn is None:
        return

    host     = conn.get("ftp_host", "–")
    port     = conn.get("ftp_port", 21)
    user     = conn.get("ftp_user", "–")
    log_dir  = conn.get("ftp_log_dir",  "Noch nicht gesetzt")

    loop = asyncio.get_running_loop()

    # ── 1. Login-Test ─────────────────────────────────────────
    connect_ok  = False
    connect_msg = ""
    t_connect   = 0.0
    try:
        import ftplib, time as _time
        def _test_login():
            t0  = _time.monotonic()
            ftp = ftplib.FTP()
            ftp.connect(host, int(port), timeout=15)
            ftp.login(user, conn.get("ftp_password", ""))
            welcome = ftp.getwelcome()
            ftp.quit()
            return _time.monotonic() - t0, welcome
        t_connect, welcome = await loop.run_in_executor(None, _test_login)
        connect_ok  = True
        connect_msg = welcome[:80] if welcome else "Verbindung erfolgreich"
    except Exception as e:
        connect_msg = str(e)[:120]

    # ── 2. Log-Verzeichnis lesen ──────────────────────────────
    adm_count  = 0
    adm_latest = "–"
    if connect_ok and log_dir and log_dir != "Noch nicht gesetzt":
        try:
            adm_files = await loop.run_in_executor(None, conn.ftp.list_adm_files, log_dir)
            adm_count  = len(adm_files)
            adm_latest = adm_files[-1].split("/")[-1] if adm_files else "Keine gefunden"
        except Exception as e:
            adm_latest = f"Fehler: {e}"

    # ── 3. Nitrado-Banliste prüfen (Servereinstellungen, nicht FTP) ──
    try:
        ban_names, _bcat, _bkey = await _read_banlist(conn)
        ban_msg = f"✅ {len(ban_names)} Einträge"
    except Exception as e:
        ban_msg = f"⚠️ {e}"

    # ── Embed zusammenbauen ───────────────────────────────────
    if connect_ok:
        color = 0x2ECC71
        title = "🟢 FTP-Verbindung erfolgreich"
    else:
        color = 0xE74C3C
        title = "🔴 FTP-Verbindung fehlgeschlagen"

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Host",
                    value=f"`{host}:{port}`",                              inline=True)
    embed.add_field(name="Benutzer",
                    value=f"`{user}`",                                     inline=True)
    embed.add_field(name="Ping / Antwortzeit",
                    value=f"`{t_connect*1000:.0f} ms`" if connect_ok else "–", inline=True)
    embed.add_field(name="Server-Antwort",
                    value=f"`{connect_msg}`" if connect_ok else f"❌ `{connect_msg}`",
                    inline=False)
    embed.add_field(name="Log-Verzeichnis",
                    value=f"`{log_dir}`",                                  inline=False)
    embed.add_field(name="ADM-Dateien gefunden",
                    value=f"`{adm_count}`  •  Neueste: `{adm_latest}`",   inline=False)
    embed.add_field(name="Nitrado-Banliste (Servereinstellungen)",
                    value=ban_msg,                                         inline=False)

    if not connect_ok:
        embed.set_footer(text="Tipp: `/ftp_scan` holt die Zugangsdaten neu über den Nitrado-Token")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════
#  /log_status – Polling-Status anzeigen
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="log_status", description="📄 Zeigt den aktuellen Log-Polling Status")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_log_status(interaction: discord.Interaction, server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)

    _conn, _fehler = _conn_waehlen(interaction, server)
    if _conn is None:
        return await interaction.response.send_message(_fehler, ephemeral=True)
    # Alles aus DIESER Verbindung – sonst zeigte der Befehl den Log-Pfad und
    # FTP-Host des Betreibers, egal aus welchem Discord er kam.
    state = _conn.log_state.get("current", {})
    embed = discord.Embed(title="📄 Log-Polling Status", color=0x5865F2)
    embed.add_field(name="Server", value=_conn.name, inline=False)
    embed.add_field(name="Aktuelle Log-Datei",
                    value=f"`{state.get('file', 'Keine')}`",         inline=False)
    embed.add_field(name="Gelesene Bytes",
                    value=f"{state.get('offset', 0):,}",             inline=True)
    embed.add_field(name="Poll-Intervall",
                    value=f"{_conn.get('log_poll_interval_seconds', 10)}s",inline=True)
    embed.add_field(name="Log-Verzeichnis",
                    value=f"`{_conn.get('ftp_log_dir') or '–'}`", inline=False)
    embed.add_field(name="Banliste",
                    value="Nitrado-Servereinstellungen (via API)",     inline=False)
    embed.add_field(name="FTP-Host",
                    value=f"`{_conn.get('ftp_host') or '–'}`",    inline=False)
    embed.add_field(name="Bekannte Spieler-Positionen",
                    value=str(len(_conn.parser.player_positions
                                  if _conn.parser else {})),
                    inline=True)
    embed.add_field(name="Lokale Bans",
                    value=str(len(_bans_of(_conn))),                  inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


cmd_ftp_scan.autocomplete("server")(_server_autocomplete)
cmd_raw_log.autocomplete("server")(_server_autocomplete)
cmd_test.autocomplete("server")(_server_autocomplete)
cmd_ftp_status.autocomplete("server")(_server_autocomplete)
cmd_log_status.autocomplete("server")(_server_autocomplete)


# ══════════════════════════════════════════════════════════════
#  /hilfe – Alle Befehle
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name=app_commands.locale_str("hilfe"),
                  description="❓ Zeigt alle verfügbaren Bot-Befehle")
async def cmd_hilfe(interaction: discord.Interaction):
    # Spam-Schutz: pro Nutzer, guild-übergreifend per gid=0-Fallback (DMs)
    gid = interaction.guild_id or 0
    remaining = db.cooldown_remaining(gid, interaction.user.id, "hilfe")
    if remaining > 0:
        return await interaction.response.send_message(
            embed=_cooldown_embed("/hilfe", remaining), ephemeral=True)
    db.set_cooldown(gid, interaction.user.id, "hilfe",
                    int(cfg.config.get("hilfe_cooldown_seconds", 30)))
    embed = discord.Embed(
        title="🎮 DayZ Bot – Befehlsübersicht",
        description="Alle Befehle (Admin-Rolle erforderlich, außer /hilfe)",
        color=0x5865F2
    )
    embed.add_field(name="⚙️ Server-Verwaltung", value=(
        "`/neustart` — Server neu starten\n"
        "`/stoppen` — Server stoppen\n"
        "`/serverstatus` — Server-Status anzeigen\n"
        "`/auto restart <intervall>` — Geplante Neustarts (Uhrzeit per Dropdown)\n"
        "`/auto status` / `/auto off` — Zeitplan anzeigen / deaktivieren"
    ), inline=False)
    embed.add_field(name="🔨 Spieler-Verwaltung", value=(
        "`/ban <spieler> [grund]` — Auf die Nitrado-Banliste setzen (Komma = mehrere)\n"
        "`/ban_entfernen <spieler>` — Von der Nitrado-Banliste entfernen\n"
        "`/banlist` — Nitrado-Banliste anzeigen\n"
        "`/whitelist add <spieler>` — Auf die Nitrado-Whitelist setzen (Komma = mehrere)\n"
        "`/whitelist remove <spieler>` — Von der Nitrado-Whitelist entfernen\n"
        "`/whitelist show` — Nitrado-Whitelist anzeigen\n"
        "`/send whitelist panel <panel> <admin>` — Whitelist-Anmelde-Panel senden\n"
        "`/admin_position` — Letzte Positionen\n"
        "`/spieler_suche <name>` — Spieler in Logs suchen"
    ), inline=False)
    embed.add_field(name="📡 Feed-Verwaltung", value=(
        "`/show_feeds` — Zeigt, welche Feeds für diesen Server eingerichtet sind\n"
        "`/test [zeilen]` — Beispiel-Event je Feed-Typ aus den letzten Log-Zeilen\n"
        "*Einrichten, ändern und löschen im Dashboard unter „Feeds“ – "
        "dort mit Farbe, Position und Zeitstempel je Feed.*"
    ), inline=False)
    embed.add_field(name="🛡️ Zonen-Pings", value=(
        "`/zone list` — Alle aktiven Zonen dieses Servers\n"
        "`/zone allowlist add|remove|show <zone> <spieler>` — Spieler in einer Zone "
        "ignorieren / wieder melden / anzeigen\n"
        "*Anlegen und Bearbeiten im Dashboard unter „Zones“ – dort auch "
        "Polygon-Zonen und mehrere Ping-Rollen.*"
    ), inline=False)
    embed.add_field(name="📢 Einrichtung", value=(
        "Nitrado-Token, Server-Auswahl, Feeds, Zonen, Shop, Ankündigungen und "
        "Auto-Neustarts werden im **Web-Dashboard** eingerichtet (Adresse steht "
        "beim Start im Log).\n"
        "Die früheren Befehle `/setup token`, `/setup feeds`, "
        "`/setup uebersicht`, `/edit_feeds` und `/zone create|edit|remove` "
        "gibt es nicht mehr."
    ), inline=False)
    embed.add_field(name="📊 Kill-Stats & Belohnungen", value=(
        "`/stats <spieler>` — Kills, Tode, K/D, Lieblingswaffe, weitester Kill\n"
        "`/leaderboard` — Top 10 PvP-Killer\n"
        "`/link <playstation-name>` / `/unlink` — Account verknüpfen (Kill- & Spielzeit-Geld)\n"
        "`/username list` — Eigene Verknüpfung anzeigen (Admins: alle, 🟢 = online)\n"
        "`/forcelink <name> <@user>` / `/forceunlink <@user>` *(Admin)*\n"
        "`/bounty <spieler> <betrag>` — Kopfgeld aussetzen · `/bounties` — aktive Kopfgelder"
    ), inline=False)
    embed.add_field(name="🔧 Diagnose", value=(
        "`/log_status` — Polling-Status\n"
        "`/ftp_scan` — FTP neu scannen\n"
        "`/ftp_status` — FTP-Verbindung testen\n"
        "`/raw_log [zeilen]` — Rohe Log-Zeilen anzeigen (Debug)\n"
        "`/test [zeilen]` — Letztes Event pro Typ in Channels posten"
    ), inline=False)
    embed.add_field(name="💰 Economy", value=(
        "`/balance [@user]` — Wallet & Bank\n"
        "`/deposit [amount]` / `/withdraw [amount]`\n"
        "`/pay <@user> <betrag>` — Geld an Mitspieler überweisen\n"
        "`/work` `/daily` `/beg` — Geld verdienen\n"
        "`/addmoney` `/removemoney` `/setbalance` *(Admin)*\n"
        "`/economy_reload` — config.json neu laden *(Admin)*"
    ), inline=False)
    embed.add_field(name="🎰 Casino", value=(
        "`/blackjack <bet>` — Blackjack mit Hit/Stand-Buttons\n"
        "`/roulette <bet> <wager>` — red/black/even/odd/low/high/0-36\n"
        "`/slots <bet>` — Slot-Maschine"
    ), inline=False)
    embed.add_field(name="🛒 Shop", value=(
        "`/shop list [category]` — Item-Katalog (leer = Kategorie-Übersicht)\n"
        "`/buy <item> <amount> <x> <z> [y]` — Item kaufen (spawnt nach Neustart)\n"
        "`/add shopitem <classnames> <price>` — Item/Bundle hinzufügen *(Admin)*\n"
        "`/bundle add` — Bundle per Formular anlegen (Menge je Item, Dropdown-Kategorie) *(Admin)*\n"
        "`/edit shopitem <item> […]` — Classnames/Preis/Name/Kategorie ändern *(Admin)*\n"
        "`/shop pending` `/shop check` `/shop cleanup` `/shop setprice` "
        "`/shop enable` `/shop removeitem` *(Admin)*\n"
        "*Shop-Log und Economy-Log als Feed einrichten: Dashboard → „Feeds“.*"
    ), inline=False)
    embed.add_field(name="📢 Ankündigungen", value=(
        "`/erstellen` — Neue wiederkehrende Ankündigung anlegen (Tag/Uhrzeit/Wiederholung per Dropdown)\n"
        "`/liste` — Alle Ankündigungen mit nächstem Sendetermin & Countdown\n"
        "`/löschen <index>` — Ankündigung löschen\n"
        "`/edit ankuendigung <index>` — Nachricht/Bild einer Ankündigung ändern\n"
        "`/hackban <user_id> [grund]` — Discord-Nutzer per ID bannen"
    ), inline=False)
    _c_hilfe = _conn_of(interaction)
    admin_ids = _c_hilfe.get("admin_role_ids", []) if _c_hilfe is not None else []
    admin_name = (_c_hilfe.get("admin_role_name", "DayZ Admin") if _c_hilfe is not None
                  else "DayZ Admin")
    # Kein Verweis mehr auf die Dashboard-Optionen: admin_role_ids wird dort
    # nicht angeboten (siehe api_options) – der Tipp schickte Betreiber auf
    # die Suche nach einer Einstellung, die es an der Stelle nicht gibt.
    footer = (f"Admin-Rollen-IDs: {', '.join(str(i) for i in admin_ids)}"
              if admin_ids else
              f"Admin-Rolle: {admin_name} "
              f"(admin_role_ids stehen in der connections.json des Servers)")
    embed.set_footer(text=footer)
    await interaction.response.send_message(embed=embed)


# ══════════════════════════════════════════════════════════════
#  ANKÜNDIGUNGEN – Wiederkehrende geplante Nachrichten
#  (/erstellen, /liste, /löschen, /edit ankuendigung, /hackban)
# ══════════════════════════════════════════════════════════════
ANNOUNCEMENTS_FILE = "announcements.json"

try:
    with open(ANNOUNCEMENTS_FILE, "r", encoding="utf-8") as f:
        ann_data = json.load(f)
except FileNotFoundError:
    ann_data = {"announcements": []}
    with open(ANNOUNCEMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(ann_data, f, ensure_ascii=False, indent=4)


def save_announcements():
    with open(ANNOUNCEMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(ann_data, f, ensure_ascii=False, indent=4)


def _ann_eigene(conn: Optional["ServerConnection"]) -> List[Tuple[int, dict]]:
    """Ankündigungen eines Servers als ``[(Position in der Datei, Eintrag)]``.

    Alle Ankündigungen liegen in einer gemeinsamen Datei. Ohne diese Filterung
    zeigte ``/liste`` jedem Kunden die Ankündigungen aller anderen – und
    ``/löschen 0`` traf den erstbesten fremden Eintrag. Alt-Einträge ohne
    ``service_id`` gehören dem Hauptserver.
    """
    if conn is None:
        return []
    haupt = connections.primary()
    out: List[Tuple[int, dict]] = []
    for i, ann in enumerate(ann_data.get("announcements", [])):
        sid = str(ann.get("service_id") or "")
        if sid == conn.service_id or (not sid and haupt is conn):
            out.append((i, ann))
    return out


async def _ann_position(interaction: discord.Interaction,
                        index: int) -> Optional[int]:
    """Rechnet die in ``/liste`` angezeigte Nummer in die Dateiposition um.

    Antwortet selbst, wenn die Nummer nicht zu einer eigenen Ankündigung
    gehört – der Aufrufer bricht dann mit ``return`` ab.
    """
    conn = _conn_of(interaction)
    if conn is None:
        await interaction.response.send_message(PREMIUM_MISSING_TEXT, ephemeral=True)
        return None
    eigene = _ann_eigene(conn)
    if index < 0 or index >= len(eigene):
        await interaction.response.send_message(
            "❌ Ungültige Nummer – `/liste` zeigt die Ankündigungen dieses Servers.",
            ephemeral=True)
        return None
    return eigene[index][0]


def should_send_today(ann: dict, today: date) -> bool:
    """
    Prüft ob eine Ankündigung heute gesendet werden soll,
    basierend auf dem repeat-Typ und dem letzten Sendedatum.
    """
    repeat = ann.get("repeat", "weekly")
    last_sent_str = ann.get("last_sent")

    # Intervall in Wochen bestimmen
    interval_map = {
        "weekly":    1,
        "biweekly":  2,
        "triweekly": 3,
        "monthly":   4,  # ~4 Wochen
    }
    interval_weeks = interval_map.get(repeat, 1)

    if not last_sent_str:
        # Noch nie gesendet → darf heute gesendet werden
        return True

    last_sent = date.fromisoformat(last_sent_str)
    next_send = last_sent + timedelta(weeks=interval_weeks)

    return today >= next_send


def get_next_send_datetime(ann: dict) -> datetime:
    """
    Berechnet den nächsten Sendezeitpunkt einer Ankündigung
    als datetime-Objekt (Europe/Berlin).
    """
    tz = _berlin_tz()
    repeat = ann.get("repeat", "weekly")
    last_sent_str = ann.get("last_sent")
    time_str = ann.get("time", "00:00")
    hour, minute = map(int, time_str.split(":"))

    interval_map = {
        "weekly":    1,
        "biweekly":  2,
        "triweekly": 3,
        "monthly":   4,
    }
    interval_weeks = interval_map.get(repeat, 1)

    today = datetime.now(tz).date()

    if not last_sent_str:
        # Noch nie gesendet → nächster passender Wochentag
        day_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2,
            "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6
        }
        target_weekday = day_map.get(ann.get("day", "monday"), 0)
        days_ahead = (target_weekday - today.weekday()) % 7
        next_date = today + timedelta(days=days_ahead)
    else:
        last_sent = date.fromisoformat(last_sent_str)
        next_date = last_sent + timedelta(weeks=interval_weeks)

    return datetime(next_date.year, next_date.month, next_date.day, hour, minute, 0, tzinfo=tz)


def format_countdown(dt: datetime) -> str:
    """Gibt die verbleibende Zeit bis dt als lesbaren String zurück."""
    now = datetime.now(_berlin_tz())
    diff = dt - now

    if diff.total_seconds() <= 0:
        return "Wird gleich gesendet"

    total_seconds = int(diff.total_seconds())
    days    = total_seconds // 86400
    hours   = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    parts = []
    if days:    parts.append(f"{days}T")
    if hours:   parts.append(f"{hours}Std")
    if minutes: parts.append(f"{minutes}Min")
    parts.append(f"{seconds}Sek")

    return " ".join(parts)


ann_already_sent = set()


async def check_announcements():
    now = datetime.now(_berlin_tz())

    day = now.strftime("%A").lower()
    time_str = now.strftime("%H:%M")
    today = now.date()

    for ann in ann_data["announcements"]:

        if ann["day"] == day and ann["time"] == time_str:

            # Die service_id gehoert in den Schluessel: zwei Server derselben
            # Guild koennen zur selben Minute unterschiedliche Ankuendigungen
            # in denselben Channel planen. Ohne sie galt die zweite nach dem
            # ersten Versand als "schon gesendet" und fiel stillschweigend aus.
            key = (f"{today.isoformat()}-{day}-{time_str}-{ann['channel_id']}"
                   f"-{ann.get('service_id') or ''}-{ann.get('message', '')[:40]}")

            if key in ann_already_sent:
                continue

            # Repeat-Logik prüfen
            if not should_send_today(ann, today):
                continue

            channel = bot.get_channel(int(ann["channel_id"]))

            if channel:

                embed = discord.Embed(
                    description=ann["message"],
                    color=discord.Color.blue()
                )

                if ann.get("image"):
                    embed.set_image(url=ann["image"])

                try:
                    await channel.send(embed=embed)
                    ann_already_sent.add(key)

                    # Letztes Sendedatum speichern
                    ann["last_sent"] = today.isoformat()
                    save_announcements()

                except Exception as e:
                    log.error(f"[ANKÜNDIGUNG] Fehler beim Senden: {e}")


@tasks.loop(minutes=1)
async def announcement_scheduler():
    await check_announcements()


# ─── Ankündigungs-UI: Tag / Uhrzeit / Wiederholung ───

class TagSelect(discord.ui.Select):

    def __init__(self):

        options = [
            discord.SelectOption(label="Montag", value="monday"),
            discord.SelectOption(label="Dienstag", value="tuesday"),
            discord.SelectOption(label="Mittwoch", value="wednesday"),
            discord.SelectOption(label="Donnerstag", value="thursday"),
            discord.SelectOption(label="Freitag", value="friday"),
            discord.SelectOption(label="Samstag", value="saturday"),
            discord.SelectOption(label="Sonntag", value="sunday"),
        ]

        super().__init__(
            placeholder="Tag auswählen",
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        self.view.selected_day = self.values[0]

        await interaction.response.defer()


class TimeSelect(discord.ui.Select):

    def __init__(self, page=0):

        all_times = []

        for h in range(24):

            all_times.append(f"{h:02d}:00")
            all_times.append(f"{h:02d}:30")

        per_page = 16

        start = page * per_page
        end = start + per_page

        times = all_times[start:end]

        options = [
            discord.SelectOption(label=t, value=t)
            for t in times
        ]

        super().__init__(
            placeholder=f"Uhrzeit (Seite {page+1}/3)",
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        self.view.selected_time = self.values[0]

        await interaction.response.defer()


class RepeatSelect(discord.ui.Select):

    def __init__(self):

        options = [
            discord.SelectOption(label="Jede Woche", value="weekly"),
            discord.SelectOption(label="Alle 2 Wochen", value="biweekly"),
            discord.SelectOption(label="Alle 3 Wochen", value="triweekly"),
            discord.SelectOption(label="Jeden Monat", value="monthly"),
        ]

        super().__init__(
            placeholder="Wiederholung auswählen",
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        self.view.selected_repeat = self.values[0]
        await interaction.response.defer()


class PageButton(discord.ui.Button):

    def __init__(self, label, page):

        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary
        )

        self.page = page

    async def callback(self, interaction: discord.Interaction):

        view = CreateAnnouncementView(page=self.page)

        view.selected_day = self.view.selected_day
        view.selected_time = self.view.selected_time
        view.selected_repeat = self.view.selected_repeat

        await interaction.response.edit_message(view=view)


class NextButton(discord.ui.Button):

    def __init__(self):

        super().__init__(
            label="Weiter",
            style=discord.ButtonStyle.success
        )

    async def callback(self, interaction: discord.Interaction):

        if not self.view.selected_day or not self.view.selected_time or not self.view.selected_repeat:

            await interaction.response.send_message(
                "Bitte Tag und Uhrzeit wählen.",
                ephemeral=True
            )

            return

        modal = AnnouncementModal(
            self.view.selected_day,
            self.view.selected_time,
            self.view.selected_repeat
        )

        await interaction.response.send_modal(modal)


class CreateAnnouncementView(discord.ui.View):

    def __init__(self, page=0):

        super().__init__(timeout=300)

        self.selected_day = None
        self.selected_time = None
        self.selected_repeat = None

        self.add_item(TagSelect())
        self.add_item(TimeSelect(page))
        self.add_item(RepeatSelect())

        if page > 0:
            self.add_item(PageButton("⬅", page - 1))

        if page < 2:
            self.add_item(PageButton("➡", page + 1))

        self.add_item(NextButton())


class AnnouncementModal(discord.ui.Modal):

    def __init__(self, day, time, repeat_type):

        super().__init__(title="Ankündigung")

        self.day = day
        self.time = time
        self.repeat_type = repeat_type

        self.msg = discord.ui.TextInput(
            label="Nachricht",
            style=discord.TextStyle.paragraph,
            max_length=2000
        )

        self.channel = discord.ui.TextInput(
            label="Channel-ID"
        )

        self.image = discord.ui.TextInput(
            label="Bild URL",
            required=False
        )

        self.add_item(self.msg)
        self.add_item(self.channel)
        self.add_item(self.image)

    async def on_submit(self, interaction: discord.Interaction):

        try:
            kanal_id = int(self.channel.value)

        except Exception:

            return await interaction.response.send_message(
                "❌ Fehlerhafte Channel-ID",
                ephemeral=True
            )

        # Bewusst NICHT bot.get_channel(): das findet jeden Channel, den der Bot
        # sieht – auch die anderer Kunden. Der Zielchannel muss in DIESER Guild
        # liegen, sonst liesse sich hier eine wiederkehrende Ankuendigung im
        # Discord eines fremden Kunden einplanen.
        channel = interaction.guild.get_channel(kanal_id) if interaction.guild else None

        if not channel:

            return await interaction.response.send_message(
                "❌ Channel nicht gefunden – er muss in diesem Discord-Server liegen.",
                ephemeral=True
            )

        _conn = _conn_of(interaction)
        ann_data["announcements"].append({
            "message": self.msg.value,
            "channel_id": str(self.channel.value),
            "day": self.day,
            "time": self.time,
            "repeat": self.repeat_type,
            "image": self.image.value.strip() if self.image.value else None,
            "last_sent": None,  # Wird nach dem ersten Senden gesetzt
            # Gehoert zu genau einem Server, sonst sehen alle Kunden alles
            "service_id": _conn.service_id if _conn is not None else "",
        })

        save_announcements()

        await interaction.response.send_message(
            "✅ Ankündigung gespeichert",
            ephemeral=True
        )


class EditAnnouncementModal(discord.ui.Modal):

    def __init__(self, index):

        super().__init__(title="Ankündigung bearbeiten")

        self.index = index

        ann = ann_data["announcements"][index]
        # Der Index ist eine Position in EINER globalen Liste. Wird waehrend
        # das Modal offen ist ein frueherer Eintrag geloescht, rutscht ein
        # fremder Datensatz auf diese Position – ohne Merkmal wuerde dann beim
        # Absenden die Ankuendigung eines anderen Kunden ueberschrieben.
        self.gehoert_zu = str(ann.get("service_id") or "")
        self.war_text = str(ann.get("message") or "")

        self.message_input = discord.ui.TextInput(
            label="Neue Nachricht",
            style=discord.TextStyle.paragraph,
            default=ann["message"],
            max_length=2000
        )

        self.image_input = discord.ui.TextInput(
            label="Neue Bild URL",
            default=ann.get("image") or "",
            required=False
        )

        self.add_item(self.message_input)
        self.add_item(self.image_input)

    async def on_submit(self, interaction: discord.Interaction):

        eintraege = ann_data["announcements"]
        # Steht an der gemerkten Stelle noch derselbe Eintrag? Sonst hat sich
        # die Liste zwischenzeitlich verschoben und wir wuerden den falschen
        # (moeglicherweise fremden) Datensatz ueberschreiben.
        ziel = eintraege[self.index] if 0 <= self.index < len(eintraege) else None
        if (ziel is None
                or str(ziel.get("service_id") or "") != self.gehoert_zu
                or str(ziel.get("message") or "") != self.war_text):
            return await interaction.response.send_message(
                "❌ Die Liste hat sich inzwischen geändert – bitte `/liste` erneut "
                "aufrufen und die Ankündigung neu auswählen.",
                ephemeral=True
            )

        ziel["message"] = self.message_input.value

        ziel["image"] = (
            self.image_input.value.strip()
            if self.image_input.value
            else None
        )

        save_announcements()

        await interaction.response.send_message(
            "✅ Ankündigung bearbeitet",
            ephemeral=True
        )


@bot.tree.command(name=app_commands.locale_str("erstellen"),
                  description="📢 Neue wiederkehrende Ankündigung anlegen")
async def cmd_ann_erstellen(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)

    await interaction.response.send_message(
        "Setup starten:",
        view=CreateAnnouncementView(),
        ephemeral=True
    )


@bot.tree.command(name=app_commands.locale_str("liste"),
                  description="📋 Zeigt alle geplanten Ankündigungen")
async def cmd_ann_liste(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)

    conn = _conn_of(interaction)
    if conn is None:
        return await interaction.response.send_message(PREMIUM_MISSING_TEXT, ephemeral=True)
    eigene = _ann_eigene(conn)

    embed = discord.Embed(
        title="📋 Ankündigungen",
        color=discord.Color.blue()
    )

    if not eigene:

        embed.description = "Keine Ankündigungen gespeichert."

    else:

        repeat_label = {
            "weekly":    "Jede Woche",
            "biweekly":  "Alle 2 Wochen",
            "triweekly": "Alle 3 Wochen",
            "monthly":   "Jeden Monat",
        }

        day_label = {
            "monday": "Montag", "tuesday": "Dienstag", "wednesday": "Mittwoch",
            "thursday": "Donnerstag", "friday": "Freitag",
            "saturday": "Samstag", "sunday": "Sonntag"
        }

        for i, (_pos, ann) in enumerate(eigene):

            last_sent_str = ann.get("last_sent")
            last_sent_display = (
                f"📅 Zuletzt gesendet: **{last_sent_str}** um **{ann['time']} Uhr**"
                if last_sent_str
                else "📅 Zuletzt gesendet: **Noch nie**"
            )

            next_dt = get_next_send_datetime(ann)
            next_display = (
                f"⏭️ Nächster Post: **{next_dt.strftime('%d.%m.%Y')}** um **{next_dt.strftime('%H:%M')} Uhr**\n"
                f"⏱️ In: **{format_countdown(next_dt)}**"
            )

            embed.add_field(
                name=f"#{i} • {day_label.get(ann['day'], ann['day'])} • {ann['time']} Uhr • {repeat_label.get(ann.get('repeat', 'weekly'), ann.get('repeat', ''))}",
                value=(
                    f"💬 {ann['message']}\n"
                    f"📢 Channel: <#{ann['channel_id']}>\n"
                    f"{last_sent_display}\n"
                    f"{next_display}"
                ),
                inline=False
            )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


@bot.tree.command(name=app_commands.locale_str("löschen"),
                  description="🗑️ Löscht eine Ankündigung")
@app_commands.describe(index="Nummer der Ankündigung (siehe /liste)")
async def cmd_ann_loeschen(
    interaction: discord.Interaction,
    index: int
):
    if not _is_admin(interaction):
        return await _deny(interaction)

    pos = await _ann_position(interaction, index)
    if pos is None:
        return

    ann_data["announcements"].pop(pos)

    save_announcements()

    await interaction.response.send_message(
        "✅ Gelöscht",
        ephemeral=True
    )


@bot.tree.command(name="hackban", description="🔨 Bannt einen Discord-Benutzer per ID.")
@app_commands.describe(
    user_id="Discord User-ID",
    grund="Grund für den Bann"
)
async def cmd_hackban(
    interaction: discord.Interaction,
    user_id: str,
    grund: str = "Kein Grund angegeben"
):
    if not _is_admin(interaction):
        return await _deny(interaction)

    try:

        user = await bot.fetch_user(int(user_id))

        await interaction.guild.ban(
            user,
            reason=grund,
            delete_message_days=0
        )

        await interaction.response.send_message(
            f"✅ Benutzer {user} wurde gebannt.\nGrund: {grund}"
        )

    except discord.NotFound:

        await interaction.response.send_message(
            "❌ Benutzer nicht gefunden.",
            ephemeral=True
        )

    except discord.Forbidden:

        await interaction.response.send_message(
            "❌ Keine Rechte.",
            ephemeral=True
        )

    except ValueError:

        await interaction.response.send_message(
            "❌ Ungültige User-ID.",
            ephemeral=True
        )

    except Exception as e:

        await interaction.response.send_message(
            f"❌ Fehler: {e}",
            ephemeral=True
        )


# ══════════════════════════════════════════════════════════════
#  ECONOMY-DATENBANK (SQLite)
#  Geldsalden/Buchungen laufen bewusst über SQLite statt JSON,
#  damit parallele Buchungen die Salden nicht korrumpieren.
# ══════════════════════════════════════════════════════════════
ECON_DB_FILE = "economy.db"

class EconomyDB:
    """Persistenz für Salden (Wallet/Bank), Cooldowns, Käufe und Casino-Historie.
    Die Verbindung wird lazy beim ersten Zugriff geöffnet – erst dann ist
    config.json geladen und economy_db_path bekannt. Ein RLock schützt
    parallele Zugriffe; WAL reduziert fsync-Blocking auf dem Event-Loop."""

    def __init__(self, path: Optional[str] = None):
        self._lock = threading.RLock()
        self._path = path
        self._db: Optional[sqlite3.Connection] = None

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            with self._lock:
                if self._db is None:
                    path = self._path or str(cfg.config.get("economy_db_path") or ECON_DB_FILE)
                    conn = sqlite3.connect(path, check_same_thread=False)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA busy_timeout=5000")
                    self._create_tables(conn)
                    self._db = conn
                    log.info(f"[ECON] SQLite-Datenbank bereit: {path}")
        return self._db

    def _create_tables(self, c: sqlite3.Connection):
        with self._lock:
            c.execute("""CREATE TABLE IF NOT EXISTS balances (
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                wallet   INTEGER NOT NULL DEFAULT 0,
                bank     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id))""")
            c.execute("""CREATE TABLE IF NOT EXISTS cooldowns (
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                action   TEXT    NOT NULL,
                ready_at REAL    NOT NULL,
                PRIMARY KEY (guild_id, user_id, action))""")
            # service_id: auf WELCHEM Nitrado-Server das Item spawnen soll.
            # Eine Guild kann mehrere Server verwalten - ohne diese Spalte
            # raeumte Server A beim Neustart die Kaeufe von Server B ab.
            c.execute("""CREATE TABLE IF NOT EXISTS purchases (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id   TEXT NOT NULL DEFAULT '',
                guild_id     INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                user_name    TEXT,
                item_name    TEXT,
                classname    TEXT,
                amount       INTEGER,
                total_price  INTEGER,
                x REAL, y REAL, z REAL,
                area_names   TEXT,
                status       TEXT DEFAULT 'pending',
                created_at   REAL,
                delivered_at REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS casino_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER,
                user_id    INTEGER,
                game       TEXT,
                bet        INTEGER,
                payout     INTEGER,
                result     TEXT,
                created_at REAL)""")
            # PvP-Kills für /stats und /leaderboard – je Nitrado-Server getrennt,
            # sonst stünden die Spieler fremder Kunden in derselben Rangliste.
            c.execute("""CREATE TABLE IF NOT EXISTS kills (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id  TEXT NOT NULL DEFAULT '',
                created_at  REAL,
                killer_name TEXT,
                killer_id   TEXT,
                victim_name TEXT,
                victim_id   TEXT,
                weapon      TEXT,
                distance    REAL)""")
            # Discord-User ↔ Ingame-Name (pro Guild, ein Name nur einmal)
            c.execute("""CREATE TABLE IF NOT EXISTS links (
                guild_id    INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                ingame_name TEXT    NOT NULL COLLATE NOCASE,
                ingame_id   TEXT,
                created_at  REAL,
                PRIMARY KEY (guild_id, user_id),
                UNIQUE (guild_id, ingame_name))""")
            # Kopfgelder (Betrag wurde beim Aussetzen bereits abgebucht)
            c.execute("""CREATE TABLE IF NOT EXISTS bounties (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                target_name TEXT    NOT NULL COLLATE NOCASE,
                amount      INTEGER NOT NULL,
                placed_by   INTEGER NOT NULL,
                created_at  REAL,
                status      TEXT DEFAULT 'open',
                claimed_by  INTEGER,
                claimed_at  REAL)""")
            # Offene Spielzeit-Sitzungen (connect → disconnect/Restart).
            # service_id gehoert in den Primaerschluessel: derselbe Spielername
            # kann gleichzeitig auf mehreren Servern online sein.
            c.execute("""CREATE TABLE IF NOT EXISTS sessions (
                service_id      TEXT NOT NULL DEFAULT '',
                ingame_name     TEXT NOT NULL COLLATE NOCASE,
                ingame_id       TEXT,
                connect_ts      REAL NOT NULL,
                last_seen_ts    REAL NOT NULL,
                credited_blocks INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (service_id, ingame_name))""")
            self._migriere_serverspalten(c)
            c.commit()

    def _migriere_serverspalten(self, c: sqlite3.Connection):
        """Ergaenzt ``service_id`` in Alt-Datenbanken.

        Ohne diese Spalte zaehlten Kills aller Kunden in eine gemeinsame
        Rangliste und ein Neustart auf einem Server beendete die Spielzeit-
        Sitzungen aller anderen. Bestandsdaten bekommen die Service-ID des
        Hauptservers, denn vor der Mandantentrennung gab es nur ihn.
        """
        try:
            haupt = connections.primary()
            alt_id = haupt.service_id if haupt is not None else ""
        except Exception:  # noqa: BLE001 – Registry evtl. noch nicht geladen
            alt_id = ""
        for tabelle in ("kills", "sessions", "purchases"):
            spalten = {r["name"] for r in c.execute(f"PRAGMA table_info({tabelle})")}
            if not spalten or "service_id" in spalten:
                continue
            if tabelle == "purchases":
                # Offene Kaeufe gehoeren dem Server, der ihre Guild bedient –
                # vor dem Mehrserverbetrieb war das je Guild genau einer.
                c.execute("ALTER TABLE purchases "
                          "ADD COLUMN service_id TEXT NOT NULL DEFAULT ''")
                try:
                    for zeile in c.execute(
                            "SELECT DISTINCT guild_id FROM purchases").fetchall():
                        gid = zeile["guild_id"] if isinstance(zeile, sqlite3.Row) else zeile[0]
                        ziel = connections.for_guild(gid)
                        if ziel is not None:
                            c.execute("UPDATE purchases SET service_id=? WHERE guild_id=?",
                                      (ziel.service_id, gid))
                except Exception as e:  # noqa: BLE001 – Registry evtl. noch leer
                    log.warning(f"[ECON] Kaeufe konnten nicht zugeordnet werden: {e}")
            elif tabelle == "sessions":
                # Primaerschluessel aendert sich → Tabelle neu aufbauen
                c.execute("ALTER TABLE sessions RENAME TO sessions_alt")
                c.execute("""CREATE TABLE sessions (
                    service_id      TEXT NOT NULL DEFAULT '',
                    ingame_name     TEXT NOT NULL COLLATE NOCASE,
                    ingame_id       TEXT,
                    connect_ts      REAL NOT NULL,
                    last_seen_ts    REAL NOT NULL,
                    credited_blocks INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (service_id, ingame_name))""")
                c.execute(
                    "INSERT INTO sessions (service_id, ingame_name, ingame_id, "
                    "connect_ts, last_seen_ts, credited_blocks) "
                    "SELECT ?, ingame_name, ingame_id, connect_ts, last_seen_ts, "
                    "credited_blocks FROM sessions_alt", (alt_id,))
                c.execute("DROP TABLE sessions_alt")
            else:
                c.execute("ALTER TABLE kills ADD COLUMN service_id TEXT NOT NULL DEFAULT ''")
                c.execute("UPDATE kills SET service_id=?", (alt_id,))
            log.info(f"[ECON] Tabelle '{tabelle}' um service_id ergaenzt "
                     f"(Bestand → Server {alt_id or '-'}).")

    # ── Salden ────────────────────────────────────────────────
    def ensure_user(self, guild_id: int, user_id: int):
        """Legt den User mit Startguthaben an, falls noch nicht vorhanden.

        Das Startguthaben kommt vom Server dieser Guild – jeder Kunde legt es
        im Dashboard selbst fest.
        """
        try:
            _c = connections.for_guild(int(guild_id))
        except Exception:  # noqa: BLE001
            _c = None
        start = int((_c.get("starting_balance", 0) if _c is not None
                     else cfg.config.get("starting_balance", 0)) or 0)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO balances (guild_id, user_id, wallet, bank) VALUES (?,?,?,0)",
                (guild_id, user_id, start))
            self._conn.commit()

    def get_balance(self, guild_id: int, user_id: int) -> Tuple[int, int]:
        self.ensure_user(guild_id, user_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT wallet, bank FROM balances WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)).fetchone()
        return (int(row["wallet"]), int(row["bank"])) if row else (0, 0)

    def add_wallet(self, guild_id: int, user_id: int, delta: int) -> Tuple[int, int]:
        """Addiert delta (auch negativ) aufs Wallet – nie unter 0. Gibt (wallet, bank) zurück."""
        self.ensure_user(guild_id, user_id)
        with self._lock:
            self._conn.execute(
                "UPDATE balances SET wallet = MAX(0, wallet + ?) WHERE guild_id=? AND user_id=?",
                (int(delta), guild_id, user_id))
            self._conn.commit()
        return self.get_balance(guild_id, user_id)

    def set_wallet(self, guild_id: int, user_id: int, value: int) -> Tuple[int, int]:
        self.ensure_user(guild_id, user_id)
        with self._lock:
            self._conn.execute(
                "UPDATE balances SET wallet = MAX(0, ?) WHERE guild_id=? AND user_id=?",
                (int(value), guild_id, user_id))
            self._conn.commit()
        return self.get_balance(guild_id, user_id)

    def try_spend_wallet(self, guild_id: int, user_id: int, amount: int) -> bool:
        """Atomare Abbuchung: nur wenn genug Guthaben vorhanden ist (kein Race möglich)."""
        if amount < 0:
            return False
        if amount == 0:
            return True   # Gratis-Item (Preis 0): nichts abzubuchen, Kauf ist gültig
        self.ensure_user(guild_id, user_id)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE balances SET wallet = wallet - ? "
                "WHERE guild_id=? AND user_id=? AND wallet >= ?",
                (amount, guild_id, user_id, amount))
            self._conn.commit()
            return cur.rowcount > 0

    def deposit(self, guild_id: int, user_id: int, amount: Optional[int]) -> Tuple[int, int, int]:
        """Wallet → Bank. amount=None → alles. Gibt (verschoben, wallet, bank) zurück."""
        self.ensure_user(guild_id, user_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT wallet, bank FROM balances WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)).fetchone()
            move = int(row["wallet"]) if amount is None else min(int(amount), int(row["wallet"]))
            if move <= 0:
                return 0, int(row["wallet"]), int(row["bank"])
            self._conn.execute(
                "UPDATE balances SET wallet = wallet - ?, bank = bank + ? "
                "WHERE guild_id=? AND user_id=?",
                (move, move, guild_id, user_id))
            self._conn.commit()
            return move, int(row["wallet"]) - move, int(row["bank"]) + move

    def withdraw(self, guild_id: int, user_id: int, amount: Optional[int]) -> Tuple[int, int, int]:
        """Bank → Wallet. amount=None → alles. Gibt (verschoben, wallet, bank) zurück."""
        self.ensure_user(guild_id, user_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT wallet, bank FROM balances WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)).fetchone()
            move = int(row["bank"]) if amount is None else min(int(amount), int(row["bank"]))
            if move <= 0:
                return 0, int(row["wallet"]), int(row["bank"])
            self._conn.execute(
                "UPDATE balances SET wallet = wallet + ?, bank = bank - ? "
                "WHERE guild_id=? AND user_id=?",
                (move, move, guild_id, user_id))
            self._conn.commit()
            return move, int(row["wallet"]) + move, int(row["bank"]) - move

    # ── Cooldowns ─────────────────────────────────────────────
    def cooldown_remaining(self, guild_id: int, user_id: int, action: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT ready_at FROM cooldowns WHERE guild_id=? AND user_id=? AND action=?",
                (guild_id, user_id, action)).fetchone()
        if not row:
            return 0.0
        return max(0.0, float(row["ready_at"]) - time.time())

    def set_cooldown(self, guild_id: int, user_id: int, action: str, seconds: float):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cooldowns (guild_id, user_id, action, ready_at) "
                "VALUES (?,?,?,?)",
                (guild_id, user_id, action, time.time() + max(0.0, seconds)))
            self._conn.commit()

    # ── Käufe / Delivery-Tracking ─────────────────────────────
    def create_purchase(self, service_id: str, guild_id: int, user_id: int,
                        user_name: str,
                        item_name: str, classname: str, amount: int, total_price: int,
                        x: float, y: float, z: float, area_names: List[str]) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO purchases
                   (service_id, guild_id, user_id, user_name, item_name, classname,
                    amount, total_price, x, y, z, area_names, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                (str(service_id or ""), guild_id, user_id, user_name, item_name,
                 classname, amount,
                 total_price, x, y, z, json.dumps(area_names), time.time()))
            self._conn.commit()
            return int(cur.lastrowid)

    def pending_purchases(self, created_before: Optional[float] = None,
                          guild_id: Optional[int] = None,
                          service_id: Optional[str] = None) -> List[sqlite3.Row]:
        """Offene Käufe eines Servers.

        ``guild_id`` grenzt gegen fremde Kunden ab, ``service_id`` gegen die
        anderen Server derselben Guild – sonst liefert Server A die Kaeufe von
        Server B aus, markiert sie als geliefert und der Kaeufer bekommt nichts.
        """
        q = "SELECT * FROM purchases WHERE status='pending'"
        args: Tuple = ()
        if created_before is not None:
            q += " AND created_at <= ?"
            args = (created_before,)
        if guild_id is None:
            # Ohne Guild gibt es nichts zurueckzugeben. Frueher lieferte der
            # Aufruf die offenen Kaeufe ALLER Kunden – ein Cleanup auf einem
            # Server markierte dann fremde Kaeufe als geliefert, obwohl die
            # Items dort nie gespawnt sind.
            return []
        q += " AND guild_id = ?"
        args = args + (int(guild_id),)
        if service_id is not None:
            # Alt-Kaeufe ohne service_id gehoeren dem Server, der sie damals
            # als einziger der Guild bedient hat – die Migration hat sie ihm
            # bereits zugeschrieben, leere Werte bleiben trotzdem sichtbar.
            q += " AND (service_id = ? OR service_id = '')"
            args = args + (str(service_id),)
        with self._lock:
            return list(self._conn.execute(q + " ORDER BY id", args).fetchall())

    def mark_delivered(self, ids: List[int]):
        if not ids:
            return
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "UPDATE purchases SET status='delivered', delivered_at=? WHERE id=?",
                [(now, i) for i in ids])
            self._conn.commit()

    # ── Casino-Historie ───────────────────────────────────────
    def log_casino(self, guild_id: int, user_id: int, game: str,
                   bet: int, payout: int, result: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO casino_history (guild_id, user_id, game, bet, payout, result, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (guild_id, user_id, game, bet, payout, result, time.time()))
            self._conn.commit()

    # ── Kill-Statistiken ──────────────────────────────────────
    def record_kill(self, service_id: str, killer_name: str, killer_id: Optional[str],
                    victim_name: str, victim_id: Optional[str],
                    weapon: Optional[str], distance: Any):
        try:
            dist: Optional[float] = float(str(distance).replace(",", "."))
        except (TypeError, ValueError):
            dist = None
        with self._lock:
            self._conn.execute(
                "INSERT INTO kills (service_id, created_at, killer_name, killer_id, "
                "victim_name, victim_id, weapon, distance) VALUES (?,?,?,?,?,?,?,?)",
                (str(service_id or ""), time.time(), killer_name, killer_id,
                 victim_name, victim_id, weapon, dist))
            self._conn.commit()

    def player_stats(self, service_id: str, name: str) -> Optional[Dict]:
        """Kills, Tode (PvP), Lieblingswaffe und weitester Kill eines Spielers
        auf **einem** Server."""
        sid = str(service_id or "")
        with self._lock:
            kills = int(self._conn.execute(
                "SELECT COUNT(*) AS n FROM kills "
                "WHERE service_id=? AND killer_name=? COLLATE NOCASE",
                (sid, name)).fetchone()["n"])
            deaths = int(self._conn.execute(
                "SELECT COUNT(*) AS n FROM kills "
                "WHERE service_id=? AND victim_name=? COLLATE NOCASE",
                (sid, name)).fetchone()["n"])
            if kills == 0 and deaths == 0:
                return None
            fav = self._conn.execute(
                "SELECT weapon, COUNT(*) AS n FROM kills "
                "WHERE service_id=? AND killer_name=? COLLATE NOCASE AND weapon IS NOT NULL "
                "AND weapon NOT IN ('', 'Unbekannt') "
                "GROUP BY weapon ORDER BY n DESC LIMIT 1", (sid, name)).fetchone()
            longest = self._conn.execute(
                "SELECT MAX(distance) AS d FROM kills "
                "WHERE service_id=? AND killer_name=? COLLATE NOCASE",
                (sid, name)).fetchone()["d"]
        return {
            "kills": kills, "deaths": deaths,
            "kd": (kills / deaths) if deaths else float(kills),
            "fav_weapon": fav["weapon"] if fav else None,
            "fav_weapon_kills": int(fav["n"]) if fav else 0,
            "longest": float(longest) if longest is not None else None,
        }

    def leaderboard(self, service_id: str, limit: int = 10) -> List[Dict]:
        sid = str(service_id or "")
        with self._lock:
            rows = self._conn.execute(
                "SELECT killer_name AS name, COUNT(*) AS kills, MAX(distance) AS best "
                "FROM kills WHERE service_id=? GROUP BY killer_name COLLATE NOCASE "
                "ORDER BY kills DESC, best DESC LIMIT ?", (sid, limit)).fetchall()
            out: List[Dict] = []
            for r in rows:
                deaths = int(self._conn.execute(
                    "SELECT COUNT(*) AS n FROM kills "
                    "WHERE service_id=? AND victim_name=? COLLATE NOCASE",
                    (sid, r["name"])).fetchone()["n"])
                out.append({"name": r["name"], "kills": int(r["kills"]), "deaths": deaths,
                            "kd": (int(r["kills"]) / deaths) if deaths else float(r["kills"]),
                            "best": float(r["best"]) if r["best"] is not None else None})
        return out

    def known_player_names(self, service_id: str, prefix: str = "",
                           limit: int = 25) -> List[str]:
        """Spielernamen aus Kills + Sitzungen **dieses** Servers (Autocomplete)."""
        like = f"%{prefix}%" if prefix else "%"
        sid = str(service_id or "")
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM ("
                "  SELECT killer_name AS name FROM kills WHERE service_id=?"
                "  UNION SELECT victim_name FROM kills WHERE service_id=?"
                "  UNION SELECT ingame_name FROM sessions WHERE service_id=?) "
                "WHERE name LIKE ? COLLATE NOCASE ORDER BY name LIMIT ?",
                (sid, sid, sid, like, limit)).fetchall()
        return [r["name"] for r in rows if r["name"]]

    # ── /link: Discord ↔ Ingame-Name ──────────────────────────
    def link_user(self, guild_id: int, user_id: int, ingame_name: str) -> Tuple[bool, str]:
        """Verknüpft einen Discord-User mit einem Ingame-Namen (pro Guild eindeutig)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id FROM links WHERE guild_id=? AND ingame_name=?",
                (guild_id, ingame_name)).fetchone()
            if row and int(row["user_id"]) != user_id:
                return False, "name_taken"
            self._conn.execute(
                "INSERT OR REPLACE INTO links (guild_id, user_id, ingame_name, ingame_id, created_at) "
                "VALUES (?,?,?,?,?)",
                (guild_id, user_id, ingame_name, None, time.time()))
            self._conn.commit()
        return True, "ok"

    def unlink_user(self, guild_id: int, user_id: int) -> Optional[str]:
        """Entfernt die Verknüpfung; gibt den bisherigen Ingame-Namen zurück."""
        with self._lock:
            row = self._conn.execute(
                "SELECT ingame_name FROM links WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)).fetchone()
            if not row:
                return None
            self._conn.execute("DELETE FROM links WHERE guild_id=? AND user_id=?",
                               (guild_id, user_id))
            self._conn.commit()
        return row["ingame_name"]

    def get_link_by_user(self, guild_id: int, user_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM links WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)).fetchone()

    def links_for_name(self, ingame_name: str,
                       guild_id: Optional[int] = None) -> List[sqlite3.Row]:
        """Verknüpfungen zu einem Ingame-Namen (case-insensitive).

        Mit ``guild_id`` nur die dieser Guild – sonst bekaeme ein gleichnamiger
        Spieler auf einem fremden Server die Belohnung ausgezahlt.
        """
        with self._lock:
            if guild_id is None:
                return list(self._conn.execute(
                    "SELECT * FROM links WHERE ingame_name=? COLLATE NOCASE",
                    (ingame_name,)).fetchall())
            return list(self._conn.execute(
                "SELECT * FROM links WHERE guild_id=? AND ingame_name=? COLLATE NOCASE",
                (int(guild_id), ingame_name)).fetchall())

    def update_link_id(self, ingame_name: str, ingame_id: str,
                       guild_id: Optional[int] = None):
        """Trägt die im Log gesehene Ingame-ID zum verlinkten Namen nach –
        mit ``guild_id`` nur in der Guild des Servers, von dem das Log stammt."""
        if not ingame_id:
            return
        with self._lock:
            if guild_id is None:
                self._conn.execute(
                    "UPDATE links SET ingame_id=? WHERE ingame_name=? COLLATE NOCASE "
                    "AND (ingame_id IS NULL OR ingame_id != ?)",
                    (ingame_id, ingame_name, ingame_id))
            else:
                self._conn.execute(
                    "UPDATE links SET ingame_id=? WHERE guild_id=? AND "
                    "ingame_name=? COLLATE NOCASE "
                    "AND (ingame_id IS NULL OR ingame_id != ?)",
                    (ingame_id, int(guild_id), ingame_name, ingame_id))
            self._conn.commit()

    def list_links(self, guild_id: int) -> List[sqlite3.Row]:
        """Alle Verknüpfungen einer Guild, alphabetisch nach PSN-Name."""
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM links WHERE guild_id=? ORDER BY ingame_name COLLATE NOCASE",
                (guild_id,)).fetchall())

    def has_session(self, service_id: str, ingame_name: str) -> bool:
        """True, wenn für den Spieler auf DIESEM Server eine Sitzung offen ist."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sessions WHERE service_id=? AND ingame_name=? COLLATE NOCASE",
                (str(service_id or ""), ingame_name)).fetchone()
        return row is not None

    def sync_sessions_from_positions(self, service_id: str, positions: Dict,
                                     max_age_seconds: int = 300,
                                     guild_id: Optional[int] = None) -> int:
        """Öffnet Sitzungen für VERLINKTE Spieler, die laut Log-Positions-Tracking
        gerade aktiv sind, aber keine offene Sitzung haben (verpasstes Connect-Event
        durch Bot-Downtime/Backlog-Skip oder /link während man schon online ist).
        Gibt die Anzahl neu geöffneter Sitzungen zurück."""
        now_utc = datetime.now(timezone.utc)
        with self._lock:
            if guild_id is None:
                rows = self._conn.execute(
                    "SELECT DISTINCT ingame_name FROM links").fetchall()
            else:
                # Nur Verknuepfungen DIESER Guild – sonst entstehen auf Server A
                # Geister-Sitzungen fuer Namen, die nur bei Kunde B verlinkt sind.
                rows = self._conn.execute(
                    "SELECT DISTINCT ingame_name FROM links WHERE guild_id=?",
                    (int(guild_id),)).fetchall()
            linked = {str(r["ingame_name"]).lower() for r in rows}
        opened = 0
        for pname, info in list(positions.items()):
            if pname.lower() not in linked:
                continue
            try:
                seen = datetime.fromisoformat(str(info.get("last_seen", "")))
            except ValueError:
                continue
            if (now_utc - seen).total_seconds() > max_age_seconds:
                continue
            if self.has_session(service_id, pname):
                continue
            self.open_session(service_id, pname, info.get("id"))
            opened += 1
            log.info(f"[PLAYTIME] Sitzung für {pname} aus Log-Sichtung geöffnet (Connect-Event verpasst).")
        return opened

    # ── Bounties (Kopfgelder) ─────────────────────────────────
    def add_bounty(self, guild_id: int, target_name: str, amount: int, placed_by: int) -> int:
        """Setzt ein Kopfgeld aus (Betrag wurde bereits abgebucht).
        Gibt die neue Gesamtsumme auf das Ziel zurück."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO bounties (guild_id, target_name, amount, placed_by, created_at) "
                "VALUES (?,?,?,?,?)",
                (guild_id, target_name, amount, placed_by, time.time()))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS total FROM bounties "
                "WHERE guild_id=? AND target_name=? COLLATE NOCASE AND status='open'",
                (guild_id, target_name)).fetchone()
        return int(row["total"])

    def open_bounties(self, guild_id: int) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT target_name, SUM(amount) AS total, COUNT(*) AS n "
                "FROM bounties WHERE guild_id=? AND status='open' "
                "GROUP BY target_name COLLATE NOCASE ORDER BY total DESC",
                (guild_id,)).fetchall())

    def claim_bounties(self, guild_id: int, target_name: str, claimed_by: int) -> int:
        """Zahlt alle offenen Kopfgelder auf target_name aus (markiert claimed).
        Gibt die Gesamtsumme zurück (0 = keine offenen Bounties)."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS total FROM bounties "
                "WHERE guild_id=? AND target_name=? COLLATE NOCASE AND status='open'",
                (guild_id, target_name)).fetchone()
            total = int(row["total"])
            if total > 0:
                self._conn.execute(
                    "UPDATE bounties SET status='claimed', claimed_by=?, claimed_at=? "
                    "WHERE guild_id=? AND target_name=? COLLATE NOCASE AND status='open'",
                    (claimed_by, now, guild_id, target_name))
                self._conn.commit()
        return total

    # ── Spielzeit-Sitzungen ───────────────────────────────────
    def open_session(self, service_id: str, ingame_name: str, ingame_id: Optional[str]):
        """Connect-Event: neue Sitzung (Reconnect setzt den Zähler zurück)."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(service_id, ingame_name, ingame_id, connect_ts, last_seen_ts, credited_blocks) "
                "VALUES (?,?,?,?,?,0)",
                (str(service_id or ""), ingame_name, ingame_id, now, now))
            self._conn.commit()

    def close_session(self, service_id: str, ingame_name: str):
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE service_id=? AND ingame_name=? COLLATE NOCASE",
                (str(service_id or ""), ingame_name))
            self._conn.commit()

    def close_all_sessions(self, service_id: str):
        """Alle offenen Sitzungen EINES Servers beenden (Server-Neustart).

        Ohne die Einschraenkung wuerde ein Neustart bei einem Kunden die
        Spielzeit-Sitzungen aller anderen Kunden mitloeschen.
        """
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE service_id=?",
                               (str(service_id or ""),))
            self._conn.commit()

    def playtime_credits_due(self, service_id: str, interval_seconds: int) -> List[Dict]:
        """Berechnet pro offener Sitzung dieses Servers neu fällige Spielzeit-
        Blöcke und schreibt credited_blocks fort. Gibt [{name, blocks}] zurück."""
        now = time.time()
        sid = str(service_id or "")
        out: List[Dict] = []
        with self._lock:
            rows = self._conn.execute("SELECT * FROM sessions WHERE service_id=?",
                                      (sid,)).fetchall()
            for r in rows:
                total = int((now - float(r["connect_ts"])) // max(60, interval_seconds))
                due = total - int(r["credited_blocks"])
                if due > 0:
                    self._conn.execute(
                        "UPDATE sessions SET credited_blocks=?, last_seen_ts=? "
                        "WHERE service_id=? AND ingame_name=?",
                        (total, now, sid, r["ingame_name"]))
                    out.append({"name": r["ingame_name"], "blocks": due})
            if out:
                self._conn.commit()
        return out

    # ── Backup ────────────────────────────────────────────────
    def backup(self, keep: int = 7) -> Optional[str]:
        """Konsistente Kopie via SQLite-Backup-API (WAL-sicher):
        economy.db.bak-YYYY-MM-DD; behält die neuesten `keep` Stück."""
        path = self._path or str(cfg.config.get("economy_db_path") or ECON_DB_FILE)
        dest = f"{path}.bak-{datetime.now().strftime('%Y-%m-%d')}"
        try:
            with self._lock:
                dst = sqlite3.connect(dest)
                try:
                    self._conn.backup(dst)
                finally:
                    dst.close()
            for old in sorted(glob.glob(f"{path}.bak-*"))[:-max(1, keep)]:
                try:
                    os.remove(old)
                except OSError:
                    pass
            return dest
        except Exception as e:
            log.error(f"[ECON] Backup fehlgeschlagen: {e}")
            return None


db = EconomyDB()


# ══════════════════════════════════════════════════════════════
#  Economy-Hilfsfunktionen
# ══════════════════════════════════════════════════════════════
def _srv_conf(interaction: discord.Interaction, key: str) -> Dict:
    """Einstellungsblock (economy/casino/bounty) des Servers dieser Guild.

    Jeder Kunde stellt Verdienstspannen, Cooldowns und Einsaetze selbst ein;
    ohne diese Aufloesung gaelten ueberall die Werte des Betreibers.
    """
    conn = _conn_of(interaction)
    wert = (conn.get(key) if conn is not None else cfg.config.get(key))
    return wert if isinstance(wert, dict) else {}


def _cur_symbol(conn: Optional[ServerConnection] = None) -> str:
    """Waehrungssymbol des gerade behandelten Servers."""
    conn = conn if conn is not None else _AKTUELLER_SERVER.get()
    if conn is not None:
        return str(conn.get("currency_symbol", "₽") or "₽")
    return cfg.config.get("currency_symbol", "₽")

def _fmt_money(n: int, conn: Optional[ServerConnection] = None) -> str:
    return f"{int(n):,} {_cur_symbol(conn)}"

def _cooldown_embed(action_label: str, remaining: float) -> discord.Embed:
    """Embed mit Discord-Relativzeit, wann der Befehl wieder nutzbar ist."""
    ready = int(time.time() + remaining)
    return discord.Embed(
        title="⏳ Cooldown",
        description=f"You can use **{action_label}** again <t:{ready}:R>.",
        color=0x95A5A6)

def _insufficient_embed(needed: int, wallet: int) -> discord.Embed:
    return discord.Embed(
        title="❌ Insufficient funds",
        description=(f"You need **{_fmt_money(needed)}** but your wallet only has "
                     f"**{_fmt_money(wallet)}**.\nUse `/withdraw` to move money from your bank."),
        color=0xE74C3C)

def _validate_bet(bet: int, conf: Dict) -> Optional[str]:
    """Gibt eine Fehlermeldung zurück, wenn der Einsatz außerhalb min/max liegt."""
    mn = int(conf.get("min_bet", 1))
    mx = int(conf.get("max_bet", 10 ** 9))
    if bet < mn:
        return f"Minimum bet is **{_fmt_money(mn)}**."
    if bet > mx:
        return f"Maximum bet is **{_fmt_money(mx)}**."
    return None

async def _post_feed(guild_id: Optional[int], log_type: str, embed: discord.Embed,
                     content: Optional[str] = None, channel_id: Optional[int] = None,
                     service_id: Optional[str] = None):
    """Postet ein Embed in den konfigurierten Feed-Channel (eine Guild oder alle).
    content: optionaler Nachrichtentext vor dem Embed (z. B. Rollen-Ping bei Zonen).
    channel_id: optionaler Ziel-Channel, der die Feed-Konfiguration überschreibt
    (z. B. eigener Warn-Channel einer Zone).
    service_id: von welchem Nitrado-Server das Ereignis stammt – entscheidet bei
    mehreren Servern derselben Guild, in welchen Channel es geht."""
    async def _send(ch_id: int, tag: str):
        ch = await bot._resolve_channel(int(ch_id))
        if not ch:
            return
        try:
            if content:
                await ch.send(content=content, embed=embed,
                              allowed_mentions=discord.AllowedMentions(roles=True))
            else:
                await ch.send(embed=embed)
        except Exception as e:
            log.error(f"[FEED] {tag}: {e}")

    if channel_id:
        await _send(channel_id, f"{log_type} → Channel {channel_id}")
        return
    # Rückfallkette wie in _dispatch: der erste Schlüssel mit gesetztem Channel
    # gewinnt. Ohne sie liefen die Betriebswarnungen ins Leere – sie posten
    # historisch auf "adminlog", das es in FEED_TYPES nicht mehr gibt und das
    # die Migration entfernt. FTP-Ausfall, übersprungener Rückstand,
    # Zonen-Rückfall und Link-Meldungen blieben damit stumm, obwohl der
    # Betreiber den sichtbaren Feed „Admin Action" eingerichtet hatte.
    kandidaten = [log_type]
    for ersatz in (_FEED_ALIASSE.get(log_type), "catch_all"):
        if ersatz and ersatz not in kandidaten:
            kandidaten.append(ersatz)
    gids = [str(guild_id)] if guild_id else list(cfg.guilds.keys())
    for gid in gids:
        ch_id = None
        treffer = log_type
        for kand in kandidaten:
            ch_id = cfg.get_channel(int(gid), kand, service_id)
            if ch_id:
                treffer = kand
                break
        if not ch_id:
            continue
        await _send(ch_id, f"{treffer} → Guild {gid}")


async def _notify_link_change(guild_id: Optional[int], embed: discord.Embed):
    """Meldet /link- und /unlink-Aktionen an die Admins:
    bevorzugt im adminlog-Feed, sonst im economy_log-Feed.

    Verknuepfungen gehoeren der Guild, nicht einem einzelnen Server – als
    Zielkanal gilt deshalb der des Leitservers.
    """
    _leit = connections.for_guild(guild_id) if guild_id else None
    sid = _leit.service_id if _leit is not None else None
    if guild_id and (cfg.get_channel(int(guild_id), "admin_action", sid)
                     or cfg.get_channel(int(guild_id), "adminlog", sid)):
        return await _post_feed(guild_id, "adminlog", embed, service_id=sid)
    await _post_feed(guild_id, "economy_log", embed, service_id=sid)


# ══════════════════════════════════════════════════════════════
#  SHOP-MANAGER – Auslieferung über cfgEffectArea.json
#  Ablauf: Kauf → Eintrag in cfgEffectArea.json (pending) →
#  Server-Neustart (Item spawnt) → Eintrag entfernen (delivered).
#  WICHTIG: Ohne Entfernen respawnt das Item bei JEDEM Neustart!
# ══════════════════════════════════════════════════════════════
class ShopManager:
    AREA_PREFIX = "SHOP_"

    def __init__(self, bot_ref: "DayZBot", conn: "ServerConnection"):
        self.bot  = bot_ref
        # Jeder Server liefert in seine eigene cfgEffectArea.json aus und wird
        # ueber seine eigene Nitrado-Verbindung neu gestartet.
        self.conn = conn
        self.lock = asyncio.Lock()   # serialisiert ALLE Schreibzugriffe auf die Datei
        self._restart_task: Optional[asyncio.Task] = None
        self._last_restart_ts = 0.0
        self._cleanup_task: Optional[asyncio.Task] = None
        self.cleanup_retry_needed = False   # FTP-Fehler beim Cleanup → Retry im Poll-Zyklus
        self._last_restart_at = 0.0         # Zeitpunkt des zuletzt ERKANNTEN Server-Neustarts

    # ── Cleanup als Task starten (Referenz halten, Fehler loggen) ─
    def spawn_cleanup(self, delayed: bool = False):
        """Startet on_restart_detected als Task – nie fire-and-forget.
        delayed=True bei frisch erkanntem Neustart: die SHOP_-Einträge bleiben
        in der Datei, bis der Server per A2S wieder online ist (= Boot fertig,
        cfgEffectArea.json sicher eingelesen), und werden dann sofort entfernt.
        Ist der Online-Status nicht prüfbar, greift stattdessen der feste
        delivery_cleanup_delay_seconds-Fallback."""
        if delayed:
            self._last_restart_at = time.time()
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_safe(delayed))

    async def _cleanup_safe(self, delayed: bool = False):
        try:
            await self.on_restart_detected(delayed)
        except Exception as e:
            log.error(f"[SHOP] Delivery-Cleanup fehlgeschlagen: {e}")
            self.cleanup_retry_needed = True

    # ── Pfad zur cfgEffectArea.json ──────────────────────────
    def effect_area_path(self) -> Optional[str]:
        path = self.conn.get("cfg_effect_area_path")
        if path:
            return path
        mission = self.conn.get("ftp_mission_dir")
        if mission:
            return f"{mission.rstrip('/')}/cfgEffectArea.json"
        return None

    # ── JSON parsen (Areas-Key dynamisch, leere Datei ok) ─────
    @staticmethod
    def _parse_effect_area(raw: Optional[str]) -> Tuple[Dict, str]:
        """Gibt (Daten, Areas-Key) zurück. Fehlende/leere Datei → Grundstruktur."""
        if not raw or not raw.strip():
            return {"Areas": [], "SafePositions": []}, "Areas"
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Wurzel-Element ist kein JSON-Objekt")
        # 1) Key 'Areas' (Groß-/Kleinschreibung egal)
        for key, val in data.items():
            if key.lower() == "areas" and isinstance(val, list):
                return data, key
        # 2) Fallback: irgendeine Liste, deren Einträge wie Areas aussehen
        for key, val in data.items():
            if isinstance(val, list) and val and isinstance(val[0], dict) and "AreaName" in val[0]:
                return data, key
        data.setdefault("Areas", [])
        return data, "Areas"

    async def _write_json(self, path: str, new_data: Dict) -> bool:
        """Schreibt die cfgEffectArea.json – OHNE Zusatzdateien im Mission-Ordner.
        Eine evtl. noch vorhandene .bak aus früheren Bot-Versionen wird entfernt."""
        loop = asyncio.get_running_loop()
        content = json.dumps(new_data, ensure_ascii=False, indent=2)
        ok = await loop.run_in_executor(None, self.conn.ftp.write_file, path, content)
        if ok:
            # Aufräumen (Best-Effort): keine .bak mehr im Mission-Ordner
            await loop.run_in_executor(None, self.conn.ftp.delete_file, path + ".bak")
        return ok

    # ── Kauf: Einträge anhängen ───────────────────────────────
    async def add_purchase_entries(self, classnames: List[str], amount: int,
                                   x: float, y: float, z: float) -> Tuple[bool, str, List[str]]:
        """Schreibt pro Stück und Classname einen Area-Eintrag (Pos=[X, Höhe, Nord]) –
        Bundles spawnen alle enthaltenen Items an derselben Koordinate.
        Gibt (ok, fehlermeldung, area_names) zurück. Erst NACH Erfolg Geld abbuchen!"""
        path = self.effect_area_path()
        if not path:
            return (False,
                    "cfgEffectArea.json path is not configured. "
                    "Run `/ftp_scan` or set `cfg_effect_area_path` in config.json.", [])
        async with self.lock:
            loop = asyncio.get_running_loop()
            raw, status = await loop.run_in_executor(None, self.conn.ftp.read_file_ex, path)
            if status == "error":
                # Inhalt unbekannt → NIE mit leerer Grundstruktur überschreiben,
                # sonst gehen Vanilla-Zonen + andere pending-Käufe verloren
                return (False,
                        "FTP read of `cfgEffectArea.json` failed – purchase cancelled, "
                        "nothing was charged. Please try again in a moment.", [])
            try:
                data, areas_key = self._parse_effect_area(raw)
            except Exception as e:
                return False, f"Could not parse cfgEffectArea.json: `{e}`", []
            try:
                radius = float(self.conn.get("default_radius", 1) or 1)
            except (TypeError, ValueError):
                radius = 1.0
            if radius.is_integer():
                radius = int(radius)   # "Radius": 1 statt 1.0 – exakt wie das Referenz-Format
            names: List[str] = []
            for _ in range(amount):
                for cn in classnames:
                    name = f"{self.AREA_PREFIX}{uuid.uuid4().hex}"
                    names.append(name)
                    data[areas_key].append({
                        "AreaName": name,
                        "Type": cn,
                        "Data": {"Pos": [float(x), float(y), float(z)], "Radius": radius},
                    })
            ok = await self._write_json(path, data)
            if not ok:
                return False, "FTP write failed – purchase cancelled, nothing was charged.", []
            return True, "", names

    # ── Cleanup: Einträge nach Auslieferung entfernen ─────────
    async def remove_area_entries(self, area_names: List[str]) -> bool:
        """Entfernt Einträge aus cfgEffectArea.json (verhindert Respawn bei jedem Neustart)."""
        if not area_names:
            return True   # nichts zu entfernen → Erfolg (sonst hängen Käufe ewig auf pending)
        path = self.effect_area_path()
        if not path:
            return False
        wanted = set(area_names)
        async with self.lock:
            loop = asyncio.get_running_loop()
            raw, status = await loop.run_in_executor(None, self.conn.ftp.read_file_ex, path)
            if status == "error":
                # Lesefehler ≠ leere Datei: sonst würden Käufe als geliefert
                # markiert, obwohl die Einträge noch drinstehen (Dauer-Respawn)
                log.error("[SHOP] Cleanup: cfgEffectArea.json nicht lesbar (FTP) – Retry folgt.")
                return False
            try:
                data, areas_key = self._parse_effect_area(raw)
            except Exception as e:
                log.error(f"[SHOP] Cleanup: Parse-Fehler in cfgEffectArea.json: {e}")
                return False
            before = len(data[areas_key])
            data[areas_key] = [a for a in data[areas_key]
                               if a.get("AreaName") not in wanted]
            if len(data[areas_key]) == before:
                return True   # nichts (mehr) enthalten → trotzdem Erfolg
            return await self._write_json(path, data)

    # ── Verwaiste SHOP_-Einträge entfernen (Selbstheilung) ────
    async def sweep_orphans(self) -> int:
        """Entfernt alle SHOP_-Einträge, die zu KEINEM pending-Kauf gehören
        (entstehen z. B. durch fehlgeschlagenen Rollback). Gibt die Anzahl
        entfernter Einträge zurück, -1 bei FTP-/Parse-Fehler."""
        path = self.effect_area_path()
        if not path:
            return -1
        valid: set = set()
        for r in db.pending_purchases(guild_id=self.conn.guild_id,
                                      service_id=self.conn.service_id):
            try:
                valid.update(json.loads(r["area_names"] or "[]"))
            except Exception:
                pass
        async with self.lock:
            loop = asyncio.get_running_loop()
            raw, status = await loop.run_in_executor(None, self.conn.ftp.read_file_ex, path)
            if status == "error":
                log.error("[SHOP] Orphan-Sweep: cfgEffectArea.json nicht lesbar (FTP).")
                return -1
            try:
                data, areas_key = self._parse_effect_area(raw)
            except Exception as e:
                log.error(f"[SHOP] Orphan-Sweep: Parse-Fehler in cfgEffectArea.json: {e}")
                return -1
            keep: List[Dict] = []
            removed = 0
            for a in data[areas_key]:
                name = str(a.get("AreaName", ""))
                if name.startswith(self.AREA_PREFIX) and name not in valid:
                    removed += 1
                else:
                    keep.append(a)
            if removed == 0:
                return 0
            data[areas_key] = keep
            ok = await self._write_json(path, data)
            return removed if ok else -1

    # ── Diagnose + Self-Heal (Basis für /shop check) ──────────
    async def check_and_heal(self) -> Dict:
        """Prüft Pfad, Lesbarkeit und JSON-Struktur der cfgEffectArea.json und
        trägt fehlende Einträge offener Käufe wieder ein (Self-Heal, z. B.
        nachdem die Datei extern überschrieben wurde). Gibt einen Report zurück."""
        report: Dict = {"path": self.effect_area_path(),
                        "last_restart_at": self._last_restart_at}
        path = report["path"]
        if not path:
            report["status"] = "no_path"
            return report
        pending = db.pending_purchases(guild_id=self.conn.guild_id,
                                       service_id=self.conn.service_id)
        report["pending"] = len(pending)
        async with self.lock:
            loop = asyncio.get_running_loop()
            raw, status = await loop.run_in_executor(None, self.conn.ftp.read_file_ex, path)
            report["status"] = status
            if status == "error":
                return report
            try:
                data, areas_key = self._parse_effect_area(raw)
            except Exception as e:
                report["status"] = "parse_error"
                report["error"] = str(e)
                return report
            areas = data[areas_key]
            present = {str(a.get("AreaName", "")) for a in areas}
            shop_n = sum(1 for n in present if n.startswith(self.AREA_PREFIX))
            report["areas_total"]     = len(areas)
            report["shop_entries"]    = shop_n
            report["vanilla_entries"] = len(areas) - shop_n
            try:
                radius = float(self.conn.get("default_radius", 1) or 1)
            except (TypeError, ValueError):
                radius = 1.0
            if radius.is_integer():
                radius = int(radius)
            healed_ids: List[int] = []
            healed_entries = 0
            for r in pending:
                try:
                    names = json.loads(r["area_names"] or "[]")
                except Exception:
                    continue
                cls_list = [c for c in str(r["classname"] or "").split("+") if c]
                if not cls_list or all(n in present for n in names):
                    continue
                # area_names wurden in der Reihenfolge Stück×Classname erzeugt →
                # Index-Mapping stellt den Classname jedes Eintrags wieder her
                for i, n in enumerate(names):
                    if n in present:
                        continue
                    areas.append({
                        "AreaName": n,
                        "Type": cls_list[i % len(cls_list)],
                        "Data": {"Pos": [float(r["x"]), float(r["y"]), float(r["z"])],
                                 "Radius": radius},
                    })
                    present.add(n)
                    healed_entries += 1
                healed_ids.append(int(r["id"]))
            report["healed_purchases"] = healed_ids
            report["healed_entries"]   = healed_entries
            if healed_entries:
                report["heal_written"] = await self._write_json(path, data)
        return report

    # ── Auto-Restart (entprellt) ──────────────────────────────
    def schedule_auto_restart(self):
        """Startet den Restart-Timer, falls noch keiner läuft.
        Käufe innerhalb restart_cooldown_seconds werden gesammelt."""
        if self._restart_task and not self._restart_task.done():
            return
        self._restart_task = asyncio.create_task(self._restart_worker())

    async def _restart_worker(self):
        delay = max(5, int(self.conn.get("restart_cooldown_seconds", 300) or 300))
        # Mindestabstand zum vorherigen Auto-Restart erzwingen (Server bootet evtl. noch)
        wait = max(delay, (self._last_restart_ts + delay) - time.time())
        log.info(f"[SHOP] Auto-Restart in {int(wait)}s geplant (Käufe werden gesammelt).")
        await asyncio.sleep(wait)
        self._last_restart_ts = time.time()
        try:
            ok, msg = await self.conn.api.restart()
            log.info(f"[SHOP] Auto-Restart nach Kauf ausgelöst: ok={ok} – {msg}")
        except Exception as e:
            log.error(f"[SHOP] Auto-Restart fehlgeschlagen: {e}")

    # ── Warten bis der Server wieder online ist (A2S) ─────────
    async def _wait_for_server_online(self) -> bool:
        """Pollt den Spielserver per A2S, bis er antwortet (= wirklich online).
        True = online gesehen. False = server_ip/query_port fehlt oder Timeout
        (delivery_online_wait_max_seconds) – dann greift der feste Delay als
        Fallback, sonst würden Items bei falschem Query-Port ewig respawnen."""
        ip = str(self.conn.get("server_ip") or "").split(":")[0].strip()
        qport = int(self.conn.get("query_port", 0) or 0)
        if not ip or not qport:
            log.warning("[SHOP] server_ip/query_port nicht gesetzt – kann Server-online "
                        "nicht prüfen, nutze festen Delivery-Delay als Fallback.")
            return False
        max_wait = max(60, int(self.conn.get("delivery_online_wait_max_seconds", 2700) or 2700))
        deadline = time.time() + max_wait
        loop = asyncio.get_running_loop()
        while time.time() < deadline:
            info = await loop.run_in_executor(None, a2s_query, ip, qport)
            if info:
                return True
            await asyncio.sleep(20)
        log.warning(f"[SHOP] Server nach {max_wait // 60} Min nicht per A2S erreichbar – "
                    "nutze festen Delivery-Delay als Fallback.")
        return False

    # ── Neustart erkannt (neue ADM-Datei) → ausliefern ────────
    async def on_restart_detected(self, delayed: bool = False):
        """Wird vom Log-Poller nach einem erkannten Server-Neustart aufgerufen.
        Wartet bei delayed, bis der Server per A2S wieder online ist – die neue ADM
        erscheint früh im Boot, der Server muss die cfgEffectArea.json aber erst
        vollständig einlesen (zu frühes Entfernen = Items spawnen nie). Antwortet
        der Server per A2S, ist die Mission geladen → sofort bereinigen. Nur wenn
        der Online-Status nicht prüfbar ist, greift delivery_cleanup_delay_seconds
        als fester Fallback-Delay. Danach werden hinreichend alte pending-Käufe
        geliefert und die Datei bereinigt."""
        self.cleanup_retry_needed = False
        grace = int(self.conn.get("delivery_grace_seconds", 90) or 90)
        poll  = int(self.conn.get("log_poll_interval_seconds", 10) or 10)
        # Grace muss über dem Poll-Intervall liegen, sonst könnte ein Kauf, der NACH
        # dem Restart einging, fälschlich als geliefert gelten (bezahlt, nie gespawnt)
        grace = max(grace, poll + 30)
        # Cutoff am ERKENNUNGS-Zeitpunkt festmachen: Käufe, die während der
        # Wartezeit oder eines Retrys eingehen, sind noch nicht gespawnt und
        # dürfen nicht als geliefert markiert werden
        restart_at = self._last_restart_at or time.time()
        cutoff = restart_at - grace
        rows = db.pending_purchases(created_before=cutoff,
                                    guild_id=self.conn.guild_id,
                                    service_id=self.conn.service_id)
        if not rows:
            return
        if delayed:
            delay = max(0, int(self.conn.get("delivery_cleanup_delay_seconds", 600) or 600))
            log.info(f"[SHOP] Server-Neustart erkannt – warte bis der Server wieder online "
                     f"ist, danach werden {len(rows)} Lieferung(en) sofort abgeschlossen.")
            while True:
                seen = self._last_restart_at   # Stand vor dem Warten
                online = await self._wait_for_server_online()
                if online:
                    # Server antwortet per A2S → Mission (inkl. cfgEffectArea.json)
                    # ist geladen, Items sind gespawnt → Einträge sofort entfernen
                    log.info("[SHOP] Server ist wieder online – SHOP_-Einträge werden "
                             "jetzt sofort entfernt.")
                elif delay:
                    # Online-Status nicht prüfbar (keine server_ip/query_port oder
                    # A2S-Timeout) → fester Delay als Sicherheits-Fallback, sonst
                    # könnten die Einträge entfernt werden, bevor der Server die
                    # Datei eingelesen hat (Item spawnt nie)
                    log.info(f"[SHOP] Server-online nicht prüfbar – Fallback: warte "
                             f"{delay // 60} Min festen Delay vor dem Entfernen.")
                    await asyncio.sleep(delay)
                # Neuer Restart während des Wartens erkannt? spawn_cleanup startet
                # keinen zweiten Task, solange dieser läuft → hier von vorn warten,
                # sonst würden die Einträge mitten im nächsten Boot entfernt.
                if self._last_restart_at <= seen:
                    break
                log.info("[SHOP] Erneuter Server-Neustart während der Wartezeit erkannt – "
                         "warte erneut auf Server-online.")
        ids:   List[int] = []
        names: List[str] = []
        for r in rows:
            ids.append(int(r["id"]))
            try:
                names.extend(json.loads(r["area_names"] or "[]"))
            except Exception:
                pass
        log.info(f"[SHOP] Liefere {len(ids)} Kauf/Käufe aus (Einträge werden entfernt).")
        ok = await self.remove_area_entries(names)
        if not ok:
            self.cleanup_retry_needed = True
            log.error("[SHOP] cfgEffectArea.json konnte nicht bereinigt werden – "
                      "automatischer neuer Versuch beim nächsten Poll-Zyklus.")
            for warn_gid in {int(r["guild_id"]) for r in rows}:
                warn = discord.Embed(
                    title="⚠️ Delivery cleanup failed",
                    description=("Could not remove delivered `SHOP_` entries from "
                                 "`cfgEffectArea.json` (FTP error). Items would respawn on "
                                 "every restart. The bot retries automatically – admins can "
                                 "also run `/shop cleanup`."),
                    color=0xE67E22)
                await _post_feed(warn_gid, "shop_log", warn,
                                 service_id=self.conn.service_id)
            return
        db.mark_delivered(ids)
        for r in rows:
            embed = discord.Embed(
                title="📦 DELIVERED",
                description=(f"**{r['amount']}× {r['item_name']}** for <@{r['user_id']}> "
                             f"spawned after the server restart."),
                color=0x2ECC71)
            embed.set_footer(text=f"Purchase #{r['id']}")
            await _post_feed(int(r["guild_id"]), "shop_log", embed,
                             service_id=self.conn.service_id)


# ══════════════════════════════════════════════════════════════
#  ECONOMY-COMMANDS – /work /daily /beg
# ══════════════════════════════════════════════════════════════
WORK_FLAVOR = [
    "You fixed a stranger's car engine and earned {amount}.",
    "You chopped firewood for a trader camp – {amount} earned.",
    "You escorted a fresh spawn safely across the map and got {amount}.",
    "You sold hand-made fishing rods at the market for {amount}.",
    "You repaired the town water pump – the mayor paid you {amount}.",
    "You hunted deer and sold the pelts for {amount}.",
    "You cleared the zombies off a farm – the owner paid {amount}.",
    "You worked a night shift at the docks and earned {amount}.",
    "You guided a group through the military zone and were paid {amount}.",
    "You patched up bullet wounds as a field medic – {amount} earned.",
]

BEG_SUCCESS = [
    "A kind survivor tossed you {amount}.",
    "You found {amount} in an old jacket by the road.",
    "A trader felt sorry for you and gave you {amount}.",
    "Someone left {amount} in a rusty can – lucky you.",
]

BEG_FAIL = [
    "People just walked past you. Nothing earned.",
    "A zombie chased you away before anyone could help.",
    "You got laughed at. No money this time.",
    "Someone threw a rotten fruit at you instead of money.",
]

async def _require_guild(interaction: discord.Interaction) -> bool:
    """Economy/Shop funktionieren nur in einer Guild (Salden sind pro Guild)."""
    if interaction.guild_id:
        return True
    await interaction.response.send_message(
        "❌ This command only works inside a server.", ephemeral=True)
    return False


@bot.tree.command(name="work", description="💼 Work a job and earn some money")
async def cmd_work(interaction: discord.Interaction):
    if not await _require_guild(interaction):
        return
    conf = _srv_conf(interaction, "economy").get("work", {})
    gid, uid = interaction.guild_id, interaction.user.id

    remaining = db.cooldown_remaining(gid, uid, "work")
    if remaining > 0:
        return await interaction.response.send_message(
            embed=_cooldown_embed("/work", remaining), ephemeral=True)

    lo, hi = int(conf.get("min", 50)), int(conf.get("max", 150))
    amount = random.randint(min(lo, hi), max(lo, hi))
    wallet, _bank = db.add_wallet(gid, uid, amount)
    db.set_cooldown(gid, uid, "work", int(conf.get("cooldown_seconds", 3600)))

    embed = discord.Embed(
        title="💼 Work complete",
        description=random.choice(WORK_FLAVOR).format(amount=f"**{_fmt_money(amount)}**"),
        color=0x2ECC71)
    embed.set_footer(text=f"Wallet: {_fmt_money(wallet)}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="daily", description="📅 Claim your daily bonus")
async def cmd_daily(interaction: discord.Interaction):
    if not await _require_guild(interaction):
        return
    conf = _srv_conf(interaction, "economy").get("daily", {})
    gid, uid = interaction.guild_id, interaction.user.id

    remaining = db.cooldown_remaining(gid, uid, "daily")
    if remaining > 0:
        return await interaction.response.send_message(
            embed=_cooldown_embed("/daily", remaining), ephemeral=True)

    # amount fest ODER min/max-Bereich – beides erlaubt
    if "min" in conf and "max" in conf:
        lo, hi = int(conf["min"]), int(conf["max"])
        amount = random.randint(min(lo, hi), max(lo, hi))
    else:
        amount = int(conf.get("amount", 300))
    wallet, _bank = db.add_wallet(gid, uid, amount)
    db.set_cooldown(gid, uid, "daily", int(conf.get("cooldown_seconds", 86400)))

    embed = discord.Embed(
        title="📅 Daily bonus",
        description=f"You claimed your daily bonus of **{_fmt_money(amount)}**!",
        color=0x2ECC71)
    embed.set_footer(text=f"Wallet: {_fmt_money(wallet)}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="beg", description="🥺 Beg for a little money – might fail")
async def cmd_beg(interaction: discord.Interaction):
    if not await _require_guild(interaction):
        return
    conf = _srv_conf(interaction, "economy").get("beg", {})
    gid, uid = interaction.guild_id, interaction.user.id

    remaining = db.cooldown_remaining(gid, uid, "beg")
    if remaining > 0:
        return await interaction.response.send_message(
            embed=_cooldown_embed("/beg", remaining), ephemeral=True)

    db.set_cooldown(gid, uid, "beg", int(conf.get("cooldown_seconds", 300)))

    if random.random() < float(conf.get("fail_chance", 0.35)):
        embed = discord.Embed(
            title="🥺 Begging failed",
            description=random.choice(BEG_FAIL),
            color=0xE74C3C)
        return await interaction.response.send_message(embed=embed)

    lo, hi = int(conf.get("min", 5)), int(conf.get("max", 50))
    amount = random.randint(min(lo, hi), max(lo, hi))
    wallet, _bank = db.add_wallet(gid, uid, amount)

    embed = discord.Embed(
        title="🥺 Begging paid off",
        description=random.choice(BEG_SUCCESS).format(amount=f"**{_fmt_money(amount)}**"),
        color=0x2ECC71)
    embed.set_footer(text=f"Wallet: {_fmt_money(wallet)}")
    await interaction.response.send_message(embed=embed)


# ══════════════════════════════════════════════════════════════
#  BANK-COMMANDS – /balance /deposit /withdraw
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="balance", description="💳 Show wallet & bank balance")
@app_commands.describe(user="Another member (optional – leave empty for yourself)")
async def cmd_balance(interaction: discord.Interaction,
                      user: Optional[discord.Member] = None):
    if not await _require_guild(interaction):
        return
    target = user or interaction.user
    wallet, bank = db.get_balance(interaction.guild_id, target.id)

    embed = discord.Embed(
        title=f"💳 Balance – {target.display_name}",
        color=0x5865F2)
    embed.add_field(name="👛 Wallet", value=_fmt_money(wallet),        inline=True)
    embed.add_field(name="🏦 Bank",   value=_fmt_money(bank),          inline=True)
    embed.add_field(name="Σ Total",   value=_fmt_money(wallet + bank), inline=True)
    if target.display_avatar:
        embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="deposit", description="🏦 Move money from wallet to bank")
@app_commands.describe(amount="Amount to deposit (leave empty = everything)")
async def cmd_deposit(interaction: discord.Interaction,
                      amount: Optional[app_commands.Range[int, 1]] = None):
    if not await _require_guild(interaction):
        return
    moved, wallet, bank = db.deposit(interaction.guild_id, interaction.user.id, amount)
    if moved <= 0:
        return await interaction.response.send_message(
            "❌ Nothing to deposit – your wallet is empty.", ephemeral=True)
    embed = discord.Embed(
        title="🏦 Deposit successful",
        description=f"Moved **{_fmt_money(moved)}** into your bank.",
        color=0x2ECC71)
    embed.add_field(name="👛 Wallet", value=_fmt_money(wallet), inline=True)
    embed.add_field(name="🏦 Bank",   value=_fmt_money(bank),   inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="withdraw", description="👛 Move money from bank to wallet")
@app_commands.describe(amount="Amount to withdraw (leave empty = everything)")
async def cmd_withdraw(interaction: discord.Interaction,
                       amount: Optional[app_commands.Range[int, 1]] = None):
    if not await _require_guild(interaction):
        return
    moved, wallet, bank = db.withdraw(interaction.guild_id, interaction.user.id, amount)
    if moved <= 0:
        return await interaction.response.send_message(
            "❌ Nothing to withdraw – your bank is empty.", ephemeral=True)
    embed = discord.Embed(
        title="👛 Withdraw successful",
        description=f"Moved **{_fmt_money(moved)}** into your wallet.",
        color=0x2ECC71)
    embed.add_field(name="👛 Wallet", value=_fmt_money(wallet), inline=True)
    embed.add_field(name="🏦 Bank",   value=_fmt_money(bank),   inline=True)
    await interaction.response.send_message(embed=embed)


# ══════════════════════════════════════════════════════════════
#  ECONOMY-ADMIN-COMMANDS – /addmoney /removemoney /setbalance
# ══════════════════════════════════════════════════════════════
async def _post_economy_admin_log(interaction: discord.Interaction, title: str,
                                  target: discord.Member, amount_text: str,
                                  wallet: int, color: int):
    """Jede Admin-Geldaktion in den economy_log-Feed posten."""
    embed = discord.Embed(
        title=title,
        description=(f"**{interaction.user.display_name}** → {target.mention}\n"
                     f"Amount: **{amount_text}**"),
        color=color)
    embed.set_footer(text=f"New wallet: {_fmt_money(wallet)}")
    _leit = connections.for_guild(interaction.guild_id)
    await _post_feed(interaction.guild_id, "economy_log", embed,
                     service_id=_leit.service_id if _leit else None)


@bot.tree.command(name="addmoney", description="💰 Add money to a member's wallet (admin)")
@app_commands.describe(user="Member who receives the money", amount="Amount to add")
async def cmd_addmoney(interaction: discord.Interaction,
                       user: discord.Member, amount: app_commands.Range[int, 1]):
    if not _is_economy_admin(interaction):
        return await _deny(interaction)
    wallet, _bank = db.add_wallet(interaction.guild_id, user.id, int(amount))
    embed = discord.Embed(
        title="💰 Money added",
        description=f"Added **{_fmt_money(amount)}** to {user.mention}'s wallet.",
        color=0x2ECC71)
    embed.set_footer(text=f"New wallet: {_fmt_money(wallet)}")
    await interaction.response.send_message(embed=embed)
    await _post_economy_admin_log(interaction, "💰 ADMIN: ADD MONEY",
                                  user, f"+{_fmt_money(amount)}", wallet, 0x2ECC71)


@bot.tree.command(name="removemoney", description="💸 Remove money from a member's wallet (admin)")
@app_commands.describe(user="Member to remove money from", amount="Amount to remove")
async def cmd_removemoney(interaction: discord.Interaction,
                          user: discord.Member, amount: app_commands.Range[int, 1]):
    if not _is_economy_admin(interaction):
        return await _deny(interaction)
    old_wallet, _ = db.get_balance(interaction.guild_id, user.id)
    wallet, _bank = db.add_wallet(interaction.guild_id, user.id, -int(amount))
    removed = old_wallet - wallet   # nie unter 0 → tatsächlich abgezogener Betrag
    embed = discord.Embed(
        title="💸 Money removed",
        description=f"Removed **{_fmt_money(removed)}** from {user.mention}'s wallet.",
        color=0xE67E22)
    embed.set_footer(text=f"New wallet: {_fmt_money(wallet)}")
    await interaction.response.send_message(embed=embed)
    await _post_economy_admin_log(interaction, "💸 ADMIN: REMOVE MONEY",
                                  user, f"-{_fmt_money(removed)}", wallet, 0xE67E22)


@bot.tree.command(name="setbalance", description="🎯 Set a member's wallet to an exact amount (admin)")
@app_commands.describe(user="Member", amount="New wallet amount")
async def cmd_setbalance(interaction: discord.Interaction,
                         user: discord.Member, amount: app_commands.Range[int, 0]):
    if not _is_economy_admin(interaction):
        return await _deny(interaction)
    wallet, _bank = db.set_wallet(interaction.guild_id, user.id, int(amount))
    embed = discord.Embed(
        title="🎯 Balance set",
        description=f"{user.mention}'s wallet is now **{_fmt_money(wallet)}**.",
        color=0x5865F2)
    await interaction.response.send_message(embed=embed)
    await _post_economy_admin_log(interaction, "🎯 ADMIN: SET BALANCE",
                                  user, _fmt_money(wallet), wallet, 0x5865F2)


@bot.tree.command(name="economy_reload",
                  description="🔄 Reload config.json (shop/economy/casino) without restarting the bot")
async def cmd_economy_reload(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    ok = cfg.reload_config()
    if ok:
        _conn_c = _conn_of(interaction)
        katalog = _catalog_of(interaction)   # nur der Katalog DIESES Servers
        if katalog is not None:
            katalog.load()
            items = [i for i in katalog.items if i.get("enabled", True)]
            katalog_txt = f"**{len(items)}** active items from `{katalog.source}`"
        else:
            katalog_txt = "kein Server zugeordnet"
        embed = discord.Embed(
            title="🔄 Config reloaded",
            description=(f"`config.json` was reloaded successfully.\n"
                         f"Catalog: {katalog_txt} · "
                         f"Currency: **{_conn_c.get('currency_name', '?') if _conn_c else '?'} "
                         f"({_cur_symbol(_conn_c)})**"),
            color=0x2ECC71)
    else:
        embed = discord.Embed(
            title="❌ Reload failed",
            description="Could not parse `config.json` – check the bot log / JSON syntax.",
            color=0xE74C3C)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════
#  KILL-STATS / LINK / BOUNTY / PAY
# ══════════════════════════════════════════════════════════════
async def _player_name_ac(interaction: discord.Interaction,
                          current: str) -> List[app_commands.Choice[str]]:
    """Autocomplete: bekannte Spielernamen aus Kills + Sitzungen dieses Servers."""
    conns = _ac_conns(interaction)
    if not conns:
        return []
    loop = asyncio.get_running_loop()
    out: List[app_commands.Choice] = []
    gesehen = set()
    for conn in conns:
        try:
            names = await loop.run_in_executor(None, db.known_player_names,
                                               conn.service_id, current, 25)
        except Exception:  # noqa: BLE001
            names = []
        for n in names:
            key = n[:100]
            if key in gesehen:
                continue        # derselbe Spieler auf beiden Servern → einmal
            gesehen.add(key)
            out.append(app_commands.Choice(name=key, value=key))
            if len(out) >= 25:
                return out
    return out


@bot.tree.command(name="stats", description="📊 Kill-Statistiken eines Spielers (Kills, Tode, K/D, Waffe)")
@app_commands.describe(spieler="Ingame-/PlayStation-Name")
@app_commands.autocomplete(spieler=_player_name_ac)
async def cmd_stats(interaction: discord.Interaction, spieler: str):
    conn = _conn_of(interaction)
    if conn is None:
        return await interaction.response.send_message(PREMIUM_MISSING_TEXT, ephemeral=True)
    st = db.player_stats(conn.service_id, spieler.strip())
    if not st:
        return await interaction.response.send_message(
            f"❌ Keine PvP-Daten für **{spieler}** gefunden. Statistiken werden "
            f"ab jetzt automatisch aus dem Killfeed aufgezeichnet.", ephemeral=True)
    e = discord.Embed(title=f"📊 Statistiken – {spieler}", color=0x5865F2)
    e.add_field(name="☠️ Kills", value=str(st["kills"]), inline=True)
    e.add_field(name="💀 Tode",  value=str(st["deaths"]), inline=True)
    e.add_field(name="⚖️ K/D",   value=f"{st['kd']:.2f}", inline=True)
    e.add_field(name="🔫 Lieblingswaffe",
                value=(f"{st['fav_weapon']} ({st['fav_weapon_kills']} Kills)"
                       if st["fav_weapon"] else "–"), inline=True)
    e.add_field(name="🎯 Weitester Kill",
                value=(f"{st['longest']:.0f} m" if st["longest"] else "–"), inline=True)
    if interaction.guild_id:
        links = db.links_for_name(spieler.strip(), interaction.guild_id)
        if links:
            e.add_field(name="🔗 Verknüpft mit",
                        value=f"<@{int(links[0]['user_id'])}>", inline=True)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="leaderboard", description="🏆 Top 10 PvP-Killer des Servers")
async def cmd_leaderboard(interaction: discord.Interaction):
    conn = _conn_of(interaction)
    if conn is None:
        return await interaction.response.send_message(PREMIUM_MISSING_TEXT, ephemeral=True)
    rows = db.leaderboard(conn.service_id, 10)
    if not rows:
        return await interaction.response.send_message(
            "❌ Noch keine PvP-Kills aufgezeichnet – das Leaderboard füllt sich "
            "automatisch aus dem Killfeed.", ephemeral=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        rank = medals[i] if i < 3 else f"`#{i + 1}`"
        best = f" · 🎯 {r['best']:.0f} m" if r["best"] else ""
        lines.append(f"{rank} **{r['name']}** – {r['kills']} Kills · "
                     f"{r['deaths']} Tode · K/D {r['kd']:.2f}{best}")
    e = discord.Embed(title="🏆 Kill-Leaderboard", description="\n".join(lines),
                      color=0xF1C40F)
    e.set_footer(text="Automatisch aus dem Killfeed · /stats <spieler> für Details")
    await interaction.response.send_message(embed=e)


def _seen_in_logs(name: str, max_age_seconds: int = 900,
                  positions: Optional[Dict[str, Dict]] = None) -> Optional[Dict]:
    """Prüft, ob der Spieler kürzlich in den ADM-Logs auftauchte (Positions-Tracking
    des Parsers). Gibt den Eintrag (mit 'id') zurück, sonst None."""
    target = name.lower()
    now = datetime.now(timezone.utc)
    for pname, info in list((positions or {}).items()):
        if pname.lower() != target:
            continue
        try:
            seen = datetime.fromisoformat(str(info.get("last_seen", "")))
        except ValueError:
            return None
        return info if (now - seen).total_seconds() <= max_age_seconds else None
    return None


@bot.tree.command(name="link", description="🔗 Verknüpft deinen Discord-Account mit deinem PlayStation-Namen")
@app_commands.describe(playstation_name="Dein Ingame-Name, exakt wie im Spiel")
@app_commands.autocomplete(playstation_name=_player_name_ac)
async def cmd_link(interaction: discord.Interaction, playstation_name: str):
    if not await _require_guild(interaction):
        return
    name = playstation_name.strip()
    if not name or len(name) > 64:
        return await interaction.response.send_message(
            "❌ Ungültiger Name.", ephemeral=True)
    existing = db.get_link_by_user(interaction.guild_id, interaction.user.id)
    if existing:
        old_name = str(existing["ingame_name"])
        if old_name.lower() == name.lower():
            return await interaction.response.send_message(
                f"✅ Du bist bereits mit **{old_name}** verbunden.", ephemeral=True)
        return await interaction.response.send_message(
            f"❌ Du bist bereits mit **{old_name}** verbunden – nutze zuerst `/unlink`, "
            f"um den Namen zu wechseln.", ephemeral=True)
    ok, _why = db.link_user(interaction.guild_id, interaction.user.id, name)
    if not ok:
        return await interaction.response.send_message(
            f"❌ **{name}** ist bereits mit einem anderen Discord-Account verknüpft. "
            f"Ein Admin kann das mit `/forcelink` korrigieren.", ephemeral=True)
    # Logs nach dem PSN-Namen prüfen: Ist der Spieler gerade auf dem Server,
    # startet der Spielzeit-Zähler sofort (kein neues Connect-Event nötig)
    _conn = _conn_of(interaction)
    seen = _seen_in_logs(name, positions=(_conn.parser.player_positions
                                          if _conn is not None and _conn.parser else {}))
    _sid = _conn.service_id if _conn is not None else ""
    if seen:
        if seen.get("id"):
            db.update_link_id(name, str(seen["id"]), interaction.guild_id)
        if not db.has_session(_sid, name):
            db.open_session(_sid, name, seen.get("id"))
        online_line = "\n🟢 Du bist gerade auf dem Server – der Spielzeit-Zähler läuft ab jetzt!"
    else:
        online_line = ("\nℹ️ Aktuell nicht in den Logs gesehen – der Spielzeit-Zähler "
                       "startet bei deinem nächsten Connect.")
    reward   = int((_conn.get("kill_reward", 0) if _conn is not None
                    else cfg.config.get("kill_reward", 0)) or 0)
    pt       = ((_conn.get("playtime_reward") if _conn is not None
                 else cfg.config.get("playtime_reward")) or {})
    pt_line  = (f"\n⏱️ Spielzeit: **{_fmt_money(int(pt.get('amount', 0)))}** pro "
                f"**{int(pt.get('interval_minutes', 30))} Min** auf dem Server"
                if int(pt.get("amount", 0)) > 0 else "")
    e = discord.Embed(
        title="🔗 Account verknüpft",
        description=(f"{interaction.user.mention} ↔ **{name}**\n"
                     f"☠️ Pro PvP-Kill: **{_fmt_money(reward)}**{pt_line}{online_line}"),
        color=0x2ECC71)
    await interaction.response.send_message(embed=e)
    note = discord.Embed(
        title="🔗 /link verwendet",
        description=f"{interaction.user.mention} (`{interaction.user}`) hat sich mit **{name}** verknüpft.",
        color=0x2ECC71)
    await _notify_link_change(interaction.guild_id, note)


@bot.tree.command(name="unlink", description="🔓 Entfernt deine eigene Ingame-Verknüpfung")
async def cmd_unlink(interaction: discord.Interaction):
    if not await _require_guild(interaction):
        return
    old = db.unlink_user(interaction.guild_id, interaction.user.id)
    if not old:
        return await interaction.response.send_message(
            "❌ Du bist mit keinem Ingame-Namen verknüpft.", ephemeral=True)
    await interaction.response.send_message(
        f"🔓 Verknüpfung mit **{old}** entfernt.", ephemeral=True)
    note = discord.Embed(
        title="🔓 /unlink verwendet",
        description=f"{interaction.user.mention} (`{interaction.user}`) hat die Verknüpfung mit **{old}** entfernt.",
        color=0xE67E22)
    await _notify_link_change(interaction.guild_id, note)


@bot.tree.command(name="forcelink", description="🔗 (Admin) Verknüpft einen Spieler mit einem Discord-Account")
@app_commands.describe(playstation_name="Ingame-Name des Spielers",
                       user="Discord-Mitglied")
@app_commands.autocomplete(playstation_name=_player_name_ac)
async def cmd_forcelink(interaction: discord.Interaction,
                        playstation_name: str, user: discord.Member):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_guild(interaction):
        return
    name = playstation_name.strip()
    # Bestehende Verknüpfung dieses Namens (anderer User) lösen
    for lk in db.links_for_name(name, interaction.guild_id):
        if int(lk["user_id"]) != user.id:
            db.unlink_user(interaction.guild_id, int(lk["user_id"]))
    db.link_user(interaction.guild_id, user.id, name)
    await interaction.response.send_message(
        f"🔗 **{name}** ↔ {user.mention} verknüpft (Admin).", ephemeral=True)
    note = discord.Embed(
        title="🔗 /forcelink verwendet",
        description=(f"{interaction.user.mention} hat {user.mention} "
                     f"mit **{name}** verknüpft."),
        color=0x2ECC71)
    await _notify_link_change(interaction.guild_id, note)


@bot.tree.command(name="forceunlink", description="🔓 (Admin) Entfernt die Verknüpfung eines Discord-Accounts")
@app_commands.describe(user="Discord-Mitglied")
async def cmd_forceunlink(interaction: discord.Interaction, user: discord.Member):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_guild(interaction):
        return
    old = db.unlink_user(interaction.guild_id, user.id)
    if not old:
        return await interaction.response.send_message(
            f"❌ {user.mention} ist mit keinem Ingame-Namen verknüpft.", ephemeral=True)
    await interaction.response.send_message(
        f"🔓 Verknüpfung {user.mention} ↔ **{old}** entfernt (Admin).", ephemeral=True)
    note = discord.Embed(
        title="🔓 /forceunlink verwendet",
        description=(f"{interaction.user.mention} hat die Verknüpfung "
                     f"{user.mention} ↔ **{old}** entfernt."),
        color=0xE67E22)
    await _notify_link_change(interaction.guild_id, note)


username_group = app_commands.Group(name="username",
                                    description="🔗 Verknüpfte PSN-Namen verwalten")


@username_group.command(name="list", description="📋 Zeigt deine verknüpften PSN-Namen (Admins: alle)")
async def username_list(interaction: discord.Interaction):
    if not await _require_guild(interaction):
        return
    if not _is_admin(interaction):
        # Normale Nutzer sehen nur die eigene Verknüpfung
        own = db.get_link_by_user(interaction.guild_id, interaction.user.id)
        if not own:
            return await interaction.response.send_message(
                "ℹ️ Du bist mit keinem PSN-Namen verknüpft. Nutze `/link <psn-name>`.",
                ephemeral=True)
        name   = str(own["ingame_name"])
        _conn  = _conn_of(interaction)
        _sid   = _conn.service_id if _conn is not None else ""
        online = "🟢 " if db.has_session(_sid, name) else "⚫ "
        e = discord.Embed(
            title="🔗 Deine Verknüpfung",
            description=f"{online}**{name}** ↔ {interaction.user.mention}",
            color=0x5865F2)
        e.set_footer(text="🟢 = gerade auf dem Server · Admins sehen die vollständige Liste")
        return await interaction.response.send_message(embed=e, ephemeral=True)
    rows = db.list_links(interaction.guild_id)
    if not rows:
        return await interaction.response.send_message(
            "ℹ️ Noch keine Verknüpfungen vorhanden. Spieler verbinden sich mit "
            "`/link <psn-name>`.", ephemeral=True)
    lines = []
    _conn = _conn_of(interaction)
    _sid  = _conn.service_id if _conn is not None else ""
    for r in rows[:50]:
        online = "🟢 " if db.has_session(_sid, str(r["ingame_name"])) else "⚫ "
        lines.append(f"{online}**{r['ingame_name']}** ↔ <@{int(r['user_id'])}>")
    e = discord.Embed(
        title=f"🔗 Verknüpfte PSN-Namen ({len(rows)})",
        description="\n".join(lines),
        color=0x5865F2)
    e.set_footer(text="🟢 = gerade auf dem Server (offene Spielzeit-Sitzung)"
                      + (f" · … und {len(rows) - 50} weitere" if len(rows) > 50 else ""))
    await interaction.response.send_message(embed=e, ephemeral=True)


bot.tree.add_command(username_group)


@bot.tree.command(name="bounty", description="🎯 Setzt ein Kopfgeld auf einen Spieler aus (sofort abgebucht)")
@app_commands.describe(spieler="Ingame-Name des Ziels", betrag="Kopfgeld aus deinem Wallet")
@app_commands.autocomplete(spieler=_player_name_ac)
async def cmd_bounty(interaction: discord.Interaction,
                     spieler: str, betrag: app_commands.Range[int, 1]):
    if not await _require_guild(interaction):
        return
    name = spieler.strip()
    own = db.get_link_by_user(interaction.guild_id, interaction.user.id)
    if own and str(own["ingame_name"]).lower() == name.lower():
        return await interaction.response.send_message(
            "❌ Auf deinen eigenen Kopf kannst du kein Kopfgeld aussetzen.", ephemeral=True)
    bconf   = _srv_conf(interaction, "bounty")
    min_amt = int(bconf.get("min_amount", 100))
    max_amt = int(bconf.get("max_amount", 10000))
    if not (min_amt <= int(betrag) <= max_amt):
        return await interaction.response.send_message(
            f"❌ Kopfgeld muss zwischen **{_fmt_money(min_amt)}** und "
            f"**{_fmt_money(max_amt)}** liegen (`bounty` in config.json).", ephemeral=True)
    remaining = db.cooldown_remaining(interaction.guild_id, interaction.user.id, "bounty")
    if remaining > 0:
        return await interaction.response.send_message(
            embed=_cooldown_embed("/bounty", remaining), ephemeral=True)
    if not db.try_spend_wallet(interaction.guild_id, interaction.user.id, int(betrag)):
        wallet, _ = db.get_balance(interaction.guild_id, interaction.user.id)
        return await interaction.response.send_message(
            embed=_insufficient_embed(int(betrag), wallet), ephemeral=True)
    total = db.add_bounty(interaction.guild_id, name, int(betrag), interaction.user.id)
    db.set_cooldown(interaction.guild_id, interaction.user.id, "bounty",
                    int(bconf.get("cooldown_seconds", 300)))
    e = discord.Embed(
        title="🎯 Kopfgeld ausgesetzt",
        description=(f"**{_fmt_money(betrag)}** auf den Kopf von **{name}**!\n"
                     f"Gesamtes Kopfgeld: **{_fmt_money(total)}**\n"
                     f"Auszahlung automatisch an den (per `/link` verknüpften) Killer."),
        color=0xE67E22)
    e.set_footer(text=f"Ausgesetzt von {interaction.user.display_name}")
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="bounties", description="🎯 Zeigt alle aktiven Kopfgelder")
async def cmd_bounties(interaction: discord.Interaction):
    if not await _require_guild(interaction):
        return
    rows = db.open_bounties(interaction.guild_id)
    if not rows:
        return await interaction.response.send_message(
            "✅ Keine aktiven Kopfgelder.", ephemeral=True)
    lines = [f"🎯 **{r['target_name']}** – {_fmt_money(int(r['total']))} "
             f"({int(r['n'])} Kopfgeld{'er' if int(r['n']) != 1 else ''})"
             for r in rows[:20]]
    e = discord.Embed(title="🎯 Aktive Kopfgelder", description="\n".join(lines),
                      color=0xE67E22)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="pay", description="💸 Überweist Geld aus deinem Wallet an ein anderes Mitglied")
@app_commands.describe(user="Empfänger", betrag="Betrag aus deinem Wallet")
async def cmd_pay(interaction: discord.Interaction,
                  user: discord.Member, betrag: app_commands.Range[int, 1]):
    if not await _require_guild(interaction):
        return
    if user.id == interaction.user.id:
        return await interaction.response.send_message(
            "❌ Du kannst dir nicht selbst Geld überweisen.", ephemeral=True)
    if user.bot:
        return await interaction.response.send_message(
            "❌ Bots brauchen kein Geld.", ephemeral=True)
    if not db.try_spend_wallet(interaction.guild_id, interaction.user.id, int(betrag)):
        wallet, _ = db.get_balance(interaction.guild_id, interaction.user.id)
        return await interaction.response.send_message(
            embed=_insufficient_embed(int(betrag), wallet), ephemeral=True)
    new_wallet, _ = db.add_wallet(interaction.guild_id, user.id, int(betrag))
    e = discord.Embed(
        title="💸 Überweisung",
        description=f"{interaction.user.mention} → {user.mention}: **{_fmt_money(betrag)}**",
        color=0x2ECC71)
    await interaction.response.send_message(embed=e)
    log_embed = discord.Embed(
        title="💸 PLAYER TRANSFER",
        description=(f"**{interaction.user.display_name}** → **{user.display_name}**\n"
                     f"Betrag: **{_fmt_money(betrag)}**"),
        color=0x3498DB)
    _leit = connections.for_guild(interaction.guild_id)
    await _post_feed(interaction.guild_id, "economy_log", log_embed,
                     service_id=_leit.service_id if _leit else None)


# ══════════════════════════════════════════════════════════════
#  CASINO – /slots
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="slots", description="🎰 Spin the slot machine")
@app_commands.describe(bet="Your bet (paid from wallet)")
async def cmd_slots(interaction: discord.Interaction, bet: app_commands.Range[int, 1]):
    if not await _require_guild(interaction):
        return
    conf = _srv_conf(interaction, "casino").get("slots", {})
    gid, uid = interaction.guild_id, interaction.user.id
    bet = int(bet)

    err = _validate_bet(bet, conf)
    if err:
        return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
    remaining = db.cooldown_remaining(gid, uid, "slots")
    if remaining > 0:
        return await interaction.response.send_message(
            embed=_cooldown_embed("/slots", remaining), ephemeral=True)
    if not db.try_spend_wallet(gid, uid, bet):
        wallet, _ = db.get_balance(gid, uid)
        return await interaction.response.send_message(
            embed=_insufficient_embed(bet, wallet), ephemeral=True)
    db.set_cooldown(gid, uid, "slots", int(conf.get("cooldown_seconds", 10)))

    symbols = list(conf.get("symbols", ["🍒", "🍋", "🍉", "🔔", "💎", "7️⃣"]))
    weights = list(conf.get("weights", []))
    if len(weights) != len(symbols):
        weights = [1] * len(symbols)   # Gewichte passen nicht → gleichverteilt
    reels = random.choices(symbols, weights=weights, k=3)

    payout_three = conf.get("payout_three", {})
    payout_two   = float(conf.get("payout_two", 1.5))
    if reels[0] == reels[1] == reels[2]:
        mult = float(payout_three.get(reels[0], 5))
        result = "three_of_a_kind"
    elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
        mult = payout_two
        result = "pair"
    else:
        mult = 0.0
        result = "lose"

    payout = int(round(bet * mult))
    if payout > 0:
        db.add_wallet(gid, uid, payout)
    db.log_casino(gid, uid, "slots", bet, payout, result)
    wallet, _bank = db.get_balance(gid, uid)

    net = payout - bet
    if net > 0:
        color, headline = 0x2ECC71, f"You won **{_fmt_money(payout)}**! (net **+{net:,}**)"
    elif net == 0:
        color, headline = 0x95A5A6, "Break-even – your bet came back."
    else:
        color, headline = 0xE74C3C, f"You lost **{_fmt_money(bet)}**."

    embed = discord.Embed(
        title="🎰 Slots",
        description=f"**| {reels[0]} | {reels[1]} | {reels[2]} |**\n\n{headline}",
        color=color)
    embed.set_footer(text=f"Bet: {_fmt_money(bet)} · Wallet: {_fmt_money(wallet)}")
    await interaction.response.send_message(embed=embed)


# ══════════════════════════════════════════════════════════════
#  CASINO – /roulette
# ══════════════════════════════════════════════════════════════
_ROULETTE_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
_ROULETTE_NAMED = ("red", "black", "green", "even", "odd", "low", "high")

@bot.tree.command(name="roulette",
                  description="🎡 Bet on red/black/even/odd/low/high or a single number (0-36)")
@app_commands.describe(bet="Your bet (paid from wallet)",
                       wager="red, black, green, even, odd, low, high or a number 0-36")
async def cmd_roulette(interaction: discord.Interaction,
                       bet: app_commands.Range[int, 1], wager: str):
    if not await _require_guild(interaction):
        return
    conf = _srv_conf(interaction, "casino").get("roulette", {})
    gid, uid = interaction.guild_id, interaction.user.id
    bet = int(bet)

    # Wette parsen
    w = wager.strip().lower()
    number: Optional[int] = None
    if w in _ROULETTE_NAMED:
        kind = w
    elif w.isdigit() and 0 <= int(w) <= 36:
        kind, number = "number", int(w)
    else:
        return await interaction.response.send_message(
            "❌ Invalid wager. Use `red`, `black`, `green`, `even`, `odd`, `low`, `high` "
            "or a number from `0` to `36`.", ephemeral=True)

    err = _validate_bet(bet, conf)
    if err:
        return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
    remaining = db.cooldown_remaining(gid, uid, "roulette")
    if remaining > 0:
        return await interaction.response.send_message(
            embed=_cooldown_embed("/roulette", remaining), ephemeral=True)
    if not db.try_spend_wallet(gid, uid, bet):
        wallet, _ = db.get_balance(gid, uid)
        return await interaction.response.send_message(
            embed=_insufficient_embed(bet, wallet), ephemeral=True)
    db.set_cooldown(gid, uid, "roulette", int(conf.get("cooldown_seconds", 5)))

    spin = random.randint(0, 36)
    if spin == 0:
        spin_disp = "🟢 **0** (green)"
    elif spin in _ROULETTE_RED:
        spin_disp = f"🔴 **{spin}** (red)"
    else:
        spin_disp = f"⚫ **{spin}** (black)"

    # Gewinn & Auszahlungs-Multiplikator (Multiplikator = Gesamt-Rückzahlung × Einsatz)
    p_num  = float(conf.get("payout_number", 36.0))
    p_col  = float(conf.get("payout_color", 2.0))
    p_eo   = float(conf.get("payout_evenodd", 2.0))
    p_hl   = float(conf.get("payout_highlow", 2.0))
    won, mult = False, 0.0
    if kind == "number":
        won, mult = (spin == number), p_num
    elif kind == "green":
        won, mult = (spin == 0), p_num
    elif kind == "red":
        won, mult = (spin != 0 and spin in _ROULETTE_RED), p_col
    elif kind == "black":
        won, mult = (spin != 0 and spin not in _ROULETTE_RED), p_col
    elif kind == "even":
        won, mult = (spin != 0 and spin % 2 == 0), p_eo
    elif kind == "odd":
        won, mult = (spin % 2 == 1), p_eo
    elif kind == "low":
        won, mult = (1 <= spin <= 18), p_hl
    elif kind == "high":
        won, mult = (19 <= spin <= 36), p_hl

    payout = int(round(bet * mult)) if won else 0
    if payout > 0:
        db.add_wallet(gid, uid, payout)
    wager_disp = f"number {number}" if kind == "number" else kind
    db.log_casino(gid, uid, "roulette", bet, payout, f"{wager_disp}|spin={spin}")
    wallet, _bank = db.get_balance(gid, uid)

    if won:
        color = 0x2ECC71
        headline = f"You won **{_fmt_money(payout)}**! (net **+{payout - bet:,}**)"
    else:
        color = 0xE74C3C
        headline = f"You lost **{_fmt_money(bet)}**."

    embed = discord.Embed(
        title="🎡 Roulette",
        description=f"The ball landed on {spin_disp}\nYour wager: **{wager_disp}**\n\n{headline}",
        color=color)
    embed.set_footer(text=f"Bet: {_fmt_money(bet)} · Wallet: {_fmt_money(wallet)}")
    await interaction.response.send_message(embed=embed)


@cmd_roulette.autocomplete("wager")
async def _roulette_wager_ac(interaction: discord.Interaction,
                             current: str) -> List[app_commands.Choice[str]]:
    cur = current.strip().lower()
    out = [app_commands.Choice(name=o, value=o)
           for o in _ROULETTE_NAMED if (not cur) or cur in o]
    if cur.isdigit() and 0 <= int(cur) <= 36:
        out.insert(0, app_commands.Choice(name=f"number {cur}", value=cur))
    return out[:25]


# ══════════════════════════════════════════════════════════════
#  CASINO – /blackjack (spielbar mit Hit/Stand-Buttons)
# ══════════════════════════════════════════════════════════════
_BJ_RANKS: Dict[str, int] = {
    "A": 11, "K": 10, "Q": 10, "J": 10, "10": 10,
    "9": 9, "8": 8, "7": 7, "6": 6, "5": 5, "4": 4, "3": 3, "2": 2,
}

def _bj_new_deck() -> List[str]:
    deck = [f"{rank}{suit}" for rank in _BJ_RANKS for suit in "♠♥♦♣"]
    random.shuffle(deck)
    return deck

def _bj_value(hand: List[str]) -> int:
    """Handwert mit flexiblen Assen (11 → 1 solange über 21)."""
    total, aces = 0, 0
    for card in hand:
        rank = card[:-1]
        total += _BJ_RANKS[rank]
        if rank == "A":
            aces += 1
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


class BlackjackView(discord.ui.View):
    """Interaktives Blackjack: Einsatz ist bereits abgebucht,
    Auszahlung erfolgt beim Auflösen (Win=2x, Push=1x, Blackjack=1+bonus)."""

    def __init__(self, interaction: discord.Interaction, bet: int, conf: Dict):
        super().__init__(timeout=120)
        self.user_id   = interaction.user.id
        self.guild_id  = interaction.guild_id
        self.bet       = bet
        self.payout_bj = float(conf.get("blackjack_payout", 1.5))
        self.cooldown_s = int(conf.get("cooldown_seconds", 30))
        self.deck      = _bj_new_deck()
        self.player    = [self.deck.pop(), self.deck.pop()]
        self.dealer    = [self.deck.pop(), self.deck.pop()]
        self.finished  = False
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, itx: discord.Interaction) -> bool:
        if itx.user.id != self.user_id:
            await itx.response.send_message("This is not your game.", ephemeral=True)
            return False
        return True

    def build_embed(self, reveal: bool = False, result_line: Optional[str] = None,
                    color: int = 0x5865F2) -> discord.Embed:
        dealer_hand = " ".join(self.dealer) if reveal else f"{self.dealer[0]} 🂠"
        dealer_val  = str(_bj_value(self.dealer)) if reveal else "?"
        e = discord.Embed(title="🃏 Blackjack", color=color)
        e.add_field(name=f"Your hand ({_bj_value(self.player)})",
                    value=" ".join(self.player), inline=False)
        e.add_field(name=f"Dealer ({dealer_val})", value=dealer_hand, inline=False)
        e.add_field(name="Bet", value=_fmt_money(self.bet), inline=True)
        if result_line:
            e.add_field(name="Result", value=result_line, inline=False)
        return e

    def _payout_and_log(self, mult: float, result: str) -> int:
        payout = int(round(self.bet * mult))
        if payout > 0:
            db.add_wallet(self.guild_id, self.user_id, payout)
        db.log_casino(self.guild_id, self.user_id, "blackjack", self.bet, payout, result)
        return payout

    def _dealer_play(self):
        # Dealer zieht bis mindestens 17
        while _bj_value(self.dealer) < 17:
            self.dealer.append(self.deck.pop())

    async def _finish(self, itx: Optional[discord.Interaction],
                      result: str, mult: float, color: int):
        if self.finished:
            return   # idempotent – verhindert doppelte Auszahlung bei Races
        self.finished = True
        for child in self.children:
            child.disabled = True
        payout = self._payout_and_log(mult, result)
        # Cooldown läuft ab SPIELENDE – der beim Start gesetzte wäre bei
        # längeren Partien schon abgelaufen und damit wirkungslos
        db.set_cooldown(self.guild_id, self.user_id, "blackjack", self.cooldown_s)
        wallet, _bank = db.get_balance(self.guild_id, self.user_id)
        net = payout - self.bet
        line = (f"{result}\nPayout: **{_fmt_money(payout)}** "
                f"(net **{'+' if net >= 0 else ''}{net:,}**) · Wallet: {_fmt_money(wallet)}")
        embed = self.build_embed(reveal=True, result_line=line, color=color)
        if itx is not None:
            await itx.response.edit_message(embed=embed, view=self)
        elif self.message:
            try:
                await self.message.edit(embed=embed, view=self)
            except Exception:
                pass
        self.stop()

    async def _resolve_stand(self, itx: Optional[discord.Interaction]):
        self._dealer_play()
        pv, dv = _bj_value(self.player), _bj_value(self.dealer)
        if dv > 21:
            await self._finish(itx, "Dealer busts – you win!", 2.0, 0x2ECC71)
        elif pv > dv:
            await self._finish(itx, "You win!", 2.0, 0x2ECC71)
        elif pv == dv:
            await self._finish(itx, "Push – bet returned.", 1.0, 0x95A5A6)
        else:
            await self._finish(itx, "Dealer wins.", 0.0, 0xE74C3C)

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="🃏")
    async def hit(self, itx: discord.Interaction, button: discord.ui.Button):
        if self.finished:   # Doppelklick-/Timeout-Race abfangen
            return await itx.response.defer()
        self.player.append(self.deck.pop())
        value = _bj_value(self.player)
        if value > 21:
            return await self._finish(itx, "Bust! You lose.", 0.0, 0xE74C3C)
        if value == 21:
            return await self._resolve_stand(itx)
        await itx.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, emoji="✋")
    async def stand(self, itx: discord.Interaction, button: discord.ui.Button):
        if self.finished:   # Doppelklick-/Timeout-Race abfangen
            return await itx.response.defer()
        await self._resolve_stand(itx)

    async def on_timeout(self):
        # Timeout = automatisch Stand, damit der Einsatz nicht verfällt
        if not self.finished:
            await self._resolve_stand(None)


@bot.tree.command(name="blackjack", description="🃏 Play blackjack against the dealer (Hit/Stand)")
@app_commands.describe(bet="Your bet (paid from wallet)")
async def cmd_blackjack(interaction: discord.Interaction, bet: app_commands.Range[int, 1]):
    if not await _require_guild(interaction):
        return
    conf = _srv_conf(interaction, "casino").get("blackjack", {})
    gid, uid = interaction.guild_id, interaction.user.id
    bet = int(bet)

    err = _validate_bet(bet, conf)
    if err:
        return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
    remaining = db.cooldown_remaining(gid, uid, "blackjack")
    if remaining > 0:
        return await interaction.response.send_message(
            embed=_cooldown_embed("/blackjack", remaining), ephemeral=True)
    if not db.try_spend_wallet(gid, uid, bet):
        wallet, _ = db.get_balance(gid, uid)
        return await interaction.response.send_message(
            embed=_insufficient_embed(bet, wallet), ephemeral=True)
    db.set_cooldown(gid, uid, "blackjack", int(conf.get("cooldown_seconds", 30)))

    view = BlackjackView(interaction, bet, conf)
    pv, dv = _bj_value(view.player), _bj_value(view.dealer)

    # Natürlicher Blackjack → sofort auflösen, keine Buttons nötig
    if pv == 21 or dv == 21:
        if pv == 21 and dv == 21:
            result, mult, color = "Double blackjack – push, bet returned.", 1.0, 0x95A5A6
        elif pv == 21:
            result = f"BLACKJACK! Pays {view.payout_bj}x bonus."
            mult, color = 1.0 + view.payout_bj, 0xF1C40F
        else:
            result, mult, color = "Dealer has blackjack. You lose.", 0.0, 0xE74C3C
        payout = view._payout_and_log(mult, result)
        wallet, _bank = db.get_balance(gid, uid)
        net = payout - bet
        line = (f"{result}\nPayout: **{_fmt_money(payout)}** "
                f"(net **{'+' if net >= 0 else ''}{net:,}**) · Wallet: {_fmt_money(wallet)}")
        embed = view.build_embed(reveal=True, result_line=line, color=color)
        view.stop()
        return await interaction.response.send_message(embed=embed)

    await interaction.response.send_message(embed=view.build_embed(), view=view)
    view.message = await interaction.original_response()


# ══════════════════════════════════════════════════════════════
#  SHOP-COMMANDS – /shop list|pending|cleanup|setprice und /buy
# ══════════════════════════════════════════════════════════════
class ShopCatalog:
    """Item-Katalog **eines** Nitrado-Servers.

    Jeder verbundene Server hat seinen eigenen Katalog in
    ``shop_items_<service_id>.json`` – sonst würden alle Kunden dieselben
    Items, Preise und Bundles teilen. Der Katalog wird aus der ``types.xml``
    des jeweiligen Servers erzeugt (per FTP geholt) oder von Hand gepflegt.
    Hält Indizes, damit Lookups und Autocomplete auch bei ~1700 Items
    schnell bleiben.
    """

    def __init__(self, service_id: str = "", path: str = ""):
        self.service_id = str(service_id or "")
        self._path = str(path or "")
        self.items: List[Dict] = []
        self.source = self.path
        self._by_key: Dict[str, Dict] = {}              # name/classname (lower) → Item
        self.by_category: Dict[str, List[Dict]] = {}
        # (suchtext, label, value, enabled) – vorberechnet für Autocomplete
        self._ac_index: List[Tuple[str, str, str, bool]] = []

    # ── Speicherort ──────────────────────────────────────────
    @property
    def path(self) -> str:
        """Katalogdatei dieses Servers."""
        if self._path:
            return self._path
        if self.service_id:
            return f"shop_items_{self.service_id}.json"
        return self._legacy_path()

    @staticmethod
    def _legacy_path() -> str:
        """Der frühere gemeinsame Katalog aus der Zeit vor der Mandantentrennung."""
        return str(cfg.config.get("shop_items_file") or "shop_items.json")

    def _erbt_altbestand(self) -> bool:
        """Darf dieser Katalog den alten gemeinsamen Bestand übernehmen?

        Nur der Server des Betreibers (``primary()``) – bei allen anderen wäre
        das genau das Leck, das die Trennung verhindern soll: ein neuer Kunde
        bekäme die Items und Preise eines fremden Servers.
        """
        if not self.service_id:
            return True                      # Dashboard-Vorschau ohne Verbindung
        try:
            haupt = connections.primary()
        except Exception:  # noqa: BLE001 – Registry evtl. noch nicht geladen
            return False
        return haupt is not None and haupt.service_id == self.service_id

    @staticmethod
    def _datei_lesen(path: str) -> Optional[List[Dict]]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            log.error(f"[SHOP] {path} unlesbar ({e}).")
            return None
        cand = data.get("items") if isinstance(data, dict) else data
        return cand if isinstance(cand, list) else None

    def load(self):
        path = self.path
        items = self._datei_lesen(path) if os.path.exists(path) else None
        migriert = False
        if items is None and self._erbt_altbestand():
            # Einmalige Übernahme: aus dem alten gemeinsamen shop_items.json bzw.
            # aus shop_items in der config.json.
            legacy = self._legacy_path()
            if legacy != path and os.path.exists(legacy):
                items = self._datei_lesen(legacy)
                migriert = items is not None
            if items is None:
                aus_config = list(cfg.config.get("shop_items", []) or [])
                if aus_config:
                    items, migriert = aus_config, True
        if items is None:
            items = []
        self.source = path
        self.items = [it for it in items
                      if isinstance(it, dict) and (it.get("classname") or it.get("classnames"))]
        self.rebuild_index()
        if migriert and self.items:
            self.save()          # Bestand ab jetzt unter dem eigenen Dateinamen
            log.info(f"[SHOP] Alter Katalog nach {path} übernommen.")
        log.info(f"[SHOP] Katalog {self.service_id or '-'}: "
                 f"{len(self.items)} Items aus {self.source}")

    def rebuild_index(self):
        self._by_key.clear()
        self.by_category.clear()
        self._ac_index = []
        # Symbol des eigenen Servers – der Katalog wird ausserhalb eines
        # Befehls geladen, der Kontext hilft hier also nicht weiter.
        try:
            _c = connections.for_service(self.service_id) if self.service_id else None
        except Exception:  # noqa: BLE001
            _c = None
        sym = _cur_symbol(_c)
        for it in self.items:
            cls_list = _item_classnames(it)
            if not cls_list:
                continue
            is_bundle = len(cls_list) > 1
            name = str(it.get("name") or cls_list[0])
            self._by_key.setdefault(name.lower(), it)
            if not is_bundle:
                # Classname nur für Einzelitems als Key – Bundles würden echte Items verdecken
                self._by_key.setdefault(cls_list[0].lower(), it)
            self.by_category.setdefault(str(it.get("category", "Misc")), []).append(it)
            enabled = bool(it.get("enabled", True))
            flag  = "" if enabled else "🚫 "
            if is_bundle:
                label = f"{flag}{name} – {int(it.get('price', 0)):,} {sym} (Bundle · {len(cls_list)} items)"
            else:
                label = f"{flag}{name} – {int(it.get('price', 0)):,} {sym} ({it.get('category', 'Misc')})"
            search = " ".join([name.lower()] + [c.lower() for c in cls_list])
            self._ac_index.append((search, label[:100], name[:100], enabled))

    def find(self, key: str) -> Optional[Dict]:
        return self._by_key.get(key.strip().lower())

    def save(self) -> bool:
        """Persistiert Änderungen (/shop setprice, /shop enable) in die Katalogdatei."""
        data: Dict[str, Any] = {}
        try:
            with open(self.source, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            pass
        data["items"]    = self.items
        data["_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            with open(self.source, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.rebuild_index()
            return True
        except Exception as e:
            log.error(f"[SHOP] Konnte {self.source} nicht speichern: {e}")
            return False


def _catalog_of(interaction: discord.Interaction,
                server: Optional[str] = None) -> Optional["ShopCatalog"]:
    """Der Item-Katalog des gemeinten Servers.

    Es gibt bewusst KEINEN globalen Katalog mehr: ohne zugeordneten Server
    gibt es auch keine Items, sonst sähe jede Guild den Shop des Betreibers.
    """
    conn, _fehler = _conn_waehlen(interaction, server)
    return conn.catalog if conn is not None else None


async def _require_catalog(interaction: discord.Interaction,
                           server: Optional[str] = None) -> Optional["ShopCatalog"]:
    """Wie ``_catalog_of``, antwortet aber selbst, wenn die Auswahl scheitert."""
    conn, fehler = _conn_waehlen(interaction, server)
    if conn is not None:
        return conn.catalog
    msg = fehler or PREMIUM_MISSING_TEXT
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return None

def _item_classnames(it: Dict) -> List[str]:
    """Classname-Liste eines Shop-Items: Bundle ("classnames") oder Einzelitem ("classname")."""
    cls = it.get("classnames")
    if isinstance(cls, list) and cls:
        return [str(c) for c in cls]
    cn = it.get("classname")
    return [str(cn)] if cn else []

def _shop_line(it: Dict) -> str:
    """Eine Katalog-Zeile für /shop list – Bundles zeigen ihren Inhalt kompakt."""
    cls_list = _item_classnames(it)
    name = str(it.get("name") or (cls_list[0] if cls_list else "?"))
    extra = ""
    if len(cls_list) > 1:
        inhalt = " + ".join(cls_list)
        if len(inhalt) > 60:
            inhalt = inhalt[:57] + "…"
        extra = f"Bundle: {inhalt}, "
    return (f"• **{name}** — {_fmt_money(int(it.get('price', 0)))} "
            f"*({extra}max {int(it.get('max_amount_per_buy', 1))}/buy)*")

def _make_item_autocomplete(only_enabled: bool):
    """Autocomplete über den vorberechneten Index (max. 25 Treffer, Substring-Suche)."""
    async def _ac(interaction: discord.Interaction,
                  current: str) -> List[app_commands.Choice[str]]:
        conns = _ac_conns(interaction)
        if not conns:
            return []            # kein zugeordneter Server → nichts vorschlagen
        mehrere = len(conns) > 1
        cur = current.strip().lower()
        out: List[app_commands.Choice] = []
        gesehen = set()
        for c in conns:
            cat = c.catalog
            if cat is None:
                continue
            for search, label, value, enabled in cat._ac_index:
                if only_enabled and not enabled:
                    continue
                if cur and cur not in search:
                    continue
                if value in gesehen:
                    continue      # gleiches Item in beiden Katalogen → einmal zeigen
                gesehen.add(value)
                out.append(app_commands.Choice(
                    name=f"{label} – {c.name}"[:100] if mehrere else label,
                    value=value))
                if len(out) >= 25:
                    return out
        return out
    return _ac

_shop_item_autocomplete = _make_item_autocomplete(only_enabled=False)   # Admin-Befehle
_shop_buy_autocomplete  = _make_item_autocomplete(only_enabled=True)    # /buy

async def _shop_category_autocomplete(interaction: discord.Interaction,
                                      current: str) -> List[app_commands.Choice[str]]:
    conns = _ac_conns(interaction)
    cur = current.strip().lower()
    zaehler: Dict[str, int] = {}
    for c in conns:
        katalog = c.catalog
        if katalog is None:
            continue
        for cat in katalog.by_category:
            if cur and cur not in cat.lower():
                continue
            n = sum(1 for i in katalog.by_category[cat] if i.get("enabled", True))
            if n:
                zaehler[cat] = zaehler.get(cat, 0) + n
    out: List[app_commands.Choice] = []
    for cat in sorted(zaehler):
        out.append(app_commands.Choice(name=f"{cat} ({zaehler[cat]} items)"[:100],
                                       value=cat))
        if len(out) >= 25:
            break
    return out


class ShopListView(discord.ui.View):
    """Einfache Seiten-Navigation für den Item-Katalog."""

    def __init__(self, pages: List[discord.Embed]):
        super().__init__(timeout=180)
        self.pages = pages
        self.index = 0
        self._sync()

    def _sync(self):
        self.prev_page.disabled = self.index <= 0
        self.next_page.disabled = self.index >= len(self.pages) - 1

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary)
    async def prev_page(self, itx: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync()
        await itx.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, itx: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._sync()
        await itx.response.edit_message(embed=self.pages[self.index], view=self)


shop_group = app_commands.Group(name="shop", description="🛒 Item shop")

@shop_group.command(name="list", description="🛒 Show the shop catalog (all items or one category)")
@app_commands.describe(category="Category to list – leave empty for the overview",
                       server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def shop_list(interaction: discord.Interaction, category: Optional[str] = None,
                    server: Optional[str] = None):
    katalog = await _require_catalog(interaction, server=server)
    if katalog is None:
        return
    enabled_items = [it for it in katalog.items if it.get("enabled", True)]
    if not enabled_items:
        return await interaction.response.send_message(
            "🛒 The shop is currently empty. Admins: put your `types.xml` next to the bot "
            "and restart (the catalog is generated automatically), or use `/add shopitem`.",
            ephemeral=True)

    if category is not None:
        # Eine Kategorie komplett auflisten
        wanted = category.strip().lower()
        match = next((c for c in katalog.by_category if c.lower() == wanted), None)
        items = ([i for i in katalog.by_category.get(match, []) if i.get("enabled", True)]
                 if match else [])
        if not items:
            return await interaction.response.send_message(
                f"❌ No category `{category}` – pick one from the autocomplete list.",
                ephemeral=True)
        lines = [_shop_line(it)
                 for it in sorted(items, key=lambda i: str(i.get("name", "")))]
        title = f"🛒 Item Shop – {match}"
    elif len(enabled_items) > 45:
        # Groß-Katalog (generierte shop_items.json): Kategorie-Übersicht statt 1700 Zeilen
        lines = []
        for cat in sorted(katalog.by_category):
            items = [i for i in katalog.by_category[cat] if i.get("enabled", True)]
            if not items:
                continue
            prices = [int(i.get("price", 0)) for i in items]
            lines.append(f"**{cat}** — {len(items)} items · "
                         f"{_fmt_money(min(prices))} – {_fmt_money(max(prices))}")
        lines.append("")
        lines.append("Use `/shop list category:<name>` to browse the items.")
        title = "🛒 Item Shop – Categories"
    else:
        # Kleiner Katalog: komplette Liste, nach Kategorie gruppiert
        by_cat: Dict[str, List[Dict]] = {}
        for it in enabled_items:
            by_cat.setdefault(it.get("category", "Misc"), []).append(it)
        lines = []
        for cat in sorted(by_cat):
            lines.append(f"__**{cat}**__")
            for it in sorted(by_cat[cat], key=lambda i: str(i.get("name", ""))):
                lines.append(_shop_line(it))
        title = "🛒 Item Shop"

    # In Seiten à 15 Zeilen aufteilen
    per_page = 15
    chunks = [lines[i:i + per_page] for i in range(0, len(lines), per_page)]
    pages: List[discord.Embed] = []
    for i, chunk in enumerate(chunks):
        e = discord.Embed(
            title=title,
            description="\n".join(chunk),
            color=0x5865F2)
        e.set_footer(text=(f"Page {i + 1}/{len(chunks)} · "
                           f"Buy with /buy <item> <amount> <x> <z> · "
                           f"Items spawn after the next server restart"))
        pages.append(e)

    if len(pages) == 1:
        return await interaction.response.send_message(embed=pages[0])
    await interaction.response.send_message(embed=pages[0], view=ShopListView(pages))

shop_list.autocomplete("category")(_shop_category_autocomplete)


@shop_group.command(name="pending", description="📦 Show purchases waiting for delivery (admin)")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def shop_pending(interaction: discord.Interaction, server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    _conn, _fehler = _conn_waehlen(interaction, server)
    if _conn is None:
        return await interaction.response.send_message(
            _fehler or PREMIUM_MISSING_TEXT, ephemeral=True)
    rows = db.pending_purchases(guild_id=interaction.guild_id,
                                service_id=_conn.service_id)
    if not rows:
        return await interaction.response.send_message(
            "✅ No pending deliveries.", ephemeral=True)
    lines = []
    for r in rows[:25]:
        lines.append(f"`#{r['id']}` <@{r['user_id']}> — **{r['amount']}× {r['item_name']}** "
                     f"({_fmt_money(int(r['total_price']))}) · <t:{int(r['created_at'])}:R>")
    embed = discord.Embed(
        title=f"📦 Pending deliveries ({len(rows)})",
        description="\n".join(lines),
        color=0xF39C12)
    embed.set_footer(text="Items spawn at the next server restart · /shop cleanup to finish manually")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@shop_group.command(name="cleanup",
                    description="🧹 Mark ALL pending purchases as delivered and clean cfgEffectArea.json (admin)")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def shop_cleanup(interaction: discord.Interaction, server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    await interaction.response.defer(ephemeral=True)
    # Auf dem EIGENEN Server aufräumen – sonst würde ein Admin die offenen
    # Käufe aller anderen Kunden als geliefert markieren.
    _conn = await _require_conn(interaction, need_ftp=True, server=server)
    if _conn is None:
        return
    if not _conn.shop:
        return await interaction.followup.send("❌ Shop manager not ready yet.", ephemeral=True)
    rows = db.pending_purchases(guild_id=interaction.guild_id,
                                service_id=_conn.service_id)
    ids, names = [], []
    for r in rows:
        ids.append(int(r["id"]))
        try:
            names.extend(json.loads(r["area_names"] or "[]"))
        except Exception:
            pass
    if names:
        ok = await _conn.shop.remove_area_entries(names)
        if not ok:
            return await interaction.followup.send(
                "❌ Could not clean cfgEffectArea.json (FTP/parse error) – nothing was changed.",
                ephemeral=True)
    if ids:
        db.mark_delivered(ids)
        _conn.shop.cleanup_retry_needed = False

    # Selbstheilung: verwaiste SHOP_-Einträge ohne zugehörigen Kauf entfernen
    orphans = await _conn.shop.sweep_orphans()

    parts = []
    if ids:
        parts.append(f"**{len(ids)}** purchase(s) marked as delivered, "
                     f"**{len(names)}** entries removed from cfgEffectArea.json.")
    if orphans > 0:
        parts.append(f"**{orphans}** orphaned `SHOP_` entr{'y' if orphans == 1 else 'ies'} removed.")
    elif orphans < 0:
        parts.append("⚠️ Orphan sweep failed (FTP/parse error).")
    if not parts:
        parts.append("Nothing to clean – no pending deliveries and no orphaned entries.")
    await interaction.followup.send("🧹 " + " ".join(parts), ephemeral=True)


@shop_group.command(name="check",
                    description="🩺 Delivery-Diagnose: prüft cfgEffectArea.json & repariert fehlende Einträge (admin)")
@app_commands.describe(server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def shop_check(interaction: discord.Interaction, server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    await interaction.response.defer(ephemeral=True)
    _conn = await _require_conn(interaction, need_ftp=True, server=server)
    if _conn is None:
        return
    if not _conn.shop:
        return await interaction.followup.send("❌ Shop manager not ready yet.", ephemeral=True)
    rep = await _conn.shop.check_and_heal()

    embed = discord.Embed(title="🩺 Shop-Delivery-Diagnose", color=0x5865F2)
    path = rep.get("path")
    embed.add_field(name="Pfad", value=f"`{path}`" if path else
                    "❌ Nicht konfiguriert – `/ftp_scan` ausführen oder "
                    "`cfg_effect_area_path` in config.json setzen.", inline=False)
    status = rep.get("status")
    if status == "no_path":
        embed.colour = 0xE74C3C
        return await interaction.followup.send(embed=embed, ephemeral=True)
    if status == "error":
        embed.colour = 0xE74C3C
        embed.add_field(name="Datei", value="❌ FTP-Lesefehler – Verbindung prüfen "
                        "(`/ftp_status`), dann erneut versuchen.", inline=False)
        return await interaction.followup.send(embed=embed, ephemeral=True)
    if status == "parse_error":
        embed.colour = 0xE74C3C
        embed.add_field(name="Datei", value=f"❌ Ungültiges JSON: `{rep.get('error')}`",
                        inline=False)
        return await interaction.followup.send(embed=embed, ephemeral=True)

    file_line = ("✅ lesbar, gültiges JSON" if status == "ok" else
                 "⚠️ existiert noch nicht – wird beim ersten Kauf angelegt")
    embed.add_field(name="Datei", value=file_line, inline=False)
    embed.add_field(name="Einträge",
                    value=(f"{rep.get('areas_total', 0)} gesamt · "
                           f"{rep.get('shop_entries', 0)} SHOP_ · "
                           f"{rep.get('vanilla_entries', 0)} Vanilla"), inline=False)

    pending = rep.get("pending", 0)
    healed  = rep.get("healed_entries", 0)
    if healed:
        ok_write = rep.get("heal_written", False)
        heal_txt = (f"🔧 **{healed}** fehlende Einträge aus "
                    f"{len(rep.get('healed_purchases', []))} offenen Käufen wieder "
                    f"eingetragen" + ("" if ok_write else " – ❌ FTP-Schreibfehler!"))
        embed.add_field(name="Self-Heal", value=heal_txt, inline=False)
        if not ok_write:
            embed.colour = 0xE74C3C
    embed.add_field(name="Offene Käufe", value=str(pending), inline=True)

    if pending and not _conn.get("auto_restart_after_purchase", False):
        embed.add_field(
            name="Hinweis",
            value=("`auto_restart_after_purchase` ist **aus** – Items spawnen erst "
                   "beim nächsten (manuellen/geplanten) Server-Neustart."), inline=False)
    last = rep.get("last_restart_at") or 0
    embed.set_footer(text=("Letzter erkannter Server-Neustart: " +
                           (f"vor {int((time.time() - last) // 60)} Min"
                            if last else "seit Bot-Start keiner")))
    await interaction.followup.send(embed=embed, ephemeral=True)


@shop_group.command(name="enable", description="🔧 Enable or disable a shop item (admin)")
@app_commands.describe(item="Item name", enabled="True = buyable, False = hidden from the shop",
                       server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def shop_enable(interaction: discord.Interaction, item: str, enabled: bool,
                      server: Optional[str] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    katalog = await _require_catalog(interaction, server=server)
    if katalog is None:
        return
    it = katalog.find(item)
    if not it:
        return await interaction.response.send_message(
            f"❌ Item `{item}` not found in the shop catalog.", ephemeral=True)
    it["enabled"] = bool(enabled)
    saved = katalog.save()
    state = "✅ **enabled**" if enabled else "🚫 **disabled**"
    note  = "" if saved else f"\n⚠️ Could not persist to `{katalog.source}` – change is in memory only."
    await interaction.response.send_message(
        f"🔧 **{it['name']}** is now {state}.{note}", ephemeral=True)

shop_enable.autocomplete("item")(_shop_item_autocomplete)

shop_list.autocomplete("server")(_server_autocomplete)
shop_pending.autocomplete("server")(_server_autocomplete)
shop_cleanup.autocomplete("server")(_server_autocomplete)
shop_check.autocomplete("server")(_server_autocomplete)
shop_enable.autocomplete("server")(_server_autocomplete)

bot.tree.add_command(shop_group)


# ══════════════════════════════════════════════════════════════
#  /edit shopitem – Classnames, Preis, Name usw. eines Items ändern
# ══════════════════════════════════════════════════════════════
edit_group = app_commands.Group(name="edit", description="✏️ Edit entries of the shop catalog")

@edit_group.command(name=app_commands.locale_str("ankuendigung"),
                    description="✏️ Bearbeitet eine geplante Ankündigung (Nachricht/Bild)")
@app_commands.describe(index="Nummer der Ankündigung (siehe /liste)")
async def edit_ankuendigung(interaction: discord.Interaction, index: int):
    if not _is_admin(interaction):
        return await _deny(interaction)

    pos = await _ann_position(interaction, index)
    if pos is None:
        return

    modal = EditAnnouncementModal(pos)

    await interaction.response.send_modal(modal)

bot.tree.add_command(edit_group)



@bot.tree.command(
    name="buy",
    description="🛒 Buy an item – it spawns at your coordinates after the next server restart")
@app_commands.describe(
    item="Item name (pick from the autocomplete list)",
    amount="How many to buy",
    x="iZurvive X coordinate (East – the FIRST number on iZurvive)",
    z="iZurvive Y coordinate (North – the SECOND number on iZurvive)",
    y="Height / altitude (OPTIONAL – leave empty for default ground level)",
    server="Welcher Nitrado-Server? (nur nötig, wenn mehrere verbunden sind)")
async def cmd_buy(interaction: discord.Interaction, item: str,
                  amount: app_commands.Range[int, 1], x: float, z: float,
                  y: Optional[float] = None, server: Optional[str] = None):
    if not await _require_guild(interaction):
        return
    gid, uid = interaction.guild_id, interaction.user.id

    # ── 1. Server EINMAL aufloesen – Katalog, Lieferung, Neustart und
    #       Karte muessen zwingend derselbe Server sein. Frueher wurde hier
    #       viermal unabhaengig aufgeloest: der Katalog kam von A, das Item
    #       spawnte auf B und neu gestartet wurde ein dritter Server.
    _conn, _fehler = _conn_waehlen(interaction, server)
    if _conn is None:
        return await interaction.response.send_message(
            _fehler or PREMIUM_MISSING_TEXT, ephemeral=True)
    katalog_conn = _conn
    katalog = _conn.catalog
    if katalog is None:
        return await interaction.response.send_message(
            PREMIUM_MISSING_TEXT, ephemeral=True)
    it = katalog.find(item)
    if not it or not it.get("enabled", True):
        return await interaction.response.send_message(
            f"❌ Item `{item}` is not available. Use `/shop list` to see the catalog.",
            ephemeral=True)
    # ── 1b. Rollen-Beschraenkung: leer heisst, alle duerfen kaufen ──
    #        Vor allen weiteren Pruefungen, damit niemand ueber die
    #        Fehlermeldungen erfaehrt, was er ohnehin nicht kaufen darf.
    noetig = _item_role_ids(it)
    if noetig and not (isinstance(interaction.user, discord.Member)
                       and _member_has_role_ids(interaction.user, noetig)):
        return await interaction.response.send_message(
            f"❌ Du hast nicht die erforderliche Rolle, um **{it['name']}** zu kaufen.\n"
            "Benötigt wird: " + ", ".join(f"<@&{r}>" for r in noetig),
            ephemeral=True)

    max_amount = int(it.get("max_amount_per_buy", 1))
    if amount > max_amount:
        return await interaction.response.send_message(
            f"❌ You can buy at most **{max_amount}× {it['name']}** per purchase.",
            ephemeral=True)
    cls_list = _item_classnames(it)
    if not cls_list:
        return await interaction.response.send_message(
            f"❌ Item `{it.get('name', item)}` has no classnames configured – "
            f"ask an admin to fix the catalog entry.", ephemeral=True)

    # ── 2. Koordinaten validieren (iZurvive: x=Ost, z=Nord) ───
    if not (0.0 <= x <= 20000.0 and 0.0 <= z <= 20000.0):
        return await interaction.response.send_message(
            "❌ Coordinates out of range. Enter the two iZurvive numbers as "
            "`x` (East) and `z` (North), e.g. `x: 4640` `z: 10350`.", ephemeral=True)
    # ACHTUNG Achsen-Mapping: cfgEffectArea Pos = [X, HÖHE, NORD]
    # → iZurvive-X → Pos[0], Höhe (y-Parameter) → Pos[1], iZurvive-Y → Pos[2]
    y_val = float(katalog_conn.get("default_pos_y", 0.0) or 0.0) if y is None else float(y)
    if not (-100.0 <= y_val <= 1000.0):
        return await interaction.response.send_message(
            "❌ Height `y` out of range (−100 … 1000). Leave it empty for ground level.",
            ephemeral=True)

    # ── 3. Preis prüfen (Vorprüfung, Abbuchung erst nach FTP-Erfolg) ──
    total = int(it.get("price", 0)) * int(amount)
    wallet, _bank = db.get_balance(gid, uid)
    if wallet < total:
        return await interaction.response.send_message(
            embed=_insufficient_embed(total, wallet), ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    # Auslieferung auf genau dem oben gewaehlten Server – hier wird nur noch
    # geprueft, ob er einsatzbereit ist, nicht neu aufgeloest.
    if _conn.api is None or _conn.ftp is None:
        return await interaction.followup.send(
            "❌ Für diesen Server fehlt der FTP-Zugang – ohne ihn kann nichts "
            "ausgeliefert werden.\n`/ftp_scan` versucht die Erkennung erneut.",
            ephemeral=True)
    if not _conn.shop:
        return await interaction.followup.send(
            "❌ Shop system is still starting up – try again in a moment.", ephemeral=True)

    # ── 4. Erst in cfgEffectArea.json schreiben ... ───────────
    ok, err, area_names = await _conn.shop.add_purchase_entries(
        cls_list, int(amount), x, y_val, z)
    if not ok:
        return await interaction.followup.send(
            embed=discord.Embed(title="❌ Purchase failed", description=err, color=0xE74C3C),
            ephemeral=True)

    # ── 5. ... dann Geld abbuchen (atomar). Bei Fehlschlag: Rollback ──
    if not db.try_spend_wallet(gid, uid, total):
        rollback_ok = await _conn.shop.remove_area_entries(area_names)   # Einträge zurückrollen
        if not rollback_ok:
            # Verwaiste Einträge würden bei jedem Neustart gratis spawnen → Admins warnen
            log.error(f"[SHOP] Rollback fehlgeschlagen – verwaiste Areas: {area_names}")
            warn = discord.Embed(
                title="⚠️ Orphaned shop entries",
                description=("A cancelled purchase could not be rolled back in "
                             "`cfgEffectArea.json`. Run `/shop cleanup` to remove the "
                             "orphaned entries, otherwise the items respawn on every restart."),
                color=0xE67E22)
            await _post_feed(gid, "shop_log", warn, service_id=_conn.service_id)
        wallet, _bank = db.get_balance(gid, uid)
        return await interaction.followup.send(
            embed=_insufficient_embed(total, wallet), ephemeral=True)

    # ── 6. Kauf als pending speichern ─────────────────────────
    purchase_id = db.create_purchase(
        _conn.service_id, gid, uid, str(interaction.user), it["name"],
        "+".join(cls_list),
        int(amount), total, x, y_val, z, area_names)

    # ── 7. Auto-Restart oder Hinweis auf nächsten Neustart ────
    if _conn.get("auto_restart_after_purchase", False):
        _conn.shop.schedule_auto_restart()
        cooldown = int(_conn.get("restart_cooldown_seconds", 300) or 300)
        delivery_info = (f"🔄 A server restart has been scheduled – your items will spawn "
                         f"in about **{max(5, cooldown)} seconds** (plus boot time).")
    else:
        delivery_info = "⏳ Your items will spawn at the **next scheduled server restart**."

    # ── 8. Bestätigung an den Käufer (Ort + iZurvive-Link) ────
    map_name = _conn.get("map_name", "ChernarusPlus")
    loc_url  = _izurvive_url(x, z, map_name)
    near     = _nearest_location(x, z, map_name)
    near_txt = f"\n*(Near {near})*" if near else ""
    wallet, _bank = db.get_balance(gid, uid)

    embed = discord.Embed(
        title="🛒 Purchase successful",
        description=f"You bought **{amount}× {it['name']}** for **{_fmt_money(total)}**.",
        color=0x2ECC71)
    embed.add_field(name="📍 Spawn location",
                    value=f"[{x:.1f} / {z:.1f}]({loc_url}){near_txt}", inline=False)
    embed.add_field(name="🚚 Delivery", value=delivery_info, inline=False)
    embed.add_field(name="👛 Wallet",   value=_fmt_money(wallet), inline=True)
    embed.set_footer(text=f"Purchase #{purchase_id}")
    await interaction.followup.send(embed=embed, ephemeral=True)

    # ── 9. Kauf in den shop_log-Feed posten ───────────────────
    feed = discord.Embed(
        title="🛒 SHOP PURCHASE",
        description=f"{interaction.user.mention} bought **{amount}× {it['name']}**",
        color=0x3498DB)
    feed.add_field(name="Price",    value=_fmt_money(total), inline=True)
    feed.add_field(name="Location", value=f"[{x:.1f} / {z:.1f}]({loc_url}){near_txt}", inline=True)
    feed.add_field(name="Status",   value="⏳ pending (spawns after restart)", inline=False)
    feed.set_footer(text=(f"Purchase #{purchase_id} · " + "+".join(cls_list))[:100])
    await _post_feed(gid, "shop_log", feed, service_id=_conn.service_id)

cmd_buy.autocomplete("item")(_shop_buy_autocomplete)
cmd_buy.autocomplete("server")(_server_autocomplete)


# ══════════════════════════════════════════════════════════════
#  KATALOG-GENERATOR – shop_items.json aus der types.xml erzeugen
#  (in den Bot integriert, damit alles in EINER Datei bleibt.
#   Läuft beim Start automatisch, wenn shop_items.json fehlt und
#   eine types.xml im Bot-Ordner liegt.)
# ══════════════════════════════════════════════════════════════
TYPES_XML_FILE = "types.xml"

# Keine kaufbaren Items → gar nicht in den Katalog aufnehmen
_GEN_EXCLUDED = {"Animals", "Infected", "Static Objects", "Land Items", "UNCATEGORIZED"}
# Aufnehmen, aber deaktiviert: Fahrzeug-Spawn über cfgEffectArea ist unzuverlässig
# (fehlende Anbauteile/Persistenz) – Admins können einzelne per /shop enable freischalten
_GEN_DISABLED = {"Vehicles"}
_GEN_MAX_AMOUNT = {
    "Ammo": 10, "Food": 10, "Medical Items": 10, "Supplies": 10,
    "Seeds": 10, "Plants": 10,
    "Firearms": 2, "Optics": 2,
    "Bags": 3, "Vests": 3,
    "Vehicles": 1,
}
_GEN_RE_CATEGORY = re.compile(r"<!--#+\s*(.+?)\s*#+-->")
_GEN_RE_TYPE     = re.compile(r'<type\s+name="([^"]+)"')


def _gen_prettify(classname: str) -> str:
    """Anzeigename aus Classname: 'Armband_BabyDeer' → 'Armband Baby Deer'."""
    s = classname.replace("_", " ")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)      # klein/Ziffer → GROSS
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)    # GROSS-Serie → Wortanfang
    return re.sub(r"\s+", " ", s).strip()


def generate_shop_items_from_types(input_path: str = TYPES_XML_FILE,
                                   output_path: Optional[str] = None,
                                   conn: Optional["ServerConnection"] = None) -> Optional[int]:
    """Erzeugt einen Item-Katalog aus einer types.xml (DayZBoosterZ-Format).
    Kategorie-Preise kommen aus dem jeweiligen Server (``conn``), sonst aus
    shop_category_prices in config.json.
    Per /add shopitem angelegte Items ("custom": true) werden übernommen.
    Gibt die Item-Anzahl zurück, None bei Fehler."""
    out_file = output_path or str(cfg.config.get("shop_items_file") or "shop_items.json")
    if not os.path.exists(input_path):
        return None

    quelle        = conn if conn is not None else cfg.config
    prices        = quelle.get("shop_category_prices") or {}
    default_price = int(quelle.get("shop_default_price", 100) or 100)

    # types.xml zeilenweise parsen: Kategorie-Kommentare + <type name="...">
    category = "UNCATEGORIZED"
    entries: List[Tuple[str, str]] = []
    seen: set = set()
    try:
        with open(input_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m_cat = _GEN_RE_CATEGORY.search(line)
                if m_cat:
                    category = m_cat.group(1).strip()
                    continue
                m_type = _GEN_RE_TYPE.search(line)
                if m_type:
                    cn = m_type.group(1).strip()
                    if cn.lower() not in seen:      # Duplikate überspringen
                        seen.add(cn.lower())
                        entries.append((cn, category))
    except Exception as e:
        log.error(f"[GEN] types.xml nicht lesbar: {e}")
        return None
    if not entries:
        log.error("[GEN] Keine <type name=...>-Eintraege in der types.xml gefunden.")
        return None

    items: List[Dict] = []
    name_counts: Dict[str, int] = {}
    for cn, cat in entries:
        if cat in _GEN_EXCLUDED:
            continue
        nm = _gen_prettify(cn)
        name_counts[nm.lower()] = name_counts.get(nm.lower(), 0) + 1
        items.append({
            "name":               nm,
            "classname":          cn,
            "price":              int(prices.get(cat, default_price)),
            "category":           cat,
            "enabled":            cat not in _GEN_DISABLED,
            "max_amount_per_buy": int(_GEN_MAX_AMOUNT.get(cat, 5)),
        })
    # Namens-Kollisionen eindeutig machen (Anzeigename ist der Lookup-Schlüssel)
    for it in items:
        if name_counts.get(it["name"].lower(), 0) > 1:
            it["name"] = f"{it['name']} ({it['classname']})"

    # Bewusst entfernte Items bleiben entfernt – sonst holt jedes "Items vom
    # Server laden" den ganzen aufgeraeumten Katalog wieder zurueck. Erst NACH
    # der Kollisionsauflösung, weil die Merkliste den angezeigten Namen kennt.
    if conn is not None:
        gestrichen = {str(n).strip().lower() for n in _geloeschte_items(conn)}
        if gestrichen:
            vorher = len(items)
            items = [i for i in items
                     if i["name"].lower() not in gestrichen
                     and str(i.get("classname", "")).lower() not in gestrichen]
            if vorher != len(items):
                log.info(f"[GEN] {vorher - len(items)} zuvor entfernte Items "
                         f"nicht wieder aufgenommen.")

    items.sort(key=lambda i: (i["category"].lower(), i["name"].lower()))

    # Manuell angelegte Items (/add shopitem, "custom": true) aus einer
    # bestehenden Datei übernehmen – sonst gehen sie beim Regenerieren verloren
    if os.path.exists(out_file):
        try:
            with open(out_file, "r", encoding="utf-8") as f:
                old = json.load(f)
            old_items = old.get("items") if isinstance(old, dict) else old
            if isinstance(old_items, list):
                gen_names = {i["name"].lower() for i in items}
                keep = [i for i in old_items
                        if isinstance(i, dict) and i.get("custom")
                        and str(i.get("name", "")).lower() not in gen_names]
                items.extend(keep)
                # Rollen-Beschraenkungen an erzeugten Items ueberleben das
                # Neuaufbauen: sie stehen nicht in der types.xml und waeren
                # sonst bei jedem "Items vom Server laden" weg.
                # Zuordnung ueber Name UND Classname – der Anzeigename wird aus
                # dem Classname erzeugt und kann sich mit den Regeln aendern,
                # der Classname bleibt.
                alte_rollen: Dict[str, List] = {}
                for i in old_items:
                    if not (isinstance(i, dict) and i.get("role_ids")):
                        continue
                    for s in [str(i.get("name", ""))] + _item_classnames(i):
                        if s.strip():
                            alte_rollen.setdefault(s.strip().lower(), i["role_ids"])
                if alte_rollen:
                    for i in items:
                        if i.get("role_ids"):
                            continue
                        rollen = (alte_rollen.get(str(i.get("name", "")).lower())
                                  or alte_rollen.get(str(i.get("classname", "")).lower()))
                        if rollen:
                            i["role_ids"] = rollen
        except Exception as e:
            log.warning(f"[GEN] Bestehende {out_file} nicht lesbar ({e}) - "
                        f"Custom-Items nicht uebernommen.")

    out = {
        "_generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "_source":    os.path.basename(input_path),
        "_note":      ("Automatisch aus types.xml generiert. Neu erzeugen: diese Datei "
                       "löschen und den Bot neu starten. Preise: /shop setprice oder "
                       "/edit shopitem; Kategorie-Preise: shop_category_prices in config.json."),
        "items": items,
    }
    try:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"[GEN] Konnte {out_file} nicht schreiben: {e}")
        return None
    log.info(f"[GEN] Katalog generiert: {len(items)} Items -> {out_file}")
    return len(items)


def _types_xml_kandidaten(conn: "ServerConnection") -> List[str]:
    """Wo die types.xml auf dem FTP dieses Servers liegen kann."""
    pfade: List[str] = []
    eigener = str(conn.get("types_xml_path") or "").strip()
    if eigener:
        pfade.append(eigener)
    mission = str(conn.get("ftp_mission_dir") or "").rstrip("/")
    if mission:
        pfade += [f"{mission}/db/types.xml", f"{mission}/types.xml"]
    # Ohne erkanntes Mission-Verzeichnis die üblichen Ablagen probieren
    if not mission:
        karte = str(conn.get("map_name") or "").strip()
        if karte:
            pfade.append(f"/dayzxb_missions/dayzOffline.{karte.lower()}/db/types.xml")
            pfade.append(f"/dayzstandalone/mpmissions/dayzOffline.{karte.lower()}/db/types.xml")
    # Duplikate raus, Reihenfolge behalten
    gesehen, out = set(), []
    for p in pfade:
        if p not in gesehen:
            gesehen.add(p)
            out.append(p)
    return out


async def katalog_von_server_holen(conn: "ServerConnection") -> Tuple[Optional[int], str]:
    """Holt die ``types.xml`` **dieses** Servers per FTP und baut daraus seinen Katalog.

    Jeder Kunde bekommt damit genau die Items, die auf seinem eigenen Server
    existieren – ein gemeinsamer Katalog wuerde fremde Classnames anbieten,
    die dort gar nicht spawnen koennen.
    Rueckgabe: ``(Item-Anzahl, Meldung)``; Anzahl ``None`` bei Fehlschlag.
    """
    if conn.ftp is None:
        return None, ("Für diesen Server ist kein FTP-Zugang eingerichtet – "
                      "ohne ihn ist die types.xml nicht erreichbar.")
    kandidaten = _types_xml_kandidaten(conn)
    if not kandidaten:
        return None, ("Das Mission-Verzeichnis ist noch unbekannt. "
                      "`/ftp_scan` sucht es erneut.")

    loop = asyncio.get_running_loop()
    roh, gefunden = None, ""
    for pfad in kandidaten:
        roh = await loop.run_in_executor(None, conn.ftp.read_file, pfad)
        if roh and "<type" in roh:
            gefunden = pfad
            break
        roh = None
    if roh is None:
        return None, ("Keine types.xml gefunden. Geprüft: "
                      + ", ".join(f"`{p}`" for p in kandidaten))

    tmp = f"types_{conn.service_id or 'server'}.xml"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(roh)
    except Exception as e:  # noqa: BLE001
        return None, f"Konnte die geladene types.xml nicht zwischenspeichern: {e}"

    katalog = conn.catalog
    n = await loop.run_in_executor(
        None, functools.partial(generate_shop_items_from_types, tmp, katalog.path, conn))
    try:
        os.remove(tmp)
    except OSError:
        pass
    if n is None:
        return None, f"`{gefunden}` konnte nicht ausgewertet werden."
    katalog.load()
    log.info(f"[SHOP] {conn.name}: Katalog aus {gefunden} erzeugt ({n} Items).")
    return n, f"{n} Items aus `{gefunden}` übernommen."


# ══════════════════════════════════════════════════════════════
#  Auto-Erkennung beim Start: Service-ID, FTP-Zugang & Karte
#  werden über den Nitrado-Token ermittelt und in config.json
#  als Cache gespeichert – der Nutzer trägt nur noch bot_token,
#  nitrado_token und guild_ids ein.
# ══════════════════════════════════════════════════════════════
def _cached_ftp_available(reason: str) -> bool:
    """Fallback: Sind aus einem früheren Start noch gültige Werte in
    config.json gecacht, kann der Bot trotz fehlgeschlagener Erkennung starten."""
    if all(str(cfg.config.get(k) or "").strip()
           for k in ("service_id", "ftp_host", "ftp_user", "ftp_password")):
        print(f"⚠️  {reason} – verwende gespeicherte Zugangsdaten aus config.json.")
        return True
    return False


def _apply_gameserver_info(info: Dict, conn: Optional[ServerConnection] = None) -> None:
    """Schreibt FTP-Zugang, Karte und Server-IP aus den Nitrado-Infos.

    Mit Verbindung landen die Werte dort – das ist der Normalfall. Nur ohne
    Verbindung wird noch die globale config.json beschrieben; FTP-Zugangsdaten
    eines Kunden duerfen dort nicht liegen, weil sie sonst anderen Kunden als
    Rueckfallebene dienen wuerden.
    """
    ziel = conn.data if conn is not None else cfg.config
    ftp = NitradoAPI.extract_ftp_credentials(info)
    if ftp:
        # Immer überschreiben – fängt von Nitrado geänderte Passwörter ab
        ziel["ftp_host"]     = ftp["host"]
        ziel["ftp_port"]     = ftp["port"]
        ziel["ftp_user"]     = ftp["user"]
        ziel["ftp_password"] = ftp["password"]
        log.info(f"[NITRADO] ✅ FTP-Zugang automatisch erkannt: "
                 f"{ftp['user']}@{ftp['host']}:{ftp['port']}")
    else:
        log.warning("[NITRADO] ⚠️ Keine FTP-Zugangsdaten in den "
                    "Gameserver-Infos gefunden.")

    detected_map = NitradoAPI.extract_map(info)
    if detected_map and detected_map != ziel.get("map_name"):
        log.info(f"[NITRADO] 🗺️ Aktuelle Karte erkannt: {detected_map} "
                 f"(vorher: {ziel.get('map_name')})")
        ziel["map_name"] = detected_map

    # Bonus: Server-IP/Query-Port nur befüllen, wenn noch nicht gesetzt
    if not ziel.get("server_ip") and info.get("ip"):
        ziel["server_ip"] = str(info["ip"])
        qport = (info.get("query") or {}).get("connect_port") or info.get("query_port")
        if qport:
            try:
                ziel["query_port"] = int(qport)
            except (TypeError, ValueError):
                pass
        log.info(f"[NITRADO] Server-IP automatisch gesetzt: {info['ip']}")


async def auto_detect_from_nitrado() -> bool:
    """Erkennt service_id, FTP-Zugangsdaten und die aktuelle Karte über den
    Nitrado-Token und speichert sie in config.json. Gibt True zurück, wenn
    der Bot mit gültigen FTP-Zugangsdaten starten kann."""
    # Platzhalter aus alten config.json-Versionen wie leere Felder behandeln
    for key in ("service_id", "ftp_host", "ftp_user", "ftp_password"):
        val = str(cfg.config.get(key) or "")
        if "HIER" in val or "EINTRAGEN" in val:
            cfg.config[key] = ""

    api = NitradoAPI(
        token=cfg.config["nitrado_token"],
        service_id=str(cfg.config.get("service_id") or "").strip(),
        base=cfg.config.get("nitrado_api_base", "https://api.nitrado.net"),
    )
    try:
        # ── 1. Service-ID: manuell gesetzter Wert hat Vorrang ────
        if not api.service_id:
            sid = await api.detect_service()
            if not sid:
                return _cached_ftp_available("Service-Erkennung fehlgeschlagen")
            cfg.config["service_id"] = sid
            cfg.save_config()

        # ── 2. FTP-Zugang + Karte aus den Gameserver-Infos ───────
        info = await api.get_info()
        if not info:
            return _cached_ftp_available(
                f"Nitrado-API nicht erreichbar (Service {api.service_id})")

        _apply_gameserver_info(info)
        cfg.save_config()

        if all(str(cfg.config.get(k) or "").strip()
               for k in ("ftp_host", "ftp_user", "ftp_password")):
            return True
        return _cached_ftp_available("FTP-Zugangsdaten unvollständig")
    finally:
        await api.close()


# ══════════════════════════════════════════════════════════════
#  WEB-DASHBOARD  (aiohttp-Web-App im Event-Loop des Bots)
# ══════════════════════════════════════════════════════════════
#  Bot und Dashboard laufen in EINEM Prozess und greifen auf dieselben
#  Live-Objekte zu (cfg, db, catalog, bot, bot.nitrado, bot.ftp,
#  bot.parser.player_positions). Es gibt keinen zweiten Prozess und keine
#  doppelte Datenhaltung – jede Änderung im Dashboard wirkt sofort im
#  laufenden Bot.
#
#  Die Frontend-Dateien (index.html, app.js, styles.css, map.js), die
#  Leaflet-Bibliothek und die Ortslisten liegen weiter unten in
#  _EMBEDDED_ASSETS komprimiert (zlib + base64) eingebettet. Beim Start
#  werden sie nach dashboard_web/ geschrieben, falls sie fehlen – vorhandene
#  Dateien bleiben unberührt. Ist das Verzeichnis nicht beschreibbar, werden
#  sie direkt aus dem Speicher ausgeliefert.

_DASH_BOUND = False

_DASH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_web")
_DASH_STATIC = os.path.join(_DASH_DIR, "static")

# Entpackte Assets (Cache + Fallback, wenn die Platte nicht beschreibbar ist)
_asset_cache: Dict[str, bytes] = {}


async def _dash_run(func, *args):
    """Blockierende Bot-Funktionen (SQLite/FTP) im Executor ausführen."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


def _asset_from_memory(rel: str) -> Optional[bytes]:
    """Eingebettetes Asset entpacken (gecacht). None, wenn nicht eingebettet."""
    if rel in _asset_cache:
        return _asset_cache[rel]
    blob = _EMBEDDED_ASSETS.get(rel)
    if not blob:
        return None
    try:
        data = zlib.decompress(base64.b64decode(blob))
    except Exception as e:  # noqa: BLE001
        dash_log.error(f"[DASHBOARD] Asset {rel} nicht entpackbar: {e}")
        return None
    _asset_cache[rel] = data
    return data


def _asset_path(rel: str) -> Optional[str]:
    """Sicherer Pfad unter dashboard_web/static (verhindert Verzeichnis-Ausbruch)."""
    full = os.path.normpath(os.path.join(_DASH_STATIC, rel))
    base = os.path.normpath(_DASH_STATIC)
    if full != base and not full.startswith(base + os.sep):
        return None
    return full


def _read_asset(rel: str) -> Optional[bytes]:
    """Asset lesen: Platte zuerst, dann eingebetteter Speicher."""
    path = _asset_path(rel)
    if path and os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            pass
    return _asset_from_memory(rel)


# Verweise auf eigene Frontend-Dateien in der index.html.
_ASSET_URL_RE = re.compile(rb'(/(?:static|vendor)/[A-Za-z0-9_./-]+\.(?:js|css))')


def _mit_versionsstempel(html: bytes) -> bytes:
    """Jeder Frontend-Datei in der index.html ihre Pruefsumme anhaengen.

    Hinter einem CDN nuetzt ein ``no-cache`` vom Server wenig: Cloudflare
    schreibt seine eigene Haltbarkeit darueber (``max-age=14400``) und
    liefert stundenlang die alte Datei aus – der Bot wird dabei nicht einmal
    gefragt. Genau daran hing eine halb aufgebaute Feeds-Seite: neues
    Backend, alte app.js.

    ``/static/app.js?v=<pruefsumme>`` ist bei jeder Aenderung eine NEUE
    Adresse. Die kennt kein Cache – weder Cloudflare noch der Browser –,
    also kommt zwingend die neue Fassung. Die index.html selbst wird nicht
    zwischengespeichert (``cf-cache-status: DYNAMIC``), deshalb greift das
    ohne Zutun und ohne Cache-Leeren.
    """
    def ersetze(m):
        pfad = m.group(1)
        # Aus dem URL-Pfad den Asset-Schluessel machen – dieselbe Regel wie in
        # _dash_static: /static/app.js -> "app.js", /vendor/x.js -> "vendor/x.js".
        rel = pfad.decode().lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        # Bewusst _read_asset: genau die Bytes, die auch ausgeliefert werden.
        data = _read_asset(rel)
        if data is None:
            return pfad
        return pfad + b"?v=" + _asset_digest(data)[:12].encode()
    return _ASSET_URL_RE.sub(ersetze, html)


def _asset_response(rel: str, fallback_text: str = "",
                    request: Optional[web.Request] = None) -> web.Response:
    """Eine Frontend-Datei ausliefern – mit Revalidierung beim Browser.

    Ohne Cache-Angaben behielt Chrome eine alte ``app.js`` und schickte sie
    gegen ein bereits aktualisiertes Backend. Das Ergebnis war eine halb
    aufgebaute Seite: das alte Skript fragte Felder ab, die es nicht mehr
    gibt, und brach mitten im Aufbau ab.

    ``no-cache`` heisst NICHT "nicht zwischenspeichern", sondern "vor jeder
    Benutzung nachfragen". Zusammen mit dem ETag bleibt der Verkehr klein:
    unveraenderte Dateien beantwortet der Server mit 304 statt sie erneut zu
    senden.
    """
    data = _read_asset(rel)
    if data is None:
        if fallback_text:
            return web.Response(text=fallback_text, status=500)
        return web.Response(status=404)
    if rel == "index.html":
        # VOR dem ETag: gestempelt wird genau das, was rausgeht.
        data = _mit_versionsstempel(data)
    etag = f'"{_asset_digest(data)[:32]}"'
    kopf = {"Cache-Control": "no-cache, must-revalidate", "ETag": etag}
    # Kennt der Browser die Fassung schon, reicht ein 304 ohne Inhalt.
    if request is not None and request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers=kopf)
    ctype, _enc = mimetypes.guess_type(rel)
    ctype = ctype or "application/octet-stream"
    textish = ctype.startswith("text/") or ctype in (
        "application/javascript", "application/json")
    return web.Response(body=data, content_type=ctype,
                        charset="utf-8" if textish else None, headers=kopf)


_ASSET_MANIFEST = ".assets.json"


def _asset_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_FRONTEND_VERSION: Optional[str] = None


def _frontend_version() -> str:
    """Kurzkennung der ausgelieferten Frontend-Fassung.

    Die ersten zwoelf Zeichen der Pruefsumme von ``app.js`` – dieselbe Zahl,
    die beim Einbetten in ``_ASSET_KNOWN_HASHES`` landet. Damit laesst sich
    ohne Raten feststellen, welche Fassung ein laufender Bot wirklich
    bedient, statt "es hat sich nichts geaendert" auslegen zu muessen.
    """
    global _FRONTEND_VERSION  # noqa: PLW0603
    if _FRONTEND_VERSION is None:
        data = _asset_from_memory("app.js")
        _FRONTEND_VERSION = _asset_digest(data)[:12] if data else "unbekannt"
    return _FRONTEND_VERSION


def _read_asset_manifest() -> Dict[str, str]:
    """Prüfsummen der Dateien, die wir beim letzten Start geschrieben haben."""
    try:
        with open(os.path.join(_DASH_DIR, _ASSET_MANIFEST), "r", encoding="utf-8") as f:
            data = json.load(f)
        files = data.get("files") if isinstance(data, dict) else None
        return files if isinstance(files, dict) else {}
    except Exception:
        return {}


def _write_asset_manifest(files: Dict[str, str]) -> None:
    try:
        with open(os.path.join(_DASH_DIR, _ASSET_MANIFEST), "w", encoding="utf-8") as f:
            json.dump({"_hinweis": "Von bot.py erzeugt. Merkt sich, welche Fassung der "
                                   "Frontend-Dateien ausgeliefert wurde, damit Updates "
                                   "ankommen, eigene Änderungen aber erhalten bleiben.",
                       "files": files}, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _extract_assets() -> None:
    """Eingebettete Dateien nach dashboard_web/ schreiben.

    Regel: **eigene Änderungen gehen nie verloren.** Eine vorhandene Datei wird
    nur dann ersetzt, wenn sie beweisbar noch genau die ist, die eine frühere
    Version von ``bot.py`` dort abgelegt hat – erkennbar an der Prüfsumme aus
    ``dashboard_web/.assets.json`` bzw. an den in ``_ASSET_KNOWN_HASHES``
    hinterlegten Prüfsummen früherer Auslieferungen. Weicht die Datei davon ab,
    hat der Nutzer sie angepasst und sie bleibt unangetastet (mit Hinweis im
    Log). Andernfalls kämen ausgelieferte Fehlerbehebungen am Frontend nie an.

    Schlägt das Schreiben fehl (z. B. schreibgeschütztes Verzeichnis), wird das
    nur geloggt – die Assets liefert dann _asset_response aus dem Speicher.
    """
    written = updated = kept = failed = 0
    for sub in ("", "vendor", "locations", "maps"):
        try:
            os.makedirs(os.path.join(_DASH_STATIC, sub), exist_ok=True)
        except OSError as e:
            failed += 1
            dash_log.warning(f"[DASHBOARD] Ordner '{sub or 'static'}' nicht anlegbar ({e}).")

    manifest = _read_asset_manifest()
    fresh: Dict[str, str] = {}
    for rel in _EMBEDDED_ASSETS:
        path = _asset_path(rel)
        data = _asset_from_memory(rel)
        if not path or data is None:
            continue
        digest = _asset_digest(data)
        is_update = False
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    current = _asset_digest(f.read())
            except OSError:
                current = None
            if current == digest:
                fresh[rel] = digest          # schon aktuell
                continue
            untouched = current is not None and (
                current == manifest.get(rel)
                or current in _ASSET_KNOWN_HASHES.get(rel, ()))
            if not untouched:
                kept += 1
                # Bewusst KEIN Eintrag ins Manifest: dort stehen nur Prüfsummen
                # von Dateien, die wir selbst geschrieben haben. Würden wir hier
                # die Prüfsumme der angepassten Datei vermerken, sähe sie beim
                # nächsten Start wie unsere eigene Auslieferung aus und würde
                # überschrieben – die Anpassung wäre beim zweiten Start weg.
                dash_log.info(f"[DASHBOARD] {rel}: eigene Änderung erkannt – Datei bleibt "
                              f"unverändert. Für die mitgelieferte Fassung die Datei "
                              f"löschen und neu starten.")
                continue
            is_update = True
        try:
            with open(path, "wb") as f:
                f.write(data)
            fresh[rel] = digest
            if is_update:
                updated += 1
            else:
                written += 1
        except OSError as e:
            failed += 1
            dash_log.warning(f"[DASHBOARD] {rel} nicht schreibbar ({e}) – "
                             f"wird aus dem Speicher ausgeliefert.")
    _write_asset_manifest(fresh)

    if written:
        dash_log.info(f"[DASHBOARD] {written} Frontend-Datei(en) unter {_DASH_DIR} angelegt.")
    if updated:
        dash_log.info(f"[DASHBOARD] {updated} Frontend-Datei(en) auf die neue Fassung "
                      f"aktualisiert.")
    if kept:
        dash_log.info(f"[DASHBOARD] {kept} Datei(en) mit eigenen Änderungen beibehalten.")
    if failed:
        dash_log.info("[DASHBOARD] Einige Assets werden direkt aus bot.py bedient.")


# ──────────────────────────────────────────────────────────────────────────
#  Ringpuffer der zuletzt passierten Server-Events für Karte & Event-Liste.
#
#  Der Bot verwirft geparste Events bisher nach dem Posten in die Discord-Feeds.
#  Für die Dashboard-Karte ("was ist zuletzt passiert") halten wir sie hier in
#  einem gedeckelten :class:`collections.deque` vor und reichern – wo möglich –
#  Koordinaten aus den zuletzt bekannten Spielerpositionen an.
#
#  Reines Standard-Bibliotheks-Modul: keine Bot-Imports, keine Drittpakete. Wird
#  von ``bot.py`` (Recorder, in ``DayZBot._dispatch``) und von den API-Handlern
#  (Leser) gemeinsam genutzt – beide sehen dieselbe deque, da das Modul nur einmal
#  in ``sys.modules`` existiert.
# ──────────────────────────────────────────────────────────────────────────
EV_MAX_EVENTS = 1000
EV_PERSIST_FILE = "events_recent.json"
_EV_SAVE_EVERY = 25          # nach so vielen neuen Events wird persistiert
_EV_SAVE_MIN_INTERVAL = 15   # aber höchstens alle N Sekunden

# ── Metadaten je Event-Typ: Label, Emoji, Farbe, Filter-Standard ──────
#   Diese Liste speist auch das linke Filter-Panel der Karte.
EVENT_META: Dict[str, Dict[str, Any]] = {
    "connect":      {"label": "Verbindet",        "emoji": "🟢", "color": "#2ecc71", "default": True},
    "disconnect":   {"label": "Trennt",           "emoji": "🔴", "color": "#e74c3c", "default": True},
    "connecting":   {"label": "Verbindungsversuch","emoji": "🔌", "color": "#95a5a6", "default": False},
    "kill_pvp":     {"label": "PvP-Kill",          "emoji": "☠️", "color": "#c0392b", "default": True},
    "suicide":      {"label": "Selbstmord",        "emoji": "💀", "color": "#8e44ad", "default": True},
    "kill_env":     {"label": "Umwelt-Tod",        "emoji": "🧟", "color": "#7f8c8d", "default": True},
    "damage":       {"label": "Treffer / Hit",     "emoji": "🩸", "color": "#e67e22", "default": True},
    "basebuild":    {"label": "Bau-Event",         "emoji": "🏗️", "color": "#f1c40f", "default": True},
    "vehicle":      {"label": "Fahrzeug/Crash",    "emoji": "🚗", "color": "#16a085", "default": True},
    "heli_crash":   {"label": "Helikopter-Absturz","emoji": "🚁", "color": "#2980b9", "default": True},
    "train_crash":  {"label": "Zug-Unfall",        "emoji": "🚆", "color": "#34495e", "default": True},
    "chat":         {"label": "Chat",              "emoji": "💬", "color": "#3498db", "default": False},
    "admin_action": {"label": "Admin-Aktion",      "emoji": "🛡️", "color": "#9b59b6", "default": False},
    "loot":         {"label": "Loot",              "emoji": "🎒", "color": "#27ae60", "default": False},
}

# Welcher Spielername in einem Event trägt die (Karten-)Position?
_PRIMARY_PLAYER_KEYS = {
    "kill_pvp":     "victim",
    "damage":       "victim",
    "suicide":      "player",
    "kill_env":     "player",
    "connect":      "player",
    "disconnect":   "player",
    "connecting":   "player",
    "chat":         "player",
    "admin_action": "admin",
    "basebuild":    "player",
}

_EV_LOCK = threading.Lock()
_EV_BUF: Deque[Dict[str, Any]] = deque(maxlen=EV_MAX_EVENTS)
_ev_next_id = 1
_ev_since_save = 0
_ev_last_save = 0.0

_EV_POS_RE = re.compile(r"(-?\d+(?:\.\d+)?)")


def _ev_parse_pos(pos_str: Optional[str]):
    """Erste zwei Zahlen aus 'X, Y, Z' → (x=Ost, z=Nord) oder None."""
    if not pos_str:
        return None
    nums = _EV_POS_RE.findall(str(pos_str))
    if len(nums) >= 2:
        try:
            return round(float(nums[0]), 1), round(float(nums[1]), 1)
        except ValueError:
            return None
    return None


def _ev_classify(ev: Dict[str, Any]) -> str:
    """Feinere Klassifizierung – trennt Heli/Zug aus dem generischen vehicle-Event."""
    t = ev.get("type", "")
    if t == "vehicle":
        raw = str(ev.get("raw", "")).lower()
        if "heli" in raw or "helicopter" in raw or "uh1" in raw or "mi8" in raw:
            return "heli_crash"
        if "train" in raw or "zug" in raw or "wagon" in raw or "locomotive" in raw:
            return "train_crash"
    return t


def _ev_summary(ev: Dict[str, Any], etype: str) -> str:
    """Kurzer, menschenlesbarer Text für die Event-Liste."""
    g = ev.get
    if etype == "kill_pvp":
        w = f" mit {g('weapon')}" if g("weapon") else ""
        d = f" ({g('distance')} m)" if g("distance") else ""
        return f"{g('killer','?')} → {g('victim','?')}{w}{d}"
    if etype == "damage":
        w = f" ({g('weapon')})" if g("weapon") else ""
        return f"{g('attacker','?')} trifft {g('victim','?')}{w}"
    if etype in ("suicide",):
        return f"{g('player','?')} hat sich selbst getötet"
    if etype == "kill_env":
        return f"{g('player','?')} gestorben ({g('cause','Umwelt')})"
    if etype in ("connect", "disconnect", "connecting"):
        verb = {"connect": "verbindet", "disconnect": "trennt", "connecting": "verbindet sich"}[etype]
        return f"{g('player','?')} {verb}"
    if etype == "chat":
        return f"{g('player','?')}: {g('message','')}"[:180]
    if etype == "admin_action":
        return f"{g('admin','?')}: {g('command','')}"[:180]
    if etype == "basebuild":
        return f"{g('player','?')} · {g('item','Bau')}"
    if etype in ("vehicle", "heli_crash", "train_crash"):
        return str(g("raw", "Fahrzeug-Ereignis"))[:180]
    if etype == "loot":
        return str(g("raw", "Loot"))[:180]
    return str(g("raw", etype))[:180]


def _ev_record(ev: Dict[str, Any], player_positions: Optional[Dict[str, Any]] = None,
               service_id: Optional[str] = None) -> None:
    """Ein geparstes Event in den Ringpuffer aufnehmen (aus ``_dispatch``).

    ``service_id`` haelt fest, von welchem Nitrado-Server das Ereignis stammt –
    ohne diese Angabe saehe jeder Kunde die Kills aller anderen.
    """
    global _ev_next_id, _ev_since_save, _ev_last_save
    try:
        etype = _ev_classify(ev)
        # Position bestimmen: bevorzugt aus dem Event, sonst letzte bekannte Spielerposition
        xz = _ev_parse_pos(ev.get("pos") or ev.get("position"))
        if xz is None and player_positions:
            pkey = _PRIMARY_PLAYER_KEYS.get(ev.get("type", ""))
            pname = ev.get(pkey) if pkey else None
            if pname:
                entry = player_positions.get(pname) or player_positions.get(str(pname))
                if isinstance(entry, dict):
                    xz = _ev_parse_pos(entry.get("position"))
        rec = {
            "type": etype,
            "log_type": ev.get("type"),
            "time": ev.get("timestamp"),
            "summary": _ev_summary(ev, etype),
            "player": ev.get("victim") or ev.get("player") or ev.get("killer") or ev.get("admin"),
            "raw": str(ev.get("raw", ""))[:400],
            "service_id": str(service_id or ""),
        }
        if xz:
            rec["x"], rec["z"] = xz[0], xz[1]
        with _EV_LOCK:
            rec["id"] = _ev_next_id
            rec["ts"] = time.time()
            _ev_next_id += 1
            _EV_BUF.append(rec)
            _ev_since_save += 1
            due = _ev_since_save >= _EV_SAVE_EVERY and (time.time() - _ev_last_save) >= _EV_SAVE_MIN_INTERVAL
        if due:
            _ev_persist()
    except Exception:
        # Ein Fehler hier darf niemals den Log-Dispatch des Bots stören.
        pass


def _ev_snapshot(since_id: int = 0, types: Optional[List[str]] = None,
             limit: int = 500, service_id: Optional[str] = None) -> Dict[str, Any]:
    """Aktuelle Events (optional nur neuere / bestimmte Typen) für die API.

    ``service_id`` grenzt auf einen Server ein. Altbestand ohne Server-Angabe
    zaehlt zum Hauptserver, damit vorhandene Ereignisse nicht verschwinden.
    """
    tset = set(types) if types else None
    primary = connections.primary()
    ist_haupt = bool(primary is not None and service_id == primary.service_id)

    def passt(e):
        if service_id is None:
            return True
        sid = str(e.get("service_id") or "")
        return sid == service_id or (not sid and ist_haupt)

    with _EV_LOCK:
        items = [e for e in _EV_BUF
                 if e["id"] > since_id and (tset is None or e["type"] in tset)
                 and passt(e)]
        last = _ev_next_id - 1
    if len(items) > limit:
        items = items[-limit:]
    return {"events": items, "last_id": last}


def _ev_types_meta() -> List[Dict[str, Any]]:
    """Filter-Metadaten für das linke Panel."""
    return [{"type": k, **v} for k, v in EVENT_META.items()]


# ── Persistenz (überlebt einen Bot-Neustart mit etwas History) ────────
def _ev_persist() -> None:
    global _ev_since_save, _ev_last_save
    try:
        with _EV_LOCK:
            data = {"next_id": _ev_next_id, "events": list(_EV_BUF)}
        tmp = EV_PERSIST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, EV_PERSIST_FILE)
        _ev_since_save = 0
        _ev_last_save = time.time()
    except Exception:
        pass


def _ev_load() -> None:
    """Beim Start vorhandene History laden (best effort)."""
    global _ev_next_id
    try:
        if not os.path.exists(EV_PERSIST_FILE):
            return
        with open(EV_PERSIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        with _EV_LOCK:
            for e in data.get("events", [])[-EV_MAX_EVENTS:]:
                _EV_BUF.append(e)
            _ev_next_id = max(int(data.get("next_id", 1)),
                           (_EV_BUF[-1]["id"] + 1) if _EV_BUF else 1)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────
#  Session-/Zugangsschutz für das Dashboard.
#
#  Gemäß Nutzerwahl gibt es KEIN separates Passwort – das Gate ist ein gültiger
#  Nitrado-Token. Nach erfolgreicher Token-Prüfung (Nitrado liefert Gameserver)
#  wird eine Server-seitige Session angelegt; der Browser bekommt nur eine
#  zufällige Session-ID als Cookie, der Token selbst verlässt den Server nie.
#
#  Ein optionaler Passwort-Hook ist vorbereitet (``DASHBOARD_PASSWORD`` in der
#  config), bleibt hier aber standardmäßig deaktiviert.
# ──────────────────────────────────────────────────────────────────────────
_SESS_COOKIE = "dz_sess"
_SESS_TTL = 60 * 60 * 12  # 12 Stunden

# session_id → {token, service_id, gameservers, map_name, created, seen}
_SESS_STORE: Dict[str, Dict[str, Any]] = {}

# Pfade, die ohne Session erreichbar sind
_SESS_PUBLIC_PREFIXES = ("/api/auth/", "/static/", "/vendor/", "/maps/")
_SESS_PUBLIC_EXACT = ("/", "/index.html", "/api/session", "/favicon.ico", "/api/health")
# Erreichbar mit Discord-Anmeldung, aber noch OHNE Nitrado-Token: die Auswahl
# des eigenen Discord-Servers und die Optionen-Seite, auf der der Token
# nachgetragen wird. Bewusst eine kurze, feste Liste – alles andere bleibt
# hinter dem Token. Die Mandantentrennung haengt davon nicht ab, die macht
# _session_conn.
_SESS_OHNE_TOKEN = ("/api/my-guilds", "/api/my-guilds/select", "/api/options")


def _sess_prune() -> None:
    now = time.time()
    for sid in [s for s, v in _SESS_STORE.items() if now - v.get("seen", 0) > _SESS_TTL]:
        _SESS_STORE.pop(sid, None)


def _sess_create(token: str, gameservers: list,
                 discord_user: Optional[Dict[str, Any]] = None,
                 is_admin: bool = False,
                 service_id: Optional[str] = None,
                 token_invalid: bool = False,
                 owned_guilds: Optional[list] = None,
                 guild_id: Optional[str] = None) -> str:
    """Neue Session anlegen.

    Sie entsteht jetzt schon beim Discord-Login – also bevor ein Nitrado-Token
    vorliegt. ``token`` ist dann leer und wird von post_token nachgetragen.

    Seit der Guild-Anmeldung gibt es ZWEI Anker: ``guild_id`` (die gewaehlte
    Discord-Guild) und ``service_id`` (der Nitrado-Server darin). Die Guild kann
    schon feststehen, waehrend es noch gar keinen Nitrado-Server gibt – deshalb
    sind beide unabhaengig voneinander.
    """
    _sess_prune()
    sid = secrets.token_urlsafe(32)
    now = time.time()
    _SESS_STORE[sid] = {
        "token": token,
        "gameservers": gameservers,
        "service_id": service_id,
        "map_name": None,
        "discord": discord_user,
        "is_admin": bool(is_admin),
        # Die beim Login von Discord geholten EIGENEN Guilds. Einzige Autoritaet
        # dafuer, welche Guild diese Anmeldung waehlen darf – niemals die
        # guild_id aus dem Anfrage-Body.
        "owned_guilds": list(owned_guilds or []),
        "guild_id": guild_id,
        # Gesetzt, wenn ein gespeicherter Token von Nitrado abgelehnt wurde –
        # das Frontend erklaert dann, warum die Eingabe wieder erscheint.
        "token_invalid": bool(token_invalid),
        "created": now,
        "seen": now,
    }
    return sid


def _sess_get(request: web.Request) -> Optional[Dict[str, Any]]:
    sid = request.cookies.get(_SESS_COOKIE)
    if not sid:
        return None
    sess = _SESS_STORE.get(sid)
    if not sess:
        return None
    if time.time() - sess.get("seen", 0) > _SESS_TTL:
        _SESS_STORE.pop(sid, None)
        return None
    sess["seen"] = time.time()
    return sess


def _sess_destroy(request: web.Request) -> None:
    sid = request.cookies.get(_SESS_COOKIE)
    if sid:
        _SESS_STORE.pop(sid, None)


def _ist_https(request: Optional[web.Request]) -> bool:
    """Kam die Anfrage verschluesselt an? Beruecksichtigt den Tunnel/Proxy.

    Hinter einem Cloudflare-Tunnel spricht der Bot selbst nur HTTP; ob der
    Nutzer HTTPS benutzt hat, steht dann in ``X-Forwarded-Proto``.
    """
    if request is None:
        return False
    proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.scheme == "https"


def _sess_cookie(response: web.Response, sid: str,
                 request: Optional[web.Request] = None) -> None:
    # ``Secure`` nur bei einer HTTPS-Anfrage: derselbe Port bedient bewusst
    # auch HTTP (siehe _DualProtocolSite). Wuerde das Flag immer gesetzt,
    # schickt der Browser das Cookie ueber HTTP nie mit und die Anmeldung
    # waere dort tot. Kommt der Nutzer ueber HTTPS – der Normalfall hinter dem
    # Tunnel –, verlaesst das Cookie den Browser ab jetzt nur noch verschluesselt.
    response.set_cookie(_SESS_COOKIE, sid, httponly=True, samesite="Lax",
                        max_age=_SESS_TTL, path="/", secure=_ist_https(request))


def _sess_is_public(path: str) -> bool:
    if path in _SESS_PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in _SESS_PUBLIC_PREFIXES)


@web.middleware
async def _dash_auth_middleware(request: web.Request, handler):
    """Blockt geschützte /api/-Pfade ohne gültige Session mit 401."""
    path = request.path
    if path.startswith("/api/") and not _sess_is_public(path):
        sess = _sess_get(request)
        if not sess:
            # ``session_weg`` ist das einzige Kennzeichen, an dem das Dashboard
            # "zurueck auf den Anmeldebildschirm" festmacht. Ein 401 aus einem
            # Handler (z. B. ein abgelehnter Nitrado-Token) hat es NICHT und
            # bleibt deshalb eine Meldung an Ort und Stelle.
            return web.json_response(
                {"error": "unauthorized", "session_weg": True,
                 "message": "Bitte zuerst den Nitrado-Token eingeben."},
                status=401)
        # Der Nitrado-Token bleibt das Tor zum Dashboard. Seit der Anmeldung
        # ueber den Discord-Server wird er aber erst DANACH eingetragen –
        # deshalb sind genau die Wege dorthin auch ohne ihn erreichbar,
        # und das nur fuer eine Sitzung mit Discord-Anmeldung.
        if not sess.get("token") and not (
                path in _SESS_OHNE_TOKEN and sess.get("discord")):
            # Ohne ``session_weg``: die Anmeldung ist in Ordnung, es fehlt nur
            # der Nitrado-Server. Das Dashboard bleibt deshalb stehen.
            return web.json_response(
                {"ok": False, "kein_token": True,
                 "error": "Noch kein Nitrado-Server verbunden – trage den "
                          "Token unter „Optionen“ ein."},
                status=401)
        request["session"] = sess
    # Vor dem Handler merken: beim Abmelden ist die Session danach weg.
    actor = _audit_actor(request.get("session") or _sess_get(request))
    try:
        response = await handler(request)
    except Exception:
        # Auch gescheiterte Zugriffe gehören ins Protokoll – gerade die.
        if request.method in ("POST", "PUT", "DELETE") and path.startswith("/api/"):
            try:
                _audit_add("dashboard", actor, _audit_label(request.method, path),
                           success=False)
            except Exception:  # noqa: BLE001
                pass
        raise
    # Ändernde Zugriffe ins Protokoll – an genau einer Stelle statt in 40
    # Handlern. Aufgezeichnet werden Methode, Pfad und Ergebnis, nie der
    # Inhalt der Anfrage: dort steckt sonst der Nitrado-Token.
    if (request.method in ("POST", "PUT", "DELETE")
            and path.startswith("/api/")
            and not any(path.startswith(p) for p in _AUDIT_SKIP_PREFIXES)):
        try:
            _audit_add("dashboard", actor,
                       _audit_label(request.method, path),
                       success=getattr(response, "status", 500) < 400)
        except Exception:  # noqa: BLE001 – Protokoll darf nie die Antwort kippen
            pass
    return response


# ──────────────────────────────────────────────────────────────────────────
#  Gemeinsame Helfer für die API-Handler.
# ──────────────────────────────────────────────────────────────────────────
def ok(data: Any = None, **extra) -> web.Response:
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return web.json_response(payload)


def err(message: str, status: int = 400, **extra) -> web.Response:
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return web.json_response(payload, status=status)


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def require_nitrado(request: Optional[web.Request] = None):
    """Gibt (nitrado, None) zurück oder (None, Fehlerantwort), wenn nicht eingerichtet.

    Mit Request wird der Server dieser Anmeldung genommen – sonst wuerde das
    Dashboard eines Kunden den Hauptserver steuern.
    """
    if request is not None:
        # Aus dem Dashboard gilt AUSSCHLIESSLICH der Server der Anmeldung.
        # Ein Rueckfall auf bot.nitrado (= Hauptserver des Betreibers) haette
        # jedem Angemeldeten ohne eigenen Server dessen Steuerung gegeben.
        conn, denied = _session_conn(request)
        if denied is not None:
            return None, denied
        nit = conn.api
    else:
        nit = getattr(bot, "nitrado", None) if bot else None
    if not nit or not str(getattr(nit, "service_id", "") or "").strip():
        return None, err("Nitrado-Server ist noch nicht eingerichtet.", 409)
    return nit, None


# ──────────────────────────────────────────────────────────────────────────
#  Onboarding: Nitrado-Token prüfen → Server wählen → Karte erkennen.
#  Das ist inzwischen der einzige Weg, den Token einzutragen (Optionen im
#  Dashboard) – der frühere Discord-Befehl ``/setup token`` wurde entfernt.
# ──────────────────────────────────────────────────────────────────────────
def _server_view(svc: dict) -> dict:
    d = svc.get("details") or {}
    return {
        "id": str(svc.get("id")),
        "name": d.get("name") or d.get("game") or f"Service {svc.get('id')}",
        "game": d.get("game") or "",
        "address": d.get("address") or "",
        "status": str(svc.get("status", "")),
    }


# ══════════════════════════════════════════════════════════════
#  Discord-Login fürs Dashboard (OAuth2) + Aktions-Protokoll
# ══════════════════════════════════════════════════════════════
#  Vor dem Nitrado-Token steht eine Anmeldung mit Discord. Sie sagt nur, WER
#  da ist (scope=identify) – welche Rollen die Person hat, fragt nicht Discord
#  im Namen des Nutzers ab, sondern der Bot selbst über die verbundenen
#  Guilds. Damit bleibt der OAuth-Scope so schmal wie möglich.
#
#  Ohne hinterlegtes Client-Secret ist der Login AUS und das Dashboard
#  verhält sich exakt wie vorher. Das ist Absicht: ein Update darf ein
#  laufendes Dashboard nicht aussperren.

_DISCORD_API = "https://discord.com/api/v10"
_OAUTH_STATES: Dict[str, float] = {}        # state → Ablaufzeitpunkt (CSRF-Schutz)
_OAUTH_STATE_TTL = 600


def _discord_app_id() -> Optional[int]:
    try:
        app_id = getattr(bot, "application_id", None)
        if not app_id and getattr(bot, "user", None) is not None:
            app_id = bot.user.id
        return int(app_id) if app_id else None
    except Exception:  # noqa: BLE001
        return None


def _bot_identity() -> Optional[Dict[str, Optional[str]]]:
    """Name und Profilbild des Bots – so, wie Discord sie kennt.

    Bewusst live aus ``bot.user`` statt fest im Dashboard eingetragen: benennt
    der Betreiber den Bot in Discord um oder tauscht das Bild, zieht die
    Kopfzeile beim naechsten Laden von selbst nach.

    Gibt ``None`` zurueck, solange der Bot nicht bei Discord angemeldet ist –
    dann bleibt in der Kopfzeile die bisherige Beschriftung stehen.
    """
    try:
        user = getattr(bot, "user", None) if bot is not None else None
        if user is None:
            return None
        avatar = getattr(user, "display_avatar", None)
        name = getattr(user, "display_name", None) or getattr(user, "name", None)
        return {"name": str(name) if name else None,
                "avatar_url": str(avatar.url) if avatar is not None else None}
    except Exception:  # noqa: BLE001 – die Kopfzeile darf nichts zum Absturz bringen
        return None


def _discord_login_enabled() -> bool:
    """Der Login ist genau dann aktiv, wenn ein Client-Secret hinterlegt ist."""
    return bool(str(cfg.config.get("discord_client_secret") or "").strip())


def _discord_client_id() -> str:
    cid = str(cfg.config.get("discord_client_id") or "").strip()
    return cid or str(_discord_app_id() or _DISCORD_INVITE_FALLBACK_CLIENT_ID)


def _oauth_redirect_uri(request: web.Request) -> str:
    """Rücksprungadresse – muss im Developer Portal exakt so eingetragen sein.

    Ohne feste Angabe aus dem Aufruf gebildet: wer das Dashboard über https://
    öffnet, bekommt die https-Variante. Deshalb gehören beide ins Portal.
    """
    fixed = str(cfg.config.get("discord_redirect_uri") or "").strip()
    if fixed:
        return fixed.rstrip("/")
    return f"{request.scheme}://{request.host}/api/auth/discord/callback"


def _eigene_guilds(roh: Any) -> List[Dict[str, Optional[str]]]:
    """Aus Discords Guild-Liste nur die, deren **Eigentuemer** man ist.

    Discord markiert das je Eintrag mit ``owner``. Bewusst NICHT auch
    "Server verwalten": gemeint sind die eigenen Server, nicht mitverwaltete.

    Diese Liste ist die einzige Autoritaet dafuer, welche Guild eine Anmeldung
    waehlen darf – sie stammt aus dem Nutzer-Token, nicht aus dem Browser.
    """
    if not isinstance(roh, list):
        return []
    out: List[Dict[str, Optional[str]]] = []
    for g in roh:
        if not isinstance(g, dict) or g.get("owner") is not True:
            continue
        gid = str(g.get("id") or "").strip()
        if not gid.isdigit():
            continue
        icon = g.get("icon")
        out.append({
            "id": gid,
            "name": str(g.get("name") or f"Discord {gid}"),
            "icon_url": (f"https://cdn.discordapp.com/icons/{gid}/{icon}.png?size=128"
                         if icon else None),
        })
    return out


async def _discord_fetch_user(code: str,
                              redirect_uri: str) -> Tuple[Optional[dict], list]:
    """Code gegen Zugriffstoken tauschen, Profil UND eigene Guilds holen.

    Der Zugriffstoken bleibt hier im Prozess – er wird nicht gespeichert und
    erreicht den Browser nie. Die Guild-Liste wird im selben Zug geholt, damit
    der Token danach nicht mehr gebraucht wird.

    Rueckgabe: ``(profil, guild_rohliste)``. Scheitert nur die Guild-Abfrage,
    kommt das Profil trotzdem zurueck – die Anmeldung soll daran nicht haengen.
    """
    payload = {
        "client_id": _discord_client_id(),
        "client_secret": str(cfg.config.get("discord_client_secret") or "").strip(),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{_DISCORD_API}/oauth2/token", data=payload) as r:
                if r.status != 200:
                    dash_log.warning(f"[LOGIN] Token-Tausch fehlgeschlagen "
                                     f"({r.status}): {(await r.text())[:200]}")
                    return None, []
                tok = await r.json()
            access = tok.get("access_token")
            if not access:
                return None, []
            kopf = {"Authorization": f"Bearer {access}"}
            async with s.get(f"{_DISCORD_API}/users/@me", headers=kopf) as r:
                if r.status != 200:
                    dash_log.warning(f"[LOGIN] Profil nicht abrufbar ({r.status}).")
                    return None, []
                profil = await r.json()
            # Die Guild-Liste ist Beiwerk: klemmt sie (fehlender Scope bei einer
            # alten Zustimmung, Rate-Limit), soll die Anmeldung trotzdem gelingen.
            guilds: list = []
            try:
                async with s.get(f"{_DISCORD_API}/users/@me/guilds", headers=kopf) as r:
                    if r.status == 200:
                        guilds = await r.json()
                    else:
                        dash_log.warning(f"[LOGIN] Guild-Liste nicht abrufbar "
                                         f"({r.status}) – Auswahl bleibt leer.")
            except Exception as e:  # noqa: BLE001
                dash_log.warning(f"[LOGIN] Guild-Liste fehlgeschlagen: {e}")
            return profil, guilds
    except Exception as e:  # noqa: BLE001
        dash_log.warning(f"[LOGIN] Discord nicht erreichbar: {e}")
        return None, []


async def _discord_user_is_admin(user_id: int) -> bool:
    """Hat die Person die Dashboard-Admin-Rolle in einem verbundenen Server?

    Geprüft wird über den Bot, nicht über den Nutzer-Token. ``fetch_member``
    ist ein REST-Aufruf und braucht deshalb nicht die privilegierte
    Members-Intent, die dieser Bot nicht anfordert.
    """
    try:
        role_id = int(str(cfg.config.get("dashboard_admin_role_id") or "0") or 0)
    except (TypeError, ValueError):
        return False
    if not role_id or bot is None or getattr(bot, "user", None) is None:
        return False
    for gid in _configured_guild_ids():
        guild = bot.get_guild(gid)
        if guild is None:
            continue
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:  # noqa: BLE001 – nicht Mitglied / nicht abrufbar
                continue
        if any(int(r.id) == role_id for r in getattr(member, "roles", [])):
            return True
    return False


def _discord_user_view(user: dict) -> dict:
    """Nur das, was das Dashboard anzeigt – kein Token, keine E-Mail."""
    uid = str(user.get("id") or "")
    name = user.get("global_name") or user.get("username") or f"Discord-Nutzer {uid}"
    avatar = user.get("avatar")
    if avatar and uid:
        avatar_url = f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png?size=64"
    else:
        # Kein eigenes Bild gesetzt: Discords Standard-Avatar. Der Index ergibt
        # sich bei den neuen Nutzernamen aus der ID.
        try:
            idx = (int(uid) >> 22) % 6 if uid else 0
        except (TypeError, ValueError):
            idx = 0
        avatar_url = f"https://cdn.discordapp.com/embed/avatars/{idx}.png"
    return {"id": uid, "name": name, "avatar_url": avatar_url}


async def _discord_konto_kurz(uid: Any) -> Optional[Dict[str, Optional[str]]]:
    """Name und Bild eines Discord-Kontos anhand seiner ID – fuer die Serverliste.

    Bewusst live nachgeschlagen statt beim Verbinden dauerhaft gespeichert: ein
    Nutzername kann sich aendern, und Bestandsverbindungen kennen ohnehin nur
    die ID. Schlaegt der Abruf fehl (Bot offline, Konto geloescht, kein Netz),
    bleibt nur die ID uebrig – besser als eine leere Zelle.
    """
    text = str(uid or "").strip()
    if not text:
        return None
    try:
        rid = int(text)
    except (TypeError, ValueError):
        return None
    if bot is None:
        return {"id": text, "name": None, "avatar_url": None}
    user = bot.get_user(rid)
    if user is None:
        try:
            user = await bot.fetch_user(rid)
        except Exception:  # noqa: BLE001 – Konto geloescht, Rate-Limit, kein Netz
            return {"id": text, "name": None, "avatar_url": None}
    name = getattr(user, "display_name", None) or getattr(user, "name", None)
    avatar = getattr(user, "display_avatar", None)
    return {"id": text, "name": str(name) if name else None,
            "avatar_url": str(avatar.url) if avatar is not None else None}


async def api_discord_start(request: web.Request) -> web.Response:
    """Liefert die Discord-Anmeldeadresse (das Frontend leitet dorthin weiter)."""
    if not _discord_login_enabled():
        return err("Der Discord-Login ist nicht eingerichtet.", 400)
    now = time.time()
    for st in [s for s, exp in _OAUTH_STATES.items() if exp < now]:
        _OAUTH_STATES.pop(st, None)
    state = secrets.token_urlsafe(24)
    _OAUTH_STATES[state] = now + _OAUTH_STATE_TTL
    redirect = _oauth_redirect_uri(request)
    url = (f"{_DISCORD_API.replace('/api/v10', '')}/oauth2/authorize"
           f"?client_id={_discord_client_id()}"
           f"&redirect_uri={urllib.parse.quote(redirect, safe='')}"
           # guilds kommt zu identify dazu: nur damit laesst sich auflisten,
           # welche Discord-Server der Person gehoeren. Bestehende Anmeldungen
           # sehen deshalb einmalig wieder den Zustimmungsdialog.
           f"&response_type=code&scope=identify%20guilds&state={state}")
    return ok({"url": url})


async def api_discord_callback(request: web.Request) -> web.Response:
    """Rücksprung von Discord: Code einlösen, Session anlegen, ins Dashboard."""
    def _back(reason: str) -> web.Response:
        return web.HTTPFound(f"/?login={reason}")

    if not _discord_login_enabled():
        return _back("disabled")
    if request.query.get("error"):
        return _back("denied")
    code = request.query.get("code") or ""
    state = request.query.get("state") or ""
    expires = _OAUTH_STATES.pop(state, 0)
    if not code or not state or expires < time.time():
        return _back("state")

    user, guild_roh = await _discord_fetch_user(code, _oauth_redirect_uri(request))
    if not user or not user.get("id"):
        return _back("failed")

    view = _discord_user_view(user)
    is_admin = await _discord_user_is_admin(int(user["id"]))
    eigene = _eigene_guilds(guild_roh)

    # Kennt dieses Discord-Konto schon einen Nitrado-Server? Dann nicht erneut
    # nach dem Token fragen – er steht in der Verbindung.
    conn, token_invalid = await _conn_for_login(view["id"], is_admin)

    sid = _sess_create(conn.token if conn else "", [], discord_user=view,
                       is_admin=is_admin,
                       service_id=(conn.service_id if conn else None),
                       token_invalid=token_invalid,
                       owned_guilds=eigene,
                       # Wiedererkannt: die Guild des eigenen Servers ist damit
                       # schon gewaehlt, der Auswahlschritt entfaellt.
                       guild_id=(str(conn.guild_id)
                                 if (conn is not None and conn.guild_id) else None))
    if conn is not None:
        _SESS_STORE[sid]["map_name"] = conn.get("map_name")

    resp = web.HTTPFound("/")
    _sess_cookie(resp, sid, request)
    if conn is not None:
        detail = f"Server „{conn.name}“ ohne erneute Token-Eingabe verbunden"
    elif token_invalid:
        detail = "gespeicherter Token von Nitrado abgelehnt – Eingabe nötig"
    else:
        detail = "mit Admin-Rolle" if is_admin else "ohne Admin-Rolle"
    _audit_add("dashboard", f"{view['name']} ({view['id']})", "Am Dashboard angemeldet",
               detail)
    dash_log.info(f"[LOGIN] {view['name']} ({view['id']}) angemeldet – "
                  f"Admin: {'ja' if is_admin else 'nein'} · {detail}")
    return resp


async def _conn_for_login(discord_id: str,
                          is_admin: bool) -> Tuple[Optional[ServerConnection], bool]:
    """Die Verbindung, mit der dieser Anmelder sofort weiterarbeiten kann.

    Rueckgabe ``(verbindung, token_ungueltig)``. ``(None, False)`` heisst: neues
    Konto, die Token-Eingabe erscheint wie bisher.
    """
    owned = connections.for_owner(discord_id)
    if not owned and is_admin:
        # Server aus der Migration bzw. aus /setup token haben keinen Besitzer.
        # Nur die Admin-Rolle darf sie uebernehmen.
        owned = connections.adopt_ownerless(discord_id)
        if owned:
            names = ", ".join(c.name for c in owned)
            dash_log.info(f"[LOGIN] Bisher besitzerlose Verbindung(en) übernommen: {names}")
            _audit_add("dashboard", f"Discord {discord_id}",
                       "Server-Besitz übernommen", names)
    if not owned:
        return None, False

    # Mehrere Server: der mit zugeordneter Guild ist der aktive.
    conn = next((c for c in owned if c.guild_id), owned[0])
    if not conn.token:
        return None, False

    # Gespeicherten Token einmal gegen Nitrado pruefen – zurueckgezogene Token
    # sollen hier auffallen und nicht erst beim naechsten Server-Befehl.
    api = NitradoAPI(token=conn.token, service_id="",
                     base=conn.get("nitrado_api_base", "https://api.nitrado.net"))
    try:
        services = await api.list_services()
    except Exception as e:  # noqa: BLE001
        # Nitrado gerade nicht erreichbar ist KEIN ungueltiger Token – sonst
        # sperrt eine Stoerung bei Nitrado alle Nutzer aus.
        dash_log.warning(f"[LOGIN] Token-Prüfung nicht möglich ({e}) – lasse durch.")
        return conn, False
    finally:
        await api.close()

    if services is None:
        return conn, False
    if not any(str(s.get("id")) == conn.service_id for s in services):
        dash_log.info(f"[LOGIN] Gespeicherter Token kennt {conn.name} nicht mehr – "
                      f"Token-Eingabe nötig.")
        return None, True
    return conn, False


# ──────────────────────────────────────────────────────────────────────────
#  Aktions-Protokoll: wer hat was mit dem Bot gemacht?
#  Erfasst beide Bedienwege – Slash-Befehle im Discord (on_interaction) und
#  ändernde Zugriffe im Dashboard (Middleware). Lesende Zugriffe nicht, die
#  würden das Protokoll zumüllen.
# ──────────────────────────────────────────────────────────────────────────
_AUDIT_FILE = "bot_audit.json"
_AUDIT_MAX = 500
_audit_log: Deque[Dict[str, Any]] = deque(maxlen=_AUDIT_MAX)


def _audit_add(source: str, actor: str, action: str, detail: str = "",
               success: bool = True) -> None:
    _audit_log.append({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,          # "discord" | "dashboard"
        "actor": actor,
        "action": action,
        "detail": detail[:300],
        "ok": bool(success),
    })
    _audit_persist()


def _audit_persist() -> None:
    try:
        with open(_AUDIT_FILE, "w", encoding="utf-8") as f:
            json.dump(list(_audit_log), f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _audit_load() -> None:
    try:
        with open(_AUDIT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for entry in data[-_AUDIT_MAX:]:
                if isinstance(entry, dict):
                    _audit_log.append(entry)
    except (OSError, ValueError):
        pass


# Sprechende Namen für die häufigsten Dashboard-Aktionen. Was hier fehlt,
# erscheint als "METHODE /pfad" – unschön, aber nie falsch.
_AUDIT_LABELS = {
    ("POST", "/api/server/restart"): "Server neu gestartet",
    ("POST", "/api/server/stop"): "Server gestoppt",
    ("POST", "/api/auth/token"): "Nitrado-Token eingegeben",
    ("POST", "/api/auth/select-server"): "Nitrado-Server ausgewählt",
    ("POST", "/api/auth/guild"): "Discord-Server verbunden",
    ("POST", "/api/auth/logout"): "Abgemeldet",
    ("POST", "/api/zones"): "Zone angelegt",
    ("POST", "/api/shop/items"): "Shop-Item angelegt",
    ("POST", "/api/shop/categories"): "Shop-Kategorie angelegt",
    ("POST", "/api/shop/refresh-types"): "Shop-Katalog aus types.xml erneuert",
    ("POST", "/api/shop/import"): "Shop-Katalog importiert",
    ("POST", "/api/economy/money"): "Guthaben geändert",
    ("POST", "/api/economy/config"): "Economy-Einstellungen geändert",
    ("POST", "/api/bans"): "Spieler gebannt",
    ("POST", "/api/whitelist"): "Whitelist-Eintrag hinzugefügt",
    ("POST", "/api/announcements"): "Ankündigung angelegt",
    ("POST", "/api/auto-restart"): "Auto-Neustart geändert",
}
_AUDIT_SKIP_PREFIXES = ("/api/auth/discord/",)


def _audit_label(method: str, path: str) -> str:
    exact = _AUDIT_LABELS.get((method, path))
    if exact:
        return exact
    if path.startswith("/api/shop/items/"):
        return "Shop-Item gelöscht" if method == "DELETE" else "Shop-Item geändert"
    if path.startswith("/api/zones/"):
        if "/allowlist" in path:
            return "Zonen-Allowlist geändert"
        return "Zone gelöscht" if method == "DELETE" else "Zone geändert"
    if path.startswith("/api/feeds/"):
        return "Feed-Channel gesetzt"
    if path.startswith("/api/bans/"):
        return "Bann aufgehoben"
    if path.startswith("/api/whitelist/"):
        return "Whitelist-Eintrag entfernt"
    if path.startswith("/api/announcements/"):
        return "Ankündigung gelöscht"
    if path.startswith("/api/admin/servers/"):
        if method == "DELETE":
            return "Server entfernt"
        return "Guild zugeordnet"
    return f"{method} {path}"


def _audit_actor(sess: Optional[Dict[str, Any]]) -> str:
    user = (sess or {}).get("discord") or {}
    if user.get("id"):
        return f"{user.get('name')} ({user.get('id')})"
    return "Dashboard (ohne Discord-Anmeldung)"


# Wie lange eine einmal geprueft Adminrolle als bestaetigt gilt, bevor sie
# erneut bei Discord nachgeschlagen wird. Kurz genug, dass ein Rollenentzug
# schnell greift, lang genug, dass nicht jeder Klick einen REST-Aufruf ausloest.
_ADMIN_NACHPRUEF_SEKUNDEN = 60


async def _discord_admin_status(user_id: int) -> Optional[bool]:
    """Dreiwertige Auskunft zur Dashboard-Adminrolle.

    ``True``  – Mitglied gefunden, Rolle vorhanden.
    ``False`` – Mitglied gefunden, Rolle NICHT vorhanden (echter Entzug).
    ``None``  – nicht feststellbar: keine Rolle konfiguriert, Bot nicht
                angemeldet, oder die Person war in keiner verbundenen Guild
                auffindbar.

    Die Unterscheidung ist der ganze Punkt: ``_discord_user_is_admin`` liefert
    fuer "kein Admin" und "kann ich gerade nicht sagen" dasselbe ``False``.
    Wuerde man darauf einen Entzug stuetzen, sperrte ein Discord-Ausfall oder
    ein kurzzeitig nicht abrufbares Mitglied jeden Betreiber aus.
    """
    try:
        role_id = int(str(cfg.config.get("dashboard_admin_role_id") or "0") or 0)
    except (TypeError, ValueError):
        return None
    if not role_id or bot is None or getattr(bot, "user", None) is None:
        return None
    gesehen = False
    for gid in _configured_guild_ids():
        guild = bot.get_guild(gid)
        if guild is None:
            continue
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:  # noqa: BLE001 – nicht Mitglied / nicht abrufbar
                continue
        gesehen = True
        if any(int(r.id) == role_id for r in getattr(member, "roles", [])):
            return True
    return False if gesehen else None


async def _require_admin(request: web.Request) -> Optional[web.Response]:
    """None = darf. Sonst die fertige Fehlerantwort.

    Die Adminrolle wird nicht nur beim Anmelden geprueft, sondern regelmaessig
    neu: die Session laeuft bei Benutzung immer weiter, sodass ein entzogener
    Betreiber-Zugang sonst beliebig lange gueltig blieb – samt Zugriff auf
    fremde Kundenserver, Guild-Zuordnung, Loeschen und Token-Anzeige.
    """
    sess = _sess_get(request)
    if not sess:
        return err("Session abgelaufen – bitte neu anmelden.", 401)
    if not sess.get("is_admin"):
        return err("Dafür fehlt dir die nötige Discord-Rolle.", 403)
    uid = str(((sess.get("discord") or {}).get("id")) or "")
    if uid:
        jetzt = time.time()
        if jetzt - float(sess.get("admin_geprueft_ts") or 0) > _ADMIN_NACHPRUEF_SEKUNDEN:
            try:
                stand = await _discord_admin_status(int(uid))
            except Exception as e:  # noqa: BLE001
                dash_log.warning(f"[ADMIN] Rolle nicht nachprüfbar: {e}")
                stand = None
            # Nur ein EINDEUTIGES "Rolle weg" entzieht die Rechte. Bei None
            # (nicht feststellbar) bleibt alles wie es war und es wird auch
            # kein Prueffzeitpunkt gesetzt, damit es gleich nochmal versucht wird.
            if stand is not None:
                sess["admin_geprueft_ts"] = jetzt
                if stand is False:
                    sess["is_admin"] = False
                    _audit_add("dashboard", _audit_actor(sess),
                               "Adminrechte entzogen",
                               "Discord-Rolle nicht mehr vorhanden")
                    return err("Dafür fehlt dir die nötige Discord-Rolle.", 403)
    return None


async def api_audit(request: web.Request) -> web.Response:
    denied = await _require_admin(request)
    if denied is not None:
        return denied
    entries = list(_audit_log)
    entries.reverse()                       # neueste zuerst
    return ok({"entries": entries, "max": _AUDIT_MAX})


_BACKUP_VERZEICHNIS = "backups"


def _voll_backup_erstellen(keep: int = 7) -> Optional[str]:
    """Zip mit der KOMPLETTEN Kundenkonfiguration aller Server – anders als
    ``economy_backup()`` (nur die Geld-Datenbank) auch Zugangsdaten,
    Guild-Zuordnungen, Feeds, Zonen, Shop-Kataloge und Ankündigungen. Enthält
    damit die Nitrado-Tokens ALLER Kunden – Zugriff ist deshalb bewusst auf
    Dashboard-Admins (``_require_admin``) beschränkt, siehe ``api_admin_backup``.
    Behält die neuesten ``keep`` Stück, ältere werden gelöscht."""
    try:
        os.makedirs(_BACKUP_VERZEICHNIS, exist_ok=True)
        zeitstempel = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        ziel = os.path.join(_BACKUP_VERZEICHNIS, f"betreiber_backup-{zeitstempel}.zip")

        dateien = [CONFIG_FILE, CONNECTIONS_FILE, GUILDS_FILE, ANNOUNCEMENTS_FILE,
                  BANLIST_FILE, LOG_STATE_FILE, WHITELIST_REQ_FILE,
                  _AUDIT_FILE, EV_PERSIST_FILE, ECON_DB_FILE]
        dateien += glob.glob("shop_items*.json")

        with zipfile.ZipFile(ziel, "w", zipfile.ZIP_DEFLATED) as z:
            for pfad in dateien:
                if os.path.exists(pfad):
                    z.write(pfad, arcname=os.path.basename(pfad))

        alle = sorted(glob.glob(os.path.join(_BACKUP_VERZEICHNIS, "betreiber_backup-*.zip")))
        for alt in alle[:-keep] if keep > 0 else alle:
            try:
                os.remove(alt)
            except OSError:
                pass
        return ziel
    except Exception as e:  # noqa: BLE001
        log.error(f"[BACKUP] Komplett-Backup fehlgeschlagen: {e}")
        return None


def _feeds_zaehlen(guild_id: int) -> int:
    """Wie viele Feeds sind in dieser Guild wirklich eingerichtet?

    Zaehlt beides: die aktuellen, je Server abgelegten Feeds
    (``guilds[gid]["servers"][sid]``) und die alten flachen Schluessel auf
    Guild-Ebene. Vorher wurden nur die flachen ``LOG_TYPES`` gezaehlt – eine
    vollstaendig eingerichtete Guild erschien dadurch mit ``0``.
    """
    eintrag = cfg.guilds.get(str(guild_id)) or {}
    schluessel = {k for k in eintrag if k in FEED_TYPES or k in LOG_TYPES}
    for feeds in (eintrag.get("servers") or {}).values():
        if isinstance(feeds, dict):
            schluessel |= {k for k in feeds if k in FEED_TYPES or k in LOG_TYPES}
    return len(schluessel)


async def api_admin_backup(request: web.Request) -> web.Response:
    """Komplett-Backup ALLER Kundendaten zum Download – enthält Nitrado-Tokens
    sämtlicher Server, deshalb wie Serverliste/Logs/Guild IDs strikt auf
    Dashboard-Admins beschränkt (nicht auf eine einzelne Kunden-Guild-Rolle)."""
    denied = await _require_admin(request)
    if denied is not None:
        return denied
    loop = asyncio.get_running_loop()
    keep = max(1, int(cfg.config.get("betreiber_backup_keep", 7)))
    pfad = await loop.run_in_executor(None, _voll_backup_erstellen, keep)
    if not pfad or not os.path.exists(pfad):
        return err("Backup konnte nicht erstellt werden – siehe Log.")
    with open(pfad, "rb") as f:
        daten = f.read()
    return web.Response(
        body=daten, content_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{os.path.basename(pfad)}"'})


async def api_admin_guilds(request: web.Request) -> web.Response:
    """Alle verbundenen Discord-Server mit Namen – für die Kategorie Guild IDs."""
    denied = await _require_admin(request)
    if denied is not None:
        return denied
    out = []
    for gid in _configured_guild_ids():
        guild = bot.get_guild(gid) if bot is not None else None
        out.append({
            "id": str(gid),
            "name": (guild.name if guild is not None else None),
            "available": guild is not None,
            "members": (guild.member_count if guild is not None else None),
            # Aktuelle Feeds liegen je Server unter "servers"; nur die flachen
            # LOG_TYPES zu zaehlen liess jede korrekt eingerichtete Guild mit
            # "0 Feeds" erscheinen.
            "feeds": _feeds_zaehlen(gid),
        })
    return ok({"guilds": out, "bot_online": bool(
        bot is not None and getattr(bot, "user", None) is not None)})


# ──────────────────────────────────────────────────────────────────────────
#  Serverliste (Bot-Owner) und Optionen (jeder angemeldete Nutzer)
# ──────────────────────────────────────────────────────────────────────────
def _conn_for_session(sess: Optional[Dict[str, Any]]) -> Optional[ServerConnection]:
    """Die Verbindung, die zu dieser Anmeldung gehört – sonst None.

    Bewusst OHNE Rückfall auf den Hauptserver: wer keinen eigenen Server
    ausgewählt hat, darf nicht den des Betreibers bekommen.
    """
    return connections.for_service((sess or {}).get("service_id"))


DASHBOARD_PREMIUM_TEXT = (
    "Du hast kein Premium. Dieser Nitrado-Server ist noch keinem Discord-Server "
    "zugeordnet – der Bot-Betreiber schaltet ihn frei.")

# Was auch ohne Premium erreichbar bleibt:
#  - der Serverstatus fuer die Kopfzeile (sonst staende dort dauerhaft ein
#    Fehler, obwohl es der eigene Server ist)
#  - das Anfragen/Zuordnen einer Guild-ID (der "Ja, habe ich"-Schritt im
#    Onboarding): GENAU das ist der Weg, Premium ueberhaupt erst zu bekommen.
#    Eine Sperre davor waere ein Henne-Ei-Problem - niemand ohne Premium
#    koennte je danach fragen. post_setup_guild prueft seine heikle Aktion
#    (die eigentliche Zuordnung) ohnehin selbst ab; der Anfrage-Zweig
#    schreibt nur die eigene guild_id_requested, nichts Fremdes.
_PREMIUM_FREIE_PFADE = ("/api/server/status", "/api/auth/guild")


def _sitzung_hat_premium(sess: Optional[Dict[str, Any]],
                         conn: Optional[ServerConnection]) -> bool:
    """Ist der Server DIESER Anmeldung freigeschaltet?

    Freischaltung heisst: dem Nitrado-Server ist eine Discord-Guild zugeordnet –
    dieselbe Bedingung, an der auch ``_premium_check`` die Slash-Befehle haengt.
    Bewusst pro gewaehltem Server: wer zwei Server hat, von denen nur einer frei
    ist, soll beim anderen auch kein Dashboard bekommen.

    Der Betreiber (Dashboard-Admin-Rolle) kommt immer durch – sonst koennte er
    die Freischaltung gar nicht erst vornehmen.
    """
    if (sess or {}).get("is_admin"):
        return True
    return bool(conn is not None and conn.guild_id)


def _session_conn(request: web.Request) -> Tuple[Optional[ServerConnection],
                                                 Optional[web.Response]]:
    """``(verbindung, fehlerantwort)`` für Endpunkte, die auf einen Server gehören.

    Damit hängt jeder Zugriff am eigenen Server der Anmeldung – ohne diese
    Klammer würden Endpunkte weiterhin global arbeiten und Daten anderer
    Kunden preisgeben.
    """
    sess = _sess_get(request)
    if not sess:
        return None, err("Session abgelaufen – bitte neu anmelden.", 401)
    conn = _conn_for_session(sess)
    if conn is None:
        return None, err("Für diese Anmeldung ist kein Nitrado-Server ausgewählt.", 409)
    # Die Anmeldung haengt bisher nur an der gemerkten service_id. Wechselt der
    # Eigentuemer des Servers, blieb eine alte Sitzung des frueheren Besitzers
    # voll bedienbar – inklusive Token-Anzeige. Deshalb bei jedem Zugriff
    # gegenpruefen, wem der Server JETZT gehoert.
    besitzer = str(conn.data.get("owner_discord_id") or "").strip()
    meine = str(((sess.get("discord") or {}).get("id")) or "").strip()
    if besitzer and meine and besitzer != meine and not sess.get("is_admin"):
        sess["service_id"] = None
        return None, err("Dieser Nitrado-Server gehört inzwischen einem anderen "
                         "Discord-Konto. Bitte neu anmelden.", 403)
    if not _sitzung_hat_premium(sess, conn) and request.path not in _PREMIUM_FREIE_PFADE:
        return None, err(DASHBOARD_PREMIUM_TEXT, 403)
    return conn, None


def _session_guilds(request: web.Request) -> List[int]:
    """Die Discord-Guilds, die diese Anmeldung sehen darf.

    Das ist genau die Guild des eigenen Servers – der Betreiber mit
    Admin-Rolle sieht alle.
    """
    sess = _sess_get(request)
    if (sess or {}).get("is_admin"):
        out = [int(g) for g in _configured_guild_ids()]
        for gid in cfg.guilds.keys():
            try:
                if int(gid) not in out:
                    out.append(int(gid))
            except (TypeError, ValueError):
                continue
        return out
    conn = _conn_for_session(sess)
    return [conn.guild_id] if (conn is not None and conn.guild_id) else []


async def _refresh_server_name(conn: ServerConnection) -> None:
    """Servernamen bei Bedarf von Nitrado nachladen.

    Nach der Migration einer alten Installation steht dort nur „Server <ID>“ –
    der echte Name kommt erst beim ersten Blick in die Serverliste dazu.
    """
    if not conn.name.startswith("Server ") or conn.api is None:
        return
    try:
        info = await conn.api.get_info()
    except Exception:  # noqa: BLE001 – Name ist Beiwerk, kein Fehlergrund
        return
    details = (info or {}).get("details") or {}
    name = str(details.get("name") or details.get("game") or "").strip()
    if name and name != conn.name:
        conn.data["name"] = name
        connections.save()


async def api_admin_servers(request: web.Request) -> web.Response:
    """Alle verbundenen Nitrado-Server mit ihrer Guild-Zuordnung."""
    denied = await _require_admin(request)
    if denied is not None:
        return denied
    out = []
    konten: Dict[str, Optional[Dict[str, Optional[str]]]] = {}
    for conn in connections.all():
        await _refresh_server_name(conn)
        view = conn.view()
        guild = (bot.get_guild(conn.guild_id)
                 if (bot is not None and conn.guild_id) else None)
        view["guild_name"] = (guild.name if guild is not None else None)
        view["guild_available"] = guild is not None
        # Wer diesen Server verbunden bzw. die Freischaltung angefragt hat.
        # Je Konto nur einmal nachschlagen – ein Kunde mit mehreren Servern
        # soll nicht mehrfach dieselbe Discord-Anfrage auslösen.
        besitzer = str(conn.data.get("owner_discord_id") or "").strip()
        if besitzer:
            if besitzer not in konten:
                konten[besitzer] = await _discord_konto_kurz(besitzer)
            view["owner"] = konten[besitzer]
        else:
            view["owner"] = None
        out.append(view)
    out.sort(key=lambda v: (v["guild_id"] is None, v["name"].lower()))
    return ok({"servers": out})


async def post_admin_server_guild(request: web.Request) -> web.Response:
    """Guild-ID einem Nitrado-Server zuordnen (der Premium-Schalter)."""
    denied = await _require_admin(request)
    if denied is not None:
        return denied
    service_id = request.match_info["service_id"]
    data = await body(request)
    raw = str(data.get("guild_id", "")).strip()

    if not raw:                                   # leer = Zuordnung entfernen
        # Die bisherige Guild VOR dem Zuordnen lesen – danach ist sie None.
        _alt = connections.for_service(service_id)
        alte_gid = _alt.guild_id if _alt is not None else None
        besitzer = _alt.data.get("owner_discord_id") if _alt is not None else None
        okay, msg = connections.assign_guild(service_id, None)
        if not okay:
            return err(msg)
        result = await _guild_aufraeumen(alte_gid)
        # Erst NACH assign_guild pruefen – sonst zaehlt der gerade entzogene
        # Server noch als Premium und die Rolle bliebe stehen.
        if not _hat_noch_premium(besitzer):
            hinweis = await _premium_rolle(besitzer, False)
            if hinweis:
                result["premium_rolle"] = hinweis
        result["message"] = msg
        return ok(result)
    if not raw.isdigit() or not (17 <= len(raw) <= 20):
        return err("Das sieht nicht nach einer Discord-Server-ID aus – sie besteht "
                   "nur aus Ziffern (Rechtsklick auf den Server → Server-ID kopieren).")
    gid = int(raw)
    if gid in _PLACEHOLDER_GUILD_IDS:
        return err("Das ist die Beispiel-ID aus der Anleitung, nicht die deines Servers.")

    okay, msg = connections.assign_guild(service_id, gid)
    if not okay:
        return err(msg)
    _ziel = connections.for_service(service_id)
    if _ziel is not None:
        _ziel.data.pop("guild_id_requested", None)   # Anfrage ist erledigt
        connections.save()

    # Zuordnung heißt Freischaltung – die Befehle sollen sofort dort stehen.
    ids = _configured_guild_ids()
    if gid not in ids:
        ids.append(gid)
        cfg.config["guild_ids"] = ids
        cfg.save_config()
    result = await _register_guild_commands(gid)
    # Freischaltung heisst Premium – der Kunde bekommt die Premium-Rolle im
    # Betreiber-Discord. Ein Fehlschlag steht als Hinweis in der Antwort, macht
    # die Freischaltung selbst aber nicht rueckgaengig.
    hinweis = await _premium_rolle(
        _ziel.data.get("owner_discord_id") if _ziel is not None else None, True)
    if hinweis:
        result["premium_rolle"] = hinweis
    result["message"] = msg
    return ok(result)


def _hat_noch_premium(owner_id: Any) -> bool:
    """Hat dieses Discord-Konto noch einen ANDEREN freigeschalteten Server?

    Verhindert, dass jemandem mit mehreren Servern die Premium-Rolle abgezogen
    wird, nur weil einer davon entfaellt.
    """
    uid = str(owner_id or "").strip()
    if not uid:
        return False
    return any(str(c.data.get("owner_discord_id") or "") == uid and c.guild_id
               for c in connections.all())


async def _premium_rolle(owner_id: Any, geben: bool) -> str:
    """Die Premium-Rolle im Betreiber-Discord vergeben oder wieder abziehen.

    Gibt eine kurze Meldung fuer das Dashboard zurueck ("" = nichts zu tun).
    Scheitern ist NIE hart: eine Freischaltung darf nicht daran haengen, dass
    Discord klemmt oder der Kunde dem Betreiber-Discord nicht beigetreten ist.
    """
    uid = str(owner_id or "").strip()
    gid = str(cfg.config.get("premium_role_guild_id") or "").strip()
    rid = str(cfg.config.get("premium_role_id") or "").strip()
    if not (uid and gid and rid):
        return ""
    if bot is None or getattr(bot, "user", None) is None:
        return "Premium-Rolle: Bot ist nicht bei Discord angemeldet."
    try:
        guild = bot.get_guild(int(gid))
    except (TypeError, ValueError):
        return "Premium-Rolle: premium_role_guild_id ist keine gültige ID."
    if guild is None:
        return ("Premium-Rolle: Der Bot ist nicht in dem Discord-Server "
                f"{gid} – Rolle nicht vergeben.")
    try:
        rolle = guild.get_role(int(rid))
    except (TypeError, ValueError):
        return "Premium-Rolle: premium_role_id ist keine gültige ID."
    if rolle is None:
        return f"Premium-Rolle: Rolle {rid} gibt es in „{guild.name}“ nicht."

    try:
        member = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
    except discord.NotFound:
        return f"Premium-Rolle: Der Kunde ist nicht in „{guild.name}“."
    except discord.HTTPException as e:
        return f"Premium-Rolle: Discord antwortete nicht ({e})."
    except (TypeError, ValueError):
        return "Premium-Rolle: Die gespeicherte Kunden-ID ist unbrauchbar."

    hat = any(int(r.id) == rolle.id for r in getattr(member, "roles", []))
    if hat == geben:
        return ""                                  # schon im gewuenschten Zustand
    try:
        if geben:
            await member.add_roles(rolle, reason="Premium freigeschaltet (Dashboard)")
            log.info(f"[PREMIUM] {member} hat „{rolle.name}“ bekommen.")
            return f"„{rolle.name}“ an {member} vergeben."
        await member.remove_roles(rolle, reason="Premium zurückgenommen (Dashboard)")
        log.info(f"[PREMIUM] {member} hat „{rolle.name}“ verloren.")
        return f"„{rolle.name}“ bei {member} entfernt."
    except discord.Forbidden:
        return (f"Premium-Rolle: Dem Bot fehlt das Recht „Rollen verwalten“, oder "
                f"„{rolle.name}“ steht über seiner eigenen Rolle.")
    except discord.HTTPException as e:
        return f"Premium-Rolle: Discord lehnte die Änderung ab ({e})."


async def delete_admin_server(request: web.Request) -> web.Response:
    """Einen Nitrado-Server aus der Verwaltung entfernen.

    Entfernt **nur die Verbindung** – Zonen, Shop, Feeds, Kills, Käufe und
    Banliste bleiben gespeichert. Wird derselbe Server später erneut verbunden,
    findet er alles wieder vor.
    """
    denied = await _require_admin(request)
    if denied is not None:
        return denied
    service_id = str(request.match_info["service_id"])
    conn = connections.for_service(service_id)
    if conn is None:
        return err("Diesen Server gibt es nicht (mehr).", 404)

    name = conn.name
    alte_gid = conn.guild_id
    besitzer = conn.data.get("owner_discord_id")
    war_primary = connections.primary() is conn

    # Den Hauptserver festschreiben, BEVOR die Verbindung verschwindet.
    # Sonst rueckt die naechste Verbindung nach und erbt still den gesamten
    # Altbestand ohne service_id: Ankuendigungen, Ereignisse, den gemeinsamen
    # shop_items.json und die Ban-Angaben. War der Geloeschte selbst der
    # Hauptserver, wird seine eigene ID festgeschrieben – dann gibt es
    # vorerst keinen, und niemand erbt etwas.
    if not str(cfg.config.get("service_id") or "").strip():
        haupt = connections.primary()
        cfg.config["service_id"] = (service_id if war_primary or haupt is None
                                    else haupt.service_id)
        cfg.save_config()

    try:
        await conn.close()
    except Exception as e:  # noqa: BLE001 – ein Rest darf das Entfernen nicht aufhalten
        dash_log.warning(f"[ADMIN] Verbindung {service_id} liess sich nicht sauber "
                         f"schliessen: {e}")
    connections.remove(service_id)

    # Sitzungen, die auf diesen Server zeigen, ins Leere laufen lassen statt
    # abzumelden: der Nitrado-Token gehoert dem Konto, nicht der Verbindung.
    # Danach greift der vorhandene 409 aus _session_conn, und der Umschalter
    # bietet einen anderen Server an.
    gelöst = 0
    for sess in _SESS_STORE.values():
        if str(sess.get("service_id") or "") == service_id:
            sess["service_id"] = None
            sess["map_name"] = None
            gelöst += 1

    result = await _guild_aufraeumen(alte_gid)

    # War das sein letzter freigeschalteter Server, geht die Premium-Rolle ab.
    # Erst NACH connections.remove() – sonst zaehlt der Geloeschte noch mit.
    if alte_gid and not _hat_noch_premium(besitzer):
        hinweis = await _premium_rolle(besitzer, False)
        if hinweis:
            result["premium_rolle"] = hinweis

    # bot.nitrado/bot.ftp/bot.shop hingen am Hauptserver – nach dem Schliessen
    # zeigten sie auf ein totes Objekt.
    if war_primary and bot is not None:
        try:
            await bot.init_nitrado(force=True)
        except Exception as e:  # noqa: BLE001
            dash_log.warning(f"[ADMIN] Neubindung nach dem Entfernen fehlgeschlagen: {e}")

    log.info(f"[ADMIN] Server {service_id} ({name}) entfernt – Daten bleiben gespeichert.")
    result.update({"message": f"„{name}“ wurde entfernt. Die Daten bleiben gespeichert.",
                   "service_id": service_id, "sessions_geloest": gelöst})
    return ok(result)


async def api_options(request: web.Request) -> web.Response:
    """Eigener Nitrado-Token (maskiert) und die zugeordnete Guild."""
    sess = _sess_get(request)
    if not sess:
        return err("Session abgelaufen – bitte neu anmelden.", 401)
    conn = _conn_for_session(sess)
    if conn is None:
        return ok({"connected": False})
    guild = (bot.get_guild(conn.guild_id)
             if (bot is not None and conn.guild_id) else None)
    return ok({
        "connected": True,
        "service_id": conn.service_id,
        "name": conn.name,
        "map_name": conn.get("map_name"),
        "token_masked": conn.masked_token(),
        "guild_id": (str(conn.guild_id) if conn.guild_id else None),
        "guild_name": (guild.name if guild is not None else None),
        "guild_id_requested": (str(conn.data.get("guild_id_requested"))
                               if conn.data.get("guild_id_requested") else None),
        "is_admin": bool(sess.get("is_admin")),
    })


async def api_options_token_reveal(request: web.Request) -> web.Response:
    """Token im Klartext – nur auf ausdrückliche Anforderung (Auge-Button)."""
    sess = _sess_get(request)
    if not sess:
        return err("Session abgelaufen – bitte neu anmelden.", 401)
    conn = _conn_for_session(sess)
    if conn is None:
        return err("Noch kein Nitrado-Server verbunden.", 404)
    _audit_add("dashboard", _audit_actor(sess), "Nitrado-Token eingeblendet",
               conn.name)
    return ok({"token": conn.token})


async def post_options_token(request: web.Request) -> web.Response:
    """Nitrado-Token ändern: prüfen, speichern und sofort neu einrichten."""
    sess = _sess_get(request)
    if not sess:
        return err("Session abgelaufen – bitte neu anmelden.", 401)
    conn = _conn_for_session(sess)
    if conn is None:
        return err("Noch kein Nitrado-Server verbunden.", 404)
    data = await body(request)
    token = str(data.get("token", "")).strip()
    if not token:
        return err("Bitte einen Nitrado-Token eingeben.")
    if token == conn.token:
        return err("Das ist der bisherige Token – es gibt nichts zu ändern.")

    # Gegen die Nitrado-API prüfen: der Token muss genau diesen Server kennen,
    # sonst wäre die Verbindung nach dem Speichern kaputt.
    base = conn.get("nitrado_api_base", "https://api.nitrado.net")
    api = NitradoAPI(token=token, service_id="", base=base)
    try:
        services = await api.list_services()
    except Exception as e:  # noqa: BLE001
        return err(f"Token konnte nicht geprüft werden: {e}", 502)
    finally:
        await api.close()
    if not any(str(s.get("id")) == conn.service_id for s in (services or [])):
        return err(f"Dieser Token kennt den Server „{conn.name}“ nicht. "
                   "Stammt er vom selben Nitrado-Konto?", 400)

    conn.data["nitrado_token"] = token
    connections.save()
    # Der Hauptserver steht zusätzlich in der config.json – sonst liefe der Bot
    # nach einem Neustart wieder mit dem alten Token.
    if connections.primary() is conn:
        cfg.config["nitrado_token"] = token
        cfg.save_config()

    sess["token"] = token
    # Alle ANDEREN Sitzungen, die auf diesen Server zeigen, verlieren ihre
    # Gueltigkeit. Sonst bliebe ein alter oder entwendeter Cookie nach der
    # Token-Rotation weiter bedienbar und koennte ueber die Options-Seite sogar
    # den neuen Klartext-Token auslesen – die Rotation waere wirkungslos.
    _mich = request.cookies.get(_SESS_COOKIE)
    fremde = [s for s, v in _SESS_STORE.items()
              if s != _mich and str(v.get("service_id") or "") == conn.service_id]
    for s in fremde:
        _SESS_STORE.pop(s, None)
    if fremde:
        dash_log.info(f"[LOGIN] Token für {conn.name} geändert – "
                      f"{len(fremde)} andere Sitzung(en) abgemeldet.")
    try:
        # Nur den eigenen Server neu aufsetzen, nicht die aller Kunden
        await bot.init_nitrado(force=True, only=conn)
    except Exception as e:  # noqa: BLE001
        return err(f"Token gespeichert, Neueinrichtung fehlgeschlagen: {e}", 500)
    return ok({"token_masked": conn.masked_token(),
               "message": "Token geändert und neu eingerichtet."})


async def post_token(request: web.Request) -> web.Response:
    # Ist der Discord-Login eingerichtet, geht ohne ihn gar nichts.
    pre = _sess_get(request)
    if _discord_login_enabled() and not (pre and pre.get("discord")):
        return err("Bitte zuerst mit Discord anmelden.", 401)
    data = await body(request)
    token = str(data.get("token", "")).strip()
    if not token:
        return err("Bitte einen Nitrado-Token eingeben.")

    base = cfg.config.get("nitrado_api_base", "https://api.nitrado.net")
    api = NitradoAPI(token=token, service_id="", base=base)
    try:
        services = await api.list_services()
    finally:
        await api.close()

    gameservers = [s for s in services
                   if str(s.get("type", "")).lower() == "gameserver"]
    if not gameservers:
        return err("Über diesen Token wurden keine Gameserver gefunden. "
                   "Prüfe, ob der Long-Life-Token korrekt kopiert wurde "
                   "(Nitrado → Benutzereinstellungen → API-Schlüssel).", 401)

    view = [_server_view(s) for s in gameservers[:25]]
    resp = ok({"servers": view, "count": len(gameservers)})
    if pre is not None:
        # Die Session gibt es schon (Discord-Login) – sie wird ergänzt, nicht
        # ersetzt, sonst ginge die Anmeldung beim Token-Eingeben verloren.
        pre["token"] = token
        pre["gameservers"] = gameservers
    else:
        _sess_cookie(resp, _sess_create(token, gameservers), request)
    return resp


def _gehoert_mir(sess: Dict[str, Any], guild_id: Any) -> bool:
    """Gehoert diese Discord-Guild dem angemeldeten Konto?

    Geprueft wird AUSSCHLIESSLICH gegen ``sess["owned_guilds"]`` – die Liste,
    die beim Login mit dem Nutzer-Token von Discord kam. Eine ``guild_id`` aus
    dem Anfrage-Body ist dafuer nie eine Autoritaet, sonst koennte sich jeder
    eine fremde Guild unterschieben.
    """
    gid = str(guild_id or "").strip()
    if not gid:
        return False
    return any(str(g.get("id")) == gid for g in (sess.get("owned_guilds") or []))


def _conns_meiner_guild(sess: Dict[str, Any], guild_id: Any) -> List[ServerConnection]:
    """Verbindungen dieser Guild, die diesem Konto gehoeren."""
    meine = str((sess.get("discord") or {}).get("id") or "")
    treffer = connections.all_for_guild(guild_id)
    if sess.get("is_admin"):
        return treffer
    return [c for c in treffer
            if meine and str(c.data.get("owner_discord_id") or "") == meine]


async def api_my_guilds(request: web.Request) -> web.Response:
    """Die eigenen Discord-Server zur Auswahl – mit ihrem jeweiligen Zustand."""
    sess = _sess_get(request)
    if not sess:
        return err("Session abgelaufen – bitte neu anmelden.", 401)
    out = []
    for g in (sess.get("owned_guilds") or []):
        gid = str(g.get("id") or "")
        if not gid.isdigit():
            continue
        drin = bot is not None and bot.get_guild(int(gid)) is not None
        eigene = _conns_meiner_guild(sess, gid)
        conn = eigene[0] if eigene else None
        out.append({
            "id": gid,
            "name": g.get("name"),
            "icon_url": g.get("icon_url"),
            "bot_drin": drin,
            # Bewusst nur der ANZEIGENAME des eigenen Servers, kein Token.
            "server_name": conn.name if conn is not None else None,
            "server_anzahl": len(eigene),
            "premium": bool(conn is not None and conn.guild_id),
            # Einladung direkt fuer genau diesen Server, damit im Discord-Dialog
            # nicht noch einmal ausgewaehlt werden muss.
            "invite_url": (None if drin else
                           f"{_discord_invite_url()}&guild_id={gid}"
                           "&disable_guild_select=true"),
        })
    out.sort(key=lambda e: ((not e["bot_drin"]), str(e["name"] or "").lower()))
    return ok({"guilds": out, "gewaehlt": sess.get("guild_id")})


async def post_my_guilds_select(request: web.Request) -> web.Response:
    """Einen eigenen Discord-Server auswaehlen – der Einstieg ins Dashboard.

    Waehlen heisst NICHT freischalten: Premium vergibt weiterhin allein der
    Betreiber ueber die Serverliste. Die Auswahl ersetzt nur das Abtippen der
    Server-ID und wird als Anfrage vorgemerkt, sobald ein Nitrado-Server
    dazukommt.
    """
    sess = _sess_get(request)
    if not sess:
        return err("Session abgelaufen – bitte neu anmelden.", 401)
    data = await body(request)
    gid = str(data.get("guild_id", "")).strip()
    if not _gehoert_mir(sess, gid):
        return err("Dieser Discord-Server gehört nicht zu deinem Konto.", 403)

    sess["guild_id"] = gid
    eigene = _conns_meiner_guild(sess, gid)
    conn = eigene[0] if eigene else None
    if conn is not None:
        # Es gibt schon einen Nitrado-Server in dieser Guild – direkt darauf.
        sess["service_id"] = conn.service_id
        sess["map_name"] = conn.get("map_name")
        if conn.token:
            sess["token"] = conn.token
    else:
        # Guild gewaehlt, aber noch kein Nitrado-Server: gueltiger Zustand.
        # Der Token kommt spaeter unter Optionen dazu.
        sess["service_id"] = None
        sess["map_name"] = None
    _audit_add("dashboard", _audit_actor(sess), "Discord-Server gewählt",
               f"Guild {gid}")
    return ok({
        "guild_id": gid,
        "service_id": conn.service_id if conn is not None else None,
        "server_name": conn.name if conn is not None else None,
        "server_anzahl": len(eigene),
        "premium": bool(conn is not None and conn.guild_id),
    })


def _darf_umschalten(sess: Dict[str, Any], conn: ServerConnection) -> bool:
    """Darf diese Anmeldung auf diesen Server wechseln?

    Autoritaet ist bewusst die Registry und NICHT ``sess["gameservers"]``:
    die Liste ist nach einer Wiedererkennungs-Anmeldung leer, der Nutzer
    koennte dann seine eigenen Server nicht mehr wechseln. Umgekehrt darf
    ein fremder Server niemals erreichbar sein, nur weil er zufaellig unter
    demselben Nitrado-Token liegt.
    """
    if sess.get("is_admin"):
        return True
    meine = str((sess.get("discord") or {}).get("id") or "")
    return bool(meine) and str(conn.data.get("owner_discord_id") or "") == meine


async def api_servers_mine(request: web.Request) -> web.Response:
    """Die Server, zwischen denen diese Anmeldung umschalten darf."""
    sess = _sess_get(request)
    if not sess:
        return err("Session abgelaufen – bitte neu anmelden.", 401)
    if sess.get("is_admin"):
        meine = connections.all()
    else:
        meine = connections.for_owner((sess.get("discord") or {}).get("id"))
    meine = sorted(meine, key=lambda c: c.name.lower())
    aktuell = str(sess.get("service_id") or "")
    return ok({
        "current": aktuell or None,
        "servers": [{
            "service_id": c.service_id,
            "name": c.name,
            "guild_id": str(c.guild_id) if c.guild_id else None,
            "map_name": c.get("map_name") or None,
            "ftp_ok": bool(c.get("ftp_host")),
            "current": c.service_id == aktuell,
        } for c in meine],
    })


async def post_servers_select(request: web.Request) -> web.Response:
    """Zwischen bereits eingerichteten Servern umschalten.

    Nicht zu verwechseln mit ``post_select_server``: das richtet einen NEUEN
    Server aus der Nitrado-Serviceliste ein. Hier wird nur die Mandanten-
    klammer der Session umgehaengt.
    """
    sess = _sess_get(request)
    if not sess:
        return err("Session abgelaufen – bitte neu anmelden.", 401)
    if _discord_login_enabled() and not (sess.get("discord") or {}).get("id"):
        return err("Bitte zuerst mit Discord anmelden.", 401)
    data = await body(request)
    service_id = str(data.get("service_id", "")).strip()
    conn = connections.for_service(service_id)
    if conn is None:
        return err("Server nicht gefunden.", 404)
    if not _darf_umschalten(sess, conn):
        return err("Dieser Server gehört nicht zu deinem Konto.", 403)

    sess["service_id"] = conn.service_id
    sess["map_name"] = conn.get("map_name")
    # Der Token gehoert zum Server – sonst zeigte die Optionsseite nach dem
    # Wechsel weiterhin den Token des vorherigen Servers an.
    if conn.token:
        sess["token"] = conn.token
    _audit_add("dashboard", _audit_actor(sess), "Server gewechselt",
               f"{conn.name} ({conn.service_id})")
    return ok({
        "service_id": conn.service_id,
        "name": conn.name,
        "map_name": conn.get("map_name"),
        "guild_id": str(conn.guild_id) if conn.guild_id else None,
        "guild_configured": bool(conn.guild_id),
    })


async def post_select_server(request: web.Request) -> web.Response:
    sess = _sess_get(request)
    if not sess:
        return err("Session abgelaufen – bitte Token erneut eingeben.", 401)
    data = await body(request)
    service_id = str(data.get("service_id", "")).strip()
    if not service_id:
        return err("Bitte einen Server auswählen.")

    service = next((s for s in sess.get("gameservers", [])
                    if str(s.get("id")) == service_id), None)
    if not service:
        return err("Server nicht in dieser Session gefunden.", 404)

    token = sess["token"]
    _apply = _apply_gameserver_info

    # --- Server anlegen/auffrischen und Nitrado/FTP/Shop initialisieren ---
    # Die globale config.json bleibt unangetastet: sonst wuerde der zuletzt
    # eingerichtete Kunde zum Hauptserver und seine FTP-Zugangsdaten laegen
    # dort als Rueckfallebene fuer alle anderen bereit.
    vorhandene = connections.for_service(service_id)
    if vorhandene is not None and vorhandene.token != token:
        for k in ("ftp_log_dir", "ftp_ban_file", "ftp_profile_dir",
                  "ftp_mission_dir", "cfg_effect_area_path", "server_ip"):
            vorhandene.set(k, "")
        vorhandene.data["log_state"] = {}

    base = cfg.config.get("nitrado_api_base", "https://api.nitrado.net")
    api = NitradoAPI(token=token, service_id=service_id, base=base)
    warnings = []
    try:
        info = await api.get_info()
    finally:
        await api.close()

    # Erst die Verbindung anlegen, dann die erkannten Daten hineinschreiben.
    # Bewusst OHNE Werte aus der globalen config zu kopieren – ein neuer Kunde
    # soll nicht die FTP-Pfade, Zonen und Zeitpläne des Betreibers erben.
    # Die Guild-Zuordnung bleibt unangetastet, die vergibt der Bot-Owner.
    conn = connections.upsert(
        service_id,
        nitrado_token=token,
        name=_server_view(service)["name"],
        owner_discord_id=((sess.get("discord") or {}).get("id")))

    # Die beim Login gewaehlte Guild gilt als Anfrage – das ersetzt das
    # Abtippen der Server-ID. Freischalten bleibt Sache des Betreibers, deshalb
    # NUR guild_id_requested und niemals guild_id. Eine bereits bestehende
    # Freischaltung wird nicht angefasst.
    _gewaehlt = str(sess.get("guild_id") or "").strip()
    if _gewaehlt.isdigit() and not conn.guild_id and _gehoert_mir(sess, _gewaehlt):
        conn.data["guild_id_requested"] = int(_gewaehlt)
        connections.save()
        await _betreiber_alarm(
            f"🆕 Neue Premium-Anfrage: **{conn.name}** ({conn.service_id}) → "
            f"Discord-Server `{_gewaehlt}`.", farbe=0x3498DB)

    if info and _apply:
        _apply(info, conn)
        connections.save()
    else:
        warnings.append("Gameserver-Infos konnten nicht geladen werden – "
                        "FTP/Karte evtl. nicht erkannt.")

    # Nitrado/FTP/Shop live neu initialisieren (inkl. FTP-Auto-Discovery) –
    # nur fuer den gerade gewaehlten Server
    try:
        await bot.init_nitrado(force=True, only=conn)
        _panel_view_registrieren(conn)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"Init-Warnung: {e}")

    if not conn.get("ftp_host"):
        warnings.append("Keine FTP-Zugangsdaten gefunden – Log-Feeds & "
                        "Shop-Lieferung funktionieren so nicht.")

    sess["service_id"] = service_id
    sess["map_name"] = conn.get("map_name")
    return ok({
        "service_id": service_id,
        "map_name": conn.get("map_name"),
        "ftp_host": conn.get("ftp_host") or None,
        "log_dir": conn.get("ftp_log_dir") or None,
        "server_ip": conn.get("server_ip") or None,
        "name": _server_view(service)["name"],
        "warnings": warnings,
        "guild_configured": _guild_is_configured(),
    })


# ──────────────────────────────────────────────────────────────────────────
#  Discord-Anbindung: Guild-ID setzen und die Slash-Befehle registrieren.
#
#  Ohne Guild-ID registriert der Bot global – das dauert bei Discord bis zu
#  24 Stunden. Mit Guild-ID sind die Befehle sofort da. Bisher ging das nur
#  über guild_ids in der config.json und einen Neustart; hier passiert es im
#  laufenden Betrieb mit denselben zwei Aufrufen wie in setup_hook().
# ──────────────────────────────────────────────────────────────────────────

# Aus DEFAULT_CONFIG bzw. der Anleitung – keine echten Server, sondern
# Beispielwerte. Wer die stehen lässt, hat de facto nichts konfiguriert.
_PLACEHOLDER_GUILD_IDS = {111111111111111111, 222222222222222222}

# Nur als Notnagel, falls die eigene Application-ID nicht zu ermitteln ist
# (z. B. Dashboard-Vorschau ohne Discord-Login).
_DISCORD_INVITE_FALLBACK_CLIENT_ID = "1515972647115034634"


def _configured_guild_ids() -> List[int]:
    """Echte Guild-IDs aus der Konfiguration – Platzhalter zählen nicht."""
    out: List[int] = []
    for raw in (cfg.config.get("guild_ids") or []):
        try:
            gid = int(raw)
        except (TypeError, ValueError):
            continue
        if gid and gid not in _PLACEHOLDER_GUILD_IDS and gid not in out:
            out.append(gid)
    return out


def _guild_is_configured() -> bool:
    return bool(_configured_guild_ids())


def _discord_invite_url() -> str:
    """Einladungslink für genau den Bot, der hier läuft.

    Die Client-ID ist die Application-ID der laufenden Discord-App – fest
    eingetragen wäre sie falsch, sobald jemand den Bot mit einer eigenen App
    betreibt. Ohne Discord-Login greift die hinterlegte ID.
    """
    app_id = None
    try:
        app_id = getattr(bot, "application_id", None)
        if not app_id and getattr(bot, "user", None) is not None:
            app_id = bot.user.id
    except Exception:  # noqa: BLE001
        app_id = None
    client_id = str(app_id or _DISCORD_INVITE_FALLBACK_CLIENT_ID)
    # permissions=8 = Administrator (so gewünscht); scope bot + applications.commands,
    # damit der Bot beitreten UND Slash-Befehle registrieren darf.
    return ("https://discord.com/oauth2/authorize"
            f"?client_id={client_id}&permissions=8"
            "&integration_type=0&scope=bot+applications.commands")


async def _register_guild_commands(gid: int) -> dict:
    """Slash-Befehle für eine Guild registrieren und ehrlich berichten.

    Das ist zugleich die Prüfung, ob der Bot überhaupt auf dem Server ist:
    Discord antwortet mit 403, wenn er dort nicht Mitglied ist – dann kann es
    auch keine Befehle geben, und das Dashboard sagt es statt es zu verschweigen.
    """
    result = {
        "guild_id": str(gid),
        "invite_url": _discord_invite_url(),
        "registered": False,
        "in_guild": False,
        "guild_name": None,
        "command_count": 0,
        # Ohne Discord-Login lässt sich gar nichts prüfen. Das Dashboard darf
        # daraus nicht "Bot fehlt auf dem Server" machen und niemanden aussperren.
        "bot_online": bool(bot is not None and getattr(bot, "user", None) is not None),
        "note": "",
    }
    if bot is None or getattr(bot, "user", None) is None:
        result["note"] = ("Der Bot ist gerade nicht bei Discord eingeloggt. Die ID ist "
                          "gespeichert – die Befehle werden beim nächsten Bot-Start "
                          "registriert.")
        return result

    guild_obj = discord.Object(id=gid)
    try:
        bot.tree.copy_global_to(guild=guild_obj)
        synced = await bot.tree.sync(guild=guild_obj)
    except discord.Forbidden:
        result["note"] = ("Der Bot ist noch nicht auf diesem Discord-Server – solange "
                          "kann Discord keine Befehle registrieren.")
        return result
    except discord.NotFound:
        result["note"] = ("Diese Server-ID kennt Discord nicht. Bitte die ID im Discord "
                          "erneut kopieren (Rechtsklick auf den Server → Server-ID).")
        return result
    except discord.HTTPException as e:
        result["note"] = f"Discord hat die Registrierung abgelehnt: {e}"
        return result
    except Exception as e:  # noqa: BLE001 – lieber eine Meldung als ein 500er
        dash_log.warning(f"[BOT] Registrierung für Guild {gid} fehlgeschlagen: {e}")
        result["note"] = f"Registrierung fehlgeschlagen: {e}"
        return result

    guild = bot.get_guild(gid)
    result.update({
        "registered": True,
        "in_guild": True,
        # Direkt nach dem Einladen ist die Guild evtl. noch nicht im Cache –
        # der Sync hat trotzdem geklappt, nur der Name fehlt dann kurz.
        "guild_name": (guild.name if guild is not None else None),
        "command_count": len(synced),
    })
    log.info(f"[BOT] Slash-Befehle für Guild {gid} über das Dashboard registriert "
             f"({len(synced)} Befehle).")
    return result


async def _unregister_guild_commands(gid: int) -> dict:
    """Slash-Befehle einer Guild wieder entfernen – das Gegenstück zur Freischaltung.

    Ohne das blieben nach dem Zurücknehmen tote Befehle im Discord stehen, die
    nur noch „du hast kein Premium“ antworten. Der Aufrufer muss vorher prüfen,
    dass die Guild **keinen** Server mehr hat – sonst nimmt man den übrigen
    Servern derselben Guild ihre Befehle weg.
    """
    result = {"guild_id": str(gid), "unregistered": False,
              "bot_online": bool(bot is not None and getattr(bot, "user", None) is not None),
              "note": ""}
    if bot is None or getattr(bot, "user", None) is None:
        result["note"] = ("Der Bot ist gerade nicht bei Discord eingeloggt – die Befehle "
                          "verschwinden beim nächsten Bot-Start.")
        return result

    guild_obj = discord.Object(id=gid)
    try:
        bot.tree.clear_commands(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)
    except discord.Forbidden:
        result["note"] = "Der Bot ist nicht mehr auf diesem Discord-Server."
        return result
    except discord.NotFound:
        result["note"] = "Diese Server-ID kennt Discord nicht (mehr)."
        return result
    except discord.HTTPException as e:
        result["note"] = f"Discord hat das Entfernen abgelehnt: {e}"
        return result
    except Exception as e:  # noqa: BLE001 – lieber eine Meldung als ein 500er
        dash_log.warning(f"[BOT] Befehle für Guild {gid} nicht entfernbar: {e}")
        result["note"] = f"Entfernen fehlgeschlagen: {e}"
        return result

    result["unregistered"] = True
    log.info(f"[BOT] Slash-Befehle für Guild {gid} entfernt – kein Server mehr zugeordnet.")
    return result


async def _guild_aufraeumen(gid: Optional[int]) -> dict:
    """Nach dem Wegfall einer Zuordnung: Guild aus der Konfiguration lösen.

    Nur wenn die Guild danach **gar keinen** Server mehr hat. Erst speichern,
    dann synchronisieren – ein Discord-Ausfall darf das Zurücknehmen nicht
    scheitern lassen.
    """
    if not gid:
        return {}
    if connections.all_for_guild(int(gid)):
        return {}                       # andere Server dieser Guild bleiben

    # Gezielt diesen einen Eintrag streichen. Die Liste durch
    # _configured_guild_ids() zu ersetzen wuerde Platzhalter und
    # String-Eintraege stillschweigend wegwerfen.
    ids = cfg.config.get("guild_ids") or []
    rest = []
    for eintrag in ids:
        try:
            if int(eintrag) == int(gid):
                continue
        except (TypeError, ValueError):
            pass
        rest.append(eintrag)
    if len(rest) != len(ids):
        cfg.config["guild_ids"] = rest
        cfg.save_config()
    return await _unregister_guild_commands(int(gid))


async def post_setup_guild(request: web.Request) -> web.Response:
    """Guild-ID speichern und die Befehle sofort registrieren.

    Wird vom „Ja, habe ich"-Button erneut aufgerufen: der Aufruf ist bewusst
    wiederholbar, damit nach dem Einladen einfach noch einmal geprüft wird.
    """
    # Der Schritt gehoert zum eigenen Server. Ohne diese Klammer konnte jeder
    # Angemeldete eine beliebige fremde Guild-ID in die globale Konfiguration
    # schreiben und dort die Befehle synchronisieren lassen.
    conn, denied = _session_conn(request)
    if denied is not None:
        return denied
    data = await body(request)
    raw = str(data.get("guild_id", "")).strip()
    if not raw.isdigit() or not (17 <= len(raw) <= 20):
        return err("Das sieht nicht nach einer Discord-Server-ID aus. Sie besteht nur "
                   "aus Ziffern (Discord → Einstellungen → Erweitert → Entwicklermodus, "
                   "dann Rechtsklick auf den Server → Server-ID kopieren).")
    gid = int(raw)
    if gid in _PLACEHOLDER_GUILD_IDS:
        return err("Das ist die Beispiel-ID aus der Anleitung, nicht die deines Servers.")

    sess = _sess_get(request) or {}
    bestand = connections.all_for_guild(gid)
    schon_meine = conn in bestand
    # Wer in dieser Guild bereits einen eigenen Server hat, darf dort einen
    # weiteren anschliessen. Ohne diese Klammer waere das Wegfallen der
    # Kollisionspruefung eine Rechteausweitung: ein Kunde koennte sich per
    # zweitem Server in einen fremden Discord einklinken.
    meine = str((sess.get("discord") or {}).get("id") or "")
    eigener_bestand = bool(meine) and any(
        str(c.data.get("owner_discord_id") or "") == meine for c in bestand)

    if not (sess.get("is_admin") or schon_meine or eigener_bestand):
        # Freischalten ist sonst Sache des Bot-Betreibers (Serverliste).
        conn.data["guild_id_requested"] = gid
        connections.save()
        _audit_add("dashboard", _audit_actor(sess),
                   "Discord-Server angefragt", f"Guild {gid}")
        await _betreiber_alarm(
            f"🆕 Neue Premium-Anfrage: **{conn.name}** ({conn.service_id}) → "
            f"Discord-Server `{gid}`.", farbe=0x3498DB)
        return ok({
            "registered": False,
            "guild_id": str(gid),
            "requested": True,
            "note": ("Deine Discord-Server-ID ist gespeichert. Der Bot-Betreiber "
                     "schaltet sie frei – bis dahin antworten die Befehle in "
                     "Discord mit „du hast kein Premium“."),
            "invite_url": _discord_invite_url(),
        })

    # Ab hier darf zugeordnet werden: Betreiber, eigene bereits freigeschaltete
    # Guild, oder eine Guild, in der dem Konto schon ein Server gehoert.
    if not schon_meine:
        okay, meldung = connections.assign_guild(conn.service_id, gid)
        if not okay:
            return err(meldung)
        conn.data.pop("guild_id_requested", None)   # Anfrage erledigt
        connections.save()
        _audit_add("dashboard", _audit_actor(sess),
                   "Discord-Server zugeordnet", f"Guild {gid} → {conn.name}")

    # Ergänzen statt ersetzen – vorhandene Discord-Server bleiben angebunden.
    # Platzhalter fliegen dabei raus, sonst scheitert setup_hook() beim nächsten
    # Start an der Beispiel-ID.
    ids = _configured_guild_ids()
    if gid not in ids:
        ids.append(gid)
    if ids != list(cfg.config.get("guild_ids") or []):
        cfg.config["guild_ids"] = ids
        cfg.save_config()

    return ok(await _register_guild_commands(gid))


async def api_get_session(request: web.Request) -> web.Response:
    sess = _sess_get(request)
    # In der Einzeldatei ist ``cfg`` immer vorhanden (Modul-Global), der frühere
    # "nur wenn gebunden"-Umweg über den Context entfällt.
    configured = bool(str(cfg.config.get("service_id") or "").strip())
    guild_configured = _guild_is_configured()
    login_required = _discord_login_enabled()
    discord_user = (sess or {}).get("discord")
    if not sess or (login_required and not discord_user):
        return ok({"authed": False, "configured": configured,
                   "guild_configured": guild_configured,
                   "discord_login": login_required,
                   "discord": discord_user, "is_admin": False,
                   "bot": _bot_identity(), "version": _frontend_version(),
                   "token_invalid": bool((sess or {}).get("token_invalid"))})
    _conn = _conn_for_session(sess)
    return ok({
        "authed": bool(sess.get("token")),
        "token_invalid": bool(sess.get("token_invalid")),
        "server_name": _conn.name if _conn else None,
        # Ist der gewaehlte Server freigeschaltet? Danach richtet sich, welche
        # Seiten das Dashboard ueberhaupt oeffnet.
        "premium": _sitzung_hat_premium(sess, _conn),
        "premium_text": DASHBOARD_PREMIUM_TEXT,
        "guild_id_requested": (str(_conn.data.get("guild_id_requested"))
                               if _conn is not None
                               and _conn.data.get("guild_id_requested") else None),
        "discord_login": login_required,
        "discord": discord_user,
        # Name und Bild des Bots fuer die Kopfzeile – oeffentliche Discord-Angaben,
        # kein Token und keine Application-ID.
        "bot": _bot_identity(),
        "is_admin": bool(sess.get("is_admin")),
        # Bewusst OHNE Rueckfall auf die config.json: sonst saehe jede
        # Anmeldung ohne eigenen Server die Kennung des Betreibers.
        "service_id": sess.get("service_id"),
        "map_name": sess.get("map_name"),
        # Die gewaehlte Discord-Guild – zweiter Anker neben service_id. Ist sie
        # leer und es gibt eigene Guilds, steht der Auswahlschritt noch aus.
        "guild_gewaehlt": sess.get("guild_id"),
        "hat_eigene_guilds": bool(sess.get("owned_guilds")),
        # Welche Frontend-Fassung dieser Bot ausliefert – steht unter Optionen.
        "version": _frontend_version(),
        "configured": configured,
        "guild_configured": guild_configured,
        "invite_url": _discord_invite_url(),
        "servers": [_server_view(s) for s in sess.get("gameservers", [])[:25]],
    })


async def post_logout(request: web.Request) -> web.Response:
    _sess_destroy(request)
    resp = ok()
    resp.del_cookie(_SESS_COOKIE, path="/")
    return resp


# ──────────────────────────────────────────────────────────────────────────
#  Feeds: Feed-Typ → Discord-Channel zuordnen (wie ``/setup feeds``).
# ──────────────────────────────────────────────────────────────────────────
def _guild_payload(gid: int, service_id: Optional[str] = None) -> dict:
    g = bot.get_guild(int(gid)) if bot else None
    channels = []
    if g is not None:
        for ch in getattr(g, "text_channels", []):
            channels.append({"id": str(ch.id), "name": ch.name,
                             "category": getattr(ch.category, "name", None)})
        channels.sort(key=lambda c: (c["category"] or "", c["name"].lower()))
    # Feeds gehoeren seit dem Mehrserverbetrieb zum Server, nicht zur Guild –
    # sonst zeigte das Dashboard die Kanaele des Nachbarservers derselben Guild.
    # Die Reihenfolge folgt FEED_TYPES, damit die Tabelle stabil bleibt.
    feeds = []
    for i, k in enumerate(FEED_TYPES, start=1):
        s = cfg.feed_settings(int(gid), k, service_id)
        if not s:
            continue
        meta = FEED_TYPES[k]
        feeds.append({
            "id": i,                       # laufende Anzeigenummer
            "key": k,
            "label": meta["label"],
            "gruppe": meta["gruppe"],
            "emoji": meta["emoji"],
            "channel_id": str(s["channel_id"]),
            # Als #RRGGBB, damit <input type="color"> es direkt annimmt.
            "colour": f"#{int(s['colour']) & 0xFFFFFF:06x}",
            "location": s["location"],
            "footer_ts": s["footer_ts"],
            "note": s["note"],
        })
    return {
        "id": str(gid),
        "name": (g.name if g is not None else f"Guild {gid}"),
        "available": g is not None,
        "channels": channels,
        "feeds": feeds,
    }


async def get_feeds(request: web.Request) -> web.Response:
    """Die Feeds des Discord-Servers, zu dem der gewaehlte Nitrado-Server gehoert.

    Bewusst NICHT ueber ``_session_guilds``: das ist die Berechtigungsgrenze und
    liefert dem Betreiber alle konfigurierten Guilds. Angezeigt wurden dadurch
    auch Guild-Karten, deren Kanaele ``set_feed`` anschliessend mit 403 ablehnt –
    die Zuordnung Feed → Server haengt am gewaehlten Server. Wer eine andere
    Guild einrichten will, wechselt oben den Server; ueber ``/api/servers/mine``
    erreicht der Betreiber weiterhin jeden.
    """
    sess = _sess_get(request)
    conn = _conn_for_session(sess)
    sid = conn.service_id if conn is not None else None
    guild_ids = [int(conn.guild_id)] if (conn is not None and conn.guild_id) else []
    return ok({
        # Die waehlbaren Feed-Typen, nach Gruppen sortiert wie im Dropdown.
        "feed_types": [{"key": k, "label": v["label"], "gruppe": v["gruppe"],
                        "emoji": v["emoji"],
                        "colour": f"#{v['farbe'] & 0xFFFFFF:06x}"}
                       for k, v in FEED_TYPES.items()],
        "guilds": [_guild_payload(int(gid), sid) for gid in guild_ids],
        "service_id": sid,
        "server_name": conn.name if conn is not None else None,
    })


def _kanal_gehoert_guild(gid: int, kanal_id: int,
                         feld: str = "Channel") -> Optional[web.Response]:
    """Prueft, ob ein Channel wirklich in DIESER Guild liegt.

    Ohne diese Pruefung reicht es, die Channel-ID eines anderen Kunden zu
    kennen: die Route bindet zwar Guild und Verbindung an die Anmeldung,
    speichert die fremde ID danach aber ungeprueft, und der Versand stellt
    spaeter botweit zu. Kills, Chat und Adminmeldungen des einen Kunden
    landeten so im Discord des anderen.

    Gibt ``None`` zurueck, wenn alles stimmt, sonst die fertige Fehlerantwort.
    """
    g = bot.get_guild(int(gid)) if bot else None
    if g is None:
        if bot is None or not getattr(bot, "is_ready", lambda: False)():
            return err(f"Der Bot ist gerade nicht bei Discord angemeldet – die "
                       f"{feld}-ID lässt sich erst prüfen, wenn er wieder "
                       f"verbunden ist.", 409)
        return err(f"Der Bot erreicht diesen Discord-Server nicht – die {feld}-ID "
                   f"kann deshalb nicht geprüft werden. Ist der Bot dort "
                   f"eingeladen?", 409)
    if g.get_channel(int(kanal_id)) is None:
        return err(f"Diese {feld}-ID gibt es in dem gewählten Discord-Server nicht.")
    return None


async def set_feed(request: web.Request) -> web.Response:
    gid = request.match_info["guild_id"]
    log_type = request.match_info["log_type"]
    # LOG_TYPES bleibt gueltig, damit /setup feeds und Bestandsaufrufe weiter
    # funktionieren; FEED_TYPES sind die feinen Typen aus dem Dashboard.
    if log_type not in FEED_TYPES and log_type not in LOG_TYPES:
        return err(f"Unbekannter Feed-Typ: {log_type}")
    if int(gid) not in _session_guilds(request):
        return err("Dieser Discord-Server gehört nicht zu deinem Nitrado-Server.", 403)
    # Der Feed gehoert dem ausgewaehlten Server. Ohne diese Klammer setzte ein
    # Kunde den Kanal fuer beide Server seiner Guild gleichzeitig.
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    if conn.guild_id and int(conn.guild_id) != int(gid):
        return err("Dieser Discord-Server gehört nicht zu deinem Nitrado-Server.", 403)
    data = await body(request)
    channel_id = data.get("channel_id")
    if channel_id in (None, "", "0"):
        eigen = cfg.server_feeds(int(gid), conn.service_id, anlegen=True)
        entfernt = eigen.pop(log_type, None) is not None
        # Alt-Bestand auf Guild-Ebene mitraeumen, sonst greift er wieder als
        # Rueckfall, solange die Guild nur einen Server hat.
        if str(gid) in cfg.guilds and log_type in cfg.guilds[str(gid)]:
            del cfg.guilds[str(gid)][log_type]
            entfernt = True
        if entfernt:
            cfg.save_guilds()
        _audit_add("dashboard", _audit_actor(_sess_get(request)),
                   "Feed deaktiviert", f"{log_type} · {conn.name}")
        return ok({"cleared": True})
    try:
        kanal = int(channel_id)
    except (TypeError, ValueError):
        return err("Ungültige Channel-ID.")
    fehler = _kanal_gehoert_guild(int(gid), kanal)
    if fehler is not None:
        return fehler

    # Farbe kommt als "#rrggbb" aus dem Farbwaehler oder als Zahl.
    meta = FEED_TYPES.get(log_type) or {}
    farbe = meta.get("farbe", 0x5865F2)
    roh_farbe = data.get("colour")
    if roh_farbe not in (None, ""):
        try:
            farbe = (int(str(roh_farbe).lstrip("#"), 16) if isinstance(roh_farbe, str)
                     else int(roh_farbe)) & 0xFFFFFF
        except (TypeError, ValueError):
            return err("Ungültige Farbe.")

    eintrag = {
        "channel_id": kanal,
        "colour": farbe,
        "location": bool(data.get("location", True)),
        "footer_ts": bool(data.get("footer_ts", False)),
        # Die Notiz ist reiner Text fuer den Betreiber und wird im Frontend
        # ueber createTextNode ausgegeben – hier nur die Laenge begrenzen.
        "note": str(data.get("note") or "")[:200],
    }
    cfg.server_feeds(int(gid), conn.service_id, anlegen=True)[log_type] = eintrag
    cfg.save_guilds()
    _audit_add("dashboard", _audit_actor(_sess_get(request)),
               "Feed gesetzt", f"{log_type} → {channel_id} · {conn.name}")
    return ok({"log_type": log_type, "channel_id": str(channel_id)})


# ──────────────────────────────────────────────────────────────────────────
#  Diagnose: beantwortet "liest er ueberhaupt, erkennt er etwas, kommt es
#  an?" in Klartext, statt dass der Betreiber im Terminal danach suchen muss.
#  Alle Zahlen kommen aus dem Arbeitsspeicher (DayZLogParser/ServerConnection,
#  siehe deren Kommentare) – hier wird nichts zusaetzlich gespeichert.
# ──────────────────────────────────────────────────────────────────────────
def _diagnose_ampel(conn: ServerConnection) -> Dict[str, str]:
    """Eine Zeile Klartext-Ursache statt vieler Einzelfelder, die der
    Betreiber selbst zusammenreimen muesste."""
    if conn.ftp is None:
        return {"status": "rot", "grund": "Kein FTP aufgebaut – Zugangsdaten "
                                          "unvollständig oder Verbindung noch nicht eingerichtet."}
    if conn.guild_id is None:
        return {"status": "rot", "grund": "Keinem Discord-Server zugeordnet – "
                                          "der Bot-Betreiber schaltet das frei."}
    if conn.poll_zustand:
        return {"status": "rot", "grund": conn.poll_zustand}
    parser = conn.parser
    if parser is None or parser.zeilen_gelesen == 0:
        return {"status": "gelb", "grund": "Noch keine Log-Zeilen gelesen – "
                                          "wartet auf neue Aktivität auf dem Server."}
    if parser.zeilen_erkannt == 0:
        return {"status": "gelb",
               "grund": f"{parser.zeilen_gelesen} Zeile(n) gelesen, aber keine einzige "
                        f"erkannt – das Log-Format weicht vom Erwarteten ab (siehe "
                        f"„Unerkannte Zeilen“ unten)."}
    verlauf = list(conn.dispatch_verlauf)
    if verlauf and not any(e.get("ergebnis") == "gepostet" for e in verlauf):
        return {"status": "gelb", "grund": "Ereignisse werden erkannt, aber keines konnte "
                                          "gepostet werden – siehe „Letzte Ereignisse“ unten "
                                          "für den genauen Grund je Ereignis."}
    return {"status": "grün", "grund": "Läuft normal."}


async def api_diagnose(request: web.Request) -> web.Response:
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    parser = conn.parser
    state = conn.log_state.get("current") or {}
    ampel = _diagnose_ampel(conn)

    feed_status = []
    for ft in FEED_TYPES:
        feed = cfg.feed_settings(int(conn.guild_id), ft, conn.service_id) if conn.guild_id else None
        feed_status.append({
            "key": ft, "label": FEED_TYPES[ft]["label"], "gruppe": FEED_TYPES[ft]["gruppe"],
            "eingerichtet": bool(feed),
            "channel_id": str(feed["channel_id"]) if feed else None,
        })

    verlauf = [
        {"vor_sekunden": round(time.time() - e.get("zeit", 0)), "typ": e.get("typ"),
         "ergebnis": e.get("ergebnis"), "kandidaten": e.get("kandidaten"),
         "feed_typ": e.get("feed_typ"), "channel_id": e.get("channel"), "guild": e.get("guild")}
        for e in reversed(list(conn.dispatch_verlauf))
    ]

    return ok({
        "server": conn.name,
        "ampel": ampel,
        "ftp": {
            "verbunden": conn.ftp is not None,
            "host": conn.get("ftp_host") or None,
            "log_verzeichnis": conn.get("ftp_log_dir") or None,
        },
        "guild_id": str(conn.guild_id) if conn.guild_id else None,
        "log_datei": {
            "pfad": state.get("file"),
            "offset": state.get("offset"),
            "zuletzt_gepollt_vor_sekunden": (
                round(time.time() - float(conn.log_state.get("last_poll_ts") or 0))
                if conn.log_state.get("last_poll_ts") else None),
        },
        "zeilen_gelesen": parser.zeilen_gelesen if parser else 0,
        "zeilen_erkannt": parser.zeilen_erkannt if parser else 0,
        "unerkannte_zeilen": list(parser.unerkannte_zeilen) if parser else [],
        "dispatch_verlauf": verlauf,
        "feeds": feed_status,
    })


DIAGNOSE_NACHLESEN_ZEILEN = 200
# Puffer in Bytes fuer den Tail-Read: reicht bei ueblicher ADM-Zeilenlaenge
# grosszuegig fuer DIAGNOSE_NACHLESEN_ZEILEN Zeilen.
DIAGNOSE_NACHLESEN_BYTES = 60_000


async def api_diagnose_nachlesen(request: web.Request) -> web.Response:
    """Liest die letzten Zeilen der AKTUELL gepollten .ADM-Datei per FTP neu
    ein und schickt jedes erkannte Ereignis durch den ECHTEN _dispatch-Weg.

    Anders als ein kuenstliches Testereignis beweist das den kompletten Weg
    mit den eigenen, echten Log-Zeilen samt Parser. Der persistierte
    Lese-Offset (conn.log_state) bleibt unberuehrt – dies ist ein separater
    Tail-Read, der normale Poll-Zyklus laeuft unveraendert weiter. Ereignisse,
    die der normale Zyklus laengst gepostet hat, koennen dabei ein zweites
    Mal erscheinen – bewusst in Kauf genommen, um mit echten Daten zu pruefen.
    """
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    if conn.guild_id is None:
        return err("Dieser Server ist noch keinem Discord-Server zugeordnet.", 409)
    if conn.ftp is None:
        return err("Kein FTP aufgebaut – siehe Ampel oben.", 409)
    loop = asyncio.get_running_loop()
    latest = (conn.log_state.get("current") or {}).get("file")
    if not latest:
        log_dir = conn.get("ftp_log_dir")
        if not log_dir:
            return err("Noch kein Log-Verzeichnis bekannt.", 409)
        adm_files = await loop.run_in_executor(None, conn.ftp.list_adm_files, log_dir)
        if not adm_files:
            return err(f"Keine .ADM-Dateien in {log_dir} gefunden.", 409)
        latest = adm_files[-1]
    size = await loop.run_in_executor(None, conn.ftp.file_size_or_none, latest)
    if size is None:
        return err(f"Dateigröße von {latest} nicht ermittelbar (FTP-Fehler).", 502)
    start = max(0, size - DIAGNOSE_NACHLESEN_BYTES)
    content, _ = await loop.run_in_executor(None, conn.ftp.read_from_offset, latest, start)
    if content is None:
        return err(f"{latest} nicht lesbar (FTP-Fehler).", 502)
    zeilen = content.splitlines()
    if start > 0 and zeilen:
        zeilen = zeilen[1:]  # erste Zeile kann mittendrin abgeschnitten sein
    zeilen = zeilen[-DIAGNOSE_NACHLESEN_ZEILEN:]
    # Eigener Parser und _dispatch OHNE Nebenwirkungen: diese Zeilen hat der
    # Poll-Zyklus in aller Regel laengst verarbeitet. Wuerden sie erneut
    # gebucht, zaehlte jeder Klick denselben Kill nochmal und zahlte das
    # Kopfgeld erneut aus – die Diagnose soll zeigen, nicht verrechnen.
    events, eigen = _historisch_parsen(conn, "\n".join(zeilen))
    cap = max(1, int(conn.get("max_events_per_cycle", 30)))
    if len(events) > cap:
        events = events[-cap:]
    for ev in events:
        await bot._dispatch(ev, conn, nebenwirkungen=False)
    await bot._post_unparsed_zeilen(conn, parser=eigen)
    verlauf = list(conn.dispatch_verlauf)[-len(events):] if events else []
    gepostet = sum(1 for e in verlauf if e.get("ergebnis") == "gepostet")
    return ok({
        "datei": latest,
        "zeilen_gelesen": len(zeilen),
        "ereignisse_gefunden": len(events),
        "ereignisse_gepostet": gepostet,
    })


# ──────────────────────────────────────────────────────────────────────────
#  Zonen verwalten (Dashboard – die Discord-Befehle ``/zone create|edit|
#  remove`` gibt es nicht mehr, ``/zone list`` und ``/zone allowlist`` schon).
#
#  Zonen liegen in ``config.json["zones"]``. Schema (rückwärtskompatibel,
#  siehe ``_zone_payload``):
#  ``{id, name, type: "circular"|"polygon", x?, z?, radius?, points?,
#     channel_id?, ping_role_ids?, manage_role_ids?, guild_id, allowlist?}`` –
#  x = Ost (iZurvive), z = Nord. `x`/`z`/`radius` nur bei `circular`,
#  `points` (Liste aus `{x, z}`, mindestens 3) nur bei `polygon`.
# ──────────────────────────────────────────────────────────────────────────
def _find(name: str, conn: Optional[ServerConnection] = None):
    n = (name or "").strip().lower()
    for z in _zones(conn):
        if isinstance(z, dict) and str(z.get("name", "")).lower() == n:
            return z
    return None


def _zonen_ziel(request: web.Request, conn: ServerConnection,
                data: dict, alt: Optional[dict] = None
                ) -> Tuple[Optional[dict], Optional[web.Response]]:
    """Guild, Channel und Rolle einer Zone gegen die Anmeldung pruefen.

    Ohne diese Pruefung koennte ein Kunde eine Zone auf SEINEM Server anlegen
    und als Ziel einen Channel im Discord eines anderen Kunden eintragen – der
    Bot wuerde dann dort Alarme samt Rollen-Ping posten.
    """
    erlaubt = _session_guilds(request)
    eigen = conn.guild_id
    roh = data.get("guild_id")
    if roh:
        try:
            gid = int(roh)
        except (TypeError, ValueError):
            return None, err("guild_id muss eine Zahl sein.")
        if gid not in erlaubt:
            return None, err("Dieser Discord-Server gehört nicht zu deinem "
                             "Nitrado-Server.", 403)
    else:
        gid = int((alt or {}).get("guild_id") or eigen or 0)
        # Auch eine bestehende Zone kann noch auf eine Guild zeigen, die
        # inzwischen einem anderen Server gehoert – dann nicht weiterschreiben.
        if gid and gid != eigen and gid not in erlaubt:
            return None, err("Diese Zone zeigt auf einen fremden Discord-Server. "
                             "Trage die eigene Server-ID ein oder lege sie neu an.",
                             403)
    if not gid:
        return None, err("Für diesen Server ist noch kein Discord-Server "
                         "zugeordnet – der Bot-Betreiber schaltet ihn frei.", 409)

    g = bot.get_guild(gid) if bot else None
    ziel: Dict[str, Any] = {"guild_id": gid}
    for schluessel, feld in (("channel_id", "Channel"), ("role_id", "Rolle")):
        if schluessel not in data:
            continue
        roh_id = data.get(schluessel)
        if not roh_id:
            ziel[schluessel] = None
            continue
        try:
            wert = int(roh_id)
        except (TypeError, ValueError):
            return None, err(f"{feld}-ID muss eine Zahl sein.")
        # Unveraenderter Wert einer bestehenden Zone bei UNVERAENDERTER Guild:
        # nichts Neues, nichts zu pruefen. Sonst liesse sich beim Bearbeiten
        # nicht einmal mehr der Radius aendern, wenn das Formular Channel und
        # Rolle mitschickt.
        # Die Guild MUSS mitverglichen werden: nach einem Guild-Wechsel waere
        # der alte Channel sonst ungeprueft fuer die neue Guild gueltig – und
        # _post_feed stellt bei gesetzter channel_id ohne Guild-Bezug zu.
        if (alt is not None and alt.get(schluessel) == wert
                and int(alt.get("guild_id") or 0) == gid):
            ziel[schluessel] = wert
            continue
        # Channel/Rolle muessen in DIESER Guild liegen – sonst laesst sich der
        # Alarm in einen fremden Discord umleiten.
        if g is None:
            if bot is None or not bot.is_ready():
                # Der Bot ist (noch) nicht bei Discord angemeldet, etwa in der
                # Vorschau mit --dashboard-only. Dann ist "nicht gefunden" keine
                # Aussage ueber die Guild – ein NEUES Ziel bleibt trotzdem
                # ungeprueft und wird deshalb nicht angenommen.
                return None, err("Der Bot ist gerade nicht bei Discord angemeldet – "
                                 f"eine neue {feld}-ID lässt sich erst prüfen, wenn "
                                 "er wieder verbunden ist.", 409)
            return None, err("Der Bot erreicht diesen Discord-Server nicht – "
                             f"die {feld} kann deshalb nicht geprüft werden. "
                             "Ist der Bot dort eingeladen?", 409)
        treffer = (g.get_channel(wert) if schluessel == "channel_id"
                   else g.get_role(wert))
        if treffer is None:
            return None, err(f"Diese {feld}-ID gibt es in dem gewählten "
                             f"Discord-Server nicht.")
        ziel[schluessel] = wert

    # Mehrfachauswahl (Ping Roles, Allowlist Roles) – dieselbe Prüfung wie
    # oben, nur je Eintrag einer Liste statt eines einzelnen Werts.
    for schluessel, feld in (("ping_role_ids", "Ping-Rolle"),
                             ("manage_role_ids", "Allowlist-Rolle")):
        if schluessel not in data:
            continue
        roh_liste = data.get(schluessel)
        if not isinstance(roh_liste, list):
            return None, err(f"{schluessel} muss eine Liste sein.")
        werte = []
        for roh_id in roh_liste:
            try:
                werte.append(int(roh_id))
            except (TypeError, ValueError):
                return None, err(f"{feld}-ID muss eine Zahl sein.")
        alt_liste = (alt or {}).get(schluessel)
        unveraendert = (isinstance(alt_liste, list) and sorted(alt_liste) == sorted(werte)
                       and int((alt or {}).get("guild_id") or 0) == gid)
        if werte and not unveraendert:
            if g is None:
                if bot is None or not bot.is_ready():
                    return None, err("Der Bot ist gerade nicht bei Discord angemeldet – "
                                     f"neue {feld}n lassen sich erst prüfen, wenn er "
                                     "wieder verbunden ist.", 409)
                return None, err("Der Bot erreicht diesen Discord-Server nicht – "
                                 f"{feld}n können deshalb nicht geprüft werden. "
                                 "Ist der Bot dort eingeladen?", 409)
            for wert in werte:
                if g.get_role(wert) is None:
                    return None, err(f"Diese {feld}-ID gibt es in dem gewählten "
                                     f"Discord-Server nicht.")
        ziel[schluessel] = werte
    return ziel, None


async def list_zones(request: web.Request) -> web.Response:
    _c, denied = _session_conn(request)
    if denied is not None:
        return denied
    zones = [z for z in _zones(_c) if isinstance(z, dict) and z.get("name")]
    if _ensure_zone_ids(zones):
        _zones_save(_c)
    return ok({"zones": [_zone_payload(z) for z in zones],
               "map_name": _c.get("map_name", "ChernarusPlus")})


async def create_zone(request: web.Request) -> web.Response:
    _c, denied = _session_conn(request)
    if denied is not None:
        return denied
    data = await body(request)
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 60:
        return err("Zonen-Name fehlt oder ist länger als 60 Zeichen.")
    if _find(name, _c):
        return err(f"Zone '{name}' existiert bereits.")

    ztype = str(data.get("type") or "circular").strip().lower()
    if ztype not in ("circular", "polygon"):
        return err("type muss 'circular' oder 'polygon' sein.")

    zone: Dict[str, Any] = {"name": name, "type": ztype}
    if ztype == "polygon":
        points = data.get("points")
        pts_err = _validate_zone_points(points)
        if pts_err:
            return err(pts_err.replace("❌", "").strip())
        zone["points"] = [{"x": round(float(p["x"]), 1), "z": round(float(p["z"]), 1)}
                          for p in points]
    else:
        try:
            x = float(data["x"]); z = float(data["z"]); radius = float(data["radius"])
        except (KeyError, TypeError, ValueError):
            return err("x, z und radius müssen Zahlen sein.")
        geo_err = _validate_zone_geometry(x, z, radius)
        if geo_err:
            return err(geo_err.replace("❌", "").strip())
        zone["x"], zone["z"], zone["radius"] = round(x, 1), round(z, 1), round(radius, 1)

    ziel, denied = _zonen_ziel(request, _c, data)
    if denied is not None:
        return denied
    zone["channel_id"] = ziel.get("channel_id")
    zone["ping_role_ids"] = ziel.get("ping_role_ids", [])
    zone["manage_role_ids"] = ziel.get("manage_role_ids", [])
    zone["guild_id"] = ziel["guild_id"]
    zone["allowlist"] = _allowlist_aus_anfrage(data) or []

    zones = _zones(_c)
    zones.append(zone)
    _ensure_zone_ids(zones)
    _zones_save(_c)
    return ok(_zone_payload(zone))


async def update_zone(request: web.Request) -> web.Response:
    _c, denied = _session_conn(request)
    if denied is not None:
        return denied
    zone = _find(request.match_info["name"], _c)
    if not zone:
        return err("Zone nicht gefunden.", 404)
    data = await body(request)

    new_name = str(data.get("name", zone["name"])).strip() or zone["name"]
    if new_name.lower() != str(zone["name"]).lower() and _find(new_name, _c):
        return err(f"Zone '{new_name}' existiert bereits.")

    ztype = str(data.get("type") or zone.get("type") or "circular").strip().lower()
    if ztype not in ("circular", "polygon"):
        return err("type muss 'circular' oder 'polygon' sein.")

    new_points = None
    if ztype == "polygon":
        points = data.get("points", zone.get("points"))
        pts_err = _validate_zone_points(points)
        if pts_err:
            return err(pts_err.replace("❌", "").strip())
        new_points = [{"x": round(float(p["x"]), 1), "z": round(float(p["z"]), 1)}
                     for p in points]
    else:
        try:
            x = float(data.get("x", zone.get("x", 0.0)))
            z = float(data.get("z", zone.get("z", 0.0)))
            radius = float(data.get("radius", zone.get("radius", 0.0)))
        except (TypeError, ValueError):
            return err("x, z und radius müssen Zahlen sein.")
        geo_err = _validate_zone_geometry(x, z, radius)
        if geo_err:
            return err(geo_err.replace("❌", "").strip())

    ziel, denied = _zonen_ziel(request, _c, data, zone)
    if denied is not None:
        return denied

    old_name = str(zone["name"])
    zone["name"] = new_name
    zone["type"] = ztype
    if ztype == "polygon":
        zone["points"] = new_points
        zone.pop("x", None); zone.pop("z", None); zone.pop("radius", None)
    else:
        zone["x"], zone["z"], zone["radius"] = round(x, 1), round(z, 1), round(radius, 1)
        zone.pop("points", None)
    zone["guild_id"] = ziel["guild_id"]
    if "channel_id" in ziel:
        zone["channel_id"] = ziel["channel_id"]
    if "ping_role_ids" in ziel:
        zone["ping_role_ids"] = ziel["ping_role_ids"]
        zone.pop("role_id", None)
    if "manage_role_ids" in ziel:
        zone["manage_role_ids"] = ziel["manage_role_ids"]
    neue_allowlist = _allowlist_aus_anfrage(data)
    if neue_allowlist is not None:
        zone["allowlist"] = neue_allowlist
    _zones_save(_c)
    # Name/Geometrie/Typ koennen sich geaendert haben – die naechste frische
    # Position bewertet die Zone dann komplett neu, kein Nachzieh-Ping aus
    # dem alten Zustand.
    _reset_zone_state(old_name, _c)
    _reset_zone_state(new_name, _c)
    return ok(_zone_payload(zone))


async def delete_zone(request: web.Request) -> web.Response:
    _c, denied = _session_conn(request)
    if denied is not None:
        return denied
    zone = _find(request.match_info["name"], _c)
    if not zone:
        return err("Zone nicht gefunden.", 404)
    name = str(zone["name"])
    _zones(_c).remove(zone)
    _zones_save(_c)
    # Ping-Cooldown-Status im Bot zurücksetzen (wie /zone remove), falls vorhanden
    reset = _reset_zone_state
    if callable(reset):
        try:
            reset(name, _c)
        except Exception:
            pass
    return ok({"removed": name})


# ── Allowlist ─────────────────────────────────────────────────
async def get_allowlist(request: web.Request) -> web.Response:
    _c, denied = _session_conn(request)
    if denied is not None:
        return denied
    zone = _find(request.match_info["name"], _c)
    if not zone:
        return err("Zone nicht gefunden.", 404)
    return ok({"allowlist": zone.get("allowlist", [])})


async def add_allowlist(request: web.Request) -> web.Response:
    _c, denied = _session_conn(request)
    if denied is not None:
        return denied
    zone = _find(request.match_info["name"], _c)
    if not zone:
        return err("Zone nicht gefunden.", 404)
    data = await body(request)
    player = str(data.get("player", "")).strip()
    if not player:
        return err("Spielername fehlt.")
    al = zone.setdefault("allowlist", [])
    if player.lower() not in [str(p).lower() for p in al]:
        al.append(player)
        _zones_save(_c)
    return ok({"allowlist": al})


async def remove_allowlist(request: web.Request) -> web.Response:
    _c, denied = _session_conn(request)
    if denied is not None:
        return denied
    zone = _find(request.match_info["name"], _c)
    if not zone:
        return err("Zone nicht gefunden.", 404)
    player = request.match_info["player"]
    al = zone.get("allowlist", [])
    zone["allowlist"] = [p for p in al if str(p).lower() != player.lower()]
    _zones_save(_c)
    return ok({"allowlist": zone["allowlist"]})


# ── Rollen/Channels für Picker ────────────────────────────────
async def guild_roles(request: web.Request) -> web.Response:
    # Ohne freigeschalteten Server steht im Frontend kein gid bereit; dann kam
    # hier "null" an und int() liess den Aufruf mit 500 platzen.
    try:
        gid = int(request.match_info["guild_id"])
    except (TypeError, ValueError):
        return err("Keine gültige Discord-Server-ID.", 400)
    if gid not in _session_guilds(request):
        return err("Dieser Discord-Server gehört nicht zu deinem Nitrado-Server.", 403)
    g = bot.get_guild(gid) if bot else None
    if g is None:
        return ok({"roles": []})
    roles = [{"id": str(r.id), "name": r.name}
             for r in g.roles if not r.is_default()]
    return ok({"roles": roles})


async def guild_channels(request: web.Request) -> web.Response:
    try:
        gid = int(request.match_info["guild_id"])
    except (TypeError, ValueError):
        return err("Keine gültige Discord-Server-ID.", 400)
    if gid not in _session_guilds(request):
        return err("Dieser Discord-Server gehört nicht zu deinem Nitrado-Server.", 403)
    g = bot.get_guild(gid) if bot else None
    if g is None:
        return ok({"channels": []})
    channels = [{"id": str(c.id), "name": c.name} for c in g.text_channels]
    return ok({"channels": channels})


async def guild_seen_players(request: web.Request) -> web.Response:
    """Ingame-Namen dieser Guild (aus Connect-Events) – Grundlage für die
    Allowlist-Autofill im Zonen-Formular."""
    try:
        gid = int(request.match_info["guild_id"])
    except (TypeError, ValueError):
        return err("Keine gültige Discord-Server-ID.", 400)
    if gid not in _session_guilds(request):
        return err("Dieser Discord-Server gehört nicht zu deinem Nitrado-Server.", 403)
    # Nur die Namen des gerade gewaehlten Servers – sonst schlaegt das
    # Zonenformular von Server B Spieler vor, die es nur auf A gibt.
    _c = _conn_for_session(_sess_get(request))
    sid = _c.service_id if _c is not None else None
    return ok({"players": sorted(cfg.seen_players(gid, sid), key=str.lower)})


# ──────────────────────────────────────────────────────────────────────────
#  Auto-Aufgaben: geplante Server-Neustarts (wie ``/auto restart|off|status``).
# ──────────────────────────────────────────────────────────────────────────
def _next_run(conn: Optional[ServerConnection] = None):
    """Nächster geplanter Restart-Zeitpunkt DIESES Servers. Die Funktion ist
    eine METHODE der Bot-Instanz (nicht des Moduls), daher am Bot-Objekt holen."""
    fn = getattr(bot, "_next_scheduled_restart", None)
    if callable(fn):
        try:
            return fn(conn)
        except Exception:
            return None
    return None


async def get_auto_restart(request: web.Request) -> web.Response:
    conn, denied = _session_conn(request)
    if denied is not None:
        return denied
    sched = conn.get("auto_restart_schedule",
                     {"enabled": False, "first_time": "04:00", "interval_hours": 4})
    nxt = _next_run(conn)
    return ok({
        "schedule": sched,
        "next_run_ts": nxt,
        "after_purchase": bool(conn.get("auto_restart_after_purchase", False)),
        "restart_cooldown_seconds": int(conn.get("restart_cooldown_seconds", 300) or 300),
    })


async def set_auto_restart(request: web.Request) -> web.Response:
    conn, denied = _session_conn(request)
    if denied is not None:
        return denied
    data = await body(request)
    sched = dict(conn.get("auto_restart_schedule",
                          {"enabled": False, "first_time": "04:00", "interval_hours": 4}))

    if "enabled" in data:
        sched["enabled"] = bool(data["enabled"])
    if "first_time" in data:
        ft = str(data["first_time"]).strip()
        if not _DASH_TIME_RE.match(ft):
            return err("Startzeit muss im Format HH:MM (00:00–23:59) sein.")
        sched["first_time"] = ft
    if "interval_hours" in data:
        try:
            iv = int(data["interval_hours"])
        except (TypeError, ValueError):
            return err("Intervall muss eine Zahl sein.")
        if not 1 <= iv <= 24:
            return err("Intervall muss zwischen 1 und 24 Stunden liegen.")
        sched["interval_hours"] = iv

    _conn_store(conn, "auto_restart_schedule", sched)

    if "after_purchase" in data:
        _conn_store(conn, "auto_restart_after_purchase", bool(data["after_purchase"]))
    # Angekündigte Restarts DIESES Servers zurücksetzen, damit die neue Zeit
    # sauber greift – die Zeitpläne der anderen Kunden bleiben unberührt.
    try:
        bot._restart_announced = {k for k in bot._restart_announced
                                  if k[0] != conn.service_id}
    except Exception:
        pass

    return ok({"schedule": sched, "next_run_ts": _next_run(conn)})


# ──────────────────────────────────────────────────────────────────────────
#  Shop-Katalog: Items & Bundles anlegen/bearbeiten, Autofill der Classnames.
#
#  Arbeitet immer auf dem ``ShopCatalog`` des angemeldeten Servers
#  (``_session_conn(request).catalog``) – jeder Nitrado-Server hat seine eigene
#  Item-Liste. Ein "Bundle" ist ein Item mit mehreren ``classnames``; ein
#  Einzelitem hat ``classname``. Persistiert wird über ``katalog.save()``.
# ──────────────────────────────────────────────────────────────────────────
def _classnames(it: dict) -> List[str]:
    fn = _item_classnames
    if callable(fn):
        return fn(it)
    cls = it.get("classnames")
    if isinstance(cls, list) and cls:
        return [str(c) for c in cls]
    return [str(it["classname"])] if it.get("classname") else []


def _shop_persist(katalog: "ShopCatalog") -> bool:
    """Katalog dieses Servers in seine eigene Datei schreiben."""
    return bool(katalog.save())


def _item_role_ids(it: dict) -> List[int]:
    """Rollen, die dieses Item kaufen duerfen – leer heisst: alle duerfen.

    Wird von ``/buy`` und vom Dashboard gelesen. Unbrauchbare Eintraege werden
    still uebergangen: eine kaputte Zahl darf kein Item unverkaeuflich machen.
    """
    roh = it.get("role_ids")
    if not isinstance(roh, list):
        return []
    out: List[int] = []
    for r in roh:
        try:
            rid = int(r)
        except (TypeError, ValueError):
            continue
        if rid and rid not in out:
            out.append(rid)
    return out


def _item_view(it: dict) -> dict:
    cls = _classnames(it)
    return {
        "name": str(it.get("name") or (cls[0] if cls else "?")),
        "classnames": cls,
        "is_bundle": len(cls) > 1,
        "price": int(it.get("price", 0)),
        "category": str(it.get("category", "Misc")),
        "enabled": bool(it.get("enabled", True)),
        "max_amount_per_buy": int(it.get("max_amount_per_buy", 1)),
        "custom": bool(it.get("custom", False)),
        # Als Zeichenketten: Discord-IDs sind 19-stellig und verlieren als
        # JavaScript-Zahl die letzten Stellen.
        "role_ids": [str(r) for r in _item_role_ids(it)],
    }


async def list_items(request: web.Request) -> web.Response:
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    katalog = conn.catalog
    q = request.query.get("q", "").strip().lower()
    category = request.query.get("category", "").strip()
    try:
        page = max(1, int(request.query.get("page", 1)))
        page_size = min(200, max(1, int(request.query.get("page_size", 50))))
    except ValueError:
        page, page_size = 1, 50

    items = katalog.items
    if category:
        items = [it for it in items if str(it.get("category", "Misc")) == category]
    if q:
        def match(it):
            hay = " ".join([str(it.get("name", "")).lower()] +
                           [c.lower() for c in _classnames(it)])
            return q in hay
        items = [it for it in items if match(it)]

    total = len(items)
    start = (page - 1) * page_size
    view = [_item_view(it) for it in items[start:start + page_size]]
    return ok({"items": view, "total": total, "page": page, "page_size": page_size,
               "source": getattr(katalog, "source", "?"),
               "server": conn.name, "service_id": conn.service_id})


async def api_shop_categories(request: web.Request) -> web.Response:
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    counts = {k: len(v) for k, v in getattr(conn.catalog, "by_category", {}).items()}
    preise = conn.get("shop_category_prices") or {}
    names = set(counts)
    names.update(preise.keys())
    names.update(_eigene_kategorien(conn))
    cats = [{"name": n, "count": counts.get(n, 0),
             "default_price": preise.get(n)}
            for n in sorted(names, key=str.lower)]
    return ok({"categories": cats,
               "default_price": int(conn.get("shop_default_price", 100) or 100)})


async def api_shop_classnames(request: web.Request) -> web.Response:
    """Autofill: Classnames (Einzelitems) per Substring-Suche."""
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    q = request.query.get("q", "").strip().lower()
    limit = min(50, max(1, int(request.query.get("limit", 25) or 25)))
    out = []
    seen = set()
    for it in conn.catalog.items:
        cls = _classnames(it)
        if len(cls) != 1:
            continue  # Bundles nicht als Classname vorschlagen
        cn = cls[0]
        key = cn.lower()
        if key in seen:
            continue
        name = str(it.get("name") or cn)
        if not q or q in key or q in name.lower():
            seen.add(key)
            out.append({"classname": cn, "name": name,
                        "category": str(it.get("category", "Misc"))})
            if len(out) >= limit:
                break
    return ok({"classnames": out})


def _split_classnames(raw) -> List[str]:
    if isinstance(raw, list):
        toks = [str(t) for t in raw]
    else:
        toks = re.split(r"[,;\s]+", str(raw or "").strip())
    parts, seen = [], set()
    for tok in toks:
        tok = tok.strip()
        if tok and tok.lower() not in seen:
            seen.add(tok.lower())
            parts.append(tok)
    return parts


def _rollen_aus_daten(roh) -> List[int]:
    """Rollen-IDs aus dem, was das Dashboard schickt (Liste oder Text).

    Discord-IDs kommen als Zeichenketten herein – als JavaScript-Zahl waeren
    die letzten Stellen schon verloren. Unbrauchbares wird uebergangen, eine
    leere Liste bedeutet ausdruecklich „keine Beschraenkung".
    """
    if roh is None:
        return []
    toks = roh if isinstance(roh, list) else re.split(r"[,;\s]+", str(roh))
    out: List[int] = []
    for t in toks:
        try:
            rid = int(str(t).strip())
        except (TypeError, ValueError):
            continue
        if rid > 0 and rid not in out:
            out.append(rid)
    return out


async def create_item(request: web.Request) -> web.Response:
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    katalog = conn.catalog
    data = await body(request)
    parts = _split_classnames(data.get("classnames") or data.get("classname"))
    if not parts:
        return err("Mindestens einen Classname angeben (z. B. M4A1 oder "
                   "M4A1, Mag_STANAG_60Rnd für ein Bundle).")
    is_bundle = len(parts) > 1
    display = str(data.get("name", "")).strip() or (
        f"{parts[0]} Bundle ({len(parts)} items)" if is_bundle else parts[0])
    if katalog.find(display):
        return err(f"'{display}' existiert bereits im Katalog. Anderen Namen wählen "
                   f"oder den Eintrag zuerst löschen.")

    try:
        price = int(data.get("price"))
    except (TypeError, ValueError):
        # Kategorie-Standardpreis als Fallback
        cat_prices = conn.get("shop_category_prices") or {}
        price = int(cat_prices.get(str(data.get("category", "")).strip(),
                                   conn.get("shop_default_price", 100) or 100))
    if price < 0:
        return err("Preis darf nicht negativ sein.")

    cat = str(data.get("category", "")).strip() or ("Bundles" if is_bundle else "Custom")
    try:
        mx = int(data.get("max_amount_per_buy") or data.get("limit") or (1 if is_bundle else 5))
    except (TypeError, ValueError):
        mx = 1 if is_bundle else 5
    mx = max(1, mx)

    it = {
        "name": display[:100],
        "price": price,
        "category": cat,
        "enabled": bool(data.get("enabled", True)),
        "max_amount_per_buy": mx,
        "custom": True,
    }
    rollen = _rollen_aus_daten(data.get("role_ids"))
    if rollen:
        it["role_ids"] = rollen
    if is_bundle:
        it["classnames"] = parts
    else:
        it["classname"] = parts[0]

    # Unbekannte Classnames (Tippfehler-Hinweis, nicht blockierend)
    unknown = [c for c in parts if katalog.find(c) is None]

    katalog.items.append(it)
    saved = _shop_persist(katalog)
    # Wieder angelegt – also von der Streichliste nehmen, sonst faellt es beim
    # naechsten "Items vom Server laden" erneut heraus.
    _vergiss_geloescht(conn, it["name"], *parts)
    # neue Kategorie ggf. als custom merken
    if cat not in (getattr(katalog, "by_category", {}) or {}):
        _remember_category(conn, cat)
    return ok({"item": _item_view(it), "saved": saved, "unknown_classnames": unknown})


async def update_item(request: web.Request) -> web.Response:
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    katalog = conn.catalog
    it = katalog.find(request.match_info["name"])
    if not it:
        return err("Item nicht gefunden.", 404)
    data = await body(request)

    if "name" in data:
        new_name = str(data["name"]).strip()
        if new_name and new_name.lower() != str(it.get("name", "")).lower():
            if katalog.find(new_name):
                return err(f"'{new_name}' existiert bereits.")
            it["name"] = new_name[:100]
    if "price" in data:
        try:
            it["price"] = max(0, int(data["price"]))
        except (TypeError, ValueError):
            return err("Preis muss eine Zahl sein.")
    if "category" in data and str(data["category"]).strip():
        it["category"] = str(data["category"]).strip()
        _remember_category(conn, it["category"])
    if "enabled" in data:
        it["enabled"] = bool(data["enabled"])
    if "role_ids" in data:
        # Leere Liste = Beschraenkung aufheben. Der Schluessel verschwindet dann
        # ganz, damit ein Katalog ohne Beschraenkungen sauber bleibt.
        rollen = _rollen_aus_daten(data.get("role_ids"))
        if rollen:
            it["role_ids"] = rollen
        else:
            it.pop("role_ids", None)
    if "max_amount_per_buy" in data or "limit" in data:
        try:
            it["max_amount_per_buy"] = max(1, int(data.get("max_amount_per_buy") or data.get("limit")))
        except (TypeError, ValueError):
            return err("Limit muss eine Zahl ≥ 1 sein.")
    if "classnames" in data or "classname" in data:
        parts = _split_classnames(data.get("classnames") or data.get("classname"))
        if not parts:
            return err("Mindestens einen Classname angeben.")
        it.pop("classname", None)
        it.pop("classnames", None)
        if len(parts) > 1:
            it["classnames"] = parts
        else:
            it["classname"] = parts[0]

    saved = _shop_persist(katalog)
    return ok({"item": _item_view(it), "saved": saved})


async def delete_item(request: web.Request) -> web.Response:
    """Ein Item aus dem Katalog dieses Servers entfernen – auch ein erzeugtes.

    Der Name wandert auf die Streichliste (``_merke_geloescht``), damit
    „Items vom Server laden" ihn nicht aus der types.xml zurueckholt.
    """
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    katalog = conn.catalog
    it = katalog.find(request.match_info["name"])
    if not it:
        return err("Item nicht gefunden.", 404)
    try:
        katalog.items.remove(it)
    except ValueError:
        pass
    _merke_geloescht(conn, it)
    saved = _shop_persist(katalog)
    return ok({"removed": str(it.get("name")), "saved": saved})


def _eigene_kategorien(conn: "ServerConnection") -> List[str]:
    """Selbst angelegte Shop-Kategorien **dieses** Servers.

    Der Hauptserver erbt einmalig die Liste aus der config.json – neue Kunden
    starten mit einer leeren Liste statt mit den Kategorien des Betreibers.
    """
    lst = conn.data.get("shop_categories_custom")
    if isinstance(lst, list):
        return lst
    seed = (list(cfg.config.get("shop_categories_custom", []) or [])
            if connections.primary() is conn else [])
    conn.data["shop_categories_custom"] = seed
    return seed


def _remember_category(conn: "ServerConnection", cat: str) -> None:
    lst = _eigene_kategorien(conn)
    if cat and cat not in lst:
        lst.append(cat)
        connections.save()


def _geloeschte_items(conn: "ServerConnection") -> List[str]:
    """Was auf **diesem** Server bewusst aus dem Katalog entfernt wurde.

    „Items vom Server laden" baut den Katalog vollstaendig aus der types.xml
    neu. Ohne diese Merkliste kaeme jedes geloeschte Item beim naechsten Laden
    zurueck und der aufgeraeumte Katalog waere wieder voll.

    Bewusst ueber ``conn.data`` statt ``conn.get`` – eine Rueckfallebene wuerde
    einem neuen Kunden die Streichliste des Betreibers vererben.
    """
    lst = conn.data.get("shop_geloescht")
    if not isinstance(lst, list):
        lst = []
        conn.data["shop_geloescht"] = lst
    return lst


def _merke_geloescht(conn: "ServerConnection", it: dict) -> None:
    """Name **und** Classname vormerken – die types.xml haengt bei doppelten
    Anzeigenamen den Classname an, dann passt der reine Name nicht mehr."""
    lst = _geloeschte_items(conn)
    schluessel = [str(it.get("name") or "")] + _classnames(it)
    neu = False
    for s in schluessel:
        s = s.strip().lower()
        if s and s not in lst:
            lst.append(s)
            neu = True
    if neu:
        connections.save()


def _vergiss_geloescht(conn: "ServerConnection", *namen: str) -> None:
    """Wieder angelegt oder importiert – dann gehoert es nicht mehr auf die
    Streichliste, sonst verschwaende es beim naechsten Laden erneut."""
    lst = _geloeschte_items(conn)
    weg = {str(n).strip().lower() for n in namen if str(n).strip()}
    rest = [n for n in lst if str(n).strip().lower() not in weg]
    if len(rest) != len(lst):
        conn.data["shop_geloescht"] = rest
        connections.save()


async def add_category(request: web.Request) -> web.Response:
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    data = await body(request)
    cat = str(data.get("name", "")).strip()
    if not cat:
        return err("Kategoriename fehlt.")
    if len(cat) > 60:
        return err("Kategoriename ist zu lang.")
    _remember_category(conn, cat)
    return ok({"category": cat})


async def api_shop_refresh_types(request: web.Request) -> web.Response:
    """Holt die types.xml vom eigenen Nitrado-Server und baut den Katalog neu."""
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    n, meldung = await katalog_von_server_holen(conn)
    if n is None:
        return err(meldung)
    return ok({"items": n, "message": meldung,
               "source": conn.catalog.source, "server": conn.name})


# Kennung in der Exportdatei – daran erkennt der Import, dass die Datei aus
# einem Dashboard stammt und nicht irgendein JSON ist.
_SHOP_EXPORT_FORMAT = "dayz-dashboard-shop"
_SHOP_IMPORT_MAX = 5000


async def api_shop_export(request: web.Request) -> web.Response:
    """Den kompletten Katalog dieses Servers zum Weitergeben.

    Bewusst OHNE ``service_id``, Nitrado-Token und Guild-ID: die Datei ist zum
    Teilen mit Fremden gedacht. Der Servername bleibt als Herkunftsangabe drin –
    der steht ohnehin in jeder DayZ-Serverliste.

    Immer der ganze Katalog, unabhaengig von Suche und Kategoriefilter der
    Oberflaeche – eine halbe Preisliste weiterzugeben waere die schlechtere
    Ueberraschung.
    """
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    # Rollen-Beschraenkungen bleiben draussen: Rollen-IDs gelten nur in EINER
    # Discord-Guild. Beim Empfaenger wuerden sie auf nichts zeigen und das Item
    # still unverkaeuflich machen.
    items = [{k: v for k, v in _item_view(it).items() if k != "role_ids"}
             for it in conn.catalog.items]
    return ok({
        "format": _SHOP_EXPORT_FORMAT,
        "version": 1,
        "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "server": conn.name,
        "count": len(items),
        "items": items,
    })


async def api_shop_import(request: web.Request) -> web.Response:
    """Items aus einer fremden Exportdatei in den eigenen Katalog uebernehmen.

    Nimmt sowohl die Export-Huelle als auch eine blosse ``{"items": [...]}``-Datei –
    damit laesst sich auch eine rohe ``shop_items_<id>.json`` einlesen.

    **Vorhandene Items bleiben unangetastet.** Ein Name, den es schon gibt, wird
    uebersprungen und gemeldet; dadurch ist derselbe Import gefahrlos
    wiederholbar. Ueberschrieben wird nie.
    """
    conn, fehler = _session_conn(request)
    if fehler is not None:
        return fehler
    katalog = conn.catalog
    data = await body(request)
    roh = data.get("items")
    if not isinstance(roh, list):
        return err("Die Datei enthält keine Item-Liste. Erwartet wird eine "
                   "Exportdatei aus dem Shop oder eine shop_items-Datei.")
    if len(roh) > _SHOP_IMPORT_MAX:
        return err(f"Die Datei enthält {len(roh)} Einträge – erlaubt sind "
                   f"höchstens {_SHOP_IMPORT_MAX}.")

    standard = int(conn.get("shop_default_price", 100) or 100)
    kategorie_preise = conn.get("shop_category_prices") or {}
    vorhandene_kategorien = set(getattr(katalog, "by_category", {}) or {})

    neu: List[Dict] = []
    uebersprungen: List[str] = []
    ungueltig = 0
    gesehen = set()          # Doppelte INNERHALB der Datei – der Index waechst
                             # erst beim Speichern mit.

    for eintrag in roh:
        if not isinstance(eintrag, dict):
            ungueltig += 1
            continue
        parts = _split_classnames(eintrag.get("classnames") or eintrag.get("classname"))
        if not parts:
            ungueltig += 1
            continue
        is_bundle = len(parts) > 1
        name = (str(eintrag.get("name") or "").strip()
                or (f"{parts[0]} Bundle ({len(parts)} items)" if is_bundle else parts[0]))
        name = name[:100]
        if name.lower() in gesehen or katalog.find(name) is not None:
            uebersprungen.append(name)
            continue

        cat = str(eintrag.get("category", "")).strip() or ("Bundles" if is_bundle else "Custom")
        try:
            preis = int(eintrag.get("price"))
        except (TypeError, ValueError):
            preis = int(kategorie_preise.get(cat, standard))
        if preis < 0:
            ungueltig += 1
            continue
        try:
            mx = int(eintrag.get("max_amount_per_buy") or eintrag.get("limit")
                     or (1 if is_bundle else 5))
        except (TypeError, ValueError):
            mx = 1 if is_bundle else 5

        it: Dict[str, Any] = {
            "name": name,
            "price": preis,
            "category": cat,
            "enabled": bool(eintrag.get("enabled", True)),
            "max_amount_per_buy": max(1, mx),
            # Importiertes ist loeschbar – ein versehentlicher Import laesst sich
            # ueber den Muelleimer wieder loswerden.
            "custom": True,
            # role_ids werden BEWUSST nicht uebernommen: sie gelten nur in der
            # Guild des Absenders und wuerden hier auf nichts zeigen.
        }
        if is_bundle:
            it["classnames"] = parts
        else:
            it["classname"] = parts[0]
        gesehen.add(name.lower())
        neu.append(it)

    saved = True
    if neu:
        katalog.items.extend(neu)
        saved = _shop_persist(katalog)
        # Importiertes gehoert nicht mehr auf die Streichliste.
        _vergiss_geloescht(conn, *[str(it["name"]) for it in neu],
                           *[c for it in neu for c in _classnames(it)])
        for cat in sorted({str(it["category"]) for it in neu} - vorhandene_kategorien):
            _remember_category(conn, cat)

    teile = [f"{len(neu)} hinzugefügt"]
    if uebersprungen:
        teile.append(f"{len(uebersprungen)} übersprungen (schon vorhanden)")
    if ungueltig:
        teile.append(f"{ungueltig} unbrauchbar")
    return ok({"hinzugefuegt": len(neu), "uebersprungen": uebersprungen,
               "ungueltig": ungueltig, "saved": saved,
               "quelle": str(data.get("server") or "") or None,
               "message": ", ".join(teile) + "."})


# ──────────────────────────────────────────────────────────────────────────
#  Karte & Events: Kartendaten, Live-Spielerpositionen, letzte Events.
# ──────────────────────────────────────────────────────────────────────────
# Welt-Kantenlänge je Karte in Metern (für die Leaflet-CRS-Skalierung).
# Überschreibbar via config["dashboard_map_sizes"].
DEFAULT_MAP_SIZES = {
    "ChernarusPlus": 15360,
    "Livonia": 12800,
    "Sakhal": 15360,
}

# Kanonischer Map-Name → Ordner der öffentlichen Kachelquelle (xam.nu).
# Die Kacheln lädt der Browser direkt – unabhängig von der Host-Netzpolicy.
_XAM_FOLDER = {
    "ChernarusPlus": "chernarusplus",
    "Livonia": "livonia",
    "Sakhal": "sakhal",
}
_XAM_TEMPLATE = "https://static.xam.nu/dayz/maps/{folder}/1.27/topographic/{{z}}/{{x}}/{{y}}.webp"
TILE_MAX_NATIVE_ZOOM = 7

_MAP_POS_RE = re.compile(r"(-?\d+(?:\.\d+)?)")


def _world_size(map_name: str) -> int:
    sizes = dict(DEFAULT_MAP_SIZES)
    sizes.update(cfg.config.get("dashboard_map_sizes", {}) or {})
    return int(sizes.get(map_name, 15360))


def _tile_url(map_name: str) -> str:
    """Kachel-URL je Karte: config-Override → xam.nu-Default → '' (dann Fallback)."""
    override = (cfg.config.get("dashboard_map_tiles") or {}).get(map_name)
    if override:
        return str(override)
    folder = _XAM_FOLDER.get(map_name)
    return _XAM_TEMPLATE.format(folder=folder) if folder else ""


# Gebündelte, exakte Ortslisten (dashboard/static/locations/<Karte>.json),
# generiert aus den Kartendaten von dayz.xam.nu – einmal geladen, dann gecacht.
_locations_cache: dict = {}


def _locations(map_name: str):
    if map_name in _locations_cache:
        return _locations_cache[map_name]
    out = []
    try:
        raw = _read_asset(f"locations/{map_name}.json")
        data = json.loads(raw.decode("utf-8")) if raw else {}
        out = [{"name": l["name"], "x": l["x"], "z": l["z"], "t": l.get("t", "local")}
               for l in data.get("locations", [])
               if l.get("name") and isinstance(l.get("x"), (int, float))]
    except Exception:
        out = []
    if not out:
        # Fallback: grobe Ortsliste aus dem Bot (_MAP_LOCATIONS)
        locs = _MAP_LOCATIONS or {}
        data = locs.get(map_name) or locs.get("ChernarusPlus") or []
        out = [{"name": n, "x": x, "z": z, "t": "city"} for (n, x, z) in data]
    _locations_cache[map_name] = out
    return out


async def api_map_meta(request: web.Request) -> web.Response:
    _c, denied = _session_conn(request)
    if denied is not None:
        return denied
    map_name = _c.get("map_name", "ChernarusPlus")
    return ok({
        "map_name": map_name,
        "world_size": _world_size(map_name),
        "tile_url": _tile_url(map_name),      # XYZ-Template ({z}/{x}/{y}), Browser lädt es
        "tile_max_native_zoom": TILE_MAX_NATIVE_ZOOM,
        "image": f"/maps/{map_name}.jpg",     # optionales eigenes Bild (ImageOverlay), falls vorhanden
        "locations": _locations(map_name),
        "izurvive": f"https://www.izurvive.com/?m={map_name}",
    })


async def api_map_players(request: web.Request) -> web.Response:
    # Positionen NUR des eigenen Servers – sonst saehe jeder Kunde auf der
    # Karte die Live-Positionen der Spieler aller anderen.
    _c, denied = _session_conn(request)
    if denied is not None:
        return denied
    parser = _c.parser
    positions = getattr(parser, "player_positions", {}) if parser else {}
    nearest_fn = _nearest_location
    map_name = (_c.get("map_name", "ChernarusPlus") if _c
                else cfg.config.get("map_name", "ChernarusPlus"))
    out = []
    for name, entry in list(positions.items()):
        if not isinstance(entry, dict):
            continue
        nums = _MAP_POS_RE.findall(str(entry.get("position", "")))
        if len(nums) < 2:
            continue
        try:
            x, z = round(float(nums[0]), 1), round(float(nums[1]), 1)
        except ValueError:
            continue
        near = None
        if callable(nearest_fn):
            try:
                near = nearest_fn(x, z, map_name)
            except Exception:
                near = None
        out.append({"name": name, "x": x, "z": z,
                    "last_seen": entry.get("last_seen"), "near": near})
    return ok({"players": out, "map_name": map_name})


async def api_events(request: web.Request) -> web.Response:
    try:
        since = int(request.query.get("since", 0))
    except ValueError:
        since = 0
    types = request.query.get("types")
    tlist = [t for t in types.split(",") if t] if types else None
    conn, denied = _session_conn(request)
    if denied is not None:
        return denied
    snap = _ev_snapshot(since_id=since, types=tlist, service_id=conn.service_id)
    return ok(snap)


async def api_event_types(request: web.Request) -> web.Response:
    return ok({"types": _ev_types_meta()})


# ──────────────────────────────────────────────────────────────────────────
#  Bans & Whitelist über die Nitrado-Gameserver-Settings (wie im Web-Interface).
#
#  Nutzt die vorhandenen async-Helfer ``_read_banlist``/``_write_banlist`` bzw.
#  ``_read_whitelist``/``_write_whitelist`` des Bots.
# ──────────────────────────────────────────────────────────────────────────
async def _read(kind: str, conn: ServerConnection):
    fn = globals().get(f"_read_{kind}")
    if not callable(fn):
        return None
    return await fn(conn)  # (names, category, key)


async def _write(kind: str, conn: ServerConnection, names, category, key):
    fn = globals().get(f"_write_{kind}")
    if not callable(fn):
        return False, "Funktion nicht verfügbar."
    return await fn(conn, names, category, key)


def _make_get(kind: str):
    async def handler(request: web.Request) -> web.Response:
        conn, denied = _session_conn(request)
        if denied is not None:
            return denied
        try:
            names, cat, key = await _read(kind, conn)
        except Exception as e:  # noqa: BLE001
            return err(f"Nitrado nicht erreichbar: {e}", 502)
        return ok({"names": names, "category": cat, "key": key})
    return handler


def _make_add(kind: str):
    async def handler(request: web.Request) -> web.Response:
        data = await body(request)
        player = str(data.get("player", "")).strip()
        if not player:
            return err("Spielername fehlt.")
        conn, denied = _session_conn(request)
        if denied is not None:
            return denied
        try:
            names, cat, key = await _read(kind, conn)
        except Exception as e:  # noqa: BLE001
            return err(f"Nitrado nicht erreichbar: {e}", 502)
        if player.lower() not in [n.lower() for n in names]:
            names.append(player)
            good, msg = await _write(kind, conn, names, cat, key)
            if not good:
                return err(msg or "Speichern fehlgeschlagen.", 502)
        return ok({"names": names})
    return handler


def _make_remove(kind: str):
    async def handler(request: web.Request) -> web.Response:
        player = request.match_info["player"]
        conn, denied = _session_conn(request)
        if denied is not None:
            return denied
        try:
            names, cat, key = await _read(kind, conn)
        except Exception as e:  # noqa: BLE001
            return err(f"Nitrado nicht erreichbar: {e}", 502)
        new = [n for n in names if n.lower() != player.lower()]
        if len(new) != len(names):
            good, msg = await _write(kind, conn, new, cat, key)
            if not good:
                return err(msg or "Speichern fehlgeschlagen.", 502)
        return ok({"names": new})
    return handler


get_bans = _make_get("banlist")
add_ban = _make_add("banlist")
remove_ban = _make_remove("banlist")
get_whitelist = _make_get("whitelist")
add_whitelist = _make_add("whitelist")
remove_whitelist = _make_remove("whitelist")


# ──────────────────────────────────────────────────────────────────────────
#  Economy: Guthaben ansehen & anpassen (nutzt economy.db + EconomyDB).
# ──────────────────────────────────────────────────────────────────────────
def _db_path() -> str:
    return str(cfg.config.get("economy_db_path", "db"))


def _read_balances(guild_id: int, limit: int = 200):
    if not os.path.exists(_db_path()):
        return []
    con = sqlite3.connect(_db_path())
    con.row_factory = sqlite3.Row
    try:
        # Existiert die Tabelle noch nicht (Bot hat die Economy noch nie berührt),
        # liefern wir eine leere Liste statt eines Fehlers.
        have = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='balances'").fetchone()
        if not have:
            return []
        rows = con.execute(
            """SELECT b.user_id, b.wallet, b.bank,
                      (SELECT ingame_name FROM links l
                       WHERE l.guild_id=b.guild_id AND l.user_id=b.user_id) AS ingame
               FROM balances b WHERE b.guild_id=?
               ORDER BY (b.wallet + b.bank) DESC LIMIT ?""",
            (guild_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


async def api_economy_balances(request: web.Request) -> web.Response:
    erlaubt = _session_guilds(request)
    try:
        gid = int(request.query.get("guild_id") or (erlaubt or [0])[0])
    except (TypeError, ValueError, IndexError):
        return err("Keine Guild angegeben.")
    # Ohne diese Prüfung koennte man jede fremde Guild abfragen, indem man
    # ihre ID einfach mitschickt.
    if gid not in erlaubt:
        return err("Dieser Discord-Server gehört nicht zu deinem Nitrado-Server.", 403)
    try:
        rows = await _dash_run(_read_balances, gid)
    except Exception as e:  # noqa: BLE001
        return err(f"db nicht lesbar: {e}", 500)
    _conn_g = connections.for_guild(gid)
    return ok({
        "guild_id": str(gid),
        "balances": [{"user_id": str(r["user_id"]), "ingame": r["ingame"],
                      "wallet": r["wallet"], "bank": r["bank"]} for r in rows],
        "currency": _conn_g.get("currency_name", "Rubles") if _conn_g else
                    cfg.config.get("currency_name", "Rubles"),
        "symbol": _conn_g.get("currency_symbol", "₽") if _conn_g else
                  cfg.config.get("currency_symbol", "₽"),
    })


async def api_economy_money(request: web.Request) -> web.Response:
    data = await body(request)
    try:
        gid = int(data["guild_id"]); uid = int(data["user_id"])
        amount = int(data["amount"])
    except (KeyError, TypeError, ValueError):
        return err("guild_id, user_id und amount (Zahl) erforderlich.")
    if gid not in _session_guilds(request):
        return err("Dieser Discord-Server gehört nicht zu deinem Nitrado-Server.", 403)
    op = str(data.get("op", "add"))
    try:
        if op == "add":
            wallet, bank = await _dash_run(db.add_wallet, gid, uid, amount)
        elif op == "remove":
            wallet, bank = await _dash_run(db.add_wallet, gid, uid, -amount)
        elif op == "set":
            wallet, bank = await _dash_run(db.set_wallet, gid, uid, amount)
        else:
            return err("op muss add, remove oder set sein.")
    except Exception as e:  # noqa: BLE001
        return err(f"Fehler: {e}", 500)
    return ok({"user_id": str(uid), "wallet": wallet, "bank": bank})


async def api_economy_get_config(request: web.Request) -> web.Response:
    conn, denied = _session_conn(request)
    if denied is not None:
        return denied
    return ok({
        "currency_name": conn.get("currency_name"),
        "currency_symbol": conn.get("currency_symbol"),
        "starting_balance": conn.get("starting_balance"),
        "economy": conn.get("economy"),
        "kill_reward": conn.get("kill_reward"),
    })


async def api_economy_set_config(request: web.Request) -> web.Response:
    conn, denied = _session_conn(request)
    if denied is not None:
        return denied
    data = await body(request)
    for key in ("currency_name", "currency_symbol"):
        if key in data:
            _conn_store(conn, key, str(data[key]))
    for key in ("starting_balance", "kill_reward"):
        if key in data:
            try:
                _conn_store(conn, key, int(data[key]))
            except (TypeError, ValueError):
                return err(f"{key} muss eine Zahl sein.")
    return ok()


# ──────────────────────────────────────────────────────────────────────────
#  Wiederkehrende Ankündigungen verwalten (announcements.json).
#
#  Schema je Eintrag: ``{day, time, message, channel_id, repeat, last_sent}``.
#  ``day`` = monday…sunday, ``time`` = HH:MM, ``repeat`` = weekly|biweekly|triweekly|monthly.
# ──────────────────────────────────────────────────────────────────────────
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_REPEATS = ("weekly", "biweekly", "triweekly", "monthly")
_DASH_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _data() -> dict:
    d = ann_data
    if not isinstance(d, dict):
        return {"announcements": []}
    d.setdefault("announcements", [])
    return d


def _ann_visible(request: web.Request) -> Tuple[List[dict], Optional[web.Response]]:
    """Ankündigungen des eigenen Servers.

    Alte Einträge ohne service_id gehören dem Hauptserver – so bleiben
    bestehende Ankündigungen sichtbar, ohne bei Kunden aufzutauchen.
    """
    conn, denied = _session_conn(request)
    if denied is not None:
        return [], denied
    primary = connections.primary()
    eigen = []
    for i, a in enumerate(_data()["announcements"]):
        sid = str(a.get("service_id") or "")
        if sid == conn.service_id or (not sid and primary is conn):
            eigen.append({"index": i, **a})
    return eigen, None


async def list_announcements(request: web.Request) -> web.Response:
    eigen, denied = _ann_visible(request)
    if denied is not None:
        return denied
    return ok({"announcements": eigen})


async def create_announcement(request: web.Request) -> web.Response:
    conn, denied = _session_conn(request)
    if denied is not None:
        return denied
    d = _data()
    data = await body(request)
    day = str(data.get("day", "")).strip().lower()
    time_ = str(data.get("time", "")).strip()
    message = str(data.get("message", "")).strip()
    repeat = str(data.get("repeat", "weekly")).strip().lower()
    channel_id = data.get("channel_id")

    if day not in _DAYS:
        return err("day muss einer von monday…sunday sein.")
    if not _DASH_TIME_RE.match(time_):
        return err("time muss HH:MM sein.")
    if not message:
        return err("Nachricht fehlt.")
    if repeat not in _REPEATS:
        return err("repeat muss weekly, biweekly, triweekly oder monthly sein.")
    if not channel_id:
        return err("channel_id fehlt.")
    try:
        kanal = int(channel_id)
    except (TypeError, ValueError):
        return err("Ungültige Channel-ID.")
    # Der Zielchannel muss in der Guild DIESES Servers liegen. Ohne die
    # Pruefung liess sich mit einer fremden Channel-ID eine wiederkehrende
    # Ankuendigung im Discord eines anderen Kunden einplanen.
    if conn.guild_id is None:
        return err("Für diesen Server ist noch kein Discord-Server zugeordnet – "
                   "der Bot-Betreiber schaltet ihn frei.", 409)
    fehler = _kanal_gehoert_guild(int(conn.guild_id), kanal)
    if fehler is not None:
        return fehler

    ann = {"day": day, "time": time_, "message": message,
           "channel_id": kanal, "repeat": repeat, "last_sent": None,
           "service_id": conn.service_id}
    d["announcements"].append(ann)
    save = save_announcements
    if callable(save):
        save()
    return ok({"index": len(d["announcements"]) - 1, **ann})


async def delete_announcement(request: web.Request) -> web.Response:
    eigen, denied = _ann_visible(request)
    if denied is not None:
        return denied
    d = _data()
    try:
        idx = int(request.match_info["index"])
    except ValueError:
        return err("Ungültiger Index.")
    if not 0 <= idx < len(d["announcements"]):
        return err("Ankündigung nicht gefunden.", 404)
    if idx not in [a["index"] for a in eigen]:
        return err("Diese Ankündigung gehört einem anderen Server.", 403)
    removed = d["announcements"].pop(idx)
    save = save_announcements
    if callable(save):
        save()
    return ok({"removed": removed})


# ──────────────────────────────────────────────────────────────────────────
#  Server-Steuerung: Status, Neustart, Stopp (reuse NitradoAPI + A2S).
# ──────────────────────────────────────────────────────────────────────────
async def api_server_status(request: web.Request) -> web.Response:
    a2s = a2s_query
    conn, denied = _session_conn(request)
    if denied is not None:
        return denied
    ip = str(conn.get("server_ip") or "")
    port = int(conn.get("query_port", 2302) or 2302)
    live = None
    if callable(a2s) and ip:
        try:
            live = await _dash_run(lambda: a2s(ip, port))
        except Exception:
            live = None
    nit_info = None
    nit = conn.api
    if nit and str(getattr(nit, "service_id", "") or "").strip():
        try:
            info = await nit.get_info()
            if info:
                q = info.get("query") or {}
                nit_info = {
                    "state": info.get("status") or info.get("state"),
                    "players": q.get("player_current"),
                    "max_players": q.get("player_max"),
                    "map": q.get("map"),
                }
        except Exception:
            nit_info = None
    return ok({
        "online": bool(live),
        "a2s": live,
        "nitrado": nit_info,
        "map_name": conn.get("map_name"),
        "server_ip": ip or None,
    })


async def api_server_restart(request: web.Request) -> web.Response:
    nit, e = require_nitrado(request)
    if e:
        return e
    okflag, msg = await nit.restart()
    return (ok({"message": msg}) if okflag else err(msg or "Neustart fehlgeschlagen.", 502))


async def api_server_stop(request: web.Request) -> web.Response:
    nit, e = require_nitrado(request)
    if e:
        return e
    okflag, msg = await nit.stop()
    return (ok({"message": msg}) if okflag else err(msg or "Stopp fehlgeschlagen.", 502))


# ──────────────────────────────────────────────────────────────────────────
#  aiohttp-Web-Server des Dashboards – läuft im Event-Loop des Bots.
# ──────────────────────────────────────────────────────────────────────────
dash_log = logging.getLogger("dashboard")

_dash_runner: Optional[web.AppRunner] = None
# Nur gesetzt, wenn der Port zusätzlich HTTPS annimmt (siehe _DualProtocolSite).
_dash_site: Optional[Any] = None


async def _dash_index(request: web.Request) -> web.Response:
    return _asset_response("index.html",
                           fallback_text="Dashboard-Frontend fehlt (index.html).",
                           request=request)


async def _dash_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "ready": _DASH_BOUND})


async def _dash_maps(request: web.Request) -> web.Response:
    """Optionale Kartenbilder aus static/maps/<Name>.jpg ausliefern (404 wenn keins)."""
    name = os.path.basename(request.match_info["name"])
    path = os.path.join(_DASH_STATIC, "maps", name)
    if os.path.exists(path):
        return web.FileResponse(path)
    return web.Response(status=404)


async def _dash_static(request: web.Request) -> web.Response:
    """``/static/…`` und ``/vendor/…`` ausliefern – Platte zuerst, dann Speicher.

    Bewusst ein eigener Handler statt ``add_static``: liegen die Dateien nicht
    auf der Platte (schreibgeschütztes Verzeichnis), werden sie direkt aus den
    eingebetteten Assets bedient. Das Dashboard läuft damit in jedem Fall.
    """
    tail = request.match_info.get("tail", "")
    prefix = "vendor/" if request.path.startswith("/vendor/") else ""
    return _asset_response(prefix + tail, request=request)


def build_app() -> web.Application:
    # 8 MB statt aiohttps Vorgabe von 1 MB: ein vollstaendiger Katalog aus der
    # types.xml (~1700 Items) liegt als eingerueckte Importdatei darueber. Ohne
    # die Anhebung verschluckt ``body()`` den 413 zu einem leeren Dict und der
    # Import meldete faelschlich "keine Item-Liste".
    app = web.Application(middlewares=[_dash_auth_middleware],
                          client_max_size=8 * 1024 * 1024)
    r = app.router

    # ── Seite & Statisches ──
    r.add_get("/", _dash_index)
    r.add_get("/index.html", _dash_index)
    r.add_get("/api/health", _dash_health)
    r.add_get("/maps/{name}", _dash_maps)
    r.add_get("/static/{tail:.*}", _dash_static)
    r.add_get("/vendor/{tail:.*}", _dash_static)

    # ── Auth / Session ──
    r.add_post("/api/auth/token", post_token)
    r.add_post("/api/auth/select-server", post_select_server)
    r.add_get("/api/servers/mine", api_servers_mine)
    r.add_post("/api/servers/select", post_servers_select)
    r.add_get("/api/my-guilds", api_my_guilds)
    r.add_post("/api/my-guilds/select", post_my_guilds_select)
    r.add_post("/api/auth/guild", post_setup_guild)
    r.add_get("/api/auth/discord/start", api_discord_start)
    r.add_get("/api/auth/discord/callback", api_discord_callback)

    # ── Optionen (jeder angemeldete Nutzer) ──
    r.add_get("/api/options", api_options)
    r.add_get("/api/options/token", api_options_token_reveal)
    r.add_post("/api/options/token", post_options_token)

    # ── Nur mit Dashboard-Admin-Rolle ──
    r.add_get("/api/audit", api_audit)
    r.add_get("/api/admin/guilds", api_admin_guilds)
    r.add_get("/api/admin/backup", api_admin_backup)
    r.add_get("/api/admin/servers", api_admin_servers)
    r.add_post("/api/admin/servers/{service_id}/guild", post_admin_server_guild)
    r.add_delete("/api/admin/servers/{service_id}", delete_admin_server)
    r.add_post("/api/auth/logout", post_logout)
    r.add_get("/api/session", api_get_session)

    # ── Feeds ──
    r.add_get("/api/feeds", get_feeds)
    r.add_post("/api/feeds/{guild_id}/{log_type}", set_feed)

    # ── Diagnose ──
    r.add_get("/api/diagnose", api_diagnose)
    r.add_post("/api/diagnose/nachlesen", api_diagnose_nachlesen)

    # ── Zones ──
    r.add_get("/api/zones", list_zones)
    r.add_post("/api/zones", create_zone)
    r.add_put("/api/zones/{name}", update_zone)
    r.add_delete("/api/zones/{name}", delete_zone)
    r.add_get("/api/zones/{name}/allowlist", get_allowlist)
    r.add_post("/api/zones/{name}/allowlist", add_allowlist)
    r.add_delete("/api/zones/{name}/allowlist/{player}", remove_allowlist)
    r.add_get("/api/guild/{guild_id}/roles", guild_roles)
    r.add_get("/api/guild/{guild_id}/channels", guild_channels)
    r.add_get("/api/guild/{guild_id}/seen-players", guild_seen_players)

    # ── Auto-Aufgaben ──
    r.add_get("/api/auto-restart", get_auto_restart)
    r.add_post("/api/auto-restart", set_auto_restart)

    # ── Shop ──
    r.add_get("/api/shop/items", list_items)
    r.add_get("/api/shop/categories", api_shop_categories)
    r.add_get("/api/shop/classnames", api_shop_classnames)
    r.add_post("/api/shop/items", create_item)
    r.add_put("/api/shop/items/{name}", update_item)
    r.add_delete("/api/shop/items/{name}", delete_item)
    r.add_post("/api/shop/categories", add_category)
    r.add_post("/api/shop/refresh-types", api_shop_refresh_types)
    r.add_get("/api/shop/export", api_shop_export)
    r.add_post("/api/shop/import", api_shop_import)

    # ── Karte / Events ──
    r.add_get("/api/map/meta", api_map_meta)
    r.add_get("/api/map/players", api_map_players)
    r.add_get("/api/events", api_events)
    r.add_get("/api/events/types", api_event_types)

    # ── Extras: Bans/Whitelist ──
    r.add_get("/api/bans", get_bans)
    r.add_post("/api/bans", add_ban)
    r.add_delete("/api/bans/{player}", remove_ban)
    r.add_get("/api/whitelist", get_whitelist)
    r.add_post("/api/whitelist", add_whitelist)
    r.add_delete("/api/whitelist/{player}", remove_whitelist)

    # ── Extras: Economy ──
    r.add_get("/api/economy/balances", api_economy_balances)
    r.add_post("/api/economy/money", api_economy_money)
    r.add_get("/api/economy/config", api_economy_get_config)
    r.add_post("/api/economy/config", api_economy_set_config)

    # ── Extras: Ankündigungen ──
    r.add_get("/api/announcements", list_announcements)
    r.add_post("/api/announcements", create_announcement)
    r.add_delete("/api/announcements/{index}", delete_announcement)

    # ── Extras: Server-Steuerung ──
    r.add_get("/api/server/status", api_server_status)
    r.add_post("/api/server/restart", api_server_restart)
    r.add_post("/api/server/stop", api_server_stop)

    return app


# ══════════════════════════════════════════════════════════════
#  HTTPS auf DEMSELBEN Port (Protokoll-Erkennung pro Verbindung)
# ══════════════════════════════════════════════════════════════
#  Browser stufen getippte Adressen zunehmend selbst auf https:// hoch. Trifft
#  so ein TLS-Handshake auf einen Server, der nur Klartext-HTTP spricht,
#  antwortet dieser mit HTTP-Text – der Browser kann das nicht als Zertifikat
#  lesen und bricht mit ERR_SSL_PROTOCOL_ERROR ab. Die Seite ist damit nicht
#  erreichbar, obwohl der Server läuft.
#
#  Ein zweiter Port ist keine Lösung: Hoster wie PebbleHost weisen genau einen
#  zu. Deshalb entscheidet der Server pro Verbindung. Das erste Byte eines
#  TLS-ClientHello ist immer 0x16 (Handshake-Record); alles andere ist eine
#  HTTP-Methode ("GET" = 0x47 …). Gelesen wird es mit MSG_PEEK, es bleibt also
#  im Puffer und ist danach noch Teil des Handshakes bzw. der HTTP-Anfrage.
#  Beide Fälle landen anschließend im selben aiohttp-Handler.

_TLS_HANDSHAKE_FIRST_BYTE = 0x16
_TLS_CERT_FILE = "dashboard_cert.pem"
_TLS_KEY_FILE  = "dashboard_key.pem"


def _tls_paths() -> Tuple[str, str]:
    """Zertifikat und Schlüssel liegen in dashboard_web/ – dort, wo schon die
    erzeugten Frontend-Dateien liegen. Das Verzeichnis steht in .gitignore und
    wird von /static/… nicht ausgeliefert (siehe _asset_path)."""
    return (os.path.join(_DASH_DIR, _TLS_CERT_FILE),
            os.path.join(_DASH_DIR, _TLS_KEY_FILE))


def _tls_hostnames() -> List[str]:
    """Namen/IPs, die ins Zertifikat gehören – die öffentliche Adresse zuerst."""
    names: List[str] = []

    def _add(raw: str) -> None:
        v = (raw or "").strip().rstrip("/")
        if "://" in v:
            v = v.split("://", 1)[1]
        v = v.split("/", 1)[0]
        if v.count(":") == 1:            # host:port – Port gehört nicht ins Zertifikat
            v = v.split(":", 1)[0]
        if v and v not in names:
            names.append(v)

    _add(os.environ.get("DASHBOARD_PUBLIC_HOST", ""))
    _add(str(cfg.config.get("dashboard_public_host") or ""))
    _add(str(cfg.config.get("server_ip") or ""))
    _add("localhost")
    _add("127.0.0.1")
    return names


def _cert_is_usable(cert_path: str, names: List[str]) -> bool:
    """True, wenn das vorhandene Zertifikat noch länger gültig ist und alle
    Namen abdeckt. Sonst wird es neu erzeugt (z. B. nach einem Hostwechsel)."""
    try:
        from cryptography import x509
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        expires = getattr(cert, "not_valid_after_utc", None)
        if expires is None:              # ältere cryptography-Versionen
            expires = cert.not_valid_after.replace(tzinfo=timezone.utc)
        if expires - datetime.now(timezone.utc) < timedelta(days=30):
            return False
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
        covered = {str(entry.value) for entry in san}
        return all(n in covered for n in names)
    except Exception:  # noqa: BLE001 – unlesbar/kaputt = neu erzeugen
        return False


def _create_selfsigned_cert(cert_path: str, key_path: str, names: List[str]) -> None:
    """Selbstsigniertes Zertifikat für die angegebenen Namen/IPs schreiben."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    alt_names: List[Any] = []
    for n in names:
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            alt_names.append(x509.DNSName(n))
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, names[0][:64]),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DayZ Dashboard"),
    ])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .sign(key, hashes.SHA256()))

    os.makedirs(_DASH_DIR, exist_ok=True)
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def _tls_context() -> Optional[ssl.SSLContext]:
    """SSL-Kontext fürs Dashboard – ``None`` heißt: nur HTTP wie früher.

    Schlägt irgendetwas fehl (Paket fehlt, Verzeichnis schreibgeschützt), ist
    das kein Fehler: das Dashboard läuft dann unverändert über http://.
    """
    if not cfg.config.get("dashboard_https", True):
        return None
    if not _x509_available():
        dash_log.info("[DASHBOARD] Paket 'cryptography' nicht verfügbar – HTTPS aus, "
                      "das Dashboard ist nur über http:// erreichbar.")
        return None
    cert_path, key_path = _tls_paths()
    names = _tls_hostnames()
    try:
        if not (os.path.exists(cert_path) and os.path.exists(key_path)
                and _cert_is_usable(cert_path, names)):
            _create_selfsigned_cert(cert_path, key_path, names)
            dash_log.info(f"[DASHBOARD] Selbstsigniertes Zertifikat erzeugt für: "
                          f"{', '.join(names)}")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # aiohttp spricht HTTP/1.1. Ohne diese Ansage bieten Browser zusätzlich
        # h2 (HTTP/2) an und müssten selbst zurückfallen – hier ist es eindeutig.
        try:
            ctx.set_alpn_protocols(["http/1.1"])
        except NotImplementedError:
            pass
        ctx.load_cert_chain(cert_path, key_path)
        return ctx
    except Exception as e:  # noqa: BLE001 – HTTPS ist eine Zugabe, kein Muss
        dash_log.warning(f"[DASHBOARD] HTTPS nicht einrichtbar ({e}) – nur http://.")
        return None


def _close_quietly(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


async def _peek_first_byte(sock: socket.socket) -> bytes:
    """Erstes Byte ansehen, ohne es aus dem Socket-Puffer zu nehmen (MSG_PEEK).

    Genau deshalb funktioniert die Weitergabe danach: TLS-Handshake bzw.
    HTTP-Anfrage sind noch vollständig vorhanden.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    fd = sock.fileno()

    def _ready() -> None:
        if fut.done():
            return
        try:
            data = sock.recv(1, socket.MSG_PEEK)
        except (BlockingIOError, InterruptedError):
            return                       # noch nichts da – weiter warten
        except OSError as e:
            loop.remove_reader(fd)
            fut.set_exception(e)
            return
        loop.remove_reader(fd)
        fut.set_result(data)

    loop.add_reader(fd, _ready)
    try:
        return await fut
    finally:
        try:
            loop.remove_reader(fd)
        except (OSError, ValueError, NotImplementedError):
            pass


def _loop_supports_peek() -> bool:
    """Die Protokoll-Erkennung braucht ``add_reader``. Das fehlt unter Windows
    im ProactorEventLoop – dort bleibt es beim bisherigen reinen HTTP."""
    loop = asyncio.get_running_loop()
    a, b = socket.socketpair()
    try:
        loop.add_reader(a.fileno(), lambda: None)
        loop.remove_reader(a.fileno())
        return True
    except NotImplementedError:
        return False
    except OSError:
        return False
    finally:
        a.close()
        b.close()


class _DualProtocolSite:
    """Ersetzt ``web.TCPSite`` und bedient HTTP und HTTPS auf einem Port.

    Die Verbindungen werden selbst angenommen und – je nach erstem Byte – mit
    oder ohne SSL-Kontext an denselben aiohttp-Handler übergeben
    (``runner.server`` ist genau die Protokoll-Fabrik, die auch ``TCPSite``
    benutzt). Es gibt also keinen zweiten Server und keinen Proxy davor.
    """

    def __init__(self, runner: web.AppRunner, ssl_ctx: Optional[ssl.SSLContext]):
        self._runner = runner
        self._ssl = ssl_ctx
        self._sock: Optional[socket.socket] = None
        self._accept_task: Optional[asyncio.Future] = None
        self._conns: set = set()

    async def start(self, host: str, port: int) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(128)
            sock.setblocking(False)
        except BaseException:
            _close_quietly(sock)
            raise
        self._sock = sock
        self._accept_task = asyncio.ensure_future(self._accept_loop(sock))

    async def _accept_loop(self, sock: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                conn, _addr = await loop.sock_accept(sock)
            except asyncio.CancelledError:
                raise
            except OSError:
                return                   # Listen-Socket zu = Feierabend
            task = asyncio.ensure_future(self._serve_conn(conn))
            self._conns.add(task)
            task.add_done_callback(self._conns.discard)

    async def _serve_conn(self, conn: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        try:
            conn.setblocking(False)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            _close_quietly(conn)
            return
        try:
            first = await asyncio.wait_for(_peek_first_byte(conn), timeout=30)
        except asyncio.CancelledError:
            _close_quietly(conn)
            raise
        except (asyncio.TimeoutError, OSError):
            _close_quietly(conn)         # Port-Scanner, Timeout, abgebrochen
            return
        if not first:                    # Gegenseite hat sofort wieder zugemacht
            _close_quietly(conn)
            return

        use_tls = self._ssl is not None and first[0] == _TLS_HANDSHAKE_FIRST_BYTE
        try:
            await loop.connect_accepted_socket(
                self._runner.server, conn, ssl=self._ssl if use_tls else None)
        except (ssl.SSLError, OSError) as e:
            # Typisch: der Browser lehnt das selbstsignierte Zertifikat ab.
            # Das betrifft nur diese eine Verbindung, nicht den Server.
            dash_log.debug(f"[DASHBOARD] Verbindung nicht zustande gekommen: {e}")
            _close_quietly(conn)

    async def stop(self) -> None:
        if self._accept_task is not None:
            self._accept_task.cancel()
            try:
                await self._accept_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
            self._accept_task = None
        # Verbindungen, die noch beim Erkennen bzw. im Handshake stehen: die
        # kennt der aiohttp-Runner noch nicht, also hier selbst abräumen.
        for task in list(self._conns):
            task.cancel()
        self._conns.clear()
        if self._sock is not None:
            _close_quietly(self._sock)
            self._sock = None


def _dash_public_url(port: int) -> str:
    """Öffentlich erreichbare Adresse für die Startmeldung (Env vor config).

    Ohne Portangabe wird der tatsächlich gebundene Port angehängt. Steht in der
    Konfiguration schon ein Port oder ``https://``, wird der Wert unverändert
    übernommen – so stimmt der Link auch hinter einem Reverse-Proxy.
    """
    raw = (os.environ.get("DASHBOARD_PUBLIC_HOST")
           or cfg.config.get("dashboard_public_host") or "").strip().rstrip("/")
    if not raw:
        return ""
    scheme, sep, rest = raw.partition("://")
    if not sep:
        scheme, rest = "http", raw
    if ":" in rest or scheme == "https":
        return f"{scheme}://{rest}"
    return f"{scheme}://{rest}:{port}"


def _dash_resolve_port() -> int:
    try:
        cfg_port = cfg.config.get("dashboard_port")
    except Exception:
        cfg_port = None
    for cand in (os.environ.get("SERVER_PORT"), os.environ.get("PORT"), cfg_port):
        if cand:
            try:
                return int(cand)
            except (TypeError, ValueError):
                continue
    return 8080


# ──────────────────────────────────────────────────────────────────────────
#  Cloudflare Tunnel – optionale eigene Domain ohne Port, ohne Installation
#  auf dem Server. Der Connector-Token kommt aus dem Cloudflare-Konto (Zero
#  Trust → Tunnels), Ziel und Ingress-Regeln stehen dort in der Cloud - hier
#  wird nur der Client-Prozess heruntergeladen, gestartet und am Leben
#  gehalten. Ein Fehlschlag darf NIE den Bot betreffen, das Dashboard läuft
#  in jedem Fall weiter über http(s)://…:<Port>.
# ──────────────────────────────────────────────────────────────────────────
_cloudflared_proc: Optional["asyncio.subprocess.Process"] = None
_cloudflared_task: Optional[asyncio.Task] = None
_cloudflared_stopping = False
_CLOUDFLARED_BACKOFF_START = 5    # Sekunden; verdoppelt sich bis 60 – als
_CLOUDFLARED_BACKOFF_MAX = 60     # Modulkonstanten, damit Tests sie verkürzen können.


def _cloudflared_asset() -> Optional[str]:
    """Name der passenden Cloudflare-Veröffentlichung für dieses System.

    PebbleHost-Container sind Linux/x86_64 - andere Systeme (macOS, Windows)
    unterstützt diese automatische Einrichtung bewusst nicht; dort bleibt nur
    die manuelle Installation von cloudflared laut Cloudflare-Anleitung.
    """
    if platform.system().lower() != "linux":
        return None
    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64"):
        return "cloudflared-linux-amd64"
    if arch in ("aarch64", "arm64"):
        return "cloudflared-linux-arm64"
    if arch.startswith("arm"):
        return "cloudflared-linux-arm"
    return None


def _cloudflared_pfad() -> str:
    return os.path.join(_DASH_DIR, "cloudflared")


async def _cloudflared_sicherstellen() -> Optional[str]:
    """cloudflared besorgen, falls es noch nicht da ist. None bei Fehlschlag."""
    pfad = _cloudflared_pfad()
    if os.path.exists(pfad) and os.access(pfad, os.X_OK):
        return pfad
    asset = _cloudflared_asset()
    if not asset:
        dash_log.warning("[TUNNEL] Cloudflare Tunnel wird auf diesem System nicht "
                         "automatisch eingerichtet (kein Linux). Siehe README.")
        return None
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
    try:
        os.makedirs(_DASH_DIR, exist_ok=True)
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status != 200:
                    dash_log.warning(f"[TUNNEL] cloudflared-Download fehlgeschlagen "
                                     f"(HTTP {r.status}).")
                    return None
                data = await r.read()
        with open(pfad, "wb") as f:
            f.write(data)
        os.chmod(pfad, 0o755)
        dash_log.info("[TUNNEL] cloudflared heruntergeladen.")
        return pfad
    except Exception as e:  # noqa: BLE001 – optionales Feature, darf nie stören
        dash_log.warning(f"[TUNNEL] cloudflared konnte nicht geladen werden: {e}")
        return None


async def _cloudflared_ueberwachen(pfad: str, token: str) -> None:
    """Hält den Tunnel am Leben: startet ihn neu, wenn der Prozess endet.

    Wartezeit steigt nach jedem Fehlschlag (5s → … → 60s) und wird nach einem
    erfolgreichen Start zurückgesetzt - kein Dauerfeuer bei einer echten
    Störung, aber schnelle Wiederkehr nach einem kurzen Netzwerk-Hänger.
    """
    global _cloudflared_proc
    wartezeit = _CLOUDFLARED_BACKOFF_START
    while not _cloudflared_stopping:
        try:
            _cloudflared_proc = await asyncio.create_subprocess_exec(
                pfad, "tunnel", "--no-autoupdate", "run", "--token", token,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            dash_log.info("[TUNNEL] Cloudflare Tunnel gestartet.")
            wartezeit = _CLOUDFLARED_BACKOFF_START
            async for zeile in _cloudflared_proc.stdout:
                text = zeile.decode(errors="replace").strip()
                if text:
                    dash_log.info(f"[TUNNEL] {text}")
            await _cloudflared_proc.wait()
        except FileNotFoundError:
            dash_log.warning("[TUNNEL] cloudflared-Datei fehlt – Tunnel angehalten.")
            return
        except Exception as e:  # noqa: BLE001 – Überwachungsschleife, darf nicht sterben
            dash_log.warning(f"[TUNNEL] Fehler: {e}")
        _cloudflared_proc = None
        if _cloudflared_stopping:
            return
        dash_log.warning(f"[TUNNEL] Verbindung beendet – neuer Versuch in {wartezeit}s.")
        await asyncio.sleep(wartezeit)
        wartezeit = min(wartezeit * 2, _CLOUDFLARED_BACKOFF_MAX)


async def _cloudflared_starten() -> None:
    """Aus start_dashboard() aufgerufen - kein Token, kein Tunnel, kein Aufwand."""
    global _cloudflared_task, _cloudflared_stopping
    token = str(cfg.config.get("cloudflare_tunnel_token") or "").strip()
    if not token:
        return
    _cloudflared_stopping = False
    pfad = await _cloudflared_sicherstellen()
    if not pfad:
        return
    _cloudflared_task = asyncio.create_task(_cloudflared_ueberwachen(pfad, token))


async def _cloudflared_stoppen() -> None:
    global _cloudflared_proc, _cloudflared_task, _cloudflared_stopping
    _cloudflared_stopping = True
    if _cloudflared_proc is not None:
        try:
            _cloudflared_proc.terminate()
            await asyncio.wait_for(_cloudflared_proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001 – Abschalten, keine Ausnahme darf hier stören
            try:
                _cloudflared_proc.kill()
            except Exception:  # noqa: BLE001
                pass
        _cloudflared_proc = None
    if _cloudflared_task is not None:
        _cloudflared_task.cancel()
        _cloudflared_task = None


async def start_dashboard(bot: Any) -> None:
    """Bindet den Bot ans Dashboard und startet den Web-Server (idempotent)."""
    global _dash_runner, _dash_site, _DASH_BOUND
    if _dash_runner is not None:
        return
    _DASH_BOUND = True
    # Eingebettete Frontend-Dateien anlegen, falls sie fehlen (nie überschreiben).
    _extract_assets()
    _ev_load()
    _audit_load()

    if not cfg.config.get("dashboard_enabled", True):
        dash_log.info("[DASHBOARD] deaktiviert (dashboard_enabled=false).")
        return

    port = _dash_resolve_port()
    # Bind-Host: den konfigurierten Host zuerst versuchen, aber IMMER auf
    # 0.0.0.0 zurückfallen. Eine öffentliche IP (z. B. die Panel-Adresse) lässt
    # sich im Container in der Regel NICHT direkt binden
    # ("cannot assign requested address"); 0.0.0.0 bindet alle Interfaces und
    # wird vom Host (PebbleHost/Pterodactyl) nach außen weitergereicht.
    cfg_host = str(cfg.config.get("dashboard_host", "0.0.0.0") or "0.0.0.0").strip()
    hosts = []
    for hc in (cfg_host, "0.0.0.0"):
        if hc and hc not in hosts:
            hosts.append(hc)

    app = build_app()
    _dash_runner = web.AppRunner(app, access_log=None)
    await _dash_runner.setup()

    # HTTPS ist eine Zugabe: klappt sie nicht, bleibt es beim bisherigen
    # reinen HTTP – und zwar über denselben Weg wie früher (web.TCPSite).
    ssl_ctx = _tls_context()
    if ssl_ctx is not None and not _loop_supports_peek():
        dash_log.info("[DASHBOARD] Protokoll-Erkennung auf diesem System nicht "
                      "möglich – HTTPS aus, nur http://.")
        ssl_ctx = None

    bound, last_err = None, None
    for host in hosts:
        try:
            if ssl_ctx is not None:
                site = _DualProtocolSite(_dash_runner, ssl_ctx)
                await site.start(host, port)
                _dash_site = site
            else:
                site = web.TCPSite(_dash_runner, host, port)
                await site.start()
            bound = host
            break
        except OSError as e:
            last_err = e
            hint = " Versuche 0.0.0.0 …" if host != "0.0.0.0" else ""
            dash_log.warning(f"[DASHBOARD] Bind auf {host}:{port} fehlgeschlagen ({e}).{hint}")

    if bound is None:
        await _dash_runner.cleanup()
        _dash_runner = None
        _dash_site = None
        dash_log.error(f"[DASHBOARD] Konnte auf Port {port} nicht binden: {last_err}. "
                  f"Dashboard ist AUS. Ist der Port vom Host freigegeben (SERVER_PORT) "
                  f"und nicht belegt?")
        return

    public = _dash_public_url(port)
    secure = ssl_ctx is not None
    if public:
        dash_log.info(f"[DASHBOARD] ✅ Dashboard läuft:  {public}")
        if secure and public.startswith("http://"):
            dash_log.info(f"[DASHBOARD]    …oder verschlüsselt: "
                          f"https://{public[len('http://'):]}")
            dash_log.info("[DASHBOARD]    (Das Zertifikat ist selbstsigniert – der Browser "
                          "warnt einmalig: \"Erweitert\" → \"Weiter\".)")
        elif not secure:
            dash_log.info("[DASHBOARD]    Nur http:// – ein getipptes https:// scheitert hier "
                          "mit ERR_SSL_PROTOCOL_ERROR.")
        dash_log.info(f"[DASHBOARD]    (lokal auf diesem Rechner: http://127.0.0.1:{port})")
    else:
        dash_log.info(f"[DASHBOARD] ✅ Dashboard läuft auf Port {port} "
                      f"(gebunden an {bound}{', HTTP und HTTPS' if secure else ''}).")
        dash_log.info(f"[DASHBOARD] 🌐 Im Browser über die Adresse deines Servers mit Port "
                      f"{port} öffnen – 127.0.0.1 und 0.0.0.0 sind nur lokale Adressen. "
                      f"Trage die Adresse als \"dashboard_public_host\" in die config.json "
                      f"ein, dann steht hier direkt der fertige Link.")
    await _cloudflared_starten()


async def stop_dashboard() -> None:
    global _dash_runner, _dash_site
    await _cloudflared_stoppen()
    if _dash_site is not None:
        await _dash_site.stop()
        _dash_site = None
    if _dash_runner is not None:
        await _dash_runner.cleanup()
        _dash_runner = None


# Prüfsummen früher ausgelieferter Fassungen. Passt eine Datei auf der
# Platte auf einen dieser Werte, ist sie unverändert und darf durch die
# neue Fassung ersetzt werden (siehe _extract_assets).
_ASSET_KNOWN_HASHES: Dict[str, Tuple[str, ...]] = {
    "index.html": (
        "ee561c69cf7b299f8b48061b08029fe5e4d60e05632e3950ef7ed115b6020dff",
        "490d3cceb44180fdd4986647676e8a18ec0255e5ee6b9cf645019b6ead1d093b",
        "2dcf10883a586ae06ee8b5b4acccaf61c18a870181755e78ec8f1e579bb8fc29",
        "0469d0c19aa2445e00a0d25dcafe86fe3adff474aa7711a5bc1656f1a9e5e381",
        "829b70721c73a3fe1d4b7b374cb72009e488d21e4e06dae6f9d696a6d23d75aa",
        "2dd6cd54b9c94468a6ca32e3fea5b33170e653e82be5076cc24b14e6bb80a912",
        "e3d45a35578865977226ea972d28b39e599b202a9f39ecaf9f3e1e8416ec0ae5",
        "98aa16d6c4199b123a1ce0ddb3e801c18d01bf7ad76c571086d77de050957bf4",
        "da429e13eb81eea34fae48251aaa4f241c00da29dd133961f600d4c59acef435",
        "ad31190ad2083a200a5da7b0d235d8c6fc842be023b50111ac788569b36cd38a",
        "d5f904ce8acc771c440114694b2a80ee7852a0fbcf2f8c6aadadfc591b2a28c4",
        "2f24a2193efa7709d718797326ce0a2ee3edca5d9bcb8f8bb50cfbd8c1382539",
        "359a2f3cd6dd05c12cb65c9ba7f8ff554f2ba0366d3f07216169a71b0c571d23",
    ),
    "styles.css": (
        "f68b843465b48dbe4d294a2f319a1c391eb1b43eed1fea7db39a916c1c1ad804",
        "c178de8eafdc34b2f5bce14ad45a309cf694479c6a2697e9fb52d448347e9e2b",
        "0cb11dc0971824c737ca5505bed58e31dc48263cf1ff26d60f38a6cd6e46a69a",
        "a4154181f2695360732d4d51df4e46012e38e12c1b7dbda0c763472cff7ccc87",
        "08dfff8ffc17615a9a29719174639e8acb84ad765d3a2b181e173dc77d0621b4",
        "a6e7a05ee6abab3e661621d9925dee4094217957ef17a4774f56435689e20792",
        "e0ffe280cc8ca30febb602b6611b142ca3a64a96ef1d529d9b04548056e12a67",
        "a655690c22aab2ccfb614fd0156600f0b5649d9757f42aa435a88e25670a1c10",
        "e35555671c00f3829f30ffbfa0793755eaacf6c230cd13fca818f7ccc6b7ce7f",
        "df850ef1d45b3a7883533d3b0640e3d7054a8c320b5037423474af581b73ba3b",
        "ae4ade4f00cf352d548cf1fba84c7a37d2fad67a984c9e12bdd91d5e7517e79e",
        "32c617179befba15e57c08dde154cfe8218983afdfe44481701bb307cef91a15",
        "57fc2c0b3589d7fe1807402f57280f4865442466fdb602546598c55b3e48c5aa",
        "e66066c8c0e169191ec3a36040fd867816b765c4ebbdfb3c224fcf8a4801a4df",
        "e429b2bb614a8920d84cafcb5cbbf25d6e7aa139159efbde2f5c72fcfafd596f",
        "023a0e08e2e423b019f3a6259635a5071baaf9a0ba253e6bb79861dea8b549c3",
        "2595bb9f58b189e9cdc056e3ded6e50ec29a8c5d04175381fb004d7cdaa532e0",
        "384af4b98b2564fcb2ddbf68ca79881ffa34735f49f53361a1dced07247b1346",
    ),
    "app.js": (
        "ec14b8b4a90c6553d11b22eb5f76c732b47c827e8bf4359e9f2fb793f8ca1b79",
        "82e5da5110b186c3234f7637dd397a87cb2f6939e165b35fbb21d992a4200689",
        "bee4fd3e9b3370dbd02f4f32cb88049eb754892d98fbd4a92cd2b32699d1389f",
        "087abf48c810dec7bf622d0729c4cfce47fa599fb6da1789d69b8a70bb1d0d18",
        "ab6d6775e6fa45093ff46ba47f826f311510b66ead9ef1e7dea056da8c3d912f",
        "efb12fbcf465d781d30a0f999fa614fe9fc71945db872ceecbcee3e58702cd08",
        "753c3e87015be64e6aa8821e72a273d1dd08f8d105c75fec0a41b7712239d925",
        "3294566eb69f57eaa40d7c6894ec00de6dd615b14c5a461438afe308a919080f",
        "3a80a77238af8edfd6f5b7665cc9d3e8751b93e44fc97a788539115b4b6871e7",
        "c6322507322c57be75706bde57c18aee838c3faf8fe91eeb9ed1b94073ac7e57",
        "ad875efa6aeeee0802715c71400ed69dc033d6bf1e58237846ab0339b68b52b6",
        "91c4ca87a1f11b977a25f4c82821b979d9510453f2ab3531c7fd8e70941b6ce6",
        "9fb289e3d35607cf31a56cc111e81c0d42f027a084f82dcebfa319eeed72e972",
        "0741e72a3075fd23fd9b3a8793d2bcc5f6810c14f4163ae6b90931894a0245a2",
        "f4e2be0fdffa81d9dae9c36a0eefeb9745444d7d633157e2cac341dbd41205d6",
        "df1a2923868c6b80e5d71f4721a157b3c0a6a7146ef241018346abf5860e9f8a",
        "147edc407d9732a5106c2009badc690487d48cdfb80ff8187eef01b5934d7716",
        "8cb2695d935af7edba0d89ff049fd780c5c3f4d06f9622ebd5844884e6b98b4e",
        "198aab677dc5c771fe5fd7ad773320fc7927226a4d96878d5ad024f53fba32fc",
        "4f4e0c9024847b6c4c2c70d5627037bd82de6c2c6914dda6a056829e6b7c6f81",
        "c4afe21969e46dd40f6bd1af69093e368e567e3e4fab0569c06f24bed5ebe7c9",
        "6d4705e202ee238cadc6c825ebe2b482453a513839628a18d782abad81f311b3",
        "28332ee6bf23840c37694e93a417bce7a2a87c66460639c64654a85692c0233b",
        "44477910ef10ba281b66ebecf7b9bbb321548d452ab90811391f9ea58471819b",
        "fc33b5f96d9f2f26aacca794a110a4888d798d9560184d00fd13eb53cfb0205a",
        "d2fcfb61e64ab1eb57690efb4f52af64181349713736174a0186b8db75e9e747",
    ),
    "map.js": (
        "f7c261a280532fbaaf046ad16e9fb480a6f9e98a7648c13f77d731da9409f98d",
        "8792f1ec3cda099c3175565f12218d981b6f81bc206359ee6b70460459c5bbd0",
    ),
    "vendor/leaflet.css": (
        "a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6",
    ),
    "vendor/leaflet.js": (
        "db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a",
    ),
    "locations/ChernarusPlus.json": (
        "46290c49d3ee8769168729a7377a6bac8915b379efc276d00821517b267dd9eb",
    ),
    "locations/Livonia.json": (
        "0596b35bfbc53850d1f9495eb282480993e3354819bad1b610d29916b224fb7b",
    ),
    "locations/Sakhal.json": (
        "68a7708a1b2fa4ad5689eb31005e2397f1fca1c1a02a6b7cdb0643655b909f4f",
    ),
}


# ══════════════════════════════════════════════════════════════
#  EINGEBETTETE FRONTEND-ASSETS  (zlib + base64)
# ══════════════════════════════════════════════════════════════
#  Erzeugt aus dashboard/static/** mit zlib.compress(data, 9) und
#  base64.b64encode. _extract_assets() schreibt sie beim Start nach
#  dashboard_web/static/. Reine Daten – hier
#  ist absichtlich keine Logik.

_EMBEDDED_ASSETS: Dict[str, str] = {
    # index.html  (9.910 Bytes roh → 4.176 Bytes base64)
    "index.html": (
        "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"
    ),
    # styles.css  (18.487 Bytes roh → 7.772 Bytes base64)
    "styles.css": (
        "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"
    ),
    # app.js  (152.388 Bytes roh → 55.620 Bytes base64)
    "app.js": (
        "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"
    ),
    # map.js  (12.586 Bytes roh → 5.548 Bytes base64)
    "map.js": (
        "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"
        "uS9Tv7N7t2FwBVvU4gr6/QsDewcd"
    ),
    # vendor/leaflet.css  (14.806 Bytes roh → 4.680 Bytes base64)
    "vendor/leaflet.css": (
        "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"
    ),
    # vendor/leaflet.js  (147.552 Bytes roh → 56.564 Bytes base64)
    "vendor/leaflet.js": (
        "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"
    ),
    # locations/ChernarusPlus.json  (16.626 Bytes roh → 4.548 Bytes base64)
    "locations/ChernarusPlus.json": (
        "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"
        "4vbQ63+o/vXhr/8CBQN9zw=="
    ),
    # locations/Livonia.json  (4.910 Bytes roh → 1.596 Bytes base64)
    "locations/Livonia.json": (
        "eNqVV8tu20gQvPsrBJ2zxLwfe1uvgQSxkw1sAwGyWCzGIpEdiOQIIzGKFOSSX/InGPmvNB3I6dZCNHnwgZRV6p6qrq75cjabN2E1/302v4qfUhvD/AW8"
        "2qZcl/+u476CT7hwjPVv16nLi/7NvAy7ffE5NEXbzXgh/G/i8Wt1WoRNTO0a/ufvs9nsC/zN5m1oHr90Hqs6bB9/AN5+7pG1lYX6+byHZ2+sLfTP503/"
        "lU+xrsPHag5vvr74H+BVt0xbBCe18IX5Bce5N2YC3ss6tnG9xBUq7wWGhEde2NGI57lqMZwx3NIKBecFQ3iLuNmdALsNuSX9eilcIREa80wVbhzaTdy0"
        "cYnJ4JoJDOd7dtj4XlOuMJ537InMHs4prws+Gu5DWIWyBjoSwnRMu0IgTAvH50Zjvo0lKdGAtAuP4BiXhxMYAXebVgkkg8/QQX3uF6CVkh1+4DlC/ihD"
        "83BP9MyswYQYp/mE8t50OWx2uDylDaHESKcO8zcC8DqUXUv6tcI7PMBGeT6236uquUsZ0+GMY6Q8I/WEYXsZqxzqDT1DmDDFDiruQZWEHxnf89tQ3j3c"
        "ZyIa5nGV0jt50ORzPX8IdbWOFe7Zm8NQ9GBaSzNhSF51G2wvmiuOS9PKignjAfyGxR7rhcEIYzl76Qm9w3h/1et9aKmfOu4xG9ZYNaHfq7iKxFC1VxxP"
        "sHGCTfCsy1SDCnMqd5gSBSbNyBpxfELblyEfuQIcozgI+bFKsNYJVd6ku7Qhx2iENrhtzoQWE0q8TvtNIogWasJeoyWfsorfpToQYqSwZBcLLsUEoi9S"
        "fbydJBfEavQUb7hJNHhwYwRWNhfMKOKt/wHeKYLzmhZnHaiY0MFAMWIc2kUAH/yINQ0hSJKMoEinA1g3y1DjPoX3Hu9L7jW1lyGsbcikLuXgyNFcCCFG"
        "H9l1KsEK4gIX55wjbiWYEmQqBvDOw8c2EUKdoISCYBQRcB9OT8G9z/tqvSfeB1FK4qNTsDhG413kagsSiZhVJxX2AM08ndghvMuuPmoXwhrhQno+urpX"
        "uSuxmSjwDsSDl5YTkQxhvQ7tJmQSwiWZU+c1jX1NyLE9ucNzuIvtekl2ZD8BVMbMe7pzB0Hfp+McyYU0DvfMmYMoxMYiPobnxXGVinPqAko+eeDY1sna"
        "MHB9wW33Nk8An7t9hHaftrM/w3pTk1L7fYS7h9IdCQq5g1JOWH1MuWupfsidRnAIwWbcHL+u9l2miU0wT0KHkCCgsYNyu2vuAgmVwjOiRy4EtebBSanK"
        "vFvS+pTmkgRKC5tztNHEMpG1xoQkQhQcbMeMtYW0ImFIc3L7cJCuxhJxGZpYtTF9/5aa2ZuwXO+aWMdAeNawgbEjOmVHE/MuVkvAxkYhhcTjAq5jyToZ"
        "gruBuzDQQm5zcNdyOEsrbxlh+rks2KYVsWwYC0dDjPN2QsqCSWnhToIRJcRVhY8QGCJ3h9CsTkYsiGy0PCPJCXKvRqNddw2wW9GIZS2ZZK2cJdY9gHcV"
        "tlVbkvUOY4L2nTPMEHoHwGCK49HFAda55hgPMpYRJBcNnd3D/fdvy95gqwVNDZaTG7Z2VDJPPng2++fs6w/oLkIJ"
    ),
    # locations/Sakhal.json  (4.977 Bytes roh → 1.460 Bytes base64)
    "locations/Sakhal.json": (
        "eNqVmF1v2zYUhu/zKwRftwG/Se2u6YKiWJMGsZehLYqBszmLkywK+vAqF73en9uPGt2gCA8De+RFLqRIjw7PefmeQ3+9KBY73S1+KhZLXVe6Wbzwd/52"
        "fbP5fbAH4/+BORXoeHdwU78+3lls9Hy4/KJ3l+1U4EtSviTfX2vcWo/WtYN/5tNFUXz1f8Wi1bvvL92ZsXed3jduP9QvH79m26E+vuof++KfYZKVl+jx"
        "+nD8NBIl/3FjPELWurOjj9Lf+fbi2SeWZm96t3M9oJacMUClggtIteN8AvmqtXsdwDBRTyEdaZJImgq7dVu3dwFNKgxCk+VTAv4PduUaN7ZuNgGPI4ZC"
        "nhJCAt7eNo3emhPIn6eNbcP4BKEULpbxDN7K1hUMUBARBwgXfNTQqfKudK9b/VdYWolEiBNcsWTch+lQRfnDqJQEACmhOQl0XtJuX4eKKRXFIdJf8+QY"
        "37h+1uvKANUoKaGgEeLpWfygh1bPurhzzezTGXIxBtkscYmTse/M4NXdR4v3CRVgS5fxjj6f0Eeb0FuwA7GgEVJlIN/++8849VMHoqQSw4xKhJKX/uCG"
        "0YWOg2mJocwpzdmHy2raxcpkVIFVK8VIBvJK2xp6D2YcLrr07pNBvNHez4eYGRkGxlyJDOj9tGmPMhpgOrmSgEowJ6k+eT//EeeSsKjcJM8n79zGx9i6"
        "yDoUh22GoDID+r5vomR6VcJkUspylL6adrqFTMWwjHohy6nP9d7MYf/yvRXiOM5R5dGLnvVr7xA4WjbFObvH7c041FHjYRjmkkiV4+wfq6mLk8kli2TE"
        "cU4yH+bBRWFijP1+IeFEIPilSkb+5po/izuja2DrpYB7Uggoo8ozTyVz9J5+qGD3EVEmozZxBvfOtaaZiwfXrHULxY4InP0IUjIV+34Y+xmkkbNnay5T"
        "aaupc5CmnjrMI42I5Njerk1xbzdbaBaSi7Nz0LnwemfhhIGivk3S63uthzEWjJ9xKbCykuFk4Me9OWzaeS5ubW0aQEVRb0DxcHWuyf44MBRvjOs6YGkI"
        "WhoiKH0IXE3tVvcgTEFB78beOdIHtl+Hqrag0ipyXCZo+lS17KaxtWFxpFRQ2Uwhkh6eP58NoYUxDmvir9MXu3LrCiiRSg57IGMZwb2uTG+6Y53D8kIp"
        "+q2nMqoRzXmcglqojPn2poemyljUm4lKr6o/JvgGBVUHC0H8jJPMu27sQffR9EBZCY4yHMuMCBvnZwe/jcMWSmELFYyzHOGtKxvilCAKxhdNIudwv8xm"
        "dBb0Yqg8gkQ67cYOg21McaXDABmFfVOSyKN3urftyYHbnzFq3QY88uywylDqELusdDsCnyJEgvTRYMBJmg23E7A9SQFO4AyXujU9XKkQUUMqZXppTd2H"
        "BiCxApH5I0a6AbyaQWB+8gcSpoik29NVo9d18Vp38BcXBSZgystTxIvi88W3/wDe71Er"
    ),
}


# ══════════════════════════════════════════════════════════════
#  Start
# ══════════════════════════════════════════════════════════════
def main():
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║        DayZ Discord Bot – Server Management          ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    cfg.load_all()

    # Katalog bei Bedarf automatisch aus der types.xml erzeugen (Generator ist integriert)
    shop_file = str(cfg.config.get("shop_items_file") or "shop_items.json")
    if not os.path.exists(shop_file) and os.path.exists(TYPES_XML_FILE):
        n = generate_shop_items_from_types(TYPES_XML_FILE, shop_file)
        if n:
            print(f"   Shop-Katalog aus types.xml generiert: {n} Items -> {shop_file}")

    # Kataloge sind serverbezogen. Der Hauptserver uebernimmt beim ersten Start
    # den bisherigen gemeinsamen Bestand; alle anderen Server starten leer und
    # holen sich ihre types.xml per FTP von IHREM eigenen Server.
    _haupt = connections.primary()
    if _haupt is not None:
        print(f"   Shop-Katalog {_haupt.name}: {len(_haupt.catalog.items)} Items")

    valid, missing = cfg.is_valid()
    if not valid:
        print("❌ KONFIGURATION UNVOLLSTÄNDIG!")
        print(f"   Bitte öffne '{CONFIG_FILE}' und fülle folgende Felder aus:")
        for field in missing:
            print(f"   → {field}")
        print()
        print(f"   Die Datei '{CONFIG_FILE}' wurde automatisch erstellt.")
        print("   Es werden nur bot_token und guild_ids benötigt – die Nitrado-")
        print("   Anbindung richtest du danach im Discord mit /setup token ein.")
        print()
        sys.exit(1)

    if cfg.has_nitrado_token():
        print("🔎 Erkenne Nitrado-Server (Service-ID, FTP-Zugang, Karte)...")
        if not asyncio.run(auto_detect_from_nitrado()):
            print()
            print("⚠️  NITRADO-AUTO-ERKENNUNG FEHLGESCHLAGEN!")
            print("   Der Bot startet trotzdem – richte die Nitrado-Anbindung im")
            print("   Discord (neu) ein: /setup token <dein-nitrado-token>")
            print()
    else:
        print("ℹ️  Noch kein Nitrado-Token gesetzt – der Bot startet ohne")
        print("   Nitrado-Anbindung. Richte ihn im Discord ein:")
        print("   /setup token <dein-nitrado-token> → Server im Dropdown auswählen")
        print("   → bestätigen. FTP-Zugang & Karte werden automatisch erkannt.")
        print()

    print(f"✅ Konfiguration geladen")
    print(f"   Service-ID:     {cfg.config.get('service_id') or '(per /setup token einrichten)'}")
    print(f"   FTP-Host:       {cfg.config.get('ftp_host') or '(per /setup token einrichten)'}")
    print(f"   Karte:          {cfg.config.get('map_name', 'ChernarusPlus')}")
    print(f"   Log-Verzeichnis:{cfg.config.get('ftp_log_dir') or '(wird automatisch gesucht)'}")
    print(f"   Server-IP:      {cfg.config.get('server_ip') or '(nicht gesetzt)'}")
    print(f"   Query-Port:     {cfg.config.get('query_port', 2302)}")
    print(f"   RCON-Port:      {cfg.config.get('rcon_port',  2310)}")
    print(f"   Guild IDs:      {cfg.config.get('guild_ids', [])}")
    print(f"   Poll-Intervall: {cfg.config.get('log_poll_interval_seconds', 10)}s")
    print(f"   Admin-Rolle:    {cfg.config.get('admin_role_name')}")
    print(f"   Admin-Rollen-IDs: {cfg.config.get('admin_role_ids', []) or '(keine – Fallback Rollen-Name)'}")
    print(f"   Währung:        {cfg.config.get('currency_name')} ({cfg.config.get('currency_symbol')})")
    print(f"   Shop-Items:     {len([i for i in cfg.config.get('shop_items', []) if i.get('enabled', True)])} aktiv")
    print(f"   Auto-Restart:   {'AN' if cfg.config.get('auto_restart_after_purchase') else 'AUS (Items spawnen beim nächsten regulären Neustart)'}")
    print()
    print("🚀 Starte Bot...")
    print()

    bot.run(cfg.config["bot_token"], log_handler=None)


def run_dashboard_only():
    """Nur das Web-Dashboard starten – OHNE Discord-Login.

    Praktisch für die lokale Vorschau/Entwicklung: das Dashboard läuft gegen
    dieselben Objekte wie im Vollbetrieb (cfg, catalog, db, Nitrado/FTP nach
    Token-Eingabe im Onboarding). Nur die Live-Channel-/Rollen-Listen bleiben
    leer, weil der Bot dabei nicht bei Discord eingeloggt ist.

    Aufruf:  python bot.py --dashboard-only    (oder --no-discord)
    """
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║   DayZ Dashboard – Vorschau (ohne Discord-Login)    ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    cfg.load_all()
    shop_file = str(cfg.config.get("shop_items_file") or "shop_items.json")
    if not os.path.exists(shop_file) and os.path.exists(TYPES_XML_FILE):
        generate_shop_items_from_types(TYPES_XML_FILE, shop_file)
    _haupt = connections.primary()
    if _haupt is not None:
        _haupt.catalog          # laedt bzw. uebernimmt den Katalog des Hauptservers


    async def _serve():
        # start_dashboard bindet das Dashboard an die Bot-Instanz, startet den
        # Web-Server (dieselbe Funktion wie im Vollbetrieb) und loggt den
        # klickbaren Link http://127.0.0.1:<port>.
        await start_dashboard(bot)
        print("  (ohne Discord-Verbindung · Strg+C zum Beenden)\n")
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            # Sonst bleiben Runner, Listener und ein eventuell gestarteter
            # Cloudflare-Tunnel beim Beenden haengen – stop_dashboard() wurde
            # bisher an keiner Stelle aufgerufen.
            await stop_dashboard()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    _args = [a.lower() for a in sys.argv[1:]]
    if any(a in ("--dashboard-only", "--dashboard", "--no-discord") for a in _args):
        run_dashboard_only()
    else:
        main()
