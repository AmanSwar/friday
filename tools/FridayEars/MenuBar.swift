// The menu bar icon: always there on every display, so it's the fallback if the island is ever out of
// sight. Its look says the state (blue while Friday works or talks, amber when something needs you, red
// when offline). Left-click opens the panel; right-click (or ⌃-click) shows the menu.

import Cocoa

final class MenuBar: NSObject {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    var onToggle: () -> Void = {}
    var buildMenu: () -> NSMenu = { NSMenu() }
    private var shown = ""

    override init() {
        super.init()
        item.button?.target = self
        item.button?.action = #selector(clicked)
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        set("waveform", tint: nil, dim: true, tip: "Friday is starting")
    }

    @objc private func clicked() {
        let e = NSApp.currentEvent
        if e?.type == .rightMouseUp || e?.modifierFlags.contains(.control) == true, let button = item.button {
            buildMenu().popUp(positioning: nil, at: NSPoint(x: 0, y: button.bounds.height + 4), in: button)
        } else {
            onToggle()
        }
    }

    func update(display: Display, server: ServerState, daemon: DaemonStatus) {
        switch daemon {
        case .starting: set("waveform", tint: nil, dim: true, tip: "Friday is starting"); return
        case .restarting: set("waveform.slash", tint: .systemRed, dim: false, tip: "Friday is offline (restarting)"); return
        case .ready: break
        }
        let amber = !server.permissions.isEmpty || server.masters.contains { $0.state == "needs" }
        let blue = NSColor(calibratedRed: 0.55, green: 0.70, blue: 1.0, alpha: 1)
        if amber {
            set("waveform.badge.exclamationmark", tint: .systemOrange, dim: false, tip: "Something needs you")
        } else if case .listening = display {
            set("mic.fill", tint: .systemOrange, dim: false, tip: "Listening")
        } else if server.talking || server.turn?.active == true || !server.judging.isEmpty {
            set("waveform", tint: blue, dim: false, tip: server.talking ? "Friday is talking" : "Friday is working")
        } else if server.quiet {
            set("captions.bubble", tint: nil, dim: false, tip: "Friday · quiet mode (captions only)")
        } else {
            set("waveform", tint: nil, dim: false, tip: "Friday · hold Right ⌥ to talk, double-tap for the panel")
        }
    }

    private func set(_ symbol: String, tint: NSColor?, dim: Bool, tip: String) {
        let key = "\(symbol)|\(tint?.description ?? "")|\(dim)|\(tip)"
        guard key != shown, let button = item.button else { return }
        shown = key
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Friday")
            ?? NSImage(systemSymbolName: "waveform", accessibilityDescription: "Friday")
        image?.isTemplate = true
        button.image = image
        button.contentTintColor = tint
        button.appearsDisabled = dim
        button.toolTip = tip
    }
}
