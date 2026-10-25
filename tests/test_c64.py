"""Commodore 64 containers, BASIC, unpacking, labels and the assembly judge."""

import json
import struct
from pathlib import Path

import pytest

from decompnet.rom import c64_find, c64, c64_asm, c64_basic, c64_hw, detect
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


def test_asm_judge_zero_page_names_take_the_snapshots_form():
    # the program addresses $001E absolutely (BD 1E 00) and $46 as zero page (85 46)
    code = c64.assemble("lda $001E,x\nsta $46\nrts\n", 0x747D)
    assert code[:3] == b"\xbd\x1e\x00"
    routine = c64_asm.Routine("func_747d", 0x747D, code, {"wSrcBuf": 0x1E, "wInitBuf": 0x46})
    named = "; copies one source byte to the init buffer\ncopy_src:\n  lda wSrcBuf,x\n  sta wInitBuf\n  rts\n"
    verdict = c64_asm.judge(routine, named)
    assert verdict.ok, verdict.problems
    local = "wMine = $001E\n; copies one source byte to the init buffer\ncopy_src:\n  lda wMine,x\n  sta wInitBuf\n  rts\n"
    assert c64_asm.judge(routine, local).ok
    bare = "; copies one source byte to the init buffer\ncopy_src:\n  lda $001E,x\n  sta wInitBuf\n  rts\n"
    rejected = c64_asm.judge(routine, bare)
    assert not rejected.ok and any("leaves $001E as a number" in p for p in rejected.problems)
    short = "; copies one source byte to the init buffer\ncopy_src:\n  lda $1E,x\n  sta wInitBuf\n  rts\n"
    wrong = c64_asm.judge(routine, short)
    assert not wrong.ok and any("line 3:" in p and "four digits" in p for p in wrong.problems), wrong.problems


def test_asm_judge_takes_name_offsets_for_tables_and_self_modifying_code():
    # func_e000 from Ping_Pong: copies a handler address from the table at $E00F into its own jmp, then takes it
    code = c64.assemble("lda $E00F,y\nsta $E00D\nlda $E010,y\nsta $E00E\njmp $E2B7\n", 0xE000)
    routine = c64_asm.Routine("func_e000", 0xE000, code, {"call_timer": 0xE2B7}, own={0xE2B7: "call_timer"})
    good = ("; jumps to the handler Y picks from the table at $E00F\ndispatch:\n  lda HANDLERS,y\n  sta patch+1\n"
            "  lda HANDLERS+1,y\n  sta patch+2\npatch:\n  jmp call_timer\nHANDLERS = $E00F\n")
    verdict = c64_asm.judge(routine, good)
    assert verdict.ok, verdict.problems
    bare = c64_asm.judge(routine, good.replace("HANDLERS+1,y", "$E010,y"))
    assert not bare.ok and any("leaves $E010 as a number" in p for p in bare.problems), bare.problems


def test_c64_code_under_the_rom_keeps_the_programs_names():
    """A program's routine where the ROM normally is: a jsr to it uses the project's name, not the ROM's."""
    rom_name = c64_hw.name_of(0xE2B4, call=True)
    assert rom_name  # (a BASIC routine name lives there)
    code = c64.assemble("jsr $E2B4\nrts\n", 0xE000)
    routine = c64_asm.Routine("func_e000", 0xE000, code, {rom_name: 0xE2B4, "tick_timer": 0xE2B4},
                              own={0xE2B4: "tick_timer", 0xE000: "func_e000"})
    verdict = c64_asm.judge(routine, "; runs one timer tick\ntick:\n  jsr tick_timer\n  rts\n")
    assert verdict.ok, verdict.problems
    assert "jsr tick_timer" in c64_asm.draft(routine)
    assert c64_hw.name_listing("e000  jsr $E2B4", {0xE2B4: "tick_timer"}) == "e000  jsr tick_timer"
    assert c64_hw.name_listing("e000  jsr $E2B4") == f"e000  jsr {rom_name}"
    from decompnet.app.engine import _c64_under_rom
    assert (_c64_under_rom(0xE000), _c64_under_rom(0xA123), _c64_under_rom(0x1B22)) == ("KERNAL", "BASIC", "")


# func_5965 from Ping_Pong and the head the split missed: $5959 saves X/Y, waits for the key to be let go and pressed
_WAIT_KEY = bytes.fromhex("8a489848" "2042f1d0fb" "2042f1f0fb" "856068a868aaa56060")


def test_finder_moves_a_probable_start_back_to_the_head_it_branches_into():
    ram = bytearray(0x10000)
    ram[0x5954:0x5959] = bytes.fromhex("a90185fb60")  # the routine before it: lda #$01 / sta $FB / rts
    ram[0x5959:0x5959 + len(_WAIT_KEY)] = _WAIT_KEY
    ram[0x3000:0x3004] = bytes.fromhex("65596559")  # $5965 as a word elsewhere: the tail scores, the head doesn't
    result = c64_find.find(bytes(ram), 0x5954)
    starts = {f.start: f for f in result.functions}
    assert 0x5965 not in starts and 0x5959 in starts, sorted(hex(a) for a in starts)
    head = starts[0x5959]
    assert any("moved back from $5965" in w for w in head.why) and 0x596F in head.insns
    end, _islands = c64_find.body(head, set(starts))
    assert end == 0x5959 + len(_WAIT_KEY)


