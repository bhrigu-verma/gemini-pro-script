r"""
GEMINI SELF-IMPROVEMENT LOOP
=============================
Two Gemini tabs locked in an automatic Writer <-> Critic cycle.

SETUP (do once, keep Chrome open):
  macOS:
    /Applications/Google Chrome.app/Contents/MacOS/Google Chrome \
      --remote-debugging-port=9222 \
      --user-data-dir=/tmp/chrome-gemini

  Windows PowerShell:
    & "C:\Program Files\Google\Chrome\Application\chrome.exe" `
      --remote-debugging-port=9222 `
      --user-data-dir="C:\Temp\chrome-gemini"

  Linux:
    google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-gemini

Then: python gemini_loop.py
"""

# ── stdlib ────────────────────────────────────────────────────────────────────
import os, sys, time, json, shutil, textwrap, datetime, traceback
from pathlib import Path
from typing  import Optional, List

# ── selenium ──────────────────────────────────────────────────────────────────
try:
    from selenium                               import webdriver
    from selenium.webdriver.chrome.options      import Options
    from selenium.webdriver.chrome.service      import Service
    from selenium.webdriver.common.by           import By
    from selenium.webdriver.common.keys         import Keys
    from selenium.webdriver.support.ui          import WebDriverWait
    from selenium.webdriver.support             import expected_conditions as EC
    from selenium.common.exceptions             import (
        TimeoutException, NoSuchElementException,
        StaleElementReferenceException, WebDriverException,
    )
except ImportError:
    sys.exit("ERROR: run  pip install selenium webdriver-manager  first.")

try:
    from webdriver_manager.chrome import ChromeDriverManager
    _HAS_WDM = True
except ImportError:
    _HAS_WDM = False


# ═════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═════════════════════════════════════════════════════════════════════════════

DEBUG_PORT       = 9222
GEMINI_URL       = "https://gemini.google.com/app"
OUTPUT_DIR       = Path("gemini_loop_output")

# How long to wait for Gemini to finish one response
RESPONSE_TIMEOUT = 180   # seconds total
STABLE_CHECKS    = 4     # identical snapshots needed to declare "done"
STABLE_INTERVAL  = 2.5   # seconds between snapshots

CRITIC_PROMPT = """\
You are a brutally honest world-class editor. Evaluate the following content \
with zero mercy. Your critique must follow this exact structure:

1. FACTUAL ERRORS / GAPS      – list every one
2. LOGICAL WEAKNESSES         – list every one
3. VAGUE / FLUFFY / PADDED    – quote the phrase, explain why
4. CONCRETE FIXES             – numbered, specific, actionable
5. SCORE: X/10  +  one-line verdict

--- CONTENT TO CRITIQUE ---
"""

IMPROVE_PROMPT_PREFIX = """\
A professional editor critiqued your last response. Rewrite the ENTIRE thing \
from scratch fixing every point. Make it substantially better.

--- CRITIQUE ---
"""
IMPROVE_PROMPT_SUFFIX = """
--- END CRITIQUE ---

Now write the fully improved version:"""


# ═════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═════════════════════════════════════════════════════════════════════════════

_LOG_PATH: Optional[Path] = None

def _now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

def log(msg: str, level: str = "INFO", tab: str = "") -> None:
    sym  = {"INFO":"·","OK":"✓","WARN":"⚠","ERR":"✗","DBG":"›","TX":"→","RX":"←"}.get(level,"·")
    tag  = f"[{tab}] " if tab else ""
    line = f"  [{_now()}] {sym} {tag}{msg}"
    print(line, flush=True)
    if _LOG_PATH:
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

def dbg(msg: str, tab: str = "") -> None:
    log(msg, "DBG", tab)

def hr(title: str = "", w: int = 72, c: str = "─") -> None:
    if title:
        pad = max(0, w - len(title) - 2)
        print(f"\n  {c*(pad//2)} {title} {c*(pad - pad//2)}\n", flush=True)
    else:
        print(f"  {c*w}", flush=True)


# ═════════════════════════════════════════════════════════════════════════════
#  CHROME  — attach to already-running instance only
# ═════════════════════════════════════════════════════════════════════════════

def _chrome_instructions(port: int) -> None:
    hr("START CHROME WITH REMOTE DEBUGGING", c="=")
    print(f"""
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

  4. Re-run:  python gemini_loop.py
""")

