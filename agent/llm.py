"""
Provider-agnostic LLM client wrapper, structured outputs, native tool-calling,
error classification, retry/backoff, and deterministic FakeLLM for tests.
"""
from abc import ABC, abstractmethod
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar
from pydantic import BaseModel
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

T = TypeVar("T", bound=BaseModel)


# ---- Error Classification ----

class LLMError(Exception):
    """Base error for LLM client exceptions."""
    pass


class TransientError(LLMError):
    """Transient errors that can be retried with backoff (429, 5xx, timeouts, connection errors)."""
    pass


class PermanentError(LLMError):
    """Permanent errors that should not be retried or replanned (400, 401, 403, 404, invalid model, schema/config errors)."""
    pass


def classify_exception(exc: Exception) -> Exception:
    """Classifies an arbitrary exception into TransientError or PermanentError."""
    if isinstance(exc, (TransientError, PermanentError)):
        return exc

    status = getattr(exc, "status_code", None)
    err_str = str(exc).lower()

    # Permanent checks
    if status in (400, 401, 403, 404):
        return PermanentError(f"HTTP {status} Permanent Error: {exc}")
    if any(term in err_str for term in ("model_not_found", "model does not exist", "invalid_api_key", "authentication_error", "permission_denied")):
        return PermanentError(f"Permanent Configuration/Model Error: {exc}")

    # Transient checks
    if status in (408, 429, 500, 502, 503, 504):
        return TransientError(f"HTTP {status} Transient Error: {exc}")
    if any(term in err_str for term in ("rate limit", "rate_limit", "timeout", "timed out", "overloaded", "connection error", "service unavailable")):
        return TransientError(f"Transient Network/Rate Error: {exc}")

    # If status code is 4xx other than 408/429, consider permanent
    if status and 400 <= status < 500:
        return PermanentError(f"HTTP {status} Client Error: {exc}")

    return exc


def resolve_model_tier(model_tier_or_id: Optional[str], default_tier: str = "executor") -> str:
    """
    Resolves model tier aliases (planner, executor, cheap) to real model IDs
    from environment variables (PLANNER_MODEL, EXECUTOR_MODEL, CHEAP_MODEL).
    Never passes the tier name directly to the provider.
    """
    tier = (model_tier_or_id or default_tier).strip()

    if tier.lower() == "planner":
        return os.getenv("PLANNER_MODEL", "openai/gpt-oss-120b")
    elif tier.lower() == "executor":
        return os.getenv("EXECUTOR_MODEL", "openai/gpt-oss-120b")
    elif tier.lower() == "cheap":
        return os.getenv("CHEAP_MODEL", "openai/gpt-oss-20b")
    else:
        # Already an explicit model ID or custom model string
        return tier


class LLMResponse(BaseModel):
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    finish_reason: str = "stop"
    usage: Dict[str, int] = {}
    duration_ms: int = 0


class ToolCall(BaseModel):
    name: str
    arguments: Dict[str, Any]


