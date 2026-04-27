"""
知识图谱工具编排（方式 A）：意图 LLM + BERT NER + Neo4j，只返回 <提示> 事实块，不在这里生成最终自然语言回答。
由 LangChain agent 在收到工具结果后统一作答。
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from dotenv import load_dotenv

load_dotenv()

Emit = Optional[Callable[[str, str, str], None]]


def run_knowledge_graph_search(query: str, emit: Emit = None) -> str:
    from intent import run_intent_recognition
    from neo4j_queries import build_kg_tool_context, get_neo4j_graph
    from ner_loader import get_ner_bundle, run_ner_entities

    efn = emit or (lambda _i, _l, _d: None)

    try:
        graph = get_neo4j_graph()
    except Exception as ex:
        return str(ex)

    if graph is None:
        return (
            "知识图谱不可用：未配置 NEO4J_URI/NEO4J_PASSWORD，或缺少 py2neo。"
            "请检查 .env 与依赖。"
        )

    efn("🧠", "医疗意图识别中…", query[:60])
    try:
        intent_text = run_intent_recognition(query)
    except Exception as ex:
        return f"意图识别失败: {ex!s}"

    efn("🧩", "实体识别 (BERT+规则)…", query[:60])
    try:
        bundle = get_ner_bundle()
        entities = run_ner_entities(bundle, query)
    except FileNotFoundError as ex:
        return f"NER 资源未就绪: {ex}"
    except Exception as ex:
        return f"实体识别失败: {ex!s}"

    efn("🕸️", "从 Neo4j 拉取子图…", "")
    text, _yitu, _e = build_kg_tool_context(query, intent_text, entities, graph, efn)
    return text
