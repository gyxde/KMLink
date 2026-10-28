"""Exercise all invitation directions against the Java protocol implementation.

Run after Gradle :protocol:testClasses. Pass the test runtime classpath printed
by :protocol:printTestRuntimeClasspath. No accessibility hooks or input injection.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from kmlink.pairing import PairingService
from kmlink.transport import Transport
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID


def identity(path: Path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "KMLink interoperability test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
            .not_valid_after(now+timedelta(days=1)).sign(key, hashes.SHA256()))
    path.write_bytes(pkcs12.serialize_key_and_certificates(b"test", key, cert, None,
                      serialization.BestAvailableEncryption(b"testpassword")))


class Scenario:
    def __init__(self, java: str, classpath: str, keyfile: Path):
        self.events = queue.Queue()
        self.history = []
        self.errors = []
        self.control = None
        self.role = None
        self.request_id = None
        self.python = PairingService("python-probe", "Python 设备", "Python", self.python_event)
        self.python.start("127.0.0.1", 0)
        self.proc = subprocess.Popen([java, "-cp", classpath, "io.kmlink.protocol.JVMInteropProbe",
                                     str(keyfile), "testpassword", "android-probe"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8")
        threading.Thread(target=self.read_java, daemon=True).start()
        threading.Thread(target=self.read_errors, daemon=True).start()

    def read_java(self):
        for line in self.proc.stdout:
            try: self.events.put(("java", json.loads(line)))
            except ValueError: self.errors.append(line.strip())

    def read_errors(self):
        for line in self.proc.stderr:
            self.errors.append(line.strip())
            self.errors[:] = self.errors[-20:]

    def command(self, **data):
        self.proc.stdin.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def python_event(self, kind, data):
        self.events.put(("python", {"event": kind, **data}))
        if kind in {"prepare_receiver", "connect_controller"}:
            self.request_id = data["request_id"]
            self.role = "receiver" if kind == "prepare_receiver" else "controller"
            self.control = Transport(lambda m: self.events.put(("python", {"event": "message", "message": m})), self.status)
            if self.role == "receiver": self.control.listen("127.0.0.1", 0, data["key"])
            else: self.control.connect(data["host"], data["port"], data["key"])

    def status(self, kind, detail):
        if kind == "listening": self.python.receiver_ready(self.request_id, self.control.listening_address[1])
        elif kind == "connected":
            self.events.put(("python", {"event": "connected", "role": self.role}))
            assert self.control.send({"type": "hello", "version": 1, "role": self.role,
                                     "name": "Python 设备", "platform": "Python",
                                     "bounds": {"x": 0, "y": 0, "width": 1920, "height": 1080}})
        elif kind in {"error", "disconnected"}: self.events.put(("python", {"event": kind, "detail": detail}))

    def wait(self, side, event, timeout=8, predicate=lambda value: True):
        stop = time.monotonic()+timeout
        while True:
            for owner, value in self.history:
                if owner == side and value.get("event") == event and predicate(value): return value
            remaining = stop-time.monotonic()
            if remaining <= 0: raise AssertionError(f"Missing {side}:{event}; events={self.history}; stderr={self.errors}")
            try: self.history.append(self.events.get(timeout=remaining))
            except queue.Empty: pass

    def run(self, initiator, mode):
        ready = self.wait("java", "ready")
        until = time.monotonic()+3
        while not self.python.port and time.monotonic()<until: time.sleep(.01)
        assert self.python.port
        if initiator == "python":
            self.python.request("android-probe", "127.0.0.1", ready["port"], mode)
            local = self.wait("python", "outgoing")
            remote = self.wait("java", "pairing", predicate=lambda e:e["incoming"])
        else:
            self.command(cmd="request", id="python-probe", host="127.0.0.1", port=self.python.port, mode=mode)
            local = self.wait("python", "incoming")
            remote = self.wait("java", "pairing", predicate=lambda e:not e["incoming"])
        assert local["code"] == remote["code"], (local, remote)
        assert self.control is None, "Control transport started before recipient approval"
        if initiator == "python": self.command(cmd="approve", id=remote["id"])
        else: self.python.approve(local["request_id"])
        pc = self.wait("python", "connected")
        jc = self.wait("java", "connected")
        expected = "controller" if (initiator == "python") == (mode == "control") else "receiver"
        assert pc["role"] == expected and jc["role"] != expected
        self.wait("python", "message", predicate=lambda e:e["message"].get("type")=="hello")
        self.wait("java", "message", predicate=lambda e:e["message"].get("type")=="hello")
        message = {"type": "text", "text": "双向互通 🚀", "sequence": 7}
        assert self.control.send(message)
        received = self.wait("java", "message", predicate=lambda e:e["message"].get("sequence")==7)
        assert received["message"] == message
        self.command(cmd="send", message={"type": "move", "dx": 3, "dy": -2, "sequence": 8})
        received = self.wait("python", "message", predicate=lambda e:e["message"].get("sequence")==8)
        assert received["message"]["dx"] == 3 and received["message"]["dy"] == -2
        self.command(cmd="disconnect")
        self.wait("python", "disconnected")
        print(f"PASS {initiator} initiates {mode}: SAS, consent, TLS, Unicode, bidirectional frames, disconnect")

    def close(self):
        if self.control: self.control.stop()
        self.python.stop()
        if self.proc.poll() is None:
            try: self.command(cmd="stop")
            except (BrokenPipeError, OSError): pass
            try: self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--java", default="java")
    parser.add_argument("--classpath", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="kmlink-interop-") as directory:
        fixture = Path(directory)/"identity.p12"
        identity(fixture)
        for initiator in ("python", "java"):
            for mode in ("control", "be_controlled"):
                scenario = Scenario(args.java, args.classpath, fixture)
                try: scenario.run(initiator, mode)
                finally: scenario.close()


if __name__ == "__main__": main()
