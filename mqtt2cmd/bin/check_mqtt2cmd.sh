#!/bin/bash

set -o errexit
#set -x

MQTT=192.168.10.238
/usr/bin/mosquitto_pub -h $MQTT -r -t /mqtt2cmd -m ping
sleep 3
RC=$(/usr/bin/timeout --preserve-status 5s /usr/bin/mosquitto_sub -h $MQTT -t /mqtt2cmd ||:)
EXP_RC='^pong at '
[[ "$RC" =~ ${EXP_RC} ]] || {
    sudo systemctl restart mqtt2cmd
    exit 1
}
