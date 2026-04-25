"""
懒加载 BERT+规则 NER（复用 medical/ner_model 推理逻辑）。
运行目录依赖：data/ent_aug 下词典与 tmp_data/tag2idx.npy 需可通过 NKG_CWD 找到（默认项目根目录）。
"""
from __future__ import annotations

import os
import pickle
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

# 项目根：…/Medical-Assistant
BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent


def _ensure_repo_on_path() -> None:
    r = str(REPO_ROOT)
    if r not in sys.path:
        sys.path.insert(0, r)


@dataclass
class NERBundle:
    model: Any
    tokenizer: Any
    device: Any
    idx2tag: List[str]
    rule: Any
    tfidf_r: Any


def _chdir_cwd() -> str:
    """ner_model 中 rule_find / tfidf_alignment 使用 data/ent_aug（相对路径）。"""
    base = os.environ.get("NKG_CWD", str(REPO_ROOT))
    old = os.getcwd()
    os.chdir(base)
    return old


@lru_cache(maxsize=1)
def get_ner_bundle() -> NERBundle:
    import torch
    from transformers import BertTokenizer

    _ensure_repo_on_path()
    from medical.ner_model import Bert_Model, rule_find, tfidf_alignment

    tag2idx = _load_tag2idx()
    idx2tag = list(tag2idx)
    roberta_dir = BACKEND_DIR / "model" / "chinese-roberta-wwm-ext"
    pt = BACKEND_DIR / "model" / "best_roberta_rnn_model_ent_aug.pt"
    if not roberta_dir.is_dir():
        raise FileNotFoundError(f"未找到 RoBERTa 目录: {roberta_dir}")
    if not pt.is_file():
        raise FileNotFoundError(f"未找到 NER 权重: {pt}")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model_name = str(roberta_dir)
    tokenizer = BertTokenizer.from_pretrained(model_name)
    model = Bert_Model(model_name, hidden_size=128, tag_num=len(tag2idx), bi=True)
    state = torch.load(pt, map_location=device)
    model.load_state_dict(state)
    model = model.to(device)
    model.eval()

    old = _chdir_cwd()
    try:
        rule = rule_find()
        tfidf = tfidf_alignment()
    finally:
        os.chdir(old)

    return NERBundle(
        model=model,
        tokenizer=tokenizer,
        device=device,
        idx2tag=idx2tag,
        rule=rule,
        tfidf_r=tfidf,
    )


def _load_tag2idx() -> dict:
    candidates = [
        BACKEND_DIR / "tmp_data" / "tag2idx.npy",
        REPO_ROOT / "medical" / "tmp_data" / "tag2idx.npy",
        REPO_ROOT / "tmp_data" / "tag2idx.npy",
    ]
    for p in candidates:
        if p.is_file():
            with open(p, "rb") as f:
                return pickle.load(f)
    raise FileNotFoundError(
        "未找到 tag2idx.npy。请将文件放到 backend/tmp_data/ 或 项目根 tmp_data/ 或 medical/tmp_data/。"
    )


def run_ner_entities(bundle: NERBundle, text: str) -> Dict[str, str]:
    """返回与 medical/webui 一致的 dict: 类型名 -> 规范化实体（可能对齐到词典）。"""
    _ensure_repo_on_path()
    from medical.ner_model import get_ner_result

    out = get_ner_result(
        bundle.model,
        bundle.tokenizer,
        text,
        bundle.rule,
        bundle.tfidf_r,
        bundle.device,
        bundle.idx2tag,
    )
    if not isinstance(out, dict):
        return {}
    return out
