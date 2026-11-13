// The panel: everything Friday is doing, opened from the island, the menu bar, or a double-tap of Right ⌥.
// Needs you (permission prompts, masters waiting on him, held alerts) · Now (the current turn) · Masters ·
// Earlier (the last 50 turns and alerts) · a box to type to Friday.

import SwiftUI

struct PanelActions {
    var stop: () -> Void = {}
    var setQuiet: (Bool) -> Void = { _ in }
    var playHeld: () -> Void = {}
    var openSession: (String) -> Void = { _ in }
    var pickThread: (String) -> Void = { _ in }
    var answer: (Int, String, Bool) -> Void = { _, _, _ in }
    var ask: (String) -> Void = { _ in }
    var close: () -> Void = {}
    var setPinned: (Bool) -> Void = { _ in }
}

struct PanelView: View {
    @ObservedObject var model: AppModel
    let actions: PanelActions
    @State private var draft = ""
    @State private var expanded: Set<String> = []
    @State private var pinned = false
    @FocusState private var typing: Bool

    var body: some View {
        let s = model.server
        VStack(spacing: 0) {
            header(s).padding(.horizontal, 14).padding(.top, 13).padding(.bottom, 10)
            if s.threads.count > 1 { threads(s).padding(.horizontal, 14).padding(.bottom, 10) }
            Rectangle().fill(Palette.line).frame(height: 1)
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    needsYou(s)
                    now(s)
                    masters(s)
                    earlier(s)
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Rectangle().fill(Palette.line).frame(height: 1)
            askBox.padding(10)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Palette.hud)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(Palette.line, lineWidth: 1))
        .environment(\.colorScheme, .dark)
        .onChange(of: model.panelOpen) { _, open in if open { typing = false } }
    }

    // MARK: header

    private func header(_ s: ServerState) -> some View {
        HStack(spacing: 8) {
            Text("Friday").font(.system(size: 13, weight: .bold))
            statusTag(s)
            Spacer(minLength: 4)
            Toggle(isOn: Binding(get: { s.quiet }, set: { actions.setQuiet($0) })) {
                Text("Quiet").font(.system(size: 10.5, weight: .semibold)).foregroundColor(Palette.dim)
            }
            .toggleStyle(.switch).controlSize(.mini)
            .help("Quiet mode: Friday shows captions instead of speaking")
            if s.talking || s.turn?.active == true {
                Button(action: actions.stop) {
                    Label("Stop", systemImage: "stop.fill").font(.system(size: 10.5, weight: .semibold))
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Capsule().fill(Palette.card))
                }.buttonStyle(.plain)
            }
            iconButton(pinned ? "pin.fill" : "pin", help: pinned ? "Unpin (closes when you click elsewhere)" : "Keep open") {
                pinned.toggle()
                actions.setPinned(pinned)
            }
            iconButton("xmark", help: "Close (Esc)", action: actions.close)
        }
        .foregroundColor(.white)
    }

    private func statusTag(_ s: ServerState) -> some View {
        let (text, color): (String, Color) = {
            switch model.daemon {
            case .starting: return ("Starting", Palette.dim)
            case .restarting: return ("Offline", Palette.bad)
            case .ready:
                if s.quiet { return ("Quiet · captions", Palette.fri) }
                return (s.engine == "mac" ? "Voice · Mac" : "Voice · laptop", Palette.dim)
            }
        }()
        return Text(text).font(.system(size: 10.5, weight: .semibold)).foregroundColor(color)
            .padding(.horizontal, 8).padding(.vertical, 2)
            .background(Capsule().fill(Palette.card))
    }

    private func iconButton(_ name: String, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: name).font(.system(size: 10, weight: .bold)).foregroundColor(Palette.dim)
                .frame(width: 22, height: 22).background(Circle().fill(Palette.card))
        }
        .buttonStyle(.plain).help(help)
    }

    private func threads(_ s: ServerState) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 5) {
                ForEach(s.threads) { t in
                    Button(action: { actions.pickThread(t.id) }) {
                        HStack(spacing: 4) {
                            Text(t.title).font(.system(size: 10.5, weight: t.current ? .bold : .semibold))
                            if !t.current { Text(t.ago).font(.system(size: 9)).foregroundColor(Palette.faint) }
                        }
                        .foregroundColor(t.current ? Palette.fri : Palette.dim)
                        .padding(.horizontal, 9).padding(.vertical, 3)
                        .background(Capsule().fill(t.current ? Palette.fri.opacity(0.2) : Palette.card))
                    }
                    .buttonStyle(.plain)
                    .help(t.current ? "Talking in this thread" : "Keep talking in \(t.title)")
                }
            }
        }
    }

    // MARK: needs you

    @ViewBuilder private func needsYou(_ s: ServerState) -> some View {
        let waiting = s.masters.filter { $0.state == "needs" }
        if !s.permissions.isEmpty || !waiting.isEmpty || !s.held.isEmpty {
            PanelSection(title: "Needs you") {
                ForEach(s.permissions) { p in
                    VStack(alignment: .leading, spacing: 6) {
                        Text("\(p.who) wants to \(p.summary)").font(.system(size: 12.5, weight: .semibold))
                        if !p.detail.isEmpty && p.detail != p.summary {
                            Text(p.detail).font(.system(size: 11, design: .monospaced)).foregroundColor(.white.opacity(0.9))
                                .lineLimit(8).textSelection(.enabled)
                                .padding(.horizontal, 8).padding(.vertical, 5)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(RoundedRectangle(cornerRadius: 6).fill(Color.black.opacity(0.25)))
                        }
                        HStack(spacing: 6) {
                            PillButton(label: "Allow", tint: Palette.fri) { actions.answer(p.id, "allow", false) }
                            PillButton(label: "Always", tint: Palette.fri) { actions.answer(p.id, "allow", true) }
                            PillButton(label: "Deny", tint: Palette.bad) { actions.answer(p.id, "deny", false) }
                            Spacer()
                            Elapsed(since: p.created)
                        }
                    }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Palette.needs.opacity(0.14)))
                }
                ForEach(waiting) { m in
                    Button(action: { actions.openSession(m.sessionId) }) {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack {
                                Text("\(m.title) is waiting on you").font(.system(size: 12, weight: .semibold))
                                Spacer()
                                Text("Open").font(.system(size: 10.5, weight: .semibold)).foregroundColor(Palette.needs)
                            }
                            if !m.line.isEmpty {
                                Text(m.line).font(.system(size: 11.5)).foregroundColor(Palette.dim).lineLimit(2)
                            }
                        }
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 10).fill(Palette.needs.opacity(0.1)))
                    }
                    .buttonStyle(.plain)
                }
                if !s.held.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(spacing: 8) {
                            CountBadge(n: s.held.count)
                            Text(s.held.count == 1 ? "alert held while you were busy" : "alerts held while you were busy")
                                .font(.system(size: 12)).foregroundColor(Palette.dim)
                            Spacer()
                            PillButton(label: "Play", tint: Palette.fri, action: actions.playHeld)
                        }
                        ForEach(Array(s.held.suffix(3).enumerated()), id: \.offset) { _, h in
                            Text("\(h.title): \(h.line)").font(.system(size: 11.5)).foregroundColor(Palette.faint).lineLimit(2)
                        }
                    }
                }
            }
        }
    }

    // MARK: now

    @ViewBuilder private func now(_ s: ServerState) -> some View {
        let recent = s.turn.map { $0.active || nowTS() - ($0.ended ?? 0) < 600 } ?? false
        if let say = s.saying {
            VStack(alignment: .leading, spacing: 5) {
                Text("SAYING · \(say.title.uppercased())").font(.system(size: 9.5, weight: .bold)).foregroundColor(Palette.fri.opacity(0.85))
                Text(say.text).font(.system(size: 12.5)).foregroundColor(.white.opacity(0.92)).textSelection(.enabled)
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 10).fill(Palette.fri.opacity(0.1)))
        }
        if let t = s.turn, recent {
            PanelSection(title: nowTitle(t)) {
                Text(t.prompt).font(.system(size: 13.5, weight: .medium)).lineLimit(6).textSelection(.enabled)
                if !t.steps.isEmpty {
                    VStack(alignment: .leading, spacing: 7) {
                        ForEach(t.steps) { StepRow(step: $0) }
                    }
                }
                if !t.reply.isEmpty {
                    Text(t.reply).font(.system(size: 12.5)).foregroundColor(.white.opacity(0.92)).lineSpacing(2)
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                } else if t.active {
                    Text(t.headline).font(.system(size: 12)).foregroundColor(Palette.dim)
                }
                if let e = t.error {
                    Text(e).font(.system(size: 11.5)).foregroundColor(Palette.bad)
                }
            }
        } else if s.saying == nil && s.permissions.isEmpty && s.held.isEmpty {
            Text("Nothing going on right now. Hold Right ⌥ to ask, or type below.")
                .font(.system(size: 12.5)).foregroundColor(Palette.faint)
        }
    }

    private func nowTitle(_ t: Turn) -> String {
        let thread = t.branch.isEmpty ? "" : " · \(t.branch)"
        let state = t.active ? "Now" : (t.cancelled ? "Stopped" : "Last")
        return state + thread + " · " + clock((t.ended ?? nowTS()) - t.started)
    }

    // MARK: masters

    @ViewBuilder private func masters(_ s: ServerState) -> some View {
        if !s.masters.isEmpty {
            PanelSection(title: "Masters") {
                VStack(spacing: 1) {
                    ForEach(s.masters) { m in MasterRow(m: m) { actions.openSession(m.sessionId) } }
                }
                .background(Palette.line)
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }
        }
    }

    // MARK: earlier

    @ViewBuilder private func earlier(_ s: ServerState) -> some View {
        let current = s.turn.map { "t\($0.id)" }
        let items = model.history.filter { $0.id != current }
        if !items.isEmpty {
            PanelSection(title: "Earlier") {
                VStack(alignment: .leading, spacing: 9) {
                    ForEach(items) { item in
                        let open = expanded.contains(item.id)
                        Button(action: { if open { expanded.remove(item.id) } else { expanded.insert(item.id) } }) {
                            HStack(alignment: .top, spacing: 8) {
                                Text(when(item.ts)).font(.system(size: 10, design: .monospaced)).foregroundColor(Palette.faint)
                                    .frame(width: 36, alignment: .leading).padding(.top, 1)
                                VStack(alignment: .leading, spacing: 4) {
                                    (Text(item.kind == "turn" ? "You · " : "\(item.title) · ").font(.system(size: 11.5, weight: .semibold))
                                        .foregroundColor(Palette.dim)
                                     + Text(item.text).font(.system(size: 11.5)).foregroundColor(.white.opacity(0.85)))
                                        .lineLimit(open ? nil : 1)
                                    if open && !item.reply.isEmpty {
                                        Text(item.reply).font(.system(size: 11.5)).foregroundColor(.white.opacity(0.75))
                                            .textSelection(.enabled)
                                    }
                                    if item.kind == "alert", item.delivered.hasPrefix("held") || item.delivered == "notification" {
                                        Text("held").font(.system(size: 9, weight: .bold)).foregroundColor(Palette.faint)
                                    }
                                }
                                Spacer(minLength: 0)
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }

    // MARK: ask

    private var askBox: some View {
        HStack(spacing: 8) {
            TextField("Type to Friday…", text: $draft)
                .textFieldStyle(.plain)
                .font(.system(size: 12.5))
                .focused($typing)
                .onSubmit(send)
            Text("⏎").font(.system(size: 10, design: .monospaced)).foregroundColor(Palette.faint)
                .padding(.horizontal, 4).overlay(RoundedRectangle(cornerRadius: 4).stroke(Palette.line))
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: 10).fill(Palette.card))
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        actions.ask(text)
        draft = ""
    }
}

struct PanelSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased()).font(.system(size: 9.5, weight: .bold)).tracking(0.9).foregroundColor(Palette.faint)
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct StepRow: View {
    let step: Step
    var body: some View {
        HStack(spacing: 9) {
            Group {
                switch step.status {
                case "running": Spinner(size: 11)
                case "failed": Image(systemName: "xmark.circle.fill").foregroundColor(Palette.bad)
                default: Image(systemName: step.speech ? "waveform" : "checkmark.circle.fill")
                    .foregroundColor(step.speech ? Palette.fri : Palette.ok)
                }
            }
            .font(.system(size: 11))
            .frame(width: 14, height: 14)
            Text(step.label).font(.system(size: 12, weight: step.status == "running" ? .medium : .regular))
                .foregroundColor(.white.opacity(step.status == "running" ? 1 : 0.78))
                .lineLimit(1).truncationMode(.tail)
            Spacer(minLength: 6)
            if step.ended == nil {
                Elapsed(since: step.started)
            } else {
                Text(clock((step.ended ?? step.started) - step.started))
                    .font(.system(size: 10.5, weight: .medium, design: .monospaced)).foregroundColor(Palette.faint)
            }
        }
    }
}

struct MasterRow: View {
    let m: Master
    let open: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: open) {
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text(m.title).font(.system(size: 12, weight: .semibold))
                    Spacer()
                    pill
                }
                if !m.line.isEmpty {
                    Text(m.line).font(.system(size: 11.5)).foregroundColor(Palette.dim).lineLimit(1).truncationMode(.tail)
                }
            }
            .padding(.horizontal, 10).padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(hover ? Color(white: 0.12) : Palette.hud)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
        .help("Open \(m.title) in VS Code")
    }

    private var pill: some View {
        TimelineView(.periodic(from: .now, by: 30)) { tl in
            let since = ago(tl.date.timeIntervalSince1970 - m.since)
            let (text, color): (String, Color) = {
                switch m.state {
                case "needs": return ("Waiting on you", Palette.needs)
                case "failed": return ("Stopped on an error", Palette.bad)
                case "working": return ("Working · \(since)", Palette.fri)
                default: return ("Idle · \(since)", Palette.dim)
                }
            }()
            Text(text).font(.system(size: 10, weight: .bold)).foregroundColor(color)
                .padding(.horizontal, 7).padding(.vertical, 1.5)
                .background(Capsule().fill(color.opacity(m.state == "idle" ? 0.1 : 0.18)))
        }
    }
}
