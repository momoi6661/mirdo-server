# PydanticAI、Pydantic Graph 与 FTS5 后端重构设计

## 目标

将本地 Godot AI 服务从 LangChain、Chroma 与 embedding 依赖迁移到 PydanticAI、Pydantic Graph 和 SQLite FTS5，同时保持 Godot 现有 HTTP 请求与响应契约兼容。

## 约束

- 服务继续监听 `127.0.0.1:5678`，并保留现有公开路由及其字段语义。
- Godot 调用的 `/chat`、`/outing/resolve`、`/health`、`/model/probe`、会话、记忆和 RAG 管理接口不得破坏。
- LLM 必须通过 OpenAI-compatible 配置调用：`base_url`、`api_key`、`model` 与可选 `proxy_url`；不绑定任何单一服务商。
- 保留 RAG，但改为无需向量、embedding 模型或 Chroma 的 SQLite FTS5 检索增强生成。
- 现有 `data/runtime/chroma` 仅作为历史数据保留；迁移过程不得删除它。

## 架构

### HTTP 与契约层

FastAPI 路由继续接受当前 `ChatRequest`、`OutingResolveRequest` 等 Pydantic 模型，并继续返回 Godot 已消费的 JSON。新增的内部追踪字段只能置于现有可选 `_debug` 中，不能要求客户端升级。

### LLM 适配层

建立一个 PydanticAI model factory，以当前 `Settings` 的 OpenAI-compatible 配置创建模型。聊天与外出各自声明严格的 Pydantic 结果模型；如果上游模型不可用、返回非结构化内容或校验失败，调用现有本地回退而非让游戏流程中断。

### Pydantic Graph 流程层

聊天图采用显式状态：`LoadContext -> RetrieveContext -> GenerateResponse -> ValidateResponse -> PersistTurn`。外出图采用同样的边界，但以外出上下文和结算结果模型替代 NPC 行为模型。图的 state 保存请求、会话快照、检索命中、候选结果、最终结果和调试信息；节点只依赖显式 deps，便于单元测试。

### SQLite FTS5 RAG 与记忆层

一个 SQLite 数据库保存会话、事件、长期事实、知识文档与两个 FTS5 索引：

- `knowledge_fts`：由 `/ingest` 从 `data/knowledge` 重建，供世界设定与规则检索。
- `memory_fts`：与长期事实保持同步，按 `session_id` 限定查询。

中文查询将采用现有 CJK bigram 规范化策略，结合原查询构造 FTS 查询；结果按 BM25、类型与最近性进行稳定排序。`/rag/status`、`/rag/clear`、`/memory/clear` 保留语义，但改为报告 FTS 文档与事实清理结果。

## 数据迁移

会话、事件和事实继续复用或迁入 `conversations.sqlite3`。旧 Chroma 目录不读取、不删除；已有世界知识在首次 `/ingest` 时建立 FTS，已有长期事实在服务初始化时回填 `memory_fts`。迁移设计必须幂等，重复启动不产生重复索引条目。

## 验证

- 更新依赖锁定，移除 LangChain、Chroma、FastEmbed 及 embedding 配置。
- 对 model factory、每个 Graph 节点、FTS5 查询、幂等索引、记忆清理和旧数据回填增加单元测试。
- 用 FastAPI 路由测试验证现有 JSON 契约、模型失败回退和 RAG 调试信息。
- 运行后端完整 pytest 集；通过 Godot 的编辑器请求工具或现有 Godot 测试确认 `/health`、`/model/probe`、`/chat` 与记忆管理接口可用。
