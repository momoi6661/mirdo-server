# Mirdo 后端架构说明

本文档记录 `D:\AAgodot\Server` 当前后端的运行方式、RAG/记忆架构、Godot 接口，以及外出故事生成逻辑。对应 Godot 项目为 `D:\AAgodot\FPS`。

## 1. 服务入口

- 启动入口：`run_server.py`
- FastAPI 应用：`app/main.py`
- 默认地址：`http://127.0.0.1:5678`
- Godot 运行项目（包括编辑器内运行和导出游戏）会自动检查并拉起后端：`D:\AAgodot\FPS\ai\AIServiceSupervisor.gd`。

主要路由：

| 路由 | 用途 |
| --- | --- |
| `GET /health` | 检查 LLM、RAG、SQLite 记忆库状态 |
| `GET /model/probe` | 探测当前模型是否可用 |
| `POST /ingest` | 重建 Chroma 知识库 |
| `POST /chat` | Mirdo 对话：完整 JSON 一次返回 |
| `POST /outing/resolve` | 外出地图 AI 结算和故事生成 |
| `GET /session/{session_id}/history` | 调试用：查看会话历史 |
| `GET /session/{session_id}/snapshot` | 调试用：查看会话摘要、最近对话和长期记忆 |
| `POST /memory/clear` | 清理单会话或全部会话记忆 |

## 2. 为什么不使用 streaming

游戏端需要一次性拿到这些字段并同步播放：

- `dialogue`
- `emotion`
- `expression`
- `action`
- `command`
- `command_payload`
- `visemes` / `viseme_sequence`
- `stat_change`
- `memory_updates`

如果使用流式返回，文字可能先到，动作、表情、口型和命令后到，会造成“说话已经开始但动作/嘴型还没同步”的错位。因此当前 `/chat` 和 `/outing/resolve` 都保持完整 JSON 一次返回。

## 3. LLM Provider

核心文件：`app/llm_provider.py`

当前使用 OpenAI-compatible HTTP 调用：

- 直接通过 `httpx.Client` 请求 `/chat/completions`
- `/chat` 和 `/outing/resolve` 共用这一套 provider；外出故事生成显式使用 `json_mode=True`，即请求体带 `response_format={"type":"json_object"}`，减少完整故事 JSON 解析失败。
- 支持 Godot `AISettings` 中的：
  - `base_url`
  - `api_key`
  - `model`
  - `proxy_url`
- 显式使用 `proxy_url`，避免环境代理和应用代理不一致。
- 连接对象会缓存复用，减少重复建连开销。

当前实测结论：

- RAG 检索约 `0.02s`
- 主要耗时在模型完整 JSON 生成，约 `12s`
- 慢点不是 embedding，也不是 prompt 数量爆炸，而是远端模型/API 完整响应时间。

## 4. RAG 知识库

核心文件：

- `app/rag/embeddings.py`
- `app/rag/indexer.py`
- `app/rag/retriever.py`
- `app/rag/loaders.py`

默认 embedding：

```python
embedding_provider = "fastembed"
embedding_model = "BAAI/bge-small-zh-v1.5"
embedding_cache_dir = "data/models/fastembed"
```

本地模型位置：

```text
D:\AAgodot\Server\data\models\fastembed\fast-bge-small-zh-v1.5
```

Chroma 正式库：

```text
D:\AAgodot\Server\data\runtime\chroma
```

知识源：

```text
D:\AAgodot\Server\data\knowledge
```

重点世界观文档：

- `worldview.md`
- `mirdo_home_and_outside_contrast.md`
- `outing_zombie_survival_rules.md`
- `mirdo_action_sheet.md`

当前世界观要求：

- 外面是危险的丧尸末世，有感染、废墟、噪声、搜索风险。
- 避难所是老师和 Mirdo 一起守着的安全据点；它不是普通住宅，但因为灯光、物资架、安全门和陪伴而像家一样温暖。
- Mirdo 陪伴主角/老师，会关心老师、安全和补给。
- 外出故事要体现“外面危险”和“回到避难所温暖”的反差。

