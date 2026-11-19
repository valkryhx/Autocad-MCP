# Seeing a drawing without AutoCAD's window

Two questions this answers, and one it deliberately does not.

| Question | Tool | Evidence |
|---|---|---|
| Are the layers the colours I set? | `scripts/render_svg.py` | the SVG's class table, asserted per layer |
| Did anything actually get drawn? | `scripts/render_mpl.py` | colour count and painted fraction of the PNG |
| Does the text display correctly? | **nothing here** — the operator's screen, or a printed PDF | see §4 |

## 1. Why a renderer and not a screenshot

On this machine `view_zoom_and_screenshot` cannot photograph AutoCAD 2026. The
drawing area is **GPU-composited** and is not on the GDI surface: a direct
`GetPixel` on the frame reads `RGB(255,255,255)`, and both `source: "window"`
and `source: "export_snapshot"` come back blank. Worse, `PrintWindow` sends a
synchronous `WM_PRINT` that upsets the render thread and wedges COM automation
while the process stays alive.

Rendering the DXF ourselves needs no window, no COM seat and no GPU. That makes
it the right tool for the narrow questions it answers — and the wrong tool for
"does this look right", which only a person can judge.

## 2. `scripts/render_svg.py` — layer colours, assertable

```bash
python scripts/render_svg.py FILE.dxf -o out.svg
python scripts/render_svg.py FILE.dxf --expect A-FURN-E-N=#ff7f00
```

`--expect LAYER=#rrggbb` exits **1** when the layer's colour is not that value,
so a build can gate on it — unlike an eyeball. Repeatable. A layer whose colour
is defined but unused still passes: the assertion is about the layer, not about
entities on it.

ezdxf writes colours once into `<defs><style>` as CSS classes and references
them by class, so counting hex codes in the body reports zero. The tool reads
the class table, which is the honest source.

## 3. `scripts/render_mpl.py` — pixels, not geometry

```bash
python scripts/render_mpl.py FILE.dxf -o out.png --dpi 150
```

A backend can emit valid SVG and still paint a black rectangle, so this counts
**distinct colours** and **painted fraction** on the finished PNG. One colour
means nothing was drawn → exit 1.

Two traps are handled in the code because both produce a silent wrong size:

- **`MatplotlibBackend.finalize()` overwrites your figure size.** It calls
  `plt.figaspect()` and then `set_size_inches()` on *your* figure, replacing the
  size you chose with its own ~4.8 in default. Measured: 13×14 in @ 150 dpi
  should be 1950×2100 px and came out **666×720**. Pass
  `adjust_figure=False`.
- **A fixed figure size does not "just work".** `finalize` fits the content into
  the canvas, so a square drawing in a 1.4:1 canvas is letterboxed and loses
  about a third of its pixels. Size the figure from the extents
  (`fitted_size()`), don't hard-code 14×10.

## 4. What a renderer cannot tell you: glyphs

**This is the trap that makes a renderer dangerous as an appearance check.**
Measured 2026-11-19: the same drawing rendered twice, once with `Standard` =
`simhei.ttf` and once with `Standard` = `txt` (an SHX font with no `²`). In
AutoCAD the second loses the superscript in `76.00 m²`. The two renders differ
by **168 pixels out of 1 813 000 (0.009 %)** — ezdxf falls back to a substitute
font silently and draws the character anyway.

So a clean render is **not** evidence that text will display. Inside AutoCAD
that question is settled by `text_glyphless` from the text-overlap checker, and
outside it by the operator's own screen or a real PDF. Never quote a render as
proof that `m²`, `Ø` or CJK will print.

Corollary: do not add these tools to the text-overlap skill. That skill owns
`text_glyphless`, whose whole point is that a missing glyph is silent; a
renderer that draws it fine would contradict the check on the same drawing and
teach the wrong lesson.

## 5. Memory pressure, not a version problem

`import numpy` here intermittently dies with

```text
OpenBLAS error: Memory allocation still failed after 10 retries, giving up.
```

That is **memory pressure**, measured 2026-11-19: free RAM was 0.7 GB with
AutoCAD holding 1.4 GB private on a 15.3 GB machine. Both numpy 2.4.6 and
matplotlib 3.11.2 were already the newest releases on PyPI, so `pip install
--upgrade` changes nothing. Setting `OPENBLAS_NUM_THREADS=1` (which both scripts
do) shrinks the arena reservation and helps; freeing RAM is the real answer.

Do not upgrade numpy to "fix" this. It is a runtime dependency of the MCP
server itself; a bad upgrade breaks the server all the tools run on. It did
here: an interrupted upgrade deleted 67 `fontTools` source files (leaving only
the `.pyd`, which a live MCP process had locked) and `import matplotlib` then
failed `No module named 'fontTools.agl'`. Repaired by installing the same
version into a scratch venv and copying back only the missing files.

## 6. When to run which

| Situation | Do |
|---|---|
| Before delivering, to prove layer colours | `render_svg.py --expect ...` |
| To prove a drawing is not empty / a layout survived | `render_mpl.py`, check painted % |
| To judge how it looks | ask the operator; do not report a render as appearance |
| To prove text will print | `text_glyphless` (in AutoCAD) or a real PDF |