#!/usr/bin/env python3
"""Our own DoW2 battle relay - replaces Relic's battleserverrelay (AWS Virginia) so players near the server do not
send every game turn across the ocean. Protocol learned from captures (capture_proxy.py, 27.10.2026):

  frame:  u16 BE length + message, message[0] = type
  15  hello      15 <sid u32 LE> 00000000 <id 8> <flag>        -> join session sid, echo back. Peer index = join order (1..)
  14  ping       14 <stamp 4>                                   -> echo back
  13  to peer    13 <dst> <src> <inner>                         -> to peer #dst unchanged; inner "0f 05.." (peer announce)
                                                                   goes to everyone in the session, header stripped
  10  to others  10 <src> ...                                   -> to every other peer unchanged (echo when alone)
  09  turn data  09 fe <u16 LE len + data>                      -> queued for the next tick
  17  turn+sync  17 <n> n*(crc 4 + turn u32) 09 fe <...>         -> the embedded 09 is queued (crc pairs: sync check, ignored)
  0a  tick (server -> all, every 100 ms):
                 0a fe <count> count*(<sender idx> <u16 LE len + data>) <m> m*<idx of peers silent in this tick>

The client first sends a preamble: "DWR1" + IPv4 + port of the Relic relay the game wanted (Relay.dll 5-7), or since
Relay.dll 8 "DWR2" + IPv4 + port + 1 byte: how many people are in the lobby (players and observers).
Compatibility (DWR2): a session stays here only when everyone of the lobby comes (everyone has the add-on). Until then
hello and ping are answered right away (as Relic would) and the rest is held; if somebody doesn't come within
GATE_WAIT seconds, he plays on Relic's servers - then every peer of that session is passed on to the Relic relay its
game wanted, with what it sent so far, and the answers we already gave are not repeated. Later peers follow the
decision. Old DWR1 clients join right away (no compatibility).
Usage: battle_relay.py [port] [bind]   (default 47100 127.0.0.1)
Peers silent for IDLE seconds are dropped (the game pings every ~3 s).
"""
import asyncio, json, os, socket, sys, time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47100
BIND = sys.argv[2] if len(sys.argv) > 2 else '127.0.0.1'
TICK = 0.1
IDLE = 60
GATE_WAIT = float(os.environ.get('GATE_WAIT', '45'))
_last_warn = {}
LOG = os.environ.get('RELAY_LOG', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'battle_relay.jsonl'))
sessions = {}                                       # sid bytes -> Session
gates = {}                                          # sid -> Gate: DWR2 peers waiting for the whole lobby
decided = {}                                        # sid -> ('ours' | 'relic', time)


def log(**k):
    k['t'] = round(time.time(), 3)
    with open(LOG, 'a') as f: f.write(json.dumps(k) + '\n')
    print(time.strftime('%T'), ' '.join(f'{a}={b}' for a, b in k.items() if a != 't'), flush=True)


class Peer:
    def __init__(self, w, addr): self.w, self.addr, self.sess, self.idx, self.pid = w, addr, None, 0, b''

    def send(self, m):
        if self.w.is_closing(): return
        self.w.write(len(m).to_bytes(2, 'big') + m)


class Session:
    def __init__(self, sid):
        self.sid, self.peers, self.next_idx, self.queue, self.ticker = sid, [], 1, [], None
        self.started = time.time(); self.ticks = 0; self.turn_msgs = 0

    def join(self, p):
        p.sess, p.idx = self, self.next_idx; self.next_idx += 1; self.peers.append(p)

    def leave(self, p):
        if p in self.peers: self.peers.remove(p)
        if not self.peers:
            if self.ticker: self.ticker.cancel()
            sessions.pop(self.sid, None)
            log(ev='session_end', sid=int.from_bytes(self.sid, 'little'), secs=round(time.time() - self.started), ticks=self.ticks, turns=self.turn_msgs)

    def queue_turn(self, p, data):
        self.queue.append((p.idx, data)); self.turn_msgs += 1
        if not self.ticker: self.ticker = asyncio.ensure_future(self.tick_loop())

    async def tick_loop(self):
        nxt = time.monotonic()
        while self.peers:
            nxt += TICK
            await asyncio.sleep(max(0, nxt - time.monotonic()))
            q, self.queue = self.queue, []
            sent = {i for i, _ in q}
            silent = [p.idx for p in self.peers if p.idx not in sent]
            m = bytearray(b'\x0a\xfe') + bytes([len(q)])
            for i, d in q: m += bytes([i]) + d
            m += bytes([len(silent)]) + bytes(silent)
            for p in list(self.peers): p.send(bytes(m))
            self.ticks += 1
            await asyncio.gather(*(p.w.drain() for p in self.peers if not p.w.is_closing()), return_exceptions=True)


