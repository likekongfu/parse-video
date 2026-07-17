# Compose integration kit

Copy these files into the repository root:

- compose.yml
- .gitignore
- .dockerignore
- env/*.env.example
- scripts/deploy.sh

Do not commit real env/*.env files.

First server migration for the old media image tag:

    docker image tag media-converter:token01 media-converter:latest

Then back up the current server compose before pulling the newly tracked file.
