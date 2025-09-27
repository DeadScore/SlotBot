import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask
from threading import Thread
import re
import os
import asyncio

# Token aus Umgebungsvariable
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Struktur: message_id -> slots, channel_id, guild_id
active_events = {}

CUSTOM_EMOJI_REGEX = r"<a?:\w+:\d+>"

# Flask Webserver für 24/7 Hosting
app = Flask('')

@app.route('/')
def home():
    return "Bot läuft!"

def run():
    app.run(host='0.0.0.0', port=3000)

t = Thread(target=run)
t.start()

# Emoji-Normalisierung
def normalize_emoji(emoji):
    if isinstance(emoji, str):
        return emoji
    if hasattr(emoji, "id") and emoji.id:  # Custom Emoji
        return f"<:{emoji.name}:{emoji.id}>"
    return emoji.name  # Standard-Emoji

# Hilfsfunktionen
def format_event_text(event, guild):
    text = "📋 **Event-Teilnehmerübersicht** 📋\n"
    for emoji, slot in event["slots"].items():
        main_users = [guild.get_member(uid).mention for uid in slot["main"] if guild.get_member(uid)]
        wait_users = [guild.get_member(uid).mention for uid in slot["waitlist"] if guild.get_member(uid)]
        text += f"\n{emoji} ({len(main_users)}/{slot['limit']}): " + (", ".join(main_users) if main_users else "-")
        if wait_users:
            text += f"\n   ⏳ Warteliste: " + ", ".join(wait_users)
    return text

async def update_event_message(message_id):
    if message_id not in active_events:
        return
    event = active_events[message_id]
    guild = bot.get_guild(event["guild_id"])
    if not guild:
        print(f"❌ Guild {event['guild_id']} nicht gefunden")
        return
    channel = guild.get_channel(event["channel_id"])
    if not channel:
        print(f"❌ Channel {event['channel_id']} nicht gefunden")
        return
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(content=format_event_text(event, guild))
    except Exception as e:
        print(f"❌ Fehler beim Aktualisieren von Nachricht {message_id}: {e}")

def is_valid_emoji(emoji, guild):
    if re.match(CUSTOM_EMOJI_REGEX, emoji):
        return any(str(e) == emoji for e in guild.emojis)
    else:
        return True

# Bot-Events
@bot.event
async def on_ready():
    print(f"✅ Bot ist online als {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"📂 Slash Commands global synchronisiert ({len(synced)})")
    except Exception as e:
        print(f"❌ Fehler beim Sync: {e}")

@bot.event
async def on_guild_join(guild):
    try:
        await bot.tree.sync(guild=guild)
        print(f"📂 Slash Commands für neuen Server '{guild.name}' synchronisiert")
    except Exception as e:
        print(f"❌ Fehler beim Sync auf neuem Server {guild.name}: {e}")

# Slash Command /event
@bot.tree.command(name="event", description="Erstellt ein Event mit begrenzten Slots.")
async def event(interaction: discord.Interaction, *, args: str):
    print(f"📨 /event Command aufgerufen von {interaction.user} mit args: {args}")
    parts = args.split()
    slots = {}
    description = "📋 **Event-Teilnehmerübersicht** 📋\n"

    for part in parts:
        if ":" not in part:
            continue
        try:
            emoji_raw, limit = part.rsplit(":", 1)  # nur am letzten ':' splitten
            emoji = normalize_emoji(emoji_raw)
            limit = int(limit)
        except ValueError:
            await interaction.response.send_message(f"❌ Ungültiges Format für {part}", ephemeral=True)
            return

        if not is_valid_emoji(emoji_raw, interaction.guild):
            await interaction.response.send_message(f"❌ Ungültiges Emoji: {emoji_raw}", ephemeral=True)
            return

        slots[emoji] = {"limit": limit, "main": set(), "waitlist": []}
        description += f"{emoji} (0/{limit}): -\n"

    await interaction.response.send_message("✅ Event wurde erstellt!", ephemeral=True)
    msg = await interaction.channel.send(description)
    for emoji in slots.keys():
        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            await interaction.followup.send(f"❌ Fehler beim Hinzufügen von {emoji}")
            return

    active_events[msg.id] = {"slots": slots, "channel_id": interaction.channel.id, "guild_id": interaction.guild.id}

@bot.event
async def on_raw_reaction_add(payload):
    if payload.message_id not in active_events:
        return
    event = active_events[payload.message_id]
    emoji = normalize_emoji(payload.emoji)
    if emoji not in event["slots"]:
        return
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    channel = guild.get_channel(payload.channel_id)
    if not channel:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except:
        return
    member = guild.get_member(payload.user_id)
    if not member:
        return

    # Prüfen, ob User schon im Slot ist (pro Event)
    slot = event["slots"][emoji]
    if payload.user_id in slot["main"] or payload.user_id in slot["waitlist"]:
        return

    if len(slot["main"]) < slot["limit"]:
        slot["main"].add(payload.user_id)
    else:
        slot["waitlist"].append(payload.user_id)

    await update_event_message(payload.message_id)

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
    elif user_id in slot["waitlist"]:
        slot["waitlist"].remove(user_id)

    await update_event_message(payload.message_id)

# Optional: Neustart-Loop für dauerhaften Betrieb
async def start_bot():
    while True:
        try:
            await bot.start(TOKEN)
        except Exception as e:
            print(f"❌ Bot abgestürzt: {e}, Neustart in 5 Sekunden...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(start_bot())
