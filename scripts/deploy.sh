#!/usr/bin/env bash
# Deploy SupplyLine to your AWS account with one command.
#
#   1. On the event page click "Get AWS CLI credentials" and paste the exports.
#   2. bash scripts/deploy.sh
#
# Optional environment variables:
#   AWS_REGION        region for the stack            (default us-east-1)
#   STACK_NAME        CloudFormation stack name       (default supplyline)
#   BEDROCK_REGION    region for Bedrock calls        (default = AWS_REGION)
#   BEDROCK_MODEL_IDS comma-separated model IDs       (default: Claude Haiku 4.5, Nova 2 Lite, ...)
#   GUARDRAIL_ID      optional Bedrock Guardrail ID
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
STACK="${STACK_NAME:-supplyline}"

command -v aws >/dev/null 2>&1 || { echo "The AWS CLI is not installed or not on PATH." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required to build the package." >&2; exit 1; }

echo "Checking AWS credentials..."
ACCOUNT="$(aws sts get-caller-identity --query Account --output text --region "$REGION")" || {
  echo "No working AWS credentials. Use 'Get AWS CLI credentials' on the event page, then try again." >&2; exit 1; }
BUCKET="${ARTIFACT_BUCKET:-supplyline-artifacts-${ACCOUNT}-${REGION}}"
echo "  account $ACCOUNT, region $REGION, stack $STACK"

echo "Building the package..."
rm -rf build && mkdir -p build
python3 - <<'PY'
import hashlib, pathlib, zipfile
root = pathlib.Path("app")
out = pathlib.Path("build/supplyline.zip")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(root.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts or f.suffix == ".pyc":
            continue
        info = zipfile.ZipInfo(str(f.relative_to(root)), date_time=(2026, 1, 1, 0, 0, 0))  # fixed time: same code, same hash
        info.external_attr = 0o644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, f.read_bytes())
digest = hashlib.sha256(out.read_bytes()).hexdigest()[:16]
pathlib.Path("build/code.key").write_text(f"supplyline-{digest}.zip")
print(f"  {out} ({out.stat().st_size // 1024} KB)")
PY
KEY="$(cat build/code.key)"

if ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
  echo "Creating artifact bucket $BUCKET..."
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi
echo "Uploading $KEY..."
aws s3 cp build/supplyline.zip "s3://$BUCKET/$KEY" --region "$REGION" --only-show-errors

OVERRIDES=("CodeBucket=$BUCKET" "CodeKey=$KEY" "BedrockRegion=${BEDROCK_REGION:-$REGION}")
[ -n "${BEDROCK_MODEL_IDS:-}" ] && OVERRIDES+=("BedrockModelIds=$BEDROCK_MODEL_IDS")
[ -n "${GUARDRAIL_ID:-}" ] && OVERRIDES+=("GuardrailId=$GUARDRAIL_ID")

echo "Deploying the stack (first time takes about a minute)..."
aws cloudformation deploy \
  --template-file infra/template.yaml \
  --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM \
  --region "$REGION" \
  --no-fail-on-empty-changeset \
  --parameter-overrides "${OVERRIDES[@]}"

URL="$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='AppUrl'].OutputValue" --output text)"
echo
echo "SupplyLine is live: $URL"
echo
echo "Next: prove Bedrock is answering (fails if it fell back to rules):"
echo "  python3 scripts/smoke.py $URL"
