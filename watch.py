"""EDD 網站更新追蹤機器人

定時檢查個人網站，有變動就發到 Discord 頻道，並把每次的更新記錄在 UPDATES.md。

追蹤三種內容（跟網站本身顯示的一樣）：
  1. 網頁上的文字（關於、作品、販售區、聯絡…各區塊）
  2. 「精選作品」：網站從 GitHub 自動抓的公開專案
  3. 「FiveM 插件販售」：網站從 Tebex 自動抓的商品

只用 Python 標準函式庫，不需要安裝任何套件。

環境變數：
  DISCORD_WEBHOOK_URL   Discord 頻道的 Webhook 網址（必填，除非用 --dry-run）
  SITE_URL              要追蹤的網站（預設 https://baby0900343376-collab.github.io/）
  GITHUB_TOKEN          （選填）GitHub Actions 會自動提供，用來提高 API 次數上限
"""
import argparse
import datetime as dt
import difflib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo('Asia/Taipei')
except Exception:  # 沒有時區資料時退回 UTC+8
    TZ = dt.timezone(dt.timedelta(hours=8))

SITE_URL = os.environ.get('SITE_URL', 'https://baby0900343376-collab.github.io/')
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, 'state')
SNAPSHOT = os.path.join(STATE_DIR, 'snapshot.json')
LOG_JSONL = os.path.join(STATE_DIR, 'updates.jsonl')
HEARTBEAT = os.path.join(STATE_DIR, 'heartbeat.txt')
HISTORY_MD = os.path.join(ROOT, 'UPDATES.md')

EMBED_COLOR = 0xFF8A3D          # 網站的主色
SECTION_NAMES = {
    'hero': '首頁開頭', 'about': '關於', 'work': '作品', 'store': '販售區',
    'contact': '聯絡', 'footer': '頁尾',
}
# 這些區塊的內容由 JavaScript 從 GitHub／Tebex 換掉，改由專門的追蹤處理
SKIP_IDS = {'projects', 'projects-note', 'store-grid', 'store-more'}
USER_AGENT = 'EDD-site-update-bot/1.0 (+https://github.com)'


# ---------------------------------------------------------------- 網路
def http_get(url, headers=None, tries=3):
    h = {'User-Agent': USER_AGENT, 'Cache-Control': 'no-cache'}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f'讀取失敗 {url}：{last}')


def http_get_json(url, headers=None):
    return json.loads(http_get(url, headers).decode('utf-8'))


# ---------------------------------------------------------------- 網頁文字
BLOCK_TAGS = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'div', 'section', 'header', 'footer',
              'nav', 'article', 'ul', 'ol', 'br', 'tr', 'td', 'th', 'blockquote', 'label', 'main'}
SKIP_TAGS = {'script', 'style', 'svg', 'template', 'noscript', 'head', 'title'}
VOID_TAGS = {'br', 'img', 'input', 'meta', 'link', 'hr', 'source', 'wbr', 'area', 'base', 'col', 'embed', 'param', 'track'}


