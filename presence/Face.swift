// Hobson's face: the line he is speaking, in a small window.
//
// scripts/face.py writes each line that is played to ~/.claude/hobson-face.json
// and sends this helper SIGUSR2. The line is read at once, with the loudness of
// the audio it names: say's AIFF is removed as soon as afplay finishes, and a
// daemon's playback WAV is overwritten by the next phrase. Then the page
// (presence/face/, loaded from the checkout) draws Valet saying it, in a
// borderless panel that never takes focus: typing carries on wherever it was.
//
// The page is told what to say through callAsyncJavaScript's arguments, never
// by building script text, and shows the caption through textContent.
//
// The window goes once the line has been said, or, for a line that waits on
// you, once you type in that session (its activity token is newer than the
// line), for at most ten minutes. A click dismisses it; a drag moves it, and
// the position is kept. He looks towards where the camera last saw you: the
// face boxes stay in this process, as they always have.

import AppKit
import AVFoundation
import WebKit

/// One line for the face, as face.py wrote it.
struct FaceLine {
    let id: String
    let ts: Double
    let phrase: String
    let kind: String
    let audio: String?
    let project: String?
    let width: Double
    let activity: String?   // a line that waits on you: this session's activity token
    let asked: Bool         // you asked for it (Ask Hobson): shown with the face off too

    /// Older than this, a line is not shown at all: it has been said.
    static let maxAge = 10.0

    static func read(_ path: String) -> FaceLine? {
        guard let data = FileManager.default.contents(atPath: path),
              let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let id = o["id"] as? String, let ts = (o["ts"] as? NSNumber)?.doubleValue,
              let phrase = o["phrase"] as? String, !phrase.isEmpty else { return nil }
        let width = (o["width"] as? NSNumber)?.doubleValue ?? 280
        return FaceLine(id: id, ts: ts, phrase: phrase, kind: o["kind"] as? String ?? "info",
                        audio: o["audio"] as? String, project: o["project"] as? String,
                        width: min(max(width, 200), 560), activity: o["activity"] as? String,
                        asked: o["asked"] as? Bool ?? false)
    }
}

/// The loudness of a clip, one value per `frame` seconds, 0...1.
enum Loudness {
    static let frame = 0.016

    static func of(_ path: String) -> (env: [Double], duration: Double)? {
        guard let file = try? AVAudioFile(forReading: URL(fileURLWithPath: path)) else { return nil }
        let format = file.processingFormat
        let rate = format.sampleRate
        guard file.length > 0, rate > 0, Double(file.length) < rate * 120,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(file.length)),
              (try? file.read(into: buffer)) != nil,
              let channels = buffer.floatChannelData else { return nil }
        let n = Int(buffer.frameLength), count = Int(format.channelCount)
        guard n > 0, count > 0 else { return nil }
        let window = max(1, Int(rate * frame))
        var rms: [Double] = []
        rms.reserveCapacity(n / window + 1)
        var i = 0
        while i < n {
            let end = min(n, i + window)
            var sum = 0.0
            for c in 0..<count {
                let samples = channels[c]
                for j in i..<end { let v = Double(samples[j]); sum += v * v }
            }
            rms.append((sum / Double((end - i) * count)).squareRoot())
            i = end
        }
        // Normalised to a loud frame rather than the loudest click, and the
        // quiet between words is silence: the mouth closes on it.
        let sorted = rms.sorted()
        let loud = sorted[min(sorted.count - 1, Int(Double(sorted.count) * 0.95))]
        let duration = Double(n) / rate
        guard loud > 1e-5 else { return (rms.map { _ in 0 }, duration) }
        return (rms.map { v in
            let x = min(1, v / loud)
            return x < 0.06 ? 0 : pow(x, 0.8)
        }, duration)
    }
}

/// A borderless panel that never becomes key: clicks reach it without
/// taking focus from wherever you are typing.
final class FacePanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

/// Over the page, taking the mouse: a click dismisses, a drag moves.
final class FaceOverlay: NSView {
    var onClick: (() -> Void)?
    var onMoved: (() -> Void)?
    private var grabbed: NSPoint?
    private var origin = NSPoint.zero
    private var travel: CGFloat = 0

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        grabbed = NSEvent.mouseLocation
        origin = window?.frame.origin ?? .zero
        travel = 0
    }

    override func mouseDragged(with event: NSEvent) {
        guard let start = grabbed, let window = window else { return }
        let at = NSEvent.mouseLocation
        let dx = at.x - start.x, dy = at.y - start.y
        travel = max(travel, hypot(dx, dy))
        if travel >= 3 { window.setFrameOrigin(NSPoint(x: origin.x + dx, y: origin.y + dy)) }
    }

    override func mouseUp(with event: NSEvent) {
        guard grabbed != nil else { return }
        grabbed = nil
        if travel < 3 { onClick?() } else { onMoved?() }
    }
}

