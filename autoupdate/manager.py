#!/usr/bin/python3
"""Maintain Multi Open's WeChat2 identity without touching chat-data directories.
Clone method: https://github.com/nullbyte-lab/wechat-multi-open (create_copy).
The native event listener calls this worker; updating requires the action dialog.
Staging, signature verification and rollback protect the application replacement.
"""
import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
SOURCE_ID = 'com.tencent.xinWeChat'
CLONE_ID = 'com.tencent.xinWeChat2'
MARKER = 'LocalWeChatCloneManager'
RECIPE = 1
LOG = logging.getLogger('wechat-clone')


def run(args, check=True):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True, timeout=300)
    if check and result.returncode:
        raise RuntimeError('Command failed: %s: %s' % (args[0], result.stderr.strip()[-1800:]))
    return result


def read_plist(app):
    with open(Path(app) / 'Contents/Info.plist', 'rb') as stream:
        return plistlib.load(stream)


def save_json(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temp, path)


def running_clone(target):
    rows = run(['/bin/ps', '-axo', 'comm=']).stdout.splitlines()
    prefix = str(target) + '/Contents/'
    return any(row.strip().startswith(prefix) for row in rows)


class Manager:
    def __init__(self, config, root=ROOT):
        self.config = config
        self.clone_id = config.get('bundle_id', CLONE_ID)
        if not re.fullmatch(r'com\.tencent\.xinWeChat(?:[2-9]|[1-9][0-9])', self.clone_id):
            raise ValueError('Invalid Multi Open clone Bundle ID')
        self.root = Path(root)
        self.source = Path(config['source'])
        self.target = Path(config['target'])
        self.icon = Path(config['icon'])
        self.state_path = self.root / 'state.json'
        self.backup = self.root / 'previous.app.noindex'

    def status(self, message):
        status_file = self.root / 'last-status.txt'
        previous = status_file.read_text() if status_file.exists() else None
        if message != previous:
            LOG.info(message)
            status_file.write_text(message)
        return message

    def fingerprint(self):
        info = read_plist(self.source)
        if info.get('CFBundleIdentifier') != SOURCE_ID:
            raise RuntimeError('Source is not the official WeChat bundle identity')
        executable = self.source / 'Contents/MacOS' / info['CFBundleExecutable']
        seal = self.source / 'Contents/_CodeSignature/CodeResources'
        stat = executable.stat()
        identity = {
            'info': hashlib.sha256((self.source / 'Contents/Info.plist').read_bytes()).hexdigest(),
            'seal': hashlib.sha256(seal.read_bytes()).hexdigest(),
            'executable_size': stat.st_size,
            'executable_mtime': stat.st_mtime_ns,
            'build': info.get('CFBundleVersion'),
            'version': info.get('WeChatBundleVersion', info.get('CFBundleShortVersionString')),
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(), identity

    def desired_marker(self, fingerprint):
        return {
            'recipe': RECIPE,
            'source_fingerprint': fingerprint,
            'icon_sha256': hashlib.sha256(self.icon.read_bytes()).hexdigest(),
            'display_name': self.config['display_name'],
        }

    def verify_source(self):
        run(['/usr/bin/codesign', '--verify', '--deep', '--strict', self.source])
        signature = run(['/usr/bin/codesign', '-dv', '--verbose=4', self.source]).stderr
        if 'TeamIdentifier=5A4RE8SF68' not in signature or 'Authority=Developer ID Application:' not in signature:
            raise RuntimeError('Source does not have the expected Tencent Developer ID signature')

    def configure(self, staged, marker):
        path = staged / 'Contents/Info.plist'
        info = read_plist(staged)
        info['CFBundleIdentifier'] = self.clone_id
        info['CFBundleName'] = self.config['display_name']
        info['CFBundleDisplayName'] = self.config['display_name']
        info['CFBundleIconFile'] = 'AppIcon.icns'
        info.pop('CFBundleIconName', None)  # Prefer replacement .icns over Assets.car.
        info.pop('CFBundleURLTypes', None)  # Official WeChat keeps handling weixin links.
        info['SUEnableAutomaticChecks'] = False
        info['SUAllowsAutomaticUpdates'] = False
        info['SUAutomaticallyUpdate'] = False
        info[MARKER] = marker
        with open(path, 'wb') as stream:
            plistlib.dump(info, stream)
        for strings in (staged / 'Contents/Resources').glob('*.lproj/InfoPlist.strings'):
            for key in ['CFBundleDisplayName', 'CFBundleName']:
                # Apple's localized plists may be OpenStep format; PlistBuddy reads both.
                run(['/usr/libexec/PlistBuddy', '-c', 'Set :%s %s' % (key, self.config['display_name']), strings], check=False)
        shutil.copy2(self.icon, staged / 'Contents/Resources/AppIcon.icns')
        # Same working signing recipe as Multi Open; do not apply the host's sandbox
        # entitlements to every helper/installer. Existing data paths are left alone.
        run(['/usr/bin/xattr', '-dr', 'com.apple.quarantine', staged], check=False)
        run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', staged])
        run(['/usr/bin/codesign', '--verify', '--deep', '--strict', staged])

    def ask_update(self, version):
        message = ('官方微信版本为 %s。\n\n现在更新将自动退出「微信（分身）」，保留原有数据目录，重新制作并打开新版分身。主微信继续运行。' % version)
        script = ('on run argv\n'
                  ' set answer to display dialog (item 1 of argv) with title "微信分身更新" '
                  'buttons {"稍后", "现在更新"} default button "稍后" cancel button "稍后" giving up after 240\n'
                  ' if gave up of answer then return "稍后"\n'
                  ' return button returned of answer\nend run')
        result = run(['/usr/bin/osascript', '-e', script, message], check=False)
        return result.returncode == 0 and result.stdout.strip() == '现在更新'

    def update_interactively(self, version):
        if not self.ask_update(version):
            return self.status('Update postponed by user')
        if running_clone(self.target):
            # Ask only the clone to quit gracefully; never kill either WeChat.
            run(['/usr/bin/osascript', '-e',
                 'tell application id "%s" to quit' % self.clone_id], check=False)
            for attempt in range(15):
                if not running_clone(self.target):
                    break
                time.sleep(2)
            if running_clone(self.target):
                run(['/usr/bin/osascript', '-e',
                     'display alert "分身尚未退出" message "可能有正在进行的操作。此次未替换应用，请完成操作后再更新。" giving up after 60'], check=False)
                return self.status('Update stopped: clone did not quit')
        try:
            result = self.sync()
        except Exception:
            # sync has already restored the old bundle if final verification failed.
            if self.target.exists():
                run(['/usr/bin/open', '-g', '-a', self.target], check=False)
            run(['/usr/bin/osascript', '-e',
                 'display alert "分身更新未完成" message "已保留旧版应用及数据目录。请稍后再试。" giving up after 60'], check=False)
            raise
        if result.startswith(('Updated successfully:', 'Up to date:')):
            run(['/usr/bin/open', '-a', self.target])
            run(['/usr/bin/osascript', '-e',
                 'display notification "微信分身已更新并重新打开。" with title "微信分身更新完成"'], check=False)
        else:
            run(['/usr/bin/osascript', '-e',
                 'display alert "本次暂缓更新" message "官方安装尚未完成，或分身再次打开。旧版应用和聊天数据仍保留。" giving up after 60'], check=False)
        return result

    def notify_update(self):
        """File-event entry: show actions; update only after Now is selected."""
        stable = None
        for attempt in range(6):
            try:
                fingerprint, identity = self.fingerprint()
                current = read_plist(self.target) if self.target.exists() else {}
                if current.get(MARKER, {}).get('source_fingerprint') == fingerprint:
                    return self.status('Up to date: ' + str(identity['version']))
                time.sleep(2)
                self.verify_source()
                if self.fingerprint()[0] != fingerprint:
                    continue
                stable = (fingerprint, identity)
                break
            except (OSError, RuntimeError, ValueError):
                if attempt == 5:
                    raise
                time.sleep(3)
        if stable is None:
            return self.status('Deferred reminder: official update is still changing')
        fingerprint, identity = stable
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if state.get('prompted_fingerprint') == fingerprint:
            return 'Already prompted'
        state['prompted_fingerprint'] = fingerprint
        state['prompted_at'] = time.time()
        save_json(self.state_path, state)
        message = ('官方微信版本为 %s，即将弹出分身同步操作窗口。' % identity['version'])
        script = ('on run argv\n display notification (item 1 of argv) '
                  'with title "微信分身更新提醒"\nend run')
        result = run(['/usr/bin/osascript', '-e', script, message], check=False)
        if result.returncode:
            LOG.warning('System notification could not be submitted; opening action dialog anyway')
        time.sleep(2)
        return self.update_interactively(str(identity['version']))

    def clear_clone_update_preferences(self):
        for key in ['SUEnableAutomaticChecks', 'SUAutomaticallyUpdate', 'SUAllowsAutomaticUpdates']:
            run(['/usr/bin/defaults', 'write', self.clone_id, key, '-bool', 'false'])
        # The old clone has a sandbox-domain preference file as well. Only change
        # updater preferences, never chat databases or account configuration.
        domain = Path.home() / 'Library/Containers' / self.clone_id / 'Data/Library/Preferences' / self.clone_id
        if Path(str(domain) + '.plist').exists():
            for key in ['SUEnableAutomaticChecks', 'SUAutomaticallyUpdate', 'SUAllowsAutomaticUpdates']:
                result = run(['/usr/bin/defaults', 'write', domain, key, '-bool', 'false'], check=False)
                if result.returncode:
                    LOG.warning('Cannot set container updater preference: %s', key)

    def sync(self):
        if not self.source.is_dir():
            return self.status('Waiting: official WeChat is unavailable')
        fingerprint, identity = self.fingerprint()
        marker = self.desired_marker(fingerprint)
        if self.target.exists():
            current = read_plist(self.target)
            if current.get('CFBundleIdentifier') != self.clone_id:
                raise RuntimeError('Refusing to replace an application with a different identity')
            if current.get(MARKER) == marker:
                return self.status('Up to date: ' + str(identity['version']))
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if state.get('observed_fingerprint') != fingerprint:
            state = {'observed_fingerprint': fingerprint, 'observed_at': time.time()}
            save_json(self.state_path, state)
        if running_clone(self.target):
            return self.status('Pending: quit the clone to apply version ' + str(identity['version']))
        self.verify_source()
        staged = self.target.parent / ('.' + self.target.stem + '.cm-' + uuid.uuid4().hex + '.app')
        previous = self.target.parent / ('.' + self.target.stem + '.rollback-' + uuid.uuid4().hex + '.app')
        installed = False
        moved_old = False
        try:
            LOG.info('Preparing clone %s (%s)', identity['version'], identity['build'])
            run(['/bin/cp', '-Rc', self.source, staged])
            if self.fingerprint()[0] != fingerprint:
                return self.status('Deferred: official application changed while copying')
            run(['/usr/bin/codesign', '--verify', '--deep', '--strict', staged])
            self.configure(staged, marker)
            self.verify_source()
            if self.fingerprint()[0] != fingerprint or running_clone(self.target):
                return self.status('Deferred: source changed or clone was opened while preparing')
            self.clear_clone_update_preferences()
            if self.target.exists():
                os.rename(self.target, previous)
                moved_old = True
            try:
                os.rename(staged, self.target)
                installed = True
                run(['/usr/bin/codesign', '--verify', '--deep', '--strict', self.target])
            except Exception:
                if installed:
                    shutil.rmtree(self.target)
                if moved_old:
                    os.rename(previous, self.target)
                raise
            if moved_old:
                # Retain one previous application as a rollback copy; no chat data
                # is stored or deleted here. The backup stays out of Spotlight.
                try:
                    if self.backup.exists():
                        if read_plist(self.backup).get('CFBundleIdentifier') != self.clone_id:
                            raise RuntimeError('Unexpected backup identity')
                        shutil.rmtree(self.backup)
                    os.rename(previous, self.backup)
                except Exception as exc:
                    LOG.warning('Updated, but old bundle remains at %s: %s', previous, exc)
            run(['/usr/bin/touch', self.target])
            run(['/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister', '-f', self.target], check=False)
            save_json(self.state_path, {
                'observed_fingerprint': fingerprint, 'observed_at': state['observed_at'],
                'installed_version': identity['version'], 'installed_build': identity['build'],
                'installed_at': time.time(),
            })
            return self.status('Updated successfully: ' + str(identity['version']))
        finally:
            if staged.exists():
                shutil.rmtree(staged)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--notify', action='store_true', help='Show update dialog; update only after Now is selected (default)')
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(ROOT / 'manager.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        config = json.loads((ROOT / 'config.json').read_text())
        manager = Manager(config)
        if args.status:
            print(json.dumps({
                'source': str(manager.source), 'target': str(manager.target),
                'clone_running': running_clone(manager.target),
                'source_version': read_plist(manager.source).get('WeChatBundleVersion'),
                'clone_version': read_plist(manager.target).get('WeChatBundleVersion') if manager.target.exists() else None,
                'managed': read_plist(manager.target).get(MARKER) if manager.target.exists() else None,
            }, ensure_ascii=False, indent=2))
            return 0
        try:
            manager.notify_update()
            return 0
        except Exception:
            LOG.exception('Clone refresh failed; original WeChat and chat-data directories were not modified')
            return 1


if __name__ == '__main__':
    log_dir = Path.home() / 'Library/Logs/WeChatMultiOpen'
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [RotatingFileHandler(log_dir / 'manager.log', maxBytes=2_000_000, backupCount=2)]
    if os.environ.get('WECHAT_CLONE_BACKGROUND') != '1':
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=handlers)
    sys.exit(main())
