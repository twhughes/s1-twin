// systap: record this Mac's own sound (every app, or one app) through a Core Audio process tap
// (macOS 14.2 and later), and write it as a mono 16-bit WAV at the device's rate. The Match view
// records Logic Pro with it (round 13, W-sys). synth/native/systap.py builds it (swiftc -O, into
// ~/.synth/bin/) and runs it; the contract lives there.
//
//   systap --out PATH [--app BUNDLE_ID] [--seconds N]
//       Record until SIGINT or SIGTERM, or N seconds (default 30), then write PATH. Prints
//       "recording <rate>" once the tap runs, and "wrote <frames> frames" at the end.
//   systap --list
//       One JSON line: {"permission": "granted" | "denied" | "unknown",
//                       "apps": [{"bundle", "pid", "output"}]}, the apps connected to Core Audio.
//
// Nothing is played: the tap only listens, the tapped apps still play as before (unmuted), and the
// private device that reads the tap holds nothing else, so no other device is opened. macOS asks once
// whether this helper may record system audio (its Info.plist, systap-Info.plist, is linked in and says
// why); see becomeResponsible below. Errors are one plain line on stderr, and the exit code says which:
// 2 usage, 3 macOS blocked system audio, 4 the app is not running, 5 macOS is older than 14.2,
// 1 anything else.

import CoreAudio
import Foundation

// ── words and exits ───────────────────────────────────────────────────────────────────────────────
enum Code: Int32 { case failed = 1, usage = 2, blocked = 3, noApp = 4, tooOld = 5 }

let blockedWords = "macOS blocked system audio. Allow it in System Settings > Privacy & Security > "
    + "Screen & System Audio Recording, then press Record again."
let usageWords = "usage: systap --out PATH [--app BUNDLE_ID] [--seconds N] | systap --list"

func say(_ line: String) {
    FileHandle.standardOutput.write(Data((line + "\n").utf8))   // unbuffered: the server waits for it
}

func fail(_ code: Code, _ words: String) -> Never {
    FileHandle.standardError.write(Data((words + "\n").utf8))
    exit(code.rawValue)
}

/// An OSStatus as its four characters when it has them ('!obj'), else as a number.
func statusText(_ status: OSStatus) -> String {
    let n = UInt32(bitPattern: status)
    let bytes = [24, 16, 8, 0].map { UInt8((n >> UInt32($0)) & 0xFF) }
    if bytes.allSatisfy({ $0 >= 32 && $0 < 127 }) { return "'" + String(decoding: bytes, as: UTF8.self) + "'" }
    return String(status)
}

func check(_ status: OSStatus, _ what: String) {
    if status != noErr { fail(.failed, "\(what) failed (Core Audio error \(statusText(status))).") }
}

// ── Core Audio properties ─────────────────────────────────────────────────────────────────────────
func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

/// A fixed-size property (a number, a format), or nil.
func read<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ initial: T) -> T? {
    var addr = address(selector)
    var value = initial
    var size = UInt32(MemoryLayout<T>.size)
    let status = withUnsafeMutablePointer(to: &value) { AudioObjectGetPropertyData(object, &addr, 0, nil, &size, $0) }
    return status == noErr ? value : nil
}

/// A CFString property (a bundle id), or nil. Core Audio hands it over retained.
func readString(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = address(selector)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let status = withUnsafeMutablePointer(to: &value) { AudioObjectGetPropertyData(object, &addr, 0, nil, &size, $0) }
    guard status == noErr, let string = value?.takeRetainedValue() else { return nil }
    return string as String
}

