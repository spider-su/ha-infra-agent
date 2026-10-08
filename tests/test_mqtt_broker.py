"""Isolated Mosquitto integration tests; CI requires Docker, local runs may skip."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

paho = pytest.importorskip("paho.mqtt.client")


class Mosquitto:
    def __init__(self, name: str, port: int):
        self.name = name
        self.port = port

    def stop(self):
        subprocess.run(["docker", "stop", "--time", "5", self.name], check=True, capture_output=True)

    def start(self):
        subprocess.run(["docker", "start", self.name], check=True, capture_output=True)
        wait_for(lambda: socket_ready(self.port), timeout=10, message="Mosquitto restart")


@pytest.fixture(scope="module")
def broker():
    required = os.getenv("HIA_REQUIRE_MQTT_DOCKER") == "1"
    docker = shutil.which("docker")
    if not docker:
        if required:
            pytest.fail("CI requires Docker to run the isolated Mosquitto integration tests")
        pytest.skip("Docker is unavailable; Mosquitto integration tests skipped")

    name = f"hia-mqtt-test-{uuid.uuid4().hex[:10]}"
    config = Path(__file__).parent / "fixtures" / "mosquitto.conf"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        subprocess.run([
            docker, "run", "--detach", "--name", name,
            "--publish", f"127.0.0.1:{port}:1883",
            "--volume", f"{config}:/mosquitto/config/mosquitto.conf:ro",
            "eclipse-mosquitto:2",
        ], check=True, capture_output=True, text=True, timeout=120)
        wait_for(lambda: socket_ready(port), timeout=15, message="Mosquitto startup")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
        subprocess.run([docker, "rm", "--force", name], capture_output=True)
        if required:
            pytest.fail(f"Unable to start required Mosquitto test broker: {exc}")
        pytest.skip(f"Unable to start Mosquitto test broker: {exc}")

    try:
        yield Mosquitto(name, port)
    finally:
        subprocess.run([docker, "rm", "--force", name], capture_output=True, timeout=15)


def socket_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.2):
            return True
    except OSError:
        return False


def wait_for(predicate, timeout: float, message: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError(f"timed out waiting for {message}")


@dataclass(frozen=True)
class SeenMessage:
    topic: str
    payload: bytes
    retained: bool
    sequence: int


class Capture:
    def __init__(self, broker: Mosquitto, client_id: str):
        self.messages: list[SeenMessage] = []
        self.sequence = 0
        self.condition = threading.Condition()
        self.connected = threading.Event()
        self.subscribed = threading.Event()
        self.disconnected = threading.Event()
        self.client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=client_id)
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.connect("127.0.0.1", broker.port, 15)
        self.client.loop_start()
        assert self.connected.wait(5), "observer did not connect to Mosquitto"
        assert self.subscribed.wait(5), "observer did not subscribe to Mosquitto"

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if not getattr(reason_code, "is_failure", False):
            self.disconnected.clear()
            self.subscribed.clear()
            self.connected.set()
            client.subscribe("#", qos=1)

    def _on_subscribe(self, client, userdata, mid, reason_code_list, properties):
        self.subscribed.set()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.connected.clear()
        self.disconnected.set()

    def _on_message(self, client, userdata, message):
        with self.condition:
            self.sequence += 1
            seen = SeenMessage(message.topic, bytes(message.payload), message.retain, self.sequence)
            self.messages.append(seen)
            self.condition.notify_all()

    def wait_for(self, topic: str, predicate, timeout: float = 8) -> SeenMessage:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for message in reversed(self.messages):
                    if message.topic == topic and predicate(message):
                        return message
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"no matching MQTT message on {topic}; saw {len(self.messages)} messages")
                self.condition.wait(remaining)

    def wait_for_count(self, predicate, count: int, timeout: float = 8) -> None:
        deadline = time.monotonic() + timeout
        with self.condition:
            while sum(predicate(message) for message in self.messages) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out waiting for {count} matching MQTT messages")
                self.condition.wait(remaining)

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


def make_job(job_id: str, state_topic: str):
    from home_infra_agent.core import Job
    return Job(job_id, "Broker integration test", Path("."), {
        "freshness": {"maxAge": "60s"},
        "mqtt": {"topic": state_topic},
    }, {})


def make_result(value: int, job_id: str = "mqtt-test"):
    from datetime import datetime, timezone
    from home_infra_agent.core import JobResult
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return JobResult(job_id, "OK", now, 3, {"value": value}, {}, now)


def test_connection_discovery_retention_reconnect_and_latest_state(broker):
    from home_infra_agent.app import AgentServer
    from home_infra_agent.core import JobEngine
    from home_infra_agent.mqtt import MqttAdapter
    from urllib.request import urlopen

    suffix = uuid.uuid4().hex
    root = f"hia-stage4/{suffix}"
    job_id = f"broker_{suffix[:8]}"
    job_topic = f"{root}/{job_id}"
    state_topic = f"{job_topic}/state"
    discovery_prefix = f"{root}/discovery"
    job = make_job(job_id, job_topic)
    result = make_result(1, job_id)
    adapter = MqttAdapter({"enabled": True, "host": "127.0.0.1", "port": broker.port,
                            "discoveryPrefix": discovery_prefix,
                            "username": "test-user", "password": "test-password"})
    observer = Capture(broker, f"observer-{suffix[:10]}")
    engine = JobEngine([job])
    server = AgentServer(("127.0.0.1", 0), engine, adapter)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        adapter.publish(job, result)
        adapter.start()
        assert adapter.connected.wait(5), "agent did not connect to Mosquitto"
        state = observer.wait_for(state_topic, lambda m: json.loads(m.payload)["values"].get("value") == 1)
        assert json.loads(state.payload)["freshness"]["status"] == "FRESH"
        discovery_topic = f"{discovery_prefix}/sensor/home_infra_agent_{job_id}_value/config"
        discovery = observer.wait_for(discovery_topic, lambda _m: True)
        assert json.loads(discovery.payload)["unique_id"] == f"home_infra_agent_{job_id}_value"
        assert b"test-password" not in discovery.payload
        observer.wait_for("home-infra-agent/availability", lambda m: m.payload == b"online")
        initial_live = [m for m in observer.messages if not m.retained]
        # One availability + six discovery records (five built-ins and one value) + one state.
        assert len(initial_live) == 8

        # A new subscriber sees retained discovery and state, not just live publishes.
        retained_observer = Capture(broker, f"retained-{suffix[:10]}")
        retained_state = retained_observer.wait_for(
            state_topic, lambda m: json.loads(m.payload)["values"].get("value") == 1)
        retained_discovery = retained_observer.wait_for(discovery_topic, lambda _m: True)
        assert retained_state.retained and retained_discovery.retained
        retained_observer.close()

        before_restart = list(observer.messages)
        broker.stop()
        assert observer.disconnected.wait(5), "observer did not detect broker shutdown"
        wait_for(lambda: not adapter.is_connected, 5, "agent MQTT disconnect")
        with urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=2) as response:
            health = json.load(response)
        assert health["status"] == "ok" and health["mqttConnected"] is False
        assert health["loadedJobs"] == 1 and health["invalidJobs"] == 0

        adapter.publish(job, make_result(2, job_id))
        adapter.publish(job, make_result(3, job_id))
        broker.start()
        wait_for(lambda: adapter.is_connected, 10, "agent MQTT reconnect")
        assert observer.connected.wait(5), "observer did not reconnect to Mosquitto"
        assert observer.subscribed.wait(5), "observer did not resubscribe after broker restart"
        latest = observer.wait_for(
            state_topic,
            lambda m: not m.retained and json.loads(m.payload)["values"].get("value") == 3,
        )
        assert json.loads(latest.payload)["status"] == "OK"
        assert not any(m.topic == state_topic and json.loads(m.payload)["values"].get("value") == 2
                       for m in observer.messages if m not in before_restart)
        # A live, retained publish after reconnect is distinct from the old retained copy.
        try:
            republished_discovery = observer.wait_for(
                discovery_topic, lambda m: not m.retained and m not in before_restart)
        except AssertionError as exc:
            seen = [(m.payload.decode(errors="replace"), m.retained) for m in observer.messages
                    if m.topic == discovery_topic]
            raise AssertionError(f"{exc}; discovery publications: {seen}") from exc
        assert json.loads(republished_discovery.payload)["unique_id"] == f"home_infra_agent_{job_id}_value"
        observer.wait_for("home-infra-agent/availability", lambda m: m.payload == b"online")
        observer.wait_for_count(
            lambda m: not m.retained and m.topic != "home-infra-agent/availability", 14
        )
        live_after_reconnect = [m for m in observer.messages if not m.retained]
        assert len([m for m in live_after_reconnect
                    if m.topic != "home-infra-agent/availability"]) == 14
        assert len([m for m in live_after_reconnect
                    if m.topic == "home-infra-agent/availability"]) >= 2

        final_observer = Capture(broker, f"final-{suffix[:10]}")
        final_state = final_observer.wait_for(
            state_topic, lambda m: json.loads(m.payload)["values"].get("value") == 3)
        assert final_state.retained
        final_observer.close()
    finally:
        adapter.stop()
        observer.close()
        server.shutdown()
        server.server_close()
        server_thread.join(2)
        if not socket_ready(broker.port):
            broker.start()


def test_clean_shutdown_and_unexpected_process_termination_send_offline(broker):
    from home_infra_agent.mqtt import MqttAdapter

    suffix = uuid.uuid4().hex
    root = f"hia-stage4/{suffix}"
    job_id = f"death_{suffix[:8]}"
    job_topic = f"{root}/{job_id}"
    state_topic = f"{job_topic}/state"
    availability = "home-infra-agent/availability"
    observer = Capture(broker, f"death-observer-{suffix[:7]}")
    job = make_job(job_id, job_topic)
    adapter = MqttAdapter({"enabled": True, "host": "127.0.0.1", "port": broker.port,
                            "discoveryPrefix": f"{root}/discovery"})
    adapter.publish(job, make_result(1))
    adapter.start()
    assert adapter.connected.wait(5)
    before_clean = list(observer.messages)
    observer.wait_for(availability, lambda m: m.payload == b"online")
    adapter.stop()
    observer.wait_for(availability, lambda m: m.payload == b"offline" and m not in before_clean)
    assert adapter._worker is None or not adapter._worker.is_alive()

    child_config = {"enabled": True, "host": "127.0.0.1", "port": broker.port,
                    "discoveryPrefix": f"{root}/death-discovery"}
    child_job_config = {"mqtt": {"topic": state_topic}}
    script = "\n".join([
        "import threading",
        "from pathlib import Path",
        "from home_infra_agent.core import Job, JobResult",
        "from home_infra_agent.mqtt import MqttAdapter",
        "adapter = MqttAdapter(" + repr(child_config) + ")",
        "job = Job(" + repr(job_id) + ", 'Unexpected termination', Path('.'), " + repr(child_job_config) + ", {})",
        "now = '2026-10-08T00:00:00+00:00'",
        "result = JobResult(" + repr(job_id) + ", 'OK', now, 1, {'value': 9}, {}, now)",
        "adapter.publish(job, result)",
        "adapter.start()",
        "if not adapter.connected.wait(8): raise SystemExit('MQTT did not connect')",
        "print('READY', flush=True)",
        "threading.Event().wait()",
    ])
    env = dict(os.environ)
    repo = Path(__file__).parents[1]
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    before_child = list(observer.messages)
    child = subprocess.Popen([sys.executable, "-c", script], cwd=repo, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output: list[str] = []
    ready = threading.Event()

    def read_line():
        if child.stdout:
            output.append(child.stdout.readline())
            ready.set()

    reader = threading.Thread(target=read_line, daemon=True)
    reader.start()
    try:
        assert ready.wait(10), "agent child did not become ready"
        assert output and output[0].strip() == "READY", output
        try:
            observer.wait_for(availability, lambda m: m.payload == b"online" and m not in before_child)
        except AssertionError as exc:
            seen = [(m.payload.decode(errors="replace"), m.retained) for m in observer.messages
                    if m.topic == availability]
            raise AssertionError(f"{exc}; availability messages: {seen}; child output: {output}") from exc
        child.kill()  # Deliberately bypass adapter.stop() to exercise the MQTT Last Will.
        child.wait(timeout=5)
        observer.wait_for(availability, lambda m: m.payload == b"offline" and m not in before_child, timeout=8)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        observer.close()



def test_mosquitto_restart_preserves_retained_discovery_and_state(broker):
    from home_infra_agent.mqtt import MqttAdapter

    suffix = uuid.uuid4().hex
    root = f"hia-stage4/{suffix}"
    job_id = f"persist_{suffix[:8]}"
    job_topic = f"{root}/{job_id}"
    state_topic = f"{job_topic}/state"
    discovery_prefix = f"{root}/discovery"
    job = make_job(job_id, job_topic)
    observer = Capture(broker, f"persist-writer-{suffix[:8]}")
    adapter = MqttAdapter({"enabled": True, "host": "127.0.0.1", "port": broker.port,
                            "discoveryPrefix": discovery_prefix})
    adapter.publish(job, make_result(4, job_id))
    adapter.start()
    try:
        assert adapter.connected.wait(5)
        observer.wait_for(state_topic, lambda _m: True)
        discovery_topic = f"{discovery_prefix}/sensor/home_infra_agent_{job_id}_value/config"
        observer.wait_for(discovery_topic, lambda _m: True)
        adapter.stop()
        broker.stop()
        broker.start()

        restarted = Capture(broker, f"persist-reader-{suffix[:8]}")
        try:
            retained_state = restarted.wait_for(
                state_topic, lambda m: json.loads(m.payload)["values"].get("value") == 4)
            retained_discovery = restarted.wait_for(discovery_topic, lambda _m: True)
            assert retained_state.retained and retained_discovery.retained
        finally:
            restarted.close()
    finally:
        adapter.stop()
        observer.close()
        if not socket_ready(broker.port):
            broker.start()
