#!/usr/bin/env python3
"""udp_gateway.py - the game's connection to our battle server over UDP (Relay.dll 15 "UDP to our server", 01.11.2026).
In Russia TCP to foreign hosting is cut after ~16-30 KB (DPI on the way) while UDP passes (NetProbe). Relay.dll carries
the game's TCP stream to us in UDP packets with its own delivery; here each such stream becomes an ordinary TCP
connection to battle_relay on this machine, so battle_relay does not know the difference.

Packet: "DU" + type (1) + connection id (u32 LE, random, chosen by the player) + body
  1 SYN     player -> us: version (1)                 -> we connect to battle_relay, answer SYNACK (again on a repeat)
  2 SYNACK  us -> player
  3 DATA    both: seq (u32) + ack (u32) + sack (u32) + bytes (<= 1200)
  4 ACK     both: ack (u32) + sack (u32)              ack = next packet number expected, sack bit i = ack+1+i arrived
  5 FIN     both: the connection is over
Packets are numbered from 0 on each side. Lost ones are sent again after an RTO from the measured round trip, or at once
when a later one was acknowledged twice past it. Keepalive: an ACK each second; 30 s without a packet = gone.
Usage: udp_gateway.py [port] [battle_relay host:port]   (default 47100 127.0.0.1:47100)
"""
import asyncio, os, random, struct, sys, time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47100
BR = (sys.argv[2].rsplit(':', 1) if len(sys.argv) > 2 else ['127.0.0.1', '47100'])
BR = (BR[0], int(BR[1]))
MAXP = 1200            # payload bytes per packet
WINDOW = 512           # packets in flight
PER_IP = 16            # connections per address
IDLE = 30.0
SYN, SYNACK, DATA, ACK, FIN = 1, 2, 3, 4, 5


def log(**k):
    print(time.strftime('%H:%M:%S') + ' ' + ' '.join('%s=%s' % kv for kv in k.items()), flush=True)


class Conn:
    def __init__(self, gw, cid, addr):
        self.gw, self.cid, self.addr = gw, cid, addr
        self.w = None; self.ready = False; self.closed = False
        self.snd_next = 0; self.unacked = {}                   # seq -> [payload, first_sent, last_sent, sends]
        self.rcv_next = 0; self.ooo = {}                       # out of order: seq -> payload
        self.srtt = 0.15; self.rttvar = 0.05; self.dup = 0; self.last_ack_seen = -1
        self.seen = self.sent = time.time()
        self.stats = dict(rx=0, tx=0, resent=0, lost_in=0)
        self.can_send = asyncio.Event(); self.can_send.set()

    def hdr(self, t): return b'DU' + bytes([t]) + struct.pack('<I', self.cid)

    def sack(self):
        m = 0
        for i in range(32):
            if self.rcv_next + 1 + i in self.ooo: m |= 1 << i
        return m

    def out(self, pkt):
        self.gw.tr.sendto(pkt, self.addr); self.sent = time.time()

    def send_ack(self): self.out(self.hdr(ACK) + struct.pack('<II', self.rcv_next, self.sack()))

    def send_data(self, payload):
        seq = self.snd_next; self.snd_next += 1; now = time.time()
        self.unacked[seq] = [payload, now, now, 1]
        self.out(self.hdr(DATA) + struct.pack('<III', seq, self.rcv_next, self.sack()) + payload)
        self.stats['tx'] += 1
        if len(self.unacked) >= WINDOW: self.can_send.clear()

    def resend(self, seq, now):
        u = self.unacked[seq]; u[2] = now; u[3] += 1; self.stats['resent'] += 1
        self.out(self.hdr(DATA) + struct.pack('<III', seq, self.rcv_next, self.sack()) + u[0])

    def rto(self): return min(1.0, max(0.06, self.srtt + 4 * self.rttvar))

    def on_ack(self, ack, sack):
        now = time.time()
        for s in [s for s in self.unacked if s < ack or (s > ack and s - ack - 1 < 32 and sack >> (s - ack - 1) & 1)]:
            u = self.unacked.pop(s)
            if u[3] == 1:                                       # round trip only from packets sent once (Karn)
                r = now - u[1]; self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - r); self.srtt = 0.875 * self.srtt + 0.125 * r
        if ack == self.last_ack_seen and self.unacked and ack in self.unacked and sack:
            self.dup += 1
            if self.dup == 2: self.resend(ack, now)              # later ones arrived, this one not: send it again now
        else: self.dup = 0
        self.last_ack_seen = ack
        if len(self.unacked) < WINDOW: self.can_send.set()

    def on_data(self, seq, payload):
        if seq < self.rcv_next or seq in self.ooo: return       # a copy
        if seq - self.rcv_next > 4 * WINDOW: return
        self.ooo[seq] = payload
        if seq != self.rcv_next: self.stats['lost_in'] += 1
        while self.rcv_next in self.ooo:
            d = self.ooo.pop(self.rcv_next); self.rcv_next += 1; self.stats['rx'] += 1
            if self.w and d: self.w.write(d)

    async def run(self):
        try:
            r, self.w = await asyncio.wait_for(asyncio.open_connection(*BR), 5)
        except Exception as e:
            log(ev='br_fail', cid=self.cid, peer=self.addr[0], err=repr(e)); self.close(); return
        self.ready = True; self.out(self.hdr(SYNACK))
        log(ev='open', cid=self.cid, peer='%s:%d' % self.addr)
        try:
            while not self.closed:
                d = await r.read(MAXP)
                if not d: break
                await self.can_send.wait()
                self.send_data(d)
        except Exception: pass
        self.close()

    def close(self, send_fin=True):
        if self.closed: return
        self.closed = True
        if send_fin:
            for _ in range(3): self.out(self.hdr(FIN))
        try:
            if self.w: self.w.close()
        except Exception: pass
        self.gw.conns.pop(self.cid, None)
        log(ev='close', cid=self.cid, peer=self.addr[0], secs=round(time.time() - self.started, 1), srtt_ms=round(self.srtt * 1000),
            **self.stats)


