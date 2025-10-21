import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask
from threading import Thread
import os
import re
import json
import asyncio
from datetime import datetime

# === Token ===
TOKEN = os.getenv('DISCORD_TOKEN')

if not TOKEN:
    print("❌ Kein DISCORD_TOKEN gefunden! Bitte als Environment Variable in Render setzen.")
    raise SystemExit(1)

# === Intents ===
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# === Globale Variablen ===
active_events = {}
SAVE_FILE = "events.json"
CUSTOM_EMOJI_REGEX = r"<a?:\w+:\d+>"

# === JSON Speicherfunktionen ===
def load_events():
    if os.path.exists(SAVE_FILE):
        try:
            with open(SAVE_FILE, "r") as f:
                data = json.load(f)
            for ev in data.values():
                for slot in ev["slots"].values():
                    slot["main"] = set(slot.get("main", []))
                    slot["waitlist"] = list(slot.get("waitlist", []))
                    slot["reminded"] = set(slot.get("reminded", []))
            print(f"📂 {len(data)} Events aus Datei geladen")
            return {int(k): v for k, v in data.items()}
        except Exception as e:
            print(f"⚠️ Fehler beim Laden von events.json: {e}")
    return {}

def save_events():
    serializable = {}
    for mid, ev in active_events.items():
        copy = json.loads(json.dumps(ev))
        for slot in ev["slots"].values():
            slot["main"] = list(slot["main"])
            slot["reminded"] = list(slot.get("reminded", []))
        serializable[str(mid)] = copy
    with open(SAVE_FILE, "w") as f:
        json.dump(serializable, f, indent=4)
    print("💾 Events gespeichert")

# === Flask Webserver (Render braucht offenen Port) ===
app = Flask('')

@app.route('/')
def home():
    return "✅ Bot läuft und ist wach!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    print(f"🌐 Flask läuft auf Port {port}")
    app.run(host='0.0.0.0', port=port)

Thread(target=run_flask, daemon=True).start()

# === Emoji Helper ===
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
    event = active_events[message_id]
    guild = bot.get_guild(event["guild_id"])
    if not guild:
        return
    channel = guild.get_channel(event["channel_id"])
    if not channel:
        return
    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(content=event["header"] + "\n\n" + format_event_text(event, guild))
    except Exception as e:
        print(f"❌ Fehler beim Aktualisieren: {e}")

async def log_in_thread(event, msg_id, content):
    guild = bot.get_guild(event["guild_id"])
    if not guild:
        return
    thread_id = event.get("thread_id")
    if thread_id:
        thread = guild.get_channel(thread_id)
        if thread:
            try:
                await thread.send(content)
            except:
                pass

# === Reminder Task ===
async def reminder_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.utcnow()
        for msg_id, ev in list(active_events.items()):
            guild = bot.get_guild(ev["guild_id"])
            if not guild:
                continue
            for emoji, slot in ev["slots"].items():
                for user_id in slot["main"]:
                    if user_id in slot["reminded"]:
                        continue
                    member = guild.get_member(user_id)
                    if not member:
                        continue
                    event_time = ev.get("event_time")
                    if event_time and 0 <= (event_time - now).total_seconds() <= 600:
                        try:
                            await member.send(f"⏰ Dein Event **{ev['header'].splitlines()[1]}** startet in 10 Minuten!")
                            slot["reminded"].add(user_id)
                            await log_in_thread(ev, msg_id, f"⏰ {member.mention} wurde erinnert.")
                        except:
                            pass
        await asyncio.sleep(60)

# === Startup ===
@bot.event
async def on_ready():
    global active_events
    print(f"✅ Bot ist online als {bot.user}")
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
        synced = await bot.tree.sync()
        print(f"📂 Slash Commands global synchronisiert ({len(synced)})")
    except Exception as e:
        print(f"❌ Fehler beim Sync: {e}")

