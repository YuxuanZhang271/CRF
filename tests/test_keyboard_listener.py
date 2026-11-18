"""Native shortcut filtering and focus leases; no OS keyboard events are generated."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from keyboard_listener import KeyboardListener


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
        for char in 'BPTR':
            self.assertTrue(listener.key(ord(char), True, True, True))
            self.assertTrue(listener.key(ord(char), True, True, True))
            self.assertTrue(listener.key(ord(char), False, False, True))
        self.assertEqual(listener.poll('panel', True)['commands'], ['bird-eye', 'pull-out', 'tracking', 'record'])
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

    def test_adding_ctrl_during_a_held_roll_never_swallows_the_motion_key_release(self):
        listener = KeyboardListener()
        listener.poll('panel', True)
        self.assertFalse(listener.key(ord('R'), True, False, True))
        self.assertFalse(listener.key(ord('R'), True, True, True))
        self.assertFalse(listener.key(ord('R'), False, True, True))
        self.assertEqual(listener.poll('panel', True)['commands'], [])


if __name__ == '__main__':
    unittest.main()
