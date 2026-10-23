"""C64 addresses by name, for listings and for the context a decomp agent reads.

ROM routines and ROM data tables come from ``c64_rommap`` (sk6502 / Richard Dymond; the annotations
trace back to Lee Davison's 2012 disassembly — see that module and NOTICE). VIC-II, SID, the CIAs,
colour RAM, the processor port and the usual zero-page and page-2/3 locations are the standard map,
named the way cc65's c64.inc and cbm_kernal.inc name the same registers, with a one-line description
of each. Those descriptions are not copied from sk6502.

Banking assumed everywhere a label has to choose: ``$01 = $37``, so BASIC is at ``$A000``, I/O at
``$D000`` and the KERNAL at ``$E000``. The same labels are how ``name_listing`` rewrites operands.
``#$01`` is an immediate and is left alone.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .c64_rommap import ENTRIES

# Classic KERNAL jump-table names. The sk6502 title stays the description; the short name is what
# listings and agents already use.
ALIASES = {
    0xFF81: "CINT", 0xFF84: "IOINIT", 0xFF87: "RAMTAS", 0xFF8A: "RESTOR", 0xFF8D: "VECTOR",
    0xFF90: "SETMSG", 0xFF93: "SECOND", 0xFF96: "TKSA", 0xFF99: "MEMTOP", 0xFF9C: "MEMBOT",
    0xFF9F: "SCNKEY", 0xFFA2: "SETTMO", 0xFFA5: "ACPTR", 0xFFA8: "CIOUT", 0xFFAB: "UNTLK",
    0xFFAE: "UNLSN", 0xFFB1: "LISTEN", 0xFFB4: "TALK", 0xFFB7: "READST", 0xFFBA: "SETLFS",
    0xFFBD: "SETNAM", 0xFFC0: "OPEN", 0xFFC3: "CLOSE", 0xFFC6: "CHKIN", 0xFFC9: "CHKOUT",
    0xFFCC: "CLRCHN", 0xFFCF: "CHRIN", 0xFFD2: "CHROUT", 0xFFD5: "LOAD", 0xFFD8: "SAVE",
    0xFFDB: "SETTIM", 0xFFDE: "RDTIM", 0xFFE1: "STOP", 0xFFE4: "GETIN", 0xFFE7: "CLALL",
    0xFFEA: "UDTIM", 0xFFED: "SCREEN", 0xFFF0: "PLOT", 0xFFF3: "IOBASE",
    0xE544: "CLRSCR", 0xE5B4: "KBDREAD", 0xEA31: "IRQ_DEFAULT", 0xEA81: "IRQ_EXIT",
    0xFCE2: "RESET", 0xFF48: "IRQ_ENTRY", 0xFEBC: "NMIEXIT",
}

# (address, name, description). One line each; this is what context.h carries.
_VIC = (
    (0xD000, "VIC_SPR0_X", "sprite 0 X, low 8 bits"),
    (0xD001, "VIC_SPR0_Y", "sprite 0 Y"),
    (0xD002, "VIC_SPR1_X", "sprite 1 X, low 8 bits"),
    (0xD003, "VIC_SPR1_Y", "sprite 1 Y"),
    (0xD004, "VIC_SPR2_X", "sprite 2 X, low 8 bits"),
    (0xD005, "VIC_SPR2_Y", "sprite 2 Y"),
    (0xD006, "VIC_SPR3_X", "sprite 3 X, low 8 bits"),
    (0xD007, "VIC_SPR3_Y", "sprite 3 Y"),
    (0xD008, "VIC_SPR4_X", "sprite 4 X, low 8 bits"),
    (0xD009, "VIC_SPR4_Y", "sprite 4 Y"),
    (0xD00A, "VIC_SPR5_X", "sprite 5 X, low 8 bits"),
    (0xD00B, "VIC_SPR5_Y", "sprite 5 Y"),
    (0xD00C, "VIC_SPR6_X", "sprite 6 X, low 8 bits"),
    (0xD00D, "VIC_SPR6_Y", "sprite 6 Y"),
    (0xD00E, "VIC_SPR7_X", "sprite 7 X, low 8 bits"),
    (0xD00F, "VIC_SPR7_Y", "sprite 7 Y"),
    (0xD010, "VIC_SPR_HI_X", "sprite X bit 8, one bit per sprite"),
    (0xD011, "VIC_CTRL1", "control: raster bit 8, display enable, bitmap, rows, Y scroll"),
    (0xD012, "VIC_HLINE", "raster line, low 8 bits (bit 8 is VIC_CTRL1)"),
    (0xD013, "VIC_LPEN_X", "light pen X, latched"),
    (0xD014, "VIC_LPEN_Y", "light pen Y, latched"),
    (0xD015, "VIC_SPR_ENA", "sprite enable, one bit per sprite"),
    (0xD016, "VIC_CTRL2", "control: multicolour, 40 columns, X scroll"),
    (0xD017, "VIC_SPR_EXP_Y", "sprite expand in Y, one bit per sprite"),
    (0xD018, "VIC_VIDEO_ADR", "screen and character-generator base"),
    (0xD019, "VIC_IRR", "interrupt status (raster, sprite collision, light pen); write 1s to clear"),
    (0xD01A, "VIC_IMR", "interrupt enable, same bit order as VIC_IRR"),
    (0xD01B, "VIC_SPR_BG_PRIO", "sprite-to-background priority, one bit per sprite"),
    (0xD01C, "VIC_SPR_MCOLOR", "sprite multicolour enable, one bit per sprite"),
    (0xD01D, "VIC_SPR_EXP_X", "sprite expand in X, one bit per sprite"),
    (0xD01E, "VIC_SPR_COLL", "sprite-sprite collision, latched"),
    (0xD01F, "VIC_SPR_BG_COLL", "sprite-background collision, latched"),
    (0xD020, "VIC_BORDERCOLOR", "border colour"),
    (0xD021, "VIC_BG_COLOR0", "background colour 0"),
    (0xD022, "VIC_BG_COLOR1", "background colour 1 (multicolour / extended)"),
    (0xD023, "VIC_BG_COLOR2", "background colour 2"),
    (0xD024, "VIC_BG_COLOR3", "background colour 3"),
    (0xD025, "VIC_SPR_MCOLOR0", "sprite multicolour 0"),
    (0xD026, "VIC_SPR_MCOLOR1", "sprite multicolour 1"),
    (0xD027, "VIC_SPR0_COLOR", "sprite 0 colour"),
    (0xD028, "VIC_SPR1_COLOR", "sprite 1 colour"),
    (0xD029, "VIC_SPR2_COLOR", "sprite 2 colour"),
    (0xD02A, "VIC_SPR3_COLOR", "sprite 3 colour"),
    (0xD02B, "VIC_SPR4_COLOR", "sprite 4 colour"),
    (0xD02C, "VIC_SPR5_COLOR", "sprite 5 colour"),
    (0xD02D, "VIC_SPR6_COLOR", "sprite 6 colour"),
    (0xD02E, "VIC_SPR7_COLOR", "sprite 7 colour"),
)
_SID_VOICE = (
    ("Lo", "frequency low"), ("Hi", "frequency high"),
    ("PBLo", "pulse width low"), ("PBHi", "pulse width high (low nibble)"),
    ("Ctl", "control: gate, sync, ring, test, triangle, saw, pulse, noise"),
    ("AD", "attack (high nibble) and decay (low nibble)"),
    ("SUR", "sustain (high nibble) and release (low nibble)"),
)
_SID = tuple(
    (0xD400 + voice * 7 + n, f"SID_S{voice + 1}{suffix}", f"voice {voice + 1} {desc}")
    for voice in range(3) for n, (suffix, desc) in enumerate(_SID_VOICE)
) + (
    (0xD415, "SID_FltLo", "filter cutoff, low 3 bits"),
    (0xD416, "SID_FltHi", "filter cutoff, high 8 bits"),
    (0xD417, "SID_FltCtl", "filter resonance and which voices are filtered"),
    (0xD418, "SID_Amp", "volume (low nibble) and filter mode (mute voice 3, high pass, band pass, low pass)"),
    (0xD419, "SID_ADConv1", "paddle X / pot X"),
    (0xD41A, "SID_ADConv2", "paddle Y / pot Y"),
    (0xD41B, "SID_Noise", "voice 3 oscillator output"),
    (0xD41C, "SID_Read3", "voice 3 envelope output"),
)


def _cia(base: int, prefix: str, which: str) -> tuple:
    rows = (
        (0, "PRA", f"{which} port A data (CIA1: keyboard columns / joystick 2; CIA2: VIC bank and serial bus)"),
        (1, "PRB", f"{which} port B data (CIA1: keyboard rows / joystick 1; CIA2: user port / RS-232)"),
        (2, "DDRA", f"{which} port A direction, 1 = output"),
        (3, "DDRB", f"{which} port B direction, 1 = output"),
        (4, "TA_LO", f"{which} timer A low"),
        (5, "TA_HI", f"{which} timer A high (writing starts the reload)"),
        (6, "TB_LO", f"{which} timer B low"),
        (7, "TB_HI", f"{which} timer B high"),
        (8, "TOD_10", f"{which} time of day, tenths"),
        (9, "TOD_SEC", f"{which} time of day, seconds (BCD)"),
        (10, "TOD_MIN", f"{which} time of day, minutes (BCD)"),
        (11, "TOD_HR", f"{which} time of day, hours (BCD, bit 7 is PM)"),
        (12, "SDR", f"{which} serial data"),
        (13, "ICR", f"{which} interrupt control: read flags, write enables (bit 7 set = enable the bits written)"),
        (14, "CRA", f"{which} timer A control: start, PB on, toggle, one-shot, load, input mode"),
        (15, "CRB", f"{which} timer B control, same bits as CRA plus the timer-A cascade"),
    )
    return tuple((base + off, f"{prefix}_{name}", desc) for off, name, desc in rows)


_PORT = (
    (0x0000, "CPU_DDR", "6510 processor-port direction. Bit 0 LORAM, bit 1 HIRAM, bit 2 CHAREN; a 0 bit reads as a pull-up"),
    (0x0001, "CPU_PORT", "6510 processor port. Low 3 bits bank BASIC ($A000), KERNAL ($E000) and I/O vs CHAR ROM ($D000). $37 is the usual BASIC+I/O+KERNAL map"),
)
_VECTORS = (
    (0x0314, "CINV", "IRQ RAM vector, normally points at the KERNAL default IRQ"),
    (0x0316, "CBINV", "BRK RAM vector"),
    (0x0318, "NMINV", "NMI RAM vector"),
    (0xFFFA, "NMI_VEC", "hardware NMI vector (KERNAL ROM while HIRAM is set)"),
    (0xFFFC, "RESET_VEC", "hardware reset vector"),
    (0xFFFE, "IRQ_VEC", "hardware IRQ/BRK vector"),
)
# Zero page and the system pages the disassembly keeps naming. Descriptions are the role of the
# location, short enough to sit in a comment.
_ZP = (
    (0x0007, "CHARAC", "BASIC search character"),
    (0x0008, "ENDCHR", "BASIC scan-quotes / end character flag"),
    (0x0009, "TRMPOS", "BASIC screen column the last TAB stopped at"),
    (0x000A, "VERCK", "BASIC load/verify flag, 0 = load"),
    (0x000B, "COUNT", "BASIC temporary input counter"),
    (0x000C, "DIMFLG", "BASIC DIM in progress"),
    (0x000D, "VALTYP", "BASIC value type, $FF = string, $00 = numeric"),
    (0x000E, "INTFLG", "BASIC integer type flag"),
    (0x000F, "GARBFL", "BASIC garbage-collection flag"),
    (0x0010, "SUBFLG", "BASIC subscript / FN flag"),
    (0x0011, "INPFLG", "BASIC input source, $00 = INPUT, $40 = GET, $98 = READ"),
    (0x0013, "CHANNL", "BASIC current I/O channel"),
    (0x0014, "LINNUM", "BASIC current line number (16 bit)"),
    (0x0016, "TEMPPT", "BASIC pointer to the next temporary-string descriptor"),
    (0x0019, "TEMPST", "BASIC temporary-string descriptor stack"),
    (0x0022, "INDEX", "BASIC miscellaneous pointer"),
    (0x0026, "RESHO", "BASIC series-evaluation result"),
    (0x002B, "TXTTAB", "pointer to the start of BASIC text"),
    (0x002D, "VARTAB", "pointer to the start of BASIC variables"),
    (0x002F, "ARYTAB", "pointer to the start of BASIC arrays"),
    (0x0031, "STREND", "pointer to the end of BASIC arrays"),
    (0x0033, "FRETOP", "pointer to the bottom of the BASIC string space"),
    (0x0037, "MEMSIZ", "pointer to the top of BASIC memory"),
    (0x0039, "CURLIN", "BASIC line number currently executing"),
    (0x003B, "OLDLIN", "BASIC previous line number"),
    (0x003D, "OLDTXT", "BASIC pointer to the current statement"),
    (0x003F, "DATLIN", "BASIC line number of the current DATA item"),
    (0x0041, "DATPTR", "BASIC pointer to the current DATA item"),
    (0x0043, "INPPTR", "BASIC pointer into the input buffer"),
    (0x0045, "VARNAM", "BASIC current variable name"),
    (0x0047, "VARPNT", "BASIC pointer to the current variable's value"),
    (0x0049, "FORPNT", "BASIC pointer to the variable of the current FOR"),
    (0x0061, "FAC", "BASIC floating-point accumulator 1"),
    (0x0069, "ARG", "BASIC floating-point accumulator 2"),
    (0x0073, "CHRGET", "BASIC CHRGET subroutine (self-modifying)"),
    (0x007A, "TXTPTR", "BASIC pointer to the byte CHRGET will read next"),
    (0x008B, "RNDX", "BASIC RND seed"),
    (0x0090, "STATUS", "KERNAL I/O status byte (ST)"),
    (0x0091, "STKEY", "stop key flag, $7F if STOP is down during a scan"),
    (0x0093, "VERCK_K", "KERNAL load/verify flag"),
    (0x0098, "LDTND", "number of open files"),
    (0x0099, "DFLTN", "default input device"),
    (0x009A, "DFLTO", "default output device"),
    (0x009D, "MSGFLG", "KERNAL message flag (SETMSG)"),
    (0x00A0, "TIME", "jiffy clock, three bytes, tenths of a second"),
    (0x00A6, "BUFPNT", "tape buffer pointer"),
    (0x00AC, "SAL", "start address for a save / load, low then high"),
    (0x00AE, "EAL", "end address for a save, or the last byte a loader is reading"),
    (0x00B2, "TAPE1", "pointer to the tape buffer"),
    (0x00B7, "FNLEN", "length of the current filename"),
    (0x00B8, "LA", "current logical file number"),
    (0x00B9, "SA", "current secondary address"),
    (0x00BA, "FA", "current device number"),
    (0x00BB, "FNADR", "pointer to the current filename"),
    (0x00C1, "STAL", "start address for a LOAD"),
    (0x00C3, "MEMUSS", "KERNAL temporary pointer (LOAD uses it)"),
    (0x00C5, "LSTX", "matrix coordinate of the last key, $40 = none"),
    (0x00C6, "NDX", "number of characters in the keyboard buffer"),
    (0x00C7, "RVS", "reverse-text flag, $12 on"),
    (0x00CB, "SFDX", "matrix coordinate of the key being held"),
    (0x00CC, "BLNSW", "cursor blink disable"),
    (0x00CD, "BLNCT", "cursor blink countdown"),
    (0x00CE, "GDBLN", "character under the cursor"),
    (0x00CF, "BLNON", "cursor phase, 0 = off"),
    (0x00D0, "CRSW", "CR pending flag"),
    (0x00D1, "PNT", "pointer to the current screen line"),
    (0x00D3, "PNTR", "cursor column on this line"),
    (0x00D4, "QTSW", "quote mode"),
    (0x00D5, "LNMX", "maximum column on this line"),
    (0x00D6, "TBLX", "cursor row"),
    (0x00D8, "INSRT", "insert-mode count"),
    (0x00F3, "COLOR", "pointer to the colour RAM for the current line"),
    (0x00F5, "KEYTAB", "pointer to the keyboard decode table"),
    (0x00F7, "RIBUF", "RS-232 input buffer pointer"),
    (0x00F9, "ROBUF", "RS-232 output buffer pointer"),
    (0x00FB, "FREKZP", "free zero page, four bytes programs commonly borrow"),
)
_PAGE = (
    (0x0200, "BUF", "BASIC input buffer, 89 bytes"),
    (0x0259, "LAT", "KERNAL logical-file table"),
    (0x0263, "FAT", "KERNAL device-number table"),
    (0x026D, "SAT", "KERNAL secondary-address table"),
    (0x0277, "KEYD", "keyboard buffer, 10 bytes"),
    (0x0281, "MEMSTR", "start of memory pointer the KERNAL reported"),
    (0x0283, "MEMSIZ_2", "top of memory pointer the KERNAL reported"),
    (0x0286, "COLOR_PEN", "current cursor colour"),
    (0x0288, "HIBASE", "page of the screen matrix, $04 for the default screen at $0400"),
    (0x028A, "RPTFLG", "key repeat flags"),
    (0x028D, "SHFLAG", "shift / ctrl / C= held"),
    (0x02A6, "PALNTS", "PAL/NTSC flag, 0 = NTSC, 1 = PAL"),
    (0x0300, "IERROR", "BASIC error-message vector"),
    (0x0302, "IMAIN", "BASIC warm-start vector"),
    (0x0304, "ICRNCH", "BASIC tokenise vector"),
    (0x0306, "IQPLOP", "BASIC token-list vector"),
    (0x0308, "IGONE", "BASIC execute-statement vector"),
    (0x030A, "IEVAL", "BASIC evaluate-expression vector"),
    (0x031A, "IOPEN", "KERNAL indirect OPEN"),
    (0x031C, "ICLOSE", "KERNAL indirect CLOSE"),
    (0x031E, "ICHKIN", "KERNAL indirect CHKIN"),
    (0x0320, "ICKOUT", "KERNAL indirect CHKOUT"),
    (0x0322, "ICLRCH", "KERNAL indirect CLRCHN"),
    (0x0324, "IBASIN", "KERNAL indirect CHRIN"),
    (0x0326, "IBSOUT", "KERNAL indirect CHROUT"),
    (0x0328, "ISTOP", "KERNAL indirect STOP"),
    (0x032A, "IGETIN", "KERNAL indirect GETIN"),
    (0x032C, "ICLALL", "KERNAL indirect CLALL"),
    (0x032E, "USRCMD", "USR function vector"),
    (0x0330, "ILOAD", "KERNAL indirect LOAD"),
    (0x0332, "ISAVE", "KERNAL indirect SAVE"),
    (0x033C, "TBUFFR", "tape buffer, 192 bytes"),
    (0x0400, "SCREEN_RAM", "default screen matrix, 1000 bytes at $0400-$07E7 when HIBASE is $04"),
    (0x07F8, "SPRITE_PTR0", "sprite 0 frame pointer (the last 8 bytes of the default screen page)"),
    (0x07F9, "SPRITE_PTR1", "sprite 1 frame pointer"),
    (0x07FA, "SPRITE_PTR2", "sprite 2 frame pointer"),
    (0x07FB, "SPRITE_PTR3", "sprite 3 frame pointer"),
    (0x07FC, "SPRITE_PTR4", "sprite 4 frame pointer"),
    (0x07FD, "SPRITE_PTR5", "sprite 5 frame pointer"),
    (0x07FE, "SPRITE_PTR6", "sprite 6 frame pointer"),
    (0x07FF, "SPRITE_PTR7", "sprite 7 frame pointer"),
    (0xD800, "COLOR_RAM", "colour RAM, 1000 nybbles at $D800-$DBE7, only the low nibble stores"),
)

_ROWS = _PORT + _ZP + _PAGE + _VECTORS + _VIC + _SID + _cia(0xDC00, "CIA1", "CIA1") + _cia(0xDD00, "CIA2", "CIA2")

_IMM = re.compile(r"#\$[0-9A-Fa-f]+")
_ADDR = re.compile(r"(?<![#\w$])\$([0-9A-Fa-f]{2}|[0-9A-Fa-f]{4})\b")


def _clean(text: str) -> str:
    return text.replace("*/", " ").replace("/*", " ")


@lru_cache(maxsize=1)
def rom_by_addr() -> dict[int, tuple[str, str, str]]:
    """address -> (symbol, title, description). The fullest description at that address wins."""
    out: dict[int, tuple[str, str, str]] = {}
    for addr, _kind, sym, title, desc in ENTRIES:
        prev = out.get(addr)
        if prev is None or len(desc) > len(prev[2]):
            out[addr] = (sym, title, desc)
    return out


@lru_cache(maxsize=1)
def _desc() -> dict[int, str]:
    text = {addr: desc for addr, _name, desc in _ROWS}
    for addr, (_sym, _title, desc) in rom_by_addr().items():
        text.setdefault(addr, desc)
    return text


@lru_cache(maxsize=1)
def _hardware() -> dict[int, str]:
    """Addresses named whether the operand is a call or a load/store. No ROM routines."""
    return {addr: name for addr, name, _desc in _ROWS}


def name_of(addr: int, call: bool = False) -> str | None:
    """The name of ``addr``.

    VIC/SID/CIA (and their mirrors), colour RAM, the screen, the processor port and the usual
    zero-page and system locations are always named. A BASIC or KERNAL routine is named on a
    ``jsr`` / ``jmp`` (``call=True``). The same address used as data stays a number: under
    ``$01 = $37`` a store there hits the RAM under the ROM, not the routine.
    """
    addr &= 0xFFFF
    hw = _hardware()
    if addr in hw:
        return hw[addr]
    if 0x0400 <= addr <= 0x07E7:
        off = addr - 0x0400
        return "SCREEN_RAM" if off == 0 else f"SCREEN_RAM+${off:X}"
    if 0xD800 <= addr <= 0xDBFF:
        off = addr - 0xD800
        return "COLOR_RAM" if off == 0 else f"COLOR_RAM+${off:X}"
    if 0xD000 <= addr <= 0xD3FF:
        return hw.get(0xD000 + (addr & 0x3F))
    if 0xD400 <= addr <= 0xD7FF:
        return hw.get(0xD400 + (addr & 0x1F))
    if 0xDC00 <= addr <= 0xDCFF:
        return hw.get(0xDC00 + (addr & 0x0F))
    if 0xDD00 <= addr <= 0xDDFF:
        return hw.get(0xDD00 + (addr & 0x0F))
    if call:
        if addr in ALIASES:
            return ALIASES[addr]
        rom = rom_by_addr().get(addr)
        if rom is not None:
            return rom[0]
    return None


def describe(addr: int) -> str | None:
    """The one-line description for an exact address (not a mirror or a screen offset)."""
    addr &= 0xFFFF
    if addr in _desc():
        return _desc()[addr]
    rom = rom_by_addr().get(addr)
    return rom[2] if rom else None


def name_listing(text: str) -> str:
    """Rewrite ``$`` operands to names. Immediates (``#$01``) stay numbers. Already-named text is unchanged."""

    out = []
    for line in text.splitlines():
        code, sep, comment = line.partition(";")
        call = bool(re.search(r"\b(jsr|jmp)\b", code))

        def repl(m: re.Match, call: bool = call) -> str:
            name = name_of(int(m.group(1), 16), call=call)
            return name if name else m.group(0)

        saved: list[str] = []

        def hide(m: re.Match) -> str:
            saved.append(m.group(0))
            return f"\x00{len(saved) - 1}\x00"

        code = _IMM.sub(hide, code)
        code = _ADDR.sub(repl, code)
        for i, raw in enumerate(saved):
            code = code.replace(f"\x00{i}\x00", raw)
        out.append(code + sep + comment)
    return "\n".join(out)


def context_header() -> str:
    """``#define`` lines for the map, each with its description, for context.h."""
    lines = [
        "/* Commodore 64 memory map. Assumed banking: $01 = $37, so BASIC ROM is at",
        " * $A000-$BFFF, I/O at $D000-$DFFF and KERNAL at $E000-$FFFF. Clearing those",
        " * bits of CPU_PORT banks RAM over the three windows. VIC-II registers repeat",
        " * every $40 from $D000-$D3FF, SID every $20 from $D400-$D7FF, CIA1 every $10",
        " * through $DC00-$DCFF and CIA2 through $DD00-$DDFF.",
        " *",
        " * BASIC and KERNAL routine names and their descriptions are from sk6502",
        " * (Richard Dymond, 2019, ISC). sk6502's c64rom.ctl derives those annotations",
        " * from Lee Davison's 2012 C64 ROM disassembly. See NOTICE and",
        " * licenses/sk6502-ISC.txt. I/O, colour RAM and zero-page names are the",
        " * standard map (the same identifiers as cc65's c64.inc / cbm_kernal.inc).",
        " */",
    ]
    seen_name: set[str] = set()
    seen_addr: set[int] = set()
    for addr, name, desc in _ROWS:
        if name in seen_name or addr in seen_addr:
            continue
        seen_name.add(name)
        seen_addr.add(addr)
        lines.append(f"#define {name} 0x{addr:04X} /* {_clean(desc)} */")
    for addr, (_sym, _title, desc) in sorted(rom_by_addr().items()):
        name = ALIASES.get(addr, _sym)
        if name in seen_name or addr in seen_addr:
            continue
        seen_name.add(name)
        seen_addr.add(addr)
        lines.append(f"#define {name} 0x{addr:04X} /* {_clean(desc)} */")
    return "\n".join(lines) + "\n"


def symbols() -> list[tuple[str, int, str, str]]:
    """``(name, address, kind, description)`` for the project symbol table."""
    rows: list[tuple[str, int, str, str]] = []
    seen: set[str] = set()
    for addr, name, desc in _ROWS:
        if name in seen:
            continue
        seen.add(name)
        rows.append((name, addr, "data", desc))
    code = {addr for addr, kind, *_ in ((e[0], e[1]) for e in ENTRIES) if kind in ("b", "c")}
    for addr, (sym, _title, desc) in sorted(rom_by_addr().items()):
        name = ALIASES.get(addr, sym)
        if name in seen:
            continue
        seen.add(name)
        rows.append((name, addr, "func" if addr in code else "data", desc))
    return rows


def hardware_names() -> dict[str, int]:
    """name -> address, for the assembly judge's hardware check. Mirrors are not separate names."""
    out = {name: addr for addr, name, _desc in _ROWS}
    out.update({name: addr for addr, name in ALIASES.items()})
    for addr, (sym, _t, _d) in rom_by_addr().items():
        out.setdefault(ALIASES.get(addr, sym), addr)
    return out
