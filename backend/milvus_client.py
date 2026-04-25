"""Milvus 客户端 - 支持密集向量+稀疏向量混合检索，双知识库（medical_record / medication）"""
import os
import threading
from typing import Callable, TypeVar

from dotenv import load_dotenv
from pymilvus import MilvusClient, DataType, AnnSearchRequest, RRFRanker

load_dotenv()

# Milvus 单次 query 的 limit 上限（超出会报 invalid max query result window）
QUERY_MAX_LIMIT = 16384
T = TypeVar("T")

VALID_KB_TYPES = {"medical_record", "medication"}


class MilvusManager:
    """Milvus 连接和集合管理 - 支持混合检索，双知识库分离"""

    def __init__(self):
        self.host = os.getenv("MILVUS_HOST", "localhost")
        self.port = os.getenv("MILVUS_PORT", "19530")
        self._collection_map = {
            "medical_record": os.getenv(
                "MILVUS_COLLECTION_MEDICAL_RECORD", "medical_record_collection"
            ),
            "medication": os.getenv(
                "MILVUS_COLLECTION_MEDICATION", "medication_collection"
            ),
        }
        self.uri = f"http://{self.host}:{self.port}"
        self.client = None
        self._client_lock = threading.RLock()

    def _get_collection_name(self, kb_type: str) -> str:
        name = self._collection_map.get(kb_type)
        if not name:
            raise ValueError(
                f"未知 kb_type: {kb_type!r}，合法值：{list(self._collection_map)}"
            )
        return name

    def _get_client(self) -> MilvusClient:
        # Lazy-create client to avoid blocking app import/startup when Milvus is temporarily unavailable.
        with self._client_lock:
            if self.client is None:
                self.client = MilvusClient(uri=self.uri)
            return self.client

    @staticmethod
    def _is_closed_channel_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "closed channel" in msg or "channel is closed" in msg

    @staticmethod
    def _close_client(client) -> None:
        close = getattr(client, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:
            pass

    def _reset_client(self, failed_client=None) -> None:
        with self._client_lock:
            if self.client is None:
                return
            if failed_client is not None and self.client is not failed_client:
                return
            client = self.client
            self.client = None

        self._close_client(client)

    def _run_with_reconnect(self, operation: Callable[[MilvusClient], T]) -> T:
        client = self._get_client()
        try:
            return operation(client)
        except Exception as exc:
            if not self._is_closed_channel_error(exc):
                raise

            self._reset_client(client)
            return operation(self._get_client())

    def init_collection(self, kb_type: str, dense_dim: int | None = None):
        """
        初始化指定 kb_type 的 Milvus 集合 - 同时支持密集向量和稀疏向量
        :param kb_type: 知识库类型（'medical_record' 或 'medication'）
        :param dense_dim: 密集向量维度；默认读环境变量 DENSE_EMBEDDING_DIM（本地 BAAI/bge-m3 为 1024）
        """
        if dense_dim is None:
            dense_dim = int(os.getenv("DENSE_EMBEDDING_DIM", "1024"))
        collection_name = self._get_collection_name(kb_type)

        def _init(client: MilvusClient) -> None:
            if not client.has_collection(collection_name):
                schema = client.create_schema(auto_id=True, enable_dynamic_field=True)

                schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
                schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=dense_dim)
                schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
                schema.add_field("text", DataType.VARCHAR, max_length=2000)
                schema.add_field("filename", DataType.VARCHAR, max_length=255)
                schema.add_field("file_type", DataType.VARCHAR, max_length=50)
                schema.add_field("file_path", DataType.VARCHAR, max_length=1024)
                schema.add_field("page_number", DataType.INT64)
                schema.add_field("chunk_idx", DataType.INT64)
                schema.add_field("chunk_id", DataType.VARCHAR, max_length=512)
                schema.add_field("parent_chunk_id", DataType.VARCHAR, max_length=512)
                schema.add_field("root_chunk_id", DataType.VARCHAR, max_length=512)
                schema.add_field("chunk_level", DataType.INT64)

                index_params = client.prepare_index_params()
                index_params.add_index(
                    field_name="dense_embedding",
                    index_type="HNSW",
                    metric_type="IP",
                    params={"M": 16, "efConstruction": 256},
                )
                index_params.add_index(
                    field_name="sparse_embedding",
                    index_type="SPARSE_INVERTED_INDEX",
                    metric_type="IP",
                    params={"drop_ratio_build": 0.2},
                )

                client.create_collection(
                    collection_name=collection_name,
                    schema=schema,
                    index_params=index_params,
                )

        self._run_with_reconnect(_init)

    def init_all_collections(self, dense_dim: int | None = None):
        """初始化所有 kb_type 对应的 Milvus 集合（应用启动时调用）。"""
        for kb_type in self._collection_map:
            self.init_collection(kb_type, dense_dim=dense_dim)

    def insert(self, data: list[dict], kb_type: str):
        """插入数据到指定 kb_type 的 Milvus 集合"""
        collection_name = self._get_collection_name(kb_type)
        return self._run_with_reconnect(
            lambda client: client.insert(collection_name, data)
        )

    def query(
        self,
        kb_type: str,
        filter_expr: str = "",
        output_fields: list[str] = None,
        limit: int = 10000,
        offset: int = 0,
    ):
        """查询数据。limit 不宜超过 QUERY_MAX_LIMIT。"""
        collection_name = self._get_collection_name(kb_type)
        return self._run_with_reconnect(
            lambda client: client.query(
                collection_name=collection_name,
                filter=filter_expr,
                output_fields=output_fields or ["filename", "file_type"],
                limit=min(limit, QUERY_MAX_LIMIT),
                offset=offset,
            )
        )

    def query_all(
        self,
        kb_type: str,
        filter_expr: str = "",
        output_fields: list[str] | None = None,
    ) -> list:
        """分页拉取匹配 filter 的全部行，避免单次 limit 超过服务端窗口。"""
        collection_name = self._get_collection_name(kb_type)
        fields = output_fields or ["filename", "file_type"]
        out: list = []
        offset = 0
        while True:
            batch = self._run_with_reconnect(
                lambda client: client.query(
                    collection_name=collection_name,
                    filter=filter_expr,
                    output_fields=fields,
                    limit=QUERY_MAX_LIMIT,
                    offset=offset,
                )
            )
            if not batch:
                break
            out.extend(batch)
            if len(batch) < QUERY_MAX_LIMIT:
                break
            offset += len(batch)
        return out

    def get_chunks_by_ids(self, chunk_ids: list[str], kb_type: str) -> list[dict]:
        """根据 chunk_id 批量查询分块（用于 Auto-merging 拉取父块）"""
        ids = [item for item in chunk_ids if item]
        if not ids:
            return []
        quoted_ids = ", ".join([f'"{item}"' for item in ids])
        filter_expr = f"chunk_id in [{quoted_ids}]"
        return self.query(
            kb_type=kb_type,
            filter_expr=filter_expr,
            output_fields=[
                "text",
                "filename",
                "file_type",
                "page_number",
                "chunk_id",
                "parent_chunk_id",
                "root_chunk_id",
                "chunk_level",
                "chunk_idx",
            ],
            limit=len(ids),
        )

    def hybrid_retrieve(
        self,
        dense_embedding: list[float],
        sparse_embedding: dict,
        kb_type: str,
        top_k: int = 5,
        rrf_k: int = 60,
        filter_expr: str = "",
    ) -> list[dict]:
        """
        混合检索 - 使用 RRF 融合密集向量和稀疏向量的检索结果

        :param dense_embedding: 密集向量
        :param sparse_embedding: 稀疏向量 {index: value, ...}
        :param kb_type: 知识库类型
        :param top_k: 返回结果数量
        :param rrf_k: RRF 算法参数 k，默认60
        :return: 检索结果列表
        """
        collection_name = self._get_collection_name(kb_type)
        output_fields = [
            "text",
            "filename",
            "file_type",
            "page_number",
            "chunk_id",
            "parent_chunk_id",
            "root_chunk_id",
            "chunk_level",
            "chunk_idx",
        ]

        dense_search = AnnSearchRequest(
            data=[dense_embedding],
            anns_field="dense_embedding",
            param={"metric_type": "IP", "params": {"ef": 64}},
            limit=top_k * 2,
            expr=filter_expr,
        )

        sparse_search = AnnSearchRequest(
            data=[sparse_embedding],
            anns_field="sparse_embedding",
            param={"metric_type": "IP", "params": {"drop_ratio_search": 0.2}},
            limit=top_k * 2,
            expr=filter_expr,
        )

        reranker = RRFRanker(k=rrf_k)

        results = self._run_with_reconnect(
            lambda client: client.hybrid_search(
                collection_name=collection_name,
                reqs=[dense_search, sparse_search],
                ranker=reranker,
                limit=top_k,
                output_fields=output_fields,
            )
        )

        formatted_results = []
        for hits in results:
            for hit in hits:
                formatted_results.append(
                    {
                        "id": hit.get("id"),
                        "text": hit.get("text", ""),
                        "filename": hit.get("filename", ""),
                        "file_type": hit.get("file_type", ""),
                        "page_number": hit.get("page_number", 0),
                        "chunk_id": hit.get("chunk_id", ""),
                        "parent_chunk_id": hit.get("parent_chunk_id", ""),
                        "root_chunk_id": hit.get("root_chunk_id", ""),
                        "chunk_level": hit.get("chunk_level", 0),
                        "chunk_idx": hit.get("chunk_idx", 0),
                        "score": hit.get("distance", 0.0),
                    }
                )

        return formatted_results

    def dense_retrieve(
        self,
        dense_embedding: list[float],
        kb_type: str,
        top_k: int = 5,
        filter_expr: str = "",
    ) -> list[dict]:
        """
        仅使用密集向量检索（降级模式，用于稀疏向量不可用时）
        """
        collection_name = self._get_collection_name(kb_type)
        results = self._run_with_reconnect(
            lambda client: client.search(
                collection_name=collection_name,
                data=[dense_embedding],
                anns_field="dense_embedding",
                search_params={"metric_type": "IP", "params": {"ef": 64}},
                limit=top_k,
                output_fields=[
                    "text",
                    "filename",
                    "file_type",
                    "page_number",
                    "chunk_id",
                    "parent_chunk_id",
                    "root_chunk_id",
                    "chunk_level",
                    "chunk_idx",
                ],
                filter=filter_expr,
            )
        )

        formatted_results = []
        for hits in results:
            for hit in hits:
                formatted_results.append(
                    {
                        "id": hit.get("id"),
                        "text": hit.get("entity", {}).get("text", ""),
                        "filename": hit.get("entity", {}).get("filename", ""),
                        "file_type": hit.get("entity", {}).get("file_type", ""),
                        "page_number": hit.get("entity", {}).get("page_number", 0),
                        "chunk_id": hit.get("entity", {}).get("chunk_id", ""),
                        "parent_chunk_id": hit.get("entity", {}).get("parent_chunk_id", ""),
                        "root_chunk_id": hit.get("entity", {}).get("root_chunk_id", ""),
                        "chunk_level": hit.get("entity", {}).get("chunk_level", 0),
                        "chunk_idx": hit.get("entity", {}).get("chunk_idx", 0),
                        "score": hit.get("distance", 0.0),
                    }
                )

        return formatted_results

    def delete(self, filter_expr: str, kb_type: str):
        """删除指定 kb_type 集合中满足条件的数据"""
        collection_name = self._get_collection_name(kb_type)
        return self._run_with_reconnect(
            lambda client: client.delete(
                collection_name=collection_name, filter=filter_expr
            )
        )

    def has_collection(self, kb_type: str) -> bool:
        """检查指定 kb_type 的集合是否存在"""
        collection_name = self._get_collection_name(kb_type)
        return self._run_with_reconnect(
            lambda client: client.has_collection(collection_name)
        )

    def drop_collection(self, kb_type: str):
        """删除指定 kb_type 的集合（用于重建 schema）"""
        collection_name = self._get_collection_name(kb_type)

        def _drop(client: MilvusClient) -> None:
            if client.has_collection(collection_name):
                client.drop_collection(collection_name)

        self._run_with_reconnect(_drop)
