---
name: brave-search
title: Brave Search
description: Web, news, image and local search through the Brave Search API.
publisher: Brave
homepage: https://github.com/brave/brave-search-mcp-server
command: npx
args: [-y, "@brave/brave-search-mcp-server"]
env:
  BRAVE_API_KEY: "${BRAVE_API_KEY}"
asks:
  BRAVE_API_KEY: a Brave Search API key - api-dashboard.search.brave.com
categories: [search]
---
Brave's own server, run on this machine with `npx` - Node.js has to be installed. It needs a
Brave Search API key, which installing asks for.
