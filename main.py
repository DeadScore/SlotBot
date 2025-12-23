
import os
import re
import io
import json
import asyncio
import base64
from datetime import datetime, timedelta
from threading import Thread
from typing import Dict, Any, List, Tuple

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")


if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN fehlt in den Render Environment Variables.")
import random

import requests
import pytz

import logging


from urllib.parse import quote_plus

import discord
from discord.ext import commands
from discord import app_commands

discord.utils.setup_logging(level=logging.INFO)
from flask import Flask, Response

# ----------------- Konfiguration -----------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("❌ DISCORD_TOKEN nicht gesetzt. Bitte als Environment Variable konfigurieren.")
    raise SystemExit(1)

BERLIN_TZ = pytz.timezone("Europe/Berlin")
CUSTOM_EMOJI_REGEX = r"<a?:\w+:\d+>"

GITHUB_REPO = os.getenv("GITHUB_REPO", "DeadScore/SlotBot")
GITHUB_FILE_PATH = os.getenv("GITHUB_FILE_PATH", "data/events.json")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # optional, für klickbaren ICS-Link

# Nur dieser User darf /test ausführen
OWNER_ID = 404173735130562562

# ----------------- Intents & Bot -----------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True  # für DMs / Member-Fetch

bot = commands.Bot(command_prefix="!", intents=intents)

# In-Memory
active_events: Dict[int, Dict[str, Any]] = {}  # message_id -> event data
SAVE_LOCK = asyncio.Lock()

# Abos & Event-Historie
SUBSCRIPTIONS: Dict[int, Dict[str, List[int]]] = {}  # guild_id -> { "PvE": [user_ids], ... }
EVENT_HISTORY: List[Dict[str, Any]] = []  # einfache Historie für /stats

# AFK-Pending (User müssen bestätigen, sonst werden sie gekickt)
AFK_PENDING: Dict[Tuple[int, int, int], datetime] = {}  # (guild_id, msg_id, user_id) -> deadline

# Roll-Runden: (guild_id, channel_id) -> Session-Daten
ROLL_SESSIONS: Dict[Tuple[int, int], Dict[str, Any]] = {}


async def _post_roll_result(channel: discord.abc.Messageable, guild: discord.Guild, end_at: datetime, rolls: Dict[int, int]):
    """Postet das Ergebnis einer Roll-Runde (Gewinner + Leaderboard) in den Channel."""
    if not rolls:
        await channel.send("⏱️ Die Roll-Runde ist vorbei – niemand hat gewürfelt.")
        return

    max_val = max(rolls.values())
    winners = [uid for uid, val in rolls.items() if val == max_val]

    top_sorted = sorted(rolls.items(), key=lambda kv: kv[1], reverse=True)
    leaderboard_lines = []
    for idx, (uid, val) in enumerate(top_sorted[:15], start=1):
        member = guild.get_member(uid)
        name = member.mention if member else f"<@{uid}>"
        leaderboard_lines.append(f"**#{idx}** {name}: **{val}**")

    win_mentions = []
    for uid in winners:
        member = guild.get_member(uid)
        win_mentions.append(member.mention if member else f"<@{uid}>")

    end_local = end_at.astimezone(BERLIN_TZ).strftime("%H:%M")

    result = discord.Embed(
        title="🏁 Roll-Runde beendet",
        description=(
            f"Ende: **{end_local} Uhr** (Berlin)\n"
            f"Teilnehmer: **{len(rolls)}**\n\n"
            f"🏆 **Gewinner:** {', '.join(win_mentions)}\n"
            f"🎯 **Höchster (Erst-)Wurf:** **{max_val}**"
        ),
        color=discord.Color.green(),
    )
    result.add_field(
        name="📋 Leaderboard (Top 15)",
        value="\n".join(leaderboard_lines),
        inline=False,
    )
    await channel.send(embed=result)



# Background-Tasks (werden in on_ready gestartet und für Health-Checks genutzt)
BACKGROUND_TASKS: Dict[str, asyncio.Task] = {}
TASKS_STARTED = False


COMMANDS_SYNCED = False
# ----------------- Datum/Zeit Hilfen -----------------
WEEKDAY_DE = {
    "Monday": "Montag",
    "Tuesday": "Dienstag",
    "Wednesday": "Mittwoch",
    "Thursday": "Donnerstag",
    "Friday": "Freitag",
    "Saturday": "Samstag",
    "Sunday": "Sonntag",
}

ART_EMOJI = {
    "PvE": "🟢",
    "PvP": "🔴",
    "PVX": "🟣",
}

ART_COLOR = {
    "PvE": discord.Color.green(),
    "PvP": discord.Color.red(),
    "PVX": discord.Color.purple(),
}


def format_de_datetime(local_dt: datetime) -> str:
    en = local_dt.strftime("%A")
    de = WEEKDAY_DE.get(en, en)
    return local_dt.strftime(f"%A, %d.%m.%Y %H:%M %Z").replace(en, de)


def parse_time_tolerant(s: str, fallback_hhmm: str) -> str:
    """Akzeptiert 22, 22 Uhr, 22.15, 22:15 → HH:MM. Fällt sonst auf fallback zurück."""
    if not s:
        return fallback_hhmm
    s = s.strip().lower().replace("uhr", "").strip()
    s = s.replace(".", ":")
    m = re.match(r"^(\d{1,2})(?::?(\d{2}))?$", s)
    if not m:
        return fallback_hhmm
    h = int(m.group(1))
    mnt = int(m.group(2)) if m.group(2) else 0
    if h < 0 or h > 23 or mnt < 0 or mnt > 59:
        return fallback_hhmm
    return f"{h:02d}:{mnt:02d}"


def parse_date_flexible(date_str: str) -> str:
    """Erlaubt 'heute', 'morgen', 'übermorgen' sowie DD.MM, DD.MM.YY, DD.MM.YYYY. Gibt immer DD.MM.YYYY zurück."""
    if not date_str:
        raise ValueError("Datum fehlt")
    s = date_str.strip().lower()
    today = datetime.now(BERLIN_TZ).date()

    if s in ("heute", "today"):
        d = today
    elif s in ("morgen", "tomorrow"):
        d = today + timedelta(days=1)
    elif s in ("übermorgen", "uebermorgen"):
        d = today + timedelta(days=2)
    else:
        d = None
        for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d.%m"):
            try:
                parsed = datetime.strptime(s, fmt)
                if fmt == "%d.%m":
                    d = parsed.replace(year=today.year).date()
                else:
                    d = parsed.date()
                break
            except ValueError:
                pass
        if d is None:
            raise ValueError("Ungültiges Datum")

    return d.strftime("%d.%m.%Y")



def ensure_utc_datetime(dt: datetime) -> datetime:
    """Stellt sicher, dass dt timezone-aware ist (UTC)."""
    if not isinstance(dt, datetime):
        raise ValueError("dt ist kein datetime")
    if dt.tzinfo is None:
        return pytz.utc.localize(dt)
    return dt.astimezone(pytz.utc)



def to_google_dates(start_utc: datetime, duration_hours: int = 2) -> str:
    end_utc = start_utc + timedelta(hours=duration_hours)
    fmt = "%Y%m%dT%H%M%SZ"
    return f"{start_utc.strftime(fmt)}/{end_utc.strftime(fmt)}"


def build_google_calendar_url(title: str, start_utc: datetime, location: str, description: str) -> str:
    base = "https://calendar.google.com/calendar/render?action=TEMPLATE"
    text = "&text=" + quote_plus(title or "")
    dates = "&dates=" + to_google_dates(start_utc)
    loc = "&location=" + quote_plus(location or "")
    details = "&details=" + quote_plus(description or "")
    return base + text + dates + loc + details


def build_ics_content(title: str, start_utc: datetime, duration_hours: int, location: str, description: str):
    dt_start = start_utc.strftime("%Y%m%dT%H%M%SZ")
    dt_end = (start_utc + timedelta(hours=duration_hours)).strftime("%Y%m%dT%H%M%SZ")
    uid = f"{title}-{dt_start}@slotbot"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//SlotBot//v4.6//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{dt_start}",
        f"DTEND:{dt_end}",
        f"SUMMARY:{title}",
        f"LOCATION:{location or ''}",
        "DESCRIPTION:" + (description or "").replace("\n", "\\n"),
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines)


# ----------------- Slots / Emojis -----------------
def normalize_emoji(emoji):
    if isinstance(emoji, str):
        return emoji.strip()
    if hasattr(emoji, "id") and emoji.id:
        return f"<:{emoji.name}:{emoji.id}>"
    return emoji.name


def is_valid_emoji(emoji, guild: discord.Guild):
    if re.match(CUSTOM_EMOJI_REGEX, emoji):
        return any(str(e) == emoji for e in guild.emojis)
    return True


SLOT_PATTERN = re.compile(r"(<a?:\w+:\d+>|[^\s:]+)\s*:\s*(\d+)")


def parse_slots(slots_str: str, guild: discord.Guild):
    matches = SLOT_PATTERN.findall(slots_str or "")
    if not matches:
        return None
    slot_dict: Dict[str, Dict[str, Any]] = {}
    for emoji, limit in matches:
        em = normalize_emoji(emoji)
        if not is_valid_emoji(em, guild):
            return f"Ungültiges Emoji: {em}"
        slot_dict[em] = {
            "limit": int(limit),
            "main": set(),
            "waitlist": [],
            "reminded": set(),      # DM 20 Min vorher
            "afk_dm_sent": set(),   # AFK-Check DM 10 Min vorher
        }
    return slot_dict


def format_event_text(ev: dict, guild: discord.Guild) -> str:
    text = "🎟️ **Slots & Teilnehmer:**\n"
    if not ev["slots"]:
        return text + "\n(Keine Slots definiert.)"
    for emoji, slot in ev["slots"].items():
        main_users = [guild.get_member(uid).mention for uid in slot["main"] if guild.get_member(uid)]
        wait_users = [guild.get_member(uid).mention for uid in slot["waitlist"] if guild.get_member(uid)]
        text += f"\n{emoji} **({len(main_users)}/{slot['limit']})**: "
        text += ", ".join(main_users) if main_users else "-"
        if wait_users:
            text += f"\n   ⏳ **Warteliste:** " + ", ".join(wait_users)
    return text


# ----------------- Strike-Through Utilities -----------------
def extract_current_value(header: str, prefix_regex: str) -> str:
    m = re.search(prefix_regex + r"(.*)$", header, re.M)
    if not m:
        return ""
    val = m.group(1).strip()
    if "~~" in val and "→" in val:
        parts = val.split("→", 1)
        return parts[1].strip()
    return val


