"""Gemini tab client built from immutable main.py GeminiTab."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import main as core


@dataclass
class GeminiClient:
    tab: core.GeminiTab
    use_cdp_activation: bool = True
    _target_id: Optional[str] = None

    def _resolve_target_id(self) -> Optional[str]:
        if self._target_id:
            return self._target_id
        try:
            result = self.tab.driver.execute_cdp_cmd("Target.getTargets", {})
            infos = result.get("targetInfos", [])
            # Try to locate an existing Gemini page target.
            for info in infos:
                url = str(info.get("url", ""))
                if "gemini.google.com" in url and info.get("type") == "page":
                    self._target_id = str(info.get("targetId", ""))
                    if self._target_id:
                        return self._target_id
        except Exception:
            return None
        return None

    def _activate_target_background(self) -> bool:
        if not self.use_cdp_activation:
            return False
        target_id = self._resolve_target_id()
        if not target_id:
            return False
        try:
            self.tab.driver.execute_cdp_cmd("Target.activateTarget", {"targetId": target_id})
            return True
        except Exception:
            return False

    def _prepare_context(self) -> None:
        # Best-effort background activation via CDP to reduce explicit window switching.
        if not self._activate_target_background():
            self.tab.focus()

    def send_prompt(self, prompt: str) -> None:
        self._prepare_context()
        self.tab.send(prompt)

    def wait_response(self) -> str:
        self._prepare_context()
        return self.tab.recv()

    # Compatibility aliases so existing send/recv call sites can reuse GeminiClient.
    def send(self, prompt: str) -> None:
        self.send_prompt(prompt)

    def recv(self) -> str:
        return self.wait_response()

    def ask(self, prompt: str) -> str:
        self.send_prompt(prompt)
        return self.wait_response()

    def screenshot(self, path: str) -> None:
        self.tab.screenshot(path)

    def dump_dom(self, tag: str = "dom") -> None:
        self.tab.dump_dom(tag)

    def probe(self) -> None:
        self.tab.probe()
