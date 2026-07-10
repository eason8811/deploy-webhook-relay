# ApexCamp Test Deploy Webhook Relay

This relay receives GitHub push webhooks from `eason8811/apex-camp-deploy`, verifies `X-Hub-Signature-256`, filters changes under `environments/test/`, immediately returns HTTP 202, and asynchronously triggers the Arcane GitOps webhook for `ApexCamp Test`.

For every accepted delivery, the relay can send an Arcane-styled received email
and a final result email. The first contains GitHub/PR, branch, commit, changed
files, target, and timestamp details. The second is sent after Arcane responds;
success requires both HTTP 2xx and JSON `success=true`. GitHub API lookup falls
back to parsing `Merge pull request #...` when unavailable.

Public endpoint, after Nginx routing:

```text
https://test-web.apexcamp.net/webhooks/deploy-test
```

Health endpoint inside Docker network:

```text
http://deploy-webhook-relay-test:8080/healthz
```

Configure SMTP and a fine-grained `GITHUB_TOKEN` with `Pull requests: read` in
the untracked `.env`. Start with `EMAIL_ENABLED=true` and `DRY_RUN=true` to
verify the received and skipped-result emails without invoking Arcane. SMTP,
GitHub API, and Arcane calls are not retried.
