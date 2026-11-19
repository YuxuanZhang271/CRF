"""Windows shared memory for the native Flex motor driver."""

import ctypes as ct
import logging
import math
import secrets
import struct
import threading
import time
from dataclasses import dataclass


PREFIX = r"Local\CRFFlexV5"
APPLICATION_MUTEX = r"Local\CRFFlexV1Application"
MAGIC, VERSION = 0x58454C46, 5
COMMAND = struct.Struct("<4I6Q2I3d")
STATUS = struct.Struct("<4I5Q4IiI9dIi")
VISION = struct.Struct("<4I4Q4I2diI")
assert (COMMAND.size, STATUS.size, VISION.size) == (96, 160, 88)

(IDLE, STOP, CALIBRATE, HORIZON, JOG, POSITION, TRACK, RETURN_ZERO,
 RECHECK_STARTUP, ROLL_JOG, SET_BEND_LIMIT, SET_SPEED_LIMIT,
 WATER_SPRAY, GAS_SPRAY, CLEAN_LENS) = range(15)
WATER_SPRAY_READY, GAS_SPRAY_READY = 131072, 262144
SPRAY_PHASES = ("idle", "water", "gas", "cleaning_water", "waiting_water_stop", "cleaning_gas", "stopping")
SPRAY_ERRORS = {-203: "Water pump failed to start.", -204: "Water stop command failed.",
                -205: "Pump stop confirmation timed out.", -206: "Gas serial command failed.",
                -207: "Water pump axis fault or status unavailable."}
CALIBRATED, MOVING, SIMULATED, SETUP_ENCODER_MISMATCH = (1, 4, 8, 32)
MANUAL_BENCH = 64
PLANNED_ZERO = 128
PLANNED_FULL = 256
STARTUP_RECHECK = 512
ROLL_JOG_SUPPORTED = 1024
BEND_LIMIT_SUPPORTED = 2048
TRACK_HORIZON_SUPPORTED = 4096
WIDE_BEND_RANGE = 8192
AT_SAVED_ZERO = 16384
COUPLED_ROLL_SUPPORTED = 32768
COMBINED_LIMITS_SUPPORTED = 65536
NO_RESULT, ACCEPTED, DONE, REFUSED, CANCELED, LIMIT = range(6)
ACTIONS = ("idle", "stop", "calibrate", "horizon", "jog", "position", "track", "return to zero",
           "recheck startup", "roll jog", "set bend limit", "set speed limit",
           "water spray", "gas spray", "clean lens")
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
DEFAULT_SPEED_DEG_S = 10.
SPEED_PRESETS_DEG_S = tuple(DEFAULT_SPEED_DEG_S * factor for factor in (0.5, 1., 2., 5.))
MIN_SPEED_DEG_S = 0.1


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
    flags: int
    result: int
    fault: int
    bend_x: float
    bend_y: float
    roll: float
    bend_limit: float
    tendons: tuple
    fault_axis_status: int = 0
    speed_limit: float = DEFAULT_SPEED_DEG_S
    spray_phase: int = 0
    spray_error: int = 0

    @property
    def fault_detail(self):
        if -104 <= self.fault <= -100:
            axis = -99 - self.fault
            flags = [label for bit, label in AXIS_FAULT_BITS if self.fault_axis_status & bit]
            detail = ", ".join(flags) or "check console for axis status"
            if self.fault_axis_status:
                detail += f" (0x{self.fault_axis_status:X})"
            return f"GTS axis {axis}: {detail}"
        return {
            -111: "Flex driver: target exceeds the controller position range",
            -112: "Flex driver: planned motion did not settle at its target; see terminal details",
            -113: "Flex driver: encoder did not follow commanded travel; see terminal details",
            -114: "Flex driver: drive enable was not confirmed",
        }.get(self.fault, "")

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


