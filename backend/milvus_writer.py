"""文档向量化并写入 Milvus - 支持密集+稀疏向量，双知识库（medical_record / medication）"""
from embedding import get_embedding_service
from milvus_client import MilvusManager


class MilvusWriter:
    """文档向量化并写入 Milvus 服务 - 支持混合检索"""

    def __init__(self, milvus_manager: MilvusManager = None):
        self.milvus_manager = milvus_manager or MilvusManager()

    def write_documents(
        self,
        documents: list[dict],
        kb_type: str,
        batch_size: int = 50,
        progress_callback=None,
    ):
        """
        批量写入文档到指定 kb_type 的 Milvus 集合（同时生成密集和稀疏向量）
        :param documents: 文档列表
        :param kb_type: 知识库类型（'medical_record' 或 'medication'）
        :param batch_size: 批次大小
        """
        if not documents:
            return

        embedding_service = get_embedding_service(kb_type)
        self.milvus_manager.init_collection(kb_type)

        all_texts = [doc["text"] for doc in documents]
        embedding_service.increment_add_documents(all_texts)

        total = len(documents)
        for i in range(0, total, batch_size):
            batch = documents[i : i + batch_size]
            texts = [doc["text"] for doc in batch]

            dense_embeddings, sparse_embeddings = embedding_service.get_all_embeddings(texts)

            insert_data = [
                {
                    "dense_embedding": dense_emb,
                    "sparse_embedding": sparse_emb,
                    "text": doc["text"],
                    "filename": doc["filename"],
                    "file_type": doc["file_type"],
                    "file_path": doc.get("file_path", ""),
                    "page_number": doc.get("page_number", 0),
                    "chunk_idx": doc.get("chunk_idx", 0),
                    "chunk_id": doc.get("chunk_id", ""),
                    "parent_chunk_id": doc.get("parent_chunk_id", ""),
                    "root_chunk_id": doc.get("root_chunk_id", ""),
                    "chunk_level": doc.get("chunk_level", 0),
                }
                for doc, dense_emb, sparse_emb in zip(batch, dense_embeddings, sparse_embeddings)
            ]

            self.milvus_manager.insert(insert_data, kb_type)

            if progress_callback:
                processed = min(i + batch_size, total)
                progress_callback(processed, total)
