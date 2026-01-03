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

import logging
discord.utils.setup_logging(level=logging.INFO)

# ---- Logging (helps diagnose "Die Anwendung reagiert nicht") ----
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s')
logging.getLogger('discord').setLevel(logging.INFO)

from discord import app_commands
from discord.ext import commands
from flask import Flask

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


@bot.tree.command(name="ping", description="Quick health check (antwortet sofort)")
async def ping_cmd(interaction: discord.Interaction):
    try:
        # respond fast; no heavy work
        await interaction.response.send_message("🏓 Pong! Bot ist online.", ephemeral=True)
    except discord.InteractionResponded:
        await interaction.followup.send("🏓 Pong! Bot ist online.", ephemeral=True)



@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Always log the real error to console so you get a traceback in your host logs
    import traceback
    traceback.print_exception(type(error), error, error.__traceback__)

    msg = "❌ Unerwarteter Fehler im Command. Schau in die Logs (Stacktrace)."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

# -------------------- Helpers / Persistence --------------------

def _now_utc() -> datetime:
    return datetime.now(pytz.utc)

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return pytz.utc.localize(dt)
    return dt.astimezone(pytz.utc)

def _parse_time_hhmm(s: str) -> Optional[Tuple[int, int]]:
    s = s.strip()
    m = re.match(r"^(\d{1,2})[:.](\d{2})$", s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm

GER_MONTHS = {
    "jan": 1, "januar": 1,
    "feb": 2, "februar": 2,
    "mär": 3, "maerz": 3, "märz": 3,
    "apr": 4, "april": 4,
    "mai": 5,
    "jun": 6, "juni": 6,
    "jul": 7, "juli": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oktober": 10,
    "nov": 11, "november": 11,
    "dez": 12, "dezember": 12,
}

def parse_date_flexible(date_str: str, now_local: Optional[datetime] = None) -> Optional[datetime]:
    """Returns local date (00:00) as timezone-aware datetime in Europe/Berlin."""
    if not date_str:
        return None
    s = date_str.strip().lower()
    if now_local is None:
        now_local = datetime.now(TZ)

    if s in ("heute", "today"):
        d = now_local.date()
        return TZ.localize(datetime(d.year, d.month, d.day, 0, 0, 0))
    if s in ("morgen", "tomorrow"):
        d = (now_local + timedelta(days=1)).date()
        return TZ.localize(datetime(d.year, d.month, d.day, 0, 0, 0))
    if s in ("übermorgen", "uebermorgen"):
        d = (now_local + timedelta(days=2)).date()
        return TZ.localize(datetime(d.year, d.month, d.day, 0, 0, 0))

    # DD.MM.YYYY or DD.MM.
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$", s)
    if m:
        day = int(m.group(1))
        mon = int(m.group(2))
        year = m.group(3)
        if year is None:
            year_i = now_local.year
        else:
            year_i = int(year)
            if year_i < 100:
                year_i += 2000
        try:
            return TZ.localize(datetime(year_i, mon, day, 0, 0, 0))
        except ValueError:
            return None

    # "23 dez", "23 dezember 2025"
    m = re.match(r"^(\d{1,2})\s+([a-zäöüß]+)(?:\s+(\d{2,4}))?$", s)
    if m:
        day = int(m.group(1))
        mon_s = m.group(2).replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        mon = GER_MONTHS.get(mon_s, GER_MONTHS.get(m.group(2), None))
        if not mon:
            return None
        year = m.group(3)
        year_i = now_local.year if year is None else int(year) + (2000 if int(year) < 100 else 0)
        try:
            return TZ.localize(datetime(year_i, mon, day, 0, 0, 0))
        except ValueError:
            return None

    return None

def format_dt_local(dt_utc) -> str:
    """Formatiert UTC-Zeit als lokale Zeit (Europe/Berlin). Akzeptiert datetime oder ISO-String."""
    if dt_utc is None:
        return "—"
    if isinstance(dt_utc, str):
        try:
            dt_utc = datetime.fromisoformat(dt_utc)
        except Exception:
            return str(dt_utc)
    dt_utc = _ensure_utc(dt_utc)
    dt_local = dt_utc.astimezone(TZ)
    return dt_local.strftime("%d.%m.%Y %H:%M")


    return dt_local.strftime("%d.%m.%Y %H:%M")

def safe_int(x, default=None):
    try:
        return int(x)
    except Exception:
        return default

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild

def can_edit_event(interaction: discord.Interaction, ev: dict) -> bool:
    if interaction.user is None:
        return False
    if safe_int(ev.get("owner_id")) == interaction.user.id:
        return True
    if isinstance(interaction.user, discord.Member):
        return is_admin(interaction.user)
    return False

def load_events() -> Dict[str, dict]:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return raw
    except Exception as e:
        print(f"⚠️ load_events failed: {e}")
        return {}

def save_events():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(active_events, f, ensure_ascii=False, indent=2, default=list)
    except Exception as e:
        print(f"⚠️ save_events failed: {e}")

async def safe_save():
    # run in thread to avoid blocking event loop
    await asyncio.to_thread(save_events)

def _event_key(message_id: int) -> str:
    return str(message_id)

# in-memory store
active_events: Dict[str, dict] = load_events()
print(f"✅ {len(active_events)} gespeicherte Events geladen.")

# Rolls: pro Channel nur ein Roll gleichzeitig; pro Nutzer nur eine Teilnahme
active_rolls: Dict[int, dict] = {}

# AFK DM Prompts: message_id -> (event_message_id, user_id)
afkdmprompts: Dict[int, Tuple[int, int]] = {}

# -------------------- Slot handling --------------------

DEFAULT_SLOTS = [
    ("⚔️", "DPS", 3),
    ("🛡️", "Tank", 1),
    ("💉", "Heiler", 2),
]



# Slot-Parsing: erlaubt wie früher eine freie Slot-Definition, z.B.
# `⚔️:3 🛡️:1 💉:2` oder `<:Tank:123456789012345678>:1`

def _mention_user(user_id: int) -> str:
    return f"<@{int(user_id)}>"

SLOT_LABELS = {
    "⚔️": "DPS",
    "🛡️": "Tank",
    "💉": "Heiler",
}


def _normalize_emoji(s: str) -> str:
    """Normalize unicode emoji so ⚔ and ⚔️ match. Custom emojis stay unchanged."""
    if not s:
        return s
    # keep custom emoji format as-is
    if CUSTOM_EMOJI_RE.match(s) if "CUSTOM_EMOJI_RE" in globals() else False:
        return s
    # strip common variation selectors / joiners for matching
    return s.replace("\ufe0f", "").replace("\u200d", "")

def _find_slot_key(ev: dict, emoji: str) -> Optional[str]:
    slots = ev.get("slots", {})
    if emoji in slots:
        return emoji
    ne = _normalize_emoji(emoji)
    for k in slots.keys():
        if _normalize_emoji(str(k)) == ne:
            return k
    return None


CUSTOM_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_]+:\d{15,21}>$")
COLON_NAME_RE = re.compile(r"^:[A-Za-z0-9_]{1,64}:$")

# Match one slot spec anywhere in the string, allowing spaces around ":" and supporting:
# - unicode emoji (e.g. ⚔️)
# - custom emoji <:Name:123...> or <a:Name:123...>
# - colon-name :tank: (will be resolved to a guild emoji by name if possible)
SLOT_SPEC_RE = re.compile(
    r"(?P<emo><a?:[A-Za-z0-9_]+:\d{15,21}>|:[A-Za-z0-9_]{1,64}:|[^\s]+?)\s*:\s*(?P<num>\d{1,2})"
)

