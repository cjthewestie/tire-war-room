"""warroom/kb — SOP knowledge base (Qdrant embedded or server + fastembed), Chinese SOP corpus
in warroom/kb/sop/*.md, the orders/shifts SQLite DB with Excel import, the read-only tool
functions, and the factory quiz for the NVFP4-vs-FP8 accuracy test. Owner: kb builder."""
from warroom.kb.orders_db import DEMO_DATE, OrdersDB, build_job
from warroom.kb.quiz import load_quiz, parse_choice, score
from warroom.kb.store import QdrantSopKB
from warroom.kb.tools import default_db, default_kb, query_orders_shifts_tool, sop_search_tool

__all__ = [
           "DEMO_DATE",
           "OrdersDB",
           "QdrantSopKB",
           "build_job",
           "default_db",
           "default_kb",
           "load_quiz",
           "parse_choice",
           "query_orders_shifts_tool",
           "score",
           "sop_search_tool",
]
