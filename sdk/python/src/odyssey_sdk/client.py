"""`OdysseySDK` — the hand-written half of this package. `resources/*.py`
(generated from `services/api/openapi.json`) only need a `._get(path) ->
dict|list` call; every HTTP concern (auth, error mapping, JSON decode)
lives here, once.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

from odyssey_schemas import HealthOut

from odyssey_sdk.errors import raise_for_status
from odyssey_sdk.resources.datasets import DatasetsResource
from odyssey_sdk.resources.exports import ExportsResource
from odyssey_sdk.resources.journeys import JourneysResource
from odyssey_sdk.resources.metrics import MetricsResource
from odyssey_sdk.resources.models import ModelsResource
from odyssey_sdk.resources.products import ProductsResource
from odyssey_sdk.resources.runs import RunsResource

__all__ = ["OdysseySDK", "Transport"]


class Transport:
    """stdlib `urllib` only — no HTTP framework dependency for a client
    meant to be installed by someone who has only network access to a
    deployed `services/api`, not this monorepo. Mirrors
    `odyssey.sinks.HttpSink`'s own stdlib-only choice."""

    def __init__(self, base_url: str, api_key: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request) as resp:
                body = resp.read().decode("utf-8")
                raise_for_status(resp.status, body, path)
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise_for_status(exc.code, body, path)
            raise  # unreachable — raise_for_status always raises on error status


class OdysseySDK:
    """The client. `client.journeys.list()` / `client.journeys.get(id)`,
    `client.datasets`/`client.models`/`client.runs`/`client.exports`/
    `client.products` mirror the same shape; `client.health()` is the one
    endpoint outside the resource pattern (nothing to paginate or fetch
    by id)."""

    def __init__(self, base_url: str, api_key: Optional[str] = None) -> None:
        if api_key is None:
            api_key = os.environ.get("ODYSSEY_API_AUTH_KEY")
        self._transport = Transport(base_url, api_key)
        self.journeys = JourneysResource(self._transport)
        self.datasets = DatasetsResource(self._transport)
        self.models = ModelsResource(self._transport)
        self.runs = RunsResource(self._transport)
        self.exports = ExportsResource(self._transport)
        self.metrics = MetricsResource(self._transport)
        self.products = ProductsResource(self._transport)

    def health(self) -> HealthOut:
        return HealthOut.model_validate(self._transport.get("/health"))
