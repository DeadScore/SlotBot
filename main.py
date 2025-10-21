import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask
from threading import Thread
import re
import os
import asyncio
import json
from datetime import datetime, timedelta

# === Token ===
TOKEN = os.getenv('DISCORD_TOKEN')

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
            except json.JSONDecodeError:
                print("⚠️ Fehler beim Lesen der events.json, Datei wird ignoriert.")
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

# === Flask Webserver (für Render-Uptime) ===
app = Flask('')

@app.route('/')
def home():
    return "✅ Bot läuft und ist wach!"

Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))).start()

# === Emoji & Formatierungsfunktionen ===
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
                if "reminded" not in slot:
                    slot["reminded"] = set()
                for user_id in slot["main"]:
                    member = guild.get_member(user_id)
                    if not member:
                        continue
                    if user_id in slot["reminded"]:
                        continue
                    try:
                        event_time = ev.get("event_time")
                        if event_time:
                            if 0 <= (event_time - now).total_seconds() <= 600:  # 10 Minuten vorher
                                await member.send(f"⏰ Dein Event **{ev['header'].splitlines()[1]}** startet in 10 Minuten!")
                                slot["reminded"].add(user_id)
                    except Exception as e:
                        print(f"❌ Konnte DM an {member} nicht senden: {e}")
        await asyncio.sleep(60)

# === Bot Start ===
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
        except Exception as e:
            print(f"⚠️ Event {message_id} konnte nicht wiederhergestellt werden: {e}")

    try:
        synced = await bot.tree.sync()
        print(f"📂 Slash Commands global synchronisiert ({len(synced)})")
    except Exception as e:
        print(f"❌ Fehler beim Sync: {e}")

# === /event Command ===
@bot.tree.command(name="event", description="Erstellt ein Event mit Steckbrief und begrenzten Slots.")
@app_commands.describe(
    art="Art des Events (PvE/PvP/RP)",
    zweck="Zweck (z. B. EP Farmen)",
    ort="Ort (z. B. Highwayman Hills)",
    zeit="Zeit (z. B. 19:00)",
    datum="Datum (z. B. 21.10.2025)",
    level="Levelbereich (z. B. 5–10)",
    stil="Gemütlich oder Organisiert",
    slots="Slot-Definitionen (z. B. <:Tank:ID>:2 oder <:Tank:ID> : 2)",
    typ="(Optional) Gruppe oder Raid",
    gruppenlead="(Optional) Name oder Mention des Gruppenleiters",
    anmerkung="(Optional) Freitext-Anmerkung zum Event"
)
@app_commands.choices(
    art=[
        app_commands.Choice(name="PvE", value="PvE"),
        app_commands.Choice(name="PvP", value="PvP"),
        app_commands.Choice(name="PVX", value="PVX")
    ],
    typ=[
        app_commands.Choice(name="Gruppe", value="Gruppe"),
        app_commands.Choice(name="Raid", value="Raid")
    ],
    stil=[
        app_commands.Choice(name="Gemütlich", value="Gemütlich"),
        app_commands.Choice(name="Organisiert", value="Organisiert")
    ]
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
    typ: app_commands.Choice[str] = None,
    gruppenlead: str = None,
    anmerkung: str = None
):
    print(f"📨 /event Command aufgerufen von {interaction.user}")

    # === Datum/Zeit validieren (DD.MM.YYYY HH:MM) ===
    try:
        event_datetime = datetime.strptime(f"{datum} {zeit}", "%d.%m.%Y %H:%M")
    except ValueError:
        await interaction.response.send_message(
            "❌ Ungültiges Datum oder Zeitformat.\n"
            "Datum muss im Format `DD.MM.YYYY`, Zeit im Format `HH:MM` (24h) sein.",
            ephemeral=True
        )
        return

    # === Slots verarbeiten ===
    slot_pattern = re.compile(r"(<a?:\w+:\d+>)\s*:\s*(\d+)|(\S+)\s*:\s*(\d+)")
    matches = slot_pattern.findall(slots)
    if not matches:
        await interaction.response.send_message(
            "❌ Keine gültigen Slots gefunden. Format: <:Tank:ID>:2 oder <:Tank:ID> : 2",
            ephemeral=True
        )
        return

    slot_dict = {}
    description = "📋 **Eventübersicht:**\n"

    for custom_emoji, custom_limit, normal_emoji, normal_limit in matches:
        if custom_emoji:
            emoji = normalize_emoji(custom_emoji)
            limit = int(custom_limit)
        else:
            emoji = normalize_emoji(normal_emoji)
            limit = int(normal_limit)

        if not is_valid_emoji(emoji, interaction.guild):
            await interaction.response.send_message(f"❌ Ungültiges Emoji: {emoji}", ephemeral=True)
            return

        slot_dict[emoji] = {"limit": limit, "main": set(), "waitlist": [], "reminded": set()}
        description += f"{emoji} (0/{limit}): -\n"

    # === Header bauen ===
    lines = [
        "@here",
        "🌟 **Neues Event gestartet!** 🌟",
        "",
        f"🗺️ **Art:** {art.value}",
        f"⚔️ **Zweck:** {zweck}",
        f"📍 **Ort:** {ort}",
        f"🕒 **Zeit:** {zeit} | 📅 **Datum:** {datum}",
        f"🎚️ **Level:** {level}",
    ]

    if typ:
        lines.append(f"👥 **Typ:** {typ.value}")
    lines.append(f"🏕️ **Stil:** {stil.value}")

    if gruppenlead:
        lines.append(f"👑 **Gruppenlead:** {gruppenlead}")

    if anmerkung:
        lines.append(f"📝 **Anmerkung:** {anmerkung}")

    header = "\n".join(lines)

    await interaction.response.send_message("✅ Event wurde erstellt!", ephemeral=True)
    msg = await interaction.channel.send(header + "\n\n" + description)

    for emoji in slot_dict.keys():
        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            await interaction.followup.send(f"❌ Fehler beim Hinzufügen von {emoji}")
            return

    # Auto-Thread
    try:
        thread_name = f"Event: {ort} {datum} {zeit}"
        thread = await msg.create_thread(name=thread_name, type=discord.ChannelType.public_thread)
        print(f"🧵 Thread erstellt: {thread.name}")
    except Exception as e:
        print(f"❌ Konnte Thread nicht erstellen: {e}")

    active_events[msg.id] = {
        "slots": slot_dict,
        "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id,
        "header": header,
        "creator_id": interaction.user.id,
        "event_time": event_datetime
    }

    save_events()

