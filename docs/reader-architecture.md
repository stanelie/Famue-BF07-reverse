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

---

# Session state, 2026-08-10 (pause point)

## The input path is still unidentified

Owning input was the plan; it is blocked on finding where a page turn actually
comes from. Four candidates were tested and **all four are ruled out by
measurement**:

| candidate | result |
|---|---|
| ebook-thread message | no message on a turn (open = cmd 4,5; menu = cmd 3; back = cmd 12; turns absent) |
| `_reading_scroll_event_cb` (`0x10049684`) | detour installed and byte-verified in flash; **never fires** on a turn |
| container scroll offset (`spec_attr+0x14`) | logged on change from the render pass; **never changes** on a turn |
| a vendor callback logging its own name | nothing logged but `sys_wake_lock`/`unlock`, then `reading_line` has moved |

So `cmp r6, #0xb` (`LV_EVENT_SCROLL`) in the scroll callback was a red herring:
the function exists but is not on the turn path, and the view does not actually
scroll -- the vendor repaints the labels in its timer instead.

**That search was done, and came back empty** -- which is itself a result:

- no store to `[rX, #0x194]` anywhere in the image
- no instruction mentioning `0x194` at all
- no literal equal to the live field or object address, so the reader object is
  heap-allocated rather than static
- the only ebook-module stores with displacements `0x170`-`0x1b0` are stack
  writes in an unrelated function

So `reader+0x194` is **not a plain field written by ebook code**. It is almost
certainly inside an embedded sub-structure, reached as a small displacement from
an interior pointer -- which no search for `0x194` can find. The reading view
creates a **textarea** (`lv_textarea_set_align: Deprecated` appears in the log),
so the position may live inside an embedded widget.

**Better lead:** the file size sits at the *static* address `0x1801a090`,
mirrored at `0x18019e24`, next to the vendor's open file handle (`0x1801a084`).
That is an ebook context in static memory, and unlike the heap object, code
touching it IS findable by literal search. Look there for the position writer
and for the `.bmk` pagination fields.

## What the firmware tells us about itself

`ebook_file_init` and `ebook_bmk_init` print their own state, which is worth
more than the disassembly:

```
ebook_file_init: open ebook ok, size: 495465!
ebook_file_init: ebook path[39]: /SD1://Neuromancer - William Gibson.txt
ebook_bmk_init: bmk name: /SD1://Neuromancer - William Gibson.BMK, size: 41472
ebook_bmk_init: file size: 495465, current line: 352
ebook_bmk_init: page_magic: 0x0, total: 19656.
ebook_bmk_init: the last page offset: 445200.
_reading_create_content: last reading line: 352, line_height: 20
```

- **File size is live at `0x1801a090`** (mirrored at `0x18019e24`), so the reader
  reads it exactly and the binary-search probe is now only a fallback.
- 495465 bytes over 19656 lines is **25.2 bytes per vendor line**, and our own
  lines measure 25 characters at 167 px -- so the interpolation's structure is
  sound, and `total_lines = pages x 8` is consistent.
- **The `.bmk` carries pagination**, not just bookmarks: `page_magic`, a total,
  and "the last page offset". Reading it could give an EXACT line-to-offset map
  and replace interpolation altogether. Worth doing before more guessing --
  it also explains why deleting a `.bmk` changed resume behaviour.

## Reflow is measuring well

Wrap diagnostics now report `why=2` (ran out of width) at ~167 px and 25
characters per line, against a 168 px label. The earlier complaint -- pages
holding ~16 characters a line, half of them blank -- is resolved.

## Diagnostics currently compiled in

`MSG`, `SCROLL`, `SY`, `L` and `JUMP`/`TURN`/`BOOK` logging is enabled in the
flashed build (3922 bytes). Strip it once the input path is settled.

---

# Session state, 2026-08-10 evening (pause point)

## Working

Reflow, pre-render, single-stage repaint, paragraph gaps, TTS button disabled,
back-after-jump, and **progress shown as a percentage** instead of a page
number -- the counter reads e.g. `1.4%`, computed from byte offset over file
size, exact from the first page and portable between devices.

## Open bug: the reader stops advancing at ~1.0%

Not diagnosed. A capture at the stall was set up but not yet run.

What the last capture DID establish:

- **Every real page turn moves the vendor's line by exactly 8** (23 of 23
  presses). So the lines-per-page payload patch is NOT changing the reading
  scene's stepping, and the claim that pagination is "aligned" to our 12 lines
  is wrong -- it may only affect the totals.
- At the 1.0% point the line moved by **-184** in one step. That is the
  background pagination recalculating, not a user action; it is now ignored
  (outside a two-page bound), which fixed the earlier symptom where the reader
  hopped back to 0.6/0.7/0.8% by a varying amount. The varying size came from
  mapping that line through `size * line / total_lines` while `total_lines` was
  still growing.

Next step on resume: run the stall capture and read whether `DELTA`/`TURN`
still appear after it stops. If they do, detection is fine and the fault is in
preparing or swapping the page; if they stop, the reading scene itself is
wedged and our logic is not in the loop.

## The pattern behind most of today's bugs

Every one came from treating the vendor's line counter as intent. It moves for
reasons that have nothing to do with the user: TTS walks it, scene teardown
zeroes it (which also got the zero SAVED, so resume started at the beginning),
and the background pagination recalculates it. Reading it caused phantom turns;
writing it -- to fix resume -- caused phantom turns in the other direction.

The durable fix is to stop translating position into the vendor's line units at
all: keep the byte offset in our own bookmark. `fs_write` (`0x1007fd74`) and the
`.bmk` handle (`0x1801a0ac`) are both known, and the bookmark area of that file
(`0..0x1e0`) has room. That removes the last shared field and the dependence on
a background scan that is still running.

## Also worth doing

- Suppress the background pagination entirely by writing a valid `page_magic`
  (`0x55aaaa55`) and total into the `.bmk` header, so the firmware treats the
  scan as done. This is the "wasted resources" point, and it also stabilises
  `total_lines`.
- `_ebook_return_btn_event_cb` (`0x100494dc`) is an LVGL event callback that
  receives MANY event codes -- its own first instruction is `cmp r0, #4` to
  filter. A detour placed before that filter runs on ordinary interaction. Do
  not hook it without replicating the filter.
- `lv_label_set_text_copy` (`0x100fe945`) strlens and REALLOCS; never call it
  per render. The static-text variant already in use for our own lines is
  `0x100ec577` and takes a flag argument -- using the wrong one crashed the
  device on book open.

---

# The blank-page bug was a corrupt .bmk (NOT the SD card)

Hours went into chasing blank pages and a stall partway through a book.
**Deleting the books' `.bmk` files fixed it.**

A control test on stock firmware -- all five sectors reverted, no hooks, no
injected code -- reproduced the fault exactly, with 73 filesystem errors. That
proved the fault did not need our running CODE, but the corrupt `.bmk` files
were still on the card during the control, so it did NOT prove the card was at
fault. That conclusion was drawn too fast; `SD card is not detected` is most
likely a boot-time probe message, not the cause.

