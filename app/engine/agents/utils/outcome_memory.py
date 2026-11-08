"""Durable, owner/period-scoped outcome lessons; optional analysis enrichment."""

from datetime import date
import hashlib
import json
import threading

from app.core.config import settings
from app.data.knowledge.config import embed_texts, get_embedding_config

_lock = threading.Lock()
_clients = {}


def collection():
    import chromadb
    from chromadb.config import Settings

    cfg = get_embedding_config()
    if cfg is None:
        raise RuntimeError("未配置复盘向量模型")
    signature = hashlib.sha256(json.dumps(
        [cfg["provider"], cfg["model_name"], cfg["dimension"]]
    ).encode()).hexdigest()[:16]
    path = settings.resolve_runtime_path(settings.SETTLEMENT_MEMORY_DIR)
    with _lock:
        if path not in _clients:
            _clients[path] = chromadb.PersistentClient(path=path, settings=Settings(anonymized_telemetry=False))
        return _clients[path].get_or_create_collection(
            "outcome_" + signature, metadata={"hnsw:space": "cosine"},
        )


def save_lesson(record, brief: str, lesson: str, available_date: str):
    owner = str(record.get("user_id") or "").strip()
    if not owner:
        raise ValueError("复盘缺少所有者，不能写入共享记忆")
    available = date.fromisoformat(available_date).toordinal()
    identity = hashlib.sha256(str(record["_id"]).encode()).hexdigest()
    collection().upsert(
        ids=[identity], documents=[brief], embeddings=embed_texts([brief]),
        metadatas=[{
            "recommendation": lesson, "user_id": owner,
            "horizon_days": int(record.get("horizon_days") or 7),
            "available_day": available, "resolution_date": available_date,
            "symbol": str(record.get("symbol") or ""),
            "decision_date": str(record.get("decision_date") or ""),
            "signal": str(record.get("signal") or ""),
        }],
    )


def search_lessons(situation: str, owner: str, horizon_days: int, as_of: str, n: int = 2):
    if not owner or not as_of:
        return []
    cutoff = date.fromisoformat(as_of).toordinal()
    col = collection()
    where = {"$and": [
        {"user_id": str(owner)}, {"horizon_days": horizon_days},
        {"available_day": {"$lte": cutoff}},
    ]}
    # 空库/无本用户本周期可用经验时不做向量请求。
    if not col.get(where=where, limit=1, include=[])["ids"]:
        return []
    result = col.query(query_embeddings=embed_texts([situation]), where=where,
                       n_results=n, include=["metadatas"])
    return [{**{k: m.get(k, "") for k in ("recommendation", "resolution_date", "symbol", "decision_date")},
             "source_identity": identity}
            for identity, m in zip(result["ids"][0], (result.get("metadatas") or [[]])[0])]
