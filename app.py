# JL HACK ~ CRITICAL: Add missing _sslwrap to Python to maintain gevent compat
# Please see https://github.com/gevent/gevent/issues/477
# ------------------------------------------------------------------------------
# Re-add sslwrap to Python 2.7.9
import inspect
__ssl__ = __import__('ssl')

try:
    _ssl = __ssl__._ssl
except AttributeError:
    _ssl = __ssl__._ssl2


def new_sslwrap(sock, server_side=False, keyfile=None, certfile=None, cert_reqs=__ssl__.CERT_NONE, ssl_version=__ssl__.PROTOCOL_SSLv23, ca_certs=None, ciphers=None):
    context = __ssl__.SSLContext(ssl_version)
    context.verify_mode = cert_reqs or __ssl__.CERT_NONE
    if ca_certs:
        context.load_verify_locations(ca_certs)
    if certfile:
        context.load_cert_chain(certfile, keyfile)
    if ciphers:
        context.set_ciphers(ciphers)

    caller_self = inspect.currentframe().f_back.f_locals['self']
    return context._wrap_socket(sock, server_side=server_side, ssl_sock=caller_self)

if not hasattr(_ssl, 'sslwrap'):
    _ssl.sslwrap = new_sslwrap
# ------------------------------------------------------------------------------

from gevent.monkey import patch_all
patch_all()

import random
import urlparse
import json

from datetime import datetime, timedelta

import pystache
import twilio.twiml

import urllib2
import requests

from flask import (abort, after_this_request, Flask, request, render_template,
                   url_for)
from flask_cache import Cache
from flask_jsonpify import jsonify
from raven.contrib.flask import Sentry
from twilio import TwilioRestException

from models import db, aggregate_stats, log_call, call_count, call_list
from political_data import PoliticalData
from cache_handler import CacheHandler
from fftf_leaderboard import FFTFLeaderboard
from access_control_decorator import crossdomain, requires_auth

try:
    from throttle import Throttle
    throttle = Throttle()
except ImportError:
    throttle = None

app = Flask(__name__)

app.config.from_object('config.ConfigProduction')

cache = Cache(app, config={'CACHE_TYPE': 'simple'})
sentry = Sentry(app)

# db.init_app(app) # JL HACK ~ disable mysql

# Optional Redis cache, for caching Google spreadsheet campaign overrides
cache_handler = CacheHandler(app.config['REDIS_URL'])

# FFTF Leaderboard handler. Only used if FFTF Leadboard params are passed in
leaderboard = FFTFLeaderboard(app.debug, app.config['FFTF_LB_ASYNC_POOL_SIZE'],
    app.config['FFTF_CALL_LOG_API_KEY'])

call_methods = ['GET', 'POST']

data = PoliticalData(cache_handler, app.debug)

print "Call Congress is starting up!"

def make_cache_key(*args, **kwargs):
    path = request.path
    args = str(hash(frozenset(request.args.items())))

    return (path + args).encode('utf-8')


def play_or_say(resp_or_gather, msg_template, **kwds):
    # take twilio response and play or say a mesage
    # can use mustache templates to render keyword arguments
    msg = pystache.render(msg_template, kwds)

    if msg.startswith('http'):
        resp_or_gather.play(msg)
    elif msg:
        resp_or_gather.say(msg)


def full_url_for(route, **kwds):
    return urlparse.urljoin(app.config['APPLICATION_ROOT'],
                            url_for(route, **kwds))


def parse_params(r):

    params = {
        'userPhone': r.values.get('userPhone', r.values.get('From')),
        'campaignId': r.values.get('campaignId', 'default'),
        'zipcode': r.values.get('zipcode', None),
        'repIds': r.values.getlist('repIds'),

        # only used for campaigns of infinite_loop
        'saved_zipcode': r.values.get('saved_zipcode', None),

        'ip_address': r.values.get('ip_address', None),

        # optional values for Fight for the Future org tracking
        'org': r.values.getlist('org'),

        # optional values for Fight for the Future Leaderboards
        # if present, these add extra logging functionality in call_complete
        'fftfCampaign': r.values.get('fftfCampaign'),
        'fftfReferer': r.values.get('fftfReferer'),
        'fftfSession': r.values.get('fftfSession')
    }

    # lookup campaign by ID
    campaign = data.get_campaign(params['campaignId'])

    if not campaign:
        return None, None

    # add repIds to the parameter set, if spec. by the campaign
    if campaign.get('repIds', None):
        if isinstance(campaign['repIds'], basestring):
            params['repIds'] = [campaign['repIds']]
        else:
            params['repIds'] = campaign['repIds']

        if campaign.get('randomize_order', False):
            random.shuffle(params['repIds'])

    if params['userPhone']:
        params['userPhone'] = params['userPhone'].replace('-', '')

    # get representative's id by zip code
    if params['zipcode']:
        params['repIds'] = data.locate_member_ids(
            params['zipcode'], campaign)

        if campaign.get('infinite_loop', False) == True:
            print "Saving zipcode for the future lol"
            params['saved_zipcode'] = params['zipcode']

        # delete the zipcode, since the repIds are in a particular order and
        # will be passed around from endpoint to endpoint hereafter anyway.
        del params['zipcode']

    if params['ip_address'] == None:
        params['ip_address'] = r.headers.get('x-forwarded-for', r.remote_addr)

        if "," in params['ip_address']:
            ips = params['ip_address'].split(", ")
            params['ip_address'] = ips[0]

    if 'random_choice' in campaign:
        # pick a single random choice among a selected set of members
        params['repIds'] = [random.choice(campaign['random_choice'])]

    if app.debug:
        print params

    return params, campaign


