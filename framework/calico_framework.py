#!/usr/bin/env python

# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""calico-dcos-installer-scheduler

Install calico in a DCOS cluster

Usage:
  calico_framework.py

Dockerized:
  docker run calico/calico-mesos-framework <args...>

Description:
  Add or remove containers to Calico networking, manage their IP addresses and profiles.
  All these commands must be run on the host that contains the container.
"""
import os
import mesos.interface
from mesos.interface import mesos_pb2
import mesos.native
from docopt import docopt
from calico_utils import _setup_logging
from tasks import (Task, TaskRunEtcdProxy, TaskInstallDockerClusterStore,
                   TaskInstallNetmodules, TaskRestartComponents,
                   TaskRunCalicoNode, TaskRunCalicoLibnetwork)
from constants import LOGFILE

_log = _setup_logging(LOGFILE)
NEXT_AVAILABLE_TASK_ID = 0


# TODO
# o  Need to check CPU/Mem for each calico task
# o  How do we get a list of tasks that are already running for a particular framework
#    when a framework is restarted.
# o  Need to handle versioning
# o  Check that a task can reboot the agent (quick test to make sure this will fly)
# o  Can we start a task with the same task ID of a task that is running, or failed, or completed etc?
#    or do we need to delete a task with the same ID first.
# o  What happens if a framework is killed

TASK_CLASSES = [TaskRunEtcdProxy, TaskInstallNetmodules,
                TaskInstallDockerClusterStore, TaskRestartComponents,
                TaskRunCalicoNode, TaskRunCalicoLibnetwork]

class Agent(object):
    def __init__(self, agent_id):
        self.agent_id = agent_id
        """
        The agent ID.
        """

        self.tasks = {cls.name: None for cls in TASK_CLASSES}
        """
        Tasks for each task type (we only ever have one of each running on an
        agent.
        """

        self.restarting = False
        """
        Whether this agent has initiated a restart sequence.  Once set, this
        is reset when a restart is no longer required.
        """

    def __repr__(self):
        return "Agent(%s)" % self.id

    def handle_offer(self, offer):
        """
        Ask the agent if it wants to handle the supplied offer.
        :return:  None if the offer is not accepted, otherwise return the task
                  that needs scheduling.

        Installation tasks need to be performed in a particular order:
        -  Firstly etcd proxy needs to be running.  In parallel with this we
           can install netmodules (with Calico plugin).
        -  Once etcd is installed we can update the Docker configuration to
           use etcd as its cluster store (we don't need to wait for the
           netmodules install to complete).
        -  If the netmodules or docker tasks indicated that a restart is
           required then restart the appropriate componenets.  See note below.
        -  Once Docker and Agent are restarted, we can spin up the Calico
           node and the Calico libnetwork driver.

        A note on component restarts
        ============================

        Whether or not we restart anything is handled by the install tasks for
        docker multihost networking and for netmodules.  We could make the
        restart check to see if anything needs restarting - and simply no-op if
        nothing needs restarting.  However, since we want to limit how many
        agents are restarting at any one time, this slows down how quickly we
        can perform the subsequent installation tasks.  Instead, we have the
        install tasks indicate whether a restart is required.  If a restart is
        required we will do the restart, otherwise we won't - thus agents that
        don't need a restart will not get blocked behind an agent installation
        that does require a restart.

        Since we are possibly restarting docker and/or the agent, we
        need to consider what happens to these tasks when the services
        restart.

        1) Restarting Docker may cause the framework to exit (since it is
        running as a docker container).  If that is the case the framework
        will be restarted and will kick off the install sequence again on each
        agent.  Once installed they will not be re-installed (and therefore
        the systems will not be restarted).

        2) Restarting the agent could also cause the framework to exit in which
        case the same logic applies as for a Docker restart.  Also, the task
        that the agent was running may appear as failed.  If we get a failed
        notification from a restart task, we re-run the installation tasks.

        In both cases, once a restart has successfully applied the new config
        the tasks ensure that we do not restart again - so we will not end up
        in a restart loop.
        """
        if self.task_can_be_offered(TaskRunEtcdProxy, offer):
            # We have no etcd task running - start one now.
            _log.info("Install and run etcd proxy")
            return self.new_task(TaskRunEtcdProxy)

        if self.task_can_be_offered(TaskInstallNetmodules, offer):
            # We have not yet installed netmodules - do that now (we don't need
            # to wait for etcd to come online).
            _log.info("Install or check netmodules")
            return self.new_task(TaskInstallNetmodules)

        if self.task_running(TaskRunEtcdProxy):
            # We need to wait for the proxy to come online before continuing.
            _log.info("Waiting for etcd proxy to be healthy")
            return None

        if self.task_can_be_offered(TaskInstallDockerClusterStore, offer):
            # Etcd proxy is running, so lets make sure Docker is configured to
            # use etcd as its cluster store.
            _log.info("Install or check docker multihost networking config")
            return self.new_task(TaskInstallDockerClusterStore)

        if not self.task_finished(TaskInstallNetmodules):
            # If we are waiting for successful completion of the netmodules
            # install then do not continue.
            _log.info("Waiting for netmodules installation")
            return None

        if not self.task_finished(TaskInstallDockerClusterStore):
            # If we are waiting for successful completion of the docker multi
            # host networking install then do not continue.
            _log.info("Waiting for docker networking installation")
            return None

        # Determine if a restart is required.
        # -  If a restart is required, kick off the restart task.
        # -  Otherwise, make sure our restarting flag is reset, and continue
        #    with the rest of the installation.
        restart_required = \
            self.tasks[TaskInstallNetmodules].restart_required() or \
            self.tasks[TaskInstallDockerClusterStore].restart_required()
        if not restart_required:
            self.restarting = False
        if restart_required and self.task_can_be_offered(TaskRestartComponents, offer):
            # We require a restart and we haven't already scheduled one.
            _log.info("Schedule a restart task")
            return self.new_task(TaskRestartComponents,
                                 restart_agent=self.tasks[TaskInstallNetmodules].restart_required(),
                                 restart_docker=self.tasks[TaskInstallDockerClusterStore].restart_required())

        # At this point we only continue when a restart is no longer required.
        if restart_required:
            # Still require a restart to complete (or fail).
            _log.info("Waiting for restart to be scheduled or to complete")
            return None

        # If necessary start Calico node and Calico libnetwork driver
        if self.task_can_be_offered(TaskRunCalicoNode, offer):
            # Calico node is not running, start it up.
            _log.info("Start Calico node")
            return self.new_task(TaskRunCalicoNode)

        if self.task_can_be_offered(TaskRunCalicoLibnetwork, offer):
            # Calico libnetwork driver is not running, start it up.
            _log.info("Start Calico libnetwork driver")
            return self.new_task(TaskRunCalicoLibnetwork)

        return None

    def new_task(self, task_class, *args, **kwargs):
        """
        Create a new Task of the supplied type, and update our cache to store
        the task.
        :param task_class:
        :return: The new task.
        """
        task = task_class(self, *args, **kwargs)
        self.tasks[task.name] = task
        return task

    def task_can_be_offered(self, task_class, offer):
        """
        Whether a task can be included in the offer request.  A task can be
        included when the task type needs to be scheduled (see
        task_needs_scheduling) and the task resource requirements are fulfilled
        by the offer.
        :param task_class:
        :param offer:
        :return:
        """
        needs_scheduling = self.task_needs_scheduling(task_class)
        return needs_scheduling and task_class.can_accept_offer(offer)

    def task_needs_scheduling(self, task_class):
        """
        Whether a task needs scheduling.  A task needs scheduling if it has not
        yet been run, or if it has run and failed.  Whether a task has failed
        depends on whether the task type is persistent (i.e. always supposed to
        be running)
        :param task_class:
        :return: True if the task needs scheduling.  False otherwise.
        """
        task = self.tasks[task_class.name]
        if not task:
            return True
        if task.persistent:
            return not task.running()
        else:
            return task.failed()

    def task_running(self, task_class):
        """
        Return if a task is running or not.
        :param task_class:
        :return:
        """
        task = self.tasks[task_class.name]
        if not task:
            return False
        return task.running()

    def task_finished(self, task_class):
        """
        Return if a task is finished or not.
        :param task_class:
        :return:
        """
        task = self.tasks[task_class.name]
        if not task:
            return False
        return task.finished()

    def handle_update(self, update):
        """

        :param update:
        :return:
        """
        # Extract the task name from the update and update the appropriate
        # task.  Updates for the restart task need special case processing
        name = Task.name_from_task_id(update.task_id)

        # Lookup the existing task, if there is one.  There should always
        # be an entry in the dictionary, but it might be None if this instance
        # of the framework did not schedule it.
        task = self.tasks[name]
        if not task:
            task_class = next(cls for cls in TASK_CLASSES if cls.name == name)
            task = self.new_task(task_class)
        task.update(update)

        # If the task is not running, make sure the task is deleted so that it
        # can be re-scheduled when necessary.
        #TODO

        # An update to indicate a restart task is no longer running requires
        # some additional processing to re-spawn the install tasks as this
        # ensures the installation completed successfully.
        if (name == TaskRestartComponents.name) and not task.running():
            _log.debug("Handle update for restart task")
            self.tasks[TaskInstallNetmodules.name] = None
            self.tasks[TaskInstallDockerClusterStore] = None


class CalicoScheduler(mesos.interface.Scheduler):
    def __init__(self, max_concurrent_restarts=1):
        self.agents = {}
        self.max_concurrent_restart = max_concurrent_restarts

    def can_restart(self, agent):
        """
        Determine if we are allowed to trigger an agent restart.  We rate
        limit the number of restarts that we allow at any given time.
        :param agent:  The agent that is requesting a restart.
        :return: True if allowed, False otherwise.
        """
        if agent.restarting:
            _log.debug("Allowed to restart agent as already restarting")
            return True
        num_restarting = sum(1 for a in self.agents if a.restarting)
        return num_restarting < self.max_num_concurrent_restart

    def registered(self, driver, frameworkId, masterInfo):
        """
        Callback used when the framework is successfully registered.
        """
        _log.info("REGISTERED: with framework ID %s", frameworkId.value)

    def get_agent(self, agent_id):
        """
        Return the Agent based on the agent ID.  If the agent is not in our
        cache then create an entry for it.
        :param agent_id:
        :return:
        """
        agent = self.agents.get(agent_id)
        if not agent:
            agent = Agent(agent_id)
            self.agents[agent_id] = agent
        return agent

    def resourceOffers(self, driver, offers):
        """
        Triggered when the framework is offered resources by mesos.
        """
        # Extract the task ID.  The format of the ID includes the ID of the
        # agent it is running on.
        for offer in offers:
            agent = self.get_agent(offer.slave_id.value)
            task = agent.handle_offer(offer)
            if not task:
                driver.declineOffer(offer.id)
                continue

            print "Launching Task %s" % task
            operation = mesos_pb2.Offer.Operation()
            operation.launch.task_infos.extend([task.as_new_mesos_task()])
            operation.type = mesos_pb2.Offer.Operation.LAUNCH
            driver.acceptOffers([offer.id], [operation])

    def statusUpdate(self, driver, update):
        """
        Triggered when the Framework receives a task Status Update from the
        Executor
        """
        # Extract the task ID.  The format of the ID includes the ID of the
        # agent it is running on.
        agent = self.get_agent(update.slave_id)
        agent.handle_update(update)


class NotEnoughResources(Exception):
    pass


if __name__ == "__main__":
    # arguments = docopt(__doc__)
    master_ip = os.getenv('MASTER_IP', 'mesos.master')
    print "Connecting to Master: ", master_ip

    framework = mesos_pb2.FrameworkInfo()
    framework.user = ""  # Have Mesos fill in the current user.
    framework.name = "Calico installation framework"
    framework.principal = "calico-installation-framework"

    scheduler = CalicoScheduler()

    _log.info("Launching")
    driver = mesos.native.MesosSchedulerDriver(scheduler,
                                               framework,
                                               master_ip)
    driver.start()
    driver.join()