def test_asm_judge_says_where_a_label_has_to_go():
    tail = _WAIT_KEY[12:]
    r = c64_asm.Routine("func_5965", 0x5965, tail, {"w_0060": 0x60}, own={0x5965: "func_5965"},
                        free=[(0x5959, 0x5965)])
    body = "  sta w_0060\n  pla\n  tay\n  pla\n  tax\n  lda w_0060\n  rts\n"
    after = c64_asm.judge(r, "; restores X/Y and A\nwait_tail:\n  beq key_down\n" + body + "key_down:\n")
    said = " ".join(after.problems)
    assert not after.ok and "define the name instead: `key_down = $5962`" in said, after.problems
    assert 'create_function(at="$5959")' in said and 'merge_function("func_5965")' in said, after.problems
    defined = c64_asm.judge(r, "; restores X/Y and A\nwait_tail:\n  beq key_down\n" + body + "key_down = $5962\n")
    assert defined.ok, defined.problems
    # func_359d: a label one instruction early
    code = c64.assemble("ldy $d3\niny\ncpy $d5\nbcs $35ab\nlda $029f,y\nbeq $359f\nsty $d3\njmp $c917\n", 0x359D)
    r = c64_asm.Routine("func_359d", 0x359D, code, {"handler": 0xC917, "LEVELS": 0x029F, "w_d3": 0xD3, "w_d5": 0xD5})
    early = c64_asm.judge(r, "; skips blanks\nskip:\n  ldy w_d3\nnext:\n  iny\n  cpy w_d5\n  bcs done\n  lda LEVELS,y\n"
                             "  beq next\ndone:\n  sty w_d3\n  jmp handler\n")
    assert any("`done` sits at $35A9; $35AB is line 12 (`jmp handler`): put the label on that line" in p
               for p in early.problems), early.problems


def test_asm_judge_explains_lines_py65_cant_read():
    code = c64.assemble("clc\nlda $4b\nadc #$03\nsta $4b\nrts\n", 0x31BA)
    r = c64_asm.Routine("func_31ba", 0x31BA, code, {"w_004b": 0x4B})
    bare = c64_asm.judge(r, "; adds 3\nadd_3:\n  clc\n  LDA_004B\n  adc #$03\n  sta LDA_004B\n  rts\nLDA_004B = $4B\n")
    assert bare.problems == ["line 4: `LDA_004B` on its own isn't an instruction: a name goes after one "
                             "(`lda LDA_004B`), and a label takes a colon (`LDA_004B:`)"], bare.problems
    dollar = c64_asm.judge(r, "; adds 3\nadd_3:\n  clc\n  lda w_004b\n  adc #$03\n  sta w_004b\n  rts\n$5962_target:\n")
    assert any("`$5962_target` isn't a name" in p and "`target_5962:`" in p for p in dollar.problems), dollar.problems
    word = c64_asm.judge(r, "; adds 3\nadd_3:\n  clc\n  load w_004b\n  adc #$03\n  sta w_004b\n  rts\n")
    assert word.problems == ["line 4: `load` isn't a 6502 instruction"], word.problems


def test_c64_brief_points_at_bytes_no_routine_holds(tmp_path):
    from decompnet.app.engine import RomNetwork

    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, _basic(0x0801, 0x080D, bytes([0xA9, 0xFF, 0x85, 0x90, 0x60]))))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    agent, _token = net.local_agent("maxi")
    first = net.store.one("SELECT * FROM functions WHERE project_id=? AND vram=?", proj["id"], 0x080D)
    shift = first["rom_offset"] - 0x080D
    tail = _WAIT_KEY[12:]
    with net.store.tx() as c:  # as the split had it: the routine before at $0950, the head at $0959 in nobody's hands
        c.execute("INSERT INTO functions(project_id,name,size,n_insns,target,vram,rom_offset,status,meta,unit) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)", (proj["id"], "func_0950", 9, 3, bytes(9), 0x0950, shift + 0x0950,
                                                  "asm", first["meta"], first["unit"]))
        c.execute("INSERT INTO functions(project_id,name,size,n_insns,target,vram,rom_offset,status,meta,unit) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)", (proj["id"], "func_0965", len(tail), 8, tail, 0x0965, shift + 0x0965,
                                                  "asm", first["meta"], first["unit"]))
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND name='func_0965'", proj["id"])
    assert net._c64_free(proj, fn) == [(0x0959, 0x0965)]
    brief = net._asm_brief(fn, agent, "CLAIMED")
    assert "### Where the split looks wrong" in brief and 'create_function(at="$0959")' in brief, brief


# graphics read as code, as 1942's $A686 decodes: dec $2121, and ($21,x) four times, ror $2121, and ($b4,x), jmp $4c4c
_PATTERN = bytes.fromhex("ce2121" + "2121" * 4 + "6e2121" + "21b4" + "4c4c4c")
# a real routine: clear $1100-$11FF and return
_CLEAR = bytes.fromhex("a900a2009d0011e8d0fa60")


def test_asm_data_notes_need_two_signs_of_data():
    from decompnet.rom import c64_asm

    notes = c64_asm.data_notes(c64_asm.Routine("rotate_and_or", 0xA686, _PATTERN, {}, callers=0))
    assert len(notes) == 3, notes
    assert any("`and ($21,x)` at $A689 comes twice" in n for n in notes)
    assert any("$2121, an address made of one byte twice" in n for n in notes)
    assert any("no routine calls" in n for n in notes)
    # a real routine nothing calls (a vector's, say) shows one sign: not enough to speak up
    assert c64_asm.data_notes(c64_asm.Routine("clear", 0x1519, _CLEAR, {}, callers=0)) == []
    assert c64_asm.data_notes(c64_asm.Routine("clear", 0x1519, _CLEAR, {}, callers=2)) == []
    # the same bytes twice, running off the end: two signs
    twin = c64_asm.data_notes(c64_asm.Routine("ops", 0x3E88, _CLEAR[:-1], {}, callers=1, twins=[0x4E88]))
    assert len(twin) == 2 and "$4E88" in twin[1], twin


