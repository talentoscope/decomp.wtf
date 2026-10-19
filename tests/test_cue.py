"""Original CUE/ISO transport fixtures; no retail assets and no assumed first data track."""
import hashlib
import json
import struct

import pytest

from decompnet.rom import cue, detect, psx
from decompnet.rom.mapping import MappingError, Profile, file_digest, prepare
from .test_overlays import EXE, _iso


def executable():
    header = bytearray(0x800)
    header[:8] = b'PS-X EXE'
    body = struct.pack('<II', 0x03E00008, 0) * 4
    struct.pack_into('<IIII', header, 0x10, 0x80010000, 0, 0x80010000, len(body))
    return bytes(header) + body


def disc(files=None):
    return _iso({'SYSTEM.CNF': b'BOOT = cdrom:\\BOOT.EXE;1\r\n', 'BOOT.EXE': executable(), **(files or {})})


def transport(iso, mode='MODE2/2352'):
    width, skip = cue.MODES[mode]
    out = bytearray()
    for at in range(0, len(iso), 2048):
        header = bytearray(skip)
        if width == 2352:
            header[:12] = b'\0' + b'\xff' * 10 + b'\0'
            header[15] = 1 if mode == 'MODE1/2352' else 2
        out += header + iso[at:at + 2048] + bytes(width - skip - 2048)
    return bytes(out)


def sheet(tmp_path, contents, files):
    for name, data in files.items():
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    path = tmp_path / 'disc.cue'
    path.write_text(contents)
    return path


@pytest.mark.parametrize('mode', ['MODE1/2048', 'MODE1/2352', 'MODE2/2048', 'MODE2/2336', 'MODE2/2352'])
def test_declared_binary_modes_and_plain_2336_are_readable(tmp_path, mode):
    path = sheet(tmp_path, f'FILE "track.bin" BINARY\n TRACK 01 {mode}\n INDEX 01 00:00:00\n',
                 {'track.bin': transport(disc(), mode)})
    picked = detect.identify(path)
    assert picked.data == executable() and picked.info['disc']['track'] == 1
    assert picked.info['disc']['sector_size'] == cue.MODES[mode][0]
    assert detect.identify((tmp_path / 'track.bin').read_bytes(), 'disc.img').data == executable()


def test_stored_index_pregap_is_a_file_offset_and_generated_gap_is_not(tmp_path):
    raw = transport(disc())
    path = sheet(tmp_path, 'FILE "track.bin" BINARY\nTRACK 01 MODE2/2352\nPREGAP 00:02:00\n'
                 'INDEX 00 00:00:00\nINDEX 01 00:02:00\n', {'track.bin': bytes(150 * 2352) + raw})
    track = cue.read(path).select()
    assert track.start == 150 * 2352 and track.pregap == 150
    assert detect.identify(path).data == executable()
    path.write_text('FILE "track.bin" BINARY\nTRACK 01 MODE2/2352\nPREGAP 00:02:00\nINDEX 01 00:00:00\n')
    (tmp_path / 'track.bin').write_bytes(raw)
    assert cue.read(path).select().start == 0


def test_audio_before_data_and_shared_file_index_are_honored(tmp_path):
    raw = transport(disc())
    path = sheet(tmp_path, 'FILE "mixed.bin" BINARY\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n'
                 'TRACK 02 MODE2/2352\nINDEX 01 00:01:00\n', {'mixed.bin': bytes(75 * 2352) + raw})
    picked = detect.identify(path)
    assert picked.info['disc']['track'] == 2 and picked.info['disc']['tracks'][1]['start'] == 75 * 2352
    assert picked.data == executable()


def test_separate_audio_file_is_never_sniffed_as_program_data(tmp_path):
    # Audio deliberately contains an ISO-looking payload. Its declared track type still excludes it.
    raw = transport(disc())
    path = sheet(tmp_path, 'FILE "audio.bin" BINARY\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n'
                 'FILE "data.bin" BINARY\nTRACK 02 MODE2/2352\nINDEX 01 00:00:00\n',
                 {'audio.bin': raw, 'data.bin': raw})
    assert cue.read(path).select().number == 2


