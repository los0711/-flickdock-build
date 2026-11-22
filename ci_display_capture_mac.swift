import CoreGraphics
import CoreMedia
import CoreVideo
import Darwin
import Foundation
import ScreenCaptureKit

final class Capture: NSObject, SCStreamOutput {
    let secondsPerTick: Double
    init(tickScale: Double) { secondsPerTick = tickScale }

    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .screen,
              let metadata = CMSampleBufferGetSampleAttachmentsArray(buffer, createIfNecessary: false)
                    as? [[SCStreamFrameInfo: Any]],
              let frame = metadata.first,
              (frame[.status] as? Int) == SCFrameStatus.complete.rawValue,
              let displayTime = frame[.displayTime] as? UInt64,
              let pixels = buffer.imageBuffer else { return }
        CVPixelBufferLockBaseAddress(pixels, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixels, .readOnly) }
        guard let raw = CVPixelBufferGetBaseAddress(pixels) else { return }
        let stride = CVPixelBufferGetBytesPerRow(pixels)
        let width = CVPixelBufferGetWidth(pixels)
        let height = CVPixelBufferGetHeight(pixels)
        let bytes = raw.assumingMemoryBound(to: UInt8.self)
        var hash: UInt64 = 1469598103934665603
        for row in 0..<height {
            for column in 0..<(width * 4) {
                hash = (hash ^ UInt64(bytes[row * stride + column])) &* 1099511628211
            }
        }
        let digest = String(hash, radix: 16)
        print("\(String(format: "%.9f", Double(displayTime) * secondsPerTick)) \(digest) 1")
        fflush(stdout)
    }
}

@main struct Main {
    static func main() async {
        let args = CommandLine.arguments.dropFirst().compactMap(Double.init)
        guard args.count == 5, args[2] > 0, args[3] > 0 else { exit(2) }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            guard let display = content.displays.first(where: { $0.displayID == CGMainDisplayID() }) else { exit(3) }
            let config = SCStreamConfiguration()
            config.sourceRect = CGRect(x: args[0], y: args[1], width: args[2], height: args[3])
            config.width = Int(args[2])
            config.height = Int(args[3])
            config.minimumFrameInterval = CMTime(value: 1, timescale: 240)
            config.pixelFormat = kCVPixelFormatType_32BGRA
            config.queueDepth = 4
            config.showsCursor = false
            var info = mach_timebase_info_data_t()
            mach_timebase_info(&info)
            let output = Capture(tickScale: Double(info.numer) / Double(info.denom) / 1e9)
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let stream = SCStream(filter: filter, configuration: config, delegate: nil)
            try stream.addStreamOutput(output, type: .screen, sampleHandlerQueue: DispatchQueue(label: "pixels"))
            try await stream.startCapture()
            print("READY")
            fflush(stdout)
            try await Task.sleep(nanoseconds: UInt64(args[4] * 1e9))
            try await stream.stopCapture()
        } catch {
            FileHandle.standardError.write(Data("ScreenCaptureKit: \(error)\n".utf8))
            exit(4)
        }
    }
}
