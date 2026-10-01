#!/bin/bash
# Generates a self-signed TLS certificate for the host to use.
#
# This is Phase 1 - the viewer does NOT yet verify this certificate
# against anything (it trusts whatever cert the host presents), so
# this protects against passive eavesdropping on the wire but not yet
# against an active man-in-the-middle. Real certificate pinning /
# trust-on-first-use fingerprint checking is planned for Phase 2.
set -e
cd "$(dirname "$0")"

openssl req -x509 -newkey rsa:2048 \
  -keyout host_key.pem \
  -out host_cert.pem \
  -days 365 -nodes \
  -subj "/CN=remote-desktop-host"

echo "Generated host_cert.pem and host_key.pem in $(pwd)"
