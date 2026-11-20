import Foundation
import AVFoundation
import ScreenCaptureKit
import CoreGraphics
import CoreAudio
import Darwin

func report(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}
struct RecorderError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
    init(_ message: String) { self.message = message }
}

// Microphone timestamps are converted from the capture-session clock to the
// host clock used by ScreenCaptureKit. Silence is inserted for delayed
// starts/gaps so independent tracks retain their position on the meeting timeline.
// All writes, stop state, and callbacks are serialized on Recorder.queue.
final class Track {
    let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000,
                               channels: 1, interleaved: false)!
    var file: AVAudioFile?
    // AVAudioFile.framePosition can lag accepted PCM writes due to its internal
    // conversion buffer. Count accepted frames ourselves or silence padding
    // would repeatedly compensate for that lag and stretch the timeline.
    private(set) var framesWritten: AVAudioFramePosition = 0
    var converter: AVAudioConverter?
    var inputFormat: AVAudioFormat?
    let origin: Double
    init(path: String, origin: Double) throws {
        self.origin = origin
        guard !FileManager.default.fileExists(atPath: path) else {
            throw RecorderError("Refusing to overwrite: \(path)")
        }
        file = try AVAudioFile(forWriting: URL(fileURLWithPath: path), settings: [
            AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48000,
            AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false
        ], commonFormat: .pcmFormatFloat32, interleaved: false)
    }
    func pad(to frame: AVAudioFramePosition) throws {
        guard let file else { return }
        while framesWritten < frame {
            let count = AVAudioFrameCount(min(48000, frame - framesWritten))
            let silence = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: count)!
            silence.frameLength = count
            memset(silence.floatChannelData![0], 0, Int(count) * MemoryLayout<Float>.size)
            try file.write(from: silence)
            framesWritten += AVAudioFramePosition(count)
        }
    }
    func append(_ sample: CMSampleBuffer, at hostTime: CMTime? = nil) throws {
        guard CMSampleBufferDataIsReady(sample), CMSampleBufferGetNumSamples(sample) > 0,
              let description = CMSampleBufferGetFormatDescription(sample),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(description),
              let sourceFormat = AVAudioFormat(streamDescription: asbd) else {
            throw RecorderError("Invalid audio sample buffer")
        }
        let count = AVAudioFrameCount(CMSampleBufferGetNumSamples(sample))
        guard let source = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: count) else {
            throw RecorderError("Cannot allocate input audio buffer")
        }
        source.frameLength = count
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(sample, at: 0,
            frameCount: Int32(count), into: source.mutableAudioBufferList)
        guard status == noErr else { throw RecorderError("Cannot read PCM samples: \(status)") }
        if inputFormat != sourceFormat {
            inputFormat = sourceFormat
            converter = AVAudioConverter(from: sourceFormat, to: format)
        }
        guard let converter else { throw RecorderError("Cannot convert input audio format") }
        let capacity = AVAudioFrameCount(ceil(Double(count) * 48000 / sourceFormat.sampleRate) + 256)
        let pts = CMTimeGetSeconds(hostTime ?? CMSampleBufferGetPresentationTimeStamp(sample))
        guard pts.isFinite, abs(pts - CMTimeGetSeconds(CMClockGetTime(CMClockGetHostTimeClock()))) < 60 else {
            throw RecorderError("Audio clock is not on the host timeline; recording stopped")
        }
        let target = AVAudioFramePosition(max(0, (pts - origin) * 48000))
        // Tolerate sub-buffer scheduling/resampling jitter without accumulating drift.
        if target > framesWritten + 960 { try pad(to: target) }
        var cursor: AVAudioFrameCount = 0
        while true {
            let converted = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity)!
            var error: NSError?
            let result = converter.convert(to: converted, error: &error) { requested, state in
                guard cursor < count else { state.pointee = .noDataNow; return nil }
                // A converter may request only part of the input buffer. Giving
                // it more than requested can silently discard the remaining PCM.
                let length = min(requested, count - cursor)
                let chunk = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: length)!
                chunk.frameLength = length
                let inputBuffers = UnsafeMutableAudioBufferListPointer(source.mutableAudioBufferList)
                let outputBuffers = UnsafeMutableAudioBufferListPointer(chunk.mutableAudioBufferList)
                let stride = Int(sourceFormat.streamDescription.pointee.mBytesPerFrame)
                for index in 0..<inputBuffers.count {
                    memcpy(outputBuffers[index].mData!, inputBuffers[index].mData!.advanced(by: Int(cursor) * stride), Int(length) * stride)
                }
                cursor += length
                state.pointee = .haveData
                return chunk
            }
            if result == .error { throw error ?? RecorderError("Audio conversion failed") as NSError }
            if converted.frameLength > 0 {
                try file?.write(from: converted)
                framesWritten += AVAudioFramePosition(converted.frameLength)
            }
            if result == .inputRanDry || result == .endOfStream { break }
            if converted.frameLength == 0 { throw RecorderError("Audio converter made no progress") }
        }
    }
    func close(at seconds: Double) throws {
        defer { file = nil }
        try pad(to: AVAudioFramePosition(max(0, seconds - origin) * 48000))
    }
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate, AVCaptureAudioDataOutputSampleBufferDelegate, @unchecked Sendable {
    let queue = DispatchQueue(label: "meeting.recorder.audio")
    var stream: SCStream?
    var session: AVCaptureSession?
    var system: Track?
    var microphone: Track?
    var stopping = false
    var startupComplete = false
    var finished = false
    var failure: Error?
    var signals: [DispatchSourceSignal] = []
    var observers: [NSObjectProtocol] = []
    var outputListener: AudioObjectPropertyListenerBlock?
    var completion: CheckedContinuation<Int32, Never>?

    func receive(_ buffer: CMSampleBuffer, track: Track?, hostTime: CMTime? = nil) {
        guard !stopping else { return }
        do { try track?.append(buffer, at: hostTime) }
        catch { fail(error) }
    }
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        if type == .audio { receive(sampleBuffer, track: system) }
    }
    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let clock = session?.synchronizationClock else {
            fail(RecorderError("Microphone synchronization clock unavailable"))
            return
        }
        let hostTime = CMSyncConvertTime(CMSampleBufferGetPresentationTimeStamp(sampleBuffer),
            from: clock, to: CMClockGetHostTimeClock())
        receive(sampleBuffer, track: microphone, hostTime: hostTime)
    }
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        queue.async { self.fail(error) }
    }
    func fail(_ error: Error) {
        if failure == nil { failure = error; report("[!] Capture interrupted: \(error.localizedDescription)") }
        requestStop()
    }
    func requestStop() {
        guard !stopping else { return }
        stopping = true
        // Do not hang forever inside a framework's stop/start operation. Callbacks
        // already ignore new samples once stopping, so writers can be closed safely.
        queue.asyncAfter(deadline: .now() + 10) {
            guard !self.finished else { return }
            let end = CMTimeGetSeconds(CMClockGetTime(CMClockGetHostTimeClock()))
            for track in [self.system, self.microphone].compactMap({ $0 }) {
                try? track.close(at: end)
            }
            report("[!] Capture did not stop within 10 seconds. Raw WAVs/log were kept; transcription was not started.")
            exit(1)
        }
        if startupComplete { Task { await finish() } }
    }
    func finish() async {
        do { try await stream?.stopCapture() }
        catch { queue.sync { if failure == nil { failure = error } } }
        session?.stopRunning()
        let status: Int32 = queue.sync {
            let end = CMTimeGetSeconds(CMClockGetTime(CMClockGetHostTimeClock()))
            for track in [system, microphone].compactMap({ $0 }) {
                do { try track.close(at: end) }
                catch { if failure == nil { failure = error } }
            }
            if let failure { report("[!] \(failure.localizedDescription). Raw tracks were kept.") }
            finished = true
            return failure == nil ? 0 : 1
        }
        if let outputListener {
            var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
            AudioObjectRemovePropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &address, queue, outputListener)
        }
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
        for source in signals { source.cancel() }
        completion?.resume(returning: status)
        completion = nil
    }
    func run(source: String, output: String, micOutput: String?, micID: String?, metadataPath: String?) async throws -> Int32 {
        guard CGPreflightScreenCaptureAccess() else {
            _ = CGRequestScreenCaptureAccess()
            throw RecorderError("System audio permission required. Open System Settings → Privacy & Security → Screen & System Audio Recording, allow your terminal/Meeting Recorder, then quit and reopen the terminal and retry. No microphone fallback was used.")
        }
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else { throw RecorderError("No capture display available; system audio cannot start") }
        var micDevice: AVCaptureDevice?
        if source == "both" {
            guard await AVCaptureDevice.requestAccess(for: .audio) else {
                throw RecorderError("Microphone permission required. Open System Settings → Privacy & Security → Microphone, allow your terminal/Meeting Recorder, then retry.")
            }
            micDevice = micID.flatMap { AVCaptureDevice(uniqueID: $0) } ?? (micID == nil ? AVCaptureDevice.default(for: .audio) : nil)
            guard let micDevice else { throw RecorderError("Selected microphone is unavailable; no source was substituted") }
            report("[*] Microphone: \(micDevice.localizedName) [\(micDevice.uniqueID)]")
        }
        let origin = CMTimeGetSeconds(CMClockGetTime(CMClockGetHostTimeClock()))
        if let metadataPath {
            let formatter = ISO8601DateFormatter()
            formatter.timeZone = TimeZone(identifier: "Asia/Taipei")
            var metadata: [String: Any] = ["recorded_at": formatter.string(from: Date()),
                "host_clock_start_seconds": origin, "source": source]
            if let micDevice {
                metadata["microphone_device_id"] = micDevice.uniqueID
                metadata["microphone_device_name"] = micDevice.localizedName
            }
            try JSONSerialization.data(withJSONObject: metadata, options: [.prettyPrinted, .sortedKeys])
                .write(to: URL(fileURLWithPath: metadataPath), options: .withoutOverwriting)
        }
        system = try Track(path: output, origin: origin)
        if source == "both" {
            guard let micOutput, let micDevice else { throw RecorderError("both requires --mic-output") }
            microphone = try Track(path: micOutput, origin: origin)
            let capture = AVCaptureSession()
            let input = try AVCaptureDeviceInput(device: micDevice)
            let audio = AVCaptureAudioDataOutput()
            guard capture.canAddInput(input) else { throw RecorderError("Cannot attach selected microphone") }
            capture.addInput(input)
            guard capture.canAddOutput(audio) else { throw RecorderError("Cannot attach microphone output") }
            capture.addOutput(audio)
            audio.setSampleBufferDelegate(self, queue: queue)
            session = capture
            observers.append(NotificationCenter.default.addObserver(forName: .AVCaptureSessionRuntimeError, object: capture, queue: nil) { note in
                let error = note.userInfo?[AVCaptureSessionErrorKey] as? Error ?? RecorderError("Microphone runtime error")
                self.queue.async { self.fail(error) }
            })
            observers.append(NotificationCenter.default.addObserver(forName: .AVCaptureDeviceWasDisconnected, object: micDevice, queue: nil) { _ in
                self.queue.async { self.fail(RecorderError("Selected microphone disconnected; already captured audio will be kept")) }
            })
        }
        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = 48000
        configuration.channelCount = 1
        // ScreenCaptureKit requires a display filter; no screen output is attached or saved.
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let capture = SCStream(filter: filter, configuration: configuration, delegate: self)
        try capture.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        stream = capture
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        let listener: AudioObjectPropertyListenerBlock = { _, _ in
            report("[*] Audio output device changed; system capture continues. Check the meeting app's selected output if needed.")
        }
        if AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &address, queue, listener) == noErr {
            outputListener = listener
        } else { report("[!] Output-device change notifications are unavailable; verify audio after switching devices.") }
        return await withCheckedContinuation { continuation in
            completion = continuation
            for number in [SIGINT, SIGTERM] {
                signal(number, SIG_IGN)
                let watcher = DispatchSource.makeSignalSource(signal: number, queue: queue)
                watcher.setEventHandler { self.requestStop() }
                watcher.resume()
                signals.append(watcher)
            }
            Task {
                do {
                    session?.startRunning()
                    try await capture.startCapture()
                    queue.async {
                        self.startupComplete = true
                        if self.stopping { Task { await self.finish() } }
                        else { report("[*] Recording \(source); silence before the meeting is allowed. Ctrl+C to stop.") }
                    }
                } catch {
                    queue.async {
                        self.startupComplete = true
                        if self.stopping {
                            if self.failure == nil { self.failure = error }
                            Task { await self.finish() }
                        } else { self.fail(error) }
                    }
                }
            }
        }
    }
}

