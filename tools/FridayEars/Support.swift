// Shared bits: where things are, logging, talking to fridayd, synthesized keys, formatting, colours.

import Cocoa
import SwiftUI

let home = NSHomeDirectory()
let repo = (Bundle.main.object(forInfoDictionaryKey: "FridayRepo") as? String) ?? "\(home)/code/mine/friday"
let fridayd = "http://127.0.0.1:8765"
let gatewayHealth = "http://127.0.0.1:8652/health"
let signalHealth = "http://127.0.0.1:8080/api/v1/check"
let laptopVoiceHealth = "http://127.0.0.1:8091/health"
let logDir = URL(fileURLWithPath: "\(home)/.friday/logs")

let kRightOption: Int64 = 61
let kEscape: Int64 = 53
let kFn: CGKeyCode = 63
let rightAltMask: UInt64 = 0x40          // NX_DEVICERALTKEYMASK
let holdDelay: TimeInterval = 0.25       // hold Right ⌥ this long to talk
let doubleTapWindow: TimeInterval = 0.4  // two taps this close open the panel
let ourEvent: Int64 = 0xF41D             // marks keys we synthesize, so our own tap ignores them

private let logQueue = DispatchQueue(label: "friday.log")

func log(_ msg: String) {
    let line = "\(ISO8601DateFormatter().string(from: Date())) \(msg)\n"
    logQueue.sync {
        let url = logDir.appendingPathComponent("ears.log")
        if let h = try? FileHandle(forWritingTo: url) { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile() }
        else { try? line.write(to: url, atomically: true, encoding: .utf8) }
    }
}

/// A JSON request to fridayd (POST when there's a body). `done` runs on the main thread.
func request(_ path: String, _ body: [String: Any]? = nil, timeout: TimeInterval = 3,
             done: (([String: Any]?) -> Void)? = nil) {
    var req = URLRequest(url: URL(string: path.hasPrefix("http") ? path : fridayd + path)!)
    req.timeoutInterval = timeout
    if let body {
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
    }
    URLSession.shared.dataTask(with: req) { data, _, _ in
        let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        DispatchQueue.main.async { done?(json) }
    }.resume()
}

/// Blocking health check. Never call it on the main thread.
func healthy(_ url: String, timeout: TimeInterval = 1.5) -> Bool {
    let sem = DispatchSemaphore(value: 0)
    var ok = false
    var req = URLRequest(url: URL(string: url)!)
    req.timeoutInterval = timeout
    URLSession.shared.dataTask(with: req) { _, resp, _ in
        ok = (resp as? HTTPURLResponse)?.statusCode == 200
        sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + timeout + 0.5)
    return ok
}

func key(_ code: CGKeyCode, down: Bool, flags: CGEventFlags = [], asFlagsChanged: Bool = false) {
    let src = CGEventSource(stateID: .hidSystemState)
    guard let e = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: down) else { return }
    if asFlagsChanged { e.type = .flagsChanged }
    e.flags = flags
    e.setIntegerValueField(.eventSourceUserData, value: ourEvent)
    e.post(tap: .cghidEventTap)
}

/// Is Right Option physically down right now (whatever our event tap saw)?
func rightOptionDown() -> Bool {
    (CGEventSource.flagsState(.hidSystemState).rawValue & rightAltMask) != 0
}

func clock(_ s: Double) -> String {
    let s = max(0, s)
    if s < 10 { return String(format: "%.1fs", s) }
    if s < 60 { return "\(Int(s))s" }
    if s < 3600 { return "\(Int(s) / 60)m \(Int(s) % 60)s" }
    return "\(Int(s) / 3600)h \(Int(s) % 3600 / 60)m"
}

func ago(_ s: Double) -> String {
    let s = max(0, s)
    if s < 60 { return "now" }
    if s < 3600 { return "\(Int(s) / 60)m" }
    if s < 86400 { return "\(Int(s) / 3600)h" }
    return "\(Int(s) / 86400)d"
}

func hhmm(_ ts: Double) -> String {
    let f = DateFormatter()
    f.dateFormat = "HH:mm"
    return f.string(from: Date(timeIntervalSince1970: ts))
}

/// When something happened, for the Earlier list: the time today, "Yday", or the date.
func when(_ ts: Double) -> String {
    let d = Date(timeIntervalSince1970: ts)
    if Calendar.current.isDateInToday(d) { return hhmm(ts) }
    if Calendar.current.isDateInYesterday(d) { return "Yday" }
    let f = DateFormatter()
    f.dateFormat = "d MMM"
    return f.string(from: d)
}

/// A main-thread timer in the common run-loop modes: it keeps firing while a menu is open (a default-mode
/// timer freezes then, so a follow-up window couldn't time out and a held key couldn't start listening).
@discardableResult
func commonTimer(_ interval: TimeInterval, repeats: Bool, _ block: @escaping (Timer) -> Void) -> Timer {
    let t = Timer(timeInterval: interval, repeats: repeats, block: block)
    RunLoop.main.add(t, forMode: .common)
    return t
}

func nowTS() -> Double { Date().timeIntervalSince1970 }

/// Friday's colours. The island and panel are always dark (they grow out of the black notch).
enum Palette {
    static let fri = Color(red: 0.55, green: 0.70, blue: 1.0)          // Friday working / speaking
    static let mic = Color(red: 1.0, green: 0.62, blue: 0.04)          // the mic is open
    static let needs = Color(red: 1.0, green: 0.71, blue: 0.28)        // something needs you
    static let ok = Color(red: 0.24, green: 0.84, blue: 0.55)
    static let bad = Color(red: 1.0, green: 0.35, blue: 0.31)
    static let hud = Color(red: 0.063, green: 0.071, blue: 0.086)
    static let card = Color.white.opacity(0.06)
    static let line = Color.white.opacity(0.09)
    static let dim = Color.white.opacity(0.58)
    static let faint = Color.white.opacity(0.34)
}

extension Dictionary where Key == String, Value == Any {
    func str(_ k: String) -> String { (self[k] as? String) ?? "" }
    func optStr(_ k: String) -> String? { self[k] as? String }
    func num(_ k: String) -> Double { (self[k] as? NSNumber)?.doubleValue ?? 0 }
    func optNum(_ k: String) -> Double? { (self[k] as? NSNumber)?.doubleValue }
    func int(_ k: String) -> Int { (self[k] as? NSNumber)?.intValue ?? 0 }
    func bool(_ k: String) -> Bool { (self[k] as? Bool) ?? ((self[k] as? NSNumber)?.boolValue ?? false) }
    func dict(_ k: String) -> [String: Any]? { self[k] as? [String: Any] }
    func list(_ k: String) -> [[String: Any]] { (self[k] as? [[String: Any]]) ?? [] }
}
