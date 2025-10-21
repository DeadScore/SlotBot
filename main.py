import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask
from threading import Thread
import asyncio
import os
import json
import re
from datetime import datetime, timedelta

# === Discord Token ===
TOKEN = os.getenv("DISCORD_TOKEN")

# === Intents ===
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# === Globale Variablen ===
SAVE_FILE = "events.json"
active_events = {}
CUSTOM_EMOJI_REGEX = r"<a?:\w+:\d+>"

# === Speicherfunktionen ===
def load_events():
    if os.path.exists(SAVE_FILE):
        with open(SAVE_FILE, "r") as f:
            try:
                data = json.load(f)
                new_data = {}
                for mid_str, ev in data.items():
                    mid = int(mid_str)
                    for slot in ev["slots"].values():
                        slot["main"] = set(slot.get("main", []))
                        slot["waitlist"] = list(slot.get("waitlist", []))
                        slot["reminded"] = set(slot.get("reminded", []))
                    new_data[mid] = ev
                print(f"📂 {len(new_data)} Events geladen")
                return new_data
            except Exception as e:
                print(f"⚠️ Fehler beim Laden: {e}")
    return {}

def save_events():
    serializable = {}
    for mid, ev in active_events.items():
        copy = json.loads(json.dumps(ev))
        for slot_key, slot in ev["slots"].items():
            copy["slots"][slot_key]["main"] = list(slot["main"])
            copy["slots"][slot_key]["reminded"] = list(slot.get("reminded", []))
        serializable[str(mid)] = copy
    with open(SAVE_FILE, "w") as f:
        json.dump(serializable, f, indent=4)
    print("💾 Events gespeichert")

# === Emoji Utils ===
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

# === Formatierung ===
def format_event_text(event, guild):
    text = "📋 **Eventübersicht:**\n"
    for emoji, slot in event["slots"].items():
        main_users = [guild.get_member(uid).mention for uid in slot["main"] if guild.get_member(uid)]
        wait_users = [guild.get_member(uid).mention for uid in slot["waitlist"] if guild.get_member(uid)]
        text += f"\n{emoji} ({len(main_users)}/{slot['limit']}): " + (", ".join(main_users) if main_users else "-")
        if wait_users:
            text += f"\n   ⏳ Warteliste: " + ", ".join(wait_users)
    return text + "\n"

# === Update Nachricht ===
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
        msg = await channel.fetch_message(int(message_id))
        await msg.edit(content=event["header"] + "\n\n" + format_event_text(event, guild))
    except Exception as e:
        print(f"❌ Fehler beim Aktualisieren: {e}")

# === Thread Logging ===
async def log_in_thread(event, msg_id, content):
    guild = bot.get_guild(event["guild_id"])
    if not guild:
        return
    thread_id = event.get("thread_id")
    if thread_id:
        thread = guild.get_channel(thread_id)
        if thread:
            await thread.send(content)

# === Reminder ===
async def reminder_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.utcnow()
        for msg_id, ev in list(active_events.items()):
            guild = bot.get_guild(ev["guild_id"])
            if not guild:
                continue
            for emoji, slot in ev["slots"].items():
                if "reminded" not in slot:
                    slot["reminded"] = set()
                for user_id in slot["main"]:
                    if user_id in slot["reminded"]:
                        continue
                    try:
                        event_time = ev.get("event_time")
                        if event_time and 0 <= (event_time - now).total_seconds() <= 600:
                            member = guild.get_member(user_id)
                            if member:
                                await member.send(f"⏰ Dein Event **{ev['header'].splitlines()[1]}** startet in 10 Minuten!")
                                slot["reminded"].add(user_id)
                                await log_in_thread(ev, msg_id, f"⏰ {member.mention} wurde erinnert.")
                    except Exception as e:
                        print(f"❌ Reminder-Fehler: {e}")
        await asyncio.sleep(60)

# === Bot Start ===
@bot.event
async def on_ready():
    global active_events
    print(f"✅ Bot online als {bot.user}")
    bot.loop.create_task(reminder_task())
    active_events = load_events()
    try:
        await bot.tree.sync()
        print("📂 Slash Commands synchronisiert")
    except Exception as e:
        print(f"❌ Sync-Fehler: {e}")