```
<E> file seek error (-5)      x many        (-5 = EIO)
<E> file read error (-5)
<I> sdcard is plugged
<I> SD card storage initialized!
<E> SD card is not detected                 <-- the card drops out
sdfs cannot found device sd
```

Our own reads never failed once: every `fill rc=512`, no `IOERR` logged. The
reader was healthy; the card underneath it was not. Reads succeed while data is
cached and fail the moment one really has to reach the card, and a failed read
draws a page with no lines.

**Probable cause, and it leads back to us:** we forced the vendor's
lines-per-page from 8 to 12 by patching the cmd 1 payload. `ebook_calculate_pages`
builds its page index INSIDE the `.bmk` using that divisor, so the file was
written under one geometry and read back under another -- seeks past the end,
the EIO storm, blank pages. That patch is now removed; it never worked anyway,
since measured turns still moved the line by 8.

**Two lessons, both expensive:**

1. When the vendor's own log reports errors, run the stock control BEFORE
   attributing anything to your code. Those EIO lines were in the first full
   capture and were read past for an hour.
2. A control test only clears what it actually removes. Reverting the firmware
   left the corrupt state ON DISK, so "stock reproduces it" meant "not our
   running code", not "not our fault". **A corrupt `.bmk` survives reflashing**,
   which is why unrelated builds all produced "no change". Delete the `.bmk`
   as a first-line recovery step, and always alongside any change to the
   vendor's page geometry.

Real bugs that the hunt did turn up, all worth keeping:

- A 768-byte page buffer as a LOCAL on the ebook thread, whose stack is 2280
  bytes with 328 ever unused (`kernel threads` reports the watermark). Now a
  field in the heap-allocated state, sized 512 -- a reflowed page consumes
  200-280 bytes.
- `recover()` matched state blocks by magic alone, and the LVGL heap survives a
  reset, so after any struct change the old block was adopted and every field
  past the change was read at the wrong offset. The block now carries its own
  `sizeof`, checked on recovery. This one masked several experiments: results
  came from a device running a corrupt state, which is why unrelated changes
  all produced "no change".
- `lv_label_set_text` has two variants here: `0x100fe944` copies (strlen +
  realloc) and is what the vendor uses for status widgets; `0x100ec576` stores
  the pointer and takes a flag. Using the pointer variant on the counter
  crashed the device on book open, and calling the copying one every render
  churns the heap.

---

# The ebook module, mapped properly (tools/rdisasm.py)

Black-box probing had run out: every open question -- where turns arrive, what
bounds the reading line, how page contexts relate to the `.bmk` -- is about
vendor control flow. So the module is now mapped by reading it.

## Why a new disassembler was needed

A linear sweep is unusable on this image. ARM literal pools sit inside the code
and decode as plausible nonsense (`_reading_unload_resource`'s constants once
appeared as `ldrh r5, [r6, #0x30]`), and everything after the first pool
desyncs -- which silently produced an EMPTY call graph.

`tools/rdisasm.py` walks control flow instead: from each entry it follows
branches, queues targets, stops at returns, and records every `ldr rX, [pc,#imm]`
target as DATA so pool bytes are never decoded. Output is a function inventory
with callers, callees, tail calls, RAM statics and string literals.
`docs/ebook-module-map.txt` is the current dump: 52 named entries over
0x10047000-0x1004d000, 5056 instructions reached.

## The symbol map is less reliable than assumed

**20 functions inside the module have no symbol of their own** and currently
read as `neighbour+0xNN`. The extractor names a function from its own logging
call, so any function that never logs inherits the previous name:

```
0x1004977c  reads as _reading_scroll_event_cb+0xf8   (a widget builder,
                                                      called by scene enter)
0x10049990  reads as _reading_scroll_event_cb+0x30c
0x1004b6b4  reads as _read_file_line+0xf0            (the .bmk index writer)
0x1004c3f4  reads as ebook_reading+0x45c
```

**This casts doubt on an earlier conclusion.** The scroll detour was placed at
`0x10049684` because the symbol said `_reading_scroll_event_cb`, and it never
fired. If the real event callback is a different function inside that span,
that experiment proved only that the wrong address was hooked -- not that
turns bypass the scroll callback. Re-check before trusting "input does not
arrive by scroll".

## Incidental finds

- The reading view's fonts are files: `/SD1:C/fang16.font`, `fang18`,
  `sans16`, `sans18`, `you16`, `you18`, opened via `lvgl_bitmap_font_open`.
  A replacement UI would load one of these the same way.
- `ebook_decode_get_line` (`0x1004be64`) exists and was not previously noticed.
- `_ebook_view_layout` is the scene dispatcher and holds the font table.

## Still open

No instruction in the module stores to `[rX, #0x194]`, and nothing adds 8 to a
value and stores it, so the reading line is written through an interior pointer
whose base we have not identified. That, and the clamp that pins it at ~1%,
are the two questions the next pass should answer -- now that the call graph
and data references are trustworthy.

## Input path: three callbacks eliminated by probe, writer still unidentified

With the map trustworthy, the input question was attacked directly. All three
candidates are now ruled out by INSTALLED, byte-verified probes rather than by
reading disassembly:

| candidate | probe result |
|---|---|
| ebook-thread message | no message of any kind on a turn |
| `_reading_scroll_event_cb` (`0x10049684`) | 0 hits; never runs |
| `_reading_btn_event_cb` (`0x10048d64`) | 6 hits, all at scene setup, none on a turn |

And the static picture is contradictory: across the WHOLE image only three
instructions store to `[rX,#0x194]` -- one belongs to a music view, one is the
scene entry (runs once), one is inside the scroll callback that never runs. The
timer callback contains no stores at all. Yet the value moves by +/-8 per press.

The resolution is that the field is reached through a POINTER CHAIN. The scene
itself writes it that way:

```
0x1004a296  ldr   r2, [sp, #0xc]
0x1004a29c  ldr   r2, [r2]
0x1004a2a4  str   r3, [r2, #4]      <- the reading line, as +4 of an interior pointer
```

So any component holding that pointer updates the line with a small offset and
never mentions `0x194`. Offset searches cannot find it; this needs dataflow.

Two theories killed by measurement along the way:

- **"Two reader objects"** -- the pointer had been seen holding both
  `0x18007a00` and `0x18007c00`, so the deltas might have been sampling
  artefacts. Logging the source object with every delta showed **one object,
  27/27 samples**. Wrong.
- **"The scroll callback is the input path"** -- the map shows it holds the
  only runtime writer, so it looked certain. The probe says it never executes.

## Current workaround (heuristic, not a fix)

At the frontier of its pagination the vendor refuses to advance its line: a
press moves it +8 and it pulls it back -8, which the reader followed as a page
back -- the 0.9 <-> 1.0% bounce. A negative delta arriving within ~3 frames of
a forward turn is now rejected as that rebound. A real back press never lands
that fast. This restores usable reading; it does not explain the behaviour.

## Why the reader stalls: our hook gets switched off

Traced with a sentinel write and a live state dump, and it explains the whole
family of symptoms.

