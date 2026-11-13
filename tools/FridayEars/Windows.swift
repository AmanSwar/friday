// The island's window and the panel's window.
//
// The island window is never ordered out: hidden means nothing is drawn and the window ignores the mouse.
// It catches clicks only over the island's own shape (checked 15 times a second against the pointer), so
// it never blocks the browser tabs under it. It lives in the MacBook's notch, never on the monitor (only
// with the lid closed does it fall back to the menu-bar screen, as a pill). Plugging in or removing a
// display, waking, and switching Spaces re-place it.

import Cocoa
import SwiftUI

final class KeyPanel: NSPanel {
    var onCancel: (() -> Void)?
    override var canBecomeKey: Bool { true }
    override func cancelOperation(_ sender: Any?) { onCancel?() }    // Esc
}

/// Buttons in a window that isn't key still work on the first click.
final class FirstMouseHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

func screenID(_ s: NSScreen) -> UInt32 {
    (s.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0
}

// MARK: - the island

final class IslandController {
    let window: NSPanel
    let model: AppModel
    private(set) var screen: NSScreen
    var hitRect: CGRect = .zero          // the island's shape, in SwiftUI coordinates (origin top-left)
    private var mouseTimer: Timer?

    /// The island lives in the MacBook's notch, never on the monitor (his choice, 12 Nov). Only with the
    /// lid closed (no notched screen) does it fall back to the screen with the menu bar.
    static func targetScreen() -> NSScreen {
        NSScreen.screens.first { $0.safeAreaInsets.top > 0 } ?? NSScreen.screens.first ?? NSScreen.main ?? NSScreen()
    }

    init(model: AppModel, actions: IslandActions) {
        self.model = model
        screen = Self.targetScreen()
        window = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 640, height: 300), styleMask: [.borderless, .nonactivatingPanel],
                         backing: .buffered, defer: false)
        window.level = NSWindow.Level(rawValue: NSWindow.Level.mainMenu.rawValue + 3)   // over the menu bar, at the notch
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = false
        window.isMovable = false
        window.hidesOnDeactivate = false
        window.ignoresMouseEvents = true
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        let root = IslandRoot(model: model, actions: actions, onFrame: { [weak self] r in self?.hitRect = r })
        window.contentView = FirstMouseHostingView(rootView: root)
        place(on: screen)
        window.orderFrontRegardless()

        let nc = NotificationCenter.default
        nc.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            self?.relayout("displays changed")
        }
        let ws = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didWakeNotification, NSWorkspace.screensDidWakeNotification, NSWorkspace.activeSpaceDidChangeNotification] {
            ws.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in self?.relayout(nil) }
        }
        let t = Timer(timeInterval: 1.0 / 15, repeats: true) { [weak self] _ in self?.trackMouse() }
        RunLoop.main.add(t, forMode: .common)
        mouseTimer = t
    }

    static func mouseScreen() -> NSScreen {
        let p = NSEvent.mouseLocation
        return NSScreen.screens.first { NSMouseInRect(p, $0.frame, false) } ?? NSScreen.main ?? NSScreen.screens.first ?? NSScreen()
    }

    static func geometry(for s: NSScreen) -> IslandGeometry {
        let inset = s.safeAreaInsets.top
        if inset > 0, let l = s.auxiliaryTopLeftArea, let r = s.auxiliaryTopRightArea {
            let notch = CGSize(width: s.frame.width - l.width - r.width, height: inset)
            return IslandGeometry(notch: true, notchSize: notch, top: inset, window: CGSize(width: 640, height: inset + 250))
        }
        let menuBar = max(0, s.frame.maxY - s.visibleFrame.maxY)
        let top = (menuBar > 40 ? 0 : menuBar) + 6       // under the menu bar (or the top edge if it auto-hides)
        return IslandGeometry(notch: false, notchSize: .zero, top: top, window: CGSize(width: 640, height: top + 250))
    }

    func place(on s: NSScreen) {
        screen = s
        let g = Self.geometry(for: s)
        let frame = NSRect(x: (s.frame.midX - g.window.width / 2).rounded(), y: s.frame.maxY - g.window.height,
                           width: g.window.width, height: g.window.height)
        if window.frame != frame { window.setFrame(frame, display: true) }
        if model.geometry != g { model.geometry = g }
    }

    private func relayout(_ why: String?) {
        let s = Self.targetScreen()
        place(on: s)
        window.orderFrontRegardless()
        if let why { log("island re-placed (\(why)); on \(s.localizedName)") }
    }

    /// The island is about to show something after showing nothing: open it where the pointer is.
    func displayChanged(from old: Display, to new: Display) {
        if old.isEmpty && !new.isEmpty {
            let s = Self.targetScreen()
            if screenID(s) != screenID(screen) { place(on: s) }
        }
        if !new.isEmpty { window.orderFrontRegardless() }
    }

    private func trackMouse() {
        guard !hitRect.isEmpty else {
            if !window.ignoresMouseEvents { window.ignoresMouseEvents = true }
            return
        }
        let f = window.frame
        let r = NSRect(x: f.minX + hitRect.minX, y: f.maxY - hitRect.maxY, width: hitRect.width, height: hitRect.height)
        let inside = NSMouseInRect(NSEvent.mouseLocation, r.insetBy(dx: -2, dy: -2), false)
        if window.ignoresMouseEvents == inside { window.ignoresMouseEvents = !inside }
    }
}

