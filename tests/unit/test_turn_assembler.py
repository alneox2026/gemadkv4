from services.agent_gateway_v3.app.services.turn_assembler import TurnAssembler


def test_turn_assembler_collects_text_and_usage() -> None:
    assembler = TurnAssembler()
    assembler.add_event(
        {
            "usage_metadata": {
                "prompt_token_count": 10,
                "candidates_token_count": 8,
                "thoughts_token_count": 3,
                "total_token_count": 21,
            }
        }
    )
    assembler.add_text("hello")
    assembler.add_text(" world")

    assert assembler.reply_text() == "hello world"
    assert assembler.usage["total_token_count"] == 21
    assert assembler.usage["token_counts"]["total_token_count"] == 21
    assert assembler.usage["billable_tokens"] == {
        "input_text_image_video": 10,
        "input_audio": 0,
        "output_including_thinking": 11,
    }
    assert assembler.usage["estimated_cost_usd"] == 0.00004875
    assert assembler.raw_events == [
        {
            "usage_metadata": {
                "prompt_token_count": 10,
                "candidates_token_count": 8,
                "thoughts_token_count": 3,
                "total_token_count": 21,
            }
        }
    ]


def test_turn_assembler_captures_provider_invocation_id() -> None:
    assembler = TurnAssembler()
    assembler.add_event({"invocationId": "invocation-123"})

    assert assembler.provider_invocation_id == "invocation-123"


def test_turn_assembler_applies_regional_pricing_from_region() -> None:
    assembler = TurnAssembler(model_name="gemini-3.8-flash", region="us-central1")
    assembler.add_event(
        {
            "usage_metadata": {
                "prompt_token_count": 10,
                "candidates_token_count": 8,
                "thoughts_token_count": 3,
                "total_token_count": 21,
            }
        }
    )
    assert assembler.usage["pricing_model"] == "gemini-3.8-flash"
    assert assembler.usage["pricing_catalog_key"] == "gemini-3.8-flash-regional"
    assert assembler.usage["endpoint_location"] == "us-central1"
    assert assembler.usage["pricing"]["input_text_image_video"] == 0.825
    assert assembler.usage["pricing"]["output_including_thinking"] == 4.125
    # 10 * 0.825 / 1M = 0.00000825; 11 * 4.125 / 1M = 0.000045375; total = 0.000053625
    assert assembler.usage["estimated_cost_usd"] == 0.000053625

