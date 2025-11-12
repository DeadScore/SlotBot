# main.py — SlotBot v4.4 
# Changelog v4.4:
# - Toleranter Slot-Parser (beliebig viele Leerzeichen rund um ":")
# - Kalenderlinks nebeneinander im Thread
# - /events Alias zu /event_list
# - Thread-Logs bei An-/Abmeldung (Reaktionen)
# - Neuer Befehl: /event_info (zeigt dein aktuelles Event als Embed)
# - NEU: Pro-Event Auto-Cleanup per Stunden (Default 1h, optional überschreibbar)
# - NEU: Schöner Event-Post als Embed + hübsche ephemere Erfolgsmeldung
#
# Features:
# - /event, /event_edit, /event_delete, /event_list (/events), /event_info, /help
# - Deutsche Wochentage im Datum
# - Strike-Through bei Änderungen (nur letzte alte Angabe)
# - Thread-Logs (robust: ent-archivieren/neu erstellen)
# - 10-Minuten-Reminder per DM
# - Persistenz via GitHub (data/events.json) + Start-Retry + Auto-Create
# - Reaktionen robust (Reload bei Neustart, Member-Fetch, Retry, Save-Lock)
# - Emoji-Fix: Problematische Emojis werden übersprungen; Hinweis im Thread
# - Creator-Fix: Edit/Delete findet Events serverweit des Erstellers (nicht nur Channel)
# - Kalenderlinks NUR im Thread: Google + Apple (.ics-Anhang, oder Link via PUBLIC_BASE_URL)
# - Pro-Event Auto-Cleanup: Standard 1h nach Start (optional anpassbar über /event oder /event_edit)
#
# Repo-Default (Fallback): DeadScore/SlotBot  — kann per Env überschrieben werden.
#
# ENV Variablen (Render > Environment):
# - DISCORD_TOKEN           (required)
# - GITHUB_TOKEN           (required, scope: repo)
# - GITHUB_REPO            (optional, default: DeadScore/SlotBot)
# - GITHUB_FILE_PATH       (optional, default: data/events.json)
# - PUBLIC_BASE_URL        (optional, e.g. https://slotbot-xxxx.onrender.com)  # für klickbaren ICS-Link
#
# Python >= 3.9

import os
import re
import io
import json
import asyncio
import base64
import requests
from datetime import datetime, timedelta
from threading import Thread
from typing import Optional
import pytz
from urllib.parse import quote_plus

import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask, Response

# ----------------- Konfiguration -----------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("❌ DISCORD_TOKEN nicht gesetzt. Bitte als Environment Variable konfigurieren.")
    raise SystemExit(1)

CUSTOM_EMOJI_REGEX = r"<a?:\w+:\d+>"
BERLIN_TZ = pytz.timezone("Europe/Berlin")
DEFAULT_CLEANUP_HOURS = 1  # Standardmäßig 1h nach Eventstart löschen (falls nicht überschrieben)

# GitHub
GITHUB_REPO = os.getenv("GITHUB_REPO", "DeadScore/SlotBot")
GITHUB_FILE_PATH = os.getenv("GITHUB_FILE_PATH", "data/events.json")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # optional, für klickbaren ICS-Link

# ----------------- Intents & Bot -----------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# In-Memory
active_events: dict[int, dict] = {}  # message_id -> event data
SAVE_LOCK = asyncio.Lock()

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


def format_de_datetime(local_dt: datetime) -> str:
    en = local_dt.strftime("%A")
    de = WEEKDAY_DE.get(en, en)
    return local_dt.strftime(f"%A, %d.%m.%Y %H:%M %Z").replace(en, de)


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
        "PRODID:-//SlotBot//v4.3.3//EN",
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
    # Custom-Emoji: nur wenn auf dem Server vorhanden
    if re.match(CUSTOM_EMOJI_REGEX, emoji):
        return any(str(e) == emoji for e in guild.emojis)
    # Unicode: grundsätzlich zulassen (manche scheitern erst bei add_reaction -> wird abgefangen)
    return True


