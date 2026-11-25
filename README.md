# pdf-compressor

Shrink an already-optimized PDF without changing how it looks.

Most PDF optimizers (including "Small PDF" presets) already squeeze a file with
standard zlib compression. Running them again usually makes the file *bigger*,
not smaller. This tool goes one step further.

## What it does

1. **Downscales large raster images** (optional, the only lossy step).
   Large map/photo images are halved in resolution (0.5× by default) and
   re-saved as JPEG quality 50. They keep the same display size on the page
   but are slightly less sharp when zoomed far in. Small icons are left alone.
   An image is replaced only when the new bytes are strictly smaller than
   the original stored bytes, so tiny template graphics (seals, banners,
   logos) that would grow as JPEG stay untouched. A 1-bit black-and-white
   scan is never converted to JPEG: it is downscaled, thresholded back to
   1-bit, and re-saved as Flate, so survey linework stays sharp.
   Use `--no-image-downscale` for a fully lossless run.

2. **Recompresses every Flate stream with Zopfli.**
   Zopfli produces standard DEFLATE data (the same format PDF readers already
   understand), but it searches much harder for repeated patterns than zlib.
   It is slow, but it typically saves another 10–20% on content streams that
   zlib already compressed. Text and vector content are preserved exactly.

3. **Removes non-visible data.**
   Measurement viewports (`/VP`) and `/LastModified` stamps are removed from
   pages. They do not affect appearance, text, or print output.

4. **Never returns a bigger file.**
   If compression would grow a file, the original is copied to the output
   path instead, and the result reports 0% reduction for that file.

## Graphical version (with a slider)

```bash
python pdf_compressor_gui.py
```

Pick an input PDF and an output PDF, drag the slider to set compression
strength, and click **Compress**. The window shows the actual reduction
afterwards (original size → new size, percentage, pages).

### Slider mapping

The slider sets *strength*, not a guaranteed final percentage — the actual
reduction depends on the file (see "Real results" below).

| Slider | What happens |
|-------:|--------------|
| 0%  | Lossless only: no image changes, Zopfli + metadata cleanup |
| 20% | Images 0.8×, JPEG 77 (mild) |
| 50% | Images 0.5×, JPEG 50 (balanced, recommended) |
| 80% | Images 0.2×, JPEG 25 (aggressive) |
| 100% | Images 0.2×, JPEG 25 (maximum shrink) |

50% is the tested balanced setting: it achieved 21.14% on a 44-page ESA
report. A vector-only file with no large images maxes out around 17–18%
even at 100%, because there is no image left to shrink.

The GUI needs `tkinter` (included with most Python installs; on
Debian/Ubuntu: `sudo apt install python3-tk`). Compression runs in a
background thread so the window stays responsive; detailed progress goes
to the console.

The GUI also has a **Deep vector mode** checkbox (off by default) — the
same `--deep` mode described below. When it is on, the result line says
whether the deep result was verified and kept, or fell back to the
lossless result and why.

The GUI compresses one file at a time; for several files into a zip, use
the command-line batch mode below. The GUI shares the same `compress_file()`,
so single-file runs benefit from the same smaller-check, 1-bit, and
never-bigger rules.

## Installation

```bash
python -m venv pdfvenv
pdfvenv/bin/python -m pip install -r requirements.txt
pdfvenv/bin/python pdf_compressor.py input.pdf output.pdf
```

Requires Python 3.9+ and:

- `pikepdf`
- `Pillow`
- `zopfli`
- `PyMuPDF` (optional — enables the visual render check in `--deep` mode;
  deep mode still works without it, see below)

If you run the tool with a Python that has none of these installed (for
example the system `python3`), it now stops immediately with a clear
message telling you what to install, instead of a bare
`ModuleNotFoundError` traceback.

## Command-line usage

```bash
# Basic: 20%-style shrink (image downscaling + Zopfli + metadata cleanup)
python pdf_compressor.py input.pdf output.pdf

# Same slider as the GUI, from the command line
python pdf_compressor.py input.pdf output.pdf --compression-percent 50

# Fully lossless: Zopfli + metadata cleanup only, images untouched
python pdf_compressor.py input.pdf output.pdf --no-image-downscale
python pdf_compressor.py input.pdf output.pdf --compression-percent 0

# Deep vector mode (opt-in, slow — see "Deep mode" below)
python pdf_compressor.py input.pdf output.pdf --compression-percent 0 --deep

# More aggressive image shrink
python pdf_compressor.py input.pdf output.pdf --image-scale 0.4 --image-quality 40

# Keep measurement viewports
python pdf_compressor.py input.pdf output.pdf --keep-vp
```

