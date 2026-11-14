#!/usr/bin/env python3
"""Agent Deck host daemon.

Bridges the M-VAVE FM-1 "Agent Deck" firmware (USB-MIDI channel 16, see
../PROTOCOL.md) to CLI coding agents running on this computer, via one of two
backends:

  * herdr  - the Herdr terminal multiplexer (herdr.dev). Preferred: it detects
             agents and reports Idle/Working/Blocked/Done natively over its
             newline-delimited JSON socket API.
  * tmux   - plain tmux panes; agent kind and state are heuristics over
             `pane_current_command` and captured pane content.

The device is a dumb panel; this daemon owns the behaviour. What the panel
does (the full table is in README.md):

  keys        select a slot                 SELECT    move the selection
  PLAY        start / continue; on an       K1        scroll the detail text
              empty slot: spawn an agent    K2        Up / Down in the agent
  REC         approve                       K3        Left / Right in the agent
  SAVE        approve, don't ask again      K4        jump to the next agent
  FX          deny (Esc)                              that needs you
  ENV         interrupt (Ctrl-C)            PRESETS   arm a prompt preset
  SEL         Enter                         SEQ       send the armed prompt
  LFO         cycle the agent's mode        ARP       toggle auto-approve
              (Shift+Tab)                   EDIT      detail page + focus the
  GLO (held)  + REC / FX / ENV / SEQ / ARP:           pane on the computer
              the same, for every agent it applies to

  Chords (firmware 0.3): hold a button, tap keys - the cursor stays put.
    REC / FX / ENV + key   approve / deny / interrupt that agent
    SEQ + key N            send prompt preset N to the selected agent
    SEL + key N            answer option N (types the digit 1-9)
    PLAY + key             start that agent / spawn one on an empty key
    SAVE + key             move the selected agent to that key
    SEQ held ~1 s          repeat the armed prompt at this agent each time
                           it finishes a turn (again to stop)
    EDIT on DETAIL         cycle the agent's OUTPUT, INFO (what it was
                           asked, what it wants, context size, git) and
                           DIFF (what it has changed in its repository)
    LFO + key              mark / unmark that agent; while any are marked,
                           GLO + button acts on the marked ones only
                           (GLO + LFO clears the marks)
    ARP + key              toggle auto-approve on that agent
    HOME on the GRID       the list, or the agents as coloured boxes
                           (firmware 0.6)
    GLO on APPROVALS       jump to the agent that has waited longest
  Knobs with a button held:
    PLAY + PRESETS         choose which agent PLAY starts
    SEQ + PRESETS          how many times repeat mode sends
    SEL + K1               scroll DETAIL a page at a time
    LFO + K4               step through the marked agents
  A prompt sent to a BLOCKED agent is queued and goes out once it is
  free; a preset may be a list of steps, sent one per finished turn.
  MENU page (ALGORITHM): SELECT moves (K1: a page at a time), SEL picks,
    FX backs out - this agent, fleet, new agent, send prompt, event log,
    close agent, settings, help, about.
  ABOUT page (firmware 0.6): the app, the device, this daemon.

Core uses only the Python 3 standard library. Real-MIDI mode additionally
needs `mido` (+ `python-rtmidi`), imported lazily, so `--simulate` runs
anywhere.

Usage:
  python3 agtdeck.py --backend auto|herdr|tmux [--simulate]
                       [--port SUBSTRING] [--herdr-session NAME]
                       [--herdr-socket PATH] [--poll SECONDS]
                       [--config PATH] [--no-wait]
  python3 agtdeck.py --doctor              check the setup and report
  python3 agtdeck.py --install-service     start at every login
  python3 agtdeck.py --uninstall-service

Without an FM-1 the daemon waits for one, and when the FM-1 is unplugged it
waits for it to come back and sends everything again.

Simulate mode reads commands from stdin, one per line:
  select N | start | approve | always | deny | interrupt | enter | mode
  auto | focus | next | prev | scroll N | preset N | send | prompt TEXT
  spawn [KIND] | all approve|deny|interrupt|send | keys K [K...]
  btn NAME [up|down] | enc NAME N | key N [up] | page N
  view | repeat | move N | copy | mark [N] | unmark | urgent | mute
  queue | quit
(`btn` / `enc` / `key` / `page` replay a panel control exactly as the FM-1
would send it; `btn NAME` alone is a tap, down then up.)
"""

import argparse
import json
import os
import platform
import collections
import glob
import queue
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import types

PROTO_VERSION = 6
VERSION = "0.7"               # the daemon's own, shown on ABOUT

# --- protocol constants (PROTOCOL.md) ---------------------------------------
CH = 0x0F                      # MIDI channel 16 (low nibble)
CC_FRAME_CTRL = 118
CC_FRAME_DATA = 119
FRAME_END = 0x20
FRAME_RESET = 0x7F
FT_SLOT, FT_DETAIL, FT_META, FT_FOOTER, FT_BEEP, FT_SELECT, FT_TEXT, \
    FT_PAGE, FT_KEYLEDS, FT_BTNLEDS, FT_GAUGE, FT_HELLO_ACK = \
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0F
CC_PAGE = 121
PAGE_GRID, PAGE_DETAIL, PAGE_APPROVALS, PAGE_MENU, PAGE_ABOUT = 0, 1, 2, 3, 4
ABOUT_LINE = 8                 # TEXT lines 8 and 9 are the ABOUT page's (v6)
MENU_ROWS = 7                  # rows under the MENU page's title
KIND_AUTO = 0x40               # SLOT kind-byte flag (v2+ devices only)
KIND_LOOP = 0x20               # ... repeat mode (v4+ devices only)
KIND_MARK = 0x10               # ... in the marked group (v5+ devices only)
KIND_QUEUE = 0x08              # ... prompts are queued for it (v6+ devices only)
GAUGE_NONE = 0x7F              # GAUGE: nothing known about this slot
MAX_FRAME = 48
MAX_SLOTS = 54
COLS = 29                      # characters across the FM-1's text area
DETAIL_ROWS = {1: 8, 2: 6, 3: 6, 4: 6, 5: 6, 6: 6}   # detail lines the device shows, by protocol

STATES = ["EMPTY", "IDLE", "WORKING", "BLOCKED", "DONE", "ERROR", "UNKNOWN"]
ST = {name: i for i, name in enumerate(STATES)}
KINDS = ["unknown", "claude", "codex", "opencode", "grok", "copilot", "shell"]
KIND_CODE = {name: i for i, name in enumerate(KINDS)}

BTN_NAMES = ["OCT-", "OCT+", "FX", "SEL", "ENV", "LFO", "EDIT", "GLO",
             "HOME", "SAVE", "ARP", "SEQ", "PLAY", "REC"]
ENC_NAMES = {85: "SELECT", 86: "ALGORITHM", 87: "K1", 88: "K2", 89: "K3",
             90: "K4", 91: "PRESETS"}
ENC_CC = {name: cc for cc, name in ENC_NAMES.items()}

# footer styles / chimes (PROTOCOL.md)
F_IDLE, F_INFO, F_GOOD, F_WARN, F_BAD = 0, 1, 2, 3, 4
BEEP_ATTENTION, BEEP_DONE, BEEP_ERROR, BEEP_ACK, BEEP_WARN = 1, 2, 3, 4, 5
BEEP_TICK, BEEP_UP, BEEP_DOWN, BEEP_ALERT = 6, 7, 8, 9      # v5 devices
UI_BEEPS = (BEEP_TICK, BEEP_UP, BEEP_DOWN)                  # feedback sounds
# How loud each plays on a v5 device, in 64ths (64 = as the firmware has it).
BEEP_GAIN = {BEEP_TICK: 20, BEEP_UP: 28, BEEP_DOWN: 28, BEEP_DONE: 40,
             BEEP_WARN: 44, BEEP_ACK: 40, BEEP_ALERT: 112}
# MENU text styles (the footer's five, plus:)
T_CURSOR, T_TEXT = 5, 6
# key LED modes in a KEYLEDS frame
L_OFF, L_GLOW, L_ON, L_BLINK = 0, 1, 2, 3
# button LED modes in a BTNLEDS frame (0 = the device decides)
B_DEVICE, B_OFF, B_ON, B_BLINK, B_GLOW = 0, 1, 2, 3, 4

DEFAULT_CONFIG = {
    # Prompt presets: PRESETS arms one, SEQ sends it. A string, or
    # {"label": "short name for the screen", "text": "what is sent"}, or
    # {"label": ..., "steps": ["first", "then", ...]}: a pipeline, one
    # step per finished turn.
    "presets": [
        "continue",
        {"label": "run tests + fix",
         "text": "Run the test suite and fix any failures."},
        {"label": "summarize",
         "text": "Summarize what you have done so far and what is left."},
        {"label": "review changes",
         "text": "Review your changes for bugs and edge cases, then fix "
                 "what you find."},
        {"label": "commit",
         "text": "Commit your work with a clear commit message."},
        {"label": "explain blocker",
         "text": "Explain what you are waiting on and what you need "
                 "from me."},
        {"label": "test > review > commit",
         "steps": ["Run the test suite and fix any failures.",
                   "Review your changes for bugs and edge cases, then fix "
                   "what you find.",
                   "Commit your work with a clear commit message."]},
        "/compact",
        "/clear",
    ],
    # PLAY on an empty slot starts this agent in a new pane.
    "default_agent": "claude",
    # Shell command per agent kind (add flags here).
    "agents": {"claude": "claude", "codex": "codex", "opencode": "opencode",
               "grok": "grok", "copilot": "copilot"},
    # Where a spawned agent starts; null = the selected agent's directory.
    "spawn_cwd": None,
    # Extra directories offered by MENU > NEW AGENT.
    "projects": [],
    # A prompt for a WORKING agent: false = send it now (the agent sees it
    # mid-turn), true = hold it until the turn is over. Prompts for a
    # BLOCKED agent are always held.
    "queue_when_working": False,
    # Auto-approve never answers a prompt that matches one of these
    # (regular expressions, case-insensitive, tried against what the agent
    # wants to run and the last screen lines): it alerts you instead.
    "auto_never": [r"\brm\s+-\w*[rf]", r"git\s+push", r"git\s+reset\s+--hard",
                   r"\bsudo\b", r"--force\b", r"drop\s+(table|database)",
                   r"\bmkfs\b|\bdd\s+if="],
    # The other way round: when this list is not empty, auto-approve answers
    # ONLY a prompt that matches one of these (same matching), and leaves
    # every other prompt to you. auto_never still wins over it.
    "auto_only": [],
    # When an agent blocks, move the selection to it (unless the selected
    # agent is itself waiting on you).
    "follow": False,
    # Repeat mode stops by itself after this many sends.
    "repeat_max": 10,
    # Chime again every N minutes while an agent stays BLOCKED (0 = off).
    "nag_minutes": 0,
    # MENU > THIS AGENT > OPEN FOLDER runs this with the directory.
    "open_cmd": "open" if sys.platform == "darwin" else "xdg-open",
    # MENU > THIS AGENT > COMMANDS: typed at the agent as they are.
    "commands": ["/compact", "/clear", "/context", "/review", "/status"],
    # The context gauge: tokens that count as full, and the percentage at
    # which the deck warns once (0 = never). A session seen above
    # ctx_window is taken to be on a 1M-token window.
    "ctx_window": 200000,
    "ctx_warn_pct": 80,
    # Also post a desktop notification when an agent needs you.
    "notify": False,
    "sounds": True,            # chimes on BLOCKED / DONE / ERROR
    # Quiet feedback sounds as controls act (firmware 0.5).
    "ui_sounds": True,
    # How a BLOCKED agent announces itself: "loud" (a longer, louder
    # alert; on older firmware the chime three times) or "normal".
    "alert": "loud",
    "focus_on_edit": True,     # EDIT also focuses the pane on the computer
}


def adopt_old_config_dir():
    """Up to 0.6 the settings lived in ~/.config/agentdeck. Move that
    directory to its new name once, so slots and config carry over."""
    new = os.path.expanduser("~/.config/agtdeck")
    old = os.path.expanduser("~/.config/agent" + "deck")
    if os.path.isdir(old) and not os.path.exists(new):
        try:
            os.rename(old, new)
            print("[agtdeck] moved %s to %s" % (old, new))
        except OSError as e:
            print("[agtdeck] could not move %s: %s" % (old, e))


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))        # deep copy
    explicit = path is not None
    path = os.path.expanduser(path or "~/.config/agtdeck/config.json")
    if not os.path.exists(path):
        if explicit:
            raise BackendError("config file not found: %s" % path)
        return cfg, None
    try:
        with open(path) as f:
            user = json.load(f)
    except (OSError, ValueError) as e:
        raise BackendError("config %s: %s" % (path, e))
    if not isinstance(user, dict):
        raise BackendError("config %s: expected a JSON object" % path)
    for k, v in user.items():
        if k not in cfg:
            print("[config] ignoring unknown key %r" % k)
        elif k == "agents" and isinstance(v, dict):
            cfg["agents"].update(v)
        else:
            cfg[k] = v
    return cfg, path


def normalise_presets(raw):
    out = []
    for p in raw if isinstance(raw, list) else []:
        if isinstance(p, str) and p.strip():
            out.append({"label": p.strip(), "text": p, "steps": [p]})
        elif isinstance(p, dict) and isinstance(p.get("steps"), list):
            steps = [str(x) for x in p["steps"] if str(x).strip()]
            if steps:
                out.append({"label": str(p.get("label") or steps[0]).strip(),
                            "text": steps[0], "steps": steps})
        elif isinstance(p, dict) and str(p.get("text") or "").strip():
            out.append({"label": str(p.get("label") or p["text"]).strip(),
                        "text": str(p["text"]), "steps": [str(p["text"])]})
    return out or [{"label": "continue", "text": "continue",
                    "steps": ["continue"]}]


class BackendError(Exception):
    pass


# Terminal UIs are full of glyphs the FM-1's ASCII font cannot draw; give the
# common ones a readable stand-in before everything else becomes a space.
_GLYPHS = {"❯": ">", "›": ">", "➜": ">", "→": "->", "←": "<-", "…": "...",
           "✓": "+", "✔": "+", "✗": "x", "✘": "x", "●": "*", "⏺": "*",
           "•": "*", "·": ".", "‘": "'", "’": "'", "“": '"', "”": '"',
           "–": "-", "—": "-", "⎿": "`"}
_GLYPH_RE = re.compile("|".join(map(re.escape, _GLYPHS)))


def clean_text(s, limit):
    """Printable ASCII, single line, truncated — the wire format's alphabet."""
    s = _GLYPH_RE.sub(lambda m: _GLYPHS[m.group(0)], str(s))
    s = "".join(c if 32 <= ord(c) <= 126 else " " for c in s)
    return re.sub(r"\s+", " ", s).strip()[:limit]


def wrap_lines(raw_lines, cols=COLS):
    """Raw terminal lines -> the device's lines: cleaned, blank lines
    dropped, long lines wrapped (not cut) at `cols`."""
    out = []
    for ln in raw_lines:
        ln = clean_text(ln, 400)
        if ln:
            out.extend(textwrap.wrap(ln, cols, break_long_words=True,
                                     break_on_hyphens=False) or [])
    return out


# --- device links -----------------------------------------------------------

class MidiLink:
    """Real FM-1 over USB-MIDI via mido (lazy import)."""

    def __init__(self, port_substr, event_q):
        try:
            import mido
        except ImportError:
            raise BackendError(
                "MIDI mode needs mido:  pip install mido python-rtmidi"
                "  (or use --simulate)")
        self._mido = mido
        self.event_q = event_q
        pick_in, pick_out, ins, outs = self.find_ports(mido, port_substr)
        if not pick_in or not pick_out:
            raise BackendError(
                "no FM-1 MIDI port found.\n  inputs:  %s\n  outputs: %s"
                % (ins or "(none)", outs or "(none)"))
        self.names = (pick_in, pick_out)
        self.out = mido.open_output(pick_out)
        self.inp = mido.open_input(pick_in, callback=self._cb)
        print("[midi] in=%r out=%r" % (pick_in, pick_out))

    @staticmethod
    def find_ports(mido, port_substr=None):
        """(input name, output name, all inputs, all outputs)."""
        want = [port_substr] if port_substr else []
        # "AGTDECK" is the USB name from firmware 0.6.2 on; "Felucca" (the
        # platform's own name) is how a unit on 0.6.1 or older shows up.
        want += ["AGTDECK", "FM-1", "FM1", "DINGHY", "M-VAVE", "Cuvave",
                 "Felucca"]
        ins, outs = mido.get_input_names(), mido.get_output_names()
        pick_in = next((n for w in want for n in ins
                        if w.lower() in n.lower()), None)
        pick_out = next((n for w in want for n in outs
                         if w.lower() in n.lower()), None)
        return pick_in, pick_out, ins, outs

    def alive(self):
        """False once the FM-1's ports are gone (unplugged, switched off)."""
        try:
            return self.names[0] in self._mido.get_input_names() and \
                self.names[1] in self._mido.get_output_names()
        except Exception:
            return False

    def close(self):
        for port in (self.inp, self.out):
            try:
                port.close()
            except Exception:
                pass

    def _cb(self, msg):
        self.event_q.put(("midi", msg))

    def _cc(self, ctrl, value):
        self.out.send(self._mido.Message("control_change", channel=CH,
                                         control=ctrl, value=int(value)))

    def frame(self, ftype, payload):
        payload = bytes(b & 0x7F for b in payload[:MAX_FRAME])
        self._cc(CC_FRAME_CTRL, 0x10 | ftype)
        for b in payload:
            self._cc(CC_FRAME_DATA, b)
        self._cc(CC_FRAME_CTRL, FRAME_END)

    def reset(self):
        self._cc(CC_FRAME_CTRL, FRAME_RESET)


