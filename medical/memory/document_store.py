"""父级分块文档存储（用于 Auto-merging Retriever）

参考你提供的 PostgreSQL + Redis 版本逻辑，这里改为：
- SQLite 持久化存储
- 不使用 Redis 缓存

存储字段与 DocumentLoader 输出保持一致：
text / filename / file_type / file_path / page_number /
chunk_id / parent_chunk_id / root_chunk_id / chunk_level / chunk_idx
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import sqlite3
from typing import Dict, List, Optional
from document_loader import DocumentLoader

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ParentChunkRecord:
    text: str
    filename: str
    file_type: str
    file_path: str
    page_number: int
    loaded_at: str
    chunk_id: str
    parent_chunk_id: str
    root_chunk_id: str
    chunk_level: int
    chunk_idx: int

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "filename": self.filename,
            "file_type": self.file_type,
            "file_path": self.file_path,
            "page_number": self.page_number,
            "loaded_at": self.loaded_at,
            "chunk_id": self.chunk_id,
            "parent_chunk_id": self.parent_chunk_id,
            "root_chunk_id": self.root_chunk_id,
            "chunk_level": self.chunk_level,
            "chunk_idx": self.chunk_idx,
        }


class ParentChunkStore:
    """基于 SQLite 的父级分块存储（无缓存）。"""

    def __init__(self, db_path: str = "./tmp_data/parent_chunks.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_database()

    def _init_database(self) -> None:
        cur = self._conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS parent_chunks (
                chunk_id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                filename TEXT,
                file_type TEXT,
                file_path TEXT,
                page_number INTEGER DEFAULT 0,
                loaded_at TEXT,
                parent_chunk_id TEXT,
                root_chunk_id TEXT,
                chunk_level INTEGER DEFAULT 0,
                chunk_idx INTEGER DEFAULT 0,
                updated_at TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_parent_chunks_filename ON parent_chunks(filename)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_parent_chunks_root ON parent_chunks(root_chunk_id)")
        self._conn.commit()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "text": row["text"] or "",
            "filename": row["filename"] or "",
            "file_type": row["file_type"] or "",
            "file_path": row["file_path"] or "",
            "page_number": int(row["page_number"] or 0),
            "loaded_at": row["loaded_at"] or "",
            "chunk_id": row["chunk_id"] or "",
            "parent_chunk_id": row["parent_chunk_id"] or "",
            "root_chunk_id": row["root_chunk_id"] or "",
            "chunk_level": int(row["chunk_level"] or 0),
            "chunk_idx": int(row["chunk_idx"] or 0),
        }

    def upsert_documents(self, docs: List[dict]) -> int:
        """写入/更新父级分块，返回写入条数。"""
        if not docs:
            return 0

        cur = self._conn.cursor()
        upserted = 0
        now = _utc_now_iso()

        for doc in docs:
            chunk_id = (doc.get("chunk_id") or "").strip()
            if not chunk_id:
                continue

            payload = (
                chunk_id,
                doc.get("text", "") or "",
                doc.get("filename", "") or "",
                doc.get("file_type", "") or "",
                doc.get("file_path", "") or "",
                int(doc.get("page_number", 0) or 0),
                (doc.get("loaded_at") or "") or now,
                doc.get("parent_chunk_id", "") or "",
                doc.get("root_chunk_id", "") or "",
                int(doc.get("chunk_level", 0) or 0),
                int(doc.get("chunk_idx", 0) or 0),
                now,
            )

            # SQLite UPSERT
            cur.execute("""
                INSERT INTO parent_chunks (
                    chunk_id, text, filename, file_type, file_path, page_number, loaded_at,
                    parent_chunk_id, root_chunk_id, chunk_level, chunk_idx, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    text=excluded.text,
                    filename=excluded.filename,
                    file_type=excluded.file_type,
                    file_path=excluded.file_path,
                    page_number=excluded.page_number,
                    loaded_at=excluded.loaded_at,
                    parent_chunk_id=excluded.parent_chunk_id,
                    root_chunk_id=excluded.root_chunk_id,
                    chunk_level=excluded.chunk_level,
                    chunk_idx=excluded.chunk_idx,
                    updated_at=excluded.updated_at
            """, payload)
            upserted += 1

        self._conn.commit()
        return upserted

    def get_documents_by_ids(self, chunk_ids: List[str]) -> List[dict]:
        if not chunk_ids:
            return []

        cleaned = []
        for cid in chunk_ids:
            key = (cid or "").strip()
            if key:
                cleaned.append(key)

        if not cleaned:
            return []

        placeholders = ",".join(["?"] * len(cleaned))
        cur = self._conn.cursor()
        cur.execute(f"SELECT * FROM parent_chunks WHERE chunk_id IN ({placeholders})", cleaned)

        rows = cur.fetchall()
        by_id: Dict[str, dict] = {row["chunk_id"]: self._row_to_dict(row) for row in rows}
        return [by_id[cid] for cid in cleaned if cid in by_id]

    def delete_by_filename(self, filename: str) -> int:
        """按文件名删除父级分块，返回删除条数。"""
        key = (filename or "").strip()
        if not key:
            return 0

        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(1) AS cnt FROM parent_chunks WHERE filename = ?", (key,))
        deleted = int(cur.fetchone()["cnt"] or 0)
        if deleted <= 0:
            return 0

        cur.execute("DELETE FROM parent_chunks WHERE filename = ?", (key,))
        self._conn.commit()
        return deleted

    def close(self) -> None:
        self._conn.close()


if __name__ == "__main__":
    # 最小自测：upsert -> get -> delete
    store = ParentChunkStore("./tmp_data/111_test.db")
    loader = DocumentLoader()
   
    documents = loader.load_Semantic_document("/Users/maruiyao/Desktop/study/agent/MRY_MedicalRag/bingli/dxd体检报告.pdf","dxd体检报告.pdf")
    print(documents)
    filtered_docs = [doc for doc in documents if doc.get("chunk_level") in [1,2]]
    print("-"*30)
    print(filtered_docs)
    print("-"*30)
    print("[TEST] upserted =", store.upsert_documents(filtered_docs))

    got = store.get_documents_by_ids(["dxd体检报告.pdf::p1::l1::0", "missing"])
    print("[TEST] get_documents_by_ids =", got)
    print("[TEST] deleted by filename =", store.delete_by_filename("demo.pdf"))
    store.close()