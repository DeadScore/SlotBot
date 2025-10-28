# main.py — SlotBot (Classic, vollständige Version)
# Features:
# - /event, /event_edit, /event_delete, /help (Embed)
# - Deutsche Wochentage im Datum
# - Strike-Through bei Änderungen (immer nur letzte Änderung)
# - Thread-Log (Auto-Unarchive, Notfall-Neuerstellung)
# - Google-Kalender Button (öffentlich)
# - 10-Minuten-Reminder per DM
# - Persistenz über GitHub (data/events.json)
# - Slots robust mit/ohne Leerzeichen
# - Flask für Render

import os
import re
import json
import asyncio
import base64
import requests
from datetime import datetime, timedelta
from threading import Thread
import pytz
from urllib.parse import quote_plus

import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask

# ----------------- Konfiguration -----------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("❌ DISCORD_TOKEN nicht gesetzt. Bitte als Environment Variable konfigurieren.")
    raise SystemExit(1)

CUSTOM_EMOJI_REGEX = r"<a?:\w+:\d+>"
BERLIN_TZ = pytz.timezone("Europe/Berlin")

# ----------------- Intents & Bot -----------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# In-Memory
active_events = {}  # message_id -> event data

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
    """Formatiert z. B.: 'Samstag, 25.10.2025 20:00 CET'."""
    en = local_dt.strftime("%A")
    de = WEEKDAY_DE.get(en, en)
    return local_dt.strftime(f"%A, %d.%m.%Y %H:%M %Z").replace(en, de)

def to_google_dates(start_utc: datetime, duration_hours: int = 2) -> str:
    """
    Google Calendar erwartet UTC im Format YYYYMMDDTHHMMSSZ/...
    Ende = Start + duration_hours (Standard 2h)
    """
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

# ----------------- Slots / Emojis -----------------
def normalize_emoji(emoji):
    if isinstance(emoji, str):
        return emoji.strip()
    if hasattr(emoji, "id") and emoji.id:
        return f"<:{emoji.name}:{emoji.id}>"
    return emoji.name

def is_valid_emoji(emoji, guild):
    if re.match(CUSTOM_EMOJI_REGEX, emoji):
        return any(str(e) == emoji for e in guild.emojis)
    return True

# erlaubt: '<:Tank:123>:3' oder '⚔️:2' oder '⚔️ : 2'
SLOT_PATTERN = re.compile(r"(<a?:\w+:\d+>|[^\s:]+)\s*:\s*(\d+)")

def parse_slots(slots_str: str, guild: discord.Guild):
    """Akzeptiert: '⚔️:2 🛡️:1' oder '⚔️ : 2' oder '<:Tank:123>: 3'"""
    matches = SLOT_PATTERN.findall(slots_str or "")
    if not matches:
        return None
    slot_dict = {}
    for emoji, limit in matches:
        em = normalize_emoji(emoji)
        if not is_valid_emoji(em, guild):
            return f"Ungültiges Emoji: {em}"
        slot_dict[em] = {"limit": int(limit), "main": set(), "waitlist": [], "reminded": set()}
    return slot_dict

def format_event_text(event, guild):
    text = "**📋 Eventübersicht:**\n"
    for emoji, slot in event["slots"].items():
        main_users = [guild.get_member(uid).mention for uid in slot["main"] if guild.get_member(uid)]
        wait_users = [guild.get_member(uid).mention for uid in slot["waitlist"] if guild.get_member(uid)]
        text += f"\n{emoji} ({len(main_users)}/{slot['limit']}): "
        text += ", ".join(main_users) if main_users else "-"
        if wait_users:
            text += f"\n   ⏳ Warteliste: " + ", ".join(wait_users)
    return text

# ----------------- Strike-Through Utilities -----------------
def extract_current_value(header: str, prefix_regex: str) -> str:
    """
    Holt den aktuell sichtbaren Wert (nach einem evtl. bestehenden '~~alt~~ → neu').
    prefix_regex Beispiel: r'^📍 \*\*Ort:\*\* '
    """
    m = re.search(prefix_regex + r"(.*)$", header, re.M)
    if not m:
        return ""
    val = m.group(1).strip()
    if "~~" in val and "→" in val:
        parts = val.split("→", 1)
        return parts[1].strip()
    return val

