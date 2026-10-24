#!/usr/bin/env bash
# Remove the SupplyLine stack (Lambda, API, DynamoDB table). Leaves the artifact bucket.
set -euo pipefail
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
STACK="${STACK_NAME:-supplyline}"
aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION"
aws cloudformation wait stack-delete-complete --stack-name "$STACK" --region "$REGION"
echo "Deleted stack $STACK."
