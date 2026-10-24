# Telegram Lecture Manager

A Telegram DM-controlled lecture manager with MongoDB
permanent indexing and SQLite local job queue.

## Features

- Search categories using keywords
- Select goals with index numbers
- Browse batches and packs
- Select all or specific subjects
- Discover accessible lectures
- Download video and board JSON
- Download available slide/PDF assets
- Upload files into Telegram Forum Topics
- DM live progress and error notifications
- MongoDB permanent Telegram message index
- SQLite local queue
- Skip already completed lectures

## Requirements

- Python 3.10+
- Ubuntu VPS
- MongoDB Atlas database
- Telegram bot and API credentials
- Private Telegram Supergroup with Topics enabled
- Bot with Manage Topics and posting permissions
- Legitimate access and permission to download/share source media

## Installation

Create a virtual environment:

    python3 -m venv .venv
    source .venv/bin/activate

Install dependencies:

    pip install -r requirements.txt
    python -m playwright install --with-deps chromium

Copy environment example:

    cp .env.example .env

Fill all configuration values in .env.

Check the code:

    python -m py_compile bot.py catalog.py downloader.py storage.py

Start:

    python bot.py

## Telegram Commands

/start  - Start category selection
/search - Start a new search
/status - View current progress
/pause  - Pause queue after current operation
/resume - Resume queue
/cancel - Request cancellation
/retry  - Requeue latest failed job

## Selection Example

User: /start
Bot: Enter category
User: CA
Bot: Numbered goals
User: 1
Bot: Numbered packs/batches
User: 2
Bot: Numbered subjects
User: all
Bot: Confirm
User: YES

## Database

MongoDB:
- lecture_topics
- lecture_files
- lecture_records

SQLite:
- .state/jobs.sqlite3

Previously migrated files may reside in older MongoDB
collections. They are not automatically remapped to new
lecture keys without matching metadata.

## Limitations

This is an initial integrated implementation.

Goal search and class pagination are UI-dependent and
not verified across every website category.

Downloads require legitimate access. The software does
not bypass authentication, DRM, or access restrictions.

SQLite queue state is local; migrate or back it up when
changing VPS. MongoDB stores persistent media references.

Website-to-Telegram streaming is not included yet.

Download progress depends on receiving reliable size
information from the source server.

Do not publish .env, sessions, browser profiles, download
folders or private access URLs on GitHub.
