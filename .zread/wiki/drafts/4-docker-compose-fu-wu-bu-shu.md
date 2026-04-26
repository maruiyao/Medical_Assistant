本指南将详细介绍如何使用 `docker-compose.yml` 文件一键部署 Medical-Assistant 项目所依赖的全部后端基础设施服务，包括 PostgreSQL、Redis、Milvus 及其配套组件。这对于希望快速搭建开发或测试环境的初学者至关重要。

## 核心服务架构概览
Medical-Assistant 的数据层和缓存层由多个相互协作的容器化服务构成。通过 Docker Compose，这些服务被定义在一个统一的编排文件中，确保了环境的一致性和部署的便捷性。下图展示了各服务间的依赖关系和核心作用。

```mermaid
graph TD
    A[Application Backend] -->|DB Connection| B((PostgreSQL))
    A -->|Cache/Session| C((Redis))
    A -->|Vector Search| D((Milvus Standalone))
    D -->|Metadata Store| E((etcd))
    D -->|Object Storage| F((MinIO))
    G[Attu UI] -->|Admin API| D

    subgraph "Docker Compose Services"
        B
        C
        D
        E
        F
        G
    end
```

Sources: [docker-compose.yml](docker-compose.yml#L1-L108)

## 服务配置详解
`docker-compose.yml` 文件定义了六个核心服务，每个服务都配置了必要的环境变量、端口映射、数据卷和健康检查。下表详细说明了每个服务的角色和关键配置项。

| 服务名称 | 镜像 | 主要作用 | 关键端口 | 环境变量/配置 | 数据卷 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **postgres** | `postgres:15` | 主数据库，存储用户信息、会话历史、父级文档块等结构化数据。 | `5432:5432` | `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | `./volumes/postgres` |
| **redis** | `redis:7-alpine` | 缓存服务，用于存储会话状态、临时计算结果和速率限制。 | `6379:6379` | 启用 `appendonly` 持久化 | `./volumes/redis` |
| **etcd** | `quay.io/coreos/etcd:v3.5.18` | Milvus 的元数据存储，管理集群状态和配置。 | (内部) | 自动压缩、配额设置 | `./volumes/etcd` |
| **minio** | `minio/minio:RELEASE...` | Milvus 的对象存储，用于持久化向量索引文件。 | `9000:9000` (API), `9001:9001` (Console) | `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | `./volumes/minio` |
| **standalone** | `milvusdb/milvus:v2.5.14` | 向量数据库核心服务，执行高效的稠密和稀疏向量检索。 | `19530:19530` (gRPC), `9091:9091` (Health) | 连接 `etcd` 和 `minio` | `./volumes/milvus` |
| **attu** | `zilliz/attu:v2.5.11` | Milvus 的官方 Web UI，方便开发者管理和监控向量库。 | `8080:3000` | `MILVUS_URL=standalone:19530` | N/A |

Sources: [docker-compose.yml](docker-compose.yml#L5-L108)

## 应用与服务的连接方式
后端应用（位于 `backend/` 目录）通过环境变量来发现和连接这些 Docker 服务。在开发环境中，`.env` 文件中的配置需要与 `docker-compose.yml` 中暴露的端口和服务名相匹配。

- **PostgreSQL**: 应用通过 `DATABASE_URL` 环境变量连接，默认值为 `postgresql+psycopg2://postgres:postgres@localhost:5432/langchain_app`，这与 Compose 文件中的配置完全一致。
- **Redis**: 应用通过 `REDIS_URL` 环境变量连接，默认值为 `redis://localhost:6379/0`。
- **Milvus**: 应用通过 `MILVUS_HOST` 和 `MILVUS_PORT` 环境变量连接，默认值分别为 `127.0.0.1` 和 `19530`。

这种设计使得应用代码无需硬编码服务地址，只需读取环境变量即可灵活适应不同的部署环境（本地 Docker、远程服务器等）。

Sources: [backend/database.py](backend/database.py#L5-L10), [backend/cache.py](backend/cache.py#L8), [backend/milvus_client.py](backend/milvus_client.py#L10-L11)

## 部署与验证步骤
完成服务部署后，建议按以下步骤进行验证，确保所有服务均正常运行：

1.  **启动服务**: 在项目根目录下执行 `docker-compose up -d` 启动所有服务。
2.  **检查状态**: 使用 `docker-compose ps` 查看所有容器是否处于 `Up` 状态。
3.  **验证数据库**: 尝试使用 `psql` 或其他 PostgreSQL 客户端连接 `localhost:5432`。
4.  **验证 Redis**: 使用 `redis-cli -p 6379 ping` 命令，应收到 `PONG` 回复。
5.  **验证 Milvus**: 访问 `http://localhost:9091/healthz`，应返回 `{"status":"ok"}`。
6.  **访问 Attu**: 打开浏览器访问 `http://localhost:8080`，登录后应能看到 Milvus 的管理界面。

## 下一步学习建议
成功部署基础设施后，您需要为应用本身配置必要的环境变量，并了解如何启动后端和前端服务。

- **配置环境变量**: 请参考 `[配置文件 (.env) 详解](5-pei-zhi-wen-jian-env-xiang-jie)` 页面，根据 `.env.example` 创建您自己的 `.env` 文件。
- **启动应用**: 完成配置后，请回到 `[快速开始](2-kuai-su-kai-shi)` 页面，按照指引启动后端 (`main.py`) 和前端 (`frontend/index.html`)，体验完整的应用功能。