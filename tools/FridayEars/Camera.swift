// Is Aman in front of the Mac? On-device face detection (Apple Vision), one frame a second, only while
// there's been no keyboard or mouse input for a while. No frame is stored or sent anywhere; the only
// output is "a face is there".

import AVFoundation
import Vision

final class CameraWatch: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "friday.camera")
    private var configured = false, lastCheck = Date.distantPast
    private(set) var running = false
    var onFace: () -> Void = {}

    private func configure() -> Bool {
        if configured { return true }
        guard let device = AVCaptureDevice.default(for: .video),
              let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input) else {
            log("camera: no usable camera"); return false
        }
        session.beginConfiguration()
        session.sessionPreset = .low
        session.addInput(input)
        let output = AVCaptureVideoDataOutput()
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)
        if session.canAddOutput(output) { session.addOutput(output) }
        session.commitConfiguration()
        configured = true
        return true
    }

    func start() {
        guard !running else { return }
        running = true
        AVCaptureDevice.requestAccess(for: .video) { ok in
            guard ok else { log("camera: access denied (System Settings → Privacy & Security → Camera)"); self.running = false; return }
            self.queue.async {
                guard self.configure() else { self.running = false; return }
                self.session.startRunning()
                log("camera on (you've been idle)")
            }
        }
    }

    func stop() {
        guard running else { return }
        running = false
        queue.async { if self.session.isRunning { self.session.stopRunning(); log("camera off") } }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard Date().timeIntervalSince(lastCheck) >= 1, let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        lastCheck = Date()
        let request = VNDetectFaceRectanglesRequest()
        try? VNImageRequestHandler(cvPixelBuffer: pixels, options: [:]).perform([request])
        if let faces = request.results, !faces.isEmpty { onFace() }
    }
}
