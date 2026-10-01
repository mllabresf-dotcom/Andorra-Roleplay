import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
import zipfile
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import aiohttp
import discord
from aiohttp import web


log = logging.getLogger("sentra.dashboard")
MAX_UPLOAD_BYTES = 24_500_000
MAX_BACKUP_BYTES = 100_000_000
MAX_MESSAGE_CONTENT = 2000
MAX_EMBEDS = 10
MAX_EMBED_TEXT = 6000
MAX_COMPONENTS = 5
HEX_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")
SAFE_ASSET_ID = re.compile(r"^[0-9a-f]{32}$")


class Dashboard:
    def __init__(self, bot: discord.Client, database_path: Path, root: Path) -> None:
        self.bot = bot
        self.database_path = database_path
        self.root = root
        self.uploads_root = root / "data" / "uploads"
        self.uploads_root.mkdir(parents=True, exist_ok=True)
        self.host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
        self.port = int(os.getenv("DASHBOARD_PORT", "8080"))
        self.client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
        self.redirect_uri = os.getenv(
            "DASHBOARD_REDIRECT_URI",
            f"http://localhost:{self.port}/auth/callback",
        )
        self.cookie_secure = os.getenv("DASHBOARD_COOKIE_SECURE", "false").lower() == "true"
        self.sessions: dict[str, dict[str, Any]] = {}
        self.oauth_states: dict[str, float] = {}
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.app = web.Application(client_max_size=MAX_BACKUP_BYTES + 1024 * 1024)
        self.app.add_routes(
            [
                web.get("/", self.index),
                web.get("/dashboard.css", self.dashboard_css),
                web.get("/dashboard.js", self.dashboard_js),
                web.get("/assets/{filename}", self.asset),
                web.get("/auth/login", self.login),
                web.get("/auth/callback", self.callback),
                web.post("/auth/logout", self.logout),
                web.get("/api/session", self.current_session),
                web.get("/api/guilds", self.list_guilds),
                web.get("/api/guilds/{guild_id}/options", self.guild_options),
                web.get("/api/guilds/{guild_id}/forms", self.list_forms),
                web.get(
                    "/api/guilds/{guild_id}/forms/{form_id}/submissions",
                    self.list_submissions,
                ),
                web.post("/api/guilds/{guild_id}/forms", self.create_form),
                web.get("/api/guilds/{guild_id}/forms/{form_id}", self.get_form),
                web.put("/api/guilds/{guild_id}/forms/{form_id}", self.save_form),
                web.delete("/api/guilds/{guild_id}/forms/{form_id}", self.delete_form),
                web.post(
                    "/api/guilds/{guild_id}/forms/{form_id}/attachments",
                    self.upload_attachments,
                ),
                web.post("/api/guilds/{guild_id}/forms/{form_id}/publish", self.publish_form),
                web.get("/api/guilds/{guild_id}/backup", self.create_backup),
                web.post("/api/guilds/{guild_id}/backup", self.restore_backup),
            ]
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=log)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        log.info("Dashboard disponible en http://%s:%s", self.host, self.port)

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None

    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self.root / "app" / "index.html")

    async def dashboard_css(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self.root / "app" / "dashboard.css")

    async def dashboard_js(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self.root / "app" / "dashboard.js")

    async def asset(self, request: web.Request) -> web.StreamResponse:
        filename = request.match_info["filename"]
        if filename not in {"dashboard.css", "dashboard.js"}:
            raise web.HTTPNotFound()
        return web.FileResponse(self.root / "app" / filename)

    def _oauth_configuration_ready(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def login(self, request: web.Request) -> web.StreamResponse:
        if not self._oauth_configuration_ready():
            raise web.HTTPServiceUnavailable(
                text="Faltan DISCORD_CLIENT_ID y DISCORD_CLIENT_SECRET en .env."
            )
        state = secrets.token_urlsafe(32)
        self.oauth_states[state] = time.time() + 600
        query = urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": "identify guilds",
                "state": state,
                "prompt": "consent",
            }
        )
        response = web.HTTPFound(f"https://discord.com/oauth2/authorize?{query}")
        response.set_cookie(
            "sentra_oauth_state",
            state,
            max_age=600,
            httponly=True,
            secure=self.cookie_secure,
            samesite="Lax",
            path="/auth/callback",
        )
        raise response

    async def callback(self, request: web.Request) -> web.StreamResponse:
        code = request.query.get("code")
        state = request.query.get("state")
        expires_at = self.oauth_states.pop(state or "", 0)
        state_cookie = request.cookies.get("sentra_oauth_state")
        if not code or not state or state != state_cookie or expires_at < time.time():
            raise web.HTTPBadRequest(text="La autorización caducó o no coincide. Vuelve a iniciar sesión.")
        if not self._oauth_configuration_ready():
            raise web.HTTPServiceUnavailable(text="Falta configurar OAuth de Discord en .env.")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://discord.com/api/oauth2/token",
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": self.redirect_uri,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as response:
                    if response.status != 200:
                        log.warning("OAuth token exchange failed with status %s", response.status)
                        raise web.HTTPUnauthorized(text="Discord rechazó el inicio de sesión.")
                    token_data = await response.json()
                access_token = token_data["access_token"]
                async with session.get(
                    "https://discord.com/api/users/@me",
                    headers={"Authorization": f"Bearer {access_token}"},
                ) as response:
                    if response.status != 200:
                        raise web.HTTPUnauthorized(text="No se pudo verificar tu cuenta de Discord.")
                    user = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            log.warning("OAuth request failed: %s", error)
            raise web.HTTPBadGateway(text="Discord no está disponible ahora. Vuelve a probar.") from error

        session_id = secrets.token_urlsafe(32)
        self.sessions[session_id] = {
            "access_token": access_token,
            "user": {
                "id": str(user["id"]),
                "username": user.get("global_name") or user["username"],
                "avatar": user.get("avatar"),
            },
            "expires_at": time.time() + min(int(token_data.get("expires_in", 3600)), 3600),
            "guild_cache_at": 0,
            "admin_guild_ids": set(),
        }
        response = web.HTTPFound("/")
        response.set_cookie(
            "sentra_session",
            session_id,
            max_age=3600,
            httponly=True,
            secure=self.cookie_secure,
            samesite="Lax",
            path="/",
        )
        response.del_cookie("sentra_oauth_state", path="/auth/callback")
        raise response

    async def logout(self, request: web.Request) -> web.Response:
        self._check_origin(request)
        session_id = request.cookies.get("sentra_session")
        if session_id:
            self.sessions.pop(session_id, None)
        response = web.json_response({"ok": True})
        response.del_cookie("sentra_session", path="/")
        return response

    def _session(self, request: web.Request) -> dict[str, Any]:
        self._check_origin(request)
        session_id = request.cookies.get("sentra_session")
        session = self.sessions.get(session_id or "")
        if session is None or session["expires_at"] <= time.time():
            if session_id:
                self.sessions.pop(session_id, None)
            raise web.HTTPUnauthorized(text="Inicia sesión con Discord para continuar.")
        return session

    @staticmethod
    def _check_origin(request: web.Request) -> None:
        if request.method not in {"POST", "PUT", "DELETE"}:
            return
        origin = request.headers.get("Origin")
        if not origin or urlparse(origin).netloc.lower() != request.host.lower():
            raise web.HTTPForbidden(text="Origen de solicitud no permitido.")

    async def current_session(self, request: web.Request) -> web.Response:
        try:
            session = self._session(request)
        except web.HTTPUnauthorized:
            return web.json_response({"authenticated": False})
        return web.json_response({"authenticated": True, "user": session["user"]})

    async def _oauth_guilds(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        if time.time() - session["guild_cache_at"] < 60:
            return session["guilds"]
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    "https://discord.com/api/users/@me/guilds",
                    headers={"Authorization": f"Bearer {session['access_token']}"},
                ) as response:
                    if response.status != 200:
                        raise web.HTTPUnauthorized(text="Tu sesión de Discord ha caducado.")
                    guilds = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            log.warning("Could not load OAuth guild list: %s", error)
            raise web.HTTPBadGateway(text="No se pudieron consultar tus servidores en Discord.") from error
        session["guilds"] = guilds
        session["guild_cache_at"] = time.time()
        session["admin_guild_ids"] = {
            int(guild["id"])
            for guild in guilds
            if guild.get("owner") or int(guild.get("permissions", "0")) & 0x8
        }
        return guilds

    async def _authorized_guild(
        self, request: web.Request
    ) -> tuple[dict[str, Any], discord.Guild]:
        session = self._session(request)
        try:
            guild_id = int(request.match_info["guild_id"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="ID de servidor inválido.")
        await self._oauth_guilds(session)
        if guild_id not in session["admin_guild_ids"]:
            raise web.HTTPForbidden(text="Necesitas ser owner o administrador del servidor.")
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise web.HTTPForbidden(text="El bot no está conectado a ese servidor.")
        return session, guild

    async def list_guilds(self, request: web.Request) -> web.Response:
        session = self._session(request)
        oauth_guilds = await self._oauth_guilds(session)
        manageable = []
        for guild in oauth_guilds:
            guild_id = int(guild["id"])
            if guild_id in session["admin_guild_ids"] and self.bot.get_guild(guild_id):
                manageable.append({"id": str(guild_id), "name": guild["name"], "icon": guild.get("icon")})
        return web.json_response(manageable)

    async def guild_options(self, request: web.Request) -> web.Response:
        _, guild = await self._authorized_guild(request)
        return web.json_response(
            {
                "channels": [
                    {"id": str(channel.id), "name": channel.name, "category": channel.category.name if channel.category else ""}
                    for channel in guild.text_channels
                ],
                "roles": [
                    {"id": str(role.id), "name": role.name, "color": role.color.value}
                    for role in guild.roles
                    if not role.is_default() and not role.managed
                ],
            }
        )

    def _active_premium(self, guild_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM premium_guilds WHERE guild_id = ? "
                "AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)",
                (guild_id,),
            ).fetchone()
        return row is not None

    def _form_limits(self, guild_id: int) -> tuple[int, int]:
        return (10, 50) if self._active_premium(guild_id) else (3, 20)

    @staticmethod
    def _form_to_dict(connection: sqlite3.Connection, form: sqlite3.Row) -> dict[str, Any]:
        questions = connection.execute(
            "SELECT position, prompt FROM questions WHERE form_id = ? ORDER BY position",
            (form["id"],),
        ).fetchall()
        roles = connection.execute(
            "SELECT action, role_id FROM form_staff_roles WHERE form_id = ?",
            (form["id"],),
        ).fetchall()
        message = json.loads(form["message_json"] or "{}")
        return {
            "id": form["id"],
            "name": form["name"],
            "log_channel_id": str(form["log_channel_id"]) if form["log_channel_id"] else "",
            "message_channel_id": str(form["message_channel_id"]) if form["message_channel_id"] else "",
            "published_message_id": str(form["published_message_id"]) if form["published_message_id"] else "",
            "questions": [dict(question) for question in questions],
            "accept_role_ids": [str(role["role_id"]) for role in roles if role["action"] == "accept"],
            "reject_role_ids": [str(role["role_id"]) for role in roles if role["action"] == "reject"],
            "message": message,
        }

    async def list_forms(self, request: web.Request) -> web.Response:
        _, guild = await self._authorized_guild(request)
        with self._connect() as connection:
            forms = connection.execute(
                "SELECT * FROM forms WHERE guild_id = ? ORDER BY id", (guild.id,)
            ).fetchall()
            result = [self._form_to_dict(connection, form) for form in forms]
        max_forms, max_questions = self._form_limits(guild.id)
        return web.json_response({"forms": result, "limits": {"forms": max_forms, "questions": max_questions}, "premium": self._active_premium(guild.id)})

    async def _get_form_for_guild(self, guild: discord.Guild, form_id: int) -> sqlite3.Row:
        with self._connect() as connection:
            form = connection.execute(
                "SELECT * FROM forms WHERE guild_id = ? AND id = ?",
                (guild.id, form_id),
            ).fetchone()
        if form is None:
            raise web.HTTPNotFound(text="Formulario no encontrado.")
        return form

    async def get_form(self, request: web.Request) -> web.Response:
        _, guild = await self._authorized_guild(request)
        try:
            form_id = int(request.match_info["form_id"])
        except ValueError:
            raise web.HTTPBadRequest(text="ID de formulario inválido.")
        with self._connect() as connection:
            form = connection.execute(
                "SELECT * FROM forms WHERE guild_id = ? AND id = ?", (guild.id, form_id)
            ).fetchone()
            if form is None:
                raise web.HTTPNotFound(text="Formulario no encontrado.")
            return web.json_response(self._form_to_dict(connection, form))

    async def list_submissions(self, request: web.Request) -> web.Response:
        _, guild = await self._authorized_guild(request)
        try:
            form_id = int(request.match_info["form_id"])
        except ValueError:
            raise web.HTTPBadRequest(text="ID de formulario inválido.")
        await self._get_form_for_guild(guild, form_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, user_id, status, review_reason, created_at, decided_at, answers_json "
                "FROM submissions WHERE guild_id = ? AND form_id = ? ORDER BY id DESC LIMIT 100",
                (guild.id, form_id),
            ).fetchall()
        submissions = []
        for row in rows:
            user = self.bot.get_user(row["user_id"])
            submissions.append({
                "id": row["id"],
                "user_id": str(row["user_id"]),
                "username": str(user) if user else None,
                "status": row["status"],
                "review_reason": row["review_reason"],
                "created_at": row["created_at"],
                "decided_at": row["decided_at"],
                "answers": json.loads(row["answers_json"]),
            })
        return web.json_response({"submissions": submissions})

    async def create_form(self, request: web.Request) -> web.Response:
        _, guild = await self._authorized_guild(request)
        data = await self._read_json(request)
        name = data.get("name", "").strip() if isinstance(data.get("name"), str) else ""
        if not name or len(name) > 80:
            raise web.HTTPBadRequest(text="El nombre debe tener entre 1 y 80 caracteres.")
        max_forms, _ = self._form_limits(guild.id)
        with self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM forms WHERE guild_id = ?", (guild.id,)
            ).fetchone()[0]
            if count >= max_forms:
                raise web.HTTPConflict(text=f"Este servidor alcanzó el límite de {max_forms} formularios.")
            try:
                cursor = connection.execute(
                    "INSERT INTO forms (guild_id, name) VALUES (?, ?)", (guild.id, name)
                )
            except sqlite3.IntegrityError as error:
                raise web.HTTPConflict(text="Ya existe un formulario con ese nombre.") from error
        return web.json_response({"id": cursor.lastrowid, "name": name}, status=201)

    @staticmethod
    async def _read_json(request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except (json.JSONDecodeError, web.HTTPException) as error:
            raise web.HTTPBadRequest(text="El cuerpo debe ser JSON válido.") from error
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="El cuerpo JSON debe ser un objeto.")
        return data

    @staticmethod
    def _valid_https_url(value: Any) -> bool:
        if not value:
            return True
        if not isinstance(value, str) or len(value) > 2048:
            return False
        parsed = urlparse(value)
        return parsed.scheme == "https" and bool(parsed.netloc)

    def _validate_message(
        self,
        data: Any,
        guild_id: int,
        form_id: int,
        *,
        available_assets: dict[str, int] | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="La configuración del mensaje no es válida.")
        content = data.get("content", "")
        embeds = data.get("embeds", [])
        attachments = data.get("attachments", [])
        components = data.get("components", [])
        if not isinstance(content, str) or len(content) > MAX_MESSAGE_CONTENT:
            raise web.HTTPBadRequest(text="El contenido admite hasta 2000 caracteres.")
        if not isinstance(embeds, list) or len(embeds) > MAX_EMBEDS:
            raise web.HTTPBadRequest(text="Discord admite hasta 10 embeds por mensaje.")
        if not isinstance(attachments, list) or len(attachments) > 10:
            raise web.HTTPBadRequest(text="El mensaje admite hasta 10 adjuntos.")
        if not isinstance(components, list) or len(components) > MAX_COMPONENTS:
            raise web.HTTPBadRequest(text="El mensaje admite hasta 5 componentes avanzados.")

        total_text = 0
        sanitized_embeds = []
        for embed in embeds:
            if not isinstance(embed, dict):
                raise web.HTTPBadRequest(text="Hay un embed con formato inválido.")
            author = embed.get("author") or {}
            footer = embed.get("footer") or {}
            fields = embed.get("fields") or []
            title = embed.get("title", "")
            description = embed.get("description", "")
            if not isinstance(title, str) or len(title) > 256:
                raise web.HTTPBadRequest(text="El título de un embed admite hasta 256 caracteres.")
            if not isinstance(description, str) or len(description) > 4096:
                raise web.HTTPBadRequest(text="La descripción de un embed admite hasta 4096 caracteres.")
            if not isinstance(author, dict) or len(str(author.get("name", ""))) > 256:
                raise web.HTTPBadRequest(text="El autor de un embed admite hasta 256 caracteres.")
            if not isinstance(footer, dict) or len(str(footer.get("text", ""))) > 2048:
                raise web.HTTPBadRequest(text="El pie de un embed admite hasta 2048 caracteres.")
            if author.get("name") and not isinstance(author["name"], str):
                raise web.HTTPBadRequest(text="El nombre del autor debe ser texto.")
            if footer.get("text") and not isinstance(footer["text"], str):
                raise web.HTTPBadRequest(text="El texto del pie debe ser texto.")
            if not isinstance(fields, list) or len(fields) > 25:
                raise web.HTTPBadRequest(text="Cada embed admite hasta 25 campos.")
            if not any((title, description, fields, author.get("name"), footer.get("text"), embed.get("image"), embed.get("thumbnail"))):
                raise web.HTTPBadRequest(text="Cada embed necesita descripción o algún otro campo.")
            text_parts = [title, description, str(author.get("name", "")), str(footer.get("text", ""))]
            valid_fields = []
            for field in fields:
                if not isinstance(field, dict):
                    raise web.HTTPBadRequest(text="Hay un campo de embed inválido.")
                field_name = field.get("name", "")
                field_value = field.get("value", "")
                if (
                    not isinstance(field_name, str) or not field_name or len(field_name) > 256
                    or not isinstance(field_value, str) or not field_value or len(field_value) > 1024
                ):
                    raise web.HTTPBadRequest(text="Los campos requieren nombre y valor dentro de los límites de Discord.")
                text_parts.extend((field_name, field_value))
                valid_fields.append({"name": field_name, "value": field_value, "inline": bool(field.get("inline"))})
            total_text += sum(map(len, text_parts))
            for url_value in (
                embed.get("url"), embed.get("image"), embed.get("thumbnail"),
                author.get("url"), author.get("icon_url"), footer.get("icon_url"),
            ):
                if url_value and not str(url_value).startswith("attachment://") and not self._valid_https_url(url_value):
                    raise web.HTTPBadRequest(text="Las URLs de embed deben usar HTTPS.")
            color = str(embed.get("color", "#E5323B")).lstrip("#")
            if not HEX_COLOR.fullmatch(color):
                raise web.HTTPBadRequest(text="El color debe ser hexadecimal de seis caracteres.")
            timestamp = embed.get("timestamp")
            if timestamp:
                try:
                    parsed_timestamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                    if parsed_timestamp.tzinfo is None:
                        raise ValueError("timestamp sin zona horaria")
                except ValueError as error:
                    raise web.HTTPBadRequest(text="Timestamp inválido.") from error
            sanitized_embeds.append({**embed, "author": author, "footer": footer, "fields": valid_fields, "color": f"#{color}"})
        if total_text > MAX_EMBED_TEXT:
            raise web.HTTPBadRequest(text="El texto combinado de los embeds supera 6000 caracteres.")

        valid_attachments = []
        total_bytes = 0
        for attachment in attachments:
            if not isinstance(attachment, dict) or not SAFE_ASSET_ID.fullmatch(str(attachment.get("id", ""))):
                raise web.HTTPBadRequest(text="Hay un adjunto inválido.")
            asset_path = self.uploads_root / str(guild_id) / str(form_id) / attachment["id"]
            archived_size = (available_assets or {}).get(attachment["id"])
            if not asset_path.is_file() and archived_size is None:
                raise web.HTTPBadRequest(text="Uno de los adjuntos ya no está disponible; vuelve a subirlo.")
            size = asset_path.stat().st_size if asset_path.is_file() else archived_size
            total_bytes += size
            valid_attachments.append({
                "id": attachment["id"],
                "name": Path(str(attachment.get("name", "archivo"))).name[:100],
                "size": size,
            })
        if total_bytes > MAX_UPLOAD_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD_BYTES, actual_size=total_bytes)

        valid_components = []
        for component in components:
            if not isinstance(component, dict) or component.get("type") != "button":
                raise web.HTTPBadRequest(text="El editor admite botones como componentes avanzados.")
            label = component.get("label", "")
            action = component.get("action", "link")
            if not isinstance(label, str) or not label.strip() or len(label) > 80:
                raise web.HTTPBadRequest(text="Cada botón necesita un texto de hasta 80 caracteres.")
            if action == "link":
                url = component.get("url", "")
                if not self._valid_https_url(url):
                    raise web.HTTPBadRequest(text="Los botones de enlace requieren una URL HTTPS.")
                valid_components.append({"type": "button", "action": "link", "label": label.strip(), "url": url, "style": component.get("style", "primary")})
            elif action == "apply":
                valid_components.append({"type": "button", "action": "apply", "label": label.strip(), "style": component.get("style", "success")})
            else:
                raise web.HTTPBadRequest(text="Acción de botón desconocida.")

        if (
            not allow_empty
            and not content.strip()
            and not sanitized_embeds
            and not valid_attachments
            and not valid_components
        ):
            raise web.HTTPBadRequest(text="Añade contenido, un embed, un adjunto o un componente.")
        return {"content": content, "embeds": sanitized_embeds, "attachments": valid_attachments, "components": valid_components}

    async def save_form(self, request: web.Request) -> web.Response:
        _, guild = await self._authorized_guild(request)
        try:
            form_id = int(request.match_info["form_id"])
        except ValueError:
            raise web.HTTPBadRequest(text="ID de formulario inválido.")
        data = await self._read_json(request)
        with self._connect() as connection:
            form = connection.execute(
                "SELECT id, message_json FROM forms WHERE id = ? AND guild_id = ?", (form_id, guild.id)
            ).fetchone()
        if form is None:
            raise web.HTTPNotFound(text="Formulario no encontrado.")
        name = data.get("name", "")
        questions = data.get("questions", [])
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise web.HTTPBadRequest(text="El nombre debe tener entre 1 y 80 caracteres.")
        max_forms, max_questions = self._form_limits(guild.id)
        del max_forms
        if not isinstance(questions, list) or len(questions) > max_questions:
            raise web.HTTPBadRequest(text=f"Este plan admite {max_questions} preguntas por formulario.")
        clean_questions = []
        for question in questions:
            prompt = question.get("prompt") if isinstance(question, dict) else question
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1000:
                raise web.HTTPBadRequest(text="Cada pregunta debe tener entre 1 y 1000 caracteres.")
            clean_questions.append(prompt.strip())

        accept_roles = self._validate_role_ids(guild, data.get("accept_role_ids", []))
        reject_roles = self._validate_role_ids(guild, data.get("reject_role_ids", []))
        log_channel_id = self._validate_channel_id(guild, data.get("log_channel_id"), text=True)
        message_channel_id = self._validate_channel_id(guild, data.get("message_channel_id"), text=True)
        if log_channel_id:
            log_channel = guild.get_channel(log_channel_id)
            if not self._private_channel(log_channel, set(accept_roles + reject_roles)):
                raise web.HTTPBadRequest(text="El canal de logs debe ser privado para @everyone y visible al staff seleccionado.")
        message = self._validate_message(
            data.get("message", {}), guild.id, form_id, allow_empty=True
        )
        previous_message = json.loads(form["message_json"] or "{}")
        next_attachment_ids = {item["id"] for item in message["attachments"]}
        obsolete_attachment_ids = {
            item["id"] for item in previous_message.get("attachments", [])
            if item["id"] not in next_attachment_ids
        }

        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE forms SET name = ?, log_channel_id = ?, message_channel_id = ?, message_json = ? "
                    "WHERE id = ? AND guild_id = ?",
                    (name.strip(), log_channel_id, message_channel_id, json.dumps(message, ensure_ascii=False), form_id, guild.id),
                )
                connection.execute("DELETE FROM questions WHERE form_id = ?", (form_id,))
                connection.executemany(
                    "INSERT INTO questions (form_id, position, prompt) VALUES (?, ?, ?)",
                    [(form_id, index, prompt) for index, prompt in enumerate(clean_questions, 1)],
                )
                connection.execute("DELETE FROM form_staff_roles WHERE form_id = ?", (form_id,))
                connection.executemany(
                    "INSERT INTO form_staff_roles (form_id, action, role_id) VALUES (?, ?, ?)",
                    [(form_id, action, role_id) for action, role_ids in (("accept", accept_roles), ("reject", reject_roles)) for role_id in role_ids],
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise web.HTTPConflict(text="Ese formulario ya existe.") from error
            except Exception:
                connection.rollback()
                raise
        for asset_id in obsolete_attachment_ids:
            path = self.uploads_root / str(guild.id) / str(form_id) / asset_id
            await asyncio.to_thread(path.unlink, missing_ok=True)
        return web.json_response({"ok": True})

    @staticmethod
    def _validate_role_ids(guild: discord.Guild, role_ids: Any) -> list[int]:
        if not isinstance(role_ids, list) or len(role_ids) > 25:
            raise web.HTTPBadRequest(text="Selecciona como máximo 25 roles por acción.")
        valid = []
        for value in role_ids:
            try:
                role_id = int(value)
            except (TypeError, ValueError) as error:
                raise web.HTTPBadRequest(text="ID de rol inválido.") from error
            role = guild.get_role(role_id)
            if role is None or role.is_default() or role.managed:
                raise web.HTTPBadRequest(text="Uno de los roles no pertenece al servidor o no es válido.")
            valid.append(role_id)
        return list(dict.fromkeys(valid))

    @staticmethod
    def _validate_channel_id(guild: discord.Guild, value: Any, *, text: bool) -> int | None:
        if value in (None, ""):
            return None
        try:
            channel_id = int(value)
        except (TypeError, ValueError) as error:
            raise web.HTTPBadRequest(text="ID de canal inválido.") from error
        channel = guild.get_channel(channel_id)
        if text and not isinstance(channel, discord.TextChannel):
            raise web.HTTPBadRequest(text="El canal debe ser un canal de texto de este servidor.")
        if not text and channel is None:
            raise web.HTTPBadRequest(text="El canal debe pertenecer a este servidor.")
        return channel_id

    @staticmethod
    def _private_channel(channel: discord.TextChannel, role_ids: set[int]) -> bool:
        if channel.permissions_for(channel.guild.default_role).view_channel:
            return False
        for role in channel.guild.roles:
            if role == channel.guild.default_role or role.permissions.administrator:
                continue
            can_view = channel.permissions_for(role).view_channel
            if can_view != (role.id in role_ids):
                return False
        for target, overwrite in channel.overwrites.items():
            if isinstance(target, discord.Member) and overwrite.view_channel is True and not target.guild_permissions.administrator and not role_ids.intersection(role.id for role in target.roles):
                return False
        return True

    async def delete_form(self, request: web.Request) -> web.Response:
        _, guild = await self._authorized_guild(request)
        form_id = int(request.match_info["form_id"])
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            form = connection.execute(
                "SELECT message_channel_id, published_message_id FROM forms WHERE id = ? AND guild_id = ?",
                (form_id, guild.id),
            ).fetchone()
            if form is None:
                raise web.HTTPNotFound(text="Formulario no encontrado.")
            if form["message_channel_id"] and form["published_message_id"]:
                channel = guild.get_channel(form["message_channel_id"])
                if isinstance(channel, discord.TextChannel):
                    try:
                        message = await channel.fetch_message(form["published_message_id"])
                        await message.delete()
                    except discord.HTTPException:
                        pass
            connection.execute("DELETE FROM forms WHERE id = ?", (form_id,))
        await asyncio.to_thread(shutil.rmtree, self.uploads_root / str(guild.id) / str(form_id), True)
        return web.json_response({"ok": True})

    async def upload_attachments(self, request: web.Request) -> web.Response:
        _, guild = await self._authorized_guild(request)
        form_id = int(request.match_info["form_id"])
        form = await self._get_form_for_guild(guild, form_id)
        config = json.loads(form["message_json"] or "{}")
        existing = config.get("attachments", [])
        target_dir = self.uploads_root / str(guild.id) / str(form_id)
        pending_files = list(target_dir.iterdir()) if target_dir.exists() else []
        if max(len(existing), len(pending_files)) >= 10:
            raise web.HTTPConflict(text="El mensaje ya tiene 10 adjuntos.")
        reader = await request.multipart()
        uploaded = []
        current_size = sum(int(item.get("size", 0)) for item in existing)
        remaining_slots = 10 - max(len(existing), len(pending_files))
        while part := await reader.next():
            if not part.filename:
                continue
            if len(uploaded) >= remaining_slots:
                raise web.HTTPBadRequest(text="Solo puedes subir hasta 10 adjuntos por mensaje.")
            content = await part.read(decode=False)
            current_size += len(content)
            if current_size > MAX_UPLOAD_BYTES:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD_BYTES, actual_size=current_size)
            asset_id = uuid.uuid4().hex
            name = Path(part.filename).name[:100] or "archivo"
            target_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread((target_dir / asset_id).write_bytes, content)
            uploaded.append({"id": asset_id, "name": name, "size": len(content)})
        if not uploaded:
            raise web.HTTPBadRequest(text="No se recibieron archivos.")
        return web.json_response({"attachments": uploaded}, status=201)

    async def create_backup(self, request: web.Request) -> web.Response:
        _, guild = await self._authorized_guild(request)
        with self._connect() as connection:
            forms = connection.execute(
                "SELECT * FROM forms WHERE guild_id = ? ORDER BY id", (guild.id,)
            ).fetchall()
            form_data = [self._form_to_dict(connection, form) for form in forms]
            submissions = connection.execute(
                "SELECT id, form_id, user_id, answers_json, status, reviewer_id, "
                "review_reason, created_at, decided_at FROM submissions "
                "WHERE guild_id = ? ORDER BY id",
                (guild.id,),
            ).fetchall()
        manifest = {
            "format": "sentra-security-dashboard-backup",
            "version": 1,
            "guild_id": guild.id,
            "guild_name": guild.name,
            "exported_at": discord.utils.utcnow().isoformat(),
            "forms": form_data,
            "submissions": [
                {
                    "id": row["id"],
                    "form_id": row["form_id"],
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
        output = BytesIO()
        total_files = 1
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "backup.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            for form in form_data:
                for attachment in form["message"].get("attachments", []):
                    if not SAFE_ASSET_ID.fullmatch(str(attachment.get("id", ""))):
                        continue
                    path = self.uploads_root / str(guild.id) / str(form["id"]) / attachment["id"]
                    if not path.is_file():
                        raise web.HTTPBadGateway(
                            text=f"Falta el adjunto {attachment.get('name', '')}; no se creó una copia incompleta."
                        )
                    archive.write(
                        path,
                        f"assets/{form['id']}/{attachment['id']}",
                    )
                    total_files += 1
        if output.tell() > MAX_BACKUP_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_BACKUP_BYTES, actual_size=output.tell()
            )
        return web.Response(
            body=output.getvalue(),
            content_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="sentra-backup-{guild.id}.zip"',
                "X-Backup-Files": str(total_files),
            },
        )

    async def restore_backup(self, request: web.Request) -> web.Response:
        _, guild = await self._authorized_guild(request)
        body = await request.read()
        if not body or len(body) > MAX_BACKUP_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_BACKUP_BYTES, actual_size=len(body)
            )
        try:
            with zipfile.ZipFile(BytesIO(body)) as archive:
                entries = archive.infolist()
                if len(entries) > 2000 or sum(entry.file_size for entry in entries) > MAX_BACKUP_BYTES:
                    raise ValueError("El respaldo contiene demasiados datos.")
                for entry in entries:
                    parts = Path(entry.filename).parts
                    if entry.filename.startswith("/") or ".." in parts:
                        raise ValueError("El respaldo contiene rutas no válidas.")
                if "backup.json" not in archive.namelist():
                    raise ValueError("No se encontró backup.json dentro del archivo.")
                manifest = json.loads(archive.read("backup.json").decode("utf-8"))
                if not isinstance(manifest, dict):
                    raise ValueError("El manifiesto del respaldo no es válido.")
                assets = {
                    entry.filename: archive.read(entry)
                    for entry in entries
                    if entry.filename.startswith("assets/") and not entry.is_dir()
                }
        except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise web.HTTPBadRequest(text="El archivo no es un respaldo ZIP válido.") from error
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from error

        if (
            manifest.get("format") != "sentra-security-dashboard-backup"
            or manifest.get("version") != 1
            or manifest.get("guild_id") != guild.id
        ):
            raise web.HTTPBadRequest(text="Este respaldo pertenece a otro servidor o versión.")
        backup_forms = manifest.get("forms")
        backup_submissions = manifest.get("submissions")
        max_forms, max_questions = self._form_limits(guild.id)
        if not isinstance(backup_forms, list) or len(backup_forms) > max_forms:
            raise web.HTTPConflict(text=f"El respaldo supera el límite actual de {max_forms} formularios.")
        if not isinstance(backup_submissions, list) or len(backup_submissions) > 10_000:
            raise web.HTTPBadRequest(text="El respaldo contiene demasiadas solicitudes.")

        validated_forms = []
        old_ids: set[int] = set()
        names: set[str] = set()
        assets_to_restore: list[tuple[int, str, bytes]] = []
        for form in backup_forms:
            if not isinstance(form, dict):
                raise web.HTTPBadRequest(text="Hay un formulario con formato inválido.")
            old_id = form.get("id")
            name = form.get("name")
            questions = form.get("questions")
            if not isinstance(old_id, int) or old_id in old_ids:
                raise web.HTTPBadRequest(text="El respaldo contiene IDs de formulario inválidos.")
            old_ids.add(old_id)
            if not isinstance(name, str) or not name.strip() or len(name) > 80 or name.casefold() in names:
                raise web.HTTPBadRequest(text="El respaldo contiene nombres de formulario inválidos o repetidos.")
            names.add(name.casefold())
            if not isinstance(questions, list) or len(questions) > max_questions:
                raise web.HTTPConflict(text=f"Un formulario supera el límite actual de {max_questions} preguntas.")
            clean_questions = []
            for item in questions:
                prompt = item.get("prompt") if isinstance(item, dict) else None
                if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1000:
                    raise web.HTTPBadRequest(text="El respaldo contiene una pregunta inválida.")
                clean_questions.append(prompt.strip())
            accept_roles = self._validate_role_ids(
                guild, form.get("accept_role_ids", [])
            )
            reject_roles = self._validate_role_ids(
                guild, form.get("reject_role_ids", [])
            )
            channel_value = form.get("log_channel_id")
            log_channel_id = self._validate_channel_id(guild, channel_value, text=True) if channel_value else None
            allowed_staff = set(accept_roles + reject_roles)
            if log_channel_id and not self._private_channel(guild.get_channel(log_channel_id), allowed_staff):
                log_channel_id = None
            message_channel_value = form.get("message_channel_id")
            message_channel_id = self._validate_channel_id(guild, message_channel_value, text=True) if message_channel_value else None

            message = form.get("message") or {}
            available_assets: dict[str, int] = {}
            for attachment in message.get("attachments", []):
                asset_id = str(attachment.get("id", ""))
                if not SAFE_ASSET_ID.fullmatch(asset_id):
                    raise web.HTTPBadRequest(text="El respaldo contiene un adjunto con ID inválido.")
                archive_path = f"assets/{old_id}/{asset_id}"
                asset_content = assets.get(archive_path)
                if asset_content is None:
                    raise web.HTTPBadRequest(text=f"Falta el archivo adjunto {attachment.get('name', '')} en el respaldo.")
                available_assets[asset_id] = len(asset_content)
                assets_to_restore.append((old_id, asset_id, asset_content))
            clean_message = self._validate_message(
                message,
                guild.id,
                old_id,
                available_assets=available_assets,
                allow_empty=True,
            )
            validated_forms.append({
                "old_id": old_id,
                "name": name.strip(),
                "questions": clean_questions,
                "log_channel_id": log_channel_id,
                "message_channel_id": message_channel_id,
                "message": clean_message,
                "accept_roles": accept_roles,
                "reject_roles": reject_roles,
            })

        valid_submission_statuses = {"pending", "accepted", "rejected", "delivery_failed", "archived"}
        for submission in backup_submissions:
            if not isinstance(submission, dict):
                raise web.HTTPBadRequest(text="El respaldo contiene una solicitud inválida.")
            answers = submission.get("answers") if isinstance(submission, dict) else None
            if (
                not isinstance(submission.get("form_id"), int)
                or submission["form_id"] not in old_ids
                or not isinstance(submission.get("user_id"), int)
                or submission.get("status") not in valid_submission_statuses
                or not isinstance(answers, list)
                or len(answers) > max_questions
                or any(not isinstance(answer, list) or len(answer) != 2 or not all(isinstance(value, str) for value in answer) for answer in answers)
            ):
                raise web.HTTPBadRequest(text="El respaldo contiene una solicitud inválida.")

        await self._replace_guild_backup(guild, validated_forms, backup_submissions, assets_to_restore)
        return web.json_response({"ok": True, "forms": len(validated_forms), "submissions": len(backup_submissions)})

    async def _replace_guild_backup(
        self,
        guild: discord.Guild,
        forms: list[dict[str, Any]],
        submissions: list[dict[str, Any]],
        assets: list[tuple[int, str, bytes]],
    ) -> None:
        old_messages = []
        new_form_ids: dict[int, int] = {}
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            old_messages = connection.execute(
                "SELECT message_channel_id, published_message_id FROM forms "
                "WHERE guild_id = ? AND message_channel_id IS NOT NULL AND published_message_id IS NOT NULL",
                (guild.id,),
            ).fetchall()
            connection.execute("DELETE FROM submissions WHERE guild_id = ?", (guild.id,))
            connection.execute("DELETE FROM forms WHERE guild_id = ?", (guild.id,))
            for form in forms:
                cursor = connection.execute(
                    "INSERT INTO forms (guild_id, name, log_channel_id, message_json, message_channel_id, published_message_id) "
                    "VALUES (?, ?, ?, ?, ?, NULL)",
                    (
                        guild.id,
                        form["name"],
                        form["log_channel_id"],
                        json.dumps(form["message"], ensure_ascii=False),
                        form["message_channel_id"],
                    ),
                )
                new_id = cursor.lastrowid
                new_form_ids[form["old_id"]] = new_id
                connection.executemany(
                    "INSERT INTO questions (form_id, position, prompt) VALUES (?, ?, ?)",
                    [(new_id, index, prompt) for index, prompt in enumerate(form["questions"], 1)],
                )
                connection.executemany(
                    "INSERT INTO form_staff_roles (form_id, action, role_id) VALUES (?, ?, ?)",
                    [(new_id, action, role_id) for action, key in (("accept", "accept_roles"), ("reject", "reject_roles")) for role_id in form[key]],
                )
            for item in submissions:
                status = "archived" if item["status"] == "pending" else item["status"]
                connection.execute(
                    "INSERT INTO submissions (guild_id, form_id, user_id, answers_json, status, reviewer_id, review_reason, review_message_id, created_at, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        guild.id,
                        new_form_ids[item["form_id"]],
                        item["user_id"],
                        json.dumps(item["answers"], ensure_ascii=False),
                        status,
                        item.get("reviewer_id"),
                        item.get("review_reason"),
                        item.get("created_at") or "CURRENT_TIMESTAMP",
                        item.get("decided_at"),
                    ),
                )
        guild_upload_root = self.uploads_root / str(guild.id)
        await asyncio.to_thread(shutil.rmtree, guild_upload_root, True)
        for old_form_id, asset_id, content in assets:
            new_form_id = new_form_ids[old_form_id]
            target_dir = self.uploads_root / str(guild.id) / str(new_form_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread((target_dir / asset_id).write_bytes, content)
        for old_message in old_messages:
            channel = guild.get_channel(old_message["message_channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(old_message["published_message_id"])
                    await message.delete()
                except discord.HTTPException:
                    pass

    async def publish_form(self, request: web.Request) -> web.Response:
        _, guild = await self._authorized_guild(request)
        form_id = int(request.match_info["form_id"])
        form = await self._get_form_for_guild(guild, form_id)
        if not form["message_channel_id"]:
            raise web.HTTPBadRequest(text="Selecciona el canal donde se publicará el mensaje.")
        channel = guild.get_channel(form["message_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            raise web.HTTPBadRequest(text="El canal de publicación ya no existe.")
        permissions = channel.permissions_for(guild.me)
        if not permissions.view_channel or not permissions.send_messages or not permissions.embed_links or not permissions.attach_files:
            raise web.HTTPForbidden(text="El bot necesita ver el canal y enviar mensajes, embeds y archivos.")
        with self._connect() as connection:
            questions = connection.execute(
                "SELECT COUNT(*) FROM questions WHERE form_id = ?", (form_id,)
            ).fetchone()[0]
        if not questions:
            raise web.HTTPBadRequest(text="Añade al menos una pregunta antes de publicar el formulario.")
        config = json.loads(form["message_json"] or "{}")
        if not any((config.get("content"), config.get("embeds"), config.get("attachments"), config.get("components"))):
            raise web.HTTPBadRequest(text="Añade contenido, un embed, un adjunto o un componente antes de publicar.")
        embeds = [self._build_embed(item) for item in config.get("embeds", [])]
        files = []
        for attachment in config.get("attachments", []):
            asset_id = attachment["id"]
            path = self.uploads_root / str(guild.id) / str(form_id) / asset_id
            if not path.is_file():
                raise web.HTTPBadRequest(text=f"No se encuentra el adjunto {attachment.get('name', '')}.")
            filename = attachment["name"]
            files.append(discord.File(path, filename=filename))
        view = self._build_view(form_id, form["name"], config.get("components", []))
        try:
            message = await channel.send(
                content=config.get("content") or None,
                embeds=embeds or None,
                files=files or None,
                view=view if view.children else None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as error:
            raise web.HTTPBadGateway(text=f"Discord no pudo publicar el mensaje: {error}") from error
        finally:
            for uploaded_file in files:
                uploaded_file.close()
        old_channel_id = form["message_channel_id"]
        old_message_id = form["published_message_id"]
        if old_channel_id and old_message_id:
            old_channel = guild.get_channel(old_channel_id)
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old_message = await old_channel.fetch_message(old_message_id)
                    await old_message.delete()
                except discord.HTTPException:
                    pass
        with self._connect() as connection:
            connection.execute(
                "UPDATE forms SET published_message_id = ? WHERE id = ? AND guild_id = ?",
                (message.id, form_id, guild.id),
            )
        if any(item.get("action") == "apply" for item in config.get("components", [])):
            self.bot.add_view(view, message_id=message.id)
        return web.json_response({"ok": True, "message_id": str(message.id), "jump_url": message.jump_url})

    @staticmethod
    def _build_embed(data: dict[str, Any]) -> discord.Embed:
        color = int(data.get("color", "#E5323B").lstrip("#"), 16)
        timestamp = None
        if data.get("timestamp"):
            timestamp = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        embed = discord.Embed(
            title=data.get("title") or None,
            description=data.get("description") or None,
            url=data.get("url") or None,
            color=discord.Color(color),
            timestamp=timestamp,
        )
        author = data.get("author") or {}
        if author.get("name"):
            embed.set_author(name=author["name"], url=author.get("url") or None, icon_url=author.get("icon_url") or None)
        if data.get("image"):
            embed.set_image(url=data["image"])
        if data.get("thumbnail"):
            embed.set_thumbnail(url=data["thumbnail"])
        footer = data.get("footer") or {}
        if footer.get("text"):
            embed.set_footer(text=footer["text"], icon_url=footer.get("icon_url") or None)
        for field in data.get("fields", []):
            embed.add_field(name=field["name"], value=field["value"], inline=field.get("inline", False))
        return embed

    def _build_view(self, form_id: int, form_name: str, components: list[dict[str, Any]]) -> discord.ui.View:
        return DashboardMessageView(
            self.bot.get_cog("Formularios"), form_id, form_name, components
        )


class DashboardMessageView(discord.ui.View):
    def __init__(
        self,
        cog: Any,
        form_id: int,
        form_name: str,
        components: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.form_id = form_id
        self.form_name = form_name
        for index, component in enumerate(components or []):
            if component.get("action") == "apply":
                self.add_item(
                    DashboardApplyButton(cog, form_id, form_name, component, index)
                )
            else:
                self.add_item(
                    discord.ui.Button(
                        label=component["label"],
                        style=discord.ButtonStyle.link,
                        url=component["url"],
                    )
                )


class DashboardApplyButton(discord.ui.Button):
    def __init__(
        self,
        cog: Any,
        form_id: int,
        form_name: str,
        component: dict[str, Any],
        index: int,
    ) -> None:
        self.cog = cog
        self.form_name = form_name
        style = {
            "primary": discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "success": discord.ButtonStyle.success,
            "danger": discord.ButtonStyle.danger,
        }.get(component.get("style"), discord.ButtonStyle.success)
        super().__init__(
            label=component["label"],
            style=style,
            custom_id=f"sentra:dashboard-apply:{form_id}:{index}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.start_application(interaction, self.form_name)
