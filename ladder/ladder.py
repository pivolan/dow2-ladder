#!/usr/bin/env python3
# DoW2 Elite ladder: собирает матчи из публичного Relic community API (title=dow2r),
# оставляет только матчи Elite, считает рейтинг Glicko-2 отдельно по режимам (1v1/2v2/3v3)
# и сезонам (квартал). Отдаёт JSON для страницы /ladder/. Данные только из Relic; реплеев Relic не хранит.
import random
import json, os, re, sqlite3, threading, time, math, base64, zlib, urllib.request, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
import stream, users                                # launcher users / presence, the live stream (SSE)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = ROOT + '/data/ladder.sqlite'
RELIC = 'https://dow-api.reliclink.com/community/leaderboard/'
UA = 'dow2-ladder/1.0 (community ladder; contact pivolan)'
PORT = 8217

# ---------- карты Elite (только в Elite, не в ванили): для признака матча Elite ----------
ELITE_MAPS, VAN_MAPS = set(), set()
def load_maps():
    global ELITE_MAPS, VAN_MAPS
    try:
        for line in open(os.path.dirname(os.path.abspath(__file__)) + '/maps.txt'):
            p = line.split()
            if p and p[0] == 'VAN': VAN_MAPS = set(p[1:])
            elif p and p[0] == 'ELITE_ONLY': ELITE_MAPS = set(p[1:])
    except Exception:
        pass
load_maps()

# ---------- БД ----------
_db_lock = threading.Lock()
def db():
    c = sqlite3.connect(DB, timeout=30); c.execute('PRAGMA journal_mode=WAL'); return c
with db() as c:
    c.executescript('''
    CREATE TABLE IF NOT EXISTS matches (id INTEGER PRIMARY KEY, start INTEGER, elite INTEGER, mode TEXT, season TEXT, data TEXT);
    CREATE INDEX IF NOT EXISTS matches_season ON matches (season, mode);
    CREATE TABLE IF NOT EXISTS members (profile_id INTEGER, match_id INTEGER, start INTEGER, PRIMARY KEY (profile_id, match_id));
    CREATE INDEX IF NOT EXISTS members_p ON members (profile_id, start);
    CREATE TABLE IF NOT EXISTS profiles (profile_id INTEGER PRIMARY KEY, alias TEXT, steam TEXT, country TEXT, elite_seen INTEGER DEFAULT 0, updated INTEGER);
    CREATE INDEX IF NOT EXISTS profiles_steam ON profiles (steam);
    -- рейтинг: по игроку, режиму и сезону (считается из matches, можно пересчитать с нуля)
    CREATE TABLE IF NOT EXISTS ratings (profile_id INTEGER, mode TEXT, season TEXT, rating REAL, rd REAL, vol REAL,
                                        games INTEGER, wins INTEGER, losses INTEGER, last INTEGER,
                                        PRIMARY KEY (profile_id, mode, season));
    CREATE INDEX IF NOT EXISTS ratings_board ON ratings (season, mode, rating DESC);
    -- фронтир обхода: кого ещё опросить у Relic
    CREATE TABLE IF NOT EXISTS frontier (profile_id INTEGER PRIMARY KEY, added INTEGER, fetched INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    ''')

# ---------- Relic ----------
_out = threading.Semaphore(3)
def relic(ep, **kw):
    q = urllib.parse.urlencode({'title': 'dow2r', **{k: json.dumps(v, separators=(',', ':')) for k, v in kw.items()}})
    with _out:
        req = urllib.request.Request(RELIC + ep + '?' + q, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.load(r)
    if data.get('result', {}).get('code') != 0:
        raise RuntimeError('Relic: ' + str(data.get('result')))
    return data

# ---------- классификация матча ----------
def season_of(start):
    t = time.gmtime(start); q = (t.tm_mon - 1) // 3 + 1
    return '%d-Q%d' % (t.tm_year, q)

def badges(m):
    try:
        raw = zlib.decompress(base64.b64decode(m.get('slotinfo', '')))
    except Exception:
        return set()
    return set(w.decode('latin1').lower() for w in re.findall(rb'[A-Za-z_][A-Za-z0-9_]{3,}', raw))

def mode_of(members):
    # команды равного размера: 2->1v1, 4->2v2, 6->3v3; остальное не рейтингуем
    teams = {}
    for x in members:
        teams.setdefault(x['teamid'], []).append(x)
    if len(teams) != 2:
        return None
    sizes = sorted(len(v) for v in teams.values())
    if sizes == [1, 1]: return '1v1'
    if sizes == [2, 2]: return '2v2'
    if sizes == [3, 3]: return '3v3'
    return None

# определённый Elite-признак (не зависит от знания об игроках).
# расы: 6 Тау и 7 Тираниды есть только в Elite; бейджи tau_/necron_/gk_/*elite; карты только из Elite.
def elite_sure(m):
    races = {x['race_id'] for x in m.get('matchhistorymember', [])}
    if races & {6, 7}: return True
    b = badges(m)
    if any('elite' in w or w.startswith(('tau_', 'necron', 'gk_')) for w in b): return True
    mn = (m.get('mapname') or '').lower()
    if mn in ELITE_MAPS and mn not in VAN_MAPS: return True
    return False

# ---------- Glicko-2 ----------
Q = math.log(10) / 400.0
TAU = 0.5
def _g(phi): return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))
def _E(mu, muj, phij): return 1.0 / (1.0 + math.exp(-_g(phij) * (mu - muj)))

def glicko2_update(r, rd, vol, opponents):
    # opponents: список (rating, rd, score 0/0.5/1). Один период = один матч (для команд — против средней команды соперника).
    mu = (r - 1500.0) / 173.7178
    phi = rd / 173.7178
    if not opponents:
        phi_star = math.sqrt(phi * phi + vol * vol)
        return 1500.0 + 173.7178 * mu, 173.7178 * phi_star, vol
    v_inv = 0.0; delta_sum = 0.0
    gs = []
    for (orat, ord_, s) in opponents:
        muj = (orat - 1500.0) / 173.7178
        phij = ord_ / 173.7178
        g = _g(phij); E = _E(mu, muj, phij)
        gs.append((g, E, s))
        v_inv += g * g * E * (1 - E)
    v = 1.0 / v_inv if v_inv > 0 else 1e6
    for (g, E, s) in gs:
        delta_sum += g * (s - E)
    delta = v * delta_sum
    a = math.log(vol * vol)
    def f(x):
        ex = math.exp(x)
        d2 = delta * delta
        num = ex * (d2 - phi * phi - v - ex)
        den = 2.0 * (phi * phi + v + ex) ** 2
        return num / den - (x - a) / (TAU * TAU)
    A = a; B = 0.0
    if delta * delta > phi * phi + v:
        B = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * TAU) < 0:
            k += 1
            if k > 100: break
        B = a - k * TAU
    fA = f(A); fB = f(B); it = 0
    while abs(B - A) > 1e-6 and it < 100:
        C = A + (A - B) * fA / (fB - fA); fC = f(C)
        if fC * fB < 0: A = B; fA = fB
        else: fA = fA / 2.0
        B = C; fB = fC; it += 1
    new_vol = math.exp(A / 2.0)
    phi_star = math.sqrt(phi * phi + new_vol * new_vol)
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu + new_phi * new_phi * delta_sum
    return 1500.0 + 173.7178 * new_mu, 173.7178 * new_phi, new_vol

print('ladder: loaded, maps elite=%d vanilla=%d' % (len(ELITE_MAPS), len(VAN_MAPS)))

# ---------- обход и сохранение ----------
def save_profiles(stat_groups, elite=False):
    now = int(time.time()); rows = []
    for g in stat_groups or []:
        for m in g.get('members', []):
            rows.append((m['profile_id'], m.get('alias', ''), m.get('name', '').replace('/steam/', ''),
                         m.get('country', ''), 1 if elite else 0, now))
    if rows:
        with _db_lock, db() as c:
            for r in rows:
                c.execute('''INSERT INTO profiles (profile_id,alias,steam,country,elite_seen,updated) VALUES (?,?,?,?,?,?)
                             ON CONFLICT(profile_id) DO UPDATE SET alias=excluded.alias, steam=excluded.steam,
                             country=excluded.country, elite_seen=max(elite_seen,excluded.elite_seen), updated=excluded.updated''', r)

