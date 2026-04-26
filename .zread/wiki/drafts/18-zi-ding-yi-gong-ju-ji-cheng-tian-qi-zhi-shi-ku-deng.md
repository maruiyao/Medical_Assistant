本页面详细解析了医疗助手项目中自定义工具的集成机制。系统通过 LangChain Agent 框架，将外部 API（如天气服务）与内部知识系统（向量知识库、医疗知识图谱）封装为可调用工具，使智能体能够根据用户意图动态选择并执行相应操作，从而扩展其服务能力。

## 工具定义与核心架构

项目中的所有自定义工具均在 `backend/tools.py` 文件中定义和管理。这些工具被设计为独立的函数，并通过 `@tool` 装饰器注册到 LangChain 生态中，使其能被 Agent 识别和调用。整个工具系统围绕三个核心功能构建：**外部信息获取**（天气）、**非结构化知识检索**（知识库）和**结构化知识查询**（知识图谱）。

工具的执行环境受到严格的状态管理。全局变量用于跟踪每轮对话中的工具调用次数（如 `_KNOWLEDGE_TOOL_CALLS_THIS_TURN`），以防止 Agent 过度或重复调用同一工具，确保对话效率和成本控制。此外，一个关键的 `emit_rag_step` 函数被设计用于跨线程通信，它允许在工具执行过程中（可能在异步或线程池环境中）将检索步骤的实时状态（如“正在检索...”、“实体识别中...”）安全地推送到前端，实现流式思考链路的可视化。

```mermaid
graph TD
    A[LangGraph Agent] -->|调用| B(get_current_weather)
    A -->|调用| C(search_knowledge_base)
    A -->|调用| D(search_knowledge_graph)
    B --> E[高德天气API]
    C --> F[RAG Pipeline<br/>(Milvus + PostgreSQL)]
    D --> G[GraphRAG Pipeline<br/>(Neo4j + BERT NER)]
    C & D --> H[emit_rag_step]
    H --> I[前端流式展示]
```

Sources: [tools.py](backend/tools.py#L1-L50)

## 外部工具集成：天气服务示例

`get_current_weather` 是一个典型的外部工具集成范例。它不依赖于项目的内部数据存储，而是通过 HTTP 请求与第三方 API（高德地图天气服务）交互。该函数首先从 `.env` 环境配置文件中读取必要的 API 密钥 (`AMAP_API_KEY`) 和端点 (`AMAP_WEATHER_API`)，体现了配置与代码分离的原则。

函数内部包含了完整的错误处理逻辑，能够优雅地处理网络超时、请求失败、API 返回错误以及数据解析异常等多种情况，并将这些错误转化为对用户友好的自然语言消息。这确保了即使外部服务不可用，Agent 也能维持稳定的对话体验，而不是直接崩溃。

Sources: [tools.py](backend/tools.py#L37-L98)

## 内部知识库工具：混合检索

`search_knowledge_base` 工具是连接用户问题与内部上传文档的核心桥梁。当 Agent 判断问题需要参考用户私有文档（如病历、药品说明书）时，便会触发此工具。它并非直接访问数据库，而是委托给 `rag_pipeline.py` 中定义的 `run_rag_graph` 函数来执行复杂的检索增强生成（RAG）流程。

该工具支持两种检索模式：**情景记忆**（基于会话历史的个性化检索）和**语义记忆**（基于文档内容的通用检索）。其返回结果是一个格式化的文本块，包含检索到的文档片段及其来源（文件名和页码），供 Agent 在最终生成回答时引用。工具同样受到调用次数限制（每轮一次），以避免不必要的计算开销。

Sources: [tools.py](backend/tools.py#L101-L185)

## 医疗知识图谱工具：结构化查询

`search_knowledge_graph` 工具专门用于查询预构建的医疗领域知识图谱。与知识库工具处理非结构化文本不同，此工具处理的是结构化的实体关系（如“疾病-症状”、“药品-适应症”）。其背后是由 `graphrag_pipeline.py` 驱动的复杂流程，该流程首先通过一个意图识别模型理解用户查询的目标，再利用 BERT NER 模型从查询中抽取关键医疗实体（如疾病名、药品名），最后在 Neo4j 图数据库中执行子图查询。

该工具的输出是一系列 `<提示>` 格式的事实块，其中包含了从图谱中检索到的精确关系和属性。Agent 的职责是将这些离散的事实整合成连贯、专业的自然语言回答，而不是直接将原始提示块返回给用户。这种设计分离了“信息检索”和“答案生成”两个任务，提高了系统的模块化和可维护性。

Sources: [tools.py](backend/tools.py#L210-L223), [graphrag_pipeline.py](backend/graphrag_pipeline.py#L1-L49)

## Agent 中的工具绑定与调用策略

在 `backend/agent.py` 中，通过 `create_agent` 函数将上述三个工具实例 `[get_current_weather, search_knowledge_base, search_knowledge_graph]` 绑定到 LLM Agent 上。更重要的是，一个精心设计的 `system_prompt` 被注入到 Agent 的上下文中，明确指导其何时以及如何使用这些工具。

该系统提示词规定了严格的调用规则：
- **场景区分**：文档相关问题用知识库，实体关系问题用知识图谱。
- **调用限制**：每轮对话中，知识库和知识图谱工具各自最多只能调用一次。
- **响应约束**：一旦获得检索结果，必须立即作答，不得在同一回合内继续调用其他知识类工具。

这些规则确保了 Agent 的行为既高效又可靠，避免了无意义的循环调用或信息过载，为用户提供精准、聚焦的回答。

Sources: [agent.py](backend/agent.py#L220-L250)

## 下一步阅读建议

要深入理解工具背后的检索机制，请继续阅读以下页面：
- [混合检索：稠密向量与 BM25 稀疏向量](12-hun-he-jian-suo-chou-mi-xiang-liang-yu-bm25-xi-shu-xiang-liang)
- [医疗领域检索路由 (Episodic/Semantic)](16-yi-liao-ling-yu-jian-suo-lu-you-episodic-semantic)
- [LangGraph Agent 工作流](17-langgraph-agent-gong-zuo-liu)