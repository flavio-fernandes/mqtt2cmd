#!/bin/bash

if [ -n "$1" ]; then
    sudo journalctl -u mqtt2cmd.service --no-pager --follow
else
    sudo tail -F /var/log/syslog | grep mqtt2cmd
fi