# Toleranter Slot-Parser: beliebig viele Leerzeichen rund um ":" zulassen
SLOT_PATTERN = re.compile(r"(<a?:\w+:\d+>|[^\s:]+)\s*:\s*(\d+)")


def parse_slots(slots_str: str, guild: discord.Guild):
    matches = SLOT_PATTERN.findall(slots_str or "")
    if not matches:
        return None
    slot_dict: dict[str, dict] = {}
    for emoji, limit in matches:
        em = normalize_emoji(emoji)
        if not is_valid_emoji(em, guild):
            return f"Ungültiges Emoji: {em}"
        slot_dict[em] = {"limit": int(limit), "main": set(), "waitlist": [], "reminded": set()}
    return slot_dict


def format_event_text(event, guild: discord.Guild):
    text = "**📋 Eventübersicht:**\n"
    for emoji, slot in event["slots"].items():
        main_users = [guild.get_member(uid).mention for uid in slot["main"] if guild.get_member(uid)]
        wait_users = [guild.get_member(uid).mention for uid in slot["waitlist"] if guild.get_member(uid)]
        text += f"\n{emoji} ({len(main_users)}/{slot['limit']}): "
        text += ", ".join(main_users) if main_users else "-"
        if wait_users:
            text += f"\n   ⏳ Warteliste: " + ", ".join(wait_users)
    return text


# 🆕 Neue Helferfunktionen für hübsche Embeds
def format_slots_for_embed(event, guild: discord.Guild) -> str:
    """Schöne Slotliste ohne Überschrift für Embeds."""
    lines = []
    for emoji, slot in event["slots"].items():
        main_users = [guild.get_member(uid).mention for uid in slot["main"] if guild.get_member(uid)]
        wait_users = [guild.get_member(uid).mention for uid in slot["waitlist"] if guild.get_member(uid)]
        line = f"{emoji} **({len(main_users)}/{slot['limit']})**: " + (", ".join(main_users) if main_users else "-")
        if wait_users:
            line += "\n   ⏳ **Warteliste:** " + ", ".join(wait_users)
        lines.append(line)
    return "\n".join(lines) if lines else "—"


def color_for_art(art_value: str) -> int:
    m = (art_value or "").lower()
    if m == "pve":
        return 0x2ECC71
    if m == "pvp":
        return 0xE74C3C
    return 0x3498DB  # PVX / default


def build_event_embed(zweck: str, ort: str, time_str: str, level: str, stil_value: str, art_value: str, slot_dict: dict, guild: discord.Guild, cleanup_hours: int) -> discord.Embed:
    embed = discord.Embed(
        title=zweck,
        description=(
            f"**📍 Ort:** {ort}\n"
            f"**🕒 Zeit:** {time_str}\n"
            f"**⚔️ Level:** {level}\n"
            f"**💬 Stil:** {stil_value}"
        ),
        color=color_for_art(art_value),
    )
    embed.set_author(name=f"{art_value} – Neue Gruppensuche!")
    embed.add_field(name="🎟️ Slots", value=format_slots_for_embed({"slots": slot_dict}, guild), inline=False)
    embed.set_footer(text=f"Automatisches Löschen: {cleanup_hours}h nach Start")
    return embed


