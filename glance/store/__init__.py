# Copyright 2010-2011 OpenStack Foundation
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

import collections
import itertools
import sys

from oslo.config import cfg
from stevedore import driver

#import glance.context
#import glance.domain.proxy
from glance.store.common import exception
from glance.store.common import utils
from glance.store import location
from glance.store.openstack.common.gettextutils import _
from glance.store.openstack.common import importutils
from glance.store.openstack.common import log as logging

LOG = logging.getLogger(__name__)

_DEPRECATED_STORE_OPTS = [
    cfg.DeprecatedOpt('known_stores'),
    cfg.DeprecatedOpt('default_store')
]

_SCRUBBER_OPTS = [
    cfg.StrOpt('scrubber_datadir',
               default='/var/lib/glance/scrubber',
               help=_('Directory that the scrubber will use to track '
                      'information about what to delete. '
                      'Make sure this is set in glance-api.conf and '
                      'glance-scrubber.conf')),
    cfg.BoolOpt('delayed_delete', default=False,
                help=_('Turn on/off delayed delete.')),
    cfg.IntOpt('scrub_time', default=0,
               help=_('The amount of time in seconds to delay before '
                      'performing a delete.')),
]

_STORE_OPTS = [
    cfg.ListOpt('stores', default=['file', 'http'],
                help=_('List of stores enabled'),
                deprecated_opts=[_DEPRECATED_STORE_OPTS[0]]),
    cfg.StrOpt('default_store', default='file',
               help=_("Default scheme to use to store image data. The "
                      "scheme must be registered by one of the stores "
                      "defined by the 'stores' config option."),
               deprecated_opts=[_DEPRECATED_STORE_OPTS[1]])
]

CONF = cfg.CONF
_STORE_CFG_GROUP = "glance_store"


def _oslo_config_options():
    return itertools.chain(((opt, None) for opt in _SCRUBBER_OPTS),
                           ((opt, _STORE_CFG_GROUP) for opt in _STORE_OPTS))


def register_opts(conf):
    for opt, group in _oslo_config_options():
        conf.register_opt(opt, group=group)


class Indexable(object):
    """Indexable for file-like objs iterators

    Wrapper that allows an iterator or filelike be treated as an indexable
    data structure. This is required in the case where the return value from
    Store.get() is passed to Store.add() when adding a Copy-From image to a
    Store where the client library relies on eventlet GreenSockets, in which
    case the data to be written is indexed over.
    """

    def __init__(self, wrapped, size):
        """
        Initialize the object

        :param wrappped: the wrapped iterator or filelike.
        :param size: the size of data available
        """
        self.wrapped = wrapped
        self.size = int(size) if size else (wrapped.len
                                            if hasattr(wrapped, 'len') else 0)
        self.cursor = 0
        self.chunk = None

    def __iter__(self):
        """
        Delegate iteration to the wrapped instance.
        """
        for self.chunk in self.wrapped:
            yield self.chunk

    def __getitem__(self, i):
        """
        Index into the next chunk (or previous chunk in the case where
        the last data returned was not fully consumed).

        :param i: a slice-to-the-end
        """
        start = i.start if isinstance(i, slice) else i
        if start < self.cursor:
            return self.chunk[(start - self.cursor):]

        self.chunk = self.another()
        if self.chunk:
            self.cursor += len(self.chunk)

        return self.chunk

    def another(self):
        """Implemented by subclasses to return the next element"""
        raise NotImplementedError

    def getvalue(self):
        """
        Return entire string value... used in testing
        """
        return self.wrapped.getvalue()

    def __len__(self):
        """
        Length accessor.
        """
        return self.size


def _load_store(conf, store_entry):
    store_cls = None
    try:
        LOG.debug("Attempting to import store %s", store_entry)
        mgr = driver.DriverManager('glance.store.drivers',
                                   store_entry,
                                   invoke_args=[conf],
                                   invoke_on_load=True)
        return mgr.driver
    except RuntimeError as ex:
        raise DriverLoadFailure(store_entry, ex)


