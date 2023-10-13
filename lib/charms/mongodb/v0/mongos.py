"""Code for interactions with MongoDB."""
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass
from typing import Dict, Optional, Set
from urllib.parse import quote_plus

from charms.mongodb.v0.mongodb import NotReadyError
from pymongo import MongoClient

from config import Config

# The unique Charmhub library identifier, never change it
LIBID = "e20d5b19670d4c55a4934a21d3f3b29a"

# Increment this major API version when introducing breaking changes
LIBAPI = 1

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 0

# path to store mongodb ketFile
logger = logging.getLogger(__name__)


@dataclass
class MongosConfiguration:
    """Class for mongos configuration.

    — database: database name.
    — username: username.
    — password: password.
    — hosts: full list of hosts to connect to, needed for the URI.
    - port: integer for the port to connect to connect to mongodb.
    - tls_external: indicator for use of internal TLS connection.
    - tls_internal: indicator for use of external TLS connection.
    """

    database: Optional[str]
    username: str
    password: str
    hosts: Set[str]
    port: int
    roles: Set[str]
    tls_external: bool
    tls_internal: bool

    @property
    def uri(self):
        """Return URI concatenated from fields."""
        hosts = [f"{host}:{self.port}" for host in self.hosts]
        hosts = ",".join(hosts)
        # Auth DB should be specified while user connects to application DB.
        auth_source = ""
        if self.database != "admin":
            auth_source = "&authSource=admin"
        return (
            f"mongodb://{quote_plus(self.username)}:"
            f"{quote_plus(self.password)}@"
            f"{hosts}/{quote_plus(self.database)}?"
            f"{auth_source}"
        )


class RemovePrimaryShardError(Exception):
    """Raised when there is an attempt to remove the primary shard."""


class ShardNotInClusterError(Exception):
    """Raised when shard is not present in cluster, but it is expected to be."""


class ShardNotPlannedForRemovalError(Exception):
    """Raised when it is expected that a shard is planned for removal, but it is not."""


