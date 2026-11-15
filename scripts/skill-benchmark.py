#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare a native guarded suite. This command never invokes a model."""
import argparse
import functools
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "scripts/skill-benchmark-runtime.py"
CORPUS = ROOT / "examples/skill-evaluation/model-coding"


@functools.lru_cache(maxsize=1)
def runtime_module():
    spec = importlib.util.spec_from_file_location("skill_benchmark_runtime", RUNTIME)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(args):
    runtime = runtime_module()
    config = json.loads(Path(args.config).read_text())
    if args.codex_profile:
        config["codex_profile"] = json.loads(runtime.read_regular(args.codex_profile, 1024 * 1024))
    # Enabling is a separate, explicit preparation action; config files ship off.
    runtime.require(not config["spend"]["subscription_enabled"], "input_config_must_be_disabled")
    if args.enable_subscription:
        runtime.require(args.max_calls is not None, "explicit_max_calls_required")
        config["spend"].update(subscription_enabled=True, max_calls=args.max_calls)
    else:
        runtime.require(args.max_calls is None, "enable_subscription_required")
    runtime.check_config(config)
    corpus = CORPUS
    manifest = json.loads((corpus / "corpus.json").read_text())
    runtime.require(manifest["schema"] == "lore.skill-coding-corpus.v1"
                    and manifest["version"] == "lore-regressions-v1", "corpus_version")
    content_files = {str(path.relative_to(corpus)) for folder in ("tasks", "checks")
                     for path in (corpus / folder).rglob("*") if path.is_file()}
    runtime.require(content_files == set(manifest["files"]), "corpus_file_set_changed")
    for name, expected in manifest["files"].items():
        path = Path(name)
        runtime.require(not path.is_absolute() and all(part not in ("..", ".") for part in path.parts), "corpus_path")
        runtime.require(hashlib.sha256(runtime.read_regular(corpus / path, 1024 * 1024)).hexdigest() == expected,
                        "corpus_changed_bump_version")
    output = Path(args.output).absolute()
    runtime.require(not output.exists(), "output_must_not_exist")
    # Corpus contains no links, generated caches, gold fixes or external datasets.
    for path in corpus.rglob("*"):
        runtime.require(not path.is_symlink(), "corpus_symlink")
    output.mkdir(mode=0o700, parents=True)
    try:
        cases = manifest["cases"]
        checks = {}
        for case in cases:
            source = corpus / case["fixture"]
            target = output / case["fixture"]
            shutil.copytree(source, target)
            checks[case["id"]] = (corpus / "checks" / (case["id"] + ".py")).read_text()
        prefix = RUNTIME.read_text() + "\nCONFIG = " + repr(config) + "\n"
        (output / "runner.py").write_text(prefix + "guarded_main(run_model, CONFIG)\n")
        (output / "verifier.py").write_text(prefix + "CHECKS = " + repr(checks) +
                                            "\nguarded_main(run_verifier, CONFIG, CHECKS)\n")
        suite = {"schema": "lore.skill-suite.v1", "family": "coding", "model": config["model"],
                 "runtime": config["runtime"], "interpreter": str(Path(sys.executable).resolve()),
                 "runner": "runner.py", "verifier": "verifier.py", "timeout_seconds": 120,
                 "answer_markers": manifest["answer_markers"], "cases": cases}
        (output / "suite.json").write_text(json.dumps(suite, indent=2) + "\n")
        for name in ("corpus.json", "PROVENANCE.md", "README.md"):
            shutil.copyfile(corpus / name, output / name)
    except BaseException:
        shutil.rmtree(output)
        raise
    # Two arms per case. Final-test gate may reduce the actual count.
    print(json.dumps({"suite": str(output / "suite.json"), "provider": config["provider"],
                      "model": config["model"], "subscription_enabled": args.enable_subscription,
                      "maximum_model_invocations": len(cases) * 2,
                      "attempt_budget": config["spend"]["max_calls"],
                      "next": "human terminal: lore skills evaluate PROPOSAL_ID --suite " + str(output / "suite.json")}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare", help="freeze corpus/runtime into a suite; no model calls")
    command.add_argument("--config", required=True)
    command.add_argument("--output", required=True, help="new, dedicated directory")
    command.add_argument("--enable-subscription", action="store_true", help="allow quota consumption in the prepared runner")
    command.add_argument("--max-calls", type=int, help="total attempted CLI generations across both arms/all splits")
    command.add_argument("--codex-profile", help="offline tool-audit profile for this exact Codex binary/model")
    args = parser.parse_args()
    try:
        prepare(args)
    except (runtime_module().Refused, OSError, ValueError, KeyError, TypeError) as error:
        reason = str(error) if isinstance(error, runtime_module().Refused) else type(error).__name__
        parser.exit(1, "benchmark preparation refused: " + reason + "\n")


if __name__ == "__main__":
    main()
