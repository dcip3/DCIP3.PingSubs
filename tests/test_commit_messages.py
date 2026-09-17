import unittest

from scripts.check_commit_messages import validate


class CommitMessageTests(unittest.TestCase):
    def test_accepts_conventional_messages(self):
        for message in (
            "fix: preserve monthly dates",
            "feat(bot)!: require explicit admin setup\n\nBREAKING CHANGE: setup changed.",
            "docs: explain setup\n\nAdd deployment instructions.\n# Git comment",
        ):
            with self.subTest(message=message):
                self.assertEqual(validate(message), [])

    def test_rejects_invalid_messages(self):
        for message in (
            "", "# Only a comment", "Update files", "fix: Исправить даты",
            "fix: " + "a" * 68, "fix: update dates\nMissing blank line",
        ):
            with self.subTest(message=message):
                self.assertTrue(validate(message))


if __name__ == "__main__":
    unittest.main()