def handle_msg(p, m, echo=True):
    t = m[0]
    if t == 0x15:                                                   # hello: join the session
        sid = m[1:5]
        if p.sess is None:
            s = sessions.get(sid) or sessions.setdefault(sid, Session(sid))
            s.join(p); p.pid = m[9:17]
            log(ev='join', sid=int.from_bytes(sid, 'little'), idx=p.idx, peer=p.addr, pid=p.pid.hex(), members=len(s.peers))
        if echo: p.send(m)
        return
    if t == 0x14:                                                    # ping
        if echo: p.send(m)
        return
    s = p.sess
    if s is None: log(ev='no_session', type=t, peer=p.addr); return
    if t == 0x13:
        inner = m[3:]
        if inner[:2] == b'\x0f\x05' and m[1] == 0 and len(s.peers) == 1:   # a guest (does not know its number yet) alone here:
            log(ev='mixed', sid=int.from_bytes(s.sid, 'little'), peer=p.addr)  # this lobby's host plays on Relic's servers
            p.w.write(b'DWRERR:mixed\n'); p.w.close(); return              # Relay.dll 7 shows why and closes the game
        if inner[:2] == b'\x0f\x05':                                 # peer announce: everyone, header stripped
            for q in s.peers: q.send(inner)
            return
        dst = m[1]
        targets = [q for q in s.peers if q.idx == dst] if dst else list(s.peers)
        if not targets:                                              # e.g. the game still talks to a peer that left: log once a minute
            k = (s.sid, dst); now = time.time()
            if now - _last_warn.get(k, 0) > 60: _last_warn[k] = now; log(ev='no_target', sid=int.from_bytes(s.sid, 'little'), dst=dst, src=p.idx)
        for q in targets: q.send(m)
        return
    if t == 0x10:
        others = [q for q in s.peers if q is not p]
        for q in (others or [p]): q.send(m)
        return
    if t == 0x09:
        s.queue_turn(p, m[2:]); return
    if t == 0x17:
        n = m[1]; rest = m[2 + 8 * n:]
        if rest[:1] == b'\x09': s.queue_turn(p, rest[2:])
        return
    log(ev='unknown', type=hex(t), len=len(m), head=m[:24].hex(), peer=p.addr)


class Gate:
    def __init__(self, sid, expected):
        self.sid, self.expected, self.waiting, self.mode, self.t0 = sid, expected, [], None, time.time()
        self.ev = asyncio.Event()


def gate_finish(g, mode):
    if g.mode: return
    g.mode = mode; decided[g.sid] = (mode, time.time()); gates.pop(g.sid, None)
    log(ev='gate', sid=int.from_bytes(g.sid, 'little'), mode=mode, came=len(g.waiting), expected=g.expected, waited=round(time.time() - g.t0, 1))
    if mode == 'ours':                                  # join in the order they came, what they sent so far, answers already given
        for p, held in g.waiting:
            for m in held:
                if m: handle_msg(p, m, echo=False)
    g.ev.set()


async def gate(p, sid, expected, held):
    d = decided.get(sid)
    if d and time.time() - d[1] < 3600:
        if d[0] == 'ours':
            for m in held:
                if m: handle_msg(p, m, echo=False)
        return d[0]
    g = gates.get(sid) or gates.setdefault(sid, Gate(sid, expected))
    g.expected = max(g.expected, expected); g.waiting.append((p, held))
    if len(g.waiting) >= g.expected: gate_finish(g, 'ours')
    else:
        try: await asyncio.wait_for(g.ev.wait(), max(0.1, GATE_WAIT - (time.time() - g.t0)))
        except asyncio.TimeoutError: gate_finish(g, 'relic')
    return g.mode