**Our code stops being called.** With the reader stuck, `calls` was frozen at 44
while the vendor's page total kept rising (2023 -> 2036 -> 2049), both threads
`pending` and healthy, and the display timer alive with the right callback
(`timer+0x08 = 0x1004937d`, user data the reader object, repeat infinite).

**Because the render call is conditional.** The timer callback calls the render
function at `0x100493a8` only when the preceding decode succeeds (`cbnz` skips
it). Our hook rides on that call, so when the vendor's decode fails, our reader
goes silent while everything around it looks fine.

**And its decode fails because we share its file handle.** We `fs_seek` before
every read; its decoder reads from the same handle and lands in the wrong place
-- which is what fills its log with `file seek/read error (-5)`. One chain
explaining the blank pages, the stall, the EIO storm and the "not responding".

**Likely the deeper reason: cross-thread use of one handle.** Our reads run on
the EBOOK thread (from the message-loop hook) while the decode runs on the
DISPLAY thread (from the timer). Two threads seeking one file handle with no
lock will corrupt each other's position regardless of who is "careful".

## The page-turn handler (found)

`0x100495d8` -- an unnamed function that reads as `_ebook_return_btn_event_cb+0xfc`
-- is the tap handler, registered by the scene alongside the scroll callback
(literals at `0x1004a3fc` and `0x1004a400`). It computes the next page from the
line, clamps it, and jumps:

```
0x1004966a  ldr.w r1,[r5,#0x198]   ; line
0x10049674  asrs  r1, r1, #3       ; page = line / 8
0x10049676  adds  r4, r1, #1       ; next page
0x10049624  ldr.w r1,[r5,#0x19c]   ; total pages
0x1004962a  it ge / mov r4, r1     ; CLAMP
0x10049648  bl 0x100eb534          ; go to page r4
```

So the line is RECOMPUTED from a page number, never incremented -- proven by
writing 4096 into it and pressing next, which produced 8. That is why no `+8`
store exists anywhere and why every offset search failed.

## Dead ends recorded

- `fw_get_shared_info` at `0x100ff07f` **reboots the device** when called as
  `(name, buf, size)`, even alone, even though `_reading_btn_event_cb` calls
  `0x100ff07e` exactly that way at `0x10048d96`. Address or precondition wrong.
- The book path is **not retained in RAM** while the book is open (a sweep of
  `0x18000000-0x18020000` and `0x01000000-0x01010000` finds `/SD1://EBOOK.LIB`
  and the font paths, but no book filename), so a second `fs_open` cannot get
  its path passively.

## Next

Test the cross-thread hypothesis first, since it needs no path: do our file
reads on the SAME thread as the vendor's decode, or take the FS lock around
them. If the vendor's decode stops failing, the stall goes with it.

# The filesystem layer, mapped (0x1007f900-0x10080200)

Read because every recent failure lived here: a handle copy that did not work, a
getter that reboots, a decode that breaks when we seek.

## Shape

`fs_read`/`fs_seek`/`fs_write`/`fs_close` are thin dispatchers through a vtable
reached as `file->mp->fs` (`[r0,#4]` then `[r3,#0x1c]`), with slots:

```
+0x00 open   +0x04 read   +0x08 write  +0x0c lseek
+0x10 tell   +0x14 truncate  +0x18 sync
```

**There is no `tell` wrapper in this build** -- the slot exists, nothing calls
it. So a save/restore of the vendor's file position is not available, which
kills that approach outright.

Each wrapper starts TWO BYTES before its push, with `ldr r3, [r0, #4]`. A
prologue scan lands after that and makes the function look like it takes the
mount pointer in r3. It does not: `fs_read(file, buf, size)` is correct, and our
existing addresses (`0x1007fd3d`, `0x1007fde1`) are right.

## No locking, anywhere

None of the wrappers take a mutex. The FS layer does **not** serialize
concurrent access, so two threads sharing one `fs_file_t` will corrupt each
other's position -- our reads run on the EBOOK thread, the vendor's decode on
the DISPLAY thread. That is the cross-thread hypothesis confirmed, and it means
there is no FS lock to take: the fix has to be a separate handle.

## fs_file_t, corrected

```
+0x00 filep   +0x04 mount point   +0x08 flags   +0x0c FILE SIZE
```
(the size at `+0x0c` read 0x78f69 = 495465, matching the open book)

## A second open IS allowed

`fs_open` rejects only a handle already in use (`ldr r2,[r5,#4]; cbnz r2` ->
error -0x10). A fresh `fs_file_t` opens fine. It also requires a path of length
> 1 starting with `/`.

## The way around the missing path: open by cluster

`fs_open_cluster` (`0x1007fc4c`) opens a file by **cluster and directory entry**,
no path string required -- and the ebook app logs exactly those values when it
opens a book (`file topdir: %s, cluster: %d, entry: %d`). Since the book path is
NOT retained in RAM, this is the route to our own handle.

Next: locate the topdir/cluster/entry the app holds, then call
`fs_open_cluster` with them.

# The private file handle, and what the trampoline proved

## The stall was ours, not the vendor's

A pure-observer RAM payload (no calls into our own code) settled months of
guessing in one run: with our reader out of the loop, **the vendor's reading
line advances perfectly on every press** -- 0, 8, 16, 24, 32, 40, 48, 56. Its
input path and its pagination are healthy. The stall exists only when our code
runs.

The mechanism: we shared the vendor's file handles. Our `fs_seek` moves the
position under its decoder; its decode then fails; and the render call we hooked
is CONDITIONAL (`cbnz r0` at 0x100493a4 skips it when the fourth decode returns
non-zero), so our reader was silently switched off. Traced directly: `calls`
frozen while the ebook thread spun, both threads healthy, timer alive.

Two fixes followed:

- **Hook the timer TAIL** (`0x100493b2`, the `b.w` to the timer-resume helper)
  instead of the conditional render call. It is reached on every tick whatever
  the decode does, and still runs after the vendor has drawn.
- **Share nothing.** No reads, no writes, no seeks on the vendor's book handle
  or its `.bmk` handle -- not even as a fallback. Position persistence is
  disabled until it has its own handle.

## Opening our own handle: the bug that cost hours

`fs_open` worked from the first attempt. The acceptance test did not:

```
entry 0: rc=-2      entry 1: rc=-2
entry 8: rc=0       <-- the open book, opened fine
         size=0     <-- and REJECTED on this
```

`fs_file_t+0x0c` holds the file size only on the vendor's long-lived handle; on
a freshly opened one it reads 0. So the correct file was opened, judged a
mismatch, closed -- sixteen times per attempt -- leaving nothing to read and a
blank page. It looked exactly like a filesystem failure.

The file is now identified by **probing its length**: the byte at `size-1` must
read, and one byte at `size` must not. That needs no field the FS may not fill.

## Still open

- Page turns take ~2 s with a healthy handle (`file_ready=1`, `io_fail=0`), so
  the cost is elsewhere -- the per-prepare signature read at offset 0 is a
  candidate.
