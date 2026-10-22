"""Safety and sensitive terms, blocked by code on both sides of Agent Nemo's model (CJ, 2026-10-21: 「一些安全跟敏感詞彙
還是要用程式擋下來」, a deliberate exception to 「不要程式先看」).

The list lives in config/safety/blocked_terms.yaml (editable without code) and is reloaded when the file changes.
- Input: a supervisor message that matches a category never reaches the model; the category's fixed line answers.
- Output: a model reply with a blocked term or a secret-looking string (a key or PIN value from the environment, the
  audit key, an IP address, a home / runtime path, a long hex string) is replaced by a safe line.
The caller audits each hit (kind SYSTEM, actor "safety_filter") with the matched text redacted to its first
character + ***. This is not the plant's Safety Core (warroom/safety/), which never imports the agent."""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from warroom.config import ROOT

PATH = ROOT / "config" / "safety" / "blocked_terms.yaml"
# environment variables whose values must never appear in a reply
_SECRET_ENV_RX = re.compile(r"PIN|KEY|TOKEN|PASSWORD|SECRET", re.IGNORECASE)


@dataclass(frozen=True)
class Hit:
    category: str
    matched: str
    reply: str
    side: str            # input | output

    @property
    def redacted(self) -> str:
        return redact(self.matched)

    def audit(self) -> dict[str, Any]:
        return {"msg": "blocked", "side": self.side, "category": self.category, "matched": self.redacted}


def redact(s: str) -> str:
    return (s[:1] + "***") if s else "***"


def _compile(term: str) -> re.Pattern[str]:
    if term.startswith("re:"):
        return re.compile(term[3:], re.IGNORECASE)
    esc = re.escape(term).replace(r"\ ", r"\s*")
    if re.fullmatch(r"[A-Za-z][A-Za-z ]*", term):          # an ASCII word: whole words only (「pin」 is not 「spinning」)
        esc = rf"(?<![A-Za-z]){esc}(?![A-Za-z])"
    return re.compile(esc, re.IGNORECASE)


class SafetyFilter:
    def __init__(self, path: Path = PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._allow: list[str] = []
        self.qa_allow: list[str] = []
        self._cats: list[tuple[str, str, str, list[re.Pattern[str]]]] = []
        self._out_reply = "這段回覆含有不該顯示的內容，已由系統擋下。"
        self._out: list[re.Pattern[str]] = []
        self._load()

    # ---------------------------------------------------------------- the file
    def _load(self) -> None:
        import yaml
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return                                              # no file: nothing blocked by list (secrets still are)
        if mtime == self._mtime:
            return
        with self._lock:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            cats = []
            for name, c in (data.get("categories") or {}).items():
                cats.append((name, str(c.get("reply") or "這個我不能協助。"), str(c.get("where") or "both"),
                             [_compile(str(t)) for t in c.get("terms") or []]))
            self._cats = cats
            self._allow = [str(a) for a in data.get("allow") or []]
            self.qa_allow = [str(a) for a in data.get("qa_allow") or []]
            self._out_reply = str(data.get("output_reply") or self._out_reply)
            self._out = [_compile(str(t)) for t in data.get("output_patterns") or []]
            self._mtime = mtime

    def _scan(self, text: str, side: str, extra_allow: list[str] | None = None) -> tuple[str, str, str] | None:
        self._load()                                            # cheap: one stat; re-read only when it changed
        t = text
        for a in [*self._allow, *(extra_allow or [])]:                                   # 「安全核心是什麼」 is a plant question
            t = re.sub(re.escape(a), " ", t, flags=re.IGNORECASE)
        for name, reply, where, pats in self._cats:
            if where not in (side, "both"):
                continue
            for p in pats:
                m = p.search(t)
                if m:
                    return name, m.group(0), reply
        return None

    # ---------------------------------------------------------------- the two sides
    def check_input(self, text: str, qa: bool = False) -> Hit | None:
        """qa=True: QA 模式 only (warroom/agent/qa.py) also allows the `qa_allow` words, e.g. 「可以跑 bash 嗎」 is a question
        about the framework there (CJ, 2026-10-22: 「QA 放行」). Output checks never take this."""
        hit = self._scan(text, "input", self.qa_allow if qa else None)
        return Hit(hit[0], hit[1], hit[2], "input") if hit else None

    def check_output(self, text: str, secrets: list[str] | None = None) -> Hit | None:
        for s in [*(secrets or []), *env_secrets()]:
            if s and len(s) >= 4 and s in text:
                return Hit("secret", s, self._out_reply, "output")
        for p in self._out:
            m = p.search(text)
            if m:
                return Hit("secret", m.group(0), self._out_reply, "output")
        hit = self._scan(text, "output")
        return Hit(hit[0], hit[1], self._out_reply, "output") if hit else None


def env_secrets() -> list[str]:
    return [v for k, v in os.environ.items() if _SECRET_ENV_RX.search(k) and v and len(v) >= 6 and v.lower() not in ("none", "false")]


_default: SafetyFilter | None = None


def default() -> SafetyFilter:
    global _default
    if _default is None:
        _default = SafetyFilter()
    return _default
