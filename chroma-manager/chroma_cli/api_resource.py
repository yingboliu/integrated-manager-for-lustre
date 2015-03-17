#
# INTEL CONFIDENTIAL
#
# Copyright 2013-2014 Intel Corporation All Rights Reserved.
#
# The source code contained or described herein and all documents related
# to the source code ("Material") are owned by Intel Corporation or its
# suppliers or licensors. Title to the Material remains with Intel Corporation
# or its suppliers and licensors. The Material contains trade secrets and
# proprietary and confidential information of Intel or its suppliers and
# licensors. The Material is protected by worldwide copyright and trade secret
# laws and treaty provisions. No part of the Material may be used, copied,
# reproduced, modified, published, uploaded, posted, transmitted, distributed,
# or disclosed in any way without Intel's prior express written permission.
#
# No license under any patent, copyright, trade secret or other intellectual
# property right is granted to or conferred upon you by disclosure or delivery
# of the Materials, either expressly, by implication, inducement, estoppel or
# otherwise. Any license under such intellectual property rights must be
# express and approved by Intel in writing.


class ApiResource(object):
    def __init__(self, *args, **kwargs):
        self.__attributes = kwargs
        self.list_columns = ["id"]

    def __getattr__(self, key):
        return self.__attributes[key]

    def __getitem__(self, key):
        return self.__attributes[key]

    @property
    def all_attributes(self):
        return self.__attributes

    def as_header(self):
        return self.list_columns

    def as_row(self):
        row = []
        for key in self.list_columns:
            try:
                val = getattr(self, key)
                # Don't just try: this because catching TypeErrors can
                # mask problems inside the callable.
                if callable(val):
                    row.append(val())
                else:
                    row.append(val)
            except KeyError:
                row.append("")

        return row

    def __str__(self):
        return " | ".join([str(f) for f in self.as_row()])

    # http://stackoverflow.com/a/1094933/204920
    def fmt_bytes(self, bytes):
        import math
        if bytes is None or math.isnan(float(bytes)):
            return "NaN"
        bytes = float(bytes)
        for x in ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "YiB", "ZiB"]:
            if bytes < 1024.0:
                return "%3.1f%s" % (bytes, x)
            bytes /= 1024.0
        # fallthrough, bigger than ZiB?!?!
        return "%d" % bytes

    def fmt_num(self, num):
        import math
        if num is None or math.isnan(float(num)):
            return "NaN"
        num = float(num)
        for x in ["", "K", "M", "G", "P", "E", "Y", "Z"]:
            if num < 1000.0:
                return "%3.1f%s" % (num, x)
            num /= 1000.0
        # fallthrough
        return "%d" % num

    def pretty_time(self, in_time):
        from datetime import datetime as dt
        from dateutil.tz import tzlocal, tzutc
        local_tz = tzlocal()
        local_midnight = dt.now(local_tz).replace(hour=0, minute=0,
                                                  second=0, microsecond=0)
        in_time = in_time.replace(tzinfo=tzutc())
        out_time = in_time.astimezone(local_tz)
        if out_time < local_midnight:
            return out_time.strftime("%Y/%m/%d %H:%M:%S")
        else:
            return out_time.strftime("%H:%M:%S")


class ServerProfile(ApiResource):
    def __init__(self, *args, **kwargs):
        super(ServerProfile, self).__init__(*args, **kwargs)
        self.list_columns.extend(["name", "ui_name", "ui_description", "managed", "bundles"])


class Host(ApiResource):
    def __init__(self, *args, **kwargs):
        super(Host, self).__init__(*args, **kwargs)
        # FIXME: Figure out bad interaction between role as a resource
        # field and as a filter.
        #self.list_columns.extend(["fqdn", "role", "state", "nids", "last_contact"])
        self.list_columns += 'fqdn', 'state', 'nids'

    def nids(self):
        return ",".join(self.all_attributes['nids'])


class LnetConfiguration(ApiResource):
    def __init__(self, *args, **kwargs):
        super(LnetConfiguration, self).__init__(*args, **kwargs)
        self.list_columns.extend(['host__fqdn'])


class Target(ApiResource):
    def __init__(self, *args, **kwargs):
        super(Target, self).__init__(*args, **kwargs)
        self.list_columns.extend(["name", "state", "primary_path"])

    def primary_path(self):
        try:
            primary_node = [vn for vn in self.volume['volume_nodes']
                                        if vn['primary']][0]
            return "%s:%s" % (primary_node['host_label'], primary_node['path'])
        except IndexError:
            return "Unknown"


class Filesystem(ApiResource):
    def __init__(self, *args, **kwargs):
        super(Filesystem, self).__init__(*args, **kwargs)
        self.list_columns.extend(["name", "state", "clients", "files", "space"])

    def files(self):
        # Freaking Nones
        files_total = float("nan") if self.files_total is None else self.files_total
        files_free = float("nan") if self.files_free is None else self.files_free
        return "%s/%s" % (self.fmt_num(files_free), self.fmt_num(files_total))

    def space(self):
        bytes_total = float("nan") if self.bytes_total is None else self.bytes_total
        bytes_free = float("nan") if self.bytes_free is None else self.bytes_free
        return "%s/%s" % (self.fmt_bytes(bytes_free), self.fmt_bytes(bytes_total))

    def clients(self):
        try:
            return "%d" % self.client_count
        except TypeError:
            return "0"


class Volume(ApiResource):
    def __init__(self, *args, **kwargs):
        super(Volume, self).__init__(*args, **kwargs)
        self.list_columns.extend(["name", "size", "filesystem_type", "primary", "failover", "status"])

    def name(self):
        return " ".join(self.label.split())

    def size(self):
        return self.fmt_bytes(self.all_attributes['size'])

    def primary(self):
        for node in self.volume_nodes:
            if node['primary']:
                return ":".join([node['host_label'], node['path']])

    def failover(self):
        failover_nodes = []
        for node in self.volume_nodes:
            if not node['primary']:
                failover_nodes.append(":".join([node['host_label'], node['path']]))
        return ";".join(failover_nodes)


class VolumeNode(ApiResource):
    def __init__(self, *args, **kwargs):
        super(VolumeNode, self).__init__(*args, **kwargs)
        self.list_columns.extend(["host_id", "volume_id", "host_label", "path"])
