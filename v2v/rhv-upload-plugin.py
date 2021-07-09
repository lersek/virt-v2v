# -*- python -*-
# oVirt or RHV upload nbdkit plugin used by ‘virt-v2v -o rhv-upload’
# Copyright (C) 2018 Red Hat Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import builtins
import functools
import inspect
import json
import logging
import queue
import socket
import ssl
import sys
import time

from contextlib import contextmanager
from http.client import HTTPSConnection, HTTPConnection
from urllib.parse import urlparse

import nbdkit

import ovirtsdk4 as sdk
import ovirtsdk4.types as types

# Using version 2 supporting the buffer protocol for better performance.
API_VERSION = 2

# Maximum number of connection to imageio server. Based on testing with imageio
# client, this give best performance.
MAX_CONNECTIONS = 4

# Timeout to wait for oVirt disks to change status, or the transfer
# object to finish initializing [seconds].
timeout = 5 * 60

# Parameters are passed in via a JSON doc from the OCaml code.
# Because this Python code ships embedded inside virt-v2v there
# is no formal API here.
params = None


def config(key, value):
    global params

    if key == "params":
        with builtins.open(value, 'r') as fp:
            params = json.load(fp)
        debug("using params: %s" % params)
    else:
        raise RuntimeError("unknown configuration key '%s'" % key)


def config_complete():
    if params is None:
        raise RuntimeError("missing configuration parameters")


def thread_model():
    """
    Using parallel model to speed up transfer with multiple connections to
    imageio server.
    """
    return nbdkit.THREAD_MODEL_PARALLEL


def debug(s):
    if params['verbose']:
        print(s, file=sys.stderr)
        sys.stderr.flush()


def read_password():
    """
    Read the password from file.
    """
    with builtins.open(params['output_password'], 'r') as fp:
        data = fp.read()
    return data.rstrip()


def parse_username():
    """
    Parse out the username from the output_conn URL.
    """
    parsed = urlparse(params['output_conn'])
    return parsed.username or "admin@internal"


def failing(func):
    """
    Decorator marking the handle as failed if any exception is raised in the
    decorated function.  This is used in close() to cleanup properly after
    failures.
    """
    @functools.wraps(func)
    def wrapper(h, *args):
        try:
            return func(h, *args)
        except:
            h['failed'] = True
            raise

    return wrapper


def open(readonly):
    connection = sdk.Connection(
        url=params['output_conn'],
        username=parse_username(),
        password=read_password(),
        ca_file=params['rhv_cafile'],
        log=logging.getLogger(),
        insecure=params['insecure'],
    )

    # Use the local host is possible.
    host = find_host(connection) if params['rhv_direct'] else None
    disk = create_disk(connection)

    transfer = create_transfer(connection, disk, host)
    try:
        destination_url = parse_transfer_url(transfer)
        http = create_http(destination_url)
        options = get_options(http, destination_url)

        # Close the initial connection to imageio server. When qemu-img will
        # try to access the server, HTTPConnection will reconnect
        # automatically. If we keep this connection idle and qemu-img is too
        # slow getting image extents, imageio server may close the connection,
        # and the import will fail on the first write.
        # See https://bugzilla.redhat.com/1916176.
        http.close()

        pool = create_http_pool(destination_url, host, options)
    except:
        cancel_transfer(connection, transfer)
        raise

    debug("imageio features: flush=%(can_flush)r "
          "zero=%(can_zero)r unix_socket=%(unix_socket)r "
          "max_readers=%(max_readers)r max_writers=%(max_writers)r"
          % options)

    # Save everything we need to make requests in the handle.
    return {
        'can_flush': options['can_flush'],
        'can_zero': options['can_zero'],
        'connection': connection,
        'disk_id': disk.id,
        'transfer': transfer,
        'failed': False,
        'pool': pool,
        'connections': pool.qsize(),
        'path': destination_url.path,
    }


@failing
def can_trim(h):
    return False


@failing
def can_flush(h):
    return h['can_flush']


