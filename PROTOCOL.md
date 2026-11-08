# Agent Deck wire protocol v1

How the FM-1 (running the Agent Deck firmware, a Dinghy fork) and the host
daemon (`host/agentdeck.py`) talk. Everything runs over **USB-MIDI, channel 16**
(status byte low nibble `0xF`). BLE MIDI is a possible later transport — the
protocol is transport-agnostic — but v1 is USB only.

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
| Note On `0x9F` | note = 48 + key (48–74), vel = 127 | Piano key *k* (0–26) pressed. Also selects slot `page*27 + k` on the device |
| Note Off `0x8F` (or Note On vel 0) | note = 48 + key | Key released |
| CC 102–115 | value 127 = down, 0 = up | Buttons. CC = 102 + button id (Dinghy `BTN_*`): 102 OCT−, 103 OCT+, 104 FX, 105 SEL, 106 ENV, 107 LFO, 108 EDIT, 109 GLO, 110 HOME, 111 SAVE, 112 ARP, 113 SEQ, 114 PLAY, 115 REC |
| CC 85–91 | relative: 65–127 = clockwise (value − 64 detents), 1–63 = counter-clockwise (64 − value detents), 64/0 = no-op | Encoders. 85 SELECT, 86 ALGORITHM, 87 K1, 88 K2, 89 K3, 90 K4, 91 PRESETS |
| CC 7 | absolute 0–127 | MASTER pot (device sends on change ≥ 2) |
| CC 116 | value 1 | Heartbeat, every 1000 ms |
| CC 117 | value = protocol version (1) | Hello, sent once at boot |
| CC 120 | value = slot index | Selection changed on the device (key press, SELECT encoder, or page change) |

Host semantics for the panel events (implemented by the daemon):

- **Key press** selects that agent slot.
- **PLAY** = start/resume selected agent. **REC** = approve pending prompt on
  selected agent. **SAVE** = approve too (and jumps the device to the
  APPROVALS page). **SEL** = focus/confirm selection. **EDIT** = open detail
  page. **HOME** = grid page. **GLO** = approvals page. **SEQ** = prompt mode
  (v1: host logs it; typed prompt injection is done from the daemon CLI /
  `agent.prompt`). **FX** = deny (Esc). **ENV** = interrupt (Ctrl-C).
  **OCT−/OCT+** = slot page down/up (also handled locally on the device).
  **ARP/LFO** are reserved; the daemon logs them.
- **SELECT encoder** moves the selection. **ALGORITHM encoder** cycles the
  device page (GRID → DETAIL → APPROVALS). **K1** scrolls detail lines.
  K2–K4, PRESETS, MASTER are reported but unused by the daemon in v1.

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
| 0x01 | SLOT | `[slot, state, kind, name…]` — slot 0–53, state = enum below, kind = agent-kind code below, then the agent's display name as ASCII (≤ 12 chars used on screen; ≤ 44 sent) |
| 0x02 | DETAIL | `[line, text…]` — detail line 0–7 (≤ 29 chars on screen) for the *currently selected* agent. The host only sends DETAIL for the slot the device has selected (tracked via CC 120 / key presses) |
| 0x03 | META | `[agent_count, blocked_count]` — header counts |
| 0x0F | HELLO_ACK | `[proto_version]` — host is alive; the device shows `HOST OK` |

Per refresh cycle the host sends: one META frame, one SLOT frame per occupied
slot whose (state, kind, name) changed since last cycle (plus a full resync
every 10 s and after a device hello/heartbeat gap), and DETAIL frames for the
selected agent's changed lines. A device RESET (CC 118 = 0x7F) precedes a full
resync.

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
REC blinks while the selected agent is BLOCKED, HOME/GLO lit on their page.

### Screen rendering (device)

- Header: `AGENT DECK`, agent count, blocked count, `HOST OK` once a
  HELLO_ACK (or any valid frame) has been received.
- **GRID page**: list of the current slot page (9 rows visible, follows the
  selection): slot number, state glyph (`-` EMPTY, `.` IDLE, `>` WORKING,
  `!` BLOCKED, `*` DONE, `x` ERROR, `?` UNKNOWN), name. Selected row is
  prefixed with `>` cursor and drawn bright.
- **DETAIL page**: selected agent's name, kind and state, then its 8 detail
  lines (K1 scrolls).
- **APPROVALS page**: the GRID filtered to BLOCKED slots only.

## Versioning

`PROTO_VERSION = 1` in both the firmware (`firmware/agentdeck.c`) and the
daemon (`host/agentdeck.py`). The device announces it in the boot hello
(CC 117); the host echoes it in HELLO_ACK and warns on mismatch but keeps
working (v1 has no incompatible changes). New frame types must use unused
type nibbles; new states/kinds append at the end.

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
