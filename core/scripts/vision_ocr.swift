import AppKit
import Foundation
import Vision

struct OCRItem: Codable {
    let text: String
    let confidence: Float
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

struct OCRRecord: Codable {
    let file: String
    let items: [OCRItem]
    let error: String?
}

guard CommandLine.arguments.count == 3 else {
    fputs("usage: vision_ocr IMAGE_DIR OUTPUT.jsonl\n", stderr)
    exit(2)
}

let inputDirectory = URL(fileURLWithPath: CommandLine.arguments[1])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])
let fileManager = FileManager.default
let images = try fileManager.contentsOfDirectory(
    at: inputDirectory,
    includingPropertiesForKeys: nil
).filter {
    ["jpg", "jpeg", "png", "heic", "tif", "tiff"].contains($0.pathExtension.lowercased())
}.sorted {
    $0.lastPathComponent < $1.lastPathComponent
}

guard !images.isEmpty else {
    fputs("no supported images found in \(inputDirectory.path)\n", stderr)
    exit(3)
}

fileManager.createFile(atPath: outputURL.path, contents: nil)
let output = try FileHandle(forWritingTo: outputURL)
defer { try? output.close() }
let encoder = JSONEncoder()
var failureCount = 0

for imageURL in images {
    autoreleasepool {
        var items: [OCRItem] = []
        var recordError: String? = nil
        guard
            let image = NSImage(contentsOf: imageURL),
            let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
        else {
            recordError = "image_decode_failed"
            failureCount += 1
            let record = OCRRecord(file: imageURL.lastPathComponent, items: [], error: recordError)
            if let data = try? encoder.encode(record) {
                output.write(data)
                output.write(Data([0x0A]))
            }
            return
        }

        let request = VNRecognizeTextRequest { request, error in
            if let error = error {
                recordError = "vision_request_failed: \(error)"
                return
            }
            guard let observations = request.results as? [VNRecognizedTextObservation] else {
                recordError = "vision_results_missing"
                return
            }
            items = observations.compactMap { observation in
                guard let candidate = observation.topCandidates(1).first else {
                    return nil
                }
                let box = observation.boundingBox
                return OCRItem(
                    text: candidate.string,
                    confidence: candidate.confidence,
                    x: box.origin.x,
                    y: box.origin.y,
                    width: box.size.width,
                    height: box.size.height
                )
            }
        }
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        request.recognitionLanguages = ["zh-Hans", "en-US"]

        do {
            try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
        } catch {
            recordError = "vision_handler_failed: \(error)"
        }
        if recordError != nil {
            failureCount += 1
        }
        let record = OCRRecord(file: imageURL.lastPathComponent, items: items, error: recordError)
        if let data = try? encoder.encode(record) {
            output.write(data)
            output.write(Data([0x0A]))
        }
    }
}

if failureCount > 0 {
    fputs("Vision OCR failed for \(failureCount) image(s)\n", stderr)
    exit(4)
}