def attach_driver(port: int = DEBUG_PORT) -> webdriver.Chrome:
    """
    Attach Selenium to a Chrome that was ALREADY started with
    --remote-debugging-port=<port>.  Never spawns a new process.

    IMPORTANT: debuggerAddress mode forbids passing any other Chrome option.
    Passing extra options (e.g. excludeSwitches) raises
      "unrecognized chrome option: ..."
    So we use a completely bare Options() with only debuggerAddress.
    """
    log(f"Attaching to Chrome on localhost:{port} …")
    opts = Options()
    # ↓ This is the ONLY option allowed in attach mode
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")

    err = None
    if _HAS_WDM:
        try:
            dbg("Trying webdriver-manager …")
            drv = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()), options=opts
            )
            log(f"Attached! Chrome {drv.capabilities.get('browserVersion','?')}", "OK")
            return drv
        except Exception as e:
            err = e
            dbg(f"webdriver-manager failed: {e}")

    try:
        dbg("Trying system chromedriver …")
        drv = webdriver.Chrome(options=opts)
        log("Attached via system chromedriver.", "OK")
        return drv
    except Exception as e:
        err = e
        dbg(f"System chromedriver failed: {e}")

    hr("ATTACH FAILED", c="✗")
    print(f"\n  Last error: {err}\n")
    _chrome_instructions(port)
    sys.exit(1)


# ═════════════════════════════════════════════════════════════════════════════
#  NEW TAB  — the only reliable way when attached via remote-debug
# ═════════════════════════════════════════════════════════════════════════════

def open_tab(driver: webdriver.Chrome, url: str, label: str) -> str:
    """
    Open a brand-new browser tab using the Selenium 4 WebDriver protocol
    command (switch_to.new_window), NOT window.open() JS.

    window.open() called from Selenium on sites like gemini.google.com is
    silently eaten by the browser's popup-blocker — the tab appears visually
    but WebDriver never sees the new handle, causing the list-index crash.

    switch_to.new_window('tab') goes through the WebDriver protocol directly,
    bypasses the popup-blocker, and always registers the handle immediately.
    """
    before = set(driver.window_handles)
    dbg(f"[{label}] handles before: {sorted(before)}")

    # ── PRIMARY: Selenium 4 native new-window command ─────────────────────
    try:
        driver.switch_to.new_window("tab")
        after  = set(driver.window_handles)
        new    = after - before
        dbg(f"[{label}] handles after new_window: {sorted(after)}  new={new}")
        if new:
            handle = list(new)[0]
            log(f"[{label}] Tab opened: {handle}", "OK")
            driver.switch_to.window(handle)
            driver.get(url)
            return handle
    except Exception as e:
        dbg(f"[{label}] new_window('tab') failed: {e}")

    # ── FALLBACK A: CDP Target.createTarget (requires Chrome 64+) ─────────
    try:
        dbg(f"[{label}] Trying CDP createTarget …")
        result = driver.execute_cdp_cmd("Target.createTarget", {"url": url})
        target_id = result.get("targetId", "")
        dbg(f"[{label}] CDP targetId: {target_id}")
        # give Chrome a moment to register the handle
        deadline = time.time() + 8
        while time.time() < deadline:
            time.sleep(0.4)
            after = set(driver.window_handles)
            new   = after - before
            if new:
                handle = list(new)[0]
                log(f"[{label}] Tab opened via CDP: {handle}", "OK")
                driver.switch_to.window(handle)
                return handle
        dbg(f"[{label}] CDP target created but handle not visible — continuing")
    except Exception as e:
        dbg(f"[{label}] CDP fallback failed: {e}")

    # ── FALLBACK B: keyboard shortcut Ctrl+T via JS event ─────────────────
    try:
        dbg(f"[{label}] Trying Ctrl+T simulation …")
        before2 = set(driver.window_handles)
        driver.execute_script(
            "document.dispatchEvent(new KeyboardEvent('keydown',"
            "{key:'t',code:'KeyT',ctrlKey:true,metaKey:true,bubbles:true}));"
        )
        time.sleep(2)
        after2 = set(driver.window_handles)
        new2   = after2 - before2
        if new2:
            handle = list(new2)[0]
            log(f"[{label}] Tab opened via Ctrl+T sim: {handle}", "OK")
            driver.switch_to.window(handle)
            driver.get(url)
            return handle
    except Exception as e:
        dbg(f"[{label}] Ctrl+T fallback failed: {e}")

    # ── GIVE UP ────────────────────────────────────────────────────────────
    hr("TAB OPEN FAILED", c="✗")
    print(f"""
  Could not open a new browser tab for [{label}].

  This usually means your ChromeDriver version does not match Chrome {
      driver.capabilities.get('browserVersion','?')}.

  Fix:  pip install --upgrade webdriver-manager
        pip install --upgrade selenium

  Or download the matching chromedriver from:
    https://googlechromelabs.github.io/chrome-for-testing/
""")
    sys.exit(1)


