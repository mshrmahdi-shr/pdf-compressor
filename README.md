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
   Use `--no-image-downscale` for a fully lossless run.

2. **Recompresses every Flate stream with Zopfli.**
   Zopfli produces standard DEFLATE data (the same format PDF readers already
   understand), but it searches much harder for repeated patterns than zlib.
   It is slow, but it typically saves another 10–20% on content streams that
   zlib already compressed. Text and vector content are preserved exactly.

3. **Removes non-visible data.**
   Measurement viewports (`/VP`) and `/LastModified` stamps are removed from
   pages. They do not affect appearance, text, or print output.

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

## Installation

```bash
pip install -r requirements.txt
```

Requires Python 3.9+ and:

- `pikepdf`
- `Pillow`
- `zopfli`

## Command-line usage

```bash
# Basic: 20%-style shrink (image downscaling + Zopfli + metadata cleanup)
python pdf_compressor.py input.pdf output.pdf

# Same slider as the GUI, from the command line
python pdf_compressor.py input.pdf output.pdf --compression-percent 50

# Fully lossless: Zopfli + metadata cleanup only, images untouched
python pdf_compressor.py input.pdf output.pdf --no-image-downscale
python pdf_compressor.py input.pdf output.pdf --compression-percent 0

# More aggressive image shrink
python pdf_compressor.py input.pdf output.pdf --image-scale 0.4 --image-quality 40

# Keep measurement viewports
python pdf_compressor.py input.pdf output.pdf --keep-vp
```

`--compression-percent` overrides `--image-scale` / `--image-quality`.
The tool prints the original size, the new size, the percentage reduction,
and a page-count check (`44 -> 44 OK`).

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

## How it works (the tricky part)

Neither PyMuPDF nor pikepdf will preserve hand-compressed stream bytes if you
ask them to write normally — they strip `/Filter` or recompress with zlib.
So this tool:

- writes the Zopfli (or JPEG) bytes raw with `filter=None`,
- sets `/Filter` manually (`/FlateDecode` or `/DCTDecode`),
- deletes `/DecodeParms` (pikepdf's `read_bytes()` already returns
  predictor-decoded data, so a viewer must not apply a predictor again),
- and saves with `recompress_flate=False` so the optimized bytes survive.

## Limitations

- Zopfli is slow. A 70-page vector-heavy PDF can take ~10 minutes.
  `--zopfli-iterations 15` saves only a few dozen extra bytes per large
  stream compared to the default 5, and is much slower — not recommended.
- Image downscaling is lossy. If pixel-perfect maps matter, use
  slider 0% / `--no-image-downscale` and accept a smaller reduction.
- This tool does not OCR, rasterize pages, or change page size/rotation.

## Files

- `pdf_compressor.py` — command-line tool (also importable: `compress_file`,
  `compression_percent_to_settings`)
- `pdf_compressor_gui.py` — Tkinter GUI with a compression slider
- `requirements.txt`, `LICENSE`, `.gitignore`

## License

MIT — see `LICENSE`.
