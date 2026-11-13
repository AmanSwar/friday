// Talking to Friday: hold Right Option (Wispr Flow does the speech recognition), double-tap it for the
// panel, and the follow-up window (Apple's recognizer listens a few seconds after Friday answers).
//
// Needs Accessibility (press fn, consume Right Option) and Input Monitoring (see Right Option).

import AVFoundation
import Cocoa
import Speech
import SQLite3

// MARK: - mic level (the listening island's bars follow your voice)

final class MicMeter {
    private let mic = MicCapture()
    private var last = Date.distantPast
    var onLevel: ((Float) -> Void)?       // 0…1, main thread, ~20 Hz

    func start() {
        mic.setHandler { [weak self] _, db in
            guard let self, Date().timeIntervalSince(self.last) > 0.05 else { return }
            self.last = Date()
            DispatchQueue.main.async { self.onLevel?(MicCapture.level(db)) }
        }
        mic.start(Mic.resolve().device)
    }

    func stop() {
        mic.stop()
        DispatchQueue.main.async { self.onLevel?(0) }
    }
}

// MARK: - follow-up window: after Friday answers, listen without the key

/// Apple's speech recognizer (on-device when this Mac supports it), separate from Wispr. Opens after Friday
/// finishes answering a push-to-talk turn or asks something that needs an answer (a permission prompt).
/// Up to `window` seconds for him to start (a few more if the mic hears him but no words are back yet).
/// Once he's talking it ends when no new words have come for 2.8 s and the mic has been quiet for 1.2 s
/// (words decide; background noise alone can't hold it open more than 6 s past his last word), then takes
/// the recognizer's final text.
///
/// The recognizer starts its transcript over after each pause, so each stretch is kept and the next one
/// appended (before 13 Nov only the last stretch survived: 30 s of talking reached Friday as "In case of
/// Anthropic"). Server recognition when available (more accurate than on-device), with punctuation and his
/// vocabulary as hints.
final class FollowUp {
    private let mic = MicCapture()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var tick: Timer?, finalWait: Timer?
    private var text = ""                   // everything he's said in this window
    private var committed = ""              // earlier stretches (the recognizer starts over after a pause)
    private var current = ""                // the stretch being recognized now
    private var stretches = 0
    private var window: TimeInterval = 8
    private var openedAt = Date(), lastVoice: Date?, lastWords: Date?
    private var floor: Float?               // the room's noise floor (dB), tracked while listening
    private var lastLevelAt = Date.distantPast
    private var ending = false
    private(set) var active = false
    var onHeard: ((String) -> Void)?        // partial text, for the island
    var onTalking: ((Bool) -> Void)?        // he started talking (true) / the window opened (false)
    var onLevel: ((Float) -> Void)?         // 0…1 for the listening bars
    var done: ((String?) -> Void)?          // final text, or nil when nothing was said

    static let pauseToEnd: TimeInterval = 2.8     // no new words this long (and a quiet mic): he's done (a thinking pause is ~2 s)
    static let noiseLimit: TimeInterval = 6.0     // noise can't keep it open longer than this past his last word
    static let vocabulary = ["Friday", "Anthropic", "Claude", "SGLang", "vLLM", "AWS", "Trainium", "master",
                             "master_aws", "master_sglang", "dev1", "dev2", "Wispr", "IPO", "S-1", "GPU", "B200"]
    static let maxLength: TimeInterval = 45