def create_stores(conf=CONF):
    """
    Registers all store modules and all schemes
    from the given config. Duplicates are not re-registered.
    """
    store_count = 0
    store_classes = set()

    for store_entry in set(conf.glance_store.stores):
        try:
            # FIXME(flaper87): Don't hide BadStoreConfiguration
            # exceptions. These exceptions should be propagated
            # to the user of the library.
            store_instance = _load_store(conf, store_entry)
        except exception.BadStoreConfiguration as e:
            LOG.warn(_("%s Skipping store driver.") % unicode(e))
            continue
        schemes = store_instance.get_schemes()
        if not schemes:
            raise BackendException('Unable to register store %s. '
                                   'No schemes associated with it.'
                                   % store_cls)
        else:
            LOG.debug("Registering store %s with schemes %s",
                          store_entry, schemes)
            scheme_map = {}
            for scheme in schemes:
                loc_cls = store_instance.get_store_location_class()
                scheme_map[scheme] = {
                    'store': store_instance,
                    'location_class': loc_cls,
                }
            location.register_scheme_map(scheme_map)
            store_count += 1

    return store_count


def verify_default_store():
    scheme = cfg.CONF.default_store
    context = glance.context.RequestContext()
    try:
        get_store_from_scheme(context, scheme)
    except exception.UnknownScheme:
        msg = _("Store for scheme %s not found") % scheme
        raise RuntimeError(msg)


def get_known_schemes():
    """Returns list of known schemes"""
    return location.SCHEME_TO_CLS_MAP.keys()


def get_store_from_scheme(scheme):
    """
    Given a scheme, return the appropriate store object
    for handling that scheme.
    """
    if scheme not in location.SCHEME_TO_CLS_MAP:
        raise exception.UnknownScheme(scheme=scheme)
    scheme_info = location.SCHEME_TO_CLS_MAP[scheme]
    return scheme_info['store']


def get_store_from_uri(uri):
    """
    Given a URI, return the store object that would handle
    operations on the URI.

    :param uri: URI to analyze
    """
    scheme = uri[0:uri.find('/') - 1]
    return get_store_from_scheme(scheme)


def get_from_backend(context, uri, **kwargs):
    """Yields chunks of data from backend specified by uri"""

    loc = location.get_location_from_uri(uri)
    store = get_store_from_uri(uri)

    try:
        return store.get(loc, context)
    except NotImplementedError:
        raise exception.StoreGetNotSupported


def get_size_from_backend(context, uri):
    """Retrieves image size from backend specified by uri"""

    loc = location.get_location_from_uri(uri)
    store = get_store_from_uri(uri)

    return store.get_size(loc)


def delete_from_backend(context, uri, **kwargs):
    """Removes chunks of data from backend specified by uri"""
    loc = location.get_location_from_uri(uri)
    store = get_store_from_uri(uri)

    try:
        return store.delete(loc)
    except NotImplementedError:
        raise exception.StoreDeleteNotSupported


def get_store_from_location(uri):
    """
    Given a location (assumed to be a URL), attempt to determine
    the store from the location.  We use here a simple guess that
    the scheme of the parsed URL is the store...

    :param uri: Location to check for the store
    """
    loc = location.get_location_from_uri(uri)
    return loc.store_name


def safe_delete_from_backend(context, uri, image_id, **kwargs):
    """Given a uri, delete an image from the store."""
    try:
        return delete_from_backend(context, uri, **kwargs)
    except exception.NotFound:
        msg = _('Failed to delete image %s in store from URI')
        LOG.warn(msg % image_id)
    except exception.StoreDeleteNotSupported as e:
        LOG.warn(str(e))
    except UnsupportedBackend:
        exc_type = sys.exc_info()[0].__name__
        msg = (_('Failed to delete image %s from store (%s)') %
               (image_id, exc_type))
        LOG.error(msg)


