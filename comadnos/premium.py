import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


class Premium(commands.Cog):
    def __init__(self, bot: commands.Bot, database_path: Path) -> None:
        self.bot = bot
        self.database_path = database_path
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS premium_guilds (
                    guild_id INTEGER PRIMARY KEY,
                    added_by INTEGER NOT NULL,
                    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(premium_guilds)").fetchall()
            }
            if "expires_at" not in columns:
                connection.execute("ALTER TABLE premium_guilds ADD COLUMN expires_at TEXT")

    @app_commands.command(name="premium_agregar", description="Activa premium en un servidor")
    @app_commands.describe(guild_id="ID del servidor que recibirá premium")
    @app_commands.describe(duracion_dias="Días que durará; vacío significa permanente")
    @app_commands.checks.is_owner()
    async def premium_add(
        self,
        interaction: discord.Interaction,
        guild_id: app_commands.Range[int, 1, None],
        duracion_dias: app_commands.Range[int, 1, 3650] | None = None,
    ) -> None:
        expires_at = None
        if duracion_dias is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=duracion_dias)
            ).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT expires_at FROM premium_guilds WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            if existing and duracion_dias is not None and existing["expires_at"]:
                current_expiry = datetime.strptime(
                    existing["expires_at"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                expiry_base = max(current_expiry, datetime.now(timezone.utc))
                expires_at = (expiry_base + timedelta(days=duracion_dias)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            connection.execute(
                "INSERT INTO premium_guilds (guild_id, added_by, expires_at) "
                "VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
                "added_by = excluded.added_by, expires_at = excluded.expires_at",
                (guild_id, interaction.user.id, expires_at),
            )
        duration_label = f"durante {duracion_dias} días" if duracion_dias else "permanente"
        message = (
            f"Premium activado para el servidor `{guild_id}` ({duration_label}): "
            "10 formularios y 50 preguntas por formulario."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="premium_quitar", description="Desactiva premium en un servidor")
    @app_commands.describe(guild_id="ID del servidor al que se quitará premium")
    @app_commands.checks.is_owner()
    async def premium_remove(
        self, interaction: discord.Interaction, guild_id: app_commands.Range[int, 1, None]
    ) -> None:
        with self._connect() as connection:
            removed = connection.execute(
                "DELETE FROM premium_guilds WHERE guild_id = ?", (guild_id,)
            ).rowcount
        message = (
            f"Premium quitado al servidor `{guild_id}`."
            if removed
            else f"El servidor `{guild_id}` no estaba en premium."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="premium_lista", description="Lista los servidores con premium")
    @app_commands.checks.is_owner()
    async def premium_list(self, interaction: discord.Interaction) -> None:
        with self._connect() as connection:
            premium_guilds = connection.execute(
                "SELECT guild_id, added_at, expires_at FROM premium_guilds "
                "ORDER BY added_at DESC"
            ).fetchall()
        if not premium_guilds:
            await interaction.response.send_message("No hay servidores premium.", ephemeral=True)
            return

        lines = []
        for premium_guild in premium_guilds[:40]:
            guild = self.bot.get_guild(premium_guild["guild_id"])
            guild_name = (
                discord.utils.escape_markdown(guild.name)
                if guild
                else "Servidor no cacheado"
            )
            lines.append(
                f"**{guild_name}** · `{premium_guild['guild_id']}` · "
                f"{'Permanente' if premium_guild['expires_at'] is None else 'Vence ' + premium_guild['expires_at']}"
            )
        remainder = len(premium_guilds) - len(lines)
        if remainder:
            lines.append(f"Y {remainder} servidores más.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "Solo el owner configurado del bot puede gestionar premium."
        else:
            message = "No se pudo completar la gestión premium. Inténtalo de nuevo."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
