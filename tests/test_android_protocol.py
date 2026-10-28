"""Real Python/Java wire interoperability; enabled by Android CI classpath."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from kmlink.pairing import PairingService
from kmlink.transport import Transport


@unittest.skipUnless(os.environ.get("KMLINK_JAVA_CLASSPATH"), "Android JVM classpath not configured")
class AndroidInteropTests(unittest.TestCase):
    def exercise(self, requester, mode):
        events, lines, errors, received = [], queue.Queue(), [], []
        state = {"transport": None, "connected": False, "role": None}
        temporary = tempfile.TemporaryDirectory(prefix="kmlink-interop-")
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "KMLink ephemeral test only")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                       .public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                       .sign(key, hashes.SHA256()))
        identity = Path(temporary.name) / "test-only.p12"
        identity.write_bytes(pkcs12.serialize_key_and_certificates(
            b"test", key, certificate, None, serialization.BestAvailableEncryption(b"test-password")))
        java = str(Path(os.environ["JAVA_HOME"]) / "bin" / ("java.exe" if os.name == "nt" else "java")) if os.environ.get("JAVA_HOME") else "java"
        java_id = "java-" + uuid.uuid4().hex
        process = subprocess.Popen([java, "-cp", os.environ["KMLINK_JAVA_CLASSPATH"],
                                    "io.kmlink.protocol.JVMInteropProbe", str(identity), "test-password", java_id],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", bufsize=1)
        def read_output():
            for line in process.stdout:
                try: lines.put(json.loads(line))
                except ValueError: errors.append(line.strip())
        def read_errors():
            for line in process.stderr:
                if len(errors) < 40: errors.append(line.strip())
        threading.Thread(target=read_output, daemon=True).start()
        threading.Thread(target=read_errors, daemon=True).start()
        seen = []
        def send(value):
            process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            process.stdin.flush()
        def wait(predicate, timeout=8):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                try: seen.append(lines.get(timeout=.02))
                except queue.Empty: pass
                if predicate(): return
                if process.poll() is not None:
                    self.fail(f"Java probe exited {process.returncode}: {errors}")
            self.fail(f"Interop timeout: Java={seen}, Python={[x[0] for x in events]}, stderr={errors}")
        def on_status(status, detail):
            if status == "listening":
                pairing.receiver_ready(state["request_id"], state["transport"].listening_address[1])
            elif status == "connected":
                state["connected"] = True
                state["transport"].send({"type": "hello", "version": 1, "role": state["role"],
                    "name": "Python 电脑 🙂", "platform": "Windows", "capabilities": ["text_input"],
                    "bounds": {"x": 0, "y": 0, "width": 1920, "height": 1080}})
        def on_pair(event, data):
            events.append((event, data))
            if event in {"prepare_receiver", "connect_controller"}:
                state["request_id"] = data["request_id"]
                state["role"] = "receiver" if event == "prepare_receiver" else "controller"
                transport = Transport(received.append, on_status)
                state["transport"] = transport
                if event == "prepare_receiver": transport.listen("127.0.0.1", 0, data["key"])
                else: transport.connect(data["host"], data["port"], data["key"])
        pairing = PairingService("python-" + uuid.uuid4().hex, "Python 电脑 🙂", "Windows", on_pair)
        try:
            pairing.start("127.0.0.1", 0)
            wait(lambda: pairing.port and any(x.get("event") == "ready" for x in seen))
            ready = next(x for x in seen if x.get("event") == "ready")
            if requester == "python": pairing.request(java_id, "127.0.0.1", ready["port"], mode)
            else: send({"cmd": "request", "id": pairing.identity["id"], "host": "127.0.0.1", "port": pairing.port, "mode": mode})
            wait(lambda: any(kind in {"incoming", "outgoing"} for kind, _ in events) and any(x.get("event") == "pairing" for x in seen))
            py_request = next(data for kind, data in events if kind in {"incoming", "outgoing"})
            java_request = next(x for x in seen if x.get("event") == "pairing")
            self.assertEqual(py_request["code"], java_request["code"])
            self.assertIsNone(state["transport"], "Control connection must wait for consent")
            if requester == "python": send({"cmd": "approve", "id": java_request["id"]})
            else: pairing.approve(py_request["request_id"])
            wait(lambda: state["connected"] and any(x.get("event") == "connected" for x in seen)
                 and any(x.get("type") == "hello" for x in received))
            expected = "controller" if (requester == "python") == (mode == "control") else "receiver"
            self.assertEqual(state["role"], expected)
            self.assertTrue(state["transport"].send({"type": "move", "dx": 7, "dy": -2}))
            send({"cmd": "send", "message": {"type": "text", "text": "中文 🙂\nAndroid"}})
            wait(lambda: any(x.get("event") == "message" and x.get("message", {}).get("type") == "move" for x in seen)
                 and any(x.get("text") == "中文 🙂\nAndroid" for x in received))
            self.assertEqual(next(x["message"] for x in seen if x.get("event") == "message" and x.get("message", {}).get("type") == "move"),
                             {"type": "move", "dx": 7, "dy": -2})
        finally:
            pairing.stop()
            if state["transport"]: state["transport"].stop()
            try: send({"cmd": "stop"})
            except (OSError, ValueError): pass
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream: stream.close()
            temporary.cleanup()

    def test_python_requests_control_of_java(self): self.exercise("python", "control")
    def test_python_invites_java_to_control(self): self.exercise("python", "be_controlled")
    def test_java_requests_control_of_python(self): self.exercise("java", "control")
    def test_java_invites_python_to_control(self): self.exercise("java", "be_controlled")


if __name__ == "__main__": unittest.main()
