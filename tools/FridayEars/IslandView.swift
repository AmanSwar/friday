// The island: what `AppModel.display` says, drawn. On the MacBook it grows out of the notch (flat top,
// rounded bottom); on a monitor it's a pill under the menu bar. Compact (beside the notch) for Friday's own
// background work and for badges; expanded for what you asked, what needs your answer, and her words.

import SwiftUI

struct IslandActions {
    var tap: () -> Void = {}
    var answer: (Int, String, Bool) -> Void = { _, _, _ in }     // permission id, allow | deny, always
    var later: (Int) -> Void = { _ in }
}

struct FrameKey: PreferenceKey {
    static var defaultValue: CGRect = .zero
    static func reduce(value: inout CGRect, nextValue: () -> CGRect) { value = nextValue() }
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

struct IslandRoot: View {
    @ObservedObject var model: AppModel
    let actions: IslandActions
    let onFrame: (CGRect) -> Void

    var body: some View {
        let g = model.geometry
        ZStack(alignment: .top) {
            IslandBody(display: model.display, g: g, level: model.micLevel, actions: actions)
                .padding(.top, g.notch ? 0 : g.top)
        }
        .frame(width: g.window.width, height: g.window.height, alignment: .top)
        .coordinateSpace(name: "island")
        .onPreferenceChange(FrameKey.self) { onFrame($0) }
        .environment(\.colorScheme, .dark)
    }
}

struct IslandBody: View {
    let display: Display
    let g: IslandGeometry
    let level: Float
    let actions: IslandActions

    var body: some View {
        Group {
            if display.isEmpty {
                if g.notch {      // exactly the notch: invisible, but a click on it opens the panel
                    Color.black.frame(width: g.notchSize.width, height: g.notchSize.height)
                        .clipShape(IslandShape(radius: 8))
                        .reportFrame()
                        .onTapGesture(perform: actions.tap)
                } else {
                    Color.clear.frame(width: 1, height: 1)
                        .preference(key: FrameKey.self, value: .zero)
                }
            } else if display.isCompact {
                CompactIsland(display: display, g: g).reportFrame().onTapGesture(perform: actions.tap)
                    .transition(.swapIn)
            } else {
                ExpandedIsland(display: display, g: g, level: level, actions: actions)
                    .reportFrame()
                    .transition(.swapIn)
            }
        }
        .animation(.spring(response: 0.36, dampingFraction: 0.84), value: display.kind)
    }
}

extension AnyTransition {
    /// The old content leaves at once (no two states drawn over each other); the new one fades in
    /// while the shape springs to its size.
    static var swapIn: AnyTransition {
        .asymmetric(insertion: .opacity.animation(.easeOut(duration: 0.16).delay(0.08)), removal: .identity)
    }
}

extension View {
    func reportFrame() -> some View {
        background(GeometryReader { p in Color.clear.preference(key: FrameKey.self, value: p.frame(in: .named("island"))) })
    }
}

// MARK: - compact: beside the notch (or a small pill)

struct CompactIsland: View {
    let display: Display
    let g: IslandGeometry

    var body: some View {
        switch display {
        case .background(let label, let started):
            wings(width: 92) {
                Spinner(size: 11)
            } right: {
                TimelineView(.periodic(from: .now, by: 1)) { tl in
                    Text("\(label) · \(ago2(tl.date.timeIntervalSince1970 - started))")
                        .font(.system(size: 10, weight: .semibold)).foregroundColor(.white.opacity(0.85))
                        .lineLimit(1).truncationMode(.tail)
                }
            }
        case .hidden(let amber, let held):
            wings(width: 22) {
                if held > 0 { CountBadge(n: held) }
            } right: {
                if amber { Circle().fill(Palette.needs).frame(width: 7, height: 7) }
            }
        default:
            EmptyView()
        }
    }

    @ViewBuilder
    private func wings<L: View, R: View>(width: CGFloat, @ViewBuilder left: () -> L, @ViewBuilder right: () -> R) -> some View {
        if g.notch {
            HStack(spacing: 0) {
                left().frame(width: width, alignment: .center)
                Color.clear.frame(width: g.notchSize.width)
                right().frame(width: width, alignment: .leading)
            }
            .frame(height: g.notchSize.height)
            .padding(.horizontal, 6)
            .background(IslandShape(radius: 10).fill(Color.black))
        } else {
            HStack(spacing: 8) { left(); right() }
                .padding(.horizontal, 11).padding(.vertical, 6)
                .background(Capsule().fill(Color.black))
                .shadow(color: .black.opacity(0.35), radius: 10, y: 4)
        }
    }

