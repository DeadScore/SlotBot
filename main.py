import os
# ================================
# SlotBot – FULL main.py
# ================================

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
from datetime import datetime, timedelta, timezone
import re
import random

# ================================
# CONFIG
# ================================
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_IDS = {404173735130562562}

EVENT_AUTO_DELETE_HOURS = 2
REMINDER_BEFORE_MIN = 60

AFK_ENABLED_DEFAULT = True
AFK_START_BEFORE_MIN = 30
AFK_DURATION_MIN = 20
AFK_DM_INTERVAL_MIN = 5

# ================================
# INTENTS
# ================================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ================================
# STORAGE
# ================================
events = {}
rolls = {}

# ================================
# HELPERS
# ================================
def utcnow():
    return datetime.now(timezone.utc)

def is_owner(user_id: int):
    return user_id in OWNER_IDS

SLOT_REGEX = re.compile(
    r"(?P<emoji><a?:\w+:\d+>|[\U00010000-\U0010ffff]|.)\s*:?+\s*(?P<count>\d+)",
    re.UNICODE,
)

# ================================
# AFK BUTTON
# ================================
class AFKConfirmView(discord.ui.View):
    def __init__(self, event_id: int, user_id: int):
        super().__init__(timeout=None)
        self.event_id = event_id
        self.user_id = user_id

    @discord.ui.button(label="✅ Ich bin da", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Das ist nicht dein AFK-Check.", ephemeral=True
            )
            return

        ev = events.get(self.event_id)
        if not ev:
            await interaction.response.send_message(
                "❌ Event nicht gefunden.", ephemeral=True
            )
            return

        ev["afk_confirmed"].add(self.user_id)
        await interaction.response.send_message(
            "✅ Bestätigt! Danke.", ephemeral=True
        )

        await ev["thread"].send(
            f"✅ **AFK bestätigt:** {interaction.user.mention}"
        )

# ================================
# READY
# ================================
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Bot gestartet als {bot.user}")

# ================================
# EVENT COMMAND
# ================================
@bot.tree.command(name="event", description="Erstellt ein Event")
async def event(
    interaction: discord.Interaction,
    titel: str,
    start_in_min: int,
    slots: str,
    afk_check: bool = True,
):
    await interaction.response.defer()

    start_time = utcnow() + timedelta(minutes=start_in_min)

    slot_lines = []
    slot_data = {}

    for m in SLOT_REGEX.finditer(slots):
        emoji = m.group("emoji")
        count = int(m.group("count"))
        slot_data[emoji] = {
            "max": count,
            "users": set()
        }
        slot_lines.append(f"{emoji} {count}/{count}")

    embed = discord.Embed(
        title=titel,
        description="\n".join(slot_lines),
        color=0x2ECC71,
    )
    embed.add_field(
        name="⏰ Start",
        value=start_time.strftime("%d.%m.%Y %H:%M UTC"),
        inline=False,
    )

    msg = await interaction.followup.send(embed=embed)
    thread = await msg.create_thread(
        name=f"🧵 {titel}",
        auto_archive_duration=1440
    )

    events[msg.id] = {
        "message": msg,
        "thread": thread,
        "start": start_time,
        "slots": slot_data,
        "afk_enabled": afk_check,
        "afk_confirmed": set(),
        "created_by": interaction.user.id,
    }

    for emoji in slot_data:
        try:
            await msg.add_reaction(emoji)
        except:
            pass

    await thread.send("🧵 **Thread erstellt – alle Updates landen hier.**")

    schedule_event_tasks(msg.id)

# ================================
# REACTION HANDLING
# ================================
@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    ev = events.get(payload.message_id)
    if not ev:
        return

    emoji = str(payload.emoji)
    slot = ev["slots"].get(emoji)
    if not slot:
        return

    if len(slot["users"]) >= slot["max"]:
        return

    slot["users"].add(payload.user_id)
    await update_event_post(payload.message_id)

@bot.event
async def on_raw_reaction_remove(payload):
    ev = events.get(payload.message_id)
    if not ev:
        return

    emoji = str(payload.emoji)
    slot = ev["slots"].get(emoji)
    if not slot:
        return

    slot["users"].discard(payload.user_id)
    await update_event_post(payload.message_id)