def intro_zip_gather(params, campaign):
    resp = twilio.twiml.Response()

    play_or_say(resp, campaign['msg_intro'])

    return zip_gather(resp, params, campaign)


def zip_gather(resp, params, campaign):
    with resp.gather(numDigits=5, method="POST", timeout=30,
                     action=url_for("zip_parse", **params)) as g:
        play_or_say(g, campaign['msg_ask_zip'])

    return str(resp)


def make_calls(params, campaign):
    """
    Connect a user to a sequence of congress members.
    Required params: campaignId, repIds
    Optional params: zipcode, fftfCampaign, fftfReferer, fftfSession
    """
    resp = twilio.twiml.Response()

    selection = request.values.get('Digits', '')

    if selection == "1" and campaign.get('press_1_callback'):

        url = campaign.get('press_1_callback').replace("{phone}",
                params['userPhone'])

        callback_response = get_external_url(url)
        print "--- EXTERNAL CALLBACK RESPONSE: %s" % callback_response

        play_or_say(resp, campaign['msg_press_1'])

        resp.redirect(url_for('_make_calls', **params))
        return str(resp)

    if selection == "9" and campaign.get('press_9_optout'):

        url = pystache.render(campaign.get('press_9_optout'),
            phone=params['userPhone'])

        callback_response = get_external_url(url)
        print "--- OPT OUT RESPONSE: %s" % callback_response

        play_or_say(resp, campaign['msg_opt_out'])

        return str(resp)

    n_reps = len(params['repIds'])

    play_or_say(resp, campaign['msg_call_block_intro'],
                n_reps=n_reps, many_reps=n_reps > 1)

    resp.redirect(url_for('make_single_call', call_index=0, **params))

    return str(resp)

def get_external_url(url_or_json):
    """
    Used to call an external URL callback, used for the press_1_callback or
    press_9_optout to issue some kind of remote web request around the call,
    typically used to schedule a recurring phone call at a later date, outside
    of this app.
    """
    try:
        data = json.loads(url_or_json)
        r = requests.post(data["url"], json=data)
        response = r.status_code
    except:
        user_agent = 'Mozilla/4.0 (compatible; MSIE 5.5; Windows NT)'
        headers = { 'User-Agent' : user_agent }
        request = urllib2.Request(url_or_json, '', headers)
        response = urllib2.urlopen(request).read()
    return response


@app.route('/make_calls', methods=call_methods)
def _make_calls():
    params, campaign = parse_params(request)

    if not params or not campaign:
        abort(404)

    return make_calls(params, campaign)


@app.route('/create', methods=call_methods)
@crossdomain(origin='*')
def call_user():
    """
    Makes a phone call to a user.
    Required Params:
        userPhone
        campaignId
    Optional Params:
        zipcode
        repIds
        fftfCampaign
        fftfReferer
        fftfSession
    """
    # parse the info needed to make the call
    params, campaign = parse_params(request)

    """
    if throttle and throttle.throttle(campaign.get('id'), params['userPhone'],
        params['ip_address'], request.values.get('throttle_key')):
        abort(429) # Too Many Requests
    """

    if not params or not campaign:
        abort(404)

    # initiate the call
    try:
        call = app.config['TW_CLIENT'].calls.create(
            to=params['userPhone'],
            from_=random.choice(campaign['numbers']),
            url=full_url_for("connection", **params),
            if_machine='Hangup' if campaign.get('call_human_check') else None,
            timeLimit=app.config['TW_TIME_LIMIT'],
            timeout=app.config['TW_TIMEOUT'],
            status_callback=full_url_for("call_complete_status", **params))

        result = jsonify(message=call.status, debugMode=app.debug)
        result.status_code = 200 if call.status != 'failed' else 500
    except TwilioRestException, err:
        print err.msg
        result = jsonify(message=err.msg.split(':')[1].strip())
        result.status_code = 200

    return result


