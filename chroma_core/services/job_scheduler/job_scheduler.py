# Copyright (c) 2018 DDN. All rights reserved.
# Use of this source code is governed by a MIT-style
# license that can be found in the LICENSE file.


import json
import threading
import sys
import traceback
import time
from django.core.exceptions import ObjectDoesNotExist

import os
import operator
import itertools
from collections import defaultdict
import Queue
from copy import deepcopy
from chroma_core.lib.util import all_subclasses
from chroma_core.services import dbutils


from django.contrib.contenttypes.models import ContentType
from django.db import transaction, DEFAULT_DB_ALIAS
from django.db.models import Q
import django.utils.timezone

from chroma_core.lib.cache import ObjectCache
from chroma_core.models.server_profile import ServerProfile
from chroma_core.models import Command
from chroma_core.models import StateLock
from chroma_core.models import ManagedHost
from chroma_core.models import ManagedMdt
from chroma_core.models import FilesystemMember
from chroma_core.models import ConfigureLNetJob
from chroma_core.models import ManagedTarget, ApplyConfParams, ManagedOst, Job, DeletableStatefulObject
from chroma_core.models import StepResult
from chroma_core.models import ManagedMgs, ManagedFilesystem, NetworkInterface, LNetConfiguration
from chroma_core.models import ManagedTargetMount, VolumeNode
from chroma_core.models import DeployHostJob, UpdatesAvailableAlert, LustreClientMount, Copytool
from chroma_core.models import CorosyncConfiguration
from chroma_core.models import Corosync2Configuration
from chroma_core.models import PacemakerConfiguration
from chroma_core.models import ConfigureHostFencingJob
from chroma_core.models import TriggerPluginUpdatesJob
from chroma_core.services.job_scheduler.dep_cache import DepCache
from chroma_core.services.job_scheduler.lock_cache import LockCache
from chroma_core.services.job_scheduler.command_plan import CommandPlan
from chroma_core.services.job_scheduler.agent_rpc import AgentException
from chroma_core.services.plugin_runner.agent_daemon_interface import AgentDaemonRpcInterface
from chroma_core.services.rpc import RpcError
from chroma_core.services.log import log_register
from disabled_connection import DISABLED_CONNECTION
from iml_common.lib.date_time import IMLDateTime

from chroma_help.help import help_text

import chroma_core.lib.conf_param

log = log_register(__name__.split(".")[-1])


