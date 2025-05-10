#!/usr/bin/python
#
# This script will backup EBS volumes for DB Query. Create an AMI and update
# the Launch Configuration for the ASG.
#
# @author Gregory E. McDaniel

import boto.utils
import boto.ec2
import boto.ec2.autoscale
from boto.ec2.autoscale import LaunchConfiguration
from boto.ec2.blockdevicemapping import BlockDeviceMapping, BlockDeviceType

import sys
import os
import os.path
import logging
import time
from datetime import datetime, timedelta
from subprocess import call

# Setup Logging
logger = logging.getLogger(os.path.basename(__file__))
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(levelname)s - %(asctime)s - %(name)s - %(message)s')
# create time based rotating file handler to write to specified log file.
# file rotates every Sunday and keeps 5 previous file rotations.
if not os.path.exists(os.path.dirname('/var/log/automated_backup.log')):
    os.makedirs(os.path.dirname('/var/log/automated_backup.log'))
trfh = logging.handlers.TimedRotatingFileHandler('/var/log/automated_backup.log', 'W6', 1, 5, utc=True)
# create console handler to write to console (stdout)
ch = logging.StreamHandler()
trfh.setFormatter(formatter)
trfh.setLevel(logging.INFO)
ch.setFormatter(formatter)
ch.setLevel(logging.DEBUG)
logger.addHandler(trfh)
logger.addHandler(ch)

start_message = 'Started backing up DB Query at %(date)s' % {
    'date': datetime.today().strftime('%Y-%m-%d %H:%M:%S')
}
logger.info(start_message)

s3_location = sys.argv[1]

instance_id = boto.utils.get_instance_metadata()['instance-id']
kernel_id = boto.utils.get_instance_metadata()['kernel-id']
root_device_name = boto.utils.get_instance_metadata()['block-device-mapping']['root']

region = boto.utils.get_instance_metadata()['placement']['availability-zone'][:-1]
zone = boto.utils.get_instance_metadata()['placement']['availability-zone']

conn = boto.ec2.connect_to_region(region)