class NullLink:
    """No FM-1 right now: frames go nowhere until it is plugged in again."""

    def frame(self, ftype, payload):
        pass

    def reset(self):
        pass


class SimulateLink:
    """No hardware: frames are printed, not sent."""

    FOOTER_STYLE = ["idle", "info", "good", "warn", "bad"]
    BEEPS = {1: "attention", 2: "done", 3: "error", 4: "ack", 5: "warn",
             6: "tick", 7: "up", 8: "down", 9: "alert"}

    def frame(self, ftype, payload):
        if ftype == FT_SLOT and len(payload) >= 3:
            slot, state, kind = payload[0], payload[1], payload[2]
            name = payload[3:].decode("ascii", "replace")
            print("[frame] SLOT slot=%d state=%s kind=%s%s%s%s name=%s"
                  % (slot, STATES[state], KINDS[kind & 0x07],
                     " auto" if kind & KIND_AUTO else "",
                     " loop" if kind & KIND_LOOP else "",
                     " mark" if kind & KIND_MARK else "", name) +
                  (" [queue]" if kind & KIND_QUEUE else ""))
        elif ftype == FT_DETAIL and len(payload) >= 1:
            print("[frame] DETAIL line=%d: %s"
                  % (payload[0], payload[1:].decode("ascii", "replace")))
        elif ftype == FT_META and len(payload) >= 2:
            print("[frame] META agents=%d blocked=%d%s"
                  % (payload[0], payload[1], " view=%s" % VIEWS[payload[2]]
                     if len(payload) > 2 and 0 < payload[2] < 3 else ""))
        elif ftype == FT_FOOTER and len(payload) >= 1:
            style = self.FOOTER_STYLE[payload[0]] \
                if payload[0] < len(self.FOOTER_STYLE) else str(payload[0])
            print("[frame] FOOTER %s: %s"
                  % (style, payload[1:].decode("ascii", "replace")))
        elif ftype == FT_BEEP and len(payload) >= 1:
            print("[frame] BEEP %s%s" % (
                self.BEEPS.get(payload[0], payload[0]),
                " gain=%d" % payload[1] if len(payload) > 1 else ""))
        elif ftype == FT_SELECT and len(payload) >= 1:
            print("[frame] SELECT slot=%d" % payload[0])
        elif ftype == FT_TEXT and len(payload) >= 2:
            print("[frame] TEXT %d%s: %s"
                  % (payload[0], "*" if payload[1] == T_CURSOR else " ",
                     payload[2:].decode("ascii", "replace")))
        elif ftype == FT_PAGE and len(payload) >= 1:
            print("[frame] PAGE %d" % payload[0])
        elif ftype == FT_KEYLEDS:
            print("[frame] KEYLEDS %s" % "".join(
                ".o#*"[b] if b < 4 else "?" for b in payload))
        elif ftype == FT_BTNLEDS:
            print("[frame] BTNLEDS %s" % (" ".join(
                "%s=%s" % (BTN_NAMES[i], ["", "off", "on", "blink", "glow"][b])
                for i, b in enumerate(payload[:14]) if 0 < b < 5) or
                "(all the device's)"))
        elif ftype == FT_GAUGE and len(payload) >= 2:
            print("[frame] GAUGE slot=%d %s" % (
                payload[0], "%d%%" % payload[1] if payload[1] <= 100
                else "none"))
        elif ftype == FT_HELLO_ACK:
            print("[frame] HELLO_ACK proto=%d" % (payload[0] if payload else 0))
        else:
            print("[frame] type=0x%02X payload=%r" % (ftype, bytes(payload)))

    def reset(self):
        print("[frame] RESET (clear all slots)")


# --- backends ---------------------------------------------------------------
# Backend contract:
#   list_agents() -> [ {id, name, kind, state, cwd} ]
#     id: stable string; kind: one of KINDS; state: one of STATES.
#   read_lines(agent_id, n) -> the last n raw terminal lines, oldest first
#   act(agent_id, action, arg=None, kind=...)
#     action in {"start", "approve", "deny", "interrupt", "prompt"}
#   send_keys(agent_id, keys)
#     keys from KEY_NAMES below, or single printable characters
#   spawn(command, cwd, label) -> new agent id (a shell running `command`)
#   focus(agent_id)             bring the pane to the front on the computer
#   close(agent_id)             close the pane (and whatever runs in it)
# Raise BackendError (or anything — the daemon catches all) on failure.

KEY_NAMES = ("up", "down", "left", "right", "enter", "tab", "shift+tab",
             "esc", "ctrl+c")


class TmuxBackend:
    name = "tmux"

    # Key sequences per agent kind. Claude Code's permission prompt is a
    # numbered menu (1 = Yes...); Codex asks y/n. Tune freely.
    APPROVE = {"claude": ["1", "Enter"], "codex": ["y", "Enter"],
               "opencode": ["Enter"], "grok": ["y", "Enter"],
               "copilot": ["Enter"], "shell": ["Enter"],
               "unknown": ["Enter"]}
    START_CMD = {"claude": "claude", "codex": "codex",
                 "opencode": "opencode", "grok": "grok",
                 "copilot": "copilot"}
    KEYMAP = {"up": "Up", "down": "Down", "left": "Left", "right": "Right",
              "enter": "Enter", "tab": "Tab", "shift+tab": "BTab",
              "esc": "Escape", "ctrl+c": "C-c"}
    BLOCKED_PATTERNS = [
        r"Do you want to (proceed|make this edit|create|overwrite)",
        r"❯\s*1\.\s*Yes", r"\(y/n\)", r"\[y/N\]", r"\[Y/n\]",
        r"Allow (this|once|always)", r"Waiting for (user|input|approval)",
        r"Press (Enter|any key) to continue",
    ]
    DONE_PATTERNS = [r"task complete", r"Task completed", r"\bDone\.\s*$",
                     r"✓\s", r"all tests pass"]
    SPINNERS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self):
        self._hash = {}          # pane_id -> (hash, changed_at)
        self._blocked_re = [re.compile(p) for p in self.BLOCKED_PATTERNS]
        self._done_re = [re.compile(p, re.M) for p in self.DONE_PATTERNS]

    def _tmux(self, *args):
        p = subprocess.run(["tmux"] + list(args), capture_output=True,
                           text=True, timeout=10)
        if p.returncode != 0:
            raise BackendError("tmux: %s" % (p.stderr.strip() or
                                             "no server running"))
        return p.stdout

    def _capture(self, pane_id, lines=40):
        try:
            return self._tmux("capture-pane", "-p", "-t", pane_id,
                              "-S", "-%d" % lines)
        except BackendError:
            return ""

    @staticmethod
    def _kind(cmd, *hints):
        blob = " ".join([cmd or ""] + [h or "" for h in hints]).lower()
        for kind in ("claude", "codex", "opencode", "grok", "copilot"):
            if kind in blob:
                return kind
        base = os.path.basename(cmd or "")
        if base in ("bash", "zsh", "fish", "sh", "dash"):
            return "shell"
        return "unknown"

    def list_agents(self):
        fmt = "\t".join(["#{pane_id}", "#{session_name}", "#{window_name}",
                         "#{pane_current_command}", "#{pane_current_path}"])
        out = self._tmux("list-panes", "-a", "-F", fmt)
        agents = []
        now = time.time()
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            pane_id, sess, win, cmd, cwd = parts[:5]
            content = self._capture(pane_id)
            lines = [ln.rstrip() for ln in content.splitlines() if ln.strip()]
            tail = "\n".join(lines[-15:])
            kind = self._kind(cmd, sess, win,
                              "claude code" if "claude code" in tail.lower()
                              else "")
            h = hash(content)
            if pane_id not in self._hash:
                # First sight: baseline only, don't call it "just changed"
                # (an idle shell would flash WORKING for one poll).
                self._hash[pane_id] = (h, 0.0)
                prev_h, changed_at = h, 0.0
            else:
                prev_h, changed_at = self._hash[pane_id]
                if h != prev_h:
                    changed_at = now
                    self._hash[pane_id] = (h, changed_at)
            if any(r.search(tail) for r in self._blocked_re):
                state = "BLOCKED"
            elif h != prev_h or now - changed_at < 2.0 or \
                    any(c in tail for c in self.SPINNERS):
                state = "WORKING"
            elif any(r.search(tail) for r in self._done_re):
                state = "DONE"
            else:
                state = "IDLE"
            if state == "IDLE" and kind == "claude":
                # A quiet screen is not always an idle agent (a long tool
                # call prints nothing). Claude Code's own log knows: a turn
                # it has not ended, written to in the last two minutes.
                i = claude_insight({"kind": kind, "cwd": cwd})
                if i.get("turn") == "open" and \
                        now - i.get("mtime", 0.0) < 120.0:
                    state = "WORKING"
            agents.append({
                "id": pane_id,
                "name": sess if sess else pane_id,
                "kind": kind,
                "state": state,
                "cwd": cwd,
                "_cmd": cmd,
                "_session": sess,
            })
        return agents

    def read_lines(self, agent_id, n=200):
        return self._capture(agent_id, n).splitlines()

    def _send(self, pane_id, *keys):
        for k in keys:
            self._tmux("send-keys", "-t", pane_id, k)

    def send_keys(self, agent_id, keys):
        for k in keys:
            if k in self.KEYMAP:
                self._tmux("send-keys", "-t", agent_id, self.KEYMAP[k])
            else:
                self._tmux("send-keys", "-t", agent_id, "-l", k)

    def act(self, agent_id, action, arg=None, kind="unknown"):
        if action == "approve":
            self._send(agent_id, *self.APPROVE.get(kind,
                                                   self.APPROVE["unknown"]))
        elif action == "deny":
            self._send(agent_id, "Escape")
        elif action == "interrupt":
            self._send(agent_id, "C-c")
        elif action == "prompt":
            if arg:
                self._tmux("send-keys", "-t", agent_id, "-l", arg)
                self._send(agent_id, "Enter")
        elif action == "start":
            cmd = self.START_CMD.get(kind)
            agents = {a["id"]: a for a in self.list_agents()}
            cur = agents.get(agent_id, {}).get("_cmd", "")
            if cmd and os.path.basename(cur) in ("bash", "zsh", "sh", "fish"):
                self._tmux("send-keys", "-t", agent_id, "-l", cmd)
                self._send(agent_id, "Enter")
            elif arg is None:
                self._tmux("send-keys", "-t", agent_id, "-l", "continue")
                self._send(agent_id, "Enter")
        else:
            raise BackendError("tmux: unknown action %r" % action)

    def spawn(self, command, cwd, label, near=None):
        """A new window (a shell, so it outlives the agent) running
        `command`; in the session of `near` when there is one."""
        fmt = ["-P", "-F", "#{pane_id}"]
        where = ["-c", cwd] if cwd else []
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", label)[:20] or "agent"
        try:
            target = []
            if near:
                sess = self._tmux("display-message", "-p", "-t", near,
                                  "#{session_name}").strip()
                target = ["-t", sess + ":"]
            pane = self._tmux("new-window", "-d", *fmt, *where, "-n", name,
                              *target).strip()
        except BackendError:
            pane = self._tmux("new-session", "-d", *fmt, *where,
                              "-s", "agents-%d" % int(time.time()),
                              "-n", name).strip()
        self._tmux("send-keys", "-t", pane, "-l", command)
        self._send(pane, "Enter")
        return pane

    def focus(self, agent_id):
        self._tmux("select-window", "-t", agent_id)
        self._tmux("select-pane", "-t", agent_id)
        try:
            self._tmux("switch-client", "-t", agent_id)
        except BackendError:
            pass                 # no client attached: nothing to switch

    def close(self, agent_id):
        self._tmux("kill-pane", "-t", agent_id)


class HerdrBackend:
    name = "herdr"

    # Herdr reports states natively in `agent_status`; its public schema
    # (verified against Herdr 0.9.5) uses idle/working/blocked/done/unknown.
    STATE_MAP = {"idle": "IDLE", "working": "WORKING", "blocked": "BLOCKED",
                 "done": "DONE", "complete": "DONE", "completed": "DONE",
                 "error": "ERROR", "failed": "ERROR", "unknown": "UNKNOWN",
                 "running": "WORKING", "waiting": "BLOCKED"}
    APPROVE_KEYS = {"claude": ["1", "enter"], "codex": ["y", "enter"],
                    "opencode": ["enter"], "grok": ["y", "enter"],
                    "copilot": ["enter"], "shell": ["enter"],
                    "unknown": ["enter"]}
    START_KINDS = {"pi", "claude", "codex", "gemini", "cursor", "devin",
                   "agy", "cline", "omp", "mastracode", "opencode",
                   "copilot", "kimi", "kiro", "droid", "amp", "grok",
                   "hermes", "kilo", "qodercli", "qwen", "letta", "maki",
                   "muse"}

    def __init__(self, socket_path):
        self.path = socket_path
        self._id = 0
        self._lock = threading.Lock()
        self._dirty = threading.Event()
        self._subscribe_started = False

    def dirty(self):
        """True once since Herdr last said something changed."""
        if self._dirty.is_set():
            self._dirty.clear()
            return True
        return False

    # -- socket plumbing --
    def request(self, method, params=None, timeout=5.0):
        """One newline-JSON request; returns result or raises BackendError.

        Verified live against Herdr 0.9.5 (`herdr api schema --json` plus
        socket tests): request IDs must be strings, `params` is required
        (use {} for no-arg methods), and a normal request connection is
        single-use — the server closes it after the response, so every
        request opens a fresh connection. (events.subscribe is the one
        long-lived connection; see _subscribe_thread.) Unknown fields in
        replies are tolerated.
        """
        with self._lock:
            self._id += 1
            rid = "agtdeck-%d" % self._id
        req = {"id": rid, "method": method, "params": params or {}}
        s = None
        try:
            if not os.path.exists(self.path):
                raise BackendError(
                    "herdr socket not found at %s — is herdr running?"
                    % self.path)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(self.path)
            f = s.makefile("r", encoding="utf-8", newline="\n")
            s.sendall((json.dumps(req) + "\n").encode())
            while True:
                line = f.readline()
                if not line:
                    raise BackendError("herdr closed the connection")
                msg = json.loads(line)
                if msg.get("id") != rid:
                    continue        # an event or stray reply; skip
                if "error" in msg and msg["error"]:
                    err = msg["error"]
                    if isinstance(err, dict):
                        detail = err.get("message", err)
                        if err.get("code"):
                            detail = "%s: %s" % (err["code"], detail)
                    else:
                        detail = err
                    raise BackendError("herdr %s: %s" % (method, detail))
                return msg.get("result")
        except BackendError:
            raise
        except (OSError, ValueError) as e:
            raise BackendError("herdr socket error (%s): %s" % (method, e))
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

    def _subscribe_thread(self):
        """Best effort: events.subscribe on its own connection.

        Events just flag the roster dirty; the poll in the daemon is the
        fallback and remains authoritative. Any failure is silent.
        """
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(30.0)
            s.connect(self.path)
            f = s.makefile("r", encoding="utf-8", newline="\n")
            s.sendall(b'{"id": "agtdeck-events",'
                      b' "method": "events.subscribe", "params":'
                      b' {"subscriptions": [{"type": "pane.created"},'
                      b' {"type": "pane.closed"}, {"type": "pane.updated"},'
                      b' {"type": "pane.agent_detected"}]}}\n')
            while True:
                line = f.readline()
                if not line:
                    break
                self._dirty.set()
        except Exception:
            pass

    # -- normalisation --
    @staticmethod
    def _extract(result, *keys):
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for k in keys:
                if isinstance(result.get(k), list):
                    return result[k]
            for v in result.values():
                if isinstance(v, list):
                    return v
        return []

    def _normalise(self, raw, workspaces):
        aid = raw.get("pane_id") or raw.get("id")
        if not aid:
            return None
        aid = str(aid)
        agent = str(raw.get("agent") or raw.get("display_agent") or "").lower()
        if not agent:
            kind = "shell"
        else:
            kind = next((k for k in KINDS[1:6] if k in agent), "unknown")
        state_raw = str(raw.get("agent_status") or raw.get("state") or
                        "unknown").lower()
        state = self.STATE_MAP.get(state_raw, "UNKNOWN")
        if kind == "shell" and state == "UNKNOWN":
            state = "IDLE"       # a plain shell is just sitting there
        # A stable, telling name: the workspace's label, else the directory.
        # (The terminal title is no good: every Claude Code pane says
        # "Claude Code", every shell says user@host.)
        cwd = str(raw.get("cwd") or raw.get("foreground_cwd") or "")
        ws = workspaces.get(raw.get("workspace_id")) or {}
        name = ws.get("label") or \
            os.path.basename(cwd.rstrip("/")) or agent or aid
        sess = raw.get("agent_session")
        return {"id": aid, "name": str(name), "kind": kind, "state": state,
                "cwd": cwd,
                "session": str(sess.get("value")) if isinstance(sess, dict)
                and sess.get("value") else None}

    def list_agents(self):
        res = self.request("pane.list")
        try:
            wl = self._extract(self.request("workspace.list"), "workspaces")
            workspaces = {w.get("workspace_id"): w for w in wl
                          if isinstance(w, dict)}
        except BackendError:
            workspaces = {}
        agents = [a for a in (self._normalise(r, workspaces) for r in
                              self._extract(res, "panes", "items")) if a]
        if agents and not self._subscribe_started:
            self._subscribe_started = True
            threading.Thread(target=self._subscribe_thread,
                             daemon=True).start()
        return agents

    def read_lines(self, agent_id, n=200):
        res = self.request("pane.read", {
            "pane_id": agent_id, "source": "recent_unwrapped",
            "lines": n, "format": "text", "strip_ansi": True})
        text = ""
        if isinstance(res, dict):
            read = res.get("read") if isinstance(res.get("read"), dict) else {}
            text = read.get("text") or res.get("text") or ""
        return str(text).splitlines()

    def send_keys(self, agent_id, keys):
        # Pane-level keys are the reliable path here: agent.send_keys only
        # accepts an active *named* agent and rejects e.g. lifecycle-
        # reported occupants with agent_not_ready, while pane.send_keys
        # takes the pane ID this backend uses as its agent ID.
        self.request("pane.send_keys", {"pane_id": agent_id,
                                        "keys": list(keys)})

    def act(self, agent_id, action, arg=None, kind="unknown"):
        if action == "prompt":
            # agent.prompt is atomic (prompt + encoded Enter) and its
            # params are exactly {target, text}; it deliberately rejects a
            # BLOCKED agent. For a plain shell / unnamed pane occupant, use
            # pane.send_input with the same text plus an Enter key.
            try:
                self.request("agent.prompt",
                             {"target": agent_id, "text": arg or ""})
                return
            except BackendError as e:
                if "agent_blocked" in str(e):
                    raise
                self.request("pane.send_input", {
                    "pane_id": agent_id, "text": arg or "",
                    "keys": ["enter"]})
                return
        if action == "start":
            # If Herdr already recognises an agent in this pane, PLAY means
            # "continue" at its prompt (and a failure there is the real
            # error — do not fall through to agent.start, which would just
            # report the pane busy). Otherwise start the requested kind in
            # the shell pane; agent.start requires {name, kind, pane_id}
            # and the name must match [a-z][a-z0-9_-]{0,31}.
            try:
                self.request("agent.get", {"target": agent_id})
            except BackendError:
                pass
            else:
                self.request("agent.prompt",
                             {"target": agent_id, "text": "continue"})
                return
            if kind not in self.START_KINDS:
                raise BackendError(
                    "herdr: cannot start kind %r via agent.start" % kind)
            safe_pane = re.sub(r"[^a-z0-9_-]+", "-", agent_id.lower())
            name = ("ad-%s-%s" % (kind, safe_pane))[:32].strip("-")
            self.request("agent.start", {
                "name": name, "kind": kind, "pane_id": agent_id,
                "timeout_ms": 30000}, timeout=35.0)
            return
        keys = {"approve": self.APPROVE_KEYS.get(kind, ["enter"]),
                "deny": ["esc"],
                "interrupt": ["ctrl+c"]}.get(action)
        if keys is None:
            raise BackendError("herdr: unknown action %r" % action)
        self.send_keys(agent_id, keys)

    def spawn(self, command, cwd, label, near=None):
        """A new workspace whose shell runs `command`. (A shell first, then
        the command typed into it: the pane outlives the agent, and Herdr
        detects the agent on its own.)"""
        params = {"label": label, "focus": False}
        if cwd:
            params["cwd"] = cwd
        res = self.request("workspace.create", params)
        pane = ((res or {}).get("root_pane") or {}).get("pane_id")
        if not pane:
            raise BackendError("herdr: workspace.create gave no pane")
        self.request("pane.send_input", {"pane_id": pane, "text": command,
                                         "keys": ["enter"]})
        return str(pane)

    def focus(self, agent_id):
        self.request("pane.focus", {"pane_id": agent_id})

    def close(self, agent_id):
        self.request("pane.close", {"pane_id": agent_id})