def replace_with_struck(header: str, prefix_label: str, old_visible: str, new_value: str) -> str:
    line_regex = re.compile(rf"^{re.escape(prefix_label)} .*?$", re.M)
    if line_regex.search(header):

        def _sub(m):
            line = m.group(0)
            m2 = re.search(r"~~(.*?)~~\s*→\s*(.*)", line)
            if m2:
                current_new = m2.group(2).strip()
                return f"{prefix_label} ~~{current_new}~~ → {new_value}"
            else:
                original = line.replace(prefix_label, "").strip()
                return f"{prefix_label} ~~{original}~~ → {new_value}"

        return line_regex.sub(_sub, header)
    return header.rstrip() + f"\n{prefix_label} ~~{old_visible or '?'}~~ → {new_value}"


async def update_event_message(message_id: int):
    ev = active_events.get(message_id)
    if not ev:
        return

    guild = bot.get_guild(ev["guild_id"])
    if not guild:
        return

    channel = guild.get_channel(ev["channel_id"])
    if channel is None:
        try:
            channel = await guild.fetch_channel(ev["channel_id"])
        except Exception:
            return

    content = ev["header"] + "\n\n" + format_event_text(ev, guild)

    for _ in range(3):
        try:
            msg = await channel.fetch_message(int(message_id))

            # Discord-Limit: 2000 Zeichen
            if len(content) <= 2000:
                await msg.edit(content=content, embed=None)
                return

            # Fallback: Embed
            embed = discord.Embed(
                title=f"📅 Event: {ev.get('title','(ohne Titel)')}",
                description=ev["header"],
                color=0x2ECC71,
            )
            slots_text = format_event_text(ev, guild)
            if len(slots_text) > 1024:
                slots_text = slots_text[:1020] + "…"
            embed.add_field(name="🎟️ Slots", value=slots_text, inline=False)

            await msg.edit(content=None, embed=embed)
            return

        except discord.HTTPException as e:
            print(f"⚠️ update_event_message HTTPException: {e}")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"⚠️ update_event_message Fehler: {e}")
            await asyncio.sleep(1)


# ----------------- GitHub Speicherfunktionen -----------------
def gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}


def put_empty_events(obj):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    encoded_content = base64.b64encode(json.dumps(obj, indent=2).encode()).decode()
    data = {"message": "Initialize events.json", "content": encoded_content}
    try:
        resp = requests.put(url, headers=gh_headers(), json=data, timeout=10)
        if resp.status_code in [200, 201]:
            print("💾 Leere events.json erstellt.")
        else:
            print(f"⚠️ Konnte leere events.json nicht erstellen: HTTP {resp.status_code}")
    except Exception as e:
        print(f"❌ Fehler beim Erstellen der leeren Datei: {e}")


def load_events_once() -> Dict[int, Dict[str, Any]]:
    global SUBSCRIPTIONS, EVENT_HISTORY
    if not GITHUB_TOKEN:
        print("⚠️ GITHUB_TOKEN fehlt – starte ohne Persistenz.")
        SUBSCRIPTIONS = {}
        EVENT_HISTORY = []
        return {}
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    try:
        r = requests.get(url, headers=gh_headers(), timeout=10)
        if r.status_code == 200:
            decoded = base64.b64decode(r.json()["content"]).decode("utf-8")
            raw = json.loads(decoded)
            fixed: Dict[int, Dict[str, Any]] = {}

            # Subscriptions & History extrahieren
            subs_raw = raw.get("_subscriptions")
            if isinstance(subs_raw, dict):
                subs: Dict[int, Dict[str, List[int]]] = {}
                for g_id_str, mapping in subs_raw.items():
                    try:
                        g_id = int(g_id_str)
                    except Exception:
                        continue
                    inner: Dict[str, List[int]] = {}
                    if isinstance(mapping, dict):
                        for art_key, user_list in mapping.items():
                            if isinstance(user_list, list):
                                inner[art_key] = [int(u) for u in user_list]
                    subs[g_id] = inner
                SUBSCRIPTIONS = subs
            else:
                SUBSCRIPTIONS = {}

            hist_raw = raw.get("_history")
            if isinstance(hist_raw, list):
                EVENT_HISTORY = hist_raw
            else:
                EVENT_HISTORY = []
            # Events laden (alles außer den Sonderkeys)
            for k, ev in raw.items():
                if k in ("_subscriptions", "_history"):
                    continue
                for key in ("creator_id", "channel_id", "guild_id", "thread_id"):
                    if key in ev:
                        try:
                            ev[key] = int(ev[key])
                        except Exception:
                            pass
                # Datumswerte
                if isinstance(ev.get("event_time"), str):
                    try:
                        ev["event_time"] = datetime.fromisoformat(ev["event_time"])
                    except Exception:
                        ev["event_time"] = None
                if isinstance(ev.get("delete_at"), str):
                    try:
                        ev["delete_at"] = datetime.fromisoformat(ev["delete_at"])
                    except Exception:
                        ev["delete_at"] = None
                # Slots
                for s in ev.get("slots", {}).values():
                    s["main"] = set(s.get("main", []))
                    s["waitlist"] = list(s.get("waitlist", []))
                    s["reminded"] = set(s.get("reminded", []))
                    s["afk_dm_sent"] = set(s.get("afk_dm_sent", []))
                try:
                    fixed[int(k)] = ev
                except Exception:
                    continue
            return fixed
        elif r.status_code == 404:
            print("ℹ️ Keine events.json gefunden – lege leere Datei an.")
            put_empty_events({})
            SUBSCRIPTIONS = {}
            EVENT_HISTORY = []
            return {}
        else:
            print(f"⚠️ Fehler beim Laden: HTTP {r.status_code}")
    except Exception as e:
        print(f"❌ Fehler beim Laden von events.json: {e}")
    SUBSCRIPTIONS = {}
    EVENT_HISTORY = []
    return {}


def load_events_with_retry(retries=5, delay=1.0) -> Dict[int, Dict[str, Any]]:
    import time
    for i in range(retries):
        data = load_events_once()
        if data:
            print(f"✅ {len(data)} gespeicherte Events von GitHub geladen.")
            return data
        if i < retries - 1:
            time.sleep(delay)
    return {}


def save_events():
    if not GITHUB_TOKEN:
        print("⚠️ GITHUB_TOKEN fehlt – kann events.json nicht speichern.")
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    try:
        get_resp = requests.get(url, headers=gh_headers(), timeout=10)
        sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None

        serializable: Dict[str, Any] = {}
        # Events
        for mid, ev in active_events.items():
            copy: Dict[str, Any] = {}
            for key, value in ev.items():
                if key in ("event_time", "delete_at") and isinstance(value, datetime):
                    copy[key] = value.isoformat()
                elif key == "slots":
                    slots_copy: Dict[str, Any] = {}
                    for emoji, s in value.items():
                        slots_copy[emoji] = {
                            "limit": s["limit"],
                            "main": list(s["main"]),
                            "waitlist": list(s["waitlist"]),
                            "reminded": list(s["reminded"]),
                            "afk_dm_sent": list(s["afk_dm_sent"]),
                        }
                    copy["slots"] = slots_copy
                else:
                    copy[key] = value
            serializable[str(mid)] = copy

        # Subscriptions
        subs_out: Dict[str, Dict[str, List[int]]] = {}
        for g_id, mapping in SUBSCRIPTIONS.items():
            inner: Dict[str, List[int]] = {}
            for art_key, user_list in mapping.items():
                inner[art_key] = list(set(int(u) for u in user_list))
            subs_out[str(g_id)] = inner
        serializable["_subscriptions"] = subs_out

        # Punkte (DKP)

        # History
        serializable["_history"] = EVENT_HISTORY

        encoded_content = base64.b64encode(json.dumps(serializable, indent=4).encode()).decode()
        data = {"message": "Update events.json via SlotBot v4.6", "content": encoded_content}
        if sha:
            data["sha"] = sha
        resp = requests.put(url, headers=gh_headers(), json=data, timeout=10)
        if resp.status_code in [200, 201]:
            print("💾 events.json erfolgreich auf GitHub gespeichert.")
        elif resp.status_code == 404:
            print("ℹ️ events.json fehlt – lege neu an und speichere erneut.")
            put_empty_events(serializable)
        else:
            print(f"⚠️ Fehler beim Speichern auf GitHub: HTTP {resp.status_code}")
    except Exception as e:
        print(f"❌ Fehler beim Speichern: {e}")


async def safe_save():
    async with SAVE_LOCK:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, save_events)


async def try_reload_if_missing(message_id: int):
    """Lädt Events bei Bedarf im Thread-Pool neu, blockiert den Event-Loop nicht."""
    if message_id in active_events:
        return True

    loop = asyncio.get_running_loop()
    fresh = await loop.run_in_executor(None, load_events_with_retry)

    if fresh:
        active_events.clear()
        active_events.update(fresh)
        return message_id in active_events

    return False


def get_latest_user_event(guild_id: int, user_id: int):
    own = [
        (mid, ev)
        for mid, ev in active_events.items()
        if int(ev.get("creator_id", 0)) == user_id and ev.get("guild_id") == guild_id
    ]
    if not own:
        return None
    msg_id, ev = max(own, key=lambda x: x[1].get("event_time", datetime.min.replace(tzinfo=pytz.utc)))
    return msg_id, ev



def can_edit_event(interaction: discord.Interaction, ev: dict) -> bool:
    """Ersteller darf immer; Admin/Manage_Guild dürfen alles."""
    if interaction.user.id == int(ev.get("creator_id", 0)):
        return True
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild

