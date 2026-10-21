"""DAREU A950 on its Compx 2.4 GHz receiver, over USB/HID, without vendor software.

Protocol from vamyane/MousePower (MIT), src/mousepower/core/providers/dareu_compx.py at
commit 684b5aa70e36952bac1cda094291c2ccd39c1e34, which lists 260d:1074 and 260d:1084. It is the Compx frame providers/pulsar.py sends -
17 bytes, report id 0x08, command 0x04, checksum 0x55 minus the sum of bytes 0..15 - but
the receiver takes it as a FEATURE report on the ff02:0002 collection and answers with
an INPUT report on ff01:0000, under header 0x09:

    [0] 0x09  [1] command  [2] status (0 = data, 1 = empty ack)  [5] data length
    [6] level 0..100  [7] charging  [16] checksum, same rule as the request

MousePower warns that report 0x06 on ff04 always reads 100 three times: it is not used.

Read on hardware on 260d:1074 (A950, 2.4 GHz): 09 04 00 00 00 02 5a 00 .. ec = 90 %,
on battery. 260d:1084 is claimed from MousePower only.

A sleeping or switched-off mouse does not answer while its receiver stays plugged in
(the two cannot be told apart). Its last reading is then kept greyed out for
ASLEEP_KEEP seconds, as in the Logitech, WLmouse and LAMZU providers, and then dropped,
so the icon leaves the tray; a receiver that leaves the USB tree drops it at once.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

import hid

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log
from .pulsar import CMD_POWER, LEVEL_INDEX, PAYLOAD_LEN, POWER_INDEX, checksum, make_request

VID = 0x260D
PIDS = {
    0x1074: "DAREU A950 (2.4 GHz)",
    0x1084: "DAREU mouse (2.4 GHz)",
}
COMMAND_USAGE = (0xFF02, 0x0002)   # takes the request as a feature report
REPLY_USAGE = (0xFF01, 0x0000)     # sends the answer as an input report
REPLY_HEADER = 0x09
STATUS_DATA = 0x00
ASLEEP_KEEP = 300   # s a silent mouse stays greyed, as in the Logitech, WLmouse and LAMZU providers

READ_ATTEMPTS = 4
READ_TIMEOUT_MS = 250
FLUSH_TIMEOUT_MS = 30


def parse_power(r) -> Optional[Tuple[int, bool]]:
    """(percent, charging) from a power reply, or None when this is not one."""
    if not r or len(r) < PAYLOAD_LEN:
        return None
    frame = list(r[:PAYLOAD_LEN])
    if frame[0] != REPLY_HEADER or frame[1] != CMD_POWER or frame[2] != STATUS_DATA:
        return None
    if frame[PAYLOAD_LEN - 1] != checksum(frame[:PAYLOAD_LEN - 1]):
        return None
    level = frame[LEVEL_INDEX]
    if not 0 <= level <= 100:
        return None
    return level, frame[POWER_INDEX] != 0


class DareuProvider(Provider):
    name = "dareu"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Dict[str, Tuple[str, int, bool, float]] = {}   # key -> (name, level, charging, seen)

    def _query(self, command_path: bytes, reply_path: bytes) -> Optional[Tuple[int, bool]]:
        reply, command = hid.device(), hid.device()
        try:
            reply.open_path(reply_path)
            command.open_path(command_path)
            for _ in range(4):           # drop anything the receiver pushed earlier
                if not reply.read(PAYLOAD_LEN, FLUSH_TIMEOUT_MS):
                    break
            command.send_feature_report(make_request())
            for attempt in range(READ_ATTEMPTS):
                r = reply.read(PAYLOAD_LEN, READ_TIMEOUT_MS)
                if not r:
                    break
                self._diag.append(f"  reply {attempt + 1}: {hexdump(r)}")
                parsed = parse_power(r)
                if parsed is not None:
                    return parsed
            self._diag.append("  no power reply (the mouse may be asleep or off)")
            return None
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"  query error: {e}")
            return None
        finally:
            for dev in (reply, command):
                try:
                    dev.close()
                except Exception:
                    pass

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        try:
            infos = hidlist.enumerate(VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(%04x): %s", VID, e)
            return []
        out = []
        for pid, name in PIDS.items():
            paths = {(d.get("usage_page"), d.get("usage")): d["path"]
                     for d in infos if d["product_id"] == pid}
            if not paths:
                continue
            key = f"dareu:{VID:04x}{pid:04x}"
            self._diag.append(f"[DAREU] pid={VID:04x}:{pid:04x} '{name}'")
            if COMMAND_USAGE not in paths or REPLY_USAGE not in paths:
                self._diag.append("  missing the ff02:0002 or ff01:0000 collection")
                continue
            parsed = self._query(paths[COMMAND_USAGE], paths[REPLY_USAGE])
            if parsed is not None:
                level, charging = parsed
                self._last[key] = (name, level, charging, time.time())
                out.append(DeviceStatus(key, name, level, charging, True, "dareu", kind="mouse"))
            elif key in self._last and time.time() - self._last[key][3] < ASLEEP_KEEP:
                _, level, charging, _ = self._last[key]
                self._diag.append(f"  keeping {level}% greyed for up to {ASLEEP_KEEP} s")
                out.append(DeviceStatus(key, name, level, charging, False, "dareu",
                                        kind="mouse", wake_on_input=True))
        # a receiver gone from the USB tree, or a mouse silent for ASLEEP_KEEP, drops the reading
        for key in set(self._last) - {s.key for s in out}:
            del self._last[key]
        return out

    def diagnostics(self) -> List[str]:
        return list(self._diag)
