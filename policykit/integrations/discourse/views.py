from django.shortcuts import render, redirect
from django.http import HttpResponse
from policykit.settings import SERVER_URL
from integrations.discourse.models import DiscourseCommunity, DiscourseUser, DiscourseStarterKit
from policyengine.models import *
from django.contrib.auth import login, authenticate
from django.views.decorators.csrf import csrf_exempt
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
from Crypto import Random
from urllib import parse
import urllib.request
import json
import base64
import secrets
import logging

logger = logging.getLogger(__name__)

# Create your views here.

@csrf_exempt
def configure(request):
    state = request.GET['state']

    context = {
        'state': state
    }

    return render(request, "policyadmin/configure_discourse.html", context)

@csrf_exempt
def request_key(request):
    url = request.POST['url']
    state = request.POST['state']

    request.session['discourse_url'] = url
    request.session['discourse_state'] = state

    key = RSA.generate(2048)
    private_key = key.export_key('PEM')
    public_key = key.publickey().export_key('PEM').decode('utf-8')

    request.session['private_key'] = private_key.decode('utf-8')

    params = {
        'auth_redirect': SERVER_URL + "/discourse/auth",
        'application_name': 'PolicyKit',
        'client_id': secrets.token_hex(16), # 32 random nibbles (not bytes! despite what API doc says)
        'nonce': secrets.token_hex(8), # 16 random nibbles (not bytes! despite what API doc says)
        'scopes': 'read,write,message_bus,session_info',
        'public_key': public_key
    }
    query_string = urllib.parse.urlencode(params)

    response = redirect(url + '/user-api-key/new?' + query_string)
    return response

@csrf_exempt
def auth(request):
    logger.info(request)

    state = request.session['discourse_state']
    url = request.session['discourse_url']
    private_key = RSA.import_key(request.session['private_key'])

    payload_encrypted = request.GET['payload']
    cipher_rsa = PKCS1_v1_5.new(private_key)
    sentinel = Random.new().read(100)
    payload = cipher_rsa.decrypt(base64.b64decode(payload_encrypted), sentinel).decode('utf-8', 'ignore')
    payload_body = payload[payload.index('{"key":'):] # Removes gobbledy-gook heading and returns json string
    payload_body_json = json.loads(payload_body)
    api_key = payload_body_json['key']

    request.session['discourse_api_key'] = api_key

    logger.info(api_key)

    if state == 'policykit_discourse_user_login':
        user = authenticate(request, platform='discourse')
        if user:
            login(request, user)
            response = redirect('/main')
            return response
        else:
            response = redirect('/login?error=invalid_login')
            return response

    elif state == 'policykit_discourse_mod_install':
        community = None
        s = DiscourseCommunity.objects.filter(team_id=url)

        req = urllib.request.Request(url + '/about.json')
        req.add_header("User-Api-Key", api_key)
        resp = urllib.request.urlopen(req)
        res = json.loads(resp.read().decode('utf-8'))

        title = res['about']['title']

        if s.exists():
            community = s[0]
            community.community_name = title
            community.team_id = url
            community.api_key = api_key
            community.save()

            response = redirect('/login?success=true')
            return response
        else:
            user_group,_ = CommunityRole.objects.get_or_create(role_name="Base User", name="Discourse: " + title + ": Base User")

            community = DiscourseCommunity.objects.create(
                community_name=title,
                team_id=url,
                api_key=api_key,
                base_role=user_group
            )
            user_group.community = community
            user_group.save()

            # Get the list of users and create a DiscourseUser object for each user
            req = urllib.request.Request(url + '/admin/users/list.json')
            req.add_header("User-Api-Key", api_key)
            resp = urllib.request.urlopen(req)
            users = json.loads(resp.read().decode('utf-8'))

            for u in users:
                du, _ = DiscourseUser.objects.get_or_create(
                    username=u['username'],
                    readable_name=u['username'],
                    community=community
                )
                du.save()

            context = {
                "starterkits": [kit.name for kit in DiscourseStarterKit.objects.all()],
                "community_name": community.community_name,
                "platform": "discourse"
            }
            return render(request, "policyadmin/init_starterkit.html", context)

    response = redirect('/login?error=no_community_found')
    return response

@csrf_exempt
def action(request):
    json_data = json.loads(request.body)
    logger.info('RECEIVED ACTION')
    logger.info(json_data)

def post_policy(policy, action, users=None, template=None, topic_id=None):
    logger.info('in post_policy')
    from policyengine.models import LogAPICall

    policy_message = "This action is governed by the following policy: " + policy.name
    if template:
        policy_message = template

    data = {
        'raw': policy_message,
        'topic_id': topic_id
    }

    call = '/posts.json'

    logger.info('about to make call in post_policy')
    res = policy.community.make_call(call, values=data)
    data['id'] = res['id']
    logger.info('about to make LogAPICall object in post_policy')
    _ = LogAPICall.objects.create(community=policy.community,
                                  call_type=call,
                                  extra_info=json.dumps(data))

    if action.action_type == "PlatformAction":
        action.community_post = res['id']
        logger.info('about to save action in post_policy')
        action.save()
    logger.info('finished post policy')
