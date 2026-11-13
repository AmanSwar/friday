// What fridayd says is going on (GET /ui, /ui/stream). Times are Unix timestamps; the app does the clock.

import Foundation

struct Step: Identifiable, Equatable {
    let id: Int
    let label: String
    let status: String          // running | done | failed
    let started: Double
    let ended: Double?
    let speech: Bool
}

struct Turn: Equatable {
    let id: Int
    let kind: String            // voice | typed | phone | presence
    let title: String
    let prompt: String
    let branch: String
    let started: Double
    let ended: Double?
    let active: Bool
    let headline: String
    let steps: [Step]
    let reply: String
    let error: String?
    let cancelled: Bool

    /// Aman's own question (push-to-talk or typed), as opposed to Friday's proactive turns.
    var isHis: Bool { kind == "voice" || kind == "typed" }
    var source: String { "voice:\(id)" }

    init?(_ d: [String: Any]?) {
        guard let d, d["id"] != nil else { return nil }
        id = d.int("id"); kind = d.str("kind"); title = d.str("title"); prompt = d.str("prompt")
        branch = d.str("branch"); started = d.num("started"); ended = d.optNum("ended")
        active = d.bool("active"); headline = d.str("headline"); reply = d.str("reply")
        error = d.optStr("error"); cancelled = d.bool("cancelled")
        steps = d.list("steps").enumerated().map { i, s in
            Step(id: i, label: s.str("label"), status: s.str("status"), started: s.num("started"),
                 ended: s.optNum("ended"), speech: s.bool("speech"))
        }
    }
}

struct Permission: Identifiable, Equatable {
    let id: Int
    let who: String             // "The AWS master", "dev2 on AWS"
    let summary: String         // "run python bench.py"
    let detail: String          // the full command / file
    let tool: String
    let created: Double
}

struct Master: Identifiable, Equatable {
    var id: String { title }
    let title: String           // master_aws
    let name: String            // AWS
    let sessionId: String
    let state: String           // needs | failed | working | idle
    let since: Double
    let line: String
}

struct ThreadInfo: Identifiable, Equatable {
    let id: String
    let title: String
    let ago: String
    let current: Bool
}

struct Held: Equatable {
    let title: String
    let line: String
    let ts: Double
}

struct Judging: Equatable {
    let who: String
    let since: Double
}

struct Saying: Equatable {
    let source: String
    let title: String
    let text: String
}

struct HistoryItem: Identifiable, Equatable {
    let id: String
    let ts: Double
    let kind: String            // turn | alert
    let title: String           // "You asked", "AWS master", "Slack"
    let text: String            // his words, or the alert
    let reply: String
    let thread: String
    let delivered: String       // alerts: spoken | held | held_focus | phone | notification
    let error: String?
}

struct ServerState: Equatable {
    var engine: String? = nil
    var talking = false
    var source: String? = nil
    var line: String? = nil
    var quiet = false
    var pending = false          // she has more to say (an utterance still open, or queued)
    var turn: Turn? = nil
    var saying: Saying? = nil
    var permissions: [Permission] = []
    var held: [Held] = []
    var listen = false
    var judging: [Judging] = []
    var threads: [ThreadInfo] = []
    var masters: [Master] = []
    var browser = false

    static let empty = ServerState()

    init() {}

    init(_ d: [String: Any]) {
        let v = d.dict("voice") ?? [:]
        engine = v.optStr("engine"); talking = v.bool("talking"); source = v.optStr("source")
        line = v.optStr("line"); quiet = v.bool("quiet"); pending = v.bool("pending")
        turn = Turn(d.dict("turn"))
        if let s = d.dict("saying") { saying = Saying(source: s.str("source"), title: s.str("title"), text: s.str("text")) }
        permissions = d.list("permissions").map {
            Permission(id: $0.int("id"), who: $0.str("who"), summary: $0.str("summary"), detail: $0.str("detail"),
                       tool: $0.str("tool"), created: $0.num("created"))
        }
        held = d.list("held").map { Held(title: $0.str("title"), line: $0.str("line"), ts: $0.num("ts")) }
        listen = d.bool("listen")
        judging = d.list("judging").map { Judging(who: $0.str("who"), since: $0.num("since")) }
        threads = (d.dict("threads")?.list("branches") ?? []).map {
            ThreadInfo(id: $0.str("id"), title: $0.str("title"), ago: $0.str("ago"), current: $0.bool("current"))
        }
        masters = d.list("masters").map {
            Master(title: $0.str("title"), name: $0.str("name"), sessionId: $0.str("session_id"), state: $0.str("state"),
                   since: $0.num("since"), line: $0.str("line"))
        }
        browser = d.bool("browser")
    }
}

func parseHistory(_ d: [String: Any]) -> [HistoryItem] {
    d.list("items").map {
        HistoryItem(id: $0.str("id"), ts: $0.num("ts"), kind: $0.str("kind"), title: $0.str("title"), text: $0.str("text"),
                    reply: $0.str("reply"), thread: $0.str("thread"), delivered: $0.str("delivered"), error: $0.optStr("error"))
    }
}
