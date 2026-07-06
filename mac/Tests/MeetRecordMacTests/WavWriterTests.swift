// WavWriterTests.swift
//
// Header-byte-level tests for the WAV writer. These run on any macOS host;
// they don't touch Core Audio, so they validate the on-disk contract that
// meet_record/capture.py depends on (44-byte header, 16 kHz, stereo s16le).
//
// SPDX-License-Identifier: GPL-3.0-or-later

import XCTest
@testable import MeetRecordMac

final class WavWriterTests: XCTestCase {
    func tmpURL(_ name: String) -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("meet-record-mac-tests-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent(name)
    }

    /// Empty WAV (no samples) must still be exactly 44 bytes.
    func testEmptyWavIsExactly44Bytes() throws {
        let url = tmpURL("empty.wav")
        let writer = try WavWriter(url: url, sampleRate: 16_000, channels: 2)
        try writer.close()

        let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
        let size = (attrs[.size] as? NSNumber)?.intValue ?? -1
        XCTAssertEqual(size, 44, "Empty WAV must be exactly 44 bytes (canonical RIFF header)")
    }

    /// 1 second of stereo 16 kHz s16 = 64000 bytes of audio + 44 of header.
    func testOneSecondStereo16kIs64044Bytes() throws {
        let url = tmpURL("onesec.wav")
        let writer = try WavWriter(url: url, sampleRate: 16_000, channels: 2)

        let frames = 16_000  // 1 second
        var interleaved = [Int16](repeating: 0, count: frames * 2)
        // Fill right channel with a known marker so we also verify byte
        // layout (left=0 even bytes, right!=0 odd indices).
        for i in 0..<frames {
            interleaved[2 * i] = 0
            interleaved[2 * i + 1] = 1234
        }

        try interleaved.withUnsafeBufferPointer { ptr in
            try writer.writeSamples(ptr.baseAddress!, count: interleaved.count)
        }
        try writer.close()

        let data = try Data(contentsOf: url)
        XCTAssertEqual(data.count, 44 + 64_000, "1s stereo 16k s16 must be 64044 bytes")

        // RIFF header sanity.
        XCTAssertEqual(String(data: data[0..<4], encoding: .ascii), "RIFF")
        XCTAssertEqual(String(data: data[8..<12], encoding: .ascii), "WAVE")
        XCTAssertEqual(String(data: data[12..<16], encoding: .ascii), "fmt ")
        XCTAssertEqual(String(data: data[36..<40], encoding: .ascii), "data")

        // RIFF size = file size - 8.
        let riffSize = data.subdata(in: 4..<8).withUnsafeBytes { $0.load(as: UInt32.self) }
        XCTAssertEqual(Int(UInt32(littleEndian: riffSize)), data.count - 8)

        // data chunk size = 64000.
        let dataSize = data.subdata(in: 40..<44).withUnsafeBytes { $0.load(as: UInt32.self) }
        XCTAssertEqual(Int(UInt32(littleEndian: dataSize)), 64_000)

        // Sample rate field at byte 24.
        let sr = data.subdata(in: 24..<28).withUnsafeBytes { $0.load(as: UInt32.self) }
        XCTAssertEqual(UInt32(littleEndian: sr), 16_000)

        // Byte rate at byte 28: 16000 * 2ch * 2bytes = 64000.
        let byteRate = data.subdata(in: 28..<32).withUnsafeBytes { $0.load(as: UInt32.self) }
        XCTAssertEqual(UInt32(littleEndian: byteRate), 64_000)

        // First sample frame: L=0, R=1234.
        let firstL = data.subdata(in: 44..<46).withUnsafeBytes { $0.load(as: Int16.self) }
        let firstR = data.subdata(in: 46..<48).withUnsafeBytes { $0.load(as: Int16.self) }
        XCTAssertEqual(Int16(littleEndian: firstL), 0)
        XCTAssertEqual(Int16(littleEndian: firstR), 1234)
    }

    /// Channel-order is L=mic, R=system. Ensure byte layout matches the
    /// downstream Python expectation (audio.py:read_stereo_channels assigns
    /// samples[:, 0] → mic, samples[:, 1] → system).
    func testChannelOrderingLMicRSystem() throws {
        let url = tmpURL("channels.wav")
        let writer = try WavWriter(url: url, sampleRate: 16_000, channels: 2)

        // Single frame: L=100 (mic), R=200 (system).
        let frame: [Int16] = [100, 200]
        try frame.withUnsafeBufferPointer { ptr in
            try writer.writeSamples(ptr.baseAddress!, count: frame.count)
        }
        try writer.close()

        let data = try Data(contentsOf: url)
        XCTAssertEqual(data.count, 44 + 4)
        let l = Int16(littleEndian: data.subdata(in: 44..<46).withUnsafeBytes { $0.load(as: Int16.self) })
        let r = Int16(littleEndian: data.subdata(in: 46..<48).withUnsafeBytes { $0.load(as: Int16.self) })
        XCTAssertEqual(l, 100, "Left channel must hold the mic sample")
        XCTAssertEqual(r, 200, "Right channel must hold the system-audio sample")
    }

    /// Crash resilience: after writing more than the periodic patch
    /// interval (10 s of audio) WITHOUT calling close(), the on-disk
    /// header must already hold (near-)correct sizes — so a SIGKILLed
    /// recorder leaves a usable chunk, not a zero-length `data` chunk.
    func testHeaderPatchedPeriodicallyWithoutClose() throws {
        let url = tmpURL("periodic.wav")
        let writer = try WavWriter(url: url, sampleRate: 16_000, channels: 2)

        // 11 s of stereo 16 kHz s16 = 704,000 data bytes > the 640,000-
        // byte patch interval, so at least one in-place patch must fire.
        let frames = 16_000 * 11
        let interleaved = [Int16](repeating: 42, count: frames * 2)
        try interleaved.withUnsafeBufferPointer { ptr in
            try writer.writeSamples(ptr.baseAddress!, count: interleaved.count)
        }
        // Deliberately NO close() — simulate SIGKILL by just dropping to
        // direct file inspection while the handle is still open.

        let data = try Data(contentsOf: url)
        XCTAssertEqual(data.count, 44 + 704_000)

        let dataSize = data.subdata(in: 40..<44).withUnsafeBytes { $0.load(as: UInt32.self) }
        let patched = Int(UInt32(littleEndian: dataSize))
        // The patch fires on interval boundaries, so the recorded size
        // lags the true size by at most one interval — but must never
        // still be the placeholder 0.
        XCTAssertGreaterThanOrEqual(patched, 640_000, "data size must have been patched in place")
        XCTAssertLessThanOrEqual(patched, 704_000)

        let riffSize = data.subdata(in: 4..<8).withUnsafeBytes { $0.load(as: UInt32.self) }
        XCTAssertEqual(Int(UInt32(littleEndian: riffSize)), patched + 36)

        try writer.close()
        // After close, sizes must be exact.
        let closed = try Data(contentsOf: url)
        let finalSize = closed.subdata(in: 40..<44).withUnsafeBytes { $0.load(as: UInt32.self) }
        XCTAssertEqual(Int(UInt32(littleEndian: finalSize)), 704_000)
    }
}