def store_matches(ms):
    newpids = set()
    with _db_lock, db() as c:
        for m in ms:
            mid = m['id']
            if c.execute('SELECT 1 FROM matches WHERE id=?', (mid,)).fetchone():
                continue
            mem = m.get('matchhistorymember', [])
            el = 1 if (m.get('matchtype_id') == 0 and elite_sure(m)) else 0
            mode = mode_of(mem) if m.get('matchtype_id') == 0 else None
            c.execute('INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?)',
                      (mid, m['startgametime'], el, mode or '', season_of(m['startgametime']),
                       json.dumps(m, separators=(',', ':'))))
            for x in mem:
                c.execute('INSERT OR IGNORE INTO members VALUES (?,?,?)', (x['profile_id'], mid, m['startgametime']))
                newpids.add(x['profile_id'])
    now = int(time.time())
    with _db_lock, db() as c:
        for pid in newpids:
            c.execute('INSERT OR IGNORE INTO frontier (profile_id,added) VALUES (?,?)', (pid, now))

def crawl_chunk(pids):
    try:
        d = relic('getRecentMatchHistory', profile_ids=pids[:10])
    except Exception as e:
        return 0
    ms = d.get('matchHistoryStats', [])
    store_matches(ms)
    # ники: getRecentMatchHistory возвращает их в поле profiles (по одному члену на профиль)
    save_profiles([{'members': [p]} for p in d.get('profiles', [])])
    # отметить как Elite тех, кто играл Elite-матчи
    elite_pids = set()
    for m in ms:
        if m.get('matchtype_id') == 0 and elite_sure(m):
            for x in m.get('matchhistorymember', []): elite_pids.add(x['profile_id'])
    if elite_pids:
        with _db_lock, db() as c:
            c.executemany('UPDATE profiles SET elite_seen=1 WHERE profile_id=?', [(p,) for p in elite_pids])
    now = int(time.time())
    with _db_lock, db() as c:
        c.executemany('UPDATE frontier SET fetched=? WHERE profile_id=?', [(now, p) for p in pids[:10]])
    return len(ms)

SEEDS = []  # заполняется из текущей базы relic-stats при первом старте
def seed_frontier():
    with db() as c:
        if c.execute('SELECT COUNT(*) FROM frontier').fetchone()[0] > 0:
            return
    # посев: активные профили из соседней базы статистики, если она доступна только на чтение
    seeds = set()
    try:
        s = sqlite3.connect('file:/srv/relicstats/data/relic.sqlite?mode=ro', uri=True)
        seeds = {r[0] for r in s.execute('SELECT profile_id FROM members GROUP BY profile_id ORDER BY max(start) DESC LIMIT 2000')}
        s.close()
    except Exception:
        pass
    now = int(time.time())
    with _db_lock, db() as c:
        for p in seeds:
            c.execute('INSERT OR IGNORE INTO frontier (profile_id,added) VALUES (?,?)', (p, now))
    print('ladder: seeded frontier with %d profiles' % len(seeds))

def crawler():
    seed_frontier()
    last_recompute = 0
    # посчитать рейтинг сразу на старте, если его ещё нет (не ждать завершения кроула)
    try:
        with db() as c:
            have = c.execute('SELECT COUNT(*) FROM ratings WHERE season=?', (season_of(time.time()),)).fetchone()[0]
        if have == 0:
            recompute(season_of(time.time())); last_recompute = time.time()
    except Exception as e:
        print('initial recompute error:', e)
    while True:
        try:
            with db() as c:
                # сначала не опрошенные, потом самые давние
                due = [r[0] for r in c.execute('SELECT profile_id FROM frontier ORDER BY fetched ASC, added ASC LIMIT 10')]
            if not due:
                time.sleep(30); continue
            crawl_chunk(due)
            with db() as c:
                un = c.execute('SELECT COUNT(*) FROM frontier WHERE fetched=0').fetchone()[0]
            # пересчёт рейтинга текущего сезона: периодически (раз в 10 мин) и при полном завершении круга
            if un == 0 or time.time() - last_recompute > 600:
                recompute(season_of(time.time())); last_recompute = time.time()
                if un == 0:
                    with _db_lock, db() as c:
                        c.execute('UPDATE frontier SET fetched=0 WHERE fetched < ?', (int(time.time()) - 1800,))
            time.sleep(1.2)
        except Exception as e:
            print('crawler error:', e); time.sleep(10)

# ---------- пересчёт рейтинга сезона с нуля ----------
def recompute(season):
    with db() as c:
        rows = c.execute('SELECT data FROM matches WHERE season=? AND elite=1 AND mode!="" ORDER BY start', (season,)).fetchall()
    R = {}  # (pid,mode) -> [rating, rd, vol, games, wins, losses, last]
    def get(pid, mode):
        return R.setdefault((pid, mode), [1500.0, 350.0, 0.06, 0, 0, 0, 0])
    for (d,) in rows:
        m = json.loads(d); mode = mode_of(m['matchhistorymember'])
        if not mode: continue
        teams = {}
        for x in m['matchhistorymember']:
            teams.setdefault(x['teamid'], []).append(x)
        if len(teams) != 2: continue
        tids = list(teams)
        # средний рейтинг команды соперника как один "оппонент"
        avg = {}
        for t in tids:
            rs = [get(x['profile_id'], mode) for x in teams[t]]
            avg[t] = (sum(a[0] for a in rs) / len(rs), math.sqrt(sum(a[1] * a[1] for a in rs) / len(rs)))
        start = m['startgametime']
        upd = []
        for ti, t in enumerate(tids):
            ot = tids[1 - ti]
            for x in teams[t]:
                st = get(x['profile_id'], mode)
                s = 1.0 if x.get('outcome') == 1 else (0.0 if x.get('outcome') == 0 else 0.5)
                nr, nrd, nv = glicko2_update(st[0], st[1], st[2], [(avg[ot][0], avg[ot][1], s)])
                upd.append((x['profile_id'], mode, nr, nrd, nv, s, start))
        for (pid, mode, nr, nrd, nv, s, start) in upd:
            st = get(pid, mode)
            st[0], st[1], st[2] = nr, nrd, nv
            st[3] += 1; st[4] += 1 if s == 1.0 else 0; st[5] += 1 if s == 0.0 else 0; st[6] = start
    with _db_lock, db() as c:
        c.execute('DELETE FROM ratings WHERE season=?', (season,))
        for (pid, mode), st in R.items():
            c.execute('INSERT OR REPLACE INTO ratings VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (pid, mode, season, st[0], st[1], st[2], st[3], st[4], st[5], st[6]))
        c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('recompute_' + season, str(int(time.time()))))
    print('ladder: recomputed %s, %d player-mode ratings from %d matches' % (season, len(R), len(rows)))

# ---------- HTTP API ----------
def current_season(): return season_of(time.time())

def board(season, mode, limit=100, min_games=5):
    with db() as c:
        rows = c.execute('''SELECT r.profile_id, r.rating, r.rd, r.games, r.wins, r.losses, p.alias, p.steam, p.country
                            FROM ratings r LEFT JOIN profiles p ON p.profile_id=r.profile_id
                            WHERE r.season=? AND r.mode=? AND r.games>=?
                            ORDER BY r.rating DESC LIMIT ?''', (season, mode, min_games, limit)).fetchall()
    pen = {}
    try:
        with db() as c:
            for st, n in c.execute('SELECT steam, COUNT(*) FROM mm_ruin WHERE season=? GROUP BY steam', (season,)): pen[st] = n
    except Exception: pass
    rows = sorted(((r, r[1] - RUIN_PENALTY * pen.get(r[7] or '', 0)) for r in rows), key=lambda x: -x[1])
    out = []
    for i, (r, eff) in enumerate(rows):
        out.append({'rank': i + 1, 'profile_id': r[0], 'rating': round(eff), 'rd': round(r[2]),
                    'games': r[3], 'wins': r[4], 'losses': r[5], 'ruined': pen.get(r[7] or '', 0),
                    'name': r[6] or ('#%d' % r[0]), 'steam': r[7], 'country': r[8]})
    return out

SEASONS_CACHE = []   # DISTINCT по всей таблице матчей идёт ~1 мин — считаем в фоне, отдаём готовое
def _seasons_query():
    with db() as c:
        return [r[0] for r in c.execute('SELECT DISTINCT season FROM matches WHERE elite=1 ORDER BY season DESC')]
def _seasons_refresher():
    while True:
        try:
            v = _seasons_query()
            if v: SEASONS_CACHE[:] = v
        except Exception:
            pass
        time.sleep(1800)
def seasons():
    return list(SEASONS_CACHE) or [current_season()]

