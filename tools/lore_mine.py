"""Measure lore's effect on daily work by mining Claude Code transcripts.

The script reads the raw JSONL transcripts under ~/.claude/projects/ and
counts, per day and per period, four signals in the user's own messages:

  user_msgs   genuine typed user text messages (tool results, slash-command
              XML, hook and system-reminder payloads, and meta lines are
              excluded)
  corrections user messages matching a narrow correction lexicon ("I told
              you", "that's wrong", "you ignored", ...) — a proxy for the
              model violating something the user expects it to know
  interrupts  "[Request interrupted by user...]" markers
  denials     tool_result blocks carrying "doesn't want to proceed"

The `--boundary` date splits the data into a pre and a post period; pass the
date lore's inject stage went live on the machine. The summary compares the
two periods and reports a two-proportion z-score for the correction rate.

Headless lore workers (deriver, dreamer, backfill) write transcripts through
`claude --bare -p`, and their digest prompts are saturated with
correction-shaped text; the script drops every message whose `entrypoint` is
not an interactive CLI session.

The correction lexicon is a proxy, not ground truth: it undercounts terse
corrections and overcounts quoted ones. Read the rates as a trend, and treat
a z-score under ~2 as noise.

Usage:
    python3 tools/lore_mine.py --boundary 2026-08-21
    python3 tools/lore_mine.py --boundary 2026-08-21 --project my-repo
"""

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

CORRECTION = re.compile(
    r"(?:^no\b[,.! ]"
    r"|\bnot what i (?:asked|meant|said|wanted)"
    r"|\bi (?:already )?told you"
    r"|\bas i said"
    r"|\bi said\b"
    r"|\byou ignored"
    r"|\byou didn'?t (?:read|listen|do|follow)"
    r"|\bwhy did you"
    r"|\bthat'?s (?:still )?wrong"
    r"|\bstill (?:wrong|broken|failing)"
    r"|\bundo (?:that|this)"
    r"|\brevert (?:that|this)"
    r"|\bdon'?t do that"
    r"|\bstop\b[,.! ]"
    r"|\bfalsch\b)",
    re.IGNORECASE,
)

NOISE_PREFIXES = (
    "<command-name>", "<local-command-stdout>", "<command-message>",
    "<system-reminder>", "Caveat: the messages below",
)

# Interactive sessions stamp entrypoint "cli"; transcripts from Claude Code
# versions that predate the field carry none. Everything else (sdk-cli, ...)
# is a headless run.
INTERACTIVE_ENTRYPOINTS = {"cli", None}


def user_text(obj):
    """Return the typed user text of a transcript line, or None."""
    if obj.get("type") != "user" or obj.get("isMeta"):
        return None
    content = obj.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts) if parts else None
    return None


def denial_in(obj):
    if obj.get("type") != "user":
        return False
    content = obj.get("message", {}).get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            inner = block.get("content")
            text = inner if isinstance(inner, str) else json.dumps(inner or "")
            if "doesn't want to proceed" in text:
                return True
    return False


def scan(projects_dir, project_filter):
    daily = defaultdict(lambda: defaultdict(int))
    sessions_per_day = defaultdict(set)
    n_files = 0

    for path in projects_dir.glob("*/*.jsonl"):
        if project_filter and project_filter not in path.parent.name:
            continue
        n_files += 1
        try:
            with open(path, errors="replace") as fh:
                for line in fh:
                    if '"type":"user"' not in line and '"type": "user"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("entrypoint") not in INTERACTIVE_ENTRYPOINTS:
                        continue
                    ts = obj.get("timestamp", "")
                    if len(ts) < 10:
                        continue
                    day = ts[:10]
                    if denial_in(obj):
                        daily[day]["denials"] += 1
                        continue
                    text = user_text(obj)
                    if text is None:
                        continue
                    stripped = text.strip()
                    if stripped.startswith("[Request interrupted by user"):
                        daily[day]["interrupts"] += 1
                        continue
                    if not stripped or stripped.startswith(NOISE_PREFIXES):
                        continue
                    daily[day]["user_msgs"] += 1
                    sessions_per_day[day].add(obj.get("sessionId", path.stem))
                    if CORRECTION.search(stripped):
                        daily[day]["corrections"] += 1
        except OSError as exc:
            print(f"skip {path}: {exc}", file=sys.stderr)

    return n_files, daily, sessions_per_day


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--boundary", required=True, type=date.fromisoformat,
        help="date (YYYY-MM-DD) lore's inject stage went live; splits pre/post")
    parser.add_argument(
        "--project", default=None,
        help="substring filter on the project directory name")
    parser.add_argument(
        "--projects-dir", type=Path,
        default=Path.home() / ".claude" / "projects",
        help="transcript root (default: ~/.claude/projects)")
    args = parser.parse_args()

    n_files, daily, sessions_per_day = scan(args.projects_dir, args.project)
    print(f"scanned {n_files} transcripts\n")
    print(f"{'day':<12}{'sess':>5}{'umsg':>6}{'corr':>6}{'intr':>6}{'deny':>6}"
          f"{'corr%':>8}{'intr%':>8}{'deny%':>8}")

    period = {"pre": defaultdict(int), "post": defaultdict(int)}
    for day in sorted(daily):
        d = daily[day]
        u = d["user_msgs"]
        if not u:
            continue
        row = period["post" if date.fromisoformat(day) >= args.boundary else "pre"]
        for k in ("user_msgs", "corrections", "interrupts", "denials"):
            row[k] += d[k]
        row["sessions"] += len(sessions_per_day[day])
        print(f"{day:<12}{len(sessions_per_day[day]):>5}{u:>6}{d['corrections']:>6}"
              f"{d['interrupts']:>6}{d['denials']:>6}"
              f"{100 * d['corrections'] / u:>7.1f}%{100 * d['interrupts'] / u:>7.1f}%"
              f"{100 * d['denials'] / u:>7.1f}%")

    print(f"\n{'period':<26}{'sess':>6}{'umsg':>7}{'corr%':>8}{'intr%':>8}{'deny%':>8}")
    for name, row in period.items():
        u = row["user_msgs"]
        if not u:
            continue
        label = f"{name} (lore {'off' if name == 'pre' else 'on'})"
        print(f"{label:<26}{row['sessions']:>6}{u:>7}"
              f"{100 * row['corrections'] / u:>7.2f}%"
              f"{100 * row['interrupts'] / u:>7.2f}%"
              f"{100 * row['denials'] / u:>7.2f}%")

    pre, post = period["pre"], period["post"]
    if pre["user_msgs"] and post["user_msgs"]:
        r1 = pre["corrections"] / pre["user_msgs"]
        r2 = post["corrections"] / post["user_msgs"]
        pooled = ((pre["corrections"] + post["corrections"])
                  / (pre["user_msgs"] + post["user_msgs"]))
        se = math.sqrt(pooled * (1 - pooled)
                       * (1 / pre["user_msgs"] + 1 / post["user_msgs"]))
        z = (r1 - r2) / se if se else 0.0
        print(f"\ncorrection rate: {100 * r1:.2f}% pre vs {100 * r2:.2f}% post, "
              f"z = {z:.2f} (|z| < 2 is noise)")


if __name__ == "__main__":
    main()
