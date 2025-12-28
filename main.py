
import os
import re
import io
import json
import asyncio
import base64
from datetime import datetime, timedelta
from threading import Thread
from typing import Dict, Any, List, Tuple

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")


if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN fehlt in den Render Environment Variables.")

def parse_date_flexible(date_str: str) -> str:
    """Erlaubt 'heute', 'morgen', 'übermorgen' sowie DD.MM, DD.MM.YY, DD.MM.YYYY. Gibt immer DD.MM.YYYY zurück."""
    if not date_str:
        raise ValueError("Datum fehlt")
    s = date_str.strip().lower()
    today = datetime.now(BERLIN_TZ).date()

    if s in ("heute", "today"):
        d = today
    elif s in ("morgen", "tomorrow"):
        d = today + timedelta(days=1)
    elif s in ("übermorgen", "uebermorgen"):
        d = today + timedelta(days=2)
    else:
        d = None
        for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d.%m"):
            try:
                parsed = datetime.strptime(s, fmt)
                if fmt == "%d.%m":
                    d = parsed.replace(year=today.year).date()
                else:
                    d = parsed.date()
                break
            except ValueError:
                pass
        if d is None:
            raise ValueError("Ungültiges Datum")

    return d.strftime("%d.%m.%Y")



def ensure_utc_datetime(dt: datetime) -> datetime:
    """Stellt sicher, dass dt timezone-aware ist (UTC)."""
    if not isinstance(dt, datetime):
        raise ValueError("dt ist kein datetime")
    if dt.tzinfo is None:
        return pytz.utc.localize(dt)
    return dt.astimezone(pytz.utc)



def to_google_dates(start_utc: datetime, duration_hours: int = 2) -> str:
    end_utc = start_utc + timedelta(hours=duration_hours)
    fmt = "%Y%m%dT%H%M%SZ"
    return f"{start_utc.strftime(fmt)}/{end_utc.strftime(fmt)}"


def build_google_calendar_url(title: str, start_utc: datetime, location: str, description: str) -> str:
    base = "https://calendar.google.com/calendar/render?action=TEMPLATE"
    text = "&text=" + quote_plus(title or "")
    dates = "&dates=" + to_google_dates(start_utc)
    loc = "&location=" + quote_plus(location or "")
    details = "&details=" + quote_plus(description or "")
    return base + text + dates + loc + details


def build_ics_content(title: str, start_utc: datetime, duration_hours: int, location: str, description: str):
    dt_start = start_utc.strftime("%Y%m%dT%H%M%SZ")
    dt_end = (start_utc + timedelta(hours=duration_hours)).strftime("%Y%m%dT%H%M%SZ")
    uid = f"{title}-{dt_start}@slotbot"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//SlotBot//v4.6//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{dt_start}",
        f"DTEND:{dt_end}",
        f"SUMMARY:{title}",
        f"LOCATION:{location or ''}",
        "DESCRIPTION:" + (description or "").replace("\n", "\\n"),
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines)


# ----------------- Slots / Emojis -----------------
def normalize_emoji(emoji):
    if isinstance(emoji, str):
        return emoji.strip()
    if hasattr(emoji, "id") and emoji.id:
        return f"<:{emoji.name}:{emoji.id}>"
    return emoji.name


def is_valid_emoji(emoji, guild: discord.Guild):
    if re.match(CUSTOM_EMOJI_REGEX, emoji):
        return any(str(e) == emoji for e in guild.emojis)
    return True


SLOT_PATTERN = re.compile(r"(<a?:\w+:\d+>|[^\s:]+)\s*:\s*(\d+)")


def parse_slots(slots_str: str, guild: discord.Guild):
    matches = SLOT_PATTERN.findall(slots_str or "")
    if not matches:
        return None
    slot_dict: Dict[str, Dict[str, Any]] = {}
    for emoji, limit in matches:
        em = normalize_emoji(emoji)
        if not is_valid_emoji(em, guild):
            return f"Ungültiges Emoji: {em}"
        slot_dict[em] = {
            "limit": int(limit),
            "main": set(),
            "waitlist": [],
            "reminded": set(),      # DM 20 Min vorher
            "afk_dm_sent": set(),   # AFK-Check DM 10 Min vorher
        }
    return slot_dict


