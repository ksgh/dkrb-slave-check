
from datetime import datetime
import redis
import threading
import sys
import os
import logging
import time
import json
import pymysql
import base64
import boto3
import pprint

pp = pprint.PrettyPrinter(indent=4)

# only logging to stdout at this time.
# logpath     = '/var/log'
logname = 'async-slave-check'
logger = logging.getLogger(logname)

if not len(logger.handlers):
    # logfile     = '/'.join([logpath, "%s.log" % logname])
    format = "%(levelname)s: %(asctime)s: %(message)s"
    dateformat = "%Y/%m/%d %I:%M:%S %p"
    logger.setLevel(logging.DEBUG)
    # fh          = logging.FileHandler(logfile)
    sh = logging.StreamHandler()
    formatter = logging.Formatter(format, dateformat)

    # fh.setLevel(logging.DEBUG)
    sh.setLevel(logging.INFO)
    # fh.setFormatter(formatter)
    sh.setFormatter(formatter)
    # logger.addHandler(fh)
    logger.addHandler(sh)

# Customizables
MATCH_MODE = os.environ.get('SC_MATCH_MODE', None)
DOCKER_NODES = os.environ.get('SC_DOCKER_NODES', None)
HOST_MATCH_PREFIX = os.environ.get('SC_AWS_HOST_MATCH_PREFIX', None)
AWS_REGION = os.environ.get('SC_AWS_REGION', None)
AWS_ACCESS_KEY = os.environ.get('SC_AWS_ACCESS_KEY', None)
AWS_SECRET_KEY = os.environ.get('SC_AWS_SECRET_KEY', None)
ITERATIONS = int(os.environ.get('SC_ITERATIONS', 10))
CYCLE_SLEEP = int(os.environ.get('SC_CYCLE_SLEEP', 2))
SLAVE_LAG_THRESHOLD = int(os.environ.get('SC_SLAVE_LAG_THRESHOLD', 5))  # currently responsible for 0 decisions
SLAVE_MAX_GREEN = int(os.environ.get('SC_SLAVE_MAX_GREEN', 3))
SLAVE_MIN_RED = int(os.environ.get('SC_SLAVE_MIN_RED', 7))
REDIS_HOST = os.environ.get('SC_REDIS_HOST', None)
REDIS_KEY_EXPIRE = int(os.environ.get('SC_REDIS_KEY_EXPIRE', 0))
IGNORE_HOST_PREF = os.environ.get('SC_AWS_IGNORE_HOST_PREF', None)
EXTRA_TAG_KEY = os.environ.get('SC_AWS_TAG_KEY', None)
EXTRA_TAG_VAL = os.environ.get('SC_AWS_TAG_VAL', None)

# Required
SLAVE_USER = os.environ.get('SC_SLAVE_USERNAME', None)
SLAVE_PASS = os.environ.get('SC_SLAVE_PASSWORD', None)
SLAVE_PORT = int(os.environ.get('SC_SLAVE_PORT', 3306))

# attempt to default based on other supplied (or default) values
_lower_bound = SLAVE_MAX_GREEN + 1
_upper_bound = SLAVE_MIN_RED - _lower_bound

try:
    _rng = os.environ.get('SC_SLAVE_RANGE_YELLOW', '{l},{u}'.format(l=_lower_bound, u=_upper_bound)).split(',')
    SLAVE_RANGE_YELLOW = range(int(_rng[0]), int(_rng[1]))
except Exception as e:
    logger.warning('Unable to properly parse SC_SLAVE_RANGE_YELLOW. This should be the range supplied as "start,length"')
    logger.warning('defaulting back to range(%s, %s)', _lower_bound, _upper_bound)
    logger.warning(e.message)

    SLAVE_RANGE_YELLOW = range(_lower_bound, _upper_bound)


