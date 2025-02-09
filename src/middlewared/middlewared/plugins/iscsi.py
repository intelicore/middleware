from middlewared.async_validators import check_path_resides_within_volume
from middlewared.common.attachment import LockableFSAttachmentDelegate
from middlewared.common.listen import ListenDelegate
from middlewared.plugins.zfs_.utils import zvol_name_to_path, zvol_path_to_name
from middlewared.schema import (accepts, Bool, Dict, IPAddr, Int, List, Patch,
                                Str)
from middlewared.service import CallError, CRUDService, private, ServiceChangeMixin, SharingService, ValidationErrors
import middlewared.sqlalchemy as sa
from middlewared.utils import run
from middlewared.utils.path import is_child
from middlewared.validators import Range

import bidict
import errno
import hashlib
import re
import os
import secrets
import pathlib
try:
    import sysctl
except ImportError:
    sysctl = None
import uuid

AUTHMETHOD_LEGACY_MAP = bidict.bidict({
    'None': 'NONE',
    'CHAP': 'CHAP',
    'CHAP Mutual': 'CHAP_MUTUAL',
})
RE_TARGET_NAME = re.compile(r'^[-a-z0-9\.:]+$')


class ISCSIPortalModel(sa.Model):
    __tablename__ = 'services_iscsitargetportal'

    id = sa.Column(sa.Integer(), primary_key=True)
    iscsi_target_portal_tag = sa.Column(sa.Integer(), default=1)
    iscsi_target_portal_comment = sa.Column(sa.String(120))
    iscsi_target_portal_discoveryauthmethod = sa.Column(sa.String(120), default='None')
    iscsi_target_portal_discoveryauthgroup = sa.Column(sa.Integer(), nullable=True)


class ISCSIPortalIPModel(sa.Model):
    __tablename__ = 'services_iscsitargetportalip'
    __table_args__ = (
        sa.Index('services_iscsitargetportalip_iscsi_target_portalip_ip__iscsi_target_portalip_port',
                 'iscsi_target_portalip_ip', 'iscsi_target_portalip_port',
                 unique=True),
    )

    id = sa.Column(sa.Integer(), primary_key=True)
    iscsi_target_portalip_portal_id = sa.Column(sa.ForeignKey('services_iscsitargetportal.id'), index=True)
    iscsi_target_portalip_ip = sa.Column(sa.CHAR(15))
    iscsi_target_portalip_port = sa.Column(sa.SmallInteger(), default=3260)


