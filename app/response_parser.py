from __future__ import annotations

import json
import re
from typing import Any

from .schemas import ChatResponse, NpcStats


class ResponseParser:
    def parse(self, raw_text: str, *, session_id: str = "default_session", turn_id: int = 0) -> ChatResponse:
        text = str(raw_text or "").strip()
        if not text:
            return self._error("empty_model_content", session_id=session_id, turn_id=turn_id)

        json_text = self._extract_json_text(text)
        try:
            payload = json.loads(json_text)
        except json.JSONDecodeError:
            return self._error("invalid_model_json", session_id=session_id, turn_id=turn_id)
        if not isinstance(payload, dict):
            return self._error("invalid_model_json", session_id=session_id, turn_id=turn_id)

        dialogue = str(payload.get("dialogue", "")).strip()
        if not dialogue:
            return self._error("missing_dialogue", session_id=session_id, turn_id=turn_id)

        stat_change_raw = payload.get("stat_change", {})
        stat_change = stat_change_raw if isinstance(stat_change_raw, dict) else {}
        command_payload_raw = payload.get("command_payload", {})
        command_payload = command_payload_raw if isinstance(command_payload_raw, dict) else {}
        tags_raw = payload.get("memory_tags", [])
        memory_tags = [str(tag).strip() for tag in tags_raw if str(tag).strip()] if isinstance(tags_raw, list) else []
        memory_updates_raw = payload.get("memory_updates", [])
        memory_updates = memory_updates_raw if isinstance(memory_updates_raw, list) else []

        return ChatResponse(
            ok=bool(payload.get("ok", True)),
            dialogue=dialogue,
            emotion=str(payload.get("emotion", "平静")).strip() or "平静",
            expression=str(payload.get("expression", "")).strip(),
            action=str(payload.get("action", "Idle")).strip() or "Idle",
            command=str(payload.get("command", "")).strip(),
            command_payload=command_payload,
            visemes=str(payload.get("visemes", "")).strip(),
            viseme_sequence=str(payload.get("viseme_sequence", "")).strip(),
            stat_change=NpcStats(**stat_change),
            memory_tags=memory_tags,
            session_id=session_id,
            turn_id=turn_id,
            used_knowledge=[],
            used_memory=[],
            memory_updates=memory_updates,
            error=str(payload.get("error", "")).strip(),
        )

    def _extract_json_text(self, text: str) -> str:
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            return fenced.group(1).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return text[start : end + 1]
        return text

    def _error(self, error: str, *, session_id: str, turn_id: int) -> ChatResponse:
        return ChatResponse(
            ok=False,
            error=error,
            dialogue=f"模型调用失败：{error}",
            emotion="error",
            expression="sorrow",
            action="Idle",
            command="",
            command_payload={},
            visemes="",
            viseme_sequence="",
            stat_change=NpcStats(),
            memory_tags=["model_error"],
            session_id=session_id,
            turn_id=turn_id,
        )
