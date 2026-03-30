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

# Response selectors
RESP_SELS = [
    "model-response .markdown",
    "model-response response-text",
    "model-response",
    "message-content .markdown",
    ".response-content .markdown",
    ".response-content",
]