# ═════════════════════════════════════════════════════════════════════════════
#  DOM PROBE  — log which selectors actually exist right now
# ═════════════════════════════════════════════════════════════════════════════

# Input box
INPUT_SELS = [
    "rich-textarea div[contenteditable='true']",
    "rich-textarea p",
    "div[contenteditable='true'][data-placeholder]",
    "div[contenteditable='true']",
    ".ql-editor",
    "textarea",
]
# Send button
SEND_SELS = [
    "button[aria-label='Send message']",
    "button[jsname='Qx7uuf']",
    "button[data-testid='send-button']",
    "button[mattooltip='Send message']",
    "button[aria-label='Submit']",
    "button.send-button",
    "mat-icon[data-mat-icon-name='send']",
]
# Stop streaming button (visible while Gemini is generating)
STOP_SELS = [
    "button[aria-label='Stop response']",
    "button[aria-label='Stop generating']",
    "button[jsname='k9Ysde']",
    "button[data-testid='stop-button']",
    ".stop-button",
]
# Model response blocks
RESP_SELS = [
    "model-response .markdown",
    "model-response response-text",
    "model-response",
    "message-content .markdown",
    ".response-content .markdown",
    ".response-content",
]


def probe_dom(driver: webdriver.Chrome, label: str) -> None:
    """Print a hit-count table for every selector group."""
    hr(f"DOM PROBE [{label}]", c="=")
    for group, sels in [
        ("INPUT",    INPUT_SELS),
        ("SEND BTN", SEND_SELS),
        ("STOP BTN", STOP_SELS),
        ("RESPONSE", RESP_SELS),
    ]:
        log(f"── {group} ──", tab=label)
        for s in sels:
            try:
                n = len(driver.find_elements(By.CSS_SELECTOR, s))
            except Exception:
                n = 0
            mark = "FOUND" if n else "     "
            dbg(f"  [{mark}] ({n})  {s}", tab=label)
    try:
        dbg(f"  title = {driver.title!r}", tab=label)
        dbg(f"  url   = {driver.current_url}", tab=label)
        body  = driver.execute_script(
            "return document.body ? document.body.innerText.length : 0"
        )
        dbg(f"  body.innerText length = {body}", tab=label)
    except Exception as e:
        dbg(f"  page-info error: {e}", tab=label)
    hr()


# ═════════════════════════════════════════════════════════════════════════════
#  GEMINI TAB  — all interaction with one chat window
# ═════════════════════════════════════════════════════════════════════════════

