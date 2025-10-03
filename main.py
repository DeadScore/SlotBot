# Slash Command /event mit Steckbrief
@bot.tree.command(name="event", description="Erstellt ein Event mit Steckbrief und begrenzten Slots.")
@app_commands.describe(
    art="Art des Events (z.B. PvP/PvE/RP)",
    zweck="Zweck (z.B. EP Farmen)",
    ort="Ort (z.B. Higewayman Hills)",
    zeit="Zeit (z.B. heute 19 Uhr)",
    level="Levelbereich (z.B. 5 - 10)",
    typ="Gruppe oder Raid",
    stil="Gemütlich oder Organisiert",
    slots="Slot-Definitionen im Format emoji:limit (z.B. 😀:5 🐱:3)"
)
async def event(interaction: discord.Interaction, 
                art: str, 
                zweck: str, 
                ort: str, 
                zeit: str, 
                level: str, 
                typ: str, 
                stil: str,
                slots: str):
    print(f"📨 /event Command von {interaction.user}")

    slot_parts = slots.split()
    slot_dict = {}
    description = "📋 **Event-Teilnehmerübersicht** 📋\n"

    # Slots parsen
    for part in slot_parts:
        if ":" not in part:
            continue
        try:
            emoji_raw, limit = part.rsplit(":", 1)
            emoji = normalize_emoji(emoji_raw)
            limit = int(limit)
        except ValueError:
            await interaction.response.send_message(f"❌ Ungültiges Slot-Format: {part}", ephemeral=True)
            return

        if not is_valid_emoji(emoji_raw, interaction.guild):
            await interaction.response.send_message(f"❌ Ungültiges Emoji: {emoji_raw}", ephemeral=True)
            return

        slot_dict[emoji] = {"limit": limit, "main": set(), "waitlist": []}
        description += f"{emoji} (0/{limit}): -\n"

    # Steckbrief generieren
    header = (
        f"‼️ **Neue Gruppensuche!** ‼️\n\n"
        f"**Art:** {art}\n"
        f"**Zweck:** {zweck}\n"
        f"**Ort:** {ort}\n"
        f"**Zeit:** {zeit}\n"
        f"**Levelbereich:** {level}\n"
        f"**Typ:** {typ}\n"
        f"**Stil:** {stil}\n\n"
        f"Reagiert mit eurer Klasse:\n"
        "<:Tank_Archetype:1345126746340458527> "
        "<:Fighter_Archetype:1345126842637484083> "
        "<:Rogue_Archetype:1345126778078892074> "
        "<:Ranger_Archetype:1345126793257943050> "
        "<:Mage_Archetype:1345126856348668036> "
        "<:Summoner_Archetype:1345126764447269016> "
        "<:Bard_Archetype:1345126811880914944> "
        "<:Cleric_Archetype:1345126828280512683>\n\n"
    )

    await interaction.response.send_message("✅ Event wurde erstellt!", ephemeral=True)
    msg = await interaction.channel.send(header + description)

    # Reaktionen hinzufügen
    for emoji in slot_dict.keys():
        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            await interaction.followup.send(f"❌ Fehler beim Hinzufügen von {emoji}")
            return

    active_events[msg.id] = {"slots": slot_dict, "channel_id": interaction.channel.id, "guild_id": interaction.guild.id}
