"""AIHOT reset notifications. No model calls; all Lark operations use its CLI."""
import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

SOURCE = 'https://aihot.news/api/v1/codex-resets'
CALENDAR = 'https://aihot.news/codex-reset'


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def timestamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timezone missing')
    return dt


def beijing(value):
    return timestamp(value).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')


def signature(event):
    return {k: event.get(k) for k in (
        'type', 'status', 'confirmedAt', 'schedule', 'scope', 'confirmationBasis'
    )} | {'posts': sorted((p['id'], p.get('stage')) for p in event['posts'])}


def validate(data):
    if data.get('schemaVersion') != 1 or not isinstance(data.get('events'), list):
        raise ValueError('Unexpected API schema')
    events = data['events']
    if not events or data.get('count') != len(events):
        raise ValueError('Empty or incomplete API response')
    if len({e['id'] for e in events}) != len(events):
        raise ValueError('Duplicate event IDs')
    for e in events:
        if e['type'] not in ('direct_reset', 'reset_credit') or e['status'] not in ('announced', 'confirmed'):
            raise ValueError('Unknown event type/status')
        if not isinstance(e['posts'], list) or not e['posts']:
            raise ValueError('Missing posts')
        for p in e['posts']:
            timestamp(p['publishedAt'])
            if not p['url'].startswith(('https://x.com/', 'https://twitter.com/')):
                raise ValueError('Unexpected source URL')
    return events


