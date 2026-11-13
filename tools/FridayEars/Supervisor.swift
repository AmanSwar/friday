// Friday's processes live and die with this app: the Hermes gateway (the brain), fridayd (voice, senses,
// the UI state), the Signal daemon, and the laptop voice's SSH tunnel. A loop checks them every 2 s and
// restarts whatever died or stopped answering, with backoff. There is no startup cutoff: it keeps going and
// says what it's waiting for.

import Foundation

final class Supervisor {
    enum Status: Equatable {
        case starting(String)
        case ready
        case restarting(String)
    }

    var onStatus: ((Status) -> Void)?     // called on the main thread, only when it changes

    private struct Child {
        let name: String
        let log: String
        let exe: String
        let args: [String]
        let health: String
        let startupGrace: TimeInterval    // how long it may take to answer after starting
        var process: Process?
        var startedAt = Date.distantPast
        var lastHealthy = Date.distantPast
        var attempts = 0
        var everHealthy = false
    }

    // SSH_AUTH_SOCK: the laptop's SSH key lives in the macOS agent. Without it `ssh wsl-box` is refused,
    // the laptop voice never starts, and every reply renders on this Mac's GPU.
    private let env: [String: String] = {
        var e = ["PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                 "HOME": home, "USER": NSUserName(), "LANG": "en_US.UTF-8", "PYTHONPATH": "\(repo)/src"]
        if let sock = ProcessInfo.processInfo.environment["SSH_AUTH_SOCK"] { e["SSH_AUTH_SOCK"] = sock }
        return e
    }()

    private var children: [Child] = [
        Child(name: "gateway", log: "gateway.stdout.log", exe: "/bin/bash", args: ["\(repo)/bin/friday", "gateway", "run"],
              health: gatewayHealth, startupGrace: 240),
        Child(name: "fridayd", log: "fridayd.stdout.log", exe: "\(repo)/.venv/bin/python", args: ["-m", "fridayd"],
              health: "\(fridayd)/health", startupGrace: 240),
        Child(name: "signal", log: "signal.stdout.log", exe: "/bin/bash", args: ["\(repo)/scripts/signal-daemon.sh"],
              health: signalHealth, startupGrace: 120),
    ]
    private let lock = NSLock()
    private var quitting = false
    private var looping = false
    private let loopDone = DispatchSemaphore(value: 0)
    private var status: Status?
    private var tunnel: Process?
    private var lastLaptopCheck = Date.distantPast
    private let launchedAt = Date()
    private var everReady = false

    // --- the loop (runs on its own thread) --------------------------------------------------------

    func run() {
        lock.lock(); looping = true; lock.unlock()
        defer { loopDone.signal() }
        report(.starting("Waking Friday's voice on the laptop…"))
        ensureLaptopVoice(wait: 40)
        lastLaptopCheck = Date()
        while !isQuitting {
            var up: [String: Bool] = [:]
            for i in children.indices {
                let ok = check(i)
                // One slow answer isn't an outage: up if it answered in the last 5 s.
                up[children[i].name] = ok || Date().timeIntervalSince(children[i].lastHealthy) < 5
            }
            let brain = up["gateway"] ?? false, senses = up["fridayd"] ?? false
            if brain && senses {
                everReady = true
                report(.ready)
            } else if everReady {
                report(.restarting(!senses ? "Friday stopped responding · restarting" : "Friday's brain stopped · restarting"))
            } else {
                let waited = Date().timeIntervalSince(launchedAt)
                let what = !brain ? "Starting Friday's brain…" : "Loading Friday's voice…"
                report(.starting(waited > 150 ? "\(what) (slow; see ~/.friday/logs)" : what))
            }
            if Date().timeIntervalSince(lastLaptopCheck) > 60 {
                lastLaptopCheck = Date()
                ensureLaptopVoice()
            }
            Thread.sleep(forTimeInterval: 2)
        }
    }

    /// Is this child answering? Starts or restarts it when it isn't (with backoff).
    private func check(_ i: Int) -> Bool {
        let now = Date()
        if healthy(children[i].health) {
            if !children[i].everHealthy { log("\(children[i].name) is up") }
            children[i].lastHealthy = now
            children[i].everHealthy = true
            if now.timeIntervalSince(children[i].startedAt) > 300 { children[i].attempts = 0 }
            return true
        }
        if isQuitting { return false }
        let c = children[i]
        let running = c.process?.isRunning ?? false
        if running {
            // Still starting, or it was answering until just now: give it time before calling it hung.
            let since = max(c.startedAt, c.lastHealthy)
            let grace = c.lastHealthy > c.startedAt ? 30 : c.startupGrace
            if now.timeIntervalSince(since) < grace { return false }
            log("\(c.name) isn't answering (running \(Int(now.timeIntervalSince(c.startedAt)))s); restarting it")
            terminate(c.process)
        }
        let backoff: [TimeInterval] = [0, 5, 15, 30, 60]
        let wait = backoff[min(c.attempts, backoff.count - 1)]
        if now.timeIntervalSince(c.startedAt) < wait { return false }
        if c.process == nil && !c.everHealthy && c.attempts == 0 {
            log("starting \(c.name)")
        } else {
            log("restarting \(c.name) (attempt \(c.attempts + 1))")
        }
        children[i].process = spawn(c.exe, c.args, log: c.log)
        children[i].startedAt = now
        children[i].attempts += 1
        return false
    }

    private var isQuitting: Bool {
        lock.lock(); defer { lock.unlock() }
        return quitting
    }

    private func report(_ s: Status) {
        guard s != status else { return }
        status = s
        if case .restarting(let msg) = s { log(msg) }
        DispatchQueue.main.async { self.onStatus?(s) }
    }

    // --- processes ------------------------------------------------------------------------------

    private func spawn(_ exe: String, _ args: [String], log name: String) -> Process? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        p.environment = env
        p.currentDirectoryURL = URL(fileURLWithPath: repo)
        let url = logDir.appendingPathComponent(name)
        if !FileManager.default.fileExists(atPath: url.path) { FileManager.default.createFile(atPath: url.path, contents: nil) }
        if let fh = try? FileHandle(forWritingTo: url) { fh.seekToEndOfFile(); p.standardOutput = fh; p.standardError = fh }
        do {
            try p.run()
            log("started \(name) (pid \(p.processIdentifier))")
            return p
        } catch {
            log("could not start \(name): \(error)")
            return nil
        }
    }

