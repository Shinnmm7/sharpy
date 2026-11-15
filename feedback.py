import io
import re
import time
from datetime import datetime, timezone

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


def channel_name(prefix: str, user: discord.abc.User) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", user.name.lower()).strip("-") or "user"
    return f"{prefix}-{slug}"[:90]


async def save_transcript(channel: discord.TextChannel) -> discord.File:
    lines = []
    try:
        async for m in channel.history(limit=None, oldest_first=True):
            text = m.content or ""
            if m.embeds and not text:
                text = "[embed] " + " ".join(filter(None, (m.embeds[0].title, m.embeds[0].description)))[:300]
            files = " ".join(a.url for a in m.attachments)
            lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author} : {text} {files}".strip())
    except discord.HTTPException:
        lines.append("(transcript incomplete: I couldn't read the full history)")
    return discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"{channel.name}.txt")


class SuggestionControls(discord.ui.View):
    """Approve/Deny/Close buttons inside a suggestion channel. Persistent: still work after the bot restarts.
    The cog looks up which suggestion a click belongs to by channel ID, the same way ticket channels work."""

    def __init__(self, cog: "Suggestions", decided: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        approve = discord.ui.Button(label="Approve", style=discord.ButtonStyle.success,
                                    custom_id="suggestion:approve", disabled=decided)
        deny = discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger,
                                 custom_id="suggestion:deny", disabled=decided)
        close = discord.ui.Button(label="Close", emoji="🔒", style=discord.ButtonStyle.secondary,
                                  custom_id="suggestion:close")
        approve.callback, deny.callback = self.decide("approved"), self.decide("denied")
        close.callback = self.close
        for item in (approve, deny, close):
            self.add_item(item)

    def decide(self, status: str):
        async def callback(interaction: discord.Interaction):
            await self.cog.handle_decide(interaction, status)
        return callback

    async def close(self, interaction: discord.Interaction):
        await self.cog.close_channel(interaction)


