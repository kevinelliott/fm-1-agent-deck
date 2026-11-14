#!/usr/bin/env python3
"""Unit tests for the Agent Deck daemon (host/agtdeck.py).

No FM-1, no Herdr, no tmux: a fake backend stands in for the multiplexer and
a recording link for the device, and the daemon is driven the way the panel
drives it (`btn REC`, `key 3`, ...). Standard library only.

  python3 host/test_daemon.py            # or: python3 -m unittest -v host/test_daemon.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agtdeck as ad                                      # noqa: E402


class FakeBackend:
    """A multiplexer with scripted agents; records what the daemon does."""
    name = "fake"

    def __init__(self, agents):
        self.agents = agents            # list of dicts, edited by the tests
        self.acts = []                  # (agent id, action, arg)
        self.keys = []
        self.screen = {}                # agent id -> lines

    def list_agents(self):
        return [dict(a) for a in self.agents]

    def read_lines(self, agent_id, n=200):
        return list(self.screen.get(agent_id, ["$ "]))

    def send_keys(self, agent_id, keys):
        self.keys.append((agent_id, list(keys)))

    def act(self, agent_id, action, arg=None, kind="unknown"):
        self.acts.append((agent_id, action, arg))

    def spawn(self, command, cwd, label, near=None):
        self.acts.append(("spawn", command, cwd))
        return "new"

    def focus(self, agent_id):
        self.acts.append((agent_id, "focus", None))

    def close(self, agent_id):
        self.acts.append((agent_id, "close", None))


class RecLink:
    """The device: remembers the frames it was sent."""

    def __init__(self):
        self.frames = []
        self.resets = 0

    def frame(self, ftype, payload):
        self.frames.append((ftype, bytes(payload)))

    def reset(self):
        self.resets += 1

    def of(self, ftype):
        return [p for t, p in self.frames if t == ftype]


def agent(aid, state="IDLE", kind="shell", name=None, cwd="/tmp"):
    return {"id": aid, "name": name or aid, "kind": kind, "state": state,
            "cwd": cwd}


def quiet(fn, *args, **kw):
    """Run fn with the daemon's log lines swallowed."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kw)


class DaemonCase(unittest.TestCase):
    def make(self, agents, **cfg):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name          # slots.json goes here
        self.addCleanup(lambda: os.environ.__setitem__("HOME", self.home))
        self.b = FakeBackend(agents)
        self.link = RecLink()
        config = ad.load_config(None)[0]
        config.update({"sounds": False, "notify": False})
        config.update(cfg)
        self.d = quiet(ad.Daemon, self.b, self.link, config, True)
        self.d.QUEUE_SETTLE = 0.0                   # no waiting in tests
        self.poll()
        return self.d

    def poll(self):
        quiet(self.d.poll_once)

    def cmd(self, line):
        return quiet(self.d.handle_command, line)

    def actions(self, action=None):
        return [(i, a, x) for i, a, x in self.b.acts
                if action is None or a == action]


class Roster(DaemonCase):
    def test_agents_get_slots_in_order(self):
        d = self.make([agent("a"), agent("b", "WORKING")])
        self.assertEqual(sorted(d.roster), [0, 1])
        self.assertEqual(d.roster[1]["state"], "WORKING")
        self.assertTrue(self.link.of(ad.FT_SLOT))

    def test_an_agent_keeps_its_slot_when_another_leaves(self):
        d = self.make([agent("a"), agent("b"), agent("c")])
        self.b.agents.pop(0)
        self.poll()
        self.assertEqual({s: a["id"] for s, a in d.roster.items()},
                         {1: "b", 2: "c"})

    def test_blocked_count_reaches_the_device(self):
        self.make([agent("a", "BLOCKED"), agent("b")])
        self.assertEqual(self.link.of(ad.FT_META)[-1][:2], bytes([2, 1]))


