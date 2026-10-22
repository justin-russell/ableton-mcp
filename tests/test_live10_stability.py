"""Regression tests for the Live-freeze fixes.

Each test pins a cause of Ableton Live hanging while driven over MCP:

* commands touched Live's API from socket client threads instead of Live's
  main thread, and handed work over with ControlSurface.schedule_message,
  which is not safe to call from another thread,
* a task that timed out on the client side still ran later on Live,
* browser URI lookups ignored Live's real URI prefixes (query:AudioFx#...),
  fell back to walking every root - Sounds, Samples, User Library - on the main
  thread, and had no bound on how long that could take,
* passive dataset listeners were registered in every session and rebuilt
  inside Live's own tracks notification,
* the file failed to load at all on Live 10 (Python 2.7) without a coding line.
"""

import threading
import time

import pytest

from test_remote_script_bridge import (
    BUNDLED_SCRIPT,
    REPO_SCRIPT,
    _load_script_module,
    make_instance,
)


@pytest.fixture
def script_module():
    return _load_script_module()


# ------------------------------------------------------------ main thread

def _pump_until(inst, done, timeout=5.0):
    """Act as Live's main thread: drain the queue until ``done`` is set."""
    deadline = time.time() + timeout
    while not done.is_set() and time.time() < deadline:
        inst._drain_main_queue()
        time.sleep(0.005)


def test_client_thread_work_runs_on_main_thread(script_module):
    inst = make_instance(script_module)
    ran_on = []
    done = threading.Event()
    result = {}

    def client():
        result["value"] = inst._run_on_main_thread(
            lambda: ran_on.append(threading.current_thread()) or 42, timeout=5.0)
        done.set()

    worker = threading.Thread(target=client)
    worker.start()
    _pump_until(inst, done)
    worker.join(1.0)

    assert result["value"] == 42
    assert ran_on == [inst._main_thread]


def test_errors_on_main_thread_are_raised_to_the_client(script_module):
    inst = make_instance(script_module)
    done = threading.Event()
    caught = {}

    def boom():
        raise ValueError("nope")

    def client():
        try:
            inst._run_on_main_thread(boom, timeout=5.0)
        except ValueError as e:
            caught["error"] = str(e)
        done.set()

    worker = threading.Thread(target=client)
    worker.start()
    _pump_until(inst, done)
    worker.join(1.0)
    assert caught == {"error": "nope"}


def test_timed_out_task_never_runs_later(script_module):
    """A client that already reported a timeout must not have its command
    fire seconds later (e.g. a track appearing out of nowhere)."""
    inst = make_instance(script_module)
    ran = []
    errors = []

    def client():
        try:
            inst._run_on_main_thread(lambda: ran.append(1), timeout=0.05)
        except RuntimeError as e:
            errors.append(str(e))

    worker = threading.Thread(target=client)
    worker.start()
    worker.join(2.0)          # main thread is "busy": nothing drains the queue
    inst._drain_main_queue()  # ...then Live catches up

    assert errors and "Timed out" in errors[0]
    assert ran == []


def test_handle_client_never_calls_lom_off_main_thread(script_module):
    """_process_command must only ever execute on the main thread."""
    inst = make_instance(script_module)
    seen = []

    def process(command):
        seen.append(threading.current_thread())
        return {"status": "success", "result": {}}

    inst._process_command = process
    inst.running = True

    class Client(object):
        def __init__(self):
            self.chunks = [b'{"type": "get_session_info", "params": {}}']
            self.sent = []

        def settimeout(self, _):
            pass

        def recv(self, _):
            return self.chunks.pop(0) if self.chunks else b""

        def sendall(self, data):
            self.sent.append(data)

        def close(self):
            pass

    client = Client()
    done = threading.Event()

    def serve():
        inst._handle_client(client)
        done.set()

    worker = threading.Thread(target=serve)
    worker.start()
    _pump_until(inst, done)
    worker.join(1.0)

    assert seen == [inst._main_thread]
    assert len(client.sent) == 1