/// An array of object ids, or none.
func readObjects(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> [AudioObjectID] {
    var addr = address(selector)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(object, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    let stride = MemoryLayout<AudioObjectID>.stride
    var ids = [AudioObjectID](repeating: AudioObjectID(kAudioObjectUnknown), count: Int(size) / stride)
    guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return Array(ids.prefix(Int(size) / stride))
}

/// The apps connected to Core Audio (Logic Pro while it is open), with a bundle id.
struct App { let object: AudioObjectID; let bundle: String; let pid: pid_t; let output: Bool }

func apps() -> [App] {
    readObjects(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList).compactMap { id in
        guard let bundle = readString(id, kAudioProcessPropertyBundleID), !bundle.isEmpty else { return nil }
        let pid = read(id, kAudioProcessPropertyPID, pid_t(0)) ?? 0
        let output = (read(id, kAudioProcessPropertyIsRunningOutput, UInt32(0)) ?? 0) != 0
        return App(object: id, bundle: bundle, pid: pid, output: output)
    }
}

/// What macOS says about system audio recording for this helper (see becomeResponsible). Asking never
/// prompts; the first real recording does. There is no public call for it, so this is TCC's own
/// preflight, looked up at run time: "unknown" when it is not there, or when macOS has not asked yet.
func permission() -> String {
    guard let tcc = dlopen("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_NOW),
          let symbol = dlsym(tcc, "TCCAccessPreflight") else { return "unknown" }
    typealias Preflight = @convention(c) (CFString, CFDictionary?) -> Int32
    switch unsafeBitCast(symbol, to: Preflight.self)("kTCCServiceAudioCapture" as CFString, nil) {
    case 0: return "granted"
    case 1: return "denied"
    default: return "unknown"
    }
}

// ── the take ──────────────────────────────────────────────────────────────────────────────────────
/// The recorded frames, mono, as 16-bit samples. Filled on the IO queue, read after it has drained.
final class Take {
    let samples: UnsafeMutablePointer<Int16>
    let capacity: Int
    var count = 0

    init(capacity: Int) {
        self.capacity = max(1, capacity)
        samples = UnsafeMutablePointer<Int16>.allocate(capacity: self.capacity)
    }

    /// Mix one IO cycle's channels (every buffer is the tap's: the device holds nothing else) to mono.
    func append(_ list: UnsafePointer<AudioBufferList>) {
        let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: list))
        let channels = buffers.reduce(0) { $0 + Int($1.mNumberChannels) }
        guard channels > 0, let first = buffers.first, first.mNumberChannels > 0 else { return }
        let frames = Int(first.mDataByteSize) / (MemoryLayout<Float32>.size * Int(first.mNumberChannels))
        let n = min(frames, capacity - count)
        guard n > 0 else { return }
        for i in 0..<n {
            var sum: Float32 = 0
            for buffer in buffers {
                guard let data = buffer.mData?.assumingMemoryBound(to: Float32.self) else { continue }
                let k = Int(buffer.mNumberChannels)
                for c in 0..<k { sum += data[i * k + c] }
            }
            let x = max(-1, min(1, sum / Float32(channels)))
            samples[count + i] = Int16((x < 0 ? x * 32768 : x * 32767).rounded())
        }
        count += n
    }
}

/// A 16-bit mono PCM WAV (the same kind the browser writes for a microphone take, core/wav.js).
func writeWav(_ path: String, _ take: Take, rate: Int) {
    var data = Data(capacity: 44 + 2 * take.count)
    func text(_ s: String) { data.append(contentsOf: Array(s.utf8)) }
    func u32(_ v: Int) { withUnsafeBytes(of: UInt32(v).littleEndian) { data.append(contentsOf: $0) } }
    func u16(_ v: Int) { withUnsafeBytes(of: UInt16(v).littleEndian) { data.append(contentsOf: $0) } }
    text("RIFF"); u32(36 + 2 * take.count); text("WAVE")
    text("fmt "); u32(16); u16(1); u16(1); u32(rate); u32(rate * 2); u16(2); u16(16)
    text("data"); u32(2 * take.count)
    for i in 0..<take.count { withUnsafeBytes(of: take.samples[i].littleEndian) { data.append(contentsOf: $0) } }
    do {
        try data.write(to: URL(fileURLWithPath: path))
    } catch {
        fail(.failed, "Could not write the recording to \(path) (\(error.localizedDescription)).")
    }
}

// ── recording ─────────────────────────────────────────────────────────────────────────────────────
@available(macOS 14.2, *)
func record(to path: String, app bundle: String?, seconds: Double, stop: DispatchSemaphore) {
    if permission() == "denied" { fail(.blocked, blockedWords) }

    // The tap: every app's sound, or one app's, mixed to stereo. The apps still play as before.
    let description: CATapDescription
    if let bundle {
        let ids = apps().filter { $0.bundle == bundle }.map(\.object)
        if ids.isEmpty { fail(.noApp, "No running app has the bundle id \(bundle).") }
        description = CATapDescription(stereoMixdownOfProcesses: ids)
    } else {
        description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
    }
    description.uuid = UUID()
    description.name = "synth systap"
    description.isPrivate = true
    description.muteBehavior = .unmuted
    var tap = AudioObjectID(kAudioObjectUnknown)
    check(AudioHardwareCreateProcessTap(description, &tap), "Making the tap")

    // A private device that holds only the tap. No auto-start: the take begins now, even in silence.
    let composition: [String: Any] = [
        kAudioAggregateDeviceNameKey: "synth systap",
        kAudioAggregateDeviceUIDKey: "synth-systap-\(UUID().uuidString)",
        kAudioAggregateDeviceIsPrivateKey: 1,
        kAudioAggregateDeviceIsStackedKey: 0,
        kAudioAggregateDeviceTapAutoStartKey: 0,
        kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: description.uuid.uuidString,
                                           kAudioSubTapDriftCompensationKey: 1]],
    ]
    var device = AudioObjectID(kAudioObjectUnknown)
    check(AudioHardwareCreateAggregateDevice(composition as CFDictionary, &device), "Making the recording device")

    guard let format = read(tap, kAudioTapPropertyFormat, AudioStreamBasicDescription()) else {
        fail(.failed, "Could not read the tap's sound format.")
    }
    guard format.mFormatID == kAudioFormatLinearPCM, format.mFormatFlags & kAudioFormatFlagIsFloat != 0,
          format.mBitsPerChannel == 32 else {
        fail(.failed, "The tap's sound is in a format this helper does not read (\(format.mBitsPerChannel)-bit).")
    }
    let nominal = read(device, kAudioDevicePropertyNominalSampleRate, Float64(0)) ?? 0
    let rate = nominal > 0 ? nominal : format.mSampleRate
    guard rate > 0 else { fail(.failed, "The recording device has no sample rate.") }

    let take = Take(capacity: Int(seconds * rate))
    let queue = DispatchQueue(label: "systap.io", qos: .userInteractive)
    var proc: AudioDeviceIOProcID?
    check(AudioDeviceCreateIOProcIDWithBlock(&proc, device, queue) { _, input, _, _, _ in take.append(input) },
          "Opening the recording device")
    check(AudioDeviceStart(device, proc), "Starting the recording")
    say("recording \(Int(rate.rounded()))")

    _ = stop.wait(timeout: .now() + seconds)

    AudioDeviceStop(device, proc)
    if let proc { AudioDeviceDestroyIOProcID(device, proc) }
    queue.sync {}                                  // the last IO cycle has been read
    AudioHardwareDestroyAggregateDevice(device)
    AudioHardwareDestroyProcessTap(tap)
    writeWav(path, take, rate: Int(rate.rounded()))
    say("wrote \(take.count) frames")
    // Answered "Don't Allow" while it recorded: the take is silence, and the words say what to do.
    if permission() == "denied" { fail(.blocked, blockedWords) }
}

