#!/usr/bin/env bash
#
# Build the image, push it to ECR, and point the Lambda function at the new tag.
# Run this after the one-time setup in README.md is done.
#
# Usage:  ./deploy.sh
#
set -euo pipefail

# ---- edit these once -------------------------------------------------------
AWS_REGION="${AWS_REGION:-ap-south-1}"
FUNCTION_NAME="${FUNCTION_NAME:-cv-portal}"
ECR_REPO="${ECR_REPO:-cv-portal}"
# ----------------------------------------------------------------------------

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
TAG="$(date +%Y%m%d-%H%M%S)"
IMAGE="${REGISTRY}/${ECR_REPO}:${TAG}"

echo "==> Region ${AWS_REGION}, account ${ACCOUNT_ID}"
echo "==> Building ${IMAGE}"

# Lambda runs on x86_64 unless the function is configured for arm64. Building on
# an Apple Silicon Mac without this flag produces an arm64 image that the
# function will refuse to start.
docker build --platform linux/amd64 -t "${IMAGE}" .

echo "==> Logging in to ECR"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

echo "==> Pushing"
docker push "${IMAGE}"

echo "==> Updating function code"
aws lambda update-function-code \
  --function-name "${FUNCTION_NAME}" \
  --image-uri "${IMAGE}" \
  --region "${AWS_REGION}" \
  --no-cli-pager >/dev/null

echo "==> Waiting for the update to finish"
aws lambda wait function-updated \
  --function-name "${FUNCTION_NAME}" \
  --region "${AWS_REGION}"

URL="$(aws lambda get-function-url-config \
        --function-name "${FUNCTION_NAME}" \
        --region "${AWS_REGION}" \
        --query FunctionUrl --output text 2>/dev/null || true)"

echo
echo "Deployed ${TAG}"
[ -n "${URL}" ] && echo "Function URL: ${URL}"
echo "Tail the logs with:  aws logs tail /aws/lambda/${FUNCTION_NAME} --follow"