```bash
# Batch: several PDFs into one zip (balanced 50% setting)
python pdf_compressor.py file1.pdf file2.pdf file3.pdf --zip compressed.zip --compression-percent 50
```

Each input becomes `<name>_compressed.pdf` inside the zip (duplicate
basenames get `_1`, `_2` suffixes, so extraction never overwrites a
sibling). When `--zip` is given, every positional argument is treated as
an input PDF (there is no output PDF positional in batch mode). Files
that cannot be shrunk are included unchanged, never larger. If a file
fails, the others still go into the zip and the exit code is 2; if all
files fail, no zip is written. Batch processes files one by one,
sequentially.

`--compression-percent` overrides `--image-scale` / `--image-quality`.
The tool prints the original size, the new size, the percentage reduction,
and a page-count check (`44 -> 44 OK`). In batch mode it prints a
one-line result per file, totals, and the zip size.

## Real results

On two environmental site assessment reports (already saved as "Small PDF"):

| File | Pages | Original | Compressed | Reduction |
|------|------:|--------:|----------:|----------:|
| Phase Two ESA report | 44 | 5,101,112 bytes | 4,022,589 bytes | **21.14%** |
| Cross-sections report | 70 | 6,656,488 bytes | 5,498,499 bytes | **17.40%** |

In both cases the extracted text was byte-identical before and after
(487,259 and 970,422 characters respectively), and the page count, page size,
and rotation were unchanged.

The second file only reached 17.40% because it contains almost no large
images to downscale — it is nearly all vector cross-section lines — and its
vector content was already highly compressed. Reaching 20% on a file like
that would require simplifying the vectors themselves, which risks changing
the technical drawings.

Batch example (November 2026, 8 legal/land documents, balanced 50% setting):

| File | Pages | Original | Compressed | Reduction |
|------|------:|--------:|----------:|----------:|
| PIN 21356-0025 | 2 | 227,736 bytes | 82,057 bytes | **63.97%** |
| PIN 21356-0026 | 2 | 226,987 bytes | 81,629 bytes | **64.04%** |
| PIN 21356-0222 | 3 | 262,907 bytes | 88,868 bytes | **66.20%** |
| RSC Letter | 3 | 234,622 bytes | 176,617 bytes | **24.72%** |
| AT5800460 | 3 | 121,511 bytes | 54,878 bytes | **54.84%** |
| AT5968732 | 3 | 117,727 bytes | 53,434 bytes | **54.61%** |
| AT6463335 | 3 | 122,750 bytes | 54,944 bytes | **55.24%** |
| Legal Survey | 1 | 1,363,113 bytes | 422,811 bytes | **68.98%** |
| Total (extracted) | 20 | 2,677,353 bytes | 1,015,238 bytes | **62.08%** |

The zip file itself was 853,957 bytes (68.10% smaller than the originals),
because zip compression squeezes the remaining uncompressed parts further.
In all 8 files the extracted text was identical before and after, page
counts and page sizes were unchanged, and rendered pages were spot-checked
(including the survey at overview and detail scale, where small text
stayed legible). The survey is a 1-bit scan kept sharp as Flate, not JPEG.

## Deep mode (`--deep`, opt-in)

For vector-heavy engineering PDFs (CAD exports, cross-sections,
foundation plans — typically produced by Pdftools) the lossless pass
leaves money on the table: the drawing instructions carry far more
decimal places than the drawing needs. Deep mode shortens those numbers
before Zopfli, using the exact rules proven on a 114-page Phase Two
cross-sections PDF.

What it changes (and only this):

- translations and path coordinates (`cm`/`Tm`/`Td`/`TD`, `m`/`l`/`c`/
  `v`/`y`/`re`) to 1 decimal place — max shift 0.05 pt (0.018 mm);
- colours (`rg`/`RG`/`g`/`G`/`k`/`K`/`sc`/`SC`/`scn`/`SCN`) to 2
  decimals, rounded half-up (so 0.995 → 1);
