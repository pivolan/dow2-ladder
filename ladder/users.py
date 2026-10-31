# Launcher users: one record per Steam account for the chat, the Online block and the player menu. Part of the ladder
# service. Requests read memory only (USERS, loaded at start, written through to community.sqlite); Steam is asked by a
# background thread, at most once a second (the public ?xml=1 profile, refreshed once a day), never inside a request.
# Name everywhere: the Steam name, else the name the ladder / Relic / MMFind knows (alias), else 'Player'.
#
# Presence (PRES): who has the launcher open (c/hello, the open stream: 45 s after the last sign) and who talks to the
# ladder from the game (MMFind). The 1v1 rating is kept per user: refreshed every 30 min for those online and right
# after a match they played.
import queue, re, sqlite3, threading, time, urllib.request

DB = None                                           # community.py: the community.sqlite path
ALIAS = None                                        # ladder.py: steam -> the ladder / Relic name
RATING = None                                       # ladder.py: steam -> 1v1 rating or None
LAUNCHER_SECS = 45
PROFILE_SECS = 86400
RATING_SECS = 1800
LOCK = threading.Lock()
USERS = {}                                          # steam -> {name, avatar, avatar_ts, alias, banned, first_seen, last_seen, rating, rating_ts}
PRES = {}                                           # steam -> {launcher_ts, lstate: menu|game, game_ts}
DIRTY = set()                                       # written by the flusher once a minute
_pq, _pqueued = queue.Queue(), set()                # Steam profiles to fetch
_rq, _rqueued = queue.Queue(), set()                # ratings to look up

def db():
    c = sqlite3.connect(DB, timeout=30); c.execute('PRAGMA journal_mode=WAL'); return c