def build_event_embed_from_ev(ev: dict, guild: discord.Guild) -> discord.Embed:
    # Parse values from header
    header = ev.get("header", "")
    def rex(label):
        m = re.search(rf"^{label} (.+)$", header, re.M)
        return m.group(1).strip() if m else ""
    art_value = rex("🗡️ \\*\\*Art:\\*\\*")
    zweck = rex("🎯 \\*\\*Zweck:\\*\\*")
    ort = rex("📍 \\*\\*Ort:\\*\\*")
    level = rex("⚔️ \\*\\*Levelbereich:\\*\\*")
    stil_value = rex("💬 \\*\\*Stil:\\*\\*")
    time_str = rex("🕒 \\*\\*Datum/Zeit:\\*\\*")
    embed = discord.Embed(
        title=zweck or ev.get("title", "Event"),
        description=(
            f"**📍 Ort:** {ort}\n"
            f"**🕒 Zeit:** {time_str}\n"
            f"**⚔️ Level:** {level}\n"
            f"**💬 Stil:** {stil_value}"
        ),
        color=color_for_art(art_value or ""),
    )
    embed.set_author(name=f"{(art_value or 'Event')} – Update")
    embed.add_field(name="🎟️ Slots", value=format_slots_for_embed(ev, guild), inline=False)
    cleanup_hours = int(ev.get("cleanup_hours", 1))
    embed.set_footer(text=f"Automatisches Löschen: {cleanup_hours}h nach Start")
    return embed


async def update_event_message(message_id: int):
    ev = active_events.get(message_id)
    if not ev:
        return
    guild = bot.get_guild(ev["guild_id"])
    if not guild:
        return
    channel = guild.get_channel(ev["channel_id"])
    if not channel:
        return
    for _ in range(3):
        try:
            msg = await channel.fetch_message(int(message_id))
            try:
                embed = build_event_embed_from_ev(ev, guild)
                await msg.edit(embed=embed)
            except Exception:
                await msg.edit(content=ev["header"] + "\n\n" + format_event_text(ev, guild))
            return
        except Exception:
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


def load_events_once():
    if not GITHUB_TOKEN:
        print("⚠️ GITHUB_TOKEN fehlt – starte ohne Persistenz.")
        return {}
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    try:
        r = requests.get(url, headers=gh_headers(), timeout=10)
        if r.status_code == 200:
            raw = json.loads(base64.b64decode(r.json()["content"]))
            fixed: dict[int, dict] = {}
            for k, ev in raw.items():
                for key in ("creator_id", "channel_id", "guild_id", "thread_id"):
                    if key in ev:
                        try:
                            ev[key] = int(ev[key])
                        except Exception:
                            pass
                for s in ev.get("slots", {}).values():
                    s["main"] = set(s.get("main", []))
                    s["waitlist"] = list(s.get("waitlist", []))
                    s["reminded"] = set(s.get("reminded", []))
                fixed[int(k)] = ev
            return fixed
        elif r.status_code == 404:
            print("ℹ️ Keine events.json gefunden – lege leere Datei an.")
            put_empty_events({})
            return {}
        else:
            print(f"⚠️ Fehler beim Laden: HTTP {r.status_code}")
    except Exception as e:
        print(f"❌ Fehler beim Laden von events.json: {e}")
    return {}


def load_events_with_retry(retries=5, delay=1.0):
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

        serializable = {}
        for mid, ev in active_events.items():
            copy = json.loads(json.dumps(ev))
            for s in copy["slots"].values():
                s["main"] = list(s["main"])
                s["reminded"] = list(s["reminded"])
            serializable[str(mid)] = copy

        encoded_content = base64.b64encode(json.dumps(serializable, indent=4).encode()).decode()
        data = {"message": "Update events.json via SlotBot v4.3.3", "content": encoded_content}
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
    if message_id in active_events:
        return True
    fresh = load_events_with_retry()
    if fresh:
        active_events.clear()
        active_events.update(fresh)
        return message_id in active_events
    return False


# ----------------- Reminder -----------------
async def reminder_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(pytz.utc)
        for msg_id, ev in list(active_events.items()):
            guild = bot.get_guild(ev["guild_id"])
            if not guild:
                continue
            event_time = ev.get("event_time")
            if not event_time:
                continue
            for emoji, slot in ev["slots"].items():
                if "reminded" not in slot:
                    slot["reminded"] = set()
                for user_id in list(slot["main"]):
                    if user_id in slot["reminded"]:
                        continue
                    seconds_left = (event_time - now).total_seconds()
                    if 0 <= seconds_left <= 600:
                        try:
                            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
                            await member.send(f"⏰ Dein Event **{ev['title']}** startet in 10 Minuten!")
                            slot["reminded"].add(user_id)
                        except Exception:
                            pass
        await asyncio.sleep(60)


