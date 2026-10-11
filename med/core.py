"""med.core -- the engine behind the three pillars.

    Resolver   intent (natural language)  ->  Blueprint (JSON FSM)
    Dispatcher Blueprint                  ->  executed steps (Ollama / files / passthrough)
    Verifier   each step result           ->  pass/fail against the step's spec

The Blueprint is the single contract between them: a plain JSON document. That
decoupling is the whole point of the Mediator pattern -- intelligence (the LLM
deciding the plan) is separated from execution (deterministic step running) and
from judgement (independent verification against the spec).
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional

SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------- #
#  Blueprint (the contract)
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    id: str
    op: str                      # "model" | "read" | "write" | "passthrough" | "transform"
    params: dict = field(default_factory=dict)

    # Verification spec. Empty dict == "must be non-empty".
    spec: dict = field(default_factory=dict)

    # Optional: what feeds this step.
    #   "${prev}"       -> the previous step's output
    #   "${input:<key>}"-> a key from the run's input context
    #   otherwise literal
    source: str = "${prev}"

    # When False, a failing verification aborts the run. When True, we record
    # the failure and continue to the next step (use for best-effort passes).
    soft: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Blueprint:
    intent: str
    goal: str
    steps: list
    # Static context handed into the run (not derived from LLM).
    context: dict = field(default_factory=dict)
    model: str = ""
    base_url: str = "http://127.0.0.1:11434"
    schema: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "intent": self.intent,
            "goal": self.goal,
            "model": self.model,
            "base_url": self.base_url,
            "context": self.context,
            "steps": [s.to_dict() for s in self.steps],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Blueprint":
        steps = [Step(**s) if isinstance(s, dict) else s for s in d.get("steps", [])]
        return cls(
            intent=d.get("intent", ""),
            goal=d.get("goal", ""),
            steps=steps,
            context=d.get("context", {}),
            model=d.get("model", ""),
            base_url=d.get("base_url", "http://127.0.0.1:11434"),
            schema=d.get("schema", SCHEMA_VERSION),
        )


# --------------------------------------------------------------------------- #
#  Resolver -- intent -> Blueprint
# --------------------------------------------------------------------------- #
class Resolver:
    """Turns a natural-language intent into a Blueprint.

    Two strategies, tried in order:
      1. A deterministic Program (a named, built-in capability). Fast, offline,
         and always valid -- this is the "compiled" path of the Mediator pattern.
      2. LLM planning: ask the local model to emit a Blueprint that matches our
         schema. Fallback if no program matches, or to handle novel intent.
    """

    def __init__(self, model: str, base_url: str, llm: Optional["OllamaClient"] = None):
        self.model = model
        self.base_url = base_url
        self._llm = llm or OllamaClient(base_url, model)

    def resolve(self, intent: str, **params) -> Blueprint:
        bp = self._try_program(intent, **params)
        if bp is not None:
            return bp
        return self._plan_with_llm(intent, **params)

    # -- deterministic programs -------------------------------------------- #
    def _try_program(self, intent: str, **params) -> Optional[Blueprint]:
        text = intent.lower()
        p = params
        if "flashcard" in text or "card" in text:
            if p.get("source"):
                return self._program_flashcards(intent, p)
        if "summarize" in text or "summary" in text:
            if p.get("source"):
                return self._program_summarize(intent, p)
        return None

    def _program_flashcards(self, intent: str, p: dict) -> Blueprint:
        src = p["source"]
        n = int(p.get("count", 5))
        system = (
            "You are an expert flashcard author using the FSRS spaced-repetition "
            "method. Produce ATOMIC cards: one idea per card, minimal wording, "
            "unambiguous front and back. Return ONLY a JSON ARRAY of exactly the "
            "requested number of objects, each with keys {front, back}. No prose, "
            "no markdown fences, no wrapper object."
        )
        prompt = (
            f"Create EXACTLY {n} distinct, high-quality flashcards from the material "
            f"below. Each card tests understanding, not trivia recall. "
            f"Output a JSON array of length {n}.\n\n"
            f"MATERIAL:\n{src}"
        )
        return Blueprint(
            intent=intent,
            goal=f"Generate {n} atomic FSRS flashcards from: {src[:80]}",
            model=p.get("model") or self.model,
            base_url=self.base_url,
            context={"source": src, "count": n},
            steps=[
                Step(id="read-source", op="passthrough", source="${input:source}",
                     spec={"min_chars": 1}),
                Step(id="author", op="model",
                     params={"system": system, "prompt": prompt,
                             "retries": int(p.get("retries", 2))},
                     # min_items lives HERE (not only on check-count) so a
                     # short array or a lone object lands on a RETRYABLE step:
                     # the model sees "min_items: 1 item(s) < required 4" and
                     # corrects itself instead of the run dying downstream.
                     spec={"valid_json": True, "regex": r'"front"',
                           "min_items": n}),
                Step(id="normalize", op="transform",
                     params={"transform": "json_array"},
                     spec={}),
                Step(id="check-count", op="passthrough",
                     spec={"valid_json": True, "min_items": n}),
                Step(id="export", op="write",
                     params={"path": p.get("out") or "${cwd}/flashcards.json",
                            "pretty": True},
                     spec={"min_chars": 1}),
            ],
        )

    def _program_summarize(self, intent: str, p: dict) -> Blueprint:
        src = p["source"]
        system = ("Summarize the material in a crisp, faithful brief. Return prose "
                  "only (no JSON, no fence).")
        prompt = f"Summarize the following:\n\n{src}"
        out = p.get("out") or "${cwd}/summary.md"
        return Blueprint(
            intent=intent, goal=f"Summarize: {src[:80]}",
            model=p.get("model") or self.model, base_url=self.base_url,
            context={"source": src},
            steps=[
                Step(id="read-source", op="passthrough", source="${input:source}",
                     spec={"min_chars": 1}),
                Step(id="summarize", op="model",
                     params={"system": system, "prompt": prompt},
                     spec={"min_chars": 20}),
                Step(id="export", op="write",
                     params={"path": out, "pretty": False}, spec={"min_chars": 1}),
            ],
        )

    # -- LLM planning ------------------------------------------------------- #
    _SCHEMA_HINT = """