def replace_with_struck(header: str, prefix_label: str, old_visible: str, new_value: str) -> str:
    """
    Ersetzt die komplette Zeile mit 'prefix_label ~~alt~~ → neu'.
    Falls es schon eine Strike-Zeile gibt, wird nur der sichtbare Teil aktualisiert.
    """
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
    # Zeile fehlt -> anhängen
    return header.rstrip() + f"\n{prefix_label} ~~{old_visible or '?'}~~ → {new_value}"

async def update_event_message(message_id):
    ev = active_events.get(message_id)
    if not ev:
        return
    guild = bot.get_guild(ev["guild_id"])
    if not guild:
        return
    channel = guild.get_channel(ev["channel_id"])
    if not channel:
        return
    try:
        msg = await channel.fetch_message(int(message_id))
        await msg.edit(content=ev["header"] + "\n\n" + format_event_text(ev, guild))
    except Exception as e:
        print(f"❌ Fehler beim Aktualisieren: {e}")

# ----------------- GitHub Speicherfunktionen -----------------
def load_events():
    """Lädt events.json aus dem GitHub-Repo."""
    repo = os.getenv("GITHUB_REPO")
    path = os.getenv("GITHUB_FILE_PATH", "data/events.json")
    token = os.getenv("GITHUB_TOKEN")
    if not all([repo, path, token]):
        print("⚠️ GitHub-Umgebungsvariablen fehlen – starte ohne Persistenz.")
        return {}
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}"}
    try:
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            data = json.loads(base64.b64decode(r.json()["content"]))
            for ev in data.values():
                for s in ev["slots"].values():
                    s["main"] = set(s.get("main", []))
                    s["waitlist"] = list(s.get("waitlist", []))
                    s["reminded"] = set(s.get("reminded", []))
            print("✅ events.json erfolgreich von GitHub geladen.")
            return {int(k): v for k, v in data.items()}
        elif r.status_code == 404:
            print("ℹ️ Keine events.json gefunden – starte leer.")
            return {}
        else:
            print(f"⚠️ Fehler beim Laden: HTTP {r.status_code}")
    except Exception as e:
        print(f"❌ Fehler beim Laden von events.json: {e}")
    return {}

def save_events():
    """Speichert events.json im GitHub-Repo."""
    repo = os.getenv("GITHUB_REPO")
    path = os.getenv("GITHUB_FILE_PATH", "data/events.json")
    token = os.getenv("GITHUB_TOKEN")
    if not all([repo, path, token]):
        print("⚠️ GitHub-Umgebungsvariablen fehlen – kann events.json nicht speichern.")
        return
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}"}
    try:
        get_resp = requests.get(url, headers=headers)
        sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None

        serializable = {}
        for mid, ev in active_events.items():
            copy = json.loads(json.dumps(ev))
            for s in copy["slots"].values():
                s["main"] = list(s["main"])
                s["reminded"] = list(s["reminded"])
            serializable[str(mid)] = copy

        encoded_content = base64.b64encode(json.dumps(serializable, indent=4).encode()).decode()
        data = {"message": "Update events.json via SlotBot", "content": encoded_content, "sha": sha}
        resp = requests.put(url, headers=headers, json=data)
        if resp.status_code in [200, 201]:
            print("💾 events.json erfolgreich auf GitHub gespeichert.")
        else:
            print(f"⚠️ Fehler beim Speichern auf GitHub: HTTP {resp.status_code}")
    except Exception as e:
        print(f"❌ Fehler beim Speichern: {e}")

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
                for user_id in slot["main"]:
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

# ----------------- Views -----------------
class CalendarView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="📆 Zum Google Kalender hinzufügen", url=url))

# ----------------- Events -----------------
@bot.event
async def on_ready():
    global active_events
    print(f"✅ SlotBot online als {bot.user}")
    active_events = load_events()
    bot.loop.create_task(reminder_task())
    try:
        await bot.tree.sync()
        print("📂 Slash Commands synchronisiert")
    except Exception as e:
        print(f"❌ Sync-Fehler: {e}")

