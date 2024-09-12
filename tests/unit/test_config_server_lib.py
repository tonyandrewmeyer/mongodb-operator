# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
import unittest
from unittest import mock

from ops.testing import Harness

from charm import MongodbOperatorCharm

RELATION_NAME = "s3-credentials"


class TestConfigServerInterface(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(MongodbOperatorCharm)
        self.harness.begin()
        self.harness.add_relation("database-peers", "database-peers")
        self.harness.set_leader(True)
        self.charm = self.harness.charm
        self.addCleanup(self.harness.cleanup)

    def test_on_relation_joined_failed_hook_checks(self):
        """Tests that no relation data is set when cluster joining conditions are not met."""

        def is_not_config_mock_call(*args):
            assert args == ("config-server",)
            return False

        self.harness.charm.app_peer_data["db_initialised"] = "True"

        # fails due to being run on non-config-server
        self.harness.charm.is_role = is_not_config_mock_call
        relation_id = self.harness.add_relation("cluster", "mongos")
        self.harness.add_relation_unit(relation_id, "mongos/0")
        self.harness.charm.cluster.database_provides.update_relation_data = mock.Mock()
        self.harness.charm.cluster.database_provides.update_relation_data.assert_not_called()

        # fails because db has not been initialized
        del self.harness.charm.app_peer_data["db_initialised"]

        def is_config_mock_call(*args):
            assert args == ("config-server",)
            return True

        self.harness.charm.is_role = is_config_mock_call
        self.harness.add_relation_unit(relation_id, "mongos/1")
        self.harness.charm.cluster.database_provides.update_relation_data.assert_not_called()

        # fails because not leader
        self.harness.set_leader(False)
        self.harness.add_relation_unit(relation_id, "mongos/2")
        self.harness.charm.cluster.database_provides.update_relation_data.assert_not_called()

    def test_update_rel_data_failed_hook_checks(self):
        """Tests that no relation data is set when the cluster is not ready."""

        def is_not_config_mock_call(*args):
            assert args == ("config-server",)
            return False

        self.harness.charm.app_peer_data["db_initialised"] = "True"
        self.harness.add_relation("cluster", "mongos")

        # fails due to being run on non-config-server
        self.harness.charm.is_role = is_not_config_mock_call
        self.harness.charm.cluster.update_config_server_db(mock.Mock())
        self.harness.charm.cluster.database_provides.update_relation_data = mock.Mock()
        self.harness.charm.cluster.database_provides.update_relation_data.assert_not_called()

        # fails because db has not been initialized
        del self.harness.charm.app_peer_data["db_initialised"]

        def is_config_mock_call(*args):
            assert args == ("config-server",)
            return True

        self.harness.charm.is_role = is_config_mock_call
        self.harness.charm.cluster.update_config_server_db(mock.Mock())
        self.harness.charm.cluster.database_provides.update_relation_data.assert_not_called()

        # fails because not leader
        self.harness.set_leader(False)
        self.harness.charm.cluster.update_config_server_db(mock.Mock())
        self.harness.charm.cluster.database_provides.update_relation_data.assert_not_called()

    def test_pass_hooks_check_waits_for_start_config_server(self):
        """Ensure that pass_hooks defers until the database is initialized.

        Note: in some cases sharding related hooks execute before config and leader elected hooks,
        therefore it is important that the `pass_hooks_check` defers an event until the database
        has been started
        """

        def is_shard_mock_call(*args):
            return args == ("shard")

        self.harness.charm.is_role = is_shard_mock_call

        event = mock.Mock()
        event.params = {}

        self.harness.set_leader(False)
        self.harness.charm.config_server.pass_hook_checks(event)
        event.defer.assert_called()

        # once the database has been initialised, pass hooks check should no longer defer if the
        # unit is not the leader nor is the wrong wrole
        event = mock.Mock()
        event.params = {}
        self.harness.charm.app_peer_data["db_initialised"] = "True"
        self.harness.charm.config_server.pass_hook_checks(event)
        event.defer.assert_not_called()

    def test_pass_hooks_check_waits_for_start_shard(self):
        """Ensure that pass_hooks defers until the database is initialized.

        Note: in some cases sharding related hooks execute before config and leader elected hooks,
        therefore it is important that the `pass_hooks_check` defers an event until the database
        has been started
        """

        def is_config_mock_call(*args):
            return args == ("config-server")

        self.harness.charm.is_role = is_config_mock_call

        event = mock.Mock()
        event.params = {}

        self.harness.set_leader(False)
        self.harness.charm.shard.pass_hook_checks(event)
        event.defer.assert_called()

        # once the database has been initialised, pass hooks check should no longer defer if the
        # unit is not the leader nor is the wrong wrole
        event = mock.Mock()
        event.params = {}
        self.harness.charm.app_peer_data["db_initialised"] = "True"
        self.harness.charm.shard.pass_hook_checks(event)
        event.defer.assert_not_called()
