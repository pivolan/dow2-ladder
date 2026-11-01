#!/usr/bin/env python3
# Short summary of a launcher log bundle (.txt.gz from /srv/relicstats/logs) - 31.10.2026.
# A bundle is ~0.5 MB of text (13 MB for a day): the summary is a few lines - what is NEW since this player's previous
# bundle (Windows keeps crash events for 7 days, players send several bundles in a row), known signatures explained,
# noise dropped, and anything unrecognised pointed at (file + a line or two) so only that piece has to be read.
#
#   logsummary.py <bundle.txt.gz>        print the summary (stored in STATE/summaries/<id>.txt, the player's state updated)
#   logsummary.py --unreviewed           summaries of the bundles not reviewed yet (oldest first)
#   logsummary.py --mark-reviewed [id]   mark all (or up to and including <id>) as reviewed
import gzip, json, os, re, sqlite3, sys, time

LOGS = '/srv/relicstats/logs'
STATE = os.environ.get('LOGSUM_STATE', '/var/lib/elite-logsummary')
PLAYERS = os.path.join(STATE, 'players.json')
REVIEWED = os.path.join(STATE, 'reviewed.txt')
SUMS = os.path.join(STATE, 'summaries')

# known crash signatures -> what they mean
KNOWN = [
    (r'BEX64 Elite\.exe!Elite\.exe\+0*78da5', 'лаунчер мода Elite.exe не дождался окна игры за 60 с и упал (код команды Elite; игра запускается дальше)'),
    (r'AppHang', 'зависание (Windows: «не отвечает»)'),
    (r'вне модулей', 'код в памяти без модуля - перехватчик/патч (наш или чужой)'),
]
NOISE = re.compile(r'SyncErrorChecking|Remove CRC BLOCK|tick alive|Unable to bind updater for fx|Transport|MessageCounts|PeerID|'
                   r'Badge::LoadCustom|game type \d+ \(not single player\)|is not reachable, now using|temp CreateDevice failed|'
                   r'Time between calls was|between calls!', re.I)
BAD = re.compile(r'(?i:error|exception|fatal|crash|assert|fail|mismatch|not recogn)|NOT ')
# the game's own warnings.txt is mostly noise: only what really breaks a game
WARN_BAD = re.compile(r'FATAL|Checksum fail|SyncError(?! *Checking)|out of sync|Access violation|Out of memory|ASSERT', re.I)


def sections(txt):
    out, cur, buf = {}, None, []
    for line in txt.split('\n'):
        if line.startswith('=== '):
            if cur: out[cur] = '\n'.join(buf)
            cur, buf = line[4:], []
        else: buf.append(line.rstrip('\r'))
    if cur: out[cur] = '\n'.join(buf)
    return out


def base(k): return re.sub(r'.*[\\/]', '', k.split(' (')[0])


def last_session(text, start_re):     # the lines after the last "... start" line (logs carry times of day, no dates)
    lines = text.split('\n'); idx = [i for i, l in enumerate(lines) if re.search(start_re, l)]
    return lines[idx[-1]:] if idx else lines[-300:]


def crash_events(v):
    for ev in v.split('Event[')[1:]:
        dt = re.search(r'Date: (\S+)', ev); P = dict(re.findall(r'(P\d+): (.*)', ev))
        ty = re.search(r': (APPCRASH|AppHang\w*|BEX64|BEX|MoAppCrash)\s', ev)
        if not (dt and P.get('P1') and ty) or 'StackHash' in ev: continue   # StackHash = no module: the 1000 event below has the address
        ty = ty.group(1)
        if 'Hang' in ty: sig = '%s %s %s' % (ty, P['P1'].strip(), P.get('P4', '').strip())
        else: sig = '%s %s!%s+%s %s' % (ty, P['P1'].strip(), P.get('P4', '').strip(), P.get('P7', '').strip(), P.get('P8', '').strip())
        yield dt.group(1)[:19], sig.strip()
    # Application Error 1000 with an unknown module: the address is the useful part
    for ev in v.split('Event[')[1:]:
        if 'Event ID: 1000' not in ev: continue
        dt = re.search(r'Date: (\S+)', ev); ad = re.findall(r'0x[0-9a-f]{6,8}', ev)
        if dt and 'unknown' in ev and len(ad) >= 4: yield dt.group(1)[:19], 'APPCRASH DOW2.exe код %s по адресу %s (вне модулей)' % (ad[2], ad[3])


def explain(sig):
    for p, t in KNOWN:
        if re.search(p, sig): return t
    return None