/// Messages from the page, without the content controller holding the
/// controller (it retains its handlers).
final class FaceMessages: NSObject, WKScriptMessageHandler {
    weak var owner: FaceController?
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        owner?.received(message.body)
    }
}

/// The window and what it shows. Main thread only.
final class FaceController: NSObject, WKNavigationDelegate {
    let recordPath: String
    let page: URL
    private var panel: FacePanel?
    private var web: WKWebView?
    private let messages = FaceMessages()
    private var ready = false
    private var pending: [String: Any]?
    private var latest = ""          // the id of the newest line read
    private var line: FaceLine?      // the line on screen
    private var hideTimer: Timer?
    private var holdTimer: Timer?
    private var goneTimer: Timer?    // orderOut if the page never says it has gone
    private var releaseTimer: Timer?
    private var hiding = false       // Face.hide() sent; "hidden" will follow
    private(set) var showing = false
    /// The line on screen was asked for: the face being off does not take it down.
    var showingAsked: Bool { showing && (line?.asked ?? false) }
    /// "One moment." waits for the answer that replaces it, this long at most.
    static let maxThinking = 25.0
    private var shownAt = 0.0
    private var followed = ""        // "audio 3.1s" or "the text": what the mouth followed
    private var why = "said"         // why the line on screen goes
    /// presence.py arguments, run detached: how the window reports to hobson.log
    /// (the helper never writes the log itself).
    var log: (([String]) -> Void)?

    static let defaultsKey = "faceTopLeft"
    /// A said line stays at most this long, whatever its audio claims.
    static let maxShown = 30.0
    /// A line that waits on you stays at most this long.
    static let maxHeld = 600.0
    /// Hidden this long, the web view (and its WebContent process) is let go.
    static let releaseAfter = 600.0

    init(home: String, page: String) {
        recordPath = "\(home)/hobson-face.json"
        let url = URL(fileURLWithPath: page)
        self.page = url.hasDirectoryPath || url.pathExtension.isEmpty ? url.appendingPathComponent("index.html") : url
        super.init()
        messages.owner = self
    }

    // MARK: A line arrives

