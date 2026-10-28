#!/usr/bin/env python3
"""Страница разбора реплеев DoW2 Retribution / Elite: https://rts.fintebtc.de/replay/
Загрузка .rec (сырые байты из браузера) → make_report.py → страница результатов с постоянной ссылкой.
По кнопке — история матча (Claude по выжимке фактов), с суточным лимитом.
Слушает 127.0.0.1:8215, nginx проксирует /replay/."""
import hashlib, html, json, os, re, sqlite3, subprocess, sys, threading, time, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
APP = os.path.dirname(os.path.abspath(__file__))            # /srv/replayweb/app — код и парсер, только чтение
ROOT = os.path.dirname(APP); STORE = ROOT + '/data'; QUEUE = ROOT + '/queue'; PORT = 8215
STORY_LIMIT = int(os.environ.get('STORY_LIMIT', 20)); story_today = {'day': '', 'n': 0}; running = set()
UPLOADS_PER_HOUR = 20; STORE_MAX = 3 * 1024**3; ip_hits = {}   # с одного адреса 20 загрузок в час; хранилище до 3 ГБ
def client_ip(h): return h.headers.get('CF-Connecting-IP') or h.headers.get('X-Real-IP') or h.client_address[0]
def allow_upload(ip):
    now = time.time(); hits = [t for t in ip_hits.get(ip, []) if now - t < 3600]
    if len(hits) >= UPLOADS_PER_HOUR: ip_hits[ip] = hits; return False
    hits.append(now); ip_hits[ip] = hits; return True
def store_size():
    return sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(STORE) for f in fs)
os.makedirs(STORE, exist_ok=True)

# ---------- индекс реплеев: один матч — один реплей ----------
# После матча реплей присылает каждый участник (MMFind из игры). Файлы у всех разные (шапка), но поток команд один и тот же:
# ключ матча = карта + profile_id игроков + первые 200 команд. Храним одну копию — самую длинную (кто вышел раньше, у того обрезана).
sys.path.insert(0, APP); import dow2rec
IDX = STORE + '/index.sqlite'; _ilock = threading.Lock()
def idb():
    c = sqlite3.connect(IDX, timeout=30); c.execute('PRAGMA journal_mode=WAL'); return c
with idb() as c:
    c.executescript('''CREATE TABLE IF NOT EXISTS replays (rid TEXT PRIMARY KEY, mkey TEXT UNIQUE, name TEXT, lobby TEXT, map TEXT,
        players TEXT, ticks INTEGER, recorded TEXT, uploaded INTEGER, size INTEGER, uploads INTEGER DEFAULT 1);
        CREATE INDEX IF NOT EXISTS replays_up ON replays (uploaded DESC); CREATE INDEX IF NOT EXISTS replays_lobby ON replays (lobby);''')
def rec_meta(path):
    meta, acts = dow2rec.parse(path)
    pl = [{'name': p.get('name'), 'team': p.get('team'), 'race': p.get('race'), 'hero': p.get('hero'), 'pid': p.get('profile_id'),
           'level': p.get('army_level')} for p in meta.get('players', []) if p.get('race')]
    mp = (meta.get('map') or '').replace('/', '\\').split('\\')[-1].lower()
    h = hashlib.sha256((mp + '|' + ','.join(str(x) for x in sorted(p['pid'] or 0 for p in pl))).encode())
    for a in acts[:200]: h.update(a[3])
    return {'map': mp, 'players': pl, 'ticks': meta.get('ticks') or 0, 'recorded': meta.get('recorded'), 'mkey': h.hexdigest()[:32] if acts else None}
def index_add(rid, meta, name, lobby, size):
    with _ilock, idb() as c:
        c.execute('INSERT OR IGNORE INTO replays (rid, mkey, name, lobby, map, players, ticks, recorded, uploaded, size) VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (rid, meta['mkey'], name, lobby or None, meta['map'], json.dumps(meta['players'], ensure_ascii=False), meta['ticks'],
                   meta['recorded'], int(time.time()), size))
