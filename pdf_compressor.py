#!/usr/bin/env python3
"""
pdf_compressor.py — Shrink an already-optimized PDF without changing how it looks.

Why this exists
---------------
Most PDF optimizers (including the "Small PDF" presets in Acrobat-style tools)
already squeeze a file with standard zlib compression. Running them again
usually makes the file *bigger*, not smaller.

This tool goes one step further, with three lossless-or-near-lossless tricks:

1.  Downscale large raster images (optional, the only lossy step).
    Large map/photo images are halved in resolution (0.5x by default) and
    re-saved as JPEG quality 50. They keep the same display size on the page,
    but are slightly less sharp when zoomed far in. Small icons are left alone.

2.  Recompress every Flate stream with Zopfli.
    Zopfli produces standard DEFLATE data (the same format PDF readers already
    understand), but it searches much harder for repeated patterns than zlib.
    It is slow, but it typically saves another 10-20% on content streams that
    zlib already compressed. Text and vector content are preserved exactly.

3.  Remove non-visible data.
    Measurement viewports (/VP) and /LastModified stamps are removed from
    pages. They do not affect appearance, text, or print output.

Compression slider
------------------
The companion GUI (pdf_compressor_gui.py) has a slider for compression
strength, 0-100. The same mapping is available on the command line with
``--compression-percent``:

-   0%  -> lossless only (no image changes, Zopfli + metadata cleanup)
-  50%  -> images 0.5x, JPEG quality 50 (balanced, recommended — this is the
           setting that achieved 21.14% on a 44-page ESA report)
- 100%  -> images 0.2x, JPEG quality 25 (maximum shrink, maps much less sharp)

The slider sets *strength*, not a guaranteed final percentage: the actual
reduction depends on the file. A vector-only file with no large images
maxes out around 17-18% even at 100%, because there is no image left to
shrink. The tool always reports the actual achieved percentage afterwards.

The key implementation trick: neither PyMuPDF nor pikepdf will preserve
hand-compressed stream bytes if you ask them to write normally — they strip
/Filter or recompress with zlib. So this tool writes the Zopfli (or JPEG)
bytes raw, then sets /Filter manually, deletes /DecodeParms (because
pikepdf's read_bytes() already returns predictor-decoded data), and saves
with ``recompress_flate=False`` so the optimized bytes survive to disk.

Two safety rules added in the November 2026 update, learned from a batch of
legal/land documents:

- An image is replaced only when the new bytes are strictly smaller than
  the original stored bytes. Tiny template graphics (seals, banners, logos)
  that would grow as JPEG are left untouched.
- A 1-bit black-and-white scan is never converted to JPEG. It is downscaled,
  thresholded back to 1-bit, and re-saved as Flate, with its /Decode
  preserved — and the threshold is flipped when the original /Decode is
  inverted ([1 0], the usual scanner convention), otherwise the whole page
  renders inverted. This keeps survey linework sharp instead of JPEG-blurred.
- The output is never bigger than the input: if compression would grow a
  file, the original is copied to the output path instead, and the result
  reports 0% reduction for that file.

Usage
-----
    python pdf_compressor.py input.pdf output.pdf
    python pdf_compressor.py input.pdf output.pdf --compression-percent 50
    python pdf_compressor.py input.pdf output.pdf --no-image-downscale
    python pdf_compressor.py input.pdf output.pdf --image-scale 0.4 --image-quality 40
    python pdf_compressor.py input.pdf output.pdf --compression-percent 0 --deep
    python pdf_compressor.py file1.pdf file2.pdf file3.pdf --zip compressed.zip --compression-percent 50
    python pdf_compressor_gui.py   # graphical version with a slider (one file at a time)

Batch mode (--zip): each input becomes <name>_compressed.pdf inside the
zip (duplicate basenames get _1, _2 suffixes). Files that cannot be
shrunk are included unchanged, never larger. The GUI compresses one file
at a time; batch mode is command-line only, but the GUI shares the same
compress_file(), so it benefits from the same safety rules.

Deep mode (--deep, opt-in, off by default)
-------------------------------------------
For vector-heavy engineering PDFs (CAD exports, cross-sections, foundation
plans — typically produced by Pdftools and friends) the lossless pass
leaves money on the table: the drawing instructions carry far more decimal
places than the drawing needs. Deep mode shortens those numbers before
Zopfli, using the exact rules proven on a 114-page Phase Two
cross-sections PDF (11,818,833 -> 9,508,378 bytes, 19.55%, with extracted
text identical on all 114 pages):

- translations and path coordinates (cm/Tm/Td/TD, m/l/c/v/y/re) to
  1 decimal place (max shift 0.05 pt = 0.018 mm);
- colours (rg/RG/g/G/k/K/sc/SC/scn/SCN) to 2 decimals, ROUND_HALF_UP
  (so 0.995 -> 1);
- cm/Tm scale/rotation: ONLY the exact value 0.479969 -> 0.48
  (and -0.479969 -> -0.48). Every other scale/rotation value is left
  untouched — rounding e.g. 0.479379 -> 0.479 moves far points by ~1 pt
  because shapes live in a ~0.48-scaled frame with coordinates up to
  ~3000. A naive "round all decimals to 2dp" changed ~20% of rendered
  pixels and was rejected;
- NEVER touch Tc/Tw/Tz/TL/Tf/Ts/TJ/'/" numbers (per-glyph spacing
  accumulates along a text line);
- literal strings (...) and hex strings <...> are copied verbatim, so
  every printed number and table value stays identical;
- any content stream containing a BI (inline image) operator is left
  completely untouched.

Because shortening is not risk-free, deep mode verifies itself: it builds
both a lossless and a deep candidate, then checks the deep candidate
against the original (page count, page sizes/rotations, extracted text,
and a render comparison of up to 5 pages at 72 DPI — fails if more than
8% of pixels differ by >10 levels). The deep result is kept only if
verification passes AND it is smaller than the lossless result; otherwise
the lossless result is delivered and the stats report
deep_fallback=True with the reason. The render check needs PyMuPDF
(optional dependency); without it the text check falls back to comparing
content-stream string tokens and the render check is skipped.

Deep mode is slow (about twice the lossless time, because it runs Zopfli
twice) and only helps vector-heavy PDFs. On scanned/image-heavy PDFs it
finds nothing to shorten and falls back to the lossless result.

Requirements: pikepdf, Pillow, zopfli  (see requirements.txt)
PyMuPDF is optional: it enables the render check in --deep mode.
The GUI additionally needs tkinter (included with most Python installs;
on Debian/Ubuntu: sudo apt install python3-tk).
"""

