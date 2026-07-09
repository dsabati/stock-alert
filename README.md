# stock-alert

Stock monitor script that checks exact product page URLs and sends webhook alerts when stock status changes.

The script includes dedicated parsing for ClimRadar pages (for example `https://climradar.fr/?cp=75018`) and treats `en stock` / `stock faible` as available and `rupture` as unavailable.

## Optional environment variables

- `STOCK_PRODUCTS`: required JSON array with exact product page URLs
- `EMAIL_SUBJECT_PREFIX`: email subject prefix, defaults to `Stock alert`
- `NOTIFICATION_WEBHOOK_URLS`: optional list of webhook URLs (comma-separated or newline-separated)
- `NOTIFICATION_WEBHOOK_TIMEOUT`: webhook timeout in seconds, defaults to `15`
- `NOTIFICATION_DEBUG`: if `true`, prints webhook send attempts and HTTP status in script logs, defaults to `false`
- `WEBHOOK_DEBUG_PROBE`: if `true` in GitHub Actions variables, runs an explicit webhook probe step, defaults to `false`

Notifications are webhook-only. Configure one or more webhook URLs to receive alerts.

Example with multiple webhooks:

```env
NOTIFICATION_WEBHOOK_URLS=https://ntfy.sh/my-topic/json,https://hooks.slack.com/services/T000/B000/XXX
```

You can also use newlines:

```env
NOTIFICATION_WEBHOOK_URLS=https://ntfy.sh/my-topic/json
https://discord.com/api/webhooks/123/token
```

## Webhook notification examples (no SMTP)

Slack incoming webhook:

```env
NOTIFICATION_WEBHOOK_URLS=https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX
```

Discord webhook:

```env
NOTIFICATION_WEBHOOK_URLS=https://discord.com/api/webhooks/123456789012345678/your_token
```

ntfy topic (requires `/json`):

```env
NOTIFICATION_WEBHOOK_URLS=https://ntfy.sh/your-topic/json
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