def backfill():
    with idb() as c: known = {r[0] for r in c.execute('SELECT rid FROM replays')}
    for rid in os.listdir(STORE):
        f = f'{STORE}/{rid}/replay.rec'
        if rid in known or not re.fullmatch(r'[0-9a-f]{16}', rid) or not os.path.exists(f): continue
        try:
            meta = rec_meta(f); nm = open(f'{STORE}/{rid}/name.txt').read() if os.path.exists(f'{STORE}/{rid}/name.txt') else 'replay.rec'
            if meta['mkey'] is None: meta['mkey'] = 'file:' + rid
            index_add(rid, meta, nm, None, os.path.getsize(f))
            with _ilock, idb() as c: c.execute('UPDATE replays SET uploaded = ? WHERE rid = ?', (int(os.path.getmtime(f)), rid))
        except Exception: pass
backfill()
RACES = ['chaos_space_marines', 'eldar', 'imperial_guard', 'ordo_malleus', 'orks', 'space_marines', 'tau', 'tyranids', 'necron']
def replay_list(q='', mapk='', race='', mode='', page=0, n=30):
    with idb() as c:
        rows = c.execute('SELECT rid, name, lobby, map, players, ticks, recorded, uploaded, uploads FROM replays ORDER BY uploaded DESC LIMIT 5000').fetchall()
    q = (q or '').strip().lower(); out = []
    for rid, name, lobby, mp, pl, ticks, rec, up, ups in rows:
        pl = json.loads(pl or '[]'); teams = {p.get('team') for p in pl}; per = max(1, len(pl) // max(1, len(teams)))
        md = '%dv%d' % (per, per) if len(teams) == 2 else ('ffa' if len(teams) > 2 else '?')
        if mapk and mp != mapk.lower(): continue
        if race and not any(race in (p.get('race') or '') for p in pl): continue
        if mode and md != mode: continue
        if q and q not in ' '.join([name or '', lobby or '', mp or ''] + [p.get('name') or '' for p in pl]).lower(): continue
        cap = f'{STORE}/{rid}/caption.txt'
        out.append({'id': rid, 'name': name, 'lobby': lobby, 'map': mp, 'mode': md, 'players': pl, 'duration': (ticks or 0) // 10,
                    'recorded': rec, 'uploaded': up, 'uploads': ups, 'caption': open(cap).read() if os.path.exists(cap) else None})
    page = max(0, int(page or 0)); n = max(1, min(100, int(n or 30)))
    return {'total': len(out), 'page': page, 'items': out[page * n:(page + 1) * n], 'maps': sorted({i['map'] for i in out if i['map']})}

def replay_meta(rid):
    with idb() as c:
        r = c.execute('SELECT lobby, map, players, ticks, recorded, uploaded, uploads FROM replays WHERE rid = ?', (rid,)).fetchone()
    if not r: return None
    return {'lobby': r[0], 'map': r[1], 'players': json.loads(r[2] or '[]'), 'duration': (r[3] or 0) // 10, 'recorded': r[4], 'uploaded': r[5], 'uploads': r[6]}

PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'page.html'), encoding='utf-8').read()

