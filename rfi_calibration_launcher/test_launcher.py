"""Focused preservation tests; only temporary synthetic manual products are written."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

LAUNCHER = Path(__file__).with_name('launch.py')
spec = importlib.util.spec_from_file_location('launcher', LAUNCHER)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.dest = self.root / 'masks_v1'
        self.dest.mkdir()
        self.backups = self.root / 'backups'
        self.contract = dict(campaign_root='/campaign', input_dir='/campaign/scan',
                             mask_dir=str(self.dest), mask_version='v1', idrift=153,
                             excluded_serials=[], automatic_mode='required',
                             automatic_dirs={'one_channel': '/auto/one', 'sat1380': '/auto/sat'})
        old = dict(self.contract, automatic_mode='none', created_utc='old-creation-time')
        old.pop('automatic_dirs')
        (self.dest / 'run.json').write_text(json.dumps(old))
        (self.dest / 'example_manual_mask.json').write_text(
            '{"done":false,"saved_at":"old-time","polarizations":[{"boxes":[[1,2,3,4]]}]}\n')
        (self.dest / 'example_manual_rfi_mask.npz').write_bytes(b'example-export\x00\xff')
        (self.dest / '.session.lock').touch()

    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            launcher.prepare_run_record(self.dest, self.contract, self.backups)

    def test_defaults_remain_v1_and_require_both_masks(self):
        args = launcher.parser().parse_args(['153'])
        self.assertEqual((args.version, args.auto, args.nfile), ('v1', 'required', '0001'))

    def test_backup_exact_and_relaunch_never_restores_or_overwrites_marks(self):
        before = {p.name: p.read_bytes() for p in self.dest.iterdir() if p.name != '.session.lock'}
        self.prepare()
        record = json.loads((self.dest / 'run.json').read_text())
        self.assertEqual(record['created_utc'], 'old-creation-time')
        backup = Path(record['before_auto_backup'])
        for name, data in before.items():
            self.assertEqual((backup / name).read_bytes(), data)
            self.assertNotEqual((backup / name).stat().st_ino, (self.dest / name).stat().st_ino)
            if name != 'run.json':
                self.assertEqual((self.dest / name).read_bytes(), data)
        manifest = json.loads((backup / 'backup_manifest.json').read_text())
        self.assertTrue(manifest['complete'])
        (self.dest / 'example_manual_mask.json').write_text('later user edits')
        self.prepare()
        self.assertEqual(len(list(self.backups.iterdir())), 1)
        self.assertEqual((self.dest / 'example_manual_mask.json').read_text(), 'later user edits')
        self.assertEqual((backup / 'example_manual_mask.json').read_bytes(), before['example_manual_mask.json'])

    def test_wrong_scan_or_changed_automatic_root_rejected(self):
        with self.assertRaises(ValueError):
            launcher.check_contract(self.dest, dict(self.contract, idrift=159))
        self.prepare()
        with self.assertRaises(ValueError):
            launcher.check_contract(self.dest, dict(self.contract, automatic_dirs={}))

    def test_failed_backup_cannot_switch_config_or_change_annotations(self):
        before = {p.name: p.read_bytes() for p in self.dest.iterdir()}
        with patch.object(launcher.shutil, 'copy2', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.dest.iterdir()})

    def test_symlink_cannot_alias_old_version(self):
        alias = self.root / 'masks_v2'
        alias.symlink_to(self.dest, target_is_directory=True)
        with self.assertRaises(ValueError):
            launcher.check_contract(alias, dict(self.contract, mask_dir=str(alias), mask_version='v2'))

    def test_new_scan_uses_requested_version_without_fabricating_manual_files(self):
        new = self.root / 'new_scan_v1'
        new.mkdir()
        launcher.prepare_run_record(new, dict(self.contract, mask_dir=str(new)), self.backups)
        self.assertEqual({p.name for p in new.iterdir()}, {'run.json'})
        self.assertFalse(self.backups.exists())


if __name__ == '__main__':
    unittest.main()