- It still loops around 1%: `cur.start` sits at 4845 with `last_line=8`, which
  is turn detection, not I/O.
- Per-book resume is off until the bookmark gets its own handle.

# Building our own scene: the toolkit is complete

Everything needed to stop borrowing the vendor's view and build our own is now
identified. This is the answer to "can we bypass the vendor app rather than
fight it" -- yes, and at the SCENE level it needs no new capability.

## Widget creation (canonical LVGL v8, intact in this build)

```
lv_img_create(parent)  @ 0x100a3170:
    mov  r1, r0                     ; parent
    ldr  r0, =0x1012c7d4            ; the widget CLASS
    bl   0x10096e20                 ; lv_obj_class_create_obj(class, parent)
    bl   0x100f7924                 ; lv_obj_class_init_obj(obj)
```

- **`lv_obj_class_create_obj` = `0x10096e20`** (class, parent) -> obj
- **`lv_obj_class_init_obj`   = `0x100f7924`** (obj)

Per-widget wrappers mostly do not exist (the UI is resource-driven), but they
are not needed: any widget can be built from its class.

## Classes

| class | what |
|---|---|
| `0x1012bee0` | `lv_obj` -- the base, for containers |
| `0x1012c7f0` | label (the page counter's inner widget) |
| `0x1012c7d4` | image |
| `0x1012c828` | the counter's outer widget (textarea) |
| `0x10129b54` | the reading list's own text widget |

## Input -- the piece that has blocked everything

```
0x1004a24e  movs r2, #0x0b          ; filter: LV_EVENT_SCROLL
0x1004a252  bl   0x100f687c         ; lv_obj_add_event_cb(obj, cb, filter, user_data)
0x1004a258  movs r2, #4             ; filter: LV_EVENT_SHORT_CLICKED
0x1004a25e  bl   0x100f687c         ; ...for 0x100495d8, the tap handler
```

**`lv_obj_add_event_cb` = `0x100f687c`** (obj, cb, filter, user_data).

Four probes failed to find which vendor callback receives a page turn (the
scroll cb never runs, the button cb only fires at scene setup, no message
reaches the ebook thread, and the tap cb at `0x100495d8` never set our flag).
On our own widget that question disappears: we register the callback, so we
know exactly when the user pressed and on which object.

## Text and fonts

- `lv_label_set_text` (copying) `0x100fe945`; the static-text variant is
  `0x100ec577` and takes a flag -- using the wrong one crashes on book open.
- Fonts are files: `/SD1:C/fang16.font`, `fang18`, `sans16`, `sans18`,
  `you16`, `you18`, opened with `lvgl_bitmap_font_open`.

## Why scene replacement, not a separate app

Launching our own app from the launcher means reversing app registration and
lifecycle. Replacing the reading SCENE means hooking
`ebook_scene_reading_enter`, building our own container, labels and event
callbacks, and never letting the vendor's view exist. Same result for the user,
far less unknown territory -- and it removes, by construction, every mechanism
behind tonight's bugs: shared file handles, the vendor's line counter, its
decode gating our hook, and its scroll recompute.

## First step

A single label of our own on the reading screen, with our own font, drawn from
our code -- no vendor widget involved. Everything above is address-resolved;
the SDK provides the matching API semantics.

## First scene-replacement attempt: crashed, and what it established

A RAM payload created a label of our own (`lv_obj_class_create_obj(label_class,
container)`, then `lv_obj_class_init_obj`, then set text) from the render tail.
**Entering a book crashed the device.**

Confirmed by reading the function afterwards -- the call itself was right:

```
lv_obj_class_create_obj(class in r0, parent in r1) @ 0x10096e20:
   instance size = ubfx([class+0x18], 4, 16)      ; label class -> 0x4c bytes
   lv_mem_alloc(size)  (0x100a0644)
   memset
   obj->class_p = class ; obj->parent = parent
```

So the remaining suspects are `lv_obj_class_init_obj` (`0x100f7924`) and, more
likely, **where we called it from**: our tail hook runs inside the vendor's
timer callback, after its render -- creating objects while LVGL is mid-draw is
not safe. Scene construction belongs in the scene-enter path, not the render
pass.

Next attempt should build the widgets from a hook on `ebook_scene_reading_enter`
(or the line-height hook, which already runs during layout), not from
`after_render`.

## The file list is at 0x18007000 — and only sometimes a file list

`book_open_own` builds its path from the picker's list: 0x100-byte entries,
filename at +3. The base was **0x18007800 for days, and every open failed.**

0x18007800 is inside the buffer the vendor reuses for **book text** once reading
begins. Dumping 0x18006000–0x18008000 with a book open showed what we were
actually reading:

```
[ 0] 'eed he took, '      <- 0x18007800, mid-sentence
[ 8] 'uromancer sho'
```

and the real list 0x800 lower, holding both books verbatim:

```
0x18007003: 'The Last Town - Blake Crouch.txt'
0x18007103: 'Neuromancer short.txt'
```

So the reader was opening `/SD1://uromancer sho` sixteen times per render pass,
failing all sixteen, and — since the private-handle build never falls back to the
vendor's handle — reading nothing at all. Two symptoms, one cause:

- **the 1% stall**: `file_ready` never set, so no page could ever be filled;
- **2 s per page turn**: `open_try` reached 1117 sweeps, i.e. ~18,000 failed
  `fs_open` calls, on the thread that also serves page turns.

Two lessons, both already paid for:

1. **A RAM address is only valid in a context.** This one is a file list while
   browsing and a text buffer while reading — the exact window our code runs in.
   Re-verify addresses under the workload that uses them, not at the menu.
2. **Retries hide the failure.** Retrying the open every render pass turned a
   hard error into a slow, silent one. Failures are now capped at 8 attempts.

## Reading state by name, not by offset

Two rounds of diagnosis were wasted on hardcoded struct offsets: adding
`my_file` shifted every field after it, and the dump printed `file_ready = 101`,
`nlines = 4998`. `bf07-work/state.py` now reads offsets **and sizes** from DWARF
in the build under test (`-g` costs nothing — `objcopy -O binary` drops it), so
a field cannot silently move out from under a measurement:

```
+0x041 file_ready (1B) =  0
+0x044 open_try   (4B) =  1117
```

## Input: the vendor dispatches above LVGL, not through it

For days no probe could find where a page-turn press went. Measured, with the
book open and the paginator quiet:

- pressing next changed **zero words** in the reading scene object, its label
  container, or the ebook statics;
- the vendor's `reading_line` never moved (its decode aborts, and the same
  `cbnz` skips the turn completion and our render call together);
- no LVGL object callback fired; `_reading_scroll_event_cb` never fires;
- meanwhile **exit worked**, so input plainly reached the firmware.

The firmware has a gesture/view layer above LVGL (`gesture_scroll_begin`,
`"gesture %d, start (%d %d), view %d, last_view %d, pre_view %d"`). Its
dispatcher at `0x100d92e8` takes the gesture context as its **first argument**,
so it must be hooked, not polled. Hooking it captured **nothing**: that layer
serves the swipeable multi-view UI, not the reader.

