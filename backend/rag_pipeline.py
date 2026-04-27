from typing import Literal, TypedDict, List, Optional
import os
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field

from rag_utils import retrieve_documents, step_back_expand, generate_hypothetical_document
from tools import emit_rag_step

load_dotenv()

API_KEY = os.getenv("ARK_API_KEY")
MODEL = os.getenv("MODEL")
BASE_URL = os.getenv("BASE_URL")
GRADE_MODEL = os.getenv("GRADE_MODEL", "gpt-4.1")

_grader_model = None
_router_model = None


def _get_grader_model():
    global _grader_model
    if not API_KEY or not GRADE_MODEL:
        return None
    if _grader_model is None:
        _grader_model = init_chat_model(
            model=GRADE_MODEL,
            model_provider="openai",
            api_key=API_KEY,
            base_url=BASE_URL,
            temperature=0,
            stream_usage=True,
        )
    return _grader_model


def _get_router_model():
    global _router_model
    if not API_KEY or not MODEL:
        return None
    if _router_model is None:
        _router_model = init_chat_model(
            model=MODEL,
            model_provider="openai",
            api_key=API_KEY,
            base_url=BASE_URL,
            temperature=0,
            stream_usage=True,
        )
    return _router_model


GRADE_PROMPT = (
    "You are a grader assessing relevance of a retrieved document to a user question. \n "
    "Here is the retrieved document: \n\n {context} \n\n"
    "Here is the user question: {question} \n"
    "If the document contains keyword(s) or semantic meaning related to the user question, grade it as relevant. \n"
    "Give a binary score 'yes' or 'no' score to indicate whether the document is relevant to the question."
)


class GradeDocuments(BaseModel):
    """Grade documents using a binary score for relevance check."""

    binary_score: str = Field(
        description="Relevance score: 'yes' if relevant, or 'no' if not relevant"
    )


class RewriteStrategy(BaseModel):
    """Choose a query expansion strategy."""

    strategy: Literal["step_back", "hyde", "complex"]


class RouteDecision(BaseModel):
    """医疗检索：是否需要查询情景记忆 / 语义记忆。"""

    need_episodic: bool = Field(
        description="若问题与用户个人病历、就诊或检查报告相关则为 True"
    )
    need_semantic: bool = Field(
        description="若与药品说明、医学论文、通用医学知识相关则为 True"
    )


class RAGState(TypedDict):
    question: str
    query: str
    context: str
    docs: List[dict]
    route: Optional[str]
    expansion_type: Optional[str]
    expanded_query: Optional[str]
    step_back_question: Optional[str]
    step_back_answer: Optional[str]
    hypothetical_doc: Optional[str]
    rag_trace: Optional[dict]
    need_episodic: Optional[bool]
    need_semantic: Optional[bool]
    episodic_query: Optional[str]
    semantic_query: Optional[str]
    episodic_docs: Optional[List[dict]]
    semantic_docs: Optional[List[dict]]


def _format_docs(docs: List[dict]) -> str:
    if not docs:
        return ""
    chunks = []
    for i, doc in enumerate(docs, 1):
        source = doc.get("filename", "Unknown")
        page = doc.get("page_number", "N/A")
        text = doc.get("text", "")
        chunks.append(f"[{i}] {source} (Page {page}):\n{text}")
    return "\n\n---\n\n".join(chunks)


ROUTE_PROMPT = (
    "你是医疗检索路由器。请判断以下用户问题需要查询哪些知识源：\n"
    "- need_episodic：与用户个人病历、就诊记录、检查报告相关，也被称为情景记忆\n"
    "- need_semantic：与药品说明书、医学论文、通用医学知识相关，也被称为语义记忆\n"
    "若无法判断，默认 need_semantic=true。\n"
    "一定要认真的判断"
    "用户问题：{question}"
)