# ----------------- Reminder & Cleanup & AFK -----------------
async def reminder_task():
    """Reminder 20 Min vorher + AFK-Check 10 Min vorher (DM)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(pytz.utc)
        changed = False  # ob wir reminded/afk flags geändert haben (für Persistenz)
        for msg_id, ev in list(active_events.items()):
            guild = bot.get_guild(ev["guild_id"])
            if not guild:
                continue
            event_time = ev.get("event_time")
            if not event_time:
                continue
            try:
                event_time = ensure_utc_datetime(event_time)
            except Exception:
                continue

            for emoji, slot in ev["slots"].items():
                # Robust: immer als Set führen (nach Load sollte es schon Set sein, aber sicher ist sicher)
                r = slot.get("reminded", set())
                if not isinstance(r, set):
                    r = set(r)
                slot["reminded"] = r

                a = slot.get("afk_dm_sent", set())
                if not isinstance(a, set):
                    a = set(a)
                slot["afk_dm_sent"] = a

                for user_id in list(slot["main"]):
                    try:
                        seconds_left = (event_time - now).total_seconds()
                    except Exception as e:
                        print(f"⚠️ reminder_task Zeitfehler (event={msg_id}): {e}")
                        continue

                    # 20-Min-Reminder
                    if 0 <= seconds_left <= 20 * 60 and user_id not in slot["reminded"]:
                        try:
                            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
                            await member.send(
                                f"⏰ Dein Event **{ev['title']}** startet in **20 Minuten**! "
                                f"Bitte sei rechtzeitig online."
                            )
                            slot["reminded"].add(user_id)
                            changed = True
                        except Exception as e:
                            print(f"⚠️ Reminder-DM fehlgeschlagen (user={user_id}, guild={guild.id}): {e}")

                    # 10-Min-AFK-Check
                    if 0 <= seconds_left <= 10 * 60 and user_id not in slot["afk_dm_sent"]:
                        try:
                            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
                            await member.send(
                                f"👀 AFK-Check für **{ev['title']}** in {guild.name}:\\n"
                                f"Das Event startet in **10 Minuten**.\\n"
                                f"Bitte antworte in den nächsten **5 Minuten** mit `bin da`, "
                                f"sonst wirst du automatisch aus dem Slot entfernt."
                            )
                            slot["afk_dm_sent"].add(user_id)
                            AFK_PENDING[(guild.id, int(msg_id), user_id)] = now + timedelta(minutes=5)
                            changed = True
                        except Exception as e:
                            print(f"⚠️ AFK-DM fehlgeschlagen (user={user_id}, guild={guild.id}): {e}")

        # Wichtig: Flags speichern, damit ein Restart nicht wieder Reminder spammt
        if changed:
            try:
                save_events()
            except Exception as e:
                print(f"⚠️ save_events nach Reminder fehlgeschlagen: {e}")

        await asyncio.sleep(60)



async def afk_enforcer_task():
    """Entfernt Spieler aus Slots, wenn sie beim AFK-Check nicht antworten."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(pytz.utc)
        expired = [(k, dl) for k, dl in list(AFK_PENDING.items()) if dl <= now]
        for (guild_id, msg_id, user_id), _deadline in expired:
            AFK_PENDING.pop((guild_id, msg_id, user_id), None)
            ev = active_events.get(int(msg_id))
            if not ev:
                continue

            guild = bot.get_guild(guild_id)
            if not guild:
                continue

            changed = False
            # user aus allen main slots entfernen
            for _emoji, slot in ev.get("slots", {}).items():
                mains = slot.get("main", set())
                if not isinstance(mains, set):
                    mains = set(mains)
                if user_id in mains:
                    mains.remove(user_id)
                    slot["main"] = mains
                    changed = True

            if changed:
                active_events[int(msg_id)] = ev
                try:
                    save_events()
                except Exception as e:
                    print(f"⚠️ save_events (AFK Enforcer) fehlgeschlagen: {e}")

                # Event-Post aktualisieren
                try:
                    await update_event_message(int(msg_id))
                except Exception as e:
                    print(f"⚠️ update_event_message (AFK Enforcer) fehlgeschlagen: {e}")

                # optional: Channel Info
                try:
                    ch = guild.get_channel(ev.get("channel_id"))
                    if ch:
                        await ch.send(f"🚪 <@{user_id}> wurde wegen AFK aus dem Event entfernt.")
                except Exception:
                    pass

        await asyncio.sleep(10)

async def cleanup_task():
    """Löscht abgelaufene Events nach delete_at."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(pytz.utc)
        removed_any = False

        for msg_id, ev in list(active_events.items()):
            delete_at = ev.get("delete_at")
            if not delete_at:
                continue
            try:
                delete_at = ensure_utc_datetime(delete_at)
            except Exception:
                continue

            if now < delete_at:
                continue

            guild = bot.get_guild(ev.get("guild_id"))
            if not guild:
                # trotzdem entfernen, sonst bleibt Müll liegen
                active_events.pop(int(msg_id), None)
                removed_any = True
                continue

            # Message löschen
            try:
                channel = guild.get_channel(ev.get("channel_id")) or await guild.fetch_channel(ev.get("channel_id"))
                try:
                    msg = await channel.fetch_message(int(msg_id))
                    await msg.delete()
                except Exception:
                    pass
            except Exception:
                pass

            # Thread löschen, falls vorhanden
            try:
                thread_id = ev.get("thread_id")
                if thread_id:
                    th = guild.get_thread(int(thread_id))
                    if not th:
                        th = await bot.fetch_channel(int(thread_id))
                    if th:
                        await th.delete()
            except Exception:
                pass

            active_events.pop(int(msg_id), None)
            removed_any = True

        if removed_any:
            try:
                save_events()
            except Exception as e:
                print(f"⚠️ save_events (cleanup) fehlgeschlagen: {e}")

        await asyncio.sleep(60)


async def watchdog_task():
    """Restartet Background-Tasks falls sie abstürzen (ohne auf on_ready zu warten)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for key, factory in [
                ("reminder", reminder_task),
                ("afk_enforcer", afk_enforcer_task),
                ("cleanup", cleanup_task),
            ]:
                task = BACKGROUND_TASKS.get(key)
                if not task or task.done() or task.cancelled():
                    BACKGROUND_TASKS[key] = bot.loop.create_task(factory(), name=f"slotbot_{key}_task")
        except Exception as e:
            print(f"⚠️ watchdog_task Fehler: {e}")
        await asyncio.sleep(30)

# ----------------- Thread Helper & Logging -----------------
async def get_or_restore_thread(ev: dict, guild: discord.Guild, base_message_id: int):
    thread = None
    thread_id = ev.get("thread_id")
    if thread_id:
        try:
            thread = guild.get_channel(thread_id) or await guild.fetch_channel(thread_id)
        except Exception:
            thread = None
    if thread and getattr(thread, "archived", False):
        try:
            await thread.edit(archived=False)
        except Exception:
            pass
    if thread is None:
        channel = guild.get_channel(ev["channel_id"])
        if channel is None:
            return None
        try:
            base_msg = await channel.fetch_message(base_message_id)
            thread = await base_msg.create_thread(
                name=f"Event-Log (neu): {ev['title']}",
                auto_archive_duration=1440,
            )
            ev["thread_id"] = thread.id
            await safe_save()
        except Exception as e:
            print(f"⚠️ Konnte keinen Thread erstellen: {e}")
            return None
    return thread


async def post_event_update_log(ev: dict, guild: discord.Guild, editor_mention: str, changes_text: str, base_message_id: int):
    thread = await get_or_restore_thread(ev, guild, base_message_id)
    if not thread:
        return
    try:
        await thread.send(f"✏️ **{editor_mention}** hat das Event bearbeitet ({changes_text}).")
    except Exception:
        pass


async def post_calendar_links(ev: dict, guild: discord.Guild, base_message_id: int):
    thread = await get_or_restore_thread(ev, guild, base_message_id)
    if not thread:
        return

    title = ev["title"]
    event_time_utc = ev["event_time"]
    header = ev["header"]

    m_ort = re.search(r"^📍 \*\*Ort:\*\* (.+)$", header, re.M)
    m_level = re.search(r"^⚔️ \*\*Levelbereich:\*\* (.+)$", header, re.M)
    m_stil = re.search(r"^💬 \*\*Stil:\*\* (.+)$", header, re.M)
    m_typ = re.search(r"^🏷️ \*\*Typ:\*\* (.+)$", header, re.M)
    m_lead = re.search(r"^👑 \*\*Gruppenlead:\*\* (.+)$", header, re.M)
    m_note = re.search(r"^📝 \*\*Anmerkung:\*\* (.+)$", header, re.M)

    ort = m_ort.group(1) if m_ort else ""
    description_parts: List[str] = []
    if m_level:
        description_parts.append(f"Level: {m_level.group(1)}")
    if m_stil:
        description_parts.append(f"Stil: {m_stil.group(1)}")
    if m_typ:
        description_parts.append(f"Typ: {m_typ.group(1)}")
    if m_lead:
        description_parts.append(f"Gruppenlead: {m_lead.group(1)}")
    if m_note:
        description_parts.append(f"Anmerkung: {m_note.group(1)}")
    desc_text = "\n".join(description_parts)

    g_link = build_google_calendar_url(title, event_time_utc, ort, desc_text)
    ics_text = build_ics_content(title, event_time_utc, 2, ort, desc_text)

    if PUBLIC_BASE_URL:
        url = f"{PUBLIC_BASE_URL.rstrip('/')}/ics/{base_message_id}.ics"
        await thread.send(f"📅 Kalender: [Google öffnen]({g_link})  |  [Apple (.ics)]({url})")
    else:
        try:
            fp = io.BytesIO(ics_text.encode("utf-8"))
            file = discord.File(fp, filename=f"event_{base_message_id}.ics")
            await thread.send(
                content=f"📅 Kalender: [Google öffnen]({g_link})  |  Apple: .ics angehängt",
                file=file,
            )
        except Exception:
            await thread.send(f"📅 Kalender: [Google öffnen]({g_link})")


async def log_participation_change(
    ev: dict,
    guild: discord.Guild,
    base_message_id: int,
    user_id: int,
    emoji: str,
    action: str,
    slot_type: str = "",
):
    thread = await get_or_restore_thread(ev, guild, base_message_id)
    if not thread:
        return

    member = guild.get_member(user_id)
    user_mention = member.mention if member else f"<@{user_id}>"

    if action == "join":
        if slot_type:
            text = f"✅ {user_mention} hat sich mit {emoji} angemeldet ({slot_type})."
        else:
            text = f"✅ {user_mention} hat sich mit {emoji} angemeldet."
    elif action == "leave":
        if slot_type:
            text = f"❌ {user_mention} hat sich abgemeldet ({slot_type})."
        else:
            text = f"❌ {user_mention} hat sich abgemeldet."
    else:
        return

    try:
        await thread.send(text)
    except Exception:
        pass


