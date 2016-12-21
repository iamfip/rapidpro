from __future__ import unicode_literals

import requests
import logging
import time

from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from django_redis import get_redis_connection
from djcelery_transactions import task
from enum import Enum
from temba.msgs.models import SEND_MSG_TASK, MSG_QUEUE
from temba.utils import dict_to_struct
from temba.utils.queues import start_task, push_task, nonoverlapping_task, complete_task
from temba.utils.mage import MageClient
from .models import Channel, Alert, ChannelLog, ChannelCount


logger = logging.getLogger(__name__)


class MageStreamAction(Enum):
    activate = 1
    refresh = 2
    deactivate = 3


@task(track_started=True, name='sync_channel_task')
def sync_channel_task(gcm_id, channel_id=None):  # pragma: no cover
    channel = Channel.objects.filter(pk=channel_id).first()
    Channel.sync_channel(gcm_id, channel)


@task(track_started=True, name='send_msg_task')
def send_msg_task():
    """
    Pops the next message off of our msg queue to send.
    """
    # pop off the next task
    org_id, msg_tasks = start_task(SEND_MSG_TASK)

    # it is possible we have no message to send, if so, just return
    if not msg_tasks:  # pragma: needs cover
        return

    if not isinstance(msg_tasks, list):  # pragma: needs cover
        msg_tasks = [msg_tasks]

    r = get_redis_connection()

    # acquire a lock on our contact to make sure two sets of msgs aren't being sent at the same time
    try:
        with r.lock('send_contact_%d' % msg_tasks[0]['contact'], timeout=300):
            # send each of our msgs
            while msg_tasks:
                msg_task = msg_tasks.pop(0)
                msg = dict_to_struct('MockMsg', msg_task,
                                     datetime_fields=['modified_on', 'sent_on', 'created_on', 'queued_on', 'next_attempt'])
                Channel.send_message(msg)

                # if there are more messages to send for this contact, sleep a second before moving on
                if msg_tasks:
                    time.sleep(1)

    finally:  # pragma: no cover
        # mark this worker as done
        complete_task(SEND_MSG_TASK, org_id)

        # if some msgs weren't sent for some reason, then requeue them for later sending
        if msg_tasks:
            # requeue any unsent msgs
            push_task(org_id, MSG_QUEUE, SEND_MSG_TASK, msg_tasks)


@nonoverlapping_task(track_started=True, name='check_channels_task', lock_key='check_channels')
def check_channels_task():
    """
    Run every 30 minutes.  Checks if any channels who are active have not been seen in that
    time.  Triggers alert in that case
    """
    Alert.check_alerts()


@task(track_started=True, name='send_alert_task')
def send_alert_task(alert_id, resolved):
    alert = Alert.objects.get(pk=alert_id)
    alert.send_email(resolved)


@nonoverlapping_task(track_started=True, name='trim_channel_log_task')
def trim_channel_log_task():  # pragma: needs cover
    """
    Runs daily and clears any channel log items older than 48 hours.
    """

    # keep success messages for only two days
    two_days_ago = timezone.now() - timedelta(hours=48)
    ChannelLog.objects.filter(created_on__lte=two_days_ago, is_error=False).delete()

    # keep errors for 30 days
    month_ago = timezone.now() - timedelta(days=30)
    ChannelLog.objects.filter(created_on__lte=month_ago).delete()


@task(track_started=True, name='notify_mage_task')
def notify_mage_task(channel_uuid, action):
    """
    Notifies Mage of a change to a Twitter channel. Having this in a djcelery_transactions task ensures that the channel
    db object is updated before Mage tries to fetch it
    """
    mage = MageClient(settings.MAGE_API_URL, settings.MAGE_AUTH_TOKEN)

    if action == MageStreamAction.activate:
        mage.activate_twitter_stream(channel_uuid)
    elif action == MageStreamAction.refresh:
        mage.refresh_twitter_stream(channel_uuid)
    elif action == MageStreamAction.deactivate:
        mage.deactivate_twitter_stream(channel_uuid)
    else:  # pragma: no cover
        raise ValueError('Invalid action: %s' % action)


@nonoverlapping_task(track_started=True, name="squash_channelcounts", lock_key='squash_channelcounts')
def squash_channelcounts():
    ChannelCount.squash_counts()


@task(track_started=True, name="fb_channel_subscribe")
def fb_channel_subscribe(channel_id):
    channel = Channel.objects.filter(id=channel_id, is_active=True).first()

    if channel:
        page_access_token = channel.config_json()[Channel.CONFIG_AUTH_TOKEN]

        # subscribe to messaging events for this channel
        response = requests.post('https://graph.facebook.com/v2.6/me/subscribed_apps',
                                 params=dict(access_token=page_access_token))

        if response.status_code != 200 or not response.json()['success']:
            print "Unable to subscribe for delivery of events: %s" % response.content