// ── who macOS asks ────────────────────────────────────────────────────────────────────────────────
// macOS asks about system audio for the app responsible for a process: for this helper, the app that
// started the cockpit (a terminal, Alfred). Those apps do not say why they would record system audio, so
// macOS refuses them without asking, and every take is silence. So the helper runs a copy of itself that
// is responsible for itself: macOS asks about this helper, with the words in its Info.plist, and keeps the
// answer for it. The first process passes SIGINT and SIGTERM on and exits as the copy does.
let control = DispatchQueue(label: "systap.control")   // the signals, and the copy's pid, live here
var copyPid: pid_t = 0
var stopAsked = false

/// Returns in the copy, or when no copy can start (then macOS asks the app that started the cockpit).
func becomeResponsible() {
    guard getenv("SYSTAP_RESPONSIBLE") == nil, let path = Bundle.main.executablePath,
          let program = dlopen(nil, RTLD_NOW),
          let symbol = dlsym(program, "responsibility_spawnattrs_setdisclaim") else { return }
    typealias Disclaim = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32
    var attributes: posix_spawnattr_t?
    guard posix_spawnattr_init(&attributes) == 0 else { return }
    defer { posix_spawnattr_destroy(&attributes) }
    guard unsafeBitCast(symbol, to: Disclaim.self)(&attributes, 1) == 0 else { return }
    setenv("SYSTAP_RESPONSIBLE", "1", 1)
    var child: pid_t = 0
    let spawned = control.sync { () -> Int32 in
        let status = posix_spawn(&child, path, nil, &attributes, CommandLine.unsafeArgv, environ)
        if status == 0 {
            copyPid = child
            if stopAsked { kill(child, SIGTERM) }
        }
        return status
    }
    guard spawned == 0 else { unsetenv("SYSTAP_RESPONSIBLE"); return }
    var status: Int32 = 0
    while waitpid(child, &status, 0) < 0 && errno == EINTR {}
    exit(status & 0x7f == 0 ? (status >> 8) & 0xff : Code.failed.rawValue)
}