def stats():
    with db() as c:
        return {'matches': c.execute('SELECT COUNT(*) FROM matches WHERE elite=1').fetchone()[0],
                'profiles': c.execute('SELECT COUNT(*) FROM profiles WHERE elite_seen=1').fetchone()[0],
                'frontier': c.execute('SELECT COUNT(*) FROM frontier').fetchone()[0],
                'pending': c.execute('SELECT COUNT(*) FROM frontier WHERE fetched=0').fetchone()[0],
                'rated': c.execute('SELECT COUNT(*) FROM ratings WHERE season=?', (current_season(),)).fetchone()[0]}

# ---------- матчмейкинг: своя очередь + матчер (клиент — DLL в Elite) ----------
# Поток: клиент шлёт join(steam,mode) → poll'ит статус. Матчер сводит пару по рейтингу.
# Хосту возвращаем role=host → он создаёт приватную игру и шлёт host(steam,lobby=<steam lobby id>).
# Гостю poll возвращает role=guest + lobby → он заходит в это лобби (путь Steam-приглашения). Матчер — наш, Relic не участвует.
MM_LOCK = threading.Lock()
MM = {}          # steam(str) -> игрок
MM_MATCHES = {}  # match_id -> матч
MM_SEQ = [0]
PRESENCE = {}    # steam(str) -> last_seen (лаунчер открыт/активен)
def _touch(steam):
    if steam: PRESENCE[str(steam)] = time.time(); users.game_seen(steam)
MODES = ('1v1', '2v2', '3v3')
WATCH = 'watch'
TEAM_N = {'1v1': 1, '2v2': 2, '3v3': 3}

def _rating_for(steam, mode):
    # profile_id по steam; рейтинг текущего сезона в режиме, иначе дефолт 1500/350
    try:
        with db() as c:
            row = c.execute('SELECT profile_id FROM profiles WHERE steam=? LIMIT 1', (str(steam),)).fetchone()
            if row:
                pid = row[0]
                r = c.execute('SELECT rating, rd, games FROM ratings WHERE profile_id=? AND mode=? AND season=?',
                              (pid, mode, current_season())).fetchone()
                a = c.execute('SELECT alias FROM profiles WHERE profile_id=?', (pid,)).fetchone()
                name = (a[0] if a and a[0] else None)
                if r: return pid, r[0], r[1], r[2], name
                return pid, 1500.0, 350.0, 0, name
    except Exception:
        pass
    return None, 1500.0, 350.0, 0, None

def _relic_rating(steam):
    # запасной вариант: рейтинг Relic от сервиса relicstats (то же число, что LobbyStats показывает в лобби)
    try:
        u = 'http://127.0.0.1:8216/stats/api/lobby?n=' + urllib.parse.quote('/steam/%s' % steam)
        line = urllib.request.urlopen(u, timeout=8).read().decode('utf-8').strip().split('\n')[0].split('\t')
        pid = int(line[5]) if len(line) >= 6 and line[5].isdigit() else None
        if len(line) >= 2 and line[1].isdigit(): return float(line[1]), None, pid
        return None, None, pid
    except Exception:
        pass
    return None, None, None

def _rating_info(steam, mode):
    # рейтинг для очереди и сайта: ладдер (сезон, режим) → Relic → новичок (1500 только для подбора, на сайте не показываем)
    pid, rating, rd, games, name = _rating_for(steam, mode)
    rating -= RUIN_PENALTY * _ruins(steam=steam, season=current_season())
    rr, alias, rpid = _relic_rating(steam)
    pid = pid or rpid                      # Relic profile id: так игрок записан в слотах лобби (для автокика и расстановки)
    if games: return pid, rating, rd, games, name, 'ladder'
    if rr: return pid, rr, 200.0, 0, name or alias, 'relic'
    return pid, rating, rd, 0, name or alias, 'new'

def _counts():  # вызывать под MM_LOCK
    c = {m: 0 for m in MODES + (WATCH,)}
    for p in MM.values():
        if p['state'] == 'searching' and p['mode'] in c:
            c[p['mode']] += 1
    return c

def _reap():  # вызывать под MM_LOCK: выкинуть тех, кто перестал poll'ить
    now = time.time()
    for steam in [s for s, p in MM.items()
                  if now - p['seen'] > (30 if p['state'] == 'searching' else 150)]:
        _drop(steam)
    for s in [s for s, t in PRESENCE.items() if now - t > 120]:
        PRESENCE.pop(s, None)
    _reap_playing(now)
    for mid, m in list(MM_MATCHES.items()):        # a match being set up whose players all left the queue (closed the game) - forget it
        if now - m.get('created', now) > 600 and not any(x in MM for x in m['host'] + m['guest']): MM_MATCHES.pop(mid, None)

def _drop(steam):  # под MM_LOCK: убрать игрока и, если был в матче, вернуть соперника(ов) в поиск
    p = MM.pop(steam, None)
    if p and p.get('role') == 'watcher':
        m = MM_MATCHES.get(p.get('match'))
        if m and steam in m['watchers']: m['watchers'].remove(steam); m['players'].pop(steam, None)
        return
    if p and p.get('match'):
        m = MM_MATCHES.pop(p['match'], None)
        if m:
            for s in m['host'] + m['guest']:
                if s == steam: continue
                op = MM.get(s)
                if op: op.update(state='searching', match=None, role=None)   # время ожидания (ts) сохраняется - приоритет не теряется
            for s in m.get('watchers', []):
                op = MM.get(s)
                if op: op.update(state='searching', match=None, role=None)

def _common_maps(grp):  # пересечение желаемых карт группы (пустой список у игрока = любая карта)
    sets = [set(p.get('maps') or []) for p in grp]
    want = [s for s in sets if s]
    if not want: return None              # всем всё равно
    common = set.intersection(*want)
    return common or set()                # set() = несовместимы

def _group_ok(grp):  # ограничения: карты пересекаются, разброс рейтинга в пределах max_diff каждого
    spread = grp[-1]['rating'] - grp[0]['rating']
    if any(p.get('max_diff') and spread > p['max_diff'] for p in grp): return False
    c = _common_maps(grp)
    return c is None or bool(c)

MM_OWN_OFF = ROOT + '/data/mm_own_server_off'   # file exists = automatch never uses our game servers (29.10: a match where one player
                                              # reached Moscow and the other did not never connected)
MM_BLOCK_FILE = ROOT + '/data/mm_block.txt'   # steamID на строку (# — комментарий): в подбор не попадают (напр. вечно крашится)
def _blocked():
    try: return {l.split('#')[0].strip() for l in open(MM_BLOCK_FILE) if l.split('#')[0].strip()}
    except Exception: return set()

def _try_match(mode):  # под MM_LOCK: нестрогий подбор с приоритетом ожидания
    # 1) в матч всегда входит тот, кто ждёт дольше всех (никто не «застревает», пока другие играют снова и снова);
    # 2) хватает людей — сводим сразу; есть выбор — берём к нему тех, у кого есть общие карты и ближе рейтинг;
    # 3) карты и max_diff — пожелания, не запрет.
    n = TEAM_N[mode]; need = n * 2; blocked = _blocked() | {p['steam'] for p in MM.values() if p.get('ruiner')}
    while True:
        allw = sorted([p for p in MM.values() if p['state'] == 'searching' and p['mode'] == mode and p['steam'] not in blocked], key=lambda p: p['ts'])
        # pools: test groups stay among themselves; strict own-server players (Relay 7) only with each other. Relay 8
        # (relay_compat) shares the pool with Relic players: the host is then a Relic player, our server passes the others on
        pools = {}
        for p in allw: pools.setdefault(p.get('pool', ''), []).append(p)
        waiting = next((w for w in sorted(pools.values(), key=lambda w: w[0]['ts']) if len(w) >= need), None)
        if not waiting: break
        anchor = waiting[0]; amaps = set(anchor.get('maps') or [])
        def score(q):
            qm = set(q.get('maps') or [])
            no_common = 1 if (amaps and qm and not (amaps & qm)) else 0
            diff = abs(q['rating'] - anchor['rating'])
            over = 1 if any(x.get('max_diff') and diff > x['max_diff'] for x in (anchor, q)) else 0
            return (no_common, over, diff, q['ts'])
        grp = [anchor] + sorted(waiting[1:], key=score)[:need - 1]
        grp.sort(key=lambda p: p['rating'])
        cm = _common_maps(grp)
        if cm: pool = sorted(cm)
        elif amaps: pool = sorted(amaps)
        else: pool = sorted(set().union(*[set(p.get('maps') or []) for p in grp]))
        mmap = random.choice(pool) if pool else None
        # команды: перебор всех разбиений, минимальная разница суммарного рейтинга (2v2: сильнейший + слабейший против двух средних)
        import itertools
        best_split, best_d = None, None
        for comb in itertools.combinations(range(len(grp)), n):
            if 0 not in comb: continue                      # без зеркальных повторов
            d_ = abs(sum(grp[i]['rating'] for i in comb) - sum(grp[i]['rating'] for i in range(len(grp)) if i not in comb))
            if best_d is None or d_ < best_d: best_d, best_split = d_, comb
        host_team = [grp[i]['steam'] for i in best_split]
        guest_team = [grp[i]['steam'] for i in range(len(grp)) if i not in best_split]
        MM_SEQ[0] += 1; mid = 'm%d' % MM_SEQ[0]
        relic = [p for p in grp if not p.get('own')]            # смешанный состав: хост — игрок без нашего сервера (Relic),
        host_steam = min(relic or grp, key=lambda p: p['ts'])['steam']  # Relay 8 у остальных уводит их к нему на Relic; иначе — кто дольше ждёт
        if host_steam not in host_team:  # хост всегда в host-команде первым: меняем команды местами, а не переносим его
            host_team, guest_team = guest_team, host_team
        host_team.remove(host_steam); host_team.insert(0, host_steam)
        for p in grp:
            p.update(state='matched', match=mid,
                     role='host' if p['steam'] == host_steam else 'guest')
        slots = {s_: i for i, s_ in enumerate(host_team)}; slots.update({s_: 4 + i for i, s_ in enumerate(guest_team)})
        MM_MATCHES[mid] = {'id': mid, 'mode': mode, 'host': host_team, 'guest': guest_team, 'slots': slots, 'watchers': [], 'pool': anchor.get('pool', ''),
                           'host_steam': host_steam, 'lobby': None, 'created': time.time(), 'map': mmap,
                           'state': 'creating', 'started': None,
                           'players': {p['steam']: {'name': p.get('name') or 'Player', 'rating': round(p['rating']), 'rsrc': p.get('rsrc', 'ladder'),
                                                    'race': p.get('race'), 'hero': p.get('hero')} for p in grp}}


