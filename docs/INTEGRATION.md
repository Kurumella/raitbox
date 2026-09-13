# Live integration testing & AWS setup

The unit suite (`pytest`) runs with in-memory fakes and needs no AWS. The
**integration** suite (`tests/integration/`, marker `integration`) exercises the
real services and **bills short live sessions**. It is skipped by default and
also skips per-module when the env vars a backend needs are absent.

```bash
# everything you have set up for; unconfigured backends skip themselves
AWS_PROFILE=raitbox-dev AWS_DEFAULT_REGION=us-west-2 pytest -m integration -v
```

Prereqs shared by all backends: AWS credentials on the standard chain, a region
where the target service is enabled, and boto3 ≥ 1.43.

---

## 1. `agentcore-ci` — Bedrock AgentCore Code Interpreter (lowest setup)

Only needs credentials + region.

IAM:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "bedrock-agentcore:StartCodeInterpreterSession",
      "bedrock-agentcore:InvokeCodeInterpreter",
      "bedrock-agentcore:StopCodeInterpreterSession"
    ],
    "Resource": "*"
  }]
}
```

Run:

```bash
AWS_DEFAULT_REGION=us-west-2 pytest -m integration \
    tests/integration/test_agentcore_ci_live.py -v
```

---

## 2. `agentcore-runtime` — Bedrock AgentCore Runtime

Requires an **already-deployed** runtime (RaitBox does not create runtimes — use
the AgentCore CLI or `create_agent_runtime` to deploy an ARM64 container / zip
agent that serves `/invocations` + `/ping` on port 8080).

IAM:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "bedrock-agentcore:InvokeAgentRuntimeCommand",
      "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
      "bedrock-agentcore:StopRuntimeSession"
    ],
    "Resource": "arn:aws:bedrock-agentcore:*:*:runtime/*"
  }]
}
```

Env + run (interactive-shell test also needs `pip install 'raitbox[runtime]'`):

```bash
export RAITBOX_IT_RUNTIME_ARN=arn:aws:bedrock-agentcore:us-west-2:ACCT:runtime/NAME
AWS_DEFAULT_REGION=us-west-2 pytest -m integration \
    tests/integration/test_agentcore_runtime_live.py -v
```

---

## 3. `microvm` — AWS Lambda MicroVMs

Two roles and one prebuilt image are needed.

**Build role** (assumed by `lambda.amazonaws.com`; used during image build):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::YOUR_BUILD_BUCKET/*" },
    { "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "*" }
  ]
}
```

**Execution role** (runtime; optional but recommended) — CloudWatch Logs plus
any AWS access your sandbox workloads need.

**Caller** policy (build + run):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": [
        "lambda:CreateMicrovmImage", "lambda:GetMicrovmImage",
        "lambda:RunMicrovm", "lambda:GetMicrovm", "lambda:ListMicrovms",
        "lambda:CreateMicrovmAuthToken", "lambda:TerminateMicrovm"
      ], "Resource": "*" },
    { "Effect": "Allow", "Action": ["iam:PassRole"],
      "Resource": ["arn:aws:iam::ACCT:role/raitbox-build-role",
                   "arn:aws:iam::ACCT:role/raitbox-exec"] },
    { "Effect": "Allow", "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::YOUR_BUILD_BUCKET/*" }
  ]
}
```

Build the image (bakes in the RaitBox agent server), then run the tests:

```bash
pip install -e '.[server]'
IMAGE_ARN=$(python -m raitbox.tooling.build_image \
    --name raitbox-sandbox \
    --code-bucket YOUR_BUILD_BUCKET \
    --build-role-arn arn:aws:iam::ACCT:role/raitbox-build-role \
    --region us-east-1)
    # add --egress-connector arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS
    # if your image's build steps fetch from the internet (dnf/pip/EPEL); repeatable

export RAITBOX_IT_MICROVM_IMAGE="$IMAGE_ARN"
export RAITBOX_IT_EXECUTION_ROLE=arn:aws:iam::ACCT:role/raitbox-exec   # optional
export RAITBOX_IT_EGRESS=arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS  # optional
AWS_DEFAULT_REGION=us-east-1 pytest -m integration \
    tests/integration/test_microvm_live.py -v
```

Notes:
- The `lambda-microvms` client requires boto3 ≥ 1.43; the Lambda/runtime-bundled
  boto3 does not include it.
- MicroVMs are a newer service — confirm availability in your region and that
  the feature is enabled for your account.
- Sessions bill until stopped. Tests kill their sandboxes in `finally`, and the
  process-exit guard stops any leak (cap with `RAITBOX_MAX_SESSIONS`).

### Running the example notebooks (`examples/12–18, 20`)

- Point the notebooks at your built image via **`RAITBOX_MICROVM_IMAGE`** (the
  `RAITBOX_IT_*` variables above are only for the integration test suite):
  ```bash
  export RAITBOX_MICROVM_IMAGE="$IMAGE_ARN"
  ```
- **No ingress connector needed for previews.** `get_preview_link(port)` and
  `get_host(port)` reach a port in the VM over the managed endpoint using a
  short-lived, port-scoped auth token — you do not attach an ingress connector.
- **Computer use (`18-computer-use-agent`) needs a desktop stack in the image.**
  The AWS-managed base is **aarch64**, so build a custom image (see
  `examples/21-custom-image-and-mcp`) that adds: `xorg-x11-server-Xvfb` + `xterm`
  + `ImageMagick` (all from the base repos — `ImageMagick`'s `import` is the
  screenshot tool), plus `xdotool` from **EPEL 9 (aarch64)** via a repo file
  pinned to `.../9/Everything/aarch64/`. `scrot` isn't packaged for aarch64; the
  agent server uses `import` when `scrot` is absent. The notebook preflights the
  tools and skips its live loop only if they're missing.
- Declarative `Image()` layers may set their own `WORKDIR`/`ENV` freely — the
  agent server is resolved via `PYTHONPATH`, so custom layers don't break startup.

---

## Cost & safety

Every integration test creates a short-lived session and tears it down. Prefer a
dedicated dev account/region. Set `RAITBOX_MAX_SESSIONS` to cap concurrency.
