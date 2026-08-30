"""Static benchmark-driven domain-to-model routing matrix."""

from __future__ import annotations

# This matrix is intentionally static so teams can tweak rankings without changing router logic.
# NOTE: mistral-large-latest returns 403 on free-tier keys without active billing — using mistral-small-latest until upgraded.
BENCHMARK_MATRIX: dict[str, list[dict]] = {
    "CODE_GEN": [
        {"provider": "mistral", "model": "mistral-small-latest", "key_id": "mistral_key_code1", "credential_ref": "MISTRAL_API_KEY", "rank": 1},
        {"provider": "groq", "model": "qwen/qwen3.8-27b", "key_id": "groq_key1", "credential_ref": "GROQ_API_KEY", "rank": 2},
        {"provider": "gemini", "model": "gemini-3.6-flash", "key_id": "gemini_key_code1", "credential_ref": "GEMINI_API_KEY", "rank": 3},
        {"provider": "groq_2", "model": "qwen/qwen3.8-27b", "key_id": "groq_key1_b", "credential_ref": "GROQ_API_KEY_2", "rank": 4},
        {"provider": "mistral_2", "model": "mistral-small-latest", "key_id": "mistral_key_code1_b", "credential_ref": "MISTRAL_API_KEY_2", "rank": 5},
        {"provider": "gemini_2", "model": "gemini-3.6-flash", "key_id": "gemini_key_code1_b", "credential_ref": "GEMINI_API_KEY_2", "rank": 6},
    ],
    "CREATIVE_TEXT": [
        {"provider": "mistral", "model": "mistral-small-latest", "key_id": "mistral_key1", "credential_ref": "MISTRAL_API_KEY", "rank": 1},
        {"provider": "gemini", "model": "gemini-3.6-flash", "key_id": "gemini_key1", "credential_ref": "GEMINI_API_KEY", "rank": 2},
        {"provider": "groq", "model": "qwen/qwen3.8-27b", "key_id": "groq_key4", "credential_ref": "GROQ_API_KEY", "rank": 3},
        {"provider": "mistral_2", "model": "mistral-small-latest", "key_id": "mistral_key1_b", "credential_ref": "MISTRAL_API_KEY_2", "rank": 4},
        {"provider": "gemini_2", "model": "gemini-3.6-flash", "key_id": "gemini_key1_b", "credential_ref": "GEMINI_API_KEY_2", "rank": 5},
        {"provider": "groq_2", "model": "qwen/qwen3.8-27b", "key_id": "groq_key4_b", "credential_ref": "GROQ_API_KEY_2", "rank": 6},
    ],
    "MATH_LOGIC": [
        {"provider": "groq", "model": "qwen/qwen3.8-27b", "key_id": "groq_key5", "credential_ref": "GROQ_API_KEY", "rank": 1},
        {"provider": "gemini", "model": "gemini-3.6-flash", "key_id": "gemini_key2", "credential_ref": "GEMINI_API_KEY", "rank": 2},
        {"provider": "mistral", "model": "mistral-medium-latest", "key_id": "mistral_key2", "credential_ref": "MISTRAL_API_KEY", "rank": 3},
        {"provider": "groq_2", "model": "qwen/qwen3.8-27b", "key_id": "groq_key5_b", "credential_ref": "GROQ_API_KEY_2", "rank": 4},
        {"provider": "gemini_2", "model": "gemini-3.6-flash", "key_id": "gemini_key2_b", "credential_ref": "GEMINI_API_KEY_2", "rank": 5},
        {"provider": "mistral_2", "model": "mistral-medium-latest", "key_id": "mistral_key2_b", "credential_ref": "MISTRAL_API_KEY_2", "rank": 6},
    ],
    "DOC_SUMMARIZATION": [
        {"provider": "gemini", "model": "gemini-3.6-flash", "key_id": "gemini_key3", "credential_ref": "GEMINI_API_KEY", "rank": 1},
        {"provider": "mistral", "model": "mistral-small-latest", "key_id": "mistral_key3", "credential_ref": "MISTRAL_API_KEY", "rank": 2},
        {"provider": "groq", "model": "qwen/qwen3.8-27b", "key_id": "groq_key6", "credential_ref": "GROQ_API_KEY", "rank": 3},
        {"provider": "gemini_2", "model": "gemini-3.6-flash", "key_id": "gemini_key3_b", "credential_ref": "GEMINI_API_KEY_2", "rank": 4},
        {"provider": "mistral_2", "model": "mistral-small-latest", "key_id": "mistral_key3_b", "credential_ref": "MISTRAL_API_KEY_2", "rank": 5},
        {"provider": "groq_2", "model": "qwen/qwen3.8-27b", "key_id": "groq_key6_b", "credential_ref": "GROQ_API_KEY_2", "rank": 6},
    ],
    "GENERAL": [
        {"provider": "groq", "model": "qwen/qwen3.8-27b", "key_id": "groq_key7", "credential_ref": "GROQ_API_KEY", "rank": 1},
        {"provider": "gemini", "model": "gemini-3.6-flash", "key_id": "gemini_key4", "credential_ref": "GEMINI_API_KEY", "rank": 2},
        {"provider": "mistral", "model": "mistral-small-latest", "key_id": "mistral_key4", "credential_ref": "MISTRAL_API_KEY", "rank": 3},
        {"provider": "groq_2", "model": "qwen/qwen3.8-27b", "key_id": "groq_key7_b", "credential_ref": "GROQ_API_KEY_2", "rank": 4},
        {"provider": "gemini_2", "model": "gemini-3.6-flash", "key_id": "gemini_key4_b", "credential_ref": "GEMINI_API_KEY_2", "rank": 5},
        {"provider": "mistral_2", "model": "mistral-small-latest", "key_id": "mistral_key4_b", "credential_ref": "MISTRAL_API_KEY_2", "rank": 6},
    ],
}
