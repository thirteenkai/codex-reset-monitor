"""Notify recent events and evidenced timeline updates from the official AIHOT ranking."""
import copy
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from email.utils import parsedate_to_datetime

from monitor import RemoteState, beijing, cli, deliver, digest, find_message, now, text, timestamp

SOURCE = 'https://aihot.virxact.com/api/v1/hot-topics'
HOSTS = {'aihot.news', 'aihot.virxact.com'}


class RateLimited(RuntimeError):
    def __init__(self, retry_after):
        try:
            delay = max(300, int(retry_after))
            self.retry_at = (timestamp(now()) + timedelta(seconds=delay)).isoformat()
        except (TypeError, ValueError):
            self.retry_at = max(timestamp(now()) + timedelta(seconds=300),
                                parsedate_to_datetime(retry_after)).isoformat() if retry_after else (
                timestamp(now()) + timedelta(seconds=300)).isoformat()


def trusted_link(value, prefix):
    if not isinstance(value, str):
        raise ValueError('Missing AIHOT link')
    url = urllib.parse.urlsplit(value)
    if (url.scheme != 'https' or url.netloc not in HOSTS or url.query or url.fragment
            or not re.fullmatch(prefix + r'[A-Za-z0-9-]+', url.path)):
        raise ValueError('Unexpected AIHOT link')
    return value


def event_key(item):
    if item['links'].get('story'):
        return 'story:' + trusted_link(item['links']['story'], '/story/').rsplit('/', 1)[1]
    return 'item:' + item['id']


def validate(data):
    items = data.get('items')
    if (data.get('schemaVersion') != 1 or not isinstance(items, list)
            or data.get('count') != len(items) or len(items) > 10):
        raise ValueError('Invalid official hot topics response')
    ids = set()
    for rank, item in enumerate(items, 1):
        if item.get('rank') != rank or not isinstance(item.get('id'), str) or item['id'] in ids:
            raise ValueError('Invalid official rank or ID')
        ids.add(item['id'])
        if not isinstance(item.get('title'), str) or not item['title'].strip():
            raise ValueError('Missing title')
        trusted_link(item['links']['aihot'], '/items/')
        event_key(item)
        timestamp(item['latestAt'])
        for field in ('sourceCount', 'signalCount'):
            if type(item.get(field)) is not int or item[field] < 0:
                raise ValueError('Invalid evidence count')
    return items


