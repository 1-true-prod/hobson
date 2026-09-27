// HobsonPresence — the one sensor behind Hobson's presence awareness.
//
// Hobson used to speak into the void: every finish, nudge and watchdog line
// went out whether or not anyone was at the desk. This helper answers one
// question, "is anyone listening?", and nothing else. It writes a small JSON
// state file, ~/.claude/hobson-presence.json, and on every change of state runs
// `python3 scripts/presence.py --transition FROM TO --source SRC`, detached.
// Every decision about what to say lives in Python; this is only the sensor.
//
// States: present, away, call (the microphone is in use), company (two or more
// people in frame). Signals, cheapest first:
//   - screen locked, or another user on the console          -> away (lock)
//   - display asleep                                         -> away (display)
//   - a call app (Zoom, Meet in a browser, Slack...) capturing
//     the microphone; Hobson never opens it                 -> call
//   - keyboard or mouse touched within --idle-seconds        -> present (input)
//   - the camera (a human face, nothing else), by --mode:
//       signals     never
//       auto        one short look, only when Hobson is about to speak and you
//                   have been idle (SIGUSR1 from presence.py); a look that
//                   finds nobody holds until the next keystroke or unlock
//       continuous  one frame a second: leaving is noticed within
//                   --away-after seconds, and company is seen at all
//
// Waves (continuous mode): 15 times a second the latest frame goes through
// Vision's hand pose; a raised, open hand swinging side to side three times
// within 2.5 seconds, with a human face in the same frame, runs
// `presence.py --wave`, and Hobson answers. Only faces are people: not
// bodies, not outlines, not anything else that moves.
//
// The phone switch (--phone): an Android phone lying face down turns the camera
// off, face up turns it on -- read over adb from the accelerometer Android keeps
// running for its own face-down detection. A switch that cannot be read counts
// as off: it is a privacy control, and a failure must not open the camera.
//
// Privacy: frames live in memory for one Vision request and are never saved,
// logged or sent anywhere. Only the state, its source and a person count are
// written. The camera prompt appears only with --request-permission, which
// `hobson presence setup` passes; otherwise an undecided permission means no
// camera, never a surprise dialog in the middle of work.
//
// Built by scripts/build-presence.sh into build/HobsonPresence.app, and
// launched with `open` so that the camera permission belongs to this bundle,
// not to whichever terminal ran the hook.

import AppKit
import AVFoundation
import CoreAudio
import CoreGraphics
import Foundation
import Vision

// MARK: - Options

struct Options {
    var home = NSString(string: "~/.claude").expandingTildeInPath
    var mode = "auto"            // signals | auto | continuous
    var idleSeconds = 60.0       // untouched this long before the camera may be asked
    var awayAfter = 30.0         // continuous: no face this long means away
    var interval = 1.0           // seconds between ticks
    var exitAfter = 1800.0       // no hook activity this long (and nothing held) -> exit
    var python: String?
    var script: String?
    var requestPermission = false
    var command: String?         // "signals" | "look" | "phone" | nil (run as the sensor)
    var out: String?
    var phone: String?           // the camera switch: "auto" or an adb serial prefix
    var adb = "adb"
    var preview = false          // a floating window: the feed and what the sensor makes of it
}

func parseOptions() -> Options {
    var o = Options()
    var args = Array(CommandLine.arguments.dropFirst())
    func take() -> String? { args.isEmpty ? nil : args.removeFirst() }
    func number(_ fallback: Double) -> Double { take().flatMap(Double.init) ?? fallback }
    while let arg = take() {
        switch arg {
        case "--home": o.home = take() ?? o.home
        case "--mode": o.mode = take() ?? o.mode
        case "--idle-seconds": o.idleSeconds = number(o.idleSeconds)
        case "--away-after": o.awayAfter = number(o.awayAfter)
        case "--interval": o.interval = max(0.25, number(o.interval))
        case "--exit-after": o.exitAfter = number(o.exitAfter)
        case "--python": o.python = take()
        case "--script": o.script = take()
        case "--out": o.out = take()
        case "--call-app": if let id = take() { Signals.callApps.append(id) }
        case "--request-permission": o.requestPermission = true
        case "--signals": o.command = "signals"
        case "--phone": o.phone = take()
        case "--adb": o.adb = take() ?? o.adb
        case "--read-phone": o.command = "phone"
        case "--preview": o.preview = true
        case "--look": o.command = "look"
        default: break  // LaunchServices may add its own arguments
        }
    }
    if !["signals", "auto", "continuous"].contains(o.mode) { o.mode = "auto" }
    return o
}

func now() -> Double { Date().timeIntervalSince1970 }

// MARK: - Tier 0: signals that need no camera and no permission

