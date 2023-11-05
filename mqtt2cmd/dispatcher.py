#!/usr/bin/env python

import datetime
import json
import multiprocessing
import signal
import sys
from collections import namedtuple

import dill
from flashtext import KeywordProcessor
from six.moves import queue

from mqtt2cmd import const
from mqtt2cmd import events
from mqtt2cmd import log
from mqtt2cmd import proc
from mqtt2cmd.config import Cfg

CMDQ_SIZE = 100
CMDQ_GET_TIMEOUT = 3600  # seconds.
MAX_CONCURRENT_JOBS = 128
MAX_NESTED_LOOKUPS = 20
_state = None


class State(object):
    def __init__(self, queue_event_fun, cfg_globals, topics, handlers):
        self.queueEventFun = queue_event_fun  # queue for output events
        self.cmdq = multiprocessing.Queue(CMDQ_SIZE)  # queue for input commands
        self.cfg_globals = cfg_globals
        self.topics = topics
        self.handlers = handlers
        self.next_job_id = 1
        self.curr_jobs = {}


# =============================================================================


# external to this module, once
def do_init(queueEventFun=None):
    global _state

    cfg = Cfg()
    cfg_globals = cfg.cfg_globals
    assert isinstance(cfg_globals, dict)
    topics = cfg.topics
    assert isinstance(topics, dict)
    handlers = cfg.handlers
    assert isinstance(handlers, dict)

    _state = State(queueEventFun, cfg_globals, topics, handlers)
    # logger.debug("dispatcher init called")


# =============================================================================

ShellJobInfo = namedtuple(
    "ShellJobInfo", "grp timeout stop_on_failure in_sequence log_output shell"
)


class Job(dict):
    def __init__(
        self, job_id, topic, payload, handler, attrs, vars, cfg_globals, commands
    ):
        self.job_id = job_id
        self.create_ts = datetime.datetime.now().strftime("%I:%M:%S%p on %B %d, %Y")
        self.topic = topic
        self.payload = payload
        self.handler = handler
        # make payload and attrs part of 'self'
        self._parse_payload(payload)
        # cfg_globals overwrite payload
        self.__dict__.update(cfg_globals)
        # vars overwrite cfg_globals
        self.__dict__.update(vars)
        # attrs overwrite vars
        self.__dict__.update(attrs)

        # ref: https://flashtext.readthedocs.io/en/latest/#
        keyword_processor = KeywordProcessor(case_sensitive=True)
        keyword_processor.set_non_word_boundaries(set(["~"]))
        for k, v in self.__dict__.items():
            keyword_processor.add_keyword(
                "{}{}{}".format(
                    const.CONFIG_VARIABLE_PREFIX, k, const.CONFIG_VARIABLE_SUFFIX
                ),
                str(v),
            )

        try:
            self.commands = [
                self._replace_keywords(keyword_processor, c, 0) for c in commands
            ]
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
            logger.debug("not looking into payload {}".format(e))
            return
        if isinstance(json_object, dict):
            self.__dict__.update(json_object)

    def _replace_keywords(self, keyword_processor, cmd, depth: int):
        cmd2 = keyword_processor.replace_keywords(cmd)
        if cmd != cmd2:
            if depth <= MAX_NESTED_LOOKUPS:
                return self._replace_keywords(keyword_processor, cmd2, depth + 1)
            # Likely a circular dependency issue?!?
            logger.error(
                "payload {} msg {} too many recursions to extract value for {}".format(
                    self.payload, self.handler, cmd
                )
            )
            raise ValueError("failed nested lookup variables for job", self)
        return cmd


