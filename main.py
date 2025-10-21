import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
from datetime import datetime

# === Token ===
TOKEN = os.getenv("DISCORD_TOKEN")

# === Intents ===
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# === Globale Events ===
active_events = {}  # message_id: event_data

# === Emoji Normalisierung ===
def normalize_emoji(emoji):
    if isinstance(emoji, str):
        return emoji.strip()
    if hasattr(emoji, "id") and emoji.id:
        return f"<:{emoji.name}:{emoji.id}>"
    return emoji.name

# === Event-Text formatieren ===
def format_event_text(event, guild):
    text = "📋 **Eventübersicht:**\n"
    for emoji, slot in event["slots"].items():
        main_users = []
        wait_users = []
        for uid in slot["main"]:
            member = guild.get_member(uid)
            if member:
                main_users.append(member.mention)
        for uid in slot["waitlist"]:
            member = guild.get_member(uid)
            if member:
                wait_users.append(member.mention)
        text += f"\n{emoji} ({len(main_users)}/{slot['limit']}): " + (", ".join(main_users) if main_users else "-")
        if wait_users:
            text += f"\n   ⏳ Warteliste: " + ", ".join(wait_users)
    return text + "\n"

# === Event erstellen / Command ===
@bot.tree.command(name="event", description="Erstellt ein Event mit Slots")
@app_commands.describe(
    slots="Slots definieren (z.B. <:Tank:ID>:2)"
)
async def event(interaction: discord.Interaction, slots: str):
    # --- Slot Parsing ---
    import re
    pattern = re.compile(r"(<a?:\w+:\d+>)\s*:\s*(\d+)|(\S+)\s*:\s*(\d+)")
    matches = pattern.findall(slots)
    if not matches:
        await interaction.response.send_message("❌ Keine gültigen Slots gefunden!", ephemeral=True)
        return

    slot_dict = {}
    description = ""
    for custom_emoji, custom_limit, normal_emoji, normal_limit in matches:
        if custom_emoji:
            emoji = normalize_emoji(custom_emoji)
            limit = int(custom_limit)
        else:
            emoji = normalize_emoji(normal_emoji)
            limit = int(normal_limit)
        slot_dict[emoji] = {"limit": limit, "main": set(), "waitlist": []}
        description += f"{emoji} (0/{limit}): -\n"

    header = f"📣 Neues Event!\n\nReagiert mit eurer Klasse:\n"

    await interaction.response.send_message("✅ Event erstellt!", ephemeral=True)
    msg = await interaction.channel.send(header + "\n" + description)

    # --- Reaktionen hinzufügen ---
    for emoji in slot_dict.keys():
        try:
            await msg.add_reaction(emoji)
        except:
            pass

    # --- Thread erstellen ---
    await asyncio.sleep(1)
    thread = None
    try:
        msg = await interaction.channel.fetch_message(msg.id)
        thread_name = f"Event-Log"
        thread = await msg.create_thread(name=thread_name, type=discord.ChannelType.public_thread)
        print(f"🧵 Thread erstellt: {thread.name}")
    except Exception as e:
        print(f"❌ Thread konnte nicht erstellt werden: {e}")

    # --- Event speichern ---
    active_events[msg.id] = {
        "slots": slot_dict,
        "channel_id": interaction.channel.id,
        "guild_id": interaction.guild.id,
        "header": header,
        "thread_id": thread.id if thread else None
    }

# === Reaktionen bearbeiten ===
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

    try:
        member = guild.get_member(payload.user_id)
        if not member:
            member = await guild.fetch_member(payload.user_id)
    except:
        return

    slot = event["slots"][emoji]

    # Prüfen, ob User schon irgendwo drin ist
    already_in_any = any(payload.user_id in s["main"] or payload.user_id in s["waitlist"] for s in event["slots"].values())
    if already_in_any:
        return

    # Slot füllen oder Warteliste
    if len(slot["main"]) < slot["limit"]:
        slot["main"].add(payload.user_id)
        # Thread log
        if event["thread_id"]:
            thread = guild.get_channel(event["thread_id"])
            if thread:
                await thread.send(f"✅ {member.mention} hat Slot {emoji} besetzt")
    else:
        slot["waitlist"].append(payload.user_id)
        if event["thread_id"]:
            thread = guild.get_channel(event["thread_id"])
            if thread:
                await thread.send(f"⏳ {member.mention} wurde auf Warteliste {emoji} gesetzt")

    # Event-Message updaten
    channel = guild.get_channel(event["channel_id"])
    if channel:
        try:
            msg = await channel.fetch_message(payload.message_id)
            await msg.edit(content=event["header"] + "\n\n" + format_event_text(event, guild))
        except:
            pass

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
    if not guild:
        return

    try:
        member = guild.get_member(payload.user_id)
        if not member:
            member = await guild.fetch_member(payload.user_id)
    except:
        member = None

    if payload.user_id in slot["main"]:
        slot["main"].remove(payload.user_id)
        # Warteliste nachrücken
        if slot["waitlist"]:
            next_user = slot["waitlist"].pop(0)
            slot["main"].add(next_user)
            try:
                next_member = await guild.fetch_member(next_user)
                if event["thread_id"]:
                    thread = guild.get_channel(event["thread_id"])
                    if thread:
                        await thread.send(f"➡️ {next_member.mention} ist von Warteliste nachgerückt")
            except:
                pass

    elif payload.user_id in slot["waitlist"]:
        slot["waitlist"].remove(payload.user_id)

    # Event-Message updaten
    channel = guild.get_channel(event["channel_id"])
    if channel:
        try:
            msg = await channel.fetch_message(payload.message_id)
            await msg.edit(content=event["header"] + "\n\n" + format_event_text(event, guild))
        except:
            pass

# === Bot starten ===
@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user}")
    try:
        await bot.tree.sync()
        print("📂 Slash Commands synchronisiert")
    except:
        pass

bot.run(TOKEN)
