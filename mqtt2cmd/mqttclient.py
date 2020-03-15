#!/usr/bin/env python
import datetime
import multiprocessing
import signal
import sys
import time

import dill
import paho.mqtt.client as mqtt
from six.moves import queue

from mqtt2cmd import const
from mqtt2cmd import events
from mqtt2cmd import log
from mqtt2cmd.config import Cfg

DEFAULT_MQTT_BROKER_IP = "192.168.10.238"
CMDQ_SIZE = 100
CMDQ_GET_TIMEOUT = 66  # seconds. May affect ping publishing
TOPIC_QOS = 1
_state = None


class State(object):
    def __init__(self, queueEventFun, mqtt_broker_ip, topics):
        self.queueEventFun = queueEventFun  # queue for output events
        self.cmdq = multiprocessing.Queue(CMDQ_SIZE)  # queue for input commands
        self.mqtt_broker_ip = mqtt_broker_ip
        self.topics = topics
        self.mqtt_client = None
        self.next_ping_ts = datetime.datetime.now()


# =============================================================================


# external to this module, once
def do_init(queueEventFun=None):
    global _state

    cfg = Cfg()
    mqtt_broker_ip = cfg.mqtt_host or DEFAULT_MQTT_BROKER_IP
    topics = cfg.topics
    assert isinstance(topics, dict)

    _state = State(queueEventFun, mqtt_broker_ip, list(topics.keys()))
    # logger.debug("mqttclient init called")


# =============================================================================

def _notifyMqttMsgEvent(topic, payload):
    global _state

    # filter out topics that we do not care about
    if topic not in _state.topics:
        logger.warning("ignoring mqtt message %s %s", topic, payload)
        return
    logger.info("got mqtt message %s %s", topic, payload)
    _notifyEvent(events.MqttMsgEvent(topic, payload))


def _notifyEvent(event):
    global _state
    if not _state.queueEventFun:
        return
    _state.queueEventFun(event)


# =============================================================================


def client_connect_callback(client, userdata, flags_dict, rc):
    if rc != mqtt.MQTT_ERR_SUCCESS:
        logger.warning("client connect failed with flags %s rc %s %s",
                       flags_dict, rc, mqtt.error_string(rc))
        return
    logger.info("client connected with flags %s rc %s", flags_dict, rc)
    # userdata is list of topics we care about
    assert isinstance(userdata, list), "Unexpected userdata from callback: {}".format(userdata)
    assert userdata[0] == 'topics', "Unexpected userdata from callback: {}".format(userdata)
    mqtt2cmd_topics = [(t, TOPIC_QOS) for t in userdata[1:]]
    client.subscribe(mqtt2cmd_topics)


def client_message_callback(_client, _userdata, msg):
    logger.debug("callback for mqtt message %s %s", msg.topic, msg.payload)
    topic = msg.topic.decode('utf-8') if isinstance(msg.topic, bytes) else msg.topic
    payload = msg.payload.decode('utf-8') if isinstance(msg.payload, bytes) else msg.payload
    params = [topic, payload]
    _enqueue_cmd((_notifyMqttMsgEvent, params))


def _setup_mqtt_client(broker_ip, topics):
    try:
        userdata = ['topics'] + topics
        client = mqtt.Client(client_id="mqtt2cmd", userdata=userdata)
        client.on_connect = client_connect_callback
        client.on_message = client_message_callback

        client.connect_async(broker_ip, port=1883, keepalive=181)
        return client
    except Exception as e:
        logger.info("mqtt client did not work %s", e)
    return None


# =============================================================================


# external to this module
def do_iterate():
    global _state

    if not _state.mqtt_client:
        _state.mqtt_client = _setup_mqtt_client(_state.mqtt_broker_ip, _state.topics)
        if not _state.mqtt_client:
            logger.warning("got no mqttt client")
            time.sleep(30)
            return
        logger.debug("have a mqtt_client now")
        _state.mqtt_client.loop_start()

    now = datetime.datetime.now()
    if _state.mqtt_client.is_connected() and now > _state.next_ping_ts:
        ts = now.strftime("%I:%M:%S%p on %B %d, %Y")
        _do_handle_mqtt_ping(ts)
        _state.next_ping_ts = now + datetime.timedelta(
            seconds=const.MQTT_CLIENT_PING_INTERVAL_SECS)

    try:
        cmdDill = _state.cmdq.get(True, CMDQ_GET_TIMEOUT)
        cmdFun, params = dill.loads(cmdDill)
        cmdFun(*params)
        logger.debug("executed a lambda command with params %s", params)
    except queue.Empty:
        # logger.debug("mqttclient iterate noop")
        pass
    except (KeyboardInterrupt, SystemExit):
        pass


# =============================================================================


def _do_handle_mqtt_status(payload):
    _mqtt_publish(const.MQTT_CLIENT_STATUS_TOPIC, payload)


def _do_handle_mqtt_ping(payload):
    _mqtt_publish(const.MQTT_CLIENT_PING_TOPIC, payload)


# =============================================================================


def _mqtt_publish(topic, value=None, qos=0):
    global _state
    if not _state.mqtt_client:
        logger.warning("no client to publish mqtt topic %s %s", topic, value)
        return
    try:
        # logger.debug("publishing mqtt topic %s %s", topic, newState)
        info = _state.mqtt_client.publish(topic, value, qos)
        info.wait_for_publish()
    except Exception as e:
        logger.error("client failed publish mqtt topic %s %s %s", topic, value, e)
        return
    logger.debug("published mqtt topic %s %s", topic, value)


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
def do_mqtt_status(payload):
    # chatty
    # logger.debug("queuing {} ping".format(location))
    params = [payload]
    return _enqueue_cmd((_do_handle_mqtt_status, params))


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
