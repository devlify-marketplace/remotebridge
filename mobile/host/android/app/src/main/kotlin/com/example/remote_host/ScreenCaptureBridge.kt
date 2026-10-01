package com.example.remote_host

import android.os.Handler
import android.os.Looper
import io.flutter.plugin.common.EventChannel

/**
 * `ScreenCaptureService`'s `ImageReader` callback runs on a background
 * thread; `EventChannel.EventSink.success()` must be called on the
 * platform (main) thread. This is the hop between the two - set by
 * `MainActivity`'s `EventChannel.StreamHandler` when Dart starts
 * listening, read by the service on every captured frame.
 */
object ScreenCaptureBridge {
    @Volatile var sink: EventChannel.EventSink? = null
    private val mainHandler = Handler(Looper.getMainLooper())

    fun onFrame(jpegBytes: ByteArray) {
        val currentSink = sink ?: return
        mainHandler.post { currentSink.success(jpegBytes) }
    }
}
