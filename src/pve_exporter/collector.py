"""
Prometheus collecters for Proxmox VE cluster.
"""
# pylint: disable=too-few-public-methods

import collections
import itertools
import logging
import re

from prometheus_client.samples import Sample
from prometheus_client.parser import text_string_to_metric_families
from prometheus_client.registry import Collector
from proxmoxer import ProxmoxAPI
from proxmoxer.core import ResourceException

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily

CollectorsOptions = collections.namedtuple('CollectorsOptions', [
    'status',
    'version',
    'node',
    'cluster',
    'resources',
    'config',
    'volumes',
])

class StatusCollector:
    """
    Collects Proxmox VE Node/VM/CT-Status

    # HELP pve_up Node/VM/CT-Status is online/running
    # TYPE pve_up gauge
    pve_up{id="node/proxmox-host"} 1.0
    pve_up{id="cluster/pvec"} 1.0
    pve_up{id="lxc/101"} 1.0
    pve_up{id="qemu/102"} 1.0
    """

    def __init__(self, pve):
        self._pve = pve

    def collect(self): # pylint: disable=missing-docstring
        status_metrics = GaugeMetricFamily(
            'pve_up',
            'Node/VM/CT-Status is online/running',
            labels=['id'])

        for entry in self._pve.cluster.status.get():
            if entry['type'] == 'node':
                label_values = [entry['id']]
                status_metrics.add_metric(label_values, entry['online'])
            elif entry['type'] == 'cluster':
                label_values = ['cluster/{:s}'.format(entry['name'])]
                status_metrics.add_metric(label_values, entry['quorate'])
            else:
                raise ValueError('Got unexpected status entry type {:s}'.format(entry['type']))

        for resource in self._pve.cluster.resources.get(type='vm'):
            label_values = [resource['id']]
            status_metrics.add_metric(label_values, resource['status'] == 'running')

        yield status_metrics

class VersionCollector:
    """
    Collects Proxmox VE build information. E.g.:

    # HELP pve_version_info Proxmox VE version info
    # TYPE pve_version_info gauge
    pve_version_info{release="15",repoid="7599e35a",version="4.4"} 1.0
    """

    LABEL_WHITELIST = ['release', 'repoid', 'version']

    def __init__(self, pve):
        self._pve = pve

    def collect(self): # pylint: disable=missing-docstring
        version_items = self._pve.version.get().items()
        version = {key: value for key, value in version_items if key in self.LABEL_WHITELIST}

        labels, label_values = zip(*version.items())
        metric = GaugeMetricFamily(
            'pve_version_info',
            'Proxmox VE version info',
            labels=labels
        )
        metric.add_metric(label_values, 1)

        yield metric

class ClusterNodeCollector:
    """
    Collects Proxmox VE cluster node information. E.g.:

    # HELP pve_node_info Node info
    # TYPE pve_node_info gauge
    pve_node_info{id="node/proxmox-host", level="c", name="proxmox-host",
        nodeid="0"} 1.0
    """

    def __init__(self, pve):
        self._pve = pve

    def collect(self): # pylint: disable=missing-docstring
        nodes = [entry for entry in self._pve.cluster.status.get() if entry['type'] == 'node']
        labels = ['id', 'level', 'name', 'nodeid']

        if nodes:
            info_metrics = GaugeMetricFamily(
                'pve_node_info',
                'Node info',
                labels=labels)

            for node in nodes:
                label_values = [str(node[key]) for key in labels]
                info_metrics.add_metric(label_values, 1)

            yield info_metrics

class ClusterInfoCollector:
    """
    Collects Proxmox VE cluster information. E.g.:

    # HELP pve_cluster_info Cluster info
    # TYPE pve_cluster_info gauge
    pve_cluster_info{id="cluster/pvec",nodes="2",quorate="1",version="2"} 1.0
    """

    def __init__(self, pve):
        self._pve = pve

    def collect(self): # pylint: disable=missing-docstring
        clusters = [entry for entry in self._pve.cluster.status.get() if entry['type'] == 'cluster']

        if clusters:
            # Remove superflous keys.
            for cluster in clusters:
                del cluster['type']

            # Add cluster-prefix to id.
            for cluster in clusters:
                cluster['id'] = 'cluster/{:s}'.format(cluster['name'])
                del cluster['name']

            # Yield remaining data.
            labels = clusters[0].keys()
            info_metrics = GaugeMetricFamily(
                'pve_cluster_info',
                'Cluster info',
                labels=labels)

            for cluster in clusters:
                label_values = [str(cluster[key]) for key in labels]
                info_metrics.add_metric(label_values, 1)

            yield info_metrics

