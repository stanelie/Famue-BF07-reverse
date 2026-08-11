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
