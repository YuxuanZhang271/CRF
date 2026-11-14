"""Windows shared memory for the native Flex motor driver."""

import ctypes as ct
import math
import secrets
import struct
import time
from dataclasses import dataclass


PREFIX = r"Local\CRFFlexV1"
MAGIC, VERSION = 0x58454C46, 1
COMMAND = struct.Struct("<4I6Q2I3d")
STATUS = struct.Struct("<4I5Q4IiI8d")
VISION = struct.Struct("<4I4Q4I2diI")
assert (COMMAND.size, STATUS.size, VISION.size) == (96, 144, 88)

IDLE, STOP, CALIBRATE, SET_MODE, JOG, POSITION, TRACK, RETURN_ZERO = range(8)
FIXED, FREE = range(2)
CALIBRATED, SETUP, MOVING, SIMULATED, XBOX, SETUP_ENCODER_MISMATCH = (1, 2, 4, 8, 16, 32)
MANUAL_BENCH = 64
PLANNED_ZERO = 128
PLANNED_FULL = 256
NO_RESULT, ACCEPTED, DONE, REFUSED, CANCELED, LIMIT = range(6)
MODES = ("fixed", "free")
ACTIONS = ("idle", "stop", "calibrate", "set mode", "jog", "position", "track", "return to zero")
RESULTS = ("", "in progress", "complete", "refused", "canceled", "limit reached")
AXIS_FAULT_BITS = (
    (0x02, "drive alarm"),
    (0x10, "following error"),
    (0x20, "positive limit"),
    (0x40, "negative limit"),
    (0x80, "stop input"),
    (0x100, "emergency-stop input"),
)
FEEDBACK_TIMEOUT_MS = 250
VISION_TIMEOUT_S = 0.5


def _header(layout):
    return MAGIC, VERSION, layout.size, 0


@dataclass(frozen=True)
class FlexStatus:
    driver: int
    stamp: int
    app: int
    sequence: int
    request: int
    action: int
    mode: int
    flags: int
    result: int
    fault: int
    bend_x: float
    bend_y: float
    roll: float
    bend_limit: float
    tendons: tuple
    fault_axis_status: int = 0

    @property
    def fault_detail(self):
        if -104 <= self.fault <= -100:
            axis = -99 - self.fault
            flags = [label for bit, label in AXIS_FAULT_BITS if self.fault_axis_status & bit]
            detail = ", ".join(flags) or "check console for axis status"
            if self.fault_axis_status:
                detail += f" (0x{self.fault_axis_status:X})"
            return f"GTS axis {axis}: {detail}"
        return ""

    @property
    def calibrated(self):
        return bool(self.flags & CALIBRATED)

    @property
    def planned_feedback(self):
        return bool(self.flags & (MANUAL_BENCH | PLANNED_FULL))

    @property
    def positioning_ready(self):
        return self.calibrated or bool(self.flags & PLANNED_FULL and self.flags & PLANNED_ZERO)

    @property
    def setup(self):
        return bool(self.flags & SETUP)

    @property
    def moving(self):
        return bool(self.flags & MOVING)


class _Windows:
    def __init__(self):
        dll = ct.WinDLL("kernel32", use_last_error=True)
        self.create_mutex = self._bind(dll.CreateMutexW, ct.c_void_p, ct.c_void_p, ct.c_int, ct.c_wchar_p)
        self.open_mapping = self._bind(dll.OpenFileMappingW, ct.c_void_p, ct.c_ulong, ct.c_int, ct.c_wchar_p)
        self.open_mutex = self._bind(dll.OpenMutexW, ct.c_void_p, ct.c_ulong, ct.c_int, ct.c_wchar_p)
        self.map_view = self._bind(dll.MapViewOfFile, ct.c_void_p, ct.c_void_p, ct.c_ulong,
                                   ct.c_ulong, ct.c_ulong, ct.c_size_t)
        self.wait = self._bind(dll.WaitForSingleObject, ct.c_ulong, ct.c_void_p, ct.c_ulong)
        self.release = self._bind(dll.ReleaseMutex, ct.c_int, ct.c_void_p)
        self.close = self._bind(dll.CloseHandle, ct.c_int, ct.c_void_p)
        self.unmap = self._bind(dll.UnmapViewOfFile, ct.c_int, ct.c_void_p)
        self.now = self._bind(dll.GetTickCount64, ct.c_uint64)

    @staticmethod
    def _bind(function, result, *arguments):
        function.restype = result
        function.argtypes = arguments
        return function


