"""med -- command-line entry point.

Usage:
  med models                              # list Ollama models (connectivity check)
  med resolve "make 5 flashcards ..."     # print the JSON blueprint only (no dispatch)
  med run "make 5 flashcards ..."         # resolve + dispatch + verify
        --source PATH|TEXT   material to author from (or inline TEXT)
        --count 5            program-specific param (flashcards)
        --out PATH           output file path (default ${cwd}/<name>)
        --model NAME         Ollama model (default from env MED_MODEL or qwen3.8:27b)
        --base-url URL       Ollama base (default http://127.0.0.1:11434)
        --json               emit the full run JSON (blueprint+results) to stdout
  med blueprint                           # print the Blueprint schema/contract
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .core import (
    OllamaClient, Resolver, Dispatcher, Blueprint, BlueprintError,
)

DEFAULT_MODEL = os.environ.get("MED_MODEL", "qwen3.8:27b")
DEFAULT_BASE = os.environ.get("MED_BASE_URL", "http://127.0.0.1:11434")


def _build(args):
    base = args.base_url or DEFAULT_BASE
    model = args.model or DEFAULT_MODEL
    llm = OllamaClient(base, model)
    return llm, model, base


def cmd_models(args) -> int:
    llm, model, base = _build(args)
    try:
        names = llm.list_models()
    except Exception as e:  # noqa: BLE001
        print(f"error: cannot reach Ollama at {base}: {e}", file=sys.stderr)
        return 1
    if not names:
        print("(no models installed)")
        return 0
    print(f"Ollama at {base} -- {len(names)} model(s):")
    for n in names:
        mark = "  *" if n == model else "   "
        print(f"{mark} {n}")
    print(f"    ('*' = MED_MODEL / --model = {model})")
    return 0


def _load_source(args) -> str:
    raw = args.source
    if not raw:
        return ""
    # inline text (starts with non-path) vs file path
    if os.path.exists(raw):
        with open(raw, "r", encoding="utf-8") as f:
            return f.read()
    return raw


def _params(args) -> dict:
    p = {}
    src = _load_source(args)
    if src:
        p["source"] = src
    if getattr(args, "count", None):
        p["count"] = args.count
    if getattr(args, "out", None):
        p["out"] = args.out
    if args.model or os.environ.get("MED_MODEL"):
        p["model"] = args.model or DEFAULT_MODEL
    if getattr(args, "retries", None) is not None:
        p["retries"] = args.retries
    return p


def cmd_resolve(args) -> int:
    llm, model, base = _build(args)
    resolver = Resolver(model, base, llm)
    try:
        bp = resolver.resolve(args.intent, **_params(args))
    except BlueprintError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(bp.to_json())
    return 0


def cmd_run(args) -> int:
    llm, model, base = _build(args)
    resolver = Resolver(model, base, llm)
    try:
        bp = resolver.resolve(args.intent, **_params(args))
    except BlueprintError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.json:
        print("# blueprint", file=sys.stderr)
        print("  " + json.dumps(bp.to_dict()["steps"], ensure_ascii=False))
    dispatcher = Dispatcher(llm, verbose=not args.json)
    result = dispatcher.run(bp)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print()
        if result["run_ok"]:
            print(f"RUN OK -- {len(result['results'])} step(s) verified")
            for r in result["results"]:
                last = r["output"]
                if r["op"] == "write" and isinstance(last, str):
                    print(f"  wrote: {last}")
        else:
            print("RUN FAILED at first non-soft-fail step:")
            for r in result["results"]:
                if r["status"] != "ok":
                    print(f"  step {r['id']}: {r['detail']}")
                    break
    return 0 if result["run_ok"] else 2


def cmd_blueprint(args) -> int:
    print(json.dumps({
        "contract": "med Blueprint (schema 1.0)",
        "purpose": "single JSON document between Resolver / Dispatcher / Verifier",
        "step": {
            "id": "unique slug",
            "op": ["model", "passthrough", "read", "write", "transform"],
            "params": "op-specific ({system,prompt}|{path}|{})",
            "spec": ["min_chars", "max_chars", "valid_json", "json_keys",
                     "must_contain_all", "must_contain_any", "regex"],
            "source": ["${prev}", "${input:<key>}", "<literal>"],
            "soft": "bool -- if true, a verification failure is recorded but the run continues"
        },
        "loop": ["resolve", "dispatch", "verify", "refine"],
    }, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="med", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"med {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--model", help="Ollama model name")
        p.add_argument("--base-url", help="Ollama base URL")
        p.add_argument("--source", help="material to work from (file path or inline text)")
        p.add_argument("--count", type=int, help="number of items (flashcards)")
        p.add_argument("--retries", type=int, default=None,
                       help="bounded self-correction attempts for the model step (default 2)")
        p.add_argument("--out", help="output file path")

    pm = sub.add_parser("models", help="list Ollama models / check connectivity")
    pm.add_argument("--model"); pm.add_argument("--base-url")
    pm.set_defaults(func=cmd_models)

    pr = sub.add_parser("resolve", help="resolve intent -> JSON blueprint (no dispatch)")
    pr.add_argument("intent", help="natural-language intent")
    add_common(pr)
    pr.set_defaults(func=cmd_resolve)

    pu = sub.add_parser("run", help="resolve + dispatch + verify")
    pu.add_argument("intent", help="natural-language intent")
    add_common(pu)
    pu.add_argument("--json", action="store_true", help="emit full run JSON")
    pu.set_defaults(func=cmd_run)

    pb = sub.add_parser("blueprint", help="print the Blueprint contract")
    pb.set_defaults(func=cmd_blueprint)

    args = parser.parse_args(argv)

    # fix: ensure intent is a real required arg on resolve/run
    if args.cmd in ("resolve", "run") and not getattr(args, "intent", None):
        print(f"error: {args.cmd} requires an intent, e.g. "
              f"med {args.cmd} \"make 5 flashcards about quantum entanglement\" "
              "--source FILE --count 5", file=sys.stderr)
        # trigger arg error path
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