@app.route('/connection', methods=call_methods)
@crossdomain(origin='*')
def connection():
    """
    Call handler to connect a user with their congress person(s).
    Required Params:
        campaignId
    Optional Params:
        zipcode
        repIds (if not present go to incoming_call flow and asked for zipcode)
        fftfCampaign
        fftfReferer
        fftfSession
    """
    params, campaign = parse_params(request)

    if not params or not campaign:
        abort(404)

    if params['repIds']:
        resp = twilio.twiml.Response()

        play_or_say(resp, campaign['msg_intro'])

        if campaign.get('skip_star_confirm'):
            resp.redirect(url_for('_make_calls', **params))

            return str(resp)

        action = url_for("_make_calls", **params)

        with resp.gather(numDigits=1, method="POST", timeout=30,
                         action=action) as g:
            play_or_say(g, campaign['msg_intro_confirm'])

            return str(resp)
    else:
        return intro_zip_gather(params, campaign)


@app.route('/incoming_call', methods=call_methods)
def incoming_call():
    """
    Handles incoming calls to the twilio numbers.
    Required Params: campaignId
    Optional Params: fftfCampaign, fftfReferer, fftfSession

    Each Twilio phone number needs to be configured to point to:
    server.com/incoming_call?campaignId=12345
    from twilio.com/user/account/phone-numbers/incoming
    """
    params, campaign = parse_params(request)

    if not params or not campaign:
        abort(404)

    if params['repIds']:
        return connection()
    else:
        return intro_zip_gather(params, campaign)


@app.route("/zip_parse", methods=call_methods)
def zip_parse():
    """
    Handle a zip code entered by the user.
    Required Params: campaignId, Digits
    """
    params, campaign = parse_params(request)

    if not params or not campaign:
        abort(404)

    zipcode = request.values.get('Digits', '')
    rep_ids = data.locate_member_ids(zipcode, campaign)

    if app.debug:
        print 'DEBUG: zipcode = {}'.format(zipcode)

    if not rep_ids:
        resp = twilio.twiml.Response()
        play_or_say(resp, campaign['msg_invalid_zip'])

        return zip_gather(resp, params, campaign)

    params['zipcode'] = zipcode
    params['repIds'] = rep_ids

    return make_calls(params, campaign)


@app.route('/make_single_call', methods=call_methods)
def make_single_call():
    params, campaign = parse_params(request)

    if not params or not campaign:
        abort(404)

    resp = twilio.twiml.Response()

    # return str(resp) # JL HACK ~ disable calls

    i = int(request.values.get('call_index', 0))
    params['call_index'] = i

    if "S_" in params['repIds'][i]:

        special = json.loads(params['repIds'][i].replace("S_", ""))
        to_phone = special['n']                            # "n" is for "number"
        full_name = special['p']                       # "p" is for "politician"

        if full_name == 'SKIP':
            pass
        elif special.get('i'):                                # "i" is for "intro"
            play_or_say(resp, special.get('i'))
        else:
            office = special.get('o', '')                  # "o" is for "office"
            play_or_say(resp, campaign.get('msg_special_call_intro',
                campaign['msg_rep_intro']), name=full_name, office=office)

    else:

        member = [l for l in data.legislators
                  if l['bioguide_id'] == params['repIds'][i]][0]
        to_phone = member['phone']
        title = "Representative" if member['title'] == 'Rep' else 'Senator'
        full_name = unicode("{} {} {}".format(
            title, member['first_name'], member['last_name']), 'utf8')
        title = member['title']
        state = member['state']

        if 'voted_with_list' in campaign and \
                params['repIds'][i] in campaign['voted_with_list']:
            play_or_say(
                resp, campaign['msg_rep_intro_voted_with'], name=full_name, title=title, state=state)
        else:
            play_or_say(resp, campaign['msg_rep_intro'], name=full_name, title=title, state=state)

    if campaign.get('fftf_log_extra_data'):
        leaderboard.log_extra_data(params, campaign, request, to_phone, i)

    if app.debug:
        print u'DEBUG: Call #{}, {} ({}) from {} : make_single_call()'.format(i,
            full_name.encode('ascii', 'ignore'), to_phone, params['userPhone'])

    if not full_name == "SKIP":
        resp.dial(to_phone, callerId=params['userPhone'],
                  timeLimit=app.config['TW_TIME_LIMIT'],
                  timeout=app.config['TW_TIMEOUT'], hangupOnStar=True,
                  action=url_for('call_complete', **params))
    else:
        resp.redirect(url_for('call_complete', **params))

    return str(resp)


