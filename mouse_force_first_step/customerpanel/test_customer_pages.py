from .section_test_support import stub_unlocked_navigation
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve, reverse

from .views import customer_discounts, customer_how_points_work, customer_offers


class CustomerInformationPageTests(SimpleTestCase):
    def setUp(self):
        stub_unlocked_navigation(self)
        self.url = reverse('customer_how_points_work')
        self.request = RequestFactory().get(self.url)
        self.request.resolver_match = resolve(self.url)
        self.request.user = get_user_model()(
            username='customer', role='customer', is_active=True,
        )

    def test_route_resolves_to_customer_page(self):
        self.assertEqual(self.url, '/customer/how-points-work/')
        self.assertIs(resolve(self.url).func, customer_how_points_work)

    def test_active_customer_get_renders_sections_without_database_access(self):
        # SimpleTestCase prohibits database queries, including points writes.
        with self.assertTemplateUsed('customer_how_points_work.html'):
            response = customer_how_points_work(self.request)
        self.assertEqual(response.status_code, 200)
        for heading in (
            'How Points Work', 'Daily Points', '7 Day Streak',
            '14 Day Streak', 'What happens if you miss a day?',
        ):
            self.assertContains(response, heading)
        self.assertNotContains(response, 'id="points-controls"')
        self.assertNotContains(response, 'id="points-script"')
        self.assertContains(response, 'aria-current="page"')
        self.assertContains(response, 'Back to dashboard')

    def test_anonymous_user_is_redirected_to_customer_login(self):
        self.request.user = AnonymousUser()
        response = customer_how_points_work(self.request)
        self.assertEqual(response.status_code, 302)
        destination = urlsplit(response.url)
        self.assertEqual(destination.path, reverse('login_account_customer'))
        self.assertEqual(parse_qs(destination.query)['next'], [self.url])

    def test_non_customer_is_forbidden_even_if_staff(self):
        self.request.user.role = 'user'
        for staff in (False, True):
            with self.subTest(staff=staff):
                self.request.user.is_staff = staff
                self.request.user.is_superuser = staff
                with self.assertRaises(PermissionDenied):
                    customer_how_points_work(self.request)

    def test_inactive_customer_is_forbidden(self):
        self.request.user.is_active = False
        with self.assertRaises(PermissionDenied):
            customer_how_points_work(self.request)

    def test_dashboard_navigation_connects_existing_customer_pages(self):
        html = render_to_string(
            'customer_dashboard.html', {'points_state': None}, request=self.request,
        )
        self.assertIn(f'<a href="{self.url}">How Points Work</a>', html)
        self.assertIn(f'<a href="{reverse("customer_discounts")}">Discounts</a>', html)
        self.assertIn(f'<a href="{reverse("customer_offers")}">Offers</a>', html)
        self.assertIn(f'<a href="{reverse("customer_news")}">News</a>', html)
        self.assertIn(f'<a href="{reverse("customer_rewards")}">Rewards</a>', html)


class CustomerDiscountsPageTests(SimpleTestCase):
    def setUp(self):
        stub_unlocked_navigation(self)
        self.url = reverse('customer_discounts')
        self.request = RequestFactory().get(self.url)
        self.request.resolver_match = resolve(self.url)
        self.request.user = get_user_model()(
            username='customer', role='customer', is_active=True,
        )

    def test_route_resolves_to_discounts_page(self):
        self.assertEqual(self.url, '/customer/discounts/')
        self.assertIs(resolve(self.url).func, customer_discounts)

    def test_active_customer_renders_illustration_without_database_access(self):
        # Catalogue queries are covered by the PostgreSQL Discounts tests.
        with patch('mouse_force_first_step.customerpanel.discounts.catalogue', return_value={}), self.assertTemplateUsed('customer_discounts.html'):
            response = customer_discounts(self.request)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<h1>Discounts</h1>', html=True)
        self.assertContains(response, 'Two overlapping MouseForce gift cards')
        self.assertContains(response, f'<a href="{self.url}" aria-current="page">Discounts</a>', html=True)
        self.assertNotContains(response, 'id="points-controls"')
        self.assertNotContains(response, 'id="points-script"')

    def test_anonymous_user_is_redirected_to_customer_login(self):
        self.request.user = AnonymousUser()
        response = customer_discounts(self.request)
        self.assertEqual(response.status_code, 302)
        destination = urlsplit(response.url)
        self.assertEqual(destination.path, reverse('login_account_customer'))
        self.assertEqual(parse_qs(destination.query)['next'], [self.url])

    def test_inactive_customer_and_non_customers_are_forbidden(self):
        for role, active, staff in (
            ('customer', False, False), ('user', True, False), ('user', True, True),
        ):
            with self.subTest(role=role, active=active, staff=staff):
                self.request.user.role = role
                self.request.user.is_active = active
                self.request.user.is_staff = staff
                self.request.user.is_superuser = staff
                with self.assertRaises(PermissionDenied):
                    customer_discounts(self.request)


class CustomerOffersPageTests(SimpleTestCase):
    def setUp(self):
        stub_unlocked_navigation(self)
        self.url = reverse('customer_offers')
        self.request = RequestFactory().get(self.url)
        self.request.resolver_match = resolve(self.url)
        self.request.user = get_user_model()(
            username='customer', role='customer', is_active=True,
        )

    def test_route_resolves_to_offers_page(self):
        self.assertEqual(self.url, '/customer/offers/')
        self.assertIs(resolve(self.url).func, customer_offers)

    def test_active_customer_renders_content_without_database_access(self):
        # SimpleTestCase prohibits database queries, including reward writes.
        with self.assertTemplateUsed('customer_offers.html'), self.assertTemplateUsed('customer_base.html'):
            response = customer_offers(self.request)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<h1 id="customer-offers-title">Offers</h1>', html=True)
        self.assertContains(
            response,
            'Discover limited-time deals, partner offers and special opportunities '
            'available through MouseForce. Some offers may be available to everyone, '
            'while others may be unlocked through your activity, points or account history.',
        )
        self.assertContains(
            response,
            '<p class="customer-offers-empty">No offers at the moment...</p>',
            html=True,
        )
        self.assertContains(
            response, f'<a href="{self.url}" aria-current="page">Offers</a>',
            count=1, html=True,
        )
        self.assertContains(response, 'Back to dashboard')
        self.assertNotContains(response, 'id="points-controls"')
        self.assertNotContains(response, 'id="points-script"')

    def test_anonymous_user_is_redirected_to_customer_login(self):
        self.request.user = AnonymousUser()
        response = customer_offers(self.request)
        self.assertEqual(response.status_code, 302)
        destination = urlsplit(response.url)
        self.assertEqual(destination.path, reverse('login_account_customer'))
        self.assertEqual(parse_qs(destination.query)['next'], [self.url])

    def test_non_customer_is_forbidden_even_if_staff(self):
        self.request.user.role = 'user'
        for staff in (False, True):
            with self.subTest(staff=staff):
                self.request.user.is_staff = staff
                self.request.user.is_superuser = staff
                with self.assertRaises(PermissionDenied):
                    customer_offers(self.request)

    def test_inactive_customer_is_forbidden(self):
        self.request.user.is_active = False
        with self.assertRaises(PermissionDenied):
            customer_offers(self.request)

    def test_existing_information_pages_link_to_offers(self):
        for view in (customer_discounts, customer_how_points_work):
            with self.subTest(view=view.__name__), patch('mouse_force_first_step.customerpanel.discounts.catalogue', return_value={}):
                response = view(self.request)
                self.assertContains(response, f'<a href="{self.url}">Offers</a>', html=True)