def test_c64_brief_says_when_a_routine_looks_like_data(tmp_path):
    from decompnet.app.engine import RomNetwork

    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, _basic(0x0801, 0x080D, bytes([0xA9, 0xFF, 0x85, 0x90, 0x60]))))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    agent, _token = net.local_agent("maxi")
    first = net.store.one("SELECT * FROM functions WHERE project_id=? AND vram=?", proj["id"], 0x080D)
    shift = first["rom_offset"] - 0x080D
    with net.store.tx() as c:
        for name, at, code in (("func_a686", 0xA686, _PATTERN), ("func_1519", 0x1519, _CLEAR)):
            c.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,vram,rom_offset,status,meta,"
                      "unit) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proj["id"], name, len(code), 8, code, "[]", at,
                                                              shift + at, "asm", first["meta"], first["unit"]))
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND name='func_a686'", proj["id"])
    assert net._c64_seen(proj, fn) == {"callers": 0, "twins": []}
    brief = net._asm_brief(fn, agent, "CLAIMED")
    assert "### This may be data, not code" in brief and "delete_function(" in brief, brief
    real = net.store.one("SELECT * FROM functions WHERE project_id=? AND name='func_1519'", proj["id"])
    assert "This may be data" not in net._asm_brief(real, agent, "CLAIMED")


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


# ---------------------------------------------------------------------------- VICE

def _vsf(ram: bytes, pc: int = 0x0810, sp: int = 0xF6, p01: int = 0x37) -> bytes:
    """A minimal VICE snapshot: the header, then MAINCPU and C64MEM modules."""
    def module(name: bytes, body: bytes) -> bytes:
        return name.ljust(16, b"\0") + bytes([1, 0]) + struct.pack("<I", 22 + len(body)) + body
    cpu = struct.pack("<Q", 0) + bytes([0, 0, 0, sp]) + struct.pack("<H", pc) + b"\0" * 8
    mem = bytes([p01 & 0xFF, 0x2F, 0, 0]) + ram  # the port, its direction register, EXROM, GAME
    return (b"VICE Snapshot File\x1a" + bytes([2, 0]) + b"C64SC".ljust(16, b"\0")
            + module(b"MAINCPU", cpu) + module(b"C64MEM", mem))


def test_vice_snapshot_finds_basic_through_its_pointers():
    ram = bytearray(0x10000)
    prg = _basic(0x0801, 0x0810, b"\x60")[2:]
    ram[0x0801:0x0801 + len(prg)] = prg
    end = c64_basic.decode(bytes(ram[0x0801:0xA000]), 0x0801).end
    ram[0x2B:0x2F] = struct.pack("<HH", 0x0801, end)
    image = c64.parse(c64.identify(_vsf(bytes(ram)), "game.vsf")[0])
    assert image.method == "snapshot" and image.entry == 0x0810 and image.regs == {"sp": 0xF6, "p01": 0x37}
    # BASIC's end pointer disagrees: not taken as a program, so the CPU's position is the start
    ram[0x2D:0x2F] = struct.pack("<H", 0x9000)
    found = c64.from_dump(c64.c64_vice.read_vsf(_vsf(bytes(ram), pc=0x1234)), "x.vsf", "snapshot")
    assert found.basic_text == "" and found.entry == 0x1234


def test_vice_is_a_pinned_tool_and_found_in_order(tmp_path, monkeypatch):
    from decompnet import toolchains
    from decompnet.rom import c64_vice

    tc = toolchains.get("vice")
    assert tc.kind == "tool" and tc.license.startswith("GPL") and all(p["sha256"] for p in tc.parts)
    monkeypatch.setenv("DECOMPNET_TOOLCHAINS", str(tmp_path / "tc"))
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("DECOMP_VICE", raising=False)
    assert c64_vice.find_vice() is None
    own = toolchains.path("vice") / "bin" / "x64sc.exe"
    own.parent.mkdir(parents=True)
    own.write_bytes(b"")
    (toolchains.path("vice") / toolchains.MANIFEST).write_text("{}")
    assert c64_vice.find_vice() == own
    given = tmp_path / "mine" / "bin" / "x64sc.exe"
    given.parent.mkdir(parents=True)
    given.write_bytes(b"")
    monkeypatch.setenv("DECOMP_VICE", str(tmp_path / "mine"))
    assert c64_vice.find_vice() == given


def test_tape_without_vice_installs_it_or_falls_back(monkeypatch):
    from decompnet import toolchains
    from decompnet.rom import c64_vice

    monkeypatch.setattr(c64_vice, "find_vice", lambda: None)
    monkeypatch.setattr(toolchains, "install", lambda *a, **k: pytest.fail("installed with VICE turned off"))
    monkeypatch.setenv("DECOMP_C64_VICE", "0")
    tap = c64.encode_tap(b"\xea\x60", 0xC000, "DEMO")
    assert c64.analyze(tap, "demo.tap").method != "vice"  # the KERNAL tape reader, as before
    monkeypatch.setenv("DECOMP_C64_VICE", "1")
    asked = []
    monkeypatch.setattr(toolchains, "install", lambda tid, **k: asked.append(tid))
    monkeypatch.setattr(toolchains.Toolchain, "supported", property(lambda self: True))
    c64.analyze(tap, "demo.tap", log=lambda *_: None)
    assert asked == ["vice"]  # still no VICE after "installing": read without it


# ---------------------------------------------------------------------------- function finding

def _finder_ram() -> bytearray:
    ram = bytearray(0x10000)
    main = bytes.fromhex("202008" "a9308d1403" "a9088d1503" "60")  # jsr $0820; install an IRQ handler at $0830; rts
    ram[0x0810:0x0810 + len(main)] = main
    ram[0x0820:0x0824] = bytes.fromhex("ee20d060")              # inc $d020; rts
    ram[0x0830:0x0836] = bytes.fromhex("ee21d04c31ea")          # inc $d021; jmp $ea31 (the KERNAL's IRQ)
    loose = bytes.fromhex("a9018d20d08d21d020d2ff60")             # nothing reaches it: lda/sta border, CHROUT, rts
    ram[0x0900:0x0900 + len(loose)] = loose
    ram[0x2000:0x2000 + len(loose)] = loose                       # inside the BASIC program: never code
    ram[0x0314:0x0316] = bytes.fromhex("31ea")
    return ram


