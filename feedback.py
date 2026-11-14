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


class SuggestionView(discord.ui.View):
    """Vote and Approve/Deny buttons under a suggestion. Persistent: still work after the bot restarts.
    custom_ids are static (not per-message); the cog looks up which suggestion a click belongs to by message ID,
    the same way the giveaway Enter button does."""

    def __init__(self, cog: "Suggestions", up: int = 0, down: int = 0, decided: bool = False):
        super().__init__(timeout=None)
        up_btn = discord.ui.Button(label=f"{up}", emoji="👍", style=discord.ButtonStyle.secondary,
                                   custom_id="suggestion:up", disabled=decided)
        down_btn = discord.ui.Button(label=f"{down}", emoji="👎", style=discord.ButtonStyle.secondary,
                                     custom_id="suggestion:down", disabled=decided)
        approve_btn = discord.ui.Button(label="Approve", style=discord.ButtonStyle.success,
                                        custom_id="suggestion:approve", disabled=decided)
        deny_btn = discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger,
                                     custom_id="suggestion:deny", disabled=decided)
        up_btn.callback, down_btn.callback = self.vote(cog, "up"), self.vote(cog, "down")
        approve_btn.callback, deny_btn.callback = self.decide(cog, "approved"), self.decide(cog, "denied")
        for item in (up_btn, down_btn, approve_btn, deny_btn):
            self.add_item(item)

    def vote(self, cog: "Suggestions", choice: str):
        async def callback(interaction: discord.Interaction):
            await cog.handle_vote(interaction, choice)
        return callback

    def decide(self, cog: "Suggestions", status: str):
        async def callback(interaction: discord.Interaction):
            await cog.handle_decide(interaction, status)
        return callback


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
        await db.execute("CREATE TABLE IF NOT EXISTS suggestion_votes (suggestion_id INTEGER, user_id INTEGER, "
                         "vote TEXT, PRIMARY KEY (suggestion_id, user_id))")
        await db.commit()
        self.bot.add_view(SuggestionView(self))

    async def vote_counts(self, suggestion_id: int) -> tuple[int, int]:
        async with self.bot.db.execute(
                "SELECT vote, COUNT(*) FROM suggestion_votes WHERE suggestion_id=? GROUP BY vote",
                (suggestion_id,)) as cur:
            rows = dict(await cur.fetchall())
        return rows.get("up", 0), rows.get("down", 0)

    async def handle_vote(self, interaction: discord.Interaction, choice: str):
        async with self.bot.db.execute("SELECT id, status FROM suggestions WHERE message_id=?",
                                       (interaction.message.id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I can't find this suggestion anymore.", ephemeral=True)
        suggestion_id, status = row
        if status != "pending":
            return await interaction.response.send_message("This suggestion has already been decided.", ephemeral=True)
        async with self.bot.db.execute(
                "SELECT vote FROM suggestion_votes WHERE suggestion_id=? AND user_id=?",
                (suggestion_id, interaction.user.id)) as cur:
            existing = await cur.fetchone()
        if existing and existing[0] == choice:
            await self.bot.db.execute("DELETE FROM suggestion_votes WHERE suggestion_id=? AND user_id=?",
                                      (suggestion_id, interaction.user.id))
        else:
            await self.bot.db.execute(
                "INSERT INTO suggestion_votes (suggestion_id, user_id, vote) VALUES (?,?,?) "
                "ON CONFLICT(suggestion_id, user_id) DO UPDATE SET vote=excluded.vote",
                (suggestion_id, interaction.user.id, choice))
        await self.bot.db.commit()
        up, down = await self.vote_counts(suggestion_id)
        await interaction.response.edit_message(view=SuggestionView(self, up, down))

    async def handle_decide(self, interaction: discord.Interaction, status: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server to decide suggestions.",
                                                            ephemeral=True)
        async with self.bot.db.execute("SELECT id, status FROM suggestions WHERE message_id=?",
                                       (interaction.message.id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I can't find this suggestion anymore.", ephemeral=True)
        suggestion_id, current = row
        if current != "pending":
            return await interaction.response.send_message("This suggestion has already been decided.", ephemeral=True)
        await self.bot.db.execute("UPDATE suggestions SET status=? WHERE id=?", (status, suggestion_id))
        await self.bot.db.commit()
        up, down = await self.vote_counts(suggestion_id)
        embed = interaction.message.embeds[0]
        embed.color = STATUS_COLOR[status]
        embed.set_footer(text=f"{status.title()} by {interaction.user} • #{suggestion_id}")
        await interaction.response.edit_message(embed=embed, view=SuggestionView(self, up, down, decided=True))

    async def channel_for(self, guild_id: int, category: str) -> int | None:
        async with self.bot.db.execute("SELECT channel_id FROM suggestion_config WHERE guild_id=? AND category=?",
                                       (guild_id, category)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    @suggestion.command(name="setup", description="Choose which channel each kind of suggestion is posted in")
    @app_commands.describe(game_channel="Where Roblox game suggestions are posted",
                           server_channel="Where Discord server suggestions are posted")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(embed_links=True)
    async def setup_cmd(self, interaction: discord.Interaction, game_channel: discord.TextChannel | None = None,
                        server_channel: discord.TextChannel | None = None):
        if game_channel is None and server_channel is None:
            return await interaction.response.send_message("Pick at least one channel to set.", ephemeral=True)
        me = interaction.guild.me
        for channel in (game_channel, server_channel):
            if channel and not (channel.permissions_for(me).send_messages and channel.permissions_for(me).embed_links):
                return await interaction.response.send_message(
                    f"I need Send Messages and Embed Links in {channel.mention}.", ephemeral=True)
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
            message = await channel.send(embed=embed, view=SuggestionView(self))
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
                up, down = await self.vote_counts(suggestion_id)
                await message.edit(embed=embed, view=SuggestionView(self, up, down, decided=True))
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


class ResolveView(discord.ui.View):
    """The Resolve button under a bug/exploit report. Persistent: still works after the bot restarts."""

    def __init__(self, cog: "BugExploit", decided: bool = False):
        super().__init__(timeout=None)
        button = discord.ui.Button(label="Resolved" if decided else "Resolve",
                                   style=discord.ButtonStyle.secondary if decided else discord.ButtonStyle.success,
                                   custom_id="bugreport:resolve", disabled=decided)
        button.callback = self.resolve
        self.cog = cog
        self.add_item(button)

    async def resolve(self, interaction: discord.Interaction):
        await self.cog.handle_resolve(interaction)


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
        self.bot.add_view(ResolveView(self))

    async def handle_resolve(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server to resolve reports.",
                                                            ephemeral=True)
        async with self.bot.db.execute("SELECT id, status FROM bug_reports WHERE message_id=?",
                                       (interaction.message.id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("I can't find this report anymore.", ephemeral=True)
        report_id, status = row
        if status != "open":
            return await interaction.response.send_message("This report has already been resolved.", ephemeral=True)
        await self.bot.db.execute("UPDATE bug_reports SET status='resolved' WHERE id=?", (report_id,))
        await self.bot.db.commit()
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.dark_grey()
        embed.set_footer(text=f"Resolved by {interaction.user} \N{BULLET} #{report_id}")
        await interaction.response.edit_message(embed=embed, view=ResolveView(self, decided=True))

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
            message = await channel.send(content=ping, embed=embed, view=ResolveView(self),
                                         **({"file": file} if file else {}),
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
                await message.edit(embed=embed, view=ResolveView(self, decided=True))
            except discord.HTTPException:
                pass
        await interaction.response.send_message(f"Report #{report_id} marked resolved." + (f" {note}" if note else ""),
                                                ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Suggestions(bot))
    await bot.add_cog(BugExploit(bot))