class Gateway(asyncio.DatagramProtocol):
    def __init__(self): self.conns = {}

    def connection_made(self, tr): self.tr = tr

    def datagram_received(self, d, addr):
        if len(d) < 7 or d[:2] != b'DU': return
        t, cid = d[2], struct.unpack('<I', d[3:7])[0]; body = d[7:]
        c = self.conns.get(cid)
        if c and c.addr[0] != addr[0]: return                   # an id is bound to its address (the port may change: NAT)
        if t == SYN:
            if not c:
                if sum(1 for x in self.conns.values() if x.addr[0] == addr[0]) >= PER_IP: return
                c = self.conns[cid] = Conn(self, cid, addr); c.started = time.time()
                asyncio.ensure_future(c.run())
            elif c.ready: c.out(c.hdr(SYNACK))
            return
        if not c: return
        c.addr = addr; c.seen = time.time()
        if t == DATA and len(body) >= 12:
            seq, ack, sack = struct.unpack('<III', body[:12])
            c.on_ack(ack, sack); c.on_data(seq, body[12:]); c.send_ack()
        elif t == ACK and len(body) >= 8:
            c.on_ack(*struct.unpack('<II', body[:8]))
        elif t == FIN:
            c.close(send_fin=False)

    async def timers(self):
        while True:
            await asyncio.sleep(0.02)
            now = time.time()
            for c in list(self.conns.values()):
                if now - c.seen > IDLE: c.close(); continue
                if not c.ready: continue
                rto = c.rto()
                for s in sorted(c.unacked)[:64]:
                    if now - c.unacked[s][2] > rto * min(8, 2 ** (c.unacked[s][3] - 1)): c.resend(s, now)
                if now - c.sent > 1.0: c.send_ack()


async def main():
    loop = asyncio.get_running_loop()
    tr, gw = await loop.create_datagram_endpoint(Gateway, local_addr=('0.0.0.0', PORT))
    log(ev='start', port=PORT, battle_relay='%s:%d' % BR)
    await gw.timers()

if __name__ == '__main__':
    asyncio.run(main())
