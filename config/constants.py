"""Shared constants and selector lists for Gemini automation scripts."""

from pathlib import Path

DEBUG_PORT = 9222
GEMINI_URL = "https://gemini.google.com/app"

DEFAULT_OUTPUT_DIR = Path("automation_output")

BROWSER_MODE_ATTACH = "attach"
BROWSER_MODE_HEADLESS = "headless"

DEFAULT_SPOOFED_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

CHROME_ANTI_BOT_FLAGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-breakpad",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]

GEMINI_PRIMARY_MODEL = "gemini-3.1-pro"
GEMINI_FALLBACK_MODELS = [
    "gemini-3-flash",
    "gemini-3.1-flash-lite",
]

# Input box selectors
INPUT_SELS = [
    "rich-textarea div[contenteditable='true']",
    "rich-textarea p",
    "div[contenteditable='true'][data-placeholder]",
    "div[contenteditable='true']",
    ".ql-editor",
    "textarea",
]

# Send button selectors
SEND_SELS = [
    "button[aria-label='Send message']",
    "button[jsname='Qx7uuf']",
    "button[data-testid='send-button']",
    "button[mattooltip='Send message']",
    "button[aria-label='Submit']",
    "button.send-button",
    "mat-icon[data-mat-icon-name='send']",
]

# Stop streaming selectors
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
    "message-content",
    "[data-turn-role='model']",
    "[data-message-author-role='model']",
    "message-content .markdown",
    ".response-content .markdown",
    ".response-content",
]

# Prompt Templates
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
