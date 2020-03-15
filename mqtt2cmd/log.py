#!/usr/bin/env python
import logging
from logging.handlers import SysLogHandler
from os import path


def getLogger():
    return logging.getLogger('mqtt2cmd')


def _log_handler_address(files=tuple()):
    try:
        return next(f for f in files if path.exists(f))
    except StopIteration:
        pass
    raise Exception("Invalid files: %s" % ", ".join(files))


def initLogger():
    logger = getLogger()
    logger.setLevel(logging.DEBUG)
    format = '%(asctime)s [mqtt2cmd] %(module)12s:%(lineno)-d %(levelname)-8s %(message)s'
    formatter = logging.Formatter(format)

    # Logs are normally configured here: /etc/rsyslog.d/*
    logHandlerAddress = _log_handler_address(
        ["/run/systemd/journal/syslog", "/var/run/syslog", "/dev/log"])
    syslog = SysLogHandler(address=logHandlerAddress,
                           facility=SysLogHandler.LOG_DAEMON)
    syslog.setFormatter(formatter)
    logger.addHandler(syslog)

    # FIXME(ff): DEBUG
    if True:
        consoleHandler = logging.StreamHandler()
        consoleHandler.setFormatter(formatter)
        logger.addHandler(consoleHandler)
