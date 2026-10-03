from __future__ import annotations

from pathlib import Path

from warroom.contracts import SopHit


class QdrantSopKB:
    """Implements contracts.SopKB. Port of legacy/sop_kb.py. Embedded mode (path) on the Mac,
    server mode (url) on the Spark. Fully offline once the embed model is cached.
    Must answer "硫化中斷處置" with the SOP of that title (page 11)."""

    def __init__(self, path: Path | None = None, url: str | None = None, sop_dir: Path | None = None,
                 embed_model: str | None = None, collection: str = "sop"):
        raise NotImplementedError

    def ingest(self) -> int: raise NotImplementedError
    def search(self, query: str, k: int = 3) -> list[SopHit]: raise NotImplementedError