class SlaveCheck(threading.Thread):
    def __init__(self, host, ip, thread_lock):
        super(SlaveCheck, self).__init__()
        self.hostname = host
        self.ip = ip
        self.thread_lock = thread_lock
        self.db_conn = None
        self.cursor = None
        self.slave_status = {}
        self.blocking = True
        self.is_slave = True

        self.__db_connect()

    def __db_connect(self):

        _connect_args = {
            'charset': 'utf8',
            'cursorclass': pymysql.cursors.DictCursor,
            'host': self.ip,
            'port': SLAVE_PORT,
            'user': SLAVE_USER,
            'passwd': SLAVE_PASS
        }

        try:
            self.db_conn = pymysql.connect(**_connect_args)
        except pymysql.Error as e:
            logger.warning('Failed to connect to %s', self.hostname)
            logger.warning('MySQL Said: %s: %s', e.args[0], e.args[1])

    def __query(self, sql, args=[]):
        if not self.cursor:
            self.cursor = self.db_conn.cursor()

        if args:
            self.cursor.execute(sql, args)
        else:
            self.cursor.execute(sql)

        return self.cursor

    def get_slave_status(self):
        sql = "SHOW SLAVE STATUS"
        cur = self.__query(sql)
        self.slave_status = cur.fetchone()

        # best effort "are you a slave" check for now
        if not self.slave_status:
            self.is_slave = False

        if cur:
            cur.close()

    def run(self):
        self.get_slave_status()

        if self.db_conn:
            self.db_conn.close()


def record_status(key, data, smush=False):
    r_hosts = REDIS_HOST.split(',')
    hosts = []
    if len(r_hosts) > 1:
        for h in r_hosts:
            hosts.append((h, '26379'))
        # @TODO redis master name is hard-coded
        master_name = 'redismaster'
        se = redis.Sentinel(hosts, socket_timeout=.3)
        master = se.master_for(master_name)
    else:
        master = redis.Redis(host=REDIS_HOST, port=6379)

    if smush:
        _str = base64.b64encode(json.dumps(data))
    else:
        _str = data

    # the docs don't say what the return type is, and I followed this
    # down to `command` and gave up.

    try:
        if REDIS_KEY_EXPIRE:
            master.set(key, _str, REDIS_KEY_EXPIRE)
        else:
            master.set(key, _str)
    except Exception as e:
        logger.error('Unable to write to redis')
        logger.error(e)


