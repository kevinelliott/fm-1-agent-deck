# Agent Deck wire protocol v6

How the FM-1 (running the Agent Deck firmware, a Dinghy fork) and the host
daemon (`host/agtdeck.py`) talk. Everything runs over **USB-MIDI, channel 16**
(status byte low nibble `0xF`). BLE MIDI is a possible later transport — the
protocol is transport-agnostic — but for now it is USB only.

**Division of labour.** The device is a dumb panel: it reports every control
and renders what it is told. The host owns everything that needs judgement —
what a knob does to an agent, which prompt preset is armed, when to chime,
how far the detail text is scrolled. Only what must feel instant stays on
the device: the selection cursor, the page, the LEDs, the per-slot clocks.

v6 (firmware 0.6) is a superset of v5 (0.5), v4 (0.4), v3 (0.3), v2 (0.2)
and v1 (0.1); all
interoperate — see [Versioning](#versioning).

## Why channel messages, not SysEx

Dinghy's platform owns SysEx: the firmware updater (OTA) speaks SysEx, and
`midi_get()` in `platform.c` never delivers SysEx (or any system message other
than clock/transport) to the app. So host→device data **cannot** use SysEx
without forking the platform's USB stack and risking the updater. Instead:

- Device→host uses plain Note/CC messages (the HYP-1 "remote mode" precedent).
- Host→device uses a **framed byte stream packed into CC values**. Every CC
  data byte is 7-bit clean (0–127) by definition, and every payload this
  protocol carries is either a small integer (< 128) or printable ASCII
  (32–126), so no escaping/encoding layer is needed at all.

MIDI is slow (USB-MIDI is not the bottleneck here; the FM-1 screen is), so the
host sends deltas, keeps frames small (≤ 48 payload bytes), and refreshes the
device at most ~2–4 Hz.

## Device → host (channel 16)

| Message | Encoding | Meaning |
|---|---|---|
| Note On `0x9F` | note = 48 + key (48–74), vel = 127 | Piano key *k* (0–26) pressed. Also selects slot `page*27 + k` on the device — unless a button is held (v3): then it is a **chord**, the note goes out alone and the cursor stays |
| Note Off `0x8F` (or Note On vel 0) | note = 48 + key | Key released |
| CC 102–115 | value 127 = down, 0 = up | Buttons. CC = 102 + button id (Dinghy `BTN_*`): 102 OCT−, 103 OCT+, 104 FX, 105 SEL, 106 ENV, 107 LFO, 108 EDIT, 109 GLO, 110 HOME, 111 SAVE, 112 ARP, 113 SEQ, 114 PLAY, 115 REC. Both edges are sent, so the host can treat any button as a held modifier |
| CC 85–91 | relative: 65–127 = clockwise (value − 64 detents), 1–63 = counter-clockwise (64 − value detents), 64/0 = no-op | Encoders. 85 SELECT, 86 ALGORITHM, 87 K1, 88 K2, 89 K3, 90 K4, 91 PRESETS. SELECT moves the device's own cursor (reported as CC 120), so 85 is sent only where the host owns it: on the MENU page (v3), and always by v1, where the host ignores it |
| CC 121 | value = page (0 GRID, 1 DETAIL, 2 APPROVALS, 3 MENU, 4 ABOUT in v6) | Page changed on the device, or the answer to a HELLO_ACK. *v3* |
| CC 7 | absolute 0–127 | MASTER pot (device sends on change ≥ 2). It is the chime volume; the host ignores it |
| CC 116 | value = protocol version (v1 sent a constant 1, which is its version) | Heartbeat, every 1000 ms. This is how a daemon started after the device learns what it is talking to |
| CC 117 | value = protocol version | Hello, sent once at boot |
| CC 120 | value = slot index | Selection changed on the device (key press, SELECT encoder, or page change). v2 also sends it in answer to a HELLO_ACK, so a fresh daemon learns where the cursor is |

### What the panel does (implemented by the daemon)

| Control | Action on the selected agent |
|---|---|
| **Keys** | Select that slot (empty slots can be selected too) |
| **SELECT** encoder | Move the selection across occupied slots |
| **PLAY** | Start / continue. On an **empty slot**: spawn the default agent in a new pane; it lands in that slot |
| **REC** | Approve the pending prompt |
| **SAVE** | Approve *and don't ask again*: picks the prompt's numbered "Yes, and don't ask again / allow all…" option if one is on screen; otherwise does nothing and says so |
| **FX** | Deny (Esc) |
| **ENV** | Interrupt (Ctrl-C) |
| **SEL** | Enter |
| **K2** / **K3** | Up·Down / Left·Right arrow keys — with SEL, enough to answer any menu an agent puts up |
| **LFO** | Cycle the agent's mode (Shift+Tab) |
| **PRESETS** encoder | Arm a prompt preset (shown on the footer line) |
| **SEQ** | Send the armed preset as a prompt |
| **ARP** | Toggle auto-approve for this agent: the daemon approves its prompts as they appear. The row shows `A`, the ARP LED lights |
| **K1** | Scroll the DETAIL text back through the agent's output (the host re-sends the window) |
| **K4** | Jump the selection to the next agent that needs a human (BLOCKED, then ERROR, then DONE) |
| **EDIT** | DETAIL page; the daemon also focuses that pane on the computer |
| **HOME** / **GLO** / **ALGORITHM** | GRID page / APPROVALS page / cycle pages (device-local) |
| **OCT−/OCT+** | Slot page down/up (device-local; the host tracks it) |
| **GLO held +** REC / FX / ENV / SEQ / ARP | The same action for every agent it applies to: approve or deny all BLOCKED, interrupt all WORKING, send the preset to all IDLE/DONE, auto-approve off everywhere |
| **MASTER** | Chime volume (the platform's master volume) |
| **REC / FX / ENV held + key** | Chord (v3): approve / deny / interrupt *that* slot's agent, selection unchanged |
| **SEQ held + key N** | Chord (v3): send prompt preset N to the selected agent |
| **SEL held + key N** | Chord (v3): type the digit N (1–9) — answer a numbered option |
| **PLAY held + key** | Chord (v3): start / continue that slot's agent; on an empty slot, spawn one there |
| **SAVE held + key** | Chord (v3): move the selected agent to that slot (swap if taken); the host follows with a SELECT frame |
| **LFO held + key** | Chord (v3): mark / unmark that slot's agent. While any are marked, the GLO-held actions apply to the marked agents only; GLO + LFO clears the marks |
| **ARP held + key** | Chord (v3): toggle auto-approve on that slot's agent |
| **PLAY held + PRESETS** / **SEQ held + PRESETS** / **SEL held + K1** / **LFO held + K4** | An encoder turned while a chord button is held: choose the agent PLAY starts / set the repeat count / scroll DETAIL a page / step through the marked agents. The button then does nothing on release |
| **GLO** while already on the APPROVALS page | Select the agent that has waited longest (oldest BLOCKED, else ERROR, else DONE); the host follows with a SELECT frame. (HOME while already on the GRID is the device's own: it flips the GRID between rows and boxes, v6) |
| **K1** on the MENU page | Move the menu cursor a page (7 rows) at a time |
| **SEQ held ≥ 0.7 s**, no key | Toggle repeat mode: the armed preset is re-sent each time the agent comes to rest |
| **EDIT** on the DETAIL page | Cycle DETAIL through the agent's output, the host's INFO summary and its DIFF summary (the frames are the same DETAIL lines) |
| **MENU page:** SELECT / SEL / FX | Move the menu cursor / pick / back (the host draws the page with TEXT frames) |

A chord button acts on *release* when no key was tapped meanwhile (with a
v3 device; on press with older firmware, which cannot chord). While any
button is held the device shows the host's KEYLEDS instead of the slot
states, so the keys can show what a tap would do.

## Host → device (channel 16): the CC frame stream

Two CCs carry everything:

- **CC 118 — frame control.** Value `0x10 | type` (0x10–0x1F) = *start frame*
  of that type (aborts any partial frame). Value `0x20` = *end frame*
  (commit). Value `0x7F` = *reset*: clear all slots, names, detail lines.
- **CC 119 — data.** Value = one payload byte of the open frame. Payload bytes
  are all < 128 by construction (integers are small; text is ASCII 32–126).
  Max 48 payload bytes per frame; the receiver drops longer frames.

Frame types:

| Type | Name | Payload |
|---|---|---|
| 0x01 | SLOT | `[slot, state, kind, name…]` — slot 0–53, state = enum below, kind = agent-kind code below in the low 3 bits (v2: bit 6, `0x40`, set = the host is auto-approving this agent; v4: bit 5, `0x20`, set = repeat mode; v5: bit 4, `0x10`, set = in the marked group; v6: bit 3, `0x08`, set = the host holds prompts queued for it), then the agent's display name as ASCII (≤ 12 chars used on screen; ≤ 44 sent). State EMPTY with no name vacates the slot |
| 0x02 | DETAIL | `[line, text…]` — detail line for the *currently selected* agent (≤ 29 chars). The host sends a window of 6 lines (0–5) to a v2 device and scrolls it itself; 8 lines (0–7) to a v1 device, which scrolls locally. An empty text clears the line |
| 0x03 | META | `[agent_count, blocked_count, view?]` — header counts; v6: an optional third byte says what the host is showing on DETAIL (0 output, 1 INFO, 2 DIFF), drawn as a tag on that page |
| 0x04 | FOOTER | `[style, text…]` — the bottom line of every page (≤ 29 chars): the armed prompt preset, or a transient message (the result of an action, an agent that needs you). Style: 0 idle (grey), 1 info, 2 good, 3 warning, 4 error. The host times messages out by sending the next footer. *v2* |
| 0x05 | BEEP | `[sound, gain?]` — play a sound: 1 attention, 2 done, 3 error, 4 ack (a click) *(v2)*; 5 heads-up, 6 tick, 7 up, 8 down, 9 alert (an agent became BLOCKED) *(v5)*. v5: an optional second byte is the loudness in 64ths (64 or absent = as written, 127 ≈ twice that). An older device ignores the gain and plays nothing for a sound it does not know, so the host sends it only 1–4 |
| 0x06 | SELECT | `[slot]` — move the device's selection (and key page) to this slot; not echoed as CC 120. *v2* |
| 0x07 | TEXT | `[line, style, text…]` — the MENU page: line 0 is its title, 1–7 its rows (≤ 29 chars); v6: lines 8 and 9 are the host's two lines on the ABOUT page (its version and backend, its system; ≤ 19 chars). Style: the footer's 0–4, 5 cursor (bright), 6 plain text. *v3* |
| 0x08 | PAGE | `[page]` — turn the device to this page (0 GRID, 1 DETAIL, 2 APPROVALS, 3 MENU, 4 ABOUT *(v6)*); not echoed as CC 121. *v3* |
| 0x09 | KEYLEDS | `[mode × up to 27]` — the host's lights for keys 0–26, shown only while a button is held: 0 off, 1 glow, 2 on, 3 blink. An empty frame gives the keys back. *v3* |
| 0x0A | BTNLEDS | `[mode × up to 14]` — the host's lights for buttons 0–13 (the `BTN_*` ids): 0 the device decides, 1 off, 2 on, 3 blink, 4 glow. The host lights SEQ while the selected agent is on repeat and blinks SAVE when its prompt offers "don't ask again". *v4* |
| 0x0B | GAUGE | `[slot, percent]` — how full that agent's context is, 0–100; 127 = unknown (no gauge). Drawn as a bar at the end of the slot's row and under DETAIL's status line. Cleared by an EMPTY SLOT frame. *v5* |
| 0x0F | HELLO_ACK | `[proto_version]` — host is alive; the device shows `HOST OK` (v2: and answers with CC 120; v3: and CC 121) |

Per refresh cycle (1 s, sooner after an action or a Herdr event) the host
sends: a META frame if the counts changed; a SLOT frame per slot whose
(state, kind, name, flags) changed, including an EMPTY one for a slot whose
agent went away; the selected agent's changed DETAIL lines; the FOOTER if it
changed. Every 10 s everything is re-sent unconditionally (a lost frame
heals itself). A device RESET (CC 118 = 0x7F) is sent only when a device
says hello or the daemon starts — not periodically, so the device's per-slot
clocks survive. A v2 device shows `NO HOST` again after 25 s without a frame.

A v1 device ignores frame types it does not know, so the host sends
FOOTER/BEEP/SELECT/TEXT/PAGE/KEYLEDS/BTNLEDS regardless; it withholds only the SLOT flags an older
device would misread as part of the kind (auto before v2, repeat before v4, mark before v5, queue before v6),
GAUGE frames and the v5 sounds, and host-side scrolling.

### State enum

| Value | State | Meaning |
|---|---|---|
| 0 | EMPTY | No agent in this slot |
| 1 | IDLE | Agent is up, waiting for input |
| 2 | WORKING | Agent is producing output / running tools |
| 3 | BLOCKED | Agent is waiting on the human (permission prompt, question) |
| 4 | DONE | Agent finished its turn/task |
| 5 | ERROR | Agent process errored / exited badly |
| 6 | UNKNOWN | Backend can't tell |

### Agent-kind codes

0 unknown · 1 claude (Claude Code) · 2 codex · 3 opencode · 4 grok ·
5 copilot · 6 shell/other. Herdr's native agent names map onto these;
anything unrecognised maps to 6 (or 0 before detection).

### LED rendering (device, per slot key)

| State | Key LED |
|---|---|
| EMPTY | off |
| IDLE | dim glow (`fm1_led_key_glow`) |
| WORKING | solid on |
| BLOCKED | blink, 2 Hz (250 ms on / 250 ms off) |
| DONE | solid for 2 s after the transition, then dim glow |
| ERROR | fast blink, ~6 Hz |
| UNKNOWN | glow pulsing at 1 Hz (glow/off alternate each 500 ms) |

The *selected* slot's LED is additionally forced on during the alternate
500 ms phase (except EMPTY, which only glows at 1 Hz) so you can always see
where you are. Button LEDs: PLAY lit while the selected agent is WORKING,
REC blinks while the selected agent is BLOCKED, ARP lit while the selected
agent is auto-approved, HOME/EDIT/GLO lit on their page.

### Screen rendering (device)

- Header: `AGENT DECK`, agent count, blocked count, `HOST OK` while frames
  keep arriving.
- **GRID page**: list of the current slot page (8 rows visible, follows the
  selection): slot number, state glyph (`-` EMPTY, `.` IDLE, `>` WORKING,
  `!` BLOCKED, `*` DONE, `x` ERROR, `?` UNKNOWN), name, how long the agent
  has been in that state (`7s`, `42m`, `13h` — counted on the device from
  the SLOT frame that changed it), `A` if auto-approved and `R` if on
  repeat. Selected row
  is prefixed with `>` cursor and drawn bright.
- **DETAIL page**: selected agent's name, state, kind and time in state,
  then 6 detail lines (K1 scrolls, via the host).
- **APPROVALS page**: the GRID filtered to BLOCKED slots only.
- **MENU page** (v3): eight lines of TEXT from the host — a title and seven
  rows. The device knows nothing about what they mean.
- **Footer** (every page): the host's FOOTER text; a per-page hint until
  the host has sent one.

### Sound (device)

The synth engine is gone, but the DAC is not: BEEP frames play short
triangle-wave sounds through the FM-1's own output, at the MASTER volume:
two rising calls for attention, C-E-G for done, two low notes for an
error, a click for ack; v5 adds a double note (heads-up), a tick, a rising
and a falling blip (feedback for the user's own actions) and the alert —
three rising calls, three times — for an agent that has become BLOCKED.
In v5 the host also sets each sound's loudness (BEEP's second byte): the
feedback sounds play at about a third of full, the alert near twice. The host decides when (state transitions it observes;
never on first sight of an agent), so a daemon restart does not set off
every chime at once.

## Versioning

`PROTO_VERSION = 6` in both the firmware (`firmware/agtdeck.c`) and the
daemon (`host/agtdeck.py`). The device announces its version in the boot
hello (CC 117) and in every heartbeat (CC 116); the host echoes its own in
HELLO_ACK. Either side can be the older one:

- **v6 daemon, v5 device (firmware 0.5):** everything but the `Q` marker,
  the INFO / DIFF tag on DETAIL and the ABOUT page (MENU → ABOUT shows a
  text version instead). The colour screens are firmware 0.6's own.
- **v6 daemon, v4 device (firmware 0.4):** as above, and without the `+` marker,
  the context gauges, the feedback sounds and the loud alert (a BLOCKED
  agent gets the attention chime three times instead).
- **v6 daemon, v3 device (firmware 0.3):** as above, and without the `R`
  marker and the host's button lights (SEQ and LFO lit, SAVE blinking).
- **v6 daemon, v2 device (firmware 0.2):** everything but the chords and
  the MENU page; REC/FX/ENV/SEQ/SEL act on press, as before.
- **v6 daemon, v1 device (firmware 0.1):** every control still works —
  the device has always forwarded all of them. Missing are the things only
  new firmware can render: the footer line, the chimes, K4's jump (the
  device's cursor cannot be moved by the host), deep scrolling (K1 scrolls
  the 8 lines locally), the auto-approve marker and ARP LED.
- **v1 daemon, v2 device:** works as v1 did; that daemon's 10 s RESET
  restarts the per-slot clocks each time.

New frame types must use unused type nibbles; new states/kinds append at
the end.

## Flash-chip note (why this protocol doc mentions hardware)

Kevin's unit is a late-2026 FM-1 with the **UC25HQ80** flash (JEDEC
`0xB36014`), not the early Puya P25Q80 (`0x856014`). The on-device gate is
`FL_JEDEC_OK(id)` in `vendor-dinghy/firmware/hal/fm1_flash_map.h`, used by
`platform.c`, `recovery.c` and the update loader (`loader.c`). **Dinghy 0.5
already accepts both IDs for the FM-1** —
`((id) == 0x856014u || (id) == 0xB36014u)` — so no allowlist change is needed;
if a future platform drops it, that one line is the fix. The host-side
installer `tools/fm1_install.py` performs no flash-ID check at all (it just
speaks the OTA SysEx protocol). See README.md for the arrival-day sequence.
