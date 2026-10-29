# Launcher community: Steam sign-in, who has the launcher open, the players' chat. Part of the ladder service
# (ladder.py routes /ladder/api/c/* here). Reading is open to everyone; writing needs a Steam sign-in.
#
# Sign-in (Steam OpenID 2.0, no API key): the launcher makes a random nonce and opens the browser at
#   /ladder/api/c/login?nonce=N -> Steam -> /ladder/api/c/return?nonce=N (checked with Steam: check_authentication)
# then asks /ladder/api/c/claim?nonce=N (once) for its session token. Name and avatar come from the public
# steamcommunity.com/profiles/<id>?xml=1 (no key needed), refreshed once a day.
#
# GET  login?nonce=          -> redirect to Steam            GET  return?...      <- Steam comes back here
# GET  claim?nonce=          -> {token, steam, name, avatar} (once, 10 minutes)
# POST local   {steam, name, device[, token]} -> sign-in with the Steam account logged in on this computer, no browser
#      (steamcommunity.com is blocked in Russia: the OpenID page never comes back there). The Steam id is the player's own
#      claim, so the account is bound to the first computer (device = hash of the machine id) that signs in with it; another
#      computer needs one sign-in through the browser (token of that session) to be bound too.
# Every step is written to the journal (journalctl -u dow2-ladder | grep community).
# POST me      {token}       -> the signed-in profile (name, avatar, rating)
# POST hello   {token?, state, uid?}  every ~30 s from the launcher: "I'm here" -> {online: [...]}
# GET  online                -> who has the launcher open (signed in, seen in the last 90 s)
# GET  msgs?room=&after=     -> {msgs: [...]} (the last 100, or the ones after id)
# POST say     {token, room, text}
# POST logout  {token}
# POST mod     {admin, op: del|ban|unban, id|steam}   (admin = the fb-admin token)
import html, json, os, re, secrets, sqlite3, threading, time, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = ROOT + '/data/community.sqlite'
BASE = 'https://dow2.fintebtc.de'                    # where Steam sends the player back (OpenID realm)
ROOMS = ('all', 'ru', 'en')
ONLINE_SECS = 90
RATING = None                                       # ladder.py: steam -> rating (1v1) or None
MM_STATE = None                                     # ladder.py: steam -> 'search' | 'match' | None
ADMIN_OK = None                                     # ladder.py: admin token -> bool
ALIAS = None                                        # ladder.py: steam -> the name the ladder / Relic knows (a Steam profile without a name)
LOCK = threading.Lock()
PRESENCE = {}                                       # steam -> {'seen', 'state'}  (in memory: who is here now)
RATE = {}                                           # steam -> [times of the last messages]

def log(event, **kv):
    print('community: %s %s' % (event, ' '.join('%s=%s' % (k, str(v)[:120].replace(' ', '_')) for k, v in kv.items())), flush=True)

def db():
    c = sqlite3.connect(DB, timeout=30)
    c.execute('CREATE TABLE IF NOT EXISTS device (steam TEXT, device TEXT, first INTEGER, last INTEGER, PRIMARY KEY (steam, device))')
    c.execute('CREATE TABLE IF NOT EXISTS sess (token TEXT PRIMARY KEY, steam TEXT, ts INTEGER)')
    c.execute('CREATE TABLE IF NOT EXISTS pending (nonce TEXT PRIMARY KEY, token TEXT, ts INTEGER)')
    c.execute('CREATE TABLE IF NOT EXISTS prof (steam TEXT PRIMARY KEY, name TEXT, avatar TEXT, ts INTEGER, banned INTEGER DEFAULT 0)')
    c.execute('CREATE TABLE IF NOT EXISTS msg (id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT, steam TEXT, text TEXT, ts INTEGER, deleted INTEGER DEFAULT 0)')
    c.execute('CREATE INDEX IF NOT EXISTS msg_room ON msg(room, id)')
    return c

def _get(url, data=None, timeout=15):
    r = urllib.request.Request(url, data=data, headers={'User-Agent': 'EliteLadder/1.0'})
    with urllib.request.urlopen(r, timeout=timeout) as f: return f.read().decode('utf-8', 'replace')