    @discardableResult
    func start(window: TimeInterval = 8) -> Bool {
        guard !active else { return false }
        guard let recognizer, recognizer.isAvailable else { log("follow-up: speech recognizer unavailable"); return false }
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: break
        case .notDetermined:
            log("follow-up: asking for Speech Recognition permission")
            SFSpeechRecognizer.requestAuthorization { _ in }
            return false
        default:
            log("follow-up: Speech Recognition not allowed (System Settings → Privacy & Security → Speech Recognition)")
            return false
        }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.requiresOnDeviceRecognition = false    // let Apple use its server when it can: far fewer wrong words
        req.addsPunctuation = true
        req.contextualStrings = Self.vocabulary
        let (device, why) = Mic.resolve()
        mic.setHandler { [weak self] buf, db in
            req.appendAudioSampleBuffer(buf)
            guard let self, Date().timeIntervalSince(self.lastLevelAt) > 0.05 else { return }
            self.lastLevelAt = Date()
            DispatchQueue.main.async { self.heard(db) }
        }
        mic.start(device) { [weak self] ok in
            guard let self, self.active else { return }
            if ok {
                log("follow-up: listening up to \(Int(window))s on \(self.mic.deviceName) (\(why))")
            } else {
                log("follow-up: the mic didn't start")
                self.complete()
            }
        }
        request = req; text = ""; committed = ""; current = ""; stretches = 0; self.window = window
        openedAt = Date(); lastVoice = nil; lastWords = nil; floor = nil; ending = false; active = true
        onTalking?(false)
        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
            DispatchQueue.main.async {
                guard let self, self.active else { return }
                if let result {
                    let t = result.bestTranscription.formattedString
                    if !t.isEmpty && t != self.current {
                        if self.lastWords == nil { self.onTalking?(true) }
                        if !self.current.isEmpty && Self.startsOver(t, after: self.current) {
                            self.committed = self.text          // he paused: keep what he said, add the next stretch
                            self.stretches += 1
                        }
                        self.current = t
                        self.text = [self.committed, t].filter { !$0.isEmpty }.joined(separator: " ")
                        self.lastWords = Date()
                        self.onHeard?(self.text)
                    }
                    if result.isFinal && self.ending { self.complete() }
                } else if let error {
                    if !self.ending { log("follow-up: recognizer stopped: \(error.localizedDescription)") }
                    self.complete()
                }
            }
        }
        tick = commonTimer(0.1, repeats: true) { [weak self] _ in self?.check() }
        return true
    }

    /// Mic level, ~20 times a second: the bars, and "he may have started" (well above the room's level).
    private func heard(_ db: Float) {
        guard active, !ending else { return }
        onLevel?(MicCapture.level(db))
        guard let f = floor else { floor = db; return }
        if db < f + 6 { floor = f * 0.95 + db * 0.05 }   // the room's level, from the quieter moments
        if db > max(f + 15, -45) { lastVoice = Date() }
    }

    private func check() {
        guard active, !ending else { return }
        let now = Date()
        if let words = lastWords {                       // he's talking: done after a real pause
            let sinceWords = now.timeIntervalSince(words)
            let sinceVoice = lastVoice.map { now.timeIntervalSince($0) } ?? 99
            if sinceWords > Self.pauseToEnd && sinceVoice > 1.2 { end("he stopped talking") }
            else if sinceWords > Self.noiseLimit { end("no words for \(Int(Self.noiseLimit))s") }
            else if now.timeIntervalSince(openedAt) > Self.maxLength { end("45 s is the limit") }
            return
        }
        // Not a word yet. Close at the window, unless the mic heard something loud in the last 2 s (words
        // take a moment to come back), and never more than 4 s past the window.
        let late = now.timeIntervalSince(openedAt) - window
        guard late > 0 else { return }
        let maybeTalking = lastVoice.map { now.timeIntervalSince($0) < 2 } ?? false
        if !maybeTalking || late > 4 { end("nothing said in \(Int(window))s") }
    }

    /// Stop the audio and give the recognizer a moment to settle on its final words.
    private func end(_ why: String) {
        guard active, !ending else { return }
        ending = true
        tick?.invalidate()
        mic.stop()
        request?.endAudio()
        if text.isEmpty && lastWords == nil {
            log("follow-up: \(why)")
            complete()
            return
        }
        finalWait = commonTimer(1.0, repeats: false) { [weak self] _ in self?.complete() }
    }

    /// The recognizer started a new stretch (after a pause) rather than revising the current one: a
    /// revision keeps the start ("I'll by" -> "I'll buy the"); a new stretch starts with other words and
    /// is shorter than what came before.
    static func startsOver(_ next: String, after cur: String) -> Bool {
        let a = next.lowercased().split(separator: " "), b = cur.lowercased().split(separator: " ")
        guard let a0 = a.first, let b0 = b.first else { return false }
        let firstTwoA = a.prefix(2).map(String.init), firstThreeB = Set(b.prefix(3).map(String.init))
        // shorter than before, or the earlier stretch was a word or two ("No." then "check AWS instead")
        return a0 != b0 && !firstTwoA.contains(where: firstThreeB.contains) && (next.count < cur.count || b.count <= 2)
    }

    func cancel() {
        guard active else { return }
        stop()
        done?(nil)
    }

    /// Friday started talking while the window was open: what he'd said before that is sent (it's his);
    /// with nothing said yet, the window just closes (anything now would be her voice through the speaker).
    func yieldToFriday() {
        guard active else { return }
        if text.isEmpty { cancel() } else { end("Friday started talking") }
    }

    private func complete() {
        guard active else { return }
        let said = text.trimmingCharacters(in: .whitespacesAndNewlines)
        stop()
        log(said.isEmpty ? "follow-up: heard nothing" : "follow-up: heard \(said.count) chars in \(stretches + 1) stretch(es)")
        done?(said.isEmpty ? nil : said)
    }

    private func stop() {
        guard active else { return }
        active = false
        tick?.invalidate(); finalWait?.invalidate()
        mic.stop()
        mic.setHandler(nil)
        request?.endAudio()
        task?.cancel()
        request = nil; task = nil
        onLevel?(0)
    }
}

