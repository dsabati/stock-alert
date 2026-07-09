# Cloudflare scheduler for GitHub workflow

This directory contains a Cloudflare Worker that triggers the GitHub workflow dispatch API on a fixed cron.

## 1) Create a GitHub token

Use a fine-grained personal access token with access to this repository.

Minimum permissions:

- Actions: Read and write
- Contents: Read

## 2) Install and login to Wrangler

```bash
npm install -g wrangler
wrangler login
```

## 3) Configure repository values

Edit `wrangler.toml` and set:

- `GITHUB_OWNER`
- `GITHUB_REPO`
- `GITHUB_WORKFLOW_FILE` (default is `check.yml`)
- `GITHUB_REF` (default is `main`)

## 4) Add the GitHub token as a secret

From this directory:

```bash
cd cloudflare
wrangler secret put GITHUB_TOKEN
```

Paste the token when prompted.

## 5) Deploy

```bash
wrangler deploy
```

## 6) Verify

- In Cloudflare Workers, confirm the Cron Trigger is active.
- In GitHub Actions, confirm new runs appear with trigger type `workflow_dispatch`.

## 7) Optional: manual test from terminal

```bash
curl -X POST \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/<OWNER>/<REPO>/actions/workflows/check.yml/dispatches \
  -d '{"ref":"main"}'
```

A successful dispatch returns HTTP `204 No Content`.
