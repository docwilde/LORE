#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Explicitly spend at most one subscription CLI coding-task invocation.

Tests adapter plumbing on the training task only. No native evaluation report,
pending mutation, candidate ranking, installation or final-test consumption.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("benchmark_runtime", ROOT / "scripts/skill-benchmark-runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="disabled operator config")
    parser.add_argument("--output", type=Path, required=True, help="new private artifact directory")
    parser.add_argument("--codex-profile", type=Path)
    parser.add_argument("--enable-subscription", action="store_true", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=120, help="overall deadline, 1-120 seconds")
    args = parser.parse_args()
    report = {"schema": "lore.skill-adapter-smoke.v1", "task": "train-empty-replace",
              "kind": "adapter_smoke_only", "maximum_cli_model_invocations": 1, "passed": False}
    created = False
    events = []
    try:
        runtime.require(1 <= args.timeout_seconds <= 120, "smoke_deadline_range")
        report["timeout_seconds"] = args.timeout_seconds
        config = json.loads(runtime.read_regular(args.config, 1024 * 1024))
        runtime.require(config["spend"]["subscription_enabled"] is False, "input_config_must_be_disabled")
        config["spend"].update(subscription_enabled=True, max_calls=1)
        if args.codex_profile:
            config["codex_profile"] = json.loads(runtime.read_regular(args.codex_profile, 1024 * 1024))
        runtime.check_config(config)
        runtime.require(args.output.is_absolute() and not args.output.exists(), "new_absolute_output_required")
        args.output.mkdir(mode=0o700, parents=True)
        created = True
        corpus = ROOT / "examples/skill-evaluation/model-coding"
        workspace = args.output / "workspace"
        shutil.copytree(corpus / "tasks/train-empty-replace", workspace)
        skill = args.output / "skill.md"
        skill.write_text("Read the contract carefully, fix the underlying bug, and preserve existing behavior.\n")
        report.update(provider=config["provider"], model=config["model"], runtime=config["runtime"],
                      cli_sha256=config["cli"]["sha256"], sandbox_sha256=config["sandbox"]["sha256"])
        # The script's sole live path has a wall-clock bound around both phases.
        def expired(*_):
            raise runtime.Refused("smoke_timeout")
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(args.timeout_seconds)
        os.environ.update(LORE_EVAL_MODEL=config["model"], LORE_EVAL_RUNTIME=config["runtime"],
                          LORE_EVAL_TASK_ID="train-empty-replace", LORE_EVAL_SKILL=str(skill))
        os.chdir(workspace)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            runtime.run_model(config, events.append)
        report["runner"] = json.loads(output.getvalue())
        checks = {"train-empty-replace": (corpus / "checks/train-empty-replace.py").read_text()}
        with contextlib.redirect_stdout(io.StringIO()):
            runtime.run_verifier(config, checks)
        report["passed"] = True
    except (runtime.Refused, OSError, ValueError, KeyError, TypeError) as error:
        report["reason"] = str(error) if isinstance(error, runtime.Refused) else type(error).__name__
        if isinstance(error, runtime.SubprocessFailure):
            report["failure"] = error.safe_diagnostics
    finally:
        signal.alarm(0)
    report["events"] = events
    report["cli_model_invocations"] = events.count("model_invocation_started")
    report["auth_preflights"] = events.count("auth_preflight_started")
    if created:
        (args.output / "smoke.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
