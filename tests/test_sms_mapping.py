"""Authored bus facts: no commercial input or emulator-dependent reset assumptions."""
from dataclasses import FrozenInstanceError, replace

import pytest

from decompnet.rom import banked, sms, sms_trace
from decompnet.rom.sms_mapping import BusState, Mapper, bank_mask, entry_state, wram_address

SIZE = 0x80000


def test_unknown_bus_never_becomes_cartridge_rom():
    model = Mapper("sega", SIZE)
    for cpu in (0, 0x123, 0x4000, 0x8000, 0xC000, 0xFFFF):
        address = model.resolve(cpu, BusState())
        assert address.space == "unknown" and address.rom_offset is None and address.reason


def test_fixed_prefix_and_independent_pageable_windows():
    model = Mapper("sega", SIZE)
    state = BusState.cartridge_only((5, 7, 9))
    assert model.resolve(0x3FF, state).rom_offset == 0x3FF
    for cpu, expected in ((0x400, 5 * 0x4000 + 0x400), (0x4123, 7 * 0x4000 + 0x123),
                          (0x8123, 9 * 0x4000 + 0x123)):
        assert model.resolve(cpu, state).rom_offset == expected
    state = model.write(state, 0xFFFD, None)
    assert model.resolve(0x400, state).space == "unknown"
    assert model.resolve(0x3FF, state).rom_offset == 0x3FF


def test_reset_is_a_separately_selected_recipe():
    model = Mapper("sega", SIZE)
    assert model.resolve(0x4123, BusState.cartridge_only()).rom_offset is None
    assert model.resolve(0x4123, model.reset()).rom_offset == 0x4123
    assert model.resolve(0x8123, model.reset()).rom_offset == 0x8123
    assert bank_mask(SIZE) == 31 and bank_mask(0xC000) == 3
    assert model.resolve(0x8000, model.write(model.reset(), 0xFFFF, 0x82)).bank == 2


@pytest.mark.parametrize("cpu", [0xC001, 0xE001, 0xDFFF, 0xFFFF])
def test_work_ram_aliases_are_not_rom(cpu):
    address = Mapper("sega", SIZE).resolve(cpu, BusState.cartridge_only())
    assert address.space == "wram" and address.offset == cpu & 0x1FFF and address.rom_offset is None
    assert wram_address(cpu) == 0xC000 + (cpu & 0x1FFF)


def test_ram_alias_does_not_write_a_mapper_register():
    model = Mapper("sega", SIZE)
    state = model.write(model.reset(), 0xFFFF, 5)
    assert model.write(state, 0xDFFF, 6) == state
    assert model.resolve(0x8000, state).bank == 5
    assert wram_address(0xFFFF) == wram_address(0xDFFF) == 0xDFFF
    assert wram_address(0x8000) is None


@pytest.mark.parametrize("control,bank", [(8, 0), (12, 1)])
def test_slot_two_ram_precedes_unknown_rom_bank(control, bank):
    address = Mapper("sega", SIZE).resolve(0x8123, BusState.cartridge_only(control=control))
    assert address.space == "cart_ram" and address.bank == bank and address.rom_offset is None


def test_upper_cartridge_ram_contention_and_disabled_work_ram():
    model = Mapper("sega", SIZE)
    state = BusState.cartridge_only(control=0x10)
    assert model.resolve(0xC000, state).space == "unknown"  # two devices may drive the bus
    assert model.resolve(0xC000, replace(state, wram=False)).space == "cart_ram"
    assert model.resolve(0xC000, replace(state, control=0, wram=False)).space == "unmapped"
    assert model.resolve(0xC000, replace(state, control=None)).space == "unknown"


def test_bios_disabled_cartridge_and_other_devices_cannot_resolve_to_rom():
    model = Mapper("sega", SIZE)
    state = model.reset()
    assert model.resolve(0, replace(state, bios=True)).space == "unknown"
    assert model.resolve(0, replace(state, bios=True, cartridge=False)).space == "bios"
    assert model.resolve(0, replace(state, cartridge=False)).space == "unmapped"
    assert model.resolve(0, replace(state, other_source=True)).space == "unknown"
    assert model.resolve(0x8000, replace(state, control=None)).space == "unknown"


