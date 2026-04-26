本文档将指导您完成 Medical-Assistant 项目的本地开发环境搭建，包括 Python 版本管理、依赖安装以及必要的环境变量配置。这是进行后续开发和调试的基础步骤。

## Python 版本要求

项目明确要求使用 **Python 3.12 或更高版本**。这一要求在项目的 `pyproject.toml` 文件中被严格定义，确保了所有依赖库的兼容性。您可以通过多种方式来管理 Python 版本，例如 `pyenv`、`conda` 或系统自带的包管理器。请务必在安装依赖前确认您的 Python 版本符合要求。
Sources: [pyproject.toml](pyproject.toml#L6)

## 依赖管理工具与安装

项目推荐使用现代的 Python 包管理工具 **`uv`** 来处理依赖，它比传统的 `pip` 更快、更可靠。当然，`pip` 也是受支持的备选方案。

### 使用 `uv` 安装（推荐）

首先，您需要安装 `uv` 工具本身。可以参考其官方文档进行安装。安装完成后，在项目根目录下执行以下命令即可安装所有必需的依赖：

```bash
uv pip install -e .
```

此命令会读取 `pyproject.toml` 文件中的 `[project.dependencies]` 列表，并以可编辑模式 (`-e`) 安装项目及其依赖。

### 使用 `pip` 安装

如果您选择使用 `pip`，同样在项目根目录下执行：

```bash
pip install -e .
```

### 核心依赖概览

项目的核心依赖涵盖了 Web 框架、向量数据库客户端、语言模型集成、文档处理等多个方面。下表列出了一些关键依赖及其用途：

| 依赖类别 | 关键库 | 用途 |
| :--- | :--- | :--- |
| **Web 框架** | `fastapi`, `uvicorn` | 构建高性能异步 API 服务 |
| **向量数据库** | `pymilvus` | 连接和操作 Milvus 向量数据库 |
| **LLM 集成** | `langchain`, `langgraph` | 构建检索增强生成 (RAG) 和智能体 (Agent) 工作流 |
| **嵌入模型** | `sentence-transformers`, `langchain-huggingface` | 生成文本的稠密向量表示 |
| **数据库** | `sqlalchemy`, `psycopg2-binary` | 连接和操作 PostgreSQL 数据库 |
| **缓存** | `redis` | 实现会话和数据的高速缓存 |
| **文档处理** | `pypdf`, `docx2txt`, `unstructured` | 解析 PDF、Word 等多种格式的文档 |

Sources: [pyproject.toml](pyproject.toml#L8-L40)

## 环境变量配置

项目通过 `.env` 文件来管理各种敏感信息和可配置参数。您需要根据提供的模板创建自己的 `.env` 文件。

1.  在项目根目录下，复制 `.env.example` 文件并重命名为 `.env`：
    
```bash
    cp .env.example .env
    ```

2.  使用文本编辑器打开 `.env` 文件，并根据您的实际情况填写各项配置。主要配置项包括：
    *   **API Keys**: 如 `ARK_API_KEY` 用于访问大语言模型服务。
    *   **模型名称**: 如 `MODEL`, `EMBEDDING_MODEL` 指定要使用的具体模型。
    *   **服务地址**: 如 `MILVUS_HOST` 和 `MILVUS_PORT` 指向您的 Milvus 服务实例。
    *   **设备**: `EMBEDDING_DEVICE` 可设置为 `cpu` 或 `cuda`，取决于您的硬件。

一个正确配置的 `.env` 文件是项目能够正常连接外部服务和模型的关键。
Sources: [.env.example](.env.example#L1-L22)

## 项目结构概览

了解项目的目录结构有助于您快速定位代码。以下是核心目录的简要说明：

```
Medical-Assistant/
├── backend/          # 后端核心逻辑，包含 FastAPI 应用、RAG 流水线、数据库交互等
├── frontend/         # 前端静态文件（HTML, CSS, JS）
├── medical/          # 医疗领域特定的模块，如知识图谱构建、NER 等
├── pyproject.toml    # 项目元数据和依赖声明
├── .env.example      # 环境变量配置模板
└── main.py           # 应用入口点（部分功能）
```

Sources: [README.md](README.md)

## 下一步

完成环境准备后，您可以继续阅读以下文档以深入项目：
*   学习如何使用 Docker Compose 快速部署全套服务：[Docker Compose 服务部署](4-docker-compose-fu-wu-bu-shu)
*   详细了解 `.env` 文件中每个配置项的具体作用：[配置文件 (.env) 详解](5-pei-zhi-wen-jian-env-xiang-jie)