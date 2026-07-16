from app.memory.extractor import MemoryExtractor


def test_memory_extractor_reads_player_name_and_preferences():
    extractor = MemoryExtractor()

    facts = extractor.extract("我叫刘队，记住我喜欢罐头汤，不喜欢苦咖啡。")

    assert {"subject": "player", "predicate": "name", "value": "刘队", "confidence": 0.9} in facts
    assert {"subject": "player", "predicate": "likes", "value": "罐头汤", "confidence": 0.82} in facts
    assert {"subject": "player", "predicate": "dislikes", "value": "苦咖啡", "confidence": 0.82} in facts


def test_memory_extractor_reads_explicit_expedition_target():
    facts = MemoryExtractor().extract("这次外出我想找绷带，最好再拿一瓶水。")

    assert {"subject": "player", "predicate": "wants", "value": "绷带", "confidence": 0.78} in facts
    assert {"subject": "player", "predicate": "wants", "value": "一瓶水", "confidence": 0.78} in facts


def test_memory_extractor_normalizes_model_memory_updates():
    extractor = MemoryExtractor()

    facts = extractor.extract_model_updates(
        [
            {"subject": "player", "predicate": "likes", "value": "清水", "confidence": 2.0},
            {"subject": "", "predicate": "", "value": "夜间巡逻"},
            {"value": ""},
        ]
    )

    assert facts == [
        {"subject": "player", "predicate": "likes", "value": "清水", "confidence": 1.0},
        {"subject": "player", "predicate": "related_to", "value": "夜间巡逻", "confidence": 0.75},
    ]