class ClusterResourcesCollector:
    """
    Collects Proxmox VE cluster resources information, i.e. memory, storage, cpu
    usage for cluster nodes and guests.
    """

    def __init__(self, pve):
        self._pve = pve

    def collect(self): # pylint: disable=missing-docstring
        metrics = {
            'maxdisk': GaugeMetricFamily(
                'pve_disk_size_bytes',
                'Size of storage device',
                labels=['id', 'type', 'storage', 'node']),
            'disk': GaugeMetricFamily(
                'pve_disk_usage_bytes',
                'Disk usage in bytes',
                labels=['id']),
            'maxmem': GaugeMetricFamily(
                'pve_memory_size_bytes',
                'Size of memory',
                labels=['id', 'node', 'type']),
            'mem': GaugeMetricFamily(
                'pve_memory_usage_bytes',
                'Memory usage in bytes',
                labels=['id', 'node', 'type']),
            'netout': GaugeMetricFamily(
                'pve_network_transmit_bytes',
                'Number of bytes transmitted over the network',
                labels=['id']),
            'netin': GaugeMetricFamily(
                'pve_network_receive_bytes',
                'Number of bytes received over the network',
                labels=['id']),
            'diskwrite': GaugeMetricFamily(
                'pve_disk_write_bytes',
                'Number of bytes written to storage',
                labels=['id']),
            'diskread': GaugeMetricFamily(
                'pve_disk_read_bytes',
                'Number of bytes read from storage',
                labels=['id']),
            'cpu': GaugeMetricFamily(
                'pve_cpu_usage_ratio',
                'CPU usage (value between 0.0 and pve_cpu_usage_limit)',
                labels=['id', 'node', 'type']),
            'maxcpu': GaugeMetricFamily(
                'pve_cpu_usage_limit',
                'Maximum allowed CPU usage',
                labels=['id', 'node', 'type']),
            'uptime': GaugeMetricFamily(
                'pve_uptime_seconds',
                'Number of seconds since the last boot',
                labels=['id']),
            'shared': GaugeMetricFamily(
                'pve_storage_shared',
                'Whether or not the storage is shared among cluster nodes',
                labels=['id']),
        }

        info_metrics = {
            'guest': GaugeMetricFamily(
                'pve_guest_info',
                'VM/CT info',
                labels=['id', 'node', 'name', 'type']),
            'storage': GaugeMetricFamily(
                'pve_storage_info',
                'Storage info',
                labels=['id', 'node', 'storage']),
        }

        info_lookup = {
            'lxc': {
                'labels': ['id', 'node', 'name', 'type'],
                'gauge': info_metrics['guest'],
            },
            'qemu': {
                'labels': ['id', 'node', 'name', 'type'],
                'gauge': info_metrics['guest'],
            },
            'storage': {
                'labels': ['id', 'node', 'storage'],
                'gauge': info_metrics['storage'],
            },
        }

        for resource in self._pve.cluster.resources.get():
            restype = resource['type']

            if restype in info_lookup:
                label_values = [resource.get(key, '') for key in info_lookup[restype]['labels']]
                info_lookup[restype]['gauge'].add_metric(label_values, 1)

            for key, metric_value in resource.items():
                if key in metrics:
                    metric = metrics[key]
                    label_values = [resource[labelname] for labelname in metric._labelnames if labelname in resource]
                    metric.add_metric(label_values, metric_value)

        return itertools.chain(metrics.values(), info_metrics.values())

