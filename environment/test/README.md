# ApexCamp Test Deploy Webhook Relay

This relay receives GitHub push webhooks from `eason8811/apex-camp-deploy`, verifies `X-Hub-Signature-256`, filters changes under `environments/test/`, and triggers the Arcane GitOps webhook for `ApexCamp Test` only when the push commit is the merge commit of a CI-created pull request.

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

Configure a fine-grained `GITHUB_TOKEN` with `Pull requests: read` in the
untracked `.env`; it is required for merge verification, even if email is off.
The default `CI_PULL_REQUEST_HEAD_PREFIXES=ci/` accepts CI branches such as
`ci/test/...`; set it to a comma-separated list if the CI naming changes. If
GitHub cannot be queried, the endpoint returns HTTP 503 and does not call
Arcane. Start with `EMAIL_ENABLED=true` and `DRY_RUN=true` to
verify the received and skipped-result emails without invoking Arcane. GitHub
merge metadata lookup uses bounded exponential backoff (controlled by
`GITHUB_PR_VERIFICATION_MAX_SECONDS`, default 8 seconds); SMTP and Arcane calls
are not retried.