# ----------------- /help -----------------
@bot.tree.command(name="help", description="Zeigt eine ausführliche Erklärung aller Befehle an")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 SlotBot v4.6 – Hilfe",
        description=(
            "Der SlotBot hilft dir, Events mit Slots zu erstellen und zu verwalten.\n"
            "Hier ein Überblick über die Befehle."
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="🆕 /event",
        value=(
            "**Erstellt ein neues Event mit Slots & Thread.**\n"
            "Pflicht: `art`, `zweck`, `ort`, `datum`, `zeit`, `level`, `stil`, `slots`\n"
            "Optional: `typ`, `gruppenlead`, `treffpunkt`, `anmerkung`, `auto_delete_stunden` (Default 1h)\n"
            "Beispiel:\n"
            '`/event art:PvE zweck:"XP Farmen" ort:"Calpheon" treffpunkt:"Vor dem Stall" datum:heute zeit:20:00`\noder: `/event ... datum:morgen zeit:21`\noder klassisch: `/event ... datum:27.10.2025 zeit:20:00`\n'
            '`level:61+ stil:"Organisiert" slots:"⚔️:3 🛡️:1 💉:2" auto_delete_stunden:3`\n'
            "• 20-Minuten-Reminder per DM\n"
            "• 10-Minuten-AFK-Check per DM (Auto-Kick bei Nicht-Reaktion)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎲 /roll & /start_roll",
        value=(
            "`/start_roll dauer:60` – Startet eine Roll-Runde im Channel.\n"
            "`/roll` – Würfelt 1–100 (Embed).\n"
            "Pro Spieler zählt nur der **erste** Wurf in der Runde."
        ),
        inline=False,
    )

    embed.add_field(
        name="✏️ /event_edit",
        value=(
            "Bearbeitet **dein** aktuelles Event (Datum, Zeit, Ort, Level, Anmerkung, Slots).\n"
            "Zeit-Eingaben wie `22`, `22.15`, `22:15` oder `22 Uhr` sind erlaubt.\n"
            "Datum/Zeit werden im Event mit `~~alt~~ → neu` markiert."
        ),
        inline=False,
    )
    embed.add_field(
        name="🗑️ /event_delete",
        value="Löscht dein aktuelles Event (nur Ersteller).",
        inline=False,
    )
    embed.add_field(
        name="🗓️ /events",
        value="Listet alle aktiven Events auf dem Server (Serverweit).",
        inline=False,
    )
    embed.add_field(
        name="ℹ️ /event_info",
        value="Zeigt Details & Slots zu deinem aktuellen Event als Embed.",
        inline=False,
    )
    embed.add_field(
        name="📩 /subscribe & /unsubscribe",
        value=(
            "Verwalte Benachrichtigungen für neue Events.\n"
            "`/subscribe art:PvE` – DM bei neuen PvE-Events\n"
            "`/subscribe art:PVX` – DM bei neuen PVX-Events\n"
            "`/unsubscribe art:PvE` – PvE-DMs wieder abbestellen\n"
            "`/subscribe art:Alle` – Alle Arten abonnieren"
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 /stats",
        value="Zeigt Event-Statistiken für diesen Server (Anzahl Events, Zeiten, Teilnahme-Trends).",
        inline=False,
    )
    embed.add_field(
        name="🧪 /test",
        value="Führt einen Selbsttest (GitHub, Persistenz, Rechte, Posting) aus. Nur vom Bot-Owner nutzbar.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ----------------- /roll -----------------
@bot.tree.command(name="roll", description="Würfelt eine Zahl zwischen 1 und 100")
async def roll_command(interaction: discord.Interaction):
    rolled_value = random.randint(1, 100)

    embed = discord.Embed(
        title="🎲 Wurf",
        description=f"{interaction.user.mention} würfelt eine Zahl zwischen **1** und **100**.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Ergebnis", value=f"🎯 **{rolled_value}**", inline=False)

    session_info = None
    already_rolled = False
    counted_value = None

    if interaction.guild is not None:
        key = (interaction.guild.id, interaction.channel.id)
        now = datetime.now(pytz.utc)
        session = ROLL_SESSIONS.get(key)
        if session and session["end_time"] >= now:
            session_info = session
            if interaction.user.id not in session["rolls"]:
                session["rolls"][interaction.user.id] = rolled_value  # erster Wurf zählt
                counted_value = rolled_value
            else:
                already_rolled = True
                counted_value = session["rolls"][interaction.user.id]

    if session_info:
        rest = int((session_info["end_time"] - datetime.now(pytz.utc)).total_seconds())
        if rest < 0:
            rest = 0

        if already_rolled:
            embed.add_field(
                name="⚠️ Hinweis",
                value=(
                    f"Du hast in dieser Runde schon gewürfelt.\n"
                    f"Gezählt wird **nur dein erster Wurf**: **{counted_value}**\n"
                    f"➡️ Dieser neue Wurf zählt **nicht**."
                ),
                inline=False,
            )
            embed.set_footer(text=f"Roll-Runde aktiv – noch ca. {rest} Sekunden.")
        else:
            embed.set_footer(text=f"Roll-Runde aktiv – nur dein erster Wurf zählt. Noch ca. {rest} Sekunden.")
    else:
        embed.set_footer(text="Keine Roll-Runde aktiv. Starte eine mit /start_roll.")

    await interaction.response.send_message(embed=embed, ephemeral=False)


# ----------------- /start_roll -----------------
async def event_edit_event_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete: zeigt Events auf dem Server – für Ersteller oder Admins."""
    if interaction.guild is None:
        return []

    guild_id = interaction.guild.id
    user_id = interaction.user.id
    perms = interaction.user.guild_permissions
    is_admin = perms.administrator or perms.manage_guild

    items: List[app_commands.Choice[str]] = []
    for mid, ev in active_events.items():
        if ev.get("guild_id") != guild_id:
            continue
        if not is_admin and int(ev.get("creator_id", 0)) != user_id:
            continue

        title = str(ev.get("title", "Event"))
        if current and current.lower() not in title.lower():
            continue

        when = ""
        try:
            t = ev.get("event_time")
            if isinstance(t, datetime):
                when = format_de_datetime(ensure_utc_datetime(t).astimezone(BERLIN_TZ))
        except Exception:
            when = ""

        ch_name = ""
        try:
            ch = interaction.guild.get_channel(ev.get("channel_id"))
            ch_name = f" #{ch.name}" if ch else ""
        except Exception:
            ch_name = ""

        label = f"{title} — {when}{ch_name}".strip()
        if len(label) > 100:
            label = label[:97] + "…"

        items.append(app_commands.Choice(name=label, value=str(mid)))

    def _key(choice: app_commands.Choice[str]):
        try:
            ev2 = active_events.get(int(choice.value))
            t = ev2.get("event_time")
            if isinstance(t, datetime):
                return ensure_utc_datetime(t)
        except Exception:
            pass
        return datetime.max.replace(tzinfo=pytz.utc)

    items.sort(key=_key)
    return items[:25]


@app_commands.describe(
    dauer="Dauer der Roll-Runde in Sekunden (z. B. 60) – optional, wenn du stattdessen eine Uhrzeit nutzt",
    uhrzeit="End-Uhrzeit (HH:MM oder z. B. 21, 21:30, 21.30, 21 Uhr) – optional, wenn du stattdessen Dauer nutzt",
)
@bot.tree.command(name="start_roll", description="Startet eine Roll-Runde (1–100) im aktuellen Channel")
async def start_roll_command(
    interaction: discord.Interaction,
    dauer: app_commands.Range[int, 5, 21600] = None,  # bis 6h
    uhrzeit: str = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Dieser Befehl kann nur auf einem Server benutzt werden.",
            ephemeral=True,
        )
        return

    # Genau eins von beiden muss gesetzt sein
    if (dauer is None and not uhrzeit) or (dauer is not None and uhrzeit):
        await interaction.response.send_message(
            "❌ Bitte gib **entweder** `dauer` **oder** `uhrzeit` an (nicht beides).\n"
            "Beispiele:\n"
            "• `/start_roll dauer:60`\n"
            "• `/start_roll uhrzeit:21:30`",
            ephemeral=True,
        )
        return

    key = (interaction.guild.id, interaction.channel.id)
    now_utc = datetime.now(pytz.utc)
    existing = ROLL_SESSIONS.get(key)

    if existing and existing["end_time"] > now_utc:
        rest = int((existing["end_time"] - now_utc).total_seconds())
        await interaction.response.send_message(
            f"⚠️ In diesem Channel läuft bereits eine Roll-Runde (noch ca. {rest} Sekunden).\n"
            f"Benutze `/roll`, um mitzumachen.",
            ephemeral=True,
        )
        return

    # Endzeit berechnen
    if uhrzeit:
        hhmm = parse_time_tolerant(uhrzeit, "20:00")
        local_now = datetime.now(pytz.utc).astimezone(BERLIN_TZ)

        target_local = BERLIN_TZ.localize(
            datetime.strptime(f"{local_now.strftime('%d.%m.%Y')} {hhmm}", "%d.%m.%Y %H:%M")
        )
        # Wenn schon vorbei: auf morgen schieben
        if target_local <= local_now:
            target_local = target_local + timedelta(days=1)

        end_time = target_local.astimezone(pytz.utc)
        dauer_calc = int((end_time - now_utc).total_seconds())

        if dauer_calc < 5:
            dauer_calc = 5

        # Hard cap gegen "versehentlich morgen Abend"
        if dauer_calc > 21600:
            await interaction.response.send_message(
                "❌ Die angegebene `uhrzeit` liegt zu weit in der Zukunft (max. 6 Stunden).",
                ephemeral=True,
            )
            return

        dauer = dauer_calc
    else:
        end_time = now_utc + timedelta(seconds=int(dauer))

    ROLL_SESSIONS[key] = {
        "end_time": end_time,
        "rolls": {},  # user_id -> erster (gezählter) Wurf
        "starter_id": interaction.user.id,
        "duration": int(dauer),
        "task": None,
    }

    end_local = end_time.astimezone(BERLIN_TZ)
    end_text = end_local.strftime("%H:%M")

    embed = discord.Embed(
        title="🎲 Roll-Runde gestartet",
        description=(
            f"{interaction.user.mention} hat eine Roll-Runde gestartet!\n\n"
            f"• Zahlbereich: **1–100**\n"
            f"• Läuft bis: **{end_text} Uhr** (Berlin)\n"
f"• Channel: {interaction.channel.mention}\n\n"
            f"Benutze `/roll`, um teilzunehmen.\n"
            f"Pro Spieler zählt nur der **erste** Wurf."
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Nur Würfe während der Zeit zählen. Pro Spieler gilt nur der erste Wurf.")

    await interaction.response.send_message(embed=embed, ephemeral=False)

    async def finish_roll_session(guild_id: int, channel_id: int, end_at: datetime, duration_seconds: int):
        await asyncio.sleep(duration_seconds)

        guild = bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return

        key_local = (guild_id, channel_id)
        session = ROLL_SESSIONS.get(key_local)

        now2 = datetime.now(pytz.utc)
        if not session or session["end_time"] != end_at or now2 < end_at:
            return

        rolls = session["rolls"]
        ROLL_SESSIONS.pop(key_local, None)

        if not rolls:
            await channel.send("⏱️ Die Roll-Runde ist vorbei – niemand hat gewürfelt.")
            return

        max_val = max(rolls.values())
        winners = [uid for uid, val in rolls.items() if val == max_val]

        top_sorted = sorted(rolls.items(), key=lambda kv: kv[1], reverse=True)
        leaderboard_lines = []
        for idx, (uid, val) in enumerate(top_sorted[:15], start=1):
            member = guild.get_member(uid)
            name = member.mention if member else f"<@{uid}>"
            leaderboard_lines.append(f"**#{idx}** {name}: **{val}**")

        win_mentions = []
        for uid in winners:
            member = guild.get_member(uid)
            win_mentions.append(member.mention if member else f"<@{uid}>")

        end_local2 = end_at.astimezone(BERLIN_TZ).strftime("%H:%M")

        result = discord.Embed(
            title="🏁 Roll-Runde beendet",
            description=(
                f"Ende: **{end_local2} Uhr** (Berlin)\n"
                f"Teilnehmer: **{len(rolls)}**\n\n"
                f"🏆 **Gewinner:** {', '.join(win_mentions)}\n"
                f"🎯 **Höchster (Erst-)Wurf:** **{max_val}**"
            ),
            color=discord.Color.green(),
        )
        result.add_field(
            name="📋 Leaderboard (Top 15)",
            value="\n".join(leaderboard_lines),
            inline=False,
        )
        await channel.send(embed=result)
    task = asyncio.create_task(
        finish_roll_session(interaction.guild.id, interaction.channel.id, end_time, int(dauer))
    )
    ROLL_SESSIONS[key]["task"] = task



# ----------------- /stop_roll -----------------
@bot.tree.command(name="stop_roll", description="Beendet die aktuelle Roll-Runde in diesem Channel (Starter oder Admin)")
async def stop_roll_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Dieser Befehl kann nur auf einem Server benutzt werden.", ephemeral=True)
        return

    key = (interaction.guild.id, interaction.channel.id)
    session = ROLL_SESSIONS.get(key)
    if not session:
        await interaction.response.send_message("ℹ️ In diesem Channel läuft aktuell keine Roll-Runde.", ephemeral=True)
        return

    perms = interaction.user.guild_permissions
    is_admin = perms.administrator or perms.manage_guild
    if interaction.user.id != session.get("starter_id") and not is_admin:
        await interaction.response.send_message(
            "❌ Du darfst diese Roll-Runde nicht beenden (nur der Starter oder Admins).",
            ephemeral=True,
        )
        return

    task = session.get("task")
    if task and not task.done() and not task.cancelled():
        task.cancel()

    rolls = session.get("rolls", {})
    end_at = datetime.now(pytz.utc)
    ROLL_SESSIONS.pop(key, None)

    await interaction.response.send_message("🛑 Roll-Runde beendet. Ergebnis wird gepostet.", ephemeral=True)
    await _post_roll_result(interaction.channel, interaction.guild, end_at, rolls)


# ----------------- /event -----------------
@app_commands.describe(
    art="Art des Events (PvE/PvP/PVX)",
    zweck="Zweck (z. B. EP Farmen)",
    ort="Ort (z. B. Carphin)",
    zeit="Zeit (z. B. 20:00, 20, 20 Uhr)",
    datum="Datum im Format DD.MM.YYYY",
    level="Levelbereich",
    stil="Gemütlich oder Organisiert",
    slots="Slots (z. B. ⚔️:2 🛡️:1)",
    typ="Optional: Gruppe oder Raid",
    gruppenlead="Optional: Gruppenleiter",
    anmerkung="Optional: Freitext",
    auto_delete_stunden="Nach wie vielen Stunden nach Eventstart das Event automatisch gelöscht werden soll (Standard: 1)",
)
@app_commands.choices(
    art=[app_commands.Choice(name=x, value=x) for x in ["PvE", "PvP", "PVX"]],
    stil=[app_commands.Choice(name=x, value=x) for x in ["Gemütlich", "Organisiert"]],
    typ=[app_commands.Choice(name=x, value=x) for x in ["Gruppe", "Raid"]],
)
@bot.tree.command(name="event", description="Erstellt ein Event mit Slots & Thread")
async def event(
    interaction: discord.Interaction,
    art: app_commands.Choice[str],
    zweck: str,
    ort: str,
    zeit: str,
    datum: str,
    level: str,
    stil: app_commands.Choice[str],
    slots: str,
    typ: app_commands.Choice[str] = None,
    gruppenlead: str = None,
    treffpunkt: str = None,
    anmerkung: str = None,
    auto_delete_stunden: app_commands.Range[int, 1, 168] = 1,
):
    # Datum/Zeit prüfen
    try:
        local_date = datetime.strptime(parse_date_flexible(datum), "%d.%m.%Y")
        local_date = BERLIN_TZ.localize(local_date)
    except Exception:
        await interaction.response.send_message("❌ Ungültiges Datum! Beispiele: `heute`, `morgen`, `übermorgen`, `27.12`, `27.12.25`, `27.12.2025`", ephemeral=True)
        return

    time_str = parse_time_tolerant(zeit, "20:00")
    try:
        local_dt = BERLIN_TZ.localize(
            datetime.strptime(f"{parse_date_flexible(datum)} {time_str}", "%d.%m.%Y %H:%M")
        )
    except Exception:
        await interaction.response.send_message(
            "❌ Ungültige Zeit! Nutze z. B. 20:00, 20, 20.15, 20 Uhr",
            ephemeral=True,
        )
        return

    utc_dt = local_dt.astimezone(pytz.utc)
    if utc_dt < datetime.now(pytz.utc):
        await interaction.response.send_message("❌ Datum/Zeit liegt in der Vergangenheit!", ephemeral=True)
        return

    # Slots parsen
    slot_dict = parse_slots(slots, interaction.guild)
    if slot_dict is None:
        await interaction.response.send_message("❌ Keine gültigen Slots gefunden.", ephemeral=True)
        return
    if isinstance(slot_dict, str):
        await interaction.response.send_message(f"❌ {slot_dict}", ephemeral=True)
        return

    # Header bauen
    time_str_long = format_de_datetime(local_dt)
    art_emoji = ART_EMOJI.get(art.value, "🗡️")
    sep = "─────────────────────────────────"

    header_lines = [
        f"{art_emoji} **{art.value} – Neue Gruppensuche!**",
        sep,
        f"🎯 **Zweck:** {zweck}",
        f"📍 **Ort:** {ort}",
        *([f"📌 **Treffpunkt:** {treffpunkt}"] if treffpunkt else []),
        f"🕒 **Datum/Zeit:** {time_str_long}",
        f"⚔️ **Levelbereich:** {level}",
        f"💬 **Stil:** {stil.value}",
    ]
    if typ:
        header_lines.append(f"🏷️ **Typ:** {typ.value}")
    if gruppenlead:
        header_lines.append(f"👑 **Gruppenlead:** {gruppenlead}")
    if anmerkung:
        header_lines.append(f"📝 **Anmerkung:** {anmerkung}")
    header_lines.append(sep)
    header = "\n".join(header_lines)

    # Ephemere Bestätigung
    color = ART_COLOR.get(art.value, discord.Color.blue())
    confirm = discord.Embed(
        title="✅ Event erstellt",
        description=f"{art_emoji} **{zweck}**",
        color=color,
    )
    confirm.add_field(name="📍 Ort", value=ort, inline=True)
    confirm.add_field(name="🕒 Start", value=time_str_long, inline=True)
    confirm.add_field(name="⚔️ Level", value=level, inline=True)
    confirm.add_field(name="⏱️ Auto-Löschung", value=f"{auto_delete_stunden}h nach Start", inline=True)
    await interaction.response.send_message(embed=confirm, ephemeral=True)

    # Nachricht im Channel
    try:
        msg = await interaction.channel.send(
            header + "\n\n" + format_event_text({"slots": slot_dict}, interaction.guild)
        )
    except discord.errors.Forbidden:
        await interaction.followup.send("❌ Ich darf hier keine Nachrichten senden.", ephemeral=True)
        return
    except discord.errors.HTTPException as e:
        await interaction.followup.send(f"❌ Fehler beim Erstellen des Events: {e}", ephemeral=True)
        return

    # Reaktionen
    failed_emojis = []
    for e in slot_dict.keys():
        try:
            await msg.add_reaction(e)
        except Exception:
            failed_emojis.append(e)

    # Thread
    thread_id = None
    try:
        thread = await msg.create_thread(
            name=f"Event-Log: {zweck} {datum} {time_str}",
            auto_archive_duration=1440,
        )
        await thread.send(f"🧵 Event-Log für: {zweck} — {msg.jump_url}")
        thread_id = thread.id
        if failed_emojis:
            await thread.send("⚠️ Einige Emojis konnten nicht hinzugefügt werden: " + ", ".join(failed_emojis))
        await post_calendar_links(
            {
                "title": zweck,
                "event_time": utc_dt,
                "header": header,
                "thread_id": thread_id,
                "channel_id": interaction.channel.id,
            },
            interaction.guild,
            msg.id,
        )
    except Exception as e:
        print(f"⚠️ Thread konnte nicht erstellt werden: {e}")

    delete_at = utc_dt + timedelta(hours=int(auto_delete_stunden))

    # Event in Memory
    active_events[msg.id] = {
        "title": zweck,
        "slots": slot_dict,
        "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id,
        "header": header,
        "creator_id": interaction.user.id,
        "event_time": utc_dt,
        "thread_id": thread_id,
        "auto_delete_stunden": int(auto_delete_stunden),
        "delete_at": delete_at,
        "art": art.value,
    }

    # History-Eintrag
    EVENT_HISTORY.append(
        {
            "guild_id": interaction.guild.id,
            "creator_id": interaction.user.id,
            "title": zweck,
            "art": art.value,
            "created_at": datetime.utcnow().isoformat(),
            "event_time": utc_dt.isoformat(),
        }
    )

    await safe_save()

    # Abonnenten benachrichtigen
    guild_subs = SUBSCRIPTIONS.get(interaction.guild.id, {})
    art_subs = set(guild_subs.get(art.value, []))
    if art_subs:
        for uid in list(art_subs):
            try:
                member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
                if member:
                    await member.send(
                        f"📢 Neues **{art.value}**-Event auf {interaction.guild.name}:\n"
                        f"**{zweck}** am {time_str_long} in {ort}\n"
                        f"Channel: {interaction.channel.mention} — [Zum Event]({msg.jump_url})"
                    )
            except Exception:
                pass


# ----------------- /event_edit -----------------
@app_commands.describe(
    datum="Neues Datum (DD.MM.YYYY)",
    zeit="Neue Zeit (z. B. 22, 22.15, 22:15, 22 Uhr)",
    ort="Neuer Ort",
    treffpunkt="Neuer Treffpunkt (optional)",
    level="Neuer Levelbereich",
    anmerkung="Neue Anmerkung",
    slots="Neue Slots (z. B. ⚔️:3 🛡️:2)",
    event="Optional: Event auswählen (nur eigene Events; Admins sehen alle)",
)
@bot.tree.command(name="event_edit", description="Bearbeite dein Event (Datum, Zeit, Ort, Level, Slots, Anmerkung)")
@app_commands.autocomplete(event=event_edit_event_autocomplete)
async def event_edit(
    interaction: discord.Interaction,
    event: str = None,
    datum: str = None,
    zeit: str = None,
    ort: str = None,
    treffpunkt: str = None,
    level: str = None,
    anmerkung: str = None,
    slots: str = None,
):
    # Event auswählen (Autocomplete liefert message_id als String in `event`)
    msg_id = None
    ev = None

    if event:
        try:
            msg_id = int(re.search(r"(\d{15,21})", str(event)).group(1))
        except Exception:
            msg_id = None

        if msg_id:
            ev = active_events.get(msg_id)
            if not ev or ev.get("guild_id") != interaction.guild.id:
                await interaction.response.send_message(
                    "❌ Event nicht gefunden.",
                    ephemeral=True,
                )
                return
            if not can_edit_event(interaction, ev):
                await interaction.response.send_message(
                    "❌ Du darfst dieses Event nicht bearbeiten (nur Ersteller oder Admins).",
                    ephemeral=True,
                )
                return
    else:
        found = get_latest_user_event(interaction.guild.id, interaction.user.id)
        if not found:
            await interaction.response.send_message(
                "❌ Ich finde aktuell kein Event von dir auf diesem Server.",
                ephemeral=True,
            )
            return
        msg_id, ev = found

        thread_changes = []

        PREFIX_DATE = "🕒 **Datum/Zeit:**"
        PREFIX_ORG = "📍 **Ort:**"
        PREFIX_LEVEL = "⚔️ **Levelbereich:**"

        old_event_time = ev["event_time"]

    # Datum/Zeit
    if datum or zeit:
        old_local = old_event_time.astimezone(BERLIN_TZ)
        try:
            fallback_time = old_local.strftime("%H:%M")
            time_str = parse_time_tolerant(zeit, fallback_time) if zeit else fallback_time
            new_local = BERLIN_TZ.localize(
                datetime.strptime(
                    f"{(parse_date_flexible(datum) if datum else old_local.strftime('%d.%m.%Y'))} {time_str}",
                    "%d.%m.%Y %H:%M",
                )
            )
            new_str = format_de_datetime(new_local)
            current_visible = extract_current_value(ev["header"], rf"^{re.escape(PREFIX_DATE)} ")
            if not current_visible:
                current_visible = format_de_datetime(old_local)
            ev["header"] = replace_with_struck(ev["header"], PREFIX_DATE, current_visible, new_str)
            new_event_time = new_local.astimezone(pytz.utc)

            # Auto-Delete relativ verschieben
            delete_at = ev.get("delete_at")
            if isinstance(delete_at, datetime):
                offset = delete_at - old_event_time
                ev["delete_at"] = new_event_time + offset

            ev["event_time"] = new_event_time



            # Wenn Datum/Zeit geändert wird: Reminder/AFK-Flags zurücksetzen


            for _emoji, _slot in ev["slots"].items():


                _slot["reminded"] = set()


                _slot["afk_dm_sent"] = set()


            try:


                for k in list(AFK_PENDING.keys()):


                    if k[0] == interaction.guild.id and k[1] == int(msg_id):


                        AFK_PENDING.pop(k, None)


            except Exception:


                pass
            thread_changes.append(f"Datum/Zeit: ~~{current_visible}~~ → {new_str}")
        except Exception:
            await interaction.response.send_message(
                "❌ Fehler im Datumsformat (DD.MM.YYYY / HH:MM).",
                ephemeral=True,
            )
            return

    # Ort
    if ort:
        current_visible = extract_current_value(ev["header"], rf"^{re.escape(PREFIX_ORG)} ")
        if not current_visible:
            m = re.search(rf"^{re.escape(PREFIX_ORG)} (.+)$", ev["header"], re.M)
            current_visible = m.group(1) if m else "?"
        ev["header"] = replace_with_struck(ev["header"], PREFIX_ORG, current_visible, ort)
        thread_changes.append(f"Ort: ~~{current_visible}~~ → {ort}")

    # Level
    if level:
        current_visible = extract_current_value(ev["header"], rf"^{re.escape(PREFIX_LEVEL)} ")
        if not current_visible:
            m = re.search(rf"^{re.escape(PREFIX_LEVEL)} (.+)$", ev["header"], re.M)
            current_visible = m.group(1) if m else "?"
        ev["header"] = replace_with_struck(ev["header"], PREFIX_LEVEL, current_visible, level)
        thread_changes.append(f"Level: ~~{current_visible}~~ → {level}")

    # Anmerkung
    if anmerkung:
        if "📝 **Anmerkung:**" in ev["header"]:
            ev["header"] = re.sub(
                r"📝 \*\*Anmerkung:\*\* .+",
                f"📝 **Anmerkung:** {anmerkung}",
                ev["header"],
            )
        else:
            if "─────────────────────────────────" in ev["header"]:
                ev["header"] = ev["header"].replace(
                    "─────────────────────────────────",
                    f"📝 **Anmerkung:** {anmerkung}\n─────────────────────────────────",
                    1,
                )
            else:
                ev["header"] += f"\n📝 **Anmerkung:** {anmerkung}"
        thread_changes.append("Anmerkung aktualisiert")

    # Slots
    if slots:
        parsed = parse_slots(slots, interaction.guild)
        if parsed is None or isinstance(parsed, str):
            await interaction.response.send_message(
                "❌ Ungültige Slots. Beispiel: ⚔️:2 🛡️:1",
                ephemeral=True,
            )
            return
        ev["slots"] = parsed

        guild = interaction.guild
        channel = guild.get_channel(ev["channel_id"])
        try:
            msg = await channel.fetch_message(msg_id)
        except Exception:
            await interaction.response.send_message(
                "⚠️ Konnte die Eventnachricht nicht finden (Slots).",
                ephemeral=True,
            )
            return

        try:
            await msg.clear_reactions()
        except Exception:
            pass

        failed_emojis = []
        for emoji in ev["slots"].keys():
            try:
                await msg.add_reaction(emoji)
            except Exception:
                failed_emojis.append(emoji)
        thread_changes.append("Slots angepasst")
        if failed_emojis:
            thread = await get_or_restore_thread(ev, interaction.guild, msg_id)
            if thread:
                await thread.send(
                    "⚠️ Einige Emojis konnten nicht hinzugefügt werden: "
                    + ", ".join(failed_emojis)
                )

    await update_event_message(msg_id)
    await safe_save()
    await interaction.response.send_message("✅ Event aktualisiert.", ephemeral=True)

    if thread_changes:
        guild = interaction.guild
        changes = ", ".join(thread_changes)
        await post_event_update_log(ev, guild, interaction.user.mention, changes, msg_id)
        if any(s.startswith("Datum/Zeit:") for s in thread_changes):
            await post_calendar_links(ev, guild, msg_id)


# ----------------- /event_reset_notifications -----------------
@app_commands.describe(
    event="Optional: Event auswählen (Dropdown). Wenn leer: dein aktuelles Event.",
)
@bot.tree.command(name="event_reset_notifications", description="Setzt Reminder & AFK-Check für ein Event zurück (Ersteller/Admin)")
@app_commands.autocomplete(event=event_edit_event_autocomplete)
async def event_reset_notifications(interaction: discord.Interaction, event: str = None):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Nur auf einem Server nutzbar.", ephemeral=True)
        return

    msg_id = None
    ev = None

    if event:
        try:
            msg_id = int(re.search(r"(\d{15,21})", str(event)).group(1))
        except Exception:
            msg_id = None
        if msg_id:
            ev = active_events.get(msg_id)

    if not ev:
        found = get_latest_user_event(interaction.guild.id, interaction.user.id)
        if not found:
            await interaction.response.send_message("❌ Ich finde aktuell kein Event von dir.", ephemeral=True)
            return
        msg_id, ev = found

    if not ev or ev.get("guild_id") != interaction.guild.id:
        await interaction.response.send_message("❌ Event nicht gefunden.", ephemeral=True)
        return
    if not can_edit_event(interaction, ev):
        await interaction.response.send_message("❌ Nicht erlaubt (nur Ersteller oder Admins).", ephemeral=True)
        return

    for _emoji, _slot in ev.get("slots", {}).items():
        _slot["reminded"] = set()
        _slot["afk_dm_sent"] = set()

    try:
        for k in list(AFK_PENDING.keys()):
            if k[0] == interaction.guild.id and k[1] == int(msg_id):
                AFK_PENDING.pop(k, None)
    except Exception:
        pass

    await safe_save()
    await update_event_message(int(msg_id))
    await interaction.response.send_message("✅ Reminder & AFK-Check wurden zurückgesetzt.", ephemeral=True)

# ----------------- /event_delete -----------------
@bot.tree.command(name="event_delete", description="Löscht nur dein eigenes Event")
async def event_delete(interaction: discord.Interaction):
    found = get_latest_user_event(interaction.guild.id, interaction.user.id)
    if not found:
        await interaction.response.send_message(
            "❌ Ich finde aktuell kein Event von dir auf diesem Server.",
            ephemeral=True,
        )
        return

    msg_id, ev = found
    try:
        channel = interaction.guild.get_channel(ev["channel_id"])
        if channel:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.delete()
            except Exception:
                pass
        thread_id = ev.get("thread_id")
        if thread_id:
            try:
                thread = interaction.guild.get_channel(thread_id) or await interaction.guild.fetch_channel(thread_id)
                await thread.delete()
            except Exception:
                pass
        del active_events[msg_id]
        await safe_save()
        await interaction.response.send_message("✅ Dein Event wurde gelöscht.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Fehler beim Löschen: {e}",
            ephemeral=True,
        )


# ----------------- /event_list + /events -----------------
async def _send_event_list(interaction: discord.Interaction):
    items = sorted(
        [
            (mid, ev)
            for mid, ev in active_events.items()
            if ev.get("guild_id") == interaction.guild.id
        ],
        key=lambda kv: kv[1].get("event_time", datetime.now(pytz.utc)),
    )
    if not items:
        await interaction.response.send_message(
            "ℹ️ Es sind keine aktiven Events auf diesem Server vorhanden.",
            ephemeral=True,
        )
        return

    lines = []
    for mid, ev in items:
        guild = interaction.guild
        ch = guild.get_channel(ev["channel_id"])
        when = (
            format_de_datetime(ev["event_time"].astimezone(BERLIN_TZ))
            if ev.get("event_time")
            else "unbekannt"
        )
        creator = guild.get_member(ev["creator_id"])
        creator_name = creator.mention if creator else f"<@{ev['creator_id']}>"
        channel_tag = ch.mention if ch else "#gelöscht"
        jump_url = f"https://discord.com/channels/{guild.id}/{ev['channel_id']}/{mid}"
        art = ev.get("art", "Event")
        art_emoji = ART_EMOJI.get(art, "🎮")
        lines.append(
            f"{art_emoji} **{ev['title']}** — {when} — von {creator_name} — {channel_tag} — [zum Event]({jump_url})"
        )

    embed = discord.Embed(
        title="📅 Aktive Events (Serverweit)",
        description="\n".join(lines),
        color=0x2ECC71,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="event_list", description="Listet alle aktiven Events auf dem Server auf")
async def event_list(interaction: discord.Interaction):
    await _send_event_list(interaction)


@bot.tree.command(name="events", description="Listet alle aktiven Events auf dem Server auf (Alias)")
async def events_alias(interaction: discord.Interaction):
    await _send_event_list(interaction)


# ----------------- /event_info -----------------
@bot.tree.command(name="event_info", description="Zeigt Details zu deinem aktuellen Event auf diesem Server")
async def event_info(interaction: discord.Interaction):
    found = get_latest_user_event(interaction.guild.id, interaction.user.id)

    if not found:
        await interaction.response.send_message(
            "ℹ️ Ich finde aktuell kein Event von dir auf diesem Server.",
            ephemeral=True,
        )
        return

    msg_id, ev = found
    guild = interaction.guild

    art = ev.get("art", "Event")
    art_emoji = ART_EMOJI.get(art, "🎮")

    embed = discord.Embed(
        title=f"{art_emoji} Event-Info: {ev['title']}",
        color=0x3498DB,
    )
    embed.add_field(
        name="📄 Basisdaten",
        value=ev["header"],
        inline=False,
    )

    slot_lines: List[str] = []
    for emoji, slot in ev["slots"].items():
        main_users = [
            guild.get_member(uid).mention
            for uid in slot["main"]
            if guild.get_member(uid)
        ]
        wait_users = [
            guild.get_member(uid).mention
            for uid in slot["waitlist"]
            if guild.get_member(uid)
        ]
        line = f"{emoji} **({len(main_users)}/{slot['limit']})**: "
        line += ", ".join(main_users) if main_users else "-"
        if wait_users:
            line += "\n   ⏳ **Warteliste:** " + ", ".join(wait_users)
        slot_lines.append(line)

    embed.add_field(
        name="🎟️ Slots",
        value="\n".join(slot_lines) if slot_lines else "Keine Slots vorhanden.",
        inline=False,
    )

    jump_url = f"https://discord.com/channels/{guild.id}/{ev['channel_id']}/{msg_id}"
    embed.add_field(
        name="🔗 Direkt zum Event",
        value=f"[Hier klicken]({jump_url})",
        inline=False,
    )

    auto_del = ev.get("auto_delete_stunden")
    if auto_del:
        embed.add_field(
            name="⏱️ Auto-Löschung",
            value=f"{auto_del}h nach Start",
            inline=True,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ----------------- /subscribe & /unsubscribe -----------------
@app_commands.choices(
    art=[app_commands.Choice(name=x, value=x) for x in ["PvE", "PvP", "PVX", "Alle"]],
)
@bot.tree.command(name="subscribe", description="Abonniere Benachrichtigungen für neue Events")
async def subscribe_command(interaction: discord.Interaction, art: app_commands.Choice[str]):
    guild_id = interaction.guild.id
    user_id = interaction.user.id
    art_value = art.value

    if guild_id not in SUBSCRIPTIONS:
        SUBSCRIPTIONS[guild_id] = {"PvE": [], "PvP": [], "PVX": []}

    if art_value == "Alle":
        for key in ["PvE", "PvP", "PVX"]:
            if user_id not in SUBSCRIPTIONS[guild_id].setdefault(key, []):
                SUBSCRIPTIONS[guild_id][key].append(user_id)
        await interaction.response.send_message(
            "✅ Du erhältst jetzt DMs für **alle** neuen Events (PvE, PvP, PVX).",
            ephemeral=True,
        )
    else:
        li = SUBSCRIPTIONS[guild_id].setdefault(art_value, [])
        if user_id in li:
            await interaction.response.send_message(
                f"ℹ️ Du warst bereits für **{art_value}**-Events abonniert.",
                ephemeral=True,
            )
        else:
            li.append(user_id)
            await interaction.response.send_message(
                f"✅ Du erhältst jetzt DMs für neue **{art_value}**-Events.",
                ephemeral=True,
            )

    await safe_save()


@app_commands.choices(
    art=[app_commands.Choice(name=x, value=x) for x in ["PvE", "PvP", "PVX", "Alle"]],
)
@bot.tree.command(name="unsubscribe", description="Beende Benachrichtigungen für neue Events")
async def unsubscribe_command(interaction: discord.Interaction, art: app_commands.Choice[str]):
    guild_id = interaction.guild.id
    user_id = interaction.user.id
    art_value = art.value

    if guild_id not in SUBSCRIPTIONS:
        SUBSCRIPTIONS[guild_id] = {"PvE": [], "PvP": [], "PVX": []}

    if art_value == "Alle":
        for key in ["PvE", "PvP", "PVX"]:
            lst = SUBSCRIPTIONS[guild_id].setdefault(key, [])
            if user_id in lst:
                lst.remove(user_id)
        await interaction.response.send_message(
            "✅ Du erhältst keine DMs mehr für neue Events.",
            ephemeral=True,
        )
    else:
        lst = SUBSCRIPTIONS[guild_id].setdefault(art_value, [])
        if user_id in lst:
            lst.remove(user_id)
            await interaction.response.send_message(
                f"✅ Du erhältst keine DMs mehr für neue **{art_value}**-Events.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ Du warst für **{art_value}**-Events nicht abonniert.",
                ephemeral=True,
            )

    await safe_save()


# ----------------- /stats -----------------
@bot.tree.command(name="stats", description="Zeigt Event-Statistiken für diesen Server")
async def stats_command(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    now = datetime.utcnow()

    # Filter History auf diesen Guild
    hist_guild = [h for h in EVENT_HISTORY if h.get("guild_id") == guild_id]

    total_events = len(hist_guild)
    last_30_days = [
        h for h in hist_guild
        if "created_at" in h
        and datetime.fromisoformat(h["created_at"]) >= now - timedelta(days=30)
    ]
    last_7_days = [
        h for h in hist_guild
        if "created_at" in h
        and datetime.fromisoformat(h["created_at"]) >= now - timedelta(days=7)
    ]

    by_art = {"PvE": 0, "PvP": 0, "PVX": 0, "Sonstige": 0}
    for h in hist_guild:
        art = h.get("art")
        if art in by_art:
            by_art[art] += 1
        else:
            by_art["Sonstige"] += 1

    active_on_server = [
        ev for ev in active_events.values()
        if ev.get("guild_id") == guild_id
    ]
    upcoming = [
        ev for ev in active_on_server
        if ev.get("event_time") and ev["event_time"] >= datetime.now(pytz.utc)
    ]

    embed = discord.Embed(
        title=f"📊 SlotBot-Stats für {interaction.guild.name}",
        color=0xF1C40F,
    )

    embed.add_field(
        name="📦 Gesamt",
        value=(
            f"• Events gesamt: **{total_events}**\n"
            f"• Letzte 30 Tage: **{len(last_30_days)}**\n"
            f"• Letzte 7 Tage: **{len(last_7_days)}**"
        ),
        inline=False,
    )

    embed.add_field(
        name="🎮 Nach Event-Art",
        value=(
            f"• PvE: **{by_art['PvE']}**\n"
            f"• PvP: **{by_art['PvP']}**\n"
            f"• PVX: **{by_art['PVX']}**\n"
            f"• Sonstige: **{by_art['Sonstige']}**"
        ),
        inline=False,
    )

    embed.add_field(
        name="📅 Aktive / Bevorstehende Events",
        value=(
            f"• Aktive Events: **{len(active_on_server)}**\n"
            f"• Davon noch bevorstehend: **{len(upcoming)}**"
        ),
        inline=False,
    )

    # Punkte-Statistik

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ----------------- Punkte-System Commands -----------------
@app_commands.describe(
    member="Spieler, der Punkte bekommen soll",
    amount="Anzahl der Punkte",
    reason="Optional: Grund für die Punktevergabe",
)

# ----------------- Reaction Handling -----------------
async def _fetch_message_with_retry(channel: discord.abc.Messageable, message_id: int, tries: int = 3):
    for _ in range(tries):
        try:
            return await channel.fetch_message(message_id)
        except Exception:
            await asyncio.sleep(1)
    return None


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return

    if payload.message_id not in active_events:
        ok = await try_reload_if_missing(payload.message_id)
        if not ok:
            return

    ev = active_events.get(payload.message_id)
    emoji = normalize_emoji(payload.emoji)
    if emoji not in ev["slots"]:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    channel = guild.get_channel(payload.channel_id)
    if not channel:
        return

    msg = await _fetch_message_with_retry(channel, payload.message_id)
    if not msg:
        return

    try:
        member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
    except Exception:
        return

    # Nur eine Slot-Reaktion pro Nutzer erlauben
    for e in list(ev["slots"].keys()):
        if e != emoji:
            try:
                await msg.remove_reaction(e, member)
            except Exception:
                pass

    # Schon eingetragen?
    if any(
        payload.user_id in s["main"] or payload.user_id in s["waitlist"]
        for s in ev["slots"].values()
    ):
        return

    # Eintragen
    slot = ev["slots"][emoji]
    if len(slot["main"]) < slot["limit"]:
        slot["main"].add(payload.user_id)
        slot_type = "Hauptslot"
    else:
        slot["waitlist"].append(payload.user_id)
        slot_type = "Warteliste"

    await update_event_message(payload.message_id)
    await safe_save()

    try:
        await log_participation_change(
            ev,
            guild,
            payload.message_id,
            payload.user_id,
            emoji,
            "join",
            slot_type,
        )
    except Exception:
        pass


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.message_id not in active_events:
        ok = await try_reload_if_missing(payload.message_id)
        if not ok:
            return

    ev = active_events.get(payload.message_id)
    emoji = normalize_emoji(payload.emoji)
    if emoji not in ev["slots"]:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    slot = ev["slots"][emoji]
    user_id = payload.user_id

    promoted_user = None
    left_slot_type = None

    if user_id in slot["main"]:
        left_slot_type = "Hauptslot"
        slot["main"].remove(user_id)
        if slot["waitlist"]:
            promoted_user = slot["waitlist"].pop(0)
            slot["main"].add(promoted_user)
    elif user_id in slot["waitlist"]:
        left_slot_type = "Warteliste"
        try:
            slot["waitlist"].remove(user_id)
        except ValueError:
            pass

    await update_event_message(payload.message_id)
    await safe_save()

    if left_slot_type:
        try:
            await log_participation_change(
                ev,
                guild,
                payload.message_id,
                user_id,
                emoji,
                "leave",
                left_slot_type,
            )
        except Exception:
            pass

    if promoted_user is not None:
        try:
            member = guild.get_member(promoted_user) or await guild.fetch_member(promoted_user)
            await member.send(f"🎟️ Du bist jetzt im **Hauptslot** für **{ev['title']}**! Viel Spaß 🎉")
        except Exception:
            pass
        try:
            thread = await get_or_restore_thread(ev, guild, payload.message_id)
            if thread:
                await thread.send(
                    f"🔄 <@{promoted_user}> wurde automatisch aus der Warteliste in den Hauptslot verschoben."
                )
        except Exception:
            pass


# ----------------- AFK-Check DM-Handling -----------------
@bot.event
async def on_message(message: discord.Message):
    # Normale Bot-Commands nicht blockieren
    await bot.process_commands(message)

    # Wir interessieren uns nur für DMs an den Bot
    if message.author.bot:
        return
    if message.guild is not None:
        return  # keine Guild-Message, nur DM

    user_id = message.author.id
    # Suche alle offenen AFK-Pending-Einträge dieses Users
    to_remove = []
    for (guild_id, msg_id, uid), deadline in AFK_PENDING.items():
        if uid != user_id:
            continue
        # User meldet sich -> AFK-Check bestanden
        to_remove.append((guild_id, msg_id, uid))

        guild = bot.get_guild(guild_id)
        ev = active_events.get(msg_id)
        if guild and ev:
            try:
                await message.channel.send(
                    f"✅ Danke für deine Rückmeldung! Du bleibst im Event **{ev['title']}** eingetragen."
                )
            except Exception:
                pass
    for key in to_remove:
        AFK_PENDING.pop(key, None)


# ----------------- /test -----------------
@bot.tree.command(name="test", description="Prüft grundlegende Bot-Funktionalität")
async def test_command(interaction: discord.Interaction):
    # Nur Owner darf testen
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message(
            "❌ Du darfst diesen Test nicht ausführen.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    results: List[tuple[str, bool]] = []

    # ENV / GitHub Basics
    results.append(("DISCORD_TOKEN gesetzt", TOKEN is not None))
    results.append(("GITHUB_TOKEN gesetzt", GITHUB_TOKEN is not None))
    results.append(("GITHUB_REPO gesetzt", bool(GITHUB_REPO)))
    results.append(("GITHUB_FILE_PATH gesetzt", bool(GITHUB_FILE_PATH)))

    # GitHub erreichbar?
    gh_read_ok = False
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
            r = requests.get(url, headers=gh_headers(), timeout=10)
            gh_read_ok = r.status_code in (200, 404)
        except Exception:
            gh_read_ok = False
    results.append(("GitHub erreichbar (events.json)", gh_read_ok))

    # Save-Test
    save_ok = True
    try:
        save_events()
    except Exception:
        save_ok = False
    results.append(("Persistenz-Speicherfunktion ausführbar", save_ok))

    # Aktive Events vorhanden?
    results.append(("Aktive Events im Speicher", len(active_events) > 0))

    # ICS-Test (falls Event vorhanden)
    ics_ok = False
    if active_events:
        any_ev = next(iter(active_events.values()))
        try:
            header = any_ev["header"]
            m_ort = re.search(r"^📍 \*\*Ort:\*\* (.+)$", header, re.M)
            ort = m_ort.group(1) if m_ort else ""
            _ics = build_ics_content(any_ev["title"], any_ev["event_time"], 2, ort, "Test")
            ics_ok = bool(_ics)
        except Exception:
            ics_ok = False
    results.append(("ICS-Generierung für ein Event", ics_ok))

    # Guild/Channel Tests
    channel_send_ok = False
    thread_create_ok = False
    reaction_ok = False
    perms_ok_send = False
    perms_ok_thread = False
    perms_ok_react = False

    if interaction.guild and isinstance(interaction.channel, discord.abc.Messageable):
        guild = interaction.guild
        me = guild.me or guild.get_member(bot.user.id)
        perms = interaction.channel.permissions_for(me)

        perms_ok_send = perms.send_messages
        perms_ok_thread = perms.create_public_threads or perms.create_private_threads or perms.send_messages_in_threads
        perms_ok_react = perms.add_reactions

        results.append(("Recht: Nachrichten senden im aktuellen Channel", perms_ok_send))
        results.append(("Recht: Threads erstellen im aktuellen Channel", perms_ok_thread))
        results.append(("Recht: Reaktionen hinzufügen im aktuellen Channel", perms_ok_react))

        test_msg = None
        test_thread = None

        # Test: Nachricht senden
        try:
            test_msg = await interaction.channel.send("🧪 SlotBot-Test: Nachricht senden...")
            channel_send_ok = True
        except Exception:
            channel_send_ok = False

        # Test: Thread
        if test_msg:
            try:
                test_thread = await test_msg.create_thread(
                    name="🧪 SlotBot-Test-Thread",
                    auto_archive_duration=60,
                )
                thread_create_ok = True
            except Exception:
                thread_create_ok = False

        # Test: Reaktion
        if test_msg:
            try:
                await test_msg.add_reaction("✅")
                reaction_ok = True
            except Exception:
                reaction_ok = False

        # Cleanup Test-Objekte
        try:
            if test_thread:
                await test_thread.delete()
        except Exception:
            pass
        try:
            if test_msg:
                await test_msg.delete()
        except Exception:
            pass

    else:
        results.append(("Guild-Kontext vorhanden", False))

    results.append(("Nachricht im aktuellen Channel sendbar (praktisch)", channel_send_ok))
    results.append(("Thread im aktuellen Channel erstellbar (praktisch)", thread_create_ok))
    results.append(("Reaktionen im aktuellen Channel nutzbar (praktisch)", reaction_ok))

    # Reminder/Cleanup (logische Checks)
    results.append(("Reminder-Task registriert (logisch)", True))
    results.append(("AFK-Check-Task registriert (logisch)", True))
    results.append(("Auto-Cleanup aktiv (logisch)", True))

    ok_count = sum(1 for _, ok in results if ok)
    total = len(results)

    embed = discord.Embed(
        title="🧪 SlotBot – Selbsttest (Owner)",
        description=f"{ok_count}/{total} Checks OK",
        color=discord.Color.green() if ok_count == total else discord.Color.orange(),
    )

    for name, ok in results:
        emoji = "✅" if ok else "❌"
        embed.add_field(name=name, value=emoji, inline=False)

    embed.set_footer(text="Reale Event-Slots, DMs & AFK-Checks bitte mit einem Test-Event prüfen.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ----------------- Flask (Render) -----------------
flask_app = Flask("bot_flask")



def run_flask():
    # Render erwartet, dass wir auf PORT binden (Healthchecks).
    flask_app.run(host="0.0.0.0", port=port)

@flask_app.route("/")
def index():
    return "✅ SlotBot v4.6 läuft (Render kompatibel)."


@flask_app.route("/ics/<int:message_id>.ics")
def ics_file(message_id: int):
    ev = active_events.get(message_id)
    if not ev:
        fresh = load_events_with_retry()
        active_events.clear()
        active_events.update(fresh)
        ev = active_events.get(message_id)
        if not ev:
            return Response("Event nicht gefunden.", status=404)

    header = ev["header"]
    m_ort = re.search(r"^📍 \*\*Ort:\*\* (.+)$", header, re.M)
    ort = m_ort.group(1) if m_ort else ""
    desc = "Event aus SlotBot"
    ics_text = build_ics_content(ev["title"], ev["event_time"], 2, ort, desc)

    return Response(
        ics_text,
        mimetype="text/calendar",
        headers={
            "Content-Disposition": f'attachment; filename="event_{message_id}.ics"'
        },
    )



async def _discord_main():
    # Watchdog: zeigt, ob der Bot wirklich ready wird
    async def _ready_watchdog():
        for secs in (15, 30, 60, 120):
            await asyncio.sleep(secs)
            if not bot.is_ready():
                print(f"⚠️ Bot noch nicht ready nach {secs}s – prüfe Token/Intents/Discord-Ausfall…")
            else:
                return

    asyncio.create_task(_ready_watchdog())
    await bot.start(DISCORD_TOKEN)

def run_bot():
    print("🤖 Starte Discord Bot…")
    try:
        asyncio.run(_discord_main())
    except Exception:
        import traceback
        print("❌ Discord Bot ist beim Start abgestürzt:")
        traceback.print_exc()
        raise



@bot.event
async def on_ready():
    print(f"✅ Discord online als {bot.user} | Guilds={len(bot.guilds)}")
    global TASKS_STARTED

    print(f"✅ SlotBot v4.6 online als {bot.user} (instance={globals().get('INSTANCE_ID', 'unknown')})")
    loaded = load_events_with_retry()
    active_events.clear()
    active_events.update(loaded)
    print(f"📂 Aktive Events im Speicher: {len(active_events)}")

    # Extra-Schutz: Falls der Prozess aus irgendeinem Grund on_ready doppelt bekommt,
    # starten wir die Background-Tasks nicht nochmal.
    # (TASKS_STARTED hilft im selben Prozess; zusätzlich prüfen wir laufende Tasks.)
    for t in BACKGROUND_TASKS.values():
        if t and not t.done() and not t.cancelled():
            # Es läuft bereits mindestens ein Background-Task -> nichts doppelt starten
            return


    # Background-Tasks nur einmal pro Prozess starten, um doppelte DMs zu vermeiden
    if not TASKS_STARTED:
        BACKGROUND_TASKS["reminder"] = bot.loop.create_task(reminder_task(), name="slotbot_reminder_task")
        BACKGROUND_TASKS["afk_enforcer"] = bot.loop.create_task(afk_enforcer_task(), name="slotbot_afk_enforcer_task")
        BACKGROUND_TASKS["cleanup"] = bot.loop.create_task(cleanup_task(), name="slotbot_cleanup_task")
        BACKGROUND_TASKS["watchdog"] = bot.loop.create_task(watchdog_task(), name="slotbot_watchdog_task")
        TASKS_STARTED = True
    else:
        # Falls ein Task unerwartet beendet wurde, ggf. neu starten
        for key, factory in [
            ("reminder", reminder_task),
            ("afk_enforcer", afk_enforcer_task),
            ("cleanup", cleanup_task),
        ]:
            task = BACKGROUND_TASKS.get(key)
            if not task or task.done() or task.cancelled():
                BACKGROUND_TASKS[key] = bot.loop.create_task(factory(), name=f"slotbot_{key}_task")

    try:
        await bot.tree.sync()
        print("📂 Slash Commands synchronisiert")
    except Exception as e:
        print(f"❌ Sync-Fehler: {e}")


if __name__ == "__main__":
    print("🚀 Starte SlotBot v4.6 + Flask ...")

    # Flask im Hintergrund (für Render Healthcheck)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Discord im Hauptthread (stabiler als Bot im Neben-Thread)
    print("🤖 Starte Discord Bot…")
    bot.run(DISCORD_TOKEN)
