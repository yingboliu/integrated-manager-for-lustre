import inspect
import logging
import os
import subprocess
import time
import socket

from django.utils.unittest import TestCase
from testconfig import config

from tests.integration.core.constants import TEST_TIMEOUT

logger = logging.getLogger('test')
logger.setLevel(logging.DEBUG)
handler = logging.FileHandler(os.path.join(config.get('log_dir', '/var/log/'), 'chroma_test.log'))
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


class UtilityTestCase(TestCase):
    """ Adds a few non-api specific utility functions for the integration tests. """

    # Allows us to not fetch help repeatedly for the same error buy keeping track of
    # those things we have fetched help for.
    help_fetched_list = []

    def setUp(self):
        self.maxDiff = None                  # By default show the complete diff on errors.

    def wait_until_true(self, lambda_expression, error_message='', timeout=TEST_TIMEOUT):
        """Evaluates lambda_expression once/1s until True or hits timeout.

        :param lambda_expression: the expression to repeatedly evaluate
        :param error_message: optional string to print or expression to call on failure (useful for debug)
        :param timeout: the maximum number of seconds to wait for the expression to evaluate as true. If
                        it does not return in this amount of time, then an error will be raised.
        :return: None

        Typical usage.
        self.wait_until_true(lambda: len(self.get_list('/api/target/)) == expected_len)

        self.wait_until_true(lambda: len(self.get_list('/api/target/)) == expected_len,
                             "Timed out waiting for there to be %s targets." %s expected_len,
                             timeout = LONG_TEST_TIMEOUT,
                             debug_timeout=30)
        """

        assert hasattr(lambda_expression, '__call__'), 'lambda_expression is not callable: %s' % type(lambda_expression)
        assert hasattr(error_message, '__call__') or type(error_message) is str, 'error_message is not callable and not a str: %s' % type(error_message)
        assert type(timeout) == int, 'timeout is not an int: %s' % type(timeout)

        def evaluate_expression(expression, maxtime):
            running_time = 0
            lambda_result = None
            wait_time = 0.01

            while not lambda_result and running_time < maxtime:
                lambda_result = expression()
                logger.debug("%s evaluated to %s" % (inspect.getsource(expression), lambda_result))

                if not lambda_result:
                    time.sleep(wait_time)
                    wait_time = min(1, wait_time * 10)
                    running_time += wait_time

            return lambda_result, running_time

        # Wait for lambda_expression to return true for up to timeout
        original_lambda_result, original_running_time = evaluate_expression(lambda_expression, timeout)

        if not original_lambda_result:
            # Did not become true in timeout
            if hasattr(error_message, '__call__'):
                # Call the error callback to get debug info at point of failure
                error_message = error_message()

            debug_lambda_result, debug_running_time = evaluate_expression(lambda_expression, timeout)

            if debug_lambda_result:
                logger.debug("lambda eventually passed after %s additional seconds past the timeout." % debug_running_time)

            # This will always assert Raise an error if did not pass before reaching timeout
            raise self.failureException('Timed out after %s seconds waiting for %s\nError Message %s' %
                                        (timeout, inspect.getsource(lambda_expression), error_message))

    def wait_for_items_length(self, fetch_items, length, timeout=TEST_TIMEOUT):
        """
        Assert length of items list generated by func over time or till timeout.
        """
        items = fetch_items()
        while timeout and length != len(items):
            logger.debug("%s evaluated to %s expecting list size of %s items" % (inspect.getsource(fetch_items), items, length))
            time.sleep(1)
            timeout -= 1
            items = fetch_items()
        self.assertNotEqual(0, timeout, "Timed out waiting for %s." % inspect.getsource(fetch_items))

    def wait_for_assert(self, lambda_expression, timeout=TEST_TIMEOUT):
        """
        Evaluates lambda_expression once/1s until no AssertionError or hits
        timeout.
        """
        running_time = 0
        assertion = None
        while running_time < timeout:
            try:
                lambda_expression()
            except AssertionError, e:
                assertion = e
                logger.debug("%s tripped assertion: %s" % (inspect.getsource(lambda_expression), e))
            else:
                break
            time.sleep(1)
            running_time += 1
        self.assertLess(running_time,
                        timeout,
                        "Timed out waiting for %s\nAssertion %s" % (inspect.getsource(lambda_expression), assertion))

    def get_host_config(self, nodename):
        """
        Get the entry for a lustre server from the cluster config.
        """
        for host in config['lustre_servers']:
            if host['nodename'] == nodename:
                return host

    def _fetch_help(self, assert_test, tell_who, message=None, callback=lambda: True, timeout=1800):
        """When an error occurs that we want to hold the cluster for until someone logs in then this function will do that.

        The file /tmp/waiting_help is used as an exit switch along with time. Deleting this file will cause the test to
        continue running - actually raising the exception in fact. This file is also used to put the message in.

        :param assert_test: test that if it occurs will fetch the help
        :param callback: optional but if present returning False will cause the routine to not fetch help.
        :param tell_who: list of email addresses to contact about the issue
        :param message: message to deliver to those people, can be a callable returning a string, or None to use the exception.
        :param timeout: How long to wait before continuing.
        :return: None

        Typical usage.
        self._fetch_help(lambda: self.assertEqual(commandResult, True),
                         ['chris.gearing@intel.com'],
                         'Send the cavalry',
                         callback=lambda: check_if_significant(data))

        self._fetch_help(lambda: self.assertEqual(commandResult, True),
                         ['chris.gearing@intel.com'],
                         lambda: 'Send the cavalry',
                         callback=lambda: check_if_significant(data))

        """

        try:
            return assert_test()
        except Exception as exception:
            if callback() == False or assert_test in self.help_fetched_list:
                raise

            self.help_fetched_list.append(assert_test)

            key_file = '/tmp/waiting_help'

            if message is None:
                message = str(exception)
            elif hasattr(message, '__call__'):
                message = message()

            # First create the file, errors in here do destroy the original, but will be reported by the test framework
            fd = os.open(key_file, os.O_RDWR | os.O_CREAT)
            os.write(fd, "Subject: %s\n\n%s\n\nTest Runner %s" % (message, message, socket.gethostname()))
            os.lseek(fd, 0, os.SEEK_SET)
            subprocess.call(['sendmail'] + tell_who, stdin=fd)
            os.close(fd)

            while timeout > 0 and os.path.isfile(key_file):
                timeout -= 1
                time.sleep(1)

            raise
