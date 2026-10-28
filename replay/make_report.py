#!/usr/bin/env python3
"""Аналитический md-отчёт по реплею DoW2 (v2): разбежки, бои, захваты по именам точек,
потери, гипотеза победителя. Использует item_tables.json и maps_points.json."""
import sys, json, struct, os, math
sys.path.insert(0, '/var/www/dow_replay')
from dow2rec import parse
from collections import Counter, defaultdict

BASE = os.environ.get('DOW_BASE') or os.path.dirname(os.path.abspath(__file__))
IT = json.load(open(BASE + '/item_tables.json'))['tables']
MAPS = json.load(open(BASE + '/maps_points.json'))
RACE_DIR = {'chaos_marines': 'race_chaos', 'orks': 'race_ork', 'space_marines': 'race_marine',
            'eldar': 'race_eldar', 'imperial_guard': 'race_imperial_guard', 't_tau': 'race_tau',
            'tyranids': 'race_tyranid', 'grey_knights': 'race_gk', 'gk': 'race_gk',
            'necrons': 'race_necron', 'necron': 'race_necron'}
RACE_RU = {'chaos_marines': 'хаос', 'orks': 'орки', 'space_marines': 'СМ', 'eldar': 'эльдары',
           'imperial_guard': 'ИГ', 't_tau': 'тау', 'tyranids': 'тираниды', 'grey_knights': 'СР'}
COMBAT = {0x30, 0x31, 0x35, 0x32, 0x46}

