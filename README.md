# Agent Deck — an FM-1 control surface for coding agents

Custom firmware for the **M-VAVE FM-1** that turns the little FM synth into a
hardware desk controller for CLI coding agents (Claude Code, Codex, …)
running on your computer: agent states on the key LEDs and the 240×240
screen, and physical buttons to approve, deny, interrupt and resume agents
without switching windows.

Status: **built and verified without hardware** (Kevin's FM-1 arrives
~early Dec 2026). The daemon's tmux simulate test passes end-to-end, the
firmware passes Dinghy's host emulator, and a real flashable package —
`firmware-build/build/agentdeck-0.1.fwsc`, identity **FM-1_961** — has been
built with the JieLi toolchain and validated by the installer's own package
loader. Nothing has been flashed (no device yet).

## Architecture

```
 ┌──────────────────────── FM-1 ────────────────────────┐
 │ Agent Deck firmware (Dinghy fork, firmware/agentdeck.c)│
 │  keys/buttons/knobs ──► panel events                   │
 │  LEDs + screen     ◄── agent states (CC frame stream)  │
 └──────────────┬───────────────────────────────────────┘
                │  USB-MIDI channel 16  (PROTOCOL.md)
 ┌──────────────▼───────────────────────────────────────┐
 │ host/agentdeck.py  (the daemon, on the Mac)            │
 │  panel events ──► approve / deny / interrupt / prompt  │
 │  backend states ──► SLOT / DETAIL / META frames        │
 └──────┬───────────────────────────────┬───────────────┘
        │ socket API (newline JSON)      │ tmux CLI
 ┌──────▼───────┐                 ┌──────▼───────┐
 │    Herdr     │                 │     tmux     │
 │ agent-aware: │                 │ heuristics:  │
 │ Idle/Working/│                 │ command +    │
 │ Blocked/Done │                 │ pane content │
 │   natively   │                 │ patterns     │
 └──────────────┘                 └──────────────┘
```

Why MIDI and why CCs for data: Dinghy's platform keeps SysEx for its own
firmware updater, so apps never see SysEx. Device→host panel events are
plain notes/CCs (the HYP-1 remote-mode precedent); host→device data rides a
framed CC byte stream (CC 118 control / CC 119 data). Full spec:
**[PROTOCOL.md](PROTOCOL.md)**.

## The two backends

- **Herdr** (preferred when running): the daemon talks to
  `~/.config/herdr/herdr.sock` (or `$HERDR_SOCKET_PATH`; named session:
  `--herdr-session NAME` → `~/.config/herdr/sessions/<NAME>/herdr.sock`).
  Roster from `agent.list` (fallback `pane.list`), states from the native
  `agent_status` field, detail text via `pane.read`, prompts via
  `agent.prompt` (which Herdr refuses for a BLOCKED agent — the daemon
  surfaces that instead of typing over the approval dialog), raw keys via
  `pane.send_keys` (`agent.send_keys` only accepts active *named* agents),
  start via `agent.start`, plus a best-effort `events.subscribe` listener
  (the 1 s poll is the fallback and stays authoritative).
  **Verified live against Herdr 0.9.5** (2026-11-08, `host/herdr-test.log`):
  the full loop — discover, BLOCKED/IDLE states, approve, deny, interrupt,
  prompt, start — was exercised against a real headless Herdr server.
  Three protocol facts from that verification are baked into the client:
  request IDs must be strings, `params` is always sent, and a normal
  request connection is single-use (the server closes it after replying),
  so each request opens a fresh connection. Never run bare `herdr` to
  test (it opens the TUI); `herdr server` runs it headless.
- **tmux**: panes discovered with `list-panes -a`, kind guessed from
  `pane_current_command` / session names / banner text, state from content
  heuristics: permission-prompt patterns → BLOCKED, recent output change or
  spinner → WORKING, completion markers → DONE, quiet shell/agent → IDLE.
  Approve sequences per kind are configurable in `TmuxBackend.APPROVE`
  (Claude Code: `1` + Enter; Codex: `y` + Enter; deny = Esc,
  interrupt = Ctrl-C, prompt = literal send-keys + Enter).

