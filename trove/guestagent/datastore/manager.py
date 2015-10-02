# Copyright 2014 Tesora, Inc.
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
#

import abc

from oslo_config import cfg as oslo_cfg
from oslo_log import log as logging
from oslo_service import periodic_task

from trove.common import cfg
from trove.common import exception
from trove.common.i18n import _
from trove.common.notification import EndNotification
from trove.guestagent.common import guestagent_utils
from trove.guestagent.common import operating_system
from trove.guestagent.common.operating_system import FileMode
from trove.guestagent import guest_log


LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class Manager(periodic_task.PeriodicTasks):
    """This is the base class for all datastore managers.  Over time, common
    functionality should be pulled back here from the existing managers.
    """

    GUEST_LOG_TYPE_LABEL = 'type'
    GUEST_LOG_USER_LABEL = 'user'
    GUEST_LOG_FILE_LABEL = 'file'
    GUEST_LOG_SECTION_LABEL = 'section'
    GUEST_LOG_ENABLE_LABEL = 'enable'
    GUEST_LOG_DISABLE_LABEL = 'disable'

    GUEST_LOG_BASE_DIR = '/var/log/trove'
    GUEST_LOG_DATASTORE_DIRNAME = 'datastore'
    GUEST_LOG_DEFS_GENERAL_LABEL = 'general'
    GUEST_LOG_DEFS_ERROR_LABEL = 'error'
    GUEST_LOG_DEFS_SLOW_QUERY_LABEL = 'slow_query'

    def __init__(self):

        super(Manager, self).__init__(CONF)

        # Manager properties
        self.__prepare_error = False

        # Guest log
        self._guest_log_context = None
        self._guest_log_loaded_context = None
        self._guest_log_cache = None
        self._guest_log_defs = None

    @property
    def manager(self):
        """This should return the name of the manager.  Each datastore
        can override this if the default is not correct.
        """
        return CONF.datastore_manager

    @property
    def configuration_manager(self):
        """If the datastore supports the new-style configuration manager,
        it should override this to return it.
        """
        return None

    @abc.abstractproperty
    def status(self):
        """This should return an instance of a status class that has been
        inherited from datastore.service.BaseDbStatus.  Each datastore
        must implement this property.
        """
        return None

    @property
    def prepare_error(self):
        return self.__prepare_error

    @prepare_error.setter
    def prepare_error(self, prepare_error):
        self.__prepare_error = prepare_error

    @property
    def datastore_log_defs(self):
        """Any datastore-specific log files should be overridden in this dict
        by the corresponding Manager class.

        Format of a dict entry:

        'name_of_log': {self.GUEST_LOG_TYPE_LABEL:
                            Specified by the Enum in guest_log.LogType,
                        self.GUEST_LOG_USER_LABEL:
                            User that owns the file,
                        self.GUEST_LOG_FILE_LABEL:
                            Path on filesystem where the log resides,
                        self.GUEST_LOG_SECTION_LABEL:
                            Section where to put config (if ini style)
                        self.GUEST_LOG_ENABLE_LABEL: {
                            Dict of config_group settings to enable log},
                        self.GUEST_LOG_DISABLE_LABEL: {
                            Dict of config_group settings to disable log},

        See guestagent_log_defs for an example.
        """
        return {}

    @property
    def guestagent_log_defs(self):
        """These are log files that should be available on every Trove
        instance.  By definition, these should be of type LogType.SYS
        """
        log_dir = CONF.get('log_dir', '/var/log/trove/')
        log_file = CONF.get('log_file', 'trove-guestagent.log')
        guestagent_log = guestagent_utils.build_file_path(log_dir, log_file)
        return {
            'guest': {
                self.GUEST_LOG_TYPE_LABEL: guest_log.LogType.SYS,
                self.GUEST_LOG_USER_LABEL: None,
                self.GUEST_LOG_FILE_LABEL: guestagent_log,
            },
        }

    @property
    def guest_log_defs(self):
        """Return all the guest log defs."""
        if not self._guest_log_defs:
            self._guest_log_defs = dict(self.datastore_log_defs)
            self._guest_log_defs.update(self.guestagent_log_defs)
        return self._guest_log_defs

    @property
    def guest_log_context(self):
        return self._guest_log_context

    @guest_log_context.setter
    def guest_log_context(self, context):
        self._guest_log_context = context

    @property
    def guest_log_cache(self):
        """Make sure the guest_log_cache is loaded and return it."""
        self._refresh_guest_log_cache()
        return self._guest_log_cache

    def _refresh_guest_log_cache(self):
        if self._guest_log_cache:
            # Replace the context if it's changed
            if self._guest_log_loaded_context != self.guest_log_context:
                for log_name in self._guest_log_cache.keys():
                    self._guest_log_cache[log_name].context = (
                        self.guest_log_context)
        else:
            # Load the initial cache
            self._guest_log_cache = {}
            if self.guest_log_context:
                gl_defs = self.guest_log_defs
                try:
                    exposed_logs = CONF.get(self.manager).get(
                        'guest_log_exposed_logs')
                except oslo_cfg.NoSuchOptError:
                    pass
                if not exposed_logs:
                    exposed_logs = CONF.guest_log_exposed_logs
                LOG.debug("Available log defs: %s" % ",".join(gl_defs.keys()))
                exposed_logs = exposed_logs.lower().replace(',', ' ').split()
                LOG.debug("Exposing log defs: %s" % ",".join(exposed_logs))
                expose_all = 'all' in exposed_logs
                for log_name in gl_defs.keys():
                    gl_def = gl_defs[log_name]
                    exposed = expose_all or log_name in exposed_logs
                    LOG.debug("Building guest log '%s' from def: %s "
                              "(exposed: %s)" %
                              (log_name, gl_def, exposed))
                    self._guest_log_cache[log_name] = guest_log.GuestLog(
                        self.guest_log_context, log_name,
                        gl_def[self.GUEST_LOG_TYPE_LABEL],
                        gl_def[self.GUEST_LOG_USER_LABEL],
                        gl_def[self.GUEST_LOG_FILE_LABEL],
                        exposed)

        self._guest_log_loaded_context = self.guest_log_context

    ########################
    # Status related methods
    ########################
    @periodic_task.periodic_task
    def update_status(self, context):
        """Update the status of the trove instance. It is decorated with
        perodic task so it is called automatically.
        """
        LOG.debug("Update status called.")
        self.status.update()

    #########################
    # Prepare related methods
    #########################
    def prepare(self, context, packages, databases, memory_mb, users,
                device_path=None, mount_point=None, backup_info=None,
                config_contents=None, root_password=None, overrides=None,
                cluster_config=None, snapshot=None):
        """Set up datastore on a Guest Instance."""
        LOG.info(_("Starting datastore prepare."))
        with EndNotification(context):
            self.status.begin_install()
            post_processing = True if cluster_config else False
            try:
                self.do_prepare(
                    context, packages, databases, memory_mb, users,
                    device_path=device_path, mount_point=mount_point,
                    backup_info=backup_info, config_contents=config_contents,
                    root_password=root_password, overrides=overrides,
                    cluster_config=cluster_config, snapshot=snapshot)
            except Exception:
                self.prepare_error = True
                LOG.exception("An error occurred preparing datastore")
                raise
            finally:
                LOG.info(_("Ending datastore prepare."))
                self.status.end_install(error_occurred=self.prepare_error,
                                        post_processing=post_processing)
        LOG.info(_('Completed setup of datastore successfully.'))

    @abc.abstractmethod
    def do_prepare(self, context, packages, databases, memory_mb, users,
                   device_path, mount_point, backup_info, config_contents,
                   root_password, overrides, cluster_config, snapshot):
        """This is called from prepare when the Trove instance first comes
        online.  'Prepare' is the first rpc message passed from the
        task manager.  do_prepare handles all the base configuration of
        the instance and is where the actual work is done.  Each datastore
        must implement this method.
        """
        pass

    #########################
    # Cluster related methods
    #########################
    def cluster_complete(self, context):
        LOG.debug("Cluster creation complete, starting status checks.")
        self.status.end_install()

    #####################
    # Log related methods
    #####################
    def guest_log_list(self, context):
        LOG.debug("Getting list of guest logs.")
        self.guest_log_context = context
        gl_cache = self.guest_log_cache
        result = filter(None, [gl_cache[log_name].show()
                        for log_name in gl_cache.keys()])
        LOG.debug("Returning list of logs: %s", result)
        return result

    def guest_log_publish(self, context, log_name, disable):
        LOG.debug("publishing guest log %s (disable=%s)." %
                  (log_name, disable))
        self.guest_log_context = context
        gl_cache = self.guest_log_cache
        if log_name in gl_cache:
            if gl_cache[log_name].type == guest_log.LogType.USER:
                requires_change = (
                    (gl_cache[log_name].enabled and disable) or
                    (not gl_cache[log_name].enabled and not disable))
                if requires_change:
                    self.guest_log_enable(context, log_name, disable)
                    gl_cache[log_name].enabled = not disable
            return gl_cache[log_name].publish_log(disable)
        else:
            raise exception.NotFound("Log '%s' is not defined." % log_name)

    def guest_log_enable(self, context, log_name, disable):
        """This method can be overridden by datastore implementations to
        facilitate enabling and disabling USER type logs.  If the logs
        can be enabled with simple configuration group changes, however,
        the code here will probably suffice.
        """
        if self.configuration_manager:
            prefix = ("Dis" if disable else "En")
            LOG.debug("%sabling log '%s'" % (prefix, log_name))
            gl_def = self.guest_log_defs[log_name]
            enable_cfg_label = "%s_%s_log" % (self.GUEST_LOG_ENABLE_LABEL,
                                              log_name)
            disable_cfg_label = "%s_%s_log" % (self.GUEST_LOG_DISABLE_LABEL,
                                               log_name)
            if disable:
                self._apply_log_overrides(
                    context, enable_cfg_label, disable_cfg_label,
                    gl_def.get(self.GUEST_LOG_DISABLE_LABEL),
                    gl_def.get(self.GUEST_LOG_SECTION_LABEL))
            else:
                self._apply_log_overrides(
                    context, disable_cfg_label, enable_cfg_label,
                    gl_def.get(self.GUEST_LOG_ENABLE_LABEL),
                    gl_def.get(self.GUEST_LOG_SECTION_LABEL))

    def _apply_log_overrides(self, context, remove_label,
                             apply_label, cfg_values, section_label):
        self.configuration_manager.remove_system_override(
            change_id=remove_label)
        if cfg_values:
            config_man_values = cfg_values
            if section_label:
                config_man_values = {section_label: cfg_values}
            self.configuration_manager.apply_system_override(
                config_man_values, change_id=apply_label)
            self.apply_overrides(context, cfg_values)

    def build_log_file_name(self, log_name, owner, datastore_dir=None):
        """Build a log file name based on the log_name and make sure the
        directories exist and are accessible by owner.
        """
        if datastore_dir is None:
            base_dir = self.GUEST_LOG_BASE_DIR
            if not operating_system.exists(base_dir, is_directory=True):
                operating_system.create_directory(
                    base_dir, user=owner, group=owner, force=True,
                    as_root=True)
            datastore_dir = guestagent_utils.build_file_path(
                base_dir, self.GUEST_LOG_DATASTORE_DIRNAME)

        if not operating_system.exists(datastore_dir, is_directory=True):
            operating_system.create_directory(
                datastore_dir, user=owner, group=owner, force=True,
                as_root=True)
        log_file_name = guestagent_utils.build_file_path(
            datastore_dir, '%s-%s.log' % (self.manager, log_name))

        return self.validate_log_file(log_file_name, owner)

    def validate_log_file(self, log_file, owner):
        """Make sure the log file exists and is accessible by owner.
        """
        if not operating_system.exists(log_file):
            operating_system.write_file(log_file, '', as_root=True)

        operating_system.chown(log_file, user=owner, group=owner,
                               as_root=True)
        operating_system.chmod(log_file, FileMode.ADD_USR_RW_GRP_RW_OTH_R,
                               as_root=True)
        return log_file
