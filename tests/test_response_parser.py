from app.response_parser import ResponseParser


def test_response_parser_accepts_plain_json():
    parser = ResponseParser()
    parsed = parser.parse(
    '{"dialogue":"你好，老师。","emotion":"平静","action":"Talk","stat_change":{"mood":1},"memory_tags":["greeting"]}'
    )

    assert parsed.ok is True
    assert parsed.dialogue == "你好，老师。"
    assert parsed.emotion == "平静"
    assert parsed.action == "Talk"
    assert parsed.stat_change.mood == 1
    assert parsed.memory_tags == ["greeting"]


def test_response_parser_accepts_fenced_json():
    parser = ResponseParser()
    parsed = parser.parse(
        """```json
        {"dialogue":"收到。","command":"follow_player","command_payload":{"follow_target":"player"}}
        ```"""
    )

    assert parsed.ok is True
    assert parsed.dialogue == "收到。"
    assert parsed.command == "follow_player"
    assert parsed.command_payload["follow_target"] == "player"


def test_response_parser_returns_error_response_for_invalid_json():
    parser = ResponseParser()
    parsed = parser.parse("not json")

    assert parsed.ok is False
    assert parsed.error == "invalid_model_json"
    assert parsed.action == "Idle"
    assert "模型调用失败" in parsed.dialogue
