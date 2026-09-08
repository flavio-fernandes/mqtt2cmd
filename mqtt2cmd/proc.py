# Subprocess containers
"""
# Copied from mortoray / shelljob
# - https://pypi.org/project/shelljob/

    A mechanism to run subprocesses asynchronously and with non-blocking read.
"""
import atexit
import os
import queue
import shlex
import subprocess
import threading


_active_handles = set()
_active_handles_lock = threading.Lock()
_owner_pid = os.getpid()


def _terminate_active_handles():
    """Terminate every child still running when the interpreter exits."""
    # A forked child inherits this registry but owns none of these processes;
    # terminating them would signal the parent's children. Testing the pid
    # before touching the lock also avoids blocking forever on a lock that a
    # thread which does not exist in this process held at fork time if the
    # registered child hook ever fails to run.
    if os.getpid() != _owner_pid:
        return

    with _active_handles_lock:
        handles = list(_active_handles)

    for handle in handles:
        try:
            handle.terminate()
        except Exception:
            pass  # may have exited after the snapshot was taken


# One stable callback for the life of the interpreter. Registering and
# unregistering a closure per child leaves an inactive atexit registry slot
# behind on CPython 3.10-3.13, so the churn itself grows without bound.
atexit.register(_terminate_active_handles)


def _reset_after_fork():
    """Re-arm tracking in a forked child so it can use this module itself.

    The pid guard above already makes the inherited state harmless. This
    additionally lets a forked child run its own commands and have them
    terminated at its exit, and replaces a lock that may have been held by a
    thread that does not exist here.
    """
    global _active_handles_lock, _owner_pid
    _active_handles.clear()
    _active_handles_lock = threading.Lock()
    _owner_pid = os.getpid()


# Non-forking platforms such as Windows do not expose this Unix-only API.
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


class CommandException(Exception):
    def __init__(self, msg):
        super(CommandException, self).__init__(msg)


class Group:
    """
    Runs a subprocess in parallel, capturing it's output and providing non-blocking reads
    (well, at least for the caller they appear non-blocking).
    """

    def __init__(self):
        self.output = queue.Queue()
        self.handles = []
        self.waiting = 0

    def __del__(self):
        self.close()

    def run(self, cmd, shell=False):
        """
        Adds a new process to this object. This process is run and the output collected.

        @param cmd: the command to execute. This may be an array as passed to Popen,
            or a string, which will be parsed by 'shlex.split'
        @param shell: specifies whether to use the shell as the program to execute.
        @return: the handle to the process return from Popen
        """
        try:
            return self._run_impl(cmd, shell)
        except Exception as e:
            raise CommandException("Group.run '{}' failed".format(cmd)) from e

    def _run_impl(self, cmd, shell):
        cmd = _expand_cmd(cmd)

        handle = subprocess.Popen(
            cmd,
            shell=shell,
            # bufsize=1,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # needed to detach from calling terminal (other wacky things
            # can happen)
            stdin=subprocess.PIPE,
            close_fds=True,
        )
        handle.group_output_done = False
        self.handles.append(handle)

        # Keep only live children in the module-level shutdown registry.  The
        # registry's single atexit callback avoids one callback slot per run.
        with _active_handles_lock:
            _active_handles.add(handle)

        # a thread is created to do blocking-read
        self.waiting += 1

        def block_read():
            try:
                for line in iter(handle.stdout.readline, b""):
                    self.output.put((handle, line))
            except Exception:
                pass
            finally:
                # To force return of any waiting read (and indicate this process is done
                try:
                    self.output.put((handle, None))
                except Exception:
                    pass

                # Everything here must run on every path. A skipped decrement
                # leaves readlines() waiting forever, and a skipped discard
                # retains the handle for the life of the interpreter.
                for stream in (handle.stdout, handle.stdin):
                    try:
                        stream.close()
                    except Exception:
                        pass
                self.waiting -= 1

                # EOF on the pipe only means the child closed (or replaced) its
                # stdout and stderr -- it may still be running. Wait for the real
                # exit so a child that outlives its output is still terminated.
                try:
                    handle.wait()
                except Exception:
                    pass
                with _active_handles_lock:
                    _active_handles.discard(handle)

        block_thread = threading.Thread(target=block_read)
        block_thread.daemon = True
        block_thread.start()

        return handle

    def readlines(self, max_lines=1000, timeout=2.0):
        """
        Reads available lines from any of the running processes. If no lines are available now
        it will wait until 'timeout' to read a line. If nothing is running the timeout is not
        waited and the function simply returns.

        When a process has been completed and all output has been read from it, a
        variable 'group_ouput_done' will be set to True on the process handle.

        @param timeout: how long to wait if there is nothing available now
        @param max_lines: maximum number of lines to get at once
        @return: An array of tuples of the form:
            ( handle, line )
            There 'handle' was returned by 'run' and 'line' is the line which is read.
            If no line is available an empty list is returned.
        """
        lines = []
        try:
            while len(lines) < max_lines:
                handle, line = self.output.get_nowait()
                # interrupt waiting if nothing more is expected
                if line is None:
                    handle.group_output_done = True
                    if self.waiting == 0:
                        break
                else:
                    lines.append((handle, line))
            return lines

        except queue.Empty:
            # if nothing yet, then wait for something
            if len(lines) > 0 or self.waiting == 0:
                return lines

            item = self.readline(timeout=timeout)
            if item is not None:
                lines.append(item)
            return lines

    def readline(self, timeout=2.0):
        """
        Read a single line from any running process.

        Note that this will end up blocking for timeout once all processes have completed.
        'readlines' however can properly handle that situation and stop reading once
        everything is complete.

        @return: Tuple of ( handle, line ) or None if no output generated.
        """
        try:
            handle, line = self.output.get(timeout=timeout)
            if line is None:
                handle.group_output_done = True
                return None
            return handle, line
        except queue.Empty:
            return None

    def is_pending(self):
        """
        Determine if calling readlines would actually yield any output. This returns true
        if there is a process running or there is data in the queue.
        """
        if self.waiting > 0:
            return True
        return not self.output.empty()

    def count_running(self):
        """
        Return the number of processes still running. Note that although a process may
        be finished there could still be output from it in the queue. You should use
        'is_pending' to determine if you should still be reading.
        """
        count = 0
        for handle in self.handles:
            if handle.poll() is None:
                count += 1
        return count

    def get_exit_codes(self):
        """
        Return a list of all processes and their exit code.

        @return: A list of tuples:
            ( handle, exit_code )
            'handle' as returned from 'run'
            'exit_code' of the process or None if it has not yet finished
        """
        codes = []
        for handle in self.handles:
            codes.append((handle, handle.poll()))
        return codes

    def clear_finished(self):
        """
        Remove all finished processes from the managed list.
        """
        nhandles = []
        for handle in self.handles:
            if not handle.group_output_done or handle.poll() is None:
                nhandles.append(handle)
        self.handles = nhandles

    def close(self):
        """
        Experimental closing of all handles, even if they haven't finished. This likely doesn't
        work on all platforms
        """
        for handle in self.handles:
            try:
                handle.terminate()
            except Exception:
                pass
            handle.group_output_done = True

        self.get_exit_codes()