    private func ago2(_ s: Double) -> String { s < 60 ? "\(Int(max(0, s)))s" : "\(Int(s) / 60)m" }
}

struct CountBadge: View {
    let n: Int
    var body: some View {
        Text("\(n)").font(.system(size: 9, weight: .bold)).foregroundColor(.black)
            .padding(.horizontal, 4).frame(minWidth: 13, minHeight: 13)
            .background(Capsule().fill(Palette.fri))
    }
}

// MARK: - expanded

struct ExpandedIsland: View {
    let display: Display
    let g: IslandGeometry
    let level: Float
    let actions: IslandActions

    var body: some View {
        let width: CGFloat = g.notch ? 410 : 390
        VStack(spacing: 0) {
            if g.notch { Color.clear.frame(height: g.top) }
            content
                .id(display.kind)                 // a new state is a new view: it swaps in, never cross-fades
                .transition(.swapIn)
                .padding(.horizontal, 16)
                .padding(.top, g.notch ? 7 : 11)
                .padding(.bottom, 12)
        }
        .frame(width: width, alignment: .leading)
        .background(shape.fill(Color.black))
        .overlay(needsRing)
        .clipShape(shape)
        .shadow(color: .black.opacity(0.38), radius: 14, y: 6)
        .contentShape(shape)
        .onTapGesture(perform: actions.tap)
    }

    private var shape: AnyShape {
        g.notch ? AnyShape(IslandShape(radius: 22)) : AnyShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
    }

    @ViewBuilder private var needsRing: some View {
        if case .needs = display { shape.stroke(Palette.needs.opacity(0.35), lineWidth: 1) }
    }

    @ViewBuilder private var content: some View {
        switch display {
        case .listening(let started):
            Row(icon: { LevelBars(level: level) },
                title: { Chip(text: "MIC ON", color: Palette.mic); Text("Listening") },
                text: Text("Release Right ⌥ to send"),
                trailing: { Elapsed(since: started.timeIntervalSince1970) })
        case .transcribing:
            Row(icon: { Dots() }, title: { Text("Transcribing") }, text: Text("One moment…"))
        case .starting(let msg):
            Row(icon: { Spinner(size: 15) }, title: { Text("Friday") }, text: Text(msg))
        case .offline(let msg):
            Row(icon: { Circle().fill(Palette.bad).frame(width: 10, height: 10) },
                title: { Text("Offline").foregroundColor(Palette.bad) }, text: Text(msg))
        case .needs(let p, let more, let micOn):
            NeedsCard(p: p, more: more, micOn: micOn, actions: actions)
        case .notice(let title, let text):
            Row(icon: { Image(systemName: "sparkles").font(.system(size: 14)).foregroundColor(.white.opacity(0.85)) },
                title: { Text(title) }, text: Text(text))
        case .speaking(let c):
            Row(icon: { if c.quiet { CCBadge() } else { SpeakingBars() } },
                title: {
                    if !c.chip.isEmpty { Chip(text: c.chip, color: Palette.fri) }
                    Text(c.quiet ? "Friday · quiet" : "Friday")
                },
                text: CaptionText.text(c, at: Date()), live: c)
        case .followUp(let started, let window, let partial) where partial.isEmpty:
            Row(icon: { Countdown(started: started, window: window) },
                title: { Chip(text: "YOUR TURN", color: Palette.mic); Text("Mic on") },
                text: Text("Go ahead, I'm listening"),
                trailing: { Remaining(started: started, window: window) })
        case .followUp(_, _, let partial):      // he's talking: no countdown, it listens until he stops
            Row(icon: { LevelBars(level: level) },
                title: { Chip(text: "MIC ON", color: Palette.mic); Text("Listening") },
                text: Text(partial.isEmpty ? "…" : partial))
        case .heard(let said):
            Row(icon: { Image(systemName: "quote.opening").font(.system(size: 13, weight: .semibold)).foregroundColor(Palette.dim) },
                title: { Text("You") }, text: Text(said))
        case .working(let chip, let prompt, let headline, let started, let done, let total):
            Row(icon: { Spinner(size: 15) },
                title: {
                    if !chip.isEmpty { Chip(text: chip, color: Palette.fri) }
                    Text(prompt).lineLimit(1).truncationMode(.tail)
                },
                text: Text(headline),
                trailing: { Elapsed(since: started) },
                extra: { if total > 0 { StepDots(done: done, total: total) } })
        default:
            EmptyView()
        }
    }
}

/// One island line: indicator · (title line, main text, extra) · trailing.
struct Row<Icon: View, Title: View, Trailing: View, Extra: View>: View {
    let icon: Icon
    let title: Title
    let text: Text
    let trailing: Trailing
    let extra: Extra
    var live: Caption? = nil

