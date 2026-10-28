"""Navigation/data-preservation tests using tiny temporary mask exports."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

import crafts_tod_native_web as web
from crafts_tod_mask import BeamAveragedTod, TodFile, TodGroup, mask_path, save_mask_document


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.groups = [TodGroup('scan', serial, 'W', '_T', tuple(
            TodFile(self.root/f'scan_arcdrift-M{i:02}_W_{serial}_T.fits',
                    'scan', f'M{i:02}', 'W', serial, '_T') for i in range(1, 20)))
            for serial in ['0001', '0002']]
        args = SimpleNamespace(input_dir=self.root, mask_dir=self.root/'masks', target='0001',
            one_channel_dir=None, sat1380_dir=None, block_records=64, require_all_auto_masks=False)
        with patch.object(web, 'discover_groups', return_value=self.groups):
            self.state = web.NativeMaskState(args)
        self.addCleanup(self.state.executor.shutdown, wait=True)
        self.addCleanup(self.state.render_executor.shutdown, wait=True)
        self.state.schedule_spectrum = Mock()
        self.state.aggregate = self.aggregate()
        self.state.frequency = self.state.aggregate.frequency_mhz
        self.state.mjd = self.state.aggregate.mjd
        self.state.phase, self.state.busy = 'ready', False
        self.state.document.polarizations[0].frequency_ranges_mhz = [[1300., 1301.]]
        self.state.dirty, self.state.revision = True, 4
        self.before = copy.deepcopy(self.state.document)

    def aggregate(self, *args, **kwargs):
        frequency = np.arange(1300., 1304.)
        mjd = np.arange(3.) + 60000
        raw = np.ones((3, 4, 2), dtype=np.float32)
        return BeamAveragedTod(frequency, mjd, raw, raw.copy(), {},
                              np.zeros(raw.shape, dtype=np.uint8), {}, {}, [])

    def navigate(self, resolution):
        return asyncio.run(self.state.navigate('next', resolution, self.state.group.key, self.state.revision))

    def assert_edits_retained(self):
        self.assertEqual(self.state.index, 0)
        self.assertEqual(self.state.document, self.before)
        self.assertTrue(self.state.dirty)
        self.assertFalse(self.state.navigation_busy)

    def test_unsaved_without_explicit_choice_never_leaves(self):
        with self.assertRaisesRegex(ValueError, '未保存'):
            self.navigate('ask')
        self.assert_edits_retained()
        self.assertFalse(self.state.args.mask_dir.exists())

    def test_stale_context_and_busy_navigation_are_rejected(self):
        for key, revision in [('wrong-group', 4), (self.state.group.key, 3)]:
            with self.assertRaisesRegex(ValueError, '已变化'):
                asyncio.run(self.state.navigate('next', 'discard', key, revision))
        self.state.navigation_busy = True
        with self.assertRaisesRegex(ValueError, '尚未完成'):
            self.navigate('discard')
        self.state.navigation_busy = False
        self.assert_edits_retained()

    def test_export_failure_never_navigates_and_retains_in_memory_edits(self):
        # Simulates a failure after JSON writing. The pre-existing exporter is
        # not a multi-file transaction; this test does not claim disk rollback.
        with patch.object(web, 'save_beam_mask_products', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.navigate('save')
        self.assert_edits_retained()
        self.assertEqual(self.state.phase, 'ready')
        self.assertFalse(self.state.busy)

    def test_save_exports_original_group_before_loading_next(self):
        with patch.object(web, 'load_beam_averaged_tod_with_auto_masks', side_effect=self.aggregate):
            result = self.navigate('save')
        self.assertTrue(result['saved'])
        self.assertEqual(len(result['paths']), 20)
        self.assertTrue(all(Path(p).is_file() for p in result['paths']))
        self.assertEqual(len(list(self.state.args.mask_dir.glob('*0001*manual_rfi_mask.npz'))), 19)
        self.assertFalse(mask_path(self.state.args.mask_dir, self.groups[1]).exists())
        self.assertEqual(self.state.index, 1)
        self.assertFalse(self.state.dirty)
        with np.load(next(self.state.args.mask_dir.glob('*M01*manual_rfi_mask.npz'))) as z:
            self.assertTrue(z['mask'][:, :2, 0].all())
            self.assertFalse(z['mask'][:, :, 1].any())

    def test_discard_leaves_saved_files_unchanged(self):
        path = mask_path(self.state.args.mask_dir, self.groups[0])
        saved = copy.deepcopy(self.before)
        saved.polarizations[0].frequency_ranges_mhz = [[1302., 1303.]]
        save_mask_document(path, saved)
        before = path.read_bytes()
        with patch.object(web, 'load_beam_averaged_tod_with_auto_masks', side_effect=self.aggregate):
            result = self.navigate('discard')
        self.assertFalse(result['saved'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.state.index, 1)
        self.assertFalse(self.state.dirty)

    def test_load_failure_restores_discarded_in_memory_edits_and_current_data(self):
        aggregate = self.state.aggregate
        with patch.object(web, 'load_beam_averaged_tod_with_auto_masks', side_effect=OSError('bad next FITS')):
            with self.assertRaisesRegex(OSError, 'bad next FITS'):
                self.navigate('discard')
        self.assert_edits_retained()
        self.assertIs(self.state.aggregate, aggregate)
        self.assertEqual(self.state.phase, 'ready')
        self.assertTrue(self.state.public_state()['ready'])

    def test_load_failure_after_save_keeps_saved_current_group(self):
        with patch.object(web, 'load_beam_averaged_tod_with_auto_masks', side_effect=OSError('bad next FITS')):
            with self.assertRaisesRegex(OSError, 'bad next FITS'):
                self.navigate('save')
        self.assertEqual(self.state.index, 0)
        self.assertFalse(self.state.dirty)
        self.assertTrue(self.state.document.done)
        self.assertEqual(self.state.document.polarizations, self.before.polarizations)
        self.assertEqual(len(list(self.state.args.mask_dir.glob('*manual_rfi_mask.npz'))), 19)

    def shutdown_payload(self):
        return dict(pid=os.getpid(), instance=self.state.session_instance,
                    group_key=self.state.group.key, revision=self.state.revision)

    def test_shutdown_refuses_dirty_busy_and_changed_context(self):
        payload = self.shutdown_payload()
        with self.assertRaisesRegex(ValueError, '未保存'):
            self.state.prepare_shutdown(payload, self.state.control_token)
        self.state.dirty = False
        self.state.busy = True
        with self.assertRaisesRegex(ValueError, '正在'):
            self.state.prepare_shutdown(payload, self.state.control_token)
        self.state.busy = False
        with self.assertRaisesRegex(ValueError, '状态已变化'):
            self.state.prepare_shutdown(dict(payload, revision=3), self.state.control_token)
        self.assertFalse(self.state.shutting_down)

    def test_shutdown_authentication_and_instance_must_match(self):
        self.state.dirty = False
        payload = self.shutdown_payload()
        with self.assertRaises(PermissionError):
            self.state.prepare_shutdown(payload, 'wrong-token')
        with self.assertRaisesRegex(ValueError, '实例已变化'):
            self.state.prepare_shutdown(dict(payload, instance='old-instance'), self.state.control_token)
        self.assertFalse(self.state.shutting_down)

    def test_shutdown_freezes_further_loading_before_exit(self):
        self.state.dirty = False
        self.state.prepare_shutdown(self.shutdown_payload(), self.state.control_token)
        self.assertTrue(self.state.shutting_down)
        self.assertTrue(self.state.busy)
        self.assertFalse(self.state.public_state()['ready'])
        with self.assertRaisesRegex(ValueError, '正在关闭'):
            asyncio.run(self.state.load(1))
        self.assertEqual(self.state.index, 0)
        self.assertEqual(self.state.document, self.before)

    def test_control_credentials_are_private_and_not_in_public_state(self):
        self.state.args.port = 19001
        with patch.object(web.Path, 'home', return_value=self.root):
            record = web.register_session_control(self.state)
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertEqual(record.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(record.read_text())['token'], self.state.control_token)
        self.assertNotIn(self.state.control_token, json.dumps(self.state.public_state()))
        web.remove_session_control(record, self.state)
        self.assertFalse(record.exists())

    def test_old_session_cleanup_cannot_remove_a_new_instances_record(self):
        self.state.args.port = 19001
        with patch.object(web.Path, 'home', return_value=self.root):
            record = web.register_session_control(self.state)
        newer = json.loads(record.read_text()); newer['instance'] = 'newer'
        record.write_text(json.dumps(newer))
        web.remove_session_control(record, self.state)
        self.assertTrue(record.exists())


if __name__ == '__main__':
    unittest.main(buffer=True)
