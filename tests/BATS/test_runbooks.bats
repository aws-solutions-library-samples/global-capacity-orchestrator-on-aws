#!/usr/bin/env bats
# Functional regression coverage for the Root-State Recovery ALB association check.

RUNBOOK="docs/RUNBOOKS.md"
LOAD_BALANCER_ARN="arn:aws:elasticloadbalancing:us-west-2:123456789012:loadbalancer/app/gco-backend/50dc6c495c0c9188"
LISTENER_ONE_ARN="arn:aws:elasticloadbalancing:us-west-2:123456789012:listener/app/gco-backend/50dc6c495c0c9188/1111111111111111"
LISTENER_TWO_ARN="arn:aws:elasticloadbalancing:us-west-2:123456789012:listener/app/gco-backend/50dc6c495c0c9188/2222222222222222"
CERT_ARN="arn:aws:acm:us-west-2:123456789012:certificate/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
VERIFIED_ALB_HOSTNAME="internal-gco.us-west-2.elb.amazonaws.com"

setup() {
    STUB_BIN="$BATS_TEST_TMPDIR/bin"
    mkdir -p "$STUB_BIN"
    export AWS_CALLS="$BATS_TEST_TMPDIR/aws-calls"
    : > "$AWS_CALLS"

    cat > "$STUB_BIN/aws" <<'AWS_STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$AWS_CALLS"
case "$1 $2" in
    "elbv2 describe-load-balancers")
        [[ "$*" == *"--load-balancer-arns $LOAD_BALANCER_ARN"* ]]
        printf '%s\n' "$VERIFIED_ALB_HOSTNAME"
        ;;
    "elbv2 describe-listeners")
        [[ "$*" == *"--load-balancer-arn $LOAD_BALANCER_ARN"* ]]
        printf '%s\t%s\n' "$LISTENER_ONE_ARN" "$LISTENER_TWO_ARN"
        ;;
    "elbv2 describe-listener-certificates")
        [[ "$*" == *"--listener-arn "* ]]
        [[ "$*" == *":listener/app/"* ]]
        if [ "$ATTACHMENT_PRESENT" = "true" ] && [[ "$*" == *"$LISTENER_TWO_ARN"* ]]; then
            printf '%s\n' "$CERT_ARN"
        else
            printf 'None\n'
        fi
        ;;
    *)
        echo "unexpected aws call: $*" >&2
        exit 2
        ;;
esac
AWS_STUB
    chmod +x "$STUB_BIN/aws"

    CHECK_SCRIPT="$BATS_TEST_TMPDIR/check-association.sh"
    cat > "$CHECK_SCRIPT" <<'CHECK'
#!/usr/bin/env bash
set -euo pipefail
CERTIFICATE_ATTACHED=false
VERIFIED_LOAD_BALANCER_ARN=""
for LOAD_BALANCER_ARN in $LOAD_BALANCERS; do
    LISTENER_ARNS=$(aws elbv2 describe-listeners \
        --load-balancer-arn "$LOAD_BALANCER_ARN" --region "$REGION" \
        --query 'Listeners[].ListenerArn' --output text)
    for LISTENER_ARN in $LISTENER_ARNS; do
        ATTACHED=$(aws elbv2 describe-listener-certificates \
            --listener-arn "$LISTENER_ARN" --region "$REGION" \
            --query "Certificates[?CertificateArn=='${CERT_ARN}'].CertificateArn | [0]" \
            --output text)
        if [ "$ATTACHED" = "$CERT_ARN" ]; then
            CERTIFICATE_ATTACHED=true
            VERIFIED_LOAD_BALANCER_ARN=$LOAD_BALANCER_ARN
            break
        fi
    done
    [ "$CERTIFICATE_ATTACHED" = "true" ] && break
done
if [ "$CERTIFICATE_ATTACHED" != "true" ]; then
    echo "ERROR: certificate is absent from every load-balancer listener" >&2
    exit 1
fi
RESOLVED_ALB_HOSTNAME=$(aws elbv2 describe-load-balancers \
    --load-balancer-arns "$VERIFIED_LOAD_BALANCER_ARN" --region "$REGION" \
    --query 'LoadBalancers[0].DNSName' --output text)
if [ "$ALB_HOSTNAME" != "$RESOLVED_ALB_HOSTNAME" ]; then
    echo "ERROR: SSM hostname does not identify the certificate-bearing ALB" >&2
    exit 1
fi
CHECK
    chmod +x "$CHECK_SCRIPT"

    export PATH="$STUB_BIN:$PATH"
    export REGION="us-west-2"
    export ALB_HOSTNAME="$VERIFIED_ALB_HOSTNAME"
    export LOAD_BALANCERS="$LOAD_BALANCER_ARN"
    export LOAD_BALANCER_ARN LISTENER_ONE_ARN LISTENER_TWO_ARN CERT_ARN VERIFIED_ALB_HOSTNAME
}

@test "runbook resolves load balancers to listeners before checking certificates" {
    describe_listeners_line=$(grep -n 'aws elbv2 describe-listeners' "$RUNBOOK" | head -1 | cut -d: -f1)
    describe_certificates_line=$(grep -n 'aws elbv2 describe-listener-certificates' "$RUNBOOK" | head -1 | cut -d: -f1)
    [ "$describe_listeners_line" -lt "$describe_certificates_line" ]
    grep -q -- '--load-balancer-arn "$LOAD_BALANCER_ARN"' "$RUNBOOK"
    grep -q -- '--listener-arn "$LISTENER_ARN"' "$RUNBOOK"
    grep -q -- '--load-balancer-arns "$VERIFIED_LOAD_BALANCER_ARN"' "$RUNBOOK"
    grep -q -- '"$ALB_HOSTNAME" != "$VERIFIED_ALB_HOSTNAME"' "$RUNBOOK"
    grep -q -- 'openssl s_client -connect "${VERIFIED_ALB_HOSTNAME}:443"' "$RUNBOOK"
}

@test "association check accepts one matching listener" {
    export ATTACHMENT_PRESENT=true
    run bash "$CHECK_SCRIPT"
    [ "$status" -eq 0 ]
    grep -q 'describe-listeners.*loadbalancer/app/' "$AWS_CALLS"
    grep -q 'describe-listener-certificates.*listener/app/' "$AWS_CALLS"
    grep -q 'describe-load-balancers.*loadbalancer/app/' "$AWS_CALLS"
    ! grep -q 'describe-listener-certificates.*loadbalancer/app/' "$AWS_CALLS"
}

@test "association check fails when SSM points at a different ALB" {
    export ATTACHMENT_PRESENT=true
    export ALB_HOSTNAME="different.us-west-2.elb.amazonaws.com"
    run bash "$CHECK_SCRIPT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not identify the certificate-bearing ALB"* ]]
}

@test "association check fails when no listener contains the certificate" {
    export ATTACHMENT_PRESENT=false
    run bash "$CHECK_SCRIPT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"absent from every load-balancer listener"* ]]
}