@failing
def can_fua(h):
    # imageio flush feature is is compatible with NBD_CMD_FLAG_FUA.
    return h['can_flush']


@failing
def get_size(h):
    return params['disk_size']


# Any unexpected HTTP response status from the server will end up calling this
# function which logs the full error, and raises a RuntimeError exception.
def request_failed(r, msg):
    status = r.status
    reason = r.reason
    try:
        body = r.read()
    except EnvironmentError as e:
        body = "(Unable to read response body: %s)" % e

    # Log the full error if we're verbose.
    debug("unexpected response from imageio server:")
    debug(msg)
    debug("%d: %s" % (status, reason))
    debug(body)

    # Only a short error is included in the exception.
    raise RuntimeError("%s: %d %s: %r" % (msg, status, reason, body[:200]))


# For documentation see:
# https://github.com/oVirt/ovirt-imageio/blob/master/docs/random-io.md
# For examples of working code to read/write from the server, see:
# https://github.com/oVirt/ovirt-imageio/blob/master/daemon/test/server_test.py


@failing
def pread(h, buf, offset, flags):
    count = len(buf)
    headers = {"Range": "bytes=%d-%d" % (offset, offset + count - 1)}

    with http_context(h) as http:
        http.request("GET", h['path'], headers=headers)

        r = http.getresponse()
        # 206 = HTTP Partial Content.
        if r.status != 206:
            request_failed(r,
                           "could not read sector offset %d size %d" %
                           (offset, count))

        content_length = int(r.getheader("content-length"))
        if content_length != count:
            # Should never happen.
            request_failed(r,
                           "unexpected Content-Length offset %d size %d got %d" %
                           (offset, count, content_length))

        with memoryview(buf) as view:
            got = 0
            while got < count:
                n = r.readinto(view[got:])
                if n == 0:
                    request_failed(r,
                                   "short read offset %d size %d got %d" %
                                   (offset, count, got))
                got += n


@failing
def pwrite(h, buf, offset, flags):
    count = len(buf)

    flush = "y" if (h['can_flush'] and (flags & nbdkit.FLAG_FUA)) else "n"

    with http_context(h) as http:
        http.putrequest("PUT", h['path'] + "?flush=" + flush)
        # The oVirt server only uses the first part of the range, and the
        # content-length.
        http.putheader("Content-Range", "bytes %d-%d/*" %
                       (offset, offset + count - 1))
        http.putheader("Content-Length", str(count))
        http.endheaders()

        try:
            http.send(buf)
        except BrokenPipeError:
            pass

        r = http.getresponse()
        if r.status != 200:
            request_failed(r,
                           "could not write sector offset %d size %d" %
                           (offset, count))

        r.read()


@failing
def zero(h, count, offset, flags):
    # Unlike the trim and flush calls, there is no 'can_zero' method
    # so nbdkit could call this even if the server doesn't support
    # zeroing.  If this is the case we must emulate.
    if not h['can_zero']:
        emulate_zero(h, count, offset, flags)
        return

    flush = bool(h['can_flush'] and (flags & nbdkit.FLAG_FUA))

    # Construct the JSON request for zeroing.
    buf = json.dumps({'op': "zero",
                      'offset': offset,
                      'size': count,
                      'flush': flush}).encode()

    headers = {"Content-Type": "application/json",
               "Content-Length": str(len(buf))}

    with http_context(h) as http:
        http.request("PATCH", h['path'], body=buf, headers=headers)

        r = http.getresponse()
        if r.status != 200:
            request_failed(r,
                           "could not zero sector offset %d size %d" %
                           (offset, count))

        r.read()


