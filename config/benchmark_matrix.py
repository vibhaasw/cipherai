"""Static benchmark-driven domain-to-model routing matrix."""

from __future__ import annotations

# This matrix is intentionally static so teams can tweak rankings without changing router logic.
BENCHMARK_MATRIX: dict[str, list[dict]] = {
    "CODE_GEN": [
        {"provider": "mistral", "model": "mistral-small-latest", "key_id": "mistral_key_code1", "rank": 1},
        {"provider": "groq", "model": "llama-3.1-8b-instant", "key_id": "groq_key1", "rank": 2},
        {"provider": "gemini", "model": "gemini-1.5-pro", "key_id": "gemini_key_code1", "rank": 3},
    ],
    "CREATIVE_TEXT": [
        {"provider": "mistral", "model": "mistral-large-latest", "key_id": "mistral_key1", "rank": 1},
        {"provider": "gemini", "model": "gemini-1.5-pro", "key_id": "gemini_key1", "rank": 2},
        {"provider": "groq", "model": "llama-3.1-70b-versatile", "key_id": "groq_key4", "rank": 3},
    ],
    "MATH_LOGIC": [
        {"provider": "groq", "model": "deepseek-r1-distill-llama-70b", "key_id": "groq_key5", "rank": 1},
        {"provider": "gemini", "model": "gemini-1.5-pro", "key_id": "gemini_key2", "rank": 2},
        {"provider": "mistral", "model": "mistral-medium-latest", "key_id": "mistral_key2", "rank": 3},
    ],
    "DOC_SUMMARIZATION": [
        {"provider": "gemini", "model": "gemini-1.5-pro", "key_id": "gemini_key3", "rank": 1},
        {"provider": "mistral", "model": "mistral-large-latest", "key_id": "mistral_key3", "rank": 2},
        {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_id": "groq_key6", "rank": 3},
    ],
    "GENERAL": [
        {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_id": "groq_key7", "rank": 1},
        {"provider": "gemini", "model": "gemini-1.5-flash", "key_id": "gemini_key4", "rank": 2},
        {"provider": "mistral", "model": "mistral-small-latest", "key_id": "mistral_key4", "rank": 3},
    ],
}
