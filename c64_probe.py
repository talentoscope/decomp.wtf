import sys

PRG = r"E:\DEV\retro\retro.wtf\c64-work\out\MICROCOSM.prg"
with open(PRG, "rb") as f:
    raw = f.read()
data = raw[2:]
base = raw[0] | (raw[1] << 8)
print(f"Total: {len(raw)} bytes ({len(data)} data) at ${base:04x}")
print(f"range ${base:04x} .. ${base+len(data)-1:04x}")
print("=" * 60)

# 1) printable ascii strings (len>=4)
s = b""
runs = []
for b in data:
    if 32 <= b < 127:
        s += bytes([b])
    else:
        if len(s) >= 5:
            runs.append(s.decode("latin1"))
        s = b""
if len(s) >= 5:
    runs.append(s.decode("latin1"))
print("STRING RUNS:")
for r in runs:
    print("  ", r)

print("=" * 60)
# 2) tail: last 128 bytes disassembled
import importlib.util
spec = importlib.util.spec_from_file_location("mos", r"E:\DEV\retro\decomp.wtf\src\decompnet\rom\mos6502.py")
mos = importlib.util.module_from_spec(spec); spec.loader.exec_module(mos)
print("TAIL disasm (last 128 bytes):")
tail_off = len(data) - 128
for addr, text, ln in mos.disassemble(data, base, 40, start=tail_off):
    print(f"${addr:04x}  {text}")
