"""Notify once per newly listed AIHOT event, using the official ranking unchanged."""
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


def render(item):
    lines = ['**AIHOT 官方热点 · 新上榜**',
             f"第 {item['rank']} 名｜{text(item['title'])}", '',
             f"独立信源：{item['sourceCount']} 个 · 信号：{item['signalCount']} 条",
             '最新信号：' + beijing(item['latestAt']) + '（北京时间）',
             '新上榜不代表事件刚发生。', '',
             f"[AIHOT 阅读]({item['links']['aihot']})"]
    if item['links'].get('story'):
        lines.append(f"[事件时间线与官方综述]({item['links']['story']})")
    lines.append('数据来源：AIHOT；按官方榜单排名展示。')
    return '\n'.join(lines)


def baseline(items, etag):
    return {'version': 1, 'kind': 'official-hot-topics', 'initializedAt': now(),
            'events': {event_key(i): {'firstSeenAt': now()} for i in items},
            'itemAliases': {i['id']: event_key(i) for i in items},
            'pending': {}, 'sent': {}, 'etag': etag, 'lastPollAt': now(),
            'chatId': os.environ['LARK_CHAT_ID'], 'appId': os.environ['EXPECTED_APP_ID']}


def discover(state, items):
    added = 0
    for item in items:
        key = event_key(item)
        alias = state['itemAliases'].get(item['id'])
        # Attaching a story link later must not turn an already seen item into new news.
        known = key in state['events'] or alias in state['events']
        if not known:
            delivery_key = 'hot-' + digest(key)[:40]
            if delivery_key not in state['pending'] and delivery_key not in state['sent']:
                state['pending'][delivery_key] = {'body': render(item), 'phase': 'prepared'}
                added += 1
        state['events'].setdefault(key, {'firstSeenAt': now()})
        state['itemAliases'][item['id']] = key
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
        items, etag = fetch_topics(state.get('etag'))
    except RateLimited as error:
        state['retryAfter'] = error.retry_at
        remote.save(state)
        raise
    added = discover(state, items) if items is not None else 0
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