class GeminiTab:

    def __init__(self, driver: webdriver.Chrome, handle: str, name: str):
        self.driver = driver
        self.handle = handle
        self.name   = name

    # ── focus ────────────────────────────────────────────────────────────
    def focus(self) -> None:
        try:
            self.driver.switch_to.window(self.handle)
        except WebDriverException as e:
            log(f"Lost session: {e}", "ERR", self.name)
            raise

    def probe(self) -> None:
        self.focus()
        probe_dom(self.driver, self.name)

    # ── find first matching element ───────────────────────────────────────
    def _find(self, sels: List[str], timeout: int = 12,
              what: str = "element"):
        self.focus()
        for s in sels:
            try:
                el = WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, s))
                )
                dbg(f"Found {what} via: {s}", tab=self.name)
                return el
            except TimeoutException:
                dbg(f"Not found ({what}): {s}", tab=self.name)
        raise TimeoutException(
            f"[{self.name}] Nothing matched for '{what}': {sels}"
        )

    # ── type into contenteditable ─────────────────────────────────────────
    def _type_text(self, el, text: str) -> None:
        """
        Use execCommand('insertText') — the only method that:
        - works on contenteditable divs (not just <textarea>)
        - fires the React/Angular synthetic events Gemini listens to
        - doesn't trip paste-detection
        """
        dbg(f"Typing {len(text)} chars …", tab=self.name)
        # 1. focus & clear
        self.driver.execute_script(
            "arguments[0].focus();"
            "document.execCommand('selectAll',false,null);"
            "document.execCommand('delete',false,null);",
            el,
        )
        time.sleep(0.15)

        # 2. insert in 400-char chunks
        for i in range(0, len(text), 400):
            chunk = text[i:i+400]
            # escape backticks and backslashes for the template literal
            safe  = chunk.replace("\\", "\\\\").replace("`", "\\`")
            self.driver.execute_script(
                "document.execCommand('insertText',false,`" + safe + "`);",
                el,
            )
            time.sleep(0.04)

        # 3. verify
        try:
            placed = self.driver.execute_script(
                "return arguments[0].innerText||arguments[0].value||'';", el
            )
            dbg(f"Input verified: {len(placed)} chars placed", tab=self.name)
        except Exception:
            pass

    # ── click send button ─────────────────────────────────────────────────
    def _click_send(self) -> bool:
        self.focus()
        for s in SEND_SELS:
            els = self.driver.find_elements(By.CSS_SELECTOR, s)
            if not els:
                continue
            el = els[0]
            # walk up to <button> if we matched an inner icon
            if el.tag_name.lower() != "button":
                try:
                    el = el.find_element(By.XPATH, "ancestor::button[1]")
                except NoSuchElementException:
                    pass
            try:
                self.driver.execute_script("arguments[0].click();", el)
                dbg(f"Send clicked via JS: {s}", tab=self.name)
                return True
            except Exception:
                try:
                    el.click()
                    dbg(f"Send clicked direct: {s}", tab=self.name)
                    return True
                except Exception as e:
                    dbg(f"Click failed ({s}): {e}", tab=self.name)
        return False

    # ── is Gemini still streaming? ────────────────────────────────────────
    def _streaming(self) -> bool:
        self.focus()
        for s in STOP_SELS:
            try:
                if self.driver.find_element(By.CSS_SELECTOR, s).is_displayed():
                    return True
            except NoSuchElementException:
                pass
        return False

    # ── extract text of the last response block ───────────────────────────
    def _last_text(self) -> str:
        self.focus()
        for s in RESP_SELS:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, s)
                if els:
                    t = els[-1].text.strip()
                    if t:
                        return t
            except StaleElementReferenceException:
                continue
        # JS fallback
        try:
            t = self.driver.execute_script("""
                var tags=['model-response','.response-content','[data-chunk-index]'];
                for(var i=0;i<tags.length;i++){
                    var nodes=document.querySelectorAll(tags[i]);
                    if(nodes.length) return nodes[nodes.length-1].innerText;
                }
                return '';
            """)
            if t and t.strip():
                dbg("Text captured via JS fallback.", tab=self.name)
                return t.strip()
        except Exception as e:
            dbg(f"JS text fallback error: {e}", tab=self.name)
        return ""

    # ── count response blocks (used to detect new response) ───────────────
    def _resp_count(self) -> int:
        self.focus()
        for s in RESP_SELS:
            n = len(self.driver.find_elements(By.CSS_SELECTOR, s))
            if n:
                return n
        return 0

    # ─────────────────────────────────────────────────────────────────────
    #  PUBLIC: send a message
    # ─────────────────────────────────────────────────────────────────────
    def send(self, text: str) -> None:
        self.focus()
        log(f"Sending {len(text):,} chars …", "TX", self.name)

        # locate input box
        el = self._find(INPUT_SELS, timeout=15, what="input box")
        self._type_text(el, text)
        time.sleep(0.6)

        # send
        if not self._click_send():
            log("Send button not found — pressing Enter.", "WARN", self.name)
            try:
                el.send_keys(Keys.RETURN)
            except Exception as e:
                log(f"Enter fallback failed: {e}", "ERR", self.name)

        log("Dispatched.", "OK", self.name)

    # ─────────────────────────────────────────────────────────────────────
    #  PUBLIC: wait for response and return its text
    # ─────────────────────────────────────────────────────────────────────
    def recv(self) -> str:
        self.focus()
        log("Waiting for response …", "RX", self.name)

        count_before = self._resp_count()
        dbg(f"Response blocks before send: {count_before}", tab=self.name)

        # Phase 1 — wait for streaming to START (up to 15 s)
        started = False
        t0 = time.time()
        while time.time() - t0 < 15:
            if self._streaming() or self._resp_count() > count_before:
                dbg("Streaming started.", tab=self.name)
                started = True
                break
            time.sleep(0.5)
        if not started:
            log("Stream-start not detected — continuing anyway.", "WARN", self.name)

        # Phase 2 — wait for streaming to FINISH
        deadline  = time.time() + RESPONSE_TIMEOUT
        last_text = ""
        stable    = 0
        tick      = 0

        while time.time() < deadline:
            time.sleep(STABLE_INTERVAL)
            tick += 1
            live = self._streaming()
            cur  = self._last_text()

            if tick % 5 == 0:
                dbg(f"tick={tick} streaming={live} len={len(cur)} stable={stable}",
                    tab=self.name)

            if live:
                stable    = 0
                last_text = cur
                continue

            if cur and cur == last_text:
                stable += 1
                dbg(f"Stable {stable}/{STABLE_CHECKS} ({len(cur):,} chars)",
                    tab=self.name)
                if stable >= STABLE_CHECKS:
                    log(f"Response ready: {len(cur):,} chars.", "OK", self.name)
                    return cur
            else:
                stable    = 0
                last_text = cur

        log("Timed out — returning best capture.", "WARN", self.name)
        return last_text or "[RESPONSE TIMED OUT]"

    # ── screenshot ────────────────────────────────────────────────────────
    def screenshot(self, path: str) -> None:
        self.focus()
        try:
            self.driver.save_screenshot(path)
        except Exception as e:
            log(f"Screenshot failed: {e}", "WARN", self.name)

    # ── dump page source for post-mortem debugging ────────────────────────
    def dump_dom(self, tag: str = "dom") -> None:
        self.focus()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        p  = OUTPUT_DIR / f"{tag}_{self.name.lower()}_{ts}.html"
        try:
            p.write_text(self.driver.page_source, encoding="utf-8")
            log(f"DOM dumped → {p}", tab=self.name)
        except Exception as e:
            log(f"DOM dump failed: {e}", "WARN", self.name)


