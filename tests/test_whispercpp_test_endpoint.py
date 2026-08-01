"""Tests for the settings Test button backend for Whisper.cpp (_test_whispercpp)."""

import unittest
from unittest.mock import patch, MagicMock

import requests

from src.web_server import _test_whispercpp


def _resp(status_code):
    r = MagicMock()
    r.status_code = status_code
    return r


class TestWhisperCppTestEndpoint(unittest.TestCase):
    def test_missing_url(self):
        result = _test_whispercpp("")
        self.assertFalse(result["ok"])
        self.assertIn("Missing", result["message"])

    def test_reachable_even_on_405(self):
        """GET on a POST-only endpoint (404/405) still proves reachability."""
        with patch("src.web_server.requests.get", return_value=_resp(405)):
            result = _test_whispercpp("http://x/v1/audio/transcriptions")
        self.assertTrue(result["ok"])
        self.assertIn("405", result["message"])

    def test_connection_refused(self):
        err = requests.exceptions.ConnectionError("Connection refused")
        with patch("src.web_server.requests.get", side_effect=err):
            result = _test_whispercpp("http://x/v1/audio/transcriptions")
        self.assertFalse(result["ok"])
        self.assertIn("Connection refused", result["message"])

    def test_timeout(self):
        with patch("src.web_server.requests.get", side_effect=requests.exceptions.Timeout()):
            result = _test_whispercpp("http://x/v1/audio/transcriptions")
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["message"])


if __name__ == "__main__":
    unittest.main()
