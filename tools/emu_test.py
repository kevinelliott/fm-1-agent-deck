#!/usr/bin/env python3
"""emu_test.py — test the packaged Agent Deck firmware in fm1-emu.

This is the on-device test stand-in for this project. Dinghy's host
harness (firmware/host-test) compiles the app for the host and stubs the
hardware; fm1-emu instead runs the *actual shipped .fwsc* on an emulated
JieLi AC791N — real boot, real LCD, real panel matrix, real MIDI input.
Anything that needs a device (boot behaviour, screen rendering, panel
input, the wire protocol) is tested here, against the package, until —
and after — real hardware is on the desk.

What it does:
  1. Boots dist/agentdeck-0.1.fwsc in the fm1-emu batch runner.
  2. Injects, through the FM-1's MIDI IN jack (the firmware's platform
     merges TRS MIDI into the same queue as USB MIDI), the exact CC
     frame stream the real daemon sends on a full resync: RESET,
     HELLO_ACK, META, two SLOT frames (CLAUDE-1 working, CODEX-2
     blocked) and four DETAIL lines for the selected agent.
  3. Turns the ALGORITHM encoder twice to walk GRID -> DETAIL ->
     APPROVALS.
  4. Snapshots the LCD at each page and checks the run: clean exit, the
     CPU ran the full session without a halt (an unknown opcode or a
     wild jump halts the core, so a full-length run is the clean-decode
     evidence), the LCD produced frames, snapshots present and non-blank.

Usage:
  tools/get_emu.sh                 # one-time: fetch the fm1-emu binary
  python3 tools/emu_test.py        # full session -> build/emu-test/
  python3 tools/emu_test.py --boot-only   # quick boot check (~1/3 time)
  python3 tools/emu_test.py --out docs    # regenerate the README shots

fm1-emu binary lookup: $FM1_EMU, then tools/fm1-emu-bin/fm1-emu, then
PATH. Note the x86-64 build interprets (no JIT yet): a 13 s emulated
session takes several minutes of wall time there. On Apple Silicon the
JIT build runs faster than real time.

Requires Python 3 and Pillow (already a build dependency) for PNG
conversion; without Pillow the .bmp snapshots are kept as-is.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CC_CTRL, CC_DATA = 118, 119
FT_SLOT, FT_DETAIL, FT_META, FT_HELLO_ACK = 0x01, 0x02, 0x03, 0x0F
FRAME_END, FRAME_RESET = 0x20, 0x7F


def frame_stream():
    """The daemon's full-resync frame stream as (ctrl, value) CC pairs.

    Mirrors host/agentdeck.py Daemon.poll_once(full=True) for a roster of
    CLAUDE-1 (WORKING, claude) in slot 0 and CODEX-2 (BLOCKED, codex) in
    slot 1, with detail lines for the selected slot 0.
    """
    msgs = []

    def cc(ctrl, val):
        msgs.append((ctrl, val))

    def frame(ftype, payload):
        cc(CC_CTRL, 0x10 | ftype)
        for b in payload:
            cc(CC_DATA, b & 0x7F)
        cc(CC_CTRL, FRAME_END)

    cc(CC_CTRL, FRAME_RESET)
    frame(FT_HELLO_ACK, bytes([1]))
    frame(FT_META, bytes([2, 1]))
    frame(FT_SLOT, bytes([0, 2, 1]) + b"CLAUDE-1")  # state 2=WORKING, kind 1=claude
    frame(FT_SLOT, bytes([1, 3, 2]) + b"CODEX-2")   # state 3=BLOCKED, kind 2=codex
    for i, line in enumerate([
            "claude code - agent deck",
            "editing host/agentdeck.py",
            "tests: pytest -q",
            "34 passed, 0 failed"]):
        frame(FT_DETAIL, bytes([i]) + line.encode())
    return msgs


def keys_script(msgs, full):
    items = []
    t = 2000
    for ctrl, val in msgs:
        items.append("%d:midi:bf%02x%02x" % (t, ctrl, val))
        t += 3  # ~9x slower than the 31250-baud jack: comfortably safe
    if full:
        items.append("7000:ALGO:+1")    # GRID -> DETAIL
        items.append("10000:ALGO:+1")   # DETAIL -> APPROVALS
    return ",".join(items)


def find_emu():
    cand = os.environ.get("FM1_EMU")
    if cand and os.path.isfile(cand):
        return cand
    local = os.path.join(ROOT, "tools", "fm1-emu-bin", "fm1-emu")
    if os.path.isfile(local):
        return local
    return shutil.which("fm1-emu")


def convert(bmp, png):
    """BMP -> PNG upscaled 2x nearest-neighbour (crisp pixels)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    im = Image.open(bmp)
    im = im.resize((im.width * 2, im.height * 2), Image.NEAREST)
    im.save(png)
    return png


