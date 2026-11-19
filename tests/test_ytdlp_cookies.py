#!/usr/bin/env python3
from __future__ import annotations
import os, sys, unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.sourcing import _cookie_flags_in, ytdlp_cookie_args  # noqa: E402


class CookieArgs(unittest.TestCase):
    def test_explicit_browser(self):
        with patch.dict(os.environ, {"VICE_YT_COOKIES_BROWSER": "chrome", "GITHUB_ACTIONS": ""}, clear=False):
            os.environ.pop("GITHUB_ACTIONS", None)
            self.assertEqual(ytdlp_cookie_args(), ["--cookies-from-browser", "chrome"])

    def test_github_actions_stays_off_without_env(self):
        env = {"GITHUB_ACTIONS": "true"}
        env.pop("VICE_YT_COOKIES_BROWSER", None)
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("VICE_YT_COOKIES_BROWSER", None)
            # may still pick up VICE_YT_COOKIES path; just assert no implicit chrome when GA
            args = ytdlp_cookie_args()
            if "VICE_YT_COOKIES" not in os.environ or not os.environ.get("VICE_YT_COOKIES"):
                self.assertNotEqual(args[:1], ["--cookies-from-browser"])

    def test_no_double_cookie_flags(self):
        self.assertTrue(_cookie_flags_in(["--cookies-from-browser", "chrome", "-o", "x"]))
        self.assertTrue(_cookie_flags_in(["--cookies", "/tmp/c.txt"]))
        self.assertFalse(_cookie_flags_in(["-f", "bv*", "-o", "x"]))


if __name__ == "__main__":
    unittest.main()

