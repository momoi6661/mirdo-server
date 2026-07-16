# VOICEVOX 后端联调说明

当前阶段只验证 Server，不修改 Godot。主后端启动后，TTS 接口也在同一个端口：

```powershell
cd D:\AAgodot\Server
uv run uvicorn app.main:app --host 127.0.0.1 --port 5678
```

VOICEVOX Engine 仍需要单独常驻在 `127.0.0.1:50021`。当前使用 Windows NVIDIA/CUDA
版，启动脚本是：

```powershell
powershell -ExecutionPolicy Bypass -File D:\AAgodot\VOICEVOX\start_engine.ps1
```

脚本会使用 `windows-nvidia\run.exe --use_gpu`。GPU 引擎第一次合成会加载模型，之后
短句合成明显更快；测试中预热后的 `audio_query + synthesis` 约 0.5 秒。若引擎不在默认地址，设置：

```powershell
$env:TTS_ENGINE_URL = "http://127.0.0.1:50021"
```

## 先测试后端

```powershell
Invoke-RestMethod http://127.0.0.1:5678/tts/health
Invoke-RestMethod http://127.0.0.1:5678/tts/info
Invoke-RestMethod http://127.0.0.1:5678/tts/dialogue/ja_jp/mirdo/opening

$body = @{ text = "おかえり。"; voice_profile = "mirdo_ja"; emotion = "温柔" } |
    ConvertTo-Json
Invoke-WebRequest http://127.0.0.1:5678/tts/synthesize `
    -Method Post -ContentType "application/json" -Body $body `
    -OutFile .\data\runtime\tts\manual_test.wav
```

## Mirdo 声线选择

默认声线是 **もち子さん / 麻糬子**。可选声线通过请求里的
`tts_voice_profile` 选择，不需要修改 Python 代码：

| `tts_voice_profile` | 声线 | VOICEVOX `speaker_id` |
| --- | --- | ---: |
| `mirdo_ja` | もち子さん / 麻糬子（默认） | 20 |
| `mirdo_ja_bii` | 猫使ビィ / 猫使比伊 | 58 |
| `mirdo_ja_hau` | 雨晴はう / 雨晴羽 | 10 |
| `mirdo_ja_kotone` | 琴詠ニア / 琴咏妮娅 | 74 |
| `mirdo_ja_voidoll` | Voidoll | 89 |
| `mirdo_ja_ankomon` | あんこもん / 红豆萌（普通声线） | 113 |

例如使用猫使ビィ：

```json
{
  "player_text": "我今天有点累",
  "use_tts": true,
  "generate_japanese": true,
  "tts_voice_profile": "mirdo_ja_bii"
}
```

如果只想临时指定 VOICEVOX 的风格 ID，也可以传 `tts_speaker_id`。它的优先级高于
profile 文件中的 ID，但不会修改默认配置：

```json
{
  "use_tts": true,
  "generate_japanese": true,
  "tts_voice_profile": "mirdo_ja",
  "tts_speaker_id": 58
}
```

直接调用 `/tts/synthesize` 时字段名称是 `speaker_id`；调用 `/chat` 或
`/godot/action-result` 时使用 `tts_speaker_id`，避免和其他协议字段混淆。

声线 JSON 位于 `data/tts/characters/`。每个选项文件只保存 `speaker_id`；公共
情绪参数集中在 `app/tts/profiles.py`，因此换声线不会复制一大段调音配置。

`/tts/synthesize` 返回 WAV。`/chat` 默认只返回文字；请求传入 `use_tts: true` 后才会
生成对应语音。Godot 接入时也可以直接使用返回的 `tts.audio_url`。

## Chat 与 Agent 的关系

`/chat` 只有在请求明确传入 `use_tts: true` 时，才会把 Graph/Agent 最终产出的
`dialogue` 和 `emotion` 交给 TTS Provider：

```json
{
  "use_tts": true,
  "generate_japanese": true,
  "player_text": "おかえり"
}
```

Agent 会返回中文 `dialogue`，以及可选的平行字段 `dialogue_ja`。VOICEVOX 的日语声线会
优先使用 `dialogue_ja`。响应里的 `tts.audio_url` 是相对地址，例如
`/tts/audio/<cache_key>`。不传 `use_tts` 或明确关闭时只返回文字：

```json
{
  "use_tts": false,
  "player_text": "只返回文字"
}
```

这样 TTS 不会由模型自己决定，也不会把音频二进制塞进 Agent 的结构化输出；Agent 只负责
对白和情绪，后端负责稳定地生成和缓存音频。

## Godot 的字幕与播放顺序

当前 `/chat` 在返回 JSON 前会等待一次 VOICEVOX 合成（缓存命中时不会再次访问引擎），
所以 Godot 收到响应时，`tts.audio_url` 已经是可下载的 WAV 地址。Godot 的呈现顺序固定为：

1. `CharacterAIDialogueComponent` 收到 `dialogue` 和可播放的 `tts`。
2. 先发出 `dialogue_presenting`，`XiaokongControlComponent` 显示这一句字幕。
3. 再由角色身上的 `AIVoicePlayer` 请求 `tts.audio_url` 并播放空间音频。
4. 音频播放器发出 `playback_finished` 后，组件才发出 `dialogue_completed`，队列中的下一句才会继续。

因此不会出现声音先于字幕，也不会在语音尚未结束时切换下一句。若 `tts.generated=false`、
TTS 被关闭或引擎失败，则跳过等待，文字字幕直接完成；`/tts/synthesize` 仍可用于需要
“先返回文字、之后单独合成”的客户端。

## 文件职责

- `data/tts/characters/mirdo_ja.json`：Mirdo 的 VOICEVOX 音色和情绪参数。
- `data/dialogue/ja_jp/mirdo_opening.json`：日语开场台词。
- `data/runtime/tts/`：按文本和参数哈希缓存的 WAV，不提交到 Git。

新增语言时增加 `data/dialogue/{locale}/`，不修改 Python Provider。