class MongosConnection:
    """In this class we create connection object to Mongos.

    Real connection is created on the first call to Mongos.
    Delayed connectivity allows to firstly check database readiness
    and reuse the same connection for an actual query later in the code.

    Connection is automatically closed when object destroyed.
    Automatic close allows to have more clean code.

    Note that connection when used may lead to the following pymongo errors: ConfigurationError,
    ConfigurationError, OperationFailure. It is suggested that the following pattern be adopted
    when using MongoDBConnection:

    with MongoMongos(self._mongos_config) as mongo:
        try:
            mongo.<some operation from this class>
        except ConfigurationError, OperationFailure:
            <error handling as needed>
    """

    def __init__(self, config: MongosConfiguration, uri=None, direct=False):
        """A MongoDB client interface.

        Args:
            config: MongoDB Configuration object.
            uri: allow using custom MongoDB URI, needed for replSet init.
            direct: force a direct connection to a specific host, avoiding
                    reading replica set configuration and reconnection.
        """
        self.mongodb_config = config

        if uri is None:
            uri = config.uri

        self.client = MongoClient(
            uri,
            directConnection=direct,
            connect=False,
            serverSelectionTimeoutMS=1000,
            connectTimeoutMS=2000,
        )
        return

    def __enter__(self):
        """Return a reference to the new connection."""
        return self

    def __exit__(self, object_type, value, traceback):
        """Disconnect from MongoDB client."""
        self.client.close()
        self.client = None

    def get_shard_members(self) -> Set[str]:
        """Gets shard members.

        Returns:
            A set of the shard members as reported by mongos.

        Raises:
            ConfigurationError, OperationFailure
        """
        shard_list = self.client.admin.command("listShards")
        curr_members = [
            self._hostname_from_hostport(member["host"]) for member in shard_list["shards"]
        ]
        return set(curr_members)

    def add_shard(self, shard_name, shard_hosts, shard_port=Config.MONGODB_PORT):
        """Adds shard to the cluster.

        Raises:
            ConfigurationError, OperationFailure
        """
        shard_hosts = [f"{host}:{shard_port}" for host in shard_hosts]
        shard_hosts = ",".join(shard_hosts)
        shard_url = f"{shard_name}/{shard_hosts}"
        # TODO Future PR raise error when number of shards currently adding are higher than the
        # number of secondaries on the primary shard. This will be challenging, as there is no
        # MongoDB command to retrieve the primary shard. Will likely need to be done via
        # mongosh

        if shard_name in self.get_shard_members():
            logger.info("Skipping adding shard %s, shard is already in cluster", shard_name)
            return

        logger.info("Adding shard %s", shard_name)
        self.client.admin.command("addShard", shard_url)

    def remove_shard(self, shard_name: str) -> None:
        """Removes shard from the cluster.

        Raises:
            ConfigurationError, OperationFailure, NotReadyError,
            RemovePrimaryShardError
        """
        sc_status = self.client.admin.command("listShards")
        # It is necessary to call removeShard multiple times on a shard to guarantee removal.
        # Allow re-removal of shards that are currently draining.
        if self._is_any_draining(sc_status, ignore_shard=shard_name):
            cannot_remove_shard = (
                f"cannot remove shard {shard_name} from cluster, another shard is draining"
            )
            logger.error(cannot_remove_shard)
            raise NotReadyError(cannot_remove_shard)

        # TODO Follow up PR, there is no MongoDB command to retrieve primary shard, this is
        # possible with mongosh.
        primary_shard = self.get_primary_shard()
        if primary_shard:
            # TODO Future PR, support removing Primary Shard if there are no unsharded collections
            # on it. All sharded collections should perform `MovePrimary`
            cannot_remove_primary_shard = (
                f"Shard {shard_name} is the primary shard, cannot remove."
            )
            logger.error(cannot_remove_primary_shard)
            raise RemovePrimaryShardError(cannot_remove_primary_shard)

        logger.info("Attempting to remove shard %s", shard_name)
        removal_info = self.client.admin.command("removeShard", shard_name)

        # process removal status
        remaining_chunks = (
            removal_info["remaining"]["chunks"] if "remaining" in removal_info else "None"
        )
        dbs_to_move = (
            removal_info["dbsToMove"]
            if "dbsToMove" in removal_info and removal_info["dbsToMove"] != []
            else ["None"]
        )
        logger.info(
            "Shard %s is draining status is: %s. Remaining chunks: %s. DBs to move: %s.",
            shard_name,
            removal_info["state"],
            str(remaining_chunks),
            ",".join(dbs_to_move),
        )

    def _is_shard_draining(self, shard_name: str) -> bool:
        """Reports if a given shard is currently in the draining state.

        Raises:
            ConfigurationError, OperationFailure, ShardNotInClusterError,
            ShardNotPlannedForRemovalError
        """
        sc_status = self.client.admin.command("listShards")
        for shard in sc_status["shards"]:
            if shard["_id"] == shard_name:
                if "draining" not in shard:
                    raise ShardNotPlannedForRemovalError(
                        f"Shard {shard_name} has not been marked for removal",
                    )
                return shard["draining"]

        raise ShardNotInClusterError(
            f"Shard {shard_name} not in cluster, could not retrieve draining status"
        )

    def get_primary_shard(self) -> str:
        """Processes sc_status and identifies the primary shard."""
        # TODO Follow up PR, implement this function there is no MongoDB command to retrieve
        # primary shard, this is possible with mongosh.
        return False

    @staticmethod
    def _is_any_draining(sc_status: Dict, ignore_shard: str = "") -> bool:
        """Returns true if any shard members is draining.

        Checks if any members in sharded cluster are draining data.

        Args:
            sc_status: current state of shard cluster status as reported by mongos.
            ignore_shard: shard to ignore
        """
        return any(
            # check draining status of all shards except the one to be ignored.
            shard.get("draining", False) if shard["_id"] != ignore_shard else False
            for shard in sc_status["shards"]
        )

    @staticmethod
    def _hostname_from_hostport(hostname: str) -> str:
        """Return hostname part from MongoDB returned.

        mongos typically returns a value that contains both, hostname, hosts, and ports.
        e.g. input: shard03/host7:27018,host8:27018,host9:27018
        Return shard name
        e.g. output: shard03
        """
        return hostname.split("/")[0]