def load():
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS users (steam TEXT PRIMARY KEY, name TEXT, avatar TEXT, avatar_ts INTEGER,
                     alias TEXT, relic_pid INTEGER, banned INTEGER DEFAULT 0, first_seen INTEGER, last_seen INTEGER)''')
        if not c.execute('SELECT 1 FROM users LIMIT 1').fetchone():   # the first start: copied once from prof (prof stays as a fallback)
            c.execute("INSERT OR IGNORE INTO users(steam, name, avatar, avatar_ts, banned, first_seen, last_seen) "
                      "SELECT steam, CASE WHEN name='Player' THEN '' ELSE name END, avatar, ts, banned, ts, ts FROM prof")
        rows = c.execute('SELECT steam, name, avatar, avatar_ts, alias, banned, first_seen, last_seen FROM users').fetchall()
    with LOCK:
        for r in rows:
            USERS[r[0]] = {'name': r[1] or '', 'avatar': r[2] or '', 'avatar_ts': r[3] or 0, 'alias': r[4] or '', 'banned': r[5] or 0,
                           'first_seen': r[6] or 0, 'last_seen': r[7] or 0, 'rating': None, 'rating_ts': 0}
    for f in (_profiles, _ratings, _flusher): threading.Thread(target=f, daemon=True).start()

def display_name(u):
    return u['name'] or u['alias'] or 'Player'

def _user(steam, now):  # under LOCK: the record, a stub for a Steam id never seen
    u = USERS.get(steam)
    if u is None:
        u = USERS[steam] = {'name': '', 'avatar': '', 'avatar_ts': 0, 'alias': '', 'banned': 0, 'first_seen': int(now), 'last_seen': 0,
                            'rating': None, 'rating_ts': 0}
        DIRTY.add(steam)
    return u

def get(steam, rated=False):
    # {steam, name, avatar, rating, banned} from memory; an unknown or day-old profile is fetched from Steam in the background,
    # the rating looked up once if asked for (the Online block, the own card - not every chat author)
    steam = str(steam); now = time.time()
    with LOCK:
        u = _user(steam, now)
        out = {'steam': steam, 'name': display_name(u), 'avatar': u['avatar'], 'rating': u['rating'], 'banned': u['banned']}
        old, unrated = now - u['avatar_ts'] > PROFILE_SECS, rated and not u['rating_ts']
    if old: _enqueue(_pq, _pqueued, steam)
    if unrated: _enqueue(_rq, _rqueued, steam)
    return out

def card(steam):  # what the Online block and the stream show of a person
    u = get(steam, True)
    return {'name': u['name'], 'avatar': u['avatar'], 'rating': u['rating']}

def rating(steam):
    return get(steam, True)['rating']

def _enqueue(q, queued, steam):
    with LOCK:
        if steam in queued: return
        queued.add(steam)
    q.put(steam)

def refresh_rating(steams):  # right after a match: its players' ratings are looked up again
    for s in steams: _enqueue(_rq, _rqueued, str(s))

def set_alias(steam, name):  # the name the ladder / Relic / MMFind knows: shown while Steam gives none
    name = str(name or '').strip()[:48]
    if not name or name == 'Player': return
    with LOCK:
        u = _user(str(steam), time.time())
        if u['alias'] != name: u['alias'] = name; DIRTY.add(str(steam))

def set_banned(steam, on):
    with LOCK: _user(str(steam), time.time())['banned'] = 1 if on else 0
    _write([str(steam)])

def fetch_now(steam, timeout=8):
    # at sign-in only: the name for the "signed in as" page and the first card (once per sign-in, not on a hot path)
    _fetch(str(steam), timeout)

def _fetch(steam, timeout=15):
    try:
        r = urllib.request.Request('https://steamcommunity.com/profiles/%s/?xml=1' % steam, headers={'User-Agent': 'EliteLadder/1.0'})
        with urllib.request.urlopen(r, timeout=timeout) as f: x = f.read().decode('utf-8', 'replace')
    except Exception:
        x = None
    name = avatar = None
    if x:
        m = re.search(r'<steamID><!\[CDATA\[(.*?)\]\]></steamID>', x, re.S); name = m and m.group(1).strip()[:48]
        m = re.search(r'<avatarMedium><!\[CDATA\[(.*?)\]\]></avatarMedium>', x, re.S); avatar = m and m.group(1).strip()
    alias = None
    if not name:
        try: alias = ALIAS and ALIAS(steam)
        except Exception: pass
    with LOCK:
        u = _user(steam, time.time())
        if name: u['name'] = name
        if avatar: u['avatar'] = avatar
        if alias: u['alias'] = str(alias)[:48]
        u['avatar_ts'] = int(time.time()) if x else int(time.time()) - PROFILE_SECS + 3600   # Steam did not answer: again in an hour
    _write([steam])

def _profiles():
    while True:
        steam = _pq.get()
        with LOCK: _pqueued.discard(steam)
        t = time.time(); _fetch(steam)
        time.sleep(max(0, 1 - (time.time() - t)))   # at most one Steam request a second

def _ratings():
    # the queue first (new people, finished matches); idle: those online whose rating is older than 30 min
    while True:
        try: steam = _rq.get(timeout=60)
        except queue.Empty:
            now = time.time()
            with LOCK:
                for s in [s for s in online(now) if now - USERS.get(s, {}).get('rating_ts', 0) > RATING_SECS]: _rqueued.add(s); _rq.put(s)
            continue
        with LOCK: _rqueued.discard(steam)
        try: r = RATING and RATING(steam)
        except Exception: r = None
        with LOCK:
            u = _user(steam, time.time()); u['rating'] = r; u['rating_ts'] = time.time()

def _write(steams):
    with LOCK:
        rows = [(s, u['name'], u['avatar'], u['avatar_ts'], u['alias'], u['banned'], u['first_seen'], u['last_seen'])
                for s in steams for u in [USERS.get(s)] if u]
        DIRTY.difference_update(steams)
    if not rows: return
    with db() as c:
        c.executemany('''INSERT INTO users(steam, name, avatar, avatar_ts, alias, banned, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?)
                         ON CONFLICT(steam) DO UPDATE SET name=excluded.name, avatar=excluded.avatar, avatar_ts=excluded.avatar_ts,
                         alias=excluded.alias, banned=excluded.banned, last_seen=excluded.last_seen''', rows)

def _flusher():
    while True:
        time.sleep(60)
        with LOCK:
            todo = list(DIRTY)
            old = [s for s, p in PRES.items() if time.time() - max(p['launcher_ts'], p['game_ts']) > 600]
            for s in old: PRES.pop(s, None)
        try: _write(todo)
        except Exception as e: print('users: flush %s' % e, flush=True)

# ---------- presence ----------
def _pres(steam):  # under LOCK
    return PRES.setdefault(steam, {'launcher_ts': 0, 'lstate': 'menu', 'game_ts': 0})

def launcher_seen(steam, state=None):
    # c/hello and the open stream (connect, every ping); state only when the launcher says it (menu / game)
    steam = str(steam); now = time.time()
    with LOCK:
        p = _pres(steam); p['launcher_ts'] = now
        if state in ('menu', 'game'): p['lstate'] = state
        u = _user(steam, now)
        if now - u['last_seen'] > 60: u['last_seen'] = int(now); DIRTY.add(steam)

def game_seen(steam):  # MMFind talked to the ladder (ladder._touch)
    with LOCK: _pres(str(steam))['game_ts'] = time.time()

def in_launcher(now=None):  # [(steam, menu|game)] of those with the launcher open now
    now = now or time.time()
    with LOCK: return sorted(((s, p['lstate']) for s, p in PRES.items() if now - p['launcher_ts'] < LAUNCHER_SECS), key=lambda x: x[0])

def online(now):  # under LOCK: anyone seen from the launcher or the game in the last 2 minutes
    return [s for s, p in PRES.items() if now - max(p['launcher_ts'], p['game_ts']) < 120]
