from __future__ import absolute_import, unicode_literals

from celery import shared_task
from celery.schedules import crontab
from policykit.settings import DISCORD_BOT_TOKEN, DISCORD_CLIENT_ID
from policyengine.models import Proposal, LogAPICall, PlatformPolicy, PlatformAction, BooleanVote, NumberVote
from integrations.discord.models import DiscordCommunity, DiscordUser, DiscordPostMessage
from policyengine.views import filter_policy, check_policy, initialize_policy
from urllib import parse
import urllib.request
import urllib.error
import json
import datetime
import logging
import json

logger = logging.getLogger(__name__)

# Used for Boolean voting
EMOJI_LIKE = '%F0%9F%91%8D'
EMOJI_DISLIKE = '%F0%9F%91%8E'

def is_policykit_action(community, call_type, data, id, type):
    if type == 'message' and data['author']['id'] == DISCORD_CLIENT_ID:
        return True
    else:
        current_time_minus = datetime.datetime.now() - datetime.timedelta(minutes=2)
        logs = LogAPICall.objects.filter(
            proposal_time__gte=current_time_minus,
            call_type=call_type
        )
        if logs.exists():
            for log in logs:
                j_info = json.loads(log.extra_info)
                if id == j_info['id']:
                    return True
    return False

@shared_task
def discord_listener_actions():
    for community in DiscordCommunity.objects.all():
        logger.info('discord: in community loop')
        actions = []

        req = urllib.request.Request('https://discordapp.com/api/guilds/%s/channels' % community.team_id)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header('Authorization', 'Bot %s' % DISCORD_BOT_TOKEN)
        req.add_header("User-Agent", "Mozilla/5.0") # yes, this is strange. discord requires it when using urllib for some weird reason
        resp = urllib.request.urlopen(req)
        channels = json.loads(resp.read().decode('utf-8'))

        for channel in channels:
            if channel['type'] != 0: # We only want to check text channels
                continue

            channel_id = channel['id']

            # Post Message

            req = urllib.request.Request('https://discordapp.com/api/channels/%s/messages' % channel_id)
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header('Authorization', 'Bot %s' % DISCORD_BOT_TOKEN)
            req.add_header("User-Agent", "Mozilla/5.0") # yes, this is strange. discord requires it when using urllib for some weird reason
            resp = urllib.request.urlopen(req)
            messages = json.loads(resp.read().decode('utf-8'))

            call_type = ('channels/%s/messages' % channel_id)
            for message in messages:
                if not is_policykit_action(community, call_type, message, message['id'], 'message'):
                    post = DiscordPostMessage.objects.filter(id=message['id'])
                    if not post.exists():
                        new_api_action = DiscordPostMessage()
                        new_api_action.community = community
                        new_api_action.text = message['content']
                        new_api_action.channel = message['channel_id']
                        new_api_action.id = message['id']

                        u,_ = DiscordUser.objects.get_or_create(username=message['author']['id'],
                                                               community=community)
                        new_api_action.initiator = u
                        actions.append(new_api_action)

            # Rename Channel

            logger.info('discord: about to check rename channels')
            call_type = ('channels/%s' % channel_id)

            id = str(channel['id']) + '_' + channel['name']
            if not is_policykit_action(community, call_type, channel, id, 'channel'):
                logger.info('discord: is_policy_action rename channels')
                c = DiscordRenameChannel.objects.filter(id=id)
                if not c.exists():
                    logger.info('discord: exists rename channels')
                    new_api_action = DiscordRenameChannel()
                    new_api_action.community = community
                    new_api_action.name = channel['name']
                    new_api_action.channel = channel['id']
                    new_api_action.id = id

                    actions.append(new_api_action)

        for action in actions:
            action.community_origin = True
            action.is_bundled = False
            action.save()
            if action.community_revert:
                action.revert()

        # Manage proposals
        logger.info('discord: about to manage proposals')
        proposed_actions = PlatformAction.objects.filter(
            community=community,
            proposal__status=Proposal.PROPOSED,
            community_post__isnull=False
        )
        for proposed_action in proposed_actions:
            channel_id = proposed_action.channel
            message_id = proposed_action.community_post

            # Check if community post still exists
            call = ('channels/%s/messages/%s' % (channel_id, message_id))
            try:
                community.make_call(call)
            except urllib.error.HTTPError as e:
                if e.code == 404: # Message not found
                    proposed_action.delete()
                continue

            # Manage voting
            for reaction in [EMOJI_LIKE, EMOJI_DISLIKE]:
                call = ('channels/%s/messages/%s/reactions/%s' % (channel_id, message_id, reaction))
                users_with_reaction = community.make_call(call)

                for user in users_with_reaction:
                    u = DiscordUser.objects.filter(
                        username=user['id'],
                        community=community
                    )
                    if u.exists():
                        u = u[0]

                        # Manage Boolean voting
                        if reaction in [EMOJI_LIKE, EMOJI_DISLIKE]:
                            val = (reaction == EMOJI_LIKE)

                            bool_vote = BooleanVote.objects.filter(proposal=proposed_action.proposal, user=u)

                            if bool_vote.exists():
                                vote = bool_vote[0]
                                if vote.boolean_value != val:
                                    vote.boolean_value = val
                                    vote.save()
                            else:
                                b = BooleanVote.objects.create(proposal=proposed_action.proposal, user=u, boolean_value=val)

            # Update proposal
            for policy in PlatformPolicy.objects.filter(community=community):
                if filter_policy(policy, proposed_action):
                    cond_result = check_policy(policy, proposed_action)
                    if cond_result == Proposal.PASSED:
                        pass_policy(policy, proposed_action)
                    elif cond_result == Proposal.FAILED:
                        fail_policy(policy, proposed_action)