class PageText(HTMLParser):
    """把網頁拆成「區塊 → 一行行文字」。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # (tag, skip, section_name or None)
        self.sections = {}
        self.order = []
        self.buf = []

    def _section(self):
        for tag, skip, sec in reversed(self.stack):
            if sec:
                return sec
        return None

    def _skipping(self):
        return any(skip for _, skip, _ in self.stack)

    def _flush(self):
        text = re.sub(r'\s+', ' ', ''.join(self.buf)).strip()
        self.buf = []
        sec = self._section()
        # 只有表情符號、箭頭之類的裝飾不算內容
        if not text or not sec or not re.search(r'[\w぀-ヿ㐀-鿿]', text):
            return
        lines = self.sections.setdefault(sec, [])
        if sec not in self.order:
            self.order.append(sec)
        if text not in lines:
            lines.append(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in BLOCK_TAGS:
            self._flush()
        elif not self._skipping():
            self.buf.append(' ')
        if tag in VOID_TAGS:
            return
        skip = (tag in SKIP_TAGS or a.get('aria-hidden') == 'true' or 'hidden' in a
                or a.get('id') in SKIP_IDS)
        sec = None
        if tag == 'section':
            sec = a.get('id') or (a.get('class') or 'section').split()[0]
        elif tag == 'footer':
            sec = 'footer'
        elif tag in ('header', 'nav'):
            skip = True      # 導覽列不追蹤
        self.stack.append((tag, skip, sec))

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if tag in BLOCK_TAGS:
            self._flush()
        # 找到對應的開始標籤（容忍沒關好的 HTML）
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self._skipping():
            self.buf.append(data)


def parse_page(raw_html):
    p = PageText()
    p.feed(raw_html)
    p.close()
    sections = {k: p.sections[k] for k in p.order}

    # 讀出網站自己的 GitHub／Tebex 設定，跟網站顯示的內容保持一致
    cfg = {'gh_user': None, 'gh_exclude': [], 'gh_titles': {}, 'gh_extra': [], 'tebex_token': None}
    m = re.search(r'const\s+GH\s*=\s*\{(.*?)\n\};', raw_html, re.S)
    if m:
        block = m.group(1)
        u = re.search(r"user:\s*'([^']+)'", block)
        cfg['gh_user'] = u and u.group(1)
        ex = re.search(r'exclude:\s*\[([^\]]*)\]', block)
        if ex:
            cfg['gh_exclude'] = re.findall(r"'([^']+)'", ex.group(1))
        extra = re.search(r'extra:\s*\[([^\]]*)\]', block)
        if extra:
            cfg['gh_extra'] = re.findall(r"'([^']+)'", extra.group(1))
        ti = re.search(r'titles:\s*\{(.*?)\}', block, re.S)
        if ti:
            cfg['gh_titles'] = dict(re.findall(r"'([^']+)'\s*:\s*'([^']+)'", ti.group(1)))
    m = re.search(r'const\s+TEBEX\s*=\s*\{(.*?)\n\};', raw_html, re.S)
    if m:
        t = re.search(r"token:\s*'([^']*)'", m.group(1))
        cfg['tebex_token'] = (t and t.group(1)) or None
    return sections, cfg


# ---------------------------------------------------------------- GitHub 作品
def repo_title(name, titles):
    return titles.get(name) or re.sub(r'\b\w', lambda m: m.group(0).upper(), re.sub(r'[-_]+', ' ', name))


def fetch_projects(cfg):
    user = cfg['gh_user']
    if not user:
        return None
    headers = {'Accept': 'application/vnd.github+json'}
    if os.environ.get('GITHUB_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    repos = http_get_json(f'https://api.github.com/users/{user}/repos?per_page=100&sort=pushed', headers)
    for full in cfg['gh_extra']:
        try:
            repos.append(http_get_json(f'https://api.github.com/repos/{full}', headers))
        except Exception:
            pass
    out = {}
    for r in repos:
        if r.get('fork') or r.get('archived') or r['name'] in cfg['gh_exclude']:
            continue
        out[r['name']] = {
            'title': repo_title(r['name'], cfg['gh_titles']),
            'description': r.get('description') or '',
            'tags': [t for t in [r.get('language')] + (r.get('topics') or []) if t][:6],
            'url': r.get('html_url'),
            'updated': (r.get('pushed_at') or '')[:10],
        }
    return out


# ---------------------------------------------------------------- Tebex 商店
def strip_html(s):
    s = re.sub(r'<[^>]+>', ' ', s or '')
    s = html.unescape(s)
    # 商品介紹有時是把 HTML 原始碼當文字貼上，再剝一次
    if re.search(r'</?(p|h\d|ul|li|strong|hr)\b', s, re.I):
        s = html.unescape(re.sub(r'<[^>]+>', ' ', s))
    return re.sub(r'\s+', ' ', s).strip()


def fetch_store(cfg):
    token = cfg['tebex_token']
    if not token:
        return None
    api = 'https://headless.tebex.io/api/accounts/' + urllib.request.quote(token)
    pkgs = http_get_json(api + '/packages').get('data') or []
    store_url = ''
    try:
        store_url = (http_get_json(api).get('data') or {}).get('webstore_url') or ''
    except Exception:
        pass
    out = {}
    for p in pkgs:
        total = float(p.get('total_price') if p.get('total_price') is not None else p.get('base_price') or 0)
        cur = p.get('currency') or 'USD'
        desc = strip_html(p.get('description'))
        out[str(p['id'])] = {
            'name': p.get('name') or '',
            'price': '免費' if not total else f'{total:.2f} {cur}',
            'description': desc[:160],
            'url': f"{store_url.rstrip('/')}/package/{p['id']}" if store_url else '',
        }
    return out


# ---------------------------------------------------------------- 比對
def short(s, n=180):
    s = s.strip()
    return s if len(s) <= n else s[:n - 1] + '…'


def diff_sections(old, new):
    events = []
    for sec in list(new) + [s for s in old if s not in new]:
        a, b = old.get(sec, []), new.get(sec, [])
        if a == b:
            continue
        name = SECTION_NAMES.get(sec, sec)
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == 'equal':
                continue
            olds, news = a[i1:i2], b[j1:j2]
            # 成對的段落視為「修改」
            while olds and news and op == 'replace':
                o, n = olds.pop(0), news.pop(0)
                events.append({'cat': 'page', 'kind': 'edit', 'where': name, 'old': o, 'new': n})
            for n in news:
                events.append({'cat': 'page', 'kind': 'add', 'where': name, 'new': n})
            for o in olds:
                events.append({'cat': 'page', 'kind': 'remove', 'where': name, 'old': o})
    return events


def diff_items(cat, old, new, fields, update_field=None):
    events = []
    for key, n in new.items():
        o = old.get(key)
        if o is None:
            events.append({'cat': cat, 'kind': 'add', 'item': n})
            continue
        changed = [f for f in fields if o.get(f) != n.get(f)]
        if changed:
            events.append({'cat': cat, 'kind': 'edit', 'item': n, 'old': o, 'fields': changed})
        elif update_field and o.get(update_field) != n.get(update_field):
            events.append({'cat': cat, 'kind': 'bump', 'item': n})
    for key, o in old.items():
        if key not in new:
            events.append({'cat': cat, 'kind': 'remove', 'item': o})
    return events


FIELD_NAMES = {'title': '名稱', 'name': '名稱', 'description': '介紹', 'tags': '標籤', 'price': '價格'}


def describe(ev):
    """把一筆變動轉成給人看的一行（Discord markdown）。"""
    c, k = ev['cat'], ev['kind']
    if c == 'page':
        w = ev['where']
        if k == 'add':
            return f"➕ **{w}** 新增：{short(ev['new'])}"
        if k == 'remove':
            return f"➖ **{w}** 移除：~~{short(ev['old'], 120)}~~"
        return f"✏️ **{w}** 修改：{short(ev['new'])}\n　　（原本：{short(ev['old'], 100)}）"
    it = ev['item']
    if c == 'project':
        link = f"[{it['title']}]({it['url']})" if it.get('url') else it['title']
        if k == 'add':
            d = f"：{short(it['description'], 140)}" if it['description'] else ''
            return f"🆕 新作品 {link}{d}"
        if k == 'remove':
            return f"🗑️ 作品下架：{it['title']}"
        if k == 'bump':
            return f"🔄 作品 {link} 有新的更新（{it['updated'].replace('-', '/')}）"
        parts = []
        for f in ev['fields']:
            nv = ', '.join(it[f]) if f == 'tags' else it[f]
            parts.append(f"{FIELD_NAMES.get(f, f)} → {short(str(nv), 140) or '（空白）'}")
        return f"✏️ 作品 {link} 更新了 " + '；'.join(parts)
    if c == 'store':
        link = f"[{it['name']}]({it['url']})" if it.get('url') else it['name']
        if k == 'add':
            return f"🛒 新商品上架 {link}（{it['price']}）" + (f"：{short(it['description'], 120)}" if it['description'] else '')
        if k == 'remove':
            return f"🗑️ 商品下架：{it['name']}"
        parts = []
        for f in ev['fields']:
            if f == 'price':
                parts.append(f"價格 {ev['old']['price']} → **{it['price']}**")
            else:
                parts.append(f"{FIELD_NAMES.get(f, f)} → {short(str(it[f]), 140) or '（空白）'}")
        return f"✏️ 商品 {link} 更新了 " + '；'.join(parts)
    return json.dumps(ev, ensure_ascii=False)


CATEGORY_TITLES = {'page': '📄 網頁內容', 'project': '🧰 精選作品', 'store': '🛍️ 插件販售'}

# 卡片左邊的顏色條：新增＝綠、移除＝紅、修改＝黃、有新更新＝藍
KIND_COLORS = {'add': 0x57F287, 'remove': 0xED4245, 'edit': 0xFEE75C, 'bump': 0x5865F2, 'info': 0xFF8A3D}
CATEGORY_LABELS = {'page': '網頁內容', 'project': '精選作品', 'store': '插件販售'}


def field(name, value, inline=True):
    value = (value or '').strip() or '—'
    return {'name': name, 'value': value[:1024], 'inline': inline}


def event_card(ev):
    """一項變動 → 一張 Discord 卡片（仿照語音頻道進出通知的樣式）。"""
    c, k = ev['cat'], ev['kind']
    fields, url = [], SITE_URL
    if c == 'page':
        w = ev['where']
        anchor = next((sid for sid, n in SECTION_NAMES.items() if n == w), '')
        url = SITE_URL.rstrip('/') + '/#' + anchor if anchor and anchor not in ('hero', 'footer') else SITE_URL
        desc = {'add': f'**{w}** 區塊新增了內容。',
                'remove': f'**{w}** 區塊移除了內容。',
                'edit': f'**{w}** 區塊的內容被修改了。'}[k]
        fields.append(field('區塊', w))
        fields.append(field('變動', {'add': '新增', 'remove': '移除', 'edit': '修改'}[k]))
        if k in ('add', 'edit'):
            fields.append(field('內容', short(ev['new'], 900), False))
        if k in ('remove', 'edit'):
            fields.append(field('原本', short(ev['old'], 900), False))
    elif c == 'project':
        it = ev['item']
        url = it.get('url') or SITE_URL
        name = it['title']
        if k == 'add':
            desc = f'新增了作品 **{name}**。'
        elif k == 'remove':
            desc = f'作品 **{name}** 已從網站移除。'
        elif k == 'bump':
            desc = f'作品 **{name}** 有新的更新。'
        else:
            desc = f'作品 **{name}** 的資訊更新了。'
        fields.append(field('作品', name))
        fields.append(field('最後更新', (it.get('updated') or '').replace('-', '/')))
        if it.get('tags'):
            fields.append(field('標籤', ', '.join(it['tags'])))
        if k == 'edit':
            for f in ev['fields']:
                ov, nv = ev['old'].get(f), it.get(f)
                if f == 'tags':
                    ov, nv = ', '.join(ov or []), ', '.join(nv or [])
                fields.append(field(f'{FIELD_NAMES.get(f, f)}（新）', short(str(nv or ''), 500), False))
                fields.append(field(f'{FIELD_NAMES.get(f, f)}（原本）', short(str(ov or ''), 300), False))
        elif k in ('add', 'bump') and it.get('description'):
            fields.append(field('介紹', short(it['description'], 500), False))
    else:  # store
        it = ev['item']
        url = it.get('url') or SITE_URL.rstrip('/') + '/#store'
        name = it['name']
        if k == 'add':
            desc = f'新商品 **{name}** 上架了！'
        elif k == 'remove':
            desc = f'商品 **{name}** 已下架。'
        else:
            desc = f'商品 **{name}** 的資訊更新了。'
        fields.append(field('商品', name))
        if k == 'edit' and 'price' in ev['fields']:
            fields.append(field('價格', f"~~{ev['old']['price']}~~ → **{it['price']}**"))
        else:
            fields.append(field('價格', it['price']))
        if k == 'edit':
            for f in ev['fields']:
                if f == 'price':
                    continue
                fields.append(field(f'{FIELD_NAMES.get(f, f)}（新）', short(str(it.get(f) or ''), 500), False))
        elif k == 'add' and it.get('description'):
            fields.append(field('介紹', short(it['description'], 500), False))
    return {'kind': k, 'cat': c, 'description': desc, 'fields': fields[:25], 'url': url}


def card_embed(card, now, author):
    return {
        'author': author,
        'description': card['description'],
        'url': card['url'],
        'color': KIND_COLORS.get(card['kind'], KIND_COLORS['info']),
        'fields': card['fields'],
        'footer': {'text': CATEGORY_LABELS.get(card['cat'], '網站更新')},
        'timestamp': now.astimezone(dt.timezone.utc).isoformat(),
    }


def embed_size(e):
    n = len(e.get('description', '')) + len(e.get('footer', {}).get('text', '')) + len((e.get('author') or {}).get('name', ''))
    return n + sum(len(f['name']) + len(f['value']) for f in e.get('fields', []))


def build_card_payloads(cards, now, author):
    """每則訊息最多 10 張卡片、總長 6000 字（Discord 的上限）。"""
    payloads, cur, size = [], [], 0
    for card in cards:
        e = card_embed(card, now, author)
        s = embed_size(e)
        if cur and (len(cur) == 10 or size + s > 5800):
            payloads.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += s
    if cur:
        payloads.append(cur)
    return [{'username': 'EDD 網站更新', 'avatar_url': author.get('icon_url'),
             'embeds': p, 'allowed_mentions': {'parse': []}} for p in payloads]


def make_author(cfg):
    user = (cfg or {}).get('gh_user') or 'baby0900343376-collab'
    return {'name': os.environ.get('AUTHOR_NAME', 'EDD'), 'url': SITE_URL,
            'icon_url': os.environ.get('AVATAR_URL', f'https://github.com/{user}.png')}


def info_card(text, fields=None):
    return {'kind': 'info', 'cat': 'info', 'description': text, 'fields': fields or [], 'url': SITE_URL}


DEMO_EVENTS = [
    {'cat': 'page', 'kind': 'edit', 'where': '關於', 'old': '喜歡寫寫 Python、研究 Cisco 網路架構。',
     'new': '喜歡寫寫 Python 和 Lua、研究 Cisco 網路架構。'},
    {'cat': 'page', 'kind': 'add', 'where': '關於', 'new': '幫 ROB RUN 做了繁體中文化，歡迎下載。'},
    {'cat': 'project', 'kind': 'add', 'item': {'title': 'Robrun Zh Tw', 'description': 'ROB RUN 繁體中文化補丁產生器',
     'tags': ['Python', 'PowerShell'], 'url': 'https://github.com/baby0900343376-collab/robrun-zh-tw', 'updated': '2026-11-18'}},
    {'cat': 'project', 'kind': 'bump', 'item': {'title': 'EDD Set Callsign', 'description': '', 'tags': ['Lua', 'fivem'],
     'url': 'https://github.com/baby0900343376-collab', 'updated': '2026-11-18'}},
    {'cat': 'store', 'kind': 'edit', 'fields': ['price'], 'old': {'price': '12.50 USD'},
     'item': {'name': 'EDD Set Callsign', 'price': '9.99 USD', 'description': '', 'url': ''}},
    {'cat': 'store', 'kind': 'remove', 'item': {'name': 'EDD Old Script', 'price': '5.00 USD', 'description': '', 'url': ''}},
]


# ---------------------------------------------------------------- Discord
def send_discord(payloads, dry_run):
    url = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    for p in payloads:
        if dry_run or not url:
            print('---- Discord 訊息（未送出）----')
            print(json.dumps(p, ensure_ascii=False, indent=1))
            continue
        data = json.dumps(p).encode('utf-8')
        for attempt in range(5):
            req = urllib.request.Request(url, data=data, method='POST',
                                         headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT})
            try:
                urllib.request.urlopen(req, timeout=30).read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:   # 被限速，照 Discord 說的時間等
                    try:
                        wait = float(json.loads(e.read().decode()).get('retry_after', 2))
                    except Exception:
                        wait = 2
                    time.sleep(min(wait, 30))
                    continue
                raise RuntimeError(f'Discord 拒絕訊息（HTTP {e.code}）：{e.read().decode(errors="replace")[:300]}')
        time.sleep(1)


# ---------------------------------------------------------------- 記錄
def append_history(now, title, groups):
    stamp = now.strftime('%Y-%m-%d %H:%M')
    lines = [f'## {stamp}（台北時間）— {title}', '']
    for head, items in groups:
        lines.append(f'**{head}**')
        lines.append('')
        for it in items:
            lines.append('- ' + it.replace('\n　　', '  \n  '))
        lines.append('')
    header = '# EDD 網站更新記錄\n\n> 由 [watch.py](watch.py) 自動產生，最新的在最上面。\n\n'
    old = ''
    if os.path.exists(HISTORY_MD):
        old = open(HISTORY_MD, encoding='utf-8').read()
        if old.startswith(header):
            old = old[len(header):]
    with open(HISTORY_MD, 'w', encoding='utf-8') as f:
        f.write(header + '\n'.join(lines) + '\n' + old)


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 主程式
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='只印出訊息，不送到 Discord')
    ap.add_argument('--fixtures', help='（測試用）從資料夾讀 page.html / repos.json / tebex.json')
    ap.add_argument('--test-message', action='store_true', help='送一則測試訊息到 Discord')
    ap.add_argument('--demo', action='store_true', help='送幾張範例卡片到 Discord，預覽通知的樣子')
    a = ap.parse_args()
    now = dt.datetime.now(TZ)

    if a.test_message:
        # 測試訊息順便附上範例卡片，讓你看到真正通知的樣子
        cards = [info_card('✅ 連線正常！機器人可以發訊息到這個頻道了。下面是通知的範例（不是真的更新）：',
                           [field('追蹤網站', SITE_URL, False)])] + [event_card(e) for e in DEMO_EVENTS]
        send_discord(build_card_payloads(cards, now, make_author(None)), a.dry_run)
        return
    if a.demo:
        cards = [info_card('👀 以下是範例通知（不是真的更新）')] + [event_card(e) for e in DEMO_EVENTS]
        send_discord(build_card_payloads(cards, now, make_author(None)), a.dry_run)
        return

    if not a.dry_run and not os.environ.get('DISCORD_WEBHOOK_URL'):
        sys.exit('缺少 DISCORD_WEBHOOK_URL（請在 GitHub 的 Settings → Secrets and variables → Actions 新增）')

    # 1. 抓資料；某個來源失敗時沿用上次的資料，避免誤報「下架」
    prev = load_json(SNAPSHOT, None)
    warnings = []
    if a.fixtures:
        raw = open(os.path.join(a.fixtures, 'page.html'), encoding='utf-8').read()
    else:
        raw = http_get(SITE_URL + ('&' if '?' in SITE_URL else '?') + f'_={int(time.time())}').decode('utf-8', 'replace')
    sections, cfg = parse_page(raw)
    author = make_author(cfg)
    if not sections:
        sys.exit('網頁讀到了，但解析不出任何內容，先不更新記錄。')

    def from_fixture(name):
        p = os.path.join(a.fixtures, name)
        return json.load(open(p, encoding='utf-8')) if os.path.exists(p) else None

    try:
        if a.fixtures:
            fake = from_fixture('repos.json')
            projects = None if fake is None else {
                r['name']: {'title': repo_title(r['name'], cfg['gh_titles']), 'description': r.get('description') or '',
                            'tags': [t for t in [r.get('language')] + (r.get('topics') or []) if t][:6],
                            'url': r.get('html_url'), 'updated': (r.get('pushed_at') or '')[:10]}
                for r in fake if not r.get('fork') and not r.get('archived') and r['name'] not in cfg['gh_exclude']}
        else:
            projects = fetch_projects(cfg)
    except Exception as e:
        projects = None
        warnings.append(f'GitHub 作品讀取失敗：{e}')
    try:
        if a.fixtures:
            fake = from_fixture('tebex.json')
            store = None if fake is None else {
                str(p['id']): {'name': p['name'], 'price': '免費' if not float(p.get('total_price') or 0) else f"{float(p['total_price']):.2f} {p.get('currency', 'USD')}",
                               'description': strip_html(p.get('description'))[:160], 'url': ''} for p in fake}
        else:
            store = fetch_store(cfg)
    except Exception as e:
        store = None
        warnings.append(f'Tebex 商品讀取失敗：{e}')
    for w in warnings:
        print('警告：' + w, file=sys.stderr)

    snap = {
        'sections': sections,
        'projects': projects if projects is not None else (prev or {}).get('projects'),
        'store': store if store is not None else (prev or {}).get('store'),
    }

    # 2. 第一次執行：建立基準，發一則「開始追蹤」
    if prev is None:
        summary = [f"網頁區塊：{len(sections)} 個",
                   f"精選作品：{len(snap['projects'] or {})} 個" + ('' if projects is not None else '（這次沒讀到，下次再試）'),
                   f"販售商品：{len(snap['store'] or {})} 個" + ('' if store is not None else '（這次沒讀到，下次再試）'),
                   '之後網站有任何更新，都會在這裡通知。']
        card = info_card('✅ 開始追蹤網站！之後有任何更新都會在這裡通知。', [
            field('網頁區塊', f'{len(sections)} 個'),
            field('精選作品', f"{len(snap['projects'] or {})} 個" if projects is not None else '這次沒讀到'),
            field('販售商品', f"{len(snap['store'] or {})} 個" if store is not None else '這次沒讀到')])
        send_discord(build_card_payloads([card], now, author), a.dry_run)
        if not a.dry_run:
            save_json(SNAPSHOT, snap)
            append_history(now, '開始追蹤', [('目前內容', summary[:3])])
        print('已建立基準快照。')
        write_heartbeat(now, a.dry_run)
        return

    # 3. 比對
    events = diff_sections(prev.get('sections', {}), sections)
    if projects is not None and prev.get('projects') is not None:
        events += diff_items('project', prev['projects'], projects, ['title', 'description', 'tags'], 'updated')
    if store is not None and prev.get('store') is not None:
        events += diff_items('store', prev['store'], store, ['name', 'price', 'description'])

    if not events:
        print('沒有變動。')
        if not a.dry_run:
            save_json(SNAPSHOT, snap)   # 補上之前沒讀到的來源
        write_heartbeat(now, a.dry_run)
        return

    groups = []
    for cat in ('page', 'project', 'store'):
        lines = [describe(e) for e in events if e['cat'] == cat]
        if lines:
            groups.append((CATEGORY_TITLES[cat], lines))
    title = f'🔔 網站有 {len(events)} 項更新'
    order = {'page': 0, 'project': 1, 'store': 2}
    cards = [event_card(e) for e in sorted(events, key=lambda e: order[e['cat']])]
    send_discord(build_card_payloads(cards, now, author), a.dry_run)
    if not a.dry_run:
        save_json(SNAPSHOT, snap)
        append_history(now, title, groups)
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_JSONL, 'a', encoding='utf-8') as f:
            for e in events:
                f.write(json.dumps({'time': now.isoformat(timespec='seconds'), **e}, ensure_ascii=False) + '\n')
    print(f'發現 {len(events)} 項更新，已通知 Discord。')
    write_heartbeat(now, a.dry_run)


def write_heartbeat(now, dry_run):
    """每週寫一次，讓 GitHub 不會因為 60 天沒動靜而停用排程。"""
    if dry_run:
        return
    week = '%d-W%02d' % now.isocalendar()[:2]
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(HEARTBEAT) or open(HEARTBEAT).read().strip() != week:
        with open(HEARTBEAT, 'w') as f:
            f.write(week + '\n')


if __name__ == '__main__':
    main()