# ----------------- Auto-Cleanup (pro Event, Standard 1h) -----------------
async def cleanup_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(pytz.utc)
        to_cleanup: list[tuple[int, dict]] = []

        # Kandidaten sammeln
        for msg_id, ev in list(active_events.items()):
            event_time = ev.get("event_time")
            if not event_time:
                continue
            hours = int(ev.get("cleanup_hours", DEFAULT_CLEANUP_HOURS))
            if hours < 1:
                hours = 1
            if now >= event_time + timedelta(hours=hours):
                to_cleanup.append((msg_id, ev))

        # Aufräumen
        for msg_id, ev in to_cleanup:
            guild = bot.get_guild(ev.get("guild_id"))
            if not guild:
                active_events.pop(msg_id, None)
                continue

            # Event-Message löschen
            channel = guild.get_channel(ev.get("channel_id"))
            if channel:
                try:
                    msg = await channel.fetch_message(msg_id)
                    await msg.delete()
                except Exception:
                    # Message evtl. schon manuell gelöscht – ignorieren
                    pass

            # Thread löschen (falls vorhanden)
            thread_id = ev.get("thread_id")
            if thread_id:
                thread = guild.get_channel(thread_id)
                if thread:
                    try:
                        await thread.delete()
                    except Exception:
                        pass

            # Aus Speicher entfernen
            active_events.pop(msg_id, None)

        # Nur speichern, wenn wirklich was geändert wurde
        if to_cleanup:
            await safe_save()

        # Regelmäßig prüfen
        await asyncio.sleep(300)


# ----------------- Thread Helper & Logging -----------------
async def get_or_restore_thread(ev: dict, guild: discord.Guild, base_message_id: int):
    thread = None
    thread_id = ev.get("thread_id")
    if thread_id:
        thread = guild.get_channel(thread_id)
        if thread is None:
            try:
                thread = await guild.fetch_channel(thread_id)
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
        print("⚠️ Kein Thread verfügbar für Log-Post.")
        return
    for _ in range(3):
        try:
            embed = discord.Embed(
                description=f"✏️ **{editor_mention}** hat das Event bearbeitet\n{changes_text}",
                color=0xF1C40F,
                timestamp=datetime.utcnow()
            )
            await thread.send(embed=embed)
            return
        except Exception:
            await asyncio.sleep(1)


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
    description_parts = []
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


async def log_error_to_thread(ev: dict, guild: discord.Guild, base_message_id: int, message: str):
    thread = await get_or_restore_thread(ev, guild, base_message_id)
    if not thread:
        print("⚠️ (Threadlog) " + message)
        return
    try:
        await thread.send(f"⚠️ {message}")
    except Exception:
        pass


async def log_participation_change(
    ev: dict,
    guild: discord.Guild,
    base_message_id: int,
    user_id: int,
    emoji: str,
    action: str,
    slot_type: str = "",
):
    """
    action: "join" oder "leave"
    slot_type: "Hauptslot" / "Warteliste" oder ""
    """
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


# ----------------- Events -----------------
@bot.event
async def on_ready():
    print(f"✅ SlotBot online als {bot.user}")
    loaded = load_events_with_retry()
    active_events.clear()
    active_events.update(loaded)
    print(f"📂 Aktive Events im Speicher: {len(active_events)}")

    bot.loop.create_task(reminder_task())
    bot.loop.create_task(cleanup_task())  # Auto-Cleanup starten

    try:
        await bot.tree.sync()
        print("📂 Slash Commands synchronisiert")
    except Exception as e:
        print(f"❌ Sync-Fehler: {e}")


