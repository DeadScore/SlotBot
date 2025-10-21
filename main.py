import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import json
import re
from datetime import datetime
from flask import Flask
from threading import Thread

# ================== Minimaler Webserver für Render ==================
app = Flask("")

@app.route("/")
def home():
    return "✅ Bot läuft!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_flask).start()
# ===================================================================

# ================== Discord Bot Setup ==================
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ================== Globals ==================
active_events = {}
SAVE_FILE = "events.json"
CUSTOM_EMOJI_REGEX = r"<a?:\w+:\d+>"

# ================== JSON Speicher ==================
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
                print(f"📂 {len(new_data)} Events aus Datei geladen")
                return new_data
            except:
                print("⚠️ Fehler beim Laden der events.json")
                return {}
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

# ================== Emoji Funktionen ==================
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

# ================== Event Text ==================
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

# ================== Thread-Logging ==================
async def log_in_thread(event, msg_id, content):
    guild = bot.get_guild(event["guild_id"])
    if not guild:
        return
    thread_id = event.get("thread_id")
    if thread_id:
        thread = guild.get_channel(thread_id)
        if thread:
            await thread.send(content)

# ================== Reminder Task ==================
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
                    member = guild.get_member(user_id)
                    if not member:
                        try: member = await guild.fetch_member(user_id)
                        except: continue
                    if user_id in slot["reminded"]: continue
                    try:
                        event_time = ev.get("event_time")
                        if event_time and 0 <= (event_time - now).total_seconds() <= 600:
                            await member.send(f"⏰ Dein Event **{ev['header'].splitlines()[1]}** startet in 10 Minuten!")
                            slot["reminded"].add(user_id)
                            await log_in_thread(ev, msg_id, f"⏰ {member.mention} wurde 10 Minuten vor Eventstart erinnert.")
                    except: pass
        await asyncio.sleep(60)

# ================== Bot Start ==================
@bot.event
async def on_ready():
    global active_events
    bot.loop.create_task(reminder_task())
    print(f"✅ Bot ist online als {bot.user}")
    active_events = load_events()
    for message_id, ev in list(active_events.items()):
        try:
            guild = bot.get_guild(ev["guild_id"])
            channel = guild.get_channel(ev["channel_id"])
            msg = await channel.fetch_message(int(message_id))
            await msg.edit(content=ev["header"] + "\n\n" + format_event_text(ev, guild))
        except: pass
    try: await bot.tree.sync()
    except: pass

# ================== /event Command ==================
@bot.tree.command(name="event", description="Erstellt ein Event mit Slots")
@app_commands.describe(
    art="Art des Events (PvE/PvP/PvX)",
    zweck="Zweck des Events",
    ort="Ort des Events",
    zeit="Zeit (HH:MM 24h)",
    datum="Datum (DD.MM.YYYY)",
    level="Levelbereich",
    stil="Stil des Events",
    slots="Slot-Definitionen (z.B. <:Tank:ID>:2)",
    typ="Gruppe oder Raid (optional)",
    gruppenlead="Gruppenlead Name oder Mention (optional)",
    anmerkung="Freitext (optional)"
)
async def event(interaction: discord.Interaction, art: str, zweck: str, ort: str, zeit: str, datum: str, level: str, stil: str, slots: str, typ: str = None, gruppenlead: str = None, anmerkung: str = None):
    try:
        event_datetime = datetime.strptime(f"{datum} {zeit}", "%d.%m.%Y %H:%M")
        if event_datetime < datetime.utcnow(): raise ValueError
    except: 
        await interaction.response.send_message("❌ Ungültiges Datum/Zeit!", ephemeral=True)
        return
    slot_pattern = re.compile(r"(<a?:\w+:\d+>)\s*:\s*(\d+)|(\S+)\s*:\s*(\d+)")
    matches = slot_pattern.findall(slots)
    if not matches:
        await interaction.response.send_message("❌ Keine gültigen Slots!", ephemeral=True)
        return
    slot_dict = {}
    description = ""
    for custom_emoji, custom_limit, normal_emoji, normal_limit in matches:
        if custom_emoji: emoji, limit = normalize_emoji(custom_emoji), int(custom_limit)
        else: emoji, limit = normalize_emoji(normal_emoji), int(normal_limit)
        slot_dict[emoji] = {"limit": limit, "main": set(), "waitlist": [], "reminded": set()}
        description += f"{emoji} (0/{limit}): -\n"
    header = f"📣 @here\n‼️ **Neue Gruppensuche!** ‼️\n\n**Art:** {art}\n**Zweck:** {zweck}\n**Ort:** {ort}\n**Datum/Zeit:** {datum} {zeit} UTC\n**Levelbereich:** {level}\n**Stil:** {stil}\n"
    if typ: header += f"**Typ:** {typ}\n"
    if gruppenlead: header += f"**Gruppenlead:** {gruppenlead}\n"
    if anmerkung: header += f"**Anmerkung:** {anmerkung}\n"
    header += "\nReagiert mit eurer Klasse:\n"
    await interaction.response.send_message("✅ Event erstellt!", ephemeral=True)
    msg = await interaction.channel.send(header + "\n" + description)
    for emoji in slot_dict.keys():
        try: await msg.add_reaction(emoji)
        except: pass
    await asyncio.sleep(1)  # WICHTIG für Thread
    msg = await interaction.channel.fetch_message(msg.id)
    try:
        thread = await msg.create_thread(name=f"Event-Log: {ort} {datum} {zeit}", type=discord.ChannelType.public_thread)
    except: thread = None
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

