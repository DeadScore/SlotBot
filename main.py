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
active_events = {}

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
            for ev in data.values():
                for s in ev["slots"].values():
                    s["main"] = set(s.get("main", []))
                    s["waitlist"] = list(s.get("waitlist", []))
                    s["reminded"] = set(s.get("reminded", []))
            print("✅ events.json von GitHub geladen.")
            return {int(k): v for k, v in data.items()}
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
        print("💾 events.json auf GitHub gespeichert." if r.status_code in [200, 201] else f"⚠️ Fehler: {r.status_code}")
    except Exception as e:
        print(f"❌ Fehler beim Speichern: {e}")

# ----------------- Hilfsfunktionen -----------------
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
    channel = guild.get_channel(ev["channel_id"])
    try:
        msg = await channel.fetch_message(int(message_id))
        await msg.edit(content=ev["header"] + "\n\n" + format_event_text(ev, guild))
    except Exception as e:
        print(f"❌ Fehler beim Aktualisieren: {e}")

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
                        except:
                            pass
        await asyncio.sleep(60)

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
        "Bearbeite dein bestehendes Event (nur vom Ersteller möglich).\n"
        "Beim Bearbeiten werden alte Werte automatisch **durchgestrichen** angezeigt.\n"
        "Beispiel:\n"
        "```/event_edit datum:28.10.2025 zeit:21:00 ort:\"Velia\" level:62+ anmerkung:\"Treffen 10 Min früher\"```\n"
        "➡️ Aktualisiert die Werte und schreibt automatisch eine Meldung ins Event-Thread-Log.\n\n"

        "### 🔁 Slots bearbeiten\n"
        "Ändert direkt die Reaktions-Slots (alte Reaktionen werden automatisch entfernt und neu gesetzt).\n"
        "Beispiel:\n"
        "```/event_edit slots:\"⚔️:2 🛡️:2 💉:1\"```\n\n"

        "### ❌ `/event_delete`\n"
        "Löscht dein eigenes Event (Nachricht, Thread und gespeicherte Daten).\n"
        "Beispiel:\n"
        "```/event_delete```\n\n"

        "### ℹ️ `/help`\n"
        "Zeigt diese Hilfe an.\n\n"

        "### 💡 **Hinweise:**\n"
        "- 🔔 Der Bot erinnert automatisch **10 Minuten vor Eventstart** alle Teilnehmer per DM.\n"
        "- 💾 Events werden **dauerhaft auf GitHub gespeichert** – Neustarts sind kein Problem.\n"
        "- ✨ Beim Ändern von Datum, Ort oder Level wird der alte Wert **durchgestrichen** angezeigt.\n"
        "- 🧵 Änderungen werden im zugehörigen Thread-Log dokumentiert.\n"
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

    try:
        local_dt = BERLIN_TZ.localize(datetime.strptime(f"{datum} {zeit}", "%d.%m.%Y %H:%M"))
        utc_dt = local_dt.astimezone(pytz.utc)
    except:
        await interaction.response.send_message("❌ Ungültiges Format. DD.MM.YYYY HH:MM", ephemeral=True)
        return

    weekday = local_dt.strftime("%A")
    weekday_de = {
        "Monday": "Montag", "Tuesday": "Dienstag", "Wednesday": "Mittwoch",
        "Thursday": "Donnerstag", "Friday": "Freitag", "Saturday": "Samstag", "Sunday": "Sonntag"
    }[weekday]
    time_str = local_dt.strftime(f"%A, %d.%m.%Y %H:%M %Z").replace(weekday, weekday_de)

    slot_pattern = re.compile(r"(<a?:\w+:\d+>|\S+)\s*:\s*(\d+)")
    matches = slot_pattern.findall(slots)
    slot_dict = {}
    for emoji, limit in matches:
        emoji = normalize_emoji(emoji)
        if not is_valid_emoji(emoji, interaction.guild):
            await interaction.response.send_message(f"❌ Ungültiges Emoji: {emoji}", ephemeral=True)
            return
        slot_dict[emoji] = {"limit": int(limit), "main": set(), "waitlist": [], "reminded": set()}

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

    msg = await interaction.channel.send(header + "\n\n" + format_event_text({"slots": slot_dict}, interaction.guild))
    await interaction.response.send_message("✅ Event erstellt!", ephemeral=True)
    for e in slot_dict.keys():
        try: await msg.add_reaction(e)
        except: pass

    try:
        thread = await msg.create_thread(name=f"Event-Log: {zweck}", auto_archive_duration=1440)
        await thread.send(f"🧵 Event-Log gestartet — {interaction.user.mention} hat ein neues Event erstellt.")
        thread_id = thread.id
    except Exception:
        thread_id = None

    active_events[msg.id] = {
        "title": zweck, "slots": slot_dict, "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id, "header": header, "creator_id": interaction.user.id,
        "event_time": utc_dt, "thread_id": thread_id
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
        old = ev["event_time"].astimezone(BERLIN_TZ)
        try:
            new_dt = BERLIN_TZ.localize(datetime.strptime(
                f"{datum or old.strftime('%d.%m.%Y')} {zeit or old.strftime('%H:%M')}", "%d.%m.%Y %H:%M"))
            weekday = new_dt.strftime("%A")
            weekday_de = {"Monday":"Montag","Tuesday":"Dienstag","Wednesday":"Mittwoch",
                          "Thursday":"Donnerstag","Friday":"Freitag","Saturday":"Samstag","Sunday":"Sonntag"}[weekday]
            new_str = new_dt.strftime(f"%A, %d.%m.%Y %H:%M %Z").replace(weekday, weekday_de)
            old_str = old.strftime(f"%A, %d.%m.%Y %H:%M %Z").replace(old.strftime("%A"), weekday_de)
            ev["header"] = re.sub(r"🕒 \*\*Datum/Zeit:\*\* .+", f"🕒 **Datum/Zeit:** ~~{old_str}~~ → {new_str}", ev["header"])
            ev["event_time"] = new_dt.astimezone(pytz.utc)
            changed_fields.append("Datum/Zeit")
        except:
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
        slot_pattern = re.compile(r"(<a?:\w+:\d+>|\S+)\s*:\s*(\d+)")
        matches = slot_pattern.findall(slots)
        if not matches:
            await interaction.response.send_message("❌ Ungültiges Slot-Format.", ephemeral=True)
            return
        new_slots = {}
        for emoji, limit in matches:
            emoji = normalize_emoji(emoji)
            new_slots[emoji] = {"limit": int(limit), "main": set(), "waitlist": [], "reminded": set()}
        ev["slots"] = new_slots
        guild = interaction.guild
        channel = guild.get_channel(ev["channel_id"])
        msg = await channel.fetch_message(msg_id)
        await msg.clear_reactions()
        for emoji in new_slots.keys():
            try: await msg.add_reaction(emoji)
            except: pass
        changed_fields.append("Slots")

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

# ----------------- Flask -----------------
flask_app = Flask("bot_flask")

@flask_app.route("/")
def index():
    return "✅ Bot läuft (Render kompatibel)."

def run_bot():
    asyncio.run(bot.start(TOKEN))

if __name__ == "__main__":
    print("🚀 Starte Bot + Flask ...")
    Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
