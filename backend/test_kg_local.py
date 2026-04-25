#!/usr/bin/env python3
"""
本地快速测知识图谱链路（不启动 FastAPI）。

用法（在项目根目录 Medical-Assistant 下）:
  uv run python backend/test_kg_local.py
  uv run python backend/test_kg_local.py "高血压的常见症状有哪些"

依赖: .env 中 NEO4J_URI / NEO4J_PASSWORD、ARK 意图模型；backend/model 下 NER 权重；
      项目根下 data/ent_aug（或设 NKG_CWD）；tmp_data/tag2idx.npy
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = Path(__file__).resolve().parent

# 与 uvicorn 从 backend 导入时一致：backend 包在 path 最前
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
load_dotenv(BACKEND / ".env")

# 先 import：在 py2neo 首次被加载前把 NEO4J_URI 从 neo4j* 规范为 bolt*（见 neo4j_queries 模块说明）
import neo4j_queries  # noqa: F401


def main() -> None:
    q = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "百日咳有什么症状，一般吃什么药"
    )
    print("查询:", q)
    print("-" * 60)
    try:
        from graphrag_pipeline import run_knowledge_graph_search

        out = run_knowledge_graph_search(q, emit=None)
        print(out)
    except Exception as e:
        print("失败:", type(e).__name__, e)
        raise


if __name__ == "__main__":
    main()
