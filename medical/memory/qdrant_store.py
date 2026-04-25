"""
Qdrant向量数据库存储实现
使用专业的Qdrant向量数据库替代ChromaDB
"""
from __future__ import annotations

import json
import os
import sys
from dotenv import load_dotenv
load_dotenv()
import logging
#import os
import uuid
import threading
from typing import Dict, List, Optional, Any, Union, TextIO
from datetime import datetime

try:
    from memory.embedding import embedding_service as _embedding_service
except ImportError:
    from embedding import embedding_service as _embedding_service

try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models
    from qdrant_client.http.models import (
        Distance,
        VectorParams,
        PointStruct,
        Filter,
        FieldCondition,
        MatchValue,
        SearchRequest,
    )
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    QdrantClient = None
    models = None

# Hybrid 检索/写入依赖（qdrant-client 通常 >= 1.7）
try:
    from qdrant_client.http.models import (
        NearestQuery,
        SparseVector,
        SparseVectorParams,
    )
    QDRANT_HYBRID_MODELS = True
except ImportError:
    NearestQuery = None  # type: ignore
    SparseVector = None  # type: ignore
    SparseVectorParams = None  # type: ignore
    QDRANT_HYBRID_MODELS = False

logger = logging.getLogger(__name__)

class QdrantConnectionManager:
    """Qdrant连接管理器 - 防止重复连接和初始化"""
    _instances = {}  # key: (url, collection_name) -> QdrantVectorStore instance
    _lock = threading.Lock()
    
    @classmethod
    def get_instance(
        cls, 
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        collection_name: str = "hello_agents_vectors",
        vector_size: int = 512,
        distance: str = "cosine",
        timeout: int = 30,
        **kwargs
    ) -> 'QdrantVectorStore':
        """获取或创建Qdrant实例（单例模式）"""
        # 创建唯一键
        key = (url or "local", collection_name)
        
        if key not in cls._instances:
            with cls._lock:
                # 双重检查锁定
                if key not in cls._instances:
                    logger.debug(f"🔄 创建新的Qdrant连接: {collection_name}")
                    cls._instances[key] = QdrantVectorStore(
                        url=url,
                        api_key=api_key,
                        collection_name=collection_name,
                        vector_size=vector_size,
                        distance=distance,
                        timeout=timeout,
                        **kwargs
                    )
                else:
                    logger.debug(f"♻️ 复用现有Qdrant连接: {collection_name}")
        else:
            logger.debug(f"♻️ 复用现有Qdrant连接: {collection_name}")
            
        return cls._instances[key]