def emulate_zero(h, count, offset, flags):
    flush = "y" if (h['can_flush'] and (flags & nbdkit.FLAG_FUA)) else "n"

    with http_context(h) as http:
        http.putrequest("PUT", h['path'] + "?flush=" + flush)
        http.putheader("Content-Range",
                       "bytes %d-%d/*" % (offset, offset + count - 1))
        http.putheader("Content-Length", str(count))
        http.endheaders()

        try:
            buf = bytearray(128 * 1024)
            while count > len(buf):
                http.send(buf)
                count -= len(buf)
            http.send(memoryview(buf)[:count])
        except BrokenPipeError:
            pass

        r = http.getresponse()
        if r.status != 200:
            request_failed(r,
                           "could not write zeroes offset %d size %d" %
                           (offset, count))

        r.read()


@failing
def flush(h, flags):
    # Construct the JSON request for flushing.
    buf = json.dumps({'op': "flush"}).encode()

    headers = {"Content-Type": "application/json",
               "Content-Length": str(len(buf))}

    # Wait until all inflight requests are completed, and send a flush request
    # for all imageio connections.

    for http in iter_http_pool(h):
        http.request("PATCH", h['path'], body=buf, headers=headers)

        r = http.getresponse()
        if r.status != 200:
            request_failed(r, "could not flush")

        r.read()


def close(h):
    connection = h['connection']
    transfer = h['transfer']
    disk_id = h['disk_id']

    # This is sometimes necessary because python doesn't set up
    # sys.stderr to be line buffered and so debug, errors or
    # exceptions printed previously might not be emitted before the
    # plugin exits.
    sys.stderr.flush()

    close_http_pool(h)

    # If the connection failed earlier ensure we cancel the transfer. Canceling
    # the transfer will delete the disk.
    if h['failed']:
        try:
            cancel_transfer(connection, transfer)
        finally:
            connection.close()
        return

    # Try to finalize the transfer. On errors the transfer may be paused by the
    # system, and we need to cancel the transfer to remove the disk.
    try:
        finalize_transfer(connection, transfer, disk_id)
    except:
        cancel_transfer(connection, transfer)
        raise
    else:
        # Write the disk ID file.  Only do this on successful completion.
        with builtins.open(params['diskid_file'], 'w') as fp:
            fp.write(disk_id)
    finally:
        connection.close()


# Modify http.client.HTTPConnection to work over a Unix domain socket.
# Derived from uhttplib written by Erik van Zijst under an MIT license.
# (https://pypi.org/project/uhttplib/)
# Ported to Python 3 by Irit Goihman.


