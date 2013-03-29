#
# ========================================================
# Copyright (c) 2012 Whamcloud, Inc.  All rights reserved.
# ========================================================


from collections import namedtuple, defaultdict
from dateutil import parser
from django.db import transaction
from django.utils.timezone import now

from chroma_core.models import ManagedHost, HostOfflineAlert
from chroma_core.services import ChromaService, log_register
from chroma_core.services.job_scheduler.job_scheduler_client import JobSchedulerClient
from chroma_core.services.queue import AgentRxQueue

log = log_register(__name__)

COROSYNC_PLUGIN_NAME = 'corosync'


class CorosyncRxQueue(AgentRxQueue):
    plugin = COROSYNC_PLUGIN_NAME


class Service(ChromaService):
    """Corosync host offline detection service

    The corosync agent will report host status for all nodes in it's
    peer group.  Any node that is down according to corosync
    will be recorded here, and in the DB, and and alert will be saved.

    Be sure to have all nodes on the exact same time - ntp.  This service will
    drop older reports that come in late, so correct timing is critical.
    """

    def __init__(self):

        super(Service, self).__init__()

        #  Class to store the in-memory online/offline status and sample times
        #  a HostStatus object is created for each host that is reported
        self.HostStatus = namedtuple('HostStatus', 'status, datetime')

        #  Holds each host seen as a key with a HostStatus value last set
        self._host_status = defaultdict(self.HostStatus)

        self._queue = CorosyncRxQueue()

    # Using transaction decorator to ensure that subsequent calls
    # see fresh data when polling the ManagedHost model.
    @transaction.commit_on_success()
    def on_data(self, fqdn, body):
        """Process all incoming messages from the Corosync agent plugin

        Request to have the status changed for an instance.  If the current
        state determines that a host is offline, then raise that alert.

        old messages should not be processed.

        datetime is in UTC of the node's localtime in the standard
        ISO string format
        """

        # TODO:  When the nodes are empty, and the datetime is '' corosync is
        # down.  The manager should be alerted:
        # http://jira.whamcloud.com/browse/HYD-1670

        dt = body['datetime']
        try:
            dt = parser.parse(dt)
        except ValueError:
            if dt != '':
                log.warning("Invalid date or tz string from "
                            "corosync plugin: %s" % dt)
                raise

        def is_new(peer_fqdn):
            return (peer_fqdn not in self._host_status or
                    self._host_status[peer_fqdn].datetime < dt)

        peers_str = "; ".join(["%s: online=%s, new=%s" %
                                (peer_fqdn, data['online'], is_new(peer_fqdn))
                                for peer_fqdn, data in body['nodes'].items()])
        log.debug("Incoming peer report from %s:  %s" % (fqdn, peers_str))

        # NB: This will ignore any unknown peers in the report.
        cluster_nodes = ManagedHost.objects.select_related('ha_cluster_peers').filter(fqdn__in = body['nodes'].keys())

        #  Consider all nodes in the peer group for this reporting agent
        for host in cluster_nodes:
            data = body['nodes'][host.fqdn]

            cluster_peer_keys = sorted([node.pk for node in cluster_nodes
                                            if node is not host])

            if is_new(host.fqdn):
                incoming_status = data['online'] == 'true'

                log.debug("Corosync processing "
                          "peer %s of %s " % (host.fqdn, fqdn))

                #  Raise an Alert - system supresses dups
                log.debug("Alert notify on %s: active=%s" % (host, not incoming_status))
                HostOfflineAlert.notify(host, not incoming_status)

                #  Attempt to save the state.
                attrs = {}
                if host.corosync_reported_up != incoming_status:
                    attrs['corosync_reported_up'] = incoming_status

                peer_host_peer_keys = sorted([h.pk for h in
                                              host.ha_cluster_peers.all()])
                if peer_host_peer_keys != cluster_peer_keys:
                    attrs['ha_cluster_peers'] = cluster_peer_keys

                if len(attrs):
                    JobSchedulerClient.notify(host, now(), attrs)

                #  Keep internal track of the hosts state.
                curr_status = self.HostStatus(status=incoming_status,
                                              datetime=dt)
                self._host_status[host.fqdn] = curr_status

    def run(self):
        self._queue.serve(data_callback=self.on_data)

    def stop(self):
        self._queue.stop()
