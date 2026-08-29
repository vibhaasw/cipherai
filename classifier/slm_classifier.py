"""Offline-first prompt classifier backed by local Ollama."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re

import httpx

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "10"))

ALLOWED_DOMAINS = {"CODE_GEN", "CREATIVE_TEXT", "MATH_LOGIC", "DOC_SUMMARIZATION", "GENERAL"}
ALLOWED_COMPLEXITIES = {"LOW", "MEDIUM", "HIGH"}

logger = logging.getLogger("cipherai.classifier.slm_classifier")


@dataclass
class ClassificationResult:
    """Structured prompt classification metadata."""

    domain: str
    complexity: str
    complexity_score: int
    est_input_tokens: int
    est_output_tokens: int


async def classify_prompt(prompt: str, model: str = "phi4-mini:latest") -> ClassificationResult:
    """
    Classify an incoming prompt using local Ollama.

    Falls back to keyword heuristics on any network, timeout, or parse failure.
    This function never raises.
    """
    system_instruction = (
        "You are a strict JSON classifier. Return ONLY valid JSON with keys:\n"
        "domain, complexity, complexity_score, est_input_tokens, est_output_tokens.\n"
        "domain must be one of CODE_GEN, CREATIVE_TEXT, MATH_LOGIC, DOC_SUMMARIZATION, GENERAL.\n"
        "complexity must be one of LOW, MEDIUM, HIGH.\n"
        "complexity_score must be integer 1-10.\n"
        "No markdown, no code fences, no extra text."
    )

    fallback_reason = "unknown error"
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": model,
                    "prompt": (
                        f"{system_instruction}\n\n"
                        f"Prompt to classify:\n{prompt}\n\n"
                        "Return JSON only."
                    ),
                    "stream": False,
                    "format": "json",
                },
            )
            response.raise_for_status()
            raw_text = str(response.json().get("response", ""))
            parsed = _safe_parse_classification_json(raw_text)
            if parsed is not None:
                if parsed.est_input_tokens <= 1:
                    parsed.est_input_tokens = max(len(prompt) // 4, 1)
                if parsed.est_output_tokens <= 1:
                    parsed.est_output_tokens = _default_output_tokens(parsed.complexity)
                logger.info(
                    "[CipherAI][Classifier] Used LOCAL Ollama model (%s) — domain=%s complexity=%s",
                    model,
                    parsed.domain,
                    parsed.complexity,
                )
                return parsed
            fallback_reason = "JSON parse failure"
    except httpx.TimeoutException:
        fallback_reason = "timeout"
    except (httpx.ConnectError, httpx.NetworkError):
        fallback_reason = "connection error"
    except (ValueError, json.JSONDecodeError):
        fallback_reason = "JSON parse failure"
    except httpx.HTTPError:
        fallback_reason = "connection error"
    except Exception:
        fallback_reason = "JSON parse failure"

    fallback = _heuristic_classification(prompt)
    logger.info(
        "[CipherAI][Classifier] FALLBACK heuristic used — reason: %s — domain=%s complexity=%s",
        fallback_reason,
        fallback.domain,
        fallback.complexity,
    )
    return fallback


def _safe_parse_classification_json(raw_text: str) -> ClassificationResult | None:
    """Parse and normalize model JSON output into ClassificationResult."""
    text = raw_text.strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    payload = json.loads(text)
    if not isinstance(payload, dict):
        return None

    domain = str(payload.get("domain", "GENERAL")).strip().upper()
    if domain not in ALLOWED_DOMAINS:
        domain = "GENERAL"

    complexity_score = _clamp_int(payload.get("complexity_score", 5), min_value=1, max_value=10, default=5)
    complexity = str(payload.get("complexity", _score_to_complexity(complexity_score))).strip().upper()
    if complexity not in ALLOWED_COMPLEXITIES:
        complexity = _score_to_complexity(complexity_score)

    est_input_tokens = _clamp_int(
        payload.get("est_input_tokens", 0),
        min_value=1,
        max_value=200_000,
        default=1,
    )
    est_output_tokens = _clamp_int(
        payload.get("est_output_tokens", _default_output_tokens(complexity)),
        min_value=1,
        max_value=200_000,
        default=_default_output_tokens(complexity),
    )

    return ClassificationResult(
        domain=domain,
        complexity=complexity,
        complexity_score=complexity_score,
        est_input_tokens=est_input_tokens,
        est_output_tokens=est_output_tokens,
    )


def _heuristic_classification(prompt: str) -> ClassificationResult:
    """Fallback classifier based on keywords and regex patterns."""
    lower = prompt.lower()

    code_pattern = re.compile(
        r"\b(function|class|rust|python|java|typescript|javascript|sql|bug|debug|compile|refactor|api|thread)\b"
    )
    math_pattern = re.compile(r"(\bsolve\b|\bcalculate\b|\bequation\b|\bintegral\b|\bderivative\b|[=+\-*/^])")
    summary_pattern = re.compile(r"\b(summarize|summary|tl;dr|tldr|condense|brief)\b")
    creative_pattern = re.compile(r"\b(write a story|story|poem|lyrics|creative|novel|narrative)\b")

    if code_pattern.search(lower):
        domain = "CODE_GEN"
        complexity_score = 8 if len(prompt) > 300 else 6
    elif math_pattern.search(lower):
        domain = "MATH_LOGIC"
        complexity_score = 7 if len(prompt) > 200 else 5
    elif summary_pattern.search(lower):
        domain = "DOC_SUMMARIZATION"
        complexity_score = 5
    elif creative_pattern.search(lower):
        domain = "CREATIVE_TEXT"
        complexity_score = 5
    else:
        domain = "GENERAL"
        complexity_score = 4

    complexity = _score_to_complexity(complexity_score)
    est_input_tokens = max(len(prompt) // 4, 1)
    est_output_tokens = _default_output_tokens(complexity)

    return ClassificationResult(
        domain=domain,
        complexity=complexity,
        complexity_score=complexity_score,
        est_input_tokens=est_input_tokens,
        est_output_tokens=est_output_tokens,
    )


def _score_to_complexity(score: int) -> str:
    """Map complexity score to LOW/MEDIUM/HIGH buckets."""
    if score <= 3:
        return "LOW"
    if score <= 7:
        return "MEDIUM"
    return "HIGH"


def _default_output_tokens(complexity: str) -> int:
    """Heuristic output token estimate by complexity bucket."""
    defaults = {"LOW": 200, "MEDIUM": 600, "HIGH": 1500}
    return defaults.get(complexity, 600)


def _clamp_int(value: object, min_value: int, max_value: int, default: int) -> int:
    """Convert arbitrary value to bounded integer."""
    try:
        casted = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(min(casted, max_value), min_value)
