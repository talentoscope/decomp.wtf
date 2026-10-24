"""Commodore 64 containers, BASIC, unpacking, labels and the assembly judge."""

import struct
from pathlib import Path

import pytest

from decompnet.rom import c64, c64_asm, c64_basic, c64_hw, detect
from decompnet.rom.n64 import RomError

SAMPLE_T64 = Path("/tmp/c64media/MICROCOS.T64")
SAMPLE_PRG = Path("/tmp/c64media/MICROCOSM.prg")


def _prg(load: int, payload: bytes, name: str = "demo.prg") -> bytes:
    return struct.pack("<H", load) + payload


def _basic(load: int, target: int, code: bytes) -> bytes:
    """One BASIC line ``10 SYS <target>`` and ``code`` placed at ``target``."""
    body = bytes([0x9E]) + str(target).encode("ascii") + b"\x00"
    content = struct.pack("<H", 10) + body
    link = load + 2 + len(content)
    program = struct.pack("<H", link) + content + b"\x00\x00"
    pad = target - (load + len(program))
    assert pad >= 0
    return program + bytes(pad) + code


def test_basic_sys_spacing_parens_and_stale_pointer():
    load = 0x0801
    stale = bytearray(b"\x1f\x08" + struct.pack("<H", 1992) + b"\x9e" + b" 2080 BROKEN" + b"\x00\x00\x00")
    program = c64_basic.decode(bytes(stale), load)
    assert program.sys == [2080]
    assert "SYS" in program.text and "2080" in program.text
    assert program.end == load + len(stale)

    paren = _basic(load, 2061, b"")
    # rebuild the line with SYS(2061) in place of SYS2061
    raw = bytearray(paren)
    text = raw.split(b"\x9e", 1)
    raw = text[0] + b"\x9e(2061)" + text[1][4:]  # drop the four digits already there
    # the link and end marker are now stale; the walker must still find the number
    got = c64_basic.decode(bytes(raw), load)
    assert got.sys == [2061]

    # a second line would be needed for RUN; one line that is PRINT "SYS 9999" must not yield it
    line = struct.pack("<HH", load + 20, 20) + b"\x99" + b'"SYS 9999"' + b"\x00\x00\x00"
    only_quotes = c64_basic.decode(line, load)
    assert only_quotes.sys == []
    run = struct.pack("<HH", load + 16, 100) + b"\x8a" + b"100" + b"\x00\x00\x00"
    assert c64_basic.decode(run, load).sys == []
    load_stmt = struct.pack("<HH", load + 20, 10) + b"\x93" + b'"FILE"' + b"\x00\x00\x00"
    assert c64_basic.decode(load_stmt, load).sys == []


def test_prg_header_and_t64_payload_has_no_load_word():
    blob = _prg(0x0801, b"\x9e\x00")
    block = c64.read_prg(blob, "demo.prg")[0]
    assert block.load == 0x0801 and block.data == b"\x9e\x00"
    raw = bytearray(0x62)
    raw[0:19] = b"C64 tape image file"
    struct.pack_into("<HH", raw, 0x22, 1, 1)
    struct.pack_into("<BBHHHI", raw, 0x40, 1, 0, 0x0801, 0x0803, 0, 0x60)
    raw[0x50:0x54] = b"DEMO"
    raw[0x60:0x62] = b"\x9e\x00"
    block = c64.read_t64(bytes(raw), "demo.t64")[0]
    assert block.load == 0x0801 and block.data == b"\x9e\x00" and block.name.startswith("DEMO")


def test_gcr_nibble_and_round_trip():
    encoded = c64.gcr_encode(b"\x01\x00\x00\x00")
    assert encoded[0] == 0x52
    assert c64.gcr_decode(encoded).startswith(b"\x01\x00\x00\x00")
    payload = bytes(range(32))
    assert c64.gcr_decode(c64.gcr_encode(payload)) == payload


def _d64_with(payload: bytes, load: int = 0xC000) -> bytes:
    image = bytearray(174848)
    bam = c64._track_offset(18, 0)
    image[bam + 2:bam + 4] = b"A\x32"
    directory = bytearray(256)
    directory[0:2] = b"\x00\x00"
    entry = bytearray(32)
    entry[0] = 0x82
    entry[1], entry[2] = 17, 0
    name = b"DEMO"
    entry[3:3 + len(name)] = name
    entry[3 + len(name):19] = b"\xa0" * (16 - len(name))
    directory[2:34] = entry
    image[c64._track_offset(18, 1):c64._track_offset(18, 1) + 256] = directory
    file_bytes = struct.pack("<H", load) + payload
    sector = bytearray(256)
    sector[0] = 0
    sector[1] = 2 + len(file_bytes)
    sector[2:2 + len(file_bytes)] = file_bytes
    image[c64._track_offset(17, 0):c64._track_offset(17, 0) + 256] = sector
    return bytes(image)