The touch driver one level down is the real source. **`_lvgl_pointer_put`
(`0x100e07b4`)** is called with a point struct:

| offset | field |
|---|---|
| `+0x00` | x (int16) |
| `+0x02` | y (int16) |
| `+0x08` | press state (1 = down) |

Captured on hardware (176x264 screen): taps on the right came in at
**(140,133)** and **(139,134)**, a middle tap at **(84,145)**. It is also called
on idle polls at ~1.3/s with all-zero words -- which is why a first attempt that
decoded assumed offsets saw only zeros and proved nothing.

The reader now turns its own pages from this: right third forward, left third
back, edge-triggered (a hold repeats samples), with the top and bottom strips
left to the vendor's own icons. **The vendor's reading_line is no longer used as
the turn signal**, which removes the last dependency on a decode that cannot
succeed while our page holds more lines than its context.

### Why our logging is invisible

`fw_log` output does not reach the UART, though the vendor's own logging does.
Every "log for N seconds" measurement in this project was therefore blind, and
several conclusions drawn from silence were worthless. Capture into state and
read it over `dbg mdw` instead -- that is what found the touch layout.

## Working reader: what actually drives it now

Confirmed on hardware: the book opens, pages advance and go back, the position
resumes after leaving and re-entering the book, and progress moves past 1%.

Nothing in that path depends on the vendor's reader:

| concern | owner |
|---|---|
| file handle | ours (`book_open_own`, opened by length probe) |
| wrapping / pagination | ours (`wrap_one`, byte extents per page) |
| page turns | ours (touch driver hook, left/right thirds) |
| progress display | ours (tenths of a percent, from byte offset) |
| drawing | ours (12 labels, written every render pass) |

The vendor is told a page holds **8** lines while we draw **12**. Those no
longer have to agree: its line counter is not a signal we consume, it only has
to stay a value it can service so its decode does not overflow its 8-record page
context and its paginator terminates.

### The press latch -- the real "stall at 1%"

For days the reader advanced a little and then stopped, at a different offset
each session. The cause was in our own touch handler, one line early:

```c
if ((w0 | w1 | w2) == 0) return;   /* idle sample -- returned HERE */
...
was = S->touch_down;               /* never reached on release */
S->touch_down = down;
if (!down || was) return;          /* so `was` was always 1 */
```

Finger-up arrives as an all-zero sample, so the release returned **before**
clearing `touch_down`. It latched at 1 and every later press looked like a
continuing hold. The first tap after boot worked; the rest were swallowed --
except when a release happened to carry coordinates, which is why the reader
advanced sporadically and looked like a slow percentage rather than a bug.

Measured while stalled: **18 taps recorded, `cur` frozen at `[2585..2783]`,
`sp` stuck at 12, `want` never set.** Counting the taps separately from acting
on them is what made this visible.

### Debounce

One physical tap could register twice when contact bounce split it into two
press edges around a brief idle sample. Turns are gated on `S->calls` (the
render pass, ~4/s) advancing by 2 -- about 500 ms, shorter than the e-ink
refresh each turn triggers. The DWT cycle counter would be a truer clock, but
`CYCCNT` is stopped on this device and starting it means writing core debug
registers at runtime.

## Do not write the vendor's counters

A build published our own totals into `total_lines` (0x1801a030), `total_pages`
(rd+0x19c) and `reading_line` (rd+0x194) every render pass, to give the "select
page" dialog a range after we had disabled the paginator.

That corrupted the count the paginator was building. Measured while it scanned,
`total_lines` oscillated **8664 -> 8672 -> 8736 -> 8808 -> 8880** as our writes
fought its increments, and its page count collapsed to **1** -- so the keypad
dialog had a range of one page and every digit was a no-op. With the writes
removed it climbs monotonically (72 -> 1312 at ~27 lines/s).

**The vendor's bookkeeping is its own.** Our progress display and page extents
are byte offsets and need none of it.

### The paginator was never the problem

It was disabled because it scanned forever. That was our bug: we told the vendor
a page holds 12 lines, and `ebook_calculate_pages` loops on exact equality
against a divisor of 8, so it could never terminate. Told the truth (8), it
finishes and stops -- one bounded scan per book open, as stock always did.
On a 255 KB book that is ~5 minutes at ~27 lines/s, and `total_pages` stays 0
until it completes.

## Popup detection

The reading menu is a popup INSIDE the reading scene, so taps kept falling
through to the book underneath. Four gates were tried and failed:

| gate | why it failed |
|---|---|
| scene pointer (`app_global+0x3c`) | unchanged while the popup is up |
| container identity vs what we drew | circular: our render reads the same field the popup repoints, so it follows the popup and agrees |
| tap inside the page rectangle | the page rect is (4,24)-(179,263) -- the whole screen, keypad drawn inside it |
| page is topmost child of the screen | a sibling (0x01004314) sits above our branch even while reading, so this blocked every tap |

What works: **the popup is added as an extra child of the page's PARENT.**
Compare the sibling count against the fewest ever seen while actually drawing
the page (the minimum, so a popup open during a render cannot raise the bar).
Measured: 6 siblings while reading, 7 with the keypad up.

## Percent seek: the vendor's keypad, our logic

The vendor's "select page" dialog is built from its paginator's page count, which
costs a ~5 minute scan per book. A percent seek needs none of that: our pages are
byte extents, so a destination is just an offset.

So the dialog is treated as a **surface**, not as logic. Its taps already arrive
at our touch hook with coordinates; we read the keypad ourselves.

Grid, measured by tapping 1-9 then 0 (consecutive duplicate samples are the press
and release of one tap):

```
 (34,155) (89,157) (147,156)      1 2 3
 (29,190) (89,189) (151,188)      4 5 6
 (29,221) (86,221) (148,220)      7 8 9
  [bksp]  (74,259)  [enter]         0
```

Columns at x ~31/88/149; rows at y ~156/189/221; 0 centred at y 259, with
backspace to its left and enter to its right.

- digits type into a percent, shown live in the top line (`show_percent`
  displays the target being typed instead of the current position);
- backspace divides it by ten, so our number tracks what is on screen;
- enter seeks to `(size / 100) * percent` and snaps forward to a word break;
- closing the dialog with a number typed commits the same seek, so the feature
  does not depend on identifying the enter key.

**A jump clears the back stack.** Pushing the pre-jump position made the first
back tap teleport to where the jump came from; after a deliberate jump, back
should mean "the page before this one", which the `want_prev` path computes
exactly from the current offset.

The paginator stays disabled: nothing here consults a page count.

## Text layout: measured, not estimated

### Glyph widths come from the font

`bitmap_font_get_glyph_dsc_cb` (`0x100e1348`) is the renderer's own glyph
lookup: `(font, dsc_out, letter, letter_next)`, writing the advance at `dsc+0`,
then `box_w +2`, `box_h +4`, `ofs_x +6`, `ofs_y +8`, `bpp +10`.

The font pointer cannot be looked up -- it is reached through style lookups we
have no symbols for -- so it is **captured** by hooking that callback and
keeping its first argument. The alphabet is then measured once and cached.

