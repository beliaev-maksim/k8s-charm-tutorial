#!/usr/bin/env python3
# Copyright 2022 Ubuntu
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""
import json
import logging
from typing import Any

import requests
from charms.data_platform_libs.v0.database_requires import DatabaseCreatedEvent, DatabaseRequires
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.loki_k8s.v0.loki_push_api import LogProxyConsumer
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.pebble import Layer

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

PEER_NAME = "fastapi-peer"


class FastAPIDemoCharm(CharmBase):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(*args)
        self.pebble_service_name = "fastapi-service"
        self.container = self.unit.get_container(
            "demo-server"
        )  # see 'containers' in metadata.yaml

        # Provide ability for prometheus to be scraped by Prometheus using prometheus_scrape
        self._prometheus_scraping = MetricsEndpointProvider(
            self,
            relation_name="metrics-endpoint",
            jobs=[{"static_configs": [{"targets": [f"*:{self.config['server-port']}"]}]}],
            refresh_event=self.on.config_changed,
        )

        # Enable log forwarding for Loki and other charms that implement loki_push_api
        self._logging = LogProxyConsumer(
            self, relation_name="logging", log_files=["demo_server.log"]
        )

        # Provide grafana dashboards over a relation interface
        self._grafana_dashboards = GrafanaDashboardProvider(
            self, relation_name="grafana-dashboard"
        )

        # Charm events defined in the database requires charm library.
        self.database = DatabaseRequires(self, relation_name="database", database_name="names_db")
        self.framework.observe(self.database.on.database_created, self._on_database_created)
        self.framework.observe(self.database.on.endpoints_changed, self._on_database_created)
        self.framework.observe(self.on.database_relation_joined, self._on_database_created)
        self.framework.observe(
            self.on.database_relation_broken, self._on_database_relation_removed
        )

        self.framework.observe(self.on.demo_server_pebble_ready, self._update_layer_and_restart)
        self.framework.observe(self.on.config_changed, self._on_config_changed)

        # events on custom actions that are run via 'juju run-action'
        self.framework.observe(self.on.get_db_info_action, self._on_get_db_info_action)

    @property
    def peers(self):
        """Fetch the peer relation."""
        return self.model.get_relation(PEER_NAME)

    def set_peer_data(self, key: str, data: Any) -> None:
        """Put information into the peer data bucket instead of `StoredState`."""
        self.peers.data[self.app][key] = json.dumps(data)

    def get_peer_data(self, key: str) -> dict[Any, Any]:
        """Retrieve information from the peer data bucket instead of `StoredState`."""
        if not self.peers:
            return {}
        data = self.peers.data[self.app].get(key, "")
        return json.loads(data) if data else {}

    @property
    def app_environment(self):
        """Create application environment variables dict from peer data."""
        db_data = self.get_peer_data("db_data")
        env = {
            "DEMO_SERVER_DB_HOST": db_data.get("db_host", None),
            "DEMO_SERVER_DB_PORT": db_data.get("db_port", None),
            "DEMO_SERVER_DB_USER": db_data.get("db_username", None),
            "DEMO_SERVER_DB_PASSWORD": db_data.get("db_password", None),
        }
        return env

    def fetch_postgres_relation_data(self):
        """Fetch postgres relation data and update peer relation databag."""
        data = self.database.fetch_relation_data()
        logger.debug("Got following database data: %s", data)
        for key, val in data.items():
            if not val:
                continue
            logger.info("New PSQL database endpoint is %s", val["endpoints"])
            host, port = val["endpoints"].split(":")
            self.set_peer_data(
                "db_data",
                {
                    "db_host": host,
                    "db_port": port,
                    "db_username": val["username"],
                    "db_password": val["password"],
                },
            )
            return

    def _on_database_created(self, event: DatabaseCreatedEvent) -> None:
        """Fetch info from database charm and update pebble layer.

        Event is fired when postgres database was created.
        """
        self.fetch_postgres_relation_data()
        self._update_layer_and_restart(None)

    def _on_database_relation_removed(self, event) -> None:
        """Reset peer data and put charm into waiting status.

        Event is fired when relation with postgres is broken.
        """
        self.set_peer_data("db_data", {})
        self.unit.status = WaitingStatus("Waiting for database relation")

    def _on_config_changed(self, event):
        port = self.config["server-port"]  # see config.yaml
        logger.debug("New application port is requested: %s", port)

        if int(port) == 22:
            self.unit.status = BlockedStatus("invalid port number, 22 is reserved for SSH")
            return

        self._update_layer_and_restart(None)

    def _on_get_db_info_action(self, event) -> None:
        """Show information about database access points.

        Learn more about actions at https://juju.is/docs/sdk/actions
        """
        show_password = event.params["show-password"]  # see actions.yaml
        db_data = self.get_peer_data("db_data")

        output = {
            "db-host": db_data.get("db_host", None),
            "db-port": db_data.get("db_port", None),
        }

        if show_password:
            output.update(
                {
                    "db-username": db_data.get("db_username", None),
                    "db-password": db_data.get("db_password", None),
                }
            )

        event.set_results(output)

    def _update_layer_and_restart(self, event) -> None:
        """Define and start a workload using the Pebble API.

        You'll need to specify the right entrypoint and environment
        configuration for your specific workload. Tip: you can see the
        standard entrypoint of an existing container using docker inspect

        Learn more about Pebble layers at https://github.com/canonical/pebble
        """

        # Learn more about statuses in the SDK docs:
        # https://juju.is/docs/sdk/constructs#heading--statuses
        self.unit.status = MaintenanceStatus("Assembling pod spec")
        if self.container.can_connect():
            new_layer = self._pebble_layer.to_dict()
            # Get the current pebble layer config
            services = self.container.get_plan().to_dict().get("services", {})
            if services != new_layer["services"]:
                # Changes were made, add the new layer
                self.container.add_layer("fastapi_demo", self._pebble_layer, combine=True)
                logger.info("Added updated layer 'fastapi_demo' to Pebble plan")

                self.container.restart(self.pebble_service_name)
                logger.info(f"Restarted '{self.pebble_service_name}' service")

            # add workload version in juju status
            self.unit.set_workload_version(self.version)
            self.unit.status = ActiveStatus()
        else:
            self.unit.status = WaitingStatus("Waiting for Pebble in workload container")

    @property
    def _pebble_layer(self):
        """Return a dictionary representing a Pebble layer."""
        command = " ".join(
            [
                "uvicorn",
                "api_demo_server.app:app",
                "--host=0.0.0.0",
                f"--port={self.config['server-port']}",
            ]
        )
        pebble_layer = {
            "summary": "FastAPI demo service",
            "description": "pebble config layer for FastAPI demo server",
            "services": {
                self.pebble_service_name: {
                    "override": "replace",
                    "summary": "fastapi demo",
                    "command": command,
                    "startup": "enabled",
                    "environment": self.app_environment,
                }
            },
        }
        return Layer(pebble_layer)

    @property
    def version(self) -> str:
        """Reports the current workload (FastAPI app) version."""
        if self.container.can_connect() and self.container.get_services(self.pebble_service_name):
            try:
                return self._request_version()
            # Catching Exception is not ideal, but we don't care much for the error here, and just
            # default to setting a blank version since there isn't much the admin can do!
            except Exception as e:
                logger.warning("unable to get version from API: %s", str(e))
                logger.exception(e)
        return ""

    def _request_version(self) -> str:
        """Helper for fetching the version from the running workload using the API."""
        resp = requests.get(f"http://localhost:{self.config['server-port']}/version", timeout=10)
        return resp.json()["version"]


if __name__ == "__main__":  # pragma: nocover
    main(FastAPIDemoCharm)
