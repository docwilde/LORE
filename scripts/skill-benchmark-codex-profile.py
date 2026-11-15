#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Credential-free, localhost-only probe of a pinned Codex CLI's tool registry.

Never contacts a provider. Deliberately forges shell, patch and input calls
against the actual CLI to verify rejection, then writes a frozen tool profile.
"""
import argparse
import hashlib
import http.server
import importlib.util
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.dont_write_bytecode = True

spec = importlib.util.spec_from_file_location("benchmark_runtime", Path(__file__).with_name("skill-benchmark-runtime.py"))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def tool_names(tools):
    names = []
    for tool in tools:
        if tool.get("type") == "namespace":
            names.extend(tool_names(tool["tools"]))
        else:
            names.append(tool.get("name", tool.get("type")))
    return sorted(names)


def probe(cli, model):
    executable = runtime.pin(cli)
    with tempfile.TemporaryDirectory(prefix="lore-codex-offline-audit-") as directory:
        root = Path(directory)
        bundled = runtime.bounded([str(cli), "--no-daemon", "debug", "models", "--bundled"],
                                  b"", root, 8, 1024 * 1024)
        models = json.loads(bundled)["models"]
        selected = [item for item in models if item.get("slug") == model]
        runtime.require(len(selected) == 1, "model_missing_from_bundled_catalog")
        catalog = {"models": [runtime.harden_codex_model(selected[0])]}
        catalog_path = root / "catalog.json"
        catalog_path.write_text(json.dumps(catalog))
        schema_path = root / "schema.json"
        schema_path.write_text(json.dumps(runtime.SCHEMA))
        credential = root / "fake-credential"
        credential.write_text("NOT_A_REAL_CREDENTIAL")
        marker = root / "forged-tool-executed"
        captured = []
        errors = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    runtime.require(self.path == "/v1/responses", "unexpected_offline_endpoint")
                    length = int(self.headers.get("Content-Length", "0"))
                    runtime.require(0 < length <= 2 * 1024 * 1024, "offline_request_limit")
                    value = json.loads(self.rfile.read(length))
                    runtime.require(len(captured) < 2, "offline_request_count")
                    names = tool_names(value.get("tools", []))
                    runtime.require(set(names) <= {"request_user_input"}, "unsafe_model_tools")
                    captured.append(value)
                    if len(captured) == 1:
                        items = [
                            {"type": "function_call", "id": "exec_item", "call_id": "probe_exec", "name": "exec_command",
                             "arguments": json.dumps({"cmd": "cat " + str(credential) + " > " + str(marker)})},
                            {"type": "custom_tool_call", "id": "patch_item", "call_id": "probe_patch", "name": "apply_patch",
                             "input": "*** Begin Patch\n*** Add File: " + str(marker) + "\n+forged\n*** End Patch"},
                            {"type": "function_call", "id": "input_item", "call_id": "probe_input", "name": "request_user_input",
                             "arguments": json.dumps({"questions": [{"header": "Offline", "id": "offline",
                                            "question": "Offline fixture?", "options": [
                                                {"label": "One", "description": "Offline one."},
                                                {"label": "Two", "description": "Offline two."}]}]})}]
                    else:
                        items = [{"type": "message", "id": "message_item", "role": "assistant", "status": "completed",
                                  "content": [{"type": "output_text", "text": "offline audit complete"}]}]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    events = [{"type": "response.created", "response": {"id": "response_fixture"}}]
                    events += [{"type": "response.output_item.done", "item": item} for item in items]
                    events += [{"type": "response.completed", "response": {"id": "response_fixture",
                                "status": "completed", "output": items,
                                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}]
                    for event in events:
                        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.flush()
                except (runtime.Refused, ValueError, KeyError, OSError) as error:
                    errors.append(type(error).__name__)
                    self.send_error(400, "offline tool audit refused")

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.timeout = 1
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = {"provider": "codex", "model": model}
        argv = runtime.provider_argv(config)
        argv[0] = str(cli)
        # Replace only the model endpoint and catalog in this auth-free fixture.
        # Production runner never accepts a configurable provider/base URL.
        argv = [item if item != 'model_catalog_json="/io/catalog.json"'
                else "model_catalog_json=" + json.dumps(str(catalog_path)) for item in argv]
        argv = [str(schema_path) if item == "/io/schema.json" else item for item in argv]
        argv += ["-c", 'model_provider="offline_fixture"',
                 "-c", 'model_providers.offline_fixture.name="offline fixture"',
                 "-c", "model_providers.offline_fixture.base_url=" + json.dumps("http://127.0.0.1:" + str(server.server_port) + "/v1"),
                 "-c", 'model_providers.offline_fixture.wire_api="responses"',
                 "-c", 'model_providers.offline_fixture.requires_openai_auth=false']
        # No real auth file, user configuration, provider key or task source.
        try:
            runtime.bounded(argv, b"Offline registry probe. Reply with offline audit complete.", root, 15, 1024 * 1024)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
        runtime.require(not errors and len(captured) == 2 and not marker.exists(), "offline_tool_probe_failed")
        outputs = {item.get("call_id"): str(item.get("output", "")) for item in captured[1].get("input", [])
                   if item.get("type") in ("function_call_output", "custom_tool_call_output")}
        for call in ("probe_exec", "probe_patch"):
            output = outputs.get(call, "").lower()
            runtime.require("unsupported" in output or "not found" in output or "unrecognized" in output,
                            "forged_execution_not_refused")
            runtime.require("NOT_A_REAL_CREDENTIAL" not in outputs.get(call, ""), "forged_read_executed")
        input_output = outputs.get("probe_input", "").lower()
        runtime.require("default mode" in input_output
                        and ("not available" in input_output or "unavailable" in input_output), "input_tool_not_inert")
        return {"schema": "lore.codex-subscription-tools.v1", "cli_sha256": executable["sha256"],
                "model": model, "catalog": catalog, "tool_names": tool_names(captured[0].get("tools", [])),
                "catalog_sha256": hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest(),
                "rejected_calls": ["exec_command", "apply_patch", "request_user_input"],
                "offline_requests": len(captured)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        runtime.require(not args.output.exists(), "profile_output_must_not_exist")
        profile = probe(args.cli, args.model)
        with args.output.open("x") as stream:
            json.dump(profile, stream, indent=2)
            stream.write("\n")
        print(json.dumps({"profile": str(args.output), "model": args.model,
                          "cli_sha256": profile["cli_sha256"], "tool_names": profile["tool_names"],
                          "forged_calls_refused": profile["rejected_calls"], "provider_calls": 0}))
    except (runtime.Refused, OSError, ValueError, KeyError, TypeError) as error:
        reason = str(error) if isinstance(error, runtime.Refused) else type(error).__name__
        parser.exit(1, "offline Codex probe refused: " + reason + "\n")


if __name__ == "__main__":
    main()