class ClusterNodeConfigCollector:
    """
    Collects Proxmox VE VM information directly from config, i.e. boot, name, onboot, etc.
    For manual test: "pvesh get /nodes/<node>/<type>/<vmid>/config"

    # HELP pve_onboot_status Proxmox vm config onboot value
    # TYPE pve_onboot_status gauge
    pve_onboot_status{id="qemu/113",node="XXXX",type="qemu"} 1.0
    """

    def __init__(self, pve):
        self._pve = pve
        self._log = logging.getLogger(__name__)

    def collect(self): # pylint: disable=missing-docstring
        metrics = {
            'onboot': GaugeMetricFamily(
                'pve_onboot_status',
                'Proxmox vm config onboot value',
                labels=['id', 'node', 'type']),
        }

        for node in self._pve.nodes.get():
            # The nodes/{node} api call will result in requests being forwarded
            # from the api node to the target node. Those calls can fail if the
            # target node is offline or otherwise unable to respond to the
            # request. In that case it is better to just skip scraping the
            # config for guests on that particular node and continue with the
            # next one in order to avoid failing the whole scrape.
            try:
                # Qemu
                vmtype = 'qemu'
                for vmdata in self._pve.nodes(node['node']).qemu.get():
                    config = self._pve.nodes(node['node']).qemu(vmdata['vmid']).config.get().items()
                    for key, metric_value in config:
                        label_values = ["%s/%s" % (vmtype, vmdata['vmid']), node['node'], vmtype]
                        if key in metrics:
                            metrics[key].add_metric(label_values, metric_value)
                # LXC
                vmtype = 'lxc'
                for vmdata in self._pve.nodes(node['node']).lxc.get():
                    config = self._pve.nodes(node['node']).lxc(vmdata['vmid']).config.get().items()
                    for key, metric_value in config:
                        label_values = ["%s/%s" % (vmtype, vmdata['vmid']), node['node'], vmtype]
                        if key in metrics:
                            metrics[key].add_metric(label_values, metric_value)

            except ResourceException:
                self._log.exception(
                    "Exception thrown while scraping quemu/lxc config from %s",
                    node['node']
                )
                continue

        return metrics.values()


class VolumesCollector(Collector):
    """
    Collects info on volume sizes - the storage disk usage may not reflect the commitments
    in case of thin allocation.
    """
    def __init__(self, pve):
        self._pve = pve

    def collect(self):  # pylint: disable=missing-docstring
        disk_size = GaugeMetricFamily(
            'pve_volume_size_bytes',
            'Proxmox volume commitments',
            labels=['id', 'node', 'storage']
        )
        seen_shared_storages = set()
        for node in self._pve.nodes.get():
            # The nodes/{node} api call will result in requests being forwarded
            # from the api node to the target node. Those calls can fail if the
            # target node is offline or otherwise unable to respond to the
            # request. In that case it is better to just skip scraping the
            # config for guests on that particular node and continue with the
            # next one in order to avoid failing the whole scrape.
            try:
                storage_api = self._pve.nodes(node['node']).storage
                for storage in storage_api.get():
                    if not (storage['type'] != 'dir' and storage['active']):
                        continue
                    if storage['shared'] and storage['storage'] in seen_shared_storages:
                        continue
                    else:
                        seen_shared_storages.add(storage['storage'])

                    for disk in storage_api(storage['storage']).content.get():
                        disk_size.add_metric([f'disk/{node["node"]}/{disk["volid"]}', node['node'],
                                              storage['storage']], disk['size'])
            except ResourceException:
                self._log.exception(
                    "Exception thrown while scraping quemu/lxc config from %s",
                    node['node']
                )
                continue
        return [disk_size]


class ClusterCustomMetricsCollector(Collector):
    """
    Collects custom labels defined in the Notes section
    """
    def __init__(self, pve):
        self._pve = pve

    def collect(self):
        metrics = []
        notes = self._pve.cluster.options.get().get('description', '')
        if m := re.match(r'#+\s*Prometheus metrics\s*```(.*?)```', notes, re.MULTILINE | re.DOTALL):
            custom_metrics_text = m.group(1)

            for metric in text_string_to_metric_families(custom_metrics_text):
                metric.name = f'pve_{metric.name}'
                new_samples = [
                    Sample(name=f'pve_{sample.name}', labels=sample.labels | {'__source': 'Datacenter > Notes'},
                           exemplar=sample.exemplar, value=sample.value)
                    for sample in metric.samples
                ]
                metric.samples = new_samples

                metrics.append(metric)

        return metrics


def collect_pve(config, host, options: CollectorsOptions):
    """Scrape a host and return prometheus text format for it"""

    pve = ProxmoxAPI(host, **config)

    registry = CollectorRegistry()

    registry.register(ClusterCustomMetricsCollector(pve))

    if options.status:
        registry.register(StatusCollector(pve))
    if options.resources:
        registry.register(ClusterResourcesCollector(pve))
    if options.node:
        registry.register(ClusterNodeCollector(pve))
    if options.cluster:
        registry.register(ClusterInfoCollector(pve))
    if options.config:
        registry.register(ClusterNodeConfigCollector(pve))
    if options.version:
        registry.register(VersionCollector(pve))
    if options.volumes:
        registry.register(VolumesCollector(pve))

    return generate_latest(registry)
