#!/usr/bin/env python3
"""Graphical front-end for pdf_compressor.py, with a slider for compression.

Run:
    python pdf_compressor_gui.py

The window has:
- an input PDF picker and an output PDF picker,
- a slider (0-100) for compression strength,
- a Compress button, and a result line showing the actual reduction.

Slider mapping (same as --compression-percent in the CLI):
-   0%  -> lossless only (no image changes)
-  50%  -> images 0.5x, JPEG quality 50 (balanced, recommended)
- 100%  -> images 0.2x, JPEG quality 25 (maximum shrink)

The slider sets *strength*, not a guaranteed final percentage. The actual
reduction depends on the file: a vector-only file with no large images
maxes out around 17-18% even at 100%, because there is no image left to
shrink. The result line always shows the real achieved percentage.

Deep mode checkbox (opt-in, off by default): safely shortens numbers in
vector drawing instructions (coordinates to 1 decimal, colours to 2
decimals, exact 0.479969 -> 0.48 scale only — see vector_shorten.py),
then verifies the result (page count, text, render comparison) and falls
back to the lossless result if verification fails or deep is not smaller.
Best for vector-heavy engineering PDFs; slow (about twice the lossless
time); does nothing extra for scanned/image PDFs.

Compression runs in a background thread so the window stays responsive.
Detailed progress goes to the console; the window shows status and the
final result. Requires tkinter (included with most Python installs;
on Debian/Ubuntu: sudo apt install python3-tk).
"""

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from pdf_compressor import (
        compress_file,
        compression_percent_to_settings,
    )
except ImportError:  # running from another directory: add script dir to path
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pdf_compressor import (
        compress_file,
        compression_percent_to_settings,
    )


def slider_description(percent):
    """Human-readable label for a slider value."""
    enabled, scale, quality = compression_percent_to_settings(percent)
    if not enabled:
        return f"{percent}%  ->  lossless only (no image changes)"
    if percent == 50:
        return f"{percent}%  ->  images {scale}x, JPEG {quality} (balanced, recommended)"
    if percent >= 80:
        return f"{percent}%  ->  images {scale}x, JPEG {quality} (aggressive)"
    return f"{percent}%  ->  images {scale}x, JPEG {quality}"


class CompressorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("PDF Compressor")
        self.root.minsize(520, 450)
        self._worker = None
        self._result = None

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        # Input file
        ttk.Label(frame, text="Input PDF:").grid(row=0, column=0, sticky=tk.W)
        self.input_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.input_var, width=48).grid(
            row=0, column=1, sticky=tk.EW, padx=(8, 8)
        )
        ttk.Button(frame, text="Browse...", command=self._pick_input).grid(
            row=0, column=2
        )

        # Output file
        ttk.Label(frame, text="Output PDF:").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        self.output_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.output_var, width=48).grid(
            row=1, column=1, sticky=tk.EW, padx=(8, 8), pady=(8, 0)
        )
        ttk.Button(frame, text="Browse...", command=self._pick_output).grid(
            row=1, column=2, pady=(8, 0)
        )

        # Slider
        ttk.Label(frame, text="Compression strength:").grid(
            row=2, column=0, sticky=tk.W, pady=(16, 0)
        )
        self.percent_var = tk.IntVar(value=50)
        self.slider = tk.Scale(
            frame,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            variable=self.percent_var,
            command=self._on_slider,
            length=320,
        )
        self.slider.grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=(4, 0))
        self.slider_label = ttk.Label(frame, text=slider_description(50))
        self.slider_label.grid(row=4, column=0, columnspan=3, sticky=tk.W)

        ttk.Label(
            frame,
            text="Note: the slider sets strength, not a guaranteed percentage. "
            "Vector-only files max out around 17-18% even at 100%, because "
            "there is no large image left to shrink.",
            wraplength=480,
        ).grid(row=5, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))

        # Deep vector mode checkbox (opt-in, off by default)
        self.deep_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="Deep vector mode (slower; best for engineering drawings)",
            variable=self.deep_var,
        ).grid(row=6, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
        ttk.Label(
            frame,
            text="Shortens numbers in vector drawing instructions, then "
            "verifies the result (text + render) and falls back to the "
            "lossless result if anything looks wrong. About twice as slow.",
            wraplength=480,
        ).grid(row=7, column=0, columnspan=3, sticky=tk.W)

        # Compress button + status
        self.compress_button = ttk.Button(
            frame, text="Compress", command=self._start_compression
        )
        self.compress_button.grid(row=8, column=0, columnspan=3, pady=(16, 0))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(frame, textvariable=self.status_var, wraplength=480).grid(
            row=9, column=0, columnspan=3, sticky=tk.W, pady=(8, 0)
        )

        frame.columnconfigure(1, weight=1)

    def _pick_input(self):
        path = filedialog.askopenfilename(
            title="Choose input PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if path:
            self.input_var.set(path)
            # Suggest a default output next to the input.
            base, _ext = os.path.splitext(path)
            self.output_var.set(base + "_compressed.pdf")

    def _pick_output(self):
        path = filedialog.asksaveasfilename(
            title="Choose output PDF",
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if path:
            self.output_var.set(path)

    def _on_slider(self, _value):
        try:
            percent = self.percent_var.get()
        except tk.TclError:
            return
        self.slider_label.config(text=slider_description(percent))

    def _start_compression(self):
        input_path = self.input_var.get().strip()
        output_path = self.output_var.get().strip()
        if not input_path or not os.path.isfile(input_path):
            messagebox.showerror("Missing input", "Please choose an existing input PDF.")
            return
        if not output_path:
            messagebox.showerror("Missing output", "Please choose an output PDF path.")
            return
        if os.path.abspath(input_path) == os.path.abspath(output_path):
            messagebox.showerror(
                "Same file", "Output must be a different file from input."
            )
            return
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("Busy", "Compression is already running.")
            return

        percent = self.percent_var.get()
        enabled, scale, quality = compression_percent_to_settings(percent)
        deep = self.deep_var.get()
        self.compress_button.config(state=tk.DISABLED)
        self.status_var.set(
            f"Compressing at {percent}% "
            f"({'lossless only' if not enabled else f'images {scale}x, JPEG {quality}'})"
            f"{' + deep vector mode' if deep else ''}... "
            "this can take several minutes on large files. Please wait."
        )
        self._result = None

        def _run():
            try:
                stats = compress_file(
                    input_path,
                    output_path,
                    image_scale=scale,
                    image_quality=quality,
                    downscale_enabled=enabled,
                    deep=deep,
                    verbose=False,
                )
                self._result = ("ok", stats)
            except Exception as exc:  # report in the GUI thread, not here
                self._result = ("error", str(exc))
            self.root.after(0, self._on_complete)

        self._worker = threading.Thread(target=_run, daemon=True)
        self._worker.start()

    def _on_complete(self):
        self.compress_button.config(state=tk.NORMAL)
        kind, payload = self._result or ("error", "Unknown error.")
        if kind == "error":
            self.status_var.set(f"Failed: {payload}")
            messagebox.showerror("Compression failed", str(payload))
            return
        stats = payload
        deep_note = ""
        if stats.get("deep_fallback"):
            deep_note = (
                f" Deep mode fell back to lossless "
                f"({stats['deep_fallback_reason']})."
            )
        elif stats.get("deep"):
            deep_note = " Deep mode verified and kept."
        self.status_var.set(
            f"Done! {stats['original_size']:,} -> {stats['new_size']:,} bytes "
            f"({stats['reduction_percent']:.2f}% smaller, "
            f"saved {stats['saved_bytes']:,} bytes). "
            f"Pages: {stats['in_pages']} -> {stats['out_pages']}."
            f"{deep_note}"
        )


def main():
    root = tk.Tk()
    CompressorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