async def update_event_post(event_id: int):
    ev = events[event_id]
    msg = ev["message"]

    lines = []
    for emoji, data in ev["slots"].items():
        lines.append(f"{emoji} {len(data['users'])}/{data['max']}")

    embed = msg.embeds[0]
    embed.description = "\n".join(lines)
    await msg.edit(embed=embed)

# ================================
# SCHEDULE TASKS
# ================================
def schedule_event_tasks(event_id: int):
    ev = events[event_id]

    async def reminder():
        await asyncio.sleep(
            (ev["start"] - timedelta(minutes=REMINDER_BEFORE_MIN) - utcnow()).total_seconds()
        )
        await ev["thread"].send("🔔 **Reminder: Event startet in 60 Minuten!**")

    async def afk_check():
        if not ev["afk_enabled"]:
            return

        await asyncio.sleep(
            (ev["start"] - timedelta(minutes=AFK_START_BEFORE_MIN) - utcnow()).total_seconds()
        )

        await ev["thread"].send("🟡 **AFK-Check gestartet**")

        participants = set()
        for slot in ev["slots"].values():
            participants |= slot["users"]

        end_time = utcnow() + timedelta(minutes=AFK_DURATION_MIN)

        while utcnow() < end_time:
            for uid in participants:
                if uid in ev["afk_confirmed"]:
                    continue
                try:
                    user = await bot.fetch_user(uid)
                    await user.send(
                        "⏰ **AFK-Check** – bitte bestätigen:",
                        view=AFKConfirmView(event_id, uid),
                    )
                except:
                    pass
            await asyncio.sleep(AFK_DM_INTERVAL_MIN * 60)

        for uid in participants:
            if uid not in ev["afk_confirmed"]:
                await ev["thread"].send(
                    f"⚠️ **Nicht bestätigt (Soft-Kick):** <@{uid}>"
                )

        await ev["thread"].send("✅ **AFK-Check abgeschlossen**")

    async def auto_delete():
        await asyncio.sleep(
            (ev["start"] + timedelta(hours=EVENT_AUTO_DELETE_HOURS) - utcnow()).total_seconds()
        )
        await ev["message"].delete()
        await ev["thread"].delete()
        events.pop(event_id, None)

    bot.loop.create_task(reminder())
    bot.loop.create_task(afk_check())
    bot.loop.create_task(auto_delete())

# ================================
# ROLL SYSTEM
# ================================
@bot.tree.command(name="start_roll", description="Startet einen Roll")
async def start_roll(interaction: discord.Interaction, dauer: int):
    rolls[interaction.channel.id] = {
        "end": utcnow() + timedelta(minutes=dauer),
        "values": {}
    }
    await interaction.response.send_message("🎲 Roll gestartet! Nutze /roll")

@bot.tree.command(name="roll", description="Würfelt (1x)")
async def roll(interaction: discord.Interaction):
    r = rolls.get(interaction.channel.id)
    if not r:
        await interaction.response.send_message("❌ Kein aktiver Roll", ephemeral=True)
        return
    if interaction.user.id in r["values"]:
        await interaction.response.send_message("❌ Du hast schon gewürfelt", ephemeral=True)
        return

    value = random.randint(1, 100)
    r["values"][interaction.user.id] = value
    await interaction.response.send_message(
        f"🎲 {interaction.user.mention} würfelt **{value}**"
    )

@bot.tree.command(name="stop_roll", description="Beendet den Roll")
async def stop_roll(interaction: discord.Interaction):
    r = rolls.pop(interaction.channel.id, None)
    if not r:
        await interaction.response.send_message("❌ Kein aktiver Roll", ephemeral=True)
        return

    if not r["values"]:
        await interaction.response.send_message("❌ Niemand hat gewürfelt")
        return

    winner = max(r["values"].items(), key=lambda x: x[1])[0]
    await interaction.response.send_message(f"🏆 Gewinner: <@{winner}>")

# ================================
# RUN
# ================================

# =========================
# Bot Start (SAFE)
# =========================
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN ist nicht gesetzt (Render Environment Variable).")
    bot.run(TOKEN)
