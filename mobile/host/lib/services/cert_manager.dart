/// Mirrors `desktop/generate_cert.sh`'s job - the host needs a TLS
/// certificate and private key before it can accept a video/control
/// connection - but a phone app can't shell out to openssl, so this
/// generates a self-signed RSA cert+key with `package:basic_utils` the
/// first time the app hosts, and caches both PEMs in the app's documents
/// directory (`host_cert.pem`/`host_key.pem`, same file names as the
/// desktop side) so every later session reuses the same identity instead
/// of minting a new one per run.
///
/// Trust model note: exactly like `desktop/viewer.py` (see its README),
/// nothing on the viewer side pins this certificate's fingerprint yet -
/// `wire_socket.dart`'s client-side `connectTls`/`connectRelayTls` trust
/// whatever cert the far end presents. Self-signed + unpinned means the
/// TLS here defeats casual eavesdropping on the wire but not a
/// man-in-the-middle who can intercept the very first connection -
/// unchanged from the desktop app's own documented pre-hardening state.
library cert_manager;

import 'dart:io';
import 'dart:typed_data';

import 'package:basic_utils/basic_utils.dart';
import 'package:path_provider/path_provider.dart';

class HostCertificate {
  final Uint8List certPem;
  final Uint8List keyPem;
  const HostCertificate(this.certPem, this.keyPem);
}

class CertManager {
  static const _certFile = 'host_cert.pem';
  static const _keyFile = 'host_key.pem';

  /// Loads the cached cert/key if present, otherwise generates a new
  /// self-signed pair and writes it to disk before returning it.
  Future<HostCertificate> loadOrCreate() async {
    final dir = await getApplicationDocumentsDirectory();
    final certPath = '${dir.path}/$_certFile';
    final keyPath = '${dir.path}/$_keyFile';
    final certFile = File(certPath);
    final keyFile = File(keyPath);

    if (await certFile.exists() && await keyFile.exists()) {
      return HostCertificate(await certFile.readAsBytes(), await keyFile.readAsBytes());
    }

    final pair = CryptoUtils.generateRSAKeyPair(keySize: 2048);
    final privateKey = pair.privateKey as RSAPrivateKey;
    final publicKey = pair.publicKey as RSAPublicKey;

    // generateSelfSignedCertificate signs a CSR with its own subject's
    // key rather than taking subject fields directly, so build a throwaway
    // CSR first (never sent anywhere - `generateRsaCsrPem` just gives us a
    // conveniently-encoded subject+public-key blob to self-sign).
    final csrPem = X509Utils.generateRsaCsrPem(
      {'CN': 'remotebridge-mobile-host'},
      privateKey,
      publicKey,
    );

    // Self-signed - issuer == subject. Validity matches
    // generate_cert.sh's 825-day window (the same span macOS/iOS impose
    // as a maximum for trusted certs, kept here even though this cert is
    // never submitted for that kind of trust).
    final certPem = X509Utils.generateSelfSignedCertificate(privateKey, csrPem, 825);
    final keyPem = CryptoUtils.encodeRSAPrivateKeyToPem(privateKey);

    await certFile.writeAsString(certPem);
    await keyFile.writeAsString(keyPem);
    return HostCertificate(
      Uint8List.fromList(certPem.codeUnits),
      Uint8List.fromList(keyPem.codeUnits),
    );
  }

  /// Builds the `SecurityContext` `wire_socket.dart`'s
  /// `listenDirectTls`/`registerRelayTls` need, from a loaded/generated
  /// [HostCertificate].
  Future<SecurityContext> buildContext(HostCertificate cert) async {
    final context = SecurityContext();
    context.useCertificateChainBytes(cert.certPem);
    context.usePrivateKeyBytes(cert.keyPem);
    return context;
  }
}
