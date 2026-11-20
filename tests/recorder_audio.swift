// Offline native test: exercise real sample-buffer extraction/resampling and
// common-clock alignment without opening ScreenCaptureKit or a microphone.
import Foundation
import AVFoundation

func sample(rate: Double, channels: AVAudioChannelCount, pts: Double) throws -> CMSampleBuffer {
    let count = Int(rate / 10)
    let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: channels, interleaved: true)!
    let values = (0..<(count * Int(channels))).map { Float(sin(Double($0 / Int(channels)) * 0.1) * 0.2) }
    var description: CMAudioFormatDescription?
    guard CMAudioFormatDescriptionCreate(allocator: nil, asbd: format.streamDescription,
        layoutSize: 0, layout: nil, magicCookieSize: 0, magicCookie: nil,
        extensions: nil, formatDescriptionOut: &description) == noErr else { throw RecorderError("format") }
    var block: CMBlockBuffer?
    let bytes = values.count * MemoryLayout<Float>.size
    guard CMBlockBufferCreateWithMemoryBlock(allocator: nil, memoryBlock: nil, blockLength: bytes,
        blockAllocator: nil, customBlockSource: nil, offsetToData: 0, dataLength: bytes,
        flags: 0, blockBufferOut: &block) == noErr else { throw RecorderError("block") }
    let status = values.withUnsafeBytes {
        CMBlockBufferReplaceDataBytes(with: $0.baseAddress!, blockBuffer: block!, offsetIntoDestination: 0, dataLength: bytes)
    }
    guard status == noErr else { throw RecorderError("copy") }
    var buffer: CMSampleBuffer?
    guard CMAudioSampleBufferCreateReadyWithPacketDescriptions(allocator: nil, dataBuffer: block!,
        formatDescription: description!, sampleCount: count,
        presentationTimeStamp: CMTime(seconds: pts, preferredTimescale: 1_000_000_000),
        packetDescriptions: nil, sampleBufferOut: &buffer) == noErr else { throw RecorderError("sample") }
    return buffer!
}

@main struct AudioTests {
    static func main() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let origin = CMTimeGetSeconds(CMClockGetTime(CMClockGetHostTimeClock()))
        let url = directory.appendingPathComponent("track.wav")
        let track = try Track(path: url.path, origin: origin)
        try track.append(sample(rate: 48000, channels: 1, pts: origin + 0.2))
        try track.append(sample(rate: 44100, channels: 2, pts: origin + 0.3))
        try track.close(at: origin + 0.5)
        let file = try AVAudioFile(forReading: url)
        guard abs(file.length - 24000) <= 2 else { throw RecorderError("Unexpected duration: \(file.length)") }
        let pcm = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length))!
        try file.read(into: pcm)
        let data = pcm.floatChannelData![0]
        guard (0..<9000).allSatisfy({ data[$0] == 0 }),
              (10000..<18000).contains(where: { abs(data[$0]) > 0.01 }),
              (20000..<Int(pcm.frameLength)).allSatisfy({ data[$0] == 0 }) else {
            throw RecorderError("Track timing or conversion lost signal")
        }
        do {
            _ = try Track(path: url.path, origin: origin)
            throw RecorderError("Overwrite was allowed")
        } catch let error as RecorderError where error.message.hasPrefix("Refusing") { }
        let clockTrack = try Track(path: directory.appendingPathComponent("clock.wav").path, origin: origin)
        try clockTrack.append(sample(rate: 48000, channels: 1, pts: 0.1),
                              at: CMTime(seconds: origin + 0.2, preferredTimescale: 1_000_000_000))
        guard abs(clockTrack.framesWritten - 14400) <= 2 else { throw RecorderError("Host-clock override length: \(clockTrack.framesWritten)") }
        try clockTrack.close(at: origin + 0.4)
        let longTrack = try Track(path: directory.appendingPathComponent("continuous.wav").path, origin: origin)
        for index in 0..<100 {
            try longTrack.append(sample(rate: 44100, channels: 2, pts: origin + Double(index) * 0.1))
        }
        guard abs(longTrack.framesWritten - 480000) < 200 else {
            throw RecorderError("Resampling accumulated drift: \(longTrack.framesWritten)")
        }
        try longTrack.close(at: origin + 10)
        print("Native audio tests passed: PCM conversion, format change, silence alignment, WAV finalization, overwrite protection")
    }
}
