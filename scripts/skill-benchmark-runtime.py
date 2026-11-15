# SPDX-License-Identifier: AGPL-3.0-only
"""Standalone code embedded in frozen skill benchmark runners/verifiers.

No provider SDK, shell invocation, inherited environment or credential parsing.
Linux bubblewrap and direct, pinned subscription CLI binaries are mandatory.
"""
import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
from pathlib import Path


class Refused(Exception):
    pass


class SubprocessFailure(Refused):
    def __init__(self, diagnostics):
        super().__init__("subprocess_failed")
        self.safe_diagnostics = diagnostics


SCHEMA = {"type": "object", "properties": {"source": {"type": "string"}},
          "required": ["source"], "additionalProperties": False}
CONFIG_KEYS = {"schema", "provider", "model", "runtime", "cli", "sandbox",
               "auth_file", "limits", "spend", "codex_profile"}
LIMIT_KEYS = {"model_seconds", "verify_seconds", "output_bytes", "source_bytes"}
CLAUDE_SETTINGS = {"disableAllHooks": True, "autoMemoryEnabled": False,
                   "forceLoginMethod": "claudeai", "enableAllProjectMcpServers": False}


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def read_regular(path, cap):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "not_regular")
        data = stream.read(cap + 1)
    require(len(data) <= cap, "input_limit")
    return data


def pin(path):
    path = Path(path)
    require(path.is_absolute() and not path.is_symlink(), "absolute_direct_executable_required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        require(stat.S_ISREG(meta.st_mode) and meta.st_uid in (0, os.geteuid())
                and not meta.st_mode & 0o022 and os.access(path, os.X_OK), "unsafe_executable")
        require(meta.st_size <= 512 << 20, "executable_limit")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        require(os.fstat(stream.fileno()).st_mtime_ns == meta.st_mtime_ns, "executable_changed")
    return {"path": str(path), "sha256": digest.hexdigest()}


def check_config(config):
    require(isinstance(config, dict) and set(config) == CONFIG_KEYS, "config_fields")
    require(config["schema"] == "lore.skill-benchmark-runtime.v1", "config_schema")
    require(config["provider"] in ("claude", "codex"), "subscription_provider_required")
    for name in ("model", "runtime"):
        value = config[name]
        require(isinstance(value, str) and 0 < len(value) <= 180
                and not value.startswith("-") and all(c.isalnum() or c in "._:/+@-" for c in value),
                "explicit_identity_required")
    for name in ("cli", "sandbox"):
        require(isinstance(config[name], dict) and set(config[name]) == {"path", "sha256"}, "pin_fields")
        require(config[name] == pin(config[name]["path"]), "executable_changed")
    limits = config["limits"]
    require(isinstance(limits, dict) and set(limits) == LIMIT_KEYS, "limit_fields")
    for key, low, high in (("model_seconds", 1, 90), ("verify_seconds", 1, 10),
                           ("output_bytes", 1024, 1024 * 1024), ("source_bytes", 1024, 128 * 1024)):
        require(type(limits[key]) in (int, float) and math.isfinite(limits[key])
                and low <= limits[key] <= high, "limit_range")
    require(type(limits["output_bytes"]) is int and type(limits["source_bytes"]) is int, "integer_byte_limit")
    spend = config["spend"]
    require(isinstance(spend, dict) and set(spend) == {"subscription_enabled", "max_calls", "ledger"}, "spend_fields")
    require(type(spend["subscription_enabled"]) is bool and type(spend["max_calls"]) is int
            and 0 <= spend["max_calls"] <= 128, "spend_policy")
    require(not spend["subscription_enabled"] or spend["max_calls"] > 0, "call_budget_required")
    require(Path(config["auth_file"]).is_absolute() and Path(spend["ledger"]).is_absolute(), "absolute_private_paths_required")
    if config["provider"] == "claude":
        require(config["codex_profile"] is None, "unexpected_codex_profile")
    elif spend["subscription_enabled"] or config["codex_profile"] is not None:
        check_codex_profile(config)


def harden_codex_model(model):
    model = dict(model)
    model.update(shell_type="disabled", apply_patch_tool_type=None, node_repl_disabled=True,
                 experimental_supported_tools=[], supports_search_tool=False, tool_mode="disabled",
                 multi_agent_version="disabled", use_responses_lite=False,
                 include_skills_usage_instructions=False, include_plugin_usage_instructions=False,
                 include_apps_usage_instructions=False)
    return model


