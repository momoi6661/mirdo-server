# 可引导的事件驱动 Agent 循环

本文解释 Mirdo 项目采用的核心交互架构：**可引导的事件驱动 Agent 循环，带人在回路和工具结果反馈**。

英文可以理解为：

```text
Steerable, event-driven agent loop with human-in-the-loop and tool-result feedback
```

它不是普通的“玩家输入一句，模型回复一句”的聊天机器人，而是让 Mirdo 在游戏世界里像一个有身体、有任务、有记忆的角色一样运转。

---

## 1. 为什么不是普通聊天

普通聊天模型的流程通常是：

```text
玩家输入
↓
模型回复
↓
结束
```

但游戏里的 Mirdo 需要：

```text
玩家输入
↓
Mirdo 理解意图
↓
人格化回应
↓
规划动作
↓
Godot 执行动作
↓
Godot 回传真实结果
↓
后端再次推理
↓
Mirdo 根据结果继续说话或行动
```

例如玩家说：

```text
去检查柜子。
```

Mirdo 不应该直接编造“柜子里有什么”，而应该先走到柜子，再由 Godot 检查真实物品，然后把结果回传给后端。

---

## 2. 核心概念

### 2.1 Agent Loop

Agent Loop 是“观察 → 规划 → 行动 → 观察”的循环。

在本项目里，它大致是：

```text
Observe  读取玩家输入、记忆、场景状态、工具结果
Plan     PydanticAI Agent 规划对白和 action_line
Act      Godot 执行导航、检查、拿取、递交等动作
Observe  Godot 把动作结果作为 tool result 回传
```

后端不会在一个 HTTP 请求里一直等 Godot 走路或拿东西；每一步都是一次新的请求和一次新的 `agent.run`。

---

### 2.2 Event-driven Agent

事件驱动表示：Mirdo 的下一步不是只由玩家文本触发，也可以由游戏事件触发。

常见事件：

- 玩家说话。
- Mirdo 到达目标点。
- 柜子检查完成。
- 物品拿取成功或失败。
- 玩家中途补充、改口或取消。
- 自主生活事件触发。

这些事件会进入后端上下文，但身份不同。玩家输入是 user message；Godot 动作结果是 tool result / observation。

---

### 2.3 Human-in-the-loop

人在回路表示：玩家可以在 Mirdo 生成、说话、行动过程中介入。

玩家的新输入可能是：

- 补充信息：`顺便看看有没有杯子。`
- 改口：`不用拿水，先回来。`
- 强打断：`停下。`
- 新任务：`先去检查医疗柜。`
- 临时问题：`你现在害怕吗？`

这些输入不应该都被当成普通新对话，而应由 Agent 判断对当前任务的影响。

---

### 2.4 Steerable Agent

可引导表示：玩家可以像使用 Codex 一样，在任务进行中改变方向。

项目使用 `steering` 协议描述这种引导：

```json
{
  "mode": "interrupt",
  "phase": "presentation",
  "target_request_id": "godot:mirdo:...",
  "target_client_sequence": 12,
  "heard_dialogue": "老师，我先去看看。",
  "interrupted_dialogue": "如果水还够，我就拿一瓶。",
  "boundary_reason": "segment_finished",
  "reason": "player_guidance"
}
```

含义：

- `heard_dialogue`：Mirdo 已经自然说完的当前句。
- `interrupted_dialogue`：原本准备继续说、但因为玩家介入被跳过的旧后续。
- `boundary_reason`：为什么在这里介入，例如当前语音段播放完成。
- `phase`：介入发生在生成中、播放中还是动作中。

这样后端知道玩家是在“引导当前任务”，不是开启一条完全无关的新对话。

---

### 2.5 Tool-result Feedback

Godot 在这个系统里不是“另一个用户”，而是 Agent 的工具执行器。

错误方式：

```text
玩家：柜子里有两瓶水。
```

正确方式：

```text
Tool result: inspect_object succeeded.
Observation: food_cabinet contains water_bottle x2.
```

这能避免后端把游戏观察误存成玩家说过的话。

---

## 3. 一次完整例子：检查柜子

### 第一步：玩家发起任务

玩家：

```text
Mirdo，去检查柜子。
```

Godot 请求后端 `/chat`：

```json
{
  "session_id": "mirdo_session",
  "player_text": "Mirdo，去检查柜子。",
  "context": {
    "request_source": "player"
  }
}
```

后端执行一次 `agent.run`，返回：

```json
{
  "dialogue_segments": [
    {"text": "好呀老师，我去看看。"}
  ],
  "action_line": [
    {
      "step_id": "go_cabinet",
      "command": "go_to_nav_point",
      "command_payload": {"target_ref": "food_cabinet"},
      "wait_for_result": true,
      "success_next_step": "inspect_cabinet"
    },
    {
      "step_id": "inspect_cabinet",
      "command": "inspect_object",
      "command_payload": {"target_ref": "food_cabinet"},
      "wait_for_result": true
    }
  ],
  "current_step_id": "go_cabinet"
}
```

后端这次请求已经结束，不会挂起等待 Godot。

---

### 第二步：Godot 执行导航

