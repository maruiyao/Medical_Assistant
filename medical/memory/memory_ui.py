import os
from datetime import datetime
from pathlib import Path

import streamlit as st

from document_loader import DocumentLoader
from document_store import ParentChunkStore


def _now_str() -> str:
    return datetime.now().strftime("%Y%m%d:%H%M%S")


def _save_upload_to_tmp(uploaded_file, subdir: str) -> str:
    tmp_dir = Path("./tmp_data/uploads") / subdir
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # 防重名：时间戳 + 原文件名
    safe_name = uploaded_file.name.replace("/", "_").replace("\\", "_")
    out_path = tmp_dir / f"{_now_str()}__{safe_name}"

    out_path.write_bytes(uploaded_file.getvalue())
    return str(out_path)


def _ingest_file(
    store: ParentChunkStore,
    loader: DocumentLoader,
    file_path: str,
    original_filename: str,
    memory_type: str,
) -> int:
    # memory_type: "episodic" / "semantic"
    if memory_type == "episodic":
        docs = loader.load_Episodic_document(file_path=file_path, filename=original_filename)
    else:
        docs = loader.load_Semantic_document(file_path=file_path, filename=original_filename)

    return store.upsert_documents(docs)


def main():
    st.set_page_config(page_title="Memory Uploader", layout="wide")
    st.title("记忆上传与入库")

    st.caption("第一个上传框：情景记忆（Episodic，二级切分）；第二个上传框：语义记忆（Semantic，三级切分）。")

    with st.sidebar:
        st.subheader("存储配置")
        db_path = st.text_input("SQLite 路径", value="./tmp_data/parent_chunks.db")
        st.write("会写入 `parent_chunks` 表（含 parent/root/level/idx/loaded_at）。")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("情景记忆（Episodic）")
        episodic_file = st.file_uploader(
            "上传病例/短文档",
            type=["pdf", "docx", "doc", "xlsx", "xls"],
            key="episodic_uploader",
        )

    with col2:
        st.subheader("语义记忆（Semantic）")
        semantic_file = st.file_uploader(
            "上传说明书/长文档",
            type=["pdf", "docx", "doc", "xlsx", "xls"],
            key="semantic_uploader",
        )

    ingest_btn = st.button("开始加载并入库", type="primary")
    show_db_btn = st.button("打印/查看数据库当前内容")

    if ingest_btn:
        store = ParentChunkStore(db_path=db_path)
        loader = DocumentLoader()

        try:
            total_upserted = 0

            if episodic_file is not None:
                episodic_path = _save_upload_to_tmp(episodic_file, "episodic")
                n = _ingest_file(
                    store=store,
                    loader=loader,
                    file_path=episodic_path,
                    original_filename=episodic_file.name,
                    memory_type="episodic",
                )
                st.success(f"情景记忆入库完成：写入/更新 {n} 条 chunk")
                total_upserted += n
                
                
            if semantic_file is not None:
                semantic_path = _save_upload_to_tmp(semantic_file, "semantic")
                n = _ingest_file(
                    store=store,
                    loader=loader,
                    file_path=semantic_path,
                    original_filename=semantic_file.name,
                    memory_type="semantic",
                )
                st.success(f"语义记忆入库完成：写入/更新 {n} 条 chunk")
                total_upserted += n

            if episodic_file is None and semantic_file is None:
                st.warning("你还没上传文件。")
            else:
                st.info(f"本次总计写入/更新：{total_upserted} 条 chunk")

        finally:
            try:
                store.close()
            except Exception:
                pass

    if show_db_btn:
        store = ParentChunkStore(db_path=db_path)
        try:
            cur = store._conn.cursor()
            cur.execute("""
                SELECT chunk_id, filename, chunk_level, chunk_idx, loaded_at, updated_at
                FROM parent_chunks
                ORDER BY rowid DESC
                LIMIT 50
            """)
            rows = [dict(r) for r in cur.fetchall()]
            st.write(f"当前数据库 `parent_chunks` 最近 {len(rows)} 条：")
            st.dataframe(rows, use_container_width=True)

            # 同步打印到控制台，方便你看日志
            print("[DB] parent_chunks latest rows:")
            for r in rows[:10]:
                print(r)
        finally:
            try:
                store.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()

