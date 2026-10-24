import sys, struct
sys.path.insert(0, r"E:\DEV\retro\decomp.wtf\src\decompnet\rom")
from mos6502 import disassemble, flow
sys.path.insert(0, r"E:\DEV\retro\decomp.wtf")
from c64_basic import parse_basic

PRG = r"E:\DEV\retro\retro.wtf\c64-work\out\MICROCOSM.prg"

with open(PRG, "rb") as f:
    raw = f.read()

lo = raw[0]
hi = raw[1]
base = lo | (hi << 8)
print(f"PRG {len(raw)} bytes, load addr ${base:04x}, data offset 2")
print(f"raw first bytes: {raw[:16].hex(' ')}")
print("=" * 60)

data = raw[2:]  # bytes start at $base

# Disassemble first ~60 instructions showing address/text/len
count = int(sys.argv[1]) if len(sys.argv) > 1 else 60
# start at the BASIC stub's SYS target (or argv[2], e.g. 0x0820), not the stub itself
targets = [t[0] for l in parse_basic(data, base)["lines"] for t in l["entry_targets"]]
entry = int(sys.argv[2], 0) if len(sys.argv) > 2 else (targets[0] if targets else base)
print(f"entry ${entry:04x}")
for addr, text, ln in disassemble(data, base, count, start=entry - base):
    print(f"${addr:04x}  {text}")
