import io, os
mojibake_map = {
    '\u00e2\u201a\u00b9': '\u20B9',           # â‚¹ -> ₹
    '\u00f0\u0178\u2018\u2039': '\U0001F44B', # ðŸ'‹ -> 👋
    '\u00e2\u20ac\u201d': '\u2014',           # â€” -> —
    '\u00e2\u20ac\u201c': '\u2013',           # â€" -> –
    '\u00e2\u20ac\u02dc': '\u2018',           # â€˜ -> '
    '\u00e2\u20ac\u2122': '\u2019',           # â€™ -> '
    '\u00e2\u20ac\u0153': '\u201C',           # â€œ -> "
    '\u00e2\u20ac\ufffd': '\u201D',
    '\u00c2\u00b7': '\u00b7',                 # Â· -> ·
    '\u00c2\u00a0': '\u00a0',
}
folder = 'application/templates'
for f in os.listdir(folder):
    if not f.endswith('.html'):
        continue
    p = os.path.join(folder, f)
    t = io.open(p, 'r', encoding='utf-8').read()
    fixed = t
    n = 0
    for bad, good in mojibake_map.items():
        c = fixed.count(bad)
        if c:
            fixed = fixed.replace(bad, good)
            n += c
    if n:
        io.open(p, 'w', encoding='utf-8').write(fixed)
        print(f'fixed {n} in {f}')
