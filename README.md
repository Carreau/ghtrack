# GitHub PR Activity Tracker

A Python script that generates a daily report of all PRs and issues you contributed to in the last week using the GitHub API.

## Setup

1. Install dependencies:
```
uv sync
```

2. Create a GitHub Personal Access Token:
   - Go to GitHub Settings → Developer settings → Personal access tokens → Tokens (classic)
   - Generate new token with `repo` scope (or `public_repo` if you only need public repos)
   - Copy the token

3. Set environment variables:
```
export GITHUB_TOKEN="your_token_here"
export GITHUB_USERNAME="your_username"
```

## Usage

```
python main.py
```

The script will display a report showing URLs grouped by day for:
- PRs where you pushed commits
- PRs and issues where you commented

## How It Works

The script:
1. Searches for PRs you authored and fetches your commits from the last 7 days
2. Searches for PRs and issues where you commented in the last 7 days
3. Groups all activity by day with unique URLs

## Token Permissions

Your token needs:
- `repo` scope for private repositories
- `public_repo` scope if you only work with public repositories
