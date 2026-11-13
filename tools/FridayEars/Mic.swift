// Which microphone Friday listens on (the follow-up window and the listening bars). By default the same one
// Wispr Flow uses, since that's the mic he talks into; the menu can pin another, or the system default.
//
// Why it matters: the system default input can be a Bluetooth speaker's own mic (SoundDrum P). It's far
// from him, and opening it drops the speaker into 16 kHz headset mode, so Friday sounds bad and hears
// nothing. Wispr keeps its choice in config.json: prefs.user.overrideAudioDeviceId names one of
// prefs.user.rankedAudioDevices ("Built-in mic (recommended)", "SoundDrum P (Bluetooth)", "default", ...).

import AVFoundation
import CoreAudio
import Foundation

struct MicDevice: Equatable {
    let id: AudioDeviceID
    let uid: String
    let name: String
    let builtIn: Bool
    let bluetooth: Bool
}

enum Mic {
    static let prefKey = "micChoice"           // "wispr" (default) | "system" | a device UID

    static var choice: String {
        get { UserDefaults.standard.string(forKey: prefKey) ?? "wispr" }
        set { UserDefaults.standard.set(newValue, forKey: prefKey) }
    }

    // --- CoreAudio ---------------------------------------------------------------------------------

    private static func get(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
                               _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, _ value: inout UInt32) -> Bool {
        var a = AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &value) == noErr
    }

    private static func string(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String {
        var a = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &value) == noErr, let v = value else { return "" }
        return v.takeRetainedValue() as String
    }

    /// Every device that can record.
    static func inputs() -> [MicDevice] {
        var a = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal,
                                           mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size) == noErr else { return [] }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &ids) == noErr else { return [] }
        return ids.compactMap { id in
            var sa = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams, mScope: kAudioObjectPropertyScopeInput,
                                                mElement: kAudioObjectPropertyElementMain)
            var ssize: UInt32 = 0
            guard AudioObjectGetPropertyDataSize(id, &sa, 0, nil, &ssize) == noErr, ssize > 0 else { return nil }
            var transport: UInt32 = 0
            _ = get(id, kAudioDevicePropertyTransportType, kAudioObjectPropertyScopeGlobal, &transport)
            return MicDevice(id: id, uid: string(id, kAudioDevicePropertyDeviceUID), name: string(id, kAudioObjectPropertyName),
                             builtIn: transport == kAudioDeviceTransportTypeBuiltIn,
                             bluetooth: transport == kAudioDeviceTransportTypeBluetooth || transport == kAudioDeviceTransportTypeBluetoothLE)
        }
    }

    static func systemDefault() -> MicDevice? {
        var id = AudioDeviceID(0)
        guard get(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultInputDevice, kAudioObjectPropertyScopeGlobal, &id)
        else { return nil }
        return inputs().first { $0.id == id }
    }

    // --- Wispr Flow's choice -------------------------------------------------------------------------

    /// Wispr's selected mic as it names it ("Built-in mic (recommended)"), or nil for its auto/default.
    static func wisprName() -> String? {
        let url = URL(fileURLWithPath: "\(home)/Library/Application Support/Wispr Flow/config.json")
        guard let data = try? Data(contentsOf: url),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let user = (root["prefs"] as? [String: Any])?["user"] as? [String: Any],
              let chosen = user["overrideAudioDeviceId"] as? String, !chosen.isEmpty, chosen != "default",
              let ranked = user["rankedAudioDevices"] as? [[String: Any]],
              let entry = ranked.first(where: { ($0["deviceId"] as? String) == chosen })
        else { return nil }
        return entry["name"] as? String
    }

    /// A Wispr name matched to a device: "Built-in mic …" is the MacBook's own; "X (Bluetooth)" / "X (USB)"
    /// is X; otherwise the longest device name that starts it, or that it starts.
    static func match(wispr name: String, in devices: [MicDevice]) -> MicDevice? {
        if name.lowercased().hasPrefix("built-in") { return devices.first { $0.builtIn } }
        let bare = name.replacingOccurrences(of: #"\s*\((Bluetooth|USB|Virtual|Aggregate)\)$"#, with: "", options: .regularExpression)
        if let exact = devices.first(where: { $0.name == bare }) { return exact }
        return devices.filter { $0.name.hasPrefix(bare) || bare.hasPrefix($0.name) }.max { $0.name.count < $1.name.count }
    }

    // --- the decision --------------------------------------------------------------------------------

    /// The mic Friday should open now, and why (for the log and the menu). nil device = the system default.
    static func resolve() -> (device: MicDevice?, why: String) {
        let devices = inputs()
        switch choice {
        case "system":
            return (nil, "system default")
        case "wispr":
            if let name = wisprName() {
                if let d = match(wispr: name, in: devices) { return (d, "same as Wispr Flow") }
                return (nil, "Wispr's \(name) isn't connected; system default")
            }
            return (nil, "Wispr uses auto-detect; system default")
        default:
            if let d = devices.first(where: { $0.uid == choice }) { return (d, "chosen") }
            return (nil, "chosen mic isn't connected; system default")
        }
    }
}

/// Audio from one microphone. AVCaptureSession picks any input by its CoreAudio UID without touching the
/// system default. (AVAudioEngine with a device override reported the old device's format and threw an
/// Objective-C exception in installTap, which aborts the whole app: Friday vanished at every push-to-talk.)
final class MicCapture: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    private let queue = DispatchQueue(label: "friday.mic")
    private var session: AVCaptureSession?
    private(set) var deviceName = ""
    var onBuffer: ((CMSampleBuffer, Float) -> Void)?     // the audio and its loudness in dB, on the capture queue

    /// Loudness in dB as 0…1 for the listening bars.
    static func level(_ db: Float) -> Float { max(0, min(1, (db + 55) / 40)) }

    /// Opens the mic off the main thread; `started` gets the result on the main thread.
    func start(_ device: MicDevice?, started: ((Bool) -> Void)? = nil) {
        queue.async {
            let ok = self.open(device)
            DispatchQueue.main.async { started?(ok) }
        }
    }

    func stop() {
        queue.async { self.close() }
    }

    /// Set (or clear) the audio handler on the capture queue, where it's read.
    func setHandler(_ handler: ((CMSampleBuffer, Float) -> Void)?) {
        queue.async { self.onBuffer = handler }
    }

    private func open(_ device: MicDevice?) -> Bool {
        close()
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: break
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { _ in }
            return false
        default:
            log("mic: not allowed (System Settings → Privacy & Security → Microphone → FridayEars)")
            return false
        }
        let dev = device.flatMap { AVCaptureDevice(uniqueID: $0.uid) } ?? AVCaptureDevice.default(for: .audio)
        guard let dev, let input = try? AVCaptureDeviceInput(device: dev) else {
            log("mic: couldn't open \(device?.name ?? "the default mic")")
            return false
        }
        let s = AVCaptureSession()
        let out = AVCaptureAudioDataOutput()
        s.beginConfiguration()
        guard s.canAddInput(input) else { s.commitConfiguration(); return false }
        s.addInput(input)
        guard s.canAddOutput(out) else { s.commitConfiguration(); return false }
        s.addOutput(out)
        s.commitConfiguration()
        out.setSampleBufferDelegate(self, queue: queue)
        s.startRunning()
        session = s
        deviceName = dev.localizedName
        return s.isRunning
    }

    private func close() {
        session?.stopRunning()
        session = nil
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        onBuffer?(sampleBuffer, connection.audioChannels.first?.averagePowerLevel ?? -60)
    }
}
