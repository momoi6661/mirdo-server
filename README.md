# Mirdo Server

Mirdo Server 是 Mirdo 游戏的本地 AI 后端。它不是简单的聊天转发服务，而是一个围绕游戏 NPC 设计的 Agent 后端：接收玩家输入和 Godot 场景状态，组织上下文，调用 PydanticAI Agent，按需使用 tools 查询记忆/知识/动作能力，然后返回 Mirdo 的对白、情绪、行为线和可选 TTS 音频。

## 仓库关系

- 游戏端：`https://github.com/momoi6661/mirdo`
- 后端服务：`https://github.com/momoi6661/mirdoserver`

本仓库只保存后端代码、知识库文档和服务配置；Godot 导出的 exe/zip 放到游戏仓库 Release，不提交到后端仓库。

## 核心能力

- FastAPI 后端服务，默认端口 `5678`。
- PydanticAI Agent：负责 Mirdo 对话、工具调用和结构化输出。
- pydantic-graph：负责一轮聊天的 Graph 编排，例如保存输入、加载上下文、运行 Agent、校验并持久化。
- Context Engineering：通过 `MirdoContextEngine` 控制每轮上下文预算，避免把所有知识、导航和历史都塞进 prompt。
- PromptedOutput 结构化输出：统一把 Pydantic 模型转成 JSON Schema 注入提示词，再由 PydanticAI 校验结果。
- Tools：记忆查询、知识库查询、共同经历读取、保存事实、保存故事事件、读取 Godot 可用动作。
- SQLite / FTS5 RAG：用于知识库和轻量检索，不依赖旧向量数据库服务。
- 长会话摘要、事实记忆、故事事件和冲突处理。
- 外出 GM 叙事：主角外出时由独立 GM Agent 生成连续故事。
- 可选 VOICEVOX TTS：后端可以生成日语语音并返回 URL 或 inline base64。
- Docker / uv 两种启动方式。

## 快速启动：uv

```powershell
cd D:\AAgodot\Server
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 5678
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:5678/health
```

## 快速启动：Docker

```powershell
cd D:\AAgodot\Server
docker compose up -d --build
docker compose logs -f mirdo-server
```

容器名固定为：

```text
mirdo-server
```

宿主机访问地址：

```text
http://127.0.0.1:5678
```

## 模型配置

复制 `.env.example` 为 `.env`：

```env
API_BASE_URL=https://api.openai.com/v1
API_KEY=your-key
CHAT_MODEL=gpt-4o-mini
REQUEST_TIMEOUT=45
CHAT_MAX_TOKENS=0
MODEL_TOOLS_ENABLED=true
```

说明：

- 模型接口按 OpenAI-compatible 形式接入。
- `CHAT_MAX_TOKENS=0` 表示不主动传 `max_tokens`，让上游模型使用默认输出预算。
- Godot 请求可以携带 provider 配置覆盖 `.env` 默认模型。
- 当前最终结构化输出统一使用 `PromptedOutput`，避免按 DeepSeek/非 DeepSeek 做输出分支。

## 架构文档

- [可引导的事件驱动 Agent 循环](docs/steerable_event_driven_agent_loop.md)：详细说明 Human-in-the-loop、steering、Godot tool result、动作回执和多次 `agent.run` 如何组成连续 NPC 行为。

## Agent 与上下文怎么工作

一轮 `/chat` 大致流程：

```text
Godot /chat 请求
  -> Graph 保存玩家输入或 Godot 动作回执
  -> ContextEngine 生成本轮上下文计划
  -> 并行检索记忆、知识、故事事件
  -> Agent.run(...)
       - 固定人格和行为规则来自 Agent instructions
       - 本轮 runtime_state / memory / story / knowledge 来自 run instructions
       - 最近对话来自 message_history
       - 更深信息通过 tools 按需查询
  -> 校验 ChatResponse
  -> 保存记忆、故事、回复
  -> 返回 dialogue / action_line / tts
```

关键文件：

```text
app/mirdo_agent.py       # PydanticAI Agent、tools、PromptedOutput
app/agent_graphs.py      # Chat Graph 编排
app/context_engine.py    # 上下文选择注入
app/memory/              # 记忆、故事事件、摘要
app/rag/                 # SQLite/FTS5 知识库检索
app/tts/                 # VOICEVOX TTS 适配
```

## 知识库与 RAG

知识库文档放在：

```text
data/knowledge/
```

重新摄取知识库：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:5678/ingest -ContentType 'application/json' -Body '{}'
```

运行时索引、记忆库、日志等生成数据位于：

```text
data/runtime/
```

该目录不提交到 Git。

## 记忆系统

服务会保存：

- 最近对话 turn。
- 长会话摘要。
- 玩家明确说出的长期事实和偏好。
- 已发生且值得回忆的日常/剧情事件。
- 外出故事 continuity key 和线索状态。

普通对话不会把所有记忆直接塞进 prompt；Agent 需要更早事实时可以调用 `recall_memory`、`recall_session_summary` 或 `recall_story_events`。

## TTS / VOICEVOX

TTS 引擎需要用户自己下载、配置并启动。当前推荐 VOICEVOX GPU 版本，默认地址：

```text
http://127.0.0.1:50021
```

请求里可以决定是否启用 TTS、speaker id 和音频返回方式。后端支持：

- `inline`：直接在 `/chat` 返回 base64 音频，Godot 不用二次下载。
- `url`：返回后端音频 URL。
- `auto`：由后端选择。

GPU 版本通常比 CPU 版本更适合游戏实时语音；如果没有 NVIDIA GPU，也可以先用 CPU 版测试音色。

## 常用接口

```text
GET  /health
POST /chat
POST /ingest
GET  /tts/profiles
POST /tts/synthesize
```

## 测试

```powershell
cd D:\AAgodot\Server
uv run pytest -q
```

当前测试覆盖 Agent 架构、上下文工程、TTS、记忆和后端路由。

## 不提交的内容

`.gitignore` 应排除：

- `.env` 和密钥。
- `.venv/`。
- `data/runtime/`。
- 日志、缓存、临时文件。
- Godot 导出的 exe/zip。

## 与游戏端联调

1. 启动后端：`uv run uvicorn app.main:app --host 127.0.0.1 --port 5678`。
2. 可选启动 VOICEVOX Engine：`http://127.0.0.1:50021`。
3. 打开 Mirdo 游戏端。
4. 在 AI Settings 设置后端 Base URL、模型和 API Key。
5. 与 Mirdo 对话，观察后端终端中的 input、context、tool、model_timing 日志。
