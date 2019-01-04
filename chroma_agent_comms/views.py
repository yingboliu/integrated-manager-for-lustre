# Copyright (c) 2018 DDN. All rights reserved.
# Use of this source code is governed by a MIT-style
# license that can be found in the LICENSE file.


import Queue
import json
import traceback
import time

from django.db import transaction
from django.http import HttpResponseNotAllowed, HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View
from functools import wraps
import settings
from os import path
from tastypie.http import HttpForbidden

from chroma_core.models import ManagedHost, ClientCertificate, RegistrationToken, ServerProfile, Bundle
from chroma_core.models.copytool import Copytool, CopytoolEvent, CopytoolOperation, log as copytool_log, UNKNOWN_UUID
from chroma_core.models.log import LogMessage, MessageClass
from chroma_core.models.utils import Version
from chroma_core.services import log_register
from chroma_core.services.crypto import Crypto
from iml_common.lib.date_time import IMLDateTime

log = log_register("agent_views")
import logging

log.setLevel(logging.WARN)


def log_exception(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args)
        except Exception:
            log.error(traceback.format_exc())
            raise

    return wrapped


class ValidatedClientView(View):
    @classmethod
    def valid_fqdn(cls, request):
        "Return fqdn if certificate is valid."
        fqdn = cls.valid_certs.get(request.META["HTTP_X_SSL_CLIENT_SERIAL"])
        if not fqdn:
            log.warning("Rejecting certificate %s" % request.META["HTTP_X_SSL_CLIENT_SERIAL"])
        elif fqdn != request.META["HTTP_X_SSL_CLIENT_NAME"]:
            log.info("Domain name changed %s" % fqdn)
        return fqdn


class CopytoolEventView(ValidatedClientView):
    @log_exception
    def post(self, request):
        """
        Receive copytool events from the monitor.
        Handle a POST containing events from the monitor
        """
        body = json.loads(request.body)
        fqdn = self.valid_fqdn(request)
        if not fqdn:
            copytool_log.error("Invalid client: %s" % request.META)
            return HttpForbidden()

        copytool_log.debug("Incoming payload: %s" % body)
        try:
            copytool = Copytool.objects.select_related().get(id=body["copytool"])
            events = [CopytoolEvent(**e) for e in body["events"]]
        except KeyError as e:
            return HttpResponseBadRequest("Missing attribute '%s'" % e.args[0])
        except Copytool.DoesNotExist:
            return HttpResponseBadRequest("Unknown copytool: %s" % body["copytool"])

        copytool_log.debug("Received %d events from %s on %s" % (len(events), copytool, copytool.host))

        from chroma_core.services.job_scheduler.job_scheduler_client import JobSchedulerClient

        active_operations = {}
        for event in sorted(events, key=lambda event: event.timestamp):
            copytool_log.debug(event)

            # These types aren't associated with active operations
            if event.type == "UNREGISTER":
                JobSchedulerClient.unregister_copytool(copytool.id)
                continue
            elif event.type == "REGISTER":
                JobSchedulerClient.register_copytool(copytool.id, event.uuid)
                continue
            elif event.type == "LOG":
                LogMessage.objects.create(
                    fqdn=copytool.host.fqdn,
                    message=event.message,
                    severity=getattr(logging, event.level),
                    facility=4,  # daemon
                    tag=str(copytool),
                    datetime=event.timestamp,
                    message_class=MessageClass.COPYTOOL_ERROR if event.level == "ERROR" else MessageClass.COPYTOOL,
                )
                continue

            # Fixup for times when the register event was missed
            if copytool.state != "started":
                # FIXME: Figure out how to find the uuid after the fact. Maybe
                # the solution is to send uuid with every event from the
                # copytool, but that seems kludgy.
                JobSchedulerClient.register_copytool(copytool.id, UNKNOWN_UUID)

            try:
                active_operations[event.data_fid] = CopytoolOperation.objects.get(id=event.active_operation)
            except AttributeError:
                if event.state == "START":
                    kwargs = dict(
                        start_time=event.timestamp, type=event.type, path=event.lustre_path, fid=event.data_fid
                    )
                    active_operations[event.data_fid] = copytool.create_operation(**kwargs)
                    continue
                elif event.source_fid in active_operations:
                    active_operations[event.data_fid] = active_operations.pop(event.source_fid)
                else:
                    copytool_log.error(
                        "%s on %s, received malformed non-START event: %s" % (copytool, copytool.host, event)
                    )
                    continue
            except CopytoolOperation.DoesNotExist:
                copytool_log.error(
                    "%s on %s, received event for unknown operation: %s" % (copytool, copytool.host, event)
                )
                continue

            if event.state in ["FINISH", "ERROR"]:
                active_operations[event.data_fid].finish(event.timestamp, event.state, event.error)
                del active_operations[event.data_fid]
            elif event.state == "RUNNING":
                active_operations[event.data_fid].update(event.timestamp, event.current_bytes, event.total_bytes)
            else:
                copytool_log.error("%s on %s, received unknown event type: %s" % (copytool, copytool.host, event))
                continue

        try:
            return HttpResponse(
                json.dumps({"active_operations": dict((fid, op.id) for fid, op in active_operations.items())}),
                mimetype="application/json",
            )
        except AttributeError:
            return HttpResponse()


