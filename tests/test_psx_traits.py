"""PS1 briefs expose measured instruction facts and explicitly partial signature evidence."""
import json
import struct

from decompnet.rom.psx_traits import LIMIT, analyze, brief
from .test_psyq import net_with_overlays


def code(*words):
    return struct.pack('<' + 'I' * len(words), *words)


def test_prologue_frame_register_saves_stack_arguments_and_calls():
    target = code(0x27BDFFE0, 0xAFBF001C, 0xAFB00018, 0x00808021, 0x8FA20030,
                  0x0C004000, 0x00A02021, 0x8FBF001C, 0x03E00008, 0x27BD0020)
    evidence = analyze(target)
    assert evidence['frame_bytes'] == 32
    assert evidence['saved_registers'] == {'ra': -4, 's0': -8}
    assert evidence['argument_registers'] == ['a0', 'a1']  # a1 read in call delay slot
    assert evidence['potential_stack_slots'] == [16]
    assert evidence['direct_calls'] == 1 and evidence['leaf'] is False
    assert evidence['entry_instructions'] == 7
    assert 'later arguments may be missing' in '\n'.join(brief(evidence))


def test_indirect_calls_are_distinct_from_unproven_table_jumps():
    evidence = analyze(code(0x0320F809, 0, 0x00400008, 0), ['jtbl_80100000'])
    assert evidence['indirect_calls'] == 1 and evidence['indirect_jumps'] == 1
    assert 'not proven' in evidence['table_method']
    assert analyze(code(0x00400008, 0))['jump_tables'] == []
    assert 'do not establish' in analyze(code(0x00400008, 0))['table_method']


def test_constant_resolved_io_and_scratchpad_accesses():
    evidence = analyze(code(0x3C081F80, 0xAD041810, 0x8D020000, 0x03E00008, 0))
    assert [(r['address'], r['width'], r['kind']) for r in evidence['resolved_memory']] == [
        (0x1F801810, 4, 'I/O range'), (0x1F800000, 4, 'scratchpad')]
    assert evidence['leaf'] is True
    assert '0x1F801810' in '\n'.join(brief(evidence))


def test_loaded_or_unknown_address_is_not_treated_as_a_constant():
    evidence = analyze(code(0x3C081F80, 0x8C880000, 0xAD041810, 0x03E00008, 0))
    assert evidence['resolved_memory'] == []
    assert analyze(code(0xAD041810, 0x03E00008, 0))['resolved_memory'] == []


def test_segment_aliases_and_unmapped_scratchpad_mirror():
    assert analyze(code(0x3C085F80, 0xAD041810, 0x03E00008, 0))['resolved_memory'] == []
    assert analyze(code(0x3C08BF80, 0xAD040000, 0x03E00008, 0))['resolved_memory'] == []
    assert analyze(code(0x3C08BF80, 0xAD041810, 0x03E00008, 0))['resolved_memory'][0]['kind'] == 'I/O range'
    assert analyze(code(0x3C081F80, 0xAD043800, 0x03E00008, 0))['resolved_memory'][0]['kind'] == 'expansion I/O range'


def test_signature_evidence_stops_after_first_branch_and_tracks_writes():
    evidence = analyze(code(0x24040001, 0x10800002, 0x00A01021, 0x00C01021, 0x03E00008, 0))
    assert evidence['argument_registers'] == ['a1']  # a0 overwritten; a2 only in later block
    assert evidence['entry_instructions'] == 3


def test_unknown_or_dynamic_stack_adjustment_is_unresolved():
    assert analyze(code(0x03A4E823, 0x03E00008, 0))['frame_bytes'] is None
    assert analyze(code(0xFFFFFFFF))['frame_bytes'] is None


def test_saved_register_must_still_contain_its_entry_value():
    evidence = analyze(code(0x24100001, 0xAFB00000, 0x03E00008, 0))
    assert evidence['saved_registers'] == {}


def test_analysis_budgets_and_gte_count():
    evidence = analyze(code(0x4A180001, 0x03E00008, 0))
    assert evidence['gte_instructions'] == 1
    evidence = analyze(bytes((LIMIT + 1) * 4))
    assert evidence['limited'] and evidence['leaf'] is None
    assert evidence['entry_instructions'] == 256
    assert 'partial evidence' in '\n'.join(brief(evidence))


def test_real_function_brief_retains_unit_recipe_and_table_method(tmp_path):
    net = net_with_overlays(tmp_path)
    target = code(0x3C081F80, 0xAD041810, 0x03E00008, 0)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET seed='',asm='',target=?,size=16,n_insns=4,relocs=? WHERE unit='main'",
                   (target, json.dumps([[0, 5, 0x80010010]])))
        db.execute("INSERT INTO symbols VALUES(1,'jtbl_80010010',2147549200,16,'jtbl',NULL)")
    text = net.get_function('func_80010000', 'synthetic')
    assert 'PS1 instruction evidence' in text and '0x1F801810' in text
    assert 'compiler gcc-2.7.2-psx' in text and '-G0' in text
    assert 'named symbol/address reference' in text
    assert 'check the access mode' in text
