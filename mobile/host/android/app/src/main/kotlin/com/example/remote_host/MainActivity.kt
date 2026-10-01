package com.example.remote_host
// NOTE: this package must match whatever `flutter create .` generates
// for this project (from pubspec.yaml's `name: remote_host` plus
// whatever bundle ID you choose) - `com.example.remote_host` is a
// placeholder, same spirit as the controller app's manifest snippet
// note about FlutterFragmentActivity. Rename the directory to match.

import android.app.Activity
import android.content.Intent
import android.media.projection.MediaProjectionManager
import androidx.annotation.NonNull
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel

/**
 * Phase 6 host bridge. Owns the one Activity-only step in this flow -
 * `MediaProjectionManager.createScreenCaptureIntent()` must be launched
 * with `startActivityForResult` from an Activity, since it shows the
 * system's "Start recording or casting?" dialog - everything after that
 * (the actual capture loop) lives in [ScreenCaptureService] so it can
 * keep running with the app backgrounded.
 */
class MainActivity : FlutterActivity() {
    private val methodChannelName = "remote_host/screen_capture"
    private val eventChannelName = "remote_host/screen_capture_frames"
    private val projectionRequestCode = 6001

    private lateinit var projectionManager: MediaProjectionManager
    private var pendingPermissionResult: MethodChannel.Result? = null
    private var pendingStartCall: Pair<Int, Intent>? = null

    override fun configureFlutterEngine(@NonNull flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        projectionManager = getSystemService(android.content.Context.MEDIA_PROJECTION_SERVICE)
                as MediaProjectionManager

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, methodChannelName)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "requestPermission" -> {
                        pendingPermissionResult = result
                        startActivityForResult(
                            projectionManager.createScreenCaptureIntent(),
                            projectionRequestCode
                        )
                    }
                    "start" -> {
                        val targetFps = call.argument<Int>("targetFps") ?: 15
                        val targetQuality = call.argument<Int>("targetQuality") ?: 60
                        val pending = pendingStartCall
                        if (pending == null) {
                            result.error(
                                "NOT_PERMITTED",
                                "requestPermission() must succeed before start()",
                                null
                            )
                        } else {
                            val (resultCode, data) = pending
                            val dims = ScreenCaptureService.start(
                                this, resultCode, data, targetFps, targetQuality
                            )
                            result.success(
                                mapOf(
                                    "width" to dims.width,
                                    "height" to dims.height,
                                    "devicePixelRatio" to dims.devicePixelRatio
                                )
                            )
                        }
                    }
                    "updateEncoderSettings" -> {
                        val quality = call.argument<Int>("quality") ?: 60
                        val fps = call.argument<Int>("fps") ?: 15
                        ScreenCaptureService.updateEncoderSettings(quality, fps)
                        result.success(null)
                    }
                    "stop" -> {
                        ScreenCaptureService.stop(this)
                        result.success(null)
                    }
                    else -> result.notImplemented()
                }
            }

        EventChannel(flutterEngine.dartExecutor.binaryMessenger, eventChannelName)
            .setStreamHandler(object : EventChannel.StreamHandler {
                override fun onListen(arguments: Any?, sink: EventChannel.EventSink) {
                    ScreenCaptureBridge.sink = sink
                }

                override fun onCancel(arguments: Any?) {
                    ScreenCaptureBridge.sink = null
                }
            })
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != projectionRequestCode) return

        val granted = resultCode == Activity.RESULT_OK && data != null
        if (granted) {
            pendingStartCall = Pair(resultCode, data!!)
        }
        pendingPermissionResult?.success(granted)
        pendingPermissionResult = null
    }
}
