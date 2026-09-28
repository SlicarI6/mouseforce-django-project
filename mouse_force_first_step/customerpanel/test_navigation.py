from .section_test_support import stub_unlocked_navigation
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse

from .navigation import customer_session_token
from .views import customer_offers, customer_session


class CustomerShellTests(SimpleTestCase):
    def setUp(self):
        stub_unlocked_navigation(self)
        self.request = RequestFactory().get(reverse('customer_offers'))
        self.request.user = get_user_model()(pk=1, username='customer', role='customer', is_active=True)
        self.request.session = SessionStore(session_key='private-session-cookie')

    def test_named_regions_and_fixed_source_scripts(self):
        response = customer_offers(self.request)
        self.assertContains(response, 'id="customer-page-main"')
        self.assertContains(response, 'id="customer-page-top"')
        self.assertContains(response, 'data-page="offers"')
        self.assertContains(response, '/static/js/customer_navigation.js')
        self.assertContains(response, 'data-customer-page-style')
        self.assertNotContains(response, 'private-session-cookie')
        self.assertIn('no-store', response['Cache-Control'])

    def test_session_identity_is_stable_but_changes_on_session_or_customer_change(self):
        token = customer_session_token(self.request)
        self.assertEqual(token, customer_session_token(self.request))
        self.request.session = SessionStore(session_key='another-private-session')
        self.assertNotEqual(token, customer_session_token(self.request))
        self.request.user.pk = 2
        self.assertNotEqual(token, customer_session_token(self.request))

    def test_session_check_returns_only_noncredential_identity(self):
        response = customer_session(self.request)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'private-session-cookie')
        self.assertNotContains(response, 'customer')
        self.assertIn('no-store', response['Cache-Control'])

    def test_session_check_rejects_anonymous_inactive_and_non_customer(self):
        self.request.user = AnonymousUser()
        self.assertEqual(customer_session(self.request).status_code, 401)
        for role, active in [('customer', False), ('user', True)]:
            self.request.user = get_user_model()(role=role, is_active=active)
            self.assertEqual(customer_session(self.request).status_code, 403)

    def test_navigation_request_rejects_non_customer_without_rendering_content(self):
        self.request.META['HTTP_X_CUSTOMER_NAVIGATION'] = '1'
        self.request.user.role = 'user'
        response = customer_offers(self.request)
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, 'customer-page-main', status_code=403)

    def test_session_endpoint_does_not_accept_post(self):
        self.request.method = 'POST'
        self.assertEqual(customer_session(self.request).status_code, 405)
