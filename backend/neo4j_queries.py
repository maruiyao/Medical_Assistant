"""
从 medical/webui 的 Neo4j 查库与分支逻辑抽出；工具模式只拼接 <提示> 事实块，不拼「用户问题」和二次指令。
疾病节点属性名仅允许白名单内字段，防注入；实体名称使用 $ 参数化。
"""
from __future__ import annotations

import logging
import os
import random
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

Emit = Optional[Callable[[str, str, str], None]]


def _neo4j_err_hint(ex: BaseException) -> str:
    s = str(ex)
    if "DatabaseNotFound" in s or "Database.DatabaseNotFound" in s:
        return (
            " Neo4j 数据库名配置不匹配。请检查 .env 里的 `NEO4J_DB` 是否与实例实际库名一致；"
            "有些 Aura 实例默认库名是 `neo4j`，也有些会使用实例 ID。删掉该变量时，代码会尝试回退到服务默认库。"
        )
    if "Security.Unauthorized" in s or "authentication failure" in s.lower():
        return (
            " Neo4j 账号或密码认证失败。请检查 `.env` 里的 `NEO4J_USER` / `NEO4J_USERNAME`"
            " 与 `NEO4J_PASSWORD` 是否仍然有效。"
        )
    if (
        "Cannot open connection" in s
        or "Cannot connect to" in s
        or "nodename nor servname provided" in s
    ):
        return (
            " Neo4j 主机当前不可达。请检查 `NEO4J_URI` 是否填写正确，"
            "并确认本机网络/DNS 能访问 Aura 域名。"
        )
    if "Connection has been closed" in s:
        return " 底层连接已被服务端关闭，通常是因为前面的认证失败或 TLS/URI 配置不匹配。"
    return ""


def _format_neo4j_connect_error(ex: BaseException) -> str:
    return f"知识图谱连接失败: {type(ex).__name__}: {ex!s}.{_neo4j_err_hint(ex)}"

# 与 add_shuxing_prompt 中使用的属性列一致
_SHUXING_WHITELIST = {
    "疾病简介",
    "疾病病因",
    "预防措施",
    "治疗周期",
    "治愈概率",
    "疾病易感人群",
}


def _env_clean(value: Optional[str]) -> str:
    """去掉首尾空白；若整段被引号包住则去掉（防 .env 里误写）。"""
    if value is None:
        return ""
    s = str(value).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def _normalize_neo4j_uri_for_py2neo(uri: str) -> str:
    """
    py2neo 2021.x 只认 bolt* 方案，不认 neo4j*。
    - 本地单机：常用 bolt://host:7687
    - Neo4j Aura 云：控制台给出 neo4j+s://xxx.databases.neo4j.io → 映射为 bolt+s://（加密）
    """
    u = (uri or "").strip()
    if u.startswith("neo4j://"):
        return "bolt://" + u[len("neo4j://") :]
    if u.startswith("neo4j+s://"):
        return "bolt+s://" + u[len("neo4j+s://") :]
    if u.startswith("neo4j+ssc://"):
        return "bolt+ssc://" + u[len("neo4j+ssc://") :]
    return u


def _sync_neo4j_uri_in_os_env_before_py2neo() -> None:
    """
    py2neo 在 import 时执行 NEO4J_URI = getenv("NEO4J_URI")（只读环境一次）。
    在任意代码 import py2neo 之前，应先把 OS 中的 neo4j* 改为 bolt*。
    """
    raw = _env_clean(os.getenv("NEO4J_URI"))
    if not raw:
        return
    fixed = _normalize_neo4j_uri_for_py2neo(raw)
    if fixed != raw:
        os.environ["NEO4J_URI"] = fixed


_sync_neo4j_uri_in_os_env_before_py2neo()


@lru_cache(maxsize=1)
def get_neo4j_graph():
    raw_uri = _env_clean(os.getenv("NEO4J_URI"))
    user = (
        _env_clean(os.getenv("NEO4J_USER"))
        or _env_clean(os.getenv("NEO4J_USERNAME"))
        or "neo4j"
    )
    password = _env_clean(os.getenv("NEO4J_PASSWORD"))
    db = _env_clean(os.getenv("NEO4J_DB")) or _env_clean(os.getenv("NEO4J_DATABASE"))
    if not raw_uri or not password:
        return None
    uri = _normalize_neo4j_uri_for_py2neo(raw_uri)
    if uri != raw_uri:
        os.environ["NEO4J_URI"] = uri

    # 须先规范化再 import：否则 py2neo/__init__.py 里 NEO4J_URI = getenv(...) 会锁死 neo4j* 方案
    try:
        import py2neo
    except ImportError:
        return None
    # 若其它模块已提前 import 过 py2neo，覆盖其模块级缓存
    py2neo.NEO4J_URI = os.environ.get("NEO4J_URI")
    try:
        if db:
            return py2neo.Graph(uri, name=db, auth=(user, password))
        return py2neo.Graph(uri, auth=(user, password))
    except Exception as ex:
        logger.warning("get_neo4j_graph direct Graph failed: %s", ex)
        try:
            gs = py2neo.GraphService(uri, user=user, password=password)
            graph_name = db or gs.connector.default_graph_name()
            return gs[graph_name]
        except Exception as ex2:
            logger.warning("get_neo4j_graph GraphService fallback failed: %s", ex2)
            raise RuntimeError(_format_neo4j_connect_error(ex2)) from ex2


