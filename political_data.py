import csv
import yaml
import random
import time
import urllib2
import json

class PoliticalData():

    SPREADSHEET_CACHE_TIMEOUT = 60 # seconds

    overrides_data = {}     # the google spreadsheet overrides for each campaign
    scrape_times = {}       # the last google scrape timestamps for campaigns

    exclusions = {}         # the google spreadsheet exclusions per campaign
    exclusion_scrapes = {}  # the last google scrape timestamps for exclusions

    cache_handler = None
    campaigns = None
    legislators = None
    districts = None
    debug_mode = False

    def __init__(self, cache_handler, debug_mode):
        """
        Load data in database
        """
        legislators = []

        self.cache_handler = cache_handler
        self.debug_mode = debug_mode

        with open('data/legislators.csv') as f:
            reader = csv.DictReader(f)

            for legislator in reader:
                if legislator['type'] == 'sen':
                    legislator['chamber'] = 'senate'
                else:
                    legislator['chamber'] = 'house'

                # Turn 'rep' into 'Rep' and 'sen' into 'Sen'
                legislator['title'] = legislator['type'].capitalize()

                legislators.append(legislator)

        districts = []

        with open('data/districts.csv') as f:
            reader = csv.DictReader(
                f, fieldnames=['zipcode', 'state', 'district_number'])

            for district in reader:
                districts.append(district)

        with open('data/campaigns.yaml', 'r') as f:
            campaigns = {c['id']: c for c in yaml.load(f.read())}

        self.campaigns = campaigns
        self.legislators = legislators
        self.districts = districts

    def get_campaign(self, campaign_id):
        if campaign_id in self.campaigns:
            return dict(self.campaigns['default'],
                        **self.campaigns[campaign_id])

    def get_senators(self, districts, get_one=False):
        states = [d['state'] for d in districts]

        senators = [l for l in self.legislators
                if l['chamber'] == 'senate'
                and l['state'] in states]

        random.shuffle(senators)    # mix it up! always do this :)

        if senators and get_one:
            return [random.choice(senators)]
        else:
            return senators

    def get_house_members(self, districts, get_one=False):
        states = [d['state'] for d in districts]
        district_numbers = [d['district_number'] for d in districts]

        reps = [l for l in self.legislators
                if l['chamber'] == 'house'
                and l['state'] in states
                and l['district'] in district_numbers]

        if reps and get_one:
            return [random.choice(reps)]
        else:
            return reps

    def get_legislator_by_id(self, member_id):
        for l in self.legislators:
            if l['bioguide_id'] == member_id:
                return l
        return None

    def format_special_call(self, name, number, office='', intro = None):
        return "S_%s" % json.dumps({
            'p': name, 'n': number, 'i': intro,
            'o': office})

    def pick_lucky_recipients(self, list_so_far, campaign, which='first',num=1):
        lucky = campaign.get('extra_%s_calls' % which)
        random.shuffle(lucky)
        lucky = lucky[0:num]

        for person in lucky:

            if isinstance(person, basestring):
                p = self.get_legislator_by_id(person)

                if not p or not p.get('phone'):
                    continue

                person = {
                    "name": "%s %s"%(p.get('first_name'), p.get('last_name')),
                    "number": p.get('phone')
                }

            special_call = self.format_special_call(
                person.get('name'),
                person.get('number'),
                person.get('office', ''),
                person.get('intro', None)
            )
            if which == 'first':
                list_so_far.insert(0, special_call)
            else:
                list_so_far.append(special_call)

        return list_so_far

    def locate_member_ids(self, zipcode, campaign):
        """get congressional member ids from zip codes to districts data"""
        local_districts = [d for d in self.districts
                           if d['zipcode'] == str(zipcode)]
        member_ids = []

        individual_target = campaign.get('target_member_id', None)

        if individual_target:
            member_ids = [individual_target]
            return member_ids

        target_senate = campaign.get('target_senate')
        target_house_first = campaign.get('target_house_first')
        target_house = campaign.get('target_house')

        # Instantiate some extra variables related to state-specific overrides
        target_individual = None
        state = None
        first_call_name = None
        first_call_number = None

        # check if there's a google spreadsheet with state-specific overrides
        if self.has_special_overrides(local_districts, campaign):

            overrides = self.get_override_values(local_districts, campaign)

            target_senate = overrides['target_senate']
            target_house_first = overrides['target_house_first']
            target_house = overrides['target_house']
            target_individual = overrides['target_individual']
            first_call_name = overrides['first_call_name']
            first_call_number = overrides['first_call_number']
            state = overrides['_STATE_ABBREV']

        # filter list by campaign target_house, target_senate
        if target_senate and not target_house_first:
            sens = [s['bioguide_id'] for s
                        in self.get_senators(local_districts, campaign.get('only_call_1_sen', False))]
            if self.debug_mode:
                print "got %s sens" % sens
            random.shuffle(sens)
            member_ids.extend(sens)

        if target_house:
            reps = [h['bioguide_id'] for h
                       in self.get_house_members(local_districts, campaign.get('only_call_1_rep', False))]
            if self.debug_mode:
                print "got %s reps" % reps
            member_ids.extend(reps)

        if target_senate and target_house_first:
            sens = [s['bioguide_id'] for s
                       in self.get_senators(local_districts, campaign.get('only_call_1_sen', False))]
            if self.debug_mode:
                print "got %s sens" % sens
            random.shuffle(sens)
            member_ids.extend(sens)


        if campaign.get('randomize_order', False):
            random.shuffle(member_ids)

        # if targeting an individual by name, pop them to the front of the list
        # JL NOTE ~ Tony C=>A<=rdenas (C001097) has bad data, unicode warning
        if target_individual != None and target_individual != "":
            for l in self.legislators:
                if l['last_name'] == target_individual and l['state'] == state:
                    if l['bioguide_id'] in member_ids:
                            member_ids.remove(l['bioguide_id'])     # janky
                            member_ids.insert(0, l['bioguide_id'])  # lol

        if campaign.get('max_calls_to_congress', False):
            member_ids = member_ids[0:campaign.get('max_calls_to_congress')]

        # Now handle any exclusions lol
        if campaign.get('exclusions_google_spreadsheet_id'):
            exclusions = self.get_exclusions(campaign)
            for exclusion in exclusions:
                exclusion = exclusion.encode('ascii', errors='backslashreplace')
                if exclusion in member_ids:
                    print "Politician %s is on exclusion list!" % exclusion
                member_ids = filter(lambda a: a != exclusion, member_ids)

        if campaign.get('extra_first_calls'):
            member_ids = self.pick_lucky_recipients(member_ids, campaign,
                           'first', campaign.get('number_of_extra_first_calls'))

        if campaign.get('extra_first_call_name') and \
                campaign.get('extra_first_call_num'):
            first_call = self.format_special_call(
                campaign.get('extra_first_call_name'),
                "%d" % campaign.get('extra_first_call_num'))
            member_ids.insert(0, first_call)

        if first_call_number and first_call_name:
            first_call = self.format_special_call(first_call_name,
                            first_call_number)
            member_ids.insert(0, first_call)

        if campaign.get('extra_last_calls'):
            member_ids = self.pick_lucky_recipients(member_ids, campaign,
                             'last', campaign.get('number_of_extra_last_calls'))

        if campaign.get('extra_last_call_name') and \
                campaign.get('extra_last_call_num'):
            last_call = self.format_special_call(
                campaign.get('extra_last_call_name'),
                "%d" % campaign.get('extra_last_call_num'),
                '',
                campaign.get('extra_last_call_intro'))
            member_ids.extend([last_call])

        print member_ids

        return member_ids

    def get_override_values(self, local_districts, campaign):

        overrides = self.get_overrides(campaign)

        states = [d['state'] for d in local_districts]

        for state in states:
            override = overrides.get(state)
            if override:
                override['_STATE_ABBREV'] = state
                if self.debug_mode:
                    print "Found overrides: %s / %s" % (state, str(override))
                return overrides.get(state)

        return None

    def has_special_overrides(self, local_districts, campaign):

        spreadsheet_id = campaign.get('overrides_google_spreadsheet_id', None)

        if spreadsheet_id == None:
            return False

        overrides = self.get_overrides(campaign)

        states = [d['state'] for d in local_districts]

        for state in states:
            if overrides.get(state):
                return True

        return False

    def get_exclusions(self, campaign):

        last_scraped = time.time()-self.exclusion_scrapes.get(campaign['id'], 0)
        expired = last_scraped > self.SPREADSHEET_CACHE_TIMEOUT

        if self.exclusions.get(campaign.get('id')) == None or expired:
            self.populate_exclusions(campaign)

        return self.exclusions[campaign.get('id')]

    def populate_exclusions(self, campaign):

        spreadsheet_id = campaign.get('exclusions_google_spreadsheet_id', None)
        spreadsheet_key = '%s-exclusions-list' % campaign.get('id')

        exclusions_data = self.cache_handler.get(spreadsheet_key, None)

        if exclusions_data == None:
            exclusions = self.grab_exclusions_from_google(
                            spreadsheet_id,
                            campaign.get('exclusions_spreadsheet_match_field'),
                            campaign.get('exclusions_spreadsheet_match_value'),
                            campaign.get('exclusions_spreadsheet_bioguide_col'))
            if self.debug_mode:
                print "GOT DATA FROM GOOGLE: %s" % str(exclusions)

            self.cache_handler.set(
                spreadsheet_key,
                json.dumps(exclusions),
                self.SPREADSHEET_CACHE_TIMEOUT)

            self.exclusion_scrapes[campaign.get('id')] = time.time()
        else:
            exclusions = json.loads(exclusions_data)
            if self.debug_mode:
                print "GOT DATA FROM CACHE: %s" % str(exclusions)

        self.exclusions[campaign.get('id')] = exclusions

    def grab_exclusions_from_google(self, spreadsheet_id, field, val, bioguide):

        url = ('https://spreadsheets.google.com/feeds/list/'
                '%s/default/public/values?alt=json') % spreadsheet_id

        response = urllib2.urlopen(url).read()
        data = json.loads(response)

        exclusions = []

        for row in data['feed']['entry']:
            if row.get('gsx$%s' % field) and row['gsx$%s' % field].get('$t') \
                    and row['gsx$%s' % field]['$t'] == val \
                    and row.get('gsx$%s' % bioguide) \
                    and row['gsx$%s' % bioguide].get('$t'):
                exclusions.append(str(row['gsx$%s' % bioguide]['$t']).encode(
                    'ascii', errors='backslashreplace'))

        return exclusions


    def get_overrides(self, campaign):

        # we expire whatever we're holding in memory after the timeout
        last_scraped = time.time()-self.scrape_times.get(campaign.get('id'), 0)
        expired = last_scraped > self.SPREADSHEET_CACHE_TIMEOUT

        if self.overrides_data.get(campaign.get('id')) == None or expired:
            self.populate_overrides(campaign)

        return self.overrides_data[campaign.get('id')]

    def populate_overrides(self, campaign):

        spreadsheet_id = campaign.get('overrides_google_spreadsheet_id', None)
        spreadsheet_key = '%s-spreadsheet-data' % campaign.get('id')

        overrides_data = self.cache_handler.get(spreadsheet_key, None)

        if overrides_data == None:
            overrides = self.grab_overrides_from_google(spreadsheet_id)
            if self.debug_mode:
                print "GOT DATA FROM GOOGLE: %s" % str(overrides)

            self.cache_handler.set(
                spreadsheet_key,
                json.dumps(overrides),
                self.SPREADSHEET_CACHE_TIMEOUT)

            self.scrape_times[campaign.get('id')] = time.time()
        else:
            overrides = json.loads(overrides_data)
            if self.debug_mode:
                print "GOT DATA FROM CACHE: %s" % str(overrides)

        self.overrides_data[campaign.get('id')] = overrides

    def grab_overrides_from_google(self, spreadsheet_id):

        url = ('https://spreadsheets.google.com/feeds/list/'
                '%s/default/public/values?alt=json') % spreadsheet_id

        response = urllib2.urlopen(url).read()
        data = json.loads(response)

        def is_true(val):
            return True if val == "TRUE" else False

        overrides = {}

        for row in data['feed']['entry']:

            state = row['gsx$state']['$t']
            target_senate = is_true(row['gsx$targetsenate']['$t'])
            target_house = is_true(row['gsx$targethouse']['$t'])
            target_house_first = is_true(row['gsx$targethousefirst']['$t'])
            individual = row['gsx$optionaltargetindividualfirstlastname']['$t']
            first_call_name = row['gsx$optionalextrafirstcallname']['$t']
            first_call_number = row['gsx$optionalextrafirstcallnumber']['$t']

            overrides[state] = {
                'target_senate': target_senate,
                'target_house': target_house,
                'target_house_first': target_house_first,
                'target_individual': individual,
                'first_call_name': first_call_name,
                'first_call_number': first_call_number
            }

        return overrides

