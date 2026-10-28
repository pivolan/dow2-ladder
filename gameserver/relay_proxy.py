#!/usr/bin/env python3
"""DoW2 relay proxy (stage 1): transparent TCP forwarding to Relic's battleserverrelay on AWS.
The game (EliteAI/MMFind DLL) connects to us instead of Relic: our port = Relic port + 20000, the upstream is
picked by port (Relic keeps one fixed IP per relay port). We pass bytes through untouched and log each
connection (who, how long, bytes, the first bytes of both directions) to learn the protocol for stage 2."""
import asyncio, json, os, socket, sys, time

UPSTREAM = {27001: '54.237.64.68', 27005: '54.172.17.196', 27007: '98.93.179.175', 27008: '54.237.64.68'}
OFFSET = 20000
LOG = os.environ.get('RELAY_LOG', '/srv/dow2relay/log')
HEAD = 512                                   # bytes of each direction kept per connection

os.makedirs(LOG, exist_ok=True)
stats = {'conns': 0, 'active': 0}

async def pipe(r, w, buf, counter):
    try:
        while True:
            d = await r.read(65536)
            if not d: break
            if len(buf) < HEAD: buf.extend(d[:HEAD - len(buf)])
            counter[0] += len(d)
            w.write(d); await w.drain()
    except Exception:
        pass
    finally:
        try: w.close()
        except Exception: pass

async def handle(cr, cw, rport):
    peer = cw.get_extra_info('peername'); t0 = time.time(); stats['conns'] += 1; stats['active'] += 1
    up_c, up_s, h_c, h_s = [0], [0], bytearray(), bytearray()
    host = UPSTREAM.get(rport)
    if rport == 0:   # Relay.dll 5: "DWR1" + IPv4 + port (BE) first - Relic adds relay servers, the game tells where it wanted to go
        try: pre = await asyncio.wait_for(cr.readexactly(10), 15)
        except Exception: cw.close(); stats['active'] -= 1; return
        port = int.from_bytes(pre[8:10], 'big')
        if pre[:4] != b'DWR1' or not 27000 <= port <= 27099:
            print(f'{time.strftime("%T")} {peer} bad preamble {pre.hex()}', flush=True); cw.close(); stats['active'] -= 1; return
        host, rport = socket.inet_ntoa(pre[4:8]), port
    try:
        sr, sw = await asyncio.wait_for(asyncio.open_connection(host, rport), 10)
    except Exception as e:
        print(f'{time.strftime("%T")} {peer} -> {rport}: upstream failed: {e}', flush=True); cw.close(); stats['active'] -= 1; return
    for w in (cw, sw):                       # game messages are small and frequent: no Nagle batching on either side
        try: w.get_extra_info('socket').setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception: pass
    print(f'{time.strftime("%T")} {peer[0]}:{peer[1]} -> {host}:{rport} open (active {stats["active"]})', flush=True)
    await asyncio.gather(pipe(cr, sw, h_c, up_c), pipe(sr, cw, h_s, up_s))
    dt = time.time() - t0; stats['active'] -= 1
    rec = {'t': int(t0), 'client': f'{peer[0]}:{peer[1]}', 'relay': f'{host}:{rport}', 'secs': round(dt, 1),
           'c2s': up_c[0], 's2c': up_s[0], 'c2s_head': h_c.hex(), 's2c_head': h_s.hex()}
    with open(f'{LOG}/conns.jsonl', 'a') as f: f.write(json.dumps(rec) + '\n')
    print(f'{time.strftime("%T")} {peer[0]}:{peer[1]} -> {rport} closed after {dt:.0f}s, up {up_c[0]} down {up_s[0]} bytes', flush=True)

async def main():
    for p in UPSTREAM:
        await asyncio.start_server(lambda r, w, p=p: handle(r, w, p), '0.0.0.0', p + OFFSET)
    await asyncio.start_server(lambda r, w: handle(r, w, 0), '0.0.0.0', 47000)   # preamble mode (Relay.dll 5)
    print('relay proxy on 47000 (preamble),', ', '.join(str(p + OFFSET) for p in UPSTREAM), flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
