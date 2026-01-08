#!/bin/bash
# Build script for K3s Autoscaler Lambda deployment package

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAMBDA_DIR="$SCRIPT_DIR"
BUILD_DIR="$LAMBDA_DIR/build"
ZIP_FILE="$BUILD_DIR/lambda.zip"

echo "Building Lambda deployment package..."

# Clean previous build
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# Copy source files to build (Lambda flattens structure)
echo "Copying source files..."
mkdir -p "$BUILD_DIR"
cp -r "$LAMBDA_DIR/src"/* "$BUILD_DIR/"
cp "$LAMBDA_DIR/main.py" "$BUILD_DIR/"

# Install dependencies using uv
echo "Installing dependencies with uv..."
cd "$BUILD_DIR"

# Copy pyproject.toml and uv.lock to build directory for dependency installation
cp "$LAMBDA_DIR/pyproject.toml" .
cp "$LAMBDA_DIR/uv.lock" .

# Install dependencies
uv sync --frozen --no-dev

# Copy dependencies from .venv to root (Lambda expects flattened structure)
echo "Flattening dependencies..."
cp -r .venv/lib/python*/site-packages/* . 2>/dev/null || true

# Remove venv to reduce package size
rm -rf .venv

# Create deployment package
echo "Creating deployment zip..."
zip -r "$ZIP_FILE" . -x "*.pyc" -x "__pycache__" -x "*/__pycache__/*" > /dev/null

echo "Lambda package created: $ZIP_FILE"
echo "Package size:"
ls -lh "$ZIP_FILE"
echo "Done!"

