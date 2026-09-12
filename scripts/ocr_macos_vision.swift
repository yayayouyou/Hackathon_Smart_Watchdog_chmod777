#!/usr/bin/env swift

import Foundation
import ImageIO
import Vision

struct OCRLine: Codable {
    let text: String
    let confidence: Float
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

guard CommandLine.arguments.count == 3 else {
    fail("usage: swift scripts/ocr_macos_vision.swift <input.png> <output.json>")
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])

guard let source = CGImageSourceCreateWithURL(inputURL as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fail("cannot load image: \(inputURL.path)")
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hant", "en-US"]
request.usesLanguageCorrection = false
request.minimumTextHeight = 0.006

let handler = VNImageRequestHandler(cgImage: image, options: [:])
do {
    try handler.perform([request])
} catch {
    fail("Vision OCR failed: \(error)")
}

let lines = (request.results ?? []).compactMap { observation -> OCRLine? in
    guard let candidate = observation.topCandidates(1).first else { return nil }
    let box = observation.boundingBox
    return OCRLine(
        text: candidate.string,
        confidence: candidate.confidence,
        x: box.origin.x,
        y: box.origin.y,
        width: box.size.width,
        height: box.size.height
    )
}.sorted {
    let rowDelta = $0.y - $1.y
    if abs(rowDelta) > 0.005 { return rowDelta > 0 }
    return $0.x < $1.x
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]

do {
    let data = try encoder.encode(lines)
    try FileManager.default.createDirectory(
        at: outputURL.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    try data.write(to: outputURL, options: .atomic)
} catch {
    fail("cannot write OCR JSON: \(error)")
}

let meanConfidence = lines.isEmpty
    ? 0
    : lines.reduce(0.0) { $0 + Double($1.confidence) } / Double(lines.count)
print("\(inputURL.lastPathComponent): \(lines.count) lines, mean confidence \(String(format: "%.3f", meanConfidence))")
