"""Model clients: the hosted Meta Model API (Responses) and a local chat completions server.

Both expose the same two calls. `structured` returns JSON that matches a strict schema.
`tool_step` runs one turn of a tool loop, so one extractor can drive either model
with the same code.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

META_BASE_URL = "https://api.meta.ai/v1"

# USD per million tokens: input, cached input, output (dev.meta.ai pricing, October 2026).
PRICES = {
    "muse-spark-1.3": (1.25, 0.15, 4.25),
    "muse-spark-1.3-contributor": (0.10, 0.002, 0.20),
}

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


class ModelError(RuntimeError):
    pass


class SpendLimitReached(ModelError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.cached_tokens += other.cached_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens

    def cost(self, model: str) -> float | None:
        if model not in PRICES:
            return None  # local models cost nothing per token
        fresh, cached, out = PRICES[model]
        uncached = self.input_tokens - self.cached_tokens
        return (uncached * fresh + self.cached_tokens * cached + self.output_tokens * out) / 1e6


def _usage(data: dict[str, Any]) -> Usage:
    usage = data.get("usage") or {}
    return Usage(
        input_tokens=usage.get("input_tokens", 0),
        cached_tokens=(usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        reasoning_tokens=(usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
    )


class SpendLedger:
    """What the hosted API has cost so far, one line per response, kept in the data folder
    so the total covers every run from the command line and the review screen.

    The Meta billing page only notifies when a spend limit is passed, so the limit is
    enforced here: once the total reaches it, no further request is sent. A request already
    sent can still take the total a little past the limit.
    """

    _lock = threading.Lock()

    def __init__(self, path: Path, limit_usd: float | None = None):
        self.path = path
        self.limit_usd = limit_usd

    def spent(self) -> float:
        if not self.path.exists():
            return 0.0
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return sum(json.loads(line)["usd"] for line in lines if line.strip())

    def check(self, model: str) -> None:
        if self.limit_usd is None:
            return
        if model not in PRICES:
            raise SpendLimitReached(f"no price for {model}, so the spend limit cannot be kept")
        spent = self.spent()
        if spent >= self.limit_usd:
            raise SpendLimitReached(f"spend limit reached: ${spent:.2f} of "
                                    f"${self.limit_usd:.2f} (MODEL_SPEND_LIMIT_USD)")

    def add(self, model: str, usage: Usage) -> None:
        line = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "model": model,
                "usd": round(usage.cost(model) or 0.0, 6), "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    error: str | None = None  # set when the arguments were not valid JSON


def _tool_call(call_id: str, name: str, raw: str | None) -> ToolCall:
    try:
        arguments = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return ToolCall(call_id, name, {}, error=str(exc))
    if not isinstance(arguments, dict):
        return ToolCall(call_id, name, {}, error="arguments must be a JSON object")
    return ToolCall(call_id, name, arguments)


@dataclass
class Reply:
    model: str
    text: str
    usage: Usage
    seconds: float
    response_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise ModelError(f"{self.model} did not return JSON: {self.text[:200]!r}") from exc


def _post_tools(client: httpx.Client, url: str, body: dict[str, Any], **kw: Any) -> dict:
    """POST a tool turn; a server that rejects a forced tool choice gets the turn again with
    the choice left to the model."""
    try:
        return _post(client, url, body, **kw)
    except ModelError as exc:
        if body.get("tool_choice", "auto") == "auto" or "returned 400" not in str(exc):
            raise
        return _post(client, url, body | {"tool_choice": "auto"}, **kw)


def _post(client: httpx.Client, url: str, body: dict[str, Any], attempts: int = 5) -> dict:
    delay = 2.0
    for attempt in range(attempts):
        try:
            response = client.post(url, json=body)
        except httpx.TransportError as exc:
            if attempt == attempts - 1:
                # The operating system's own message may be in the machine's language.
                reason = ("the server is not reachable" if isinstance(exc, httpx.ConnectError)
                          else type(exc).__name__)
                raise ModelError(f"request to {url} failed: {reason}") from exc
        else:
            if response.status_code < 400:
                return response.json()
            if response.status_code not in RETRY_STATUS or attempt == attempts - 1:
                raise ModelError(f"{url} returned {response.status_code}: {response.text[:500]}")
        time.sleep(delay)
        delay *= 2
    raise ModelError("unreachable")


class MetaClient:
    """Meta Model API through the Responses endpoint."""

    def __init__(self, api_key: str, model: str = "muse-spark-1.3",
                 base_url: str = META_BASE_URL, effort: str = "medium", timeout: float = 900,
                 spend: SpendLedger | None = None):
        if not api_key:
            raise ModelError("MODEL_API_KEY is empty; add it to .env")
        self.model = model
        self.effort = effort
        self.spend = spend
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout,
                                 headers={"Authorization": f"Bearer {api_key}"})
        # The server keeps the whole conversation, so no tool output is ever removed.
        self.elided: set[str] = set()

    def _send(self, body: dict[str, Any]) -> tuple[dict[str, Any], float]:
        if self.spend:
            self.spend.check(self.model)
        start = time.monotonic()
        data = _post_tools(self.http, f"{self.base_url}/responses", body)
        if self.spend:
            # Recorded before the reply is read: an incomplete response is billed too.
            self.spend.add(self.model, _usage(data))
        return data, time.monotonic() - start

    def _reply(self, data: dict[str, Any], seconds: float) -> Reply:
        if data.get("status") not in (None, "completed"):
            raise ModelError(f"response {data.get('id')} ended as {data.get('status')}: "
                             f"{data.get('incomplete_details') or data.get('error')}")
        texts: list[str] = []
        calls: list[ToolCall] = []
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        texts.append(part["text"])
                    elif part.get("type") == "refusal":
                        raise ModelError(f"model refused: {part.get('refusal')}")
            elif item.get("type") == "function_call":
                calls.append(_tool_call(item["call_id"], item["name"], item.get("arguments")))
        return Reply(
            model=self.model,
            text=data.get("output_text") or "".join(texts),
            usage=_usage(data),
            seconds=seconds,
            response_id=data.get("id"),
            tool_calls=calls,
            raw=data,
        )

    def structured(self, instructions: str, user: str, name: str,
                   schema: dict[str, Any]) -> Reply:
        body = {
            "model": self.model,
            "instructions": instructions,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": user}]}],
            "text": {"format": {"type": "json_schema", "name": name, "schema": schema,
                                "strict": True}},
            "reasoning": {"effort": self.effort},
        }
        return self._reply(*self._send(body))

    def tool_step(self, instructions: str, tools: list[dict[str, Any]],
                  user: str | None = None, previous: Reply | None = None,
                  results: list[tuple[str, str]] | None = None,
                  tool_choice: str = "auto") -> Reply:
        """One turn of a tool loop: the opening `user` message, or tool `results` (and an
        optional follow-up `user` message) for the `previous` reply. `tool_choice` is
        `auto`, or `required` to make the model call one of `tools`."""
        items: list[dict[str, Any]] = [
            {"type": "function_call_output", "call_id": call_id, "output": out}
            for call_id, out in results or []
        ]
        if user:
            items.append({"role": "user", "content": [{"type": "input_text", "text": user}]})
        body: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": items,
            "tools": [{"type": "function", **tool} for tool in tools],
            "tool_choice": tool_choice,
            "reasoning": {"effort": self.effort},
        }
        if previous is not None:
            body["previous_response_id"] = previous.response_id
        return self._reply(*self._send(body))


class LocalClient:
    """A local chat completions server, such as LM Studio or llama.cpp serving Glimmer."""

    def __init__(self, base_url: str, model: str, timeout: float = 1800,
                 temperature: float = 1.0, top_p: float = 0.95,
                 context_chars: int = 150_000):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.context_chars = context_chars
        self.http = httpx.Client(timeout=timeout)
        self._messages: list[dict[str, Any]] = []
        # Tool calls whose output was removed from the context, so they may be repeated.
        self.elided: set[str] = set()

    def _compact(self) -> None:
        """Keep the conversation inside a local context window by eliding the oldest tool
        outputs; the model can call the tool again if it needs the text back."""
        def size() -> int:
            return sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls")))
                       for m in self._messages)
        for message in self._messages:
            if size() <= self.context_chars:
                return
            content = message.get("content") or ""
            if message.get("role") == "tool" and len(content) > 200:
                message["content"] = (content[:120] + " [... removed to save context; "
                                      "call the tool again to see it]")
                self.elided.add(message["tool_call_id"])

    def _chat(self, body: dict[str, Any]) -> Reply:
        body = {"model": self.model, "temperature": self.temperature, "top_p": self.top_p,
                **body}
        start = time.monotonic()
        data = _post_tools(self.http, f"{self.base_url}/chat/completions", body, attempts=2)
        seconds = time.monotonic() - start
        message = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        calls = [_tool_call(c["id"], c["function"]["name"], c["function"].get("arguments"))
                 for c in message.get("tool_calls") or []]
        self._messages.append(message)
        return Reply(
            model=self.model,
            text=message.get("content") or "",
            usage=Usage(input_tokens=usage.get("prompt_tokens", 0),
                        output_tokens=usage.get("completion_tokens", 0)),
            seconds=seconds,
            tool_calls=calls,
            raw=data,
        )

    def structured(self, instructions: str, user: str, name: str,
                   schema: dict[str, Any]) -> Reply:
        self._messages = [{"role": "system", "content": instructions},
                          {"role": "user", "content": user}]
        return self._chat({
            "messages": list(self._messages),
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": name, "schema": schema, "strict": True}},
        })

    def tool_step(self, instructions: str, tools: list[dict[str, Any]],
                  user: str | None = None, previous: Reply | None = None,
                  results: list[tuple[str, str]] | None = None,
                  tool_choice: str = "auto") -> Reply:
        if previous is None:
            self._messages = [{"role": "system", "content": instructions}]
            self.elided.clear()
        self._messages += [{"role": "tool", "tool_call_id": call_id, "content": out}
                           for call_id, out in results or []]
        if user:
            self._messages.append({"role": "user", "content": user})
        self._compact()
        body: dict[str, Any] = {
            "messages": list(self._messages),
            "tools": [{"type": "function", "function": tool} for tool in tools],
        }
        if tool_choice != "auto":
            body["tool_choice"] = tool_choice
        return self._chat(body)


def spend_path(settings: Any) -> Path:
    return settings.runs_dir / "spend.jsonl"


def make_client(kind: str, settings: Any) -> MetaClient | LocalClient:
    """`hosted` for the Meta Model API, `local` for the local chat completions server."""
    if kind == "hosted":
        return MetaClient(settings.model_api_key or "", settings.hosted_model,
                          base_url=settings.model_api_base,
                          spend=SpendLedger(spend_path(settings), settings.spend_limit_usd))
    if kind == "local":
        return LocalClient(settings.local_api_base, settings.local_model)
    raise ValueError(f"unknown model kind {kind!r}")
