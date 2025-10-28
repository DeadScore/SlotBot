# main.py — SlotBot (vollständige Version mit /help, Edit-Fix, Thread-Log-Fix)
import os, re, json, asyncio, base64, requests, pytz
from datetime import datetime
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands
from discord import app_commands

# ================= CONFIG =================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("❌ DISCORD_TOKEN fehlt!")
    raise SystemExit(1)

CUSTOM_EMOJI_REGEX = r"<a?:\\w+:\\d+>"
BERLIN_TZ = pytz.timezone("Europe/Berlin")
bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
active_events = {}

# ================= GITHUB STORAGE =================
def load_events():
    repo, path, token = os.getenv("GITHUB_REPO"), os.getenv("GITHUB_FILE_PATH", "data/events.json"), os.getenv("GITHUB_TOKEN")
    if not all([repo, path, token]): return {}
    try:
        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        r = requests.get(url, headers={"Authorization": f"token {token}"})
        if r.status_code == 200:
            data = json.loads(base64.b64decode(r.json()["content"]))
            for ev in data.values():
                for s in ev["slots"].values():
                    s["main"] = set(s.get("main", [])); s["waitlist"] = list(s.get("waitlist", [])); s["reminded"] = set(s.get("reminded", []))
            print("✅ Events geladen."); return {int(k): v for k, v in data.items()}
        else: print(f"⚠️ Fehler GitHub {r.status_code}")
    except Exception as e: print("❌ Fehler beim Laden:", e)
    return {}

def save_events():
    repo, path, token = os.getenv("GITHUB_REPO"), os.getenv("GITHUB_FILE_PATH", "data/events.json"), os.getenv("GITHUB_TOKEN")
    if not all([repo, path, token]): return
    try:
        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        old = requests.get(url, headers={"Authorization": f"token {token}"})
        sha = old.json().get("sha") if old.status_code == 200 else None
        serializable = {str(mid): {**ev, "slots": {e: {**s, "main": list(s["main"]), "reminded": list(s["reminded"])} for e, s in ev["slots"].items()}} for mid, ev in active_events.items()}
        encoded = base64.b64encode(json.dumps(serializable, indent=4).encode()).decode()
        requests.put(url, headers={"Authorization": f"token {token}"}, json={"message": "Update events.json via SlotBot", "content": encoded, "sha": sha})
        print("💾 Events gespeichert.")
    except Exception as e: print("❌ Fehler beim Speichern:", e)

# ================= HELPERS =================
WEEKDAY_DE = {"Monday": "Montag","Tuesday": "Dienstag","Wednesday": "Mittwoch","Thursday": "Donnerstag","Friday": "Freitag","Saturday": "Samstag","Sunday": "Sonntag"}
def format_dt(local_dt): en = local_dt.strftime("%A"); return local_dt.strftime(f"%A, %d.%m.%Y %H:%M %Z").replace(en, WEEKDAY_DE.get(en, en))
def normalize_emoji(emoji): return emoji.strip() if isinstance(emoji, str) else (f"<:{emoji.name}:{emoji.id}>" if getattr(emoji, "id", None) else emoji.name)
def is_valid_emoji(emoji, guild): return any(str(e) == emoji for e in guild.emojis) if re.match(CUSTOM_EMOJI_REGEX, emoji) else True
def update_struck_line(header, label, new_value):
    lines, new_lines, updated = header.splitlines(), [], False
    for line in lines:
        if line.startswith(label):
            old_val = re.sub(r"~~(.*?)~~\\s*→\\s*(.*)", r"\\2", line.replace(label, "").strip())
            new_lines.append(f"{label} ~~{old_val}~~ → {new_value}"); updated = True
        else: new_lines.append(line)
    if not updated: new_lines.append(f"{label} ~~?~~ → {new_value}")
    return "\\n".join(new_lines)

def format_event_text(event, guild):
    txt = "**📋 Eventübersicht:**\\n"
    for emoji, slot in event["slots"].items():
        main = [guild.get_member(uid).mention for uid in slot["main"] if guild.get_member(uid)]
        wait = [guild.get_member(uid).mention for uid in slot["waitlist"] if guild.get_member(uid)]
        txt += f"\\n{emoji} ({len(main)}/{slot['limit']}): " + (", ".join(main) if main else "-")
        if wait: txt += f"\\n   ⏳ Warteliste: " + ", ".join(wait)
    return txt

