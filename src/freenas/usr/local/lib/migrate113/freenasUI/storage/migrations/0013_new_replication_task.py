# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2018-10-17 04:46
from __future__ import unicode_literals

import os

from django.db import migrations, models
import django.db.models.deletion

import freenasUI.freeadmin.models.fields


def process_ssh_keyscan_output(output):
    return [" ".join(line.split()[1:]) for line in output.split("\n") if line and not line.startswith("# ")][-1]


def is_child(child: str, parent: str):
    rel = os.path.relpath(child, parent)
    return rel == "." or not rel.startswith("..")


def repl_compression(apps, schema_editor):
    Replication = apps.get_model('storage', 'Replication')
    for replication in Replication.objects.all():
        replication.repl_compression = (
            None if replication.repl_compression == "off" else replication.repl_compression.upper())
        replication.save()


def migrate_replremotes(apps, schema_editor):
    ReplRemote = apps.get_model('storage', 'ReplRemote')
    KeychainCredential = apps.get_model('system', 'KeychainCredential')

    legacy_replication_key_pair = None
    if os.path.exists("/data/ssh/replication") and os.path.exists("/data/ssh/replication.pub"):
        with open("/data/ssh/replication") as f:
            private_key = f.read()

        with open("/data/ssh/replication.pub") as f:
            public_key = f.read()

        legacy_replication_key_pair = KeychainCredential()
        legacy_replication_key_pair.name = "Legacy Replication SSH Key Pair"
        legacy_replication_key_pair.type = "SSH_KEY_PAIR"
        legacy_replication_key_pair.attributes = {
            "private_key": private_key,
            "public_key": public_key,
        }
        legacy_replication_key_pair.save()

    if legacy_replication_key_pair:
        for repl_remote in ReplRemote.objects.all():
            cipher = "STANDARD"
            ciphers = set(
                replication.repl_remote.ssh_cipher
                for replication in repl_remote.replication_set.all()
            )
            if "fast" in ciphers:
                cipher = "FAST"
            if "none" in ciphers:
                cipher = "NONE"

            username = (repl_remote.ssh_remote_dedicateduser if repl_remote.ssh_remote_dedicateduser_enabled else 'root')
            credential = KeychainCredential()
            credential.name = f'{username}@{repl_remote.ssh_remote_hostname}'
            credential.type = 'SSH_CREDENTIALS'
            credential.attributes = {
                "host": repl_remote.ssh_remote_hostname,
                "port": repl_remote.ssh_remote_port,
                "username": username,
                "private_key": legacy_replication_key_pair.id,
                "remote_host_key": process_ssh_keyscan_output(repl_remote.ssh_remote_hostkey),
                "cipher": cipher,
                "connect_timeout": 7,
            }
            credential.save()

            for replication in repl_remote.replication_set.all():
                replication.repl_ssh_credentials = credential
                replication.save()


def migrate_filesystem(apps, schema_editor):
    Replication = apps.get_model('storage', 'Replication')
    for replication in Replication.objects.all():
        replication.repl_source_datasets = [os.path.normpath(replication.repl_filesystem.strip().strip("/").strip())]
        replication.save()


def target_dataset_normpath(apps, schema_editor):
    Replication = apps.get_model('storage', 'Replication')
    for replication in Replication.objects.all():
        replication.repl_target_dataset = os.path.normpath(replication.repl_target_dataset.strip().strip("/").strip())
        replication.save()


def migrate_followdelete(apps, schema_editor):
    Replication = apps.get_model('storage', 'Replication')
    for replication in Replication.objects.all():
        if replication.repl_followdelete:
            replication.repl_retention_policy = "SOURCE"
            replication.save()


def speed_limit(apps, schema_editor):
    Replication = apps.get_model('storage', 'Replication')
    for replication in Replication.objects.all():
        if replication.repl_speed_limit == 0:
            replication.repl_speed_limit = None
        else:
            replication.repl_speed_limit *= 1024
        replication.save()


def set_defaults(apps, schema_editor):
    Replication = apps.get_model('storage', 'Replication')
    for replication in Replication.objects.all():
        replication.repl_transport = "LEGACY"
        replication.repl_minute = "*"
        replication.repl_large_block = False
        replication.repl_embed = False
        replication.repl_compressed = False
        replication.repl_retries = 1
        replication.save()