    init(@ViewBuilder icon: () -> Icon, @ViewBuilder title: () -> Title, text: Text,
         @ViewBuilder trailing: () -> Trailing = { EmptyView() }, @ViewBuilder extra: () -> Extra = { EmptyView() },
         live: Caption? = nil) {
        self.icon = icon(); self.title = title(); self.text = text; self.trailing = trailing(); self.extra = extra()
        self.live = live
    }

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            icon.frame(width: 26, height: 24)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) { title }
                    .font(.system(size: 10.5, weight: .semibold)).foregroundColor(Palette.dim).lineLimit(1)
                if let c = live {
                    TimelineView(.periodic(from: c.started, by: 0.2)) { tl in
                        CaptionText.text(c, at: tl.date)
                            .font(.system(size: 13.5, weight: .medium)).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    }
                } else {
                    text.font(.system(size: 13.5, weight: .medium)).foregroundColor(.white)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                }
                extra
            }
            Spacer(minLength: 0)
            trailing.frame(minWidth: 0, alignment: .topTrailing)
        }
    }
}

struct NeedsCard: View {
    let p: Permission
    let more: Int
    let micOn: Bool
    let actions: IslandActions

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Circle().fill(Palette.needs).frame(width: 10, height: 10).frame(width: 26, height: 18)
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Chip(text: "NEEDS YOU", color: Palette.needs)
                    Text("\(p.who) wants to").font(.system(size: 10.5, weight: .semibold)).foregroundColor(Palette.dim).lineLimit(1)
                    Spacer(minLength: 0)
                    Elapsed(since: p.created)
                }
                Text(p.summary.prefix(1).uppercased() + p.summary.dropFirst())
                    .font(.system(size: 13.5, weight: .medium)).foregroundColor(.white).lineLimit(2)
                if !p.detail.isEmpty && p.detail != p.summary {
                    Text(p.detail).font(.system(size: 11, design: .monospaced)).foregroundColor(.white.opacity(0.9))
                        .lineLimit(4).padding(.horizontal, 8).padding(.vertical, 5)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 6).fill(Palette.card))
                }
                HStack(spacing: 6) {
                    PillButton(label: "Allow", tint: Palette.fri) { actions.answer(p.id, "allow", false) }
                    PillButton(label: "Always", tint: Palette.fri) { actions.answer(p.id, "allow", true) }
                    PillButton(label: "Deny", tint: Palette.bad) { actions.answer(p.id, "deny", false) }
                    PillButton(label: "Later", tint: .white) { actions.later(p.id) }
                    Spacer(minLength: 0)
                    Text(micOn ? "listening…" : "or say yes · no").font(.system(size: 10.5))
                        .foregroundColor(micOn ? Palette.mic : Palette.faint)
                }
                if more > 0 {
                    Text("+\(more) more waiting").font(.system(size: 10.5)).foregroundColor(Palette.faint)
                }
            }
        }
    }
}

// MARK: - pieces

struct Chip: View {
    let text: String
    let color: Color
    var body: some View {
        Text(text).font(.system(size: 9.5, weight: .bold)).foregroundColor(color)
            .padding(.horizontal, 6).padding(.vertical, 1.5)
            .background(Capsule().fill(color.opacity(0.18)))
            .fixedSize()
    }
}

struct PillButton: View {
    let label: String
    let tint: Color
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Text(label).font(.system(size: 11, weight: .semibold)).foregroundColor(.white)
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(Capsule().fill(tint.opacity(tint == .white ? 0.1 : 0.32)))
        }
        .buttonStyle(.plain)
    }
}

struct Elapsed: View {
    let since: Double
    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { tl in
            Text(clock(tl.date.timeIntervalSince1970 - since))
                .font(.system(size: 10.5, weight: .medium, design: .monospaced)).foregroundColor(Palette.faint)
        }
    }
}

struct Remaining: View {
    let started: Date
    let window: Double
    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.5)) { tl in
            Text("\(Int(max(0, window - tl.date.timeIntervalSince(started)).rounded(.up)))s")
                .font(.system(size: 10.5, weight: .medium, design: .monospaced)).foregroundColor(Palette.faint)
        }
    }
}