Return ONLY valid JSON with this exact shape (no markdown, no comments):
{
  "goal": "one sentence",
  "steps": [
     {"id": "s1",
      "op": "model" | "passthrough" | "write" | "transform",
      "params": { },          // see op rules below
      "spec": {"min_chars": 10},   // verification spec (optional)
      "source": "${prev}"     // what feeds this step (default ${prev})
    }
  ]
}
op rules:
  model       params: {system, prompt};   spec may use {valid_json, regex}
  passthrough no params; copies ${prev}
  write       params: {path}; writes ${prev} to path
Keep it to at most 3 steps. Use ${prev} to chain steps.
"""

    def _plan_with_llm(self, intent: str, **params) -> Blueprint:
        sys = ("You plan agentic workflows as strict JSON blueprints. "
               "Follow the schema EXACTLY. Do not narrate.")
        user = f"Intent: {intent}\nContext available: {json.dumps({k: v for k, v in params.items() if k != 'source'})}\n{self._SCHEMA_HINT}"
        out = self._llm.chat(sys, user, json_mode=True)
        data = _strip_and_load_json(out)
        bp = Blueprint(
            intent=intent,
            goal=data.get("goal", intent),
            model=params.get("model") or self.model,
            base_url=self.base_url,
            context=params,
            steps=[Step(**s) for s in data.get("steps", [])],
        )
        self._validate(blueprint=bp)
        return bp

    def _validate(self, blueprint: Blueprint) -> None:
        if not blueprint.steps:
            raise BlueprintError("blueprint has no steps")
        allowed = {"model", "passthrough", "read", "write", "transform", "normalize"}
        for s in blueprint.steps:
            if s.op not in allowed:
                raise BlueprintError(f"step {s.id}: unknown op '{s.op}'")


# --------------------------------------------------------------------------- #
#  Ollama client (stdlib, no deps)
# --------------------------------------------------------------------------- #
class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: int = 600):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> Any:
        url = self.base_url + path
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def chat(self, system: str, user: str, json_mode: bool = False,
             temperature: float = 0.2, max_tokens: int = 3000) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": temperature,
                        "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        data = self._post("/api/chat", payload)
        return data["message"]["content"]

    def list_models(self) -> list:
        with urllib.request.urlopen(self.base_url + "/api/tags", timeout=10) as r:
            data = json.loads(r.read().decode())
        return [m.get("name") for m in data.get("models", [])]


# --------------------------------------------------------------------------- #
#  Dispatcher -- Blueprint -> executed steps
# --------------------------------------------------------------------------- #
@dataclass
class StepResult:
    id: str
    op: str
    status: str            # "ok" | "fail" | "soft-fail"
    detail: str
    output: Any = None

    def ok(self) -> bool:
        return self.status == "ok"


class Dispatcher:
    def __init__(self, llm: OllamaClient, verbose: bool = True):
        self.llm = llm
        self.verbose = verbose
        self._log: list = []

    def _say(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    def run(self, bp: Blueprint, input_ctx: Optional[dict] = None) -> dict:
        """Execute the blueprint step-by-step, with bounded self-correction.

        A non-soft MODEL step that declares 'retries' is re-run when the
        verifier rejects it: the failure reason is appended so the model can
        correct itself. A clean attempt's output is what carries forward. A
        hard failure after all attempts stops the run and sets run_ok=False.
        """
        input_ctx = {**(bp.context or {}), **(input_ctx or {})}
        prev = None
        results: list = []
        nsteps = len(bp.steps)
        for i, step in enumerate(bp.steps):
            if self.verbose:
                self._say(f"  [{i+1}/{nsteps}] {step.id}  (op={step.op})")
            if step.op == "model":
                # For model steps the primary user content is step.params.prompt.
                # On retry we replace this with a correction message.
                attempt_in = step.params.get("prompt") or self._resolve_source(step, prev, input_ctx)
            else:
                attempt_in = self._resolve_source(step, prev, input_ctx)
            final = None
            status = None
            why = None
            attempts = 0
            while True:
                try:
                    out, err = self._exec(step, attempt_in, bp)
                except Exception as e:  # noqa: BLE001 -- report, don't crash mid-run
                    out, err = None, f"{type(e).__name__}: {e}"
                ok = err is None
                if ok:
                    ok, why = verify(out, step.spec)
                if ok:
                    final, status = out, "ok"
                    break
                attempts += 1
                can_retry = (step.op == "model"
                             and attempts <= int(step.params.get("retries", 0)))
                if can_retry:
                    if self.verbose:
                        self._say(f"            retry {attempts} -- {why}")
                    nl = "\n\n"
                    attempt_in = ((out or "").strip() + nl
                                  + f"CORRECTION -- rejected: {why}. "
                                  + "Return a fresh, fully-corrected answer (no prose, no fences).")
                    continue
                status = "soft-fail" if step.soft else "fail"
                final = out
                if why is None:
                    why = err or "verification failed"
                break
            results.append(StepResult(step.id, step.op, status, why or "", final))
            preview = why if why else (final[:120] if isinstance(final, str) else str(final)[:120])
            self._say(f"      -> {status:9} {preview!r}")
            if status == "fail":
                break
            prev = final
        run_ok = all(r.ok() for r in results)
        return {
            "run_ok": run_ok,
            "blueprint": bp.to_dict(),
            "results": [asdict(r) for r in results],
        }

    def _resolve_source(self, step, prev, input_ctx):
        s = step.source
        if s == "${prev}":
            return prev
        if s.startswith("${input:"):
            key = s[len("${input:"): -1]
            return input_ctx.get(key)
        return s  # literal

    def _exec(self, step, value, bp):
        op = step.op
        if op == "model":
            # `value` is whatever `run()` decided is the user content for this
            # attempt: the original prompt on the first try, a correction
            # message on a retry. Fall back to step.params.prompt if unset.
            user_msg = value if (value is not None and str(value).strip()) else step.params.get("prompt", "")
            # Ollama's format:"json" constrains the *shape* to "some valid
            # JSON object"; for array-requiring specs (min_items) it actively
            # pushes the model toward the simplest valid form — a single
            # object — which is the opposite of what we want. Only enable it
            # when the spec wants an object-shaped JSON.
            json_mode = bool("valid_json" in step.spec) and "min_items" not in step.spec
            content = self.llm.chat(step.params.get("system", ""),
                                    user_msg,
                                    json_mode=json_mode,
                                    temperature=step.params.get("temperature", 0.2),
                                    max_tokens=step.params.get("max_tokens", 3000))
            return content, None
        if op == "passthrough":
            return value, None
        if op == "read":
            path = str(step.params.get("path") or value)
            with open(path, "r", encoding="utf-8") as f:
                return f.read(), None
        if op == "write":
            path = str(step.params.get("path") or value)
            text = _pretty(value, step.params.get("pretty"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return path, None
        if op == "transform":
            # minimal transforms chosen by params.transform
            name = step.params.get("transform", "trim")
            if name == "trim":
                return (value or "").strip(), None
            if name == "strip_fence":
                return _strip_fence(value), None
            if name == "json_array":
                obj = json.loads(value) if isinstance(value, str) else value
                if isinstance(obj, dict):
                    obj = [obj]
                return json.dumps(obj, ensure_ascii=False), None
            raise BlueprintError(f"unknown transform '{name}'")
        raise BlueprintError(f"unknown op '{op}'")


# --------------------------------------------------------------------------- #
#  Verifier -- result vs spec
# --------------------------------------------------------------------------- #
def verify(output: Any, spec: dict) -> tuple[bool, str]:
    if output is None:
        return False, "no output"
    if isinstance(output, str):
        text = output
    else:
        text = output if isinstance(output, str) else json.dumps(output)
    if not str(output).strip() and "min_chars" not in spec:
        return False, "empty output"

    mn = spec.get("min_chars")
    if mn is not None and len(str(output)) < mn:
        return False, f"length {len(str(output))} < min_chars {mn}"
    mx = spec.get("max_chars")
    if mx is not None and len(str(output)) > mx:
        return False, f"length {len(str(output))} > max_chars {mx}"

    min_items = spec.get("min_items")
    if min_items is not None:
        try:
            obj = json.loads(output) if isinstance(output, str) else output
        except Exception as e:  # noqa: BLE001
            return False, f"min_items: not parseable as JSON ({e})"
        if not isinstance(obj, list):
            return False, (f"min_items: required a JSON ARRAY of {min_items} objects "
                           f"but got a {type(obj).__name__} -- return ONLY an array "
                           f"like: [{ '{front,back}' }, { '{front,back}' }] (no wrapper object)")
        if len(obj) < min_items:
            return False, f"min_items: {len(obj)} item(s) < required {min_items}"

    if spec.get("valid_json"):
        try:
            json.loads(output)
        except Exception as e:  # noqa: BLE001
            return False, f"not valid JSON: {e}"
    if spec.get("json_keys"):
        try:
            obj = json.loads(text) if isinstance(text, str) else text
            for k in spec["json_keys"]:
                if isinstance(obj, dict) and k not in obj:
                    return False, f"missing key '{k}'"
        except Exception:
            pass  # already surfaced via valid_json when set
    mca = spec.get("must_contain_all")
    if mca:
        for token in mca:
            if token not in text:
                return False, f"missing required text: {token!r}"
    mcy = spec.get("must_contain_any")
    if mcy and not any(t in text for t in mcy):
        return False, f"needs one of: {mcy!r}"
    rx = spec.get("regex")
    if rx and not re.search(rx, text):
        return False, f"regex not matched: {rx!r}"
    return True, ""


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
class BlueprintError(Exception):
    pass


def _strip_fence(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```[a-zA-Z]*\n(.*)\n?```$", t, re.S)
    return m.group(1).strip() if m else t


def _pretty(value: Any, pretty: bool) -> str:
    """Serialize a step output for the `write` op without corrupting it.

    Strings are written verbatim (a model's JSON is already formatted; re-dumping
    it as a string would wrap the whole thing in escaped double-quotes). Native
    data structures (list/dict) are dumped as JSON.
    """
    if isinstance(value, str):
        return value
    if pretty:
        return json.dumps(value, ensure_ascii=False, indent=2)
    return json.dumps(value, ensure_ascii=False)


def _strip_and_load_json(text: str):
    return json.loads(_strip_fence(text))
