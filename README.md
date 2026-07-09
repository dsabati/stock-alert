# stock-alert

Stock monitor script that checks exact product page URLs and sends email alerts when stock status changes.

The script includes dedicated parsing for ClimRadar pages (for example `https://climradar.fr/?cp=75018`) and treats `en stock` / `stock faible` as available and `rupture` as unavailable.

## Required secrets

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `ALERT_FROM_EMAIL`
- `ALERT_TO_EMAIL`

## Optional environment variables

- `STOCK_PRODUCTS`: required JSON array with exact product page URLs
- `EMAIL_SUBJECT_PREFIX`: email subject prefix, defaults to `Stock alert`
- `NOTIFICATION_WEBHOOK_URL`: optional HTTP webhook endpoint for notifications (Slack/Discord/ntfy/custom)
- `NOTIFICATION_WEBHOOK_TIMEOUT`: webhook timeout in seconds, defaults to `15`
- `ALERT_ON_INCONCLUSIVE`: send an email when no product status can be determined in a run, defaults to `true`
- `ALERT_ON_INCONCLUSIVE_EVERY_RUN`: if `true`, send inconclusive email every run (can be noisy), defaults to `false`
- `ALERT_ON_RECOVERY`: send an email when checks recover from inconclusive to determined, defaults to `false`

By default, inconclusive alerts are deduplicated: one email is sent when checks first become inconclusive, then suppressed on repeated inconclusive runs until recovery.

If `NOTIFICATION_WEBHOOK_URL` is set, the script sends notifications to that webhook without using SMTP. If both webhook and SMTP are configured, it attempts both channels.

## Webhook notification examples (no SMTP)

Slack incoming webhook:

```env
NOTIFICATION_WEBHOOK_URL=https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX
```

Discord webhook:

```env
NOTIFICATION_WEBHOOK_URL=https://discord.com/api/webhooks/123456789012345678/your_token
```

ntfy topic (requires `/json`):

```env
NOTIFICATION_WEBHOOK_URL=https://ntfy.sh/your-topic/json
```

## Local configuration

Use `ignored.env` for local-only product configuration. The file is git-ignored.

Example:

```env
STOCK_PRODUCTS=[{"name":"Midea PortaSplit - Paris 75018","retailer":"ClimRadar","url":"https://climradar.fr/?cp=75018"}]
```

For non-ClimRadar pages, provide keyword lists:

```env
STOCK_PRODUCTS=[{"name":"Product name","retailer":"Retailer","url":"https://example.com/product","expected_keywords":["brand","model"],"in_stock_keywords":["add to cart"],"out_of_stock_keywords":["out of stock"]}]
```