def format_event_text(ev: dict, guild: discord.Guild) -> str:
    text = "🎟️ **Slots & Teilnehmer:**\n"
    if not ev["slots"]:
        return text + "\n(Keine Slots definiert.)"

async def afk_enforcer_task():
    """AFK-Check im Thread: startet 30 Min vorher, läuft 20 Min, fragt alle 5 Min nach.
    Bestätigung per ✅ auf der AFK-Message. Nach Ablauf: Slots von Nicht-Bestätigten werden freigegeben.
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(pytz.utc)

        for msg_id, ev in list(active_events.items()):
            guild = bot.get_guild(ev.get("guild_id"))
            if not guild:
                continue
            event_time = ev.get("event_time")
            if not event_time:
                continue
            try:
                event_time = ensure_utc_datetime(event_time)
            except Exception:
                continue

            if ev.get("afk_enabled", True) is False:
                continue

            seconds_left = (event_time - now).total_seconds()
            if seconds_left < 0:
                continue

            window_start = event_time - timedelta(minutes=30)
            window_end = window_start + timedelta(minutes=20)
            interval = timedelta(minutes=5)

            ev.setdefault("afk_state", {})
            st = ev["afk_state"]
            st.setdefault("confirmed", [])
            st.setdefault("prompt_ids", [])
            st.setdefault("started", False)
            st.setdefault("finished", False)
            st.setdefault("last_prompt_at", None)

            if st["finished"]:
                continue
            if now < window_start:
                continue

            try:
                thread = await get_or_restore_thread(ev, guild, int(msg_id))
            except Exception:
                thread = None
            if not thread:
                thread = guild.get_channel(ev.get("channel_id"))

            confirmed = set(int(x) for x in st.get("confirmed", []))
            participants = set()
            for slot in ev.get("slots", {}).values():
                mains = slot.get("main", set())
                if not isinstance(mains, set):
                    mains = set(mains)
                participants |= set(mains)
            unanswered = participants - confirmed

            if not st["started"]:
                st["started"] = True
                st["last_prompt_at"] = None

            if now >= window_end:
                removed = set()
                for emoji, slot in ev.get("slots", {}).items():
                    mains = slot.get("main", set())
                    if not isinstance(mains, set):
                        mains = set(mains)
                    wl = slot.get("waitlist", [])
                    for uid in list(mains):
                        if uid in unanswered:
                            mains.remove(uid)
                            removed.add(uid)
                    while len(mains) < int(slot.get("limit", 0)) and wl:
                        nxt = wl.pop(0)
                        mains.add(nxt)
                    slot["main"] = mains
                    slot["waitlist"] = wl

                st["finished"] = True
                st["confirmed"] = list(confirmed)

                try:
                    await update_event_message(int(msg_id))
                except Exception:
                    pass
                try:
                    await safe_save()
                except Exception:
                    pass

                if thread:
                    try:
                        if removed:
                            await thread.send(f"🚪 AFK-Check vorbei: **{len(removed)}** Slot(s) wurden automatisch freigegeben.")
                        else:
                            await thread.send("✅ AFK-Check vorbei: alle bestätigt.")
                    except Exception:
                        pass
                continue

            # prompt scheduling
            last_prompt_at = st.get("last_prompt_at")
            last_dt = None
            if isinstance(last_prompt_at, str):
                try:
                    last_dt = datetime.fromisoformat(last_prompt_at)
                except Exception:
                    last_dt = None

            if unanswered and (last_dt is None or now - last_dt >= interval):
                mentions = " ".join(f"<@{uid}>" for uid in list(unanswered)[:30])
                text = "🕵️ **AFK-Check:** Bitte mit ✅ reagieren, wenn du dabei bist."
                if mentions:
                    text += "\n" + mentions
                try:
                    m = await thread.send(text)
                    try:
                        await m.add_reaction("✅")
                    except Exception:
                        pass
                    st["prompt_ids"].append(int(m.id))
                    st["last_prompt_at"] = now.isoformat()
                    st["confirmed"] = list(confirmed)
                    await safe_save()
                except Exception as e:
                    print(f"⚠️ AFK-Thread Post fehlgeschlagen: {e}")

        await asyncio.sleep(10)


async def cleanup_task():
    """Löscht abgelaufene Events nach delete_at."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(pytz.utc)
        removed_any = False

        for msg_id, ev in list(active_events.items()):
            delete_at = ev.get("delete_at")
            if not delete_at:
                continue
            try:
                delete_at = ensure_utc_datetime(delete_at)
            except Exception:
                continue

            if now < delete_at:
                continue

            guild = bot.get_guild(ev.get("guild_id"))
            if not guild:
                # trotzdem entfernen, sonst bleibt Müll liegen
                active_events.pop(int(msg_id), None)
                removed_any = True
                continue

            # Message löschen
            try:
                channel = guild.get_channel(ev.get("channel_id")) or await guild.fetch_channel(ev.get("channel_id"))
                try:
                    msg = await channel.fetch_message(int(msg_id))
                    await msg.delete()
                except Exception:
                    pass
            except Exception:
                pass

            # Thread löschen, falls vorhanden
            try:
                thread_id = ev.get("thread_id")
                if thread_id:
                    th = guild.get_thread(int(thread_id))
                    if not th:
                        th = await bot.fetch_channel(int(thread_id))
                    if th:
                        await th.delete()
            except Exception:
                pass

            active_events.pop(int(msg_id), None)
            removed_any = True

        if removed_any:
            try:
                save_events()
            except Exception as e:
                print(f"⚠️ save_events (cleanup) fehlgeschlagen: {e}")

        await asyncio.sleep(60)


