import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask
from threading import Thread
import re
import os
import asyncio

# === CONFIG ===
TOKEN = os.getenv('DISCORD_TOKEN')  # Bot Token aus Umgebungsvariablen
LOG_CHANNEL_ID = None  # Optional: Channel-ID für Lösch-Logs

# === DISCORD INTENTS ===
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# === EVENT SPEICHER ===
# Speichert alle aktiven Events: message_id -> {slots, channel_id, guild_id, creator_id, info}
active_events = {}

CUSTOM_EMOJI_REGEX = r"<a?:\w+:\d+>"

# === FLASK KEEP-ALIVE (für Hosting) ===
app = Flask('')

@app.route('/')
def home():
    return "✅ Bot läuft und ist wach!"

def run():
    """Startet Flask Webserver in eigenem Thread"""
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

Thread(target=run).start()

# === HILFSFUNKTIONEN ===
def normalize_emoji(emoji):
    """Wandelt Emoji in ein konsistentes Format um"""
    if isinstance(emoji, str):
        return emoji.strip()
    if hasattr(emoji, "id") and emoji.id:
        return f"<:{emoji.name}:{emoji.id}>"
    return emoji.name

def is_valid_emoji(emoji, guild):
    """Überprüft, ob das Emoji auf dem Server existiert"""
    if re.match(CUSTOM_EMOJI_REGEX, emoji):
        return any(str(e) == emoji for e in guild.emojis)
    else:
        return True

def format_event_text(event, guild):
    """Formatiert die Event-Nachricht mit Slots, Teilnehmern und Wartelisten"""
    text = f"📋 **Event-Teilnehmerübersicht** 📋\n👤 **Erstellt von:** <@{event['creator_id']}>\n"
    if "info" in event and event["info"]:
        text += "\n" + event["info"] + "\n"
    for emoji, slot in event["slots"].items():
        main_users = [guild.get_member(uid).mention for uid in slot["main"] if guild.get_member(uid)]
        wait_users = [guild.get_member(uid).mention for uid in slot["waitlist"] if guild.get_member(uid)]
        text += f"\n{emoji} ({len(main_users)}/{slot['limit']}): " + (", ".join(main_users) if main_users else "-")
        if wait_users:
            text += f"\n   ⏳ Warteliste: " + ", ".join(wait_users)
    return text

async def update_event_message(message_id):
    """Aktualisiert die Event-Nachricht, z.B. bei Reaktionsänderungen"""
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
        message = await channel.fetch_message(message_id)
        await message.edit(content=format_event_text(event, guild))
    except Exception as e:
        print(f"❌ Fehler beim Aktualisieren der Nachricht {message_id}: {e}")

# === BOT EVENTS ===
@bot.event
async def on_ready():
    """Wird ausgelöst, wenn Bot erfolgreich online ist"""
    print(f"✅ Bot ist online als {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"📂 Slash Commands synchronisiert ({len(synced)})")
    except Exception as e:
        print(f"❌ Fehler beim Sync: {e}")

# === SLASH COMMANDS ===
@bot.tree.command(name="event", description="Erstellt ein Event mit Steckbrief und begrenzten Slots.")
async def event(interaction: discord.Interaction, *, info: str = None, args: str = None):
    """
    Erstellt ein Event:
    - info: Textbeschreibung (Art, Zweck, Zeit etc.)
    - args: Slots im Format ':Emoji: :2' oder '<:Emoji:123>:3'
    """
    await interaction.response.defer(ephemeral=True)

    slots = {}
    description = "📋 **Event-Teilnehmerübersicht** 📋\n"

    # Standardbeschreibung
    if info:
        description += f"\n{info}\n"

    # Slots parsen
    if args:
        # Trenne nach Leerzeichen, Leerzeichen zwischen Emoji und Limit erlaubt
        parts = [p.strip() for p in re.split(r"\s+", args) if p.strip()]
        for i in range(0, len(parts), 2):
            try:
                emoji_raw = parts[i].strip()
                limit = int(parts[i+1].replace(":", "").strip())
                emoji = normalize_emoji(emoji_raw)
            except Exception:
                await interaction.followup.send(f"❌ Ungültiges Format bei `{parts[i]}`", ephemeral=True)
                return

            if not is_valid_emoji(emoji_raw, interaction.guild):
                await interaction.followup.send(f"❌ Ungültiges Emoji: {emoji_raw}", ephemeral=True)
                return

            slots[emoji] = {"limit": limit, "main": set(), "waitlist": []}
            description += f"{emoji} (0/{limit}): -\n"

    # Nachricht senden und Reaktionen hinzufügen
    msg = await interaction.channel.send(description)
    for emoji in slots.keys():
        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            await interaction.followup.send(f"❌ Fehler beim Hinzufügen von {emoji}", ephemeral=True)

    # Event speichern
    active_events[msg.id] = {
        "slots": slots,
        "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id,
        "creator_id": interaction.user.id,
        "info": info or ""
    }

    await interaction.followup.send("✅ Event wurde erstellt!", ephemeral=True)

# === EVENT DELETE ===
@bot.tree.command(name="event_delete", description="Löscht dein letztes erstelltes Event oder als Admin jedes Event im Channel.")
async def event_delete(interaction: discord.Interaction):
    """
    Löscht automatisch:
    - das letzte Event des Erstellers
    - oder, falls Admin, jedes Event im Channel
    """
    guild = interaction.guild
    channel = interaction.channel

    # Alle Events im Channel finden
    user_events = [
        (mid, ev) for mid, ev in active_events.items()
        if ev["channel_id"] == channel.id
    ]

    if not user_events:
        await interaction.response.send_message("❌ In diesem Channel gibt es keine aktiven Events.", ephemeral=True)
        return

    # Admin darf alles löschen
    if interaction.user.guild_permissions.manage_messages:
        latest_id, latest_event = max(user_events, key=lambda x: x[0])
    else:
        # Nur eigene Events
        user_own = [(mid, ev) for mid, ev in user_events if ev["creator_id"] == interaction.user.id]
        if not user_own:
            await interaction.response.send_message("❌ Du hast hier kein Event erstellt.", ephemeral=True)
            return
        latest_id, latest_event = max(user_own, key=lambda x: x[0])

    try:
        msg = await channel.fetch_message(latest_id)
        await msg.delete()
        del active_events[latest_id]
        await interaction.response.send_message("✅ Dein letztes Event wurde gelöscht.", ephemeral=True)

        # Optional: Log
        if LOG_CHANNEL_ID:
            log_channel = guild.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(f"🗑️ **Event gelöscht:** von {interaction.user.mention} (ID: `{latest_id}`) im Channel {channel.mention}")

    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler beim Löschen: {e}", ephemeral=True)

# === REAKTIONEN HANDHABEN ===
@bot.event
async def on_raw_reaction_add(payload):
    """Wenn jemand eine Reaktion hinzufügt"""
    if payload.message_id not in active_events:
        return
    event = active_events[payload.message_id]
    emoji = normalize_emoji(payload.emoji)
    if emoji not in event["slots"] or payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    if not member:
        return

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
    """Wenn jemand eine Reaktion entfernt"""
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

# === BOT START ===
async def start_bot():
    """Startet den Bot mit automatischem Neustart bei Absturz"""
    while True:
        try:
            await bot.start(TOKEN)
        except Exception as e:
            print(f"❌ Bot abgestürzt: {e}, Neustart in 5 Sekunden...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(start_bot())
