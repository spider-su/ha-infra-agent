"""Built-in source providers and their small static registry."""
from .http import HttpProvider
from .ha_meter import HomeAssistantMeterProvider
from .investory import InvestoryPostgresProvider
from .kubernetes import KubernetesProvider
from .ping import PingProvider, PresenceProvider
from .solarman import SolarmanProvider
from .speedtest import SpeedtestProvider

PROVIDERS = {
    "ping": PingProvider(),
    "http": HttpProvider(),
    "home_assistant_meter": HomeAssistantMeterProvider(),
    "kubernetes": KubernetesProvider(),
    "investory_postgres": InvestoryPostgresProvider(),
    "solarman": SolarmanProvider(),
    "speedtest": SpeedtestProvider(),
    "presence": PresenceProvider(),
}

__all__ = ["PROVIDERS", "PingProvider", "HttpProvider", "HomeAssistantMeterProvider", "KubernetesProvider",
           "InvestoryPostgresProvider", "SolarmanProvider", "SpeedtestProvider", "PresenceProvider"]
