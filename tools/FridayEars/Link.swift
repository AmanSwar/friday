// The live line to fridayd: GET /ui/stream (one JSON object per `data:` line, a heartbeat every 5 s).
// Messages carry a sequence number per type; anything not newer than what's shown is dropped, so a late
// answer can never put back an old state. If the stream drops or goes silent, it reconnects; meanwhile
// GET /ui is polled every 2 s so the island keeps up.

import Foundation

final class ServerLink {
    var onState: ((ServerState) -> Void)?
    var onHistory: (([HistoryItem]) -> Void)?
    var onConnected: ((Bool) -> Void)?

    private(set) var connected = false
    private var task: Task<Void, Never>?
    private var boot: Double = 0
    private var seq: [String: Int] = ["state": 0, "history": 0]
    private var lastMessage = Date()
    private var timer: Timer?

    func start() {
        connect()
        timer = commonTimer(2, repeats: true) { [weak self] _ in self?.watch() }
    }

    func stop() {
        task?.cancel()
        timer?.invalidate()
    }

    private func watch() {
        if connected && Date().timeIntervalSince(lastMessage) > 12 {   // silent: no heartbeat for 12 s
            log("ui stream went silent; reconnecting")
            task?.cancel()
            setConnected(false)
            connect()
        }
        if !connected {                                                // the fallback poll
            request("/ui", timeout: 1.5) { [weak self] json in
                guard let self, !self.connected, let json else { return }
                if let s = json.dict("state") { self.apply(s) }
                if let h = json.dict("history") { self.apply(h) }
            }
        }
    }

    private func connect() {
        // On the main actor: each line is applied where the UI lives; awaiting the next one doesn't block.
        task = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                do {
                    var req = URLRequest(url: URL(string: fridayd + "/ui/stream")!)
                    req.timeoutInterval = 15
                    let (bytes, resp) = try await URLSession.shared.bytes(for: req)
                    guard (resp as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
                    self?.lastMessage = Date()
                    self?.setConnected(true)
                    for try await line in bytes.lines {
                        if Task.isCancelled { break }
                        self?.lastMessage = Date()                               // data, or the ": ping" heartbeat
                        guard line.hasPrefix("data: "),
                              let obj = try? JSONSerialization.jsonObject(with: Data(line.dropFirst(6).utf8)) as? [String: Any]
                        else { continue }
                        self?.apply(obj)
                    }
                } catch {}
                if Task.isCancelled { return }
                self?.setConnected(false)
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    private func setConnected(_ on: Bool) {
        guard on != connected else { return }
        connected = on
        onConnected?(on)
    }

    private func apply(_ obj: [String: Any]) {
        let kind = obj.str("type")
        guard kind == "state" || kind == "history" else { return }
        let b = obj.num("boot")
        if b != boot {                    // fridayd restarted: its numbering starts over
            boot = b
            seq = ["state": 0, "history": 0]
        }
        let n = obj.int("seq")
        guard n > seq[kind, default: 0] else { return }
        seq[kind] = n
        if kind == "state" { onState?(ServerState(obj)) } else { onHistory?(parseHistory(obj)) }
    }
}
