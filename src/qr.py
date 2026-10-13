# -*- coding: utf-8 -*-
"""零依赖二维码生成器（MVP）

范围：字节模式、纠错等级 L、版本 1–5（单块纠错）。
payload 上限 106 字节（v5）。超出则抛 PayloadTooLarge，
调用方应降级为纯文本 order recipe（不带二维码）。

算法：标准 QR（模式头 0100 + 8bit 计数 + 数据 + 终止符 + 0xEC/0x11 填充，
Reed-Solomon GF(256) 0x11d，格式信息 BCH(15,5) poly=0x537 mask=0x5412）。
"""
import itertools

GF_EXP = [0] * 512
GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    GF_EXP[_i] = _x
    GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    GF_EXP[_i] = GF_EXP[_i - 255]


def _gmul(a, b):
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        poly = [_gmul(p, GF_EXP[i]) ^ prev if j == 0 else _gmul(p, GF_EXP[i]) ^ prev
                for j, (p, prev) in enumerate(zip([0] + poly, poly + [0]))]
    # 上式复杂易错，改为标准连乘
    poly = [1]
    for i in range(degree):
        factor = [1, GF_EXP[i]]
        new = [0] * (len(poly) + 1)
        for j, p in enumerate(poly):
            for k, f in enumerate(factor):
                new[j + k] ^= _gmul(p, f)
        poly = new
    return poly


def _rs_encode(data: list[int], ecc_len: int) -> list[int]:
    gen = _rs_generator(ecc_len)
    res = [0] * ecc_len
    for b in data:
        factor = b ^ res[0]
        res = res[1:] + [0]
        for i, g in enumerate(gen[1:]):
            res[i] ^= _gmul(g, factor)
    return res


# 纠错等级 L 的 (数据码字, ECC 码字)，版本 1-5 均为单块
_ECC_L = {1: (19, 7), 2: (34, 10), 3: (55, 15), 4: (80, 20), 5: (108, 26)}
_ALIGNMENT = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30]}


class PayloadTooLarge(Exception):
    pass


def _bits_to_bytes(bits) -> list[int]:
    out = []
    for i in range(0, len(bits), 8):
        chunk = bits[i:i + 8]
        while len(chunk) < 8:
            chunk.append(0)
        v = 0
        for b in chunk:
            v = (v << 1) | b
        out.append(v)
    return out


def _build_data_codewords(payload: bytes, data_len: int) -> list[int]:
    bits = [1, 0, 0] + [0]  # 0100 byte mode
    n = len(payload)
    bits += [(n >> i) & 1 for i in range(7, -1, -1)]  # 8bit count (v1-9)
    for byte in payload:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    # 终止符
    bits += [0] * min(4, max(0, data_len * 8 - len(bits)))
    if len(bits) % 8:
        bits += [0] * (8 - len(bits) % 8)
    codewords = _bits_to_bytes(bits)
    while len(codewords) < data_len:
        codewords += [0xEC, 0x11][: data_len - len(codewords)]
    return codewords[:data_len]


