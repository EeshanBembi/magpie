import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

// MARK: - Audio writer

/// Writes one 16 kHz mono 16-bit PCM WAV file from whatever PCM buffers the
/// stream delivers (usually float32), converting formats when needed.
final class TrackWriter {
    let url: URL
    private let file: AVAudioFile
    private var converters: [String: AVAudioConverter] = [:]

    static let outputSettings: [String: Any] = [
        AVFormatIDKey: kAudioFormatLinearPCM,
        AVSampleRateKey: 16_000,
        AVNumberOfChannelsKey: 1,
        AVLinearPCMBitDepthKey: 16,
        AVLinearPCMIsFloatKey: false,
        AVLinearPCMIsBigEndianKey: false,
    ]

    init(url: URL) throws {
        self.url = url
        self.file = try AVAudioFile(forWriting: url, settings: TrackWriter.outputSettings)
    }

    func close() {
        // Dropping the file reference flushes and finalises the WAV header.
        // AVAudioFile has no explicit close; writing stops when the recorder
        // releases this object.
    }

    private func converter(for format: AVAudioFormat) -> AVAudioConverter? {
        let key = "\(format.sampleRate)-\(format.channelCount)-\(format.commonFormat.rawValue)-\(format.isInterleaved)"
        if let cached = converters[key] {
            return cached
        }
        let created = AVAudioConverter(from: format, to: file.processingFormat)
        converters[key] = created
        return created
    }

    func write(_ input: AVAudioPCMBuffer) {
        guard input.frameLength > 0 else { return }
        guard input.format != file.processingFormat else {
            try? file.write(from: input)
            return
        }
        guard let converter = converter(for: input.format) else { return }
        let ratio = file.processingFormat.sampleRate / input.format.sampleRate
        let capacity = AVAudioFrameCount(Double(input.frameLength) * ratio) + 512
        guard let output = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: capacity) else {
            return
        }
        var consumed = false
        var conversionError: NSError?
        let status = converter.convert(to: output, error: &conversionError) { _, statusOut in
            if consumed {
                statusOut.pointee = .endOfStream
                return nil
            }
            consumed = true
            statusOut.pointee = .haveData
            return input
        }
        guard status != .error else { return }
        try? file.write(from: output)
    }
}

// MARK: - Sample buffer -> PCM buffer

extension CMSampleBuffer {
    func audioPCMBuffer() -> AVAudioPCMBuffer? {
        guard let description = CMSampleBufferGetFormatDescription(self),
              let streamDescription = CMAudioFormatDescriptionGetStreamBasicDescription(description),
              let format = AVAudioFormat(streamDescription: streamDescription)
        else {
            return nil
        }
        let frameCount = CMSampleBufferGetNumSamples(self)
        guard frameCount > 0 else { return nil }
        guard let buffer = AVAudioPCMBuffer(
            pcmFormat: format,
            frameCapacity: AVAudioFrameCount(frameCount)
        ) else {
            return nil
        }
        buffer.frameLength = AVAudioFrameCount(frameCount)
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            self,
            at: 0,
            frameCount: Int32(frameCount),
            into: buffer.mutableAudioBufferList
        )
        guard status == noErr else { return nil }
        return buffer
    }
}

// MARK: - Recorder session

final class MeetingAudioRecorder: NSObject {
    private let systemURL: URL
    private let micURL: URL
    private let sampleQueue = DispatchQueue(label: "ai.magpie.audio.samples")

    private var stream: SCStream?
    private var systemWriter: TrackWriter?
    private var micWriter: TrackWriter?
    private(set) var micEnabled = false

    private let stateLock = NSLock()
    private var finishing = false

    init(outDir: String) {
        let directory = URL(fileURLWithPath: outDir, isDirectory: true)
        self.systemURL = directory.appendingPathComponent("system.wav")
        self.micURL = directory.appendingPathComponent("mic.wav")
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false,
            onScreenWindowsOnly: false
        )
        guard let display = content.displays.first else {
            throw NSError(
                domain: "magpie-helper",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "no display available for audio capture"]
            )
        }

        self.systemWriter = try TrackWriter(url: systemURL)

        let filter = SCContentFilter(display: display, excludingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.width = 2
        configuration.height = 2
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = 16_000
        configuration.channelCount = 1
        if #available(macOS 15.0, *) {
            configuration.captureMicrophone = true
            self.micEnabled = true
            self.micWriter = try TrackWriter(url: micURL)
        }

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: sampleQueue)
        if #available(macOS 15.0, *) {
            try stream.addStreamOutput(self, type: .microphone, sampleHandlerQueue: sampleQueue)
        }
        self.stream = stream
        try await stream.startCapture()
    }

    /// Stops the stream, finalises the WAV files, prints the JSON result and
    /// exits. Safe to call multiple times from any thread.
    func finish(error: String? = nil) {
        stateLock.lock()
        if finishing {
            stateLock.unlock()
            return
        }
        finishing = true
        stateLock.unlock()

        let stream = self.stream
        self.stream = nil

        let stopped = DispatchSemaphore(value: 0)
        if let stream = stream {
            stream.stopCapture { _ in stopped.signal() }
        } else {
            stopped.signal()
        }
        _ = stopped.wait(timeout: .now() + 5)

        // Drain queued sample callbacks before closing files.
        sampleQueue.sync {
            self.systemWriter?.close()
            self.systemWriter = nil
            self.micWriter?.close()
            self.micWriter = nil
        }

        if let error = error {
            emit(["error": error])
            exit(1)
        }

        emit([
            "mic": micEnabled ? micURL.path : NSNull(),
            "system": systemURL.path,
        ])
        exit(0)
    }
}

extension MeetingAudioRecorder: SCStreamDelegate {
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        finish(error: "audio capture stopped: \(error.localizedDescription)")
    }
}

extension MeetingAudioRecorder: SCStreamOutput {
    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard CMSampleBufferIsValid(sampleBuffer),
              CMSampleBufferDataIsReady(sampleBuffer),
              let input = sampleBuffer.audioPCMBuffer()
        else {
            return
        }
        switch type {
        case .audio:
            systemWriter?.write(input)
        case .microphone:
            micWriter?.write(input)
        default:
            break
        }
    }
}

// MARK: - Subcommand entry point

func recordAudio(arguments: [String]) -> Never {
    guard arguments.count >= 3 else {
        fail("usage: magpie-helper record-audio <out-dir>")
    }
    let outDir = arguments[2]
    do {
        try FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)
    } catch {
        fail("could not create output directory: \(error.localizedDescription)")
    }

    let recorder = MeetingAudioRecorder(outDir: outDir)

    // Ignore the default signal handlers; DispatchSourceSignal delivers them
    // on the main run loop instead.
    signal(SIGINT, SIG_IGN)
    signal(SIGTERM, SIG_IGN)

    let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
    sigintSource.setEventHandler { recorder.finish() }
    sigintSource.resume()
    let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
    sigtermSource.setEventHandler { recorder.finish() }
    sigtermSource.resume()

    // Stop when stdin closes (parent process ended or pipe closed).
    DispatchQueue.global(qos: .userInitiated).async {
        while true {
            let data = FileHandle.standardInput.availableData
            if data.isEmpty {
                break
            }
        }
        DispatchQueue.main.async { recorder.finish() }
    }

    let started = DispatchSemaphore(value: 0)
    Task {
        do {
            try await recorder.start()
            started.signal()
        } catch {
            fail("could not start audio capture: \(error.localizedDescription)")
        }
    }
    started.wait()

    dispatchMain()
}