// MARK: - the panel

final class PanelController {
    let panel: KeyPanel
    let model: AppModel
    var pinned = false
    private var previousApp: NSRunningApplication?
    private var clickMonitor: Any?

    var isOpen: Bool { panel.isVisible }

    init(model: AppModel, actions: PanelActions) {
        self.model = model
        panel = KeyPanel(contentRect: NSRect(x: 0, y: 0, width: 440, height: 600), styleMask: [.borderless, .nonactivatingPanel],
                         backing: .buffered, defer: false)
        panel.level = NSWindow.Level(rawValue: NSWindow.Level.mainMenu.rawValue + 2)    // under the island
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.isMovable = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.contentView = FirstMouseHostingView(rootView: PanelView(model: model, actions: actions))
        panel.onCancel = { [weak self] in self?.close() }
        // A click into another app closes it, unless pinned.
        NotificationCenter.default.addObserver(forName: NSApplication.didResignActiveNotification, object: nil, queue: .main) { [weak self] _ in
            guard let self, self.isOpen, !self.pinned else { return }
            self.close(restore: false)
        }
    }

    func toggle(on screen: NSScreen) {
        isOpen ? close() : open(on: screen)
    }

    func open(on screen: NSScreen) {
        let g = IslandController.geometry(for: screen)
        let width: CGFloat = 440
        let top = screen.frame.maxY - (g.notch ? g.top : g.top - 6) - 64     // room for a one-line island above it
        let height = min(660, top - screen.visibleFrame.minY - 16)
        panel.setFrame(NSRect(x: (screen.frame.midX - width / 2).rounded(), y: top - height, width: width, height: height), display: true)
        let front = NSWorkspace.shared.frontmostApplication
        previousApp = front?.bundleIdentifier == Bundle.main.bundleIdentifier ? nil : front
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        model.panelOpen = true
        // macOS may not make Friday the active app (cooperative activation), so "a click elsewhere
        // closes it" can't rely on Friday losing focus: watch for clicks in other apps instead.
        clickMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]) { [weak self] _ in
            guard let self, self.isOpen, !self.pinned else { return }
            self.close(restore: false)
        }
    }

    func focus() {
        guard isOpen else { return }
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKey()
    }

    func close(restore: Bool = true) {
        if let m = clickMonitor { NSEvent.removeMonitor(m); clickMonitor = nil }
        guard isOpen else { return }
        panel.orderOut(nil)
        model.panelOpen = false
        if restore { previousApp?.activate() }
        previousApp = nil
    }
}
