# ApexCamp Test Deploy Webhook Relay

This relay receives GitHub push webhooks from `eason8811/apex-camp-deploy`, verifies `X-Hub-Signature-256`, filters changes under `environments/test/`, immediately returns HTTP 202, and asynchronously triggers the Arcane GitOps webhook for `ApexCamp Test`.

Public endpoint, after Nginx routing:

```text
https://test-web.apexcamp.net/webhooks/deploy-test
```

Health endpoint inside Docker network:

```text
http://deploy-webhook-relay-test:8080/healthz
```
