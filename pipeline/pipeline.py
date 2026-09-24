# -*- coding: utf-8 -*-
"""
NAT CAT Zoning — automated data pipeline.

Discovers the latest resources from the Thai government open-data catalogs (CKAN),
downloads them, aggregates every hazard to อำเภอ level, classifies the risk levels
and writes data_updated.js. Designed to run unattended in CI.

  python pipeline.py --check      # only report whether sources changed (exit 0)
  python pipeline.py              # full rebuild of data_updated.js
"""
import argparse, csv, io, json, os, re, sys, urllib.request, time
from datetime import date as calendar_date
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE_DIR, '.cache')
STATE_FILE = os.path.join(BASE_DIR, 'sources_state.json')
OUT_FILE = os.path.join(BASE_DIR, 'data_updated.js')

DDPM = 'https://catalog.disaster.go.th'
LDD = 'https://lddcatalog.ldd.go.th'

# dataset id -> (catalog base, friendly key)
DATASETS = {
    'storm':   (DDPM, '32ab6f0a-142a-4b60-ab5e-7f2b057957b7'),  # วาตภัย
    'flood':   (DDPM, 'b3f55bb3-0f35-4fda-b41c-f337aaf4f605'),  # อุทกภัย
    'slide':   (DDPM, '016d235e-6d62-4d72-aeae-769b1ab0db31'),  # ดินโคลนถล่ม
    'lddflood': (LDD, 'ldd_21_04'),                             # น้ำท่วมซ้ำซาก
    'lddrought': (LDD, 'lpd05'),                                # แล้งซ้ำซาก
}
SOURCE_NOTES = []


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={'User-Agent': 'natcat-pipeline/1.0'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            break
        except (OSError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    return data if binary else data.decode('utf-8-sig', 'replace')


def package_show(base, pkg_id):
    d = json.loads(fetch('%s/api/3/action/package_show?id=%s' % (base, pkg_id)))
    if not d.get('success'):
        raise RuntimeError('package_show failed for %s' % pkg_id)
    return d['result']


def survey():
    """Return {dataset: {resource_id: {...}}} describing every usable resource."""
    out = {}
    for key, (base, pkg) in DATASETS.items():
        res = package_show(base, pkg)
        items = {}
        for r in res.get('resources', []):
            fmt = (r.get('format') or '').upper()
            if fmt not in ('CSV', 'XLS', 'XLSX'):
                continue
            items[r['id']] = {
                'name': (r.get('name') or '').strip(),
                'format': fmt,
                'url': r.get('url'),
                'modified': r.get('last_modified') or r.get('created'),
            }
        out[key] = {
            'metadata_modified': res.get('metadata_modified'),
            'resources': items,
        }
    return out


def load_state():
    # A staged snapshot already fetched successfully must not rebuild daily.
    candidate = os.path.join(BASE_DIR, 'sources_candidate.json')
    state_path = candidate if os.path.exists(candidate) else STATE_FILE
    if os.path.exists(state_path):
        with open(state_path, encoding='utf-8') as f:
            return json.load(f)
    return {}


def fingerprint(s):
    """Compact signature used to decide whether anything actually changed."""
    fp = {}
    for k, v in s.items():
        fp[k] = {
            'metadata_modified': v['metadata_modified'],
            'resources': {rid: {kk: r.get(kk) for kk in ('modified','url','format','name')}
                          for rid, r in sorted(v['resources'].items())},
        }
    return fp


# ---------------------------------------------------------------- downloads
def cached(url, name):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    # Always fetch on a rebuild: resource IDs/URLs can stay the same after edits.
    data = fetch(url, binary=True)
    with open(path + '.tmp', 'wb') as f:
        f.write(data)
    os.replace(path + '.tmp', path)
    return path


def year_of(name):
    m = re.search(r'(25\d\d)', name or '')
    return m.group(1) if m else None


def pick_yearly(ds, want_years=None, prefer_xlsx=False):
    """Return one resource per year, with an explicit, auditable format preference."""
    # The flood CSV for 2563 has blank/invalid incident dates, while the
    # official XLSX contains the district-level incident table and valid dates.
    # Keep CSV as the normal default for the other hazards, but prefer XLSX for
    # flood rather than silently accepting the malformed CSV.
    ranks = ({'XLSX': 3, 'XLS': 2, 'CSV': 1} if prefer_xlsx
             else {'CSV': 3, 'XLSX': 2, 'XLS': 1})
    best = {}
    for rid, r in ds['resources'].items():
        y = year_of(r['name'])
        if not y:
            continue
        if want_years and y not in want_years:
            continue
        cur = best.get(y)
        if cur is None or ranks.get(r['format'], 0) > ranks.get(cur['format'], 0):
            best[y] = dict(r, id=rid)
    return dict(sorted(best.items()))


# ---------------------------------------------------------------- parsing
def norm_prov(s):
    return re.sub(r'^(จ\.|จังหวัด)\s*', '', (s or '').strip()).strip()


def norm_dist(s, prov=''):
    s = re.sub(r'^(อ\.|อำเภอ|เขต)\s*', '', (s or '').strip()).strip()
    if s == 'เมือง' and prov:
        s = 'เมือง' + prov
    return s


def to_num(s):
    s = str(s or '').strip().replace(',', '')
    if s in ('', '-', 'ไม่มี', 'None'):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def read_rows(path, fmt):
    """Return a list of rows (lists of strings) from CSV or XLSX."""
    if fmt == 'CSV':
        with open(path, encoding='utf-8-sig', errors='replace') as f:
            return [r for r in csv.reader(f)]
    with open(path, 'rb') as handle:
        workbook_bytes = handle.read()
    # Some CKAN resources are labelled XLS but contain an XLSX ZIP workbook.
    if fmt == 'XLS' and not workbook_bytes.startswith(b'PK'):
        import xlrd
        wb = xlrd.open_workbook(path)
        ws = next((s for s in wb.sheets() if s.name != 'Column_Name'), wb.sheet_by_index(0))
        return [[xlrd.xldate_as_datetime(c.value, wb.datemode).isoformat() if c.ctype == xlrd.XL_CELL_DATE
                 else str(c.value) if c.value is not None else '' for c in ws.row(i)] for i in range(ws.nrows)]
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(workbook_bytes), read_only=True, data_only=True)
    sheets = [s for s in wb.sheetnames if s != 'Column_Name'] or wb.sheetnames

    # Some official workbooks put a province summary first and the actual
    # village/district incident table on a later sheet (e.g. flood 2563's
    # "Book1"). Select the sheet whose early rows expose district event fields.
    def sheet_score(name):
        preview = list(wb[name].iter_rows(min_row=1, max_row=4, values_only=True))
        header = ' '.join(str(c or '').lower() for row in preview for c in row)
        return (int('province' in header or 'จังหวัด' in header)
                + int('district' in header or 'อำเภอ' in header)
                + int('disaster area date' in header or 'วันที่เกิด' in header))

    ws = wb[max(sheets, key=sheet_score)]
    return [['' if c is None else str(c) for c in row]
            for row in ws.iter_rows(values_only=True)]


THAI_MONTHS = {
    'ม.ค': 1, 'มกราคม': 1, 'ก.พ': 2, 'กุมภาพันธ์': 2, 'มี.ค': 3, 'มีนาคม': 3,
    'เม.ย': 4, 'เมษายน': 4, 'พ.ค': 5, 'พฤษภาคม': 5, 'มิ.ย': 6, 'มิถุนายน': 6,
    'ก.ค': 7, 'กรกฎาคม': 7, 'ส.ค': 8, 'สิงหาคม': 8, 'ก.ย': 9, 'กันยายน': 9,
    'ต.ค': 10, 'ตุลาคม': 10, 'พ.ย': 11, 'พฤศจิกายน': 11, 'ธ.ค': 12, 'ธันวาคม': 12,
}

ALIASES = {
    'prov': ['จังหวัด', 'province'],
    'dist': ['อำเภอ', 'district'],
    'date': ['วันที่เกิดภัย', 'วันที่เกิดสถานการณ์', 'วันที่เกิดเหตุ', 'disaster date',
             'situation_date', 'incident date', 'disaster area date'],
    'cause': ['ลักษณะ/สาเหตุ', 'nature/cause', 'cause', 'flood type', 'event type'],
    'agri': ['ด้านการเกษตร', 'agriculture'],
    'times': ['จำนวนครั้งการเกิด'],
    'n_amphoe': ['จำนวน อำเภอ'],
    'n_tambon': ['จำนวน ตำบล'],
}


def parse_date(s, file_year):
    """Normalise the many date spellings used across years -> 'YYYY-MM-DD'.

    Anything unparseable collapses to the file's year, so a district can never be
    credited with more event-days than it really had.
    """
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    if not s:
        return '%s-??' % file_year
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)           # 2024-05-07 00:00:00
    if m:
        return '%s-%s-%s' % m.groups()
    m = re.match(r'(\d{1,2})[ /-]([^\d /-]+)[ /-](\d{2,4})', s)  # 6 พฤศจิกายน 2564 | 10-ต.ค.-63
    if m:
        day, mon, yr = m.group(1), m.group(2).strip(' .'), m.group(3)
        mon_n = None
        for name, n in THAI_MONTHS.items():
            if mon.startswith(name):
                mon_n = n
                break
        if mon_n:
            y = int(yr)
            if y < 100:
                y += 2500
            if y > 2400:
                y -= 543
            return '%04d-%02d-%02d' % (y, mon_n, int(day))
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', s)        # 10/10/2563
    if m:
        y = int(m.group(3))
        if y > 2400:
            y -= 543
        return '%04d-%02d-%02d' % (y, int(m.group(2)), int(m.group(1)))
    return '%s-??' % file_year