Godot 执行：

```text
go_to_nav_point(food_cabinet)
```

到达后，Godot 不是伪装成玩家，而是调用动作结果接口：

```json
{
  "session_id": "mirdo_session",
  "tool_call_id": "task123:go_cabinet",
  "task_id": "task123",
  "step_id": "go_cabinet",
  "command": "go_to_nav_point",
  "event": "navigation_goal_finished",
  "status": "succeeded",
  "ok": true,
  "observation": {
    "arrived": true,
    "target_ref": "food_cabinet"
  }
}
```

后端把它作为工具结果，再执行一次 `agent.run`。

---

### 第三步：后端根据工具结果继续

Agent 看到导航成功，于是继续下一步：

```json
{
  "dialogue_segments": [
    {"text": "老师，我到了。"},
    {"text": "我现在打开看看。"}
  ],
  "current_step_id": "inspect_cabinet",
  "action_line": [
    {
      "step_id": "inspect_cabinet",
      "command": "inspect_object",
      "command_payload": {"target_ref": "food_cabinet"},
      "wait_for_result": true
    }
  ]
}
```

Godot 再执行检查。

---

### 第四步：Godot 回传检查结果

```json
{
  "session_id": "mirdo_session",
  "tool_call_id": "task123:inspect_cabinet",
  "task_id": "task123",
  "step_id": "inspect_cabinet",
  "command": "inspect_object",
  "event": "object_inspected",
  "status": "succeeded",
  "ok": true,
  "observation": {
    "target_ref": "food_cabinet",
    "items": [
      {"id": "water_bottle", "name": "水瓶", "amount": 2},
      {"id": "bread", "name": "面包", "amount": 1}
    ]
  }
}
```

后端再次 `agent.run`，Mirdo 才能说：

```json
{
  "dialogue_segments": [
    {"text": "老师，里面还有两瓶水。"},
    {"text": "要我拿一瓶吗？"}
  ]
}
```

注意：这是基于 Godot 真实观察，不是模型编造。

---

## 4. 正在说话时的玩家引导

现在 Mirdo 的对白会拆成 `dialogue_segments`：

```json
{
  "dialogue_segments": [
    {"text": "老师，我先去看看。"},
    {"text": "如果水还够，我就拿一瓶。"}
  ]
}
```

Godot 会按顺序播放：

```text
第 1 句字幕 + 第 1 句语音
↓ 等语音结束
第 2 句字幕 + 第 2 句语音
```

如果玩家在第 1 句播放时输入：

```text
不用拿水，先回来。
```

默认策略不是立即打断半句话，而是：

```text
第 1 句自然说完
↓
跳过第 2 句
↓
把玩家新输入作为 steering 发给后端
↓
后端判断应该 cancel、replace、pause 还是 continue
```

如果玩家说的是明确打断词，例如：

```text
停下。
```

Godot 才会立即停止当前语音和字幕。

---

## 5. 前后端职责边界

### 后端负责

- 维护会话、摘要、记忆、故事事件。
- 选择本轮上下文。
- 调用 PydanticAI Agent。
- 通过 tools 查询记忆和知识。
- 生成结构化 `ChatResponse`。
- 规划 `action_line`。
- 接收 Godot 工具结果并继续推理。
- 可选生成 TTS。

### Godot 负责

- 收集玩家输入和场景感知。
- 执行后端给出的动作线。
- 导航、检查、拿取、递交、使用物品。
- 把动作结果作为 tool result 回传。
- 播放 TTS。
- 逐句显示 3D 字幕。
- 在玩家引导时按策略打断、延迟或排队。

---

## 6. 为什么要这样设计

这样做的好处：

1. **角色不编造世界状态**：柜子里有什么由 Godot 决定。
2. **连续任务可恢复**：每一步都有 `task_id`、`step_id`、`tool_call_id`。
3. **玩家可以自然介入**：普通补充等当前句说完，明确停止才硬打断。
4. **对话和行为统一**：说话、动作、工具结果都进入同一个 Agent Loop。
5. **记忆更干净**：玩家说的话、工具观察、长期事实不会混在一起。
6. **适合游戏节奏**：HTTP 请求短，走路和交互在 Godot 内异步完成。

---

## 7. 相关代码位置

### Godot 游戏端

```text
scripts/character_ai/components/character_ai_dialogue_component.gd
ai/AIVoicePlayer.gd
scripts/character_ai/components/character_action_executor_component.gd
scripts/character_ai/components/character_task_manager_component.gd
```

### 后端服务

```text
app/agent_graphs.py
app/mirdo_agent.py
app/context_engine.py
app/schemas.py
app/tts/chat.py
app/memory/
app/rag/
```

---

## 8. 简短总结

Mirdo 当前的目标架构是：

```text
玩家输入 / Godot 事件
↓
后端 Agent 理解上下文
↓
返回对白 + 行为线
↓
Godot 执行动作并展示语音字幕
↓
Godot 把真实结果作为 tool result 回传
↓
后端再次 agent.run 继续任务
```

这就是“可引导的事件驱动 Agent 循环，带人在回路和工具结果反馈”。