class ISCSIPortalService(CRUDService):

    class Config:
        datastore = 'services.iscsitargetportal'
        datastore_extend = 'iscsi.portal.config_extend'
        datastore_prefix = 'iscsi_target_portal_'
        namespace = 'iscsi.portal'
        cli_namespace = 'sharing.iscsi.portal'

    @private
    async def config_extend(self, data):
        data['listen'] = []
        for portalip in await self.middleware.call(
            'datastore.query',
            'services.iscsitargetportalip',
            [('portal', '=', data['id'])],
            {'prefix': 'iscsi_target_portalip_'}
        ):
            data['listen'].append({
                'ip': portalip['ip'],
                'port': portalip['port'],
            })
        data['discovery_authmethod'] = AUTHMETHOD_LEGACY_MAP.get(
            data.pop('discoveryauthmethod')
        )
        data['discovery_authgroup'] = data.pop('discoveryauthgroup')
        return data

    @accepts()
    async def listen_ip_choices(self):
        """
        Returns possible choices for `listen.ip` attribute of portal create and update.
        """
        choices = {'0.0.0.0': '0.0.0.0', '::': '::'}
        if (await self.middleware.call('iscsi.global.config'))['alua']:
            # If ALUA is enabled we actually want to show the user the IPs of each node
            # instead of the VIP so its clear its not going to bind to the VIP even though
            # thats the value used under the hoods.
            filters = [('int_vip', 'nin', [None, ''])]
            for i in await self.middleware.call('datastore.query', 'network.Interfaces', filters):
                choices[i['int_vip']] = f'{i["int_address"]}/{i["int_address_b"]}'

            filters = [('alias_vip', 'nin', [None, ''])]
            for i in await self.middleware.call('datastore.query', 'network.Alias', filters):
                choices[i['alias_vip']] = f'{i["alias_address"]}/{i["alias_netmask"]}'
        else:
            for i in await self.middleware.call('interface.query'):
                for alias in i.get('failover_virtual_aliases') or []:
                    choices[alias['address']] = alias['address']
                for alias in i['aliases']:
                    choices[alias['address']] = alias['address']
        return choices

    async def __validate(self, verrors, data, schema, old=None):
        if not data['listen']:
            verrors.add(f'{schema}.listen', 'At least one listen entry is required.')
        else:
            system_ips = await self.listen_ip_choices()
            new_ips = set(i['ip'] for i in data['listen']) - set(i['ip'] for i in old['listen']) if old else set()
            for i in data['listen']:
                filters = [
                    ('iscsi_target_portalip_ip', '=', i['ip']),
                    ('iscsi_target_portalip_port', '=', i['port']),
                ]
                if schema == 'iscsiportal_update':
                    filters.append(('iscsi_target_portalip_portal', '!=', data['id']))
                if await self.middleware.call(
                    'datastore.query', 'services.iscsitargetportalip', filters
                ):
                    verrors.add(f'{schema}.listen', f'{i["ip"]}:{i["port"]} already in use.')

                if (
                    (i['ip'] in new_ips or not new_ips) and
                    i['ip'] not in system_ips
                ):
                    verrors.add(f'{schema}.listen', f'IP {i["ip"]} not configured on this system.')

        if data['discovery_authgroup']:
            if not await self.middleware.call(
                'datastore.query', 'services.iscsitargetauthcredential',
                [('iscsi_target_auth_tag', '=', data['discovery_authgroup'])]
            ):
                verrors.add(
                    f'{schema}.discovery_authgroup',
                    f'Auth Group "{data["discovery_authgroup"]}" not found.',
                    errno.ENOENT,
                )
        elif data['discovery_authmethod'] in ('CHAP', 'CHAP_MUTUAL'):
            verrors.add(f'{schema}.discovery_authgroup', 'This field is required if discovery method is '
                                                         'set to CHAP or CHAP Mutual.')

    @accepts(Dict(
        'iscsiportal_create',
        Str('comment'),
        Str('discovery_authmethod', default='NONE', enum=['NONE', 'CHAP', 'CHAP_MUTUAL']),
        Int('discovery_authgroup', default=None, null=True),
        List('listen', required=True, items=[
            Dict(
                'listen',
                IPAddr('ip', required=True),
                Int('port', default=3260, validators=[Range(min=1, max=65535)]),
            ),
        ]),
        register=True,
    ))
    async def do_create(self, data):
        """
        Create a new iSCSI Portal.

        `discovery_authgroup` is required for CHAP and CHAP_MUTUAL.
        """
        verrors = ValidationErrors()
        await self.__validate(verrors, data, 'iscsiportal_create')
        if verrors:
            raise verrors

        # tag attribute increments sequentially
        data['tag'] = (await self.middleware.call(
            'datastore.query', self._config.datastore, [], {'count': True}
        )) + 1

        listen = data.pop('listen')
        data['discoveryauthgroup'] = data.pop('discovery_authgroup', None)
        data['discoveryauthmethod'] = AUTHMETHOD_LEGACY_MAP.inv.get(data.pop('discovery_authmethod'), 'None')
        pk = await self.middleware.call(
            'datastore.insert', self._config.datastore, data,
            {'prefix': self._config.datastore_prefix}
        )
        try:
            await self.__save_listen(pk, listen)
        except Exception as e:
            await self.middleware.call('datastore.delete', self._config.datastore, pk)
            raise e

        await self._service_change('iscsitarget', 'reload')

        return await self.get_instance(pk)

    async def __save_listen(self, pk, new, old=None):
        """
        Update database with a set new listen IP:PORT tuples.
        It will delete no longer existing addresses and add new ones.
        """
        new_listen_set = set([tuple(i.items()) for i in new])
        old_listen_set = set([tuple(i.items()) for i in old]) if old else set()
        for i in new_listen_set - old_listen_set:
            i = dict(i)
            await self.middleware.call(
                'datastore.insert',
                'services.iscsitargetportalip',
                {'portal': pk, 'ip': i['ip'], 'port': i['port']},
                {'prefix': 'iscsi_target_portalip_'}
            )

        for i in old_listen_set - new_listen_set:
            i = dict(i)
            portalip = await self.middleware.call(
                'datastore.query',
                'services.iscsitargetportalip',
                [('portal', '=', pk), ('ip', '=', i['ip']), ('port', '=', i['port'])],
                {'prefix': 'iscsi_target_portalip_'}
            )
            if portalip:
                await self.middleware.call(
                    'datastore.delete', 'services.iscsitargetportalip', portalip[0]['id']
                )

    @accepts(
        Int('id'),
        Patch(
            'iscsiportal_create',
            'iscsiportal_update',
            ('attr', {'update': True})
        )
    )
    async def do_update(self, pk, data):
        """
        Update iSCSI Portal `id`.
        """

        old = await self.get_instance(pk)

        new = old.copy()
        new.update(data)

        verrors = ValidationErrors()
        await self.__validate(verrors, new, 'iscsiportal_update', old)
        if verrors:
            raise verrors

        listen = new.pop('listen')
        new['discoveryauthgroup'] = new.pop('discovery_authgroup', None)
        new['discoveryauthmethod'] = AUTHMETHOD_LEGACY_MAP.inv.get(new.pop('discovery_authmethod'), 'None')

        await self.__save_listen(pk, listen, old['listen'])

        await self.middleware.call(
            'datastore.update', self._config.datastore, pk, new,
            {'prefix': self._config.datastore_prefix}
        )

        await self._service_change('iscsitarget', 'reload')

        return await self.get_instance(pk)

    @accepts(Int('id'))
    async def do_delete(self, id):
        """
        Delete iSCSI Portal `id`.
        """
        await self.get_instance(id)
        await self.middleware.call(
            'datastore.delete', 'services.iscsitargetgroups', [['iscsi_target_portalgroup', '=', id]]
        )
        await self.middleware.call(
            'datastore.delete', 'services.iscsitargetportalip', [['iscsi_target_portalip_portal', '=', id]]
        )
        result = await self.middleware.call('datastore.delete', self._config.datastore, id)

        for i, portal in enumerate(await self.middleware.call('iscsi.portal.query', [], {'order_by': ['tag']})):
            await self.middleware.call(
                'datastore.update', self._config.datastore, portal['id'], {'tag': i + 1},
                {'prefix': self._config.datastore_prefix}
            )

        await self._service_change('iscsitarget', 'reload')

        return result


class ISCSIPortalListenDelegate(ListenDelegate, ServiceChangeMixin):
    def __init__(self, middleware):
        self.middleware = middleware

    async def get_listen_state(self, ips):
        return await self.middleware.call('datastore.query', 'services.iscsitargetportalip', [['ip', 'in', ips]],
                                          {'prefix': 'iscsi_target_portalip_'})

    async def set_listen_state(self, state):
        for row in state:
            await self.middleware.call('datastore.update', 'services.iscsitargetportalip', row['id'],
                                       {'ip': row['ip']}, {'prefix': 'iscsi_target_portalip_'})

        await self._service_change('iscsitarget', 'reload')

    async def listens_on(self, state, ip):
        return any(row['ip'] == ip for row in state)

    async def reset_listens(self, state):
        for row in state:
            await self.middleware.call('datastore.update', 'services.iscsitargetportalip', row['id'],
                                       {'ip': '0.0.0.0'}, {'prefix': 'iscsi_target_portalip_'})

        await self._service_change('iscsitarget', 'reload')

    async def repr(self, state):
        return {'type': 'SERVICE', 'service': 'iscsi.portal'}


class iSCSITargetAuthCredentialModel(sa.Model):
    __tablename__ = 'services_iscsitargetauthcredential'

    id = sa.Column(sa.Integer(), primary_key=True)
    iscsi_target_auth_tag = sa.Column(sa.Integer(), default=1)
    iscsi_target_auth_user = sa.Column(sa.String(120))
    iscsi_target_auth_secret = sa.Column(sa.EncryptedText())
    iscsi_target_auth_peeruser = sa.Column(sa.String(120))
    iscsi_target_auth_peersecret = sa.Column(sa.EncryptedText())


