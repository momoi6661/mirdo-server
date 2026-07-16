# TTS 角色声线目录

这里存放角色的**声音配置**，不要把长篇台词放进来。

## 命名规则

- 文件名：`{character_id}_{locale}.json`，全部使用小写 `snake_case`。
- `profile_id` 必须和文件名去掉 `.json` 后一致，例如 `mirdo_ja`。
- `character_id` 使用稳定的内部 ID，不随显示名改变，例如 `mirdo`。
- `speaker_id` 是 VOICEVOX 的风格 ID，调音时只修改这里的角色文件。

默认的 `mirdo_ja` 使用 VOICEVOX `20`（もち子さん / ノーマル）。可选声线配置放在同一目录：雨晴はう `10`、猫使ビィ `58`、琴詠ニア `74`、Voidoll `89`、あんこもん `113`。
这些选项只改 `speaker_id`，公共情绪参数由 Python 默认配置提供，避免 JSON 重复。

## 当前可选声线

| `tts_voice_profile` | 声音 | `speaker_id` |
| --- | --- | ---: |
| `mirdo_ja` / `mirdo_ja_mochiko` | もち子さん / 麻糬子（默认） | 20 |
| `mirdo_ja_bii` | 猫使ビィ / 猫使比伊 | 58 |
| `mirdo_ja_hau` | 雨晴はう / 雨晴羽 | 10 |
| `mirdo_ja_kotone` | 琴詠ニア / 琴咏妮娅 | 74 |
| `mirdo_ja_voidoll` | Voidoll | 89 |
| `mirdo_ja_ankomon` | あんこもん / 红豆萌（普通声线） | 113 |

请求时只需传 profile 名称，例如：

```json
{"use_tts": true, "generate_japanese": true, "tts_voice_profile": "mirdo_ja_bii"}
```
如果以后更换音色，只改这个文件，不改 Chat、Graph 或 Godot 协议。