**The advance is in WHOLE PIXELS here**, not the 8.4 fixed point upstream LVGL
documents for `adv_w`. Read back from the device: `i` 4, `e` 8, `m` 12, `W` 14,
space 4. Treating it as sixteenths made every glyph ~8x too narrow and ran text
off the screen edge.

### Three separate causes of ragged lines

All three were found by reading the rendered page out of our own state and
re-measuring it with the same table the wrap used:

1. **A trailing space evicted a word.** `"...shouldered his"` measured 162px
   against a 164px limit; adding the *following* space reached 166, broke the
   line, and the backtrack to the previous space dropped `his`. A trailing space
   is invisible, so it now ends the line and is swallowed.
2. **UTF-8 charged per byte.** Every byte >= 0x80 cost the 11px fallback, so a
   curly quote cost ~33px for a glyph drawn in 3-5px. Continuation bytes now
   cost nothing and the common punctuation is measured:
   `' ' = 3, " " = 5, en 7, em 14, ellipsis 14, nbsp 4`.
3. **A 4px safety cushion.** Justified when widths were estimated; with measured
   widths it only discarded words -- `"Dedication to"` left 81px free while
   `"commerce?"` needed 84. The labels were read off the live object tree at
   exactly **168px** (x 4..171), so the full width is used.

Hyphenated compounds also split now: a hyphen **between two letters or digits**
is a break candidate, and the wrap breaks at whichever comes later, the hyphen
or the last space. `seven-function` / `force-feedback` were stranding 70-83px of
empty line. The letter/digit test excludes dashes used as punctuation, leading
minus signs, and the `--` em-dash spelling.

### Vertical position

The font ships `base_line = -2` with `line_height = 17` in a 20px label. LVGL
draws each glyph at `y + (line_height - base_line) - box_h - ofs_y`, so a
negative base line pushes every glyph **down**: descenders clipped at the bottom
while 2px went unused above. Setting `base_line = 0` lifts the text 2px and
fixes it. Re-applied on every render pass, because the font is shared and
reloaded when the user changes it.

Tools: `tools/screen.py` dumps the rendered page with per-line widths and says
whether the next line's first word would have fitted -- which is how each of
these was proved rather than guessed.

## Who owns the labels, and why the page used to flicker back

The vendor **refills all 12 labels on every render**. Proved by reading their text
pointers at `label+0x24`: they pointed at its own line buffers (`0x180190cc`,
`0x18019140`, `0x180191b4`, spaced 0x74 apart), not at our page.

For a long time our render simply rewrote every label on every tick, which hid
this at a heavy price: LVGL was invalidating twelve labels ~3 times a second and
the panel never stopped redrawing. Measured: **3.3 render passes/s, 304 ms per
tick** — and since a tap can only appear on the next pass, that idle churn set
the floor on responsiveness.

Gating the repaint on "did the page actually change" exposed the underlying
conflict immediately: a turned page appeared instantly, then ~0.3 s later the
vendor repainted **the page the book was opened on** over it (its reading line
never moves, because we stopped consuming it as a signal).

The fix is to stop it drawing at all. It is told a page holds **12** lines —
deliberately more than its 8-record page context — so its decode fails and the
`cbnz r0` at `0x100493a4` skips the render call at `0x100493a8`. It never draws,
and its decode and layout work disappear from every tick.

That same lie was catastrophic earlier in the project and made the paginator spin
forever. Neither applies now:

| old failure | why it no longer applies |
|---|---|
| our render hook rode on the skipped call | we hook the timer TAIL (`0x100493b2`), reached unconditionally |
| `ebook_calculate_pages` never terminated | the paginator is disabled |
| its `reading_line` froze | we do not consume it; input comes from the touch driver |

**Result: 10.0 render passes/s, 100 ms per tick — 3x faster**, with the vendor's
decode, layout and drawing all gone.

### The labels self-heal

Before skipping a repaint we check that each label's text pointer at `+0x24` is
still ours (`lv_label_set_text` is the *static* setter, so it stores our pointer
rather than copying). Twelve word reads per tick, against a full repaint per
tick. If anything ever takes the labels again, the next pass notices and
repaints — the bug above cannot come back silently behind an assumption.

## Page turn latency, end to end

| stage | cost |
|---|---|
| tap -> `want` set (touch driver hook) | immediate |
| page content | **already built** — `nxt` is pre-rendered right after each swap, so `nxt.start == cur.end` |
| swap into `cur` | a `memcpy`, on the next render pass (<= 100 ms) |
| e-ink refresh | the hardware's own, ~0.5-1 s |

Page turns never waited on file I/O. What made them feel slow was the redraw
cycle above, not preparation.

## Position across a power cycle: our own bookmark

Resume worked within a session and failed after a reset: the book reopened at
the beginning.

Both halves of the cause were already known, just never put together:

- **Our state does not survive a reset.** It lives in the LVGL heap, and the
  marker test proved RAM is cleared on reset.
- **The vendor's `.bmk` cannot stand in.** We keep its decode failing on purpose
  (12 lines into an 8-record context), so its `reading_line` never advances.
  Measured after a reset: `reading_line = 0`. It faithfully saves 0.

So the reader keeps its own bookmark: `/SD1://bf07read.pos`, a table of 8
`(book signature, byte offset)` records. Signature rather than filename, because
a book is already identified by hashing its first 64 bytes -- so several books
each keep their place, and renaming a file does not lose it.

- **load** once per book, as soon as the private handle is open;
- **save** whenever the page settles and the offset differs from what was
  written, so an abrupt power-off loses at most one page.

Both run on the **ebook thread**, where file I/O is legal; the display thread
must never touch the filesystem.

## Hyphenation: Knuth-Liang, English and French

Patterns live in flash and are read through XIP, so they cost **no RAM** — which
was the deciding constraint, since the LVGL heap could not spare 8 KB, let alone
35 KB.

| | |
|---|---|
| English | 4,938 patterns, 26,611 bytes packed |
| French | 1,216 patterns, 8,285 bytes packed |
| reader total | 48,262 bytes of a 53,248-byte window |

### The tables are separate, deliberately

Fusing them was measured and rejected. Knuth-Liang patterns *compete*: values are
max'd across matches, odd allows a break, even inhibits one. A union therefore
opens breaks one language forbids and suppresses breaks the other needs. On
4,000-word samples a fused table left only **62% of English and 47% of French**
words correct, producing `cat-ti-sh-ly`, `en-vy-in-gly`, `éli-m-i-nais`.

So the language is chosen **per book** instead, from the opening 512 bytes:
accented characters score strongly, with French/English stop words settling
books that open on an unaccented passage. Verified on prose, including French
written without accents.

### Packing

Sorted patterns are front-coded (one byte of `shared<<4 | suffix_len`, then the
suffix), values are stored per pattern, and a **fixed-size** sparse index every
32 patterns carries *both* stream offsets — the values stream is variable length
per pattern and cannot be indexed any other way. Raw 31 KB becomes 26.6 KB for
English.