# ----------------- /help (ausführlich) -----------------
@bot.tree.command(name="help", description="Zeigt eine ausführliche Erklärung aller Befehle an")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 SlotBot – Ausführliche Hilfe",
        description=(
            "Der SlotBot hilft dir, Events zu erstellen, zu verwalten und übersichtlich zu halten.\n"
            "Unten findest du alle Befehle mit Beispielen und Hinweisen."
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="🆕 /event",
        value=(
            "**Beschreibung:** Erstellt ein neues Event mit Slots und Thread.\n"
            "**Pflichtfelder:** `art`, `zweck`, `ort`, `datum`, `zeit`, `level`, `stil`, `slots`\n"
            "**Optional:** `cleanup_hours` (Standard **1h**), `typ`, `gruppenlead`, `anmerkung`\n"
            "**Datum/Zeit:** Wochentag wird automatisch angehängt.\n"
            "**Beispiel:**\n"
            "`/event art:PvE zweck:\"XP Farmen\" ort:\"Calpheon\" datum:27.10.2025 zeit:20:00 level:61+ stil:\"Organisiert\" "
            "slots:\"⚔️:3 🛡️:1 💉:2\" cleanup_hours:24 typ:\"Gruppe\" gruppenlead:\"Matze\" anmerkung:\"Treffpunkt vor der Bank\"`"
        ),
        inline=False,
    )
    embed.add_field(
        name="✏️ /event_edit",
        value=(
            "**Beschreibung:** Bearbeitet **dein** Event (nur Ersteller).\n"
            "**Unterstützt:** `datum`, `zeit`, `ort`, `level`, `anmerkung`, `slots`, `cleanup_hours`\n"
            "**Anzeige:** Alte Werte werden `~~durchgestrichen~~ → neu` angezeigt (nur letzte Änderung)."
        ),
        inline=False,
    )
    embed.add_field(
        name="🗑️ /event_delete",
        value=(
            "**Beschreibung:** Löscht **dein** aktuelles Event (nur Ersteller)."
        ),
        inline=False,
    )
    embed.add_field(
        name="🗓️ /events",
        value=(
            "**Beschreibung:** Zeigt alle **aktiven Events des gesamten Servers** mit Zeit, Ersteller & Channel-Link."
        ),
        inline=False,
    )
    embed.add_field(
        name="ℹ️ /event_info",
        value=(
            "**Beschreibung:** Zeigt Details zu **deinem aktuellen Event** auf diesem Server als Embed.\n"
            "Enthält Basisdaten, Slots (Hauptslot + Warteliste) und einen Direktlink zur Event-Nachricht."
        ),
        inline=False,
    )
    embed.add_field(
        name="📅 Kalenderlinks",
        value=(
            "Bei neuem Event postet der Bot im **Thread**:\n"
            "• Link zu **Google Kalender**\n"
            "• **Apple Kalender** (.ics) — als Datei-Anhang oder Link, wenn `PUBLIC_BASE_URL` gesetzt ist\n"
            "Format: `📅 Kalender: [Google öffnen](...)  |  [Apple (.ics)](...)`"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ----------------- /event -----------------
@bot.tree.command(name="event", description="Erstellt ein Event mit Slots & Thread")
@app_commands.describe(
    art="Art des Events (PvE/PvP/PVX)",
    zweck="Zweck (z. B. EP Farmen)",
    ort="Ort (z. B. Carphin)",
    zeit="Zeit im Format HH:MM",
    datum="Datum im Format DD.MM.YYYY",
    level="Levelbereich",
    stil="Gemütlich oder Organisiert",
    slots="Slots (z. B. ⚔️:2 🛡️:1)",
    cleanup_hours="Optional: In wie vielen Stunden nach Start wird das Event automatisch gelöscht (Default 1)",
    typ="Optional: Gruppe oder Raid",
    gruppenlead="Optional: Gruppenleiter",
    anmerkung="Optional: Freitext",
)
@app_commands.choices(
    art=[app_commands.Choice(name=x, value=x) for x in ["PvE", "PvP", "PVX"]],
    stil=[app_commands.Choice(name=x, value=x) for x in ["Gemütlich", "Organisiert"]],
    typ=[app_commands.Choice(name=x, value=x) for x in ["Gruppe", "Raid"]],
)
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
    cleanup_hours: Optional[app_commands.Range[int, 1, 168]] = None,
    typ: app_commands.Choice[str] = None,
    gruppenlead: str = None,
    anmerkung: str = None,
):
    # Datum/Zeit prüfen
    try:
        local_dt = BERLIN_TZ.localize(datetime.strptime(f"{datum} {zeit}", "%d.%m.%Y %H:%M"))
        utc_dt = local_dt.astimezone(pytz.utc)
        if utc_dt < datetime.now(pytz.utc):
            await interaction.response.send_message("❌ Datum/Zeit liegt in der Vergangenheit!", ephemeral=True)
            return
    except Exception:
        await interaction.response.send_message(
            "❌ Ungültiges Format! Nutze DD.MM.YYYY HH:MM",
            ephemeral=True,
        )
        return

    # Slots parsen (tolerant)
    slot_dict = parse_slots(slots, interaction.guild)
    if slot_dict is None:
        await interaction.response.send_message("❌ Keine gültigen Slots gefunden.", ephemeral=True)
        return
    if isinstance(slot_dict, str):
        await interaction.response.send_message(f"❌ {slot_dict}", ephemeral=True)
        return

    # Header bauen (Cleanup NICHT posten)
    time_str = format_de_datetime(local_dt)
    header = (
        f"📣 **@here — Neue Gruppensuche!**\n\n"
        f"🗡️ **Art:** {art.value}\n"
        f"🎯 **Zweck:** {zweck}\n"
        f"📍 **Ort:** {ort}\n"
        f"🕒 **Datum/Zeit:** {time_str}\n"
        f"⚔️ **Levelbereich:** {level}\n"
        f"💬 **Stil:** {stil.value}\n"
    )
    if typ:
        header += f"🏷️ **Typ:** {typ.value}\n"
    if gruppenlead:
        header += f"👑 **Gruppenlead:** {gruppenlead}\n"
    if anmerkung:
        header += f"📝 **Anmerkung:** {anmerkung}\n"

    # Ephemere hübsche Erfolgsmeldung
    success = discord.Embed(
        title="✅ Event erfolgreich erstellt!",
        description=f"**{zweck}** am **{datum} um {zeit}** wurde angelegt.",
        color=0x2ECC71
    )
    success.add_field(name="Löschzeit", value=f"{(int(cleanup_hours) if cleanup_hours else 1)} Stunden nach Start", inline=True)
    await interaction.response.send_message(embed=success, ephemeral=True)

    # Nachricht als Embed absenden
    try:
        embed = build_event_embed(
            zweck, ort, time_str, level, stil.value, art.value,
            slot_dict, interaction.guild, int(cleanup_hours) if cleanup_hours else DEFAULT_CLEANUP_HOURS
        )
        msg = await interaction.channel.send(embed=embed)
    except discord.errors.Forbidden:
        await interaction.followup.send("❌ Ich darf hier keine Nachrichten senden.", ephemeral=True)
        return
    except discord.errors.HTTPException as e:
        await interaction.followup.send(f"❌ Fehler beim Erstellen des Events: {e}", ephemeral=True)
        return

    # Reaktionen hinzufügen (Emoji-Fix: Fehlerhafte Emojis überspringen)
    failed_emojis = []
    for e in slot_dict.keys():
        try:
            await msg.add_reaction(e)
        except Exception:
            failed_emojis.append(e)

    # Thread erstellen
    thread_id = None
    try:
        thread = await msg.create_thread(
            name=f"Event-Log: {zweck} {datum} {zeit}",
            auto_archive_duration=1440,
        )
        await thread.send(f"🧵 Event-Log für: {zweck} — {msg.jump_url}")
        thread_id = thread.id
        if failed_emojis:
            await thread.send("⚠️ Einige Emojis konnten nicht hinzugefügt werden: " + ", ".join(failed_emojis))
        # Kalenderlinks (Google + Apple)
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

    active_events[msg.id] = {
        "title": zweck,
        "slots": slot_dict,
        "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id,
        "header": header,
        "creator_id": interaction.user.id,
        "event_time": utc_dt,
        "thread_id": thread_id,
        "cleanup_hours": int(cleanup_hours) if cleanup_hours else DEFAULT_CLEANUP_HOURS,
    }
    await safe_save()


# ----------------- /event_edit -----------------
@bot.tree.command(name="event_edit", description="Bearbeite dein Event (Datum, Zeit, Ort, Level, Slots, Anmerkung, Cleanup)")
@app_commands.describe(
    datum="Neues Datum (DD.MM.YYYY)",
    zeit="Neue Zeit (HH:MM)",
    ort="Neuer Ort",
    level="Neuer Levelbereich",
    anmerkung="Neue Anmerkung",
    slots="Neue Slots (z. B. ⚔️:3 🛡️:2)",
    cleanup_hours="Neue Auto-Cleanup-Zeit in Stunden (z. B. 24)",
)
async def event_edit(
    interaction: discord.Interaction,
    datum: str = None,
    zeit: str = None,
    ort: str = None,
    level: str = None,
    anmerkung: str = None,
    slots: str = None,
    cleanup_hours: Optional[app_commands.Range[int, 1, 168]] = None,
):
    own = [
        (mid, ev)
        for mid, ev in active_events.items()
        if int(ev.get("creator_id", 0)) == interaction.user.id
        and ev.get("guild_id") == interaction.guild.id
    ]
    if not own:
        await interaction.response.send_message(
            "❌ Ich finde aktuell kein Event von dir auf diesem Server.",
            ephemeral=True,
        )
        return

    msg_id, ev = max(
        own,
        key=lambda x: x[1].get("event_time", datetime.min.replace(tzinfo=pytz.utc)),
    )
    thread_changes: list[str] = []

    PREFIX_DATE = "🕒 **Datum/Zeit:**"
    PREFIX_ORG = "📍 **Ort:**"
    PREFIX_LEVEL = "⚔️ **Levelbereich:**"

    # Datum/Zeit
    if datum or zeit:
        old_local = ev["event_time"].astimezone(BERLIN_TZ)
        try:
            new_local = BERLIN_TZ.localize(
                datetime.strptime(
                    f"{datum or old_local.strftime('%d.%m.%Y')} {zeit or old_local.strftime('%H:%M')}",
                    "%d.%m.%Y %H:%M",
                )
            )
            new_str = format_de_datetime(new_local)
            current_visible = extract_current_value(ev["header"], rf"^{re.escape(PREFIX_DATE)} ")
            if not current_visible:
                current_visible = format_de_datetime(old_local)
            ev["header"] = replace_with_struck(ev["header"], PREFIX_DATE, current_visible, new_str)
            ev["event_time"] = new_local.astimezone(pytz.utc)
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
            ev["header"] += f"📝 **Anmerkung:** {anmerkung}\n"
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
            await log_error_to_thread(
                ev,
                interaction.guild,
                msg_id,
                "Fehler: Eventnachricht nicht gefunden (Slots neu setzen).",
            )
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

    # Cleanup Hours
    if cleanup_hours is not None:
        ev["cleanup_hours"] = int(cleanup_hours)
        thread_changes.append(f"Cleanup auf {int(cleanup_hours)}h geändert")

    await update_event_message(msg_id)
    await safe_save()
    await interaction.response.send_message("✅ Event aktualisiert.", ephemeral=True)

    if thread_changes:
        guild = interaction.guild
        changes = ", ".join(thread_changes)
        await post_event_update_log(ev, guild, interaction.user.mention, changes, msg_id)
        if any(s.startswith("Datum/Zeit:") for s in thread_changes):
            await post_calendar_links(ev, guild, msg_id)


# ----------------- /event_delete -----------------
@bot.tree.command(name="event_delete", description="Löscht nur dein eigenes Event")
async def event_delete(interaction: discord.Interaction):
    own_events = [
        (mid, ev)
        for mid, ev in active_events.items()
        if int(ev.get("creator_id", 0)) == interaction.user.id
        and ev.get("guild_id") == interaction.guild.id
    ]
    if not own_events:
        await interaction.response.send_message(
            "❌ Ich finde aktuell kein Event von dir auf diesem Server.",
            ephemeral=True,
        )
        return

    msg_id, ev = max(
        own_events,
        key=lambda x: x[1].get("event_time", datetime.min.replace(tzinfo=pytz.utc)),
    )
    try:
        channel = interaction.guild.get_channel(ev["channel_id"])
        msg = await channel.fetch_message(msg_id)
        await msg.delete()
        thread = interaction.guild.get_channel(ev.get("thread_id"))
        if thread:
            try:
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
    if not active_events:
        await interaction.response.send_message(
            "ℹ️ Es sind keine aktiven Events vorhanden.",
            ephemeral=True,
        )
        return

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
        lines.append(
            f"• **{ev['title']}** — {when} — von {creator_name} — {channel_tag} — [zum Event]({jump_url})"
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
    own_events = [
        (mid, ev)
        for mid, ev in active_events.items()
        if int(ev.get("creator_id", 0)) == interaction.user.id
        and ev.get("guild_id") == interaction.guild.id
    ]

    if not own_events:
        await interaction.response.send_message(
            "ℹ️ Ich finde aktuell kein Event von dir auf diesem Server.",
            ephemeral=True,
        )
        return

    msg_id, ev = max(
        own_events,
        key=lambda x: x[1].get("event_time", datetime.min.replace(tzinfo=pytz.utc)),
    )

    guild = interaction.guild

    embed = discord.Embed(
        title=f"📋 Event-Info: {ev['title']}",
        color=0x3498DB,
    )

    embed.add_field(
        name="📄 Basisdaten",
        value=ev["header"],
        inline=False,
    )

    slot_lines: list[str] = []
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

    if slot_lines:
        embed.add_field(
            name="🎟️ Slots",
            value="\n".join(slot_lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="🎟️ Slots",
            value="Keine Slots vorhanden.",
            inline=False,
        )

    jump_url = f"https://discord.com/channels/{guild.id}/{ev['channel_id']}/{msg_id}"
    embed.add_field(
        name="🔗 Direkt zum Event",
        value=f"[Hier klicken]({jump_url})",
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ----------------- Reaction Handling (robust) -----------------
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

    # Thread-Log: Anmeldung
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

    # Thread-Log: Abmeldung
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

    # DM + Thread-Log bei Promotion
    if promoted_user is not None:
        try:
            member = guild.get_member(promoted_user) or await guild.fetch_member(promoted_user)
            await member.send(f"🎟️ Du bist jetzt im **Hauptslot** für **{ev['title']}**! Viel Spaß 🎉")
        except Exception:
            pass
        try:
            await log_error_to_thread(
                ev,
                guild,
                payload.message_id,
                f"🔄 <@{promoted_user}> wurde automatisch aus der Warteliste in den Hauptslot verschoben.",
            )
        except Exception:
            pass


# ----------------- Flask (Render) -----------------
flask_app = Flask("bot_flask")


@flask_app.route("/")
def index():
    return "✅ SlotBot läuft (Render kompatibel)."


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


def run_bot():
    asyncio.run(bot.start(TOKEN))


if __name__ == "__main__":
    print("🚀 Starte SlotBot v4.3.3 + Flask ...")
    active_events.update(load_events_with_retry())
    Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
