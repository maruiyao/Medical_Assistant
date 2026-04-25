"""文本向量化服务 - 支持密集向量和稀疏向量（BM25），词表与 df 持久化 + 增量更新"""
import json
import math
import os
import threading


from collections import Counter
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "bm25_state.json"
# 稀疏词表侧车文件：get_sparse_embeddings 时与主 BM25 状态合并写入（默认 memory/sparse/bingli.json）
_DEFAULT_BINGLI_VOCAB_PATH = Path(__file__).resolve().parent / "sparse" / "bingli.json"


def _strip_env(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().strip('"').strip("'")


class _DashScopeCompatibleDenseEmbeddings:
    """通过 DashScope OpenAI 兼容接口（.env: LLM_BASE_URL / LLM_API_KEY / LLM_EMBEDDING）生成稠密向量。"""

    def __init__(self) -> None:
        api_key = _strip_env(os.getenv("LLM_API_KEY"))
        base_url = _strip_env(os.getenv("LLM_BASE_URL")).rstrip("/")
        model = _strip_env(os.getenv("LLM_EMBEDDING")) or "text-embedding-v3"
        if not api_key or not base_url:
            raise ValueError(
                "稠密向量需要配置 LLM_API_KEY 与 LLM_BASE_URL（与 .env 中 DashScope 兼容接口一致）"
            )
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        # DashScope 兼容接口单批 input 条数上限通常为 10；可用 EMBEDDING_API_BATCH_MAX 调高（如 OpenAI 官方）
        _req = int(os.getenv("EMBEDDING_API_BATCH_SIZE", "10"))
        _cap = int(os.getenv("EMBEDDING_API_BATCH_MAX", "10"))
        self._batch_size = max(1, min(_req, _cap))
        self._normalize = os.getenv("EMBEDDING_NORMALIZE_L2", "1").strip() not in ("0", "false", "False")
        # 与 QDRANT_VECTOR_SIZE 一致；text-embedding-v3 等可通过 dimensions 指定输出维数，设为 0 表示不传（用模型默认全长）
        _dim_raw = _strip_env(os.getenv("EMBEDDING_DIMENSION")) or _strip_env(
            os.getenv("LLM_EMBEDDING_DIMENSION")
        )
        if not _dim_raw:
            _dim_raw = "512"
        try:
            _dim_n = int(_dim_raw)
        except ValueError:
            _dim_n = 512
        self._embedding_dimensions: int | None = _dim_n if _dim_n > 0 else None

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            _kwargs: dict = {"model": self._model, "input": batch}
            if self._embedding_dimensions is not None:
                _kwargs["dimensions"] = self._embedding_dimensions
            resp = self._client.embeddings.create(**_kwargs)
            ordered = sorted(resp.data, key=lambda d: d.index)
            for row in ordered:
                vec = [float(x) for x in row.embedding]
                if self._normalize:
                    arr = np.asarray(vec, dtype=np.float64)
                    nrm = float(np.linalg.norm(arr)) or 1.0
                    vec = (arr / nrm).tolist()
                out.append(vec)
        return out


def _create_dense_embedder() -> _DashScopeCompatibleDenseEmbeddings:
    return _DashScopeCompatibleDenseEmbeddings()


class EmbeddingService:
    """文本向量化服务 - 稠密向量（DashScope 兼容 API）+ BM25 稀疏向量（持久化统计）"""

    def __init__(self, state_path: Path | str | None = None):
        self._embedder = _create_dense_embedder()
        self._state_path = Path(state_path or os.getenv("BM25_STATE_PATH", _DEFAULT_STATE_PATH))
        _bingli = _strip_env(os.getenv("SPARSE_BINGLI_VOCAB_PATH"))
        self._bingli_vocab_path = Path(_bingli) if _bingli else _DEFAULT_BINGLI_VOCAB_PATH
        self._lock = threading.Lock()

        # BM25 参数
        self.k1 = 1.5
        self.b = 0.75

        self._vocab: dict[str, int] = {}
        self._vocab_counter = 0
        self._doc_freq: Counter[str] = Counter()
        self._total_docs = 0
        self._sum_token_len = 0
        self._avg_doc_len = 1.0

        self._load_state()

    def _recompute_avg_len(self) -> None:
        self._avg_doc_len = (
            self._sum_token_len / self._total_docs if self._total_docs > 0 else 1.0
        )

    def _load_state(self) -> None:
        path = self._state_path
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if raw.get("version") != 1:
            return
        self._vocab = {str(k): int(v) for k, v in raw.get("vocab", {}).items()}
        self._doc_freq = Counter({str(k): int(v) for k, v in raw.get("doc_freq", {}).items()})
        self._total_docs = int(raw.get("total_docs", 0))
        self._sum_token_len = int(raw.get("sum_token_len", 0))
        if self._vocab:
            self._vocab_counter = max(self._vocab.values()) + 1
        else:
            self._vocab_counter = 0
        self._recompute_avg_len()

    def _persist_unlocked(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "total_docs": self._total_docs,
            "sum_token_len": self._sum_token_len,
            "vocab": self._vocab,
            "doc_freq": dict(self._doc_freq),
        }
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._state_path)

    def _persist(self) -> None:
        with self._lock:
            self._persist_unlocked()

    def _merge_and_persist_bingli_unlocked(self) -> None:
        """
        将当前内存中的 vocab / doc_freq 与已有 bingli.json 合并后写回。
        同 token 以内存（本次 EmbeddingService）为准，便于与 Qdrant 稀疏向量下标一致。
        """
        path = self._bingli_vocab_path
        path.parent.mkdir(parents=True, exist_ok=True)
        old_vocab: dict[str, int] = {}
        old_df: dict[str, int] = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw.get("vocab"), dict):
                    old_vocab = {str(k): int(v) for k, v in raw["vocab"].items()}
                if isinstance(raw.get("doc_freq"), dict):
                    old_df = {str(k): int(v) for k, v in raw["doc_freq"].items()}
            except (json.JSONDecodeError, OSError, ValueError, TypeError):
                pass
        merged_vocab = {**old_vocab, **self._vocab}
        merged_df = {**old_df, **{k: int(v) for k, v in self._doc_freq.items()}}
        payload = {
            "version": 1,
            "vocab": merged_vocab,
            "doc_freq": merged_df,
            "total_docs": self._total_docs,
            "sum_token_len": self._sum_token_len,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def increment_add_documents(self, texts: list[str]) -> None:
        """
        将每个 text 视为 BM25 中的一篇文档（与当前 chunk 写入粒度一致），增量更新 N / df / 长度和。
        """
        if not texts:
            return
        with self._lock:
            for text in texts:
                tokens = self.tokenize(text)
                doc_len = len(tokens)
                self._sum_token_len += doc_len
                self._total_docs += 1
                for token in set(tokens):
                    if token not in self._vocab:
                        self._vocab[token] = self._vocab_counter
                        self._vocab_counter += 1
                    self._doc_freq[token] += 1
            self._recompute_avg_len()
            self._persist_unlocked()

    def increment_remove_documents(self, texts: list[str]) -> None:
        """
        从语料统计中移除与 increment_add_documents 对称的文档集合（如删除某文件的全部 chunk 文本）。
        词表索引不回收，避免与 Milvus 中仍可能存在的旧稀疏向量维度冲突。
        """
        if not texts:
            return
        with self._lock:
            for text in texts:
                tokens = self.tokenize(text)
                doc_len = len(tokens)
                self._sum_token_len = max(0, self._sum_token_len - doc_len)
                self._total_docs = max(0, self._total_docs - 1)
                for token in set(tokens):
                    if token not in self._doc_freq:
                        continue
                    self._doc_freq[token] -= 1
                    if self._doc_freq[token] <= 0:
                        del self._doc_freq[token]
            self._recompute_avg_len()
            self._persist_unlocked()

    def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            return self._embedder.embed_documents(texts)
        except Exception as e:
            raise Exception(f"本地嵌入模型调用失败: {str(e)}") from e

    def tokenize(self, text: str) -> list[str]:
        # 使用 jieba 识别中文词组（惰性导入，避免未安装时 import 本模块即失败）
        try:
            import jieba as _jieba
        except ImportError as e:
            raise ImportError(
                "未安装 jieba，BM25 分词不可用。请在当前环境执行: pip install jieba"
            ) from e
        return [t.lower() for t in _jieba.cut(text) if t.strip()]

    def _sparse_vector_for_text_unlocked(self, text: str) -> tuple[dict, bool]:
        tokens = self.tokenize(text)
        doc_len = len(tokens)
        tf = Counter(tokens)
        sparse_vector: dict[int, float] = {}
        vocab_changed = False
        n = max(self._total_docs, 0)
        avg = max(self._avg_doc_len, 1.0)

        for token, freq in tf.items():
            if token not in self._vocab:
                self._vocab[token] = self._vocab_counter
                self._vocab_counter += 1
                vocab_changed = True

            idx = self._vocab[token]
            df = self._doc_freq.get(token, 0)
            if df == 0:
                idf = math.log((n + 1) / 1)
            else:
                idf = math.log((n - df + 0.5) / (df + 0.5) + 1)

            numerator = freq * (self.k1 + 1)
            denominator = freq + self.k1 * (1 - self.b + self.b * doc_len / avg)
            score = idf * numerator / denominator
            if score > 0:
                sparse_vector[idx] = float(score)

        return sparse_vector, vocab_changed

    def get_sparse_embedding(self, text: str) -> dict:
        with self._lock:
            sparse_vector, vocab_changed = self._sparse_vector_for_text_unlocked(text)
            if vocab_changed:
                self._persist_unlocked()
            self._merge_and_persist_bingli_unlocked()
        return sparse_vector

    def get_sparse_embeddings(self, texts: list[str]) -> list[dict]:
        if not texts:
            return []
        with self._lock:
            out: list[dict] = []
            any_new_vocab = False
            for text in texts:
                sparse_vector, vocab_changed = self._sparse_vector_for_text_unlocked(text)
                out.append(sparse_vector)
                any_new_vocab = any_new_vocab or vocab_changed
            if any_new_vocab:
                self._persist_unlocked()
            self._merge_and_persist_bingli_unlocked()
        return out

    def get_all_embeddings(self, texts: list[str]) -> tuple[list[list[float]], list[dict]]:
        dense_embeddings = self.get_embeddings(texts)
        sparse_embeddings = self.get_sparse_embeddings(texts)
        return dense_embeddings, sparse_embeddings


class _LazyEmbeddingService:
    """惰性创建默认 EmbeddingService，避免仅跑 BM25 自检时也必须初始化稠密 API。"""

    __slots__ = ("_instance",)

    def __init__(self) -> None:
        self._instance: EmbeddingService | None = None

    def _get(self) -> EmbeddingService:
        if self._instance is None:
            self._instance = EmbeddingService()
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._get(), name)


