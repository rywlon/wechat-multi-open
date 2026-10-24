import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import tempfile
import subprocess
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('installer',Path(__file__).resolve().parents[1]/'autoupdate/installer.py')
i=importlib.util.module_from_spec(spec);spec.loader.exec_module(i)

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'WeChat.app';self.target=self.root/'Renamed clone.app'
        for app,bid in [(self.source,i.BASE_ID),(self.target,i.BASE_ID+'7')]:
            (app/'Contents').mkdir(parents=True)
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':bid}))
        self.icon=self.root/'icon.icns';self.icon.write_bytes(b'icon')
        self.args=argparse.Namespace(source=str(self.source),target=str(self.target),clone=7,icon=str(self.icon),name='Work',dry_run=True)

    def test_custom_named_clone_keeps_numbered_identity(self):
        config,_=i.build_config(self.args)
        self.assertEqual(i.BASE_ID+'7',config['bundle_id'])
        self.assertEqual(str(self.target),config['target'])

    def test_mismatched_existing_clone_is_rejected(self):
        self.args.clone=2
        with self.assertRaises(ValueError):i.build_config(self.args)

    def test_source_cannot_be_used_as_clone(self):
        self.args.target=str(self.source)
        with self.assertRaises(ValueError):i.build_config(self.args)

    def test_agent_has_no_scan_timer_or_watchpaths(self):
        agent=i.agent_config()
        self.assertTrue(agent['KeepAlive'])
        self.assertEqual([str(i.RUNTIME/'watcher')],agent['ProgramArguments'])
        for key in ['StartInterval','StartCalendarInterval','WatchPaths']:
            self.assertNotIn(key,agent)

    def test_dry_run_does_not_write_or_run_commands(self):
        runtime=self.root/'not-created'
        with patch.object(i,'RUNTIME',runtime),patch.object(i,'run') as command,contextlib.redirect_stdout(io.StringIO()) as output:
            i.install(self.args)
        self.assertFalse(runtime.exists())
        command.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())['creates_or_replaces_clone_now'])

    def test_reinstall_refuses_to_switch_account_identity(self):
        runtime=self.root/'runtime';runtime.mkdir()
        (runtime/'config.json').write_text(json.dumps({'source':str(self.source),'target':str(self.target),'bundle_id':i.BASE_ID+'2'}))
        with patch.object(i,'RUNTIME',runtime):
            with self.assertRaises(ValueError):i.install(self.args)

    def test_uninstall_dry_run_preserves_apps_and_chat_fixture(self):
        runtime=self.root/'runtime';runtime.mkdir()
        marker=runtime/'marker';marker.write_text('preserve')
        chats=self.root/'chat-data';chats.mkdir();(chats/'sentinel').write_text('keep')
        with patch.object(i,'RUNTIME',runtime),patch.object(i,'run') as command,contextlib.redirect_stdout(io.StringIO()):
            i.uninstall(argparse.Namespace(dry_run=True))
        command.assert_not_called();self.assertTrue(marker.exists())
        self.assertTrue(self.source.exists());self.assertTrue(self.target.exists());self.assertEqual('keep',(chats/'sentinel').read_text())

    def test_failed_service_reload_restores_previous_installation(self):
        self.args.dry_run=False
        runtime=self.root/'runtime';runtime.mkdir()
        agent=self.root/'agents/updater.plist';agent.parent.mkdir()
        logs=self.root/'logs'
        old_config={'source':str(self.source),'target':str(self.target),'bundle_id':i.BASE_ID+'7'}
        originals={'manager.py':b'old manager','watcher.swift':b'old source','watcher':b'old executable','clone-icon.icns':b'old icon','config.json':json.dumps(old_config).encode()}
        for name,data in originals.items():(runtime/name).write_bytes(data)
        agent.write_bytes(plistlib.dumps({'Label':i.LABEL,'KeepAlive':True}))
        old_agent=agent.read_bytes();boots=[]
        def command(args,check=True):
            args=[str(x) for x in args]
            if args[0]=='swiftc':Path(args[-1]).write_bytes(b'new executable')
            if args[0]=='/bin/launchctl' and args[1]=='bootstrap':
                boots.append(args)
                if len(boots)==1:raise RuntimeError('simulated bootstrap failure')
            return subprocess.CompletedProcess(args,0,'','TeamIdentifier=5A4RE8SF68\nAuthority=Developer ID Application: Tencent')
        with patch.object(i,'RUNTIME',runtime),patch.object(i,'AGENT',agent),patch.object(i,'LOGS',logs),patch.object(i.sys,'platform','darwin'),patch.object(i.shutil,'which',return_value='swiftc'),patch.object(i.Path,'home',return_value=self.root/'fake-home'),patch.object(i,'run',side_effect=command):
            with self.assertRaises(RuntimeError):i.install(self.args)
        self.assertEqual(2,len(boots))
        self.assertEqual(old_agent,agent.read_bytes())
        for name,data in originals.items():self.assertEqual(data,(runtime/name).read_bytes())

    def test_reinstall_without_icon_preserves_installed_choice(self):
        runtime=self.root/'runtime';runtime.mkdir()
        saved=runtime/'clone-icon.icns';saved.write_bytes(b'selected blue icon')
        self.args.icon=None
        with patch.object(i,'RUNTIME',runtime):
            config,icon=i.build_config(self.args)
        self.assertEqual(saved,icon)
        self.assertEqual(str(saved),config['icon'])

    def test_reinstall_keeps_renamed_target_and_display_name(self):
        runtime=self.root/'runtime';runtime.mkdir()
        config={'source':str(self.source),'target':str(self.target),'bundle_id':i.BASE_ID+'7','display_name':'微信GMKT'}
        (runtime/'config.json').write_text(json.dumps(config))
        self.args.target=None;self.args.name=None
        with patch.object(i,'RUNTIME',runtime):
            actual,_=i.build_config(self.args)
        self.assertEqual(str(self.target),actual['target'])
        self.assertEqual('微信GMKT',actual['display_name'])

if __name__=='__main__':unittest.main()
