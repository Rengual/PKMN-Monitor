# PKMN-Monitor

A public Pokémon TCG 30th Celebration product-availability monitor.

## What it checks
The monitor scans configured retailers for English Pokémon 30th Celebration products, including Elite Trainer Boxes and Ultra-Premium Collections, and records availability and prices where detectable.

## How it runs
The monitor is designed to run as a GitHub Actions workflow. It stores its latest comparison baseline in `status.json` so that changes can be detected between runs.

- Run frequency is controlled by the external scheduler that dispatches the workflow.
- You can also start a run manually from **Actions → Pokémon Monitor → Run workflow**.
- The workflow commits updated `status.json` and `monitor_health.json` files.

## Notifications
Notifications are optional. To enable them, create a private ntfy topic and add a repository Actions secret named `NTFY_TOPIC` in **Settings → Secrets and variables → Actions**. Do not put notification topics or other credentials directly into code.

## Public repository notice
This repository is public. Do not add passwords, API tokens, private dashboard files, or other credentials. Retailer pages may block automated requests or change their layout, so availability and price detections are best-effort and should be confirmed at the retailer before purchase.