import argparse
import io
import os
import shutil
import sys
import tempfile
import zipfile
from collections import Counter

# ---------------------------------------------------------------------------
# Dependency check.
#
# The natural way to run this tool is `python pdf_compressor.py ...`, but
# the system python3 on many machines has none of the required packages,
# and the raw ModuleNotFoundError tells the user nothing useful. So the
# imports below are guarded: _check_deps() raises a RuntimeError with
# install instructions instead, and main() prints it cleanly (exit 2).
# The GUI catches the same error and shows it in a message box.
# ---------------------------------------------------------------------------
_MISSING_DEPS = None
try:
    import pikepdf
    from PIL import Image
    import zopfli.zlib
except ImportError as _import_error:  # pragma: no cover - missing deps
    _MISSING_DEPS = _import_error
    pikepdf = None
    Image = None
    zopfli = None


def _check_deps():
    """Raise a helpful error if a required package is missing."""
    if _MISSING_DEPS is not None:
        missing = getattr(_MISSING_DEPS, "name", None) or str(_MISSING_DEPS)
        raise RuntimeError(
            f"Missing required package: {missing}.\n"
            "Install the dependencies first:\n"
            "    python -m pip install -r requirements.txt\n"
            "If you ran this with the system python3, use a virtual "
            "environment instead:\n"
            "    python -m venv pdfvenv\n"
            "    pdfvenv/bin/python -m pip install -r requirements.txt\n"
            "    pdfvenv/bin/python pdf_compressor.py input.pdf output.pdf"
        )


try:
    import fitz  # PyMuPDF — optional, enables the render check in --deep mode
except ImportError:
    fitz = None

try:
    from PIL import ImageChops
except ImportError:  # pragma: no cover - Pillow always present with Image
    ImageChops = None

import vector_shorten


def compression_percent_to_settings(percent):
    """Map a 0-100 slider value to (downscale_enabled, image_scale, jpeg_quality).

    - 0%   -> (False, 1.0, 95)  lossless only, images untouched
    - 50%  -> (True, 0.5, 50)   balanced, the tested default
    - 100% -> (True, 0.2, 25)   maximum shrink

    Scale falls linearly from 1.0 to 0.2 and quality from 95 to 25, both
    capped at the maximum end so the slider never produces invalid values.
    """
    percent = max(0, min(100, int(percent)))
    if percent == 0:
        return False, 1.0, 95
    scale = max(0.2, 1.0 - percent / 100.0)
    quality = int(max(25, 95 - percent * 0.9))
    return True, scale, quality


