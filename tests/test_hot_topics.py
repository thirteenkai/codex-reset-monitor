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
    def test_initialization_never_broadcasts_history(self):
        state = hot.baseline([item()], 'etag')
        self.assertFalse(state['pending'])
        self.assertEqual(hot.discover(state, [item()]), 0)

    def test_new_official_event_only_once_even_after_leaving_and_reentering(self):
        state = hot.baseline([item()], 'etag')
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
        rendered = hot.render(value)
        self.assertIn('第 1 名', rendered)
        self.assertIn('最新信号：2026-09-15 10:00', rendered)
        self.assertNotIn('<at', rendered)
        self.assertNotIn('![', rendered)

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
        state = hot.baseline([], None)
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
