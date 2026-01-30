# SlotBot - Clean Rebuild (Option A)
# Web Service (Flask) + Discord Slash Commands (interaction-safe)
# Python 3.11 + discord.py 2.6.x

import os
import json
import asyncio
import threading
import uuid
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

import discord
from discord import app_commands
from flask import Flask

# =========================
# Config
# =========================
PORT = int(os.environ.get("PORT", "10000"))
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
DATA_FILE = Path("events.json")

# =========================
# Flask (Render Web Service requirement)
# =========================
app = Flask("slotbot")

@app.get("/")
def index():
    return "SlotBot is running.", 200

def run_flask():
    # Use reloader OFF to avoid double-start
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# =========================
# Persistence
# =========================
def load_events() -> Dict[str, Dict[str, Any]]:
    if not DATA_FILE.exists():
        return {}
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_events(events: Dict[str, Dict[str, Any]]) -> None:
    try:
        DATA_FILE.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("⚠️  Could not save events:", e)

EVENTS: Dict[str, Dict[str, Any]] = load_events()
print(f"✅ {len(EVENTS)} gespeicherte Events geladen.")

# =========================
# Discord Client + Tree
# =========================
intents = discord.Intents.none()
intents.guilds = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

_synced = False
_scheduler_task: Optional[asyncio.Task] = None
_flask_started = False

# =========================
# Time helpers
# =========================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def parse_dt_utc(dt_str: str) -> datetime:
    """
    Accepts:
      - 'YYYY-MM-DD HH:MM' (assumed UTC)
      - ISO like '2026-01-30T19:30:00+00:00'
      - ISO with Z suffix
      - unix seconds
    """
    s = (dt_str or "").strip()
    if not s:
        raise ValueError("Startzeit fehlt.")
    if s.isdigit():
        return datetime.fromtimestamp(int(s), tz=timezone.utc)

    s = s.replace("Z", "+00:00")
    # common 'YYYY-MM-DD HH:MM'
    try:
        if "T" not in s and len(s) <= 16:
            return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        raise ValueError("Zeitformat ungültig. Nutze z.B. `2026-01-30 19:30` (UTC) oder Unix-Timestamp.")

# =========================
# Interaction-safe responders
# =========================
async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool) -> None:
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral, thinking=True)
    except Exception as e:
        # Already acknowledged or expired
        print("⚠️ defer failed:", e)