def is_summary(rows):
    """True for the province-level yearly summary sheets (no per-district rows)."""
    for row in rows[:4]:
        for c in row:
            if 'จำนวนครั้งการเกิด' in str(c or ''):
                return True
    return False


def header_map(rows, summary=False):
    """Locate the header row (EN and/or TH) and map logical fields to columns."""
    best, best_start, best_score = {}, 1, -1
    for idx in range(min(4, len(rows))):
        cells = [re.sub(r'\s+', ' ', str(c or '')).strip() for c in rows[idx]]
        low = [c.lower() for c in cells]
        m = {}
        for field, names in ALIASES.items():
            candidates = []
            for i, (c, lc) in enumerate(zip(cells, low)):
                if not c:
                    continue
                if field in ('prov', 'dist') and ('code' in lc or 'รหัส' in c):
                    continue
                exact = any(lc == n.lower() for n in names)
                prefix = any(lc.startswith(n.lower()) for n in names)
                if exact or prefix:
                    candidates.append((2 if exact else 1, -i, i))
            if candidates:
                m[field] = max(candidates)[2]
        score = len(m)
        if summary:
            if 'times' in m:
                score += 5          # the TH row is the only one naming the count column
        elif 'prov' in m and 'dist' in m:
            score += 5
        if score > best_score:
            best, best_start, best_score = m, idx + 1, score
    # a second header row (TH under EN) must be skipped too
    if best_start < len(rows):
        nxt = [str(c or '').strip() for c in rows[best_start]]
        if any(c in ('จังหวัด', 'ประเภทภัย', 'ชื่อสถานการณ์') for c in nxt):
            best_start += 1
    return best, best_start