class NotificationBuffer(object):
    """
    Provides a simple buffer for notifications which would otherwise be
    dropped due to lock contention on the notified item.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._notifications = defaultdict(Queue.Queue)

    def add_notification_for_key(self, key, notification):
        with self._lock:
            self._notifications[key].put(notification)

    @property
    def notification_keys(self):
        with self._lock:
            return self._notifications.keys()

    def clear_notifications_for_key(self, key):
        with self._lock:
            del (self._notifications[key])

    def drain_notifications_for_key(self, key):
        """
        :return: A list of argument lists for use in JobScheduler._notify
        """
        notifications = []
        with self._lock:
            while not self._notifications[key].empty():
                notifications.append(self._notifications[key].get())
            del (self._notifications[key])

        # For multiple notifications affecting the same set of attributes, drop all but the latest
        seen_attr_tuples = set()
        notifications.reverse()
        trimmed_notifications = []
        for notification in notifications:
            update_attrs = notification[3]
            attr_tuple = tuple(sorted(update_attrs.keys()))
            if attr_tuple in seen_attr_tuples:
                # There was a later update to this set of attributes, skip
                continue
            else:
                trimmed_notifications.append(notification)
                seen_attr_tuples.add(attr_tuple)
        trimmed_notifications.reverse()

        return trimmed_notifications


class SimpleConnectionQuota(object):
    """
    This class provides a way to limit the total number of DB connections
    used by a population of threads.

    It is *not* a pool: the connections are destroyed and created every time.

    It's for when threads need to briefly dip into a period of database access
    before giving it up again.
    """

    def __init__(self, max_connections):
        self._semaphore = threading.Semaphore(max_connections)
        self.db_alias = DEFAULT_DB_ALIAS
        self.database = django.db.connections.databases[self.db_alias]

    def acquire(self):
        self._semaphore.acquire()
        if django.db.connection.connection == DISABLED_CONNECTION:
            django.db.connection.connection = None
        # connection = load_backend(self.database['ENGINE']).DatabaseWrapper(self.database, self.db_alias)
        # return connection

    def release(self, connection):
        # Close the connection if present, and hand back our token
        if django.db.connection.connection:
            _disable_database()

        self._semaphore.release()


def _disable_database():
    if django.db.connection.connection is not None and django.db.connection.connection != DISABLED_CONNECTION:
        django.db.connection.close()
    django.db.connection.connection = DISABLED_CONNECTION


class JobProgress(threading.Thread, Queue.Queue):
    """
    A thread and a queue for handling progress/completion information
    from RunJobThread
    """

    def __init__(self, job_scheduler):
        threading.Thread.__init__(self)
        Queue.Queue.__init__(self)

        self._job_scheduler = job_scheduler

        self._stopping = threading.Event()
        self._job_to_result = {}

    def run(self):
        while not self._stopping.is_set():
            try:
                self._handle(self.get(block=True, timeout=1))
            except Queue.Empty:
                pass

        for msg in self.queue:
            self._handle(msg)

    def _handle(self, msg):
        fn = getattr(self, "_%s" % msg[0])

        fn(*msg[1], **msg[2])

    def stop(self):
        self._stopping.set()

    def complete_job(self, job_id, errored):
        dbutils.exit_if_in_transaction(log)
        self.put(("complete_job", (job_id, errored), {}))

    def __getattr__(self, name):
        if not name.startswith("_"):
            # Throw an exception if there isn't an underscored method to
            # handle this
            self.__getattr__("_%s" % name)

            def getter(*args, **kwargs):
                dbutils.exit_if_in_transaction(log)
                log.info("putting: {} on the queue".format(name))

                try:
                    self.put(deepcopy((name, args, kwargs)))
                except RuntimeError as e:
                    from remote_pdb import RemotePdb
                    RemotePdb("127.0.0.1", 4444).set_trace()

            return getter

    def _complete_job(self, job_id, errored):
        self._job_scheduler.complete_job(job_id, errored=errored)

    def _advance(self):
        self._job_scheduler.advance()

    def _start_step(self, job_id, **kwargs):
        with transaction.atomic():
            result = StepResult(job_id=job_id, **kwargs)
            result.save()
        self._job_to_result[job_id] = result

    def _log(self, job_id, log_string):
        result = self._job_to_result[job_id]
        with transaction.atomic():
            result.log += log_string
            result.save()

    def _console(self, job_id, log_string):
        result = self._job_to_result[job_id]
        with transaction.atomic():
            result.console += log_string
            result.save()

    def _step_failure(self, job_id, backtrace):
        result = self._job_to_result[job_id]
        with transaction.atomic():
            result.state = "failed"
            result.backtrace = backtrace
            result.save()

    def _step_success(self, job_id, step_result):
        result = self._job_to_result[job_id]
        with transaction.atomic():
            result.state = "success"
            result.result = json.dumps(step_result)
            result.save()


class RunJobThread(threading.Thread):
    CANCEL_TIMEOUT = 30

    def __init__(self, job_progress, connection_quota, job, steps):
        super(RunJobThread, self).__init__()
        self.job = job
        self._job_progress = job_progress
        self._connection_quota = connection_quota
        self._cancel = threading.Event()
        self._complete = threading.Event()
        self.steps = steps

    def cancel(self):
        log.info("Job %s: cancelling" % self.job.id)
        self._cancel.set()
        log.info("Job %s: waiting %ss for run to complete" % (self.job.id, self.CANCEL_TIMEOUT))

    def cancel_complete(self):
        self._complete.wait(self.CANCEL_TIMEOUT)
        if self._complete.is_set():
            log.info("Job %s: cancel completed" % self.job.id)
        else:
            # HYD-1485: Get a mechanism to interject when the thread is blocked on an agent call
            log.error("Job %s: cancel timed out, will continue as zombie thread!" % self.job.id)

    def run(self):
        if django.db.connection.connection:
            log.error("RunJobThread started with a DB connection!")

        try:
            self._run()
            self._complete.set()
        except Exception:
            log.critical("Unhandled exception in RunJobThread: %s" % traceback.format_exc())
            # Better to die clean than live on dirty (an unhandled exception
            # could mean that we will leave Command/Job/StepResult objects in
            # a bad/never-completing state).
            os._exit(-1)

        if django.db.connection.connection and django.db.connection.connection != DISABLED_CONNECTION:
            django.db.connection.close()

    def _run(self):
        log.info("Job %d: %s.run" % (self.job.id, self.__class__.__name__))

        step_index = 0
        finish_step = -1
        while step_index < len(self.steps) and not self._cancel.is_set():
            klass, args = self.steps[step_index]

            # Do not persist any sensitive arguments (prefixed with __)
            clean_args = dict([(k, v) for k, v in args.items() if not k.startswith("__")])

            self._job_progress.start_step(
                self.job.id, step_klass=klass, args=clean_args, step_index=step_index, step_count=len(self.steps)
            )

            step = klass(
                self.job,
                args,
                lambda l: self._job_progress.log(self.job.id, l),
                lambda c: self._job_progress.console(self.job.id, c),
                self._cancel,
            )

            try:
                if step.database:
                    # Get a token entitling code running in this thread
                    # to open a database connection when it chooses to
                    self._connection_quota.acquire()
                else:
                    _disable_database()

                log.debug("Job %d running step %d" % (self.job.id, step_index))
                result = step.run(args)
                log.debug("Job %d step %d successful result %s" % (self.job.id, step_index, result))

                self._job_progress.step_success(self.job.id, result)
            except AgentException as e:
                log.error("Job %d step %d encountered an agent error: %s" % (self.job.id, step_index, e.backtrace))

                # Don't bother storing the backtrace to invoke_agent, the interesting part
                # is the backtrace inside the AgentException
                self._job_progress.step_failure(self.job.id, e.backtrace)
                self._job_progress.complete_job(self.job.id, errored=True)
                return
            except Exception as e:
                backtrace = traceback.format_exc()
                log.error("Job %d step %d encountered an error: %s:%s" % (self.job.id, step_index, e, backtrace))

                self._job_progress.step_failure(self.job.id, backtrace)
                self._job_progress.complete_job(self.job.id, errored=True)
                return
            finally:
                if step.database:
                    log.debug("Job %d releasing database connection" % self.job.id)
                    self._connection_quota.release(django.db.connection.connection)

            finish_step = step_index
            step_index += 1

        if self._cancel.is_set():
            return

        log.info("Job %d finished %d steps successfully" % (self.job.id, finish_step + 1))

        self._job_progress.complete_job(self.job.id, errored=False)


class JobCollection(object):
    def __init__(self):
        self.flush()

    def flush(self):
        self._state_jobs = {"pending": {}, "tasked": {}, "complete": {}}
        self._jobs = {}
        self._commands = {}

        self._command_to_jobs = defaultdict(set)
        self._job_to_commands = defaultdict(set)

    def add(self, job):
        self._jobs[job.id] = job
        self._state_jobs[job.state][job.id] = job

    def add_command(self, command, jobs):
        """Add command if it doesn't already exist, and ensure that all
        of `jobs` are associated with it

        """
        for job in jobs:
            self.add(job)
        self._commands[command.id] = command
        self._command_to_jobs[command.id] |= set([j.id for j in jobs])
        for job in jobs:
            self._job_to_commands[job.id].add(command.id)

    def get(self, job_id):
        return self._jobs[job_id]

    def update(self, job, new_state, **kwargs):
        initial_state = job.state

        Job.objects.filter(id=job.id).update(state=new_state, **kwargs)
        job.state = new_state
        for attr, val in kwargs.items():
            setattr(job, attr, val)

        try:
            del self._state_jobs[initial_state][job.id]
        except KeyError:
            log.warning("Cancelling uncached Job %s" % job.id)
        else:
            self._state_jobs[job.state][job.id] = job

    def update_commands(self, job):
        """
        Update any commands which relate to this job (complete the command if all its jobs are complete)
        """
        if job.state == "complete":
            for command_id in self._job_to_commands[job.id]:
                jobs = [self._jobs[job_id] for job_id in self._command_to_jobs[command_id]]
                if set([j.state for j in jobs]) == set(["complete"]) or len(jobs) == 0:
                    # Mark the command as complete
                    errored = True in [j.errored for j in jobs]
                    cancelled = True in [j.cancelled for j in jobs]
                    log.debug(
                        "Completing command %s (%s, %s) as a result of completing job %s"
                        % (command_id, errored, cancelled, job.id)
                    )
                    if errored or cancelled:
                        for job in jobs:
                            log.debug("Command %s job %s: %s %s" % (command_id, job.id, job.errored, job.cancelled))
                    self._commands[command_id].completed(errored, cancelled)
                    # Command.objects.filter(pk = command_id).update(errored = errored, cancelled = cancelled, complete = True)

    def update_many(self, jobs, new_state):
        for job in jobs:
            del self._state_jobs[job.state][job.id]
            job.state = new_state
            self._state_jobs[job.state][job.id] = job

        Job.objects.filter(id__in=[j.id for j in jobs]).update(state=new_state)

    @property
    def ready_jobs(self):
        result = []
        for job in self._state_jobs["pending"].values():
            wait_for_ids = json.loads(job.wait_for_json)
            complete_job_ids = [j.id for j in self._state_jobs["complete"].values()]
            if not set(wait_for_ids) - set(complete_job_ids):
                result.append(job)

        if len(result) == 0 and len(self.pending_jobs) == 0 and len(self.tasked_jobs) == 0:
            # A quiescent state, flush the collection (avoid building up an indefinitely
            # large collection of complete jobs)
            log.debug("%s.flush" % (self.__class__.__name__))
            self.flush()

        return result

    @property
    def pending_jobs(self):
        return self._state_jobs["pending"].values()

    @property
    def tasked_jobs(self):
        return self._state_jobs["tasked"].values()


class JobScheduler(object):
    """A single instance of this class is created within the `job_scheduler` service.

    It is on the receiving end of RPCs (JobSchedulerRpc) and also is called
    by the handler for NotificationQueue


    """

    MAX_STEP_DB_CONNECTIONS = 10

    def __init__(self):
        self._lock = threading.RLock()
        """Globally serialize all scheduling operations: within a given cluster, they all potentially
        interfere with one another.  In the future, if this class were handling multiple isolated
        clusters then they could take a lock each and run in parallel

        """

        self._lock_cache = LockCache()
        self._job_collection = JobCollection()
        self._notification_buffer = NotificationBuffer()

        self._db_quota = SimpleConnectionQuota(self.MAX_STEP_DB_CONNECTIONS)
        self._run_threads = {}  # Map of job ID to RunJobThread

        self.progress = JobProgress(self)

        # This is an actual list of hooks, so that we can actually have completion hooks in our code. Basic today
        # but maybe improved in the future.
        self.completion_hooks = []

    def join_run_threads(self):
        for job_id, thread in self._run_threads.items():
            log.info("Joining thread for job %s" % job_id)
            thread.join()

    def _run_next(self):
        ready_jobs = self._job_collection.ready_jobs

        log.info(
            "run_next: %d runnable jobs of (%d pending, %d tasked)"
            % (len(ready_jobs), len(self._job_collection.pending_jobs), len(self._job_collection.tasked_jobs))
        )

        dep_cache = DepCache()
        ok_jobs, cancel_jobs = self._check_jobs(ready_jobs, dep_cache)

        for job in cancel_jobs:
            self._complete_job(job, False, True)

        self._job_collection.update_many(ok_jobs, "tasked")
        for job in ok_jobs:
            self._spawn_job(job)

        if cancel_jobs:
            # Cancellations may have made some jobs ready, run me again
            self._run_next()

    def _check_jobs(self, jobs, dep_cache):
        """Return the list of jobs which pass their checks"""
        ok_jobs = []
        cancel_jobs = []

        for job in jobs:
            try:
                deps_satisfied = job._deps_satisfied(dep_cache)
            except Exception:
                # Catchall exception handler to ensure progression even if Job
                # subclasses have bugs in their get_deps etc.
                log.error(
                    "Job %s: exception in dependency check: %s"
                    % (job.id, "\n".join(traceback.format_exception(*(sys.exc_info()))))
                )
                cancel_jobs.append(job)
            else:
                if not deps_satisfied:
                    log.warning("Job %d: cancelling because of failed dependency" % job.id)
                    cancel_jobs.append(job)
                    # TODO: tell someone WHICH dependency
                else:
                    try:
                        job.steps = job.get_steps()
                    except Exception:
                        log.error("Job %d: exception in get_steps: %s" % (job.id, traceback.format_exc()))
                        cancel_jobs.append(job)
                    else:
                        ok_jobs.append(job)

        return ok_jobs, cancel_jobs

    def _spawn_job(self, job):
        # NB job.steps was decorated onto job in _check_jobs, because that's where we need to handle any exceptions from it
        # NB we call get_steps in here rather than RunJobThread so that steps can be composed using DB operations
        # without having a database connection for each RunJobThread

        if job.steps:
            thread = RunJobThread(self.progress, self._db_quota, job, job.steps)
            assert job.id not in self._run_threads
            self._run_threads[job.id] = thread

            thread.start()
            log.debug("_spawn_job: %s threads in flight" % len(self._run_threads))
        else:
            log.debug("_spawn_job: No steps for %s, completing" % job.pk)
            # No steps: skip straight to completion
            self.progress.complete_job(job.id, False)

    def _complete_job(self, job, errored, cancelled):
        try:
            del self._run_threads[job.id]
        except KeyError:
            pass

        log.debug("_complete_job: %s threads in flight" % len(self._run_threads))

        log.info("Job %s completing (errored=%s, cancelled=%s)" % (job.id, errored, cancelled))

        try:
            command = Command.objects.filter(jobs=job, complete=False)[0]
        except IndexError:
            log.warning("Job %s: No incomplete command while completing" % job.pk)
            command = None

        if errored:
            job.on_error()

        self._job_collection.update(job, "complete", errored=errored, cancelled=cancelled)

        locks = json.loads(job.locks_json)

        # Update _lock_cache to remove the completed job's locks
        self._lock_cache.remove_job(job)

        # Check for completion callbacks on anything this job held a writelock on
        for lock in locks:
            if lock["write"]:
                lock = StateLock.from_dict(job, lock)
                log.debug("Job %s completing, held writelock on %s" % (job.pk, lock.locked_item))
                try:
                    self._completion_hooks(lock.locked_item, command)
                except Exception:
                    log.error(
                        "Error in completion hooks: %s" % "\n".join(traceback.format_exception(*(sys.exc_info())))
                    )

        # Do this last so that the state of the command reflects both the completion
        # of this job and any new jobs added by completion hooks
        self._job_collection.update_commands(job)

    def add_completion_hook(self, addition):
        self.completion_hooks.append(addition)

    def del_completion_hook(self, deletion):
        self.completion_hooks.remove(deletion)

    def _completion_hooks(self, changed_item, command=None, updated_attrs=[]):
        """
        :param command: If set, any created jobs are added
        to this command object.
        """
        log.debug(
            "_completion_hooks command %s, %s (%s) state=%s"
            % (
                None if command is None else command.id,
                changed_item,
                changed_item.__class__,
                getattr(changed_item, "state", "n/a"),
            )
        )

        for hook in self.completion_hooks:
            hook(changed_item, command, updated_attrs)

        def running_or_failed(klass, **kwargs):
            """Look for jobs of the same type with the same params, either incomplete (don't start the job because
            one is already pending) or complete in the same command (don't start the job because we already tried and failed)"""
            if command:
                count = klass.objects.filter(~Q(state="complete") | Q(command=command), **kwargs).count()
            else:
                count = klass.objects.filter(~Q(state="complete"), **kwargs).count()

            return bool(count)

        if isinstance(changed_item, ManagedTarget):
            if issubclass(changed_item.downcast_class, FilesystemMember):
                affected_filesystems = [changed_item.downcast().filesystem]
            else:
                affected_filesystems = changed_item.downcast().managedfilesystem_set.all()

            for filesystem in affected_filesystems:
                members = filesystem.get_targets()
                states = set([t.state for t in members])
                now = django.utils.timezone.now()

                if (
                    not filesystem.state == "available"
                    and changed_item.state in ["mounted", "removed"]
                    and states == set(["mounted"])
                ):
                    self._notify(
                        ContentType.objects.get_for_model(filesystem).natural_key(),
                        filesystem.id,
                        now,
                        {"state": "available"},
                        ["stopped", "unavailable"],
                    )
                if changed_item.state == "unmounted" and filesystem.state != "stopped" and states == set(["unmounted"]):
                    self._notify(
                        ContentType.objects.get_for_model(filesystem).natural_key(),
                        filesystem.id,
                        now,
                        {"state": "stopped"},
                        ["stopped", "unavailable"],
                    )
                if changed_item.state == "unmounted" and filesystem.state == "available" and states != set(["mounted"]):
                    self._notify(
                        ContentType.objects.get_for_model(filesystem).natural_key(),
                        filesystem.id,
                        now,
                        {"state": "unavailable"},
                        ["available"],
                    )

        if isinstance(changed_item, ManagedHost):
            # Sometimes we have been removed and yet some stray messages are hanging about, I don't think this should be
            # dealt with at this level, but for now to get 2.1 out the door I will do so.
            if not changed_item.not_deleted:
                return command

            if "ha_cluster_peers" in updated_attrs:
                try:
                    AgentDaemonRpcInterface().rebalance_host_volumes(changed_item.id)
                except RpcError:
                    log.error("Host volumes failed to rebalance: " + traceback.format_exc())

            if "needs_update" in updated_attrs:
                UpdatesAvailableAlert.notify(changed_item, changed_item.needs_update)

        if isinstance(changed_item, ManagedTarget):
            # See if any MGS conf params need applying
            if issubclass(changed_item.downcast_class, FilesystemMember):
                mgs = changed_item.downcast().filesystem.mgs
            else:
                mgs = changed_item.downcast()

            if mgs.conf_param_version != mgs.conf_param_version_applied:
                if not running_or_failed(ApplyConfParams, mgs=mgs.managedtarget_ptr):
                    with transaction.atomic():
                        job = ApplyConfParams(mgs=mgs.managedtarget_ptr)
                        if DepCache().get(job).satisfied():
                            if not command:
                                command = Command.objects.create(
                                    message="Updating configuration parameters on %s" % mgs
                                )
                            self.CommandPlan.add_jobs([job], command)

            # Update TargetFailoverAlert from .active_mount
            from chroma_core.models import TargetFailoverAlert

            failed_over = (
                changed_item.active_mount is not None
                and changed_item.active_mount != changed_item.managedtargetmount_set.get(primary=True)
            )
            TargetFailoverAlert.notify(changed_item, failed_over)

        if isinstance(changed_item, PacemakerConfiguration) and "reconfigure_fencing" in updated_attrs:
            with transaction.atomic():
                job = ConfigureHostFencingJob(host=changed_item.host)
                if not command:
                    command = Command.objects.create(message="Configuring fencing agent on %s" % changed_item)
                self.CommandPlan.add_jobs([job], command)

    def _drain_notification_buffer(self):
        # Give any buffered notifications a chance to drain out
        for buffer_key in self._notification_buffer.notification_keys:
            content_type, object_id = buffer_key
            model_klass = ContentType.objects.get_by_natural_key(*content_type).model_class()
            try:
                instance = model_klass.objects.get(pk=object_id).downcast()
            except model_klass.DoesNotExist:
                log.warning(
                    "_drain_notification_buffer: Dropping buffered notifications for not-found object %s/%s"
                    % buffer_key
                )
                self._notification_buffer.clear_notifications_for_key(buffer_key)
                continue

            # Try again later if the instance is still locked
            if self._lock_cache.get_by_locked_item(instance):
                continue

            notifications = self._notification_buffer.drain_notifications_for_key(buffer_key)

            log.info(
                "Replaying %d buffered notifications for %s-%s"
                % (len(notifications), model_klass.__name__, instance.pk)
            )
            for notification in notifications:
                log.debug("Replaying buffered notification: %s" % (notification,))
                self._notify(*notification)

    def set_state(self, object_ids, message, run):
        with self._lock:
            with transaction.atomic():
                command = self.CommandPlan.command_set_state(object_ids, message)
            if run:
                self.progress.advance()
        return command.id

    def advance(self):
        with self._lock:
            self._run_next()

    def _notify(self, content_type, object_id, notification_time, update_attrs, from_states):
        # Get the StatefulObject
        model_klass = ContentType.objects.get_by_natural_key(*content_type).model_class()
        try:
            instance = ObjectCache.get_by_id(model_klass, object_id)
        except model_klass.DoesNotExist:
            log.warning("_notify: Dropping update for not-found object %s/%s" % (content_type, object_id))
            return

        # Drop if it's not in an allowed state
        if from_states and instance.state not in from_states:
            log.info("_notify: Dropping update to %s because %s is not in %s" % (instance, instance.state, from_states))
            return

        # Drop state-modifying updates if outdated
        modified_at = instance.state_modified_at
        if "state" in update_attrs and notification_time <= modified_at:
            log.info("notify: Dropping update of %s (%s) because it has been updated since" % (instance.id, instance))
            return

        # Buffer updates on locked instances, except for state changes. By the
        # time a buffered state change notification would be replayed, the
        # state change would probably not make any sense.
        if self._lock_cache.get_by_locked_item(instance):
            if "state" in update_attrs:
                return

            log.info("_notify: Buffering update to %s because of locks" % instance)
            for lock in self._lock_cache.get_by_locked_item(instance):
                log.info("  %s" % lock)

            buffer_key = (tuple(content_type), object_id)
            notification = (content_type, object_id, notification_time, update_attrs, from_states)
            self._notification_buffer.add_notification_for_key(buffer_key, notification)

            return

        with transaction.atomic():
            for attr, value in update_attrs.items():
                old_value = getattr(instance, attr)
                if old_value == value:
                    log.debug("_notify: Dropping %s.%s = %s because it is already set" % (instance, attr, value))
                    continue

                log.info(
                    "_notify: Updating .%s of item %s (%s) from %s to %s"
                    % (attr, instance.id, instance, old_value, value)
                )
                if attr == "state":
                    # If setting the special 'state' attribute then maybe schedule some jobs
                    instance.set_state(value)
                else:
                    # If setting a normal attribute just write it straight away
                    setattr(instance, attr, value)
                    instance.save()
                    log.info(
                        "_notify: Set %s=%s on %s (%s-%s) and saved"
                        % (attr, value, instance, model_klass.__name__, instance.id)
                    )

            instance.save()

            # Foreign keys: annoyingly, if foo_id was 7, and we assign it to 8, then .foo will still be
            # the '7' instance, even after a save().  To be safe against any such strangeness, pull a
            # fresh instance of everything we update (this is safe because earlier we checked that nothing is
            # locking this object.
            instance = ObjectCache.update(instance)

        # FIXME: should check the new state against reverse dependencies
        # and apply any fix_states
        self._completion_hooks(instance, updated_attrs=update_attrs.keys())

    def notify(self, content_type, object_id, time_serialized, update_attrs, from_states):
        with self._lock:
            notification_time = IMLDateTime.parse(time_serialized)
            self._notify(content_type, object_id, notification_time, update_attrs, from_states)

            self._run_next()

    def run_jobs(self, job_dicts, message):
        with self._lock:
            result = self.CommandPlan.command_run_jobs(job_dicts, message)
            self.progress.advance()
            return result

    def get_transition_consequences(cls, stateful_object_class, stateful_object_id, new_state):
        """Query what the side effects of a state transition are.  Effectively does
        a dry run of scheduling jobs for the transition.

        The return format is like this:
        ::

            {
                'transition_job': <job dict>,
                'dependency_jobs': [<list of job dicts>]
            }
            # where each job dict is like
            {
                'class': '<job class name>',
                'requires_confirmation': <boolean, whether to prompt for confirmation>,
                'confirmation_prompt': <string, confirmation prompt>,
                'description': <string, description of the job>,
                'stateful_object_id': <ID of the object modified by this job>,
                'stateful_object_content_type_id': <Content type ID of the object modified by this job>
            }

        :param stateful_object: A StatefulObject instance
        :param new_state: Hypothetical new value of the 'state' attribute

        """
        # Corosync2Configuration is needed by the line below although not directly referenced,
        # so reference to remove warnings.
        Corosync2Configuration

        stateful_object = ObjectCache.get_by_id(
            getattr(sys.modules[__name__], stateful_object_class), stateful_object_id
        )

        return CommandPlan(LockCache(), None).get_transition_consequences(stateful_object, new_state)

    def cancel_job(self, job_id):
        cancelled_thread = None

        with self._lock:
            try:
                job = self._job_collection.get(job_id)
            except KeyError:
                # Job has been cleaned out of collection, therefore is complete
                # However, to avoid being too trusting, let's retrieve it and
                # let the following check for completeness happen
                job = Job.objects.get(pk=job_id)

            log.info("cancel_job: Cancelling job %s (%s)" % (job.id, job.state))
            if job.state == "complete":
                return
            elif job.state == "tasked":
                try:
                    cancelled_thread = self._run_threads[job_id]
                    cancelled_thread.cancel()
                except KeyError:
                    pass
                with transaction.atomic():
                    self._job_collection.update(job, "complete", cancelled=True)
                    self._job_collection.update_commands(job)
            elif job.state == "pending":
                with transaction.atomic():
                    self._job_collection.update(job, "complete", cancelled=True)
                    self._job_collection.update_commands(job)
            self._lock_cache.remove_job(job)

        # Drop self._lock while the thread completes - it will need
        # this lock to send back its completion
        if cancelled_thread is not None:
            cancelled_thread.cancel_complete()
        else:
            # So that anything waiting on this job can be cancelled too
            self.progress.advance()

    def complete_job(self, job_id, errored=False, cancelled=False):
        # TODO: document the rules here: jobs may only modify objects that they
        # have taken out a writelock on, and they may only modify instances obtained
        # via ObjectCache, or via their stateful_object attribute.  Jobs may not
        # modify objects via .update() calls, all changes must be done on loaded instances.
        # They do not have to .save() their stateful_object, but they do have to .save()
        # any other objects that they modify (having obtained their from ObjectCache and
        # held a writelock on them)

        job = self._job_collection.get(job_id)
        with self._lock:
            with transaction.atomic():
                if not errored and not cancelled:
                    try:
                        job.on_success()
                    except Exception:
                        log.error("Error in Job %s on_success:%s" % (job.id, traceback.format_exc()))
                        errored = True

                log.info("Job %d: complete_job: Updating cache" % job.pk)
                # Freshen cached information about anything that this job held a writelock on
                for lock in self._lock_cache.get_by_job(job):
                    if lock.write:
                        if hasattr(lock.locked_item, "not_deleted"):
                            log.info(
                                "Job %d: locked_item %s %s %s %s"
                                % (
                                    job.id,
                                    id(lock.locked_item),
                                    lock.locked_item.__class__,
                                    isinstance(lock.locked_item, DeletableStatefulObject),
                                    lock.locked_item.not_deleted,
                                )
                            )
                        if hasattr(lock.locked_item, "not_deleted") and lock.locked_item.not_deleted is None:
                            log.debug(
                                "Job %d: purging %s/%s" % (job.id, lock.locked_item.__class__, lock.locked_item.id)
                            )
                            ObjectCache.purge(lock.locked_item.__class__, lambda o: o.id == lock.locked_item.id)
                        else:
                            log.debug(
                                "Job %d: updating write-locked %s/%s"
                                % (job.id, lock.locked_item.__class__, lock.locked_item.id)
                            )

                            # Ensure that any notifications prior to release of the writelock are not
                            # applied
                            if hasattr(lock.locked_item, "state_modified_at"):
                                lock.locked_item.__class__.objects.filter(pk=lock.locked_item.pk).update(
                                    state_modified_at=django.utils.timezone.now()
                                )

                            ObjectCache.update(lock.locked_item)

                if job.state != "tasked":
                    # This happens if a Job is cancelled while it's calling this
                    log.info("Job %s has state %s in complete_job" % (job.id, job.state))
                    return

                self._complete_job(job, errored, cancelled)

            self._drain_notification_buffer()
            self._run_next()

    def test_host_contact(self, address, root_pw=None, pkey=None, pkey_pw=None):
        with self._lock:
            with transaction.atomic():
                command = CommandPlan(self._lock_cache, self._job_collection).command_run_jobs(
                    [
                        {
                            "class_name": "TestHostConnectionJob",
                            "args": {"address": address, "root_pw": root_pw, "pkey": pkey, "pkey_pw": pkey_pw},
                        }
                    ],
                    help_text["validating_host"] % address,
                )

        self.progress.advance()

        return command

    def update_corosync_configuration(self, corosync_configuration_id, mcast_port, network_interface_ids):
        with self._lock:
            with transaction.atomic():
                # For now we only support 1 or 2 network configurations, jobs aren't so helpful at supporting lists
                corosync_configuration = CorosyncConfiguration.objects.get(id=corosync_configuration_id)

                assert len(network_interface_ids) == 1 or len(network_interface_ids) == 2
                network_interface_0 = NetworkInterface.objects.get(id=network_interface_ids[0])
                network_interface_1 = (
                    None
                    if len(network_interface_ids) == 1
                    else NetworkInterface.objects.get(id=network_interface_ids[1])
                )

                command_id = CommandPlan(self._lock_cache, self._job_collection).command_run_jobs_preserve_states(
                    [
                        {
                            "class_name": corosync_configuration.configure_job_name,
                            "args": {
                                "corosync_configuration": corosync_configuration,
                                "mcast_port": mcast_port,
                                "network_interface_0": network_interface_0,
                                "network_interface_1": network_interface_1,
                            },
                        }
                    ],
                    [corosync_configuration, corosync_configuration.host.pacemaker_configuration],
                    "Update Corosync Configuration on host %s" % corosync_configuration.host.fqdn,
                )

        self.progress.advance()

        return command_id

    @classmethod
    def order_targets(cls, targets_data):
        "Return sorted sequence of target_data dicts, such that sequential MDTs/OSTs will be distributed across hosts."
        volumes_ids = map(operator.itemgetter("volume_id"), targets_data)
        host_ids = dict(
            VolumeNode.objects.filter(volume_id__in=volumes_ids, primary=True).values_list("volume_id", "host_id")
        )

        def key(td):
            return host_ids[int(td["volume_id"])]

        for host_id, target_group in itertools.groupby(sorted(targets_data, key=key), key=key):
            for index, target_data in enumerate(target_group):
                target_data["index"] = index

        sorted_list = sorted(targets_data, key=operator.itemgetter("index"))

        # Finally an MDT entry is marked as root in the rest API to signify that this should be MDT0 so
        # if we have an entry with 'root'=true then move it to the front of the list before returning the result
        return sorted(sorted_list, key=lambda entry: entry.get("root", False), reverse=True)

    def create_client_mount(self, host_id, filesystem_id, mountpoint):
        # RPC-callable
        host = ObjectCache.get_one(ManagedHost, lambda mh: mh.id == host_id)
        filesystem = ObjectCache.get_one(ManagedFilesystem, lambda mf: mf.id == filesystem_id)

        mount = self._create_client_mount(host, filesystem, mountpoint)

        self.progress.advance()
        return mount.id

    def _create_client_mount(self, host, filesystem, mountpoint):
        # Used for intra-JobScheduler calls
        log.debug("Creating client mount for %s as %s:%s" % (filesystem, host, mountpoint))

        with self._lock:
            from django.db import transaction

            with transaction.atomic():
                mount, created = LustreClientMount.objects.get_or_create(host=host, filesystem=filesystem)
                mount.mountpoint = mountpoint
                mount.save()

            ObjectCache.add(LustreClientMount, mount)

        if created:
            log.info("Created client mount: %s" % mount)

        return mount

    def create_copytool(self, copytool_data):
        from django.db import transaction

        log.debug("Creating copytool from: %s" % copytool_data)
        with self._lock:
            host = ObjectCache.get_by_id(ManagedHost, int(copytool_data["host"]))
            copytool_data["host"] = host
            filesystem = ObjectCache.get_by_id(ManagedFilesystem, int(copytool_data["filesystem"]))
            copytool_data["filesystem"] = filesystem

            with transaction.atomic():
                copytool = Copytool.objects.create(**copytool_data)

            # Add the copytool after the transaction commits
            ObjectCache.add(Copytool, copytool)

        log.debug("Created copytool: %s" % copytool)

        mount = self._create_client_mount(host, filesystem, copytool_data["mountpoint"])

        # Make the association between the copytool and client mount
        with self._lock:
            copytool.client_mount = mount

            with transaction.atomic():
                copytool.save()

            ObjectCache.update(copytool)

        self.progress.advance()
        return copytool.id

    def register_copytool(self, copytool_id, uuid):
        from django.db import transaction

        with self._lock:
            copytool = ObjectCache.get_by_id(Copytool, int(copytool_id))
            log.debug("Registering copytool %s with uuid %s" % (copytool, uuid))

            with transaction.atomic():
                copytool.register(uuid)

            ObjectCache.update(copytool)

        self.progress.advance()

    def unregister_copytool(self, copytool_id):
        from django.db import transaction

        with self._lock:
            copytool = ObjectCache.get_by_id(Copytool, int(copytool_id))
            log.debug("Unregistering copytool %s" % copytool)

            with transaction.atomic():
                copytool.unregister()

            ObjectCache.update(copytool)

        self.progress.advance()

    def create_filesystem(self, fs_data):
        # FIXME: HYD-970: check that the MGT or hosts aren't being removed while
        # creating the filesystem

        def _target_kwargs(attrs):
            result = {}
            for attr in ["inode_count", "inode_size", "bytes_per_inode"]:
                try:
                    result[attr] = attrs[attr]
                except KeyError:
                    pass
            return result

        with self._lock:
            mounts = []
            mgt_data = fs_data["mgt"]
            if "volume_id" in mgt_data:
                mgt, mgt_mounts = ManagedMgs.create_for_volume(
                    mgt_data["volume_id"], reformat=mgt_data.get("reformat", False), **_target_kwargs(mgt_data)
                )
                mounts.extend(mgt_mounts)
                ObjectCache.add(ManagedTarget, mgt.managedtarget_ptr)
                mgt_id = mgt.pk
            else:
                mgt_id = mgt_data["id"]

            with transaction.atomic():
                mgs = ManagedMgs.objects.get(id=mgt_id)
                fs = ManagedFilesystem(mgs=mgs, name=fs_data["name"])
                fs.save()

            # Now that the creation has committed, update ObjectCache
            ObjectCache.add(ManagedFilesystem, fs)

            with transaction.atomic():
                chroma_core.lib.conf_param.set_conf_params(fs, fs_data["conf_params"])

                mdts = []
                for mdt_data in self.order_targets(fs_data["mdts"]):
                    mdt, mdt_mounts = ManagedMdt.create_for_volume(
                        mdt_data["volume_id"],
                        reformat=mdt_data.get("reformat", False),
                        filesystem=fs,
                        **_target_kwargs(mdt_data)
                    )
                    mdts.append(mdt)
                    mounts.extend(mdt_mounts)
                    chroma_core.lib.conf_param.set_conf_params(mdt, mdt_data["conf_params"])

                osts = []
                for ost_data in self.order_targets(fs_data["osts"]):
                    ost, ost_mounts = ManagedOst.create_for_volume(
                        ost_data["volume_id"],
                        reformat=ost_data.get("reformat", False),
                        filesystem=fs,
                        **_target_kwargs(ost_data)
                    )
                    osts.append(ost)
                    mounts.extend(ost_mounts)
                    chroma_core.lib.conf_param.set_conf_params(ost, ost_data["conf_params"])

            for ost in osts:
                ObjectCache.add(ManagedTarget, ost.managedtarget_ptr)
            for mdt in mdts:
                ObjectCache.add(ManagedTarget, mdt.managedtarget_ptr)
            for mount in mounts:
                ObjectCache.add(ManagedTargetMount, mount)

            with transaction.atomic():
                command = self.CommandPlan.command_set_state(
                    [(ContentType.objects.get_for_model(fs).natural_key(), fs.id, "available")],
                    "Creating filesystem %s" % fs_data["name"],
                )

        self.progress.advance()

        return fs.id, command.id

    def create_targets(self, targets_data):
        # FIXME: HYD-970: check that the filesystem or hosts aren't being removed while
        # creating the target

        targets = []
        with self._lock:
            for target_data in self.order_targets(targets_data):
                target_class = ContentType.objects.get_by_natural_key(*(target_data["content_type"])).model_class()
                if target_class().filesystem_member:
                    fs = ManagedFilesystem.objects.get(id=target_data["filesystem_id"])
                    create_kwargs = {"filesystem": fs}
                elif target_class == ManagedMgs:
                    create_kwargs = {}
                else:
                    raise NotImplementedError(target_class)

                with transaction.atomic():
                    target, target_mounts = target_class.create_for_volume(
                        target_data["volume_id"], reformat=target_data.get("reformat", False), **create_kwargs
                    )

                ObjectCache.add(ManagedTarget, target.managedtarget_ptr)
                for mount in target_mounts:
                    ObjectCache.add(ManagedTargetMount, mount)

                targets.append(target)

            if len(targets) == 1:
                command_description = "Creating %s" % targets[0]
            else:
                command_description = "Creating %s targets" % len(targets)

            with transaction.atomic():
                command = self.CommandPlan.command_set_state(
                    [
                        (ContentType.objects.get_for_model(ManagedTarget).natural_key(), x.id, "mounted")
                        for x in targets
                    ],
                    command_description,
                )

        self.progress.advance()

        return [x.id for x in targets], command.id

    def create_host_ssh(self, address, profile, root_pw, pkey, pkey_pw):
        """
        Create a ManagedHost object and deploy the agent to its address using SSH.

        :param address: the resolvable address of the host option user@ in front

        :param root_pw: is either the root password, or the password that goes with
        the user if address is user@address, or, pw is it is the password of
        the private key if a pkey is specified.

        :param pkey is the private key that matches the public keys installed on the
        server at this address.
        """
        from chroma_core.services.job_scheduler.agent_rpc import AgentSsh

        with self._lock:
            # See if the host exists, then this is a failed deploy being retried
            try:
                host = ObjectCache.get_one(ManagedHost, lambda host: host.address == address)

                assert host.state == "undeployed"  # assert the fact this is undeployed being setup
            except ManagedHost.DoesNotExist:
                fqdn_nodename_command = (
                    'python -c "import os; print os.uname()[1] ; import socket ; print socket.getfqdn();"'
                )
                agent_ssh = AgentSsh(address, timeout=5)
                auth_args = agent_ssh.construct_ssh_auth_args(root_pw, pkey, pkey_pw)
                tries = 1
                fqdn = None
                while tries < 31:
                    rc, stdout, stderr = agent_ssh.ssh(fqdn_nodename_command, auth_args=auth_args)
                    log.info(
                        "%setting FQDN for '%s': %s" % ("G" if tries < 2 else "Try #%s g" % tries, address, stdout)
                    )
                    try:
                        nodename, fqdn = tuple([l.strip() for l in stdout.strip().split("\n")])
                        break
                    except ValueError:
                        pass
                    tries += 1
                    if tries > 10:
                        time.sleep(1)

                if not fqdn:
                    log.error("Failed getting FQDN for '%s'.  Are name resolution services broken?" % address)
                    # TODO: we should bubble this error up to the user

                if root_pw:
                    install_method = ManagedHost.INSTALL_SSHPSW
                elif pkey:
                    install_method = ManagedHost.INSTALL_SSHPKY
                else:
                    install_method = ManagedHost.INSTALL_SSHSKY

                with transaction.atomic():
                    server_profile = ServerProfile.objects.get(name=profile)
                    host = ManagedHost.objects.create(
                        state="undeployed",
                        address=address,
                        nodename=nodename,
                        fqdn=fqdn,
                        immutable_state=not server_profile.managed,
                        server_profile=server_profile,
                        install_method=install_method,
                    )

                    lnet_configuration = LNetConfiguration.objects.create(host=host)

                ObjectCache.add(LNetConfiguration, lnet_configuration)
                ObjectCache.add(ManagedHost, host)

            with transaction.atomic():
                command = self.CommandPlan.command_set_state(
                    [
                        (
                            ContentType.objects.get_for_model(host).natural_key(),
                            host.id,
                            host.server_profile.initial_state,
                        )
                    ],
                    help_text["deploying_host"] % host,
                )

            # Tag the in-memory SSH auth information onto this DeployHostJob instance
            for job_id in self._job_collection._command_to_jobs[command.id]:
                job = self._job_collection.get(job_id)
                if isinstance(job, DeployHostJob):
                    job.auth_args = {"root_pw": root_pw, "pkey": pkey, "pkey_pw": pkey_pw}
                    break

        self.progress.advance()

        return host.id, command.id

    def set_host_profile(self, host_id, server_profile_id):
        """
        Set the profile for the given host to the given profile.

        :param host_id:
        :param server_profile_id:
        :return: Command for the host job or None if no commands were created.
        """

        with self._lock:
            with transaction.atomic():
                server_profile = ServerProfile.objects.get(pk=server_profile_id)
                host = ObjectCache.get_one(ManagedHost, lambda mh: mh.id == host_id)

                commands_required = host.set_profile(server_profile_id)

                if commands_required:
                    command = self.CommandPlan.command_run_jobs(
                        commands_required, help_text["change_host_profile"] % (host.fqdn, server_profile.ui_name)
                    )
                else:
                    command = None

        if command:
            self.progress.advance()

        return command

    def create_host(self, fqdn, nodename, address, server_profile_id):
        """
        Create a new host, or update a host in the process of being deployed.
        """
        server_profile = ServerProfile.objects.get(pk=server_profile_id)

        with self._lock:
            with transaction.atomic():
                try:
                    # If there is already a host record (SSH-assisted host addition) then
                    # update it
                    host = ManagedHost.objects.get(fqdn=fqdn, state="undeployed")
                    # host.fqdn = fqdn
                    # host.nodename = nodename
                    # host.save()
                    job = DeployHostJob.objects.filter(~Q(state="complete"), managed_host=host)
                    command = Command.objects.filter(jobs=job)[0]

                except ManagedHost.DoesNotExist:
                    # Else create a new one
                    host = ManagedHost.objects.create(
                        fqdn=fqdn,
                        nodename=nodename,
                        immutable_state=not server_profile.managed,
                        address=address,
                        server_profile=server_profile,
                        install_method=ManagedHost.INSTALL_MANUAL,
                    )
                    lnet_configuration = LNetConfiguration.objects.create(host=host)

                    ObjectCache.add(LNetConfiguration, lnet_configuration)
                    ObjectCache.add(ManagedHost, host)

                    with transaction.atomic():
                        command = self.CommandPlan.command_set_state(
                            [
                                (
                                    ContentType.objects.get_for_model(host).natural_key(),
                                    host.id,
                                    server_profile.initial_state,
                                )
                            ],
                            help_text["deploying_host"] % host,
                        )

        self.progress.advance()

        return host.id, command.id

    @staticmethod
    def _retrieve_stateful_object(obj_content_type_natural_key, object_id):
        """Get the stateful object from cache or DB"""

        model_klass = ContentType.objects.get_by_natural_key(*obj_content_type_natural_key).model_class()
        if issubclass(model_klass, ManagedTarget):
            stateful_object = ObjectCache.get_by_id(ManagedTarget, object_id)
        else:
            stateful_object = ObjectCache.get_by_id(model_klass, object_id)

        return stateful_object.downcast()

    def available_transitions(self, object_list):
        """Compute the available transitional states for each stateful object

        Return dict of transition state name lists that are available for each
        object in the object_list, depending on its current state.
        The key in the return dict is the object id, and
        the value is the list of states available for that object

        If an object in the list is locked, it will be included in the return
        dict, but it's transitions will be an empty list.

        :param object_list: list of serialized tuples: [(obj_key, obj_id), ...]
        :return: dict of list of states {obj_id: ['<state1>','<state2',etc], }
        """

        with self._lock:
            transitions = defaultdict(list)
            for obj_key, obj_id in object_list:
                try:
                    # Hit the DB for the statefulobject (ManagedMgs, ManagedMdt, etc., avoiding all caches
                    # Localize fixed for HYD-2714.  May chance again as HYD-3155 is resolved.
                    model_klass = ContentType.objects.get_by_natural_key(*obj_key).model_class()
                    stateful_object = model_klass.objects.get(pk=obj_id)

                    # Used to leverage the ObjectCache, but this suspect now:  HYD-3155
                    # stateful_object = JobScheduler._retrieve_stateful_object(obj_key, obj_id)

                    log.debug("available_transitions object: %s, state: %s" % (stateful_object, stateful_object.state))
                except ObjectDoesNotExist:
                    # Do not advertise transitions for an object that does not exist
                    # as can happen if a parallel operation deletes this object
                    transitions[obj_id] = []
                    log.debug("available_transitions object: %s" % obj_id)
                else:
                    # We don't advertise transitions for anything which is currently
                    # locked by an incomplete job.  We could alternatively advertise
                    # which jobs would actually be legal to add by skipping this
                    # check and using get_expected_state in place of .state below.
                    if self._lock_cache.get_latest_write(stateful_object):
                        transitions[obj_id] = []
                        log.debug("available_transitions object is LOCKED: %s" % obj_id)
                    else:
                        # XXX: could alternatively use expected_state here if you
                        # want to advertise
                        # what jobs can really be added (i.e. advertise transitions
                        # which will
                        # be available when current jobs are complete)
                        #  See method self.get_expected_state(stateful_object)
                        from_state = stateful_object.state
                        available_states = stateful_object.get_available_states(from_state)
                        log.debug("available_transitions from_state: %s, states: %s" % (from_state, available_states))

                        # Add the job verbs to the possible state transitions for displaying as a choice.
                        transitions[obj_id] = self._add_verbs(stateful_object, available_states)

            return transitions

    def _add_verbs(self, stateful_object, raw_transitions):
        """Lookup the verb for each available state

        raw_transitions is a list of possible transition state names
        a list of dicts containing state and verb are returned.
        """

        log.debug("Adding verbs to %s on %s" % (raw_transitions, stateful_object))

        from_state = stateful_object.state
        transitions = []
        for to_state in raw_transitions:
            # Fetch the last job in a list of jobs that will transition this object from from_state to to_state
            job_class = stateful_object.get_job_class(from_state, to_state, last_job_in_route=True)

            # Now check that that job can run on this instance of the stateful object. In truth this needs to be expanded
            # to make sure every job in the route can be run, but that is a bigger step beyond the scope here. And generally
            # the situation is that it's the final step that is the decider.
            # NB: a None verb means its an internal transition that shouldn't be advertised
            if job_class.state_verb and job_class.can_run(stateful_object):
                log.debug("Adding verb: %s, for job_class: %s" % (job_class.state_verb, job_class))
                transitions.append(
                    {
                        "state": to_state,
                        "verb": job_class.state_verb,
                        "long_description": job_class.long_description(stateful_object),
                        "display_group": job_class.display_group,
                        "display_order": job_class.display_order,
                    }
                )
            else:
                log.debug("Skipping verb for %s on object %s" % (job_class, stateful_object))

        return transitions

    def _fetch_jobs(self, stateful_object):
        from chroma_core.models import AdvertisedJob

        available_jobs = []
        for job_class in all_subclasses(AdvertisedJob):
            if not job_class.plural:
                for class_name in job_class.classes:
                    ct = ContentType.objects.get_by_natural_key("chroma_core", class_name.lower())
                    klass = ct.model_class()
                    if isinstance(stateful_object, klass):
                        if job_class.can_run(stateful_object):
                            available_jobs.append(
                                {
                                    "verb": job_class.verb,
                                    "long_description": job_class.long_description(stateful_object),
                                    "display_group": job_class.display_group,
                                    "display_order": job_class.display_order,
                                    "confirmation": job_class.get_confirmation(stateful_object),
                                    "class_name": job_class.__name__,
                                    "args": job_class.get_args(stateful_object),
                                }
                            )
        return available_jobs

    def available_jobs(self, object_list):
        """Compute the available jobs for the stateful object

        Return a dict of jobs that are available for each object in object_list.
        The key in the dict is the object id, and the value is the list of
        job classes.

        If an object in the list is locked, it will be included in the return
        dict, but it's jobs will be an empty list.

        :param object_list: list of serialized tuples: [(obj_key, obj_id), ...]
        :return: A dict of lists of jobs like {obj1_id: [{'verb': ...,
                        'confirmation': ..., 'class_name': ..., 'args: ...}], ...}
        """

        with self._lock:

            jobs = defaultdict(list)
            for obj_key, obj_id in object_list:

                try:
                    stateful_object = JobScheduler._retrieve_stateful_object(obj_key, obj_id)
                except ObjectDoesNotExist:
                    # Do not advertise jobs for an object that does not exist
                    # as can happen if a parallel operation deletes this object
                    jobs[obj_id] = []
                else:
                    # If the object is subject to an incomplete Job
                    # then don't offer any actions
                    if self._lock_cache.get_latest_write(stateful_object) > 0:
                        jobs[obj_id] = []
                    else:
                        jobs[obj_id] = self._fetch_jobs(stateful_object)

            return jobs

    def get_locks(self, obj_key, obj_id):
        locks = {"read": [], "write": []}

        try:
            object = JobScheduler._retrieve_stateful_object(obj_key, obj_id)
            locks["read"] = list(set([x.job.id for x in self._lock_cache.read_by_item[object]]))
            locks["write"] = list(set([x.job.id for x in self._lock_cache.write_by_item[object]]))
        except ObjectDoesNotExist:
            pass

        return locks

    def update_nids(self, nid_list):
        # Although this is creating/deleting a NID it actually rewrites the whole NID configuration for the node
        # this is all in here for now, but as we move to dynamic lnet it will probably get it's own file.
        with self._lock:
            lnet_configurations = set()
            lnet_nid_data = defaultdict(lambda: {"nid_updates": {}, "nid_deletes": {}})

            for nid_data in nid_list:
                network_interface = NetworkInterface.objects.get(id=nid_data["network_interface"])
                lnet_configuration = LNetConfiguration.objects.get(host=network_interface.host_id)
                lnet_configurations.add(lnet_configuration)

                if str(nid_data["lnd_network"]) == "-1":
                    lnet_nid_data[lnet_configuration]["nid_deletes"][network_interface.id] = nid_data
                else:
                    lnet_nid_data[lnet_configuration]["nid_updates"][network_interface.id] = nid_data

            jobs = []
            for lnet_configuration in lnet_configurations:
                jobs.append(
                    ConfigureLNetJob(
                        lnet_configuration=lnet_configuration,
                        config_changes=json.dumps(lnet_nid_data[lnet_configuration]),
                    )
                )

            with transaction.atomic():
                command = Command.objects.create(message="Configuring NIDS for hosts")
                self.CommandPlan.add_jobs(jobs, command)

        self.progress.advance()

        return command.id

    def trigger_plugin_update(self, include_host_ids, exclude_host_ids, plugin_names):
        """
        Cause the plugins on the hosts passed to send an update irrespective of whether any
        changes have occurred.

        :param include_host_ids: List of host ids to include in the trigger update.
        :param exclude_host_ids: List of host ids to exclude from the include list (makes for usage easy)
        :param plugin_names: list of plugins to trigger update on - empty list means all.
        :return: command id that caused updates to be sent.
        """

        host_ids = [host.id for host in ManagedHost.objects.all()] if include_host_ids is None else include_host_ids
        host_ids = host_ids if exclude_host_ids is None else list(set(host_ids) - set(exclude_host_ids))

        if host_ids:
            with self._lock:
                jobs = [
                    TriggerPluginUpdatesJob(host_ids=json.dumps(host_ids), plugin_names_json=json.dumps(plugin_names))
                ]

                with transaction.atomic():
                    command = Command.objects.create(
                        message="%s triggering updates from agents"
                        % ManagedHost.objects.get(id=exclude_host_ids[0]).fqdn
                    )
                    self.CommandPlan.add_jobs(jobs, command)

            self.progress.advance()

            return command.id
        else:
            return None

    def update_lnet_configuration(self, lnet_configuration_list):
        with self._lock:
            host_states = []

            # This today uses the state change of the Host mechanism, this makes no sense, but we have to
            # move slowly.
            # The correct way is to change the state of the LNetConfiguration so we probably need to make
            # that a stateful object, but that is for lnet mk2 I think
            for lnet_configuration_data in lnet_configuration_list:
                host = ManagedHost.objects.get(id=lnet_configuration_data["host_id"])
                current_lnet_configuration = LNetConfiguration.objects.get(host=host)

                # Now should we just do it, or only do it when it changes?
                if current_lnet_configuration.state != lnet_configuration_data["state"]:
                    host_states.append((host, lnet_configuration_data["state"]))

        command = Command.set_state(host_states)

        if command:
            return command.id
        else:
            return None

    @property
    def CommandPlan(self):
        return CommandPlan(self._lock_cache, self._job_collection)