# перевод отчёта make_report.py на английский: фиксированные фразы + направления на карте
EN = [
 ('Разбежка (первые 90 секунд)', 'Opening (first 90 seconds)'), ('Заказы и тиры', 'Orders and tiers'),
 ('Контроль карты (доля кликов на чужой половине)', 'Map control (share of clicks on the enemy half)'),
 ('Вероятные потери (отряд исчез из лога)', 'Probable losses (squad disappears from the log)'),
 ('Захваты (приказы по точкам)', 'Captures (capture orders by point)'),
 ('Конец: маркера сдачи нет → **победа по очкам VP** (или обрыв записи) на', 'End: no surrender marker → **victory on VP points** (or the recording stopped) at'),
 ('Конец: **сдача/выход** в', 'End: **surrender/leave** at'), ('(маркер 0x61; отправитель ≠ сдавшийся)', '(marker 0x61; the sender is not necessarily the one who surrendered)'),
 ('Гипотеза о победителе (уверенность', 'Winner hypothesis (confidence'), ('Признаки:', 'Signs:'),
 ('ретриты в концовке', 'retreats in the endgame'), ('потери в концовке', 'losses in the endgame'),
 ('вероятный победитель', 'likely winner'), ('старт.отряд/герой', 'starting squad/hero'),
 ('Отступили:', 'Retreated:'), ('активны:', 'active:'), ('боевых команд', 'combat orders'), ('и ещё', 'and'),
 ('| Игрок | Раса | Ур. | Команда | APM | Тир | Ретриты |', '| Player | Race | Lvl | Team | APM | Tier | Retreats |'),
 ('## Игроки', '## Players'), ('## Бои', '## Fights'), ('## Итог', '## Result'),
 ('записан', 'recorded'), ('данные:', 'data:'), ('тиры:', 'tiers:'), ('капы:', 'captures:'), ('заказы:', 'orders:'),
 ('высокая', 'high'), ('средняя', 'medium'), ('низкая', 'low'), ('команда', 'team'), ('Команда', 'Team'),
 ('начало', 'early'), ('середина', 'mid'), ('конец', 'late'), ('ГЕРОЙ', 'HERO'), ('отряд', 'squad'),
 ('тираниды', 'Tyranids'), ('эльдары', 'Eldar'), ('хаос', 'Chaos'), ('орки', 'Orks'), ('тау', 'Tau'),
 ('ИГ', 'Imperial Guard'), ('СМ', 'Space Marines'), ('СР', 'Grey Knights'),
 ('реквизиция', 'requisition'), ('энергоузел', 'power node'), ('точка', 'point'), ('база', 'base'), (' у ', ' near '),
 ('в (', 'at ('),
]
DIRS = {'СЗ': 'NW', 'СВ': 'NE', 'ЮЗ': 'SW', 'ЮВ': 'SE', 'С': 'N', 'Ю': 'S', 'З': 'W', 'В': 'E', 'центр': 'centre'}
def to_en(t):
    if not t: return t
    t = re.sub(r'(VP|энергоузел|реквизиция|база)-(СЗ|СВ|ЮЗ|ЮВ|центр|С|Ю|З|В)\b', lambda m: m.group(1) + '-' + DIRS[m.group(2)], t)
    for a, b in EN: t = t.replace(a, b)
    t = re.sub(r'(\d+)\s*с\b', r'\1 s', t)
    t = re.sub(r'Т(\d)', r'T\1', t)          # кириллическая «Т» в тирах
    return t