class Panel(DaemonCase):
    def test_rec_approves_the_selected_agent(self):
        self.make([agent("a", "BLOCKED")])
        self.cmd("btn REC")
        self.assertEqual(self.actions("approve"), [("a", "approve", None)])

    def test_fx_denies_and_env_interrupts(self):
        self.make([agent("a", "BLOCKED")])
        self.cmd("btn FX")
        self.cmd("btn ENV")
        self.assertEqual([a for _, a, _ in self.b.acts],
                         ["deny", "interrupt"])

    def test_a_key_selects_and_a_chord_acts_without_moving(self):
        d = self.make([agent("a"), agent("b", "BLOCKED")])
        self.cmd("key 0")
        self.assertEqual(d.selected, 0)
        self.cmd("btn REC down")
        self.cmd("key 1")
        self.cmd("btn REC up")
        self.assertEqual(self.actions("approve"), [("b", "approve", None)])
        self.assertEqual(d.selected, 0)

    def test_glo_rec_approves_every_blocked_agent(self):
        self.make([agent("a", "BLOCKED"), agent("b"), agent("c", "BLOCKED")])
        self.cmd("all approve")
        self.assertEqual(sorted(i for i, _, _ in self.actions("approve")),
                         ["a", "c"])

    def test_marks_narrow_a_fleet_action(self):
        self.make([agent("a", "BLOCKED"), agent("b", "BLOCKED")])
        self.cmd("mark 1")
        self.cmd("all approve")
        self.assertEqual(self.actions("approve"), [("b", "approve", None)])


class Prompts(DaemonCase):
    def test_seq_sends_the_armed_preset(self):
        d = self.make([agent("a")])
        self.cmd("preset 2")
        self.cmd("send")
        self.assertEqual(self.actions("prompt"),
                         [("a", "prompt", d.presets[1]["text"])])

    def test_a_prompt_for_a_blocked_agent_waits_in_the_queue(self):
        d = self.make([agent("a", "BLOCKED")], presets=["hello there"])
        self.cmd("send")                            # SEQ, as on the device
        self.assertEqual(self.actions("prompt"), [])
        self.assertEqual(len(d.queue["a"]["items"]), 1)
        self.b.agents[0]["state"] = "IDLE"          # answered: now it goes
        self.poll()
        self.assertEqual(self.actions("prompt"),
                         [("a", "prompt", "hello there")])
        self.assertNotIn("a", d.queue)

    def test_a_pipeline_sends_one_step_per_finished_turn(self):
        d = self.make([agent("a")], presets=[
            {"label": "pipe", "steps": ["one", "two", "three"]}])
        self.cmd("send")
        self.assertEqual([x for _, _, x in self.actions("prompt")], ["one"])
        self.b.agents[0]["state"] = "WORKING"
        self.poll()
        self.b.agents[0]["state"] = "DONE"
        self.poll()
        self.assertEqual([x for _, _, x in self.actions("prompt")],
                         ["one", "two"])
        self.b.agents[0]["state"] = "BLOCKED"       # held while it waits
        self.poll()
        self.assertEqual(len(self.actions("prompt")), 2)
        self.assertEqual(len(d.queue["a"]["items"]), 1)

    def test_a_moment_of_idle_between_tool_calls_sends_nothing(self):
        d = self.make([agent("a")], presets=[
            {"label": "pipe", "steps": ["one", "two"]}])
        d.QUEUE_SETTLE = 3.0
        self.cmd("send")
        for state in ("WORKING", "IDLE", "BLOCKED", "WORKING", "IDLE"):
            self.b.agents[0]["state"] = state      # idle only in passing
            self.poll()
        self.assertEqual([x for _, _, x in self.actions("prompt")], ["one"])
        d.queue["a"]["rest"] -= 3.0                 # ... and now for 3 s
        self.poll()
        self.assertEqual([x for _, _, x in self.actions("prompt")],
                         ["one", "two"])

    def test_claude_mid_turn_gets_nothing_even_if_it_looks_idle(self):
        d = self.make([agent("a", kind="claude")], presets=[
            {"label": "pipe", "steps": ["one", "two"]}])
        turn = {"turn": "open", "mtime": time.time()}
        orig = ad.claude_insight
        ad.claude_insight = lambda a, *x: dict(turn)
        self.addCleanup(lambda: setattr(ad, "claude_insight", orig))
        self.cmd("send")
        d.queue["a"]["t"] -= 60                     # long past the 15 s
        self.poll()
        self.assertEqual([x for _, _, x in self.actions("prompt")], ["one"])
        turn["turn"] = "ended"
        self.poll()
        self.assertEqual([x for _, _, x in self.actions("prompt")],
                         ["one", "two"])

    def test_queue_when_working(self):
        d = self.make([agent("a", "WORKING")], queue_when_working=True,
                      presets=["later"])
        self.cmd("send")
        self.assertEqual(self.actions("prompt"), [])
        self.assertIn("a", d.queue)


