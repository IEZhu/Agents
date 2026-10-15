"""Fixed-size aggregate accounting; never stores labels, prompts or capabilities."""
from dataclasses import dataclass

TIMING_SPANS = ("queue", "startup", "render_tokenize", "source_check", "snapshot_lookup",
                "snapshot_load", "snapshot_restore", "model", "snapshot_save", "snapshot_store")


def timings() -> dict:
    return {"total": 0.0, **{name: 0.0 for name in TIMING_SPANS}, "first_token": None}


@dataclass
class Metrics:
    accepted: int = 0
    dispatched: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    evaluated_input_tokens: int = 0
    reused_input_tokens: int = 0
    output_tokens: int = 0