Backend choice: `--backend herdr|tmux|auto` (auto = herdr if its socket
exists, else tmux). A backend that dies or errors is reported once and
retried every poll; it never takes the MIDI loop down.

## Running the daemon

On the Mac (Python 3, stdlib only for simulate mode):

```sh
# no hardware needed — prints frames, takes typed commands
python3 host/agentdeck.py --backend auto --simulate
#   commands: select N | start | approve | deny | interrupt | prompt TEXT | quit

# real FM-1 attached over USB
pip install mido python-rtmidi
python3 host/agentdeck.py --backend auto            # --port FM-1 to pick a port
python3 host/agentdeck.py --backend herdr --herdr-session work
```

Run the self-test any time (creates three throwaway tmux sessions — a fake
Claude Code approval prompt, a spinner, an idle shell — and drives the
daemon through discovery → BLOCKED/WORKING/IDLE → an approve that lands in
the pane):

```sh
sh host/test_simulate.sh     # log: host/simulate-test.log — currently ALL PASS
```

Panel controls (device): a **key press** selects that agent; **PLAY**
start/resume, **REC** or **SAVE** approve, **FX** deny (Esc), **ENV**
interrupt (Ctrl-C), **HOME** grid page, **EDIT** detail page, **GLO**
approvals page, **ALGORITHM** cycles pages, **SELECT** moves the selection,
**K1** scrolls detail lines, **OCT−/+** switch slot pages (54 slots =
2 × 27 keys). Key LEDs: solid = WORKING, glow = IDLE, 2 Hz blink = BLOCKED,
fast blink = ERROR, DONE flashes solid 2 s then glows.

## Building the firmware

`firmware/agentdeck.c` is the app; `firmware-build/` is the Dinghy tree
with it dropped in as the app file and its own identity (package
**FM-1_96N**, update screen `AGTDECK`, `STORE_MAGIC` `0x4147444B` "AGDK").
`vendor-dinghy/` is the pristine upstream clone for reference.

```sh
# one-time setup (Linux x86-64; on a Mac use Docker/WSL per Dinghy's README)
cd firmware-build
tools/get_toolchain.sh                       # JieLi pi32v2 clang -> ~/.jieli/toolchain
git clone --depth 1 --branch AC79NN_SDK_V1.2.1_2023-12-13 \
    https://gitee.com/Jieli-Tech/fw-AC79_AIoT_SDK.git ~/fw-AC79_AIoT_SDK
pip install mido python-rtmidi Pillow

./build.sh --release 0.1                     # -> build/agentdeck-0.1.fwsc (FM-1_961)

# host emulator test (no device, no toolchain): the script
# tests/agentdeck.txt injects protocol frames, presses keys/buttons and
# renders the screen to build/host/*.png
cc -O2 -Wall -Wno-unused-function -DDINGHY_HOST -DAPP_VER="\"dev\"" \
   -Ifirmware/hal -Ifirmware/src -Ifirmware/gen \
   -o build/host/agentdeck firmware/src/dinghy.c
build/host/agentdeck build/host/agentdeck tests/agentdeck.txt
```

Verified 2026-11-08 in the sandbox: device build passes all of Dinghy's
checks (image 57,364 B of 581,564; RAM 17,728 B of 98,304), the `.fwsc`
parses under `tools/fm1_install.py`'s `load_package` (CRCs OK), and the
emulator log is in `firmware/host-test.log` with a screenshot at
`firmware/screen-grid.png`. One emulator quirk worth knowing: the host
harness stubs `fm1_input_note_edges()` to 0, so the firmware diffs
`fm1_in.notes` levels like Dinghy's own app does — identical behaviour on
real hardware.

## Arrival day (read this before plugging anything in)

Kevin's unit is a **late-2026 FM-1**: it will almost certainly carry the
**UC25HQ80** flash chip (JEDEC id `0xB36014`), *not* the early Puya P25Q80
(`0x856014`), and it will likely ship with **stock firmware V16**.

**Flash-chip check — already handled, verify anyway.** The only flash-ID
gate in the whole stack is on-device: `FL_JEDEC_OK(id)` in
`firmware-build/firmware/hal/fm1_flash_map.h`, used by the app
(`platform.c`, `recovery.c`) and the update loader (`loader.c`).
**Dinghy 0.5 already accepts both chips on the FM-1:**