# ═════════════════════════════════════════════════════════════════════════════
#  ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════

class GeminiLoop:

    def __init__(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        global _LOG_PATH
        _LOG_PATH = OUTPUT_DIR / (
            "run_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log"
        )
        log(f"Log → {_LOG_PATH}")
        self.driver:   Optional[webdriver.Chrome] = None
        self.improver: Optional[GeminiTab]        = None
        self.critic:   Optional[GeminiTab]        = None
        self.history:  List[dict]                 = []

    # ── setup ─────────────────────────────────────────────────────────────
    def setup(self) -> None:
        hr("GEMINI SELF-IMPROVEMENT LOOP", c="=")
        print("""
  Needs:
    * Chrome running with --remote-debugging-port=9222 --user-data-dir=...
    * Logged into gemini.google.com in that window
""")
        input("  Press ENTER to connect … ")

        self.driver = attach_driver(DEBUG_PORT)

        # sanity-check session
        try:
            n = len(self.driver.window_handles)
            log(f"Session OK. Open windows: {n}", "OK")
        except Exception as e:
            log(f"Session broken right after attach: {e}", "ERR")
            sys.exit(1)

        # ── open two Gemini tabs using WebDriver protocol ─────────────────
        #
        #  WHY NOT window.open()?
        #  gemini.google.com blocks window.open() calls that aren't initiated
        #  by a user gesture.  The call appears to succeed but Chrome never
        #  registers a new window handle with the WebDriver session.
        #
        #  driver.switch_to.new_window('tab') goes through the W3C WebDriver
        #  "New Window" endpoint, completely bypassing the popup-blocker.
        #  It always returns a visible, registered handle immediately.

        log("Opening Improver tab (WebDriver new_window) …")
        imp_handle = open_tab(self.driver, GEMINI_URL, "IMPROVER")
        time.sleep(5)
        self.improver = GeminiTab(self.driver, imp_handle, "IMPROVER")
        log("Improver ready.", "OK")
        self.improver.probe()

        log("Opening Critic tab (WebDriver new_window) …")
        crit_handle = open_tab(self.driver, GEMINI_URL, "CRITIC")
        time.sleep(5)
        self.critic = GeminiTab(self.driver, crit_handle, "CRITIC")
        log("Critic ready.", "OK")
        self.critic.probe()

        # login check
        for tab in (self.improver, self.critic):
            tab.focus()
            url = self.driver.current_url
            dbg(f"URL: {url}", tab=tab.name)
            if "accounts.google.com" in url or "signin" in url.lower():
                hr("LOGIN REQUIRED", c="!")
                print(f"  [{tab.name}] is showing the login page.")
                print("  Log in inside Chrome, then press ENTER.\n")
                input()

    # ── save round ────────────────────────────────────────────────────────
    def _save(self, rnd: int, version: str, critique: str) -> None:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.improver.screenshot(str(OUTPUT_DIR / f"r{rnd:02d}_improver_{ts}.png"))
        self.critic.screenshot(str(OUTPUT_DIR / f"r{rnd:02d}_critic_{ts}.png"))
        self.history.append({"round": rnd, "ts": ts,
                             "version": version, "critique": critique})
        (OUTPUT_DIR / "history.json").write_text(
            json.dumps(self.history, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log(f"Round {rnd} saved → {OUTPUT_DIR}/", "OK")

    # ── show preview ──────────────────────────────────────────────────────
    @staticmethod
    def _show(label: str, text: str, n: int = 420) -> None:
        hr(label)
        for line in textwrap.wrap(text[:n], 70):
            print(f"    {line}")
        if len(text) > n:
            print(f"    … [{len(text)-n} more chars]")
        hr()

    # ── main ──────────────────────────────────────────────────────────────
    def run(self) -> None:
        self.setup()

        hr("YOUR PROMPT", c="=")
        print("  Type / paste your starting request. ENTER twice to submit.\n")
        lines: List[str] = []
        while True:
            line = input()
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)
        prompt = "\n".join(lines).strip()
        if not prompt:
            print("No prompt — exiting.")
            return

        # round 0: first draft
        hr("ROUND 0 — FIRST DRAFT", c="=")
        try:
            self.improver.send(prompt)
            version = self.improver.recv()
        except Exception as e:
            log(f"Round 0 crashed: {e}", "ERR")
            self.improver.dump_dom("crash_r0")
            traceback.print_exc()
            return
        self._show("VERSION 0", version)

        # iterative loop
        rnd = 1
        while True:
            hr(f"ROUND {rnd}", c="─")
            print(f"  ENTER = run round {rnd}   |   'stop' = finish\n")
            if input("  > ").strip().lower() in ("stop","quit","q","done","exit"):
                break

            # critique
            try:
                self.critic.send(CRITIC_PROMPT + version)
                critique = self.critic.recv()
            except Exception as e:
                log(f"Critic crashed: {e}", "ERR")
                self.critic.dump_dom(f"crash_r{rnd}_critic")
                traceback.print_exc()
                break
            self._show(f"CRITIQUE round {rnd}", critique)

            # improve
            try:
                self.improver.send(IMPROVE_PROMPT_PREFIX + critique + IMPROVE_PROMPT_SUFFIX)
                version = self.improver.recv()
            except Exception as e:
                log(f"Improver crashed: {e}", "ERR")
                self.improver.dump_dom(f"crash_r{rnd}_improver")
                traceback.print_exc()
                break
            self._show(f"VERSION {rnd}", version)
            self._save(rnd, version, critique)
            rnd += 1

        hr("DONE", c="=")
        print(f"  Rounds: {rnd-1}   Output: {OUTPUT_DIR.resolve()}\n")
        final = OUTPUT_DIR / "final_version.txt"
        final.write_text(version, encoding="utf-8")
        log(f"Final version → {final}", "OK")
        print("  Browser left open. Close Chrome manually when done.")


# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    loop = GeminiLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\n\n  Interrupted.")
    except Exception as e:
        log(f"Unhandled: {e}", "ERR")
        traceback.print_exc()