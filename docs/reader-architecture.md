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
