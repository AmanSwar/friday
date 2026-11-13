// FridayEars: Friday's app. Opening it starts Friday (Hermes gateway + fridayd); quitting it stops
// both and frees their memory. Hold Right Option to talk (Wispr Flow does the speech recognition).
// The UI lives in an island attached to the notch: listening, transcribing, what you said, the step
// Friday is on ("Checking master_aws…"), and live captions while she speaks. Click the island for a
// panel with the whole turn: your words, each step with its time, the full answer, and Stop.
//
// Needs Accessibility (press fn / ⌘⌃C, consume Right Option) and Input Monitoring (see Right Option).
// Build: scripts/build-ears.sh   Logs: ~/.friday/logs/ears.log, fridayd.stdout.log, gateway.stdout.log

import AVFoundation
import Cocoa
import Speech
import SQLite3
import SwiftUI
import Vision

// MARK: - config

let home = NSHomeDirectory()
let repo = (Bundle.main.object(forInfoDictionaryKey: "FridayRepo") as? String) ?? "\(home)/code/mine/friday"
let fridayd = "http://127.0.0.1:8765"
let gatewayHealth = "http://127.0.0.1:8652/health"
let laptopVoiceHealth = "http://127.0.0.1:8091/health"
let logDir = URL(fileURLWithPath: "\(home)/.friday/logs")
let kRightOption: Int64 = 61
let kFn: CGKeyCode = 63
let kC: CGKeyCode = 8
let rightAltMask: UInt64 = 0x40          // NX_DEVICERALTKEYMASK
let holdDelay: TimeInterval = 0.25
let ourEvent: Int64 = 0xF41D

func log(_ msg: String) {
    let line = "\(ISO8601DateFormatter().string(from: Date())) \(msg)\n"
    let url = logDir.appendingPathComponent("ears.log")
    if let h = try? FileHandle(forWritingTo: url) { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile() }
    else { try? line.write(to: url, atomically: true, encoding: .utf8) }
}

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

func healthy(_ url: String) -> Bool {
    let sem = DispatchSemaphore(value: 0)
    var ok = false
    var req = URLRequest(url: URL(string: url)!); req.timeoutInterval = 1.5
    URLSession.shared.dataTask(with: req) { _, resp, _ in
        ok = (resp as? HTTPURLResponse)?.statusCode == 200; sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + 2)
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

// MARK: - supervisor: Friday's processes live and die with this app

final class Supervisor {
    private var children: [Process] = []
    // SSH_AUTH_SOCK: the laptop's SSH key lives in the macOS agent. Without it `ssh wsl-box` is refused,
    // the laptop voice never starts, and every reply renders on this Mac's GPU.
    private let env: [String: String] = {
        var e = ["PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                 "HOME": home, "USER": NSUserName(), "LANG": "en_US.UTF-8", "PYTHONPATH": "\(repo)/src"]
        if let sock = ProcessInfo.processInfo.environment["SSH_AUTH_SOCK"] { e["SSH_AUTH_SOCK"] = sock }
        return e
    }()

    private func spawn(_ exe: String, _ args: [String], log name: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        p.environment = env
        p.currentDirectoryURL = URL(fileURLWithPath: repo)
        let url = logDir.appendingPathComponent(name)
        if !FileManager.default.fileExists(atPath: url.path) { FileManager.default.createFile(atPath: url.path, contents: nil) }
        if let fh = try? FileHandle(forWritingTo: url) { fh.seekToEndOfFile(); p.standardOutput = fh; p.standardError = fh }
        do { try p.run(); children.append(p); log("started \(name) (pid \(p.processIdentifier))") }
        catch { log("could not start \(name): \(error)") }
    }

    private var tunnel: Process?

    /// Run a short command and wait for it (with a deadline). Returns the exit status, or -1.
    @discardableResult
    private func run(_ args: [String], timeout: TimeInterval) -> Int32 {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: args[0])
        p.arguments = Array(args.dropFirst())
        p.environment = env
        p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return -1 }
        let deadline = Date().addingTimeInterval(timeout)
        while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
        if p.isRunning { p.terminate(); return -1 }
        return p.terminationStatus
    }

    /// Friday's voice runs on the laptop's GPU: start its server there and own the SSH tunnel to it.
    /// If the laptop is off or asleep, fridayd simply uses the Mac voice. Safe to call repeatedly.
    func ensureLaptopVoice(wait: TimeInterval = 0) {
        if let t = tunnel, t.isRunning, healthy(laptopVoiceHealth) { return }
        if let t = tunnel, t.isRunning { t.terminate() }
        guard run(["/bin/bash", "\(repo)/scripts/laptop-voice.sh", "start"], timeout: 12) == 0 else {
            log("laptop voice: laptop not reachable (the Mac voice covers)"); return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = ["\(repo)/scripts/laptop-voice.sh", "tunnel"]
        p.environment = env
        p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return }
        tunnel = p
        log("laptop voice: tunnel up (pid \(p.processIdentifier))")
        let deadline = Date().addingTimeInterval(wait)
        while Date() < deadline && !healthy(laptopVoiceHealth) { Thread.sleep(forTimeInterval: 1) }
    }

    /// Start whatever isn't already running and wait until both answer. Runs off the main thread.
    func start(_ progress: @escaping (String) -> Void) -> Bool {
        progress("Waking Friday's voice on the laptop…")
        ensureLaptopVoice(wait: 40)
        if !healthy(gatewayHealth) {
            progress("Starting Friday's brain…")
            spawn("/bin/bash", ["\(repo)/bin/friday", "gateway", "run"], log: "gateway.stdout.log")
        }
        if !healthy("http://127.0.0.1:8080/api/v1/check") {   // Signal: live messages + fast sending
            spawn("/bin/bash", ["\(repo)/scripts/signal-daemon.sh"], log: "signal.stdout.log")
        }
        if !healthy("\(fridayd)/health") {
            progress("Loading Friday's voice…")
            spawn("\(repo)/.venv/bin/python", ["-m", "fridayd"], log: "fridayd.stdout.log")
        }
        let deadline = Date().addingTimeInterval(150)
        while Date() < deadline {
            let g = healthy(gatewayHealth), f = healthy("\(fridayd)/health")
            if g && f { return true }
            progress(!g ? "Starting Friday's brain…" : "Loading Friday's voice…")
            Thread.sleep(forTimeInterval: 1)
        }
        return false
    }

    /// Stop both, whether this app started them or found them running. Runs off the main thread.
    func stop() {
        let sem = DispatchSemaphore(value: 0)
        request("/quit", [:], timeout: 2) { _ in sem.signal() }
        _ = sem.wait(timeout: .now() + 3)
        let stopper = Process()
        stopper.executableURL = URL(fileURLWithPath: "/bin/bash")
        stopper.arguments = ["\(repo)/bin/friday", "gateway", "stop"]
        stopper.environment = env
        stopper.standardOutput = FileHandle.nullDevice; stopper.standardError = FileHandle.nullDevice
        try? stopper.run()
        for p in children where p.isRunning { p.terminate() }
        if let t = tunnel, t.isRunning { t.terminate() }
        run(["/bin/bash", "\(repo)/scripts/laptop-voice.sh", "stop"], timeout: 10)   // frees the laptop GPU too
        // the Signal daemon, even if adopted; anchored to the binary so no other command line can match
        run(["/usr/bin/pkill", "-f", "^[^ ]*/signal-cli -a [^ ]+ daemon --http 127\\.0\\.0\\.1:8080"], timeout: 5)
        let deadline = Date().addingTimeInterval(20)
        while Date() < deadline && (stopper.isRunning || children.contains { $0.isRunning }) { Thread.sleep(forTimeInterval: 0.2) }
        for p in children where p.isRunning { kill(p.processIdentifier, SIGKILL) }
        if stopper.isRunning { stopper.terminate() }
        log("Friday stopped")
    }
}