enum Signals {
    /// Seconds since any keyboard, mouse or trackpad event.
    static func idleSeconds() -> Double {
        guard let any = CGEventType(rawValue: ~0) else { return 0 }
        return CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: any)
    }

    /// Screen locked, and whether this user's session is the one on screen
    /// (fast user switching puts it in the background).
    static func session() -> (locked: Bool, onConsole: Bool) {
        guard let d = CGSessionCopyCurrentDictionary() as? [String: Any] else { return (false, true) }
        func flag(_ key: String) -> Bool? {
            if let b = d[key] as? Bool { return b }
            if let n = d[key] as? NSNumber { return n.boolValue }
            return nil
        }
        return (flag("CGSSessionScreenIsLocked") ?? false, flag("kCGSSessionOnConsoleKey") ?? true)
    }

    static func displayAsleep() -> Bool { CGDisplayIsAsleep(CGMainDisplayID()) != 0 }

    /// Apps whose use of the microphone means a call. A known list, not "any
    /// app using the mic": the first real machine this ran on had OBS holding
    /// the microphone all day, which would have held every word Hobson said.
    /// An app not listed here changes nothing, which is the behaviour Hobson
    /// had before; `--call-app` adds more. Matched as bundle-id prefixes, so a
    /// browser's helper processes count as the browser.
    static var callApps = [
        "us.zoom.", "com.microsoft.teams", "com.tinyspeck.slackmacgap", "com.apple.FaceTime",
        "com.hnc.Discord", "Cisco-Systems.Spark", "com.cisco.webexmeetingsapp", "net.whatsapp.",
        "ru.keepcoder.Telegram", "org.whispersystems.signal-desktop", "app.tuple.", "com.around.",
        "com.skype.", "com.google.Chrome", "com.brave.Browser", "com.microsoft.edgemac",
        "org.mozilla.firefox", "company.thebrowser.", "com.vivaldi.", "com.operasoftware.",
        "com.apple.Safari", "com.apple.WebKit",
    ]

    /// The call app capturing audio input right now, or nil. Uses Core Audio's
    /// per-process objects (macOS 14.2): the device-level "running somewhere"
    /// flag cannot say who, and a recorder is not a call.
    static func callInProgress() -> String? {
        guard #available(macOS 14.2, *) else { return nil }
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyProcessObjectList,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        let system = AudioObjectID(kAudioObjectSystemObject)
        var size = UInt32(0)
        guard AudioObjectGetPropertyDataSize(system, &address, 0, nil, &size) == noErr, size > 0 else {
            return nil
        }
        var processes = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(system, &address, 0, nil, &size, &processes) == noErr else {
            return nil
        }
        for process in processes {
            var input = UInt32(0)
            var inputSize = UInt32(MemoryLayout<UInt32>.size)
            address.mSelector = kAudioProcessPropertyIsRunningInput
            guard AudioObjectGetPropertyData(process, &address, 0, nil, &inputSize, &input) == noErr,
                  input != 0 else { continue }
            var bundle: Unmanaged<CFString>?
            var bundleSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
            address.mSelector = kAudioProcessPropertyBundleID
            guard AudioObjectGetPropertyData(process, &address, 0, nil, &bundleSize, &bundle) == noErr,
                  let id = bundle?.takeRetainedValue() as String?, !id.isEmpty else { continue }
            if callApps.contains(where: { id.hasPrefix($0) }) { return id }
        }
        return nil
    }

    static func snapshot() -> [String: Any] {
        let (locked, onConsole) = session()
        var record: [String: Any] = ["idle": (idleSeconds() * 10).rounded() / 10, "locked": locked,
                                     "on_console": onConsole, "display_asleep": displayAsleep()]
        record["call_app"] = callInProgress() ?? NSNull()
        return record
    }
}

// MARK: - The camera

struct Sighting {
    var people: Int      // human faces in the frame
    var confident: Int   // distinct faces sure enough to count as company
    var faces: [CGRect] = []   // for the debug window only: never written anywhere
    var frameSize = CGSize.zero
}

