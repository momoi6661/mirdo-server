from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChatModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.last_messages = None

    def invoke(self, messages):
        self.last_messages = messages
        return _FakeMessage(self.content)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path,
        conversation_db=tmp_path / "conversations.sqlite3",
        chroma_dir=tmp_path / "chroma",
        knowledge_dir=tmp_path / "knowledge",
        api_base_url="http://localhost:11434/v1",
        api_key="",
        chat_model="qwen3",
    )


def _payload() -> dict:
    return {
        "session_id": "outing-test",
        "location": {
            "id": "clinic",
            "name": "街区诊所",
            "description": "小型诊所入口被杂物挡住。",
            "route_hint": "沿东北街区小路进入。",
            "threat_level": 3,
            "loot_bias_tags": ["药品", "绷带"],
            "recommended_tools": ["撬棍", "手电"],
            "ai_exploration_rule": "生成诊所入口、药柜和感染风险叙事。",
            "discoverable": False,
        },
        "loadout": [],
        "time": {"route_minutes": 90, "search_minutes": 35, "total_minutes": 125},
        "available_loot": {
            "药品": ["res://resources/items/painkiller.tres", "res://resources/items/medkit.tres"],
            "绷带": ["res://resources/items/bandage.tres"],
        },
        "unlocked_neighbors": [],
    }


def test_outing_resolve_accepts_empty_loadout_and_filters_loot(tmp_path: Path):
    fake = _FakeChatModel(
        '{"summary":"轻装搜索完成。","experience":["你从侧门进入诊所。","药柜只剩下少量用品。"],'
        '"risk_result":"轻装行动，收益保守。",'
        '"loot":[{"item_path":"res://resources/items/bandage.tres","amount":2,"tag":"绷带"},'
        '{"item_path":"res://resources/items/knife.tres","amount":1,"tag":"非法"}],'
        '"mood":"谨慎"}'
    )
    app = create_app(_settings(tmp_path), chat_model_factory=lambda _resolved: fake)

    with TestClient(app) as client:
        response = client.post("/outing/resolve", json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["summary"] == "轻装搜索完成。"
    assert body["experience"][0] == "你从侧门进入诊所。"
    assert body["loot"] == [{"item_path": "res://resources/items/bandage.tres", "item_name": "bandage", "amount": 2, "tag": "绷带"}]
    assert "ai_exploration_rule" in fake.last_messages[-1][1]


def test_outing_resolve_falls_back_when_model_fails(tmp_path: Path):
    def broken(_resolved):
        raise RuntimeError("offline")

    app = create_app(_settings(tmp_path), chat_model_factory=broken)
    with TestClient(app) as client:
        response = client.post("/outing/resolve", json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["fallback"] is False
    assert body["loot"] == []
    assert body["experience"]
    assert body["error"] == "offline"


def test_outing_prompt_uses_memory_and_world_knowledge(tmp_path: Path):
    fake = _FakeChatModel(
        '{"summary":"回到了温暖的庇护所。","story":"你离开庇护所，记着Mirdo说过老师喜欢罐头汤。外面有丧尸，返程时灯光像家。",'
        '"experience":["从庇护所出发。"],"risk_result":"遇到丧尸后撤离。",'
        '"loot":[{"item_path":"res://resources/items/bandage.tres","amount":1,"tag":"绷带"}]}'
    )
    settings = _settings(tmp_path)
    app = create_app(settings, chat_model_factory=lambda _resolved: fake)

    with TestClient(app) as client:
        store = app.state.memory_store
        store.upsert_memory_fact("outing-test", "player", "likes", "罐头汤", 0.9, 0)
        client.app.state.expedition_orchestrator.rag_retriever = _FakeRetriever([
            {"source": "mirdo_home_and_outside_contrast.md", "text": "外面是危险的丧尸末世，庇护所是Mirdo和老师温馨的小家。"}
        ])
        response = client.post("/outing/resolve", json=_payload())

    assert response.status_code == 200
    flattened = "\n".join(content for _role, content in fake.last_messages)
    assert "player likes: 罐头汤" in flattened
    assert "mirdo_home_and_outside_contrast.md" in flattened
    assert "温馨的小家" in flattened
    assert "外面是危险的丧尸末世" in flattened


class _FakeRetriever:
    def __init__(self, hits):
        self.hits = hits
        self.last_query = ""
        self.last_top_k = 0

    def retrieve(self, query: str, top_k: int = 4):
        self.last_query = query
        self.last_top_k = top_k
        return self.hits


def test_outing_uses_http_json_mode_for_story_generation(tmp_path: Path):
    created = []

    def factory(_resolved):
        raise AssertionError("base factory should not be used for json-mode outing call")

    settings = _settings(tmp_path)
    app = create_app(settings, chat_model_factory=factory)

    class CaptureProvider:
        def build_chat_model(self, request_provider=None, *, max_tokens=None, timeout=None, json_mode=False):
            created.append({"max_tokens": max_tokens, "timeout": timeout, "json_mode": json_mode})
            return _FakeChatModel(
                '{"summary":"完成。","story":"你从庇护所出发，避开丧尸后回到Mirdo等你的家。",'
                '"experience":["出发。"],"risk_result":"安全撤回。",'
                '"loot":[{"item_path":"res://resources/items/bandage.tres","amount":1,"tag":"绷带"}]}'
            )

    with TestClient(app) as client:
        client.app.state.expedition_orchestrator.llm_provider = CaptureProvider()
        response = client.post("/outing/resolve", json=_payload())

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert created
    assert created[0]["json_mode"] is True
    assert created[0]["max_tokens"] == 3200


def test_outing_resolve_accepts_item_id_alias_for_loot_path(tmp_path: Path):
    fake = _FakeChatModel(
        '{"summary":"完成。","story":"你回到庇护所，Mirdo帮你清点药品。","experience":["进入诊所。"],'
        '"risk_result":"安全撤回。",'
        '"loot":[{"item_id":"res://resources/items/bandage.tres","amount":2,"tag":"绷带"}]}'
    )
    app = create_app(_settings(tmp_path), chat_model_factory=lambda _resolved: fake)

    with TestClient(app) as client:
        response = client.post("/outing/resolve", json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["loot"] == [{"item_path": "res://resources/items/bandage.tres", "item_name": "bandage", "amount": 2, "tag": "绷带"}]
