# Sentra Security

Bot de Discord y dashboard web para gestionar formularios por servidor. Los datos se comparten en `data/formularios.sqlite3`.

## Configuración

1. Copia `.env.example` a `.env` y completa `DISCORD_TOKEN`, `BOT_OWNER_ID`, `DISCORD_CLIENT_ID` y `DISCORD_CLIENT_SECRET`.
2. En Discord Developer Portal, añade `http://localhost:8080/auth/callback` a OAuth2 → Redirects y habilita el scope `identify` y `guilds` para OAuth.
3. Invita el bot al servidor con los scopes `bot` y `applications.commands`. Dale permisos para ver/escribir en canales, insertar enlaces y adjuntar archivos. Activa Message Content Intent para recoger las respuestas de formularios por DM.
4. Selecciona un intérprete Python en VS Code y pulsa F5. La tarea previa instala `requirements.txt`; el bot y el dashboard arrancan juntos.
5. Abre `http://localhost:8080` e inicia sesión con Discord. Solo aparecen servidores donde seas owner/admin y esté instalado el bot.

No compartas `.env` ni las copias descargadas. En producción, usa HTTPS, fija `DASHBOARD_COOKIE_SECURE=true`, configura un `DASHBOARD_REDIRECT_URI` HTTPS y protege el puerto del dashboard con un proxy inverso/firewall. El dashboard por defecto escucha solo en `127.0.0.1`.

## Funciones

- Configuración por formulario de preguntas, canal privado de logs y roles de staff.
- Editor de mensajes con contenido, formato básico, hasta 10 embeds (6000 caracteres combinados), 10 adjuntos (24.5 MB), botones de enlace/aplicación y previsualización.
- Guardado del borrador y publicación en Discord. Los botones de aplicación continúan el formulario por DM.
- Historial de solicitudes y motivos; las decisiones se hacen desde el canal privado de logs.
- Copia y restauración ZIP por servidor. Las solicitudes pendientes se archivan al restaurar; conserva los adjuntos del mensaje.
- Premium con caducidad opcional: 10 formularios, 50 preguntas por formulario y exportación JSON de solicitudes.

Para activar premium usa `/premium_agregar` (opcionalmente `duracion_dias`), `/premium_lista` y `/premium_quitar`. Solo el owner configurado en `BOT_OWNER_ID` puede ejecutarlos.

Las pruebas unitarias se ejecutan desde VS Code con `Terminal → Run Task → Test Sentra dashboard`.

## Vista estática en Vercel

Para una demo visual sin backend:

1. En Vercel selecciona **Add New → Project** e importa este repositorio desde GitHub.
2. En la configuración del proyecto usa **Framework Preset: Other**, **Root Directory: `.`**, deja el Build Command vacío y configura **Output Directory: `app`**.
3. Pulsa **Deploy**. Al terminar, abre la URL asignada y añade `?demo=1`, por ejemplo `https://tu-proyecto.vercel.app/?demo=1`.

El distintivo `DEMO · NO GUARDA EN DISCORD` indica que los servidores y formularios son ejemplos y los cambios viven solo en la memoria de esa pestaña. Vercel no ejecuta el bot, el login OAuth ni el API SQLite. Para la dashboard real abre la dirección local `http://localhost:8080` con el bot iniciado; para publicarla funcionalmente hace falta alojar el backend Python por separado y configurar OAuth con el dominio publicado.
