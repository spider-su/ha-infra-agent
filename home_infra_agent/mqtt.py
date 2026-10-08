"""Home Assistant MQTT Discovery output adapter."""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)
AVAILABILITY_TOPIC = "home-infra-agent/availability"
_RESERVED_IDS = {"status", "last_run", "duration_ms", "last_success", "freshness"}


def _job_max_age(job: Any) -> int | None:
    value = job.config.get("freshness", {}).get("maxAge")
    if value is None:
        return None
    text = str(value).strip().lower()
    try:
        multiplier = 1 if text.endswith("s") else 60 if text.endswith("m") else 3600 if text.endswith("h") else 1
        amount = int(text[:-1] if text[-1:] in "smh" else text)
        return max(1, amount * multiplier)
    except (ValueError, IndexError):
        return None


def _result_data(result: Any) -> dict[str, Any]:
    return result.to_dict() if hasattr(result, "to_dict") else dict(result)


def _age_seconds(timestamp: str | None) -> int | None:
    if not timestamp:
        return None
    try:
        return max(0, int((datetime.now().astimezone() - datetime.fromisoformat(timestamp).astimezone()).total_seconds()))
    except (TypeError, ValueError):
        return None


def state_payload(job_id: str, result: Any, max_age: int | None = None,
                  known_keys: set[str] | None = None) -> str:
    data = _result_data(result)
    values = dict(data.get("values") or {})
    if known_keys:
        for key in known_keys:
            values.setdefault(key, None)
    age = _age_seconds(data.get("lastSuccess"))
    freshness = "UNKNOWN" if age is None or max_age is None else "STALE" if age > max_age else "FRESH"
    data["values"] = values
    data["freshness"] = {"status": freshness, "ageSeconds": age, "maxAgeSeconds": max_age}
    return json.dumps(data, separators=(",", ":"), default=str)


def discovery_configs(job: Any, result: Any, prefix: str = "homeassistant") -> list[tuple[str, str]]:
    mqtt = job.config.get("mqtt", {}) if job.valid else {}
    topic = mqtt.get("topic", f"home/{job.id}").rstrip("/") + "/state"
    device = mqtt.get("device", {})
    identifier = f"home_infra_agent_{job.id}"
    entities = [("sensor", "status", "Status", "status", {}), ("sensor", "last_run", "Last run", "timestamp", {}),
                ("sensor", "duration_ms", "Duration", "durationMs", {}),
                ("sensor", "last_success", "Last success", "lastSuccess", {}),
                ("sensor", "freshness", "Result freshness", "freshness.status", {})]
    values = result.values if result else {}
    metadata = mqtt.get("entities", {})
    metadata_fields = {"name", "unit_of_measurement", "device_class", "state_class", "expire_after", "icon"}
    max_age = _job_max_age(job)
    for key, value in values.items():
        key_str = str(key)
        slug = re.sub(r"[^a-zA-Z0-9_]", "_", key_str)
        entity_metadata = metadata.get(key_str, {}) if isinstance(metadata, dict) else {}
        entity_metadata = entity_metadata if isinstance(entity_metadata, dict) else {}
        component = entity_metadata.get("component", "binary_sensor" if value in ("UP", "DOWN") else "sensor")
        entities.append((component, slug, key_str, key_str, entity_metadata))
    configs = []
    seen = set()
    for component, key, name, template_path, entity_metadata in entities:
        if key in _RESERVED_IDS and template_path not in {"status", "timestamp", "durationMs", "lastSuccess", "freshness.status"}:
            log.warning("skipping MQTT value that conflicts with built-in entity %s: %s", job.id, key)
            continue
        if key in seen:
            log.warning("skipping duplicate MQTT entity identifier for job %s: %s", job.id, key)
            continue
        seen.add(key)
        uid = f"{identifier}_{key}"
        if template_path in {"status", "timestamp", "durationMs", "lastSuccess"}:
            template = "{{ value_json." + template_path + " }}"
        elif template_path == "freshness.status":
            template = "{{ value_json.freshness.status }}"
        else:
            template = "{{ value_json[\"values\"][\"" + template_path + "\"] }}"
        config = {"name": entity_metadata.get("name", name), "unique_id": uid, "state_topic": topic,
                  "value_template": template,
                  "availability_topic": AVAILABILITY_TOPIC,
                  "device": {"identifiers": [identifier], "name": device.get("name", job.name),
                             "manufacturer": device.get("manufacturer", "Custom"),
                             "model": device.get("model", job.name)}}
        if component == "binary_sensor":
            config.update(payload_on=entity_metadata.get("payload_on", "UP"),
                          payload_off=entity_metadata.get("payload_off", "DOWN"))
        if key == "duration_ms":
            config["unit_of_measurement"] = "ms"
        config.update({field: value for field, value in entity_metadata.items()
                       if field in metadata_fields and value is not None})
        unit_field = entity_metadata.get("unit_of_measurement_field")
        if unit_field and values.get(unit_field) is not None:
            config["unit_of_measurement"] = str(values[unit_field])
        if max_age:
            config.setdefault("expire_after", max_age)
        configs.append((f"{prefix}/{component}/{uid}/config", json.dumps(config, separators=(",", ":"))))
    return configs


