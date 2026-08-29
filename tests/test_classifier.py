"""Basic tests for classifier fallback behavior."""

from __future__ import annotations

import asyncio

from classifier.slm_classifier import _heuristic_classification


def test_heuristic_code_detection() -> None:
    """Classifier should tag code-focused prompts as CODE_GEN."""
    result = _heuristic_classification("Write a Rust function with async error handling.")
    assert result.domain == "CODE_GEN"
    assert result.est_input_tokens > 0


def test_heuristic_summary_detection() -> None:
    """Classifier should tag summary prompts as DOC_SUMMARIZATION."""
    result = _heuristic_classification("Summarize this meeting transcript in bullet points.")
    assert result.domain == "DOC_SUMMARIZATION"


def test_async_entrypoint_does_not_raise() -> None:
    """Public classifier coroutine should always return a valid object."""

    async def _run() -> None:
        result = await asyncio.wait_for(
            asyncio.create_task(
                __import__("classifier.slm_classifier", fromlist=["classify_prompt"]).classify_prompt(
                    "Tell me a short story about the sea."
                )
            ),
            timeout=15,
        )
        assert result.domain in {"CODE_GEN", "CREATIVE_TEXT", "MATH_LOGIC", "DOC_SUMMARIZATION", "GENERAL"}

    asyncio.run(_run())
