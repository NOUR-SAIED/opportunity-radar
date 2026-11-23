import unittest

from src.auth import AuthError, LocalDemoAuth, validate_credentials
from src.profile import normalize_profile, validate_profile


class AuthTests(unittest.TestCase):
    def test_demo_sign_in(self):
        user = LocalDemoAuth().sign_in("demo@example.com", "demo-password")
        self.assertEqual(user.email, "demo@example.com")

    def test_invalid_demo_credentials_are_rejected(self):
        with self.assertRaises(AuthError):
            LocalDemoAuth().sign_in("demo@example.com", "wrong")

    def test_password_validation(self):
        validate_credentials("person@example.com", "Strongpass1")
        with self.assertRaises(AuthError):
            validate_credentials("person@example.com", "12345678")


class ProfileTests(unittest.TestCase):
    def test_profile_normalization_deduplicates_skills(self):
        profile = normalize_profile({
            "name": "  Nour  ",
            "headline": " Data Engineer ",
            "target_roles": ["Data Engineer", "data engineer", "BI Engineer"],
            "skills": ["Python", " python ", "SQL"],
            "languages": ["English", "English"],
            "experience": ["  Built a warehouse.  "],
        })
        self.assertEqual(profile["name"], "Nour")
        self.assertEqual(profile["target_roles"], ["data engineer", "bi engineer"])
        self.assertEqual(profile["skills"], ["python", "sql"])
        self.assertEqual(profile["languages"], ["English"])

    def test_incomplete_profile_is_rejected(self):
        errors = validate_profile({"name": "", "headline": "", "skills": [], "target_roles": []})
        self.assertIn("Add your name.", errors)
        self.assertIn("Add at least one skill.", errors)

    def test_experience_objects_are_normalized_to_text(self):
        profile = normalize_profile({
            "name": "Nour",
            "headline": "Data Engineer",
            "skills": ["Python"],
            "target_roles": ["Data Engineer"],
            "experience": [{
                "company": "Example",
                "role": "Data Intern",
                "description": "Built pipelines",
            }],
        })
        self.assertEqual(profile["experience"], ["Example Data Intern Built pipelines"])


if __name__ == "__main__":
    unittest.main()
