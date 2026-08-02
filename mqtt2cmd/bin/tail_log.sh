#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

UNIT="${MQTT2CMD_SYSTEMD_UNIT:-mqtt2cmd.service}"
LOG_LINES="${MQTT2CMD_LOG_LINES:-100}"

exec sudo journalctl \
    --unit="${UNIT}" \
    --lines="${LOG_LINES}" \
    --follow \
    --output=short-iso \
    "$@"
