#!/usr/bin/env python
import collections

import yaml

from collections import namedtuple
from mqtt2cmd import const
from mqtt2cmd import log

CFG_FILENAME = "config.yaml"
MAX_CMDS_PER_HADLER = 20
Info = namedtuple('Info', 'mqtt globals topics handlers raw')
Handler = namedtuple('Handler', 'name commands')


class Cfg(object):
    _info = None  # class (or static) variable

    def __init__(self):
        pass

    @property
    def mqtt_host(self):
        attr = self._get_info().mqtt
        if isinstance(attr, collections.abc.Mapping):
            return attr.get('host')
        return None

    @property
    def globals(self):
        return self._get_info().globals

    @property
    def topics(self):
        return self._get_info().topics

    @property
    def handlers(self):
        return self._get_info().handlers

    @classmethod
    def _get_info(clazz):
        if not clazz._info:
            logger.info("loading yaml config file %s", CFG_FILENAME)
            with open(CFG_FILENAME, 'r') as ymlfile:
                rawCfg = yaml.safe_load(ymlfile)
                clazz._parse_rawCfg(rawCfg)
        return clazz._info

    @classmethod
    def _parse_rawCfg(clazz, rawCfg):
        globals = rawCfg.get('globals', {})
        assert isinstance(globals, dict)
        topics = rawCfg.get('topics')
        assert isinstance(topics, dict)
        handlers = rawCfg.get('handlers')
        assert isinstance(handlers, dict)
        for topic, payloads in topics.items():
            for payload, handler in payloads.items():
                if handler not in handlers:
                    logger.warning(
                        "topic {} payload {} uses unknown msg {}".format(topic, payload,
                                                                         handler))

        def getHandlerCommands(handler, rc):
            # Likely a circular dependency issue?!?
            assert len(rc) < MAX_CMDS_PER_HADLER, "Too manny commands for {} msg: {}".format(
                handler, rc)

            for c in handlers.get(handler, {}).get(const.CONFIG_CMDS, []):
                # if command is a reference to another msg, recurse
                if c in handlers:
                    rc = getHandlerCommands(c, rc)
                else:
                    rc.append(c)
            return rc

        handlersParsed = {}
        for handler, job in handlers.items():
            attrs = job.get(const.CONFIG_ATTRS, {})
            vars = job.get(const.CONFIG_VARS, {})
            cmds = getHandlerCommands(handler, [])
            handlersParsed[handler] = {const.CONFIG_ATTRS: attrs,
                                       const.CONFIG_VARS: vars,
                                       const.CONFIG_CMDS: cmds}

        clazz._info = Info(rawCfg.get('mqtt'), globals, topics, handlersParsed, rawCfg)


# =============================================================================


logger = log.getLogger()
if __name__ == "__main__":
    log.initLogger()
    c = Cfg()
    logger.info("c.mqtt_host: {}".format(c.mqtt_host))
    logger.info("c.globals: {}".format(c.globals))
    logger.info("c.topics: {}".format(c.topics))
    logger.info("c.handlers: {}".format(c.handlers))
