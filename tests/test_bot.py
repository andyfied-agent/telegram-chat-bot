import asyncio
import importlib.util
import pathlib
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("bot", pathlib.Path(__file__).parents[1] / "telegram-chat-bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.settings = bot.Settings(token="test", max_messages=2, max_chars=20)
        self.state = bot.State(self.settings)

    def test_history_is_bounded(self):
        self.state.history[1].extend([{"role": "user", "content": str(i)} for i in range(5)])
        self.assertEqual([m["content"] for m in self.state.history[1]], ["3", "4"])

    def test_complete_uses_openai_shape(self):
        response = b'{"choices":[{"message":{"content":"hello"}}]}'
        with patch.object(bot, "urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = response
            self.assertEqual(bot.complete(self.settings, [{"role": "user", "content": "hi"}]), "hello")
            request = open_url.call_args.args[0]
            self.assertTrue(request.full_url.endswith("/chat/completions"))


if __name__ == "__main__":
    unittest.main()