class iSCSITargetAuthCredentialService(CRUDService):

    class Config:
        namespace = 'iscsi.auth'
        datastore = 'services.iscsitargetauthcredential'
        datastore_prefix = 'iscsi_target_auth_'
        cli_namespace = 'sharing.iscsi.target.auth_credential'

    @accepts(Dict(
        'iscsi_auth_create',
        Int('tag', required=True),
        Str('user', required=True),
        Str('secret', required=True),
        Str('peeruser', default=''),
        Str('peersecret', default=''),
        register=True
    ))
    async def do_create(self, data):
        """
        Create an iSCSI Authorized Access.

        `tag` should be unique among all configured iSCSI Authorized Accesses.

        `secret` and `peersecret` should have length between 12-16 letters inclusive.

        `peeruser` and `peersecret` are provided only when configuring mutual CHAP. `peersecret` should not be
        similar to `secret`.
        """
        verrors = ValidationErrors()
        await self.validate(data, 'iscsi_auth_create', verrors)

        if verrors:
            raise verrors

        data['id'] = await self.middleware.call(
            'datastore.insert', self._config.datastore, data,
            {'prefix': self._config.datastore_prefix}
        )

        await self._service_change('iscsitarget', 'reload')

        return await self.get_instance(data['id'])

    @accepts(
        Int('id'),
        Patch(
            'iscsi_auth_create',
            'iscsi_auth_update',
            ('attr', {'update': True})
        )
    )
    async def do_update(self, id, data):
        """
        Update iSCSI Authorized Access of `id`.
        """
        old = await self.get_instance(id)

        new = old.copy()
        new.update(data)

        verrors = ValidationErrors()
        await self.validate(new, 'iscsi_auth_update', verrors)
        if new['tag'] != old['tag'] and not await self.query([['tag', '=', old['tag']], ['id', '!=', id]]):
            usages = await self.is_in_use_by_portals_targets(id)
            if usages['in_use']:
                verrors.add('iscsi_auth_update.tag', usages['usages'])

        if verrors:
            raise verrors

        await self.middleware.call(
            'datastore.update', self._config.datastore, id, new,
            {'prefix': self._config.datastore_prefix}
        )

        await self._service_change('iscsitarget', 'reload')

        return await self.get_instance(id)

    @accepts(Int('id'))
    async def do_delete(self, id):
        """
        Delete iSCSI Authorized Access of `id`.
        """
        config = await self.get_instance(id)
        if not await self.query([['tag', '=', config['tag']], ['id', '!=', id]]):
            usages = await self.is_in_use_by_portals_targets(id)
            if usages['in_use']:
                raise CallError(usages['usages'])

        return await self.middleware.call(
            'datastore.delete', self._config.datastore, id
        )

    @private
    async def is_in_use_by_portals_targets(self, id):
        config = await self.get_instance(id)
        usages = []
        portals = await self.middleware.call(
            'iscsi.portal.query', [['discovery_authgroup', '=', config['tag']]], {'select': ['id']}
        )
        if portals:
            usages.append(
                f'Authorized access of {id} is being used by portal(s): {", ".join(p["id"] for p in portals)}'
            )
        groups = await self.middleware.call(
            'datastore.query', 'services.iscsitargetgroups', [['iscsi_target_authgroup', '=', config['tag']]]
        )
        if groups:
            usages.append(
                f'Authorized access of {id} is being used by following target(s): '
                f'{", ".join(str(g["iscsi_target"]["id"]) for g in groups)}'
            )

        return {'in_use': bool(usages), 'usages': '\n'.join(usages)}

    @private
    async def validate(self, data, schema_name, verrors):
        secret = data.get('secret')
        peer_secret = data.get('peersecret')
        peer_user = data.get('peeruser', '')

        if not peer_user and peer_secret:
            verrors.add(
                f'{schema_name}.peersecret',
                'The peer user is required if you set a peer secret.'
            )

        if len(secret) < 12 or len(secret) > 16:
            verrors.add(
                f'{schema_name}.secret',
                'Secret must be between 12 and 16 characters.'
            )

        if not peer_user:
            return

        if not peer_secret:
            verrors.add(
                f'{schema_name}.peersecret',
                'The peer secret is required if you set a peer user.'
            )
        elif peer_secret == secret:
            verrors.add(
                f'{schema_name}.peersecret',
                'The peer secret cannot be the same as user secret.'
            )
        elif peer_secret:
            if len(peer_secret) < 12 or len(peer_secret) > 16:
                verrors.add(
                    f'{schema_name}.peersecret',
                    'Peer Secret must be between 12 and 16 characters.'
                )


class iSCSITargetExtentModel(sa.Model):
    __tablename__ = 'services_iscsitargetextent'

    id = sa.Column(sa.Integer(), primary_key=True)
    iscsi_target_extent_name = sa.Column(sa.String(120), unique=True)
    iscsi_target_extent_serial = sa.Column(sa.String(16))
    iscsi_target_extent_type = sa.Column(sa.String(120))
    iscsi_target_extent_path = sa.Column(sa.String(120))
    iscsi_target_extent_filesize = sa.Column(sa.String(120), default=0)
    iscsi_target_extent_blocksize = sa.Column(sa.Integer(), default=512)
    iscsi_target_extent_pblocksize = sa.Column(sa.Boolean(), default=False)
    iscsi_target_extent_avail_threshold = sa.Column(sa.Integer(), nullable=True)
    iscsi_target_extent_comment = sa.Column(sa.String(120))
    iscsi_target_extent_naa = sa.Column(sa.String(34), unique=True)
    iscsi_target_extent_insecure_tpc = sa.Column(sa.Boolean(), default=True)
    iscsi_target_extent_xen = sa.Column(sa.Boolean(), default=False)
    iscsi_target_extent_rpm = sa.Column(sa.String(20), default='SSD')
    iscsi_target_extent_ro = sa.Column(sa.Boolean(), default=False)
    iscsi_target_extent_enabled = sa.Column(sa.Boolean(), default=True)
    iscsi_target_extent_vendor = sa.Column(sa.Text(), nullable=True)