if conn:
    # Used for tagging and deleting old snapshots/images
    today = datetime.now()
    old_backup_limit = today - timedelta(days=1)

    # Used to tag snapshots in case they don't get removed by this script
    expiration_date = (today + timedelta(days=2)).strftime("%Y-%m-%d")

    instance = conn.get_all_instances([instance_id])[0].instances[0]

    root_vol_id = instance.block_device_mapping['/dev/sda1'].volume_id
    data_vol_id = instance.block_device_mapping['/dev/sdb'].volume_id

    root_snapshot = None
    data_snapshot = None

    try:
        logger.info("Freezing XFS data volume and Beginning Snapshots")
        # Freeze the jackdb02-data volume for a snapshot
        call(["xfs_freeze", "-f", "/data"])

        # Snapshot both EBS Volumes
        root_volume = conn.get_all_volumes([root_vol_id])[0]
        data_volume = conn.get_all_volumes([data_vol_id])[0]

        root_snapshot = root_volume.create_snapshot(description="jackdb02-root hourly backup")
        data_snapshot = data_volume.create_snapshot(description="jackdb02-data hourly backup")
    except:
        logger.error("Failed to initiate snapshot(s)", exc_info=True)
        raise
    finally:
        # Don't have to wait for the snapshot to finish. Unfreeze the jackdb02-data volume
        call(["xfs_freeze", "-u", "/data"])
        logger.info("Unfreezing the XFS data volume")

    # Wait for snapshots to complete
    while root_snapshot.status != 'completed' or data_snapshot.status != 'completed':
        time.sleep(2)
        root_snapshot.update()
        data_snapshot.update()
        if root_snapshot.status == 'error' or data_snapshot.status == 'error':
            logger.error("Failed to create snapshot(s)")
            exit()

    try:
        logger.info("Tagging Snapshots")

        root_snapshot.add_tag('Expiration', expiration_date)
        root_snapshot.add_tag('Name', 'jackdb02-root')

        data_snapshot.add_tag('Expiration', expiration_date)
        data_snapshot.add_tag('Name', 'jackdb02-data')
    except:
        logger.error("Failed to tag snapshot(s) [%(id1)s, %(id2)s]" % {
            'id1' : root_snapshot.id,
            'id2' : data_snapshot.id
        }, exc_info=True)

    # Create AMI from snapshots of the EBS volumes
    ami_name = "jackdb02-ami_" + today.strftime("%Y%m%d%H")
    ami_desc = today.isoformat()
    ami_arch = "x86_64"
    ami_kern = kernel_id
    ami_root = root_device_name
    ami_bkdm = BlockDeviceMapping()
    ami_virt = "paravirtual"

    sda1 = BlockDeviceType()
    sdb = BlockDeviceType()
    sdc = BlockDeviceType()
    sdd = BlockDeviceType()

    sda1.delete_on_termination = True
    sda1.size = 25
    sda1.snapshot_id = root_snapshot.id
    sda1.volume_type = 'gp2'

    sdb.delete_on_termination = True
    sdb.size = 250
    sdb.snapshot_id = data_snapshot.id
    sdb.volume_type = 'gp2'

    sdc.ephemeral_name = 'ephemeral0'

    sdd.ephemeral_name = 'ephemeral1'

    ami_bkdm['/dev/sda1'] = sda1
    ami_bkdm['/dev/sdb'] = sdb
    ami_bkdm['/dev/sdc'] = sdc
    ami_bkdm['/dev/sdd'] = sdd

    jackdb02_ami_id = None

    try:
        logger.info("Registering new AMI")
        jackdb02_ami_id = conn.register_image(name=ami_name, description=ami_desc, architecture=ami_arch,
                            kernel_id=ami_kern, root_device_name=ami_root, block_device_map=ami_bkdm,
                            virtualization_type=ami_virt)
    except:
        logger.error("Failed to create jackdb02 AMI", exc_info=True)
        raise

    logger.info("Tagging AMI")
    error_count = 0
    isAvailable = False
    while not isAvailable:
        try:
            error_count += 1
            conn.create_tags([jackdb02_ami_id], { "Name" : "jackdb02-ami" } )
            isAvailable = True
        except:
            time.sleep(1)
        if error_count >= 10:
            break

    if not isAvailable:
        logger.error("Failed to tag jackdb02 AMI, couldn't find AMI [%(id)s]" % { 'id': jackdb02_ami_id }, exc_info=True)
        exit()

    # Get current images to iterate through and delete if they are too old
    jackdb02_amis = conn.get_all_images(filters={'tag-key':'Name', 'tag-value':'jackdb02-ami'})

    # If there are images and they are older than a day, try to delete them. If it fails, no big deal.
    if jackdb02_amis:
        for image in jackdb02_amis:
            image_date = datetime.strptime(image.description[:19], "%Y-%m-%dT%H:%M:%S")
            if old_backup_limit > image_date:
                try:
                    logger.info("Deregistering AMI '%(id)s'" % {
                        'id': image.id
                    })
                    image.deregister()
                except:
                    logger.error("Failed to deregister jackdb02 AMI, %(id)s" % { 'id': image.id }, exc_info=True)

    # Get current root and data snapshots to iterate through and delete if they are too old
    # Have to get all and filter in memory because boto breaks.
    all_snapshots = conn.get_all_snapshots()
    jackdb02_root_snapshots = []
    jackdb02_data_snapshots = []
    for snapshot in all_snapshots:
        if snapshot.tags.has_key('Name'):
            if snapshot.tags['Name'] == 'jackdb02-root':
                jackdb02_root_snapshots.append(snapshot)
            if snapshot.tags['Name'] == 'jackdb02-data':
                jackdb02_data_snapshots.append(snapshot)

    # If there are jackdb02-root snapshots and they are older than a day, try to delete them. If it fails, no big deal.
    if jackdb02_root_snapshots:
        for snapshot in jackdb02_root_snapshots:
            snapshot_date = datetime.strptime(snapshot.start_time[:19], "%Y-%m-%dT%H:%M:%S")
            if old_backup_limit > snapshot_date:
                try:
                    logger.info("Deleting Snapshot '%(id)s'" % {
                        'id': snapshot.id
                    })
                    snapshot.delete()
                except:
                    logger.error("Failed to delete old Root EBS Snapshot, %(id)s" % { 'id': snapshot.id }, exc_info=True)

    # If there are jackdb02-data snapshots and they are older than a day, try to delete them. If it fails, no big deal.
    if jackdb02_data_snapshots:
        for snapshot in jackdb02_data_snapshots:
            snapshot_date = datetime.strptime(snapshot.start_time[:19], "%Y-%m-%dT%H:%M:%S")
            if old_backup_limit > snapshot_date:
                try:
                    logger.info("Deleting Snapshot '%(id)s'" % {
                        'id': snapshot.id
                    })
                    snapshot.delete()
                except:
                    logger.error("Failed to delete old Data EBS Snapshot, %(id)s" % { 'id': snapshot.id }, exc_info=True)

    # Get AutoScaling Group Name
    asg_name = instance.tags['aws:autoscaling:groupName']

    # Connect to autoscaling in EC2
    as_conn = boto.ec2.autoscale.connect_to_region(region)

    if as_conn:
        as_group = as_conn.get_all_groups(names=[asg_name])[0]
        lc = as_conn.get_all_launch_configurations(names=[as_group.launch_config_name])[0]

        lc_name = 'jackdb02launchconfig_' + today.strftime("%Y%m%d%H")
        lc_inst = lc.instance_type
        lc_keyn = lc.key_name
        lc_secg = lc.security_groups
        lc_imag = jackdb02_ami_id
        lc_ramd = lc.ramdisk_id
        lc_kern = lc.kernel_id
        lc_user = "#cloud-config\nruncmd:\n  - 'aws --region=\"" + region + "\" s3 cp s3://" + s3_location + "/scripts/tag_volumes.py /tmp/tag_volumes.py'\n  - 'python /tmp/tag_volumes.py'"
        lc_insm = lc.instance_monitoring
        lc_insp = lc.instance_profile_name
        lc_ebso = lc.ebs_optimized

        logger.info("Creating new LaunchConfiguration for the ASG")
        new_lc = LaunchConfiguration(name=lc_name, image_id=lc_imag, key_name=lc_keyn, security_groups=lc_secg,
                    user_data=lc_user, instance_type=lc_inst, kernel_id=lc_kern, ramdisk_id=lc_ramd,
                    instance_monitoring=lc_insm, instance_profile_name=lc_insp, ebs_optimized=lc_ebso)

        as_conn.create_launch_configuration(new_lc)

        logger.info("Updating the LaunchConfiguration on the ASG")
        as_group.launch_config_name = lc_name
        try:
            as_group.update()
        except:
            logger.error("Failed to update the ASG with new LC", exc_info=True)
            raise

        logger.info("Deleting old LC")
        try:
            lc.delete()
        except:
            logger.error("Failed to delete old LC for the ASG", exc_info=True)
            raise

logger.info('Finished backing up DB Query at %(date)s' % {
    'date': datetime.today().strftime('%Y-%m-%d %H:%M:%S')
})