// MARK: - camera: is Aman in front of the Mac? (only while keyboard and mouse are idle)

/// On-device face detection (Apple Vision), one frame a second. No frame is stored or sent anywhere;
/// the only output is "a face is there". Runs only while there's no keyboard/mouse input.
// MARK: - follow-up window: after Friday answers, listen a few seconds without the key

/// Apple's speech recognizer (on-device when this Mac supports it), separate from Wispr. Opens after
/// Friday finishes answering a push-to-talk turn or asks a question that needs an answer (a permission
/// prompt). Speech within `window` seconds is transcribed until a ~1.3 s pause, then handed to `done`.
final class FollowUp {
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var silence: Timer?, deadline: Timer?
    private var text = ""
    private(set) var active = false
    var onHeard: ((String) -> Void)?       // partial text, for the island
    var done: ((String?) -> Void)?         // final text, or nil when nothing was said

    func start(window: TimeInterval = 6) {
        guard !active, let recognizer, recognizer.isAvailable else { return }
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: break
        case .notDetermined: SFSpeechRecognizer.requestAuthorization { _ in }; return
        default: return
        }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        if recognizer.supportsOnDeviceRecognition { req.requiresOnDeviceRecognition = true }
        let input = engine.inputNode
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { buf, _ in req.append(buf) }
        engine.prepare()
        do { try engine.start() } catch { log("follow-up mic failed: \(error)"); return }
        request = req; text = ""; active = true
        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
            guard let self else { return }
            DispatchQueue.main.async {
                if let result {
                    self.text = result.bestTranscription.formattedString
                    if !self.text.isEmpty {
                        self.deadline?.invalidate()           // he started talking: wait for the pause instead
                        self.onHeard?(self.text)
                        self.silence?.invalidate()
                        self.silence = Timer.scheduledTimer(withTimeInterval: 1.3, repeats: false) { _ in self.finish() }
                    }
                    if result.isFinal { self.finish() }
                } else if error != nil, self.active {
                    self.finish()
                }
            }
        }
        deadline = Timer.scheduledTimer(withTimeInterval: window, repeats: false) { _ in
            if self.text.isEmpty { self.finish() }
        }
    }

    func cancel() { stop(); done?(nil) }

    private func finish() {
        guard active else { return }
        let said = text.trimmingCharacters(in: .whitespacesAndNewlines)
        stop()
        done?(said.isEmpty ? nil : said)
    }

    private func stop() {
        guard active else { return }
        active = false
        silence?.invalidate(); deadline?.invalidate()
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        request?.endAudio()
        task?.cancel()
        request = nil; task = nil
    }
}

final class CameraWatch: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "friday.camera")
    private var configured = false, lastCheck = Date.distantPast
    private(set) var running = false
    var onFace: () -> Void = {}

    private func configure() -> Bool {
        if configured { return true }
        guard let device = AVCaptureDevice.default(for: .video),
              let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input) else {
            log("camera: no usable camera"); return false
        }
        session.beginConfiguration()
        session.sessionPreset = .low
        session.addInput(input)
        let output = AVCaptureVideoDataOutput()
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)
        if session.canAddOutput(output) { session.addOutput(output) }
        session.commitConfiguration()
        configured = true
        return true
    }

    func start() {
        guard !running else { return }
        running = true
        AVCaptureDevice.requestAccess(for: .video) { ok in
            guard ok else { log("camera: access denied (System Settings → Privacy & Security → Camera)"); self.running = false; return }
            self.queue.async {
                guard self.configure() else { self.running = false; return }
                self.session.startRunning()
                log("camera on (you've been idle)")
            }
        }
    }

    func stop() {
        guard running else { return }
        running = false
        queue.async { if self.session.isRunning { self.session.stopRunning(); log("camera off") } }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard Date().timeIntervalSince(lastCheck) >= 1, let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        lastCheck = Date()
        let request = VNDetectFaceRectanglesRequest()
        try? VNImageRequestHandler(cvPixelBuffer: pixels, options: [:]).perform([request])
        if let faces = request.results, !faces.isEmpty { onFace() }
    }
}