def check_codex_profile(config):
    profile = config["codex_profile"]
    require(isinstance(profile, dict) and profile.get("schema") == "lore.codex-subscription-tools.v1"
            and profile.get("cli_sha256") == config["cli"]["sha256"]
            and profile.get("model") == config["model"], "codex_offline_tool_profile_required")
    require(set(profile.get("tool_names", [])) <= {"request_user_input"}
            and set(profile.get("rejected_calls", [])) == {"exec_command", "apply_patch", "request_user_input"},
            "codex_tool_audit_required")
    models = profile.get("catalog", {}).get("models", [])
    require(len(models) == 1 and models[0].get("slug") == config["model"]
            and harden_codex_model(models[0]) == models[0], "codex_hardened_catalog_required")
    require(profile.get("catalog_sha256") == hashlib.sha256(json.dumps(profile["catalog"], sort_keys=True).encode()).hexdigest(),
            "codex_catalog_changed")


def private_file(path):
    # Inspect metadata only. Never open or parse subscription credential bytes.
    path = Path(path)
    meta = path.lstat()
    require(stat.S_ISREG(meta.st_mode) and meta.st_uid == os.geteuid()
            and not meta.st_mode & 0o077, "private_auth_file_required")
    return str(path)


def reserve_call(config):
    spend = config["spend"]
    require(spend["subscription_enabled"], "subscription_spend_disabled")
    ledger = Path(spend["ledger"])
    parent = ledger.parent.lstat()
    require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.geteuid()
            and not parent.st_mode & 0o077, "private_ledger_directory_required")
    fd = os.open(ledger, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        meta = os.fstat(stream.fileno())
        require(stat.S_ISREG(meta.st_mode) and meta.st_uid == os.geteuid()
                and not meta.st_mode & 0o077, "unsafe_ledger")
        fcntl.flock(stream, fcntl.LOCK_EX)
        raw = stream.read(128 * 1024 + 1)
        require(len(raw) <= 128 * 1024, "ledger_limit")
        # Bind budget to exact prepared configuration, including the enabled gate.
        identity = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        rows = [json.loads(line) for line in raw.splitlines()]
        require(all(row.get("config_sha256") == identity for row in rows), "ledger_config_changed")
        require(len(rows) < spend["max_calls"], "call_budget_exhausted")
        stream.seek(0, 2)
        stream.write((json.dumps({"config_sha256": identity, "attempt": len(rows) + 1}) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())


def clean_env(home):
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(home),
            "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "LORE_SKIP": "1", "LORE_DISABLE_REVIEW": "1", "LORE_DISABLE_SYNC": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}


def failure_diagnostics(stdout, stderr, exit_code):
    """Return only fixed category/event names and numeric status codes.

    Raw messages are transient classification input, never excerpts or fields
    in the returned object. In particular, provider error text can contain
    accounts, auth material and source; no arbitrary string survives here.
    """
    allowed_events = {"thread.started", "turn.started", "turn.completed", "turn.failed",
                      "item.started", "item.updated", "item.completed", "error"}
    allowed_statuses = {400, 401, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504}
    events, statuses = set(), set()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("type"), str) and event["type"] in allowed_events:
            events.add(event["type"])
        for value in (event, event.get("error")):
            if isinstance(value, dict):
                for key in ("status", "status_code", "http_status"):
                    if type(value.get(key)) is int and value[key] in allowed_statuses:
                        statuses.add(value[key])
    text = (stdout + b"\n" + stderr).decode(errors="replace").lower()
    for match in re.finditer(r"(?:http(?:/\d\.\d)?(?: status)?|status(?: code)?)\s*[:=]?\s*(\d{3})\b", text):
        code = int(match.group(1))
        if code in allowed_statuses:
            statuses.add(code)
    auth = any(word in text for word in ("auth.json", "credentials", "oauth", "authentication", "refresh token"))
    if auth and "read-only file system" in text:
        category = "auth_refresh_read_only"
    elif auth and "permission denied" in text:
        category = "auth_file_access_denied"
    elif any(word in text for word in ("unknown argument", "unexpected argument", "error loading config.toml", "unknown variant")):
        category = "cli_contract_unsupported"
    elif "bwrap:" in text and any(word in text for word in ("failed", "operation not permitted", "permission denied")):
        category = "sandbox_setup_failed"
    elif 401 in statuses or any(word in text for word in ("not logged in", "log in again", "token has expired", "invalid refresh token")):
        category = "subscription_auth_required"
    elif auth and "refresh" in text and any(word in text for word in ("failed", "error", "expired")):
        category = "subscription_auth_refresh_failed"
    elif 429 in statuses or any(word in text for word in ("rate limit", "usage limit", "quota exceeded")):
        category = "subscription_rate_limited"
    elif "model" in text and any(word in text for word in ("not supported", "not found", "not available", "does not exist")):
        category = "provider_model_unsupported"
    elif "schema" in text and (400 in statuses or "invalid" in text or "unsupported" in text):
        category = "provider_schema_rejected"
    elif 403 in statuses:
        category = "subscription_access_denied"
    elif 400 in statuses or 422 in statuses:
        category = "provider_request_rejected"
    elif any(word in text for word in ("certificate", "tls", "dns", "connection refused", "failed to connect", "failed to lookup")):
        category = "provider_transport_failed"
    else:
        category = "unclassified_process_failure"
    return {"category": category, "exit_code": exit_code,
            "event_types": sorted(events), "http_status_codes": sorted(statuses)}


def bounded(argv, payload, home, seconds, cap, *, status_stderr=False):
    """Bound pipe reads in memory, including a child that exits after a flood.

    Remain in the evaluator's process group. bwrap's private PID namespace and
    --die-with-parent kill its descendants on timeout or outer evaluator death.
    Never forward CLI/auth logs into the evaluator's retained excerpts.
    """
    deadline = time.monotonic() + seconds
    with tempfile.TemporaryFile(dir=home) as request:
        request.write(payload)
        request.seek(0)
        child = subprocess.Popen(argv, stdin=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=clean_env(home), close_fds=True)
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            with selectors.DefaultSelector() as selector:
                for name in buffers:
                    pipe = getattr(child, name)
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                while selector.get_map():
                    require(time.monotonic() < deadline, "subprocess_timeout")
                    for key, _ in selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                        data = os.read(key.fd, 16384)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        target = buffers[key.data]
                        require(len(target) + len(data) <= cap, "subprocess_output_limit")
                        target.extend(data)
                exit_code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
                if exit_code != 0:
                    raise SubprocessFailure(failure_diagnostics(bytes(buffers["stdout"]), bytes(buffers["stderr"]), exit_code))
            return bytes(buffers["stderr"] if status_stderr else buffers["stdout"])
        except subprocess.TimeoutExpired:
            raise Refused("subprocess_timeout") from None
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()
            child.stderr.close()


def sandbox_argv(config, home, io_dir, *, online, workspace=None, check=None):
    """Empty root, private PID/IPC/user namespaces; no host HOME or repo mount.

    Only the subscription CLI has network. The independent Python verifier
    gets neither network nor any auth mount. OS libraries are host dependencies;
    the recorded CLI/bwrap hashes do not attest the entire operating system.
    """
    argv = [config["sandbox"]["path"], "--unshare-all", "--die-with-parent", "--cap-drop", "ALL"]
    if online:
        argv += ["--share-net"]
    for name in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(name).exists():
            argv += ["--ro-bind", name, name]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
             "--dir", "/home", "--bind", str(home), "/home/benchmark",
             "--ro-bind", str(io_dir), "/io", "--tmpfs", "/workspace"]
    if workspace is not None:
        argv += ["--ro-bind", str(workspace), "/workspace"]
    if online:
        for name in ("/etc/ssl", "/etc/resolv.conf", "/etc/hosts"):
            if Path(name).exists():
                argv += ["--ro-bind", name, name]
        auth_dest = (".codex/auth.json" if config["provider"] == "codex" else ".claude/.credentials.json")
        (home / Path(auth_dest).parent).mkdir(exist_ok=True)
        argv += ["--ro-bind", config["cli"]["path"], "/opt/model-cli",
                 "--ro-bind", private_file(config["auth_file"]), "/home/benchmark/" + auth_dest]
    if check is not None:
        argv += ["--ro-bind", str(check), "/checks.py"]
    argv += ["--clearenv"]
    for key, value in clean_env("/home/benchmark").items():
        argv += ["--setenv", key, value]
    argv += ["--setenv", "CODEX_HOME", "/home/benchmark/.codex",
             "--chdir", "/workspace", "--"]
    return argv