class iSCSITargetExtentService(SharingService):

    share_task_type = 'iSCSI Extent'

    class Config:
        namespace = 'iscsi.extent'
        datastore = 'services.iscsitargetextent'
        datastore_prefix = 'iscsi_target_extent_'
        datastore_extend = 'iscsi.extent.extend'
        cli_namespace = 'sharing.iscsi.extent'

    @private
    async def sharing_task_datasets(self, data):
        if data['type'] == 'DISK':
            if data['path'].startswith('zvol/'):
                return [zvol_path_to_name(f'/dev/{data["path"]}')]
            else:
                return []

        return await super().sharing_task_datasets(data)

    @private
    async def sharing_task_determine_locked(self, data, locked_datasets):
        if data['type'] == 'DISK':
            if data['path'].startswith('zvol/'):
                return any(zvol_path_to_name(f'/dev/{data["path"]}') == d['id'] for d in locked_datasets)
            else:
                return False
        else:
            return await super().sharing_task_determine_locked(data, locked_datasets)

    @accepts(Dict(
        'iscsi_extent_create',
        Str('name', required=True),
        Str('type', enum=['DISK', 'FILE'], default='DISK'),
        Str('disk', default=None, null=True),
        Str('serial', default=None, null=True),
        Str('path', default=None, null=True),
        Int('filesize', default=0),
        Int('blocksize', enum=[512, 1024, 2048, 4096], default=512),
        Bool('pblocksize'),
        Int('avail_threshold', validators=[Range(min=1, max=99)], null=True),
        Str('comment'),
        Bool('insecure_tpc', default=True),
        Bool('xen'),
        Str('rpm', enum=['UNKNOWN', 'SSD', '5400', '7200', '10000', '15000'],
            default='SSD'),
        Bool('ro'),
        Bool('enabled', default=True),
        register=True
    ))
    async def do_create(self, data):
        """
        Create an iSCSI Extent.

        When `type` is set to FILE, attribute `filesize` is used and it represents number of bytes. `filesize` if
        not zero should be a multiple of `blocksize`. `path` is a required attribute with `type` set as FILE.

        With `type` being set to DISK, a valid ZFS volume is required.

        `insecure_tpc` when enabled allows an initiator to bypass normal access control and access any scannable
        target. This allows xcopy operations otherwise blocked by access control.

        `xen` is a boolean value which is set to true if Xen is being used as the iSCSI initiator.

        `ro` when set to true prevents the initiator from writing to this LUN.
        """
        verrors = ValidationErrors()
        await self.validate(data)
        await self.clean(data, 'iscsi_extent_create', verrors)
        verrors.check()

        await self.save(data, 'iscsi_extent_create', verrors)

        data['id'] = await self.middleware.call(
            'datastore.insert', self._config.datastore, {**data, 'vendor': 'TrueNAS'},
            {'prefix': self._config.datastore_prefix}
        )

        return await self.get_instance(data['id'])

    @accepts(
        Int('id'),
        Patch(
            'iscsi_extent_create',
            'iscsi_extent_update',
            ('attr', {'update': True})
        )
    )
    async def do_update(self, id, data):
        """
        Update iSCSI Extent of `id`.
        """
        verrors = ValidationErrors()
        old = await self.get_instance(id)

        new = old.copy()
        new.update(data)

        await self.validate(new)
        await self.clean(
            new, 'iscsi_extent_update', verrors, old=old
        )
        verrors.check()

        await self.save(new, 'iscsi_extent_update', verrors)
        new.pop(self.locked_field)

        await self.middleware.call(
            'datastore.update',
            self._config.datastore,
            id,
            new,
            {'prefix': self._config.datastore_prefix}
        )

        await self._service_change('iscsitarget', 'reload')

        return await self.get_instance(id)

    @accepts(
        Int('id'),
        Bool('remove', default=False),
        Bool('force', default=False),
    )
    async def do_delete(self, id, remove, force):
        """
        Delete iSCSI Extent of `id`.

        If `id` iSCSI Extent's `type` was configured to FILE, `remove` can be set to remove the configured file.
        """
        data = await self.get_instance(id)
        target_to_extents = await self.middleware.call('iscsi.targetextent.query', [['extent', '=', id]])
        active_sessions = await self.middleware.call(
            'iscsi.target.active_sessions_for_targets', [t['target'] for t in target_to_extents]
        )
        if active_sessions:
            sessions_str = f'Associated target(s) {",".join(active_sessions)} ' \
                           f'{"is" if len(active_sessions) == 1 else "are"} in use.'
            if force:
                self.middleware.logger.warning('%s. Forcing deletion of extent.', sessions_str)
            else:
                raise CallError(sessions_str)

        if remove:
            delete = await self.remove_extent_file(data)

            if delete is not True:
                raise CallError('Failed to remove extent file')

        for target_to_extent in target_to_extents:
            await self.middleware.call('iscsi.targetextent.delete', target_to_extent['id'], force)

        try:
            return await self.middleware.call(
                'datastore.delete', self._config.datastore, id
            )
        finally:
            await self._service_change('iscsitarget', 'reload')

    @private
    async def validate(self, data):
        data['serial'] = await self.extent_serial(data['serial'])
        data['naa'] = self.extent_naa(data.get('naa'))

    @private
    async def extend(self, data):
        if data['type'] == 'DISK':
            data['disk'] = data['path']
        elif data['type'] == 'FILE':
            data['disk'] = None
            extent_size = data['filesize']
            # Legacy Compat for having 2[KB, MB, GB, etc] in database
            if not str(extent_size).isdigit():
                suffixes = {
                    'PB': 1125899906842624,
                    'TB': 1099511627776,
                    'GB': 1073741824,
                    'MB': 1048576,
                    'KB': 1024,
                    'B': 1
                }
                for x in suffixes.keys():
                    if str(extent_size).upper().endswith(x):
                        extent_size = str(extent_size).upper().strip(x)
                        extent_size = int(extent_size) * suffixes[x]

                        data['filesize'] = extent_size

        return data

    @private
    async def clean(self, data, schema_name, verrors, old=None):
        await self.clean_name(data, schema_name, verrors, old=old)
        await self.clean_type_and_path(data, schema_name, verrors)
        await self.clean_size(data, schema_name, verrors)

    @private
    async def clean_name(self, data, schema_name, verrors, old=None):
        name = data['name']
        old = old['name'] if old is not None else None
        serial = data['serial']
        name_filters = [('name', '=', name)]
        max_serial_len = 20  # SCST max length

        if '"' in name:
            verrors.add(f'{schema_name}.name', 'Double quotes are not allowed')

        if '"' in serial:
            verrors.add(f'{schema_name}.serial', 'Double quotes are not allowed')

        if len(serial) > max_serial_len:
            verrors.add(
                f'{schema_name}.serial',
                f'Extent serial can not exceed {max_serial_len} characters'
            )

        if name != old or old is None:
            name_result = await self.middleware.call(
                'datastore.query',
                self._config.datastore,
                name_filters,
                {'prefix': self._config.datastore_prefix}
            )
            if name_result:
                verrors.add(f'{schema_name}.name', 'Extent name must be unique')

    @private
    async def clean_type_and_path(self, data, schema_name, verrors):
        if data['type'] is None:
            return data

        extent_type = data['type']
        disk = data['disk']
        path = data['path']
        if extent_type == 'DISK':
            if not disk:
                verrors.add(f'{schema_name}.disk', 'This field is required')
                raise verrors

            if not disk.startswith('zvol/'):
                verrors.add(f'{schema_name}.disk', 'Disk name must start with "zvol/"')
                raise verrors

            device = os.path.join('/dev', disk)

            zvol_name = zvol_path_to_name(device)
            zvol = await self.middleware.call('pool.dataset.query', [['id', '=', zvol_name]])
            if not zvol:
                verrors.add(f'{schema_name}.disk', f'Volume {zvol_name!r} does not exist')

            if not os.path.exists(device):
                verrors.add(f'{schema_name}.disk', f'Device {device!r} for volume {zvol_name!r} does not exist')
        elif extent_type == 'FILE':
            if not path:
                verrors.add(f'{schema_name}.path', 'This field is required')
                raise verrors  # They need this for anything else

            if os.path.exists(path):
                if not os.path.isfile(path) or path[-1] == '/':
                    verrors.add(
                        f'{schema_name}.path',
                        'You need to specify a filepath not a directory'
                    )

            await check_path_resides_within_volume(
                verrors, self.middleware, f'{schema_name}.path', path
            )

        return data

    @private
    async def clean_size(self, data, schema_name, verrors):
        # only applies to files
        if data['type'] != 'FILE':
            return data

        path = data['path']
        size = data['filesize']
        blocksize = data['blocksize']

        if not path:
            verrors.add(f'{schema_name}.path', 'This field is required')
        elif size == 0:
            if not os.path.exists(path) or not os.path.isfile(path):
                verrors.add(
                    f'{schema_name}.path',
                    'The file must exist if the extent size is set to auto (0)'
                )
        elif float(size) % blocksize:
            verrors.add(
                f'{schema_name}.filesize',
                f'File size ({size}) must be a multiple of block size ({blocksize})'
            )

        return data

    @private
    async def extent_serial(self, serial):
        if serial is None:
            used_serials = [i['serial'] for i in (await self.query())]
            tries = 5
            for i in range(tries):
                serial = secrets.token_hex()[:15]
                if serial not in used_serials:
                    break
                else:
                    if i < tries - 1:
                        continue
                    else:
                        raise CallError(
                            'Failed to generate a random extent serial'
                        )

        return serial

    @private
    def extent_naa(self, naa):
        if naa is None:
            return '0x6589cfc000000' + hashlib.sha256(str(uuid.uuid4()).encode()).hexdigest()[0:19]
        else:
            return naa

    @accepts(List('ignore'))
    async def disk_choices(self, ignore):
        """
        Return a dict of available zvols that can be used
        when creating an extent.

        `ignore` is a list of paths (i.e. ['zvol/cargo/zvol01',])
        that will be ignored and included in the returned dict
        of available zvols even if they are already being used.
        For example, if zvol/cargo/zvol01 has already been added to
        an extent, and you pass that path in to this method then it
        will be returned (even though it's being used).
        """
        diskchoices = {}

        zvol_query_filters = [('type', '=', 'DISK')]
        for i in ignore:
            zvol_query_filters.append(('path', 'nin', i))

        used_zvols = [
            i['path'] for i in (await self.query(zvol_query_filters))
        ]

        zvols = await self.middleware.call(
            'pool.dataset.query', [
                ('type', '=', 'VOLUME'),
                ('locked', '=', False)
            ]
        )
        zvol_list = []
        for zvol in zvols:
            zvol_name = zvol['name']
            zvol_size = zvol['volsize']['value']
            zvol_list.append(zvol_name)
            key = os.path.relpath(zvol_name_to_path(zvol_name), '/dev')
            if key not in used_zvols:
                diskchoices[key] = f'{zvol_name} ({zvol_size})'

        zfs_snaps = await self.middleware.call('zfs.snapshot.query', [], {'select': ['name']})
        for snap in zfs_snaps:
            ds_name, snap_name = snap['name'].rsplit('@', 1)
            if ds_name in zvol_list:
                diskchoices[os.path.relpath(zvol_name_to_path(snap['name']), '/dev')] = f'{snap["name"]} [ro]'

        return diskchoices

    @private
    async def save(self, data, schema_name, verrors):
        if data['type'] == 'FILE':
            path = data['path']
            dirs = '/'.join(path.split('/')[:-1])

            # create extent directories
            try:
                pathlib.Path(dirs).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                raise CallError(
                    f'Failed to create {dirs} with error: {e}'
                )

            # create the extent
            if not os.path.exists(path):
                await run(['truncate', '-s', str(data['filesize']), path])

            data.pop('disk', None)
        else:
            data['path'] = data.pop('disk', None)

    @private
    async def remove_extent_file(self, data):
        if data['type'] == 'FILE':
            try:
                os.unlink(data['path'])
            except Exception as e:
                return e

        return True


