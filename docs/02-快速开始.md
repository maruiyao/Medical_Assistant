本指南将带你完成 Medical-Assistant 项目的本地部署与首次运行。通过几个简单步骤，你就能启动一个具备完整医疗问答、文档知识库和智能体能力的个人健康助理。整个过程涵盖了环境准备、依赖安装、服务配置和应用启动。

## 项目架构概览

Medical-Assistant 采用前后端分离架构，后端基于 FastAPI 构建 RESTful API 和流式聊天接口，前端是一个轻量级的 Vue 3 单页应用。核心数据存储依赖 PostgreSQL（关系型数据）和 Milvus（向量数据），并使用 Redis 进行缓存加速。

```mermaid
graph LR
    A[前端 Vue 3] -->|HTTP/SSE| B(FastAPI 后端)
    B --> C[(PostgreSQL<br/>用户/会话/父文档)]
    B --> D[(Redis<br/>会话缓存)]
    B --> E[Milvus<br/>向量检索]
    B --> F[LLM API<br/>(如 Ark/DeepSeek)]
    B --> G[Rerank API<br/>(可选)]
    subgraph "Docker Compose"
        C
        D
        E
    end
```

Sources: [README.md](README.md#L45-L100), [backend/app.py](backend/app.py#L1-L58)

## 本地部署步骤

### 第一步：克隆代码与准备环境

确保你的系统已安装 Python 3.12+、Git 和 Docker。推荐使用 `uv` 作为包管理器以获得最佳体验。

```bash
git clone <your-repo-url>
cd Medical-Assistant
```

### 第二步：安装项目依赖

项目根目录下的 `pyproject.toml` 定义了所有依赖。你可以选择 `uv` 或传统的 `pip` 方式进行安装。

| 安装方式 | 命令 | 说明 |
|---------|------|------|
| **推荐 (uv)** | `uv sync` | 速度更快，依赖解析更优 |
| **传统 (pip)** | `pip install -e .` | 需先创建并激活虚拟环境 |

Sources: [README.md](README.md#L16-L30)

### 第三步：配置环境变量

从 `.env.example` 复制一份 `.env` 文件到项目根目录，并根据你的实际情况填写各项配置。最关键的配置是 LLM 的 API 密钥和模型名称。

```bash
cp .env.example .env
# 然后编辑 .env 文件
```

核心配置项包括：
- **模型接入**: `ARK_API_KEY`, `MODEL`, `BASE_URL`
- **向量数据库**: `MILVUS_HOST`, `MILVUS_PORT`
- **关系数据库**: `DATABASE_URL`
- **认证安全**: `JWT_SECRET_KEY`, `ADMIN_INVITE_CODE`

Sources: [README.md](README.md#L32-L70)

### 第四步：启动依赖服务

项目依赖的 PostgreSQL、Redis 和 Milvus 向量库都通过 `docker-compose.yml` 进行管理。在项目根目录执行以下命令即可一键启动所有依赖。

```bash
docker compose up -d
```

启动后，你可以通过 `docker compose ps` 查看各服务状态。关键服务端口如下：
- **PostgreSQL**: 5432
- **Redis**: 6379
- **Milvus**: 19530
- **Attu (Milvus UI)**: 8080

Sources: [README.md](README.md#L72-L87)

### 第五步：启动应用并访问

所有依赖就绪后，即可启动 FastAPI 后端服务。服务会自动挂载 `frontend` 目录，提供完整的 Web 界面。

```bash
# 使用 uv (推荐)
uv run uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload

# 或使用 python
python backend/app.py
```

启动成功后，打开浏览器访问以下地址：
- **主应用界面**: `http://127.0.0.1:8000/`
- **API 交互文档**: `http://127.0.0.1:8000/docs`

Sources: [backend/app.py](backend/app.py#L50-L58), [README.md](README.md#L89-L95)

## 下一步学习建议

恭喜你已完成项目的快速启动！为了更深入地理解和使用 Medical-Assistant，建议按以下顺序继续探索：

1.  **[环境准备与依赖安装](3-huan-jing-zhun-bei-yu-yi-lai-an-zhuang)**: 详细了解各种依赖的版本要求和替代方案。
2.  **[Docker Compose 服务部署](4-docker-compose-fu-wu-bu-shu)**: 学习如何自定义和管理 Docker 服务。
3.  **[配置文件 (.env) 详解](5-pei-zhi-wen-jian-env-xiang-jie)**: 掌握所有配置项的详细含义和调优方法。
4.  **[用户注册、登录与权限管理](6-yong-hu-zhu-ce-deng-lu-yu-quan-xian-guan-li)**: 体验完整的用户认证和权限控制流程。