# How the reader actually works, and what to build instead

Established by reversing, after the symbol map made the functions findable.

## The vendor's structure

`_reading_create_content` (`0x10049ec0`) registers a **periodic timer**:

```
1004a01a  movs r1, #2            ; period
1004a01c  ldr  r0, =0x1004937d   ; callback
1004a022  bl   0x100a1130        ; timer_create(cb, period, user=reader obj)
1004a02a  str  r0, [r4, #0x30]
```

So `0x1004937c` -- the function the injected reader replaced -- is a **timer
callback on the display thread**, firing ~3x/second (measured with a call
counter). It decodes up to four page contexts and renders whichever succeeded.

`0x100fd8b8` / `0x100fd8c2` are **not locks**. `timer_create` does
`bfc r3,#0,#1` on `[timer+0x14]`, the same flag bit those two set and clear:
they are timer pause/resume, i.e. a re-entrancy guard. Three separate readings
of these helpers were wrong before the symbol map existed.

Meanwhile `ebook_calculate_pages` (`0x1004bd6c`) is called from
`_ebook_reading_event_handle` (`0x1004bf98`) on the **`ebook` thread**
(`_ebook_app_loop`, `0x10047860`), paginating in the background.

## Why the injected reader crashes, and why it is slow

Both come from the same mistake: we did **blocking SD I/O inside a display-thread
timer callback**.

- **Crash.** `fs_open`/`fs_read` from the timer callback races
  `ebook_calculate_pages` doing file I/O on the `ebook` thread. The result is
  `ASSERTION FAIL [thread->base.pended_on] @ sched.c:595` on the `ebook` thread
  -- scheduler/FS state corruption, not a fault at the point of damage. It only
  appeared when a different book file changed the timing.
- **Slow page turns.** Every turn does open/seek/read/close, wraps the page, and
  then sets N label texts, each invalidating LVGL. Cost scales with line count,
  which is why 13 lines felt worse than 12.

## The design to build

Exactly the double-buffered pre-render the user asked for, mapped onto this
architecture:

1. **Timer callback (display thread): drawing only.** Copy an already-prepared
   page into the labels. No file I/O, no wrapping, no allocation. This is the
   only thing that must be fast, and it becomes O(lines) of `lv_label_set_text`.
2. **Preparation on the `ebook` thread**, which already owns file access --
   hook `_ebook_reading_event_handle` (`0x1004bf98`) rather than the timer.
   Never touch the filesystem from the display thread again.
3. **Two page buffers, `cur` and `next`.** A page turn swaps them (instant, no
   I/O) and then requests preparation of the new `next` while the user reads.
   Back-paging keeps the existing stack of page-start offsets.

The remaining cost per turn is the e-ink refresh itself, which is inherent.

## Consequences for the current code

- `reader_body` must stop calling `repaginate()`.
- `st` must be shared between two threads: the preparing thread writes `next`,
  the timer reads `cur`. A single ownership flag is enough -- prepare into a
  buffer the drawer is not using, then flip.
- The hardcoded book path goes away: `ebook_file_init` (`0x1004b29c`) and
  `ebook_read_page_data` (`0x1004b7c4`) show how the vendor resolves the file.

## Next

Reverse `_ebook_reading_event_handle` to find the hook point on the `ebook`
thread and how key events arrive. That single piece unlocks owning the page
turn, the pre-render, and the fix for the crash.

---

# The ebook thread's message loop (reversed)

`_ebook_reading_event_handle` (`0x1004bf98`) is the reading scene's loop, on the
**`ebook` thread**. It owns the file (`ebook_file_init`, `0x1004b29c`) and then
pumps messages:

```
1004bfe2  bl 0x1004b29c        ; ebook_file_init(...)
1004bffe  movs r1, #1          ; <-- TOP OF LOOP
1004c002  bl 0x100ff06c        ; msg_manager_receive_msg -> os_receive_msg
1004c008  beq .. exit
1004c030  tbh  [pc, r3, lsl #1]; switch on msg TYPE  ([sp+0x19])
1004c050  tbh  [pc, r3, lsl #1]; switch on COMMAND   ([sp+0x1a])
1004c0b8  bl 0x1004bd6c        ; ebook_calculate_pages (guarded by two flags)
1004c0e2  beq 0x1004bffe       ; loop
```

## Dispatch

Message **type** (`[sp+0x19]`, 4..11):

| type | target |
|---|---|
| 4 | `0x1004c344` |
| 7 | `0x1004c324` |
| **8** | **`0x1004c044`** -- user commands |
| 11 | `0x1004c2f8` |
| 5, 6, 9, 10 | `0x1004c33a` (ignored) |

Type 8 **command** (`[sp+0x1a]`, 1..16) -- observed in the log as
`recv msg: type 8, cmd 4/5/1`:

| cmd | target | note |
|---|---|---|
| 1 | `0x1004c144` | copies 0x10 bytes of payload to `0x1801a098` |
| 4 | `0x1004c186` | 4-byte payload -> `0x1801a080` |
| 5 | `0x1004c192` | payload -> `0x1801a0a8`, then joins the page path at `0x1004c076` |
| 6 | `0x1004c1a8` | -> `ebook_bmk_add` |
| 12 | `0x1004c074` | the page path |
| 11, 13-16 | `0x1004c264`, `0x1004c2c2`... | |

