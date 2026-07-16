# Docker 运行说明

后端现在可以作为一个独立的 Docker 服务运行。容器只负责 FastAPI、PydanticAI、Graph、SQLite 记忆和 RAG；VOICEVOX GPU 引擎仍然是独立进程。

## 1. 配置模型服务

在 `D:\AAgodot\Server` 创建 `.env`（不要提交到 Git）：

```dotenv
API_BASE_URL=https://api.openai.com/v1
API_KEY=your-key
CHAT_MODEL=gpt-4o-mini
```

如果模型代理在宿主机，例如 `127.0.0.1:8317`，容器内要改成：

```dotenv
API_BASE_URL=http://host.docker.internal:8317/v1
```

容器里的 `127.0.0.1` 指向容器自身，不是 Windows 主机。

## 2. 启动

```powershell
cd D:\AAgodot\Server
docker compose up -d --build
```

检查状态和日志：

```powershell
docker compose ps
docker compose logs -f mirdo-server
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:5678/health
```

## 3. VOICEVOX

VOICEVOX 仍在宿主机运行：

```powershell
powershell.exe -ExecutionPolicy Bypass -File D:\AAgodot\VOICEVOX\start_engine.ps1
```

启动脚本使用 NVIDIA 版并带 `--use_gpu`。compose 中默认的 `TTS_ENGINE_URL` 是 `http://host.docker.internal:50021`，因此 Docker 后端可以访问宿主机的 VOICEVOX。

如果暂时不使用语音：

```dotenv
TTS_ENABLED=false
```

## 4. 停止与数据

```powershell
docker compose down
```

`data/runtime` 是宿主机挂载目录，包含 SQLite 记忆、RAG 索引、TTS 缓存和 `server.log`，删除容器不会删除这些数据。
