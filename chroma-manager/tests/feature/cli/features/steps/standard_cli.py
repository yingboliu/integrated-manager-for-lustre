#
# ========================================================
# Copyright (c) 2012 Whamcloud, Inc.  All rights reserved.
# ========================================================


from behave import *
from nose.tools import *

from chroma_cli.main import standard_cli


@when('I run chroma {args}')
def step(context, args):
    from StringIO import StringIO
    import sys
    context.stdout = StringIO()
    context.stderr = StringIO()
    try:
        sys.stdout = context.stdout
        sys.stderr = context.stderr
        if 'cli_config' in context and context.cli_config:
            standard_cli(args=args.split(), config=context.cli_config)
        else:
            standard_cli(args.split())
    except SystemExit, e:
        context.stdout.seek(0)
        context.stderr.seek(0)
        forced = any([a in ['--force', '-f'] for a in args.split()])
        if e.code != 0 and not context.cli_failure_expected:
            fail("code: %d stdout: %s stderr: %s" %
                 (e.code, context.stdout.readlines(), context.stderr.readlines()))
        elif e.code == 0 and context.cli_failure_expected and not forced:
            fail("Failure expected but didn't happen!\nstdout: %s, stderr: %s" %
                 (context.stdout.readlines(), context.stderr.readlines()))
    except Exception, e:
        context.stdout.seek(0)
        context.stderr.seek(0)
        from traceback import format_exc
        fail("%s\nstdout:\n%s\nstderr:\n%s" %
             (format_exc(),
              "".join(context.stdout.readlines()),
              "".join(context.stderr.readlines())))

    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__


@then('I should see output containing "{message}"')
def step(context, message):
    context.stdout.seek(0)
    try:
        ok_(message in "".join(context.stdout.readlines()))
    except AssertionError:
        context.stdout.seek(0)
        print context.stdout.readlines()
        raise


@then('I should not see output containing "{message}"')
def step(context, message):
    context.stdout.seek(0)
    try:
        ok_(message not in "".join(context.stdout.readlines()))
    except AssertionError:
        context.stdout.seek(0)
        print context.stdout.readlines()
        raise