def _do_handle_dispatch(topic, payload):
    global _state

    # find msg for topic and payload tuple
    topicPayloads = _state.topics.get(topic, {})
    handler = topicPayloads.get(payload) or topicPayloads.get(
        const.CONFIG_DEFAULT_PAYLOAD
    )
    handler_job = _state.handlers.get(handler, {})
    attrs = handler_job.get(const.CONFIG_ATTRS, {})
    vars = handler_job.get(const.CONFIG_VARS, {})
    commands = handler_job.get(const.CONFIG_CMDS, [])

    job = Job(
        _state.next_job_id,
        topic,
        payload,
        handler,
        attrs,
        vars,
        _state.cfg_globals,
        commands,
    )

    _state.next_job_id += 1
    if _state.next_job_id > 0xFFFF:
        logger.info("wrapping next_job_id from {} to 1".format(_state.next_job_id))
        _state.next_job_id = 1

    if not job.commands:
        logger.debug("ignoring noop job {}".format(job))
        return

    jobs_count = len(_state.curr_jobs)
    if jobs_count >= MAX_CONCURRENT_JOBS:
        logger.error(
            "too many jobs: {}. dispatcher unable to start job {})".format(
                jobs_count, job
            )
        )
        return

    now = datetime.datetime.now()
    timeout_param = int(
        job.get(const.CONFIG_TIMEOUT, const.CONFIG_TIMEOUT_DEFAULT_SECS)
    )
    if timeout_param:
        timeout = now + datetime.timedelta(seconds=timeout_param)
    else:
        timeout = now + datetime.timedelta(weeks=1)

    stop_on_failure = job.get(const.CONFIG_STOP, const.CONFIG_STOP_DEFAULT)
    in_sequence = job.get(const.CONFIG_IN_SEQ, const.CONFIG_IN_SEQ_DEFAULT)
    log_output = job.get(
        const.CONFIG_LOG_CMD_OUTPUT, const.CONFIG_LOG_CMD_OUTPUT_DEFAULT
    )
    shell = job.get(const.CONFIG_SHELL, const.CONFIG_SHELL_DEFAULT)

    logger.debug("starting job {})".format(job))

    job_params_info = ""
    if stop_on_failure != const.CONFIG_STOP_DEFAULT:
        job_params_info += " stop_on_failure: {}".format(stop_on_failure)
    if in_sequence != const.CONFIG_IN_SEQ_DEFAULT:
        job_params_info += " in_sequence: {}".format(in_sequence)
    if timeout_param != const.CONFIG_TIMEOUT_DEFAULT_SECS:
        job_params_info += " timeout: {}".format(timeout)
    if log_output != const.CONFIG_LOG_CMD_OUTPUT_DEFAULT:
        job_params_info += " log_output: {}".format(log_output)
    if shell != const.CONFIG_SHELL_DEFAULT:
        job_params_info += " shell: {}".format(shell)

    logger.info(
        "starting job: {} handler: {} cmds: {}{} (curr total jobs: {})".format(
            job.job_id, job.handler, job.commands, job_params_info, jobs_count + 1
        )
    )

    grp = proc.Group()
    job.shell_job_info_cmds = [c for c in job.commands]
    for c in job.commands:
        logger.debug("job %s handle %s starting command %s", job.job_id, job.handler, c)
        try:
            p = grp.run(c, shell=shell)
            job.shell_job_info_proc_handles.append(p)
        except proc.CommandException as e:
            logger.error(
                "job %s handle %s failed command %s: %s", job.job_id, job.handler, c, e
            )
            logger.error("job %s %s", job.job_id, e)
            kill_job_procs(job)
            job.shell_job_info_cmds.clear()
            job.shell_job_info_rc = -1
            break
        if in_sequence:
            break

    job.shell_job_info = ShellJobInfo(
        grp, timeout, stop_on_failure, in_sequence, log_output, shell
    )
    _state.curr_jobs[job.job_id] = job
    _enqueue_cmd((_noop, []))


def _noop():
    pass


def _notifyDispatcherHandlerDoneEvent(msg):
    logger.info("done handling %s", msg)
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
        logger.debug(
            "finished with jobs {} (curr total jobs: {})".format(
                jobs_finished, len(_state.curr_jobs)
            )
        )


def _log_shell_job_output(job):
    shi = job.shell_job_info
    lines = shi.grp.readlines(timeout=0.2)
    for _proc, line in lines:
        logger.debug("job %s handle %s output %s", job.job_id, job.handler, line)
        if shi.log_output:
            logger.info("job %s handle %s output %s", job.job_id, job.handler, line)


def _check_job(job_id):
    global _state

    job = _state.curr_jobs[job_id]
    shi = job.shell_job_info
    _log_shell_job_output(job)

    if not shi.grp.count_running():
        # output one last time for cmd(s)
        _log_shell_job_output(job)
        _enqueue_cmd((_noop, []))

        if not job.shell_job_info_cmds:
            _notifyDispatcherHandlerDoneEvent(
                "job {} handle {} finished with rc {}".format(
                    job.job_id, job.handler, job.shell_job_info_rc
                )
            )
            return True

        cmd = job.shell_job_info_cmds.pop(0)
        rc = 0
        if not job.shell_job_info_proc_handles:
            # If we make it here, it means that last cmd did not make it
            # very far, and there are no exit_codes in shi that we can use.
            # In such cases, simply use the value stored in aggregate result.
            rc = job.shell_job_info_rc
        else:
            for _p, rc in shi.grp.get_exit_codes():
                if rc != 0:
                    job.shell_job_info_rc = rc
                    break
        logger.debug(
            "job %s handle %s finished cmd %s exit code %s",
            job.job_id,
            job.handler,
            cmd,
            rc,
        )
        shi.grp.clear_finished()

        if job.shell_job_info_rc and shi.stop_on_failure:
            job.shell_job_info_cmds.clear()

        if shi.in_sequence and job.shell_job_info_cmds:
            cmd = job.shell_job_info_cmds[0]
            logger.debug(
                "job %s handle %s starting command cmd %s", job.job_id, job.handler, cmd
            )
            try:
                p = shi.grp.run(cmd, shi.shell)
                # start new proc handles
                job.shell_job_info_proc_handles = [p]
            except proc.CommandException as e:
                logger.error(
                    "job %s handle %s failed command %s: %s",
                    job.job_id,
                    job.handler,
                    cmd,
                    e,
                )
                job.shell_job_info_rc = -2
                # job.shell_job_info_cmds.pop(0)  NOTE: pop(0) happens on next iteration
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


def _signal_handler(_signal, _frame):
    logger.info("process terminated")
    sys.exit(0)


# =============================================================================


logger = log.getLogger()
if __name__ == "__main__":
    log.initLogger(testing=True)
    do_init(None)
    signal.signal(signal.SIGINT, _signal_handler)
    while True:
        do_iterate()
