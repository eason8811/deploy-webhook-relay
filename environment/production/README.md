# ApexCamp Deploy Webhook Relay

Flow:

GitHub deploy repo push -> Relay -> Arcane Git Sync WebHook

- `environments/production/core/**` triggers core Arcane webhook.
- `environments/production/portal-111/**` triggers portal Arcane webhook.
- Other changes are ignored.

For every accepted delivery, the relay can send two Arcane-styled emails:

1. A received email with the GitHub event, associated pull request, branch,
   commit, changed files, targets, and timestamps.
2. A result email after all Arcane targets finish. A target is successful only
   when Arcane returns HTTP 2xx and JSON `success=true`.

Production targets remain sequential (`core` then `portal`). Arcane, GitHub API,
and SMTP calls are not retried. Failure or timeout details are included in the
result email and logs.

Start:

```bash
cd /data/docker/deploy-webhook-relay
cp .env.example .env
nano .env
docker compose --env-file .env -f docker-compose.yaml up -d --build
```

Before enabling email, configure the SMTP fields and a fine-grained
`GITHUB_TOKEN` with `Pull requests: read` access to the deploy repository. Keep
`EMAIL_ENABLED=false` and `DRY_RUN=true` for the first deployment, verify the
received and skipped-result emails, then enable the real Arcane call.

`/healthz` reports whether email and PR lookup are configured without exposing
credentials or recipient addresses.
