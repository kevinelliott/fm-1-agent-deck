#!/usr/bin/env python3
"""Generate firmware-build/tests/agentdeck.txt, the host-harness session.

The harness (firmware-build/tests/host.c) replays a script of timed panel
and MIDI events against the natively compiled app. Frames are tedious to
write by hand as one CC per line, so this builds the script from the same
frame helpers the daemon uses (protocol v6: PROTOCOL.md).

The session: a host says hello and syncs three agents (one auto-approved,
one blocked), sets a footer, asks for the attention chime, moves the
cursor with a SELECT frame; then the panel is exercised (a key, REC, the
SELECT encoder, K2, the pages), a chord is played under the host's key
lights, and the host writes and opens the MENU page. Screens are shot along
the way and the
chimes land in the harness's .wav.

Usage: python3 tools/gen_host_test.py   (rewrites the script in place)
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "firmware-build", "tests", "agentdeck.txt")

CC_CTRL, CC_DATA = 118, 119
FT_SLOT, FT_DETAIL, FT_META, FT_FOOTER, FT_BEEP, FT_SELECT, FT_TEXT, \
    FT_PAGE, FT_KEYLEDS, FT_BTNLEDS, FT_GAUGE, FT_HELLO_ACK = \
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0F
KIND_AUTO, KIND_LOOP, KIND_MARK, KIND_QUEUE = 0x40, 0x20, 0x10, 0x08
BTN = {"OCT-": 0, "OCT+": 1, "FX": 2, "SEL": 3, "ENV": 4, "LFO": 5, "EDIT": 6,
       "GLO": 7, "HOME": 8, "SAVE": 9, "ARP": 10, "SEQ": 11, "PLAY": 12,
       "REC": 13}
ENC = {"SELECT": 0, "ALGORITHM": 1, "K1": 2, "K2": 3, "K3": 4, "K4": 5,
       "PRESETS": 6}

lines = []
t = 200


def cc(ctrl, val):
    global t
    lines.append("%d midi 191 %d %d" % (t, ctrl, val))
    t += 4


def frame(ftype, payload):
    cc(CC_CTRL, 0x10 | ftype)
    for b in bytes(payload):
        cc(CC_DATA, b & 0x7F)
    cc(CC_CTRL, 0x20)


def at(ms, text):
    global t
    t = max(t, ms)
    lines.append("%d %s" % (t, text))
    t += 20


at(300, "shot")                                          # the power-on splash
at(1000, "shot")                                         # no host yet: the empty deck
cc(CC_CTRL, 0x7F)                                        # RESET
frame(FT_HELLO_ACK, [6])
frame(FT_META, [3, 1, 1])                               # v6: DETAIL shows INFO
frame(FT_SLOT, bytes([0, 2, 1 | KIND_AUTO | KIND_LOOP]) + b"api-server")   # WORKING claude, auto + loop
frame(FT_BTNLEDS, [0] * 9 + [3, 0, 2])                  # SAVE blinks, SEQ lit (v4)
frame(FT_SLOT, bytes([1, 3, 2 | KIND_MARK | KIND_QUEUE]) + b"web-frontend")  # BLOCKED codex, marked (v5), queue (v6)
frame(FT_GAUGE, [0, 42])                                # context gauges (v5)
frame(FT_GAUGE, [1, 91])
frame(FT_SLOT, bytes([2, 4, 1]) + b"docs")                      # DONE claude
for i, line in enumerate(["Edit host/agentdeck.py",
                          "  + def approve_always(self):",
                          "Running: pytest -q",
                          "41 passed in 2.31s",
                          "",
                          "Do you want to proceed?"]):
    frame(FT_DETAIL, bytes([i]) + line.encode())
frame(FT_FOOTER, bytes([0]) + b"P2/8 run the tests")
frame(FT_BEEP, [1])                                      # attention chime
at(t + 300, "shot")                                      # GRID
at(t + 100, "keys 2")                                    # key 1: select slot 1
at(t + 200, "keys 0")
at(t + 100, "btn %d" % BTN["REC"])
frame(FT_FOOTER, bytes([2]) + b"APPROVED web-frontend")
frame(FT_BEEP, [4])                                      # ack click
at(t + 200, "btn %d" % BTN["GLO"])                       # APPROVALS
at(t + 300, "shot")
at(t + 100, "btn %d" % BTN["EDIT"])                      # DETAIL
at(t + 300, "shot")
frame(FT_SELECT, [2])                                    # host moves the cursor (no echo)
frame(FT_BEEP, [2])                                      # done chime
at(t + 100, "enc %d 1" % ENC["K2"])                      # forwarded only
at(t + 100, "enc %d -1" % ENC["SELECT"])                 # local move -> CC 120
at(t + 100, "btn %d" % BTN["HOME"])
at(t + 1200, "shot")
at(t + 100, "btn %d" % BTN["HOME"])                      # HOME again: GRID as boxes
at(t + 300, "shot")
at(t + 100, "btn %d" % BTN["HOME"])                      # and back to the list
frame(FT_BEEP, [3])                                      # error chime
frame(FT_BEEP, [5])                                      # heads-up chime (v5)
at(t + 400, "shot")
frame(FT_BEEP, [6, 24])                                  # v5: a quiet tick,
at(t + 150, "shot")
frame(FT_BEEP, [7, 32])                                  # a soft "sent",
at(t + 250, "shot")
frame(FT_BEEP, [9, 110])                                 # and the loud BLOCKED alert
# v3: a chord (REC held + key 1: the note goes out, the cursor stays) under
# the host's key lights, then the MENU page, written and opened by the host
at(t + 900, "btndown %d" % BTN["REC"])
frame(FT_KEYLEDS, [0, 3, 0, 1])
at(t + 100, "keys 4")
at(t + 150, "keys 0")
at(t + 100, "btnup %d" % BTN["REC"])
for i, (style, text) in enumerate([(1, "MENU"), (5, "> NEW AGENT"),
                                   (6, "  SEND PROMPT"), (6, "  EVENT LOG"),
                                   (6, "  CLOSE AGENT"), (6, "  SETTINGS"),
                                   (6, "  HELP")]):
    frame(FT_TEXT, bytes([i, style]) + text.encode())
frame(FT_PAGE, [3])                                      # no echo
at(t + 100, "enc %d 1" % ENC["SELECT"])                  # on MENU: forwarded
at(t + 300, "shot")
at(t + 100, "enc %d 1" % ENC["ALGORITHM"])               # MENU -> ABOUT, CC 121
frame(FT_TEXT, bytes([8, 0]) + b"0.6 via herdr")         # v6: the host's lines for ABOUT
frame(FT_TEXT, bytes([9, 0]) + b"macOS, python 3.12")
at(t + 1200, "shot")
at(t + 100, "enc %d 1" % ENC["ALGORITHM"])               # ABOUT -> GRID
at(t + 400, "end")

with open(OUT, "w") as f:
    f.write("\n".join(lines) + "\n")
print("wrote %s (%d events, %d ms)" % (OUT, len(lines), t))
