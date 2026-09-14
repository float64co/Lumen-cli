"""The /research pipeline: Plan -> extract -> synthesize -> critique -> Polish.

Each stage is a fresh, isolated model call (its own system + one user turn) --
never a continuation of the main chat's message history. A stage's output is
handed to the next stage as plain text, matching the graph-mode design this
was modeled on. `critique` is the one branching stage: it must call the
`route` tool to say whether the report is ready ("Polish") or needs another
pass ("Plan"), capped at MAX_CRITIQUE_LOOPS revisions so a stubborn model
can't loop forever.
"""

from . import chat as chat_mod
from . import tools as tools_mod
from .ollama_client import OllamaError

MAX_CRITIQUE_LOOPS = 3

PLAN_PROMPT = (
    "You are a search query decomposition agent. Given a topic or question, "
    "output exactly 5 specific, non-overlapping search queries or "
    "sub-questions that together provide comprehensive coverage of the topic. "
    "Each query should target a distinct angle or aspect. Output only the 5 "
    "queries, one per line, with no numbering, labels, or additional text."
)

EXTRACT_PROMPT = (
    "You are a research extraction agent. Given a list of topics or questions "
    "from the Planner, retrieve only the facts, statistics, and citations that "
    "directly address each item on that list. Do not include background "
    "context, opinions, or information not explicitly requested. For each "
    "item, return the relevant data point and its source citation. If no "
    "reliable information exists for an item, state that explicitly."
)

SYNTHESIZE_PROMPT = "Form a structured report with headers and a summary."

CRITIQUE_PROMPT = (
    'You are a validation agent. Analyze the input and output a structured '
    'list of issues grouped into exactly two categories: "Missing Info" '
    "(required fields or context that are absent) and \"Errors\" (incorrect, "
    "conflicting, or malformed data). Each issue must be specific and "
    "actionable -- state what is missing or wrong and why it matters. If no "
    'issues are found in either category, output only "No issues found." '
    "Do not add commentary outside the structured output. You MUST end your "
    "response by calling the route tool: choose 'Polish' if no issues were "
    "found, or 'Plan' to send the research back for revision if you found "
    "issues."
)

POLISH_PROMPT = (
    "You are the final output stage of a document pipeline. Your sole "
    "responsibility is to produce a clean, correctly formatted, "
    "publication-ready document.\n\n"
    "Rules:\n"
    "- Output the document and nothing else -- no preamble, no commentary, "
    "no meta-notes.\n"
    "- Every fact, figure, name, and claim must have been verified by an "
    "earlier stage; do not invent, infer, or assume anything not present in "
    "your input.\n"
    "- If any section of the input is flagged as unverified, incomplete, or "
    "contradictory, do not guess -- instead output a clearly marked "
    "placeholder: [UNVERIFIED: describe the gap].\n"
    "- Preserve all verified content exactly; do not paraphrase, reorder, or "
    "omit without explicit instruction.\n"
    "- Apply consistent formatting throughout: headings, spacing, "
    "punctuation, and style must be uniform.\n"
    "- Format the document as Markdown. If the input does not specify a "
    "structure, default to clean prose with clear section headings.\n"
    "- Do not summarise, truncate, or add content beyond what was provided "
    "and verified upstream."
)

ROUTE_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "route",
        "description": "Choose the next stage of the research pipeline based on your critique.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["Polish", "Plan"],
                    "description": (
                        "'Polish' if no issues were found and the report is ready "
                        "to finalize. 'Plan' to loop back and revise the research "
                        "because issues were found."
                    ),
                }
            },
            "required": ["target"],
        },
    },
}


class Stopped(Exception):
    """Raised internally when stop_event fires mid-pipeline."""


def _check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise Stopped()


def _noop(*args):
    pass


def _run_stage(client, model, system_prompt, user_text, options, tools_enabled,
                stage_label, stop_event, on_thinking, on_content, on_tool_call, on_notice):
    """Run one fresh, isolated stage turn and return its final text."""
    _check_stop(stop_event)
    convo = chat_mod.Conversation(client, model, system_prompt, tools_enabled, options)
    text = convo.send(
        user_text,
        on_content=lambda chunk: on_content(stage_label, chunk),
        on_thinking=lambda chunk: on_thinking(stage_label, chunk),
        on_tool_call=lambda name, args, result: on_tool_call(stage_label, name, args, result),
        on_notice=lambda message: on_notice(stage_label, message),
        stop_event=stop_event,
    )
    _check_stop(stop_event)
    return text