class _MappingBusy(OSError):
    pass


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
        if acquired == 0x102:  # WAIT_TIMEOUT
            raise _MappingBusy("Flex mapping lock timed out")
        if acquired != 0:
            raise ct.WinError(ct.get_last_error())
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
    """Keep native I/O on one worker; publish intent from the coordinator.

    The worker repeats the coordinator's timestamp, so stalled control work
    cannot keep an old movement alive past the native command watchdog.
    """

    def __init__(self):
        self._api = _Windows()
        self._owner = self._api.create_mutex(None, False, APPLICATION_MUTEX)
        error = ct.get_last_error()
        if not self._owner:
            raise ct.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            self._api.close(self._owner)
            raise RuntimeError("A Flex control application is already running in this session.")
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._worker = None
        self._maps = {}
        self._retry_at = 0.0
        self.app = secrets.randbits(64) or 1
        self.status = None  # Immutable feedback latched by poll() for a control tick.
        self._feedback = None
        self.sequence = 0
        self._request_counter = 0
        self._intent = (IDLE, 0, 0, 0., 0., 0.)
        self._published = (0, self._intent, 0.)
        self._observation = None
        self._vision_frame = 0

    @property
    def ready(self):
        return self.status is not None and self.status.app == self.app

    def start(self):
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="flex-ipc")
            self._worker.start()

    def close(self):
        self._shutdown.set()
        self._wake.set()
        if self._worker is not None:
            self._worker.join()
        self._detach()
        self.status = None
        if self._owner:
            self._api.close(self._owner)
            self._owner = None

    def _detach(self):
        # Only the I/O worker touches mappings until it has joined.
        for mapping in self._maps.values():
            mapping.close()
        self._maps.clear()
        self._retry_at = time.monotonic() + 0.3
        self._vision_frame = 0
        with self._lock:
            self._feedback = None
            self.command(IDLE)
            self._published = (self.sequence, self._intent, 0.)
            self._observation = None

    def poll(self):
        """Latch a complete snapshot without waiting for a Windows mutex."""
        with self._lock:
            status = self._feedback
        self.status = (status if status is not None
                       and 0 <= self._api.now() - status.stamp <= FEEDBACK_TIMEOUT_MS else None)
        return self.status

    def _read_status(self):
        if not self._maps and time.monotonic() < self._retry_at:
            return None
        if not self._maps:
            for name, layout in (("Command", COMMAND), ("Status", STATUS), ("Vision", VISION)):
                self._maps[name] = _Mapping(self._api, PREFIX + name, layout.size, name != "Status")
        try:
            fields = STATUS.unpack(self._maps["Status"].copy())
        except _MappingBusy:
            status = self._feedback
            if status and 0 <= self._api.now() - status.stamp <= FEEDBACK_TIMEOUT_MS:
                return status
            raise
        if fields[:4] != _header(STATUS):
            raise OSError("Invalid Flex status layout")
        driver, stamp, app, seq, req, action, reserved, flags, result, fault, axis_status, *pose = fields[4:]
        if (not driver or not stamp or not 0 <= self._api.now() - stamp <= FEEDBACK_TIMEOUT_MS
                or action >= len(ACTIONS) or reserved or result >= len(RESULTS)
                or not 0 <= pose[9] < len(SPRAY_PHASES)
                or not all(map(math.isfinite, pose)) or pose[3] < 0 or pose[8] < MIN_SPEED_DEG_S
                or (pose[3] == 0 and flags & (CALIBRATED | PLANNED_ZERO))):
            raise OSError("Invalid or stale Flex status")
        return FlexStatus(driver, stamp, app, seq, req, action, flags, result, fault,
                          *pose[:4], tuple(pose[4:8]), axis_status, pose[8], pose[9], pose[10])

    def command(self, action, *, request=0, frame=0, x=0., y=0., roll=0.):
        with self._lock:
            intent = (action, request, frame, x, y, roll)
            if intent != self._intent or not self.sequence:
                self.sequence += 1
                self._intent = intent

    def request(self, action, **parameters):
        with self._lock:
            self._request_counter += 1
            self.command(action, request=self._request_counter, **parameters)
            return self._request_counter

    def send(self, observation=None):
        """Publish the latest intent; native status confirms actual completion."""
        with self._lock:
            self._published = (self.sequence, self._intent, time.monotonic())
            self._observation = observation
            available = self._feedback is not None
        self._wake.set()
        return available

    def _write(self, status):
        with self._lock:
            sequence, intent, published_at = self._published
            observation = self._observation
        if not published_at:
            return
        tick = self._api.now()
        now = time.monotonic()
        # Repeating a heartbeat never renews intent that the coordinator stopped publishing.
        stamp = max(0, tick - round(max(0., now - published_at) * 1000))
        action, request, frame, x, y, roll = intent
        if observation is not None and observation.frame != self._vision_frame:
            capture = max(0, tick - round(max(0., now - observation.captured_at) * 1000))
            data = VISION.pack(*_header(VISION), status.driver, self.app,
                               observation.frame, capture, int(observation.valid),
                               observation.width, observation.height, int(observation.centered),
                               observation.dx, observation.dy,
                               observation.category if observation.category is not None else -1,
                               observation.revision)
            try:
                self._maps["Vision"].copy(data)
            except _MappingBusy:
                pass  # Retry vision without delaying the command heartbeat.
            else:
                self._vision_frame = observation.frame
        data = COMMAND.pack(*_header(COMMAND), status.driver, self.app, sequence,
                            stamp, request, frame, action, 0, x, y, roll)
        self._maps["Command"].copy(data)

    def _run(self):
        try:
            while not self._shutdown.is_set():
                self._wake.clear()
                try:
                    status = self._read_status()
                    if status is not None:
                        with self._lock:
                            old = self._feedback
                            if (old is None or status.driver != old.driver
                                    or (old.app == self.app and status.app != self.app)):
                                self._vision_frame = 0
                                self.command(IDLE)
                                self._published = (self.sequence, self._intent, 0.)
                                self._observation = None
                            self._feedback = status
                        self._write(status)
                except _MappingBusy:
                    pass  # A single busy copy is not a lost connection.
                except OSError as error:
                    if self._feedback is not None:
                        logging.getLogger(__name__).warning("Flex shared memory unavailable: %s", error)
                    self._detach()
                self._wake.wait(0.01)
        finally:
            # Let the last queued STOP reach the driver before closing the maps.
            if self._feedback is not None and self._published[1][0] == STOP:
                try:
                    self._write(self._feedback)
                except OSError:
                    pass  # Native heartbeat expiry remains the fallback stop.
