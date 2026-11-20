"""Deliver browser-reserved Alt shortcuts to the focused control panel on Windows.

Only B/P/T/R/M with Alt are intercepted, while a dashboard owns a fresh focus
lease. The hook never moves hardware: the browser consumes queued button clicks.
Captured Alt chords include a no-action key event so releasing Alt does not
activate the browser menu after the command letter has been suppressed.
Held Freedrive (Alt+Shift+F) and zoom use browser key events for immediate release.
Win32 reference: https://learn.microsoft.com/windows/win32/winmsg/lowlevelkeyboardproc
"""

import ctypes as ct
import logging
import os
import threading
import time
from collections import deque

COMMANDS = {ord('B'): 'bird-eye', ord('P'): 'pull-out', ord('T'): 'tracking',
            ord('R'): 'record', ord('M'): 'arm-mode'}
DASHBOARD_TITLE = 'ABML General Surgical Platform'
FOCUS_LEASE_S = 1.
MENU_MASK_KEY = 0xE8  # Unassigned VK: no text, modifier changes or app commands.


class _KeyboardInput(ct.Structure):
    _fields_ = [('vk', ct.c_uint16), ('scan', ct.c_uint16), ('flags', ct.c_uint32),
                ('time', ct.c_uint32), ('extra', ct.c_size_t)]


class _MouseInput(ct.Structure):
    _fields_ = [('x', ct.c_int32), ('y', ct.c_int32), ('data', ct.c_uint32),
                ('flags', ct.c_uint32), ('time', ct.c_uint32), ('extra', ct.c_size_t)]


class _InputData(ct.Union):
    # Include MOUSEINPUT to preserve the full Windows INPUT union size/alignment.
    _fields_ = [('keyboard', _KeyboardInput), ('mouse', _MouseInput)]


class _Input(ct.Structure):
    _fields_ = [('type', ct.c_uint32), ('data', _InputData)]


def mask_alt_menu(send_input):
    """Mark a captured Alt chord as used without sending another shortcut.

    The command letter never reaches the foreground window. Without an
    intervening key, Windows treats the remaining Alt down/up as menu input.
    Send a balanced unassigned key pair; never swallow Alt-up or refocus apps.
    """
    events = (_Input * 2)()
    for event in events:
        event.type = 1  # INPUT_KEYBOARD
        event.data.keyboard.vk = MENU_MASK_KEY
    events[1].data.keyboard.flags = 2  # KEYEVENTF_KEYUP
    return send_input(2, events, ct.sizeof(_Input)) == 2


