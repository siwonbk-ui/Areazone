"""Carry per-run bookkeeping between hourly runs without committing it.

The feed throttles (lastAttemptAt) and the once-a-day baseline gate
(baselineCheckDay) live in files that are no longer committed on every run, so a
fresh runner would otherwise see stale values and re-fetch too often. The
workflow keeps them in the Actions cache; `restore` takes whichever copy — cached
or committed — is newer, so a manual edit pushed to the repo is never lost.

    python pipeline/state_cache.py restore|save
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / '.state-cache'
STATUS = 'pipeline/update_status.json'
CURRENT = 'pipeline/current_state.json'


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def newer_status(committed, cached):
    if not cached:
        return committed
    if not committed:
        return cached
    return cached if cached.get('checkedAt', '') > committed.get('checkedAt', '') else committed


def newer_sources(committed, cached):
    """Per source, keep the copy with the later attempt; sources added in the repo survive."""
    merged = dict(committed or {})
    for key, source in (cached or {}).items():
        mine = merged.get(key)
        if mine is None or source.get('lastAttemptAt', '') > mine.get('lastAttemptAt', ''):
            merged[key] = source
    return merged


def restore():
    for rel, pick in ((STATUS, newer_status), (CURRENT, newer_sources)):
        cached = read(CACHE / Path(rel).name)
        if cached is None:
            continue
        committed = read(ROOT / rel)
        chosen = pick(committed, cached)
        if chosen != committed:
            write(ROOT / rel, chosen)
            print('restored newer', rel, 'from cache')


def save():
    CACHE.mkdir(exist_ok=True)
    for rel in (STATUS, CURRENT):
        if (ROOT / rel).exists():
            shutil.copyfile(ROOT / rel, CACHE / Path(rel).name)


if __name__ == '__main__':
    {'restore': restore, 'save': save}[sys.argv[1]]()