def _resolve_colon_name_to_guild_emoji(emo: str, guild: Optional[discord.Guild]) -> Optional[str]:
    if not guild:
        return None
    name = emo.strip(":")
    for e in guild.emojis:
        if e.name == name:
            return str(e)
    return None

def _parse_slots_spec(spec: str, guild: Optional[discord.Guild]) -> Optional[Dict[str, dict]]:
    if not spec:
        return None
    s = spec.strip()
    if not s:
        return None

    slots: Dict[str, dict] = {}
    consumed = 0

    for m in SLOT_SPEC_RE.finditer(s):
        between = s[consumed:m.start()]
        if between.strip():
            return None
        consumed = m.end()

        emo = m.group("emo").strip()
        num = int(m.group("num"))

        if num < 0 or num > 99:
            return None

        # Resolve :name: to actual guild emoji if available
        if COLON_NAME_RE.match(emo):
            resolved = _resolve_colon_name_to_guild_emoji(emo, guild)
            if resolved:
                emo = resolved
            else:
                # Discord does NOT accept :name: as a reaction emoji. Reject clearly.
                return None

        if not (CUSTOM_EMOJI_RE.match(emo) or len(emo) <= 64):
            return None

        label = SLOT_LABELS.get(emo, "Slot")
        slots[emo] = {"label": label, "limit": num, "main": [], "waitlist": []}

    if s[consumed:].strip():
        return None

    if not slots:
        return None
    return slots

def _default_slots_dict() -> Dict[str, dict]:

    slots = {}
    for emoji, label, limit in DEFAULT_SLOTS:
        slots[emoji] = {"label": label, "limit": limit, "main": [], "waitlist": []}
    return slots
def build_event_header(ev: dict) -> str:
    lines = []
    lines.append(f"📣 **Event:** {ev['title']}")
    lines.append(f"⏰ **START (Berlin):** **{format_dt_local(ev['event_time_utc'])} Uhr**")
    lines.append(f"🎯 **Zweck:** {ev.get('zweck','-')}")
    lines.append(f"📍 **Ort:** {ev.get('ort','-')}")
    if ev.get("min_level") is not None:
        lines.append(f"🎚️ **Mindestlevel:** **{int(ev.get('min_level') or 0)}+**")
    if ev.get("treffpunkt"):
        lines.append(f"📌 **Treffpunkt:** {ev.get('treffpunkt')}")
    if ev.get("gruppenlead"):
        lines.append(f"👑 **Gruppenlead:** {ev.get('gruppenlead')}")
    if ev.get("anmerkung"):
        lines.append(f"📝 **Anmerkung:** {ev.get('anmerkung')}")
    lines.append("─────────────────────────────────")
    return "\n".join(lines)

def build_slots_text(ev: dict) -> str:
    out = []
    for emoji, slot in ev["slots"].items():
        limit = int(slot.get("limit", 0))
        mains = list(slot.get("main", []))
        wait = list(slot.get("waitlist", []))
        def fmt_users(uids):
            return " ".join(f"<@{uid}>" for uid in uids) if uids else "—"
        out.append(f"{emoji} **{slot.get('label','Slot')}** ({len(mains)}/{limit})")
        out.append(f"• Main: {fmt_users(mains)}")
        if wait:
            out.append(f"• WL: {fmt_users(wait)}")
        out.append("")
    return "\n".join(out).strip()


async def post_to_event_thread(guild: discord.Guild, ev: dict, content: str):
    """Postet eine Nachricht in den Event-Thread (falls vorhanden)."""
    try:
        tid = ev.get("thread_id")
        if not tid:
            return
        th = guild.get_thread(int(tid))
        if th is None:
            ch = await bot.fetch_channel(int(tid))
            if isinstance(ch, discord.Thread):
                th = ch
        if th:
            await th.send(content)
    except Exception:
        pass

async def fetch_message(guild: discord.Guild, channel_id: int, message_id: int) -> Optional[discord.Message]:
    ch = guild.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception:
            return None
    try:
        return await ch.fetch_message(message_id)
    except Exception:
        return None

async def get_or_create_thread(message: discord.Message, ev: dict) -> Optional[discord.Thread]:
    # Try stored thread
    tid = safe_int(ev.get("thread_id"))
    if tid:
        th = message.guild.get_thread(tid)
        if th:
            return th
        try:
            fetched = await bot.fetch_channel(tid)
            if isinstance(fetched, discord.Thread):
                return fetched
        except Exception:
            pass
    # Create a thread if possible
    try:
        th = await message.create_thread(name=f"Event: {ev['title']}", auto_archive_duration=1440)
        ev["thread_id"] = th.id
        await safe_save()
        return th
    except Exception as e:
        print(f"⚠️ thread create failed: {e}")
        return None

async def update_event_post(guild: discord.Guild, message_id: int):
    ev = active_events.get(_event_key(message_id))
    if not ev:
        return
    msg = await fetch_message(guild, ev["channel_id"], message_id)
    if not msg:
        return
    header = build_event_header(ev)
    slots = build_slots_text(ev)
    content = header + "\n\n" + slots + "\n\n" + "Reagiere mit dem passenden Emoji um dich einzutragen."
    try:
        await msg.edit(content=content)
    except Exception as e:
        print(f"⚠️ msg.edit failed: {e}")

def _slot_all_mains(ev: dict) -> Set[int]:
    s = set()
    for slot in ev["slots"].values():
        s |= set(int(x) for x in slot.get("main", []))
    return s

def _slot_remove_user(ev: dict, user_id: int):
    for slot in ev["slots"].values():
        mains = list(slot.get("main", []))
        if user_id in mains:
            mains.remove(user_id)
            slot["main"] = mains
        wl = list(slot.get("waitlist", []))
        if user_id in wl:
            wl = [x for x in wl if x != user_id]
            slot["waitlist"] = wl

def _slot_add_user(ev: dict, emoji: str, user_id: int) -> Tuple[str, str]:
    """Returns (status, message). status in {'main','wait','reject'}"""
    # only one main slot across all roles
    if user_id in _slot_all_mains(ev):
        return "reject", "Du bist schon als Main in einem Slot eingetragen."
    slot_key = _find_slot_key(ev, emoji)
    if not slot_key:
        return "reject", "Unbekannter Slot."
    slot = ev["slots"][slot_key]
    limit = int(slot.get("limit", 0))
    mains = list(slot.get("main", []))
    wl = list(slot.get("waitlist", []))
    if user_id in mains or user_id in wl:
        return "reject", "Du bist hier schon eingetragen."
    if len(mains) < limit:
        mains.append(user_id)
        slot["main"] = mains
        return "main", "Eingetragen."
    wl.append(user_id)
    slot["waitlist"] = wl
    return "wait", "Slot voll, auf Warteliste gesetzt."


def _slot_promote_waitlist(ev: dict) -> List[Tuple[str, int]]:
    """Füllt freie Main-Slots aus der Warteliste auf. Gibt Liste der Nachrücker zurück."""
    promoted: List[Tuple[str, int]] = []
    for emoji, slot in ev["slots"].items():
        limit = int(slot.get("limit", 0))
        mains = list(slot.get("main", []))
        wl = list(slot.get("waitlist", []))
        changed = False
        while len(mains) < limit and wl:
            nxt = wl.pop(0)
            if nxt in mains:
                continue
            mains.append(nxt)
            promoted.append((emoji, int(nxt)))
            changed = True
        if changed:
            slot["main"] = mains
            slot["waitlist"] = wl
    return promoted

