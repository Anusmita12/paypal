"""Gemini-driven buyer run (AI_MODE=gemini): the same manual tool loop, system prompt, validated tools and limits
as the Claude driver. The server still owns prices, arithmetic, ranking, approval and payment, and there is no
fallback to the fixture script."""

import json
import time

import httpx
from google import genai
from google.genai import errors, types

from app.agent_llm import SYSTEM, _kickoff, tool_schema
from app.agent_tools import TOOLS, ToolContext, execute
from app.errors import AppError

try:  # the SDK may run on httpx or httpx2 depending on version
    import httpx2

    _TRANSPORT_ERRORS: tuple[type[Exception], ...] = (httpx.HTTPError, httpx2.HTTPError)
except ImportError:  # pragma: no cover
    _TRANSPORT_ERRORS = (httpx.HTTPError,)

REQUEST_TIMEOUT_SECONDS = 60.0
MAX_TURNS = 30


def declarations() -> list[types.FunctionDeclaration]:
    out = []
    for name in TOOLS:
        schema = tool_schema(name)
        params = schema["input_schema"]
        decl = {"name": name, "description": schema["description"]}
        if params["properties"]:
            decl["parameters_json_schema"] = params
        out.append(types.FunctionDeclaration(**decl))
    return out


def run_gemini(ctx: ToolContext, client: genai.Client | None = None) -> str:
    settings = ctx.settings
    client = client or genai.Client(api_key=settings.gemini_api_key)
    tools = [types.Tool(function_declarations=declarations())]
    contents: list = [types.Content(role="user", parts=[types.Part(text=_kickoff(ctx))])]
    final_text = ""

    for _ in range(MAX_TURNS):
        ctx.check_limits()
        remaining = settings.ai_max_run_seconds - (time.monotonic() - ctx.started)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM,
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            http_options=types.HttpOptions(timeout=int(1000 * max(5.0, min(REQUEST_TIMEOUT_SECONDS, remaining)))),
        )
        try:
            response = client.models.generate_content(model=settings.gemini_model, contents=contents, config=config)
        except errors.APIError as exc:
            if exc.code == 429:
                raise AppError("model_rate_limited", "Gemini free-tier rate limit reached; wait a minute and retry.", 429) from exc
            raise AppError("model_error", f"Gemini API error {exc.code}: {exc.message}"[:300], 502) from exc
        except _TRANSPORT_ERRORS as exc:
            raise AppError("model_unavailable", f"Gemini API unreachable: {type(exc).__name__}", 502) from exc

        usage = response.usage_metadata
        if usage:
            ctx.run.input_tokens += (usage.prompt_token_count or 0) + (usage.tool_use_prompt_token_count or 0)
            ctx.run.output_tokens += (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
            ctx.db.commit()

        candidate = (response.candidates or [None])[0]
        if candidate is None or candidate.content is None:
            reason = getattr(candidate, "finish_reason", None) or getattr(response.prompt_feedback, "block_reason", None)
            raise AppError("model_refusal", f"Gemini returned no content ({reason}).", 502)
        parts = candidate.content.parts or []
        text = " ".join(p.text for p in parts if p.text and not p.thought).strip()
        if text:
            final_text = text
            ctx.event("message", text)

        calls = [p.function_call for p in parts if p.function_call]
        if not calls:
            break
        contents.append(candidate.content)  # pass the model turn back unchanged (keeps thought signatures)
        results = []
        for call in calls:
            result = execute(ctx, call.name or "", dict(call.args or {}))
            results.append(types.Part(function_response=types.FunctionResponse(
                id=call.id, name=call.name, response=json.loads(json.dumps(result, default=str)))))
        contents.append(types.Content(role="user", parts=results))
    else:
        raise AppError("tool_call_limit", f"Run stopped after {MAX_TURNS} model turns.", 409)

    if ctx.proposal_id:
        from app.models import Proposal

        return ctx.db.get(Proposal, ctx.proposal_id).explanation
    return final_text or "The model finished without a proposal."
