# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- **Breaking:** settings are read only from `SORTIFY_`-prefixed environment
  variables (for example `SORTIFY_MAX_FOLDERS=9` instead of `MAX_FOLDERS=9`),
  and a `.env` file is read only from the app directory (`~/.autosorter/.env`
  or `$AUTOSORTER_APP_DIR/.env`), never from the current working directory.
  Values from the environment are no longer written to `settings.json`.
  Rename any unprefixed variables you set. (#736)