def _assign_watchers():  # под MM_LOCK: зрителей — в собираемые матчи (до старта), по вместимости слотов наблюдателей
    forming = sorted([m for m in MM_MATCHES.values() if m.get('state') != 'playing'], key=lambda m: m['created'])
    for w in sorted([p for p in MM.values() if p['mode'] == WATCH and p['state'] == 'searching'], key=lambda p: p['ts']):
        for m in forming:
            if m.get('pool', '') != w.get('pool', ''): continue
            cap = 8 - 2 * TEAM_N[m['mode']]
            if len(m['watchers']) < cap:
                m['watchers'].append(w['steam']); w.update(state='matched', match=m['id'], role='watcher')
                m['players'][w['steam']] = {'name': w.get('name') or 'Player', 'watcher': True}
                break

RELAY_ORDER = ['15.220.68.107', '116.202.106.150']   # owner 29.10: Warsaw (AWS) if everyone reaches it, then Germany, else Relic; Moscow off (players report small lags through it)

def _best_relay(steams):
    # Relay 9: every player pinged our game servers (relay_ping {ip: ms}, -1 = no answer). The match goes to the first server
    # of RELAY_ORDER that every player reaches; none =  (play on Relic). 0 ms counts as no answer: no server is that close
    # over the internet, it is something on the player own PC (VPN, a blocking bypass, an antivirus) answering for it -
    # 29.10 a player from Ukraine "reached" Moscow that way and the match never connected. Someone without pings (an older
    # add-on): nothing is known about what he reaches - Relic too.
    pings = [(MM.get(s) or {}).get('ping') or {} for s in steams]
    if not pings or not all(pings): return ''
    for h in RELAY_ORDER:
        if all(x.get(h, -1) > 0 for x in pings): return h
    return ''

def _player_view(p):  # под MM_LOCK: что вернуть игроку в poll
    if p['mode'] == WATCH and p['state'] == 'searching':
        _assign_watchers()
    if p['state'] == 'searching' and p['mode'] == WATCH:
        return {'state': 'searching', 'mode': WATCH, 'queue': _counts()}
    if p['state'] == 'searching':
        return {'state': 'searching', 'mode': p['mode'], 'rating': round(p['rating']), 'queue': _counts()}
    m = MM_MATCHES.get(p['match'])
    if not m:
        p.update(state='searching', match=None, role=None)
        return {'state': 'searching', 'mode': p['mode'], 'queue': _counts()}
    mine = m['host'] if p['steam'] in m['host'] else m['guest']      # своя команда — та, где игрок есть (не по роли)
    theirs = m['guest'] if p['steam'] in m['host'] else m['host']
    def side(steams):
        out = []
        for s in steams:
            q = MM.get(s, {})
            pl = m['players'].get(s, {})
            out.append({'steam': s, 'name': q.get('name') or pl.get('name') or 'Player', 'rating': round(q.get('rating', pl.get('rating', 1500))),
                        'race': q.get('race') or pl.get('race')})
        return out
    roster = [{'steam': s_, 'slot': sl, 'pid': (MM.get(s_) or {}).get('pid') or 0, 'name': m['players'].get(s_, {}).get('name')} for s_, sl in m.get('slots', {}).items()]
    wpids = [(MM.get(w) or {}).get('pid') or 0 for w in m.get('watchers', [])]
    # everyone (players and watchers) has our game server (Relay 8): only then the match plays on it, else on Relic
    everyone = list(m.get('slots', {})) + list(m.get('watchers', []))
    own_all = all((MM.get(s_) or {}).get('own') for s_ in everyone)
    if os.path.exists(MM_OWN_OFF): own_all = False               # switch: automatch on Relic servers only, our servers unused
    if own_all and 'relay' not in m and m.get('slots'):   # picked once per match: the host's lobby opens there first
        m['relay'] = _best_relay(everyone)
        print('mm: match %s relay %r (pings %s)' % (m['id'], m['relay'], [(MM.get(s_) or {}).get('ping') for s_ in everyone]), flush=True)
    relay = m.get('relay') if own_all else None
    if relay == '': own_all = False                       # none of our servers reaches everyone: Relic
    extra = {'relay_host': relay} if relay else {}
    wnames = [m['players'].get(w, {}).get('name') for w in m.get('watchers', [])]
    if p['role'] == 'watcher':
        return {'state': 'matched', 'match': m['id'], 'mode': m['mode'], 'role': 'watcher', 'host_steam': m['host_steam'],
                'lobby': m['lobby'], 'map': m.get('map'), 'roster': roster, 'watchers': m['watchers'], 'watcher_pids': wpids, 'watcher_names': wnames, 'allies': [], 'enemies': [], 'own_server': own_all, **extra}
    return {'state': 'matched', 'match': m['id'], 'mode': m['mode'], 'role': p['role'], 'slot': m.get('slots', {}).get(p['steam']),
            'roster': roster, 'watchers': m.get('watchers', []), 'watcher_pids': wpids, 'watcher_names': wnames, 'bots': bool(m.get('bots')),
            'host_steam': m['host_steam'], 'lobby': m['lobby'], 'map': m.get('map'), 'own_server': own_all,
            'allies': side([s for s in mine if s != p['steam']]), 'enemies': side(theirs), **extra}

def _clean(v, n=48):
    v = str(v or '').strip()
    return ''.join(ch for ch in v if ch.isprintable())[:n] or None

def _relay_ping(v):  # {ip: ms} from Relay 9 via MMFind: IPv4 keys, -1..5000
    out = {}
    if isinstance(v, dict):
        for k, x in list(v.items())[:8]:
            try: x = int(x)
            except Exception: continue
            if re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', str(k)): out[str(k)] = max(-1, min(5000, x))
    return out or None

