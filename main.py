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

# Flask Webserver für Render Free
app = Flask('')

@app.route('/')
def home():
    return "✅ Bot läuft und ist wach!"

def run():
    port = int(os.environ.get("PORT", 5000))  # Render gibt PORT-Variable vor
    app.run(host='0.0.0.0', port=port)

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
        await message.edit(content=event["header"] + "\n" + format_event_text(event, guild))
    except Exception as e:
        print(f"❌ Fehler beim Aktualisieren von Nachricht {message_id}: {e}")

def is_valid_emoji(emoji, guild):
    if re.match(CUSTOM_EMOJI_REGEX, emoji):
        return any(str(e) == emoji for e in guild.emojis)
    else:
        return True

# Bot