## 5. 长期记忆

核心文件：

- `app/memory/store.py`
- `app/memory/extractor.py`

SQLite 数据库：

```text
D:\AAgodot\Server\data\runtime\conversations.sqlite3
```

记忆分两类：

1. 最近对话：`turns`
2. 长期事实：`memory_facts`

`MemoryExtractor` 会从玩家输入和模型的 `memory_updates` 中抽取事实，例如：

```json
{"subject":"player","predicate":"likes","value":"罐头汤","confidence":0.8}
```

当前已经加入两层记忆检索：

1. `MemoryRAGRetriever`：把 `memory_facts` 同步到 Chroma 的独立 `session_memory` collection，使用本地 fastembed 做语义检索。
2. `search_memory_facts(session_id, query, limit)`：SQLite 轻量词面检索作为兜底，按当前输入与记忆事实的重合、偏好类关键词、置信度和新近度排序。

旧逻辑只按更新时间取最近事实；现在“你还记得我喜欢吃什么吗？”可以找回较早写入的 `player likes: 罐头汤`，而不是被最近无关记录挤掉。

## 6. `/chat` 对话链路

核心文件：`app/chat_orchestrator.py`

流程：

1. 写入用户 turn。
2. 读取最近对话。
3. 使用 `search_memory_facts` 检索长期记忆。
4. 使用 Chroma RAG 检索世界知识。
5. `PromptBuilder` 组装：
   - runtime state
   - long-term memory
   - world knowledge
   - recent dialogue
6. 调用 LLM。
7. 由 PydanticAI 校验结构化输出（不再维护独立 ResponseParser）。
8. 执行行为后处理：修正动作、表情、命令、称呼。
9. 写入新的长期记忆和 assistant turn。
10. 返回完整 `ChatResponse`。

`used_knowledge` 和 `used_memory` 会随响应返回，方便调试 RAG 和记忆是否命中。

## 7. `/outing/resolve` 外出故事链路

核心文件：`app/expedition_agent.py`、`app/expedition_orchestrator.py`

Godot 调用位置：

```text
D:\AAgodot\FPS\levels\outing\outing_map_level_v3.gd
```

请求字段包括：

- 地点规则：名称、描述、路线提示、威胁等级、地点 AI 规则。
- 携带物：物品名、分类、数量、AI 规则提示。
- 时间：路程、搜索、总耗时。
- 可生成 loot 路径白名单。
- 可解锁邻居地点。
- 当前 provider 配置。
- `session_id` 与 `/chat` 共用同一条 AI 时间线；未传时默认为 `default_session`。

外出使用独立的 GM Agent（不是 Mirdo Agent），并注入：

- 同 session 的近期对话和明确 `wants` 目标，让 GM 知道主角真正想找什么。
- 同 session 的长期记忆和已经保存的 `story_markers`；同地点的 active 线索优先续写。
- 世界知识 RAG 命中内容。
- 固定世界基调：外面危险，主角需要在风险和资源之间做决定。
- 使用 PydanticAI `ExpeditionResponse` 结构化输出，故事标记在模型成功后由编排器统一落库。

输出字段：

```json
{
  "ok": true,
  "title": "外出行动报告",
  "summary": "一句摘要",
  "story": "完整连续探索故事",
  "experience": ["行动日志"],
  "risk_result": "风险与代价",
  "loot": [{"item_path":"...","amount":1,"tag":"..."}],
  "discovered_clues": [],
  "search_focus": ["玩家明确想寻找的目标"],
  "story_markers": [{"continuity_key":"旧药店:地下室", "status":"active", "next_hooks":["寻找钥匙"]}],
  "mood": "谨慎",
  "health_damage": 0
}
```

重要保护：

- `loot` 只能使用 Godot 传来的 `available_loot` 路径，不能凭空造物品。
- 模型/API 真失败时返回 `ok=false`，Godot 端会阻止扣物资、写地图进展。这比“假装成功”更安全。
- 后端解析时如果模型没写 `story`，会补一个本地 fallback story，保证结果页有故事文本。

