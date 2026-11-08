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

Core uses only the Python 3 standard library. Real-MIDI mode additionally
needs `mido` (+ `python-rtmidi`), imported lazily, so `--simulate` runs
anywhere.

Usage:
  python3 agentdeck.py --backend auto|herdr|tmux [--simulate]
                       [--port SUBSTRING] [--herdr-session NAME]
                       [--herdr-socket PATH] [--poll SECONDS]

Simulate mode reads commands from stdin, one per line:
  select N | start | approve | deny | interrupt | prompt TEXT | quit
"""

import argparse
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time

PROTO_VERSION = 1

# --- protocol constants (PROTOCOL.md) ---------------------------------------
CH = 0x0F                      # MIDI channel 16 (low nibble)
CC_FRAME_CTRL = 118
CC_FRAME_DATA = 119
FRAME_END = 0x20
FRAME_RESET = 0x7F
FT_SLOT, FT_DETAIL, FT_META, FT_HELLO_ACK = 0x01, 0x02, 0x03, 0x0F
MAX_FRAME = 48

STATES = ["EMPTY", "IDLE", "WORKING", "BLOCKED", "DONE", "ERROR", "UNKNOWN"]
ST = {name: i for i, name in enumerate(STATES)}
KINDS = ["unknown", "claude", "codex", "opencode", "grok", "copilot", "shell"]
KIND_CODE = {name: i for i, name in enumerate(KINDS)}

BTN_NAMES = ["OCT-", "OCT+", "FX", "SEL", "ENV", "LFO", "EDIT", "GLO",
             "HOME", "SAVE", "ARP", "SEQ", "PLAY", "REC"]
ENC_NAMES = {85: "SELECT", 86: "ALGORITHM", 87: "K1", 88: "K2", 89: "K3",
             90: "K4", 91: "PRESETS"}


class BackendError(Exception):
    pass


def clean_text(s, limit):
    """Printable ASCII, single line, truncated — the wire format's alphabet."""
    s = "".join(c if 32 <= ord(c) <= 126 else " " for c in str(s))
    return re.sub(r"\s+", " ", s).strip()[:limit]


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
        want += ["FM-1", "FM1", "DINGHY", "AGTDECK", "M-VAVE", "Cuvave"]
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
        self.event_q.put(msg)

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

    def frame(self, ftype, payload):
        if ftype == FT_SLOT and len(payload) >= 3:
            slot, state, kind = payload[0], payload[1], payload[2]
            name = payload[3:].decode("ascii", "replace")
            print("[frame] SLOT slot=%d state=%s kind=%s name=%s"
                  % (slot, STATES[state], KINDS[kind], name))
        elif ftype == FT_DETAIL and len(payload) >= 1:
            print("[frame] DETAIL line=%d: %s"
                  % (payload[0], payload[1:].decode("ascii", "replace")))
        elif ftype == FT_META and len(payload) >= 2:
            print("[frame] META agents=%d blocked=%d"
                  % (payload[0], payload[1]))
        elif ftype == FT_HELLO_ACK:
            print("[frame] HELLO_ACK proto=%d" % (payload[0] if payload else 0))
        else:
            print("[frame] type=0x%02X payload=%r" % (ftype, bytes(payload)))

    def reset(self):
        print("[frame] RESET (clear all slots)")


