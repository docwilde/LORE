#!/usr/bin/env python3
"""Exercise a disposable private hub with the installed native LORE CLI.

Credentials exist only in memory and child environments. Evidence contains
selected counters, hashes, Docker metadata, and scrubbed service logs.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class Failure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Failure(message)


class Verifier:
    def __init__(self, args):
        self.args = args
        self.secrets = []
        self.cleanup_allowed = False
        self.tunnel = None
        self.transport_url = args.api_url
        self.tailnet_host = urllib.parse.urlsplit(args.api_url).hostname
        self.ssh_options = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                            "-o", "HostName=" + self.tailnet_host,
                            "-o", "HostKeyAlias=" + args.ssh_host,
                            "-o", "StrictHostKeyChecking=yes",
                            "-o", "ControlPath=none"]
        self.report = {"result": "running", "source_sha": args.source_sha,
                       "project": args.project, "steps": [], "cleanup": {}}
        self.compose = ["docker", "compose", "--project-directory", args.remote_dir,
                        "-p", args.project, "-f", args.remote_dir + "/" + args.compose_file]
        self.output = Path(args.report_dir)
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)

    def scrub(self, text):
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
        text = re.sub(r"(?i)postgres(?:ql)?://[^\s\"']+", "<database-url-redacted>", text)
        text = re.sub(r"(?i)bearer\s+[^\s\"']+", "Bearer <redacted>", text)
        return text

    def process(self, argv, *, env=None, data=None, timeout=45, allowed=(0,)):
        try:
            p = subprocess.run(argv, input=data, env=env, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Failure("subprocess " + type(exc).__name__) from None
        require(p.returncode in allowed,
                f"subprocess exit {p.returncode}; stderr bytes {len(p.stderr)} (redacted)")
        return p

    def remote(self, argv, *, data=None, allowed=(0,), timeout=45):
        return self.process([*self.ssh_options, self.args.ssh_host, shlex.join(argv)],
                            data=data, allowed=allowed, timeout=timeout)

    def ssh_route_proof(self):
        connection = self.remote(["printenv", "SSH_CONNECTION"]).stdout.decode().split()
        require(len(connection) == 4, "SSH connection metadata is unavailable")
        server_ip = ipaddress.ip_address(connection[2])
        require(server_ip in ipaddress.ip_network("100.64.0.0/10") and
                str(server_ip) == self.tailnet_host,
                "SSH must terminate at the literal Tailnet IP from the API URL")
        return {"ssh_server_is_tailnet": True, "ssh_server_matches_api_ip": True,
                "ssh_host_override": self.tailnet_host,
                "host_key_alias": self.args.ssh_host,
                "strict_host_key_checking": True, "existing_ssh_multiplexing_disabled": True}

    def remote_json(self, argv, **kwargs):
        try:
            return json.loads(self.remote(argv, **kwargs).stdout)
        except ValueError:
            raise Failure("remote response was not JSON (redacted)") from None

    def docker_state(self):
        # Read only selected fields; never emit Docker's environment or DSN.
        code = r'''
import json,subprocess,sys
project,volume,sentinel=sys.argv[1:]
def run(*args):
 p=subprocess.run(['docker',*args],capture_output=True,text=True)
 if p.returncode: raise RuntimeError('docker inspection failed')
 return json.loads(p.stdout)
def selected(c):
 return {'id':c['Id'],'name':c['Name'].lstrip('/'),
 'running':c['State']['Running'],'started_at':c['State']['StartedAt'],
 'labels':{k:v for k,v in c['Config']['Labels'].items() if k.startswith('com.docker.compose.')},
 'bindings':c['HostConfig']['PortBindings'],'ports':c['NetworkSettings']['Ports'],
 'networks':sorted(c['NetworkSettings']['Networks']),
 'mounts':[{'type':m['Type'],'name':m.get('Name'),'destination':m['Destination']} for m in c['Mounts']]}
ids=subprocess.run(['docker','ps','-aq','--filter','label=com.docker.compose.project='+project],capture_output=True,text=True,check=True).stdout.split()
containers=[selected(c) for c in run('inspect',*ids)] if ids else []
nets=sorted({n for c in containers for n in c['networks']})
networks=[{'name':n['Name'],'internal':n['Internal'],'project':n.get('Labels',{}).get('com.docker.compose.project')} for n in run('network','inspect',*nets)] if nets else []
vol=run('volume','inspect',volume)[0]
print(json.dumps({'containers':containers,'networks':networks,
 'volume':{'name':vol['Name'],'labels':vol['Labels']},'sentinel':selected(run('inspect',sentinel)[0])}))
'''
        return self.remote_json(["python3", "-c", code, self.args.project,
                                 self.args.volume, self.args.preserve_container])

    def preflight(self):
        state = self.docker_state()
        by_service = {c["labels"].get("com.docker.compose.service"): c
                      for c in state["containers"]}
        require(len(state["containers"]) == 2 and set(by_service) == {"api", "postgres"},
                "unexpected disposable project membership")
        for service, c in by_service.items():
            require(c["name"] == self.args.project + "-" + service + "-1" and c["running"],
                    "expected disposable container is not running")
            require(c["labels"].get("com.docker.compose.project.working_dir") == self.args.remote_dir,
                    "compose working directory differs")
            require(c["labels"].get("com.docker.compose.project.config_files") ==
                    self.args.remote_dir + "/" + self.args.compose_file, "compose file differs")
        db, api = by_service["postgres"], by_service["api"]
        require(not db["bindings"] and all(not v for v in db["ports"].values()),
                "disposable database has a published port")
        require(db["mounts"] == [{"type": "volume", "name": self.args.volume,
                                 "destination": "/var/lib/postgresql/data"}],
                "unexpected database mount")
        require(not api["mounts"], "API has unexpected host or volume mounts")
        require(state["volume"]["labels"].get("com.docker.compose.project") == self.args.project,
                "volume does not belong to disposable project")
        # Scope is now proven even if a subsequent exposure/health check fails.
        self.cleanup_allowed = True
        self.before = state
        endpoint = urllib.parse.urlsplit(self.args.api_url)
        expected = {f"{self.args.container_port}/tcp":
                    [{"HostIp": endpoint.hostname, "HostPort": str(endpoint.port)}]}
        require(api["bindings"] == expected and api["ports"] == expected,
                "API port is not effectively published only on the requested Tailnet address")
        nets = {n["name"]: n for n in state["networks"]}
        require(all(nets[n]["internal"] for n in db["networks"]),
                "database is attached to an external network")
        require(set(db["networks"]) <= set(api["networks"]), "API cannot share internal DB network")
        require(all(n["project"] == self.args.project for n in nets.values()),
                "project shares an unrelated network")
        self.report["docker_before"] = state
        database = self.remote(self.compose + ["exec", "-T", "postgres", "sh", "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT current_database()"']).stdout.decode().strip()
        require(database == self.args.database, "unexpected disposable database")
        self.report["database"] = database

    def source_proof(self):
        expected = {}
        with tarfile.open(self.args.source_archive) as archive:
            files = [m for m in archive.getmembers() if m.isfile()]
            prefixes = {m.name.split("/", 1)[0] for m in files}
            require(len(prefixes) == 1 and next(iter(prefixes)).endswith(self.args.source_sha),
                    "source archive root does not identify expected commit")
            for m in files:
                relative = m.name.split("/", 1)[1]
                require(not relative.startswith("/") and ".." not in Path(relative).parts,
                        "unsafe source archive path")
                expected[relative] = hashlib.sha256(archive.extractfile(m).read()).hexdigest()
        code = r'''
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve();expected=json.load(sys.stdin)
actual={}
for name in expected:
 p=root/name
 if not p.resolve().is_relative_to(root): raise RuntimeError('unsafe source path')
 actual[name]=hashlib.sha256(p.read_bytes()).hexdigest()
print(json.dumps(actual))
'''
        actual = self.remote_json(["python3", "-c", code, self.args.remote_dir],
                                  data=json.dumps(expected).encode())
        require(actual == expected, "staged source differs from supplied commit archive")
        package_expected = {k.removeprefix("src/lore_hub/"): v for k, v in expected.items()
                            if k.startswith("src/lore_hub/")}
        code = r'''
import hashlib,json,pathlib,lore_hub
root=pathlib.Path(lore_hub.__file__).parent
print(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}))
'''
        installed = self.remote_json(self.compose + ["exec", "-T", "api", "python", "-c", code])
        require(installed == package_expected, "running installed hub code differs from source archive")
        self.report["source_proof"] = {"staged_files_verified": len(expected),
            "installed_package_files_verified": len(installed), "matches_expected_commit_archive": True,
            "archive_sha256": hashlib.sha256(Path(self.args.source_archive).read_bytes()).hexdigest()}

    def http(self, path, *, token=None, method="GET", body=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.transport_url.rstrip("/") + path, headers=headers,
                                     method=method, data=json.dumps(body).encode() if body is not None else None)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=8) as response:
                return response.status, json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read(4096))
        except (OSError, ValueError):
            raise Failure("private hub HTTP connection or JSON response failed") from None

    def open_transport(self):
        if not self.args.ssh_tunnel:
            self.report["transport"] = {"mode": "direct-tailnet-http"}
            return
        route = self.ssh_route_proof()
        endpoint = urllib.parse.urlsplit(self.args.api_url)
        try:
            status, _ = self.http("/v1/health")
            direct = {"status": status}
        except Failure:
            direct = {"reachable": False, "diagnostic": "direct HTTP connection failed"}
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            local_port = reservation.getsockname()[1]
        self.tunnel = subprocess.Popen([*self.ssh_options,
            "-o", "ExitOnForwardFailure=yes", "-N", "-L",
            f"127.0.0.1:{local_port}:{endpoint.hostname}:{endpoint.port}", self.args.ssh_host],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            require(self.tunnel.poll() is None, "temporary SSH forward exited")
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=.2):
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise Failure("temporary SSH forward did not become ready")
        self.transport_url = f"http://127.0.0.1:{local_port}"
        self.report["transport"] = {"mode": "loopback-ssh-forward-over-tailnet", **route,
            "direct_http_probe": direct,
            "forward_is_temporary": True, "local_bind": "127.0.0.1"}

    def mint(self, account, machine):
        result = self.remote(self.compose + ["exec", "-T", "api", "lore-hub", "token", "new",
            "--account", account, "--machine", machine, "--scopes", "push,pull"])
        match = re.search(rb"^token:\s+(\S+)\s*$", result.stdout, re.MULTILINE)
        require(match is not None, "fixture token could not be captured (output redacted)")
        token = match.group(1).decode()
        self.secrets.append(token)
        return token

    def native(self, label, env, *args):
        start = time.monotonic()
        result = self.process([self.args.cli, *args, "--cwd", str(self.cwd)], env=env, timeout=30)
        require(not result.stderr, "native command emitted stderr (redacted)")
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise Failure("native response was not JSON (redacted)") from None
        self.report["steps"].append({"step": label, "exit_code": 0, "stderr_bytes": 0,
                                     "elapsed_ms": round((time.monotonic() - start) * 1000)})
        return value

    def clean_counters(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"failed", "unverified", "deferred", "quarantined", "unknown",
                           "staged", "may_have_applied", "conflicts", "unpushed"}:
                    require(item == 0, "nonzero sync counter: " + key)
                require(key != "error" or item in (None, ""), "sync response reports an error")
                self.clean_counters(item)
        elif isinstance(value, list):
            for item in value:
                self.clean_counters(item)

    def ledger(self, root, key):
        with sqlite3.connect((root / "state.db").as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute("SELECT op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,applied FROM sync_ops ORDER BY op_id")]
            peers = [dict(r) for r in db.execute("SELECT pushed_seq,pulled_cursor,last_push,last_pull,last_error FROM sync_peers")]
        for row in rows:
            envelope = [row[n] for n in ("op_id", "machine_id", "machine_seq", "lamport", "class", "op", "project_key")]
            envelope.append(json.loads(row["payload"]))
            canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            require(row["mac"] is not None and hmac.compare_digest(
                hmac.new(key.encode(), canonical, hashlib.sha256).hexdigest(), row["mac"]),
                "native fixture operation signature did not verify")
            require(row["applied"] == 1, "operation is not applied")
        require(len(peers) == 1 and peers[0]["last_error"] is None, "unexpected or failed banked cursor")
        return rows, peers[0]

    def curated(self, root):
        paths = [root / "USER.md", *root.glob("projects/*/MEMORY.md")]
        return {str(p.relative_to(root)): p.read_bytes() for p in paths if p.exists()}

    def convergence(self):
        version = self.process([self.args.cli, "--version"]).stdout.decode().strip()
        require(version.removeprefix("lore-rs ") == self.args.cli_version, "installed CLI version differs")
        self.report["cli"] = {"path": self.args.cli, "version": version,
                              "sha256": hashlib.sha256(Path(self.args.cli).read_bytes()).hexdigest()}
        status, health = self.http("/v1/health")
        require(status == 200 and health.get("ok") is True, "health is not reachable over Tailnet")
        get_status, _ = self.http("/v1/ops")
        post_status, _ = self.http("/v1/ops", method="POST", body={})
        require(get_status == post_status == 401, "unauthenticated ops were not refused")
        self.report["http"] = {"health_status": status, "health": health,
                               "unauthenticated_get_ops": get_status, "unauthenticated_post_ops": post_status}
        with tempfile.TemporaryDirectory(prefix="lore-private-hub-fixtures-") as directory:
            fixture = Path(directory)
            self.cwd = fixture / "project"
            self.cwd.mkdir()
            signing_key = secrets.token_hex(32)
            self.secrets.append(signing_key)
            account = "fixture-private-hub-" + uuid.uuid4().hex
            roots, envs = {}, {}
            for label in ("A", "B"):
                root = fixture / label
                root.mkdir(mode=0o700)
                machine = str(uuid.uuid4())
                token = self.mint(account, machine)
                # Do not inherit API keys, real LORE config, roots, or proxies.
                env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                    "CLAUDE_CONFIG_DIR": str(root / "config"), "LORE_ROOT": str(root),
                    "LORE_PROJECTS_DIR": str(root / "session-projects"),
                    "LORE_CODEX_SESSIONS_DIR": str(root / "codex-sessions"),
                    "LORE_SKILLS_DIR": str(root / "skills"), "LORE_MACHINE_ID": machine,
                    "LORE_SYNC_URL": self.transport_url, "LORE_SYNC_AUTH": "token",
                    "LORE_SYNC_TOKEN": token, "LORE_SYNC_HMAC_KEY": signing_key,
                    "LORE_SYNC_CLASSES": "memory", "LORE_DISABLE_SYNC": "0",
                    # Explicitly authorized synthetic writes; never real roots.
                    "LORE_WRITE_GATE": "off",
                    "LORE_DISABLE_REVIEW": "1", "LORE_DISABLE_INDEX": "1", "LORE_SYNC_TIMEOUT": "8"}
                roots[label], envs[label] = root, env
                who = self.native(label + " authenticated identity", env, "sync", "whoami")
                require(who.get("machine_id") == machine and who.get("account") == account,
                        "fixture token identity differs from its native machine")
            facts = {"user": [["Synthetic private hub fixture A first fact.",
                               "Synthetic private hub fixture A second fact."],
                              ["Synthetic private hub fixture B first fact.",
                               "Synthetic private hub fixture B second fact."]]}
            for scope in facts:
                for text in facts[scope][0]:
                    result = self.native("A write " + scope, envs["A"], "memory", "add", "--scope", scope, text)
                    require(result.get("status") != "staged", "fixture write unexpectedly staged")
            require(not list((roots["A"] / "pending").glob("*")), "fixture A has pending proposals")
            push_a = self.native("A signed push", envs["A"], "sync", "push")
            require(push_a.get("accepted") == 2, "A push did not accept both signed memories")
            boot_b = self.native("B bootstrap", envs["B"], "sync", "bootstrap")
            self.clean_counters(boot_b)
            require(boot_b.get("applied") == 2 and boot_b.get("cursor_advanced") is True,
                    "B bootstrap did not apply both memories and bank a cursor")
            require(self.curated(roots["A"]) == self.curated(roots["B"]), "bootstrap curated bytes differ")
            for scope in facts:
                for text in facts[scope][1]:
                    result = self.native("B write " + scope, envs["B"], "memory", "add", "--scope", scope, text)
                    require(result.get("status") != "staged", "fixture write unexpectedly staged")
            require(not list((roots["B"] / "pending").glob("*")), "fixture B has pending proposals")
            push_b = self.native("B signed push", envs["B"], "sync", "push")
            require(push_b.get("accepted") == 2, "B push did not accept both signed memories")
            pull_a = self.native("A pull B changes", envs["A"], "sync", "pull")
            self.clean_counters(pull_a)
            require(pull_a["hub"].get("applied") == 2 and pull_a["hub"].get("cursor_advanced") is True,
                    "A pull did not apply B changes and bank a cursor")
            curated = self.curated(roots["A"])
            require(curated == self.curated(roots["B"]) and len(curated) == 1, "final curated files differ")
            for name, body in curated.items():
                scope = "user" if name == "USER.md" else "project"
                require(sorted(line[2:] for line in body.decode().splitlines() if line.startswith("- ")) ==
                        sorted(facts[scope][0] + facts[scope][1]), "curated state differs from exact fixture facts")
            before_repeat, cursors, initial_rows = {}, {}, {}
            for label, root in roots.items():
                rows, peer = self.ledger(root, signing_key)
                require(len(rows) == 4, "fixture ledger should contain four signed operations")
                initial_rows[label] = rows
                before_repeat[label] = self.curated(root)
                require(int(peer["pulled_cursor"] or 0) > 0 and peer["last_pull"] and peer["last_push"],
                        "successful transport cursors were not banked")
                cursors[label] = {"pushed_seq": peer["pushed_seq"], "pulled_cursor": peer["pulled_cursor"]}
            require(initial_rows["A"] == initial_rows["B"], "signed ledgers differ")
            repeat = {}
            for label, root in roots.items():
                forced = self.native(label + " forced duplicate push", envs[label], "sync", "push", "--from-seq", "0")
                require(forced.get("accepted") == 0 and forced.get("duplicate") == 2,
                        "hub did not deduplicate native signed replay")
                noop = self.native(label + " repeat push", envs[label], "sync", "push")
                require(noop.get("accepted") == noop.get("duplicate") == 0, "repeat push was not a no-op")
                pull = self.native(label + " repeat pull", envs[label], "sync", "pull")
                status = self.native(label + " final status", envs[label], "sync", "status")
                require(isinstance(status, dict) and all(k in status for k in
                        ("conflicts", "quarantined", "unpushed", "unverified")),
                        "final native sync status is unavailable or incomplete")
                self.clean_counters(pull)
                self.clean_counters(status)
                require(pull["hub"].get("fetched") == pull["hub"].get("applied") == 0,
                        "repeat pull was not a no-op")
                rows, peer = self.ledger(root, signing_key)
                require(rows == initial_rows[label] and self.curated(root) == before_repeat[label],
                        "replay changed signed ledger or curated bytes")
                require({"pushed_seq": peer["pushed_seq"], "pulled_cursor": peer["pulled_cursor"]} == cursors[label],
                        "repeat changed settled cursor positions")
                repeat[label] = {"duplicate_push": forced, "noop_push": noop, "noop_pull": pull, "status": status}
            self.report["convergence"] = {"A_push": push_a, "B_bootstrap": boot_b,
                "B_push": push_b, "A_pull": pull_a, "curated_byte_identical": True,
                "curated_files": len(curated), "exact_fixture_facts": 4,
                "curated_sha256": {k: hashlib.sha256(v).hexdigest() for k, v in curated.items()},
                "independently_verified_signed_ops_per_store": 4, "banked_cursors": cursors,
                "repeat": repeat, "local_fixture_roots_removed": True}

    def finish(self):
        if not self.cleanup_allowed:
            self.report["cleanup"] = {"performed": False, "reason": "project ownership not proven"}
            return
        try:
            logs = self.remote(self.compose + ["logs", "--no-color", "--timestamps", "--tail", "200"], timeout=30)
            scrubbed = self.scrub((logs.stdout + logs.stderr).decode(errors="replace"))
            (self.output / "service-logs.txt").write_text(scrubbed)
            remote_log = self.args.remote_dir + "/private-hub-verifier-" + self.output.name + ".log"
            code = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); f=p.open('xb'); f.write(sys.stdin.buffer.read()); f.close()"
            self.remote(["python3", "-c", code, remote_log],
                        data=scrubbed.encode())
            self.report["service_logs"] = {"local": "service-logs.txt",
                "remote": remote_log, "scrubbed": True}
        except Exception:  # noqa: BLE001
            # Unexpected evidence/redaction failures must not prevent teardown.
            self.report["service_logs"] = {"capture_failed": True}
        if not self.args.cleanup:
            self.report["cleanup"] = {"performed": False, "reason": "--cleanup not supplied"}
            return
        try:
            self.remote(self.compose + ["down", "--volumes", "--timeout", "10"], timeout=60)
            remaining = self.remote(["docker", "ps", "-aq", "--filter",
                                     "label=com.docker.compose.project=" + self.args.project]).stdout.strip()
            volume = self.remote(["docker", "volume", "inspect", self.args.volume], allowed=(0, 1))
            networks = self.remote(["docker", "network", "ls", "-q", "--filter",
                                    "label=com.docker.compose.project=" + self.args.project]).stdout.strip()
            require(not remaining and volume.returncode == 1 and not networks,
                    "disposable containers, volume, or networks remain")
            code = r'''
import json,subprocess,sys
c=json.loads(subprocess.check_output(['docker','inspect',sys.argv[1]]))[0]
print(json.dumps({'id':c['Id'],'running':c['State']['Running'],'started_at':c['State']['StartedAt'],'ports':c['NetworkSettings']['Ports']}))
'''
            sentinel = self.remote_json(["python3", "-c", code, self.args.preserve_container])
            require(sentinel == {k: self.before["sentinel"][k] for k in sentinel},
                    "unrelated preserved container changed")
            self.report["cleanup"] = {"performed": True, "containers_absent": True, "volume_absent": True,
                "networks_absent": True,
                "preserved_unrelated_container_unchanged": True,
                "remote_source_and_logs_retained": True}
        except Exception as exc:  # noqa: BLE001
            # Always retain a cleanup report; exception text could contain credentials.
            self.report["cleanup"] = {"performed": True, "error": type(exc).__name__}

    def run(self):
        try:
            self.report["control_transport"] = self.ssh_route_proof()
            self.preflight()
            self.source_proof()
            self.open_transport()
            self.convergence()
            self.report["result"] = "passed"
        except Exception as exc:  # noqa: BLE001
            # Keep boundary diagnostics scrubbed while finally tears down resources.
            self.report["result"] = "failed"
            self.report["failure"] = self.scrub(str(exc)) if isinstance(exc, Failure) else type(exc).__name__
        finally:
            self.finish()
            if self.tunnel is not None:
                self.tunnel.terminate()
                try:
                    self.tunnel.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.tunnel.kill()
                    self.tunnel.wait()
                if self.tunnel.stderr is not None:
                    self.tunnel.stderr.close()
                self.report["cleanup"]["temporary_ssh_forward_stopped"] = True
            if self.args.cleanup and not self.report["cleanup"].get("volume_absent"):
                self.report["result"] = "failed"
            encoded = self.scrub(json.dumps(self.report, indent=2)) + "\n"
            (self.output / "report.json").write_text(encoded)
            print(json.dumps({"result": self.report["result"], "report": str(self.output / "report.json"),
                              "cleanup": self.report["cleanup"]}))
        return 0 if self.report["result"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("ssh-host", "remote-dir", "project", "volume", "api-url", "source-sha", "source-archive", "report-dir"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--compose-file", default="compose.test.yml")
    parser.add_argument("--database", default="lore_hub_test")
    parser.add_argument("--container-port", default=8080, type=int)
    parser.add_argument("--preserve-container", default="lore-hub-postgres")
    parser.add_argument("--cli", default=str(Path.home() / ".local/bin/lore-rs"))
    parser.add_argument("--cli-version", default="0.62.29")
    parser.add_argument("--cleanup", action="store_true", help="remove only the verified disposable compose project and its volume")
    parser.add_argument("--ssh-tunnel", action="store_true",
                        help="use a temporary local loopback forward over the authorized private SSH connection")
    args = parser.parse_args()
    require(re.fullmatch(r"[a-z0-9][a-z0-9_-]+", args.project) is not None, "invalid disposable project")
    require(args.volume == args.project + "_pgdata", "volume must be the explicit disposable project pgdata volume")
    require(args.remote_dir.startswith("/tmp/") and ".." not in Path(args.remote_dir).parts,
            "remote disposable source must be below /tmp")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+", args.compose_file) is not None, "compose file must be a basename")
    require(re.fullmatch(r"[0-9a-f]{40}", args.source_sha) is not None, "expected full source SHA required")
    url = urllib.parse.urlsplit(args.api_url)
    require(url.scheme == "http" and not url.username and not url.password and not url.query and not url.fragment
            and url.path in ("", "/") and url.port is not None, "credential-free private HTTP origin required")
    require(ipaddress.ip_address(url.hostname) in ipaddress.ip_network("100.64.0.0/10"), "API must use a Tailnet IP")
    os.umask(0o077)
    return Verifier(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