def test_finder_layers_certain_probable_and_basic_kept_out():
    from decompnet.rom import c64_find

    result = c64_find.find(bytes(_finder_ram()), 0x0810, p01=0x37, basic=(0x2000, 0x2100))
    got = {f.start: f for f in result.functions}
    assert {a for a, f in got.items() if f.confidence == "certain"} == {0x0810, 0x0820, 0x0830}
    assert "CINV vector install" in got[0x0830].why  # found only through the pointer the code stores
    assert got[0x0900].confidence == "probable" and got[0x0900].score >= c64_find.MIN_SCORE
    assert not any(0x2000 <= a < 0x2100 for a in got)
    cov = result.coverage
    assert cov["certain_functions"] == 3 and cov["probable_functions"] >= 1 and cov["basic_bytes"] == 0x100


def test_split_reports_confidence_and_coverage():
    ram = _finder_ram()
    image = c64.Image(bytes(ram), 0x0810, 0, 0, 0, 0, "snapshot", "", "demo", regs={"sp": 0xFF, "p01": 0x37})
    report: dict = {}
    fns = c64.split(c64.pack(image), log=None, report=report)
    by = {f.vram: f for f in fns}
    assert by[0x0810].code.endswith(b"\x60") and report["found"][0x0900]["confidence"] == "probable"
    assert report["found"][0x0820]["confidence"] == "certain" and report["coverage"]["certain_bytes"] > 0


def test_agents_never_see_the_basic_program(tmp_path):
    """BASIC stays in the project's settings: no tool an agent calls shows its listing or its bytes."""
    from decompnet.app import tools
    from decompnet.app.engine import RomNetwork
    from decompnet.network import NetworkError

    payload = _basic(0x0801, 0x080D, b"\xa9\x01\x8d\x20\xd0\x60")
    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, payload))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    listing = [line for line in net.cfg(proj)["rom"]["basic"].splitlines() if line.strip()]
    assert listing and any("SYS" in line for line in listing)
    agent, _token = net.local_agent("maxi")
    names = [r["name"] for r in net.store.all("SELECT name FROM functions WHERE project_id=?", proj["id"])]
    calls = [("project_info", {}), ("get_context", {}), ("list_functions", {}), ("guide", {}),
             ("read_data", {"what": "$0801", "length": 64}), ("read_data", {"what": "0x0809", "length": 4}),
             ("read_data", {"what": "$07F0", "length": 64})] + [("get_function", {"function": n}) for n in names]
    out = []
    for tool, args in calls:
        try:
            out.append(str(tools.HANDLERS[tool](net, agent, slug, **args)))
        except (NetworkError, TypeError) as e:
            out.append(str(e))
    text = "\n".join(out)
    for line in listing:
        assert line.strip() not in text
    assert "9e 32 30 36 31" not in text and "2061" not in text  # the SYS line's bytes and its number
    assert "not part of the machine code" in text
    assert "a9 01 8d 20 d0 60" in net.read_data(slug, "$080D", 6)  # the code itself stays readable


def test_an_agent_flags_a_routine_that_is_not_code(tmp_path):
    """flag_for_review works on the assembly track: the routine leaves it for a person, and comes back if given back."""
    from decompnet.app.engine import RomNetwork
    from decompnet.network import NetworkError

    payload = _basic(0x0801, 0x080D, b"\x20\x11\x08\x60\xa9\x01\x60")
    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, payload))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    agent, _token = net.local_agent("maxi")
    net.claim_asm(agent, slug)
    held = net.store.one("SELECT f.* FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=?", agent.id)
    with pytest.raises(NetworkError):
        net.flag_for_review(agent, slug, held["name"], "too short")
    said = net.flag_for_review(agent, slug, held["name"], "not code: these bytes are a text table read by the loop")
    assert "set aside" in said, said
    fn = net.store.one("SELECT * FROM functions WHERE id=?", held["id"])
    assert fn["status"] == "asm" and not net.asm_track(fn)
    assert net.store.one("SELECT * FROM claims WHERE function_id=?", fn["id"]) is None
    names = set()
    while (row := net.claim_asm_row(agent, slug)) is not None:
        names.add(row["name"])
        with net.store.tx() as db:
            db.execute("DELETE FROM claims WHERE function_id=?", (row["id"],))
            db.execute("UPDATE functions SET status='documented' WHERE id=?", (row["id"],))
    assert held["name"] not in names
    assert net.give_to_agents(slug, held["name"])["status"] == "asm"
    assert net.asm_track(net.store.one("SELECT * FROM functions WHERE id=?", held["id"]))
    assert "EA3E" in net.read_data(slug, "$00EA3E", 4).upper()