# --- backends ---------------------------------------------------------------
# Backend contract:
#   list_agents() -> [ {id, name, kind, state, detail:[lines]} ]
#     id: stable string; kind: one of KINDS; state: one of STATES.
#   act(agent_id, action, arg=None)
#     action in {"start", "approve", "deny", "interrupt", "prompt"}
# Raise BackendError (or anything — the daemon catches all) on failure.

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

    def _capture(self, pane_id):
        try:
            return self._tmux("capture-pane", "-p", "-t", pane_id, "-S", "-40")
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
                         "#{pane_current_command}", "#{pane_title}"])
        out = self._tmux("list-panes", "-a", "-F", fmt)
        agents = []
        now = time.time()
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            pane_id, sess, win, cmd, _title = parts[:5]
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
            elif kind == "shell":
                state = "IDLE"
            else:
                state = "IDLE"
            agents.append({
                "id": pane_id,
                "name": sess if sess else pane_id,
                "kind": kind,
                "state": state,
                "detail": [clean_text(ln, 60) for ln in lines[-8:]],
                "_cmd": cmd,
            })
        return agents

    def _send(self, pane_id, *keys):
        for k in keys:
            self._tmux("send-keys", "-t", pane_id, k)

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

    # -- socket plumbing --
    def request(self, method, params=None):
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
                s.settimeout(5.0)
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
                raise BackendError("herdr socket error (%s): %s"
                                   % (method, e))
            finally:
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass

    def _subscribe_thread(self):
        """Best effort: events.subscribe on its own connection.

        Events just flag the roster dirty; the 1 s poll in the daemon is the
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

    def _normalise(self, raw, source):
        aid = None
        for k in ("id", "agent_id", "pane_id", "pane"):
            if raw.get(k):
                aid = str(raw[k])
                break
        if aid is None:
            return None
        kind_raw = " ".join(str(raw.get(k) or "") for k in
                            ("kind", "agent", "display_agent", "name",
                             "title")).lower()
        kind = next((k for k in KINDS if k in kind_raw), "unknown")
        if kind == "unknown" and source == "pane":
            kind = "shell"
        state_raw = str(raw.get("agent_status") or raw.get("state") or
                        raw.get("status") or "unknown").lower()
        state = self.STATE_MAP.get(state_raw, "UNKNOWN")
        name = (raw.get("name") or raw.get("label") or raw.get("title") or
                raw.get("terminal_title_stripped"))
        if not name:
            agent_label = raw.get("display_agent") or raw.get("agent")
            if agent_label:
                name = "%s %s" % (agent_label, aid)
            elif raw.get("cwd"):
                name = os.path.basename(str(raw["cwd"]).rstrip("/")) or aid
            else:
                name = aid
        detail = []
        for k in ("last_line", "summary", "detail", "last_output"):
            if raw.get(k):
                detail = [clean_text(raw[k], 60)]
                break
        return {"id": aid, "name": str(name), "kind": kind,
                "state": state, "detail": detail}

    def _read_detail(self, pane_id):
        """Last visible lines for the FM-1 detail page (pane.read)."""
        try:
            res = self.request("pane.read", {
                "pane_id": pane_id, "source": "recent_unwrapped",
                "lines": 8, "format": "text", "strip_ansi": True})
        except BackendError:
            return []
        text = ""
        if isinstance(res, dict):
            read = res.get("read") if isinstance(res.get("read"), dict) else {}
            text = read.get("text") or res.get("text") or ""
        lines = [ln.rstrip() for ln in str(text).splitlines() if ln.strip()]
        return [clean_text(ln, 60) for ln in lines[-8:]]

    def list_agents(self):
        agents = []
        try:
            res = self.request("agent.list")
            agents = [a for a in (self._normalise(r, "agent")
                                  for r in self._extract(
                                      res, "agents", "items")) if a]
        except BackendError:
            agents = []
        if not agents:
            res = self.request("pane.list")
            agents = [a for a in (self._normalise(r, "pane")
                                  for r in self._extract(
                                      res, "panes", "items")) if a]
        for agent in agents:
            if not agent["detail"]:
                agent["detail"] = self._read_detail(agent["id"])
        if agents and not self._subscribe_started:
            self._subscribe_started = True
            threading.Thread(target=self._subscribe_thread,
                             daemon=True).start()
        return agents

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
                "timeout_ms": 30000})
            return
        keys = {"approve": self.APPROVE_KEYS.get(kind, ["enter"]),
                "deny": ["esc"],
                "interrupt": ["ctrl+c"]}.get(action)
        if keys is None:
            raise BackendError("herdr: unknown action %r" % action)
        # Pane-level keys are the reliable path here: agent.send_keys only
        # accepts an active *named* agent and rejects e.g. lifecycle-
        # reported occupants with agent_not_ready, while pane.send_keys
        # takes the pane ID this backend uses as its agent ID.
        self.request("pane.send_keys", {"pane_id": agent_id, "keys": keys})

    def _send_text(self, agent_id, text):
        """Literal text (no Enter) via the verified pane.send_text method."""
        self.request("pane.send_text", {"pane_id": agent_id, "text": text})


def default_herdr_socket(session=None):
    env = os.environ.get("HERDR_SOCKET_PATH")
    if session:
        return os.path.expanduser(
            "~/.config/herdr/sessions/%s/herdr.sock" % session)
    if env:
        return env
    return os.path.expanduser("~/.config/herdr/herdr.sock")


# --- daemon -----------------------------------------------------------------

BUTTON_ACTIONS = {"PLAY": "start", "REC": "approve", "SAVE": "approve",
                  "FX": "deny", "ENV": "interrupt"}


class Daemon:
    def __init__(self, backend, link, simulate):
        self.backend = backend
        self.link = link
        self.simulate = simulate
        self.slots = {}            # agent_id -> slot
        self.roster = {}           # slot -> agent dict
        self.selected = 0
        self.sent = {}             # slot -> (state, kind, name) last framed
        self.sent_detail = {}      # line -> text last framed
        self.sent_meta = None
        self.last_full = 0.0
        self.last_error = None
        self.fail_streak = 0

    # -- roster / framing --
    def _assign_slots(self, agents):
        used = set(self.slots.values())
        for a in agents:
            if a["id"] not in self.slots:
                slot = next(i for i in range(54) if i not in used)
                self.slots[a["id"]] = slot
                used.add(slot)
                print("[discover] slot %d: %s (id=%s, kind=%s)"
                      % (slot, a["name"], a["id"], a["kind"]))
        live = {a["id"] for a in agents}
        for aid in [k for k in self.slots if k not in live]:
            slot = self.slots.pop(aid)
            self.sent.pop(slot, None)
            print("[gone] slot %d: %s" % (slot, aid))

    def poll_once(self):
        try:
            agents = self.backend.list_agents()
        except Exception as e:                     # never kill the loop
            msg = str(e)
            if msg != self.last_error:
                print("[backend] %s unavailable: %s — retrying"
                      % (self.backend.name, msg))
                self.last_error = msg
            self.fail_streak += 1
            return
        if self.last_error is not None:
            print("[backend] %s is back" % self.backend.name)
            self.last_error = None
        self.fail_streak = 0
        self._assign_slots(agents)
        self.roster = {self.slots[a["id"]]: a for a in agents}
        if self.roster and self.selected not in self.roster:
            self.selected = min(self.roster)
        full = time.time() - self.last_full > 10.0
        if full:
            self.link.reset()
            self.sent.clear()
            self.sent_detail.clear()
            self.last_full = time.time()
        blocked = sum(1 for a in agents if a["state"] == "BLOCKED")
        meta = (len(agents), blocked)
        if meta != self.sent_meta or full:
            self.link.frame(FT_META, bytes([meta[0], meta[1]]))
            self.sent_meta = meta
        for slot in sorted(self.roster):
            a = self.roster[slot]
            sig = (a["state"], a["kind"], a["name"])
            old = self.sent.get(slot)
            if old is None or old[0] != a["state"]:
                print("[state] slot %d %s: %s -> %s"
                      % (slot, a["name"], old[0] if old else "NONE",
                         a["state"]))
            if sig != old or full:
                payload = bytes([slot, ST[a["state"]],
                                 KIND_CODE[a["kind"]]]) + \
                    clean_text(a["name"], 44).encode("ascii", "replace")
                self.link.frame(FT_SLOT, payload)
                self.sent[slot] = sig
        sel = self.roster.get(self.selected)
        if sel:
            for i, line in enumerate(sel["detail"][:8]):
                if self.sent_detail.get(i) != line or full:
                    self.link.frame(FT_DETAIL, bytes([i]) +
                                    line.encode("ascii", "replace"))
                    self.sent_detail[i] = line

    # -- actions --
    def do_action(self, action, arg=None):
        a = self.roster.get(self.selected)
        if not a:
            print("[action] %s: no agent selected" % action)
            return
        print("[action] %s -> slot %d (%s)" % (action, self.selected,
                                               a["name"]))
        try:
            self.backend.act(a["id"], action, arg, kind=a["kind"])
        except Exception as e:
            print("[action] %s failed: %s" % (action, e))

    def select_slot(self, slot):
        if slot in self.roster:
            self.selected = slot
            self.sent_detail.clear()
            print("[select] slot %d (%s)" % (slot, self.roster[slot]["name"]))
        elif self.roster:
            print("[select] slot %d is empty" % slot)

    # -- device events --
    def handle_midi(self, msg):
        t = msg.type
        if t == "note_on" and msg.velocity > 0 and 48 <= msg.note <= 74:
            self.select_slot(msg.note - 48 + self._page * 27
                             if hasattr(self, "_page") else msg.note - 48)
        elif t == "control_change":
            cc, val = msg.control, msg.value
            if cc == 117:
                print("[device] hello, proto v%d — full resync" % val)
                self.last_full = 0.0
                self.link.frame(FT_HELLO_ACK, bytes([PROTO_VERSION]))
            elif cc == 120:
                self.select_slot(val)
            elif 102 <= cc <= 115 and val == 127:
                name = BTN_NAMES[cc - 102]
                if name in ("OCT-", "OCT+"):
                    self._page = getattr(self, "_page", 0) + (1 if name == "OCT+" else -1)
                    self._page = max(0, min(1, self._page))
                    print("[device] slot page %d" % self._page)
                elif name in BUTTON_ACTIONS:
                    self.do_action(BUTTON_ACTIONS[name])
                else:
                    print("[device] button %s (no host action)" % name)
            elif cc in ENC_NAMES and val not in (0, 64):
                delta = val - 64
                if ENC_NAMES[cc] == "SELECT":
                    slots = sorted(self.roster)
                    if slots:
                        i = slots.index(self.selected) \
                            if self.selected in slots else 0
                        self.select_slot(slots[(i + delta) % len(slots)])
                else:
                    print("[device] encoder %s %+d" % (ENC_NAMES[cc], delta))

    def handle_command(self, line):
        parts = line.strip().split(None, 1)
        if not parts:
            return True
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else None
        if cmd == "quit":
            return False
        if cmd == "select" and arg is not None:
            try:
                self.select_slot(int(arg))
            except ValueError:
                print("[cmd] select needs a slot number")
        elif cmd in ("start", "approve", "deny", "interrupt"):
            self.do_action(cmd)
        elif cmd == "prompt":
            self.do_action("prompt", arg or "")
        else:
            print("[cmd] unknown: %s" % cmd)
        return True


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
    ap.add_argument("--poll", type=float, default=1.0)
    args = ap.parse_args(argv)

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
        link = MidiLink(args.port, event_q)
    d = Daemon(backend, link, args.simulate)
    print("[agentdeck] backend=%s mode=%s proto=v%d"
          % (backend.name, "simulate" if args.simulate else "midi",
             PROTO_VERSION))
    if isinstance(backend, HerdrBackend):
        print("[agentdeck] herdr socket: %s" % backend.path)
    if args.simulate:
        print("[agentdeck] commands: select N | start | approve | deny |"
              " interrupt | prompt TEXT | quit")

    cmd_q = queue.Queue()
    if args.simulate:
        def reader():
            for line in sys.stdin:
                cmd_q.put(line)
            cmd_q.put("quit")
        threading.Thread(target=reader, daemon=True).start()

    d.link.frame(FT_HELLO_ACK, bytes([PROTO_VERSION]))
    try:
        while True:
            d.poll_once()
            while not event_q.empty():
                d.handle_midi(event_q.get())
            while not cmd_q.empty():
                if not d.handle_command(cmd_q.get()):
                    print("[agentdeck] bye")
                    return 0
            time.sleep(args.poll if args.simulate else 0.25)
    except KeyboardInterrupt:
        print("\n[agentdeck] interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
