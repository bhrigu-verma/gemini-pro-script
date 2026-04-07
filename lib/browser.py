"""Browser session helpers for Gemini automation."""

from __future__ import annotations

import os
import sys
import subprocess
import time
from typing import Optional, Set, List

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import WebDriverException

try:
    from webdriver_manager.chrome import ChromeDriverManager
    _HAS_WDM = True
except ImportError:
    _HAS_WDM = False

from config.constants import (
    BROWSER_MODE_ATTACH,
    BROWSER_MODE_HEADLESS,
    CHROME_ANTI_BOT_FLAGS,
    DEBUG_PORT,
    DEFAULT_SPOOFED_USER_AGENT,
    GEMINI_URL,
)
from config.defaults import DEFAULT_CHROME_USER_DATA_DIR


def _chrome_instructions(port: int) -> str:
    return f"""
  START CHROME WITH REMOTE DEBUGGING
  ====================================
  1. Quit Chrome completely  (Cmd+Q on macOS).

  2. Run this in a terminal:

     macOS:
       /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
         --remote-debugging-port={port} \\
         --user-data-dir=/tmp/chrome-gemini

     Windows (PowerShell):
       & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `
         --remote-debugging-port={port} `
         --user-data-dir="C:\\Temp\\chrome-gemini"

     Linux:
       google-chrome --remote-debugging-port={port} \\
         --user-data-dir=/tmp/chrome-gemini

  3. Navigate to gemini.google.com and log in.
"""


def attach_driver(port: int = DEBUG_PORT) -> webdriver.Chrome:
    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")

    err = None
    if _HAS_WDM:
        try:
            drv = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()), options=opts
            )
            return drv
        except Exception as e:
            err = e

    try:
        drv = webdriver.Chrome(options=opts)
        return drv
    except Exception as e:
        err = e

    print(f"\n  ATTACH FAILED: {err}\n")
    print(_chrome_instructions(port))
    sys.exit(1)


def open_tab(driver: webdriver.Chrome, url: str, label: str = "") -> str:
    before = set(driver.window_handles)

    # PRIMARY: Selenium 4 native new-window command
    try:
        driver.switch_to.new_window("tab")
        after = set(driver.window_handles)
        new = after - before
        if new:
            handle = list(new)[0]
            driver.switch_to.window(handle)
            driver.get(url)
            return handle
    except Exception:
        pass

    # FALLBACK A: CDP Target.createTarget
    try:
        result = driver.execute_cdp_cmd("Target.createTarget", {"url": url})
        deadline = time.time() + 8
        while time.time() < deadline:
            time.sleep(0.4)
            after = set(driver.window_handles)
            new = after - before
            if new:
                handle = list(new)[0]
                driver.switch_to.window(handle)
                return handle
    except Exception:
        pass

    # FALLBACK B: keyboard shortcut Ctrl+T via JS event
    try:
        driver.execute_script(
            "document.dispatchEvent(new KeyboardEvent('keydown',"
            "{key:'t',code:'KeyT',ctrlKey:true,metaKey:true,bubbles:true}));"
        )
        time.sleep(2)
        after2 = set(driver.window_handles)
        new2 = after2 - before
        if new2:
            handle = list(new2)[0]
            driver.switch_to.window(handle)
            driver.get(url)
            return handle
    except Exception:
        pass

    raise RuntimeError(f"Failed to open new tab for {label}")


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

    def attach(self) -> webdriver.Chrome:
        if self.browser_mode == BROWSER_MODE_HEADLESS:
            self._launch_headless_chrome()
        self.driver = attach_driver(self.port)
        return self.driver

    def open_tab(self, label: str, url: str = GEMINI_URL, wait_seconds: float = 2.0) -> str:
        if self.driver is None:
            raise RuntimeError("BrowserSession is not attached")
        handle = open_tab(self.driver, url, label)
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
