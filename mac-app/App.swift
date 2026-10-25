import AppKit
import Darwin

final class DotPhoneApp: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var summary = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
    var backend = NSMenuItem(title: "Local service: starting", action: nil, keyEquivalent: "")
    var tunnel = NSMenuItem(title: "ChatGPT connection: starting", action: nil, keyEquivalent: "")
    var chrome = NSMenuItem(title: "Chrome: waiting", action: nil, keyEquivalent: "")
    var notice = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    var restartItem: NSMenuItem!
    var root: URL!
    var child: Process?
    var input: Pipe?
    var timer: Timer?
    var quitting = false
    var activeCall = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let path = Bundle.main.object(forInfoDictionaryKey: "DotPhoneProjectRoot") as? String else { exit(1) }
        root = URL(fileURLWithPath: path)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "Dot Phone"
        item.button?.image = NSImage(systemSymbolName: "phone", accessibilityDescription: "Dot Phone")
        item.button?.imagePosition = .imageLeading
        let menu = NSMenu()
        menu.addItem(summary); menu.addItem(backend); menu.addItem(tunnel); menu.addItem(chrome)
        menu.addItem(notice); menu.addItem(.separator())
        restartItem = add(menu, "Restart services", #selector(restart))
        _ = add(menu, "Open logs", #selector(openLogs))
        _ = add(menu, "Open project", #selector(openProject))
        menu.addItem(.separator())
        _ = add(menu, "Quit Dot Phone", #selector(quit))
        item.menu = menu
        launch()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in self.refresh() }
    }

    func add(_ menu: NSMenu, _ title: String, _ action: Selector) -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
        entry.target = self; menu.addItem(entry); return entry
    }

    func launch() {
        let process = Process(), pipe = Pipe()
        process.executableURL = root.appendingPathComponent(".venv/bin/python")
        process.arguments = [root.appendingPathComponent("mac-app/supervisor.py").path]
        process.currentDirectoryURL = root
        process.standardInput = pipe
        let log = root.appendingPathComponent(".runtime/menu-bar-app.log")
        if !FileManager.default.fileExists(atPath: log.path) {
            FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        guard let handle = try? FileHandle(forWritingTo: log) else { exit(1) }
        handle.seekToEndOfFile(); process.standardOutput = handle; process.standardError = handle
        process.terminationHandler = { task in
            // Supervisor owns this process group. Reap surviving services on a crash.
            kill(-task.processIdentifier, SIGTERM)
            DispatchQueue.main.async {
                self.child = nil
                if self.quitting { NSApp.reply(toApplicationShouldTerminate: true) }
                else { exit(task.terminationStatus == 0 ? 0 : 1) }
            }
        }
        child = process; input = pipe
        do { try process.run() } catch { exit(1) }
    }

    func refresh() {
        let url = root.appendingPathComponent(".runtime/app-status.json")
        guard let data = try? Data(contentsOf: url),
              let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let updated = value["updated_at"] as? Double,
              Date().timeIntervalSince1970 - updated < 10 else {
            summary.title = "Starting or recovering services…"; return
        }
        activeCall = value["active_call"] as? Bool ?? false
        summary.title = value["status"] as? String ?? "Checking services"
        backend.title = "Local service: " + ((value["backend_ready"] as? Bool == true) ? "running" : "starting")
        tunnel.title = "ChatGPT connection: " + ((value["tunnel_ready"] as? Bool == true) ? "connected" : "connecting")
        chrome.title = "Chrome: " + ((value["chrome_connected"] as? Bool == true) ? "connected" : "open Chrome to connect")
        notice.title = value["message"] as? String ?? ""
        notice.isHidden = notice.title.isEmpty
        restartItem.isEnabled = !activeCall && !quitting
        item.button?.toolTip = summary.title
        if quitting && !notice.isHidden {
            // Supervisor refused shutdown because call cleanup was unconfirmed.
            quitting = false; NSApp.reply(toApplicationShouldTerminate: false)
        }
    }

    func send(_ action: String, endCall: Bool = false) {
        guard let data = try? JSONSerialization.data(withJSONObject: ["action": action, "end_call": endCall]) else { return }
        do { try input?.fileHandleForWriting.write(contentsOf: data + Data([10])) }
        catch { summary.title = "Services are restarting" }
    }
    @objc func restart() { if !activeCall { send("restart") } }
    @objc func openLogs() { NSWorkspace.shared.open(root.appendingPathComponent(".runtime")) }
    @objc func openProject() { NSWorkspace.shared.open(root) }
    @objc func quit() { NSApp.terminate(nil) }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard child?.isRunning == true else { return .terminateNow }
        if quitting { return .terminateLater }
        if activeCall {
            let alert = NSAlert()
            alert.messageText = "End the call and quit Dot Phone?"
            alert.informativeText = "The phone call will end and your normal audio devices will be restored."
            alert.addButton(withTitle: "End call and quit"); alert.addButton(withTitle: "Keep running")
            NSApp.activate(ignoringOtherApps: true)
            if alert.runModal() != .alertFirstButtonReturn { return .terminateCancel }
        }
        quitting = true; send("quit", endCall: activeCall)
        return .terminateLater
    }
}
let app = NSApplication.shared
let delegate = DotPhoneApp()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
