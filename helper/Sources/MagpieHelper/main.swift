import AppKit
import CoreGraphics
import Foundation
import ImageIO
import Vision

// MARK: - Output helpers

func emit(_ object: [String: Any]) {
    guard JSONSerialization.isValidJSONObject(object),
          let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
          let text = String(data: data, encoding: .utf8)
    else {
        FileHandle.standardError.write(Data("failed to encode JSON\n".utf8))
        exit(1)
    }
    print(text)
}

func fail(_ message: String) -> Never {
    emit(["error": message])
    exit(1)
}

// MARK: - OCR

struct OCRLine {
    let text: String
    let confidence: Double
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

func loadCGImage(path: String) -> CGImage {
    let url = URL(fileURLWithPath: path)
    guard FileManager.default.fileExists(atPath: url.path) else {
        fail("image not found: \(path)")
    }
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else {
        fail("could not decode image: \(path)")
    }
    return image
}

func performOCR(path: String) -> Never {
    let image = loadCGImage(path: path)
    let pixelWidth = Double(image.width)
    let pixelHeight = Double(image.height)

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([request])
    } catch {
        fail("ocr failed: \(error.localizedDescription)")
    }

    guard let observations = request.results else {
        emit(["text": "", "lines": []])
        exit(0)
    }

    var lines: [OCRLine] = []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        // Vision uses a bottom-left origin; convert to top-left pixel coordinates.
        let x = Double(box.minX) * pixelWidth
        let y = (1.0 - Double(box.maxY)) * pixelHeight
        let w = Double(box.width) * pixelWidth
        let h = Double(box.height) * pixelHeight
        lines.append(
            OCRLine(
                text: candidate.string,
                confidence: Double(candidate.confidence),
                x: (x * 1000).rounded() / 1000,
                y: (y * 1000).rounded() / 1000,
                width: (w * 1000).rounded() / 1000,
                height: (h * 1000).rounded() / 1000
            )
        )
    }

    // Group by row (rounded to the nearest pixel) so tiny baseline
    // differences do not break left-to-right ordering within a line.
    lines.sort { lhs, rhs in
        let lhsRow = Int(lhs.y.rounded())
        let rhsRow = Int(rhs.y.rounded())
        if lhsRow != rhsRow {
            return lhsRow < rhsRow
        }
        if lhs.x != rhs.x {
            return lhs.x < rhs.x
        }
        return lhs.y < rhs.y
    }

    let payloadLines: [[String: Any]] = lines.map { line in
        [
            "text": line.text,
            "confidence": line.confidence,
            "x": line.x,
            "y": line.y,
            "w": line.width,
            "h": line.height
        ]
    }

    emit([
        "text": lines.map(\.text).joined(separator: "\n"),
        "lines": payloadLines
    ])
    exit(0)
}

// MARK: - Frontmost application

func windowTitle(for pid: pid_t) -> String {
    guard let rawWindows = CGWindowListCopyWindowInfo(
        [.optionOnScreenOnly, .excludeDesktopElements],
        kCGNullWindowID
    ) as? [[String: Any]] else {
        return ""
    }

    var bestTitle = ""
    var bestArea = -1.0
    for window in rawWindows {
        guard let layer = window[kCGWindowLayer as String] as? Int, layer == 0 else { continue }
        guard let ownerPID = window[kCGWindowOwnerPID as String] as? Int,
              pid_t(ownerPID) == pid
        else { continue }
        guard let bounds = window[kCGWindowBounds as String] as? [String: Any] else { continue }
        let width = (bounds["Width"] as? NSNumber)?.doubleValue ?? 0
        let height = (bounds["Height"] as? NSNumber)?.doubleValue ?? 0
        let area = width * height
        let title = (window[kCGWindowName as String] as? String) ?? ""
        if title.isEmpty {
            continue
        }
        if area > bestArea {
            bestArea = area
            bestTitle = title
        }
    }
    return bestTitle
}

func printFrontmost() -> Never {
    let workspace = NSWorkspace.shared
    guard let app = workspace.frontmostApplication else {
        emit([
            "app": "",
            "bundle_id": "",
            "pid": 0,
            "window_title": ""
        ])
        exit(0)
    }

    let name = app.localizedName ?? ""
    let bundleID = app.bundleIdentifier ?? ""
    let pid = app.processIdentifier
    let title = windowTitle(for: pid)

    emit([
        "app": name,
        "bundle_id": bundleID,
        "pid": Int(pid),
        "window_title": title
    ])
    exit(0)
}

// MARK: - Idle

func printIdle() -> Never {
    // ~0 as CGEventType means "any input event".
    let anyInput = CGEventType(rawValue: ~0)!
    let seconds = CGEventSource.secondsSinceLastEventType(
        .combinedSessionState,
        eventType: anyInput
    )
    emit(["seconds": seconds])
    exit(0)
}

// MARK: - Permissions

func printPermissions() -> Never {
    emit(["screen_recording": CGPreflightScreenCaptureAccess()])
    exit(0)
}

// MARK: - Async runner

/// Runs an async subcommand to completion from the synchronous entry point.
/// Subcommands finish by calling exit(); the semaphore is a safety net.
func runAsync(_ work: @escaping () async -> Void) -> Never {
    let semaphore = DispatchSemaphore(value: 0)
    Task {
        await work()
        semaphore.signal()
    }
    semaphore.wait()
    exit(0)
}

// MARK: - Entry point

let arguments = CommandLine.arguments
guard arguments.count >= 2 else {
    fail("usage: magpie-helper <ocr|frontmost|idle|permissions|record-audio|calendar-events|add-reminder|add-event> [args]")
}

switch arguments[1].lowercased() {
case "ocr":
    guard arguments.count >= 3 else {
        fail("ocr requires an image path")
    }
    performOCR(path: arguments[2])
case "frontmost":
    printFrontmost()
case "idle":
    printIdle()
case "permissions":
    printPermissions()
case "record-audio":
    recordAudio(arguments: arguments)
case "calendar-events":
    runAsync { await calendarEvents(arguments: arguments) }
case "add-reminder":
    runAsync { await addReminder(arguments: arguments) }
case "add-event":
    runAsync { await addEvent(arguments: arguments) }
default:
    fail("unknown subcommand: \(arguments[1])")
}