class BadExitCode(Exception):
    def __init__(self, exit_code, output):
        Exception.__init__(
            self, "subprocess-bad-exit-code: {}: {}".format(exit_code, output[:1024])
        )
        self.exit_code = exit_code
        self.output = output


class Timeout(Exception):
    def __init__(self, output):
        Exception.__init__(self, "subprocess-timeout")
        self.output = output


def call(
    cmd, encoding="utf-8", shell=False, check_exit_code=True, timeout=None, cwd=None
):
    """
    Calls a subprocess and returns the output and optionally exit code.

    @param cmd: the command to execute. This may be an array as passed to Popen,
        or a string, which will be parsed by 'shlex.split'
    @param encoding: convert output to unicode objects with this encoding, set to None to
        get the raw output
    @param shell: specifies whether to use the shell as the program to execute.
    @param check_exit_code: set to False to ignore the exit code, otherwise any non-zero
        result will throw BadExitCode.
    @param timeout: If specified only this amount of time (seconds) will be waited for
        the subprocess to return
    @param cwd: x
    @return: If check_exit_code is False: list( output, exit_code ), else just the output
    """
    cmd = _expand_cmd(cmd)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        shell=shell,
        cwd=cwd,
        close_fds=True,
    )

    def decode(param_out):
        if encoding is not None:
            return param_out.decode(encoding)
        else:
            return raw_out

    if timeout is None:
        raw_out, ignore_err = proc.communicate()
    else:
        # Read from subprocess in a thread so the main one can check for the timeout
        outq = queue.Queue()

        def block_read():
            proc_out = proc.stdout.read()
            # wait before pushing, occasionally read returns prior to process terminating,
            # thus "poll" would return None
            proc.wait()
            outq.put(proc_out)

        block_thread = threading.Thread(target=block_read)
        block_thread.daemon = True
        block_thread.start()

        try:
            raw_out = outq.get(True, timeout)
        except queue.Empty:
            proc.terminate()
            # wait again for partial output (process is terminated, so reading should end)
            raw_out = outq.get()
            raise Timeout(decode(raw_out))

    out = decode(raw_out)
    exit_code = proc.poll()

    if check_exit_code:
        if exit_code != 0:
            raise BadExitCode(exit_code, out)
        return out

    return out, proc.poll()


def _expand_cmd(cmd):
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    return cmd