    private func terminate(_ p: Process?) {
        guard let p, p.isRunning else { return }
        p.terminate()
        let deadline = Date().addingTimeInterval(5)
        while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

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

    /// Stop everything, whether this app started it or found it running. Runs off the main thread.
    func stop() {
        lock.lock(); quitting = true; let wasLooping = looping; lock.unlock()
        // Let the loop finish its current step, so nothing it's starting right now is left behind.
        if wasLooping { _ = loopDone.wait(timeout: .now() + 3) }
        // /quit without going through the main thread (stop() may run on it: a main-queue completion would
        // only arrive after this times out)
        var req = URLRequest(url: URL(string: "\(fridayd)/quit")!)
        req.httpMethod = "POST"; req.timeoutInterval = 2
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, _, _ in sem.signal() }.resume()
        _ = sem.wait(timeout: .now() + 3)
        let stopper = Process()
        stopper.executableURL = URL(fileURLWithPath: "/bin/bash")
        stopper.arguments = ["\(repo)/bin/friday", "gateway", "stop"]
        stopper.environment = env
        stopper.standardOutput = FileHandle.nullDevice; stopper.standardError = FileHandle.nullDevice
        try? stopper.run()
        let procs = children.compactMap(\.process)
        for p in procs where p.isRunning { p.terminate() }
        if let t = tunnel, t.isRunning { t.terminate() }
        run(["/bin/bash", "\(repo)/scripts/laptop-voice.sh", "stop"], timeout: 10)   // frees the laptop GPU too
        // the Signal daemon, even if adopted; anchored to the binary so no other command line can match
        run(["/usr/bin/pkill", "-f", "^[^ ]*/signal-cli -a [^ ]+ daemon --http 127\\.0\\.0\\.1:8080"], timeout: 5)
        let deadline = Date().addingTimeInterval(20)
        while Date() < deadline && (stopper.isRunning || procs.contains { $0.isRunning }) { Thread.sleep(forTimeInterval: 0.2) }
        for p in procs where p.isRunning { kill(p.processIdentifier, SIGKILL) }
        if stopper.isRunning { stopper.terminate() }
        log("Friday stopped")
    }
}