final class Camera: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let frames = DispatchQueue(label: "hobson.presence.frames")
    private(set) var session: AVCaptureSession?
    private var latest: CVPixelBuffer?   // touched on `frames` only
    private(set) var deviceName: String?
    /// Told when capture starts and stops (the debug window shows the feed).
    var onSession: ((AVCaptureSession?) -> Void)?

    var running: Bool { session != nil }

    /// A virtual camera (OBS, Snap, mmhmm...) shows a black or looping frame
    /// forever, which would read as "away" forever.
    static func isVirtual(_ device: AVCaptureDevice) -> Bool {
        if device.transportType == Int32(bitPattern: 0x7669_7274) { return true }  // 'virt'
        let name = "\(device.localizedName) \(device.modelID)".lowercased()
        return ["virtual", "obs", "snap camera", "mmhmm", "camo", "ndi"].contains { name.contains($0) }
    }

    /// The built-in camera first, then a real external one. Never a virtual
    /// camera, never a suspended one (a closed lid), never the iPhone
    /// (Continuity Camera is wherever the phone is, not at the desk).
    static func pickDevice() -> AVCaptureDevice? {
        let usable = { (d: AVCaptureDevice) in !isVirtual(d) && !d.isSuspended && d.isConnected }
        let builtIn = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.builtInWideAngleCamera], mediaType: .video, position: .unspecified).devices
        if let d = builtIn.first(where: usable) { return d }
        var types: [AVCaptureDevice.DeviceType]
        if #available(macOS 14.0, *) {
            types = [.external]
        } else {
            types = [.externalUnknown]
        }
        let external = AVCaptureDevice.DiscoverySession(
            deviceTypes: types, mediaType: .video, position: .unspecified).devices
        return external.first(where: usable)
    }

    static func status() -> String {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: return pickDevice() == nil ? "unavailable" : "authorized"
        case .denied: return "denied"
        case .restricted: return "restricted"
        case .notDetermined: return "not-determined"
        @unknown default: return "unknown"
        }
    }

    func start() -> Bool {
        if session != nil { return true }
        guard AVCaptureDevice.authorizationStatus(for: .video) == .authorized,
              let device = Camera.pickDevice(),
              let input = try? AVCaptureDeviceInput(device: device) else { return false }
        let s = AVCaptureSession()
        s.beginConfiguration()
        if s.canSetSessionPreset(.vga640x480) {
            s.sessionPreset = .vga640x480
        } else if s.canSetSessionPreset(.low) {
            s.sessionPreset = .low
        }
        let output = AVCaptureVideoDataOutput()
        output.alwaysDiscardsLateVideoFrames = true
        output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        output.setSampleBufferDelegate(self, queue: frames)
        guard s.canAddInput(input), s.canAddOutput(output) else { return false }
        s.addInput(input)
        s.addOutput(output)
        s.commitConfiguration()
        s.startRunning()
        session = s
        deviceName = device.localizedName
        onSession?(s)
        return true
    }

    func stop() {
        session?.stopRunning()
        session = nil
        frames.sync { latest = nil }
        onSession?(nil)
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        latest = CMSampleBufferGetImageBuffer(sampleBuffer)
    }

    func latestFrame() -> CVPixelBuffer? { frames.sync { latest } }

    /// The people in one frame are its human faces, and nothing else: only a
    /// face is sure to be a person, so bodies, outlines and anything else that
    /// moves do not count. Faces do lose someone who looks down (measured: 0
    /// of 30 frames, seated at a screen-lit desk looking down), which is what
    /// --away-after's grace and the keyboard are for.
    static func sight(_ frame: CVPixelBuffer) -> Sighting {
        let request = VNDetectFaceRectanglesRequest()
        try? VNImageRequestHandler(cvPixelBuffer: frame, orientation: .up, options: [:]).perform([request])
        let faces = request.results ?? []
        return Sighting(people: faces.count,
                        confident: distinct(faces.filter { $0.confidence >= 0.6 }.map { $0.boundingBox }),
                        faces: faces.map { $0.boundingBox },
                        frameSize: CGSize(width: CVPixelBufferGetWidth(frame), height: CVPixelBufferGetHeight(frame)))
    }

    /// How many of these boxes are separate people: a box overlapping one
    /// already counted (intersection over union past 0.2) is the same one.
    static func distinct(_ boxes: [CGRect]) -> Int {
        var kept: [CGRect] = []
        for box in boxes {
            let overlaps = kept.contains { other in
                let shared = box.intersection(other)
                guard !shared.isNull else { return false }
                let inter = shared.width * shared.height
                let union = box.width * box.height + other.width * other.height - inter
                return union > 0 && inter / union > 0.2
            }
            if !overlaps { kept.append(box) }
        }
        return kept.count
    }

    /// Mean brightness of a frame, 0-255, from a sparse sample of pixels.
    static func brightness(_ frame: CVPixelBuffer) -> Double {
        CVPixelBufferLockBaseAddress(frame, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(frame, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(frame) else { return 0 }
        let width = CVPixelBufferGetWidth(frame), height = CVPixelBufferGetHeight(frame)
        let row = CVPixelBufferGetBytesPerRow(frame)
        let bytes = base.assumingMemoryBound(to: UInt8.self)
        var total = 0.0, count = 0.0
        for y in stride(from: 0, to: height, by: 16) {
            for x in stride(from: 0, to: width, by: 16) {
                let p = bytes + y * row + x * 4  // BGRA
                total += 0.114 * Double(p[0]) + 0.587 * Double(p[1]) + 0.299 * Double(p[2])
                count += 1
            }
        }
        return count > 0 ? total / count : 0
    }

    /// Too dark to see anyone: a covered camera, a dark room, a cold sensor
    /// still settling. Such a frame says nothing, and is never "nobody".
    static let darkFrame = 12.0

    /// One short look: the camera on for about two seconds, then off. Dark
    /// frames are skipped -- the first out of a cold camera are dark, and a
    /// dark frame has nobody in it. nil when no frame could be seen at all:
    /// unknown, which holds nothing.
    func glance(seconds: Double = 2.2) -> Sighting? {
        let wasRunning = running
        guard start() else { return nil }
        let deadline = Date().addingTimeInterval(seconds)
        Thread.sleep(forTimeInterval: wasRunning ? 0 : 0.4)
        var best = Sighting(people: 0, confident: 0)
        var looked = 0
        while Date() < deadline {
            if let frame = latestFrame(), Camera.brightness(frame) >= Camera.darkFrame {
                var s = Camera.sight(frame)
                if s.people < best.people { s = best }  // keep the frame that saw the most
                s.people = max(best.people, s.people)
                s.confident = max(best.confident, s.confident)
                best = s
                looked += 1
                if best.people > 0 && looked >= 2 { break }
            }
            Thread.sleep(forTimeInterval: 0.2)
        }
        if !wasRunning { stop() }
        return looked > 0 ? best : nil
    }
}

// MARK: - The phone switch

/// Run a command, its output as text, or nil if it failed or overran.
func capture(_ path: String, _ args: [String], timeout: Double = 4) -> String? {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    guard (try? p.run()) != nil else { return nil }
    var data = Data()
    let done = DispatchSemaphore(value: 0)
    DispatchQueue.global().async {
        data = pipe.fileHandleForReading.readDataToEndOfFile()  // drains as it goes: dumpsys is long
        done.signal()
    }
    if done.wait(timeout: .now() + timeout) == .timedOut {
        p.terminate()
        return nil
    }
    p.waitUntilExit()
    return p.terminationStatus == 0 ? String(data: data, encoding: .utf8) : nil
}

final class PhoneSwitch {
    let want: String   // "auto": the one real (non-emulator) device; else a serial prefix
    let adb: String
    private(set) var position = "unknown"   // up | down | unknown
    private(set) var reachable = false
    private(set) var serial: String?
    private var serialAt = 0.0
    var readAt = 0.0

    init(want: String, adb: String) {
        self.want = want
        self.adb = adb
    }

    /// The camera may be used only while the phone is known to be face up.
    var allowsCamera: Bool { reachable && position == "up" }

    func resolveSerial() -> String? {
        guard let out = capture(adb, ["devices"]) else { return nil }
        let serials = out.split(separator: "\n").compactMap { line -> String? in
            let parts = line.split(separator: "\t")
            guard parts.count == 2, parts[1] == "device" else { return nil }
            let serial = String(parts[0])
            if serial.hasPrefix("emulator-") { return nil }
            return want == "auto" || serial.hasPrefix(want) ? serial : nil
        }
        // A wireless device is listed twice, by address and by its mDNS name;
        // the name survives Wireless debugging picking a new port.
        return serials.first { $0.contains("_adb-tls-connect") } ?? serials.first
    }

    /// The newest reading of the phone's accelerometer, from dumpsys: Android
    /// keeps it sampled for its own face-down and rotation detection.
    static func newestGravity(_ dump: String) -> Double? {
        var inBlock = false
        var last: Substring?
        for line in dump.split(separator: "\n", omittingEmptySubsequences: false) {
            if line.contains("Accelerometer") && line.contains(": last ") && line.hasSuffix("events") {
                let lower = line.lowercased()
                inBlock = !(lower.contains("uncalibrated") || lower.contains("linear")
                            || lower.contains("limited") || lower.contains("bias"))
                continue
            }
            if inBlock {
                if line.hasPrefix("\t") { last = line } else { inBlock = false }
            }
        }
        guard let event = last, let close = event.lastIndex(of: ")") else { return nil }
        let values = event[event.index(after: close)...].split(separator: ",")
            .compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
        return values.count >= 3 ? values[2] : nil
    }

    /// Read the phone. Face up past +7 m/s², face down past -7; anything in
    /// between -- tilted, in a hand -- keeps the last position, so picking the
    /// phone up to read it flips nothing. Returns true when the position changed.
    @discardableResult
    func read(_ t: Double) -> Bool {
        readAt = t
        if serial == nil || t - serialAt > 60 {
            serial = resolveSerial()
            serialAt = t
        }
        let before = allowsCamera ? "up" : "down"
        guard let s = serial,
              let dump = capture(adb, ["-s", s, "shell", "dumpsys", "sensorservice"]),
              let z = PhoneSwitch.newestGravity(dump) else {
            reachable = false
            serial = nil
            return before != "down"
        }
        reachable = true
        if z >= 7 { position = "up" } else if z <= -7 { position = "down" }
        return before != (allowsCamera ? "up" : "down")
    }

    var report: String { reachable ? position : "unreachable" }
}

// MARK: - Waves

/// A raised, open hand swinging side to side, with a human face in frame.
/// Hand pose runs on its own queue at `rate`; a wave is `swings` reversals
/// of at least `reach` (a share of the frame's width) inside `window`
/// seconds. A reach for a mug is one swing and typing hands are not
/// raised, so neither is a wave; nor is anything with no face to go with it.
final class WaveDetector {
    static let rate = 15.0
    static let window = 2.5
    static let reach = 0.025
    static let swings = 3
    static let cooldown = 3.0
    /// Farther than this between two frames is another hand, not this one
    /// moving: a brisk wave covers under 0.1 of the frame per frame.
    static let maxStep = 0.15

    private let queue = DispatchQueue(label: "hobson.presence.hands")
    private var timer: DispatchSourceTimer?
    private var tracks: [[(t: Double, x: Double)]] = []
    private var lastWave = 0.0
    private let lock = NSLock()
    private var _hand: [CGPoint] = []
    private var _wavedAt = 0.0

    /// The raised hands' fingertips and wrists, normalized, for the preview.
    var hand: [CGPoint] { lock.lock(); defer { lock.unlock() }; return _hand }
    var wavedAt: Double { lock.lock(); defer { lock.unlock() }; return _wavedAt }

    func start(frames: @escaping () -> CVPixelBuffer?, onWave: @escaping () -> Void) {
        guard timer == nil else { return }
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now(), repeating: 1 / WaveDetector.rate)
        t.setEventHandler { [weak self] in
            guard let self = self, let frame = frames() else { return }
            if self.see(frame, at: now()) { onWave() }
        }
        t.resume()
        timer = t
    }

    func stop() {
        timer?.cancel()
        timer = nil
        queue.async { self.tracks = [] }
        lock.lock(); _hand = []; lock.unlock()
    }

    /// Each raised, open hand in this frame. Raised and open: at least three
    /// fingertips above the wrist (Vision's y points up). Its x is the centre
    /// of every joint seen: the fingertips alone come and go frame to frame,
    /// and moved a still hand's x by as much as a small wave.
    static func raisedHands(_ frame: CVPixelBuffer) -> [(x: Double, points: [CGPoint])] {
        let request = VNDetectHumanHandPoseRequest()
        request.maximumHandCount = 2
        try? VNImageRequestHandler(cvPixelBuffer: frame, orientation: .up, options: [:]).perform([request])
        let tips: [VNHumanHandPoseObservation.JointName] = [.indexTip, .middleTip, .ringTip, .littleTip]
        return (request.results ?? []).compactMap { hand in
            guard let joints = try? hand.recognizedPoints(.all),
                  let wrist = joints[.wrist], wrist.confidence > 0.3 else { return nil }
            let up = tips.compactMap { joints[$0] }.filter { $0.confidence > 0.3 && $0.location.y > wrist.location.y + 0.02 }
            guard up.count >= 3 else { return nil }
            let seen = joints.values.filter { $0.confidence > 0.3 }
            let x = seen.map { Double($0.location.x) }.reduce(0, +) / Double(seen.count)
            return (x, [wrist.location] + up.map { $0.location })
        }
    }

    /// Adds this frame's hands to the tracks, one track per hand: each x
    /// joins the track it is nearest, within `maxStep`, or starts its own.
    /// Two still hands must never read as one hand swinging between them. Pure.
    static func follow(_ tracks: [[(t: Double, x: Double)]], _ xs: [Double], at t: Double) -> [[(t: Double, x: Double)]] {
        var tracks = tracks.map { $0.filter { t - $0.t <= window } }.filter { !$0.isEmpty }
        var taken = Set<Int>()
        for x in xs {
            let near = tracks.indices.filter { !taken.contains($0) && abs(tracks[$0].last!.x - x) <= maxStep }
            if let i = near.min(by: { abs(tracks[$0].last!.x - x) < abs(tracks[$1].last!.x - x) }) {
                tracks[i].append((t, x))
                taken.insert(i)
            } else {
                tracks.append([(t, x)])
                taken.insert(tracks.count - 1)
            }
        }
        return tracks
    }

    /// Side-to-side reversals of at least `reach` in `xs`. Pure.
    static func swingCount(_ xs: [Double], reach: Double) -> Int {
        guard var anchor = xs.first else { return 0 }
        var low = anchor, high = anchor
        var direction = 0, swings = 0
        for x in xs.dropFirst() {
            switch direction {
            case 0:  // the first swing is measured from the extremes so far, not the first sample
                low = min(low, x)
                high = max(high, x)
                if x - low >= reach { direction = 1; swings = 1; anchor = x } else if high - x >= reach { direction = -1; swings = 1; anchor = x }
            case 1:
                if x > anchor { anchor = x } else if anchor - x >= reach { direction = -1; swings += 1; anchor = x }
            default:
                if x < anchor { anchor = x } else if x - anchor >= reach { direction = 1; swings += 1; anchor = x }
            }
        }
        return swings
    }

    private func see(_ frame: CVPixelBuffer, at t: Double) -> Bool {
        let hands = WaveDetector.raisedHands(frame)
        lock.lock(); _hand = hands.flatMap { $0.points }; lock.unlock()
        tracks = WaveDetector.follow(tracks, hands.map { $0.x }, at: t)
        guard t - lastWave > WaveDetector.cooldown,
              tracks.contains(where: { $0.last!.t == t  // a hand in this frame, not one that has gone
                  && WaveDetector.swingCount($0.map { $0.x }, reach: WaveDetector.reach) >= WaveDetector.swings }),
              Camera.sight(frame).people > 0  // a human face in this very frame, or it is not a wave
        else { return false }
        lastWave = t
        tracks = []
        lock.lock(); _wavedAt = t; lock.unlock()
        return true
    }
}

