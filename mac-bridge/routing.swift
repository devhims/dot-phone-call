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
let selectors: [String: AudioObjectPropertySelector] = ["input": kAudioHardwarePropertyDefaultInputDevice, "output": kAudioHardwarePropertyDefaultOutputDevice]
do {
    let args = CommandLine.arguments
    let ids = try devices()
    if args.count == 2 && args[1] == "list" {
        let values = try ids.map { id in ["id": String(id), "name": try text(id, kAudioObjectPropertyName), "uid": try text(id, kAudioDevicePropertyDeviceUID)] }
        print(String(data: try JSONSerialization.data(withJSONObject: values, options: [.prettyPrinted]), encoding: .utf8)!)
    } else if args.count == 2 && args[1] == "status" {
        var values: [String: [String: String]] = [:]
        for (key, selector) in selectors {
            let id = try getDefault(selector)
            values[key] = ["name": try text(id, kAudioObjectPropertyName), "uid": try text(id, kAudioDevicePropertyDeviceUID)]
        }
        print(String(data: try JSONSerialization.data(withJSONObject: values), encoding: .utf8)!)
    } else if args.count == 3 && args[1] == "enable" {
        let snapshot = URL(fileURLWithPath: args[2])
        guard !FileManager.default.fileExists(atPath: snapshot.path) else { throw NSError(domain: "Routing snapshot exists; restore it first", code: 1) }
        guard let input = try ids.first(where: { try text($0, kAudioObjectPropertyName) == "BlackHole 16ch" }),
              let output = try ids.first(where: { try text($0, kAudioObjectPropertyName) == "BlackHole 2ch" }) else { throw NSError(domain: "BlackHole drivers not available", code: 2) }
        var original: [String: String] = [:]
        for (key, selector) in selectors { original[key] = try text(getDefault(selector), kAudioDevicePropertyDeviceUID) }
        try JSONSerialization.data(withJSONObject: original).write(to: snapshot, options: [.atomic])
        try setDefault(selectors["input"]!, input)
        try setDefault(selectors["output"]!, output)
        print("Routing enabled; original devices saved for restoration.")
    } else if args.count == 3 && args[1] == "restore" {
        let snapshot = URL(fileURLWithPath: args[2])
        let original = try JSONSerialization.jsonObject(with: Data(contentsOf: snapshot)) as! [String: String]
        for (key, selector) in selectors {
            guard let id = try ids.first(where: { try text($0, kAudioDevicePropertyDeviceUID) == original[key] }) else { throw NSError(domain: "Original device unavailable; snapshot retained", code: 3) }
            try setDefault(selector, id)
        }
        try FileManager.default.removeItem(at: snapshot)
        print("Original audio devices restored.")
    } else { print("Usage: routing list | status | enable SNAPSHOT | restore SNAPSHOT"); exit(2) }
} catch { fputs("Audio routing error: \(error)\n", stderr); exit(1) }