def profile(steam, refresh=False):
    with db() as c:
        row = c.execute('SELECT name, avatar, ts, banned FROM prof WHERE steam=?', (steam,)).fetchone()
    if row and not refresh and time.time() - row[2] < 86400: return {'name': row[0], 'avatar': row[1], 'banned': row[3]}
    name, avatar = (row[0], row[1]) if row else ('Player', '')
    try:
        x = _get('https://steamcommunity.com/profiles/%s/?xml=1' % steam)
        m = re.search(r'<steamID><!\[CDATA\[(.*?)\]\]></steamID>', x, re.S); name = (m and m.group(1).strip()[:48]) or name
        m = re.search(r'<avatarMedium><!\[CDATA\[(.*?)\]\]></avatarMedium>', x, re.S); avatar = (m and m.group(1).strip()) or avatar
    except Exception:
        pass
    if name == 'Player':
        try: name = (ALIAS and ALIAS(steam)) or name
        except Exception: pass
    with db() as c:
        c.execute('INSERT INTO prof(steam, name, avatar, ts) VALUES(?,?,?,?) ON CONFLICT(steam) DO UPDATE SET name=excluded.name, avatar=excluded.avatar, ts=excluded.ts',
                  (steam, name, avatar, int(time.time())))
    return {'name': name, 'avatar': avatar, 'banned': row[3] if row else 0}

def _session(token):
    if not token or len(str(token)) > 80: return None
    with db() as c:
        r = c.execute('SELECT steam FROM sess WHERE token=?', (str(token),)).fetchone()
    return r and r[0]

RCACHE = {}                                         # steam -> (rating, time): looked up in the background, at most every 30 min
def _rating(steam):
    v = RCACHE.get(steam)
    if not v or time.time() - v[1] > 1800:
        RCACHE[steam] = (v[0] if v else None, time.time())
        def look():
            try: RCACHE[steam] = (RATING and RATING(steam), time.time())
            except Exception: pass
        threading.Thread(target=look, daemon=True).start()
    return RCACHE[steam][0]

def _user(steam):
    p = profile(steam)
    return {'steam': steam, 'name': p['name'], 'avatar': p['avatar']}

# ---------- sign-in ----------
def login_url(nonce):
    ret = BASE + '/ladder/api/c/return?' + urllib.parse.urlencode({'nonce': nonce})
    q = {'openid.ns': 'http://specs.openid.net/auth/2.0', 'openid.mode': 'checkid_setup', 'openid.return_to': ret,
         'openid.realm': BASE + '/', 'openid.identity': 'http://specs.openid.net/auth/2.0/identifier_select',
         'openid.claimed_id': 'http://specs.openid.net/auth/2.0/identifier_select'}
    return 'https://steamcommunity.com/openid/login?' + urllib.parse.urlencode(q)

