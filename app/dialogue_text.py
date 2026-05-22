from __future__ import annotations

import re


ORDERED_MESSAGE_MARKERS = ("第", "随后：", "继续：", "补充：")
CORRECTION_MARKERS = ("不对", "不是", "等等", "先别", "别去", "改成", "算了", "不要", "先陪")


def extract_ordered_player_messages(player_text: str) -> list[str]:
    text = str(player_text or "").strip()
    if not text:
        return []
    if "玩家连续输入" not in text and "AI Agent" not in text:
        return []
    parts: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        content = _extract_ordered_line_content(line)
        if content:
            parts.append(content)
    return parts


def compact_player_query(player_text: str) -> str:
    parts = extract_ordered_player_messages(player_text)
    if not parts:
        return str(player_text or "").strip()
    return "\n".join(parts)


def effective_player_intent_text(player_text: str) -> str:
    parts = extract_ordered_player_messages(player_text)
    if not parts:
        return str(player_text or "").strip()
    if len(parts) == 1:
        return parts[0]
    if any(_has_correction_marker(part) for part in parts[1:]):
        return parts[-1]
    return "\n".join(parts)


def memory_extraction_text(player_text: str) -> str:
    parts = extract_ordered_player_messages(player_text)
    if not parts:
        return str(player_text or "").strip()
    if len(parts) == 1:
        return parts[0]
    if any(_has_correction_marker(part) for part in parts[1:]):
        return parts[-1]
    return "\n".join(parts)


def _extract_ordered_line_content(line: str) -> str:
    for prefix in ("随后：", "继续：", "补充："):
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    match = re.match(r"^第\d+句：(.+)$", line)
    if match:
        return match.group(1).strip()
    return ""


def _has_correction_marker(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in CORRECTION_MARKERS)