# ================= REMINDER =================
async def reminder_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(pytz.utc)
        for mid, ev in list(active_events.items()):
            guild = bot.get_guild(ev["guild_id"]); 
            if not guild or not ev.get("event_time"): continue
            for slot in ev["slots"].values():
                for uid in slot["main"]:
                    if uid in slot.get("reminded", set()): continue
                    if 0 <= (ev["event_time"] - now).total_seconds() <= 600:
                        try: m = guild.get_member(uid) or await guild.fetch_member(uid); await m.send(f"⏰ Dein Event **{ev['title']}** startet in 10 Minuten!"); slot["reminded"].add(uid)
                        except: pass
        await asyncio.sleep(60)

# ================= EVENTS =================
@bot.event
async def on_ready():
    global active_events; print(f"✅ SlotBot online als {bot.user}"); active_events = load_events()
    bot.loop.create_task(reminder_task())
    try: await bot.tree.sync(); print("📂 Slash Commands synchronisiert")
    except Exception as e: print("❌ Sync-Fehler:", e)

# ================= /event =================
@bot.tree.command(name="event", description="Erstellt ein Event mit Slots & Thread")
@app_commands.describe(art="Art (PvE/PvP/PVX)", zweck="Zweck", ort="Ort", zeit="HH:MM", datum="DD.MM.YYYY", level="Level", stil="Gemütlich/Organisiert", slots="Slots (⚔️:2 🛡️:1)")
@app_commands.choices(art=[app_commands.Choice(name=x,value=x) for x in["PvE","PvP","PVX"]],stil=[app_commands.Choice(name=x,value=x) for x in["Gemütlich","Organisiert"]])
async def event(inter: discord.Interaction, art: app_commands.Choice[str], zweck: str, ort: str, zeit: str, datum: str, level: str, stil: app_commands.Choice[str], slots: str):
    try: local_dt = BERLIN_TZ.localize(datetime.strptime(f"{datum} {zeit}", "%d.%m.%Y %H:%M")); utc_dt = local_dt.astimezone(pytz.utc)
    except: return await inter.response.send_message("❌ Ungültiges Datum/Zeit.", ephemeral=True)
    slot_dict = {}; 
    for m in re.findall(r"(<a?:\\w+:\\d+>|[^\\s:]+)\\s*:\\s*(\\d+)", slots or ""):
        emoji, lim = normalize_emoji(m[0]), int(m[1]); 
        if not is_valid_emoji(emoji, inter.guild): return await inter.response.send_message(f"❌ Ungültiges Emoji: {emoji}", ephemeral=True)
        slot_dict[emoji] = {"limit": lim,"main": set(),"waitlist": [],"reminded": set()}
    header = f"📣 **@here — Neue Gruppensuche!**\\n\\n🗡️ **Art:** {art.value}\\n🎯 **Zweck:** {zweck}\\n📍 **Ort:** {ort}\\n🕒 **Datum/Zeit:** {format_dt(local_dt)}\\n⚔️ **Levelbereich:** {level}\\n💬 **Stil:** {stil.value}\\n"
    msg = await inter.channel.send(header + "\\n\\n" + format_event_text({"slots": slot_dict}, inter.guild))
    for e in slot_dict: 
        try: await msg.add_reaction(e)
        except: pass
    thread = await msg.create_thread(name=f"Event-Log: {zweck}", auto_archive_duration=1440); await thread.send(f"🧵 Event-Log für: {zweck} — {msg.jump_url}")
    active_events[msg.id] = {"title": zweck,"slots": slot_dict,"channel_id": inter.channel.id,"guild_id": inter.guild.id,"header": header,"creator_id": inter.user.id,"event_time": utc_dt,"thread_id": thread.id}
    save_events(); await inter.response.send_message("✅ Event erstellt!", ephemeral=True)

