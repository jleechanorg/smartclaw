#!/bin/bash
# Send /loop steering prompt to antig cmux terminal every 10 min
export CMUX_SOCKET_PATH=/private/tmp/cmux-debug-appclick.sock
export CMUX_WORKSPACE_ID=workspace:2
export CMUX_SURFACE_ID=surface:4
/Applications/cmux.app/Contents/Resources/bin/cmux send --workspace workspace:2 --surface surface:4 "/loop"
/Applications/cmux.app/Contents/Resources/bin/cmux send-key --workspace workspace:2 --surface surface:4 Enter
