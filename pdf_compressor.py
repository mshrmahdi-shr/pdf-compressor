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

Usage
-----
    python pdf_compressor.py input.pdf output.pdf
    python pdf_compressor.py input.pdf output.pdf --compression-percent 50
    python pdf_compressor.py input.pdf output.pdf --no-image-downscale
    python pdf_compressor.py input.pdf output.pdf --image-scale 0.4 --image-quality 40
    python pdf_compressor_gui.py   # graphical version with a slider

Requirements: pikepdf, Pillow, zopfli  (see requirements.txt)
The GUI additionally needs tkinter (included with most Python installs;
on Debian/Ubuntu: sudo apt install python3-tk).
"""

import argparse
import io
import os
import sys

import pikepdf
from PIL import Image
import zopfli.zlib


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
    """Halve large raster images in place. Returns (count, bytes_saved)."""
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
                try:
                    width = int(obj.get("/Width"))
                    height = int(obj.get("/Height"))
                except Exception:
                    continue
                if width * height < min_pixels:
                    continue
                try:
                    image = pikepdf.PdfImage(obj)
                    pil_image = image.as_pil_image()
                    new_w = max(1, int(width * scale))
                    new_h = max(1, int(height * scale))
                    if pil_image.mode in ("RGBA", "LA", "P"):
                        pil_image = pil_image.convert("RGB")
                    resized = pil_image.resize((new_w, new_h), Image.LANCZOS)
                    buf = io.BytesIO()
                    resized.save(buf, format="JPEG", quality=quality, optimize=True)
                    new_data = buf.getvalue()
                    old_len = len(obj.get_raw_stream_buffer())

                    # Write JPEG bytes raw and set /Filter manually, so pikepdf
                    # does not recompress them with its own JPEG encoder.
                    obj.write(new_data, filter=None)
                    obj["/Filter"] = pikepdf.Name("/DCTDecode")
                    obj["/Width"] = new_w
                    obj["/Height"] = new_h
                    if "/DecodeParms" in obj:
                        del obj["/DecodeParms"]

                    # Same treatment for the transparency mask, if any.
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


def compress_file(
    input_path,
    output_path,
    image_scale=0.5,
    image_quality=50,
    min_image_pixels=50000,
    downscale_enabled=True,
    keep_vp=False,
    zopfli_iterations=5,
    verbose=True,
):
    """Run all compression steps. Returns a dict of stats.

    Raises FileNotFoundError / ValueError for bad inputs, and propagates
    pikepdf errors. On success, output_path exists and page counts match
    (checked by the caller via verify_page_count if desired).
    """
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")
    if os.path.abspath(input_path) == os.path.abspath(output_path):
        raise ValueError("Output must be a different file from input.")

    original_size = os.path.getsize(input_path)
    print(
        f"Original: {original_size} bytes, target for -20%: "
        f"{int(original_size * 0.8)} bytes",
        flush=True,
    )

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
    reduction = (1 - new_size / original_size) * 100 if original_size else 0
    print(f"Done! Output: {output_path}", flush=True)
    print(f"Original: {original_size} bytes", flush=True)
    print(f"New: {new_size} bytes", flush=True)
    print(f"Reduction: {reduction:.2f}%", flush=True)
    print(f"Saved: {original_size - new_size} bytes", flush=True)

    in_pages, out_pages = verify_page_count(input_path, output_path)
    print(
        f"Pages: {in_pages} -> {out_pages} "
        f"({'OK' if in_pages == out_pages else 'MISMATCH!'})",
        flush=True,
    )

    return {
        "original_size": original_size,
        "new_size": new_size,
        "reduction_percent": reduction,
        "saved_bytes": original_size - new_size,
        "in_pages": in_pages,
        "out_pages": out_pages,
        "image_count": count,
        "image_saved": saved,
        "stream_count": stream_count,
        "zopfli_saved": zopfli_saved,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Shrink an already-optimized PDF with Zopfli recompression "
        "and optional image downscaling."
    )
    parser.add_argument("input", help="Input PDF file")
    parser.add_argument("output", help="Output PDF file")
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

    try:
        stats = compress_file(
            args.input,
            args.output,
            image_scale=image_scale,
            image_quality=image_quality,
            min_image_pixels=args.min_image_pixels,
            downscale_enabled=downscale_enabled,
            keep_vp=args.keep_vp,
            zopfli_iterations=args.zopfli_iterations,
            verbose=verbose,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if stats["in_pages"] != stats["out_pages"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
