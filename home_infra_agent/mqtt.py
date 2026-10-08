"""Home Assistant MQTT Discovery output adapter."""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

log = logging.getLogger(__name__)


def state_payload(job_id: str, result: Any) -> str:
    data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    return json.dumps(data, separators=(",", ":"), default=str)


def discovery_configs(job: Any, result: Any, prefix: str = "homeassistant") -> list[tuple[str, str]]:
    mqtt = job.config.get("mqtt", {}) if job.valid else {}
    topic = mqtt.get("topic", f"home/{job.id}").rstrip("/") + "/state"
    device = mqtt.get("device", {})
    identifier = f"home_infra_agent_{job.id}"
    entities = [("sensor", "status", "Status", "status"), ("sensor", "last_run", "Last run", "timestamp"),
                ("sensor", "duration_ms", "Duration", "durationMs")]
    values = result.values if result else {}
    for key, value in values.items():
        key_str = str(key)
        slug = re.sub(r"[^a-zA-Z0-9_]", "_", key_str)
        component = "binary_sensor" if value in ("UP", "DOWN") else "sensor"
        entities.append((component, slug, key_str, f'values["{key_str}"]'))
    configs = []
    for component, key, name, template_path in entities:
        uid = f"{identifier}_{key}"
        config = {"name": name, "unique_id": uid, "state_topic": topic,
                  "value_template": "{{ value_json." + template_path + " }}",
                  "availability_topic": "home-infra-agent/availability",
                  "device": {"identifiers": [identifier], "name": device.get("name", job.name),
                             "manufacturer": device.get("manufacturer", "Custom"),
                             "model": device.get("model", job.name)}}
        if component == "binary_sensor":
            config.update(payload_on="UP", payload_off="DOWN")
        if key == "duration_ms":
            config["unit_of_measurement"] = "ms"
        configs.append((f"{prefix}/{component}/{uid}/config", json.dumps(config, separators=(",", ":"))))
    return configs


class MqttAdapter:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.client = None
        self.connected = threading.Event()
        self._jobs: dict[str, tuple[Any, Any]] = {}

    @property
    def is_connected(self) -> bool:
        return self.connected.is_set()

    def start(self) -> None:
        if not self.config.get("enabled", False):
            return
        import paho.mqtt.client as mqtt
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="home-infra-agent")
        username = self.config.get("username")
        if username:
            client.username_pw_set(username, self.config.get("password"))
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.will_set("home-infra-agent/availability", "offline", qos=1, retain=True)
        self.client = client
        client.connect_async(self.config.get("host", "localhost"), int(self.config.get("port", 1883)), 60)
        client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self.connected.set()
        log.info("connected to MQTT broker")
        client.publish("home-infra-agent/availability", "online", qos=1, retain=True)
        for job, result in self._jobs.values():
            self.publish(job, result)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.connected.clear()
        log.warning("disconnected from MQTT broker")

    def publish(self, job: Any, result: Any) -> None:
        self._jobs[job.id] = (job, result)
        if not self.client or not self.connected.is_set():
            return
        cfg = job.config.get("mqtt", {})
        if cfg.get("enabled", True) is False:
            return
        prefix = self.config.get("discoveryPrefix", "homeassistant")
        for topic, payload in discovery_configs(job, result, prefix):
            self.client.publish(topic, payload, qos=1, retain=True)
        state_topic = cfg.get("topic", f"home/{job.id}").rstrip("/") + "/state"
        self.client.publish("home-infra-agent/availability", "online", qos=1, retain=True)
        self.client.publish(state_topic, state_payload(job.id, result), qos=1, retain=True)

    def stop(self) -> None:
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