class KeyboardListener:
    def __init__(self):
        self._lock = threading.Lock()
        self._owner = None
        self._lease = 0.
        self._commands = deque(maxlen=16)
        self._pressed = set()
        self._down = set()
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self.available = False

    def poll(self, client, enabled):
        with self._lock:
            now = time.monotonic()
            if enabled:
                if client != self._owner or now >= self._lease:
                    self._commands.clear()
                self._owner, self._lease = client, now + FOCUS_LEASE_S
            elif client == self._owner:
                self._lease = 0.
                self._commands.clear()
            commands = list(self._commands) if enabled and client == self._owner else []
            if commands:
                self._commands.clear()
            return {'available': self.available, 'commands': commands}

    def key(self, code, down, alt, foreground, *, mask_menu=None):
        """Fast, testable hook logic; repeats never become a second command."""
        if code not in COMMANDS:
            return False
        with self._lock:
            if not down:
                self._down.discard(code)
                if code in self._pressed:
                    self._pressed.remove(code)
                    return True
                return False
            if code in self._down:
                return code in self._pressed
            self._down.add(code)
            if not alt or not foreground or time.monotonic() >= self._lease:
                return False
            self._pressed.add(code)
            self._commands.append(COMMANDS[code])
        # Outside the state lock: injected events can re-enter the OS hook.
        # Mask once for each captured press, never for repeats or unrelated keys.
        if mask_menu is not None:
            mask_menu()
        return True

    def start(self):
        if os.name == 'nt':
            self._thread = threading.Thread(target=self._run, name='dashboard-shortcuts', daemon=True)
            self._thread.start()
            self._ready.wait(2.)
        return self

    def close(self):
        if self._thread_id is not None:
            user = ct.WinDLL('user32', use_last_error=True)
            post = user.PostThreadMessageW
            post.argtypes = [ct.c_ulong, ct.c_uint, ct.c_size_t, ct.c_ssize_t]
            post.restype = ct.c_int
            post(self._thread_id, 0x0012, 0, 0)  # WM_QUIT, to this thread's message queue.
        if self._thread is not None:
            self._thread.join(timeout=2.)

    def _run(self):
        from ctypes import wintypes as wt

        user, kernel = ct.WinDLL('user32', use_last_error=True), ct.WinDLL('kernel32', use_last_error=True)
        callback_type = ct.WINFUNCTYPE(ct.c_ssize_t, ct.c_int, wt.WPARAM, wt.LPARAM)

        class Key(ct.Structure):
            _fields_ = [('vkCode', wt.DWORD), ('scanCode', wt.DWORD), ('flags', wt.DWORD),
                        ('time', wt.DWORD), ('extra', ct.c_size_t)]

        def bind(dll, name, result, *args):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = result, list(args)
            return fn

        install = bind(user, 'SetWindowsHookExW', ct.c_void_p, ct.c_int, callback_type, wt.HINSTANCE, wt.DWORD)
        remove = bind(user, 'UnhookWindowsHookEx', wt.BOOL, ct.c_void_p)
        next_hook = bind(user, 'CallNextHookEx', ct.c_ssize_t, ct.c_void_p, ct.c_int, wt.WPARAM, wt.LPARAM)
        foreground = bind(user, 'GetForegroundWindow', wt.HWND)
        window_text = bind(user, 'GetWindowTextW', ct.c_int, wt.HWND, wt.LPWSTR, ct.c_int)
        key_state = bind(user, 'GetAsyncKeyState', ct.c_short, ct.c_int)
        send_input = bind(user, 'SendInput', wt.UINT, wt.UINT, ct.POINTER(_Input), ct.c_int)
        module = bind(kernel, 'GetModuleHandleW', wt.HMODULE, wt.LPCWSTR)
        thread_id = bind(kernel, 'GetCurrentThreadId', wt.DWORD)
        get_message = bind(user, 'GetMessageW', ct.c_int, ct.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
        peek = bind(user, 'PeekMessageW', wt.BOOL, ct.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
        hook = None
        mask_warning_reported = False

        def suppress_menu():
            nonlocal mask_warning_reported
            if not mask_alt_menu(send_input) and not mask_warning_reported:
                mask_warning_reported = True
                logging.getLogger(__name__).warning('Could not suppress the browser Alt menu for a control shortcut.')

        @callback_type
        def callback(code, message, pointer):
            if code >= 0 and message in (0x100, 0x104, 0x101, 0x105):
                key = ct.cast(pointer, ct.POINTER(Key)).contents
                if key.vkCode in COMMANDS:
                    title = ct.create_unicode_buffer(512)
                    window_text(foreground(), title, len(title))
                    focused = title.value.startswith(DASHBOARD_TITLE)
                    # LLKHF_ALTDOWN is bit 5 of the hook event's flags.
                    alt = bool(key.flags & 0x20) and not any(
                        key_state(vk) & 0x8000 for vk in (0x11, 0x10, 0x5B, 0x5C))
                    if self.key(key.vkCode, message in (0x100, 0x104), alt, focused,
                                mask_menu=suppress_menu):
                        return 1
            return next_hook(None, code, message, pointer)

        try:
            message = wt.MSG()
            peek(ct.byref(message), None, 0, 0, 0)  # Ensure the thread queue exists before close().
            self._thread_id = thread_id()
            hook = install(13, callback, module(None), 0)  # WH_KEYBOARD_LL
            if not hook:
                raise ct.WinError(ct.get_last_error())
            self.available = True
            self._ready.set()
            while get_message(ct.byref(message), None, 0, 0) > 0:
                pass  # Low-level callbacks are delivered while retrieving messages.
        except OSError as error:
            logging.getLogger(__name__).warning('Native dashboard shortcuts unavailable: %s', error)
        finally:
            self.available = False
            self._ready.set()
            if hook:
                remove(hook)
