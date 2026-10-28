"""Safety checks for occupied-port choices; no real processes are stopped."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import session_control as control


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.session = dict(port=19002, recognized=True, editor='/editor.py', input_dir='/input', mask_dir='/masks/v1',
            identity=dict(pid=12345, uid=os.getuid(), start_ticks='55', state='S'),
            state=dict(version='native-web-v1', dirty=False, busy=False, ready=True, phase='ready', revision=2,
                group=dict(key='scan:0002', scan='scan', serial='0002'),
                session=dict(protocol=1, pid=12345, instance='current')))
        p=self.home/'.cache/crafts-rfi/sessions/19002-12345.json';p.parent.mkdir(parents=True, mode=0o700)
        p.write_text(json.dumps(dict(protocol=1,pid=12345,instance='current',port=19002,token='x'*43,editor='/editor.py')))
        p.chmod(0o600);self.record=p
        patcher=patch.object(control.Path, 'home', return_value=self.home)
        patcher.start();self.addCleanup(patcher.stop)

    def choose(self, choices, fresh=None, interactive=True):
        snapshots=[copy.deepcopy(self.session),copy.deepcopy(fresh or self.session)]
        with patch.object(control, 'port_available', return_value=False), \
             patch.object(control, 'inspect_session', side_effect=snapshots), \
             patch.object(control.sys.stdin, 'isatty', return_value=interactive), \
             patch('builtins.input', side_effect=choices), \
             patch.object(control, 'shutdown_session') as stop, \
             contextlib.redirect_stdout(io.StringIO()):
            result=control.resolve_occupied_port(19002,Path('/editor.py'),'requested scan/v1/0001')
            return result,stop.call_count

    def test_continue_and_default_cancel_never_stop_a_session(self):
        self.assertEqual(self.choose(['1']),(False,0))
        self.assertEqual(self.choose(['']),(False,0))

    def test_safe_restart_requires_explicit_choice(self):
        self.assertEqual(self.choose(['2']),(True,1))

    def test_dirty_busy_or_legacy_session_cannot_be_restarted(self):
        for field,value in [('dirty',True),('busy',True),('session',{})]:
            with self.subTest(field=field):
                original=copy.deepcopy(self.session)
                self.session['state'][field]=value
                self.assertEqual(self.choose(['2','0']),(False,0))
                self.session=original

    def test_changed_revision_or_new_edits_require_a_fresh_choice(self):
        fresh=copy.deepcopy(self.session);fresh['state']['revision']=3
        self.assertEqual(self.choose(['2','0'],fresh),(False,0))
        fresh['state']['dirty']=True
        self.assertEqual(self.choose(['2','0'],fresh),(False,0))

    def test_pid_reuse_is_rejected(self):
        fresh=copy.deepcopy(self.session);fresh['identity']['start_ticks']='99'
        with self.assertRaisesRegex(ValueError,'进程已变化'):
            self.choose(['2'],fresh)

    def test_unknown_owner_and_noninteractive_input_never_stop(self):
        with self.assertRaisesRegex(ValueError,'交互终端'):
            self.choose([],interactive=False)
        self.session.update(recognized=False,reason='other service')
        with self.assertRaisesRegex(ValueError,'保留现有服务'):
            self.choose(['2'])

    def test_dry_run_never_prompts_or_stops(self):
        with patch.object(control,'port_available',return_value=False), \
             patch.object(control,'inspect_session',return_value=self.session), \
             patch('builtins.input',side_effect=AssertionError('must not prompt')), \
             patch.object(control,'shutdown_session',side_effect=AssertionError('must not stop')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(control.resolve_occupied_port(19002,Path('/editor.py'),'requested',read_only=True))

    def test_insecure_or_stale_control_record_blocks_restart(self):
        self.record.chmod(0o644)
        self.assertIn('权限',control.restart_block_reason(self.session))
        self.record.chmod(0o600)
        record=json.loads(self.record.read_text());record['instance']='old'
        self.record.write_text(json.dumps(record))
        self.assertIn('不一致',control.restart_block_reason(self.session))

    def test_authenticated_shutdown_waits_for_original_process_and_free_port(self):
        identity=self.session['identity'];zombie=dict(identity,state='Z')
        with patch.object(control,'process_identity',side_effect=[identity,zombie]), \
             patch.object(control,'request_json',return_value=dict(stopping=True,pid=12345,instance='current')) as request, \
             patch.object(control,'port_available',return_value=True), \
             contextlib.redirect_stdout(io.StringIO()):
            control.shutdown_session(self.session)
            payload=request.call_args.args[2]
            self.assertEqual(payload['revision'],2)
            self.assertEqual(payload['group_key'],'scan:0002')

    def test_refused_shutdown_or_reoccupied_port_never_starts_new_server(self):
        identity=self.session['identity']
        with patch.object(control,'process_identity',return_value=identity), \
             patch.object(control,'request_json',side_effect=ValueError('new unsaved edit')):
            with self.assertRaisesRegex(ValueError,'unsaved'):
                control.shutdown_session(self.session)
        with patch.object(control,'process_identity',side_effect=[identity,dict(identity,state='Z')]), \
             patch.object(control,'request_json',return_value=dict(stopping=True,pid=12345,instance='current')), \
             patch.object(control,'port_available',return_value=False):
            with self.assertRaisesRegex(ValueError,'其他进程'):
                control.shutdown_session(self.session)


if __name__=='__main__':
    unittest.main(buffer=True)