# ----------------- /help (Embed) -----------------
@bot.tree.command(name="help", description="Zeigt alle verfügbaren Befehle und Beispiele an")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 SlotBot Hilfe",
        description="Schnellüberblick über die verfügbaren Slash-Commands.",
        color=0x5865F2
    )
    embed.add_field(
        name="🆕 /event",
        value=(
            "Erstellt ein Event.\n"
            "**Beispiel:**\n"
            "`/event art:PvE zweck:\"XP Farmen\" ort:\"Calpheon\" datum:27.10.2025 zeit:20:00 "
            "level:61+ stil:\"Organisiert\" slots:\"⚔️:3 🛡️:1 💉:2\" typ:\"Gruppe\" gruppenlead:\"Matze\" anmerkung:\"Treffpunkt vor der Bank\"`"
        ),
        inline=False
    )
    embed.add_field(
        name="✏️ /event_edit",
        value=(
            "Bearbeite **dein** Event (nur Ersteller). Alte Werte werden `~~durchgestrichen~~ → neu` angezeigt.\n"
            "**Beispiel:**\n"
            "`/event_edit datum:28.10.2025 zeit:21:00 ort:\"Velia\" level:62+ slots:\"⚔️:2 🛡️:2\" anmerkung:\"10 Min früher treffen\"`"
        ),
        inline=False
    )
    embed.add_field(
        name="❌ /event_delete",
        value="Löscht dein aktuelles Event im Channel. ` /event_delete `",
        inline=False
    )
    embed.add_field(
        name="ℹ️ Hinweise",
        value=(
            "• 🔔 10-Minuten-Reminder per DM\n"
            "• 💾 Persistenz via GitHub (`data/events.json`)\n"
            "• ✨ Änderungen (Datum/Ort/Level) zeigen **immer nur die letzte** alte Angabe\n"
            "• 🧵 Änderungen werden im Thread-Log dokumentiert (Auto-Unarchive)\n"
            "• 🔤 Slots akzeptieren `⚔️:2`, `⚔️ : 2` oder `<:Tank:123>: 3`\n"
            "• 📆 Google Kalender Button erscheint öffentlich unter dem Event"
        ),
        inline=False
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
    typ="Optional: Gruppe oder Raid",
    gruppenlead="Optional: Gruppenleiter",
    anmerkung="Optional: Freitext"
)
@app_commands.choices(
    art=[app_commands.Choice(name=x, value=x) for x in ["PvE", "PvP", "PVX"]],
    stil=[app_commands.Choice(name=x, value=x) for x in ["Gemütlich", "Organisiert"]],
    typ=[app_commands.Choice(name=x, value=x) for x in ["Gruppe", "Raid"]]
)
async def event(interaction: discord.Interaction,
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
                anmerkung: str = None):

    # Datum/Zeit prüfen
    try:
        local_dt = BERLIN_TZ.localize(datetime.strptime(f"{datum} {zeit}", "%d.%m.%Y %H:%M"))
        utc_dt = local_dt.astimezone(pytz.utc)
        if utc_dt < datetime.now(pytz.utc):
            await interaction.response.send_message("❌ Datum/Zeit liegt in der Vergangenheit!", ephemeral=True)
            return
    except Exception:
        await interaction.response.send_message("❌ Ungültiges Format! Nutze DD.MM.YYYY HH:MM", ephemeral=True)
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

    # Öffentliche Kalender-URL (inkl. Beschreibung)
    description = (
        f"Art: {art.value}\n"
        f"Zweck: {zweck}\n"
        f"Ort: {ort}\n"
        f"Datum/Zeit: {time_str}\n"
        f"Level: {level}\n"
        f"Stil: {stil.value}\n"
        + (f"Typ: {typ.value}\n" if typ else "")
        + (f"Gruppenlead: {gruppenlead}\n" if gruppenlead else "")
        + (f"Anmerkung: {anmerkung}" if anmerkung else "")
    )
    gcal_url = build_google_calendar_url(
        title=f"{zweck} ({art.value})",
        start_utc=utc_dt,
        location=ort,
        description=description
    )
    view = CalendarView(gcal_url)

    # Nachricht + Reaktionen + View
    await interaction.response.send_message("✅ Event erstellt!", ephemeral=True)
    msg = await interaction.channel.send(header + "\n\n" + format_event_text({"slots": slot_dict}, interaction.guild), view=view)

    for e in slot_dict.keys():
        try:
            await msg.add_reaction(e)
        except Exception:
            pass

    # Thread
    try:
        thread = await msg.create_thread(name=f"Event-Log: {zweck} {datum} {zeit}", auto_archive_duration=1440)
        await thread.send(f"🧵 Event-Log für: {zweck} — {msg.jump_url}")
        thread_id = thread.id
    except Exception as e:
        print(f"⚠️ Thread konnte nicht erstellt werden: {e}")
        thread_id = None

    # Speichern
    active_events[msg.id] = {
        "title": zweck,
        "slots": slot_dict,
        "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id,
        "header": header,
        "creator_id": interaction.user.id,
        "event_time": utc_dt,
        "thread_id": thread_id,
    }
    save_events()

