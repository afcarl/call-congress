import os

from distutils.util import strtobool

import twilio.rest


class Config(object):
    DEBUG = True

    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL','sqlite:///dev.db')

    APPLICATION_ROOT = 'http://1cf55a5a.ngrok.io'

    TW_CLIENT = twilio.rest.TwilioRestClient(
        os.environ.get('TWILIO_DEV_ACCOUNT_SID'),
        os.environ.get('TWILIO_DEV_AUTH_TOKEN'))

    TASKFORCE_KEY = os.environ.get('TASKFORCE_KEY')
    SUNLIGHTLABS_KEY = os.environ.get('SUNLIGHTLABS_KEY')

    REDIS_URL = os.environ.get('REDISTOGO_URL') or None # JL NOTE ~ optional

    # limit on the length of the call
    TW_TIME_LIMIT = 60 * 20  # 4 minutes

    # limit on the amount of time to ring before giving up
    TW_TIMEOUT = 40  # seconds

    # number of threads to limit asynchronous leaderboard requests
    FFTF_LB_ASYNC_POOL_SIZE = 8

    # only used for FFTF extra call data logging
    FFTF_CALL_LOG_API_KEY = os.environ.get('FFTF_CALL_LOG_API_KEY')

    SECRET_KEY = 'AOUSBDAONPSOMDASIDUBSDOUABER)*#(R&(&@@#))'


class ConfigProduction(Config):
    DEBUG = strtobool(os.environ.get('DEBUG', 'false'))

    SENTRY_DSN = os.environ.get('SENTRY_DSN')

    SQLALCHEMY_POOL_RECYCLE = 60 * 60  # 1 hour
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URI')

    APPLICATION_ROOT = os.environ.get('APPLICATION_ROOT')

    TW_CLIENT = twilio.rest.TwilioRestClient(
        os.environ.get('TWILIO_ACCOUNT_SID'),
        os.environ.get('TWILIO_AUTH_TOKEN'))

    SECRET_KEY = os.environ.get('SECRET_KEY')


class ConfigTesting(Config):
    TESTING = True
    APPLICATION_ROOT = ''
    SQLALCHEMY_DATABASE_URI = 'sqlite:///test.db'