### How it was verified

`tools/test_hyphen.c` compiles the **device** implementation natively, and
`tools/mkhyphen.py` contains a plain-Python reference. Diffing them across 5,000
dictionary words per language is what makes this trustworthy — and it earned its
keep immediately: the block scan originally stopped at exactly `stride` entries,
so a pattern just past a block boundary was invisible and the prefix pruning
then concluded nothing longer could match. **99 of 5,000 English words were
wrong.** Scanning across the boundary is safe, because a block start re-encodes
its string whole. Both languages now match the reference 5,000/5,000.

Words containing characters outside the pattern alphabet (notably `-`) are left
alone. That is why compounds like `court-circuiteras` are not split internally —
the wrap already breaks them at their explicit hyphen.

**Headroom is now ~5 KB.** Anything substantial added next needs either a
smaller pattern set or space beyond `0x1f4000`.

### The ebook thread's stack, again

Adding French meant sampling a book's opening text to pick the language, and the
first attempt read it into a **512-byte local**. The device then reset on every
book open.

That stack is **2280 bytes and has historically had ~328 to spare** — a 768-byte
buffer caused exactly this crash earlier in the project, and the fix then was the
fix now: read into the heap scratch page (`S->nxt.text`, rebuilt immediately
afterwards) instead of the stack. `hyphenate()`'s own frame was also trimmed by
dropping `HY_MAXWORD` from 48 to 32, since it runs on the same thread; words
over 30 characters are simply left unhyphenated.

The lesson is not "avoid big locals" — it is that this constraint was already
written down, and the code was written without re-reading it.

## User fonts: the vendor's loader cannot see the volume you can

The goal was fonts a user installs themselves, over USB, with no soldering and
no opening the case. That took three attempts, and the first two failed for
reasons worth keeping.

### The storage the host sees is not the storage the fonts live in

The vendor's fonts sit in the sdfs container at card offset `0x10000`, inside the
hidden region *before* the FAT partition. When the player is in disk mode it
exposes **55,609,941 sectors with no partition table** — and the FAT partition on
the card begins at LBA **5,457,067**. The exposed LUN size equals the partition
size exactly, so sector 0 of the host's view *is* the start of the partition and
everything before it — sdfs, every vendor font — is unaddressable from a host at
any offset.

So the fonts cannot be replaced over USB, and a font dropped on the visible drive
cannot be named by any vendor font path.

### Attempt 1: redirect the vendor's loader. Dead end, and the UART said why

Hooking `lvgl_bitmap_font_open` (`0x100e1440`) and swapping the `fang18` path for
`/SD1://custom.font` fails:

```
sdfs:stor_id=1, p=255
sd_fopen no this file /SD1://custom.font
<E> bitmap_font_open:  open font file /SD1://custom.font failed!
```

The loader opens through **`sd_fopen` — the sdfs resource filesystem — not Zephyr
FS**. Our own `fs_open` succeeds on that identical path, which is exactly what
made the idea look sound: two different filesystem APIs, one path string. No
spelling of the path can name a FAT file to `sd_fopen`.

This is also the answer to "why not just add a menu row": the row table is
understood (below), but a new row would still have to name a file the loader
cannot open.

### The free-on-failure trap

A failed open must not be "cleaned up". `lvgl_bitmap_font_open` **frees its own
descriptor before returning -1** (`0x100e14c0`), so a fallback that calls
`lvgl_bitmap_font_close` afterwards frees it a second time:

```
<E> res_fs_close: Null file handle to close
0x18006b40 freed already
rom_buddy_free: buddy_no 1, where 0x18006b40, info 0x(nil), offset 0x34
```

— and the player reboots. The safety net *was* the crash. Note the menu handler
at `0x1004ae28` does `close; open; if (rc) close;` on its own, which looks like
the same bug but is not the one that fired here.

### Attempt 2, which works: our own glyph backend

The reader reads the file itself with `fs_open` (proven to work on that path),
keeps it in the LVGL heap, and answers LVGL's two glyph callbacks directly. The
vendor's `fang18` still opens normally underneath and stays loaded as a fallback;
our callbacks are installed **after** its open succeeds, never instead of it, so
the struct is always fully initialised.

Both layouts were read from the stores in the vendor's own callback at
`0x100e1344`, not assumed from upstream LVGL:

| struct | layout |
|---|---|
| `lv_font_t` | `+0x00` get_glyph_dsc, `+0x04` get_glyph_bitmap (**dsc first** — v8 order), `+0x08` line_height, `+0x0a` base_line (int16), `+0x14` dsc |
| `lv_font_glyph_dsc_t` | `+0` adv_w, `+2` box_w, `+4` box_h, `+6` ofs_x, `+8` ofs_y, `+10` bpp; returns 1 on success |

`adv_w` is in **whole pixels** in this firmware, which is also what `mkfont.py`
writes, so advances pass straight through.

The file's glyph bitmap is already a continuous 1 bpp stream, but it starts
26 bits into the glyph record, so each glyph is shifted into a small buffer that
LVGL can read directly.

### Generating a font that survives 1 bpp at 13 px

`tools/mkfont.py` renders any TTF into the same `head`/`cmap`/`loca`/`glyf`
format the device already loads. Two separate things make small monochrome text
look blotchy, and both must be handled:

1. **Stroke contrast in the design.** A face drawn for large text has stems much
   heavier than its hairlines; at 13 px the grid rounds those to 2 px and 1 px,
   so the contrast reappears as *weight*. Literata's 18 pt cut gives `b` a 2 px
   stem against 1 px hairlines. Use a small optical size — for a variable font,
   the smallest `opsz` (Literata: 7).
2. **Grid fitting.** Unhinted, each stem rounds independently: caps land on 2 px
   while lowercase lands on 1 px (`H` vs `b`), and one bowl comes out 1 px on the
   left and 2 px on the right (`e`, `o`). FreeType's autohinter normalises stem
   widths, which is why this renders through freetype-py with `FORCE_AUTOHINT`
   rather than through Pillow — variable-font instances carry no usable
   TrueType hints of their own.

Render size is set by the line box, not by the name: at `opsz=7`, 13 px gives
ascent 16 / descent 4, matching the vendor's `you18` exactly.

### Widths must be re-measured when the font changes

The width table is measured once from the renderer's own font. Nothing announces
a font change — the vendor **reuses the same `lv_font_t`**, so the pointer never
changes while every width behind it does. (That reuse is also why `base_line` has
to be re-forced every pass.) Symptom when this is missed: after switching from a
wide font back to a narrow one, every line breaks 2-3 characters early, because
wrapping charges the old font's advances for the new font's glyphs.

Four glyphs (`m W i l`) are probed each draw pass and compared against the table.
On a mismatch the alphabet is re-measured, the page re-wrapped, and the repaint
forced — that last part matters, because a re-wrap keeps the same start offset
and often the same line count, which the redraw test otherwise reads as "nothing
changed".

### The menu row table