// MARK: - The debug window (--preview)

struct DebugSnapshot {
    let state: String, source: String
    let since: Double, idle: Double
    let mode: String, camera: String
    let cameraOn: Bool, cameraAllowed: Bool
    let phone: String?, callApp: String?
    let locked: Bool
    let sighting: Sighting?
    let sightingAge: Double
    let last: String
    let hand: [CGPoint]
    let wavedAgo: Double?
}

/// A small floating panel: the camera feed (mirrored, as a mirror is), a box
/// around each face, the fingertips of a raised hand, and what the sensor
/// makes of it all. It only shows what the sensor already has; it
/// saves nothing. Closing it hides it until the sensor restarts.
final class DebugWindow {
    let panel: NSPanel
    let preview = AVCaptureVideoPreviewLayer()
    let faceBoxes = CAShapeLayer()
    let handDots = CAShapeLayer()
    let frameBorder = CALayer()
    let placeholder = CATextLayer()
    let text = CATextLayer()
    let video: CGRect

    static let colors: [String: NSColor] = [
        "present": .systemGreen, "away": .systemYellow, "company": .systemTeal, "call": .systemPink,
    ]

    init() {
        let width: CGFloat = 360, height: CGFloat = 270, strip: CGFloat = 96
        video = CGRect(x: 0, y: strip, width: width, height: height)
        let frame = NSRect(x: 0, y: 0, width: width, height: height + strip)
        panel = NSPanel(contentRect: frame,
                        styleMask: [.titled, .closable, .utilityWindow, .hudWindow, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        panel.title = "Hobson Presence"
        panel.level = .floating
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = true
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let view = NSView(frame: frame)
        view.wantsLayer = true
        let root = view.layer!
        root.backgroundColor = NSColor.black.cgColor

        preview.frame = video
        preview.videoGravity = .resizeAspect
        root.addSublayer(preview)

        faceBoxes.frame = video
        faceBoxes.strokeColor = NSColor.systemOrange.cgColor
        faceBoxes.fillColor = nil
        faceBoxes.lineWidth = 2
        root.addSublayer(faceBoxes)
        handDots.frame = video
        handDots.fillColor = NSColor.systemYellow.cgColor
        handDots.strokeColor = nil
        root.addSublayer(handDots)

        frameBorder.frame = video
        frameBorder.borderWidth = 3
        root.addSublayer(frameBorder)

        placeholder.frame = CGRect(x: 0, y: video.midY - 20, width: width, height: 40)
        placeholder.alignmentMode = .center
        placeholder.fontSize = 13
        placeholder.foregroundColor = NSColor.secondaryLabelColor.cgColor
        placeholder.contentsScale = 2
        root.addSublayer(placeholder)

        text.frame = CGRect(x: 10, y: 6, width: width - 20, height: strip - 10)
        text.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        text.fontSize = 11
        text.foregroundColor = NSColor.white.cgColor
        text.isWrapped = true
        text.contentsScale = 2
        root.addSublayer(text)

        panel.contentView = view
        if let screen = NSScreen.main?.visibleFrame {
            panel.setFrameTopLeftPoint(NSPoint(x: screen.maxX - frame.width - 20, y: screen.maxY - 20))
        }
        panel.orderFrontRegardless()
    }

    func attach(_ session: AVCaptureSession?) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        preview.session = session
        if let connection = preview.connection, connection.isVideoMirroringSupported {
            connection.automaticallyAdjustsVideoMirroring = false
            connection.isVideoMirrored = true
        }
        CATransaction.commit()
    }

    /// Where the picture actually is inside the video area: the feed is
    /// fitted, not stretched (the built-in camera delivers 16:9 frames into
    /// a 4:3 panel, whatever preset was asked for).
    private func picture(_ frameSize: CGSize) -> CGRect {
        let size = frameSize.width > 0 && frameSize.height > 0 ? frameSize : CGSize(width: 16, height: 9)
        let scale = min(video.width / size.width, video.height / size.height)
        let w = size.width * scale, h = size.height * scale
        return CGRect(x: (video.width - w) / 2, y: (video.height - h) / 2, width: w, height: h)
    }

    /// Vision's boxes are normalized to the frame, origin bottom-left,
    /// unmirrored; the feed is shown mirrored.
    private func path(_ rects: [CGRect], in pic: CGRect) -> CGPath {
        let path = CGMutablePath()
        for r in rects {
            path.addRect(CGRect(x: pic.minX + (1 - r.maxX) * pic.width, y: pic.minY + r.minY * pic.height,
                                width: r.width * pic.width, height: r.height * pic.height))
        }
        return path
    }

    func update(_ s: DebugSnapshot) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        let color = DebugWindow.colors[s.state] ?? .systemGray
        frameBorder.borderColor = color.cgColor

        let fresh = s.sighting != nil && s.cameraOn && s.sightingAge < 3
        let pic = picture(s.sighting?.frameSize ?? .zero)
        faceBoxes.path = fresh ? path(s.sighting!.faces, in: pic) : nil
        let dots = CGMutablePath()
        if s.cameraOn {
            for p in s.hand {
                dots.addEllipse(in: CGRect(x: pic.minX + (1 - p.x) * pic.width - 4,
                                           y: pic.minY + p.y * pic.height - 4, width: 8, height: 8))
            }
        }
        handDots.path = dots
        let waving = (s.wavedAgo ?? 99) < 2
        frameBorder.borderWidth = waving ? 8 : 3

        if s.cameraOn {
            placeholder.string = ""
        } else if !s.cameraAllowed && s.phone != nil {
            placeholder.string = "camera off: phone \(s.phone!)"
        } else if s.camera != "authorized" {
            placeholder.string = "camera \(s.camera)"
        } else if s.mode == "auto" {
            placeholder.string = "camera off: auto looks only before speaking"
        } else if s.mode == "signals" {
            placeholder.string = "camera off: signals mode"
        } else {
            placeholder.string = s.locked ? "camera off: screen locked" : "camera off"
        }

        let seen = s.sighting.map { "faces \($0.people)  (only faces count)" } ?? "faces -"
        text.string = [
            "\(s.state.uppercased())  (\(s.source))  for \(Int(s.since))s",
            seen,
            "idle \(Int(s.idle))s  mode \(s.mode)  phone \(s.phone ?? "off")  call \(s.callApp ?? "no")",
            "last  \(s.last)" + (s.wavedAgo.map { $0 < 60 ? "   wave \(Int($0))s ago" : "" } ?? ""),
        ].joined(separator: "\n")
        CATransaction.commit()
    }
}