def test_start_from_scratch_reverts_renames_and_agent_work(tmp_path):
    """Starting again puts back the import's names, header and queue: renames, documented routines, header edits and
    review flags are undone; the layout stays."""
    from decompnet.app.engine import RomNetwork

    payload = _basic(0x0801, 0x080D, b"\x20\x14\x08\x20\x17\x08\x60\xa9\x01\x60\xa9\x02\x60\xa9\x03\x60")
    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, payload))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    agent, _token = net.local_agent("maxi")
    q = lambda sql, *a: net.store.all(sql, proj["id"], *a)
    names = lambda: sorted(r["name"] for r in q("SELECT name FROM functions WHERE project_id=?"))
    syms = lambda: sorted((r["name"], r["address"]) for r in q("SELECT name, address FROM symbols WHERE project_id=?"))
    header = lambda: net._context(proj["id"])["header"]
    before_names, before_syms, before_header = names(), syms(), header()
    before_status = {r["name"]: r["status"] for r in q("SELECT name, status FROM functions WHERE project_id=?")}
    assert len(before_names) >= 3, before_names

    first, second, third = before_names[:3]
    assert "is now" in net.rename_symbol(agent, slug, first, "start_game")
    assert "is now" in net.rename_symbol(agent, slug, "start_game", "main_loop")  # a chain: undone newest first
    assert "is now" in net.rename_symbol(agent, slug, second, "draw_score")
    with net.store.tx() as db:  # an agent's header edit, as edit_context stores one
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,2,?,?,?,0,'')", (proj["id"], before_header + "\n/* an agent's note */", agent.id, "note"))
    assert header() != before_header
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='documented', matched_source='; done', meta=? WHERE project_id=? "
                   "AND name='draw_score'", ('{"sample": true}', proj["id"]))
    net.claim_asm(agent, slug)
    held = net.store.one("SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=?",
                         agent.id)
    assert "set aside" in net.flag_for_review(agent, slug, held["name"], "not code: a table of sprite pointers")
    assert "main_loop" in names() and "draw_score" in names()

    out = net.reset_project_progress(slug)
    assert out["renames"] == 3 and out["documented"] == 1 and out["reviews"] == 1, out
    assert names() == before_names
    assert syms() == before_syms
    assert header() == before_header
    rows = q("SELECT name, status, meta, matched_source FROM functions WHERE project_id=?")
    assert {r["name"]: r["status"] for r in rows} == before_status
    for r in rows:
        meta = json.loads(r["meta"] or "{}")
        assert not {"review", "review_track", "sample"} & set(meta) and r["matched_source"] is None
    assert all(net.asm_track(f) for f in q("SELECT * FROM functions WHERE project_id=?"))


def test_vice_tape_position_and_a_stop_with_tape_left():
    """The loader reads the tape's position from the cycle counter, and a stop with tape left is a prompt."""
    from decompnet.rom import c64_vice as v

    v0 = b"C64-TAPE-RAW" + bytes([0, 0, 0, 0]) + (3).to_bytes(4, "little") + bytes([0x30, 0x40, 0])
    assert v.tape_cycles(v0) == 8 * (0x30 + 0x40 + 256)
    v1 = b"C64-TAPE-RAW" + bytes([1, 0, 0, 0]) + (5).to_bytes(4, "little") + bytes([0x30, 0]) + (500000).to_bytes(3, "little")
    assert v.tape_cycles(v1) == 8 * 0x30 + 500000
    assert v.tape_cycles(b"not a tape") is None
    assert v.clock(v.PAL_HZ * 192) == "03:12"

    line = ".;e5cd 00 00 0a f3 2f 37 00100010 000 001   98524800"
    assert v.parse_state("  ADDR A  X  Y  SP 00 01 NV-BDIZC LIN CYC  STOPWATCH\n" + line) == (0x37, 98524800)
    assert v.parse_state("garbage") == (None, None)

    clock = v.TapeClock()
    clock.update(False, 0)
    clock.update(True, 1000)        # started in between: half counts
    clock.update(True, 3000)
    clock.update(False, 5000)       # stopped in between: half counts
    clock.update(False, 9000)       # off: nothing
    assert clock.played == 500 + 2000 + 1000

    total = 600 * v.PAL_HZ          # a ten-minute tape
    assert v.stopped_early(120 * v.PAL_HZ, total)       # two minutes in: waiting for a key
    assert not v.stopped_early(590 * v.PAL_HZ, total)   # at the end: loaded
    assert not v.stopped_early(10 * v.PAL_HZ, None)     # length unknown: never a failure
    assert "VICE" in v.STOP_MESSAGE and ".vsf" in v.STOP_MESSAGE
    assert not hasattr(v, "keys_from_env") and not hasattr(v, "DEFAULT_KEYS")


def test_an_agent_splits_and_joins_c64_routines(tmp_path):
    """split_function and merge_function work on the C64 as on the Game Boy: by the bytes, on the assembly track."""
    from decompnet.app.engine import RomNetwork
    from decompnet.network import NetworkError

    # $080D: lda #1 / sta $d020 / jsr $0816 / rts      $0816: lda #2 / rts
    code = bytes([0xA9, 0x01, 0x8D, 0x20, 0xD0, 0x20, 0x16, 0x08, 0x60, 0xA9, 0x02, 0x60])
    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, _basic(0x0801, 0x080D, code)))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    agent, _token = net.local_agent("maxi")
    row = lambda name: net.store.one("SELECT * FROM functions WHERE project_id=? AND name=?", proj["id"], name)
    assert row("func_080d")["size"] == 9 and row("func_0816") is not None
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='documented', matched_source='; sets the border' WHERE project_id=? "
                   "AND name='func_080d'", (proj["id"],))

    with pytest.raises(NetworkError):
        net.split_function(agent, slug, "func_080d", "$080E")      # the middle of lda #1
    said = net.split_function(agent, slug, "func_080d", "$080F")
    assert "split" in said and "func_080f" in said, said
    head, tail = row("func_080d"), row("func_080f")
    assert (head["size"], tail["size"], tail["vram"], tail["rom_offset"]) == (2, 7, 0x080F, head["rom_offset"] + 2)
    assert head["status"] == tail["status"] == "asm" and head["matched_source"] is None
    assert net.asm_track(head) and net.asm_track(tail)
    assert "VIC_BORDERCOLOR" in tail["asm"] and json.loads(tail["relocs"]) == [[0, 1, 0xD020], [3, 10, 0x0816]]
    assert json.loads(tail["meta"])["c64_found"] == "split"

    with pytest.raises(NetworkError):
        net.merge_function(agent, slug, "func_0816")               # called from the routine before it: its own
    said = net.merge_function(agent, slug, "func_080f")           # lda #1 runs on into it
    assert "merged" in said, said
    whole = row("func_080d")
    assert row("func_080f") is None and whole["size"] == 9 and bytes(whole["target"]) == code[:9]
    assert whole["status"] == "asm" and net.asm_track(whole)
    assert net.store.one("SELECT * FROM symbols WHERE project_id=? AND name='func_080f'", proj["id"]) is None