def parse_incidents(files, summary_out=None):
    """Aggregate DDPM incident files -> {(prov,dist): set(dates)}, agri damage, causes."""
    days = defaultdict(set)
    agri = defaultdict(float)
    summer = defaultdict(set)     # hail proxy: summer storms Feb–May
    years_used = []
    for year, r in files.items():
        path = cached(r['url'], '%s_%s.%s' % (r['id'][:8], year, r['format'].lower()))
        rows = read_rows(path, r['format'])
        summary = is_summary(rows)
        m, start = header_map(rows, summary)
        if 'prov' not in m:
            raise ValueError('Province header missing: ' + r['name'])
        # Some annual province totals call their district-count column "District".
        district_cells = [str(row[m['dist']]).strip() for row in rows[start:]
                          if 'dist' in m and len(row) > m['dist'] and str(row[m['dist']]).strip()]
        count_only = bool(district_cells) and all(re.fullmatch(r'\d+(?:\.0+)?', v) for v in district_cells)
        if summary or 'dist' not in m or count_only:
            SOURCE_NOTES.append({'year': year, 'resource': r['id'], 'url': r['url'],
                                 'reason': 'province_summary_not_used_for_district_event_days'})
            # province-level summary (e.g. the newest year) -> keep separately
            if summary_out is not None and 'times' in m:
                for row in rows[start:]:
                    if len(row) <= m['prov']:
                        continue
                    p = norm_prov(row[m['prov']])
                    if not re.search(r'[ก-๙]', p):
                        continue
                    summary_out[p] = {
                        'year': year,
                        'times': int(to_num(row[m['times']])),
                        'amphoe': int(to_num(row[m['n_amphoe']])) if 'n_amphoe' in m else 0,
                        'tambon': int(to_num(row[m['n_tambon']])) if 'n_tambon' in m else 0,
                    }
            continue
        if 'date' not in m:
            raise ValueError('Incident date header missing: ' + r['name'])
        invalid_dates, year_has_data = 0, False
        for row in rows[start:]:
            if len(row) <= max(m['prov'], m['dist']):
                continue
            p = norm_prov(row[m['prov']])
            if not p or not re.search(r'[ก-๙]', p):
                continue
            d = norm_dist(row[m['dist']], p)
            if not d:
                continue
            key = (p, d)
            if ' รวม' in p or ' รวม' in d or d.isdigit():
                continue                      # province/region subtotal rows
            raw = row[m['date']] if 'date' in m and len(row) > m['date'] else ''
            date = parse_date(raw, year)
            try:
                calendar_date.fromisoformat(date)
            except ValueError:
                invalid_dates += 1
                continue
            days[key].add(date)
            year_has_data = True
            if 'agri' in m and len(row) > m['agri']:
                agri[key] += to_num(row[m['agri']])
            cause = str(row[m['cause']] if 'cause' in m and len(row) > m['cause'] else '')
            mo = re.match(r'\d{4}-(\d{2})', date)
            if 'ฤดูร้อน' in cause and mo and int(mo.group(1)) in (2, 3, 4, 5):
                summer[key].add(date)
        if year_has_data:
            years_used.append(year)
        if invalid_dates:
            SOURCE_NOTES.append({'year':year, 'resource':r['id'], 'url':r['url'],
                                 'reason':'invalid_incident_dates', 'rows':invalid_dates})
    return days, agri, summer, sorted(years_used)