// MARK: - island model + view

final class Island: ObservableObject {
    enum Phase: Equatable { case hidden, message, listening, transcribing, heard, thinking, speaking }
    @Published var phase: Phase = .hidden
    @Published var title = ""
    @Published var text = ""
    var onTap: () -> Void = {}

    func set(_ phase: Phase, _ title: String = "", _ text: String = "") {
        self.phase = phase; self.title = title; self.text = text
    }
}

struct IslandShape: Shape {           // flat top (flush with the screen edge), rounded bottom
    var radius: CGFloat
    var animatableData: CGFloat { get { radius } set { radius = newValue } }
    func path(in r: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: r.minX, y: r.minY))
        p.addLine(to: CGPoint(x: r.maxX, y: r.minY))
        p.addLine(to: CGPoint(x: r.maxX, y: r.maxY - radius))
        p.addQuadCurve(to: CGPoint(x: r.maxX - radius, y: r.maxY), control: CGPoint(x: r.maxX, y: r.maxY))
        p.addLine(to: CGPoint(x: r.minX + radius, y: r.maxY))
        p.addQuadCurve(to: CGPoint(x: r.minX, y: r.maxY - radius), control: CGPoint(x: r.minX, y: r.maxY))
        p.closeSubpath()
        return p
    }
}

struct Indicator: View {
    let phase: Island.Phase
    var body: some View {
        TimelineView(.animation) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            switch phase {
            case .listening:
                ZStack {
                    Circle().fill(Color.orange.opacity(0.25)).scaleEffect(1 + 0.25 * sin(t * 5))
                    Image(systemName: "mic.fill").font(.system(size: 13, weight: .semibold)).foregroundColor(.orange)
                }
            case .transcribing:
                HStack(spacing: 3) {
                    ForEach(0..<3) { i in
                        Circle().fill(Color.white.opacity(0.35 + 0.65 * max(0, sin(t * 4 - Double(i) * 0.9)))).frame(width: 5, height: 5)
                    }
                }
            case .thinking:
                HStack(spacing: 4) {
                    ForEach(0..<3) { i in
                        Circle().fill(Color(red: 0.55, green: 0.7, blue: 1)).frame(width: 6, height: 6)
                            .offset(y: -4 * max(0, sin(t * 6 - Double(i) * 0.8)))
                    }
                }
            case .speaking:
                HStack(alignment: .center, spacing: 3) {
                    ForEach(0..<5) { i in
                        Capsule().fill(Color(red: 0.55, green: 0.7, blue: 1))
                            .frame(width: 3, height: 5 + 13 * abs(sin(t * (5 + Double(i)) + Double(i) * 1.3)))
                    }
                }
            case .heard:
                Image(systemName: "text.bubble.fill").font(.system(size: 14)).foregroundColor(.white.opacity(0.8))
            case .message, .hidden:
                Image(systemName: "sparkles").font(.system(size: 14)).foregroundColor(.white.opacity(0.85))
            }
        }
    }
}

