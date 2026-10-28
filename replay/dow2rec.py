#!/usr/bin/env python3
"""DoW2 (Retribution / Elite mod) replay (.rec) parser.

Format: header + two embedded "Relic Chunky" trees + action stream.
Spec source: http://dow2replay.pbworks.com/w/page/18160568/File%20Format
"""
import struct, sys, json, re

# decoded empirically vs live viewing — see REFERENCE.md
ACTION_TYPES = {
    0x03: "build_unit",
    0x05: "cancel_queue",
    0x09: "unknown_hq",
    0x0B: "rally_point",
    0x0F: "tier/wargear_research",
    0x17: "buy_leader/hero_wargear",
    0x2B: "stance_toggle",
    0x2C: "move",
    0x2F: "capture_point",
    0x30: "attack_target",
    0x31: "attack_move",
    0x32: "ability",
    0x34: "move_variant",
    0x35: "ability",
    0x38: "garrison/load",
    0x3D: "retreat",
    0x46: "special_shot",
    0x47: "weapon_upgrade",
    0x4E: "construct_building",
    0x61: "game_end_marker",  # sender is NOT necessarily the one who surrendered
}

def u32(d, o): return struct.unpack_from('<I', d, o)[0]
def utf16(d, o, chars): return d[o:o + chars * 2].decode('utf-16-le', 'replace')


class Reader:
    def __init__(self, d, o=0):
        self.d, self.o = d, o
    def u8(self):
        v = self.d[self.o]; self.o += 1; return v
    def u16(self):
        v = struct.unpack_from('<H', self.d, self.o)[0]; self.o += 2; return v
    def u32(self):
        v = u32(self.d, self.o); self.o += 4; return v
    def f32(self):
        v = struct.unpack_from('<f', self.d, self.o)[0]; self.o += 4; return v
    def utf16(self):
        n = self.u32(); s = utf16(self.d, self.o, n); self.o += n * 2; return s
    def ascii(self):
        n = self.u32(); s = self.d[self.o:self.o + n].decode('latin-1'); self.o += n; return s
    def skip(self, n): self.o += n


def parse_chunks(d, off, end):
    """Yield (type_id, version, payload_offset, payload_size) walking one chunky level."""
    while off + 28 <= end:
        tid = d[off:off + 8]
        if tid[:4] not in (b'FOLD', b'DATA'):
            break
        ver, size, nlen = struct.unpack_from('<III', d, off + 8)
        payload = off + 28 + nlen
        yield tid.decode('ascii'), ver, payload, size
        off = payload + size