class Suggestions(commands.Cog):
    """/suggestion: members suggest things for the Roblox game or the Discord server. Submitting opens a private
    channel for that one suggestion, like a support ticket, where staff discuss and decide it."""

    suggestion = app_commands.Group(name="suggestion", description="Suggest something for the game or the server",
                                    guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS suggestion_config (
                guild_id INTEGER PRIMARY KEY, category_id INTEGER, staff_role_id INTEGER, log_channel_id INTEGER)""")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS suggestions (
                channel_id INTEGER PRIMARY KEY, guild_id INTEGER, user_id INTEGER, category TEXT, text TEXT,
                message_id INTEGER, status TEXT DEFAULT 'pending', created_at INTEGER)""")
        await db.commit()
        self.bot.add_view(SuggestionControls(self))

    async def config(self, guild_id: int):
        async with self.bot.db.execute(
                "SELECT category_id, staff_role_id, log_channel_id FROM suggestion_config WHERE guild_id=?",
                (guild_id,)) as cur:
            row = await cur.fetchone()
        return {"category_id": row[0], "staff_role_id": row[1], "log_channel_id": row[2]} if row else None

    async def row_for_channel(self, channel_id: int):
        async with self.bot.db.execute("SELECT user_id, category, status FROM suggestions WHERE channel_id=?",
                                       (channel_id,)) as cur:
            return await cur.fetchone()

    def is_staff(self, member: discord.Member, cfg: dict) -> bool:
        return member.guild_permissions.manage_guild or any(r.id == cfg["staff_role_id"] for r in member.roles)

    # ---------- /suggestion setup ----------

    @suggestion.command(name="setup", description="Set up where new suggestion channels are created")
    @app_commands.describe(category="Category that new suggestion channels are created in",
                           staff_role="Role that can see and decide every suggestion",
                           log_channel="Where transcripts are sent when a suggestion channel closes (optional)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def setup_cmd(self, interaction: discord.Interaction, category: discord.CategoryChannel,
                        staff_role: discord.Role, log_channel: discord.TextChannel | None = None):
        if not category.permissions_for(interaction.guild.me).manage_channels:
            return await interaction.response.send_message(
                f"I need Manage Channels in the **{category.name}** category.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO suggestion_config (guild_id, category_id, staff_role_id, log_channel_id) VALUES (?,?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET category_id=excluded.category_id, "
            "staff_role_id=excluded.staff_role_id, log_channel_id=excluded.log_channel_id",
            (interaction.guild_id, category.id, staff_role.id, log_channel.id if log_channel else None))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"Got it. New suggestions will open a private channel in **{category.name}**, visible to "
            f"{staff_role.mention}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ---------- /suggestion submit (opens the channel) ----------

    @suggestion.command(name="submit", description="Suggest something for the Roblox game or the Discord server")
    @app_commands.describe(category="What the suggestion is for",
                           suggestion="Your suggestion (type \\n for a new line)")
    @app_commands.choices(category=CATEGORY_CHOICES)
    async def submit(self, interaction: discord.Interaction, category: app_commands.Choice[str],
                     suggestion: app_commands.Range[str, 5, 1500]):
        guild, user = interaction.guild, interaction.user
        cfg = await self.config(guild.id)
        cat_channel = guild.get_channel(cfg["category_id"]) if cfg else None
        staff_role = guild.get_role(cfg["staff_role_id"]) if cfg else None
        if not cfg or cat_channel is None or staff_role is None:
            return await interaction.response.send_message(
                "Suggestions aren't set up yet. Ask staff to run `/suggestion setup` first.", ephemeral=True)

        async with self.bot.db.execute(
                "SELECT channel_id FROM suggestions WHERE guild_id=? AND user_id=? AND status='pending'",
                (guild.id, user.id)) as cur:
            existing = [r[0] for r in await cur.fetchall()]
        for channel_id in existing:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                return await interaction.response.send_message(
                    f"You already have an open suggestion: {channel.mention}", ephemeral=True)
            await self.bot.db.execute("DELETE FROM suggestions WHERE channel_id=?", (channel_id,))  # deleted by hand
        await self.bot.db.commit()

        text = suggestion.replace("\\n", "\n")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                              attach_files=True, embed_links=True),
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                    read_message_history=True, attach_files=True,
                                                    manage_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                  read_message_history=True, manage_channels=True),
        }
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await guild.create_text_channel(
                channel_name(f"suggest-{category.value}", user), category=cat_channel, overwrites=overwrites,
                topic=f"{category.name} suggestion from {user} ({user.id})",
                reason=f"Suggestion opened by {user}")
        except discord.HTTPException:
            return await interaction.followup.send(
                "I couldn't create the suggestion channel (the category may be full, or I'm missing permissions).",
                ephemeral=True)

        embed = discord.Embed(title=f"💡 {category.name} suggestion", description=text,
                              color=STATUS_COLOR["pending"], timestamp=datetime.now(timezone.utc))
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.set_footer(text="Pending")
        message_id = None
        try:
            message = await channel.send(content=f"{user.mention} {staff_role.mention}", embed=embed,
                                         view=SuggestionControls(self),
                                         allowed_mentions=discord.AllowedMentions(users=[user], roles=[staff_role]))
            message_id = message.id
        except discord.HTTPException:
            pass
        await self.bot.db.execute(
            "INSERT INTO suggestions (channel_id, guild_id, user_id, category, text, message_id, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (channel.id, guild.id, user.id, category.value, text, message_id, int(time.time())))
        await self.bot.db.commit()
        await interaction.followup.send(f"Your suggestion is in: {channel.mention}", ephemeral=True)
        await self.bot.log(guild, "Suggestion opened", f"{category.name} suggestion {channel.mention} by {user}")

    # ---------- deciding and closing (buttons inside the channel) ----------

    async def handle_decide(self, interaction: discord.Interaction, status: str):
        cfg = await self.config(interaction.guild_id)
        if not cfg or not self.is_staff(interaction.user, cfg):
            return await interaction.response.send_message("Only staff can decide suggestions.", ephemeral=True)
        row = await self.row_for_channel(interaction.channel_id)
        if not row:
            return await interaction.response.send_message("This isn't an open suggestion channel.", ephemeral=True)
        user_id, category, current = row
        if current != "pending":
            return await interaction.response.send_message("This suggestion has already been decided.", ephemeral=True)
        await self.bot.db.execute("UPDATE suggestions SET status=? WHERE channel_id=?",
                                  (status, interaction.channel_id))
        await self.bot.db.commit()
        embed = interaction.message.embeds[0]
        embed.color = STATUS_COLOR[status]
        embed.set_footer(text=f"{status.title()} by {interaction.user}")
        await interaction.response.edit_message(embed=embed, view=SuggestionControls(self, decided=True))
        await self.bot.log(interaction.guild, f"Suggestion {status}",
                           f"{interaction.channel.mention} {status} by {interaction.user}",
                           STATUS_COLOR[status])

    async def close_channel(self, interaction: discord.Interaction):
        cfg = await self.config(interaction.guild_id)
        row = await self.row_for_channel(interaction.channel_id)
        if not row or not cfg:
            return await interaction.response.send_message("This isn't an open suggestion channel.", ephemeral=True)
        user_id, category, status = row
        if interaction.user.id != user_id and not self.is_staff(interaction.user, cfg):
            return await interaction.response.send_message("Only the submitter or staff can close this.",
                                                           ephemeral=True)
        await interaction.response.send_message("Closing this suggestion and saving a transcript…")
        channel = interaction.channel
        transcript = await save_transcript(channel)
        log_channel = interaction.guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
        if log_channel is not None:
            try:
                await log_channel.send(
                    f"Suggestion **{channel.name}** ({CATEGORY_LABEL.get(category, category)}, {status}) closed by "
                    f"{interaction.user}. Submitted by <@{user_id}>.", file=transcript,
                    allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        await self.bot.db.execute("DELETE FROM suggestions WHERE channel_id=?", (channel.id,))
        await self.bot.db.commit()
        await self.bot.log(interaction.guild, "Suggestion closed", f"{channel.name} closed by {interaction.user}")
        try:
            await channel.delete(reason=f"Suggestion closed by {interaction.user}")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        # keep the table tidy when a suggestion channel is deleted by hand
        await self.bot.db.execute("DELETE FROM suggestions WHERE channel_id=?", (channel.id,))
        await self.bot.db.commit()


class BugReportControls(discord.ui.View):
    """Resolve/Close buttons inside a bug/exploit report channel. Persistent: still work after the bot restarts."""

    def __init__(self, cog: "BugExploit", decided: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        resolve = discord.ui.Button(label="Resolved" if decided else "Resolve",
                                    style=discord.ButtonStyle.secondary if decided else discord.ButtonStyle.success,
                                    custom_id="bugreport:resolve", disabled=decided)
        close = discord.ui.Button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger,
                                  custom_id="bugreport:close")
        resolve.callback = self.resolve
        close.callback = self.close
        for item in (resolve, close):
            self.add_item(item)

    async def resolve(self, interaction: discord.Interaction):
        await self.cog.handle_resolve(interaction)

    async def close(self, interaction: discord.Interaction):
        await self.cog.close_channel(interaction)


class BugExploit(commands.Cog):
    """/bug-exploit report: players report bugs/exploits in the Roblox game. Reporting opens a private staff
    channel for that one report, like a support ticket -- exploit reports are easiest to abuse before they're
    patched, so each one stays visible only to the reporter, staff, and the bot."""

    group = app_commands.Group(name="bug-exploit", description="Report a bug or exploit in the Roblox game",
                               guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db = self.bot.db
        await db.execute(
            """CREATE TABLE IF NOT EXISTS bugreport_config (
                guild_id INTEGER PRIMARY KEY, category_id INTEGER, staff_role_id INTEGER, log_channel_id INTEGER)""")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS bug_reports (
                channel_id INTEGER PRIMARY KEY, guild_id INTEGER, user_id INTEGER, title TEXT, description TEXT,
                severity TEXT, proof_url TEXT, message_id INTEGER, status TEXT DEFAULT 'open', created_at INTEGER)""")
        await db.commit()
        self.bot.add_view(BugReportControls(self))

    async def config(self, guild_id: int):
        async with self.bot.db.execute(
                "SELECT category_id, staff_role_id, log_channel_id FROM bugreport_config WHERE guild_id=?",
                (guild_id,)) as cur:
            row = await cur.fetchone()
        return {"category_id": row[0], "staff_role_id": row[1], "log_channel_id": row[2]} if row else None

    async def row_for_channel(self, channel_id: int):
        async with self.bot.db.execute("SELECT user_id, title, status FROM bug_reports WHERE channel_id=?",
                                       (channel_id,)) as cur:
            return await cur.fetchone()

    def is_staff(self, member: discord.Member, cfg: dict) -> bool:
        return member.guild_permissions.manage_guild or any(r.id == cfg["staff_role_id"] for r in member.roles)

    # ---------- /bug-exploit setup ----------

    @group.command(name="setup", description="Set up where new bug/exploit report channels are created")
    @app_commands.describe(category="A staff-only category -- exploit reports can be sensitive",
                           staff_role="Role that can see and resolve every report",
                           log_channel="Where transcripts are sent when a report channel closes (optional)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def setup_cmd(self, interaction: discord.Interaction, category: discord.CategoryChannel,
                        staff_role: discord.Role, log_channel: discord.TextChannel | None = None):
        if not category.permissions_for(interaction.guild.me).manage_channels:
            return await interaction.response.send_message(
                f"I need Manage Channels in the **{category.name}** category.", ephemeral=True)
        await self.bot.db.execute(
            "INSERT INTO bugreport_config (guild_id, category_id, staff_role_id, log_channel_id) VALUES (?,?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET category_id=excluded.category_id, "
            "staff_role_id=excluded.staff_role_id, log_channel_id=excluded.log_channel_id",
            (interaction.guild_id, category.id, staff_role.id, log_channel.id if log_channel else None))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"Got it. New bug/exploit reports will open a private channel in **{category.name}**, visible to "
            f"{staff_role.mention}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ---------- /bug-exploit report (opens the channel) ----------

    @group.command(name="report", description="Report a bug or exploit in the Roblox game (opens a private staff channel)")
    @app_commands.describe(title="Short summary of the bug or exploit",
                           description="What happens and how to reproduce it (type \\n for a new line)",
                           severity="How bad is it? Pick Critical if it's being actively abused right now",
                           proof="Optional screenshot or video showing it")
    @app_commands.choices(severity=SEVERITY_CHOICES)
    async def report(self, interaction: discord.Interaction, title: app_commands.Range[str, 3, 150],
                     description: app_commands.Range[str, 5, 1500], severity: app_commands.Choice[str],
                     proof: discord.Attachment | None = None):
        guild, user = interaction.guild, interaction.user
        cfg = await self.config(guild.id)
        cat_channel = guild.get_channel(cfg["category_id"]) if cfg else None
        staff_role = guild.get_role(cfg["staff_role_id"]) if cfg else None
        if not cfg or cat_channel is None or staff_role is None:
            return await interaction.response.send_message(
                "Bug/exploit reporting isn't set up yet. Ask staff to run `/bug-exploit setup` first.", ephemeral=True)
        if proof and proof.size > 25 * 1024 * 1024:
            return await interaction.response.send_message("That file is too large to attach.", ephemeral=True)

        async with self.bot.db.execute(
                "SELECT channel_id FROM bug_reports WHERE guild_id=? AND user_id=? AND status='open'",
                (guild.id, user.id)) as cur:
            existing = [r[0] for r in await cur.fetchall()]
        for channel_id in existing:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                return await interaction.response.send_message(
                    f"You already have an open report: {channel.mention}", ephemeral=True)
            await self.bot.db.execute("DELETE FROM bug_reports WHERE channel_id=?", (channel_id,))  # deleted by hand
        await self.bot.db.commit()

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                              attach_files=True, embed_links=True),
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                    read_message_history=True, attach_files=True,
                                                    manage_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                  read_message_history=True, manage_channels=True),
        }
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await guild.create_text_channel(
                channel_name("bugreport", user), category=cat_channel, overwrites=overwrites,
                topic=f"[{severity.value}] {title} -- reported by {user} ({user.id})",
                reason=f"Bug/exploit report opened by {user}")
        except discord.HTTPException:
            return await interaction.followup.send(
                "I couldn't create the report channel (the category may be full, or I'm missing permissions).",
                ephemeral=True)

        desc = description.replace("\\n", "\n")
        embed = discord.Embed(title=f"[{severity.value}] {title}", description=desc,
                              color=SEVERITY_COLOR[severity.value], timestamp=datetime.now(timezone.utc))
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.set_footer(text="Open")
        file = None
        if proof:
            try:
                file = await proof.to_file()
                if (proof.content_type or "").startswith("image/"):
                    embed.set_image(url=f"attachment://{file.filename}")
            except discord.HTTPException:
                file = None
        message_id = None
        try:
            message = await channel.send(content=f"{user.mention} {staff_role.mention}", embed=embed,
                                         view=BugReportControls(self), **({"file": file} if file else {}),
                                         allowed_mentions=discord.AllowedMentions(users=[user], roles=[staff_role]))
            message_id = message.id
        except discord.HTTPException:
            pass
        await self.bot.db.execute(
            "INSERT INTO bug_reports (channel_id, guild_id, user_id, title, description, severity, proof_url, "
            "message_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (channel.id, guild.id, user.id, title, desc, severity.value, proof.url if proof else None, message_id,
             int(time.time())))
        await self.bot.db.commit()
        await interaction.followup.send(f"Your report is in: {channel.mention}", ephemeral=True)
        await self.bot.log(guild, "Bug/exploit report opened", f"{channel.mention} by {user}")

    # ---------- resolving and closing (buttons inside the channel) ----------

    async def handle_resolve(self, interaction: discord.Interaction):
        cfg = await self.config(interaction.guild_id)
        if not cfg or not self.is_staff(interaction.user, cfg):
            return await interaction.response.send_message("Only staff can resolve reports.", ephemeral=True)
        row = await self.row_for_channel(interaction.channel_id)
        if not row:
            return await interaction.response.send_message("This isn't an open report channel.", ephemeral=True)
        user_id, title, status = row
        if status != "open":
            return await interaction.response.send_message("This report has already been resolved.", ephemeral=True)
        await self.bot.db.execute("UPDATE bug_reports SET status='resolved' WHERE channel_id=?",
                                  (interaction.channel_id,))
        await self.bot.db.commit()
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.dark_grey()
        embed.set_footer(text=f"Resolved by {interaction.user}")
        await interaction.response.edit_message(embed=embed, view=BugReportControls(self, decided=True))
        await self.bot.log(interaction.guild, "Bug/exploit report resolved",
                           f"{interaction.channel.mention} resolved by {interaction.user}")

    async def close_channel(self, interaction: discord.Interaction):
        cfg = await self.config(interaction.guild_id)
        row = await self.row_for_channel(interaction.channel_id)
        if not row or not cfg:
            return await interaction.response.send_message("This isn't an open report channel.", ephemeral=True)
        user_id, title, status = row
        if interaction.user.id != user_id and not self.is_staff(interaction.user, cfg):
            return await interaction.response.send_message("Only the reporter or staff can close this.",
                                                           ephemeral=True)
        await interaction.response.send_message("Closing this report and saving a transcript…")
        channel = interaction.channel
        transcript = await save_transcript(channel)
        log_channel = interaction.guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
        if log_channel is not None:
            try:
                await log_channel.send(
                    f"Report **{channel.name}** ({status}) closed by {interaction.user}. "
                    f"Reported by <@{user_id}>.", file=transcript, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        await self.bot.db.execute("DELETE FROM bug_reports WHERE channel_id=?", (channel.id,))
        await self.bot.db.commit()
        await self.bot.log(interaction.guild, "Bug/exploit report closed", f"{channel.name} closed by {interaction.user}")
        try:
            await channel.delete(reason=f"Report closed by {interaction.user}")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        # keep the table tidy when a report channel is deleted by hand
        await self.bot.db.execute("DELETE FROM bug_reports WHERE channel_id=?", (channel.id,))
        await self.bot.db.commit()


async def setup(bot: commands.Bot):
    await bot.add_cog(Suggestions(bot))
    await bot.add_cog(BugExploit(bot))