def test_c64_read_data_shows_words_owners_and_uses(tmp_path):
    """read_data on the C64: $-addresses, words low byte first, who holds a byte and who reads or writes it; none
    of the N64 output (8-digit addresses, big-endian words, floats)."""
    from decompnet.app.engine import RomNetwork

    # $080D: jsr $0814 / lda $0818 / rts     $0814: sta $0818 / rts     $0818: a variable, then the word $71C6
    code = bytes([0x20, 0x14, 0x08, 0xAD, 0x18, 0x08, 0x60, 0x8D, 0x18, 0x08, 0x60, 0x00, 0xC6, 0x71])
    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, _basic(0x0801, 0x080D, code)))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    var = net.read_data(slug, "$0818", 4)
    assert "0818: 00 c6 71" in var and "$0818 read by func_080d at $0810" in var, var
    assert "$0818 written by func_0814 at $0814" in var, var
    assert "0x0000" not in var and "f32" not in var.lower(), var
    covered = net.store.one("SELECT name FROM functions WHERE project_id=? AND vram<=? AND vram+size>?",
                            proj["id"], 0x0818, 0x0818)
    assert ("between routines" in var) == (covered is None), var
    assert "$71C6" in net.read_data(slug, "$0819", 4)
    inside = net.read_data(slug, "$0811", 4)
    assert "operand byte of `lda $0818` at $0810 in func_080d" in inside, inside


def test_c64_progress_counts_documented_routines(tmp_path):
    """A C64 project has no C: its progress is routines documented out of those to document, not 0 / 0 functions."""
    from decompnet.app.engine import RomNetwork

    code = bytes([0x20, 0x14, 0x08, 0xAD, 0x18, 0x08, 0x60, 0x8D, 0x18, 0x08, 0x60, 0x00, 0xC6, 0x71])
    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, _basic(0x0801, 0x080D, code)))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    ov = net.overview(slug)
    before = ov["asm_only"]["to_document"]
    assert before >= 2 and ov["asm_only"]["to_document_bytes"] > 0, ov["asm_only"]
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='documented' WHERE project_id=? AND name='func_0814'", (proj["id"],))
    ov = net.overview(slug)
    assert ov["documented"] == 1 and ov["asm_only"]["to_document"] == before - 1
    agent, _token = net.local_agent("maxi")
    net.flag_for_review(agent, slug, "func_080d", "not code: these bytes are a table the loop reads")
    assert net.overview(slug)["asm_only"]["to_document"] == before - 2


def test_c64_header_edits_work_and_name_addresses(tmp_path):
    """edit_context on the C64: no C compiler to run, and a #define with an address becomes a label."""
    from decompnet.app.engine import RomNetwork

    code = bytes([0x20, 0x14, 0x08, 0xAD, 0x18, 0x08, 0x60, 0x8D, 0x18, 0x08, 0x60, 0x00, 0xC6, 0x71])
    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, _basic(0x0801, 0x080D, code)))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    agent, _token = net.local_agent("maxi")
    said = net.edit_context(agent, slug, "", "#define wCounter 0x0818 /* how many letters are left */",
                            "name the counter")
    assert "header is now v2" in said and "wCounter = $0818" in said, said
    assert net.store.one("SELECT * FROM symbols WHERE project_id=? AND name='wCounter'", proj["id"])["address"] == 0x0818
    assert net.read_data(slug, "$0818", 2).startswith("wCounter at $0818")
    net.reset_project_progress(slug)
    assert net.store.one("SELECT * FROM symbols WHERE project_id=? AND name='wCounter'", proj["id"]) is None
    assert net.read_data(slug, "$0818", 2).startswith("$0818")


def test_c64_addresses_work_in_get_function_rename_and_read_data(tmp_path):
    """On the C64 agents read $-addresses: get_function and rename_symbol take them, and read_data decodes code for
    them so nobody works instructions out from hex."""
    import pytest
    from decompnet.app.engine import RomNetwork

    code = bytes([0x20, 0x14, 0x08, 0xAD, 0x18, 0x08, 0x60, 0x8D, 0x18, 0x08, 0x60, 0x00, 0xC6, 0x71])
    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, _basic(0x0801, 0x080D, code)))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    agent, _token = net.local_agent("maxi")
    assert "func_0814" in net.get_function("$0814", slug)
    assert "func_0814" in net.get_function("0x0814", slug)
    with pytest.raises(Exception, match="inside func_080d"):
        net.get_function("$0810", slug)
    shown = net.read_data(slug, "$080D", 7)
    assert "as code (func_080d)" in shown and "  080D  jsr $0814" in shown and "  0810  lda $0818" in shown, shown
    net.rename_symbol(agent, slug, "$0818", "wCounter", "name the counter")
    assert net.read_data(slug, "$0818", 2).startswith("wCounter at $0818")