## 8. 性能现状和优化方向

已确认：

- 本地 embedding 和 RAG 检索不是主耗时。
- SQLite 记忆读写耗时很低。
- 主要瓶颈是远端 LLM 完整 JSON 返回。

已做优化：

- 本地 fastembed 替代不存在/不稳定的 embedding API。
- Chroma retriever 复用 vector store。
- LLM HTTP client 复用连接并显式走代理。
- 对话和外出都保留完整 JSON，一次性同步到 Godot。
- 记忆从“最近事实”升级为“相关性检索 + 最近兜底”。

下一步建议：

1. 对 `/chat` 默认启用 JSON mode，如果当前模型兼容 `response_format={"type":"json_object"}`。
2. 对外出故事减少 token：把 `story` 目标降到 350~520 字，可能从 12s 降低一些。
3. 增加 `/debug/prompt` 或日志开关，方便直接查看 prompt 长度、RAG 命中和 memory 命中。
4. 如果必须明显变快，换更快的 provider/model 比继续压 RAG 更有效。


## 9. Mirdo 行为意图合同更新

依据 `D:\AAgodot\FPS\docs\mirdo_ai_backend_task.md`，后端对 `/chat` 的行为字段遵守以下规则：

- 角色身份来自 `ChatRequest.context.npc`，默认是 Mirdo，不再以“小空”作为默认身份。
- `action` 会被后处理规范到 `context.npc.available_body_actions`，旧的 `Talk` 会转成 `listen`。
- `expression` 会被规范到 `context.npc.available_expressions`。
- `visemes` / `viseme_sequence` 会被限制到 `context.npc.available_visemes`。
- 玩家要求查看、检查、打开设施时，后端必须输出可执行 `command`，不能只口头回答。
- 目标优先级：
  1. 当前 `context.perception.nearby_objects / visible_items / areas` 中的 object id；
  2. 没有当前感知目标时，才退到 `known_nav_points / ai_nav_points`。
- 因此“去看看食物柜”在当前感知里有 `food_cabinet` 时会输出：

```json
{
  "command": "go_to_object",
  "command_payload": {
    "target_object": "food_cabinet",
    "marker_role": "approach"
  },
  "action": "work_count_supplies"
}
```

## 10. 真实外出归来事件

如果 `ChatRequest.context` 包含：

```json
{
  "event": "real_outing_return",
  "real_outing": true,
  "location_name": "超市",
  "total_minutes": 86,
  "loot_added": 3,
  "health_damage": 4
}
```

后端会把它视为 Godot 已确认的真实外出归来事件，而不是普通地图 UI 切换。

此时行为后处理会强制：

- 清空 `command` 和 `command_payload`；
- 禁止 `go_to_object`、`go_to_nav_point`、`follow_player`；
- 使用轻量欢迎动作，例如 `tiny_wave`、`react_wave`、`happy_bounce`、`listen`；
- 对白保持 1~2 句欢迎/关心，不写长剧情。

Mirdo 本地迎接移动仍由 Godot 的 `CharacterReturnGreetingComponent` 处理；后端只负责短对白、表情和轻量动作建议。

## 存档与 AI 平行时间线

Godot 的游戏存档保存 `ai_timeline_id` 和 `ai_turn_id`。后端把 `session_id` 当作一条 AI 时间线，而不是简单的存档槽名。

- 正常继续游戏：请求携带当前 `session_id` 和 `ai_checkpoint_turn_id`，如果 checkpoint 已经是最新或没有未来内容，直接追加到同一时间线。
- 读取旧存档后继续：请求的 `ai_checkpoint_turn_id` 小于该 `session_id` 的最新 turn，后端复制 checkpoint 以前的 turns/memory facts 到新的 `:branch_xxx` session，再把本次新对话写入分支。
- 后端不会删除旧时间线，因此之前保存的未来进度仍可再次读取。
- 响应会返回新的 `session_id`、`turn_id`，以及可选的 `forked_from`、`forked_at_turn_id`；Godot 需要把它们写回当前运行态，下一次保存时进入存档。
