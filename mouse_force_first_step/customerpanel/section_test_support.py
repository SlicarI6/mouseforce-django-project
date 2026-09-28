"""Pre-existing paid access fixtures; never imported by application code."""
from unittest.mock import patch

from .models import CustomerSectionUnlock


def seed_paid_access(*users, sections=('rewards',)):
    for user in users:
        for section in sections:
            CustomerSectionUnlock.objects.get_or_create(user=user, section=section,
                defaults={'points_spent': 10, 'balance_after': 0, 'source': 'points'})


def stub_unlocked_navigation(test):
    # Existing provider/layout unit tests remain DB-free. Real authorization is
    # covered separately by the new database-backed section access tests.
    test.enterContext(patch('mouse_force_first_step.customerpanel.navigation.get_section_state',
        return_value={'unlocked': list(CustomerSectionUnlock.Section.values), 'cost': 10,
                      'points': {'total_points': 0}}))