def provider_argv(config, *, status=False):
    if config["provider"] == "claude":
        base = ["/opt/model-cli", "--safe-mode", "--setting-sources", "", "--settings",
                json.dumps(CLAUDE_SETTINGS, separators=(",", ":"))]
        if status:
            return base + ["auth", "status", "--json"]
        return base + ["--print", "--no-session-persistence", "--model", config["model"],
                       "--max-turns", "2",
                       "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                       "--disable-slash-commands", "--permission-mode", "dontAsk", "--no-chrome",
                       "--output-format", "json", "--json-schema", json.dumps(SCHEMA)]
    base = ["/opt/model-cli", "--no-daemon", "-c", 'forced_login_method="chatgpt"']
    if status:
        return base + ["login", "status"]
    argv = base + ["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                   "--skip-git-repo-check", "--sandbox", "read-only", "--model", config["model"],
                   "--json", "--output-schema", "/io/schema.json"]
    for setting in ('model_catalog_json="/io/catalog.json"', 'approval_policy="never"',
                    'web_search="disabled"', 'history.persistence="none"',
                    'shell_environment_policy.inherit="none"', 'skills.bundled.enabled=false',
                    'skills.include_instructions=false', 'feedback.enabled=false',
                    'features.hooks=false', 'features.shell_tool=false', 'features.memories=false',
                    'features.apps=false', 'features.multi_agent=false', 'features.multi_agent_v2=false',
                    'features.code_mode=false', 'features.view_image=false', 'features.image_generation=false',
                    'features.computer_use=false', 'features.browser_use=false', 'features.plugins=false',
                    'features.remote_plugin=false', 'features.goals=false', 'features.sleep_tool=false',
                    'features.skill_search=false', 'features.unbounded_connection_retries=false'):
        argv += ["-c", setting]
    return argv + ["-"]