# === /event Command ===
@bot.tree.command(name="event", description="Erstellt ein Event mit Reaktionen, Slots & Thread")
@app_commands.describe(
    art="Art des Events (PvE/PvP/PVX)",
    zweck="Zweck (z. B. EP Farmen)",
    ort="Ort (z. B. Highwayman Hills)",
    zeit="Zeit im Format HH:MM",
    datum="Datum im Format DD.MM.YYYY",
    level="Levelbereich",
    stil="Gemütlich oder Organisiert",
    slots="Slot-Definition (z. B. <:Tank:ID>:2)",
    typ="Optional: Gruppe oder Raid",
    gruppenlead="Optional: Gruppenleiter",
    anmerkung="Optional: Freitext"
)
@app_commands.choices(
    art=[app_commands.Choice(name=x, value=x) for x in ["PvE", "PvP", "PVX"]],
    stil=[app_commands.Choice(name=x, value=x) for x in ["Gemütlich", "Organisiert"]],
    typ=[app_commands.Choice(name=x, value=x) for x in ["Gruppe", "Raid"]]
)
async def event(interaction: discord.Interaction, art: app_commands.Choice[str], zweck: str, ort: str,
                zeit: str, datum: str, level: str, stil: app_commands.Choice[str], slots: str,
                typ: app_commands.Choice[str] = None, gruppenlead: str = None, anmerkung: str = None):
    
    # --- Zeit validieren ---
    try:
        event_time = datetime.strptime(f"{datum} {zeit}", "%d.%m.%Y %H:%M")
        if event_time < datetime.utcnow():
            await interaction.response.send_message("❌ Datum/Zeit liegt in der Vergangenheit!", ephemeral=True)
            return
    except:
        await interaction.response.send_message("❌ Ungültiges Datum/Zeit-Format!", ephemeral=True)
        return

    # --- Slots ---
    pattern = re.compile(r"(<a?:\w+:\d+>)\s*:\s*(\d+)|(\S+)\s*:\s*(\d+)")
    matches = pattern.findall(slots)
    if not matches:
        await interaction.response.send_message("❌ Ungültiges Slot-Format.", ephemeral=True)
        return

    slot_dict = {}
    description = ""
    for custom_emoji, custom_limit, normal_emoji, normal_limit in matches:
        emoji = normalize_emoji(custom_emoji or normal_emoji)
        limit = int(custom_limit or normal_limit)
        if not is_valid_emoji(emoji, interaction.guild):
            await interaction.response.send_message(f"❌ Ungültiges Emoji: {emoji}", ephemeral=True)
            return
        slot_dict[emoji] = {"limit": limit, "main": set(), "waitlist": [], "reminded": set()}
        description += f"{emoji} (0/{limit}): -\n"

    # --- Header ---
    header = f"📣 @here\n‼️ **Neue Gruppensuche!** ‼️\n\n" \
             f"**Art:** {art.value}\n**Zweck:** {zweck}\n**Ort:** {ort}\n" \
             f"**Datum/Zeit:** {datum} {zeit} UTC\n**Levelbereich:** {level}\n" \
             f"**Stil:** {stil.value}\n"
    if typ: header += f"**Typ:** {typ.value}\n"
    if gruppenlead: header += f"**Gruppenlead:** {gruppenlead}\n"
    if anmerkung: header += f"**Anmerkung:** {anmerkung}\n"
    header += "\nReagiert mit eurer Klasse:\n"

    await interaction.response.send_message("✅ Event erstellt!", ephemeral=True)
    msg = await interaction.channel.send(header + "\n" + description)

    # --- Reaktionen ---
    for emoji in slot_dict.keys():
        try: await msg.add_reaction(emoji)
        except: pass

    await asyncio.sleep(1)
    msg = await interaction.channel.fetch_message(msg.id)
    try:
        thread = await msg.create_thread(name=f"Event-Log: {ort} {datum} {zeit}", type=discord.ChannelType.public_thread)
        print(f"🧵 Thread erstellt: {thread.name}")
    except:
        thread = None

    active_events[msg.id] = {
        "slots": slot_dict,
        "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id,
        "header": header,
        "creator_id": interaction.user.id,
        "event_time": event_time,
        "thread_id": thread.id if thread else None
    }
    save_events()

# === /event_delete Command ===
@bot.tree.command(name="event_delete", description="Löscht dein Event und zugehörigen Thread")
async def event_delete(interaction: discord.Interaction):
    channel_events = [(mid, ev) for mid, ev in active_events.items() if ev["channel_id"] == interaction.channel.id]
    if not channel_events:
        await interaction.response.send_message("❌ Keine Events hier.", ephemeral=True)
        return

    if interaction.user.guild_permissions.manage_messages:
        target_id, target_event = max(channel_events, key=lambda x: x[0])
    else:
        own = [(mid, ev) for mid, ev in channel_events if ev["creator_id"] == interaction.user.id]
        if not own:
            await interaction.response.send_message("❌ Kein Event von dir gefunden.", ephemeral=True)
            return
        target_id, target_event = max(own, key=lambda x: x[0])

    guild = interaction.guild
    channel = interaction.channel
    try:
        msg = await channel.fetch_message(target_id)
        await msg.delete()
        thread_id = target_event.get("thread_id")
        if thread_id:
            thread = guild.get_channel(thread_id)
            if thread:
                try: await thread.delete()
                except: pass
        del active_events[target_id]
        save_events()
        await interaction.response.send_message("✅ Event und Thread gelöscht.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)

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
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    if not member:
        return

    message = await guild.get_channel(payload.channel_id).fetch_message(payload.message_id)
    for e in event["slots"].keys():
        if e != emoji:
            try:
                await message.remove_reaction(e, member)
            except:
                pass

    slot = event["slots"][emoji]
    if any(payload.user_id in s["main"] or payload.user_id in s["waitlist"] for s in event["slots"].values()):
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
    user_id = payload.user_id
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(user_id)

    if user_id in slot["main"]:
        slot["main"].remove(user_id)
        await log_in_thread(event, payload.message_id, f"❌ {member.mention} hat Slot {emoji} freigegeben")
        if slot["waitlist"]:
            next_user = slot["waitlist"].pop(0)
            slot["main"].add(next_user)
            next_member = await guild.fetch_member(next_user)
            await log_in_thread(event, payload.message_id, f"➡️ {next_member.mention} ist nachgerückt")
            await next_member.send(f"🎉 Du bist von der Warteliste für **{event['header'].splitlines()[1]}** nachgerückt!")
    elif user_id in slot["waitlist"]:
        slot["waitlist"].remove(user_id)
        await log_in_thread(event, payload.message_id, f"❌ {member.mention} von Warteliste entfernt")

    await update_event_message(payload.message_id)
    save_events()

# === Flask Webserver (Render-kompatibel) ===
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Bot läuft auf Render!"

# === Start für Render ===
def run_bot():
    asyncio.run(bot.start(TOKEN))

if __name__ == "__main__":
    bot_thread = Thread(target=run_bot)
    bot_thread.start()
    port = int(os.environ.get("PORT", 5000))
    print(f"🌐 Flask läuft auf Port {port}")
    app.run(host="0.0.0.0", port=port)
