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
import glob
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
    "log_poll_interval_seconds": 10,
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
    "dashboard_public_host": "testdashboard.my.pebble.host",
    # HTTPS: Das Dashboard nimmt auf DEMSELBEN Port zusätzlich TLS an, weil
    # Browser getippte Adressen oft von sich aus auf https:// hochstufen – ohne
    # TLS endet das in ERR_SSL_PROTOCOL_ERROR und die Seite ist gar nicht
    # erreichbar. Das Zertifikat ist selbstsigniert und wird beim Start
    # automatisch erzeugt; der Browser zeigt deshalb einmalig eine Warnung
    # ("Erweitert" → "Weiter"). Ganz ohne Warnung geht nur ein echtes
    # Zertifikat, z. B. über einen Cloudflare Tunnel (siehe README).
    # false = wie früher ausschließlich HTTP.
    "dashboard_https":       True,

    # ─────────── DISCORD-LOGIN FÜRS DASHBOARD ───────────
    # Solange discord_client_secret leer ist, bleibt der Login AUS und das
    # Dashboard verhält sich wie bisher. Das ist Absicht: ein Update darf ein
    # laufendes Dashboard nicht aussperren.
    # Einrichten: Discord Developer Portal → deine App → OAuth2 →
    #   1. "Client Secret" kopieren und hier eintragen
    #   2. unter "Redirects" die Dashboard-Adresse + /api/auth/discord/callback
    #      eintragen, z. B. http://testdashboard.my.pebble.host:25590/api/auth/discord/callback
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
3. Führe im Discord /setup token <dein-nitrado-token> aus:
   Es öffnet sich ein Dropdown mit deinen Nitrado-Servern –
   Server auswählen und bestätigen. FTP-Zugang, die aktive Karte
   und die Log-Verzeichnisse erkennt der Bot dann automatisch.
4. Benutze /setup feeds im Discord um Channels zuzuweisen.

BEFEHLE (alle nur für Admins mit der konfigurierten Rolle)
──────────────────────────────────────────────────────────
/setup token <token>            → Nitrado-Token setzen; danach Server im
                                   Dropdown auswählen & bestätigen (erkennt
                                   FTP-Zugang und aktive Karte automatisch)
/setup feeds <feed> #channel    → Feed-Channel setzen (Dropdown-Auswahl:
                                   killfeed, damagefeed, joinleave, suicide,
                                   chat, adminlog, envdeath, vehiclecrash,
                                   basebuild, loot, connecting, shop_log,
                                   economy_log, status, restart, zone)
/setup uebersicht               → Alle Einstellungen anzeigen

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

    def get_channel(self, guild_id: int, log_type: str) -> Optional[int]:
        return self.guilds.get(str(guild_id), {}).get(log_type)

    def set_channel(self, guild_id: int, log_type: str, channel_id: int):
        gid = str(guild_id)
        self.guilds.setdefault(gid, {})[log_type] = channel_id
        self.save_guilds()

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

    def read_from_offset(self, path: str, offset: int) -> Tuple[str, int]:
        def op(ftp):
            buf = io.BytesIO()
            ftp.retrbinary(f"RETR {path}", buf.write, rest=offset if offset > 0 else None)
            return buf.getvalue()
        try:
            raw = self._with_conn(op)
        except Exception as e:
            log.debug(f"[FTP] read_from_offset({path}): {e}")
            return "", offset
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

    def file_size(self, path: str) -> int:
        def op(ftp):
            return ftp.size(path)
        try:
            return int(self._with_conn(op) or 0)
        except Exception:
            return 0

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

def _izurvive_url(x: float, y: float, map_name: str = "ChernarusPlus") -> str:
    return f"https://www.izurvive.com/?m={map_name}#l={x:.1f};{y:.1f}"

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
    map_name = cfg.config.get("map_name", "ChernarusPlus")
    url  = _izurvive_url(x, y, map_name)
    loc  = _nearest_location(x, y, map_name)
    near = f"\n*(Near {loc})*" if loc else ""
    return f"[{x:.1f}, {y:.1f}, {z:.1f}]({url}){near}"


# ══════════════════════════════════════════════════════════════
#  DayZ Log-Parser
#  Quelle: Nitrado DayZ Konsolen-Server .ADM Logs
# ══════════════════════════════════════════════════════════════

class DayZLogParser:
    # Spieler-Positionen in-memory halten
    player_positions: Dict[str, Dict] = {}

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
        # Umwelt-Tod (kein PvP) – verschiedene Formulierungen
        "kill_env": re.compile(
            PLAYER + r'\s*(?:died|was killed|has died|perished|bled out|starved|dehydrated|drowned|suffocated|froze to death)'
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
            PLAYER + r'\s+(?:placed|built|constructed|dismantled|repaired|attached|removed|folded|packed|deployed|mounted|unmounted)\s+([^\n]+)',
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
    }

    @classmethod
    def _players_found(cls, line: str) -> List[Dict[str, Optional[str]]]:
        return [{"name": name, "id": pid or None} for name, pid in re.findall(cls.PLAYER, line)]

    @classmethod
    def _set_position(cls, name: str, player_id: Optional[str], pos: str):
        cls.player_positions[name] = {
            "id": player_id,
            "position": pos.strip(),
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def _extract_ts(cls, line: str) -> str:
        ts_m = re.match(r'^(\d{2}:\d{2}:\d{2})\s*\|?\s*', line)
        return ts_m.group(1) if ts_m else ""

    @classmethod
    def _generic_kill_event(cls, line: str, ts: str) -> Optional[Dict]:
        players = cls._players_found(line)
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
            cls._set_position(victim["name"], victim["id"], pos_m.group(1))

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

    @classmethod
    def _generic_damage_event(cls, line: str, ts: str) -> Optional[Dict]:
        players = cls._players_found(line)
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
            cls._set_position(victim["name"], victim["id"], pos_m.group(1))

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

    @classmethod
    def _generic_env_death_event(cls, line: str, ts: str) -> Optional[Dict]:
        """Tod ohne zweiten Spieler: Zombie, Explosion, Verbluten, Sturz usw."""
        players = cls._players_found(line)
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
            cls._set_position(p["name"], p["id"], pos_m.group(1))

        return {
            "type": "kill_env",
            "timestamp": ts,
            "player": p["name"],
            "player_id": p["id"] or "Unbekannt",
            "cause": cause,
            "raw": line,
        }

    @classmethod
    def _generic_env_damage_event(cls, line: str, ts: str) -> Optional[Dict]:
        """Treffer ohne zweiten Spieler: Zombie, FallDamage, Explosion, Tier usw."""
        players = cls._players_found(line)
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
            cls._set_position(victim["name"], victim["id"], pos_m.group(1))

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

    @classmethod
    def parse_line(cls, line: str):
        line = line.strip()
        if not line:
            return None

        ts = cls._extract_ts(line)

        # Positionen immer tracken – Konsolen-Format zuerst (pro Spieler in
        # der eigenen id-Klammer), sonst altes Format als Fallback
        tracked = False
        for pm in cls.P["position"].finditer(line):
            cls._set_position(pm.group(1), pm.group(2), pm.group(3))
            tracked = True
        if not tracked:
            pm = cls.P["position_legacy"].search(line)
            if pm:
                cls._set_position(pm.group(1), pm.group(2), pm.group(3))

        # Reihenfolge ist wichtig:
        # 1) Kill
        m = cls.P["kill_pvp"].search(line)
        if m:
            pos = m.group(7)
            if pos:
                cls._set_position(m.group(1), m.group(2), pos)
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
        m = cls.P["suicide"].search(line)
        if m:
            return {
                "type": "suicide",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                "raw": line,
            }

        # 3) Environment death
        m = cls.P["kill_env"].search(line)
        if m and "killed by player" not in line.lower():
            # Gruppe 3 = "by <Ursache>", Gruppe 5 = "due to <Ursache>"
            cause = m.group(3) or m.group(5)
            if not cause:
                ev = cls._generic_env_death_event(line, ts)
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
        m = cls.P["damage"].search(line)
        if m:
            pos = m.group(9)
            if pos:
                cls._set_position(m.group(1), m.group(2), pos)
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
        m = cls.P["connect"].search(line)
        if m:
            pos = m.group(3)
            if pos:
                cls._set_position(m.group(1), m.group(2), pos)
            return {
                "type": "connect",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                "position": pos.strip() if pos else None,
                "raw": line,
            }

        m = cls.P["disconnect"].search(line)
        if m:
            pos = m.group(3)
            if pos:
                cls._set_position(m.group(1), m.group(2), pos)
            return {
                "type": "disconnect",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                "position": pos.strip() if pos else None,
                "raw": line,
            }

        m = cls.P["connecting"].search(line)
        if m:
            pos = m.group(3)
            if pos:
                cls._set_position(m.group(1), m.group(2), pos)
            return {
                "type": "connecting",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                "position": pos.strip() if pos else None,
                "raw": line,
            }

        # 6) Chat
        m = cls.P["chat"].search(line)
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
        m = cls.P["chat_console"].search(line)
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
        m = cls.P["admin_action"].search(line)
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
        m = cls.P["basebuild"].search(line)
        if m:
            return {
                "type": "basebuild",
                "timestamp": ts,
                "player": m.group(1),
                "player_id": m.group(2) or "Unbekannt",
                "item": m.group(3).strip(),
                "raw": line,
            }

        # 9) Fahrzeug
        m = cls.P["vehicle"].search(line)
        if m:
            return {
                "type": "vehicle",
                "timestamp": ts,
                "raw": line,
            }

        # 10) Loot
        m = cls.P["loot"].search(line)
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
            players = cls._players_found(line)
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
            ev = cls._generic_kill_event(line, ts)
            if ev:
                return ev
            ev = cls._generic_env_death_event(line, ts)
            if ev:
                return ev

        # Treffer: erst PvP (2 Spieler), sonst Umwelt (Zombie, FallDamage, ...)
        if "hit by" in low:
            ev = cls._generic_damage_event(line, ts)
            if ev:
                return ev
            ev = cls._generic_env_damage_event(line, ts)
            if ev:
                return ev

        # Sonstige Todesarten ohne "killed by"
        if any(kw in low for kw in (" died", "bled out", "perished", "starved",
                                    "dehydrated", "drowned", "suffocated", "froze to death")):
            ev = cls._generic_env_death_event(line, ts)
            if ev:
                return ev

        # Join/Leave/Connecting – falls das Format erneut abweicht
        if "connect" in low:
            players = cls._players_found(line)
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
                        cls._set_position(p["name"], p["id"], pos_m.group(1))
                    return {
                        "type": ctype,
                        "timestamp": ts,
                        "player": p["name"],
                        "player_id": p["id"] or "Unbekannt",
                        "position": pos_m.group(1).strip() if pos_m else None,
                        "raw": line,
                    }

        # Basis-Bau – falls Verb/Format abweicht
        m_build = re.search(r'\b(placed|built|constructed|dismantled|repaired|attached|removed|folded|packed|deployed|mounted|unmounted)\s+(.+)$',
                            line, re.IGNORECASE)
        if m_build:
            players = cls._players_found(line)
            if players:
                return {
                    "type": "basebuild",
                    "timestamp": ts,
                    "player": players[0]["name"],
                    "player_id": players[0]["id"] or "Unbekannt",
                    "item": m_build.group(2).strip(),
                    "raw": line,
                }

        return None

    @classmethod
    def parse_lines(cls, content: str) -> List[Dict]:
        events = []
        for line in content.splitlines():
            ev = cls.parse_line(line)
            if ev:
                events.append(ev)
        return events


# ══════════════════════════════════════════════════════════════
#  Discord Embed-Builder
# ══════════════════════════════════════════════════════════════
def _footer(ev: Dict) -> str:
    return f"🕐 {ev['timestamp']}" if ev.get("timestamp") else ""

def _dist(d: str) -> str:
    return f"{d} m" if d != "?" else "Nah­kampf"

def _add_location_field(e: discord.Embed, ev: Dict, player_key: str):
    """Fügt das '📍 • Player Location'-Feld hinzu (gleiches Aussehen wie bei
    Connect/Disconnect): Position aus dem Event selbst oder die zuletzt
    getrackte Position des Spielers, als klickbarer iZurvive-Link."""
    name = ev.get(player_key) or ""
    pos = ev.get("position") or DayZLogParser.player_positions.get(name, {}).get("position")
    loc_val = _location_field_value(pos)
    if loc_val:
        e.add_field(name="📍 • Player Location", value=loc_val, inline=False)


class EmbedBuilder:
    @staticmethod
    def build(ev: Dict) -> Optional[discord.Embed]:
        t = ev["type"]
        if t == "kill_pvp":
            e = discord.Embed(
                title="☠️ KILL",
                description=f"**{ev['killer']}** hat **{ev['victim']}** getötet",
                color=0xE74C3C
            )
            e.add_field(name="Waffe",       value=ev["weapon"],         inline=True)
            e.add_field(name="Distanz",     value=_dist(ev["distance"]),inline=True)
            _add_location_field(e, ev, "victim")
            e.add_field(name="Killer ID",   value=f"`{ev['killer_id']}`",  inline=False)
            e.add_field(name="Opfer ID",    value=f"`{ev['victim_id']}`",  inline=False)

        elif t == "suicide":
            e = discord.Embed(
                title="💀 SELBSTMORD",
                description=f"**{ev['player']}** hat sein Leben beendet",
                color=0x7F8C8D
            )
            _add_location_field(e, ev, "player")
            e.add_field(name="Steam-ID", value=f"`{ev['player_id']}`", inline=False)

        elif t == "kill_env":
            e = discord.Embed(
                title="☠️ TOD",
                description=f"**{ev['player']}** ist gestorben",
                color=0xE67E22
            )
            e.add_field(name="Ursache",  value=ev["cause"],              inline=True)
            _add_location_field(e, ev, "player")
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
            _add_location_field(e, ev, "victim")

        elif t == "connect":
            e = discord.Embed(
                title=f"→ • Connect • {ev.get('timestamp', '')}",
                description=f"**{ev['player']}** connected to the game server.",
                color=0x5865F2
            )
            _add_location_field(e, ev, "player")
            e.add_field(name="Steam-ID", value=f"`{ev['player_id']}`", inline=False)

        elif t == "disconnect":
            e = discord.Embed(
                title=f"← • Disconnect • {ev.get('timestamp', '')}",
                description=f"**{ev['player']}** left the game server.",
                color=0xE74C3C
            )
            _add_location_field(e, ev, "player")
            e.add_field(name="Steam-ID", value=f"`{ev['player_id']}`", inline=False)

        elif t == "connecting":
            e = discord.Embed(
                title=f"↔ • Connecting • {ev.get('timestamp', '')}",
                description=f"**{ev['player']}** verbindet sich...",
                color=0x3498DB
            )
            _add_location_field(e, ev, "player")
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
            _add_location_field(e, ev, "player")
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
        else:
            return None

        if t in ("connect", "disconnect", "connecting"):
            if ev.get("timestamp"):
                e.set_footer(text=f"Server Time: {ev['timestamp']}")
        elif _footer(ev):
            e.set_footer(text=_footer(ev))
        return e


# ══════════════════════════════════════════════════════════════
#  Bot-Klasse
# ══════════════════════════════════════════════════════════════
class DayZBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree    = app_commands.CommandTree(self)
        self.nitrado: Optional[NitradoAPI] = None
        self.ftp:     Optional[FTPManager]  = None
        self.parser   = DayZLogParser()
        self.shop:    Optional["ShopManager"] = None   # wird in on_ready initialisiert
        self._ftp_warned_ts   = 0.0    # Zeitpunkt der letzten FTP-Ausfall-Warnung
        self._ftp_warn_active = False  # Warnung aktiv → bei Erholung Entwarnung posten
        self._online_since: Optional[float] = None  # Server online seit (A2S, Bot-Sicht)
        self._restart_announced: set = set()  # (restart_ts, minuten) bereits angekündigt
        # Zonen-Pings (/zone create): wiederholte Pings im Cooldown-Intervall
        self._zone_last_ping: Dict[Tuple[str, str], float] = {}  # letzter Ping pro Zone+Spieler
        self._zone_pos_seen: Dict[str, str] = {}              # Spieler → bereits bewertetes last_seen
        self._discover_retry_ts = 0.0  # letzter Auto-Discovery-Retry (log_poll)

    async def setup_hook(self):
        # Web-Dashboard im selben Prozess/Loop starten (aiohttp). Fehler hier
        # dürfen den Bot-Start nicht verhindern.
        try:
            await start_dashboard(self)
        except Exception as e:
            log.error(f"[DASHBOARD] Start fehlgeschlagen: {e}")

        # Persistente Views registrieren, damit Panel-/Freigabe-Buttons einen
        # Bot-Neustart überleben (timeout=None + feste custom_ids)
        try:
            self.add_view(WhitelistPanelView())
            for reqid in list(cfg.whitelist_reqs.keys()):
                self.add_view(WhitelistApprovalView(reqid))
            if cfg.whitelist_reqs:
                log.info(f"[BOT] {len(cfg.whitelist_reqs)} offene Whitelist-Anfrage(n) "
                         f"wiederhergestellt.")
        except Exception as e:
            log.error(f"[BOT] Persistente Whitelist-Views konnten nicht registriert werden: {e}")

        guild_ids = cfg.config.get("guild_ids", [])
        if not guild_ids:
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
        if cfg.has_nitrado_token() and str(cfg.config.get("service_id") or "").strip():
            await self.init_nitrado()
        else:
            log.warning("[BOT] ⚠️ Noch kein Nitrado-Token/Server eingerichtet – "
                        "führe im Discord /setup token aus.")
        # Nahezu-Echtzeit: höchstens 10s zwischen den Polls, mindestens 5s
        # (schont den FTP-Server). Größere Werte aus alten Configs werden
        # automatisch begrenzt, damit Feeds sofort nach Erscheinen posten.
        interval = int(cfg.config.get("log_poll_interval_seconds", 10))
        if interval > 10:
            log.info(f"[POLL] log_poll_interval_seconds={interval} wird auf 10s begrenzt (Echtzeit-Feeds)")
            interval = 10
        interval = max(5, interval)
        self.log_poll.change_interval(seconds=interval)
        if not self.log_poll.is_running():
            self.log_poll.start()
        if not self.economy_backup.is_running():
            self.economy_backup.start()
        status_iv = max(60, int(cfg.config.get("status_update_interval_seconds", 180)))
        self.status_update.change_interval(seconds=status_iv)
        if not self.status_update.is_running():
            self.status_update.start()
        if not self.restart_scheduler.is_running():
            self.restart_scheduler.start()
        if not announcement_scheduler.is_running():
            announcement_scheduler.start()

    async def init_nitrado(self, force: bool = False):
        """Initialisiert NitradoAPI + FTPManager + ShopManager aus der Config.
        force=True (für /setup token) ersetzt bestehende Instanzen – die alte
        aiohttp-Session wird dabei sauber geschlossen."""
        if force and self.nitrado is not None:
            try:
                await self.nitrado.close()
            except Exception:
                pass
            self.nitrado = None
        if force:
            self.ftp = None

        if self.nitrado is None:
            self.nitrado = NitradoAPI(
                token=cfg.config["nitrado_token"],
                service_id=str(cfg.config.get("service_id") or ""),
                base=cfg.config.get("nitrado_api_base", "https://api.nitrado.net"),
            )
        if self.ftp is None and all(str(cfg.config.get(k) or "").strip()
                                    for k in ("ftp_host", "ftp_user", "ftp_password")):
            self.ftp = FTPManager(
                host=cfg.config["ftp_host"],
                port=cfg.config.get("ftp_port", 21),
                user=cfg.config["ftp_user"],
                password=cfg.config["ftp_password"],
            )
            try:
                await self._auto_discover()
            except Exception as e:
                # FTP gerade nicht erreichbar → Init nicht abbrechen;
                # Discovery kann später per /ftp_scan nachgeholt werden
                log.warning(f"[FTP] Auto-Discovery fehlgeschlagen: {e}")
        # Shop-/Delivery-Manager initialisieren (braucht FTP + Nitrado)
        if self.shop is None and self.ftp is not None:
            self.shop = ShopManager(self)

    async def _auto_discover(self):
        """Sucht automatisch nach DayZ-Log-Verzeichnissen via FTP."""
        log.info("[FTP] Starte Auto-Discovery der Log-Verzeichnisse...")
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(
            None,
            functools.partial(self.ftp.discover_paths,
                              cfg.config.get("map_name", "ChernarusPlus"))
        )

        changed = False
        if "log_dir" in found and not cfg.config.get("ftp_log_dir"):
            cfg.config["ftp_log_dir"] = found["log_dir"]
            changed = True
        if "ban_file" in found and not cfg.config.get("ftp_ban_file"):
            cfg.config["ftp_ban_file"] = found["ban_file"]
            changed = True
        if "mission_dir" in found and not cfg.config.get("ftp_mission_dir"):
            cfg.config["ftp_mission_dir"] = found["mission_dir"]
            changed = True
        if "cfg_effect_area" in found and not cfg.config.get("cfg_effect_area_path"):
            cfg.config["cfg_effect_area_path"] = found["cfg_effect_area"]
            changed = True
        if changed:
            cfg.save_config()
            log.info(f"[FTP] 💾 config.json aktualisiert: log_dir={cfg.config.get('ftp_log_dir')}, "
                     f"ban_file={cfg.config.get('ftp_ban_file')}, "
                     f"cfgEffectArea={cfg.config.get('cfg_effect_area_path')}")

        # Selbstheilung: Der konfigurierte cfgEffectArea-Pfad zeigt auf einen Ordner,
        # den es auf dem FTP gar nicht gibt (z.B. Chernarus-Pfad, obwohl der Server
        # Sakhal läuft) → Shop-Käufe landen sonst in einer Datei, die der Server nie
        # liest, und spawnen nie. Auf den tatsächlich gefundenen Ordner korrigieren.
        configured   = str(cfg.config.get("cfg_effect_area_path") or "")
        found_effect = found.get("cfg_effect_area")
        if configured and found_effect and configured != found_effect:
            parent  = configured.rsplit("/", 1)[0] or "/"
            entries = await loop.run_in_executor(None, self.ftp.list_dir, parent)
            if not entries:
                log.warning(f"[FTP] ⚠️ Konfigurierter cfg_effect_area_path existiert nicht "
                            f"auf dem FTP ({configured}) – korrigiert auf {found_effect}")
                cfg.config["cfg_effect_area_path"] = found_effect
                if found.get("mission_dir"):
                    cfg.config["ftp_mission_dir"] = found["mission_dir"]
                cfg.save_config()

    @tasks.loop(seconds=10)
    async def log_poll(self):
        if not self.ftp:
            return
        log_dir = cfg.config.get("ftp_log_dir")
        if not log_dir:
            # Discovery beim Start fehlgeschlagen oder noch nicht gelaufen →
            # automatisch erneut versuchen (alle 120s), sonst würden nie
            # Kills/Builds/Damage gepostet, bis jemand /ftp_scan ausführt
            now = time.time()
            if now - self._discover_retry_ts < 120:
                return
            self._discover_retry_ts = now
            try:
                await self._auto_discover()
            except Exception as e:
                log.warning(f"[FTP] Auto-Discovery-Retry fehlgeschlagen: {e}")
            log_dir = cfg.config.get("ftp_log_dir")
            if not log_dir:
                return
        try:
            loop = asyncio.get_running_loop()
            adm_files = await loop.run_in_executor(None, self.ftp.list_adm_files, log_dir)
            if not adm_files:
                await self._check_ftp_health()
                return

            latest = adm_files[-1]
            state = cfg.log_state.get("current")
            if state is None:
                # Erststart ohne gespeicherten Offset: Alt-Events NICHT in die
                # Feeds nachposten, sondern ab dem aktuellen Dateiende weiterlesen
                size_now = await loop.run_in_executor(None, self.ftp.file_size, latest)
                cfg.log_state["current"] = {"file": latest, "offset": int(size_now or 0)}
                cfg.log_state["last_poll_ts"] = time.time()
                cfg.save_log_state()
                log.info(f"[POLL] Erststart – überspringe Alt-Events, Offset={int(size_now or 0)} ({latest})")
                return

            # Offline-Lücke erkennen: War der Bot (oder das FTP) länger weg als
            # max_backlog_minutes, die aufgelaufenen Alt-Events NICHT nachposten –
            # sonst flutet der Bot die Feeds mit stundenalten Embeds.
            # last_poll_ts fehlt bei Updates von älteren Versionen → Lücke unbekannt
            # → sicherheitshalber ebenfalls überspringen (verliert max. 1 Poll-Zyklus).
            now = time.time()
            last_poll = float(cfg.log_state.get("last_poll_ts") or 0)
            backlog_limit = max(1, int(cfg.config.get("max_backlog_minutes", 10))) * 60
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
                        None, self.ftp.read_from_offset, old_file, state.get("offset", 0)
                    )
                    if old_tail:
                        events.extend(self.parser.parse_lines(old_tail))
                        log.info(f"[POLL] {len(events)} Events aus dem Rest der alten Datei {old_file}")
                state = {"file": latest, "offset": 0}

            current_size = await loop.run_in_executor(None, self.ftp.file_size, latest)
            if current_size and state.get("offset", 0) > current_size:
                # Gleiche Datei, aber geschrumpft: Server hat die ADM beim Neustart geleert
                log.info(f"[POLL] Offset {state.get('offset', 0)} > Dateigröße {current_size} – Neustart (Truncation) erkannt")
                restart_detected = True
                state = {"file": latest, "offset": 0}

            if self.shop and (restart_detected or self.shop.cleanup_retry_needed):
                # Offene Käufe ausliefern; nach FTP-Fehler automatisch erneut versuchen.
                # Bei frisch erkanntem Neustart bleiben die Einträge in der Datei,
                # bis der Server per A2S wieder online ist (Mission-Load fertig),
                # und werden dann sofort entfernt
                self.shop.spawn_cleanup(delayed=restart_detected)

            if restart_detected:
                # Server-Neustart wirft alle Spieler → offene Spielzeit-Sitzungen beenden
                await loop.run_in_executor(None, db.close_all_sessions)

            if skip_backlog:
                # Fast-Forward ans aktuelle Dateiende – nichts nachposten
                size_now = current_size or await loop.run_in_executor(None, self.ftp.file_size, latest)
                if not size_now:
                    # Größe nicht ermittelbar (FTP-Fehler?) → nächsten Zyklus abwarten,
                    # last_poll_ts NICHT aktualisieren, damit der Skip erneut greift
                    await self._check_ftp_health()
                    return
                state = {"file": latest, "offset": int(size_now)}
                cfg.log_state["current"] = state
                cfg.log_state["last_poll_ts"] = now
                cfg.save_log_state()
                await loop.run_in_executor(None, db.close_all_sessions)
                mins = int(gap // 60) if gap >= 0 else 0
                log.info(f"[POLL] Bot war {mins} Min offline – überspringe Alt-Events, Offset={state['offset']} ({latest})")
                if gap >= 0:
                    info = discord.Embed(
                        title="⏭️ Alte Log-Events übersprungen",
                        description=(f"Der Bot war ca. **{mins} Minuten** offline. Log-Events aus "
                                     f"dieser Zeit werden nicht nachgepostet, um die Feeds nicht zu "
                                     f"fluten (Grenze: `max_backlog_minutes` in config.json)."),
                        color=0x95A5A6)
                    await _post_feed(None, "adminlog", info)
                await self._check_ftp_health()
                return

            content, new_offset = await loop.run_in_executor(
                None, self.ftp.read_from_offset, latest, state["offset"]
            )
            if content:
                state["offset"] = new_offset
                events.extend(self.parser.parse_lines(content))

            # Zustand auch bei reiner Rotation (ohne neuen Inhalt) speichern
            cfg.log_state["current"] = state
            cfg.log_state["last_poll_ts"] = now
            cfg.save_log_state()

            if events:
                log.info(f"[POLL] {len(events)} neue Events aus {latest}")
                # Rate-Limit-Schutz: pro Zyklus höchstens N Events posten
                cap = max(1, int(cfg.config.get("max_events_per_cycle", 30)))
                if len(events) > cap:
                    log.warning(f"[POLL] {len(events)} Events in einem Zyklus – "
                                f"poste nur die neuesten {cap} (max_events_per_cycle)")
                    events = events[-cap:]
                for ev in events:
                    await self._dispatch(ev)

            # Zonen-Pings: frisch getrackte Positionen gegen /zone-Zonen prüfen
            await self._check_zones()
            # Spielzeit-Belohnung für offene Sitzungen gutschreiben
            await self._credit_playtime()
            await self._check_ftp_health()
        except Exception as e:
            log.error(f"[POLL] Fehler: {e}")
            await self._check_ftp_health()

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

    # ── Auto-Status-Embed (eine Nachricht pro Guild, wird editiert) ──
    @tasks.loop(seconds=180)
    async def status_update(self):
        # tasks.loop stoppt bei unbehandelten Exceptions dauerhaft → alles fangen
        try:
            await self._status_update_once()
        except Exception as e:
            log.error(f"[STATUS] Fehler: {e}")

    async def _status_update_once(self):
        ip = str(cfg.config.get("server_ip") or "").split(":")[0].strip()
        qport = int(cfg.config.get("query_port", 0) or 0)
        if not ip or not qport:
            return
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, a2s_query, ip, qport)
        if info:
            if self._online_since is None:
                self._online_since = time.time()
        else:
            self._online_since = None
        embed = self._build_status_embed(info)
        for gid_str in list(cfg.guilds.keys()):
            ch_id = cfg.get_channel(int(gid_str), "status")
            if not ch_id:
                continue
            ch = await self._resolve_channel(int(ch_id))
            if not ch:
                continue
            msg = None
            msg_id = cfg.guilds.get(gid_str, {}).get("status_message_id")
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
                    cfg.guilds.setdefault(gid_str, {})["status_message_id"] = msg.id
                    cfg.save_guilds()
            except Exception as e:
                log.error(f"[STATUS] Guild {gid_str}: {e}")

    @status_update.before_loop
    async def _before_status(self):
        await self.wait_until_ready()

    def _build_status_embed(self, info: Optional[Dict]) -> discord.Embed:
        if info:
            e = discord.Embed(title="🟢 Server ONLINE", color=0x2ECC71)
            e.add_field(name="Server", value=str(info.get("name") or "?"), inline=False)
            e.add_field(name="👥 Spieler",
                        value=f"{info.get('players', '?')} / {info.get('max_players', '?')}",
                        inline=True)
            e.add_field(name="🗺️ Map", value=str(info.get("map") or "?"), inline=True)
            if self._online_since:
                h, m = divmod(int((time.time() - self._online_since) // 60), 60)
                e.add_field(name="⏱️ Online seit (Bot-Sicht)",
                            value=f"{h} Std {m} Min", inline=True)
        else:
            e = discord.Embed(
                title="🔴 Server OFFLINE",
                description=("Keine Antwort auf die A2S-Abfrage – Server ist aus, "
                             "startet gerade oder der Query-Port stimmt nicht."),
                color=0xE74C3C)
        nxt = self._next_scheduled_restart()
        if nxt:
            e.add_field(name="⏰ Nächster Auto-Restart", value=f"<t:{int(nxt)}:R>", inline=True)
        e.set_footer(text="Auto-Status · aktualisiert sich automatisch")
        e.timestamp = datetime.now(timezone.utc)
        return e

    # ── Zonen-Pings (/zone create): Spieler in der Zone ───────
    async def _check_zones(self):
        """Bewertet frisch getrackte Spieler-Positionen gegen die konfigurierten
        Zonen und pingt WIEDERHOLT (alle zone_ping_cooldown_seconds, Default 5 Min),
        solange sich ein Spieler in der Zone befindet – auch mehrfach für denselben
        Spieler. Allowlist-Spieler werden nie gemeldet.
        Wird pro Poll-Zyklus aufgerufen; fängt eigene Fehler selbst ab, damit
        der Poll-Zyklus (Spielzeit-Gutschrift etc.) nie daran scheitert."""
        try:
            zones = [z for z in (cfg.config.get("zones") or [])
                     if isinstance(z, dict) and z.get("name")]
            if not zones:
                return
            # Zustände entfernter Zonen entsorgen
            zone_keys = {str(z["name"]).strip().lower() for z in zones}
            self._zone_last_ping = {k: v for k, v in self._zone_last_ping.items()
                                    if k[0] in zone_keys}
            cooldown = max(0, int(cfg.config.get("zone_ping_cooldown_seconds", 300)))
            now = time.time()
            for pname, info in list(DayZLogParser.player_positions.items()):
                # Nur NEU eingetroffene Positions-Samples bewerten – alte Daten
                # dürfen nach Zonen-Änderungen keine nachträglichen Pings auslösen
                last_seen = str(info.get("last_seen") or "")
                if self._zone_pos_seen.get(pname) == last_seen:
                    continue
                self._zone_pos_seen[pname] = last_seen
                parts = [p.strip() for p in str(info.get("position") or "").split(",")]
                if len(parts) < 2:
                    continue
                try:
                    px, pz = float(parts[0]), float(parts[1])   # ADM pos = <Ost, Nord, Höhe>
                except ValueError:
                    continue
                for zone in zones:
                    try:
                        zx = float(zone.get("x", 0.0))
                        zz = float(zone.get("z", 0.0))
                        zr = float(zone.get("radius", 0.0))
                    except (TypeError, ValueError):
                        continue
                    zkey = (str(zone["name"]).strip().lower(), pname)
                    inside = (px - zx) ** 2 + (pz - zz) ** 2 <= zr * zr
                    if not inside:
                        continue
                    if _player_in_allowlist(zone, pname):
                        continue     # Allowlist: nie pingen
                    if now - self._zone_last_ping.get(zkey, 0.0) < cooldown:
                        continue     # Wiederhol-Intervall noch nicht abgelaufen
                    self._zone_last_ping[zkey] = now
                    await self._post_zone_ping(zone, pname, info)
        except Exception as e:
            log.error(f"[ZONE] Zonen-Prüfung fehlgeschlagen: {e}")

    async def _post_zone_ping(self, zone: Dict, player: str, info: Dict):
        e = discord.Embed(
            title="🛡️ • Ping On Detection",
            description=f"**{player}** was located within the zone **{zone['name']}**.",
            color=0x9B59B6)
        loc = _location_field_value(info.get("position"))
        if loc:
            e.add_field(name="📍 • Player Location", value=loc, inline=False)
        e.add_field(name="🎯 Zone",
                    value=f"`{zone.get('x')}, {zone.get('z')}` · Radius **{zone.get('radius')} m**",
                    inline=False)
        e.set_footer(text=f"Zone: {zone['name']}")
        e.timestamp = datetime.now(timezone.utc)
        role_id = zone.get("role_id")
        content = f"<@&{int(role_id)}>" if role_id else None
        gid = int(zone["guild_id"]) if zone.get("guild_id") else None
        zone_ch = zone.get("channel_id")
        if zone_ch:
            await _post_feed(gid, "zone", e, content=content, channel_id=int(zone_ch))
        elif gid is None or cfg.get_channel(gid, "zone"):
            await _post_feed(gid, "zone", e, content=content)
        else:
            await _post_feed(gid, "adminlog", e, content=content)

    # ── Geplante Neustarts (/auto restart) ────────────────────
    def _next_scheduled_restart(self) -> Optional[float]:
        """Nächster geplanter Restart-Zeitpunkt (lokale Serverzeit des Bots)."""
        sched = cfg.config.get("auto_restart_schedule") or {}
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
        nxt = self._next_scheduled_restart()
        if nxt is None:
            if self._restart_announced:
                self._restart_announced.clear()
            return
        remaining = nxt - time.time()
        # Ankündigungen 15/5/1 Minuten vorher (45s-Fenster > 30s-Loop-Takt)
        for mins in (15, 5, 1):
            key = (int(nxt), mins)
            if (mins * 60 - 45) < remaining <= mins * 60 and key not in self._restart_announced:
                self._restart_announced.add(key)
                e = discord.Embed(
                    title=f"🔄 Server-Neustart in {mins} Minute{'n' if mins != 1 else ''}!",
                    description=(f"Geplanter Neustart um <t:{int(nxt)}:t> Uhr – "
                                 f"bitte sichere Position und Loot."),
                    color=0xE67E22 if mins <= 5 else 0xF1C40F)
                await self._post_restart_feed(e)
        # Restart auslösen
        key0 = (int(nxt), 0)
        if remaining <= 30 and key0 not in self._restart_announced:
            self._restart_announced.add(key0)
            try:
                ok, msg = await self.nitrado.restart()
            except Exception as ex:
                ok, msg = False, str(ex)
            log.info(f"[AUTO-RESTART] Geplanter Neustart ausgelöst: ok={ok} – {msg}")
            e = discord.Embed(
                title="🔄 Server wird jetzt neu gestartet" if ok
                      else "❌ Geplanter Neustart fehlgeschlagen",
                description=("Der geplante Neustart wurde über die Nitrado-API ausgelöst."
                             if ok else f"Nitrado-API-Fehler: {msg}"),
                color=0x2ECC71 if ok else 0xE74C3C)
            await self._post_restart_feed(e)
        # Alte Ankündigungs-Marker aufräumen
        cutoff = time.time() - 3600
        self._restart_announced = {k for k in self._restart_announced if k[0] > cutoff}

    @restart_scheduler.before_loop
    async def _before_restart_scheduler(self):
        await self.wait_until_ready()

    async def _post_restart_feed(self, embed: discord.Embed):
        """Postet in den restart-Feed; ohne konfigurierten Channel → adminlog."""
        for gid_str in cfg.guilds:
            gid = int(gid_str)
            lt = "restart" if cfg.get_channel(gid, "restart") else "adminlog"
            await _post_feed(gid, lt, embed)

    async def _try_refresh_ftp_credentials(self) -> bool:
        """Selbstheilung bei FTP-Dauerausfall: Zugangsdaten frisch über den
        Nitrado-Token holen und den FTPManager ersetzen, falls Nitrado sie
        geändert hat (z.B. Passwort-Rotation). True = neue Daten übernommen."""
        if not self.nitrado:
            return False
        try:
            info = await self.nitrado.get_info()
        except Exception:
            return False
        if not info:
            return False
        creds = NitradoAPI.extract_ftp_credentials(info)
        if not creds:
            return False
        changed = (creds["host"] != cfg.config.get("ftp_host")
                   or creds["user"] != cfg.config.get("ftp_user")
                   or creds["password"] != cfg.config.get("ftp_password")
                   or int(creds["port"]) != int(cfg.config.get("ftp_port") or 21))
        if not changed:
            return False
        cfg.config["ftp_host"]     = creds["host"]
        cfg.config["ftp_port"]     = creds["port"]
        cfg.config["ftp_user"]     = creds["user"]
        cfg.config["ftp_password"] = creds["password"]
        cfg.save_config()
        self.ftp = FTPManager(host=creds["host"], port=creds["port"],
                              user=creds["user"], password=creds["password"])
        log.info("[NITRADO] 🔄 FTP-Zugangsdaten über die API erneuert – "
                 "Verbindung wird mit den neuen Daten aufgebaut.")
        return True

    async def _check_ftp_health(self):
        """Warnt im Adminlog-Feed, wenn das FTP-Polling dauerhaft fehlschlägt
        (Passwort geändert, Nitrado-Wartung), und meldet die Erholung.
        Versucht vorher, die FTP-Zugangsdaten über den Nitrado-Token zu erneuern."""
        if not self.ftp:
            return
        fails     = self.ftp.consecutive_failures
        threshold = max(1, int(cfg.config.get("ftp_fail_warn_cycles", 10)))
        now = time.time()
        if fails >= threshold:
            if now - self._ftp_warned_ts >= 1800:   # höchstens alle 30 Min erneut warnen
                self._ftp_warned_ts   = now
                if await self._try_refresh_ftp_credentials():
                    # Zugangsdaten waren veraltet → mit den neuen weitermachen,
                    # keine Ausfall-Warnung nötig
                    self._ftp_warn_active = False
                    embed = discord.Embed(
                        title="🔄 FTP-Zugang automatisch erneuert",
                        description=("Die FTP-Zugriffe schlugen wiederholt fehl – der Bot "
                                     "hat die Zugangsdaten über den Nitrado-Token neu "
                                     "geholt und die Verbindung neu aufgebaut."),
                        color=0x2ECC71)
                    await _post_feed(None, "adminlog", embed)
                    return
                self._ftp_warn_active = True
                embed = discord.Embed(
                    title="🚨 FTP-Verbindung gestört",
                    description=(f"**{fails} FTP-Zugriffe in Folge fehlgeschlagen** "
                                 f"(Host `{cfg.config.get('ftp_host')}`).\n"
                                 f"Log-Feeds und Shop-Lieferungen sind unterbrochen!\n"
                                 f"Mögliche Ursachen: FTP-Passwort geändert, Nitrado-Wartung.\n"
                                 f"Letzter Fehler: `{self.ftp.last_error or 'unbekannt'}`"),
                    color=0xE74C3C)
                await _post_feed(None, "adminlog", embed)
        elif fails == 0 and self._ftp_warn_active:
            self._ftp_warn_active = False
            self._ftp_warned_ts   = 0.0
            embed = discord.Embed(
                title="✅ FTP-Verbindung wiederhergestellt",
                description="Der FTP-Zugriff funktioniert wieder – die Feeds laufen normal weiter.",
                color=0x2ECC71)
            await _post_feed(None, "adminlog", embed)

    async def _resolve_channel(self, channel_id: int):
        ch = self.get_channel(channel_id)
        if ch is not None:
            return ch
        try:
            return await self.fetch_channel(channel_id)
        except Exception as e:
            log.debug(f"[DISPATCH] fetch_channel({channel_id}) fehlgeschlagen: {e}")
            return None

    async def _dispatch(self, ev: Dict):
        log_type = DayZLogParser.EVENT_TO_LOG.get(ev["type"])
        if not log_type:
            return
        # Für die Dashboard-Karte/Event-Liste festhalten (mit Koordinaten aus
        # dem Event bzw. der letzten bekannten Spielerposition). Fehler hier
        # dürfen den Log-Dispatch niemals stören.
        try:
            _ev_record(ev, self.parser.player_positions)
        except Exception:
            pass
        # Kill-Statistik, Sessions, Kill-Belohnung & Bounties verarbeiten
        rewards = await self._process_event_rewards(ev)
        embed = EmbedBuilder.build(ev)
        if not embed:
            return
        for gid_str in cfg.guilds:
            ch_id = cfg.get_channel(int(gid_str), log_type)
            if not ch_id:
                continue
            send_embed = embed
            reward_line = rewards.get(int(gid_str))
            if reward_line:
                send_embed = embed.copy()
                send_embed.add_field(name="💰 Belohnung", value=reward_line, inline=False)
            ch = await self._resolve_channel(int(ch_id))
            if ch:
                try:
                    await ch.send(embed=send_embed)
                except discord.Forbidden:
                    log.warning(f"[DISPATCH] Keine Rechte in Channel {ch_id} (Guild {gid_str})")
                except Exception as e:
                    log.error(f"[DISPATCH] Fehler in Guild {gid_str}: {e}")
            else:
                log.warning(f"[DISPATCH] Channel {ch_id} in Guild {gid_str} nicht gefunden")

    async def _process_event_rewards(self, ev: Dict) -> Dict[int, str]:
        """Nebenwirkungen eines Log-Events: Kill-Statistik schreiben, Spielzeit-
        Sitzungen öffnen/schließen, Kill-Belohnung und Kopfgelder an verlinkte
        Spieler auszahlen. Gibt pro Guild eine Belohnungszeile fürs Embed zurück."""
        out: Dict[int, str] = {}
        loop = asyncio.get_running_loop()
        t = ev["type"]
        try:
            if t == "connect":
                pid = ev.get("player_id")
                pid = pid if pid and pid != "Unbekannt" else None
                await loop.run_in_executor(None, db.open_session, ev["player"], pid)
                if pid:
                    await loop.run_in_executor(None, db.update_link_id, ev["player"], pid)

            elif t == "disconnect":
                await loop.run_in_executor(None, db.close_session, ev["player"])

            elif t == "kill_pvp":
                killer = ev.get("killer") or ""
                victim = ev.get("victim") or ""
                await loop.run_in_executor(
                    None, db.record_kill, killer, ev.get("killer_id"),
                    victim, ev.get("victim_id"), ev.get("weapon"), ev.get("distance"))
                for nm, key in ((killer, "killer_id"), (victim, "victim_id")):
                    pid = ev.get(key)
                    if nm and pid and pid != "Unbekannt":
                        await loop.run_in_executor(None, db.update_link_id, nm, pid)
                if killer and victim and killer.lower() != victim.lower():
                    reward = max(0, int(cfg.config.get("kill_reward", 0)))
                    links = await loop.run_in_executor(None, db.links_for_name, killer)
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

    async def _credit_playtime(self):
        """Schreibt verlinkten Spielern volle Spielzeit-Blöcke gut
        (playtime_reward: amount pro interval_minutes, z.B. 500 pro 30 Min)."""
        conf = cfg.config.get("playtime_reward") or {}
        amount = max(0, int(conf.get("amount", 0)))
        if amount <= 0:
            return
        interval = max(1, int(conf.get("interval_minutes", 30))) * 60
        loop = asyncio.get_running_loop()
        try:
            # Verpasste Connect-Events abfangen: verlinkte Spieler, die laut Log
            # gerade aktiv sind, aber keine offene Sitzung haben → Sitzung öffnen
            positions = dict(DayZLogParser.player_positions)
            await loop.run_in_executor(None, db.sync_sessions_from_positions, positions, 300)
            due = await loop.run_in_executor(None, db.playtime_credits_due, interval)
            for entry in due:
                links = await loop.run_in_executor(None, db.links_for_name, entry["name"])
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
    if _member_has_role_ids(interaction.user, cfg.config.get("admin_role_ids", [])):
        return True
    role_name = cfg.config.get("admin_role_name", "")
    if role_name and any(r.name == role_name for r in interaction.user.roles):
        return True
    return False

def _is_economy_admin(interaction: discord.Interaction) -> bool:
    """Economy-Admin = economy_admin_role_ids ODER voller Admin."""
    if _is_admin(interaction):
        return True
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return _member_has_role_ids(interaction.user, cfg.config.get("economy_admin_role_ids", []))

async def _deny(interaction: discord.Interaction):
    msg = ("❌ No permission. You need one of the configured admin roles "
           "(`admin_role_ids` in config.json) or Administrator rights.")
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


async def _require_nitrado(interaction: discord.Interaction,
                           need_ftp: bool = False) -> bool:
    """True, wenn die Nitrado-Anbindung (und optional FTP) einsatzbereit ist.
    Sonst ephemere Hinweis-Meldung → Befehl mit `return` abbrechen."""
    if bot.nitrado is not None and (not need_ftp or bot.ftp is not None):
        return True
    msg = ("❌ Nitrado ist noch nicht eingerichtet.\n"
           "Führe zuerst `/setup token <dein-nitrado-token>` aus und wähle "
           "deinen Server im Dropdown aus.")
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return False


# ══════════════════════════════════════════════════════════════
#  /setup – Alle Log-Channels konfigurieren
# ══════════════════════════════════════════════════════════════
setup_group = app_commands.Group(name="setup", description="⚙️ Log-Channels konfigurieren")

@setup_group.command(name="feeds", description="⚙️ Feed-Channel für einen Log-Typ festlegen")
@app_commands.describe(feed="Welcher Feed?", channel="Ziel-Channel für diesen Feed")
@app_commands.choices(feed=[
    app_commands.Choice(name=LOG_TYPES[k][:100], value=k) for k in LOG_TYPES
])
async def setup_feeds(interaction: discord.Interaction,
                      feed: app_commands.Choice[str],
                      channel: discord.TextChannel):
    if not _is_admin(interaction):
        return await _deny(interaction)
    cfg.set_channel(interaction.guild_id, feed.value, channel.id)
    await interaction.response.send_message(
        f"✅ **{LOG_TYPES[feed.value]}** → {channel.mention}", ephemeral=True)


@setup_group.command(name="uebersicht", description="📋 Zeigt alle konfigurierten Channels")
async def setup_overview(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    guild_cfg = cfg.guilds.get(str(interaction.guild_id), {})
    embed = discord.Embed(title="📋 Aktuelle Channel-Konfiguration", color=0x5865F2)
    for lt, desc in LOG_TYPES.items():
        ch_id = guild_cfg.get(lt)
        embed.add_field(
            name=desc,
            value=f"<#{ch_id}>" if ch_id else "❌ Nicht gesetzt",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def _finish_token_setup(token: str, service_id: str,
                              service: Dict) -> discord.Embed:
    """Wendet Token + Server-Auswahl aus /setup token an: speichert beides,
    erkennt FTP-Zugang & aktuelle Karte, initialisiert Nitrado/FTP/Shop neu
    und gibt ein Ergebnis-Embed zurück."""
    old_service = str(cfg.config.get("service_id") or "").strip()
    cfg.config["nitrado_token"] = token
    cfg.config["service_id"]    = service_id
    if old_service and old_service != service_id:
        # Server-Wechsel: per-Server-Caches leeren, sonst zeigen Pfade,
        # Server-IP und Log-Offset noch auf den alten Server
        for k in ("ftp_log_dir", "ftp_ban_file", "ftp_profile_dir",
                  "ftp_mission_dir", "cfg_effect_area_path", "server_ip"):
            cfg.config[k] = ""
        cfg.log_state.pop("current", None)
        cfg.save_log_state()
        log.info(f"[SETUP] Server-Wechsel {old_service} → {service_id}: "
                 f"FTP-Pfade und Log-Position zurückgesetzt.")

    api = NitradoAPI(token=token, service_id=service_id,
                     base=cfg.config.get("nitrado_api_base", "https://api.nitrado.net"))
    try:
        info = await api.get_info()
    finally:
        await api.close()

    warnings = []
    if info:
        _apply_gameserver_info(info)
    else:
        warnings.append("⚠️ Gameserver-Infos konnten nicht geladen werden "
                        "(Nitrado-API-Fehler) – FTP/Karte nicht erkannt.")
    cfg.save_config()

    # Nitrado/FTP/Shop mit den neuen Daten (neu) initialisieren –
    # inklusive FTP-Auto-Discovery der Log-Verzeichnisse
    await bot.init_nitrado(force=True)

    details  = service.get("details") or {}
    name     = details.get("name") or details.get("game") or f"Service {service_id}"
    ftp_host = cfg.config.get("ftp_host") or "❌ Nicht gefunden"
    log_dir  = cfg.config.get("ftp_log_dir") or "❌ Nicht gefunden"
    if not cfg.config.get("ftp_host"):
        warnings.append("⚠️ Keine FTP-Zugangsdaten gefunden – Log-Feeds und "
                        "Shop-Lieferung funktionieren so nicht.")

    embed = discord.Embed(
        title="✅ Nitrado-Server eingerichtet",
        description=f"Der Bot arbeitet jetzt mit **{name}**.",
        color=0x2ECC71 if not warnings else 0xE67E22)
    embed.add_field(name="Service-ID",      value=f"`{service_id}`", inline=True)
    embed.add_field(name="Aktive Karte",    value=cfg.config.get("map_name", "–"), inline=True)
    embed.add_field(name="FTP-Host",        value=f"`{ftp_host}`",   inline=False)
    embed.add_field(name="Log-Verzeichnis", value=f"`{log_dir}`",    inline=False)
    if warnings:
        embed.add_field(name="Hinweise", value="\n".join(warnings), inline=False)
    embed.set_footer(text="Alle Werte wurden in config.json gespeichert – "
                          "beim nächsten Start ist kein /setup token nötig.")
    return embed


class NitradoServerSelectView(discord.ui.View):
    """Server-Auswahl für /setup token: Dropdown der über den Token
    verfügbaren Nitrado-Server + Bestätigen-Button."""

    def __init__(self, interaction: discord.Interaction, token: str,
                 services: List[Dict]):
        super().__init__(timeout=180)
        self.author_id = interaction.user.id
        self.token     = token
        self.selected: Optional[str] = None
        self._services = {str(s.get("id")): s for s in services}
        options = []
        for s in services[:25]:   # Discord erlaubt max. 25 Optionen pro Dropdown
            details = s.get("details") or {}
            label = str(details.get("name") or details.get("game")
                        or f"Service {s.get('id')}")[:100]
            desc  = " · ".join(x for x in (str(details.get("game") or "")[:50],
                                           str(s.get("status") or ""),
                                           f"ID {s.get('id')}") if x)[:100]
            options.append(discord.SelectOption(label=label,
                                                value=str(s.get("id")),
                                                description=desc or None))
        self.sel_server.options = options

    async def interaction_check(self, itx: discord.Interaction) -> bool:
        if itx.user.id != self.author_id:
            await itx.response.send_message(
                "❌ Nur wer den Befehl aufgerufen hat, kann hier auswählen.", ephemeral=True)
            return False
        return True

    @discord.ui.select(placeholder="🖥️ Nitrado-Server auswählen",
                       options=[discord.SelectOption(label="wird geladen…", value="0")])
    async def sel_server(self, itx: discord.Interaction, select: discord.ui.Select):
        self.selected = select.values[0]
        await itx.response.defer()

    @discord.ui.button(label="✅ Server bestätigen", style=discord.ButtonStyle.success)
    async def confirm(self, itx: discord.Interaction, button: discord.ui.Button):
        if self.selected is None:
            return await itx.response.send_message(
                "❌ Bitte zuerst einen Server im Dropdown auswählen.", ephemeral=True)
        for child in self.children:
            child.disabled = True
        await itx.response.edit_message(
            content="🔧 Richte den Server ein (FTP-Zugang, Karte, Log-Verzeichnisse)…",
            embed=None, view=self)
        try:
            embed = await _finish_token_setup(
                self.token, self.selected, self._services.get(self.selected) or {})
        except Exception as e:
            log.error(f"[SETUP] /setup token fehlgeschlagen: {e}")
            embed = discord.Embed(
                title="❌ Einrichtung fehlgeschlagen",
                description=f"Unerwarteter Fehler: `{e}`\nBitte erneut versuchen.",
                color=0xE74C3C)
        await itx.edit_original_response(content=None, embed=embed, view=self)
        self.stop()


@setup_group.command(name="token",
                     description="🔑 Nitrado-Token setzen & Server per Dropdown auswählen")
@app_commands.describe(token="Dein Nitrado Long-Life-Token (Nitrado → Benutzereinstellungen → API-Schlüssel)")
async def setup_token(interaction: discord.Interaction, token: str):
    if not _is_admin(interaction):
        return await _deny(interaction)
    await interaction.response.defer(ephemeral=True)

    token = token.strip()
    api = NitradoAPI(token=token, service_id="",
                     base=cfg.config.get("nitrado_api_base", "https://api.nitrado.net"))
    try:
        services = await api.list_services()
    finally:
        await api.close()

    gameservers = [s for s in services
                   if str(s.get("type", "")).lower() == "gameserver"]
    if not gameservers:
        return await interaction.followup.send(
            "❌ Über diesen Token wurden keine Gameserver gefunden.\n"
            "Prüfe, ob der Token korrekt kopiert wurde "
            "(Nitrado → Benutzereinstellungen → API-Schlüssel, Long-Life-Token "
            "mit Berechtigung für deine Services).", ephemeral=True)

    desc = (f"Token akzeptiert – **{len(gameservers)} Server** gefunden.\n"
            "Wähle im Dropdown den Server aus, mit dem der Bot arbeiten soll, "
            "und bestätige. FTP-Zugang und die aktive Karte werden dann "
            "automatisch erkannt.")
    if len(gameservers) > 25:
        desc += "\n⚠️ Es werden nur die ersten 25 Server angezeigt."
    embed = discord.Embed(title="🔑 Nitrado-Server auswählen",
                          description=desc, color=0x5865F2)
    await interaction.followup.send(
        embed=embed,
        view=NitradoServerSelectView(interaction, token, gameservers),
        ephemeral=True)


bot.tree.add_command(setup_group)


# ══════════════════════════════════════════════════════════════
#  /show_feeds – Alle Feed-Channels auf einen Blick
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="show_feeds", description="📡 Zeigt alle Feed-Channels und ihren Status")
async def cmd_show_feeds(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    guild_cfg  = cfg.guilds.get(str(interaction.guild_id), {})
    active     = {lt: ch for lt, ch in guild_cfg.items() if ch and lt in LOG_TYPES}
    inactive   = [lt for lt in LOG_TYPES if lt not in active]

    embed = discord.Embed(
        title="📡 Feed-Channel Übersicht",
        description=(
            f"**{len(active)}** Feed{'s' if len(active) != 1 else ''} aktiv  •  "
            f"**{len(inactive)}** nicht konfiguriert"
        ),
        color=0x2ECC71 if active else 0x95A5A6,
    )

    # ── Aktive Feeds ──────────────────────────────────────────
    if active:
        lines = []
        for lt, ch_id in active.items():
            desc = LOG_TYPES.get(lt, lt)
            ch   = interaction.guild.get_channel(int(ch_id)) if interaction.guild else None
            ch_mention = ch.mention if ch else f"<#{ch_id}> *(Channel nicht gefunden)*"
            lines.append(f"{desc}\n╰ {ch_mention}")
        embed.add_field(
            name="✅ Aktive Feeds",
            value="\n\n".join(lines),
            inline=False,
        )

    # ── Inaktive Feeds ────────────────────────────────────────
    if inactive:
        lines = [f"❌ {LOG_TYPES[lt]}" for lt in inactive]
        embed.add_field(
            name="⚪ Nicht konfiguriert",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text="Nutze /edit_feeds um einzelne Feeds zu ändern")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════
#  /edit_feeds – Einzelnen Feed-Channel ändern
#  Wähle den Feed-Typ per Autocomplete, dann den neuen Channel
# ══════════════════════════════════════════════════════════════
@bot.tree.command(
    name="edit_feeds",
    description="✏️ Feed-Channel ändern oder deaktivieren"
)
@app_commands.describe(
    feed="Welcher Feed soll geändert werden? (Tippe um zu filtern)",
    channel="Neuer Channel – leer lassen zum Deaktivieren"
)
async def cmd_edit_feeds(
    interaction: discord.Interaction,
    feed: str,
    channel: Optional[discord.TextChannel] = None,
):
    if not _is_admin(interaction):
        return await _deny(interaction)

    # Ungültigen Feed-Key abfangen (falls jemand manuell eingibt)
    if feed not in LOG_TYPES:
        choices = ", ".join(f"`{k}`" for k in LOG_TYPES)
        return await interaction.response.send_message(
            f"❌ Unbekannter Feed `{feed}`.\nGültige Feeds: {choices}",
            ephemeral=True,
        )

    desc = LOG_TYPES[feed]

    if channel is None:
        # Feed deaktivieren
        gid = str(interaction.guild_id)
        if gid in cfg.guilds and feed in cfg.guilds[gid]:
            del cfg.guilds[gid][feed]
            cfg.save_guilds()
        embed = discord.Embed(
            title="⚪ Feed deaktiviert",
            description=f"**{desc}**\nwird nicht mehr gepostet.",
            color=0x95A5A6,
        )
    else:
        # Feed auf neuen Channel setzen
        cfg.set_channel(interaction.guild_id, feed, channel.id)
        embed = discord.Embed(
            title="✅ Feed geändert",
            description=f"**{desc}**\n╰ {channel.mention}",
            color=0x2ECC71,
        )

    embed.set_footer(text="Nutze /show_feeds für eine Gesamtübersicht")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@cmd_edit_feeds.autocomplete("feed")
async def _feed_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Autocomplete: zeigt alle passenden Feed-Typen mit ihrem aktuellen Channel."""
    guild_cfg = cfg.guilds.get(str(interaction.guild_id), {})
    results   = []
    for lt, desc in LOG_TYPES.items():
        if current.lower() in lt.lower() or current.lower() in desc.lower() or not current:
            ch_id  = guild_cfg.get(lt)
            status = "✅" if ch_id else "❌"
            # Channel-Name im Label anzeigen wenn möglich
            ch_name = ""
            if ch_id and interaction.guild:
                ch = interaction.guild.get_channel(int(ch_id))
                ch_name = f" → #{ch.name}" if ch else f" → <#{ch_id}>"
            label = f"{status} {desc.strip()}{ch_name}"[:100]
            results.append(app_commands.Choice(name=label, value=lt))
    return results[:25]


# ══════════════════════════════════════════════════════════════
#  /neustart – Server Neustart
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="neustart", description="🔄 Startet den DayZ Server neu")
async def cmd_neustart(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction):
        return
    await interaction.response.defer()
    ok, msg = await bot.nitrado.restart()
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
@bot.tree.command(name="stoppen", description="⏹️ Stoppt den DayZ Server")
async def cmd_stoppen(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction):
        return
    await interaction.response.defer()
    ok, msg = await bot.nitrado.stop()
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
async def cmd_status(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction):
        return
    await interaction.response.defer()

    # ── 1. Nitrado API (parallel zum A2S-Ping) ────────────────
    loop       = asyncio.get_running_loop()
    nitrado_task = loop.run_in_executor(None, lambda: None)   # Placeholder
    info = await bot.nitrado.get_info()

    # ── 2. Direkter A2S UDP-Ping ─────────────────────────────
    srv_ip    = cfg.config.get("server_ip",  "")
    qport     = int(cfg.config.get("query_port",  2302))
    rcon_port = int(cfg.config.get("rcon_port",   2310))

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
    embed.set_footer(text=f"Quellen: {', '.join(src) or '–'} | Service ID: {cfg.config.get('service_id','–')}")
    await interaction.followup.send(embed=embed)


# ══════════════════════════════════════════════════════════════
#  /auto – Geplante automatische Server-Neustarts
# ══════════════════════════════════════════════════════════════
auto_group = app_commands.Group(name="auto", description="⏰ Automatische Server-Neustarts planen")


class AutoRestartView(discord.ui.View):
    """Uhrzeit-Auswahl für /auto restart: Stunde (0–23) + Minute (:00/:30).
    Zwei Dropdowns, weil Discord max. 25 Optionen pro Select erlaubt."""

    def __init__(self, interaction: discord.Interaction, interval_hours: int):
        super().__init__(timeout=180)
        self.author_id      = interaction.user.id
        self.interval_hours = interval_hours
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
        first = f"{self.hour:02d}:{self.minute:02d}"
        cfg.config["auto_restart_schedule"] = {
            "enabled": True, "first_time": first, "interval_hours": self.interval_hours}
        cfg.save_config()
        bot._restart_announced.clear()
        nxt = bot._next_scheduled_restart()
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
@app_commands.describe(intervall="Abstand in Stunden, z.B. 2 = alle 2 Stunden (1–24)")
async def auto_restart(interaction: discord.Interaction,
                       intervall: app_commands.Range[int, 1, 24]):
    if not _is_admin(interaction):
        return await _deny(interaction)
    view = AutoRestartView(interaction, int(intervall))
    e = discord.Embed(
        title="⏰ Auto-Restart einrichten",
        description=(f"Intervall: **alle {int(intervall)} Stunde(n)**\n\n"
                     f"Wähle unten die Uhrzeit der **ersten Ausführung** "
                     f"(danach immer im gewählten Intervall) und bestätige."),
        color=0x5865F2)
    await interaction.response.send_message(embed=e, view=view, ephemeral=True)


@auto_group.command(name="off", description="⏹️ Deaktiviert die geplanten Neustarts")
async def auto_off(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    sched = dict(cfg.config.get("auto_restart_schedule") or {})
    was_on = bool(sched.get("enabled"))
    sched["enabled"] = False
    cfg.config["auto_restart_schedule"] = sched
    cfg.save_config()
    bot._restart_announced.clear()
    await interaction.response.send_message(
        "⏹️ Geplante Neustarts deaktiviert." if was_on
        else "ℹ️ Es waren keine geplanten Neustarts aktiv.", ephemeral=True)


@auto_group.command(name="status", description="📋 Zeigt den aktuellen Restart-Zeitplan")
async def auto_status(interaction: discord.Interaction):
    sched = cfg.config.get("auto_restart_schedule") or {}
    if not sched.get("enabled"):
        return await interaction.response.send_message(
            "ℹ️ Keine geplanten Neustarts aktiv. Einrichten: `/auto restart`.", ephemeral=True)
    nxt = bot._next_scheduled_restart()
    e = discord.Embed(
        title="⏰ Auto-Restart Zeitplan",
        description=(f"Startzeit: **{sched.get('first_time', '?')} Uhr** · "
                     f"Intervall: **alle {sched.get('interval_hours', '?')} Stunde(n)**\n"
                     f"Nächster Neustart: <t:{int(nxt)}:F> (<t:{int(nxt)}:R>)"),
        color=0x5865F2)
    await interaction.response.send_message(embed=e, ephemeral=True)


bot.tree.add_command(auto_group)


# ══════════════════════════════════════════════════════════════
#  /zone – Überwachte Zonen: wiederholter Ping (alle 5 Min),
#  solange ein Spieler in der Zone steht (außer Allowlist)
#  (Positionen kommen aus den ADM-Logs, Prüfung in _check_zones)
# ══════════════════════════════════════════════════════════════
zone_group = app_commands.Group(name="zone",
                                description="🛡️ Zonen-Pings verwalten (Admin)")

def _zones() -> List[Dict]:
    zs = cfg.config.get("zones")
    if not isinstance(zs, list):
        zs = []
        cfg.config["zones"] = zs
    return zs

def _find_zone(name: str) -> Optional[Dict]:
    key = name.strip().lower()
    for z in _zones():
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

def _player_in_allowlist(zone: Dict, pname: str) -> bool:
    """True, wenn der Spieler in dieser Zone ignoriert werden soll (case-insensitiv)."""
    key = (pname or "").strip().lower()
    return any(str(n).strip().lower() == key for n in _zone_allowlist(zone))

def _reset_zone_state(zone_name: str):
    """Ping-Cooldowns einer Zone verwerfen (nach remove/edit),
    damit die nächste frische Position sauber neu bewertet wird."""
    zk = zone_name.strip().lower()
    bot._zone_last_ping = {k: v for k, v in bot._zone_last_ping.items() if k[0] != zk}

def _zone_summary(z: Dict) -> str:
    role = f" · Ping: <@&{int(z['role_id'])}>" if z.get("role_id") else ""
    chan = f" · Channel: <#{int(z['channel_id'])}>" if z.get("channel_id") else ""
    return (f"Zentrum `{z.get('x')}, {z.get('z')}` (x=Ost, z=Nord) · "
            f"Radius **{z.get('radius')} m**{role}{chan}")

async def _zone_name_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    return [app_commands.Choice(name=str(z["name"]), value=str(z["name"]))
            for z in _zones()
            if isinstance(z, dict) and z.get("name") and cur in str(z["name"]).lower()][:25]

def _validate_zone_geometry(x: float, z: float, radius: float) -> Optional[str]:
    """Gibt eine Fehlermeldung zurück oder None, wenn alles ok ist."""
    if not (0.0 <= x <= 20000.0 and 0.0 <= z <= 20000.0):
        return ("❌ Koordinaten außerhalb der Map. Gib die beiden iZurvive-Zahlen "
                "als `x` (Ost) und `z` (Nord) an, z. B. `x: 4522` `z: 9638`.")
    if not (10.0 <= radius <= 10000.0):
        return "❌ Radius muss zwischen **10** und **10000** Metern liegen."
    return None


@zone_group.command(name="create",
                    description="🛡️ Zone anlegen – pingt alle 5 Min, solange ein Spieler darin steht (Admin)")
@app_commands.describe(
    x="X-Koordinate des Zentrums (iZurvive, Ost)",
    z="Z-Koordinate des Zentrums (iZurvive, Nord)",
    name="Name der Zone (frei wählbar, einmalig)",
    radius="Radius in Metern, in dem der Bot nach Spielern schaut",
    channel="Optional: Channel, in den die Warnungen dieser Zone gepostet werden",
    rolle="Optional: Rolle, die beim Ping mit @ markiert wird")
async def zone_create(interaction: discord.Interaction, x: float, z: float,
                      name: str, radius: float,
                      channel: Optional[discord.TextChannel] = None,
                      rolle: Optional[discord.Role] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    name = name.strip()
    if not name or len(name) > 60:
        return await interaction.response.send_message(
            "❌ Zonen-Name fehlt oder ist länger als 60 Zeichen.", ephemeral=True)
    if _find_zone(name):
        return await interaction.response.send_message(
            f"❌ Zone **{name}** existiert bereits – `/zone edit` zum Ändern "
            f"oder `/zone remove` zum Löschen.", ephemeral=True)
    err = _validate_zone_geometry(x, z, radius)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    zone = {
        "name":       name,
        "x":          round(float(x), 1),
        "z":          round(float(z), 1),
        "radius":     round(float(radius), 1),
        "role_id":    int(rolle.id) if rolle else None,
        "channel_id": int(channel.id) if channel else None,
        "guild_id":   int(interaction.guild_id),
    }
    _zones().append(zone)
    cfg.save_config()
    e = discord.Embed(title="🛡️ Zone angelegt",
                      description=f"**{name}**\n{_zone_summary(zone)}",
                      color=0x2ECC71)
    if not channel and not cfg.get_channel(interaction.guild_id, "zone"):
        e.add_field(name="ℹ️ Hinweis",
                    value="Kein Channel gesetzt – Pings gehen in den **adminlog**. "
                          "Gib bei `/zone create` das Feld `channel` an oder setze mit "
                          "`/setup feeds` den Feed **zone**.",
                    inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@zone_group.command(name="remove",
                    description="🗑️ Zone entfernen – dort wird nicht mehr gesucht (Admin)")
@app_commands.describe(name="Name der Zone (Autocomplete)")
async def zone_remove(interaction: discord.Interaction, name: str):
    if not _is_admin(interaction):
        return await _deny(interaction)
    zone = _find_zone(name)
    if not zone:
        return await interaction.response.send_message(
            f"❌ Keine Zone namens **{name.strip()}** gefunden – `/zone list` zeigt alle.",
            ephemeral=True)
    _zones().remove(zone)
    cfg.save_config()
    _reset_zone_state(str(zone["name"]))
    await interaction.response.send_message(
        embed=discord.Embed(
            title="🗑️ Zone entfernt",
            description=f"**{zone['name']}** wird nicht mehr überwacht.\n{_zone_summary(zone)}",
            color=0xE74C3C),
        ephemeral=True)

zone_remove.autocomplete("name")(_zone_name_autocomplete)


@zone_group.command(name="list", description="📋 Alle aktiven Zonen anzeigen (Admin)")
async def zone_list(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    zones = [z for z in _zones() if isinstance(z, dict) and z.get("name")]
    if not zones:
        return await interaction.response.send_message(
            "ℹ️ Keine Zonen angelegt. Mit `/zone create` eine Zone einrichten.",
            ephemeral=True)
    e = discord.Embed(title=f"🛡️ Aktive Zonen ({len(zones)})", color=0x3498DB)
    for z in zones[:25]:
        e.add_field(name=f"📍 {z['name']}", value=_zone_summary(z), inline=False)
    if len(zones) > 25:
        e.set_footer(text=f"… und {len(zones) - 25} weitere (Embed-Limit)")
    await interaction.response.send_message(embed=e, ephemeral=True)


@zone_group.command(name="edit",
                    description="✏️ Zone bearbeiten – nur die angegebenen Felder werden geändert (Admin)")
@app_commands.describe(
    name="Name der Zone, die bearbeitet werden soll (Autocomplete)",
    neuer_name="Optional: neuer Name der Zone",
    x="Optional: neue X-Koordinate (iZurvive, Ost)",
    z="Optional: neue Z-Koordinate (iZurvive, Nord)",
    radius="Optional: neuer Radius in Metern",
    channel="Optional: neuer Channel für die Warnungen dieser Zone",
    channel_entfernen="True = eigenen Zonen-Channel entfernen (Fallback: Feed zone/adminlog)",
    rolle="Optional: neue Rolle für den Ping",
    rolle_entfernen="True = Rollen-Ping ausschalten")
async def zone_edit(interaction: discord.Interaction, name: str,
                    neuer_name: Optional[str] = None,
                    x: Optional[float] = None, z: Optional[float] = None,
                    radius: Optional[float] = None,
                    channel: Optional[discord.TextChannel] = None,
                    channel_entfernen: bool = False,
                    rolle: Optional[discord.Role] = None,
                    rolle_entfernen: bool = False):
    if not _is_admin(interaction):
        return await _deny(interaction)
    zone = _find_zone(name)
    if not zone:
        return await interaction.response.send_message(
            f"❌ Keine Zone namens **{name.strip()}** gefunden – `/zone list` zeigt alle.",
            ephemeral=True)
    if (neuer_name is None and x is None and z is None and radius is None
            and rolle is None and not rolle_entfernen
            and channel is None and not channel_entfernen):
        return await interaction.response.send_message(
            "❌ Nichts zu ändern – mindestens ein Feld angeben "
            "(`neuer_name`, `x`, `z`, `radius`, `channel`, `channel_entfernen`, "
            "`rolle`, `rolle_entfernen`).",
            ephemeral=True)
    new_x = float(x)      if x      is not None else float(zone.get("x", 0.0))
    new_z = float(z)      if z      is not None else float(zone.get("z", 0.0))
    new_r = float(radius) if radius is not None else float(zone.get("radius", 0.0))
    err = _validate_zone_geometry(new_x, new_z, new_r)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if neuer_name is not None:
        neuer_name = neuer_name.strip()
        if not neuer_name or len(neuer_name) > 60:
            return await interaction.response.send_message(
                "❌ Neuer Name fehlt oder ist länger als 60 Zeichen.", ephemeral=True)
        existing = _find_zone(neuer_name)
        if existing is not None and existing is not zone:
            return await interaction.response.send_message(
                f"❌ Es gibt bereits eine Zone namens **{neuer_name}**.", ephemeral=True)
    old_name = str(zone["name"])
    if neuer_name is not None:
        zone["name"] = neuer_name
    zone["x"], zone["z"], zone["radius"] = round(new_x, 1), round(new_z, 1), round(new_r, 1)
    if rolle_entfernen:
        zone["role_id"] = None
    elif rolle is not None:
        zone["role_id"] = int(rolle.id)
    if channel_entfernen:
        zone["channel_id"] = None
    elif channel is not None:
        zone["channel_id"] = int(channel.id)
    cfg.save_config()
    # Alten UND neuen Zustand verwerfen: Geometrie/Name haben sich evtl. geändert,
    # die nächste frische Position bewertet die Zone komplett neu
    _reset_zone_state(old_name)
    _reset_zone_state(str(zone["name"]))
    await interaction.response.send_message(
        embed=discord.Embed(
            title="✏️ Zone aktualisiert",
            description=f"**{zone['name']}**\n{_zone_summary(zone)}",
            color=0x2ECC71),
        ephemeral=True)

zone_edit.autocomplete("name")(_zone_name_autocomplete)


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
    spieler="PlayStation-/Ingame-Name, der nicht mehr gemeldet werden soll")
async def zone_allowlist_add(interaction: discord.Interaction, zone: str, spieler: str):
    if not _is_admin(interaction):
        return await _deny(interaction)
    z = _find_zone(zone)
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
    cfg.save_config()
    await interaction.response.send_message(
        f"🙈 **{spieler}** wird in Zone **{z['name']}** ab sofort **nicht** mehr gemeldet.",
        ephemeral=True)

zone_allowlist_add.autocomplete("zone")(_zone_name_autocomplete)


@allowlist_group.command(
    name="remove",
    description="🔔 Spieler wieder melden – von der Ignorier-Liste entfernen (Admin)")
@app_commands.describe(
    zone="Name der Zone (Autocomplete)",
    spieler="Name, der wieder gemeldet werden soll")
async def zone_allowlist_remove(interaction: discord.Interaction, zone: str, spieler: str):
    if not _is_admin(interaction):
        return await _deny(interaction)
    z = _find_zone(zone)
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
    cfg.save_config()
    await interaction.response.send_message(
        f"🔔 **{matches[0]}** wird in Zone **{z['name']}** wieder gemeldet.",
        ephemeral=True)

zone_allowlist_remove.autocomplete("zone")(_zone_name_autocomplete)


@allowlist_group.command(
    name="show",
    description="📋 Ignorierte Spieler einer Zone anzeigen (Admin)")
@app_commands.describe(zone="Name der Zone (Autocomplete)")
async def zone_allowlist_show(interaction: discord.Interaction, zone: str):
    if not _is_admin(interaction):
        return await _deny(interaction)
    z = _find_zone(zone)
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


bot.tree.add_command(zone_group)


# ══════════════════════════════════════════════════════════════
#  Ban-Hilfsfunktionen (Banliste in den Nitrado-Servereinstellungen –
#  dasselbe Settings-Feld wie im Webinterface, 1 Name pro Zeile)
# ══════════════════════════════════════════════════════════════
def _find_ban_setting(settings: Dict) -> Tuple[str, str, str]:
    """Sucht das Banlisten-Setting in den Nitrado-Settings.
    Reihenfolge: Config-Override (nitrado_ban_category/nitrado_ban_key) →
    Auto-Erkennung (Key 'bans', Kategorie egal) → Fallback ('general', 'bans').
    Gibt (category, key, aktueller_wert) zurück."""
    ov_cat = str(cfg.config.get("nitrado_ban_category") or "").strip()
    ov_key = str(cfg.config.get("nitrado_ban_key") or "").strip()
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

async def _read_banlist() -> Tuple[List[str], str, str]:
    """Liest die Banliste aus den Nitrado-Servereinstellungen.
    Gibt (namen, category, key) zurück. Wirft RuntimeError bei API-Fehler –
    Aufrufer dürfen dann NICHT schreiben (sonst würde die Liste überschrieben)."""
    settings = await bot.nitrado.get_settings()
    if settings is None:
        raise RuntimeError("Nitrado-API nicht erreichbar (Settings konnten nicht gelesen werden)")
    category, key, raw = _find_ban_setting(settings)
    names = [l.strip() for l in raw.splitlines() if l.strip()]
    return names, category, key

async def _write_banlist(names: List[str], category: str, key: str) -> Tuple[bool, str]:
    """Schreibt die Banliste in die Nitrado-Servereinstellungen (1 Name pro Zeile)."""
    return await bot.nitrado.set_setting(category, key, "\r\n".join(names))

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
    grund="Grund für den Ban (optional)"
)
async def cmd_ban(interaction: discord.Interaction, spieler: str, grund: str = "Kein Grund angegeben"):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction):
        return
    await interaction.response.defer()

    names = _split_names(spieler)
    if not names:
        return await interaction.followup.send("❌ Keinen gültigen Namen angegeben.")

    # Erst lesen – bei API-Fehler NICHT schreiben, sonst würde die
    # bestehende Nitrado-Banliste überschrieben/geleert
    try:
        current, category, key = await _read_banlist()
    except Exception as e:
        return await interaction.followup.send(
            f"❌ Nitrado-Banliste konnte nicht gelesen werden – nichts geändert.\n`{e}`")

    existing_lower = {n.lower() for n in current}
    added   = [n for n in names if n.lower() not in existing_lower]
    already = [n for n in names if n.lower() in existing_lower]

    sv = "ℹ️ Alle Namen standen bereits auf der Banliste"
    if added:
        ok, msg = await _write_banlist(current + added, category, key)
        if not ok:
            return await interaction.followup.send(
                f"❌ Nitrado-Banliste konnte nicht gespeichert werden – nichts geändert.\n`{msg}`")
        sv = "✅ In der Nitrado-Banliste gespeichert"

    # Lokale Metadaten (nur für die Anzeige in /banlist)
    now = datetime.now(timezone.utc).isoformat()
    for n in added:
        cfg.bans[n] = {"name": n, "reason": grund,
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
@bot.tree.command(name="ban_entfernen",
                  description="✅ Entfernt Spieler von der Banliste in den Nitrado-Servereinstellungen")
@app_commands.describe(spieler="Name(n) – mehrere per Komma getrennt")
async def cmd_unban(interaction: discord.Interaction, spieler: str):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction):
        return
    await interaction.response.defer()

    names = _split_names(spieler)
    if not names:
        return await interaction.followup.send("❌ Keinen gültigen Namen angegeben.")

    try:
        current, category, key = await _read_banlist()
    except Exception as e:
        return await interaction.followup.send(
            f"❌ Nitrado-Banliste konnte nicht gelesen werden – nichts geändert.\n`{e}`")

    wanted_lower = {n.lower() for n in names}
    new_list  = [n for n in current if n.lower() not in wanted_lower]
    removed   = [n for n in current if n.lower() in wanted_lower]
    not_found = [n for n in names if n.lower() not in {r.lower() for r in removed}]

    sv = "ℹ️ Keiner der Namen stand auf der Banliste"
    if removed:
        ok, msg = await _write_banlist(new_list, category, key)
        if not ok:
            return await interaction.followup.send(
                f"❌ Nitrado-Banliste konnte nicht gespeichert werden – nichts geändert.\n`{msg}`")
        sv = "✅ Von der Nitrado-Banliste entfernt"
        # Lokale Metadaten aufräumen (case-insensitive)
        for local_key in [k for k in cfg.bans if k.lower() in wanted_lower]:
            cfg.bans.pop(local_key, None)
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
async def cmd_banlist(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction):
        return
    await interaction.response.defer(ephemeral=True)

    try:
        all_bans, _category, _key = await _read_banlist()
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
    local = {k.lower(): v for k, v in cfg.bans.items()}
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
def _find_whitelist_setting(settings: Dict) -> Tuple[str, str, str]:
    """Sucht das Whitelist-Setting in den Nitrado-Settings.
    Reihenfolge: Config-Override (nitrado_whitelist_category/-key) →
    Auto-Erkennung (Key 'whitelist') → Fallback ('general', 'whitelist').
    Gibt (category, key, aktueller_wert) zurück."""
    ov_cat = str(cfg.config.get("nitrado_whitelist_category") or "").strip()
    ov_key = str(cfg.config.get("nitrado_whitelist_key") or "").strip()
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

async def _read_whitelist() -> Tuple[List[str], str, str]:
    """Liest die Whitelist aus den Nitrado-Servereinstellungen.
    Gibt (namen, category, key) zurück. Wirft RuntimeError bei API-Fehler –
    Aufrufer dürfen dann NICHT schreiben (sonst würde die Liste überschrieben)."""
    settings = await bot.nitrado.get_settings()
    if settings is None:
        raise RuntimeError("Nitrado-API nicht erreichbar (Settings konnten nicht gelesen werden)")
    category, key, raw = _find_whitelist_setting(settings)
    names = [l.strip() for l in raw.splitlines() if l.strip()]
    return names, category, key

async def _write_whitelist(names: List[str], category: str, key: str) -> Tuple[bool, str]:
    """Schreibt die Whitelist in die Nitrado-Servereinstellungen (1 Name pro Zeile)."""
    return await bot.nitrado.set_setting(category, key, "\r\n".join(names))


# ══════════════════════════════════════════════════════════════
#  /whitelist add|remove|show – Whitelist verwalten (Admin)
# ══════════════════════════════════════════════════════════════
whitelist_group = app_commands.Group(
    name="whitelist",
    description="✅ Whitelist in den Nitrado-Servereinstellungen verwalten (Admin)")


@whitelist_group.command(
    name="add",
    description="✅ Spieler zur Whitelist hinzufügen (mehrere per Komma/Zeile)")
@app_commands.describe(spieler="PlayStation-Name(n) – mehrere per Komma getrennt")
async def whitelist_add(interaction: discord.Interaction, spieler: str):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction):
        return
    await interaction.response.defer()

    names = _split_names(spieler.replace("\n", ","))
    if not names:
        return await interaction.followup.send("❌ Keinen gültigen Namen angegeben.")

    try:
        current, category, key = await _read_whitelist()
    except Exception as e:
        return await interaction.followup.send(
            f"❌ Nitrado-Whitelist konnte nicht gelesen werden – nichts geändert.\n`{e}`")

    existing_lower = {n.lower() for n in current}
    added   = [n for n in names if n.lower() not in existing_lower]
    already = [n for n in names if n.lower() in existing_lower]

    sv = "ℹ️ Alle Namen standen bereits auf der Whitelist"
    if added:
        ok, msg = await _write_whitelist(current + added, category, key)
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
@app_commands.describe(spieler="PlayStation-Name(n) – mehrere per Komma getrennt")
async def whitelist_remove(interaction: discord.Interaction, spieler: str):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction):
        return
    await interaction.response.defer()

    names = _split_names(spieler.replace("\n", ","))
    if not names:
        return await interaction.followup.send("❌ Keinen gültigen Namen angegeben.")

    try:
        current, category, key = await _read_whitelist()
    except Exception as e:
        return await interaction.followup.send(
            f"❌ Nitrado-Whitelist konnte nicht gelesen werden – nichts geändert.\n`{e}`")

    wanted_lower = {n.lower() for n in names}
    new_list  = [n for n in current if n.lower() not in wanted_lower]
    removed   = [n for n in current if n.lower() in wanted_lower]
    not_found = [n for n in names if n.lower() not in {r.lower() for r in removed}]

    sv = "ℹ️ Keiner der Namen stand auf der Whitelist"
    if removed:
        ok, msg = await _write_whitelist(new_list, category, key)
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
async def whitelist_show(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction):
        return
    await interaction.response.defer(ephemeral=True)

    try:
        names, _category, _key = await _read_whitelist()
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

    def __init__(self):
        super().__init__()
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
        admin_ch_id = cfg.get_channel(gid, "whitelist_request")
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


class WhitelistPanelView(discord.ui.View):
    """Persistentes Panel mit dem Button, der das PSN-Eingabe-Modal öffnet."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="PSN Name eintragen", emoji="🎮",
                       style=discord.ButtonStyle.primary,
                       custom_id="wl_panel_open")
    async def open_modal(self, interaction: discord.Interaction,
                         button: discord.ui.Button):
        await interaction.response.send_modal(WhitelistRequestModal())


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

        if bot.nitrado is None:
            cfg.whitelist_reqs[self.reqid] = req
            cfg.save_whitelist_reqs()
            return await interaction.response.send_message(
                "❌ Nitrado ist noch nicht eingerichtet – führe zuerst "
                "`/setup token` aus. Anfrage bleibt offen.", ephemeral=True)

        await interaction.response.defer()
        try:
            current, category, key = await _read_whitelist()
        except Exception as e:
            cfg.whitelist_reqs[self.reqid] = req
            cfg.save_whitelist_reqs()
            return await interaction.followup.send(
                f"❌ Whitelist konnte nicht gelesen werden – nichts geändert. "
                f"Anfrage bleibt offen.\n`{e}`", ephemeral=True)

        psn = req["psn"]
        if psn.lower() not in {n.lower() for n in current}:
            ok, msg = await _write_whitelist(current + [psn], category, key)
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
    admin_channel="Staff-Channel, in dem die Anfragen zur Freigabe landen")
async def send_whitelist_panel(interaction: discord.Interaction,
                               panel_channel: discord.TextChannel,
                               admin_channel: discord.TextChannel):
    if not _is_admin(interaction):
        return await _deny(interaction)

    # Anfrage-Channel pro Guild merken (das Modal liest ihn beim Absenden aus)
    cfg.set_channel(interaction.guild_id, "whitelist_request", admin_channel.id)

    panel_embed = discord.Embed(
        title="✅ Whitelist-Anmeldung",
        description=WHITELIST_PANEL_TEXT,
        color=0x5865F2)
    try:
        await panel_channel.send(embed=panel_embed, view=WhitelistPanelView())
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
async def cmd_positions(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)

    positions = DayZLogParser.player_positions
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
@bot.tree.command(name="spieler_suche", description="🔍 Sucht einen Spieler in den aktuellen Logs")
@app_commands.describe(name="Ingame-Name oder Steam64-ID")
async def cmd_search(interaction: discord.Interaction, name: str):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction, need_ftp=True):
        return
    await interaction.response.defer(ephemeral=True)

    log_dir = cfg.config.get("ftp_log_dir")
    if not log_dir:
        return await interaction.followup.send(
            "❌ Log-Verzeichnis nicht konfiguriert. Starte den Bot neu oder nutze `/ftp_scan`.",
            ephemeral=True
        )

    loop = asyncio.get_running_loop()
    adm_files = await loop.run_in_executor(None, bot.ftp.list_adm_files, log_dir)
    if not adm_files:
        return await interaction.followup.send("❌ Keine Log-Dateien gefunden.", ephemeral=True)

    content = await loop.run_in_executor(None, bot.ftp.read_file, adm_files[-1])
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


# ══════════════════════════════════════════════════════════════
#  /ftp_scan – FTP-Verzeichnisse neu scannen
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="ftp_scan", description="🔎 Scannt FTP-Server erneut nach Log-Verzeichnissen")
async def cmd_ftp_scan(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction, need_ftp=True):
        return
    await interaction.response.defer(ephemeral=True)

    # Pfade zurücksetzen damit discover_paths nicht überspringt
    cfg.config["ftp_log_dir"]          = ""
    cfg.config["ftp_ban_file"]         = ""
    cfg.config["ftp_mission_dir"]      = ""
    cfg.config["cfg_effect_area_path"] = ""
    cfg.log_state = {}
    cfg.save_config()
    cfg.save_log_state()

    await bot._auto_discover()

    log_dir  = cfg.config.get("ftp_log_dir")          or "Nicht gefunden"
    ban_file = cfg.config.get("ftp_ban_file")         or "Nicht gefunden"
    mission  = cfg.config.get("ftp_mission_dir")      or "Nicht gefunden"
    effect   = cfg.config.get("cfg_effect_area_path") or "Nicht gefunden"

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
@app_commands.describe(zeilen="Anzahl der Zeilen (Standard: 20, max. 40)")
async def cmd_raw_log(interaction: discord.Interaction, zeilen: int = 20):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction, need_ftp=True):
        return
    await interaction.response.defer(ephemeral=True)

    log_dir = cfg.config.get("ftp_log_dir")
    if not log_dir:
        return await interaction.followup.send(
            "❌ Log-Verzeichnis nicht konfiguriert. Nutze `/ftp_scan`.", ephemeral=True
        )

    loop = asyncio.get_running_loop()
    adm_files = await loop.run_in_executor(None, bot.ftp.list_adm_files, log_dir)
    if not adm_files:
        return await interaction.followup.send("❌ Keine ADM-Dateien gefunden.", ephemeral=True)

    content = await loop.run_in_executor(None, bot.ftp.read_file, adm_files[-1])
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
@app_commands.describe(zeilen="Zu durchsuchende Log-Zeilen (Standard: 500, max: 2000)")
async def cmd_test(interaction: discord.Interaction, zeilen: int = 500):
    if not _is_admin(interaction):
        return await _deny(interaction)
    if not await _require_nitrado(interaction, need_ftp=True):
        return
    await interaction.response.defer(ephemeral=True)

    # ── 1. Log-Datei lesen ────────────────────────────────────
    log_dir = cfg.config.get("ftp_log_dir")
    if not log_dir:
        return await interaction.followup.send(
            "❌ Log-Verzeichnis nicht konfiguriert. Nutze `/ftp_scan`.", ephemeral=True
        )

    loop = asyncio.get_running_loop()
    adm_files = await loop.run_in_executor(None, bot.ftp.list_adm_files, log_dir)
    if not adm_files:
        return await interaction.followup.send("❌ Keine ADM-Dateien gefunden.", ephemeral=True)

    content = await loop.run_in_executor(None, bot.ftp.read_file, adm_files[-1])
    if not content:
        return await interaction.followup.send("❌ Log-Datei konnte nicht gelesen werden.", ephemeral=True)

    # ── 2. Letzten N Zeilen parsen ────────────────────────────
    zeilen = max(50, min(zeilen, 2000))
    recent_lines = "\n".join(content.splitlines()[-zeilen:])
    events = bot.parser.parse_lines(recent_lines)

    # ── 3. Pro Log-Typ das neueste Event merken ───────────────
    # Events kommen in Lesereihenfolge → letztes überschreibt → neuestes bleibt
    latest_by_logtype: Dict[str, Dict] = {}
    for ev in events:
        lt = DayZLogParser.EVENT_TO_LOG.get(ev["type"])
        if lt:
            latest_by_logtype[lt] = ev

    # ── 4. Pro Log-Typ in konfigurierten Channel posten ───────
    guild_cfg = cfg.guilds.get(str(interaction.guild_id), {})

    sent:     List[Tuple[str, str]] = []  # (log_type, channel_mention)
    no_event: List[str]             = []  # Log-Typ ohne Event im gescannten Bereich
    no_ch:    List[str]             = []  # Log-Typ mit Event aber ohne Channel
    errors:   List[Tuple[str, str]] = []  # (log_type, Fehlermeldung)

    for lt in LOG_TYPES:
        ev    = latest_by_logtype.get(lt)
        ch_id = guild_cfg.get(lt)

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

    if sent:
        lines = [f"✅ `{lt}` → {ch}" for lt, ch in sent]
        summary.add_field(
            name=f"✅ Erfolgreich gepostet ({len(sent)})",
            value="\n".join(lines),
            inline=False
        )
    if no_ch:
        lines = [f"⚪ `{lt}`" for lt in no_ch]
        summary.add_field(
            name=f"⚪ Kein Channel konfiguriert ({len(no_ch)})",
            value="  ".join(lines),
            inline=False
        )
    if no_event:
        lines = [f"🔍 `{lt}`" for lt in no_event]
        summary.add_field(
            name=f"🔍 Kein Event in den letzten {zeilen} Zeilen ({len(no_event)})",
            value="  ".join(lines),
            inline=False
        )
    if errors:
        lines = [f"❌ `{lt}` — {msg}" for lt, msg in errors]
        summary.add_field(
            name=f"❌ Fehler ({len(errors)})",
            value="\n".join(lines),
            inline=False
        )

    summary.set_footer(
        text="🔍-Typen = diese Events kommen in deinen Logs nicht vor "
             "(z.B. Damage/Loot brauchen Server-Mods). "
             "⚪-Typen → /setup feeds <typ> #channel"
    )
    await interaction.followup.send(embed=summary, ephemeral=True)


@bot.tree.command(name="ftp_status", description="🔌 Testet die FTP-Verbindung zum Nitrado-Server")
async def cmd_ftp_status(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    await interaction.response.defer(ephemeral=True)

    host     = cfg.config.get("ftp_host", "–")
    port     = cfg.config.get("ftp_port", 21)
    user     = cfg.config.get("ftp_user", "–")
    log_dir  = cfg.config.get("ftp_log_dir",  "Noch nicht gesetzt")

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
            ftp.login(user, cfg.config.get("ftp_password", ""))
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
            adm_files = await loop.run_in_executor(None, bot.ftp.list_adm_files, log_dir)
            adm_count  = len(adm_files)
            adm_latest = adm_files[-1].split("/")[-1] if adm_files else "Keine gefunden"
        except Exception as e:
            adm_latest = f"Fehler: {e}"

    # ── 3. Nitrado-Banliste prüfen (Servereinstellungen, nicht FTP) ──
    try:
        ban_names, _bcat, _bkey = await _read_banlist()
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
        embed.set_footer(text="Tipp: Prüfe Host, Port, Benutzername und Passwort in config.json")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════
#  /log_status – Polling-Status anzeigen
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="log_status", description="📄 Zeigt den aktuellen Log-Polling Status")
async def cmd_log_status(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)

    state = cfg.log_state.get("current", {})
    embed = discord.Embed(title="📄 Log-Polling Status", color=0x5865F2)
    embed.add_field(name="Aktuelle Log-Datei",
                    value=f"`{state.get('file', 'Keine')}`",         inline=False)
    embed.add_field(name="Gelesene Bytes",
                    value=f"{state.get('offset', 0):,}",             inline=True)
    embed.add_field(name="Poll-Intervall",
                    value=f"{cfg.config.get('log_poll_interval_seconds', 10)}s",inline=True)
    embed.add_field(name="Log-Verzeichnis",
                    value=f"`{cfg.config.get('ftp_log_dir', '–')}`", inline=False)
    embed.add_field(name="Banliste",
                    value="Nitrado-Servereinstellungen (via API)",     inline=False)
    embed.add_field(name="FTP-Host",
                    value=f"`{cfg.config.get('ftp_host', '–')}`",    inline=False)
    embed.add_field(name="Bekannte Spieler-Positionen",
                    value=str(len(DayZLogParser.player_positions)),   inline=True)
    embed.add_field(name="Lokale Bans",
                    value=str(len(cfg.bans)),                         inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════
#  /hilfe – Alle Befehle
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="hilfe", description="❓ Zeigt alle verfügbaren Bot-Befehle")
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
        "`/show_feeds` — Alle Feeds & Channels anzeigen\n"
        "`/edit_feeds <feed> [#channel]` — Feed ändern oder deaktivieren"
    ), inline=False)
    embed.add_field(name="🛡️ Zonen-Pings", value=(
        "`/zone create <x> <z> <name> <radius> [channel] [rolle]` — Zone anlegen "
        "(pingt beim Betreten; Channel & Rolle optional)\n"
        "`/zone remove <name>` — Zone entfernen\n"
        "`/zone list` — Alle aktiven Zonen (Name, x/z, Radius, Channel)\n"
        "`/zone edit <name> […]` — Zone bearbeiten (auch Channel)\n"
        "`/zone allowlist add|remove|show <zone> <spieler>` — Spieler in einer Zone "
        "ignorieren / wieder melden / anzeigen"
    ), inline=False)
    embed.add_field(name="📢 Setup", value=(
        "`/setup token <token>` — Nitrado-Token setzen; Server im Dropdown "
        "auswählen & bestätigen (FTP-Zugang und aktive Karte werden "
        "automatisch erkannt)\n"
        "`/setup feeds <feed> #channel` — Feed-Channel per Dropdown setzen "
        "(killfeed, damagefeed, joinleave, suicide, chat, adminlog, envdeath, "
        "vehiclecrash, basebuild, loot, connecting, shop_log, economy_log, "
        "status, restart, zone)\n"
        "`/setup uebersicht` — Alle konfigurierten Channels anzeigen\n"
        "`/edit_feeds <feed> [#channel]` — Feed ändern oder deaktivieren"
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
        "`/setup feeds shop_log|economy_log #channel` — Feed-Channels"
    ), inline=False)
    embed.add_field(name="📢 Ankündigungen", value=(
        "`/erstellen` — Neue wiederkehrende Ankündigung anlegen (Tag/Uhrzeit/Wiederholung per Dropdown)\n"
        "`/liste` — Alle Ankündigungen mit nächstem Sendetermin & Countdown\n"
        "`/löschen <index>` — Ankündigung löschen\n"
        "`/edit ankuendigung <index>` — Nachricht/Bild einer Ankündigung ändern\n"
        "`/hackban <user_id> [grund]` — Discord-Nutzer per ID bannen"
    ), inline=False)
    admin_ids = cfg.config.get("admin_role_ids", [])
    footer = (f"Admin-Rollen-IDs: {', '.join(str(i) for i in admin_ids)}"
              if admin_ids else
              f"Admin-Rolle: {cfg.config.get('admin_role_name', 'DayZ Admin')} "
              f"(Tipp: admin_role_ids in config.json setzen)")
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

            key = f"{today.isoformat()}-{day}-{time_str}-{ann['channel_id']}"

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
            channel = bot.get_channel(int(self.channel.value))

        except Exception:

            return await interaction.response.send_message(
                "❌ Fehlerhafte Channel-ID",
                ephemeral=True
            )

        if not channel:

            return await interaction.response.send_message(
                "❌ Channel nicht gefunden",
                ephemeral=True
            )

        ann_data["announcements"].append({
            "message": self.msg.value,
            "channel_id": str(self.channel.value),
            "day": self.day,
            "time": self.time,
            "repeat": self.repeat_type,
            "image": self.image.value.strip() if self.image.value else None,
            "last_sent": None  # Wird nach dem ersten Senden gesetzt
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

        ann_data["announcements"][self.index]["message"] = self.message_input.value

        ann_data["announcements"][self.index]["image"] = (
            self.image_input.value.strip()
            if self.image_input.value
            else None
        )

        save_announcements()

        await interaction.response.send_message(
            "✅ Ankündigung bearbeitet",
            ephemeral=True
        )


@bot.tree.command(name="erstellen", description="📢 Neue wiederkehrende Ankündigung anlegen")
async def cmd_ann_erstellen(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)

    await interaction.response.send_message(
        "Setup starten:",
        view=CreateAnnouncementView(),
        ephemeral=True
    )


@bot.tree.command(name="liste", description="📋 Zeigt alle geplanten Ankündigungen")
async def cmd_ann_liste(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)

    embed = discord.Embed(
        title="📋 Ankündigungen",
        color=discord.Color.blue()
    )

    if not ann_data["announcements"]:

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

        for i, ann in enumerate(ann_data["announcements"]):

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


@bot.tree.command(name="löschen", description="🗑️ Löscht eine Ankündigung")
@app_commands.describe(index="Nummer der Ankündigung (siehe /liste)")
async def cmd_ann_loeschen(
    interaction: discord.Interaction,
    index: int
):
    if not _is_admin(interaction):
        return await _deny(interaction)

    if index < 0 or index >= len(ann_data["announcements"]):

        return await interaction.response.send_message(
            "❌ Ungültiger Index",
            ephemeral=True
        )

    ann_data["announcements"].pop(index)

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
            c.execute("""CREATE TABLE IF NOT EXISTS purchases (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
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
            # PvP-Kills für /stats und /leaderboard (Server-weit, nicht pro Guild)
            c.execute("""CREATE TABLE IF NOT EXISTS kills (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
            # Offene Spielzeit-Sitzungen (connect → disconnect/Restart)
            c.execute("""CREATE TABLE IF NOT EXISTS sessions (
                ingame_name     TEXT PRIMARY KEY COLLATE NOCASE,
                ingame_id       TEXT,
                connect_ts      REAL NOT NULL,
                last_seen_ts    REAL NOT NULL,
                credited_blocks INTEGER NOT NULL DEFAULT 0)""")
            c.commit()

    # ── Salden ────────────────────────────────────────────────
    def ensure_user(self, guild_id: int, user_id: int):
        """Legt den User mit Startguthaben an, falls noch nicht vorhanden."""
        start = int(cfg.config.get("starting_balance", 0))
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
    def create_purchase(self, guild_id: int, user_id: int, user_name: str,
                        item_name: str, classname: str, amount: int, total_price: int,
                        x: float, y: float, z: float, area_names: List[str]) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO purchases
                   (guild_id, user_id, user_name, item_name, classname, amount,
                    total_price, x, y, z, area_names, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                (guild_id, user_id, user_name, item_name, classname, amount,
                 total_price, x, y, z, json.dumps(area_names), time.time()))
            self._conn.commit()
            return int(cur.lastrowid)

    def pending_purchases(self, created_before: Optional[float] = None) -> List[sqlite3.Row]:
        q = "SELECT * FROM purchases WHERE status='pending'"
        args: Tuple = ()
        if created_before is not None:
            q += " AND created_at <= ?"
            args = (created_before,)
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
    def record_kill(self, killer_name: str, killer_id: Optional[str],
                    victim_name: str, victim_id: Optional[str],
                    weapon: Optional[str], distance: Any):
        try:
            dist: Optional[float] = float(str(distance).replace(",", "."))
        except (TypeError, ValueError):
            dist = None
        with self._lock:
            self._conn.execute(
                "INSERT INTO kills (created_at, killer_name, killer_id, victim_name, "
                "victim_id, weapon, distance) VALUES (?,?,?,?,?,?,?)",
                (time.time(), killer_name, killer_id, victim_name, victim_id, weapon, dist))
            self._conn.commit()

    def player_stats(self, name: str) -> Optional[Dict]:
        """Kills, Tode (PvP), Lieblingswaffe und weitester Kill eines Spielers."""
        with self._lock:
            kills = int(self._conn.execute(
                "SELECT COUNT(*) AS n FROM kills WHERE killer_name=? COLLATE NOCASE",
                (name,)).fetchone()["n"])
            deaths = int(self._conn.execute(
                "SELECT COUNT(*) AS n FROM kills WHERE victim_name=? COLLATE NOCASE",
                (name,)).fetchone()["n"])
            if kills == 0 and deaths == 0:
                return None
            fav = self._conn.execute(
                "SELECT weapon, COUNT(*) AS n FROM kills "
                "WHERE killer_name=? COLLATE NOCASE AND weapon IS NOT NULL "
                "AND weapon NOT IN ('', 'Unbekannt') "
                "GROUP BY weapon ORDER BY n DESC LIMIT 1", (name,)).fetchone()
            longest = self._conn.execute(
                "SELECT MAX(distance) AS d FROM kills WHERE killer_name=? COLLATE NOCASE",
                (name,)).fetchone()["d"]
        return {
            "kills": kills, "deaths": deaths,
            "kd": (kills / deaths) if deaths else float(kills),
            "fav_weapon": fav["weapon"] if fav else None,
            "fav_weapon_kills": int(fav["n"]) if fav else 0,
            "longest": float(longest) if longest is not None else None,
        }

    def leaderboard(self, limit: int = 10) -> List[Dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT killer_name AS name, COUNT(*) AS kills, MAX(distance) AS best "
                "FROM kills GROUP BY killer_name COLLATE NOCASE "
                "ORDER BY kills DESC, best DESC LIMIT ?", (limit,)).fetchall()
            out: List[Dict] = []
            for r in rows:
                deaths = int(self._conn.execute(
                    "SELECT COUNT(*) AS n FROM kills WHERE victim_name=? COLLATE NOCASE",
                    (r["name"],)).fetchone()["n"])
                out.append({"name": r["name"], "kills": int(r["kills"]), "deaths": deaths,
                            "kd": (int(r["kills"]) / deaths) if deaths else float(r["kills"]),
                            "best": float(r["best"]) if r["best"] is not None else None})
        return out

    def known_player_names(self, prefix: str = "", limit: int = 25) -> List[str]:
        """Spielernamen aus Kills + Sitzungen (für Autocomplete)."""
        like = f"%{prefix}%" if prefix else "%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM ("
                "  SELECT killer_name AS name FROM kills"
                "  UNION SELECT victim_name FROM kills"
                "  UNION SELECT ingame_name FROM sessions) "
                "WHERE name LIKE ? COLLATE NOCASE ORDER BY name LIMIT ?",
                (like, limit)).fetchall()
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

    def links_for_name(self, ingame_name: str) -> List[sqlite3.Row]:
        """Alle Guild-Verknüpfungen für einen Ingame-Namen (case-insensitive)."""
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM links WHERE ingame_name=? COLLATE NOCASE",
                (ingame_name,)).fetchall())

    def update_link_id(self, ingame_name: str, ingame_id: str):
        """Trägt die im Log gesehene Ingame-ID zum verlinkten Namen nach."""
        if not ingame_id:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE links SET ingame_id=? WHERE ingame_name=? COLLATE NOCASE "
                "AND (ingame_id IS NULL OR ingame_id != ?)",
                (ingame_id, ingame_name, ingame_id))
            self._conn.commit()

    def list_links(self, guild_id: int) -> List[sqlite3.Row]:
        """Alle Verknüpfungen einer Guild, alphabetisch nach PSN-Name."""
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM links WHERE guild_id=? ORDER BY ingame_name COLLATE NOCASE",
                (guild_id,)).fetchall())

    def has_session(self, ingame_name: str) -> bool:
        """True, wenn für den Spieler gerade eine Spielzeit-Sitzung offen ist."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sessions WHERE ingame_name=? COLLATE NOCASE",
                (ingame_name,)).fetchone()
        return row is not None

    def sync_sessions_from_positions(self, positions: Dict, max_age_seconds: int = 300) -> int:
        """Öffnet Sitzungen für VERLINKTE Spieler, die laut Log-Positions-Tracking
        gerade aktiv sind, aber keine offene Sitzung haben (verpasstes Connect-Event
        durch Bot-Downtime/Backlog-Skip oder /link während man schon online ist).
        Gibt die Anzahl neu geöffneter Sitzungen zurück."""
        now_utc = datetime.now(timezone.utc)
        with self._lock:
            linked = {str(r["ingame_name"]).lower()
                      for r in self._conn.execute(
                          "SELECT DISTINCT ingame_name FROM links").fetchall()}
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
            if self.has_session(pname):
                continue
            self.open_session(pname, info.get("id"))
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
    def open_session(self, ingame_name: str, ingame_id: Optional[str]):
        """Connect-Event: neue Sitzung (Reconnect setzt den Zähler zurück)."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(ingame_name, ingame_id, connect_ts, last_seen_ts, credited_blocks) "
                "VALUES (?,?,?,?,0)",
                (ingame_name, ingame_id, now, now))
            self._conn.commit()

    def close_session(self, ingame_name: str):
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE ingame_name=? COLLATE NOCASE",
                               (ingame_name,))
            self._conn.commit()

    def close_all_sessions(self):
        with self._lock:
            self._conn.execute("DELETE FROM sessions")
            self._conn.commit()

    def playtime_credits_due(self, interval_seconds: int) -> List[Dict]:
        """Berechnet pro offener Sitzung neu fällige Spielzeit-Blöcke und
        schreibt credited_blocks fort. Gibt [{name, blocks}] zurück."""
        now = time.time()
        out: List[Dict] = []
        with self._lock:
            rows = self._conn.execute("SELECT * FROM sessions").fetchall()
            for r in rows:
                total = int((now - float(r["connect_ts"])) // max(60, interval_seconds))
                due = total - int(r["credited_blocks"])
                if due > 0:
                    self._conn.execute(
                        "UPDATE sessions SET credited_blocks=?, last_seen_ts=? "
                        "WHERE ingame_name=?",
                        (total, now, r["ingame_name"]))
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
def _cur_symbol() -> str:
    return cfg.config.get("currency_symbol", "₽")

def _fmt_money(n: int) -> str:
    return f"{int(n):,} {_cur_symbol()}"

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
                     content: Optional[str] = None, channel_id: Optional[int] = None):
    """Postet ein Embed in den konfigurierten Feed-Channel (eine Guild oder alle).
    content: optionaler Nachrichtentext vor dem Embed (z. B. Rollen-Ping bei Zonen).
    channel_id: optionaler Ziel-Channel, der die Feed-Konfiguration überschreibt
    (z. B. eigener Warn-Channel einer Zone)."""
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
    gids = [str(guild_id)] if guild_id else list(cfg.guilds.keys())
    for gid in gids:
        ch_id = cfg.get_channel(int(gid), log_type)
        if not ch_id:
            continue
        await _send(ch_id, f"{log_type} → Guild {gid}")


async def _notify_link_change(guild_id: Optional[int], embed: discord.Embed):
    """Meldet /link- und /unlink-Aktionen an die Admins:
    bevorzugt im adminlog-Feed, sonst im economy_log-Feed."""
    if guild_id and cfg.get_channel(int(guild_id), "adminlog"):
        return await _post_feed(guild_id, "adminlog", embed)
    await _post_feed(guild_id, "economy_log", embed)


# ══════════════════════════════════════════════════════════════
#  SHOP-MANAGER – Auslieferung über cfgEffectArea.json
#  Ablauf: Kauf → Eintrag in cfgEffectArea.json (pending) →
#  Server-Neustart (Item spawnt) → Eintrag entfernen (delivered).
#  WICHTIG: Ohne Entfernen respawnt das Item bei JEDEM Neustart!
# ══════════════════════════════════════════════════════════════
class ShopManager:
    AREA_PREFIX = "SHOP_"

    def __init__(self, bot_ref: "DayZBot"):
        self.bot  = bot_ref
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
        path = cfg.config.get("cfg_effect_area_path")
        if path:
            return path
        mission = cfg.config.get("ftp_mission_dir")
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
        ok = await loop.run_in_executor(None, self.bot.ftp.write_file, path, content)
        if ok:
            # Aufräumen (Best-Effort): keine .bak mehr im Mission-Ordner
            await loop.run_in_executor(None, self.bot.ftp.delete_file, path + ".bak")
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
            raw, status = await loop.run_in_executor(None, self.bot.ftp.read_file_ex, path)
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
                radius = float(cfg.config.get("default_radius", 1))
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
            raw, status = await loop.run_in_executor(None, self.bot.ftp.read_file_ex, path)
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
        for r in db.pending_purchases():
            try:
                valid.update(json.loads(r["area_names"] or "[]"))
            except Exception:
                pass
        async with self.lock:
            loop = asyncio.get_running_loop()
            raw, status = await loop.run_in_executor(None, self.bot.ftp.read_file_ex, path)
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
        pending = db.pending_purchases()
        report["pending"] = len(pending)
        async with self.lock:
            loop = asyncio.get_running_loop()
            raw, status = await loop.run_in_executor(None, self.bot.ftp.read_file_ex, path)
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
                radius = float(cfg.config.get("default_radius", 1))
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
        delay = max(5, int(cfg.config.get("restart_cooldown_seconds", 300)))
        # Mindestabstand zum vorherigen Auto-Restart erzwingen (Server bootet evtl. noch)
        wait = max(delay, (self._last_restart_ts + delay) - time.time())
        log.info(f"[SHOP] Auto-Restart in {int(wait)}s geplant (Käufe werden gesammelt).")
        await asyncio.sleep(wait)
        self._last_restart_ts = time.time()
        try:
            ok, msg = await self.bot.nitrado.restart()
            log.info(f"[SHOP] Auto-Restart nach Kauf ausgelöst: ok={ok} – {msg}")
        except Exception as e:
            log.error(f"[SHOP] Auto-Restart fehlgeschlagen: {e}")

    # ── Warten bis der Server wieder online ist (A2S) ─────────
    async def _wait_for_server_online(self) -> bool:
        """Pollt den Spielserver per A2S, bis er antwortet (= wirklich online).
        True = online gesehen. False = server_ip/query_port fehlt oder Timeout
        (delivery_online_wait_max_seconds) – dann greift der feste Delay als
        Fallback, sonst würden Items bei falschem Query-Port ewig respawnen."""
        ip = str(cfg.config.get("server_ip") or "").split(":")[0].strip()
        qport = int(cfg.config.get("query_port", 0) or 0)
        if not ip or not qport:
            log.warning("[SHOP] server_ip/query_port nicht gesetzt – kann Server-online "
                        "nicht prüfen, nutze festen Delivery-Delay als Fallback.")
            return False
        max_wait = max(60, int(cfg.config.get("delivery_online_wait_max_seconds", 2700)))
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
        grace = int(cfg.config.get("delivery_grace_seconds", 90))
        poll  = int(cfg.config.get("log_poll_interval_seconds", 10))
        # Grace muss über dem Poll-Intervall liegen, sonst könnte ein Kauf, der NACH
        # dem Restart einging, fälschlich als geliefert gelten (bezahlt, nie gespawnt)
        grace = max(grace, poll + 30)
        # Cutoff am ERKENNUNGS-Zeitpunkt festmachen: Käufe, die während der
        # Wartezeit oder eines Retrys eingehen, sind noch nicht gespawnt und
        # dürfen nicht als geliefert markiert werden
        restart_at = self._last_restart_at or time.time()
        cutoff = restart_at - grace
        rows = db.pending_purchases(created_before=cutoff)
        if not rows:
            return
        if delayed:
            delay = max(0, int(cfg.config.get("delivery_cleanup_delay_seconds", 600)))
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
                await _post_feed(warn_gid, "shop_log", warn)
            return
        db.mark_delivered(ids)
        for r in rows:
            embed = discord.Embed(
                title="📦 DELIVERED",
                description=(f"**{r['amount']}× {r['item_name']}** for <@{r['user_id']}> "
                             f"spawned after the server restart."),
                color=0x2ECC71)
            embed.set_footer(text=f"Purchase #{r['id']}")
            await _post_feed(int(r["guild_id"]), "shop_log", embed)


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
    conf = cfg.config.get("economy", {}).get("work", {})
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
    conf = cfg.config.get("economy", {}).get("daily", {})
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
    conf = cfg.config.get("economy", {}).get("beg", {})
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
    await _post_feed(interaction.guild_id, "economy_log", embed)


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
        catalog.load()   # Katalog (shop_items.json bzw. config-Fallback) mit neu laden
        items = [i for i in catalog.items if i.get("enabled", True)]
        embed = discord.Embed(
            title="🔄 Config reloaded",
            description=(f"`config.json` was reloaded successfully.\n"
                         f"Catalog: **{len(items)}** active items from `{catalog.source}` · "
                         f"Currency: **{cfg.config.get('currency_name', '?')} ({_cur_symbol()})**"),
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
    """Autocomplete: bekannte Spielernamen aus Kills + Sitzungen."""
    loop = asyncio.get_running_loop()
    try:
        names = await loop.run_in_executor(None, db.known_player_names, current, 25)
    except Exception:
        names = []
    return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names[:25]]


@bot.tree.command(name="stats", description="📊 Kill-Statistiken eines Spielers (Kills, Tode, K/D, Waffe)")
@app_commands.describe(spieler="Ingame-/PlayStation-Name")
@app_commands.autocomplete(spieler=_player_name_ac)
async def cmd_stats(interaction: discord.Interaction, spieler: str):
    st = db.player_stats(spieler.strip())
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
        links = [lk for lk in db.links_for_name(spieler.strip())
                 if int(lk["guild_id"]) == interaction.guild_id]
        if links:
            e.add_field(name="🔗 Verknüpft mit",
                        value=f"<@{int(links[0]['user_id'])}>", inline=True)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="leaderboard", description="🏆 Top 10 PvP-Killer des Servers")
async def cmd_leaderboard(interaction: discord.Interaction):
    rows = db.leaderboard(10)
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


def _seen_in_logs(name: str, max_age_seconds: int = 900) -> Optional[Dict]:
    """Prüft, ob der Spieler kürzlich in den ADM-Logs auftauchte (Positions-Tracking
    des Parsers). Gibt den Eintrag (mit 'id') zurück, sonst None."""
    target = name.lower()
    now = datetime.now(timezone.utc)
    for pname, info in list(DayZLogParser.player_positions.items()):
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
    seen = _seen_in_logs(name)
    if seen:
        if seen.get("id"):
            db.update_link_id(name, str(seen["id"]))
        if not db.has_session(name):
            db.open_session(name, seen.get("id"))
        online_line = "\n🟢 Du bist gerade auf dem Server – der Spielzeit-Zähler läuft ab jetzt!"
    else:
        online_line = ("\nℹ️ Aktuell nicht in den Logs gesehen – der Spielzeit-Zähler "
                       "startet bei deinem nächsten Connect.")
    reward   = int(cfg.config.get("kill_reward", 0))
    pt       = cfg.config.get("playtime_reward") or {}
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
    for lk in db.links_for_name(name):
        if int(lk["guild_id"]) == interaction.guild_id and int(lk["user_id"]) != user.id:
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
        online = "🟢 " if db.has_session(name) else "⚫ "
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
    for r in rows[:50]:
        online = "🟢 " if db.has_session(str(r["ingame_name"])) else "⚫ "
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
    bconf   = cfg.config.get("bounty", {})
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
    await _post_feed(interaction.guild_id, "economy_log", log_embed)


# ══════════════════════════════════════════════════════════════
#  CASINO – /slots
# ══════════════════════════════════════════════════════════════
@bot.tree.command(name="slots", description="🎰 Spin the slot machine")
@app_commands.describe(bet="Your bet (paid from wallet)")
async def cmd_slots(interaction: discord.Interaction, bet: app_commands.Range[int, 1]):
    if not await _require_guild(interaction):
        return
    conf = cfg.config.get("casino", {}).get("slots", {})
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
    conf = cfg.config.get("casino", {}).get("roulette", {})
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
    conf = cfg.config.get("casino", {}).get("blackjack", {})
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
    """Item-Katalog: lädt bevorzugt shop_items.json (generiert aus types.xml),
    sonst shop_items aus config.json. Hält Indizes, damit Lookups und
    Autocomplete auch bei ~1700 Items schnell bleiben."""

    def __init__(self):
        self.items: List[Dict] = []
        self.source = "config.json"
        self._by_key: Dict[str, Dict] = {}              # name/classname (lower) → Item
        self.by_category: Dict[str, List[Dict]] = {}
        # (suchtext, label, value, enabled) – vorberechnet für Autocomplete
        self._ac_index: List[Tuple[str, str, str, bool]] = []

    def load(self):
        items: Optional[List[Dict]] = None
        path = str(cfg.config.get("shop_items_file") or "shop_items.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cand = data.get("items") if isinstance(data, dict) else data
                if isinstance(cand, list):
                    items, self.source = cand, path
            except Exception as e:
                log.error(f"[SHOP] {path} unlesbar ({e}) – Fallback auf config.json.")
        if items is None:
            items, self.source = list(cfg.config.get("shop_items", [])), "config.json"
        self.items = [it for it in items
                      if isinstance(it, dict) and (it.get("classname") or it.get("classnames"))]
        self.rebuild_index()
        log.info(f"[SHOP] Katalog geladen: {len(self.items)} Items aus {self.source}")

    def rebuild_index(self):
        self._by_key.clear()
        self.by_category.clear()
        self._ac_index = []
        sym = _cur_symbol()
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
        """Persistiert Änderungen (/shop setprice, /shop enable) in die geladene Quelle."""
        if self.source == "config.json":
            cfg.save_config()
            self.rebuild_index()
            return True
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


catalog = ShopCatalog()


def _find_shop_item(name: str) -> Optional[Dict]:
    """Sucht ein Item per Anzeigename oder Classname (O(1) über den Katalog-Index)."""
    return catalog.find(name)

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
        cur = current.strip().lower()
        out: List[app_commands.Choice] = []
        for search, label, value, enabled in catalog._ac_index:
            if only_enabled and not enabled:
                continue
            if cur and cur not in search:
                continue
            out.append(app_commands.Choice(name=label, value=value))
            if len(out) >= 25:
                break
        return out
    return _ac

_shop_item_autocomplete = _make_item_autocomplete(only_enabled=False)   # Admin-Befehle
_shop_buy_autocomplete  = _make_item_autocomplete(only_enabled=True)    # /buy

async def _shop_category_autocomplete(interaction: discord.Interaction,
                                      current: str) -> List[app_commands.Choice[str]]:
    cur = current.strip().lower()
    out: List[app_commands.Choice] = []
    for cat in sorted(catalog.by_category):
        if cur and cur not in cat.lower():
            continue
        n = sum(1 for i in catalog.by_category[cat] if i.get("enabled", True))
        if n == 0:
            continue
        out.append(app_commands.Choice(name=f"{cat} ({n} items)"[:100], value=cat))
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
@app_commands.describe(category="Category to list – leave empty for the overview")
async def shop_list(interaction: discord.Interaction, category: Optional[str] = None):
    enabled_items = [it for it in catalog.items if it.get("enabled", True)]
    if not enabled_items:
        return await interaction.response.send_message(
            "🛒 The shop is currently empty. Admins: put your `types.xml` next to the bot "
            "and restart (the catalog is generated automatically), or use `/add shopitem`.",
            ephemeral=True)

    if category is not None:
        # Eine Kategorie komplett auflisten
        wanted = category.strip().lower()
        match = next((c for c in catalog.by_category if c.lower() == wanted), None)
        items = ([i for i in catalog.by_category.get(match, []) if i.get("enabled", True)]
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
        for cat in sorted(catalog.by_category):
            items = [i for i in catalog.by_category[cat] if i.get("enabled", True)]
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
async def shop_pending(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    rows = db.pending_purchases()
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
async def shop_cleanup(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    await interaction.response.defer(ephemeral=True)
    if not bot.shop:
        return await interaction.followup.send("❌ Shop manager not ready yet.", ephemeral=True)
    rows = db.pending_purchases()
    ids, names = [], []
    for r in rows:
        ids.append(int(r["id"]))
        try:
            names.extend(json.loads(r["area_names"] or "[]"))
        except Exception:
            pass
    if names:
        ok = await bot.shop.remove_area_entries(names)
        if not ok:
            return await interaction.followup.send(
                "❌ Could not clean cfgEffectArea.json (FTP/parse error) – nothing was changed.",
                ephemeral=True)
    if ids:
        db.mark_delivered(ids)
        bot.shop.cleanup_retry_needed = False

    # Selbstheilung: verwaiste SHOP_-Einträge ohne zugehörigen Kauf entfernen
    orphans = await bot.shop.sweep_orphans()

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
async def shop_check(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    await interaction.response.defer(ephemeral=True)
    if not bot.shop:
        return await interaction.followup.send("❌ Shop manager not ready yet.", ephemeral=True)
    rep = await bot.shop.check_and_heal()

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

    if pending and not cfg.config.get("auto_restart_after_purchase", False):
        embed.add_field(
            name="Hinweis",
            value=("`auto_restart_after_purchase` ist **aus** – Items spawnen erst "
                   "beim nächsten (manuellen/geplanten) Server-Neustart."), inline=False)
    last = rep.get("last_restart_at") or 0
    embed.set_footer(text=("Letzter erkannter Server-Neustart: " +
                           (f"vor {int((time.time() - last) // 60)} Min"
                            if last else "seit Bot-Start keiner")))
    await interaction.followup.send(embed=embed, ephemeral=True)


@shop_group.command(name="setprice", description="💲 Change the price of a shop item (admin)")
@app_commands.describe(item="Item name", price="New price")
async def shop_setprice(interaction: discord.Interaction,
                        item: str, price: app_commands.Range[int, 0]):
    if not _is_admin(interaction):
        return await _deny(interaction)
    it = _find_shop_item(item)
    if not it:
        return await interaction.response.send_message(
            f"❌ Item `{item}` not found in the shop catalog.", ephemeral=True)
    old = int(it.get("price", 0))
    it["price"] = int(price)
    saved = catalog.save()
    note = "" if saved else f"\n⚠️ Could not persist to `{catalog.source}` – change is in memory only."
    await interaction.response.send_message(
        f"💲 **{it['name']}**: {_fmt_money(old)} → **{_fmt_money(int(price))}**{note}",
        ephemeral=True)

shop_setprice.autocomplete("item")(_shop_item_autocomplete)


@shop_group.command(name="enable", description="🔧 Enable or disable a shop item (admin)")
@app_commands.describe(item="Item name", enabled="True = buyable, False = hidden from the shop")
async def shop_enable(interaction: discord.Interaction, item: str, enabled: bool):
    if not _is_admin(interaction):
        return await _deny(interaction)
    it = _find_shop_item(item)
    if not it:
        return await interaction.response.send_message(
            f"❌ Item `{item}` not found in the shop catalog.", ephemeral=True)
    it["enabled"] = bool(enabled)
    saved = catalog.save()
    state = "✅ **enabled**" if enabled else "🚫 **disabled**"
    note  = "" if saved else f"\n⚠️ Could not persist to `{catalog.source}` – change is in memory only."
    await interaction.response.send_message(
        f"🔧 **{it['name']}** is now {state}.{note}", ephemeral=True)

shop_enable.autocomplete("item")(_shop_item_autocomplete)


@shop_group.command(name="removeitem",
                    description="🗑️ Remove an item/bundle from the shop catalog (admin)")
@app_commands.describe(item="Item name")
async def shop_removeitem(interaction: discord.Interaction, item: str):
    if not _is_admin(interaction):
        return await _deny(interaction)
    it = _find_shop_item(item)
    if not it:
        return await interaction.response.send_message(
            f"❌ Item `{item}` not found in the shop catalog.", ephemeral=True)
    try:
        catalog.items.remove(it)
    except ValueError:
        pass
    saved = catalog.save()
    note = "" if saved else f"\n⚠️ Could not persist to `{catalog.source}` – change is in memory only."
    await interaction.response.send_message(
        f"🗑️ **{it.get('name', item)}** was removed from the catalog.{note}", ephemeral=True)

shop_removeitem.autocomplete("item")(_shop_item_autocomplete)

bot.tree.add_command(shop_group)


# ══════════════════════════════════════════════════════════════
#  /add shopitem – Items/Bundles zur Laufzeit in den Katalog
# ══════════════════════════════════════════════════════════════
add_group = app_commands.Group(name="add", description="➕ Add entries to the shop catalog")

@add_group.command(name="shopitem",
                   description="➕ Add an item or bundle to the shop catalog (admin)")
@app_commands.describe(
    classnames="One classname, or several separated by comma/space = bundle (e.g. M4A1, Mag_STANAG_60Rnd)",
    price="Price for the item / the whole bundle",
    name="Display name (optional – default: the classname itself)",
    category="Shop category (optional – default: Custom, bundles: Bundles)",
    max_amount="Max amount per purchase (optional – default: 5, bundles: 1)")
async def add_shopitem(interaction: discord.Interaction, classnames: str,
                       price: app_commands.Range[int, 0],
                       name: Optional[str] = None,
                       category: Optional[str] = None,
                       max_amount: Optional[app_commands.Range[int, 1]] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)

    # Classnames parsen: Komma/Semikolon/Leerzeichen, Reihenfolge behalten, Duplikate raus
    parts: List[str] = []
    seen: set = set()
    for tok in re.split(r"[,;\s]+", classnames.strip()):
        if tok and tok.lower() not in seen:
            seen.add(tok.lower())
            parts.append(tok)
    if not parts:
        return await interaction.response.send_message(
            "❌ No classname given. Example: `M4A1` or `M4A1, Mag_STANAG_60Rnd` for a bundle.",
            ephemeral=True)

    is_bundle = len(parts) > 1
    # Anforderung: der Classname IST der Anzeigename in /shop list (Bundles brauchen einen eigenen)
    display = (name or "").strip() or (f"{parts[0]} Bundle ({len(parts)} items)" if is_bundle else parts[0])
    if catalog.find(display):
        return await interaction.response.send_message(
            f"❌ `{display}` already exists in the catalog. Pick a different `name` or "
            f"remove the existing entry first (`/shop removeitem`).", ephemeral=True)

    # Tippfehler-Schutz VOR dem Einfügen: unbekannte Classnames melden (nicht blockierend)
    unknown = [c for c in parts if catalog.find(c) is None]

    cat = (category or "").strip() or ("Bundles" if is_bundle else "Custom")
    mx  = int(max_amount) if max_amount is not None else (1 if is_bundle else 5)

    it: Dict[str, Any] = {
        "name":               display[:100],
        "price":              int(price),
        "category":           cat,
        "enabled":            True,
        "max_amount_per_buy": mx,
        "custom":             True,   # übersteht die automatische Katalog-Regenerierung
    }
    if is_bundle:
        it["classnames"] = parts
    else:
        it["classname"] = parts[0]

    catalog.items.append(it)
    saved = catalog.save()

    embed = discord.Embed(
        title="➕ Shop bundle added" if is_bundle else "➕ Shop item added",
        description=f"**{display}** — {_fmt_money(int(price))}",
        color=0x2ECC71)
    cls_txt = ", ".join(f"`{c}`" for c in parts)
    if len(cls_txt) > 1000:
        cls_txt = cls_txt[:997] + "…"
    embed.add_field(name="Classnames" if is_bundle else "Classname", value=cls_txt, inline=False)
    embed.add_field(name="Category", value=cat,     inline=True)
    embed.add_field(name="Max/buy",  value=str(mx), inline=True)
    if unknown:
        embed.add_field(
            name="⚠️ Not found in the types.xml catalog",
            value=(", ".join(f"`{c}`" for c in unknown))[:900] +
                  "\nCheck the spelling – an unknown classname will NOT spawn in game.",
            inline=False)
    if not saved:
        embed.add_field(name="⚠️ Warning",
                        value=f"Could not persist to `{catalog.source}` – item is in memory only.",
                        inline=False)
    embed.set_footer(text=f"Catalog: {catalog.source} · buy it with /buy {display[:40]}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

add_shopitem.autocomplete("category")(_shop_category_autocomplete)

bot.tree.add_command(add_group)


# ══════════════════════════════════════════════════════════════
#  /edit shopitem – Classnames, Preis, Name usw. eines Items ändern
# ══════════════════════════════════════════════════════════════
edit_group = app_commands.Group(name="edit", description="✏️ Edit entries of the shop catalog")

@edit_group.command(name="shopitem",
                    description="✏️ Edit a shop item/bundle – change classnames, price, name, … (admin)")
@app_commands.describe(
    item="Item to edit (pick from the autocomplete list)",
    classnames="New classname(s) – several separated by comma/space = bundle (optional)",
    price="New price (optional)",
    name="New display name (optional)",
    category="New category (optional)",
    max_amount="New max amount per purchase (optional)")
async def edit_shopitem(interaction: discord.Interaction, item: str,
                        classnames: Optional[str] = None,
                        price: Optional[app_commands.Range[int, 0]] = None,
                        name: Optional[str] = None,
                        category: Optional[str] = None,
                        max_amount: Optional[app_commands.Range[int, 1]] = None):
    if not _is_admin(interaction):
        return await _deny(interaction)
    it = _find_shop_item(item)
    if not it:
        return await interaction.response.send_message(
            f"❌ Item `{item}` not found in the shop catalog.", ephemeral=True)
    if classnames is None and price is None and name is None \
            and category is None and max_amount is None:
        return await interaction.response.send_message(
            "❌ Nothing to change – set at least one of `classnames`, `price`, "
            "`name`, `category`, `max_amount`.", ephemeral=True)

    changes: List[str] = []
    unknown: List[str] = []

    # ── Classnames ändern (einer = Einzelitem, mehrere = Bundle) ──
    if classnames is not None:
        parts: List[str] = []
        seen: set = set()
        for tok in re.split(r"[,;\s]+", classnames.strip()):
            if tok and tok.lower() not in seen:
                seen.add(tok.lower())
                parts.append(tok)
        if not parts:
            return await interaction.response.send_message(
                "❌ No valid classname given. Example: `M4A1` or `M4A1, Mag_STANAG_60Rnd`.",
                ephemeral=True)
        old_cls = " + ".join(_item_classnames(it)) or "—"
        if len(parts) > 1:
            it["classnames"] = parts
            it.pop("classname", None)
        else:
            it["classname"] = parts[0]
            it.pop("classnames", None)
        # Tippfehler-Schutz: unbekannte Classnames nur melden, nicht blockieren
        unknown = [c for c in parts if catalog.find(c) is None]
        changes.append(f"Classnames: `{old_cls}` → `{' + '.join(parts)}`")

    # ── Preis ─────────────────────────────────────────────────
    if price is not None:
        old_price = int(it.get("price", 0))
        it["price"] = int(price)
        changes.append(f"Price: {_fmt_money(old_price)} → **{_fmt_money(int(price))}**")

    # ── Anzeigename (Kollision mit anderem Eintrag abfangen) ──
    if name is not None:
        new_name = name.strip()[:100]
        if not new_name:
            return await interaction.response.send_message(
                "❌ `name` must not be empty.", ephemeral=True)
        existing = catalog.find(new_name)
        if existing is not None and existing is not it:
            return await interaction.response.send_message(
                f"❌ `{new_name}` is already used by another catalog entry.", ephemeral=True)
        changes.append(f"Name: **{it.get('name', '?')}** → **{new_name}**")
        it["name"] = new_name

    # ── Kategorie / Max-Menge ─────────────────────────────────
    if category is not None and category.strip():
        changes.append(f"Category: {it.get('category', 'Misc')} → **{category.strip()}**")
        it["category"] = category.strip()
    if max_amount is not None:
        changes.append(f"Max/buy: {int(it.get('max_amount_per_buy', 1))} → **{int(max_amount)}**")
        it["max_amount_per_buy"] = int(max_amount)

    saved = catalog.save()   # persistiert + Index/Autocomplete neu aufbauen

    embed = discord.Embed(
        title="✏️ Shop item updated",
        description=f"**{it.get('name', item)}**\n" + "\n".join(f"• {c}" for c in changes),
        color=0x5865F2)
    if unknown:
        embed.add_field(
            name="⚠️ Not found in the types.xml catalog",
            value=(", ".join(f"`{c}`" for c in unknown))[:900] +
                  "\nCheck the spelling – an unknown classname will NOT spawn in game.",
            inline=False)
    if not saved:
        embed.add_field(name="⚠️ Warning",
                        value=f"Could not persist to `{catalog.source}` – change is in memory only.",
                        inline=False)
    embed.set_footer(text=f"Catalog: {catalog.source}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

edit_shopitem.autocomplete("item")(_shop_item_autocomplete)
edit_shopitem.autocomplete("category")(_shop_category_autocomplete)

@edit_group.command(name="ankuendigung",
                    description="✏️ Bearbeitet eine geplante Ankündigung (Nachricht/Bild)")
@app_commands.describe(index="Nummer der Ankündigung (siehe /liste)")
async def edit_ankuendigung(interaction: discord.Interaction, index: int):
    if not _is_admin(interaction):
        return await _deny(interaction)

    if index < 0 or index >= len(ann_data["announcements"]):

        return await interaction.response.send_message(
            "❌ Ungültiger Index",
            ephemeral=True
        )

    modal = EditAnnouncementModal(index)

    await interaction.response.send_modal(modal)

bot.tree.add_command(edit_group)


# ══════════════════════════════════════════════════════════════
#  /bundle add – Bundle über ein Modal anlegen (mehrere Items,
#  ein Kauf). Eingabe pro Zeile: "<Menge>x<Classname>", z. B.
#      1xAKM
#      2xMag_AKM_30Rnd
#  Da Discord-Modals keine Dropdowns erlauben, wird die Kategorie
#  über ein vorgeschaltetes Select-Menü gewählt; danach öffnet
#  das Modal mit den restlichen Feldern.
# ══════════════════════════════════════════════════════════════
MAX_BUNDLE_PIECES = 60   # Sicherheitslimit: so viele Einzelstücke max. pro Bundle

# Zeilenformat: optionaler Mengen-Präfix "<zahl>x" / "<zahl> * " vor dem Classname.
_BUNDLE_LINE_RE = re.compile(r"^\s*(?:(\d+)\s*[x×*]\s*)?(\S.*?)\s*$", re.IGNORECASE)


def _parse_bundle_items(text: str) -> Tuple[List[str], List[Tuple[int, str]], List[str]]:
    """Parst die Modal-Eingabe (eine Zeile je Position, Format ``2xClassname``).

    Rückgabe: (expanded, summary, errors)
      * expanded – Classname-Liste MIT Wiederholung (2xMag → zweimal Mag);
        genau diese Liste landet als ``classnames`` im Katalog und spawnt
        pro Eintrag ein Stück (siehe ShopManager.add_purchase_entries).
      * summary  – [(menge, classname)] je Zeile, für die Bestätigungsanzeige.
      * errors   – menschenlesbare Fehler (leere/kaputte Zeilen, Menge <1)."""
    expanded: List[str] = []
    summary: List[Tuple[int, str]] = []
    errors: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _BUNDLE_LINE_RE.match(line)
        if not m:
            errors.append(f"`{line[:60]}` – not understood (use `2xClassname`).")
            continue
        count_txt, classname = m.group(1), m.group(2).strip()
        # Nur der reine Classname (keine Leerzeichen) – nimm das erste Token.
        classname = classname.split()[0] if classname else ""
        if not classname:
            errors.append(f"`{line[:60]}` – no classname found.")
            continue
        try:
            count = int(count_txt) if count_txt else 1
        except ValueError:
            count = 1
        if count < 1:
            errors.append(f"`{line[:60]}` – amount must be at least 1.")
            continue
        summary.append((count, classname))
        expanded.extend([classname] * count)
    return expanded, summary, errors


def _bundle_category_options(preselect: str = "Bundles") -> List[discord.SelectOption]:
    """Kategorie-Optionen fürs Dropdown: 'Bundles' zuerst, dann bestehende
    Katalog-Kategorien (Discord erlaubt max. 25 Optionen)."""
    opts: List[discord.SelectOption] = [
        discord.SelectOption(label="Bundles", value="Bundles", emoji="📦",
                             description="Default category for bundles",
                             default=(preselect == "Bundles")),
    ]
    for cat in sorted(catalog.by_category):
        if cat.lower() == "bundles":
            continue
        opts.append(discord.SelectOption(label=cat[:100], value=cat[:100],
                                         default=(cat == preselect)))
        if len(opts) >= 25:
            break
    return opts


class BundleAddModal(discord.ui.Modal, title="📦 Create shop bundle"):
    """Modal mit den Bundle-Feldern. Die Kategorie kommt aus dem vorgelagerten
    Dropdown und wird hier nur noch mitgeführt."""

    def __init__(self, category: str):
        super().__init__()
        self.category = category or "Bundles"
        self.items_in = discord.ui.TextInput(
            label="Items – one per line: 2xClassname",
            style=discord.TextStyle.paragraph,
            placeholder="1xAKM\n2xMag_AKM_30Rnd\n1xNVGoggles",
            required=True, max_length=1500)
        self.name_in = discord.ui.TextInput(
            label="Name in the shop",
            placeholder="e.g. AKM Starter Bundle",
            required=True, max_length=100)
        self.price_in = discord.ui.TextInput(
            label="Price (for the whole bundle)",
            placeholder="e.g. 2500", required=True, max_length=15)
        self.max_in = discord.ui.TextInput(
            label="Max amount per purchase",
            placeholder="1", required=False, default="1", max_length=6)
        for comp in (self.items_in, self.name_in, self.price_in, self.max_in):
            self.add_item(comp)

    async def on_submit(self, interaction: discord.Interaction):
        # Admin-Recht erneut prüfen (Modal kann verzögert abgeschickt werden)
        if not _is_admin(interaction):
            return await _deny(interaction)

        # ── Items parsen ─────────────────────────────────────────
        expanded, summary, errors = _parse_bundle_items(str(self.items_in.value))
        if not summary:
            return await interaction.response.send_message(
                "❌ No valid items. Enter one per line, e.g. `1xAKM` or "
                "`2xMag_AKM_30Rnd`." +
                ("\n" + "\n".join(f"• {e}" for e in errors[:5]) if errors else ""),
                ephemeral=True)
        if len(expanded) > MAX_BUNDLE_PIECES:
            return await interaction.response.send_message(
                f"❌ Too many pieces ({len(expanded)}). A bundle may contain at "
                f"most **{MAX_BUNDLE_PIECES}** individual items.", ephemeral=True)

        # ── Preis parsen (Tausenderpunkte/-kommas tolerieren) ────
        price_digits = re.sub(r"[^\d]", "", str(self.price_in.value))
        if not price_digits:
            return await interaction.response.send_message(
                "❌ Price must be a whole number, e.g. `2500`.", ephemeral=True)
        price = int(price_digits)

        # ── Max-Menge parsen ─────────────────────────────────────
        max_digits = re.sub(r"[^\d]", "", str(self.max_in.value or "")) or "1"
        max_amount = max(1, int(max_digits))

        # ── Name / Kollision ─────────────────────────────────────
        display = str(self.name_in.value).strip()[:100]
        if not display:
            return await interaction.response.send_message(
                "❌ The shop name must not be empty.", ephemeral=True)
        if catalog.find(display):
            return await interaction.response.send_message(
                f"❌ `{display}` already exists in the catalog. Pick a different "
                f"name or remove the existing entry first (`/shop removeitem`).",
                ephemeral=True)

        # Tippfehler-Schutz: unbekannte Classnames melden (nicht blockierend)
        seen_unknown: set = set()
        unknown: List[str] = []
        for _, cn in summary:
            if catalog.find(cn) is None and cn.lower() not in seen_unknown:
                seen_unknown.add(cn.lower())
                unknown.append(cn)

        # ── Katalog-Eintrag bauen (immer als Bundle: classnames-Liste) ──
        it: Dict[str, Any] = {
            "name":               display,
            "price":              price,
            "category":           self.category,
            "enabled":            True,
            "max_amount_per_buy": max_amount,
            "classnames":         expanded,
            "custom":             True,   # übersteht die Katalog-Regenerierung
        }
        catalog.items.append(it)
        saved = catalog.save()

        # ── Bestätigung ──────────────────────────────────────────
        sym = _cur_symbol()
        total_pieces = len(expanded)
        embed = discord.Embed(
            title="📦 Shop bundle added",
            description=f"**{display}** — {_fmt_money(price)}",
            color=0x2ECC71)
        content_lines = [f"• **{cnt}×** `{cn}`" for cnt, cn in summary]
        content_txt = "\n".join(content_lines)
        if len(content_txt) > 1000:
            content_txt = content_txt[:997] + "…"
        embed.add_field(name=f"Contents ({total_pieces} pieces)",
                        value=content_txt, inline=False)
        embed.add_field(name="Category", value=self.category, inline=True)
        embed.add_field(name="Max/buy",  value=str(max_amount), inline=True)
        if unknown:
            embed.add_field(
                name="⚠️ Not found in the types.xml catalog",
                value=(", ".join(f"`{c}`" for c in unknown))[:900] +
                      "\nCheck the spelling – an unknown classname will NOT spawn in game.",
                inline=False)
        if not saved:
            embed.add_field(
                name="⚠️ Warning",
                value=f"Could not persist to `{catalog.source}` – bundle is in memory only.",
                inline=False)
        embed.set_footer(text=f"Catalog: {catalog.source} · buy it with /buy {display[:40]}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error(f"[SHOP] Bundle-Modal-Fehler: {error}")
        msg = "❌ Something went wrong while creating the bundle. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class BundleCategorySelect(discord.ui.Select):
    """Dropdown zur Kategorie-Wahl; öffnet danach das Bundle-Modal."""

    def __init__(self):
        super().__init__(placeholder="📂 Choose a category for the bundle…",
                         min_values=1, max_values=1,
                         options=_bundle_category_options())

    async def callback(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await _deny(interaction)
        await interaction.response.send_modal(BundleAddModal(self.values[0]))


class BundleCategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(BundleCategorySelect())


bundle_group = app_commands.Group(
    name="bundle", description="📦 Create shop bundles (several items, one purchase)")


@bundle_group.command(
    name="add",
    description="📦 Create a bundle via a form: several items, sold as one purchase (admin)")
async def bundle_add(interaction: discord.Interaction):
    if not _is_admin(interaction):
        return await _deny(interaction)
    embed = discord.Embed(
        title="📦 Create a shop bundle",
        description=("Choose a **category** below – then a form opens where you "
                     "enter the items, name, price and max amount.\n\n"
                     "**Items:** one per line as `<amount>x<classname>`, e.g.\n"
                     "```\n1xAKM\n2xMag_AKM_30Rnd\n1xNVGoggles\n```\n"
                     "All items of the bundle spawn together on a single `/buy`."),
        color=0x5865F2)
    await interaction.response.send_message(
        embed=embed, view=BundleCategoryView(), ephemeral=True)


bot.tree.add_command(bundle_group)


@bot.tree.command(
    name="buy",
    description="🛒 Buy an item – it spawns at your coordinates after the next server restart")
@app_commands.describe(
    item="Item name (pick from the autocomplete list)",
    amount="How many to buy",
    x="iZurvive X coordinate (East – the FIRST number on iZurvive)",
    z="iZurvive Y coordinate (North – the SECOND number on iZurvive)",
    y="Height / altitude (OPTIONAL – leave empty for default ground level)")
async def cmd_buy(interaction: discord.Interaction, item: str,
                  amount: app_commands.Range[int, 1], x: float, z: float,
                  y: Optional[float] = None):
    if not await _require_guild(interaction):
        return
    gid, uid = interaction.guild_id, interaction.user.id

    # ── 1. Item & Menge validieren ────────────────────────────
    it = _find_shop_item(item)
    if not it or not it.get("enabled", True):
        return await interaction.response.send_message(
            f"❌ Item `{item}` is not available. Use `/shop list` to see the catalog.",
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
    y_val = float(cfg.config.get("default_pos_y", 0.0)) if y is None else float(y)
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
    if not bot.shop:
        return await interaction.followup.send(
            "❌ Shop system is still starting up – try again in a moment.", ephemeral=True)

    # ── 4. Erst in cfgEffectArea.json schreiben ... ───────────
    ok, err, area_names = await bot.shop.add_purchase_entries(
        cls_list, int(amount), x, y_val, z)
    if not ok:
        return await interaction.followup.send(
            embed=discord.Embed(title="❌ Purchase failed", description=err, color=0xE74C3C),
            ephemeral=True)

    # ── 5. ... dann Geld abbuchen (atomar). Bei Fehlschlag: Rollback ──
    if not db.try_spend_wallet(gid, uid, total):
        rollback_ok = await bot.shop.remove_area_entries(area_names)   # Einträge zurückrollen
        if not rollback_ok:
            # Verwaiste Einträge würden bei jedem Neustart gratis spawnen → Admins warnen
            log.error(f"[SHOP] Rollback fehlgeschlagen – verwaiste Areas: {area_names}")
            warn = discord.Embed(
                title="⚠️ Orphaned shop entries",
                description=("A cancelled purchase could not be rolled back in "
                             "`cfgEffectArea.json`. Run `/shop cleanup` to remove the "
                             "orphaned entries, otherwise the items respawn on every restart."),
                color=0xE67E22)
            await _post_feed(gid, "shop_log", warn)
        wallet, _bank = db.get_balance(gid, uid)
        return await interaction.followup.send(
            embed=_insufficient_embed(total, wallet), ephemeral=True)

    # ── 6. Kauf als pending speichern ─────────────────────────
    purchase_id = db.create_purchase(
        gid, uid, str(interaction.user), it["name"], "+".join(cls_list),
        int(amount), total, x, y_val, z, area_names)

    # ── 7. Auto-Restart oder Hinweis auf nächsten Neustart ────
    if cfg.config.get("auto_restart_after_purchase", False):
        bot.shop.schedule_auto_restart()
        cooldown = int(cfg.config.get("restart_cooldown_seconds", 300))
        delivery_info = (f"🔄 A server restart has been scheduled – your items will spawn "
                         f"in about **{max(5, cooldown)} seconds** (plus boot time).")
    else:
        delivery_info = "⏳ Your items will spawn at the **next scheduled server restart**."

    # ── 8. Bestätigung an den Käufer (Ort + iZurvive-Link) ────
    map_name = cfg.config.get("map_name", "ChernarusPlus")
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
    await _post_feed(gid, "shop_log", feed)

cmd_buy.autocomplete("item")(_shop_buy_autocomplete)


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
                                   output_path: Optional[str] = None) -> Optional[int]:
    """Erzeugt shop_items.json aus einer types.xml (DayZBoosterZ-Format).
    Kategorie-Preise kommen aus shop_category_prices in config.json.
    Per /add shopitem angelegte Items ("custom": true) werden übernommen.
    Gibt die Item-Anzahl zurück, None bei Fehler."""
    out_file = output_path or str(cfg.config.get("shop_items_file") or "shop_items.json")
    if not os.path.exists(input_path):
        return None

    prices        = cfg.config.get("shop_category_prices") or {}
    default_price = int(cfg.config.get("shop_default_price", 100))

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


def _apply_gameserver_info(info: Dict) -> None:
    """Schreibt FTP-Zugang, aktuelle Karte und (falls leer) Server-IP/Query-Port
    aus den Nitrado-Gameserver-Infos in cfg.config. Speichert NICHT selbst."""
    ftp = NitradoAPI.extract_ftp_credentials(info)
    if ftp:
        # Immer überschreiben – fängt von Nitrado geänderte Passwörter ab
        cfg.config["ftp_host"]     = ftp["host"]
        cfg.config["ftp_port"]     = ftp["port"]
        cfg.config["ftp_user"]     = ftp["user"]
        cfg.config["ftp_password"] = ftp["password"]
        log.info(f"[NITRADO] ✅ FTP-Zugang automatisch erkannt: "
                 f"{ftp['user']}@{ftp['host']}:{ftp['port']}")
    else:
        log.warning("[NITRADO] ⚠️ Keine FTP-Zugangsdaten in den "
                    "Gameserver-Infos gefunden.")

    detected_map = NitradoAPI.extract_map(info)
    if detected_map and detected_map != cfg.config.get("map_name"):
        log.info(f"[NITRADO] 🗺️ Aktuelle Karte erkannt: {detected_map} "
                 f"(vorher: {cfg.config.get('map_name')})")
        cfg.config["map_name"] = detected_map

    # Bonus: Server-IP/Query-Port nur befüllen, wenn noch nicht gesetzt
    if not cfg.config.get("server_ip") and info.get("ip"):
        cfg.config["server_ip"] = str(info["ip"])
        qport = (info.get("query") or {}).get("connect_port") or info.get("query_port")
        if qport:
            try:
                cfg.config["query_port"] = int(qport)
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


def _asset_response(rel: str, fallback_text: str = "") -> web.Response:
    data = _read_asset(rel)
    if data is None:
        if fallback_text:
            return web.Response(text=fallback_text, status=500)
        return web.Response(status=404)
    ctype, _enc = mimetypes.guess_type(rel)
    ctype = ctype or "application/octet-stream"
    textish = ctype.startswith("text/") or ctype in (
        "application/javascript", "application/json")
    return web.Response(body=data, content_type=ctype,
                        charset="utf-8" if textish else None)


_ASSET_MANIFEST = ".assets.json"


def _asset_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def _ev_record(ev: Dict[str, Any], player_positions: Optional[Dict[str, Any]] = None) -> None:
    """Ein geparstes Event in den Ringpuffer aufnehmen (aus ``_dispatch``)."""
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
             limit: int = 500) -> Dict[str, Any]:
    """Aktuelle Events (optional nur neuere / bestimmte Typen) für die API."""
    tset = set(types) if types else None
    with _EV_LOCK:
        items = [e for e in _EV_BUF
                 if e["id"] > since_id and (tset is None or e["type"] in tset)]
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


def _sess_prune() -> None:
    now = time.time()
    for sid in [s for s, v in _SESS_STORE.items() if now - v.get("seen", 0) > _SESS_TTL]:
        _SESS_STORE.pop(sid, None)


def _sess_create(token: str, gameservers: list,
                 discord_user: Optional[Dict[str, Any]] = None,
                 is_admin: bool = False) -> str:
    """Neue Session anlegen.

    Sie entsteht jetzt schon beim Discord-Login – also bevor ein Nitrado-Token
    vorliegt. ``token`` ist dann leer und wird von post_token nachgetragen.
    """
    _sess_prune()
    sid = secrets.token_urlsafe(32)
    now = time.time()
    _SESS_STORE[sid] = {
        "token": token,
        "gameservers": gameservers,
        "service_id": None,
        "map_name": None,
        "discord": discord_user,
        "is_admin": bool(is_admin),
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


def _sess_cookie(response: web.Response, sid: str) -> None:
    response.set_cookie(_SESS_COOKIE, sid, httponly=True, samesite="Lax",
                        max_age=_SESS_TTL, path="/")


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
        # Der Nitrado-Token bleibt das eigentliche Tor: eine Session, die nur
        # vom Discord-Login stammt, darf hier noch nicht durch.
        if not sess or not sess.get("token"):
            return web.json_response(
                {"error": "unauthorized",
                 "message": "Bitte zuerst den Nitrado-Token eingeben."},
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


def require_nitrado():
    """Gibt (nitrado, None) zurück oder (None, Fehlerantwort), wenn nicht eingerichtet."""
    nit = getattr(bot, "nitrado", None) if bot else None
    if not nit or not str(getattr(nit, "service_id", "") or "").strip():
        return None, err("Nitrado-Server ist noch nicht eingerichtet.", 409)
    return nit, None


# ──────────────────────────────────────────────────────────────────────────
#  Onboarding: Nitrado-Token prüfen → Server wählen → Karte erkennen.
#
#  Spiegelt den Discord-Flow ``/setup token`` (bot.py ~3003) +
#  ``_finish_token_setup`` (bot.py ~2879), nur über HTTP.
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


async def _discord_fetch_user(code: str, redirect_uri: str) -> Optional[dict]:
    """Code gegen Zugriffstoken tauschen und das Discord-Profil holen.

    Der Zugriffstoken bleibt hier im Prozess – er wird nicht gespeichert und
    erreicht den Browser nie.
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
                    return None
                tok = await r.json()
            access = tok.get("access_token")
            if not access:
                return None
            async with s.get(f"{_DISCORD_API}/users/@me",
                             headers={"Authorization": f"Bearer {access}"}) as r:
                if r.status != 200:
                    dash_log.warning(f"[LOGIN] Profil nicht abrufbar ({r.status}).")
                    return None
                return await r.json()
    except Exception as e:  # noqa: BLE001
        dash_log.warning(f"[LOGIN] Discord nicht erreichbar: {e}")
        return None


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
    return {
        "id": uid,
        "name": name,
        "avatar_url": (f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png?size=64"
                       if avatar and uid else None),
    }


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
           f"&response_type=code&scope=identify&state={state}")
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

    user = await _discord_fetch_user(code, _oauth_redirect_uri(request))
    if not user or not user.get("id"):
        return _back("failed")

    view = _discord_user_view(user)
    is_admin = await _discord_user_is_admin(int(user["id"]))
    sid = _sess_create("", [], discord_user=view, is_admin=is_admin)
    resp = web.HTTPFound("/")
    _sess_cookie(resp, sid)
    _audit_add("dashboard", f"{view['name']} ({view['id']})", "Am Dashboard angemeldet",
               "mit Admin-Rolle" if is_admin else "ohne Admin-Rolle")
    dash_log.info(f"[LOGIN] {view['name']} ({view['id']}) angemeldet "
                  f"– Admin: {'ja' if is_admin else 'nein'}")
    return resp


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
    return f"{method} {path}"


def _audit_actor(sess: Optional[Dict[str, Any]]) -> str:
    user = (sess or {}).get("discord") or {}
    if user.get("id"):
        return f"{user.get('name')} ({user.get('id')})"
    return "Dashboard (ohne Discord-Anmeldung)"


def _require_admin(request: web.Request) -> Optional[web.Response]:
    """None = darf. Sonst die fertige Fehlerantwort."""
    sess = _sess_get(request)
    if not sess:
        return err("Session abgelaufen – bitte neu anmelden.", 401)
    if not sess.get("is_admin"):
        return err("Dafür fehlt dir die nötige Discord-Rolle.", 403)
    return None


async def api_audit(request: web.Request) -> web.Response:
    denied = _require_admin(request)
    if denied is not None:
        return denied
    entries = list(_audit_log)
    entries.reverse()                       # neueste zuerst
    return ok({"entries": entries, "max": _AUDIT_MAX})


async def api_admin_guilds(request: web.Request) -> web.Response:
    """Alle verbundenen Discord-Server mit Namen – für die Kategorie Guild IDs."""
    denied = _require_admin(request)
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
            "feeds": len(cfg.guilds.get(str(gid), {}) or {}),
        })
    return ok({"guilds": out, "bot_online": bool(
        bot is not None and getattr(bot, "user", None) is not None)})


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
        _sess_cookie(resp, _sess_create(token, gameservers))
    return resp


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

    # --- Kern von _finish_token_setup (ohne Discord-Embed) ---
    old_service = str(cfg.config.get("service_id") or "").strip()
    cfg.config["nitrado_token"] = token
    cfg.config["service_id"] = service_id
    if old_service and old_service != service_id:
        for k in ("ftp_log_dir", "ftp_ban_file", "ftp_profile_dir",
                  "ftp_mission_dir", "cfg_effect_area_path", "server_ip"):
            cfg.config[k] = ""
        cfg.log_state.pop("current", None)
        cfg.save_log_state()

    base = cfg.config.get("nitrado_api_base", "https://api.nitrado.net")
    api = NitradoAPI(token=token, service_id=service_id, base=base)
    warnings = []
    try:
        info = await api.get_info()
    finally:
        await api.close()
    if info and _apply:
        _apply(info)
    else:
        warnings.append("Gameserver-Infos konnten nicht geladen werden – "
                        "FTP/Karte evtl. nicht erkannt.")
    cfg.save_config()

    # Nitrado/FTP/Shop live neu initialisieren (inkl. FTP-Auto-Discovery)
    try:
        await bot.init_nitrado(force=True)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"Init-Warnung: {e}")

    if not cfg.config.get("ftp_host"):
        warnings.append("Keine FTP-Zugangsdaten gefunden – Log-Feeds & "
                        "Shop-Lieferung funktionieren so nicht.")

    sess["service_id"] = service_id
    sess["map_name"] = cfg.config.get("map_name")
    return ok({
        "service_id": service_id,
        "map_name": cfg.config.get("map_name"),
        "ftp_host": cfg.config.get("ftp_host") or None,
        "log_dir": cfg.config.get("ftp_log_dir") or None,
        "server_ip": cfg.config.get("server_ip") or None,
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


async def post_setup_guild(request: web.Request) -> web.Response:
    """Guild-ID speichern und die Befehle sofort registrieren.

    Wird vom „Ja, habe ich"-Button erneut aufgerufen: der Aufruf ist bewusst
    wiederholbar, damit nach dem Einladen einfach noch einmal geprüft wird.
    """
    sess = _sess_get(request)
    if not sess:
        return err("Session abgelaufen – bitte Token erneut eingeben.", 401)
    data = await body(request)
    raw = str(data.get("guild_id", "")).strip()
    if not raw.isdigit() or not (17 <= len(raw) <= 20):
        return err("Das sieht nicht nach einer Discord-Server-ID aus. Sie besteht nur "
                   "aus Ziffern (Discord → Einstellungen → Erweitert → Entwicklermodus, "
                   "dann Rechtsklick auf den Server → Server-ID kopieren).")
    gid = int(raw)
    if gid in _PLACEHOLDER_GUILD_IDS:
        return err("Das ist die Beispiel-ID aus der Anleitung, nicht die deines Servers.")

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
                   "discord": discord_user, "is_admin": False})
    return ok({
        "authed": bool(sess.get("token")),
        "discord_login": login_required,
        "discord": discord_user,
        "is_admin": bool(sess.get("is_admin")),
        "service_id": sess.get("service_id") or cfg.config.get("service_id"),
        "map_name": sess.get("map_name") or cfg.config.get("map_name"),
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
def _guild_payload(gid: int) -> dict:
    g = bot.get_guild(int(gid)) if bot else None
    channels = []
    if g is not None:
        for ch in getattr(g, "text_channels", []):
            channels.append({"id": str(ch.id), "name": ch.name,
                             "category": getattr(ch.category, "name", None)})
        channels.sort(key=lambda c: (c["category"] or "", c["name"].lower()))
    feeds = {k: str(v) for k, v in (cfg.guilds.get(str(gid), {}) or {}).items()
             if isinstance(v, int) or (isinstance(v, str) and str(v).isdigit())}
    return {
        "id": str(gid),
        "name": (g.name if g is not None else f"Guild {gid}"),
        "available": g is not None,
        "channels": channels,
        "feeds": feeds,
    }


async def get_feeds(request: web.Request) -> web.Response:
    guild_ids = list(cfg.config.get("guild_ids", []) or [])
    for gid in cfg.guilds.keys():
        try:
            if int(gid) not in [int(x) for x in guild_ids]:
                guild_ids.append(int(gid))
        except (TypeError, ValueError):
            continue
    return ok({
        "log_types": [{"key": k, "label": v} for k, v in LOG_TYPES.items()],
        "guilds": [_guild_payload(int(gid)) for gid in guild_ids],
    })


async def set_feed(request: web.Request) -> web.Response:
    gid = request.match_info["guild_id"]
    log_type = request.match_info["log_type"]
    if log_type not in LOG_TYPES:
        return err(f"Unbekannter Feed-Typ: {log_type}")
    data = await body(request)
    channel_id = data.get("channel_id")
    guilds = cfg.guilds.setdefault(str(gid), {})
    if channel_id in (None, "", "0"):
        guilds.pop(log_type, None)
        cfg.save_guilds()
        return ok({"cleared": True})
    try:
        cfg.set_channel(int(gid), log_type, int(channel_id))
    except (TypeError, ValueError):
        return err("Ungültige Channel-ID.")
    return ok({"log_type": log_type, "channel_id": str(channel_id)})


# ──────────────────────────────────────────────────────────────────────────
#  Zonen verwalten (wie ``/zone create|edit|remove|list`` + Allowlist).
#
#  Zonen liegen in ``config.json["zones"]``. Schema:
#  ``{name, x, z, radius, role_id?, channel_id?, guild_id, allowlist?}`` –
#  x = Ost (iZurvive), z = Nord.
# ──────────────────────────────────────────────────────────────────────────
def _find(name: str):
    n = (name or "").strip().lower()
    for z in _zones():
        if isinstance(z, dict) and str(z.get("name", "")).lower() == n:
            return z
    return None


def _default_guild() -> int:
    gids = cfg.config.get("guild_ids", []) or []
    return int(gids[0]) if gids else 0


async def list_zones(request: web.Request) -> web.Response:
    zones = [z for z in _zones() if isinstance(z, dict) and z.get("name")]
    return ok({"zones": zones, "map_name": cfg.config.get("map_name", "ChernarusPlus")})


async def create_zone(request: web.Request) -> web.Response:
    data = await body(request)
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 60:
        return err("Zonen-Name fehlt oder ist länger als 60 Zeichen.")
    if _find(name):
        return err(f"Zone '{name}' existiert bereits.")
    try:
        x = float(data["x"]); z = float(data["z"]); radius = float(data["radius"])
    except (KeyError, TypeError, ValueError):
        return err("x, z und radius müssen Zahlen sein.")

    validate = _validate_zone_geometry
    if validate:
        geo_err = validate(x, z, radius)
        if geo_err:
            return err(geo_err.replace("❌", "").strip())

    zone = {
        "name": name,
        "x": round(x, 1), "z": round(z, 1), "radius": round(radius, 1),
        "role_id": int(data["role_id"]) if data.get("role_id") else None,
        "channel_id": int(data["channel_id"]) if data.get("channel_id") else None,
        "guild_id": int(data.get("guild_id") or _default_guild()),
    }
    _zones().append(zone)
    cfg.save_config()
    return ok(zone)


async def update_zone(request: web.Request) -> web.Response:
    zone = _find(request.match_info["name"])
    if not zone:
        return err("Zone nicht gefunden.", 404)
    data = await body(request)

    new_name = str(data.get("name", zone["name"])).strip() or zone["name"]
    if new_name.lower() != str(zone["name"]).lower() and _find(new_name):
        return err(f"Zone '{new_name}' existiert bereits.")
    try:
        x = float(data.get("x", zone["x"]))
        z = float(data.get("z", zone["z"]))
        radius = float(data.get("radius", zone["radius"]))
    except (TypeError, ValueError):
        return err("x, z und radius müssen Zahlen sein.")

    validate = _validate_zone_geometry
    if validate:
        geo_err = validate(x, z, radius)
        if geo_err:
            return err(geo_err.replace("❌", "").strip())

    zone["name"] = new_name
    zone["x"], zone["z"], zone["radius"] = round(x, 1), round(z, 1), round(radius, 1)
    if "role_id" in data:
        zone["role_id"] = int(data["role_id"]) if data.get("role_id") else None
    if "channel_id" in data:
        zone["channel_id"] = int(data["channel_id"]) if data.get("channel_id") else None
    cfg.save_config()
    return ok(zone)


async def delete_zone(request: web.Request) -> web.Response:
    zone = _find(request.match_info["name"])
    if not zone:
        return err("Zone nicht gefunden.", 404)
    name = str(zone["name"])
    _zones().remove(zone)
    cfg.save_config()
    # Ping-Cooldown-Status im Bot zurücksetzen (wie /zone remove), falls vorhanden
    reset = _reset_zone_state
    if callable(reset):
        try:
            reset(name)
        except Exception:
            pass
    return ok({"removed": name})


# ── Allowlist ─────────────────────────────────────────────────
async def get_allowlist(request: web.Request) -> web.Response:
    zone = _find(request.match_info["name"])
    if not zone:
        return err("Zone nicht gefunden.", 404)
    return ok({"allowlist": zone.get("allowlist", [])})


async def add_allowlist(request: web.Request) -> web.Response:
    zone = _find(request.match_info["name"])
    if not zone:
        return err("Zone nicht gefunden.", 404)
    data = await body(request)
    player = str(data.get("player", "")).strip()
    if not player:
        return err("Spielername fehlt.")
    al = zone.setdefault("allowlist", [])
    if player.lower() not in [str(p).lower() for p in al]:
        al.append(player)
        cfg.save_config()
    return ok({"allowlist": al})


async def remove_allowlist(request: web.Request) -> web.Response:
    zone = _find(request.match_info["name"])
    if not zone:
        return err("Zone nicht gefunden.", 404)
    player = request.match_info["player"]
    al = zone.get("allowlist", [])
    zone["allowlist"] = [p for p in al if str(p).lower() != player.lower()]
    cfg.save_config()
    return ok({"allowlist": zone["allowlist"]})


# ── Rollen/Channels für Picker ────────────────────────────────
async def guild_roles(request: web.Request) -> web.Response:
    g = bot.get_guild(int(request.match_info["guild_id"])) if bot else None
    if g is None:
        return ok({"roles": []})
    roles = [{"id": str(r.id), "name": r.name}
             for r in g.roles if not r.is_default()]
    return ok({"roles": roles})


async def guild_channels(request: web.Request) -> web.Response:
    g = bot.get_guild(int(request.match_info["guild_id"])) if bot else None
    if g is None:
        return ok({"channels": []})
    channels = [{"id": str(c.id), "name": c.name} for c in g.text_channels]
    return ok({"channels": channels})


# ──────────────────────────────────────────────────────────────────────────
#  Auto-Aufgaben: geplante Server-Neustarts (wie ``/auto restart|off|status``).
# ──────────────────────────────────────────────────────────────────────────
def _next_run():
    """Nächster geplanter Restart-Zeitpunkt. Die Funktion ist eine METHODE der
    Bot-Instanz (nicht des Moduls), daher am Bot-Objekt holen."""
    fn = getattr(bot, "_next_scheduled_restart", None)
    if callable(fn):
        try:
            return fn()
        except Exception:
            return None
    return None


async def get_auto_restart(request: web.Request) -> web.Response:
    sched = cfg.config.get("auto_restart_schedule",
                           {"enabled": False, "first_time": "04:00", "interval_hours": 4})
    nxt = _next_run()
    return ok({
        "schedule": sched,
        "next_run_ts": nxt,
        "after_purchase": bool(cfg.config.get("auto_restart_after_purchase", False)),
        "restart_cooldown_seconds": int(cfg.config.get("restart_cooldown_seconds", 300)),
    })


async def set_auto_restart(request: web.Request) -> web.Response:
    data = await body(request)
    sched = dict(cfg.config.get("auto_restart_schedule",
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

    cfg.config["auto_restart_schedule"] = sched

    if "after_purchase" in data:
        cfg.config["auto_restart_after_purchase"] = bool(data["after_purchase"])

    cfg.save_config()
    # Angekündigte Restarts zurücksetzen, damit die neue Zeit sauber greift
    try:
        bot._restart_announced.clear()
    except Exception:
        pass

    return ok({"schedule": sched, "next_run_ts": _next_run()})


# ──────────────────────────────────────────────────────────────────────────
#  Shop-Katalog: Items & Bundles anlegen/bearbeiten, Autofill der Classnames.
#
#  Nutzt den vorhandenen ``ShopCatalog`` (``catalog``). Ein "Bundle" ist ein Item
#  mit mehreren ``classnames``; ein Einzelitem hat ``classname``. Persistiert wie
#  der Bot über ``catalog.save()`` – bei Quelle ``config.json`` wird zusätzlich
#  ``config["shop_items"]`` gespiegelt, damit auch Hinzufügen/Löschen dauerhaft ist.
# ──────────────────────────────────────────────────────────────────────────
def _classnames(it: dict) -> List[str]:
    fn = _item_classnames
    if callable(fn):
        return fn(it)
    cls = it.get("classnames")
    if isinstance(cls, list) and cls:
        return [str(c) for c in cls]
    return [str(it["classname"])] if it.get("classname") else []


def _shop_persist() -> bool:
    """catalog.save() + bei config.json-Quelle die Liste spiegeln."""
    if getattr(catalog, "source", "") == "config.json":
        cfg.config["shop_items"] = catalog.items
    return bool(catalog.save())


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
    }


async def list_items(request: web.Request) -> web.Response:
    q = request.query.get("q", "").strip().lower()
    category = request.query.get("category", "").strip()
    try:
        page = max(1, int(request.query.get("page", 1)))
        page_size = min(200, max(1, int(request.query.get("page_size", 50))))
    except ValueError:
        page, page_size = 1, 50

    items = catalog.items
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
               "source": getattr(catalog, "source", "?")})


async def api_shop_categories(request: web.Request) -> web.Response:
    counts = {k: len(v) for k, v in getattr(catalog, "by_category", {}).items()}
    names = set(counts)
    names.update((cfg.config.get("shop_category_prices") or {}).keys())
    names.update(cfg.config.get("shop_categories_custom", []) or [])
    cats = [{"name": n, "count": counts.get(n, 0),
             "default_price": (cfg.config.get("shop_category_prices") or {}).get(n)}
            for n in sorted(names, key=str.lower)]
    return ok({"categories": cats,
               "default_price": int(cfg.config.get("shop_default_price", 100))})


async def api_shop_classnames(request: web.Request) -> web.Response:
    """Autofill: Classnames (Einzelitems) per Substring-Suche."""
    q = request.query.get("q", "").strip().lower()
    limit = min(50, max(1, int(request.query.get("limit", 25) or 25)))
    out = []
    seen = set()
    for it in catalog.items:
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


async def create_item(request: web.Request) -> web.Response:
    data = await body(request)
    parts = _split_classnames(data.get("classnames") or data.get("classname"))
    if not parts:
        return err("Mindestens einen Classname angeben (z. B. M4A1 oder "
                   "M4A1, Mag_STANAG_60Rnd für ein Bundle).")
    is_bundle = len(parts) > 1
    display = str(data.get("name", "")).strip() or (
        f"{parts[0]} Bundle ({len(parts)} items)" if is_bundle else parts[0])
    if catalog.find(display):
        return err(f"'{display}' existiert bereits im Katalog. Anderen Namen wählen "
                   f"oder den Eintrag zuerst löschen.")

    try:
        price = int(data.get("price"))
    except (TypeError, ValueError):
        # Kategorie-Standardpreis als Fallback
        cat_prices = cfg.config.get("shop_category_prices") or {}
        price = int(cat_prices.get(str(data.get("category", "")).strip(),
                                   cfg.config.get("shop_default_price", 100)))
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
    if is_bundle:
        it["classnames"] = parts
    else:
        it["classname"] = parts[0]

    # Unbekannte Classnames (Tippfehler-Hinweis, nicht blockierend)
    unknown = [c for c in parts if catalog.find(c) is None]

    catalog.items.append(it)
    saved = _shop_persist()
    # neue Kategorie ggf. als custom merken
    if cat not in (getattr(catalog, "by_category", {}) or {}):
        _remember_category(cat)
    return ok({"item": _item_view(it), "saved": saved, "unknown_classnames": unknown})


async def update_item(request: web.Request) -> web.Response:
    it = catalog.find(request.match_info["name"])
    if not it:
        return err("Item nicht gefunden.", 404)
    data = await body(request)

    if "name" in data:
        new_name = str(data["name"]).strip()
        if new_name and new_name.lower() != str(it.get("name", "")).lower():
            if catalog.find(new_name):
                return err(f"'{new_name}' existiert bereits.")
            it["name"] = new_name[:100]
    if "price" in data:
        try:
            it["price"] = max(0, int(data["price"]))
        except (TypeError, ValueError):
            return err("Preis muss eine Zahl sein.")
    if "category" in data and str(data["category"]).strip():
        it["category"] = str(data["category"]).strip()
        _remember_category(it["category"])
    if "enabled" in data:
        it["enabled"] = bool(data["enabled"])
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

    saved = _shop_persist()
    return ok({"item": _item_view(it), "saved": saved})


async def delete_item(request: web.Request) -> web.Response:
    it = catalog.find(request.match_info["name"])
    if not it:
        return err("Item nicht gefunden.", 404)
    try:
        catalog.items.remove(it)
    except ValueError:
        pass
    saved = _shop_persist()
    return ok({"removed": str(it.get("name")), "saved": saved})


def _remember_category(cat: str) -> None:
    lst = cfg.config.setdefault("shop_categories_custom", [])
    if cat and cat not in lst:
        lst.append(cat)
        cfg.save_config()


async def add_category(request: web.Request) -> web.Response:
    data = await body(request)
    cat = str(data.get("name", "")).strip()
    if not cat:
        return err("Kategoriename fehlt.")
    if len(cat) > 60:
        return err("Kategoriename ist zu lang.")
    _remember_category(cat)
    return ok({"category": cat})


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
    map_name = cfg.config.get("map_name", "ChernarusPlus")
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
    parser = getattr(bot, "parser", None) if bot else None
    positions = getattr(parser, "player_positions", {}) if parser else {}
    nearest_fn = _nearest_location
    map_name = cfg.config.get("map_name", "ChernarusPlus")
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
    snap = _ev_snapshot(since_id=since, types=tlist)
    return ok(snap)


async def api_event_types(request: web.Request) -> web.Response:
    return ok({"types": _ev_types_meta()})


# ──────────────────────────────────────────────────────────────────────────
#  Bans & Whitelist über die Nitrado-Gameserver-Settings (wie im Web-Interface).
#
#  Nutzt die vorhandenen async-Helfer ``_read_banlist``/``_write_banlist`` bzw.
#  ``_read_whitelist``/``_write_whitelist`` des Bots.
# ──────────────────────────────────────────────────────────────────────────
async def _read(kind: str):
    fn = globals().get(f"_read_{kind}")
    if not callable(fn):
        return None
    return await fn()  # (names, category, key)


async def _write(kind: str, names, category, key):
    fn = globals().get(f"_write_{kind}")
    if not callable(fn):
        return False, "Funktion nicht verfügbar."
    return await fn(names, category, key)


def _make_get(kind: str):
    async def handler(request: web.Request) -> web.Response:
        try:
            names, cat, key = await _read(kind)
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
        try:
            names, cat, key = await _read(kind)
        except Exception as e:  # noqa: BLE001
            return err(f"Nitrado nicht erreichbar: {e}", 502)
        if player.lower() not in [n.lower() for n in names]:
            names.append(player)
            good, msg = await _write(kind, names, cat, key)
            if not good:
                return err(msg or "Speichern fehlgeschlagen.", 502)
        return ok({"names": names})
    return handler


def _make_remove(kind: str):
    async def handler(request: web.Request) -> web.Response:
        player = request.match_info["player"]
        try:
            names, cat, key = await _read(kind)
        except Exception as e:  # noqa: BLE001
            return err(f"Nitrado nicht erreichbar: {e}", 502)
        new = [n for n in names if n.lower() != player.lower()]
        if len(new) != len(names):
            good, msg = await _write(kind, new, cat, key)
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
    try:
        gid = int(request.query.get("guild_id") or (cfg.config.get("guild_ids") or [0])[0])
    except (TypeError, ValueError, IndexError):
        return err("Keine Guild angegeben.")
    try:
        rows = await _dash_run(_read_balances, gid)
    except Exception as e:  # noqa: BLE001
        return err(f"db nicht lesbar: {e}", 500)
    return ok({
        "guild_id": str(gid),
        "balances": [{"user_id": str(r["user_id"]), "ingame": r["ingame"],
                      "wallet": r["wallet"], "bank": r["bank"]} for r in rows],
        "currency": cfg.config.get("currency_name", "Rubles"),
        "symbol": cfg.config.get("currency_symbol", "₽"),
    })


async def api_economy_money(request: web.Request) -> web.Response:
    data = await body(request)
    try:
        gid = int(data["guild_id"]); uid = int(data["user_id"])
        amount = int(data["amount"])
    except (KeyError, TypeError, ValueError):
        return err("guild_id, user_id und amount (Zahl) erforderlich.")
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
    return ok({
        "currency_name": cfg.config.get("currency_name"),
        "currency_symbol": cfg.config.get("currency_symbol"),
        "starting_balance": cfg.config.get("starting_balance"),
        "economy": cfg.config.get("economy"),
        "kill_reward": cfg.config.get("kill_reward"),
    })


async def api_economy_set_config(request: web.Request) -> web.Response:
    data = await body(request)
    for key in ("currency_name", "currency_symbol"):
        if key in data:
            cfg.config[key] = str(data[key])
    for key in ("starting_balance", "kill_reward"):
        if key in data:
            try:
                cfg.config[key] = int(data[key])
            except (TypeError, ValueError):
                return err(f"{key} muss eine Zahl sein.")
    cfg.save_config()
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


async def list_announcements(request: web.Request) -> web.Response:
    d = _data()
    return ok({"announcements": [
        {"index": i, **a} for i, a in enumerate(d["announcements"])]})


async def create_announcement(request: web.Request) -> web.Response:
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

    ann = {"day": day, "time": time_, "message": message,
           "channel_id": int(channel_id), "repeat": repeat, "last_sent": None}
    d["announcements"].append(ann)
    save = save_announcements
    if callable(save):
        save()
    return ok({"index": len(d["announcements"]) - 1, **ann})


async def delete_announcement(request: web.Request) -> web.Response:
    d = _data()
    try:
        idx = int(request.match_info["index"])
    except ValueError:
        return err("Ungültiger Index.")
    if not 0 <= idx < len(d["announcements"]):
        return err("Ankündigung nicht gefunden.", 404)
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
    ip = cfg.config.get("server_ip") or ""
    port = int(cfg.config.get("query_port", 2302) or 2302)
    live = None
    if callable(a2s) and ip:
        try:
            live = await _dash_run(lambda: a2s(ip, port))
        except Exception:
            live = None
    nit_info = None
    nit = getattr(bot, "nitrado", None)
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
        "map_name": cfg.config.get("map_name"),
        "server_ip": ip or None,
    })


async def api_server_restart(request: web.Request) -> web.Response:
    nit, e = require_nitrado()
    if e:
        return e
    okflag, msg = await nit.restart()
    return (ok({"message": msg}) if okflag else err(msg or "Neustart fehlgeschlagen.", 502))


async def api_server_stop(request: web.Request) -> web.Response:
    nit, e = require_nitrado()
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
                           fallback_text="Dashboard-Frontend fehlt (index.html).")


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
    return _asset_response(prefix + tail)


def build_app() -> web.Application:
    app = web.Application(middlewares=[_dash_auth_middleware])
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
    r.add_post("/api/auth/guild", post_setup_guild)
    r.add_get("/api/auth/discord/start", api_discord_start)
    r.add_get("/api/auth/discord/callback", api_discord_callback)

    # ── Nur mit Dashboard-Admin-Rolle ──
    r.add_get("/api/audit", api_audit)
    r.add_get("/api/admin/guilds", api_admin_guilds)
    r.add_post("/api/auth/logout", post_logout)
    r.add_get("/api/session", api_get_session)

    # ── Feeds ──
    r.add_get("/api/feeds", get_feeds)
    r.add_post("/api/feeds/{guild_id}/{log_type}", set_feed)

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


async def stop_dashboard() -> None:
    global _dash_runner, _dash_site
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
    ),
    "styles.css": (
        "f68b843465b48dbe4d294a2f319a1c391eb1b43eed1fea7db39a916c1c1ad804",
        "c178de8eafdc34b2f5bce14ad45a309cf694479c6a2697e9fb52d448347e9e2b",
    ),
    "app.js": (
        "ec14b8b4a90c6553d11b22eb5f76c732b47c827e8bf4359e9f2fb793f8ca1b79",
        "82e5da5110b186c3234f7637dd397a87cb2f6939e165b35fbb21d992a4200689",
    ),
    "map.js": (
        "f7c261a280532fbaaf046ad16e9fb480a6f9e98a7648c13f77d731da9409f98d",
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
    # index.html  (6.872 Bytes roh → 2.880 Bytes base64)
    "index.html": (
        "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"
    ),
    # styles.css  (11.251 Bytes roh → 4.840 Bytes base64)
    "styles.css": (
        "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"
    ),
    # app.js  (45.531 Bytes roh → 15.332 Bytes base64)
    "app.js": (
        "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"
        "NoL6TQ9Li7//H0I2q8k="
    ),
    # map.js  (8.934 Bytes roh → 4.372 Bytes base64)
    "map.js": (
        "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"
    ),
    # vendor/leaflet.css  (14.806 Bytes roh → 4.680 Bytes base64)
    "vendor/leaflet.css": (
        "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"
    ),
    # vendor/leaflet.js  (147.552 Bytes roh → 56.564 Bytes base64)
    "vendor/leaflet.js": (
        "eNrMvWlb5EayMPr9/RWFji+WqKwNut1tFXK9vdlmpml6GrxiDo+oSii5C6ksqYBq4L/fiMhdC+A5c+5zzxk3pdyXyMiIyFgGW53/u8x5wfMr/n86W533"
        "PD5f8LIz6n/bf8Y6cecfh51FcpbH+bpznuWdJC15Hk/L5Ip3LuNl0e/My3JZhIPBQtT8s+hPs0tsyp8Gne3haNjbHm7vdH5exLPkMsk7ry7i8yz9nKTM"
        "LjEadd4sstVsP57hOAb/Z+N8lUI3WeqXjAe3Xnb2J5+WXhSV6yXPzjv8ZpnlZbG56a3SGT9PUj7zNlTmZTZbLfiE+7JUEHqqOdOCqLW5Kf7248vZRPz0"
        "jz1ZzzuBvkPu+2XU1M3FIjuLF0fzpJiYn2F5d1fwxXnQlysS3d4H934JWczMCWa0KninKPMEZjVWGZ0FZsFC+1dx3uEsYWk0YlkU5xerS56WBTSaXpTz"
        "cbqbjdNuN8CiHHalk5gyx+lJUB7zkyiBf8Y5L1d52invscVP0QGtY3+a87jkd3f2iGTJD/1lnpUZzjEqWcqvOx/uzQA/+MHtvf6Kxe5g0zjUV3ker031"
        "frFIplyPoH+WpLOJ+NOPl8vFGmqn/Wm8WPh68GwUBKGfRLX07cAsnx5rKduBlZIrM0kA/tJpXPrVFqBd8xu2BAf9Nhqaqc3NInincvNOk5kHqwt76pd9"
        "KzHqdt8GzEkyq/InrgpLxLqkLGNFZA09jTZgSzc3/VwOPmFZAHu8MQruWe6UnFg7H/qlLm/mwQpeHiWXPFuVfgFbAZuwMQzu1ZrnZlA/OoOK+PEIgBv+"
        "DE+g77RntimKonRzM5mUIcB9jwf/T9bN8B9u2lrpHdgYmdREAIPKgHY4NsKj/bic95fZtT8asqssmXWGlPdNCOOlvDyD0+WXWzwY8MA0+IsFlWUfjsrl"
        "RPzxg7Ds53y5iKfcH/z3H0X3Dv77anDBPM+q/71VH9vqF8tFUvoDKDuwik3FuNWpS/A4yWNiQHkeFwfX6cc8W/K8XAvQKpmXLbGJwgsIQORXpH9NPpnU"
        "IAREAB3phOPkBNY/MUdUZZih/VSBpOj4ZIzjTHGMZZD1l6ti7vN0ms34T5/23mSXyywFwPCTSdovs5+WMNo3ccFhwdKg60Vet6FsiQgjkKPwASP2Rhuw"
        "P304pvzm4Nz3Jl4w8Ta9EH90s/6fWZL68C2O0M/R4I/bzpZ//Mf1aad30g06W3/cDy7MsfoL5pBY+6j27Wfmongu8FVy7hsYCcp5nl13EAm9y3OYuPch"
        "61zFixXvwNZcJTM+o2sJBpLEZwve8bqlmkmHRw1YH2+FBHC6RAAzibOSgv42IUTvWNw9HSpxAk1FNegos0OAy/RCwkVgIcwfKtAFGCfZLRUeTwCHw4xL"
        "hAWcrxx6IufQG9Eo/xl5s7iMw+QyvuCDi+R8fAab+s0z9mm4+OHg7WL+6l+vXr969Xbw6s31K/o/+n715tXbIrJul9+sA3EN25tdH3vX/OxzUsK6ndzd"
        "qbTL7EslocBvGsuvNsr8BzZIV1XUxU16C5cKS8SBv4xv/CEbfdPzee9XDV9Qn3cTJhruW9gLgYQ6+CqSmTn/a8WL8lUK08bevs/jS7iyfvO9T005eAb/"
        "wf6lak/jdMoXDZXfNGRgXZ3V2roNG6oXwP+5noK97zfq6ML2biR3d1/BmfqH2t+vBKCIRpi4RYNxKVJtFJhjX+Xm5r+cCqVYqRJoi9NTgsLT0zBdLRaM"
        "35Q8nYULJi748BPD2zaM2QUQdYu4KPdm5vZ8e8+KMr5chnOG56wsFzz8k13n8fLD6jL8kZ3Hi4J/n4YrBtALa4GpCUMEHP7CCJf+kuWzIvweb6EDgbvC"
        "KXb1MYY1E0ci/ImV/BKOPIzmLybPWThjEruEPzDILdd7CNo/5Yvwn0zuO3T8FRO7CD//xSxwoB0Jb5jZY5GSW+vPS6RUeNkXSxLZm2cKQZkp0WZwmcO/"
        "gPOSMokXyRdAg5UERbIgIWddwFQKN2cPSv6YZZ8LX+wOEkS8f3parAAJn55GVFCjDMDln/w0IGyON46fId0C9OBqWmY5YAKLCsuok+BpdxIOLwmALtbl"
        "gGCNEkjQQ5EJPl4+NCz4GyDiLfsAD2UyBbIawJDpT6R0knS6WM14Ic473nEqBWs2kcfvNzfxf/395CZJg9simvlFMCnC40JcYthODtgk39VkbQ7osDjO"
        "ERfKev13V7jOm5u4OtmC969juKS8txy4FqDyAP/LYXSwR6dO2MG54UVB6wMYb7HonPFOzi+zK6rYOV/BQcAUOMYFL1hnST8ga87zpOyc59kltErtUY2i"
        "5PGs7zFfX0kBLtL0c3C/kABC5/A4O1F0qLV0wb08eQsf9jRgM+i3hJGrdTYJqgrLFFEA+6V/A3VsCIzUJjDYwpRihvQA+rJ/migABSoCvh2YtclORFgE"
        "rKbGGyjLZ0CRurU2NysJBgblwag2AdSp3vsyGgIFag9MgUG5y8clgIKdd1yeWK3f3zMO/9Nr6xxvcSG5500txVjvQOU8loFZL1jr5toRF9O65PkFlxjP"
        "t5cZc2lcdomogctatPRA41CNxLOZWtqGCTZyW3V2Cu7jBjqoBNLc2nPCArDCFo7jho2oDNWCpNacuzsAstZcQbcmcqpjMZ/bLA1tklBenjXWvwwUBEky"
        "WEAaVEoZ0rIw8jGHm6ujimUAaEUETDwyBIECsmy3GGcAZLp2CYeWerWnfc+y8/OmYVVZ8uDvDPX8vDLWWzG4sRky8k/VPmAWQ2ARNfVY7ObjAs/JxLRb"
        "AnoF9sj5pkHf05pIDCPyOeFJd7qntV1ggLwNBG0YStpFydd5ll50FgmgyJTD6YZSIdLjsnQQEksoOhalCrmceBVFt+dpyNm0vAmTKJEFJ4ITCJN7lmKh"
        "PmBUjgwus8cf2R93d4AE7QSA6qjybWDTpElOClA0O23acc3K0z1p1YVxpdUOAgSGpg0kyBGFzxOkjt4A5yuARABpWoXOFG+S8zRajRt2DrqiXf3P7A5Q"
        "BT5v3CDcoSRKgT1jtcFjFg2wtuopzIZwkg+IMSXmG35zlPDcM2iBNx0rasT0Dn1LgcXCh43l7JZGXrISlpaXBOesyFb5lB+JFCDcABVaKXd3BNhBdeNE"
        "w1l177BYJhCiM8+oltId3d2N9IkVRzOrHk3qJI4yPIXzKIaVGscExpK+pCPK5izuA+gHbC7xN36JgcPpu6913evd3yeyBWQAlvEF0EJEpgBRee8caLmY"
        "TYe6IArdAE1ZIbO+ViUIXjoKmlH4CmiOz74OxmINOeLXJiik07GxgUxhFonT7B5eNQlzdnADIFkdmc1NgswqWGYIGlLUNMQqqUa1OaFa0yowI7Tf7v7L"
        "VCA2+w6ww8KYdrWQi502L6N1oG2oSptQDo5yQ7H4Gxv6qCtsR4dMrtHYvbtqaAEnLDFDFMEq0wfADHwlisnM7s3wEeL+Q5crA/z7P7pfqQEHRIHOeWf2"
        "JGwgl+obF9WTqrhfbTEKduESkHSVIP//dn/4UFBFwE4HsvnKcXQa14L9FghtaJrDHgOqBCKTqFTAgot4zfMQvgm7Md3d7HtgVnT6PRKSKAq+Z0kJeL2v"
        "1vi9vAIivE0Z71vLYWWRXOPVYuGkF1jn/JxRYwcpR7lHvc0phwI4ZsqKxG9G3GqtNXmomGbT8QY2PPxSQSotzE2UTGxxcRCKLe2v3Qx8siEOHHqnZGCq"
        "02njW8dwtxQ1zxdZhrKWkL6mPKlI8S7VSBR8EC+I4gc4M0ugpXE8E2QKYcwoUy+PR0CGIS8IRwvy6wcNOPIbeluAH2v6oer3b4AVWUNt8SmoNz2UcyFQ"
        "RLwTGKkinxxD8gksSQqnMYuS6ksRrZRc5AQlvqbFU0dwjxemPbtzGD2ORPRrahX/8XFc2ONwl7iQgygqg7iycFlSfIg/wGDu7sQvAKWa+HgvvYoXgGHf"
        "x+V7oIfEpoQdHyihrseAIOJdL/CCsSBD4jLqShhbpBdRl8vnCyk/oYx4AYXs/bl+CFSuJKjAnkuAMJcvgM1kB+8C9ZSF474y4AT/bANMbT9QREDcY2AH"
        "8zLwBrVxosyDGYrUEicLiGQB+KHE+QWh9WojOhAVBWQuLVHV7RRq8bD+Uidhmc6xPLUBIf5W7Est+UH/FAr5l3izAHJ9qMJNNyp14/h7LVFysTor8dH6"
        "8a5USd3fo1VvelanPavTWYLPE6/Xj3eqSiLGYaeP1rsZRArvDfSldrlalAmw7k/pz5QVPT6h7s2W7nNL91kAqdoyUHu3t8z6bJW066v0iVUHpupAVCUM"
        "3wBe7gzFPYBze7D8TWRfJ5QSqBulmrNWYhm6KR4dgbhPcAQPlpcjkLdPwwisHD0CvJ0eHQBdYdj/Q6Vl9+K6a+jdZOjO6S59tHcqRd0/WP4mwudHt2OV"
        "onucJQJ7HmVhXQYGRCcdVTiHCrfgGRQfa0Vl0mSKv/LS51u8W24h2PO/VvGiaIBA06QSV9wAkwIjk19r2IEsLeMkLRrPC9UW79rxGUoHb4LdyHyK2W5u"
        "WgXWtQII6upNsb543scsgWXyuolqTtxdiaqMdxgQfuc2YpYvQ7UlZAkxxuIid6iauzsvXV2e8dy6PeCigWRBtwQ8SmiyghuA+rQfp7R4l0kKPF+Jr4Bs"
        "g9/dbWi2BMco7lcoI3hcLDXxVZoCS/gJdKtEAJQugQRK6zLxjZ/oMpgemPJru521lW61s7bbWVvpqIaiahBBTKCtCwBhI5Mq7DbQ3m/wtSBvAo5L35pl"
        "1xr0YJuZnHXXGgbkILhCs6+zsswu3/PzsuE8XVoN27OgmkfZ8lNyMX+oHo7CXiFVr6U7VdIaWFsPqn0qeph84Q+U6esLWHUQtJw2Bbv6zPotwFqh1C/D"
        "0wAB1CFwUexlQywwEADaQN70b76LzLpubgKo7UZmwVDUtLZKrLHE2ioBuII05gqgwdzxi3Mytp4qZP+yKpDPYkilPEQZ0hnfASTe4KsLDCNRyA4T15i4"
        "xkSN9DLAWsD4X/F8ES//I53rvk3Xuudax0nxM1La9c3ekK9LdPo3VJewz8u4AT21DE+jSxhTD4YTbAFVYqeuIXUNqWpQp0DSYVlkeeF6CNgl4o0uNIvH"
        "LXjoQtjYQCGnWDNzRERxwN7mnPgoKNV7bxewTggUQqa8eBpuZkqSVGSrcv4LL0qmJJZplpfzd3FRjquY+yoAPiwqxcMCCp3c7CLQqHhic2PXxDtd4Okg"
        "ueqYMHpp943nw3TcgNgVLoStzSZ+SiyUhYSR0aBEFAgjS2XnpRciER8rTT1CzFgvE/Uyq57Ig3qUqDG2GXAkGBXRMReNV1ZPFhF9JKIdSXk8AJDumrgt"
        "uuAJzfao8RqIQlc90aEBU8HjWqMmgMWSuGRmpAS4kCxAt+HOcejpK7+6MNRGZdw0SH0R2WWxn2pZGPZgW/R9qEq2YXbTFpX/oFppLa/7MeVb2pecKNZS"
        "JdUtDd9YyQ+sUbb06rZCJa1WsJJq5WnTxOUxnT6lfCym+rRV0c3TfJ9SHptvvEefeHFeAdWnxQbX4UWg748n4qhmoQ7hFxuC/IBwjA0ksPL6OoYhfBcR"
        "AsGbFv7sRpn4okNCeekF5aUXlIcL1XoFX1RuwUdOdW2oZW2odFPSKLkYVypGKdALlqdhcjHMVAyTRvuka/t/ccDOeN3hOqNtGGyZvX6d3bRxK8fuYWQP"
        "nzJWOconUt2UeU1XtCW2lJf0hb6krdNlX8X2cnB1X1tHxS5rrxM9DT9K1JhOFW1jmoZrXzyXldHtguSPRxlxcpUJlVqrAkV3kKx+4tTkxpMAxefOO0of"
        "KOe0EKp6WOtUfwuhKVtiZ0eZkH2GVe1bp10mB1FpcpW6bVYVRNSAV6kZ8j2Tv1ulS40zRTHR36jo9kjTaKq2/fybLa2OLjirL1l22VSUii0yAKgB1AoG"
        "9P3+wzYh3o+iMz57jSKiBtbEWZkkPUfVFyk89Xltg8+oFbXoYhPkhS+fFysbYbaBC0JdAv1DxYBwLgOCYjmccMO/csjQx4/XtZZtIPHlCi2QWhFEBKtk"
        "IPkiKKvdyNLunox479uQDlZd2GH0rwW8GnEHoSZX5IE0YCnEHm3yIrkfcMJVAaHmdC0kvaj12nCRQnHMkUMQqmeI75p3vYxGL4db5eDZcPji+XD0Ak+r"
        "gJtpVojV+rg3oDJyIvoEXfjHZhH1k0OPnzCdbj1FdPkJMsYPyNqvrLXSKyTfKwARBcxMq43GpRLpxeRHvzTVZSI+LYalfRVYncqmRUU1AJmoKiI9Ll8X"
        "7q2xtB0ncW8JIlfQCFabuDVk+mJR22jbYihsNcgh2bYMpWVK8qQL0ibJE0mSJ2JzFEleyuRSJgsOrxQ6K1nJbuWqhcc92HwG/52wT+E3Oy9GfEfDa+Xc"
        "iec0C2boPod+thK4s7n8VYgSBVyRkl8QJ3ArAeKccTdXH0ORW0bFVtHV4JkGW/p3FmzxLVTa2BZpMYxw2zdiVHXE6WMEvIl7E3zaShDE8jKCOb4c7bzA"
        "n7efwrxk+69+PX3/6mjv6Ke378KXz/vD56PR9ssXL7592XhNiM23V0Fuvd2ObXGgGcqESVzUSwJVABci2TLXlvXW8GmLlmZLKnh+2tLo3x91k2AAs0wC"
        "wfE030tipDDCgRytezB8ayXFIPkNPvWuB6K/IOipWW4HOIr+zRZXefdMXBDyHfa458N65uWWrAFzzEtAFLC88Nd+tkb1Y6XtQy/UkreLI/FkSB9nkXhd"
        "pI9phK+M8mMGHzsnmq2O1SMQVOG6fKILp9Yr6Nzq2VoIa0D3cWlfPvqqarx/JEdjUR9aIsvFM8PDtfs3qNvG7+5GwZaaDT5MSb72DJV/1xFXeVN8qZJ5"
        "M8jDTX+kCwFLKO4fqJ56qvGB7JHRhvOealimTxVluJAoA4j3WzTTCr13Hw9/CHdePn/hMUMu4DlyL/oQlhsq958P1C2zlZcAOKz/nPUWJfwJ4DyuZPML"
        "t/lvh8NvRzvevaV4QWSUmtksm5LalzQafbfg+PXh0PfQ9jccDK6vr/vXO/0svxhsD4fDQXF14SFppZublRUjKEaqlCyPPI/F0ZDNjU5rvAv/oeIcWUZE"
        "iPZ84FGO4xOtWZTspmQ4lXcjP5l4773Q2/eCrl9EGVoywK56QBrQ+08XMP1ZHwY08b5AsRv4z1Myqhx42v1hZ+iRsshpGemJqh9yqv2iXC84W5aR94pM"
        "n38VphjIDUuLnMsyWgKFtKGbqKrbwDS8y+J9DEsy/ylPsGoaXyUXcZnlUM/3VM192BjMVd8BOy+jta/stAJ2QZ9xOsuBkILvK/u7s42GStb3DpRYw+Bi"
        "YID30tIf/MLP/pmUfwz842Hv25NucPfVAFARn/p6OP1VwfNXF9g34oXRkFq4gOlBuz9k2cWCe8A4rcvd5zsvaOyvVrMk+yAHLlYkYDdltLEhzaTQAiNm"
        "15CSUivTeZ6hTRU7o8Ff8OnnDNvcOMdFvMF/liX7COWvRbdFfB7DqgXsPVVYzuO0zC49ZF+9gyM8CwlpHkL/pyU7KiMkMM2M0PwID4uxafwlSaH2vtg2"
        "r6y28AX2WqzUm8NDOFJ5cmPmBhUuRyPaQjj0coqV0jCDqxLwUuTtZ18+8rxAjUkAHdn+G5yaqPj+9O3e4avX79+d7rwFGnsfOMcv8N8pKolsAIO98b5k"
        "vzcboGd5ArtEGIA2/TI7S3Bz2GEZ/Q7zOi/ZO/HrS8k+my6J6+Q5AejmpkzcP7ST2QEU95srbHwGqHwFQ8pgE1bTOdAvuXUYgPNV9Y4wW7T32p7xh4PT"
        "o4Of3vwI830Fcz2A9vbEQG9K9kn8OivZByCod6XRW3/Gr5Ip/5jc8MUnnLKyTgR2Kec8lfm/voUb1M2ASzwBjgpzAva2tE1dBNG+MRqX+Vre4NLKSqy1"
        "sq1CrOkt46LAHWS3qGdsG09EG0Ogd8ay2+rR970SiMuPojblHK6W6F/AYys02dbmjjVFvEdq3k/jckpW6/oJ8h4uxD/x4DUjbTh5cXoVF15A5DTsH4cF"
        "/5G2ug3Nw3bCdeAhUg9k3uHPP3yCRQrYL9jVj8ga+r9Y+LPS5yy58oIAzh5M6cej/feRtwutDb7z2EM3CBxgaBS1B4vyzTxZzDY3nc9+Gl/yYhlP0ZrZ"
        "1hpcOwp+UQNe65fZ++xamUZrpCDNKs+i24SHgH8Svii/DS/h6ppd8DBlAgWHcKokdg0v9M/tnfBKfxyW2fRzuC4Z4b0QllggvPC6ZITrQgBugdLCjyWT"
        "2Cx8LyuMtsMMoSI8wjHszML9UvYNv7/IJuDnKXa4hh9vSiaOfvi7+vWLGOuh+w1l30FKIc9z+LkUQiH4eQA0BZ7W8LX88SFGfBW+Ui0c0Fz21OcPNI9P"
        "QHLyMknj8AM0ZQFqEb4tmQC28E+Y7NVF+GPJri4XIcGLfXrk0XsEfhgqF1gw9PXuVVjMY1RLn/0ZeSNv8N3XzIYPYwcu7u/+GZ8DKGRAdKzyhf9fcMLj"
        "1aL8r5/33wce40Yjz9iKw1H+0xwyrU2NhyxJF4AfDmFav8CKxNPwsQtnP57CJKDS6ubRsu+xFAo8vy+js77eromncfTb7DoFWkZu3gy/2E9thfcBr5jC"
        "iGU89nNb4Z+Wpuhq6bG/2goKY2lTWBjjeuwHYPXMtRB+L8EJuw1/kh/42vmz/C3qhX8Bkf1Pt2pFQok31MHeh6N3n06Pfvv4Tt0gvC8HcIR8BDouaC53"
        "gOJSzqUs1IyJczMm9VuOifN79hs6cGG/0i1h7N5LV8nTs+5B9J1wd/crugxppQZ9WJWvSBbC2svAYv3rsTK4iPyRMn/pMjgL+PNP9A4zAeKafpA/FmU3"
        "zBrq/4ClUG0fbQmBGWwyLCKd9Q4SOcl5wmehhxfbymIBviKO4rfyuFTbtTc7iUpT4F9NBeBiqVWxGi1RSnor9fIrBU0puef4Eu7ACtk7VYFl/+Cnw3fA"
        "GFxmcFvAreW6BUFPDwgqZEL7Wxnob2E+9ltJltS8PwWMfsFnR6owGk6hXb8wDOcRXHIGmFIuPGQ0LL03O1tMF8n0s4dmicoJiFWXG4EzWckA3VLGyWKS"
        "6p+hnAppDVuzv7srpZnUm3gZA0YH8pcXQNw1JZMOf2GouQKAG7jcD5x0vQq42vwmGb392IdI+318xhcSraNOXVwCqXy2KrGDLL9Hgpc/vbW9dLlSLFrl"
        "uRKzDwEspir/PhAjRgcV/TS79oOgl+1GCZ+gOnW3ix5v/Gb9i9t7baRPOm+4xywl5y2IfMipEZeHKAi50UhKA7LZiKxNTOWmRNvwMymO8hVs9AwdAqX9"
        "0yK5XKGbBDSSZuk96WEI90/cUjJrABLZOJy4W9VVmDBo7q364gLyMs4KDhc2izmbcwY87YpH1+jwSosTgCYTpMaRlXJgfwA/Y39eFubrJGBT2aDViOCr"
        "mM1kMYdpM22qb9mo+DxBQ/mo3iJs3BTA2GmK0iZT3vXepTO4m0yfgN4tTyin3HpgkEZulm20Rqb4Likg6PV6T9icGMSy5LaoVooJ4Kzj0ZqucjTgOcQk"
        "BHX727jl8uJVmRGlm5CLBvpE6wIguo00QhAqPyf8epKEvi2nMDmCnr+EE8Fn1An61lotFkEwQfRNJgGBeH9KzAw+agObNuLLvFCgxA0IvA9AdeMF5wFK"
        "wof2eLmEpSWKi55eTeNHtvR+SfZMKCMY07ER3I6qZipdcmUoNbaJuXFQulVsQs+yjeEP9Yn+T6gGYkpMcMduGaI82IzpWbUDmAe4xNcchk0g0Ty4K+4I"
        "DLUpiVxYPM8T63df6WugQdNwF/b9hlv2dZubKHz4xC/e3Sx9z//vuz/+KAKyX/Hh191XAXBryD06E9u3ANZ4WnLHYAwAo+/xWaXFAtEeKVpooAWi1ASW"
        "E727W+MPH2BbjHySoGAu9Lyga9vwfJGDemw9xP7jalC7v/g+ivmoaWw30J6lPGHH0wFEAsmB1dWa231Fui8E6j76VPo5XkwcQA8bSkSWK7QbC5F0dLM+"
        "viNNMzjyxTJLZ4Bd5HmaNCeHZfDwQJqGYQbxRhM4XgYMcVIKezKBkAL5ty+zIq71tb3zZAHkgF1YIjO8kSLv7a/klkcj+P5+Ms2zIjsv+68WwLV6JDdB"
        "1CcaKvpJyS/RAlkzTdIQXpm93nPbsmI0HG4BhCVAi/bfpejAaxZBGlLQLOkfqPHig6GYguimG3no1eUCGHCvmwLAq5lJ8617yxiLO3aXj4l4271zIfGn"
        "FR2N0zbb/90ZV9iUEKR4EBiyITp2ElfDip9E/lkfefqJuJrwvvdh2CizXt4w/LXGX4G6u7DAzqypCDSMQu9k4nWEwoDXTXD2oeMD73epU2I8FS4zdGNy"
        "1ifRwUQMGp945BIv+DkabVJnHlOpZbaMZL926x+546LP7sNeAFPhPXpcOlR+rLxZHl8IrokdWKWOsNTnx0rtm6uiR64l+mV8toc8NNwWLt6W0wB24gs+"
        "GS145M8BtQfqZKxK4uYJBsqGxMhLgYLwmBn5Z74WXPcXbiMYbP52jh3NeVM7C6S6IuhboAr2+bEG35hJbvi+O60ArXELXv6SzPA+KOXnjxz1jIEIgTXR"
        "8H6WzdbB2FzmFoTwyis7vcGjp4EFSpdR0Oererc3IfB92N2gtDu/uxuxNWTNqetBdSQj8X7ptIk0acxRhlwQoa6FyGrIEz/ntqzWrL1dASHCdgz6ubUU"
        "wHghiVMUBAr2AK4IIRn7yU46cL6AOHW+LwvrE4hTd5gkyhoXnNRlHkE3GXITBTKH+K8EMnc+1NDDzVDdjFqT3gPug/Gy7hvu6NOrD4ffH3zaD1dcfOwd"
        "7R18CKf21+m7D2/DGUcdp/CU/hDghkuuXMp9lBb04ZHw3BZeotDk+zxDRTb8+ToGjuMCOIy4eIN3VnjF0dBT/N6XtcXXF/QcJ36uqTPx+4YLj3KE1cM3"
        "jKTgebYMrylD30fhGX1/zASJH/5Oqlnq6yO12JklBd4sR/ymFHvm6MrknPS5Ojx9oFDMSa0IS9CF+BYQUvieM1HJJB1xtsxJFHIgDn24j+xWUQJdqFK+"
        "cGUHM/uoT3L4RqSS2trv3HrBPzTP6Ci/aJJSaoItIxFF8JmTfwyO7haSiqsG4aUEGGEk7YKqsxKqydFVCXbo+GjAyu8i4xdXeLawOKrPzkAbnd8cklKb"
        "cqNw/O7EGI49ZV4HDfMiEwUiU7fbemR1WQI5OP0BPdzhUWlYIF5dmYP2lTFbxRscyeI0lU+QRDugnQ2C4+EJshMbHMiluztqPyWOTfyTSGHROx7dkgBH"
        "6PcLYQ5qB3uMfsJ+wFGUyStAT9dzzhfhBlCCKf20nmTx3RLLiXQLyD6jW55SbB4tOL3Nl1100dX104l36sHvNEDSYsxhRsD0wL/HObC6+Ore5OAsEZ5s"
        "0rs7aFq/1RHYAIbbOOtbjwybm2dKNEXaWeRcUMrCqZgXTLLoHyWNMgtCWRnmo0UrWAnKpDgTKOFVpSS4DHxiy2mxBrCxWhCsEsTqyA9rvaiHuh8N/x0n"
        "/y4ImhswLecRZHPzVn6HQCkGcp9oK50eaBcf7oG5q1xGlUVlv9AmwiYXpAK7MQrChrZK2FqZVZaxFOchsHhdXFqG+xrhP+RMRe5ylFkUyQFXJx3K32ZR"
        "BjvcDCl0nliOKlsEM/gvHN4x+k54BAB4HQDQzRZAZcJ+gNsy4ieTtPHJ9AdyeJbTJJ8mqUbP4M0QxbHThPk5koqN77OOaE/+BoqgubAuaYnmoG+vobDy"
        "xNDUzjshY+JCJB8KQa+zk5wk+WK1I5L8mO175ZLscDctP0oXMpA9qaWQr+0sTy6SNBbuYCaV7/4pVlmS3BL5VHo7eb06g6sRJZkuknxtd0/3jTxw7BUP"
        "KmX37LJ4EYrDgmRyxxzkjlr3zlS8Z8MNsJLNGY5EXt+nVLI+rANnUeQl/laI1ybVBOl+HAv/jL6okVt2m/tkjxzbZrTslVIkPie7a4yZADwTkgRwaxmB"
        "m5XqG59QHF8fkki5+hkn44Brj4oJXDMW56PeQE2vb7njtF+XQFj3k+h3TpRBnWBnWsNtSmm/9lJiFoNB0r/pcZmKdpBMl/mth27Il1hkrYscQYJ2bqMa"
        "Y7qKeIT/k0dnfXotRdwgns8nLZogcHov4+lkZ6stf7jbkjPZbq1jvfb9aO/mWR+1AQAmCGzf8kUZ/zbYplOIP9X9RV+oyzXpqazBn9wqNqoU2x5u6ZJW"
        "se1KsW8aiv2KAmf69fsET6AZ2cR3xonlzGcgh40PEo4/ApEU7O5sv/jmuR4YPSbpByb1Y9CjUlvfDMOh5bnfkYyjtJceNo4kvKJHbONkDWVIxOCiPBnl"
        "f+PABeKGJ3hFZGBxgpfLOr8DLMAhee/8zCo4LXzFFT1/OM2zxcLOe63z3iCusLP2NH0vMUF4QE2Hn7g0ClFF8cSGHyh1H3GWZkreUtovehPCHwFZF+9u"
        "4OYDfCp9GP5CDJNC+DALcQ3ohM/37HseJdpp1m2+anLaKYxJYHy+9rm30Cq8CTqOvgDmpFDYsH86W+U0enx/6G8/V5XiArmXj6gxE40GWtUaSLv+c9bf"
        "1vZWiI1hotFHboywhCAAQMC17NeFncroUMxyJE855P3MEySbKhyTa3aODkZo+SuObO3JKTdN0AFf+sZ1KKJW5EN84VZINFhvCDP2ZtGN73TMbA/H1DD5"
        "CoIfDSrhej69ykwBVY/4zpa79GO+m0hvrrCp5Nbcd7bB5wO83LVSti41ap6aym82MtP7QDJ8e8v6jpugYEyvWMqzDvtdDWqhTY/URvGlR35n5Cgc+xTf"
        "XtWgARJHdlv4ZIdNyZk3mfOMeia4iLKbcQCWbF1e2WdFOloOb6d5ES5KNhWclRTIkTWY/H2ZpL/bn/GN/Ul++IoQrmLIkHYrMgsQ14znbqM6nEC4UUk4"
        "msPc59liFj5j5/GMOyUv4/wzz3+vNqDfa3fevk8ukzJ8ufPy5TfDl9TyYRovwxH9FDhmhOWnnz/xAt1vbAzRBEy5ta/ZAU6V+2e5mvM4nS3QXZ92mitm"
        "Hmlni9gRLcD7SgZ290aoQhgkg12/EU9byI/YjsKhPsZR0LgjFSOOYt9NYFUP44LbQmOAvt4MadoJoLyvknwrOzC+3HgfZ6AxBX5of6Owtrj4vigTYA8C"
        "YDY3a/VVf/ge61/7qiTWwQLsFgNtlbj+zWEDrOUUmw3LNiXeiGT1m5vANFm6eMqDrIDnvgNTDU3p+Qk5HibDubtZQ7+HvvYke7NmM23bgdeueVx/l87E"
        "wms0PJM77jsDEeCBuFksRZPfUV/JKkWUID3YsLbu2hRVJEoTMLJDlONU/Vpbm+ANZsDQvgIXGRwxXMukT9sBv8ifH/o4Nt79+hLTk3djAoiFf6tuCZ0L"
        "50iABUNyJW0swxRmhyT1816UD+wd3zALAQSR2rTfDWBVk8Vqiv7DWomPMS637EZxFG60En1EMSUXY3HMuE5pgQiaRV/QGD75w59+mqFW4SHeHspdBpT9"
        "3bWlrdnviMWfOAdFGXxr476S3RIo8Ht9yYkjyaT9JX7tpS39oFzEV69btQMi8aFceDliq5Ou0A7En+6N85/roie6kBmvKh7qnCBdammw4KGyzM50MgqR"
        "/aCv/QeSTZ9fc02qDEHJ4FxjXuFDjNS3FGWWBfalP+qNBmmgnDtMnXrKiNzPBN1Qsf9TW5uiw27ay+QeL3L1uFSIvW6AFno5xYNrPUUVE+s38N7aCQJ6"
        "HwPGF0aQ4NTIB8/dnU65uzsesuEJTsEqZ7niaSprVlf0J08aECaJUHMwkbKyqOY5g+4XrDKxHNvIJBRLZgHgu9FgOLmVVIdr1kpLld2jhXhqtiVxtzh1"
        "rMfrTg0ybR7rFHF8PgRMDwALGuP5lCbp9khJnGrJAZL/9rJmqyt1EKQXhqAvvSX4xt+QxOQNcEC1mYsY5P3JSnF9cnTLWvHnKprpAKvYSbOyQ85d+54Y"
        "3y9Zvmj3Fqon4B8f974dsh5Z5R5/K8xzT8g1ACA6x4q8is8MdjTHm91CLYovhtUdH5dG9UZHKBPQLl0OSq0IdD2InPp6AvwKyULlVSKd5ZmTrzWEjGUn"
        "9IlXAIajs7/Jy9H36mKXiX0Y1C1xLYq8glvD3PeHkMNQK7oxF6iBe0UOcPs6kKMUJDyyrYp32xBh5eRkJv6+HOJlvISWOfOkrK4Hw+thKU9DMkLMvigG"
        "vIpv4SyzapW5Ad9TaT+BcaobWPC3vI+swntAa3GelMbl32keX3/EzfNd5oaeCQLrgziUQF2/1nXpnir3NFbuu0ChUGaje19+y7YrXaIb0rUNmmhPKWSJ"
        "FGXDpzvq9j4woLMhL6ygGVFj9bFNLo1lmKcHh46vRHaBVDso0UDK4sg6HPPIT6NrLJcZAjCbxGFmvEqgz0eODhhh9+db9TswRky0iPK+8fTgFwFqGayi"
        "Uf/ZNptGq62VEd/NpJ+jZCvpzbfmXb+c9EZwQW9N4f8XW4tg4G9vQWISzjFtESg3XcLUfavsoimvvuB20WsFtPAyNB5CXGfa2pOosfOW1t34G63lB9v3"
        "tpPzpgrdagWSckUzf2iZOp1bWG2+5S/9y2DLP0V00l1tlcEAGwp6p5AcDKbUwoWlAs2uIn/mw+Qug8GKrVHOKI/HhAQT+jPEz6ut/kuXPLxUR95H5s4l"
        "CY2Ovq99tZqeexfBYA3b6zv8O2ej/vMg2Loa891opDAaQTqJMFAM47B+pA9YOWtCkpEbDFE4VM05XG6DBcBWbHkiQrCiQzdH2+2EyaWE5cN1RH8OWPxW"
        "nDlk39SJpwHgO5j4CTgRRUz31TBY8rg2XpiP3YnOklMzDRcjkZOazW12S2s8I6l4FxqfGNS5B9h9xnVLgRWl5AmlMUycuvMnfjNzpgWQihFra01hdJjG"
        "E7pWqLveH8mCJeeAqySFOi1OT3R9UcxZ/UpebSZc6AP7FsbG/VnwK74ohEmIV8Xyu00tBxOXdxDgpjb5ScMXxZqHL/L+I8P/rqnltuHrbWs+CEJ2l55n"
        "+TRRnmAwXpxUr6+xiQ1igcQmxC5sviTRjr0CSaAQcWeRnfWuR6wy7kYXLZe+L+m44H+HF2m/fctKgYAVep3oKUufpCQ69Y+F41JB4LOKv900OMHaibm4"
        "zcpZZKamK+v7RP5pDNPijhQfdKQAtgwsCtbC0ixDD/7oXnY4QZeuIXqZzdCTP/qXpbR1qH00y91zcT/qL7TvpsLFiYgBgXbMrvzTDn0oDoXrs9qW7wBw"
        "IJ2PslQi0suJyRreh66bPkMMwZX3gGiUDBRozQzWMpWFhyObMVOUL4bZaEpPbTYyM25vkL9Ikb8ojZwLFUvTiVpbaCclpWAS+VSI4rSBKGb4IIiPxlO+"
        "L5D1xH9c3GQlRFYs4NjgICrGDOXLtofDoJkodkaVk6DYY7fZYkbbzPH9mn4l6v6uvx5VaGNLcmMkkgbDqB5t6Q/K3h0O6Aroa2IMvMARR96zRTZ13pxq"
        "Xq01vyxKqmiSAIWlWCigy56xa5TSonoRvsR4Fzyj0tKVhbYyRlebSowuJPo/mJKfyB6hUHL1pL0ksd2KButT1xNrjFDiF0zbm1n2zdaQRA31GOojTwa3"
        "Q3NRxB/CaKpSXhFfzYPzhWubIbvkRRFf8NCzSpCQoBA+FPis791r2SXsyfvKfhjXPk3j29xsHjYBPS3C4yX8xqUzkmpr17X03klVDJyF3VqWpcFDoXot"
        "lII9oyuTzCgiI+qf4FsA+gAXawnYn4w5Jt6S55dJURB7xdOEzzyKS4M5crM6K5h9nCzwIR2NGQTEeo2TU9NA69M6bahEOb5zwNXsOM7Okw6NePOuUxkK"
        "d9j1cNODpmVSh6B6ITywTLaZqPIEN80wJjbKW5NyhcunU7L0gpIQX6NajLyct7d0kXg6BY5rutb3uLtMqLlrrZXPm2SVWUWKlrKiQShpEgN0wMjQISlG"
        "/UmVp7GM4Y6J8OBlX/++DyxjWDnsoO5AWOYcJ2jBnVOYaZMk5QzuNp5jtx7D91rUffhRPDo2y964njuFmYSF507MYfViqWJ6OlgaqqDJnlDd9rWvb6nT"
        "7oZBrj0wav2BGpvxdxklF6j2ZjrEYTOg1QSf+/Gyo4t2kqJzxoHW6eQcFeQ6Z+tODKhujlnyMQCDWuXrWyeIXWNfrLnI3syo4TxQWxm21GrLjHsd/RlB"
        "yNj8NSFC9aKqcbPvXqHsyJXw6WVFBIsPHXm2gLtD0S+VZN2YIBU+ifDyCK9+Q3pjYaFbafcq4baoPTdaIL9KMdFgQvFcan9gPFpp7SjOW2kCBgI08SI4"
        "8q0viprpvrOJh3j9Yk/F8NPZWyUYdRKV7oIKNkSPxVis9lLzUXhmceS23EPxmtdRSYByjVkmtgLFvaDribKoJcyUWFvvop6KvvPULCPYYv6wu3u5F3M+"
        "/bx3/p4W3+yFJq/1RUOR4P1gUi2gvAuqx7N19eFLC21E+fe6hHIU73LplfEhpShDp7icsHJJ1cjFuUECqjLmvhMiRmG2agkVCobCUJB6WlUeUrdmbRJR"
        "TGxAk23c3Q3DprKin6rg4rF+5J3l5qoOZeZoMAwb0sOmlqz1rj08ChN4kpAhQ58oFnxcf4f1gQceWs+Ecu6+zXvLYfgkH+/bEQyMi1kdjsC4s21gi1HV"
        "CwOPOGw+UwyYSskxaK3FZeZR6+u01A2KMXwMhVdDzpyiq0FHfKKF8DG6etH0An4xsw5GYFpa1jA5Bfu1zHvTgZ8PRsivbckfyMFOTGixdJAHW3loRToT"
        "KZaLafUuAIPAUMjBo1GEiKPU59tis7XoglSKLNev1kUmVJClSWPtEpPZyqpxGDSpOY3sVIVGhM9wc5bbZcBSdCRf55nrqxZDcJZGU89IBqwODkg1/29i"
        "xqWpaVoiovsBibINWtNcoCvXLbrx9S0jzegzFMqYVpWL5QGXHO5NEJa6etE6Vyosrgu1he3rokpo5H1Y8SFvefqozNw85zbrNKHqjPLpPki0j/17Zh+l"
        "J/WE+h0P9fKFzuSW6cMSgMroqAJnNjnltyns1j5qu+5EMpB6WS1Okv+9DpyoBRSYkzqo38mVWBn0om6fFet4+BWFFTd6gJqSudQfi0BA8zaBJ61Aos2d"
        "37e5X287W5anc8thfavP9MebkQf0QjTW4oO8rRntQZ+2+9onF/51DaGHVo92p6ITXnnkr2yyq7nU2qbRY64316zEVIGbFnUnTd9d1tSdmshDC4weH3it"
        "laqWlq3BZQ2FQAF6IrMoYlGf0hsZAFWwXqWRJ0D+AyskQqC1jkmAnNvdgwehjfxuHrGM3OuoGTdtsZl8RN6gyCylfIzTRgEiySr6HtUo/yabHi+AoZqt"
        "O0YBm1oi67aCbFE8rf0sbFOcF2+boZ6L8L1GcbqFjTCVyIemeFTXeuZAszjn29FA17rHDD0HaUZPNwjcnrSctHg+YbBJbn4gV7gHtbJFgs4nJ6tWdraY"
        "JVznCiepVrZ0BC3y63OxSmKyUCPCwgEjb11aPAkpYy8+K7LFquTks3Fz0yMzJXTxK7/PAWnP1EdRJtPPa/ml3aWo9iJT2VZMJwLFDyqq765cwk311Z7W"
        "aBt7SwVDf3s/Np+fJONu6d9LjlqiNM3J+57M8GpoQBt2KNmAceKidNatZspkQb+8hjwRwWrdll3M41l23ZYrrB7acsssQ32Otuxltlw5mYYRrNlSAFMA"
        "kN03/RkgJ13deTLDZrCMGXFjGbK14W0q7w8sq+QxHck38Je+/Y1PhDxq0o13H5qWsP/OW5PSLFe6XUZlJ4NhKQ0auOuNAktWa1U1mTpiLCHEwvjhqs0W"
        "6sGphQNSCoG8VVVQPmW4Illt12ZIRyMEFw9yIr5W5M59XBO96OgV1oNrWWeFDFf2gV/bBByGZpqQ97tlkuJDT2WCHvlJ9bO7O1MoaCxVe0iFNGf679Jm"
        "FdZaa+J1tFlZ8LTlnTOvqVhVtCgt7RxSqrQsKRDc5YOwM8AqpD+qvykVw4nlwif1Fg7NFqv0asIXaKMuZq+b81WUAowmiK2X0qJIREZtNvdceSxwFQdq"
        "lMAhLzuXSA0QuHXidNbBbeuQq0BSWbYeGlx6Rdh2kGko4XypWTD5HB6OuQrtKfOP51WhRnBCcBzYdIBwRWAs541dPf1aLTvaz0hHuRYRPxCqbEP7zme+"
        "XqLtXkc6sMK/6Dfafop5e7AvggBYpIxaX8sybXOTa+9N6v2+0fBLG0RVW3Js4vCWnqhXGSkSTAOjm8eqDzVZui9OHG6G6rLJfvEJLwM3fhX4XLUT/7ai"
        "LkF6hfdKUU1Tf3UYNuIoQS8eZcuoLqoSeShKiobU3H4VmbgEReWAjhtivGFsdSdqffBd9MgGNOpFO0pcrsozjPQcAEDQ9AKgK5eKFUIG3zOyyLi+icjx"
        "tuUhBxNQKiv9JJDH43wqfWexHD16F2NhsqD0zM0pKoITNAnzjDMQaBsg3Xzr4JHon+4CnxP36TUBMPhtjoprZ0CUfL6H5ukWUFqX5IqbesXgHr9wv0Cd"
        "BiqLDEWqPTlkgW6giGrPg7JGERW2pbwSFmp/nXd3+d1dpoJROmOYpCHFljtB1FOQsftbYfjuXjnCGStMtv5GOZbuK5rdbFgm/o7funv9/K4QQ5MSTMu+"
        "jR08CzdsvXMuXVSbvSvJE7MmuN3JwloDGWh8i5A/Xi7rwCbvc19d1Kd4tapBE0AKAkjxoUV4rLy8WK5hbKdNlqcmz/ZXcsKctmsEZH0uvh+LcE4oeRKO"
        "pjV4No02ZjEVQwrDfteonTghdkawNcctw8NWRMNxgQFgpW8s9I55XJy4ULW5mUkIhqxgnEYZigqmMaoY3hN8S99ct878BTV3oEy7chZHKYYom0e3jrOZ"
        "sBTuuD1173gbZkm0E8VKGtxHVoqfRzHiHCFKQF9EMSq3zZIVwIz+uRth4KN5Rc4R5ZOHzOmsdn2lp/SgIAQ6MNKNR0VP1dGI6qi4AeNy+m59t7T7CwK5"
        "pamzpbBHuKtERXI2p8gB8xaXP4BYUCOIKqhr4Axd/wCNtW+OxeamdLmmuBCdg0dIuoaFk+Ri0gbClzxgUqkEt878lgod9JYbCnqnsZh88JWU31l2Y1nY"
        "yi9VBuk9+03fvjq1FkNkGDOtdiIXs9zl4xI9JTu5+LYvERV2cT3nQGjEs/VTzGYF7SLZJnYr0KNQZQxCrSxPjBnTis2CvDaXewOB/ZFXFCkc562CEXky"
        "5WA4lA0dTVm8poqR2C9cjZP2S9u2ftLKhKHb2mbx+uOC5Xp77e8v5ln21FKybQp9LDlp23LztF0erZ8LYDgKn8CYGmWv0nC+lSNNWoIx10ekOxNcjemy"
        "4WXy6X2eyvC1rVaplVGwFgPVJ5e2n8+f2HZzaQWWVT2ONj70Mbl3M7Q4nRyQg5UHxN01QX/jW0mj5oltDtFsVc4y5bs6AbKnak3MHrMwzyLxEu1oljNh"
        "VpAFurKxZxKzRXkTtq0ZiAzYCbhjLcdTGbAUkKIQeM0qOZOxPmmGtTWsG9dihBsxVj9pVqgJtEEEKn6QQYS2xXQfkpyp4ETwdrct2hvGIzyBV5Q3eBUc"
        "q/DKqycnIfMJCvNtAQF+BhTk2DHjQFHmTVN8W2DCSLPTR6uKHkderpq+xvS1FGSe1dwSWDHVyq7U4hAIrOxx9CWmWcYhMyoepQpsa2UIVQ9uwPUhiyZH"
        "xSZpULFJH1N20atBLpntcQ/SYCu1lE04s8MHB4IPr9lE11hy2w6iXqXCdH95gtVzixTPde/RrkNiYxlEHVARiuqrecMnjyc+BbOQ9h+B0phpNC4PlCae"
        "sAwhJLFBlEHFl0zrcwVmNigMYnLHEabL8cy8wHraQCkcrVYt2oiiebAuLR6rg9GKazNy6lDHaRmfccfxTeXecIwsXVGFMESW5pcjOqBRYxeK5RTTAgLU"
        "duiSEVg7kOIrERBzyLlORVKFzUmBjvM8iOWleqly6QZIJM/Weoe0gKmaYW/dkb0saiio2fy0sTj6pNSGdLH2sAzKlgvxis7dw1ulrb5cXym4MwijNTdG"
        "DeLVygYNdyOKTk3RPynohvFHq8MdBY9s5CmqYJM54Ku6fzl5FqtyOxNwqHi9fqNCfvheyyGRvIaLHBrVG42E2pkqZSh3SRt1r1GSwUsqPihqU0OWSt/k"
        "ytUdHZzv2r1Uac9nSlI0Gj/g+CZ9AMNpCkV4raG3/Iojp1b8lgam+7rY1rVyT1xPFxujQDsF1D6ZCDOKY6YEYMbFYMPeaFeN6k5Ap1Na99nZLWMqKJs7"
        "yirPVzr9d/HO1epkQwNSxXDOYFLtIoZJ1aw0+2mJ8uswVQ7MTkt+ufwe6mFvxNBrLctajvsq1zDaulV/dZZNc5Ru3EhKUTchbDqaEkttPx/KG75WotkP"
        "pIUe/Mp+fXnCKrPG/Rz9jyfdstz158YKYq7VaDLq1GOTrg3QnaJxP/GT7RgXic3XKobu64g3eFxUyhKhV2bLHLVovRZXhMGtdEOo9ERVzRauTKEX1cO9"
        "ExeihbTE7RobmyIdlEzoYvgNj1Va26Nkgnqazeql/77GKTTj+BmSUKdMQIw2R1TaZsWWBpOkFl5JVsawO8p0UuidUw0Y7ZssT1EWpeMYdfZ97ur3QCkA"
        "ABLbJebyOyMDAw94NDfoGXdD74alQ6lxawr9Go0ipsnsBaxbY7mOR+IlnMGj2kunIpo+UXX1PCg/zRF1RoNkzcPDEStvTH2IPzrPpqviIN2Plw3EBI2v"
        "RCqilEHIf7U/flN0Aw7AhhW4lahdn7yW/swj/xUs83SxmnH/1sBao+SgT0Dk11fxwUoOiFlac0IXqUqoJRbcGShCfaM00sDjaRGALqagVDEAadeTeZYq"
        "WU0XaWz5kBG+xY6B8TyxWikxyFs37WLIh/sSFYOWxF2ckw6J+haIhhIk+NpldJIspqS+zavgWK81LUZgQ6V10tBEq5al4bZmcmdqtuQp3AFw8rrBtW22"
        "WMTLguOrMmvAugwDTf5OsbKwBGowHcKVtOCv44J8GxRZXgrfnurre7kIDcSL0jDfTdGxUrqbTEbh8L7VySyz3pVSXMepiT5sbHnk+lOQ17rvWeu7KMVE"
        "9CM4SdqF63p09a9uWOm526QGrhdTvzxOT9CcBceWipAzlRKcSggCj5Btw9lv8GO7IqoJPn0LkQeab7V4KdIvUe+SYv0FIrJ8/w/HfNdeC/XcwPVTgzQi"
        "5CfirYc6gjl0xGE32g7UgTBckb08/YKSJV/3yeU6PqwJBGSpV2jNCH4DTPxs7/xDBrhFAqZPS/iphucrmPlvr08J61M2rk9ZXZ9Srw/29KQFopXAM0I5"
        "Dz3ZGKhi9u03cSFCuqGAwgdCXfKpTVqUX2ur0ml8ZaQYC/DpEx5bYiXR+dxRf1c3mVxqjG80VTetTFO0A4DD6NExC1CxAWK/esvXyJSe6Kgn6nJjWVuI"
        "OF5SU1fEpqN7fGzLpuQlLLnCda9mFCb8oR9lyy5wDMYNnNuHbRpmXBvK3NYhCy2dszh/eMyliLsYfvm3m204OPrSV5dFA7n15d9d/Ef00lta8VidslW3"
        "vSFrtSWKwiVAffgcHR68UvHEAbrzJO7N44KUgT06LnskRXjNDTkq1xH7kD+hn67XQ4UGj94yLEZc0a62hoUagqQWD4EOtqN12ejvMD7ni7UdtMtp4d51"
        "rS3W432SfsbBxWJYZXYBVzQG+g7GWX8ORGjk/ReQW/0yKTFG5Xu5iFllLWCB0e4aPkpUfIdhZuxWqko4mGG0Q/olkPUmm2l1GXv8iLZFSHG73oFfwfW6"
        "sJJmJlJuJLL1zXimUClF+DV7DZPFLE9busJWLeMcXak4hXSqVVIqntdbVBlU1hUlp/J9ro4sn3bval8R+urd3Jz7jbcxegAugwZrfSiBYiKJ5OvmyRZn"
        "8fQb3QEnoZdzSx9hyVKMCgGks7x5kvsKs2towCqWhww/9h+3FbPpRr8U82dc/i37OAD0HYt6xy78qwYMmYqzRo9M8sOvUn/dLrMK1LKDx2gReRE1OgAx"
        "LMmtjsbhQm7AdIYNfg8StIZkEXR0hFblw3HWCGQYcTtxTOAxCqKmC/Yw7HJC74GoESfHwMgL7Ib5TruR/pgMw9HYWWyXC9Br7M5UXkyzBK76eE2OZjc3"
        "R7tpMPG8UAYwrR7ZSh1ELKb0vR1Lkbkw3EBeO8S9EkYrIsIRjtgUi9QkJJd5XK8AHiGjUTdRJiuYGqoPecLCallcFdoI6RiRQg6NS5uzJ0macPYu38k+"
        "xbMkUyG/q893X+8mCBodocqHSmiZ16Gg31HrPU+hSgH7dfAMRd7XwBF/Df/6fPJ1h658Pos8+cP7moymvMF3eM3qkLJiZHJQAltqIgdfPQDsfzzafx+5"
        "Ye0FpkKwa1TdbGt9EZ9xFCulFu0Fl7QkayWCxOu81JuE3hXbmqMF87T6I030LLvxMEiHDuH+6OKhW2gRzumNXLI0CDUhUts48xaDMCBbo7CHAuOm7ade"
        "OySSOFAY8alpI/XgUBdwGKgewbp6W2tZigLQmiRZUmvXUD4iEe0jNY27R/tizNAtY0WUl1WuTmbtVh0Hho340mkieZhKTQgtmJVotruw2XtJZjLz8Fvf"
        "C6U/Doi4UWwwHJuQsErltTcaD3ejYlz0ekEZoYIrq2GbUm1toO85eQAnqdr/UKfd3WUqkfoT+piZrY/ZcFIyVK21UY3F6olMq7H0kcZQdzOwfPD0NZ9J"
        "OeMHhSq2IJTIvaY9bBSgPbY5DnMmVT2yyk5k40zuRPa0nZCKl8oZFfmjrzir2dxMd2uJd3dNFWKpPJp+x+vOZhopjYcl2hvN7I3caId01u0LSrtN70Ix"
        "itru1xWCDRnZHUuccyCPodNRMLYe1Zw4605FXpOv3aMMO2B/8ej1Iw9BJIuVQV0wAHf49S7ipI5g4ZLZjKeRV+Yr7n3X3R1g1ndfq+LI+IQerngnST0V"
        "t+WRVjb/62Z7e7Q9dhrDalZrqKHfJOQTFEbtTpH2fJrf4F1jiCz48NRhXus2iXvpa+LPnHtHJAFGN6vD9AeOFjvq4cxtz8d7qW3+CRN7rGW5ZMx86bbJ"
        "UiFxm3PlmtqMApXmLJlmp+Y8urGaYhCbxIFGWPXvtKljtreeOo0O9LN6fUpCLJU+sZ3Rw+2c1kIXBbcbbhs2Wqfiuw4m1HpvdjnRqi2nqMYj2iID5uS8"
        "/CdfT3ZCoRhTj3L0+Gi+c0ejNPOqoznQHn+fPhwbMGuvDBhrOpGyEDsEe+KQqImRiiRSKkLOdx4UhyRNoiNBqaLsdo/Mfw79ROO7T9z9zrT4J6kQcPb1"
        "aAC9Ah1tyBtWjlmoRm2JN/7i19EGjtRO16feiJ/t4k1T1h0w7zxeFNxrxCJPrFmHI6P64UCOsMB/bNxP7Z4QfPCE7tUxqnRvL+ffXjXZOb7J/UBvt5cc"
        "uE7pvNWnQFjqMZZiAL5CUkvF/6spHtmHRlZTPLmVRBrOf0kz64o6gl0OhtX4UmhuYvEQKi5joGLICVo4GqIjZZjxFN8Jk8slhznT8J9+N5IvKudyfPg2"
        "hFmQzleB8cNIGptyYSvfpxtLnJ5f5jzdmy2AFVf6iKFnC8NEKeOqWtnZ+A3ZD18+zm78/c4FqyxmVJfq9cXqGikzFTRLJfwB9NXK63JJQ7lGKVYdo5T2"
        "c8dgmxRDtEensi3S2/GQ8ZOAtedXreYJgLCOYiHE2OTWlma0taWpHAC1Qlq4IqrtU7LRApalzUpVyu/JDLtr0UarfhCs0yfUFP+wQqdP9Wk4e8b47ojv"
        "TJDyu/RCPoAP+PkZyEI+sLpUw2iSm8BW7PS3Xw5f7nz77VY5fg4/d8uJnzQMB4Y4wHyXFrNHJSCEJTieBM7PgGOQlKRxak9pBPFCMiiDysY1gLQQ913j"
        "/jvq9k0AspUE9LiFMK4vci6k8WqADftjogYNmW8ZFZTQmtaK7Y1I62mgPYBwoDuiEer2TkbD8Dn+fR7u4J+dcBv/bAMpQmzLPx9hWwSylMoUwPmcJzfA"
        "cMQdQX7My3JZhIOBxIR/IjN36XXkM82rzj/iq/hwmifLsrNIzvI4X3eALwYOpuRoxZFccXQVUXjffU0eklLEgIdXFxNgaa4uGjiazs3lIi1Ev9Dt9fV1"
        "/3qnn+UXg+3hcDiASl5HbIc32vY68lXRe+l10L3L6+wm8oadYWe03XlZEznG8r6DaffOF/GF993uEta7c54sFkBoPXvz4vW7kdeZRd4+tDEfbV89+3H4"
        "xRu4xb7//u3z4VAVe4bFdhqKvRu+fmOKvcBiI1kMZ/FdR8ow34vB7Q7i756gQajk5GYmqLPUdIPp154OGhj2rRrquq260qgbNahrz6rsERVZ11Yzzp/t"
        "Z6ILQ2SQCy51tVuJ/gMVqr4NjfqJYM+oHkqzzfOBVbvZ29jDzNlTWhS3oJVcaUhoYlSn7jfPvakwOTzWCi9TCgEiruQqYSXSn9SiepUS6qTimD/mZ1Fg"
        "A62QbVZfqXq0LULN7bUNsMfliVatrqRHwyYIhwx8E3PG4CoGPj6OxlbbhtfrtfT20NMaamMqYa16E7N06+wugpbRcCFA1TFhlUDX3Q/yw0Tl6nkkj6cb"
        "Q5fi/T+BuvE91tHq8ZZGiLmpElHu606rsOlOSpk6X8PNck+PG1XGoI5m/g5/UK+NfpiBK/gnD2ylUKL/ZYT2nzn8JgX0v/CXoCV/wJ8WUET/5Own+VJR"
        "RNVr3tL6/pnLxHssTw62GuBJMCpEfUEpYgraiv2gi1mTayv8T1HY0jdvuQ8s5btHJTrSm4AV7soVFGGIChG0XUH5Y7Im2YI+PbrFka12XGlUFmqwG9pw"
        "WiGa5byMhKXsURY1v833TWQN31W4vpXuSzhAyW880k4d6Q95ZTM+oojXUb/ZrzxKGlT9SQxylEFP5Fd25wFtUFQhlXe1Flxy8TqlsSj6byCbG+GjBJ+5"
        "S1ev82BVIpkUJU17W91UFfC+0ixMXb+8vYXZOZehAYFmyWJ1m3/l0jNQkl5I60Btk5Emxfwt5FFAkc//k8HY9iPkfZwey95WNXtUYKlmWFSVmdaSl+vf"
        "aMiCh0LAB4cZjYRnFfEp0ejkSXMPQrsYefmRIsG7O9Hq9TxB73riQ8jryKuE7A0G4q6yJM5ciCAHPs6sAvaevORbphXZFQ3Bjn1I4B0IP0Siv4n+dTw8"
        "CfHGelNrWcUngW0UPl2EDXYiFQR/ZerXb5WyRfSxpTHhOEng6N9Jey2JXG9FQimBHfrqjZclE0+7a8PoSzhsV1Vo3xg5NNRbLVUtlLrTjykeZMs77U9S"
        "ohkE0oxqv0oe2mGmLJCzYGe3BjkOQA7DDV+56hcemXTNqAHq3O0J9JJzveRVx9zWRmHUbQAu3l/bhoz9GxmIV3ytK/E6XSwH8AMVBlFt20RA46aMNbN0"
        "52jWFSAE0LadYpqlYft61/pn2Wxtjqo6Dp4d90TizRZfaMLrXv/w5x9kyp6MGmQ0v1QTOqBQdt55oJpf77qSADxxnlPMLQx6+VOhNBICbbNoyrqT64k5"
        "GMoMIAQPkHueVNh6+4S7oRalWaJDthqDKUvQ0WR9JuRqVedUbtv3dqQrwEo4eNJk/b2CZu1pONaYqgYdsJ+WD9w3Nex6z8yXcyy//BuAAxv65am7Uoc6"
        "YTv12aAZg5w6D6OmWp3VsvM0tMSOKPh4VSuMjo9+rSKgGLsQMmLOnQLf3LGlxBwySbhNsz3gA4CogbtAe9C3t9KOUO4CZ3DvWlL+g9csY1jGCpazmM3Z"
        "gq2i4xF7xrbZyxM2jYZsBidZqnVMd2fjKap2HE9PkEmZ8ahIfPxijlLJM1IAofbn0Qp1V0gDZkp6h75pMOiNqM0swlbziFqK4U92wnLRweZ8EqtfeAkv"
        "oizxYxjsXAiizTAWJOwXPNUC5Y+6Hjqwe3I99TNHYXKUap1BE0X9K265gGHW4gm37nd3FIVETbHmnVV4USvIvfsyLgpyQLEnAt4C31dkKNCM89SUBCQu"
        "nMFDwkbnIF2sgdDnwp1rB4OXAHZcLDpnvLMqhFo8LOHwxFiqLKJr6RcLdpdCD00jf1XxhGRD0KriGSbYWlWcydRKWy0Fu6MXwyGs+iL6F5cubuRqADQB"
        "IJwClJzuTsenCCU4wCUMsDw+hSHPFFNswl0cL9FPUW+B/zL4nV7g7/TiBPEmTvE0yqM4mgN0JdEUQAqbTiJsPI1m0CoA3ew4QaeCaX+9lfVvehn8TeGq"
        "jLvIx9x00UPQVsHm9Lnuonsg+My70c5WMV5FuKH5ZIZ3/XE8ABga5CewktzyG3Tpr4wwDMa8wtF2xZhXONquHLOBI1obW+sbhj+0lXNtjdxboR6Gy5TB"
        "MvFuRN6bWEI/0guWdrv3pns+SFkySE+EiXSZsAu4QE5PyYLq9JQ0SFG5fvkxW6wv4Lb5B2dL8VP6c/qKM7TJz7NkFv6L3xv0wRNpIXnuUyhf5WJRGm1G"
        "LT5Uo2MESUQEI5we+UqV00t3izGMPvCb3KIBQdPD2FHofmgNv9b6NX4r6fItYCKFMR0ty3fkz0AKXyCVXEgFsJRFb2SkMsfwZaLYJffYXQRNocBEKX0J"
        "31O+DAn0ExBtL1/lebzekOpcXc+bmNSQ/g187AtmGWXHKXQBM7XMSqVqASvEzCS2jYTuXxxl3dE43o1gaOMY1mIe5YnPj2NArKRwhjgUGbjF7lx4lYSq"
        "8wCWDiUwyXGOnZk+UOArP3KGMkU/gVTOALp6I3FJYYRPKQSTunuEs1Gjb3Mzt91qKgfeGhP2C7K8siA5SSqiGqJji7/y0s9lFnmiNTXSxNa2kAPCJYnS"
        "SZmEeKvgM+wCETQ2LKR1SbQYCz+wG/78bqGcaRxDUyeIfOebC+NfI8a6OaJ97KmI4PpYQIcZ+cFCL7fziV9GcJijGC4LDr8W8OvejDFrHmMdJHHUqL5H"
        "QbAv4xu1ZC83k4mPgH7TjbcQsWDhYMBh6eEjCJ85+XM7f4752yIfERUGd1sDvCO2ws6DAQx6tIkmQzAzu8DcFJCu/3HUmbX2RWIfsKGW4PdvdoXrr5tJ"
        "cheNQkj4Tvj8QisISNpGRL6WhdZY6BkUWstCa1HoJb5x67709qeivyziauUoBDygYhh6AlPnsLLFVtHNt/Kx9v4VQ5ujXTgePvqE7mWAlbvoD7rHMXQb"
        "THHio6ImtpjgiokaGeDErZgBjsy3YvI8WsqOqCpLJ7KjUCxQhorzesh7Rui3MfPpIr2787IzRPPeho4SBunwP8t9pEwxDcWJJT50b/a3HP3notMbvLI7"
        "0N4pXu6sswQyt6B7vPO+/z5J+U9lsugnxfeQS+wQjzGIC8NBmp7mSSNF8v9bemRu6JGFoEdWkb94gB5Z1OiRxQP0yKKNHpnX6ZEphSqFUzDbXY1n6qI9"
        "pYt2BkOeNtAjp0SPkD9cdkr0yNyhR5JINgjIHJuEO3oKjdlDhO8uXEKDbdwj3J8kiKHQ8GTMF7D3ZBdebeUcYB3bAXgW1Vmy66cA5FFmN41ikts4Oi4A"
        "5gH/+WkvCTDUoV8Q4XMD5wFOQY7faySEghPpPXxRIWhih6AhUqYrJk2kTFdOmiiMqzp5USSXy0Vyvg55wmRgtUN+gfzVW8XAJAkQIVkBm0RykYNUFghb"
        "XgAUNhGWgUtVOk3oDf/d7ILvIfEiVZHDTKS/Rj8TMw6XCjst/nrT2GGeMHHIwj1GRzGME6KIUKwnSaJ5cs/WMM96tDnnkWCJb3xAkOFLX9kSns6pgKG8"
        "1wxx9r0KiS18SR73Ri+HrPft8IQd469vybHtDQzhU/jNzouXo50X7NPp/t6HA/x+/s2L59v9ndGz7WfPRy++rTa1PRzuvHg+fNnfebYNub3R82fffvP8"
        "xbD/YufbF9vQQ6XA6OWLb5598/yb/vbOaOclnNSmWVgaEh/3BjhGqfTxCdWucAG2uPLRIUc6gCs4MvTBqJdtZYiisy2RmKRo51GIIgAmvmr9WS8dbAcD"
        "rY4BVYtg4I+6wIBnkAM99hLRyCK78LWnxYKNeG80DKquKnGTtvgWXk1tm6Rpc5gWTG4gh6JUyj5Vp5aqcdtTy0USv1n6eL8PYHaxXrLt3rYYcoxTBboN"
        "uZhF1B+N57uj55ubMPQXu1oquAjGc8AEwLaaxYrRFM9eFE6LAtRsgYsCHM6irTcgeXvx2IHEGNYDIHELeAc0sbiuH+v3aDZXhuuS7fN8ijZ34U3JDpdz"
        "nifTeKET8/KefSyF2/iiZLfIbofeu4+HP4Q7O98+9xQ84UGFBrSrO/LWFs5L/6yM+s8Havu3bsr+p4D1n7PeWQl/8I1xkTQ2/2xn+xun+XVT8yMC1xEc"
        "BPohWnwvB5xBi482AOR0D/6Bha4oJ7lkMG7LNsnVvlR9j9rFEGZLCd7vP2y3BkpUbBFeOwLRIDWFFxKdt3GNApeMUoDPYiimK7l48s0iPyv772K4JiNY"
        "QfwNq4d7E300ny+fv4gW+vPb4fDb0U600gm41tEiwc9DxPc8eg+Ee5M/Loy+6kTqYtaDqwCtBm/upP7Z6hKkNAZET3fpJHK/z9GFnnqmtWySjrJXM4zK"
        "Yoq1aSw4plAVn1wtwWY7jko7xRArHV+ypHtRukGlcemEO5A9o7UlxJ0Pd2EH6pGhedS8ntRSNYJ7pUEz20b1jmaVFWvTyT04rF2jbq8VlGOcWIabuMy2"
        "n4TINhjRYQYT91t77FQhAnzj9FMkoe0fqixx1yVqi25PQspIWunWMScnx2hutC3UV8L2ZTAzrcKkzOPJqb5QBlwlVUdcdT8B4q1LBkORCxjoYLdz37UF"
        "R7UtFJxWktArkAb3SGiv9c/IwxokIHTrD9s7naXbbDpnpZz9uMZcHAFfAIgUPWbOOoKHwtCUyD3EHZoaxaNq8x8j6AvSuGl2Y2BefyhugXBYYDlhq/i7"
        "0vWYW8t+B6MyatPVFqHDApkvs4iLECoe1kLartsU0DYdLZxS4s7mnvF4Om/y9qOFaJ1KlUCFaXA3PDlxlsty9VC0OZyA44b29RP06TcpQwx7fXwSaL8T"
        "u4nx+WQ5seInUtkO9XsoDpPTvowDXTVYhPNbyxEmGoFWgsHTS97OxbAVBnPez6hTMtDypSdx3JjmoZQCiJobJ0UzB07qJR7suZpae7yDO3o0wEXu4R/L"
        "u6wKRecHVX20yhCCarhBd3hqJZkV7TpTyzrhoXY2DuS4Xm0Yj11Y7M4kMd7VE6aT1doJEJM2LRH6osYpTUQ7YeIAojS8iTiUsgpxlmqvrM4S1PyIOuZ3"
        "ATMh3ivq3bana2eAMlGb9X7XXqaQZRpKtPaszHgbJl3tebe9TGPPcotQjS97WNvMEvzgpSDVm7h7CVCg1kAIJ4aCNxNHO9mFWg1HOzkRJEcbTlazey/s"
        "ntsxtD60wp9M3fS7dMm2BidiUVlFfCpuwwMDsLpbmGPebMdeKdZ8aZQnj+N130tXl2cUD07JCRuHWsf95AiybsNuT0lfD75Fx74X5uaCEEnSq+wzbzT3"
        "SCN6o7B86JEUX9wfQGbRwzewygIP8dpN41dcwgDqpwVNEPnEy+Vi7du2kmKlWhwXWhPRYEAMUrPmd7WKM/Hgf/HabPTS1ATl5Ykp3BBH+fhk3LKPJFwk"
        "swFS+5ZuMtu6E9uLDsRkSU/5LJaQFc6Rhpwm0SrROKPxDNutGto6IFYkFLtCdPFH0h+SlBQ06nhgFORb3QljG5mLNG7rIW8bke83Hn939VEDSzT8yKit"
        "3h8eeJ3+U24DYfEP0ejoKbtEBcUmneXkwh74yfZ4P6qmXdYzdV/HrkOSB6piUU+ARj0+raJIkEwvqmSHPCGOD169zmOuwCrp66Yn1m8/CBMnLp16NOR4"
        "kc2SFlfdy9XyVTqdZ3lIrwJMRt120qZ5VhQyftbG6Cm+vIWR9960xebA8lCAZXwvmZJTPl3zkKJwP62uiNgt1acaO65E+kLKB0v8lKOVoAiBqF6kHe8J"
        "e5cXGAVoc9Pb2/+BYhcCW3yB7oaArDPsBjrgoAYJ5goKKsMqXj7MCkJr3kaFmrGygQxP7O9oo0b7WLnoYKstD2hMnJtYW3JHV2MRE7EKxBMWpPTXwXl0"
        "ZGsdv+Cc3pdm2bQI+sZlFE4eA07b825fcEUgp1FyzLseGqV6qI5w6adR7dZOJ8cpS0/CFMVkl3p7ReTIRMZnF+CJTtBwCuoL1ja1oljRu/e4bHISJSLB"
        "97wuJ9/SpLgrC93deUDvZqTCKkwcoTAsJwXZxUccZchoZ2J0XnzQER48WWrVFhZ5abWe8lOq6hjYvbxojpdHXt9afWRdXqCHrCKfogsvYVgpYbzpFJ0B"
        "Oi6TNHZjKx+XXe8TpUM170RK5qxMSkaNNnqoS6JZUkcpEqZCT64xfveX6YXH8Jdu3snvbd+IImJ37WyRYhpA4AmPt5+zZ/gIpzc/PB5tU5KD0Uast/Os"
        "htNG37De9ssT2Zlo8NkIaz+2bl5RIpI3T9GnAICX8QX/GKNVkW9/RioyT8mn1CQmos2HXzFkluXv7uzaQXdmX57WsJzLk4K+58myOlyjMuQ+4yEQwZbx"
        "qQj3xIHwutchN7Hs4L9X+eIP3z/+2jsBCqDfDf4Y/RF8NWDbVJ5K+P2twNq8P3BzoMRIxBuyp1unx2p2lNInmwADNBL1mKO3ik8sS+zXO4OL9YJsjHu0"
        "TGiiUM3ZExkKTTktqbgPKqqUEkTqFcRYZYDYYSV0vb9WPF8fShdy/teLJP18jMa/X2lU0p8WhXfydYCxEjAHg7QJKPGHTCZJO2WnijFeDj0Pb+hlAsjg"
        "ibZNuPZo3qTsk9o9AWDJ/iku7rgSxVrLe3QKUSW/4oqWhDpZJR+FtLdaVT3UVivKoIVJ3Wed8zHnmMmcRJ2C0dDtxHfpTMpzZZRXn3TEWRVp6/EY6WVt"
        "EWojPz///2joOugrq2+BHUHGpD48wVpQVtclkSpaC1IudaBJRvjnqijdUHLVACnYL4UfJOcRqQs+tptWaOZwyflMR39oLvMRIBOOACNzFy7mGZC2UTU4"
        "I4utRBXaFRJP/Zx0nYwpRxyo+JA5KT05OVaUymAcm6hPBco1kY4wj9Kx0KtiRf8m6MmPYOCLRm90Ss/XgrtYqGbpGvghauAvncKqfaxR3UP1sdZ9rHVK"
        "rQ9TAz90H2udEvQvV3iXLdZA4SCJJ4P1ZexWhg5CCr1+dJWq+qlcwlq+tqdQJX5X+9bWlolGR5YIliET3PKAOIsyUh56NQj2zxLpIgIfLqQVmzqE9QOc"
        "LWaCnXGBzeJy3FNGGi0fkQIw4qZKRrWKCYQkDXDqJjki2KNACK3ouHoCUFXUry7I/2SJnnR6BUkDB9g+dpk8102BwjGI2ubm7yRAQvd+Ms54JlRo1M/q"
        "Puhv4S1WRZKCATcZtEgM6dJTDWvjSP5MFy3bReEyq8YaJUltL5MoayZMSSXnNGGWDw70ePSZr8+yOKc4KqVwD+ixeFGG3j5167EveyK+AYV/HbJsGU+T"
        "ch2OWJ4U/CD9EV/XMYYKfYpS28+HTDy9i7GLl3exQ/RG7ZnfXvMD/IhJaDpIv0cPYzg+fQ6tbOunxLzhMfT+HHh3G2mHo2Grga5m3y3pNe3/tV8+EAfF"
        "efqtJ9GjoH4swFX43Y4MyFpqOKE+ncBoFBbNNpHF6ZAcQCYotwQPSTStkPH2pwkur14l1dANiaQsy3SdikG3DcOqTNsslcfDp0xTqg5YE5VJQlLiC4GT"
        "hJsatUBOyUJrfRh6hMl5wUs79V4KNPHYtZIcAixIGFctWUFMouS4oTYBlbtftbB07FajgJAzUS90hhBYgttHgm1rL2tWYfU6YiBGMlmP6VEgIqGuH5Rv"
        "2aXdhxj/YcjVl4R1j+GdIK4vK88up7qz9GBqHsgrGymm0eJKbFwraj3gUIRl85zTFMnc2qdAOoayhGVAPfhl4MQQqS68xcaY91XXTt3r+vWTNfF02NTQ"
        "Q1/30ribxBCW2NBMDLUXAX8mkZ/ot1ExYb8+e+ccpoQOhAdKsn4Rrp9kSsCU2DBRYkMqBPcKFIF/SdKEPA6FTKeAHHQLiabiMxHvyht6j/m21LgQQQ2V"
        "CK1LSYfH80XIErqopLNuS9YtIpZgFBc1T3207huiNZgraXOT/GGSA0zPkDcyVzvms5ZfYiwJDPRBHs03RmOzASJdb4GL67DwELYVsuXiiVX1TFACKklr"
        "Ia/q3ZHrJ+5AJPskrNNxFFErzPVabsOJdW4V/UBwAAPJKo2UfXO5NzUo561VJ6rg76x4w3YiP/u/tJ+frTk/trFH9fVpUXJrWkgEVylJtje4zgcoaDhy"
        "V8/dbCOTdqL5mZBtdIx/t2dXuk2YXElH63xxb5CdT+t9wmorbUUcaXhebME0Uj6serS67xI93Rxh2ESGJ2VBhZI/8OsWrCwDnjMVAF1j6XEFSyv8bCC+"
        "flm4F57ZejqgNjQp/G2VUWe2ScfSAZplYu6dGmHGKoSdKlin7Sr0m5bXVDKEP4pEKeA1k4rCfQAslcQmjxIDEhk1KWZU0ZLjVavWftMFqVof25D15kF4"
        "f1OFd+ix7bnU0cKy4u84WIpOgkBrDY/qjY2gox0LszQ8l+oQAhjJV0CBEt7aVJamgfrqoWByCVyw+gjCS3+IOuMoAzcvB7qM+NSlSLKylxZAQrgH53Yp"
        "WKyjbIlvQkCcyoTX5C3yE77rhKWRSPGAXt9PKU6veZx4ApFpBSTW9URDR86Lxt9pynkLUU85540Mc1HmqNICNM40W0A33n/t7Lx8eX7usWt6vAp3LD6Y"
        "jHXiZegRGvHo+x+AdXTCLC7mwmKXFM7xUxLt9I2uIZGNxb9vqDudrIC/v02fn1bIn6PvnwxVHCqsfJsiu1apbcDBOcyb5yhSx/v7k/yS/tSqrG+1kiAI"
        "6HHHZdgwaL35UoUBVEzZh4JjmiribrRrkbD6+uEwE9VWpI8Td5wP61RUXWuqxiyyzG2dWmqKqC1AiZz1HpAmsvW2NY+Lg+v0Y54teV6u5fMW8wSMeYGL"
        "H5XYWA3/Kbod1rirw7arO8vyBL2Px5vF2lXrhMe4MnwCU+izDhTKQrAac5bCGTuu2Grt+w0b4tg/9MWKD7bDYdCtbrBBH9oL0pBkbRdJdN7wCiyO85Bh"
        "gCvA6f+u4EkBHrXiYnyR9h+TQ4gj9e/KIf6W2OSTWJPHaAV70noF3MFSx9XmqlBq1ql+0OUykUUNlVPWOOJLrdm5/RatGmrQ4tKTUx6Z8GavGvdpWCbH"
        "aU8XI7AmPKAppBZdK2f5dPAg8fkbehfUGj/O8UE4iI7LLjCX3USFjlreiE4iYddpzcJc9kbET+n4llK2uGxvoQCFmoBxSlV5Rm9GvG+SfKowLwYNuly6"
        "xGIDRKhARDbaoukJAh4NeQvfnTihGfmyRhvUbBhWc4dEaxHsOovfbV52RZFcJdFF8pjyNeo1WGrXdV0iPiGTwoTdSjzE74OwEc9Iq4j6+QvqWlNisTsi"
        "vzONU1SfOuMdaMFT7NPlp4dR1iM4wNT/+wdeVn1IAzE6do5F9VBYnye2jWrh26e19pbUciCsaNCP1ZLHJbCxVRPqacQqVpBN9MLgIH8y1swrSXGJDq90"
        "yIR5FFtqdMV4rgE5iqLCgPXETx2za/0SLtZ9AEXRHLmE5tR1fZx3YUQnyHrYiT2VWEovdpbyGkotY8snAA9owLLreAq8uXg8xl/ZVhr0tFlyDl/GSDmB"
        "r2Bgyppc/KLcAIa7IU5ACvQWetRAPcQ0yga6oDXjrTxwsBywU+YpvvaWX7nDVTeTYcjp7VwvR8KKXnoS9G9ceFSeXoLQz6K5tSJzXdWVNuuxHDv7gi4v"
        "3GHHT71ynJtYQCqOvKF2FqAeQONFJRHbupleKi6zrJx/H6OeEXBTafZmkSzblW2byCZNCAmpjSZJikdoEpuGegAjOc1XsVJSvGu+cjacnqTS071yPWFW"
        "rsU4TlhuESeYRnmi3FcpB5uliZy7W5CvLlU1twuhG6c4GsEJz1X5eHdOzp4IayzYik0jXNZFlB/HvRF6GMvRA9TGMBhPdzlZrU5xJFiGrYxydULCeo0o"
        "jOk3DyjgDOyCdGHR5hddWSUmSh1LKMQdzuOlrUW0tHFTULf23F8VZQeDRROK7ZQZxpboCK63syrQM4weix+0Kqg7Wy7oAWmLVCGxLYfTpJjaPHhxuWrf"
        "7cxuVx2COjSdtsCjXVuo0Ds3eWGiyl/x3DkMzsAa5rvnnPsimDifDrmPbmcFHWR10gK+xycAM+jPR7qWs/yuZeR3LZn4/Dg9IZc36DOtaX2wQABkC5Zr"
        "niAW0MFP7tsvR0G4SgoFWVQMUM0czvK9mKO7HMyqwaq7mBQ/A3aiR/PS/lAcZHz9UdHNZSNmfCoN30ChS3ciTJuXWr3pEVTI9uNaSVKtqlPwTon4RhIn"
        "JzRed7VqRKn27EnOlyxXulfkCEvZ/537hYAWjE+LIAJwkRNcZGaz21gjghd9k2QEA/9vdd/e3baR7Pn/fgqJs1cDWCBNKslkhhTC9SNOPLFsH1uJk+jo"
        "+EAkLHVMAQwA2aItffetR7/RICln7t6zJzMW0eg3uqurqqt+JTdaGd9aUEam1vCXRgg/BvZ5y7CEwrMhcAQpT1iYaFpGlNZaT/ozuM+OdBHHwgu8xQdf"
        "bFdpLT6D0KR96yxWj7LDBA+B2A+TC7ugmvIKyH4ljwnpel3a+U4qx/O6PyLfa5jEQsAU56cJ/IPoT/id0UQWMeDgnEjxn5sbGDz+4I9QIOxWUpyMTtHF"
        "l4rt7eXAWon+ATBXdsbRKYLF7NPKUtBN7c+gY+o6w/X04DYfkXT5m56I0xSxJBGYM98onEZ6+8m1YW7ynb56Silp7n5HQRZBMbUY2yVrBhFgO6iEtUC7"
        "VqQ2FoX1qNx0g0yGACZDyNXDBB1BvjKH1RB6ASHCLu68Mi0YQixCpRny1uiFj3BaGTEmuHMP0wuFnqhAAYH3A5ZxJQaEiZVmgtjHa5GuutVtd2XEbm52"
        "/aNNs2hbcy7/O/9/zbmsO3x5h6wc5wk3u6u3ys3+kOM5OERPI59wc8og//MqWyDRzBHIFI+8ZUk2pd1ci9sXk8nth8+BWNjqzNucOI+nm5mayKsS6dEG"
        "viaWvM2djwGfDLEqOVGo/jlJKUpv5p63ZPqizlfgmv8nTxSh+LT2yaE4NqAO/1YLno8MRMClw0BHF7JJB6O5fjEZTCgOyRqtm3MQKiBxsqVBAEpnovRu"
        "D1I+Dd7KDMkwycJyVqblrBoyAf270JexWtqySWCNR26KALcizRmPPIdjFliX1Xcg1u+iCxz+wL10fUggqwIhoQlrVJARfCF/7QuCQ4XhLTS/u7i5aW12"
        "M1X2HqOplFL4R5HOxGZshzWADiZg2+OsyUjIkL+7Pk8qAV6awbs8g77nxDyVfLAzM1L6qC8RKmHQ4X9wnpcUIRPjoRTqaYW/VW34e1aW1VwUsILq2Ouh"
        "BxCggIgDAVqjXbSzXsCmurlRv9hfDD75GR5cVRxPo1q1nP6OaKtJPZD0SEa8So3lnpTvlPoO2N4Ew7d9n80unnAdCHzsJEArtbEMYZ/0WnuCm7pCCgsD"
        "GTINozdoPSJxGmODhsMQdI03EkfDAtXIa07/inO1kAslDt225F2IEm0J128nV0BX6pBR77zl2mgFKXyqnnpnqcNJj4HGafLLEZCS/ip57EYrOAtB26Y9"
        "+YFMRJppo9fjGFWq1bSyFyJf5F8gIV/gyQpCilIAI4zEFadRgVpphW9unpHz8m62t7dbxZbX8aT+KJrZRVRR07BFszrv0V7vjWW2lxhYAG92rgihMZ5Q"
        "niP0m5EZFYmrUQfoKpCoFJKw5IJpt6otyV0Yy5mILmTVCBb8mj1Dx6YtO1WVS48xEoJdAOHteRaH41FyxcDRK4FBkWXtEiferlonefWqdFPpaHygKr22"
        "K/1BfrBH5WLBSIv2rFQWufEniJbDDOnAZ/3V7fzI02LTY71QEA8S7/bh5Zgu/uXDLfVmb09O9Ewr1bzplfV0dVXRv2BH59hRk4cA3ePJXDc6DzQqN/+4"
        "xak+LT6gdmMHJu/fr188l6BqiKJm8KBfWqjbar9PCRE+JueTS4G+Nuiozv4Q9dMC4TsVqYEXFrj0M9HGqz1B8fMEkRpODuyQBsciEFsENx0f05rLrUEk"
        "qSk+Qz7FMngkAzOL/YsE7ruY0hD7iMVTvepL09iRcDSAGog7Yo1fjOa40xMh4XjRQJeibzbqJ7yGn6fjcBYLyPuTNZ1qXOtGJTudTz/JoT3l0eB1Aw4S"
        "6hkfyVeF3tC7CC2wtzc8LDUPJyvCmAIKdB+mxHTskfCC7sklNpUniHxMzCbBu0c4KW2889+t79umqzc3gaVvUd3x5+599hkFOEWTb2+Pm/RzU8p1Gzoy"
        "H7H6OZFVMp1MbCp+JBXUjvMdBbgxJ8Zrd1JwxX7kpNtLoaEVj4FZ+OA+nruPK/PY0W3Wr+76UtMkOBxYDkw3ezKusCG/zhA/CU/vifRzmGD0PY7kk1x/"
        "acfQPQ+OsoBIBnJRq10xPRjLtodJ0zUq4Y5KEX9nSE2aM6Yg9//K6b85Eh02RQdY3cyvqLgeAz0ZeFGpVp7DjkLr3iKzTmSnzwItagKza7EiUg59p5hH"
        "+0k335FMu0eOze6D4RftmYFWMZx66MDEAhxdaR1HZ6aGrZKdqYIvXagpVP2Lx+FtD4WJXsS8/bmcxP8qDMUBflZVGeOki6mednNIjwt5RgeGBWTKJSzW"
        "u0Q1Mi5utTB11KSvRfJ90IbUGIiSO6VnrjnyoIWSHA/aF4xUTCggyvMSKtAILQiYkKwxCZFayGqhLxzkldE5zriU6US3ZSehS2h4BDJ+x5TIs2nc5Fji"
        "Ze8wi8eav9guHgurdta6r1BG1zI1bHt6HCgRHME2vh5Ux1+yj6cq1lvIrwUFUxUauEdVXL+6kyUpKcTf5e1J2s5ilIqfB4tD1zqwetrLWc6KXQ0j/1iV"
        "+XdpTfBuN7X89h2rYWO83OFYKgMjGsdSLuQ7lnLqrWt86XrCRuyJgn4tacsJdkt4Ptcfxwerlc44Zjhb3ntv40SJk69cZPB3e46UF6D+jsobUJsjUiXq"
        "8MEc45cS0WlyFNnUAfP1SQmlPd3cuXSy62C+5A3pbWZNTblQ+B1e3UMC4d8QGEJ6RSk6rqN8RiznNDNIzlQddASSe1wHEXaV5a3Eon9RkGAlc1OW3l9C"
        "Tsv/U8hpgUXlUiGzpsxXS2kXArfJm1Elw/jJ5dPxuVus9eVy4R2WCsqWgsnK7RInAYcvXtfG7Utesduruu33dSmKyZlDkxJWObWN0O0rAM6ZO8awa814"
        "pb2AFx8ouVNJikPEUYdi4qkpEZiAKJ78juc/jiZOXOw10YG9JiT22jovq0fuvATOqm4fP4efsKNVhRYX6ra63rmEvuUe6GYnzA1nf7V6xNbtcr9NAo5k"
        "Hhs2afTir6iTukutU0meQx3XhwEqO7Du9lBCeS/S7wMXnOikuoTeoE/BoiyXjNqRLx/US6BNrxBNApMur4DUISOJeeunBXoh0b3oWgL9y9PH37/4AhKN"
        "tpplj26s/v+h0/kc1mrWTa0bO3Ik7S95BtYPV8c8K1GvLq+qGbKoJP5wzExhx8wsVBjD8hSXhLLPwfUy1DmnxfiEVswp2at8nptVZeDosciJ/nnqnw/e"
        "ItjSv0huo0TGlnsiyNFIJQ90atrDm3U6w9QKTHd3W+7TmA5ZcGH6rzENyRIuTP8dJaJ1nFmsfhbrlQ7hxkEjzVpVKjCl86wQZU99oUnlnkak7nNEhAr2"
        "nRLlXji7b92mcbaFqnzi4LVtsw2aL9kGTec2aFrboHG3gRrogzBEkCucluyfKEFwLfGTcX2YoCd44Ygs4hqpFOgukkw7qPqHmxuydZ9GbWcEfe0YjyPf"
        "B4+/a+rLZrITxlqHn9NQJhT94PO/CGGXRE2qEcnVLlWsPDfNth9eWBgMNB+MB0SGvts4KoZw4VUlTXl+vlgf04cJM7UWYfzp1hjsyYMBpXaCNoHMl1kF"
        "XEBeGFkf5yniMCPa6XA7MKRQHBx1O41Xq0a9ACMGviaiqCbvgEKbDfIo8oqhZzLh1R+LS4R3cPAxZNoWCgFdXwuFZ3MPRrKM4zu5lbLD1PGFCg/T7U58"
        "J7f/0yg4g4FJS2GnqznNomMZdiJpTdfBcAjb8rhjHsPj/3Sn8a9XqjhT8FfRmta7RDrmf8oupRYS9MSDrTMuucjQSZLY1StJiKhbrbxNOLMP17S9S62e"
        "shDaUcDQUWWXzMAHUYszsUC1FAIKzfOi56gX5AA880ezp9dN37q2eoEp3loZ47bX0sq4r/+iekbUSDIDJoe7QSrvnhxfpHjztt2XKN9aVdjk3w2lhCGv"
        "hXNgxLYBJNtaCftwnwkTSk06r7hHKBsSmXgfBZqtFSf5KVf8GYrBgwwOix0QpjHnKBP0tsHiwohScZMKW7BiKzeVhwmByqMu8ygPDdYKKtB2fvyZgE/Q"
        "ShMy7XAvpOUm14Qo8bJqZeHvyHj2N9FUKaSF1Bud5DdnrwVNUhV34zoNcOLzco6yXMAmxsk2dZ4ie6b5vI7HTg7C16+VIYeysokboP+wsn48PnqW5jyv"
        "+KUnDa59OoaxPzAxk7hxUakHFMiZHhC53j648/jWEt5lB3hW0AAh8fZ00DNyYlO7/E6+1+mla1bJvHGcWDEVGFMkCrH6099bRyDFMYkEmnQZ/0dO0lK3"
        "zs3QKilFfBf+S0bmJ9crxjDy3r9BLdD9g3i/0b6FPt094/pzqScK5llgM0Kj9esBtwkOCQyozHjohhQkZk+qVbxbKytIuza3sClKQzDGuA+bqIgH5txk"
        "RzO0VzCbSaClAkWvulvTItz0FISQEvFhS8NBs91cSZfaBUbD4J7JeIx2/1RnHoiuyKSEbsMoqEbi+vY0ucyu6dONvxoOk0tR8MM3Q3zxIyPQEHHVIKhD"
        "DwRVIfTYuV62sXoCr8cn3yTfnJKG6WnxCxyedFeJ0sVDwtxTbVFEa0LIwR8viu/rWbbMf8pXnGZfWHaKXLtfKHOhwawGifRgIAe6c61gqTInsfr0iyNQ"
        "PrCNaLmvXqSBttTjlsHXHiaEJFbUDNbZSzjijIy76Yi0mv+SQ15bFG15ndL2gn0nlHAlq0I8WTjdyTWklzzIO6UHb0CcY82Y6Kt/4aACZe84KsSP9Ye1"
        "hj10BqfzmdEpM5MorKiWK/wRtjbtfDMOeH1o7G2Zh3wbBqrnqZHcPSHK7D4qIEGfU48rZt8LI0YH9LtK2KKp7rVPFx2VouFoMGHtEsF1QoYufZU6lz5W"
        "eGw7lfblcd2X73pohNhiUEIlegnQz6dom5s8zKNWkTh5jZpkSrlugIJc0UKQi6gRy0ehMfbhRV+PvpfYBfx8vUBdLd2cpovwoSKDqKKTsdJMjgtT+xK5"
        "FFVc66FNW9CnWSWy/iI7yxeQiyjcDn9WzIrRNtLe3+TWEhYb9vfDGg6bHSrNQlzaa6qrvPfd3t8ORt9MDu/j++/+nhCaqdxSNml4oVlUqWRSsYc159W1"
        "/EKMqLyJQv9DdUsF8h78vBBN/nqZzRDMpMTVgrdWfCrS8YfLTMcMEIkfjVPniVQ2CiXqh86U2fQF2f6IGR+3A/gsr8p6pht88sZKpLEa5zdJ6W24fj2r"
        "0IJo3ptEaPh6iE7POv4RN3wUjz/FwI6ULcGYuupvWHtGNlyj/lVYTE0tLAZ30uZmFQcbhwNutNBJ0FNnEoJhtQNsE+AqstC2+KzSYAWVJpoB3UJkRQG8"
        "y7SVku6OxjZiM4bUyaoadUzRMm8rpzimFbNIsK9Gw/jmZtgmm86C2M+1w5D77RJYitJPu822J/28H2T1gS5QIIqXbXmd8ZxtJBlNmPh7Co4cFppdyd7F"
        "FHxsTQbJOd7clBvrsjhJyl4ziiBfSpM3Mt4/Z/vl4Pq7mvyXKrqRzvrwhKlIuK77Vb9A96csHcYqS7/gl6v9HPKtoPQKXmV0b51D6RWmUoZ+BnlXh0P1"
        "Gp/iJKpubrIWlr17qraXytDwODJdBcbgSCMnsHJP4zjeIAPtXEYuI+Tysy4Y5nTdyyjmGxiO0v6URKrLvDrPpcl89LnFYeCVL0gcRvxBDpIytIwFXWse"
        "IyRFD0XCWfQlgH+t4Ve4zR0K897IlMtjhCt0hTWN9h7G27BqSsP9tjHizelOCT9mxXyRVzUw6/lcXbIiOvjMYt/0dGFkDGDU6lqHOwLZ5iUmyMhLY3Mk"
        "cgGTSAGIKPG2uwepYnlvk6siMO7QoPWKRhDs/6luj+x3Tuz50FpbMwxXb6ivvVmcc+RtB+/fua1qlGgg1TXaBVXKhHr9uvdYoVUc7qldnbNw5d3cnSvh"
        "Ys6eEjVv4bW65EBVrH6O2NCUqth4pRCoxdJYNNbdwhYDQ3ub4CcnDfAk0CLr317lkfI/x7MMr1AbuulJAisApDEQQDyBcBpcK7m5upQhI2Qonzged2jh"
        "FV2aetsiiseddcGw9U4J3IXqWVUKXlWQQgKp7egUHH1Fzi0l2eBlC5JPMSjCI+CZ1dRZPYnXa5Uk0LGvVxqeJnM43BjInE64XgIS2SVkgxUD27pugKCs"
        "6Ppf4Q7/64uUH77Bcch2TB+1srdS0yGf1mkUAmGi9e1EEtA5rGmAnNf/ukZE1qn0GluMIRw3epthBBuR4/gPqEBco2K9PJA4d+gttlVByA7fUcuwbaST"
        "gE6hU9URuIIMCeKyw71N2cXcCuopC0G/Jdj0Oim5S2LSsRy8+wzjVW9JNIU/UGDbhb7W8AUE+0IKxQmxTpxo4sTD8dQEBNj7wpZFQQQqHKkouei6N1kE"
        "7k1ECtO9REPEGkTktLp/kGTxuMeXE07yMB7Drx7LqPwGkse9ijCz6Xk47uGNhXw5BnniEISNaVSnMhdVUqecK6n2D+5FFyCRLEDoiJPs/gFe41lAnpfo"
        "w8o4FyiYXfAf4Ho+RUX7y3MTHS+pxY53OAEdr86UQHoUetvbrzFoYcFBDDZcht3h6suJhyHkCux0alnrzaIXZ8is5r81uEfHIP6aAOcEIVBwwgYOAY/R"
        "6fpCrmj3yr0tw6NK5t5eanu6UWprV2m5C20QykLd8Tojl6OcLlHLIsyiqpvtwqpMq3BkyTQ8KDuLLdRhPlmTFaKl9mttH2YOM9plnaHjg+tPEbniWmtG"
        "uqYjWt9dffNhf6PW1NhiVldVoYNisquHK6vyxWC07e4BbYajFPXSIv3cEhRlW76oKJNvN833VCDBTA0bq6YOjiMdyan9OlFvr5q03ReMW695EaeYZxmJ"
        "yjQKr/JM1MAbwMhh79k9ar3vWkC8sQn21NjVtqaDS5/kp6iN6575dFdZo67bloGV1CE1q86G5Gb9zpGcDdCWGagnLmtzSmloNtXE9EElssd5PQMmKJ8/"
        "XL0orK0zdp2C5frzoGi6K5G3C+tJl2X9Insfh+bMurIgsXvrTevWEpTZLfq2qT5XUpcFN8rqHZ3pkNe3HBrxnN6Cb1kFtr54q8gXfPCuOjTAT1eGgINY"
        "yJzJ6jVqGc0TWnjBWqf7QhnUrWUG2do+SZv8y3svvng8W1xVvTbhNldjnevbY2/uOhRKCdwMzlVDZ6uev++N6CLmsdTahMiOrbfxVp9tJ4qnpRdJ1knD"
        "C2uBlnU60gJTlln+ZJGdK92NnYbhJKVZVizv0BHDUoc7tj9Y7hcdJblzAiCDhw4EHV82rHRyyq8h/1OlzpFOELHWB3VOaKqKTNafK54CyIs1eqnDOrXl"
        "NLeg6yA5c/K63L654+sKURD7Z4jRa+WkhpqLcLxpvAUan4wOktHBaXLRXFKIq7NzDFBIVkeWiZCSZ0A672PRHhB9Ha40dK25t9d7/PQXCQjDjlnTZjyH"
        "rY17RAbbVDuGhP7Yg2nUmNED7Jp9pCq6F12SktKzRKTsqE20Lth3R6iipDdT/jOmC2QaLFkbYpw1ejL+mWfZ7P052ewpsSTt5+ytudPbh5/soGnhr+Fk"
        "kLt/ja5DNE+kfbFDiwaIP8WGhO0wgQ/1mOGd0reMqPo86AnUiEVOH+/gm39YUc5YrHxzkRdPgYkZn8F6PIOcVjqKbmhABluZE4k3/ZAtxgfDoQGzYOdH"
        "XgSQnwQ+MmyjX9L5AB6fZ+gB4CSKop1YlG+qbEmOj6z8hE6x5tNxW8K7v4dX794B+T3ocFbSoIfNGoAMKKg3n2bSF/mHfMHgiNJiBL6SeSTzc7x2xCOk"
        "IxobHY84rmfiUjTdMdKsyG344sFicYyNRSYgqW3erbJ5FftCOVtqm75j9pTn9y5G6lGXlToze1dNKX3HlX1GfGcj9uj8Tk2IwvfNoCLb+Ev8BeiOTrSO"
        "/yCUg6iflRnZa3b6v/D7UMi8QJxr+3hvLa3IANFnl0tcHVHLQx/d0nUMY7WIjIShVpU7qGe0cYwvvBsIfL3LBzp2gDxj+3YIRqmDKqDzYTwO3IaJD91B"
        "iZLV0PemR6igL+a3QcW4Sw21EMalvMf0j8irMwnUpYilZCATtvpL7ZJhT9LNXip8QuDHDCyWtacmCxbqQFgfglWdG3q+7FMVZPGxCVmwETfAUhf+ZewA"
        "35h9PX6AQ0XCsS2U9t14Gc6QOajyIoFDvIn6GLoF/h/LmC3Ch2fMU3YOt7qTYIrePtbwUXApUrQv3yeE6PoJHkE5RSuKQlSj2MceaFdFj3rE6xAfbIhz"
        "4NzPBiJfNP8ySBCWxjZ0ozeRhl7pPn7px9AGzBScyxhjnvx9cEXsWCekNL7XiPOUeNKcTkCkuqoqYoHRjRw992G0pTH/G4Gw0Fev4vvomZg8ikDkXKAd"
        "XXk4mkLLwzGksB/hFCFR9M6Gof95RfsB4XhVnpTu0AotshTly+qq0NrfJT4ogoixb5RYi06XTyr2wXYT0usocCAoCZFug01XxleJy12s2RWawLQvtzQz"
        "zY5J6y/b4u4TZmusr7t43Fp3YUT4uy4n1GnhY6tLJhF9jwxZsEAbzPpijiwW6XOKi2dUcfziRJzCWtH7VscEEISEFwWyOrQjZ5eb7AxY8b6uuix+NmMj"
        "957oOFCV74MLK+oBn6BWTcz2qZqSeQ6fNN/xKuMdp+7/ZHJzasKyZUsN2wtLJmrlBBY1hh61Fw9+AGMm3WH33fYQLtozBWksm4KooYKWlVYoszIUNU1H"
        "ZIfSn2yuoWZe47jKihq++iWQxdK+0eQnyY4kVxF1yLEhVhP8iE47nuAidlZHWvDetL4mbk7no3CCVQkmGBLRGTjCNoZNAzwULu3iu9Civ7kpDn1jZjrh"
        "wzyb8b1rUV1tpq7oLUx3jp8QTipJdyfbFtR0elcMtNN3jhURqLVe6li9NHQAKReEYwQN+YT/738T67iLlO2R3JR+xv0DOP+C/XI7JQdjKlXbjOxmCNvJ"
        "23jhY35dK/mpHODgU0qgQK22JIqU/VGCEW3CrbidTlzGdkM9kvR5pKexSY9LXySJRFVPiM40KghsSyzoEBidz93hcdfwWf5uUeIHvX+A+hkrJb9PUSAF"
        "MDKlNKHeb5ADwsAzJUy6sBp/JPHTf8pXUYlQxc4qLTWYa4mugpIfKNWSRy6BXhg2w7wC+nMozMe1FrEaTKyHq5ZtYMAGvfngXgNcIPy7f+DE7KvhTT6p"
        "4U0Obyw4Gx57iQEBqjSqcOT7o46xo3145Yy9grFjeAE56MoeNL3Qg67sQUMTh4U7bL0p0YAfu1AwB6PFJ//Onh2dRDG7QEXru8XquLSu4kkZ4pA/TcED"
        "RDFpNpgIuNXKm39lCYA4woOiZGrOob6kENsFPOfpCDWrQRBrRgMFM3ToJU2957Fd1lZpIe6/k3DYTL2UsQys482u67dquf7S5ghLS/IIgWXecbR0FOMT"
        "BooFT54plxr7qoGyZTXU0hFClS1dwQRZ79rcZyqyUio/j7OyaqTSQy1OO81HoZCqBVsH90OFAfoSPdrS5WkpGrI6Nxx+35EI0t1dEXcwJLWMWdF+4VlO"
        "dPOsHZyOx/slHe2EA/J1ASqKREEq5tr8dHCJCNDLxerhivytdXRAY4aDQIb5R5trw6Y01zY5G2TF6qv5FLEVUSbLEXv+d/lbwyvi11hjnsRdMvGzEpv7"
        "QEWDlsKVgiLSrllGgijdoVOf35TVYu4ARKrs8aTUeoPzRXmW0Vn3Kis01JcKIHRc6jd06BgHyF9hr+PfZ7hMd72ATKir3ts7sQ8/EwZ4mOiSJ8NTROkf"
        "XN/HiEAJxyLOxaIr+8hkX8WnVm9+U73Jmjv2RpfDiAtDqn61pjcm+8jODr0hZlpqvjrwZ3atxcVUHja0pLGOYpANt3Dq5/QpA1i/HoIoQWW6lU61f6B+"
        "kx+XLHBa8sPYelCAm87WcZdOgtGgdVxm/62cX9xoFnKnFfX6nnBirGCwMDdQmPGy86F82oNGMULD2fv02R6jEaVTCW7J3TMmou7RYAYkI31aPoL+V0Fy"
        "Wq7ZNjmeE674ViGmoif0m5sbtDGlqSkZYgVtItFXLbIjX+dJPz+VguBxuSTvtIgtNuU7GvRupFVpJUVlu8bQhG7SykvKrlu5IGkVx22smAdNk18uQUpG"
        "uBjksnayAij9Oyq4UxDHvYM3/XjA9FiuKYLqsQuHoyuU4DG5YNHDU7dnJrAZs44XIERdYB+dg5UrYtkt3R3FCKUzOtRKDdH3PnbssVhA7Y2ASXGkUzll"
        "k8VhKudlsrA43CuZ4XpypTLATx1UJ5lLVvcqWcSTOYzNo+NSwc9BdkkwmqMGNJq5ImmQMZ7Hp/F0ZsY7HFcqBA6NvBrUwEZEYdNGHeH6uIzquJ+7z7dx"
        "gntDBddWRE3eveg5l89o5WAh2cjUnpRrcSLepp4+/rF8fFJl52wAklylQ5hEHc/7Soe3hTVOM1OdXJ0mbxW7TSwD6kFszdxbQr5EydLM6FocZusgpu2T"
        "D9RqdsmMd2rKvPo8jDDSneCVgILB9XdC7qubm13rpMIQeDLfCvOtZL6VidZ3a0f3VN07k+H+NMSPvRYMWTqPAqXiAYMHL2tJZ9/nK1VojZFWoAF6QaWP"
        "9VuKSZE42Z9/fJ1vOLrCLM6gxiMITg2BB1MhsYPk8QEnsVGvYXSMT+jubpKI56NTud31oOW132Uyi+Ag5JEMxzQyUbi9WyFmMrQTKC4l+ZE1aDerOLxN"
        "GzZSvt7vjXv7DRpH0N9P6iuZeQ7Ziwzq5UI0EZSJdWDOfer6vtP3HOjOPgaXSnJHT9T5kfSNBaq9juFkRR2Lo0RROex9j2lXhEiMfjxYPxbkQDDKb7O9"
        "em7jWJn++l1ywGSxQoltHWKRJy5CeN6BEK4MUDbjyibynkgbTLfuDR75sLUWfrikWaEQxy5n8bKso8Zl8G0ab3gNc9sZGTZYz2KSWcfgqzyb831MArtz"
        "YtlpY2mFyGAqlCT38GBv7zrqqighYwqUCX6PykTYajI8dtPP+WJc6o+dyFOJoLpddGLVvlk0uGTYJV2uG1PPrdpD1JWWFKgDBem6GAM9+Ux/x3lC9Qmr"
        "PudyoT3dE08bXGBM4EhIlZJ1B2ix9j4+qcBlPwS2769dpaEdub7DS9riO2x6OBGOuDn3joN72wvOtJpkYW/O29hoA6j64xL1D5EVtVid9SP/rIdm5F7R"
        "8Wjb06IGagYgQ3U6KK3hPMnBN0MNTyA3TZiQqgOkTR9aEj/zD6wbwKrNdgqQRRvugoTh6Y+RwZ6jFDSesxN+oywrOwWzrByq3OAdACJVtuWILuLsUj1P"
        "tELG4qpQs5Ab8YwDMjuvSNK1RYxRAmcGToWzBLZS75vQxPq6Ql5ia5BLFQxd4XU/FunzgI1l23Ru9M8E+jgvL5H9H/eys1mPo0phJ1VIKcj4gj2CYadc"
        "1mg3h2mv4CNXNUF/z+HwmjWvcpCWs0CcqioHSaxCO9GFmKGz8DrobytSAnBkBtx7YDeyt3eG2mf6NTzMlVYSoXK0qse+G9CpdG0wsLqPRcwI+30OikFa"
        "RG06oOs3L/dHSEPsovv7ic6XWtoCWcC87I+QunitQNahSSRFgt3J7TqFpbbpQZy0YTgHZh0wtL9+TO0HxRZpQwBEi7OZE3VbRHuNjvs4EK6p5QiGH5y0"
        "CVqzwLeCBuONl4REFh2wgZo2bQuZK1lcQZfBEkcNkh0hLCsehH1Ovyhwp/JBTSpLBr2SZ6Gb0wrQw1n/QnQe8Z+KztP61k5Wd2/ScewmpWuyI5wORuvB"
        "SHUmcIKkovDFSTuuDcL8iF1MeD9XY7WXp73/c3CN8RkTxdKi1kutPazsmg6CFdL65JPJg4v6SVlhi/FtwEhxNyiUanHUspr3ZFEWIPv6rNGGa5e8S1YY"
        "RSw/6fVXvVP8+acJw5EsvAAkWpbjFeUtVHnKT90jm9cScYfA4AzhkIvkg67LC1rTVuE7YWrkd8CLE1LaWR4h8P16aI5Z8P6Hr4logZHgxuwd7ZkhYz9U"
        "WCsyHU/cb/KlJjsaH8i1WbPJYh+ExHi/9Z4pMvdDrx9PUDXmOCDOIO/wX65HriF5UmkSsuk0uTBoaOJcLoUxhbexRlB2AiHb2JZBBcqPavavYBuq6GH4"
        "e1ZeLlGwJHKCX/SnxBdDldXFcctmqEsMpTFqRldxufltHMd3lIBJ46w5NrQDai3I5Kc4eW7jUFgNeIira0QZ10ox2hU3N6Jj9f+ktEU7brO6crtVqt2J"
        "Av9HO6bvYxnTF6fgR5E+NoyZDJ395rJ+mYHEAhxanVcfxCwf994cve4B3/TnVV43GHe0OUJwQwZCJwJJXh34C2/wMshD8WPu/7HMz+EQxqu9JRtTAP+F"
        "m4UAXEYD+I/QdSVHCFSQvSquEGgT44dvzZ1NZHTZlEM44wt/PKyjpOvKPDa3lrJ1WJTFiThNc22W1qTbcHzTg/FICe4uw15IBUU5uL4HMrXSS5SD1T2p"
        "TfioupYW3YFJYFL8o7ZGhaJ9sSglj8v6F55bhgh8ArtQykC6pYGcfaVeGQ2+Okz94tMeVAonXw3/TtzyJ/lpqvsFu3WeJ4/Xg+lsOHC7NXTCNAS/30ZC"
        "303lFHvZfkZxJkGrLeAuCRMEqDY8R8HhqSs5nNk0XYjpCRtpXSMcHwL6nWJoKzbdEpx6Gg/+KOHQ7yUEFvvYw6KRw3PGrSi02P/Z+waJ8M3n5WqPPVtX"
        "/WLa23v48MWvKXyTvbOz8jrtxfs5Y2bxVg3ysgu/3Sbu4FiBZsCQYJenP4rkD4FF0mCVSEF+lBSEVtCboAfWUiJxD0abPZWSC8elh4lKaj/c3Hy+7fRn"
        "apsU+x5OayLYeDDAXxB8560T0oTkD4nr7wYPgeO9Tjp9ohT2CKJCbygu0+dAiatyZY1zs69Jy5XEdiMpWSpzfUi4dcpmO5ZgzpZjyUanjpKgT7VPh/Uc"
        "oHrcsmU84tpEMQ/olu0suq2VlrmZDhujuFrdlilKbjm3xL5xrLwttyxTBt+4nOJSwZvaBh/6Np4XH09BjrU7Vi59wffD5bbWLjga18rF2yJARcngxU8O"
        "hhYNKIvcbdHxTdQntabN3vex/YDMoQl/bRbhJlvSjprUxFp4Q7jHvrA2y7ijHaUo4Gkkv3WShxaJjY21zvc5d1ZAY1kvOVGiWWEoEsH2F3ah0f7BPauc"
        "9qqkz5Ku2zds097eQuhP/USkbwLqvqZc5BVeO4+Ha0nVm61x3ga2A50mMni//7IC7hgJHNMZO6lNL5Zl3SxBTvnZLIMU7zJaB86bNYyOpst0qt6u8YTx"
        "gcXZ+6VLJzTLig8Z2le8xhsyfWllNFtHmIS2SRraoZEY6DvzswX/oGLz8mPBv66WOw7ovPYowMx2NQq8RmW5IPgXavHFVaPOI1Sw03H6di5qDDD0NqdP"
        "Zq4yZs01Ays/4maj3sGc4rD4p5jj3ad9OXFOX7Hw4QmFUHHIa/h9O8ktZpxkuza/UXMHlkeQNnA/H8pNZ9yRN9GMiV08sqIBdSCQTzZZmmkLAme9ykqd"
        "FZs6waVbeIOE1W3IUpE6Ms/veOBxyGZ1FVvcE8Q2K3z4e4L46mA85zwczzkxmnT9ifmuJzpIDmKzoAYkVS5wCvt8G3KdyB8r5zJMhVkKnlxvPGEeXrf3"
        "dGAF6LuyIPEYtZm3SN9942Mnw/M4qy8eVFVG95MggDqrBhjlGPV6sFerOdCMz5Q+bhKggR/Ghv48y2qQN2GfkTB9O3Hf6K6rhAFmTRubgmGydlnChCcY"
        "2ir1npWhsSrBl+Ed45Pag1e8zBujnGnlV4gjPEqyW8Qekt0hjnQipoJ+pLk36DRP8HKCxiPGfu+FJgKqatdZxJrkpLPP5pMGxshn3iuLEDAAh+E28MFj"
        "jTobIaiMrVbKppp0Xj82XlsZb3AyVRnPb7idQV7C3D9Jdk7373eHlcYGc+3OeFKiCF8/z56jrRIf6RMZeRrDpel23uqG0oJCTe8E3wU6RkvMmpLAXNqg"
        "AR1fr30IpYG0m5tr57wyKErtakOARcbAlC6aIjOcjzmHJxjG2m/GOWraSTc3xPMFMiuuzGqOKKZtfIr2pdsWza6VWSqW4j3tQzWEJtA6Hu02LPwGq2Hs"
        "4Ft9ux14D714y1fc+nzAKLuRy5MlHcc0+dNA9sBxG5jbST5VFsbyWDQnElXzihRS8kDiv6uE7AXQ9E9BOtGpln1wy+Nli5aQRskQ/qN/w23gax8fgM7Y"
        "VuqFjPpiaoGDrimrnA+lILZGYiB6neG3ui+ku2RoQs7yc/Q3ay4it3G04ZMzJC0kE6lvc4eKkGX2R2QDVG1qWvgH0gQoCAjGSP9jWGQMB5WQgt3ZYPYa"
        "piC+NRqHocMzvTM0XnNnuvVRaBYtzNrFylMZaGW/rEIqEVQkGTznllnV1EmmbXATHTOnucZbiYxJ8IUznwIIrDjMJgIpKxlfp0OguhW6aEuiWxyWkwJe"
        "15R6UpwmFyfFtIfx6I/L3rjHkF+906iGya/RXiXf27uQEPLUyq00YhKLxWtg1N/n0UUifVBlTE9RzUJ3Kxwqx508vPgEKnu5RKd7jmGJ/BN8AKPqBX6+"
        "MMYCdlhIWIfZXFwBUzaC/+2mUZlGazP+hjmBFMb3C7brUgtWspMjsnAT7qQOsmqm3HbvlzCGYXJwj2p/+TTZ5ZbLvT1hvn3SmiKRsKGsSQpqkXLtpScG"
        "mJVUZnzh+2CxvMBYMJisDcUGXB/wBPLNo3JRVnh1NMMfMkPE715dIaZLDwWxck6aTRg29WVvD829hTxWqFEERIM1gVyCROZTj5HuJF7Qtk/dm5uTU+Rp"
        "3G6Xusu41N5Ivp8bJCtN7Icaieo8Zn2ULRFrhH/JtH+X5NatfuryTLuk0NoFc+Jo4Wz4ORdCrmwRknJSpiUTElinJVOSOBwiPNe0tuYaab1FMhAZQ7wB"
        "dwWfQwf804mxEx2K0AdRgkZ5Hn1l8KY0zbVhJwo0NIBodxfoLjnRxdI5SWoBAryFMTdQDHsQ6dB+6QqWljH6+nlsaQl+RHt0VjZCN131Qedd0AUWyudU"
        "9eTuIditWTrJ0cvQqDF0D60WbF7kUvf5+AKkwmZBwWK8rtP7NpHf7azCcpVEorh2rYkNa034aw1XEJB+EU8KfT9vD0+zVO7kkyEyvDv64qktrKmF5jrm"
        "Nm6XVNFg3F6etBNPTZyD4IcZJo51StsvL/g1FQTqVwekatH9at3S2+atkOnxiyPuf44etjntXt59YXg5+yTUwuyErFkki0KODyjHJrjIIxZu4UuCdEsS"
        "bD7OHVmdpVhU/nNOT+APSvVFLP+aRa6F5aJDbIytQbl4dluMKVKDIhqoRhJP1ejGoqV/gCGrIXG8damQcAfuKyNkgZaOYs2obMuIn4VlTXw2YDXrFOWm"
        "J/iGNSc42F9Eaq+satWCHSuyy7zGqIsU2CXqLT5cYnxLyDOuZxf5ZVb3LwXawJXvmv4MXerhfZwErJq7lMCHWCW6i+z/fYfwj1Ju5Lu/w6BmWTNDpcVn"
        "FZ39LhVzndeXiwLqDPZ4oHrcbhmYn09N+nlr0Kc2Sg9UZcUyDV+frDmVNio4w1rAoBZOa51Md38RUa++yJbojHKEiMl2t+nFRnSqXBo0oQVwb7Qz6pE2"
        "CNqmyvEHOvS498ycwb05I27JHLGuQrBD79Ye0cSXDN0Y9UkTPnTIc/epSTkm0GEbIGabto+5BS4Qqq1bHbdGJaYbYq6QNFTEeROFksPBQ9fuSil5SDgP"
        "dgv5OymJcZZpXIN8hYFbCBJRtcIrg37iRy79ecwlswukkH8olTcxu5DKTG+ucTcLzTMXRn0FNBN/M6M8j6w38dR6kIYpOz0gWlZqlS8XQJOi+9HOvWTn"
        "Xnz/PKE8dqUE8gsdnwHfXSi+W5ekOL6wWd8tsoZWKTZESnuZGfnxmA6pUn5WayWpqeKwDzyjUzgdBE8kPtI04o/2JKJboJ4tS9hRUyesqbOkpJiOF7c3"
        "gnpD7VFfNsquWizVGF4iDUuiRTD9N5DJ7KAtKFqi3KNE32nvaLgzBPn7wTOgH+S+luDfFQWwFvRUwG8QPb/65l8H/xgOexInw9tmZALH1GLwIc3XsSKI"
        "a2vfh6054c/9rLfJnyI9GwDRm/4ixrMm+cG54N2e/P+Ju+bD+cZwVDT5edXna0SYjqIsDA9alWXzA0z5kuoL1da22tFlNl06/pXLRDdZN+kmw/hRTbb+"
        "ku8/d8WHmmTnli8O2Bl4qJiqjzpqED8OgKHKFjVLpc6bNG9F+iZFJFKX63YUcFZH9kjdhy6GQl0j+hnxev9hCYTzRF3wNa628NTQvi887Oko/lMfxZOm"
        "fZLv7R3RTWfrRed56fAKITHqy890b20pmT0M6q2zdfAX4WNdve8+3I/tOrrOc51l86neHmXHjdk2fAA2ijfbWruWs45LneS+VTef4hhtCw8WPOZCGfry"
        "uCFvYhX/sSOrXPuNPPs78+EROuMg8fLoXZsVl3rPUorhzNi8QrAgZsgwQ8/OjVyAxMJbU0I1wO4L61pgfMqek39TG7IM+ZaFP4km+5J38PMR5yDfSvag"
        "8ytiHvsb2jxDOHN1tchVzpY2dRzui+zx2vsAny+Yy62CVwA2OM5G7kRxJWsV5UXayxRPsZW6XGD8yuHOKBnu9Ai/qsW1HEFNQH/7mFPyLcX+wT1sA8rQ"
        "7758aDFB+WZGxp3XeY/LbGBsFMFaz9NwLlsL8JOrBYDz7OaGOR3UBPxgNAGUuLf3g9Ax5T41TpByIHmvgMTmVSs6TmQRI9gRnEcRbmlqrIpa174YC0Lp"
        "ZLtKq2dzIqsUbTqP4r6uPXZjpJpwcaohOBBUSiIdluzuBZTLXF+PcUdWFLZi1/Kc5N8GoEkrkpdWtQTEyhdCoV5zTODGRPT2iqJKjGERnXIbgxQsyXvw"
        "EWl+9vZIIXRzQytCOS7/JtJrw+h2uqFc20yYyWV7A1gcmIpMQ9YF0h11A0SLjlijkFnaNdHqd1MD1Z3AYqSQOMQFllfNxRtghumQRRvhsgokfJ+ZBCrC"
        "Cac4ST8IqVECHgrEggERJugB7u903iQf8V64vMybaiXvCtIzgakz6evBXU2fBRLr9JhSTRxYfJseWYm1Tv0kuK3mCfTmqsrTR5SQ1er5dwG79TIH1uQF"
        "f/7o81nJXue7Q/m536LAvvZrW9qodmBQR8dgrVXmSoq8Hli7xAZ1fN2QtzApstMhoZMAD+t4MEuZRXkQwD79sSzfOxaLr9uqfG336ZmLPoYkVRWf0a3a"
        "3n9ZbVjXvDsACr41ElhI8CKy5/KMmKRs92iqQtYlgWk0TZLan608HrPH8PxVsDaDEteqEegTVaAuHDqyxd0f1rqpwynzbbF2gUm9EO+an/LVzQ0GZAIG"
        "8kLMLvb2+AG1MWVhUBYsS5f2oKJ2PxCcDv95Zl4SEgvdI20ZpQvtgpUuOflsGRGPX+WJNk4et22Tpd2x++bnZfI+X+GK0ukweJybWx1Rr+tq0/q6+vCT"
        "33qoye11W+NMHj9nKFS2pPo1d2HkwX6RCXPNhTPFwiZUhpUyqk1sbEbvMKvS3VBCe5jyrc/EKKdagp/8rs+DayVE64QOkCTvtQeWRDdihagvAhp4nF0C"
        "idJ1YKznbeYsTjJcdMf4z/v/mQX0sxvVrnN/6cXE02AcheTo77rr1O4PYN+YTPJKcmihkq1xAOGwdq2VsWURWlixu4wbbe7ormmKW6jOScmjNNKzWk+1"
        "M68H35JpA3yVR+XcXF96k3kXokUuJY8ajC5N6grR4FkV9fAI5Gi0sLtlF3vJW+LK3XN+Xl6dLXKyE9HnfWIO+tBZagE0wFGs/CjMuffYVLnhHLVqQue+"
        "rapyUzdCwxqI18IYFZGX3ON80WRstGtHV9TnzFT0i7HYLyZWMHpTgzdvrACQYT7G+uGBkiPdVcee6HHy+4Yv5zXSg08d+ILSVgUD82ELwJhZPx/nsxxd"
        "iyhY/FdfD/WLo+z69TIHfgTDGeVZnaNZU1ZhzKDBQfIRMZQflcvVv68uKfoeSMa8xn8R9QxDGq7Gm1cKByLTl9BsvoNExPpI3lva4r+SRApvmRkMOJbq"
        "/LgGaQpot2tK9xhSXmNKgu+cZEqxHTYxER02E89VyTSAtkmYYBbmyyrHYhR0T7v6aKNke/LsG/0takQ4RVMhZNSkxvMv1Zk+XuQFoR1EwRyxObtJZR44"
        "jM6r7EwHgWnKq9lFn/rWnou8wD+R5ftBsS1rNGpXvuqXOT527vlPW3SmZ87OrmxruyldraKNPLcuotT5pg7Fj7MF2JfWQBH67PXYbUiNuM45nlrl0vHA"
        "taBWtIlu90u9PacszXbn1NFCSNdIKzl9G9nhYpVo6QWjdYVhx223P7J7vq4GS1j2ayBDkYDrpz6XP6gx2hHDLGyubaYnjuNxe/wM2CPPeVwAknGVKZrO"
        "0I2vud4ggmp7PXXtilgvhpDd0KTdcVN3rr3+a2KWnDBs5tXLsk5bCzE7qyHd8khSL6AKeOHvZvYzsaAtofPK98TGX1ShbQlQt8X6S09Me+56bBPmFm6Z"
        "p05Gh36H2GZ7b++b4WHTt3p1MjydxH5mOsAjt/cyrcs32otNEERw94Mp89q2bDxPhsnw1IYZFRnHImCdP2OzSffz2GAkYg42A14fysC6nBxcw0BoF2A0"
        "dVi4HXBt/ajp5/E9b9fQJNgHWIAmTbwyiuZYm8VC4mqtKN/R3sogufLaTKdVJ/qCHEr/EEaTTq2OyMES4qTMRJE0V7LIioGlw0VWyt+Eilx/x3hhm1sh"
        "9GpqRRbZ3AoDWScdc5N2TgkTPilAWPxA10o1S0eFV5LsJodX6lqISdH52a4xAFPRz/dF/F/Nft5HU8ao2OfnPj5bYFwlpBpQ+QKepuW4mHRQn842Z4uy"
        "yKPO6cKPo6mms21ddwodZhKZyirVmuwkS3crRUfxEqwon+oHi0Io7N9JbREsZrziZNei7i7l01TYeAxLMuzvAU2gCJ8nM+6rTM89shbfH+VfwWetBg5v"
        "TuVAZLFOzPx+hvhLBrae6ZC+AYPTUY9fMf1JRt21qxFQDXxtcd/ktmWHezDAqHSL9AtYaBofARHfS1j4U8iG48IdwQsOo0qF+I9r2yyZLnWg0jL5PL+S"
        "8krhCSZ5UpSo8GBOCoQciU5DUisIVOPaOnvw27GY9XqTmCUlqB4IZG35CsT1szKr5tie+g1yCYmP43+6QpAU7evx50X+rhmffPXtaVKhDgl+/us0IWXM"
        "ydfD0+RqCSn/PCXwmKeQNvrnt8lo+G3yj1Ey+nbE6egGAC/+BS/+lXzzNbz46rQTMaiteqebRO6mdXPmj8CLdORn14Jyh17dPzhtc8pBk51RmMjDdEg0"
        "Uz2nvWGPQRTgvJ9d1VoSe4JPydniqtJJD+Eh0cr1cUu37kptUinhVKu6bddrySa3W+korJeRsXryhJP/ltG8e/dlwwlr09t2VFQ36Ykt7NWzEvHOzbP6"
        "IW2jCUysnlXlYnFcop2NeSB8MX7C2C3mHT4loWkbUA9gVj+KAmZFV0VoljwQmsn2R5FdN0ptw3zSqx6XxklfU3jUVlpDAWNVKHd60InK3Nf+lK/q9POt"
        "OnYVIUjY8RBdvd81vuthfsLpJ8XpaXrSH91rgHBPjLuiGBDtCBWjF1yuVQiXVqgMpnMRmC+3yNUyVOBqqbJj305NOLLNM4J0Y9OUMO0LNcxvqPHG6ack"
        "i11l4BUV6ksLscA1oNav96Ry3ChWpMpWb6DOy7871BFWBFOAogbheOk+qxnMmmohf17mTQY/Y9ebRE6eE/sXY8kahBW5BuOSfiJ4GIa9VL+RD3xZledV"
        "Xte01Z1C5K+kFZ/oD5qiHtM+678iK+SALoK8spzTniIdB/I6NTg6sim5EZrAIV4IYSeYsA5dJcOiUK0wDiAYQC7GpWQj4AWFMNpxZ0mtS5gmpa+1Ygrv"
        "R5b696vxSIlOqhRGQeDASFDrwbcIwHtzs4uzXC7R3lD/tOwhyxqBf+tZtszxs0osh1I6EmNmEEtfyei1MJjvNzAr6gDvAVvTZlaYeL65yPOFVOcnH/Hh"
        "cY72DzO6cRl/LRNfXr/MK8zGIY7/sVmh+7pDK0fVmV1A7b+mrni4fEA30uHGe/W7Vk8bzUoN3Ar86KBm2iqW1vyQXbDp736aJ4ZVpzPViDOb74Stuyis"
        "3JgHq5TUEiG0Ekv0jWTR9wrgXVg8Cdy3o+xQ2XqPKnSztswrxFDQYbfQziZ5JSMRWy83xW60g+e1ZxW3zOsCvWqHKEM2nmKTZvZ+9PW9ju/hLs0YJZOv"
        "2dV8UZ5HB/ej0T495dfLyERrL4AaxPfp8dnzA2RFpiaYYXFfxPcEyBTknUcEi+Pl7UfDQ6tXUzHuA/XoKx2IXLT+h5TueEAArfshwzh7G3HaaHqT7xcY"
        "AMO7IWotsATzsfzyfgNJ8NrqAQ1pU4YmW/5YLubjswGpzjkQLSL11tk7kK7w12V5Jhaw2LPlsYajG33zxUSB2pHxbMyF3tbGNXetTj4GzPACW+UCpkIb"
        "pYzY4xzrz42ukSAYZCKFwbIvldX+lyodjouSIyoHrIVfE/XrN+P4q9tL1zvmznDaF3qjiPo4W1IMN4KFsBkY7htqKF44pif6xQ794Aq1SQc90ZXiy4p8"
        "SDSJEnDco//jB8bWs0HwiBFnYvGP4XBzayEIPX6jEfS8CnzcPgXZR6aLrT7rj7zTeByZMynvN0+Kqd9eg5tWzPu/Ov73245fR1f1Y7dZS3MSWIuNXouN"
        "XosUD1Atp84bLal2sxRK7sqPDwPk3qYZKCfYa8m3ZaYeHulDE9UAZ1dnZ4iRDuwKTxNqAPEJvVzGLBgmQObyvPgVb9f5l0z5Taf8lshBj81WlIPXKb/d"
        "oiOL7iFJjzn0H/0ycNBL9BiWHWPy+2ID+ZWUtQd0OkB28SvRcSoJb8KMxgOi/qQ2rrcxujja6j4UT11WrWxNQo8xja/K19PlT3fpwfu/1AMnscvDXstB"
        "u3ov3Nwc7LZJOTpG+t7Rhq/nOJ7otbmOj7N2G8dk3Crz6NRFjWVrwsK6yHlr3y9Z+0ya/hbrzZWsWuNEsSEolZh9qZaf0V+LQk7spjZyKWE5nYydbj4W"
        "CLpg0wrhvKfwRoXFKrp2p4nzGXAnFppP3HBC0PpwkF23O/9kSWNA8ULfuOgqfVldf9C9vYMgq+AMI2yHtPXiKrbNjIuL1D1m8ov4vvdxJhphi0CJGZoN"
        "EZ5Y9PW/VZzs5k7YVJdQ6UWElR1SZUccsAqZk/JwZG+s7/g9B4jB96PDMnZqwMjdhg+3gK7NYnZsrvRitoDDHDhma+sgU1dKafuWRXaYK1QKYdR7e0VD"
        "Mfeixt5VOH9Y094eOmgIvC/kH1qSd7phB2LNPXxyf+PZyN7mrkjYINIKYkxZ8kqrFKokgt2yO/J2lBVsEQmewgWeFGmmCid50oEvnnymLuKxS0IJhRdh"
        "5xR1q21Vml5HzHon2OoLOiwTa3t12sg6m2UaeSRgFBzAJn7Jpwbvv5QahAVZBu7H2B8t3+PcWrvOjFKe4OpOdodJt8AcW6F02daTY3LfpQVt22KmlTiZ"
        "X8UGTkZtMOCeiZd5yKaj6dsGHh671ojpI0qssvP0d/z1k1RMpa/x6bUrk6bfY+Ixc0rpe3pQjaUv8N5fgk6+w59V+bFGbxj4/ejV67TE9+yKlD5Bh2h2"
        "/0s/mN9HWfUeSpxTCkGx5FQIiCf0I30Ivx+LD0/hpEtfCX54wf4m6QN6Li+J3qaXDT/93IhFuqQHbZr4K9oiUjbYa2SsIB1o2ON+hhX9kJf/fv3iefqR"
        "HiohsZ6e46OcaEThGVBX5pj6FIMBqc58jynyYP6gf8q5qSkBqyvVL275ikrBIUG9/oA9O8qW6QP6SxNziTmOxLUoUryGGZAP0jtMZJ5kSb8WK6rgvJFP"
        "59DHayEf0LU2XfHTEhp9yD9rvTnSJzg/L5no4fNHrAfhOLPiHKbvN0GP0gfvDT69/uWH9Af5Q03BC3zGmDE81Mf0WJaoiU6f0oOCAeVWM2yF+t3gr1+A"
        "rpeqrveY/wzEhzTDHzyNb1Ek4tX0M76f8WryIa6soC4fhEy81dnlvHbGgTmXcWAStv3FRfgzTs9crsKANxsWe0UedpCPZYF0gU629iLrbHBmGjyXa/C1"
        "kA81NHiEk3OuF2RH+89V+2JNJ+c6k71018zf9/b8seVU+lH/lKv7nBL0mu4c6JUZKF53d+Z7YGVb/60uTY3smXJJv3j9d5a6tkvJ7dGZeWVnXq4b3kOT"
        "s9JbpzP3byY3nBRS6kxnBASZXS7TC/z14Tz9SfCPLT7WC/tjNXof/oE1NHIfdvbnqelP425TQreU8bXS3mjwr8HX6EH+wd6ua3r13vRK3hM/Qzm+QP74"
        "HRxJTdrWZ6iM6a9Cxh7VKRgA+3/dv/+3nbq8qmawm5fA+5z//OpZKsXZwR94c7X8v6YsHmo="
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

    catalog.load()   # Item-Katalog (shop_items.json bzw. shop_items aus config.json)

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
    catalog.load()


    async def _serve():
        # start_dashboard bindet das Dashboard an die Bot-Instanz, startet den
        # Web-Server (dieselbe Funktion wie im Vollbetrieb) und loggt den
        # klickbaren Link http://127.0.0.1:<port>.
        await start_dashboard(bot)
        print("  (ohne Discord-Verbindung · Strg+C zum Beenden)\n")
        while True:
            await asyncio.sleep(3600)

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