class MessageView(ValidatedClientView):
    queues = None
    sessions = None
    hosts = None

    LONG_POLL_TIMEOUT = 30

    @log_exception
    def post(self, request):
        """
        Receive messages FROM the agent.
        Handle a POST containing messages from the agent
        """

        body = json.loads(request.body)
        fqdn = self.valid_fqdn(request)
        if not fqdn:
            return HttpForbidden()

        try:
            messages = body["messages"]
        except KeyError:
            return HttpResponseBadRequest("Missing attribute 'messages'")

        # Check that the server identifier in each message
        # is valid by comparing against the SSL_CLIENT_NAME
        # which is cryptographically vouched for at the HTTPS frontend
        for message in messages:
            if message["fqdn"] != fqdn:
                return HttpResponseBadRequest("Incorrect client name")

        log.debug("MessageView.post: %s %s messages: %s" % (fqdn, len(messages), body))
        for message in messages:
            if message["type"] == "DATA":
                try:
                    self.sessions.get(fqdn, message["plugin"], message["session_id"])
                except KeyError:
                    log.warning(
                        "Terminating session because unknown %s/%s/%s"
                        % (fqdn, message["plugin"], message["session_id"])
                    )
                    self.queues.send(
                        {
                            "fqdn": fqdn,
                            "type": "SESSION_TERMINATE",
                            "plugin": message["plugin"],
                            "session_id": None,
                            "session_seq": None,
                            "body": None,
                        }
                    )
                else:
                    log.debug(
                        "Forwarding valid message %s/%s/%s-%s"
                        % (fqdn, message["plugin"], message["session_id"], message["session_seq"])
                    )
                    self.queues.receive(message)

            elif message["type"] == "SESSION_CREATE_REQUEST":
                session = self.sessions.create(fqdn, message["plugin"])
                log.info("Creating session %s/%s/%s" % (fqdn, message["plugin"], session.id))

                # When creating a session, it may be for a new agent instance.  There may be an older
                # agent instance with a hanging GET.  We need to make sure that messages that we send
                # from this point onwards go to the new agent and not any GET handlers that haven't
                # caught up yet.  Achive this by sending a barrier message with the agent start time, such
                # that any GET handler receiving the barrier which has a different agent start time will
                # detach itself from the TX queue.  NB the barrier only works because there's also a lock,
                # so if there was a zombie GET, it will be holding the lock and receive the barrier.

                self.queues.send({"fqdn": fqdn, "type": "TX_BARRIER", "client_start_time": body["client_start_time"]})

                self.queues.send(
                    {
                        "fqdn": fqdn,
                        "type": "SESSION_CREATE_RESPONSE",
                        "plugin": session.plugin,
                        "session_id": session.id,
                        "session_seq": None,
                        "body": None,
                    }
                )

        return HttpResponse()

    def _filter_valid_messages(self, fqdn, messages):
        plugin_to_session_id = {}

        def is_valid(message):
            try:
                session_id = plugin_to_session_id[message["plugin"]]
            except KeyError:
                try:
                    plugin_to_session_id[message["plugin"]] = session_id = self.sessions.get(fqdn, message["plugin"]).id
                except KeyError:
                    plugin_to_session_id[message["plugin"]] = session_id = None

            if message["session_id"] != session_id:
                log.debug(
                    "Dropping message because it has stale session id (current is %s): %s" % (session_id, message)
                )
                return False

            return True

        return [m for m in messages if is_valid(m)]

    @log_exception
    def get(self, request):
        """
        Send messages TO the agent.
        Handle a long-polling GET for messages to the agent
        """

        fqdn = self.valid_fqdn(request)
        if not fqdn:
            return HttpForbidden()
        server_boot_time = IMLDateTime.parse(request.GET["server_boot_time"])
        client_start_time = IMLDateTime.parse(request.GET["client_start_time"])

        messages = []

        try:
            reset_required = self.hosts.update(fqdn, server_boot_time, client_start_time)
        except ManagedHost.DoesNotExist:
            # This should not happen because the HTTPS frontend should have the
            # agent certificate revoked before removing the ManagedHost from the database
            log.error("GET from unknown server %s" % fqdn)
            return HttpResponseBadRequest("Unknown server '%s'" % fqdn)

        if reset_required:
            # This is the case where the http_agent service restarts, so
            # we have to let the agent know that all open sessions
            # are now over.
            messages.append(
                {
                    "fqdn": fqdn,
                    "type": "SESSION_TERMINATE_ALL",
                    "plugin": None,
                    "session_id": None,
                    "session_seq": None,
                    "body": None,
                }
            )

        log.debug("MessageView.get: composing messages for %s" % fqdn)
        queues = self.queues.get(fqdn)

        # If this handler is sitting on the TX queue, draining messages, then
        # when a new session starts, *before* sending any TX messages, we have to
        # make sure it has been disconnected, to avoid the TX messages being sent
        # to an 'old' session (old session meaning TCP connection from a now-dead agent)

        with queues.tx_lock:
            try:
                first_message = queues.tx.get(block=True, timeout=self.LONG_POLL_TIMEOUT)
                if first_message["type"] == "TX_BARRIER":
                    if first_message["client_start_time"] != request.GET["client_start_time"]:
                        log.warning(
                            "Cancelling GET due to barrier %s %s"
                            % (first_message["client_start_time"], request.GET["client_start_time"])
                        )
                        return HttpResponse(json.dumps({"messages": []}), mimetype="application/json")
                else:
                    messages.append(first_message)
            except Queue.Empty:
                pass
            else:
                # TODO: limit number of messages per response
                while True:
                    try:
                        message = queues.tx.get(block=False)
                        if message["type"] == "TX_BARRIER":
                            if message["client_start_time"] != request.GET["client_start_time"]:
                                log.warning(
                                    "Cancelling GET due to barrier %s %s"
                                    % (message["client_start_time"], request.GET["client_start_time"])
                                )
                                return HttpResponse(json.dumps({"messages": []}), mimetype="application/json")
                        else:
                            messages.append(message)
                    except Queue.Empty:
                        break

        messages = self._filter_valid_messages(fqdn, messages)

        log.debug("MessageView.get: responding to %s with %s messages (%s)" % (fqdn, len(messages), client_start_time))
        return HttpResponse(json.dumps({"messages": messages}), mimetype="application/json")