def nonblank(png_or_bmp):
    try:
        from PIL import Image
        im = Image.open(png_or_bmp).convert("RGB")
        return len(im.getcolors(maxcolors=1 << 22) or []) >= 4
    except Exception:
        return os.path.getsize(png_or_bmp) > 1000


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fwsc", default=os.path.join(ROOT, "dist", "agentdeck-0.1.fwsc"))
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "emu-test"))
    ap.add_argument("--boot-only", action="store_true",
                    help="boot + frames + GRID snapshot only")
    ap.add_argument("--emu", default=None, help="path to the fm1-emu binary")
    args = ap.parse_args()

    emu = args.emu or find_emu()
    if not emu:
        print("fm1-emu not found. Run tools/get_emu.sh, set $FM1_EMU, "
              "or pass --emu.", file=sys.stderr)
        return 2
    if not os.path.isfile(args.fwsc):
        print("firmware package not found: %s" % args.fwsc, file=sys.stderr)
        return 2

    full = not args.boot_only
    if full:
        snaps = [(5000, "emu-grid"), (9000, "emu-detail"),
                 (12000, "emu-approvals")]
        ms = "13000"
    else:
        snaps = [(4000, "emu-grid")]
        ms = "5000"

    os.makedirs(args.out, exist_ok=True)
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "work"))  # snapshots land in work/
        cmd = [emu, "--fwsc", args.fwsc, "--ms", ms,
               "--keys", keys_script(frame_stream(), full),
               "--snap", ",".join(str(s[0]) for s in snaps)]
        print("running fm1-emu on %s (%s emulated)..."
              % (os.path.basename(args.fwsc), ms + " ms"))
        r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        out = r.stdout + r.stderr
        if r.returncode != 0:
            failures.append("fm1-emu exited %d" % r.returncode)
        if "(step limit" not in out or "halt reason" in out:
            failures.append("run did not complete cleanly "
                            "(no step-limit finish, or a CPU halt)")
        m = re.search(r"lcd frames=(\d+).*display_on=(\w+)", out)
        if not m or int(m.group(1)) == 0 or m.group(2) != "true":
            failures.append("LCD produced no frames")
        for at, name in snaps:
            bmp = os.path.join(tmp, "work", "lcd_%d.bmp" % at)
            if not os.path.isfile(bmp):
                failures.append("snapshot missing at %d ms" % at)
                continue
            png = os.path.join(args.out, name + ".png")
            if convert(bmp, png) is None:
                png = os.path.join(args.out, name + ".bmp")
                shutil.copyfile(bmp, png)
            if not nonblank(png):
                failures.append("snapshot %s looks blank" % name)
            else:
                print("  snapshot ok: %s" % png)
        tail = [ln for ln in out.splitlines()
                if "stopped after" in ln or "lcd frames=" in ln]
        for ln in tail[:4]:
            print("  " + ln.strip())

    if failures:
        for f in failures:
            print("FAIL: " + f, file=sys.stderr)
        return 1
    print("PASS: packaged firmware booted in fm1-emu, accepted the "
          "daemon frame stream, and rendered %s."
          % ("all three pages" if full else "the GRID page"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
