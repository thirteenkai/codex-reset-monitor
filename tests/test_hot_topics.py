import copy
import io
import os
import unittest
import urllib.error
from unittest.mock import patch

import hot_topics as hot
from monitor import deliver


def item(index=1, story='event-1'):
    links = {'aihot': f'https://aihot.news/items/item-{index}'}
    if story:
        links['story'] = f'https://aihot.virxact.com/story/{story}'
    return {'id': f'item-{index}', 'rank': index, 'title': f'官方热点{index}',
            'sourceCount': 3, 'signalCount': 7, 'latestAt': '2026-09-15T02:00:00Z', 'links': links}


@patch.dict(os.environ, {'LARK_CHAT_ID': 'test-chat', 'EXPECTED_APP_ID': 'test-app'})
class HotTopicsTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch('hot_topics.now', return_value='2026-09-15T02:10:00Z')
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.fetch = patch('hot_topics.fetch_story', side_effect=lambda i: self.story(i))
        self.fetch.start()
        self.addCleanup(self.fetch.stop)

    def story(self, value):
        if not value['links'].get('story'):
            return None
        return {'publicId': value['links']['story'].rsplit('/', 1)[1],
                'firstReportAt': '2026-09-15T01:00:00Z',
                'latestAt': '2026-09-15T02:00:00Z', 'latest': '官方新增进展',
                'reports': [{'id': 'report-1', 'title': '本次报道标题',
                             'publishedAt': '2026-09-15T02:00:00Z',
                             'links': {'aihot': 'https://aihot.news/items/report-1'}}]}

    def ready(self, items):
        state = hot.baseline(items, None)
        hot.discover(state, items)
        return state

    def test_initialization_never_broadcasts_history(self):
        state = self.ready([item()])
        self.assertFalse(state['pending'])
        self.assertEqual(hot.discover(state, [item()]), 0)

    def test_new_official_event_only_once_even_after_leaving_and_reentering(self):
        state = self.ready([item()])
        self.assertEqual(hot.discover(state, [item(2, 'event-2')]), 1)
        hot.discover(state, [])
        newer = item(3, 'event-2')
        newer.update(sourceCount=20, signalCount=100, title='换了代表报道')
        self.assertEqual(hot.discover(state, [newer]), 0)
        self.assertEqual(len(state['pending']), 1)

    def test_later_story_attachment_is_not_a_new_event(self):
        state = hot.baseline([item(story=None)], None)
        self.assertEqual(hot.discover(state, [item()]), 0)

    def test_counts_rank_and_title_changes_do_not_trigger(self):
        state = hot.baseline([item()], None)
        changed = item()
        changed.update(rank=9, title='更新标题', sourceCount=999, signalCount=1234)
        self.assertEqual(hot.discover(state, [changed]), 0)

    def test_invalid_api_and_untrusted_links_fail_closed(self):
        valid = {'schemaVersion': 1, 'count': 1, 'items': [item()]}
        self.assertEqual(len(hot.validate(valid)), 1)
        for mutate in (lambda d: d.update(count=2),
                       lambda d: d['items'][0].update(rank=4),
                       lambda d: d['items'][0]['links'].update(aihot='https://evil.test/items/1'),
                       lambda d: d['items'][0]['links'].update(story='https://aihot.news/story/1?token=bad')):
            bad = copy.deepcopy(valid)
            mutate(bad)
            with self.assertRaises(ValueError):
                hot.validate(bad)

    def test_render_preserves_official_rank_and_labels_time_honestly(self):
        value = item()
        value['title'] = '<at user_id="all"> ![image](https://evil.test)'
        report = self.story(value)['reports'][0]
        report['title'] = value['title']
        rendered = hot.render(value, self.story(value), report)
        self.assertIn('第 1 名', rendered)
        self.assertIn('本次报道：2026-09-15 10:00', rendered)
        self.assertIn('事件首报：', rendered)
        self.assertNotIn('新上榜', rendered)
        self.assertNotIn('<at', rendered)
        self.assertNotIn('![', rendered)

    def test_old_event_with_recent_signal_is_silent(self):
        state = self.ready([])
        story = self.story(item())
        story['firstReportAt'] = '2026-09-08T06:57:09Z'
        self.assertEqual(hot.discover(state, [item()], lambda _: story), 0)
        self.assertFalse(state['pending'])

    def test_migration_preserves_receipts_and_silently_baselines(self):
        state = hot.baseline([], None)
        state['pending'] = {'draft': {'phase': 'prepared'}, 'receipt': {'phase': 'sent'}}
        state['sent'] = {'existing': {'messageId': 'old'}}
        self.assertEqual(hot.discover(state, [item()]), 0)
        self.assertNotIn('draft', state['pending'])
        self.assertIn('receipt', state['pending'])
        self.assertIn('existing', state['sent'])

    def test_missing_timeline_cannot_send(self):
        state = self.ready([])
        self.assertEqual(hot.discover(state, [item()], lambda _: None), 0)

    def test_new_report_and_changed_progress_required_for_old_story(self):
        state = self.ready([item()])
        story = self.story(item())
        story['firstReportAt'] = '2026-09-08T06:57:09Z'
        story['reports'][0].update(id='report-2', publishedAt='2026-09-15T02:11:00Z')
        story['latest'] = '新增官方进展'
        with patch('hot_topics.now', return_value='2026-09-15T02:15:00Z'):
            self.assertEqual(hot.discover(state, [item()], lambda _: story), 1)
            self.assertEqual(hot.discover(state, [item()], lambda _: story), 0)
        body = next(iter(state['pending'].values()))['body']
        self.assertIn('事件进展', body)
        self.assertIn('新增官方进展', body)
        self.assertIn('2026-09-08', body)

    def test_new_report_without_progress_change_is_silent(self):
        state = self.ready([item()])
        story = self.story(item())
        story['reports'][0].update(id='report-2', publishedAt='2026-09-15T02:11:00Z')
        with patch('hot_topics.now', return_value='2026-09-15T02:15:00Z'):
            self.assertEqual(hot.discover(state, [item()], lambda _: story), 0)

    def test_304_still_checks_cached_story_timeline(self):
        state = self.ready([item()])
        state['lastPollAt'] = '2026-09-15T02:00:00Z'
        with patch('hot_topics.HotState') as remote, patch('hot_topics.fetch_topics', return_value=(None, 'etag')), patch('hot_topics.fetch_story', return_value=self.story(item())) as fetch:
            remote.return_value.load.return_value = state
            hot.main()
            fetch.assert_called_once_with(item())

    def test_summary_change_without_new_report_is_silent(self):
        state = self.ready([item()])
        story = self.story(item())
        story['latest'] = '仅改写综述'
        self.assertEqual(hot.discover(state, [item()], lambda _: story), 0)

    @patch('hot_topics.urllib.request.urlopen')
    def test_conditional_request_and_304(self, request):
        request.side_effect = urllib.error.HTTPError(hot.SOURCE, 304, '', {}, io.BytesIO())
        self.assertEqual(hot.fetch_topics('test-etag'), (None, 'test-etag'))
        self.assertEqual(request.call_args.args[0].get_header('If-none-match'), 'test-etag')

    @patch('hot_topics.urllib.request.urlopen')
    def test_rate_limit_preserves_server_backoff(self, request):
        request.side_effect = urllib.error.HTTPError(hot.SOURCE, 429, '', {'Retry-After': '1200'}, io.BytesIO())
        with self.assertRaises(hot.RateLimited) as caught:
            hot.fetch_topics('etag')
        self.assertGreater((hot.timestamp(caught.exception.retry_at) - hot.timestamp(hot.now())).total_seconds(), 1190)

    def test_failed_content_readback_retains_receipt_and_never_resends(self):
        state = self.ready([])
        hot.discover(state, [item()])
        sent = []
        def call(args):
            if '--dry-run' in args:
                return {}
            if args[0] == '+messages-send':
                sent.append(args)
                return {'message_id': 'om_test'}
            return {'messages': [{'message_id': 'om_test', 'chat_id': 'test-chat',
                                  'sender': {'id': 'test-app'}, 'content': 'wrong content'}]}
        with self.assertRaises(RuntimeError):
            deliver(state, lambda _: None, hot.checked_cli(state, 'bot-id', call))
        pending = next(iter(state['pending'].values()))
        self.assertEqual(pending['phase'], 'sent')
        def read(args):
            self.assertEqual(args[0], '+messages-mget')
            return {'messages': [{'message_id': 'om_test', 'chat_id': 'test-chat',
                                  'sender': {'id': 'test-app'}, 'content': pending['body']}]}
        self.assertEqual(deliver(state, lambda _: None, hot.checked_cli(state, 'bot-id', read)), 1)
        self.assertEqual(len(sent), 1)


if __name__ == '__main__':
    unittest.main()