- `cm`/`Tm` scale/rotation: **only** the exact value 0.479969 → 0.48
  (and −0.479969 → −0.48). Every other scale/rotation value is left
  untouched. Shapes in these PDFs live in a ~0.48-scaled frame with
  coordinates up to ~3000, so rounding e.g. 0.479379 → 0.479 moves far
  points by ~1 pt. A naive "round all decimals to 2dp" changed ~20% of
  rendered pixels and was rejected;
- **never** touches `Tc`/`Tw`/`Tz`/`TL`/`Tf`/`Ts`/`TJ`/`'`/`"` numbers
  (per-glyph spacing accumulates along a text line);
- literal strings `(...)` and hex strings `<...>` are copied verbatim, so
  every printed number and table value stays identical;
- any content stream containing a `BI` (inline image) operator is left
  completely untouched.

Because shortening is not risk-free, deep mode verifies itself. It builds
both a lossless and a deep candidate, then checks the deep candidate
against the original: page count, page sizes/rotations, extracted text
(per-page word multisets and alphanumeric character count), and a render
comparison of up to 5 evenly spaced pages at 72 DPI (fails if more than
8% of pixels differ by >10 levels in any channel). The deep result is
kept only if verification passes **and** it is smaller than the lossless
result; otherwise the lossless result is delivered and the output says so
(`deep_fallback` in the stats, with the reason). The render check needs
PyMuPDF; without it the text check falls back to comparing content-stream
string tokens and the render check is skipped, with a note in the output.

When it helps, and when it doesn't:

- **Helps:** vector-heavy engineering PDFs. Phase Two cross-sections,
  114 pages: 11,818,833 → 9,508,378 bytes (**19.55%**, lossless alone
  was 10,097,355). Partial Building Permit, 2 pages: 415,739 →
  196,897 bytes (**52.64%**, lossless alone was 366,808). In both cases
  the extracted text was identical and the render check stayed at
  ~2–3% of pixels differing by >10 (sub-pixel anti-aliasing only).
- **Doesn't help:** scanned/image-heavy PDFs (nothing to shorten — it
  falls back to the lossless result), and files whose only content
  stream contains an inline image. Example: the S3.00 foundation plan
  (177,732 bytes) has a single content stream with a `BI` operator, so
  deep mode shortened 0 streams and fell back to the lossless
  157,392 bytes (11.44%) — correctly, and it says so.
- **Slow:** about twice the lossless time, because it runs Zopfli twice
  (once per candidate). The 114-page Phase Two file takes ~16–18 minutes
  lossless, so deep mode on it takes roughly half an hour.

## How it works (the tricky part)

Neither PyMuPDF nor pikepdf will preserve hand-compressed stream bytes if you
ask them to write normally — they strip `/Filter` or recompress with zlib.
So this tool:

- writes the Zopfli (or JPEG) bytes raw with `filter=None`,
- sets `/Filter` manually (`/FlateDecode` or `/DCTDecode`),
- deletes `/DecodeParms` (pikepdf's `read_bytes()` already returns
  predictor-decoded data, so a viewer must not apply a predictor again),
- and saves with `recompress_flate=False` so the optimized bytes survive.

Additionally:

- for 1-bit scans, it writes Flate-packed bits (never JPEG) with `/Decode`
  preserved, flipping the threshold when `/Decode` is inverted (`[1 0]`);
- it replaces an image only when the new bytes are smaller, and copies the
  original to the output when the whole file would grow.

## Limitations

- Zopfli is slow. A 70-page vector-heavy PDF can take ~10 minutes.
  `--zopfli-iterations 15` saves only a few dozen extra bytes per large
  stream compared to the default 5, and is much slower — not recommended.
- Deep mode (`--deep`) is about twice as slow again, because it builds
  both a lossless and a deep candidate. It only pays off on vector-heavy
  PDFs; on scanned/image PDFs it falls back to the lossless result.
- Image downscaling is lossy. If pixel-perfect maps matter, use
  slider 0% / `--no-image-downscale` and accept a smaller reduction.
- This tool does not OCR, rasterize pages, or change page size/rotation.

## Files

- `pdf_compressor.py` — command-line tool (also importable: `compress_file`,
  `compress_batch`, `compression_percent_to_settings`, `verify_output`)
- `vector_shorten.py` — the deep-mode number shortener: operator-aware,
  string-safe content-stream tokenizer plus the proven rounding rules
- `pdf_compressor_gui.py` — Tkinter GUI with a compression slider and a
  Deep vector mode checkbox
- `requirements.txt`, `LICENSE`, `.gitignore`

## License

MIT — see `LICENSE`.
