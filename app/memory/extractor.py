from __future__ import annotations

import re
from typing import Any


class MemoryExtractor:
    def extract(self, player_text: str) -> list[dict[str, Any]]:
        text = str(player_text or "").strip()
        if not text:
            return []

        facts: list[dict[str, Any]] = []
        facts.extend(self._extract_name(text))
        facts.extend(self._extract_preferences(text, "dislikes", [r"不喜欢", r"讨厌"]))
        facts.extend(self._extract_preferences(text, "likes", [r"喜欢", r"爱吃", r"爱喝"]))
        facts.extend(self._extract_expedition_wants(text))
        facts.extend(self._extract_remember_notes(text))
        return self._dedupe(facts)

    def extract_model_updates(self, updates: Any) -> list[dict[str, Any]]:
        if not isinstance(updates, list):
            return []
        facts: list[dict[str, Any]] = []
        for item in updates:
            if not isinstance(item, dict):
                continue
            value = self._clean_value(item.get("value", ""))
            if not value:
                continue
            facts.append(
                {
                    "subject": self._clean_token(item.get("subject", "")) or "player",
                    "predicate": self._clean_token(item.get("predicate", "")) or "related_to",
                    "value": value,
                    "confidence": self._clamp_confidence(item.get("confidence", 0.75)),
                }
            )
        return self._dedupe(facts)

    def _extract_name(self, text: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for pattern in [r"(?:我叫|叫我|我的名字是)\s*([^，。,.！!\s]{1,16})"]:
            for match in re.finditer(pattern, text):
                value = self._clean_value(match.group(1))
                if value:
                    facts.append({"subject": "player", "predicate": "name", "value": value, "confidence": 0.9})
        return facts

    def _extract_preferences(self, text: str, predicate: str, markers: list[str]) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        marker_pattern = "|".join(markers)
        pattern = rf"(?:我)?(?:{marker_pattern})\s*([^，。,.！!；;\n]{{1,24}})"
        for match in re.finditer(pattern, text):
            value = self._clean_value(match.group(1))
            if value:
                facts.append({"subject": "player", "predicate": predicate, "value": value, "confidence": 0.82})
        return facts

    def _extract_remember_notes(self, text: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        pattern = r"(?:记住|记得)\s*(?:我)?([^，。,.！!；;\n]{2,32})"
        for match in re.finditer(pattern, text):
            value = self._clean_value(match.group(1))
            if not value:
                continue
            if value.startswith("叫"):
                value = self._clean_value(value[1:])
                if value:
                    facts.append({"subject": "player", "predicate": "name", "value": value, "confidence": 0.86})
                continue
            if value.startswith("喜欢"):
                value = self._clean_value(value[2:])
                if value:
                    facts.append({"subject": "player", "predicate": "likes", "value": value, "confidence": 0.86})
                continue
            facts.append({"subject": "player", "predicate": "note", "value": value, "confidence": 0.72})
        return facts

    def _extract_expedition_wants(self, text: str) -> list[dict[str, Any]]:
        """提取玩家明确说过的寻找目标，供外出 GM 排定搜索重点。

        这不是把模型的猜测写成事实；只有“想找/想要/需要/缺少”等明确表达才会
        进入 ``wants``，下一次外出仍会把原始对话和来源回合一起交给 GM 判断。
        """
        facts: list[dict[str, Any]] = []
        pattern = r"(?:我)?(?:想找|想要|想拿|需要|缺少|缺|寻找|最好(?:再)?拿|再拿)\s*([^，。,.！!；;\n]{1,24})"
        for match in re.finditer(pattern, text):
            value = self._clean_value(match.group(1))
            value = re.sub(r"^(?:去|找|拿|一个|一些|一份|点)\s*", "", value).strip()
            if not value or value in {"帮助", "休息", "出去", "回家"}:
                continue
            facts.append({"subject": "player", "predicate": "wants", "value": value, "confidence": 0.78})
        return facts

    def _dedupe(self, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str, str]] = set()
        result: list[dict[str, Any]] = []
        for fact in facts:
            key = (str(fact["subject"]), str(fact["predicate"]), str(fact["value"]))
            if key in seen:
                continue
            seen.add(key)
            result.append(fact)
        return result

    @staticmethod
    def _clean_token(value: Any) -> str:
        text = str(value or "").strip()
        return re.sub(r"\s+", "_", text)[:40]

    @staticmethod
    def _clean_value(value: Any) -> str:
        text = str(value or "").strip()
        text = re.sub(r"^(我|会|是|：|:)+", "", text).strip()
        text = text.strip(" ，。,.！!；;")
        return text[:80]

    @staticmethod
    def _clamp_confidence(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.75
        return max(0.0, min(number, 1.0))
