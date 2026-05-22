from __future__ import annotations

import json
import re
import time
from typing import Any
from uuid import uuid4

from .config import Settings
from .llm_provider import LLMProvider
from .memory.store import MemoryStore
from .rag.retriever import RAGRetriever
from .schemas import ExpeditionLootEntry, ExpeditionRequest, ExpeditionResponse


class ExpeditionOrchestrator:
    MODEL_TIMEOUT_SECONDS = 120.0
    EXPEDITION_MAX_TOKENS = 3200
    EXPEDITION_RETRY_MAX_TOKENS = 1400
    ENABLE_JSON_REPAIR_RETRY = True

    def __init__(
        self,
        *,
        settings: Settings,
        llm_provider: LLMProvider,
        memory_store: MemoryStore | None = None,
        rag_retriever: RAGRetriever | None = None,
        memory_retriever: Any | None = None,
    ) -> None:
        self.settings = settings
        self.llm_provider = llm_provider
        self.memory_store = memory_store
        self.rag_retriever = rag_retriever
        self.memory_retriever = memory_retriever

    def _log_model_output(self, raw_text: str) -> None:
        preview = str(raw_text or "").strip()
        if len(preview) > 2200:
            preview = preview[:2200] + "...<truncated>"
        print("[ExpeditionAI] raw model output begin", flush=True)
        print(preview if preview else "<empty>", flush=True)
        print("[ExpeditionAI] raw model output end", flush=True)

    def _log_parsed_response(self, response: ExpeditionResponse) -> None:
        story_len = len(response.story or "")
        print(
            "[ExpeditionAI] parsed ok=%s title=%s story_chars=%d loot=%d health_damage=%.1f error=%s"
            % (response.ok, response.title, story_len, len(response.loot), response.health_damage, response.error),
            flush=True,
        )

    def resolve(self, request: ExpeditionRequest) -> ExpeditionResponse:
        started = time.perf_counter()
        request, fork_info = self._resolve_timeline_for_write(request)
        user_turn_id = self._record_expedition_turn(request, "user", self._expedition_user_turn_text(request), request.model_dump(mode="json"))
        self._log_request_start(request)
        try:
            raw_text = self._invoke_model_text(request, started)
            self._log_timing("model_text_ready", started)
            self._log_model_output(raw_text)
            try:
                parsed = self._parse_response(raw_text, request)
            except (json.JSONDecodeError, ValueError) as parse_exc:
                self._log_timing(f"parse_failed:{parse_exc.__class__.__name__}", started)
                if not self.ENABLE_JSON_REPAIR_RETRY:
                    raise
                raw_text = self._invoke_json_repair_text(request, raw_text, started)
                self._log_timing("repair_text_ready", started)
                self._log_model_output(raw_text)
                parsed = self._parse_response(raw_text, request)
            self._log_timing("parse_done", started)
            self._log_parsed_response(parsed)
        except Exception as exc:
            self._log_timing(f"failure:{exc.__class__.__name__}", started)
            failed = self._model_failure_response(request, exc)
            assistant_turn_id = self._record_expedition_turn(request, "assistant", failed.summary, failed.model_dump(mode="json"))
            self._finalize_timeline_fields(failed, request, assistant_turn_id, fork_info)
            return failed
        if not parsed.ok or parsed.error:
            parsed.ok = False
            parsed.fallback = False
        assistant_turn_id = self._record_expedition_turn(request, "assistant", parsed.summary or parsed.story, parsed.model_dump(mode="json"))
        self._finalize_timeline_fields(parsed, request, assistant_turn_id, fork_info)
        self._log_timing("resolve_done", started)
        return parsed


    def _resolve_timeline_for_write(self, request: ExpeditionRequest) -> tuple[ExpeditionRequest, dict[str, Any]]:
        checkpoint = self._request_checkpoint_turn_id(request)
        if checkpoint <= 0 or self.memory_store is None:
            return request, {}
        latest_turn_id = self.memory_store.get_latest_turn_id(request.session_id)
        if latest_turn_id <= 0 or checkpoint >= latest_turn_id:
            return request, {}
        forked_session = self._build_branch_session_id(request.session_id)
        self.memory_store.fork_session(request.session_id, checkpoint, forked_session)
        if self.memory_retriever is not None:
            try:
                self.memory_retriever.clear_session_vectors(forked_session)
            except Exception:
                pass
        data = request.model_dump(mode="python")
        data["session_id"] = forked_session
        forked_request = ExpeditionRequest(**data)
        return forked_request, {"forked_from": request.session_id, "forked_at_turn_id": checkpoint}

    def _request_checkpoint_turn_id(self, request: ExpeditionRequest) -> int:
        raw = getattr(request, "ai_checkpoint_turn_id", 0)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def _build_branch_session_id(self, source_session_id: str) -> str:
        base = str(source_session_id or "outing_expedition").strip() or "outing_expedition"
        for _attempt in range(8):
            candidate = f"{base}:branch_{uuid4().hex[:10]}"
            if self.memory_store is None or self.memory_store.get_latest_turn_id(candidate) == 0:
                return candidate
        return f"{base}:branch_{uuid4().hex}"

    def _record_expedition_turn(self, request: ExpeditionRequest, role: str, content: str, payload: dict[str, Any]) -> int:
        if self.memory_store is None:
            return 0
        turn = self.memory_store.add_turn(request.session_id, role, content, payload)
        return int(turn.id)

    def _expedition_user_turn_text(self, request: ExpeditionRequest) -> str:
        return "外出探索：%s" % (request.location.name or request.location.id or "未知地点")

    def _finalize_timeline_fields(self, response: ExpeditionResponse, request: ExpeditionRequest, turn_id: int, fork_info: dict[str, Any]) -> None:
        response.session_id = request.session_id
        response.turn_id = int(turn_id)
        if fork_info:
            response.forked_from = str(fork_info.get("forked_from", ""))
            response.forked_at_turn_id = int(fork_info.get("forked_at_turn_id", 0))

    def _log_request_start(self, request: ExpeditionRequest) -> None:
        loot_count = sum(len(paths) for paths in request.available_loot.values())
        print(
            "[ExpeditionAI] request start place=%s threat=%s loadout=%d loot_paths=%d minutes=%d"
            % (request.location.name, request.location.threat_level, len(request.loadout), loot_count, request.time.total_minutes),
            flush=True,
        )

    def _log_timing(self, stage: str, started: float) -> None:
        print("[ExpeditionAI] timing %-24s %.2fs" % (stage, time.perf_counter() - started), flush=True)


    def _invoke_model_text(self, request: ExpeditionRequest, started: float | None = None) -> str:
        started = started if started is not None else time.perf_counter()
        memory_facts = self._load_memory_facts(request.session_id, limit=12)
        knowledge_hits = self._retrieve_knowledge(request)
        self._log_timing("context_loaded memory=%d knowledge=%d" % (len(memory_facts), len(knowledge_hits)), started)
        messages = self._build_messages(request, memory_facts=memory_facts, knowledge_hits=knowledge_hits)
        self._log_timing("prompt_built", started)
        print("[ExpeditionAI] building chat model...", flush=True)
        chat_model = self.llm_provider.build_chat_model(
            request.provider,
            max_tokens=self.EXPEDITION_MAX_TOKENS,
            timeout=self.MODEL_TIMEOUT_SECONDS,
            json_mode=True,
        )
        self._log_timing("model_built", started)

        print("[ExpeditionAI] invoke start; waiting for complete JSON response...", flush=True)
        self._log_timing("invoke_start", started)
        model_message = chat_model.invoke(messages)
        self._log_timing("invoke_done", started)
        text = self._message_text(model_message)
        if text:
            return text

        finish_reason = self._finish_reason(model_message)
        if finish_reason in {"length", "max_tokens", "stop_length"}:
            print("[ExpeditionAI] empty content with finish_reason=%s, retrying compact prompt..." % finish_reason, flush=True)
            retry_model = self.llm_provider.build_chat_model(
                request.provider,
                max_tokens=self.EXPEDITION_RETRY_MAX_TOKENS,
                timeout=self.MODEL_TIMEOUT_SECONDS,
                json_mode=True,
            )
            retry_messages = self._build_retry_messages(
                request,
                memory_facts=self._load_memory_facts(request.session_id, limit=8),
                knowledge_hits=self._retrieve_knowledge(request, top_k=2),
            )
            self._log_timing("retry_invoke_start", started)
            retry_message = retry_model.invoke(retry_messages)
            self._log_timing("retry_invoke_done", started)
            retry_text = self._message_text(retry_message)
            if retry_text:
                return retry_text
            retry_reason = self._finish_reason(retry_message)
            raise ValueError("empty_model_content_after_retry" + (f":{retry_reason}" if retry_reason else ""))

        raise ValueError("empty_model_content_after_reasoning" + (f":{finish_reason}" if finish_reason else ""))

    def _invoke_json_repair_text(self, request: ExpeditionRequest, bad_text: str, started: float | None = None) -> str:
        started = started if started is not None else time.perf_counter()
        print("[ExpeditionAI] non-json output; retrying strict JSON-only prompt...", flush=True)
        repair_model = self.llm_provider.build_chat_model(
            request.provider,
            max_tokens=self.EXPEDITION_RETRY_MAX_TOKENS,
            timeout=self.MODEL_TIMEOUT_SECONDS,
            json_mode=False,
        )
        self._log_timing("repair_invoke_start", started)
        repair_message = repair_model.invoke(self._build_json_repair_messages(request, bad_text))
        self._log_timing("repair_invoke_done", started)
        text = self._message_text(repair_message)
        if not text:
            raise ValueError("empty_json_repair_content")
        return text


    def _message_text(self, model_message: Any) -> str:
        content = getattr(model_message, "content", "")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    value = part.get("text", part.get("content", ""))
                    if value:
                        parts.append(str(value))
                elif part:
                    parts.append(str(part))
            text = "\n".join(parts).strip()
        else:
            text = str(content or "").strip()

        if not text:
            extra = getattr(model_message, "additional_kwargs", {}) or {}
            value = extra.get("content") or extra.get("text")
            if value:
                text = str(value).strip()
        return text

    def _finish_reason(self, model_message: Any) -> str:
        response_metadata = getattr(model_message, "response_metadata", {}) or {}
        if isinstance(response_metadata, dict):
            reason = response_metadata.get("finish_reason", "")
            if reason:
                return str(reason)
            token_usage = response_metadata.get("token_usage", {})
            if isinstance(token_usage, dict):
                reason = token_usage.get("finish_reason", "")
                if reason:
                    return str(reason)
        additional = getattr(model_message, "additional_kwargs", {}) or {}
        if isinstance(additional, dict):
            reason = additional.get("finish_reason", "")
            if reason:
                return str(reason)
        return ""

    def _compact_error(self, exc: Exception) -> str:
        text = str(exc).strip() or exc.__class__.__name__
        text = text.replace("\n", " ").replace("\r", " ")
        if len(text) > 160:
            text = text[:157] + "..."
        return text

    def _build_retry_messages(
        self,
        request: ExpeditionRequest,
        *,
        memory_facts: list[dict[str, Any]] | None = None,
        knowledge_hits: list[dict[str, Any]] | None = None,
    ) -> list[tuple[str, str]]:
        location = request.location
        loot_paths: list[str] = []
        for paths in request.available_loot.values():
            for path in paths:
                clean = str(path).strip()
                if clean and clean not in loot_paths:
                    loot_paths.append(clean)
        loadout_names = [item.name for item in request.loadout if item.name][:4]
        compact_input = {
            "place": location.name,
            "threat": location.threat_level,
            "rule": location.ai_exploration_rule,
            "loot": loot_paths[:14],
            "loadout": loadout_names,
            "minutes": request.time.total_minutes,
            "memory": self._format_memory_facts(memory_facts or [])[:600],
            "world": self._format_knowledge_hits(knowledge_hits or [])[:900],
        }
        prompt = (
            "只输出JSON对象，不要markdown。字段：ok,title,summary,story,experience,risk_result,loot,discovered_clues,mood,health_damage。"
            "story是最重要字段，绝不能省略；写240到360字连续叙事，不要列表，必须按离开庇护所、靠近地点、搜索、遭遇丧尸/环境意外、取舍、撤离返程展开。"
            "世界基调：外面是危险的丧尸末世；庇护所是老师和Mirdo一起守着的安全据点，因灯光、补给和陪伴而像家一样温暖。"
            "外出故事要形成家的反差：出门前或返程时提到庇护所灯光、门、Mirdo或老师的归处，但不要让Mirdo亲自参战，除非输入明确同行。"
            "外面全是丧尸，探索不一定顺利；允许受伤、武器损坏、消耗物资、失败或空手，但不要死亡，不要生成新庇护所。"
            "experience为4到6个短句；loot由AI按剧情和地点判断：通常3到8项，搜索充分/携带工具合适/地点资源丰富可到10项；受伤、惊动尸群、提前撤退则1到4项；可给食物/水/材料 amount 1到4，武器和大型工具通常 amount 1；严重失败才允许空loot。输入="
            + json.dumps(compact_input, ensure_ascii=False, separators=(",", ":"))
        )
        return [
            ("system", "只输出JSON对象。"),
            ("user", prompt),
        ]

    def _build_json_repair_messages(self, request: ExpeditionRequest, bad_text: str) -> list[tuple[str, str]]:
        valid_paths = list(self._valid_loot_paths(request).keys())[:18]
        compact_input = {
            "location": request.location.model_dump(mode="json"),
            "loadout": [item.model_dump(mode="json") for item in request.loadout[:6]],
            "time": request.time.model_dump(mode="json"),
            "available_loot_paths": valid_paths,
            "bad_output_preview": str(bad_text or "")[:1200],
        }
        prompt = (
            "你上一次输出不是合法JSON。现在必须修正。"
            "只输出一个JSON对象；第一个字符必须是{，最后一个字符必须是}。"
            "不要解释，不要写思考过程，不要markdown，不要代码块。"
            "字段必须包含ok,title,summary,story,experience,risk_result,loot,discovered_clues,mood,health_damage。"
            "story写180到280字连续中文叙事，必须包含：离开庇护所、外面丧尸危险、地点搜索、一次风险或受伤、带回物资、回到老师和Mirdo一起守着的、像家一样温暖的避难所。"
            "loot只能从available_loot_paths选择，格式为[{\"item_path\":路径,\"amount\":1到4,\"tag\":短标签}]。"
            "health_damage为0到35；experience为4到6个短句。输入="
            + json.dumps(compact_input, ensure_ascii=False, separators=(",", ":"))
        )
        return [("system", "只输出合法JSON对象。不要解释。"), ("user", prompt)]

    def _build_messages(
        self,
        request: ExpeditionRequest,
        *,
        memory_facts: list[dict[str, Any]] | None = None,
        knowledge_hits: list[dict[str, Any]] | None = None,
    ) -> list[tuple[str, str]]:
        location = request.location
        loot_paths: list[str] = []
        for paths in request.available_loot.values():
            for path in paths:
                clean = str(path).strip()
                if clean and clean not in loot_paths:
                    loot_paths.append(clean)
        compact_payload = {
            "session_id": request.session_id,
            "place": {
                "id": location.id,
                "name": location.name,
                "desc": self._short(location.description, 120),
                "route": self._short(location.route_hint, 80),
                "threat": location.threat_level,
                "ai_exploration_rule": self._short(location.ai_exploration_rule, 140),
                "notes": [self._short(v, 40) for v in location.detail_notes[:4]],
            },
            "loadout": [
                {
                    "name": item.name,
                    "cat": item.category,
                    "amount": item.amount,
                    "hint": self._short(item.ai_rule_hint, 70),
                }
                for item in request.loadout[:5]
            ],
            "time_min": request.time.total_minutes,
            "loot_paths": loot_paths[:18],
            "memory": self._format_memory_facts(memory_facts or [])[:360],
            "world": self._format_knowledge_hits(knowledge_hits or [])[:520],
        }
        system = (
            "你是外出结算JSON生成器。必须只输出一个合法JSON对象。"
            "禁止解释、禁止思考过程、禁止markdown、禁止代码块。"
            "第一个字符必须是{，最后一个字符必须是}。"
            "所有字符串必须闭合，不能输出JSON外文本。"
        )
        user_prompt = (
            "按输入生成外出结果JSON。字段固定且只允许这些字段："
            "ok,title,summary,story,experience,risk_result,loot,discovered_clues,mood,health_damage。"
            "story为520到780字中文连续叙事，用第二人称你，细节丰富，包含离开庇护所、路上环境、地点搜索、丧尸或环境意外、物资取舍、一次代价、回到老师和Mirdo一起守着的、像家一样温暖的避难所。"
            "summary为20到45字。experience为4到6个短句。risk_result一句话。"
            "loot只能从loot_paths复制路径；每项格式为{\"item_path\":路径,\"amount\":1到4,\"tag\":短标签}。"
            "health_damage为0到35。不要使用item_id/name/path字段替代item_path。"
            "如果受伤/提前撤离，loot 1到4项；普通成功3到8项；不要空loot，除非故事完全失败。"
            "输入="
            + json.dumps(compact_payload, ensure_ascii=False, separators=(",", ":"))
        )
        return [("system", system), ("user", user_prompt)]

    def _short(self, value: Any, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) > limit:
            return text[: max(0, limit - 1)] + "…"
        return text

    def _load_memory_facts(self, session_id: str, limit: int = 12) -> list[dict[str, Any]]:
        if self.memory_store is None:
            return []
        try:
            query = "外出 丧尸 庇护所 Mirdo 老师 补给 回到避难所 喜欢 记得"
            if self.memory_retriever is not None:
                return list(self.memory_retriever.retrieve(session_id, query, top_k=limit))
            return [fact.to_dict() for fact in self.memory_store.search_memory_facts(session_id, query, limit=limit)]
        except Exception:
            return []

    def _retrieve_knowledge(self, request: ExpeditionRequest, top_k: int | None = None) -> list[dict[str, Any]]:
        if self.rag_retriever is None:
            return []
        query_parts = [
            request.location.name,
            request.location.description,
            request.location.ai_exploration_rule,
            "外出 丧尸 避难所 Mirdo 老师 返程 像家一样温暖",
        ]
        query = "\n".join(part for part in query_parts if str(part or "").strip())
        try:
            return list(self.rag_retriever.retrieve(query, top_k=top_k or min(max(int(self.settings.top_k), 1), 4)))
        except Exception:
            return []

    def _format_memory_facts(self, facts: list[dict[str, Any]]) -> str:
        if not facts:
            return "(none)"
        lines: list[str] = []
        for fact in facts[:12]:
            value = str(fact.get("value", "") or "").strip()
            if not value:
                continue
            lines.append(
                "- {subject} {predicate}: {value}".format(
                    subject=str(fact.get("subject", "player") or "player"),
                    predicate=str(fact.get("predicate", "related_to") or "related_to"),
                    value=value,
                )
            )
        return "\n".join(lines) if lines else "(none)"

    def _format_knowledge_hits(self, hits: list[dict[str, Any]]) -> str:
        if not hits:
            return "(none)"
        lines: list[str] = []
        for hit in hits[:4]:
            text = str(hit.get("text", hit.get("content", "")) or "").strip()
            if not text:
                continue
            if len(text) > 650:
                text = text[:647] + "..."
            source = str(hit.get("source", "knowledge") or "knowledge")
            lines.append(f"[{source}] {text}")
        return "\n".join(lines) if lines else "(none)"

    def _parse_response(self, raw_text: str, request: ExpeditionRequest) -> ExpeditionResponse:
        payload = self._extract_json(raw_text)
        if not isinstance(payload, dict):
            raise ValueError("invalid_expedition_json")

        valid_paths = self._valid_loot_paths(request)
        loot_entries: list[ExpeditionLootEntry] = []
        for raw_entry in payload.get("loot", []):
            if isinstance(raw_entry, str):
                item_path = raw_entry.strip()
                amount = 1
                tag = "现场物资"
            elif isinstance(raw_entry, dict):
                item_path = str(raw_entry.get("item_path", raw_entry.get("path", raw_entry.get("item_id", "")))).strip()
                amount = max(1, min(int(raw_entry.get("amount", 1) or 1), 99))
                tag = str(raw_entry.get("tag", "物资") or "物资").strip() or "物资"
            else:
                continue
            if item_path not in valid_paths:
                continue
            loot_entries.append(
                ExpeditionLootEntry(
                    item_path=item_path,
                    item_name=valid_paths.get(item_path, ""),
                    amount=amount,
                    tag=tag,
                )
            )

        experience_raw = payload.get("experience", [])
        experience = [str(v).strip() for v in experience_raw if str(v).strip()] if isinstance(experience_raw, list) else []
        if not experience:
            experience = self._fallback_experience(request)

        return ExpeditionResponse(
            ok=bool(payload.get("ok", True)),
            title=str(payload.get("title", "外出行动报告") or "外出行动报告").strip(),
            summary=str(payload.get("summary", "") or "").strip() or self._fallback_summary(request),
            story=str(payload.get("story", payload.get("narrative", "")) or "").strip() or self._fallback_story(request),
            experience=experience[:7],
            risk_result=str(payload.get("risk_result", "") or "").strip() or self._fallback_risk(request),
            loot=loot_entries[:12],
            discovered_clues=[str(v).strip() for v in payload.get("discovered_clues", []) if str(v).strip()]
            if isinstance(payload.get("discovered_clues", []), list)
            else [],
            mood=str(payload.get("mood", "冷静") or "冷静").strip(),
            health_damage=max(0.0, min(float(payload.get("health_damage", payload.get("damage", 0.0)) or 0.0), 35.0)),
            fallback=False,
            error=str(payload.get("error", "") or "").strip(),
        )

    def _extract_json(self, text: str) -> dict[str, Any]:
        source = str(text or "").strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", source, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            source = fenced.group(1).strip()
        else:
            start = source.find("{")
            end = source.rfind("}")
            if start >= 0 and end > start:
                source = source[start : end + 1]
        data = json.loads(source)
        if not isinstance(data, dict):
            raise ValueError("json_root_not_object")
        return data

    def _local_fallback(self, request: ExpeditionRequest) -> ExpeditionResponse:
        loot = self._fallback_loot(request)
        return ExpeditionResponse(
            ok=True,
            title="外出行动报告",
            summary=self._fallback_summary(request),
            story=self._fallback_story(request),
            experience=self._fallback_experience(request),
            risk_result=self._fallback_risk(request),
            loot=loot,
            discovered_clues=[],
            mood="谨慎",
            fallback=True,
            error="model_call_failed",
        )

    def _model_failure_response(self, request: ExpeditionRequest, exc: Exception) -> ExpeditionResponse:
        error = self._compact_error(exc)
        return ExpeditionResponse(
            ok=False,
            title="外出 AI 结算失败",
            summary="后端服务已连接，但模型没有返回可用的外出结算。",
            experience=[
                f"已连接后端并提交 {request.location.name or '目标地点'} 的外出规则。",
                "模型请求失败或超时，本次行动尚未写入物资和地图进展。",
                "请检查 API 配置、模型名称或稍后重试。",
            ],
            risk_result="后端 AI 未完成结算，行动保持在出发前状态。",
            loot=[],
            discovered_clues=[],
            mood="中断",
            health_damage=0.0,
            fallback=False,
            error=error,
        )

    def _fallback_story(self, request: ExpeditionRequest) -> str:
        location = request.location
        carried = "、".join(item.name for item in request.loadout[:3] if item.name) if request.loadout else "没有额外携带物"
        return (
            f"离开庇护所时，门缝里还留着一点暖光，Mirdo在物资架旁小声提醒你早点回来。"
            f"外面的街道比屋内冷得多，远处丧尸的拖行声沿着{location.route_hint or '旧道路'}断断续续传来。"
            f"你靠近{location.name or '目标地点'}，先确认退路，再按地点线索搜索入口和安全角落；本次携带：{carried}。"
            f"当杂物后方传来碰撞声时，你没有恋战，只把能确认的补给收好，沿原路撤离。"
            f"重新看见庇护所的门和灯光时，外面的血腥味才被隔在身后；这里不是普通的家，却因为有Mirdo等着老师，显得像家一样安心。"
        )

    def _fallback_summary(self, request: ExpeditionRequest) -> str:
        location = request.location
        if request.loadout:
            return f"你按计划搜索了{location.name}，在丧尸靠近前带着补给回到庇护所。"
        return f"你轻装进入{location.name}外围，保守搜索后回到Mirdo等你的庇护所。"

    def _fallback_experience(self, request: ExpeditionRequest) -> list[str]:
        location = request.location
        carried = "、".join(item.name for item in request.loadout[:3]) if request.loadout else "没有携带辅助物资"
        return [
            f"从温暖的庇护所出发，沿着{location.route_hint or '旧道路'}靠近目标，入口处先停下听丧尸动静。",
            f"本次携带：{carried}。搜索时优先检查了和地点线索相符的区域。",
            f"威胁等级约为 {location.threat_level}/5，行动没有深入最危险的房间。",
            "返程前把能确认的物资打包，沿原路线回到老师和 Mirdo 一起守着的避难所。",
        ]

    def _fallback_risk(self, request: ExpeditionRequest) -> str:
        threat = request.location.threat_level
        has_weapon = any(item.category == "weapon" for item in request.loadout)
        has_medical = any(item.category == "medical" for item in request.loadout)
        if threat >= 4 and not has_weapon:
            return "高威胁且缺少防身装备，搜索路线主动收缩。"
        if threat >= 3 and not has_medical:
            return "缺少医疗兜底，遇到割伤和粉尘风险时提前撤离。"
        if not request.loadout:
            return "轻装探索降低准备成本，但收益和深入程度都偏保守。"
        return "携带物资覆盖了主要风险，行动按计划完成并安全回到庇护所。"

    def _fallback_loot(self, request: ExpeditionRequest) -> list[ExpeditionLootEntry]:
        valid_paths = self._valid_loot_paths(request)
        if not valid_paths:
            return []
        preferred: list[str] = []
        for tag in request.location.loot_bias_tags or ["default"]:
            for path in request.available_loot.get(tag, []):
                if path in valid_paths and path not in preferred:
                    preferred.append(path)
        if not preferred:
            preferred = list(valid_paths.keys())
        base_count = 1 if not request.loadout else 2
        if request.location.threat_level >= 4 and not request.loadout:
            base_count = 2
        result: list[ExpeditionLootEntry] = []
        for path in preferred[:base_count]:
            result.append(ExpeditionLootEntry(item_path=path, item_name=valid_paths[path], amount=2 if any(key in path for key in ("can_soup", "water_bottle", "energy_bar")) else 1, tag="现场线索"))
        return result

    def _valid_loot_paths(self, request: ExpeditionRequest) -> dict[str, str]:
        result: dict[str, str] = {}
        for paths in request.available_loot.values():
            for path in paths:
                clean = str(path).strip()
                if clean:
                    result[clean] = clean.rsplit("/", 1)[-1].removesuffix(".tres")
        return result


