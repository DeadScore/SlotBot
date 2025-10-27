# main.py
import os
import re
import json
import asyncio
import base64
import requests
from datetime import datetime
from threading import Thread
import pytz

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
active_events = {}  # message_id -> event data

# ----------------- Hilfsfunktionen (Datum / Format) -----------------
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
    """Formatiert ein tz-aware Datum in Deutsch mit Wochentag, z.B. 'Samstag, 25.10.2025 20:00 CEST'."""
    en = local_dt.strftime("%A")
    de = WEEKDAY_DE[en]
    return local_dt.strftime(f"%A, %d.%m.%Y %H:%M %Z").replace(en, de)

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

def parse_slots(slots_str: str, guild: discord.Guild):
    """
    Erwartet z.B.: "⚔️:3 🛡️:2 <:Custom:1234567890>:4"
    Gibt dict zurück: {emoji: {"limit": int, "main": set(), "waitlist": [], "reminded": set()}}
    """
    slot_pattern = re.compile(r"(<a?:\w+:\d+>|\S+)\s*:\s*(\d+)")
    matches = slot_pattern.findall(slots_str or "")
    if not matches:
        return None
    slot_dict = {}
    for emoji, limit in matches:
        emoji = normalize_emoji(emoji)
        if not is_valid_emoji(emoji, guild):
            return f"Ungültiges Emoji: {emoji}"
        slot_dict[emoji] = {"limit": int(limit), "main": set(), "waitlist": [], "reminded": set()}
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
        content = ev["header"] + "\n\n" + format_event_text(ev, guild)
        await msg.edit(content=content)
    except Exception as e:
        print(f"❌ Fehler beim Aktualisieren: {e}")

# ----------------- GitHub Speicherfunktionen -----------------
def load_events():
    repo = os.getenv("GITHUB_REPO")
    path = os.getenv("GITHUB_FILE_PATH", "data/events.json")
    token = os.getenv("GITHUB_TOKEN")
    if not all([repo, path, token]):
        print("⚠️ GitHub-Variablen fehlen.")
        return {}
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}"}
    try:
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            content = base64.b64decode(resp.json()["content"])
            data = json.loads(content)
            # Sets rekonstruieren
            for ev in data.values():
                for s in ev["slots"].values():
                    s["main"] = set(s.get("main", []))
                    s["waitlist"] = list(s.get("waitlist", []))
                    s["reminded"] = set(s.get("reminded", []))
            print("✅ events.json von GitHub geladen.")
            return {int(k): v for k, v in data.items()}
        elif resp.status_code == 404:
            print("ℹ️ Keine events.json gefunden – starte leer.")
            return {}
        else:
            print(f"⚠️ Laden fehlgeschlagen (HTTP {resp.status_code})")
            return {}
    except Exception as e:
        print(f"❌ Fehler beim Laden: {e}")
        return {}

def save_events():
    repo = os.getenv("GITHUB_REPO")
    path = os.getenv("GITHUB_FILE_PATH", "data/events.json")
    token = os.getenv("GITHUB_TOKEN")
    if not all([repo, path, token]):
        print("⚠️ GitHub-Variablen fehlen.")
        return
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}"}
    try:
        old = requests.get(url, headers=headers)
        sha = old.json().get("sha") if old.status_code == 200 else None

        serializable = {}
        for mid, ev in active_events.items():
            copy = json.loads(json.dumps(ev))
            for s in copy["slots"].values():
                s["main"] = list(s["main"])
                s["reminded"] = list(s["reminded"])
            serializable[str(mid)] = copy

        encoded = base64.b64encode(json.dumps(serializable, indent=4).encode()).decode()
        data = {"message": "Update via bot", "content": encoded, "sha": sha}
        r = requests.put(url, headers=headers, json=data)
        if r.status_code in [200, 201]:
            print("💾 events.json auf GitHub gespeichert.")
        else:
            print(f"⚠️ Speichern fehlgeschlagen (HTTP {r.status_code})")
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
                    if 0 <= (event_time - now).total_seconds() <= 600:
                        try:
                            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
                            await member.send(f"⏰ Dein Event **{ev['title']}** startet in 10 Minuten!")
                            slot["reminded"].add(user_id)
                        except Exception:
                            pass
        await asyncio.sleep(60)

