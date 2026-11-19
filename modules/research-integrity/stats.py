"""Statistical-reporting check with an explicit grammar. Stdlib numerics only, no network, no model.

Grammar (after NFKC and mapping the minus signs to "-"), each followed by `, p = y`, `, p < y`, ...:
  t(df) = x      F(d1, d2) = x      z = x      chi2(df) = x   (also written with the chi letter)
The two-sided p of t and z is recomputed; F and chi2 use the upper tail. A reported `p = v` is
consistent when some statistic inside the rounding interval of x (half a unit of its last digit) gives
a p that rounds to v at v's decimals. `p < c` passes when the smallest p over that interval is below c
(`>`, the non-strict forms likewise). Any `p` comparison left after the grammar matches is reported as
`unsupported-grammar` and never counts as a pass.
"""

import math
import re
import unicodedata

NUM = r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)"
DF = r"\d+(?:\.\d+)?"
TAIL = r"\s*,\s*p\s*(<=|>=|≤|≥|<|>|=)\s*(\d*\.\d+|\d+)"
PATTERNS = (
    ("t", re.compile(rf"(?<![\w])t\s*\(\s*({DF})\s*\)\s*=\s*({NUM}){TAIL}")),
    ("F", re.compile(rf"(?<![\w])F\s*\(\s*({DF})\s*,\s*({DF})\s*\)\s*=\s*({NUM}){TAIL}")),
    ("z", re.compile(rf"(?<![\w])z\s*=\s*({NUM}){TAIL}")),
    ("chi2", re.compile(rf"(?<![\w])(?:chi2|χ2)\s*\(\s*({DF})\s*\)\s*=\s*({NUM}){TAIL}")),
)
STATLIKE = re.compile(r"(?<![\w])p\s*[<=>≤≥]")
EPS = 1e-12


def _betacf(a, b, x):
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def betai(a, b, x):
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def gammaq(a, x):
    """Regularised upper incomplete gamma Q(a, x)."""
    if x <= 0.0:
        return 1.0
    front = math.exp(-x + a * math.log(x) - math.lgamma(a))
    if x < a + 1.0:
        term = total = 1.0 / a
        n = a
        for _ in range(2000):
            n += 1.0
            term *= x / n
            total += term
            if abs(term) < abs(total) * 1e-16:
                break
        return 1.0 - total * front
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 2000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = d if abs(d) > tiny else tiny
        c = b + an / c
        c = c if abs(c) > tiny else tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return front * h


def p_value(kind, stat, dfs):
    s = abs(stat)
    if kind == "t":
        return betai(dfs[0] / 2.0, 0.5, dfs[0] / (dfs[0] + s * s))
    if kind == "F":
        return betai(dfs[1] / 2.0, dfs[0] / 2.0, dfs[1] / (dfs[1] + dfs[0] * s))
    if kind == "z":
        return math.erfc(s / math.sqrt(2.0))
    return gammaq(dfs[0] / 2.0, s / 2.0)


def decimals(text):
    return len(text.split(".", 1)[1]) if "." in text else 0


def p_range(kind, stat_text, dfs):
    """(smallest, largest) p over the rounding interval of the reported statistic."""
    half = 0.5 * 10.0 ** -decimals(stat_text.lstrip("+-"))
    x = abs(float(stat_text))
    return p_value(kind, x + half, dfs), p_value(kind, max(0.0, x - half), dfs)


def consistent(op, p_text, low, high):
    c = float(p_text)
    if op == "=":
        half = 0.5 * 10.0 ** -decimals(p_text)
        return low < c + half - EPS and high >= c - half - EPS
    if op in ("<", "<="):
        return low < c if op == "<" else low <= c
    return high > c if op == ">" else high >= c


def fmt(p):
    return f"{p:.4f}".lstrip("0") if p < 1 else "1"


def normalise(text):
    return unicodedata.normalize("NFKC", text).translate({0x2212: "-", 0x2013: "-", 0x2014: "-"})


def check_text(text):
    """Return one item per statistic or unsupported fragment: kind, text, status, detail."""
    text = normalise(text)
    items, spans = [], []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groups()
            dfs = [float(g) for g in groups[:-3]]
            stat_text, op, p_text = groups[-3], groups[-2], groups[-1]
            low, high = p_range(kind, stat_text, dfs)
            ok = consistent(op, p_text, low, high)
            detail = "" if ok else f"recomputed p = {fmt(p_value(kind, float(stat_text), dfs))}"
            items.append({"kind": kind, "text": match.group(0), "status": "PASS" if ok else "FAIL",
                          "detail": detail, "at": match.start()})
            spans.append((match.start(), match.end()))
    rest = list(text)
    for start, end in spans:
        rest[start:end] = " " * (end - start)
    leftover = "".join(rest)
    for match in STATLIKE.finditer(leftover):
        start = max(text.rfind("\n", 0, match.start()) + 1, match.start() - 24)
        end = text.find("\n", match.end())
        snippet = re.sub(r"\s+", " ", text[start:min(match.end() + 16, end if end >= 0 else len(text))]).strip()
        items.append({"kind": "unsupported", "text": snippet, "status": "unsupported-grammar",
                      "detail": "outside the supported grammar", "at": match.start()})
    return sorted(items, key=lambda item: item["at"])
