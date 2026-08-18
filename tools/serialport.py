#!/usr/bin/env python3
"""Find the BF07 debug UART on whatever OS this happens to be running on.

The tools used to glob "/dev/cu.usbserial-*", which is a macOS spelling: the
same FTDI cable is /dev/ttyUSB0 on Linux and COM3 on Windows. Rather than
teach every tool three globs, ask pyserial -- list_ports works on all three.

Resolution order, first hit wins:
    1. an explicit argument, or --port/-p on the command line
    2. $BF07_PORT
    3. autodetect

The value in 1 and 2 may be a full device path, or any substring of a port's
serial number / description / path. `--port AV7K776E` therefore selects that
one cable on every OS, which a device path cannot do.

Autodetect deliberately refuses to guess between two plausible cables: on a
bench with several USB-serial devices, silently picking the wrong one writes
flash commands at something that is not the reader. It raises and lists them
instead. Note that a Nordic PPK2 -- which is usually on the bench precisely
when you are measuring this board -- enumerates two CDC-ACM ports, so
"the only serial device present" is rarely true here.
"""
import os
import sys

import serial
import serial.tools.list_ports

BAUD = 2000000

# USB-serial bridge chips: an adapter with one of these is almost certainly a
# UART cable rather than a device that merely speaks CDC-ACM.
UART_BRIDGE_VIDS = {
    0x0403,  # FTDI
    0x10C4,  # Silicon Labs CP210x
    0x10C5,  # Silicon Labs
    0x067B,  # Prolific PL2303
    0x1A86,  # WCH CH340/CH341
    0x2341,  # Arduino-style FTDI/16u2 cables
}

# Things that enumerate as serial ports but are not the debug cable.
NOT_A_UART_VIDS = {
    0x1915,  # Nordic Semiconductor -- e.g. the PPK2 power profiler
}

NOT_A_UART_PATHS = ("/dev/ttyS", "/dev/cu.Bluetooth", "/dev/cu.debug-console",
                    "/dev/cu.wlan-debug")


class PortError(RuntimeError):
    pass


def _usable(p):
    if p.device.startswith(NOT_A_UART_PATHS):
        return False
    if p.vid is None:                       # no USB behind it: built-in port
        return False
    if p.vid in NOT_A_UART_VIDS:
        return False
    return True


def _describe(p):
    sn = f" sn={p.serial_number}" if p.serial_number else ""
    vp = f" {p.vid:04x}:{p.pid:04x}" if p.vid is not None else ""
    return f"{p.device}{vp}{sn}  {p.description}"


def candidates():
    """Ports that could plausibly be the debug cable, best first."""
    ports = [p for p in serial.tools.list_ports.comports() if _usable(p)]
    ports.sort(key=lambda p: (p.vid not in UART_BRIDGE_VIDS, p.device))
    return ports


def arg(argv=None):
    """Pull --port/-p out of argv and return it, or None.

    Removes the flag so that scripts which do their own sys.argv handling --
    most of these tools predate having any -- do not then trip over it.
    """
    argv = sys.argv if argv is None else argv
    for flag in ("--port", "-p"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 >= len(argv):
                raise PortError(f"{flag} needs a value")
            val = argv[i + 1]
            del argv[i:i + 2]
            return val
    for i, a in enumerate(list(argv)):
        if a.startswith("--port="):
            del argv[i]
            return a.split("=", 1)[1]
    return None


def add_argument(parser):
    """For the tools that do use argparse."""
    parser.add_argument("-p", "--port", default=None,
                        help="debug UART device path, or a substring of its "
                             "serial number (default: $BF07_PORT, else "
                             "autodetect)")


def resolve(port=None):
    """Device path of the debug UART. Raises PortError if it cannot be sure."""
    want = port or arg() or os.environ.get("BF07_PORT")

    if want:
        if os.path.exists(want) or want.upper().startswith("COM"):
            return want
        # not a path -- treat it as a substring to match against
        hits = [p for p in serial.tools.list_ports.comports()
                if want.lower() in " ".join(filter(None, (
                    p.device, p.serial_number, p.description))).lower()]
        if len(hits) == 1:
            return hits[0].device
        if not hits:
            raise PortError(
                f"no serial port matches {want!r}. Present:\n  " +
                "\n  ".join(_describe(p)
                            for p in serial.tools.list_ports.comports()))
        raise PortError(f"{want!r} matches {len(hits)} ports:\n  " +
                        "\n  ".join(_describe(p) for p in hits))

    found = candidates()
    if len(found) == 1:
        return found[0].device
    if not found:
        raise PortError(
            "no USB serial adapter found. Is the debug cable plugged in?\n"
            "Ports present:\n  " +
            ("\n  ".join(_describe(p)
                         for p in serial.tools.list_ports.comports()) or
             "(none)"))
    raise PortError(
        f"{len(found)} USB serial adapters present -- pass --port (or set "
        f"$BF07_PORT) to say which:\n  " +
        "\n  ".join(_describe(p) for p in found))


def find(port=None):
    """Like resolve(), but returns None instead of raising.

    For the callers that treat a missing cable as a state to wait out rather
    than an error -- the ADFU hammer loops, and the tools where the UART is
    only one of two ways in.
    """
    try:
        return resolve(port)
    except PortError:
        return None


def open(port=None, baud=BAUD, timeout=0.4, **kw):
    """serial.Serial on the resolved port."""
    return serial.Serial(resolve(port), baud, timeout=timeout, **kw)


if __name__ == "__main__":
    all_ports = serial.tools.list_ports.comports()
    shown = [p for p in all_ports if p.vid is not None]
    for p in shown:
        print(f" {'*' if _usable(p) else ' '} {_describe(p)}")
    hidden = len(all_ports) - len(shown)
    if hidden:
        print(f"   ({hidden} built-in ports with no USB behind them, ignored)")
    print()
    try:
        print("resolves to:", resolve())
    except PortError as e:
        print("cannot resolve:", e)