# === /event_delete Command ===
@bot.tree.command(name="event_delete", description="Löscht dein letztes erstelltes Event oder als Admin jedes Event im Channel.")
async def event_delete(interaction: discord.Interaction):
    guild = interaction.guild
    channel = interaction.channel

    channel_events = [(mid, ev) for mid, ev in active_events.items() if ev["channel_id"] == channel.id]
    if not channel_events:
        await interaction.response.send_message("❌ In diesem Channel gibt es keine aktiven Events.", ephemeral=True)
        return

    if interaction.user.guild_permissions.manage_messages:
        target_id, target_event = max(channel_events, key=lambda x: x[0])
    else:
        own_events = [(mid, ev) for mid, ev in channel_events if ev["creator_id"] == interaction.user.id]
        if not own_events:
            await interaction.response.send_message("❌ Du hast hier kein Event erstellt.", ephemeral=True)
            return
        target_id, target_event = max(own_events, key=lambda x: x[0])

    try:
        msg = await channel.fetch_message(target_id)
        await msg.delete()
        del active_events[target_id]
        save_events()
        await interaction.response.send_message("✅ Event wurde gelöscht.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler beim Löschen: {e}", ephemeral=True)

# === Reaktions-Handling ===
@bot.event
async def on_raw_reaction_add(payload):
    if payload.message_id not in active_events or payload.user_id == bot.user.id:
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

    # Nur eine Reaktion pro User
    for e in event["slots"].keys():
        if e != emoji:
            try:
                message = await guild.get_channel(payload.channel_id).fetch_message(payload.message_id)
                await message.remove_reaction(e, member)
            except Exception:
                pass

    slot = event["slots"][emoji]

    # Doppelte Einträge vermeiden
    if payload.user_id in slot["main"] or payload.user_id in slot["waitlist"]:
        return
    for s in event["slots"].values():
        if payload.user_id in s["main"] or payload.user_id in s["waitlist"]:
            return

    if len(slot["main"]) < slot["limit"]:
        slot["main"].add(payload.user_id)
    else:
        slot["waitlist"].append(payload.user_id)
        try:
            await member.send(f"📌 Du wurdest auf die Warteliste für Event **{event['header'].splitlines()[1]}** gesetzt.")
        except Exception:
            pass

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

    if user_id in slot["main"]:
        slot["main"].remove(user_id)
        if slot["waitlist"]:
            next_user = slot["waitlist"].pop(0)
            slot["main"].add(next_user)
            try:
                member = bot.get_guild(event["guild_id"]).get_member(next_user)
                if member:
                    await member.send(f"✅ Du bist von der Warteliste nachgerückt für Event **{event['header'].splitlines()[1]}**!")
            except Exception:
                pass
    elif user_id in slot["waitlist"]:
        slot["waitlist"].remove(user_id)

    await update_event_message(payload.message_id)
    save_events()

# === Dauerbetrieb (Auto-Restart) ===
async def start_bot():
    while True:
        try:
            await bot.start(TOKEN)
        except Exception as e:
            print(f"❌ Bot abgestürzt: {e}, Neustart in 5 Sekunden...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(start_bot())