# ----------------- /event_edit -----------------
@bot.tree.command(name="event_edit", description="Bearbeite dein Event (Datum, Zeit, Ort, Level, Slots, Anmerkung)")
@app_commands.describe(
    datum="Neues Datum (DD.MM.YYYY)",
    zeit="Neue Zeit (HH:MM)",
    ort="Neuer Ort",
    level="Neuer Levelbereich",
    anmerkung="Neue Anmerkung",
    slots="Neue Slots (z. B. ⚔️:3 🛡️:2)"
)
async def event_edit(interaction: discord.Interaction,
                     datum: str = None, zeit: str = None, ort: str = None,
                     level: str = None, anmerkung: str = None, slots: str = None):
    own = [(mid, ev) for mid, ev in active_events.items()
           if ev["creator_id"] == interaction.user.id and ev["channel_id"] == interaction.channel.id]
    if not own:
        await interaction.response.send_message("❌ Du hast hier kein eigenes Event.", ephemeral=True)
        return

    msg_id, ev = max(own, key=lambda x: x[0])
    thread_changes = []

    PREFIX_DATE = "🕒 **Datum/Zeit:**"
    PREFIX_ORG = "📍 **Ort:**"
    PREFIX_LEVEL = "⚔️ **Levelbereich:**"

    # Datum/Zeit
    if datum or zeit:
        old_local = ev["event_time"].astimezone(BERLIN_TZ)
        try:
            new_local = BERLIN_TZ.localize(datetime.strptime(
                f"{datum or old_local.strftime('%d.%m.%Y')} {zeit or old_local.strftime('%H:%M')}",
                "%d.%m.%Y %H:%M"
            ))
            new_str = format_de_datetime(new_local)
            current_visible = extract_current_value(ev["header"], rf"^{re.escape(PREFIX_DATE)} ")
            if not current_visible:
                current_visible = format_de_datetime(old_local)
            ev["header"] = replace_with_struck(ev["header"], PREFIX_DATE, current_visible, new_str)
            ev["event_time"] = new_local.astimezone(pytz.utc)
            thread_changes.append(f"Datum/Zeit: ~~{current_visible}~~ → {new_str}")
        except Exception:
            await interaction.response.send_message("❌ Fehler im Datumsformat (DD.MM.YYYY / HH:MM).", ephemeral=True)
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

    # Anmerkung (ohne Strike)
    if anmerkung:
        if "📝 **Anmerkung:**" in ev["header"]:
            ev["header"] = re.sub(r"📝 \*\*Anmerkung:\*\* .+", f"📝 **Anmerkung:** {anmerkung}", ev["header"])
        else:
            ev["header"] += f"📝 **Anmerkung:** {anmerkung}\n"
        thread_changes.append("Anmerkung aktualisiert")

    # Slots
    if slots:
        parsed = parse_slots(slots, interaction.guild)
        if parsed is None or isinstance(parsed, str):
            await interaction.response.send_message("❌ Ungültige Slots. Beispiel: ⚔️:2 🛡️:1", ephemeral=True)
            return
        ev["slots"] = parsed
        guild = interaction.guild
        channel = guild.get_channel(ev["channel_id"])
        msg = await channel.fetch_message(msg_id)
        try:
            await msg.clear_reactions()
        except Exception:
            pass
        for emoji in ev["slots"].keys():
            try:
                await msg.add_reaction(emoji)
            except Exception:
                pass
        thread_changes.append("Slots angepasst")

    # Nachricht & Speicherung
    await update_event_message(msg_id)
    save_events()
    await interaction.response.send_message("✅ Event aktualisiert.", ephemeral=True)

    # Thread-Log (mit Auto-Unarchive; wenn kein thread_id, neu erzeugen)
    thread_id = ev.get("thread_id")
    thread = interaction.guild.get_channel(thread_id) if thread_id else None
    if not thread:
        try:
            channel = interaction.guild.get_channel(ev["channel_id"])
            base_msg = await channel.fetch_message(msg_id)
            thread = await base_msg.create_thread(name=f"Event-Log (neu): {ev['title']}", auto_archive_duration=1440)
            ev["thread_id"] = thread.id
            save_events()
        except Exception as e:
            print(f"⚠️ Konnte keinen Thread erstellen: {e}")
            thread = None

    if thread:
        try:
            if getattr(thread, "archived", False):
                await thread.edit(archived=False)
            changes = ", ".join(thread_changes) if thread_changes else "Details geändert"
            await thread.send(f"✏️ **{interaction.user.mention}** hat das Event bearbeitet ({changes}).")
        except Exception as e:
            print(f"⚠️ Thread-Update fehlgeschlagen: {e}")

