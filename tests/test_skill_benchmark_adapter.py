# SPDX-License-Identifier: AGPL-3.0-only
"""Offline subprocess contract and independent coding-verifier controls.

No installed config, credentials, provider or production LORE store is used.
Fake bwrap checks the requested isolation contract but cannot prove kernel
isolation. Run with: python3 -m unittest discover -s tests -p test_skill_benchmark_adapter.py
"""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "examples/skill-evaluation/model-coding"
SPEC = importlib.util.spec_from_file_location("benchmark_runtime", REPO / "scripts/skill-benchmark-runtime.py")
RUNTIME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME)
MANIFEST = json.loads((CORPUS / "corpus.json").read_text())

# This executable enforces and records argv/env rather than just returning 0.
# It emulates mount-path translation ONLY for offline contract tests.
FAKE_BWRAP = r'''#!/usr/bin/python3
import json, os, pathlib, sys, tempfile
args = sys.argv[1:]
assert '--unshare-all' in args and '--die-with-parent' in args
assert '--clearenv' in args and '--cap-drop' in args
assert '--new-session' not in args and '--ro-bind-try' not in args
assert not any(key in os.environ for key in ('OPENAI_API_KEY','ANTHROPIC_API_KEY','CODEX_API_KEY','LORE_ROOT','PYTHONPATH'))
assert os.environ['LORE_SKIP'] == '1' and os.environ['LORE_DISABLE_REVIEW'] == '1'
assert os.environ['HOME'] != '/operator/real-home'
mounts = {}
i = 0
while args[i] != '--':
    key = args[i]
    if key in ('--ro-bind', '--bind'):
        mounts[args[i+2]] = args[i+1]
        i += 3
    elif key == '--setenv':
        assert args[i+1] not in ('OPENAI_API_KEY','ANTHROPIC_API_KEY','CODEX_API_KEY')
        i += 3
    elif key in ('--cap-drop','--proc','--dev','--tmpfs','--dir','--chdir'):
        i += 2
    else:
        assert key in ('--unshare-all','--die-with-parent','--share-net','--clearenv'), key
        i += 1
cmd = args[i+1:]
payload = sys.stdin.buffer.read(600000)
audit = pathlib.Path(__file__).with_suffix('.audit')
with audit.open('a') as stream:
    stream.write(json.dumps({'argv':args, 'env':dict(os.environ), 'prompt':payload.decode()})+'\n')
if cmd[0] == '/opt/model-cli':
    assert '--share-net' in args
    assert '/workspace' not in mounts and '/checks.py' not in mounts
    assert len([p for p in mounts if p.endswith(('auth.json','.credentials.json'))]) == 1
    if 'exec' in cmd:
        catalog = json.loads((pathlib.Path(mounts['/io'])/'catalog.json').read_text())
        model = catalog['models'][0]
        assert model['shell_type'] == 'disabled' and model['apply_patch_tool_type'] is None
        assert model['node_repl_disabled'] and model['multi_agent_version'] == 'disabled'
    request = tempfile.TemporaryFile()
    request.write(payload); request.seek(0)
    os.dup2(request.fileno(), 0)
    os.execve(mounts['/opt/model-cli'], [mounts['/opt/model-cli'], *cmd[1:]], dict(os.environ))
else:
    assert '--share-net' not in args and '/opt/model-cli' not in mounts
    assert not any(p.endswith(('auth.json','.credentials.json')) for p in mounts)
    assert cmd == ['/usr/bin/python3','-I','-B','/checks.py']
    source = pathlib.Path(mounts['/checks.py']).read_text()
    source = source.replace('"/workspace/benchmark.py"', repr(mounts['/workspace']+'/benchmark.py'))
    script = pathlib.Path(mounts['/home/benchmark'])/'translated_checks.py'
    script.write_text(source)
    os.execve('/usr/bin/python3', cmd[:3]+[str(script)], dict(os.environ))
'''

