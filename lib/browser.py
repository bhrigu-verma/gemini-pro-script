"""Browser session helpers wrapping immutable main.py primitives."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

import main as core

from config.constants import (
    BROWSER_MODE_ATTACH,
    BROWSER_MODE_HEADLESS,
    CHROME_ANTI_BOT_FLAGS,
    DEBUG_PORT,
    DEFAULT_SPOOFED_USER_AGENT,
    GEMINI_URL,
)
from config.defaults import DEFAULT_CHROME_USER_DATA_DIR


class BrowserSession:
    def __init__(
        self,
        port: int = DEBUG_PORT,
        browser_mode: str = BROWSER_MODE_ATTACH,
        headless: bool = False,
        user_data_dir: str = DEFAULT_CHROME_USER_DATA_DIR,
        user_agent: str = DEFAULT_SPOOFED_USER_AGENT,
    ) -> None:
        self.port = port
        self.browser_mode = browser_mode
        self.headless = headless
        self.user_data_dir = user_data_dir
        self.user_agent = user_agent or DEFAULT_SPOOFED_USER_AGENT
        self.driver = None
        self._chrome_proc: Optional[subprocess.Popen] = None

    @staticmethod
    def _chrome_binary() -> str:
        return os.environ.get(
            "CHROME_BIN",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )

    def _is_debug_port_live(self) -> bool:
        try:
            import urllib.request

            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/version", timeout=1.0
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _launch_headless_chrome(self) -> None:
        if self._is_debug_port_live():
            return

        args = [
            self._chrome_binary(),
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            f"--user-agent={self.user_agent}",
            *CHROME_ANTI_BOT_FLAGS,
        ]

        if self.headless or self.browser_mode == BROWSER_MODE_HEADLESS:
            args.append("--headless=new")

        self._chrome_proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        for _ in range(50):
            if self._is_debug_port_live():
                return
            time.sleep(0.2)

        raise RuntimeError(
            f"Chrome debug endpoint not reachable on port {self.port} after launch"
        )

    def attach(self):
        if self.browser_mode == BROWSER_MODE_HEADLESS:
            self._launch_headless_chrome()
        self.driver = core.attach_driver(self.port)
        return self.driver

    def open_tab(self, label: str, url: str = GEMINI_URL, wait_seconds: float = 2.0):
        if self.driver is None:
            raise RuntimeError("BrowserSession is not attached")
        handle = core.open_tab(self.driver, url, label)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        return handle

    def ensure_logged_in(self, tab_name: str) -> None:
        if self.driver is None:
            raise RuntimeError("BrowserSession is not attached")
        url = self.driver.current_url.lower()
        if "accounts.google.com" in url or "signin" in url:
            print(f"[{tab_name}] login required. Complete login then press ENTER.")
            input()