def mm_join(steam, mode, d=None):
    d = d or {}
    steam = str(steam)
    if mode not in MODES and mode != WATCH: mode = '1v1'
    pid, rating, rd, games, name, rsrc = _rating_info(steam, mode)
    maps = [_clean(x, 64) for x in (d.get('maps') or []) if _clean(x, 64)][:200]
    try: max_diff = max(0, min(3000, int(d.get('max_diff') or 0)))
    except Exception: max_diff = 0
    users.set_alias(steam, name or _clean(d.get('name')))
    now = time.time()
    with MM_LOCK:
        _reap()
        # searching again = the previous match is over (players who quit the game from a match never send "ended")
        for mid, m in list(MM_PLAYING.items()):
            if steam in m['host'] + m['guest'] + m.get('watchers', []):
                _end_match(mid, m, now)
        old = MM.get(steam)
        if old and old['mode'] == mode and now - old.get('jts', 0) < 15:   # повтор того же join через другое зеркало (клиент не дождался ответа)
            old['seen'] = now
            return _player_view(old)
        MM[steam] = {'jts': now, 'steam': steam, 'mode': mode, 'rating': rating, 'rd': rd, 'games': games, 'ruiner': _ruin_blocked(steam),
                     'pid': pid, 'name': name or _clean(d.get('name')), 'state': 'searching', 'match': None, 'role': None,
                     'rsrc': rsrc, 'race': _clean(d.get('race'), 32), 'hero': _clean(d.get('hero'), 32), 'maps': maps, 'max_diff': max_diff,
                     'ts': now, 'seen': now, 'pool': '%s|%s' % (_clean(d.get('group'), 24) or '', 'own' if d.get('own_server') and not d.get('relay_compat') else ''),
                     'own': bool(d.get('own_server')), 'ping': _relay_ping(d.get('relay_ping')) if d.get('own_server') else None,
                     'group': _clean(d.get('group'), 24) or '', 'compat': bool(d.get('relay_compat')),
                     'ua': _clean(d.get('_ua'), 80) or '', 'ver': _clean(d.get('ver') or d.get('launcher'), 32) or '', 'elite': _clean(d.get('elite'), 32) or '', 'os': _clean(d.get('os'), 24) or ''}
        if mode == WATCH: _assign_watchers()
        elif d.get('bots'):   # тест: матч сразу против компьютеров, хост заполнит пустые слоты ИИ
            MM_SEQ[0] += 1; mid = 'm%d' % MM_SEQ[0]; p = MM[steam]
            p.update(state='matched', match=mid, role='host')
            MM_MATCHES[mid] = {'id': mid, 'mode': mode, 'host': [steam], 'guest': [], 'slots': {steam: 0}, 'watchers': [], 'bots': True,
                               'host_steam': steam, 'lobby': None, 'created': now, 'map': (maps and random.choice(maps)) or None,
                               'state': 'creating', 'started': None,
                               'players': {steam: {'name': p.get('name') or 'Player', 'rating': round(p['rating']), 'rsrc': p.get('rsrc', 'ladder'),
                                                   'race': p.get('race'), 'hero': p.get('hero')}}}
            _assign_watchers()
        else: _try_match(mode); _assign_watchers()
        return _player_view(MM[steam])

def mm_poll(steam):
    steam = str(steam)
    with MM_LOCK:
        p = MM.get(steam)
        if not p: return {'state': 'idle'}
        p['seen'] = time.time()
        return _player_view(p)

def mm_host(steam, lobby, mapname=None):
    steam = str(steam)
    with MM_LOCK:
        p = MM.get(steam)
        if p and p['state'] == 'matched' and p['role'] == 'host':
            m = MM_MATCHES.get(p['match'])
            if m:
                m['lobby'] = str(lobby)[:80]
                if _clean(mapname, 64): m['map'] = _clean(mapname, 64)     # the map the host really set (history shows it)
            p['seen'] = time.time()
            return {'ok': True}
    return {'ok': False}

MM_PLAYING = {}   # match_id -> матч, который уже запущен (держим до конца игры / 2 часа)
def mm_started(steam):  # хост нажал Start: матч переходит в «идёт»
    steam = str(steam)
    with MM_LOCK:
        p = MM.get(steam)
        m = MM_MATCHES.get(p['match']) if p and p.get('match') else None
        if not m: return {'ok': False}
        m['state'] = 'playing'; m['started'] = time.time()
        MM_PLAYING[m['id']] = m
        try: _history_add(m)
        except Exception: pass
        for s in m['host'] + m['guest'] + m.get('watchers', []):
            MM.pop(s, None)                 # игроки и зрители ушли в бой — из очереди их убираем
        MM_MATCHES.pop(m['id'], None)
        return {'ok': True}

def _end_match(mid, m, now):  # под MM_LOCK: матч закончился (вернулись в меню / все вышли из игры / есть итог Relic)
    MM_PLAYING.pop(mid, None)
    users.refresh_rating(m['host'] + m['guest'])
    if m.get('lobby'):
        try:
            with _db_lock, db() as c: c.execute('UPDATE mm_history SET ended = ? WHERE lobby = ? AND ended IS NULL', (int(now), m['lobby']))
        except Exception: pass

ALIVE_GAP = 180   # MMFind 0.23+ шлёт alive раз в минуту, пока идёт матч; 3 минуты тишины от всех = все вышли
def mm_alive(steam):
    steam = str(steam); now = time.time()
    with MM_LOCK:
        for m in MM_PLAYING.values():
            if steam in m['host'] + m['guest']: m.setdefault('alive', {})[steam] = now
    return {'ok': True}

def _reap_playing(now):  # под MM_LOCK
    for mid, m in list(MM_PLAYING.items()):
        a = m.get('alive')
        if a and now - max(a.values()) > ALIVE_GAP: _end_match(mid, m, now)       # старые клиенты alive не шлют - для них как раньше

def mm_ended(steam):  # игрок вернулся в меню после матча
    steam = str(steam)
    with MM_LOCK:
        for mid, m in list(MM_PLAYING.items()):
            if steam in m['host'] + m['guest']:
                if not m.get('back') and m.get('lobby'):
                    try:
                        with _db_lock, db() as c: c.execute('UPDATE mm_history SET ended = ? WHERE lobby = ? AND ended IS NULL', (int(time.time()), m['lobby']))
                    except Exception: pass
                m.setdefault('back', set()).add(steam)
                if len(m['back']) >= 1: MM_PLAYING.pop(mid, None); users.refresh_rating(m['host'] + m['guest'])
        return {'ok': True}

# ---------- сорванные загрузки: у кого игра упала, пока матч грузился (остальные остались без игры) ----------
RUIN_PENALTY = 25      # очков рейтинга за каждый срыв в сезоне (в таблице и в подборе)
RUIN_BLOCK = (3, 86400)  # 3 срыва за сутки - сутки вне подбора
with db() as c:
    c.execute('CREATE TABLE IF NOT EXISTS mm_ruin (steam TEXT, lobby TEXT, t INTEGER, season TEXT, PRIMARY KEY (steam, lobby))')

def mm_ruined(steam, lobby):
    steam = str(steam); lobby = _clean(lobby, 80)
    if not steam or not lobby or not lobby.startswith('MM-'): return {'ok': False}
    with db() as c:
        row = c.execute('SELECT players, started FROM mm_history WHERE lobby = ?', (lobby,)).fetchone()
    if not row or steam not in [p.get('steam') for p in json.loads(row[0])]: return {'ok': False}   # только свои матчи ладдера
    with _db_lock, db() as c:
        c.execute('INSERT OR IGNORE INTO mm_ruin VALUES (?,?,?,?)', (steam, lobby, int(time.time()), season_of(row[1])))
    return {'ok': True}

def _ruins(steam=None, pid=None, season=None, since=None):
    q, a = 'SELECT COUNT(*) FROM mm_ruin r', []
    if pid is not None: q += ' JOIN profiles p ON p.steam = r.steam WHERE p.profile_id = ?'; a.append(pid)
    else: q += ' WHERE r.steam = ?'; a.append(str(steam))
    if season: q += ' AND r.season = ?'; a.append(season)
    if since: q += ' AND r.t > ?'; a.append(int(since))
    try:
        with db() as c: return c.execute(q, a).fetchone()[0]
    except Exception: return 0

def _ruin_blocked(steam):
    return _ruins(steam=steam, since=time.time() - RUIN_BLOCK[1]) >= RUIN_BLOCK[0]

# ---------- история матчей ладдера: состав при старте, результат — из Relic (по имени лобби), реплей — с /replay/ ----------
with db() as c:
    c.executescript('''
    CREATE TABLE IF NOT EXISTS mm_history (lobby TEXT PRIMARY KEY, mid TEXT, mode TEXT, map TEXT, started INTEGER, ended INTEGER,
        players TEXT, watchers TEXT, result TEXT, replay TEXT, checked INTEGER DEFAULT 0, tries INTEGER DEFAULT 0, rtries INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS mm_history_started ON mm_history (started DESC);
    ''')