def _make_matrix(version: int, codewords: list[int]) -> list[list[bool]]:
    size = 17 + 4 * version
    mat = [[None] * size for _ in range(size)]

    def set_fn(r, c, v):
        mat[r][c] = bool(v)

    # 定位图形 + 分隔符
    def finder(r, c):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if not (0 <= rr < size and 0 <= cc < size):
                    continue
                if (0 <= dr < 7 and 0 <= dc < 7 and
                        (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))):
                    set_fn(rr, cc, True)
                else:
                    set_fn(rr, cc, False)

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    # 时序图形
    for i in range(8, size - 8):
        set_fn(6, i, i % 2 == 0)
        set_fn(i, 6, i % 2 == 0)

    # 校正图形
    centers = _ALIGNMENT[version]
    for r, c in itertools.product(centers, centers):
        if mat[r][c] is not None and isinstance(mat[r][c], bool):
            continue
        # 避开三个定位图形区
        if (r <= 8 and c <= 8) or (r <= 8 and c >= size - 9) or (r >= size - 9 and c <= 8):
            continue
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                set_fn(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)

    # 格式信息区预留（先置 False，后写）
    reserved = [[False] * size for _ in range(size)]
    for i in range(size):
        for j in range(size):
            if mat[i][j] is not None:
                reserved[i][j] = True

    # 数据放置
    all_bits = []
    for b in codewords:
        all_bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        for i in range(size):
            r = (size - 1 - i) if upward else i
            for c in (col, col - 1):
                if not reserved[r][c]:
                    v = all_bits[idx] if idx < len(all_bits) else False
                    mat[r][c] = bool(v)
                    idx += 1
        upward = not upward
        col -= 2

    # 掩码 + 格式信息：尝试 8 种掩码，取简化罚分最低
    best_mat, best_score = None, None
    for mask in range(8):
        m2 = [row[:] for row in mat]
        for r in range(size):
            for c in range(size):
                if not reserved[r][c]:
                    m2[r][c] ^= _mask_bit(mask, r, c)
        fmt = _format_bits(mask)
        _draw_format(m2, fmt, size)
        score = _penalty(m2)
        if best_score is None or score < best_score:
            best_mat, best_score = m2, score
    return best_mat


def _mask_bit(mask: int, r: int, c: int) -> bool:
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0


def _format_bits(mask: int) -> int:
    data = (0b01 << 3) | mask  # L=01
    rem = data << 10
    g = 0x537
    for i in range(4, -1, -1):
        if rem & (1 << (i + 10)):
            rem ^= g << i
    return ((data << 10) | rem) ^ 0x5412


def _draw_format(mat, fmt: int, size: int):
    for i in range(15):
        bit = bool((fmt >> i) & 1)
        # around top-left
        if i < 6:
            mat[8][i] = bit
        elif i == 6:
            mat[8][7] = bit
        elif i == 7:
            mat[8][8] = bit
        elif i == 8:
            mat[7][8] = bit
        else:
            mat[14 - i][8] = bit
        # mirrored
        if i < 8:
            mat[size - 1 - i][8] = bit
        else:
            mat[8][size - 15 + i] = bit
    mat[size - 8][8] = True  # 固定暗模块


def _penalty(m) -> int:
    size = len(m)
    score = 0
    # 规则1：同行/列连续同色
    for row in m + [list(c) for c in zip(*m)]:
        run = 1
        for i in range(1, size):
            if row[i] == row[i - 1]:
                run += 1
                if run == 5:
                    score += 3
                elif run > 5:
                    score += 1
            else:
                run = 1
    # 规则2：2x2 同色块
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    return score


def qr_matrix(text: str) -> list[list[bool]]:
    payload = text.encode("utf-8")
    for version in sorted(_ECC_L):
        data_len, ecc_len = _ECC_L[version]
        if len(payload) + 2 <= data_len:  # 2 = 模式+计数开销
            cw = _build_data_codewords(payload, data_len)
            ecc = _rs_encode(cw, ecc_len)
            final = cw + ecc
            # 交错了不重要：单块直接拼接
            return _make_matrix(version, final)
    raise PayloadTooLarge(
        f"payload {len(payload)} 字节超出 MVP 二维码上限（v5=106 字节），"
        f"请缩短 order recipe 或降级为纯文本分享。"
    )


def qr_svg(text: str, module: int = 6, quiet: int = 4, dark: str = "#1d2129",
           light: str = "#ffffff") -> str:
    m = qr_matrix(text)
    size = len(m)
    w = (size + quiet * 2) * module
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {w}" width="{w}" height="{w}">']
    parts.append(f'<rect width="{w}" height="{w}" fill="{light}"/>')
    parts.append(f'<path fill="{dark}" d="')
    for r in range(size):
        for c in range(size):
            if m[r][c]:
                x = (c + quiet) * module
                y = (r + quiet) * module
                parts.append(f"M{x} {y}h{module}v{module}h-{module}z")
    parts.append('"/></svg>')
    return "".join(parts)