async def watchdog_task():
    """Restartet Background-Tasks falls sie abstürzen (ohne auf on_ready zu warten)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for key, factory in [
                ("reminder", reminder_task),
                ("afk_enforcer", afk_enforcer_task),
                ("cleanup", cleanup_task),
            ]:
                task = BACKGROUND_TASKS.get(key)
                if not task or task.done() or task.cancelled():
                    BACKGROUND_TASKS[key] = bot.loop.create_task(factory(), name=f"slotbot_{key}_task")
        except Exception as e:
            print(f"⚠️ watchdog_task Fehler: {e}")
        await asyncio.sleep(30)

# ----------------- Thread Helper & Logging -----------------
async def get_or_restore_thread(ev: dict, guild: discord.Guild, base_message_id: int):
    thread = None
    thread_id = ev.get("thread_id")
    if thread_id:
        try:
            thread = guild.get_channel(thread_id) or await guild.fetch_channel(thread_id)
        except Exception:
            thread = None
    if thread and getattr(thread, "archived", False):
        try:
            await thread.edit(archived=False)
        except Exception:
            pass
    if thread is None:
        channel = guild.get_channel(ev["channel_id"])
        if channel is None:
            return None
        try:
            base_msg = await channel.fetch_message(base_message_id)
            thread = await base_msg.create_thread(
                name=f"Event-Log (neu): {ev['title']}",
                auto_archive_duration=1440,
            )
            ev["thread_id"] = thread.id
            await safe_save()
        except Exception as e:
            print(f"⚠️ Konnte keinen Thread erstellen: {e}")
            return None
    return thread


async def post_event_update_log(ev: dict, guild: discord.Guild, editor_mention: str, changes_text: str, base_message_id: int):
    thread = await get_or_restore_thread(ev, guild, base_message_id)
    if not thread:
        return
    try:
        await thread.send(f"✏️ **{editor_mention}** hat das Event bearbeitet ({changes_text}).")
    except Exception:
        pass


async def post_calendar_links(ev: dict, guild: discord.Guild, base_message_id: int):
    thread = await get_or_restore_thread(ev, guild, base_message_id)
    if not thread:
        return

    title = ev["title"]
    event_time_utc = ev["event_time"]
    header = ev["header"]

    m_ort = re.search(r"^📍 \*\*Ort:\*\* (.+)$", header, re.M)
    m_level = re.search(r"^⚔️ \*\*Levelbereich:\*\* (.+)$", header, re.M)
    m_stil = re.search(r"^💬 \*\*Stil:\*\* (.+)$", header, re.M)
    m_typ = re.search(r"^🏷️ \*\*Typ:\*\* (.+)$", header, re.M)
    m_lead = re.search(r"^👑 \*\*Gruppenlead:\*\* (.+)$", header, re.M)
    m_note = re.search(r"^📝 \*\*Anmerkung:\*\* (.+)$", header, re.M)

    ort = m_ort.group(1) if m_ort else ""
    treff = m_treff.group(1) if m_treff else ""
    location = treff or ort
    description_parts: List[str] = []
    if m_level:
        description_parts.append(f"Level: {m_level.group(1)}")
    if m_stil:
        description_parts.append(f"Stil: {m_stil.group(1)}")
    if m_typ:
        description_parts.append(f"Typ: {m_typ.group(1)}")
    if m_lead:
        description_parts.append(f"Gruppenlead: {m_lead.group(1)}")
    if m_note:
        description_parts.append(f"Anmerkung: {m_note.group(1)}")
    desc_text = "\n".join(description_parts)

    g_link = build_google_calendar_url(title, event_time_utc, location, desc_text)
    ics_text = build_ics_content(title, event_time_utc, 2, location, desc_text)

    if PUBLIC_BASE_URL:
        url = f"{PUBLIC_BASE_URL.rstrip('/')}/ics/{base_message_id}.ics"
        await thread.send(f"📅 Kalender: [Google öffnen]({g_link})  |  [Apple (.ics)]({url})")
    else:
        try:
            fp = io.BytesIO(ics_text.encode("utf-8"))
            file = discord.File(fp, filename=f"event_{base_message_id}.ics")
            await thread.send(
                content=f"📅 Kalender: [Google öffnen]({g_link})  |  Apple: .ics angehängt",
                file=file,
            )
        except Exception:
            await thread.send(f"📅 Kalender: [Google öffnen]({g_link})")


async def log_participation_change(
    ev: dict,
    guild: discord.Guild,
    base_message_id: int,
    user_id: int,
    emoji: str,
    action: str,
    slot_type: str = "",
):
    thread = await get_or_restore_thread(ev, guild, base_message_id)
    if not thread:
        return

    member = guild.get_member(user_id)
    user_mention = member.mention if member else f"<@{user_id}>"

    if action == "join":
        if slot_type:
            text = f"✅ {user_mention} hat sich mit {emoji} angemeldet ({slot_type})."
        else:
            text = f"✅ {user_mention} hat sich mit {emoji} angemeldet."
    elif action == "leave":
        if slot_type:
            text = f"❌ {user_mention} hat sich abgemeldet ({slot_type})."
        else:
            text = f"❌ {user_mention} hat sich abgemeldet."
    else:
        return

    try:
        await thread.send(text)
    except Exception:
        pass


# ----------------- /help -----------------
@bot.tree.command(name="help", description="Zeigt eine ausführliche Erklärung aller Befehle an")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 SlotBot v4.6 – Hilfe",
        description=(
            "Der SlotBot hilft dir, Events mit Slots zu erstellen und zu verwalten.\n"
            "Hier ein Überblick über die Befehle."
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="🆕 /event",
        value=(
            "**Erstellt ein neues Event mit Slots & Thread.**\n"
            "Pflicht: `art`, `zweck`, `ort`, `datum`, `zeit`, `level`, `stil`, `slots`\n"
            "Optional: `typ`, `gruppenlead`, `anmerkung`, `auto_delete_stunden` (Default 1h)\n"
            "Beispiel:\n"
            '`/event art:PvE zweck:"XP Farmen" ort:"Calpheon" datum:heute zeit:20:00`\noder: `/event ... datum:morgen zeit:21`\noder klassisch: `/event ... datum:27.10.2025 zeit:20:00`\n'
            '`level:61+ stil:"Organisiert" slots:"⚔️:3 🛡️:1 💉:2" auto_delete_stunden:3`\n'
            "• 20-Minuten-Reminder per DM\n"
            "• 10-Minuten-AFK-Check per DM (Auto-Kick bei Nicht-Reaktion)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎲 /roll & /start_roll",
        value=(
            "`/start_roll dauer:60` – Startet eine Roll-Runde im Channel.\n"
            "`/roll` – Würfelt 1–100 (Embed).\n"
            "Pro Spieler zählt nur der **erste** Wurf in der Runde."
        ),
        inline=False,
    )

    embed.add_field(
        name="✏️ /event_edit",
        value=(
            "Bearbeitet **dein** aktuelles Event (Datum, Zeit, Ort, Level, Anmerkung, Slots).\n"
            "Zeit-Eingaben wie `22`, `22.15`, `22:15` oder `22 Uhr` sind erlaubt.\n"
            "Datum/Zeit werden im Event mit `~~alt~~ → neu` markiert."
        ),
        inline=False,
    )
    embed.add_field(
        name="🗑️ /event_delete",
        value="Löscht dein aktuelles Event (nur Ersteller).",
        inline=False,
    )
    embed.add_field(
        name="🗓️ /events",
        value="Listet alle aktiven Events auf dem Server (Serverweit).",
        inline=False,
    )
    embed.add_field(
        name="ℹ️ /event_info",
        value="Zeigt Details & Slots zu deinem aktuellen Event als Embed.",
        inline=False,
    )
    embed.add_field(
        name="📩 /subscribe & /unsubscribe",
        value=(
            "Verwalte Benachrichtigungen für neue Events.\n"
            "`/subscribe art:PvE` – DM bei neuen PvE-Events\n"
            "`/subscribe art:PVX` – DM bei neuen PVX-Events\n"
            "`/unsubscribe art:PvE` – PvE-DMs wieder abbestellen\n"
            "`/subscribe art:Alle` – Alle Arten abonnieren"
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 /stats",
        value="Zeigt Event-Statistiken für diesen Server (Anzahl Events, Zeiten, Teilnahme-Trends).",
        inline=False,
    )
    embed.add_field(
        name="🧪 /test",
        value="Führt einen Selbsttest (GitHub, Persistenz, Rechte, Posting) aus. Nur vom Bot-Owner nutzbar.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ----------------- /roll -----------------
@bot.tree.command(name="roll", description="Würfelt eine Zahl zwischen 1 und 100")
async def roll_command(interaction: discord.Interaction):
    rolled_value = random.randint(1, 100)

    embed = discord.Embed(
        title="🎲 Wurf",
        description=f"{interaction.user.mention} würfelt eine Zahl zwischen **1** und **100**.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Ergebnis", value=f"🎯 **{rolled_value}**", inline=False)

    session_info = None
    already_rolled = False
    counted_value = None

    if interaction.guild is not None:
        key = (interaction.guild.id, interaction.channel.id)
        now = datetime.now(pytz.utc)
        session = ROLL_SESSIONS.get(key)
        if session and session["end_time"] >= now:
            session_info = session
            if interaction.user.id not in session["rolls"]:
                session["rolls"][interaction.user.id] = rolled_value  # erster Wurf zählt
                counted_value = rolled_value
            else:
                already_rolled = True
                counted_value = session["rolls"][interaction.user.id]

    if session_info:
        rest = int((session_info["end_time"] - datetime.now(pytz.utc)).total_seconds())
        if rest < 0:
            rest = 0

        if already_rolled:
            embed.add_field(
                name="⚠️ Hinweis",
                value=(
                    f"Du hast in dieser Runde schon gewürfelt.\n"
                    f"Gezählt wird **nur dein erster Wurf**: **{counted_value}**\n"
                    f"➡️ Dieser neue Wurf zählt **nicht**."
                ),
                inline=False,
            )
            embed.set_footer(text=f"Roll-Runde aktiv – noch ca. {rest} Sekunden.")
        else:
            embed.set_footer(text=f"Roll-Runde aktiv – nur dein erster Wurf zählt. Noch ca. {rest} Sekunden.")
    else:
        embed.set_footer(text="Keine Roll-Runde aktiv. Starte eine mit /start_roll.")

    await interaction.response.send_message(embed=embed, ephemeral=False)


# ----------------- /start_roll -----------------
async def event_edit_event_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete: zeigt Events auf dem Server – für Ersteller oder Admins."""
    if interaction.guild is None:
        return []

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"❌ Slash-Command-Fehler: {error}")
    try:
        msg = "❌ Bei diesem Befehl ist ein Fehler aufgetreten. Bitte probiere es später erneut."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass
