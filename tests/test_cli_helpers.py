"""Unit tests for helper functions in scripts/openproject_cli.py."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def load_cli_module():
    root = Path(__file__).resolve().parents[1]
    module_path = root / "scripts" / "openproject_cli.py"
    spec = importlib.util.spec_from_file_location("openproject_cli", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load scripts/openproject_cli.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load_cli_module()


class HelperTests(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(cli.slugify("Use OpenProject as PM source of truth"), "use-openproject-as-pm-source-of-truth")
        self.assertEqual(cli.slugify("***"), "decision")

    def test_status_bucket(self) -> None:
        self.assertEqual(cli.status_bucket("Closed"), "completed")
        self.assertEqual(cli.status_bucket("In progress"), "in_progress")
        self.assertEqual(cli.status_bucket("On hold / blocker"), "blockers")

    def test_build_decision_markdown(self) -> None:
        content = cli.build_decision_markdown(
            date_text="2026-02-23",
            project="know-malawi",
            title="Adopt CLI",
            decision="Proceed with alpha",
            context="Current flow is manual",
            impact="Faster updates",
            followup="Review after sprint",
        )
        self.assertIn("# Decision: Adopt CLI", content)
        self.assertIn("Project: know-malawi", content)
        self.assertIn("## Follow-up", content)

    def test_write_text_file_and_unique_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "decision.md"
            cli.write_text_file(base, "first")
            next_path = cli.unique_path(base)
            self.assertNotEqual(base, next_path)
            self.assertEqual(next_path.name, "decision-2.md")

    def test_to_api_path(self) -> None:
        self.assertEqual(cli.to_api_path("/api/v3/work_packages/1"), "/work_packages/1")
        self.assertEqual(cli.to_api_path("https://example.org/api/v3/projects/8"), "/projects/8")
        self.assertEqual(cli.to_api_path("work_packages/1"), "/work_packages/1")

    def test_ensure_iso_date(self) -> None:
        self.assertEqual(cli.ensure_iso_date("2026-02-24", "--start-date"), "2026-02-24")
        with self.assertRaises(cli.OpenProjectError):
            cli.ensure_iso_date("24.02.2026", "--start-date")

    def test_extract_numeric_id_from_href(self) -> None:
        self.assertEqual(
            cli.extract_numeric_id_from_href("/api/v3/projects/42", "projects"),
            42,
        )
        self.assertIsNone(
            cli.extract_numeric_id_from_href("/api/v3/projects/demo-project", "projects")
        )

    def test_user_helpers(self) -> None:
        users = [
            {"id": 1, "name": "Alice Admin", "login": "alice"},
            {"id": 2, "firstName": "Bob", "lastName": "Builder", "login": "bob"},
        ]
        self.assertEqual(cli.user_display_name(users[0]), "Alice Admin")
        self.assertEqual(cli.user_display_name(users[1]), "Bob Builder")
        self.assertEqual(len(cli.filter_users(users, "bob")), 1)
        self.assertEqual(cli.filter_users(users, "3"), [])

    def test_wiki_helpers(self) -> None:
        self.assertEqual(cli.encode_wiki_title("Project Home"), "Project%20Home")

        wrapped = {"wiki_page": {"title": "Home", "text": "hello"}}
        self.assertEqual(cli.extract_legacy_wiki_page(wrapped)["title"], "Home")
        self.assertEqual(cli.extract_wiki_text({"text": {"raw": "abc"}}), "abc")
        self.assertEqual(cli.extract_wiki_text({"text": "def"}), "def")

    # ------------------------------------------------------------------
    # New tests for log-spent-hours feature
    # ------------------------------------------------------------------

    def test_hours_to_iso_duration_whole(self) -> None:
        self.assertEqual(cli.hours_to_iso_duration(1), "PT1H")
        self.assertEqual(cli.hours_to_iso_duration(2), "PT2H")

    def test_hours_to_iso_duration_fractional(self) -> None:
        self.assertEqual(cli.hours_to_iso_duration(1.5), "PT1H30M")
        self.assertEqual(cli.hours_to_iso_duration(0.25), "PT15M")

    def test_hours_to_iso_duration_zero_raises(self) -> None:
        with self.assertRaises(cli.OpenProjectError):
            cli.hours_to_iso_duration(0)
        with self.assertRaises(cli.OpenProjectError):
            cli.hours_to_iso_duration(-1)

    def test_find_or_create_work_package_finds_existing(self) -> None:
        """find_or_create_work_package returns existing WP without creating."""
        existing_wp = {"id": 10, "subject": "Internal discussion and meetings"}
        project = {"id": 1, "_links": {"self": {"href": "/api/v3/projects/1"}}}

        client = MagicMock(spec=cli.OpenProjectClient)
        client.list_work_packages.return_value = [existing_wp]

        wp, created = cli.OpenProjectClient.find_or_create_work_package(
            client, project=project, subject="Internal discussion and meetings"
        )
        self.assertFalse(created)
        self.assertEqual(wp["id"], 10)
        client.create_work_package.assert_not_called()

    def test_find_or_create_work_package_creates_when_missing(self) -> None:
        """find_or_create_work_package creates WP when subject not found."""
        new_wp = {"id": 99, "subject": "Internal discussion and meetings"}
        project = {"id": 1, "_links": {"self": {"href": "/api/v3/projects/1"}}}

        client = MagicMock(spec=cli.OpenProjectClient)
        client.list_work_packages.return_value = []
        client.create_work_package.return_value = new_wp

        wp, created = cli.OpenProjectClient.find_or_create_work_package(
            client, project=project, subject="Internal discussion and meetings"
        )
        self.assertTrue(created)
        self.assertEqual(wp["id"], 99)
        client.create_work_package.assert_called_once()

    def test_log_time_invalid_hours_raises(self) -> None:
        """log_time rejects non-positive hours."""
        client = MagicMock(spec=cli.OpenProjectClient)
        with self.assertRaises(cli.OpenProjectError):
            cli.OpenProjectClient.log_time(client, work_package_id=1, hours=0, activity_name="x")

    def test_log_time_uses_provided_date(self) -> None:
        """log_time sends the supplied spent_on date in the API payload."""
        client = MagicMock(spec=cli.OpenProjectClient)
        client.resolve_time_entry_activity.return_value = ("Development", "/api/v3/time_entries/activities/1")
        client._request.return_value = {"id": 5, "spentOn": "2026-01-15"}

        cli.OpenProjectClient.log_time(
            client, work_package_id=7, hours=1, activity_name="Development", spent_on="2026-01-15"
        )

        call_args = client._request.call_args
        payload = call_args.kwargs.get("payload") or call_args[1].get("payload") or call_args[0][2]
        self.assertEqual(payload["spentOn"], "2026-01-15")

    def test_log_time_defaults_to_today(self) -> None:
        """log_time uses today's date when spent_on is not provided."""
        from datetime import datetime as _dt

        today = _dt.now().date().isoformat()
        client = MagicMock(spec=cli.OpenProjectClient)
        client.resolve_time_entry_activity.return_value = ("Development", "/api/v3/time_entries/activities/1")
        client._request.return_value = {"id": 6, "spentOn": today}

        cli.OpenProjectClient.log_time(
            client, work_package_id=8, hours=2, activity_name="Development"
        )

        call_args = client._request.call_args
        payload = call_args.kwargs.get("payload") or call_args[1].get("payload") or call_args[0][2]
        self.assertEqual(payload["spentOn"], today)


if __name__ == "__main__":
    unittest.main()
