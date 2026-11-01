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
Relay.dll 8 "DWR2" + the same + 1 byte (players in the lobby, informational): "DWR2" = the player allows Relic as fallback.
The game opens this connection when a lobby is created (host) or joined (guest) and keeps it through the match.
Compatibility (DWR2): a guest whose session is not here (its host is not on this server) is passed on - to the one of
our other servers (RELAY_PEERS) that has the session, else to the Relic relay its game wanted (the host plays without
the add-on). A guest is told apart by its first peer announce "13 00 .. 0f 05" (it doesn't know its number yet).
Until then hello and ping are answered right away (as Relic would) and the rest is held; when passing on, what the
peer sent goes first and the answers we already gave are not repeated. Servers ask each other "DWRQ" + sid -> Y/N.
Bridge (DWR3, Relay.dll 15, 01.11.2026): "DWR3" + IPv4 + port of the Relic relay - the player plays on Relic (with anybody)
and we only carry the connection: everything is passed on to that Relic relay at once, nothing is hosted here. For players
whose own path to Relic is bad or cut on the way (Russia: DPI on foreign hosting) - the server near them carries it.
Usage: battle_relay.py [port] [bind]   (default 47100 127.0.0.1)
Peers silent for IDLE seconds are dropped (the game pings every ~3 s).
"""
import asyncio, ipaddress, json, os, socket, struct, sys, time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47100
BIND = sys.argv[2] if len(sys.argv) > 2 else '127.0.0.1'
TICK = 0.1
IDLE = 60
DECIDE = 5                                          # s: no peer announce from a new session's first peer -> it's the host
PEERS = [h for h in os.environ.get('RELAY_PEERS', '83.217.192.34,116.202.106.150,15.220.68.107').split(',') if h]
_last_warn = {}
LOG = os.environ.get('RELAY_LOG', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'battle_relay.jsonl'))
sessions = {}                                       # sid bytes -> Session
GAP = 0.25                                          # s: a player's turns this far apart count as a stall (a lost packet, a hiccup)
TCP_QUICKACK, TCP_THIN_LINEAR_TIMEOUTS = 12, 16     # Linux socket options (not in the socket module everywhere)


def tcp_info(w):
    # (rtt ms, rttvar ms, retransmitted segments in all, lost now) of a peer connection, from Linux TCP_INFO; None elsewhere
    try:
        b = w.get_extra_info('socket').getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104)
        lost, rtt, rttvar, total = struct.unpack_from('<I', b, 32)[0], struct.unpack_from('<I', b, 68)[0], struct.unpack_from('<I', b, 72)[0], struct.unpack_from('<I', b, 100)[0]
        return rtt // 1000, rttvar // 1000, total, lost
    except Exception: return None


def log(**k):
    k['t'] = round(time.time(), 3)
    with open(LOG, 'a') as f: f.write(json.dumps(k) + '\n')
    print(time.strftime('%T'), ' '.join(f'{a}={b}' for a, b in k.items() if a != 't'), flush=True)


class Peer:
    def __init__(self, w, addr):
        self.w, self.addr, self.sess, self.idx, self.pid = w, addr, None, 0, b''
        # what the connection was like, for the log when the player leaves: stalls in the turns (GAP), the longest one,
        # TCP rtt (the worst seen) and retransmissions, how far the player's send buffer got behind
        self.turns, self.last_turn, self.gaps, self.max_gap, self.max_rtt, self.retrans, self.max_buf = 0, 0.0, 0, 0.0, 0, 0, 0

    def stats(self):
        i = tcp_info(self.w)
        if i:
            self.max_rtt = max(self.max_rtt, i[0]); self.retrans = i[2]
        return dict(peer=self.addr, idx=self.idx, turns=self.turns, gaps=self.gaps, max_gap_ms=round(self.max_gap * 1000),
                    rtt_ms=i[0] if i else None, rttvar_ms=i[1] if i else None, max_rtt_ms=self.max_rtt, retrans=self.retrans,
                    max_buf=self.max_buf)

    def send(self, m):
        if self.w.is_closing(): return
        self.w.write(len(m).to_bytes(2, 'big') + m)


class Session:
    def __init__(self, sid):
        self.sid, self.peers, self.next_idx, self.queue, self.ticker = sid, [], 1, [], None
        self.by_pid = {}                                  # player id -> its number in this session
        self.started = time.time(); self.ticks = 0; self.turn_msgs = 0; self.late = 0; self.max_late = 0.0

    def join(self, p):
        # 30.10.2026: a number per PLAYER, not per connection - the host numbers the players itself in the order they came
        # and a player who connects again (the same id) keeps the number; with a new number per connection the host's
        # messages to the next newcomer went to a number nobody had, and every later guest of that lobby failed to join
        old = [q for q in self.peers if q.pid == p.pid]
        for q in old:
            self.peers.remove(q)
            try: q.w.close()
            except Exception: pass
        if p.pid in self.by_pid: p.idx = self.by_pid[p.pid]
        else: p.idx = self.by_pid[p.pid] = self.next_idx; self.next_idx += 1
        p.sess = self; self.peers.append(p)

    def leave(self, p):
        if p in self.peers: self.peers.remove(p)
        if not self.peers:
            if self.ticker: self.ticker.cancel()
            sessions.pop(self.sid, None)
            log(ev='session_end', sid=int.from_bytes(self.sid, 'little'), secs=round(time.time() - self.started), ticks=self.ticks, turns=self.turn_msgs,
                late_ticks=self.late, max_late_ms=round(self.max_late * 1000))

    def queue_turn(self, p, data):
        self.queue.append((p.idx, data)); self.turn_msgs += 1
        now = time.monotonic()
        if p.last_turn:
            g = now - p.last_turn
            if g > GAP: p.gaps += 1
            p.max_gap = max(p.max_gap, g)
        p.last_turn = now; p.turns += 1
        if not self.ticker: self.ticker = asyncio.ensure_future(self.tick_loop())

    async def tick_loop(self):
        nxt = time.monotonic()
        while self.peers:
            nxt += TICK
            await asyncio.sleep(max(0, nxt - time.monotonic()))
            late = time.monotonic() - nxt                    # the tick went out this much after its time
            if late > 0.03: self.late += 1
            self.max_late = max(self.max_late, late)
            if late > 0.5: nxt = time.monotonic()            # after a long hold: carry on from now, no burst of catch-up ticks
            q, self.queue = self.queue, []
            sent = {i for i, _ in q}
            silent = [p.idx for p in self.peers if p.idx not in sent]
            m = bytearray(b'\x0a\xfe') + bytes([len(q)])
            for i, d in q: m += bytes([i]) + d
            m += bytes([len(silent)]) + bytes(silent)
            for p in list(self.peers): p.send(bytes(m))
            self.ticks += 1
            # no waiting here for the players' connections to take the data (that held the tick for everyone when one
            # player's connection stalled): the data sits in that player's buffer, which is watched instead
            for p in self.peers:
                try: p.max_buf = max(p.max_buf, p.w.transport.get_write_buffer_size())
                except Exception: pass
            if self.ticks % 50 == 0:
                for p in self.peers: p.stats()                # the worst rtt is sampled every 5 s


def handle_msg(p, m, echo=True):
    t = m[0]
    if t == 0x15:                                                   # hello: join the session
        sid = m[1:5]
        if p.sess is None:
            s = sessions.get(sid) or sessions.setdefault(sid, Session(sid))
            p.pid = m[9:17]; s.join(p)
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


async def find_peer(sid):
    """which of our other servers has this session (its host)"""
    for h in PEERS:                                     # "ip" or "ip:port"
        hp = (h.rsplit(':', 1)[0], int(h.rsplit(':', 1)[1])) if ':' in h else (h, PORT)
        try:
            r2, w2 = await asyncio.wait_for(asyncio.open_connection(*hp), 2)
            w2.write(b'DWRQ' + sid); await w2.drain()
            a = await asyncio.wait_for(r2.readexactly(1), 2); w2.close()
            if a == b'Y': return hp
        except Exception: pass
    return None


async def pass_on(q, w, p, dest, held, answered, pre=b''):
    """pass this peer on (to our other server or to the Relic relay its game wanted): what it sent so far, then both
    ways as they are"""
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_connection(*dest), 10)
    except Exception as e:
        log(ev='pass_fail', peer=p.addr, dest=f'{dest[0]}:{dest[1]}', err=repr(e)); return
    try: uw.get_extra_info('socket').setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception: pass
    log(ev='pass_on', peer=p.addr, dest=f'{dest[0]}:{dest[1]}', to='ours' if pre else ('relic' if held else 'relic_bridge'), held=len(held))
    if pre: uw.write(pre)
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
    for opt in (socket.TCP_NODELAY, TCP_THIN_LINEAR_TIMEOUTS):   # thin stream: a lost packet is sent again without the
        try: sock.setsockopt(socket.IPPROTO_TCP, opt, 1)           # exponential back-off of the timeouts
        except Exception: pass
    a = w.get_extra_info('peername'); p = Peer(w, f'{a[0]}:{a[1]}')
    q = asyncio.Queue(); rt = None

    async def reader():                                 # one reader per peer: frames into q, None at the end
        try:
            while True:
                n = int.from_bytes(await r.readexactly(2), 'big')
                await q.put(await r.readexactly(n) if n else b'')
                try: sock.setsockopt(socket.IPPROTO_TCP, TCP_QUICKACK, 1)   # acknowledge at once: the player's next turn
                except Exception: pass                                    # does not wait for our delayed ack
        except Exception: pass
        await q.put(None)
    try:
        pre = await asyncio.wait_for(r.readexactly(4), 15)
        if pre == b'DWRQ':                              # another server of ours asks: is this session here?
            sid = await asyncio.wait_for(r.readexactly(4), 5)
            w.write(b'Y' if sid in sessions else b'N'); await w.drain(); w.close(); return
        pre += await asyncio.wait_for(r.readexactly(6), 15)
        if pre[:4] == b'DWR3':                    # bridge: straight on to the Relic relay the game wanted
            dest = (socket.inet_ntoa(pre[4:8]), int.from_bytes(pre[8:10], 'big'))
            ip = ipaddress.ip_address(dest[0])          # only Relic relay ports on public addresses: not an open proxy
            if not 27000 <= dest[1] <= 27099 or not ip.is_global:
                log(ev='bridge_refused', peer=p.addr, dest=f'{dest[0]}:{dest[1]}'); w.close(); return
            rt = asyncio.ensure_future(reader())
            await pass_on(q, w, p, dest, [], []); return
        if pre[:4] not in (b'DWR1', b'DWR2'): log(ev='bad_preamble', peer=p.addr, hex=pre.hex()); w.close(); return
        compat = pre[:4] == b'DWR2'
        if compat: await asyncio.wait_for(r.readexactly(1), 15)
        rt = asyncio.ensure_future(reader())
        if True:   # 30.10.2026: a guest whose lobby is on another of our servers is passed on there in the strict mode (DWR1) too
            relic = (socket.inet_ntoa(pre[4:8]), int.from_bytes(pre[8:10], 'big'))
            held, answered, sid = [], [], None
            while sid is None and len(held) < 64:
                m = await asyncio.wait_for(q.get(), IDLE)
                if m is None: return
                held.append(m)
                if m[:1] in (b'\x15', b'\x14'): p.send(m); answered.append(m)
                if m[:1] == b'\x15': sid = m[1:5]
            away, end = False, time.time() + DECIDE
            while sid is not None and sid not in sessions:   # a new session here: its host, or a guest whose host is elsewhere?
                try: m = await asyncio.wait_for(q.get(), max(0.05, end - time.time()))
                except asyncio.TimeoutError: break           # no announce: the host
                if m is None: return
                held.append(m)
                if m[:1] in (b'\x15', b'\x14'): p.send(m); answered.append(m); continue
                away = m[:1] == b'\x13' and m[1] == 0 and m[3:5] == b'\x0f\x05'
                break
            if away and sid not in sessions:
                h = await find_peer(sid)
                if h: await pass_on(q, w, p, h, held, answered, pre=b'DWR1' + pre[4:10]); return
                if compat: await pass_on(q, w, p, relic, held, answered); return
                # strict (DWR1) and none of ours has it: the lobby is on Relic - handled below ("mixed", Relay.dll explains)
            for m in held:
                if m: handle_msg(p, m, echo=False)
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
            log(ev='leave', sid=int.from_bytes(p.sess.sid, 'little'), **p.stats())
            p.sess.leave(p)
        try: w.close()
        except Exception: pass


async def main():
    srv = await asyncio.start_server(handle, BIND, PORT)
    log(ev='start', bind=BIND, port=PORT)
    async with srv: await srv.serve_forever()

if __name__ == '__main__':
    asyncio.run(main())