#if !RECORDER_TESTS
@main struct MeetingRecorder {
    static func main() async {
        do {
            var options: [String: String] = [:]
            let args = Array(CommandLine.arguments.dropFirst())
            if args == ["--help"] || args.isEmpty {
                print("meeting-recorder --source system|both --output SYSTEM.wav [--mic-output MIC.wav] [--mic-device-id ID] [--metadata INFO.json]\nmeeting-recorder --list-microphones")
                return
            }
            if args == ["--list-microphones"] {
                let devices = AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone, .external], mediaType: .audio, position: .unspecified).devices
                for device in devices { print("\(device.uniqueID)\t\(device.localizedName)") }
                return
            }
            guard args.count % 2 == 0 else { throw RecorderError("Options require values; use --help") }
            for i in stride(from: 0, to: args.count, by: 2) {
                guard ["--source", "--output", "--mic-output", "--mic-device-id", "--metadata"].contains(args[i]), options[args[i]] == nil else {
                    throw RecorderError("Unknown or duplicate option: \(args[i])")
                }
                options[args[i]] = args[i + 1]
            }
            guard let source = options["--source"], ["system", "both"].contains(source), let output = options["--output"] else {
                throw RecorderError("Required: --source system|both --output PATH")
            }
            if source == "both" && (options["--mic-output"] == nil || options["--mic-output"] == output) {
                throw RecorderError("both requires a separate --mic-output PATH")
            }
            let recorder = Recorder()
            let status = try await recorder.run(source: source, output: output, micOutput: options["--mic-output"], micID: options["--mic-device-id"], metadataPath: options["--metadata"])
            exit(status)
        } catch { report("[!] \(error.localizedDescription)"); exit(1) }
    }
}

#endif