FAKE_CLI = r'''#!/usr/bin/python3
import json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
mode = (root/'mode').read_text()
args = sys.argv[1:]
is_codex = 'exec' in args or 'login' in args
if 'status' in args:
    if is_codex:
        assert 'forced_login_method="chatgpt"' in args
        if mode == 'warning':
            print('WARNING: proceeding, even though we could not create PATH aliases: private sandbox',file=sys.stderr)
        print('Logged in using an API key' if mode == 'api' else 'Logged in using ChatGPT', file=sys.stderr)
    else:
        assert '--safe-mode' in args and '--bare' not in args
        settings = json.loads(args[args.index('--settings')+1])
        assert settings['disableAllHooks'] and settings['forceLoginMethod'] == 'claudeai'
        print(json.dumps({'loggedIn':True,'authMethod':'api_key' if mode == 'api' else 'claude.ai',
                          'subscriptionType':'max','email':'do-not-retain@invalid'}))
    sys.exit(0)
assert args[args.index('--model')+1] == 'operator-exact-model'
if is_codex:
    assert '--ignore-user-config' in args and '--ephemeral' in args and '--ignore-rules' in args
    assert '--no-daemon' in args and args[args.index('--sandbox')+1] == 'read-only'
    for flag in ('features.hooks=false','features.shell_tool=false','features.memories=false',
                 'features.multi_agent=false','features.apps=false','web_search="disabled"'):
        assert flag in args
else:
    assert '--safe-mode' in args and '--no-session-persistence' in args
    assert args[args.index('--tools')+1] == '' and '--strict-mcp-config' in args
    assert args[args.index('--setting-sources')+1] == '' and '--disable-slash-commands' in args
prompt = json.loads(sys.stdin.read().split('\n',1)[1])
assert set(prompt) == {'skill','files'}
assert set(prompt['files']) == {'TASK.md','benchmark.py','test_public.py'}
assert prompt['skill'] in ('General reusable coding guidance.',
                          'Read the contract carefully, fix the underlying bug, and preserve existing behavior.\n')
assert 'StartupChecks' not in str(prompt) and 'OPERATOR_SECRET' not in str(prompt)
if mode == 'timeout': time.sleep(5)
if mode in ('flood','stderr_flood'):
    os.write(2 if mode == 'stderr_flood' else 1, b'OPERATOR_SECRET'+b'x'*1000000)
    sys.exit(0)
if mode == 'fail':
    print('OPERATOR_SECRET',file=sys.stderr); sys.exit(7)
if mode == 'auth_readonly':
    print('Error refreshing authentication auth.json: Read-only file system OPERATOR_SECRET do-not-retain@invalid',file=sys.stderr)
    sys.exit(1)
if mode == 'unsupported_model':
    print(json.dumps({'type':'error','message':'Requested model is not supported. HTTP status 400 OPERATOR_SECRET'}))
    print(json.dumps({'type':'turn.failed','error':{'status_code':400,'message':'do-not-retain@invalid'}}))
    sys.exit(1)
if mode == 'malformed':
    print('Tests passed! OPERATOR_SECRET'); sys.exit(0)
source = (root/'response.py').read_text()
if is_codex:
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'source':source})}}))
    print(json.dumps({'type':'turn.completed','usage':{'output_tokens':20}}))
else:
    print(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':{'source':source}}))
'''


def minimal_repair(task_id, source):
    # Test-only repairs never enter the corpus or model prompt.
    if task_id == "train-empty-replace":
        return source.replace('    new = one_line(text)\n', '    new = one_line(text)\n    if not new:\n        return "empty text"\n')
    if task_id == "replay-worker-window":
        return source[:source.index('def is_worker_transcript')] + '''import json
def is_worker_transcript(path):
    try:
        with path.open('rb') as stream:
            used = 0
            for _ in range(MAX_RECORDS):
                line = stream.readline(MAX_BYTES - used + 1)
                used += len(line)
                if not line or used > MAX_BYTES:
                    break
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(value, dict) or value.get('type') not in ('user','assistant'):
                    continue
                message = value.get('message')
                if not isinstance(message, dict):
                    continue
                content = message.get('content')
                text = content if isinstance(content, str) else ''
                if isinstance(content, list):
                    text = '\\n'.join(block['text'] for block in content if isinstance(block,dict)
                                     and block.get('type') == 'text' and isinstance(block.get('text'),str))
                if any(marker in text for marker in MARKERS):
                    return True
    except OSError:
        return False
    return False
'''
    if task_id == "validation-worktree-identity":
        return source[:source.index('def project_identity_root')] + '''def project_identity_root(cwd):
    try:
        result = subprocess.run(['git','rev-parse','--show-toplevel','--git-common-dir'],
                                cwd=cwd,capture_output=True,text=True,timeout=5)
        lines = result.stdout.splitlines()
        if result.returncode or len(lines) != 2:
            return project_root(cwd)
        top, common = lines
        path = Path(common) if Path(common).is_absolute() else Path(cwd)/common
        if path.name == '.git' and path.parent.is_dir():
            return str(path.parent.resolve())
        return top or project_root(cwd)
    except (OSError,subprocess.TimeoutExpired):
        return project_root(cwd)
'''
    if task_id == "test-startup-pending":
        return source.replace('    ids = pending_ids()\n', '''    count, error = None, None
    try:
        count = len(pending_ids())
    except PendingUnavailable as failure:
        error = failure.code
''').replace('"pending_count": len(ids), "pending_error": None', '"pending_count": count, "pending_error": error')
    raise AssertionError(task_id)


class BenchmarkAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lore-benchmark-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.sandbox = self.base / "fake-bwrap"
        self.cli = self.base / "fake-cli"
        for path, source in ((self.sandbox, FAKE_BWRAP), (self.cli, FAKE_CLI)):
            path.write_text(source)
            path.chmod(0o700)
        (self.base / "mode").write_text("normal")
        (self.base / "oauth-only").write_text("fake-auth-never-read-by-adapter")
        (self.base / "oauth-only").chmod(0o600)
        self.private = self.base / "budget"
        self.private.mkdir(mode=0o700)
        self.workspace = self.base / "workspace"
        self.skill = self.base / "skill.md"
        self.skill.write_text("General reusable coding guidance.")
        self.config = {"schema":"lore.skill-benchmark-runtime.v1", "provider":"claude",
                       "codex_profile":None,
                       "model":"operator-exact-model", "runtime":"pinned.subscription.test",
                       "cli":RUNTIME.pin(self.cli), "sandbox":RUNTIME.pin(self.sandbox),
                       "auth_file":str(self.base / "oauth-only"),
                       "limits":{"model_seconds":2,"verify_seconds":5,"output_bytes":32768,"source_bytes":65536},
                       "spend":{"subscription_enabled":False,"max_calls":0,"ledger":str(self.private / "calls.jsonl")}}
        self.load_task("train-empty-replace")

    def load_task(self, task_id):
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        shutil.copytree(CORPUS / "tasks" / task_id, self.workspace)
        self.task_id = task_id
        self.original = (self.workspace / "benchmark.py").read_text()
        (self.base / "response.py").write_text(minimal_repair(task_id, self.original))

    def enable(self, provider="claude", calls=1):
        self.config["provider"] = provider
        self.config["spend"].update(subscription_enabled=True,max_calls=calls)
        self.config["codex_profile"] = None if provider == "claude" else {
            "schema":"lore.codex-subscription-tools.v1", "cli_sha256":self.config["cli"]["sha256"],
            "model":self.config["model"], "catalog":{"models":[RUNTIME.harden_codex_model({"slug":self.config["model"]})]},
            "tool_names":["request_user_input"],"rejected_calls":["exec_command","apply_patch","request_user_input"]}
        if self.config["codex_profile"]:
            self.config["codex_profile"]["catalog_sha256"] = hashlib.sha256(json.dumps(
                self.config["codex_profile"]["catalog"],sort_keys=True).encode()).hexdigest()

    @contextlib.contextmanager
    def evaluation(self):
        previous = Path.cwd()
        environment = {"LORE_EVAL_MODEL":self.config["model"], "LORE_EVAL_RUNTIME":self.config["runtime"],
                       "LORE_EVAL_TASK_ID":self.task_id, "LORE_EVAL_SKILL":str(self.skill),
                       "HOME":"/operator/real-home", "OPENAI_API_KEY":"OPERATOR_SECRET",
                       "ANTHROPIC_API_KEY":"OPERATOR_SECRET", "CODEX_API_KEY":"OPERATOR_SECRET",
                       "LORE_ROOT":"/never/use/production", "PYTHONPATH":"/untrusted/import/path"}
        with patch.dict(os.environ, environment):
            os.chdir(self.workspace)
            try:
                yield
            finally:
                os.chdir(previous)

    def run_model(self):
        output = io.StringIO()
        with self.evaluation(), contextlib.redirect_stdout(output):
            RUNTIME.run_model(self.config)
        return output.getvalue()

    def verify(self):
        checks = {self.task_id:(CORPUS / "checks" / (self.task_id + ".py")).read_text()}
        with self.evaluation(), contextlib.redirect_stdout(io.StringIO()):
            RUNTIME.run_verifier(self.config, checks)

    def audits(self):
        path = self.sandbox.with_suffix(".audit")
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    def test_disabled_default_starts_no_subprocess_or_ledger(self):
        with self.assertRaisesRegex(RUNTIME.Refused, "spend_disabled"):
            self.run_model()
        self.assertEqual(self.audits(), [])
        self.assertFalse(Path(self.config["spend"]["ledger"]).exists())
        self.assertEqual((self.workspace / "benchmark.py").read_text(), self.original)

    def test_both_subscription_providers_argv_prompt_environment_and_budget(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                self.config["spend"]["ledger"] = str(self.private / (provider + ".jsonl"))
                self.enable(provider)
                output = self.run_model()
                self.assertIn('"source_sha256"', output)
                self.assertNotIn("OPERATOR_SECRET", output)
                self.assertNotIn("do-not-retain", output)
                self.verify()
                with self.assertRaisesRegex(RUNTIME.Refused, "budget_exhausted"):
                    self.run_model()
        self.assertEqual(len(self.audits()), 6)  # auth + one generation + independent verifier, each provider
        for row in self.audits():
            self.assertNotIn("OPERATOR_SECRET", json.dumps(row))
            self.assertNotIn("/operator/real-home", json.dumps(row))

    def test_false_success_does_not_pass_any_independent_regression(self):
        for case in MANIFEST["cases"]:
            self.load_task(case["id"])
            with self.subTest(task=self.task_id):
                with self.assertRaisesRegex(RUNTIME.Refused, "subprocess_failed"):
                    self.verify()
                # Public checks succeed on the deliberately broken starting point.
                result = subprocess.run([sys.executable,"-B","test_public.py"], check=False, cwd=self.workspace,
                                        env=RUNTIME.clean_env(self.base),capture_output=True,timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                (self.workspace / "benchmark.py").write_text(minimal_repair(self.task_id, self.original))
                self.verify()

    def test_source_exiting_zero_is_not_a_completed_independent_check(self):
        (self.workspace / "benchmark.py").write_text("raise SystemExit(0)\n")
        with self.assertRaisesRegex(RUNTIME.Refused, "checks_incomplete"):
            self.verify()

    def test_provider_claiming_success_with_unchanged_source_fails_verifier(self):
        self.enable()
        (self.base / "response.py").write_text(self.original)
        self.run_model()
        with self.assertRaisesRegex(RUNTIME.Refused, "subprocess_failed"):
            self.verify()

    def test_api_auth_fails_before_model_call_but_consumes_attempt(self):
        for provider in ("claude", "codex"):
            self.config["spend"]["ledger"] = str(self.private / (provider + ".jsonl"))
            self.enable(provider)
            (self.base / "mode").write_text("api")
            with self.assertRaises(RUNTIME.Refused):
                self.run_model()
        self.assertEqual(len(self.audits()), 2)
        self.assertTrue(all("status" in row["argv"] for row in self.audits()))

    def test_codex_missing_stale_or_unsafe_tool_profiles_cannot_launch(self):
        self.enable("codex")
        good = copy.deepcopy(self.config["codex_profile"])
        for key, value in (("cli_sha256","stale"), ("model","other"),
                           ("tool_names",["exec_command"]), ("rejected_calls",[])):
            self.config["codex_profile"] = {**good,key:value}
            with self.assertRaises(RUNTIME.Refused):
                self.run_model()
        self.config["codex_profile"] = good
        self.config["codex_profile"]["catalog"]["models"][0]["apply_patch_tool_type"] = "freeform"
        with self.assertRaisesRegex(RUNTIME.Refused,"hardened_catalog"):
            self.run_model()
        self.assertEqual(self.audits(),[])

    def test_codex_known_alias_warning_preserves_strict_subscription_check(self):
        self.enable("codex")
        (self.base / "mode").write_text("warning")
        output = self.run_model()
        self.assertIn('"source_sha256"',output)
        self.assertNotIn("WARNING",output)
        for raw in (b"Logged in using an API key\n", b"unknown auth\nLogged in using ChatGPT\n",
                    b"Logged in using ChatGPT\nLogged in using ChatGPT\n",
                    b"WARNING: proceeding, even though we could not create PATH aliases: private sandbox\nLogged in using an API key\n"):
            with self.assertRaisesRegex(RUNTIME.Refused,"chatgpt_auth_required"):
                RUNTIME.subscription_status(self.config,raw)

    def test_flood_deadline_nonzero_and_malformed_outputs_fail_without_retries(self):
        for mode, reason in (("flood","output_limit"),("stderr_flood","output_limit"),
                             ("timeout","timeout"),("fail","subprocess_failed"),("malformed",None)):
            with self.subTest(mode=mode):
                self.config["spend"]["ledger"] = str(self.private / (mode + ".jsonl"))
                self.config["limits"]["model_seconds"] = 1
                self.enable()
                (self.base / "mode").write_text(mode)
                start = time.monotonic()
                with self.assertRaises((RUNTIME.Refused, ValueError)) as failure:
                    self.run_model()
                if reason:
                    self.assertIn(reason, str(failure.exception))
                self.assertLess(time.monotonic() - start, 3)
                self.assertEqual((self.workspace / "benchmark.py").read_text(), self.original)
        self.assertEqual(len(self.audits()), 10)

    def test_identity_changed_pin_unsafe_auth_and_bad_limits_are_refused(self):
        self.enable()
        with (self.evaluation(), patch.dict(os.environ, {"LORE_EVAL_MODEL":"other-model"}),
              self.assertRaisesRegex(RUNTIME.Refused,"identity_mismatch")):
            RUNTIME.run_model(self.config)
        self.cli.write_text(FAKE_CLI + "\n# changed\n")
        with self.assertRaisesRegex(RUNTIME.Refused,"executable_changed"):
            self.run_model()
        self.config["cli"] = RUNTIME.pin(self.cli)
        self.config["limits"]["model_seconds"] = float("nan")
        with self.assertRaisesRegex(RUNTIME.Refused,"limit_range"):
            self.run_model()
        self.config["limits"]["model_seconds"] = 1
        Path(self.config["auth_file"]).chmod(0o644)
        with self.assertRaisesRegex(RUNTIME.Refused,"private_auth_file"):
            self.run_model()
        self.assertEqual(self.audits(), [])

    def test_credentials_are_never_opened_and_raw_errors_not_retained(self):
        self.enable()
        original_open = os.open
        def checked_open(path, *args, **kwargs):
            self.assertNotEqual(str(path), self.config["auth_file"])
            return original_open(path, *args, **kwargs)
        with patch.object(RUNTIME.os, "open", side_effect=checked_open):
            self.run_model()
        (self.base / "mode").write_text("fail")
        self.config["spend"]["ledger"] = str(self.private / "failure.jsonl")
        output = io.StringIO()
        with self.evaluation(), contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            RUNTIME.guarded_main(RUNTIME.run_model, self.config)
        self.assertNotIn("OPERATOR_SECRET", output.getvalue())
        self.assertIn("subprocess_failed", output.getvalue())

    def test_failure_classification_is_allow_listed_and_retains_no_messages(self):
        for mode, category in (("auth_readonly","auth_refresh_read_only"),
                               ("unsupported_model","provider_model_unsupported")):
            self.enable("codex")
            self.config["spend"]["ledger"] = str(self.private / (mode + ".jsonl"))
            (self.base / "mode").write_text(mode)
            with self.assertRaises(RUNTIME.SubprocessFailure) as failure:
                self.run_model()
            diagnostic = failure.exception.safe_diagnostics
            self.assertEqual(set(diagnostic),{"category","exit_code","event_types","http_status_codes"})
            self.assertEqual(diagnostic["category"],category)
            self.assertNotIn("OPERATOR_SECRET",json.dumps(diagnostic))
            self.assertNotIn("do-not-retain",json.dumps(diagnostic))
            if mode == "unsupported_model":
                self.assertEqual(diagnostic["event_types"],["error","turn.failed"])
                self.assertEqual(diagnostic["http_status_codes"],[400])
        unknown = RUNTIME.failure_diagnostics(b'{"type":"OPERATOR_SECRET","status_code":123,"source":"ANSWER"}',
                                              b'account@invalid OPERATOR_SECRET',7)
        self.assertEqual(unknown,{"category":"unclassified_process_failure","exit_code":7,
                                  "event_types":[],"http_status_codes":[]})
        malformed = RUNTIME.failure_diagnostics(b'{"type":{"OPERATOR_SECRET":"ANSWER"}}',b'',1)
        self.assertEqual(malformed["category"],"unclassified_process_failure")
        self.assertEqual(malformed["event_types"],[])

    def test_preparation_freezes_standalone_scripts_and_disables_spend(self):
        config_file = self.base / "config.json"
        config_file.write_text(json.dumps(self.config))
        prepared = self.base / "prepared"
        command = [sys.executable,str(REPO / "scripts/skill-benchmark.py"),"prepare",
                   "--config",str(config_file),"--output",str(prepared)]
        result = subprocess.run(command,check=False,env=RUNTIME.clean_env(self.base),capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        plan = json.loads(result.stdout)
        self.assertFalse(plan["subscription_enabled"])
        self.assertEqual(plan["maximum_model_invocations"],8)
        manifest = json.loads((prepared / "suite.json").read_text())
        self.assertEqual({case["split"] for case in manifest["cases"]}, {"train","replay","validation","test"})
        self.assertNotIn("StartupChecks", (prepared / "runner.py").read_text())
        self.assertIn("StartupChecks", (prepared / "verifier.py").read_text())
        with self.evaluation():
            result = subprocess.run([sys.executable,"-I",str(prepared / "runner.py")],check=False,env=dict(os.environ),
                                    capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,1)
        self.assertIn("spend_disabled",result.stdout)
        self.assertEqual(self.audits(),[])
        # A separate explicitly enabled suite has a frozen enabled gate.
        command[command.index(str(prepared))] = str(self.base / "enabled")
        result = subprocess.run(command+["--enable-subscription","--max-calls","1"],
                                check=False,env=RUNTIME.clean_env(self.base),capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)["subscription_enabled"])
        enabled = self.base / "enabled"
        with self.evaluation():
            for script in ("runner.py", "verifier.py"):
                result = subprocess.run([sys.executable,"-I",str(enabled / script)],check=False,
                                        env=dict(os.environ),capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(len(self.audits()),3)

    def test_explicit_smoke_is_one_training_invocation_and_retains_safe_counts(self):
        config_file = self.base / "smoke-config.json"
        config_file.write_text(json.dumps(self.config))
        output = self.base / "smoke"
        command = [sys.executable,str(REPO / "scripts/skill-benchmark-smoke.py"),
                   "--config",str(config_file),"--output",str(output),"--enable-subscription"]
        result = subprocess.run(command,check=False,env=RUNTIME.clean_env(self.base),capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        report = json.loads(result.stdout)
        self.assertTrue(report["passed"])
        self.assertEqual(report["cli_model_invocations"],1)
        self.assertEqual(report["auth_preflights"],1)
        self.assertEqual(report["task"],"train-empty-replace")
        self.assertNotIn("OPERATOR_SECRET",result.stdout)
        second = command.copy()
        second[second.index(str(output))] = str(self.base / "second-smoke")
        result = subprocess.run(second,check=False,env=RUNTIME.clean_env(self.base),capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,1)
        self.assertEqual(json.loads(result.stdout)["cli_model_invocations"],0)
        self.assertEqual(len(self.audits()),3)
        before = (output / "smoke.json").read_bytes()
        subprocess.run(command,check=False,env=RUNTIME.clean_env(self.base),capture_output=True,timeout=10)
        self.assertEqual((output / "smoke.json").read_bytes(),before)

    def test_call_ledger_serializes_competing_processes(self):
        self.enable(calls=1)
        path = self.base / "concurrent-config.json"
        path.write_text(json.dumps(self.config))
        code = ("import importlib.util,json;"
                "s=importlib.util.spec_from_file_location('runtime'," + repr(str(REPO / "scripts/skill-benchmark-runtime.py")) + ");"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "m.reserve_call(json.load(open(" + repr(str(path)) + ")))")
        children = [subprocess.Popen([sys.executable,"-B","-c",code],env=RUNTIME.clean_env(self.base),
                                     stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
        for child in children:
            child.communicate(timeout=5)
        self.assertEqual(sorted(child.returncode for child in children),[0,1])
        self.assertEqual(len(Path(self.config["spend"]["ledger"]).read_text().splitlines()),1)

    def test_corpus_integrity_split_uniqueness_license_and_no_gold_fixtures(self):
        digests = set()
        for name, digest in MANIFEST["files"].items():
            self.assertEqual(hashlib.sha256((CORPUS / name).read_bytes()).hexdigest(),digest,name)
        for case in MANIFEST["cases"]:
            fixture = CORPUS / case["fixture"]
            self.assertEqual({p.name for p in fixture.iterdir()}, {"TASK.md","benchmark.py","test_public.py","case.json"})
            digest = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(fixture.iterdir()))).hexdigest()
            self.assertNotIn(digest,digests)
            digests.add(digest)
        self.assertEqual(MANIFEST["license"],"AGPL-3.0-only")
        self.assertEqual(len(MANIFEST["cases"]),4)


if __name__ == "__main__":
    unittest.main()