def parse_ldd(res):
    """LDD recurrence CSV (province/district forward-filled) -> {(prov,dist): [hi, mid, low]}."""
    path = cached(res['url'], 'ldd_%s.csv' % res['id'][:8])
    with open(path, encoding='utf-8-sig', errors='replace') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError('Empty LDD CSV')
    cols = list(rows[0].keys())
    if len(cols) < 8 or 'จังหวัด' not in cols[0] or 'อำเภอ' not in cols[1]:
        raise ValueError('Unexpected LDD schema; review column mapping')
    c_prov, c_dist = cols[0], cols[1]
    c_hi, c_md, c_lo = cols[3], cols[4], cols[5]
    out = defaultdict(lambda: [0.0, 0.0, 0.0])
    years = {}
    cp = cd = ''
    for r in rows:
        if (r[c_prov] or '').strip():
            cp = norm_prov(r[c_prov])
        if (r[c_dist] or '').strip():
            cd = norm_dist(r[c_dist], cp)
        if not (cp and cd):
            continue
        k = (cp, cd)
        out[k][0] += to_num(r[c_hi])
        out[k][1] += to_num(r[c_md])
        out[k][2] += to_num(r[c_lo])
        y = re.search(r'25\d\d', str(r.get(cols[7]) or ''))
        if y:
            years[cp] = max(years.get(cp, ''), y.group())
    return out, years