def test_bank_shift_latches_only_after_next_bank_register_write():
    model = Mapper("sega", SIZE)
    initial = model.reset()
    control = model.write(initial, 0xFFFC, 1)
    assert control.banks == initial.banks
    shifted = model.write(control, 0xFFFF, 5)
    assert shifted.registers == (0, 1, 5) and shifted.banks == (24, 25, 29)
    assert model.resolve(0x8000, shifted).rom_offset == 29 * 0x4000
    unshift = model.write(shifted, 0xFFFC, 0)
    assert unshift.banks == shifted.banks
    assert model.write(unshift, 0xFFFE, 3).banks == (0, 3, 5)
    other = Mapper("sega", 0x20000)
    assert other.write(other.write(other.reset(), 0xFFFC, 1), 0xFFFF, 5).banks == (None,) * 3


def test_unknown_control_and_rom_writes_invalidate_only_established_facts():
    model = Mapper("sega", SIZE)
    state = model.write(model.reset(), 0xFFFC, None)
    assert model.write(state, 0xFFFF, 5).banks == (None,) * 3
    assert model.resolve(0, model.write(state, 0x1000, None)).space == "unknown"
    assert model.write(model.reset(), 0x1000, 7) == model.reset()  # ROM writes disabled explicitly
    writable = model.write(model.reset(), 0xFFFC, 0x80)
    assert model.resolve(0, model.write(writable, 0x1000, 7)).space == "unknown"
    disabled = replace(model.reset(), cartridge=None)
    assert model.write(disabled, 0xFFFF, 5).control is None


def test_state_is_immutable_and_paths_are_independent():
    model = Mapper("sega", SIZE)
    original = model.reset()
    left, right = model.write(original, 0xFFFF, 5), model.write(original, 0xFFFF, 6)
    assert (original.banks[2], left.banks[2], right.banks[2]) == (2, 5, 6)
    with pytest.raises(FrozenInstanceError):
        original.control = 8


def test_mapperless_does_not_assume_small_rom_mirroring_or_mapper_writes():
    model = Mapper("mapperless", 0xC000)
    state = BusState.cartridge_only()
    assert model.resolve(0x8123, state).rom_offset == 0x8123
    assert model.write(state, 0xFFFF, 5) == state
    assert model.resolve(0xC000, state).space == "wram"
    assert Mapper("mapperless", 0x2000).resolve(0x2123, state).space == "unknown"
    assert Mapper("sega", 0xC000).resolve(0x8000, BusState.cartridge_only((0, 1, 3))).space == "unknown"


def test_entry_evidence_only_establishes_its_own_window():
    state = entry_state(5 * 0x4000 + 0x123, 0x8123, SIZE)
    assert state.banks == (None, None, 5)
    assert entry_state(0x123, 0x123, SIZE).banks == (None,) * 3
    assert entry_state(0x14000, 0x8123, SIZE).banks == (None,) * 3
    assert sms.target_flat(0x8124, 5, SIZE, 0x8123) == 0x14000 + 0x124
    assert sms.target_flat(0x4123, 5, SIZE, 0x8123) is None


def test_explicit_bus_state_wins_over_instruction_geometry_in_adapters():
    state = BusState.cartridge_only((0, 1, 6), control=0)
    lane = banked.Lane("z80", {"size": SIZE})
    for cpu in (0x123, 0x4123, 0x8123, 0xC000):
        expected = Mapper("sega", SIZE).resolve(cpu, state).rom_offset
        assert sms.target_flat(cpu, 5, SIZE, 0x8123, mapping=state) == expected
        assert sms_trace.resolve(cpu, 0x14123, 0x8123, 1, 5, SIZE, mapping=state) == expected
        assert lane.flat_of(cpu, 0x14123, 0x8123, mapping=state) == expected
    assert sms_trace.resolve(0x8000, 0x14123, 0x8123, 1, 5, SIZE,
                             mapping=replace(state, control=8)) is None


@pytest.mark.parametrize("kind,size", [("unknown", SIZE), ("codemasters", SIZE), ("mapperless", SIZE),
                                      ("sega", 1), ("sega", True), ("sega", 0x2001)])
def test_unsupported_models_are_rejected(kind, size):
    with pytest.raises(ValueError):
        Mapper(kind, size)


def test_malformed_bus_facts_and_writes_are_rejected():
    for kwargs in ({"banks": (0, 1)}, {"banks": (0, 1, True)}, {"control": 256}, {"cartridge": 1}):
        with pytest.raises(ValueError):
            BusState(**kwargs)
    model = Mapper("sega", SIZE)
    for cpu, value in ((-1, 0), (0x10000, 0), (0xFFFF, 256), (0xFFFF, True)):
        with pytest.raises(ValueError):
            model.write(model.reset(), cpu, value)
    assert model.resolve(-1, model.reset()).space == "unknown"
