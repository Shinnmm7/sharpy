import time

import discord
from discord import app_commands
from discord.ext import commands

CATEGORY_CHOICES = [app_commands.Choice(name="Roblox game", value="game"),
                    app_commands.Choice(name="Discord server", value="server")]
CATEGORY_LABEL = {"game": "Roblox game", "server": "Discord server"}
STATUS_COLOR = {"pending": discord.Color.blurple(), "approved": discord.Color.green(), "denied": discord.Color.red()}

SEVERITY_CHOICES = [app_commands.Choice(name="Low", value="Low"), app_commands.Choice(name="Medium", value="Medium"),
                    app_commands.Choice(name="High", value="High"),
                    app_commands.Choice(name="Critical (actively being abused)", value="Critical")]
SEVERITY_COLOR = {"Low": discord.Color.light_grey(), "Medium": discord.Color.gold(), "High": discord.Color.orange(),
                  "Critical": discord.Color.red()}


class Suggestions(commands.Cog):
    """/suggestion: members suggest things for the Roblox game or the Discord server; staff set the channels."""

    suggestion = app_commands.Group(name="suggestion", description="Suggest something for the game or the server",
                                    guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS suggestion_config (guild_id INTEGER, category TEXT, "
                         "channel_id INTEGER, PRIMARY KEY (guild_id, category))")
        await db.execute("CREATE TABLE IF NOT EXISTS suggestions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "guild_id INTEGER, category TEXT, user_id INTEGER, text TEXT, channel_id INTEGER, "
                         "message_id INTEGER, status TEXT DEFAULT 'pending', created_at INTEGER)")
        await db.commit()

    async def channel_for(self, guild_id: int, category: str) -> int | None:
        async with self.bot.db.execute("SELECT channel_id FROM suggestion_config WHERE guild_id=? AND category=?",
                                       (guild_id, category)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    @suggestion.command(name="setup", description="Choose which channel each kind of suggestion is posted in")
    @app_commands.describe(game_channel="Where Roblox game suggestions are posted",
                           server_channel="Where Discord server suggestions are posted")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(embed_links=True, add_reactions=True)
    async def setup_cmd(self, interaction: discord.Interaction, game_channel: discord.TextChannel | None = None,
                        server_channel: discord.TextChannel | None = None):
        if game_channel is None and server_channel is None:
            return await interaction.response.send_message("Pick at least one channel to set.", ephemeral=True)
        me = interaction.guild.me
        for channel in (game_channel, server_channel):
            if channel and not (channel.permissions_for(me).send_messages and channel.permissions_for(me).embed_links
                                and channel.permissions_for(me).add_reactions):
                return await interaction.response.send_message(
                    f"I need Send Messages, Embed Links and Add Reactions in {channel.mention}.", ephemeral=True)
        rows = [(interaction.guild_id, cat, ch.id) for cat, ch in (("game", game_channel), ("server", server_channel))
               if ch is not None]
        await self.bot.db.executemany(
            "INSERT INTO suggestion_config (guild_id, category, channel_id) VALUES (?,?,?) "
            "ON CONFLICT(guild_id, category) DO UPDATE SET channel_id=excluded.channel_id", rows)
        await self.bot.db.commit()
        parts = [f"Roblox game suggestions -> {game_channel.mention}" for _ in [0] if game_channel]
        parts += [f"Discord server suggestions -> {server_channel.mention}" for _ in [0] if server_channel]
        await interaction.response.send_message("Saved.\n" + "\n".join(parts), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @suggestion.command(name="submit", description="Suggest something for the Roblox game or the Discord server")
    @app_commands.describe(category="What the suggestion is for",
                           suggestion="Your suggestion (type \\n for a new line)")
    @app_commands.choices(category=CATEGORY_CHOICES)
    async def submit(self, interaction: discord.Interaction, category: app_commands.Choice[str],
                     suggestion: app_commands.Range[str, 5, 1500]):
        channel_id = await self.channel_for(interaction.guild_id, category.value)
        if not channel_id or not interaction.guild.get_channel(channel_id):
            return await interaction.response.send_message(
                "Suggestions for that aren't set up yet. Ask staff to run `/suggestion setup` first.", ephemeral=True)
        channel = interaction.guild.get_channel(channel_id)
        text = suggestion.replace("\\n", "\n")
        embed = discord.Embed(title=f"{category.name} suggestion", description=text, color=STATUS_COLOR["pending"],
                              timestamp=discord.utils.utcnow())
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="Pending")
        try:
            message = await channel.send(embed=embed)
            await message.add_reaction("\N{THUMBS UP SIGN}")
            await message.add_reaction("\N{THUMBS DOWN SIGN}")
        except discord.HTTPException:
            return await interaction.response.send_message("I couldn't post that. Ask staff to check my permissions "
                                                           f"in {channel.mention}.", ephemeral=True)
        cur = await self.bot.db.execute(
            "INSERT INTO suggestions (guild_id, category, user_id, text, channel_id, message_id, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (interaction.guild_id, category.value, interaction.user.id, text, channel.id, message.id,
             int(time.time())))
        await self.bot.db.commit()
        embed.set_footer(text=f"Pending • #{cur.lastrowid}")
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass
        await interaction.response.send_message(f"Suggestion #{cur.lastrowid} posted in {channel.mention}. Thanks!",
                                                ephemeral=True)

    async def resolve(self, interaction: discord.Interaction, suggestion_id: int, status: str, reason: str | None):
        async with self.bot.db.execute(
                "SELECT category, user_id, text, channel_id, message_id FROM suggestions "
                "WHERE id=? AND guild_id=?", (suggestion_id, interaction.guild_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I can't find a suggestion with that ID in this server.",
                                                           ephemeral=True)
        category, user_id, text, channel_id, message_id = row
        await self.bot.db.execute("UPDATE suggestions SET status=? WHERE id=?", (status, suggestion_id))
        await self.bot.db.commit()
        channel = interaction.guild.get_channel(channel_id)
        if channel:
            try:
                message = await channel.fetch_message(message_id)
                embed = message.embeds[0]
                embed.color = STATUS_COLOR[status]
                footer = f"{status.title()} by {interaction.user} • #{suggestion_id}"
                embed.set_footer(text=footer + (f" • {reason}" if reason else ""))
                await message.edit(embed=embed)
            except discord.HTTPException:
                pass
        await interaction.response.send_message(f"Suggestion #{suggestion_id} marked **{status}**.", ephemeral=True)

    @suggestion.command(name="approve", description="Mark a suggestion approved (staff)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def approve(self, interaction: discord.Interaction, suggestion_id: int, reason: str | None = None):
        await self.resolve(interaction, suggestion_id, "approved", reason)

    @suggestion.command(name="deny", description="Mark a suggestion denied (staff)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def deny(self, interaction: discord.Interaction, suggestion_id: int, reason: str | None = None):
        await self.resolve(interaction, suggestion_id, "denied", reason)


class BugExploit(commands.Cog):
    """/bug-exploit report: players report bugs and exploits in the Roblox game, sent to a private staff channel."""

    group = app_commands.Group(name="bug-exploit", description="Report a bug or exploit in the Roblox game",
                               guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute("CREATE TABLE IF NOT EXISTS bugreport_config (guild_id INTEGER PRIMARY KEY, "
                         "channel_id INTEGER, ping_role_id INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS bug_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "guild_id INTEGER, user_id INTEGER, title TEXT, description TEXT, severity TEXT, "
                         "proof_url TEXT, status TEXT DEFAULT 'open', channel_id INTEGER, message_id INTEGER, "
                         "created_at INTEGER)")
        await db.commit()

    @group.command(name="setup", description="Choose the private channel bug/exploit reports are sent to")
    @app_commands.describe(channel="A staff-only channel -- exploit reports can be sensitive",
                           ping_role="Optional role to ping on a new report (e.g. your developer role)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(embed_links=True)
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel,
                        ping_role: discord.Role | None = None):
        me = interaction.guild.me
        if not (channel.permissions_for(me).send_messages and channel.permissions_for(me).embed_links):
            return await interaction.response.send_message(
                f"I need Send Messages and Embed Links in {channel.mention}.", ephemeral=True)
        if channel.permissions_for(interaction.guild.default_role).view_channel:
            await interaction.response.send_message(
                f"Heads up: {channel.mention} looks visible to everyone. Exploit reports are easiest to abuse before "
                "they're fixed, so a staff-only channel is safer.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO bugreport_config (guild_id, channel_id, ping_role_id) VALUES (?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, ping_role_id=excluded.ping_role_id",
            (interaction.guild_id, channel.id, ping_role.id if ping_role else None))
        await self.bot.db.commit()
        if interaction.response.is_done():
            await interaction.followup.send(f"Bug/exploit reports will now be sent to {channel.mention}.",
                                            ephemeral=True)
        else:
            await interaction.response.send_message(f"Bug/exploit reports will now be sent to {channel.mention}.",
                                                    ephemeral=True)

    @group.command(name="report", description="Report a bug or exploit in the Roblox game (sent privately to staff)")
    @app_commands.describe(title="Short summary of the bug or exploit",
                           description="What happens and how to reproduce it (type \\n for a new line)",
                           severity="How bad is it? Pick Critical if it's being actively abused right now",
                           proof="Optional screenshot or video showing it")
    @app_commands.choices(severity=SEVERITY_CHOICES)
    async def report(self, interaction: discord.Interaction, title: app_commands.Range[str, 3, 150],
                     description: app_commands.Range[str, 5, 1500], severity: app_commands.Choice[str],
                     proof: discord.Attachment | None = None):
        async with self.bot.db.execute("SELECT channel_id, ping_role_id FROM bugreport_config WHERE guild_id=?",
                                       (interaction.guild_id,)) as cur:
            row = await cur.fetchone()
        channel = interaction.guild.get_channel(row[0]) if row else None
        if not channel:
            return await interaction.response.send_message(
                "Bug/exploit reporting isn't set up yet. Ask staff to run `/bug-exploit setup` first.", ephemeral=True)
        if proof and proof.size > 25 * 1024 * 1024:
            return await interaction.response.send_message("That file is too large to attach.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        desc = description.replace("\\n", "\n")
        embed = discord.Embed(title=f"[{severity.value}] {title}", description=desc,
                              color=SEVERITY_COLOR[severity.value], timestamp=discord.utils.utcnow())
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="Reported by", value=f"{interaction.user.mention} ({interaction.user.id})")
        embed.set_footer(text="Open")
        file = None
        if proof:
            try:
                file = await proof.to_file()
                if (proof.content_type or "").startswith("image/"):
                    embed.set_image(url=f"attachment://{file.filename}")
            except discord.HTTPException:
                file = None
        ping = f"<@&{row[1]}>" if row[1] else None
        try:
            message = await channel.send(content=ping, embed=embed, **({"file": file} if file else {}),
                                         allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException:
            return await interaction.followup.send("I couldn't post that report. Ask staff to check my permissions "
                                                    f"in {channel.mention}.", ephemeral=True)
        cur = await self.bot.db.execute(
            "INSERT INTO bug_reports (guild_id, user_id, title, description, severity, proof_url, channel_id, "
            "message_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (interaction.guild_id, interaction.user.id, title, desc, severity.value,
             proof.url if proof else None, channel.id, message.id, int(time.time())))
        await self.bot.db.commit()
        embed.set_footer(text=f"Open • #{cur.lastrowid}")
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass
        await interaction.followup.send(f"Report #{cur.lastrowid} sent to staff privately. Thanks for the heads up.",
                                        ephemeral=True)

    @group.command(name="resolve", description="Mark a bug/exploit report resolved or closed (staff)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def resolve_cmd(self, interaction: discord.Interaction, report_id: int, note: str | None = None):
        async with self.bot.db.execute(
                "SELECT channel_id, message_id FROM bug_reports WHERE id=? AND guild_id=?",
                (report_id, interaction.guild_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I can't find a report with that ID in this server.",
                                                           ephemeral=True)
        await self.bot.db.execute("UPDATE bug_reports SET status='resolved' WHERE id=?", (report_id,))
        await self.bot.db.commit()
        channel = interaction.guild.get_channel(row[0]) if row[0] else None
        if channel:
            try:
                message = await channel.fetch_message(row[1])
                embed = message.embeds[0]
                embed.color = discord.Color.dark_grey()
                embed.set_footer(text=f"Resolved by {interaction.user} • #{report_id}" + (f" • {note}" if note else ""))
                await message.edit(embed=embed)
            except discord.HTTPException:
                pass
        await interaction.response.send_message(f"Report #{report_id} marked resolved." + (f" {note}" if note else ""),
                                                ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Suggestions(bot))
    await bot.add_cog(BugExploit(bot))
