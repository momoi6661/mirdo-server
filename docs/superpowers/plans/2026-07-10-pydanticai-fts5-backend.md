# PydanticAI、Pydantic Graph 与 FTS5 后端重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除 LangChain/Chroma/embedding，保留 Godot API 契约，以 PydanticAI、Pydantic Graph 和 SQLite FTS5 实现 RAG 与 Agent 流程。

**Architecture:** FastAPI 继续作为兼容门面。SQLite FTS5 为世界知识和会话事实提供检索；Pydantic Graph 将聊天和外出流程编排为显式状态机，PydanticAI 以 OpenAI-compatible 配置生成强类型结果。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、PydanticAI、Pydantic Graph、SQLite FTS5、pytest。

## Global Constraints

- 监听地址、现有 HTTP 路由、请求字段和 Godot 已消费的响应字段保持兼容。
- 模型配置只能依赖 `base_url`、`api_key`、`model` 和可选 `proxy_url`。
- 不删除 `data/runtime/chroma`，不读取其内容作为运行时依赖。
- 不保留 LangChain、Chroma、FastEmbed 或 embedding 配置项。

## File Structure

- `app/config.py`：移除 Chroma/embedding 设置，新增 FTS 数据库路径。
- `app/rag/sqlite_store.py`：知识文档分块、FTS5 索引、状态、检索和清理。
- `app/memory/store.py`：事实 FTS5 生命周期与检索。
- `app/memory/retriever.py`：SQLite 事实检索适配层，保留现有调用名。
- `app/llm_provider.py`：OpenAI-compatible PydanticAI model factory。
- `app/agent_graphs.py`：聊天、外出 Pydantic Graph state/node 与执行入口。
- `app/chat_orchestrator.py`、`app/expedition_orchestrator.py`：保留回退与持久化，委托图运行。
- `app/main.py`：组装新的 stores 和 Graph 依赖，不改路由。
- `tests/test_*`：替换 Chroma 断言，覆盖 FTS、Graph、路由兼容。

### Task 1: 建立 SQLite FTS5 RAG 与会话记忆索引

**Files:**
- Create: `app/rag/sqlite_store.py`
- Modify: `app/config.py`, `app/rag/indexer.py`, `app/rag/retriever.py`, `app/memory/store.py`, `app/memory/retriever.py`
- Test: `tests/test_rag_indexer_retriever.py`, `tests/test_memory_retriever.py`, `tests/test_memory_store.py`

**Interfaces:**
- Produces: `SQLiteRAGStore.ingest(folder, clear_first) -> dict`, `retrieve(query, top_k) -> list[dict]`, `clear() -> dict`。
- Produces: `MemoryStore.search_memory_facts(session_id, query, limit)` 通过 `memory_fts` 返回活跃事实。

- [ ] 编写失败测试：写入中文知识和旧事实，断言 FTS 检索命中且 session 隔离。
- [ ] 运行 `pytest tests/test_rag_indexer_retriever.py tests/test_memory_retriever.py -v`，确认旧 Chroma 依赖测试失败。
- [ ] 实现 FTS5 virtual table、CJK bigram 查询规范化、知识分块/重建、事实同步、删除和清空。
- [ ] 将 `RAGIndexer`/`RAGRetriever` 改为 SQLite 实现，保持 `ingest`、`retrieve`、`status`、`clear` 方法签名。
- [ ] 重新运行上述测试，确认通过。

### Task 2: 替换模型依赖并定义强类型 Agent 输出

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `app/config.py`, `app/llm_provider.py`, `app/schemas.py`
- Test: `tests/test_llm_provider.py`, `tests/test_schemas.py`

**Interfaces:**
- Produces: `LLMProvider.build_agent_model(provider) -> Model`，由 `ResolvedProvider` 创建 PydanticAI OpenAI-compatible model。
- Produces: `NPCDecision` 与 `ExpeditionDecision`，字段可转换为既有 `ChatResponse`/`ExpeditionResponse`。

- [ ] 编写失败测试：以本地兼容地址创建模型，断言没有 LangChain 类型且缺少 URL/model 时仍报 `ProviderResolutionError`。
- [ ] 运行 `pytest tests/test_llm_provider.py tests/test_schemas.py -v`，确认迁移前失败。
- [ ] 用 `pydantic-ai`/`pydantic-graph` 替换依赖，移除 LangChain、Chroma、FastEmbed 与 embedding 设置。
- [ ] 实现兼容模型 factory；保留 Godot 配置优先级、空 API key 本地模型和代理支持。
- [ ] 重新生成锁文件并运行上述测试。

### Task 3: 实现 Pydantic Graph 聊天与外出状态机

**Files:**
- Create: `app/agent_graphs.py`
- Modify: `app/chat_orchestrator.py`, `app/expedition_orchestrator.py`
- Test: `tests/test_chat_orchestrator.py`, `tests/test_expedition_route.py`, `tests/test_agent_graphs.py`

**Interfaces:**
- Produces: `ChatGraph.run_sync(request, deps) -> ChatResponse` 与 `ExpeditionGraph.run_sync(request, deps) -> ExpeditionResponse`。
- Consumes: 既有 prompt builder、response parser、fallback、timeline 和 memory extraction。

- [ ] 编写失败测试：假模型返回合法 JSON 时 Graph 产出通过契约校验；模型错误时保留原本 fallback/error 行为。
- [ ] 运行 `pytest tests/test_chat_orchestrator.py tests/test_expedition_route.py tests/test_agent_graphs.py -v`，确认 Graph 尚不存在。
- [ ] 实现加载上下文、FTS 检索、PydanticAI 生成、结果解析/动作白名单校验、写入记忆的节点和边。
- [ ] 让两个 orchestrator 只作为 Graph 的同步外观，复用既有本地回退和时间线分叉逻辑。
- [ ] 重新运行上述测试，确认通过。

### Task 4: 组装服务并验证 Godot 兼容契约

**Files:**
- Modify: `app/main.py`, `run_server.py`, `docs/backend-quick-guide.md`, `.env.example`
- Test: `tests/test_health.py`, `tests/test_chat_route.py`, `tests/test_rag_routes.py`, `tests/test_memory_routes.py`, `tests/test_model_probe_route.py`

- [ ] 编写失败测试：`/ingest`、`/rag/status`、`/rag/clear`、`/memory/clear` 返回兼容字段且不提及 Chroma。
- [ ] 运行目标路由测试，确认旧向量计数断言失败。
- [ ] 在 lifespan 中构造 SQLite RAG 与记忆检索器；改写清理计数为 FTS 文档/事实计数；更新运维文档。
- [ ] 运行 `pytest -q`，修复所有旧依赖引用和契约差异。
- [ ] 用 `TestClient` 调用 `/health`、`/model/probe`、`/chat`、`/outing/resolve` 和管理接口，记录响应字段。

### Task 5: 清理遗留依赖并做回归验证

**Files:**
- Delete: `app/rag/embeddings.py`
- Modify: 所有仍引用 LangChain/Chroma/FastEmbed 的一方代码和测试

- [ ] 运行 `rg -n -i 'langchain|chroma|fastembed|embedding' app tests pyproject.toml`，确认仅迁移说明可提及旧 Chroma 目录。
- [ ] 运行 `python -m compileall app`、`pytest -q` 和 `ruff check app tests`。
- [ ] 启动 `run_server.py`，调用健康检查，确认默认端口 `5678` 和 Godot supervisor 可用。
