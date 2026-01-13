#!/bin/bash
# Build script for Bootstrap Test Lambda deployment package

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAMBDA_DIR="$SCRIPT_DIR"
BUILD_DIR="$LAMBDA_DIR/build"
ZIP_FILE="$BUILD_DIR/lambda.zip"

echo "Building Bootstrap Test Lambda deployment package..."

# Clean previous build
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# Copy source files to build (Lambda flattens structure)
echo "Copying source files..."
mkdir -p "$BUILD_DIR"
cp "$LAMBDA_DIR/main.py" "$BUILD_DIR/"

# Install dependencies using uv
echo "Installing dependencies with uv..."
cd "$BUILD_DIR"

# Copy pyproject.toml to build directory
cp "$LAMBDA_DIR/pyproject.toml" .

# Install dependencies (boto3 is included in Lambda runtime, but we include for local dev)
uv pip install --system --target . boto3

# Create deployment package
echo "Creating deployment zip..."
zip -r "$ZIP_FILE" . -x "*.pyc" -x "__pycache__" -x "*/__pycache__/*" > /dev/null

echo "Bootstrap Test Lambda package created: $ZIP_FILE"
echo "Package size:"
ls -lh "$ZIP_FILE"
echo "Done!"