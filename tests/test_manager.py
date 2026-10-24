import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('manager', Path(__file__).resolve().parents[1] / 'autoupdate/manager.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source, self.target = root/'WeChat.app', root/'WeChat2.app'
        for app, bid in [(self.source,m.SOURCE_ID),(self.target,m.CLONE_ID)]:
            (app/'Contents/MacOS').mkdir(parents=True)
            (app/'Contents/Resources').mkdir()
            (app/'Contents/_CodeSignature').mkdir()
            (app/'Contents/_CodeSignature/CodeResources').write_bytes(b'valid-seal')
            (app/'Contents/MacOS/WeChat').write_bytes(b'executable')
            with (app/'Contents/Info.plist').open('wb') as f:
                plistlib.dump({'CFBundleIdentifier':bid,'CFBundleExecutable':'WeChat',
                  'CFBundleShortVersionString':'4.1.15','CFBundleVersion':'270135',
                  'WeChatBundleVersion':'4.1.15.55','CFBundleIconName':'AppIcon'},f)
        self.data = root/'chat-data'
        self.data.mkdir()
        (self.data/'sentinel').write_text('DO NOT MODIFY')
        icon=root/'dark.icns';icon.write_bytes(b'dark-icon')
        state=root/'manager';state.mkdir()
        self.manager=m.Manager({'source':str(self.source),'target':str(self.target),
         'icon':str(icon),'display_name':'微信（分身）'},state)

    def fake_run(self,args,check=True):
        args=[str(x) for x in args]
        if args[0]=='/bin/cp': shutil.copytree(args[2],args[3])
        return subprocess.CompletedProcess(args,0,'','TeamIdentifier=5A4RE8SF68\nAuthority=Developer ID Application: Tencent')

    def test_running_clone_defers_without_touching_bundle_or_data(self):
        original=(self.target/'Contents/Info.plist').read_bytes()
        with patch.object(m,'running_clone',return_value=True),patch.object(m,'run',side_effect=self.fake_run):
            self.assertIn('Pending',self.manager.sync())
        self.assertEqual(original,(self.target/'Contents/Info.plist').read_bytes())
        self.assertEqual('DO NOT MODIFY',(self.data/'sentinel').read_text())

    def test_signing_failure_keeps_old_app(self):
        original=(self.target/'Contents/Info.plist').read_bytes()
        def fail_sign(args,check=True):
            if '--sign' in args: raise RuntimeError('simulated signing failure')
            return self.fake_run(args,check)
        with patch.object(m,'running_clone',return_value=False),patch.object(m,'run',side_effect=fail_sign):
            with self.assertRaises(RuntimeError):self.manager.sync()
        self.assertEqual(original,(self.target/'Contents/Info.plist').read_bytes())
        self.assertFalse(list(self.target.parent.glob('.WeChat2.cm-*')))

    def test_success_preserves_identity_data_and_official_app(self):
        official=(self.source/'Contents/Info.plist').read_bytes()
        with patch.object(m,'running_clone',return_value=False),patch.object(m,'run',side_effect=self.fake_run):
            self.assertIn('Updated successfully',self.manager.sync())
            self.assertIn('Up to date',self.manager.sync())
        info=m.read_plist(self.target)
        self.assertEqual(m.CLONE_ID,info['CFBundleIdentifier'])
        self.assertNotIn('CFBundleIconName',info)
        self.assertEqual('dark-icon',(self.target/'Contents/Resources/AppIcon.icns').read_text())
        self.assertEqual(official,(self.source/'Contents/Info.plist').read_bytes())
        self.assertEqual('DO NOT MODIFY',(self.data/'sentinel').read_text())
        self.assertTrue(self.manager.backup.exists())

    def test_same_short_version_new_build_is_detected(self):
        with patch.object(m,'running_clone',return_value=False),patch.object(m,'run',side_effect=self.fake_run):
            self.manager.sync()
            info=m.read_plist(self.source);info['CFBundleVersion']='270136'
            with (self.source/'Contents/Info.plist').open('wb') as f:plistlib.dump(info,f)
            self.assertIn('Updated successfully',self.manager.sync())
        self.assertEqual('270136',m.read_plist(self.target)['CFBundleVersion'])

    def test_source_changes_during_copy_does_not_replace(self):
        original=(self.target/'Contents/Info.plist').read_bytes()
        def changing_copy(args,check=True):
            result=self.fake_run(args,check)
            if str(args[0])=='/bin/cp':
                (self.source/'Contents/MacOS/WeChat').write_bytes(b'newer-executable')
            return result
        with patch.object(m,'running_clone',return_value=False),patch.object(m,'run',side_effect=changing_copy):
            self.assertIn('Deferred',self.manager.sync())
        self.assertEqual(original,(self.target/'Contents/Info.plist').read_bytes())

    def test_final_verification_failure_rolls_back(self):
        original=(self.target/'Contents/Info.plist').read_bytes()
        def fail_final_verify(args,check=True):
            if '--verify' in args and Path(args[-1])==self.target:
                raise RuntimeError('simulated final verification failure')
            return self.fake_run(args,check)
        with patch.object(m,'running_clone',return_value=False),patch.object(m,'run',side_effect=fail_final_verify):
            with self.assertRaises(RuntimeError):self.manager.sync()
        self.assertEqual(original,(self.target/'Contents/Info.plist').read_bytes())

    def test_later_does_not_replace_and_prompts_only_once(self):
        before=(self.target/'Contents/Info.plist').read_bytes()
        events=[]
        def record(args,check=True):
            if str(args[0])=='/usr/bin/osascript' and 'display notification' in str(args):
                events.append('notification')
            return self.fake_run(args,check)
        def answer(version):
            events.append('dialog')
            return False
        with patch.object(m,'run',side_effect=record),patch.object(m.time,'sleep'),patch.object(self.manager,'ask_update',side_effect=answer) as dialog:
            self.assertIn('postponed',self.manager.notify_update())
            self.assertEqual('Already prompted',self.manager.notify_update())
            dialog.assert_called_once()
        self.assertEqual(['notification','dialog'],events)
        self.assertEqual(before,(self.target/'Contents/Info.plist').read_bytes())

    def test_now_quits_clone_updates_then_reopens(self):
        calls=[]
        def record(args,check=True):
            calls.append([str(x) for x in args]);return self.fake_run(args,check)
        with patch.object(m,'run',side_effect=record),patch.object(m.time,'sleep'),patch.object(self.manager,'ask_update',return_value=True),patch.object(m,'running_clone',side_effect=[True,False,False]),patch.object(self.manager,'sync',return_value='Updated successfully: 4.1.15.55') as update:
            self.manager.update_interactively('4.1.15.55')
            update.assert_called_once_with()
        quit_calls=[c for c in calls if c[0]=='/usr/bin/osascript' and ' to quit' in c[-1]]
        self.assertEqual(1,len(quit_calls))
        self.assertIn('com.tencent.xinWeChat2',quit_calls[0][-1])
        self.assertTrue(any(c[0]=='/usr/bin/open' and str(self.target) in c for c in calls))

    def test_failed_graceful_quit_does_not_replace(self):
        with patch.object(m,'run',side_effect=self.fake_run),patch.object(m.time,'sleep'),patch.object(self.manager,'ask_update',return_value=True),patch.object(m,'running_clone',return_value=True),patch.object(self.manager,'sync') as update:
            self.assertIn('did not quit',self.manager.update_interactively('4.1.15.55'))
            update.assert_not_called()

    def test_notification_skips_current_clone(self):
        with patch.object(m,'running_clone',return_value=False),patch.object(m,'run',side_effect=self.fake_run):
            self.manager.sync()
        with patch.object(m,'run') as command:
            self.assertIn('Up to date',self.manager.notify_update())
            command.assert_not_called()

    def test_selected_clone_identity_is_preserved_in_rebuild(self):
        config=dict(self.manager.config,bundle_id='com.tencent.xinWeChat7')
        info=m.read_plist(self.target);info['CFBundleIdentifier']='com.tencent.xinWeChat7'
        with (self.target/'Contents/Info.plist').open('wb') as f:plistlib.dump(info,f)
        manager=m.Manager(config,self.manager.root)
        with patch.object(m,'run',side_effect=self.fake_run),patch.object(m,'running_clone',return_value=False):
            manager.sync()
        self.assertEqual('com.tencent.xinWeChat7',m.read_plist(self.target)['CFBundleIdentifier'])

    def test_invalid_bundle_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            m.Manager(dict(self.manager.config,bundle_id='com.tencent.xinWeChat'),self.manager.root)

if __name__=='__main__': unittest.main(verbosity=2)
