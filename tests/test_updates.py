import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import current
import review
import pipeline as baseline


def row(**kw):
    r = {'province':'เชียงใหม่','district':'เมือง','postal':50000,'postal_office':'ปณ.','note':None,
         'ev':{'fromXlsx':[]}, **{h:'orange' for h in review.HAZARDS}}
    r.update(kw)
    return r


class BaselineTests(unittest.TestCase):
    def test_blocked_layer_keeps_color_and_thresholds(self):
        previous={'records':[row(flood='red')], 'hazardMeta':[{'key':'flood','method':'old'}],
                  'buildInfo':{'cutoffs':{'flood':[20,13,8]},'incidentYears':{'flood':['2567']}}}
        candidate={'records':[row(flood='green')], 'hazardMeta':[{'key':'flood','method':'new'}],
                   'buildInfo':{'builtAt':'2026-09-24','blockedHazards':['flood'],'cutoffs':{'flood':[29,18,11]}}}
        result,_=review.retain_missing(candidate,previous,{'records':[row()]})
        self.assertEqual(result['records'][0]['flood'],'red')
        self.assertEqual(result['hazardMeta'][0]['method'],'old')
        self.assertEqual(result['buildInfo']['cutoffs']['flood'],[20,13,8])

    def test_name_columns_are_not_code_columns(self):
        mapping,_=baseline.header_map([['Province Code','Province','District Code','District','Disaster Date']])
        self.assertEqual(mapping['prov'],1)
        self.assertEqual(mapping['dist'],3)

    def test_province_count_summary_is_not_incident_year(self):
        fixture=[['Province','District'],['จังหวัด','อำเภอ'],['เชียงใหม่','5']]
        with patch.object(baseline,'cached',return_value='fixture'), patch.object(baseline,'read_rows',return_value=fixture):
            days,_,_,years=baseline.parse_incidents({'2562':{'id':'fixture','name':'summary','url':'https://example.com','format':'CSV'}})
        self.assertFalse(days)
        self.assertEqual(years,[])

    def test_flood_can_prefer_official_xlsx_over_malformed_csv(self):
        resources = {'resources': {
            'csv': {'name':'2563 flood', 'format':'CSV', 'url':'https://example.com/bad.csv'},
            'xlsx': {'name':'2563 flood', 'format':'XLSX', 'url':'https://example.com/good.xlsx'},
        }}
        chosen = baseline.pick_yearly(resources, prefer_xlsx=True)
        self.assertEqual(chosen['2563']['id'], 'xlsx')

    def test_missing_keeps_previous_before_xlsx(self):
        previous={'records':[row(flood='red')]}
        base={'records':[row(xlsx_flood='yellow')]}
        candidate={'buildInfo':{'builtAt':'2026-09-23'},'records':[row(flood='yellow',ev={'fromXlsx':['flood']})]}
        merged, changes=review.retain_missing(candidate,previous,base)
        self.assertEqual(merged['records'][0]['flood'],'red')
        self.assertEqual(merged['records'][0]['provenance']['flood']['status'],'retained')
        self.assertEqual(changes,[])

    def test_xlsx_used_when_no_previous(self):
        candidate={'buildInfo':{'builtAt':'2026-09-23'},'records':[row(flood=None)]}
        merged,_=review.retain_missing(candidate,{'records':[row(flood=None)]},{'records':[row(xlsx_flood='yellow')]})
        self.assertEqual(merged['records'][0]['flood'],'yellow')

    def test_wrong_area_or_color_rejected(self):
        with self.assertRaises(ValueError): review.validate({'records':[row(province='ผิด')]},{'records':[row()]})
        with self.assertRaises(ValueError): review.validate({'records':[row(flood='blue')]},{'records':[row()]})

    def test_stage_does_not_publish_and_hash_is_required(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(review,'ROOT',Path(tmp)):
            root=Path(tmp)
            review.write_json(root/'pipeline/districts.json',{'records':[row(xlsx_flood='yellow')]})
            review.write_js(root/'data_updated.js',{'records':[row()]})
            before=(root/'data_updated.js').read_bytes()
            candidate={'buildInfo':{'builtAt':'2026-09-23'},'records':[row(flood='red')]}
            review.stage(candidate,{'storm':{}})
            self.assertEqual(before,(root/'data_updated.js').read_bytes())
            with self.assertRaises(ValueError): review.approve('wrong','tester')
            report=json.loads((root/'pipeline/review_report.json').read_text(encoding='utf-8'))
            review.approve(report['reviewHash'],'tester')
            self.assertEqual(review.read_js(root/'data_updated.js')['records'][0]['flood'],'red')

    def test_changed_baseline_invalidates_approval(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(review,'ROOT',Path(tmp)):
            root=Path(tmp)
            review.write_json(root/'pipeline/districts.json',{'records':[row()]})
            review.write_js(root/'data_updated.js',{'records':[row()]})
            review.stage({'buildInfo':{'builtAt':'2026-09-23'},'records':[row(flood='red')]},{})
            report=json.loads((root/'pipeline/review_report.json').read_text(encoding='utf-8'))
            review.write_js(root/'data_updated.js',{'records':[row(flood='green')]})
            with self.assertRaises(ValueError): review.approve(report['reviewHash'],'tester')


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,9,23,12,tzinfo=timezone.utc)
        self.source=current.SOURCES[1]
        self.xml=b'''<rss><channel><item><title>Report</title><link>https://earthquake.tmd.go.th/event</link><pubDate>Wed, 23 Sep 2026 10:00:00 +0700</pubDate></item></channel></rss>'''

    def test_official_not_impact_verified(self):
        events,_=current.parse_feed(self.xml,self.source,self.now,[])
        self.assertEqual(len(events),1)
        self.assertFalse(events[0]['impactConfirmed'])
        self.assertNotIn('color',events[0])

    def test_wrong_domain_rejected(self):
        with self.assertRaises(ValueError):
            current.parse_feed(self.xml.replace(b'earthquake.tmd.go.th',b'fake.example'),self.source,self.now,[])

    def test_old_feed_not_current_and_future_rejected(self):
        events,_=current.parse_feed(self.xml,self.source,self.now+timedelta(days=8),[])
        self.assertEqual(events,[])
        with self.assertRaises(ValueError): current.parse_feed(self.xml,self.source,self.now-timedelta(days=1),[])

    def test_outage_retains_data(self):
        previous={'events':[{'id':'existing'}],'lastSuccessAt':'2026-09-22T00:00:00+00:00'}
        with patch.object(current,'fetch',side_effect=TimeoutError):
            result=current.collect(self.source,previous,self.now,[])
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['events'],previous['events'])
        self.assertEqual(result['lastSuccessAt'],previous['lastSuccessAt'])

    def test_feed_interval_honored(self):
        previous={'lastAttemptAt':self.now.isoformat(),'status':'ok'}
        with patch.object(current,'fetch') as fetch:
            self.assertEqual(current.collect(self.source,previous,self.now,[]),previous)
            fetch.assert_not_called()


if __name__ == '__main__': unittest.main()
