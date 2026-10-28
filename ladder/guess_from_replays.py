#!/usr/bin/env python3
"""Оценка исхода наших ладдерных матчей по РЕПЛЕЮ, пока Relic не отдал результат.
Победитель — гипотеза парсера dow_replay (только уверенность высокая/средняя), дельта MMR — Glicko-2.
Результат — в mm_history.guess (JSON с estimated:true). Настоящий Relic-результат имеет приоритет и затирает оценку.
Запуск: root (читает .rec replayweb + парсер dow_replay, пишет ladder.sqlite)."""
import json, os, subprocess, sqlite3, sys, tempfile
sys.path.insert(0, '/srv/ladder/app')
import ladder   # glicko2_update, db(), current_season(), RACE_KEYS...

REC = '/srv/replayweb/data/%s/replay.rec'
PARSER = '/var/www/dow_replay/make_report.py'
OK_CONF = {'высокая', 'средняя'}

def parse_replay(rid):
    rec = REC % rid
    if not os.path.isfile(rec): return None
    fd, dg = tempfile.mkstemp(suffix='.json'); os.close(fd)
    try:
        subprocess.run(['python3', PARSER, rec], env=dict(os.environ, DIGEST=dg),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        d = json.load(open(dg))
        return d
    except Exception:
        return None
    finally:
        try: os.remove(dg)
        except Exception: pass

def rrv(c, pid, mode, season):
    r = c.execute('SELECT rating, rd, vol FROM ratings WHERE profile_id=? AND mode=? AND season=?', (pid, mode, season)).fetchone()
    return (r[0], r[1], r[2]) if r else (1500.0, 350.0, 0.06)   # «до» — ладдерный рейтинг (шкала 1500), не Relic

def main():
    season = ladder.current_season()
    with ladder.db() as c:
        rows = c.execute("SELECT lobby, mode, players, replay FROM mm_history "
                         "WHERE (result IS NULL OR result='') AND replay IS NOT NULL "
                         "AND (guess IS NULL OR guess='')").fetchall()
    for lobby, mode, pl, rid in rows:
        players = json.loads(pl)
        d = parse_replay(rid)
        conf = (d or {}).get('confidence')
        wg = (d or {}).get('winner_guess')
        if not d or conf not in OK_CONF or not wg:
            with ladder._db_lock, ladder.db() as c:   # помечаем, чтобы не парсить снова
                c.execute("UPDATE mm_history SET guess=? WHERE lobby=?", (json.dumps({'skip': True, 'confidence': conf}), lobby))
            print('skip', lobby, 'conf', conf); continue
        winners = {n.strip() for n in wg.split(',')}
        # победившая команда = та, чьи игроки в списке победителей (по имени — надёжно)
        wteam = None
        for p in players:
            if (p.get('name') or '') in winners: wteam = p.get('team'); break
        if wteam is None:
            with ladder._db_lock, ladder.db() as c:
                c.execute("UPDATE mm_history SET guess=? WHERE lobby=?", (json.dumps({'skip': True}), lobby)); 
            print('skip(no team)', lobby); continue
        # средний рейтинг команд (рейтинг на момент матча из mm_history.players)
        with ladder.db() as c:
            teams = {}
            for p in players: teams.setdefault(p.get('team'), []).append(p)
            if len(teams) != 2:
                c2=c
            avg = {}
            for t, ps in teams.items():
                import math
                rs = [rrv(c, p.get('pid'), mode, season) for p in ps]
                avg[t] = (sum(r[0] for r in rs)/len(rs), math.sqrt(sum(r[1]*r[1] for r in rs)/len(rs)))
            gp = []
            tids = list(teams)
            for p in players:
                t = p.get('team'); ot = [x for x in tids if x != t][0]
                before, rd, vol = rrv(c, p.get('pid'), mode, season)
                s = 1.0 if t == wteam else 0.0
                nr, nrd, nv = ladder.glicko2_update(before, rd, vol, [(avg[ot][0], avg[ot][1], s)])
                gp.append({'pid': p.get('pid'), 'team': t, 'win': (t == wteam),
                           'mmr': round(nr), 'mmr_delta': round(nr - before)})
        guess = {'estimated': True, 'confidence': conf, 'winner_team': wteam,
                 'mapname': (d.get('map') or ''), 'players': gp}
        with ladder._db_lock, ladder.db() as c:
            c.execute("UPDATE mm_history SET guess=? WHERE lobby=?", (json.dumps(guess, ensure_ascii=False), lobby))
        print('guess', lobby, 'conf', conf, 'wteam', wteam, 'winners', winners)

if __name__ == '__main__':
    # колонка guess
    with ladder._db_lock, ladder.db() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(mm_history)")]
        if 'guess' not in cols:
            c.execute("ALTER TABLE mm_history ADD COLUMN guess TEXT")
    main()
