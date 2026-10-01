import sqlite3
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

from dashboard import Dashboard


class DashboardValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dashboard = Dashboard.__new__(Dashboard)
        self.dashboard.uploads_root = Path(self.temp_dir.name) / "uploads"
        self.dashboard.database_path = Path(self.temp_dir.name) / "dashboard.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_content_limit_accepts_2000_characters(self) -> None:
        config = self.dashboard._validate_message(
            {"content": "x" * 2000}, 1, 1
        )
        self.assertEqual(len(config["content"]), 2000)

    def test_content_limit_rejects_more_than_2000_characters(self) -> None:
        with self.assertRaises(web.HTTPBadRequest):
            self.dashboard._validate_message(
                {"content": "x" * 2001}, 1, 1
            )

    def test_embed_combined_text_limit_is_6000(self) -> None:
        config = self.dashboard._validate_message(
            {"embeds": [{"description": "x" * 4096}, {"description": "y" * 1904}]},
            1,
            1,
        )
        self.assertEqual(sum(len(embed["description"]) for embed in config["embeds"]), 6000)
        with self.assertRaises(web.HTTPBadRequest):
            self.dashboard._validate_message(
                {"embeds": [{"description": "x" * 4096}, {"description": "y" * 1905}]},
                1,
                1,
            )

    def test_embed_urls_require_https(self) -> None:
        with self.assertRaises(web.HTTPBadRequest):
            self.dashboard._validate_message(
                {"embeds": [{"title": "Aviso", "image": "http://example.com/image.png"}]},
                1,
                1,
            )

    def test_expired_premium_does_not_grant_premium_limits(self) -> None:
        with sqlite3.connect(self.dashboard.database_path) as connection:
            connection.execute(
                "CREATE TABLE premium_guilds (guild_id INTEGER PRIMARY KEY, expires_at TEXT)"
            )
            connection.execute(
                "INSERT INTO premium_guilds VALUES (1, datetime('now', '-1 day'))"
            )
            connection.execute(
                "INSERT INTO premium_guilds VALUES (2, datetime('now', '+1 day'))"
            )
            connection.execute(
                "INSERT INTO premium_guilds VALUES (3, NULL)"
            )
        self.assertEqual(self.dashboard._form_limits(1), (3, 20))
        self.assertEqual(self.dashboard._form_limits(2), (10, 50))
        self.assertEqual(self.dashboard._form_limits(3), (10, 50))


if __name__ == "__main__":
    unittest.main()
