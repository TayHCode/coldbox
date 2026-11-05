# Security

Coldbox runs other people's CI code on your machine. Each job gets a fresh VM that is destroyed afterwards, its network can
reach the internet but not your LAN, and by default it serves only private repos and refuses pull requests from forks. Read
the safety model in the README before using `--allow-public` or `--allow-forks`. Do not include tokens, private hostnames or
personal paths in reports.

This project is unpublished and has no public security reporting channel. Report concerns to the maintainer through an
already established private channel. No support or response-time commitment is offered.