def validate_token(key, credits=1):
    """
    Validate that a token is valid to authorize a setup/register operation:
     * Check it's not expired
     * Check it has some credits

    :param credits: number of credits to decrement if valid
    :return 2-tuple (<http response if error, else None>, <registration token if valid, else None>)
    """
    try:
        with transaction.commit_on_success():
            token = RegistrationToken.objects.get(secret=key)
            if not token.credits:
                log.warning("Attempt to register with exhausted token %s" % key)
                return HttpForbidden(), None
            else:
                # Decrement .credits
                RegistrationToken.objects.filter(secret=key).update(credits=token.credits - credits)
    except RegistrationToken.DoesNotExist:
        log.warning("Attempt to register with non-existent token %s" % key)
        return HttpForbidden(), None
    else:
        now = IMLDateTime.utcnow()

        if token.expiry < now:
            log.warning("Attempt to register with expired token %s (now %s, expired at %s)" % (key, now, token.expiry))
            return HttpForbidden(), None
        elif token.cancelled:
            log.warning("Attempt to register with cancelled token %s" % key)
            return HttpForbidden(), None

    return None, token


@csrf_exempt
@log_exception
def setup(request, key):
    token_error, token = validate_token(key, credits=0)
    if token_error:
        return token_error

    # the minimum repos needed on a storage server now
    repos = open("/usr/share/chroma-manager/storage_server.repo").read()

    repo_names = token.profile.bundles.values_list("bundle_name", flat=True)
    for bundle in Bundle.objects.all():
        if bundle.bundle_name != "external":
            repos += """[%s]
name=%s
baseurl={0}/%s/$releasever/
enabled=0
gpgcheck=0
sslverify = 1
sslcacert = {1}
sslclientkey = {2}
sslclientcert = {3}
proxy=_none_

""" % (
                bundle.bundle_name,
                bundle.description,
                bundle.bundle_name,
            )

    base_url = str(settings.SERVER_HTTP_URL)
    reg_url = path.join(base_url, "agent/register/%s/" % key)
    repo_url = path.join(base_url, "repo/")
    crypto = Crypto()
    cert_str = open(crypto.AUTHORITY_CERT_FILE).read()

    repo_packages = "python2-iml-agent"
    server_profile = ServerProfile.objects.get(name=request.REQUEST["profile_name"])

    try:
        if server_profile.managed:
            repo_packages += " python2-iml-agent-management"
    except (ServerProfile.DoesNotExist, KeyError) as e:
        if type(e) is KeyError:
            err = "Profile name not specified"
        else:
            err = "Profile %s not a valid profile" % request.REQUEST["profile_name"]
        log.error(err)
        return HttpResponse(status=400, content=err)

    server_epoch_seconds = time.time()

    profile_json = json.dumps(server_profile.as_dict)

    # read in script template before populating (parent dir is chroma-manager basedir)
    with open(
        path.join(path.dirname(path.dirname(path.abspath(__file__))), "agent-bootstrap-script.template"), "r"
    ) as f:
        setup_script_template = f.read()

    script_formatted = setup_script_template.format(
        reg_url=reg_url,
        cert_str=cert_str,
        repo_url=repo_url,
        base_url=base_url,
        repos=repos,
        repo_names=",".join(repo_names),
        server_epoch_seconds=server_epoch_seconds,
        repo_packages=repo_packages,
        profile_json=profile_json,
    )

    return HttpResponse(status=201, content=script_formatted)


