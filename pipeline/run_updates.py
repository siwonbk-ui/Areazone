"""Hourly orchestrator; baseline at most once per Bangkok date unless explicitly forced."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from review import ROOT, approve, now, write_json
import current
import pipeline


def run():
    status_path = ROOT / 'pipeline/update_status.json'
    status = json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else {}
    errors = []
    review_hash = os.environ.get('APPROVE_HASH', '').strip()
    force = os.environ.get('FORCE_CHECK', '').lower() == 'true'
    if review_hash:
        approve(review_hash, os.environ.get('REVIEWER', ''))
        status['lastApprovedAt'] = now()
    today = datetime.now(timezone(timedelta(hours=7))).date().isoformat()
    if not review_hash and (force or status.get('baselineCheckDay') != today):
        status.update(baselineCheckDay=today, baselineCheckedAt=now())
        try:
            args = sys.argv
            try:
                sys.argv = ['pipeline.py'] + (['--force'] if force else [])
                pipeline.main()
            finally:
                sys.argv = args
            status.update(baselineStatus='ok', baselineError=None)
        except Exception as exc:
            status.update(baselineStatus='unavailable', baselineError=str(exc)[:500])
            errors.append('baseline')
    try:
        current.main(force=force)
        states = json.loads((ROOT / 'pipeline/current_state.json').read_text(encoding='utf-8'))
        status['currentSources'] = {k: v['status'] for k, v in states.items()}
        errors += [k for k, v in states.items() if v['status'] in ('unavailable', 'stale')]
        status.pop('currentError', None)
    except Exception as exc:
        status['currentError'] = str(exc)[:500]
        errors.append('current')
    status.update(checkedAt=now(), status='degraded' if errors or status.get('baselineStatus') == 'unavailable' else 'ok')
    report = ROOT / 'pipeline/review_report.json'
    if report.exists():
        r = json.loads(report.read_text(encoding='utf-8'))
        status['review'] = {k: r[k] for k in ('status','reviewHash','changeCount')}
        status['review']['blockedHazards'] = r.get('blockedHazards', [])
    write_json(status_path, status)
    print(json.dumps(status, ensure_ascii=False))
    # A temporary upstream outage is an expected operating condition: preserve
    # the last verified data, publish `degraded` for n8n to notify the owner,
    # and let the scheduled GitHub job complete successfully. A red Action
    # must be reserved for a workflow/runtime failure, not for a source that
    # is temporarily unavailable.
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