def fetch_events():
    result = subprocess.run(['curl', '--fail', '--silent', '--show-error', '--max-time', '30',
                             '--user-agent', 'codex-reset-monitor/1.0', SOURCE],
                            capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError('AIHOT fetch failed')
    return validate(json.loads(result.stdout))


def text(value):
    # Prevent API text from becoming mentions, images, or executable Markdown.
    value = str(value or '')
    for char in ('\\', '`', '*', '_', '[', ']', '<', '>', '!', '#'):
        value = value.replace(char, ' ')
    return value[:1600]


def render(event, old=None):
    label = '全员重置' if event['type'] == 'direct_reset' else '发重置卡'
    status = '预告' if event['status'] == 'announced' else '确认'
    lines = [f'**Codex｜{label} · {status}**']
    if event.get('confirmationBasis') == 'receipt_review':
        lines.append('AIHOT 回执核对确认（不代表你的账户已经领取）。')
    if event.get('scope'):
        lines.append('适用范围：' + text(event['scope']))
    if event.get('schedule'):
        lines.append(text(event['schedule'].get('label')))
    if event.get('confirmedAt'):
        lines.append('确认时间：' + beijing(event['confirmedAt']) + '（北京时间）')
    old_ids = {p['id'] for p in old['posts']} if old else set()
    posts = [p for p in event['posts'] if p['id'] not in old_ids]
    if not posts:
        posts = sorted(event['posts'], key=lambda p: p['publishedAt'], reverse=True)[:1]
    for p in sorted(posts, key=lambda p: p['publishedAt'])[:5]:
        lines += ['', f"{text(p.get('stage'))} · {beijing(p['publishedAt'])}（北京时间）",
                  text(p.get('text') or p.get('originalText')), f"[查看原帖]({p['url']})"]
    lines += ['', f'[来源：AIHOT · 重置日历]({CALENDAR})']
    return '\n'.join(lines)


def discover(state, events):
    """Queue real changes; baseline and historical backfills are never broadcast."""
    cutoff = timestamp(state['initializedAt'])
    added = 0
    for event in events:
        old = state['events'].get(event['id'])
        changed = old is None or signature(old) != signature(event)
        times = [timestamp(p['publishedAt']) for p in event['posts']]
        if event.get('confirmedAt'):
            times.append(timestamp(event['confirmedAt']))
        backfill = old is None and max(times) < cutoff
        if changed and not backfill:
            key = 'cr-' + digest([event['id'], signature(event)])[:40]
            if key not in state['sent'] and key not in state['pending']:
                state['pending'][key] = {'body': render(event, old), 'phase': 'prepared'}
                added += 1
        state['events'][event['id']] = event
    return added


class RemoteState:
    def __init__(self):
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.token = os.environ['GH_TOKEN']
        self.cipher = Fernet(os.environ['STATE_KEY'].encode())
        self.url = f'https://api.github.com/repos/{self.repo}/contents/state.enc'
        self.sha = None

    def request(self, method='GET', body=None):
        headers = {'Authorization': 'Bearer ' + self.token,
                   'Accept': 'application/vnd.github+json', 'User-Agent': 'codex-reset-monitor'}
        req = urllib.request.Request(self.url, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body else None)
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def load(self):
        result = self.request()
        self.sha = result['sha']
        state = json.loads(self.cipher.decrypt(base64.b64decode(result['content'])))
        if state.get('version') != 1 or not all(k in state for k in ('events', 'sent', 'pending', 'initializedAt')):
            raise ValueError('Missing or invalid state; initialization must be explicit')
        return state

    def save(self, state):
        encrypted = self.cipher.encrypt(json.dumps(state, ensure_ascii=False).encode())
        body = {'message': 'Record encrypted monitor checkpoint', 'sha': self.sha,
                'content': base64.b64encode(encrypted).decode(), 'branch': 'main'}
        result = self.request('PUT', body)
        self.sha = result['content']['sha']


class CliFailure(RuntimeError):
    pass


def cli(args):
    result = subprocess.run(['lark-cli', 'im', *args, '--as', 'bot', '--json'],
                            capture_output=True, text=True, timeout=50)
    if result.returncode:
        # Raw errors may contain group identifiers or message contents.
        raise CliFailure(json.dumps({'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}))
    data = json.loads(result.stdout)
    if data.get('ok') is not True:
        raise RuntimeError('Lark CLI returned an unsuccessful result')
    return data.get('data', data)


def find_message(value, message_id=None):
    if isinstance(value, dict):
        if value.get('message_id') and (message_id is None or value['message_id'] == message_id):
            return value
        for item in value.values():
            found = find_message(item, message_id)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = find_message(item, message_id)
            if found:
                return found
    return None


def deliver(state, save, call=cli):
    chat = os.environ['LARK_CHAT_ID']
    delivered = 0
    for key, item in list(state['pending'].items()):
        if item['phase'] != 'sent':
            if item['phase'] == 'attempting' and (timestamp(now()) - timestamp(item['attemptedAt'])).total_seconds() > 3300:
                raise RuntimeError('Uncertain delivery exceeded safe dedup window; manual readback required')
            args = ['+messages-send', '--chat-id', chat, '--markdown', item['body'], '--idempotency-key', key]
            call(args + ['--dry-run'])
            if item['phase'] == 'prepared':
                item.update(phase='attempting', attemptedAt=now())
                save(state)  # Never send if durable pending checkpoint fails.
            result = call(args)
            message = find_message(result)
            if not message or not message.get('message_id'):
                raise RuntimeError('Send result missing message ID; retain pending')
            item.update(phase='sent', messageId=message['message_id'])
            save(state)  # Persist receipt before any readback.
        result = call(['+messages-mget', '--message-ids', item['messageId'], '--no-reactions'])
        message = find_message(result, item['messageId'])
        if not message:
            raise RuntimeError('Receipt readback missing; do not resend')
        if message.get('chat_id') and message['chat_id'] != chat:
            raise RuntimeError('Receipt target mismatch')
        state['sent'][key] = {'messageId': item['messageId'], 'verifiedAt': now()}
        del state['pending'][key]
        save(state)
        delivered += 1
    return delivered


def main():
    remote = RemoteState()
    state = remote.load()
    before = copy.deepcopy(state)
    events = fetch_events()
    added = discover(state, events)
    state['lastCheckedDay'] = now()[:10]
    if state != before:
        remote.save(state)
    try:
        delivered = deliver(state, remote.save)
    except CliFailure as error:
        state['diagnostic'] = str(error)
        remote.save(state)  # Diagnostics stay encrypted, never in public logs.
        raise
    print(f'Checked {len(events)} events; queued {added}; verified deliveries {delivered}.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never print credential-bearing request objects, response bodies, or state.
        print(f'Monitor failed safely ({type(error).__name__}). Check pending state privately.', file=sys.stderr)
        sys.exit(1)
