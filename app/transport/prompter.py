"""Questions claude asks, turned into something a chat can answer.

Two things arrive as `can_use_tool` requests: a permission prompt for a
tool call, and an `AskUserQuestion` -- the multiple-choice dialog the TUI
draws. An MCP server's `elicitation` request is a third: its form is walked
field by field and answered with `accept` / `decline` / `cancel`. Both become a message with buttons; a free-text option waits for the
next message from the same chat. When the last answer is in, the request is
answered over the control channel; when nobody answers in time, it is denied
with a reason so that claude carries on instead of hanging.

Everything here is chat-agnostic: the session decides how a `Prompt` looks
and feeds back button presses and typed lines.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.transport.driver import Driver, Event
from ui.i18n import t

ASK_TOOL = "AskUserQuestion"
# Leaving plan mode is a tool call the host approves, and approving it is
# also where the next mode is chosen -- the same two choices the TUI offers.
PLAN_TOOL = "ExitPlanMode"
PLAN_CHOICES = {"e": "acceptEdits", "m": "manual"}
CUSTOM_ANSWER = "__custom__"
INPUT_PREVIEW_LIMIT = 600
PLAN_PREVIEW_LIMIT = 3000
DONE_MARK = "✅"


@dataclass(frozen=True)
class Choice:
    label: str
    data: str


@dataclass(frozen=True)
class Prompt:
    """One message to show: text plus keyboard rows."""

    key: str
    request_id: str
    text: str
    rows: list[list[Choice]]
    multi: bool = False


@dataclass
class Outcome:
    """What the session should do after a press or a typed line."""

    ack: str = ""
    mode: str = ""  # the permission mode this answer switched claude into
    consumed: bool = False
    close_prompt: str = ""  # key of the prompt whose keyboard is done
    summary: str = ""  # a line to show in place of the keyboard
    next_prompt: Prompt | None = None
    finished_request: str = ""


@dataclass
class _Pending:
    number: int
    request_id: str
    tool_name: str
    tool_input: dict[str, Any]
    suggestions: list[dict[str, Any]]
    opened_at: float = field(default_factory=time.time)
    # An MCP elicitation: the request as it arrived, the form fields left to
    # ask about, and what has been filled in so far.
    elicit: dict[str, Any] | None = None
    fields: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    content: dict[str, Any] = field(default_factory=dict)
    questions: list[dict[str, Any]] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    index: int = 0
    selected: set[int] = field(default_factory=set)
    awaiting_text: bool = False

    @property
    def is_question(self) -> bool:
        return self.tool_name == ASK_TOOL

    @property
    def is_plan(self) -> bool:
        return self.tool_name == PLAN_TOOL

    @property
    def is_elicit(self) -> bool:
        return self.elicit is not None

    def key(self) -> str:
        return f"{self.number}:{self.index}"


class Prompter:
    def __init__(self, driver: Driver, *, timeout_seconds: float = 0.0) -> None:
        self.driver = driver
        self.timeout = timeout_seconds
        self._pending: dict[str, _Pending] = {}
        self._by_number: dict[int, _Pending] = {}
        self._counter = 0

    # -- state -------------------------------------------------------------

    @property
    def open(self) -> list[_Pending]:
        return list(self._pending.values())

    @property
    def awaiting_text(self) -> bool:
        return any(pending.awaiting_text for pending in self._pending.values())

    def current(self) -> _Pending | None:
        """The request a typed digit or a bare answer refers to."""
        for pending in self._pending.values():
            if pending.awaiting_text:
                return pending
        for pending in self._pending.values():
            return pending
        return None

    # -- arrivals ----------------------------------------------------------

    def on_ask(self, event: Event) -> Prompt:
        data = event.data
        self._counter += 1
        pending = _Pending(
            number=self._counter,
            request_id=str(data.get("request_id") or ""),
            tool_name=str(data.get("tool_name") or ""),
            tool_input=dict(data.get("input") or {}),
            suggestions=list(data.get("suggestions") or []),
        )
        if pending.is_question:
            questions = pending.tool_input.get("questions")
            pending.questions = [q for q in questions if isinstance(q, dict)] if isinstance(questions, list) else []
        self._pending[pending.request_id] = pending
        self._by_number[pending.number] = pending
        return self._prompt_for(pending)

    def on_elicit(self, event: Event) -> Prompt:
        data = event.data
        self._counter += 1
        pending = _Pending(
            number=self._counter,
            request_id=str(data.get("request_id") or ""),
            tool_name=str(data.get("server") or ""),
            tool_input={},
            suggestions=[],
            elicit=dict(data),
        )
        # A url elicitation has nothing to fill in: the person opens the link
        # and says when they are done.
        if str(data.get("mode") or "") != "url":
            schema = data.get("schema")
            properties = schema.get("properties") if isinstance(schema, dict) else None
            if isinstance(properties, dict):
                pending.fields = [(str(k), v) for k, v in properties.items() if isinstance(v, dict)]
        self._pending[pending.request_id] = pending
        self._by_number[pending.number] = pending
        return self._elicit_prompt(pending)

    def on_cancel(self, request_id: str) -> str:
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return ""
        self._by_number.pop(pending.number, None)
        return pending.key()

    def tick(self) -> list[Outcome]:
        """Expire requests nobody answered; returns what to show for each."""
        if not self.timeout:
            return []
        outcomes: list[Outcome] = []
        now = time.time()
        for pending in list(self._pending.values()):
            if now - pending.opened_at < self.timeout:
                continue
            self._finish(pending)
            if pending.is_elicit:
                self.driver.respond(pending.request_id, result={"action": "cancel"})
            else:
                self.driver.respond(
                    pending.request_id,
                    result={"behavior": "deny", "message": t("tg.prompt_timed_out_reason")},
                )
            outcomes.append(
                Outcome(close_prompt=pending.key(), summary=t("tg.prompt_timed_out"), finished_request=pending.request_id)
            )
        return outcomes

    # -- answers -----------------------------------------------------------

    def on_callback(self, data: str) -> Outcome:
        parts = data.split(":")
        if len(parts) != 4 or parts[0] != "a":
            return Outcome()
        try:
            number, index = int(parts[1]), int(parts[2])
        except ValueError:
            return Outcome()
        pending = self._by_number.get(number)
        if pending is None:
            return Outcome(ack=t("tg.prompt_gone"), consumed=True)
        if index != pending.index:
            return Outcome(ack=t("tg.prompt_stale"), consumed=True)
        return self._choose(pending, parts[3])

    def on_text(self, text: str) -> Outcome:
        """A typed line: the custom answer being waited for, or a digit that
        picks a button by its number -- what the local console does."""
        pending = self.current()
        if pending is None:
            return Outcome()
        if pending.awaiting_text:
            pending.awaiting_text = False
            return self._answer(pending, text.strip())
        probe = text.strip().lower()
        if pending.is_elicit:
            mapping = {"1": "y", "y": "y", "yes": "y", "2": "n", "n": "n", "no": "n"}
            return self._choose(pending, mapping[probe]) if probe in mapping else Outcome()
        if pending.is_question:
            options = self._options(pending)
            if probe.isdigit() and 1 <= int(probe) <= len(options):
                return self._choose(pending, str(int(probe) - 1))
            if probe.isdigit() and int(probe) == len(options) + 1:
                return self._choose(pending, "t")
            return Outcome()
        if pending.is_plan:
            mapping = {"1": "e", "2": "m", "3": "n", "n": "n", "no": "n"}
        else:
            mapping = {"1": "y", "y": "y", "yes": "y", "2": "s", "3": "n", "n": "n", "no": "n"}
        if probe in mapping:
            return self._choose(pending, mapping[probe])
        return Outcome()

    # -- internals ---------------------------------------------------------

    def _finish(self, pending: _Pending) -> None:
        self._pending.pop(pending.request_id, None)
        self._by_number.pop(pending.number, None)

    def _options(self, pending: _Pending) -> list[dict[str, Any]]:
        if pending.index >= len(pending.questions):
            return []
        options = pending.questions[pending.index].get("options")
        return [o for o in options if isinstance(o, dict)] if isinstance(options, list) else []

    def _question_text(self, pending: _Pending) -> str:
        return str(pending.questions[pending.index].get("question") or "")

    def _prompt_for(self, pending: _Pending) -> Prompt:
        if pending.is_elicit:
            return self._elicit_prompt(pending)
        if pending.is_question:
            return self._question_prompt(pending)
        if pending.is_plan:
            return self._plan_prompt(pending)
        return self._permission_prompt(pending)

    def _question_prompt(self, pending: _Pending) -> Prompt:
        question = pending.questions[pending.index] if pending.index < len(pending.questions) else {}
        header = str(question.get("header") or "")
        multi = bool(question.get("multiSelect"))
        options = self._options(pending)
        lines = [f"❓ <b>{_esc(header)}</b>" if header else "❓", _esc(self._question_text(pending))]
        if multi:
            lines.append(f"<i>{_esc(t('tg.prompt_multi_hint'))}</i>")
        rows: list[list[Choice]] = []
        for number, option in enumerate(options):
            label = str(option.get("label") or "")
            description = str(option.get("description") or "")
            lines.append(f"{number + 1}. <b>{_esc(label)}</b>" + (f" — {_esc(description)}" if description else ""))
            mark = f"{DONE_MARK} " if number in pending.selected else ""
            rows.append([Choice(f"{mark}{number + 1}. {label}"[:60], f"a:{pending.number}:{pending.index}:{number}")])
        tail: list[Choice] = [Choice(t("tg.prompt_custom"), f"a:{pending.number}:{pending.index}:t")]
        if multi:
            tail.insert(0, Choice(t("tg.prompt_done"), f"a:{pending.number}:{pending.index}:d"))
        rows.append(tail)
        if len(pending.questions) > 1:
            lines.insert(0, f"<i>{pending.index + 1}/{len(pending.questions)}</i>")
        return Prompt(pending.key(), pending.request_id, "\n".join(lines), rows, multi=multi)

    def _plan_prompt(self, pending: _Pending) -> Prompt:
        plan = str(pending.tool_input.get("plan") or "").strip()
        lines = [f"📋 <b>{_esc(t('tg.plan_ready'))}</b>"]
        if plan:
            lines.append(_esc(plan[:PLAN_PREVIEW_LIMIT]))
        rows = [
            [
                Choice(t("tg.plan_go"), f"a:{pending.number}:0:e"),
                Choice(t("tg.plan_ask"), f"a:{pending.number}:0:m"),
            ],
            [Choice(t("tg.plan_revise"), f"a:{pending.number}:0:n")],
        ]
        return Prompt(pending.key(), pending.request_id, "\n".join(lines), rows)

    def _permission_prompt(self, pending: _Pending) -> Prompt:
        lines = [f"🔐 <b>{_esc(pending.tool_name)}</b>", f"<pre>{_esc(describe_input(pending.tool_name, pending.tool_input))}</pre>"]
        rows = [
            [
                Choice(t("tg.perm_allow"), f"a:{pending.number}:0:y"),
                Choice(t("tg.perm_always"), f"a:{pending.number}:0:s"),
            ],
            [Choice(t("tg.perm_deny"), f"a:{pending.number}:0:n")],
        ]
        return Prompt(pending.key(), pending.request_id, "\n".join(lines), rows)

    def _elicit_field(self, pending: _Pending) -> tuple[str, dict[str, Any]]:
        if pending.index < len(pending.fields):
            return pending.fields[pending.index]
        return "", {}

    def _elicit_prompt(self, pending: _Pending) -> Prompt:
        info = pending.elicit or {}
        head = str(info.get("title") or info.get("display_name") or "") or t(
            "tg.elicit_title", server=str(info.get("server") or "")
        )
        lines = [f"🔐 <b>{_esc(head)}</b>"]
        for key in ("description", "message", "url"):
            value = str(info.get(key) or "").strip()
            if value:
                lines.append(_esc(value[:INPUT_PREVIEW_LIMIT]))
        cancel = [Choice(t("tg.elicit_cancel"), f"a:{pending.number}:{pending.index}:c")]

        name, spec = self._elicit_field(pending)
        if not name:
            done = t("tg.elicit_done") if info.get("url") else t("tg.elicit_accept")
            rows = [
                [
                    Choice(done, f"a:{pending.number}:0:y"),
                    Choice(t("tg.elicit_decline"), f"a:{pending.number}:0:d"),
                ],
                cancel,
            ]
            return Prompt(pending.key(), pending.request_id, "\n".join(lines), rows)

        lines.append(f"<b>{_esc(str(spec.get('title') or name))}</b>")
        if spec.get("description"):
            lines.append(_esc(str(spec["description"])[:INPUT_PREVIEW_LIMIT]))
        if len(pending.fields) > 1:
            lines.insert(0, f"<i>{pending.index + 1}/{len(pending.fields)}</i>")
        choices = spec.get("enum") if isinstance(spec.get("enum"), list) else None
        if spec.get("type") == "boolean":
            rows = [
                [
                    Choice(t("tg.elicit_yes"), f"a:{pending.number}:{pending.index}:y"),
                    Choice(t("tg.elicit_no"), f"a:{pending.number}:{pending.index}:n"),
                ],
                cancel,
            ]
        elif choices:
            rows = [
                [Choice(str(value)[:60], f"a:{pending.number}:{pending.index}:o{index}")]
                for index, value in enumerate(choices)
            ]
            rows.append(cancel)
        else:
            # Nothing to press: the answer is whatever the person types next.
            pending.awaiting_text = True
            lines.append(f"<i>{_esc(t('tg.elicit_type'))}</i>")
            rows = [cancel]
        return Prompt(pending.key(), pending.request_id, "\n".join(lines), rows)

    def _decide_elicit(self, pending: _Pending, option: str) -> Outcome:
        if option == "c":
            return self._settle_elicit(pending, {"action": "cancel"}, f"✖️ {_esc(t('tg.elicit_cancelled'))}")
        if option == "d":
            return self._settle_elicit(pending, {"action": "decline"}, f"⛔ {_esc(t('tg.elicit_declined'))}")
        name, spec = self._elicit_field(pending)
        if not name:
            if option != "y":
                return Outcome()
            return self._settle_elicit(
                pending,
                {"action": "accept", "content": pending.content},
                f"{DONE_MARK} {_esc(t('tg.elicit_accepted'))}",
            )
        if spec.get("type") == "boolean" and option in {"y", "n"}:
            return self._store_elicit(pending, option == "y", t("tg.elicit_yes") if option == "y" else t("tg.elicit_no"))
        if option.startswith("o"):
            choices = spec.get("enum") if isinstance(spec.get("enum"), list) else []
            try:
                index = int(option[1:])
            except ValueError:
                return Outcome()
            if not 0 <= index < len(choices):
                return Outcome()
            return self._store_elicit(pending, choices[index], str(choices[index]))
        return Outcome()

    def _answer_elicit(self, pending: _Pending, text: str) -> Outcome:
        name, spec = self._elicit_field(pending)
        if not name:
            return Outcome()
        kind = spec.get("type")
        value: Any = text
        if kind in {"number", "integer"}:
            try:
                value = int(text) if kind == "integer" else float(text)
            except ValueError:
                pending.awaiting_text = True
                return Outcome(ack=t("tg.elicit_need_number"), consumed=True)
        return self._store_elicit(pending, value, text)

    def _store_elicit(self, pending: _Pending, value: Any, shown: str) -> Outcome:
        name, spec = self._elicit_field(pending)
        pending.content[name] = value
        summary = f"{DONE_MARK} {_esc(str(spec.get('title') or name))} — <b>{_esc(shown)}</b>"
        key = pending.key()
        pending.index += 1
        pending.awaiting_text = False
        if pending.index < len(pending.fields):
            return Outcome(consumed=True, close_prompt=key, summary=summary, next_prompt=self._elicit_prompt(pending))
        outcome = self._settle_elicit(pending, {"action": "accept", "content": pending.content}, summary)
        return Outcome(consumed=True, close_prompt=key, summary=summary, finished_request=outcome.finished_request)

    def _settle_elicit(self, pending: _Pending, result: dict[str, Any], summary: str) -> Outcome:
        self._finish(pending)
        self.driver.respond(pending.request_id, result=result)
        return Outcome(consumed=True, close_prompt=pending.key(), summary=summary, finished_request=pending.request_id)

    def _choose(self, pending: _Pending, option: str) -> Outcome:
        if pending.is_elicit:
            return self._decide_elicit(pending, option)
        if not pending.is_question:
            return self._decide_permission(pending, option)

        if option == "t":
            pending.awaiting_text = True
            return Outcome(ack=t("tg.prompt_type_answer"), consumed=True)
        if option == "d":
            options = self._options(pending)
            chosen = [str(options[i].get("label") or "") for i in sorted(pending.selected) if i < len(options)]
            if not chosen:
                return Outcome(ack=t("tg.prompt_pick_one"), consumed=True)
            return self._answer(pending, ", ".join(chosen))
        try:
            index = int(option)
        except ValueError:
            return Outcome()
        options = self._options(pending)
        if not 0 <= index < len(options):
            return Outcome()
        question = pending.questions[pending.index]
        if question.get("multiSelect"):
            if index in pending.selected:
                pending.selected.discard(index)
            else:
                pending.selected.add(index)
            return Outcome(ack=t("tg.prompt_toggled"), consumed=True, next_prompt=self._question_prompt(pending), close_prompt=pending.key())
        return self._answer(pending, str(options[index].get("label") or ""))

    def _answer(self, pending: _Pending, answer: str) -> Outcome:
        if pending.is_elicit:
            return self._answer_elicit(pending, answer)
        if not pending.is_question:
            return Outcome()
        pending.answers[self._question_text(pending)] = answer
        summary = f"{DONE_MARK} {_esc(self._question_text(pending))} — <b>{_esc(answer)}</b>"
        pending.index += 1
        pending.selected = set()
        if pending.index < len(pending.questions):
            return Outcome(consumed=True, close_prompt=f"{pending.number}:{pending.index - 1}", summary=summary, next_prompt=self._question_prompt(pending))
        self._finish(pending)
        self.driver.respond(
            pending.request_id,
            result={"behavior": "allow", "updatedInput": {**pending.tool_input, "answers": pending.answers}},
        )
        return Outcome(consumed=True, close_prompt=f"{pending.number}:{pending.index - 1}", summary=summary, finished_request=pending.request_id)

    def _decide_permission(self, pending: _Pending, option: str) -> Outcome:
        if pending.is_plan and option in PLAN_CHOICES:
            mode = PLAN_CHOICES[option]
            result = {
                "behavior": "allow",
                "updatedInput": pending.tool_input,
                # Approving the plan *is* the mode switch: claude applies the
                # update itself, exactly as the TUI's "how do you want to
                # proceed" does.
                "updatedPermissions": [
                    {"type": "setMode", "mode": mode, "destination": "session"}
                ],
            }
            self._finish(pending)
            self.driver.respond(pending.request_id, result=result)
            return Outcome(
                consumed=True,
                close_prompt=pending.key(),
                summary=f"{DONE_MARK} {_esc(t('tg.plan_accepted', mode=mode))}",
                finished_request=pending.request_id,
                mode=mode,
            )

        if option == "n":
            reason = t("tg.plan_revise_reason") if pending.is_plan else t("tg.perm_denied_reason")
            result: dict[str, Any] = {"behavior": "deny", "message": reason}
            summary = f"⛔ {_esc(pending.tool_name)} — {_esc(t('tg.perm_deny'))}"
        elif option in {"y", "s"}:
            result = {"behavior": "allow", "updatedInput": pending.tool_input}
            if option == "s" and pending.suggestions:
                result["updatedPermissions"] = pending.suggestions
            summary = f"{DONE_MARK} {_esc(pending.tool_name)} — {_esc(t('tg.perm_always') if option == 's' else t('tg.perm_allow'))}"
        else:
            return Outcome()
        self._finish(pending)
        self.driver.respond(pending.request_id, result=result)
        return Outcome(consumed=True, close_prompt=pending.key(), summary=summary, finished_request=pending.request_id)


def describe_input(tool_name: str, tool_input: dict[str, Any]) -> str:
    """The one thing a human wants to see before saying yes."""
    for key in ("command", "file_path", "path", "url", "pattern", "notebook_path", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            if key == "command":
                return value[:INPUT_PREVIEW_LIMIT]
            extra = ""
            if tool_name in {"Edit", "Write", "NotebookEdit"}:
                body = tool_input.get("new_string") or tool_input.get("content") or tool_input.get("new_source") or ""
                if isinstance(body, str) and body:
                    extra = "\n" + body[: INPUT_PREVIEW_LIMIT // 2]
            return f"{key}: {value}{extra}"[:INPUT_PREVIEW_LIMIT]
    try:
        return json.dumps(tool_input, ensure_ascii=False)[:INPUT_PREVIEW_LIMIT]
    except (TypeError, ValueError):
        return str(tool_input)[:INPUT_PREVIEW_LIMIT]


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
