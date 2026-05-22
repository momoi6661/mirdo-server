# Server

本目录是游戏的本地对话服务。它由 Godot 启动或连接，负责：

-  NPC 对话
- 世界观 RAG 检索
- SQLite 长期记忆
- OpenAI-compatible 模型调用
- Godot 兼容 SSE 流式输出

详细设计见：`docs/server-v2-design.md`

## 开发启动（计划）

```powershell
cd D:\AAgodot\Server
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 5678
```

## 发布形态（计划）

发布时将本服务打包成 `server/xiaokong_server.exe`，由 Godot 自动启动。