def default_herdr_socket(session=None):
    env = os.environ.get("HERDR_SOCKET_PATH")
    if session:
        return os.path.expanduser(
            "~/.config/herdr/sessions/%s/herdr.sock" % session)
    if env:
        return env
    return os.path.expanduser("~/.config/herdr/herdr.sock")


# --- insight: what an agent is up to, beyond its terminal ---------------------
# Claude Code keeps a JSONL log per session under ~/.claude/projects/. It is
# an internal format, so everything here is best effort: any surprise just
# means less to show. Only the tail of the file is read, and nothing is ever
# written.

CLAUDE_PROJECTS = "~/.claude/projects"
_session_paths = {}
_insight_cache = {}            # path -> ((mtime, size), info)
_git_cache = {}                # cwd -> (time, text)


def claude_session_path(agent):
    sid = agent.get("session")
    root = os.path.expanduser(CLAUDE_PROJECTS)
    if sid:
        if sid not in _session_paths:
            hits = glob.glob(os.path.join(root, "*", sid + ".jsonl"))
            if not hits:
                return None            # not written yet: look again later
            _session_paths[sid] = hits[0]
        return _session_paths[sid]
    cwd = agent.get("cwd")
    if not cwd:
        return None
    # No session id (tmux): the newest log of that directory's project.
    logs = glob.glob(os.path.join(
        root, re.sub(r"[^A-Za-z0-9]", "-", cwd), "*.jsonl"))
    return max(logs, key=os.path.getmtime) if logs else None


def tool_summary(name, inp):
    """A tool call in a few words: `Bash npm test`, `Edit agtdeck.py`."""
    inp = inp if isinstance(inp, dict) else {}
    for key in ("command", "file_path", "path", "pattern", "url", "query",
                "description", "prompt"):
        if inp.get(key):
            val = str(inp[key])
            if key in ("file_path", "path"):
                val = os.path.basename(val.rstrip("/")) or val
            return "%s %s" % (name, val)
    return str(name)


def claude_insight(agent, tail_bytes=768 * 1024):
    """{title, prompt, prompts, mode, model, ctx, out, turn, age, pending,
    doing, said, todo} from the session log, or {}.

    out: output tokens over the part of the log that was read (the tail of
    a long session, so a floor, not a total). prompts: what the user typed,
    newest last. turn: "open" while the agent still owes an answer, "ended"
    once it stopped by itself. mtime: when the log was last written."""
    if agent.get("kind") != "claude":
        return {}
    try:
        path = claude_session_path(agent)
        if not path:
            return {}
        st = os.stat(path)
        sig = (st.st_mtime, st.st_size)
        hit = _insight_cache.get(path)
        if hit and hit[0] == sig:
            return hit[1]
        with open(path, "rb") as f:
            if st.st_size > tail_bytes:
                f.seek(st.st_size - tail_bytes)
                f.readline()                   # drop the partial line
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return {}
    info = {}
    open_tools = {}                            # tool_use id -> summary
    out_by_msg = {}                            # message id -> output tokens
    prompts = []
    for line in raw.splitlines():
        if len(line) > 400000:
            continue                           # request blobs: not for us
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if t == "ai-title" and d.get("aiTitle"):
            info["title"] = str(d["aiTitle"])
        elif t == "last-prompt" and d.get("lastPrompt"):
            info["prompt"] = str(d["lastPrompt"])
        elif t == "permission-mode" and d.get("permissionMode"):
            info["mode"] = str(d["permissionMode"])
        elif t in ("assistant", "user") and not d.get("isSidechain"):
            msg = d.get("message") if isinstance(d.get("message"), dict) \
                else {}
            content = msg.get("content")
            if t == "assistant":
                if msg.get("model"):
                    info["model"] = str(msg["model"])
                u = msg.get("usage") if isinstance(msg.get("usage"), dict) \
                    else {}
                ctx = sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens"))
                if ctx:
                    info["ctx"] = ctx
                if u.get("output_tokens"):     # one message spans several
                    out_by_msg[msg.get("id") or len(out_by_msg)] = \
                        int(u["output_tokens"])        # lines: count it once
                stop = msg.get("stop_reason")
                if stop:
                    info["turn"] = "ended" if stop in (
                        "end_turn", "stop_sequence", "max_tokens",
                        "refusal") else "open"
            elif not d.get("isMeta"):          # what the user typed
                said = content if isinstance(content, str) else " ".join(
                    str(b.get("text") or "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text") \
                    if isinstance(content, list) else ""
                said = " ".join(said.split())
                if said.startswith("[Request interrupted"):
                    info["turn"] = "ended"     # Esc: the turn is over
                    open_tools.clear()
                elif said and not said.startswith(("<", "[", "Caveat:")):
                    if said in prompts:
                        prompts.remove(said)
                    prompts.append(said)
                    info["turn"] = "open"
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text" and t == "assistant" and \
                        str(block.get("text") or "").strip():
                    info["said"] = str(block["text"]).strip()
                elif bt == "tool_use":
                    summary = tool_summary(block.get("name"),
                                           block.get("input"))
                    open_tools[block.get("id")] = summary
                    info["doing"] = summary
                    todos = (block.get("input") or {}).get("todos") \
                        if isinstance(block.get("input"), dict) else None
                    if isinstance(todos, list) and todos:
                        done = sum(1 for x in todos if isinstance(x, dict)
                                   and x.get("status") == "completed")
                        cur = next((x for x in todos if isinstance(x, dict)
                                    and x.get("status") == "in_progress"),
                                   None)
                        info["todo"] = "%d/%d%s" % (
                            done, len(todos), ": %s" % (
                                cur.get("activeForm") or cur.get("content"))
                            if cur else "")
                elif bt == "tool_result":
                    open_tools.pop(block.get("tool_use_id"), None)
    if open_tools:
        info["pending"] = list(open_tools.values())[-1]
        info["turn"] = "open"
    if out_by_msg:
        info["out"] = sum(out_by_msg.values())
    if prompts:
        info["prompts"] = prompts[-8:]
    info["mtime"] = st.st_mtime
    _insight_cache[path] = (sig, info)
    return info


def git_summary(cwd):
    """`main +2 ~5 ^1`: branch, untracked, changed, ahead. Cached 5 s."""
    if not cwd or not os.path.isdir(cwd):
        return ""
    hit = _git_cache.get(cwd)
    if hit and time.time() - hit[0] < 5.0:
        return hit[1]
    text = ""
    try:
        p = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain=v2", "--branch"],
            capture_output=True, text=True, timeout=3)
        if p.returncode == 0:
            branch, ahead, new, changed = "?", 0, 0, 0
            for ln in p.stdout.splitlines():
                if ln.startswith("# branch.head "):
                    branch = ln.split(" ", 2)[2]
                elif ln.startswith("# branch.ab "):
                    ahead = int(ln.split()[2].lstrip("+") or 0)
                elif ln.startswith("?"):
                    new += 1
                elif ln[:1] in "12u":
                    changed += 1
            text = branch + (" +%d" % new if new else "") + \
                (" ~%d" % changed if changed else "") + \
                (" ^%d" % ahead if ahead else "")
            if not (new or changed or ahead):
                text += " clean"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _git_cache[cwd] = (time.time(), text)
    return text


_diff_cache = {}               # cwd -> (time, lines)


def diff_lines(cwd):
    """The DIFF view: what has changed in the agent's repository."""
    if not cwd or not os.path.isdir(cwd):
        return ["(no directory)"]
    hit = _diff_cache.get(cwd)
    if hit and time.time() - hit[0] < 5.0:
        return hit[1]

    def git(*args):
        p = subprocess.run(("git", "-C", cwd) + args, capture_output=True,
                           text=True, timeout=4)
        return p.stdout.splitlines() if p.returncode == 0 else None
    out = []
    try:
        stat = git("diff", "--numstat", "HEAD")
        if stat is None:
            out = ["(not a git repository)"]
        else:
            new = [ln[3:] for ln in git("status", "--porcelain") or []
                   if ln.startswith("??")]
            rows, add, rem = [], 0, 0
            for ln in stat:
                bits = ln.split("\t")
                if len(bits) < 3:
                    continue
                plus = int(bits[0]) if bits[0].isdigit() else 0
                minus = int(bits[1]) if bits[1].isdigit() else 0
                add, rem = add + plus, rem + minus
                rows.append((plus + minus, "+%d -%d %s" % (
                    plus, minus, os.path.basename(bits[2].rstrip("/")))))
            if rows:
                out.append("%d FILES +%d -%d" % (len(rows), add, rem))
                out += [text for _, text in sorted(rows, reverse=True)]
            out += ["new: " + f for f in new]
            log = git("log", "-3", "--format=%h %cr: %s")
            out += ["-- recent commits --"] + log if log else []
            if not stat and not new:
                out.insert(0, "(no changes since the last commit)")
    except (OSError, subprocess.SubprocessError):
        out = ["(git did not answer)"]
    _diff_cache[cwd] = (time.time(), out)
    return out


OPTION_RE = re.compile(r"^\W*(\d)\.\s+(\S.*)$")


def parse_options(lines):
    """The numbered choices of the prompt on screen: [(digit, text)], or []
    unless the last lines hold a run 1, 2, ... of at least two."""
    found = []
    for ln in lines[-20:]:
        m = OPTION_RE.match(ln.strip())
        if m:
            found.append((int(m.group(1)), m.group(2).strip()))
    start = max((i for i, (d, _) in enumerate(found) if d == 1), default=None)
    if start is None:
        return []
    run = []
    for d, text in found[start:]:
        if d != len(run) + 1:
            break
        run.append((d, text))
    return run if len(run) >= 2 else []


def notify_desktop(title, text):
    """A notification on the computer; quietly nothing where there is no
    way to post one."""
    try:
        if sys.platform == "darwin":
            esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')  # noqa
            cmd = ["osascript", "-e", 'display notification "%s" with title '
                   '"%s"' % (esc(text), esc(title))]
        elif shutil.which("notify-send"):
            cmd = ["notify-send", title, text]
        else:
            return
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        pass


def make_worktree(cwd):
    """A new git worktree next to the repository `cwd` is in, on a new
    branch: (path, branch). Raises BackendError."""
    def git(*args):
        return subprocess.run(("git",) + args, capture_output=True,
                              text=True, timeout=20)
    try:
        top = git("-C", cwd, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            raise BackendError("not a git repository: %s" % cwd)
        # From inside a worktree, still number next to the main checkout.
        common = git("-C", cwd, "rev-parse", "--path-format=absolute",
                     "--git-common-dir")
        root = top.stdout.strip()
        if common.returncode == 0 and \
                os.path.basename(common.stdout.strip()) == ".git":
            root = os.path.dirname(common.stdout.strip())
        for n in range(1, 100):
            path, branch = "%s-wt%d" % (root, n), "deck/wt%d" % n
            if os.path.exists(path) or git(
                    "-C", root, "show-ref", "--verify", "--quiet",
                    "refs/heads/" + branch).returncode == 0:
                continue
            p = git("-C", root, "worktree", "add", "-b", branch, path)
            if p.returncode != 0:
                raise BackendError((p.stderr.strip().splitlines() or
                                    ["git worktree failed"])[-1])
            return path, branch
        raise BackendError("no free worktree name")
    except (OSError, subprocess.SubprocessError) as e:
        raise BackendError("git: %s" % e)


def fmt_tokens(n):
    return "%dk" % round(n / 1000.0) if n >= 1000 else str(n)


def copy_to_clipboard(text):
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text.encode(), timeout=5, check=True)
            return True
    return False


# --- daemon -----------------------------------------------------------------

# A numbered "yes, and stop asking" option in a permission prompt, e.g.
#   2. Yes, and don't ask again for npm test commands
#   2. Yes, allow all edits during this session (shift+tab)
ALWAYS_RE = re.compile(
    r"^\W*(\d)\.\s+Yes\b.*(don.t ask again|always|allow all|"
    r"(during|for) this session)", re.I)

# Which states an action makes sense for when it is applied to every agent.
FLEET_STATES = {"approve": ("BLOCKED",), "deny": ("BLOCKED",),
                "interrupt": ("WORKING",), "send": ("IDLE", "DONE")}
CHORD_BUTTONS = ("REC", "FX", "ENV", "SEQ", "SEL", "PLAY", "SAVE", "LFO",
                 "ARP")
VIEWS = ("output", "info", "diff")
GLYPH = {"IDLE": ".", "WORKING": ">", "BLOCKED": "!", "DONE": "*",
         "ERROR": "x", "UNKNOWN": "?"}
