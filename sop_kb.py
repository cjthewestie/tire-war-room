"""SOP 知識庫：本機 Qdrant + 本機嵌入模型，完全離線。

用法：
  .venv/bin/python sop_kb.py ingest            # 讀 sop/ 下的 .md/.txt/.pdf，建索引
  .venv/bin/python sop_kb.py search "冷卻水 溫差"
程式內：from sop_kb import sop_search
"""
import sys, uuid
from pathlib import Path
from qdrant_client import QdrantClient, models

SOP_DIR = Path(__file__).parent / "sop"
DB_DIR = Path(__file__).parent / "qdrant_db"      # 嵌入式模式：就是一個資料夾，不用開伺服器
COLLECTION = "sop"
# 多語嵌入模型（約 220 MB，ONNX，CPU 可跑）。上 DGX Spark 後可換 NVIDIA llama-nemotron-embed。
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CHUNK, OVERLAP = 400, 80                          # 中文用字數切；1000 字對中文太長

_client = None
def client():
    global _client
    if _client is None:
        _client = QdrantClient(path=str(DB_DIR))
        _client.set_model(EMBED_MODEL)
    return _client

def read(p: Path) -> str:
    if p.suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(p).pages)
    return p.read_text(encoding="utf-8")

def chunks(text: str):
    i = 0
    while i < len(text):
        yield text[i:i + CHUNK]
        i += CHUNK - OVERLAP

def ingest():
    c = client()
    if c.collection_exists(COLLECTION):
        c.delete_collection(COLLECTION)               # 重建，避免刪掉的 SOP 還留在索引裡
    docs, meta, ids = [], [], []
    for p in sorted(SOP_DIR.glob("*")):
        if p.suffix not in {".md", ".txt", ".pdf"}:
            continue
        for n, ch in enumerate(chunks(read(p))):
            docs.append(ch); meta.append({"source": p.name, "chunk": n})
            ids.append(str(uuid.uuid5(uuid.NAMESPACE_URL, f"{p.name}#{n}")))
    c.add(COLLECTION, documents=docs, metadata=meta, ids=ids)
    print(f"indexed {len(docs)} chunks from {len({m['source'] for m in meta})} files")

def sop_search(query: str, k: int = 3):
    """Agent 工具（唯讀）：回傳最相關的 SOP 段落與來源，供事件指揮官引用。"""
    hits = client().query(COLLECTION, query_text=query, limit=k)
    return [{"source": h.metadata["source"], "score": round(h.score, 3), "text": h.document} for h in hits]

if __name__ == "__main__":
    if sys.argv[1:2] == ["ingest"]:
        ingest()
    elif sys.argv[1:2] == ["search"]:
        for h in sop_search(" ".join(sys.argv[2:])):
            print(f"[{h['score']}] {h['source']}\n{h['text'].strip()[:160]}\n")
    else:
        print(__doc__)
