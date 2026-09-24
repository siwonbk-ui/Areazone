"""One-time metadata migration and deterministic n8n export. Does not change colors."""
import json
from pathlib import Path
from review import ROOT, HAZARDS, read_js, write_js, write_json


def migrate():
    data = read_js(ROOT / 'data_updated.js')
    for r in data['records']:
        r.setdefault('provenance', {h: {'status': 'xlsx' if h in r.get('ev', {}).get('fromXlsx', []) else
                                        'legacy' if r.get(h) else 'missing'} for h in HAZARDS})
    flood = next(h for h in data['hazardMeta'] if h['key'] == 'flood')
    template = json.loads((ROOT / 'pipeline/hazard_meta.json').read_text(encoding='utf-8'))
    fm = next(h for h in template if h['key'] == 'flood')
    flood.pop('url', None)
    flood['urls'] = fm['urls']
    cut = data['buildInfo']['cutoffs']['flood']
    flood['method'] = fm['method'] + ' · เหตุการณ์จริง: แดง ≥%s วัน; ส้ม ≥%s; เหลือง ≥%s; เขียว ≥1' % tuple(cut)
    flood['method'] += ' · ถ้าข้อมูลใหม่ไม่พร้อมคงค่าพื้นฐานเดิม'
    write_js(ROOT / 'data_updated.js', data)


def n8n():
    # Independent daily monitor. GitHub owns the hourly/daily data collection.
    nodes = [
        {'id':'daily', 'name':'ทุกวัน 07:00 ไทย', 'type':'n8n-nodes-base.scheduleTrigger', 'typeVersion':1.2,
         'position':[0,0], 'parameters':{'rule':{'interval':[{'field':'days','triggerAtHour':7,'triggerAtMinute':0}]}}},
        {'id':'status', 'name':'อ่านผลตรวจ GitHub', 'type':'n8n-nodes-base.httpRequest', 'typeVersion':4.2,
         'position':[240,0], 'retryOnFail':True, 'maxTries':3, 'waitBetweenTries':5000,
         'onError':'continueRegularOutput', 'parameters':{
             'url':'https://raw.githubusercontent.com/siwonbk-ui/Areazone/main/pipeline/update_status.json',
             'options':{'timeout':30000, 'response':{'response':{'responseFormat':'json'}}}}},
        {'id':'summary', 'name':'สรุปผลและตรวจความสด', 'type':'n8n-nodes-base.code', 'typeVersion':2,
         'position':[480,0], 'parameters':{'jsCode':'''const s = $input.first().json;
const checked = Date.parse(s.checkedAt || '');
const stale = !Number.isFinite(checked) || Date.now() - checked > 4 * 3600000;
const failed = Boolean(s.error) || stale || s.status !== 'ok';
const review = s.review || {};
const messages = [
  failed ? 'NAT CAT: ต้องตรวจระบบ (ดึงผลไม่ได้ / ข้อมูลล่าช้า / แหล่งข้อมูลขัดข้อง)' : 'NAT CAT: ระบบตรวจข้อมูลทำงาน',
  'ตรวจ GitHub ล่าสุด: ' + (s.checkedAt || 'ไม่ทราบ'),
  'ผล baseline: ' + (s.baselineStatus || 'ไม่ทราบ'),
  'แหล่งปัจจุบัน: ' + JSON.stringify(s.currentSources || {}),
  review.status === 'pending_review' ? 'มีรายการสีพื้นฐานรอตรวจ ' + review.changeCount + ' รายการ; ยังใช้ค่าเดิม' : 'สถานะตรวจสี: ' + (review.status || 'ไม่มีรายการใหม่'),
  'ชั้นภัยที่คงค่าเดิมเพราะคุณภาพข้อมูล: ' + (review.blockedHazards || []).join(', '),
  'ผลรัน: https://github.com/siwonbk-ui/Areazone/actions/workflows/update-data.yml'
];
return [{json:{needsAttention:failed || review.status === 'pending_review', text:messages.join('\\n'), checkedAt:s.checkedAt || null}}];'''}},
        {'id':'delivery', 'name':'ผลสรุป — ต่อช่องทางแจ้งทีมที่นี่', 'type':'n8n-nodes-base.noOp', 'typeVersion':1,
         'position':[720,0], 'parameters':{}}
    ]
    connections = {a['name']:{'main':[[{'node':b['name'],'type':'main','index':0}]]} for a,b in zip(nodes,nodes[1:])}
    output = {'name':'NAT CAT v2 — ตรวจผลรายวัน (GitHub เก็บข้อมูล)', 'active':False,
              'nodes':nodes, 'connections':connections, 'pinData':{},
              'settings':{'executionOrder':'v1','timezone':'Asia/Bangkok'}}
    write_json(ROOT / 'n8n/natcat-daily-update.json', output)
    write_json(ROOT.parent / 'NAT CAT v2 — ตรวจผลรายวัน.json', output)


if __name__ == '__main__':
    migrate()
    n8n()
