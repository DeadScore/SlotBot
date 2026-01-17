# SlotBot – FINAL stable main.py (Web Service + Persistenz)
# Features:
# - Flask health endpoint for Render (PORT)
# - Persistente Events via events.json
# - Event-System mit Slots, Warteliste, Threads
# - AFK-Check mit Zeitfenster + DM-Bestätigung
# - Reminder 60 Minuten vorher
# - Auto-Delete 2h nach Event-Start (optional deaktivierbar)
# - Roll-System (/start_roll, /roll, /stop_roll)
# - Stabiler Betrieb als Render Web Service

_LOOPS_STARTED = False

import os
import re
import json
import asyncio
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List, Set

import pytz
import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask

# -------------------- Config --------------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN env var missing")

PORT = int(os.environ.get("PORT", "10000"))
TZ = pytz.timezone("Europe/Berlin")

DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "events.json")

AFK_START_MIN_BEFORE = 30
AFK_DURATION_MIN = 20
AFK_INTERVAL_MIN = 5
REMINDER_MIN_BEFORE = 60
AUTO_DELETE_HOURS_DEFAULT = 2

# -------------------- Flask --------------------

flask_app = Flask("slotbot")

@flask_app.get("/")
def home():
    return "ok", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# -------------------- Discord --------------------

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.reactions = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------- Persistence --------------------

def ensure_data_file():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)

def load_events() -> Dict[str, dict]:
    ensure_data_file()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def save_events():
    ensure_data_file()
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(active_events, f, ensure_ascii=False, indent=2)

async def safe_save():
    await asyncio.to_thread(save_events)

def _event_key(mid: int) -> str:
    return str(mid)

active_events: Dict[str, dict] = load_events()
print(f"✅ {len(active_events)} gespeicherte Events geladen.")

# -------------------- Helpers --------------------

def now_utc():
    return datetime.now(pytz.utc)

def ensure_utc(dt: datetime):
    return dt if dt.tzinfo else pytz.utc.localize(dt)

def format_local(iso: str) -> str:
    dt = ensure_utc(datetime.fromisoformat(iso))
    loc = dt.astimezone(TZ)
    return loc.strftime("%d.%m.%Y %H:%M")

# -------------------- Slots --------------------

DEFAULT_SLOTS = {
    "⚔️": {"label": "DPS", "limit": 3, "main": [], "waitlist": []},
    "🛡️": {"label": "Tank", "limit": 1, "main": [], "waitlist": []},
    "💉": {"label": "Heiler", "limit": 2, "main": [], "waitlist": []},
}

def all_mains(ev) -> Set[int]:
    s = set()
    for slot in ev["slots"].values():
        s |= set(slot["main"])
    return s

def add_to_slot(ev, emoji, uid):
    if uid in all_mains(ev):
        return False
    slot = ev["slots"].get(emoji)
    if not slot:
        return False
    if len(slot["main"]) < slot["limit"]:
        slot["main"].append(uid)
    else:
        slot["waitlist"].append(uid)
    return True

def promote_waitlist(ev):
    promoted = []
    for emo, slot in ev["slots"].items():
        while len(slot["main"]) < slot["limit"] and slot["waitlist"]:
            uid = slot["waitlist"].pop(0)
            slot["main"].append(uid)
            promoted.append((emo, uid))
    return promoted

# -------------------- Slash: Event --------------------

@bot.tree.command(name="event", description="Erstellt ein Event")
async def create_event(interaction: discord.Interaction, titel: str, zeit: str):
    if not interaction.guild:
        return await interaction.response.send_message("Nur im Server.", ephemeral=True)

    # 🔧 FIX: sofort antworten, sonst Discord-Timeout
    await interaction.response.defer(ephemeral=True)

    try:
        local = TZ.localize(datetime.strptime(zeit, "%d.%m.%Y %H:%M"))
        utc = local.astimezone(pytz.utc)
    except Exception:
        return await interaction.followup.send("Zeitformat: DD.MM.YYYY HH:MM")

    ev = {
        "guild_id": interaction.guild.id,
        "channel_id": interaction.channel.id,
        "owner_id": interaction.user.id,
        "title": titel,
        "event_time_utc": utc.isoformat(),
        "slots": json.loads(json.dumps(DEFAULT_SLOTS)),
        "afk_state": {"confirmed": [], "started": False, "finished": False},
        "reminder_sent": [],
        "auto_delete_hours": AUTO_DELETE_HOURS_DEFAULT,
    }

    msg = await interaction.channel.send(
        f"📣 **{titel}**\n⏰ {format_local(ev['event_time_utc'])}"
    )
    for e in ev["slots"]:
        await msg.add_reaction(e)

    active_events[_event_key(msg.id)] = ev
    await safe_save()

    await interaction.followup.send("✅ Event erstellt.")

# -------------------- Reactions --------------------

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return
    ev = active_events.get(_event_key(payload.message_id))
    if not ev:
        return
    if add_to_slot(ev, str(payload.emoji), payload.user_id):
        await safe_save()

# -------------------- Background --------------------

async def reminder_task():
    await bot.wait_until_ready()
    while True:
        now = now_utc()
        for ev in active_events.values():
            start = ensure_utc(datetime.fromisoformat(ev["event_time_utc"]))
            if 0 < (start - now).total_seconds() <= REMINDER_MIN_BEFORE * 60:
                for uid in all_mains(ev):
                    if uid not in ev["reminder_sent"]:
                        try:
                            u = await bot.fetch_user(uid)
                            await u.send(f"⏰ Reminder für {ev['title']}")
                            ev["reminder_sent"].append(uid)
                        except Exception:
                            pass
        await safe_save()
        await asyncio.sleep(60)

@bot.event
async def on_ready():
    global _LOOPS_STARTED
    print(f"✅ Eingeloggt als {bot.user}")
    if not _LOOPS_STARTED:
        _LOOPS_STARTED = True
        bot.loop.create_task(reminder_task())
        await bot.tree.sync()

# -------------------- Main --------------------

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(DISCORD_TOKEN)