def _history_add(m):  # под MM_LOCK, в момент старта: кто, за какую сторону, рейтинг на входе
    if not m.get('lobby') or m.get('bots'): return          # тесты против компьютера (TestBots) — не матчи ладдера
    if (m.get('pool') or '').split('|')[0]: return         # тестовая группа (MMFind TestGroup) — не матчи ладдера
    pl = []
    for team, side in ((1, m['host']), (2, m['guest'])):
        for s in side:
            p = m['players'].get(s, {}); q = MM.get(s) or {}
            pl.append({'steam': s, 'pid': q.get('pid'), 'name': p.get('name') or 'Player', 'team': team, 'rating': p.get('rating'),
                       'rsrc': p.get('rsrc'), 'race': p.get('race'), 'hero': p.get('hero')})
    w = [{'steam': s, 'name': (m['players'].get(s) or {}).get('name') or 'Player'} for s in m.get('watchers', [])]
    with _db_lock, db() as c:
        c.execute('INSERT OR IGNORE INTO mm_history (lobby, mid, mode, map, started, players, watchers) VALUES (?,?,?,?,?,?,?)',
                  (m['lobby'], m['id'], m['mode'], m.get('map'), int(m['started']), json.dumps(pl, ensure_ascii=False), json.dumps(w, ensure_ascii=False)))

def _local_json(url, timeout=30):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r: return json.load(r)
    except Exception: return None

def _history_resolver():
    """Фон: для сыгранных матчей ладдера берём итог у Relic (matchhistory с description = имя лобби) и ищем реплей."""
    while True:
        now = int(time.time())
        try:
            with db() as c:
                rows = c.execute('''SELECT lobby, players, ended, started, tries, result, replay, rtries FROM mm_history
                    WHERE started > ? AND ((result IS NULL AND tries < 40) OR (replay IS NULL AND rtries < 40)) AND checked < ?''',
                    (now - 3 * 86400, now - 90)).fetchall()
            for lobby, pl, ended, started, tries, result, replay, rtries in rows:
                pl = json.loads(pl); upd = {'checked': now}
                if result is None and (ended or now - started > 1200):
                    pid = next((p['pid'] for p in pl if p.get('pid')), None); upd['tries'] = tries + 1
                    j = pid and _local_json('http://127.0.0.1:8216/stats/api/match?' + urllib.parse.urlencode({'lobby': lobby, 'pid': pid}))
                    if j and j.get('description') == lobby:
                        res = {'relic_id': j.get('id'), 'mapname': j.get('mapname'), 'start': j.get('startgametime'),
                               'duration': (j.get('completiontime') or 0) - (j.get('startgametime') or 0), 'players': []}
                        for r in j.get('matchhistoryreportresults', []):
                            try: cn = json.loads(r.get('counters') or '{}')
                            except Exception: cn = {}
                            hero = next((i for i, k in enumerate(('ha', 'hb', 'hc')) if cn.get(k)), None)
                            res['players'].append({'pid': r.get('profile_id'), 'result': r.get('resulttype'), 'team': r.get('teamid'),
                                                   'race': r.get('race_id'), 'hero': hero,
                                                   'stats': {k: cn.get(k) for k in ('ukill', 'uprod', 'plost', 'pseized', 'hkill', 'hlost', 'mvp', 'sbuilt')}})
                        upd['result'] = json.dumps(res, ensure_ascii=False)
                        if not ended: upd['ended'] = (j.get('completiontime') or now)
                        with MM_LOCK:
                            for mid, m in list(MM_PLAYING.items()):
                                if m.get('lobby') == lobby: MM_PLAYING.pop(mid, None)
                if replay is None and (ended or now - started > 600):
                    upd['rtries'] = rtries + 1
                    r = _local_json('http://127.0.0.1:8215/replay/api/by_lobby/' + urllib.parse.quote(lobby))
                    if r and r.get('id'): upd['replay'] = r['id']
                with _db_lock, db() as c:
                    c.execute('UPDATE mm_history SET %s WHERE lobby = ?' % ', '.join(k + ' = ?' for k in upd), list(upd.values()) + [lobby])
        except Exception:
            pass
        time.sleep(60)

RACE_KEYS = ['chaos_marines', 'eldar', 'imperial_guard', 'ordo_malleus', 'orks', 'space_marines', 't_tau', 'tyranids', 'z_unecron']   # как в игре и в реплеях
RACE_ALIAS = {'chaos_space_marines': 'chaos_marines', 'tau': 't_tau', 'necron': 'z_unecron'}                                     # так их шлёт MMFind
def history(q='', mode='', mapk='', race='', page=0, n=30, off=None):
    """Сыгранные через ладдер матчи, новые сверху; фильтры: текст (ник/карта/лобби), режим, карта, раса."""
    with db() as c:
        rows = c.execute('SELECT lobby, mid, mode, map, started, ended, players, watchers, result, replay FROM mm_history ORDER BY started DESC LIMIT 5000').fetchall()
    q = (q or '').strip().lower(); out = []
    for lobby, mid, md, mp, started, ended, pl, w, res, rep in rows:
        pl = json.loads(pl); res = json.loads(res) if res else None; w = json.loads(w or '[]')
        mapk_ = ((res or {}).get('mapname') or mp or '').lower()
        if res:                                    # Relic знает точно: раса, герой, победа
            by = {r['pid']: r for r in res['players']}
            for p in pl:
                r = by.get(p.get('pid'))
                if r:
                    if r.get('race') is not None and 0 <= r['race'] < len(RACE_KEYS): p['race'] = RACE_KEYS[r['race']]
                    if r.get('hero') is not None: p['hero'] = r['hero']
                    p['win'] = r.get('result') == 1; p['stats'] = r.get('stats')
        for p in pl:
            p.pop('steam', None); p['race'] = RACE_ALIAS.get(p.get('race'), p.get('race'))
            try: p['hero'] = int(p['hero']) if p.get('hero') not in (None, '') else None
            except Exception: p['hero'] = None
        win = next((p['team'] for p in pl if p.get('win')), None)
        state = 'finished' if res else ('playing' if not ended and time.time() - started < 7200 else 'unknown')
        it = {'lobby': lobby, 'mode': md, 'map': mapk_, 'started': started, 'ended': ended,
              'duration': (res or {}).get('duration') or ((ended - started) if ended else None),
              'players': pl, 'watchers': [x.get('name') for x in w], 'winner': win, 'state': state, 'replay': rep}
        if mode and md != mode: continue
        if mapk and mapk_ != mapk.lower(): continue
        if race and not any(p.get('race') == race for p in pl): continue
        if q and q not in (' '.join([lobby, mapk_] + [p.get('name') or '' for p in pl])).lower(): continue
        out.append(it)
    page = max(0, int(page or 0)); n = max(1, min(100, int(n or 30)))
    off = max(0, int(off)) if off not in (None, '') else page * n          # off — сколько уже показано (страницы разного размера)
    return {'total': len(out), 'page': page, 'items': out[off:off + n],
            'maps': sorted({i['map'] for i in out if i['map']})}

STEAM_NOW = {'t': 0, 'n': None}
def _steam_now():  # игроков DoW2: Retribution в Steam прямо сейчас (Steam Web API, без ключа); кэш 5 минут
    if time.time() - STEAM_NOW['t'] > 300:
        STEAM_NOW['t'] = time.time()
        try:
            with urllib.request.urlopen('https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid=56400', timeout=8) as r:
                STEAM_NOW['n'] = json.load(r)['response'].get('player_count')
        except Exception: pass
    return STEAM_NOW['n']

def mm_live(debug=False):  # публично: кто ищет и какие матчи создаются / идут (без SteamID). debug=1 — детали для отладки подбора
    now = time.time()
    with MM_LOCK:
        _reap()
        for mid in [k for k, m in MM_PLAYING.items() if now - (m.get('started') or now) > 7200]:
            MM_PLAYING.pop(mid, None)
        _reap_playing(now)
        searching = [{'name': p.get('name') or 'Player', 'steam': p['steam'], 'games': p.get('games') or 0, 'rsrc': p.get('rsrc', 'ladder'), 'mode': p['mode'], 'rating': round(p['rating']),
                      'race': p.get('race'), 'maps': len(p.get('maps') or []), 'max_diff': p.get('max_diff') or 0,
                      'waiting': int(now - p['ts']), **(dict(pool=p.get('pool'), own=bool(p.get('own')), compat=bool(p.get('compat')), group=p.get('group') or '', ping=p.get('ping'), ver=p.get('ver') or '', elite=p.get('elite') or '', os=p.get('os') or '', ua=p.get('ua') or '', maxdiff=p.get('max_diff') or 0) if debug else {})}
                     for p in sorted(MM.values(), key=lambda p: p['ts']) if p['state'] == 'searching']
        def team(m, steams):
            out = []
            for s in steams:
                base = dict(m['players'].get(s, {'name': 'Player'}), steam=s)
                if debug:
                    q = MM.get(s) or {}
                    base.update(pool=q.get('pool'), own=bool(q.get('own')), compat=bool(q.get('compat')), group=q.get('group') or '', ver=q.get('ver') or '', elite=q.get('elite') or '', os=q.get('os') or '')
                out.append(base)
            return out
        matches = []
        for m in list(MM_MATCHES.values()) + list(MM_PLAYING.values()):
            st = m.get('state') if m.get('state') == 'playing' else ('lobby' if m.get('lobby') else 'creating')
            matches.append({'id': m['id'], 'mode': m['mode'], 'map': m.get('map'), 'state': st,
                            'watchers': [dict(m['players'].get(w, {'name': 'Player'}), steam=w) for w in m.get('watchers', [])],
                            'since': int(now - (m.get('started') or m['created'])),
                            'team1': team(m, m['host']), 'team2': team(m, m['guest']), **({'relay': m.get('relay')} if debug else {})})
        online = sum(1 for t in PRESENCE.values() if now - t < 60)
        res = {'online': online, 'queue': _counts(), 'searching': searching, 'matches': matches}
    res['in_game'] = _steam_now()
    return res

