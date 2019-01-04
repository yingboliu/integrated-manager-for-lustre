# Copyright (c) 2018 DDN. All rights reserved.
# Use of this source code is governed by a MIT-style
# license that can be found in the LICENSE file.


import settings

from tastypie.http import HttpUnauthorized

from tastypie.authentication import Authentication, ApiKeyAuthentication
from tastypie.authorization import Authorization
from django.utils.crypto import constant_time_compare


def has_api_key(request):
    result = ApiKeyAuthentication().is_authenticated(request)

    return not isinstance(result, HttpUnauthorized)


class CsrfAuthentication(Authentication):
    """Tastypie authentication class for rejecting POSTs
    which do not contain a valid CSRF token.

    Note that unlike many REST APIs, Chroma requires CSRF protection
    because the API is used directly from web browsers.

    Some APIs just check that the call is AJAX and trust XHR protection.
    This was okay until http://gursevkalra.blogspot.com/2011/12/json-csrf-with-parameter-padding.html

    Some APIs assume a request isn't CSRF if its JSON encoded (browsers
    don't let you set enctype to application/json for <form>).  That would
    be true apart from http://gursevkalra.blogspot.com/2011/12/json-csrf-with-parameter-padding.html

    In principle you can argue that any CSRF attacks on JSON APIs are
    the browser's (read: end user's) fault, not ours.  In the wild, that
    is a very unhelpful attitude.
    """

    def is_authenticated(self, request, object=None):
        # Return early if there is a valid API key
        if has_api_key(request):
            return True

        if request.method != "POST":
            return True

        request_csrf_token = request.META.get("HTTP_X_CSRFTOKEN", "")
        csrf_token = request.META["CSRF_COOKIE"]

        if not constant_time_compare(csrf_token, request_csrf_token):
            return False
        else:
            return True


class AnonymousAuthentication(CsrfAuthentication):
    """Tastypie authentication class which only allows in
    logged-in users unless settings.ALLOW_ANONYMOUS_READ is true"""

    def is_authenticated(self, request, object=None):
        # Return early if there is a valid API key
        if has_api_key(request):
            return True

        # If any authentication in the class hierarchy refuses, we refuse
        if not super(AnonymousAuthentication, self).is_authenticated(request, object):
            return False

        return settings.ALLOW_ANONYMOUS_READ or request.user.is_authenticated()

    def get_identifier(self, request):
        if request.user.is_authenticated():
            return request.user.username
        else:
            return "Anonymous user"


class PermissionAuthorization(Authorization):
    """Tastypie authentication class which checks for
    the presence of a single permission for access to
    a resource"""

    def __init__(self, perm_name):
        self.perm_name = perm_name

    def is_authorized(self, request, object=None):
        return request.user.has_perm(self.perm_name)