# 全进程唯一实例：写入与检索共用同一份 BM25 持久化状态（首次访问属性时创建）
embedding_service = _LazyEmbeddingService()


def bm25_selftest(sentence: str) -> None:
    """BM25 稀疏向量自检：分词、词表、稀疏向量（使用独立 state 文件，不写默认 bm25_state）。"""
    state_path = Path(__file__).resolve().parent.parent / "tmp_data" / "bm25_sparse_selftest.json"
    svc = EmbeddingService(state_path=state_path)

    print("=== 输入句子 ===")
    print(sentence)
    print()

    tokens = svc.tokenize(sentence)
    print("=== 切分结果（token 列表；jieba 分词）===")
    print(tokens)
    print("token 数量:", len(tokens))
    print()

    # 把本句当作语料里的一篇文档，更新 N / df，稀疏向量的 idf 才有意义
    svc.increment_add_documents([sentence])

    with svc._lock:
        vocab_snapshot = dict(svc._vocab)
        doc_freq_snapshot = dict(svc._doc_freq)
        n_docs = svc._total_docs
        avg_len = svc._avg_doc_len

    print("=== 语料统计（increment_add_documents 之后）===")
    print("total_docs:", n_docs, "avg_doc_len:", round(avg_len, 4))
    print()

    print("=== 当前词表 token -> id（按 id 排序）===")
    for tok, idx in sorted(vocab_snapshot.items(), key=lambda kv: kv[1]):
        df = doc_freq_snapshot.get(tok, 0)
        print(f"  [{idx:4d}] {tok!r}  df={df}")
    print("词表大小:", len(vocab_snapshot))
    print()

    sparse = svc.get_sparse_embedding(sentence)
    print("=== 本句稀疏向量（下标 -> BM25 权重，按 id 排序）===")
    for idx in sorted(sparse.keys()):
        print(f"  {idx}: {sparse[idx]:.6f}")
    print("非零维度数:", len(sparse))


if __name__ == "__main__":
    
    import sys

    default_sentence = "你这个大笨蛋"
    sentence = sys.argv[1] if len(sys.argv) > 1 else default_sentence

    print(sentence)
    print("---------- BM25 稀疏向量测试 ----------\n")
    bm25_selftest(sentence)
    