def parse(path):
    d = open(path, 'rb').read()
    out = {}
    out['file'] = path
    out['header_dword0'] = u32(d, 0)
    assert d[12:20] == b'DOW2_REC', 'not a DoW2 replay'
    # UTF-16 date right after magic+version byte
    m = re.match(rb'(?:[\x20-\x7e]\x00)+', d[0x14:0x60])
    out['recorded'] = m.group().decode('utf-16-le') if m else None

    # ---- chunky #1: FOLDPOST/DATADATA (tick count) ----
    c1 = d.find(b'Relic Chunky\r\n\x1a\x00')
    first = u32(d, c1 + 24)
    for tid, ver, po, ps in parse_chunks(d, c1 + first, len(d)):
        if tid == 'FOLDPOST':
            for tid2, v2, po2, ps2 in parse_chunks(d, po, po + ps):
                if tid2 == 'DATADATA':
                    out['ticks'] = u32(d, po2)
                    out['duration_s'] = out['ticks'] // 10
            stream_guess = po + ps
        break

    # ---- chunky #2: DATASDSC (map) + FOLDINFO (players) ----
    c2 = d.find(b'Relic Chunky\r\n\x1a\x00', c1 + 16)
    first2 = u32(d, c2 + 24)
    players = []
    stream_start = None
    for tid, ver, po, ps in parse_chunks(d, c2 + first2, len(d)):
        if tid == 'DATASDSC':
            r = Reader(d, po)
            r.skip(4)
            out['map_built'] = r.utf16()
            r.skip(4)                        # 04 00 00 00
            assert d[r.o:r.o+4] == b'DOW2'; r.skip(4)
            out['map'] = r.ascii()
            out['map_locname_id'] = None
            mm = re.search(rb'\$\x00[0-9\x00]+', d[po:po + ps])
            if mm:
                out['map_locname_id'] = mm.group().decode('utf-16-le')
        elif tid == 'FOLDINFO':
            for tid2, v2, po2, ps2 in parse_chunks(d, po, po + ps):
                if tid2 == 'FOLDGPLY':
                    for tid3, v3, po3, ps3 in parse_chunks(d, po2, po2 + ps2):
                        if tid3 == 'DATAINFO':
                            r = Reader(d, po3)
                            p = {'name': r.utf16(),
                                 'type': r.u32(),   # 0 host / 3 player / 5 observer / 7 empty
                                 'team': r.u32(),
                                 'race': r.ascii()}
                            if p['race']:
                                p['profile_id'] = r.u32()
                                r.skip(4)
                                p['army_level'] = r.u32()
                                r.skip(8)
                                p['hero'] = r.u32()  # 0/1/2 = commander choice per race
                            # trailing: badge path (ascii) + badge name (utf16)
                            tail = d[po3:po3 + ps3]
                            bm = re.search(rb'data:art\\badges\\[\x20-\x7e]+', tail)
                            if bm:
                                p['badge'] = bm.group().decode().split('\\')[-1]
                            players.append(p)
            stream_start = po + ps
    out['players'] = players

    # ---- action stream ----
    r = Reader(d, stream_start)
    ticks = 0
    last_tick = 0
    chat = []
    actions = []          # (tick, player_nibble, action_type, raw)
    while r.o + 8 <= len(d):
        rectype = r.u32()
        reclen = r.u32()
        recend = r.o + reclen
        if rectype == 1:                                  # chat / broadcast msg
            rr = Reader(d, r.o)
            rr.skip(8)
            rr.skip(16)
            try:
                n = u32(d, rr.o)
                if 0 < n < 200:
                    chat.append({'tick': last_tick, 'text': utf16(d, rr.o + 4, n)})
            except Exception:
                pass
        elif rectype == 0:                                # action tick
            rr = Reader(d, r.o)
            rr.skip(1)                                    # 0x20
            tick = rr.u32()
            last_tick = tick
            ticks += 1
            rr.skip(8)                                    # counter + varying dword
            nbundles = rr.u32()
            for _ in range(nbundles):
                rr.skip(8)                                # bundle id + unk
                blen = rr.u32()
                rr.skip(1)                                # total-length byte
                bend = rr.o + blen
                while rr.o < bend and rr.o < recend:
                    alen = d[rr.o]
                    if alen < 8:
                        break
                    a = d[rr.o:rr.o + alen]               # action incl. len byte
                    atype = a[2]
                    base = struct.unpack_from('<H', a, 4)[0]
                    # pid follows map start positions, NOT DATAINFO slot order;
                    # teams group together but order within a team is the map
                    # spawn order (temp.rec: 0=SX-R,1/2=Mago/Perfect, 3=mimicrrrr,
                    # 4=Insomnia, 5=mishai) — names can't be mapped from file alone
                    pid = base - 1000                     # e8 03 = player 0
                    actions.append((tick, pid, atype, a))
                    rr.o += alen
        else:
            break
        r.o = recend
    out['stream_ticks'] = ticks
    out['last_tick'] = last_tick
    out['chat'] = chat
    return out, actions


if __name__ == '__main__':
    meta, actions = parse(sys.argv[1])
    print(json.dumps({k: v for k, v in meta.items() if k != 'chat'},
                     ensure_ascii=False, indent=2))
    print('\nchat/broadcast messages:', len(meta['chat']))
    for c in meta['chat'][:20]:
        print('  tick %6d  %s' % (c['tick'], c['text']))
    print('\ntotal actions:', len(actions))
    from collections import Counter
    by_player = Counter(a[1] for a in actions)
    print('actions by player-nibble:', dict(by_player))
    by_type = Counter(a[2] for a in actions)
    print('actions by type:')
    for t, n in by_type.most_common(25):
        print('  0x%02X %-22s %5d' % (t, ACTION_TYPES.get(t, '?'), n))
