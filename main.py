# main.py
import os
import re
import json
import asyncio
from datetime import datetime, timedelta, timezone
from threading import Thread

import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask

# ----------------- Konfiguration -----------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("❌ DISCORD_TOKEN nicht gesetzt. Bitte als Environment Variable konfigurieren.")
    raise SystemExit(1)

SAVE_FILE = "events.json"
CUSTOM_EMOJI_REGEX = r"<a?:\w+:\d+>"

# ----------------- Intents & Bot -----------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------- In-Memory -----------------
active_events = {}  # message_id -> event data

# ----------------- Hilfsfunktionen: Persistenz -----------------
def load_events():
    if os.path.exists(SAVE_FILE):
        try:
            with open(SAVE_FILE, "r") as f:
                data = json.load(f)
            new = {}
            for mid_str, ev in data.items():
                mid = int(mid_str)
                for slot in ev["slots"].values():
                    slot["main"] = set(slot.get("main", []))
                    slot["waitlist"] = list(slot.get("waitlist", []))
                    slot["reminded"] = set(slot.get("reminded", []))
                new[mid] = ev
            print(f"📂 {len(new)} Events aus {SAVE_FILE} geladen")
            return new
        except Exception as e:
            print(f"⚠️ Fehler beim Laden von {SAVE_FILE}: {e}")
    return {}

def save_events():
    try:
        serializable = {}
        for mid, ev in active_events.items():
            copy = json.loads(json.dumps(ev))
            for slot in copy["slots"].values():
                slot["main"] = list(slot["main"])
                slot["reminded"] = list(slot.get("reminded", []))
            serializable[str(mid)] = copy
        with open(SAVE_FILE, "w") as f:
            json.dump(serializable, f, indent=4)
        print("💾 Events gespeichert")
    except Exception as e:
        print(f"⚠️ Fehler beim Speichern: {e}")

# ----------------- Emoji Helpers -----------------
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

# ----------------- Message Format -----------------
def format_event_text(event, guild):
    text = "📋 **Eventübersicht:**\n"
    for emoji, slot in event["slots"].items():
        main_users = [guild.get_member(uid).mention for uid in slot["main"] if guild.get_member(uid)]
        wait_users = [guild.get_member(uid).mention for uid in slot["waitlist"] if guild.get_member(uid)]
        text += f"\n{emoji} ({len(main_users)}/{slot['limit']}): " + (", ".join(main_users) if main_users else "-")
        if wait_users:
            text += f"\n   ⏳ Warteliste: " + ", ".join(wait_users)
    return text + "\n"

async def update_event_message(message_id):
    if message_id not in active_events:
        return
    ev = active_events[message_id]
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
        print(f"❌ Fehler beim Aktualisieren der Event-Nachricht: {e}")

# ----------------- Thread Logging -----------------
async def log_in_thread(ev, msg_id, content):
    # aktuell deaktiviert, weil Threads Logs optional sind
    return

# ----------------- Reminder Task (10 Minuten) -----------------
async def reminder_task():
    await bot.wait_until_ready()
    berlin = timezone(timedelta(hours=2))  # UTC+2 für Sommerzeit (CEST)
    while not bot.is_closed():
        now = datetime.now(tz=berlin)
        for msg_id, ev in list(active_events.items()):
            guild = bot.get_guild(ev["guild_id"])
            if not guild:
                continue
            for emoji, slot in ev["slots"].items():
                if "reminded" not in slot:
                    slot["reminded"] = set()
                for user_id in list(slot["main"]):
                    if user_id in slot["reminded"]:
                        continue
                    event_time = ev.get("event_time")
                    if not event_time:
                        continue
                    seconds_left = (event_time - now).total_seconds()
                    if 0 <= seconds_left <= 600:
                        try:
                            member = guild.get_member(user_id)
                            if not member:
                                try:
                                    member = await guild.fetch_member(user_id)
                                except:
                                    continue
                            await member.send(f"⏰ Dein Event **{ev['header'].splitlines()[0]}** startet in 10 Minuten!")
                            slot["reminded"].add(user_id)
                        except Exception as e:
                            print(f"❌ Reminder: konnte {user_id} nicht DM'en: {e}")
        await asyncio.sleep(60)