@app.route('/call_complete', methods=call_methods)
def call_complete():
    params, campaign = parse_params(request)

    if not params or not campaign:
        abort(404)

    # log_call(params, campaign, request)

    # If FFTF Leaderboard params are present, log this call
    if params['fftfCampaign'] and params['fftfReferer']:
        leaderboard.log_call(params, campaign, request)

    resp = twilio.twiml.Response()

    i = int(request.values.get('call_index', 0))

    if campaign.get('infinite_loop') and params['saved_zipcode']:
        params['zipcode'] = params['saved_zipcode']
        del params['saved_zipcode']
        del params['repIds']
        resp.redirect(url_for('make_single_call', **params))

    elif i == len(params['repIds']) - 1:
        # thank you for calling message
        play_or_say(resp, campaign['msg_final_thanks'])

        # If FFTF Leaderboard params are present, log the call completion status
        if params['fftfCampaign'] and params['fftfReferer']:
            leaderboard.log_complete(params, campaign, request)
    else:
        # call the next representative
        params['call_index'] = i + 1  # increment the call counter

        play_or_say(resp, campaign['msg_between_thanks'])

        resp.redirect(url_for('make_single_call', **params))

    return str(resp)


@app.route('/call_complete_status', methods=call_methods)
def call_complete_status():
    # asynch callback from twilio on call complete
    params, _ = parse_params(request)

    if not params:
        abort(404)

    return jsonify({
        'phoneNumber': request.values.get('To', ''),
        'callStatus': request.values.get('CallStatus', 'unknown'),
        'repIds': params['repIds'],
        'campaignId': params['campaignId'],
        'fftfCampaign': params['fftfCampaign'],
        'fftfReferer': params['fftfReferer'],
        'fftfSession': params['fftfSession']
    })

@app.route('/hello')
def hello():
    return "OHAI"


@app.route('/demo')
def demo():
    return render_template('demo.html')


@cache.cached(timeout=60)
@app.route('/count')
def count():
    @after_this_request
    def add_expires_header(response):
        expires = datetime.utcnow()
        expires = expires + timedelta(seconds=60)
        expires = datetime.strftime(expires, "%a, %d %b %Y %H:%M:%S GMT")

        response.headers['Expires'] = expires

        return response

    campaign = request.values.get('campaign', 'default')

    # return jsonify(campaign=campaign, count=call_count(campaign))
    return jsonify('DISABLED') # JL HACK ~ disable mysql


@cache.cached(timeout=60, key_prefix=make_cache_key)
@app.route('/recent_calls')
def recent_calls():
    @after_this_request
    def add_expires_header(response):
        expires = datetime.utcnow()
        expires = expires + timedelta(seconds=60)
        expires = datetime.strftime(expires, "%a, %d %b %Y %H:%M:%S GMT")

        response.headers['Expires'] = expires

        return response

    campaign = request.values.get('campaign', 'default')
    since = request.values.get('since', datetime.utcnow() - timedelta(days=1))
    limit = request.values.get('limit', 50)

    # calls = call_list(campaign, since, limit)
    # serialized_calls = []
    # if not calls:
    #     return jsonify(campaign=campaign, calls=[], count=0)
    # for c in calls:
    #     s = dict(timestamp = c.timestamp.isoformat(),
    #              number = '%s-%s-XXXX' % (c.areacode, c.exchange))
    #     member = data.get_legislator_by_id(c.member_id)
    #     if member:
    #         s['member'] = dict(
    #                         title=member['title'],
    #                         first_name=member['first_name'],
    #                         last_name=member['last_name']
    #                     )
    #     serialized_calls.append(s)

    # return jsonify(campaign=campaign, calls=serialized_calls, count=len(serialized_calls))
    return jsonify('DISABLED') # JL NOTE ~ disable db

@app.route('/live')
@requires_auth
def live():
    campaign = request.values.get('campaign', 'default')
    return render_template('live.html')


@cache.cached(timeout=60, key_prefix=make_cache_key)
@app.route('/stats')
def stats():
    password = request.values.get('password', None)
    campaign = request.values.get('campaign', 'default')

    # if password == app.config['SECRET_KEY']:
    #     return jsonify(aggregate_stats(campaign))
    # else:
    #     return jsonify(error="access denied")

    return jsonify(error="access denied")   # JL HACK ~ disable mysql


if __name__ == '__main__':
    # load the debugger config
    app.config.from_object('config.Config')
    app.run(host='0.0.0.0')
