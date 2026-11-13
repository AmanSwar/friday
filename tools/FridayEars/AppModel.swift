// The one place that decides what the island shows: `derive()` turns (what you're doing with the key and
// mic, fridayd's latest state, the supervisor's status, the clock) into a `Display`, in a fixed priority:
// listening › transcribing › starting/offline › needs you › notice › speaking › follow-up › heard ›
// working on your question › background › idle. No other code shows or hides the island.

import Cocoa
import SwiftUI

enum Local: Equatable {
    case idle
    case listening(Date)
    case transcribing(Date)
    case followUp(started: Date, window: Double, partial: String, forPermission: Bool)
}

enum DaemonStatus: Equatable {
    case starting(String)
    case ready
    case restarting(String)
}

struct Notice: Equatable {
    let title: String
    let text: String
    let until: Date
}

struct Caption: Equatable {
    let chip: String            // which thread, or who the alert is about
    let line: String            // the piece being spoken now
    let started: Date           // when this piece started (the highlight runs from here)
    let quiet: Bool             // quiet mode: a caption instead of speech
}

enum Display: Equatable {
    case hidden(amber: Bool, held: Int)
    case listening(started: Date)
    case transcribing
    case starting(String)
    case offline(String)
    case needs(Permission, more: Int, micOn: Bool)
    case notice(title: String, text: String)
    case speaking(Caption)
    case followUp(started: Date, window: Double, partial: String)
    case heard(String)
    case working(chip: String, prompt: String, headline: String, started: Double, done: Int, total: Int)
    case background(label: String, started: Double)

    var isHidden: Bool { if case .hidden = self { return true }; return false }

    /// Hidden with nothing to show at all (no badges): the window is fully click-through.
    var isEmpty: Bool { if case .hidden(let a, let h) = self { return !a && h == 0 }; return false }

    var isCompact: Bool {
        switch self {
        case .background: return true
        case .hidden(let a, let h): return a || h > 0
        default: return false
        }
    }

    /// Changes in kind animate the shape; changes within a kind (new text) only cross-fade.
    var kind: String {
        switch self {
        case .hidden(let a, let h): return a || h > 0 ? "badges" : "hidden"
        case .listening: return "listening"
        case .transcribing: return "transcribing"
        case .starting: return "starting"
        case .offline: return "offline"
        case .needs: return "needs"
        case .notice: return "notice"
        case .speaking: return "speaking"
        case .followUp: return "followUp"
        case .heard: return "heard"
        case .working: return "working"
        case .background: return "background"
        }
    }
}

/// Where the island sits on the screen it's on: grown out of the notch, or a pill under the menu bar.
struct IslandGeometry: Equatable {
    var notch: Bool
    var notchSize: CGSize       // .zero without a notch
    var top: CGFloat            // from the window's top edge to where the island's content starts
    var window: CGSize
    static let fallback = IslandGeometry(notch: false, notchSize: .zero, top: 30, window: CGSize(width: 640, height: 320))
}

final class AppModel: ObservableObject {
    @Published private(set) var display: Display = .starting("Starting Friday…")
    @Published private(set) var server = ServerState.empty
    @Published private(set) var history: [HistoryItem] = []
    @Published var connected = false { didSet { refresh() } }
    @Published var daemon: DaemonStatus = .starting("Starting Friday…") { didSet { refresh() } }
    @Published var local: Local = .idle { didSet { refresh() } }
    @Published var micLevel: Float = 0
    @Published var panelOpen = false { didSet { refresh() } }
    @Published var geometry = IslandGeometry.fallback
    @Published var collapsed: Set<Int> = [] { didSet { refresh() } }   // permission prompts put away with "Later"

    private var notice: Notice?
    private var heard: (text: String, at: Date)?
    private var lastCaption: Caption?
    private var lastTalkingAt: Date?
    private var lineStarted = Date()
    private var timer: Timer?

    /// Friday stopped talking (old state, new state): the app decides whether to open the follow-up window.
    var onTalkingEnded: ((ServerState, ServerState) -> Void)?