```c
#define FL_JEDEC_OK(id) ((id) == 0x856014u || (id) == 0xB36014u)
```

so no allowlist change is needed (older Felucca-derived bases saved to RAM
only on UC25HQ80 units — that's the failure this avoids). The host-side
installer `tools/fm1_install.py` performs **no** flash-ID check at all.
If a future Dinghy rebase ever drops the second clause, that one line is
the fix.

**Sequence for the day the unit lands:**

1. Power it on stock first. Check the stock firmware version (settings /
   the M-VAVE app) and enjoy the synth briefly — it's a good one.
2. **Return to stock V15 before flashing anything custom.** Community
   guidance and every custom-firmware installer assume V15 as the base:
   use M-VAVE's M-UPGRADE updater (or a browser installer's
   "Return to official V15" option) to put official V15 on it if it
   shipped with V16.
3. Flash Agent Deck over USB:
   `python3 firmware-build/tools/fm1_install.py firmware-build/build/agentdeck-0.1.fwsc`
   (or the FM-1 Firmware Hub in Chrome/Edge). The installer asks before it
   writes; the FM-1 restarts into Agent Deck.
4. Start the daemon (`--backend auto`), watch the FM-1 screen switch from
   `NO HOST` to `HOST OK`, and tap a few keys.
5. Going back: any installer can return the unit to official V15/V16 the
   same way.

**Risk notes.** The FM-1's flash is **single-bank**: an interrupted write
can leave the unit dark. This is recoverable — the JieLi chip's mask-ROM
boot mode (WL82 UBOOT) is in ROM, not flash, and Dinghy's docs point at
FM-1-transporter for exactly this; holding **OCT−** at power-on also
forces the USB rescue in Dinghy-derived firmware. Still: flash on a
charged battery / stable USB, and don't unplug mid-write.

## v1 scope limits

- USB-MIDI only. The protocol is transport-agnostic and BLE MIDI is the
  obvious next step, but Dinghy's BLE path is untested for this use.
- Typed prompt entry happens from the daemon's CLI (`prompt TEXT` /
  Herdr's `agent.prompt`); the FM-1's SEQ button is wired as a host event
  placeholder, not a text-entry mode (27 keys ≠ a keyboard, whatever the
  synth pretends).
- At most 54 agent slots (2 pages); the daemon assigns slots in discovery
  order and keeps them stable for the life of the process.
- Screen refresh is delta-based at the daemon's 1 s poll — this is a
  status deck, not a terminal. The pane text on the DETAIL page is the
  last 8 captured lines.
- The Herdr backend is verified against Herdr 0.9.5's schema and a live
  server; other Herdr versions may rename socket methods — the daemon
  surfaces API errors (with Herdr's error code) rather than crashing, and
  `herdr api schema --json` on the machine running Herdr is the authority
  if a method ever breaks.
- GPL-3.0 throughout, inherited from Dinghy/Felucca — keep `LICENSING.md`
  and the copyright headers if this is ever published.

## Files

| Path | What |
|---|---|
| `PROTOCOL.md` | The wire spec (v1) |
| `host/agentdeck.py` | The daemon: herdr + tmux backends, MIDI + simulate modes |
| `host/test_simulate.sh` | End-to-end simulate test against real tmux panes |
| `host/simulate-test.log` | Latest tmux test run — ALL PASS |
| `host/herdr-test.log` | Live Herdr-backend simulate run (Herdr 0.9.5) |
| `firmware/agentdeck.c` | The firmware app (drop into a Dinghy tree as the app file) |
| `firmware/host-test.log` | Emulator run of the exact shipped source |
| `firmware/screen-grid.png` | The GRID page as rendered by the emulator |
| `firmware-build/` | Dinghy tree + Agent Deck identity; `./build.sh` here |
| `firmware-build/build/agentdeck-0.1.fwsc` | The built package, ready to flash (FM-1_961) |
| `vendor-dinghy/` | Pristine upstream Dinghy clone (reference) |
