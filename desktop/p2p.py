"""
RemoteBridge P2P & STUN Module (Phase 15)

Provides STUN client probes for public IP/port discovery and UDP hole punching
to establish direct peer-to-peer transport between host and viewer endpoints.
"""

import socket
import struct
import random
import time
import logging

logger = logging.getLogger("p2p")

# Public STUN Servers
DEFAULT_STUN_SERVERS = [
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
    ("stun2.l.google.com", 19302),
]

def get_public_endpoint(stun_host=None, stun_port=None, timeout=3.0) -> tuple:
    """Probes a STUN server to discover the local socket's public IP and mapped port.
    Returns (public_ip, public_port) or (None, None) on failure."""
    if not stun_host:
        server = random.choice(DEFAULT_STUN_SERVERS)
        stun_host, stun_port = server

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)

    try:
        # Build STUN Binding Request (20 bytes header)
        # Message Type: 0x0001 (Binding Request)
        # Message Length: 0x0000
        # Magic Cookie: 0x2112A442
        # Transaction ID: 12 random bytes
        tx_id = bytes([random.randint(0, 255) for _ in range(12)])
        request = struct.pack("!HH4s12s", 0x0001, 0, bytes.fromhex("2112A442"), tx_id)

        server_ip = socket.gethostbyname(stun_host)
        sock.sendto(request, (server_ip, stun_port))

        data, _ = sock.recvfrom(2048)
        if len(data) < 20:
            return None, None

        msg_type, msg_len, magic, resp_tx_id = struct.unpack("!HH4s12s", data[:20])
        if resp_tx_id != tx_id:
            return None, None

        # Parse attributes
        pos = 20
        while pos < 20 + msg_len and pos + 4 <= len(data):
            attr_type, attr_len = struct.unpack("!HH", data[pos:pos+4])
            pos += 4
            attr_data = data[pos:pos+attr_len]
            pos += attr_len + (4 - (attr_len % 4)) if attr_len % 4 != 0 else attr_len

            # XOR-MAPPED-ADDRESS (0x0020)
            if attr_type == 0x0020 and len(attr_data) >= 8:
                family = attr_data[1]
                if family == 0x01: # IPv4
                    x_port = struct.unpack("!H", attr_data[2:4])[0]
                    port = x_port ^ 0x2112
                    x_ip = struct.unpack("!I", attr_data[4:8])[0]
                    ip_int = x_ip ^ 0x2112A442
                    ip = socket.inet_ntoa(struct.pack("!I", ip_int))
                    return ip, port
            # MAPPED-ADDRESS (0x0001)
            elif attr_type == 0x0001 and len(attr_data) >= 8:
                family = attr_data[1]
                if family == 0x01: # IPv4
                    port = struct.unpack("!H", attr_data[2:4])[0]
                    ip = socket.inet_ntoa(attr_data[4:8])
                    return ip, port

    except Exception as err:
        logger.debug(f"STUN probe failed: {err}")
    finally:
        sock.close()

    return None, None


class UDPPuncher:
    """Manages UDP hole punching synchronization between host and viewer."""
    def __init__(self, local_port=0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", local_port))
        self.local_port = self.sock.getsockname()[1]

    def punch_hole(self, remote_ip: str, remote_port: int, secret_token: str, timeout=5.0) -> bool:
        """Sends UDP keepalive packets to remote_ip:remote_port to punch through NAT routers."""
        self.sock.settimeout(0.2)
        end_time = time.time() + timeout
        token_bytes = secret_token.encode("utf-8")

        while time.time() < end_time:
            try:
                self.sock.sendto(b"PING:" + token_bytes, (remote_ip, remote_port))
                data, addr = self.sock.recvfrom(1024)
                if data == b"PONG:" + token_bytes or data == b"PING:" + token_bytes:
                    self.sock.sendto(b"PONG:" + token_bytes, (remote_ip, remote_port))
                    return True
            except socket.timeout:
                pass
            except Exception:
                break
            time.sleep(0.05)

        return False

    def close(self):
        self.sock.close()
