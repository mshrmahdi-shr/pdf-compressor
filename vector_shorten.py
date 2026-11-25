#!/usr/bin/env python3
"""vector_shorten.py — Safe shortening of vector drawing numbers in PDF
content streams (the "deep" mode of pdf_compressor.py).

Vendored from the one-off scripts that proved this method on a 114-page
Phase Two cross-sections PDF (November 2026): the operator-aware,
string-safe tokenizer comes from aggressive_round.py and the rounding
rules from round_final.py. Nothing here is imported from those scripts;
this module is self-contained so the repo stays portable.

What it does
------------
A PDF content stream is a list of drawing instructions: numbers followed
by an operator, e.g. ``0.161 0.184 0.439 rg`` (set the fill colour to a
dark blue) or ``1179.9 1522.53 cm`` (place a shape). The numbers carry
far more decimal places than the drawing needs. Shortening them makes the
decoded stream smaller, and Zopfli then compresses the shorter stream
even further. The visible drawing does not change: text strings are
copied verbatim, so every printed number and table value stays identical.

Rules (learned the hard way — do not "simplify" them)
------------------------------------------------------
- NEVER round scale/rotation components of cm/Tm matrices, EXCEPT the
  exact value 0.479969 -> 0.48 (and -0.479969 -> -0.48). Shapes in these
  engineering PDFs live in a ~0.48-scaled frame with coordinates up to
  ~3000, so rounding e.g. 0.479379 -> 0.479 moves far points by ~1 pt.
  A naive "round all decimals to 2dp" changed ~20% of rendered pixels
  and was rejected.
- NEVER touch Tc/Tw/Tz/TL/Tf/Ts/TJ/'/" numbers: per-glyph spacing
  accumulates along a text line, so rounding those shifts later glyphs.
- Round ONLY:
    * cm/Tm/Td/TD translations and path coordinates (m/l/c/v/y/re) to
      1 decimal place (max shift 0.05 pt = 0.018 mm);
    * colours (rg/RG/g/G/k/K/sc/SC/scn/SCN) to 2 decimals with
      ROUND_HALF_UP (so 0.995 -> 1, not 0.99).
- Literal strings (...) and hex strings <...> are copied verbatim, so
  extracted text never changes.
- Any content stream containing a BI (inline image) operator is left
  completely untouched.

Verification (in pdf_compressor.py) checks the result afterwards — page
count, page sizes, extracted text, and a render comparison — and falls
back to the lossless result if anything looks wrong.
"""

import re
import zlib
from decimal import Decimal, ROUND_HALF_UP

# Operators whose numeric operands may be shortened.
ROUND_OPS = {
    "m", "l", "c", "v", "y", "re",       # path coordinates -> 1dp
    "cm", "Tm", "Td", "TD",              # cm/Tm: translations -> 1dp
    "rg", "RG", "g", "G", "k", "K",      # colours -> 2dp
    "sc", "SC", "scn", "SCN",
}

# The ONLY scale/rotation value that may be changed, and its replacement.
# Every other cm/Tm operand in slots 1-4 is left byte-identical.
SCALE_EXACT = {b"0.479969": b"0.48", b"-0.479969": b"-0.48"}

DELIMS = set(b" \t\r\n\f\x00()<>[]{}/%")
NUM_RE = re.compile(rb"[+-]?(?:\d+\.\d*|\.\d+|\d+)")


def _scan_string(data, start):
    """Return end index (exclusive) of a literal string starting at start.

    Handles nested parentheses and backslash escapes, so a ')' inside a
    string (or a number inside a string) is never mistaken for structure.
    """
    depth = 1
    i = start + 1
    n = len(data)
    while i < n and depth > 0:
        c = data[i]
        if c == 0x5C:  # backslash
            if i + 1 < n and data[i + 1] == 0x0D:  # \r or \r\n continuation
                i += 3 if (i + 2 < n and data[i + 2] == 0x0A) else 2
            else:
                i += 2
            continue
        if c == 0x28:  # (
            depth += 1
        elif c == 0x29:  # )
            depth -= 1
        i += 1
    return i  # if depth > 0 the string is unterminated; verbatim to EOF


def tokens(data):
    """Yield (kind, bytes) tokens from a content stream, skipping whitespace.

    kind is one of: comment, string, hex, dict, array, name, number, op,
    other. Strings and hex strings are yielded whole, so their contents
    are never split or modified by the caller.
    """
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c in b" \t\r\n\f\x00":
            i += 1
            continue
        if c == 0x25:  # % comment
            j = data.find(b"\n", i)
            j = n if j < 0 else j
            yield ("comment", data[i:j])
            i = j
            continue
        if c == 0x28:  # ( literal string
            j = _scan_string(data, i)
            yield ("string", data[i:j])
            i = j
            continue
        if c == 0x3C:  # <
            if i + 1 < n and data[i + 1] == 0x3C:
                yield ("dict", b"<<")
                i += 2
                continue
            j = data.find(b">", i)
            j = n if j < 0 else j + 1
            yield ("hex", data[i:j])
            i = j
            continue
        if c == 0x3E:  # >
            if i + 1 < n and data[i + 1] == 0x3E:
                yield ("dict", b">>")
                i += 2
                continue
            yield ("other", b">")
            i += 1
            continue
        if c in b"[]":
            yield ("array", data[i:i + 1])
            i += 1
            continue
        if c == 0x2F:  # / name
            j = i + 1
            while j < n and data[j] not in DELIMS:
                j += 1
            yield ("name", data[i:j])
            i = j
            continue
        if c in b"+-.0123456789":
            m = NUM_RE.match(data, i)
            if m and (c not in b"+-." or (m.end() > i + 1)):
                yield ("number", m.group(0))
                i = m.end()
                continue
            yield ("other", data[i:i + 1])
            i += 1
            continue
        if c == 0x27 or c == 0x22:  # ' "
            yield ("op", data[i:i + 1])
            i += 1
            continue
        if 0x41 <= c <= 0x5A or 0x61 <= c <= 0x7A:  # letters
            j = i
            while j < n and (0x41 <= data[j] <= 0x5A or 0x61 <= data[j] <= 0x7A):
                j += 1
            if j < n and data[j] == 0x2A:  # trailing *
                j += 1
            yield ("op", data[i:j])
            i = j
            continue
        yield ("other", data[i:i + 1])
        i += 1