def summarize(path):
    name = os.path.basename(path); rid = name.split('_')[0]; who = name[len(rid) + 1:-7]
    t = gzip.open(path).read().decode('utf-8', 'replace').replace('\x00', '')
    S = sections(t); head = t.split('\n=== ')[0]
    steam = (re.search(r'start, steam=(\d+)', t) or re.search(r'Found profile: /steam/(\d+)', t) or [None, ''])[1]
    uid = ''
    try: uid = open(os.path.join(LOGS, rid + '.uid')).read().strip()
    except Exception: pass
    key = steam or uid or who
    os.makedirs(SUMS, exist_ok=True)
    try: players = json.load(open(PLAYERS))
    except Exception: players = {}
    prev = players.get(key, {})
    since = prev.get('last_event') or time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - 86400))
    nick = ''
    if steam:
        try: nick = (sqlite3.connect('/srv/ladder/data/ladder.sqlite').execute('SELECT alias FROM profiles WHERE steam=? LIMIT 1', (steam,)).fetchone() or [''])[0]
        except Exception: pass
    L = []
    lv = re.search(r'Elite Launcher (\S+)', head); ev_ = re.search(r'Elite (\d[\d.]*) \((\w+)\)', head)
    L.append('📋 %s%s%s · лаунчер %s · Elite %s · id %s%s' % (who, ' · ник ' + nick if nick else '', ' · steam ' + steam if steam else '',
             lv.group(1) if lv else '?', ev_.group(1) if ev_ else '?', rid, ' · повтор (прошлый %s)' % prev['last_rid'] if prev.get('last_rid') else ''))
    on = re.findall(r'^  (\w+): ON \(latest ([^,]+), installed ([^,)]+)', head, re.M)
    hand = re.findall(r'hand-installed copy: ([^)]+)', head)
    dp = re.search(r'^dow2-patches: (.*)$', head, re.M)
    L.append('дополнения вкл: ' + (', '.join('%s %s%s' % (i, inst, '' if inst == lat else ' (есть %s)' % lat) for i, lat, inst in on) or 'нет')
             + ('; ручные копии: ' + ', '.join(hand) if hand else '') + ('; dow2-patches: ' + dp.group(1) if dp and dp.group(1) != '-' else ''))
    # crashes since the previous bundle of this player
    evs, newest = {}, prev.get('last_event', '')
    for k, v in S.items():
        if k.startswith('Windows event log'):
            for dt, sig in crash_events(v):
                newest = max(newest, dt)
                if dt > since: evs.setdefault(sig, []).append(dt)
    if evs:
        L.append('падения после прошлого лога:')
        for sig, dts in sorted(evs.items(), key=lambda x: -len(x[1])):
            e = explain(sig)
            L.append('  %s× %s (%s…%s)%s' % (len(set(dts)), sig, min(dts)[5:16].replace('T', ' '), max(dts)[5:16].replace('T', ' '), ' - ' + e if e else ''))
    else: L.append('новых падений нет')
    dumps = [base(k) for k in S if base(k).endswith('.dmp')]
    if dumps: L.append('дампы: ' + ', '.join(dumps) + (' (больше лимита, не приложены)' if any('too big' in k for k in S) else ''))
    # the last session of each add-on log: what went wrong
    starts = {'Relay.log': r'Relay \d+ start', 'MMFind.log': r'MMFind \d\.\d+ start', 'LobbyStats.log': r'LobbyStats [\d.]+ ', 'MatchTimer.log': r'MatchTimer|Direct3DCreate9',
              'Cooldowns.log': r'Cooldowns \d', 'LoadingRatings.log': r'LoadingRatings', 'ObsFix.log': r'ObsFix', 'Reverse.log': r'Reverse \d', 'EliteAI.log': r'EliteAI', 'warnings.txt': r'^\S+\s+(Logfile|.*Starting)'}
    unknown = []
    for k, v in S.items():
        b = base(k)
        if b == 'NetProbe.log':                         # the connection check (experiment): per server what passed
            sess = last_session(v, r'NetProbe \d+:')
            res = {}
            for l in sess:
                m = re.search(r'(Poland|Germany|Moscow)\s+(tcp|udp)/(\d+)\s*(plain|split)?:\s*(.*)', l)
                if not m: continue
                srv, proto, port, kind, rest = m.groups()
                if proto == 'tcp': short = 'OK' if rest.startswith('OK') else ('стоп %s' % re.search(r'at (\d+ KB)', rest).group(1) if 'STALL' in rest else 'нет')
                else:
                    e = re.search(r'echo (\d+)/40.*burst (\d+)/200', rest); short = ('эхо %s/40 поток %s/200' % e.groups()) if e else 'нет'
                res.setdefault(srv, []).append('%s%s/%s %s' % (proto, '(split)' if kind == 'split' else '', port, short))
            if res: L.append('проверка связи: ' + ' | '.join('%s: %s' % (srv, ', '.join(x)) for srv, x in res.items()))
            continue
        if b not in starts: continue
        sess = last_session(v, starts[b])
        if b == 'MMFind.log':
            fails = [re.sub(r'^\S+ ', '', l) for l in sess if re.search(r'-> fail|fell apart|did not start|could not|NOT installed|mismatch|no \w+ map|cancelled by|Cancel pressed', l)]
            loads = sum('loading screen' in l for l in sess); ver = (re.findall(r'MMFind (\d\.\d+) start', v) or ['?'])[-1]
            if fails or loads: L.append('автоматч %s: загрузок матча %d%s' % (ver, loads, '; ' + '; '.join(sorted(set(fails))[:4]) if fails else ''))
            continue
        hits = [l.strip() for l in sess if (WARN_BAD if b == 'warnings.txt' else BAD).search(l) and not NOISE.search(l)]
        if b == 'Relay.log':
            fat = [re.sub(r'^\S+ ', '', l) for l in hits if 'FATAL' in l or 'does not answer' in l]
            ver = (re.search(r'Relay (\d+) start', sess[0]) or [0, '?'])[1] if sess else '?'
            if fat: L.append('Relay %s: %s' % (ver, '; '.join(sorted(set(fat))[:3])))
            hits = [h for h in hits if 'FATAL' not in h and 'does not answer' not in h]
        if b == 'warnings.txt':
            fe = [h for h in hits if 'FATAL' in h]
            if fe: L.append('игра: ' + '; '.join(sorted(set(fe))[:2]))
            ds = [h for h in hits if re.search(r'Checksum fail|sync', h, re.I)]
            if ds:
                # who left the common checksum: the odd peer(s), this player's own station, the odd peer's address
                fr = re.search(r'Checksum fail on frame (\d+)', '\n'.join(sess)); sums = {}
                for l in sess:
                    m = re.search(r'Checksum for peer (\d+) = \[(-?\d+)\]', l)
                    if m: sums[m.group(1)] = m.group(2)
                me = (re.findall(r'registered local station \[([0-9a-f]+)\]', v) or ['?'])[-1]   # whole file: the session split cuts at the match start
                me = str(int(me, 16)) if me != '?' else me
                if sums:
                    vals = list(sums.values()); major = max(set(vals), key=vals.count)
                    odd = [p for p, v in sums.items() if v != major]
                    addr = {p: (re.findall(r'accepted Peer %s into the match at address list = WINaddr:([\d.]+)' % p, v) or ['?'])[-1] for p in odd}
                    L.append('игра: рассинхрон на кадре %s - разошлись peer %s из %d (%s); этот игрок peer %s %s' % (
                        fr.group(1) if fr else '?', ', '.join('%s [%s]' % (p, addr[p]) for p in odd), len(sums),
                        'адрес 26.x = Radmin VPN' if any(a.startswith('26.') for a in addr.values()) else 'адреса из лога игры',
                        me, 'в большинстве - виноват не он' if me not in odd else 'РАЗОШЁЛСЯ САМ'))
                else: L.append('игра: рассинхрон (%d строк, первая «%s»)' % (len(ds), ds[0][:90]))
            hits = [h for h in hits if 'FATAL' not in h and h not in ds]
        if hits: unknown.append((b, len(hits), hits[:2]))
    if unknown:
        L.append('непонятное (смотреть сырой лог):')
        for b, n, ex in unknown[:5]: L.append('  %s: %d строк, напр. «%s»' % (b, n, ex[0][:140]))
    out = '\n'.join(L)
    open(os.path.join(SUMS, rid + '.txt'), 'w').write(out + '\n')
    players[key] = {'last_event': newest, 'last_rid': rid, 'who': who}
    json.dump(players, open(PLAYERS + '.tmp', 'w'), ensure_ascii=False, indent=1); os.replace(PLAYERS + '.tmp', PLAYERS)
    return out