class MqttAdapter:
    """Serialize broker IO on one worker; keep only the newest pending state per Job."""
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.client = None
        self.connected = threading.Event()
        self._jobs: dict[str, tuple[Any, Any]] = {}
        self._known_keys: dict[str, set[str]] = {}
        self._published_discovery: dict[str, str] = {}
        self._pending: dict[str, bool] = {}
        self._condition = threading.Condition()
        self._stopping = False
        self._worker: threading.Thread | None = None

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
        client.will_set(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
        self.client = client
        self._worker = threading.Thread(target=self._publish_loop, name="mqtt-publisher", daemon=True)
        self._worker.start()
        client.connect_async(self.config.get("host", "localhost"), int(self.config.get("port", 1883)), 60)
        client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if getattr(reason_code, "is_failure", False):
            log.error("MQTT connection rejected: %s", reason_code)
            return
        with self._condition:
            if self._stopping:
                return
        self.connected.set()
        log.info("connected to MQTT broker")
        client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)
        with self._condition:
            for job_id in self._jobs:
                self._pending[job_id] = True
            self._condition.notify_all()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.connected.clear()
        log.warning("disconnected from MQTT broker")

    def publish(self, job: Any, result: Any) -> None:
        with self._condition:
            self._jobs[job.id] = (job, result)
            values = getattr(result, "values", {}) or {}
            self._known_keys.setdefault(job.id, set()).update(str(key) for key in values)
            if job.config.get("mqtt", {}).get("enabled", True) is False:
                return
            self._pending[job.id] = self._pending.get(job.id, False)
            self._condition.notify()

    def _publish_loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopping or (self.connected.is_set() and self._pending))
                if self._stopping:
                    return
                job_id = next(iter(self._pending))
                force_discovery = self._pending.pop(job_id)
                job, result = self._jobs[job_id]
            try:
                self._publish_one(job, result, force_discovery)
            except Exception:
                log.exception("MQTT publish failed for job %s", job_id)

    def _publish_one(self, job: Any, result: Any, force_discovery: bool = False) -> None:
        client = self.client
        if not client or not self.connected.is_set():
            return
        cfg = job.config.get("mqtt", {})
        if cfg.get("enabled", True) is False:
            return
        prefix = self.config.get("discoveryPrefix", "homeassistant")
        for topic, payload in discovery_configs(job, result, prefix):
            if force_discovery or self._published_discovery.get(topic) != payload:
                client.publish(topic, payload, qos=1, retain=True)
                self._published_discovery[topic] = payload
        state_topic = cfg.get("topic", f"home/{job.id}").rstrip("/") + "/state"
        payload = state_payload(job.id, result, _job_max_age(job), self._known_keys.get(job.id))
        client.publish(state_topic, payload, qos=1, retain=True)

    def stop(self) -> None:
        client = self.client
        with self._condition:
            self._stopping = True
            self._pending.clear()
            self._condition.notify_all()
        if client and self.connected.is_set():
            try:
                info = client.publish(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
                if hasattr(info, "wait_for_publish"):
                    info.wait_for_publish(timeout=2)
            except Exception:
                log.debug("unable to publish clean MQTT offline state", exc_info=True)
        self.connected.clear()
        if self._worker:
            self._worker.join(timeout=2)
        if client:
            client.loop_stop()
            client.disconnect()
