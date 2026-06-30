# stock-alert

GitHub Actions stock monitor for the Midea PortaSplit product on Amazon, Castorama, Darty, and Leroy Merlin.

## Required secrets

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `ALERT_FROM_EMAIL`
- `ALERT_TO_EMAIL`

## Optional environment variables

- `STOCK_PRODUCTS`: JSON array used to override the default tracked products
- `EMAIL_SUBJECT_PREFIX`: email subject prefix, defaults to `Stock alert`