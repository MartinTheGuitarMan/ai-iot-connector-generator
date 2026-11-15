"""Authoring loop: protocol doc -> LLM -> ProtocolSpec YAML -> eval -> retry.

The model is injected as a plain callable so the loop is testable offline:

    call_model(system: str, messages: list[dict]) -> ModelReply

Feedback modes control what the model learns about a failed attempt:
  "errors"  validation/codegen/import errors, plus pass count and the
            labels of failing cases (default; does not reveal expected output)
  "diff"    additionally shows expected vs. actual canonical output for
            failing cases (turns the loop into debugging against the answer
            key; report it separately from "errors" results)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from authoring.eval import PROTOCOLS, EvalResult, evaluate
from authoring.prompt import SYSTEM_PROMPT, USER_TEMPLATE

DEFAULT_MODEL = "claude-opus-5-5"
FEEDBACK_MODES = ("errors", "diff")


@dataclass
class ModelReply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None


CallModel = Callable[[str, list[dict]], ModelReply]


@dataclass
class Attempt:
    n: int
    reply_text: str
    spec_yaml: str | None
    result: EvalResult | None
    note: str | None = None  # e.g. "no yaml block", "refusal"
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class AuthoringRun:
    protocol: str
    feedback: str
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.attempts) and self.attempts[-1].result is not None and self.attempts[-1].result.ok

    @property
    def attempts_used(self) -> int:
        return len(self.attempts)

    @property
    def final_spec(self) -> str | None:
        return self.attempts[-1].spec_yaml if self.attempts else None


_FENCE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL)


def extract_yaml(text: str) -> str | None:
    blocks = _FENCE.findall(text)
    return blocks[-1].strip() + "\n" if blocks else None


def _feedback_message(result: EvalResult, mode: str) -> str:
    if result.stage != "ok":
        return (
            f"Your spec failed at the '{result.stage}' stage:\n{result.error}\n\n"
            "Fix it and reply with the complete corrected YAML in one ```yaml block."
        )
    lines = [
        f"Your spec loads and generates a connector, but it disagrees with the "
        f"known-good behaviour on {len(result.failures)} of {result.total} test inputs "
        f"({result.passed} pass).",
        "Failing cases: " + ", ".join(f.case for f in result.failures[:12]),
    ]
    if mode == "diff":
        for f in result.failures[:5]:
            lines.append(f"\n[{f.case}]\n  expected: {f.expected}\n  got:      {f.got}")
    lines.append(
        "\nRe-read the protocol doc, correct the spec, and reply with the complete "
        "corrected YAML in one ```yaml block."
    )
    return "\n".join(lines)


def author_spec(
    protocol: str,
    call_model: CallModel,
    max_attempts: int = 3,
    feedback: str = "errors",
) -> AuthoringRun:
    if feedback not in FEEDBACK_MODES:
        raise ValueError(f"feedback must be one of {FEEDBACK_MODES}")
    doc = PROTOCOLS[protocol].doc.read_text()
    messages: list[dict] = [{"role": "user", "content": USER_TEMPLATE.format(doc=doc)}]
    run = AuthoringRun(protocol=protocol, feedback=feedback)

    for n in range(1, max_attempts + 1):
        reply = call_model(SYSTEM_PROMPT, messages)
        attempt = Attempt(n, reply.text, None, None,
                          input_tokens=reply.input_tokens, output_tokens=reply.output_tokens)
        run.attempts.append(attempt)

        if reply.stop_reason in ("refusal", "max_tokens"):
            attempt.note = reply.stop_reason
            break  # not recoverable by re-prompting the same way

        attempt.spec_yaml = extract_yaml(reply.text)
        messages.append({"role": "assistant", "content": reply.text})
        if attempt.spec_yaml is None:
            attempt.note = "no yaml block"
            messages.append({"role": "user", "content":
                             "I could not find a ```yaml block. Reply with the complete spec in one."})
            continue

        attempt.result = evaluate(protocol, attempt.spec_yaml)
        if attempt.result.ok:
            break
        messages.append({"role": "user", "content": _feedback_message(attempt.result, feedback)})
    return run


def anthropic_caller(model: str = DEFAULT_MODEL, max_tokens: int = 16000) -> CallModel:
    """CallModel backed by the Anthropic API (needs credentials in the environment)."""
    import anthropic

    client = anthropic.Anthropic()

    def call(system: str, messages: list[dict]) -> ModelReply:
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages,
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        return ModelReply(text, resp.usage.input_tokens, resp.usage.output_tokens, resp.stop_reason)

    return call
