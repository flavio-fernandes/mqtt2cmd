#!/usr/bin/env python

import datetime
import json
import multiprocessing
import signal
import sys
from collections import namedtuple
from typing import Dict

import dill
from shelljob import proc
from six.moves import queue

from mqtt2cmd import const
from mqtt2cmd import events
from mqtt2cmd import log
from mqtt2cmd.config import Cfg

CMDQ_SIZE = 100
CMDQ_GET_TIMEOUT = 3600  # seconds.
MAX_CONCURRENT_JOBS = 128
_state = None


class State(object):
    def __init__(self, queueEventFun, globals, topics, handlers):
        self.queueEventFun = queueEventFun  # queue for output events
        self.cmdq = multiprocessing.Queue(CMDQ_SIZE)  # queue for input commands
        self.globals = globals
        self.topics = topics
        self.handlers = handlers
        self.next_job_id: int = 1
        self.curr_jobs: Dict[int, Job] = {}


# =============================================================================


# external to this module, once
def do_init(queueEventFun=None):
    global _state

    cfg = Cfg()
    globals = cfg.globals
    assert isinstance(globals, dict)
    topics = cfg.topics
    assert isinstance(topics, dict)
    handlers = cfg.handlers
    assert isinstance(handlers, dict)

    _state = State(queueEventFun, globals, topics, handlers)
    # logger.debug("dispatcher init called")


# =============================================================================

ShellJobInfo = namedtuple('ShellJobInfo', 'grp timeout stop_on_failure in_sequence')


class Job(dict):
    def __init__(self, job_id, topic, payload, handler, attrs, vars, globals, commands):
        self.job_id = job_id
        self.create_ts = datetime.datetime.now().strftime("%I:%M:%S%p on %B %d, %Y")
        self.topic = topic
        self.payload = payload
        self.handler = handler
        # make payload and attrs part of 'self'
        self._parse_payload(payload)
        # globals overwrite payload
        self.__dict__.update(globals)
        # vars overwrite globals
        self.__dict__.update(vars)
        # attrs overwrite vars
        self.__dict__.update(attrs)
        try:
            self.commands = [self._expand_cmd(c) for c in commands]
        except ValueError as e:
            logger.error(e)
            self.commands = []
        self.shell_job_info = None
        self.shell_job_info_cmds = []
        self.shell_job_info_proc_handles = []
        self.shell_job_info_rc = 0
        super().__init__()

    def get(self, attr, default=None):
        return self.__dict__.get(attr, default)

    def __getattr__(self, attr):
        return self[attr]

    def __repr__(self):
        return str(self.__dict__)

    def _parse_payload(self, payload):
        try:
            json_object = json.loads(payload)
        except ValueError as e:
            logger.debug("dispatcher not looking into payload {}".format(e))
            return
        if isinstance(json_object, dict):
            self.__dict__.update(json_object)

    def _expand_cmd(self, cmd):
        rc, unknowns = self._expand_cmd2(cmd.split(), [], [])
        if unknowns:
            logger.error(
                "payload {} msg {} unable to find a value for '{}' in command '{}'".format(
                    self.payload, self.handler, ','.join(unknowns), cmd))
            # logger.error("failed parsing command for job {}".format(self))
            # return ''
            raise ValueError('failed parsing command for job', self)
        return ' '.join(rc)

    def _expand_cmd2(self, params: str, rc: list, unknowns: list):
        if not params:
            return rc, unknowns
        param = params[0]
        if param.startswith(const.CONFIG_VARIABLE_PREFIX) and param.endswith(
                const.CONFIG_VARIABLE_SUFFIX) and len(param) > len(
            const.CONFIG_VARIABLE_PREFIX + const.CONFIG_VARIABLE_SUFFIX):
            var_name = param[len(const.CONFIG_VARIABLE_PREFIX):]
            var_name = var_name[:len(var_name) - len(const.CONFIG_VARIABLE_SUFFIX)]
            if var_name in self.__dict__:
                rc.append(str(self.__dict__[var_name]))
            else:
                unknowns.append(param)
        else:
            rc.append(param)
        return self._expand_cmd2(params[1:], rc, unknowns)


def _do_handle_dispatch(topic, payload):
    global _state

    # find msg for topic and payload tuple
    topicPayloads = _state.topics.get(topic, {})
    handler = topicPayloads.get(payload) or topicPayloads.get(const.CONFIG_DEFAULT_PAYLOAD)
    job = _state.handlers.get(handler, {})
    attrs = job.get(const.CONFIG_ATTRS, {})
    vars = job.get(const.CONFIG_VARS, {})
    commands = job.get(const.CONFIG_CMDS, [])

    job = Job(_state.next_job_id, topic, payload, handler, attrs, vars, _state.globals, commands)
    _state.next_job_id += 1
    if not job.commands:
        logger.debug("dispatcher ignoring noop job {}".format(job))
        return

    jobs_count = len(_state.curr_jobs)
    if jobs_count >= MAX_CONCURRENT_JOBS:
        logger.error("too many jobs: {}. dispatcher unable to start job {})".format(
            jobs_count, job))
        return

    logger.info("dispatcher starting on job {} (curr total jobs: {})".format(job, jobs_count + 1))

    now = datetime.datetime.now()
    timeout_param = int(job.get(const.CONFIG_TIMEOUT, const.CONFIG_TIMEOUT_DEFAULT_SECS))
    if timeout_param:
        timeout = now + datetime.timedelta(seconds=timeout_param)
    else:
        timeout = now + datetime.timedelta(weeks=1)

    stop_on_failure = job.get(const.CONFIG_STOP, const.CONFIG_STOP_DEFAULT)
    in_sequence = job.get(const.CONFIG_IN_SEQ, const.CONFIG_IN_SEQ_DEFAULT)

    grp = proc.Group()
    job.shell_job_info_cmds = [c for c in job.commands]
    for c in job.commands:
        logger.debug("job %s handle %s starting command %s", job.job_id, job.handler, c)
        try:
            p = grp.run(c)
            job.shell_job_info_proc_handles.append(p)
        except proc.CommandException as e:
            logger.error("job %s handle %s failed command %s: %s", job.job_id, job.handler, c, e)
            logger.error("job %s %s", job.job_id, e)
            kill_job_procs(job)
            job.shell_job_info_cmds.clear()
            job.shell_job_info_rc = -1
            break
        if in_sequence:
            break

    job.shell_job_info = ShellJobInfo(grp, timeout, stop_on_failure, in_sequence)
    _state.curr_jobs[job.job_id] = job
    _enqueue_cmd((_noop, []))