// MARK: - Files

func writeJSON(_ object: [String: Any], to path: String) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return }
    let tmp = "\(path).\(getpid()).tmp"
    guard FileManager.default.createFile(atPath: tmp, contents: data) else { return }
    if rename(tmp, path) != 0 { unlink(tmp) }
}

func emit(_ object: [String: Any], _ o: Options) {
    if let out = o.out {
        writeJSON(object, to: out)
    } else if let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
              let text = String(data: data, encoding: .utf8) {
        print(text)
    }
}

// MARK: - The sensor

final class Monitor {
    let o: Options
    let camera = Camera()
    let work = DispatchQueue(label: "hobson.presence.work")
    var timer: DispatchSourceTimer?
    var signalSources: [DispatchSourceSignal] = []

    var state = "present"
    var source = "start"
    var since = now()
    var candidate: (state: String, source: String, ticks: Int)?
    var glanceVerdict: (state: String, at: Double)?
    var glanceAt = 0.0
    var lookRequested = false
    var lastSeen = now()  // starting up is not evidence that you left
    var people: Int?
    var companyHits: [Bool] = []
    var cameraStatus = Camera.status()
    var ticks = 0
    var lastSignals: [String: Any] = [:]
    let phone: PhoneSwitch?
    var phoneKnown = false
    var debug: DebugWindow?
    let waves = WaveDetector()
    var lastSighting: Sighting?
    var lastTransition = "none yet"

