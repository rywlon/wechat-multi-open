#!/usr/bin/env python3
"""Install one event-driven updater for a selected Multi Open clone on macOS."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

PACKAGE = Path(__file__).resolve().parent
RUNTIME = Path.home() / 'Library/Application Support/WeChatMultiOpen'
LOGS = Path.home() / 'Library/Logs/WeChatMultiOpen'
LABEL = 'local.wechat-multi-open.updater'
AGENT = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
BASE_ID = 'com.tencent.xinWeChat'


def run(args, check=True):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError('%s: %s' % (args[0], result.stderr.strip()))
    return result


def info(path):
    with (Path(path) / 'Contents/Info.plist').open('rb') as stream:
        return plistlib.load(stream)


def build_config(args):
    if not 2 <= args.clone <= 99:
        raise ValueError('副本编号必须为 2–99')
    source = Path(args.source).expanduser().absolute()
    target = Path(args.target or '/Applications/WeChat%d.app' % args.clone).expanduser().absolute()
    icon = Path(args.icon).expanduser().absolute()
    if source.resolve() == target.resolve():
        raise ValueError('原版和副本路径不能相同')
    if info(source).get('CFBundleIdentifier') != BASE_ID:
        raise ValueError('原版的 Bundle ID 不是官方微信')
    bundle_id = BASE_ID + str(args.clone)
    if target.exists() and info(target).get('CFBundleIdentifier') != bundle_id:
        raise ValueError('已有目标应用的 Bundle ID 不匹配，拒绝接管')
    if not icon.is_file():
        raise ValueError('找不到图标文件: %s' % icon)
    if not args.name.strip() or any(c in args.name for c in '\n\r\x00'):
        raise ValueError('显示名称不能为空或包含换行')
    return {
        'source': str(source), 'target': str(target), 'bundle_id': bundle_id,
        'icon': str(RUNTIME / 'clone-icon.icns'), 'display_name': args.name,
    }, icon


def agent_config():
    return {
        'Label': LABEL, 'ProgramArguments': [str(RUNTIME / 'watcher')],
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 30,
        'ProcessType': 'Background', 'LowPriorityIO': True, 'Nice': 10,
        'EnvironmentVariables': {'WECHAT_CLONE_BACKGROUND': '1'},
        'StandardOutPath': str(LOGS / 'service.log'),
        'StandardErrorPath': str(LOGS / 'service.log'),
    }


def restore_files(snapshots):
    for path, contents in snapshots.items():
        if contents is None:
            if path.exists():
                path.unlink()
        else:
            path.write_bytes(contents)
    if (RUNTIME / 'watcher').exists():
        os.chmod(RUNTIME / 'watcher', 0o755)


def install(args):
    config, icon = build_config(args)
    if (RUNTIME / 'config.json').exists():
        previous = json.loads((RUNTIME / 'config.json').read_text())
        if any(previous.get(k) != config[k] for k in ['source', 'target', 'bundle_id']):
            raise ValueError('此监听已接管其他身份或路径，请先卸载监听再重新安装；不要更改已有账号的 Bundle ID')
    legacy = Path.home() / 'Library/LaunchAgents/local.wechat-clone.sync.plist'
    if args.dry_run:
        print(json.dumps({'runtime': str(RUNTIME), 'launch_agent': str(AGENT),
                          'configuration': config, 'icon_source': str(icon),
                          'creates_or_replaces_clone_now': False, 'conflicting_legacy_listener': legacy.exists()}, ensure_ascii=False, indent=2))
        return
    if legacy.exists():
        raise ValueError('检测到之前的本地监听 local.wechat-clone.sync。请先停用并移走该旧配置，避免两个监听重复弹窗；无需删除微信或聊天目录。')
    if sys.platform != 'darwin':
        raise RuntimeError('安装仅支持 macOS')
    if not shutil.which('swiftc'):
        raise RuntimeError('需要 Swift / Xcode Command Line Tools，请先安装开发工具')
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', config['source']])
    signature = run(['/usr/bin/codesign', '-dv', '--verbose=4', config['source']]).stderr
    if 'TeamIdentifier=5A4RE8SF68' not in signature or 'Authority=Developer ID Application:' not in signature:
        raise RuntimeError('当前仅支持腾讯官网 Developer ID 签名的微信，不支持 App Store 版')
    if not os.access(Path(config['target']).parent, os.W_OK):
        raise RuntimeError('当前用户不能写入副本所在目录。请使用 --target 指定可写位置；无需 sudo 运行整个监听')
    # Compile and validate before stopping an existing installed listener.
    with tempfile.TemporaryDirectory(prefix='wechat-multi-open-build-') as temp:
        binary = Path(temp) / 'watcher'
        run(['swiftc', PACKAGE / 'watcher.swift', '-o', binary])
        run([binary, '--self-test'])
        run(['/usr/bin/codesign', '--force', '--sign', '-', binary])
        RUNTIME.mkdir(parents=True, exist_ok=True)
        LOGS.mkdir(parents=True, exist_ok=True)
        AGENT.parent.mkdir(parents=True, exist_ok=True)
        domain = 'gui/%d' % os.getuid()
        import fcntl
        files = [RUNTIME / n for n in ['manager.py', 'watcher.swift', 'watcher', 'clone-icon.icns', 'config.json']] + [AGENT]
        snapshots = {p: p.read_bytes() if p.exists() else None for p in files}
        with (RUNTIME / 'manager.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('维护窗口或分身更新仍在进行，请先完成或取消再安装')
            run(['/bin/launchctl', 'bootout', domain + '/' + LABEL], check=False)
            try:
                for name in ['manager.py', 'watcher.swift']:
                    temporary = RUNTIME / (name + '.installing')
                    shutil.copy2(PACKAGE / name, temporary)
                    os.replace(temporary, RUNTIME / name)
                shutil.copy2(binary, RUNTIME / 'watcher.installing')
                os.replace(RUNTIME / 'watcher.installing', RUNTIME / 'watcher')
                os.chmod(RUNTIME / 'watcher', 0o755)
                shutil.copy2(icon, RUNTIME / 'clone-icon.icns')
                (RUNTIME / 'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2))
                AGENT.write_bytes(plistlib.dumps(agent_config()))
            except Exception:
                restore_files(snapshots)
                if snapshots[AGENT] is not None:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    run(['/bin/launchctl', 'bootstrap', domain, AGENT], check=False)
                raise
        # Release the worker lock before launchd's initial version check starts.
        try:
            run(['/bin/launchctl', 'bootstrap', domain, AGENT])
        except Exception:
            run(['/bin/launchctl', 'bootout', domain + '/' + LABEL], check=False)
            restore_files(snapshots)
            if snapshots[AGENT] is not None:
                run(['/bin/launchctl', 'bootstrap', domain, AGENT], check=False)
            raise
    print('已安装事件监听。发现需要同步时先提醒，再弹操作窗口；点击「现在更新」才制作副本。')
    print('监听配置: %s\n日志: %s' % (AGENT, LOGS))


def uninstall(args):
    if args.dry_run:
        print('将停用并移除: %s\n将移除维护程序及一份程序备份: %s\n保留: 微信应用、聊天数据、日志。' % (AGENT, RUNTIME))
        return
    if sys.platform != 'darwin':
        raise RuntimeError('卸载仅支持 macOS')
    # A running worker may be holding an update dialog or replacing the clone.
    lock_path = RUNTIME / 'manager.lock'
    lock = None
    if lock_path.exists():
        import fcntl
        lock = lock_path.open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError('维护窗口或更新仍在进行，请先完成或取消，再卸载')
    try:
        run(['/bin/launchctl', 'bootout', 'gui/%d/%s' % (os.getuid(), LABEL)], check=False)
        if AGENT.exists():
            AGENT.unlink()
        if RUNTIME.exists():
            shutil.rmtree(RUNTIME)
    finally:
        if lock:
            lock.close()
    print('已移除事件监听和维护程序。微信应用、聊天数据和日志均保留。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    install_parser = sub.add_parser('install', help='安装或更新原生事件监听')
    install_parser.add_argument('--source', default='/Applications/WeChat.app')
    install_parser.add_argument('--clone', type=int, default=2)
    install_parser.add_argument('--target', help='已有副本路径，默认 /Applications/WeChatN.app')
    install_parser.add_argument('--icon', default=str(PACKAGE.parent / 'icon/wechat-dark.icns'))
    install_parser.add_argument('--name', default='微信（分身）')
    install_parser.add_argument('--dry-run', action='store_true')
    uninstall_parser = sub.add_parser('uninstall', help='卸载维护程序，保留应用和聊天数据')
    uninstall_parser.add_argument('--dry-run', action='store_true')
    sub.add_parser('status', help='显示监听状态')
    args = parser.parse_args()
    try:
        if args.command == 'install':
            install(args)
        elif args.command == 'uninstall':
            uninstall(args)
        else:
            result = run(['/bin/launchctl', 'print', 'gui/%d/%s' % (os.getuid(), LABEL)], check=False)
            print(result.stdout or '监听尚未加载。')
            if (RUNTIME / 'config.json').exists():
                print((RUNTIME / 'config.json').read_text())
    except (OSError, ValueError, RuntimeError) as exc:
        print('错误: %s' % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