def fetch_topics(etag=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'aihot-hot-topics-monitor/1.0'}
    if etag:
        headers['If-None-Match'] = etag
    request = urllib.request.Request(SOURCE, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return validate(json.load(response)), response.headers.get('ETag')
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return None, etag
        if error.code in (429, 503):
            raise RateLimited(error.headers.get('Retry-After')) from None
        # No immediate retries: the next five-minute run respects normal rate limits.
        raise RuntimeError('Official hot topics request failed') from None


def fetch_story(item):
    if not item['links'].get('story'):
        return None
    public_id = event_key(item).split(':', 1)[1]
    request = urllib.request.Request(
        'https://aihot.virxact.com/api/v1/stories/' + public_id,
        headers={'Accept': 'application/json', 'User-Agent': 'aihot-hot-topics-monitor/1.1'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (429, 503):
            raise RateLimited(error.headers.get('Retry-After')) from None
        if error.code == 404:
            return None
        raise
    story = data['story']
    if data.get('schemaVersion') != 1 or not story.get('publicId'):
        raise ValueError('Invalid story')
    timestamp(story['firstReportAt'])
    timestamp(story['latestAt'])
    if not isinstance(story.get('latest'), str) or not isinstance(story.get('reports'), list):
        raise ValueError('Incomplete story timeline')
    for report in story['reports']:
        timestamp(report['publishedAt'])
        trusted_link(report['links']['aihot'], '/items/')
    return story


def render(item, story=None, report=None, update=False):
    if story is None:
        return 'AIHOT 热点监控接入检查（不发送）'
    lines = ['**AIHOT 官方热点 · ' + ('事件进展' if update else '近期事件') + '**',
             f"第 {item['rank']} 名｜{text(report['title'])}", '',
             '最新进展（AIHOT 综述）：' + text(story['latest']), '',
             '事件首报：' + beijing(story['firstReportAt']) + '（北京时间）',
             '本次报道：' + beijing(report['publishedAt']) + '（北京时间）',
             '报道来源：' + text(report.get('source', {}).get('name', 'AIHOT')), '',
             f"[本次报道]({report['links']['aihot']})",
             f"[事件时间线]({item['links']['story']})",
             '数据来源：AIHOT；排名沿用官方榜单，时效与去重为本监控规则。']
    return '\n'.join(lines)


def baseline(items, etag):
    return {'version': 1, 'kind': 'official-hot-topics', 'initializedAt': now(),
            'events': {event_key(i): {'firstSeenAt': now()} for i in items},
            'itemAliases': {i['id']: event_key(i) for i in items},
            'pending': {}, 'sent': {}, 'etag': etag, 'lastPollAt': now(),
            'chatId': os.environ['LARK_CHAT_ID'], 'appId': os.environ['EXPECTED_APP_ID']}


def discover(state, items, story_fetch=None):
    story_fetch = story_fetch or fetch_story
    added = 0
    migrating = state.get('freshnessVersion') != 2
    current = timestamp(now())
    # Cancel only unsent legacy drafts; retain uncertain sends and receipts for reconciliation.
    for key, pending in list(state['pending'].items()):
        if migrating and pending['phase'] == 'prepared':
            del state['pending'][key]
    for item in items:
        key = event_key(item)
        alias = state['itemAliases'].get(item['id'])
        previous = state['events'].get(key) or state['events'].get(alias)
        story = story_fetch(item)
        state['itemAliases'][item['id']] = key
        if not story:
            state['events'].setdefault(key, {'firstSeenAt': now()})
            continue
        canonical = 'story:' + story['publicId']
        previous = state['events'].get(canonical) or previous
        reports = sorted(story['reports'], key=lambda r: timestamp(r['publishedAt']), reverse=True)
        record = dict(previous or {'firstSeenAt': now()})
        old_ids = set(record.get('reportIds', []))
        fresh_reports = [r for r in reports if r['id'] not in old_ids
                         and timedelta(0) <= current - timestamp(r['publishedAt']) <= timedelta(hours=48)]
        recent_event = timedelta(0) <= current - timestamp(story['firstReportAt']) <= timedelta(hours=48)
        update = bool(previous and 'latestSummary' in previous)
        eligible = not migrating and fresh_reports and (
            (not previous and recent_event) or
            (update and story['latest'].strip() != previous['latestSummary']
             and any(timestamp(r['publishedAt']) > timestamp(previous['observedAt']) for r in fresh_reports)))
        if eligible:
            report = next((r for r in fresh_reports if not update or
                           timestamp(r['publishedAt']) > timestamp(previous['observedAt'])), None)
            delivery_key = 'hot-v2-' + digest(canonical + ':' + report['id'])[:40]
            if delivery_key not in state['pending'] and delivery_key not in state['sent']:
                state['pending'][delivery_key] = {
                    'body': render(item, story, report, update), 'phase': 'prepared'}
                added += 1
        record.update(reportIds=sorted(old_ids | {r['id'] for r in reports}),
                      latestSummary=story['latest'].strip(), observedAt=now())
        state['events'][canonical] = record
        state['events'][key] = record
        state['itemAliases'][item['id']] = canonical
    state['freshnessVersion'] = 2
    state['cachedItems'] = items
    return added


class HotState(RemoteState):
    def __init__(self):
        super().__init__()
        self.url = f'https://api.github.com/repos/{self.repo}/contents/hot-topics-state.enc'

    def request(self, method='GET', body=None):
        if body is not None and body.get('sha') is None:
            body = {k: v for k, v in body.items() if k != 'sha'}
        return super().request(method, body)

    def load(self):
        state = super().load()
        if (state.get('kind') != 'official-hot-topics'
                or not isinstance(state.get('itemAliases'), dict)
                or state.get('chatId') != os.environ['LARK_CHAT_ID']
                or state.get('appId') != os.environ['EXPECTED_APP_ID']):
            raise ValueError('Hot topics state or destination mismatch')
        return state


def preflight():
    result = subprocess.run(['lark-cli', 'auth', 'status', '--verify', '--json'],
                            capture_output=True, text=True, timeout=50)
    if result.returncode:
        raise RuntimeError('Bot preflight failed')
    identity = json.loads(result.stdout)
    bot = identity.get('identities', {}).get('bot', {})
    if identity.get('appId') != os.environ['EXPECTED_APP_ID'] or not bot.get('verified'):
        raise RuntimeError('Unexpected bot identity')
    cli(['chats', 'get', '--params', json.dumps({'chat_id': os.environ['LARK_CHAT_ID']})])
    return bot.get('openId')


def checked_cli(state, bot_id, call=cli):
    def checked(args):
        result = call(args)
        if args[0] == '+messages-mget':
            message_id = args[args.index('--message-ids') + 1]
            message = find_message(result, message_id)
            pending = next(p for p in state['pending'].values() if p.get('messageId') == message_id)
            if not message or message.get('chat_id') != state['chatId']:
                raise RuntimeError('Message target verification failed')
            sender = message.get('sender', {})
            if sender.get('id') not in {state['appId'], bot_id}:
                raise RuntimeError('Message sender verification failed')
            normalize = lambda value: re.sub(r'[\s*`_]+', '', value)
            if normalize(pending['body']) not in normalize(message.get('content', '')):
                raise RuntimeError('Message content verification failed')
        return result
    return checked


def main():
    remote = HotState()
    initialize = os.environ.get('INITIALIZE_HOT_TOPICS') == 'true'
    try:
        state = remote.load()
    except urllib.error.HTTPError as error:
        if error.code != 404 or not initialize:
            raise
        bot_id = preflight()
        items, etag = fetch_topics()
        state = baseline(items, etag)
        if items:
            cli(['+messages-send', '--chat-id', state['chatId'], '--markdown', render(items[0]), '--dry-run'])
        remote.save(state)
        print(f'Official hot topics initialized: {len(items)} entries; no history sent; bot/chat/dry-run passed.')
        return
    if initialize:
        print('Official hot topics already initialized; existing delivery state preserved.')
        return
    before = copy.deepcopy(state)
    if state.get('retryAfter') and timestamp(now()) < timestamp(state['retryAfter']):
        print('Official hot topics: respecting server Retry-After.')
        return
    remaining = 300 - (timestamp(now()) - timestamp(state['lastPollAt'])).total_seconds()
    if 0 < remaining <= 20:
        time.sleep(remaining)
    if remaining > 20:
        print('Official hot topics: cached interval; waiting for next poll.')
        return
    try:
        items, etag = fetch_topics(state.get('etag') if state.get('freshnessVersion') == 2 else None)
        added = discover(state, items if items is not None else state.get('cachedItems', []))
    except RateLimited as error:
        state['retryAfter'] = error.retry_at
        remote.save(state)
        raise
    state.update(etag=etag, lastPollAt=now())
    state.pop('retryAfter', None)
    if state != before:
        remote.save(state)
    delivered = 0
    if state['pending']:
        bot_id = preflight()
        delivered = deliver(state, remote.save, checked_cli(state, bot_id))
    print(f'Official hot topics: {"unchanged (304)" if items is None else str(len(items)) + " entries"}; '
          f'queued {added}; verified deliveries {delivered}.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Hot topics monitor failed safely ({type(error).__name__}); retained delivery state.', file=sys.stderr)
        sys.exit(1)
