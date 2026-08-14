# User fonts

Drop `custom.font` on the FAT volume the player exposes over USB, in the root,
keeping that exact name. Then pick **"Imitation Song large"** in the reader's
font menu.

Nothing is written to the SD card and the vendor's sdfs container stays stock:
the reader reads this file itself and answers LVGL's glyph callbacks directly.
The menu row still carries the vendor's label because the localised string
resource behind the label ids has not been located — see
[reader-architecture.md](../docs/reader-architecture.md) for the row table and
what is still missing.

## What is here

`custom.font` is **Literata** at 13 px, variable-font instance `opsz=7`,
`wght=400`, rendered 1 bpp with FreeType autohinting. 170 glyphs: ASCII,
Latin-1, `Œ œ Ÿ`, and typographic punctuation. Anything outside that — CJK
especially — will not draw while this font is selected.

Literata is licensed under the SIL Open Font License; see [OFL.txt](OFL.txt).
It is a Google Fonts release, redistributable under that licence.

## Making your own

```
tools/mkfont.py <font.ttf> <px> custom.font [--axes opsz,wght]
```

Two settings decide whether small monochrome text reads well or looks blotchy,
and both are about the same thing — a 1 bpp grid can only round a stroke to a
whole pixel:

- **Use a small optical size.** A face drawn for large text has stems much
  heavier than its hairlines, and at 13 px that contrast comes back as uneven
  weight. For a variable font pass the smallest `opsz` (Literata: 7). Literata's
  18 pt cut at 13 px gives `b` a 2 px stem against 1 px hairlines.
- **Autohinting is on by default** and is what makes stems land on one uniform
  width. Without it, capitals round to 2 px while lowercase rounds to 1 px, and
  a single `o` comes out 1 px on one side and 2 px on the other.

Pick the pixel size by the line box, not by the font's nominal size: the reader
draws on a 20 px pitch, and 13 px at `opsz=7` gives ascent 16 / descent 4, which
matches the vendor's own fonts exactly.

Requires `freetype-py`.
