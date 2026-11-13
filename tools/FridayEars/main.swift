// FridayEars: Friday's app. Opening it starts Friday (Hermes gateway, fridayd, Signal, the laptop voice);
// quitting it stops them. Hold Right Option to talk (Wispr Flow does the speech recognition); double-tap
// it for the panel.
//
//   Supervisor.swift   starts and restarts Friday's processes
//   Link.swift         the live UI state from fridayd (GET /ui/stream)
//   AppModel.swift     decides what the island shows (one function, fixed priority)
//   Windows.swift      the island's and panel's windows: which screen, click-through, re-placement
//   IslandView.swift   the island's states       PanelView.swift   the panel
//   Input.swift        push-to-talk, the follow-up window, the mic meter
//   MenuBar.swift      the menu bar icon         Camera.swift      idle face check
//
// Build: scripts/build-ears.sh   Logs: ~/.friday/logs/ears.log, fridayd.stdout.log, gateway.stdout.log

import Cocoa
import Combine
import SwiftUI

final class FridayApp: NSObject, NSApplicationDelegate {
    let model = AppModel()
    let link = ServerLink()
    let supervisor = Supervisor()
    let camera = CameraWatch()
    var island: IslandController!
    var panel: PanelController!
    var ptt: PushToTalk!
    var menuBar: MenuBar!
    var quitting = false
    var lastDisplay: Display = .starting("")
    var lastFacePost = Date.distantPast
    var lastTypingSent = -1
    var wasReady = false
    var subs: Set<AnyCancellable> = []
    var signalSources: [DispatchSourceSignal] = []
    var lastStopAt = Date.distantPast