def add_shuxing_prompt(
    client: Any, entity: str, shuxing: str, emit: Emit = None
) -> str:
    if shuxing not in _SHUXING_WHITELIST:
        return ""
    add_prompt = ""
    try:
        # 用 a[$k] 访问中文等属性，避免 a.疾病简介 在部分 Neo4j 版本解析失败
        cypher = "MATCH (a:疾病 {名称: $e}) RETURN a[$k] AS v"
        if emit:
            emit("📊", f"图谱属性: {shuxing}", entity[:40])
        row = client.run(cypher, e=entity, k=shuxing).data()
        add_prompt += "<提示>"
        add_prompt += f"用户对{entity}可能有查询{shuxing}需求，知识库内容如下："
        if row and list(row[0].values()):
            v = list(row[0].values())[0]
            add_prompt += "" if v is None else str(v)
        else:
            add_prompt += "图谱中无信息，查找失败。"
        add_prompt += "</提示>"
    except Exception as ex:
        logger.warning("add_shuxing_prompt failed: %s", ex, exc_info=True)
        return (
            f"<提示>图谱属性「{shuxing}」查询失败: {type(ex).__name__}。"
            f"请确认库中存在 :疾病{{名称:与NER一致}} 且节点上有属性「{shuxing}」。"
            f"{_neo4j_err_hint(ex)}</提示>"
        )
    return add_prompt


_REL_WHITELIST = {
    ("疾病使用药品", "药品"),
    ("疾病宜吃食物", "食物"),
    ("疾病忌吃食物", "食物"),
    ("疾病所需检查", "检查项目"),
    ("疾病所属科目", "科目"),
    ("疾病的症状", "疾病症状"),
    ("治疗的方法", "治疗方法"),
    ("疾病并发疾病", "疾病"),
}


def add_lianxi_prompt(
    client: Any, entity: str, lianxi: str, target: str, emit: Emit = None
) -> str:
    if (lianxi, target) not in _REL_WHITELIST:
        return ""
    add_prompt = ""
    try:
        cypher = (
            f"MATCH (a:疾病 {{名称: $e}})-[r:`{lianxi}`]->(b:`{target}`) RETURN b.名称 AS n"
        )
        if emit:
            emit("📊", f"图谱关系: {lianxi}", entity[:40])
        rows = client.run(cypher, e=entity).data()
        res = [list(d.values())[0] for d in rows if d]
        add_prompt += "<提示>"
        add_prompt += f"用户对{entity}可能有查询{lianxi}需求，知识库内容如下："
        if res:
            add_prompt += "、".join(res)
        else:
            add_prompt += "图谱中无信息，查找失败。"
        add_prompt += "</提示>"
    except Exception as ex:
        logger.warning("add_lianxi_prompt failed: %s", ex, exc_info=True)
        return (
            f"<提示>图谱关系「{lianxi}」→「{target}」查询失败: {type(ex).__name__}。"
            f"请确认关系类型、终点标签与图建模一致，且疾病名称与 NER 一致。"
            f"{_neo4j_err_hint(ex)}</提示>"
        )
    return add_prompt


def _producer_prompt(client: Any, drug: str) -> str:
    add_prompt = ""
    try:
        cypher = "MATCH (a:药品商)-[r:生产]->(b:药品 {名称: $d}) RETURN a.名称 AS n"
        row = client.run(cypher, d=drug).data()
        add_prompt += "<提示>"
        add_prompt += f"用户对{drug}可能有查询药品生产商的需求，知识图谱内容如下："
        if row and list(row[0].values()):
            add_prompt += "".join(str(x) for x in row[0].values() if x is not None)
        else:
            add_prompt += "图谱中无信息，查找失败"
        add_prompt += "</提示>"
    except Exception as ex:
        logger.warning("_producer_prompt failed: %s", ex, exc_info=True)
        return (
            f"<提示>生产商查询失败: {type(ex).__name__}。请确认存在 :药品{{名称:…}} 与 生产 关系。"
            f"{_neo4j_err_hint(ex)}</提示>"
        )
    return add_prompt