    /// Two people in this many frames running (one a second) is company: it
    /// changes what is said, so it must not be a detector's double take.
    static let companyFrames = 5

    init(_ o: Options) {
        self.o = o
        phone = o.phone.map { PhoneSwitch(want: $0, adb: o.adb) }
    }

    var statePath: String { "\(o.home)/hobson-presence.json" }

    func run() {
        signal(SIGUSR1, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        let look = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: work)
        look.setEventHandler { [weak self] in
            self?.lookRequested = true
            self?.tick()
        }
        let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: work)
        term.setEventHandler { [weak self] in self?.shutdown("terminated") }
        look.resume()
        term.resume()
        signalSources = [look, term]

        let t = DispatchSource.makeTimerSource(queue: work)
        t.schedule(deadline: .now(), repeating: o.interval)
        t.setEventHandler { [weak self] in self?.tick() }
        t.resume()
        timer = t
    }

    func tick() {
        var t = now()
        ticks += 1
        if ticks % 15 == 1 { cameraStatus = Camera.status() }
        let idle = Signals.idleSeconds()
        let (locked, onConsole) = Signals.session()
        let asleep = Signals.displayAsleep()
        let callApp = Signals.callInProgress()
        let microphone = callApp != nil
        lastSignals = ["idle": (idle * 10).rounded() / 10, "locked": locked,
                       "display_asleep": asleep, "call_app": callApp ?? NSNull()]
        let screenUp = !locked && onConsole && !asleep
        if let phone = phone, o.mode != "signals",
           lookRequested || t - phone.readAt >= (o.mode == "continuous" ? 2 : 3) {
            let flipped = phone.read(t)
            if flipped && phoneKnown { announceSwitch(phone.allowsCamera ? "up" : "down") }
            phoneKnown = true
        }
        let cameraOK = cameraStatus == "authorized" && o.mode != "signals"
            && (phone?.allowsCamera ?? true)

        if o.mode == "continuous" && cameraOK && screenUp {
            sample(t)
            if camera.running {
                waves.start(frames: { [weak self] in self?.camera.latestFrame() },
                            onWave: { [weak self] in self?.work.async { self?.runScript(["--wave"]) } })
            }
        } else if camera.running {
            waves.stop()
            camera.stop()
            companyHits = []
        }
        if lookRequested {
            lookRequested = false
            if o.mode == "auto" && cameraOK && screenUp && !microphone {
                look()
                t = now()
            }
        }

        let (s, src) = decide(t, idle: idle, locked: locked || !onConsole, asleep: asleep,
                              microphone: microphone, cameraOK: cameraOK)
        settle(s, src, t)
        writeState(t, idle: idle)
        if let window = debug {
            let snapshot = DebugSnapshot(
                state: state, source: source, since: t - since, idle: idle, mode: o.mode,
                camera: cameraStatus, cameraOn: camera.running, cameraAllowed: cameraOK,
                phone: phone?.report, callApp: callApp, locked: locked || !onConsole,
                sighting: lastSighting, sightingAge: t - glanceAt, last: lastTransition,
                hand: waves.hand, wavedAgo: waves.wavedAt > 0 ? t - waves.wavedAt : nil)
            DispatchQueue.main.async { window.update(snapshot) }
        }
        if ticks % 60 == 0 { exitIfUnneeded(t) }
    }

    func sample(_ t: Double) {
        if !camera.running {
            _ = camera.start()
            return
        }
        guard let frame = camera.latestFrame(), Camera.brightness(frame) >= Camera.darkFrame else {
            lastSeen = t  // cannot see: not evidence that you left
            return
        }
        let s = Camera.sight(frame)
        lastSighting = s
        if s.people > 0 { lastSeen = t }
        people = s.people
        glanceAt = t
        companyHits.append(s.confident >= 2)
        if companyHits.count > Monitor.companyFrames { companyHits.removeFirst() }
    }

    /// An empty look is taken again before it counts: "away" holds what
    /// Hobson says while you may be sitting right there, the costly mistake.
    func look() {
        guard var s = camera.glance() else { return }
        if s.people == 0, let again = camera.glance(seconds: 1.6) { s = again }
        lastSighting = s
        let t = now()
        people = s.people
        glanceAt = t
        glanceVerdict = (s.confident >= 2 ? "company" : s.people > 0 ? "present" : "away", t)
    }

    func decide(_ t: Double, idle: Double, locked: Bool, asleep: Bool, microphone: Bool,
                cameraOK: Bool) -> (String, String) {
        if locked { return ("away", "lock") }
        if asleep { return ("away", "display") }
        if microphone { return ("call", "microphone") }
        if o.mode == "continuous" && cameraOK {
            if companyHits.count == Monitor.companyFrames && !companyHits.contains(false) {
                return ("company", "camera")
            }
            if t - lastSeen <= o.awayAfter { return ("present", "camera") }
            // Typing in the dark: the camera misses you, the keyboard does not.
            if idle < o.awayAfter { return ("present", "input") }
            return ("away", "camera")
        }
        if idle < o.idleSeconds { return ("present", "input") }
        // A look stands until the next keystroke: nothing since says otherwise.
        if let v = glanceVerdict, v.at >= t - idle { return (v.state, "camera") }
        return ("present", "idle")
    }

    /// Change state only once the new one has held for two ticks, except for
    /// the unambiguous ones: a keystroke, a lock, a look just taken.
    func settle(_ s: String, _ src: String, _ t: Double) {
        if s == state {
            candidate = nil
            source = src
            return
        }
        let instant = src == "input" || src == "lock" || (src == "camera" && t - glanceAt < 1.5)
        var seen = 1
        if let c = candidate, c.state == s { seen = c.ticks + 1 }
        candidate = (s, src, seen)
        if !instant && seen < 2 { return }
        let previous = state
        // Seen leaving by the camera, you left when last seen, not when the
        // sensor gave up on you --away-after seconds later: the absence a
        // greeting is measured by starts there.
        let begun = s == "away" && src == "camera" && o.mode == "continuous" ? min(t, lastSeen) : t
        let lasted = begun - since
        state = s
        source = src
        since = begun
        candidate = nil
        transition(from: previous, to: s, source: src, lasted: lasted)
    }

    /// `lasted` is how long the state being left had held: a return after
    /// ten seconds is not a return worth a briefing about waits.
    func transition(from: String, to: String, source: String, lasted: Double) {
        let clock = DateFormatter()
        clock.dateFormat = "HH:mm:ss"
        lastTransition = "\(from) → \(to) (\(source)) at \(clock.string(from: Date()))"
        runScript(["--transition", from, to, "--source", source, "--away-for", String(Int(lasted))])
    }

    /// The phone was turned over: presence.py says so out loud.
    func announceSwitch(_ position: String) {
        runScript(["--switch", position])
    }

    func runScript(_ args: [String]) {
        guard let python = o.python, let script = o.script else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [script] + args
        var env = ProcessInfo.processInfo.environment
        env["HOME"] = (o.home as NSString).deletingLastPathComponent
        p.environment = env
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { _ in }
        try? p.run()
    }

    func writeState(_ t: Double, idle: Double) {
        var record: [String: Any] = [
            "state": state, "source": source, "since": since, "ts": t,
            "mode": o.mode, "camera": cameraStatus, "pid": Int(getpid()),
            "glance_ts": glanceAt, "idle_seconds": o.idleSeconds, "version": 1,
        ]
        record["people"] = people ?? NSNull()
        record["phone"] = phone?.report ?? NSNull()
        record["phone_setting"] = o.phone ?? NSNull()
        record["preview"] = o.preview
        record["camera_allowed"] = cameraStatus == "authorized" && o.mode != "signals"
            && (phone?.allowsCamera ?? true)
        for (k, v) in lastSignals { record[k] = v }
        writeJSON(record, to: statePath)
    }

    /// Stay while anything could still need a return briefing; leave once no
    /// hook has run for --exit-after seconds and nothing is held.
    func exitIfUnneeded(_ t: Double) {
        guard state == "present" else { return }
        let fm = FileManager.default
        let held = "\(o.home)/hobson-held.json"
        if let data = fm.contents(atPath: held),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let items = obj["items"] as? [Any], !items.isEmpty {
            return
        }
        var newest = 0.0
        let names = (try? fm.contentsOfDirectory(atPath: o.home)) ?? []
        let activity = ["hobson-sessions", "hobson-presence.spawn"]
        for name in names where name.hasPrefix("hobson-alive-") || activity.contains(name) {
            if let attrs = try? fm.attributesOfItem(atPath: "\(o.home)/\(name)"),
               let m = attrs[.modificationDate] as? Date {
                newest = max(newest, m.timeIntervalSince1970)
            }
        }
        if t - newest > o.exitAfter { shutdown("no hook activity for \(Int(o.exitAfter))s") }
    }

    func shutdown(_ why: String) {
        camera.stop()
        unlink(statePath)
        exit(0)
    }
}