def subscription_status(config, raw):
    if config["provider"] == "claude":
        value = json.loads(raw)
        require(isinstance(value, dict) and value.get("loggedIn") is True and value.get("authMethod") == "claude.ai"
                and value.get("subscriptionType") in ("pro", "max", "team", "enterprise"),
                "subscription_auth_required")
    else:
        lines = raw.decode().splitlines()
        # Some pinned builds warn about helper aliases before the status line.
        # Accept that one known warning class, never arbitrary extra auth text.
        lines = [line for line in lines if not line.startswith(
            "WARNING: proceeding, even though we could not create PATH aliases:")]
        require(lines == ["Logged in using ChatGPT"], "chatgpt_auth_required")


def response_source(config, raw):
    if config["provider"] == "claude":
        value = json.loads(raw)
        require(value.get("type") == "result" and value.get("subtype") == "success"
                and value.get("is_error") is False, "model_result_failed")
        result = value.get("structured_output")
    else:
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        require(not any(e.get("type") in ("error", "turn.failed") for e in events), "model_result_failed")
        require(sum(e.get("type") == "turn.completed" for e in events) == 1, "model_turn_incomplete")
        messages = [e["item"].get("text", "") for e in events
                    if e.get("type") == "item.completed" and e.get("item", {}).get("type") == "agent_message"]
        require(len(messages) == 1, "single_source_response_required")
        result = json.loads(messages[0])
    require(isinstance(result, dict) and set(result) == {"source"}
            and isinstance(result["source"], str), "source_response_required")
    source = result["source"].encode()
    require(0 < len(source) <= config["limits"]["source_bytes"], "source_output_limit")
    return source


def response_metadata(config, raw):
    if config["provider"] == "claude":
        value = json.loads(raw)
        models = list(value.get("modelUsage", {}))
        models = [model for model in models if isinstance(model, str) and model.startswith("claude-")
                  and len(model) <= 180 and all(c.isalnum() or c in "._-" for c in model)]
        turns = value.get("num_turns")
        return {"provider_reported_models": models,
                "provider_reported_turns": turns if type(turns) is int and 0 <= turns <= 256 else None}
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return {"completed_turns": sum(e.get("type") == "turn.completed" for e in events),
            "completed_items": sum(e.get("type") == "item.completed" for e in events),
            "provider_reported_models": []}


