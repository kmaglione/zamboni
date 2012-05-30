import json
import os
import shutil
import urlparse

from django.conf import settings
from django.core.cache import cache
from django.utils.encoding import iri_to_uri
from django.utils.http import http_date, urlencode

from mock import Mock, patch
from nose.tools import eq_
from pyquery import PyQuery as pq
import waffle
from waffle.models import Switch

import amo
import amo.tests
from amo.utils import Message
from amo.urlresolvers import reverse
from addons.models import Addon
from files.models import File
from users.models import UserProfile

# Stolen from files tests.
dictionary = 'apps/files/fixtures/files/dictionary-test.xpi'

class TestVersions(amo.tests.TestCase):
    fixtures = ['base/addon_3615', 'base/users']

    def login_as_editor(self):
        assert self.client.login(username='editor@mozilla.com',
                                 password='password')

    def setUp(self):
        self.addon = Addon.objects.get(pk=3615)
        self.dev = self.addon.authors.all()[0]
        self.regular = UserProfile.objects.get(pk=999)
        self.version = self.addon.versions.latest()
        self.file = self.version.all_files[0]

        self.file_two = File()
        self.file_two.version = self.version
        self.file_two.filename = 'dictionary-test.xpi'
        self.file_two.save()

        self.login_as_editor()

        for file_obj in [self.file, self.file_two]:
            src = os.path.join(settings.ROOT, dictionary)
            try:
                os.makedirs(os.path.dirname(file_obj.file_path))
            except OSError:
                pass
            shutil.copyfile(src, file_obj.file_path)

    def tearDown(self):
        pass

    def test_files_disabled_anon_access(self):
        self.client.logout()
        self.file.update(status=amo.STATUS_DISABLED)
        eq_(self.client.head(self.file.get_url_path('test')).status_code,
            404)

    def test_files_disabled_editor_access(self):
        self.file.update(status=amo.STATUS_DISABLED)
        eq_(self.client.head(self.file.get_url_path('test')).status_code,
            200)

