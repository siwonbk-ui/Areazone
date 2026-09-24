"""Official bulletins: validate source/date, retain last success; never infer risk colors."""
import argparse
import hashlib
import json
import re
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

from review import ROOT, write_js, write_json

SOURCES = [
    {'id': 'dmr', 'name': 'กรมทรัพยากรธรณี — รายงานประจำวัน',
     'url': 'https://www.dmr.go.th/category/geohazard_daily_report/feed/',
     'home': 'https://www.dmr.go.th/geohazard/', 'hosts': ['www.dmr.go.th','dmr.go.th'],
     'hazards': ['slide', 'flood', 'eq'], 'intervalHours': 1, 'staleHours': 48},
    {'id': 'tmd-earthquake', 'name': 'กรมอุตุนิยมวิทยา — รายงานแผ่นดินไหว',
     'url': 'https://earthquake.tmd.go.th/feed/rss_inside.xml',
     'home': 'https://earthquake.tmd.go.th/', 'hosts': ['earthquake.tmd.go.th'],
     'hazards': ['eq'], 'intervalHours': 2, 'staleHours': 48},
]
DIRECTORY = [
    {'name': 'GISTDA — น้ำท่วม / ภัยแล้ง', 'url': 'https://disaster.gistda.or.th/',
     'note': 'ลิงก์ประกอบ: ยังไม่เชื่อมข้อมูลเชิงพื้นที่อัตโนมัติ'},
    {'name': 'กรมอุตุนิยมวิทยา — ประกาศเตือนพายุ', 'url': 'https://www.tmd.go.th/warning-and-events/warning-storm',
     'note': 'ลิงก์ประกอบ: ยังไม่เชื่อมประกาศพายุ/ลูกเห็บอัตโนมัติ'},
    {'name': 'ThaiWater — ฝนและระดับน้ำ', 'url': 'https://www.thaiwater.net/',
     'note': 'ลิงก์ประกอบ: ต้องยืนยันช่องทางข้อมูลและสิทธิ์การเข้าถึงก่อนเชื่อม'},
    {'name': 'ปภ. — รายงานสาธารณภัย', 'url': 'https://ddc.disaster.go.th/',
     'note': 'ลิงก์ประกอบ: ยังไม่เชื่อมรายงานเหตุอัตโนมัติ'},
]


def fetch(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'Areazone/2.0 official-feed-reader'})
            with urllib.request.urlopen(request, timeout=20) as response:
                if urlparse(response.url).hostname != urlparse(url).hostname:
                    raise ValueError('Unexpected source redirect')
                content = response.read(4_000_001)
                if len(content) > 4_000_000:
                    raise ValueError('Feed too large')
                return content
        except (OSError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def parse_feed(content, source, now, provinces):
    if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
        raise ValueError('Unsupported XML declarations')
    root = ET.fromstring(content)
    items = root.findall('./channel/item')
    if not items:
        raise ValueError('Feed has no items; retain previous success')
    events, newest = {}, None
    for item in items:
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        pub = parsedate_to_datetime(item.findtext('pubDate') or '')
        if not title or not pub.tzinfo or pub > now + timedelta(minutes=10):
            raise ValueError('Missing title/time or future timestamp')
        pub = pub.astimezone(timezone.utc)
        if urlparse(link).scheme != 'https' or urlparse(link).hostname not in source['hosts']:
            raise ValueError('Item URL is not an approved official source')
        newest = max(newest, pub) if newest else pub
        if pub < now - timedelta(days=7):
            continue
        # A location mentioned in a title is not an impact area. DMR PDFs are not geocoded.
        mentioned = [p for p in provinces if re.search(r'(?:จ\.|จังหวัด)\s*' + re.escape(p) + r'(?:\s|\(|$)', title)]
        hazards = ['eq'] if source['id'] == 'tmd-earthquake' else [
            h for h, word in [('slide','ดินถล่ม'), ('flood','น้ำป่า'), ('eq','แผ่นดินไหว')] if word in title]
        identity = hashlib.sha256((source['id'] + '|' + link).encode()).hexdigest()[:24]
        events[identity] = {'id': identity, 'sourceId': source['id'], 'title': title,
            'url': link, 'publishedAt': pub.isoformat(), 'hazards': hazards or source['hazards'],
            'mentionedProvinces': mentioned, 'areaStatus': 'not_verified',
            'verification': 'official_feed', 'impactConfirmed': False,
            'note': 'ตรวจแหล่งเผยแพร่และวันเวลาแล้ว; ยังไม่ยืนยันขอบเขตผลกระทบหรือความเสียหายรายพื้นที่'}
    return list(events.values()), newest.isoformat()


def collect(source, previous, now, provinces, force=False):
    stamp = now.isoformat(timespec='seconds')
    old = previous or {}
    last = old.get('lastAttemptAt')
    if last and not force and now - datetime.fromisoformat(last) < timedelta(hours=source['intervalHours']):
        return old
    status = {k: v for k, v in source.items() if k != 'hosts'}
    status.update(lastAttemptAt=stamp, lastSuccessAt=old.get('lastSuccessAt'), events=old.get('events', []))
    try:
        events, newest = parse_feed(fetch(source['url']), source, now, provinces)
        status.update(status='ok', events=events, lastSuccessAt=stamp, newestPublishedAt=newest)
        if now - datetime.fromisoformat(newest) > timedelta(hours=source['staleHours']):
            status['status'] = 'stale'
    except Exception as exc:
        status.update(status='unavailable', error=type(exc).__name__, newestPublishedAt=old.get('newestPublishedAt'))
        if isinstance(exc, urllib.error.HTTPError):
            status['httpStatus'] = exc.code
            status['errorDetail'] = 'HTTP ' + str(exc.code)
        else:
            status['errorDetail'] = str(exc)[:300]
    return status


def main(force=False):
    path = ROOT / 'pipeline/current_state.json'
    old = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    base = json.loads((ROOT / 'pipeline/districts.json').read_text(encoding='utf-8'))
    provinces = set(r['province'] for r in base['records'])
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda s: collect(s, old.get(s['id']), now, provinces, force), SOURCES))
    write_json(path, {s['id']: s for s in results})
    events = [e for s in results for e in s['events'] if datetime.fromisoformat(e['publishedAt']) >= now - timedelta(days=7)]
    write_js(ROOT / 'current_data.js', {
        'schemaVersion': 1, 'checkedAt': now.isoformat(timespec='seconds'),
        'sources': [{k:v for k,v in s.items() if k != 'events'} for s in results],
        'events': sorted(events, key=lambda e: e['publishedAt'], reverse=True), 'directory': DIRECTORY,
    }, 'NATCAT_CURRENT')
    print(json.dumps({s['id']: {'status': s['status'], 'items': len(s['events'])} for s in results}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true')
    main(parser.parse_args().force)