def run_model(config, observer=None):
    check_config(config)
    require(os.environ.get("LORE_EVAL_MODEL") == config["model"]
            and os.environ.get("LORE_EVAL_RUNTIME") == config["runtime"], "evaluation_identity_mismatch")
    task_id = os.environ.get("LORE_EVAL_TASK_ID")
    case = json.loads(read_regular("case.json", 8192))
    require(case["id"] == task_id, "evaluation_task_mismatch")
    skill = read_regular(os.environ["LORE_EVAL_SKILL"], 16 * 1024).decode()
    files = {name: read_regular(name, 128 * 1024).decode()
             for name in ("TASK.md", "benchmark.py", "test_public.py")}
    prompt = ("Repair benchmark.py according to TASK.md. Apply the supplied reusable coding skill when useful. "
              "Return only a JSON object with key source containing the entire repaired benchmark.py. "
              "No tools, external services, alternate files, or prose. Public tests are illustrative; "
              "independent regression tests determine correctness.\n" +
              json.dumps({"skill": skill, "files": files}, ensure_ascii=False)).encode()
    reserve_call(config)  # Default disabled; never launch even auth status without this gate.
    def observed(event):
        if observer is not None:
            observer(event)
    observed("attempt_reserved")
    with tempfile.TemporaryDirectory(prefix="benchmark-model-") as temporary:
        root = Path(temporary)
        home, io_dir = root / "home", root / "io"
        home.mkdir(mode=0o700)
        io_dir.mkdir()
        (io_dir / "schema.json").write_text(json.dumps(SCHEMA))
        if config["provider"] == "codex":
            (io_dir / "catalog.json").write_text(json.dumps(config["codex_profile"]["catalog"]))
        (home / ".claude.json").write_text('{"hasCompletedOnboarding":true}')
        argv = sandbox_argv(config, home, io_dir, online=True)
        observed("auth_preflight_started")
        status = bounded(argv + provider_argv(config, status=True), b"", home, 8,
                         config["limits"]["output_bytes"], status_stderr=config["provider"] == "codex")
        subscription_status(config, status)
        observed("auth_preflight_passed")
        observed("model_invocation_started")
        raw = bounded(argv + provider_argv(config), prompt, home, config["limits"]["model_seconds"],
                      config["limits"]["output_bytes"])
        observed("model_invocation_completed")
        source = response_source(config, raw)
        metadata = response_metadata(config, raw)
    # Exactly one predeclared regular file; no patch paths, model commands or logs.
    require(not Path("benchmark.py").is_symlink(), "source_symlink")
    Path("benchmark.py").write_bytes(source)
    print(json.dumps({"adapter": "subscription-cli-v1", "provider": config["provider"],
                      "model": config["model"], "runtime": config["runtime"],
                      "cli_model_invocations": 1, "auth_preflights": 1,
                      "metadata": metadata, "source_sha256": hashlib.sha256(source).hexdigest()}))


def run_verifier(config, checks):
    check_config(config)
    task_id = os.environ.get("LORE_EVAL_TASK_ID")
    require(task_id in checks, "unknown_verifier_task")
    # A fresh copy excludes model output logs, mutable public tests and home.
    source = read_regular("benchmark.py", config["limits"]["source_bytes"])
    with tempfile.TemporaryDirectory(prefix="benchmark-verify-") as temporary:
        root = Path(temporary)
        home, io_dir, workspace = root / "home", root / "io", root / "workspace"
        for path in (home, io_dir, workspace):
            path.mkdir(mode=0o700)
        (workspace / "benchmark.py").write_bytes(source)
        check = root / "checks.py"
        receipt = "verified:" + os.urandom(24).hex()
        check.write_text(checks[task_id] + "\nresult = unittest.main(exit=False).result\n"
                         "if not result.wasSuccessful():\n    raise SystemExit(1)\n"
                         + "print(" + repr(receipt) + ")\n")
        argv = sandbox_argv(config, home, io_dir, online=False, workspace=workspace, check=check)
        raw = bounded(argv + ["/usr/bin/python3", "-I", "-B", "/checks.py"], b"", home,
                      config["limits"]["verify_seconds"], config["limits"]["output_bytes"])
        require(raw.decode().strip() == receipt, "independent_checks_incomplete")
    print(json.dumps({"independent_verifier": "passed", "task": task_id}))


def guarded_main(function, *args):
    try:
        function(*args)
    except (Refused, OSError, ValueError, KeyError, TypeError) as error:
        # Never emit provider payloads, credential bytes, identities or tracebacks.
        reason = str(error) if isinstance(error, Refused) else type(error).__name__
        status = {"benchmark": "refused", "reason": reason}
        if isinstance(error, SubprocessFailure):
            status["failure"] = error.safe_diagnostics
        print(json.dumps(status))
        raise SystemExit(1) from None