class Migration(migrations.Migration):

    dependencies = [
        ('storage', '0012_new_periodic_snapshot_task'),
        ('system', '0034_keychain_credential'),
    ]

    operations = [
        migrations.RunPython(repl_compression),

        migrations.AddField(
            model_name='replication',
            name='repl_direction',
            field=models.CharField(choices=[('PUSH', 'Push'), ('PULL', 'Pull')], default='PUSH', max_length=120, verbose_name='Direction'),
        ),

        migrations.AddField(
            model_name='replication',
            name='repl_transport',
            field=models.CharField(choices=[('SSH', 'SSH'), ('SSH+NETCAT', 'SSH+netcat'), ('LOCAL', 'Local'), ('LEGACY', 'Legacy')], default='SSH', max_length=120, verbose_name='Transport'),
        ),

        migrations.AddField(
            model_name='replication',
            name='repl_ssh_credentials',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='system.SSHCredentialsKeychainCredential', verbose_name='SSH Connection'),
        ),
        migrations.RunPython(migrate_replremotes),
        migrations.RemoveField(
            model_name='replication',
            name='repl_remote',
        ),
        migrations.DeleteModel(
            name='ReplRemote',
        ),

        migrations.AddField(
            model_name='replication',
            name='repl_netcat_active_side',
            field=models.CharField(choices=[('LOCAL', 'Local'), ('REMOTE', 'Remote')], default=None, max_length=120, null=True, verbose_name='Netcat Active Side'),
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_netcat_active_side_port_min',
            field=models.PositiveIntegerField(null=True, verbose_name='Netcat Active Side Min Port'),
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_netcat_active_side_port_max',
            field=models.PositiveIntegerField(null=True, verbose_name='Netcat Active Side Max Port'),
        ),

        migrations.AddField(
            model_name='replication',
            name='repl_source_datasets',
            field=freenasUI.freeadmin.models.fields.ListField(default=[], verbose_name='Source Datasets'),
            preserve_default=False,
        ),
        migrations.RunPython(migrate_filesystem),
        migrations.RemoveField(
            model_name='replication',
            name='repl_filesystem',
        ),
        migrations.AlterModelOptions(
            name='replication',
            options={'verbose_name': 'Replication Task', 'verbose_name_plural': 'Replication Tasks'},
        ),

        migrations.RenameField(
            model_name='replication',
            old_name='repl_zfs',
            new_name='repl_target_dataset',
        ),
        migrations.RunPython(target_dataset_normpath),
        migrations.AlterField(
            model_name='replication',
            name='repl_target_dataset',
            field=models.CharField(help_text='This should be the name of the ZFS filesystem on remote side. eg: Volumename/Datasetname not the mountpoint or filesystem path', max_length=120, verbose_name='Target Dataset'),
        ),

        migrations.RenameField(
            model_name='replication',
            old_name='repl_userepl',
            new_name='repl_recursive',
        ),

        migrations.AddField(
            model_name='replication',
            name='repl_exclude',
            field=freenasUI.freeadmin.models.fields.ListField(default=[], verbose_name='Exclude child datasets'),
            preserve_default=False,
        ),

        migrations.AddField(
            model_name='replication',
            name='repl_periodic_snapshot_tasks',
            field=models.ManyToManyField(blank=True, related_name='replication_tasks', to='storage.Task', verbose_name='Periodic snapshot tasks'),
        ),

        migrations.AddField(
            model_name='replication',
            name='repl_naming_schema',
            field=freenasUI.freeadmin.models.fields.ListField(verbose_name='Also replicate snapshots matching naming schema'),
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_auto',
            field=models.BooleanField(default=True, verbose_name='Run automatically'),
            preserve_default=False,
        ),

        migrations.AddField(
            model_name='replication',
            name='repl_minute',
            field=models.CharField(default='00', help_text='Values allowed:<br>Slider: 0-30 (as it is every Nth minute).<br>Specific Minute: 0-59.', max_length=100, verbose_name='Minute'),
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_hour',
            field=models.CharField(default='*', help_text='Values allowed:<br>Slider: 0-12 (as it is every Nth hour).<br>Specific Hour: 0-23.', max_length=100, verbose_name='Hour'),
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_daymonth',
            field=models.CharField(default='*', help_text='Values allowed:<br>Slider: 0-15 (as its is every Nth day).<br>Specific Day: 1-31.', max_length=100, verbose_name='Day of month'),
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_month',
            field=models.CharField(default='*', max_length=100, verbose_name='Month'),
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_dayweek',
            field=models.CharField(default='*', max_length=100, verbose_name='Day of week'),
        ),

        migrations.AddField(
            model_name='replication',
            name='repl_only_matching_schedule',
            field=models.BooleanField(default=False,
                                      verbose_name='Only replicate snapshots matching schedule'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_allow_from_scratch',
            field=models.BooleanField(default=True,
                                      verbose_name='Replicate from scratch if incremental is not possible'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_hold_pending_snapshots',
            field=models.BooleanField(default=False,
                                      verbose_name='Hold pending snapshots'),
            preserve_default=False,
        ),

        migrations.AddField(
            model_name='replication',
            name='repl_retention_policy',
            field=models.CharField(choices=[('SOURCE', 'Same as source'), ('CUSTOM', 'Custom'), ('NONE', 'None')], default='NONE', max_length=120, verbose_name='Snapshot retention policy'),
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_lifetime_unit',
            field=models.CharField(choices=[('HOUR', 'Hour(s)'), ('DAY', 'Day(s)'), ('WEEK', 'Week(s)'), ('MONTH', 'Month(s)'), ('YEAR', 'Year(s)')], default='WEEK', max_length=120, null=True, verbose_name='Snapshot lifetime unit'),
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_lifetime_value',
            field=models.PositiveIntegerField(default=2, null=True, verbose_name='Snapshot lifetime value'),
        ),
        migrations.RunPython(migrate_followdelete),
        migrations.RemoveField(
            model_name='replication',
            name='repl_followdelete',
        ),

        migrations.AlterField(
            model_name='replication',
            name='repl_compression',
            field=models.CharField(blank=True, choices=[('LZ4', 'lz4 (fastest)'), ('PIGZ', 'pigz (all rounder)'), ('PLZIP', 'plzip (best compression)')], default='LZ4', max_length=120, null=True, verbose_name='Stream Compression'),
        ),

        migrations.RenameField(
            model_name='replication',
            old_name='repl_limit',
            new_name='repl_speed_limit',
        ),
        migrations.AlterField(
            model_name='replication',
            name='repl_speed_limit',
            field=models.IntegerField(blank=True, default=None, help_text='Limit the replication speed. Unit in kilobits/second. 0 = unlimited.', null=True, verbose_name='Limit (kbps)'),
        ),
        migrations.RunPython(speed_limit),

        migrations.AddField(
            model_name='replication',
            name='repl_dedup',
            field=models.BooleanField(default=False,
                                      help_text="Blocks\twhich would have been sent multiple times in\tthe send stream\twill only be sent once. The receiving system must also support this feature to receive a deduplicated\tstream. This flag can be used regard-less of the dataset's dedup property, but performance will be much better if\tthe filesystem uses a dedup-capable checksum (eg. sha256).",
                                      verbose_name='Send deduplicated stream'),
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_large_block',
            field=models.BooleanField(default=True,
                                      help_text='Generate a stream which may contain blocks larger than\t128KB. This flag has no effect if the\tlarge_blocks pool feature is disabled, or if the recordsize\tproperty of this filesystem has never been\tset above 128KB. The receiving\tsystem must have the large_blocks pool feature enabled as well. See zpool-features(7) for details on ZFS feature flags and\tthe large_blocks feature.',
                                      verbose_name='Allow blocks larger than 128KB'),
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_embed',
            field=models.BooleanField(default=True,
                                      help_text='Generate a more compact stream by using WRITE_EMBEDDED records for blocks which are stored more compactly on disk by the embedded_data pool feature. This flag has no effect if the embedded_data feature is disabled. The receiving system must have the embedded_data feature enabled. If the lz4_compress feature is active on the sending system, then the receiving system must have that feature enabled as well. See zpool-features(7) for details on ZFS feature flags and the embedded_data feature.',
                                      verbose_name='Allow WRITE_EMBEDDED records'),
        ),
        migrations.AddField(
            model_name='replication',
            name='repl_compressed',
            field=models.BooleanField(default=True,
                                      help_text='Generate a more compact stream by using compressed WRITE records for blocks which are compressed on disk and in memory (see the compression property for details). If the lz4_compress feature is active on the sending system, then the receiving system must have that feature enabled as well. If the large_blocks feature is enabled on the sending system but the -L option is not supplied in conjunction with -c then the data will be decompressed before sending so it can be split into smaller block sizes. ',
                                      verbose_name='Allow compressed WRITE records'),
        ),

        migrations.AddField(
            model_name='replication',
            name='repl_retries',
            field=models.PositiveIntegerField(default=5, verbose_name='Number of retries for failed replications'),
        ),

        migrations.RemoveField(
            model_name='replication',
            name='repl_lastsnapshot',
        ),

        migrations.RunPython(set_defaults),
    ]