    init() {
        // Time-based changes (your words shown for a second, a caption lingering, a notice expiring).
        timer = commonTimer(0.25, repeats: true) { [weak self] _ in self?.refresh() }
    }

    var ready: Bool { daemon == .ready }

    // --- inputs ------------------------------------------------------------------------------------

    func apply(_ s: ServerState) {
        let old = server
        if s.line != old.line || s.source != old.source { lineStarted = Date() }
        if s != old { server = s }
        if s.talking {
            lastTalkingAt = Date()
            lastCaption = caption(for: s)
        }
        let ids = Set(s.permissions.map(\.id))
        if !collapsed.isSubset(of: ids) { collapsed = collapsed.intersection(ids) }
        // "She's done": no longer talking and nothing more pending (not just a pause between her sentences)
        if (old.talking || old.pending) && !(s.talking || s.pending) { onTalkingEnded?(old, s) }
        refresh()
    }

    func applyHistory(_ items: [HistoryItem]) {
        if items != history { history = items }
    }

    func flash(_ title: String, _ text: String, for seconds: TimeInterval = 2.2) {
        notice = Notice(title: title, text: text, until: Date().addingTimeInterval(seconds))
        refresh()
    }

    func clearNotice() {
        notice = nil
        refresh()
    }

    func heardSaid(_ text: String) {
        heard = (text, Date())
        refresh()
    }

    private func caption(for s: ServerState) -> Caption {
        let src = s.source ?? ""
        var chip = ""
        if src.hasPrefix("voice:") {
            let thread = s.turn?.branch ?? ""
            chip = thread == "General" ? "" : thread
        } else if let say = s.saying {
            chip = say.title == "Friday" ? "" : say.title
        }
        return Caption(chip: chip, line: s.line ?? "", started: lineStarted, quiet: s.quiet)
    }

    // --- the decision ------------------------------------------------------------------------------

    func refresh() {
        let d = derive(Date())
        if d != display { display = d }
    }

    func derive(_ now: Date) -> Display {
        switch local {
        case .listening(let t): return .listening(started: t)
        case .transcribing: return .transcribing
        default: break
        }
        switch daemon {
        case .starting(let msg): return .starting(msg)
        case .restarting(let msg): return .offline(msg)
        case .ready: break
        }
        let s = server
        var micOn = false
        if case .followUp = local { micOn = true }
        // With the panel open it shows everything; the island only expands for the mic.
        if !panelOpen, let p = s.permissions.first(where: { !collapsed.contains($0.id) }) {
            return .needs(p, more: s.permissions.count - 1, micOn: micOn)
        }
        if let n = notice, n.until > now { return .notice(title: n.title, text: n.text) }
        if !panelOpen, s.talking, let c = lastCaption { return .speaking(c) }
        // Her turn is over and the mic is open for his reply: this shows the moment it opens.
        if case .followUp(let st, let w, let partial, _) = local {
            return .followUp(started: st, window: w, partial: partial)
        }
        if !panelOpen, let c = lastCaption, let at = lastTalkingAt, now.timeIntervalSince(at) < 1.5, s.turn?.active != true {
            return .speaking(c)
        }
        if !panelOpen {
            if let h = heard, now.timeIntervalSince(h.at) < 1.0 { return .heard(h.text) }
            if let t = s.turn, t.active, t.isHis {
                let steps = t.steps.filter { !$0.speech }
                return .working(chip: t.branch == "General" ? "" : t.branch, prompt: t.prompt, headline: t.headline,
                                started: t.started, done: steps.filter { $0.status != "running" }.count, total: steps.count)
            }
        }
        if let t = s.turn, t.active {
            return .background(label: t.isHis ? (t.branch.isEmpty || t.branch == "General" ? "Working" : t.branch) : t.title,
                               started: t.started)
        }
        if let j = s.judging.first { return .background(label: "Checking \(j.who)", started: j.since) }
        let amber = !s.permissions.isEmpty || s.masters.contains { $0.state == "needs" }
        return .hidden(amber: amber, held: s.held.count)
    }
}
