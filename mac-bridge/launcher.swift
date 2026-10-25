import AppKit
import AVFoundation

// Owns the macOS microphone permission for the existing Python companion.
// No physical input is opened here; the preflight targets BlackHole by name.
final class CompanionApp: NSObject, NSApplicationDelegate {
    let nativeMode = CommandLine.arguments.dropFirst().first?.hasPrefix("chrome-extension://") == true
    var item: NSStatusItem!
    var label: NSMenuItem!
    var child: Process?
    var stopping = false
    var root: URL!

    func status(_ text: String) {
        label?.title = text
        let value: [String: Any] = ["status": text, "updated_at": Date().timeIntervalSince1970]
        if let data = try? JSONSerialization.data(withJSONObject: value) {
            try? data.write(to: root.appendingPathComponent(".runtime/native-companion-status.json"), options: .atomic)
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let path = Bundle.main.object(forInfoDictionaryKey: "DotPhoneProjectRoot") as? String else {
            NSApp.terminate(nil); return
        }
        root = URL(fileURLWithPath: path)
        if !nativeMode {
            item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            item.button?.title = "Dot Phone"
            let menu = NSMenu()
            label = NSMenuItem(title: "Starting", action: nil, keyEquivalent: "")
            menu.addItem(label)
            menu.addItem(.separator())
            let quit = NSMenuItem(title: "Quit Dot Phone", action: #selector(quitApp), keyEquivalent: "")
            quit.target = self
            menu.addItem(quit)
            item.menu = menu
        }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: nativeMode ? startCompanion() : checkAudio()
        case .notDetermined:
            status("Waiting for microphone permission")
            NSApp.activate(ignoringOtherApps: true)
            AVCaptureDevice.requestAccess(for: .audio) { allowed in
                DispatchQueue.main.async {
                    if allowed { self.nativeMode ? self.startCompanion() : self.checkAudio() }
                    else { self.status("Microphone permission denied"); if self.nativeMode { NSApp.terminate(nil) } }
                }
            }
        default: status("Enable microphone access in System Settings"); if nativeMode { NSApp.terminate(nil) }
        }
    }

    func process(_ arguments: [String]) -> Process {
        let task = Process()
        task.executableURL = root.appendingPathComponent(".venv/bin/python")
        task.arguments = arguments
        task.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        task.environment = env
        return task
    }

    func checkAudio() {
        status("Checking BlackHole audio")
        let task = process([root.appendingPathComponent("mac-bridge/devices.py").path, "--test-loopback"])
        let output = Pipe()
        task.standardOutput = output
        task.standardError = output
        task.terminationHandler = { process in
            let data = output.fileHandleForReading.readDataToEndOfFile()
            try? data.write(to: self.root.appendingPathComponent(".runtime/native-audio-check.log"), options: .atomic)
            DispatchQueue.main.async {
                self.child = nil
                if self.stopping { NSApp.reply(toApplicationShouldTerminate: true); return }
                if process.terminationStatus == 0 { self.startCompanion() }
                else { self.status("Audio check failed; calling disabled") }
            }
        }
        do { child = task; try task.run() }
        catch { child = nil; status("Could not run audio check") }
    }

    func startCompanion() {
        let script = nativeMode ? "mac-bridge/native_host.py" : "mac-bridge/companion.py"
        let task = process([root.appendingPathComponent(script).path] + (nativeMode ? [CommandLine.arguments[1]] : []))
        if nativeMode {
            task.standardInput = FileHandle.standardInput
            task.standardOutput = FileHandle.standardOutput
            task.standardError = FileHandle.standardError
        }
        if !nativeMode {
            for (name, isError) in [("mac-companion.log", false), ("mac-companion-errors.log", true)] {
                let url = root.appendingPathComponent(".runtime/" + name)
                if !FileManager.default.fileExists(atPath: url.path) {
                    FileManager.default.createFile(atPath: url.path, contents: nil, attributes: [.posixPermissions: 0o600])
                }
                guard let handle = try? FileHandle(forWritingTo: url) else { status("Cannot open companion log"); return }
                handle.seekToEndOfFile()
                if isError { task.standardError = handle } else { task.standardOutput = handle }
            }
        }
        task.terminationHandler = { _ in
            DispatchQueue.main.async {
                self.child = nil
                if self.stopping { NSApp.reply(toApplicationShouldTerminate: true); return }
                if self.nativeMode { self.status("Helper exited"); NSApp.terminate(nil); return }
                self.status("Companion stopped; retrying")
                DispatchQueue.main.asyncAfter(deadline: .now()+10) {
                    if !self.stopping { self.startCompanion() }
                }
            }
        }
        do { child = task; try task.run(); status("Companion running") }
        catch { child = nil; status("Could not start companion") }
    }

    @objc func quitApp() { NSApp.terminate(nil) }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        stopping = true
        if let task = child, task.isRunning {
            status("Stopping and restoring audio")
            task.terminate()
            return .terminateLater
        }
        return .terminateNow
    }
}

let app = NSApplication.shared
let delegate = CompanionApp()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