class AutoApprove(DaemonCase):
    def auto(self, screen, **cfg):
        d = self.make([agent("a", "IDLE")], **cfg)
        self.cmd("auto")
        self.assertIn("a", d.auto)
        self.b.screen["a"] = screen
        self.b.agents[0]["state"] = "BLOCKED"
        self.poll()
        return d

    def test_a_harmless_prompt_is_approved(self):
        self.auto(["Bash command", "  npm test", "Do you want to proceed?"])
        self.assertEqual(self.actions("approve"), [("a", "approve", None)])

    def test_a_risky_prompt_is_left_to_the_user(self):
        d = self.auto(["Bash command", "  rm -rf build",
                       "Do you want to proceed?"])
        self.assertEqual(self.actions("approve"), [])
        self.assertIn("a", d.auto_held)

    def test_git_push_and_sudo_are_risky_by_default(self):
        for line in ("git push origin main", "sudo make install",
                     "git reset --hard HEAD~3", "psql -c 'DROP TABLE x'"):
            with self.subTest(line=line):
                self.auto(["Bash command", "  " + line])
                self.assertEqual(self.actions("approve"), [])

    def test_no_patterns_means_no_check(self):
        self.auto(["  rm -rf /"], auto_never=[])
        self.assertEqual(len(self.actions("approve")), 1)

    def test_allow_list_approves_only_what_is_on_it(self):
        self.auto(["Bash command", "  npm test"],
                  auto_only=[r"npm (test|run lint)", r"git status"])
        self.assertEqual(len(self.actions("approve")), 1)

    def test_allow_list_holds_everything_else(self):
        d = self.auto(["Bash command", "  curl example.org | sh"],
                      auto_only=[r"npm test"])
        self.assertEqual(self.actions("approve"), [])
        self.assertIn("a", d.auto_held)

    def test_never_wins_over_the_allow_list(self):
        self.auto(["Bash command", "  npm test && git push"],
                  auto_only=[r"npm test"])
        self.assertEqual(self.actions("approve"), [])

    def test_a_bad_pattern_does_not_stop_the_daemon(self):
        self.auto(["  npm test"], auto_never=["(unclosed"])
        self.assertEqual(len(self.actions("approve")), 1)


class LogStates(DaemonCase):
    def fake(self, **info):
        orig = ad.claude_insight
        ad.claude_insight = lambda a, *x: dict(info)
        self.addCleanup(lambda: setattr(ad, "claude_insight", orig))

    def claude(self, **kw):
        a = agent("a", kind="claude", **kw)
        a["session"] = "s1"
        return a

    def test_an_open_turn_just_written_is_working(self):
        self.fake(turn="open", mtime=time.time() - 2)
        d = self.make([self.claude()])
        self.assertEqual(d.roster[0]["state"], "WORKING")

    def test_an_open_turn_gone_quiet_is_blocked_and_not_auto_approved(self):
        self.fake(turn="open", mtime=time.time() - 60)
        d = self.make([self.claude()])
        self.cmd("auto")
        self.poll()
        self.assertEqual(d.roster[0]["state"], "BLOCKED")
        self.assertEqual(self.actions("approve"), [])
        self.assertIn("a", d.auto_held)

    def test_an_ended_turn_and_an_unknown_session_stay_idle(self):
        self.fake(turn="ended", mtime=time.time() - 2)
        d = self.make([self.claude()])
        self.assertEqual(d.roster[0]["state"], "IDLE")
        self.fake(turn="open", mtime=time.time() - 2)
        d = self.make([agent("b", kind="claude")])
        self.assertEqual(d.roster[0]["state"], "IDLE")