// MARK: - paste sink: Wispr pastes into whatever has focus, so during a push-to-talk this tiny,
// invisible window holds it (the transcript itself is read from Wispr's history, not the paste).

final class PasteSink {
    let window: KeyPanel
    let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 4, height: 4))

    init() {
        window = KeyPanel(contentRect: NSRect(x: 0, y: 0, width: 4, height: 4), styleMask: [.borderless, .nonactivatingPanel],
                          backing: .buffered, defer: false)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.alphaValue = 0.02
        window.hasShadow = false
        window.ignoresMouseEvents = true
        window.level = NSWindow.Level(rawValue: NSWindow.Level.mainMenu.rawValue + 1)
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle, .stationary]
        let container = NSView(frame: NSRect(x: 0, y: 0, width: 4, height: 4))
        container.addSubview(field)
        window.contentView = container
    }

    func focus(on screen: NSScreen) {
        window.setFrameOrigin(NSPoint(x: screen.frame.minX, y: screen.frame.maxY - 4))
        field.stringValue = ""
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(field)
    }

    func release() {
        window.orderOut(nil)
    }
}

// MARK: - push-to-talk

final class PushToTalk {
    let model: AppModel
    let followUp = FollowUp()
    let meter = MicMeter()
    let sink = PasteSink()
    var onDoubleTap: () -> Void = {}
    var panelIsOpen: () -> Bool = { false }
    var refocusPanel: () -> Void = {}
    private(set) var keyTimes: [Double] = []     // key presses (counted only, never recorded): how hard he types

    private var tap: CFMachPort?
    private var optionDown = false, otherKey = false
    private var listening = false, waiting = false
    private var holdTimer: Timer?, pollTimer: Timer?, watchdog: Timer?
    private var pressedAt = Date(), releasedAt = Date(), lastTapAt = Date.distantPast, listenedAt = Date()
    private var previousApp: NSRunningApplication?
    private var pollInFlight = false
    private var sinkHeld = false            // the paste sink has focus (a push-to-talk is in progress)
    private var panelWasKey = false         // the panel had focus when the push-to-talk started
    private let wisprQueue = DispatchQueue(label: "friday.wispr")

    init(model: AppModel) {
        self.model = model
        meter.onLevel = { [weak model] level in model?.micLevel = level }
        followUp.onHeard = { [weak self] partial in
            guard let self, case .followUp(let st, let w, _, let fp) = self.model.local else { return }
            self.model.local = .followUp(started: st, window: w, partial: partial, forPermission: fp)
        }
        followUp.onLevel = { [weak model] level in model?.micLevel = level }
        followUp.done = { [weak self] said in
            guard let self else { return }
            request("/mic", ["open": false], timeout: 1)
            if let said {
                self.waiting = true
                self.submit(said, reply: true)
            } else if case .followUp = self.model.local {
                self.model.local = .idle
            }
        }
    }

    func countTyping(window: Double = 60) -> Int {
        let cutoff = Date().timeIntervalSince1970 - window
        keyTimes.removeAll { $0 < cutoff }
        return keyTimes.count
    }

    // --- the event tap -----------------------------------------------------------------------------