class LLMClient(ABC):
    @abstractmethod
    def complete(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.0,
        model: Optional[str] = None,
    ) -> LLMResponse:
        pass

    @abstractmethod
    def structured(
        self,
        prompt: str,
        schema: Type[T],
        system: Optional[str] = None,
        model: Optional[str] = None,
    ) -> T:
        """Extracts validated structured output matching the Pydantic schema."""
        pass

    @abstractmethod
    def choose_tool(
        self,
        messages: List[Dict[str, str]],
        tool_specs: List[Dict[str, Any]],
        system: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[ToolCall]:
        """Picks the next tool call from tool specifications."""
        pass

    def validate_configuration(self) -> None:
        """Preflight check validating that configured models exist and are accessible."""
        pass


class FakeLLM(LLMClient):
    """
    Deterministic scripted LLM for testing and offline runs.
    Accepts:
    - Pre-canned complete responses
    - Pre-canned structured responses
    - Pre-canned tool choices
    - Or default sensible heuristics for security questionnaire workflow.
    """
    def __init__(
        self,
        responses: Optional[List[LLMResponse]] = None,
        structured_responses: Optional[List[Any]] = None,
        tool_choices: Optional[List[Optional[ToolCall]]] = None,
    ) -> None:
        self.responses = list(responses) if responses else []
        self.structured_responses = list(structured_responses) if structured_responses else []
        self.tool_choices = list(tool_choices) if tool_choices else []
        self.call_history: List[Dict[str, Any]] = []

    def queue_response(self, response: LLMResponse) -> None:
        self.responses.append(response)

    def queue_structured(self, obj: Any) -> None:
        self.structured_responses.append(obj)

    def queue_tool_choice(self, tool_call: Optional[ToolCall]) -> None:
        self.tool_choices.append(tool_call)

    def complete(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.0,
        model: Optional[str] = None,
    ) -> LLMResponse:
        resolved_model = resolve_model_tier(model, default_tier="executor")
        self.call_history.append({"type": "complete", "messages": messages, "tools": tools, "model": resolved_model})
        if self.responses:
            return self.responses.pop(0)

        last_content = messages[-1]["content"] if messages else ""
        return LLMResponse(
            content=f"FakeLLM response for: {last_content[:50]}",
            tool_calls=None,
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

    def structured(
        self,
        prompt: str,
        schema: Type[T],
        system: Optional[str] = None,
        model: Optional[str] = None,
    ) -> T:
        resolved_model = resolve_model_tier(model, default_tier="planner")
        self.call_history.append({"type": "structured", "prompt": prompt, "schema": schema.__name__, "model": resolved_model})
        if self.structured_responses:
            resp = self.structured_responses.pop(0)
            if isinstance(resp, schema):
                return resp
            if isinstance(resp, dict):
                return schema(**resp)
            if isinstance(resp, str):
                return schema(**json.loads(resp))

        # Default heuristic based on schema fields
        fields = schema.model_fields
        defaults: Dict[str, Any] = {}
        for f_name, f_info in fields.items():
            if f_name == "customer":
                defaults[f_name] = "Acme"
            elif f_name == "questionnaire_path":
                defaults[f_name] = "data/inbox/Acme_Security_Questionnaire.xlsx"
            elif f_name == "selection_rationale":
                defaults[f_name] = "Picked primary current questionnaire."
            elif f_name == "constraints":
                defaults[f_name] = ["Do not sign 99.99% uptime guarantee"]
            elif f_name == "key_points":
                defaults[f_name] = ["Compliant with standard policies."]
            elif f_name == "low_confidence_goal":
                defaults[f_name] = False
            elif f_name == "needs_human_clarification":
                defaults[f_name] = False
            elif f_name == "answer":
                defaults[f_name] = "Compliant with standard policy."
            elif f_info.default_factory is not None:
                defaults[f_name] = f_info.default_factory()
            elif f_info.default is not None and str(f_info.default) != "PydanticUndefined":
                defaults[f_name] = f_info.default
            else:
                defaults[f_name] = None
        return schema(**defaults)

    def choose_tool(
        self,
        messages: List[Dict[str, str]],
        tool_specs: List[Dict[str, Any]],
        system: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[ToolCall]:
        resolved_model = resolve_model_tier(model, default_tier="executor")
        self.call_history.append({"type": "choose_tool", "messages": messages, "model": resolved_model})
        if self.tool_choices:
            return self.tool_choices.pop(0)

        # Inspect messages for tool_hint in current plan step
        for msg in reversed(messages):
            content = msg.get("content", "")
            match = re.search(r"tool_hint:\s*([a-zA-Z0-9_]+)", content)
            if match:
                tool_name = match.group(1)
                # Provide reasonable default arguments for standard tools
                args: Dict[str, Any] = {}
                if tool_name == "find_inbox_files":
                    args = {"inbox_dir": "data/inbox"}
                elif tool_name == "parse_questionnaire":
                    args = {"file_path": "data/inbox/Acme_Security_Questionnaire.xlsx"}
                elif tool_name == "write_filled_xlsx":
                    args = {
                        "source_file": "data/inbox/Acme_Security_Questionnaire.xlsx",
                        "output_file": "data/output/Acme_Security_Questionnaire_Filled.xlsx",
                    }
                elif tool_name == "process_question":
                    args = {"question_id": "Q-01"}
                return ToolCall(name=tool_name, arguments=args)

        # Default fallback
        if tool_specs:
            fn_name = tool_specs[0].get("function", {}).get("name", "find_inbox_files")
            return ToolCall(name=fn_name, arguments={})
        return None

    def validate_configuration(self) -> None:
        pass


class GroqLLM(LLMClient):
    """
    Groq-powered LLM client using the Groq Python SDK.
    Supports structured output validation with repair retry, tool calling,
    model tier resolution, and automatic retry-with-backoff on transient errors.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.getenv("GROQ_API_KEY")

        if not self.api_key:
            raise PermanentError("GROQ_API_KEY environment variable is missing.")

        self.client = Groq(
            api_key=self.api_key,
            max_retries=0,
        )

    def validate_configuration(self) -> None:
        """
        Preflight check: validates that each configured model exists on the provider.
        Aborts with a clear configuration error message before planning.
        """
        models_to_check = {
            "PLANNER_MODEL": resolve_model_tier("planner"),
            "EXECUTOR_MODEL": resolve_model_tier("executor"),
            "CHEAP_MODEL": resolve_model_tier("cheap"),
        }

        try:
            available_models = [m.id for m in self.client.models.list().data]
        except Exception as e:
            classified = classify_exception(e)
            raise PermanentError(f"configuration error: Failed to reach LLM provider to list models: {classified}")

        for env_var, model_id in models_to_check.items():
            if model_id not in available_models:
                raise PermanentError(
                    f"configuration error: Configured model '{model_id}' ({env_var}) is not available on Groq. "
                    f"Available models: {', '.join(sorted(available_models))}"
                )

    def _retry_call(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        delays = [1.0, 2.0, 4.0]
        last_exc: Optional[Exception] = None

        for attempt, delay in enumerate(delays):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                classified = classify_exception(e)
                if isinstance(classified, PermanentError):
                    # Permanent error: fail immediately, do not retry
                    raise classified

                if isinstance(classified, TransientError):
                    last_exc = classified
                    if attempt < len(delays) - 1:
                        time.sleep(delay)
                        continue
                raise classified

        raise last_exc or TransientError("Max retries exceeded on LLM call.")

    @staticmethod
    def _normalize_messages(
        messages: List[Dict[str, str]],
        system: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []

        if system:
            normalized.append({"role": "system", "content": system})

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system" and system:
                continue

            normalized.append({
                "role": role,
                "content": content,
            })

        return normalized

    def complete(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.0,
        model: Optional[str] = None,
        system: Optional[str] = None,
    ) -> LLMResponse:
        model_name = resolve_model_tier(model, default_tier="executor")

        kwargs: Dict[str, Any] = {
            "model": model_name,
            "messages": self._normalize_messages(messages, system),
            "temperature": temperature,
            "max_tokens": 4096,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        t0 = time.time()
        res = self._retry_call(
            self.client.chat.completions.create,
            **kwargs,
        )
        duration_ms = int((time.time() - t0) * 1000)

        choice = res.choices[0]
        content = choice.message.content or ""
        usage = res.usage

        return LLMResponse(
            content=content,
            finish_reason=choice.finish_reason or "stop",
            usage={
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "total_tokens": usage.total_tokens if usage else 0,
            },
            duration_ms=duration_ms,
        )

    def structured(
        self,
        prompt: str,
        schema: Type[T],
        system: Optional[str] = None,
        model: Optional[str] = None,
    ) -> T:
        model_name = resolve_model_tier(model, default_tier="planner")

        schema_json = json.dumps(
            schema.model_json_schema(),
            indent=2,
        )

        system_text = (
            (system or "")
            + "\nRespond with valid JSON strictly conforming to this JSON schema:\n"
            + schema_json
            + "\nDo not output markdown codeblocks, only valid raw JSON."
        )

        def parse_json(raw: str) -> T:
            raw = raw.strip()

            if raw.startswith("```"):
                lines = raw.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                raw = "\n".join(lines).strip()

            try:
                return schema.model_validate_json(raw)
            except Exception:
                start = raw.find("{")
                end = raw.rfind("}")

                if start < 0 or end < start:
                    raise

                return schema.model_validate_json(raw[start:end + 1])

        resp = self.complete(
            [{"role": "user", "content": prompt}],
            system=system_text,
            model=model_name,
            temperature=0.0,
        )

        try:
            return parse_json(resp.content)
        except Exception as first_error:
            repair_prompt = (
                f"The following output failed validation for schema "
                f"{schema.__name__}:\n{resp.content}\n"
                f"Validation error: {first_error}\n"
                "Correct the output and return only valid JSON."
            )

            repair_resp = self.complete(
                [{"role": "user", "content": repair_prompt}],
                system=system_text,
                model=model_name,
                temperature=0.0,
            )

            try:
                return parse_json(repair_resp.content)
            except Exception as e:
                raise PermanentError(f"Failed to parse structured output for {schema.__name__}: {e}")

    def choose_tool(
        self,
        messages: List[Dict[str, str]],
        tool_specs: List[Dict[str, Any]],
        system: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[ToolCall]:
        model_name = resolve_model_tier(model, default_tier="executor")

        kwargs: Dict[str, Any] = {
            "model": model_name,
            "messages": self._normalize_messages(messages, system),
            "tools": tool_specs,
            "tool_choice": "auto",
            "temperature": 0.0,
            "max_tokens": 2048,
        }

        res = self._retry_call(
            self.client.chat.completions.create,
            **kwargs,
        )

        message = res.choices[0].message
        tool_calls = message.tool_calls or []

        if not tool_calls:
            return None

        call = tool_calls[0]
        arguments = json.loads(call.function.arguments or "{}")

        return ToolCall(
            name=call.function.name,
            arguments=arguments,
        )


def get_llm_client(provider: Optional[str] = None) -> LLMClient:
    """Factory to create the configured LLM client."""

    prov = (
        provider or os.getenv("LLM_PROVIDER", "fake")
    ).lower()

    if prov == "fake":
        return FakeLLM()

    if prov in ("groq", "real"):
        return GroqLLM()

    if prov == "anthropic":
        raise ValueError(
            "Anthropic support was replaced with Groq. "
            "Set LLM_PROVIDER=groq."
        )

    if prov in ("openai", "gemini"):
        raise NotImplementedError(
            f"Provider '{prov}' is not implemented."
        )

    raise ValueError(f"Unknown LLM provider: {prov}")
