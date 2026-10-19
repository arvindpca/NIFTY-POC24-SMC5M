"""Tests for Telegram delivery acknowledgements without making network calls."""
import os
import unittest
from unittest.mock import Mock, patch

from app import notifications


class TelegramDeliveryTests(unittest.TestCase):
    def test_missing_credentials_fail_instead_of_silently_skipping(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "delivery was not attempted"):
                notifications.send_telegram("sample")

    def test_positive_bot_api_acknowledgement_is_returned(self):
        response = Mock()
        response.json.return_value = {
            "ok": True,
            "result": {"message_id": 123, "chat": {"id": 456}},
        }
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "456"},
            clear=True,
        ), patch("app.notifications.requests.post", return_value=response) as post:
            ack = notifications.send_telegram("sample")

        self.assertEqual(ack["message_id"], 123)
        self.assertEqual(ack["chat_id"], 456)
        response.raise_for_status.assert_called_once_with()
        post.assert_called_once()

    def test_non_success_bot_api_response_fails(self):
        response = Mock()
        response.json.return_value = {"ok": False, "description": "rejected"}
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "456"},
            clear=True,
        ), patch("app.notifications.requests.post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "did not acknowledge"):
                notifications.send_telegram("sample")


if __name__ == "__main__":
    unittest.main()