def test_d64_closed_prg():
    image = _d64_with(b"\xa9\x00\x60")
    block = c64.read_d64(image, "demo.d64")[0]
    assert block.load == 0xC000 and block.data == b"\xa9\x00\x60" and block.name.startswith("DEMO")
    assert c64.sniff(image, "demo.d64") == "d64"


def _g64_track(sectors: dict[int, tuple[int, bytes]]) -> bytes:
    blob = bytearray()
    for sector, (track, data) in sectors.items():
        header = bytes([0x08, 0x00, sector & 0xFF, track & 0xFF, 0x31, 0x30, 0x00, 0x00])
        checksum = 0
        for b in data:
            checksum ^= b
        block = bytes([0x07]) + data + bytes([checksum & 0xFF, 0x00, 0x00])
        blob += b"\xff" * 8 + c64.gcr_encode(header) + b"\xff" * 8 + c64.gcr_encode(block)
    return bytes(blob)


def test_g64_directory_round_trip():
    file_bytes = struct.pack("<H", 0xC000) + b"\xa9\x01\x60"
    file_sector = bytearray(256)
    file_sector[0] = 0
    file_sector[1] = 2 + len(file_bytes)
    file_sector[2:2 + len(file_bytes)] = file_bytes
    directory = bytearray(256)
    entry = bytearray(32)
    entry[0] = 0x82
    entry[1], entry[2] = 17, 0
    entry[3:7] = b"DEMO"
    entry[7:19] = b"\xa0" * 12
    directory[2:34] = entry
    track17 = _g64_track({0: (17, bytes(file_sector))})
    track18 = _g64_track({1: (18, bytes(directory))})
    ntracks = 70
    offsets = [0] * ntracks
    body = bytearray(12 + ntracks * 4)
    body[0:8] = b"GCR-1541"
    body[8] = 0
    body[9] = ntracks

    def place(index: int, track: bytes) -> None:
        off = len(body)
        offsets[index] = off
        body.extend(struct.pack("<H", len(track)))
        body.extend(track)

    place(32, track17)  # track 17, even index
    place(34, track18)
    for i, off in enumerate(offsets):
        struct.pack_into("<I", body, 12 + 4 * i, off)
    block = c64.read_g64(bytes(body), "demo.g64")[0]
    assert block.load == 0xC000 and block.data == b"\xa9\x01\x60"


def test_tap_round_trip_and_crt_cold_start():
    payload = b"\xa9\x01\x60"
    image = c64.encode_tap(payload, 0xC000, "DEMO")
    blocks = c64.read_tap(image, "demo.tap")
    assert blocks[0].load == 0xC000 and blocks[0].data == payload
    body = bytes([0x00, 0x80, 0x00, 0x00]) + b"CBM80" + b"\x60"
    header = bytearray(0x40)
    header[0:13] = b"C64 CARTRIDGE"
    header[0x10:0x14] = (0x40).to_bytes(4, "big")
    chip = bytearray(b"CHIP")
    chip += (16 + len(body)).to_bytes(4, "big")
    chip += (0).to_bytes(4, "big")
    chip += (0x8000).to_bytes(2, "big")
    chip += len(body).to_bytes(2, "big")
    chip += body
    cart = c64.analyze(bytes(header) + bytes(chip), "cart.crt")
    assert cart.entry == 0x8000 and cart.method == "static"


def test_small_copy_is_the_unpacked_entry():
    # Eight bytes is under the relocated-loader threshold, so the jump is the entry.
    # x = 8..1 copies payload[7]..payload[0] through sta $BFFF,x onto $C000.
    payload = bytes([0xA9, 0x00, 0x8D, 0x20, 0xD0, 0x60, 0xEA, 0xEA])
    origin = 0x0820
    payload_at = origin + 14  # ldx, lda abs,x, sta abs,x, dex, bne, jmp
    mover = "\n".join([
        "ldx #$08",
        f"lda ${payload_at - 1:04X},x",
        "sta $BFFF,x",
        "dex",
        f"bne ${origin + 2:04X}",
        "jmp $C000",
        ".byte " + ",".join(f"${b:02X}" for b in payload),
    ])
    probe = c64.assemble(mover, origin)
    program = _basic(0x0801, origin, probe)
    assert probe == program[origin - 0x0801:]
    image = c64.analyze(_prg(0x0801, program), "unpack.prg")
    assert image.entry == 0xC000 and image.method == "emulated"
    assert image.ram[0xC000:0xC006] == bytes([0xA9, 0x00, 0x8D, 0x20, 0xD0, 0x60])
    fns, _regions = c64.functions(image.ram, image.entry)
    assert any(f.vram == 0xC000 for f in fns)
    listing = next(f.asm for f in fns if f.vram == 0xC000)
    assert "VIC_BORDERCOLOR" in listing


