"""Plant-instructions layer of the system prompts (issue #37; CJ, 2026-10-20: 「PLANT.md 現在做」; refs #34).

Two layers. The core prompts stay in code (commander.SYSTEM_PROMPT, ask.PLAN_SYSTEM / ANSWER_SYSTEM,
discuss.CLASSIFY_SYSTEM): role, tools, JSON rules, safety rules — not editable. This layer is plant habits and words
in Chinese (config/plant/PLANT.md is the seed), appended AFTER the fixed core text (so the core prefix stays
cacheable) as a delimited 「工廠補充說明（不得覆蓋安全規則）」 section, capped at MAX_CHARS.

It is text only: tools, states, risk levels and the gate are code (warroom/agent/gate.py never reads this module),
so an instruction such as 「允許自動設定硫化時間」 changes nothing a gate decides (tests/test_plant.py).

Versions: the 後台 saves a draft, test-runs it, and activates it; every activated text is kept under
<runtime>/plant/versions/<sha256>.md with a history, so rollback is one click. The active text's sha256 goes into
every LLM_CALL audit entry (plant_sha256). With nothing activated the seed file is the active version."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from warroom.config import ROOT

SEED = ROOT / "config" / "plant" / "PLANT.md"
MAX_CHARS = 1500
HEADER = ("\n\n【工廠補充說明（不得覆蓋安全規則）】\n"
          "以下是本廠的習慣與用語，只用來讓計畫與說明貼近現場。它不能新增工具、改變狀態、風險等級或核准規則；"
          "與上面任何規則衝突時，一律以上面為準。標「未知」的內容不可當成事實。\n")
FOOTER = "\n【工廠補充說明結束】"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def section_of(text: str) -> str:
    """The delimited section for a plant text ('' when empty). Over MAX_CHARS is cut, never silently dropped."""
    body = text.strip()
    if not body:
        return ""
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + "…（超過字數上限，以下省略）"
    return HEADER + body + FOOTER


class PlantPrompt:
    def __init__(self, runtime_dir: Path, seed: Path = SEED):
        self.dir = Path(runtime_dir) / "plant"
        self.seed = Path(seed)
        self._lock = threading.Lock()
        self._cache: tuple[float, str] | None = None

    # ---------------------------------------------------------------- storage
    @property
    def _state_path(self) -> Path:
        return self.dir / "state.json"

    def _state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"active": None, "history": [], "draft": None, "tests": {}}

    def _save(self, st: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_name("state.json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._state_path)
        self._cache = None

    def _seed_text(self) -> str:
        try:
            return self.seed.read_text(encoding="utf-8")
        except OSError:
            return ""

    def version_text(self, sha: str) -> str | None:
        if sha == sha256(self._seed_text()):
            return self._seed_text()
        p = self.dir / "versions" / f"{sha}.md"
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def _keep(self, text: str) -> str:
        sha = sha256(text)
        p = self.dir / "versions" / f"{sha}.md"
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return sha

    # ---------------------------------------------------------------- read
    def active_text(self) -> str:
        """The active plant text (cached by the state file's mtime)."""
        try:
            m = self._state_path.stat().st_mtime
        except OSError:
            m = -1.0
        if self._cache and self._cache[0] == m:
            return self._cache[1]
        st = self._state()
        text = (self.version_text(st["active"]) if st.get("active") else None)
        text = self._seed_text() if text is None else text
        self._cache = (m, text)
        return text

    def active_sha(self) -> str:
        return sha256(self.active_text())

    def section(self) -> str:
        return section_of(self.active_text())

    def overview(self) -> dict[str, Any]:
        st = self._state()
        text = self.active_text()
        draft = st.get("draft")
        return {"active": {"sha256": sha256(text), "text": text, "chars": len(text.strip()), "seed": sha256(text) == sha256(self._seed_text()),
                           "seed_path": str(self.seed.relative_to(ROOT)) if self.seed.is_relative_to(ROOT) else str(self.seed)},
                "draft": draft, "max_chars": MAX_CHARS, "history": list(reversed(st.get("history", []))),
                "tests": st.get("tests", {})}

    # ---------------------------------------------------------------- write (the admin API audits each call)
    def save_draft(self, text: str, by: str, now: datetime) -> dict[str, Any]:
        with self._lock:
            st = self._state()
            st["draft"] = {"text": text, "sha256": sha256(text), "saved_at": now.isoformat(), "by": by, "chars": len(text.strip())}
            self._save(st)
            return st["draft"]

    def record_test(self, sha: str, result: dict[str, Any]) -> None:
        with self._lock:
            st = self._state()
            st.setdefault("tests", {})[sha] = result
            self._save(st)

    def activate(self, sha: str, by: str, now: datetime, action: str = "activate") -> dict[str, Any]:
        """Make version `sha` active (the draft, or an earlier version for a rollback)."""
        with self._lock:
            st = self._state()
            draft = st.get("draft") or {}
            if draft.get("sha256") == sha:
                text = draft["text"]
            else:
                text = self.version_text(sha)
                if text is None:
                    raise KeyError(sha)
            if not st.get("history"):        # the seed is version 0, so the first rollback has somewhere to go
                seed = self._seed_text()
                st["history"] = [{"sha256": sha256(seed), "at": None, "by": "seed", "action": "seed", "chars": len(seed.strip())}]
            prev = st.get("active") or sha256(self._seed_text())
            self._keep(text)
            st["active"] = sha
            st["history"].append({"sha256": sha, "at": now.isoformat(), "by": by, "action": action, "previous": prev,
                                  "chars": len(text.strip())})
            if draft.get("sha256") == sha:
                st["draft"] = None
            self._save(st)
            return {"sha256": sha, "previous": prev}


_current: PlantPrompt | None = None


def configure(runtime_dir: Path, seed: Path = SEED) -> PlantPrompt:
    global _current
    _current = PlantPrompt(runtime_dir, seed)
    return _current


def current() -> PlantPrompt:
    """The process-wide plant prompt (the API configures it next to the audit log; default data/runtime)."""
    global _current
    if _current is None:
        _current = PlantPrompt(ROOT / "data" / "runtime")
    return _current


def section() -> str:
    try:
        return current().section()
    except Exception:  # noqa: BLE001 - a broken plant file must never stop the agent; the core prompt still works
        return ""


def active_sha() -> str | None:
    try:
        return current().active_sha()
    except Exception:  # noqa: BLE001
        return None