class UnixHTTPConnection(HTTPConnection):
    def __init__(self, path, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        self.path = path
        HTTPConnection.__init__(self, "localhost", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            self.sock.settimeout(timeout)
        self.sock.connect(self.path)


# oVirt SDK operations


def find_host(connection):
    """Return the current host object or None."""
    try:
        with builtins.open("/etc/vdsm/vdsm.id") as f:
            vdsm_id = f.readline().strip()
    except Exception as e:
        # This is most likely not an oVirt host.
        debug("cannot read /etc/vdsm/vdsm.id, using any host: %s" % e)
        return None

    debug("hw_id = %r" % vdsm_id)

    system_service = connection.system_service()
    storage_name = params['output_storage']
    data_centers = system_service.data_centers_service().list(
        search='storage.name=%s' % storage_name,
        case_sensitive=True,
    )
    if len(data_centers) == 0:
        # The storage domain is not attached to a datacenter
        # (shouldn't happen, would fail on disk creation).
        debug("storange domain (%s) is not attached to a DC" % storage_name)
        return None

    datacenter = data_centers[0]
    debug("datacenter = %s" % datacenter.name)

    hosts_service = system_service.hosts_service()
    hosts = hosts_service.list(
        search="hw_id=%s and datacenter=%s and status=Up"
               % (vdsm_id, datacenter.name),
        case_sensitive=True,
    )
    if len(hosts) == 0:
        # Couldn't find a host that's fulfilling the following criteria:
        # - 'hw_id' equals to 'vdsm_id'
        # - Its status is 'Up'
        # - Belongs to the storage domain's datacenter
        debug("cannot find a running host with hw_id=%r, "
              "that belongs to datacenter '%s', "
              "using any host" % (vdsm_id, datacenter.name))
        return None

    host = hosts[0]
    debug("host.id = %r" % host.id)

    return types.Host(id=host.id)


def create_disk(connection):
    """
    Create a new disk for the transfer and wait until the disk is ready.

    Returns disk object.
    """
    system_service = connection.system_service()
    disks_service = system_service.disks_service()

    if params['disk_format'] == "raw":
        disk_format = types.DiskFormat.RAW
    else:
        disk_format = types.DiskFormat.COW

    disk = disks_service.add(
        disk=types.Disk(
            # The ID is optional.
            id=params.get('rhv_disk_uuid'),
            name=params['disk_name'],
            description="Uploaded by virt-v2v",
            format=disk_format,
            # XXX For qcow2 disk on block storage, we should use the estimated
            # size, based on qemu-img measure of the overlay.
            initial_size=params['disk_size'],
            provisioned_size=params['disk_size'],
            # Handling this properly will be complex, see:
            # https://www.redhat.com/archives/libguestfs/2018-March/msg00177.html
            sparse=True,
            storage_domains=[
                types.StorageDomain(
                    name=params['output_storage'],
                )
            ],
        )
    )

    debug("disk.id = %r" % disk.id)

    # Wait till the disk moved from LOCKED state to OK state, as the transfer
    # can't start if the disk is locked.

    disk_service = disks_service.disk_service(disk.id)
    endt = time.time() + timeout
    while True:
        time.sleep(1)
        disk = disk_service.get()
        if disk.status == types.DiskStatus.OK:
            break
        if time.time() > endt:
            raise RuntimeError(
                "timed out waiting for disk %s to become unlocked" % disk.id)

    return disk


def create_transfer(connection, disk, host):
    """
    Create image transfer and wait until the transfer is ready.

    Returns a transfer object.
    """
    system_service = connection.system_service()
    transfers_service = system_service.image_transfers_service()

    extra = {}
    if transfer_supports_format():
        extra["format"] = types.DiskFormat.RAW

    transfer = transfers_service.add(
        types.ImageTransfer(
            disk=types.Disk(id=disk.id),
            host=host,
            inactivity_timeout=3600,
            **extra,
        )
    )

    # At this point the transfer owns the disk and will delete the disk if the
    # transfer is canceled, or if finalizing the transfer fails.

    debug("transfer.id = %r" % transfer.id)

    # Get a reference to the created transfer service.
    transfer_service = transfers_service.image_transfer_service(transfer.id)

    # Wait until transfer's phase change from INITIALIZING to TRANSFERRING. On
    # errors transfer's phase can change to PAUSED_SYSTEM or FINISHED_FAILURE.
    # If the transfer was paused, we need to cancel it to remove the disk,
    # otherwise the system will remove the disk and transfer shortly after.

    endt = time.time() + timeout
    while True:
        time.sleep(1)
        try:
            transfer = transfer_service.get()
        except sdk.NotFoundError:
            # The system has removed the disk and the transfer.
            raise RuntimeError("transfer %s was removed" % transfer.id)

        if transfer.phase == types.ImageTransferPhase.FINISHED_FAILURE:
            # The system will remove the disk and the transfer soon.
            raise RuntimeError(
                "transfer %s has failed" % transfer.id)

        if transfer.phase == types.ImageTransferPhase.PAUSED_SYSTEM:
            transfer_service.cancel()
            raise RuntimeError(
                "transfer %s was paused by system" % transfer.id)

        if transfer.phase == types.ImageTransferPhase.TRANSFERRING:
            break

        if transfer.phase != types.ImageTransferPhase.INITIALIZING:
            transfer_service.cancel()
            raise RuntimeError(
                "unexpected transfer %s phase %s"
                % (transfer.id, transfer.phase))

        if time.time() > endt:
            transfer_service.cancel()
            raise RuntimeError(
                "timed out waiting for transfer %s" % transfer.id)

    return transfer


def cancel_transfer(connection, transfer):
    """
    Cancel a transfer, removing the transfer disk.
    """
    debug("canceling transfer %s" % transfer.id)
    transfer_service = (connection.system_service()
                        .image_transfers_service()
                        .image_transfer_service(transfer.id))
    transfer_service.cancel()


def finalize_transfer(connection, transfer, disk_id):
    """
    Finalize a transfer, making the transfer disk available.

    If finalizing succeeds, the transfer's disk status will change to OK
    and transfer's phase will change to FINISHED_SUCCESS. Unfortunately,
    the disk status is modified before the transfer finishes, and oVirt
    may still hold a lock on the disk at this point.

    The only way to make sure that the disk is unlocked, is to wait
    until the transfer phase switches FINISHED_SUCCESS. Unfortunately
    oVirt makes this hard to use because the transfer is removed shortly
    after switching the phase to the final phase. However if the
    transfer was removed, we can be sure that the disk is not locked,
    since oVirt releases the locks before removing the transfer.

    On errors, the transfer's phase will change to FINISHED_FAILURE and
    the disk status will change to ILLEGAL and it will be removed. Again
    the transfer will be removed shortly after that.

    If oVirt fails to finalize the transfer, transfer's phase will
    change to PAUSED_SYSTEM. In this case the disk's status will change
    to ILLEGAL and it will not be removed.

    oVirt 4.4.7 made waiting for transfer easier by keeping transfers
    after they complete, but we must support older versions so we have
    generic code that work with any version.

    For more info see:
    - http://ovirt.github.io/ovirt-engine-api-model/4.4/#services/image_transfer
    - http://ovirt.github.io/ovirt-engine-sdk/master/types.m.html#ovirtsdk4.types.ImageTransfer
    """
    debug("finalizing transfer %s" % transfer.id)
    transfer_service = (connection.system_service()
                        .image_transfers_service()
                        .image_transfer_service(transfer.id))

    start = time.time()

    transfer_service.finalize()

    while True:
        time.sleep(1)
        try:
            transfer = transfer_service.get()
        except sdk.NotFoundError:
            # Transfer was removed (ovirt < 4.4.7). We need to check the
            # disk status to understand if the transfer was successful.
            # Due to the way oVirt does locking, we know that the disk
            # is unlocked at this point so we can check only once.

            debug("transfer %s was removed, checking disk %s status"
                  % (transfer.id, disk_id))

            disk_service = (connection.system_service()
                            .disks_service()
                            .disk_service(disk_id))

            try:
                disk = disk_service.get()
            except sdk.NotFoundError:
                raise RuntimeError(
                    "transfer %s failed: disk %s was removed"
                    % (transfer.id, disk_id))

            debug("disk %s is %s" % (disk_id, disk.status))

            if disk.status == types.DiskStatus.OK:
                break

            raise RuntimeError(
                "transfer %s failed: disk is %s" % (transfer.id, disk.status))
        else:
            # Transfer exists, check if it reached one of the final
            # phases, or we timed out.

            debug("transfer %s is %s" % (transfer.id, transfer.phase))

            if transfer.phase == types.ImageTransferPhase.FINISHED_SUCCESS:
                break

            if transfer.phase == types.ImageTransferPhase.FINISHED_FAILURE:
                raise RuntimeError(
                    "transfer %s has failed" % (transfer.id,))

            if transfer.phase == types.ImageTransferPhase.PAUSED_SYSTEM:
                raise RuntimeError(
                    "transfer %s was paused by system" % (transfer.id,))

            if time.time() > start + timeout:
                raise RuntimeError(
                    "timed out waiting for transfer %s to finalize, "
                    "transfer is %s"
                    % (transfer.id, transfer.phase))

    debug("transfer %s finalized in %.3f seconds"
          % (transfer.id, time.time() - start))


def transfer_supports_format():
    """
    Return True if transfer supports the "format" argument, enabing the NBD
    bakend on imageio side, which allows uploading to qcow2 images.

    This feature was added in ovirt 4.3. We assume that the SDK version matches
    engine version.
    """
    sig = inspect.signature(types.ImageTransfer)
    return "format" in sig.parameters


# Connection pool managment


def create_http_pool(url, host, options):
    pool = queue.Queue()

    count = min(options["max_readers"],
                options["max_writers"],
                MAX_CONNECTIONS)

    debug("creating http pool connections=%d" % count)

    unix_socket = options["unix_socket"] if host is not None else None

    for i in range(count):
        http = create_http(url, unix_socket=unix_socket)
        pool.put(http)

    return pool


@contextmanager
def http_context(h):
    """
    Context manager yielding an imageio http connection from the pool. Blocks
    until a connection is available.
    """
    pool = h["pool"]
    http = pool.get()
    try:
        yield http
    finally:
        pool.put(http)


def iter_http_pool(h):
    """
    Wait until all inflight requests are done, and iterate on imageio
    connections.

    The pool is empty during iteration. New requests issued during iteration
    will block until iteration is done.
    """
    pool = h["pool"]
    locked = []

    # Lock the pool by taking the connection out.
    while len(locked) < h["connections"]:
        locked.append(pool.get())

    try:
        for http in locked:
            yield http
    finally:
        # Unlock the pool by puting the connection back.
        for http in locked:
            pool.put(http)


def close_http_pool(h):
    """
    Wait until all inflight requests are done, close all connections and remove
    them from the pool.

    No request can be served by the pool after this call.
    """
    debug("closing http pool")

    pool = h["pool"]
    locked = []

    while len(locked) < h["connections"]:
        locked.append(pool.get())

    for http in locked:
        http.close()


# oVirt imageio operations


def parse_transfer_url(transfer):
    """
    Returns a parsed transfer url, preferring direct transfer if possible.
    """
    if params['rhv_direct']:
        if transfer.transfer_url is None:
            raise RuntimeError("direct upload to host not supported, "
                               "requires ovirt-engine >= 4.2 and only works "
                               "when virt-v2v is run within the oVirt/RHV "
                               "environment, eg. on an oVirt node.")
        return urlparse(transfer.transfer_url)
    else:
        return urlparse(transfer.proxy_url)


def create_http(url, unix_socket=None):
    """
    Create http connection for transfer url.

    Returns HTTPConnection.
    """
    if unix_socket:
        debug("creating unix http connection socket=%r" % unix_socket)
        try:
            return UnixHTTPConnection(unix_socket)
        except Exception as e:
            # Very unlikely, but we can recover by using https.
            debug("cannot create unix socket connection: %s" % e)

    if url.scheme == "https":
        context = \
            ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH,
                                       cafile=params['rhv_cafile'])
        if params['insecure']:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        debug("creating https connection host=%s port=%s" %
              (url.hostname, url.port))
        return HTTPSConnection(url.hostname, url.port, context=context)
    elif url.scheme == "http":
        debug("creating http connection host=%s port=%s" %
              (url.hostname, url.port))
        return HTTPConnection(url.hostname, url.port)
    else:
        raise RuntimeError("unknown URL scheme (%s)" % url.scheme)


def get_options(http, url):
    """
    Send OPTIONS request to imageio server and return options dict.
    """
    http.request("OPTIONS", url.path)
    r = http.getresponse()
    data = r.read()

    if r.status == 200:
        j = json.loads(data)
        features = j["features"]
        return {
            "can_flush": "flush" in features,
            "can_zero": "zero" in features,
            "unix_socket": j.get('unix_socket'),
            "max_readers": j.get("max_readers", 1),
            "max_writers": j.get("max_writers", 1),
        }

    elif r.status == 405 or r.status == 204:
        # Old imageio servers returned either 405 Method Not Allowed or
        # 204 No Content (with an empty body).
        return {
            "can_flush": False,
            "can_zero": False,
            "unix_socket": None,
            "max_readers": 1,
            "max_writers": 1,
        }
    else:
        raise RuntimeError("could not use OPTIONS request: %d: %s" %
                           (r.status, r.reason))