def ts(t): return '%d:%02d' % (t // 600, t // 10 % 60)

def sqs_of(a):
    if len(a) > 11 and a[10] == 0xc3 and a[8] == 0x20: return [a[11]]
    if len(a) > 9 and 0x41 <= a[8] <= 0x4f: return [a[9 + i*4] for i in range(a[8] - 0x40)]
    return []

def move_xy(a):
    if len(a) >= 27 and a[10] == 0xc3:
        try:
            x, h, y = struct.unpack_from('<fff', a, 15)
            if abs(x) < 600 and abs(y) < 600: return (x, y)
        except Exception: pass
    return None

def target_sq(a):
    return a[-2] if a and a[-1] == 0xc3 else None

def resolve(table, kind, item, race):
    lst = IT[table][kind]
    rd = RACE_DIR.get(race, '@@')
    for idx in ([item, item - 1] if kind == 'sbps' else [item - 1, item, item - 2]):
        if 0 <= idx < len(lst) and rd in lst[idx]:
            return lst[idx].split('/')[-1].replace('.rbf', '')
    for idx in ([item, item - 1] if kind == 'sbps' else [item - 1, item]):
        if 0 <= idx < len(lst):
            return lst[idx].split('/')[-1].replace('.rbf', '') + '?'
    return f'id{item}'

def short(name):
    for p in ('ork_', 'tau_', 'csm_', 'sm_', 'eld_', 'ig_', 'tyr_', 'gk_', 'nec_', 'ap_'):
        name = name.replace(p, '')
    return name

def fix_ticks(actions):
    out, last = [], 0
    for tick, pid, at, a in actions:
        if tick == 0 and last > 100: tick = last
        last = max(last, tick)
        out.append((tick, pid, at, a))
    return out

def octant(x, y):
    d = ['В', 'СВ', 'С', 'СЗ', 'З', 'ЮЗ', 'Ю', 'ЮВ']
    if x*x + y*y < 400: return 'центр'
    return d[int(((math.degrees(math.atan2(y, x)) + 382.5) % 360) // 45)]

KIND_RU = {'victory_point_02': 'VP', 'capture_point': 'энергоузел',
           'requisition_point': 'реквизиция', 'starting_point': 'база'}

def report(path):
    meta, actions = parse(path)
    actions = fix_ticks(actions)
    players = [p for p in meta['players'] if p['race']]
    dur_ticks = meta['ticks']; dur = dur_ticks / 600
    mapname = meta['map'].split('\\')[-1]
    mpoints = MAPS.get(mapname, [])
    pid_act = Counter(a[1] for a in actions)
    pids = sorted(pid_act)

    # --- pid → раса по item id построек; затем pid → слот
    def race_of_items(pid, table):
        sc = Counter()
        for t, q, at, a in actions:
            if q == pid and at == 0x03 and len(a) >= 18:
                item = struct.unpack_from('<I', a, 14)[0]
                lst = IT[table]['sbps']
                if 0 <= item < len(lst):
                    for race, rd in RACE_DIR.items():
                        if rd in lst[item]: sc[race] += 1
        return sc.most_common(1)[0][0] if sc else None
    table = 'elite30' if any(p['race'] in ('t_tau','tyranids','grey_knights','necrons') for p in players) else None
    if table is None:
        sc_e = sum(1 for pid in pids if race_of_items(pid, 'elite30') in [p['race'] for p in players])
        sc_v = sum(1 for pid in pids if race_of_items(pid, 'vanilla') in [p['race'] for p in players])
        table = 'elite30' if sc_e >= sc_v else 'vanilla'
    pid2p, used = {}, set()
    for pid in pids:
        r = race_of_items(pid, table)
        cand = [i for i, p in enumerate(players) if i not in used and p['race'] == r]
        if not cand:
            cand = [i for i in range(len(players)) if i not in used]
        if cand:
            pid2p[pid] = players[cand[0]]; used.add(cand[0])
    def pname(pid): return pid2p.get(pid, {}).get('name', f'p{pid}')
    def prace(pid): return pid2p.get(pid, {}).get('race', '')
    def pteam(pid): return pid2p.get(pid, {}).get('team', -1)

    # --- отряды: id → имя юнита
    first_seen, last_seen, cmd_n = {}, {}, Counter()
    for t, pid, at, a in actions:
        for s in sqs_of(a):
            k = (pid, s)
            first_seen.setdefault(k, t); last_seen[k] = t; cmd_n[k] += 1
    builds = [(t, pid, struct.unpack_from('<I', a, 14)[0]) for t, pid, at, a in actions
              if at == 0x03 and len(a) >= 18]
    sqname = {}
    unmatched = sorted(builds)
    for k in sorted(first_seen, key=lambda k: first_seen[k]):
        pid, s = k
        if first_seen[k] < 150:
            sqname[k] = 'старт.отряд/герой'
            continue
        best = None
        for i, (bt, bpid, item) in enumerate(unmatched):
            if bpid == pid and 100 <= first_seen[k] - bt <= 900:
                best = i; break
        if best is not None:
            bt, bpid, item = unmatched.pop(best)
            sqname[k] = short(resolve(table, 'sbps', item, prace(pid)))
        else:
            sqname[k] = 'отряд'
    # герой = самый первый отряд игрока
    for pid in pids:
        own = [k for k in first_seen if k[0] == pid]
        if own:
            hero = min(own, key=lambda k: (first_seen[k], k[1]))
            sqname[hero] = 'ГЕРОЙ'
    def sqn(pid, s): return sqname.get((pid, s), 'отряд')

    # --- координаты рантайм-точек по кликам + имена из карты
    last_move = {}
    ent_est = defaultdict(list)
    cap_events = []       # (tick, pid, sq, ent)
    for t, pid, at, a in actions:
        xy = move_xy(a)
        if xy:
            for s in sqs_of(a): last_move[(pid, s)] = (t, xy)
        if at == 0x2F:
            ent = struct.unpack_from('<I', a, len(a) - 4)[0]
            for s in sqs_of(a):
                lm = last_move.get((pid, s))
                if lm and t - lm[0] <= 400: ent_est[ent].append(lm[1])
                cap_events.append((t, pid, s, ent))
    def med(v): v = sorted(v); return v[len(v)//2]
    ent_name = {}
    cx = med([p['x'] for p in mpoints]) if mpoints else 0
    cy = med([p['y'] for p in mpoints]) if mpoints else 0
    est_xy = {ent: (med([c[0] for c in v]), med([c[1] for c in v])) for ent, v in ent_est.items()}
    if mpoints:
        # уникальное жадное сопоставление entity ↔ точка карты
        cand = []
        caps_pts = [p for p in mpoints if p['kind'] != 'starting_point']
        for ent, (ex, ey) in est_xy.items():
            for j, p in enumerate(caps_pts):
                cand.append((math.hypot(p['x']-ex, p['y']-ey), ent, j))
        cand.sort()
        used_e, used_p = set(), set()
        for d, ent, j in cand:
            if d > 60 or ent in used_e or j in used_p: continue
            p = caps_pts[j]
            ent_name[ent] = f"{KIND_RU.get(p['kind'], p['kind'])}-{octant(p['x']-cx, p['y']-cy)}"
            used_e.add(ent); used_p.add(j)
    for ent, (ex, ey) in est_xy.items():
        ent_name.setdefault(ent, f"точка({ex:+.0f},{ey:+.0f})")

    # --- базы/стороны
    base_side = {}
    for pid in pids:
        early = [move_xy(a) for t, q, at, a in actions if q == pid and t < 300 and move_xy(a)]
        if early:
            bx = med([e[0] for e in early]); by = med([e[1] for e in early])
            base_side[pid] = (bx, by)
    def enemy_half_share(pid, t0, t1):
        mine = base_side.get(pid)
        if not mine: return None
        pts = [move_xy(a) for t, q, at, a in actions if q == pid and t0 <= t < t1 and move_xy(a)]
        if not pts: return None
        far = sum(1 for x, y in pts if (x-mine[0])**2 + (y-mine[1])**2 >
                  (x-(-mine[0]))**2 + (y-(-mine[1]))**2)
        return far / len(pts)

    # --- бои: кластеры боевых команд с целью-врагом
    sq_owner = {s: pid for (pid, s) in first_seen}
    combat_ev = []
    for t, pid, at, a in actions:
        if at in COMBAT:
            tsq = target_sq(a)
            enemy = tsq is not None and sq_owner.get(tsq) is not None and pteam(sq_owner[tsq]) != pteam(pid)
            combat_ev.append((t, pid, at, a, tsq if enemy else None))
    fights = []
    cur = None
    for ev in combat_ev:
        if cur and ev[0] - cur['end'] <= 100 and ev[0] - cur['start'] <= 1200:
            cur['end'] = ev[0]; cur['ev'].append(ev)
        else:
            if cur and len(cur['ev']) >= 8: fights.append(cur)
            cur = {'start': ev[0], 'end': ev[0], 'ev': [ev]}
    if cur and len(cur['ev']) >= 8: fights.append(cur)
    rts = [(t, pid, sqs_of(a)) for t, pid, at, a in actions if at == 0x3D]

    def fight_desc(f):
        parts = Counter()
        tgt_pairs = set()
        for t, pid, at, a, tsq in f['ev']:
            for s in sqs_of(a): parts[(pid, s)] += 1
            if tsq is not None: tgt_pairs.add((pname(pid), pname(sq_owner[tsq])))
        locs = [last_move[k][1] for k in parts if k in last_move]
        loc = ''
        if locs:
            lx, ly = med([l[0] for l in locs]), med([l[1] for l in locs])
            if mpoints:
                best = min(mpoints, key=lambda p: (p['x']-lx)**2 + (p['y']-ly)**2)
                loc = f" у {KIND_RU.get(best['kind'],'?')}-{octant(best['x']-cx, best['y']-cy)}"
            else: loc = f" в ({lx:+.0f},{ly:+.0f})"
        who = Counter(pname(pid) for (pid, s) in parts)
        ret_after = [f"{pname(pid)}:{'+'.join(sqn(pid,s) for s in ss) or '?'}"
                     for t, pid, ss in rts if f['start'] <= t <= f['end'] + 300]
        d = f"`{ts(f['start'])}–{ts(f['end'])}`{loc}: {' vs '.join(sorted(set(x for pr in tgt_pairs for x in pr)) or who)}"
        top = [f"{pname(p)}/{sqn(p,s)}" for (p, s), n in parts.most_common(4)]
        d += f" — активны: {', '.join(top)}"
        if ret_after: d += f". Отступили: {', '.join(ret_after[:4])}"
        return d

    # --- сборка отчёта
    L = [f"# {os.path.basename(path)}"]
    races = ' vs '.join('+'.join(RACE_RU.get(p['race'], p['race']) for p in players if p['team'] == tm)
                        for tm in sorted(set(p['team'] for p in players)))
    tiers = {}
    for t, pid, at, a in actions:
        if at == 0x0F and len(a) >= 18:
            item = struct.unpack_from('<I', a, 14)[0]
            nm = resolve(table, 'upgrade', item, prace(pid))
            if 'tier' in nm:
                tiers.setdefault(pid, []).append((t, nm))
    maxtier = {pid: max((n for _, n in v), default='') for pid, v in tiers.items()}
    ends = [(t, pid) for t, pid, at, a in actions if at == 0x61]

    # гипотеза победителя: концовка (последние 25%)
    t0 = int(dur_ticks * 0.75)
    sc = defaultdict(float)
    for t, pid, ss in rts:
        if t >= t0: sc[pteam(pid)] -= 1.5
    # кап-приказы НЕ в плюс: массовые перекапы в концовке — признак проигрывающего
    deaths = [(l, pid, s) for (pid, s), l in last_seen.items()
              if l < dur_ticks - 600 and cmd_n[(pid, s)] > 8]
    for l, pid, s in deaths:
        if l >= t0: sc[pteam(pid)] -= 2.0
    # VP-«владение» по кликам не считаем: владелец точку не перекликивает,
    # сигнал систематически кредитует атакующего (проверено на dronewars)
    teams = sorted(set(p['team'] for p in players))
    if len(teams) == 2 and sc:
        wt = max(teams, key=lambda tm: sc.get(tm, 0))
        margin = abs(sc.get(teams[0], 0) - sc.get(teams[1], 0))
        conf = 'высокая' if margin >= 5 else ('средняя' if margin >= 2 else 'низкая')
        winners = ', '.join(p['name'] for p in players if p['team'] == wt)
    else:
        wt, conf, winners = None, 'низкая', '?'

    L.append(f"\n**{mapname}** · {races} · **{ts(dur_ticks)}** · записан {meta['recorded']} · данные: {table}")
    L.append("\n## Игроки\n\n| Игрок | Раса | Ур. | Команда | APM | Тир | Ретриты |")
    L.append("|---|---|---|---|---|---|---|")
    rt_by = Counter(pid for t, pid, ss in rts)
    for pid in pids:
        p = pid2p.get(pid)
        if not p: continue
        L.append(f"| {p['name']} | {RACE_RU.get(p['race'], p['race'])} | {p.get('army_level','?')} "
                 f"| {p['team']} | {pid_act[pid]/dur:.0f} | {maxtier.get(pid,'т1').replace('tier_','Т') or 'Т1'} "
                 f"| {rt_by.get(pid,0)} |")

    # разбежка
    L.append("\n## Разбежка (первые 90 секунд)\n")
    for pid in pids:
        caps90 = [(t, s, ent) for t, q, s, ent in [(c[0], c[1], c[2], c[3]) for c in cap_events]
                  if False]  # placeholder
    for pid in pids:
        my = [(t, s, ent) for (t, q, s, ent) in cap_events if q == pid and t <= 900]
        b90 = [(t, item) for t, bpid, item in builds if bpid == pid and t <= 900]
        row = [f"{sqn(pid,s)}→{ent_name.get(ent,'?')}" for t, s, ent in my[:6]]
        bs = [short(resolve(table, 'sbps', item, prace(pid))) for t, item in b90[:4]]
        L.append(f"- **{pname(pid)}**: капы: {', '.join(row) or '—'}; заказы: {', '.join(bs) or '—'}")

    # хронология экономики
    L.append("\n## Заказы и тиры\n")
    for t, pid, at, a in actions:
        if at == 0x03 and len(a) >= 18:
            item = struct.unpack_from('<I', a, 14)[0]
            L.append(f"- `{ts(t)}` {pname(pid)}: **{short(resolve(table,'sbps',item,prace(pid)))}**")
        elif at == 0x0F and len(a) >= 18:
            item = struct.unpack_from('<I', a, 14)[0]
            L.append(f"- `{ts(t)}` {pname(pid)}: 🔬 {resolve(table,'upgrade',item,prace(pid))}")

    # бои
    L.append(f"\n## Бои ({len(fights)})\n")
    for f in fights[:14]:
        L.append('- ' + fight_desc(f))
    if len(fights) > 14: L.append(f"- … и ещё {len(fights)-14}")

    # агрессия по фазам
    L.append("\n## Контроль карты (доля кликов на чужой половине)\n")
    ph = [(0, dur_ticks//3, 'начало'), (dur_ticks//3, 2*dur_ticks//3, 'середина'),
          (2*dur_ticks//3, dur_ticks, 'конец')]
    for pid in pids:
        row = []
        for a0, a1, nm in ph:
            sh = enemy_half_share(pid, a0, a1)
            row.append(f"{nm} {sh*100:.0f}%" if sh is not None else f"{nm} —")
        L.append(f"- {pname(pid)}: " + ' · '.join(row))

    # потери
    if deaths:
        L.append("\n## Вероятные потери (отряд исчез из лога)\n")
        for l, pid, s in sorted(deaths):
            L.append(f"- ~`{ts(l)}` {pname(pid)}: **{sqn(pid,s)}**")

    # захваты сводно
    L.append("\n## Захваты (приказы по точкам)\n")
    per = defaultdict(Counter)
    for t, pid, s, ent in cap_events: per[pid][ent_name.get(ent, '?')] += 1
    for pid in pids:
        tot = sum(per[pid].values())
        top = ', '.join(f"{n}×{c}" for n, c in per[pid].most_common(5))
        L.append(f"- {pname(pid)} ({tot}): {top}")

    # итог
    L.append("\n## Итог\n")
    if ends:
        L.append(f"Конец: **сдача/выход** в `{ts(ends[0][0])}` (маркер 0x61; отправитель ≠ сдавшийся).")
    else:
        L.append(f"Конец: маркера сдачи нет → **победа по очкам VP** (или обрыв записи) на `{ts(dur_ticks)}`.")
    if wt is not None:
        why = []
        lr = Counter(pteam(pid) for t, pid, ss in rts if t >= t0)
        if lr: why.append('ретриты в концовке: ' + ', '.join(f"команда {k}: {v}" for k, v in sorted(lr.items())))
        ld = Counter(pteam(pid) for l, pid, s in deaths if l >= t0)
        if ld: why.append('потери в концовке: ' + ', '.join(f"команда {k}: {v}" for k, v in sorted(ld.items())))
        L.append(f"\n**Гипотеза о победителе (уверенность {conf}):** команда {wt} — {winners}. " +
                 ('Признаки: ' + '; '.join(why) + '.' if why else ''))
    caption = (f"{mapname} · {races} · {ts(dur_ticks)} · тиры: " +
               ', '.join(f"{pname(pid)}:{maxtier.get(pid,'Т1').replace('tier_','Т') or 'Т1'}" for pid in pids) +
               (f" · вероятный победитель: {winners} ({conf})" if wt is not None else ''))
    if os.environ.get('DIGEST'):   # выжимка фактов для подробной истории (LLM пишет прозу по ней)
        ph = lambda t: 'начало' if t < dur_ticks / 3 else ('середина' if t < 2 * dur_ticks / 3 else 'конец')
        dg = {'map': mapname, 'duration': ts(dur_ticks), 'winner_guess': winners if wt is not None else None, 'confidence': conf if wt is not None else None,
              'players': [{'name': pname(pid), 'race': prace(pid), 'team': pteam(pid),
                           'retreats_by_phase': dict(Counter(ph(t) for t, q, ss in rts if q == pid))} for pid in pids],
              'fights': [fight_desc(f) + f" [боевых команд: {len(f['ev'])}]" for f in fights],
              'orders': [f"{ts(t)} {pname(pid)}: {short(resolve(table, 'sbps', item, prace(pid)))}" for t, pid, item in sorted(builds)],
              'tiers': [f"{ts(t)} {pname(pid)}: {nm}" for pid, v in tiers.items() for t, nm in v],
              'losses': [f"{ts(l)} {pname(pid)}: {sqn(pid, s)}" for l, pid, s in sorted(deaths)],
              'abilities_by_phase': {pname(pid): dict(Counter(ph(t) for t, q, at, a in actions if q == pid and at in (0x32, 0x35, 0x46))) for pid in pids},
              'enemy_half_share': {pname(pid): [(lambda v: round(v) if v is not None else None)(enemy_half_share(pid, a_, b_)) for a_, b_ in ((0, dur_ticks/3), (dur_ticks/3, 2*dur_ticks/3), (2*dur_ticks/3, dur_ticks))] for pid in pids}}
        json.dump(dg, open(os.environ['DIGEST'], 'w'), ensure_ascii=False, indent=1)
    return '\n'.join(L), caption

if __name__ == '__main__':
    os.makedirs(BASE + '/reports', exist_ok=True)
    for path in sys.argv[1:]:
        try:
            md, caption = report(path)
        except Exception as e:
            import traceback; traceback.print_exc(); continue
        out = BASE + '/reports/' + os.path.basename(path).replace('.rec', '.md')
        open(out, 'w').write(md)
        open(out + '.caption', 'w').write(caption)
        print('written', out)
        print('  caption:', caption)
