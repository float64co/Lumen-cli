"""Conversation state + the tool-calling loop between Lumen and Ollama."""

from . import tools as tools_mod
from .ollama_client import OllamaError

MAX_TOOL_HOPS = 6


class Conversation:
    def __init__(self, client, model, system_prompt, tools_enabled=None, options=None):
        self.client = client
        self.model = model
        self.tools_enabled = tools_enabled or {}
        self.options = options or {}
        self.messages = [{"role": "system", "content": system_prompt}]
        self.tool_specs = tools_mod.specs_for(self.tools_enabled)

    def set_system_prompt(self, text):
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0]["content"] = text
        else:
            self.messages.insert(0, {"role": "system", "content": text})

    def send(
        self, user_text, on_content=None, on_tool_call=None, on_thinking=None,
        on_notice=None, stop_event=None,
    ):
        """Send a user message, resolving any tool calls, return final text."""
        self.messages.append({"role": "user", "content": user_text})
        final_text = ""

        for _ in range(MAX_TOOL_HOPS):
            if stop_event is not None and stop_event.is_set():
                return final_text
            content_chunks = []
            tool_calls = []

            try:
                for event in self.client.chat_stream(
                    self.model, self.messages, tools=self.tool_specs, options=self.options,
                    stop_event=stop_event,
                ):
                    if event["type"] == "thinking":
                        if on_thinking:
                            on_thinking(event["text"])
                    elif event["type"] == "content":
                        content_chunks.append(event["text"])
                        if on_content:
                            on_content(event["text"])
                    elif event["type"] == "tool_calls":
                        tool_calls.extend(event["tool_calls"])
                    elif event["type"] == "done":
                        break
            except OllamaError as e:
                if e.status_code == 400 and self.tool_specs and not content_chunks:
                    self.tool_specs = []
                    if on_notice:
                        on_notice(
                            f"Model '{self.model}' rejected tool definitions (400 Bad "
                            "Request); disabling tools for this conversation and retrying."
                        )
                    continue
                raise

            final_text = "".join(content_chunks)
            assistant_msg = {"role": "assistant", "content": final_text}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self.messages.append(assistant_msg)

            if not tool_calls:
                return final_text
            if stop_event is not None and stop_event.is_set():
                return final_text

            for tc in tool_calls:
                fn = tc.get("function", {}) or {}
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                result = tools_mod.call_tool(name, args, self.tools_enabled)
                self.messages.append({"role": "tool", "name": name, "content": result})
                if on_tool_call:
                    on_tool_call(name, args, result)

        return final_text or "(reached tool-call limit without a final answer)"
