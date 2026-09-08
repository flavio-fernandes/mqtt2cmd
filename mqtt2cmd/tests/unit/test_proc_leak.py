"""Regression test for the atexit handler leak in proc.Group.

Group._run_impl() registers an atexit handler per spawned process. The handler
closes over the Popen handle, so leaving it registered retains every process
the group has ever run, for the life of the interpreter. Upstream report:
https://github.com/mortoray/shelljob/issues/14
"""
import atexit
import gc
import time

from mqtt2cmd import proc

COMMANDS = 8


def _drain(group):
    deadline = time.time() + 10
    while group.count_running() and time.time() < deadline:
        group.readlines(timeout=0.05)
    group.readlines(timeout=0.05)


def test_atexit_handlers_are_released_after_processes_finish():
    before = atexit._ncallbacks()

    for _ in range(COMMANDS):
        group = proc.Group()
        group.run(["/bin/echo", "regression"])
        _drain(group)
        group.clear_finished()
        del group

    # block_read runs on a daemon thread; give it a moment to unregister.
    deadline = time.time() + 5
    while atexit._ncallbacks() > before and time.time() < deadline:
        time.sleep(0.05)
    gc.collect()

    leaked = atexit._ncallbacks() - before
    assert leaked == 0, (
        "{} atexit handler(s) leaked after running {} commands; each one "
        "retains its Popen handle for the life of the process".format(
            leaked, COMMANDS
        )
    )


def test_handler_retained_while_child_outlives_its_output():
    """EOF on the pipe does not mean the child exited.

    A command that closes both stdout and stderr while continuing to run
    reaches EOF immediately. The atexit handler must stay registered until the
    process actually exits, otherwise an interpreter exit would leave the child
    orphaned instead of terminating it.
    """
    before = atexit._ncallbacks()

    group = proc.Group()
    handle = group.run(["sh", "-c", "exec 1>/dev/null 2>/dev/null; sleep 5"])
    try:
        # wait for block_read to hit EOF (self.waiting drops back to zero)
        deadline = time.time() + 10
        while group.waiting > 0 and time.time() < deadline:
            group.readlines(timeout=0.05)
        time.sleep(0.3)

        assert handle.poll() is None, "child should still be running after EOF"
        assert atexit._ncallbacks() - before == 1, (
            "atexit handler was released while the child was still running; "
            "an interpreter exit would leave it orphaned"
        )
    finally:
        handle.kill()
        handle.wait()

    # once it really exits, the handler must be released
    deadline = time.time() + 5
    while atexit._ncallbacks() > before and time.time() < deadline:
        time.sleep(0.05)
    assert atexit._ncallbacks() - before == 0, "handler not released after exit"
