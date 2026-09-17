import AppKit
import Foundation
import Vision

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("需要图片路径\n".utf8))
    exit(2)
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: url),
      let data = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: data),
      let cgImage = bitmap.cgImage else {
    FileHandle.standardError.write(Data("无法读取图片\n".utf8))
    exit(3)
}
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = true
do {
    try VNImageRequestHandler(cgImage: cgImage).perform([request])
    let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
    print(lines.joined(separator: "\n"))
} catch {
    FileHandle.standardError.write(Data("OCR 失败：\(error.localizedDescription)\n".utf8))
    exit(4)
}