class QdrantVectorStore:
    """Qdrant向量数据库存储实现"""
    
    def __init__(
        self, 
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        collection_name: str = "hello_agents_vectors",
        vector_size: int = 512,
        distance: str = "cosine",
        timeout: int = 30,
        **kwargs
    ):
        """
        初始化Qdrant向量存储 (支持云API)
        
        Args:
            url: Qdrant云服务URL (如果为None则使用本地)
            api_key: Qdrant云服务API密钥
            collection_name: 集合名称
            vector_size: 向量维度
            distance: 距离度量方式 (cosine, dot, euclidean)
            timeout: 连接超时时间
        """
        if not QDRANT_AVAILABLE:
            raise ImportError(
                "qdrant-client未安装。请运行: pip install qdrant-client>=1.6.0"
            )
        
        self.url = url
        self.api_key = api_key
        self.collection_name = collection_name
        self.vector_size = vector_size
        self.timeout = timeout
        # HNSW/Query params via env
        try:
            self.hnsw_m = int(os.getenv("QDRANT_HNSW_M", "32"))
        except Exception:
            self.hnsw_m = 32
        try:
            self.hnsw_ef_construct = int(os.getenv("QDRANT_HNSW_EF_CONSTRUCT", "256"))
        except Exception:
            self.hnsw_ef_construct = 256
        try:
            self.search_ef = int(os.getenv("QDRANT_SEARCH_EF", "128"))
        except Exception:
            self.search_ef = 128
        self.search_exact = os.getenv("QDRANT_SEARCH_EXACT", "0") == "1"
        
        # 距离度量映射
        distance_map = {
            "cosine": Distance.COSINE,
            "dot": Distance.DOT,
            "euclidean": Distance.EUCLID,
        }
        self.distance = distance_map.get(distance.lower(), Distance.COSINE)

        # Hybrid：命名稠密向量 + 命名稀疏向量（与 create_collection / upsert 一致）
        self.dense_vector_name = (os.getenv("QDRANT_DENSE_VECTOR_NAME") or "dense").strip()
        self.sparse_vector_name = (os.getenv("QDRANT_SPARSE_VECTOR_NAME") or "sparse").strip()
        
        # 初始化客户端
        self.client = None
        self._initialize_client()
        
    def _initialize_client(self):
        """初始化Qdrant客户端和集合"""
        try:
            # 根据配置创建客户端连接
            if self.url and self.api_key:
                # 使用云服务API
                self.client = QdrantClient(
                    url=self.url,
                    api_key=self.api_key,
                    timeout=self.timeout
                )
                logger.info(f"✅ 成功连接到Qdrant云服务: {self.url}")
            elif self.url:
                # 使用自定义URL（无API密钥）
                self.client = QdrantClient(
                    url=self.url,
                    timeout=self.timeout
                )
                logger.info(f"✅ 成功连接到Qdrant服务: {self.url}")
            else:
                # 使用本地服务（默认）
                self.client = QdrantClient(
                    host="localhost",
                    port=6333,
                    timeout=self.timeout
                )
                logger.info("✅ 成功连接到本地Qdrant服务: localhost:6333")
            
            # 检查连接
            collections = self.client.get_collections()
            
            # 创建或获取集合
            self._ensure_collection()
            
        except Exception as e:
            logger.error(f"❌ Qdrant连接失败: {e}")
            if not self.url:
                logger.info("💡 本地连接失败，可以考虑使用Qdrant云服务")
                logger.info("💡 或启动本地服务: docker run -p 6333:6333 qdrant/qdrant")
            else:
                logger.info("💡 请检查URL和API密钥是否正确")
            raise
    
    def _validate_hybrid_schema(self) -> None:
        """已存在集合须为 hybrid：命名稠密 + 命名稀疏，否则与 add_vectors 不兼容。"""
        if not QDRANT_HYBRID_MODELS:
            raise RuntimeError(
                "当前 qdrant-client 缺少 SparseVector 等模型，请升级: pip install -U qdrant-client"
            )
        info = self.client.get_collection(self.collection_name)
        params = info.config.params
        sparse_map = getattr(params, "sparse_vectors", None)
        if not sparse_map or self.sparse_vector_name not in sparse_map:
            raise ValueError(
                f"集合「{self.collection_name}」不是 hybrid（需要 sparse_vectors 中包含名称 {self.sparse_vector_name!r}）。"
                f"请改用新的 QDRANT_COLLECTION，或删除该集合后由本类自动重建。"
            )
        vecs = getattr(params, "vectors", None)
        if isinstance(vecs, dict):
            if self.dense_vector_name not in vecs:
                raise ValueError(
                    f"集合「{self.collection_name}」vectors 中缺少稠密向量名 {self.dense_vector_name!r}。"
                )

    def _ensure_collection(self):
        """确保集合存在；新建时为 Qdrant 原生 hybrid（稠密 + 稀疏向量）。"""
        try:
            collections = self.client.get_collections().collections
            collection_names = [c.name for c in collections]

            hnsw_cfg = None
            try:
                hnsw_cfg = models.HnswConfigDiff(m=self.hnsw_m, ef_construct=self.hnsw_ef_construct)
            except Exception:
                hnsw_cfg = None

            if self.collection_name not in collection_names:
                if not QDRANT_HYBRID_MODELS or SparseVectorParams is None:
                    raise ImportError(
                        "需要 qdrant-client 支持 SparseVectorParams（建议 >=1.7）。请执行: pip install -U qdrant-client"
                    )
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config={
                        self.dense_vector_name: VectorParams(
                            size=self.vector_size,
                            distance=self.distance,
                        )
                    },
                    sparse_vectors_config={
                        self.sparse_vector_name: SparseVectorParams(),
                    },
                    hnsw_config=hnsw_cfg,
                )
                logger.info(
                    f"✅ 创建 hybrid 集合: {self.collection_name} "
                    f"(dense={self.dense_vector_name!r}, sparse={self.sparse_vector_name!r})"
                )
            else:
                logger.info(f"✅ 使用现有集合: {self.collection_name}")
                self._validate_hybrid_schema()
                try:
                    self.client.update_collection(
                        collection_name=self.collection_name,
                        hnsw_config=models.HnswConfigDiff(m=self.hnsw_m, ef_construct=self.hnsw_ef_construct),
                    )
                except Exception as ie:
                    logger.debug(f"跳过更新HNSW配置: {ie}")

            self._ensure_payload_indexes()

        except Exception as e:
            logger.error(f"❌ 集合初始化失败: {e}")
            raise

    def _ensure_payload_indexes(self):
        """为常用过滤字段创建payload索引"""
        try:
            index_fields = [
                ("user_id", models.PayloadSchemaType.KEYWORD),
            
            # --- 新增的 RAG 业务索引 ---
                ("filename", models.PayloadSchemaType.KEYWORD),       # 按文件名筛选
                ("file_type", models.PayloadSchemaType.KEYWORD),     # 按文件类型（PDF/Word）筛选
                ("file_path", models.PayloadSchemaType.KEYWORD),
                ("page_number", models.PayloadSchemaType.KEYWORD),
                ("loaded_at", models.PayloadSchemaType.KEYWORD),
                ("chunk_id", models.PayloadSchemaType.KEYWORD),
                ("chunk_level", models.PayloadSchemaType.INTEGER),   # 按层级（1/2/3）筛选
                ("parent_chunk_id", models.PayloadSchemaType.KEYWORD), # 找某个块的所有子块
                ("root_chunk_id", models.PayloadSchemaType.KEYWORD),   # 找某份报告的所有分块
                ("chunk_idx", models.PayloadSchemaType.INTEGER),     # 排序或范围筛选
                
            ]
        
            for field_name, schema_type in index_fields:
                try:
                    self.client.create_payload_index(
                        collection_name=self.collection_name,
                        field_name=field_name,
                        field_schema=schema_type,
                    )
                except Exception as ie:
                    # 索引已存在会报错，忽略
                    logger.debug(f"索引 {field_name} 已存在或创建失败: {ie}")
        except Exception as e:
            logger.debug(f"创建payload索引时出错: {e}")
    
    @staticmethod
    def _sparse_dict_to_qdrant_sparse(sparse: Dict[int, float]):
        """BM25 稀疏 dict[int,float] -> Qdrant ``SparseVector``。"""
        if not SparseVector:
            raise RuntimeError("SparseVector 不可用，请升级 qdrant-client")
        if not sparse:
            return SparseVector(indices=[], values=[])
        items = sorted(sparse.items(), key=lambda kv: kv[0])
        return SparseVector(
            indices=[int(k) for k, _ in items],
            values=[float(v) for _, v in items],
        )

    def add_vectors(
        self,
        metadata: List[Dict[str, Any]],
        ids: Optional[List[str]] = None,
    ) -> bool:
        """
        根据每条记录的文本自行计算稠密向量（Embedding API）与 BM25 稀疏向量（embedding 模块），再写入 Qdrant。

        - **稠密向量**：写入命名向量 ``dense_vector_name``（默认 ``dense``）。
        - **稀疏向量**：写入 Qdrant 原生稀疏向量 ``sparse_vector_name``（默认 ``sparse``），与 ``create_collection`` 中
          ``sparse_vectors_config`` 一致。
        - 若设置环境变量 ``QDRANT_MIRROR_SPARSE_TO_PAYLOAD=1``，会同时在 payload 中镜像
          ``sparse_indices`` / ``sparse_values``（一般不需要）。

        Args:
            metadata: 每条必须包含 ``text``（用于向量化）；其余字段（chunk_id、filename 等）进入 payload。
            ids: 可选，与 metadata 等长；否则用 chunk_id 或自动生成占位。

        Returns:
            bool: 是否成功
        """
        try:
            if not QDRANT_HYBRID_MODELS or SparseVector is None:
                logger.error("当前环境不支持 Qdrant hybrid（缺少 SparseVector 等），请升级 qdrant-client")
                return False

            if not metadata:
                logger.warning("⚠️ metadata 为空")
                return False

            texts: List[str] = []
            for i, meta in enumerate(metadata):
                t = (meta.get("text") or "").strip()
                if not t:
                    logger.error(f"[Qdrant] metadata[{i}] 缺少非空 text 字段，无法生成向量")
                    return False
                texts.append(t)

            # 1) BM25 语料增量（与 embedding 模块一致）
            _embedding_service.increment_add_documents(texts)

            # 2) 稠密 + 稀疏
            dense_vectors = _embedding_service.get_embeddings(texts)
            
            sparse_vectors = _embedding_service.get_sparse_embeddings(texts)

            if len(dense_vectors) != len(metadata) or len(sparse_vectors) != len(metadata):
                logger.error(
                    f"[Qdrant] 向量条数不一致: meta={len(metadata)} dense={len(dense_vectors)} sparse={len(sparse_vectors)}"
                )
                return False

            if ids is None:
                ids = [
                    f"vec_{i}_{int(datetime.now().timestamp() * 1000000)}"
                    for i in range(len(metadata))
                ]
            elif len(ids) != len(metadata):
                logger.error(f"[Qdrant] ids 与 metadata 数量不一致")
                return False

            logger.info(
                f"[Qdrant] add_vectors start: n={len(metadata)} collection={self.collection_name} (dense+sparse from EmbeddingService)"
            )
            points = []
            for i, (vector, sparse_dict, meta, point_id) in enumerate(
                zip(dense_vectors, sparse_vectors, metadata, ids)
            ):
                try:
                    vlen = len(vector)
                except Exception:
                    logger.error(f"[Qdrant] 非法稠密向量: index={i} type={type(vector)}")
                    continue
                if vlen != self.vector_size:
                    logger.warning(
                        f"⚠️ 稠密向量维度不匹配: 期望{self.vector_size}, 实际{vlen}（请检查 QDRANT_VECTOR_SIZE 与 LLM_EMBEDDING 模型维度）"
                    )
                    continue
                    
                # 添加时间戳到元数据
                meta_with_timestamp = meta.copy()
                meta_with_timestamp["timestamp"] = int(datetime.now().timestamp())
                meta_with_timestamp["added_at"] = int(datetime.now().timestamp())

                sparse_q = self._sparse_dict_to_qdrant_sparse(
                    sparse_dict if isinstance(sparse_dict, dict) else {}
                )
                if os.getenv("QDRANT_MIRROR_SPARSE_TO_PAYLOAD", "0").strip() in (
                    "1",
                    "true",
                    "True",
                ):
                    idx_list = list(sparse_q.indices) if sparse_q.indices is not None else []
                    val_list = list(sparse_q.values) if sparse_q.values is not None else []
                    meta_with_timestamp["sparse_indices"] = idx_list
                    meta_with_timestamp["sparse_values"] = val_list

                if "external" in meta_with_timestamp and not isinstance(
                    meta_with_timestamp.get("external"), bool
                ):
                    val = meta_with_timestamp.get("external")
                    meta_with_timestamp["external"] = (
                        True if str(val).lower() in ("1", "true", "yes") else False
                    )

                business_id = (meta_with_timestamp.get("chunk_id") or "").strip()
                if not business_id:
                    business_id = (str(point_id).strip() if point_id is not None else "")
                    if business_id:
                        meta_with_timestamp["chunk_id"] = business_id

                safe_id: Any
                if isinstance(point_id, int):
                    safe_id = point_id
                else:
                    seed = business_id or (str(point_id).strip() if point_id is not None else "") or str(i)
                    safe_id = str(uuid.uuid5(uuid.NAMESPACE_URL, seed))

                point = PointStruct(
                    id=safe_id,
                    vector={
                        self.dense_vector_name: vector,
                        self.sparse_vector_name: sparse_q,
                    },
                    payload=meta_with_timestamp,
                )
                points.append(point)

            if not points:
                logger.warning("⚠️ 没有有效的向量点")
                return False

            logger.info(f"[Qdrant] upsert begin: points={len(points)}")
            self.client.upsert(
                collection_name=self.collection_name,
                points=points,
                wait=True,
            )
            logger.info("[Qdrant] upsert done")
            logger.info(
                f"✅ 成功添加 {len(points)} 个点到 Qdrant（hybrid：{self.dense_vector_name!r} + {self.sparse_vector_name!r}）"
            )
            return True

        except Exception as e:
            logger.error(f"❌ 添加向量失败: {e}")
            return False
    
    @staticmethod
    def _minmax_normalize(scores: Dict[Any, float]) -> Dict[Any, float]:
        """将分数字典 min-max 归一化到 [0, 1]；空或全等时返回均匀 1.0 / 空。"""
        if not scores:
            return {}
        vals = list(scores.values())
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-12:
            return {k: 1.0 for k in scores}
        return {k: (float(v) - lo) / (hi - lo) for k, v in scores.items()}

    def _build_payload_filter(
        self, where: Optional[Dict[str, Any]]
    ) -> Optional[Filter]:
        if not where:
            return None
        conditions = []
        for key, value in where.items():
            if isinstance(value, (str, int, float, bool)):
                conditions.append(
                    FieldCondition(key=key, match=MatchValue(value=value))
                )
        if not conditions:
            return None
        return Filter(must=conditions)

    def search_similar(
        self,
        query_vector: List[float],
        limit: int = 10,
        score_threshold: Optional[float] = None,
        where: Optional[Dict[str, Any]] = None,
        query_text: Optional[str] = None,
        score_alpha: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """
        相似检索：稠密向量必选；若提供 ``query_text`` 则并行稀疏检索并按 ``score_alpha`` 融合。

        融合方式：对稠密、稀疏两路分数分别做 min-max 归一化后线性组合
        ``final = alpha * dense_norm + (1 - alpha) * sparse_norm``。
        ``alpha`` 越大越偏语义；越小越偏 BM25 关键词。仅稠密时忽略 ``query_text`` / ``score_alpha``。

        Args:
            query_vector: 查询稠密向量
            limit: 返回条数
            score_threshold: 作用于**融合后**的 ``score``（若仅稠密则作用于稠密分）
            where: payload 过滤（must 且条件）
            query_text: 非空时启用稀疏分支（与入库相同的 jieba + BM25 稀疏查询向量）
            score_alpha: 稠密权重，默认 0.5，范围会钳制到 [0, 1]

        Returns:
            ``[{"id", "score", "score_dense", "score_sparse", "metadata"}, ...]``（融合时含子分数；仅稠密时后两者与 score 一致或稀疏为 0）
        """
        try:
            if len(query_vector) != self.vector_size:
                logger.error(
                    f"❌ 查询向量维度错误: 期望{self.vector_size}, 实际{len(query_vector)}"
                )
                return []

            query_filter = self._build_payload_filter(where)

            search_params = None
            try:
                search_params = models.SearchParams(
                    hnsw_ef=self.search_ef, exact=self.search_exact
                )
            except Exception:
                search_params = None

            alpha = max(0.0, min(1.0, float(score_alpha)))
            qtext = (query_text or "").strip()

            use_hybrid = bool(
                qtext and QDRANT_HYBRID_MODELS and NearestQuery and SparseVector
            )

            if use_hybrid:
                sparse_dict = _embedding_service.get_sparse_embedding(qtext)
                if not sparse_dict:
                    use_hybrid = False

            if not use_hybrid:
                # —— 仅稠密：NearestQuery.nearest 为 float 列表，向量名走 using= —— #
                if not NearestQuery:
                    logger.error("❌ 当前环境不支持 NearestQuery，无法检索")
                    return []
                q_dense = [float(x) for x in query_vector]
                response = self.client.query_points(
                    collection_name=self.collection_name,
                    query=NearestQuery(nearest=q_dense),
                    using=self.dense_vector_name,
                    query_filter=query_filter,
                    limit=limit,
                    score_threshold=score_threshold,
                    with_payload=True,
                    with_vectors=False,
                    search_params=search_params,
                )
                search_result = response.points

                results: List[Dict[str, Any]] = []
                for hit in search_result:
                    results.append(
                        {
                            "id": hit.id,
                            "score": hit.score,
                            "score_dense": hit.score,
                            "score_sparse": 0.0,
                            "metadata": hit.payload or {},
                        }
                    )
                logger.debug(f"🔍 Qdrant 稠密检索返回 {len(results)} 条")
                return results

            # —— 稠密 + 稀疏：nearest 为向量本体，名称用 using= —— #
            prefetch_limit = min(max(limit * 4, 32), 256)
            q_dense = [float(x) for x in query_vector]
            sparse_q = self._sparse_dict_to_qdrant_sparse(sparse_dict)

            dense_raw: Dict[Any, float] = {}
            sparse_raw: Dict[Any, float] = {}
            payload_by_id: Dict[Any, Any] = {}

            dr = self.client.query_points(
                collection_name=self.collection_name,
                query=NearestQuery(nearest=q_dense),
                using=self.dense_vector_name,
                query_filter=query_filter,
                limit=prefetch_limit,
                with_payload=True,
                with_vectors=False,
                search_params=search_params,
            )
            for hit in dr.points:
                dense_raw[hit.id] = float(hit.score)
                payload_by_id[hit.id] = hit.payload or {}

            sr = self.client.query_points(
                collection_name=self.collection_name,
                query=NearestQuery(nearest=sparse_q),
                using=self.sparse_vector_name,
                query_filter=query_filter,
                limit=prefetch_limit,
                with_payload=True,
                with_vectors=False,
                search_params=search_params,
            )
            for hit in sr.points:
                sparse_raw[hit.id] = float(hit.score)
                if hit.id not in payload_by_id:
                    payload_by_id[hit.id] = hit.payload or {}

            nd = self._minmax_normalize(dense_raw)
            ns = self._minmax_normalize(sparse_raw)
            all_ids = set(nd.keys()) | set(ns.keys())

            fused: List[tuple[float, Any, float, float]] = []
            for pid in all_ids:
                dn = nd.get(pid, 0.0)
                sn = ns.get(pid, 0.0)
                comb = alpha * dn + (1.0 - alpha) * sn
                d_orig = dense_raw.get(pid)
                s_orig = sparse_raw.get(pid)
                fused.append(
                    (
                        comb,
                        pid,
                        float(d_orig) if d_orig is not None else 0.0,
                        float(s_orig) if s_orig is not None else 0.0,
                    )
                )

            fused.sort(key=lambda x: x[0], reverse=True)
            out: List[Dict[str, Any]] = []
            for comb, pid, dscore, sscore in fused[:limit]:
                if score_threshold is not None and comb < score_threshold:
                    continue
                out.append(
                    {
                        "id": pid,
                        "score": comb,
                        "score_dense": dscore,
                        "score_sparse": sscore,
                        "metadata": payload_by_id.get(pid, {}),
                    }
                )

            logger.debug(f"🔍 Qdrant hybrid 检索 fusion 返回 {len(out)} 条 (alpha={alpha})")
            return out

        except Exception as e:
            logger.error(f"❌ 向量搜索失败: {e}")
            return []

    def delete_vectors(self, ids: List[str]) -> bool:
        """
        删除向量
        
        Args:
            ids: 要删除的向量ID列表
        
        Returns:
            bool: 是否成功
        """
        try:
            if not ids:
                return True
                
            operation_info = self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.PointIdsList(
                    points=ids
                ),
                wait=True
            )
            
            logger.info(f"✅ 成功删除 {len(ids)} 个向量")
            return True
            
        except Exception as e:
            logger.error(f"❌ 删除向量失败: {e}")
            return False
    
    def clear_collection(self) -> bool:
        """
        清空集合
        
        Returns:
            bool: 是否成功
        """
        try:
            # 删除并重新创建集合
            self.client.delete_collection(collection_name=self.collection_name)
            self._ensure_collection()
            
            logger.info(f"✅ 成功清空Qdrant集合: {self.collection_name}")
            return True
            
        except Exception as e:
            logger.error(f"❌ 清空集合失败: {e}")
            return False
    
    def delete_memories(self, memory_ids: List[str]):
        """
        删除指定记忆（通过payload中的 memory_id 过滤删除）
        
        注意：由于写入时可能将非UUID的点ID转换为UUID，这里不再依赖点ID，
        而是通过payload中的memory_id来匹配删除，确保一致性。
        """
        try:
            if not memory_ids:
                return
            # 构建 should 过滤条件：memory_id 等于任一给定值
            conditions = [
                FieldCondition(key="memory_id", match=MatchValue(value=mid))
                for mid in memory_ids
            ]
            query_filter = Filter(should=conditions)
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=query_filter),
                wait=True,
            )
            logger.info(f"✅ 成功按memory_id删除 {len(memory_ids)} 个Qdrant向量")
        except Exception as e:
            logger.error(f"❌ 删除记忆失败: {e}")
            raise
    
    def get_collection_info(self) -> Dict[str, Any]:
        """
        获取集合信息
        
        Returns:
            Dict: 集合信息
        """
        try:
            collection_info = self.client.get_collection(self.collection_name)

            info: Dict[str, Any] = {
                "name": self.collection_name,
                "points_count": getattr(collection_info, "points_count", None),
                "segments_count": getattr(collection_info, "segments_count", None),
                "dense_vector_name": self.dense_vector_name,
                "sparse_vector_name": self.sparse_vector_name,
                "config": {
                    "vector_size": self.vector_size,
                    "distance": self.distance.value,
                },
            }
            for k in ("vectors_count", "indexed_vectors_count"):
                if hasattr(collection_info, k):
                    info[k] = getattr(collection_info, k)

            return info
            
        except Exception as e:
            logger.error(f"❌ 获取集合信息失败: {e}")
            return {}
    
    def get_collection_stats(self) -> Dict[str, Any]:
        """
        获取集合统计信息（兼容抽象接口）
        """
        info = self.get_collection_info()
        if not info:
            return {"store_type": "qdrant", "name": self.collection_name}
        info["store_type"] = "qdrant"
        return info

    @staticmethod
    def _vectors_to_jsonable(vectors: Any) -> Any:
        """将 Qdrant 返回的 vector（命名稠密+稀疏或单列表）转为可 JSON 序列化的结构。"""
        if vectors is None:
            return None
        ind = getattr(vectors, "indices", None)
        vals = getattr(vectors, "values", None)
        if ind is not None and vals is not None:
            return {
                "indices": list(ind),
                "values": [float(x) for x in vals],
            }
        if isinstance(vectors, dict):
            return {
                str(k): QdrantVectorStore._vectors_to_jsonable(v)
                for k, v in vectors.items()
            }
        if isinstance(vectors, (list, tuple)):
            return [float(x) for x in vectors]
        return repr(vectors)

    def print_all_points(
        self,
        page_size: int = 128,
        file: Optional[TextIO] = None,
        indent: int = 2,
    ) -> int:
        """
        使用 scroll 遍历当前集合，打印每个点的 id、完整 payload 与向量值
        （hybrid：各命名向量下的稠密浮点列表与稀疏 indices/values）。

        Args:
            page_size: 每页 scroll 条数
            file: 输出流，默认 stdout
            indent: JSON 缩进

        Returns:
            打印的点数量
        """
        out = file if file is not None else sys.stdout
        total = 0
        offset = None
        while True:
            batch = self.client.scroll(
                collection_name=self.collection_name,
                limit=page_size,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            if isinstance(batch, tuple):
                records, offset = batch[0], batch[1]
            elif hasattr(batch, "points"):
                records = batch.points or []
                offset = getattr(batch, "next_page_offset", None)
            else:
                records = []
                offset = None

            for rec in records:
                total += 1
                pid = getattr(rec, "id", None)
                payload = getattr(rec, "payload", None) or {}
                raw_vec = getattr(rec, "vector", None)
                doc = {
                    "index": total,
                    "id": pid,
                    "payload": payload,
                    "vectors": QdrantVectorStore._vectors_to_jsonable(raw_vec),
                }
                line = json.dumps(doc, ensure_ascii=False, indent=indent, default=str)
                print(line, file=out)
                print("-" * 72, file=out)

            if not records or offset is None:
                break

        print(f"[print_all_points] collection={self.collection_name!r} total={total}", file=out)
        return total

    def health_check(self) -> bool:
        """
        健康检查
        
        Returns:
            bool: 服务是否健康
        """
        try:
            # 尝试获取集合列表
            collections = self.client.get_collections()
            return True
        except Exception as e:
            logger.error(f"❌ Qdrant健康检查失败: {e}")
            return False
    
    def __del__(self):
        """析构函数，清理资源"""
        if hasattr(self, 'client') and self.client:
            try:
                self.client.close()
            except:
                pass

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if not QDRANT_AVAILABLE:
        raise SystemExit("pip install -U qdrant-client")

    store = QdrantVectorStore(
        url=(os.getenv("QDRANT_URL") or "").strip(),
        api_key=(os.getenv("QDRANT_API_KEY") or "").strip(),
        collection_name=(os.getenv("QDRANT_COLLECTION") or "hello_agents_vectors").strip(),
        vector_size=int(os.getenv("QDRANT_VECTOR_SIZE") or "512"),
        distance=(os.getenv("QDRANT_DISTANCE") or "cosine").strip(),
        timeout=int(os.getenv("QDRANT_TIMEOUT") or "30"),
    )

    # 优先用真实的 DocumentLoader 产出 chunk dict；如果当前环境缺少依赖，则用虚拟 chunk 做演示写入。
    document: List[Dict[str, Any]]

    from document_loader import DocumentLoader  # 同目录导入

    loader = DocumentLoader()
    document = loader.load_Semantic_document(
        "/Users/maruiyao/Desktop/study/agent/MRY_MedicalRag/bingli/maruiyao.pdf",
        "dxd体检报告.pdf",
    )
    
    metadatas: List[Dict[str, Any]] = []
    ids: List[str] = []
    for chunk in document:
        chunk_id = (chunk.get("chunk_id") or "").strip()
        print(chunk_id)
        if not chunk_id:
            continue
        metadatas.append(chunk)
        ids.append(chunk_id)

    #print(f"[TEST] loaded chunks = {len(document)}; to upsert = {len(metadatas)}; dim = {vector_size}")

    # 2) 写入测试集合（内部用 EmbeddingService 生成稠密 + BM25 稀疏）
    store.clear_collection()
    ok = store.add_vectors(metadata=metadatas, ids=ids)
    print("[TEST] add_vectors ok =", ok)
    
    #store.print_all_points()


    # 3) 用第一条文本的稠密向量做相似检索验证
    print(document)
    batch = store.client.scroll(
        collection_name=store.collection_name, limit=1, with_payload=True, with_vectors=True
    )
    recs = batch[0] if isinstance(batch, tuple) else batch.points
    if not recs:
        raise SystemExit("集合为空，无法取第一条向量")
    raw_v = recs[0].vector
    q = raw_v[store.dense_vector_name] if isinstance(raw_v, dict) else raw_v
    text = (recs[0].payload or {}).get("text") or ""
    hits = store.search_similar(q, limit=5, query_text=text.strip() or None)
    print("hits:", hits)
