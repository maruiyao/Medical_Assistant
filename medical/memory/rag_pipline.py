"""RAG Pipeline: 文件解析 → level1 存 SQLite / level2 存 Qdrant → hybrid 检索"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from document_loader import DocumentLoader
from document_store import ParentChunkStore
from qdrant_store import QdrantVectorStore
from embedding import embedding_service


class RAGPipeline:
    def __init__(
        self,
        db_path: str = "./tmp_data/parent_chunks.db",
    ):
        self.loader = DocumentLoader(is_Episodic=True)
        self.sqlite = ParentChunkStore(db_path=db_path)
        self.qdrant = QdrantVectorStore(
            url=(os.getenv("QDRANT_URL") or "").strip(),
            api_key=(os.getenv("QDRANT_API_KEY") or "").strip(),
            collection_name=(os.getenv("QDRANT_COLLECTION") or "hello_agents_vectors").strip(),
            vector_size=int(os.getenv("QDRANT_VECTOR_SIZE") or "512"),
            distance=(os.getenv("QDRANT_DISTANCE") or "cosine").strip(),
            timeout=int(os.getenv("QDRANT_TIMEOUT") or "30"),
        )

    def ingest(self, file_path: str, filename: str) -> dict:
        """解析文件（情景记忆），level1→SQLite，level2→Qdrant，返回写入统计。"""
        docs = self.loader.load_Episodic_document(file_path, filename)
        level1 = [d for d in docs if d["chunk_level"] == 1]
        level2 = [d for d in docs if d["chunk_level"] == 2]

        sqlite_n = self.sqlite.upsert_documents(level1)
        qdrant_ok = self.qdrant.add_vectors(metadata=level2, ids=[d["chunk_id"] for d in level2]) if level2 else True
        return {"total": len(docs), "level1_sqlite": sqlite_n, "level2_qdrant": len(level2), "qdrant_ok": qdrant_ok}

    def search(self, query_text: str, limit: int = 5, score_alpha: float = 0.5):
        """hybrid 检索：先拿稠密向量，再联合 BM25 稀疏做融合。"""
        q_vec = embedding_service.get_embeddings([query_text])[0]
        return self.qdrant.search_similar(
            query_vector=q_vec,
            query_text=query_text,
            limit=limit,
            score_alpha=score_alpha,
        )

    def search_and_merge(self, query_text: str, limit: int = 5, score_alpha: float = 0.5):
        """检索 level2 命中后，自动从 SQLite 取回对应 level1 父文本。"""
        hits = self.search(query_text, limit=limit, score_alpha=score_alpha)
        parent_ids = list({h["metadata"].get("parent_chunk_id", "") for h in hits} - {""})
        parents = {d["chunk_id"]: d for d in self.sqlite.get_documents_by_ids(parent_ids)} if parent_ids else {}
        for h in hits:
            pid = h["metadata"].get("parent_chunk_id", "")
            h["parent_text"] = parents.get(pid, {}).get("text", "")
        return hits


if __name__ == "__main__":
    pipe = RAGPipeline()

    bingli_dir = Path(__file__).resolve().parent.parent / "bingli"
    for f in sorted(bingli_dir.glob("*.pdf")):
        print(f"=== 入库: {f.name} ===")
        stat = pipe.ingest(str(f), f.name)
        print(stat)

    query = "患者 3 天前因受凉后出现干咳，无痰。昨日起病情加重，咳黄色粘痰，量多，不易咳出。"
    print(f"\n=== 检索: {query} ===")
    results = pipe.search_and_merge(query, limit=5, score_alpha=0.5)
    for i, r in enumerate(results, 1):
        print(f"\n--- hit {i}  score={r['score']:.4f} (dense={r['score_dense']:.4f} sparse={r['score_sparse']:.4f}) ---")
        print(f"chunk_id : {r['metadata'].get('chunk_id')}")
        print(f"filename : {r['metadata'].get('filename')}")
        print(f"level2   : {r['metadata'].get('text', '')[:200]}")
        if r.get("parent_text"):
            print(f"level1   : {r['parent_text'][:300]}")