def test_ambiguous_data_tracks_require_explicit_selection(tmp_path):
    raw = transport(disc())
    path = sheet(tmp_path, 'FILE "one.bin" BINARY\nTRACK 01 MODE2/2352\nINDEX 01 00:00:00\n'
                 'FILE "two.bin" BINARY\nTRACK 02 MODE2/2352\nINDEX 01 00:00:00\n', {'one.bin': raw, 'two.bin': raw})
    with pytest.raises(psx.RomError, match='several bootable'):
        detect.identify(path)
    assert detect.identify(path, cue_track=2).info['disc']['track'] == 2
    with pytest.raises(psx.RomError, match='1 to 99'):
        detect.identify(path, cue_track=0)
    with pytest.raises(psx.RomError, match='local CUE'):
        detect.identify(raw, cue_track=1)


def test_declared_mode_is_not_replaced_by_an_unrelated_geometry_guess(tmp_path):
    path = sheet(tmp_path, 'FILE "track.bin" BINARY\nTRACK 01 MODE1/2352\nINDEX 01 00:00:00\n',
                 {'track.bin': transport(disc(), 'MODE2/2352')})
    with pytest.raises(psx.RomError, match='no readable'):
        detect.identify(path)


def test_quoted_paths_backslashes_and_unique_case_fallback(tmp_path):
    path = sheet(tmp_path, 'FILE "SUBDIR\\TRACK 02.BIN" BINARY\nTRACK 01 MODE2/2336\nINDEX 01 00:00:00\n',
                 {'subdir/track 02.bin': transport(disc(), 'MODE2/2336')})
    picked = detect.identify(path)
    assert picked.data == executable()
    assert picked.info['disc']['tracks'][0]['file'] == 'track 02.bin'
    assert str(tmp_path) not in json.dumps(picked.info)


@pytest.mark.parametrize('extra', ['INDEX 01 00:00:00\nINDEX 01 00:00:01', 'INDEX 01 00:00:75',
                                  'INDEX 01 00:60:00', 'INDEX 00 00:02:00\nINDEX 01 00:00:00',
                                  'INDEX 02 00:00:00', 'INDEX 01 01:00:00', 'INDEX 01 00:00:00\nPREGAP 00:02:00'])
def test_bad_indices_and_gap_order_fail(tmp_path, extra):
    path = sheet(tmp_path, 'FILE "track.bin" BINARY\nTRACK 01 MODE2/2352\n' + extra + '\n',
                 {'track.bin': transport(disc())})
    with pytest.raises(psx.RomError):
        cue.read(path)


def test_truncated_file_and_mixed_width_shared_file_fail(tmp_path):
    path = sheet(tmp_path, 'FILE "track.bin" BINARY\nTRACK 01 MODE2/2352\nINDEX 01 00:00:00\n',
                 {'track.bin': transport(disc())[:-1]})
    with pytest.raises(psx.RomError, match='truncated sector'):
        cue.read(path)
    (tmp_path / 'track.bin').write_bytes(transport(disc()))
    path.write_text('FILE "track.bin" BINARY\nTRACK 01 MODE2/2352\nINDEX 01 00:00:00\n'
                    'TRACK 02 MODE1/2048\nINDEX 01 00:00:01\n')
    with pytest.raises(psx.RomError, match='mixed sector widths'):
        cue.read(path)


@pytest.mark.parametrize('filename', ['../outside.bin', '/outside.bin', 'missing.bin'])
def test_missing_and_outside_assets_are_rejected_without_reading_them(tmp_path, filename):
    path = sheet(tmp_path, f'FILE "{filename}" BINARY\nTRACK 01 MODE2/2352\nINDEX 01 00:00:00\n', {})
    with pytest.raises(psx.RomError, match='selected folder'):
        cue.read(path)