def check_slaves(instances):
    sw_threads = []

    # threading just to check them all simultaneously as opposed to in succession
    for data in instances:
        sw = SlaveCheck(data['hostname'], data['ip_address'], threading.Lock())
        if sw.db_conn:
            sw.start()
            sw_threads.append(sw)

    slave_status = []

    for s in sw_threads:
        s.join()
        # discard anything that doesn't appear to be a slave
        if s.is_slave:
            _d = {
                'host': s.hostname,
                'seconds': s.slave_status['Seconds_Behind_Master'],
                'datetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            slave_status.append(_d)

    return slave_status

# @TODO next two method could probably be combined
def __get_instances(filters):
    '''
    :param filters: list of dictionaries. See boto3 docs.. it's all explained there.
    :return: Single instance object
    '''
    all_instances = []
    for region in AWS_REGION.split(','):
        # connect to this region...
        client = boto3.client('ec2', region, aws_access_key_id=AWS_ACCESS_KEY, aws_secret_access_key=AWS_SECRET_KEY)
        # attempt to locate any instances tha match our criteria
        reservations = client.describe_instances(Filters=filters)
        instances = None

        try:
            instances = [i for re in reservations['Reservations'] for i in re['Instances']]
        except:
            logger.warning('Unable to get instances')

        all_instances += instances

    return all_instances


def get_db_instances(filters):
    instances = __get_instances(filters)

    db_hosts = []

    # whittle this down a bit...
    for i in instances:
        tag_name = 'unknown'
        if i.get('Tags', None):
            for tag in i['Tags']:
                if tag['Key'] == 'Name':
                    tag_name = tag['Value']

        add_host = True
        if IGNORE_HOST_PREF:
            for host_prefix in IGNORE_HOST_PREF.split(','):
                if tag_name.startswith(host_prefix):
                    add_host = False

        if add_host:
            _d = {
                'hostname': tag_name,
                'ip_address': i['PrivateIpAddress'],
            }

            db_hosts.append(_d)

    return db_hosts


def preflight_checks():
    _ok = True

    if MATCH_MODE.lower() not in ('docker', 'aws'):
        logger.error('Available modes are: docker, aws')
        sys.exit(1)

    if MATCH_MODE.lower() == 'docker':
        if not DOCKER_NODES:
            logger.error('SC_MATCH_MODE is docker, but no nodes to match on were specified')
            _ok = False
    else:
        if not HOST_MATCH_PREFIX:
            logger.error('SC_MATCH_MODE is aws, but no hosts to match on was supplied')
            _ok = False

        if IGNORE_HOST_PREF:
            logger.warning('The following prefixes will be ignored: %s', ', '.join(IGNORE_HOST_PREF.split(',')))

        if not AWS_REGION:
            logger.error('SC_AWS_REGION must be defined in the environment')
            _ok = False

    if not SLAVE_PASS:
        logger.error('SC_SLAVE_PASSWORD must be set in the environment')
        _ok = False

    if not SLAVE_USER:
        logger.error('SC_SLAVE_USERNAME must be set in the environment')
        _ok = False

    #if not os.path.isfile('/etc/boto.cfg'):
    #    logger.warning('/etc/boto.cfg not found - this is where we\'re expecting to obtain auth info from')
    #    logger.warning('Letting this slide, but this could cause misleading timeouts')

    #if not os.environ.get('http_proxy', None) or not os.environ.get('https_proxy', None):
    #    logger.warning('"http_proxy" or "https_proxy" is not set in the environment')
    #    logger.warning('This could cause boto to hang while trying to connect')

    if not _ok:
        sys.exit(1)


def locate_aws_instances():
    match_prefixes = [p + '*' for p in HOST_MATCH_PREFIX.split(',')]
    extra_tag_match = {}

    name_match = {
        'Name': 'tag:Name',
        'Values': match_prefixes
    }

    state_match = {
        'Name': 'instance-state-name',
        'Values': ['running']
    }

    if EXTRA_TAG_KEY and EXTRA_TAG_VAL:
        extra_tag_match = {
            'Name': 'tag:{key}'.format(key=EXTRA_TAG_KEY),
            'Values': [EXTRA_TAG_VAL]
        }

    filters = [
        name_match,
        state_match
    ]

    if extra_tag_match:
        filters.append(extra_tag_match)

    db_data = get_db_instances(filters)

    if not db_data:
        logger.error('No instances found!')
        sys.exit(1)

    return db_data


if __name__ == '__main__':
    preflight_checks()
    db_hosts = []
    if MATCH_MODE == 'aws':
        db_hosts = locate_aws_instances()

    if MATCH_MODE == 'docker':
        _hosts = DOCKER_NODES.split(',')
        # mimicking the same structure used for the aws method. In AWS land a hostname may not be resolvable,
        # therefore we also grab the private IP address. In docker land we should typically be able to resolve
        # services by "service name"
        db_hosts = []
        for n in _hosts:
            db_hosts.append({'hostname': n, 'ip_address': n})

    logger.info('Starting round of (%s) checks...', ITERATIONS)
    counter = 1

    while counter < ITERATIONS:
        logger.debug('Iteration: %s', counter)

        hosts = check_slaves(db_hosts)

        # mmmm oddly, nothing doing any sort of replication ?
        if not hosts:
            logger.warning('Nothing could be considered a slave - after successfully locating instances.')
            logger.warning('Exiting with non-zero status to indicate potential unexpected behavior.')
            sys.exit(1)

        # Ok so what we're gonna do here is average out the slave lag and test that against our
        # "red", "yellow", and "green" flags to set an overall health for slave lag

        seconds_total = 0
        seconds_avg = 0
        num_slaves = len(hosts)
        val = None

        for h in hosts:
            try:
                seconds_total += int(h['seconds'])
            except TypeError:
                pass

            if h['seconds'] > SLAVE_LAG_THRESHOLD:
                # again, currently has no bearing on anything
                logger.warning('Host: %s is %s seconds behind', h['host'], h['seconds'])

        if seconds_total == 0:
            seconds_avg = 0
        else:
            seconds_avg = round(seconds_total / num_slaves)

        if seconds_avg <= SLAVE_MAX_GREEN:
            val = 'green'
        elif seconds_avg in SLAVE_RANGE_YELLOW:
            val = 'yellow'
        elif seconds_avg >= SLAVE_MIN_RED:
            ## yeah this is really just "everything else", but whatevs...
            val = 'red'

        status = {
            'status': val,
            'avg_seconds': seconds_avg,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'hosts': hosts
        }

        record_status('slave_status', status, True)

        counter += 1
        time.sleep(CYCLE_SLEEP)

    logger.info('Done with this round of checks')
    sys.exit(0)
