# Copyright 2011 OpenStack Foundation
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

from oslo_config.cfg import NoSuchOptError
from oslo_log import log as logging
from oslo_utils import strutils
import webob.exc

from trove.backup.models import Backup as backup_model
from trove.backup import views as backup_views
import trove.common.apischema as apischema
from trove.common import cfg
from trove.common import exception
from trove.common.exception import DatabaseForUserNotInDatabaseListError
from trove.common.i18n import _
from trove.common.i18n import _LI
from trove.common import notification
from trove.common.notification import StartNotification
from trove.common import pagination
from trove.common import policy
from trove.common.remote import create_guest_client
from trove.common import utils
from trove.common import wsgi
from trove.datastore import models as datastore_models
from trove.extensions.common.service import RoutingDatabaseController
from trove.extensions.common.service import RoutingUserController
from trove.instance import models, views
from trove.module import models as module_models
from trove.module import views as module_views


CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class InstanceController(wsgi.Controller):

    """Controller for instance functionality."""
    schemas = apischema.instance.copy()

    @classmethod
    def authorize_instance_action(cls, context, instance_rule_name, instance):
        policy.authorize_on_target(context, 'instance:%s' % instance_rule_name,
                                   {'tenant': instance.tenant_id})

    @classmethod
    def get_action_schema(cls, body, action_schema):
        action_type = list(body.keys())[0]
        action_schema = action_schema.get(action_type, {})
        if action_type == 'resize':
            # volume or flavorRef
            resize_action = list(body[action_type].keys())[0]
            action_schema = action_schema.get(resize_action, {})
        return action_schema

    @classmethod
    def get_schema(cls, action, body):
        action_schema = super(InstanceController, cls).get_schema(action, body)
        if action == 'action':
            # resize or restart
            action_schema = cls.get_action_schema(body, action_schema)
        return action_schema

    def action(self, req, body, tenant_id, id):
        """
        Handles requests that modify existing instances in some manner. Actions
        could include 'resize', 'restart', 'reset_password'
        :param req: http request object
        :param body: deserialized body of the request as a dict
        :param tenant_id: the tenant id for whom owns the instance
        :param id: instance id
        """
        LOG.debug("instance action req : '%s'\n\n", req)
        if not body:
            raise exception.BadRequest(_("Invalid request body."))
        context = req.environ[wsgi.CONTEXT_KEY]
        _actions = {
            'restart': self._action_restart,
            'resize': self._action_resize,
            'reset_password': self._action_reset_password,
            'promote_to_replica_source':
            self._action_promote_to_replica_source,
            'eject_replica_source': self._action_eject_replica_source,
            'reset_status': self._action_reset_status,
        }
        selected_action = None
        action_name = None
        for key in body:
            if key in _actions:
                selected_action = _actions[key]
                action_name = key
        LOG.info(_LI("Performing %(action_name)s action against "
                     "instance %(instance_id)s for tenant '%(tenant_id)s'"),
                 {'action_name': action_name, 'instance_id': id,
                  'tenant_id': tenant_id})
        needs_server = True
        if action_name in ['reset_status']:
            needs_server = False
        instance = models.Instance.load(context, id, needs_server=needs_server)
        return selected_action(context, req, instance, body)

    def _action_restart(self, context, req, instance, body):
        context.notification = notification.DBaaSInstanceRestart(context,
                                                                 request=req)
        self.authorize_instance_action(context, 'restart', instance)
        with StartNotification(context, instance_id=instance.id):
            instance.restart()
        return wsgi.Result(None, 202)

    def _action_resize(self, context, req, instance, body):
        """
        Handles 2 cases
        1. resize volume
            body only contains {volume: {size: x}}
        2. resize instance
            body only contains {flavorRef: http.../2}

        If the body has both we will throw back an error.
        """
        options = {
            'volume': self._action_resize_volume,
            'flavorRef': self._action_resize_flavor
        }
        selected_option = None
        args = None
        for key in options:
            if key in body['resize']:
                selected_option = options[key]
                args = body['resize'][key]
                break
        return selected_option(context, req, instance, args)

    def _action_resize_volume(self, context, req, instance, volume):
        context.notification = notification.DBaaSInstanceResizeVolume(
            context, request=req)
        self.authorize_instance_action(context, 'resize_volume', instance)

        with StartNotification(context, instance_id=instance.id,
                               new_size=volume['size']):
            instance.resize_volume(volume['size'])
        return wsgi.Result(None, 202)

    def _action_resize_flavor(self, context, req, instance, flavorRef):
        context.notification = notification.DBaaSInstanceResizeInstance(
            context, request=req)
        self.authorize_instance_action(context, 'resize_flavor', instance)

        new_flavor_id = utils.get_id_from_href(flavorRef)
        with StartNotification(context, instance_id=instance.id,
                               new_flavor_id=new_flavor_id):
            instance.resize_flavor(new_flavor_id)
        return wsgi.Result(None, 202)

    def _action_reset_password(self, context, instance, body):
        self.authorize_instance_action(context, 'reset_password', instance)
        raise webob.exc.HTTPNotImplemented()  # TODO(pmalik): keep this? :P

    def _action_promote_to_replica_source(self, context, req, instance, body):
        self.authorize_instance_action(
            context, 'promote_to_replica_source', instance)
        context.notification = notification.DBaaSInstanceEject(context,
                                                               request=req)
        with StartNotification(context, instance_id=instance.id):
            instance.promote_to_replica_source()
        return wsgi.Result(None, 202)

    def _action_eject_replica_source(self, context, req, instance, body):
        self.authorize_instance_action(
            context, 'eject_replica_source', instance)
        context.notification = notification.DBaaSInstancePromote(context,
                                                                 request=req)
        with StartNotification(context, instance_id=instance.id):
            instance.eject_replica_source()
        return wsgi.Result(None, 202)

    def _action_reset_status(self, context, req, instance, body):
        if 'force_delete' in body['reset_status']:
            self.authorize_instance_action(context, 'force_delete', instance)
        else:
            self.authorize_instance_action(
                context, 'reset_status', instance)
        context.notification = notification.DBaaSInstanceResetStatus(
            context, request=req)
        with StartNotification(context, instance_id=instance.id):
            instance.reset_status()

            LOG.debug("Failing backups for instance %s." % instance.id)
            backup_model.fail_for_instance(instance.id)

        return wsgi.Result(None, 202)

    def index(self, req, tenant_id):
        """Return all instances."""
        LOG.info(_LI("Listing database instances for tenant '%s'"), tenant_id)
        LOG.debug("req : '%s'\n\n", req)
        context = req.environ[wsgi.CONTEXT_KEY]
        policy.authorize_on_tenant(context, 'instance:index')
        clustered_q = req.GET.get('include_clustered', '').lower()
        include_clustered = clustered_q == 'true'
        servers, marker = models.Instances.load(context, include_clustered)
        view = views.InstancesView(servers, req=req)
        paged = pagination.SimplePaginatedDataView(req.url, 'instances', view,
                                                   marker)
        return wsgi.Result(paged.data(), 200)

    def backups(self, req, tenant_id, id):
        """Return all backups for the specified instance."""
        LOG.info(_LI("Listing backups for instance '%s'"),
                 id)
        LOG.debug("req : '%s'\n\n", req)
        context = req.environ[wsgi.CONTEXT_KEY]

        instance = models.Instance.load(context, id)
        self.authorize_instance_action(context, 'backups', instance)

        backups, marker = backup_model.list_for_instance(context, id)
        view = backup_views.BackupViews(backups)
        paged = pagination.SimplePaginatedDataView(req.url, 'backups', view,
                                                   marker)
        return wsgi.Result(paged.data(), 200)

    def show(self, req, tenant_id, id):
        """Return a single instance."""
        LOG.info(_LI("Showing database instance '%(instance_id)s' for tenant "
                     "'%(tenant_id)s'"),
                 {'instance_id': id, 'tenant_id': tenant_id})
        LOG.debug("req : '%s'\n\n", req)

        context = req.environ[wsgi.CONTEXT_KEY]
        server = models.load_instance_with_info(models.DetailInstance,
                                                context, id)
        self.authorize_instance_action(context, 'show', server)
        return wsgi.Result(views.InstanceDetailView(server,
                                                    req=req).data(), 200)

    def delete(self, req, tenant_id, id):
        """Delete a single instance."""
        LOG.info(_LI("Deleting database instance '%(instance_id)s' for tenant "
                     "'%(tenant_id)s'"),
                 {'instance_id': id, 'tenant_id': tenant_id})
        LOG.debug("req : '%s'\n\n", req)
        context = req.environ[wsgi.CONTEXT_KEY]
        instance = models.load_any_instance(context, id)
        self.authorize_instance_action(context, 'delete', instance)
        context.notification = notification.DBaaSInstanceDelete(
            context, request=req)
        with StartNotification(context, instance_id=instance.id):
            marker = 'foo'
            while marker:
                instance_modules, marker = module_models.InstanceModules.load(
                    context, instance_id=id)
                for instance_module in instance_modules:
                    instance_module = module_models.InstanceModule.load(
                        context, instance_module['instance_id'],
                        instance_module['module_id'])
                    module_models.InstanceModule.delete(
                        context, instance_module)
            instance.delete()
        return wsgi.Result(None, 202)

    def create(self, req, body, tenant_id):
        # TODO(hub-cap): turn this into middleware
        LOG.info(_LI("Creating a database instance for tenant '%s'"),
                 tenant_id)
        LOG.debug("req : '%s'\n\n", strutils.mask_password(req))
        LOG.debug("body : '%s'\n\n", strutils.mask_password(body))
        context = req.environ[wsgi.CONTEXT_KEY]
        policy.authorize_on_tenant(context, 'instance:create')
        context.notification = notification.DBaaSInstanceCreate(context,
                                                                request=req)
        datastore_args = body['instance'].get('datastore', {})
        datastore, datastore_version = (
            datastore_models.get_datastore_version(**datastore_args))
        image_id = datastore_version.image_id

        name = body['instance']['name']
        flavor_ref = body['instance']['flavorRef']
        flavor_id = utils.get_id_from_href(flavor_ref)

        configuration = self._configuration_parse(context, body)
        users, databases = self._parse_users_and_databases(
            datastore_version.manager, body)

        modules = body['instance'].get('modules')

        # The following operations have their own API calls.
        # We need to make sure the same policies are enforced when
        # creating an instance.
        # i.e. if attaching configuration group to an existing instance is not
        # allowed, it should not be possible to create a new instance with the
        # group attached either
        if configuration:
            policy.authorize_on_tenant(context, 'instance:update')
        if modules:
            policy.authorize_on_tenant(context, 'instance:module_apply')
        if users:
            policy.authorize_on_tenant(
                context, 'instance:extension:user:create')
        if databases:
            policy.authorize_on_tenant(
                context, 'instance:extension:database:create')

        modules = body['instance'].get('modules')

        # The following operations have their own API calls.
        # We need to make sure the same policies are enforced when
        # creating an instance.
        # i.e. if attaching configuration group to an existing instance is not
        # allowed, it should not be possible to create a new instance with the
        # group attached either
        if configuration:
            policy.authorize_on_tenant(context, 'instance:update')
        if modules:
            policy.authorize_on_tenant(context, 'instance:module_apply')
        if users:
            policy.authorize_on_tenant(
                context, 'instance:extension:user:create')
        if databases:
            policy.authorize_on_tenant(
                context, 'instance:extension:database:create')

        if 'volume' in body['instance']:
            volume_info = body['instance']['volume']
            volume_size = int(volume_info['size'])
            volume_type = volume_info.get('type')
        else:
            volume_size = None
            volume_type = None

        if 'restorePoint' in body['instance']:
            backupRef = body['instance']['restorePoint']['backupRef']
            backup_id = utils.get_id_from_href(backupRef)
        else:
            backup_id = None

        availability_zone = body['instance'].get('availability_zone')
        nics = body['instance'].get('nics')

        slave_of_id = body['instance'].get('replica_of',
                                           # also check for older name
                                           body['instance'].get('slave_of'))
        replica_count = body['instance'].get('replica_count')
        locality = body['instance'].get('locality')
        if locality:
            locality_domain = ['affinity', 'anti-affinity']
            locality_domain_msg = ("Invalid locality '%s'. "
                                   "Must be one of ['%s']" %
                                   (locality,
                                    "', '".join(locality_domain)))
            if locality not in locality_domain:
                raise exception.BadRequest(msg=locality_domain_msg)
            if slave_of_id:
                dupe_locality_msg = (
                    'Cannot specify locality when adding replicas to existing '
                    'master.')
                raise exception.BadRequest(msg=dupe_locality_msg)
        region_name = body['instance'].get('region_name', CONF.os_region_name)

        instance = models.Instance.create(context, name, flavor_id,
                                          image_id, databases, users,
                                          datastore, datastore_version,
                                          volume_size, backup_id,
                                          availability_zone, nics,
                                          configuration, slave_of_id,
                                          replica_count=replica_count,
                                          volume_type=volume_type,
                                          modules=modules,
                                          locality=locality,
                                          region_name=region_name)

        view = views.InstanceDetailView(instance, req=req)
        return wsgi.Result(view.data(), 200)

    def _parse_users_and_databases(self, manager, body):
        """Parse user/database models from the request body.
        Use the controllers for a given manager.
        If the datastore does not support related operations
        (i.e. does not have controller) ignore any payload.
        If the datastore sets its controller to None
        fail if the payload is non-empty.
        """

        try:
            user_data = body['instance'].get('users', [])
            user_models = []
            user_controller = None
            try:
                user_controller = RoutingUserController.load_controller(
                    manager)
                if not user_controller:
                    # Datastore supports user operations but the related
                    # controller is undefined.
                    raise exception.BadRequest(
                        _("Datastore does not have user controller "
                          "configured."))
                user_models = user_controller.parse_users_from_request(
                    user_data)
            except NoSuchOptError:
                # Datastore does not support users at all.
                pass

            db_data = body['instance'].get('databases', [])
            db_models = []
            db_controller = None
            try:
                db_controller = RoutingDatabaseController.load_controller(
                    manager)
                if not db_controller:
                    # Datastore supports database operations but the
                    # related controller is undefined.
                    raise exception.BadRequest(
                        _("Datastore does not have database controller "
                          "configured."))
                db_models = db_controller.parse_databases_from_request(
                    db_data)
            except NoSuchOptError:
                # Datastore does not support databases at all.
                pass

            unique_db_ids = set()
            for db_model in db_models:
                database_id = db_controller.get_database_id(db_model)
                if database_id in unique_db_ids:
                    raise exception.DatabaseInitialDatabaseDuplicateError()
                unique_db_ids.add(database_id)

            unique_user_ids = set()
            for user_model in user_models:
                user_id = user_controller.get_user_id(user_model)
                if user_id in unique_user_ids:
                    raise exception.DatabaseInitialUserDuplicateError()
                unique_user_ids.add(user_id)

                if hasattr(user_model, 'databases') and user_model.databases:
                    user_dbs = db_controller.parse_databases_from_response(
                        user_model.databases)
                    for db_model in user_dbs:
                        database_id = db_controller.get_database_id(db_model)
                        if database_id not in unique_db_ids:
                            raise DatabaseForUserNotInDatabaseListError(
                                user=user_id, database=database_id)

            users = [user_model.serialize() for user_model in user_models]
            databases = [db_model.serialize() for db_model in db_models]

            return users, databases
        except ValueError as ve:
            raise exception.BadRequest(msg=ve)

    def _configuration_parse(self, context, body):
        if 'configuration' in body['instance']:
            configuration_ref = body['instance']['configuration']
            if configuration_ref:
                configuration_id = utils.get_id_from_href(configuration_ref)
                return configuration_id

    def _modify_instance(self, context, req, instance, **kwargs):
        if 'detach_replica' in kwargs and kwargs['detach_replica']:
            LOG.debug("Detaching replica from source.")
            context.notification = notification.DBaaSInstanceDetach(
                context, request=req)
            with StartNotification(context, instance_id=instance.id):
                instance.detach_replica()
        if 'configuration_id' in kwargs:
            if kwargs['configuration_id']:
                context.notification = (
                    notification.DBaaSInstanceAttachConfiguration(context,
                                                                  request=req))
                configuration_id = kwargs['configuration_id']
                with StartNotification(context, instance_id=instance.id,
                                       configuration_id=configuration_id):
                    instance.attach_configuration(configuration_id)
            else:
                context.notification = (
                    notification.DBaaSInstanceDetachConfiguration(context,
                                                                  request=req))
                with StartNotification(context, instance_id=instance.id):
                    instance.detach_configuration()
        if 'datastore_version' in kwargs:
            datastore_version = datastore_models.DatastoreVersion.load(
                instance.datastore, kwargs['datastore_version'])
            context.notification = (
                notification.DBaaSInstanceUpgrade(context, request=req))
            with StartNotification(context, instance_id=instance.id,
                                   datastore_version_id=datastore_version.id):
                instance.upgrade(datastore_version)
        if kwargs:
            instance.update_db(**kwargs)

    def update(self, req, id, body, tenant_id):
        """Updates the instance to attach/detach configuration."""
        LOG.info(_LI("Updating database instance '%(instance_id)s' for tenant "
                     "'%(tenant_id)s'"),
                 {'instance_id': id, 'tenant_id': tenant_id})
        LOG.debug("req: %s", req)
        LOG.debug("body: %s", body)
        context = req.environ[wsgi.CONTEXT_KEY]

        instance = models.Instance.load(context, id)
        self.authorize_instance_action(context, 'update', instance)

        # Make sure args contains a 'configuration_id' argument,
        args = {}
        args['configuration_id'] = self._configuration_parse(context, body)
        self._modify_instance(context, req, instance, **args)
        return wsgi.Result(None, 202)

    def edit(self, req, id, body, tenant_id):
        """
        Updates the instance to set or unset one or more attributes.
        """
        LOG.info(_LI("Editing instance for tenant id %s."), tenant_id)
        LOG.debug("req: %s", strutils.mask_password(req))
        LOG.debug("body: %s", strutils.mask_password(body))
        context = req.environ[wsgi.CONTEXT_KEY]

        instance = models.Instance.load(context, id)
        self.authorize_instance_action(context, 'edit', instance)

        args = {}
        args['detach_replica'] = ('replica_of' in body['instance'] or
                                  'slave_of' in body['instance'])

        if 'name' in body['instance']:
            args['name'] = body['instance']['name']
        if 'configuration' in body['instance']:
            args['configuration_id'] = self._configuration_parse(context, body)
        if 'datastore_version' in body['instance']:
            args['datastore_version'] = body['instance'].get(
                'datastore_version')

        self._modify_instance(context, req, instance, **args)
        return wsgi.Result(None, 202)

    def configuration(self, req, tenant_id, id):
        """
        Returns the default configuration template applied to the instance.
        """
        LOG.info(_LI("Getting default configuration for instance %s"), id)
        context = req.environ[wsgi.CONTEXT_KEY]
        instance = models.Instance.load(context, id)
        self.authorize_instance_action(context, 'configuration', instance)

        LOG.debug("Server: %s", instance)
        config = instance.get_default_configuration_template()
        LOG.debug("Default config for instance %(instance_id)s is %(config)s",
                  {'instance_id': id, 'config': config})
        return wsgi.Result(views.DefaultConfigurationView(
                           config).data(), 200)

    def guest_log_list(self, req, tenant_id, id):
        """Return all information about all logs for an instance."""
        LOG.debug("Listing logs for tenant %s" % tenant_id)
        context = req.environ[wsgi.CONTEXT_KEY]
        instance = models.Instance.load(context, id)
        if not instance:
            raise exception.NotFound(uuid=id)
        self.authorize_instance_action(context, 'guest_log_list', instance)
        client = create_guest_client(context, id)
        guest_log_list = client.guest_log_list()
        return wsgi.Result({'logs': guest_log_list}, 200)

    def guest_log_action(self, req, body, tenant_id, id):
        """Processes a guest log."""
        LOG.info(_("Processing log for tenant %s"), tenant_id)
        context = req.environ[wsgi.CONTEXT_KEY]
        instance = models.Instance.load(context, id)
        if not instance:
            raise exception.NotFound(uuid=id)
        log_name = body['name']
        enable = body.get('enable', None)
        disable = body.get('disable', None)
        publish = body.get('publish', None)
        discard = body.get('discard', None)
        if enable and disable:
            raise exception.BadRequest(_("Cannot enable and disable log."))
        client = create_guest_client(context, id)
        guest_log = client.guest_log_action(log_name, enable, disable,
                                            publish, discard)
        return wsgi.Result({'log': guest_log}, 200)

    def module_list(self, req, tenant_id, id):
        """Return information about modules on an instance."""
        context = req.environ[wsgi.CONTEXT_KEY]
        instance = models.Instance.load(context, id)
        if not instance:
            raise exception.NotFound(uuid=id)
        self.authorize_instance_action(context, 'module_list', instance)
        from_guest = bool(req.GET.get('from_guest', '').lower())
        include_contents = bool(req.GET.get('include_contents', '').lower())
        if from_guest:
            return self._module_list_guest(
                context, id, include_contents=include_contents)
        else:
            return self._module_list(
                context, id, include_contents=include_contents)

    def _module_list_guest(self, context, id, include_contents):
        """Return information about modules on an instance."""
        client = create_guest_client(context, id)
        result_list = client.module_list(include_contents)
        return wsgi.Result({'modules': result_list}, 200)

    def _module_list(self, context, id, include_contents):
        """Return information about instance modules."""
        client = create_guest_client(context, id)
        result_list = client.module_list(include_contents)
        return wsgi.Result({'modules': result_list}, 200)

    def module_apply(self, req, body, tenant_id, id):
        """Apply modules to an instance."""
        context = req.environ[wsgi.CONTEXT_KEY]
        instance = models.Instance.load(context, id)
        if not instance:
            raise exception.NotFound(uuid=id)
        self.authorize_instance_action(context, 'module_apply', instance)
        module_ids = [mod['id'] for mod in body.get('modules', [])]
        modules = module_models.Modules.load_by_ids(context, module_ids)
        models.validate_modules_for_apply(
            modules, instance.datastore.id, instance.datastore_version.id)
        module_list = module_views.get_module_list(modules)
        client = create_guest_client(context, id)
        result_list = client.module_apply(module_list)
        models.Instance.add_instance_modules(context, id, modules)
        return wsgi.Result({'modules': result_list}, 200)

    def module_remove(self, req, tenant_id, id, module_id):
        """Remove module from an instance."""
        context = req.environ[wsgi.CONTEXT_KEY]
        instance = models.Instance.load(context, id)
        if not instance:
            raise exception.NotFound(uuid=id)
        self.authorize_instance_action(context, 'module_remove', instance)
        module = module_models.Module.load(context, module_id)
        module_info = module_views.DetailedModuleView(module).data()
        client = create_guest_client(context, id)
        client.module_remove(module_info)
        instance_modules = module_models.InstanceModules.load_all(
            context, instance_id=id, module_id=module_id)
        for instance_module in instance_modules:
            module_models.InstanceModule.delete(context, instance_module)
            LOG.debug("Deleted IM record %s (instance %s, module %s)." %
                      (instance_module.id, id, module_id))
        return wsgi.Result(None, 200)