def openid_return(query):
    q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    nonce = q.get('nonce', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', nonce): return None, 'bad nonce'
    if q.get('openid.mode') == 'cancel': return None, 'cancelled on the Steam page'
    if not q.get('openid.return_to', '').startswith(BASE + '/ladder/api/c/return'): return None, 'bad return_to'
    m = re.fullmatch(r'https://steamcommunity\.com/openid/id/(\d{17})', q.get('openid.claimed_id', ''))
    if not m: return None, 'no Steam id'
    check = {k: v for k, v in q.items() if k.startswith('openid.')}; check['openid.mode'] = 'check_authentication'
    try: ok = 'is_valid:true' in _get('https://steamcommunity.com/openid/login', urllib.parse.urlencode(check).encode())
    except Exception as e: return None, 'Steam did not answer (%s)' % type(e).__name__
    if not ok: return None, 'Steam did not confirm the sign-in'
    steam = m.group(1); token = secrets.token_urlsafe(32); now = int(time.time())
    with db() as c:
        c.execute('INSERT INTO sess VALUES(?,?,?)', (token, steam, now))
        c.execute('INSERT OR REPLACE INTO pending VALUES(?,?,?)', (nonce, token, now))
        c.execute('DELETE FROM pending WHERE ts < ?', (now - 600,))
    return steam, None

def claim(nonce):
    with db() as c:
        r = c.execute('SELECT token, ts FROM pending WHERE nonce=?', (str(nonce)[:64],)).fetchone()
        if not r: return {'ok': False, 'wait': True}
        c.execute('DELETE FROM pending WHERE nonce=?', (str(nonce)[:64],))
    if time.time() - r[1] > 600: return {'ok': False, 'error': 'expired'}
    steam = _session(r[0])
    return dict(_user(steam), ok=True, token=r[0], rating=_rating(steam))

def local(d, ip=''):
    steam = str(d.get('steam', '')); dev = str(d.get('device', ''))[:64].lower()
    if not re.fullmatch(r'7656119\d{10}', steam): log('local-bad', ip=ip, steam=steam); return {'ok': False, 'error': 'no Steam account found on this computer'}
    if not re.fullmatch(r'[0-9a-f]{32,64}', dev): log('local-bad', ip=ip, steam=steam, device=dev[:12]); return {'ok': False, 'error': 'bad device id'}
    now = int(time.time())
    with db() as c:
        known = [r[0] for r in c.execute('SELECT device FROM device WHERE steam=?', (steam,))]
        if known and dev not in known and _session(d.get('token')) != steam:
            log('local-other-computer', ip=ip, steam=steam, device=dev[:12], known=len(known))
            return {'ok': False, 'need_browser': True, 'error': 'this Steam account signed in from another computer: confirm once through Steam in the browser'}
        c.execute('INSERT INTO device VALUES(?,?,?,?) ON CONFLICT(steam, device) DO UPDATE SET last=excluded.last', (steam, dev, now, now))
        token = secrets.token_urlsafe(32)
        c.execute('INSERT INTO sess VALUES(?,?,?)', (token, steam, now))
    p = profile(steam, refresh=True)
    nm = str(d.get('name', '')).strip()[:48]
    if p['name'] in ('', 'Player') and nm:
        with db() as c: c.execute('UPDATE prof SET name=? WHERE steam=?', (nm, steam))
    log('local-ok', ip=ip, steam=steam, device=dev[:12], first=not known)
    return dict(_user(steam), ok=True, token=token, rating=_rating(steam))

# ---------- presence and chat ----------
def hello(d):
    steam = _session(d.get('token'))
    state = d.get('state') if d.get('state') in ('menu', 'game') else 'menu'
    if steam:
        with LOCK: PRESENCE[steam] = {'seen': time.time(), 'state': state}
    return {'ok': True, 'signed_in': bool(steam), 'online': online()}

def online():
    now = time.time()
    with LOCK: here = [(s, p) for s, p in PRESENCE.items() if now - p['seen'] < ONLINE_SECS]
    out = []
    for s, p in here:
        u = _user(s); st = p['state']
        try: st = (MM_STATE and MM_STATE(s)) or st
        except Exception: pass
        out.append(dict(u, state=st, rating=_rating(s)))
    out.sort(key=lambda u: u['name'].lower())
    return out

def _msg(r):
    p = profile(r[2])
    return {'id': r[0], 'room': r[1], 'steam': r[2], 'name': p['name'], 'avatar': p['avatar'], 'text': r[3], 'ts': r[4]}

def msgs(room, after):
    room = room if room in ROOMS else 'all'
    try: after = int(after)
    except Exception: after = 0
    with db() as c:
        if after > 0:
            rows = c.execute('SELECT id, room, steam, text, ts FROM msg WHERE room=? AND id>? AND deleted=0 ORDER BY id LIMIT 200', (room, after)).fetchall()
        else:
            rows = c.execute('SELECT id, room, steam, text, ts FROM msg WHERE room=? AND deleted=0 ORDER BY id DESC LIMIT 100', (room,)).fetchall()[::-1]
        dead = [r[0] for r in c.execute('SELECT id FROM msg WHERE room=? AND deleted=1 AND id>? ORDER BY id DESC LIMIT 50', (room, max(0, after - 500)))]
    return {'room': room, 'msgs': [_msg(r) for r in rows], 'deleted': dead}

def say(d):
    steam = _session(d.get('token'))
    if not steam: return {'ok': False, 'error': 'sign in with Steam to write'}
    if profile(steam).get('banned'): return {'ok': False, 'error': 'you cannot write in the chat'}
    room = d.get('room') if d.get('room') in ROOMS else 'all'
    text = ''.join(ch for ch in str(d.get('text', '')) if ch == '\n' or ch.isprintable()).strip()[:500]
    if not text: return {'ok': False, 'error': 'empty'}
    now = time.time()
    with LOCK:
        t = [x for x in RATE.get(steam, []) if now - x < 60]
        if t and now - t[-1] < 1.5 or len(t) >= 20: return {'ok': False, 'error': 'too fast'}
        RATE[steam] = t + [now]
        PRESENCE.setdefault(steam, {'state': 'menu'})['seen'] = now
    with db() as c:
        cur = c.execute('INSERT INTO msg(room, steam, text, ts) VALUES(?,?,?,?)', (room, steam, text, int(now)))
    return {'ok': True, 'id': cur.lastrowid}

def me(d):
    steam = _session(d.get('token'))
    if not steam: return {'ok': False, 'signed_in': False}
    return dict(_user(steam), ok=True, signed_in=True, rating=_rating(steam))

def logout(d):
    with db() as c: c.execute('DELETE FROM sess WHERE token=?', (str(d.get('token', ''))[:80],))
    return {'ok': True}

def mod(d):
    if not (ADMIN_OK and ADMIN_OK(d.get('admin'))): return {'ok': False, 'error': 'token'}
    op = d.get('op')
    with db() as c:
        if op == 'del': c.execute('UPDATE msg SET deleted=1 WHERE id=?', (int(d.get('id', 0)),))
        elif op in ('ban', 'unban'):
            profile(str(d.get('steam', '')))
            c.execute('UPDATE prof SET banned=? WHERE steam=?', (1 if op == 'ban' else 0, str(d.get('steam', ''))))
        else: return {'ok': False, 'error': 'op'}
    return {'ok': True}

PAGE = '''<!doctype html><meta charset="utf-8"><title>Elite Launcher</title>
<style>body{background:#101614;color:#e8e2cf;font:16px system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0}
div{max-width:420px;text-align:center;line-height:1.5}b{color:#d4b25c}</style><div>%s</div>'''

def handle(h, method, u, d):
    p = u.path[len('/ladder/api/c/'):]
    ip = h.headers.get('X-Real-IP') or h.headers.get('X-Forwarded-For', '').split(',')[0] or h.client_address[0]
    if method == 'GET':
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if p == 'login':
            if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', qs.get('nonce', '')): return h.send(400, {'error': 'nonce'}, cache=0)
            log('login', ip=ip, nonce=qs['nonce'][:8], ua=h.headers.get('User-Agent', '')[:60])
            h.send_response(302); h.send_header('Location', login_url(qs['nonce'])); h.send_header('Cache-Control', 'no-store')
            h.send_header('Content-Length', '0'); h.end_headers(); return
        if p == 'return':
            steam, err = openid_return(u.query)
            log('return', ip=ip, nonce=qs.get('nonce', '')[:8], steam=steam or '-', error=err or '-')
            if err: return h.send_html(PAGE % ('Не получилось войти / Sign-in failed: %s' % html.escape(err)), cache=0)
            name = html.escape(profile(steam, refresh=True)['name'])
            return h.send_html(PAGE % ('Вы вошли как <b>%s</b>. Вернитесь в лаунчер.<br><br>Signed in as <b>%s</b>. Go back to the launcher.' % (name, name)), cache=0)
        if p == 'claim':
            r = claim(qs.get('nonce', ''))
            if not r.get('wait'): log('claim', ip=ip, nonce=qs.get('nonce', '')[:8], ok=r.get('ok'), steam=r.get('steam', '-'), error=r.get('error', '-'))
            return h.send(200, r, cache=0)
        if p == 'online': return h.send(200, {'online': online()}, cache=0)
        if p == 'msgs': return h.send(200, msgs(qs.get('room', 'all'), qs.get('after', '0')), cache=0)
        return h.send(404, {'error': 'not found'}, cache=0)
    if p == 'local': return h.send(200, local(d or {}, ip), cache=0)
    fn = {'hello': hello, 'say': say, 'me': me, 'logout': logout, 'mod': mod}.get(p)
    if not fn: return h.send(404, {'error': 'not found'}, cache=0)
    return h.send(200, fn(d or {}), cache=0)
