from dataclasses import replace

from src.local_inference.models import SourceFile
from src.local_inference.render import messages
from tests.test_local_inference_context import setup


def test_framing_preserves_original_source_and_task_without_changing_prefix(setup):
    from src.local_inference.models import ContextCreateRequest
    registry, client, *_ = setup
    data = registry.create(client, ContextCreateRequest(common_system="rules", shared_context="shared\ntext"))
    context = registry.contexts[data["context_id"]].captured
    raw = 'line1\nline2\t"quoted"\r\nλ'
    source = SourceFile("source.py", "a" * 64, raw, len(raw.encode()), 1, 2, 3)
    captured = replace(context, sources=(source,))
    first, second = messages(captured, "task\nA"), messages(captured, "task\nB")
    assert first[:2] == second[:2]
    assert raw in first[1]["content"] and "shared\ntext" in first[1]["content"]
    assert first[-1] == {"role": "user", "content": "task\nA"}
    assert data["context_id"] not in first[1]["content"]
    assert data["access_token"] not in first[1]["content"]
