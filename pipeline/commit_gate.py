"""Decide whether an hourly run produced anything worth committing.

Timestamps change on every run; committing them alone floods the history and
redeploys Pages every hour. Commit only when content changed, or as a heartbeat
often enough that the page's and n8n's 4-hour "checking is late" warnings stay
truthful.

    python pipeline/commit_gate.py FILE...   exit 0 = commit, exit 1 = skip
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOLATILE = {'checkedAt', 'lastAttemptAt', 'lastSuccessAt', 'baselineCheckedAt',
            'baselineCheckDay', 'createdAt', 'builtAt'}
# hourly cron + GitHub's schedule delay must stay under the 4h lateness threshold
HEARTBEAT = timedelta(hours=2.5)
STATUS_FILE = 'pipeline/update_status.json'


def load(text):
    text = text.strip().lstrip('﻿')
    if text.startswith('window.'):
        text = text.split('=', 1)[1].strip().rstrip(';')
    return json.loads(text)


def strip(value):
    if isinstance(value, dict):
        return {k: strip(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [strip(v) for v in value]
    return value


def differs(old, new):
    """True when two versions of a file differ in anything but volatile timestamps."""
    if old is None or new is None:
        return old != new
    try:
        return strip(load(old)) != strip(load(new))
    except ValueError:
        return old != new


def decide(pairs, head_status, now):
    """pairs: {path: (committed_text, working_text)}. Returns (commit?, reason)."""
    if not pairs:
        return False, 'no files changed'
    content = sorted(path for path, (old, new) in pairs.items() if differs(old, new))
    if content:
        return True, 'content changed: ' + ', '.join(content)
    try:
        checked = datetime.fromisoformat(load(head_status)['checkedAt'])
    except (TypeError, ValueError, KeyError):
        return True, 'heartbeat: no committed check time'
    age = now - checked
    if age >= HEARTBEAT:
        return True, 'heartbeat: last committed check %.1fh ago' % (age.total_seconds() / 3600)
    return False, 'timestamps only (last committed check %.1fh ago)' % (age.total_seconds() / 3600)


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, encoding='utf-8')


def committed(path):
    result = git('show', 'HEAD:' + path)
    return result.stdout if result.returncode == 0 else None


def main(paths):
    listed = git('status', '--porcelain', '--untracked-files=all', '--', *paths).stdout.splitlines()
    pairs = {}
    for line in listed:
        path = line[3:].strip().strip('"')
        working = ROOT / path
        pairs[path] = (committed(path), working.read_text(encoding='utf-8') if working.exists() else None)
    commit, reason = decide(pairs, committed(STATUS_FILE), datetime.now(timezone.utc))
    print(('commit — ' if commit else 'skip — ') + reason)
    return 0 if commit else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