## The hook point for pre-rendering

`bl 0x100ff06c` at **`0x1004c002`** is the message receive at the top of the
loop, and it is **unconditional** -- every iteration passes through it, unlike
`0x1004c0b8` (two flag guards) or `0x100493a8` (a `cbnz` skips it).

Wrapping it gives exactly what the pre-render design needs:

- runs on the **`ebook` thread**, the one that already owns file access, so no
  cross-thread filesystem races -- the cause of the `pended_on` assertion
- runs **before** the loop blocks waiting for the next message, so preparation
  happens in the idle time while the user reads
- the original is tail-called afterwards, so the loop is unchanged

```
our_hook:  prepare next page if needed   ; file I/O + wrap, on the right thread
           tail-call 0x100ff06c          ; then block for the next message
```

The display-thread timer callback then only copies a prepared page into the
labels: no I/O, no wrapping, no allocation.

---

# As built

The design above was implemented and is running. This section records what the
finished reader actually does, and the measurements that shaped it.

## Integration points

| address | vendor role | our hook | thread |
|---|---|---|---|
| `0x1004a288` | line height (`content_h / 8`) | `hook()` | display |
| `0x100493a8` | the one call to the render function | `render_hook()` | display |
| `0x1004c002` | `msg_manager_receive_msg` at the top of the loop | `prepare_hook()` | **ebook** |

Code lives at XIP `0x101d3000` (flash `0x1e7000`). `0x1004925a`, the render tail
call, is **unusable** as a hook site — hooking it hangs the device with no Zephyr
fault. Do not retry it.

## Memory

The vendor decodes book text into **every** page context, so no context RAM is
reusable. Two independent proofs: a live read found our magic replaced by the
book's own words (`"forw"`, `"ard "`, `"afte"`), and NOPing the standalone decode
at `0x1004938a` — whose return value the original *discards* — stopped all
decoding, so its side effects are load-bearing.

State is therefore heap-allocated with `lv_mem_alloc` (`0x100a0644`), with only an
8-byte anchor at `0x18018e98`. That address is the one region whose canary
survived the **real workload** — reading, paging, audio playback and scene changes
— with 0 of 128 words touched. An idle canary previously "proved" `0x18210000`
free; it bus-faulted under load. Idle canaries prove nothing.

## The pre-render, and the two-step repaint

`after_render` runs several times per turn (the vendor renders up to three
contexts). With a single buffer being refilled asynchronously, one pass painted
the old page and a later one repainted the new — visible as a two-stage redraw
with the last stage landing on the first two lines.

The fix is that `cur` is only ever replaced by a page that is **already complete**:

```c
if (S->want >= 0 && S->nxt_valid && S->nxt.start == S->want) {
    fw_memcpy(&S->cur, &S->nxt, sizeof(struct page));   /* not `=`: see below */
    S->nxt_valid = 0;
    S->want = -1;
    S->need_prep = 1;
}
```

`nxt` is filled on the ebook thread in the idle time *before* its message loop
blocks — i.e. while the user is reading. Confirmed on hardware: the repaint is now
a single stage.

Note the explicit `fw_memcpy`. A struct assignment of a ~540-byte page makes the
compiler emit a call to `memcpy`, which does not exist in a `-ffreestanding`
image; it fails at link time as `dangerous relocation: unsupported relocation`.

Labels are still written on every pass, because the vendor fills them itself and
skipping would let its text show through.

## Reflow — what the text actually looked like

Pages were holding only ~212 bytes across 12 lines. Two candidate causes: the
file's own newlines, or a conservative width estimate. Instrumenting the wrapper
to report *why* each line ended settled it over one real reading session:

| line ended because | count |
|---|---|
| a newline in the file | 143 |
| ran out of width | 29 |
| hit the 43-char line buffer | 6 |

**84 of the 143 newline-terminated lines were empty**, and the non-empty ones
averaged 16 characters. Width-terminated lines landed at 164–171 px with ~24
characters — so the width estimate was fine; it was almost never the thing ending
a line. The book file is hard-wrapped narrow with a blank line between each line,
and the reader was faithfully reproducing someone else's layout.

So the wrapper reflows: **a single newline is soft** (it becomes a space), and
**only a blank line is a paragraph break**. That takes lines from ~16 to ~24
characters, roughly 50% more text per page at the same line count.

Reflow consumes the file's blank line, so the paragraph gap is then re-emitted
deliberately as one blank display line — never at the top of a page, where a page
opening on blank reads as a rendering failure.

Two consequences worth knowing:

- The read window grew from 512 to 768 bytes, since a reflowed page consumes far
  more source text.
- `wrap_one` returns **source bytes consumed**, not characters emitted, so page
  offsets stay exact with no second buffer and no index map. A page that runs off
  the end of the read window drops its last line rather than showing a broken word.

## Instrumentation gotcha

`fw_log` takes exactly **one** integer — a format with two `%d` prints stack
garbage, which silently invalidated an earlier round of readings. Packing several
fields into one integer works, but give each field enough room: packing
`why*10000 + px*100 + chars` let a `px` above 99 carry into the reason field and
corrupt it. The data was still recoverable only because width and character count
must agree at roughly 7 px per character.