# ---------------------------------------------------------------- classify
EQ3 = ['กาญจนบุรี','เชียงราย','เชียงใหม่','ตาก','น่าน','พะเยา','แพร่','แม่ฮ่องสอน','ลำปาง','ลำพูน','สุโขทัย','อุตรดิตถ์']
EQ2 = ['กรุงเทพมหานคร','กำแพงเพชร','ชัยนาท','นนทบุรี','นครปฐม','นครสวรรค์','ปทุมธานี','พังงา','พระนครศรีอยุธยา','ภูเก็ต','ระนอง','ราชบุรี','สมุทรปราการ','สมุทรสงคราม','สมุทรสาคร','สุพรรณบุรี','อุทัยธานี']
EQ1 = ['ตรัง','ชุมพร','นครพนม','นครศรีธรรมราช','บึงกาฬ','ประจวบคีรีขันธ์','พิษณุโลก','เพชรบุรี','เลย','สตูล','สงขลา','สุราษฎร์ธานี','หนองคาย','กระบี่']

RANK = {'red': 4, 'orange': 3, 'yellow': 2, 'green': 1, None: 0}


def worst(*levels):
    best = None
    for lv in levels:
        if RANK.get(lv, 0) > RANK.get(best, 0):
            best = lv
    return best


def cls_flood_area(a):
    if not a:
        return None
    hi, md, lo = a
    if hi >= 1000: return 'red'
    if hi > 0 or md >= 5000: return 'orange'
    if md > 0 or lo >= 10000: return 'yellow'
    if lo > 0: return 'green'
    return None


def cls_drought_area(a, cuts):
    if not a:
        return None
    hi, md, lo = a
    if hi >= cuts[0]: return 'red'
    if hi >= cuts[1]: return 'orange'
    if hi >= cuts[2]: return 'yellow'
    if hi + md + lo > 0: return 'green'
    return None


def cls_count(n, cuts):
    r, o, y = cuts
    if not n:
        return None
    if n >= r: return 'red'
    if n >= o: return 'orange'
    if n >= y: return 'yellow'
    return 'green'


def percentile_cuts(values, ps=(95, 85, 65)):
    """Cut-offs from the distribution of districts that recorded at least one event."""
    vals = sorted(v for v in values if v > 0)
    if not vals:
        return (1, 1, 1)
    def at(p):
        return vals[min(len(vals) - 1, int(len(vals) * p / 100))]
    cuts = [max(1, at(p)) for p in ps]
    # keep the bands strictly decreasing so every level stays reachable
    for i in range(1, 3):
        if cuts[i] >= cuts[i - 1]:
            cuts[i] = max(1, cuts[i - 1] - 1)
    return tuple(cuts)


def area_cuts(areas, ps=(90, 70, 50)):
    vals = sorted(a[0] for a in areas if a and a[0] > 0)
    if not vals:
        return (1, 1, 1)
    def at(p):
        return vals[min(len(vals) - 1, int(len(vals) * p / 100))]
    cuts = [at(p) for p in ps]
    for i in range(1, 3):
        if cuts[i] >= cuts[i - 1]:
            cuts[i] = max(1, cuts[i - 1] - 1)
    return tuple(cuts)


