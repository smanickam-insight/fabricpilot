from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from services.access import AccessDenied
from services.identity import EntraValidator, application_credential


class IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.tenant = "11111111-1111-1111-1111-111111111111"

    def setUp(self):
        self.validator = EntraValidator("pilot-api", [self.tenant])
        self.validator.clients[self.tenant] = Mock(get_signing_key_from_jwt=Mock(return_value=SimpleNamespace(key=self.key.public_key())))
        now = datetime.now(timezone.utc)
        self.claims = {"tid": self.tenant, "oid": "22222222-2222-2222-2222-222222222222",
                       "aud": "pilot-api", "iss": f"https://login.microsoftonline.com/{self.tenant}/v2.0",
                       "iat": now, "nbf": now, "exp": now + timedelta(minutes=5), "scp": "access_as_user"}

    def test_valid_signed_token(self):
        token = jwt.encode(self.claims, self.key, algorithm="RS256")
        self.assertEqual(self.validator.validate(token)["tid"], self.tenant)

    def test_wrong_audience_issuer_scope_and_expiration(self):
        for change in [{"aud": "another-api"}, {"iss": "https://attacker.invalid"},
                       {"scp": "read"}, {"exp": datetime.now(timezone.utc) - timedelta(minutes=1)}]:
            with self.subTest(change=change), self.assertRaises(AccessDenied):
                self.validator.validate(jwt.encode(self.claims | change, self.key, algorithm="RS256"))

    def test_unonboarded_tenant_and_unsigned_token(self):
        with self.assertRaises(AccessDenied):
            self.validator.validate(jwt.encode(self.claims | {"tid": "33333333-3333-3333-3333-333333333333"}, self.key, algorithm="RS256"))
        with self.assertRaises(AccessDenied):
            self.validator.validate(jwt.encode(self.claims, "", algorithm="none"))

    @patch("services.identity.ClientSecretCredential")
    def test_application_mode_does_not_accept_foreign_tenant(self, factory):
        with self.assertRaises(AccessDenied):
            application_credential("foreign", "client", "secret", [self.tenant])
        factory.assert_not_called()