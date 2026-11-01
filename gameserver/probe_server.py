#!/usr/bin/env python3
# probe_server.py - what gets through from a player to our game servers (01.11.2026, the NetProbe experiment).
# Players in Russia and Ukraine lose our TCP game connection after ~16-30 KB (DPI on the way); this answers NetProbe.dll
# on several ports so we see what passes there: TCP downloads, UDP echo and UDP bursts.
#
# TCP (every port in PORTS):  "PRB1" + u32 size  -> size random bytes (up to 1 MB), then close
# UDP (every port in PORTS):  "PU0"               -> "PC0" + cookie (8 bytes, for this address, valid ~2 min)
#                             "PU1" + any (<=1400)-> the same datagram back (echo; one per datagram, no amplification)
#                             "PU2" + cookie + u16 count + u16 size -> count datagrams "PD2"+u16 seq+padding of size, 100/s
# A burst is answered only with a fresh cookie of the same address (so a forged sender address gets nothing) and at most
# one burst per address and port per 10 s, 300 datagrams at most. Every test is one line in the journal.
import asyncio, hashlib, hmac, os, struct, sys, time

PORTS = [int(p) for p in (sys.argv[1] if len(sys.argv) > 1 else '47110,3478,27015').split(',')]
KEY = os.urandom(16)
LAST_BURST = {}

def log(**k):
    print(time.strftime('%H:%M:%S') + ' ' + ' '.join('%s=%s' % kv for kv in k.items()), flush=True)

def cookie(ip, port, slot=None):
    slot = int(time.time() // 120) if slot is None else slot
    return hmac.new(KEY, ('%s:%d:%d' % (ip, port, slot)).encode(), hashlib.sha256).digest()[:8]

async def tcp_client(r, w, port):
    peer = w.get_extra_info('peername'); t0 = time.time(); sent = 0
    try:
        hdr = await asyncio.wait_for(r.readexactly(8), 15)
        if hdr[:4] != b'PRB1': return
        size = min(struct.unpack('>I', hdr[4:])[0], 1 << 20)
        while sent < size:
            n = min(1400, size - sent); w.write(os.urandom(n)); sent += n
            await w.drain()
    except Exception as e:
        log(ev='tcp_err', port=port, peer=peer[0], sent=sent, err=type(e).__name__)
    finally:
        log(ev='tcp', port=port, peer=peer[0], sent=sent, secs=round(time.time() - t0, 1))
        try: w.close()
        except Exception: pass

class Udp(asyncio.DatagramProtocol):
    def __init__(self, port): self.port = port
    def connection_made(self, t): self.t = t
    def datagram_received(self, d, addr):
        ip, p = addr[0], addr[1]
        if d[:3] == b'PU0': self.t.sendto(b'PC0' + cookie(ip, p), addr)
        elif d[:3] == b'PU1' and len(d) <= 1400: self.t.sendto(d, addr)
        elif d[:3] == b'PU2' and len(d) >= 15:
            c, count, size = d[3:11], *struct.unpack('>HH', d[11:15])
            if c not in (cookie(ip, p), cookie(ip, p, int(time.time() // 120) - 1)): return
            now = time.time()
            if now - LAST_BURST.get((ip, self.port), 0) < 10: return
            LAST_BURST[(ip, self.port)] = now
            asyncio.ensure_future(self.burst(addr, min(count, 300), max(16, min(size, 1400))))
    async def burst(self, addr, count, size):
        for i in range(count):
            self.t.sendto(b'PD2' + struct.pack('>H', i) + os.urandom(size - 5), addr)
            await asyncio.sleep(0.01)
        log(ev='udp_burst', port=self.port, peer=addr[0], count=count, size=size)

async def main():
    loop = asyncio.get_running_loop()
    for port in PORTS:
        await asyncio.start_server(lambda r, w, port=port: tcp_client(r, w, port), '0.0.0.0', port)
        await loop.create_datagram_endpoint(lambda port=port: Udp(port), local_addr=('0.0.0.0', port))
    log(ev='start', ports=','.join(map(str, PORTS)))
    while True:
        await asyncio.sleep(3600)
        cut = time.time() - 60
        for k in [k for k, t in LAST_BURST.items() if t < cut]: LAST_BURST.pop(k, None)

if __name__ == '__main__':
    asyncio.run(main())