def build(state):
    SOURCE_NOTES.clear()
    storm_files = pick_yearly(state['storm'])
    flood_files = pick_yearly(state['flood'])
    # Only 2563 has been audited as a replacement. Other years' XLSX sheets
    # have different schemas; do not replace valid CSVs indiscriminately.
    flood_files.update(pick_yearly(state['flood'], want_years={'2563'}, prefer_xlsx=True))
    slide_files = pick_yearly(state['slide'])

    flood2568 = {}
    storm_days, storm_agri, hail_days, storm_years = parse_incidents(storm_files)
    blocked = ['wind','hail'] if any(n['reason']=='invalid_incident_dates' for n in SOURCE_NOTES) else []
    note_count = len(SOURCE_NOTES)
    flood_days, flood_agri, _, flood_years = parse_incidents(flood_files, summary_out=flood2568)
    if any(n['reason']=='invalid_incident_dates' for n in SOURCE_NOTES[note_count:]):
        blocked.append('flood')
    note_count = len(SOURCE_NOTES)
    slide_days, _, _, slide_years = parse_incidents(slide_files)
    if any(n['reason']=='invalid_incident_dates' for n in SOURCE_NOTES[note_count:]):
        blocked.append('slide')

    ldd_flood_res = [r for r in state['lddflood']['resources'].values() if r['format'] == 'CSV'][0]
    ldd_drought_res = [r for r in state['lddrought']['resources'].values()
                       if r['format'] == 'CSV' and 'สถิติ' in r['name']][0]
    ldd_flood_res['id'] = [k for k, v in state['lddflood']['resources'].items() if v is ldd_flood_res or v['url'] == ldd_flood_res['url']][0]
    ldd_drought_res['id'] = [k for k, v in state['lddrought']['resources'].items() if v['url'] == ldd_drought_res['url']][0]
    flood_area, flood_area_years = parse_ldd(ldd_flood_res)
    drought_area, drought_years = parse_ldd(ldd_drought_res)

    print('storm years %s | flood years %s | slide years %s' % (storm_years, flood_years, slide_years))
    print('districts — storm %d, flood-event %d, slide %d, flood-area %d, drought-area %d'
          % (len(storm_days), len(flood_days), len(slide_days), len(flood_area), len(drought_area)))

    # self-calibrating cut-offs from this build's data
    cuts = {
        'wind':  percentile_cuts([len(v) for v in storm_days.values()]),
        'hail':  percentile_cuts([len(v) for v in hail_days.values()]),
        'flood': percentile_cuts([len(v) for v in flood_days.values()]),
        'slide': percentile_cuts([len(v) for v in slide_days.values()]),
        'drought': area_cuts(list(drought_area.values())),
    }
    print('cut-offs (red/orange/yellow):', {k: v for k, v in cuts.items()})

    eq_zone = {}
    for p in EQ3: eq_zone[p] = ('red', 'บริเวณที่ 3 — เสี่ยงสูง')
    for p in EQ2: eq_zone[p] = ('orange', 'บริเวณที่ 2 — เสี่ยงปานกลาง (รวม กทม./ปริมณฑล ชั้นดินอ่อนขยายแรงสั่นสะเทือน)')
    for p in EQ1: eq_zone[p] = ('yellow', 'บริเวณที่ 1 — เฝ้าระวัง')

    with open(os.path.join(BASE_DIR, 'districts.json'), encoding='utf-8') as f:
        base = json.load(f)

    records = []
    for r in base['records']:
        p = r['province'].strip()
        dk = norm_dist(r['district'], p)
        k = (p, dk)
        fa = flood_area.get(k)
        da = drought_area.get(k)
        wn, hn, sn = len(storm_days.get(k, ())), len(hail_days.get(k, ())), len(slide_days.get(k, ()))
        fd = len(flood_days.get(k, ()))
        eq = eq_zone.get(p, (None, None))

        hail = cls_count(hn, cuts['hail'])
        wind = cls_count(wn, cuts['wind'])
        flood = worst(cls_flood_area(fa), cls_count(fd, cuts['flood']))

        # fall back to the original NAT CAT.xlsx value for a hazard/district the
        # public sources don't cover at all (drought/slide have no counterpart
        # in that file, so they're never filled this way).
        eq_val, eq_from_xlsx = eq[0], False
        if eq_val is None and r.get('xlsx_eq'):
            eq_val, eq_from_xlsx = r['xlsx_eq'], True
        flood_from_xlsx = False
        if flood is None and r.get('xlsx_flood'):
            flood, flood_from_xlsx = r['xlsx_flood'], True
        wind_from_xlsx = False
        if wind is None and r.get('xlsx_wind'):
            wind, wind_from_xlsx = r['xlsx_wind'], True
        hail_from_xlsx = False
        if hail is None and r.get('xlsx_hail'):
            hail, hail_from_xlsx = r['xlsx_hail'], True

        records.append({
            'province': p, 'district': r['district'], 'postal': r['postal'],
            'postal_office': r['postal_office'], 'note': r['note'],
            'eq': eq_val, 'hail': hail, 'wind': wind, 'flood': flood,
            'drought': cls_drought_area(da, cuts['drought']),
            'slide': cls_count(sn, cuts['slide']),
            'ev': {
                'eqZone': eq[1] if not eq_from_xlsx else None,
                'floodArea': [round(x) for x in fa] if fa else None,
                'droughtArea': [round(x) for x in da] if da else None,
                'windDays': wn, 'hailDays': hn, 'slideDays': sn, 'floodDays': fd,
                'floodAgri': round(flood_agri.get(k, 0)),
                'windAgri': round(storm_agri.get(k, 0)),
                'fromXlsx': [kk for kk, f in (('eq', eq_from_xlsx), ('flood', flood_from_xlsx),
                                               ('wind', wind_from_xlsx), ('hail', hail_from_xlsx)) if f],
            },
        })
        for h in blocked:
            records[-1][h] = None

    with open(os.path.join(BASE_DIR, 'hazard_meta.json'), encoding='utf-8') as f:
        meta = json.load(f)
    # keep the "data as of" lines honest — they follow whatever was actually downloaded
    year_sets = {'wind': storm_years, 'hail': storm_years, 'slide': slide_years, 'flood': flood_years}
    def days_txt(c):
        return 'แดง ≥%d วัน (เปอร์เซ็นไทล์ 95) · ส้ม ≥%d (p85) · เหลือง ≥%d (p65) · เขียว ≥1' % c
    cut_txt = {
        'wind': days_txt(cuts['wind']),
        'hail': days_txt(cuts['hail']),
        'slide': days_txt(cuts['slide']),
        'flood': 'เหตุการณ์จริง: ' + days_txt(cuts['flood']),
        'drought': 'แดง ≥%s ไร่ (p90) · ส้ม ≥%s ไร่ (p70) · เหลือง ≥%s ไร่ (p50)'
                   % tuple(format(int(x), ',') for x in cuts['drought']),
    }
    xlsx_fallback_note = 'อำเภอที่ไม่มีข้อมูลจากแหล่งเปิดภาครัฐเลย ใช้ค่าจากไฟล์ NAT CAT.xlsx ภายในแทนถ้ามี'
    for m in meta:
        years = year_sets.get(m['key'], [])
        span = '%s–%s' % (years[0], years[-1]) if years else 'n/a'
        m['asof'] = m['asof'].replace('{{YEARS}}', span)
        if m['key'] in cut_txt:
            m['method'] = re.sub(r'· แดง.*$', '', m['method']).strip() + ' · ' + cut_txt[m['key']]
        if m['key'] in ('flood', 'wind', 'hail'):  # 'eq' already mentions this in its own text
            m['method'] = m['method'].rstrip('.') + ' · ' + xlsx_fallback_note
        m['autoCalibrated'] = m['key'] in cut_txt

    out = {
        'regionMap': base['regionMap'],
        'regionNames': base['regionNames'],
        'legend': base['legend'],
        'hazardMeta': meta,
        'flood2568': flood2568,
        'buildInfo': {
            'builtAt': __import__('datetime').datetime.now().astimezone().isoformat(timespec='seconds'),
            'incidentYears': {'storm': storm_years, 'flood': flood_years, 'slide': slide_years},
            'lddFloodYears': flood_area_years, 'lddDroughtYears': drought_years,
            'cutoffs': {k: list(v) for k, v in cuts.items()},
            'sourceNotes': list(SOURCE_NOTES),
            'blockedHazards': blocked,
        },
        'records': records,
    }
    print('built candidate |', len(records), 'records')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='only report whether sources changed')
    ap.add_argument('--force', action='store_true', help='rebuild even if sources are unchanged')
    args = ap.parse_args()

    current = survey()
    old = load_state()
    changed = fingerprint(current) != fingerprint(old) if old else True

    if args.check:
        print(json.dumps({'changed': changed,
                          'datasets': {k: v['metadata_modified'] for k, v in current.items()}},
                         ensure_ascii=False))
        return 0

    if not changed and not args.force:
        print('sources unchanged — nothing to rebuild')
        return 0

    from review import stage
    stage(build(current), current)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