def test_c64_routines_are_handed_out_bottom_up(tmp_path):
    """On the C64 a routine is handed out once what it calls is documented: leaves first, then up the call chain, and
    the claim lists what it calls with the comment of each documented one."""
    import time
    from decompnet.app.engine import RomNetwork

    # $080D: jsr $0814 / jsr $0818 / rts     $0814: jsr $0818 / rts     $0818: lda #$01 / rts
    code = bytes([0x20, 0x14, 0x08, 0x20, 0x18, 0x08, 0x60, 0x20, 0x18, 0x08, 0x60, 0xA9, 0x01, 0x60])
    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, _basic(0x0801, 0x080D, code)))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    agent, _token = net.local_agent("maxi")
    names = {r["name"] for r in net.store.all("SELECT name FROM functions WHERE project_id=? AND status='asm'",
                                              proj["id"])}
    assert {"func_080d", "func_0814", "func_0818"} <= names, names

    def claim_and_document(expected):
        brief = net.claim_asm(agent, slug)
        assert f"/{expected}" in brief.splitlines()[0], brief.splitlines()[0]
        with net.store.tx() as db:
            db.execute("UPDATE functions SET status='documented', matched_source=? WHERE project_id=? AND name=?",
                       (f"; does the {expected} thing\n{expected}:\n  rts", proj["id"], expected))
            db.execute("DELETE FROM claims")
            db.execute("INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], agent.id, "documented", expected, time.time()))
        return brief

    claim_and_document("func_0818")
    middle = claim_and_document("func_0814")
    assert "func_0818 at $0818 (calls it): documented: does the func_0818 thing" in middle, middle
    top = claim_and_document("func_080d")
    assert "func_0814 at $0814 (calls it): documented" in top and "func_0818 at $0818 (calls it): documented" in top, top


def test_c64_a_routine_that_runs_on_into_the_next_waits_for_it(tmp_path):
    """A C64 routine that runs on into the next one (or branches into it) depends on it like a call: the next one is
    handed out first, and the claim says how they're linked and when to merge_function."""
    import time
    from decompnet.app.engine import RomNetwork
    from decompnet.network import NetworkError

    # $080D: jsr $0814 / jsr $0818 / rts     $0814: cmp #$1e / bcs $081b (runs on)     $0818: lda #$01 / nop / rts
    code = bytes([0x20, 0x14, 0x08, 0x20, 0x18, 0x08, 0x60, 0xC9, 0x1E, 0xB0, 0x03, 0xA9, 0x01, 0xEA, 0x60])
    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, _basic(0x0801, 0x080D, code)))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    agent, _token = net.local_agent("maxi")
    names = {r["name"] for r in net.store.all("SELECT name FROM functions WHERE project_id=? AND status='asm'",
                                              proj["id"])}
    assert {"func_080d", "func_0814", "func_0818"} <= names, names

    def claim_and_document(expected):
        brief = net.claim_asm(agent, slug)
        assert f"/{expected}" in brief.splitlines()[0], brief.splitlines()[0]
        with net.store.tx() as db:
            db.execute("UPDATE functions SET status='documented', matched_source=? WHERE project_id=? AND name=?",
                       (f"; does the {expected} thing\n{expected}:\n  rts", proj["id"], expected))
            db.execute("DELETE FROM claims")
            db.execute("INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], agent.id, "documented", expected, time.time()))
        return brief

    claim_and_document("func_0818")  # smallest-first would have handed out func_0814
    head = claim_and_document("func_0814")
    assert ("func_0818 at $0818 (`bcs $081b` goes into it, 3 bytes in; runs on into it: no rts, rti or jmp at the "
            "end): documented: does the func_0818 thing") in head, head
    assert "It runs on into func_0818" in head and 'merge_function("func_0814")' in head, head
    try:  # func_080d calls func_0818, so it's a routine of its own: the merge the brief hedges on is refused
        net.merge_function(agent, slug, "func_0814")
        raise AssertionError("merged")
    except NetworkError as e:
        assert "func_080d" in str(e), e
    claim_and_document("func_080d")


def test_c64_compile_off_the_track_answers_instead_of_failing(tmp_path, monkeypatch):
    """A documented C64 routine has no C behind it: compiling it again tells the agent so, and a tool that fails
    some other way comes back as an error the agent reads, not one that ends its session."""
    from decompnet.app import tools
    from decompnet.app.engine import RomNetwork
    from decompnet.network import NetworkError

    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, _basic(0x0801, 0x080D, bytes([0xA0, 0x00, 0x4C, 0x00, 0x02]))))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    agent, _token = net.local_agent("maxi")
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND vram=?", proj["id"], 0x080D)
    with net.store.tx() as c:
        c.execute("UPDATE functions SET status='documented' WHERE id=?", (fn["id"],))
    source = "; enters the input buffer\nenter_buf:\n  ldy #$00\n  jmp BUF\n"
    with pytest.raises(NetworkError) as e:
        net.compile_and_diff(agent, fn["name"], source, slug)
    assert "already documented" in str(e.value) and "claim_function" in str(e.value)
    said = tools.call(net, agent, slug, "compile_and_diff", {"function": "$080D", "source": source})
    assert said.startswith("error:") and "already documented" in said, said

    def broken(*_a, **_k):
        raise RuntimeError("disk on fire")
    monkeypatch.setitem(tools.HANDLERS, "project_info", broken)
    assert tools.call(net, agent, slug, "project_info", {}) == "error: project_info failed (RuntimeError: disk on fire)"