def downscale_images(pdf, scale=0.5, quality=50, min_pixels=50000, verbose=True):
    """Downscale large raster images in place. Returns (count, bytes_saved).

    Safety rules (November 2026 update):

    - An image is replaced only when the new bytes are strictly smaller
      than the original stored bytes. Tiny template graphics (seals,
      banners, logos) that would grow as JPEG are left untouched.
    - A 1-bit black-and-white scan is never converted to JPEG. It is
      downscaled, thresholded back to 1-bit, and re-saved as Flate, with
      its /Decode preserved. The threshold is flipped when the original
      /Decode is inverted ([1 0], the usual scanner convention), because
      Pillow's "1" packing maps white -> sample 1 while such PDFs use
      sample 1 = black ink; writing Pillow bits unchanged would invert
      the whole page. Thresholding uses dither=NONE so no dither noise
      is added to already-thresholded linework.
    - CMYK images are left untouched: JPEG CMYK handling risks colour
      inversion in PDF readers, and such images are rare here.
    - Transparency masks (/SMask) follow the same smaller-check rule.
    - Image masks (/ImageMask) are left untouched.
    """
    seen = set()
    saved = 0
    count = 0

    for page in pdf.pages:
        try:
            resources = page.get("/Resources")
            if not resources:
                continue
            xobjects = resources.get("/XObject")
            if not xobjects:
                continue
            for _name, obj in list(xobjects.items()):
                try:
                    objgen = obj.objgen
                except Exception:
                    continue
                if objgen in seen:
                    continue
                seen.add(objgen)
                if obj.get("/Subtype") != "/Image":
                    continue
                if obj.get("/ImageMask", False):
                    continue
                try:
                    width = int(obj.get("/Width"))
                    height = int(obj.get("/Height"))
                except Exception:
                    continue
                if width * height < min_pixels:
                    continue
                try:
                    bits = int(obj.get("/BitsPerComponent", 8))
                except Exception:
                    bits = 8
                try:
                    old_len = len(obj.get_raw_stream_buffer())
                    image = pikepdf.PdfImage(obj)
                    pil_image = image.as_pil_image()
                    new_w = max(1, int(width * scale))
                    new_h = max(1, int(height * scale))
                    if bits == 1:
                        # 1-bit scan: keep it 1-bit sharp, never JPEG.
                        decode = obj.get("/Decode", None)
                        inverted_decode = False
                        try:
                            if decode is not None and len(decode) >= 2:
                                if float(decode[0]) > float(decode[1]):
                                    inverted_decode = True
                        except Exception:
                            inverted_decode = False
                        if pil_image.mode != "L":
                            pil_image = pil_image.convert("L")
                        resized = pil_image.resize((new_w, new_h), Image.LANCZOS)
                        if inverted_decode:
                            bw = resized.point(
                                lambda p: 0 if p > 128 else 255
                            ).convert("1", dither=Image.NONE)
                        else:
                            bw = resized.point(
                                lambda p: 255 if p > 128 else 0
                            ).convert("1", dither=Image.NONE)
                        packed = bw.tobytes()
                        new_data = zopfli.zlib.compress(packed, numiterations=5)
                        if len(new_data) >= old_len:
                            if verbose:
                                print(
                                    f"  1-bit {width}x{height} -> {new_w}x{new_h}: "
                                    f"new {len(new_data)} >= old {old_len}, "
                                    "kept original",
                                    flush=True,
                                )
                            continue
                        obj.write(new_data, filter=None)
                        obj["/Filter"] = pikepdf.Name("/FlateDecode")
                        obj["/Width"] = new_w
                        obj["/Height"] = new_h
                        if "/DecodeParms" in obj:
                            del obj["/DecodeParms"]
                        saved += old_len - len(new_data)
                        count += 1
                        if verbose:
                            print(
                                f"  1-bit {width}x{height} -> {new_w}x{new_h}, "
                                f"saved {old_len - len(new_data)}",
                                flush=True,
                            )
                    else:
                        if pil_image.mode == "CMYK":
                            if verbose:
                                print(
                                    "  Skip CMYK image (colour safety)",
                                    flush=True,
                                )
                            continue
                        if pil_image.mode in ("RGBA", "LA", "P"):
                            pil_image = pil_image.convert("RGB")
                        resized = pil_image.resize((new_w, new_h), Image.LANCZOS)
                        buf = io.BytesIO()
                        resized.save(buf, format="JPEG", quality=quality, optimize=True)
                        new_data = buf.getvalue()
                        if len(new_data) >= old_len:
                            if verbose:
                                print(
                                    f"  Image {width}x{height} -> {new_w}x{new_h}: "
                                    f"new {len(new_data)} >= old {old_len}, "
                                    "kept original",
                                    flush=True,
                                )
                            continue
                        # Write JPEG bytes raw and set /Filter manually, so pikepdf
                        # does not recompress them with its own JPEG encoder.
                        obj.write(new_data, filter=None)
                        obj["/Filter"] = pikepdf.Name("/DCTDecode")
                        obj["/Width"] = new_w
                        obj["/Height"] = new_h
                        if "/DecodeParms" in obj:
                            del obj["/DecodeParms"]

                        # Same smaller-check treatment for the transparency
                        # mask, if any.
                        smask = obj.get("/SMask", None)
                        if smask is not None:
                            try:
                                sw = int(smask.get("/Width"))
                                sh = int(smask.get("/Height"))
                                smask_image = pikepdf.PdfImage(smask)
                                smask_pil = smask_image.as_pil_image()
                                if smask_pil.mode != "L":
                                    smask_pil = smask_pil.convert("L")
                                new_sw = max(1, int(sw * scale))
                                new_sh = max(1, int(sh * scale))
                                smask_resized = smask_pil.resize(
                                    (new_sw, new_sh), Image.LANCZOS
                                )
                                sbuf = io.BytesIO()
                                smask_resized.save(
                                    sbuf, format="JPEG", quality=quality, optimize=True
                                )
                                smask_data = sbuf.getvalue()
                                smask_old = len(smask.get_raw_stream_buffer())
                                if len(smask_data) < smask_old:
                                    smask.write(smask_data, filter=None)
                                    smask["/Filter"] = pikepdf.Name("/DCTDecode")
                                    smask["/Width"] = new_sw
                                    smask["/Height"] = new_sh
                                    if "/DecodeParms" in smask:
                                        del smask["/DecodeParms"]
                                    saved += smask_old - len(smask_data)
                            except Exception as exc:  # keep going if a mask fails
                                if verbose:
                                    print(f"  SMask error: {exc}", flush=True)

                        saved += old_len - len(new_data)
                        count += 1
                        if verbose:
                            print(
                                f"  Image {width}x{height} -> {new_w}x{new_h}, "
                                f"saved {old_len - len(new_data)}",
                                flush=True,
                            )
                except Exception as exc:
                    if verbose:
                        print(f"  Image error: {exc}", flush=True)
        except Exception as exc:
            if verbose:
                print(f"  Page error: {exc}", flush=True)

    return count, saved


