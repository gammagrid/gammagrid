# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Email **[hello@gammagrid.io](mailto:hello@gammagrid.io)** with a description
of the issue and steps to reproduce it. We'll acknowledge receipt and follow
up as soon as we can.

## Scope

GammaGrid is a self-hosted dashboard intended to run on `localhost` (the
default `docker-compose.yml` publishes the port on `127.0.0.1` only, not the
local network). It has no authentication layer, stores no credentials, and
its only outbound calls are to Yahoo Finance (via `yfinance`) and, in the UI,
an embedded TradingView chart widget. Relevant reports include anything that
would let a snapshot's data, the SQLite file, or the host running the
container be compromised — not "the app has no login screen," which is a
known, intentional property of a single-user local tool.

## Supported versions

Pre-1.0, only the latest released version is supported. There is no formal
LTS branch at this stage.
