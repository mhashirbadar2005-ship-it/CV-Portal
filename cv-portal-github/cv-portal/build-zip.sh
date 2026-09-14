#!/usr/bin/env bash
#
# Builds a .zip you can upload straight into the Lambda console —
# no Docker, no ECR, no CLI deploy needed.
#
# Produces two files in ./dist:
#   layer.zip     dependencies   (~5 MB)  — upload once as a Lambda Layer
#   function.zip  your code only (~30 KB) — re-upload on every change
#
# Splitting them means a code change is a 30 KB upload instead of 5 MB.
# If you would rather keep it to one file, pass --single:
#   ./build-zip.sh --single    →  dist/lambda.zip  (code + deps together)
#
# Usage:  ./build-zip.sh
#
set -euo pipefail

cd "$(dirname "$0")"
SINGLE=false
[ "${1:-}" = "--single" ] && SINGLE=true

# Lambda's Python 3.12 runtime. Must match the runtime you pick in the console.
PY_VERSION="3.12"

rm -rf dist build
mkdir -p dist build

echo "==> Installing dependencies for the Lambda runtime"
# --platform / --only-binary matter: pydantic-core is a compiled extension, so
# installing on Windows or macOS without these flags downloads a wheel that
# cannot run on Lambda. The error you get is a confusing
# "Unable to import module 'lambda_handler'".
#
# boto3 and botocore are deliberately left out — the Lambda runtime already
# includes them, and adding them wastes ~15 MB.
pip install \
  --target build/python \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version "$PY_VERSION" \
  --only-binary=:all: \
  --upgrade \
  --quiet \
  fastapi==0.141.1 \
  starlette==1.6.0 \
  jinja2==3.1.6 \
  python-multipart==0.0.32 \
  pydantic==2.13.5 \
  pydantic-settings==2.15.0 \
  email-validator==2.3.0 \
  mangum==0.22.0

echo "==> Stripping build artefacts"
find build/python -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find build/python -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true
find build/python -type f -name "*.pyc" -delete 2>/dev/null || true
# NOTE: .dist-info folders are deliberately kept. pydantic checks
# email-validator's installed version via importlib.metadata at import time,
# which reads these folders -- stripping them breaks the import entirely
# ("No package metadata was found for email-validator"). They're a few KB
# each, not worth the risk.

if [ "$SINGLE" = true ]; then
  echo "==> Packaging one combined zip"
  cp -r build/python/. build/bundle/ 2>/dev/null || { mkdir -p build/bundle; cp -r build/python/. build/bundle/; }
  cp -r app build/bundle/
  cp lambda_handler.py build/bundle/
  find build/bundle -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
  # Lambda runs as a non-root user and needs read access to every file.
  chmod -R a+rX build/bundle
  (cd build/bundle && zip -rq ../../dist/lambda.zip .)
  echo
  echo "dist/lambda.zip  $(du -h dist/lambda.zip | cut -f1)"
  echo "Upload this under Code → Upload from → .zip file"
else
  echo "==> Packaging layer.zip (dependencies)"
  # A layer must contain a top-level directory named exactly "python".
  chmod -R a+rX build/python
  (cd build && zip -rq ../dist/layer.zip python)

  echo "==> Packaging function.zip (your code)"
  mkdir -p build/fn
  cp -r app build/fn/
  cp lambda_handler.py build/fn/
  find build/fn -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
  chmod -R a+rX build/fn
  (cd build/fn && zip -rq ../../dist/function.zip .)

  echo
  echo "dist/layer.zip     $(du -h dist/layer.zip | cut -f1)   → Lambda → Layers → Create layer"
  echo "dist/function.zip  $(du -h dist/function.zip | cut -f1)   → your function → Code → Upload from → .zip"
fi

rm -rf build
echo
echo "Handler to set in the console:  lambda_handler.handler"
