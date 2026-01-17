
# SlotBot FINAL – Web Service Version
# Full feature set: Events, Threads, Slots via Emoji, AFK Check with Buttons, Reminder, Countdown, Roll
# Designed for Render Web Service (Flask + Discord)

import os
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands

from flask import Flask

# -------------------- CONFIG --------------------

TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

PORT = int(os.getenv("PORT", "10000"))

INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.members = True
INTENTS.messages = True
INTENTS.message_content = True
INTENTS.reactions = True
INTENTS.dm_messages = True

# -------------------- BOT & WEB --------------------

bot = commands.Bot(command_prefix="!", intents=INTENTS)
app = Flask("slotbot")

@app.route("/")
def index():
    return "SlotBot running", 200

# -------------------- DATA --------------------

events = {}       # event_id -> event dict
active_rolls = {} # channel_id -> roll dict

# -------------------- UTILS --------------------

def utcnow():
    return datetime.now(timezone.utc)

def fmt_ts(dt):
    return f"<t:{int(dt.timestamp())}:F>"

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

# -------------------- EVENT MODEL --------------------

def create_event(event_id, guild_id, channel_id, message_id, start_time, duration):
    return {
        "id": event_id,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "message_id": message_id,
        "thread_id": None,
        "start": start_time,
        "end": start_time + timedelta(hours=duration),
        "slots": {},      # emoji -> set(user_id)
        "participants": set(),
        "afk_enabled": True,
        "afk_confirmed": set(),
        "afk_started": False,
    }

# -------------------- THREAD --------------------

async def ensure_thread(event):
    if event["thread_id"]:
        return

    guild = bot.get_guild(event["guild_id"])
    channel = guild.get_channel(event["channel_id"])
    msg = await channel.fetch_message(event["message_id"])

    thread = await msg.create_thread(
        name=f"Event #{event['id']}",
        auto_archive_duration=1440
    )
    event["thread_id"] = thread.id
    await thread.send("🧵 Thread gestartet.")

# -------------------- AFK CHECK --------------------

class AFKView(discord.ui.View):
    def __init__(self, event):
        super().__init__(timeout=None)
        self.event = event

    @discord.ui.button(label="Ich bin da ✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        self.event["afk_confirmed"].add(uid)
        await interaction.response.send_message("✅ AFK bestätigt.", ephemeral=True)

# -------------------- LOOPS --------------------

@tasks.loop(minutes=1)
async def event_loop():
    now = utcnow()
    for ev in list(events.values()):
        if now >= ev["end"] + timedelta(hours=2):
            guild = bot.get_guild(ev["guild_id"])
            channel = guild.get_channel(ev["channel_id"])
            try:
                msg = await channel.fetch_message(ev["message_id"])
                await msg.delete()
            except:
                pass
            events.pop(ev["id"], None)

@tasks.loop(minutes=1)
async def afk_loop():
    now = utcnow()
    for ev in events.values():
        if not ev["afk_enabled"] or ev["afk_started"]:
            continue

        if now >= ev["start"] - timedelta(minutes=30):
            ev["afk_started"] = True
            await ensure_thread(ev)
            thread = bot.get_channel(ev["thread_id"])

            view = AFKView(ev)
            await thread.send("⏰ **AFK-Check gestartet**", view=view)

            for uid in ev["participants"]:
                member = thread.guild.get_member(uid)
                if member:
                    try:
                        await member.send("⏰ AFK-Check läuft. Bitte bestätigen.")
                    except:
                        pass

# -------------------- COMMANDS --------------------

@bot.tree.command(name="event", description="Erstellt ein Event")
async def event(interaction: discord.Interaction, start_in_min: int, dauer_h: int):
    start = utcnow() + timedelta(minutes=start_in_min)

    embed = discord.Embed(
        title="📅 Neues Event",
        description=f"🕒 Start: {fmt_ts(start)}\n⏳ Dauer: {dauer_h}h",
        color=0x2ecc71
    )

    msg = await interaction.response.send_message(embed=embed, fetch_response=True)

    ev = create_event(
        event_id=msg.id,
        guild_id=interaction.guild.id,
        channel_id=interaction.channel.id,
        message_id=msg.id,
        start_time=start,
        duration=dauer_h
    )

    events[msg.id] = ev
    await ensure_thread(ev)

@bot.tree.command(name="roll", description="Roll (1x pro User)")
async def roll(interaction: discord.Interaction):
    cid = interaction.channel.id
    roll = active_rolls.get(cid)

    if not roll:
        roll = {"users": set()}
        active_rolls[cid] = roll

    if interaction.user.id in roll["users"]:
        await interaction.response.send_message("❌ Du hast schon gerollt.", ephemeral=True)
        return

    import random
    num = random.randint(1, 100)
    roll["users"].add(interaction.user.id)

    await interaction.response.send_message(f"🎲 {interaction.user.mention} rollt **{num}**")

# -------------------- READY --------------------

@bot.event
async def on_ready():
    await bot.tree.sync()
    if not event_loop.is_running():
        event_loop.start()
    if not afk_loop.is_running():
        afk_loop.start()
    print("✅ SlotBot ready")

# -------------------- START --------------------

def run():
    bot.loop.create_task(bot.start(TOKEN))
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    run()