# ----------------- /event_delete -----------------
@bot.tree.command(name="event_delete", description="Löscht nur dein eigenes Event")
async def event_delete(interaction: discord.Interaction):
    own_events = [
        (mid, ev) for mid, ev in active_events.items()
        if ev["creator_id"] == interaction.user.id and ev["channel_id"] == interaction.channel.id
    ]
    if not own_events:
        await interaction.response.send_message("❌ Du hast hier kein eigenes Event.", ephemeral=True)
        return

    msg_id, ev = max(own_events, key=lambda x: x[0])
    try:
        channel = interaction.channel
        msg = await channel.fetch_message(msg_id)
        await msg.delete()
        thread = interaction.guild.get_channel(ev.get("thread_id"))
        if thread:
            try:
                await thread.delete()
            except Exception:
                pass
        del active_events[msg_id]
        save_events()
        await interaction.response.send_message("✅ Dein Event wurde gelöscht.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler beim Löschen: {e}", ephemeral=True)

# ----------------- Reaction Handling -----------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    ev = active_events.get(payload.message_id)
    if not ev:
        return
    emoji = normalize_emoji(payload.emoji)
    if emoji not in ev["slots"]:
        return
    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
    channel = guild.get_channel(payload.channel_id)
    msg = await channel.fetch_message(payload.message_id)

    # Nur eine Slot-Reaktion pro Nutzer erlauben
    for e in ev["slots"]:
        if e != emoji:
            try:
                await msg.remove_reaction(e, member)
            except Exception:
                pass

    # Prüfen, ob Nutzer schon irgendwo drin ist
    if any(payload.user_id in s["main"] or payload.user_id in s["waitlist"] for s in ev["slots"].values()):
        return

    slot = ev["slots"][emoji]
    if len(slot["main"]) < slot["limit"]:
        slot["main"].add(payload.user_id)
    else:
        slot["waitlist"].append(payload.user_id)

    await update_event_message(payload.message_id)
    save_events()

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    ev = active_events.get(payload.message_id)
    if not ev:
        return
    emoji = normalize_emoji(payload.emoji)
    if emoji not in ev["slots"]:
        return
    slot = ev["slots"][emoji]
    user_id = payload.user_id

    if user_id in slot["main"]:
        slot["main"].remove(user_id)
        if slot["waitlist"]:
            next_user = slot["waitlist"].pop(0)
            slot["main"].add(next_user)
    elif user_id in slot["waitlist"]:
        slot["waitlist"].remove(user_id)

    await update_event_message(payload.message_id)
    save_events()

# ----------------- Flask (Render) -----------------
flask_app = Flask("bot_flask")

@flask_app.route("/")
def index():
    return "✅ SlotBot läuft (Render kompatibel)."

def run_bot():
    asyncio.run(bot.start(TOKEN))

if __name__ == "__main__":
    print("🚀 Starte SlotBot + Flask ...")
    Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
