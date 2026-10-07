import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mount_medic.authorization import Authorizer
from mount_medic.model import MedicError
from mount_medic.protocol import authorization_hours, validate


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        repository = SimpleNamespace(
            Gio=SimpleNamespace(DBusCallFlags=SimpleNamespace(NONE=0)),
            GLib=SimpleNamespace(Variant=lambda signature, value: value, VariantType=lambda signature: signature))
        modules = patch.dict(sys.modules, {"gi.repository": repository})
        modules.start()
        self.addCleanup(modules.stop)
        self.now = 100
        clock = patch("mount_medic.authorization.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.bus = Mock()
        self.bus.call_sync.return_value.unpack.return_value = ((True, False, {}),)
        self.authorizer = Authorizer(self.bus)
        self.authorizer.user_id = Mock(return_value=1000)
        self.request = {"op": "repair", "auth_hours": 1}

    def test_approval_expires_without_sliding_or_following_preference_changes(self):
        self.authorizer.authorize(":1.20", self.request)
        self.now += 3599
        self.authorizer.authorize(":1.20", {**self.request, "auth_hours": 24})
        self.assertEqual(self.bus.call_sync.call_count, 1)
        self.now += 1
        self.bus.call_sync.return_value.unpack.return_value = ((False, True, {}),)
        with self.assertRaisesRegex(MedicError, "denied or cancelled"):
            self.authorizer.authorize(":1.20", self.request)
        self.assertFalse(self.authorizer.active())

    def test_maximum_duration_and_expired_idle_cleanup(self):
        self.authorizer.authorize(":1.20", {**self.request, "auth_hours": 24})
        self.now += 24 * 3600 - 1
        self.assertTrue(self.authorizer.active())
        self.now += 1
        self.assertFalse(self.authorizer.active())

    def test_cancellation_creates_no_approval(self):
        self.bus.call_sync.return_value.unpack.return_value = ((False, True, {}),)
        with self.assertRaises(MedicError):
            self.authorizer.authorize(":1.20", self.request)
        self.assertFalse(self.authorizer.active())

    def test_other_connection_cannot_borrow_approval(self):
        self.authorizer.authorize(":1.20", self.request)
        self.bus.call_sync.return_value.unpack.return_value = ((False, True, {}),)
        with self.assertRaises(MedicError):
            self.authorizer.authorize(":1.21", self.request)
        self.assertEqual(self.authorizer.authorize(":1.20", self.request), 1000)

    def test_other_uid_cannot_borrow_approval(self):
        self.authorizer.authorize(":1.20", self.request)
        self.authorizer.user_id.return_value = 1001
        self.bus.call_sync.return_value.unpack.return_value = ((False, True, {}),)
        with self.assertRaises(MedicError):
            self.authorizer.authorize(":1.20", self.request)

    def test_forgetting_only_revokes_the_callers_approval(self):
        self.authorizer.authorize(":1.20", self.request)
        self.authorizer.authorize(":1.21", self.request)
        self.authorizer.forget(":1.20")
        self.bus.call_sync.return_value.unpack.return_value = ((False, True, {}),)
        with self.assertRaises(MedicError):
            self.authorizer.authorize(":1.20", self.request)
        self.assertEqual(self.authorizer.authorize(":1.21", self.request), 1000)

    def test_background_work_still_checks_noninteractive_polkit_policy(self):
        self.authorizer.authorize(":1.20", self.request)
        self.bus.call_sync.return_value.unpack.return_value = ((False, False, {}),)
        with self.assertRaises(MedicError):
            self.authorizer.authorize(":1.20", {"op": "automatic"})
        self.assertEqual(self.bus.call_sync.call_args.args[4][3], 0)

    def test_disconnected_caller_during_authentication_is_not_remembered(self):
        self.authorizer.user_id.side_effect = [1000, MedicError("caller disconnected")]
        with self.assertRaisesRegex(MedicError, "disconnected"):
            self.authorizer.authorize(":1.20", self.request)
        self.assertFalse(self.authorizer.active())

    def test_worker_restart_does_not_restore_approval(self):
        self.authorizer.authorize(":1.20", self.request)
        self.assertFalse(Authorizer(self.bus).active())

    def test_quit_during_password_prompt_cannot_create_a_late_approval(self):
        def completed_prompt(*args):
            self.authorizer.forget(":1.20")
            return SimpleNamespace(unpack=lambda: ((True, False, {}),))
        self.bus.call_sync.side_effect = completed_prompt
        with self.assertRaisesRegex(MedicError, "cleared before"):
            self.authorizer.authorize(":1.20", self.request)
        self.assertFalse(self.authorizer.active())

    def test_failed_authority_call_cleans_pending_authentication(self):
        self.bus.call_sync.side_effect = MedicError("authority unavailable")
        with self.assertRaisesRegex(MedicError, "unavailable"):
            self.authorizer.authorize(":1.20", self.request)
        self.assertFalse(self.authorizer.pending)

    def test_hours_are_strictly_bounded_on_the_privileged_request(self):
        request = {"op": "repair", "id": "a" * 24, "clear_dirty": True, "retry": False}
        for value in (0, 25, -1, True, 1.5, "24", None):
            with self.subTest(value=value), self.assertRaises(MedicError):
                validate({**request, "auth_hours": value})
        self.assertEqual(authorization_hours(24), 24)


if __name__ == "__main__":
    unittest.main()
