# Server

本目录是游戏的本地对话服务。它由 Godot 启动或连接，负责：

-  NPC 对话
- 世界观 RAG 检索
- SQLite 长期记忆
- OpenAI-compatible 模型调用
- Godot 兼容 SSE 流式输出

详细设计见：`docs/server-v2-design.md`

## 本地开发启动

```powershell
cd D:\AAgodot\Server
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 5678
```

## Docker 启动（推荐）

容器内监听 `0.0.0.0:5678`，宿主机仍只暴露 `127.0.0.1:5678`。

```powershell
cd D:\AAgodot\Server
docker compose up -d --build
docker compose logs -f mirdo-server
```

详细配置见 `docs/docker.md`。VOICEVOX GPU 仍作为宿主机独立引擎运行，容器通过 `host.docker.internal:50021` 访问。

## Windows 发布形态

如果不使用 Docker，也可以继续使用 `uv run uvicorn ...`；Docker 和本地 uv 启动方式共享同一套数据目录。
