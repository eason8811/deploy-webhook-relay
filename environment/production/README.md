# ApexCamp Deploy Webhook Relay

Flow:

GitHub deploy repo push -> Relay -> Arcane Git Sync WebHook

- `environments/production/core/**` triggers core Arcane webhook.
- `environments/production/portal-111/**` triggers portal Arcane webhook.
- Other changes are ignored.

Start:

```bash
cd /data/docker/deploy-webhook-relay
cp .env.example .env
nano .env
docker compose --env-file .env -f docker-compose.yaml up -d --build
```