struct IslandView: View {
    @ObservedObject var m: Island
    let notch: CGSize
    var body: some View {
        let open = m.phase != .hidden
        let w: CGFloat = open ? 460 : notch.width
        let h: CGFloat = open ? notch.height + (m.text.count > 60 ? 74 : 56) : notch.height
        ZStack(alignment: .top) {
            IslandShape(radius: open ? 22 : 8)
                .fill(Color.black)
                .frame(width: w, height: h)
                .shadow(color: .black.opacity(open ? 0.4 : 0), radius: 14, y: 6)
            if open {
                HStack(spacing: 14) {
                    Indicator(phase: m.phase).frame(width: 30, height: 24)
                    VStack(alignment: .leading, spacing: 3) {
                        if !m.title.isEmpty {
                            Text(m.title).font(.system(size: 11, weight: .semibold)).foregroundColor(.white.opacity(0.55))
                        }
                        if !m.text.isEmpty {
                            Text(m.text).font(.system(size: 14, weight: .medium)).foregroundColor(.white)
                                .lineLimit(2).multilineTextAlignment(.leading)
                        }
                    }
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 20)
                .frame(width: w, height: h - notch.height)
                .offset(y: notch.height)
                .transition(.opacity.combined(with: .scale(scale: 0.96, anchor: .top)))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .contentShape(Rectangle())
        .onTapGesture { m.onTap() }
        .animation(.spring(response: 0.38, dampingFraction: 0.8), value: m.phase)
        .animation(.easeInOut(duration: 0.18), value: m.text)
    }
}

// MARK: - activity panel: the whole turn (what he asked, each step, the answer), opened from the island

struct StepInfo: Identifiable {
    let id: Int
    let label: String
    let status: String          // running | done | failed
    let seconds: Double?
}

struct TurnInfo {
    let id: Int
    let kind: String            // voice | phone | presence
    let title: String
    let prompt: String
    let elapsed: Double
    let active: Bool
    let headline: String
    let steps: [StepInfo]
    let reply: String
    let error: String?
    let branch: String

    init?(_ d: [String: Any]?) {
        guard let d, let id = d["id"] as? Int else { return nil }
        self.id = id
        kind = d["kind"] as? String ?? "voice"
        title = d["title"] as? String ?? "You asked"
        prompt = d["prompt"] as? String ?? ""
        elapsed = d["elapsed"] as? Double ?? 0
        active = d["active"] as? Bool ?? false
        headline = d["headline"] as? String ?? ""
        reply = d["reply"] as? String ?? ""
        error = d["error"] as? String
        branch = d["branch"] as? String ?? ""
        steps = ((d["steps"] as? [[String: Any]]) ?? []).enumerated().map { i, s in
            StepInfo(id: i, label: s["label"] as? String ?? "", status: s["status"] as? String ?? "done",
                     seconds: s["seconds"] as? Double)
        }
    }
}

struct BranchInfo: Identifiable, Equatable {
    let id: String
    let title: String
    let ago: String
    let current: Bool
}

struct PermissionInfo: Identifiable, Equatable {
    let id: Int
    let who: String
    let summary: String
    let detail: String
}

final class ActivityModel: ObservableObject {
    @Published var branches: [BranchInfo] = []
    @Published var permissions: [PermissionInfo] = []

    func updatePermissions(_ list: [[String: Any]]) {
        let items = list.compactMap { d -> PermissionInfo? in
            guard let id = d["id"] as? Int else { return nil }
            return PermissionInfo(id: id, who: d["who"] as? String ?? "", summary: d["summary"] as? String ?? "",
                                  detail: d["detail"] as? String ?? "")
        }
        if items != permissions { permissions = items }
    }

    func updateBranches(_ b: [String: Any]) {
        let list = ((b["branches"] as? [[String: Any]]) ?? []).compactMap { d -> BranchInfo? in
            guard let id = d["id"] as? String else { return nil }
            return BranchInfo(id: id, title: d["title"] as? String ?? id, ago: d["ago"] as? String ?? "",
                              current: d["current"] as? Bool ?? false)
        }
        if list != branches { branches = list }
    }

    @Published var turn: TurnInfo?
    @Published var saying: String?      // an alert or recap being spoken (not part of a turn)
    @Published var sayingTitle = ""

    func update(_ a: [String: Any]) {
        let t = TurnInfo(a["turn"] as? [String: Any])
        let sp = a["speaking"] as? [String: Any]
        let text = sp?["text"] as? String
        let source = sp?["source"] as? String ?? ""
        let newTitle = source.hasPrefix("alert:") ? String(source.dropFirst(6)) : (source == "recap" ? "While you were away" : "")
        if t?.id != turn?.id || t?.elapsed != turn?.elapsed || t?.reply != turn?.reply || t?.headline != turn?.headline
            || t?.steps.count != turn?.steps.count || t?.steps.last?.status != turn?.steps.last?.status { turn = t }
        if text != saying { saying = text }
        if newTitle != sayingTitle { sayingTitle = newTitle }
    }
}

func clock(_ s: Double) -> String {
    s < 60 ? String(format: s < 10 ? "%.1fs" : "%.0fs", s) : "\(Int(s) / 60)m \(Int(s) % 60)s"
}

let accent = Color(red: 0.55, green: 0.7, blue: 1)

struct StepRow: View {
    let step: StepInfo
    var body: some View {
        HStack(spacing: 10) {
            Group {
                switch step.status {
                case "running": ProgressView().controlSize(.small).scaleEffect(0.75)
                case "failed": Image(systemName: "xmark.circle.fill").foregroundColor(.red.opacity(0.85))
                default: Image(systemName: "checkmark.circle.fill").foregroundColor(Color(red: 0.45, green: 0.8, blue: 0.55))
                }
            }
            .frame(width: 16, height: 16)
            Text(step.label).font(.system(size: 13, weight: step.status == "running" ? .medium : .regular))
                .foregroundColor(.white.opacity(step.status == "running" ? 1 : 0.78))
                .lineLimit(1).truncationMode(.tail)
            Spacer(minLength: 6)
            if let s = step.seconds {
                Text(clock(s)).font(.system(size: 11, design: .monospaced)).foregroundColor(.white.opacity(0.38))
            }
        }
    }
}

struct ActivityView: View {
    @ObservedObject var m: ActivityModel
    var close: () -> Void
    var stop: () -> Void

    func icon(_ kind: String) -> String {
        switch kind { case "phone": return "iphone"; case "presence": return "hand.wave.fill"; default: return "waveform" }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: icon(m.turn?.kind ?? "voice")).font(.system(size: 12, weight: .semibold)).foregroundColor(accent)
                Text(m.turn?.title ?? "Friday").font(.system(size: 12, weight: .semibold)).foregroundColor(.white.opacity(0.62))
                if let b = m.turn?.branch, !b.isEmpty {
                    Text(b).font(.system(size: 11, weight: .semibold)).foregroundColor(accent)
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(Capsule().fill(accent.opacity(0.14)))
                }
                if let t = m.turn {
                    Text("· " + clock(t.elapsed)).font(.system(size: 12)).foregroundColor(.white.opacity(0.38))
                }
                Spacer()
                if m.turn?.active == true || m.saying != nil {
                    Button(action: stop) {
                        Label("Stop", systemImage: "stop.fill").font(.system(size: 11, weight: .semibold))
                            .padding(.horizontal, 9).padding(.vertical, 4)
                            .background(Capsule().fill(Color.white.opacity(0.12)))
                    }.buttonStyle(.plain).foregroundColor(.white)
                }
                Button(action: close) {
                    Image(systemName: "xmark").font(.system(size: 11, weight: .bold)).foregroundColor(.white.opacity(0.55))
                        .frame(width: 22, height: 22).background(Circle().fill(Color.white.opacity(0.08)))
                }.buttonStyle(.plain)
            }
            if m.branches.count > 1 {
                // Threads: tap one to keep talking there (the next turns go to it until you switch again).
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(m.branches) { b in
                            Button(action: { request("/branch", ["id": b.id]) }) {
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(b.title).font(.system(size: 11, weight: b.current ? .bold : .medium)).lineLimit(1)
                                    Text(b.ago).font(.system(size: 9)).foregroundColor(.white.opacity(0.4))
                                }
                                .padding(.horizontal, 9).padding(.vertical, 5)
                                .background(RoundedRectangle(cornerRadius: 8).fill(b.current ? accent.opacity(0.25) : Color.white.opacity(0.07)))
                            }.buttonStyle(.plain).foregroundColor(.white)
                        }
                    }
                }
            }
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        ForEach(m.permissions) { p in
                            // A Claude Code permission prompt: say "yes"/"no", or tap. VS Code's own dialog still works too.
                            VStack(alignment: .leading, spacing: 6) {
                                Text("\(p.who) wants to \(p.summary)").font(.system(size: 13, weight: .semibold)).foregroundColor(.white)
                                if !p.detail.isEmpty {
                                    Text(p.detail).font(.system(size: 11.5, design: .monospaced)).foregroundColor(.white.opacity(0.8))
                                        .lineLimit(6).textSelection(.enabled)
                                }
                                HStack(spacing: 8) {
                                    ForEach([("Allow", "allow", false), ("Always", "allow", true), ("Deny", "deny", false)], id: \.0) { label, decision, always in
                                        Button(action: { request("/permission/answer", ["id": p.id, "decision": decision, "always": always]) }) {
                                            Text(label).font(.system(size: 11.5, weight: .semibold))
                                                .padding(.horizontal, 10).padding(.vertical, 4)
                                                .background(Capsule().fill(decision == "deny" ? Color.red.opacity(0.35) : accent.opacity(0.35)))
                                        }.buttonStyle(.plain).foregroundColor(.white)
                                    }
                                }
                            }
                            .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                            .background(RoundedRectangle(cornerRadius: 10).fill(Color.orange.opacity(0.14)))
                        }
                        if let said = m.saying {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(m.sayingTitle.isEmpty ? "SAYING" : "SAYING · \(m.sayingTitle.uppercased())")
                                    .font(.system(size: 10, weight: .bold)).foregroundColor(accent.opacity(0.8))
                                Text(said).font(.system(size: 13.5)).foregroundColor(.white.opacity(0.92)).textSelection(.enabled)
                            }
                            .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                            .background(RoundedRectangle(cornerRadius: 10).fill(accent.opacity(0.1)))
                        }
                        if let t = m.turn {
                            Text(t.prompt).font(.system(size: 15, weight: .medium)).foregroundColor(.white)
                                .lineLimit(6).textSelection(.enabled)
                            if !t.steps.isEmpty {
                                VStack(alignment: .leading, spacing: 8) { ForEach(t.steps) { StepRow(step: $0) } }
                                    .padding(11).frame(maxWidth: .infinity, alignment: .leading)
                                    .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.06)))
                            } else if t.active {
                                HStack(spacing: 8) {
                                    ProgressView().controlSize(.small).scaleEffect(0.75)
                                    Text(t.headline).font(.system(size: 13)).foregroundColor(.white.opacity(0.6))
                                }
                            }
                            if !t.reply.isEmpty {
                                VStack(alignment: .leading, spacing: 5) {
                                    Text("FRIDAY").font(.system(size: 10, weight: .bold)).foregroundColor(.white.opacity(0.38))
                                    Text(t.reply).font(.system(size: 13.5)).foregroundColor(.white.opacity(0.92))
                                        .lineSpacing(2).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                                }
                            }
                            if let e = t.error {
                                Text(e).font(.system(size: 12)).foregroundColor(.red.opacity(0.85))
                            }
                        } else if m.saying == nil {
                            Text("Nothing yet. Hold Right ⌥ and ask me something.")
                                .font(.system(size: 13)).foregroundColor(.white.opacity(0.5))
                        }
                        Color.clear.frame(height: 1).id("end")
                    }
                }
                .onChange(of: m.turn?.reply) { _, _ in if m.turn?.active == true { proxy.scrollTo("end", anchor: .bottom) } }
                .onChange(of: m.turn?.steps.count) { _, _ in if m.turn?.active == true { proxy.scrollTo("end", anchor: .bottom) } }
            }
        }
        .padding(16)
        .frame(width: 440, height: 440, alignment: .top)
        .background(RoundedRectangle(cornerRadius: 18).fill(Color(white: 0.07)))
        .overlay(RoundedRectangle(cornerRadius: 18).stroke(Color.white.opacity(0.09), lineWidth: 1))
        .environment(\.colorScheme, .dark)
    }
}

