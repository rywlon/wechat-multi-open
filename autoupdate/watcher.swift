import Foundation
import AppKit
import CoreServices

let eventTest = CommandLine.arguments.contains("--event-test")
let testDirectory = NSTemporaryDirectory() + "wechat-event-test-" + UUID().uuidString
let managerRoot = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL.deletingLastPathComponent()
let configURL = managerRoot.appendingPathComponent("config.json")
let configuration = (try? Data(contentsOf: configURL)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
let configuredSource = configuration?["source"] as? String ?? "/Applications/WeChat.app"
let sourcePath = eventTest ? testDirectory + "/WeChat.app" : (configuredSource as NSString).standardizingPath
let watchRoot = eventTest ? testDirectory : (sourcePath as NSString).deletingLastPathComponent
let recoveryFlags = FSEventStreamEventFlags(kFSEventStreamEventFlagMustScanSubDirs | kFSEventStreamEventFlagUserDropped | kFSEventStreamEventFlagKernelDropped | kFSEventStreamEventFlagRootChanged | kFSEventStreamEventFlagEventIdsWrapped)

func relevant(_ path: String, flags: FSEventStreamEventFlags) -> Bool {
    if flags & recoveryFlags != 0 { return true }
    let normalized = (path as NSString).standardizingPath
    return normalized == sourcePath || normalized.hasPrefix(sourcePath + "/") || normalized == watchRoot
}

if CommandLine.arguments.contains("--self-test") {
    assert(relevant(sourcePath + "/Contents/Info.plist", flags: 0))
    assert(relevant(sourcePath, flags: 0))
    assert(relevant("/Applications", flags: 0))
    assert(!relevant("/Applications/WeChat2.app/Contents/Info.plist", flags: 0))
    assert(!relevant("/Applications/WeChat.app.other/Contents/Info.plist", flags: 0))
    assert(!relevant("/Users/test/Library/Containers/com.tencent.xinWeChat/Data/Documents/test.db", flags: 0))
    assert(relevant("/Applications/unrelated", flags: FSEventStreamEventFlags(kFSEventStreamEventFlagUserDropped)))
    print("7 event-filter checks passed")
    exit(0)
}

final class Watcher {
    var stream: FSEventStreamRef?
    var scheduled: DispatchWorkItem?
    var worker: Process?
    var pending = false
    var observers: [NSObjectProtocol] = []

    func request(_ reason: String, delay: Double = 5) {
        // One-shot debounce after an actual event, never a repeating timer.
        scheduled?.cancel()
        let item = DispatchWorkItem { [weak self] in self?.launchWorker(reason) }
        scheduled = item
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: item)
    }

    func launchWorker(_ reason: String) {
        if eventTest {
            print("NATIVE_FILE_EVENT_RECEIVED: " + reason)
            try? FileManager.default.removeItem(atPath: testDirectory)
            exit(reason == "official app filesystem event" ? 0 : 1)
        }
        if worker != nil {
            pending = true
            return
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        task.arguments = [managerRoot.appendingPathComponent("manager.py").path, "--notify"]
        var environment = ProcessInfo.processInfo.environment
        environment["WECHAT_CLONE_BACKGROUND"] = "1"
        task.environment = environment
        task.standardOutput = FileHandle.standardOutput
        task.standardError = FileHandle.standardError
        task.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.worker = nil
                if self.pending {
                    self.pending = false
                    self.request("events received while checking")
                }
            }
        }
        worker = task
        do {
            try task.run()
            print("Check triggered: \(reason)")
            fflush(stdout)
        } catch {
            worker = nil
            fputs("Cannot start check: \(error)\n", stderr)
        }
    }

    func start() throws {
        if eventTest {
            try FileManager.default.createDirectory(atPath: sourcePath + "/Contents", withIntermediateDirectories: true)
        }
        let callback: FSEventStreamCallback = { _, context, count, pathsPointer, flags, _ in
            guard let context = context else { return }
            let watcher = Unmanaged<Watcher>.fromOpaque(context).takeUnretainedValue()
            let paths = unsafeBitCast(pathsPointer, to: NSArray.self)
            for index in 0..<count {
                if let path = paths[index] as? String, relevant(path, flags: flags[index]) {
                    watcher.request("official app filesystem event")
                    break
                }
            }
        }
        var context = FSEventStreamContext(version: 0, info: Unmanaged.passUnretained(self).toOpaque(), retain: nil, release: nil, copyDescription: nil)
        let options = FSEventStreamCreateFlags(kFSEventStreamCreateFlagUseCFTypes | kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagWatchRoot)
        guard let created = FSEventStreamCreate(nil, callback, &context, [watchRoot] as CFArray, FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 1.0, options) else {
            throw NSError(domain: "WeChatCloneWatcher", code: 1)
        }
        stream = created
        FSEventStreamSetDispatchQueue(created, DispatchQueue.main)
        guard FSEventStreamStart(created) else { throw NSError(domain: "WeChatCloneWatcher", code: 2) }
        let center = NSWorkspace.shared.notificationCenter
        for event in [NSWorkspace.didLaunchApplicationNotification, NSWorkspace.didTerminateApplicationNotification] {
            observers.append(center.addObserver(forName: event, object: nil, queue: .main) { [weak self] notification in
                guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                      app.bundleIdentifier == "com.tencent.xinWeChat" else { return }
                self?.request("official WeChat launched or terminated")
            })
        }
        observers.append(center.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            self?.request("Mac woke from sleep")
        })
        // Catch updates made while this user session or listener was stopped.
        if eventTest {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                try? Data("isolated event test".utf8).write(to: URL(fileURLWithPath: sourcePath + "/Contents/Info.plist"))
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
                fputs("Native event test timed out\n", stderr)
                try? FileManager.default.removeItem(atPath: testDirectory)
                exit(1)
            }
        } else {
            request("listener started", delay: 0)
        }
        print("Listening to FSEvents and official WeChat lifecycle; no periodic scans")
        fflush(stdout)
    }
}

let watcher = Watcher()
do {
    try watcher.start()
    RunLoop.main.run()
} catch {
    fputs("Watcher initialization failed: \(error)\n", stderr)
    exit(1)
}
