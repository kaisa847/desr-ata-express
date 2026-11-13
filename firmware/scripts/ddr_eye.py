# ddr_eye.py - checks in the raw DDR trace, per grid offset, whether the stream of the rising or
# falling halves matches the CRC16 (all 4 lines). Usage: ddr_eye.py <jtag_trace output file>
import sys; import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ddr_cap
from ddr_cap import crc16
h,raw=ddr_cap.parse(open(sys.argv[1],encoding='utf-8',errors='replace').read().splitlines())
s=ddr_cap.samples(raw)
f0=next(i for i,v in enumerate(s) if v==0)
base=f0+4
def chk(st):
    d=[s[st+4*k] for k in range(512)]; c=[s[st+2048+4*k] for k in range(16)]
    return ''.join('1' if crc16([(n>>b)&1 for n in d])==int(''.join(str((x>>b)&1) for x in c),2) else '.' for b in range(4))
print('first 0 at', f0)
for off in range(-3,6):
    print('grid offset %+d: rising stream %s   falling stream %s' % (off, chk(base+off), chk(base+2+off)))