// ── main ──────────────────────────────────────────────────────────────────────────────────────────
// SIGINT and SIGTERM end the take (never the process): they are caught from the start, so one that
// comes early still leaves a WAV behind.
let stop = DispatchSemaphore(value: 0)
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let signalSources = [SIGINT, SIGTERM].map { number -> DispatchSourceSignal in
    let source = DispatchSource.makeSignalSource(signal: number, queue: control)
    source.setEventHandler {
        stopAsked = true
        if copyPid > 0 { kill(copyPid, SIGTERM) }
        stop.signal()
    }
    source.resume()
    return source
}

let args = Array(CommandLine.arguments.dropFirst())
if args == ["--list"] {
    becomeResponsible()
    let rows: [[String: Any]] = apps().map { ["bundle": $0.bundle, "pid": Int($0.pid), "output": $0.output] }
    let json: [String: Any] = ["permission": permission(), "apps": rows]
    guard let data = try? JSONSerialization.data(withJSONObject: json, options: [.sortedKeys]) else {
        fail(.failed, "Could not list the apps.")
    }
    FileHandle.standardOutput.write(data)
    say("")
    exit(0)
}

var out: String?, app: String?, seconds = 30.0
var i = 0
while i < args.count {
    let flag = args[i], next = i + 1 < args.count ? args[i + 1] : nil
    switch (flag, next) {
    case ("--out", let v?): out = v
    case ("--app", let v?): app = v
    case ("--seconds", let v?):
        guard let s = Double(v), s > 0, s <= 600 else { fail(.usage, usageWords) }
        seconds = s
    default: fail(.usage, usageWords)
    }
    i += 2
}
guard let out else { fail(.usage, usageWords) }
becomeResponsible()
if #available(macOS 14.2, *) {
    record(to: out, app: app, seconds: seconds, stop: stop)
} else {
    fail(.tooOld, "Recording this Mac's sound needs macOS 14.2 or later.")
}
exit(0)