class Menu(DaemonCase):
    def test_the_menu_lists_recent_prompts_for_claude(self):
        d = self.make([agent("a", kind="claude", cwd=self.id())])
        ad._insight_cache.clear()
        orig = ad.claude_insight
        ad.claude_insight = lambda a, *x: {"prompt": "fix it",
                                           "prompts": ["one", "fix it"]}
        self.addCleanup(lambda: setattr(ad, "claude_insight", orig))
        title, items = d._m_prompts()
        labels = [t for t, _ in items]
        self.assertIn("RECENT PROMPTS", labels)
        title, items = d._m_recent()
        self.assertEqual([t for t, _ in items], ["fix it", "one"])
        quiet(items[1][1])
        self.assertEqual(self.actions("prompt"), [("a", "prompt", "one")])

    def test_about_says_when_the_firmware_is_older(self):
        d = self.make([agent("a")])
        self.assertNotIn("UPDATE", " ".join(d.about_lines()))
        d.dev_proto = ad.PROTO_VERSION - 1
        self.assertIn("UPDATE FIRMWARE", " ".join(d.about_lines()))


class Link(DaemonCase):
    def test_full_resync_sends_everything_again(self):
        d = self.make([agent("a"), agent("b")])
        self.link.frames.clear()
        self.poll()
        self.assertEqual(self.link.of(ad.FT_SLOT), [])     # nothing changed
        quiet(d.full_resync)
        self.poll()
        self.assertEqual(len(self.link.of(ad.FT_SLOT)), 2)
        self.assertEqual(self.link.of(ad.FT_HELLO_ACK),
                         [bytes([ad.PROTO_VERSION])])

    def test_the_link_is_swapped_out_and_back(self):
        d = self.make([agent("a")])

        class Fake(ad.MidiLink):
            def __init__(self):
                self.up = True

            def alive(self):
                return self.up

            def close(self):
                self.closed = True

            def frame(self, ftype, payload):
                pass

            def reset(self):
                pass
        d.link = Fake()
        self.assertIs(quiet(ad.watch_link, d, None, None), d.link)
        d.link.up = False
        gone = quiet(ad.watch_link, d, None, None)
        self.assertIsInstance(gone, ad.NullLink)


def log_line(**kw):
    return json.dumps(kw) + "\n"


class Insight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orig = ad.CLAUDE_PROJECTS
        ad.CLAUDE_PROJECTS = self.tmp.name
        self.addCleanup(lambda: setattr(ad, "CLAUDE_PROJECTS", self.orig))
        ad._insight_cache.clear()
        ad._session_paths.clear()

    def write(self, lines, cwd="/work/proj"):
        d = os.path.join(self.tmp.name, "-work-proj")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "s1.jsonl"), "w") as f:
            f.writelines(lines)
        return {"kind": "claude", "cwd": cwd}

    def assistant(self, mid, content, out=0, stop=None, ctx=1000):
        return log_line(type="assistant", message={
            "id": mid, "model": "claude-fable-5-2", "content": content,
            "stop_reason": stop,
            "usage": {"input_tokens": ctx, "output_tokens": out}})

    def test_prompts_tokens_and_an_ended_turn(self):
        a = self.write([
            log_line(type="user", message={"content": "run the tests"}),
            self.assistant("m1", [{"type": "text", "text": "On it."}], 40),
            self.assistant("m1", [{"type": "tool_use", "id": "t1",
                                   "name": "Bash",
                                   "input": {"command": "npm test"}}], 40,
                           "tool_use"),
            log_line(type="user", message={"content": [
                {"type": "tool_result", "tool_use_id": "t1"}]}),
            self.assistant("m2", [{"type": "text", "text": "All green."}],
                           25, "end_turn", ctx=5000),
        ])
        i = ad.claude_insight(a)
        self.assertEqual(i["prompts"], ["run the tests"])
        self.assertEqual(i["out"], 65)          # m1 counted once, plus m2
        self.assertEqual(i["ctx"], 5000)
        self.assertEqual(i["turn"], "ended")
        self.assertEqual(i["said"], "All green.")
        self.assertNotIn("pending", i)

    def test_an_unanswered_tool_call_is_pending_and_the_turn_open(self):
        a = self.write([
            log_line(type="user", message={"content": "deploy"}),
            self.assistant("m1", [{"type": "tool_use", "id": "t1",
                                   "name": "Bash",
                                   "input": {"command": "git push"}}], 9,
                           "tool_use"),
        ])
        i = ad.claude_insight(a)
        self.assertEqual(i["pending"], "Bash git push")
        self.assertEqual(i["turn"], "open")

    def test_an_interrupted_turn_is_over(self):
        a = self.write([
            log_line(type="user", message={"content": "deploy"}),
            self.assistant("m1", [{"type": "tool_use", "id": "t1",
                                   "name": "Bash",
                                   "input": {"command": "git push"}}], 9,
                           "tool_use"),
            log_line(type="user", message={"content": [{
                "type": "text",
                "text": "[Request interrupted by user for tool use]"}]}),
        ])
        i = ad.claude_insight(a)
        self.assertEqual(i["turn"], "ended")
        self.assertNotIn("pending", i)
        self.assertEqual(i["prompts"], ["deploy"])

    def test_meta_and_tagged_user_lines_are_not_prompts(self):
        a = self.write([
            log_line(type="user", isMeta=True, message={"content": "x"}),
            log_line(type="user", message={
                "content": "<command-name>/clear</command-name>"}),
            log_line(type="user", message={"content": "first"}),
            log_line(type="user", message={"content": "second"}),
            log_line(type="user", message={"content": "first"}),
        ])
        self.assertEqual(ad.claude_insight(a)["prompts"], ["second", "first"])

    def test_other_kinds_are_left_alone(self):
        self.assertEqual(ad.claude_insight({"kind": "codex", "cwd": "/x"}),
                         {})


