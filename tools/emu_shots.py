#!/usr/bin/env python3
"""emu_shots.py - the README's screenshots, from the packaged firmware.

Boots the newest dist/agentdeck-*.fwsc in fm1-emu once per scene, feeds it
the frames a daemon would send for that scene (through the MIDI IN jack,
as tools/emu_test.py does), presses what needs pressing, and saves the LCD
as docs/shot-*.png (2x, crisp pixels). It also plays them in the display of
the hero picture, as docs/intro.gif: the animation at the top of the README.

Nothing here is drawn by hand: every image is the LCD of the firmware
package itself. The scenes are made up (a fleet of eight agents with
plausible names); the pixels are not.

Usage:
  tools/get_emu.sh                    # one-time: fetch the fm1-emu binary
  python3 tools/emu_shots.py          # every scene -> docs/
  python3 tools/emu_shots.py grid about   # just these scenes
  python3 tools/emu_shots.py --out build/shots
  python3 tools/emu_shots.py --intro-only  # just the animation, no emulator

Requires Pillow.
"""
import argparse
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emu_test                                              # noqa: E402

ROOT = emu_test.ROOT
CC_CTRL, CC_DATA = 118, 119
SLOT, DETAIL, META, FOOTER, SELECT, TEXT, PAGE, GAUGE, HELLO_ACK = \
    0x01, 0x02, 0x03, 0x04, 0x06, 0x07, 0x08, 0x0B, 0x0F
EMPTY, IDLE, WORKING, BLOCKED, DONE, ERROR = 0, 1, 2, 3, 4, 5
CLAUDE, CODEX, OPENCODE, GROK, COPILOT, SHELL = 1, 2, 3, 4, 5, 6
AUTO, LOOP, MARK, QUEUE = 0x40, 0x20, 0x10, 0x08
P_GRID, P_DETAIL, P_APPROVALS, P_MENU, P_ABOUT = 0, 1, 2, 3, 4

# (slot, state, kind, flags, name, context %)
FLEET = [
    (0, WORKING, CLAUDE, AUTO, "api-server", 42),
    (1, BLOCKED, CLAUDE, MARK | QUEUE, "web-frontend", 91),
    (2, DONE, CLAUDE, 0, "docs", 18),
    (3, WORKING, CODEX, LOOP, "auth-service", None),
    (4, IDLE, CODEX, 0, "billing", None),
    (5, ERROR, COPILOT, 0, "infra", None),
    (6, IDLE, OPENCODE, MARK, "research", None),
    (7, WORKING, CLAUDE, 0, "mobile-app", 67),
]
MORE = ["search", "payments", "ingest", "worker", "gateway", "cli", "sdk-js",
        "sdk-py", "billing-ui", "admin", "etl", "ml-train", "ml-serve",
        "notifier", "scheduler", "cache", "edge", "metrics", "tracing"]
STATES = [WORKING, IDLE, WORKING, DONE, IDLE, BLOCKED, WORKING, IDLE, ERROR]


class Scene:
    """A timeline of emulator inputs and LCD snapshots."""

    def __init__(self):
        self.items = []
        self.snaps = []                 # (ms, name)
        self.t = 2000

    def cc(self, ctrl, val):
        self.items.append("%d:midi:bf%02x%02x" % (self.t, ctrl, val))
        self.t += 3

    def frame(self, ftype, payload):
        self.cc(CC_CTRL, 0x10 | ftype)
        for b in bytes(payload):
            self.cc(CC_DATA, b & 0x7F)
        self.cc(CC_CTRL, 0x20)

    def text(self, ftype, head, s):
        self.frame(ftype, bytes(head) + s.encode("ascii", "replace"))

    def hello(self):
        self.cc(CC_CTRL, 0x7F)                               # RESET
        self.frame(HELLO_ACK, [6])
        self.text(TEXT, [8, 0], "0.6 via herdr")             # ABOUT's lines
        self.text(TEXT, [9, 0], "macOS, python 3.12")
        self.footer(0, "P2/9 run tests + fix")

    def footer(self, style, s):
        self.text(FOOTER, [style], s)

    def slot(self, slot, state, kind, flags, name, gauge=None):
        self.text(SLOT, [slot, state, kind | flags], name)
        if gauge is not None:
            self.frame(GAUGE, [slot, gauge])

    def fleet(self, agents=FLEET, view=0):
        for a in agents:
            self.slot(*a)
        self.meta(agents, view)

    def meta(self, agents, view=0):
        self.frame(META, [len(agents),
                          sum(1 for a in agents if a[1] == BLOCKED), view])

    def detail(self, lines):
        for i in range(6):
            self.text(DETAIL, [i], lines[i] if i < len(lines) else "")

    def menu(self, title, rows, cursor=0):
        self.text(TEXT, [0, 1], title)
        for i in range(7):
            row = rows[i] if i < len(rows) else ""
            on = i == cursor and row
            self.text(TEXT, [i + 1, 5 if on else 6],
                      ("> " if on else "  ") + row if row else "")

    def press(self, name):
        self.t += 150
        self.items.append("%d:%s" % (self.t, name))
        self.t += 150

    def snap(self, name, settle=1300):
        """Shoot once the screen has had `settle` ms to catch up."""
        self.t += settle
        self.snaps.append((self.t, name))
        self.t += 100


