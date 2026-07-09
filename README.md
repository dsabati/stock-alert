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
