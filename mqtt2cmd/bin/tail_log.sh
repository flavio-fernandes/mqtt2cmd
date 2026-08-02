#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

UNIT="${MQTT2CMD_SYSTEMD_UNIT:-mqtt2cmd.service}"
LINES="${LINES:-100}"

exec sudo journalctl \
    --unit="${UNIT}" \
    --lines="${LINES}" \
    --follow \
    --output=short-iso \
    "$@"