# === /event Command ===
@bot.tree.command(name="event", description="Erstellt ein Event mit Steckbrief und begrenzten Slots.")
@app_commands.describe(
    art="Art des Events",
    zweck="Zweck (z. B. EP Farmen)",
    ort="Ort (z. B. Highwayman Hills)",
    zeit="Zeit im Format HH:MM (24h)",
    datum="Datum im Format DD.MM.YYYY",
    level="Levelbereich (z. B. 5–10)",
    stil="Gemütlich oder Organisiert",
    slots="Slot-Definitionen, z. B. <:Tank:12345>:2",
    typ="(Optional) Gruppe oder Raid",
    gruppenlead="(Optional) Name oder Mention des Gruppenleiters",
    anmerkung="(Optional) Freitext-Anmerkung"
)
@app_commands.choices(
    art=[
        app_commands.Choice(name="PvE", value="PvE"),
        app_commands.Choice(name="PvP", value="PvP"),
        app_commands.Choice(name="PVX", value="PVX")
    ],
    stil=[
        app_commands.Choice(name="Gemütlich", value="Gemütlich"),
        app_commands.Choice(name="Organisiert", value="Organisiert")
    ]
)
async def event(interaction: discord.Interaction, art: app_commands.Choice[str], zweck: str, ort: str, zeit: str,
                datum: str, level: str, stil: app_commands.Choice[str],
                slots: str, typ: app_commands.Choice[str] = None,
                gruppenlead: str = None, anmerkung: str = None):

    try:
        event_datetime = datetime.strptime(f"{datum} {zeit}", "%d.%m.%Y %H:%M")
        if event_datetime < datetime.utcnow():
            return await interaction.response.send_message("❌ Datum/Zeit liegt in der Vergangenheit!", ephemeral=True)
    except ValueError:
        return await interaction.response.send_message("❌ Ungültiges Datum/Zeit-Format!", ephemeral=True)

    # Slots parsen
    slot_pattern = re.compile(r"(<a?:\w+:\d+>)\s*:\s*(\d+)|(\S+)\s*:\s*(\d+)")
    matches = slot_pattern.findall(slots)
    if not matches:
        return await interaction.response.send_message("❌ Keine gültigen Slots gefunden.", ephemeral=True)

    slot_dict = {}
    desc = ""
    for c_emoji, c_limit, n_emoji, n_limit in matches:
        emoji = normalize_emoji(c_emoji or n_emoji)
        limit = int(c_limit or n_limit)
        if not is_valid_emoji(emoji, interaction.guild):
            return await interaction.response.send_message(f"❌ Ungültiges Emoji: {emoji}", ephemeral=True)
        slot_dict[emoji] = {"limit": limit, "main": set(), "waitlist": [], "reminded": set()}
        desc += f"{emoji} (0/{limit}): -\n"

    # Header
    header = f"📣 @here\n‼️ **Neue Gruppensuche!** ‼️\n\n" \
             f"**Art:** {art.value}\n**Zweck:** {zweck}\n**Ort:** {ort}\n" \
             f"**Datum/Zeit:** {datum} {zeit} UTC\n**Levelbereich:** {level}\n" \
             f"**Stil:** {stil.value}\n"
    if typ: header += f"**Typ:** {typ.value}\n"
    if gruppenlead: header += f"**Gruppenlead:** {gruppenlead}\n"
    if anmerkung: header += f"**Anmerkung:** {anmerkung}\n"
    header += "\nReagiert mit eurer Klasse:\n"

    await interaction.response.send_message("✅ Event wurde erstellt!", ephemeral=True)
    msg = await interaction.channel.send(header + "\n" + desc)

    for emoji in slot_dict.keys():
        try:
            await msg.add_reaction(emoji)
        except:
            pass

    # Thread erstellen
    thread = None
    try:
        thread = await msg.create_thread(name=f"Event-Log: {ort} {datum} {zeit}",
                                         type=discord.ChannelType.public_thread)
        print(f"🧵 Thread erstellt: {thread.name}")
    except Exception as e:
        print(f"❌ Thread konnte nicht erstellt werden: {e}")

    active_events[msg.id] = {
        "slots": slot_dict,
        "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id,
        "header": header,
        "creator_id": interaction.user.id,
        "event_time": event_datetime,
        "thread_id": thread.id if thread else None
    }
    save_events()

# === /event_delete Command ===
@bot.tree.command(name="event_delete", description="Löscht ein Event und zugehörigen Thread.")
async def event_delete(interaction: discord.Interaction):
    guild, channel = interaction.guild, interaction.channel
    channel_events = [(mid, ev) for mid, ev in active_events.items() if ev["channel_id"] == channel.id]
    if not channel_events:
        return await interaction.response.send_message("❌ Keine Events in diesem Channel.", ephemeral=True)

    if interaction.user.guild_permissions.manage_messages:
        target_id, target = max(channel_events, key=lambda x: x[0])
    else:
        own = [(mid, ev) for mid, ev in channel_events if ev["creator_id"] == interaction.user.id]
        if not own:
            return await interaction.response.send_message("❌ Du hast kein Event hier erstellt.", ephemeral=True)
        target_id, target = max(own, key=lambda x: x[0])

    try:
        msg = await channel.fetch_message(target_id)
        await msg.delete()
        if target.get("thread_id"):
            thread = guild.get_channel(target["thread_id"])
            if thread:
                await thread.delete()
        del active_events[target_id]
        save_events()
        await interaction.response.send_message("✅ Event & Thread gelöscht.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler beim Löschen: {e}", ephemeral=True)

# === Reaktionen ===
@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return
    if payload.message_id not in active_events:
        return

    event = active_events[payload.message_id]
    emoji = normalize_emoji(payload.emoji)
    if emoji not in event["slots"]:
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    if not member:
        return

    message = await guild.get_channel(payload.channel_id).fetch_message(payload.message_id)

    # Nur eine Reaktion pro User
    for e in event["slots"].keys():
        if e != emoji:
            await message.remove_reaction(e, member)

    slot = event["slots"][emoji]
    already_in = any(payload.user_id in s["main"] or payload.user_id in s["waitlist"] for s in event["slots"].values())
    if already_in:
        return

    if len(slot["main"]) < slot["limit"]:
        slot["main"].add(payload.user_id)
        await log_in_thread(event, payload.message_id, f"✅ {member.mention} hat Slot {emoji} besetzt")
    else:
        slot["waitlist"].append(payload.user_id)
        await log_in_thread(event, payload.message_id, f"⏳ {member.mention} auf Warteliste {emoji}")

    await update_event_message(payload.message_id)
    save_events()

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.message_id not in active_events:
        return
    event = active_events[payload.message_id]
    emoji = normalize_emoji(payload.emoji)
    if emoji not in event["slots"]:
        return

    slot = event["slots"][emoji]
    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    user_id = payload.user_id

    if user_id in slot["main"]:
        slot["main"].remove
