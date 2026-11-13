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
  MENU page (ALGORITHM): SELECT moves, SEL picks, FX backs out - new
    agent, send prompt, event log, close agent, settings, help.

Core uses only the Python 3 standard library. Real-MIDI mode additionally
needs `mido` (+ `python-rtmidi`), imported lazily, so `--simulate` runs
anywhere.

Usage:
  python3 agentdeck.py --backend auto|herdr|tmux [--simulate]
                       [--port SUBSTRING] [--herdr-session NAME]
                       [--herdr-socket PATH] [--poll SECONDS]
                       [--config PATH]

Simulate mode reads commands from stdin, one per line:
  select N | start | approve | always | deny | interrupt | enter | mode
  auto | focus | next | prev | scroll N | preset N | send | prompt TEXT
  spawn [KIND] | all approve|deny|interrupt|send | keys K [K...]
  btn NAME [up|down] | enc NAME N | key N [up] | page N | quit
(`btn` / `enc` / `key` / `page` replay a panel control exactly as the FM-1
would send it; `btn NAME` alone is a tap, down then up.)
"""

import argparse
import json
import os
import collections
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

PROTO_VERSION = 3

# --- protocol constants (PROTOCOL.md) ---------------------------------------
CH = 0x0F                      # MIDI channel 16 (low nibble)
CC_FRAME_CTRL = 118
CC_FRAME_DATA = 119
FRAME_END = 0x20
FRAME_RESET = 0x7F
FT_SLOT, FT_DETAIL, FT_META, FT_FOOTER, FT_BEEP, FT_SELECT, FT_TEXT, \
    FT_PAGE, FT_KEYLEDS, FT_HELLO_ACK = \
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0F
CC_PAGE = 121
PAGE_GRID, PAGE_DETAIL, PAGE_APPROVALS, PAGE_MENU = 0, 1, 2, 3
MENU_ROWS = 7                  # rows under the MENU page's title
KIND_AUTO = 0x40               # SLOT kind-byte flag (v2 devices only)
MAX_FRAME = 48
MAX_SLOTS = 54
COLS = 29                      # characters across the FM-1's text area
DETAIL_ROWS = {1: 8, 2: 6, 3: 6}   # detail lines the device shows, by protocol

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
BEEP_ATTENTION, BEEP_DONE, BEEP_ERROR, BEEP_ACK = 1, 2, 3, 4
# MENU text styles (the footer's five, plus:)
T_CURSOR, T_TEXT = 5, 6
# key LED modes in a KEYLEDS frame
L_OFF, L_GLOW, L_ON, L_BLINK = 0, 1, 2, 3

DEFAULT_CONFIG = {
    # Prompt presets: PRESETS arms one, SEQ sends it. A string, or
    # {"label": "short name for the screen", "text": "what is sent"}.
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
    "sounds": True,            # chimes on BLOCKED / DONE / ERROR
    "focus_on_edit": True,     # EDIT also focuses the pane on the computer
}


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))        # deep copy
    explicit = path is not None
    path = os.path.expanduser(path or "~/.config/agentdeck/config.json")
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
            out.append({"label": p.strip(), "text": p})
        elif isinstance(p, dict) and str(p.get("text") or "").strip():
            out.append({"label": str(p.get("label") or p["text"]).strip(),
                        "text": str(p["text"])})
    return out or [{"label": "continue", "text": "continue"}]


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
        want = [port_substr] if port_substr else []
        # "Felucca" last: Agent Deck inherits the platform's USB product
        # name, so that is how a flashed unit shows up on some hosts.
        want += ["FM-1", "FM1", "DINGHY", "AGTDECK", "M-VAVE", "Cuvave",
                 "Felucca"]
        ins, outs = mido.get_input_names(), mido.get_output_names()
        pick_in = next((n for w in want for n in ins
                        if w.lower() in n.lower()), None)
        pick_out = next((n for w in want for n in outs
                         if w.lower() in n.lower()), None)
        if not pick_in or not pick_out:
            raise BackendError(
                "no FM-1 MIDI port found.\n  inputs:  %s\n  outputs: %s"
                % (ins or "(none)", outs or "(none)"))
        self.out = mido.open_output(pick_out)
        self.inp = mido.open_input(pick_in, callback=self._cb)
        print("[midi] in=%r out=%r" % (pick_in, pick_out))

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


class SimulateLink:
    """No hardware: frames are printed, not sent."""

    FOOTER_STYLE = ["idle", "info", "good", "warn", "bad"]
    BEEPS = {1: "attention", 2: "done", 3: "error", 4: "ack"}

    def frame(self, ftype, payload):
        if ftype == FT_SLOT and len(payload) >= 3:
            slot, state, kind = payload[0], payload[1], payload[2]
            name = payload[3:].decode("ascii", "replace")
            print("[frame] SLOT slot=%d state=%s kind=%s%s name=%s"
                  % (slot, STATES[state], KINDS[kind & 0x3F],
                     " auto" if kind & KIND_AUTO else "", name))
        elif ftype == FT_DETAIL and len(payload) >= 1:
            print("[frame] DETAIL line=%d: %s"
                  % (payload[0], payload[1:].decode("ascii", "replace")))
        elif ftype == FT_META and len(payload) >= 2:
            print("[frame] META agents=%d blocked=%d"
                  % (payload[0], payload[1]))
        elif ftype == FT_FOOTER and len(payload) >= 1:
            style = self.FOOTER_STYLE[payload[0]] \
                if payload[0] < len(self.FOOTER_STYLE) else str(payload[0])
            print("[frame] FOOTER %s: %s"
                  % (style, payload[1:].decode("ascii", "replace")))
        elif ftype == FT_BEEP and len(payload) >= 1:
            print("[frame] BEEP %s" % self.BEEPS.get(payload[0], payload[0]))
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
            rid = "agentdeck-%d" % self._id
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
            s.sendall(b'{"id": "agentdeck-events",'
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
        return {"id": aid, "name": str(name), "kind": kind, "state": state,
                "cwd": cwd}

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
CHORD_BUTTONS = ("REC", "FX", "ENV", "SEQ", "SEL")
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
    "MASTER    chime volume"]
SLOTS_FILE = "~/.config/agentdeck/slots.json"


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
        self.dev_proto = 3 if simulate else 1   # until the device says
        self.dev_page = PAGE_GRID  # the page the device shows (CC 121)
        self.chord = None          # {"name", "used", "fleet"}: a held button
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
        if self.cfg.get("sounds", True):
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
        """Housekeeping between events: expire the toast."""
        if self.toast_text and time.time() >= self.toast_until:
            self._send_footer()

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

    def refresh_detail(self, force=False):
        a = self.roster.get(self.selected)
        if a:
            try:
                self.detail_all = wrap_lines(
                    self.backend.read_lines(a["id"], 200))
            except Exception:
                self.detail_all = []
        else:
            self.detail_all = []
        self._send_detail(force)

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
        self._assign_slots(agents)
        agents = [a for a in agents if a["id"] in self.slots]
        self.roster = {self.slots[a["id"]]: a for a in agents}
        if not self.sel_known and self.roster and \
                self.selected not in self.roster:
            self.selected = min(self.roster)   # a v1 device never says
        self._auto_approve(agents)
        self._notify(agents)

        full = self.need_reset or time.time() - self.last_full > 10.0
        if self.need_reset:
            # A fresh device or a fresh daemon: wipe, then resend it all.
            self.link.reset()
            self.need_reset = False
            self.sent_footer = None
        if full:
            self.last_full = time.time()
        blocked = sum(1 for a in agents if a["state"] == "BLOCKED")
        meta = (min(len(agents), 99), min(blocked, 99))
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
            sig = (a["state"], a["kind"], a["name"], auto)
            if old is None or old[0] != a["state"]:
                print("[state] slot %d %s: %s -> %s"
                      % (slot, a["name"], old[0] if old else "NONE",
                         a["state"]))
            if sig != old or full:
                kind = KIND_CODE[a["kind"]]
                if auto and self.dev_proto >= 2:
                    kind |= KIND_AUTO
                payload = bytes([slot, ST[a["state"]], kind]) + \
                    clean_text(a["name"], 44).encode("ascii", "replace")
                self.link.frame(FT_SLOT, payload)
                self.sent[slot] = sig
        self.refresh_detail(force=full)
        if full:
            self.sent_footer = None
            self.sent_text.clear()
        self._send_footer()
        if self.dev_page == PAGE_MENU:
            self._menu_render()                # its ages and names move

    def _auto_approve(self, agents):
        now = time.time()
        for a in agents:
            if a["id"] in self.auto and a["state"] == "BLOCKED" and \
                    now - self.auto_t.get(a["id"], 0.0) > 3.0:
                self.auto_t[a["id"]] = now
                print("[auto] approve -> %s" % a["name"])
                try:
                    self.backend.act(a["id"], "approve", None, kind=a["kind"])
                    self.toast("AUTO-OK %s" % a["name"], F_GOOD, 2.0)
                    self.log_event("%s auto-ok" % a["name"])
                except Exception as e:
                    print("[auto] approve failed: %s" % e)
                self.poll_soon = now + 0.4

    def _notify(self, agents):
        """Chime + toast when an agent starts needing a human."""
        best = None
        for a in agents:
            old = self.prev_state.get(a["id"])
            self.prev_state[a["id"]] = a["state"]
            if old is None or old == a["state"]:
                continue                       # first sight is not news
            if a["state"] in ("BLOCKED", "ERROR", "DONE"):
                self.log_event("%s %s" % (a["name"], a["state"]))
            if a["state"] == "BLOCKED" and a["id"] not in self.auto:
                ev = (0, BEEP_ATTENTION, "%s NEEDS YOU" % a["name"], F_WARN)
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
        if self._run(a, action, arg):
            done = label or PAST.get(action, action.upper())
            self.toast("%s %s" % (done, a["name"]), F_GOOD, 2.0)
            self.log_event("%s %s" % (a["name"], done.lower().rstrip(" >")))
        self.poll_soon = time.time() + 0.4

    def do_fleet(self, action):
        """GLO held: the action, for every agent it applies to."""
        states = FLEET_STATES[action]
        targets = [self.roster[s] for s in sorted(self.roster)
                   if self.roster[s]["state"] in states]
        if not targets:
            self.toast("ALL: NONE %s" % "/".join(states), F_WARN)
            return
        backend_action = "prompt" if action == "send" else action
        arg = self.presets[self.preset]["text"] if action == "send" else None
        ok = 0
        for a in targets:
            print("[action] %s (all) -> %s" % (action, a["name"]))
            ok += 1 if self._run(a, backend_action, arg) else 0
        if ok:
            self.toast("%s x%d" % (PAST.get(backend_action, action.upper()),
                                   ok), F_GOOD)
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
            self.toast("ALWAYS OK %s" % a["name"], F_GOOD, 2.0)
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
        self.do_action("prompt", p["text"], label="SENT P%d >" %
                       (self.preset + 1))

    def arm_preset(self, delta=0, index=None):
        n = len(self.presets)
        self.preset = (index if index is not None
                       else self.preset + delta) % n
        text = self._default_footer()[1]
        print("[preset] %s" % text)
        self.toast(text, F_INFO, 1.5)

    def toggle_auto(self):
        a = self.roster.get(self.selected)
        if not a:
            self.toast("SLOT %02d IS EMPTY" % self.selected, F_WARN)
            return
        on = a["id"] not in self.auto
        (self.auto.add if on else self.auto.discard)(a["id"])
        print("[auto] %s for slot %d (%s)" % ("ON" if on else "off",
                                              self.selected, a["name"]))
        self.toast("AUTO-APPROVE %s %s" % ("ON" if on else "OFF", a["name"]),
                   F_WARN if on else F_INFO)
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

    def spawn(self, kind=None, cwd=None):
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
        label = os.path.basename(cwd.rstrip("/")) or kind
        print("[spawn] %s in %s (slot %d)" % (command, cwd, self.selected))
        try:
            self.backend.spawn(command, cwd, label,
                               near=(near or {}).get("id"))
        except Exception as e:
            self._fail("spawn", e)
            return
        if self.selected not in self.roster:
            self.spawn_slot = (self.selected, time.time() + 15.0)
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

    def menu_move(self, delta):
        if self.menu:
            self.menu[-1][1] += 1 if delta > 0 else -1
        self._menu_render()

    def menu_pick(self):
        if not self.menu:
            self._menu_render()
        builder, cur = self.menu[-1]
        _, items = builder()
        if 0 <= cur < len(items) and items[cur][1]:
            items[cur][1]()
        if self.dev_page == PAGE_MENU:
            self._menu_render()

    def _target(self):
        a = self.roster.get(self.selected)
        return a["name"] if a else "SLOT %02d" % self.selected

    def _m_root(self):
        return "MENU - %s" % self._target(), [
            ("NEW AGENT", self._menu_push(self._m_new)),
            ("SEND PROMPT", self._menu_push(self._m_prompts)),
            ("EVENT LOG", self._menu_push(self._m_log)),
            ("CLOSE AGENT", self._menu_push(self._m_close)),
            ("SETTINGS", self._menu_push(self._m_settings)),
            ("HELP", self._menu_push(self._m_help))]

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
        items = []
        for d in dirs:
            for k in kinds:
                def go(k=k, d=d):
                    self.spawn(k, d)
                    self._menu_leave()
                items.append(("%s %s" % (
                    k, os.path.basename(d.rstrip("/")) or d), go))
        return "NEW AGENT IN...", items or [("(no agent CLI found)", None)]

    def _m_prompts(self):
        items = []
        for i, p in enumerate(self.presets):
            def go(i=i):
                self.preset = i
                self.send_preset()
            items.append(("%d %s" % (i + 1, p["label"]), go))
        return "PROMPT > %s" % self._target(), items

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

        def resync():
            self.need_reset = True
            self.sent.clear()
            self.sent_detail.clear()
            self.sent_meta = None
            self.poll_soon = time.time()
        on = lambda key: "ON" if self.cfg.get(key, True) else "OFF"  # noqa
        return "SETTINGS", [
            ("SOUNDS: %s" % on("sounds"), flip("sounds")),
            ("FOCUS ON EDIT: %s" % on("focus_on_edit"),
             flip("focus_on_edit")),
            ("AUTO-APPROVE OFF, ALL (%d)" % len(self.auto),
             self.auto_off_all),
            ("RESYNC THE DEVICE", resync)]

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
                self.send_keys([str(k + 1)], "OPTION %d >" % (k + 1))

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
                    self.toast_text[1].startswith("ALL:"):
                self.toast_until = 0.0         # drop the modifier hint
                self._send_footer()
            c = self.chord
            if c and c["name"] == name:
                self.chord = None
                if not c["used"]:              # a plain tap after all
                    self._press(name, c["fleet"])
            return
        self.held.add(name)
        fleet = "GLO" in self.held and name != "GLO"
        if self.dev_page == PAGE_MENU and name in ("SEL", "FX") and not fleet:
            self.menu_pick() if name == "SEL" else (
                self._menu_back(),
                self._menu_render() if self.dev_page == PAGE_MENU else None)
            return
        if name in CHORD_BUTTONS and self.dev_proto >= 3:
            # Wait for the release: keys tapped meanwhile make it a chord.
            self.chord = {"name": name, "used": False, "fleet": fleet}
            self._chord_leds(name)
            return
        self._press(name, fleet)

    def _press(self, name, fleet):
        if name == "GLO":
            self.toast("ALL: REC FX ENV SEQ ARP", F_INFO, 30.0)
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
            self.send_keys(["shift+tab"], "MODE >")
        elif name == "EDIT":
            if self.cfg.get("focus_on_edit", True):
                self.focus()
        # HOME: a page jump on the device, nothing for the host to do

    def on_encoder(self, name, delta):
        if name == "K1":
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

    def handle_midi(self, msg):
        t = msg.type
        if t == "note_on" and msg.velocity > 0 and 48 <= msg.note <= 74:
            self.on_key(msg.note - 48)
        elif t == "control_change":
            cc, val = msg.control, msg.value
            if cc == 117:
                print("[device] hello, proto v%d — full resync" % val)
                self.dev_proto = max(1, min(val, PROTO_VERSION))
                self.need_reset = True
                self.held.clear()
                self.chord = None
                self.sent_text.clear()
                self.sent.clear()
                self.sent_detail.clear()
                self.sent_meta = None
                self.link.frame(FT_HELLO_ACK, bytes([PROTO_VERSION]))
                self.poll_soon = time.time()
            elif cc == 116:
                proto = max(1, min(val, PROTO_VERSION))
                if proto != self.dev_proto:    # first heartbeat we hear
                    print("[device] firmware speaks proto v%d" % proto)
                    self.dev_proto = proto
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
            " key N | page N | quit")


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
                         "~/.config/agentdeck/config.json if present)")
    args = ap.parse_args(argv)

    try:
        cfg, cfg_path = load_config(args.config)
    except BackendError as e:
        print("[agentdeck] %s" % e, file=sys.stderr)
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
            print("[agentdeck] %s" % e, file=sys.stderr)
            return 1
    d = Daemon(backend, link, cfg, args.simulate)
    print("[agentdeck] backend=%s mode=%s proto=v%d"
          % (backend.name, "simulate" if args.simulate else "midi",
             PROTO_VERSION))
    if isinstance(backend, HerdrBackend):
        print("[agentdeck] herdr socket: %s" % backend.path)
    print("[agentdeck] config: %s, %d prompt presets"
          % (cfg_path or "defaults", len(d.presets)))
    if args.simulate:
        print("[agentdeck] commands: " + SIM_HELP)

        def reader():
            for line in sys.stdin:
                event_q.put(("cmd", line))
            event_q.put(("cmd", "quit"))
        threading.Thread(target=reader, daemon=True).start()

    d.link.frame(FT_HELLO_ACK, bytes([PROTO_VERSION]))
    next_poll = 0.0
    try:
        while True:
            now = time.time()
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
                    print("[agentdeck] bye")
                    return 0
                try:
                    ev = event_q.get_nowait()
                except queue.Empty:
                    ev = None
    except KeyboardInterrupt:
        print("\n[agentdeck] interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
