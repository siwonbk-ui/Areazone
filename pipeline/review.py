"""Stage baseline updates for an explicit, hash-bound review; never erase old values."""
import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HAZARDS = ('eq', 'hail', 'wind', 'flood', 'drought', 'slide')
COLORS = {None, 'red', 'orange', 'yellow', 'green'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def read_js(path):
    text = Path(path).read_text(encoding='utf-8-sig').strip()
    return json.loads(text.removeprefix('window.NATCAT_DATA = ').rstrip(';'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    temp.replace(path)


def write_js(path, value, global_name='NATCAT_DATA'):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text('window.' + global_name + ' = ' + json.dumps(value, ensure_ascii=False) + ';\n', encoding='utf-8', newline='\n')
    temp.replace(path)


def key(row):
    return tuple(str(row.get(k) or '') for k in ('province', 'district', 'postal', 'postal_office', 'note'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate(data, base):
    rows = data.get('records', [])
    if not rows or sorted(map(key, rows)) != sorted(map(key, base['records'])):
        raise ValueError('พื้นที่/รายการไม่ตรง NAT CAT เดิม: ต้องตรวจสอบก่อนเผยแพร่')
    if len(set(map(key, rows))) != len(rows):
        raise ValueError('พบรายการพื้นที่ซ้ำ')
    for row in rows:
        for h in HAZARDS:
            if h not in row or row[h] not in COLORS:
                raise ValueError('Missing/invalid hazard: ' + h)


def retain_missing(candidate, previous, base):
    """A missing source cannot replace an established value with null or an XLSX fallback."""
    result = copy.deepcopy(candidate)
    # A quarantined layer must retain its old explanatory thresholds/years too.
    blocked = candidate.get('buildInfo', {}).get('blockedHazards', [])
    for h in blocked:
        old_meta = next((m for m in previous.get('hazardMeta', []) if m['key'] == h), None)
        if old_meta:
            result['hazardMeta'] = [copy.deepcopy(old_meta) if m['key'] == h else m for m in result.get('hazardMeta', [])]
        old_info = previous.get('buildInfo', {})
        if h in old_info.get('cutoffs', {}):
            result['buildInfo']['cutoffs'][h] = copy.deepcopy(old_info['cutoffs'][h])
        source_key = {'wind':'storm','hail':'storm','flood':'flood','slide':'slide'}.get(h)
        if source_key in old_info.get('incidentYears', {}):
            result['buildInfo'].setdefault('incidentYears', {})[source_key] = old_info['incidentYears'][source_key]
        if h == 'flood':
            result['buildInfo']['lddFloodYears'] = old_info.get('lddFloodYears', {})
    old = {key(r): r for r in previous['records']}
    original = {key(r): r for r in base['records']}
    changes = []
    for row in result['records']:
        prev = old[key(row)]
        origin = original[key(row)]
        provenance = {}
        for h in HAZARDS:
            missing = h in blocked or row[h] is None or h in row.get('ev', {}).get('fromXlsx', [])
            if missing:
                row[h] = prev.get(h) or origin.get('xlsx_' + h)
                prior = prev.get('provenance', {}).get(h, {'status': 'legacy'})
                provenance[h] = {
                    'status': 'retained' if row[h] else 'missing',
                    'reason': 'ยังไม่มีข้อมูลใหม่ที่ผ่านการตรวจ ใช้ค่าที่มีอยู่เดิม',
                    'originStatus': prior.get('originStatus', prior['status']),
                    'dataBuiltAt': prior.get('dataBuiltAt', previous.get('buildInfo', {}).get('builtAt')),
                }
                fields = {'eq':['eqZone'], 'hail':['hailDays'], 'wind':['windDays','windAgri'],
                          'flood':['floodDays','floodAgri','floodArea'], 'drought':['droughtArea'], 'slide':['slideDays']}[h]
                for field in fields:
                    row.setdefault('ev', {})[field] = prev.get('ev', {}).get(field)
                fallback_flags = row.setdefault('ev', {}).setdefault('fromXlsx', [])
                if h in fallback_flags:
                    fallback_flags.remove(h)
                if h in prev.get('ev', {}).get('fromXlsx', []) or (not prev.get(h) and origin.get('xlsx_' + h)):
                    fallback_flags.append(h)
            else:
                provenance[h] = {'status': 'pending_review', 'dataBuiltAt': candidate['buildInfo']['builtAt']}
            if row[h] != prev.get(h):
                changes.append({'area': dict(zip(('province','district','postal','postal_office','note'), key(row))),
                                'hazard': h, 'before': prev.get(h), 'after': row[h]})
        row['provenance'] = provenance
    return result, changes


def stage(candidate, source_state):
    base = json.loads((ROOT / 'pipeline/districts.json').read_text(encoding='utf-8'))
    previous = read_js(ROOT / 'data_updated.js')
    validate(candidate, base)
    validate(previous, base)
    merged, changes = retain_missing(candidate, previous, base)
    payload = {'data': merged, 'sourceState': source_state, 'previousHash': digest(previous)}
    review_hash = digest(payload)
    write_json(ROOT / 'pipeline/pending_update.json', payload)
    write_json(ROOT / 'pipeline/review_report.json', {
        'status': 'pending_review', 'reviewHash': review_hash, 'createdAt': now(),
        'changeCount': len(changes), 'changes': changes,
        'blockedHazards': candidate.get('buildInfo', {}).get('blockedHazards', []),
        'sourceNotes': candidate.get('buildInfo', {}).get('sourceNotes', []),
        'checks': ['record identities match baseline', 'colors valid', 'missing values retained'],
        'requires': 'ตรวจปีข้อมูล แหล่งอ้างอิง เกณฑ์สี และพื้นที่ที่เปลี่ยน ก่อนยืนยัน reviewHash',
    })
    write_json(ROOT / 'pipeline/sources_candidate.json', source_state)
    print('Pending review:', review_hash, '| changes:', len(changes))


def approve(review_hash, reviewer):
    payload = json.loads((ROOT / 'pipeline/pending_update.json').read_text(encoding='utf-8'))
    if not reviewer.strip() or digest(payload) != review_hash:
        raise ValueError('ผู้ตรวจหรือ hash ไม่ตรงรายการที่รอตรวจ')
    if digest(read_js(ROOT / 'data_updated.js')) != payload['previousHash']:
        raise ValueError('ค่าที่เผยแพร่เปลี่ยนแล้ว ต้องสร้าง candidate และตรวจใหม่')
    base = json.loads((ROOT / 'pipeline/districts.json').read_text(encoding='utf-8'))
    data = payload['data']
    validate(data, base)
    stamp = now()
    for row in data['records']:
        for source in row.get('provenance', {}).values():
            if source['status'] == 'pending_review':
                source.update(status='reviewed', reviewer=reviewer, reviewedAt=stamp)
    data['buildInfo']['review'] = {'hash': review_hash, 'reviewer': reviewer, 'reviewedAt': stamp}
    write_js(ROOT / 'data_updated.js', data)
    write_json(ROOT / 'pipeline/sources_state.json', payload['sourceState'])
    report = json.loads((ROOT / 'pipeline/review_report.json').read_text(encoding='utf-8'))
    report.update(status='approved', reviewer=reviewer, reviewedAt=stamp)
    write_json(ROOT / 'pipeline/review_report.json', report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--approve', required=True)
    parser.add_argument('--reviewer', required=True)
    args = parser.parse_args()
    approve(args.approve, args.reviewer)