def all_rids(): return sorted(f.split('_')[0] for f in os.listdir(LOGS) if f.endswith('.txt.gz'))


def reviewed():
    try: return set(open(REVIEWED).read().split())
    except Exception: return set()


if __name__ == '__main__':
    a = sys.argv[1:]
    os.makedirs(SUMS, exist_ok=True)
    if a and a[0] == '--unreviewed':
        rv = reviewed()
        for rid in [r for r in all_rids() if r not in rv]:
            p = os.path.join(SUMS, rid + '.txt')
            if not os.path.exists(p):
                f = [x for x in os.listdir(LOGS) if x.startswith(rid + '_') and x.endswith('.txt.gz')]
                if f: summarize(os.path.join(LOGS, f[0]))
            if os.path.exists(p): print(open(p).read())
    elif a and a[0] == '--mark-reviewed':
        upto = a[1] if len(a) > 1 else None
        rv = reviewed() | {r for r in all_rids() if upto is None or r <= upto}
        open(REVIEWED, 'w').write('\n'.join(sorted(rv)) + '\n'); print('reviewed: %d bundles' % len(rv))
    elif a:
        print(summarize(a[0]))
    else:
        print(__doc__ if __doc__ else 'usage: logsummary.py <bundle.txt.gz> | --unreviewed | --mark-reviewed [id]')