class iSCSITargetAuthorizedInitiatorModel(sa.Model):
    __tablename__ = 'services_iscsitargetauthorizedinitiator'

    id = sa.Column(sa.Integer(), primary_key=True)
    iscsi_target_initiator_initiators = sa.Column(sa.Text(), default="ALL")
    iscsi_target_initiator_auth_network = sa.Column(sa.Text(), default="ALL")
    iscsi_target_initiator_comment = sa.Column(sa.String(120))


class iSCSITargetAuthorizedInitiator(CRUDService):

    class Config:
        namespace = 'iscsi.initiator'
        datastore = 'services.iscsitargetauthorizedinitiator'
        datastore_prefix = 'iscsi_target_initiator_'
        datastore_extend = 'iscsi.initiator.extend'
        cli_namespace = 'sharing.iscsi.target.authorized_initiator'

    @accepts(Dict(
        'iscsi_initiator_create',
        List('initiators'),
        List('auth_network', items=[IPAddr('ip', network=True)]),
        Str('comment'),
        register=True
    ))
    async def do_create(self, data):
        """
        Create an iSCSI Initiator.

        `initiators` is a list of initiator hostnames which are authorized to access an iSCSI Target. To allow all
        possible initiators, `initiators` can be left empty.

        `auth_network` is a list of IP/CIDR addresses which are allowed to use this initiator. If all networks are
        to be allowed, this field should be left empty.
        """
        await self.compress(data)

        data['id'] = await self.middleware.call(
            'datastore.insert', self._config.datastore, data,
            {'prefix': self._config.datastore_prefix})

        await self._service_change('iscsitarget', 'reload')

        return await self.get_instance(data['id'])

    @accepts(
        Int('id'),
        Patch(
            'iscsi_initiator_create',
            'iscsi_initiator_update',
            ('attr', {'update': True})
        )
    )
    async def do_update(self, id, data):
        """
        Update iSCSI initiator of `id`.
        """
        old = await self.get_instance(id)

        new = old.copy()
        new.update(data)

        await self.compress(new)
        await self.middleware.call(
            'datastore.update', self._config.datastore, id, new,
            {'prefix': self._config.datastore_prefix})

        await self._service_change('iscsitarget', 'reload')

        return await self.get_instance(id)

    @accepts(Int('id'))
    async def do_delete(self, id):
        """
        Delete iSCSI initiator of `id`.
        """
        await self.get_instance(id)
        result = await self.middleware.call(
            'datastore.delete', self._config.datastore, id
        )

        await self._service_change('iscsitarget', 'reload')

        return result

    @private
    async def compress(self, data):
        initiators = data['initiators']
        auth_network = data['auth_network']

        initiators = 'ALL' if not initiators else '\n'.join(initiators)
        auth_network = 'ALL' if not auth_network else '\n'.join(auth_network)

        data['initiators'] = initiators
        data['auth_network'] = auth_network

        return data

    @private
    async def extend(self, data):
        initiators = data['initiators']
        auth_network = data['auth_network']

        initiators = [] if initiators == 'ALL' else initiators.split()
        auth_network = [] if auth_network == 'ALL' else auth_network.split()

        data['initiators'] = initiators
        data['auth_network'] = auth_network

        return data