def _merge_branch_meta(a: dict, b: dict) -> dict:
    if not a:
        return dict(b) if b else {}
    if not b:
        return dict(a)
    return {
        "rerank_enabled": bool(a.get("rerank_enabled") or b.get("rerank_enabled")),
        "rerank_applied": bool(a.get("rerank_applied") or b.get("rerank_applied")),
        "rerank_model": a.get("rerank_model") or b.get("rerank_model"),
        "rerank_endpoint": a.get("rerank_endpoint") or b.get("rerank_endpoint"),
        "rerank_error": a.get("rerank_error") or b.get("rerank_error"),
        "retrieval_mode": a.get("retrieval_mode") or b.get("retrieval_mode"),
        "candidate_k": max(
            (a.get("candidate_k") or 0) or 0, (b.get("candidate_k") or 0) or 0
        ),
        "leaf_retrieve_level": a.get("leaf_retrieve_level") or b.get("leaf_retrieve_level"),
        "auto_merge_enabled": a.get("auto_merge_enabled", b.get("auto_merge_enabled")),
        "auto_merge_applied": bool(a.get("auto_merge_applied") or b.get("auto_merge_applied")),
        "auto_merge_threshold": a.get("auto_merge_threshold") or b.get("auto_merge_threshold"),
        "auto_merge_replaced_chunks": (a.get("auto_merge_replaced_chunks") or 0)
        + (b.get("auto_merge_replaced_chunks") or 0),
        "auto_merge_steps": (a.get("auto_merge_steps") or 0) + (b.get("auto_merge_steps") or 0),
    }


