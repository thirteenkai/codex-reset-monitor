import copy
import os
import unittest
from unittest.mock import patch

from monitor import discover, deliver, render, validate


def event():
    return {'id': 'e1', 'type': 'direct_reset', 'status': 'announced',
            'posts': [{'id': 'p1', 'publishedAt': '2026-09-13T10:00:00+08:00',
                       'stage': '预告', 'text': '稍后重置', 'url': 'https://x.com/example/status/1'}]}


def state():
    return {'version': 1, 'initializedAt': '2026-09-13T00:00:00+08:00',
            'events': {}, 'pending': {}, 'sent': {}}


class DiscoveryTests(unittest.TestCase):
    def test_new_then_unchanged_then_confirmation(self):
        s, e = state(), event()
        self.assertEqual(discover(s, [copy.deepcopy(e)]), 1)
        self.assertEqual(discover(s, [copy.deepcopy(e)]), 0)
        e['status'] = 'confirmed'
        e['confirmedAt'] = '2026-09-13T12:00:00+08:00'
        self.assertEqual(discover(s, [e]), 1)
        self.assertEqual(len(s['pending']), 2)

    def test_history_backfill_and_timestamp_only_edit(self):
        s, e = state(), event()
        s['initializedAt'] = '2026-09-14T00:00:00+08:00'
        self.assertEqual(discover(s, [copy.deepcopy(e)]), 0)
        e['updatedAt'] = '2026-09-14T12:00:00+08:00'
        self.assertEqual(discover(s, [e]), 0)
        self.assertIn('e1', s['events'])

    def test_new_post_for_old_event_is_not_lost(self):
        s, e = state(), event()
        s['events']['e1'] = copy.deepcopy(e)
        e['posts'].append(dict(e['posts'][0], id='p2', text='已完成'))
        self.assertEqual(discover(s, [e]), 1)

    def test_invalid_api_does_not_become_empty_baseline(self):
        for d in ({'schemaVersion': 2, 'events': []},
                  {'schemaVersion': 1, 'events': [], 'count': 0}):
            with self.assertRaises(ValueError):
                validate(d)

    def test_receipt_confirmation_and_markup_safety(self):
        e = event()
        e.update(type='reset_credit', status='confirmed', confirmationBasis='receipt_review')
        e['posts'][0]['text'] = '![picture](https://evil.test) <at user_id="all">'
        body = render(e)
        self.assertIn('回执核对', body)
        self.assertIn('发重置卡', body)
        self.assertNotIn('![', body)
        self.assertNotIn('<at', body)


class DeliveryTests(unittest.TestCase):
    @patch.dict(os.environ, {'LARK_CHAT_ID': 'test-chat'})
    def test_receipt_saved_before_failed_readback_and_no_resend(self):
        s = state()
        s['pending']['key'] = {'phase': 'prepared', 'body': 'test'}
        saved, sent = [], []
        def save(v):
            saved.append(copy.deepcopy(v))
        def call(args):
            if '--dry-run' in args:
                return {}
            if args[0] == '+messages-send':
                self.assertEqual(saved[-1]['pending']['key']['phase'], 'attempting')
                sent.append(args)
                return {'message_id': 'om_test'}
            raise RuntimeError('read failed')
        with self.assertRaises(RuntimeError):
            deliver(s, save, call)
        self.assertEqual(s['pending']['key']['phase'], 'sent')
        def read(args):
            self.assertEqual(args[0], '+messages-mget')
            return {'items': [{'message_id': 'om_test', 'chat_id': 'test-chat'}]}
        self.assertEqual(deliver(s, save, read), 1)
        self.assertEqual(len(sent), 1)
        self.assertFalse(s['pending'])

    @patch.dict(os.environ, {'LARK_CHAT_ID': 'test-chat'})
    def test_expired_uncertain_send_is_never_repeated(self):
        s = state()
        s['pending']['key'] = {'phase': 'attempting', 'attemptedAt': '2020-01-01T00:00:00Z', 'body': 'test'}
        def forbidden(args):
            self.fail('Must not send an uncertain expired attempt')
        with self.assertRaises(RuntimeError):
            deliver(s, lambda s: None, forbidden)

    @patch.dict(os.environ, {'LARK_CHAT_ID': 'test-chat'})
    def test_no_send_if_pending_cannot_be_persisted(self):
        s = state()
        s['pending']['key'] = {'phase': 'prepared', 'body': 'test'}
        def save(s):
            raise RuntimeError('state conflict')
        def call(args):
            self.assertIn('--dry-run', args)
            return {}
        with self.assertRaises(RuntimeError):
            deliver(s, save, call)


if __name__ == '__main__':
    unittest.main()
