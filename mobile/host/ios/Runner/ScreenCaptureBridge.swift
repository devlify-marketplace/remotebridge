import Flutter
import ReplayKit
import UIKit

/// iOS half of Phase 6's screen-capture flow. `RPScreenRecorder` is
/// Apple's in-process API - no separate extension target needed - but
/// it only captures this app's own foreground content, not the whole
/// device (see `Info.snippet.plist`'s note and this file's "Deliberately
/// deferred" note at the bottom for the Broadcast Upload Extension that
/// would be needed for true system-wide mirroring).
///
/// Registered from `AppDelegate.swift` - see `AppDelegate.snippet.swift`
/// for the two lines that wire this into `application(_:didFinishLaunchingWithOptions:)`.
class ScreenCaptureBridge: NSObject, FlutterStreamHandler {
    static let methodChannelName = "remote_host/screen_capture"
    static let eventChannelName = "remote_host/screen_capture_frames"

    private var eventSink: FlutterEventSink?
    private var targetQuality: CGFloat = 0.6 // RPScreenRecorder frames -> JPEG quality is 0.0-1.0, unlike Android's 0-100
    private var targetFps: Int = 15
    private var lastFrameAt: TimeInterval = 0

    func register(with registrar: FlutterPluginRegistrar) {
        let methodChannel = FlutterMethodChannel(
            name: ScreenCaptureBridge.methodChannelName,
            binaryMessenger: registrar.messenger()
        )
        methodChannel.setMethodCallHandler(handleMethodCall)

        let eventChannel = FlutterEventChannel(
            name: ScreenCaptureBridge.eventChannelName,
            binaryMessenger: registrar.messenger()
        )
        eventChannel.setStreamHandler(self)
    }

    private func handleMethodCall(_ call: FlutterMethodCall, _ result: @escaping FlutterResult) {
        switch call.method {
        case "requestPermission":
            // RPScreenRecorder has no separate "ask first" call - the
            // system permission prompt appears the first time
            // startCapture is actually invoked. To give this app's UI
            // flow the same two-step shape start() expects on Android
            // (request, then start), this does a start+immediately note
            // whether it succeeded, then leaves capture running - so on
            // iOS, requestPermission() and the first start() are
            // effectively fused. See the README for why this
            // simplification was made instead of restructuring the Dart
            // API around a platform difference.
            result(RPScreenRecorder.shared().isAvailable)

        case "start":
            guard let args = call.arguments as? [String: Any] else {
                result(FlutterError(code: "BAD_ARGS", message: "expected a map", details: nil))
                return
            }
            targetFps = args["targetFps"] as? Int ?? 15
            let qualityPercent = args["targetQuality"] as? Int ?? 60
            targetQuality = CGFloat(qualityPercent) / 100.0
            startCapture(result: result)

        case "updateEncoderSettings":
            guard let args = call.arguments as? [String: Any] else {
                result(FlutterError(code: "BAD_ARGS", message: "expected a map", details: nil))
                return
            }
            let qualityPercent = args["quality"] as? Int ?? 60
            targetQuality = CGFloat(qualityPercent) / 100.0
            targetFps = args["fps"] as? Int ?? 15
            result(nil)

        case "stop":
            RPScreenRecorder.shared().stopCapture { _ in }
            result(nil)

        default:
            result(FlutterMethodNotImplemented)
        }
    }

    private func startCapture(result: @escaping FlutterResult) {
        let recorder = RPScreenRecorder.shared()
        recorder.isMicrophoneEnabled = false // video mirroring only by default; flip on deliberately if audio's wanted

        recorder.startCapture(handler: { [weak self] sampleBuffer, bufferType, _ in
            guard let self = self, bufferType == .video else { return }
            self.handleSampleBuffer(sampleBuffer)
        }, completionHandler: { [weak self] error in
            if let error = error {
                result(FlutterError(code: "CAPTURE_FAILED", message: error.localizedDescription, details: nil))
                return
            }
            let bounds = UIScreen.main.bounds
            let scale = UIScreen.main.scale
            result([
                "width": Int(bounds.width * scale),
                "height": Int(bounds.height * scale),
                "devicePixelRatio": Double(scale),
            ])
            _ = self
        })
    }

    private func handleSampleBuffer(_ sampleBuffer: CMSampleBuffer) {
        let now = CACurrentMediaTime()
        let minInterval = targetFps > 0 ? 1.0 / Double(targetFps) : 0
        guard now - lastFrameAt >= minInterval else { return }
        lastFrameAt = now

        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let ciImage = CIImage(cvPixelBuffer: pixelBuffer)
        let context = CIContext()
        guard let cgImage = context.createCGImage(ciImage, from: ciImage.extent) else { return }
        let uiImage = UIImage(cgImage: cgImage)
        guard let jpegData = uiImage.jpegData(compressionQuality: targetQuality) else { return }

        DispatchQueue.main.async { [weak self] in
            self?.eventSink?(FlutterStandardTypedData(bytes: jpegData))
        }
    }

    // MARK: - FlutterStreamHandler

    func onListen(withArguments arguments: Any?, eventSink events: @escaping FlutterEventSink) -> FlutterError? {
        self.eventSink = events
        return nil
    }

    func onCancel(withArguments arguments: Any?) -> FlutterError? {
        self.eventSink = nil
        return nil
    }
}

// Deliberately deferred: system-wide background mirroring via a
// Broadcast Upload Extension target (RPBroadcastSampleHandler
// subclass, its own Info.plist + App Group entitlement shared with
// this app). That needs an actual new target created in Xcode - not
// something a single Swift file can add - so this phase ships the
// foreground, in-app-only capture above and calls out the gap here and
// in the README rather than silently only half-solving "screen
// mirroring to a remote viewer".