    var cameraEnabled: Bool {
        get { UserDefaults.standard.object(forKey: "cameraEnabled") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "cameraEnabled") }
    }

    func applicationWillFinishLaunching(_ note: Notification) {
        // Logout, restart and `quit app` send a quit Apple event; without this handler it never reached
        // applicationShouldTerminate, so Friday held up logout and left its processes running.
        NSAppleEventManager.shared().setEventHandler(self, andSelector: #selector(quitEvent(_:withReplyEvent:)),
                                                     forEventClass: AEEventClass(kCoreEventClass),
                                                     andEventID: AEEventID(kAEQuitApplication))
    }

    @objc private func quitEvent(_ event: NSAppleEventDescriptor, withReplyEvent reply: NSAppleEventDescriptor) {
        let sender = event.attributeDescriptor(forKeyword: AEKeyword(keySenderPIDAttr))?.int32Value ?? 0
        log("quit Apple event from \(NSRunningApplication(processIdentifier: pid_t(sender))?.localizedName ?? "pid \(sender)")")
        // NSApp.terminate from inside this handler exits without asking the delegate (seen on macOS 27),
        // which left the gateway and fridayd running. So shut down here, answer the event, then exit.
        guard !quitting else { return }
        let suspended = NSAppleEventManager.shared().suspendCurrentAppleEvent()
        shutdown {
            if let suspended { NSAppleEventManager.shared().resume(withSuspensionID: suspended) }
            exit(0)
        }
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)

        island = IslandController(model: model, actions: IslandActions(
            tap: { [weak self] in self?.togglePanel() },
            answer: { id, decision, always in request("/permission/answer", ["id": id, "decision": decision, "always": always]) },
            later: { [weak self] id in self?.model.collapsed.insert(id) }))
        panel = PanelController(model: model, actions: PanelActions(
            stop: { [weak self] in self?.stopFriday() },
            setQuiet: { on in request("/quiet", ["on": on]) },
            playHeld: { request("/held/play", [:]) },
            openSession: { id in request("/open-session", ["session_id": id]) },
            pickThread: { id in request("/branch", ["id": id]) },
            answer: { id, decision, always in request("/permission/answer", ["id": id, "decision": decision, "always": always]) },
            ask: { text in
                request("/ask", ["text": text, "typed": true])
                log("typed: \(text.prefix(120))")
            },
            close: { [weak self] in self?.panel.close() },
            setPinned: { [weak self] on in self?.panel.pinned = on }))

        ptt = PushToTalk(model: model)
        ptt.onDoubleTap = { [weak self] in self?.togglePanel() }
        ptt.panelIsOpen = { [weak self] in self?.panel.isOpen ?? false }
        ptt.refocusPanel = { [weak self] in self?.panel.focus() }

        menuBar = MenuBar()
        menuBar.onToggle = { [weak self] in self?.togglePanel() }
        menuBar.buildMenu = { [weak self] in self?.menu() ?? NSMenu() }

        model.$display.removeDuplicates().sink { [weak self] d in self?.displayChanged(d) }.store(in: &subs)
        model.onTalkingEnded = { [weak self] old, new in self?.maybeFollowUp(old, new) }
        link.onState = { [weak self] s in
            guard let self else { return }
            self.guardFollowUp(s)
            self.model.apply(s)
            self.menuBar.update(display: self.model.display, server: s, daemon: self.model.daemon)
        }
        link.onHistory = { [weak self] h in self?.model.applyHistory(h) }
        link.onConnected = { [weak self] on in
            self?.model.connected = on
            log(on ? "ui stream connected" : "ui stream disconnected")
        }
        supervisor.onStatus = { [weak self] st in self?.supervisorSaid(st) }
        camera.onFace = { [weak self] in
            DispatchQueue.main.async {
                guard let self, Date().timeIntervalSince(self.lastFacePost) >= 2 else { return }
                self.lastFacePost = Date()
                request("/presence", ["face": true], timeout: 1)
            }
        }

        let trusted = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
        if !CGPreflightListenEventAccess() { CGRequestListenEventAccess() }
        log("started; accessibility=\(trusted) inputMonitoring=\(CGPreflightListenEventAccess()) screens=\(NSScreen.screens.map(\.localizedName))")
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler { [weak self] in
                log("signal \(sig): shutting down")
                guard let self else { exit(0) }
                self.shutdown { exit(0) }
            }
            src.resume()
            signalSources.append(src)
        }
        ptt.install()
        link.start()
        if ProcessInfo.processInfo.environment["FRIDAY_SUPERVISE"] != "0" {   // 0: the app alone (for testing quit paths)
            Thread { self.supervisor.run() }.start()
        }
        commonTimer(2, repeats: true) { [weak self] _ in self?.checkIdle() }
        commonTimer(10, repeats: true) { [weak self] _ in self?.reportTyping() }
    }

    // --- state → windows ---------------------------------------------------------------------------

    private func displayChanged(_ d: Display) {
        island.displayChanged(from: lastDisplay, to: d)
        menuBar.update(display: d, server: model.server, daemon: model.daemon)
        lastDisplay = d
    }

    private func supervisorSaid(_ st: Supervisor.Status) {
        guard !quitting else { return }
        switch st {
        case .starting(let msg): model.daemon = .starting(msg)
        case .restarting(let msg): model.daemon = .restarting(msg)
        case .ready:
            model.daemon = .ready
            model.flash("Friday", wasReady ? "Back. Hold Right ⌥ to talk." : "Ready. Hold Right ⌥ to talk.")
            wasReady = true
        }
        menuBar.update(display: model.display, server: model.server, daemon: model.daemon)
    }

    private func togglePanel() {
        panel.toggle(on: island.screen)
    }

    /// Friday stopped talking: if she just answered his push-to-talk question, or asked him something
    /// (a permission prompt), listen a few seconds for a reply. Not in quiet mode (someone's in the room).
    /// The mic opens only when she has completely finished, never between her sentences: on a loudspeaker an
    /// open mic hears her, and her own words came back as his "reply" (13 Nov: "Almost there I think"
    /// cancelled his real question). Two cases: her answer to his turn (or her offer) is done, the turn is
    /// over and nothing more is queued; or she just asked him a permission question out loud.
    private func maybeFollowUp(_ old: ServerState, _ new: ServerState) {
        guard !new.quiet, model.ready, Date().timeIntervalSince(lastStopAt) > 2, new.turn?.cancelled != true else { return }
        let src = old.source ?? ""
        let askedPermission = src.hasPrefix("permission:") && new.listen
        let answeredHim = new.turn.map { ($0.kind == "voice" || $0.kind == "offer") && $0.source == src } ?? false
        guard askedPermission || answeredHim else { return }
        func done(_ s: ServerState) -> Bool {
            !s.talking && !s.pending && (askedPermission || s.turn?.active != true)
        }
        guard done(new) else { return }
        // 0.6 s: let her last word leave the speaker (a Bluetooth one lags), then check again
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
            guard done(self.model.server) else { return }
            self.ptt.openFollowUp(window: askedPermission ? 12 : 8, forPermission: askedPermission)
        }
    }

    /// While the mic is open for his reply: if she starts talking, close it (what he'd said already is sent;
    /// anything after would be her voice through the speaker); if it was for a permission question that got
    /// answered elsewhere (a button, VS Code), close it.
    private func guardFollowUp(_ s: ServerState) {
        guard case .followUp(_, _, _, let forPermission) = model.local else { return }
        if s.talking {
            log("follow-up: Friday started talking; closing the mic")
            ptt.followUp.yieldToFriday()
        } else if forPermission && !s.listen {
            log("follow-up: the permission was answered elsewhere; closing the mic")
            ptt.followUp.cancel()
        }
    }

    // --- menu ------------------------------------------------------------------------------------

    private func menu() -> NSMenu {
        let m = NSMenu()
        let s = model.server
        let state: String = {
            switch model.daemon {
            case .starting(let msg): return msg
            case .restarting(let msg): return msg
            case .ready: return s.quiet ? "Friday is running · quiet mode" : "Friday is running · voice on \(s.engine == "mac" ? "Mac" : "laptop")"
            }
        }()
        m.addItem(withTitle: state, action: nil, keyEquivalent: "")
        m.addItem(withTitle: "Hold Right ⌥ to talk · double-tap for the panel", action: nil, keyEquivalent: "")
        m.addItem(.separator())
        add(m, panel.isOpen ? "Hide Friday's panel" : "Show Friday's panel", #selector(menuTogglePanel))
        add(m, "Quiet mode (captions instead of speech)", #selector(toggleQuiet)).state = s.quiet ? .on : .off
        m.addItem(micMenu())
        add(m, "Stop speaking", #selector(hushNow))
        add(m, "Use camera when I'm idle (on-device)", #selector(toggleCamera)).state = cameraEnabled ? .on : .off
        m.addItem(.separator())
        add(m, "Quit Friday", #selector(quitFriday)).keyEquivalent = "q"
        return m
    }

    /// Microphone ▸ Same as Wispr Flow / System default / each connected mic. Friday's follow-up window and
    /// listening bars use it; Wispr's own dictation follows Wispr's setting.
    private func micMenu() -> NSMenuItem {
        let devices = Mic.inputs()
        let (current, _) = Mic.resolve()
        let shown = current?.name ?? Mic.systemDefault()?.name ?? "system default"
        let item = NSMenuItem(title: "Microphone: \(shown)", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        let wispr = Mic.wisprName().flatMap { Mic.match(wispr: $0, in: devices) }
        let wisprTitle = "Same as Wispr Flow (\(wispr?.name ?? "auto"))"
        let systemTitle = "System default (\(Mic.systemDefault()?.name ?? "none"))"
        var options: [(String, String)] = [(wisprTitle, "wispr"), (systemTitle, "system")]
        for d in devices {
            let title: String = d.bluetooth ? "\(d.name) · Bluetooth" : d.name
            options.append((title, d.uid))
        }
        for (i, (title, value)) in options.enumerated() {
            if i == 2 { sub.addItem(.separator()) }
            let o = NSMenuItem(title: title, action: #selector(pickMic(_:)), keyEquivalent: "")
            o.target = self
            o.representedObject = value
            o.state = Mic.choice == value ? .on : .off
            sub.addItem(o)
        }
        item.submenu = sub
        return item
    }

    @objc private func pickMic(_ sender: NSMenuItem) {
        guard let value = sender.representedObject as? String else { return }
        Mic.choice = value
        log("mic choice: \(value) -> \(Mic.resolve().device?.name ?? "system default")")
    }

    @discardableResult
    private func add(_ m: NSMenu, _ title: String, _ action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        m.addItem(item)
        return item
    }

    @objc private func menuTogglePanel() { togglePanel() }

    /// Quit Friday: stop everything it runs, then exit. Done here rather than through NSApp.terminate,
    /// which in this app exited without asking the delegate and left the gateway, fridayd, Signal and the
    /// laptop voice running (12 Nov).
    @objc private func quitFriday() {
        log("quit from the menu")
        shutdown { exit(0) }
    }
    @objc private func toggleQuiet() { request("/quiet", ["on": !model.server.quiet]) }
    @objc private func hushNow() { stopFriday() }

    /// Stop: she stops talking and working. No follow-up window after it (her turn "ended" because of Stop).
    private func stopFriday() {
        lastStopAt = Date()
        request("/stop", [:])
        model.clearNotice()
    }
    @objc private func toggleCamera() {
        cameraEnabled.toggle()
        if !cameraEnabled { camera.stop() }
    }

    // --- presence --------------------------------------------------------------------------------

    /// How hard he's typing (keys in the last minute), so fridayd can hold routine alerts until he pauses.
    private func reportTyping() {
        let n = ptt.countTyping()
        guard abs(n - lastTypingSent) >= 10 || (n == 0) != (lastTypingSent == 0) else { return }
        lastTypingSent = n
        request("/presence", ["typing": n], timeout: 1)
    }

    /// Camera on only while there's been no keyboard/mouse input for 4 minutes; off at the first touch.
    private func checkIdle() {
        let any = CGEventType(rawValue: ~UInt32(0))!
        let idle = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: any)
        if cameraEnabled && idle >= 240 { camera.start() } else if idle < 5 || !cameraEnabled { camera.stop() }
    }

    // --- quit ------------------------------------------------------------------------------------

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !quitting else { return .terminateNow }
        log("quit requested (event: \(NSApp.currentEvent.map { "type \($0.type.rawValue) \($0.charactersIgnoringModifiers ?? "")" } ?? "none"))")
        shutdown { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }

    func applicationWillTerminate(_ note: Notification) {
        guard !quitting else { return }
        quitting = true
        log("terminating without a shutdown; stopping Friday's processes now")
        supervisor.stop()
    }

    /// Stop Friday's processes (and free the laptop GPU), then `finish`. Runs once.
    private func shutdown(then finish: @escaping () -> Void) {
        guard !quitting else { return }
        quitting = true
        ptt.shutdown()          // nothing left listening: fn up, mic closed, follow-up shut
        camera.stop()
        panel.close(restore: false)
        model.daemon = .starting("Shutting down…")
        DispatchQueue.global().async {
            self.supervisor.stop()
            DispatchQueue.main.async(execute: finish)
        }
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

if CommandLine.arguments.contains("--mic-test") {   // open Friday's mic for 3 s: buffers and loudest level
    let mic = MicCapture()
    var buffers = 0
    var peak: Float = 0
    let lock = NSLock()
    mic.onBuffer = { _, level in lock.lock(); buffers += 1; peak = max(peak, level); lock.unlock() }
    let (device, why) = Mic.resolve()
    var opened = false
    mic.start(device) { ok in opened = ok }
    RunLoop.main.run(until: Date().addingTimeInterval(3))
    mic.stop()
    lock.lock()
    log("mic test: \(device?.name ?? "system default") (\(why)) opened=\(opened) buffers=\(buffers) peak=\(String(format: "%.2f", peak))")
    lock.unlock()
    exit(0)
}

if CommandLine.arguments.contains("--follow-test") {   // the follow-up window for 5 s, without the rest of Friday
    let f = FollowUp()
    var result = "no result"
    f.onHeard = { partial in log("follow test partial: \(partial.prefix(60))") }
    f.done = { said in result = said.map { "heard \"\($0.prefix(80))\"" } ?? "heard nothing" }
    var finished = false
    let report = f.done
    f.done = { said in report?(said); finished = true }
    let started = f.start(window: 5)
    let until = Date().addingTimeInterval(started ? 30 : 1)
    while !finished && Date() < until { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
    log("follow test: started=\(started) \(result)")
    exit(0)
}

let app = NSApplication.shared
let friday = FridayApp()
app.delegate = friday
app.setActivationPolicy(.accessory)
app.run()