For a properly labelled sixth row, the structure is known: rows are **28 bytes**
at `0x10128e50`, the five font rows share group id `0xcefe2df9`, the callback is
the last word, and there is a **spare all-zero row at `0x10128edc`**. The blocker
is word 4 — a **label id into a localised string resource** that is not plaintext
in the firmware or the first 16 MB of the card, so an unknown id would draw
blank. Until that resource is found, the user font takes over the `fang18` slot,
shown in the menu as "Imitation Song large".

### Installing a font

`mkfont.py` a TTF, drop the result on the visible drive as `custom.font`, pick
that row. Nothing is written to the card, and the sdfs container stays stock.

### Relabelling the menu row

The row still said "Imitation Song large font" because the label is not in the
firmware. Tracing it end to end:

1. Each menu row carries a 32-bit **id**. `app_menulist_load_res_id`
   (`0x1005934c`) walks the rows at **stride 28** and stops at the first row
   whose id is zero, copying `+0x00`, `+0x08` and `+0x0c` into a 16-byte item.
2. The id selects a **36-byte record in `common.sty`**, whose `+0x08` field is
   a string index.
3. That index picks an entry from the per-language string file. `common.eng`
   holds 325 entries named `STR1`..`STR325`: a `RES` header, 16-byte entries of
   `offset(4) len(2) type(1) name(9)`, then the strings.

The six font labels are `STR157`..`STR162`, and the mapping is:

| id | string |
|---|---|
| `f40f37ef` | Song typeface |
| `f40f37ec` | Song typeface small font |
| `f40f37ed` | Imitation Song large font |
| **`f40f37ea`** | **Fangsong Small Font — referenced by no row** |
| `f40f37eb` | Young round large font |
| `f40f37e8` | Young round small font |

That unused pair matches the `fang16` font index that has no row either. So a
complete label+id was already built and wired with nothing pointing at it, which
is what makes the relabel two small patches instead of a relocated table:

- a **word patch** in `fw0_sys` repointing our row's id to `0xf40f37ea`, and
- a **string rewrite** in NOR (`tools/set_menu_label.py`), `common.eng` at flash
  `0x3b98d5`, turning "Fangsong Small Font" into "Custom".

The resources live in a second sdfs container in NOR at flash `0x299000`, mapped
to `0x12400000` while the firmware runs — which is how they were read at all,
since the flash copy is encrypted. `dbg mdw` on the mapped view shows plaintext.

**There is no spare row.** An earlier note here claimed the all-zero row at
`0x10128ee0` was a free slot; it is the array **terminator**, and the next
submenu's array begins 28 bytes later. A genuinely additional sixth row needs
the array relocated into our flash and a handler for font index 2.

### The write that ACKs but never programs

**The first `write_plain` issued after a run of `write_raw` is acknowledged and
never programmed.** The block stays erased, and the OTFD faithfully decrypts
`0xFF` into noise — which is how a menu label came out as `G   =`.

It survived three attempts because the natural check is "did this block change
from stock?", and an unwritten block differs from stock too. The same hole was
in `bf07.py install --patch`, which flagged a patched block only when it came
back *unchanged*.

Both now do the same two things:

- **encrypted blocks are written first**, before any verbatim restores, and
- **blocks are verified for what they ARE** — still `0xff`? — with a retry,
  rather than for having changed.

The general lesson is the one this project keeps relearning: an ACK is not proof
of a program, and "it changed" is not proof it changed *into the right thing*.

### Two bugs from installing the font at the wrong moment

Both were caught after the backend already worked, and both are about *when*
and *on which thread* it loads.

**Descenders clipped on a cold boot.** With the custom font already selected,
a fresh boot rendered with the descenders cut off; switching font and back
fixed it. Measured on the running device:

```
fo_calls = 0        our hook never saw an open this boot
cf_installed = 0    the backend was never installed
font +0x00 = 0x100e1345, +0x04 = 0x100e13bd    the VENDOR's callbacks
line_height = 24, base_line = 0
```

So the book was not being drawn with our font at all — it was the vendor's own
face, and the `base_line = 0` correction (which exists for *that* font at
line_height 17) pushed every glyph 4 px down inside a 20 px label. The ebook
opens its font before our state exists, so `fontopen_body` passed through with
`S` null and nothing installed; only a font switch ran an open through our hook.

Waiting for the open was the mistake. The font object says which file is behind
it: `font+0x14` → `dsc[0]` → the bitmap_font slot, whose path sits at `+8` —
the same string `bitmap_font_open` strcmps against when looking a font up. The
display pass now walks that chain, and installs whenever the loaded file is the
slot we took over. Whether the open happens before or after our state exists no
longer matters.

**Books stopped resuming after a reset.** The first version of the loader called
`fs_open`/`fs_read` from the display hook. Line 798 of `main.c` already said
this was not allowed:

> One bookmark read per book, then persist every settled page. Both run on the
> ebook thread, where file I/O is legal — **the display thread must not touch
> the filesystem.**

The font read raced `book_open_own`, the FS layer takes no lock anywhere, and
the bookmark was the casualty — the same class of failure as the original stall.
Loading is now split so each thread does only what it may:

| step | thread | why |
|---|---|---|
| `cfont_size` — walk chunks for the total | ebook | file I/O lives here; also validates before a buffer is committed |
| `lv_mem_alloc(cf_len)` | display | where every other allocation in this reader happens |
| `cfont_read` — fill and parse | ebook | file I/O |
| install into `lv_font_t` | display | pointer walking only, no I/O |

Accepted fonts are capped at 32 KB: the heap could not spare 8 KB for
hyphenation patterns, so a larger font would fail the allocation anyway, and
failing while sizing is cheaper than failing later.

### The label follows the file

The row's label id is a flash patch, so on its own it reads "Custom" whether or
not a `custom.font` exists — and with no file the row loads the vendor's fang18,
which is not custom anything.

The id is only static until it is *copied*. `app_menulist_load_res_id` builds a
16-byte item per row with the id at `item+0x0c`, so the reader wraps that
function and, when no file was found, rewrites `0xf40f37ea` back to
`0xf40f37ed` — the entry the row pointed at originally, still present and still
correct for the vendor's font. No flash write, and it is re-evaluated on every
menu build, so dropping the file on the drive is all it takes.

### What a missing font does

Every install path is guarded on `cf_ready == 1`, so an absent or malformed file
simply never installs: `fs_open` fails (or the chunk walk never reaches `glyf`),
nothing is allocated, and the vendor's loader serves the row normally.

One thing did NOT degrade gracefully, and it is worth keeping as a warning about
corrections written for one font. The display pass forced `base_line = 0` on
whatever font was loaded whenever ours was not installed. That was written for
the vendor's original font, which reported **-2** and pushed glyphs down into
the bottom of the label. Applied to the fang18 slot — line_height 24, base_line
legitimately positive — it clipped the descenders of the very font the no-file
case falls back to. The test is now `if (*base_line < 0)`, which reproduces the
original fix exactly and leaves other fonts alone.

Remaining rough edges, neither user-visible:

- a file that sizes correctly but fails to parse leaks its buffer until reboot
- the file is read once per boot, so replacing it while running needs a reset
