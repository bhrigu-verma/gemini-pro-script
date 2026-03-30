r"""
GEMINI SELF-IMPROVEMENT LOOP (HEADLESS ATTACH)
==============================================
Same Writer <-> Critic logic as main.py, but intended to attach to a
headless Chrome debug instance.

SETUP (do once, keep Chrome open):
  macOS:
    /Applications/Google Chrome.app/Contents/MacOS/Google Chrome \
      --remote-debugging-port=9222 \
            --user-data-dir=/tmp/chrome-gemini-headless \
            --headless=new

  Windows PowerShell:
    & "C:\Program Files\Google\Chrome\Application\chrome.exe" `
      --remote-debugging-port=9222 `
      --user-data-dir="C:\Temp\chrome-gemini"

  Linux:
        google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-gemini-headless --headless=new

Then: python mainh.py
"""

# ── stdlib ────────────────────────────────────────────────────────────────────
import os, sys, time, json, shutil, textwrap, datetime, traceback, random, gc
import shlex, socket, subprocess
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
AUTO_LAUNCH_WAIT = int(os.environ.get("AUTO_LAUNCH_WAIT", "20"))
CHROME_LOG_FILE  = Path(os.environ.get("CHROME_LOG_FILE", "/tmp/chrome-headless.log"))

# How long to wait for Gemini to finish one response
RESPONSE_TIMEOUT = 180   # seconds total
STABLE_CHECKS    = 4     # identical snapshots needed to declare "done"
STABLE_INTERVAL  = 2.5   # seconds between snapshots

# Long-run hardening (env-overridable)
INTER_ROUND_SLEEP_MIN = int(os.environ.get("INTER_ROUND_SLEEP_MIN", "45"))
INTER_ROUND_SLEEP_MAX = int(os.environ.get("INTER_ROUND_SLEEP_MAX", "90"))
RATE_LIMIT_BACKOFF = [
    int(v.strip())
    for v in os.environ.get("RATE_LIMIT_BACKOFF", "60,120,300").split(",")
    if v.strip()
]
CONTEXT_RESET_INTERVAL = int(os.environ.get("CONTEXT_RESET_INTERVAL", "15"))
SCREENSHOT_INTERVAL = int(os.environ.get("SCREENSHOT_INTERVAL", "10"))
SCREENSHOT_ON_ERROR = os.environ.get("SCREENSHOT_ON_ERROR", "1") == "1"
MEMORY_CLEAN_INTERVAL = int(os.environ.get("MEMORY_CLEAN_INTERVAL", "20"))
AUTO_MAX_ROUNDS = int(os.environ.get("AUTO_MAX_ROUNDS", "0"))

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


def _ask_yes_no(prompt: str) -> bool:
    while True:
        ans = input(prompt).strip().lower()
        if ans in ("yes", "y"):
            return True
        if ans in ("no", "n"):
            return False
        print("  Please type yes or no.")


def _debug_port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.8):
            return True
    except OSError:
        return False


def _chrome_launch_cmd(port: int) -> List[str]:
    if sys.platform == "darwin":
        chrome_bin = os.environ.get(
            "CHROME_BIN",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        user_data = os.environ.get("CHROME_USER_DATA_DIR", "/tmp/chrome-gemini-headless")
        return [
            chrome_bin,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data}",
            "--headless=new",
        ]

    if os.name == "nt":
        chrome_bin = os.environ.get(
            "CHROME_BIN",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        )
        user_data = os.environ.get("CHROME_USER_DATA_DIR", r"C:\Temp\chrome-gemini-headless")
        return [
            chrome_bin,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data}",
            "--headless=new",
        ]

    chrome_bin = os.environ.get("CHROME_BIN", "google-chrome")
    user_data = os.environ.get("CHROME_USER_DATA_DIR", "/tmp/chrome-gemini-headless")
    return [
        chrome_bin,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        "--headless=new",
    ]


