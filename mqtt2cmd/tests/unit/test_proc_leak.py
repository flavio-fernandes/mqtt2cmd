"""Regression tests for the shutdown-handler leak in proc.Group (issue #14).

Group._run_impl used to register an atexit handler per spawned process. The
handler closed over the Popen handle, so the registry retained every process
the group had ever run. Unregistering per child is not enough either: on
CPython 3.10-3.13 atexit.unregister leaves an inactive registry slot behind,
so the churn grows without bound. One stable callback tracking only live
children avoids both.

These tests avoid atexit._ncallbacks(): it is a private counter that does not
decrease on unregister before 3.14. They assert the properties that matter --
a finished child becomes collectable, runs do not churn the registry, and a
child that outlives its output is still terminated at interpreter exit.
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
SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(proc.__file__))))


def _drain(group):
    deadline = time.time() + 10
    while group.count_running() and time.time() < deadline:
        group.readlines(timeout=0.05)
    group.readlines(timeout=0.05)


def _run_helper(program):
    """Run an isolated interpreter and capture its text output."""
    return subprocess.run(
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=60,
    )


def test_finished_processes_become_collectable():
    """A finished child must be releasable, not retained for the process life."""
    refs = []

    for _ in range(COMMANDS):
        group = proc.Group()
        handle = group.run(["/bin/echo", "regression"])
        refs.append(weakref.ref(handle))
        _drain(group)
        group.clear_finished()
        del handle
        del group

    deadline = time.time() + 10
    while time.time() < deadline:
        gc.collect()
        if all(r() is None for r in refs):
            break
        time.sleep(0.05)
    gc.collect()

    leaked = sum(1 for r in refs if r() is not None)
    assert leaked == 0, (
        "{} of {} finished handles still reachable; the shutdown handler "
        "retains them for the life of the process".format(leaked, COMMANDS)
    )


def test_runs_do_not_churn_the_atexit_registry(monkeypatch):
    """One module-level callback must serve every invocation.

    atexit.unregister does not reclaim its registry slot before CPython 3.14,
    so registering per child leaks a slot per command even when unregistered.
    """
    registrations = []
    unregistrations = []
    monkeypatch.setattr(proc.atexit, "register", lambda cb: registrations.append(cb))
    monkeypatch.setattr(
        proc.atexit, "unregister", lambda cb: unregistrations.append(cb)
    )

    group = proc.Group()
    for _ in range(COMMANDS):
        group.run(["/bin/echo", "regression"])
    _drain(group)

    assert registrations == []
    assert unregistrations == []


def test_child_outliving_its_output_is_still_killed_at_exit():
    """EOF on the pipe does not mean the child exited.

    A command that closes both stdout and stderr while still running hits EOF
    immediately. If tracking is released there rather than at process exit, an
    interpreter exit leaves the child orphaned. This runs a real interpreter to
    completion and checks the child was terminated.
    """
    program = textwrap.dedent("""
        import sys, time
        sys.path.insert( 0, {src!r} )
        from mqtt2cmd import proc

        group = proc.Group()
        # `exec sleep` replaces the shell, so handle.pid IS the long-lived
        # process rather than a short-lived wrapper.
        handle = group.run( ['sh', '-c', 'exec 1>/dev/null 2>/dev/null; exec sleep 30'] )
        deadline = time.time() + 10
        while group.waiting > 0 and time.time() < deadline:
            group.readlines( timeout = 0.05 )
        print( handle.pid, flush = True )
        """).format(src=SRC)

    out = _run_helper(program)
    assert out.stdout.strip(), "helper produced no pid (stderr: {})".format(out.stderr)
    pid = int(out.stdout.strip().splitlines()[-1])

    time.sleep(0.5)
    alive = True
    try:
        os.kill(pid, 0)
    except OSError:
        alive = False

    if alive:  # don't leave a stray sleep behind on failure
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    assert not alive, (
        "child pid {} survived interpreter exit; shutdown tracking was "
        "released at output EOF instead of at process exit".format(pid)
    )


def test_forked_child_does_not_kill_the_parents_processes():
    """A forked worker must not inherit responsibility for the parent's children.

    fork() clones only the calling thread, so a child inherits locks held by
    threads that do not exist in it -- including each handle's internal
    Popen._waitpid_lock, held by its reader thread for the life of the process.
    poll() then cannot read the real status, so terminate() signals a process
    the child does not own. The after-fork hook clears the inherited state.
    """
    program = textwrap.dedent("""
        import os, sys, time
        sys.path.insert( 0, {src!r} )
        from mqtt2cmd import proc

        group = proc.Group()
        handle = group.run( ['sh', '-c', 'exec 1>/dev/null 2>/dev/null; exec sleep 20'] )
        time.sleep( 1 )
        target = handle.pid
        sys.stdout.flush()

        pid = os.fork()
        if pid == 0:
            sys.exit( 0 ) # normal exit -> atexit runs in the forked child
        os.waitpid( pid, 0 )
        time.sleep( 1 )
        try:
            os.kill( target, 0 )
            alive = True
        except OSError:
            alive = False
        print( '{{}} {{}}'.format( target, alive ), flush = True )
        try:
            handle.kill()
        except Exception:
            pass
        """).format(src=SRC)

    out = _run_helper(program)
    assert out.stdout.strip(), "helper produced no output (stderr: {})".format(
        out.stderr
    )
    target, alive = out.stdout.strip().splitlines()[-1].split()

    assert alive == "True", (
        "forked child's exit killed the parent's process {}; inherited "
        "handles must be dropped after fork".format(target)
    )


def test_handle_released_even_if_stream_close_raises():
    """Cleanup must not be skippable.

    handle.stdin.close() can raise BrokenPipeError when the child closed its
    input, and a caller's on_error can raise too. If either escapes before the
    handle is discarded, it stays referenced for the life of the interpreter --
    the leak this change exists to remove.
    """
    group = proc.Group()
    handle = group.run(["/bin/echo", "regression"])

    original = handle.stdin.close

    def exploding_close():
        original()
        raise BrokenPipeError("simulated")

    handle.stdin.close = exploding_close

    ref = weakref.ref(handle)
    _drain(group)
    group.clear_finished()
    del handle
    del group

    deadline = time.time() + 10
    while time.time() < deadline:
        gc.collect()
        if ref() is None:
            break
        time.sleep(0.05)
    gc.collect()

    assert (
        ref() is None
    ), "handle still reachable after close() raised; cleanup was skipped"


def test_pid_guard_makes_inherited_shutdown_state_inert(monkeypatch):
    """The shutdown callback must ignore state owned by another process."""
    monkeypatch.setattr(
        proc, "_owner_pid", proc.os.getpid() + 1
    )  # pretend we are a fork
    sentinel = []

    class FakeHandle:
        def terminate(self):
            sentinel.append("terminated")

    fake = FakeHandle()
    proc._active_handles.add(fake)
    try:
        proc._terminate_active_handles()
    finally:
        proc._active_handles.discard(fake)

    assert (
        sentinel == []
    ), "atexit handler terminated an inherited handle despite a pid mismatch"


def test_import_without_register_at_fork():
    """Non-forking platforms such as Windows must still import the module."""
    program = textwrap.dedent("""
        import os, sys
        sys.path.insert( 0, {src!r} )
        if hasattr( os, 'register_at_fork' ):
            del os.register_at_fork
        from mqtt2cmd import proc
        print( proc.Group.__name__ )
        """).format(src=SRC)

    out = _run_helper(program)
    assert (
        out.returncode == 0
    ), "module import failed without register_at_fork (stderr: {})".format(out.stderr)
    assert out.stdout.strip() == "Group"


def test_forked_child_rearms_shutdown_tracking():
    """The at-fork hook must reset the lock and track worker-owned commands."""
    program = textwrap.dedent("""
        import os, sys
        sys.path.insert( 0, {src!r} )
        from mqtt2cmd import proc

        proc._active_handles_lock.acquire()
        worker = os.fork()
        if worker == 0:
            if not proc._active_handles_lock.acquire( False ):
                raise RuntimeError( 'at-fork hook did not replace the inherited lock' )
            proc._active_handles_lock.release()

            group = proc.Group()
            handle = group.run(
                ['sh', '-c', 'exec 1>/dev/null 2>/dev/null; exec sleep 30'] )
            print( handle.pid, flush = True )
            sys.exit( 0 ) # normal exit -> the module-level atexit callback runs

        proc._active_handles_lock.release()
        os.waitpid( worker, 0 )
        """).format(src=SRC)

    out = _run_helper(program)
    assert out.returncode == 0, "fork helper failed (stderr: {})".format(out.stderr)
    assert (
        out.stdout.strip()
    ), "forked worker produced no child pid (stderr: {})".format(out.stderr)
    pid = int(out.stdout.strip().splitlines()[-1])

    time.sleep(0.5)
    alive = True
    try:
        os.kill(pid, 0)
    except OSError:
        alive = False

    if alive:  # don't leave a stray sleep behind on failure
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    assert not alive, (
        "forked worker's child pid {} survived interpreter exit; the at-fork "
        "hook did not re-arm shutdown tracking".format(pid)
    )
