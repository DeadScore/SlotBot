
# SlotBot – rebuilt stable main.py
# Features:
# - Flask health endpoint for Render (PORT)
# - Slash commands (app_commands) with global sync
# - /event create (main command): creates an event post + thread, slots via reactions
# - Optional "Treffpunkt" like Gruppenlead (extra field only)
# - /event_edit: select event by name (autocomplete) for creator/admins; updates post with strike-through changes
# - /event_afk on|off: toggle AFK check per event
# - Reminder: 60 minutes before start (DM once per user)
# - AFK check: starts 30 min before start, lasts 20 min, prompts every 5 min in thread; ✅ confirms; no confirm => auto-release slot, promote waitlist
# - /event_reset_notifications: clears sent reminder flags for an event
# - No points system

import os
import re
import json
import time
import asyncio
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

import pytz
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
from flask import Response
from flask import Response

def _dt_ics(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")

def _escape_ics(text: str) -> str:
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )

@flask_app.get("/ics/<event_id>.ics")
def ics_event(event_id: str):
    ev = active_events.get(str(event_id))
    if not ev:
        return "not found", 404

    start = _ensure_utc(datetime.fromisoformat(ev["event_time_utc"]))
    end = start + timedelta(hours=2)

    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//SlotBot//DE\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:slotbot-{event_id}@slotbot\r\n"
        f"DTSTART:{_dt_ics(start)}\r\n"
        f"DTEND:{_dt_ics(end)}\r\n"
        f"SUMMARY:{_escape_ics(ev.get('title','Event'))}\r\n"
        f"DESCRIPTION:{_escape_ics(ev.get('zweck',''))}\r\n"
        f"LOCATION:{_escape_ics(ev.get('ort',''))}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    return Response(ics, mimetype="text/calendar")
import urllib.parse

def build_apple_calendar_link(event_message_id: int) -> Optional[str]:
    if not PUBLIC_BASE_URL:
        return None
    return f"{PUBLIC_BASE_URL}/ics/{event_message_id}.ics"

def build_google_calendar_link(ev: dict) -> str:
    start = _ensure_utc(datetime.fromisoformat(ev["event_time_utc"]))
    end = start + timedelta(hours=2)

    dates = f"{start.strftime('%Y%m%dT%H%M%SZ')}/{end.strftime('%Y%m%dT%H%M%SZ')}"

    params = {
        "action": "TEMPLATE",
        "text": ev.get("title", "Event"),
        "dates": dates,
        "details": ev.get("zweck", ""),
        "location": ev.get("ort", ""),
    }

    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)

# -------------------- Config --------------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN env var missing")

PORT = int(os.environ.get("PORT", "10000"))
TZ = pytz.timezone("Europe/Berlin")

DATA_FILE = os.environ.get("EVENTS_FILE", "events.json")

AFK_START_MIN_BEFORE = 30
AFK_DURATION_MIN = 20
AFK_INTERVAL_MIN = 5

REMINDER_MIN_BEFORE = 60

AUTO_DELETE_HOURS_DEFAULT = 2  # Event wird standardmäßig 2h nach Start gelöscht
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

# -------------------- Flask (Render healthcheck) --------------------

flask_app = Flask("slotbot")

@flask_app.get("/")
def home():
    return "ok", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# -------------------- Discord bot --------------------

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.reactions = True
intents.messages = True
intents.message_content = False  # not needed for slash + reactions

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------- Helpers / Persistence --------------------

def _now_utc() -> datetime:
    return datetime.now(pytz.utc)

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return pytz.utc.localize(dt)
    return dt.astimezone(pytz.utc)

def format_dt_local(dt_utc) -> str:
    """Formatiert UTC-Zeit als lokale Zeit (Europe/Berlin) inkl. deutschem Wochentag (fett)."""
    if dt_utc is None:
        return "—"

    if isinstance(dt_utc, str):
        try:
            dt_utc = datetime.fromisoformat(dt_utc)
        except Exception:
            return str(dt_utc)

    dt_utc = _ensure_utc(dt_utc)
    dt_local = dt_utc.astimezone(TZ)

    # garantiert deutsch, unabhängig von System-Locale
    wd = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][dt_local.weekday()]

    # Beispiel: **Mi**, 23.12.2026 20:00
    return f"**{wd}**, {dt_local.strftime('%d.%m.%Y %H:%M')}"

# --- REST OF FILE UNCHANGED ---