def big_fleet(n):
    out = list(FLEET)
    for i in range(8, n):
        out.append((i, STATES[i % len(STATES)],
                    [CLAUDE, CODEX, CLAUDE, OPENCODE, COPILOT, GROK][i % 6],
                    0, MORE[(i - 8) % len(MORE)],
                    (i * 37) % 100 if i % 3 == 0 else None))
    return out


def scene_boot(s):
    s.snaps += [(400, "splash"), (1900, "no-host")]
    s.t = 2400


def scene_empty(s):
    s.hello()
    s.frame(META, [0, 0, 0])
    s.snap("empty")


def scene_grid(s):
    s.hello()
    s.fleet()
    s.frame(SELECT, [1])
    s.snap("grid")
    s.footer(3, "web-fronte: Bash npm test")
    s.snap("grid-alert", 700)
    s.footer(0, "P2/9 run tests + fix")
    s.press("HOME")                                          # the boxes
    s.snap("boxes-8")


def scene_boxes(s):
    s.hello()
    s.fleet(FLEET[:4])
    s.frame(SELECT, [1])
    s.press("HOME")
    s.snap("boxes-4")
    s.fleet(big_fleet(16))
    s.snap("boxes-16")
    s.fleet(big_fleet(27))
    s.snap("boxes-27")


def scene_detail(s):
    s.hello()
    s.fleet()
    s.frame(SELECT, [1])
    s.frame(PAGE, [P_DETAIL])
    s.detail(["Bash command",
              "  npm test -- --watch=false",
              "Do you want to proceed?",
              "> 1. Yes",
              "  2. Yes, and don't ask again",
              "  3. No, tell Claude what to do"])
    s.footer(3, "web-fronte: Bash npm test")
    s.snap("detail-output")
    s.footer(0, "P2/9 run tests + fix")
    s.meta(FLEET, 1)
    s.detail(["Fix flaky login test",
              "fable-5-2 ctx 182k",
              "mode default git main ~6",
              "QUEUE 1: P5",
              "ASKED: run the tests and fix",
              "WANTS: Bash npm test"])
    s.snap("detail-info")
    s.meta(FLEET, 2)
    s.detail(["6 FILES +214 -87",
              "+120 -41 login.test.ts",
              "+58 -30 session.ts",
              "+22 -9 auth.ts",
              "new: fixtures/users.json",
              "-- recent commits --"])
    s.snap("detail-diff")


def scene_approvals(s):
    waiting = [(a[0], BLOCKED if a[0] in (3, 7) else a[1]) + a[2:]
               for a in FLEET]
    s.hello()
    s.fleet(waiting)
    s.frame(SELECT, [1])
    s.frame(PAGE, [P_APPROVALS])
    s.snap("approvals")
    s.fleet([(a[0], WORKING if a[1] == BLOCKED else a[1]) + a[2:]
             for a in waiting])
    s.footer(2, "APPROVED x3")
    s.snap("all-clear")


def scene_menu(s):
    s.hello()
    s.fleet()
    s.frame(SELECT, [1])
    s.frame(PAGE, [P_MENU])
    s.menu("MENU - web-frontend",
           ["THIS AGENT", "FLEET", "NEW AGENT", "SEND PROMPT", "EVENT LOG",
            "CLOSE AGENT", "SETTINGS"])
    s.snap("menu")
    rows = [(0, ">", " ", "api-server", "12m", "84k"),     # the daemon's
            (1, "!", "+", "web-frontend", "48s", "182k"),   # own format
            (2, "*", " ", "docs", " 3m", "36k"),
            (3, ">", " ", "auth-service", " 6m", ""),
            (4, ".", " ", "billing", "41m", ""),
            (5, "x", " ", "infra", " 2m", "")]
    s.menu("FLEET 8 1! 3> 1* 1x 2.",
           ["CLEAR MARKS (2)"] + ["%02d %s%s%-10s %3s %s" % (
               n, g, m, name[:10], age, ctx) for n, g, m, name, age, ctx
               in rows], 2)
    s.snap("menu-fleet")
    s.menu("SETTINGS",
           ["SOUNDS: ON", "FEEDBACK SOUNDS: ON", "BLOCKED ALERT: LOUD",
            "NAG WHILE BLOCKED: 5M", "DESKTOP NOTIFY: OFF",
            "FOLLOW THE BLOCKED: ON", "FOCUS ON EDIT: ON"], 3)
    s.snap("menu-settings")
    s.menu("web-frontend",
           ["VIEW: INFO", "REPEAT PROMPT: OFF", "AUTO-APPROVE: OFF",
            "MARKED: YES", "MUTED: NO", "QUEUED: 1 - CLEAR", "COMMANDS"], 6)
    s.snap("menu-agent")


