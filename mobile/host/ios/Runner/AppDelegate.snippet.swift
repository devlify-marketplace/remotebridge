// Additions to merge into the `flutter create .`-generated
// ios/Runner/AppDelegate.swift. The generated file already calls
// GeneratedPluginRegistrant.register(with: self) inside
// application(_:didFinishLaunchingWithOptions:) - add the two lines
// below right after that call, and the import at the top.

// import UIKit
// import Flutter
//
// @UIApplicationMain
// @objc class AppDelegate: FlutterAppDelegate {
//   override func application(
//     _ application: UIApplication,
//     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
//   ) -> Bool {
//     GeneratedPluginRegistrant.register(with: self)
//
//     // --- Phase 6 addition: register the ReplayKit screen-capture bridge ---
//     if let registrar = self.registrar(forPlugin: "ScreenCaptureBridge") {
//       ScreenCaptureBridge().register(with: registrar)
//     }
//     // ------------------------------------------------------------------
//
//     return super.application(application, didFinishLaunchingWithOptions: launchOptions)
//   }
// }
