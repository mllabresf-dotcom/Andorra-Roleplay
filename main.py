import logging
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from comadnos.formularios import Formularios
from comadnos.premium import Premium
from dashboard import Dashboard


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DATABASE_PATH = DATA_DIR / "formularios.sqlite3"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

try:
    BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "").strip() or "0")
except ValueError as error:
    raise RuntimeError("BOT_OWNER_ID debe ser un ID numérico de Discord.") from error


class SentraSecurity(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            owner_id=BOT_OWNER_ID,
        )
        self.dashboard = Dashboard(self, DATABASE_PATH, ROOT)

    async def setup_hook(self) -> None:
        await self.add_cog(Formularios(self, DATABASE_PATH))
        await self.add_cog(Premium(self, DATABASE_PATH))
        synced = await self.tree.sync()
        logging.info("Comandos slash sincronizados: %s", len(synced))
        await self.dashboard.start()

    async def close(self) -> None:
        await self.dashboard.close()
        await super().close()


intents = discord.Intents.default()
intents.message_content = True
bot = SentraSecurity()


@bot.event
async def on_ready() -> None:
    logging.info("Sentra Security conectado como %s", bot.user)
    logging.info(
        "Dashboard: http://%s:%s",
        bot.dashboard.host,
        bot.dashboard.port,
    )


token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("Configura la variable de entorno DISCORD_TOKEN antes de iniciar.")

bot.run(token)