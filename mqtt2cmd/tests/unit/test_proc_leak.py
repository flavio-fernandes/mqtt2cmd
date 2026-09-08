"""Regression tests for the atexit handler leak in proc.Group.

Group._run_impl() registers an atexit handler per spawned process. The handler
closes over the Popen handle, so leaving it registered forever retains every
process the group has ever run. Upstream report:
https://github.com/mortoray/shelljob/issues/14

These tests deliberately avoid atexit._ncallbacks(): it is a private CPython
counter that does not decrease on unregister before 3.14, which would make
these tests fail on the very interpreters the service runs on. Instead they
assert the properties that actually matter -- the handle becomes collectable
once the child exits, and a child that outlives its output is still terminated
at interpreter exit.
"""
import gc
import os
import signal
import subprocess
import sys
import textwrap
import time
import weakref

from mqtt2cmd import proc

COMMANDS = 8
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


def _drain(group):
    deadline = time.time() + 10
    while group.count_running() and time.time() < deadline:
        group.readlines(timeout=0.05)
    group.readlines(timeout=0.05)


def test_finished_processes_become_collectable():
    """Each finished child must be releasable, not retained for the process life."""
    refs = []

    for _ in range(COMMANDS):
        group = proc.Group()
        handle = group.run(["/bin/echo", "regression"])
        refs.append(weakref.ref(handle))
        _drain(group)
        group.clear_finished()
        del handle
        del group

    # block_read finishes on a daemon thread; give it a moment to release.
    deadline = time.time() + 10
    while time.time() < deadline:
        gc.collect()
        if all(r() is None for r in refs):
            break
        time.sleep(0.05)
    gc.collect()

    leaked = sum(1 for r in refs if r() is not None)
    assert leaked == 0, (
        "{} of {} finished Popen handles were still reachable; the atexit "
        "handler retains them for the life of the process".format(leaked, COMMANDS)
    )


def test_child_outliving_its_output_is_still_killed_at_exit():
    """EOF on the pipe does not mean the child exited.

    A command that closes both stdout and stderr while continuing to run hits
    EOF immediately. If the atexit handler is released at EOF rather than at
    process exit, an interpreter exit leaves the child orphaned. This runs a
    real interpreter to completion and checks the grandchild was terminated.
    """
    program = textwrap.dedent(
        """
        import sys, time
        sys.path.insert(0, {root!r})
        from mqtt2cmd import proc

        group = proc.Group()
        # `exec sleep` replaces the shell, so handle.pid IS the long-lived
        # process. Without it, handle.pid is a short-lived sh wrapper and
        # terminate() would never have covered the surviving grandchild.
        handle = group.run(
            ["sh", "-c", "exec 1>/dev/null 2>/dev/null; exec sleep 30"]
        )
        # wait for block_read to reach EOF, so the handler would be released
        # by any implementation that unregisters there
        deadline = time.time() + 10
        while group.waiting > 0 and time.time() < deadline:
            group.readlines(timeout=0.05)
        # Group.__del__ -> close() terminates every tracked handle, which
        # would kill the child regardless of atexit and mask what is being
        # tested. Drop the group's own tracking so ONLY premature_exit can
        # terminate it.
        group.handles.clear()
        print(handle.pid, flush=True)
        # normal interpreter exit -> atexit handlers run
        """
    ).format(root=REPO_ROOT)

    out = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, timeout=60,
    )
    assert out.stdout.strip(), "helper produced no pid (stderr: {})".format(out.stderr)
    pid = int(out.stdout.strip().splitlines()[-1])

    # The helper has exited, so its atexit handlers have run. The grandchild
    # should have been terminated rather than left behind.
    time.sleep(0.5)
    alive = True
    try:
        os.kill(pid, 0)
    except OSError:
        alive = False

    if alive:                      # don't leak a stray sleep(30) on failure
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    assert not alive, (
        "child pid {} survived interpreter exit; the atexit handler was "
        "released at output EOF instead of at process exit".format(pid)
    )
