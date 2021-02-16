from __future__ import absolute_import, unicode_literals

from celery import shared_task
from celery.schedules import crontab
from policyengine.models import Proposal, LogAPICall, PlatformPolicy, PlatformAction, BooleanVote, NumberVote
from integrations.discourse.models import DiscourseCommunity, DiscourseUser, DiscourseCreateTopic, DiscourseCreatePost
from policyengine.views import filter_policy, check_policy, initialize_policy
from urllib import parse
import urllib.request
import urllib.error
import json
import datetime
import logging
import json

logger = logging.getLogger(__name__)

def is_policykit_action(community, call_type, topic, policykit_id):
    user_id = topic['posters'][0]['user_id']

    if user_id == policykit_id:
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
                if topic['id'] == j_info['id']:
                    return True
    return False

@shared_task
def discourse_listener_actions():
    logger.info('discourse: listening with celery')
    for community in DiscourseCommunity.objects.all():
        logger.info('discourse: in community loop')
        actions = []

        url = community['team_id']
        api_key = community['api_key']

        req = urllib.request.Request(url + '/session/current.json')
        req.add_header("User-Api-Key", api_key)
        logger.info('discourse: just created request')
        resp = urllib.request.urlopen(req)
        logger.info('discourse: just received response')
        res = json.loads(resp.read().decode('utf-8'))
        logger.info('discourse: just loaded res')
        policykit_id = res['current_user']['id']

        logger.info('discourse: just found policykit id')

        req = urllib.request.Request(url + '/latest.json')
        req.add_header("User-Api-Key", api_key)
        resp = urllib.request.urlopen(req)
        res = json.loads(resp.read().decode('utf-8'))
        topics = res['topic_list']['topics']
        users = res['users']

        logger.info('discourse: just got latest topics')

        for topic in topics:
            logger.info('discourse: in topic loop')
            user_id = topic['posters'][0]['user_id']

            call_type = '/posts.json'
            if not is_policykit_action(community, call_type, topic, policykit_id):
                logger.info('discourse: not policykit action')
                t = DiscourseCreateTopic.objects.filter(id=topic['id'])
                if not t.exists():
                    logger.info('Discourse: creating new DiscourseCreateTopic for: ' + topic['title'])
                    new_api_action = DiscourseCreateTopic()
                    new_api_action.community = community
                    new_api_action.title = topic['title']
                    new_api_action.category = topic['category_id']
                    new_api_action.id = topic['id']

                    for u in users:
                        if u['id'] == user_id:
                            u,_ = DiscourseUser.objects.get_or_create(
                                username=u['username'],
                                community=community
                            )
                            new_api_action.initiator = u
                            actions.append(new_api_action)
                            break

        for action in actions:
            logger.info('discourse: in action loop')
            action.community_origin = True
            action.is_bundled = False
            action.save()
            if action.community_revert:
                action.revert()

        # Manage proposals
        logger.info('discourse: about to manage proposals')
        proposed_actions = PlatformAction.objects.filter(
            community=community,
            proposal__status=Proposal.PROPOSED,
            community_post__isnull=False
        )
        for proposed_action in proposed_actions:
            id = proposed_action.community_post

            req = urllib.request.Request(url + '/posts/' + id + '.json')
            req.add_header("User-Api-Key", api_key)
            resp = urllib.request.urlopen(req)
            res = json.loads(resp.read().decode('utf-8'))
            poll = res['polls'][0]

            # Manage Boolean voting
            for option in poll['options']:
                val = (option['html'] == 'Yes')

                for user in poll['preloaded_voters'][option['id']]:
                    u = DiscourseUser.objects.filter(
                        username=user['id'],
                        community=community
                    )
                    if u.exists():
                        u = u[0]

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