class Helpers(unittest.TestCase):
    def test_numbered_options_are_read_from_the_screen(self):
        opts = ad.parse_options(["Do you want to proceed?", "> 1. Yes",
                                 "  2. Yes, and don't ask again",
                                 "  3. No"])
        self.assertEqual([n for n, _ in opts], [1, 2, 3])

    def test_presets_take_strings_texts_and_pipelines(self):
        p = ad.normalise_presets(["go", {"label": "t", "text": "test"},
                                  {"label": "p", "steps": ["a", "b"]}, 7])
        self.assertEqual([x["label"] for x in p], ["go", "t", "p"])
        self.assertEqual(p[2]["steps"], ["a", "b"])

    def test_an_unknown_config_key_is_ignored(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            json.dump({"repeat_max": 3, "no_such_key": 1}, f)
            f.flush()
            cfg, path = quiet(ad.load_config, f.name)
        self.assertEqual(cfg["repeat_max"], 3)
        self.assertNotIn("no_such_key", cfg)

    def test_the_service_definition_starts_this_daemon(self):
        text = ad.service_text(["--backend", "herdr"])
        if text is None:
            self.skipTest("no service support on this platform")
        self.assertIn(os.path.abspath(ad.__file__), text)
        self.assertIn("herdr", text)

    def test_ages_and_token_counts_are_short(self):
        self.assertEqual(ad.fmt_tokens(182400), "182k")
        self.assertTrue(ad.fmt_age(75))


class Editor(unittest.TestCase):
    """docs/editor/ carries a copy of the defaults: it must not go stale."""

    def test_the_editor_defaults_match_the_daemon(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "docs", "editor", "defaults.json")
        with open(path) as f:
            page = json.load(f)
        want = json.loads(json.dumps(ad.DEFAULT_CONFIG))
        for cfg in (page, want):
            cfg.pop("open_cmd", None)           # differs by platform
        self.assertEqual(page, want, "run: python3 host/agtdeck.py "
                         "--print-config > docs/editor/defaults.json")


class Doctor(unittest.TestCase):
    def test_the_check_up_runs_and_reports(self):
        out = io.StringIO()
        args = ad.types.SimpleNamespace(
            port="no-such-port", config=None, backend="tmux",
            herdr_socket="/nonexistent/herdr.sock", herdr_session=None)
        with contextlib.redirect_stdout(out):
            rc = ad.doctor(args)
        self.assertIn(rc, (0, 1))
        self.assertIn("Agent Deck %s" % ad.VERSION, out.getvalue())
        self.assertIn("config", out.getvalue())


if __name__ == "__main__":
    t0 = time.time()
    unittest.main(verbosity=1)
