#!/usr/bin/env bash
# The one place the production server's address lives. Every script that
# reaches the server sources this file; nothing else hardcodes the address.
#
#   SERVER_HOST   the Tokyo box's Elastic IP (since 2026-09-21; the earlier
#                 auto-assigned address was released and is not ours any more —
#                 see docs/DEPLOYMENT.md)
#   SERVER_USER   login user on the box
#   SERVER        user@host, what ssh/rsync take; override any of the three
#                 with an environment variable of the same name
SERVER_HOST="${SERVER_HOST:-16.76.92.209}"
SERVER_USER="${SERVER_USER:-ubuntu}"
SERVER="${SERVER:-${SERVER_USER}@${SERVER_HOST}}"
