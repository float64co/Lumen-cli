"""Small client for the local Ollama HTTP API (/api/tags, /api/chat)."""

import json

import requests

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


class OllamaError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _longest_partial_suffix(s, tag):
    """Length of the longest suffix of `s` that is a proper prefix of `tag`.

    Used to hold back a chunk tail that might be the start of a split tag
    (e.g. content arrives as "...<th" then "ink>...") until more text
    arrives to confirm or rule it out.
    """
    for length in range(min(len(s), len(tag) - 1), 0, -1):
        if s.endswith(tag[:length]):
            return length
    return 0


def _drain_think_tags(pending, in_think):
    """Split buffered text on literal <think>/</think> tags.

    Some models (e.g. certain Nemotron builds) emit their reasoning as
    inline <think>...</think> markup inside `message.content` instead of
    using Ollama's separate `message.thinking` field. This routes that
    text to the same place a native `thinking` field would, so it renders
    in the collapsible thinking block instead of the visible answer.

    Returns (events, remaining_pending, in_think) where events is a list
    of (is_thinking, text) tuples ready to emit.
    """
    events = []
    while pending:
        tag = _THINK_CLOSE if in_think else _THINK_OPEN
        idx = pending.find(tag)
        if idx != -1:
            if idx > 0:
                events.append((in_think, pending[:idx]))
            pending = pending[idx + len(tag):]
            in_think = not in_think
            continue
        hold = _longest_partial_suffix(pending, tag)
        emit_len = len(pending) - hold
        if emit_len > 0:
            events.append((in_think, pending[:emit_len]))
        pending = pending[emit_len:]
        break
    return events, pending, in_think


class OllamaClient:
    def __init__(self, host="http://localhost:11434", timeout=120):
        self.host = host.rstrip("/")
        self.timeout = timeout

    def list_models(self):
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise OllamaError(f"Could not reach Ollama at {self.host}: {e}") from e
        try:
            data = resp.json()
        except ValueError as e:
            raise OllamaError(f"Unexpected response from Ollama: {e}") from e
        models = data.get("models", [])
        models.sort(key=lambda m: m.get("name", ""))
        return models

    def chat_stream(self, model, messages, tools=None, options=None, stop_event=None):
        """Yield events while streaming a chat completion.

        Events: {"type": "content", "text": str}
                {"type": "thinking", "text": str}
                {"type": "tool_calls", "tool_calls": [...]}
                {"type": "done"}

        If `stop_event` is set (a threading.Event) between chunks, the
        connection is closed and the generator stops early.
        """
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        if options:
            payload["options"] = options

        try:
            resp = requests.post(
                f"{self.host}/api/chat",
                json=payload,
                stream=True,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            status_code = getattr(e.response, "status_code", None)
            detail = None
            if e.response is not None:
                try:
                    detail = e.response.json().get("error")
                except ValueError:
                    detail = e.response.text.strip() or None
            err_message = f"Chat request failed: {detail}" if detail else f"Chat request failed: {e}"
            raise OllamaError(err_message, status_code=status_code) from e

        pending = ""
        in_think = False
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if stop_event is not None and stop_event.is_set():
                    resp.close()
                    return
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("error"):
                    raise OllamaError(str(data["error"]))
                message = data.get("message") or {}
                thinking = message.get("thinking")
                if thinking:
                    yield {"type": "thinking", "text": thinking}
                content = message.get("content")
                if content:
                    pending += content
                    events, pending, in_think = _drain_think_tags(pending, in_think)
                    for is_think, text in events:
                        yield {"type": "thinking" if is_think else "content", "text": text}
                tool_calls = message.get("tool_calls")
                if tool_calls:
                    yield {"type": "tool_calls", "tool_calls": tool_calls}
                if data.get("done"):
                    if pending:
                        yield {"type": "thinking" if in_think else "content", "text": pending}
                        pending = ""
                    yield {"type": "done"}
                    break
        except requests.RequestException as e:
            raise OllamaError(f"Chat stream interrupted: {e}") from e