def scene_about(s):
    s.hello()
    s.fleet()
    s.frame(PAGE, [P_ABOUT])
    s.snap("about")


SCENES = {"boot": scene_boot, "empty": scene_empty, "grid": scene_grid,
          "boxes": scene_boxes, "detail": scene_detail,
          "approvals": scene_approvals, "menu": scene_menu,
          "about": scene_about}
# the README's intro animation: (shot, seconds)
INTRO = [("splash", 1.6), ("no-host", 1.6), ("empty", 1.8), ("grid", 2.4),
         ("grid-alert", 2.4), ("detail-output", 2.6), ("detail-info", 2.2),
         ("detail-diff", 2.0), ("approvals", 2.2), ("all-clear", 1.8),
         ("boxes-8", 2.4), ("boxes-16", 1.6), ("boxes-27", 1.6),
         ("menu", 1.8), ("menu-settings", 1.8), ("about", 2.6)]
HERO = "fm1-agent-deck-hero.jpg"        # the FM-1 the screens are set into
HERO_LCD = (641, 319, 286)              # its display: x, y, size in pixels
INTRO_W = 1600                          # at this width the LCD is 240 px: 1:1


def run_scene(emu, fwsc, name, out):
    s = Scene()
    SCENES[name](s)
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "work"))
        cmd = [emu, "--fwsc", fwsc, "--ms", str(s.t + 300),
               "--snap", ",".join(str(t) for t, _ in s.snaps)]
        if s.items:
            cmd += ["--keys", ",".join(s.items)]
        r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0:
            print("  fm1-emu exited %d on scene %s" % (r.returncode, name),
                  file=sys.stderr)
            return False
        ok = True
        for t, shot in s.snaps:
            bmp = os.path.join(tmp, "work", "lcd_%d.bmp" % t)
            png = os.path.join(out, "shot-%s.png" % shot)
            if not os.path.isfile(bmp) or emu_test.convert(bmp, png) is None \
                    or not emu_test.nonblank(png):
                print("  MISSING or blank: %s" % shot, file=sys.stderr)
                ok = False
            else:
                print("  %s" % os.path.relpath(png, ROOT))
        return ok


def make_intro(out):
    """docs/intro.gif: the shots playing in the hero picture's display."""
    from PIL import Image
    hero = Image.open(os.path.join(out, HERO)).convert("RGB")
    k = INTRO_W / hero.width
    hero = hero.resize((INTRO_W, round(hero.height * k)), Image.LANCZOS)
    x, y, size = (round(v * k) for v in HERO_LCD)
    hero.paste((5, 5, 7), (x - 2, y - 2, x + size + 2, y + size + 2))
    screens, times = [], []
    for shot, secs in INTRO:
        path = os.path.join(out, "shot-%s.png" % shot)
        if not os.path.isfile(path):
            return None
        screens.append(Image.open(path).convert("RGB").resize(
            (size, size), Image.NEAREST))
        times.append(int(secs * 1000))
    # one palette for the photo and every screen, so no frame shifts colour
    sheet = Image.new("RGB", (hero.width, hero.height + size * 3))
    sheet.paste(hero, (0, 0))
    for i, scr in enumerate(screens):
        sheet.paste(scr, (i % 6 * size, hero.height + i // 6 * size))
    pal = sheet.quantize(colors=255, method=Image.MEDIANCUT,
                         dither=Image.NONE)
    base = hero.quantize(palette=pal, dither=Image.FLOYDSTEINBERG)
    frames = []
    for scr in screens:                 # the screen itself: no dither, crisp
        f = base.copy()
        f.paste(scr.quantize(palette=pal, dither=Image.NONE), (x, y))
        frames.append(f)
    path = os.path.join(out, "intro.gif")
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=times, loop=0, optimize=False)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scenes", nargs="*", help="default: all (%s)"
                    % " ".join(SCENES))
    ap.add_argument("--fwsc", default=emu_test.newest_fwsc())
    ap.add_argument("--out", default=os.path.join(ROOT, "docs"))
    ap.add_argument("--emu", default=None)
    ap.add_argument("--intro-only", action="store_true",
                    help="rebuild intro.gif from the shots already in --out")
    args = ap.parse_args()
    if args.intro_only:
        gif = make_intro(args.out)
        print("animation: %s" % (os.path.relpath(gif, ROOT) if gif
                                 else "skipped (a shot is missing)"))
        return 0 if gif else 1
    emu = args.emu or emu_test.find_emu()
    if not emu:
        print("fm1-emu not found: run tools/get_emu.sh", file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)
    ok = True
    for name in args.scenes or list(SCENES):
        if name not in SCENES:
            print("no such scene: %s" % name, file=sys.stderr)
            return 2
        print("scene %s (%s)" % (name, os.path.basename(args.fwsc)))
        ok = run_scene(emu, args.fwsc, name, args.out) and ok
    if not args.scenes:
        gif = make_intro(args.out)
        print("animation: %s" % (os.path.relpath(gif, ROOT) if gif
                                 else "skipped (a shot is missing)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
