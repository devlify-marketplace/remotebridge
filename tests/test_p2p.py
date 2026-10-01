import unittest
import socket
import threading
import time
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "desktop"))

import p2p

class TestP2PAndSTUN(unittest.TestCase):
    def test_get_public_endpoint_fallback(self):
        # Verify fallback on invalid host returns (None, None) gracefully
        ip, port = p2p.get_public_endpoint(stun_host="127.0.0.1", stun_port=9999, timeout=0.2)
        self.assertIsNone(ip)
        self.assertIsNone(port)

    def test_udp_hole_punching_loopback(self):
        peer1 = p2p.UDPPuncher()
        peer2 = p2p.UDPPuncher()

        success1 = [False]
        success2 = [False]
        token = "test-secret-token"

        def t1():
            success1[0] = peer1.punch_hole("127.0.0.1", peer2.local_port, token, timeout=2.0)

        def t2():
            success2[0] = peer2.punch_hole("127.0.0.1", peer1.local_port, token, timeout=2.0)

        th1 = threading.Thread(target=t1)
        th2 = threading.Thread(target=t2)
        th1.start(); th2.start()
        th1.join(); th2.join()

        peer1.close()
        peer2.close()

        self.assertTrue(success1[0])
        self.assertTrue(success2[0])

if __name__ == "__main__":
    unittest.main()