// MARK: - Main

func singleInstance(_ o: Options) -> Bool {
    let path = "\(o.home)/hobson-presence.lock"
    let fd = open(path, O_CREAT | O_RDWR, 0o644)
    guard fd >= 0, flock(fd, LOCK_EX | LOCK_NB) == 0 else { return false }
    ftruncate(fd, 0)
    let pid = "\(getpid())\n"
    _ = pid.withCString { write(fd, $0, strlen($0)) }
    return true  // the descriptor stays open, and locked, for the life of the process
}

func requestCameraAccess() -> Bool {
    if AVCaptureDevice.authorizationStatus(for: .video) != .notDetermined {
        return AVCaptureDevice.authorizationStatus(for: .video) == .authorized
    }
    let done = DispatchSemaphore(value: 0)
    var granted = false
    AVCaptureDevice.requestAccess(for: .video) { ok in
        granted = ok
        done.signal()
    }
    done.wait()
    return granted
}

let options = parseOptions()
try? FileManager.default.createDirectory(atPath: options.home, withIntermediateDirectories: true)

switch options.command {
case "signals":
    var record = Signals.snapshot()
    record["camera"] = Camera.status()
    record["device"] = Camera.pickDevice()?.localizedName ?? NSNull()
    emit(record, options)
    exit(0)
case "phone":
    let phone = PhoneSwitch(want: options.phone ?? "auto", adb: options.adb)
    phone.read(now())
    emit(["phone": phone.report, "serial": phone.serial ?? NSNull(),
          "camera_allowed": phone.allowsCamera], options)
    exit(0)
case "look":
    if options.requestPermission { _ = requestCameraAccess() }
    let camera = Camera()
    let sighting = camera.glance()
    var record: [String: Any] = ["camera": Camera.status(),
                                 "device": camera.deviceName ?? Camera.pickDevice()?.localizedName ?? NSNull()]
    record["people"] = sighting.map { $0.people } ?? NSNull()
    record["confident"] = sighting.map { $0.confident } ?? NSNull()
    emit(record, options)
    exit(0)
default:
    guard singleInstance(options) else { exit(0) }  // one sensor is running already
    if options.requestPermission { _ = requestCameraAccess() }
    let monitor = Monitor(options)
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    if options.preview {
        let window = DebugWindow()
        monitor.debug = window
        monitor.camera.onSession = { session in DispatchQueue.main.async { window.attach(session) } }
        // Launched with `open -j`, the app starts hidden, and so would the
        // window. Shown without taking focus from whatever you are doing.
        app.unhideWithoutActivation()
        window.panel.orderFrontRegardless()
    }
    monitor.run()
    app.run()
}