def test_c64_documenting_with_a_real_label_names_the_routine(tmp_path):
    """One compile does it: the label is the routine's new name, so no rename_symbol comes first."""
    from decompnet.app.agents import _args_brief
    from decompnet.app.engine import RomNetwork

    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, _basic(0x0801, 0x080D, bytes([0xA9, 0xFF, 0x85, 0x90, 0x60]))))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    agent, _token = net.local_agent("maxi")
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND vram=?", proj["id"], 0x080D)
    auto = f"; sets the KERNAL status byte to $FF\n{fn['name']}:\n  lda #$FF\n  sta STATUS\n  rts\n"
    said = net.compile_and_diff(agent, fn["name"], auto, slug)
    assert "write a real one as the label" in said and "no rename_symbol needed" in said, said
    named = "; sets the KERNAL status byte to $FF\nset_error_flag:\n  lda #$FF\n  sta STATUS\n  rts\n"
    said = net.compile_and_diff(agent, fn["name"], named, slug)
    assert "DOCUMENTED: set_error_flag" in said, said
    row = net.store.one("SELECT name, status FROM functions WHERE id=?", fn["id"])
    assert (row["name"], row["status"]) == ("set_error_flag", "documented")
    assert _args_brief({"function": "x", "source": named}) == "x, 5 lines of assembly"
    assert _args_brief({"source": "void f(void) {\n}\n"}) == "2 lines of C"


def test_c64_agents_create_and_delete_routines(tmp_path):
    """C64 agents add a routine the finder missed (create_function) and remove one that isn't code (delete_function,
    which keeps its bytes as data with the reason); both refuse what isn't theirs to change."""
    from decompnet.app.engine import RomNetwork
    from decompnet.network import NetworkError

    # $080D: jsr $0822 / lda $081D / sta $FB / lda $081E / sta $FC / jmp ($00FB)    $081D: .word $081F (a jump table)
    # $081F: lda #$05 / rts (reached only through the table)    $0822: lda #$01 / rts    $0825: bytes that aren't code
    code = bytes([0x20, 0x22, 0x08, 0xAD, 0x1D, 0x08, 0x85, 0xFB, 0xAD, 0x1E, 0x08, 0x85, 0xFC, 0x6C, 0xFB, 0x00,
                  0x1F, 0x08, 0xA9, 0x05, 0x60, 0xA9, 0x01, 0x60, 0x02, 0x02, 0x02, 0x00])
    path = tmp_path / "demo.prg"
    path.write_bytes(_prg(0x0801, _basic(0x0801, 0x080D, code)))
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    agent, _token = net.local_agent("maxi")
    other, _token = net.local_agent("other")
    why = "reached only through the jump table at $081D, entry 0"

    def starts():
        return {r["vram"]: r["name"] for r in net.store.all("SELECT name, vram FROM functions WHERE project_id=?",
                                                            proj["id"])}

    def refused(call, *words):
        with pytest.raises(NetworkError) as e:
            call()
        assert all(w in str(e.value) for w in words), str(e.value)

    if 0x081F in starts():  # (a generous finder may have found it: then it's the one deleted and brought back)
        net.delete_function(agent, slug, starts()[0x081F], "test setup: start from the routine being missed")
    assert 0x081F not in starts() and {0x080D, 0x0822} <= set(starts()), starts()

    text = net.create_function(agent, slug, "$081F", why)
    assert "created: func_081f at $081F, 3 bytes; it ends with `rts` at $0821" in text, text
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND vram=?", proj["id"], 0x081F)
    assert fn["status"] == "asm" and net.asm_track(fn) and bytes(fn["target"]) == bytes([0xA9, 0x05, 0x60])
    assert "func_081f at $081F" in net.store.one("SELECT detail FROM events WHERE kind='create'")["detail"]

    refused(lambda: net.create_function(agent, slug, "$0813", why), "inside func_080d", "split_function")
    refused(lambda: net.create_function(agent, slug, "$0822", why), "func_0822 already starts")
    refused(lambda: net.create_function(agent, slug, "$0825", why), "doesn't decode as code")
    refused(lambda: net.create_function(agent, slug, "$0801", why), "BASIC")
    refused(lambda: net.create_function(agent, slug, "$0825", "data"), "say why")

    text = net.delete_function(agent, slug, "func_081f", "jump table entry data, not code, for the test's sake")
    assert "deleted: func_081f is gone and its 3 bytes at $081F are recorded as data" in text, text
    assert 0x081F not in starts()
    shown = net.read_data(slug, "$081F", 8)
    assert "recorded as data from $081F, 3 bytes: not code (deleted func_081f" in shown, shown
    assert "brings it back" in text and 0x081F not in starts()
    net.create_function(agent, slug, "$081F", why)  # brought back: the data record goes
    assert 0x081F in starts() and "recorded as data" not in net.read_data(slug, "$081F", 8)

    with net.store.tx() as db:  # func_080d documented by another agent, as code that calls func_0822
        db.execute("UPDATE functions SET status='documented', matched_agent_id=?, matched_source='; x' WHERE "
                   "project_id=? AND vram=?", (other.id, proj["id"], 0x080D))
    refused(lambda: net.delete_function(agent, slug, "func_0822", "not code at all, just a test of refusing"),
            "func_080d documented as code that reaches func_0822", "flag_for_review")
    refused(lambda: net.delete_function(agent, slug, "func_080d", "not code at all, just a test of refusing"),
            "documented as code by other")


def test_asm_prompt_keeps_api_helpers_going():
    """On the assembly track an API helper (whose context the app trims) keeps claiming until nothing is left, like on
    the C track; a CLI helper still stops after its per-session count. The C64 prompt names delete/create_function."""
    from types import SimpleNamespace

    from decompnet.app import tools
    from decompnet.app.agents import Manager

    def prompt(kind, platform):
        s = SimpleNamespace(kind=kind, options={"per_session": 2})
        return tools.ASM_PROMPT.format(title="Ping_Pong", repeat=Manager._asm_repeat(s),
                                       **tools.asm_words(platform))

    api = prompt("api", "c64")
    assert "until claim_function says nothing is left" in api and "up to 2" not in api, api
    assert "delete_function" in api and "create_function" in api and "correct_split" not in api, api
    cli = prompt("claude", "c64")
    assert "Repeat for up to 2 routines, then stop" in cli, cli
    assert "correct_split or flag_for_review" in prompt("api", "gb")