class _Mapping:
    def __init__(self, api, name, size, writable):
        self.api, self.size = api, size
        access = 2 if writable else 4  # FILE_MAP_WRITE / FILE_MAP_READ
        self.file = api.open_mapping(access, False, name)
        if not self.file:
            raise ct.WinError(ct.get_last_error())
        self.mutex = api.open_mutex(0x100001, False, name + "Mutex")
        self.view = api.map_view(self.file, access, 0, 0, size) if self.mutex else None
        if not self.view:
            error = ct.WinError(ct.get_last_error())
            self.close()
            raise error

    def copy(self, data=None):
        acquired = self.api.wait(self.mutex, 2)
        if acquired == 0x80:  # WAIT_ABANDONED
            self.api.release(self.mutex)
            raise OSError("Flex mapping lock was abandoned")
        if acquired != 0:
            raise OSError("Flex mapping lock timed out")
        try:
            if data is None:
                return ct.string_at(self.view, self.size)
            ct.memmove(self.view, data, self.size)
        finally:
            self.api.release(self.mutex)

    def close(self):
        if self.view:
            self.api.unmap(self.view)
        if self.mutex:
            self.api.close(self.mutex)
        self.api.close(self.file)


class FlexClient:
    """Called under the control node's control lock."""

    def __init__(self):
        self._api = _Windows()
        self._owner = self._api.create_mutex(None, False, PREFIX + "Application")
        error = ct.get_last_error()
        if not self._owner:
            raise ct.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            self._api.close(self._owner)
            raise RuntimeError("A Flex control application is already running in this session.")
        self._maps = {}
        self._retry_at = 0.0
        self.app = secrets.randbits(64) or 1
        self.status = None
        self.sequence = 0
        self._request_counter = 0
        self._intent = (IDLE, FIXED, 0, 0, 0., 0., 0.)
        self._vision_frame = 0

    @property
    def ready(self):
        return self.status is not None and self.status.app == self.app

    def close(self):
        self._detach()
        if self._owner:
            self._api.close(self._owner)
            self._owner = None

    def _detach(self):
        for mapping in self._maps.values():
            mapping.close()
        self._maps.clear()
        self.status = None
        self._retry_at = time.monotonic() + 0.3
        self._vision_frame = 0
        self.command(IDLE)

    def poll(self):
        if not self._maps and time.monotonic() < self._retry_at:
            return None
        try:
            if not self._maps:
                for name, layout in (("Command", COMMAND), ("Status", STATUS), ("Vision", VISION)):
                    self._maps[name] = _Mapping(self._api, PREFIX + name, layout.size, name != "Status")
            fields = STATUS.unpack(self._maps["Status"].copy())
            if fields[:4] != _header(STATUS):
                raise OSError("Invalid Flex status layout")
            driver, stamp, app, seq, req, action, mode, flags, result, fault, axis_status, *pose = fields[4:]
            if (not driver or not stamp or not 0 <= self._api.now() - stamp <= FEEDBACK_TIMEOUT_MS
                    or action >= len(ACTIONS) or mode >= len(MODES) or result >= len(RESULTS)
                    or not all(map(math.isfinite, pose)) or pose[3] < 0
                    or (pose[3] == 0 and flags & (CALIBRATED | PLANNED_ZERO))):
                raise OSError("Invalid or stale Flex status")
            if self.status is None or driver != self.status.driver or (self.ready and app != self.app):
                self._vision_frame = 0
                self.command(IDLE)
            self.status = FlexStatus(driver, stamp, app, seq, req, action, mode, flags, result, fault,
                                     *pose[:4], tuple(pose[4:]), axis_status)
            return self.status
        except OSError:
            self._detach()
            return None

    def command(self, action, *, mode=FIXED, request=0, frame=0, x=0., y=0., roll=0.):
        intent = (action, mode, request, frame, x, y, roll)
        if intent != self._intent or not self.sequence:
            self.sequence += 1
            self._intent = intent

    def request(self, action, **parameters):
        self._request_counter += 1
        self.command(action, request=self._request_counter, **parameters)
        return self._request_counter

    def send(self, observation=None):
        if self.status is None:
            return False
        tick = self._api.now()
        action, mode, request, frame, x, y, roll = self._intent
        try:
            if observation is not None and observation.frame != self._vision_frame:
                age = max(0., time.monotonic() - observation.captured_at)
                capture = max(0, tick - round(age * 1000))
                data = VISION.pack(*_header(VISION), self.status.driver, self.app,
                                   observation.frame, capture, int(observation.valid),
                                   observation.width, observation.height, int(observation.centered),
                                   observation.dx, observation.dy,
                                   observation.category if observation.category is not None else -1,
                                   observation.revision)
                self._maps["Vision"].copy(data)
                self._vision_frame = observation.frame
            data = COMMAND.pack(*_header(COMMAND), self.status.driver, self.app, self.sequence,
                                tick, request, frame, action, mode, x, y, roll)
            self._maps["Command"].copy(data)
            return True
        except OSError:
            self._detach()
            return False
