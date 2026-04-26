本文档深入解析医疗助手项目中基于 Jina AI Rerank API 的精排（Re-ranking）机制及其内置的多层降级策略。该机制在混合检索（稠密向量 + BM25 稀疏向量）后对候选文档进行相关性重排序，并通过详尽的元数据追踪确保系统在任何异常情况下都能优雅降级，保障服务的高可用性。

## 精排核心逻辑与配置

Jina Rerank 的集成核心位于 `backend/rag_utils.py` 文件中的 `_rerank_documents` 函数。该函数首先检查三个关键环境变量：`RERANK_MODEL`（模型名，如 `jina-reranker-v2-base-multilingual`）、`RERANK_API_KEY`（API 密钥）和 `RERANK_BINDING_HOST`（API 服务地址）。只有当三者均有效时，精排功能才会被激活。

一旦启用，系统会向指定的 Jina Rerank API 端点（通常为 `{host}/v1/rerank`）发送一个包含查询（query）和候选文档列表（documents）的 POST 请求。API 返回每个文档的相关性分数（`relevance_score`），系统据此对文档重新排序，并将分数存储在文档的 `rerank_score` 字段中。整个过程设有 15 秒的超时限制，以防止因外部服务延迟而阻塞主流程。

Sources: [rag_utils.py](backend/rag_utils.py#L67-L123)

## 多层次降级与容错机制

本系统的降级机制设计得极为周全，贯穿于整个检索-精排流程，确保在任何环节失败时都能回退到一个可用的状态。

1.  **精排禁用降级**：如果缺少任一环境变量（模型、密钥或主机），`_rerank_documents` 函数会直接跳过 API 调用，原样返回由混合检索得到的初始结果，并在元数据中标记 `rerank_applied: False`。
2.  **API 调用失败降级**：在调用 Jina API 时，任何网络异常（`requests.RequestException`）、响应解析错误（`json.JSONDecodeError`）或 HTTP 错误状态码（>=400）都会被捕获。此时，系统会记录具体的错误信息（如 `HTTP 401: ...` 或异常堆栈），并立即降级，返回未经精排的原始检索结果。
3.  **空结果降级**：即使 API 调用成功，若返回的结果为空或格式异常，系统同样会视为失败，触发降级并记录 `empty_rerank_results` 错误。
4.  **上游检索失败降级**：在更上游的 `_retrieve_from_kb` 函数中，如果混合检索失败，系统会先尝试降级为仅使用稠密向量检索；若再次失败，则返回空列表。最终，在 `retrieve_documents` 函数中，无论上游是何种失败，都会构造一个包含完整 rerank 元数据（标记为未应用且错误为 `retrieve_failed`）的响应，保证了输出结构的一致性。

Sources: [rag_utils.py](backend/rag_utils.py#L89-L123), [rag_utils.py](backend/rag_utils.py#L270-L355)

## 元数据追踪与监控

为了便于调试、监控和后续分析，系统为每一次检索-精排操作生成了详尽的元数据（meta data）。这些元数据不仅包含精排本身的状态（是否启用、是否应用、使用的模型和端点），还记录了潜在的错误信息。

在 `backend/schemas.py` 中定义了 `DocumentResponse` 模型，明确包含了 `rerank_score`, `rerank_enabled`, `rerank_applied`, `rerank_model`, `rerank_endpoint`, 和 `rerank_error` 等字段。这意味着前端或日志系统可以清晰地看到本次请求是否使用了精排、使用了哪个模型、以及是否遇到了问题。这种透明化的设计对于维护一个复杂的 RAG 系统至关重要。

| 元数据字段 | 说明 |
| :--- | :--- |
| `rerank_enabled` | 基于环境变量判断精排功能是否已配置启用。 |
| `rerank_applied` | 标记本次请求是否实际执行了精排 API 调用。 |
| `rerank_model` | 使用的 Jina Rerank 模型名称。 |
| `rerank_endpoint` | 实际调用的 API 端点 URL。 |
| `rerank_error` | 如果精排过程中发生任何错误，此处会记录具体原因；否则为 `None`。 |

Sources: [schemas.py](backend/schemas.py#L105-L110), [rag_utils.py](backend/rag_utils.py#L71-L75)

## 在 LangGraph Pipeline 中的整合

精排作为检索环节的最后一步，被无缝集成到 LangGraph 工作流中。在 `backend/rag_pipeline.py` 中，`retrieve_and_expand` 节点负责执行检索，并将包含精排元数据的完整结果存入图状态（graph state）。后续的节点（如文档分级、答案生成）可以直接利用已经过精排（或已优雅降级）的文档列表。

此外，系统还支持对不同查询扩展策略（如 Step-Back 和 HyDE）分别进行检索和精排。`rag_pipeline.py` 中的代码会聚合所有策略的精排元数据，确保最终报告的元数据能反映整个检索过程的真实状态，例如合并多个可能的错误信息。