# ----------------- Bot Lifecycle -----------------
@bot.event
async def on_ready():
    global active_events
    print(f"✅ Bot online als {bot.user}")
    active_events = load_events()
    bot.loop.create_task(reminder_task())
    try:
        await bot.tree.sync()
        print("📂 Slash Commands synchronisiert")
    except Exception as e:
        print(f"❌ Sync-Fehler: {e}")

# ----------------- /help -----------------
@bot.tree.command(name="help", description="Zeigt alle verfügbaren Befehle und Beispiele an")
async def help_command(interaction: discord.Interaction):
    help_text = (
        "## 📖 **Event-Bot Hilfe**\n"
        "Hier findest du alle verfügbaren Befehle und Beispiele zur Nutzung.\n\n"
        "### 🆕 `/event`\n"
        "Erstellt ein neues Event mit Datum, Zeit, Ort und Slots.\n"
        "Beispiel:\n"
        "```/event art:PvE zweck:\"XP Farmen\" ort:\"Calpheon\" datum:27.10.2025 zeit:20:00 level:61+ "
        "stil:\"Organisiert\" slots:\"⚔️:3 🛡️:1 💉:2\" typ:\"Gruppe\" gruppenlead:\"Matze\" anmerkung:\"Treffpunkt vor der Bank\"```\n"
        "➡️ Erstellt ein Event mit Reaktions-Slots und Thread.\n\n"
        "### ✏️ `/event_edit`\n"
        "Bearbeite dein bestehendes Event (nur vom Ersteller möglich). Alte Werte werden **durchgestrichen** angezeigt.\n"
        "Beispiel:\n"
        "```/event_edit datum:28.10.2025 zeit:21:00 ort:\"Velia\" level:62+ anmerkung:\"Treffen 10 Min früher\"```\n"
        "➡️ Aktualisiert Werte und loggt es im Event-Thread.\n\n"
        "### 🔁 Slots bearbeiten\n"
        "```/event_edit slots:\"⚔️:2 🛡️:2 💉:1\"```\n\n"
        "### ❌ `/event_delete`\n"
        "```/event_delete```\n\n"
        "### ℹ️ `/help`\n"
        "Zeigt diese Hilfe an.\n\n"
        "### 💡 **Hinweise:**\n"
        "- 🔔 10-Minuten-Reminder per DM\n"
        "- 💾 Persistenz via GitHub (`data/events.json`)\n"
        "- ✨ Änderungen an Datum/Ort/Level zeigen den letzten alten Wert ~~durchgestrichen~~\n"
        "- 🧵 Änderungen werden im Thread-Log dokumentiert\n"
    )
    await interaction.response.send_message(help_text, ephemeral=True)

# ----------------- /event -----------------
@bot.tree.command(name="event", description="Erstellt ein Event mit Slots & Thread")
@app_commands.describe(
    art="Art des Events (PvE/PvP/PVX)",
    zweck="Zweck (z. B. EP Farmen)",
    ort="Ort (z. B. Calpheon)",
    zeit="Zeit im Format HH:MM",
    datum="Datum im Format DD.MM.YYYY",
    level="Levelbereich",
    stil="Gemütlich oder Organisiert",
    slots="Slots (z. B. ⚔️:2, 🛡️:1)",
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

    # Datum/Zeit prüfen & formatieren
    try:
        local_dt = BERLIN_TZ.localize(datetime.strptime(f"{datum} {zeit}", "%d.%m.%Y %H:%M"))
        utc_dt = local_dt.astimezone(pytz.utc)
    except Exception:
        await interaction.response.send_message("❌ Ungültiges Format. Bitte DD.MM.YYYY und HH:MM nutzen.", ephemeral=True)
        return

    time_str = format_de_datetime(local_dt)

    # Slots parsen
    parsed = parse_slots(slots, interaction.guild)
    if parsed is None:
        await interaction.response.send_message("❌ Keine gültigen Slots gefunden.", ephemeral=True)
        return
    if isinstance(parsed, str):
        await interaction.response.send_message(f"❌ {parsed}", ephemeral=True)
        return
    slot_dict = parsed

    # Header aufbauen
    header = (
        f"📣 **@here — Neue Gruppensuche!**\n\n"
        f"🗡️ **Art:** {art.value}\n"
        f"🎯 **Zweck:** {zweck}\n"
        f"📍 **Ort:** {ort}\n"
        f"🕒 **Datum/Zeit:** {time_str}\n"
        f"⚔️ **Levelbereich:** {level}\n"
        f"💬 **Stil:** {stil.value}\n"
    )
    if typ: header += f"🏷️ **Typ:** {typ.value}\n"
    if gruppenlead: header += f"👑 **Gruppenlead:** {gruppenlead}\n"
    if anmerkung: header += f"📝 **Anmerkung:** {anmerkung}\n"

    # Nachricht + Reaktionen
    msg = await interaction.channel.send(header + "\n\n" + format_event_text({"slots": slot_dict}, interaction.guild))
    await interaction.response.send_message("✅ Event erstellt!", ephemeral=True)
    for e in slot_dict.keys():
        try:
            await msg.add_reaction(e)
        except Exception:
            pass

    # Thread anlegen
    try:
        thread = await msg.create_thread(name=f"Event-Log: {zweck}", auto_archive_duration=1440)
        await thread.send(f"🧵 Event-Log gestartet — {interaction.user.mention} hat ein neues Event erstellt.")
        thread_id = thread.id
    except Exception:
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
        "thread_id": thread_id
    }
    save_events()

