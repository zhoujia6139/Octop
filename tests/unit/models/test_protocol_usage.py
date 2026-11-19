"""Wire edge cases that affect authoritative usage rather than presentation."""

import json

import pytest

from octop.infra.models.protocols import UsageCapture, normalize


def test_sse_split_crlf_and_cumulative_usage_is_never_added_twice():
    capture = UsageCapture("chat_completions")
    usage = {
        "prompt_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 3},
        "completion_tokens": 4,
        "completion_tokens_details": {"reasoning_tokens": 2},
    }
    wire = (
        "".join(
            "data: " + json.dumps(event) + "\r\n\r\n"
            for event in [
                {
                    "choices": [{"delta": {"content": "中文"}, "finish_reason": None}],
                    "usage": usage,
                },
                {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage},
                {"choices": [], "usage": usage},
            ]
        )
        + "data: [DONE]\r\n\r\n"
    )
    for char in wire:
        capture.feed(char)
    assert capture.final_usage().model_dump() == {
        "uncached_input": 7,
        "cache_read": 3,
        "cache_write": 0,
        "output": 4,
    }


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": 2.5, "completion_tokens": 1},
        {"prompt_tokens": 2, "completion_tokens": -1},
        {"prompt_tokens": 2, "prompt_tokens_details": {"cached_tokens": 3}, "completion_tokens": 1},
    ],
)
def test_malformed_provider_usage_cannot_settle(usage):
    with pytest.raises(ValueError):
        normalize("chat_completions", usage)


def test_partial_anthropic_usage_is_not_final_and_error_overrides_completion():
    capture = UsageCapture("messages")
    capture.event(
        {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}
    )
    capture.event(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 4},
        }
    )
    assert capture.final_usage() is None
    capture.event({"type": "message_stop"})
    assert capture.final_usage().output == 4
    capture.event({"type": "error", "error": {"type": "overloaded_error"}})
    assert capture.final_usage() is None