def test_labels_on_operands_and_in_the_header():
    text = c64_hw.name_listing("  lda #$01\n  sta $d020\n  jsr $ffd2\n  lda $01\n  sta $a000\n")
    assert "#$01" in text
    assert "VIC_BORDERCOLOR" in text
    assert "CHROUT" in text
    assert "CPU_PORT" in text
    assert "$a000" in text.lower()
    header = c64_hw.context_header()
    assert "CHROUT" in header and "VIC_BORDERCOLOR" in header and "output character" in header
    assert "$01 = $37" in header or "$01 = $37" in header.replace(" ", "")
    assert c64_hw.name_of(0xFFD2, call=True) == "CHROUT"
    assert c64_hw.name_of(0xA000, call=False) is None


def test_detect_and_page_identify_a_prg(tmp_path):
    payload = _basic(0x0801, 0x080D, b"\x60")
    data = _prg(0x0801, payload)
    picked = detect.identify(data, "demo.prg")
    assert picked.platform == "c64" and picked.filename == "memory.c64" and picked.big_endian is False
    assert picked.info["sys"] == 0x080D
    image = c64.parse(picked.data)
    assert image.entry == 0x080D
    from .test_sms import _page_identify
    info = _page_identify(b"C64 tape image file" + b"\x00" * 32, "x.bin")
    assert info["platform"] == "c64" and info["order"] == "T64"
    assert _page_identify(data, "demo.prg")["order"] == "PRG"
    assert _page_identify(b"\x00" * 64, "demo.d64")["order"] == "D64"
    with pytest.raises(RomError):
        detect.identify(b"\x00" * 64, "notes.txt")


def test_asm_judge_accepts_names_and_rejects_a_bare_address():
    code = c64.assemble("lda #$00\nsta $D020\nrts\n", 0xC000)
    routine = c64_asm.Routine("func_c000", 0xC000, code, {})
    good = "; paints the border black\npaint:\n  lda #$00\n  sta VIC_BORDERCOLOR\n  rts\n"
    verdict = c64_asm.judge(routine, good)
    assert verdict.ok, verdict.problems
    assert verdict.label == "paint"
    bare = "; paints the border black\npaint:\n  lda #$00\n  sta $D020\n  rts\n"
    rejected = c64_asm.judge(routine, bare)
    assert not rejected.ok and any(p.startswith("5.") for p in rejected.problems)
    branched = c64.assemble("lda #$01\nbne $C006\nlda #$02\nrts\n", 0xC000)
    text = "; skips the second load\nspin:\n  lda #$01\n  bne done\n  lda #$02\ndone:\n  rts\n"
    forward = c64_asm.judge(c64_asm.Routine("func_c000", 0xC000, branched, {}), text)
    assert forward.ok, forward.problems


def test_import_puts_routines_on_the_assembly_track(tmp_path):
    from decompnet.app.engine import RomNetwork

    payload = _basic(0x0801, 0x080D, b"\x60")
    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, payload))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    assert proj["arch"] == "6510"
    ov = net.overview(slug)
    assert ov["platform"] == "c64" and ov["compiler"]["toolchain"] == "py65"
    assert ov["built_with"] is None and ov["exportable"] is False
    rows = net.store.all("SELECT name, status, meta, vram FROM functions WHERE project_id=?", proj["id"])
    assert rows and all(r["status"] == "asm" for r in rows)
    assert all(net.asm_track(r) for r in rows)
    header = net._context(proj["id"])["header"]
    assert "CHROUT" in header and "VIC_BORDERCOLOR" in header
    symbol = net.store.one("SELECT address, kind FROM symbols WHERE project_id=? AND name='CHROUT'", proj["id"])
    assert symbol["address"] == 0xFFD2 and symbol["kind"] == "func"
    agent, _token = net.local_agent("maxi")
    brief = net.claim_asm(agent, slug)
    assert "py65" in brief
    source = "; return\nfinished:\n  rts\n"
    fn = net.store.one("SELECT name, target FROM functions WHERE project_id=? AND vram=?", proj["id"], 0x080D)
    assert bytes(fn["target"]) == b"\x60"
    result = net.document_function(agent, slug, fn["name"], source)
    assert result.startswith("✅"), result


@pytest.mark.skipif(not SAMPLE_T64.is_file(), reason="sample tape image is not on this machine")
def test_sample_tape_reaches_the_unpacked_entry():
    image, info = c64.identify(SAMPLE_T64.read_bytes(), SAMPLE_T64.name)
    assert info["entry"] == 0x9700 and info["method"] == "emulated"
    assert info["sys"] == 2080
    fns = c64.split(image)
    assert len(fns) == 28
    assert any(f.vram == 0x9700 for f in fns)
    if SAMPLE_PRG.is_file():
        other, oinfo = c64.identify(SAMPLE_PRG.read_bytes(), SAMPLE_PRG.name)
        assert oinfo["entry"] == 0x9700 and len(c64.split(other)) == len(fns)