# ================== /event_delete ==================
@bot.tree.command(name="event_delete", description="Löscht ein Event und Thread")
async def event_delete(interaction: discord.Interaction):
    channel_events = [(mid, ev) for mid, ev in active_events.items() if ev["channel_id"] == interaction.channel.id]
    if not channel_events:
        await interaction.response.send_message("❌ Keine Events hier.", ephemeral=True)
        return
    if interaction.user.guild_permissions.manage_messages:
        target_id, target_event = max(channel_events, key=lambda x: x[0])
    else:
        own_events = [(mid, ev) for mid, ev in channel_events if ev["creator_id"] == interaction.user.id]
        if not own_events:
            await interaction.response.send_message("❌ Du hast hier kein Event.", ephemeral=True)
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
                try: await thread.delete()
                except: pass
        del active_events[target_id]
        save_events()
        await interaction.response.send_message("✅ Event und Thread gelöscht.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)

# ================== Reaktionen ==================
@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id: return
    if payload.message_id not in active_events: return
    event = active_events[payload.message_id]
    emoji = normalize_emoji(payload.emoji)
    if emoji not in event["slots"]: return
    guild = bot.get_guild(payload.guild_id)
    if not guild: return
    try: member = guild.get_member(payload.user_id)
    except: member = None
    slot = event["slots"][emoji]
    for e in event["slots"].keys():
        if e != emoji:
            try: message = await guild.get_channel(payload.channel_id).fetch_message(payload.message_id)
            if member: await message.remove_reaction(e, member)
            except: pass
    already = any(payload.user_id in s["main"] or payload.user_id in s["waitlist"] for s in event["slots"].values())
    if already: return
    if len(slot["main"]) < slot["limit"]:
        slot["main"].add(payload.user_id)
        if member: await log_in_thread(event, payload.message_id, f"✅ {member.mention} hat Slot {emoji} besetzt")
    else:
        slot["waitlist"].append(payload.user_id)
        if member: await log_in_thread(event, payload.message_id, f"⏳ {member.mention} auf Warteliste {emoji}")
    await update_event_message(payload.message_id)
    save_events()

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.message_id not in active_events: return
    event = active_events[payload.message_id]
    emoji = normalize_emoji(payload.emoji)
    if emoji not in event["slots"]: return
    slot = event["slots"][emoji]
    user_id = payload.user_id
    guild = bot.get_guild(payload.guild_id)
    if not guild: return
    try: member = guild.get_member(user_id)
    except: member = None
    if user_id in slot["main"]:
        slot["main"].remove(user_id)
        if member: await log_in_thread(event, payload.message_id, f"❌ {member.mention} hat Slot {emoji} freigegeben")
        if slot["waitlist"]:
            next_user = slot["waitlist"].pop(0)
            slot["main"].add(next_user)
            try: next_member = await guild.fetch_member(next_user)
                await log_in_thread(event, payload.message_id, f"➡️ {next_member.mention} von Warteliste nachgerückt")
                await next_member.send(f"🎉 Du bist von der Warteliste nachgerückt für **{event['header'].splitlines()[1]}**")
            except: pass
    elif user_id in slot["waitlist"]:
        slot["waitlist"].remove(user_id)
        if member: await log_in_thread(event, payload.message_id, f"❌ {member.mention} von Warteliste entfernt")
    await update_event_message(payload.message_id)
    save_events()

# ================== Start Bot ==================
if __name__ == "__main__":
    asyncio.run(bot.start(TOKEN))
