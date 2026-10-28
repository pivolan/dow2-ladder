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

The client (Relay.dll 5) first sends a 10-byte preamble "DWR1" + IPv4 + port of Relic's relay it wanted; we accept and ignore it.
Usage: battle_relay.py [port] [bind]   (default 47100 127.0.0.1)
Peers silent for IDLE seconds are dropped (the game pings every ~3 s).
"""
import asyncio, json, os, socket, sys, time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47100
BIND = sys.argv[2] if len(sys.argv) > 2 else '127.0.0.1'
TICK = 0.1
IDLE = 60
_last_warn = {}
LOG = os.environ.get('RELAY_LOG', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'battle_relay.jsonl'))
sessions = {}                                       # sid bytes -> Session


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


def handle_msg(p, m):
    t = m[0]
    if t == 0x15:                                                   # hello: join the session
        sid = m[1:5]
        if p.sess is None:
            s = sessions.get(sid) or sessions.setdefault(sid, Session(sid))
            s.join(p); p.pid = m[9:17]
            log(ev='join', sid=int.from_bytes(sid, 'little'), idx=p.idx, peer=p.addr, pid=p.pid.hex(), members=len(s.peers))
        p.send(m); return
    if t == 0x14: p.send(m); return                                  # ping
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


async def handle(r, w):
    sock = w.get_extra_info('socket')
    try: sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception: pass
    a = w.get_extra_info('peername'); p = Peer(w, f'{a[0]}:{a[1]}')
    try:
        pre = await asyncio.wait_for(r.readexactly(10), 15)
        if pre[:4] != b'DWR1': log(ev='bad_preamble', peer=p.addr, hex=pre.hex()); w.close(); return
        while True:
            n = int.from_bytes(await asyncio.wait_for(r.readexactly(2), IDLE), 'big')
            m = await asyncio.wait_for(r.readexactly(n), IDLE) if n else b''
            if m: handle_msg(p, m)
            await w.drain()
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError): pass
    except Exception as e: log(ev='error', peer=p.addr, err=repr(e))
    finally:
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
