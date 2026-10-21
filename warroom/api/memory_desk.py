"""The Runtime's memory desk (CJ, 2026-10-22: 「加每日摘要」, tiered memory, claude-mem style observations).

- At each 一日循環 wrap (循環 n ends) and at each real factory-date boundary, the day that ended gets a daily summary:
  code builds the facts from that day's audit rows (warroom/data/memory_log.py), ONE model call at the lowest queue
  priority ("report") turns them into ≤ 8 plain sentences, the grounding check keeps only a summary whose every
  number is in the facts, and on any failure the code's sentences are the summary. Then the weekly / monthly roll-up
  runs and the search index is refreshed. Each summary is audited (SYSTEM, actor "memory").
- Observations are captured from the audit as it grows (`sync()`, incremental), never by a model.
Everything is derived: scripts/rebuild_memory.py rebuilds the same files and rows from the audit file."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from warroom.contracts import AuditKind
from warroom.data import memory_log as ML

log = logging.getLogger(__name__)


class MemoryDesk:
    def __init__(self, rt: Any):
        self.rt = rt
        base = Path(rt.settings.audit_path).parent
        self.audit_path = Path(rt.settings.audit_path)
        self.files = ML.MemoryFiles(base / "memory")
        self.obs = ML.ObservationStore(base / "observations.sqlite")
        self.index = ML.MemoryIndex(getattr(rt.c, "kb", None))
        self._offset = 0                    # audit file bytes already turned into observations
        self._loop: int | None = None       # the 一日循環 cycle at that point of the file
        self._day: str | None = None        # the factory date the desk last saw (date boundary)
        self._tasks: set[asyncio.Task] = set()
        self._index_at = 0.0
        self._started = False

    # ---------------------------------------------------------------- observations
    def _new_rows(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            # an in-memory audit (tests with fallbacks): read it whole, keep only rows past the stored seq
            last = self.obs.last_seq()
            return [r for r in ML.read_rows(None, self.rt.audit) if int(r["seq"]) > last]
        size = self.audit_path.stat().st_size
        if size < self._offset:
            self._offset, self._loop = 0, None
        with self.audit_path.open("rb") as fh:
            fh.seek(self._offset)
            data = fh.read()
        end = data.rfind(b"\n") + 1
        self._offset += end
        out = []
        for line in data[:end].splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def sync(self) -> int:
        """Turn the audit rows written since the last call into observations. Cheap; called before a chat turn."""
        rows = self._new_rows()
        if not rows:
            return 0
        last = self.obs.last_seq()
        items = []
        loop = self._loop
        for (day, lp), r in ML.scan(rows, loop):
            loop = lp
            if int(r["seq"]) <= last:
                continue
            o = ML.observe(r)
            if o is not None:
                items.append((ML.day_name(day, lp), o))
        self._loop = loop
        return self.obs.add(items)

    def today_key(self) -> tuple[str, int | None]:
        now = self.rt.clock.now().astimezone(ML.F.TZ)
        info = self.rt.day_loop_info() if hasattr(self.rt, "day_loop_info") else None
        return now.date().isoformat(), (info or {}).get("loop") or None

    def today(self) -> list[dict[str, Any]]:
        self.sync()
        day, loop = self.today_key()
        return self.obs.day(ML.day_name(day, loop))

    def day_index(self) -> str:
        return ML.day_index(self.today())

    # ---------------------------------------------------------------- daily summary
    def facts(self, day: str, loop: int | None) -> dict[str, Any]:
        rows = [r for (d, lp), r in ML.scan(ML.read_rows(self.audit_path, self.rt.audit)) if (d, lp) == (day, loop)]
        return ML.daily_facts(rows, day, loop)

    async def _complete(self, f: dict[str, Any]) -> str:
        """The one model call (lowest queue priority). Raises when there is no model or it fails."""
        s = self.rt.settings
        if str(getattr(s, "llm_mode", "mock")) != "llm":
            raise RuntimeError("rule mode: no model")
        from openai import AsyncOpenAI

        from warroom.agent.commander import detect_server_kind, thinking_off
        from warroom.agent.orchestrator import PRIORITY
        client = AsyncOpenAI(base_url=s.base_url, api_key=s.api_key_effective, timeout=min(40.0, s.llm_timeout_s),
                             max_retries=0)
        extra = thinking_off(detect_server_kind(s.base_url))
        async with self.rt.orch.queue.slot(PRIORITY["report"], "memory", None, "每日摘要"):
            resp = await client.chat.completions.create(
                model=s.model, temperature=0.0, max_tokens=500,
                messages=[{"role": "system", "content": ML.SUMMARY_SYSTEM},
                          {"role": "user", "content": "當天事實：\n" + ML.facts_text(f)}],
                **({"extra_body": extra} if extra else {}))
        return (resp.choices[0].message.content or "").strip()

    async def _model_sentences(self, f: dict[str, Any]) -> tuple[list[str] | None, str | None]:
        try:
            text = await self._complete(f)
        except Exception as e:  # noqa: BLE001 - the code's sentences are the summary
            return None, f"{type(e).__name__}: {e}"[:200]
        sents = ML.check_sentences(text, f)
        return (sents, None) if sents else (None, "ungrounded or too long: " + text[:200])

    async def summarize(self, day: str, loop: int | None, model: bool = True) -> dict[str, Any]:
        t0 = time.perf_counter()
        f = await asyncio.to_thread(self.facts, day, loop)
        sents, err = await self._model_sentences(f) if model else (None, "model off")
        source = "model" if sents else "code"
        doc = self.files.daily_doc(f, sents or ML.code_sentences(f), source, self.rt.clock.now().isoformat())
        path = await asyncio.to_thread(self.files.write, "daily", doc)
        rolled = await asyncio.to_thread(self.files.roll_up, self.rt.clock.now().isoformat())
        self.refresh_index(force=True)
        self.rt.audit.append(AuditKind.SYSTEM, "memory", {
            "msg": "daily summary", "name": doc["name"], "label": doc["label"], "source": source,
            "sentences": len(doc["sentences"]), "tasks": f["tasks"], "facts_sha": doc["facts_sha"],
            "model_error": err if source == "code" else None, "rolled_up": rolled, "file": str(path.name),
            "ms": round((time.perf_counter() - t0) * 1000)})
        return doc

    def schedule(self, day: str, loop: int | None) -> None:
        """Write the summary in the background: the plant never waits on it."""
        try:
            task = asyncio.get_running_loop().create_task(self.summarize(day, loop))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.warning("daily summary failed: %s", t.exception())

    async def drain(self) -> None:
        """Tests and shutdown: wait for summaries being written."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def on_tick(self) -> None:
        """A real date boundary: the factory clock ran from one date into the next (a scenario load that jumps
        dates is not one)."""
        day, loop = self.today_key()
        prev, self._day = self._day, day
        if prev is None or loop:
            return
        if (ML.date.fromisoformat(day) - ML.date.fromisoformat(prev)) == timedelta(days=1):
            self.schedule(prev, None)

    def on_wrap(self, day: str, ended_loop: int | None) -> None:
        self.schedule(day, ended_loop)

    # ---------------------------------------------------------------- reading
    def daily(self, name: str | None = None, before: str | None = None) -> dict[str, Any] | None:
        """One daily summary: by name or date (the latest cycle of that date), or the latest one before today."""
        docs = self.files.all("daily")
        if not docs:
            return None
        docs.sort(key=lambda d: (d["day"], d.get("loop") or 0))
        if name:
            hit = [d for d in docs if d["name"] == name or d["day"] == name]
            return hit[-1] if hit else None
        if before:
            older = [d for d in docs if d["name"] != before]
            return older[-1] if older else None
        return docs[-1]

    def refresh_index(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._index_at < 30:
            return
        self._index_at = time.monotonic()
        self.sync()
        self.index.set_docs(ML.summary_docs(self.files) + ML.observation_docs(self.obs))

    def search(self, q: str, day: str | None = None, type_: str | None = None, level: str | None = None) -> dict[str, Any]:
        self.refresh_index()
        return self.index.search(q, day=day, type_=type_, level=level)
