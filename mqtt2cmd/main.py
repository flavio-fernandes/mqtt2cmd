#!/usr/bin/env python

import multiprocessing

from six.moves import queue

from mqtt2cmd import events, dispatcher
from mqtt2cmd import log
from mqtt2cmd import mqttclient

EVENTQ_SIZE = 1000
EVENTQ_GET_TIMEOUT = 15  # seconds


class ProcessBase(multiprocessing.Process):
    def __init__(self, eventq):
        multiprocessing.Process.__init__(self)
        self.eventq = eventq

    def putEvent(self, event):
        try:
            self.eventq.put(event, False)
        except queue.Full:
            logger.error("Exiting: Queue is stuck, cannot add event: %s %s",
                         event.name, event.description)
            raise RuntimeError("Main process has a full event queue")


class MqttclientProcess(ProcessBase):
    def __init__(self, eventq):
        ProcessBase.__init__(self, eventq)
        mqttclient.do_init(self.putEvent)

    def run(self):
        logger.debug("mqttclient process started")
        while True:
            mqttclient.do_iterate()


def processMqttMsgEvent(topic, payload):
    dispatcher.do_dispatch(topic, payload)


def processEventMqttClient(event):
    syncFunHandlers = {"MqttMsgEvent": processMqttMsgEvent, }
    cmdFun = syncFunHandlers.get(event.name)
    if not cmdFun:
        logger.warning("Don't know how to process event %s: %s", event.name, event.description)
        return
    if event.params:
        cmdFun(*event.params)
    else:
        cmdFun()


class DispatcherProcess(ProcessBase):
    def __init__(self, eventq):
        ProcessBase.__init__(self, eventq)
        dispatcher.do_init(self.putEvent)

    def run(self):
        logger.debug("dispatcher process started")
        while True:
            dispatcher.do_iterate()


def processHandlerDoneEvent(msg):
    mqttclient.do_mqtt_status(msg)


def processEventDispatcher(event):
    syncFunHandlers = {"DispatcherHandlerDoneEvent": processHandlerDoneEvent, }
    cmdFun = syncFunHandlers.get(event.name)
    if not cmdFun:
        logger.warning("Don't know how to process event %s: %s", event.name, event.description)
        return
    if event.params:
        cmdFun(*event.params)
    else:
        cmdFun()


def processEvent(event):
    # Based on the event, call a lambda to make mqtt and smartswitch in sync
    syncFunHandlers = {"mqtt": processEventMqttClient,
                       "dispatcher": processEventDispatcher, }
    cmdFun = syncFunHandlers.get(event.group)
    if not cmdFun:
        logger.warning("Don't know how to process event %s: %s", event.name, event.description)
        return
    cmdFun(event)


def processEvents(timeout):
    global stop_gracefully
    try:
        event = eventq.get(True, timeout)
        if isinstance(event, events.Base):
            logger.debug("Process event for %s", type(event))
            processEvent(event)
        else:
            logger.warning("Ignoring unexpected event: %s", event)
    except (KeyboardInterrupt, SystemExit):
        logger.info("got KeyboardInterrupt")
        stop_gracefully = True
    except queue.Empty:
        # make sure children are still running
        for p in myProcesses:
            if p.is_alive():
                continue
            logger.error("%s child died", p.__class__.__name__)
            logger.info("exiting so systemd can restart")
            raise RuntimeError("Child process terminated unexpectedly")


def main():
    try:
        # Start our processes
        [p.start() for p in myProcesses]
        logger.debug("Starting main event processing loop")
        while not stop_gracefully:
            processEvents(EVENTQ_GET_TIMEOUT)
    except Exception as e:
        logger.error("Unexpected event: %s", e)
    # make sure all children are terminated
    [p.terminate() for p in myProcesses]


# globals
stop_gracefully = False
logger = None
eventq = None
myProcesses = []

if __name__ == "__main__":
    # global logger, eventq, myProcesses

    logger = log.getLogger()
    log.initLogger()
    logger.debug("mqtt2cmd process started")
    eventq = multiprocessing.Queue(EVENTQ_SIZE)
    myProcesses.append(MqttclientProcess(eventq))
    myProcesses.append(DispatcherProcess(eventq))
    main()
    if not stop_gracefully:
        raise RuntimeError("main is exiting")
