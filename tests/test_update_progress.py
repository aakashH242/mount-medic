from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import os

from mount_medic import __version__, updates
from mount_medic.model import MedicError
from mount_medic.storage import Preferences
from update_fixture import TARGET_VERSION, metadata


class UpdateProgressTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, XDG_CONFIG_HOME=directory.name, XDG_STATE_HOME=directory.name)
        environment.start()
        self.addCleanup(environment.stop)
        self.preferences = Preferences()

    def test_overlapping_update_is_refused_without_borrowing_or_overwriting_a_result(self):
        self.preferences.save_updates({'install_result': {'state': 'success', 'message': 'Other attempt succeeded'}})
        progress = Mock()
        with updates.update_lock(), patch('mount_medic.updates.fetch_release', side_effect=AssertionError('Second update started')):
            from types import SimpleNamespace
            self.assertEqual(updates.run_update(SimpleNamespace(version=TARGET_VERSION), progress), 2)
        self.assertIn('already running', progress.result.call_args.args[0])
        self.assertEqual(self.preferences.updates()['install_result']['message'], 'Other attempt succeeded')

    def test_real_installer_output_only_accepts_known_stages(self):
        progress = Mock()
        script = "print('compiler output'); print('MM_UPDATE_STAGE:verify'); print('MM_UPDATE_STAGE:unknown'); print('MM_UPDATE_STAGE:install')"
        with tempfile.TemporaryFile(mode='w+') as log, patch('sys.stderr', log):
            self.assertEqual(updates.installer_command([sys.executable, '-c', script], progress), 0)
            log.seek(0)
            self.assertIn('compiler output', log.read())
        self.assertEqual([call.args[0] for call in progress.stage.call_args_list],
                         ['Verifying the update…', 'Installing update…'])

    def test_reopened_app_refuses_installation_and_leaves_the_replacement_running(self):
        with patch('os.geteuid', return_value=1000), patch('mount_medic.updates.installed', return_value=True), patch('mount_medic.updates.confirm_dependencies', return_value=[]), patch('mount_medic.updates.download_archive', return_value=Path('/tmp/fixture')), patch('mount_medic.updates.stop_desktop', return_value=3), patch('mount_medic.updates.installer_command') as installer, patch('mount_medic.updates.subprocess.Popen') as restart:
            with self.assertRaisesRegex(MedicError, 'reopened during the update'):
                updates.install(metadata())
        installer.assert_not_called()
        restart.assert_not_called()

    def test_initial_result_write_failure_never_displays_a_previous_success(self):
        from types import SimpleNamespace
        self.preferences.save_updates({'install_result': {'state': 'success', 'message': 'Older attempt succeeded'}})
        progress = Mock(result_message=None)
        with patch('mount_medic.storage.Preferences.save_updates', side_effect=OSError('Cannot save new attempt')), patch('mount_medic.updates.source_version', return_value=__version__), patch('mount_medic.updates.recovery_pending', return_value=False), patch.dict(sys.modules, {'mount_medic.notifications': Mock()}):
            self.assertEqual(updates.run_update(SimpleNamespace(version=TARGET_VERSION, restart='gui'), progress), 2)
        message = progress.result.call_args.args[0]
        self.assertIn('Cannot save new attempt', message)
        self.assertNotIn('Older attempt succeeded', message)

    def test_result_acknowledgement_preserves_a_newer_result(self):
        old = updates.installation_result(TARGET_VERSION)
        newer = {**old, 'id': 'newer', 'state': 'failed', 'message': 'New attempt failed'}
        self.preferences.save_updates({'install_result': newer})
        self.preferences.mark_update_result_seen(old)
        self.assertEqual(self.preferences.updates()['install_result'], newer)
        self.preferences.mark_update_result_seen(newer)
        self.assertTrue(self.preferences.updates()['install_result']['seen'])

    def test_identical_messages_from_separate_attempts_have_distinct_ids(self):
        self.assertNotEqual(updates.installation_result(TARGET_VERSION)['id'], updates.installation_result(TARGET_VERSION)['id'])

    def test_cli_clears_previous_result_before_installing(self):
        from types import SimpleNamespace
        self.preferences.save_updates({'install_result': {'state': 'success', 'message': 'Older attempt succeeded'}})
        def install(release):
            self.assertIsNone(self.preferences.updates()['install_result'])
            raise MedicError('Synthetic install failure')
        with patch('mount_medic.updates.fetch_release', return_value=metadata()), patch('mount_medic.updates.install', side_effect=install), patch('mount_medic.updates.source_version', return_value=__version__), patch('mount_medic.updates.recovery_pending', return_value=False):
            with self.assertRaisesRegex(MedicError, 'Synthetic install failure'):
                updates.cli(SimpleNamespace(action='install', dry_run=False))
        self.assertIn('Synthetic install failure', self.preferences.updates()['install_result']['message'])

    def test_failure_only_claims_previous_version_when_recovery_is_clear(self):
        with patch('mount_medic.updates.source_version', return_value=__version__):
            updates.save_result(TARGET_VERSION, MedicError('cancelled'))
        self.assertIn(f'Kept version {__version__}', self.preferences.updates()['install_result']['message'])
        with patch('mount_medic.updates.source_version', return_value=TARGET_VERSION):
            updates.save_result(TARGET_VERSION, MedicError('commit uncertain'))
        self.assertNotIn('Kept version', self.preferences.updates()['install_result']['message'])
        with patch('mount_medic.updates.source_version', return_value=__version__), patch('mount_medic.updates.recovery_pending', return_value=True):
            updates.save_result(TARGET_VERSION, MedicError('interrupted'))
        message = self.preferences.updates()['install_result']['message']
        self.assertIn('--recover', message)
        self.assertNotIn('Kept version', message)

    def test_result_is_saved_before_restart_on_success_and_failure(self):
        for returncode in (0, 126):
            with self.subTest(returncode=returncode):
                seen = []
                def reopen(*args, **kwargs):
                    seen.append(self.preferences.updates()['install_result'])
                    return Mock()
                with patch('os.geteuid', return_value=1000), patch('mount_medic.updates.installed', return_value=True), patch('mount_medic.updates.confirm_dependencies', return_value=[]), patch('mount_medic.updates.download_archive', return_value=Path('/tmp/fixture')), patch('mount_medic.updates.stop_desktop', return_value=10), patch('mount_medic.updates.worker_running', return_value=False), patch('mount_medic.installer.elevated', side_effect=lambda command: command), patch('mount_medic.updates.installer_command', return_value=returncode), patch('mount_medic.updates.source_version', return_value=TARGET_VERSION if returncode==0 else __version__), patch('mount_medic.updates.subprocess.Popen', side_effect=reopen):
                    if returncode:
                        with self.assertRaises(MedicError):
                            updates.install(metadata())
                    else:
                        updates.install(metadata())
                self.assertEqual(seen[0]['state'], 'failed' if returncode else 'success')

    def test_restart_failure_preserves_the_committed_update_result(self):
        with patch('os.geteuid', return_value=1000), patch('mount_medic.updates.installed', return_value=True), patch('mount_medic.updates.confirm_dependencies', return_value=[]), patch('mount_medic.updates.download_archive', return_value=Path('/tmp/fixture')), patch('mount_medic.updates.stop_desktop', return_value=10), patch('mount_medic.updates.worker_running', return_value=False), patch('mount_medic.installer.elevated', side_effect=lambda command: command), patch('mount_medic.updates.installer_command', return_value=0), patch('mount_medic.updates.source_version', return_value=TARGET_VERSION), patch('mount_medic.updates.subprocess.Popen', side_effect=OSError('launcher unavailable')):
            with self.assertRaisesRegex(MedicError, 'launcher unavailable'):
                updates.install(metadata())
        result = self.preferences.updates()['install_result']
        self.assertEqual(result['state'], 'restart_failed')
        self.assertIn('Update installed successfully', result['message'])
        self.assertNotIn('Kept version', result['message'])

    def test_committed_update_remains_success_when_reporting_fails(self):
        for installer_code, report_failure in ((0, False), (0, True), (2, False)):
            with self.subTest(installer_code=installer_code, report_failure=report_failure):
                launched = []
                def restart(command, **kwargs):
                    import json
                    launched.append(json.loads(kwargs['env']['MOUNT_MEDIC_UPDATE_RESULT']))
                    return Mock()
                real_store = updates.store_result
                def store(result):
                    if report_failure:
                        raise OSError('result file is unwritable')
                    real_store(result)
                with patch('os.geteuid', return_value=1000), patch('mount_medic.updates.installed', return_value=True), patch('mount_medic.updates.confirm_dependencies', return_value=[]), patch('mount_medic.updates.download_archive', return_value=Path('/tmp/fixture')), patch('mount_medic.updates.stop_desktop', return_value=10), patch('mount_medic.updates.worker_running', return_value=False), patch('mount_medic.installer.elevated', side_effect=lambda command: command), patch('mount_medic.updates.installer_command', return_value=installer_code), patch('mount_medic.updates.source_version', return_value=TARGET_VERSION), patch('mount_medic.updates.store_result', side_effect=store), patch('mount_medic.updates.subprocess.Popen', side_effect=restart):
                    result = updates.install(metadata())
                self.assertEqual(result['updated'], TARGET_VERSION)
                self.assertEqual(launched[0]['state'], 'success')
                self.assertIn(TARGET_VERSION, launched[0]['message'])
                self.assertNotIn('Kept version', launched[0]['message'])

    def test_restart_message_is_bounded_validated_and_consumed(self):
        import json
        for invalid in ('invalid', json.dumps({'state': [], 'message': 'forged'}), ' ' * 16385):
            with self.subTest(value=invalid[:30]), patch.dict(os.environ, MOUNT_MEDIC_UPDATE_RESULT=invalid):
                self.assertIsNone(updates.restart_result())
                self.assertNotIn('MOUNT_MEDIC_UPDATE_RESULT', os.environ)
        value = {'state': 'success', 'message': 'Installed'}
        with patch.dict(os.environ, MOUNT_MEDIC_UPDATE_RESULT=json.dumps(value)):
            self.assertEqual(updates.restart_result(), value)
            self.assertIsNone(updates.restart_result())


if __name__ == '__main__':
    unittest.main()