def _dedupe_and_rank_docs(docs: List[dict]) -> List[dict]:
    if not docs:
        return []
    seen = set()
    out = []
    for item in docs:
        key = item.get("chunk_id") or (
            item.get("filename"),
            item.get("page_number"),
            item.get("text"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=lambda d: d.get("score", 0.0) or 0.0, reverse=True)
    for idx, item in enumerate(out, 1):
        item["rrf_rank"] = idx
    return out


def _emit_retrieve_path_steps(
    label_prefix: str, results: List[dict], meta: dict
) -> None:
    emit_rag_step(
        "🧱",
        f"{label_prefix}三级分块检索",
        (
            f"叶子层 L{meta.get('leaf_retrieve_level', 3)} 召回，"
            f"候选 {meta.get('candidate_k', 0)}"
        ),
    )
    emit_rag_step(
        "🧩",
        f"{label_prefix}Auto-merging 合并",
        (
            f"启用: {bool(meta.get('auto_merge_enabled'))}，"
            f"应用: {bool(meta.get('auto_merge_applied'))}，"
            f"替换片段: {meta.get('auto_merge_replaced_chunks', 0)}"
        ),
    )
    emit_rag_step(
        "✅",
        f"{label_prefix}检索完成，找到 {len(results)} 个片段",
        f"模式: {meta.get('retrieval_mode', 'hybrid')}",
    )


def route_and_build_queries_node(state: RAGState) -> RAGState:
    question = state["question"]
    need_episodic = False
    need_semantic = True
    router = _get_router_model()
    if router:
        try:
            decision = router.with_structured_output(RouteDecision).invoke(
                [{"role": "user", "content": ROUTE_PROMPT.format(question=question)}]
            )
            need_episodic = bool(decision.need_episodic)
            need_semantic = bool(decision.need_semantic)
        except Exception:
            need_episodic = False
            need_semantic = True
    if not need_episodic and not need_semantic:
        need_semantic = True
    episodic_query = question
    semantic_query = question
    emit_rag_step(
        "🧭",
        f"路由识别完成 need_episodic={need_episodic} need_semantic={need_semantic}",
        "",
    )
    if need_episodic:
        emit_rag_step(
            "📝",
            f"[情景记忆] 查询语句: {episodic_query[:120]}"
            + ("..." if len(episodic_query) > 120 else ""),
            "",
        )
    if need_semantic:
        emit_rag_step(
            "📝",
            f"[语义记忆] 查询语句: {semantic_query[:120]}"
            + ("..." if len(semantic_query) > 120 else ""),
            "",
        )
    return {
        "need_episodic": need_episodic,
        "need_semantic": need_semantic,
        "episodic_query": episodic_query,
        "semantic_query": semantic_query,
    }


def _run_branch_retrieve(q: str, kbts: List[str]) -> tuple:
    r = retrieve_documents(q, top_k=5, kb_types=kbts)
    return r.get("docs", []), r.get("meta", {})


def retrieve_initial(state: RAGState) -> RAGState:
    question = state["question"]
    need_episodic = state.get("need_episodic")
    need_semantic = state.get("need_semantic")
    episodic_query = state.get("episodic_query") or question
    semantic_query = state.get("semantic_query") or question
    if need_episodic is None and need_semantic is None:
        need_episodic, need_semantic = False, True
    if need_episodic is None:
        need_episodic = False
    if need_semantic is None:
        need_semantic = True

    emit_rag_step("🔍", "正在并行检索知识库...", f"查询: {question[:80]}")

    epi_results: List[dict] = []
    sem_results: List[dict] = []
    epi_meta: dict = {}
    sem_meta: dict = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_ep = (
            ex.submit(_run_branch_retrieve, episodic_query, ["medical_record"])
            if need_episodic
            else None
        )
        f_se = (
            ex.submit(_run_branch_retrieve, semantic_query, ["medication"])
            if need_semantic
            else None
        )
        if need_episodic:
            emit_rag_step("🔍", "[情景记忆] 开始检索", "")
        if need_semantic:
            emit_rag_step("🔍", "[语义记忆] 开始检索", "")
        epi_results, epi_meta = f_ep.result() if f_ep else ([], {})
        sem_results, sem_meta = f_se.result() if f_se else ([], {})
        if need_episodic:
            _emit_retrieve_path_steps("[情景记忆] ", epi_results, epi_meta)
        if need_semantic:
            _emit_retrieve_path_steps("[语义记忆] ", sem_results, sem_meta)

    episodic_docs = list(epi_results)
    semantic_docs = list(sem_results)
    results = _dedupe_and_rank_docs(epi_results + sem_results)
    retrieve_meta = _merge_branch_meta(epi_meta, sem_meta)
    context = _format_docs(results)
    query = question
    rag_trace = {
        "tool_used": True,
        "tool_name": "search_knowledge_base",
        "query": query,
        "expanded_query": query,
        "retrieved_chunks": results,
        "initial_retrieved_chunks": results,
        "retrieval_stage": "initial",
        "route_decision": {
            "need_episodic": need_episodic,
            "need_semantic": need_semantic,
        },
        "episodic_query": episodic_query,
        "semantic_query": semantic_query,
        "episodic_retrieved_chunks": episodic_docs,
        "semantic_retrieved_chunks": semantic_docs,
        "rerank_enabled": retrieve_meta.get("rerank_enabled"),
        "rerank_applied": retrieve_meta.get("rerank_applied"),
        "rerank_model": retrieve_meta.get("rerank_model"),
        "rerank_endpoint": retrieve_meta.get("rerank_endpoint"),
        "rerank_error": retrieve_meta.get("rerank_error"),
        "retrieval_mode": retrieve_meta.get("retrieval_mode"),
        "candidate_k": retrieve_meta.get("candidate_k"),
        "leaf_retrieve_level": retrieve_meta.get("leaf_retrieve_level"),
        "auto_merge_enabled": retrieve_meta.get("auto_merge_enabled"),
        "auto_merge_applied": retrieve_meta.get("auto_merge_applied"),
        "auto_merge_threshold": retrieve_meta.get("auto_merge_threshold"),
        "auto_merge_replaced_chunks": retrieve_meta.get("auto_merge_replaced_chunks"),
        "auto_merge_steps": retrieve_meta.get("auto_merge_steps"),
    }
    return {
        "query": query,
        "docs": results,
        "context": context,
        "rag_trace": rag_trace,
        "episodic_docs": episodic_docs,
        "semantic_docs": semantic_docs,
    }


def grade_documents_node(state: RAGState) -> RAGState:
    grader = _get_grader_model()
    emit_rag_step("📊", "正在评估文档相关性...")
    if not grader:
        grade_update = {
            "grade_score": "unknown",
            "grade_route": "rewrite_question",
            "rewrite_needed": True,
        }
        rag_trace = state.get("rag_trace", {}) or {}
        rag_trace.update(grade_update)
        return {"route": "rewrite_question", "rag_trace": rag_trace}
    question = state["question"]
    context = state.get("context", "")
    prompt = GRADE_PROMPT.format(question=question, context=context)
    response = grader.with_structured_output(GradeDocuments).invoke(
        [{"role": "user", "content": prompt}]
    )
    score = (response.binary_score or "").strip().lower()
    route = "generate_answer" if score == "yes" else "rewrite_question"
    if route == "generate_answer":
        emit_rag_step("✅", "文档相关性评估通过", f"评分: {score}")
    else:
        emit_rag_step("⚠️", "文档相关性不足，将重写查询", f"评分: {score}")
    grade_update = {
        "grade_score": score,
        "grade_route": route,
        "rewrite_needed": route == "rewrite_question",
    }
    rag_trace = state.get("rag_trace", {}) or {}
    rag_trace.update(grade_update)
    return {"route": route, "rag_trace": rag_trace}


def rewrite_question_node(state: RAGState) -> RAGState:
    question = state["question"]
    emit_rag_step("✏️", "正在重写查询...")
    router = _get_router_model()
    strategy = "step_back"
    if router:
        prompt = (
            "请根据用户问题选择最合适的查询扩展策略，仅输出策略名。\n"
            "- step_back：包含具体名称、日期、代码等细节，需要先理解通用概念的问题。\n"
            "- hyde：模糊、概念性、需要解释或定义的问题。\n"
            "- complex：多步骤、需要分解或综合多种信息的复杂问题。\n"
            f"用户问题：{question}"
        )
        try:
            decision = router.with_structured_output(RewriteStrategy).invoke(
                [{"role": "user", "content": prompt}]
            )
            strategy = decision.strategy
        except Exception:
            strategy = "step_back"

    expanded_query = question
    step_back_question = ""
    step_back_answer = ""
    hypothetical_doc = ""

    if strategy in ("step_back", "complex"):
        emit_rag_step("🧠", f"使用策略: {strategy}", "生成退步问题")
        step_back = step_back_expand(question)
        step_back_question = step_back.get("step_back_question", "")
        step_back_answer = step_back.get("step_back_answer", "")
        expanded_query = step_back.get("expanded_query", question)

    if strategy in ("hyde", "complex"):
        emit_rag_step("📝", "HyDE 假设性文档生成中...")
        hypothetical_doc = generate_hypothetical_document(question)

    rag_trace = state.get("rag_trace", {}) or {}
    rag_trace.update({
        "rewrite_strategy": strategy,
        "rewrite_query": expanded_query,
    })

    return {
        "expansion_type": strategy,
        "expanded_query": expanded_query,
        "step_back_question": step_back_question,
        "step_back_answer": step_back_answer,
        "hypothetical_doc": hypothetical_doc,
        "rag_trace": rag_trace,
    }


def retrieve_expanded(state: RAGState) -> RAGState:
    strategy = state.get("expansion_type") or "step_back"
    emit_rag_step("🔄", "使用扩展查询重新检索...", f"策略: {strategy}")
    results: List[dict] = []
    rerank_applied_any = False
    rerank_enabled_any = False
    rerank_model = None
    rerank_endpoint = None
    rerank_errors = []
    retrieval_mode = None
    candidate_k = None
    leaf_retrieve_level = None
    auto_merge_enabled = None
    auto_merge_applied = False
    auto_merge_threshold = None
    auto_merge_replaced_chunks = 0
    auto_merge_steps = 0

    if strategy in ("hyde", "complex"):
        hypothetical_doc = state.get("hypothetical_doc") or generate_hypothetical_document(state["question"])
        retrieved_hyde = retrieve_documents(hypothetical_doc, top_k=5)
        results.extend(retrieved_hyde.get("docs", []))
        hyde_meta = retrieved_hyde.get("meta", {})
        emit_rag_step(
            "🧱",
            "HyDE 三级检索",
            (
                f"L{hyde_meta.get('leaf_retrieve_level', 3)} 召回，"
                f"候选 {hyde_meta.get('candidate_k', 0)}，"
                f"合并替换 {hyde_meta.get('auto_merge_replaced_chunks', 0)}"
            ),
        )
        rerank_applied_any = rerank_applied_any or bool(hyde_meta.get("rerank_applied"))
        rerank_enabled_any = rerank_enabled_any or bool(hyde_meta.get("rerank_enabled"))
        rerank_model = rerank_model or hyde_meta.get("rerank_model")
        rerank_endpoint = rerank_endpoint or hyde_meta.get("rerank_endpoint")
        if hyde_meta.get("rerank_error"):
            rerank_errors.append(f"hyde:{hyde_meta.get('rerank_error')}")
        retrieval_mode = retrieval_mode or hyde_meta.get("retrieval_mode")
        candidate_k = candidate_k or hyde_meta.get("candidate_k")
        leaf_retrieve_level = leaf_retrieve_level or hyde_meta.get("leaf_retrieve_level")
        auto_merge_enabled = auto_merge_enabled if auto_merge_enabled is not None else hyde_meta.get("auto_merge_enabled")
        auto_merge_applied = auto_merge_applied or bool(hyde_meta.get("auto_merge_applied"))
        auto_merge_threshold = auto_merge_threshold or hyde_meta.get("auto_merge_threshold")
        auto_merge_replaced_chunks += int(hyde_meta.get("auto_merge_replaced_chunks") or 0)
        auto_merge_steps += int(hyde_meta.get("auto_merge_steps") or 0)

    if strategy in ("step_back", "complex"):
        expanded_query = state.get("expanded_query") or state["question"]
        retrieved_stepback = retrieve_documents(expanded_query, top_k=5)
        results.extend(retrieved_stepback.get("docs", []))
        step_meta = retrieved_stepback.get("meta", {})
        emit_rag_step(
            "🧱",
            "Step-back 三级检索",
            (
                f"L{step_meta.get('leaf_retrieve_level', 3)} 召回，"
                f"候选 {step_meta.get('candidate_k', 0)}，"
                f"合并替换 {step_meta.get('auto_merge_replaced_chunks', 0)}"
            ),
        )
        rerank_applied_any = rerank_applied_any or bool(step_meta.get("rerank_applied"))
        rerank_enabled_any = rerank_enabled_any or bool(step_meta.get("rerank_enabled"))
        rerank_model = rerank_model or step_meta.get("rerank_model")
        rerank_endpoint = rerank_endpoint or step_meta.get("rerank_endpoint")
        if step_meta.get("rerank_error"):
            rerank_errors.append(f"step_back:{step_meta.get('rerank_error')}")
        retrieval_mode = retrieval_mode or step_meta.get("retrieval_mode")
        candidate_k = candidate_k or step_meta.get("candidate_k")
        leaf_retrieve_level = leaf_retrieve_level or step_meta.get("leaf_retrieve_level")
        auto_merge_enabled = auto_merge_enabled if auto_merge_enabled is not None else step_meta.get("auto_merge_enabled")
        auto_merge_applied = auto_merge_applied or bool(step_meta.get("auto_merge_applied"))
        auto_merge_threshold = auto_merge_threshold or step_meta.get("auto_merge_threshold")
        auto_merge_replaced_chunks += int(step_meta.get("auto_merge_replaced_chunks") or 0)
        auto_merge_steps += int(step_meta.get("auto_merge_steps") or 0)

    deduped = []
    seen = set()
    for item in results:
        key = (item.get("filename"), item.get("page_number"), item.get("text"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    # 扩展阶段可能合并了多路召回（如 hyde + step_back），
    # 这里统一重排展示名次，避免出现 1,2,3,4,5,4,5 这类重复名次。
    for idx, item in enumerate(deduped, 1):
        item["rrf_rank"] = idx

    context = _format_docs(deduped)
    emit_rag_step("✅", f"扩展检索完成，共 {len(deduped)} 个片段")
    rag_trace = state.get("rag_trace", {}) or {}
    rag_trace.update({
        "expanded_query": state.get("expanded_query") or state["question"],
        "step_back_question": state.get("step_back_question", ""),
        "step_back_answer": state.get("step_back_answer", ""),
        "hypothetical_doc": state.get("hypothetical_doc", ""),
        "expansion_type": strategy,
        "retrieved_chunks": deduped,
        "expanded_retrieved_chunks": deduped,
        "retrieval_stage": "expanded",
        "rerank_enabled": rerank_enabled_any,
        "rerank_applied": rerank_applied_any,
        "rerank_model": rerank_model,
        "rerank_endpoint": rerank_endpoint,
        "rerank_error": "; ".join(rerank_errors) if rerank_errors else None,
        "retrieval_mode": retrieval_mode,
        "candidate_k": candidate_k,
        "leaf_retrieve_level": leaf_retrieve_level,
        "auto_merge_enabled": auto_merge_enabled,
        "auto_merge_applied": auto_merge_applied,
        "auto_merge_threshold": auto_merge_threshold,
        "auto_merge_replaced_chunks": auto_merge_replaced_chunks,
        "auto_merge_steps": auto_merge_steps,
    })
    return {"docs": deduped, "context": context, "rag_trace": rag_trace}


def build_rag_graph():
    graph = StateGraph(RAGState)
    graph.add_node("route_and_build_queries", route_and_build_queries_node)
    graph.add_node("retrieve_initial", retrieve_initial)
    graph.add_node("grade_documents", grade_documents_node)
    graph.add_node("rewrite_question", rewrite_question_node)
    graph.add_node("retrieve_expanded", retrieve_expanded)

    graph.set_entry_point("route_and_build_queries")
    graph.add_edge("route_and_build_queries", "retrieve_initial")
    graph.add_edge("retrieve_initial", "grade_documents")
    graph.add_conditional_edges(
        "grade_documents",
        lambda state: state.get("route"),
        {
            "generate_answer": END,
            "rewrite_question": "rewrite_question",
        },
    )
    graph.add_edge("rewrite_question", "retrieve_expanded")
    graph.add_edge("retrieve_expanded", END)
    return graph.compile()


rag_graph = build_rag_graph()


def run_rag_graph(question: str) -> dict:
    out = rag_graph.invoke({
        "question": question,
        "query": question,
        "context": "",
        "docs": [],
        "route": None,
        "expansion_type": None,
        "expanded_query": None,
        "step_back_question": None,
        "step_back_answer": None,
        "hypothetical_doc": None,
        "rag_trace": None,
        "need_episodic": None,
        "need_semantic": None,
        "episodic_query": None,
        "semantic_query": None,
        "episodic_docs": None,
        "semantic_docs": None,
    })
    if isinstance(out, dict):
        epi = out.get("episodic_docs")
        sem = out.get("semantic_docs")
        if epi is None:
            out["episodic_docs"] = []
        if sem is None:
            out["semantic_docs"] = []
    return out
