#!/bin/bash
# A script to add, commit, and push changes to GitHub.
# It uses a default timestamped message if none is provided.

# Get the repository root directory (works from any subdirectory in the repo)
REPO_ROOT="$(git rev-parse --show-toplevel)" || exit
cd "$REPO_ROOT" || exit

# --- New Logic ---
# Generate the Pacific Time timestamp.
# Using "America/Los_Angeles" is the correct way to handle PST/PDT automatically.
TIMESTAMP=$(TZ='America/Los_Angeles' date '+%Y-%m-%d %H:%M:%S %Z')

# Check if a commit message argument was provided.
if [ -z "$1" ]; then
  # If no argument, create the default message.
  COMMIT_MSG="commit at this time ${TIMESTAMP}"
else
  # If an argument exists, combine it with the timestamp.
  COMMIT_MSG="$1 ${TIMESTAMP}"
fi
# --- End New Logic ---

echo "Staging all changes..."
git add .

# Use the dynamically created commit message
echo "Committing with message: '${COMMIT_MSG}'..."
git commit -m "${COMMIT_MSG}"

echo "Pushing changes to GitHub..."
git push

echo "Push complete."