    /// SIGUSR2, or a start: read the newest line, and its audio, now. With
    /// the face off only a line you asked for is shown.
    func lineArrived(faceOn: Bool = true) {
        guard let next = FaceLine.read(recordPath), next.id != latest, faceOn || next.asked else { return }
        let age = now() - next.ts
        guard age < FaceLine.maxAge else {
            // A line from long ago (read at a start) is not news; one just missed is.
            if age < 60 { note("skipped", next, String(format: "too old (%.0fs) when read", age)) }
            latest = next.id
            return
        }
        latest = next.id
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let audio = next.audio.flatMap { Loudness.of($0) }
            DispatchQueue.main.async { self?.show(next, audio) }
        }
    }

    private func show(_ next: FaceLine, _ audio: (env: [Double], duration: Double)?) {
        guard next.id == latest else { return }  // a newer line came in meanwhile
        if let path = next.audio, audio == nil { note("audio", next, path) }
        if let a = audio, now() - next.ts > a.duration - 0.5 {  // a cold start: nearly over
            note("skipped", next, String(format: "nearly over (%.1fs of %.1fs) after a cold start",
                                         now() - next.ts, a.duration))
            return
        }
        if showing, line != nil {
            why = "replaced"
            report()
        }
        line = next
        shownAt = now()
        why = "said"
        followed = audio.map { String(format: "audio %.1fs", $0.duration) } ?? "the text"
        var u: [String: Any] = ["text": next.phrase, "kind": next.kind, "project": next.project ?? "",
                                "start": next.ts, "hold": next.activity != nil || next.kind == "thinking"]
        if let a = audio {
            u["env"] = a.env
            u["frame"] = Loudness.frame
        }
        present(width: next.width)
        if ready { say(u) } else { pending = u }
    }

    private func say(_ u: [String: Any]) {
        guard let web = web, let shown = line else { return }
        web.callAsyncJavaScript("return Face.say(u);", arguments: ["u": u], in: nil, in: .page) { [weak self] result in
            var duration = 4.0
            if case .success(let value) = result, let d = (value as? NSNumber)?.doubleValue { duration = d }
            self?.schedule(shown, duration: duration)
        }
    }

    private func schedule(_ shown: FaceLine, duration: Double) {
        guard shown.id == line?.id else { return }
        hideTimer?.invalidate()
        holdTimer?.invalidate()
        if shown.kind == "thinking" {
            // Until the answer replaces it; by the planner it lasts a second.
            hideTimer = Timer.scheduledTimer(withTimeInterval: FaceController.maxThinking, repeats: false) { [weak self] _ in
                self?.hide("cap")
            }
        } else if let token = shown.activity {
            holdTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
                let typed = ((try? FileManager.default.attributesOfItem(atPath: token))?[.modificationDate] as? Date)
                    .map { $0.timeIntervalSince1970 > shown.ts } ?? false
                if typed { self?.hide("typed") } else if now() - shown.ts > FaceController.maxHeld { self?.hide("cap") }
            }
        } else {
            let wanted = max(0.5, shown.ts + duration + 1.5 - now())
            let delay = min(FaceController.maxShown, wanted)
            let reason = wanted > delay ? "cap" : "said"
            hideTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in self?.hide(reason) }
        }
    }

    // MARK: The window

    private func present(width: Double) {
        releaseTimer?.invalidate()
        goneTimer?.invalidate()
        hiding = false
        let panel = self.panel ?? makePanel()
        if web == nil { makeWeb(in: panel) }
        let frame = panel.frame
        if !showing || abs(frame.width - CGFloat(width)) > 0.5 {
            place(panel, width: CGFloat(width), height: frame.height)
        }
        showing = true
        // Launched with `open -j`, the app starts hidden, and so would this.
        NSApplication.shared.unhideWithoutActivation()
        panel.orderFrontRegardless()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) { [weak panel] in panel?.invalidateShadow() }
    }

    /// `reason` goes to the log: said, typed, clicked, cap, face off.
    func hide(_ reason: String = "said") {
        hideTimer?.invalidate()
        holdTimer?.invalidate()
        if !hiding { why = reason }
        guard showing, let web = web else { return gone() }
        hiding = true
        web.callAsyncJavaScript("Face.hide();", arguments: [:], in: nil, in: .page, completionHandler: nil)
        goneTimer?.invalidate()
        goneTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: false) { [weak self] _ in self?.gone() }
    }

    private func gone() {
        goneTimer?.invalidate()
        hiding = false
        showing = false
        report()
        line = nil
        panel?.orderOut(nil)
        releaseTimer?.invalidate()
        releaseTimer = Timer.scheduledTimer(withTimeInterval: FaceController.releaseAfter, repeats: false) { [weak self] _ in
            self?.release()
        }
    }

    /// The line on screen, once, as it goes.
    private func report() {
        guard let l = line else { return }
        note("shown", l, String(format: "%.1fs, following %@, gone: %@", now() - shownAt, followed, why))
    }

    private func note(_ event: String, _ l: FaceLine?, _ detail: String) {
        log?(["--face-event", event, l?.phrase ?? "", l?.project ?? "", detail])
    }

    /// Memory: a WebContent process is 60-100 MB, for a window seen now and then.
    private func release() {
        guard !showing else { return }
        web?.configuration.userContentController.removeScriptMessageHandler(forName: "face")
        web?.removeFromSuperview()
        web = nil
        ready = false
    }

    private func makePanel() -> FacePanel {
        let p = FacePanel(contentRect: NSRect(x: 0, y: 0, width: 280, height: 430),
                          styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.level = .floating
        p.isFloatingPanel = true
        p.hidesOnDeactivate = false
        p.becomesKeyOnlyIfNeeded = true
        p.isReleasedWhenClosed = false
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.title = "Hobson"
        let root = NSView(frame: p.contentLayoutRect)
        root.autoresizingMask = [.width, .height]
        p.contentView = root
        let overlay = FaceOverlay(frame: root.bounds)
        overlay.autoresizingMask = [.width, .height]
        overlay.onClick = { [weak self] in self?.hide("clicked") }
        overlay.onMoved = { [weak self, weak p] in
            guard let f = p?.frame else { return }
            UserDefaults.standard.set([Double(f.minX), Double(f.maxY)], forKey: FaceController.defaultsKey)
            self?.panel?.invalidateShadow()
        }
        root.addSubview(overlay)
        panel = p
        return p
    }

    private func makeWeb(in panel: FacePanel) {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.userContentController.add(messages, name: "face")
        let view = WKWebView(frame: panel.contentView!.bounds, configuration: config)
        view.autoresizingMask = [.width, .height]
        view.setValue(false, forKey: "drawsBackground")  // no white flash before the page paints
        view.navigationDelegate = self
        panel.contentView!.addSubview(view, positioned: .below, relativeTo: nil)
        view.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent())
        web = view
        ready = false
        // The one failure nothing else would show: a page that never runs (its
        // script blocked or missing) leaves an empty, invisible window.
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self, weak view] in
            guard let self = self, let view = view, view === self.web, !self.ready else { return }
            self.note("page", nil, "not ready 5s after loading \(self.page.path) (script blocked or missing?)")
        }
    }

    /// The top-left corner where you last dragged it, if that is still on a
    /// screen; else the top right of the main screen, under the menu bar.
    private func place(_ panel: FacePanel, width: CGFloat, height: CGFloat) {
        var topLeft: NSPoint?
        if let saved = UserDefaults.standard.array(forKey: FaceController.defaultsKey) as? [Double], saved.count == 2 {
            let p = NSPoint(x: saved[0], y: saved[1])
            if NSScreen.screens.contains(where: { $0.visibleFrame.insetBy(dx: -2, dy: -2).contains(p) }) { topLeft = p }
        }
        if topLeft == nil, let screen = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame {
            topLeft = NSPoint(x: screen.maxX - width - 16, y: screen.maxY - 10)
        }
        let at = topLeft ?? NSPoint(x: 40, y: 800)
        panel.setFrame(NSRect(x: at.x, y: at.y - height, width: width, height: height), display: true)
    }

    // MARK: The page

    func received(_ body: Any) {
        guard let msg = body as? [String: Any], let type = msg["type"] as? String else { return }
        switch type {
        case "ready":
            ready = true
            if let u = pending { pending = nil; say(u) }
        case "size":
            guard let panel = panel, let h = (msg["h"] as? NSNumber)?.doubleValue, h > 50, h < 2000 else { return }
            let f = panel.frame
            if abs(f.height - CGFloat(h)) > 0.5 {
                panel.setFrame(NSRect(x: f.minX, y: f.maxY - CGFloat(h), width: f.width, height: CGFloat(h)), display: true)
                panel.invalidateShadow()
            }
        case "hidden":
            if hiding { gone() }
        default:
            break
        }
    }

    /// Only the page itself: nothing else is ever loaded in this view.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let target = action.request.url
        decisionHandler(target?.isFileURL == true && target?.path == page.path ? .allow : .cancel)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        note("page", nil, "crashed (its WebContent process ended), reloading")
        ready = false
        webView.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent())
    }

    // MARK: Gaze

    /// Where you are from his point of view, from the largest face the camera
    /// saw: -1...1 each way, or nothing if no one was seen lately. The camera
    /// sits at the top centre of the main screen and you about 60 cm from it.
    func gaze(_ sighting: Sighting?, age: Double) {
        guard showing, ready, let web = web, let panel = panel else { return }
        var args: [String: Any] = ["x": NSNull(), "y": NSNull()]
        if let s = sighting, age < 20,
           let face = s.faces.max(by: { $0.width * $0.height < $1.width * $1.height }),
           let screen = NSScreen.main ?? NSScreen.screens.first {
            let (x, y) = FaceController.direction(face: face, window: panel.frame, screen: screen)
            args = ["x": x, "y": y]
        }
        web.callAsyncJavaScript("Face.gaze(x, y);", arguments: args, in: nil, in: .page, completionHandler: nil)
    }

    static func direction(face: CGRect, window: NSRect, screen: NSScreen) -> (Double, Double) {
        let distance = 0.6                          // metres from the camera
        let fieldWide = 2 * distance * tan(0.61)    // about 70 degrees across
        let fieldHigh = fieldWide * 9 / 16
        // Vision's box is unmirrored with its origin bottom-left: your right
        // (as you face the screen) is the frame's left.
        let youX = (0.5 - Double(face.midX)) * fieldWide
        let youY = (Double(face.midY) - 0.5) * fieldHigh
        // The window, in metres from the camera, from the screen's real size.
        var metresPerPoint = 0.0002
        if let id = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber {
            let mm = CGDisplayScreenSize(CGDirectDisplayID(id.uint32Value))
            if mm.width > 0 { metresPerPoint = Double(mm.width) / 1000 / Double(screen.frame.width) }
        }
        let f = screen.frame
        let winX = Double(window.midX - f.midX) * metresPerPoint
        let winY = Double(window.maxY - window.height * 0.25 - f.maxY) * metresPerPoint  // his eyes
        let yaw = atan2(youX - winX, distance), pitch = atan2(youY - winY, distance)
        return (max(-1, min(1, yaw / 0.6)), max(-1, min(1, pitch / 0.6)))
    }
}