def live_view():
    # the launcher's Online block (c/live, the stream), only from memory: searching (looking for a game, watchers waiting
    # with mode watch), ingame (every match: "setup" until the host starts it, then "playing" with the start time; watchers
    # in their match), launcher (has the launcher open, nothing of the above) and users (name, avatar, rating of everyone
    # listed). Each person in one group only: ingame > searching > launcher. Times are absolute (unix): the launcher counts
    # the minutes, so the state does not change every second. Games without the launcher, not searching and not playing,
    # are not listed (plan, decision 3).
    now = time.time()
    with MM_LOCK:
        _reap()
        for mid in [k for k, m in MM_PLAYING.items() if now - (m.get('started') or now) > 7200]:
            MM_PLAYING.pop(mid, None)
        side = lambda m, ss: [{'steam': s, 'race': (m['players'].get(s) or {}).get('race')} for s in ss]
        ingame = [{'id': m['id'], 'state': 'playing' if m.get('state') == 'playing' else 'setup', 'mode': m['mode'], 'map': m.get('map'),
                   'started': int(m['started']) if m.get('state') == 'playing' and m.get('started') else None,
                   'team1': side(m, m['host']), 'team2': side(m, m['guest']), 'watchers': list(m.get('watchers', []))}
                  for m in sorted(list(MM_MATCHES.values()) + list(MM_PLAYING.values()), key=lambda m: m['created'])]
        busy = {x['steam'] for m in ingame for x in m['team1'] + m['team2']} | {w for m in ingame for w in m['watchers']}
        searching = [{'steam': p['steam'], 'mode': p['mode'], 'race': p.get('race'), 'since': int(p['ts'])}
                     for p in sorted(MM.values(), key=lambda p: p['ts']) if p['state'] == 'searching' and p['steam'] not in busy]
    busy |= {p['steam'] for p in searching}
    launcher = [{'steam': s, 'state': st} for s, st in users.in_launcher(now) if s not in busy]
    # now: the server's clock, so a launcher whose PC clock is off still counts the minutes right
    return {'now': int(now), 'searching': searching, 'ingame': ingame, 'launcher': launcher,
            'users': {s: users.card(s) for s in sorted(busy | {p['steam'] for p in launcher})}}

def mm_leave(steam):
    with MM_LOCK:
        _drop(str(steam))
    return {'ok': True}

def mm_status():
    now = time.time()
    with MM_LOCK:
        _reap()
        online = sum(1 for t in PRESENCE.values() if now - t < 60)
        playing = len(MM_PLAYING) + sum(1 for m in MM_MATCHES.values() if m['lobby'])
        q = _counts()
        return {'online': online, 'queue': q, 'searching': sum(q.values()),
                'matched': len(MM_MATCHES), 'playing': playing}

def mm_hello(steam):  # лёгкий heartbeat от открытого лаунчера (присутствие онлайн)
    with MM_LOCK:
        _touch(steam); _reap()
        online = sum(1 for t in PRESENCE.values() if time.time() - t < 60)
        q = _counts()
        return {'ok': True, 'online': online, 'searching': sum(q.values()),
                'playing': sum(1 for m in MM_MATCHES.values() if m['lobby'])}

# ---------- отзывы из лаунчера: чат игрок ↔ разработчики (26.10.2026) ----------
# Игрок: uid — случайный секрет из настроек лаунчера (знать uid = видеть свою переписку), имя по желанию.
# Разработчик отвечает со страницы /ladder/fb-admin (вход по токену из data/fb_admin_token).
FB_DIR = ROOT + '/data/fb'
FB_TOKEN_FILE = ROOT + '/data/fb_admin_token'
FB_LOCK = threading.Lock()
def fb_db():
    c = sqlite3.connect(ROOT + '/data/feedback.sqlite', timeout=30)
    c.execute('CREATE TABLE IF NOT EXISTS fb (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, name TEXT, author TEXT, text TEXT, images TEXT, ts INTEGER, seen INTEGER DEFAULT 0, meta TEXT)')
    c.execute('CREATE INDEX IF NOT EXISTS fb_uid ON fb (uid, id)')
    return c
def _fb_uid(u):
    u = str(u or '')
    return u if re.fullmatch(r'[A-Za-z0-9_-]{16,64}', u) else None
def _fb_token_ok(t):
    try: return bool(t) and t == open(FB_TOKEN_FILE).read().strip()
    except Exception: return False
def _fb_msg(r):
    return {'id': r[0], 'author': r[3], 'name': r[2], 'text': r[4], 'images': json.loads(r[5] or '[]'), 'ts': r[6]}
def fb_send(d, author='player'):
    uid = _fb_uid(d.get('uid'))
    if not uid: return {'error': 'uid'}
    text = str(d.get('text') or '')[:8000]
    name = _clean(d.get('name'), 48) or ''
    imgs = []
    os.makedirs(FB_DIR, exist_ok=True)
    for b64 in (d.get('images') or [])[:6]:
        try:
            raw = base64.b64decode(str(b64).split(',', 1)[-1], validate=False)
        except Exception:
            continue
        if not raw or len(raw) > 8 << 20: continue
        ext = 'png' if raw[:4] == b'\x89PNG' else 'jpg' if raw[:2] == b'\xff\xd8' else 'gif' if raw[:3] == b'GIF' else 'webp' if raw[8:12] == b'WEBP' else None
        if not ext: continue
        fn = '%d_%s.%s' % (int(time.time() * 1000), os.urandom(4).hex(), ext)
        with open(FB_DIR + '/' + fn, 'wb') as f: f.write(raw)
        imgs.append(fn)
    if not text.strip() and not imgs: return {'error': 'empty'}
    meta = json.dumps({k: str(d.get(k))[:120] for k in ('launcher', 'os', 'elite', 'steam') if d.get(k)}, ensure_ascii=False)
    with FB_LOCK:
        c = fb_db()
        try:
            c.execute('INSERT INTO fb (uid, name, author, text, images, ts, seen, meta) VALUES (?,?,?,?,?,?,?,?)',
                      (uid, name, author, text, json.dumps(imgs), int(time.time()), 1 if author == 'dev' else 0, meta))
            c.commit()
        finally:
            c.close()
    return {'ok': True}
def fb_thread(uid, after=0):
    uid = _fb_uid(uid)
    if not uid: return {'error': 'uid'}
    c = fb_db()
    try:
        rows = c.execute('SELECT * FROM fb WHERE uid=? AND id>? ORDER BY id', (uid, int(after or 0))).fetchall()
    finally:
        c.close()
    return {'messages': [_fb_msg(r) for r in rows]}
def fb_admin_list():
    c = fb_db()
    try:
        rows = c.execute('SELECT uid, MAX(id), COUNT(*), SUM(CASE WHEN author="player" AND seen=0 THEN 1 ELSE 0 END), MAX(ts) FROM fb GROUP BY uid ORDER BY MAX(id) DESC').fetchall()
        out = []
        for uid, mid, n, unseen, ts in rows:
            last = c.execute('SELECT * FROM fb WHERE id=?', (mid,)).fetchone()
            nm = c.execute('SELECT name, meta FROM fb WHERE uid=? AND author="player" AND name<>"" ORDER BY id DESC LIMIT 1', (uid,)).fetchone()
            out.append({'uid': uid, 'name': (nm[0] if nm else ''), 'count': n, 'unseen': unseen or 0, 'ts': ts, 'last': _fb_msg(last)})
        return {'threads': out}
    finally:
        c.close()
