#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(date +%Y%m%d-%H%M%S)}"
DIST_DIR="${PROJECT_DIR}/dist"
ARCHIVE_ROOT="migrator"
ARCHIVE_NAME="${ARCHIVE_ROOT}-installer-${VERSION}.tar.gz"
STAGING_DIR="${DIST_DIR}/.build-installer"

mkdir -p "$DIST_DIR"
rm -f "${DIST_DIR}/${ARCHIVE_NAME}"
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR/${ARCHIVE_ROOT}"

tar \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='data' \
  --exclude='dist' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  -C "$PROJECT_DIR" \
  -cf - \
  . | tar -C "${STAGING_DIR}/${ARCHIVE_ROOT}" -xf -

tar -C "$STAGING_DIR" -czf "${DIST_DIR}/${ARCHIVE_NAME}" "$ARCHIVE_ROOT"
rm -rf "$STAGING_DIR"

echo "${DIST_DIR}/${ARCHIVE_NAME}"
