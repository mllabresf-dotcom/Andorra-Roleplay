import asyncio
import io
import json
import sqlite3
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


FREE_MAX_FORMS = 3
PREMIUM_MAX_FORMS = 10
FREE_MAX_QUESTIONS = 20
PREMIUM_MAX_QUESTIONS = 50
DM_TIMEOUT = 300


class Formularios(commands.Cog):
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
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS forms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE,
                    log_channel_id INTEGER,
                    message_json TEXT NOT NULL DEFAULT '{}',
                    message_channel_id INTEGER,
                    published_message_id INTEGER,
                    UNIQUE (guild_id, name)
                );
                CREATE TABLE IF NOT EXISTS questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    form_id INTEGER NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    UNIQUE (form_id, position)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    guild_id INTEGER PRIMARY KEY,
                    results_channel_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS form_staff_roles (
                    form_id INTEGER NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
                    action TEXT NOT NULL CHECK (action IN ('accept', 'reject')),
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (form_id, action, role_id)
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    form_id INTEGER NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    answers_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reviewer_id INTEGER,
                    review_reason TEXT,
                    review_message_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    decided_at TEXT
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(forms)").fetchall()
            }
            if "log_channel_id" not in columns:
                connection.execute("ALTER TABLE forms ADD COLUMN log_channel_id INTEGER")
            if "message_json" not in columns:
                connection.execute(
                    "ALTER TABLE forms ADD COLUMN message_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "message_channel_id" not in columns:
                connection.execute("ALTER TABLE forms ADD COLUMN message_channel_id INTEGER")
            if "published_message_id" not in columns:
                connection.execute("ALTER TABLE forms ADD COLUMN published_message_id INTEGER")
            submission_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(submissions)").fetchall()
            }
            if "review_reason" not in submission_columns:
                connection.execute("ALTER TABLE submissions ADD COLUMN review_reason TEXT")
            old_settings = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'settings'"
            ).fetchone()
            if old_settings:
                connection.execute(
                    "UPDATE forms SET log_channel_id = ("
                    "SELECT results_channel_id FROM settings "
                    "WHERE settings.guild_id = forms.guild_id) "
                    "WHERE log_channel_id IS NULL"
                )

    @staticmethod
    def _admin_only() -> app_commands.check:
        return app_commands.checks.has_permissions(administrator=True)

    def _find_form(self, guild_id: int, name: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT id, name, log_channel_id FROM forms WHERE guild_id = ? AND name = ?",
                (guild_id, name.strip()),
            ).fetchone()

    def _get_staff_roles(self, form_id: int, action: str) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT role_id FROM form_staff_roles WHERE form_id = ? AND action = ?",
                (form_id, action),
            ).fetchall()
        return [row["role_id"] for row in rows]

    def _is_premium(self, guild_id: int) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM premium_guilds WHERE guild_id = ? "
                "AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)",
                (guild_id,),
            ).fetchone() is not None

    def _limits_for_guild(self, guild_id: int) -> tuple[int, int]:
        if self._is_premium(guild_id):
            return PREMIUM_MAX_FORMS, PREMIUM_MAX_QUESTIONS
        return FREE_MAX_FORMS, FREE_MAX_QUESTIONS

    @staticmethod
    def _private_log_channel(
        channel: discord.TextChannel, allowed_staff_role_ids: set[int]
    ) -> bool:
        if channel.permissions_for(channel.guild.default_role).view_channel:
            return False
        for role in channel.guild.roles:
            if role == channel.guild.default_role or role.permissions.administrator:
                continue
            can_view = channel.permissions_for(role).view_channel
            if role.id in allowed_staff_role_ids and not can_view:
                return False
            if can_view and role.id not in allowed_staff_role_ids:
                return False
        for target, overwrite in channel.overwrites.items():
            if (
                isinstance(target, discord.Member)
                and overwrite.view_channel is True
                and not target.guild_permissions.administrator
                and not allowed_staff_role_ids.intersection(
                    role.id for role in target.roles
                )
            ):
                return False
        return True

    async def cog_load(self) -> None:
        from dashboard import DashboardMessageView

        with self._connect() as connection:
            pending = connection.execute(
                "SELECT id, review_message_id FROM submissions "
                "WHERE status = 'pending' AND review_message_id IS NOT NULL"
            ).fetchall()
            published_forms = connection.execute(
                "SELECT id, name, published_message_id, message_json FROM forms "
                "WHERE published_message_id IS NOT NULL"
            ).fetchall()
        for submission in pending:
            self.bot.add_view(
                DecisionView(self, submission["id"]),
                message_id=submission["review_message_id"],
            )
        for form in published_forms:
            config = json.loads(form["message_json"] or "{}")
            components = config.get("components", [])
            if not any(component.get("action") == "apply" for component in components):
                continue
            self.bot.add_view(
                DashboardMessageView(
                    self, form["id"], form["name"], components
                ),
                message_id=form["published_message_id"],
            )

    async def handle_decision(
        self,
        interaction: discord.Interaction,
        submission_id: int,
        action: str,
        reason: str,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Esta acción solo funciona dentro del servidor.", ephemeral=True)
            return
        with self._connect() as connection:
            submission = connection.execute(
                "SELECT s.*, f.name AS form_name FROM submissions s "
                "JOIN forms f ON f.id = s.form_id "
                "WHERE s.id = ? AND s.guild_id = ?",
                (submission_id, interaction.guild.id),
            ).fetchone()
        if submission is None:
            await interaction.response.send_message("No se encontró esa solicitud.", ephemeral=True)
            return
        if action not in {"accept", "reject"}:
            await interaction.response.send_message("Decisión no válida.", ephemeral=True)
            return
        allowed_roles = set(self._get_staff_roles(submission["form_id"], action))
        member_roles = {role.id for role in interaction.user.roles}
        if not interaction.user.guild_permissions.administrator and not allowed_roles.intersection(member_roles):
            await interaction.response.send_message(
                "No tienes un rol autorizado para esta decisión.", ephemeral=True
            )
            return
        if submission["status"] != "pending":
            await interaction.response.send_message("Esta solicitud ya fue revisada.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        status = "accepted" if action == "accept" else "rejected"
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE submissions SET status = ?, reviewer_id = ?, review_reason = ?, "
                "decided_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status = 'pending'",
                (status, interaction.user.id, reason, submission_id),
            ).rowcount
        if not updated:
            await interaction.followup.send("Otro miembro ya revisó esta solicitud.", ephemeral=True)
            return

        message = interaction.message
        if message is not None:
            embed = message.embeds[0].copy() if message.embeds else discord.Embed()
            embed.color = discord.Color.green() if action == "accept" else discord.Color.dark_red()
            embed.add_field(name="Motivo de revisión", value=reason[:1000], inline=False)
            embed.set_footer(
                text=f"{'Aceptada' if action == 'accept' else 'Rechazada'} por {interaction.user}"
            )
            await message.edit(embed=embed, view=None)
        try:
            applicant = self.bot.get_user(submission["user_id"]) or await self.bot.fetch_user(
                submission["user_id"]
            )
            await applicant.send(
                f"Tu solicitud para **{discord.utils.escape_markdown(submission['form_name'])}** "
                f"en **{discord.utils.escape_markdown(interaction.guild.name)}** ha sido "
                f"{'aceptada' if action == 'accept' else 'rechazada'}.\n"
                f"Motivo: {reason}"
            )
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            f"Solicitud {'aceptada' if action == 'accept' else 'rechazada'}.", ephemeral=True
        )

    @app_commands.guild_only()
    @app_commands.command(name="formulario_crear", description="Crea un formulario de seguridad")
    @app_commands.describe(nombre="Nombre único del formulario")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_form(self, interaction: discord.Interaction, nombre: str) -> None:
        if interaction.guild is None:
            return
        name = nombre.strip()
        if not name or len(name) > 80:
            await interaction.response.send_message(
                "El nombre debe tener entre 1 y 80 caracteres.", ephemeral=True
            )
            return
        with self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM forms WHERE guild_id = ?", (interaction.guild.id,)
            ).fetchone()[0]
            max_forms, _ = self._limits_for_guild(interaction.guild.id)
            if count >= max_forms:
                await interaction.response.send_message(
                    f"Este servidor ya tiene el máximo de {max_forms} formularios.",
                    ephemeral=True,
                )
                return
            try:
                connection.execute(
                    "INSERT INTO forms (guild_id, name) VALUES (?, ?)",
                    (interaction.guild.id, name),
                )
            except sqlite3.IntegrityError:
                await interaction.response.send_message(
                    "Ya existe un formulario con ese nombre.", ephemeral=True
                )
                return
        await interaction.response.send_message(
            f"Formulario **{discord.utils.escape_markdown(name)}** creado. "
            "Añade preguntas con `/formulario_pregunta`.",
            ephemeral=True,
        )

    @app_commands.guild_only()
    @app_commands.command(name="formulario_pregunta", description="Añade una pregunta al formulario")
    @app_commands.describe(nombre="Nombre del formulario", pregunta="Pregunta que recibirá la persona por DM")
    @app_commands.checks.has_permissions(administrator=True)
    async def add_question(
        self, interaction: discord.Interaction, nombre: str, pregunta: str
    ) -> None:
        if interaction.guild is None:
            return
        form = self._find_form(interaction.guild.id, nombre)
        if form is None:
            await interaction.response.send_message("No existe ese formulario.", ephemeral=True)
            return
        prompt = pregunta.strip()
        if not prompt or len(prompt) > 1000:
            await interaction.response.send_message(
                "La pregunta debe tener entre 1 y 1000 caracteres.", ephemeral=True
            )
            return
        with self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM questions WHERE form_id = ?", (form["id"],)
            ).fetchone()[0]
            _, max_questions = self._limits_for_guild(interaction.guild.id)
            if count >= max_questions:
                await interaction.response.send_message(
                    f"El formulario ya tiene el máximo de {max_questions} preguntas.",
                    ephemeral=True,
                )
                return
            connection.execute(
                "INSERT INTO questions (form_id, position, prompt) VALUES (?, ?, ?)",
                (form["id"], count + 1, prompt),
            )
        await interaction.response.send_message(
            f"Pregunta {count + 1} añadida a **{discord.utils.escape_markdown(form['name'])}**.",
            ephemeral=True,
        )

    @app_commands.guild_only()
    @app_commands.command(name="formulario_borrar_pregunta", description="Borra una pregunta por su número")
    @app_commands.describe(nombre="Nombre del formulario", numero="Número de pregunta que se borrará")
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_question(
        self, interaction: discord.Interaction, nombre: str, numero: app_commands.Range[int, 1, 50]
    ) -> None:
        if interaction.guild is None:
            return
        form = self._find_form(interaction.guild.id, nombre)
        if form is None:
            await interaction.response.send_message("No existe ese formulario.", ephemeral=True)
            return
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            rows = connection.execute(
                "SELECT id, position FROM questions WHERE form_id = ? ORDER BY position",
                (form["id"],),
            ).fetchall()
            deleted_id = next(
                (row["id"] for row in rows if row["position"] == numero), None
            )
            if deleted_id is None:
                await interaction.response.send_message(
                    "No hay una pregunta con ese número.", ephemeral=True
                )
                return
            deleted = connection.execute(
                "DELETE FROM questions WHERE id = ?", (deleted_id,)
            ).rowcount
            if deleted:
                remaining_ids = [row["id"] for row in rows if row["id"] != deleted_id]
                _save_question_order(
                    connection, form["id"], remaining_ids
                )
        message = "Pregunta borrada." if deleted else "No hay una pregunta con ese número."
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.guild_only()
    @app_commands.command(name="formulario_borrar", description="Elimina un formulario y sus preguntas")
    @app_commands.describe(nombre="Nombre del formulario que se eliminará")
    @app_commands.checks.has_permissions(administrator=True)
    async def delete_form(self, interaction: discord.Interaction, nombre: str) -> None:
        if interaction.guild is None:
            return
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            deleted = connection.execute(
                "DELETE FROM forms WHERE guild_id = ? AND name = ?",
                (interaction.guild.id, nombre.strip()),
            ).rowcount
        message = "Formulario eliminado." if deleted else "No existe ese formulario."
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.guild_only()
    @app_commands.command(name="formulario_lista", description="Muestra los formularios de este servidor")
    async def list_forms(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        with self._connect() as connection:
            forms = connection.execute(
                "SELECT f.name, COUNT(q.id) AS question_count FROM forms f "
                "LEFT JOIN questions q ON q.form_id = f.id "
                "WHERE f.guild_id = ? GROUP BY f.id ORDER BY f.id",
                (interaction.guild.id,),
            ).fetchall()
        if not forms:
            await interaction.response.send_message("Este servidor aún no tiene formularios.", ephemeral=True)
            return
        _, max_questions = self._limits_for_guild(interaction.guild.id)
        lines = [
            f"**{discord.utils.escape_markdown(form['name'])}**: "
            f"{form['question_count']}/{max_questions} preguntas"
            for form in forms
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.guild_only()
    @app_commands.command(name="formularios_panel", description="Abre la configuración unificada de formularios")
    @app_commands.checks.has_permissions(administrator=True)
    async def forms_panel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        with self._connect() as connection:
            forms = connection.execute(
                "SELECT id, name, log_channel_id FROM forms "
                "WHERE guild_id = ? ORDER BY id",
                (interaction.guild.id,),
            ).fetchall()
        if not forms:
            await interaction.response.send_message(
                "Crea un formulario primero con `/formulario_crear`.", ephemeral=True
            )
            return
        panel = FormConfigPanel(
            self,
            interaction.guild.id,
            interaction.user.id,
            forms,
            forms[0]["id"],
        )
        await interaction.response.send_message(view=panel, ephemeral=True)

    @app_commands.guild_only()
    @app_commands.command(name="formularios_historial", description="Consulta las últimas solicitudes revisadas")
    @app_commands.describe(nombre="Filtrar por un formulario (opcional)")
    @app_commands.checks.has_permissions(administrator=True)
    async def forms_history(
        self, interaction: discord.Interaction, nombre: str | None = None
    ) -> None:
        if interaction.guild is None:
            return
        query = (
            "SELECT s.id, s.user_id, s.status, s.reviewer_id, s.review_reason, "
            "s.created_at, s.decided_at, f.name AS form_name "
            "FROM submissions s JOIN forms f ON f.id = s.form_id "
            "WHERE s.guild_id = ?"
        )
        parameters: list[object] = [interaction.guild.id]
        if nombre:
            query += " AND f.name = ? COLLATE NOCASE"
            parameters.append(nombre.strip())
        query += " ORDER BY s.id DESC LIMIT 12"
        with self._connect() as connection:
            history = connection.execute(query, parameters).fetchall()
        if not history:
            await interaction.response.send_message(
                "No hay solicitudes que coincidan con ese filtro.", ephemeral=True
            )
            return
        status_labels = {
            "pending": "Pendiente",
            "accepted": "Aceptada",
            "rejected": "Rechazada",
            "delivery_failed": "Error al enviar",
            "archived": "Archivada",
        }
        lines = []
        for item in history:
            reason = (item["review_reason"] or "Sin motivo")[:180]
            reviewer = f" · staff `{item['reviewer_id']}`" if item["reviewer_id"] else ""
            lines.append(
                f"**#{item['id']} · {discord.utils.escape_markdown(item['form_name'])}** — "
                f"{status_labels.get(item['status'], item['status'])}\n"
                f"Usuario `{item['user_id']}`{reviewer} · Motivo: {reason}"
            )
        await interaction.response.send_message("\n\n".join(lines), ephemeral=True)

    @app_commands.guild_only()
    @app_commands.command(name="premium_exportar", description="Exporta solicitudes del servidor en JSON")
    @app_commands.checks.has_permissions(administrator=True)
    async def premium_export(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        if not self._is_premium(interaction.guild.id):
            await interaction.response.send_message(
                "Esta exportación está disponible solo para servidores con premium activo.",
                ephemeral=True,
            )
            return
        with self._connect() as connection:
            submissions = connection.execute(
                "SELECT s.id, f.name AS form_name, s.user_id, s.answers_json, s.status, "
                "s.reviewer_id, s.review_reason, s.created_at, s.decided_at "
                "FROM submissions s JOIN forms f ON f.id = s.form_id "
                "WHERE s.guild_id = ? ORDER BY s.id",
                (interaction.guild.id,),
            ).fetchall()
        export_data = {
            "guild_id": interaction.guild.id,
            "guild_name": interaction.guild.name,
            "exported_at": discord.utils.utcnow().isoformat(),
            "submissions": [
                {
                    "id": row["id"],
                    "form": row["form_name"],
                    "user_id": row["user_id"],
                    "answers": json.loads(row["answers_json"]),
                    "status": row["status"],
                    "reviewer_id": row["reviewer_id"],
                    "review_reason": row["review_reason"],
                    "created_at": row["created_at"],
                    "decided_at": row["decided_at"],
                }
                for row in submissions
            ],
        }
        payload = json.dumps(export_data, ensure_ascii=False, indent=2).encode("utf-8")
        file = discord.File(
            io.BytesIO(payload),
            filename=f"sentra-solicitudes-{interaction.guild.id}.json",
        )
        await interaction.response.send_message(
            content=f"Exportadas {len(submissions)} solicitudes.",
            file=file,
            ephemeral=True,
        )

    @app_commands.guild_only()
    @app_commands.command(name="formulario_aplicar", description="Responde un formulario por mensaje directo")
    @app_commands.describe(nombre="Nombre del formulario al que quieres responder")
    async def apply_form(self, interaction: discord.Interaction, nombre: str) -> None:
        await self.start_application(interaction, nombre)

    async def start_application(self, interaction: discord.Interaction, nombre: str) -> None:
        if interaction.guild is None or interaction.user.bot:
            return
        form = self._find_form(interaction.guild.id, nombre)
        if form is None:
            await interaction.response.send_message("No existe ese formulario.", ephemeral=True)
            return
        with self._connect() as connection:
            questions = connection.execute(
                "SELECT prompt FROM questions WHERE form_id = ? ORDER BY position",
                (form["id"],),
            ).fetchall()
        if not questions:
            await interaction.response.send_message("Este formulario aún no tiene preguntas.", ephemeral=True)
            return
        allowed_staff_role_ids = set(self._get_staff_roles(form["id"], "accept"))
        allowed_staff_role_ids.update(self._get_staff_roles(form["id"], "reject"))
        channel = interaction.guild.get_channel(form["log_channel_id"]) if form["log_channel_id"] else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Un administrador debe configurar el formulario con `/formularios_panel`.",
                ephemeral=True,
            )
            return
        bot_permissions = channel.permissions_for(interaction.guild.me)
        if (
            not bot_permissions.view_channel
            or not bot_permissions.send_messages
            or not bot_permissions.embed_links
            or not self._private_log_channel(channel, allowed_staff_role_ids)
        ):
            await interaction.response.send_message(
                "El canal de logs ya no cumple los permisos privados requeridos. Avísale a un administrador.",
                ephemeral=True,
            )
            return
        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                f"Formulario **{discord.utils.escape_markdown(form['name'])}** de "
                f"**{discord.utils.escape_markdown(interaction.guild.name)}**.\n"
                "Responde a cada pregunta en este chat. Tienes 5 minutos por respuesta. "
                "Escribe `cancelar` para detenerlo."
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "No puedo enviarte un DM. Activa los mensajes directos de miembros del servidor e inténtalo de nuevo.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("Te he enviado el formulario por DM.", ephemeral=True)

        answers: list[tuple[str, str]] = []
        for number, question in enumerate(questions, start=1):
            await dm.send(f"**{number}/{len(questions)}.** {question['prompt']}")

            def check(message: discord.Message) -> bool:
                return message.author.id == interaction.user.id and message.channel.id == dm.id

            try:
                answer_message = await self.bot.wait_for("message", check=check, timeout=DM_TIMEOUT)
            except asyncio.TimeoutError:
                await dm.send("Se acabó el tiempo y el formulario no se ha enviado. Puedes volver a empezarlo cuando quieras.")
                return
            answer = answer_message.content.strip()
            if answer.casefold() == "cancelar":
                await dm.send("Formulario cancelado. No se ha enviado ninguna respuesta.")
                return
            if not answer:
                await dm.send("La respuesta no puede estar vacía. Vuelve a iniciar el formulario para responderlo.")
                return
            answers.append((question["prompt"], answer))

        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO submissions (guild_id, form_id, user_id, answers_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    interaction.guild.id,
                    form["id"],
                    interaction.user.id,
                    json.dumps(answers, ensure_ascii=False),
                ),
            )
            submission_id = cursor.lastrowid

        embeds: list[discord.Embed] = []
        fields_per_page = 4
        for page_start in range(0, len(answers), fields_per_page):
            embed = discord.Embed(
                title=f"Nueva respuesta: {form['name']}",
                color=discord.Color.red(),
                description=(
                    f"Servidor: {discord.utils.escape_markdown(interaction.guild.name)}\n"
                    f"Usuario: {discord.utils.escape_markdown(str(interaction.user))} "
                    f"(`{interaction.user.id}`)"
                ) if page_start == 0 else None,
            )
            for index, (prompt, answer) in enumerate(
                answers[page_start : page_start + fields_per_page], start=page_start + 1
            ):
                embed.add_field(
                    name=f"{index}. {prompt[:200]}",
                    value=answer[:1000],
                    inline=False,
                )
            if page_start == 0:
                embed.set_footer(text=f"Pendiente de revisión • Solicitud #{submission_id}")
            else:
                embed.set_footer(
                    text=f"Solicitud #{submission_id} • Página "
                    f"{page_start // fields_per_page + 1}/"
                    f"{(len(answers) + fields_per_page - 1) // fields_per_page}"
                )
            embeds.append(embed)
        try:
            review_message = None
            for index, embed in enumerate(embeds):
                message = await channel.send(
                    embed=embed,
                    view=DecisionView(self, submission_id) if index == 0 else None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                if index == 0:
                    review_message = message
        except discord.Forbidden:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE submissions SET status = 'delivery_failed' WHERE id = ?",
                    (submission_id,),
                )
            await dm.send("No se pudieron guardar las respuestas: el bot perdió acceso al canal configurado.")
            return
        if review_message is not None:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE submissions SET review_message_id = ? WHERE id = ?",
                    (review_message.id, submission_id),
                )
        await dm.send("Gracias. El formulario se ha enviado correctamente.")

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.errors.MissingPermissions):
            message = "Solo un administrador de este servidor puede usar ese comando."
        elif isinstance(error, app_commands.CheckFailure):
            message = "Solo el owner configurado del bot puede gestionar premium."
        else:
            message = "No se pudo completar el comando. Comprueba la configuración del bot e inténtalo de nuevo."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class FormConfigPanel(discord.ui.LayoutView):
    def __init__(
        self,
        cog: Formularios,
        guild_id: int,
        owner_id: int,
        forms: list[sqlite3.Row],
        selected_form_id: int,
        question_page: int = 0,
        selected_question_position: int | None = None,
    ) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.forms = forms
        self.selected_form_id = selected_form_id
        self.question_page = question_page
        self.form = next(
            form for form in forms if form["id"] == selected_form_id
        )
        max_forms, max_questions = cog._limits_for_guild(guild_id)
        with cog._connect() as connection:
            question_count = connection.execute(
                "SELECT COUNT(*) FROM questions WHERE form_id = ?",
                (selected_form_id,),
            ).fetchone()[0]
            questions = connection.execute(
                "SELECT position, prompt FROM questions WHERE form_id = ? ORDER BY position",
                (selected_form_id,),
            ).fetchall()
        self.selected_question_position = selected_question_position
        if (
            self.selected_question_position is None
            or not any(
                question["position"] == self.selected_question_position
                for question in questions
            )
        ):
            self.selected_question_position = questions[0]["position"] if questions else None
        premium = cog._is_premium(guild_id)
        guild = cog.bot.get_guild(guild_id)
        channel = guild.get_channel(self.form["log_channel_id"]) if guild else None
        accept_roles = [
            role
            for role_id in cog._get_staff_roles(selected_form_id, "accept")
            if guild and (role := guild.get_role(role_id)) is not None
        ]
        reject_roles = [
            role
            for role_id in cog._get_staff_roles(selected_form_id, "reject")
            if guild and (role := guild.get_role(role_id)) is not None
        ]
        log_channel_text = channel.mention if isinstance(channel, discord.TextChannel) else "Sin configurar"
        accept_text = ", ".join(role.mention for role in accept_roles) or "Sin roles"
        reject_text = ", ".join(role.mention for role in reject_roles) or "Sin roles"
        if self.selected_question_position is None:
            selected_question_text = "Aún no hay preguntas."
        else:
            selected_question = next(
                question for question in questions
                if question["position"] == self.selected_question_position
            )
            selected_question_text = (
                f"**Pregunta {selected_question['position']}:** "
                f"{discord.utils.escape_markdown(selected_question['prompt'])[:900]}"
            )

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# SENTRA SECURITY · CONFIGURACIÓN DE FORMULARIOS"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    f"**Formulario:** {discord.utils.escape_markdown(self.form['name'])}\n"
                    f"**Plan:** {'PREMIUM' if premium else 'GRATUITO'} · "
                    f"{len(forms)}/{max_forms} formularios · "
                    f"{question_count}/{max_questions} preguntas\n"
                    f"**Canal de logs:** {log_channel_text}\n"
                    f"**Pueden aceptar:** {accept_text}\n"
                    f"**Pueden rechazar:** {reject_text}"
                ),
                discord.ui.ActionRow(FormOptionSelect(self, forms)),
                discord.ui.TextDisplay("**Preguntas por DM**"),
                discord.ui.ActionRow(
                    FormQuestionSelect(
                        self,
                        questions,
                        question_page,
                        self.selected_question_position,
                    )
                ),
                discord.ui.TextDisplay(selected_question_text),
                discord.ui.ActionRow(
                    AddQuestionButton(self),
                    EditQuestionButton(self),
                    DeleteQuestionButton(self),
                    MoveQuestionButton(self, -1),
                    MoveQuestionButton(self, 1),
                ),
                discord.ui.TextDisplay("**Canal privado para los logs de este formulario**"),
                discord.ui.ActionRow(LogChannelSelect(self, channel)),
                discord.ui.TextDisplay("**Roles autorizados para aceptar solicitudes**"),
                discord.ui.ActionRow(StaffRoleSelect(self, "accept", accept_roles)),
                discord.ui.TextDisplay("**Roles autorizados para rechazar solicitudes**"),
                discord.ui.ActionRow(StaffRoleSelect(self, "reject", reject_roles)),
                accent_colour=discord.Color.red(),
            )
        )

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id and interaction.guild_id == self.guild_id:
            return True
        await interaction.response.send_message(
            "Este panel pertenece a otra persona o servidor.", ephemeral=True
        )
        return False

    async def refresh(
        self,
        interaction: discord.Interaction,
        form_id: int | None = None,
        question_page: int | None = None,
        selected_question_position: int | None = None,
    ) -> None:
        if not await self._check_owner(interaction):
            return
        with self.cog._connect() as connection:
            forms = connection.execute(
                "SELECT id, name, log_channel_id FROM forms "
                "WHERE guild_id = ? ORDER BY id",
                (self.guild_id,),
            ).fetchall()
        if not forms:
            await interaction.response.edit_message(view=None, content="No quedan formularios en este servidor.")
            return
        next_form_id = form_id or self.selected_form_id
        if not any(form["id"] == next_form_id for form in forms):
            next_form_id = forms[0]["id"]
        next_page = self.question_page if question_page is None else question_page
        next_selected_position = (
            self.selected_question_position
            if selected_question_position is None
            else selected_question_position
        )
        await interaction.response.edit_message(
            view=FormConfigPanel(
                self.cog,
                self.guild_id,
                self.owner_id,
                forms,
                next_form_id,
                next_page,
                next_selected_position if next_form_id == self.selected_form_id else None,
            ),
            content=None,
            embeds=[],
        )

    async def reorder_question(
        self, interaction: discord.Interaction, offset: int
    ) -> None:
        if not await self._check_owner(interaction):
            return
        if self.selected_question_position is None:
            await interaction.response.send_message("Selecciona una pregunta primero.", ephemeral=True)
            return
        with self.cog._connect() as connection:
            rows = connection.execute(
                "SELECT id, position FROM questions WHERE form_id = ? ORDER BY position",
                (self.selected_form_id,),
            ).fetchall()
            source_index = next(
                index for index, row in enumerate(rows)
                if row["position"] == self.selected_question_position
            )
            destination_index = source_index + offset
            if not 0 <= destination_index < len(rows):
                await interaction.response.send_message(
                    "La pregunta ya está en ese extremo del formulario.", ephemeral=True
                )
                return
            ordered_ids = [row["id"] for row in rows]
            ordered_ids[source_index], ordered_ids[destination_index] = (
                ordered_ids[destination_index], ordered_ids[source_index]
            )
            _save_question_order(connection, self.selected_form_id, ordered_ids)
        await self.refresh(
            interaction, selected_question_position=destination_index + 1
        )

    async def delete_selected_question(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        if self.selected_question_position is None:
            await interaction.response.send_message("Selecciona una pregunta primero.", ephemeral=True)
            return
        with self.cog._connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            rows = connection.execute(
                "SELECT id, position FROM questions WHERE form_id = ? ORDER BY position",
                (self.selected_form_id,),
            ).fetchall()
            deleted = next(
                (row for row in rows if row["position"] == self.selected_question_position),
                None,
            )
            if deleted is None:
                await interaction.response.send_message("Esa pregunta ya no existe.", ephemeral=True)
                return
            connection.execute("DELETE FROM questions WHERE id = ?", (deleted["id"],))
            remaining_ids = [row["id"] for row in rows if row["id"] != deleted["id"]]
            _save_question_order(connection, self.selected_form_id, remaining_ids)
        next_position = min(self.selected_question_position, len(remaining_ids)) or None
        await self.refresh(
            interaction,
            question_page=max((next_position - 1) // 24, 0) if next_position else 0,
            selected_question_position=next_position,
        )

    async def save_question(
        self, interaction: discord.Interaction, prompt: str, edit: bool
    ) -> None:
        if not await self._check_owner(interaction):
            return
        prompt = prompt.strip()
        if not prompt or len(prompt) > 1000:
            await interaction.response.send_message(
                "La pregunta debe tener entre 1 y 1000 caracteres.", ephemeral=True
            )
            return
        with self.cog._connect() as connection:
            if edit:
                if self.selected_question_position is None:
                    await interaction.response.send_message(
                        "Selecciona una pregunta para editar.", ephemeral=True
                    )
                    return
                result = connection.execute(
                    "UPDATE questions SET prompt = ? WHERE form_id = ? AND position = ?",
                    (prompt, self.selected_form_id, self.selected_question_position),
                )
                if not result.rowcount:
                    await interaction.response.send_message("Esa pregunta ya no existe.", ephemeral=True)
                    return
                next_position = self.selected_question_position
            else:
                count = connection.execute(
                    "SELECT COUNT(*) FROM questions WHERE form_id = ?",
                    (self.selected_form_id,),
                ).fetchone()[0]
                _, max_questions = self.cog._limits_for_guild(self.guild_id)
                if count >= max_questions:
                    await interaction.response.send_message(
                        f"El formulario ha alcanzado su límite de {max_questions} preguntas.",
                        ephemeral=True,
                    )
                    return
                next_position = count + 1
                connection.execute(
                    "INSERT INTO questions (form_id, position, prompt) VALUES (?, ?, ?)",
                    (self.selected_form_id, next_position, prompt),
                )
        await self.refresh(
            interaction,
            question_page=(next_position - 1) // 24,
            selected_question_position=next_position,
        )

    async def set_log_channel(
        self, interaction: discord.Interaction, channel_id: int | None
    ) -> None:
        if not await self._check_owner(interaction):
            return
        if interaction.guild is None:
            return
        if channel_id is not None:
            channel = interaction.guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message(
                    "Selecciona un canal de texto de este servidor.", ephemeral=True
                )
                return
            bot_member = interaction.guild.me
            permissions = channel.permissions_for(bot_member)
            if (
                not permissions.view_channel
                or not permissions.send_messages
                or not permissions.embed_links
            ):
                await interaction.response.send_message(
                    "El bot necesita ver el canal y poder enviar mensajes y enlaces incrustados.",
                    ephemeral=True,
                )
                return
            allowed_staff_role_ids = set(
                self.cog._get_staff_roles(self.selected_form_id, "accept")
            )
            allowed_staff_role_ids.update(
                self.cog._get_staff_roles(self.selected_form_id, "reject")
            )
            if not self.cog._private_log_channel(channel, allowed_staff_role_ids):
                await interaction.response.send_message(
                    "Cierra el canal a @everyone y permite verlo solo a administradores y al staff de este formulario.",
                    ephemeral=True,
                )
                return
        with self.cog._connect() as connection:
            connection.execute(
                "UPDATE forms SET log_channel_id = ? WHERE id = ? AND guild_id = ?",
                (channel_id, self.selected_form_id, self.guild_id),
            )
        await self.refresh(interaction)

    async def set_staff_roles(
        self, interaction: discord.Interaction, action: str, roles: list[discord.Role]
    ) -> None:
        if not await self._check_owner(interaction):
            return
        if action not in {"accept", "reject"}:
            await interaction.response.send_message("Acción no válida.", ephemeral=True)
            return
        invalid_roles = [
            role for role in roles
            if role.is_default() or role.managed or role.guild.id != self.guild_id
        ]
        if invalid_roles:
            await interaction.response.send_message(
                "No se pueden asignar @everyone ni roles gestionados por integraciones.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        with self.cog._connect() as connection:
            form = connection.execute(
                "SELECT log_channel_id FROM forms WHERE id = ? AND guild_id = ?",
                (self.selected_form_id, self.guild_id),
            ).fetchone()
        if guild is not None and form and form["log_channel_id"]:
            channel = guild.get_channel(form["log_channel_id"])
            if isinstance(channel, discord.TextChannel):
                other_action = "reject" if action == "accept" else "accept"
                allowed_staff_role_ids = {
                    role.id for role in roles
                }
                allowed_staff_role_ids.update(
                    self.cog._get_staff_roles(self.selected_form_id, other_action)
                )
                if not self.cog._private_log_channel(channel, allowed_staff_role_ids):
                    await interaction.response.send_message(
                        "Ajusta los permisos del canal para @everyone y los roles staff seleccionados antes de guardarlos.",
                        ephemeral=True,
                    )
                    return
        with self.cog._connect() as connection:
            connection.execute(
                "DELETE FROM form_staff_roles WHERE form_id = ? AND action = ?",
                (self.selected_form_id, action),
            )
            connection.executemany(
                "INSERT INTO form_staff_roles (form_id, action, role_id) VALUES (?, ?, ?)",
                [(self.selected_form_id, action, role.id) for role in roles],
            )
        await self.refresh(interaction)


class FormOptionSelect(discord.ui.Select):
    def __init__(self, panel: FormConfigPanel, forms: list[sqlite3.Row]) -> None:
        self.panel = panel
        options = [
            discord.SelectOption(
                label=discord.utils.escape_markdown(form["name"])[:100],
                value=str(form["id"]),
                default=form["id"] == panel.selected_form_id,
            )
            for form in forms
        ]
        super().__init__(
            placeholder="Selecciona el formulario que quieres configurar",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.refresh(interaction, int(self.values[0]))


def _save_question_order(
    connection: sqlite3.Connection, form_id: int, question_ids: list[int]
) -> None:
    connection.executemany(
        "UPDATE questions SET position = ? WHERE id = ? AND form_id = ?",
        [(-index, question_id, form_id) for index, question_id in enumerate(question_ids, 1)],
    )
    connection.executemany(
        "UPDATE questions SET position = ? WHERE id = ? AND form_id = ?",
        [(index, question_id, form_id) for index, question_id in enumerate(question_ids, 1)],
    )


class FormQuestionSelect(discord.ui.Select):
    PAGE_SIZE = 23

    def __init__(
        self,
        panel: FormConfigPanel,
        questions: list[sqlite3.Row],
        page: int,
        selected_position: int | None,
    ) -> None:
        self.panel = panel
        self.page = page
        start = page * self.PAGE_SIZE
        page_questions = questions[start : start + self.PAGE_SIZE]
        options: list[discord.SelectOption] = []
        if page > 0:
            options.append(
                discord.SelectOption(label="Preguntas anteriores...", value="page:previous")
            )
        options.extend(
            discord.SelectOption(
                label=f"{question['position']}. "
                f"{discord.utils.escape_markdown(question['prompt'])[:85]}",
                value=f"question:{question['position']}",
                default=question["position"] == selected_position,
            )
            for question in page_questions
        )
        if start + self.PAGE_SIZE < len(questions):
            options.append(
                discord.SelectOption(label="Más preguntas...", value="page:next")
            )
        if not options:
            options.append(discord.SelectOption(label="Sin preguntas", value="empty"))
        super().__init__(
            placeholder=f"Selecciona una pregunta · página {page + 1}",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0]
        if selected == "page:previous":
            await self.panel.refresh(
                interaction,
                question_page=max(self.page - 1, 0),
            )
        elif selected == "page:next":
            await self.panel.refresh(interaction, question_page=self.page + 1)
        elif selected.startswith("question:"):
            position = int(selected.partition(":")[2])
            await self.panel.refresh(
                interaction,
                question_page=self.page,
                selected_question_position=position,
            )
        else:
            await interaction.response.defer()


class QuestionModal(discord.ui.Modal):
    def __init__(self, panel: FormConfigPanel, *, edit: bool) -> None:
        super().__init__(
            title="Editar pregunta" if edit else "Añadir pregunta",
            timeout=300,
        )
        self.panel = panel
        self.edit = edit
        default = None
        if edit and panel.selected_question_position is not None:
            with panel.cog._connect() as connection:
                question = connection.execute(
                    "SELECT prompt FROM questions WHERE form_id = ? AND position = ?",
                    (panel.selected_form_id, panel.selected_question_position),
                ).fetchone()
            default = question["prompt"] if question else None
        self.prompt = discord.ui.TextInput(
            label="Pregunta",
            style=discord.TextStyle.paragraph,
            placeholder="Escribe lo que se preguntará por mensaje directo",
            default=default,
            min_length=1,
            max_length=1000,
        )
        self.add_item(self.prompt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.panel.save_question(interaction, self.prompt.value, self.edit)


class AddQuestionButton(discord.ui.Button):
    def __init__(self, panel: FormConfigPanel) -> None:
        self.panel = panel
        super().__init__(label="Añadir", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        if await self.panel._check_owner(interaction):
            await interaction.response.send_modal(QuestionModal(self.panel, edit=False))


class EditQuestionButton(discord.ui.Button):
    def __init__(self, panel: FormConfigPanel) -> None:
        self.panel = panel
        super().__init__(label="Editar", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.panel.selected_question_position is None:
            await interaction.response.send_message("Selecciona una pregunta primero.", ephemeral=True)
        elif await self.panel._check_owner(interaction):
            await interaction.response.send_modal(QuestionModal(self.panel, edit=True))


class DeleteQuestionButton(discord.ui.Button):
    def __init__(self, panel: FormConfigPanel) -> None:
        self.panel = panel
        super().__init__(label="Borrar", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.delete_selected_question(interaction)


class MoveQuestionButton(discord.ui.Button):
    def __init__(self, panel: FormConfigPanel, offset: int) -> None:
        self.panel = panel
        self.offset = offset
        super().__init__(
            label="Subir" if offset < 0 else "Bajar",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.reorder_question(interaction, self.offset)


class LogChannelSelect(discord.ui.ChannelSelect):
    def __init__(
        self, panel: FormConfigPanel, channel: discord.TextChannel | None
    ) -> None:
        self.panel = panel
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Elige o quita el canal de logs",
            min_values=0,
            max_values=1,
            default_values=[channel] if channel else [],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        channel_id = self.values[0].id if self.values else None
        await self.panel.set_log_channel(interaction, channel_id)


class StaffRoleSelect(discord.ui.RoleSelect):
    def __init__(
        self, panel: FormConfigPanel, action: str, roles: list[discord.Role]
    ) -> None:
        self.panel = panel
        self.action = action
        super().__init__(
            placeholder="Selecciona los roles autorizados (vacío = solo administradores)",
            min_values=0,
            max_values=25,
            default_values=roles,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.set_staff_roles(interaction, self.action, list(self.values))


class DecisionButton(discord.ui.Button):
    def __init__(self, cog: Formularios, submission_id: int, action: str) -> None:
        self.cog = cog
        self.submission_id = submission_id
        self.action = action
        accepted = action == "accept"
        super().__init__(
            label="Aceptar" if accepted else "Rechazar",
            style=discord.ButtonStyle.success if accepted else discord.ButtonStyle.danger,
            custom_id=f"sentra:decision:{submission_id}:{action}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        with self.cog._connect() as connection:
            submission = connection.execute(
                "SELECT form_id FROM submissions WHERE id = ?", (self.submission_id,)
            ).fetchone()
        if submission is None:
            await interaction.response.send_message("No se encontró esa solicitud.", ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Esta acción solo funciona dentro del servidor.", ephemeral=True)
            return
        allowed_roles = set(self.cog._get_staff_roles(submission["form_id"], self.action))
        member_roles = {role.id for role in interaction.user.roles}
        if (
            not interaction.user.guild_permissions.administrator
            and not allowed_roles.intersection(member_roles)
        ):
            await interaction.response.send_message(
                "No tienes un rol autorizado para esta decisión.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            DecisionReasonModal(self.cog, self.submission_id, self.action)
        )


class DecisionReasonModal(discord.ui.Modal):
    def __init__(self, cog: Formularios, submission_id: int, action: str) -> None:
        super().__init__(
            title="Motivo de aceptación" if action == "accept" else "Motivo de rechazo",
            timeout=300,
        )
        self.cog = cog
        self.submission_id = submission_id
        self.action = action
        self.reason = discord.ui.TextInput(
            label="Motivo que verá la persona solicitante",
            style=discord.TextStyle.paragraph,
            min_length=3,
            max_length=1000,
            placeholder="Explica brevemente la decisión",
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_decision(
            interaction,
            self.submission_id,
            self.action,
            self.reason.value.strip(),
        )


class DecisionView(discord.ui.View):
    def __init__(self, cog: Formularios, submission_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(DecisionButton(cog, submission_id, "accept"))
        self.add_item(DecisionButton(cog, submission_id, "reject"))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Formularios(bot, Path("data/formularios.sqlite3")))