def test_main_thread_commands_do_not_reschedule(script_module):
    """Inside _process_command we are already on the main thread; scheduling
    again and waiting would make the main thread wait on itself."""
    inst = make_instance(script_module)

    def no_schedule(*a, **k):
        raise AssertionError("schedule_message must not be used")

    inst.schedule_message = no_schedule
    inst._set_device_parameter = lambda *a: {"ok": True}
    inst._set_tempo = lambda tempo: {"tempo": tempo}

    r1 = inst._process_command({"type": "set_device_parameter", "params": {}})
    r2 = inst._process_command({"type": "set_tempo", "params": {"tempo": 128.0}})

    assert r1 == {"status": "success", "result": {"ok": True}}
    assert r2 == {"status": "success", "result": {"tempo": 128.0}}


# ---------------------------------------------------------------- browser

ROOT_NAMES = ["instruments", "sounds", "drums", "audio_effects", "midi_effects",
              "plugins", "max_for_live", "user_library", "packs", "samples"]


@pytest.mark.parametrize("uri, expected_first", [
    ("query:AudioFx#EQ%20Eight", "audio_effects"),
    ("query:Synths#Simpler", "instruments"),
    ("query:Drums#FileId_2739", "drums"),
    ("query:Plugins#VST3:Xfer%20Records:Serum%202", "plugins"),
    ("query:MidiFx#Arpeggiator", "midi_effects"),
])
def test_live_uri_prefixes_pick_the_right_root(script_module, uri, expected_first):
    inst = make_instance(script_module)
    roots = [(name, object()) for name in ROOT_NAMES]
    order = [name for name, _ in inst._order_roots_by_uri(roots, uri)]

    assert order[0] == expected_first
    # Heavy roots are never walked speculatively.
    for heavy in ("sounds", "samples", "user_library", "packs", "max_for_live"):
        assert heavy not in order


def test_heavy_root_is_walked_when_the_uri_names_it(script_module):
    inst = make_instance(script_module)
    roots = [(name, object()) for name in ROOT_NAMES]
    order = [name for name, _ in inst._order_roots_by_uri(roots, "query:Sounds#Bass")]
    assert order[0] == "sounds"


class _Node(object):
    def __init__(self, uri, children=()):
        self.uri = uri
        self.children = list(children)


def test_browser_walk_gives_up_instead_of_freezing(script_module, monkeypatch):
    monkeypatch.setattr(script_module, "BROWSER_WALK_MAX_NODES", 50)
    inst = make_instance(script_module)
    # A wide tree that does not contain the URI.
    big = _Node("root", [_Node("a%d" % i, [_Node("b%d_%d" % (i, j)) for j in range(20)])
                         for i in range(20)])

    started = time.time()
    with pytest.raises(RuntimeError) as err:
        inst._find_browser_item_by_uri(big, "query:AudioFx#Missing")
    assert "Gave up" in str(err.value)
    assert time.time() - started < 1.0


def test_browser_walk_still_finds_items(script_module):
    inst = make_instance(script_module)
    target = _Node("query:AudioFx#EQ%20Eight")
    tree = _Node("root", [_Node("x"), _Node("y", [target])])
    assert inst._find_browser_item_by_uri(tree, "query:AudioFx#EQ%20Eight") is target


# ----------------------------------------------------------------- passive

def test_passive_capture_is_off_until_first_drain(script_module):
    inst = make_instance(script_module)
    inst._passive_events = []
    inst._passive_lock = threading.Lock()
    calls = []
    inst._setup_passive_listeners = lambda: calls.append(1)

    assert inst._passive_enabled is False
    inst._drain_passive_events()
    inst._drain_passive_events()
    assert calls == [1]
    assert inst._passive_enabled is True


def test_track_rebind_is_deferred_to_update_display(script_module):
    inst = make_instance(script_module)
    calls = []
    inst._rebind_track_listeners = lambda: calls.append(1)

    inst._passive_rebind_pending = True
    assert calls == []           # nothing happens inside the notification
    inst._drain_main_queue()     # next update_display tick
    assert calls == [1]
    assert inst._passive_rebind_pending is False


# ------------------------------------------------------------------ Live 10

@pytest.mark.parametrize("path", [REPO_SCRIPT, BUNDLED_SCRIPT])
def test_script_declares_utf8_for_python2(path):
    """Live 10 runs Remote Scripts on Python 2.7, which rejects non-ASCII
    source without a PEP 263 coding line in the first two lines."""
    first_two = path.read_bytes().splitlines()[:2]
    assert any(b"coding: utf-8" in line for line in first_two)


def test_both_script_copies_are_identical():
    assert REPO_SCRIPT.read_bytes() == BUNDLED_SCRIPT.read_bytes()