class iSCSITargetModel(sa.Model):
    __tablename__ = 'services_iscsitarget'

    id = sa.Column(sa.Integer(), primary_key=True)
    iscsi_target_name = sa.Column(sa.String(120), unique=True)
    iscsi_target_alias = sa.Column(sa.String(120), nullable=True, unique=True)
    iscsi_target_mode = sa.Column(sa.String(20), default='iscsi')


class iSCSITargetGroupModel(sa.Model):
    __tablename__ = 'services_iscsitargetgroups'
    __table_args__ = (
        sa.Index(
            'services_iscsitargetgroups_iscsi_target_id__iscsi_target_portalgroup_id',
            'iscsi_target_id', 'iscsi_target_portalgroup_id',
            unique=True
        ),
    )

    id = sa.Column(sa.Integer(), primary_key=True)
    iscsi_target_id = sa.Column(sa.ForeignKey('services_iscsitarget.id'), index=True)
    iscsi_target_portalgroup_id = sa.Column(sa.ForeignKey('services_iscsitargetportal.id'), index=True)
    iscsi_target_initiatorgroup_id = sa.Column(sa.ForeignKey('services_iscsitargetauthorizedinitiator.id',
                                                             ondelete='SET NULL'), index=True, nullable=True)
    iscsi_target_authtype = sa.Column(sa.String(120), default="None")
    iscsi_target_authgroup = sa.Column(sa.Integer(), nullable=True)
    iscsi_target_initialdigest = sa.Column(sa.String(120), default="Auto")


