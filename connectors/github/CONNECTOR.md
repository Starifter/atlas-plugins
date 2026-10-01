---
name: github
title: GitHub
description: Repositories, issues, pull requests and Actions on GitHub.
publisher: GitHub
homepage: https://github.com/github/github-mcp-server
url: https://api.githubcopilot.com/mcp/
headers:
  Authorization: "Bearer ${GITHUB_TOKEN}"
asks:
  GITHUB_TOKEN: a personal access token - github.com/settings/personal-access-tokens
categories: [developer]
---
GitHub's own remote server. GitHub's sign-in does not register clients on the fly, so this
one takes a personal access token instead: a fine-grained token limited to the repositories
you want the model to reach is the careful choice.
