"""CD-ROM EDC and two-parity Reed–Solomon codes, using syndrome equations over GF(256).

The parity symbols solve sum(c)=0 and sum(c*alpha**position)=0. This original implementation uses
weighted syndromes, with physical stripe geometry from PSX-SPX's CDROM Format reference.
"""
from functools import lru_cache


def multiply(left, right):
    result = 0
    while right:
        if right & 1:
            result ^= left
        left <<= 1
        if left & 0x100:
            left ^= 0x11D
        right >>= 1
    return result


def power(value, exponent):
    result = 1
    while exponent:
        if exponent & 1:
            result = multiply(result, value)
        value = multiply(value, value)
        exponent >>= 1
    return result


@lru_cache
def weights(length):
    return [bytes(multiply(v, power(2, length - i + 1)) for v in range(256)) for i in range(length)]


INVERSE_THREE = power(3, 254)


def rs(values):
    total = weighted = 0
    for i, value in enumerate(values):
        total ^= value
        weighted ^= weights(len(values))[i][value]
    first = multiply(weighted ^ total, INVERSE_THREE)
    return first, first ^ total


def ecc(data, columns, rows, stride, first_stride):
    first, second = bytearray(), bytearray()
    for column in range(columns):
        start = (column // 2) * first_stride + column % 2
        a, b = rs([data[(start + stride * row) % len(data)] for row in range(rows)])
        first.append(a)
        second.append(b)
    return first + second


@lru_cache
def edc_table():
    result = []
    for byte in range(256):
        for _ in range(8):
            byte = (byte >> 1) ^ (0xD8018001 if byte & 1 else 0)
        result.append(byte)
    return result


def edc(data):
    value = 0
    table = edc_table()
    for byte in data:
        value = (value >> 8) ^ table[(value ^ byte) & 255]
    return value.to_bytes(4, 'little')


def regenerate(raw):
    out = bytearray(raw)
    if len(out) != 2352 or out[:12] != b'\0' + b'\xff' * 10 + b'\0':
        raise ValueError('expected a complete raw CD-ROM sector with sync')
    if out[15] == 1:
        out[0x810:0x814] = edc(out[:0x810])
        out[0x814:0x81c] = bytes(8)
        data = out[12:0x81c]
    elif out[15] == 2 and not out[18] & 0x20:
        if out[16:20] != out[20:24]:
            raise ValueError('Mode 2 subheader copies differ')
        out[0x818:0x81c] = edc(out[16:0x818])
        data = bytes(4) + out[16:0x81c]
    else:
        raise ValueError('unsupported sector')
    out[0x81c:0x8c8] = ecc(data, 86, 24, 86, 2)
    out[0x8c8:0x930] = ecc(data + out[0x81c:0x8c8], 52, 43, 88, 86)
    return bytes(out)



def update_sector(raw: bytes, data: bytes, size: int, skip: int) -> bytes:
    """Replace Form 1 userdata, preserving every untouched sector and all header/subchannel bytes."""
    if len(raw) != size or len(data) != 2048 or (size, skip) not in ((2048, 0), (2336, 8), (2352, 16), (2352, 24), (2448, 16), (2448, 24)):
        raise ValueError('unsupported or truncated data sector')
    if raw[skip:skip + 2048] == data:
        return raw
    if size == 2048:
        return data
    if size == 2336:
        prefix = bytes([0]) + bytes([255]) * 10 + bytes([0, 0, 0, 0, 2])
        sector = bytearray(prefix + raw)
        sector[24:24 + 2048] = data
        return regenerate(sector)[16:]
    expected = 1 if skip == 16 else 2
    if raw[15] != expected:
        raise ValueError('changed sector mode differs from the data-track geometry')
    sector = bytearray(raw[:2352])
    sector[skip:skip + 2048] = data
    return regenerate(sector) + raw[2352:]
