"""Medical RAG Agent — LangGraph 图定义

流程拓扑（线性两阶段，episodic/semantic 并行后汇聚到 neo4j）：

  START → router ─┬─ episodic_query（if is_episodic）─┐
                   │                                    ├─ neo4j_query → synthesize → END
                   └─ semantic_query（if is_semantic）──┘

  - router 从 state 读取 is_episodic / is_semantic / is_nextneo4j
  - episodic 和 semantic 可并行；skip 的分支直接汇聚到 neo4j_query
  - neo4j_query 等待所有上游分支完成后再执行
  - synthesize 汇总所有 context，生成回答
"""
from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages


def _last(existing: str, new: str) -> str:
    """并行分支写同一 key 时只保留最后到达的值。"""
    return new


# ─────────────────────────── State ───────────────────────────

class OverallState(TypedDict):
    is_episodic: bool
    episodic_query: dict
    semantic_query: dict
    nextneo4j_query: dict
    is_semantic: bool
    is_nextneo4j: bool
    episodic_context: dict
    semantic_context: dict
    nextneo4j_context: dict
    is_step_back: bool
    messages: Annotated[list, add_messages]
    current_step: Annotated[str, _last]


# ─────────────────────────── Nodes ───────────────────────────

def router(state: OverallState) -> dict:
    """解析用户意图，决定走哪几条检索分支。（TODO：接入 LLM 或规则引擎）"""
    return {"current_step": "router"}


def episodic_query(state: OverallState) -> dict:
    """查询情景记忆。（TODO：调用 RAGPipeline.search_and_merge）"""
    print("查询情景记忆")
    return {"episodic_context": {}, "current_step": "episodic_query"}


def semantic_query(state: OverallState) -> dict:
    """查询语义记忆。（TODO：调用语义知识库检索）"""
    print("查询语义记忆")
    return {"semantic_context": {}, "current_step": "semantic_query"}

def memory_gate(state: OverallState) -> dict:
    """汇聚 episodic/semantic 记忆。"""
    print("汇聚情景记忆和语义记忆")
    return {"current_step": "memory_gate"}


def neo4j_query(state: OverallState) -> dict:
    """查询知识图谱。等待 episodic/semantic 全部完成后执行。（TODO：调用 Neo4j 检索）"""
    print("查询知识图谱")
    return {"nextneo4j_context": {}, "current_step": "neo4j_query"}


def synthesize(state: OverallState) -> dict:
    """汇总各路检索结果，生成最终回答。（TODO：调用 LLM 合成）"""
    return {"current_step": "synthesize"}


# ─────────────────────── Conditional edges ───────────────────

def should_episodic(state: OverallState) -> Literal["episodic_query", "skip_episodic"]:
    if state.get("is_episodic"):
        return "episodic_query"
    return "skip_episodic"


def should_semantic(state: OverallState) -> Literal["semantic_query", "skip_semantic"]:
    if state.get("is_semantic"):
        return "semantic_query"
    return "skip_semantic"


def should_neo4j(state: OverallState) -> Literal["neo4j_query", "skip_neo4j"]:
    """memory_gate 汇聚后：is_nextneo4j=True → 执行 neo4j；False → 跳过直接 synthesize"""
    if state.get("is_nextneo4j"):
        return "neo4j_query"
    return "skip_neo4j"


# ─────────────────────── Graph assembly ──────────────────────

def build_graph() -> StateGraph:
    g = StateGraph(OverallState)

    g.add_node("router", router)
    g.add_node("episodic_query", episodic_query)
    g.add_node("semantic_query", semantic_query)
    g.add_node("memory_gate", memory_gate)
    g.add_node("neo4j_query", neo4j_query)
    g.add_node("synthesize", synthesize)

    # START → router
    g.add_edge(START, "router")

    # router 并行扇出两条分支，skip 直接汇聚到 memory_gate
    g.add_conditional_edges(
        "router",
        should_episodic,
        {"episodic_query": "episodic_query", "skip_episodic": "memory_gate"},
    )
    g.add_conditional_edges(
        "router",
        should_semantic,
        {"semantic_query": "semantic_query", "skip_semantic": "memory_gate"},
    )

    # episodic / semantic 完成后 → memory_gate（fan-in：等所有上游分支到齐）
    g.add_edge("episodic_query", "memory_gate")
    g.add_edge("semantic_query", "memory_gate")

    # memory_gate → 条件判断 is_nextneo4j → neo4j_query 或直接 synthesize
    g.add_conditional_edges(
        "memory_gate",
        should_neo4j,
        {"neo4j_query": "neo4j_query", "skip_neo4j": "synthesize"},
    )

    # neo4j_query → synthesize → END
    g.add_edge("neo4j_query", "synthesize")
    g.add_edge("synthesize", END)

    return g


graph = build_graph().compile()


if __name__ == "__main__":
    cases = [
        {"label": "epi=T sem=T neo4j=T → 并行记忆 → neo4j → synthesize",
         "is_episodic": True, "is_semantic": True, "is_nextneo4j": True},
        {"label": "epi=T sem=F neo4j=T → episodic → neo4j → synthesize",
         "is_episodic": True, "is_semantic": False, "is_nextneo4j": True},
        {"label": "epi=T sem=T neo4j=F → 并行记忆 → skip neo4j → synthesize",
         "is_episodic": True, "is_semantic": True, "is_nextneo4j": False},
        {"label": "epi=F sem=F neo4j=F → skip all → synthesize",
         "is_episodic": False, "is_semantic": False, "is_nextneo4j": False},
    ]
    for c in cases:
        print(f"=== {c['label']} ===")
        r = graph.invoke({
            "is_episodic": c["is_episodic"],
            "is_semantic": c["is_semantic"],
            "is_nextneo4j": c["is_nextneo4j"],
            "messages": [("user", "test")],
        })
        print(f"  final step: {r.get('current_step')}\n")