def analyse(rid):
    d = f'{STORE}/{rid}'
    env = dict(os.environ, DIGEST=f'{d}/digest.json')
    env['DOW_BASE'] = ROOT          # make_report пишет в ROOT/reports, таблицы читает из APP
    r = subprocess.run(['python3', APP + '/make_report.py', f'{d}/replay.rec'], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    src = f"{ROOT}/reports/replay.md"
    if os.path.exists(src):
        rep = open(src).read(); os.remove(src)
        nm = open(f'{d}/name.txt').read() if os.path.exists(f'{d}/name.txt') else 'replay.rec'
        open(f'{d}/report.md', 'w').write(re.sub(r'^# replay\.rec', '# ' + nm, rep, count=1))
        if os.path.exists(src + '.caption'):   # data и reports в песочнице — разные точки монтирования: rename нельзя
            open(f'{d}/caption.txt', 'w').write(open(src + '.caption').read()); os.remove(src + '.caption')
    else:
        open(f'{d}/error.txt', 'w').write((r.stderr or r.stdout)[-3000:] or 'не удалось разобрать')

def queue_story(rid, lang):
    # историю пишет отдельный процесс (replay_story_worker от root, Claude без инструментов): у этого сервиса
    # нет доступа ни к логину Claude, ни к ключам. Заявка — файл в очереди, результат — story_<lang>.md в папке разбора.
    open(f'{QUEUE}/{rid}_{lang}.req', 'w').write(json.dumps({'rid': rid, 'lang': lang, 'ts': time.time()}))

def story_running(rid):
    return [l for l in ('ru', 'en') if os.path.exists(f'{QUEUE}/{rid}_{l}.req') or os.path.exists(f'{QUEUE}/{rid}_{l}.work')]

def recent(n=15):
    items = []
    for rid in sorted(os.listdir(STORE), key=lambda x: -os.path.getmtime(f'{STORE}/{x}'))[:n]:
        cap = f'{STORE}/{rid}/caption.txt'
        if os.path.exists(cap): items.append({'id': rid, 'name': open(f'{STORE}/{rid}/name.txt').read() if os.path.exists(f'{STORE}/{rid}/name.txt') else rid,
                                              'caption': open(cap).read(), 'caption_en': to_en(open(cap).read())})
    return items

class H(BaseHTTPRequestHandler):
    def out(self, code, body, ctype='text/html; charset=utf-8'):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store'); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path.rstrip('/')
        if p in ('', '/replay'): return self.out(200, PAGE)
        if p == '/replay/api/recent': return self.out(200, json.dumps(recent(), ensure_ascii=False), 'application/json')
        if p == '/replay/api/list':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); g = lambda k: qs.get(k, [''])[0]
            try: return self.out(200, json.dumps(replay_list(g('q')[:64], g('map')[:64], g('race')[:32], g('mode')[:8], g('page') or 0, g('n') or 30), ensure_ascii=False), 'application/json')
            except ValueError: return self.out(400, '{}', 'application/json')
        m = re.match(r'^/replay/api/by_lobby/([\w.\-]{1,80})$', p)
        if m:
            with idb() as c: r = c.execute('SELECT rid FROM replays WHERE lobby = ? ORDER BY ticks DESC LIMIT 1', (m.group(1),)).fetchone()
            return self.out(200 if r else 404, json.dumps({'id': r[0]} if r else {}), 'application/json')
        m = re.match(r'^/replay/api/r/([0-9a-f]{16})/replay\.rec$', p)
        if m and os.path.exists(f'{STORE}/{m.group(1)}/replay.rec'):
            nm = open(f'{STORE}/{m.group(1)}/name.txt').read() if os.path.exists(f'{STORE}/{m.group(1)}/name.txt') else 'replay.rec'
            b = open(f'{STORE}/{m.group(1)}/replay.rec', 'rb').read()
            self.send_response(200); self.send_header('Content-Type', 'application/octet-stream'); self.send_header('Content-Length', str(len(b)))
            self.send_header('Content-Disposition', 'attachment; filename="%s"' % re.sub(r'[^\w.\-\[\]]', '_', nm)); self.end_headers(); self.wfile.write(b); return
        m = re.match(r'^/replay/api/r/([0-9a-f]{16})$', p)
        if m:
            d = f'{STORE}/{m.group(1)}'
            if not os.path.isdir(d): return self.out(404, '{}', 'application/json')
            rd = lambda f: open(f'{d}/{f}').read() if os.path.exists(f'{d}/{f}') else None
            return self.out(200, json.dumps({'name': rd('name.txt'), 'caption': rd('caption.txt'), 'report': rd('report.md'),
                'caption_en': to_en(rd('caption.txt')), 'report_en': to_en(rd('report.md')),
                'error': rd('error.txt'), 'story_ru': rd('story_ru.md'), 'story_en': rd('story_en.md'),
                'story_running': story_running(m.group(1)), 'meta': replay_meta(m.group(1))}, ensure_ascii=False), 'application/json')
        m = re.match(r'^/replay/r/([0-9a-f]{16})$', p)
        if m: return self.out(200, PAGE)
        m = re.match(r'^/replay/api/r/([0-9a-f]{16})/report(_en)?\.md$', p)
        if m and os.path.exists(f'{STORE}/{m.group(1)}/report.md'):
            t = open(f'{STORE}/{m.group(1)}/report.md').read()
            return self.out(200, to_en(t) if m.group(2) else t, 'text/markdown; charset=utf-8')
        self.out(404, 'not found', 'text/plain')
    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p == '/replay/api/upload':
            n = int(self.headers.get('Content-Length', 0))
            if n <= 0 or n > 30 * 1024 * 1024: return self.out(413, json.dumps({'error': 'file is empty or larger than 30 MB'}), 'application/json')
            ladder = (self.headers.get("X-Lobby") or "").startswith("MM-")   # sent by the game after a ladder match: several mirrors share one address
            if not ladder and not allow_upload(client_ip(self)): return self.out(429, json.dumps({'error': 'too many uploads, try again in an hour'}), 'application/json')
            if store_size() > STORE_MAX: return self.out(507, json.dumps({'error': 'storage is full, try later'}), 'application/json')
            data = self.rfile.read(n)
            if data[12:20] != b'DOW2_REC': return self.out(400, json.dumps({'error': 'not a Dawn of War II replay (.rec)'}), 'application/json')
            rid = hashlib.sha256(data).hexdigest()[:16]; d = f'{STORE}/{rid}'
            name = re.sub(r'[^\w.\-\[\] ]', '_', urllib.parse.unquote(self.headers.get('X-Filename', 'replay.rec')))[:120]
            lobby = re.sub(r'[^\w.\-]', '', self.headers.get('X-Lobby', ''))[:80]
            tmp = f'{STORE}/.up_{rid}.rec'; open(tmp, 'wb').write(data)
            try: meta = rec_meta(tmp)
            except Exception: meta = None
            if not meta or not meta['mkey']: meta = dict(meta or {'map': '', 'players': [], 'ticks': 0, 'recorded': None}, mkey='file:' + rid)
            with _ilock, idb() as c:                  # проверка и запись в индекс — одним шагом: двое шлют один матч одновременно
                old = c.execute('SELECT rid, ticks, lobby FROM replays WHERE mkey = ?', (meta['mkey'],)).fetchone()
                if not old:
                    c.execute('INSERT OR IGNORE INTO replays (rid, mkey, name, lobby, map, players, ticks, recorded, uploaded, size) VALUES (?,?,?,?,?,?,?,?,?,?)',
                              (rid, meta['mkey'], name, lobby or None, meta['map'], json.dumps(meta['players'], ensure_ascii=False), meta['ticks'],
                               meta['recorded'], int(time.time()), len(data)))
            if old:                                   # этот матч уже есть: копию не храним, более полную запись — подменяем
                rid0, t0, lb0 = old; d0 = f'{STORE}/{rid0}'
                with _ilock, idb() as c:
                    c.execute('UPDATE replays SET uploads = uploads + 1, lobby = COALESCE(lobby, ?) WHERE rid = ?', (lobby or None, rid0))
                    if meta['ticks'] > (t0 or 0): c.execute('UPDATE replays SET ticks = ?, size = ? WHERE rid = ?', (meta['ticks'], len(data), rid0))
                if meta['ticks'] > (t0 or 0):
                    os.replace(tmp, f'{d0}/replay.rec')
                    for f in ('report.md', 'caption.txt', 'error.txt', 'digest.json'):
                        if os.path.exists(f'{d0}/{f}'): os.remove(f'{d0}/{f}')
                    analyse(rid0)
                else: os.remove(tmp)
                return self.out(200, json.dumps({'id': rid0, 'duplicate': True}), 'application/json')
            if not os.path.exists(f'{d}/report.md'):
                os.makedirs(d, exist_ok=True); os.replace(tmp, f'{d}/replay.rec'); open(f'{d}/name.txt', 'w').write(name)
                analyse(rid)
            elif os.path.exists(tmp): os.remove(tmp)
            return self.out(200, json.dumps({'id': rid}), 'application/json')
        m = re.match(r'^/replay/api/r/([0-9a-f]{16})/story/(ru|en)$', p)
        if m:
            rid, lang = m.groups(); d = f'{STORE}/{rid}'
            if not os.path.exists(f'{d}/report.md'): return self.out(404, '{}', 'application/json')
            if os.path.exists(f'{d}/story_{lang}.md') or lang in story_running(rid): return self.out(200, '{"ok":true}', 'application/json')
            day = time.strftime('%F')
            if story_today['day'] != day: story_today.update(day=day, n=0)
            if story_today['n'] >= STORY_LIMIT: return self.out(429, json.dumps({'error': 'лимит историй на сегодня исчерпан'}), 'application/json')
            story_today['n'] += 1; queue_story(rid, lang)
            return self.out(200, '{"ok":true}', 'application/json')
        self.out(404, 'not found', 'text/plain')

if __name__ == '__main__':
    print('replay web на', PORT, flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
