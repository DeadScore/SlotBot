
# --- SlotBot v4.6 FINAL ---
# Reminder: 60 Minuten
# AFK-Check: Start 30 Min vorher, Dauer 20 Min, alle 5 Min
# AFK im Thread | pro Event deaktivierbar | Auto-Freigabe
# Treffpunkt Edit mit Durchstreichung
# Kein Punkte-System

import os
import asyncio
import threading
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks
from flask import Flask

TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)
flask_app = Flask("bot_flask")

events = {}

@flask_app.route("/")
def home():
    return "SlotBot running", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

@bot.event
async def on_ready():
    print(f"✅ SlotBot online als {bot.user}")

def schedule_tasks(event_id):
    reminder_task.start(event_id)
    afk_check_task.start(event_id)

@tasks.loop(count=1)
async def reminder_task(event_id):
    event = events.get(event_id)
    if not event:
        return
    await asyncio.sleep(max(0, event["start_ts"] - 3600 - int(datetime.utcnow().timestamp())))
    await event["channel"].send("⏰ **Reminder:** Event startet in 60 Minuten!")

@tasks.loop(count=4)
async def afk_check_task(event_id):
    event = events.get(event_id)
    if not event or not event.get("afk_enabled", True):
        return

    thread = event["thread"]
    unanswered = set(event["participants"]) - set(event["afk_confirmed"])
    if not unanswered:
        return

    msg = await thread.send("🕵️ **AFK-Check:** Bitte reagieren, wenn du noch dabei bist!")

    def check(reaction, user):
        return reaction.message.id == msg.id and user.id in unanswered

    try:
        reaction, user = await bot.wait_for("reaction_add", timeout=300, check=check)
        event["afk_confirmed"].add(user.id)
    except asyncio.TimeoutError:
        pass

@afk_check_task.after_loop
async def afk_cleanup():
    for event in events.values():
        missing = set(event["participants"]) - set(event["afk_confirmed"])
        for uid in missing:
            event["participants"].remove(uid)
        if missing:
            await event["thread"].send(
                f"❌ **AFK:** {len(missing)} Slot(s) wurden automatisch freigegeben."
            )

@bot.command()
async def create(ctx, *, title):
    thread = await ctx.channel.create_thread(name=title)
    event_id = str(thread.id)

    events[event_id] = {
        "title": title,
        "thread": thread,
        "channel": ctx.channel,
        "participants": set(),
        "afk_confirmed": set(),
        "afk_enabled": True,
        "start_ts": int((datetime.utcnow() + timedelta(hours=2)).timestamp())
    }

    schedule_tasks(event_id)
    await ctx.send(f"✅ Event **{title}** erstellt")

@bot.command()
async def afk(ctx, mode: str):
    for event in events.values():
        if event["thread"].id == ctx.channel.id:
            event["afk_enabled"] = mode.lower() == "on"
            await ctx.send(f"🕵️ AFK-Check {'aktiviert' if mode=='on' else 'deaktiviert'}")
            return

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(TOKEN)
