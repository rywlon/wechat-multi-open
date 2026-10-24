# 事件驱动的微信副本维护

这个可选模块让原版微信继续接收官方更新，再把选定副本同步到原版。适合微信一直登录、不想定期退出或手动重建的人。

原版变化 → 校验版本和腾讯签名 → 系统提醒 → 等待 2 秒 → 操作弹窗 → 点击「现在更新」后退出副本、重建并重新打开。

## 安装

要求 macOS 13+、Python 3（含系统 `/usr/bin/python3`）、Swift / Xcode Command Line Tools，以及腾讯官网 Developer ID 签名的微信。App Store 版不在本模块当前支持范围内。

必须下载完整仓库，单独下载旧版 `.sh` 不包含本模块：

```sh
git clone https://github.com/rywlon/wechat-multi-open.git
cd wechat-multi-open
./wechat-multi-open.sh
```

菜单 **9 → 1** 安装监听，选择要维护的副本编号。原有 1–8 菜单含义不变。

也可以用命令安装，例如维护第 2 个副本并使用深色图标：

```sh
python3 autoupdate/installer.py install --clone 2 --name '微信（分身）' --icon icon/wechat-dark.icns
```

先查看计划，不写文件、不启动服务：

```sh
python3 autoupdate/installer.py install --clone 2 --dry-run
```

副本路径被改过名称时，显式指定已有路径；编号对应的 Bundle ID 必须匹配，否则拒绝接管：

```sh
python3 autoupdate/installer.py install --clone 2 --target '/Applications/微信（工作）.app'
```

如果系统不允许普通用户写 `/Applications`，可以先创建 `~/Applications`，再用 `--target "$HOME/Applications/WeChat2.app"`。不要以 root 身份运行整套用户级监听。

目前**一次安装维护一个选定副本**，编号范围 2–99；原脚本仍可创建和管理多个副本。重新安装可以更新维护程序、图标和显示名；未指定目标路径或显示名时沿用已安装配置，不会重置为默认名称。也不会悄悄切换账号身份或存储路径。

安装本身只部署维护程序。第一次发现副本尚未接入本模块，或版本落后时，会提示同步；只有点击「现在更新」才制作/替换应用。图标会复制到运行目录，之后移动仓库不会丢失图标。以后安装/更新维护程序时，省略 `--icon` 会沿用已选图标，不会重新设为默认深色；显式提供 `--icon` 才更换。

如果存在早期本地方案的 `local.wechat-clone.sync.plist`，安装会阻止重复部署。先停用那个任务并将其 plist 移出 LaunchAgents，再安装本模块；不需要删除微信或聊天目录。

## 日常行为

- 原生 `watcher` 常驻，订阅 FSEvents、官方微信启动/退出和电脑唤醒事件；监听启动时也检查一次。
- 没有每分钟、每小时扫描，没有 `StartInterval` 或 `WatchPaths`。
- 接收应用目录的原生文件事件后，过滤与官方微信无关的内部路径。目录替换、系统报告事件丢失等情况也会触发微信检查。
- FSEvents 的 1 秒批量延迟及收到事件后的 5 秒一次性防抖用于合并事件，不是循环扫描。
- 文件安装未完成时会在本次事件内短暂重试。多种事件互补，不能承诺任何事件机制绝不漏报。
- 对比完整构建信息、配置、签名清单和主程序元信息，能识别同一 `4.1.15` 内的不同构建。
- 同一份原版只自动提示一次。点「稍后」、取消、或 240 秒无人操作不更新，也不会安排定时重提醒。
- 先发系统通知，2 秒后弹操作窗口；即使通知被专注模式隐藏，操作窗口仍会尝试显示。
- 点「现在更新」后正常退出副本，最多等约 30 秒。退出失败就停止，不强制结束进程。原版微信继续运行。

首次重建和未来版本都需要自行确认登录、消息收发及所需功能；签名校验通过不等于所有微信功能已经实测。

## 更新与数据

在临时位置复制官方微信，保持 `com.tencent.xinWeChatN`，替换显示名称/图标，设置副本关闭自身自动更新，再本地重新签名并验证。制作成功后才替换原应用；最终校验失败会尝试还原旧应用。

修改后副本使用的是 ad-hoc 本地签名，**不会获得腾讯官方签名**。内置辅助组件仍沿用上游复制方案，本模块不是对所有内部组件身份和权限的完整重构。

应用程序与聊天数据分开保存。第 N 个副本通常使用 `~/Library/Containers/com.tencent.xinWeChatN/`；已有微信自定义数据位置仍以实际配置为准。维护程序不删除、搬动聊天数据库，只调整副本更新偏好。相同身份和数据路径用于延续原有存储，但登录有效期及未来数据库迁移仍由微信决定。

只保留一份 `previous.app.noindex` 旧程序作为恢复材料；这不是聊天记录备份。原版文件和官方签名不会被本模块改写。副本不会重复注册原版微信网页协议。

## 文件与卸载

运行目录：`~/Library/Application Support/WeChatMultiOpen/`。
日志：`~/Library/Logs/WeChatMultiOpen/`，`manager.log` 自动轮转。
用户级任务：`~/Library/LaunchAgents/local.wechat-multi-open.updater.plist`。

```sh
python3 autoupdate/installer.py status
python3 autoupdate/installer.py uninstall --dry-run
python3 autoupdate/installer.py uninstall
```

卸载移除监听、维护程序和程序备份，**保留微信应用、聊天数据和日志**。如果更新窗口或更新操作仍在进行，会拒绝卸载，先完成或取消即可。

本模块重建时会替换副本程序，因此不要让旧交互脚本和这个监听同时重建同一个副本。删除受维护副本之前，先卸载监听，否则后续事件可能再次提示创建它。

## 验证

```sh
bash -n wechat-multi-open.sh
python3 -m unittest discover -s tests -v
mkdir -p .build
swiftc autoupdate/watcher.swift -o .build/watcher
.build/watcher --self-test
.build/watcher --event-test
```

Python 检查使用临时应用和模拟系统命令，不登录微信或修改真实账号。原生事件检查在临时目录写文件，验证实际 FSEvents 回调；不会改动真实微信。一键本地检查：`./ci/check.sh`。`ci/github-actions.yml` 提供 Linux Python/Shell 与 macOS 原生事件的 GitHub Actions 模板，当前尚未启用；如需使用，将模板放到 `.github/workflows/checks.yml`，提交账号需具备写入工作流的权限。

复制、修改 Bundle ID、ad-hoc 重签的基本方法来自上游 `create_copy`；本模块补充事件监听、交互式同步、安装卸载和失败恢复。
