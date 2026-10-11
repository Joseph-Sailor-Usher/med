# med — the Cognitive Mediator

> A living interface between **intent** (a sentence) and **execution** (local
> models + files). Resolve, dispatch, verify — with a JSON contract in the
> middle.

`med` is the working first artifact of the **Cognitive_Mediator** research spec
(lives at `~/Documents/Learning/Autodidactism/Research/Cognitive_Mediator/`).
It is **not** an OS and not "another agent framework." It is a
small, honest CLI with one job: turn a human intent into a **Blueprint** you can
see and inspect, execute it against your **local Ollama** models, and **verify
each step against a spec** rather than trusting the model's self-report.

Pure **Python 3.10+ stdlib** — no `pip install` of runtime deps needed. Talks
to Ollama over plain `urllib` HTTP.

## The three pillars

| Pillar     | What it does                                              | Where                  |
|------------|-----------------------------------------------------------|------------------------|
| **Resolve**| intent → declarative JSON Blueprint (an FSM of steps)     | `med/core.py :: Resolver` |
| **Dispatch**| run the Blueprint, routing `model` steps to Ollama        | `med/core.py :: Dispatcher` |
| **Verify**  | judge each step's output against its `spec` (pass/fail)   | `med/core.py :: verify` |

The **Blueprint** is the single contract between them — plain JSON. That
decoupling (intelligence / execution / judgement) is the Mediator pattern:
the model decides the plan **once**; the engine runs and judges it without
re-invoking the model at every step.

## Install

```bash
cd med
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # stdlib-only at runtime
# or run in place:
python3 -m med.cli --help
```

## Connectivity

```bash
med models                 # reach Ollama, list models, mark the default
```

## End-to-end (resolves *and* executes)

```bash
# From a file:
med run "make 5 flashcards about quantum entanglement" \
    --source path/to/note.md --count 5 --out cards.json

# Inline text:
med run "summarize this" --source "FSRS models memory as (D,S,R)..." --out brief.md

# Full machine-readable transcript (blueprint + per-step results + verdict):
med run "make 3 flashcards about X" --source note.md --out cards.json --json
```

Exit codes: `0` all steps verified · `2` a hard step failed · `1` setup error.

## Self-correction (verify → correct → verify)

`med` does not just fail on bad output — it gives the model the failure
reason and a bounded chance to fix it:

```
author (model step, retries=2)
   attempt 0  ->  "min_items: 1 item(s) < required 4"
   attempt 1  ->  same spec, with the rejection appended to the prompt
   attempt 2  ->  (last attempt) still failing?  ->  step fails, run halts
```

```bash
# default is 2 retries; override per-run:
med run "make 4 flashcards ... " --source note.md --count 4 --retries 3
```

Rules:

- Only **non-soft `model` steps** with a `retries` count in `params` retry.
- The first *clean* attempt is the one that carries forward — retry output
  never poisons a step that already passed.
- Retries are bounded (the `attempts` counter) — no infinite loops.
- `soft: true` steps record the failure and keep going (no retries by
  default); hard steps stop the run with exit code `2`.

The verifier's message is written for the *model*, not the human:
`"min_items: required a JSON ARRAY of 4 objects but got a dict -- return
ONLY an array like: [{front,back}, ...] (no wrapper object)"`.

## Resolve-only (see the Blueprint, don't execute)

```bash
med resolve "make 5 flashcards about X" --source note.md --count 5 | jq .
```

This is the "compiled plan" — inspect it, edit it, or hand it to another tool.

## The Blueprint contract

```bash
med blueprint
```

Each **step** is:

```json
{
  "id": "author",
  "op": "model | passthrough | read | write | transform",
  "params": { "system": "...", "prompt": "..." },
  "spec": { "valid_json": true, "min_items": 4, "must_contain_all": ["front"] },
  "source": "${prev}",
  "soft": false
}
```

`spec` is how we *verify instead of trust* — `min_chars`, `max_chars`,
`min_items` (for JSON arrays: enforce a minimum count), `valid_json`,
`json_keys`, `must_contain_all/any`, `regex`. A step that fails its spec stops
the run (`soft: true` lets best-effort passes continue and record the failure
instead).

## Design intent (why it exists)

This is deliberately small so you can *read the whole thing* and then grow it:

1. **Programs** — deterministic, offline, named capabilities (flashcards,
   summarize) that never touch the network. The resolver tries these first.
2. **LLM planning** — for novel intents, the model emits a Blueprint that
   matches the schema; the resolver *validates* the shape before it runs.
3. **Verifier is independent** — the model never grades itself; `verify()` is
   plain Python comparing output vs. `spec`.

Next growth candidates (already in the research vault): an MCP server front-end
so *another* agent can drive `med`; a `run` that fans a step out to multiple
models and lets the Verifier pick the winner; filesystem + Obsidian-vault `read`/`write`
ops (already present as `read`/`write`).

## Repo layout

```
med/
├─ pyproject.toml     # no runtime deps; entry point `med` -> med.cli:main
├─ med/
│  ├─ __init__.py
│  ├─ core.py         # Blueprint, Resolver, Dispatcher, Verifier, OllamaClient
│  └─ cli.py          # argparse front-end (models / resolve / run / blueprint)
└─ README.md
```

## License

MIT
