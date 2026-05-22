from app.character_ai.behavior_planner import CharacterBehaviorPlanner
from app.schemas import ChatRequest, ChatResponse


def _mirdo_request(text: str) -> ChatRequest:
    return ChatRequest(
        player_text=text,
        context={
            "npc": {
                "name": "Mirdo",
                "available_body_actions": [
                    "idle_normal",
                    "listen",
                    "walk",
                    "work_count_supplies",
                    "work_check_shelf",
                    "tiny_wave",
                ],
                "available_expressions": ["neutral", "joy", "surprised", "sorrow"],
                "available_visemes": ["aa", "ih", "ou", "E", "oh"],
            },
            "perception": {
                "nearby_objects": [
                    {
                        "id": "food_cabinet_runtime",
                        "name": "食品柜",
                        "type": "storage",
                        "description": "存放食物和饮水补给的柜子。",
                        "tags": ["storage", "food", "supplies"],
                        "actions": ["inspect", "open"],
                    }
                ]
            },
            "known_nav_points": [
                {
                    "id": "food_cabinet_1_approach",
                    "name": "食品柜1号检查点",
                    "type": "supplies",
                    "description": "食品柜前方的站位，适合清点食物。",
                    "tags": ["storage", "food", "supplies", "cabinet"],
                    "action_options": ["work_count_supplies", "work_take_item"],
                    "expression_options": ["neutral", "fun"],
                }
            ],
        },
    )


def test_behavior_planner_turns_cabinet_request_into_go_to_object():
    planner = CharacterBehaviorPlanner()
    request = _mirdo_request("去看看食物柜")
    response = ChatResponse(dialogue="好呀，我看看。", emotion="好奇", expression="", action="Talk")

    finalized = planner.finalize_response(request, response)

    assert finalized.command == "go_to_object"
    assert finalized.command_payload == {"target_object": "food_cabinet_runtime", "marker_role": "approach"}
    assert finalized.action == "work_count_supplies"
    assert finalized.expression == "surprised"
    assert "老师" in finalized.dialogue
    assert "队长" not in finalized.dialogue


def test_behavior_planner_follow_and_stop_commands():
    planner = CharacterBehaviorPlanner()

    follow = planner.finalize_response(
        _mirdo_request("跟着我"),
        ChatResponse(dialogue="好。", emotion="乖巧", expression="joy", action="idle_normal"),
    )
    assert follow.command == "follow_player"
    assert follow.command_payload["follow_target"] == "player"
    assert follow.action == "walk"

    stop = planner.finalize_response(
        _mirdo_request("你先别跟着我"),
        ChatResponse(dialogue="好。", emotion="温和", expression="joy", action="walk"),
    )
    assert stop.command == "stop_follow"
    assert stop.command_payload == {}
    assert stop.action == "idle_normal"


def test_behavior_planner_restricts_action_expression_and_visemes():
    planner = CharacterBehaviorPlanner()
    response = planner.finalize_response(
        _mirdo_request("你好"),
        ChatResponse(
            dialogue="队长，我在。",
            emotion="开心",
            expression="bad_expression",
            action="bad_action",
            visemes="aa、bad、ih、xx、ou",
        ),
    )

    assert response.action in {"idle_normal", "listen", "walk", "work_count_supplies", "work_check_shelf", "tiny_wave"}
    assert response.expression == "joy"
    assert response.visemes == "aa、ih、ou"
    assert "老师" in response.dialogue
    assert "队长" not in response.dialogue


def test_behavior_planner_model_failure_can_still_return_local_object_command():
    planner = CharacterBehaviorPlanner()
    response = planner.local_fallback_response(_mirdo_request("打开食物柜看看"))

    assert response is not None
    assert response.fallback is True
    assert response.command == "go_to_object"
    assert response.command_payload == {"target_object": "food_cabinet_runtime", "marker_role": "open"}
    assert response.action == "work_count_supplies"


def test_behavior_planner_real_outing_return_does_not_emit_movement_command():
    planner = CharacterBehaviorPlanner()
    request = _mirdo_request("我回来了")
    request.context["event"] = "real_outing_return"
    request.context["real_outing"] = True
    request.context["location_name"] = "超市"
    request.context["health_damage"] = 4
    response = ChatResponse(
        dialogue="欢迎回来，老师。",
        emotion="开心又关心",
        expression="joy",
        action="walk",
        command="follow_player",
        command_payload={"follow_target": "player"},
    )

    finalized = planner.finalize_response(request, response)

    assert finalized.command == ""
    assert finalized.command_payload == {}
    assert finalized.action in {"tiny_wave", "listen", "idle_normal", "walk", "work_count_supplies", "work_check_shelf"}
    assert finalized.action != "walk"
    assert finalized.expression == "joy"


def test_behavior_planner_answers_hunger_question_from_stats():
    planner = CharacterBehaviorPlanner()
    request = _mirdo_request("你饿不饿")
    request.npc_stats.hunger = 82
    response = ChatResponse(dialogue="老师，我听到啦。要我检查补给，还是陪你看看周围？", emotion="开心", expression="joy", action="listen")

    finalized = planner.finalize_response(request, response)

    assert "听到啦" not in finalized.dialogue
    assert "不太饿" in finalized.dialogue
    assert finalized.command == ""
    assert finalized.action == "listen"


def test_behavior_planner_answers_low_hunger_as_hungry():
    planner = CharacterBehaviorPlanner()
    request = _mirdo_request("Mirdo，你饿吗")
    request.npc_stats.hunger = 18
    response = ChatResponse(dialogue="嗯？", emotion="", expression="", action="Talk")

    finalized = planner.finalize_response(request, response)

    assert "有点饿" in finalized.dialogue
    assert finalized.expression == "sorrow"


def test_behavior_planner_answers_thirst_question_from_stats():
    planner = CharacterBehaviorPlanner()
    request = _mirdo_request("你渴不渴")
    request.npc_stats.thirst = 22
    response = ChatResponse(dialogue="老师，我听到啦。", emotion="", expression="", action="Talk")

    finalized = planner.finalize_response(request, response)

    assert "有点渴" in finalized.dialogue
    assert "听到啦" not in finalized.dialogue


def test_behavior_planner_answers_tired_question_from_energy_context():
    planner = CharacterBehaviorPlanner()
    request = _mirdo_request("你累不累")
    request.context["resource_stats"] = {"energy": 28, "mood": 65}
    response = ChatResponse(dialogue="老师，我听到啦。", emotion="", expression="", action="Talk")

    finalized = planner.finalize_response(request, response)

    assert "有点累" in finalized.dialogue
    assert finalized.expression == "sorrow"