def build_kg_tool_context(
    _query: str,
    intent_text: str,
    entities: Dict[str, str],
    client: Any,
    emit: Emit = None,
) -> Tuple[str, List[str], Dict[str, str]]:
    """
    仅返还可作为 Agent 工具上下文的 <提示> 块（方式 A，不在此调用 chat 生成最终答案）。
    分支与 medical/webui.generate_prompt 中查图谱部分对齐。
    """
    yitu: List[str] = []
    parts: List[str] = []
    e = dict(entities)
    response = intent_text

    if "疾病症状" in e and "疾病" not in e:
        try:
            cypher = "MATCH (a:疾病)-[r:疾病的症状]->(b:疾病症状 {名称: $s}) RETURN a.名称 AS n"
            res = [list(d.values())[0] for d in client.run(cypher, s=e["疾病症状"]).data()]
            if res:
                e["疾病"] = random.choice(res)
                all_en = "、".join(res)
                parts.append(
                    f"<提示>用户有{e['疾病症状']}的情况，知识库推测其可能是得了{all_en}。"
                    f"请注意这只是一个推测，你需要明确告知用户这一点。</提示>"
                )
        except Exception:
            pass

    # 与 webui 一致：pre_len 为「症状→疾病」写完之后的长度；若后续分支一字未增，则加异常提示
    plen = len("".join(parts))

    if "简介" in response and "疾病" in e:
        parts.append(add_shuxing_prompt(client, e["疾病"], "疾病简介", emit))
        yitu.append("查询疾病简介")
    if "病因" in response and "疾病" in e:
        parts.append(add_shuxing_prompt(client, e["疾病"], "疾病病因", emit))
        yitu.append("查询疾病病因")
    if "预防" in response and "疾病" in e:
        parts.append(add_shuxing_prompt(client, e["疾病"], "预防措施", emit))
        yitu.append("查询疾病预防措施")
    if "治疗周期" in response and "疾病" in e:
        parts.append(add_shuxing_prompt(client, e["疾病"], "治疗周期", emit))
        yitu.append("查询治疗周期")
    if "治愈概率" in response and "疾病" in e:
        parts.append(add_shuxing_prompt(client, e["疾病"], "治愈概率", emit))
        yitu.append("查询治愈概率")
    if "易感人群" in response and "疾病" in e:
        parts.append(add_shuxing_prompt(client, e["疾病"], "疾病易感人群", emit))
        yitu.append("查询疾病易感人群")
    if "药品" in response and "疾病" in e:
        parts.append(
            add_lianxi_prompt(client, e["疾病"], "疾病使用药品", "药品", emit)
        )
        yitu.append("查询疾病使用药品")
    if "宜吃食物" in response and "疾病" in e:
        parts.append(
            add_lianxi_prompt(client, e["疾病"], "疾病宜吃食物", "食物", emit)
        )
        yitu.append("查询疾病宜吃食物")
    if "忌吃食物" in response and "疾病" in e:
        parts.append(
            add_lianxi_prompt(client, e["疾病"], "疾病忌吃食物", "食物", emit)
        )
        yitu.append("查询疾病忌吃食物")
    if "检查项目" in response and "疾病" in e:
        parts.append(
            add_lianxi_prompt(client, e["疾病"], "疾病所需检查", "检查项目", emit)
        )
        yitu.append("查询疾病所需检查")
    if "查询疾病所属科目" in response and "疾病" in e:
        parts.append(
            add_lianxi_prompt(client, e["疾病"], "疾病所属科目", "科目", emit)
        )
        yitu.append("查询疾病所属科目")
    if "症状" in response and "疾病" in e:
        parts.append(
            add_lianxi_prompt(client, e["疾病"], "疾病的症状", "疾病症状", emit)
        )
        yitu.append("查询疾病的症状")
    if "治疗" in response and "疾病" in e:
        parts.append(
            add_lianxi_prompt(client, e["疾病"], "治疗的方法", "治疗方法", emit)
        )
        yitu.append("查询治疗的方法")
    if "并发" in response and "疾病" in e:
        parts.append(
            add_lianxi_prompt(client, e["疾病"], "疾病并发疾病", "疾病", emit)
        )
        yitu.append("查询疾病并发疾病")
    if "生产商" in response and "药品" in e:
        parts.append(_producer_prompt(client, e["药品"]))
        yitu.append("查询药物生产商")

    blocks_core = "".join(parts)
    # 勿用 len(s_final)==plen：子查询若异常返回空串，append 后 join 长度仍不变，会误判
    if not blocks_core.strip():
        parts.append(
            "<提示>提示：未命中任何图谱查询分支（常见原因：①意图原文里缺少与问题对应的关键字，如「症状」「简介」「药品」「检查项目」等；"
            "②NER 未识别出「疾病」实体，或图中疾病名称与识别结果不一致；③仅有「疾病症状」但未在图中关联到疾病）。"
            "请据实说明无法从当前知识图谱回答，或请用户换用图中存在的标准病名/药名重试。</提示>"
        )
    blocks = "".join(parts)

    header = (
        "## 知识图谱检索结果（仅含 <提示> 事实，供你结合用户问题组织回答；勿编造提示外信息）\n"
        f"**意图识别原文（节选）:** {intent_text[:500]}{'...' if len(intent_text) > 500 else ''}\n"
        f"**NER 实体:** {e}\n\n"
    )
    return header + blocks, yitu, e