@csrf_exempt
@log_exception
def register(request, key):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    token_error, registration_token = validate_token(key)
    if token_error:
        return token_error

    host_attributes = json.loads(request.body)

    # Fail at the first if the version of the agent on the server is incorrect
    manager, agent = Version(settings.VERSION), Version(host_attributes["version"])
    if manager and agent and not (manager.major == agent.major and manager.minor >= agent.minor):
        err = "Version incompatibility between manager {0} and agent {1}".format(manager, agent)
        log.error(err)
        return HttpResponse(status=400, content=err)

    # Fulfil the registering server's request for a certificate authenticating
    # it as the owner of this FQDN.
    csr = host_attributes["csr"]

    # Check that the commonName in the CSR is the same as that in host_attributes
    # (prevent registering as one host and getting a certificate to impersonate another)
    csr_fqdn = Crypto().get_common_name(csr)
    if csr_fqdn != host_attributes["fqdn"]:
        # Terse response to attacker
        log.error(
            "FQDN mismatch '%s' vs. '%s' from %s"
            % (csr_fqdn, host_attributes["fqdn"], request.META["HTTP_X_FORWARDED_FOR"])
        )
        return HttpResponse(status=400, content="")

    with transaction.commit_on_success():
        # Isolate transaction to avoid locking ManagedHost table, this
        # is just a friendly pre-check and will be enforced again inside
        # job_scheduler.create_host
        try:
            existing_host = ManagedHost.objects.get(fqdn=host_attributes["fqdn"])
        except ManagedHost.DoesNotExist:
            pass
        else:
            if existing_host.state != "undeployed":
                return HttpResponse(status=400, content=json.dumps({"fqdn": ["FQDN in use"]}))

    certificate_str = Crypto().sign(csr)
    certificate_serial = Crypto().get_serial(certificate_str)
    log.info("Generated certificate %s:%s" % (host_attributes["fqdn"], certificate_serial))
    ValidatedClientView.valid_certs[certificate_serial] = host_attributes["fqdn"]

    # FIXME: handle the case where someone registers,
    # and then dies before saving their certificate:
    # when they come through here again, currently
    # we'll reject them because the FQDN is taken
    # ... maybe hand back the certificate here, but
    # then don't create the host until they first
    # connect using the certificate?
    # in that case to avoid handing out another cert
    # to someone else spamming our URL, we should have
    # some logic during the second addition to revoke
    # the first (should never be used) host cert.

    server_profile = registration_token.profile
    from chroma_core.services.job_scheduler.job_scheduler_client import JobSchedulerClient

    host, command = JobSchedulerClient.create_host(
        address=host_attributes["address"],
        fqdn=host_attributes["fqdn"],
        nodename=host_attributes["nodename"],
        server_profile_id=server_profile.pk,
    )

    with transaction.commit_on_success():
        ClientCertificate.objects.create(host=host, serial=certificate_serial)

    # TODO: document this return format
    return HttpResponse(
        status=201,
        content=json.dumps({"command_id": command.id, "host_id": host.id, "certificate": certificate_str}),
        mimetype="application/json",
    )


@csrf_exempt
@log_exception
def reregister(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    fqdn = MessageView.valid_fqdn(request)
    if not fqdn:
        return HttpForbidden()
    host_attributes = json.loads(request.body)

    ValidatedClientView.valid_certs[request.META["HTTP_X_SSL_CLIENT_SERIAL"]] = host_attributes["fqdn"]
    ManagedHost.objects.filter(fqdn=fqdn).update(fqdn=host_attributes["fqdn"], address=host_attributes["address"])
    return HttpResponse()