async def to_relic(q, w, p, relic, held, answered):
    """pass this peer on to the Relic relay its game wanted: what it sent so far, then both ways as they are"""
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_connection(*relic), 10)
    except Exception as e:
        log(ev='relic_fail', peer=p.addr, relic=f'{relic[0]}:{relic[1]}', err=repr(e)); return
    try: uw.get_extra_info('socket').setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception: pass
    log(ev='to_relic', peer=p.addr, relic=f'{relic[0]}:{relic[1]}', held=len(held))
    for m in held: uw.write(len(m).to_bytes(2, 'big') + m)
    await uw.drain()

    async def up():                                     # game -> Relic, frame by frame as it came
        try:
            while True:
                m = await q.get()
                if m is None: break
                uw.write(len(m).to_bytes(2, 'big') + m); await uw.drain()
        except Exception: pass
        finally: uw.close()

    async def down():                                   # Relic -> game; its echoes of hello/ping we answered already are dropped
        try:
            while True:
                n = int.from_bytes(await ur.readexactly(2), 'big')
                m = await ur.readexactly(n) if n else b''
                if answered and m in answered: answered.remove(m); continue
                w.write(n.to_bytes(2, 'big') + m); await w.drain()
        except Exception: pass
        finally: w.close()
    await asyncio.gather(up(), down())


async def handle(r, w):
    sock = w.get_extra_info('socket')
    try: sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception: pass
    a = w.get_extra_info('peername'); p = Peer(w, f'{a[0]}:{a[1]}')
    q = asyncio.Queue(); rt = None

    async def reader():                                 # one reader per peer: frames into q, None at the end
        try:
            while True:
                n = int.from_bytes(await r.readexactly(2), 'big')
                await q.put(await r.readexactly(n) if n else b'')
        except Exception: pass
        await q.put(None)
    try:
        pre = await asyncio.wait_for(r.readexactly(10), 15)
        if pre[:4] not in (b'DWR1', b'DWR2'): log(ev='bad_preamble', peer=p.addr, hex=pre.hex()); w.close(); return
        expected = (await asyncio.wait_for(r.readexactly(1), 15))[0] if pre[:4] == b'DWR2' else 0
        rt = asyncio.ensure_future(reader())
        if expected > 1:                                # compatibility: hold until the whole lobby is here, or pass on to Relic
            relic = (socket.inet_ntoa(pre[4:8]), int.from_bytes(pre[8:10], 'big'))
            held, answered, sid = [], [], None
            while sid is None and len(held) < 64:
                m = await asyncio.wait_for(q.get(), IDLE)
                if m is None: return
                held.append(m)
                if m[:1] in (b'\x15', b'\x14'): p.send(m); answered.append(m)
                if m[:1] == b'\x15': sid = m[1:5]
            if sid is not None:
                gt = asyncio.ensure_future(gate(p, sid, expected, held))
                while not gt.done():                    # keep answering pings and holding the rest while we wait
                    gq = asyncio.ensure_future(q.get())
                    done, _ = await asyncio.wait({gt, gq}, return_when=asyncio.FIRST_COMPLETED)
                    if gq not in done: gq.cancel(); continue
                    m = gq.result()
                    if m is None: await q.put(None); break          # gone: let the code below see the end too
                    if p.sess is not None: handle_msg(p, m); continue   # already joined here (held went in)
                    held.append(m)
                    if m[:1] == b'\x14': p.send(m); answered.append(m)
                mode = await gt
                if mode == 'relic':
                    await to_relic(q, w, p, relic, held, answered); return
        while True:
            m = await asyncio.wait_for(q.get(), IDLE)
            if m is None: break
            if m: handle_msg(p, m)
            await w.drain()
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError): pass
    except Exception as e: log(ev='error', peer=p.addr, err=repr(e))
    finally:
        if rt: rt.cancel()
        if p.sess:
            log(ev='leave', sid=int.from_bytes(p.sess.sid, 'little'), idx=p.idx, peer=p.addr)
            p.sess.leave(p)
        try: w.close()
        except Exception: pass


async def main():
    srv = await asyncio.start_server(handle, BIND, PORT)
    log(ev='start', bind=BIND, port=PORT)
    async with srv: await srv.serve_forever()

if __name__ == '__main__':
    asyncio.run(main())
