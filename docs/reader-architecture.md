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

---

# The speaker (TTS) button, and state recovery

## Symptom

Pressing the speaker icon in the reading status bar restarted the book from the
beginning while the vendor's page counter carried on unchanged.

## What was actually happening

Two separate faults, found by watching state over UART across a real press:

1. **The anchor is not ours.** `0x18018e98` sits inside the READING SCENE's own
   data. Searching the image for literals in that range found
   `_reading_unload_resource` owning `0x18018e20`, `0x18018e35` and
   `0x18018e95` -- three bytes below the anchor -- and `txt_analy_one_line`
   using `0x18018e18`/`0x18018e1c`. The button triggers a scene resource unload,
   which clears the area; a live trace caught the anchor holding `0x0000005d`,
   a real value rather than a memset. The original canary passed because it
   covered reading, paging, audio and scene changes, but never this path.
2. **TTS drives the vendor's line counter.** With the anchor fixed, the page
   still walked away on its own: the trace showed `reading_line` climbing
   1040 -> 1088 -> 1136 with no user input. Our reader treats a change in that
   counter as a page-turn signal.

## State recovery (fix for 1)

No durable RAM is needed. The state block is `lv_mem_alloc`'d and **never
freed** -- only the 8-byte pointer to it is lost. When the anchor is missing,
`recover()` scans the LVGL heap (`0x01000000`-`0x01020000`, the observed
allocation window) for the newest block carrying our magic and adopts it,
resuming at `cur.start`. A `gen` counter incremented on each adoption keeps a
stale twin from outranking the live block, and plausibility checks on line
counts and offsets stop heap junk impersonating a state. Confirmed working: a
trace showed `gen` climbing 3 -> 4 -> 5 -> 6 across repeated presses.

## Disabling the button (fix for 2)

Our position is a byte offset into the file; TTS's is a line index into the
vendor's pagination. They are not reconcilable without adopting the vendor's
line model -- the very thing replaced to get 12 reflowed lines. So the button
is disabled.

It is a **16x16 icon at (33,8), a direct child of the SCREEN**, not of the
status bar container, sitting just right of the back button.
`disable_tts_button()` finds it by geometry and clears
`LV_OBJ_FLAG_CLICKABLE` (`1<<1`).

## Two traps this exposed

- **The scene reallocates its objects.** `screen` moved from `0x0100497c` to
  `0x0100498c` between a dump and the next press. Flags written from the
  debugger land on stale objects and appear to do nothing -- the tests read as
  negative when they were merely invalid. The flag must be re-applied from the
  render pass, every tick.
- **A gap in the statics table is not free RAM.** Canaries written to eight
  addresses chosen that way crashed the device: one belonged to the audio
  player. Verify a region is unused *at runtime* before writing to it.

## LVGL v8 object layout on this build

Established by decoding the screen's own extent (`0x010700af` = x2 175, y2 263,
exactly the 176x264 panel):

```
+0x00 class_p   +0x04 parent   +0x08 spec_attr   +0x0c styles
+0x14 coords: x1,y1,x2,y2 as int16      +0x1c flags
spec_attr: +0x00 children**  +0x04 child_cnt
LV_OBJ_FLAG_HIDDEN = 1<<0     LV_OBJ_FLAG_CLICKABLE = 1<<1
```

The reading screen has 6 children: [0] the 18-child label container, [3] the
status bar (back button, page counter, two icons), [4] the TTS icon.

---

# What a scene takeover requires (measured, 2026-08-10)

Groundwork for replacing the vendor's reading scene rather than sharing state
with it. Two structural facts change the design.

## Paging is scrolling

`_reading_scroll_event_cb` (`0x10049684`) tests `cmp r6, #0xb` --
**`LV_EVENT_SCROLL`**. The reading view is a tall scrolled container of labels
and `reading_line` (`+0x194`) is *derived from the scroll position*. Our page
model and the vendor's scroll model are different things laid over the same
widgets, which is the root of every coupling bug: the speaker reset, the runaway
page, the page-jump mismatch and the wrong resume.

## Page turns send no message

Captured over UART with every message the reading loop receives:

| action | messages |
|---|---|
| open book | `type=8 cmd=4` then `cmd=5` |
| **next / previous page** | **none** |
| select-page menu | `cmd=5`, 6x `cmd=3`, then `cmd=4`+`cmd=5` on confirm |
| back to list | `cmd=5`, `cmd=12` |

`type=8 cmd=1` (10946 in one short session) and `cmd=8` (806) are periodic
housekeeping, not input. Turns never reach the ebook thread -- they are handled
on the display side by LVGL touch callbacks. So input must be taken at the
event callback, not at the message loop.

## The UI is resource-driven

Scene creation calls `lvgl_res_load_scene`, `lvgl_res_load_group_from_scene`,
`lvgl_res_load_pictures_from_scene`, `lvgl_res_load_strings_from_scene`. Widgets
come from resource definitions, not hand-written LVGL calls, so a replacement
scene has to build its own objects programmatically.

That needs LVGL constructors, which the symbol map does not name (only functions
that log get names). They are reachable: every object stores its class pointer
at `+0x00`, and the class struct is confirmed LVGL v8 --

```
image class 0x1012c7d4:
  +0x00 base_class 0x1012bee0   +0x04 constructor 0x100fe6a1
  +0x08 destructor 0x100fe6f5   +0x10 event_cb   0x100a3329
  +0x14 width_def/height_def = 0x27d1 0x27d1  (LV_SIZE_CONTENT, confirms v8)
```

Five literals reference that class; the code ones are the image constructor's
callers. The same method finds the label class from any label object's `+0x00`.

## Staging

Full UI replacement also needs fonts (resource-loaded via
`lvgl_bitmap_font_open`), styles and the exit path. A cheaper first stage gets
the whole behavioural benefit: **take the scroll event callback**, so turns,
jumps and position are ours, while the vendor's resource-built widgets are still
used for drawing (we already fill them). At that point nothing is read from
vendor state and the coupling bugs cannot recur; replacing the widgets
themselves becomes optional polish rather than a prerequisite.