def maybe_launch_chrome(port: int) -> None:
    if _debug_port_open(port):
        log(f"Debug port {port} is already open. Using existing Chrome session.", "OK")
        return

    cmd = _chrome_launch_cmd(port)
    log("Starting Chrome with remote-debugging flags…", "INFO")
    dbg("Launch command: " + shlex.join(cmd))

    CHROME_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with CHROME_LOG_FILE.open("a", encoding="utf-8") as out:
            subprocess.Popen(
                cmd,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except FileNotFoundError:
        hr("CHROME LAUNCH FAILED", c="✗")
        print(
            "\n  Chrome executable not found.\n"
            "  Set CHROME_BIN env var or start Chrome manually, then rerun.\n"
        )
        _chrome_instructions(port)
        sys.exit(1)
    except Exception as e:
        hr("CHROME LAUNCH FAILED", c="✗")
        print(f"\n  Failed to launch Chrome: {e}\n")
        _chrome_instructions(port)
        sys.exit(1)

    deadline = time.time() + AUTO_LAUNCH_WAIT
    while time.time() < deadline:
        if _debug_port_open(port):
            log(f"Chrome debug endpoint is ready on localhost:{port}", "OK")
            return
        time.sleep(0.5)

    log(
        f"Launch command executed, but localhost:{port} did not open within {AUTO_LAUNCH_WAIT}s.",
        "WARN",
    )
    log("Will still attempt attach; if it fails, check the Chrome log file above.", "WARN")


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
                 --user-data-dir=/tmp/chrome-gemini-headless \\
                 --headless=new

     Windows (PowerShell):
       & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `
         --remote-debugging-port={port} `
         --user-data-dir="C:\\Temp\\chrome-gemini"

     Linux:
       google-chrome --remote-debugging-port={port} \\
                 --user-data-dir=/tmp/chrome-gemini-headless --headless=new

  3. Navigate to gemini.google.com and log in.

    4. Re-run:  python mainh.py
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
    "div[contenteditable='true'][data-placeholder]",
    "rich-textarea div[contenteditable='true']",
    "rich-textarea p",
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
    "button[aria-label='Stop']",
    "button[jsname='k9Ysde']",
    "button[data-testid='stop-button']",
    ".stop-button",
]
# Model response blocks
RESP_SELS = [
    "model-response .markdown",
    "model-response response-text",
    "model-response",
    "message-content",
    "[data-turn-role='model']",
    "[data-message-author-role='model']",
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
            # Avoid redundant tab activation calls that can steal OS focus.
            if self.driver.current_window_handle != self.handle:
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
        if what == "input box":
            # Gemini frequently renders multiple contenteditable nodes.
            # Prefer a visible, non-zero sized element to avoid typing into
            # hidden/ghost editors that lead to no-op sends.
            deadline = time.time() + timeout
            while time.time() < deadline:
                for s in sels:
                    try:
                        els = self.driver.find_elements(By.CSS_SELECTOR, s)
                    except Exception:
                        continue

                    if not els:
                        continue

                    visible = []
                    for el in els:
                        try:
                            sz = el.size or {}
                            if (
                                el.is_displayed()
                                and sz.get("width", 0) > 0
                                and sz.get("height", 0) > 0
                            ):
                                visible.append(el)
                        except StaleElementReferenceException:
                            continue
                        except Exception:
                            continue

                    if visible:
                        picked = visible[-1]
                        dbg(
                            f"Found {what} via: {s} (visible {len(visible)}/{len(els)})",
                            tab=self.name,
                        )
                        return picked

                time.sleep(0.2)

            raise TimeoutException(
                f"[{self.name}] Nothing matched for '{what}': {sels}"
            )

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
        for s in STOP_SELS:
            try:
                if self.driver.find_element(By.CSS_SELECTOR, s).is_displayed():
                    return True
            except NoSuchElementException:
                pass
        return False

    # ── extract text of the last response block ───────────────────────────
    def _last_text(self) -> str:
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

        # Deep fallback for modern Gemini layouts where chat content lives
        # in nested custom elements / shadow roots.
        try:
            t = self.driver.execute_script("""
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return false;
                    const style = window.getComputedStyle(el);
                    return style && style.display !== 'none' && style.visibility !== 'hidden';
                }

                function roots() {
                    const out = [document];
                    const stack = [document.documentElement];
                    while (stack.length) {
                        const n = stack.pop();
                        if (!n) continue;
                        if (n.shadowRoot) {
                            out.push(n.shadowRoot);
                            stack.push(n.shadowRoot);
                        }
                        const kids = n.children || [];
                        for (let i = 0; i < kids.length; i++) stack.push(kids[i]);
                    }
                    return out;
                }

                const bad = /(send message|new chat|gemini can make mistakes)/i;
                const cand = [];
                for (const root of roots()) {
                    const nodes = root.querySelectorAll(
                        'model-response, message-content, [data-turn-role="model"], [data-message-author-role="model"], [role="article"], .response-content, .markdown'
                    );
                    for (const n of nodes) {
                        if (!visible(n)) continue;
                        const txt = (n.innerText || '').trim();
                        if (txt.length < 8) continue;
                        if (bad.test(txt) && txt.length < 120) continue;
                        const y = n.getBoundingClientRect().top;
                        cand.push({ y, len: txt.length, txt });
                    }
                }

                if (!cand.length) return '';
                cand.sort((a, b) => (a.y - b.y) || (a.len - b.len));
                return cand[cand.length - 1].txt || '';
            """)
            if t and t.strip():
                dbg("Text captured via deep shadow fallback.", tab=self.name)
                return t.strip()
        except Exception as e:
            dbg(f"Deep fallback error: {e}", tab=self.name)
        return ""

    # ── count response blocks (used to detect new response) ───────────────
    def _resp_count(self) -> int:
        for s in RESP_SELS:
            n = len(self.driver.find_elements(By.CSS_SELECTOR, s))
            if n:
                return n

        # Shadow-root aware fallback for modern Gemini chat trees.
        try:
            n = self.driver.execute_script("""
                function roots() {
                    const out = [document];
                    const stack = [document.documentElement];
                    while (stack.length) {
                        const n = stack.pop();
                        if (!n) continue;
                        if (n.shadowRoot) {
                            out.push(n.shadowRoot);
                            stack.push(n.shadowRoot);
                        }
                        const kids = n.children || [];
                        for (let i = 0; i < kids.length; i++) stack.push(kids[i]);
                    }
                    return out;
                }

                const sels = [
                    'model-response',
                    'message-content',
                    '[data-turn-role="model"]',
                    '[data-message-author-role="model"]',
                    '.response-content'
                ];

                let count = 0;
                for (const root of roots()) {
                    for (const s of sels) {
                        count += root.querySelectorAll(s).length;
                    }
                }
                return count;
            """)
            if n:
                return int(n)
        except Exception:
            pass
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
        if SCREENSHOT_ON_ERROR and not last_text:
            try:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                self.screenshot(str(OUTPUT_DIR / f"timeout_{self.name.lower()}_{ts}.png"))
                self.dump_dom(f"timeout_{self.name.lower()}")
            except Exception as e:
                dbg(f"Timeout diagnostics capture failed: {e}", tab=self.name)
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

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "429" in text
            or "rate limit" in text
            or "quota" in text
            or "too many requests" in text
            or "resource_exhausted" in text
        )

    def _sleep_with_jitter(self) -> None:
        if INTER_ROUND_SLEEP_MAX <= 0:
            return
        lower = max(0, INTER_ROUND_SLEEP_MIN)
        upper = max(lower, INTER_ROUND_SLEEP_MAX)
        delay = random.randint(lower, upper)
        log(f"Cooling down {delay}s before next round.", "INFO")
        time.sleep(delay)

    def _ensure_logged_in(self, tab: GeminiTab) -> bool:
        try:
            tab.focus()
            url = (self.driver.current_url or "").lower()
            if "gemini.google.com" not in url:
                return False
            tab._find(INPUT_SELS, timeout=2, what="input box")
            return True
        except Exception:
            return False

    def _pause_for_relogin(self, tab: GeminiTab) -> None:
        hr("SESSION HEALTH CHECK", c="!")
        print(f"  [{tab.name}] session appears expired or input not ready.")
        print("  Re-login in Chrome, then press ENTER to continue.\n")
        input()

    def _new_tab_client(self, label: str) -> GeminiTab:
        handle = open_tab(self.driver, GEMINI_URL, label)
        time.sleep(4)
        tab = GeminiTab(self.driver, handle, label)
        tab.probe()
        return tab

    def _context_reset(self, best_version: str, rnd: int) -> None:
        if CONTEXT_RESET_INTERVAL <= 0 or rnd % CONTEXT_RESET_INTERVAL != 0:
            return
        log(f"Context reset at round {rnd}: reopening Gemini tabs.", "INFO")
        self.improver = self._new_tab_client("IMPROVER")
        self.critic = self._new_tab_client("CRITIC")
        seed = (
            "Here is the current best version:\n\n"
            f"{best_version}\n\n"
            "Continue improving from this baseline."
        )
        try:
            self.improver.send(seed)
            _ = self.improver.recv()
        except Exception as e:
            log(f"Context reseed failed: {e}", "WARN")

    def _memory_cleanup(self, rnd: int) -> None:
        if MEMORY_CLEAN_INTERVAL <= 0 or rnd % MEMORY_CLEAN_INTERVAL != 0:
            return
        try:
            self.driver.execute_script("window.gc && window.gc()")
        except Exception:
            pass
        gc.collect()

    # ── setup ─────────────────────────────────────────────────────────────
    def setup(self) -> None:
        hr("GEMINI SELF-IMPROVEMENT LOOP", c="=")
        print(f"""
  What this script does:
    1) Uses Chrome DevTools endpoint on localhost:{DEBUG_PORT}
    2) Opens two Gemini tabs (Improver + Critic)
    3) Runs iterative self-improvement rounds

  If you answer yes, this script will auto-run Chrome with:
    --remote-debugging-port={DEBUG_PORT}
    --user-data-dir (headless profile)
    --headless=new
""")
        if _ask_yes_no("  Launch Chrome automatically now? (yes/no): "):
            maybe_launch_chrome(DEBUG_PORT)
        else:
            log("Auto-launch skipped. Expecting Chrome to already be running.", "INFO")

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
        if SCREENSHOT_INTERVAL > 0 and rnd % SCREENSHOT_INTERVAL == 0:
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
            if AUTO_MAX_ROUNDS > 0:
                if rnd > AUTO_MAX_ROUNDS:
                    log(f"AUTO_MAX_ROUNDS reached ({AUTO_MAX_ROUNDS}).", "OK")
                    break
            else:
                print(f"  ENTER = run round {rnd}   |   'stop' = finish\n")
                if input("  > ").strip().lower() in ("stop","quit","q","done","exit"):
                    break

            for tab in (self.improver, self.critic):
                if not self._ensure_logged_in(tab):
                    self._pause_for_relogin(tab)

            self._context_reset(version, rnd)

            # critique
            critique = ""
            for idx, backoff in enumerate([0] + RATE_LIMIT_BACKOFF):
                try:
                    if backoff:
                        log(f"Rate-limit backoff before critic retry: {backoff}s", "WARN")
                        time.sleep(backoff)
                    self.critic.send(CRITIC_PROMPT + version)
                    critique = self.critic.recv()
                    break
                except Exception as e:
                    if self._is_rate_limit_error(e) and idx < len(RATE_LIMIT_BACKOFF):
                        continue
                    log(f"Critic crashed: {e}", "ERR")
                    if SCREENSHOT_ON_ERROR:
                        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        self.critic.screenshot(str(OUTPUT_DIR / f"err_r{rnd:02d}_critic_{ts}.png"))
                    self.critic.dump_dom(f"crash_r{rnd}_critic")
                    traceback.print_exc()
                    return
            self._show(f"CRITIQUE round {rnd}", critique)

            # improve
            for idx, backoff in enumerate([0] + RATE_LIMIT_BACKOFF):
                try:
                    if backoff:
                        log(f"Rate-limit backoff before improver retry: {backoff}s", "WARN")
                        time.sleep(backoff)
                    self.improver.send(IMPROVE_PROMPT_PREFIX + critique + IMPROVE_PROMPT_SUFFIX)
                    version = self.improver.recv()
                    break
                except Exception as e:
                    if self._is_rate_limit_error(e) and idx < len(RATE_LIMIT_BACKOFF):
                        continue
                    log(f"Improver crashed: {e}", "ERR")
                    if SCREENSHOT_ON_ERROR:
                        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        self.improver.screenshot(str(OUTPUT_DIR / f"err_r{rnd:02d}_improver_{ts}.png"))
                    self.improver.dump_dom(f"crash_r{rnd}_improver")
                    traceback.print_exc()
                    return
            self._show(f"VERSION {rnd}", version)
            self._save(rnd, version, critique)
            self._memory_cleanup(rnd)
            self._sleep_with_jitter()
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