    func install() {
        let mask = (1 << CGEventType.flagsChanged.rawValue) | (1 << CGEventType.keyDown.rawValue)
        let me = Unmanaged.passUnretained(self).toOpaque()
        // Active tap: Right Option's own flagsChanged events are consumed, so other apps (Wispr above all)
        // never see it held next to fn. Keys pressed while it is held still carry the ⌥ flag.
        tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .defaultTap,
                                eventsOfInterest: CGEventMask(mask), callback: { _, type, event, ctx in
            let ptt = Unmanaged<PushToTalk>.fromOpaque(ctx!).takeUnretainedValue()
            return ptt.handle(type, event) ? nil : Unmanaged.passUnretained(event)
        }, userInfo: me)
        guard let tap else {
            log("event tap failed: grant Input Monitoring and Accessibility to FridayEars")
            commonTimer(3, repeats: false) { _ in self.install() }
            return
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), CFMachPortCreateRunLoopSource(nil, tap, 0), .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        log("listening for Right Option")
        watchdog = commonTimer(5, repeats: true) { [weak self] _ in self?.checkKey() }
    }

    /// Returns true to swallow the event.
    private func handle(_ type: CGEventType, _ event: CGEvent) -> Bool {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return false
        }
        if event.getIntegerValueField(.eventSourceUserData) == ourEvent { return false }
        let code = event.getIntegerValueField(.keyboardEventKeycode)
        // A missed Right Option release: any other key event carries the current modifiers, and if Right
        // Option isn't among them it was let go. (Asking macOS for the key's state doesn't work: it says
        // "up" while it's held, because this tap consumes its events.)
        if listening && code != kRightOption && (event.flags.rawValue & (rightAltMask | CGEventFlags.maskAlternate.rawValue)) == 0 {
            log("Right Option's release was missed; sending what was said")
            optionReleased()
        }
        if type == .keyDown {
            keyTimes.append(Date().timeIntervalSince1970)
            if code == kEscape && followUp.active { followUp.cancel() }
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

    /// Safety net: nobody holds the key for two minutes; stop and send what was said.
    private func checkKey() {
        guard listening, Date().timeIntervalSince(listenedAt) > 120 else { return }
        log("listening for over 2 minutes; sending what was said")
        optionReleased()
    }

    private func optionPressed() {
        guard !optionDown else { return }
        optionDown = true; otherKey = false
        pressedAt = Date()
        holdTimer?.invalidate()
        holdTimer = commonTimer(holdDelay, repeats: false) { _ in
            if self.optionDown && !self.otherKey { self.startListening() }
        }
    }

    private func optionReleased() {
        let held = Date().timeIntervalSince(pressedAt)
        optionDown = false
        holdTimer?.invalidate()
        if listening {
            stopListening()
        } else if !otherKey && held < holdDelay {      // a tap; two quick taps open the panel
            if Date().timeIntervalSince(lastTapAt) < doubleTapWindow {
                lastTapAt = .distantPast
                onDoubleTap()
            } else {
                lastTapAt = Date()
            }
        }
    }

    // --- a turn ------------------------------------------------------------------------------------

    private func startListening() {
        guard model.ready else { model.flash("Friday", "Still starting… one moment."); return }
        if followUp.active { followUp.cancel() }       // the key wins over the follow-up window
        listening = true; waiting = false
        listenedAt = Date()
        model.local = .listening(Date())
        // Talking over Friday silences her. Only her voice: what she's working on carries on until his new
        // question arrives (a push-to-talk that ends in nothing used to throw her answer away).
        request("/hush", [:])
        let front = NSWorkspace.shared.frontmostApplication
        previousApp = front?.bundleIdentifier == Bundle.main.bundleIdentifier ? nil : front
        panelWasKey = previousApp == nil && panelIsOpen()
        sink.focus(on: IslandController.mouseScreen())
        sinkHeld = true
        key(kFn, down: true, flags: .maskSecondaryFn, asFlagsChanged: true)   // Wispr push-to-talk
        meter.start()
        request("/mic", ["open": true], timeout: 1)                          // music down while he talks
        log("listening (from \(front?.localizedName ?? "?"))")
    }

    private func stopListening() {
        listening = false; waiting = true
        releasedAt = Date()
        key(kFn, down: false, asFlagsChanged: true)
        meter.stop()
        request("/mic", ["open": false], timeout: 1)
        model.local = .transcribing(Date())
        pollTimer?.invalidate()
        pollTimer = commonTimer(0.25, repeats: true) { _ in self.poll() }
    }

    /// After release, wait for Wispr to finish *this* dictation and read its text straight from Wispr's
    /// own history (flow.sqlite → History): the row that started when Right Option went down. Read off
    /// the main thread, so a slow database never stalls the key tap.
    private func poll() {
        guard waiting else { pollTimer?.invalidate(); return }
        guard !pollInFlight else { return }
        pollInFlight = true
        let since = pressedAt.addingTimeInterval(-1.5)
        let elapsed = Date().timeIntervalSince(releasedAt)
        wisprQueue.async {
            let result = Self.wisprTranscript(startedAfter: since)
            DispatchQueue.main.async {
                self.pollInFlight = false
                guard self.waiting else { return }
                switch result {
                case .text(let said):
                    log(String(format: "Wispr transcript after %.1fs", elapsed))
                    self.submit(said)
                case .nothing(let status):
                    log("Wispr heard nothing (\(status))")
                    self.finish("Didn't catch that.")
                case .missing:
                    // no recording at all: Wispr isn't running or didn't take the key; don't hold focus 25 s
                    if elapsed > 3 { log("Wispr didn't record anything"); self.finish("Wispr didn't hear that. Is it running?") }
                case .pending:
                    if elapsed > 25 { log("gave up: Wispr didn't finish within 25 s"); self.finish("Wispr didn't finish transcribing.") }
                }
            }
        }
    }

    enum WisprResult { case pending, missing, text(String), nothing(String) }

    static func wisprTranscript(startedAfter start: Date) -> WisprResult {
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
        guard sqlite3_step(stmt) == SQLITE_ROW else { return .missing }   // Wispr never started recording
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

    private func submit(_ raw: String, reply: Bool = false) {
        let said = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard waiting, !said.isEmpty else { return }
        waiting = false
        pollTimer?.invalidate()
        request("/ask", reply ? ["text": said, "reply": true] : ["text": said])   // a reply stays in her thread
        log("\(reply ? "replied" : "asked"): \(said.prefix(120))")
        model.heardSaid(said)
        model.local = .idle
        restoreFocus()
    }

    private func finish(_ message: String) {
        waiting = false
        pollTimer?.invalidate()
        model.local = .idle
        restoreFocus()
        model.flash("Friday", message, for: 1.6)
    }

    private func cancel(_ why: String) {
        log("cancelled: \(why)")
        listening = false; waiting = false
        key(kFn, down: false, asFlagsChanged: true)
        meter.stop()
        request("/mic", ["open": false], timeout: 1)
        pollTimer?.invalidate()
        model.local = .idle
        restoreFocus()
    }

    /// After a push-to-talk: give focus back to where it was (the panel only if it had it). A follow-up reply
    /// never took focus, so it gives nothing back.
    private func restoreFocus() {
        guard sinkHeld else { return }
        sinkHeld = false
        sink.release()
        if panelWasKey && panelIsOpen() { refocusPanel() } else { previousApp?.activate() }
        previousApp = nil
    }

    /// Quitting: nothing left listening (fn up, mic closed, the follow-up window shut).
    func shutdown() {
        if listening { key(kFn, down: false, asFlagsChanged: true) }
        listening = false; waiting = false
        pollTimer?.invalidate(); holdTimer?.invalidate()
        meter.stop()
        if followUp.active { followUp.cancel() }
        restoreFocus()
    }

    /// Listen for a reply without the key: after Friday answers him, or asks something (a permission).
    func openFollowUp(window: TimeInterval, forPermission: Bool) {
        guard !listening, !waiting, !followUp.active, model.local == .idle else { return }
        model.local = .followUp(started: Date(), window: window, partial: "", forPermission: forPermission)
        if let tick = NSSound(named: "Tink") { tick.volume = 0.25; tick.play() }   // "your turn", without looking
        if followUp.start(window: window) {
            request("/mic", ["open": true], timeout: 1)
        } else {
            model.local = .idle
        }
    }
}
