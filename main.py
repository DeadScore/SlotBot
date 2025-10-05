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

# Struktur: message_id -> slots, channel_id, guild_id, header, creator_id
active_events = {}

CUSTOM_EMOJI_REGEX = r"<a?:\w+:\d+>"

# Flask Webserver für Render
app = Flask('')

@app.route('/')
def home():
    return "✅ Bot läuft und ist wach!"

def run():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

t = Thread(target=run)
t.start()

# Emoji normalisieren
def normalize_emoji(emoji):
    if isinstance(emoji, str):
        return emoji
    if hasattr(emoji, "id") and emoji.id:
        return f"<:{emoji.name}:{emoji.id}>"
    return emoji.name

# Formatierung der Event-Nachricht
def format_event_text(event, guild):
    text = "📋 **Event-Teilnehmerübersicht** 📋\n"
    for emoji, slot in event["slots"].items():
        main_users = [guild.get_member(uid).mention for uid in slot["main"] if guild.get_member(uid)]
        wait_users = [guild.get_member(uid).mention for uid in slot["waitlist"] if guild.get_member(uid)]
        text += f"\n{emoji} ({len(main_users)}/{slot['limit']}): " + (", ".join(main_users) if main_users else "-")
        if wait_users:
            text += f"\n   ⏳ Warteliste: " + ", ".join(wait_users)
    return text

# Event-Message aktualisieren
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
        message = await channel.fetch_message(message_id)
        await message.edit(content=event["header"] + "\n" + format_event_text(event, guild))
    except Exception as e:
        print(f"❌ Fehler beim Aktualisieren: {e}")

# Emoji-Validierung
def is_valid_emoji(emoji, guild):
    if re.match(CUSTOM_EMOJI_REGEX, emoji):
        return any(str(e) == emoji for e in guild.emojis)
    return True

# Bot-Start
@bot.event
async def on_ready():
    print(f"✅ Bot ist online als {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"📂 Slash Commands global synchronisiert ({len(synced)})")
    except Exception as e:
        print(f"❌ Fehler beim Sync: {e}")

# /event Command
@bot.tree.command(name="event", description="Erstellt ein Event mit Steckbrief und begrenzten Slots.")
@app_commands.describe(
    art="Art des Events (PvE/PvP/RP)",
    zweck="Zweck (z. B. EP Farmen)",
    ort="Ort (z. B. Higewayman Hills)",
    zeit="Zeit (z. B. heute 19 Uhr)",
    level="Levelbereich (z. B. 5–10)",
    typ="Gruppe oder Raid",
    stil="Gemütlich oder Organisiert",
    slots="Slot-Definitionen (z. B. <:Tank:ID>:2 oder <:Tank:ID> :2)"
)
@app_commands.choices(
    art=[
        app_commands.Choice(name="PvE", value="PvE"),
        app_commands.Choice(name="PvP", value="PvP"),
        app_commands.Choice(name="RP", value="RP")
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
async def event(interaction: discord.Interaction,
                art: app_commands.Choice[str],
                zweck: str,
                ort: str,
                zeit: str,
                level: str,
                typ: app_commands.Choice[str],
                stil: app_commands.Choice[str],
                slots: str):

    print(f"📨 /event Command aufgerufen von {interaction.user}")

    # Sofort acken
    await interaction.response.defer(ephemeral=True)

    slot_pattern = re.compile(r"(<a?:\w+:\d+>)\s*:\s*(\d+)|(\S+)\s*:\s*(\d+)")
    matches = slot_pattern.findall(slots)
    if not matches:
        await interaction.followup.send(
            "❌ Keine gültigen Slots gefunden. Format: <:Tank:ID>:2 oder <:Tank:ID> : 2",
            ephemeral=True
        )
        return

    slot_dict = {}
    description = "📋 **Event-Teilnehmerübersicht** 📋\n"

    for custom_emoji, custom_limit, normal_emoji, normal_limit in matches:
        if custom_emoji:
            emoji = normalize_emoji(custom_emoji)
            limit = int(custom_limit)
        else:
            emoji = normalize_emoji(normal_emoji)
            limit = int(normal_limit)

        if not is_valid_emoji(emoji, interaction.guild):
            await interaction.followup.send(f"❌ Ungültiges Emoji: {emoji}", ephemeral=True)
            return

        slot_dict[emoji] = {"limit": limit, "main": set(), "waitlist": []}
        description += f"{emoji} (0/{limit}): -\n"

    header = (
        f"‼️ **Neue Gruppensuche!** ‼️\n\n"
        f"👤 **Erstellt von:** {interaction.user.mention}\n\n"
        f"**Art:** {art.value}\n"
        f"**Zweck:** {zweck}\n"
        f"**Ort:** {ort}\n"
        f"**Zeit:** {zeit}\n"
        f"**Levelbereich:** {level}\n"
        f"**Typ:** {typ.value}\n"
        f"**Stil:** {stil.value}\n\n"
        f"Reagiert mit eurer Klasse:\n"
    )

    msg = await interaction.channel.send(header + "\n" + description)

    async def add_reactions():
        for emoji in slot_dict.keys():
            try:
                await msg.add_reaction(emoji)
            except discord.HTTPException:
                await interaction.followup.send(f"❌ Fehler beim Hinzufügen von {emoji}", ephemeral=True)

        # Event speichern
        active_events[msg.id] = {
            "slots": slot_dict,
            "channel_id": interaction.channel.id,
            "guild_id": interaction.guild.id,
            "header": header,
            "creator_id": interaction.user.id
        }

    asyncio.create_task(add_reactions())

    await interaction.followup.send("✅ Event wurde erstellt!", ephemeral=True)

# /event_delete Command
@bot.tree.command(name="event_delete", description="Löscht dein Event.")
async def event_delete(interaction: discord.Interaction, message_id: str):
    try:
        msg_id = int(message_id)
    except ValueError:
        await interaction.response.send_message("❌ Ungültige Nachricht-ID.", ephemeral=True)
        return

    if msg_id not in active_events:
        await interaction.response.send_message("❌ Kein aktives Event mit dieser ID gefunden.", ephemeral=True)
        return

    event = active_events[msg_id]

    # Nur der Ersteller darf löschen
    if interaction.user.id != event["creator_id"]:
        await interaction.response.send_message("❌ Du darfst dieses Event nicht löschen.", ephemeral=True)
        return

    guild = bot.get_guild(event["guild_id"])
    if not guild:
        await interaction.response.send_message("❌ Server nicht gefunden.", ephemeral=True)
        return

    channel = guild.get_channel(event["channel_id"])
    if not channel:
        await interaction.response.send_message("❌ Channel nicht gefunden.", ephemeral=True)
        return

    try:
        msg = await channel.fetch_message(msg_id)
        await msg.delete()
        del active_events[msg_id]
        await interaction.response.send_message("✅ Event wurde gelöscht.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler beim Löschen: {e}", ephemeral=True)

# Reaktionen verwalten
@bot.event
async def on_raw_reaction_add(payload):
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

# Dauerbetrieb
async def start_bot():
    while True:
        try:
            await bot.start(TOKEN)
        except Exception as e:
            print(f"❌ Bot abgestürzt: {e}, Neustart in 5 Sekunden...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(start_bot())