async def safe_send(
    interaction: discord.Interaction,
    *,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
    ephemeral: bool = False,
) -> None:
    """Sends exactly one response path that won't hang interactions."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=ephemeral)
        else:
            await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=ephemeral)
    except Exception as e:
        print("⚠️ send failed:", e)

# =========================
# Discord fetch helpers (rate-limit friendly)
# =========================
async def fetch_channel(guild: discord.Guild, channel_id: int):
    ch = guild.get_channel(channel_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(channel_id)
        except Exception:
            return None
    return ch

async def fetch_message(channel: discord.abc.Messageable, message_id: int):
    try:
        return await channel.fetch_message(message_id)
    except Exception:
        return None

# =========================
# Event rendering
# =========================
def event_embed(ev: Dict[str, Any]) -> discord.Embed:
    start_dt = datetime.fromisoformat(ev["start_utc"]).astimezone(timezone.utc)
    slots = int(ev["slots"])
    participants: List[int] = ev.get("participants", [])
    waitlist: List[int] = ev.get("waitlist", [])
    afk_checked = set(ev.get("afk_checked", []))

    def fmt(ids: List[int]) -> str:
        return "\n".join([f"<@{uid}>" for uid in ids]) if ids else "—"

    emb = discord.Embed(title=ev["title"], description="SlotBot Event", timestamp=start_dt)
    emb.add_field(name="🕒 Start (UTC)", value=start_dt.strftime("%Y-%m-%d %H:%M"), inline=True)
    emb.add_field(name="🎟️ Slots", value=f"{len(participants)}/{slots}", inline=True)
    emb.add_field(name="✅ Teilnehmer", value=fmt(participants), inline=False)
    emb.add_field(name="⏳ Warteliste", value=fmt(waitlist), inline=False)

    if participants:
        missing = [uid for uid in participants if uid not in afk_checked]
        emb.add_field(name="🟡 AFK-Check offen", value=fmt(missing), inline=False)

    emb.set_footer(text=f"Event-ID: {ev['event_id']}")
    return emb

def afk_open(ev: Dict[str, Any], t: datetime) -> bool:
    start = datetime.fromisoformat(ev["start_utc"]).astimezone(timezone.utc)
    return (start - timedelta(minutes=30)) <= t <= start

def afk_finalize_window(ev: Dict[str, Any], t: datetime) -> bool:
    start = datetime.fromisoformat(ev["start_utc"]).astimezone(timezone.utc)
    return (start - timedelta(minutes=10)) <= t <= start

async def ensure_thread(message: discord.Message, ev: Dict[str, Any]) -> Optional[discord.Thread]:
    tid = ev.get("thread_id")
    if tid:
        th = message.guild.get_thread(int(tid))
        if th:
            return th
        try:
            ch = await message.guild.fetch_channel(int(tid))
            if isinstance(ch, discord.Thread):
                return ch
        except Exception:
            pass

    try:
        th = await message.create_thread(name=f"🧵 {ev['title']}", auto_archive_duration=1440)
        ev["thread_id"] = th.id
        save_events(EVENTS)
        return th
    except Exception as e:
        print("⚠️ thread create failed:", e)
        return None

async def refresh_event_message(guild: discord.Guild, ev: Dict[str, Any]) -> None:
    channel = await fetch_channel(guild, int(ev["channel_id"]))
    if not channel:
        return
    msg = await fetch_message(channel, int(ev["message_id"]))
    if not msg:
        return
    try:
        await msg.edit(embed=event_embed(ev), view=EventView(ev["event_id"]))
    except Exception as e:
        print("⚠️ message edit failed:", e)

# =========================
# UI View
# =========================
class EventView(discord.ui.View):
    def __init__(self, ev_id: str):
        super().__init__(timeout=None)  # persistent
        self.ev_id = ev_id

    @discord.ui.button(label="✅ Join", style=discord.ButtonStyle.success, custom_id="slotbot_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await safe_defer(interaction, ephemeral=True)
        ev = EVENTS.get(self.ev_id)
        if not ev:
            return await safe_send(interaction, content="❌ Event nicht gefunden.", ephemeral=True)

        uid = interaction.user.id
        participants = ev.setdefault("participants", [])
        waitlist = ev.setdefault("waitlist", [])
        slots = int(ev["slots"])

        if uid in participants:
            return await safe_send(interaction, content="Du bist schon drin.", ephemeral=True)
        if uid in waitlist:
            return await safe_send(interaction, content="Du bist schon auf der Warteliste.", ephemeral=True)

        if len(participants) < slots:
            participants.append(uid)
            msg_txt = "✅ Du bist dem Event beigetreten."
        else:
            waitlist.append(uid)
            msg_txt = "⏳ Event voll – du bist auf der Warteliste."

        save_events(EVENTS)
        if interaction.guild:
            await refresh_event_message(interaction.guild, ev)
        await safe_send(interaction, content=msg_txt, ephemeral=True)

    @discord.ui.button(label="🚪 Leave", style=discord.ButtonStyle.secondary, custom_id="slotbot_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await safe_defer(interaction, ephemeral=True)
        ev = EVENTS.get(self.ev_id)
        if not ev:
            return await safe_send(interaction, content="❌ Event nicht gefunden.", ephemeral=True)

        uid = interaction.user.id
        participants = ev.setdefault("participants", [])
        waitlist = ev.setdefault("waitlist", [])
        afk_checked = set(ev.get("afk_checked", []))

        removed = False
        if uid in participants:
            participants.remove(uid)
            removed = True
        if uid in waitlist:
            waitlist.remove(uid)
            removed = True
        if uid in afk_checked:
            afk_checked.discard(uid)
            ev["afk_checked"] = list(afk_checked)

        # promote from waitlist if free slot
        slots = int(ev["slots"])
        if len(participants) < slots and waitlist:
            promoted = waitlist.pop(0)
            participants.append(promoted)

        save_events(EVENTS)
        if interaction.guild:
            await refresh_event_message(interaction.guild, ev)
        await safe_send(interaction, content=("🚪 Du bist raus." if removed else "Du warst nicht eingetragen."), ephemeral=True)

    @discord.ui.button(label="🟡 AFK-Check", style=discord.ButtonStyle.primary, custom_id="slotbot_afk")
    async def afk(self, interaction: discord.Interaction, button: discord.ui.Button):
        await safe_defer(interaction, ephemeral=True)
        ev = EVENTS.get(self.ev_id)
        if not ev:
            return await safe_send(interaction, content="❌ Event nicht gefunden.", ephemeral=True)

        t = now_utc()
        if not afk_open(ev, t):
            return await safe_send(interaction, content="⏳ AFK-Check ist erst 30 Minuten vor Start möglich.", ephemeral=True)

        uid = interaction.user.id
        participants = ev.setdefault("participants", [])
        if uid not in participants:
            return await safe_send(interaction, content="Du bist nicht in der Teilnehmerliste.", ephemeral=True)

        afk_checked = set(ev.get("afk_checked", []))
        afk_checked.add(uid)
        ev["afk_checked"] = list(afk_checked)
        save_events(EVENTS)

        if interaction.guild:
            await refresh_event_message(interaction.guild, ev)
        await safe_send(interaction, content="✅ AFK-Check bestätigt.", ephemeral=True)

# =========================
# Background Scheduler
# =========================
async def scheduler_loop():
    print("⏱️ Scheduler gestartet.")
    while True:
        try:
            t = now_utc()
            changed = False

            for ev_id, ev in list(EVENTS.items()):
                if "guild_id" not in ev or "start_utc" not in ev:
                    continue

                guild = client.get_guild(int(ev["guild_id"]))
                if guild is None:
                    continue

                channel = await fetch_channel(guild, int(ev["channel_id"]))
                if channel is None:
                    continue

                start = datetime.fromisoformat(ev["start_utc"]).astimezone(timezone.utc)
                sent = set(ev.get("reminders_sent", []))

                async def send_once(key: str, text: str):
                    nonlocal changed
                    if key in sent:
                        return
                    try:
                        await channel.send(text)
                        sent.add(key)
                        ev["reminders_sent"] = list(sent)
                        changed = True
                    except Exception as e:
                        print("⚠️ reminder send failed:", e)

                # 60 min reminder
                if (start - timedelta(minutes=60)) <= t <= (start - timedelta(minutes=59, seconds=30)):
                    await send_once("60", f"⏰ Erinnerung: **{ev['title']}** startet in 60 Minuten. AFK-Check ab 30 Minuten vor Start!")

                # 30 min reminder
                if (start - timedelta(minutes=30)) <= t <= (start - timedelta(minutes=29, seconds=30)):
                    await send_once("30", f"🟡 AFK-Check offen: **{ev['title']}**. Bitte jetzt bestätigen!")

                # finalize 10 min before (once)
                if afk_finalize_window(ev, t) and not ev.get("afk_finalized", False):
                    participants: List[int] = ev.get("participants", [])
                    waitlist: List[int] = ev.get("waitlist", [])
                    slots = int(ev["slots"])
                    afk_checked = set(ev.get("afk_checked", []))

                    kicked = [uid for uid in participants if uid not in afk_checked]
                    kept = [uid for uid in participants if uid in afk_checked]

                    while len(kept) < slots and waitlist:
                        kept.append(waitlist.pop(0))

                    ev["participants"] = kept
                    ev["waitlist"] = waitlist
                    ev["afk_finalized"] = True
                    changed = True

                    try:
                        if kicked:
                            await channel.send("🚫 AFK-Check nicht bestanden, raus: " + " ".join([f"<@{u}>" for u in kicked]))
                        await channel.send("✅ Teilnehmerliste aktualisiert. (Nachrücker wurden ggf. gezogen.)")
                    except Exception as e:
                        print("⚠️ finalize announce failed:", e)

                    await refresh_event_message(guild, ev)

            if changed:
                save_events(EVENTS)

        except Exception as e:
            print("⚠️ Scheduler error:", e)

        await asyncio.sleep(10)

# =========================
# Slash Commands
# =========================
@tree.command(name="test", description="Check ob SlotBot lebt")
async def test_cmd(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    await safe_send(interaction, content="✅ SlotBot ist online und reagiert.", ephemeral=True)

@tree.command(name="roll", description="Würfeln")
@app_commands.describe(sides="Wie viele Seiten? (Standard 100)", times="Wie oft würfeln? (Standard 1)")
async def roll_cmd(interaction: discord.Interaction, sides: int = 100, times: int = 1):
    await safe_defer(interaction, ephemeral=False)
    sides = max(2, min(10_000, int(sides)))
    times = max(1, min(20, int(times)))
    rolls = [random.randint(1, sides) for _ in range(times)]
    txt = ", ".join(map(str, rolls))
    await safe_send(interaction, content=f"🎲 {interaction.user.mention} würfelt ({times}× d{sides}): **{txt}**", ephemeral=False)

event_group = app_commands.Group(name="event", description="Event Verwaltung")

@event_group.command(name="create", description="Event erstellen (Thread + Slots + Warteliste + AFK-Check)")
@app_commands.describe(title="Event Titel", start_utc="Startzeit UTC (z.B. 2026-01-30 19:30)", slots="Slots (Standard 10)")
async def event_create(interaction: discord.Interaction, title: str, start_utc: str, slots: int = 10):
    await safe_defer(interaction, ephemeral=False)

    try:
        start_dt = parse_dt_utc(start_utc)
    except Exception as e:
        return await safe_send(interaction, content=f"❌ {e}", ephemeral=True)

    slots = max(1, min(50, int(slots)))
    ev_id = str(uuid.uuid4())[:8]

    channel = interaction.channel
    if channel is None or not isinstance(channel, discord.abc.Messageable):
        return await safe_send(interaction, content="❌ Kein gültiger Channel.", ephemeral=True)

    # Guard: prevent duplicate creation on same interaction (Discord retries)
    # We use interaction.id as a last-seen key in memory
    last_key = f"__last_create_{interaction.id}"
    if last_key in EVENTS:
        return await safe_send(interaction, content="⚠️ Dieser Create wurde schon verarbeitet.", ephemeral=True)
    EVENTS[last_key] = {"ts": now_utc().isoformat()}

    ev: Dict[str, Any] = {
        "event_id": ev_id,
        "guild_id": interaction.guild_id,
        "channel_id": channel.id,
        "title": title,
        "start_utc": start_dt.astimezone(timezone.utc).isoformat(),
        "slots": slots,
        "participants": [],
        "waitlist": [],
        "afk_checked": [],
        "afk_finalized": False,
        "reminders_sent": [],
        "created_by": interaction.user.id,
    }

    msg = await channel.send(embed=event_embed(ev), view=EventView(ev_id))
    ev["message_id"] = msg.id

    th = await ensure_thread(msg, ev)
    if th:
        try:
            await th.send("🧵 Thread erstellt. Hier kann alles zum Event besprochen werden.")
        except Exception:
            pass

    EVENTS.pop(last_key, None)
    EVENTS[ev_id] = ev
    save_events(EVENTS)

    await safe_send(interaction, content=f"✅ Event erstellt: **{title}** (ID: `{ev_id}`)", ephemeral=False)

@event_group.command(name="edit", description="Event bearbeiten")
@app_commands.describe(event_id="Event-ID", title="Neuer Titel (optional)", start_utc="Neue Startzeit UTC (optional)", slots="Neue Slot-Anzahl (optional)")
async def event_edit(interaction: discord.Interaction, event_id: str, title: Optional[str] = None, start_utc: Optional[str] = None, slots: Optional[int] = None):
    await safe_defer(interaction, ephemeral=True)

    ev = EVENTS.get(event_id)
    if not ev:
        return await safe_send(interaction, content="❌ Event nicht gefunden.", ephemeral=True)

    if title:
        ev["title"] = title

    if start_utc:
        try:
            ev["start_utc"] = parse_dt_utc(start_utc).isoformat()
            ev["reminders_sent"] = []
            ev["afk_finalized"] = False
        except Exception as e:
            return await safe_send(interaction, content=f"❌ {e}", ephemeral=True)

    if slots is not None:
        new_slots = max(1, min(50, int(slots)))
        ev["slots"] = new_slots
        participants: List[int] = ev.get("participants", [])
        waitlist: List[int] = ev.get("waitlist", [])
        while len(participants) > new_slots:
            waitlist.insert(0, participants.pop())
        ev["participants"] = participants
        ev["waitlist"] = waitlist

    save_events(EVENTS)

    guild = client.get_guild(int(ev["guild_id"]))
    if guild:
        await refresh_event_message(guild, ev)

    await safe_send(interaction, content="✅ Event aktualisiert.", ephemeral=True)

@event_group.command(name="delete", description="Event löschen")
@app_commands.describe(event_id="Event-ID")
async def event_delete(interaction: discord.Interaction, event_id: str):
    await safe_defer(interaction, ephemeral=True)

    ev = EVENTS.get(event_id)
    if not ev:
        return await safe_send(interaction, content="❌ Event nicht gefunden.", ephemeral=True)

    guild = client.get_guild(int(ev["guild_id"]))
    if guild:
        channel = await fetch_channel(guild, int(ev["channel_id"]))
        if channel:
            msg = await fetch_message(channel, int(ev["message_id"]))
            if msg:
                try:
                    await msg.delete()
                except Exception:
                    pass

        tid = ev.get("thread_id")
        if tid:
            th = guild.get_thread(int(tid))
            if th is None:
                try:
                    ch = await guild.fetch_channel(int(tid))
                    if isinstance(ch, discord.Thread):
                        th = ch
                except Exception:
                    th = None
            if th:
                try:
                    await th.delete()
                except Exception:
                    pass

    EVENTS.pop(event_id, None)
    save_events(EVENTS)

    await safe_send(interaction, content="🗑️ Event gelöscht.", ephemeral=True)

tree.add_command(event_group)

# =========================
# Events
# =========================
@client.event
async def on_ready():
    global _synced, _scheduler_task, _flask_started

    print(f"🚀 Starte SlotBot + Flask (Web Service stabil) ...")
    print(f"🤖 Discord: bereit als {client.user}")

    # Start flask exactly once
    if not _flask_started:
        _flask_started = True
        threading.Thread(target=run_flask, daemon=True).start()

    # Register persistent views for existing events so buttons work after restart
    for ev_id, ev in list(EVENTS.items()):
        if isinstance(ev, dict) and ev.get("event_id"):
            try:
                client.add_view(EventView(ev_id))
            except Exception:
                pass

    # Sync slash commands exactly once
    if not _synced:
        try:
            await tree.sync()
            _synced = True
            print("✅ Slash Commands synchronisiert.")
        except Exception as e:
            print("⚠️ tree.sync failed:", e)

    # Start scheduler exactly once
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(scheduler_loop())

# =========================
# Entrypoint
# =========================
def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN fehlt in den Environment Variablen!")

    # NOTE: Flask starts in on_ready thread to avoid double-start on import.
    client.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
