"""Deliver browser-reserved Ctrl shortcuts to the focused control panel on Windows.

Only B/P/T/R with Ctrl are intercepted, while a dashboard owns a fresh focus
lease. The hook never moves hardware: the browser consumes queued button clicks.
Win32 reference: https://learn.microsoft.com/windows/win32/winmsg/lowlevelkeyboardproc
"""

import ctypes as ct
import logging
import os
import threading
import time
from collections import deque

COMMANDS = {ord('B'): 'bird-eye', ord('P'): 'pull-out', ord('T'): 'tracking', ord('R'): 'record'}
FOCUS_LEASE_S = 1.


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

    def key(self, code, down, ctrl, foreground):
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
            if code not in COMMANDS or not ctrl or not foreground or time.monotonic() >= self._lease:
                return False
            self._pressed.add(code)
            self._commands.append(COMMANDS[code])
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
        module = bind(kernel, 'GetModuleHandleW', wt.HMODULE, wt.LPCWSTR)
        thread_id = bind(kernel, 'GetCurrentThreadId', wt.DWORD)
        get_message = bind(user, 'GetMessageW', ct.c_int, ct.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
        peek = bind(user, 'PeekMessageW', wt.BOOL, ct.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
        hook = None

        @callback_type
        def callback(code, message, pointer):
            if code >= 0 and message in (0x100, 0x104, 0x101, 0x105):
                key = ct.cast(pointer, ct.POINTER(Key)).contents
                if key.vkCode in COMMANDS:
                    title = ct.create_unicode_buffer(512)
                    window_text(foreground(), title, len(title))
                    focused = title.value.startswith('CRF Control')
                    ctrl = bool(key_state(0x11) & 0x8000) and not any(
                        key_state(vk) & 0x8000 for vk in (0x12, 0x10, 0x5B, 0x5C))
                    if self.key(key.vkCode, message in (0x100, 0x104), ctrl, focused):
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