# ----------------- on_ready -----------------
@bot.event
async def on_ready():
    global active_events
    print(f"✅ Bot online als {bot.user}")
    active_events = load_events()
    bot.loop.create_task(reminder_task())
    for msg_id, ev in list(active_events.items()):
        try:
            guild = bot.get_guild(ev["guild_id"])
            channel = guild.get_channel(ev["channel_id"])
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(content=ev["header"] + "\n\n" + format_event_text(ev, guild))
        except Exception as e:
            print(f"⚠️ Event {msg_id} konnte nicht wiederhergestellt werden: {e}")
    try:
        await bot.tree.sync()
        print("📂 Slash Commands synchronisiert")
    except Exception as e:
        print(f"❌ Sync-Fehler: {e}")

# ----------------- /event Command -----------------
@bot.tree.command(name="event", description="Erstellt ein Event mit Reaktionen, Slots & Thread")
@app_commands.describe(
    art="Art des Events (PvE/PvP/PVX)",
    zweck="Zweck",
    ort="Ort",
    zeit="HH:MM",
    datum="DD.MM.YYYY",
    level="Levelbereich",
    stil="Gemütlich/Organisiert",
    slots="Slots z.B. <:Tank:ID>:2",
    typ="Optional Gruppe/Raid",
    gruppenlead="Optional Gruppenleiter",
    anmerkung="Optional Freitext"
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
    berlin = timezone(timedelta(hours=2))
    # Zeit validieren
    try:
        event_time = datetime.strptime(f"{datum} {zeit}", "%d.%m.%Y %H:%M").replace(tzinfo=berlin)
        if event_time < datetime.now(tz=berlin):
            await interaction.response.send_message("❌ Datum/Zeit liegt in der Vergangenheit!", ephemeral=True)
            return
    except:
        await interaction.response.send_message("❌ Ungültiges Datum/Zeit-Format!", ephemeral=True)
        return

    slot_pattern = re.compile(r"(<a?:\w+:\d+>)\s*:\s*(\d+)|(\S+)\s*:\s*(\d+)")
    matches = slot_pattern.findall(slots)
    if not matches:
        await interaction.response.send_message("❌ Keine gültigen Slots gefunden.", ephemeral=True)
        return

    slot_dict = {}
    slot_text = ""
    for c_emoji, c_limit, n_emoji, n_limit in matches:
        emoji = normalize_emoji(c_emoji or n_emoji)
        limit = int(c_limit or n_limit)
        if not is_valid_emoji(emoji, interaction.guild):
            await interaction.response.send_message(f"❌ Ungültiges Emoji: {emoji}", ephemeral=True)
            return
        slot_dict[emoji] = {"limit": limit, "main": set(), "waitlist": [], "reminded": set()}
        slot_text += f"{emoji} **(0/{limit})** – *frei*\n"

    header = (
        f"📣 **@here — Neue Gruppensuche!**\n\n"
        f"🗡️ **Art:** {art.value}\n"
        f"🎯 **Zweck:** {zweck}\n"
        f"📍 **Ort:** {ort}\n"
        f"🕒 **Datum/Zeit:** {datum} {zeit} CEST\n"
        f"⚔️ **Levelbereich:** {level}\n"
        f"💬 **Stil:** {stil.value}\n"
    )
    if typ:
        header += f"🏷️ **Typ:** {typ.value}\n"
    if gruppenlead:
        header += f"👑 **Gruppenlead:** {gruppenlead}\n"
    if anmerkung:
        header += f"📝 **Anmerkung:** {anmerkung}\n"

    full_message = f"{header}\n---\n**Reagiert mit eurer Klasse:**\n\n{slot_text}"

    await interaction.response.send_message("✅ Event erstellt!", ephemeral=True)
    msg = await interaction.channel.send(full_message)

    for emoji in slot_dict.keys():
        try:
            await msg.add_reaction(emoji)
        except:
            pass

    await asyncio.sleep(1)
    try:
        msg = await interaction.channel.fetch_message(msg.id)
    except:
        pass

    thread = None
    try:
        thread = await msg.create_thread(name=f"Event-Log: {zweck} {datum} {zeit}", auto_archive_duration=1440)
        try:
            await thread.send(f"🧵 Event-Log für: {zweck}")
        except:
            pass
    except:
        thread = None

    active_events[msg.id] = {
        "slots": slot_dict,
        "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id,
        "header": full_message,
        "creator_id": interaction.user.id,
        "event_time": event_time,
        "thread_id": thread.id if thread else None
    }
    save_events()

# ----------------- /event_delete nur eigene -----------------
@bot.tree.command(name="event_delete", description="Löscht dein letztes erstelltes Event")
async def event_delete(interaction: discord.Interaction):
    channel_events = [(mid, ev) for mid, ev in active_events.items() if ev["channel_id"] == interaction.channel.id]
    if not channel_events:
        await interaction.response.send_message("❌ In diesem Channel gibt es keine aktiven Events.", ephemeral=True)
        return

    own_events = [(mid, ev) for mid, ev in channel_events if ev["creator_id"] == interaction.user.id]
    if not own_events:
        await interaction.response.send_message("❌ Du hast hier kein Event erstellt.", ephemeral=True)
        return

    target_id, target_event = max(own_events, key=lambda x: x[0])
    guild = interaction.guild
    channel = interaction.channel
    try:
        msg = await channel.fetch_message(target_id)
        await msg.delete()
        thread_id = target_event.get("thread_id")
        if thread_id:
            thread = guild.get_channel(thread_id)
            if thread:
                try:
                    await thread.delete()
                except:
                    pass
        del active_events[target_id]
        save_events()
        await interaction.response.send_message("✅ Dein Event und zugehöriger Thread wurden gelöscht.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler beim Löschen: {e}", ephemeral=True)

# ----------------- /help Command -----------------
@bot.tree.command(name="help", description="Zeigt eine Übersicht aller Bot-Befehle")
async def help_command(interaction: discord.Interaction):
    help_text = (
        "**Bot-Befehle:**\n"
        "/event – Event erstellen\n"
        "/event_delete – Eigene Events löschen\n"
        "/help – Diese Übersicht\n"
    )
    await interaction.response.send_message(help_text, ephemeral=True)

# ----------------- Reactions -----------------
@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return
    if payload.message_id not in active_events:
        return
    ev = active_events[payload.message_id]
    emoji = normalize_emoji(payload.emoji)
    if emoji not in ev["slots"]:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    try:
        member = guild.get_member(payload.user_id)
        if not member:
            member = await guild.fetch_member(payload.user_id)
    except:
        return
    try:
        channel = guild.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        for e in ev["slots"].keys():
            if e != emoji:
                try:
                    await message.remove_reaction(e, member)
                except:
                    pass
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
    if payload.message_id not in active_events:
        return
    ev = active_events[payload.message_id]
    emoji = normalize_emoji(payload.emoji)
    if emoji not in ev["slots"]:
        return
    slot = ev["slots"][emoji]
    user_id = payload.user_id
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    try:
        member = guild.get_member(user_id)
        if not member:
            member = await guild.fetch_member(user_id)
    except:
        member = None
    if user_id in slot["main"]:
        slot["main"].remove(user_id)
        if slot["waitlist"]:
            next_user = slot["waitlist"].pop(0)
            slot["main"].add(next_user)
            try:
                next_member = await guild.fetch_member(next_user)
                try:
                    await next_member.send(f"🎉 Du bist von der Warteliste für **{ev['header'].splitlines()[0]}** nachgerückt!")
                except:
                    pass
            except:
                pass
    elif user_id in slot["waitlist"]:
        try:
            slot["waitlist"].remove(user_id)
        except ValueError:
            pass
    await update_event_message(payload.message_id)
    save_events()

# ----------------- Minimaler Webserver (Render) -----------------
flask_app = Flask("bot_flask")

@flask_app.route("/")
def index():
    return "✅ Discord-Bot läuft (Render-compatible)."

# ----------------- Run Bot in Background Thread + Flask in Main -----------------
def run_bot():
    asyncio.run(bot.start(TOKEN))

if __name__ == "__main__":
    print("🚀 Starte Bot-Thread und Flask...")
    bot_thread = Thread(target=run_bot, daemon=True)
    bot_thread.start()
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