def _noop():
    pass


def _notifyDispatcherHandlerDoneEvent(msg):
    logger.info("dispatcher done handling %s", msg)
    _notifyEvent(events.DispatcherHandlerDoneEvent(msg))


def _notifyEvent(event):
    global _state
    if not _state.queueEventFun:
        return
    _state.queueEventFun(event)


# =============================================================================

def _check_curr_jobs():
    global _state

    jobs_finished = [job_id for job_id in _state.curr_jobs if _check_job(job_id)]
    if jobs_finished:
        for job_id in jobs_finished:
            del _state.curr_jobs[job_id]
        logger.debug("dispatcher finished with jobs {} (curr total jobs: {})".format(
            jobs_finished, len(_state.curr_jobs)))


def _check_job(job_id):
    global _state

    job: Job = _state.curr_jobs[job_id]
    shi: ShellJobInfo = job.shell_job_info
    if not shi.grp.is_pending():
        lines = shi.grp.readlines(timeout=0.5)
        for _proc, line in lines:
            logger.debug("job %s handle %s output %s", job.job_id, job.handler, line)

    if not shi.grp.count_running():
        _enqueue_cmd((_noop, []))
        lines = shi.grp.readlines()
        for _proc, line in lines:
            logger.debug("job %s handle %s output %s", job.job_id, job.handler, line)
        if not job.shell_job_info_cmds:
            _notifyDispatcherHandlerDoneEvent(
                "job {} handle {} is finished with rc {}".format(job.job_id, job.handler,
                                                                 job.shell_job_info_rc))
            return True

        cmd = job.shell_job_info_cmds.pop(0)
        rc = 0
        for _p, rc in shi.grp.get_exit_codes():
            if rc != 0:
                job.shell_job_info_rc = rc
                break
        logger.debug("job %s handle %s finished cmd %s exit code %s", job.job_id, job.handler, cmd,
                     rc)
        shi.grp.clear_finished()
        if job.shell_job_info_rc and shi.stop_on_failure:
            job.shell_job_info_cmds.clear()
        if shi.in_sequence and job.shell_job_info_cmds:
            cmd = job.shell_job_info_cmds[0]
            logger.debug("job %s handle %s starting command cmd %s", job.job_id, job.handler, cmd)
            try:
                p = shi.grp.run(cmd)
                # start new proc handles
                job.shell_job_info_proc_handles = [p]
            except proc.CommandException as e:
                logger.error("job %s handle %s failed command %s: %s", job.job_id, job.handler,
                             cmd, e)
                job.shell_job_info_rc = -2
                job.shell_job_info_cmds.pop(0)
                job.shell_job_info_proc_handles = []

    now = datetime.datetime.now()
    if shi.timeout < now:
        logger.warning("job %s handle %s timed out", job.job_id, job.handler)
        kill_job_procs(job)
        job.shell_job_info_cmds.clear()
        job.shell_job_info_rc = -9

    return False


def kill_job_procs(job):
    for p in job.shell_job_info_proc_handles:
        logger.debug("job %s handle %s killing proc %s", job.job_id, job.handler, p)
        try:
            p.kill()
            p.wait(3)
        except Exception:
            pass


# =============================================================================

# external to this module
def do_iterate():
    global _state

    _check_curr_jobs()
    try:
        # quick timeout if there are jobs currently running
        timeout = 1 if _state.curr_jobs else CMDQ_GET_TIMEOUT
        cmdDill = _state.cmdq.get(True, timeout)
        cmdFun, params = dill.loads(cmdDill)
        if params:
            cmdFun(*params)
        else:
            cmdFun()
        logger.debug("executed a lambda command with params %s", params)
    except queue.Empty:
        # logger.debug("dispatcher iterate noop")
        pass
    except (KeyboardInterrupt, SystemExit):
        pass


# =============================================================================


def _enqueue_cmd(l):
    global _state
    lDill = dill.dumps(l)
    try:
        _state.cmdq.put(lDill, False)
    except queue.Full:
        logger.error("command queue is full: cannot add")
        return False
    return True


# external to this module
def do_dispatch(topic, payload):
    # chatty
    # logger.debug("queuing {} ping".format(location))
    params = [topic, payload]
    return _enqueue_cmd((_do_handle_dispatch, params))


# =============================================================================


def _signal_handler(signal, frame):
    logger.info("process terminated")
    sys.exit(0)


# =============================================================================


logger = log.getLogger()
if __name__ == "__main__":
    log.initLogger()
    do_init(None)
    signal.signal(signal.SIGINT, _signal_handler)
    while True:
        do_iterate()
