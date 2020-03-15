#!/usr/bin/env python


class Base(object):
    def __init__(self, group, description, params=[]):
        self.name = self.__class__.__name__
        self.group = group
        self.description = description
        self.params = params


class MqttMsgEvent(Base):
    def __init__(self, topic, payload):
        params = [topic, payload]
        Base.__init__(self, "mqtt", "mqtt msg", params)


class DispatcherHandlerDoneEvent(Base):
    def __init__(self, msg):
        params = [msg]
        Base.__init__(self, "dispatcher", "dispatcher msg done", params)
