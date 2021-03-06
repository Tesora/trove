# Copyright 2015 Tesora Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from trove.common import cfg
from trove.common.db.cassandra import models as guest_models
from trove.extensions.cassandra import views as cassandra_views
from trove.extensions.common.service import DatastoreDatabaseController
from trove.extensions.common.service import DatastoreUserAccessController
from trove.extensions.common.service import DatastoreUserController
from trove.extensions.common.service import DefaultRootController


class CassandraUserController(DatastoreUserController):

    def is_reserved_id(self, user_id):
        return user_id in cfg.get_ignored_users()

    def build_model_view(self, user_model):
        return cassandra_views.UserView(user_model)

    def build_model_collection_view(self, user_models):
        return cassandra_views.UsersView(user_models)

    def parse_user_from_response(self, user_data):
        return guest_models.CassandraUser.deserialize(user_data)

    def parse_user_from_request(self, user_data):
        name = user_data['name']
        password = user_data['password']
        databases = user_data.get('databases', [])
        user_model = guest_models.CassandraUser(name, password)
        for db in databases:
            user_model.databases = db['name']
        return user_model


class CassandraDatabaseController(DatastoreDatabaseController):

    def is_reserved_id(self, database_id):
        return database_id in cfg.get_ignored_dbs()

    def build_model_view(self, database_model):
        return cassandra_views.DatabaseView(database_model)

    def build_model_collection_view(self, database_models):
        return cassandra_views.DatabasesView(database_models)

    def parse_database_from_response(self, database_data):
        return guest_models.CassandraSchema.deserialize(database_data)

    def parse_database_from_request(self, database_data):
        name = database_data['name']
        return guest_models.CassandraSchema(name)


class CassandraUserAccessController(DatastoreUserAccessController):

    @property
    def user_controller(self):
        return CassandraUserController()

    @property
    def database_controller(self):
        return CassandraDatabaseController()


class CassandraRootController(DefaultRootController):
    pass
