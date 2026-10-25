import Foundation
import CoreAudio

func check(_ result: OSStatus) throws {
    if result != noErr { throw NSError(domain: "CoreAudio", code: Int(result)) }
}
func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
}
func devices() throws -> [AudioDeviceID] {
    var a = address(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    try check(AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size))
    var ids = [AudioDeviceID](repeating: 0, count: Int(size)/MemoryLayout<AudioDeviceID>.size)
    try check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &ids))
    return ids
}
func text(_ id: AudioDeviceID, _ selector: AudioObjectPropertySelector) throws -> String {
    var a = address(selector)
    var value: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    try check(AudioObjectGetPropertyData(id, &a, 0, nil, &size, &value))
    return value as String
}
func getDefault(_ selector: AudioObjectPropertySelector) throws -> AudioDeviceID {
    var a = address(selector), id: AudioDeviceID = 0
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    try check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &id))
    return id
}
func setDefault(_ selector: AudioObjectPropertySelector, _ id: AudioDeviceID) throws {
    var a = address(selector), value = id
    try check(AudioObjectSetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, UInt32(MemoryLayout<AudioDeviceID>.size), &value))
}
func isRunning(_ id: AudioDeviceID) throws -> Bool {
    var a = address(kAudioDevicePropertyDeviceIsRunningSomewhere), value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    try check(AudioObjectGetPropertyData(id, &a, 0, nil, &size, &value))
    return value != 0
}
func isBlackHole(_ id: AudioDeviceID) throws -> Bool {
    return try text(id, kAudioObjectPropertyName).hasPrefix("BlackHole ")
}
func builtIn(_ key: String, _ ids: [AudioDeviceID]) throws -> AudioDeviceID {
    let uid = key == "input" ? "BuiltInMicrophoneDevice" : "BuiltInSpeakerDevice"
    guard let id = try ids.first(where: { try text($0, kAudioDevicePropertyDeviceUID) == uid }) else {
        throw NSError(domain: "AUDIO_ROUTE_RECOVERY_UNAVAILABLE", code: 76)
    }
    return id
}
func applyAndVerify(_ targets: [String: AudioDeviceID]) throws {
    for (key, id) in targets { try setDefault(selectors[key]!, id) }
    var stable = 0
    for _ in 0..<100 {
        if try targets.allSatisfy({ try getDefault(selectors[$0.key]!) == $0.value }) { stable += 1 } else { stable = 0 }
        if stable >= 5 { return }
        Thread.sleep(forTimeInterval: 0.02)
    }
    throw NSError(domain: "AUDIO_ROUTE_CHANGE_UNCONFIRMED", code: 77)
}
let selectors: [String: AudioObjectPropertySelector] = ["input": kAudioHardwarePropertyDefaultInputDevice, "output": kAudioHardwarePropertyDefaultOutputDevice]
do {
    let args = CommandLine.arguments
    let ids = try devices()
    if args.count == 2 && args[1] == "list" {
        let values = try ids.map { id in ["id": String(id), "name": try text(id, kAudioObjectPropertyName), "uid": try text(id, kAudioDevicePropertyDeviceUID)] }
        print(String(data: try JSONSerialization.data(withJSONObject: values, options: [.prettyPrinted]), encoding: .utf8)!)
    } else if args.count == 2 && args[1] == "status" {
        var values: [String: [String: Any]] = [:]
        for (key, selector) in selectors {
            let id = try getDefault(selector)
            values[key] = ["name": try text(id, kAudioObjectPropertyName), "uid": try text(id, kAudioDevicePropertyDeviceUID), "running": try isRunning(id)]
        }
        print(String(data: try JSONSerialization.data(withJSONObject: values), encoding: .utf8)!)
    } else if args.count == 3 && args[1] == "recover-idle" {
        guard !FileManager.default.fileExists(atPath: args[2]) else { throw NSError(domain: "Routing snapshot exists; restore it first", code: 1) }
        var targets: [String: AudioDeviceID] = [:]
        var observed: [String: AudioDeviceID] = [:]
        for (key, selector) in selectors {
            let current = try getDefault(selector)
            observed[key] = current
            if try isBlackHole(current) {
                guard try !isRunning(current) else { throw NSError(domain: "AUDIO_ROUTE_ALREADY_IN_USE", code: 75) }
                targets[key] = try builtIn(key, ids)
            }
        }
        // Recheck immediately before mutation. Unknown/active routes are never taken over.
        for (key, current) in observed {
            guard try getDefault(selectors[key]!) == current else { throw NSError(domain: "AUDIO_ROUTE_ALREADY_IN_USE", code: 75) }
            if try targets[key] != nil && isRunning(current) { throw NSError(domain: "AUDIO_ROUTE_ALREADY_IN_USE", code: 75) }
        }
        try applyAndVerify(targets)
        print("Idle BlackHole defaults recovered to built-in audio; no call placed.")
    } else if args.count == 3 && args[1] == "enable" {
        let snapshot = URL(fileURLWithPath: args[2])
        guard !FileManager.default.fileExists(atPath: snapshot.path) else { throw NSError(domain: "Routing snapshot exists; restore it first", code: 1) }
        guard let input = try ids.first(where: { try text($0, kAudioObjectPropertyName) == "BlackHole 16ch" }),
              let output = try ids.first(where: { try text($0, kAudioObjectPropertyName) == "BlackHole 2ch" }) else { throw NSError(domain: "BlackHole drivers not available", code: 2) }
        var original: [String: String] = [:]
        for (key, selector) in selectors { original[key] = try text(getDefault(selector), kAudioDevicePropertyDeviceUID) }
        try JSONSerialization.data(withJSONObject: original).write(to: snapshot, options: [.atomic])
        try applyAndVerify(["input": input, "output": output])
        print("Routing enabled; original devices saved for restoration.")
    } else if args.count == 3 && args[1] == "restore" {
        let snapshot = URL(fileURLWithPath: args[2])
        let original = try JSONSerialization.jsonObject(with: Data(contentsOf: snapshot)) as! [String: String]
        var targets: [String: AudioDeviceID] = [:]
        for key in selectors.keys {
            if let id = try ids.first(where: { try text($0, kAudioDevicePropertyDeviceUID) == original[key] }) {
                targets[key] = id
            } else {
                targets[key] = try builtIn(key, ids)
                print("Original \(key) device disconnected; restoring built-in audio.")
            }
        }
        try applyAndVerify(targets)
        try FileManager.default.removeItem(at: snapshot)
        print("Original audio devices restored.")
    } else { print("Usage: routing list | status | enable SNAPSHOT | restore SNAPSHOT | recover-idle SNAPSHOT"); exit(2) }
} catch { fputs("Audio routing error: \(error)\n", stderr); let code = (error as NSError).code; exit([75, 76, 77].contains(code) ? Int32(code) : 1) }
