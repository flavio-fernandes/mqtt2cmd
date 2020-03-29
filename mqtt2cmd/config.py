#!/usr/bin/env python
import collections
import os
import sys
from collections import namedtuple

import yaml

from mqtt2cmd import const
from mqtt2cmd import log

CFG_FILENAME = os.path.dirname(os.path.abspath(const.__file__)) + "/../data/config.yaml"
MAX_CMDS_PER_HADLER = 20
Info = namedtuple('Info', 'mqtt knobs cfg_globals topics handlers raw')
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
    def mqtt_client_id(self):
        attr = self._get_info().mqtt
        if isinstance(attr, collections.abc.Mapping):
            return attr.get('client_id', const.MQTT_CLIENT_ID_DEFAULT)
        return const.MQTT_CLIENT_ID_DEFAULT

    @property
    def mqtt_status_topic(self):
        attr = self._get_info().mqtt
        if isinstance(attr, collections.abc.Mapping):
            return attr.get('status_topic', const.MQTT_CLIENT_DEFAULT_TOPIC_STATUS)
        return const.MQTT_CLIENT_DEFAULT_TOPIC_STATUS

    @property
    def mqtt_ping_topic(self):
        attr = self._get_info().mqtt
        if isinstance(attr, collections.abc.Mapping):
            return attr.get('ping_topic', const.MQTT_CLIENT_DEFAULT_TOPIC_PING)
        return const.MQTT_CLIENT_DEFAULT_TOPIC_PING

    @property
    def knobs(self):
        return self._get_info().knobs

    @property
    def cfg_globals(self):
        return self._get_info().cfg_globals

    @property
    def topics(self):
        return self._get_info().topics

    @property
    def handlers(self):
        return self._get_info().handlers

    @classmethod
    def _get_config_filename(cls):
        if len(sys.argv) > 1:
            return sys.argv[1]
        return CFG_FILENAME

    @classmethod
    def _get_info(cls):
        if not cls._info:
            config_filename = cls._get_config_filename()
            logger.info("loading yaml config file %s", config_filename)
            with open(config_filename, 'r') as ymlfile:
                raw_cfg = yaml.safe_load(ymlfile)
                cls._parse_raw_cfg(raw_cfg)
        return cls._info

    @classmethod
    def _parse_raw_cfg(cls, raw_cfg):
        cfg_globals = raw_cfg.get('globals', {})
        assert isinstance(cfg_globals, dict)
        topics = raw_cfg.get('topics')
        assert isinstance(topics, dict)
        handlers = raw_cfg.get('handlers')
        assert isinstance(handlers, dict)
        for topic, payloads in topics.items():
            for payload, h in payloads.items():
                if h not in handlers:
                    logger.warning(
                        "topic {} payload {} uses unknown msg {}".format(topic, payload, h))

        def get_handler_commands(curr_handler, curr_cmds_acc, curr_attrs_acc, curr_vars_acc):
            # Likely a circular dependency issue?!?
            assert len(
                curr_cmds_acc) < MAX_CMDS_PER_HADLER, "Too manny commands for {} msg: {}".format(
                curr_handler, curr_cmds_acc)

            job = handlers[curr_handler]
            cmd_raw = job.get(const.CONFIG_CMDS, None)
            if not cmd_raw:
                cmd_raw = job.get(const.CONFIG_CMD, None)
            else:
                assert job.get(const.CONFIG_CMD) is None, \
                    "Error in {}: {} and {} cannot be used together".format(
                        curr_handler, const.CONFIG_CMDS, const.CONFIG_CMD)

            # Translate singletons into a list
            if cmd_raw is None:
                cmds = []
            elif isinstance(cmd_raw, list):
                cmds = cmd_raw
            else:
                assert isinstance(cmd_raw, str)
                cmds = [cmd_raw]

            # Add attrs and vars w/out overrides
            # ref: https://stackoverflow.com/questions/6354436/python-dictionary-merge-by-updating-but-not-overwriting-if-value-exists   # noqa
            job_attrs = job.get(const.CONFIG_ATTRS, {})
            curr_attrs_acc = dict(list(job_attrs.items()) + list(curr_attrs_acc.items()))
            job_vars = job.get(const.CONFIG_VARS, {})
            curr_vars_acc = dict(list(job_vars.items()) + list(curr_vars_acc.items()))

            for cmd in cmds:
                # if command is a reference to another curr_handler, recurse
                if cmd in handlers:
                    curr_cmds_acc, curr_attrs_acc, curr_vars_acc = \
                        get_handler_commands(cmd, curr_cmds_acc, curr_attrs_acc, curr_vars_acc)
                else:
                    curr_cmds_acc.append(cmd)
            return curr_cmds_acc, curr_attrs_acc, curr_vars_acc

        handlers_parsed = {}
        for h in handlers:
            cmds_acc, attrs_acc, vars_acc = get_handler_commands(h, [], {}, {})
            handlers_parsed[h] = {const.CONFIG_ATTRS: attrs_acc,
                                  const.CONFIG_VARS: vars_acc,
                                  const.CONFIG_CMDS: cmds_acc}

        cls._info = Info(raw_cfg.get('mqtt'), raw_cfg.get('knobs', {}), cfg_globals, topics,
                         handlers_parsed, raw_cfg)


# =============================================================================


logger = log.getLogger()
if __name__ == "__main__":
    log.initLogger()
    c = Cfg()
    logger.info("c.knobs: {}".format(c.knobs))
    logger.info("c.mqtt_host: {}".format(c.mqtt_host))
    logger.info("c.cfg_globals: {}".format(c.cfg_globals))
    logger.info("c.topics: {}".format(c.topics))
    logger.info("c.handlers: {}".format(c.handlers))