# ================= /event_edit =================
@bot.tree.command(name="event_edit", description="Bearbeite dein Event (Ort, Datum, Zeit, Level)")
@app_commands.describe(ort="Neuer Ort", datum="DD.MM.YYYY", zeit="HH:MM", level="Neues Level")
async def event_edit(inter: discord.Interaction, ort: str=None, datum: str=None, zeit: str=None, level: str=None):
    own = [(mid, ev) for mid, ev in active_events.items() if ev["creator_id"] == inter.user.id and ev["channel_id"] == inter.channel.id]
    if not own: return await inter.response.send_message("❌ Kein eigenes Event gefunden.", ephemeral=True)
    mid, ev = max(own,key=lambda x:x[0]); updated=False; changes=[]
    if ort: ev["header"]=update_struck_line(ev["header"],"📍 **Ort:**",ort); changes.append(f"Ort: {ort}"); updated=True
    if datum or zeit:
        old = ev["event_time"].astimezone(BERLIN_TZ)
        try: new = BERLIN_TZ.localize(datetime.strptime(f"{datum or old.strftime('%d.%m.%Y')} {zeit or old.strftime('%H:%M')}", "%d.%m.%Y %H:%M"))
        except: return await inter.response.send_message("❌ Falsches Datum/Zeit-Format.", ephemeral=True)
        ev["header"]=update_struck_line(ev["header"],"🕒 **Datum/Zeit:**",format_dt(new)); ev["event_time"]=new.astimezone(pytz.utc); changes.append("Datum/Zeit geändert"); updated=True
    if level: ev["header"]=update_struck_line(ev["header"],"⚔️ **Levelbereich:**",level); changes.append(f"Level: {level}"); updated=True
    if not updated: return await inter.response.send_message("ℹ️ Keine Änderungen angegeben.", ephemeral=True)
    g, c, m = inter.guild, inter.guild.get_channel(ev["channel_id"]), await inter.guild.get_channel(ev["channel_id"]).fetch_message(mid)
    await m.edit(content=ev["header"]+"\\n\\n"+format_event_text(ev,g)); save_events(); await inter.response.send_message("✅ Event aktualisiert.", ephemeral=True)
    t = g.get_channel(ev.get("thread_id")); 
    if t: 
        try: 
            if getattr(t,"archived",False): await t.edit(archived=False)
            await t.send(f"✏️ {inter.user.mention} hat das Event bearbeitet ({', '.join(changes)}).")
        except Exception as e: print("⚠️ Thread-Fehler:", e)

# ================= /event_delete =================
@bot.tree.command(name="event_delete", description="Löscht dein Event")
async def event_delete(inter: discord.Interaction):
    own = [(mid,ev) for mid,ev in active_events.items() if ev["creator_id"]==inter.user.id and ev["channel_id"]==inter.channel.id]
    if not own: return await inter.response.send_message("❌ Kein eigenes Event gefunden.", ephemeral=True)
    mid, ev = max(own,key=lambda x:x[0]); c=inter.channel; msg=await c.fetch_message(mid); await msg.delete()
    if (t:=inter.guild.get_channel(ev.get("thread_id"))): await t.delete()
    del active_events[mid]; save_events(); await inter.response.send_message("✅ Event gelöscht.", ephemeral=True)

# ================= /help =================
@bot.tree.command(name="help", description="Zeigt alle Befehle an")
async def help_cmd(inter: discord.Interaction):
    txt = ("**📖 SlotBot Befehle:**\\n"
           "• `/event` – Erstelle ein neues Event.\\n"
           "• `/event_edit` – Bearbeite dein bestehendes Event.\\n"
           "• `/event_delete` – Lösche dein Event.\\n"
           "• Änderungen werden im Thread-Log dokumentiert.\\n"
           "• Reminder 10 Min. vor Start per DM.")
    await inter.response.send_message(txt, ephemeral=True)

# ================= Flask =================
flask_app = Flask("bot_flask")
@flask_app.route("/") 
def index(): return "✅ SlotBot läuft."
def run_bot(): asyncio.run(bot.start(TOKEN))
if __name__=="__main__": print("🚀 Starte SlotBot ..."); Thread(target=run_bot,daemon=True).start(); flask_app.run(host="0.0.0.0",port=int(os.getenv("PORT",5000)))
