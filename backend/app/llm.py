"""One structured-output call to the configured model (Gemini or Claude). Callers validate the result; this
module only transports. Fixture mode never reaches here: callers use their deterministic template instead."""

import json

import anthropic
from google import genai
from google.genai import errors, types

from app.config import Settings
from app.errors import AppError


def generate_json(settings: Settings, system: str, prompt: str, schema: dict, timeout_s: float = 60.0) -> tuple[dict, dict]:
    """Return (parsed JSON, usage {input_tokens, output_tokens, model})."""
    if settings.ai_mode == "gemini":
        client = genai.Client(api_key=settings.gemini_api_key)
        config = types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json", response_json_schema=schema,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
        )
        try:
            r = client.models.generate_content(model=settings.gemini_model, contents=prompt, config=config)
        except errors.APIError as exc:
            code = "model_rate_limited" if exc.code == 429 else "model_error"
            raise AppError(code, f"Gemini API error {exc.code}: {exc.message}"[:300], 502) from exc
        except Exception as exc:
            raise AppError("model_unavailable", f"Gemini API unreachable: {type(exc).__name__}", 502) from exc
        text = r.text or ""
        u = r.usage_metadata
        usage = {"input_tokens": (u.prompt_token_count or 0) if u else 0,
                 "output_tokens": ((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else 0,
                 "model": settings.gemini_model}
    elif settings.ai_mode == "anthropic":
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=2, timeout=timeout_s)
        try:
            r = client.messages.create(
                model=settings.anthropic_model, max_tokens=8000, system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": settings.ai_effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.APIConnectionError as exc:
            raise AppError("model_unavailable", f"Claude API unreachable: {type(exc).__name__}", 502) from exc
        except anthropic.APIStatusError as exc:
            raise AppError("model_error", f"Claude API error {exc.status_code}", 502) from exc
        if r.stop_reason == "refusal":
            raise AppError("model_refusal", "The model declined this request.", 502)
        text = "".join(b.text for b in r.content if b.type == "text")
        usage = {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens, "model": settings.anthropic_model}
    else:
        raise AppError("no_model", "No model configured (AI_MODE=fixture).", 409)
    try:
        return json.loads(text), usage
    except ValueError as exc:
        raise AppError("model_bad_json", "The model did not return valid JSON.", 502) from exc