LONG_PRESS = 0.7               # seconds: SEQ held this long = repeat mode
CHORD_ACTION = {"REC": "approve", "FX": "deny", "ENV": "interrupt"}
HELP_LINES = [
    "KEYS      pick an agent", "PLAY      start / spawn", "REC       approve",
    "SAVE      approve always", "FX        deny (esc)", "ENV       interrupt",
    "SEL       enter", "K2 K3     arrow keys", "LFO       agent mode",
    "PRESETS   arm a prompt", "SEQ       send the prompt",
    "ARP       auto-approve", "K1        scroll detail",
    "K4        next needing you", "EDIT      detail + focus",
    "GLO+btn   for all agents", "REC+key   approve that one",
    "FX+key    deny that one", "ENV+key   stop that one",
    "SEQ+key   send preset N", "SEL+key   answer option N",
    "PLAY+key  start / spawn there", "SAVE+key  move agent there",
    "LFO+key   mark / unmark it", "GLO+btn   marked ones, if any",
    "GLO+LFO   clear the marks", "ARP+key   auto-approve it",
    "SEQ held  repeat the prompt", "EDIT x2   output/info/diff",
    "HOME x2   list / boxes", "GLO x2    longest waiting", "K1 (menu) a page at a time",
    "PLAY+PRESETS which agent", "SEQ+PRESETS repeat count",
    "SEL+K1    scroll a page", "LFO+K4    marked agents",
    "SEQ > blocked: queued",
    "MASTER    chime volume"]
SLOTS_FILE = "~/.config/agtdeck/slots.json"