class iSCSITargetService(CRUDService):

    class Config:
        namespace = 'iscsi.target'
        datastore = 'services.iscsitarget'
        datastore_prefix = 'iscsi_target_'
        datastore_extend = 'iscsi.target.extend'
        cli_namespace = 'sharing.iscsi.target'

    @private
    async def extend(self, data):
        data['mode'] = data['mode'].upper()
        data['groups'] = await self.middleware.call(
            'datastore.query',
            'services.iscsitargetgroups',
            [('iscsi_target', '=', data['id'])],
        )
        for group in data['groups']:
            group.pop('id')
            group.pop('iscsi_target')
            group.pop('iscsi_target_initialdigest')
            for i in ('portal', 'initiator'):
                val = group.pop(f'iscsi_target_{i}group')
                if val:
                    val = val['id']
                group[i] = val
            group['auth'] = group.pop('iscsi_target_authgroup')
            group['authmethod'] = AUTHMETHOD_LEGACY_MAP.get(
                group.pop('iscsi_target_authtype')
            )
        return data

    @accepts(Dict(
        'iscsi_target_create',
        Str('name', required=True),
        Str('alias', null=True),
        Str('mode', enum=['ISCSI', 'FC', 'BOTH'], default='ISCSI'),
        List('groups', items=[
            Dict(
                'group',
                Int('portal', required=True),
                Int('initiator', default=None, null=True),
                Str('authmethod', enum=['NONE', 'CHAP', 'CHAP_MUTUAL'], default='NONE'),
                Int('auth', default=None, null=True),
            ),
        ]),
        register=True
    ))
    async def do_create(self, data):
        """
        Create an iSCSI Target.

        `groups` is a list of group dictionaries which provide information related to using a `portal`, `initiator`,
        `authmethod` and `auth` with this target. `auth` represents a valid iSCSI Authorized Access and defaults to
        null.
        """
        verrors = ValidationErrors()
        await self.__validate(verrors, data, 'iscsi_target_create')
        if verrors:
            raise verrors

        await self.compress(data)
        groups = data.pop('groups')
        pk = await self.middleware.call(
            'datastore.insert', self._config.datastore, data,
            {'prefix': self._config.datastore_prefix})
        try:
            await self.__save_groups(pk, groups)
        except Exception as e:
            await self.middleware.call('datastore.delete', self._config.datastore, pk)
            raise e

        await self._service_change('iscsitarget', 'reload')

        return await self.get_instance(pk)

    async def __save_groups(self, pk, new, old=None):
        """
        Update database with a set of new target groups.
        It will delete no longer existing groups and add new ones.
        """
        new_set = set([tuple(i.items()) for i in new])
        old_set = set([tuple(i.items()) for i in old]) if old else set()

        for i in old_set - new_set:
            i = dict(i)
            targetgroup = await self.middleware.call(
                'datastore.query',
                'services.iscsitargetgroups',
                [
                    ('iscsi_target', '=', pk),
                    ('iscsi_target_portalgroup', '=', i['portal']),
                    ('iscsi_target_initiatorgroup', '=', i['initiator']),
                    ('iscsi_target_authtype', '=', i['authmethod']),
                    ('iscsi_target_authgroup', '=', i['auth']),
                ],
            )
            if targetgroup:
                await self.middleware.call(
                    'datastore.delete', 'services.iscsitargetgroups', targetgroup[0]['id']
                )

        for i in new_set - old_set:
            i = dict(i)
            await self.middleware.call(
                'datastore.insert',
                'services.iscsitargetgroups',
                {
                    'iscsi_target': pk,
                    'iscsi_target_portalgroup': i['portal'],
                    'iscsi_target_initiatorgroup': i['initiator'],
                    'iscsi_target_authtype': i['authmethod'],
                    'iscsi_target_authgroup': i['auth'],
                },
            )

    async def __validate(self, verrors, data, schema_name, old=None):

        if not RE_TARGET_NAME.search(data['name']):
            verrors.add(
                f'{schema_name}.name',
                'Lowercase alphanumeric characters plus dot (.), dash (-), and colon (:) are allowed.'
            )
        else:
            filters = [('name', '=', data['name'])]
            if old:
                filters.append(('id', '!=', old['id']))
            names = await self.middleware.call(f'{self._config.namespace}.query', filters, {'force_sql_filters': True})
            if names:
                verrors.add(f'{schema_name}.name', 'Target name already exists')

        if data.get('alias') is not None:
            if '"' in data['alias']:
                verrors.add(f'{schema_name}.alias', 'Double quotes are not allowed')
            elif data['alias'] == 'target':
                verrors.add(f'{schema_name}.alias', 'target is a reserved word')
            else:
                filters = [('alias', '=', data['alias'])]
                if old:
                    filters.append(('id', '!=', old['id']))
                aliases = await self.middleware.call(f'{self._config.namespace}.query', filters, {'force_sql_filters': True})
                if aliases:
                    verrors.add(f'{schema_name}.alias', 'Alias already exists')

        if (
            data['mode'] != 'ISCSI' and
            not await self.middleware.call('system.feature_enabled', 'FIBRECHANNEL')
        ):
            verrors.add(f'{schema_name}.mode', 'Fibre Channel not enabled')

        # Creating target without groups should be allowed for API 1.0 compat
        # if not data['groups']:
        #    verrors.add(f'{schema_name}.groups', 'At least one group is required')

        db_portals = list(
            map(
                lambda v: v['id'],
                await self.middleware.call('datastore.query', 'services.iSCSITargetPortal', [
                    ['id', 'in', list(map(lambda v: v['portal'], data['groups']))]
                ])
            )
        )

        db_initiators = list(
            map(
                lambda v: v['id'],
                await self.middleware.call('datastore.query', 'services.iSCSITargetAuthorizedInitiator', [
                    ['id', 'in', list(map(lambda v: v['initiator'], data['groups']))]
                ])
            )
        )

        portals = []
        for i, group in enumerate(data['groups']):
            if group['portal'] in portals:
                verrors.add(f'{schema_name}.groups.{i}.portal', f'Portal {group["portal"]} cannot be '
                                                                'duplicated on a target')
            elif group['portal'] not in db_portals:
                verrors.add(
                    f'{schema_name}.groups.{i}.portal',
                    f'{group["portal"]} Portal not found in database'
                )
            else:
                portals.append(group['portal'])

            if group['initiator'] and group['initiator'] not in db_initiators:
                verrors.add(
                    f'{schema_name}.groups.{i}.initiator',
                    f'{group["initiator"]} Initiator not found in database'
                )

            if not group['auth'] and group['authmethod'] in ('CHAP', 'CHAP_MUTUAL'):
                verrors.add(f'{schema_name}.groups.{i}.auth', 'Authentication group is required for '
                                                              'CHAP and CHAP Mutual')
            elif group['auth'] and group['authmethod'] == 'CHAP_MUTUAL':
                auth = await self.middleware.call('iscsi.auth.query', [('tag', '=', group['auth'])])
                if not auth:
                    verrors.add(f'{schema_name}.groups.{i}.auth', 'Authentication group not found', errno.ENOENT)
                else:
                    if not auth[0]['peeruser']:
                        verrors.add(f'{schema_name}.groups.{i}.auth', f'Authentication group {group["auth"]} '
                                                                      'does not support CHAP Mutual')

    @accepts(
        Int('id'),
        Patch(
            'iscsi_target_create',
            'iscsi_target_update',
            ('attr', {'update': True})
        )
    )
    async def do_update(self, id, data):
        """
        Update iSCSI Target of `id`.
        """
        old = await self.get_instance(id)
        new = old.copy()
        new.update(data)

        verrors = ValidationErrors()
        await self.__validate(verrors, new, 'iscsi_target_create', old=old)
        if verrors:
            raise verrors

        await self.compress(new)
        groups = new.pop('groups')

        oldgroups = old.copy()
        await self.compress(oldgroups)
        oldgroups = oldgroups['groups']

        await self.middleware.call(
            'datastore.update', self._config.datastore, id, new,
            {'prefix': self._config.datastore_prefix}
        )

        await self.__save_groups(id, groups, oldgroups)

        await self._service_change('iscsitarget', 'reload')

        return await self.get_instance(id)

    @accepts(Int('id'), Bool('force', default=False))
    async def do_delete(self, id, force):
        """
        Delete iSCSI Target of `id`.

        Deleting an iSCSI Target makes sure we delete all Associated Targets which use `id` iSCSI Target.
        """
        target = await self.get_instance(id)
        if await self.active_sessions_for_targets([target['id']]):
            if force:
                self.middleware.logger.warning('Target %s is in use.', target['name'])
            else:
                raise CallError(f'Target {target["name"]} is in use.')
        for target_to_extent in await self.middleware.call('iscsi.targetextent.query', [['target', '=', id]]):
            await self.middleware.call('iscsi.targetextent.delete', target_to_extent['id'], force)

        await self.middleware.call(
            'datastore.delete', 'services.iscsitargetgroups', [['iscsi_target', '=', id]]
        )
        rv = await self.middleware.call('datastore.delete', self._config.datastore, id)

        if await self.middleware.call('service.started', 'iscsitarget'):
            # We explicitly need to do this unfortunately as scst does not accept these changes with a reload
            # So this is the best way to do this without going through a restart of the service
            g_config = await self.middleware.call('iscsi.global.config')
            cp = await run([
                'scstadmin', '-force', '-noprompt', '-rem_target',
                f'{g_config["basename"]}:{target["name"]}', '-driver', 'iscsi'
            ], check=False)
            if cp.returncode:
                self.middleware.logger.error('Failed to remove %r target: %s', target['name'], cp.stderr.decode())

        await self._service_change('iscsitarget', 'reload')
        return rv

    @private
    async def active_sessions_for_targets(self, target_id_list):
        targets = await self.middleware.call(
            'iscsi.target.query', [['id', 'in', target_id_list]],
            {'force_sql_filters': True},
        )
        check_targets = []
        global_basename = (await self.middleware.call('iscsi.global.config'))['basename']
        for target in targets:
            name = target['name']
            if not name.startswith(('iqn.', 'naa.', 'eui.')):
                name = f'{global_basename}:{name}'
            check_targets.append(name)

        return [
            s['target'] for s in await self.middleware.call(
                'iscsi.global.sessions', [['target', 'in', check_targets]]
            )
        ]

    @private
    async def compress(self, data):
        data['mode'] = data['mode'].lower()
        for group in data['groups']:
            group['authmethod'] = AUTHMETHOD_LEGACY_MAP.inv.get(group.pop('authmethod'), 'NONE')
        return data


