package com.example.remote_host

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.IBinder
import android.util.DisplayMetrics
import androidx.core.app.NotificationCompat
import java.io.ByteArrayOutputStream

data class ScreenDimensions(val width: Int, val height: Int, val devicePixelRatio: Double)

/**
 * Phase 6 - the actual "screen mirroring to a remote viewer" loop:
 * MediaProjection -> VirtualDisplay -> ImageReader -> Bitmap -> JPEG
 * bytes -> [ScreenCaptureBridge] -> Flutter's EventChannel ->
 * `screen_capture_service.dart`'s `frames` stream -> `host_session.dart`
 * wraps each one in `MSG_VIDEO_FRAME` exactly like
 * `desktop/host.py`'s `video_loop` does with its `mss`-captured frames -
 * same wire message, same JPEG format, different capture backend.
 *
 * Runs as a foreground service (not tied to MainActivity's lifecycle) so
 * capture keeps going while the host phone's screen shows something
 * else - the entire point of Phase 6 versus Phase 5's controller app,
 * which only ever needs to run in the foreground.
 */
class ScreenCaptureService : Service() {

    companion object {
        private const val notificationChannelId = "screen_capture_channel"
        private const val notificationId = 6002

        @Volatile private var mediaProjection: MediaProjection? = null
        @Volatile private var virtualDisplay: VirtualDisplay? = null
        @Volatile private var imageReader: ImageReader? = null

        // Encoder settings the Dart side updates via updateEncoderSettings()
        // (driven by battery_guard.dart + adaptive_bitrate.dart) - read by
        // the ImageReader callback on every frame, written from the
        // platform-channel handler thread, hence @Volatile rather than a
        // lock: a torn read here just means one frame used the previous
        // setting, which is harmless.
        @Volatile private var targetQuality: Int = 60
        @Volatile private var targetFps: Int = 15
        @Volatile private var lastFrameAtMillis: Long = 0

        fun start(
            context: Context,
            resultCode: Int,
            data: Intent,
            initialFps: Int,
            initialQuality: Int
        ): ScreenDimensions {
            targetFps = initialFps
            targetQuality = initialQuality

            val intent = Intent(context, ScreenCaptureService::class.java)
            intent.putExtra("resultCode", resultCode)
            intent.putExtra("data", data)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }

            val metrics = DisplayMetrics()
            val windowManager = context.getSystemService(Context.WINDOW_SERVICE)
                    as android.view.WindowManager
            @Suppress("DEPRECATION")
            windowManager.defaultDisplay.getRealMetrics(metrics)
            return ScreenDimensions(metrics.widthPixels, metrics.heightPixels, metrics.density.toDouble())
        }

        fun updateEncoderSettings(quality: Int, fps: Int) {
            targetQuality = quality
            targetFps = fps
        }

        fun stop(context: Context) {
            virtualDisplay?.release()
            imageReader?.close()
            mediaProjection?.stop()
            virtualDisplay = null
            imageReader = null
            mediaProjection = null
            context.stopService(Intent(context, ScreenCaptureService::class.java))
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val resultCode = intent?.getIntExtra("resultCode", 0) ?: 0
        @Suppress("DEPRECATION")
        val data = intent?.getParcelableExtra<Intent>("data")

        startForeground(notificationId, buildNotification())

        if (data != null) {
            beginCapture(resultCode, data)
        }
        return START_NOT_STICKY
    }

    private fun buildNotification(): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                notificationChannelId,
                "Screen sharing",
                NotificationManager.IMPORTANCE_LOW
            )
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(channel)
        }
        return NotificationCompat.Builder(this, notificationChannelId)
            .setContentTitle("Screen is being shared")
            .setContentText("A remote viewer can see this phone's screen.")
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setOngoing(true)
            .build()
    }

    private fun beginCapture(resultCode: Int, data: Intent) {
        val manager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        val projection = manager.getMediaProjection(resultCode, data) ?: return
        mediaProjection = projection

        val metrics = resources.displayMetrics
        val width = metrics.widthPixels
        val height = metrics.heightPixels
        val density = metrics.densityDpi

        val reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2)
        imageReader = reader

        virtualDisplay = projection.createVirtualDisplay(
            "remotebridge-host",
            width, height, density,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader.surface, null, null
        )

        reader.setOnImageAvailableListener({ imageReader ->
            val image = imageReader.acquireLatestImage() ?: return@setOnImageAvailableListener
            try {
                // Frame-rate throttling happens here rather than by
                // skipping the ImageReader callback itself, since
                // MediaProjection delivers frames at the display's own
                // refresh rate regardless of what we do with them -
                // dropping the ones we don't need keeps CPU/JPEG-encode
                // cost (and therefore battery draw) proportional to
                // targetFps instead of the screen's refresh rate.
                val now = System.currentTimeMillis()
                val minIntervalMillis = if (targetFps > 0) 1000L / targetFps else 0L
                if (now - lastFrameAtMillis < minIntervalMillis) return@setOnImageAvailableListener
                lastFrameAtMillis = now

                val jpeg = imageToJpeg(image, targetQuality)
                ScreenCaptureBridge.onFrame(jpeg)
            } finally {
                image.close()
            }
        }, null)
    }

    private fun imageToJpeg(image: android.media.Image, quality: Int): ByteArray {
        val plane = image.planes[0]
        val buffer = plane.buffer
        val pixelStride = plane.pixelStride
        val rowStride = plane.rowStride
        val rowPadding = rowStride - pixelStride * image.width

        val bitmap = Bitmap.createBitmap(
            image.width + rowPadding / pixelStride,
            image.height,
            Bitmap.Config.ARGB_8888
        )
        bitmap.copyPixelsFromBuffer(buffer)

        val cropped = if (rowPadding == 0) bitmap else
            Bitmap.createBitmap(bitmap, 0, 0, image.width, image.height)

        val out = ByteArrayOutputStream()
        cropped.compress(Bitmap.CompressFormat.JPEG, quality, out)
        if (cropped !== bitmap) bitmap.recycle()
        cropped.recycle()
        return out.toByteArray()
    }

    override fun onDestroy() {
        virtualDisplay?.release()
        imageReader?.close()
        mediaProjection?.stop()
        super.onDestroy()
    }
}
