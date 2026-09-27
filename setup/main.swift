// HobsonSetup — the window the setup wizard runs in.
//
// The wizard is a page (setup/ui/) served by scripts/setup_wizard.py on
// 127.0.0.1. This app is only its window: a WKWebView in a dark, title-less
// NSWindow, with an Edit menu so a pasted API key works. Every decision and
// every action lives in Python; nothing here reads or writes Hobson's state.
//
//   HobsonSetup --url http://127.0.0.1:PORT/?t=TOKEN
//
// The page closes the window with
//   window.webkit.messageHandlers.hobson.postMessage({close: true})
// and closing the window quits the app, which is how setup_wizard.py (waiting
// on `open -W`) knows the wizard is over.

import AppKit
import WebKit

/// The strip under the traffic lights: the page draws there, the window is
/// dragged from there. A WKWebView swallows mouse-downs, so it needs its own.
final class DragStrip: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
    override func mouseDown(with event: NSEvent) { window?.performDrag(with: event) }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler {
    let url: URL
    var window: NSWindow!

    init(url: URL) { self.url = url }

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let frame = NSRect(x: 0, y: 0, width: 1120, height: 740)
        window = NSWindow(contentRect: frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Hobson Setup"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = .black
        window.minSize = NSSize(width: 960, height: 640)
        window.delegate = self

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "hobson")
        config.mediaTypesRequiringUserActionForPlayback = []
        let web = WKWebView(frame: frame, configuration: config)
        web.setValue(false, forKey: "drawsBackground")  // no white flash before the page paints
        web.autoresizingMask = [.width, .height]

        let container = NSView(frame: frame)
        container.addSubview(web)
        let strip = DragStrip(frame: NSRect(x: 0, y: frame.height - 30, width: frame.width, height: 30))
        strip.autoresizingMask = [.width, .minYMargin]
        container.addSubview(strip)
        window.contentView = container

        web.load(URLRequest(url: url))
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        if let body = message.body as? [String: Any], body["close"] as? Bool == true {
            window.close()
        }
    }

    /// Without a main menu, Cmd-V does nothing in a WKWebView text field.
    private func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit Hobson Setup", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowItem.submenu = windowMenu
        main.addItem(windowItem)
        NSApp.mainMenu = main
    }
}

func urlArgument() -> URL? {
    let args = CommandLine.arguments
    guard let i = args.firstIndex(of: "--url"), i + 1 < args.count else { return nil }
    return URL(string: args[i + 1])
}

guard let url = urlArgument() else {
    FileHandle.standardError.write("usage: HobsonSetup --url URL\n".data(using: .utf8)!)
    exit(2)
}
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate(url: url)
app.delegate = delegate
app.run()
