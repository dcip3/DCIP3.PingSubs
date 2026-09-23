# Security

Report suspected vulnerabilities privately to keypoints.motion@gmail.com. Include
the affected version, reproduction steps, and impact. Do not include live tokens,
private keys, or real member/payment data in a public issue.

Use the latest version on `main`; older revisions do not receive security fixes.

## Deployment

- Keep the bot token in `.env` or your deployment secret store. Never commit it.
- Set `ADMIN_IDS` to your own Telegram ID before the first startup. Without it,
  the first user to send `/start` to a fresh database becomes the administrator.
- Invite links work once and expire after seven days; whoever opens one first is
  linked to that member, so send each link privately to its person.
- Keep the SQLite data directory private and back it up outside the repository.
- Store deployment credentials in GitHub Actions secrets, using a dedicated SSH
  account with only the access needed to deploy the bot, and pin the server's
  host key with `VDS_HOST_FINGERPRINT`.
- Run the secret and dependency checks described in `CONTRIBUTING.md` before
  publishing changes. Automated scans reduce risk but cannot prove no secret exists.

If a credential is committed, revoke or rotate it first. Removing a file or
rewriting Git history does not invalidate an exposed credential. Follow
[GitHub's sensitive-data removal guide](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
for history, cached references, forks, and other clones.
