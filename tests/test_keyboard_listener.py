"""Native shortcut filtering and focus leases; no OS keyboard events are generated."""

import sys
import ctypes as ct
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from keyboard_listener import DASHBOARD_TITLE, MENU_MASK_KEY, KeyboardListener, _Input, mask_alt_menu


class KeyboardListenerTests(unittest.TestCase):
    def test_only_focused_control_shortcuts_are_queued_once(self):
        listener = KeyboardListener()
        self.assertFalse(listener.key(ord('R'), True, True, True))
        listener.key(ord('R'), False, False, True)
        listener.poll('panel', True)
        self.assertFalse(listener.key(ord('R'), True, True, False))
        listener.key(ord('R'), False, False, False)
        self.assertFalse(listener.key(ord('R'), True, False, True))
        listener.key(ord('R'), False, False, True)
        self.assertFalse(listener.key(ord('W'), True, True, True))
        for char in 'BPTRM':
            self.assertTrue(listener.key(ord(char), True, True, True))
            self.assertTrue(listener.key(ord(char), True, True, True))
            self.assertTrue(listener.key(ord(char), False, False, True))
        self.assertEqual(listener.poll('panel', True)['commands'], ['bird-eye', 'pull-out', 'tracking', 'record', 'arm-mode'])
        self.assertEqual(listener.poll('panel', True)['commands'], [])

    def test_focus_changes_and_expired_leases_discard_commands(self):
        listener = KeyboardListener()
        with patch('keyboard_listener.time.monotonic', return_value=10.):
            listener.poll('first', True)
            listener.key(ord('T'), True, True, True)
            self.assertEqual(listener.poll('second', False)['commands'], [])
            self.assertEqual(listener.poll('second', True)['commands'], [])
        with patch('keyboard_listener.time.monotonic', return_value=20.):
            self.assertFalse(listener.key(ord('P'), True, True, True))
            self.assertEqual(listener.poll('second', True)['commands'], [])

    def test_adding_alt_during_a_held_roll_never_swallows_the_motion_key_release(self):
        listener = KeyboardListener()
        listener.poll('panel', True)
        self.assertFalse(listener.key(ord('R'), True, False, True))
        self.assertFalse(listener.key(ord('R'), True, True, True))
        self.assertFalse(listener.key(ord('R'), False, True, True))
        self.assertEqual(listener.poll('panel', True)['commands'], [])

    def test_title_matches_dashboard_and_held_freedrive_stays_in_browser(self):
        html = (Path(__file__).resolve().parents[1] / 'src/static/index.html').read_text(encoding='utf-8')
        self.assertIn(f'<title>{DASHBOARD_TITLE}</title>', html)
        listener = KeyboardListener()
        listener.poll('panel', True)
        self.assertFalse(listener.key(ord('F'), True, True, True))
        self.assertFalse(listener.key(ord('F'), False, True, True))
        self.assertEqual(listener.poll('panel', True)['commands'], [])

    def test_captured_alt_chords_mask_menu_once_without_swallowing_alt_release(self):
        listener = KeyboardListener()
        mask = Mock()
        listener.poll('panel', True)
        for char in 'MBPTR':
            mask.reset_mock()
            self.assertTrue(listener.key(ord(char), True, True, True, mask_menu=mask))
            self.assertTrue(listener.key(ord(char), True, True, True, mask_menu=mask))
            self.assertTrue(listener.key(ord(char), False, True, True, mask_menu=mask))
            mask.assert_called_once_with()
        for code in (0x12, 0xa4, 0xa5):  # Alt, left Alt, right Alt
            mask.reset_mock()
            self.assertFalse(listener.key(code, False, False, True, mask_menu=mask))
            mask.assert_not_called()

    def test_menu_mask_never_runs_for_editing_other_apps_or_unrelated_keys(self):
        for focused, enabled, alt, code in ((False, True, True, ord('M')),
                (True, False, True, ord('M')), (True, True, False, ord('M')),
                (True, True, True, ord('F')), (True, True, True, MENU_MASK_KEY)):
            listener = KeyboardListener()
            listener.poll('panel', enabled)
            mask = Mock()
            self.assertFalse(listener.key(code, True, alt, focused, mask_menu=mask))
            mask.assert_not_called()
            self.assertEqual(listener.poll('panel', enabled)['commands'], [])

    def test_menu_mask_is_balanced_unassigned_input_with_native_windows_layout(self):
        send = Mock(return_value=2)
        self.assertTrue(mask_alt_menu(send))
        count, events, size = send.call_args.args
        self.assertEqual(count, 2)
        self.assertEqual(size, 40 if ct.sizeof(ct.c_void_p) == 8 else 28)
        self.assertEqual(size, ct.sizeof(_Input))
        self.assertEqual([event.type for event in events], [1, 1])
        self.assertEqual([event.data.keyboard.vk for event in events], [MENU_MASK_KEY] * 2)
        self.assertEqual([event.data.keyboard.flags for event in events], [0, 2])
        self.assertTrue(all(event.data.keyboard.scan == 0 and event.data.keyboard.extra == 0
                            for event in events))
        send.return_value = 0
        self.assertFalse(mask_alt_menu(send))


if __name__ == '__main__':
    unittest.main()