struct Spinner: View {
    let size: CGFloat
    var body: some View {
        TimelineView(.animation) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            Circle().trim(from: 0, to: 0.72)
                .stroke(Palette.fri, style: StrokeStyle(lineWidth: max(1.6, size / 8), lineCap: .round))
                .frame(width: size, height: size)
                .rotationEffect(.degrees(t.truncatingRemainder(dividingBy: 0.9) / 0.9 * 360))
        }
    }
}

struct Dots: View {
    var body: some View {
        TimelineView(.animation) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            HStack(spacing: 3) {
                ForEach(0..<3) { i in
                    Circle().fill(Color.white.opacity(0.35 + 0.65 * max(0, sin(t * 4 - Double(i) * 0.9))))
                        .frame(width: 5, height: 5)
                }
            }
        }
    }
}

struct SpeakingBars: View {
    var body: some View {
        TimelineView(.animation) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            HStack(alignment: .center, spacing: 3) {
                ForEach(0..<5) { i in
                    Capsule().fill(Palette.fri)
                        .frame(width: 3, height: 5 + 13 * abs(sin(t * (5 + Double(i)) + Double(i) * 1.3)))
                }
            }
        }
    }
}

/// The listening bars follow the real mic level.
struct LevelBars: View {
    let level: Float
    private let shape: [CGFloat] = [0.55, 0.85, 1.0, 0.8, 0.5]
    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30)) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            HStack(alignment: .center, spacing: 3) {
                ForEach(0..<5) { i in
                    let wobble = 0.75 + 0.25 * abs(sin(t * 9 + Double(i) * 1.7))
                    Capsule().fill(Palette.mic)
                        .frame(width: 3, height: 4 + 15 * CGFloat(level) * shape[i] * CGFloat(wobble))
                }
            }
            .animation(.easeOut(duration: 0.08), value: level)
        }
    }
}

struct Countdown: View {
    let started: Date
    let window: Double
    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.1)) { tl in
            let left = max(0, 1 - tl.date.timeIntervalSince(started) / window)
            ZStack {
                Circle().stroke(Palette.mic.opacity(0.22), lineWidth: 2.4)
                Circle().trim(from: 0, to: left).stroke(Palette.mic, style: StrokeStyle(lineWidth: 2.4, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            }
            .frame(width: 18, height: 18)
        }
    }
}

struct CCBadge: View {
    var body: some View {
        Text("CC").font(.system(size: 9, weight: .bold)).foregroundColor(.white)
            .padding(.horizontal, 3).padding(.vertical, 2)
            .overlay(RoundedRectangle(cornerRadius: 4).stroke(Palette.dim, lineWidth: 1.3))
    }
}

struct StepDots: View {
    let done: Int
    let total: Int
    var body: some View {
        HStack(spacing: 4) {
            ForEach(0..<min(total, 12), id: \.self) { i in
                Capsule().fill(i < done ? Palette.ok : (i == done ? Palette.fri : Palette.line)).frame(width: 14, height: 3)
            }
        }
        .padding(.top, 3)
    }
}

/// Live captions: the sentence being said now, with the words already spoken bright. Word timing isn't
/// known, so it's estimated from when the piece started (about 2.6 words a second). Quiet-mode captions
/// are shown whole.
enum CaptionText {
    static func text(_ c: Caption, at now: Date) -> Text {
        let sentences = split(c.line)
        guard !sentences.isEmpty else { return Text("") }
        if c.quiet { return Text(c.line).foregroundColor(.white) }
        let spoken = Int(now.timeIntervalSince(c.started) * 2.6)
        var before = 0
        for s in sentences {
            let words = s.split(separator: " ").map(String.init)
            if spoken < before + words.count || s == sentences.last {
                let k = max(1, min(words.count, spoken - before + 1))
                let said = words.prefix(k).joined(separator: " ")
                let rest = words.dropFirst(k).joined(separator: " ")
                return Text(said).foregroundColor(.white) + Text(rest.isEmpty ? "" : " " + rest).foregroundColor(Palette.faint)
            }
            before += words.count
        }
        return Text(c.line).foregroundColor(.white)
    }

    static func split(_ line: String) -> [String] {
        var out: [String] = []
        var cur = ""
        let chars = Array(line)
        for (i, ch) in chars.enumerated() {
            cur.append(ch)
            if ".!?".contains(ch), i + 1 < chars.count, chars[i + 1] == " " {
                out.append(cur.trimmingCharacters(in: .whitespaces))
                cur = ""
            }
        }
        let tail = cur.trimmingCharacters(in: .whitespaces)
        if !tail.isEmpty { out.append(tail) }
        return out
    }
}
