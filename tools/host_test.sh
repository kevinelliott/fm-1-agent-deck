#!/bin/sh
# Build the firmware app for this computer (Dinghy's host harness: the
# hardware stubbed out) and run it through the scripted session in
# firmware-build/tests/agtdeck.txt, which tools/gen_host_test.py writes.
# Fails on a compiler warning-as-error, a crash, or a session that does
# not end with its sound file written. No toolchain, no device.
set -e
cd "$(dirname "$0")/.."
python3 tools/gen_host_test.py >/dev/null
cd firmware-build
mkdir -p build/host
SECTION=
[ "$(uname -s)" = Darwin ] && SECTION='-Dsection(x)='
cc -O2 -Wall -Wextra -Werror -Wno-missing-field-initializers -Wno-unused-function \
   -Wno-unused-parameter -DDINGHY_HOST $SECTION -DAPP_VER='"dev"' \
   -Ifirmware/hal -Ifirmware/src -Ifirmware/gen \
   -o build/host/agtdeck firmware/src/dinghy.c
rm -f build/host/agtdeck-*.ppm
build/host/agtdeck build/host/agtdeck tests/agtdeck.txt > build/host/session.log
tail -1 build/host/session.log | grep -q '^sound' || { echo "host test: the session did not finish"; tail -5 build/host/session.log; exit 1; }
n=$(ls build/host/agtdeck-*.ppm 2>/dev/null | wc -l | tr -d ' ')
[ "$n" -ge 8 ] || { echo "host test: only $n screens rendered"; exit 1; }
echo "host test: ok ($n screens rendered)"
