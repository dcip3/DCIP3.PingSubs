import unittest

from scripts.check_commit_messages import validate


class CommitMessageTests(unittest.TestCase):
    def test_accepts_conventional_messages(self):
        for message in (
            "fix: preserve monthly dates",
            "feat(bot)!: require explicit admin setup\n\nBREAKING CHANGE: setup changed.",
            "docs: explain setup\n\nAdd deployment instructions.\n# Git comment",
            'Revert "fix: preserve monthly dates"',
            'Revert "feat: add a long enough subject to pass the usual limit" (#12)',
            "fix: " + "a" * 67 + " (#123)",
        ):
            with self.subTest(message=message):
                self.assertEqual(validate(message), [])

    def test_rejects_invalid_messages(self):
        for message in (
            "", "# Only a comment", "Update files", "fix: Исправить даты",
            "fix: update dates — again", "fix: " + "a" * 68, "fix: " + "a" * 68 + " (#1)",
            "fix: update dates\nMissing blank line",
        ):
            with self.subTest(message=message):
                self.assertTrue(validate(message))

    def test_ignores_the_diff_below_the_scissors_line(self):
        message = (
            "fix: preserve monthly dates\n\n"
            "# ------------------------ >8 ------------------------\n"
            "+    text = \"✅ Payment confirmed\"\n"
        )
        self.assertEqual(validate(message), [])

    def test_honours_a_custom_comment_character(self):
        message = "fix: preserve monthly dates\n; Please enter the message\n"
        self.assertEqual(validate(message, ";"), [])

    def test_stored_messages_keep_lines_that_start_with_hash(self):
        self.assertEqual(validate("fix: count members\n\n#12 was off by one.", None), [])


if __name__ == "__main__":
    unittest.main()