def schedule_delayed_delete_from_backend(context, uri, image_id, **kwargs):
    """Given a uri, schedule the deletion of an image location."""
    # FIXME(flaper87): Remove this function
    from glance.store import scrubber
    (file_queue, _db_queue) = scrubber.get_scrub_queues()
    # NOTE(zhiyan): Defautly ask glance-api store using file based queue.
    # In future we can change it using DB based queued instead,
    # such as using image location's status to saving pending delete flag
    # when that property be added.
    file_queue.add_location(image_id, uri)


def delete_image_from_backend(context, store_api, image_id, uri):
    if CONF.delayed_delete:
        store_api.schedule_delayed_delete_from_backend(context, uri, image_id)
    else:
        store_api.safe_delete_from_backend(context, uri, image_id)


def check_location_metadata(val, key=''):
    if isinstance(val, dict):
        for key in val:
            check_location_metadata(val[key], key=key)
    elif isinstance(val, list):
        ndx = 0
        for v in val:
            check_location_metadata(v, key='%s[%d]' % (key, ndx))
            ndx = ndx + 1
    elif not isinstance(val, unicode):
        raise BackendException(_("The image metadata key %s has an invalid "
                                 "type of %s.  Only dict, list, and unicode "
                                 "are supported.") % (key, type(val)))


def store_add_to_backend(image_id, data, size, store):
    """
    A wrapper around a call to each stores add() method.  This gives glance
    a common place to check the output

    :param image_id:  The image add to which data is added
    :param data: The data to be stored
    :param size: The length of the data in bytes
    :param store: The store to which the data is being added
    :return: The url location of the file,
             the size amount of data,
             the checksum of the data
             the storage systems metadata dictionary for the location
    """
    (location, size, checksum, metadata) = store.add(image_id, data, size)
    if metadata is not None:
        if not isinstance(metadata, dict):
            msg = (_("The storage driver %s returned invalid metadata %s"
                     "This must be a dictionary type") %
                   (str(store), str(metadata)))
            LOG.error(msg)
            raise BackendException(msg)
        try:
            check_location_metadata(metadata)
        except BackendException as e:
            e_msg = (_("A bad metadata structure was returned from the "
                       "%s storage driver: %s.  %s.") %
                     (str(store), str(metadata), str(e)))
            LOG.error(e_msg)
            raise BackendException(e_msg)
    return (location, size, checksum, metadata)


def add_to_backend(context, scheme, image_id, data, size):
    store = get_store_from_scheme(context, scheme)
    try:
        return store_add_to_backend(image_id, data, size, store)
    except NotImplementedError:
        raise exception.StoreAddNotSupported


def set_acls(context, location_uri, public=False, read_tenants=[],
             write_tenants=[]):
    loc = location.get_location_from_uri(location_uri)
    scheme = get_store_from_location(location_uri)
    store = get_store_from_scheme(context, scheme, loc)
    try:
        store.set_acls(loc, public=public, read_tenants=read_tenants,
                       write_tenants=write_tenants)
    except NotImplementedError:
        LOG.debug(_("Skipping store.set_acls... not implemented."))

def _check_location_uri(context, store_api, uri):
    """
    Check if an image location uri is valid.

    :param context: Glance request context
    :param store_api: store API module
    :param uri: location's uri string
    """
    is_ok = True
    try:
        size = store_api.get_size_from_backend(context, uri)
        # NOTE(zhiyan): Some stores return zero when it catch exception
        is_ok = size > 0
    except (exception.UnknownScheme, exception.NotFound):
        is_ok = False
    if not is_ok:
        raise exception.BadStoreUri(_('Invalid location: %s') % uri)


def _check_image_location(context, store_api, location):
    _check_location_uri(context, store_api, location['url'])
    store_api.check_location_metadata(location['metadata'])


def _set_image_size(context, image, locations):
    if not image.size:
        for location in locations:
            size_from_backend = glance.store.get_size_from_backend(
                context, location['url'])
            if size_from_backend:
                # NOTE(flwang): This assumes all locations have the same size
                image.size = size_from_backend
                break