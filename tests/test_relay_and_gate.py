import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import current
import commit_gate
import state_cache

NOW = datetime(2026, 9, 25, 6, tzinfo=timezone.utc)
DMR = current.SOURCES[0]
FEED = ('<rss><channel><item><title>รายงานสถานการณ์ดินถล่ม จ.น่าน</title>'
        '<link>https://www.dmr.go.th/report-1</link>'
        '<pubDate>Fri, 25 Sep 2026 09:00:00 +0700</pubDate></item></channel></rss>')


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'dmr.json'
        self.source = dict(DMR, relayFile=str(self.path))

    def tearDown(self):
        self.tmp.cleanup()

    def relay(self, **kw):
        body = {'source': DMR['url'], 'fetchedAt': '2026-09-25T02:10:00Z', 'relay': 'n8n', 'xml': FEED, **kw}
        self.path.write_text(json.dumps(body, ensure_ascii=False), encoding='utf-8')

    def test_dmr_is_configured_for_the_relay(self):
        self.assertEqual(DMR['id'], 'dmr')
        self.assertEqual(DMR['relayFile'], 'pipeline/feeds/dmr.json')

    def test_relay_used_instead_of_network(self):
        self.relay()
        with patch.object(current, 'fetch') as fetch:
            result = current.collect(self.source, None, NOW, ['น่าน'])
            fetch.assert_not_called()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['transport'], 'n8n-relay')
        self.assertEqual(result['lastSuccessAt'], '2026-09-25T02:10:00+00:00')
        self.assertEqual(result['events'][0]['mentionedProvinces'], ['น่าน'])
        self.assertNotIn('relayFile', result)

    def test_relay_not_throttled(self):
        self.relay()
        previous = {'lastAttemptAt': NOW.isoformat(), 'status': 'ok', 'events': []}
        result = current.collect(self.source, previous, NOW, [])
        self.assertEqual(len(result['events']), 1)

    def test_relay_for_other_source_rejected_and_previous_kept(self):
        self.relay(source='https://fake.example/feed')
        previous = {'events': [{'id': 'kept'}], 'lastSuccessAt': '2026-09-24T02:00:00+00:00'}
        result = current.collect(self.source, previous, NOW, [])
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['events'], previous['events'])
        self.assertEqual(result['lastSuccessAt'], previous['lastSuccessAt'])

    def test_future_relay_time_rejected(self):
        self.relay(fetchedAt='2026-09-26T00:00:00Z')
        self.assertEqual(current.collect(self.source, None, NOW, [])['status'], 'unavailable')

    def test_relay_item_from_foreign_domain_rejected(self):
        self.relay(xml=FEED.replace('www.dmr.go.th', 'fake.example'))
        self.assertEqual(current.collect(self.source, None, NOW, [])['status'], 'unavailable')

    def test_relay_not_refreshed_in_time_is_stale(self):
        self.relay(fetchedAt=(NOW - timedelta(hours=DMR['intervalHours'] * 2 + 1)).isoformat())
        result = current.collect(self.source, None, NOW, [])
        self.assertEqual(result['status'], 'stale')

    def test_direct_fetch_keeps_hourly_pace_without_relay(self):
        previous = {'lastAttemptAt': (NOW - timedelta(hours=1, minutes=1)).isoformat(), 'status': 'unavailable'}
        with patch.object(current, 'fetch', return_value=FEED.encode('utf-8')) as fetch:
            current.collect(self.source, previous, NOW, [])
            fetch.assert_called_once()

    def test_no_relay_file_falls_back_to_network(self):
        with patch.object(current, 'fetch', return_value=FEED.encode('utf-8')) as fetch:
            result = current.collect(self.source, None, NOW, [])
            fetch.assert_called_once()
        self.assertNotIn('transport', result)


def status(checked, **kw):
    return json.dumps({'checkedAt': checked, 'status': 'ok', **kw})


class CommitGateTests(unittest.TestCase):
    head = status('2026-09-25T05:17:00+00:00')

    def test_timestamps_only_is_skipped(self):
        pairs = {'pipeline/update_status.json': (self.head, status('2026-09-25T06:17:00+00:00'))}
        commit, _ = commit_gate.decide(pairs, self.head, NOW)
        self.assertFalse(commit)

    def test_js_wrapper_timestamps_only_is_skipped(self):
        old = 'window.NATCAT_CURRENT = {"checkedAt": "a", "events": [1]};\n'
        new = 'window.NATCAT_CURRENT = {"checkedAt": "b", "events": [1]};\n'
        self.assertFalse(commit_gate.differs(old, new))

    def test_content_change_is_committed(self):
        pairs = {'pipeline/update_status.json': (self.head, status('2026-09-25T06:17:00+00:00', status='degraded'))}
        commit, reason = commit_gate.decide(pairs, self.head, NOW)
        self.assertTrue(commit)
        self.assertIn('update_status.json', reason)

    def test_new_or_deleted_file_is_committed(self):
        self.assertTrue(commit_gate.decide({'pipeline/new.json': (None, '{}')}, self.head, NOW)[0])
        self.assertTrue(commit_gate.decide({'pipeline/old.json': ('{}', None)}, self.head, NOW)[0])

    def test_heartbeat_keeps_page_freshness(self):
        old_head = status((NOW - commit_gate.HEARTBEAT - timedelta(minutes=1)).isoformat())
        pairs = {'pipeline/update_status.json': (old_head, status(NOW.isoformat()))}
        commit, reason = commit_gate.decide(pairs, old_head, NOW)
        self.assertTrue(commit)
        self.assertIn('heartbeat', reason)

    def test_heartbeat_below_page_lateness_threshold(self):
        # page and n8n flag a check older than 4h as late; heartbeat + 1h cron must stay under it
        self.assertLess(commit_gate.HEARTBEAT + timedelta(hours=1), timedelta(hours=4))

    def test_nothing_changed(self):
        self.assertFalse(commit_gate.decide({}, self.head, NOW)[0])


class StateCacheTests(unittest.TestCase):
    def test_newer_status_wins(self):
        a, b = {'checkedAt': '2026-09-25T05:00:00+00:00'}, {'checkedAt': '2026-09-25T06:00:00+00:00'}
        self.assertEqual(state_cache.newer_status(a, b), b)
        self.assertEqual(state_cache.newer_status(b, a), b)
        self.assertEqual(state_cache.newer_status(a, None), a)

    def test_sources_merged_per_source(self):
        committed = {'dmr': {'lastAttemptAt': '2026-09-25T06:00:00+00:00'},
                     'new-src': {'lastAttemptAt': '2026-09-25T01:00:00+00:00'}}
        cached = {'dmr': {'lastAttemptAt': '2026-09-25T05:00:00+00:00'},
                  'tmd-earthquake': {'lastAttemptAt': '2026-09-25T05:30:00+00:00'}}
        merged = state_cache.newer_sources(committed, cached)
        self.assertEqual(merged['dmr'], committed['dmr'])
        self.assertIn('new-src', merged)
        self.assertEqual(merged['tmd-earthquake'], cached['tmd-earthquake'])


if __name__ == '__main__':
    unittest.main()