def fmt_age(secs):
    secs = int(max(0, secs))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm" % (secs // 60)
    return "%dh" % min(99, secs // 3600)


PAST = {"start": "STARTED", "approve": "APPROVED", "deny": "DENIED",
        "interrupt": "STOPPED", "prompt": "SENT"}


class Daemon:
    def __init__(self, backend, link, cfg, simulate):
        self.backend = backend
        self.link = link
        self.cfg = cfg
        self.simulate = simulate
        self.presets = normalise_presets(cfg.get("presets"))
        self.preset = 0
        self.slots = {}            # agent_id -> slot
        self.roster = {}           # slot -> agent dict
        self.used = set()          # slots that ever held an agent
        self.selected = 0
        self.sel_known = False     # has the device told us its selection?
        self.page = 0              # the device's key page (OCT-/OCT+)
        self.held = set()          # buttons held down right now
        self.auto = set()          # agent ids on auto-approve
        self.auto_t = {}           # agent id -> last auto-approve time
        self.prev_state = {}       # agent id -> state at the last poll
        self.spawn_slot = None     # (slot, deadline): where a spawn lands
        self.scroll = 0            # detail lines scrolled back from the end
        self.detail_all = []       # the selected agent's wrapped lines
        self.dev_proto = PROTO_VERSION if simulate else 1   # until it says
        self.dev_page = PAGE_GRID  # the page the device shows (CC 121)
        self.chord = None          # {"name", "used", "fleet", "t"}: a held button
        self.view = "output"       # DETAIL shows: one of VIEWS
        self.beep_due = []         # when to repeat the attention chime
        self.queue = {}            # agent id -> {"items": [(text, tag)],
        #                            "wait": sent, not yet seen WORKING, "t"}
        self.muted = set()         # agent ids that make no sound
        self.auto_held = set()     # agent ids whose prompt auto would not answer
        self.marks = set()         # agent ids in the marked group
        self.options = []          # the selected prompt's [(digit, text)]
        self.ctx_pct = {}          # agent id -> context fullness, 0..100
        self.ctx_warned = set()    # agent ids already warned about it
        self.sent_gauge = {}       # slot -> percentage last framed
        self.state_since = {}      # agent id -> when its state last changed
        self.worktree = False      # MENU > NEW AGENT: in a new worktree
        self.repeat = {}           # agent id -> {"text", "label", "n", "due"}
        self.blocked_since = {}    # agent id -> when it became BLOCKED
        self.last_nag = 0.0
        self.sent_btnleds = None
        self.always_ready = False  # the selected prompt has an "always" option
        self.menu = []             # MENU page: a stack of [builder, cursor]
        self.sent_text = {}        # MENU line -> (style, text) last framed
        self.events = collections.deque(maxlen=40)   # (time, text)
        self.state_key = "%s:%s" % (backend.name,
                                    getattr(backend, "path", ""))
        self.remembered = {} if simulate else self._load_slots()
        self.sent = {}             # slot -> (state, kind, name, auto) framed
        self.sent_detail = {}      # line -> text last framed
        self.sent_meta = None
        self.sent_footer = None
        self.toast_text = None     # a transient footer, until toast_until
        self.toast_until = 0.0
        self.last_full = 0.0
        self.need_reset = True
        self.last_error = None
        self.poll_soon = 0.0       # an action happened: poll again by then

    # -- framing helpers --
    def toast(self, text, style=F_INFO, secs=3.0, beep=None):
        """A transient message on the device's footer line."""
        self.toast_text = (style, clean_text(text, COLS))
        self.toast_until = time.time() + secs
        self._send_footer()
        if beep:
            self.beep(beep)

    def beep(self, which):
        """Ask for a sound. A v5 device has the quiet feedback sounds, the
        loud alert, and a loudness per sound; an older one gets the nearest
        thing it knows (the alert: its attention chime, three times)."""
        if not self.cfg.get("sounds", True):
            return
        if which in UI_BEEPS and not self.cfg.get("ui_sounds", True):
            return
        if which == BEEP_ALERT and self.cfg.get("alert", "loud") != "loud":
            which = BEEP_ATTENTION
        if self.dev_proto >= 5:
            gain = BEEP_GAIN.get(which)
            self.link.frame(FT_BEEP, bytes([which, gain] if gain
                                           else [which]))
        elif which == BEEP_ALERT:
            now = time.time()
            self.link.frame(FT_BEEP, bytes([BEEP_ATTENTION]))
            self.beep_due = [now + 0.7, now + 1.4]
        elif which == BEEP_WARN:
            self.link.frame(FT_BEEP, bytes([BEEP_ACK]))
        elif which < BEEP_WARN:
            self.link.frame(FT_BEEP, bytes([which]))

    def _default_footer(self):
        p = self.presets[self.preset]
        return (F_IDLE, clean_text("P%d/%d %s" % (
            self.preset + 1, len(self.presets), p["label"]), COLS))

    def _send_footer(self):
        if self.toast_text and time.time() >= self.toast_until:
            self.toast_text = None
        want = self.toast_text or self._default_footer()
        if want != self.sent_footer:
            self.link.frame(FT_FOOTER, bytes([want[0]]) +
                            want[1].encode("ascii", "replace"))
            self.sent_footer = want

    def tick(self):
        """Housekeeping between events: expire the toast, repeat a chime."""
        if self.toast_text and time.time() >= self.toast_until:
            self._send_footer()
        if self.beep_due and time.time() >= self.beep_due[0]:
            self.beep_due.pop(0)
            self.link.frame(FT_BEEP, bytes([BEEP_ATTENTION]))

    def _send_detail(self, force=False):
        rows = DETAIL_ROWS.get(self.dev_proto, 6)
        lines = self.detail_all
        if self.dev_proto < 2:
            self.scroll = 0            # a v1 device scrolls by itself
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - rows)))
        end = len(lines) - self.scroll
        window = lines[max(0, end - rows):end]
        window += [""] * (rows - len(window))
        for i, line in enumerate(window):
            if force or self.sent_detail.get(i) != line:
                self.link.frame(FT_DETAIL, bytes([i]) +
                                line.encode("ascii", "replace"))
                self.sent_detail[i] = line

    def info_lines(self, a):
        """The INFO view: what the agent is about, not what it printed."""
        i = claude_insight(a)
        out = []
        if i.get("title"):
            out.append(i["title"])
        head = " ".join(x for x in (
            i.get("model", "").replace("claude-", ""),
            "ctx %s" % fmt_tokens(i["ctx"]) if i.get("ctx") else "",
            "out %s" % fmt_tokens(i["out"]) if i.get("out") else "") if x)
        if head:
            out.append(head)
        git = git_summary(a.get("cwd"))
        tail = " ".join(x for x in (
            "mode %s" % i["mode"] if i.get("mode") else "",
            "git %s" % git if git else "") if x)
        if tail:
            out.append(tail)
        if a["id"] in self.repeat:
            r = self.repeat[a["id"]]
            out.append("REPEAT %d/%d: %s" % (
                r["n"], self._repeat_max(), r["label"]))
        if self.queue.get(a["id"]):
            items = self.queue[a["id"]]["items"]
            out.append("QUEUE %d: %s" % (len(items), ", ".join(
                tag for _, tag in items)))
        if a["id"] in self.auto_held:
            out.append("AUTO-APPROVE HELD: risky")
        if i.get("prompt"):
            out.append("ASKED: " + i["prompt"])
        if a["id"] in self.ctx_pct:
            out.append("CONTEXT %d%% FULL" % self.ctx_pct[a["id"]])
        if a["state"] == "BLOCKED" and i.get("pending"):
            out.append("WANTS: " + i["pending"])
        elif a["state"] == "WORKING" and i.get("doing"):
            out.append("DOING: " + i["doing"])
        if a["state"] == "BLOCKED":
            out += ["SEL+%d: %s" % o for o in self.options]
        if i.get("todo"):
            out.append("TODO " + i["todo"])
        if i.get("said"):
            out.append("SAID: " + i["said"][:600])
        if not i:
            out.insert(0, "%s in %s" % (a["kind"], os.path.basename(
                (a.get("cwd") or "").rstrip("/")) or "?"))
            out.append("(no session details yet)" if a["kind"] == "claude"
                       else "(session details: Claude Code only)")
        return wrap_lines(out)

    def refresh_detail(self, force=False):
        a = self.roster.get(self.selected)
        raw = []
        if a:
            try:
                raw = self.backend.read_lines(a["id"], 200)
            except Exception:
                raw = []
        # SAVE can only do something when the prompt offers "always".
        self.always_ready = bool(a and a["state"] == "BLOCKED" and any(
            ALWAYS_RE.match(ln.strip()) for ln in raw[-30:]))
        self.options = parse_options(raw) if a and \
            a["state"] == "BLOCKED" else []
        if a and self.view != "output":
            before = len(self.detail_all)
            self.detail_all = self.info_lines(a) if self.view == "info" \
                else wrap_lines(diff_lines(a.get("cwd")))
            if self.scroll == 0 and len(self.detail_all) != before:
                force = True
            # INFO reads from the top; scroll counts lines down from there.
            rows = DETAIL_ROWS.get(self.dev_proto, 6)
            top = max(0, min(self.scroll, max(0, len(self.detail_all) - rows)))
            self.scroll = top
            window = self.detail_all[top:top + rows]
            window += [""] * (rows - len(window))
            for n, line in enumerate(window):
                if force or self.sent_detail.get(n) != line:
                    self.link.frame(FT_DETAIL, bytes([n]) +
                                    line.encode("ascii", "replace"))
                    self.sent_detail[n] = line
            return
        self.detail_all = wrap_lines(raw) if a else []
        self._send_detail(force)

    def toggle_view(self):
        self.view = VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)]
        self.scroll = 0
        print("[view] %s" % self.view)
        self.refresh_detail()
        self.toast("VIEW: %s" % self.view.upper(), F_INFO, 1.5,
                   beep=BEEP_TICK)
        self.poll_soon = time.time()           # the device's view tag

    def _send_btnleds(self, force=False):
        """The button LEDs the host owns: SEQ while the selected agent is
        on repeat, SAVE blinking when "always" is on offer."""
        a = self.roster.get(self.selected)
        leds = [B_DEVICE] * 14
        if a and a["id"] in self.repeat:
            leds[BTN_NAMES.index("SEQ")] = B_ON
        if self.always_ready:
            leds[BTN_NAMES.index("SAVE")] = B_BLINK
        if a and a["id"] in self.marks:
            leds[BTN_NAMES.index("LFO")] = B_ON
        leds = bytes(leds)
        if force or leds != self.sent_btnleds:
            self.link.frame(FT_BTNLEDS, leds)
            self.sent_btnleds = leds

    # -- slots that survive a daemon restart --
    def _load_slots(self):
        try:
            with open(os.path.expanduser(SLOTS_FILE)) as f:
                d = json.load(f).get(self.state_key, {})
            return {str(k): int(v) for k, v in d.items()
                    if 0 <= int(v) < MAX_SLOTS}
        except (OSError, ValueError, AttributeError, TypeError):
            return {}

    def _save_slots(self):
        if self.simulate:
            return
        path = os.path.expanduser(SLOTS_FILE)
        try:
            try:
                with open(path) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            data[self.state_key] = dict(self.slots)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "w") as f:
                json.dump(data, f, indent=1)
            os.replace(path + ".tmp", path)
        except OSError as e:
            print("[slots] could not save %s: %s" % (path, e))

    def log_event(self, text):
        self.events.appendleft((time.time(), text))

    # -- roster --
    def _assign_slots(self, agents):
        before = dict(self.slots)
        used = set(self.slots.values())
        for a in agents:
            if a["id"] in self.slots:
                continue
            slot = None
            if self.spawn_slot and time.time() < self.spawn_slot[1] and \
                    self.spawn_slot[0] not in used:
                slot = self.spawn_slot[0]     # the slot PLAY was pressed on
            self.spawn_slot = None
            if slot is None and self.remembered.get(a["id"]) not in used:
                slot = self.remembered.get(a["id"])   # where it was last run
            if slot is None:
                slot = next((i for i in range(MAX_SLOTS) if i not in used),
                            None)
            if slot is None:
                continue                      # more agents than slots
            self.slots[a["id"]] = slot
            used.add(slot)
            self.used.add(slot)
            print("[discover] slot %d: %s (id=%s, kind=%s)"
                  % (slot, a["name"], a["id"], a["kind"]))
        live = {a["id"] for a in agents}
        for aid in [k for k in self.slots if k not in live]:
            slot = self.slots.pop(aid)
            self.auto.discard(aid)
            self.repeat.pop(aid, None)
            self.marks.discard(aid)
            self.queue.pop(aid, None)
            self.muted.discard(aid)
            self.auto_held.discard(aid)
            self.ctx_pct.pop(aid, None)
            self.ctx_warned.discard(aid)
            self.state_since.pop(aid, None)
            self.blocked_since.pop(aid, None)
            self.prev_state.pop(aid, None)
            print("[gone] slot %d: %s" % (slot, aid))
        if self.slots != before:
            self._save_slots()

    @staticmethod
    def _dedupe_names(agents):
        """Two agents in the same project would read the same on a
        12-character row: number the duplicates up front."""
        count = {}
        for a in agents:
            count[a["name"]] = count.get(a["name"], 0) + 1
        seen = {}
        for a in agents:
            if count[a["name"]] > 1:
                seen[a["name"]] = seen.get(a["name"], 0) + 1
                a["name"] = "%d:%s" % (seen[a["name"]], a["name"])

    def poll_once(self):
        try:
            agents = self.backend.list_agents()
        except Exception as e:                     # never kill the loop
            msg = str(e)
            if msg != self.last_error:
                print("[backend] %s unavailable: %s — retrying"
                      % (self.backend.name, msg))
                self.last_error = msg
            return
        if self.last_error is not None:
            print("[backend] %s is back" % self.backend.name)
            self.last_error = None
        self._dedupe_names(agents)
        self._refine_states(agents)
        self._assign_slots(agents)
        agents = [a for a in agents if a["id"] in self.slots]
        self.roster = {self.slots[a["id"]]: a for a in agents}
        if not self.sel_known and self.roster and \
                self.selected not in self.roster:
            self.selected = min(self.roster)   # a v1 device never says
        self._auto_approve(agents)
        self._queue_step(agents)
        self._repeat_step(agents)
        self._notify(agents)
        self._nag(agents)
        self._context(agents)

        full = self.need_reset or time.time() - self.last_full > 10.0
        if self.need_reset:
            # A fresh device or a fresh daemon: wipe, then resend it all.
            self.link.reset()
            self.need_reset = False
            self.sent_footer = None
        if full:
            self.last_full = time.time()
        blocked = sum(1 for a in agents if a["state"] == "BLOCKED")
        meta = (min(len(agents), 99), min(blocked, 99),
                VIEWS.index(self.view))
        if meta != self.sent_meta or full:
            self.link.frame(FT_META, bytes(meta))
            self.sent_meta = meta
        for slot in sorted(self.used):
            a = self.roster.get(slot)
            old = self.sent.get(slot)
            if a is None:                      # vacated: blank it
                if old is not None or full:
                    self.link.frame(FT_SLOT, bytes([slot, ST["EMPTY"], 0]))
                    self.sent.pop(slot, None)
                continue
            auto = a["id"] in self.auto
            loop = a["id"] in self.repeat
            mark = a["id"] in self.marks
            queued = bool(self.queue.get(a["id"]))
            sig = (a["state"], a["kind"], a["name"], auto, loop, mark,
                   queued)
            if old is None or old[0] != a["state"]:
                print("[state] slot %d %s: %s -> %s"
                      % (slot, a["name"], old[0] if old else "NONE",
                         a["state"]))
            if sig != old or full:
                kind = KIND_CODE[a["kind"]]
                if auto and self.dev_proto >= 2:
                    kind |= KIND_AUTO
                if loop and self.dev_proto >= 4:
                    kind |= KIND_LOOP
                if mark and self.dev_proto >= 5:
                    kind |= KIND_MARK
                if queued and self.dev_proto >= 6:
                    kind |= KIND_QUEUE
                payload = bytes([slot, ST[a["state"]], kind]) + \
                    clean_text(a["name"], 44).encode("ascii", "replace")
                self.link.frame(FT_SLOT, payload)
                self.sent[slot] = sig
        if self.dev_proto >= 5:                # how full each context is
            for slot in sorted(self.used):
                a = self.roster.get(slot)
                pct = self.ctx_pct.get(a["id"], GAUGE_NONE) if a \
                    else GAUGE_NONE
                if self.sent_gauge.get(slot, GAUGE_NONE) != pct or \
                        (full and pct != GAUGE_NONE):
                    self.link.frame(FT_GAUGE, bytes([slot, pct]))
                    self.sent_gauge[slot] = pct
        self.refresh_detail(force=full)
        if full:
            self.sent_footer = None
            self.sent_text.clear()
            if self.dev_proto >= 6:            # our two lines of ABOUT
                for n, text in enumerate(self.about_lines()[:2]):
                    self.link.frame(FT_TEXT, bytes([ABOUT_LINE + n, 0]) +
                                    clean_text(text, 19).encode(
                                        "ascii", "replace"))
        self._send_footer()
        self._send_btnleds(force=full)
        if self.dev_page == PAGE_MENU:
            self._menu_render()                # its ages and names move

    def _refine_states(self, agents):
        """Correct IDLE from the agent's own log, where the multiplexer
        names the session (so the log is surely this agent's). A Claude
        Code turn that is still open is not idle: written to in the last
        20 s it is WORKING (multiplexers blink idle between tool calls);
        quiet for longer it is waiting on a prompt the screen does not
        show (a scrolled view), so BLOCKED, flagged "unseen" so that
        auto-approve leaves it to the user."""
        now = time.time()
        for a in agents:
            if a["state"] != "IDLE" or a["kind"] != "claude" or \
                    not a.get("session"):
                continue
            i = claude_insight(a)
            age = now - i.get("mtime", 0.0)
            if i.get("turn") != "open" or age > 600.0:
                continue
            if age < 20.0:
                a["state"] = "WORKING"
            else:
                a["state"], a["unseen"] = "BLOCKED", True

    def _auto_approve(self, agents):
        now = time.time()
        for a in agents:
            if a["state"] != "BLOCKED":
                self.auto_held.discard(a["id"])
            if a["id"] in self.auto and a["state"] == "BLOCKED" and \
                    now - self.auto_t.get(a["id"], 0.0) > 3.0:
                self.auto_t[a["id"]] = now
                risky = self._auto_risky(a)
                if risky:
                    if a["id"] not in self.auto_held:
                        self.auto_held.add(a["id"])
                        print("[auto] HELD for %s: matches %r"
                              % (a["name"], risky))
                        self.toast("AUTO HELD %s: %s" % (a["name"], "NOT ON LIST"
                                   if risky == "(not on auto_only)"
                                   else "YOU DECIDE"),
                                   F_WARN, 6.0, beep=None if a["id"] in
                                   self.muted else BEEP_ALERT)
                        self.log_event("%s auto held" % a["name"])
                    continue
                print("[auto] approve -> %s" % a["name"])
                try:
                    self.backend.act(a["id"], "approve", None, kind=a["kind"])
                    self.toast("AUTO-OK %s" % a["name"], F_GOOD, 2.0)
                    self.log_event("%s auto-ok" % a["name"])
                except Exception as e:
                    print("[auto] approve failed: %s" % e)
                self.poll_soon = now + 0.4

    def _auto_risky(self, a):
        """Why auto-approve must leave this prompt alone, or None: the
        auto_never pattern it matches, or "(not on auto_only)" when there is
        an allow-list and nothing on it matches. Looks at what the agent
        wants to run and at the screen; when it cannot read the screen it
        says so, and that holds the approval too."""
        if a.get("unseen"):                    # nothing to check it against
            return "(prompt not on screen)"
        never = [str(x) for x in self.cfg.get("auto_never") or [] if x]
        only = [str(x) for x in self.cfg.get("auto_only") or [] if x]
        if not never and not only:
            return None
        try:
            screen = self.backend.read_lines(a["id"], 30)[-30:]
        except Exception:
            return "(screen unreadable)"
        text = "\n".join([claude_insight(a).get("pending") or ""] + screen)

        def hit(pats, what):
            for pat in pats:
                try:
                    if re.search(pat, text, re.I):
                        return pat
                except re.error:
                    print("[auto] bad %s pattern %r" % (what, pat))
            return None
        bad = hit(never, "auto_never")
        if bad:
            return bad
        if only and not hit(only, "auto_only"):
            return "(not on auto_only)"
        return None

    QUEUE_SETTLE = 3.0             # seconds at rest before a queued send

    def _queue_step(self, agents):
        """Send each agent's next queued prompt once it is at rest. After a
        send, wait to see it WORKING (or 15 s) before sending another.

        At rest means settled: a multiplexer can report a busy agent as
        idle for a moment between two tool calls, and a prompt typed then
        lands in the permission dialog that follows. So the agent must have
        been at rest for QUEUE_SETTLE seconds, and a Claude Code agent's own
        log must not show a turn still open (one it has neither ended nor
        had interrupted; a log untouched for ten minutes is not trusted)."""
        now = time.time()
        for a in agents:
            q = self.queue.get(a["id"])
            if not q:
                continue
            if not q["items"]:
                del self.queue[a["id"]]
                continue
            if a["state"] == "WORKING":
                q["wait"] = False
                q.pop("rest", None)
                continue
            if a["state"] not in ("IDLE", "DONE"):
                q.pop("rest", None)
                continue                       # BLOCKED / ERROR: hold
            if q["wait"] and now - q["t"] < 15.0:
                continue
            if now - q.setdefault("rest", now) < self.QUEUE_SETTLE:
                self.poll_soon = now + min(1.0, self.QUEUE_SETTLE)
                continue
            if a["kind"] == "claude":
                i = claude_insight(a)
                if i.get("turn") == "open" and \
                        now - i.get("mtime", 0.0) < 600.0:
                    continue                   # mid-turn, whatever the
                                               # multiplexer says
            text, tag = q["items"][0]
            print("[queue] %s -> %s (%d left)" % (
                tag, a["name"], len(q["items"]) - 1))
            q["wait"], q["t"] = True, now
            q.pop("rest", None)
            if self._run(a, "prompt", text):
                q["items"].pop(0)
                self.toast("QUEUE %s > %s (%d LEFT)" % (
                    tag, a["name"], len(q["items"])), F_INFO, 2.5)
                self.log_event("%s queue %s" % (a["name"], tag))
                if not q["items"]:
                    del self.queue[a["id"]]
            self.poll_soon = now + 0.4

    def send_text(self, steps, tag):
        """A prompt (or a pipeline of them) for the selected agent: now if
        it can take one, otherwise queued."""
        a = self.roster.get(self.selected)
        if not a:
            self.toast("SLOT %02d IS EMPTY" % self.selected, F_WARN)
            return
        steps = list(steps)
        hold = a["state"] == "BLOCKED" or bool(self.queue.get(a["id"])) or (
            a["state"] == "WORKING" and self.cfg.get("queue_when_working"))
        if not hold:
            if not self.do_action("prompt", steps.pop(0),
                                  label="SENT %s >" % tag):
                return
        if steps:
            q = self.queue.setdefault(a["id"], {"items": [], "wait": False,
                                                "t": 0.0})
            n = len(steps)
            q["items"] += [(text, "%s.%d" % (tag, i + 1 + (0 if hold else 1))
                            if n > 1 or not hold else tag)
                           for i, text in enumerate(steps)]
            if not hold:
                q["wait"], q["t"] = True, time.time()
            print("[queue] +%d for %s (%d queued)" % (
                n, a["name"], len(q["items"])))
            if hold:
                self.toast("QUEUED %s > %s (%d)" % (
                    tag, a["name"], len(q["items"])), F_INFO, 2.5,
                    beep=BEEP_TICK)
            self.poll_soon = time.time()

    def clear_queue(self):
        a = self.roster.get(self.selected)
        n = len(self.queue.pop(a["id"], {"items": []})["items"]) if a else 0
        print("[queue] cleared (%d)" % n)
        self.toast("QUEUE CLEARED x%d" % n, F_INFO)
        self.poll_soon = time.time()

    def toggle_mute(self):
        a = self.roster.get(self.selected)
        if not a:
            return
        on = a["id"] not in self.muted
        (self.muted.add if on else self.muted.discard)(a["id"])
        print("[mute] %s %s" % ("on" if on else "off", a["name"]))
        self.toast("%s %s" % ("MUTED" if on else "UNMUTED", a["name"]),
                   F_INFO)

    def cycle_default_agent(self, delta):
        """PLAY held + PRESETS: which agent PLAY starts."""
        agents = self.cfg.get("agents") or {}
        kinds = [k for k in sorted(agents)
                 if shutil.which(str(agents[k]).split()[0])]
        if not kinds:
            self.toast("NO AGENT CLI FOUND", F_BAD)
            return
        cur = self.cfg.get("default_agent")
        i = (kinds.index(cur) + (1 if delta > 0 else -1)) % len(kinds) \
            if cur in kinds else 0
        self.cfg["default_agent"] = kinds[i]
        print("[spawn] default agent: %s" % kinds[i])
        self.toast("PLAY STARTS: %s" % kinds[i].upper(), F_INFO, 2.0,
                   beep=BEEP_TICK)

    def adjust_repeat(self, delta):
        """SEQ held + PRESETS: how many sends repeat mode makes."""
        n = max(1, min(99, self._repeat_max() + (1 if delta > 0 else -1)))
        self.cfg["repeat_max"] = n
        self.toast("REPEAT UP TO x%d" % n, F_INFO, 2.0, beep=BEEP_TICK)

    def jump_marked(self, direction):
        """LFO held + K4: step through the marked agents."""
        order = sorted(s for s, a in self.roster.items()
                       if a["id"] in self.marks)
        if not order:
            self.toast("NO MARKED AGENTS", F_WARN, 1.5)
            return
        later = [s for s in order if s > self.selected]
        earlier = [s for s in order if s < self.selected]
        slot = (later[0] if later else order[0]) if direction > 0 \
            else (earlier[-1] if earlier else order[-1])
        self.link.frame(FT_SELECT, bytes([slot]))
        self.select_slot(slot)

    def _notify(self, agents):
        """Chime + toast when an agent starts needing a human."""
        best = None
        for a in agents:
            old = self.prev_state.get(a["id"])
            self.prev_state[a["id"]] = a["state"]
            if old != a["state"]:
                self.state_since[a["id"]] = time.time()
            if old is None or old == a["state"]:
                continue                       # first sight is not news
            if a["state"] in ("BLOCKED", "ERROR", "DONE"):
                self.log_event("%s %s" % (a["name"], a["state"]))
            if a["id"] in self.muted:
                continue
            if a["state"] == "BLOCKED" and a["id"] not in self.auto and \
                    self.cfg.get("follow") and not self.held:
                cur = self.roster.get(self.selected)
                slot = self.slots.get(a["id"])
                if slot is not None and not (cur and
                                             cur["state"] == "BLOCKED"):
                    self.link.frame(FT_SELECT, bytes([slot]))
                    self.select_slot(slot)
            if a["state"] == "BLOCKED" and a["id"] not in self.auto:
                want = claude_insight(a).get("pending")
                ev = (0, BEEP_ALERT,
                      "%s: %s" % (a["name"][:10], want) if want
                      else "%s NEEDS YOU" % a["name"], F_WARN)
            elif a["state"] == "ERROR":
                ev = (1, BEEP_ERROR, "%s ERROR" % a["name"], F_BAD)
            elif a["state"] == "DONE":
                ev = (2, BEEP_DONE, "%s DONE" % a["name"], F_GOOD)
            else:
                continue
            if best is None or ev[0] < best[0]:
                best = ev
        if best:
            self.toast(best[2], best[3], 4.0, beep=best[1])
            if self.cfg.get("notify"):
                notify_desktop("Agent Deck", best[2])

    def _context(self, agents):
        """How full each Claude Code agent's context is; one warning as it
        crosses ctx_warn_pct (and again only after it has dropped back)."""
        try:
            window = max(1000, int(self.cfg.get("ctx_window") or 200000))
            warn = int(self.cfg.get("ctx_warn_pct") or 0)
        except (TypeError, ValueError):
            window, warn = 200000, 80
        for a in agents:
            ctx = claude_insight(a).get("ctx")
            if not ctx:
                self.ctx_pct.pop(a["id"], None)
                continue
            pct = min(100, ctx * 100 // (window if ctx <= window
                                         else max(window, 1000000)))
            self.ctx_pct[a["id"]] = pct
            if warn and pct >= warn and a["id"] not in self.ctx_warned and \
                    a["id"] not in self.muted:
                self.ctx_warned.add(a["id"])
                print("[context] %s is %d%% full" % (a["name"], pct))
                self.toast("%s CONTEXT %d%%" % (a["name"], pct), F_WARN, 4.0,
                           beep=BEEP_WARN)
                self.log_event("%s context %d%%" % (a["name"], pct))
            elif pct < warn - 10:
                self.ctx_warned.discard(a["id"])

    def _repeat_max(self):
        try:
            return max(1, int(self.cfg.get("repeat_max", 10)))
        except (TypeError, ValueError):
            return 10

    def _repeat_step(self, agents):
        """Repeat mode: each time the agent comes to rest, send the prompt
        again (2 s later, so a human can still get a word in)."""
        now = time.time()
        for a in agents:
            r = self.repeat.get(a["id"])
            if not r:
                continue
            old = self.prev_state.get(a["id"])
            resting = a["state"] in ("IDLE", "DONE")
            if a["state"] == "ERROR":
                del self.repeat[a["id"]]
                self.toast("REPEAT OFF %s (ERROR)" % a["name"], F_BAD)
                continue
            if resting and old == "WORKING" and not r["due"]:
                r["due"] = now + 2.0
                self.poll_soon = r["due"]
            if resting and r["due"] and now >= r["due"]:
                r["due"] = 0.0
                if r["n"] >= self._repeat_max():
                    del self.repeat[a["id"]]
                    print("[repeat] done for %s" % a["name"])
                    self.toast("REPEAT DONE %s" % a["name"], F_GOOD, 4.0,
                               beep=BEEP_DONE)
                    self.log_event("%s repeat done" % a["name"])
                    continue
                r["n"] += 1
                print("[repeat] %d/%d -> %s" % (r["n"], self._repeat_max(),
                                                a["name"]))
                if self._run(a, "prompt", r["text"]):
                    self.toast("REPEAT %d/%d > %s" % (
                        r["n"], self._repeat_max(), a["name"]), F_INFO, 2.0)
                self.poll_soon = now + 0.4

    def toggle_repeat(self):
        a = self.roster.get(self.selected)
        if not a:
            self.toast("SLOT %02d IS EMPTY" % self.selected, F_WARN)
            return
        if a["id"] in self.repeat:
            del self.repeat[a["id"]]
            print("[repeat] off for %s" % a["name"])
            self.toast("REPEAT OFF %s" % a["name"], F_INFO)
        else:
            p = self.presets[self.preset]
            # At rest already? Then the first send is due right away.
            due = time.time() if a["state"] in ("IDLE", "DONE") else 0.0
            self.repeat[a["id"]] = {"text": p["text"], "label": p["label"],
                                    "n": 0, "due": due}
            print("[repeat] ON for %s: %s" % (a["name"], p["label"]))
            self.toast("REPEAT P%d x%d %s" % (
                self.preset + 1, self._repeat_max(), a["name"]), F_WARN)
            self.log_event("%s repeat on" % a["name"])
        self.poll_soon = time.time()

    def _nag(self, agents):
        """Chime again while someone has been BLOCKED for a long time."""
        now = time.time()
        live = set()
        for a in agents:
            if a["state"] == "BLOCKED" and a["id"] not in self.muted and \
                    (a["id"] not in self.auto or a["id"] in self.auto_held):
                live.add(a["id"])
                self.blocked_since.setdefault(a["id"], now)
        for aid in [k for k in self.blocked_since if k not in live]:
            del self.blocked_since[aid]
        try:
            every = float(self.cfg.get("nag_minutes") or 0) * 60.0
        except (TypeError, ValueError):
            every = 0.0
        if every <= 0 or not self.blocked_since:
            return
        oldest = min(self.blocked_since, key=self.blocked_since.get)
        waited = now - self.blocked_since[oldest]
        if waited >= every and now - self.last_nag >= every:
            self.last_nag = now
            name = next((a["name"] for a in agents if a["id"] == oldest), "?")
            self.toast("STILL WAITING %s %s" % (fmt_age(waited), name),
                       F_WARN, 4.0, beep=BEEP_ALERT)

    def move_agent(self, dst):
        """SAVE + key: the selected agent takes that key (a swap if it is
        taken)."""
        a = self.roster.get(self.selected)
        if not a:
            self.toast("SLOT %02d IS EMPTY" % self.selected, F_WARN)
            return
        src = self.selected
        if dst == src or not 0 <= dst < MAX_SLOTS:
            return
        other = self.roster.get(dst)
        self.slots[a["id"]] = dst
        if other:
            self.slots[other["id"]] = src
        self.used.update((src, dst))
        self.roster = {self.slots[x["id"]]: x for x in self.roster.values()}
        self.selected = dst
        self._save_slots()
        print("[move] %s: slot %d -> %d%s" % (
            a["name"], src, dst, " (swapped with %s)" % other["name"]
            if other else ""))
        self.link.frame(FT_SELECT, bytes([dst]))
        self.toast("MOVED %s > %02d" % (a["name"], dst), F_GOOD,
                   beep=BEEP_TICK)
        self.poll_soon = time.time()

    def copy_reply(self):
        a = self.roster.get(self.selected)
        if not a:
            return
        text = claude_insight(a).get("said")
        if not text:
            try:
                text = "\n".join(self.backend.read_lines(a["id"], 60))
            except Exception:
                text = ""
        try:
            ok = bool(text) and copy_to_clipboard(text)
        except Exception:
            ok = False
        self.toast("COPIED %d CHARS" % len(text) if ok else "NOTHING COPIED",
                   F_GOOD if ok else F_WARN)

    def open_folder(self):
        a = self.roster.get(self.selected)
        cmd = str(self.cfg.get("open_cmd") or "").split()
        if not a or not a.get("cwd") or not cmd:
            self.toast("NOTHING TO OPEN", F_WARN)
            return
        try:
            subprocess.Popen(cmd + [a["cwd"]], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self.toast("OPENED %s" % os.path.basename(
                a["cwd"].rstrip("/")), F_GOOD)
        except OSError as e:
            self._fail("open", e)

    # -- actions --
    def _fail(self, what, err):
        print("[action] %s failed: %s" % (what, err))
        msg = str(err)
        if "agent_blocked" in msg:
            self.toast("BLOCKED: ANSWER IT FIRST", F_BAD)
        else:
            self.toast("%s FAILED" % what.upper(), F_BAD)

    def _run(self, a, action, arg=None):
        """One backend action on one agent; True if it went through."""
        try:
            self.backend.act(a["id"], action, arg, kind=a["kind"])
            return True
        except Exception as e:
            self._fail(action, e)
            return False

    def do_action(self, action, arg=None, label=None, slot=None):
        slot = self.selected if slot is None else slot
        a = self.roster.get(slot)
        if not a:
            if action == "start" and slot == self.selected:
                self.spawn()
            else:
                print("[action] %s: no agent in slot %d" % (action, slot))
                self.toast("SLOT %02d IS EMPTY" % slot, F_WARN)
            return
        print("[action] %s -> slot %d (%s)" % (action, slot, a["name"]))
        if action == "start" and a["kind"] == "shell":
            # A bare shell: PLAY types the default agent's command into it.
            kind = self.cfg.get("default_agent") or "claude"
            command = (self.cfg.get("agents") or {}).get(kind)
            if not command:
                self.toast("NO COMMAND FOR %s" % kind.upper(), F_BAD)
                return
            action, arg, label = "prompt", command, "STARTING %s IN" % \
                kind.upper()
        ok = self._run(a, action, arg)
        if ok:
            done = label or PAST.get(action, action.upper())
            self.toast("%s %s" % (done, a["name"]), F_GOOD, 2.0,
                       beep=BEEP_DOWN if action in ("deny", "interrupt")
                       else BEEP_UP)
            self.log_event("%s %s" % (a["name"], done.lower().rstrip(" >")))
        self.poll_soon = time.time() + 0.4
        return ok

    def do_fleet(self, action):
        """GLO held: the action, for every agent it applies to."""
        states = FLEET_STATES[action]
        targets = [self.roster[s] for s in sorted(self.roster)
                   if self.roster[s]["state"] in states and
                   (not self.marks or self.roster[s]["id"] in self.marks)]
        if not targets:
            self.toast("%s: NONE %s" % ("MARKED" if self.marks else "ALL",
                                        "/".join(states)), F_WARN)
            return
        backend_action = "prompt" if action == "send" else action
        arg = self.presets[self.preset]["text"] if action == "send" else None
        ok = 0
        for a in targets:
            print("[action] %s (%s) -> %s" % (
                action, "marked" if self.marks else "all", a["name"]))
            ok += 1 if self._run(a, backend_action, arg) else 0
        if ok:
            self.toast("%s x%d" % (PAST.get(backend_action, action.upper()),
                                   ok), F_GOOD,
                       beep=BEEP_DOWN if action in ("deny", "interrupt")
                       else BEEP_UP)
        self.poll_soon = time.time() + 0.4

    def approve_always(self):
        """SAVE: pick the prompt's "yes, and don't ask again" option, if it
        has one. Never guesses: a numbered menu differs per prompt, and
        the wrong digit could be a "no"."""
        a = self.roster.get(self.selected)
        if not a:
            self.toast("SLOT %02d IS EMPTY" % self.selected, F_WARN)
            return
        try:
            lines = self.backend.read_lines(a["id"], 40)
        except Exception as e:
            self._fail("always", e)
            return
        digit = None
        for ln in lines[-30:]:
            m = ALWAYS_RE.match(ln.strip())
            if m:
                digit = m.group(1)
        if digit is None:
            print("[action] always: no such option on screen (%s)" % a["name"])
            self.toast("NO ALWAYS OPTION - USE REC", F_WARN)
            return
        print("[action] always -> slot %d (%s): option %s"
              % (self.selected, a["name"], digit))
        try:
            self.backend.send_keys(a["id"], [digit])
            self.toast("ALWAYS OK %s" % a["name"], F_GOOD, 2.0, beep=BEEP_UP)
        except Exception as e:
            self._fail("always", e)
        self.poll_soon = time.time() + 0.4

    def send_keys(self, keys, label):
        a = self.roster.get(self.selected)
        if not a:
            self.toast("SLOT %02d IS EMPTY" % self.selected, F_WARN)
            return
        print("[keys] %s -> slot %d (%s)" % (" ".join(keys), self.selected,
                                             a["name"]))
        try:
            self.backend.send_keys(a["id"], keys)
            if label:
                self.toast("%s %s" % (label, a["name"]), F_INFO, 1.5)
        except Exception as e:
            self._fail("keys", e)
        self.poll_soon = time.time() + 0.3

    def send_preset(self):
        p = self.presets[self.preset]
        self.send_text(p["steps"], "P%d" % (self.preset + 1))

    def arm_preset(self, delta=0, index=None):
        n = len(self.presets)
        self.preset = (index if index is not None
                       else self.preset + delta) % n
        text = self._default_footer()[1]
        print("[preset] %s" % text)
        self.toast(text, F_INFO, 1.5, beep=BEEP_TICK)

    def toggle_mark(self, slot=None):
        slot = self.selected if slot is None else slot
        a = self.roster.get(slot)
        if not a:
            self.toast("SLOT %02d IS EMPTY" % slot, F_WARN)
            return
        on = a["id"] not in self.marks
        (self.marks.add if on else self.marks.discard)(a["id"])
        print("[mark] %s slot %d (%s): %d marked" % (
            "+" if on else "-", slot, a["name"], len(self.marks)))
        self.toast("%s %s (%d MARKED)" % ("MARKED" if on else "UNMARKED",
                                          a["name"], len(self.marks)), F_INFO,
                   beep=BEEP_TICK)
        self.poll_soon = time.time()

    def clear_marks(self):
        n = len(self.marks)
        self.marks.clear()
        print("[mark] cleared (%d)" % n)
        self.toast("MARKS CLEARED x%d" % n, F_INFO)
        self.poll_soon = time.time()

    def mark_all(self):
        self.marks = {a["id"] for a in self.roster.values()}
        print("[mark] all (%d)" % len(self.marks))
        self.toast("MARKED ALL x%d" % len(self.marks), F_INFO)
        self.poll_soon = time.time()

    def jump_urgent(self):
        """GLO on APPROVALS: go to whoever has waited longest - the oldest
        BLOCKED, else an ERROR, else a DONE."""
        rank = {"BLOCKED": 0, "ERROR": 1, "DONE": 2}
        want = sorted(
            (rank[a["state"]], self.state_since.get(a["id"], 0.0), s)
            for s, a in self.roster.items() if a["state"] in rank)
        if not want:
            self.toast("NOTHING NEEDS YOU", F_GOOD, 1.5)
            return
        slot = want[0][2]
        self.link.frame(FT_SELECT, bytes([slot]))
        self.select_slot(slot)
        a = self.roster[slot]
        self.toast("%s %s %s" % (a["name"], a["state"], fmt_age(
            time.time() - self.state_since.get(a["id"], time.time()))),
            F_WARN if a["state"] == "BLOCKED" else F_INFO, 2.5)

    def goto_agent(self, slot):
        """Select a slot and show its DETAIL page, on the device too."""
        self.link.frame(FT_SELECT, bytes([slot]))
        self.select_slot(slot)
        self.menu = []
        self.dev_page = PAGE_DETAIL
        self.link.frame(FT_PAGE, bytes([PAGE_DETAIL]))

    def toggle_auto(self, slot=None):
        slot = self.selected if slot is None else slot
        a = self.roster.get(slot)
        if not a:
            self.toast("SLOT %02d IS EMPTY" % slot, F_WARN)
            return
        on = a["id"] not in self.auto
        (self.auto.add if on else self.auto.discard)(a["id"])
        print("[auto] %s for slot %d (%s)" % ("ON" if on else "off",
                                              slot, a["name"]))
        self.toast("AUTO-APPROVE %s %s" % ("ON" if on else "OFF", a["name"]),
                   F_WARN if on else F_INFO, beep=BEEP_TICK)
        self.poll_soon = time.time()           # the A marker / ARP LED

    def auto_off_all(self):
        n = len(self.auto)
        self.auto.clear()
        print("[auto] off for all (%d)" % n)
        self.toast("AUTO-APPROVE OFF x%d" % n, F_INFO)
        self.poll_soon = time.time()

    def close_agent(self):
        a = self.roster.get(self.selected)
        if not a:
            return
        print("[close] slot %d (%s)" % (self.selected, a["name"]))
        try:
            self.backend.close(a["id"])
            self.toast("CLOSED %s" % a["name"], F_WARN)
            self.log_event("%s closed" % a["name"])
        except Exception as e:
            self._fail("close", e)
        self.poll_soon = time.time() + 0.3

    def spawn(self, kind=None, cwd=None, slot=None):
        kind = kind or self.cfg.get("default_agent") or "claude"
        command = (self.cfg.get("agents") or {}).get(kind)
        if not command:
            self.toast("NO COMMAND FOR %s" % kind.upper(), F_BAD)
            print("[spawn] no command configured for kind %r" % kind)
            return
        near = self._last_agent
        cwd = cwd or self.cfg.get("spawn_cwd") or (near or {}).get("cwd") \
            or os.path.expanduser("~")
        cwd = os.path.expanduser(cwd)
        if self.worktree:
            try:
                cwd, branch = make_worktree(cwd)
            except BackendError as e:
                print("[spawn] worktree: %s" % e)
                self.toast("NO WORKTREE: %s" % e, F_BAD, 4.0)
                return
            print("[spawn] new worktree %s on %s" % (cwd, branch))
            self.log_event("worktree %s" % os.path.basename(cwd))
        label = os.path.basename(cwd.rstrip("/")) or kind
        print("[spawn] %s in %s (slot %d)" % (
            command, cwd, self.selected if slot is None else slot))
        try:
            self.backend.spawn(command, cwd, label,
                               near=(near or {}).get("id"))
        except Exception as e:
            self._fail("spawn", e)
            return
        slot = self.selected if slot is None else slot
        if slot not in self.roster:
            self.spawn_slot = (slot, time.time() + 15.0)
        self.toast("STARTING %s" % kind.upper(), F_GOOD)
        self.log_event("%s started in %s" % (kind, label))
        self.poll_soon = time.time() + 0.5

    def focus(self):
        a = self.roster.get(self.selected)
        if not a:
            return
        try:
            self.backend.focus(a["id"])
            print("[focus] slot %d (%s)" % (self.selected, a["name"]))
        except Exception as e:
            print("[focus] failed: %s" % e)

    def scroll_by(self, delta):
        """K1: + scrolls toward the newest line, - back through history."""
        if self.dev_proto < 2:
            return                             # a v1 device scrolls itself
        before = self.scroll
        if self.view != "output":              # top-anchored: + is down
            self.scroll = max(0, self.scroll + delta)
            self.refresh_detail()
            return
        self.scroll = max(0, self.scroll - delta * 2)
        self._send_detail()
        if self.scroll != before:
            self.toast("BACK %d LINES" % self.scroll if self.scroll
                       else "LATEST", F_INFO, 1.2)

    def jump_attention(self, direction):
        """K4: move the selection to the next agent that needs a human."""
        rank = {"BLOCKED": 0, "ERROR": 1, "DONE": 2}
        want = sorted((rank[a["state"]], s) for s, a in self.roster.items()
                      if a["state"] in rank)
        if not want:
            self.toast("NOTHING NEEDS YOU", F_GOOD, 1.5)
            return
        order = [s for _, s in want]
        if self.selected in order:
            i = (order.index(self.selected) +
                 (1 if direction > 0 else -1)) % len(order)
        else:
            i = 0 if direction > 0 else len(order) - 1
        slot = order[i]
        self.link.frame(FT_SELECT, bytes([slot]))   # the device follows
        self.select_slot(slot)

    @property
    def _last_agent(self):
        return self.roster.get(self.selected) or \
            self.roster.get(getattr(self, "_last_sel", -1))

    def select_slot(self, slot):
        if not 0 <= slot < MAX_SLOTS:
            return
        if self.selected in self.roster:
            self._last_sel = self.selected
        changed = slot != self.selected
        self.selected = slot
        self.sel_known = True
        self.page = slot // 27
        if slot in self.roster:
            print("[select] slot %d (%s)" % (slot, self.roster[slot]["name"]))
        else:
            print("[select] slot %d is empty" % slot)
        if changed:
            self.scroll = 0
            self.refresh_detail()

    # -- the MENU page: a title and seven rows the host writes --
    def _menu_render(self, force=False):
        if not self.menu:
            self.menu = [[self._m_root, 0]]
        builder, cur = self.menu[-1]
        title, items = builder()
        cur = max(0, min(cur, len(items) - 1))
        self.menu[-1][1] = cur
        top = max(0, min(cur - 3, len(items) - MENU_ROWS))
        lines = [(F_INFO, clean_text(title, COLS))]
        for i in range(top, top + MENU_ROWS):
            if i < len(items):
                lines.append((T_CURSOR if i == cur else T_TEXT,
                              ("> " if i == cur else "  ") +
                              clean_text(items[i][0], COLS - 2)))
            else:
                lines.append((F_IDLE, ""))
        for n, line in enumerate(lines):
            if force or self.sent_text.get(n) != line:
                self.link.frame(FT_TEXT, bytes([n, line[0]]) +
                                line[1].encode("ascii", "replace"))
                self.sent_text[n] = line

    def _menu_push(self, builder):
        def go():
            self.menu.append([builder, 0])
        return go

    def _menu_back(self):
        if len(self.menu) > 1:
            self.menu.pop()
        else:
            self._menu_leave()

    def _menu_leave(self):
        """Back to the GRID, on the device too."""
        self.menu = []
        self.dev_page = PAGE_GRID
        self.link.frame(FT_PAGE, bytes([PAGE_GRID]))

    def menu_move(self, delta, step=1):
        if self.menu:
            self.menu[-1][1] += step if delta > 0 else -step
        self.beep(BEEP_TICK)
        self._menu_render()

    def menu_pick(self):
        if not self.menu:
            self._menu_render()
        builder, cur = self.menu[-1]
        _, items = builder()
        if 0 <= cur < len(items) and items[cur][1]:
            self.beep(BEEP_TICK)
            items[cur][1]()
        if self.dev_page == PAGE_MENU:
            self._menu_render()

    def _target(self):
        a = self.roster.get(self.selected)
        return a["name"] if a else "SLOT %02d" % self.selected

    def _m_root(self):
        return "MENU - %s" % self._target(), [
            ("THIS AGENT", self._menu_push(self._m_agent)),
            ("FLEET", self._menu_push(self._m_fleet)),
            ("NEW AGENT", self._menu_push(self._m_new)),
            ("SEND PROMPT", self._menu_push(self._m_prompts)),
            ("EVENT LOG", self._menu_push(self._m_log)),
            ("CLOSE AGENT", self._menu_push(self._m_close)),
            ("SETTINGS", self._menu_push(self._m_settings)),
            ("HELP", self._menu_push(self._m_help)),
            ("ABOUT", self.about)]

    def _m_agent(self):
        a = self.roster.get(self.selected)
        if not a:
            return "THIS AGENT", [("(no agent selected)", self._menu_back)]
        return a["name"], [
            ("VIEW: %s" % self.view.upper(), self.toggle_view),
            ("REPEAT PROMPT: %s" % ("ON" if a["id"] in self.repeat
                                    else "OFF"), self.toggle_repeat),
            ("AUTO-APPROVE: %s" % ("ON" if a["id"] in self.auto
                                   else "OFF"), self.toggle_auto),
            ("MARKED: %s" % ("YES" if a["id"] in self.marks else "NO"),
             self.toggle_mark),
            ("MUTED: %s" % ("YES" if a["id"] in self.muted else "NO"),
             self.toggle_mute),
            ("QUEUED: %d - CLEAR" % len(
                self.queue.get(a["id"], {"items": []})["items"]),
             self.clear_queue),
            ("COMMANDS", self._menu_push(self._m_commands)),
            ("FOCUS ON COMPUTER", self.focus),
            ("COPY LAST REPLY", self.copy_reply),
            ("OPEN FOLDER", self.open_folder)]

    def _m_commands(self):
        items = []
        for c in self.cfg.get("commands") or []:
            c = str(c).strip()
            if c:
                def go(c=c):
                    self.send_text([c], c[:12])
                items.append((c, go))
        return "COMMAND > %s" % self._target(), \
            items or [("(none configured)", None)]

    def _m_fleet(self):
        now = time.time()
        counts = collections.Counter(a["state"] for a in self.roster.values())
        items = [("CLEAR MARKS (%d)" % len(self.marks), self.clear_marks)
                 if self.marks else ("MARK ALL", self.mark_all)]
        for slot, a in sorted(self.roster.items()):
            ctx = claude_insight(a).get("ctx")
            items.append(("%02d %s%s%-10s %3s %s" % (
                slot, GLYPH.get(a["state"], "-"),
                "+" if a["id"] in self.marks else " ", a["name"][:10],
                fmt_age(now - self.state_since.get(a["id"], now)),
                fmt_tokens(ctx) if ctx else ""),
                lambda slot=slot: self.goto_agent(slot)))
        ins = [claude_insight(a) for a in self.roster.values()]
        if any(i.get("ctx") or i.get("out") for i in ins):
            items.append(("TOKENS ctx %s out %s" % (
                fmt_tokens(sum(i.get("ctx") or 0 for i in ins)),
                fmt_tokens(sum(i.get("out") or 0 for i in ins))), None))
        title = "FLEET %d" % len(self.roster) + "".join(
            " %d%s" % (counts[st], GLYPH[st]) for st in
            ("BLOCKED", "WORKING", "DONE", "ERROR", "IDLE") if counts[st])
        return title, items

    def _m_new(self):
        dirs = []
        near = self._last_agent
        for d in [(near or {}).get("cwd")] + \
                [a.get("cwd") for _, a in sorted(self.roster.items())] + \
                list(self.cfg.get("projects") or []) + \
                [self.cfg.get("spawn_cwd")]:
            d = os.path.expanduser(str(d)) if d else None
            if d and d not in dirs:
                dirs.append(d)
        if not dirs:
            dirs = [os.path.expanduser("~")]
        agents = self.cfg.get("agents") or {}
        default = self.cfg.get("default_agent")
        kinds = [k for k in sorted(agents, key=lambda k: k != default)
                 if shutil.which(str(agents[k]).split()[0])]

        def flip_worktree():
            self.worktree = not self.worktree
        items = [("IN A NEW WORKTREE: %s" % ("YES" if self.worktree
                                             else "NO"), flip_worktree)]
        for d in dirs:
            for k in kinds:
                def go(k=k, d=d):
                    self.spawn(k, d)
                    self._menu_leave()
                items.append(("%s %s" % (
                    k, os.path.basename(d.rstrip("/")) or d), go))
        if len(items) == 1:
            items = [("(no agent CLI found)", None)]
        return "NEW AGENT IN...", items

    def _m_prompts(self):
        items = []
        a = self.roster.get(self.selected)
        last = claude_insight(a).get("prompt") if a else None
        def flip_queue():
            self.cfg["queue_when_working"] = \
                not self.cfg.get("queue_when_working")
        items.append(("IF WORKING: %s" % (
            "QUEUE IT" if self.cfg.get("queue_when_working")
            else "SEND NOW"), flip_queue))
        if last:                               # what it was asked last time
            items.append(("AGAIN: %s" % last,
                          lambda: self.send_text([last], "AGAIN")))
        if a and claude_insight(a).get("prompts"):
            items.append(("RECENT PROMPTS", self._menu_push(self._m_recent)))
        for i, p in enumerate(self.presets):
            def go(i=i):
                self.preset = i
                self.send_preset()
            items.append(("%d %s" % (i + 1, p["label"]), go))
        return "PROMPT > %s" % self._target(), items

    def _m_recent(self):
        """What was typed at this agent lately (Claude Code), newest first:
        the nearest the deck gets to free text."""
        a = self.roster.get(self.selected)
        items = []
        for text in reversed((claude_insight(a).get("prompts") or [])
                             if a else []):
            def go(text=text):
                self.send_text([text], "RECENT")
            items.append((text, go))
        return "RECENT > %s" % self._target(), \
            items or [("(nothing yet)", None)]

    def _m_log(self):
        now = time.time()
        items = [("%3s %s" % (fmt_age(now - t), text), None)
                 for t, text in self.events]
        return "EVENT LOG", items or [("(nothing yet)", None)]

    def _m_close(self):
        a = self.roster.get(self.selected)
        if not a:
            return "CLOSE AGENT", [("(no agent selected)", self._menu_back)]

        def yes():
            self.close_agent()
            self._menu_leave()
        return "CLOSE %s?" % a["name"], [
            ("NO, KEEP IT", self._menu_back),
            ("YES, CLOSE THE PANE", yes)]

    def _m_settings(self):
        def flip(key):
            def go():
                self.cfg[key] = not self.cfg.get(key, True)
            return go

        def flip_off(key):                     # for keys that default off
            def go():
                self.cfg[key] = not self.cfg.get(key)
            return go

        def alert():
            self.cfg["alert"] = "normal" if self.cfg.get(
                "alert", "loud") == "loud" else "loud"
            self.beep(BEEP_ALERT)              # hear what was chosen

        def resync():
            self.need_reset = True
            self.sent_gauge.clear()
            self.sent.clear()
            self.sent_detail.clear()
            self.sent_meta = None
            self.poll_soon = time.time()
        def nag():
            steps = [0, 2, 5, 10]
            cur = self.cfg.get("nag_minutes") or 0
            self.cfg["nag_minutes"] = steps[
                (steps.index(cur) + 1) % len(steps)] if cur in steps else 0
            self.last_nag = 0.0
        on = lambda key: "ON" if self.cfg.get(key, True) else "OFF"  # noqa
        nag_now = self.cfg.get("nag_minutes") or 0
        return "SETTINGS", [
            ("SOUNDS: %s" % on("sounds"), flip("sounds")),
            ("FEEDBACK SOUNDS: %s" % on("ui_sounds"), flip("ui_sounds")),
            ("BLOCKED ALERT: %s" % str(self.cfg.get("alert", "loud")).upper(),
             alert),
            ("NAG WHILE BLOCKED: %s" % ("%sM" % nag_now if nag_now
                                        else "OFF"), nag),
            ("DESKTOP NOTIFY: %s" % ("ON" if self.cfg.get("notify")
                                     else "OFF"), flip_off("notify")),
            ("FOLLOW THE BLOCKED: %s" % ("ON" if self.cfg.get("follow")
                                         else "OFF"), flip_off("follow")),
            ("FOCUS ON EDIT: %s" % on("focus_on_edit"),
             flip("focus_on_edit")),
            ("AUTO-APPROVE OFF, ALL (%d)" % len(self.auto),
             self.auto_off_all),
            ("RESYNC THE DEVICE", resync)]

    def about_lines(self):
        system = {"Darwin": "macOS", "Linux": "Linux"}.get(
            platform.system(), platform.system() or "?")
        old = self.dev_proto < PROTO_VERSION
        return ["%s via %s" % (VERSION, self.backend.name),
                "UPDATE FIRMWARE: v%d < v%d" % (self.dev_proto, PROTO_VERSION)
                if old else "%s, python %s" % (system, ".".join(
                    platform.python_version_tuple()[:2])),
                "firmware protocol v%d" % self.dev_proto,
                "%d agents" % len(self.roster)]

    def _m_about(self):
        return "ABOUT AGENT DECK", [("daemon " + ln if n == 0 else ln, None)
                                    for n, ln in enumerate(self.about_lines())]

    def about(self):
        """MENU > ABOUT: the device's own page where it has one."""
        if self.dev_proto >= 6:
            self.menu = []
            self.dev_page = PAGE_ABOUT
            self.link.frame(FT_PAGE, bytes([PAGE_ABOUT]))
        else:
            self.menu.append([self._m_about, 0])

    def _m_help(self):
        return "HELP", [(ln, None) for ln in HELP_LINES]

    def on_page(self, page):
        self.dev_page = page
        if page == PAGE_MENU:
            self.menu = [[self._m_root, 0]]
            self._menu_render(force=True)

    # -- chords: a button held, keys tapped --
    def _chord_leds(self, name):
        """Light the keys for what a tap would do while `name` is held."""
        leds = [L_OFF] * 27
        base = self.page * 27
        if name in CHORD_ACTION:
            hot = FLEET_STATES[CHORD_ACTION[name]]
            for k in range(27):
                a = self.roster.get(base + k)
                if a:
                    leds[k] = L_BLINK if a["state"] in hot else L_GLOW
        elif name == "SEQ":
            for k in range(min(27, len(self.presets))):
                leds[k] = L_ON if k == self.preset else L_GLOW
        elif name == "SEL":
            leds[:9] = [L_GLOW] * 9
            if self.options:                   # exactly the choices on offer
                leds[:9] = [L_OFF] * 9
                for d, text in self.options:
                    if 1 <= d <= 9:
                        leds[d - 1] = L_BLINK if ALWAYS_RE.match(
                            "%d. %s" % (d, text)) else L_ON
        elif name in ("LFO", "ARP"):           # who is marked / on auto
            group = self.marks if name == "LFO" else self.auto
            for k in range(27):
                a = self.roster.get(base + k)
                if a:
                    leds[k] = L_ON if a["id"] in group else L_GLOW
        elif name in ("PLAY", "SAVE"):         # where things are / could go
            for k in range(27):
                leds[k] = L_ON if base + k in self.roster else L_GLOW
            if name == "SAVE" and self.selected // 27 == self.page:
                leds[self.selected % 27] = L_BLINK
        self.link.frame(FT_KEYLEDS, bytes(leds))

    def _chord_key(self, name, k):
        slot = self.page * 27 + k
        if name in CHORD_ACTION:
            self.do_action(CHORD_ACTION[name], slot=slot)
        elif name == "SEQ":
            if k < len(self.presets):
                self.preset = k
                self.send_preset()
            else:
                self.toast("NO PRESET %d" % (k + 1), F_WARN)
        elif name == "SEL":
            if k < 9:
                text = dict(self.options).get(k + 1)
                self.send_keys([str(k + 1)], "%d %s >" % (k + 1, text[:14])
                               if text else "OPTION %d >" % (k + 1))
        elif name == "LFO":
            self.toggle_mark(slot)
            self._chord_leds(name)             # the lights follow
        elif name == "ARP":
            self.toggle_auto(slot)
            self._chord_leds(name)
        elif name == "PLAY":
            if slot in self.roster:
                self.do_action("start", slot=slot)
            else:
                self.spawn(slot=slot)
        elif name == "SAVE":
            self.move_agent(slot)

    def on_key(self, k):
        if self.held and self.dev_proto >= 3:
            # A chord: the device left its cursor alone, so do we.
            if self.chord:
                self.chord["used"] = True
                self._chord_key(self.chord["name"], k)
            return
        self.select_slot(self.page * 27 + k)

    # -- device events --
    def on_button(self, name, down):
        if not down:
            self.held.discard(name)
            if name == "GLO" and self.toast_text and \
                    self.toast_text[1].startswith(("ALL:", "MARKED x")):
                self.toast_until = 0.0         # drop the modifier hint
                self._send_footer()
            c = self.chord
            if c and c["name"] == name:
                self.chord = None
                if c["used"]:
                    pass
                elif name == "SEQ" and not c["fleet"] and \
                        time.time() - c["t"] >= LONG_PRESS:
                    self.toggle_repeat()       # held, no key: repeat mode
                else:                          # a plain tap after all
                    self._press(name, c["fleet"])
            return
        self.held.add(name)
        fleet = "GLO" in self.held and name != "GLO"
        if self.dev_page == PAGE_MENU and name in ("SEL", "FX") and not fleet:
            self.menu_pick() if name == "SEL" else (
                self._menu_back(),
                self._menu_render() if self.dev_page == PAGE_MENU else None)
            return
        if name == "EDIT" and self.dev_page == PAGE_DETAIL and \
                self.dev_proto >= 3:
            self.toggle_view()                 # EDIT again: the other view
            return
        if name in CHORD_BUTTONS and self.dev_proto >= 3:
            # Wait for the release: keys tapped meanwhile make it a chord.
            self.chord = {"name": name, "used": False, "fleet": fleet,
                          "t": time.time()}
            self._chord_leds(name)
            return
        self._press(name, fleet)

    def _press(self, name, fleet):
        if name == "GLO":
            self.toast("MARKED x%d: REC FX ENV SEQ LFO" % len(self.marks)
                       if self.marks else "ALL: REC FX ENV SEQ ARP",
                       F_INFO, 30.0)
            # Already on APPROVALS (the page report comes after the button):
            # GLO again goes to whoever has waited longest.
            if self.dev_page == PAGE_APPROVALS and self.dev_proto >= 3 \
                    and not self.marks:
                self.jump_urgent()
        elif name in ("OCT-", "OCT+"):
            self.page = max(0, min(1, self.page + (1 if name == "OCT+"
                                                    else -1)))
            print("[device] slot page %d" % self.page)
        elif name == "PLAY":
            self.do_action("start")
        elif name == "REC":
            self.do_fleet("approve") if fleet else self.do_action("approve")
        elif name == "SAVE":
            self.approve_always()
        elif name == "FX":
            self.do_fleet("deny") if fleet else self.do_action("deny")
        elif name == "ENV":
            self.do_fleet("interrupt") if fleet \
                else self.do_action("interrupt")
        elif name == "SEQ":
            self.do_fleet("send") if fleet else self.send_preset()
        elif name == "ARP":
            self.auto_off_all() if fleet else self.toggle_auto()
        elif name == "SEL":
            self.send_keys(["enter"], "ENTER >")
        elif name == "LFO":
            if fleet:
                self.clear_marks()
            else:
                self.send_keys(["shift+tab"], "MODE >")
        elif name == "EDIT":
            if self.cfg.get("focus_on_edit", True):
                self.focus()

    def on_encoder(self, name, delta):
        c = self.chord
        combo = (c["name"], name) if c else None
        if combo in (("PLAY", "PRESETS"), ("SEQ", "PRESETS"), ("SEL", "K1"),
                     ("LFO", "K4")):
            c["used"] = True                   # the button was a modifier
            if combo == ("PLAY", "PRESETS"):
                self.cycle_default_agent(delta)
            elif combo == ("SEQ", "PRESETS"):
                self.adjust_repeat(delta)
            elif combo == ("SEL", "K1"):
                self.scroll_by(delta * 3)
            else:
                self.jump_marked(delta)
            return
        if name == "K1" and self.dev_page == PAGE_MENU:
            self.menu_move(delta, MENU_ROWS)   # a page of the menu at a time
        elif name == "K1":
            self.scroll_by(delta)
        elif name == "K2":
            self.send_keys(["down" if delta > 0 else "up"] *
                           min(abs(delta), 5), None)
        elif name == "K3":
            self.send_keys(["right" if delta > 0 else "left"] *
                           min(abs(delta), 5), None)
        elif name == "K4":
            self.jump_attention(delta)
        elif name == "PRESETS":
            self.arm_preset(1 if delta > 0 else -1)
        elif name == "SELECT" and self.dev_page == PAGE_MENU:
            self.menu_move(delta)              # on MENU, SELECT is ours
        # SELECT elsewhere: the device moves its cursor and reports CC 120.
        # ALGORITHM: the device's page; nothing for the host to do.

    def full_resync(self):
        """Forget what the device was sent and send it all again: after its
        hello, and when it comes back from being unplugged."""
        self.need_reset = True
        self.held.clear()
        self.chord = None
        self.sent_text.clear()
        self.sent_btnleds = None
        self.sent_gauge.clear()
        self.sent.clear()
        self.sent_detail.clear()
        self.sent_meta = None
        self.link.frame(FT_HELLO_ACK, bytes([PROTO_VERSION]))
        self.poll_soon = time.time()

    def handle_midi(self, msg):
        t = msg.type
        if t == "note_on" and msg.velocity > 0 and 48 <= msg.note <= 74:
            self.on_key(msg.note - 48)
        elif t == "control_change":
            cc, val = msg.control, msg.value
            if cc == 117:
                print("[device] hello, proto v%d — full resync" % val)
                self.dev_proto = max(1, min(val, PROTO_VERSION))
                self.full_resync()
            elif cc == 116:
                proto = max(1, min(val, PROTO_VERSION))
                if proto != self.dev_proto:    # first heartbeat we hear
                    print("[device] firmware speaks proto v%d" % proto)
                    self.dev_proto = proto
                    if proto < PROTO_VERSION:
                        print("[device] older than this daemon (v%d): the "
                              "newer screens and markers stay off until "
                              "the firmware is updated" % PROTO_VERSION)
                        self.toast("FIRMWARE v%d, DAEMON v%d: UPDATE" % (
                            proto, PROTO_VERSION), F_WARN, 6.0)
                    self.sent.clear()
                    self.sent_detail.clear()
                    self.poll_soon = time.time()
            elif cc == 120:
                self.select_slot(val)
            elif cc == CC_PAGE:
                self.on_page(val)
            elif 102 <= cc <= 115:
                self.on_button(BTN_NAMES[cc - 102], val >= 64)
            elif cc in ENC_NAMES and val not in (0, 64):
                self.on_encoder(ENC_NAMES[cc], val - 64)

    def handle_command(self, line):
        parts = line.strip().split(None, 1)
        if not parts:
            return True
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else None

        def num(default=None):
            try:
                return int(arg)
            except (TypeError, ValueError):
                return default

        if cmd == "quit":
            return False
        if cmd == "select":
            if num() is None:
                print("[cmd] select needs a slot number")
            else:
                self.select_slot(num())
        elif cmd in ("start", "approve", "deny", "interrupt"):
            self.do_action(cmd)
        elif cmd == "prompt":
            self.do_action("prompt", arg or "")
        elif cmd == "always":
            self.approve_always()
        elif cmd == "enter":
            self.send_keys(["enter"], "ENTER >")
        elif cmd == "mode":
            self.send_keys(["shift+tab"], "MODE >")
        elif cmd == "keys" and arg:
            self.send_keys(arg.split(), None)
        elif cmd == "auto":
            self.toggle_auto()
        elif cmd == "focus":
            self.focus()
        elif cmd == "view":
            self.toggle_view()
        elif cmd == "repeat":
            self.toggle_repeat()
        elif cmd == "move" and num() is not None:
            self.move_agent(num())
        elif cmd == "copy":
            self.copy_reply()
        elif cmd == "mark":
            self.toggle_mark(num())
        elif cmd == "unmark":
            self.clear_marks()
        elif cmd == "urgent":
            self.jump_urgent()
        elif cmd == "mute":
            self.toggle_mute()
        elif cmd == "queue":
            a = self.roster.get(self.selected)
            print("[queue] %s" % (self.queue.get(a["id"]) if a else None))
        elif cmd in ("next", "prev"):
            self.jump_attention(1 if cmd == "next" else -1)
        elif cmd == "scroll":
            self.scroll_by(-(num(1)))          # scroll N = N steps back
        elif cmd == "preset":
            if num() is None:
                self.arm_preset(1)
            else:
                self.arm_preset(index=num() - 1)
        elif cmd == "send":
            self.send_preset()
        elif cmd == "spawn":
            self.spawn(arg)
        elif cmd == "all" and arg in FLEET_STATES:
            self.do_fleet(arg)
        elif cmd == "btn" and arg:
            bits = arg.upper().split()
            if bits[0] in BTN_NAMES:
                cc = 102 + BTN_NAMES.index(bits[0])
                how = bits[1] if len(bits) > 1 else "TAP"
                for val in {"UP": [0], "DOWN": [127]}.get(how, [127, 0]):
                    self.handle_midi(types.SimpleNamespace(
                        type="control_change", control=cc, value=val))
            else:
                print("[cmd] no such button: %s" % bits[0])
        elif cmd == "key" and arg:
            bits = arg.upper().split()
            try:
                k = int(bits[0])
                if not 0 <= k < 27:
                    raise ValueError
                if not (len(bits) > 1 and bits[1] == "UP"):
                    if not self.held:          # the device selects, then
                        self.select_slot(self.page * 27 + k)   # sends the note
                    self.handle_midi(types.SimpleNamespace(
                        type="note_on", note=48 + k, velocity=127))
            except ValueError:
                print("[cmd] key N  (N: 0-26)")
        elif cmd == "page" and num() is not None:
            self.handle_midi(types.SimpleNamespace(
                type="control_change", control=CC_PAGE, value=num()))
        elif cmd == "enc" and arg:
            bits = arg.upper().split()
            try:
                self.handle_midi(types.SimpleNamespace(
                    type="control_change", control=ENC_CC[bits[0]],
                    value=64 + max(-63, min(63, int(bits[1])))))
            except (KeyError, IndexError, ValueError):
                print("[cmd] enc NAME N  (NAME: %s)" % " ".join(ENC_CC))
        else:
            print("[cmd] unknown: %s" % cmd)
        return True


SIM_HELP = ("select N | start | approve | always | deny | interrupt | enter"
            " | mode | auto | focus | next | prev | scroll N | preset N |"
            " send | prompt TEXT | spawn [KIND] | all approve|deny|interrupt"
            "|send | keys K.. | btn NAME [up|down] | enc NAME N |"
            " key N | page N | view | repeat | move N | copy | mark [N] |"
            " unmark | urgent | mute | queue | quit")


# --- setup: the check-up and the background service ----------------------------
SERVICE_LABEL = "com.agtdeck.daemon"


def service_paths():
    """(kind, unit file, log file) for this computer, or (None, None, None)."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return ("launchd",
                os.path.join(home, "Library", "LaunchAgents",
                             SERVICE_LABEL + ".plist"),
                os.path.join(home, "Library", "Logs", "agtdeck.log"))
    if sys.platform.startswith("linux"):
        return ("systemd",
                os.path.join(home, ".config", "systemd", "user",
                             "agtdeck.service"),
                None)                          # journalctl --user -u agtdeck
    return None, None, None


def service_text(extra_args):
    """The launchd plist or systemd unit that keeps the daemon running. It
    starts this very file with this very Python, and carries the current
    PATH so the daemon finds herdr, tmux, git and the agent CLIs."""
    kind, _, log = service_paths()
    argv = [sys.executable, "-u", os.path.abspath(__file__)] + list(extra_args)
    path = os.environ.get("PATH", "/usr/bin:/bin")
    if kind == "launchd":
        from xml.sax.saxutils import escape
        return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array>
%s
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>%s</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
</dict>
</plist>
""" % (SERVICE_LABEL, "\n".join("    <string>%s</string>" % escape(a)
                                 for a in argv),
       escape(path), escape(log), escape(log))
    if kind == "systemd":
        return """[Unit]
Description=Agent Deck host daemon (FM-1 control surface for coding agents)

[Service]
ExecStart=%s
Environment=PATH=%s
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
""" % (" ".join('"%s"' % a.replace('"', '\\"') for a in argv), path)
    return None


def service_install(extra_args, dry_run=False):
    kind, unit, log = service_paths()
    text = service_text(extra_args)
    if not text:
        print("[service] no service support on %s: start the daemon from "
              "your own startup items" % sys.platform)
        return 1
    if dry_run:
        print("# %s\n%s" % (unit, text), end="")
        return 0
    os.makedirs(os.path.dirname(unit), exist_ok=True)
    with open(unit, "w") as f:
        f.write(text)
    print("[service] wrote %s" % unit)
    if kind == "launchd":
        dom = "gui/%d" % os.getuid()
        subprocess.run(["launchctl", "bootout", dom, unit],
                       capture_output=True)   # an older copy, if loaded
        r = subprocess.run(["launchctl", "bootstrap", dom, unit],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("[service] launchctl: %s" % (r.stderr.strip() or
                                               r.returncode))
            return 1
        print("[service] running, and at every login. Log: %s" % log)
    else:
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        r = subprocess.run(["systemctl", "--user", "enable", "--now",
                            "agtdeck.service"])
        if r.returncode != 0:
            return 1
        print("[service] running, and at every login. Log: "
              "journalctl --user -u agtdeck -f")
    print("[service] to remove it: agtdeck --uninstall-service")
    return 0


def service_uninstall():
    kind, unit, _ = service_paths()
    if not kind:
        return 1
    if kind == "launchd":
        subprocess.run(["launchctl", "bootout", "gui/%d" % os.getuid(), unit],
                       capture_output=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        "agtdeck.service"], capture_output=True)
    if os.path.exists(unit):
        os.remove(unit)
        print("[service] removed %s" % unit)
    else:
        print("[service] nothing installed (%s)" % unit)
    return 0


def doctor(args):
    """Check everything the daemon needs and say what is wrong. Changes
    nothing. Returns 0 when it could run, 1 when something must be fixed."""
    bad = [0]

    def row(ok, what, detail=""):
        mark = {True: "ok  ", False: "FAIL", None: "--  "}[ok]
        if ok is False:
            bad[0] += 1
        print("  %s %-14s %s" % (mark, what, detail))
    print("Agent Deck %s, protocol v%d" % (VERSION, PROTO_VERSION))
    row(sys.version_info >= (3, 8), "python", "%s (%s)" % (
        platform.python_version(), sys.executable))
    # the FM-1
    try:
        import mido
        try:
            pin, pout, ins, outs = MidiLink.find_ports(mido, args.port)
            row(bool(pin and pout), "FM-1 port", "%s" % pin if pin and pout
                else "none of the MIDI ports looks like an FM-1: %s. Is it "
                "plugged in and running Agent Deck?" % (ins or "(no ports)"))
        except Exception as e:
            row(False, "MIDI", "cannot list MIDI ports: %s" % e)
    except ImportError:
        row(False, "mido", "not installed: pip install mido python-rtmidi "
            "(--simulate works without)")
    # the config
    cfg = None
    try:
        cfg, cfg_path = load_config(args.config)
        row(True, "config", cfg_path or "defaults (no %s)" %
            "~/.config/agtdeck/config.json")
    except BackendError as e:
        row(False, "config", str(e))
    for key in ("auto_never", "auto_only"):
        for pat in (cfg or {}).get(key) or []:
            try:
                re.compile(str(pat))
            except re.error as e:
                row(False, key, "%r is not a regular expression: %s"
                    % (pat, e))
    if cfg:
        row(None, "auto-approve", "%d never-patterns%s" % (
            len(cfg.get("auto_never") or []),
            ", allow-list of %d" % len(cfg["auto_only"])
            if cfg.get("auto_only") else ", no allow-list"))
        found = [k for k, c in sorted((cfg.get("agents") or {}).items())
                 if shutil.which(str(c).split()[0])]
        row(bool(found), "agent CLIs", ", ".join(found) if found else
            "none of %s is on PATH" % ", ".join(sorted(cfg["agents"])))
    # the backend
    sock = args.herdr_socket or default_herdr_socket(args.herdr_session)
    have_herdr = os.path.exists(sock)
    if args.backend in ("herdr", "auto") and (have_herdr or
                                              args.backend == "herdr"):
        try:
            n = len(HerdrBackend(sock).list_agents())
            row(True, "herdr", "%s, %d agent%s" % (sock, n,
                                                   "" if n == 1 else "s"))
        except Exception as e:
            row(False, "herdr", "%s: %s" % (sock, e))
    else:
        row(None, "herdr", "no socket at %s" % sock)
    if args.backend == "tmux" or (args.backend == "auto" and not have_herdr):
        if not shutil.which("tmux"):
            row(False, "tmux", "not installed, and no Herdr socket either")
        else:
            try:
                n = len(TmuxBackend().list_agents())
                row(True, "tmux", "%d pane%s" % (n, "" if n == 1 else "s"))
            except Exception as e:
                row(None, "tmux", "no server running yet (%s)" % e)
    logs = os.path.expanduser(CLAUDE_PROJECTS)
    row(None if not os.path.isdir(logs) else True, "claude logs",
        logs if os.path.isdir(logs) else "%s not found: INFO, gauges and "
        "blocked reasons stay empty" % logs)
    kind, unit, _ = service_paths()
    row(None, "service", "%s installed" % unit if unit and
        os.path.exists(unit) else "not installed (--install-service)")
    print("%s" % ("all good" if not bad[0] else
                  "%d thing%s to fix" % (bad[0], "" if bad[0] == 1 else "s")))
    return 1 if bad[0] else 0


def watch_link(d, port, event_q):
    """The link to use from here on: the same one while the FM-1 is there, a
    NullLink once it is gone, a fresh MidiLink (and a full resync) when it
    is back. Called every couple of seconds."""
    link = d.link
    if isinstance(link, MidiLink):
        if link.alive():
            return link
        print("[midi] the FM-1 is gone (unplugged?) - waiting for it")
        link.close()
        return NullLink()
    try:
        link = MidiLink(port, event_q)
    except BackendError:
        return d.link                          # still not there
    except Exception as e:                     # there, but not ready yet
        print("[midi] cannot open the FM-1 yet: %s" % e)
        return d.link
    print("[midi] the FM-1 is back - full resync")
    d.link = link
    d.dev_proto = 1                            # until it says
    d.full_resync()
    return link


def main(argv=None):
    ap = argparse.ArgumentParser(description="Agent Deck host daemon")
    ap.add_argument("--backend", choices=["herdr", "tmux", "auto"],
                    default="auto")
    ap.add_argument("--simulate", action="store_true",
                    help="no MIDI; print frames, read commands from stdin")
    ap.add_argument("--port", default=None,
                    help="MIDI port name substring (default: match FM-1)")
    ap.add_argument("--herdr-session", default=None)
    ap.add_argument("--herdr-socket", default=None)
    ap.add_argument("--poll", type=float, default=1.0,
                    help="seconds between backend polls (default 1.0)")
    ap.add_argument("--config", default=None,
                    help="JSON config (default: "
                         "~/.config/agtdeck/config.json if present)")
    ap.add_argument("--no-wait", action="store_true",
                    help="exit when no FM-1 is found, instead of waiting "
                         "for one to be plugged in")
    ap.add_argument("--doctor", action="store_true",
                    help="check the FM-1, the config and the backend, "
                         "report, and exit")
    ap.add_argument("--install-service", action="store_true",
                    help="start the daemon at every login (launchd on "
                         "macOS, systemd --user on Linux), with the other "
                         "options given here")
    ap.add_argument("--uninstall-service", action="store_true")
    ap.add_argument("--print-service", action="store_true",
                    help="show what --install-service would write")
    ap.add_argument("--print-config", action="store_true",
                    help="print the default config as JSON and exit")
    ap.add_argument("--version", action="version",
                    version="agtdeck %s (protocol v%d)"
                    % (VERSION, PROTO_VERSION))
    args = ap.parse_args(argv)
    if not (args.print_config or args.print_service):
        adopt_old_config_dir()
    if args.print_config:
        print(json.dumps(DEFAULT_CONFIG, indent=2))
        return 0
    if args.doctor:
        return doctor(args)
    if args.uninstall_service:
        return service_uninstall()
    if args.install_service or args.print_service:
        keep = ["--backend", args.backend, "--poll", str(args.poll)]
        for flag, val in (("--port", args.port),
                          ("--herdr-session", args.herdr_session),
                          ("--herdr-socket", args.herdr_socket),
                          ("--config", args.config and
                           os.path.abspath(os.path.expanduser(args.config)))):
            if val:
                keep += [flag, val]
        return service_install(keep, dry_run=args.print_service)

    try:
        cfg, cfg_path = load_config(args.config)
    except BackendError as e:
        print("[agtdeck] %s" % e, file=sys.stderr)
        return 2

    backend = None
    if args.backend in ("herdr", "auto"):
        path = args.herdr_socket or default_herdr_socket(args.herdr_session)
        if args.backend == "herdr" or os.path.exists(path):
            backend = HerdrBackend(path)
    if backend is None:
        backend = TmuxBackend()

    event_q = queue.Queue()
    if args.simulate:
        link = SimulateLink()
    else:
        try:
            link = MidiLink(args.port, event_q)
        except BackendError as e:
            print("[agtdeck] %s" % e, file=sys.stderr)
            if args.no_wait or "mido" in str(e):
                return 1
            print("[agtdeck] waiting for an FM-1 to be plugged in "
                  "(--no-wait to exit instead)")
            link = NullLink()
    d = Daemon(backend, link, cfg, args.simulate)
    print("[agtdeck] backend=%s mode=%s proto=v%d"
          % (backend.name, "simulate" if args.simulate else "midi",
             PROTO_VERSION))
    if isinstance(backend, HerdrBackend):
        print("[agtdeck] herdr socket: %s" % backend.path)
    print("[agtdeck] config: %s, %d prompt presets"
          % (cfg_path or "defaults", len(d.presets)))
    if args.simulate:
        print("[agtdeck] commands: " + SIM_HELP)

        def reader():
            for line in sys.stdin:
                event_q.put(("cmd", line))
            event_q.put(("cmd", "quit"))
        threading.Thread(target=reader, daemon=True).start()

    d.link.frame(FT_HELLO_ACK, bytes([PROTO_VERSION]))
    next_poll = 0.0
    next_link = 0.0
    try:
        while True:
            now = time.time()
            if not args.simulate and now >= next_link:
                next_link = now + 2.0
                d.link = watch_link(d, args.port, event_q)
            herdr_news = getattr(backend, "dirty", lambda: False)()
            if now >= next_poll or (d.poll_soon and now >= d.poll_soon) or \
                    (herdr_news and now >= next_poll - args.poll + 0.25):
                d.poll_soon = 0.0
                d.poll_once()
                next_poll = time.time() + args.poll
            d.tick()
            wait = min(next_poll, d.poll_soon or next_poll) - time.time()
            try:
                ev = event_q.get(timeout=max(0.01, min(wait, 0.2)))
            except queue.Empty:
                continue
            while ev is not None:
                if ev[0] == "midi":
                    d.handle_midi(ev[1])
                elif ev[0] == "cmd" and not d.handle_command(ev[1]):
                    print("[agtdeck] bye")
                    return 0
                try:
                    ev = event_q.get_nowait()
                except queue.Empty:
                    ev = None
    except KeyboardInterrupt:
        print("\n[agtdeck] interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
