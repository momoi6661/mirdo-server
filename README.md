# Mirdo Server

Mirdo Server 是 Mirdo 游戏的本地 AI 后端服务，负责把玩家输入、Godot 场景状态、角色记忆和知识库资料组织成一次完整的 Agent 回合。

它的目标不是简单转发聊天模型，而是为游戏角色提供：

- PydanticAI Agent 对话
- Pydantic Graph 回合编排
- 工具调用式记忆与知识检索
- Godot 行为链规划与结果接收
- 长会话摘要与事实记忆
- 外出 GM 故事连续性
- 可选的 VOICEVOX TTS 生成
- Docker / uv 两种启动方式

## 仓库关系

- 游戏端：`https://github.com/momoi6661/mirdo`
- 后端服务：`https://github.com/momoi6661/mirdoserver`

本仓库只放后端服务代码，不提交 Godot 导出的 exe。导出产物放到游戏仓库的 GitHub Release。

## 技术栈

- Python 3.11+
- uv
- FastAPI / Uvicorn
- PydanticAI
- pydantic-graph
- SQLite / FTS5
- OpenAI-compatible chat model
- VOICEVOX Engine，可选
- Docker，可选

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

Docker Compose 项目名和容器名固定为：

```text
mirdo-server
```

宿主机访问地址：

```text
http://127.0.0.1:5678
```

更多说明见：

```text
docs/docker.md
```

## 配置

复制 `.env.example` 为 `.env`，然后按需填写：

```env
API_BASE_URL=https://api.openai.com/v1
API_KEY=your-key
CHAT_MODEL=gpt-4o-mini
REQUEST_TIMEOUT=45
CHAT_MAX_TOKENS=0
MODEL_TOOLS_ENABLED=true
```

说明：

- `CHAT_MAX_TOKENS=0` 表示不主动传 `max_tokens`，让服务商按模型默认值处理。
- 模型接口要求 OpenAI-compatible。
- Godot 请求里也可以传入 provider 配置覆盖默认值。

## 知识库与 RAG

知识库文档放在：

```text
data/knowledge/
```

当前实现使用 SQLite/FTS5 检索，不再依赖旧向量数据库运行目录。

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

- 最近对话
- 长会话摘要
- 玩家事实
- Mirdo 对玩家的印象
- 行为任务事件
- 外出故事连续性

记忆和检索不是硬塞进固定 prompt，而是作为 Agent 上下文与工具能力的一部分进入回合编排。

## Godot 行为链

后端返回给 Godot 的行为以 `line` / action chain 为核心，Godot 执行动作后把执行结果回传给后端。这样后端能知道：

- 是否到达目标
- 是否拿到物品
- 是否递交成功
- 是否失败以及失败原因

目标是形成：

```text
玩家说话 -> Mirdo 回复/行动 -> Godot 执行 -> 回传事件 -> 后端继续规划
```

## TTS

VOICEVOX 是可选能力。默认后端不强制生成 TTS，是否生成由请求决定。TTS 引擎需要用户自己下载、配置并启动；推荐优先使用 VOICEVOX GPU 版本，GPU 版通常能明显降低语音生成等待时间。没有可用 NVIDIA GPU 时，可以使用 CPU 版作为兼容方案。

VOICEVOX 默认地址：

```text
http://127.0.0.1:50021
```

TTS 配置和角色音色定义位于：

```text
data/tts/
```

说明文档：

```text
docs/tts-voicevox-guide.md
```

## 测试

```powershell
cd D:\AAgodot\Server
uv run pytest -q
```

当前提交前验证结果：

```text
82 passed
```

## 不提交的内容

`.gitignore` 已排除：

- `.env` 与本地密钥
- `.venv/`
- `__pycache__/`
- `data/runtime/`
- `*.sqlite3`、`*.db`
- 生成音频：`*.wav`、`*.mp3`、`*.ogg`
- Godot 导出产物：`*.exe`、`*.pck`

## 常用命令

```powershell
# 本地启动
uv run uvicorn app.main:app --host 127.0.0.1 --port 5678

# Docker 启动
docker compose up -d --build

# 查看 Docker 日志
docker compose logs -f mirdo-server

# 运行测试
uv run pytest -q
```