class iSCSITargetToExtentModel(sa.Model):
    __tablename__ = 'services_iscsitargettoextent'
    __table_args__ = (
        sa.Index('services_iscsitargettoextent_iscsi_target_id_757cc851_uniq',
                 'iscsi_target_id', 'iscsi_extent_id',
                 unique=True),
    )

    id = sa.Column(sa.Integer(), primary_key=True)
    iscsi_extent_id = sa.Column(sa.ForeignKey('services_iscsitargetextent.id'), index=True)
    iscsi_target_id = sa.Column(sa.ForeignKey('services_iscsitarget.id'), index=True)
    iscsi_lunid = sa.Column(sa.Integer())


class iSCSITargetToExtentService(CRUDService):

    class Config:
        namespace = 'iscsi.targetextent'
        datastore = 'services.iscsitargettoextent'
        datastore_prefix = 'iscsi_'
        datastore_extend = 'iscsi.targetextent.extend'
        cli_namespace = 'sharing.iscsi.target.extent'

    @accepts(Dict(
        'iscsi_targetextent_create',
        Int('target', required=True),
        Int('lunid', null=True),
        Int('extent', required=True),
        register=True
    ))
    async def do_create(self, data):
        """
        Create an Associated Target.

        `lunid` will be automatically assigned if it is not provided based on the `target`.
        """
        verrors = ValidationErrors()

        await self.validate(data, 'iscsi_targetextent_create', verrors)

        if verrors:
            raise verrors

        data['id'] = await self.middleware.call(
            'datastore.insert', self._config.datastore, data,
            {'prefix': self._config.datastore_prefix}
        )

        await self._service_change('iscsitarget', 'reload')

        return await self.get_instance(data['id'])

    def _set_null_false(name):
        def set_null_false(attr):
            attr.null = False
        return {'name': name, 'method': set_null_false}

    @accepts(
        Int('id'),
        Patch(
            'iscsi_targetextent_create',
            'iscsi_targetextent_update',
            ('edit', _set_null_false('lunid')),
            ('attr', {'update': True})
        )
    )
    async def do_update(self, id, data):
        """
        Update Associated Target of `id`.
        """
        verrors = ValidationErrors()
        old = await self.get_instance(id)

        new = old.copy()
        new.update(data)

        await self.validate(new, 'iscsi_targetextent_update', verrors, old)

        if verrors:
            raise verrors

        await self.middleware.call(
            'datastore.update', self._config.datastore, id, new,
            {'prefix': self._config.datastore_prefix})

        await self._service_change('iscsitarget', 'reload')

        return await self.get_instance(id)

    @accepts(Int('id'), Bool('force', default=False))
    async def do_delete(self, id, force):
        """
        Delete Associated Target of `id`.
        """
        associated_target = await self.get_instance(id)
        active_sessions = await self.middleware.call(
            'iscsi.target.active_sessions_for_targets', [associated_target['target']]
        )
        if active_sessions:
            if force:
                self.middleware.logger.warning('Associated target %s is in use.', active_sessions[0])
            else:
                raise CallError(f'Associated target {active_sessions[0]} is in use.')

        result = await self.middleware.call(
            'datastore.delete', self._config.datastore, id
        )

        await self._service_change('iscsitarget', 'reload')

        return result

    @private
    async def extend(self, data):
        data['target'] = data['target']['id']
        data['extent'] = data['extent']['id']

        return data

    @private
    async def validate(self, data, schema_name, verrors, old=None):
        if old is None:
            old = {}

        old_lunid = old.get('lunid')
        target = data['target']
        old_target = old.get('target')
        extent = data['extent']
        if data.get('lunid') is None:
            lunids = [
                o['lunid'] for o in await self.query(
                    [('target', '=', target)], {'order_by': ['lunid'], 'force_sql_filters': True}
                )
            ]
            if not lunids:
                lunid = 0
            else:
                diff = sorted(set(range(0, lunids[-1] + 1)).difference(lunids))
                lunid = diff[0] if diff else max(lunids) + 1

            data['lunid'] = lunid
        else:
            lunid = data['lunid']

        # For Linux we have
        # http://github.com/bvanassche/scst/blob/d483590da4de7d32c8371e0712fc186f3d8c509c/scst/include/scst_const.h#L69
        lun_map_size = 16383

        if lunid < 0 or lunid > lun_map_size - 1:
            verrors.add(
                f'{schema_name}.lunid',
                f'LUN ID must be a positive integer and lower than {lun_map_size - 1}'
            )

        if old_lunid != lunid and await self.query([
            ('lunid', '=', lunid), ('target', '=', target)
        ], {'force_sql_filters': True}):
            verrors.add(
                f'{schema_name}.lunid',
                'LUN ID is already being used for this target.'
            )

        if old_target != target and await self.query([
            ('target', '=', target), ('extent', '=', extent)
        ], {'force_sql_filters': True}):
            verrors.add(
                f'{schema_name}.target',
                'Extent is already in this target.'
            )


class ISCSIFSAttachmentDelegate(LockableFSAttachmentDelegate):
    name = 'iscsi'
    title = 'iSCSI Extent'
    service = 'iscsitarget'
    service_class = iSCSITargetExtentService

    async def get_query_filters(self, enabled, options=None):
        return [['type', '=', 'DISK']] + (await super().get_query_filters(enabled, options))

    async def is_child_of_path(self, resource, path):
        dataset_name = os.path.relpath(path, '/mnt')
        full_zvol_path = zvol_name_to_path(dataset_name)
        return is_child(resource[self.path_field], os.path.relpath(full_zvol_path, '/dev'))

    async def delete(self, attachments):
        orphan_targets_ids = set()
        for attachment in attachments:
            for te in await self.middleware.call('iscsi.targetextent.query', [['extent', '=', attachment['id']]]):
                orphan_targets_ids.add(te['target'])
                await self.middleware.call('datastore.delete', 'services.iscsitargettoextent', te['id'])

            await self.middleware.call('datastore.delete', 'services.iscsitargetextent', attachment['id'])
            await self.remove_alert(attachment)

        for te in await self.middleware.call('iscsi.targetextent.query', [['target', 'in', orphan_targets_ids]]):
            orphan_targets_ids.discard(te['target'])
        for target_id in orphan_targets_ids:
            await self.middleware.call('iscsi.target.delete', target_id, True)

        await self._service_change('iscsitarget', 'reload')

    async def restart_reload_services(self, attachments):
        await self._service_change('iscsitarget', 'reload')

    async def stop(self, attachments):
        await self.restart_reload_services(attachments)


async def setup(middleware):
    await middleware.call(
        'interface.register_listen_delegate',
        ISCSIPortalListenDelegate(middleware),
    )
    await middleware.call('pool.dataset.register_attachment_delegate', ISCSIFSAttachmentDelegate(middleware))