def remove_nonvisible_data(pdf):
    """Remove /VP measurement viewports and /LastModified stamps from pages."""
    for page in pdf.pages:
        if "/VP" in page:
            del page["/VP"]
        if "/LastModified" in page:
            del page["/LastModified"]
        # Seen as a typo in some producers; harmless to remove as well.
        if "/LastModfied" in page:
            del page["/LastModfied"]


def zopfli_recompress(pdf, iterations=5, verbose=True):
    """Recompress every Flate-only stream with Zopfli.

    Returns (stream_count, bytes_saved).
    """
    total_saved = 0
    stream_count = 0

    for obj in pdf.objects:
        try:
            try:
                raw = obj.get_raw_stream_buffer()
            except Exception:
                continue  # not a stream
            filter_value = str(obj.get("/Filter", None))
            if "FlateDecode" not in filter_value:
                continue
            if "DCTDecode" in filter_value:
                continue  # JPEG image, handled elsewhere
            try:
                # Fully decoded bytes (predictor already applied by pikepdf).
                decompressed = obj.read_bytes()
            except Exception:
                continue
            compressed = zopfli.zlib.compress(decompressed, numiterations=iterations)
            old_len = len(raw)
            if len(compressed) < old_len:
                # Write Zopfli bytes raw and set /Filter manually, then drop
                # /DecodeParms: the bytes we compressed are already
                # predictor-decoded, so a viewer must NOT apply a predictor
                # again when it inflates them.
                obj.write(compressed, filter=None)
                obj["/Filter"] = pikepdf.Name("/FlateDecode")
                if "/DecodeParms" in obj:
                    del obj["/DecodeParms"]
                total_saved += old_len - len(compressed)
                stream_count += 1
                if verbose and stream_count % 10 == 0:
                    print(
                        f"  Zopfli: {stream_count} streams, "
                        f"saved {total_saved} bytes so far",
                        flush=True,
                    )
        except Exception:
            continue

    return stream_count, total_saved


def verify_page_count(input_path, output_path):
    """Return (input_pages, output_pages) using pikepdf only."""
    with pikepdf.open(input_path) as src:
        in_pages = len(src.pages)
    with pikepdf.open(output_path) as dst:
        out_pages = len(dst.pages)
    return in_pages, out_pages


# Render-check gate for --deep mode. On the proven Phase Two run the safe
# shortening changed ~2-3% of pixels by >10 levels at 72 DPI (sub-pixel
# anti-aliasing only), while the rejected "round everything to 2dp" run
# changed ~20%. 8% sits well above the expected range and well below the
# range that means real damage.
RENDER_FAIL_THRESHOLD = 0.08
RENDER_SAMPLE_PAGES = 5


def _page_sizes(pdf):
    """[(width, height, rotation), ...] for every page, via pikepdf."""
    sizes = []
    for page in pdf.pages:
        box = page.get("/MediaBox")
        width = float(box[2]) - float(box[0])
        height = float(box[3]) - float(box[1])
        sizes.append((width, height, int(page.get("/Rotate", 0))))
    return sizes


def _text_fingerprint_fitz(path):
    """Per-page (word Counter, alphanumeric char count) using PyMuPDF."""
    pages = []
    with fitz.open(path) as doc:
        for page in doc:
            words = Counter(w[4] for w in page.get_text("words"))
            text = page.get_text()
            alnum = sum(1 for ch in text if ch.isalnum())
            pages.append((words, alnum))
    return pages


def _text_fingerprint_tokens(path):
    """Fallback text check without PyMuPDF: per-page Counter of the
    literal-string and hex-string tokens in the content streams.

    Deep mode copies strings verbatim, so these multisets must be
    identical between input and output. (This checks that no text was
    touched; it is not a full text extraction.)
    """
    pages = []
    with pikepdf.open(path) as pdf:
        for page in pdf.pages:
            counter = Counter()
            contents = page.get("/Contents")
            if contents is not None:
                streams = (
                    list(contents)
                    if isinstance(contents, pikepdf.Array)
                    else [contents]
                )
                for stream in streams:
                    try:
                        data = stream.read_bytes()
                    except Exception:
                        continue
                    for kind, tok in vector_shorten.tokens(data):
                        if kind in ("string", "hex"):
                            counter[tok] += 1
            pages.append(counter)
    return pages


def _render_diff_fraction(input_path, output_path, page_index, dpi=72):
    """Fraction of pixels differing by >10 (in any RGB channel) between
    the same page rendered from both files. Returns None if rendering
    is unavailable (PyMuPDF or Pillow missing)."""
    if fitz is None or ImageChops is None:
        return None
    with fitz.open(input_path) as doc_in, fitz.open(output_path) as doc_out:
        pix_in = doc_in[page_index].get_pixmap(dpi=dpi, alpha=False)
        pix_out = doc_out[page_index].get_pixmap(dpi=dpi, alpha=False)
    img_in = Image.frombytes("RGB", (pix_in.width, pix_in.height), pix_in.samples)
    img_out = Image.frombytes("RGB", (pix_out.width, pix_out.height), pix_out.samples)
    if img_in.size != img_out.size:
        return 1.0  # page size changed: treat as total failure
    diff = ImageChops.difference(img_in, img_out)
    hist = diff.histogram()
    total = img_in.width * img_in.height
    worst = 0.0
    for channel in range(3):
        channel_hist = hist[channel * 256:(channel + 1) * 256]
        worst = max(worst, sum(channel_hist[11:]) / total)
    return worst