def fb_admin_seen(uid):
    with FB_LOCK:
        c = fb_db()
        try: c.execute('UPDATE fb SET seen=1 WHERE uid=?', (uid,)); c.commit()
        finally: c.close()

class H(BaseHTTPRequestHandler):
    server_version = 'ladder'
    def log_message(self, *a): pass
    def send(self, code, body, cache=60):
        b = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', ('public, max-age=%d' % cache) if cache else 'no-store')
        self.end_headers(); self.wfile.write(b)
    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > 65536: return {}
        try: return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception: return {}
    def send_html(self, body, cache=120):
        b = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'public, max-age=%d' % cache)
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); qs = urllib.parse.parse_qs(u.query)
        try:
            if u.path.startswith('/ladder/api/c/'): return community.handle(self, 'GET', u, None)
            if u.path in ('/ladder', '/ladder/', '/ladder/index.html'):
                try:
                    with open(os.path.dirname(os.path.abspath(__file__)) + '/ladder.html', 'rb') as f:
                        return self.send_html(f.read())
                except Exception:
                    return self.send(500, {'error': 'page'})
            if u.path == '/ladder/api/stats': return self.send(200, stats())
            if u.path == '/ladder/api/seasons': return self.send(200, {'seasons': seasons(), 'current': current_season()})
            if u.path == '/ladder/api/mm/status': return self.send(200, mm_status(), cache=0)
            if u.path == '/ladder/api/mm/live': return self.send(200, mm_live(qs.get('debug', ['0'])[0] in ('1', 'true')), cache=0)
            if u.path == '/ladder/api/history':
                g = lambda k: qs.get(k, [''])[0]
                return self.send(200, history(g('q')[:64], g('mode')[:8], g('map')[:64], g('race')[:32], g('page') or 0, g('n') or 30, g('off')), cache=10)
            if u.path == '/ladder/api/fb/thread': return self.send(200, fb_thread(qs.get('uid', [''])[0], qs.get('after', ['0'])[0]), cache=0)
            if u.path.startswith('/ladder/api/fb/img/'):
                fn = u.path.rsplit('/', 1)[-1]; uid = _fb_uid(qs.get('uid', [''])[0]); tok = qs.get('token', [''])[0]
                if not re.fullmatch(r'[0-9]+_[0-9a-f]{8}\.(png|jpg|gif|webp)', fn): return self.send(404, {'error': 'img'})
                c = fb_db()
                try: row = c.execute('SELECT 1 FROM fb WHERE images LIKE ? AND (uid=? OR ?)', ('%' + fn + '%', uid or '', 1 if _fb_token_ok(tok) else 0)).fetchone()
                finally: c.close()
                if not row: return self.send(404, {'error': 'img'})
                with open(FB_DIR + '/' + fn, 'rb') as f: b = f.read()
                self.send_response(200); self.send_header('Content-Type', 'image/' + fn.rsplit('.', 1)[1].replace('jpg', 'jpeg'))
                self.send_header('Content-Length', str(len(b))); self.send_header('Cache-Control', 'private, max-age=86400'); self.end_headers(); self.wfile.write(b); return
            if u.path == '/ladder/fb-admin':
                try:
                    with open(os.path.dirname(os.path.abspath(__file__)) + '/fb_admin.html', 'rb') as f: return self.send_html(f.read(), cache=0)
                except Exception: return self.send(500, {'error': 'page'})
            if u.path == '/ladder/api/fb/admin':
                if not _fb_token_ok(qs.get('token', [''])[0]): return self.send(403, {'error': 'token'}, cache=0)
                uid = qs.get('uid', [''])[0]
                if uid:
                    r = fb_thread(uid); fb_admin_seen(uid); return self.send(200, r, cache=0)
                return self.send(200, fb_admin_list(), cache=0)
            if u.path == '/ladder/api/board':
                season = (qs.get('season', [current_season()])[0])[:9]
                mode = qs.get('mode', ['1v1'])[0]
                if mode not in MODES: mode = '1v1'
                return self.send(200, {'season': season, 'mode': mode, 'board': board(season, mode)})
            return self.send(404, {'error': 'not found'})
        except Exception as e:
            return self.send(500, {'error': type(e).__name__})
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path.startswith('/ladder/api/c/'): return community.handle(self, 'POST', u, self._body())
            if u.path in ('/ladder/api/fb/send', '/ladder/api/fb/reply'):
                n = int(self.headers.get('Content-Length') or 0)
                if n <= 0 or n > 24 << 20: return self.send(413, {'error': 'size'}, cache=0)
                try: d = json.loads(self.rfile.read(n).decode('utf-8'))
                except Exception: return self.send(400, {'error': 'json'}, cache=0)
                if u.path == '/ladder/api/fb/send': return self.send(200, fb_send(d), cache=0)
                if not _fb_token_ok(d.get('token')): return self.send(403, {'error': 'token'}, cache=0)
                d['name'] = 'Elite dev'; return self.send(200, fb_send(d, 'dev'), cache=0)
            d = self._body()
            if isinstance(d, dict): d['_ua'] = self.headers.get('User-Agent', '')
            steam = str(d.get('steam', ''))[:32]
            if not steam.isdigit(): return self.send(400, {'error': 'steam'}, cache=0)
            with MM_LOCK: _touch(steam)
            if u.path == '/ladder/api/mm/hello': return self.send(200, mm_hello(steam), cache=0)
            if u.path == '/ladder/api/mm/join':  return self.send(200, mm_join(steam, d.get('mode', '1v1'), d), cache=0)
            if u.path == '/ladder/api/mm/started': return self.send(200, mm_started(steam), cache=0)
            if u.path == '/ladder/api/mm/ended': return self.send(200, mm_ended(steam), cache=0)
            if u.path == '/ladder/api/mm/alive': return self.send(200, mm_alive(steam), cache=0)
            if u.path == '/ladder/api/mm/ruined': return self.send(200, mm_ruined(steam, d.get('lobby', '')), cache=0)
            if u.path == '/ladder/api/mm/poll':  return self.send(200, mm_poll(steam), cache=0)
            if u.path == '/ladder/api/mm/host':  return self.send(200, mm_host(steam, d.get('lobby', ''), d.get('map')), cache=0)
            if u.path == '/ladder/api/mm/leave': return self.send(200, mm_leave(steam), cache=0)
            return self.send(404, {'error': 'not found'}, cache=0)
        except Exception as e:
            return self.send(500, {'error': type(e).__name__}, cache=0)

import community                                    # launcher community: Steam sign-in, who is online, the players' chat
community.ADMIN_OK = _fb_token_ok
def _c_rating(steam):
    i = _rating_info(steam, '1v1')
    return round(i[1]) if i[5] != 'new' else None
users.RATING = _c_rating
def _c_stats(steam):
    # the launcher's profile: 1v1 of the current season - games, wins, losses (one query by the profiles_steam index).
    # No place on the ladder: it meant building the whole season board for one player (plan, decision 4); old launchers
    # simply show no place for rank null
    season = current_season()
    with db() as c:
        r = c.execute('''SELECT r.games, r.wins, r.losses FROM ratings r JOIN profiles p ON p.profile_id=r.profile_id
                         WHERE p.steam=? AND r.mode='1v1' AND r.season=? ORDER BY r.games DESC LIMIT 1''', (str(steam), season)).fetchone()
    if not r: return {'season': season, 'games': 0, 'wins': 0, 'losses': 0, 'rank': None}
    return {'season': season, 'games': r[0], 'wins': r[1], 'losses': r[2], 'rank': None}
community.STATS = _c_stats
community.LIVE = stream.LIVE = live_view
users.ALIAS = lambda steam: _rating_info(steam, '1v1')[4]
def _c_mm_state(steam):
    with MM_LOCK:
        p = MM.get(steam)
        if p: return 'search' if p['state'] == 'searching' else 'match'
        if any(steam in m['host'] + m['guest'] for m in MM_PLAYING.values()): return 'match'
    return None
community.MM_STATE = _c_mm_state

class Server(ThreadingHTTPServer):
    # the listen backlog (socketserver's default is 5): every open launcher holds a stream, and after a restart they all
    # reconnect within a few seconds - with 5, most of them were refused (load test: 51 of 500 on the ramp up alone)
    request_queue_size = 1024

if __name__ == '__main__':
    threading.Thread(target=crawler, daemon=True).start()
    threading.Thread(target=_seasons_refresher, daemon=True).start()
    threading.Thread(target=_history_resolver, daemon=True).start()
    threading.Thread(target=stream.publisher, daemon=True).start()
    Server(('127.0.0.1', PORT), H).serve_forever()
