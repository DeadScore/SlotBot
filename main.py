# main.py
import os
import re
import json
import asyncio
import base64
import requests
from datetime import datetime, timedelta
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
    """Lädt events.json aus dem GitHub-Repo."""
    repo = os.getenv("GITHUB_REPO")
    path = os.getenv("GITHUB_FILE_PATH", "data/events.json")
    token = os.getenv("GITHUB_TOKEN")

    if not all([repo, path, token]):
        print("⚠️ GitHub-Umgebungsvariablen fehlen – kann events.json nicht laden.")
        return {}

    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}"}

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            content = response.json()["content"]
            data = json.loads(base64.b64decode(content))
            for ev in data.values():
                for s in ev["slots"].values():
                    s["main"] = set(s.get("main", []))
                    s["waitlist"] = list(s.get("waitlist", []))
                    s["reminded"] = set(s.get("reminded", []))
            print("✅ events.json erfolgreich von GitHub geladen.")
            return {int(k): v for k, v in data.items()}
        elif response.status_code == 404:
            print("ℹ️ Keine events.json auf GitHub gefunden – starte leer.")
            return {}
        else:
            print(f"⚠️ Konnte events.json nicht laden (HTTP {response.status_code})")
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
        data = {"message": "Update events.json via bot", "content": encoded_content, "sha": sha}

        response = requests.put(url, headers=headers, json=data)

        if response.status_code in [200, 201]:
            print("💾 events.json erfolgreich auf GitHub gespeichert.")
        else:
            print(f"⚠️ Fehler beim Speichern auf GitHub: HTTP {response.status_code}")
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
                        member = guild.get_member(user_id)
                        if not member:
                            try:
                                member = await guild.fetch_member(user_id)
                            except:
                                continue
                        try:
                            await member.send(f"⏰ Dein Event **{ev['title']}** startet in 10 Minuten!")
                            slot["reminded"].add(user_id)
                        except Exception:
                            pass
        await asyncio.sleep(60)

# ----------------- Events -----------------
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
    slots="Slots (z. B. <:Tank:ID>:2)",
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
        if utc_dt < datetime.now(pytz.utc):
            await interaction.response.send_message("❌ Datum/Zeit liegt in der Vergangenheit!", ephemeral=True)
            return
    except:
        await interaction.response.send_message("❌ Ungültiges Format! Nutze DD.MM.YYYY HH:MM", ephemeral=True)
        return

    # Slots
    slot_pattern = re.compile(r"(<a?:\w+:\d+>)\s*:\s*(\d+)|(\S+)\s*:\s*(\d+)")
    matches = slot_pattern.findall(slots)
    if not matches:
        await interaction.response.send_message("❌ Keine gültigen Slots gefunden.", ephemeral=True)
        return

    slot_dict = {}
    for c_emoji, c_limit, n_emoji, n_limit in matches:
        emoji = normalize_emoji(c_emoji or n_emoji)
        limit = int(c_limit or n_limit)
        if not is_valid_emoji(emoji, interaction.guild):
            await interaction.response.send_message(f"❌ Ungültiges Emoji: {emoji}", ephemeral=True)
            return
        slot_dict[emoji] = {"limit": limit, "main": set(), "waitlist": [], "reminded": set()}

    # --- 🗓️ Wochentag automatisch anhängen ---
    weekday = local_dt.strftime("%A")
    weekday_de = {
        "Monday": "Montag",
        "Tuesday": "Dienstag",
        "Wednesday": "Mittwoch",
        "Thursday": "Donnerstag",
        "Friday": "Freitag",
        "Saturday": "Samstag",
        "Sunday": "Sonntag"
    }[weekday]
    time_str = local_dt.strftime(f"%A, %d.%m.%Y %H:%M %Z").replace(weekday, weekday_de)
    # ------------------------------------------

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

    msg_text = header + "\n\n" + format_event_text({"slots": slot_dict}, interaction.guild)
    await interaction.response.send_message("✅ Event erstellt!", ephemeral=True)
    msg = await interaction.channel.send(msg_text)

    # Reaktionen
    for emoji in slot_dict.keys():
        try:
            await msg.add_reaction(emoji)
        except:
            pass

    await asyncio.sleep(2)
    try:
        thread = await msg.create_thread(name=f"Event-Log: {zweck} {datum} {zeit}", auto_archive_duration=1440)
        await thread.send(f"🧵 Event-Log für: {zweck} — {msg.jump_url}")
    except Exception as e:
        print(f"⚠️ Thread konnte nicht erstellt werden: {e}")
        thread = None

    active_events[msg.id] = {
        "title": zweck,
        "slots": slot_dict,
        "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id,
        "header": header,
        "creator_id": interaction.user.id,
        "event_time": utc_dt,
        "thread_id": thread.id if thread else None,
    }
    save_events()

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
            await thread.delete()
        del active_events[msg_id]
        save_events()
        await interaction.response.send_message("✅ Dein Event wurde gelöscht.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler beim Löschen: {e}", ephemeral=True)

# ----------------- Reaction Handling -----------------
@bot.event
async def on_raw_reaction_add(payload):
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
    for e in ev["slots"]:
        if e != emoji:
            try:
                msg = await guild.get_channel(payload.channel_id).fetch_message(payload.message_id)
                await msg.remove_reaction(e, member)
            except:
                pass
    slot = ev["slots"][emoji]
    if any(payload.user_id in s["main"] or payload.user_id in s["waitlist"] for s in ev["slots"].values()):
        return
    if len(slot["main"]) < slot["limit"]:
        slot["main"].add(payload.user_id)
    else:
        slot["waitlist"].append(payload.user_id)
    await update_event_message(payload.message_id)
    save_events()

@bot.event
async def on_raw_reaction_remove(payload):
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
    return "✅ Discord-Bot läuft (Render compatible)."

def run_bot():
    asyncio.run(bot.start(TOKEN))

if __name__ == "__main__":
    print("🚀 Starte Bot + Flask...")
    Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