# ----------------- /event_edit -----------------
@bot.tree.command(name="event_edit", description="Bearbeite dein Event (Datum, Ort, Level, Slots, Anmerkung)")
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
    changed_fields = []

    # Datum/Zeit
    if datum or zeit:
        old_local = ev["event_time"].astimezone(BERLIN_TZ)
        try:
            new_local = BERLIN_TZ.localize(datetime.strptime(
                f"{datum or old_local.strftime('%d.%m.%Y')} {zeit or old_local.strftime('%H:%M')}",
                "%d.%m.%Y %H:%M"
            ))
            new_str = format_de_datetime(new_local)
            old_str = format_de_datetime(old_local)
            ev["header"] = re.sub(
                r"🕒 \*\*Datum/Zeit:\*\* .+",
                f"🕒 **Datum/Zeit:** ~~{old_str}~~ → {new_str}",
                ev["header"]
            )
            ev["event_time"] = new_local.astimezone(pytz.utc)
            changed_fields.append("Datum/Zeit")
        except Exception:
            await interaction.response.send_message("❌ Fehler im Datumsformat.", ephemeral=True)
            return

    # Ort
    if ort:
        match = re.search(r"📍 \*\*Ort:\*\* (.+)", ev["header"])
        old_ort = match.group(1) if match else "?"
        ev["header"] = re.sub(r"📍 \*\*Ort:\*\* .+", f"📍 **Ort:** ~~{old_ort}~~ → {ort}", ev["header"])
        changed_fields.append("Ort")

    # Level
    if level:
        match = re.search(r"⚔️ \*\*Levelbereich:\*\* (.+)", ev["header"])
        old_lvl = match.group(1) if match else "?"
        ev["header"] = re.sub(r"⚔️ \*\*Levelbereich:\*\* .+", f"⚔️ **Levelbereich:** ~~{old_lvl}~~ → {level}", ev["header"])
        changed_fields.append("Level")

    # Anmerkung
    if anmerkung:
        if "📝 **Anmerkung:**" in ev["header"]:
            ev["header"] = re.sub(r"📝 \*\*Anmerkung:\*\* .+", f"📝 **Anmerkung:** {anmerkung}", ev["header"])
        else:
            ev["header"] += f"📝 **Anmerkung:** {anmerkung}\n"
        changed_fields.append("Anmerkung")

    # Slots
    if slots:
        parsed = parse_slots(slots, interaction.guild)
        if parsed is None or isinstance(parsed, str):
            await interaction.response.send_message(f"❌ Ungültige Slots. Beispiel: ⚔️:2 🛡️:1", ephemeral=True)
            return
        ev["slots"] = parsed
        # Reaktionen neu setzen
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
        changed_fields.append("Slots")

    # Nachricht & Speicherung
    await update_event_message(msg_id)
    save_events()
    await interaction.response.send_message("✅ Event aktualisiert.", ephemeral=True)

    # Thread-Log
    thread_id = ev.get("thread_id")
    if thread_id:
        thread = interaction.guild.get_channel(thread_id)
        if thread:
            changes = ", ".join(changed_fields) if changed_fields else "Details"
            await thread.send(f"✏️ **{interaction.user.mention}** hat das Event bearbeitet ({changes}).")

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

# ----------------- Flask -----------------
flask_app = Flask("bot_flask")

@flask_app.route("/")
def index():
    return "✅ Discord-Bot läuft (Render kompatibel)."

def run_bot():
    asyncio.run(bot.start(TOKEN))

if __name__ == "__main__":
    print("🚀 Starte Bot + Flask ...")
    Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