def _run_critique(client, model, user_text, options, stop_event, stage_label,
                   on_thinking, on_content, on_notice):
    """Run the critique stage. Returns (critique_text, target ('Polish'/'Plan')).

    Unlike the other stages, this talks to chat_stream directly instead of
    through chat.Conversation, so it doesn't get that class's "model
    rejected tool definitions" retry. Small models are shakiest exactly
    here (a real one hallucinated a call to a tool literally named "Plan"
    instead of calling route(target="Plan")), so any mid-stream OllamaError
    is caught and treated the same as "didn't call route": fail safe to
    Polish rather than aborting the whole research run.
    """
    _check_stop(stop_event)
    messages = [
        {"role": "system", "content": CRITIQUE_PROMPT},
        {"role": "user", "content": user_text},
    ]
    content_chunks = []
    target = None
    try:
        for event in client.chat_stream(
            model, messages, tools=[ROUTE_TOOL_SPEC], options=options, stop_event=stop_event
        ):
            if event["type"] == "thinking":
                on_thinking(stage_label, event["text"])
            elif event["type"] == "content":
                content_chunks.append(event["text"])
                on_content(stage_label, event["text"])
            elif event["type"] == "tool_calls":
                for tc in event["tool_calls"]:
                    fn = tc.get("function", {}) or {}
                    if fn.get("name") == "route":
                        target = (fn.get("arguments") or {}).get("target")
            elif event["type"] == "done":
                break
    except OllamaError as e:
        on_notice(stage_label, f"route call failed ({e}); defaulting to Polish.")
    _check_stop(stop_event)
    if target not in ("Polish", "Plan"):
        target = "Polish"  # fail safe: never loop forever if the model won't route
    return "".join(content_chunks), target


def run(client, model, topic, tools_enabled, options, stop_event=None, callbacks=None,
        voice_prompt=None):
    """Run the full research pipeline for `topic`.

    Returns the Polish stage's final report text, or None if stopped early.
    `callbacks` may define: on_stage_start(stage), on_thinking(stage, chunk),
    on_content(stage, chunk), on_tool_call(stage, name, args, result),
    on_stage_done(stage, text).

    `voice_prompt` is the user's own configured system prompt (already
    $date/$year-expanded). It only reaches the Polish stage, as style
    guidance layered on top of Polish's formatting rules -- the other
    stages need to stay narrow and mechanical (clean queries, citation-only
    facts, a parseable route call), so a chatty persona there would corrupt
    the pipeline rather than just coloring its tone.
    """
    callbacks = callbacks or {}
    on_stage_start = callbacks.get("on_stage_start", _noop)
    on_thinking = callbacks.get("on_thinking", _noop)
    on_content = callbacks.get("on_content", _noop)
    on_tool_call = callbacks.get("on_tool_call", _noop)
    on_stage_done = callbacks.get("on_stage_done", _noop)
    on_notice = callbacks.get("on_notice", _noop)

    no_tools = tools_mod.all_disabled()
    report, critique_text = "", ""

    try:
        plan_input = f"Topic: {topic}"
        for loop in range(MAX_CRITIQUE_LOOPS + 1):
            suffix = f" (revision {loop})" if loop else ""

            label = f"Plan{suffix}"
            on_stage_start(label)
            queries = _run_stage(
                client, model, PLAN_PROMPT, plan_input, options, no_tools,
                label, stop_event, on_thinking, on_content, on_tool_call, on_notice,
            )
            on_stage_done(label, queries)

            label = f"extract{suffix}"
            on_stage_start(label)
            extract_input = f"Original topic: {topic}\n\nSearch queries:\n{queries}"
            facts = _run_stage(
                client, model, EXTRACT_PROMPT, extract_input, options, tools_enabled,
                label, stop_event, on_thinking, on_content, on_tool_call, on_notice,
            )
            on_stage_done(label, facts)

            label = f"synthesize{suffix}"
            on_stage_start(label)
            report = _run_stage(
                client, model, SYNTHESIZE_PROMPT, facts, options, no_tools,
                label, stop_event, on_thinking, on_content, on_tool_call, on_notice,
            )
            on_stage_done(label, report)

            label = f"critique{suffix}"
            on_stage_start(label)
            critique_text, target = _run_critique(
                client, model, report, options, stop_event, label,
                on_thinking, on_content, on_notice,
            )
            on_stage_done(label, critique_text)

            if target == "Polish" or loop == MAX_CRITIQUE_LOOPS:
                break
            plan_input = (
                f"Original topic: {topic}\n\n"
                f"Previous report:\n{report}\n\n"
                f"Issues found:\n{critique_text}\n\n"
                "Revise the research plan to address these issues."
            )

        on_stage_start("Polish")
        polish_input = f"Report:\n{report}\n\nCritique notes:\n{critique_text}"
        if voice_prompt:
            polish_input = (
                "Write the document in the following voice/style, while still "
                "following all the formatting rules above:\n"
                f"{voice_prompt}\n\n---\n\n{polish_input}"
            )
        polished = _run_stage(
            client, model, POLISH_PROMPT, polish_input, options, no_tools,
            "Polish", stop_event, on_thinking, on_content, on_tool_call, on_notice,
        )
        on_stage_done("Polish", polished)
        return polished
    except Stopped:
        return None