def verify_output(input_path, output_path, verbose=True):
    """Verify a compressed output against its input.

    Checks, in order:
    1. page count unchanged;
    2. page sizes and rotations unchanged;
    3. extracted text identical (per-page word multisets + alphanumeric
       char count with PyMuPDF; string-token multisets without it);
    4. render comparison on up to 5 evenly spaced pages at 72 DPI
       (PyMuPDF only) — fails if more than RENDER_FAIL_THRESHOLD of
       pixels differ by >10 levels in any channel.

    Returns a dict with "ok" plus per-check results. "render_ok" is None
    when the render check was skipped (PyMuPDF missing), in which case
    the text and page checks still gate the result.
    """
    result = {
        "ok": False,
        "reason": "",
        "pages_ok": False,
        "sizes_ok": False,
        "text_ok": False,
        "text_method": None,
        "render_ok": None,
        "render_max_diff": None,
        "render_pages_checked": 0,
    }

    in_pages, out_pages = verify_page_count(input_path, output_path)
    result["pages_ok"] = in_pages == out_pages
    if not result["pages_ok"]:
        result["reason"] = f"page count changed {in_pages} -> {out_pages}"
        return result

    with pikepdf.open(input_path) as src, pikepdf.open(output_path) as dst:
        result["sizes_ok"] = _page_sizes(src) == _page_sizes(dst)
    if not result["sizes_ok"]:
        result["reason"] = "page size or rotation changed"
        return result

    if fitz is not None:
        result["text_method"] = "PyMuPDF word multisets"
        in_text = _text_fingerprint_fitz(input_path)
        out_text = _text_fingerprint_fitz(output_path)
    else:
        result["text_method"] = "content-stream string tokens (PyMuPDF not installed)"
        in_text = _text_fingerprint_tokens(input_path)
        out_text = _text_fingerprint_tokens(output_path)
        if verbose:
            print(
                "  Note: PyMuPDF not installed — text check uses content-stream "
                "string tokens, render check skipped. "
                "Install PyMuPDF for the full check: pip install PyMuPDF",
                flush=True,
            )
    result["text_ok"] = in_text == out_text
    if not result["text_ok"]:
        result["reason"] = "extracted text differs"
        return result

    if fitz is not None and ImageChops is not None:
        n = in_pages
        if n <= RENDER_SAMPLE_PAGES:
            sample = list(range(n))
        else:
            sample = sorted(
                {0, n // 4, n // 2, (3 * n) // 4, n - 1}
            )
        worst = 0.0
        for idx in sample:
            frac = _render_diff_fraction(input_path, output_path, idx)
            if frac is None:
                break
            worst = max(worst, frac)
            if verbose:
                print(
                    f"  Render check page {idx + 1}/{n}: "
                    f"{frac * 100:.2f}% pixels differ >10",
                    flush=True,
                )
        result["render_max_diff"] = worst
        result["render_pages_checked"] = len(sample)
        result["render_ok"] = worst <= RENDER_FAIL_THRESHOLD
        if not result["render_ok"]:
            result["reason"] = (
                f"render differs too much "
                f"({worst * 100:.2f}% pixels > {RENDER_FAIL_THRESHOLD * 100:.0f}% limit)"
            )
            return result

    result["ok"] = True
    return result


def _run_pipeline(
    input_path,
    output_path,
    image_scale=0.5,
    image_quality=50,
    min_image_pixels=50000,
    downscale_enabled=True,
    keep_vp=False,
    zopfli_iterations=5,
    deep=False,
    verbose=True,
    label="",
):
    """One full compression pass: open, downscale, VP removal, optional
    deep vector shortening, Zopfli, save. Returns a stats dict.

    This does NOT apply the never-bigger fallback — the caller decides
    what to do with the result (deep mode needs both the lossless and
    the deep candidate before choosing).
    """
    if label:
        print(f"{label}", flush=True)
    original_size = os.path.getsize(input_path)
    pdf = pikepdf.open(input_path)

    if downscale_enabled:
        print("Step 1: Downscaling images...", flush=True)
        count, saved = downscale_images(
            pdf,
            scale=image_scale,
            quality=image_quality,
            min_pixels=min_image_pixels,
            verbose=verbose,
        )
        print(f"Images: replaced {count}, saved ~{saved} bytes raw", flush=True)
    else:
        print("Step 1: Skipped (lossless mode, no image downscaling)", flush=True)
        count, saved = 0, 0

    if not keep_vp:
        print("Step 2: Removing VP/LastModified...", flush=True)
        remove_nonvisible_data(pdf)
    else:
        print("Step 2: Skipped (keeping VP data)", flush=True)

    deep_stats = {"streams_changed": 0, "streams_skipped_bi": 0,
                  "decoded_bytes_trimmed": 0}
    if deep:
        print(
            "Step 2b: Deep vector shortening "
            "(safe number rounding, see vector_shorten.py)...",
            flush=True,
        )
        deep_stats = vector_shorten.shorten_content_streams(pdf, verbose=verbose)

    print(
        "Step 3: Zopfli-compressing Flate streams "
        "(this takes a couple minutes on large files)...",
        flush=True,
    )
    stream_count, zopfli_saved = zopfli_recompress(
        pdf, iterations=zopfli_iterations, verbose=verbose
    )
    print(
        f"Zopfli: {stream_count} streams recompressed, "
        f"saved ~{zopfli_saved} bytes",
        flush=True,
    )

    print("Saving...", flush=True)
    # recompress_flate=False is essential: it preserves our Zopfli bytes.
    pdf.save(
        output_path,
        compress_streams=True,
        object_stream_mode=pikepdf.ObjectStreamMode.generate,
        recompress_flate=False,
    )
    pdf.close()

    new_size = os.path.getsize(output_path)
    in_pages, out_pages = verify_page_count(input_path, output_path)
    stats = {
        "original_size": original_size,
        "new_size": new_size,
        "reduction_percent": (
            (1 - new_size / original_size) * 100 if original_size else 0
        ),
        "saved_bytes": original_size - new_size,
        "in_pages": in_pages,
        "out_pages": out_pages,
        "image_count": count,
        "image_saved": saved,
        "stream_count": stream_count,
        "zopfli_saved": zopfli_saved,
        "deep": deep,
        "deep_streams_changed": deep_stats["streams_changed"],
        "deep_streams_skipped_bi": deep_stats["streams_skipped_bi"],
        "deep_decoded_bytes_trimmed": deep_stats["decoded_bytes_trimmed"],
        "kept_original": False,
    }
    return stats


def _never_bigger(input_path, output_path, stats, verbose=True):
    """Never-bigger fallback: if the output grew, copy the original
    instead and adjust the stats. Returns the (possibly updated) stats."""
    if stats["new_size"] >= stats["original_size"]:
        shutil.copyfile(input_path, output_path)
        stats = dict(stats)
        stats["new_size"] = stats["original_size"]
        stats["reduction_percent"] = 0.0
        stats["saved_bytes"] = 0
        stats["kept_original"] = True
        if verbose:
            print(
                "Kept original (compressed version was not smaller)",
                flush=True,
            )
    return stats


def compress_file(
    input_path,
    output_path,
    image_scale=0.5,
    image_quality=50,
    min_image_pixels=50000,
    downscale_enabled=True,
    keep_vp=False,
    zopfli_iterations=5,
    deep=False,
    verbose=True,
):
    """Run all compression steps. Returns a dict of stats.

    Raises FileNotFoundError / ValueError for bad inputs, and propagates
    pikepdf errors. On success, output_path exists and page counts match
    (checked by the caller via verify_page_count if desired).
    The output is never bigger than the input: if compression would grow
    the file, the original is copied to output_path instead and the
    returned dict has kept_original=True with 0% reduction.

    deep=True runs the opt-in "deep" mode: the pipeline is run twice
    (once lossless, once with safe vector-number shortening), the deep
    candidate is verified against the input (page count, page sizes,
    extracted text, render comparison), and the deep result is kept
    only if verification passes AND it is smaller than the lossless
    result. Otherwise the lossless result is delivered instead and the
    returned dict has deep_fallback=True with the reason. See
    vector_shorten.py for the exact shortening rules.
    """
    _check_deps()
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")
    if os.path.abspath(input_path) == os.path.abspath(output_path):
        raise ValueError("Output must be a different file from input.")

    original_size = os.path.getsize(input_path)
    print(f"Original: {original_size} bytes", flush=True)

    if not deep:
        stats = _run_pipeline(
            input_path,
            output_path,
            image_scale=image_scale,
            image_quality=image_quality,
            min_image_pixels=min_image_pixels,
            downscale_enabled=downscale_enabled,
            keep_vp=keep_vp,
            zopfli_iterations=zopfli_iterations,
            deep=False,
            verbose=verbose,
        )
        stats["deep_fallback"] = False
        stats["deep_fallback_reason"] = ""
        stats["deep_verification"] = None
        stats = _never_bigger(input_path, output_path, stats, verbose=verbose)
    else:
        print(
            "Deep mode ON: building both a lossless and a deep candidate, "
            "then verifying the deep one (this is slow — about twice the "
            "lossless time).",
            flush=True,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            lossless_path = os.path.join(tmpdir, "lossless.pdf")
            lossless = _run_pipeline(
                input_path,
                lossless_path,
                image_scale=image_scale,
                image_quality=image_quality,
                min_image_pixels=min_image_pixels,
                downscale_enabled=downscale_enabled,
                keep_vp=keep_vp,
                zopfli_iterations=zopfli_iterations,
                deep=False,
                verbose=verbose,
                label="Pass 1/2 (lossless candidate):",
            )
            print(
                f"Lossless candidate: {lossless['new_size']} bytes",
                flush=True,
            )
            deep_stats = _run_pipeline(
                input_path,
                output_path,
                image_scale=image_scale,
                image_quality=image_quality,
                min_image_pixels=min_image_pixels,
                downscale_enabled=downscale_enabled,
                keep_vp=keep_vp,
                zopfli_iterations=zopfli_iterations,
                deep=True,
                verbose=verbose,
                label="Pass 2/2 (deep candidate):",
            )
            print(
                f"Deep candidate: {deep_stats['new_size']} bytes",
                flush=True,
            )
            print("Verifying deep candidate against the original...", flush=True)
            verification = verify_output(input_path, output_path, verbose=verbose)
            fallback_reason = ""
            if not verification["ok"]:
                fallback_reason = (
                    f"deep verification failed: {verification['reason']}"
                )
            elif deep_stats["new_size"] >= lossless["new_size"]:
                fallback_reason = (
                    f"deep result ({deep_stats['new_size']} bytes) not smaller "
                    f"than lossless ({lossless['new_size']} bytes)"
                )
            if fallback_reason:
                print(
                    f"Deep mode fell back to lossless: {fallback_reason}",
                    flush=True,
                )
                shutil.copyfile(lossless_path, output_path)
                stats = dict(lossless)
                stats["deep"] = True
                stats["deep_fallback"] = True
                stats["deep_fallback_reason"] = fallback_reason
                stats["deep_verification"] = verification
                stats["lossless_size"] = lossless["new_size"]
            else:
                print(
                    "Deep verification passed "
                    f"(text: {verification['text_method']}; "
                    + (
                        f"render max diff "
                        f"{verification['render_max_diff'] * 100:.2f}% on "
                        f"{verification['render_pages_checked']} page(s)"
                        if verification["render_max_diff"] is not None
                        else "render check skipped (PyMuPDF not installed)"
                    )
                    + "). Keeping deep result.",
                    flush=True,
                )
                stats = dict(deep_stats)
                stats["deep_fallback"] = False
                stats["deep_fallback_reason"] = ""
                stats["deep_verification"] = verification
                stats["lossless_size"] = lossless["new_size"]
        stats = _never_bigger(input_path, output_path, stats, verbose=verbose)

    print(f"Done! Output: {output_path}", flush=True)
    print(f"Original: {stats['original_size']} bytes", flush=True)
    print(f"New: {stats['new_size']} bytes", flush=True)
    print(f"Reduction: {stats['reduction_percent']:.2f}%", flush=True)
    print(f"Saved: {stats['saved_bytes']} bytes", flush=True)

    print(
        f"Pages: {stats['in_pages']} -> {stats['out_pages']} "
        f"({'OK' if stats['in_pages'] == stats['out_pages'] else 'MISMATCH!'})",
        flush=True,
    )
    if stats.get("deep_fallback"):
        print(
            f"Deep mode: fell back to lossless "
            f"({stats['deep_fallback_reason']})",
            flush=True,
        )

    return stats


def compress_batch(
    input_paths,
    zip_path,
    image_scale=0.5,
    image_quality=50,
    min_image_pixels=50000,
    downscale_enabled=True,
    keep_vp=False,
    zopfli_iterations=5,
    deep=False,
    verbose=True,
):
    """Compress several PDFs and pack the results into one zip file.

    Each input becomes <basename>_compressed.pdf inside the zip
    (duplicate basenames get _1, _2 suffixes so extraction never
    overwrites a sibling). Files are processed one by one, in order,
    with the same settings; a file that fails (missing, unreadable, or
    page-count mismatch after compression) is skipped and recorded in
    "failures", while the other files still go into the zip. Files
    that cannot be shrunk are included unchanged (never-bigger
    fallback in compress_file, reported with kept_original=True).

    Returns a dict with per-file stats ("files"), "failures",
    totals, and "zip_size" (0 and zip_path=None when no file
    succeeded, in which case no zip is written).
    """
    if not input_paths:
        raise ValueError("No input files given for batch mode.")
    _check_deps()

    # Deduplicate output names inside the zip.
    used_names = set()
    planned = []
    for input_path in input_paths:
        base = os.path.splitext(os.path.basename(input_path))[0]
        output_name = base + "_compressed.pdf"
        if output_name in used_names:
            stem = base + "_compressed"
            i = 1
            while f"{stem}_{i}.pdf" in used_names:
                i += 1
            output_name = f"{stem}_{i}.pdf"
        used_names.add(output_name)
        planned.append((input_path, output_name))

    results = []
    failures = []
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_files = []
        for input_path, output_name in planned:
            temp_path = os.path.join(tmpdir, output_name)
            try:
                stats = compress_file(
                    input_path,
                    temp_path,
                    image_scale=image_scale,
                    image_quality=image_quality,
                    min_image_pixels=min_image_pixels,
                    downscale_enabled=downscale_enabled,
                    keep_vp=keep_vp,
                    zopfli_iterations=zopfli_iterations,
                    deep=deep,
                    verbose=verbose,
                )
            except Exception as exc:
                print(f"Error compressing {input_path}: {exc}", flush=True)
                failures.append({"input": input_path, "error": str(exc)})
                continue
            if stats["in_pages"] != stats["out_pages"]:
                print(
                    f"Error compressing {input_path}: page count mismatch "
                    f"{stats['in_pages']} -> {stats['out_pages']}, "
                    "skipped from zip",
                    flush=True,
                )
                failures.append(
                    {
                        "input": input_path,
                        "error": (
                            f"Page count mismatch "
                            f"{stats['in_pages']} -> {stats['out_pages']}"
                        ),
                    }
                )
                continue
            stats = dict(stats)
            stats["input"] = input_path
            stats["output_name"] = output_name
            results.append(stats)
            temp_files.append((output_name, temp_path))
            print(
                f"RESULT {input_path}: {stats['original_size']} -> "
                f"{stats['new_size']} ({stats['reduction_percent']:.2f}%), "
                f"pages {stats['in_pages']} -> {stats['out_pages']}, "
                f"kept_original={stats['kept_original']}"
                + (
                    f", deep_fallback ({stats['deep_fallback_reason']})"
                    if stats.get("deep_fallback")
                    else ""
                ),
                flush=True,
            )
        zip_size = 0
        if temp_files:
            with zipfile.ZipFile(
                zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as zf:
                for output_name, temp_path in temp_files:
                    zf.write(temp_path, arcname=output_name)
            zip_size = os.path.getsize(zip_path)

    total_original = sum(r["original_size"] for r in results)
    total_new = sum(r["new_size"] for r in results)
    total_reduction = (
        (1 - total_new / total_original) * 100 if total_original else 0
    )
    print(
        f"Batch total: {total_original} -> {total_new} "
        f"({total_reduction:.2f}%)",
        flush=True,
    )
    if temp_files:
        print(
            f"Zip: {zip_path} ({zip_size} bytes, {len(temp_files)} files)",
            flush=True,
        )
    else:
        print("No files compressed; no zip written.", flush=True)
    if failures:
        print(f"Failures: {len(failures)} file(s) failed (see above).", flush=True)

    return {
        "files": results,
        "failures": failures,
        "total_original": total_original,
        "total_new": total_new,
        "total_saved": total_original - total_new,
        "total_reduction_percent": total_reduction,
        "zip_path": zip_path if temp_files else None,
        "zip_size": zip_size,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Shrink an already-optimized PDF with Zopfli recompression "
        "and optional image downscaling. Use --zip for batch mode."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Input PDF file(s). Single-file mode: input.pdf output.pdf. "
        "Batch mode: input1.pdf input2.pdf ... plus --zip out.zip "
        "(with --zip, every positional is treated as an input PDF).",
    )
    parser.add_argument(
        "--zip",
        dest="zip_path",
        type=str,
        default=None,
        help="Batch mode: write all compressed PDFs into this zip file "
        "(e.g. compressed.zip). Give only input PDFs positionally, "
        "no output PDF.",
    )
    parser.add_argument(
        "--compression-percent",
        type=int,
        default=None,
        help="Slider value 0-100 (overrides --image-scale/--image-quality). "
        "0 = lossless only, 50 = 0.5x / JPEG 50 (balanced, recommended), "
        "100 = 0.2x / JPEG 25 (maximum shrink).",
    )
    parser.add_argument(
        "--image-scale",
        type=float,
        default=0.5,
        help="Scale factor for large images (default: 0.5, i.e. half resolution)",
    )
    parser.add_argument(
        "--image-quality",
        type=int,
        default=50,
        help="JPEG quality for downscaled images (default: 50)",
    )
    parser.add_argument(
        "--min-image-pixels",
        type=int,
        default=50000,
        help="Only downscale images with at least this many pixels "
        "(default: 50000; smaller icons are left alone)",
    )
    parser.add_argument(
        "--no-image-downscale",
        action="store_true",
        help="Skip image downscaling (fully lossless: Zopfli + metadata only)",
    )
    parser.add_argument(
        "--keep-vp",
        action="store_true",
        help="Keep /VP measurement viewports (default: remove them)",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Deep vector mode (opt-in, slow): safely shorten numbers in "
        "vector drawing instructions (coordinates to 1 decimal, colours "
        "to 2 decimals, exact 0.479969 -> 0.48 scale only), then verify "
        "the result (page count, page sizes, extracted text, render "
        "comparison) and fall back to the lossless result if verification "
        "fails or the deep result is not smaller. Best for vector-heavy "
        "engineering PDFs; does nothing extra for scanned/image PDFs.",
    )
    parser.add_argument(
        "--zopfli-iterations",
        type=int,
        default=5,
        help="Zopfli iterations per stream (default: 5; 15 saves only a few "
        "dozen extra bytes per large stream and is much slower)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print the final summary",
    )
    args = parser.parse_args(argv)
    verbose = not args.quiet

    try:
        _check_deps()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    # Slider mapping takes precedence over manual scale/quality.
    downscale_enabled = not args.no_image_downscale
    image_scale = args.image_scale
    image_quality = args.image_quality
    if args.compression_percent is not None:
        downscale_enabled, image_scale, image_quality = (
            compression_percent_to_settings(args.compression_percent)
        )
        print(
            f"Compression slider {args.compression_percent}% -> "
            f"{'lossless only' if not downscale_enabled else f'images {image_scale}x, JPEG {image_quality}'}",
            flush=True,
        )

    # Batch mode: with --zip, every positional is an input PDF.
    if args.zip_path is not None:
        try:
            batch = compress_batch(
                args.inputs,
                args.zip_path,
                image_scale=image_scale,
                image_quality=image_quality,
                min_image_pixels=args.min_image_pixels,
                downscale_enabled=downscale_enabled,
                keep_vp=args.keep_vp,
                zopfli_iterations=args.zopfli_iterations,
                deep=args.deep,
                verbose=verbose,
            )
        except (OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        return 2 if batch["failures"] else 0

    # Single-file mode: exactly two positionals (input, output).
    if len(args.inputs) != 2:
        if len(args.inputs) < 2:
            print(
                "Error: missing output PDF. Give input.pdf output.pdf, "
                "or use --zip for batch mode.",
                file=sys.stderr,
            )
        else:
            print(
                "Error: single-file mode takes exactly one input PDF "
                "and one output PDF. Give multiple inputs only with --zip.",
                file=sys.stderr,
            )
        return 2
    input_path, output_path = args.inputs

    try:
        stats = compress_file(
            input_path,
            output_path,
            image_scale=image_scale,
            image_quality=image_quality,
            min_image_pixels=args.min_image_pixels,
            downscale_enabled=downscale_enabled,
            keep_vp=args.keep_vp,
            zopfli_iterations=args.zopfli_iterations,
            deep=args.deep,
            verbose=verbose,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if stats["in_pages"] != stats["out_pages"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
