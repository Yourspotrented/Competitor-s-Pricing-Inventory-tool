#!/bin/sh
# Vercel build: publish the dashboard as a static page that talks to the
# backend on Render. API_BASE is a Vercel environment variable, e.g.
#   API_BASE=https://competitors-data-finder.onrender.com
set -e
mkdir -p out
cp backend/static/index.html out/index.html
printf 'window.API_BASE = "%s";\n' "${API_BASE:-}" > out/config.js
echo "dashboard built; API_BASE=${API_BASE:-<not set — page will call itself and fail>}"
