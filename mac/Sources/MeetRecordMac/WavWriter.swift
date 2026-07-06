// WavWriter.swift
//
// Streaming RIFF WAV writer for stereo, 16-bit signed little-endian PCM at a
// fixed sample rate. Produces a 44-byte standard WAV header followed by
// interleaved L/R s16 samples.
//
// We do NOT use AVAudioFile here because the Python side
// (meet_record/capture.py) computes elapsed wall-clock time from
// (file_size_bytes - 44) / 64000, which assumes the canonical 44-byte
// header. AVAudioFile may write extended headers depending on the format,
// so a hand-rolled writer is the safest way to keep the contract.
//
// SPDX-License-Identifier: GPL-3.0-or-later

import Foundation

enum WavWriterError: Error {
    case openFailed(String)
    case writeFailed(String)
    case alreadyClosed
}

/// Streaming WAV writer that produces a 44-byte header followed by
/// interleaved s16 PCM samples. The header sizes are patched in `close()`.
final class WavWriter {
    let url: URL
    let sampleRate: UInt32
    let channels: UInt16
    private let bitsPerSample: UInt16 = 16

    private var handle: FileHandle?
    private var dataBytesWritten: UInt32 = 0
    private var closed = false

    /// Re-patch the header sizes in place every ~10 s of audio so a
    /// SIGKILL / hard crash (where `close()` and `deinit` never run)
    /// leaves an at-most-10-s-stale header instead of a zero-length
    /// `data` chunk. ffmpeg's concat demuxer trusts the header; the
    /// Python side additionally repairs headers from file size before
    /// stitching (capture.py:_repair_wav_header), so this is belt and
    /// braces. 640,000 bytes = 10 s at 16 kHz stereo s16.
    private let headerPatchIntervalBytes: Int = 640_000
    private var bytesSinceHeaderPatch: Int = 0

    init(url: URL, sampleRate: UInt32, channels: UInt16) throws {
        self.url = url
        self.sampleRate = sampleRate
        self.channels = channels

        // Truncate-create the file.
        FileManager.default.createFile(atPath: url.path, contents: nil)
        guard let handle = try? FileHandle(forWritingTo: url) else {
            throw WavWriterError.openFailed(url.path)
        }
        self.handle = handle

        // Write a placeholder 44-byte header. Sizes are patched on close.
        try writeHeader(dataLength: 0)
    }

    /// Write `count` interleaved s16 samples (channels × frames).
    func writeSamples(_ samples: UnsafePointer<Int16>, count: Int) throws {
        guard let handle = handle, !closed else {
            throw WavWriterError.alreadyClosed
        }
        let byteCount = count * MemoryLayout<Int16>.size
        let data = Data(bytes: samples, count: byteCount)
        do {
            try handle.write(contentsOf: data)
        } catch {
            throw WavWriterError.writeFailed("\(error)")
        }
        dataBytesWritten += UInt32(byteCount)
        maybePatchHeader(bytesJustWritten: byteCount)
    }

    /// Convenience: append a chunk of interleaved s16 samples from a Data buffer.
    func writeData(_ data: Data) throws {
        guard let handle = handle, !closed else {
            throw WavWriterError.alreadyClosed
        }
        do {
            try handle.write(contentsOf: data)
        } catch {
            throw WavWriterError.writeFailed("\(error)")
        }
        dataBytesWritten += UInt32(data.count)
        maybePatchHeader(bytesJustWritten: data.count)
    }

    /// Patch the RIFF + data chunk sizes and close the file.
    func close() throws {
        guard let handle = handle, !closed else { return }
        closed = true
        try handle.synchronize()
        try handle.seek(toOffset: 0)
        try writeHeader(dataLength: dataBytesWritten)
        try handle.synchronize()
        try handle.close()
        self.handle = nil
    }

    deinit {
        if !closed {
            try? close()
        }
    }

    // MARK: - Private

    /// Periodically rewrite the header sizes in place (crash resilience).
    /// Best-effort: an I/O error here is swallowed — the next interval or
    /// `close()` will retry, and the Python side repairs headers from the
    /// file size before stitching anyway.
    private func maybePatchHeader(bytesJustWritten: Int) {
        bytesSinceHeaderPatch += bytesJustWritten
        guard bytesSinceHeaderPatch >= headerPatchIntervalBytes else { return }
        bytesSinceHeaderPatch = 0
        guard let handle = handle, !closed else { return }
        do {
            let end = try handle.offset()
            try handle.seek(toOffset: 0)
            try writeHeader(dataLength: dataBytesWritten)
            try handle.seek(toOffset: end)
        } catch {
            // Swallow: periodic patching must never break the recording.
        }
    }

    private func writeHeader(dataLength: UInt32) throws {
        guard let handle = handle else { return }

        let blockAlign = channels * bitsPerSample / 8                // 4 for stereo s16
        let byteRate = sampleRate * UInt32(blockAlign)               // 64000 at 16 kHz stereo s16
        let riffSize = UInt32(36) &+ dataLength                      // 44 - 8 + data

        var header = Data()
        header.append(contentsOf: Array("RIFF".utf8))                // 0..4
        header.append(le32(riffSize))                                // 4..8
        header.append(contentsOf: Array("WAVE".utf8))                // 8..12
        header.append(contentsOf: Array("fmt ".utf8))                // 12..16
        header.append(le32(16))                                      // 16..20  PCM fmt chunk size
        header.append(le16(1))                                       // 20..22  PCM format
        header.append(le16(channels))                                // 22..24
        header.append(le32(sampleRate))                              // 24..28
        header.append(le32(byteRate))                                // 28..32
        header.append(le16(blockAlign))                              // 32..34
        header.append(le16(bitsPerSample))                           // 34..36
        header.append(contentsOf: Array("data".utf8))                // 36..40
        header.append(le32(dataLength))                              // 40..44

        precondition(header.count == 44, "WAV header must be exactly 44 bytes")
        try handle.write(contentsOf: header)
    }

    private func le16(_ v: UInt16) -> Data {
        var x = v.littleEndian
        return Data(bytes: &x, count: MemoryLayout<UInt16>.size)
    }

    private func le32(_ v: UInt32) -> Data {
        var x = v.littleEndian
        return Data(bytes: &x, count: MemoryLayout<UInt32>.size)
    }
}
