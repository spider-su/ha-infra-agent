"""Built-in source providers and their small static registry."""
from .http import HttpProvider
from .investory import InvestoryPostgresProvider
from .kubernetes import KubernetesProvider
from .ping import PingProvider
from .solarman import SolarmanProvider

PROVIDERS = {
    "ping": PingProvider(),
    "http": HttpProvider(),
    "kubernetes": KubernetesProvider(),
    "investory_postgres": InvestoryPostgresProvider(),
    "solarman": SolarmanProvider(),
}

__all__ = ["PROVIDERS", "PingProvider", "HttpProvider", "KubernetesProvider",
           "InvestoryPostgresProvider", "SolarmanProvider"]