# -------------------- Text replacement helpers for edits --------------------

def replace_with_struck(text: str, prefix: str, old: str, new: str) -> str:
    # Replace "prefix old" with "prefix ~~old~~ → new"
    pattern = re.compile(rf"^{re.escape(prefix)}\s*(.+)$", re.MULTILINE)
    def repl(m):
        cur = m.group(1).strip()
        if cur == new:
            return m.group(0)
        # If we don't trust 'old', use detected
        old_v = old if old else cur
        return f"{prefix} ~~{old_v}~~ → {new}"
    if pattern.search(text):
        return pattern.sub(repl, text, count=1)
    return text + f"\n{prefix} {new}"

def extract_current_value(text: str, prefix: str) -> Optional[str]:
    m = re.search(rf"^{re.escape(prefix)}\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else None

# -------------------- Slash commands --------------------

ART_CHOICES = [
    app_commands.Choice(name="PvE", value="PvE"),
    app_commands.Choice(name="PvP", value="PvP"),
    app_commands.Choice(name="Boss", value="Boss"),
    app_commands.Choice(name="Sonstiges", value="Sonstiges"),
]

def _build_event_title(art: str, zweck: str) -> str:
    z = zweck.strip()
    if len(z) > 60:
        z = z[:57] + "..."
    return f"{art}: {z}"

async def _event_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    gid = interaction.guild_id
    if not gid:
        return []
    current_l = (current or "").lower()
    items = []
    for mid_s, ev in active_events.items():
        if safe_int(ev.get("guild_id")) != gid:
            continue
        # only creator/admin can see
        if not can_edit_event(interaction, ev):
            continue
        name = ev.get("title", f"Event {mid_s}")
        if current_l and current_l not in name.lower():
            continue
        # include message id for uniqueness
        label = f"{name} (#{mid_s[-6:]})"
        items.append(app_commands.Choice(name=label[:100], value=str(mid_s)))
        if len(items) >= 25:
            break
    return items

@app_commands.describe(
    art="Event-Art",
    zweck="Kurz: was macht ihr?",
    ort="Ort",
    datum="Datum: z.B. 23.12.2025 / heute / morgen",
    zeit="Zeit: HH:MM (z.B. 20:00)",
    gruppenlead="Optional: Wer leitet?",
    treffpunkt="Optional: Treffpunkt",
    anmerkung="Optional: Zusatzinfo",
    auto_delete="Optional: Auto-Löschen deaktivieren (off)",
    slots="Slots (Pflicht): z.B. ⚔️:3 🛡️:1 💉:2 oder :tank: : 1",
)



async def _event_delete_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    # identisch zu edit: User sieht nur eigene Events, Admins alle
    return await _event_autocomplete(interaction, current)

@app_commands.choices(art=ART_CHOICES)
@bot.tree.command(name="event", description="Erstellt ein neues Event mit Slot-Registrierung")
async def event_create(
    interaction: discord.Interaction,
    art: app_commands.Choice[str],
    zweck: str,
    ort: str,
    datum: str,
    zeit: str,
    slots: str,
    level: int,
    gruppenlead: Optional[str] = None,
    treffpunkt: Optional[str] = None,
    auto_delete: Optional[str] = None,
    anmerkung: Optional[str] = None,
):
    if interaction.guild is None or interaction.channel is None:
        # Not deferred yet -> must use response
        await interaction.response.send_message("❌ Nur auf einem Server-Kanal nutzbar.", ephemeral=True)
        return

    # Defer immediately to avoid Discord's 3s interaction timeout
    await interaction.response.defer(ephemeral=True)

    dt_date = parse_date_flexible(datum)
    if not dt_date:
        await interaction.followup.send("❌ Ungültiges Datum. Beispiele: `heute`, `morgen`, `23.12.2025`", ephemeral=True)
        return
    hm = _parse_time_hhmm(zeit)
    if not hm:
        await interaction.followup.send("❌ Ungültige Zeit. Beispiel: `20:00`", ephemeral=True)
        return

    dt_local = dt_date.replace(hour=hm[0], minute=hm[1])
    dt_utc = _ensure_utc(dt_local.astimezone(pytz.utc))

    title = _build_event_title(art.value, zweck)
    # Auto-Delete: standard 2h nach Start; optional ausschaltbar mit auto_delete=off
    auto_delete_hours = AUTO_DELETE_HOURS_DEFAULT
    if auto_delete is not None and auto_delete.strip() != "":
        if auto_delete.strip().lower() != "off":
            await interaction.followup.send(
                "❌ auto_delete akzeptiert nur `off` (oder leer lassen).",
                ephemeral=True,
            )
            return
        auto_delete_hours = None


    # Build slots (default oder frei definierbar via `slots` Parameter)
    slots_dict = _parse_slots_spec(slots, interaction.guild)
    if not slots_dict:
        await interaction.followup.send("❌ Ungültige Slot-Definition. Beispiele: `⚔️ : 3 🛡️: 1 💉 :2` oder (Guild-Emoji) `:tank: : 1`", ephemeral=True)
        return
    slots = slots_dict

    # Mindestlevel
    if level < 1 or level > 100:
        await interaction.followup.send("❌ Level muss zwischen 1 und 100 liegen.", ephemeral=True)
        return

    ev = {
        "guild_id": interaction.guild.id,
        "channel_id": interaction.channel.id,
        "thread_id": None,
        "owner_id": interaction.user.id,
        "creator_id": interaction.user.id,
        "min_level": int(level),
        "title": title,
        "art": art.value,
        "zweck": zweck.strip(),
        "ort": ort.strip(),
        "treffpunkt": (treffpunkt.strip() if treffpunkt else None),
        "gruppenlead": (gruppenlead.strip() if gruppenlead else None),
        "anmerkung": (anmerkung.strip() if anmerkung else None),
        "event_time_utc": dt_utc.isoformat(),
        # flags/state
        "reminder60_sent": [],
        "auto_delete_hours": auto_delete_hours,
        "afk_enabled": True,
        "afk_state": {"confirmed": [], "prompt_ids": [], "started": False, "finished": False, "last_prompt_at": None},
    }

    # Create initial post
    header = build_event_header({**ev, "event_time_utc": dt_utc})
    ev_post = {**ev, "event_time_utc": dt_utc}  # for header build
    content = build_event_header(ev_post) + "\n\n" + build_slots_text({"slots": ev_post.get("slots", slots) or slots, **ev_post})
    content += "\n\nReagiere mit dem passenden Emoji um dich einzutragen."

    await interaction.followup.send("✅ Event wird erstellt…", ephemeral=True)
    msg = await interaction.channel.send(content)
    # add reactions
    for emoji in slots.keys():
        try:
            await msg.add_reaction(emoji)
        except Exception:
            pass

    # create thread
    th = await get_or_create_thread(msg, {"title": title, "thread_id": None})
    if th:
        ev["thread_id"] = th.id

    # persist with slots and message id key
    ev["slots"] = slots
    active_events[_event_key(msg.id)] = ev
    await safe_save()

    if th:
        await th.send("🧵 Thread für Updates.")

    # update final post with proper header using stored dt_utc
    await update_event_post(interaction.guild, msg.id)

@app_commands.describe(
    event="Event auswählen",
    ort="Neuer Ort (optional)",
    treffpunkt="Neuer Treffpunkt (optional)",
    datum="Neues Datum (optional)",
    zeit="Neue Zeit (optional)",
    gruppenlead="Neuer Gruppenlead (optional)",
    anmerkung="Neue Anmerkung (optional)",
    level="Neues Mindestlevel (z.B. 61)"
)
@bot.tree.command(name="event_edit", description="Bearbeitet ein Event (nur Ersteller/Admin)")
@app_commands.autocomplete(event=_event_autocomplete)
async def event_edit(
    interaction: discord.Interaction,
    event: str,
    ort: Optional[str] = None,
    treffpunkt: Optional[str] = None,
    datum: Optional[str] = None,
    zeit: Optional[str] = None,
    slots: Optional[str] = None,
    gruppenlead: Optional[str] = None,
    level: Optional[int] = None,
    anmerkung: Optional[str] = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server nutzbar.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)


    ev = active_events.get(str(event))
    if not ev:
        await interaction.followup.send("❌ Event nicht gefunden.", ephemeral=True)
        return
    if not can_edit_event(interaction, ev):
        await interaction.followup.send("❌ Nicht erlaubt (nur Ersteller/Admin).", ephemeral=True)
        return

    # read current header from message so we can strike-through like before
    msg_id = int(event)
    msg = await fetch_message(interaction.guild, ev["channel_id"], msg_id)
    if not msg:
        await interaction.followup.send("❌ Event-Post nicht gefunden.", ephemeral=True)
        return
    header_text = msg.content.split("\n\n", 1)[0]

    PREFIX_TIME = "🗓️ **Zeit:**"
    PREFIX_ORT = "📍 **Ort:**"
    PREFIX_TREFF = "📌 **Treffpunkt:**"
    PREFIX_LEAD = "👑 **Gruppenlead:**"
    PREFIX_NOTE = "📝 **Anmerkung:**"

    changes = []

    # time
    dt_utc = datetime.fromisoformat(ev["event_time_utc"])
    dt_utc = _ensure_utc(dt_utc)

    if datum or zeit:
        # keep existing local date/time unless changed
        cur_local = dt_utc.astimezone(TZ)
        if datum:
            d0 = parse_date_flexible(datum, now_local=datetime.now(TZ))
            if not d0:
                await interaction.followup.send("❌ Ungültiges Datum.", ephemeral=True)
                return
            cur_local = cur_local.replace(year=d0.year, month=d0.month, day=d0.day)
        if zeit:
            hm = _parse_time_hhmm(zeit)
            if not hm:
                await interaction.followup.send("❌ Ungültige Zeit (HH:MM).", ephemeral=True)
                return
            cur_local = cur_local.replace(hour=hm[0], minute=hm[1])
        new_utc = _ensure_utc(cur_local.astimezone(pytz.utc))
        old_val = extract_current_value(header_text, PREFIX_TIME) or format_dt_local(dt_utc)
        new_val = format_dt_local(new_utc)
        header_text = replace_with_struck(header_text, PREFIX_TIME, old_val, new_val)
        ev["event_time_utc"] = new_utc.isoformat()
        changes.append(f"Zeit: ~~{old_val}~~ → {new_val}")
        dt_utc = new_utc


    # Slots (mit Erhalt der bestehenden Anmeldungen)
    if slots is not None:
        new_slots_dict = _parse_slots_spec(slots, interaction.guild)
        if not new_slots_dict:
            await interaction.followup.send("❌ Ungültige Slot-Definition. Beispiel: ⚔️:3 🛡️:1 💉:2", ephemeral=True)
            return
        # event state stores only the numeric limits + signup lists
        new_slots = {k: int(v.get("limit", 0)) for k, v in new_slots_dict.items()}
        

        old_slots = ev.get("slots", {})
        updated_slots = {}

        def _n(x: str) -> str:
            try:
                return _normalize_emoji(x)
            except Exception:
                return str(x)

        old_by_norm = {_n(k): k for k in old_slots.keys()}

        new_norms = {_n(k) for k in new_slots.keys()}
        not_removable = []
        for ok, oslot in old_slots.items():
            if _n(ok) not in new_norms:
                if list(oslot.get("main", [])) or list(oslot.get("waitlist", [])):
                    not_removable.append(ok)

        if not_removable:
            await interaction.followup.send(
                "❌ Du kannst keine Slots entfernen, in denen noch Leute eingetragen sind: " + " ".join(not_removable),
                ephemeral=True,
            )
            return

        slot_lines = []
        overflow_notes = []
        overflow_moved: Dict[str, int] = {}

        for nk, nlimit in new_slots.items():
            nlimit = int(nlimit)
            ok = old_by_norm.get(_n(nk))
            if ok is not None:
                oslot = old_slots.get(ok, {})
                mains = list(oslot.get("main", []))
                wl = list(oslot.get("waitlist", []))
                old_limit = int(oslot.get("limit", 0))

                moved = 0
                if old_limit and nlimit < old_limit and len(mains) > nlimit:
                    overflow = mains[nlimit:]
                    mains = mains[:nlimit]
                    wl = overflow + wl
                    moved = len(overflow)
                    overflow_moved[nk] = moved

                updated_slots[nk] = {"limit": nlimit, "main": mains, "waitlist": wl}

                if old_limit != nlimit:
                    line = f"{nk}: {old_limit} → {nlimit}"
                    if moved:
                        line += f" (⚠️ {moved} auf Warteliste)"
                    slot_lines.append(line)
            else:
                updated_slots[nk] = {"limit": nlimit, "main": [], "waitlist": []}
                slot_lines.append(f"{nk}: neu ({nlimit})")

        # Entfernte Slots (nur möglich, wenn leer)
        for ok in old_slots.keys():
            if _n(ok) not in new_norms:
                slot_lines.append(f"{ok}: entfernt")

        ev["slots"] = updated_slots

        try:
            promoted = _slot_promote_waitlist(ev)
        except Exception:
            promoted = []

        if slot_lines:
            changes.append("Slots: " + ", ".join(slot_lines))


        try:
            for ek in updated_slots.keys():
                try:
                    await msg.add_reaction(ek)
                except Exception:
                    pass
            for ok in old_slots.keys():
                if _n(ok) not in new_norms:
                    try:
                        await msg.clear_reaction(ok)
                    except Exception:
                        pass
        except Exception:
            pass

        if slot_lines:
            try:
                tid = ev.get("thread_id")
                thread = None
                if tid:
                    thread = interaction.guild.get_thread(int(tid))
                    if thread is None:
                        ch = await bot.fetch_channel(int(tid))
                        if isinstance(ch, discord.Thread):
                            thread = ch
                if thread:
                    pretty = "\n".join(f"• {x}" for x in slot_lines)
                    await thread.send("🛠️ **Slots angepasst:**\n" + pretty)
            except Exception:
                pass



        if promoted:
            try:
                tid = ev.get("thread_id")
                thread = None
                if tid:
                    thread = interaction.guild.get_thread(int(tid))
                    if thread is None:
                        ch = await bot.fetch_channel(int(tid))
                        if isinstance(ch, discord.Thread):
                            thread = ch
                if thread:
                    lines = []
                    for emo, uid in promoted:
                        member = interaction.guild.get_member(int(uid))
                        name = member.display_name if member else f"<@{uid}>"
                        lines.append(f"{emo} → {name}")
                    if lines:
                        await thread.send("🔄 **Nachgerückt:**\n" + "\n".join(f"• {x}" for x in lines))
            except Exception:
                pass

    # Ort
    if ort is not None:
        new = ort.strip()
        old = extract_current_value(header_text, PREFIX_ORT) or (ev.get("ort") or "-")
        header_text = replace_with_struck(header_text, PREFIX_ORT, old, new)
        ev["ort"] = new
        changes.append(f"Ort: ~~{old}~~ → {new}")

    # Treffpunkt (and keep "Ort updated" in the header text flow — same strike behavior)
    if treffpunkt is not None:
        tp = treffpunkt.strip()
        if tp == "":
            # remove line entirely
            header_text = re.sub(rf"^{re.escape(PREFIX_TREFF)}.*\n?", "", header_text, flags=re.M)
            ev["treffpunkt"] = None
            changes.append("Treffpunkt entfernt")
        else:
            old = extract_current_value(header_text, PREFIX_TREFF) or (ev.get("treffpunkt") or "-")
            if re.search(rf"^{re.escape(PREFIX_TREFF)}", header_text, re.M):
                header_text = replace_with_struck(header_text, PREFIX_TREFF, old, tp)
            else:
                # insert after Ort line
                header_text = re.sub(
                    rf"^{re.escape(PREFIX_ORT)}.*$",
                    lambda m: m.group(0) + f"\n{PREFIX_TREFF} {tp}",
                    header_text,
                    flags=re.M,
                    count=1,
                )
            ev["treffpunkt"] = tp
            changes.append(f"Treffpunkt: ~~{old}~~ → {tp}")

    # Gruppenlead
    if gruppenlead is not None:
        gl = gruppenlead.strip()
        if gl == "":
            header_text = re.sub(rf"^{re.escape(PREFIX_LEAD)}.*\n?", "", header_text, flags=re.M)
            ev["gruppenlead"] = None
            changes.append("Gruppenlead entfernt")
        else:
            old = extract_current_value(header_text, PREFIX_LEAD) or (ev.get("gruppenlead") or "-")
            if re.search(rf"^{re.escape(PREFIX_LEAD)}", header_text, re.M):
                header_text = replace_with_struck(header_text, PREFIX_LEAD, old, gl)
            else:
                header_text = header_text.replace("─────────────────────────────────", f"{PREFIX_LEAD} {gl}\n─────────────────────────────────", 1)
            ev["gruppenlead"] = gl
            changes.append(f"Gruppenlead: ~~{old}~~ → {gl}")

    # Mindestlevel
    if level is not None:
        if level < 1 or level > 100:
            await interaction.followup.send("❌ Level muss zwischen 1 und 100 liegen.", ephemeral=True)
            return
        old_lvl = ev.get("min_level")
        ev["min_level"] = int(level)
        if old_lvl is None:
            changes.append(f"Mindestlevel: {int(level)}+")
        else:
            changes.append(f"Mindestlevel: ~~{int(old_lvl)}+~~ → {int(level)}+")

    # Anmerkung
    if anmerkung is not None:
        an = anmerkung.strip()
        if an == "":
            header_text = re.sub(rf"^{re.escape(PREFIX_NOTE)}.*\n?", "", header_text, flags=re.M)
            ev["anmerkung"] = None
            changes.append("Anmerkung entfernt")
        else:
            old = extract_current_value(header_text, PREFIX_NOTE) or (ev.get("anmerkung") or "-")
            if re.search(rf"^{re.escape(PREFIX_NOTE)}", header_text, re.M):
                header_text = replace_with_struck(header_text, PREFIX_NOTE, old, an)
            else:
                header_text = header_text.replace("─────────────────────────────────", f"{PREFIX_NOTE} {an}\n─────────────────────────────────", 1)
            ev["anmerkung"] = an
            changes.append(f"Anmerkung: ~~{old}~~ → {an}")

    # Apply updated content
    active_events[_event_key(msg_id)] = ev
    await safe_save()

    # rebuild full post from ev (but keep the strike-through header the user wants)
    slots_text = build_slots_text(ev)
    content = header_text + "\n\n" + slots_text + "\n\nReagiere mit dem passenden Emoji um dich einzutragen."
    try:
        await msg.edit(content=content)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Konnte Post nicht editieren: {e}", ephemeral=True)
        return

    # post changes to thread
    thread = None
    try:
        thread = await get_or_create_thread(msg, ev)
    except Exception:
        thread = None
    if thread and changes:
        try:
            await thread.send("✏️ **Event geändert:**\n" + "\n".join(f"• {c}" for c in changes))
        except Exception:
            pass

    await interaction.followup.send("✅ Event aktualisiert.", ephemeral=True)

@app_commands.describe(
    mode="AFK-Check an/aus",
    event="Event auswählen",
)
@app_commands.choices(mode=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
@bot.tree.command(name="event_afk", description="Schaltet den AFK-Check pro Event ein/aus (Ersteller/Admin)")
@app_commands.autocomplete(event=_event_autocomplete)
async def event_afk(interaction: discord.Interaction, mode: app_commands.Choice[str], event: str):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server.", ephemeral=True)
        return
    ev = active_events.get(str(event))
    if not ev:
        await interaction.response.send_message("❌ Event nicht gefunden.", ephemeral=True)
        return
    if not can_edit_event(interaction, ev):
        await interaction.response.send_message("❌ Nicht erlaubt.", ephemeral=True)
        return
    ev["afk_enabled"] = (mode.value == "on")
    active_events[str(event)] = ev
    await safe_save()
    await interaction.response.send_message(f"✅ AFK-Check ist jetzt **{mode.value}**.", ephemeral=True)

@bot.tree.command(name="event_reset_notifications", description="Setzt Reminder-Flags für ein Event zurück (Ersteller/Admin)")
@app_commands.autocomplete(event=_event_autocomplete)
async def event_reset_notifications(interaction: discord.Interaction, event: str):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server.", ephemeral=True)
        return
    ev = active_events.get(str(event))
    if not ev:
        await interaction.response.send_message("❌ Event nicht gefunden.", ephemeral=True)
        return
    if not can_edit_event(interaction, ev):
        await interaction.response.send_message("❌ Nicht erlaubt.", ephemeral=True)
        return
    ev["reminder60_sent"] = []
    active_events[str(event)] = ev
    await safe_save()
    await interaction.response.send_message("✅ Reminder-Flags zurückgesetzt.", ephemeral=True)

@bot.tree.command(name="help", description="Zeigt Hilfe")
async def help_cmd(interaction: discord.Interaction):
    txt = (
        "**SlotBot – Hilfe**\n\n"
        "• `/event` – Event erstellen\n"
        "  Beispiel: `/event art:PvE zweck:\"XP Farm\" ort:\"Calpheon\" treffpunkt:\"Vor dem Stall\" datum:heute zeit:20:00`\n"
        "• `/event_edit` – Event bearbeiten (Dropdown)\n"
        "• `/event_afk on|off` – AFK-Check pro Event an/aus\n"
        "• `/event_reset_notifications` – Reminder-Flags zurücksetzen\n\n"
        f"Reminder: **{REMINDER_MIN_BEFORE} min** vorher (DM, einmalig)\n"
        f"AFK: Start **{AFK_START_MIN_BEFORE} min** vorher, Dauer **{AFK_DURATION_MIN} min**, Ping alle **{AFK_INTERVAL_MIN} min** (per PN/DM)\n"
    )
    await interaction.response.send_message(txt, ephemeral=True)



# -------------------- Roll Commands --------------------

@bot.tree.command(name="start_roll", description="Startet einen Roll (Teilnahme via /roll). Nur ein Roll pro Channel.")
@app_commands.describe(dauer="Dauer in Minuten (z.B. 5)", grund="Optional: Preis/Grund")
async def start_roll(interaction: discord.Interaction, dauer: int, grund: Optional[str] = None):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("❌ Nur auf einem Server-Kanal.", ephemeral=True)
        return
    if dauer <= 0 or dauer > 180:
        await interaction.response.send_message("❌ Dauer muss zwischen 1 und 180 Minuten liegen.", ephemeral=True)
        return

    ch_id = interaction.channel.id
    if ch_id in active_rolls and active_rolls[ch_id].get("active"):
        await interaction.response.send_message("❌ In diesem Channel läuft schon ein Roll.", ephemeral=True)
        return

    ends_at = _now_utc() + timedelta(minutes=dauer)
    active_rolls[ch_id] = {
        "active": True,
        "owner_id": interaction.user.id,
        "guild_id": interaction.guild.id,
        "channel_id": ch_id,
        "ends_at": ends_at.isoformat(),
        "rolls": {},
    }
    msg = f"🎲 Roll gestartet! Teilnahme mit **/roll**. Ende in **{dauer} Min**."
    if grund and grund.strip():
        msg += f"\n🏷️ **Preis/Grund:** {grund.strip()}"
    await interaction.response.send_message(msg, ephemeral=False)


@bot.tree.command(name="roll", description="Würfelt im aktuellen Roll (nur 1x). Zeigt Zahl öffentlich.")
async def roll(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("❌ Nur auf einem Server-Kanal.", ephemeral=True)
        return
    ch_id = interaction.channel.id
    st = active_rolls.get(ch_id)
    if not st or not st.get("active"):
        await interaction.response.send_message("❌ Aktuell läuft hier kein Roll.", ephemeral=True)
        return

    try:
        ends_at = _ensure_utc(datetime.fromisoformat(st["ends_at"]))
    except Exception:
        ends_at = _now_utc()
    if _now_utc() >= ends_at:
        await interaction.response.send_message("⏱️ Roll ist schon abgelaufen.", ephemeral=True)
        return

    rolls = st.get("rolls") or {}
    uid = interaction.user.id
    if str(uid) in rolls:
        await interaction.response.send_message("❌ Du hast schon gewürfelt.", ephemeral=True)
        return

    import random
    value = random.randint(1, 100)
    rolls[str(uid)] = int(value)
    st["rolls"] = rolls
    active_rolls[ch_id] = st

    await interaction.response.send_message(f"🎲 <@{uid}> würfelt **{value}**!", ephemeral=False)

@bot.tree.command(name="stop_roll", description="Stoppt den aktuellen Roll und zieht einen Gewinner.")
async def stop_roll(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("❌ Nur auf einem Server-Kanal.", ephemeral=True)
        return
    ch_id = interaction.channel.id
    st = active_rolls.get(ch_id)
    if not st or not st.get("active"):
        await interaction.response.send_message("❌ Hier läuft kein Roll.", ephemeral=True)
        return

    # only starter or admin can stop
    if st.get("owner_id") != interaction.user.id:
        if isinstance(interaction.user, discord.Member) and not is_admin(interaction.user):
            await interaction.response.send_message("❌ Nur der Starter oder ein Admin kann stoppen.", ephemeral=True)
            return

    rolls = st.get("rolls") or {}
    norm = {}
    for k, v in rolls.items():
        try:
            norm[int(k)] = int(v)
        except Exception:
            pass

    st["active"] = False
    active_rolls[ch_id] = st

    if not norm:
        await interaction.response.send_message("🫠 Roll beendet – niemand hat teilgenommen.", ephemeral=False)
        return

    max_val = max(norm.values())
    top = [uid for uid, val in norm.items() if val == max_val]

    import random
    winner = random.choice(top)

    sorted_items = sorted(norm.items(), key=lambda kv: kv[1], reverse=True)
    lines = [f"• <@{uid}>: **{val}**" for uid, val in sorted_items[:20]]

    await interaction.response.send_message("🏁 **Roll beendet!**\\n" + "\\n".join(lines), ephemeral=False)
    await interaction.followup.send(f"🏆 Gewinner: <@{winner}> 🎉 (mit **{max_val}**)")

async def roll_watcher_task():

        await bot.wait_until_ready()
        while not bot.is_closed():
            now = _now_utc()
            for ch_id, st in list(active_rolls.items()):
                if not st.get("active"):
                    continue
                try:
                    ends_at = _ensure_utc(datetime.fromisoformat(st["ends_at"]))
                except Exception:
                    continue
                if now < ends_at:
                    continue

                guild = bot.get_guild(int(st.get("guild_id")))
                if not guild:
                    st["active"] = False
                    active_rolls[ch_id] = st
                    continue
                try:
                    ch = guild.get_channel(int(st.get("channel_id"))) or await bot.fetch_channel(int(st.get("channel_id")))
                except Exception:
                    st["active"] = False
                    active_rolls[ch_id] = st
                    continue

                rolls = st.get("rolls") or {}
                norm = {}
                for k, v in rolls.items():
                    try:
                        norm[int(k)] = int(v)
                    except Exception:
                        pass

                st["active"] = False
                active_rolls[ch_id] = st

                if not norm:
                    try:
                        await ch.send("🫠 Roll beendet – niemand hat teilgenommen.")
                    except Exception:
                        pass
                    continue

                max_val = max(norm.values())
                top = [uid for uid, val in norm.items() if val == max_val]

                import random
                winner = random.choice(top)

                sorted_items = sorted(norm.items(), key=lambda kv: kv[1], reverse=True)
                lines = [f"• <@{uid}>: **{val}**" for uid, val in sorted_items[:20]]

                try:
                    await ch.send("🏁 **Roll beendet!**\n" + "\n".join(lines))
                except Exception:
                    pass
                try:
                    await ch.send(f"🏆 Gewinner: <@{winner}> 🎉 (mit **{max_val}**)")
                except Exception:
                    pass

            await asyncio.sleep(2)
@bot.tree.command(name="test", description="Testet ob der Bot läuft (zeigt Basis-Status).")
async def test_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Bot läuft. Slash-Commands sind aktiv.", ephemeral=True)



# -------------------- Event Delete (nur eigene) --------------------

async def event_delete_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete: normal nur eigene Events; Admins alle."""
    res = []
    if interaction.guild is None:
        return res
    guild_id = interaction.guild.id
    uid = interaction.user.id

    isadm = isinstance(interaction.user, discord.Member) and is_admin(interaction.user)
    for mid_s, ev in active_events.items():
        if int(ev.get("guild_id", 0)) != int(guild_id):
            continue
        if (not isadm) and int(ev.get("creator_id", 0)) != int(uid):
            continue
        title = ev.get("title", "Event")
        when = format_dt_local(ev.get("event_time_utc"))
        label = f"{title} ({when})"
        if current and current.lower() not in label.lower():
            continue
        res.append(app_commands.Choice(name=label[:100], value=str(mid_s)))
        if len(res) >= 25:
            break
    return res

class ConfirmDeleteView(discord.ui.View):
    def __init__(self, *, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.confirmed = False

    @discord.ui.button(label="Löschen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.defer(ephemeral=True)

@bot.tree.command(name="event_delete", description="Löscht ein Event (normal: nur deine Events).")
@app_commands.describe(event="Event auswählen")
@app_commands.autocomplete(event=event_delete_autocomplete)
async def event_delete_cmd(interaction: discord.Interaction, event: str):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server.", ephemeral=True)
        return

    ev = active_events.get(str(event))
    if not ev or int(ev.get("guild_id", 0)) != interaction.guild.id:
        await interaction.response.send_message("❌ Event nicht gefunden.", ephemeral=True)
        return

    isadm = isinstance(interaction.user, discord.Member) and is_admin(interaction.user)
    if (not isadm) and int(ev.get("creator_id", ev.get("owner_id", 0)) or 0) != interaction.user.id:
        await interaction.response.send_message("❌ Du kannst nur deine eigenen Events löschen.", ephemeral=True)
        return

    view = ConfirmDeleteView(timeout=30)
    await interaction.response.send_message(
        f"⚠️ Willst du das Event wirklich löschen?\n**{ev.get('title','Event')}** ({format_dt_local(ev.get('event_time_utc'))})",
        ephemeral=True,
        view=view,
    )
    await view.wait()

    if not view.confirmed:
        try:
            await interaction.followup.send("✅ Abgebrochen.", ephemeral=True)
        except Exception:
            pass
        return

    guild = interaction.guild

    # Thread löschen
    try:
        tid = ev.get("thread_id")
        if tid:
            th = guild.get_thread(int(tid))
            if th is None:
                ch = await bot.fetch_channel(int(tid))
                if isinstance(ch, discord.Thread):
                    th = ch
            if th:
                await th.delete()
    except Exception:
        pass

    # Message löschen
    try:
        msg = await fetch_message(guild, int(ev["channel_id"]), int(event))
        if msg:
            await msg.delete()
    except Exception:
        pass

    # Aus Speicher entfernen
    try:
        del active_events[str(event)]
        await safe_save()
    except Exception:
        pass

    try:
        await interaction.followup.send("🗑️ Event gelöscht.", ephemeral=True)
    except Exception:
        pass

# -------------------- Reactions --------------------

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return

    emoji = str(payload.emoji)

    # ---- AFK DM confirmation ----
    if payload.guild_id is None:
        # user reacted in a DM channel
        if emoji == "✅" and payload.message_id in afkdmprompts:
            event_msg_id, target_user_id = afkdmprompts.get(payload.message_id, (None, None))
            if target_user_id != payload.user_id or event_msg_id is None:
                return
            ev = active_events.get(_event_key(int(event_msg_id)))
            if not ev:
                return
            st = ev.setdefault("afk_state", {"confirmed": [], "prompt_ids": [], "started": False, "finished": False, "last_prompt_at": None})
            confirmed = set(int(x) for x in st.get("confirmed", []))
            confirmed.add(payload.user_id)
            st["confirmed"] = list(confirmed)
            ev["afk_state"] = st
            active_events[_event_key(int(event_msg_id))] = ev
            await safe_save()
            try:
                user = bot.get_user(payload.user_id) or await bot.fetch_user(payload.user_id)
                if user:
                    await user.send("✅ Bestätigt! Du bist für das Event eingeplant.")
                    try:
                        if payload.message_id in afkdmprompts:
                            del afkdmprompts[payload.message_id]
                    except Exception:
                        pass
            except Exception:
                pass
        return

    # ---- Guild slot handling ----
    ev = active_events.get(_event_key(payload.message_id))
    if not ev:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    user_id = payload.user_id

    # Slot selection
    status, _ = _slot_add_user(ev, emoji, user_id)
    if status == "reject":
        # remove the reaction to enforce 1 slot / valid slot
        msg = await fetch_message(guild, ev["channel_id"], payload.message_id)
        if msg:
            try:
                await msg.remove_reaction(payload.emoji, discord.Object(id=user_id))
            except Exception:
                pass
        return

    active_events[_event_key(payload.message_id)] = ev
    await safe_save()
    try:
        await update_event_post(guild, payload.message_id)
    except Exception as e:
        print(f"❌ update_event_post failed: {e}")

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None:
        return
    ev = active_events.get(_event_key(payload.message_id))
    if not ev:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    emoji = str(payload.emoji)
    user_id = payload.user_id
    slot_key = _find_slot_key(ev, emoji)
    if not slot_key:
        return
    slot = ev["slots"][slot_key]
    mains = list(slot.get("main", []))
    wl = list(slot.get("waitlist", []))
    changed = False
    promoted: List[Tuple[str, int]] = []
    promoted = []
    if user_id in mains:
        mains.remove(user_id)
        slot["main"] = mains
        changed = True
    if user_id in wl:
        wl = [x for x in wl if x != user_id]
        slot["waitlist"] = wl
        changed = True
    if changed:
        promoted = _slot_promote_waitlist(ev)
        active_events[_event_key(payload.message_id)] = ev
        await safe_save()
        await update_event_post(guild, payload.message_id)
    try:
        await post_to_event_thread(guild, ev, f"➖ Abmeldung: <@{user_id}>")
        if promoted:
            await post_to_event_thread(guild, ev, "\n".join([f"➕ Nachgerückt {emo}: <@{uid}>" for emo, uid in promoted]))
    except Exception:
        pass

# -------------------- Background tasks --------------------

BACKGROUND_TASKS: Dict[str, Optional[asyncio.Task]] = {"reminder": None, "afk": None, "cleanup": None, "roll_watcher": None}
TASKS_STARTED = False

async def reminder_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = _now_utc()
        changed = False
        for mid_s, ev in list(active_events.items()):
            try:
                dt_utc = _ensure_utc(datetime.fromisoformat(ev["event_time_utc"]))
            except Exception:
                continue
            if dt_utc <= now:
                continue
            seconds_left = (dt_utc - now).total_seconds()
            if 0 <= seconds_left <= REMINDER_MIN_BEFORE * 60:
                sent = set(int(x) for x in ev.get("reminder60_sent", []))
                guild = bot.get_guild(int(ev["guild_id"]))
                if not guild:
                    continue
                for uid in _slot_all_mains(ev):
                    if uid in sent:
                        continue
                    try:
                        member = guild.get_member(uid) or await guild.fetch_member(uid)
                        await member.send(f"⏰ Dein Event **{ev.get('title','(Event)')}** startet in **{REMINDER_MIN_BEFORE} Minuten**!")
                        sent.add(uid)
                        changed = True
                    except Exception:
                        pass
                ev["reminder60_sent"] = list(sent)
                active_events[mid_s] = ev
        if changed:
            await safe_save()
        await asyncio.sleep(60)


async def afk_task():
    """AFK-Check per PN (DM): startet 30 Min vorher, läuft 20 Min, pingt alle 5 Min.
    Bestätigung per ✅ Reaktion auf die DM. Wer bestätigt hat, wird nicht mehr gepingt.
    Nach Ablauf: Slots von Nicht-Bestätigten werden freigegeben + WL rückt nach.
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = _now_utc()
        for mid_s, ev in list(active_events.items()):
            if ev.get("afk_enabled", True) is False:
                continue
            try:
                dt_utc = _ensure_utc(datetime.fromisoformat(ev["event_time_utc"]))
            except Exception:
                continue
            if dt_utc <= now:
                continue

            window_start = dt_utc - timedelta(minutes=AFK_START_MIN_BEFORE)
            window_end = window_start + timedelta(minutes=AFK_DURATION_MIN)
            interval = timedelta(minutes=AFK_INTERVAL_MIN)

            st = ev.setdefault(
                "afk_state",
                {"confirmed": [], "prompt_ids": [], "started": False, "finished": False, "last_prompt_at": None},
            )
            if st.get("finished"):
                continue
            if now < window_start:
                continue

            guild = bot.get_guild(int(ev["guild_id"]))
            if not guild:
                continue

            confirmed = set(int(x) for x in st.get("confirmed", []))
            participants = _slot_all_mains(ev)
            unanswered = participants - confirmed

            if not st.get("started"):
                st["started"] = True
                st["last_prompt_at"] = None

            # Ende des Fensters: freigeben
                        
            if now >= window_end:
                removed = set()
                promoted: list[tuple[str, int]] = []
                if unanswered:
                    for uid in list(unanswered):
                        _slot_remove_user(ev, uid)
                        removed.add(uid)
                    promoted = _slot_promote_waitlist(ev)
                    active_events[mid_s] = ev
                    await safe_save()
                    try:
                        await update_event_post(guild, int(mid_s))
                    except Exception:
                        pass
            
                st["finished"] = True
                st["confirmed"] = list(confirmed)
                ev["afk_state"] = st
                active_events[mid_s] = ev
                await safe_save()
            
                # Infos in den Event-Thread
                try:
                    if removed:
                        await post_to_event_thread(guild, ev, "🚪 Slots freigegeben: " + ", ".join([f"<@{u}>" for u in removed]))
                    if promoted:
                        await post_to_event_thread(guild, ev, "\n".join([f"➕ Nachgerückt {emo}: <@{uid}>" for emo, uid in promoted]))
                    if not removed:
                        await post_to_event_thread(guild, ev, "✅ AFK-Check vorbei: alle bestätigt.")
                    else:
                        await post_to_event_thread(guild, ev, f"🚪 AFK-Check vorbei: **{len(removed)}** Slot(s) freigegeben.")
                except Exception:
                    pass
                continue
            
            last_prompt_at = st.get("last_prompt_at")
            last_dt = None
            if isinstance(last_prompt_at, str):
                try:
                    last_dt = datetime.fromisoformat(last_prompt_at)
                except Exception:
                    last_dt = None

            if unanswered and (last_dt is None or now - last_dt >= interval):
                for uid in list(unanswered):
                    try:
                        member = guild.get_member(uid) or await guild.fetch_member(uid)
                        dm = await member.create_dm()
                        dm_msg = await dm.send(
                            f"🕵️ **AFK-Check** für **{ev.get('title','(Event)')}**\n"
                            f"Start: **{format_dt_local(dt_utc)}**\n"
                            f"Bitte reagiere mit ✅, wenn du dabei bist."
                        )
                        try:
                            await dm_msg.add_reaction("✅")
                        except Exception:
                            pass
                        afkdmprompts[int(dm_msg.id)] = (int(mid_s), int(uid))
                    except Exception:
                        pass

                st["last_prompt_at"] = now.isoformat()
                ev["afk_state"] = st
                active_events[mid_s] = ev
                await safe_save()

        await asyncio.sleep(10)


async def cleanup_task():
    """Löscht Event-Post + Thread standardmäßig 2h nach Start (wenn nicht deaktiviert)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = _now_utc()
        for mid_s, ev in list(active_events.items()):
            hours = ev.get("auto_delete_hours", AUTO_DELETE_HOURS_DEFAULT)
            if hours is None:
                continue
            try:
                dt_utc = _ensure_utc(datetime.fromisoformat(ev["event_time_utc"]))
            except Exception:
                continue
            if now < dt_utc + timedelta(hours=hours):
                continue

            guild = bot.get_guild(int(ev["guild_id"]))
            if guild:
                # delete thread first
                try:
                    tid = ev.get("thread_id")
                    if tid:
                        th = guild.get_thread(int(tid))
                        if th:
                            await th.delete()
                        else:
                            # try fetch
                            fetched = await bot.fetch_channel(int(tid))
                            if isinstance(fetched, discord.Thread):
                                await fetched.delete()
                except Exception:
                    pass

                # delete message
                try:
                    msg = await fetch_message(guild, int(ev["channel_id"]), int(mid_s))
                    if msg:
                        await msg.delete()
                except Exception:
                    pass

            # remove from store
            try:
                del active_events[mid_s]
                await safe_save()
            except Exception:
                pass

        await asyncio.sleep(60)

@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user} (Guilds: {len(bot.guilds)})")
    try:
        # Schnellere Command-Aktivierung: pro Guild syncen
        for g in bot.guilds:
            try:
                await bot.tree.sync(guild=g)
            except Exception:
                pass
    except Exception:
        pass
    global TASKS_STARTED
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} Slash Commands global synchronisiert")
    except Exception as e:
        print(f"❌ Slash Sync Fehler: {e}")
    print(f"🤖 SlotBot online als {bot.user}")

@bot.listen("on_interaction")
async def _log_interaction(interaction: discord.Interaction):
    """Log interactions without overriding discord.py's default app-command dispatch."""
    try:
        itype = getattr(interaction, "type", None)
        # For slash commands, data.name is the command name
        name = None
        if getattr(interaction, "data", None) and isinstance(interaction.data, dict):
            name = interaction.data.get("name")
        user = getattr(interaction, "user", None) or getattr(interaction, "member", None)
        uname = getattr(user, "name", str(user))
        gid = getattr(getattr(interaction, "guild", None), "id", None)
        print(f"➡️ Interaction: type={itype} name={name} user={uname} guild={gid}")
    except Exception as e:
        print("⚠️ Interaction log error:", repr(e))


if __name__ == "__main__":
    print("🚀 Starte SlotBot (rebuilt) + Flask ...")

    if not DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN fehlt (Environment Variable). Bot bleibt offline, Slash Commands reagieren nicht.")
        # Flask weiterlaufen lassen, damit Render nicht meckert
        threading.Event().wait()
        raise SystemExit(1)

    # Flask (keep-alive / health) in a daemon thread
    threading.Thread(target=run_flask, daemon=True).start()

    async def _run_discord_with_backoff():
        """
        Render (oder ähnliche Hoster) starten den Prozess bei Exit sofort neu.
        Wenn Discord uns wegen global rate limits blockt (HTTP 429), würden wir sonst
        in eine Crash-Loop geraten und die Sperre verlängern.
        """
        backoff = 30          # seconds
        max_backoff = 15 * 60 # 15 minutes

        while True:
            try:
                print("🔐 Discord login...")
                await bot.start(DISCORD_TOKEN)
                # bot.start läuft "für immer" — wenn wir hier rausfallen, wurde gestoppt
                backoff = 30
            except discord.HTTPException as e:
                # Global/Cloudflare block wegen zu vieler Requests (meist durch Restart-Loop)
                status = getattr(e, "status", None)
                if status == 429:
                    print(f"⚠️ Discord 429 (global rate limit). Warte {backoff}s und versuche es erneut ...")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
                    continue
                raise
            except Exception as e:
                print("❌ Bot ist abgestürzt:", repr(e))
                await asyncio.sleep(10)
            finally:
                try:
                    if not bot.is_closed():
                        await bot.close()
                except Exception:
                    pass

    asyncio.run(_run_discord_with_backoff())