def _fmt_dec(value, ndigits):
    """Format a float to ndigits decimals (HALF_UP), stripping trailing zeros."""
    q = Decimal(1).scaleb(-ndigits)
    s = str(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _round_token(tok, ndigits):
    """Shorten one numeric token to ndigits decimals; leave integers alone."""
    if b"." not in tok:
        return tok
    try:
        v = float(tok.decode("latin-1"))
    except ValueError:
        return tok
    return _fmt_dec(v, ndigits).encode("latin-1")


def transform_stream(data):
    """Apply the safe shortening to one decoded content stream.

    Returns (new_data, skipped). skipped is True when the stream contains
    a BI inline-image operator — such streams are returned unchanged.
    """
    buf = []
    out = bytearray()

    def flush(opname):
        nonlocal buf, out
        if not buf:
            return
        if opname in ROUND_OPS:
            nidx = 0  # counts numeric operands only
            for kind, tok in buf:
                if kind == "number":
                    if opname in ("cm", "Tm") and nidx < 4:
                        # scale/rotation slots: only the exact known value
                        out += SCALE_EXACT.get(tok, tok)
                    elif opname in (
                        "rg", "RG", "g", "G", "k", "K",
                        "sc", "SC", "scn", "SCN",
                    ):
                        out += _round_token(tok, 2)
                    else:
                        # translations and path coordinates -> 1dp
                        out += _round_token(tok, 1)
                    nidx += 1
                else:
                    out += tok
                out += b" "
        else:
            for _kind, tok in buf:
                out += tok
                out += b" "
        buf = []

    for kind, tok in tokens(data):
        if kind == "op" and tok == b"BI":
            return data, True
        if kind == "op":
            flush(tok.decode("latin-1", errors="ignore"))
            out += tok
            out += b" "
        else:
            buf.append((kind, tok))
    flush("")
    return bytes(out), False


def shorten_content_streams(pdf, verbose=True):
    """Apply transform_stream to every page content stream in place.

    Streams containing a BI operator are skipped. A shortened stream is
    immediately re-encoded with zlib and tagged /FlateDecode (with
    /DecodeParms dropped, because the bytes are predictor-decoded) —
    exactly the state the later Zopfli pass expects. This matters:
    pikepdf's stream.write() with filter=None stores the bytes raw and
    drops /Filter, so a shortened stream left unencoded would be
    invisible to the Zopfli pass and would only get plain zlib on save
    (that bug cost ~33 KB on the Partial Building Permit test file).
    Returns a stats dict: {"streams_changed": int,
    "streams_skipped_bi": int, "decoded_bytes_trimmed": int}.
    """
    if _pikepdf is None:
        raise RuntimeError("shorten_content_streams needs pikepdf")
    changed = 0
    skipped = 0
    trimmed = 0
    for page in pdf.pages:
        contents = page.get("/Contents")
        if contents is None:
            continue
        streams = (
            list(contents)
            if isinstance(contents, _Array)
            else [contents]
        )
        for stream in streams:
            try:
                data = stream.read_bytes()
            except Exception:
                continue
            new_data, was_skipped = transform_stream(data)
            if was_skipped:
                skipped += 1
                continue
            if new_data != data:
                # Re-encode now (zlib) so the stream stays a normal,
                # consistent Flate stream for the Zopfli pass.
                compressed = zlib.compress(new_data, 9)
                stream.write(compressed, filter=None)
                stream["/Filter"] = _pikepdf.Name("/FlateDecode")
                if "/DecodeParms" in stream:
                    del stream["/DecodeParms"]
                changed += 1
                trimmed += len(data) - len(new_data)
    if verbose:
        print(
            f"  Deep shorten: {changed} content streams shortened "
            f"({trimmed} decoded bytes trimmed, "
            f"{skipped} BI streams skipped)",
            flush=True,
        )
    return {
        "streams_changed": changed,
        "streams_skipped_bi": skipped,
        "decoded_bytes_trimmed": trimmed,
    }


# pikepdf.Array is only importable when pikepdf is present; fall back to a
# duck-type check so this module can still be imported for its pure
# functions (tokenizer / transform) without the dependency.
try:
    import pikepdf as _pikepdf

    _Array = _pikepdf.Array
except ImportError:  # pragma: no cover - pikepdf missing
    _Array = ()