final class KeyPanel: NSPanel { override var canBecomeKey: Bool { true } }

// MARK: - app

final class Ears: NSObject, NSApplicationDelegate, NSTextFieldDelegate {
    let island = Island()
    let activity = ActivityModel()
    var detailPanel: KeyPanel!
    let supervisor = Supervisor()
    let camera = CameraWatch()
    var lastFacePost = Date.distantPast
    var cameraEnabled: Bool {
        get { UserDefaults.standard.object(forKey: "cameraEnabled") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "cameraEnabled") }
    }
    var panel: KeyPanel!
    var field: NSTextField!            // paste target for Wispr (it usually shows its copy bubble instead)
    var status: NSStatusItem!
    var tap: CFMachPort?
    var ready = false, quitting = false
    var voiceWhere = ""

    var optionDown = false, otherKey = false, listening = false, waiting = false
    var holdTimer: Timer?, settleTimer: Timer?, pollTimer: Timer?, hideTimer: Timer?
    var releasedAt = Date(), pressedAt = Date(), askedAt: Date?
    var keyTimes: [Double] = []
    let followUp = FollowUp()
    var wasSpeaking = false
    var lastSpeechSource = ""
    var lastTypingSent = -1
    var previousApp: NSRunningApplication?

    func applicationDidFinishLaunching(_ note: Notification) {
        try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
        buildStatusItem()
        buildPanel()
        island.onTap = { [weak self] in self?.tapped() }
        followUp.onHeard = { [weak self] partial in self?.show(.listening, "Listening", partial) }
        followUp.done = { [weak self] said in
            guard let self else { return }
            if let said {
                self.waiting = true
                self.submit(said)
            } else if self.island.phase == .listening {
                self.hide(after: 0.2)
            }
        }
        camera.onFace = { [weak self] in
            DispatchQueue.main.async {
                guard let self, Date().timeIntervalSince(self.lastFacePost) >= 2 else { return }
                self.lastFacePost = Date()
                request("/presence", ["face": true], timeout: 1)
            }
        }
        let trusted = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
        if !CGPreflightListenEventAccess() { CGRequestListenEventAccess() }
        log("started; accessibility=\(trusted) inputMonitoring=\(CGPreflightListenEventAccess())")
        installTap()
        show(.message, "Friday", "Starting Friday…")
        DispatchQueue.global().async {
            let ok = self.supervisor.start { msg in
                DispatchQueue.main.async { if !self.ready { self.show(.message, "Friday", msg) } }
            }
            DispatchQueue.main.async {
                self.ready = ok
                self.updateMenu()
                if ok { self.flash("Friday", "Ready. Hold Right ⌥ to talk.") }
                else { self.flash("Friday", "Couldn't start. See ~/.friday/logs.", for: 5) }
                Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { _ in self.pollStatus() }
                Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in self.checkIdle() }
                Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { _ in self.reportTyping() }
                Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in
                    DispatchQueue.global().async { if !self.quitting { self.supervisor.ensureLaptopVoice() } }
                }
            }
        }
    }

    /// How hard he's typing (keys in the last minute), so fridayd can hold routine alerts until he pauses.
    func reportTyping() {
        let cutoff = Date().timeIntervalSince1970 - 60
        keyTimes.removeAll { $0 < cutoff }
        let n = keyTimes.count
        guard abs(n - lastTypingSent) >= 10 || (n == 0) != (lastTypingSent == 0) else { return }
        lastTypingSent = n
        request("/presence", ["typing": n], timeout: 1)
    }

    /// Camera on only while there's been no keyboard/mouse input for 4 minutes; off at the first touch.
    func checkIdle() {
        let any = CGEventType(rawValue: ~UInt32(0))!
        let idle = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: any)
        if cameraEnabled && idle >= 240 { camera.start() } else if idle < 5 || !cameraEnabled { camera.stop() }
    }

    @objc func toggleCamera() {
        cameraEnabled.toggle()
        if !cameraEnabled { camera.stop() }
        updateMenu()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !quitting else { return .terminateNow }
        quitting = true
        camera.stop()
        show(.message, "Friday", "Shutting down…")
        DispatchQueue.global().async {
            self.supervisor.stop()
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    // MARK: menu bar

    func buildStatusItem() {
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        status.button?.image = NSImage(systemSymbolName: "waveform", accessibilityDescription: "Friday")
        updateMenu()
    }

    func updateMenu() {
        let menu = NSMenu()
        let place = voiceWhere == "laptop" ? " · voice on laptop" : voiceWhere == "mac" ? " · voice on Mac" : ""
        menu.addItem(withTitle: (ready ? "Friday is running" : "Friday is starting…") + place, action: nil, keyEquivalent: "")
        menu.addItem(withTitle: "Hold Right ⌥ to talk", action: nil, keyEquivalent: "")
        menu.addItem(.separator())
        let show = NSMenuItem(title: "Show what Friday is doing", action: #selector(toggleDetail), keyEquivalent: "")
        show.target = self
        menu.addItem(show)
        let hush = NSMenuItem(title: "Stop speaking", action: #selector(hushNow), keyEquivalent: "")
        hush.target = self
        menu.addItem(hush)
        let cam = NSMenuItem(title: "Use camera when I'm idle (on-device)", action: #selector(toggleCamera), keyEquivalent: "")
        cam.target = self
        cam.state = cameraEnabled ? .on : .off
        menu.addItem(cam)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Friday", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        status.menu = menu
    }

    @objc func hushNow() { request("/stop", [:]) }

    func tapped() { toggleDetail() }

    @objc func toggleDetail() {
        if detailPanel.isVisible { detailPanel.orderOut(nil) } else { detailPanel.orderFrontRegardless() }
    }

    // MARK: island

    func buildPanel() {
        let screen = NSScreen.main ?? NSScreen.screens[0]
        let notch = notchSize(screen)
        let size = NSSize(width: 520, height: notch.height + 110)
        panel = KeyPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel],
                         backing: .buffered, defer: false)
        panel.level = NSWindow.Level(rawValue: NSWindow.Level.mainMenu.rawValue + 3)   // over the menu bar, at the notch
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        panel.setFrameOrigin(NSPoint(x: screen.frame.midX - size.width / 2, y: screen.frame.maxY - size.height))
        let host = NSHostingView(rootView: IslandView(m: island, notch: notch))
        host.frame = NSRect(origin: .zero, size: size)
        let container = NSView(frame: host.frame)
        container.addSubview(host)
        field = NSTextField(frame: NSRect(x: 0, y: 0, width: 4, height: 4))
        field.alphaValue = 0.01
        field.delegate = self
        container.addSubview(field)
        panel.contentView = container

        let dsize = NSSize(width: 440, height: 440)
        detailPanel = KeyPanel(contentRect: NSRect(origin: .zero, size: dsize), styleMask: [.borderless, .nonactivatingPanel],
                               backing: .buffered, defer: false)
        detailPanel.level = panel.level
        detailPanel.isOpaque = false
        detailPanel.backgroundColor = .clear
        detailPanel.hasShadow = true
        detailPanel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        detailPanel.setFrameOrigin(NSPoint(x: screen.frame.midX - dsize.width / 2,
                                           y: screen.frame.maxY - notch.height - 84 - dsize.height))   // under the open island
        let dhost = NSHostingView(rootView: ActivityView(m: activity, close: { [weak self] in self?.detailPanel.orderOut(nil) },
                                                         stop: { [weak self] in request("/stop", [:]); self?.hide(after: 0.2) }))
        dhost.frame = NSRect(origin: .zero, size: dsize)
        detailPanel.contentView = dhost
    }

    func notchSize(_ screen: NSScreen) -> CGSize {
        let top = screen.safeAreaInsets.top
        if top > 0, let l = screen.auxiliaryTopLeftArea, let r = screen.auxiliaryTopRightArea {
            return CGSize(width: screen.frame.width - l.width - r.width, height: top)
        }
        return CGSize(width: 180, height: 6)   // no notch: a thin tab at the top centre
    }

    func show(_ phase: Island.Phase, _ title: String = "", _ text: String = "") {
        hideTimer?.invalidate()
        if !panel.isVisible { panel.orderFrontRegardless() }
        island.set(phase, title, text)
    }

    func flash(_ title: String, _ text: String, for seconds: TimeInterval = 2.2) {
        show(.message, title, text)
        hide(after: seconds)
    }

    func hide(after delay: TimeInterval) {
        hideTimer?.invalidate()
        hideTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { _ in
            guard !self.listening && !self.waiting && !self.quitting else { return }
            self.island.set(.hidden)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) {
                if self.island.phase == .hidden { self.panel.orderOut(nil) }
            }
        }
    }

    /// Mirror fridayd: "thinking" while Friday works on an answer, live captions while she speaks
    /// (also for alerts she raises on her own).
    func pollStatus() {
        guard ready, !quitting else { return }
        request("/status", timeout: 1) { json in
            if let a = json?["activity"] as? [String: Any] { self.activity.update(a) }
            if let b = json?["branches"] as? [String: Any] { self.activity.updateBranches(b) }
            self.activity.updatePermissions(json?["permissions"] as? [[String: Any]] ?? [])
            // Follow-up window: when Friday stops talking after answering him (or asking him something), listen.
            let talking = (json?["voice"] as? [String: Any])?["speaking"] as? Bool ?? false
            let busy = ((json?["voice"] as? [String: Any])?["thinking"] as? Bool ?? false) || self.activity.turn?.active == true
            let wantsAnswer = json?["listen"] as? Bool ?? false
            if talking, let src = (json?["voice"] as? [String: Any])?["source"] as? String { self.lastSpeechSource = src }
            let answeredHim = self.lastSpeechSource.hasPrefix("voice:") && self.activity.turn?.kind == "voice"
            if self.wasSpeaking && !talking && !busy && !self.listening && !self.waiting && !self.followUp.active
                && (wantsAnswer || answeredHim) {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {   // let the last word leave the speakers
                    guard !self.listening, !self.followUp.active else { return }
                    self.show(.listening, "Listening", "Go ahead, or just let it be")
                    self.followUp.start(window: wantsAnswer ? 10 : 6)
                }
            }
            self.wasSpeaking = talking
            guard !self.listening, !self.waiting, let voice = json?["voice"] as? [String: Any] else { return }
            let speaking = voice["speaking"] as? Bool ?? false
            // Friday's turn is on until its answer has been spoken (the text can finish before the voice starts)
            let thinking = (voice["thinking"] as? Bool ?? false) || self.activity.turn?.active == true
            let line = (voice["line"] as? String) ?? ""
            let source = (voice["source"] as? String) ?? ""
            if let place = voice["engine"] as? String, place != self.voiceWhere { self.voiceWhere = place; self.updateMenu() }
            if self.listening || self.waiting { return }
            if speaking {
                let title = source.hasPrefix("alert:") ? "Friday · \(source.dropFirst(6))" : "Speaking"
                if self.island.phase != .speaking || self.island.text != line || self.island.title != title {
                    self.show(.speaking, title, line)
                }
            } else if thinking {
                if self.island.phase == .heard, let a = self.askedAt, Date().timeIntervalSince(a) < 0.9 { return }  // his words first
                let t = self.activity.turn
                let headline = t?.headline ?? "Thinking…"
                let thread = t?.branch ?? ""
                let title = "\(thread.isEmpty || thread == "General" ? "Friday" : thread) · \(Int(t?.elapsed ?? 0))s"
                if self.island.phase != .thinking || self.island.text != headline || self.island.title != title {
                    self.show(.thinking, title, headline)
                }
            } else if [.speaking, .thinking].contains(self.island.phase) {
                if let a = self.askedAt, Date().timeIntervalSince(a) < 1.5 { return }  // answer not started yet
                if self.hideTimer?.isValid != true { self.hide(after: 0.6) }   // schedule once; polls run every 0.3 s
            }
        }
    }

    // MARK: keyboard

    func installTap() {
        let mask = (1 << CGEventType.flagsChanged.rawValue) | (1 << CGEventType.keyDown.rawValue)
        let me = Unmanaged.passUnretained(self).toOpaque()
        // Active tap: Right Option's own flagsChanged events are consumed, so other apps (Wispr above all)
        // never see it held next to fn. Keys pressed while it is held still carry the ⌥ flag.
        tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .defaultTap,
                                eventsOfInterest: CGEventMask(mask), callback: { _, type, event, ctx in
            let ears = Unmanaged<Ears>.fromOpaque(ctx!).takeUnretainedValue()
            return ears.handle(type, event) ? nil : Unmanaged.passUnretained(event)
        }, userInfo: me)
        guard let tap else {
            log("event tap failed: grant Input Monitoring and Accessibility to FridayEars")
            Timer.scheduledTimer(withTimeInterval: 3, repeats: false) { _ in self.installTap() }
            return
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), CFMachPortCreateRunLoopSource(nil, tap, 0), .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        log("listening for Right Option")
    }

    /// Returns true to swallow the event.
    func handle(_ type: CGEventType, _ event: CGEvent) -> Bool {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return false
        }
        if event.getIntegerValueField(.eventSourceUserData) == ourEvent { return false }
        let code = event.getIntegerValueField(.keyboardEventKeycode)
        if type == .keyDown {
            keyTimes.append(Date().timeIntervalSince1970)   // counted only (how hard he's typing), never recorded
            if optionDown { otherKey = true; if listening { cancel("another key pressed") } }
            return false
        }
        guard type == .flagsChanged else { return false }
        if code == kRightOption {
            (event.flags.rawValue & rightAltMask) != 0 ? optionPressed() : optionReleased()
            return true
        } else if optionDown && Int(code) != Int(kFn) {
            otherKey = true
            if listening { cancel("modifier combo") }
        }
        return false
    }

    func optionPressed() {
        guard !optionDown else { return }
        optionDown = true; otherKey = false
        holdTimer?.invalidate()
        holdTimer = Timer.scheduledTimer(withTimeInterval: holdDelay, repeats: false) { _ in
            if self.optionDown && !self.otherKey { self.startListening() }
        }
    }

    func optionReleased() {
        optionDown = false
        holdTimer?.invalidate()
        if listening { stopListening() }
    }

    // MARK: a turn

    func startListening() {
        guard ready else { flash("Friday", "Still starting… one moment."); return }
        if followUp.active { followUp.cancel() }   // the key wins over the follow-up window
        listening = true; waiting = false; askedAt = nil
        pressedAt = Date()
        request("/stop", [:])                             // talking over Friday interrupts her
        let front = NSWorkspace.shared.frontmostApplication
        if front?.bundleIdentifier != Bundle.main.bundleIdentifier { previousApp = front }
        field.stringValue = ""
        show(.listening, "Listening", "Release Right ⌥ when you're done")
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(field)
        key(kFn, down: true, flags: .maskSecondaryFn, asFlagsChanged: true)   // Wispr push-to-talk
        log("listening (from \(previousApp?.localizedName ?? "?"))")
    }

    func stopListening() {
        listening = false; waiting = true
        releasedAt = Date()
        key(kFn, down: false, asFlagsChanged: true)
        show(.transcribing, "Transcribing", "")
        panel.makeFirstResponder(field)
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { _ in self.poll() }
    }

    /// After release, wait for Wispr to finish *this* dictation and read its text straight from Wispr's
    /// own history (flow.sqlite → History): the row that started when Right Option went down. No
    /// clipboard: "copy last text" returned the previous transcript, sometimes one meant for another app.
    func poll() {
        guard waiting else { pollTimer?.invalidate(); return }
        let since = Date().timeIntervalSince(releasedAt)
        switch wisprTranscript(startedAfter: pressedAt.addingTimeInterval(-1.5)) {
        case .text(let said):
            log(String(format: "Wispr transcript after %.1fs", since))
            submit(said)
        case .nothing(let status):
            log("Wispr heard nothing (\(status))")
            finish("Didn't catch that.")
        case .pending:
            if since > 25 { log("gave up: Wispr didn't finish within 25 s"); finish("Wispr didn't finish transcribing.") }
        }
    }

    enum WisprResult { case pending, text(String), nothing(String) }

    func wisprTranscript(startedAfter start: Date) -> WisprResult {
        let path = "\(home)/Library/Application Support/Wispr Flow/flow.sqlite"
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { sqlite3_close(db); return .pending }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 200)
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX"); f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
        let sql = "SELECT status, formattedText, asrText FROM History WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT 1"
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return .pending }
        defer { sqlite3_finalize(stmt) }
        let since = f.string(from: start) + " +00:00"
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        sqlite3_bind_text(stmt, 1, since, -1, transient)
        guard sqlite3_step(stmt) == SQLITE_ROW else { return .pending }
        func col(_ i: Int32) -> String { sqlite3_column_text(stmt, i).map { String(cString: $0) } ?? "" }
        let status = col(0), formatted = col(1), asr = col(2)
        switch status {
        case "formatted":
            let text = (formatted.isEmpty ? asr : formatted).trimmingCharacters(in: .whitespacesAndNewlines)
            return text.isEmpty ? .nothing("empty") : .text(text)
        case "empty", "no_audio", "dismissed": return .nothing(status)
        default: return .pending          // still recording / transcribing / formatting
        }
    }

    func typed() -> String { field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines) }

    func finish(_ message: String) {
        waiting = false
        pollTimer?.invalidate(); settleTimer?.invalidate()
        restoreFocus()
        flash("Friday", message, for: 1.6)
    }

    func cancel(_ why: String) {
        log("cancelled: \(why)")
        listening = false; waiting = false
        key(kFn, down: false, asFlagsChanged: true)
        pollTimer?.invalidate(); settleTimer?.invalidate()
        restoreFocus()
        hide(after: 0)
    }

    func controlTextDidChange(_ note: Notification) {}   // Wispr's paste is ignored; the History row is the source

    func submit(_ raw: String) {
        let said = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard waiting, !said.isEmpty else { return }
        waiting = false
        pollTimer?.invalidate()
        askedAt = Date()
        request("/ask", ["text": said])
        log("asked: \(said.prefix(120))")
        show(.heard, "You", said)
        restoreFocus()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) {
            if self.island.phase == .heard { self.show(.thinking, "Friday · 1s", "Thinking…") }
        }
    }

    func restoreFocus() {
        panel.resignKey()
        previousApp?.activate()
    }
}

if CommandLine.arguments.contains("--camera-test") {   // grant the camera once and check face detection
    let cam = CameraWatch()
    var faces = 0
    cam.onFace = { faces += 1 }
    cam.start()
    RunLoop.main.run(until: Date().addingTimeInterval(8))
    cam.stop()
    log("camera test: \(faces) face detections in ~6 s")
    Thread.sleep(forTimeInterval: 0.5)
    exit(0)
}

let app = NSApplication.shared
let ears = Ears()
app.delegate = ears
app.setActivationPolicy(.accessory)
app.run()
