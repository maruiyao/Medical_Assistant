在本项目中，`.env` 文件是整个应用的**环境变量中枢**，它集中管理了从大模型 API 密钥、数据库连接到向量库地址等所有外部服务的配置。正确配置此文件是项目成功运行的前提。本文将详细解析每个配置项的作用、默认值及其在代码中的引用位置。

## 核心配置项概览

下表总结了 `.env` 文件中定义的所有关键配置项，包括其用途、示例值和是否为必填项。

| 配置项 | 用途说明 | 示例值 | 必填 |
| :--- | :--- | :--- | :--- |
| `ARK_API_KEY` | 字节跳动 ARK 平台的大模型 API 密钥 | `7a...` | 是 |
| `MODEL` | 主对话模型，用于核心推理 | `doubao-seed-2-0-pro-260215` | 是 |
| `GRADE_MODEL` | 轻量级模型，用于意图分类等辅助任务 | `doubao-seed-2-0-lite-260215` | 是 |
| `FAST_MODEL` | 超快响应模型，用于流式回复等场景 | `doubao-seed-1-6-flash-250828` | 是 |
| `BASE_URL` | ARK 平台 API 的基础 URL | `https://ark.cn-beijing.volces.com/api/v3` | 是 |
| `EMBEDDING_MODEL` | 本地嵌入模型名称 | `BAAI/bge-m3` | 否 (有默认) |
| `EMBEDDING_DEVICE` | 嵌入模型运行设备 | `cpu` 或 `cuda` | 否 (有默认) |
| `DENSE_EMBEDDING_DIM` | 稠密向量的维度 | `1024` | 否 (有默认) |
| `RERANK_MODEL` | Jina Rerank 模型名称 | `jina-reranker-v3` | 否 (可降级) |
| `RERANK_BINDING_HOST` | Jina Rerank API 地址 | `https://api.jina.ai/v1/rerank` | 否 (可降级) |
| `RERANK_API_KEY` | Jina Rerank API 密钥 | `jina_...` | 否 (可降级) |
| `MILVUS_HOST` | Milvus 向量数据库主机 | `127.0.0.1` | 是 |
| `MILVUS_PORT` | Milvus 向量数据库端口 | `19530` | 是 |
| `BM25_STATE_PATH` | BM25 稀疏统计信息的持久化路径 | `./data/bm25_state.json` | 否 (有默认) |
| `JWT_SECRET_KEY` | JWT 令牌签名密钥 | `your-super-secret-jwt-key` | 是 |
| `JWT_ALGORITHM` | JWT 令牌签名算法 | `HS256` | 否 (有默认) |
| `DATABASE_URL` | PostgreSQL 数据库连接字符串 | `postgresql+psycopg2://...` | 否 (有默认) |
| `HOST` | FastAPI 应用监听的主机 | `0.0.0.0` | 否 (有默认) |
| `PORT` | FastAPI 应用监听的端口 | `8000` | 否 (有默认) |

Sources: [.env.example](.env.example#L1-L22)

## 配置加载机制与架构

项目的配置加载遵循 **“约定优于配置”** 的原则，并采用分层覆盖策略。首先，系统会从项目根目录下的 `.env` 文件加载环境变量。这些变量随后会被 Python 的 `os.getenv()` 函数在各个模块中读取。值得注意的是，许多配置项都提供了安全的默认值，这使得开发者可以在不修改 `.env` 文件的情况下快速启动项目进行本地测试。

整个配置体系的架构如下图所示：

```mermaid
graph LR
    A[.env 文件] -->|load_dotenv()| B(FastAPI App)
    A --> C(Backend Modules)
    A --> D(Medical Modules)
    B -->|os.getenv('HOST', '0.0.0.0')| E[服务器启动]
    C -->|os.getenv('ARK_API_KEY')| F[大模型调用]
    C -->|os.getenv('DATABASE_URL', default)| G[数据库连接]
    C -->|os.getenv('JWT_SECRET_KEY', '...')| H[用户认证]
    D -->|os.getenv('MILVUS_HOST')| I[Milvus 向量库]
```

Sources: [backend/app.py](backend/app.py#L56-L58), [backend/auth.py](backend/auth.py#L3-L4), [backend/database.py](backend/database.py#L6-L9)

## 关键配置项深度解析

### 大模型与 API 配置
`ARK_API_KEY`, `MODEL`, `GRADE_MODEL`, `FAST_MODEL`, 和 `BASE_URL` 是驱动整个智能体的核心。它们被 `backend/intent.py` 等模块直接引用，用于初始化大模型客户端。错误的 API 密钥或模型名称将导致所有依赖大模型的功能（如对话、意图识别）失效。

### 数据库与认证配置
虽然 `.env.example` 中未显式列出 `DATABASE_URL`、`JWT_SECRET_KEY` 和 `JWT_ALGORITHM`，但代码中明确使用了 `os.getenv` 来读取它们。`JWT_SECRET_KEY` **必须**在生产环境中修改，否则会带来严重的安全风险。数据库 URL 默认指向本地的 PostgreSQL 实例，可以根据实际部署情况修改。

### 向量库与检索配置
`MILVUS_HOST` 和 `MILVUS_PORT` 定义了向量数据库的连接信息，是 RAG 功能的基础。`EMBEDDING_MODEL` 系列配置则决定了文本如何被转化为向量。Rerank 相关的配置是可选的；如果未提供，系统会自动降级到仅使用混合检索的结果，而不会进行精排。

Sources: [backend/intent.py](backend/intent.py#L6-L8), [backend/auth.py](backend/auth.py#L3-L4), [backend/database.py](backend/database.py#L6-L9)

## 下一步

完成 `.env` 文件的配置后，您已经为项目打下了坚实的基础。接下来，您可以深入探索系统的具体功能：
- 了解用户如何与系统交互，请参阅 [用户注册、登录与权限管理](6-yong-hu-zhu-ce-deng-lu-yu-quan-xian-guan-li)。
- 学习如何将您的医疗文档注入知识库，请参阅 [文档上传与知识库管理](7-wen-dang-shang-chuan-yu-zhi-shi-ku-guan-li)。
- 为了理解 `.env` 中配置的检索参数如何在底层工作，请参阅 [混合检索：稠密向量与 BM25 稀疏向量](12-hun-he-jian-suo-chou-mi-xiang-liang-yu-bm25-xi-shu-xiang-liang)。