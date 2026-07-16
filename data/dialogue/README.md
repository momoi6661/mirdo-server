# 语言台词目录

这里存放**语言和场景台词**，与 `data/tts/characters` 的声线配置分离。

## 命名规则

- 语言目录：使用小写 locale，例如 `ja_jp`、`zh_cn`、`en_us`。
- 文件名：`{character_id}_{scene_id}.json`，例如 `mirdo_opening.json`。
- `line_id`：`{scene_id}_{序号}`，例如 `opening_001`。
- 台词文件只保存文本、情绪和声线 ID，不保存 WAV；音频由 TTS 接口按需生成并缓存。

这样以后增加中文或英文时，只需增加新的 locale 文件夹，不改 TTS Provider。