def test_sector_and_extent_truncation_fail_instead_of_looping():
    raw = transport(disc())
    reader = psx._Disc(raw)
    with pytest.raises(psx.RomError, match='truncated'):
        reader.read(len(raw) // 2352 - 1, 4096)
    with pytest.raises(psx.RomError, match='negative'):
        reader.sector(-1)
    with pytest.raises(psx.RomError, match='truncated'):
        psx.read_exe(b'PS-X EXE')


def test_byte_only_cue_upload_gives_local_path_guidance():
    with pytest.raises(psx.RomError, match='local path'):
        detect.identify(b'FILE "track.bin" BINARY\n', 'disc.cue')


def test_mapping_cue_identity_pins_all_tracks_and_reads_indexed_data(tmp_path):
    add = struct.pack('<II', 0x03E00008, 0x00851021)
    path = sheet(tmp_path, 'FILE "data.bin" BINARY\nTRACK 01 MODE2/2336\nINDEX 00 00:00:00\nINDEX 01 00:00:02\n'
                 'FILE "audio.bin" BINARY\nTRACK 02 AUDIO\nINDEX 01 00:00:00\n',
                 {'data.bin': bytes(2 * 2336) + transport(disc({'CODE.BIN': add}), 'MODE2/2336'), 'audio.bin': bytes(2352)})
    obj = {'version': 1, 'platform': 'psx', 'title': 'CUE mapping fixture',
           'input': {'format': 'psx-disc', 'sha256': file_digest(path)},
           'regions': [{'id': 'code', 'file': 'CODE.BIN', 'source_sha256': hashlib.sha256(add).hexdigest(),
                        'offset': 0, 'size': 8, 'vram': 0x80100000, 'kind': 'code', 'confidence': 'verified',
                        'evidence': 'original synthetic indexed-track fixture'}]}
    prepared = prepare(Profile.parse(json.dumps(obj)), path, tmp_path / 'analysis')
    assert prepared.data['code'] == add
    (tmp_path / 'audio.bin').write_bytes(b'X' + bytes(2351))
    assert file_digest(path) != obj['input']['sha256']
    with pytest.raises(MappingError, match='input SHA-256 differs'):
        prepare(Profile.parse(json.dumps(obj)), path, tmp_path / 'changed')


def test_cue_import_reaches_normal_ps1_split_and_stores_only_sanitized_provenance(tmp_path):
    if not EXE.exists():
        pytest.skip('needs the owned synthetic PS1 demo')
    from decompnet.app.engine import RomNetwork
    raw = transport(_iso({'SYSTEM.CNF': b'BOOT = cdrom:\\BOOT.EXE;1\r\n', 'BOOT.EXE': EXE.read_bytes()}), 'MODE2/2336')
    path = sheet(tmp_path, 'FILE "data.bin" BINARY\nTRACK 01 MODE2/2336\nINDEX 01 00:00:00\n', {'data.bin': raw})
    net = RomNetwork(tmp_path / 'app.db', tmp_path / 'workspace')
    slug = net.import_rom(path, confirmed=True, log=lambda _: None)
    assert net.store.one('SELECT COUNT(*) AS n FROM functions')['n'] > 10
    assert net.cfg(net._project(slug))['rom']['disc']['sector_size'] == 2336


def test_archive_discovery_profile_can_be_prepared_from_cue(tmp_path):
    from decompnet.rom import overlay_discovery
    from .test_overlay_discovery import executable as overlay_exe
    archive = bytes(0x20000) + overlay_exe()
    path = sheet(tmp_path, 'FILE "data.bin" BINARY\nTRACK 01 MODE2/2336\nINDEX 01 00:00:00\n',
                 {'data.bin': transport(disc({'PACK.PAC': archive}), 'MODE2/2336')})
    report = overlay_discovery.discover(path)
    assert report['profile']['input']['sha256'] == cue.digest(path)
    profile = Profile.parse(json.dumps(report['profile']))
    assert profile.regions[0].file == 'PACK.PAC' and profile.regions[0].offset == 0x20800
    prepared = prepare(profile, path, tmp_path / 'discovered')
    assert len(prepared.functions[profile.regions[0].id]) == 40


def test_browser_accepts_dropped_2336_image():
    import shutil
    if not shutil.which('node'):
        pytest.skip('needs node')
    from .test_sms import _page_identify
    assert _page_identify(transport(disc(), 'MODE2/2336'), 'disc.img')['platform'] == 'playstation'
