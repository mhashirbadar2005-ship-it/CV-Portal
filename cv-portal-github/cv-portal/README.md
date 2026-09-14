# CV submission portal — FastAPI + Jinja on AWS

A working CV intake form that stores files in S3 and notifies your team through SNS,
running on Lambda behind API Gateway.

```
Browser
   │  POST /apply  (multipart: form fields + PDF)
   ▼
API Gateway (HTTP API)          ← the public HTTPS endpoint
   │  event JSON, body base64-encoded
   ▼
Lambda (container image)        ← Mangum → FastAPI → Jinja
   ├─────► S3   put_object      ← the CV file, encrypted
   └─────► SNS  publish         ← plain-text alert to the hiring inbox
   │
   ▼
CloudWatch Logs                 ← everything the app printed
```

---

## Contents

1. [What each service actually is](#1-what-each-service-actually-is)
2. [Run it locally first](#2-run-it-locally-first)
3. [One-time AWS setup](#3-one-time-aws-setup)
4. [Deploy](#4-deploy)
5. [Verify and debug](#5-verify-and-debug)
6. [The gotchas that will bite you](#6-the-gotchas-that-will-bite-you)
7. [Where to take it next](#7-where-to-take-it-next)
8. [What this costs](#8-what-this-costs)
9. [Tear it all down](#9-tear-it-all-down)

---

## 1. What each service actually is

### IAM — who is allowed to do what

Everything else depends on this, so start here.

Two things are easy to confuse:

- A **role** is a set of permissions that a *service* borrows. Your Lambda function
  assumes a role and receives temporary credentials automatically. There are no keys
  to manage, and this is why the app's code never contains an access key.
- A **user** is a human identity with long-lived access keys. You need one for your
  own laptop so the AWS CLI can work.

A role has two separate policies, and mixing them up is the most common beginner error:

| | Question it answers | In this project |
|---|---|---|
| **Trust policy** | *Who may assume this role?* | `lambda.amazonaws.com` — see `infra/trust-policy.json` |
| **Permission policy** | *What may the role do once assumed?* | Write to one S3 prefix, publish to one SNS topic — see `infra/lambda-policy.json` |

The permission policy here names one bucket prefix and one topic ARN rather than `"Resource": "*"`.
Get into that habit now. A leaked function with `s3:*` on `*` can delete every bucket
in the account.

### Lambda — code that runs only when called

You hand AWS a function; it runs on demand and you pay per millisecond.

The part that changes how you write code: Lambda reuses **execution environments**.
The first request to a cold function pays for downloading your image and importing
your modules — the **cold start**, typically 1–3 seconds for a container image this
size. Subsequent requests reuse that warm environment and respond in tens of
milliseconds. Anything at module level (imported libraries, cached boto3 clients)
survives between warm invocations. That is why `app/services/s3.py` caches its
client in a global instead of building one per request.

Limits worth memorising:

| Limit | Value | Why you care |
|---|---|---|
| Request payload | **6 MB** | Caps your upload size. See §6. |
| Response payload | 6 MB | Fine for HTML. |
| Timeout | 15 min max (default 3 s) | Set 30 s; 3 s is not enough for a cold start plus an S3 write. |
| Memory | 128 MB – 10,240 MB | CPU scales with memory. 512 MB is the sweet spot here. |
| Container image | 10 GB | Zip packages are capped at 250 MB unzipped, which FastAPI + boto3 makes awkward — one reason this project uses an image. |
| `/tmp` | 512 MB default | Unused here; we stream to S3. |

The **handler** is the `module.function` string Lambda calls. Ours is
`lambda_handler.handler`.

Lambda speaks JSON events, not HTTP. **Mangum** is the adapter that turns an API
Gateway event into an ASGI call FastAPI understands, and turns the response back
into JSON. That is the entire content of `lambda_handler.py`.

### API Gateway — the public front door

Lambda has no URL of its own. API Gateway gives it one, plus throttling, auth,
custom domains and request logging.

Two flavours, and you want the second:

| | REST API (v1) | **HTTP API (v2)** |
|---|---|---|
| Price per million requests | ~$3.50 | **~$1.00** |
| Latency | Higher | Lower |
| Binary bodies | Must configure `binaryMediaTypes` manually | Handled automatically |
| Extras | API keys, usage plans, caching, request validation | Simpler, fewer knobs |

HTTP API is cheaper, faster and needs less configuration. Use REST API only when you
specifically need usage plans or built-in caching.

Concepts you will meet:

- **Route** — a method plus path, like `POST /apply`. `$default` catches everything,
  which is what we want since FastAPI does its own routing.
- **Integration** — what the route forwards to. Ours is `AWS_PROXY`, meaning "pass the
  whole request through to Lambda".
- **Stage** — a deployment of the API. `$default` with auto-deploy means no `/prod`
  prefix in your URL and no manual deploy step.
- **Payload format 2.0** — the event shape HTTP APIs send. Mangum handles both, but
  2.0 is what you get by default.

### S3 — the file store

Buckets hold objects. Two things surprise people:

**Bucket names are globally unique across every AWS account on earth.** `cv-submissions`
is long gone. Add a suffix: `averon-cv-submissions-hashir-01`.

**There are no folders.** The key `submissions/2026/09/06/AV-...pdf` is one flat string.
The console draws folders from the slashes. This matters because it means the prefix is
a naming convention you choose — so choose one that sorts usefully and lets an IAM
policy target it, which is exactly what `submissions/*` in our policy does.

For this project:

- **Block Public Access** stays on. CVs contain CNIC numbers and phone numbers. Nothing
  here should ever be publicly readable.
- **Default encryption** with `AES256` (SSE-S3). AWS manages the key, it is free, and
  it is one API call to enable. Use SSE-KMS only if you need per-key audit trails.
- **Presigned URLs** are how you share a private object: a time-limited signed link.
  The catch is in §6.
- **Lifecycle rules** delete old objects automatically. `infra/lifecycle.json` expires
  submissions after 365 days, which matches the retention promise the form makes.

### SNS — the notification fan-out

SNS is publish/subscribe. You publish one message to a **topic**; every **subscription**
on that topic receives a copy. Subscriptions can be email, SMS, Lambda, SQS or HTTPS —
so the same publish that emails you today can also trigger a Slack webhook later
without touching the app.

Two things to know before you build on it:

1. **Email subscriptions must be confirmed.** AWS sends a confirmation link when you
   subscribe. Until someone clicks it the subscription sits in `PendingConfirmation`
   and silently receives nothing. This is the number one "why isn't my email arriving"
   cause.
2. **SNS email is deliberately plain.** Plain text only, no HTML, no attachments, sender
   is always an `@sns.amazonaws.com` address, and the subject must be ASCII and under
   100 characters. Perfect for an internal alert; wrong for anything an applicant sees.
   For branded email use **SES** instead.

### CloudWatch Logs — where `print()` goes

Every Lambda writes to a log group named `/aws/lambda/<function-name>`. This is your
only window into a failing function, which is why the app logs a line for every
submission. Retention defaults to *never expire*, so set it explicitly or you pay
storage for logs forever.

---

## 2. Run it locally first

Get the app working with no AWS account at all. `DRY_RUN=true` validates input and
renders every page but skips S3 and SNS.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env               # DRY_RUN=true is already set
./run-local.sh                     # or: uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. Submit the form, watch the terminal print the S3 key it
*would* have written.

Run the tests — they use `moto` to fake AWS, so they need no credentials and cost
nothing:

```bash
pytest
```

19 tests cover CNIC and phone normalisation, the file rules, the honeypot, path
traversal in filenames, and a synthetic API Gateway event carrying a base64 multipart
upload. That last one is the test that would have caught the single most annoying
deployment bug in this stack.

Once the AWS resources in §3 exist, flip `DRY_RUN=false` and your local app will talk
to real S3 and SNS using your CLI credentials. That is the fastest way to debug an IAM
problem — no deploy loop.

---

## 3. One-time AWS setup

> **Prefer to click through the console instead?** See **[CONSOLE-SETUP.md](CONSOLE-SETUP.md)**
> — the same stack built by hand in the AWS Console, using a ZIP upload rather than a
> container image, so no Docker or ECR is involved. Worth doing once for the
> understanding, then come back here to automate it.

### 3.0 Prerequisites

- An AWS account with billing alerts configured. Do this before anything else.
- AWS CLI v2 — `aws --version`
- Docker — `docker --version`
- Python 3.12

Create an IAM user for your laptop rather than using the account root. In the console:
**IAM → Users → Create user → Attach `AdministratorAccess`** (fine while learning, too
broad for production) → **Security credentials → Create access key → CLI**.

```bash
aws configure
# Access key ID, Secret, region: ap-south-1, output: json
aws sts get-caller-identity     # should print your account ID
```

Never commit those keys. `.gitignore` already excludes `.env`.

**Region choice:** `ap-south-1` (Mumbai) is the lowest-latency region from Karachi;
`me-central-1` (UAE) is a close second. Every resource below must live in the *same*
region, and S3 bucket names must be unique worldwide.

### 3.1 Set your shell variables

Everything after this reuses these. Keep the terminal open.

```bash
export AWS_REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export BUCKET=averon-cv-submissions-hashir-01     # change this, must be globally unique
export TOPIC_NAME=cv-submissions
export FUNCTION_NAME=cv-portal
export ROLE_NAME=cv-portal-role
export ECR_REPO=cv-portal
export NOTIFY_EMAIL=you@example.com               # your real address
```

### 3.2 Create the S3 bucket

```bash
aws s3api create-bucket \
  --bucket "$BUCKET" \
  --region "$AWS_REGION" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION"
```

> `LocationConstraint` is required for every region **except** `us-east-1`, where
> passing it is an error. A frequent first-command failure.

Lock it down and turn on encryption and expiry:

```bash
aws s3api put-public-access-block \
  --bucket "$BUCKET" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws s3api put-bucket-encryption \
  --bucket "$BUCKET" \
  --server-side-encryption-configuration file://infra/bucket-encryption.json

aws s3api put-bucket-lifecycle-configuration \
  --bucket "$BUCKET" \
  --lifecycle-configuration file://infra/lifecycle.json

aws s3api put-bucket-versioning \
  --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
```

Versioning means a bad deploy or an accidental overwrite cannot destroy a CV.

### 3.3 Create the SNS topic and subscribe

```bash
export TOPIC_ARN=$(aws sns create-topic --name "$TOPIC_NAME" --query TopicArn --output text)
echo "$TOPIC_ARN"

aws sns subscribe \
  --topic-arn "$TOPIC_ARN" \
  --protocol email \
  --notification-endpoint "$NOTIFY_EMAIL"
```

**Now go to your inbox and click the confirmation link.** Then check:

```bash
aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" \
  --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output table
```

If `SubscriptionArn` still says `PendingConfirmation`, the click did not register and
no email will ever arrive. Send yourself a test:

```bash
aws sns publish --topic-arn "$TOPIC_ARN" --subject "Test" --message "Hello from SNS"
```

### 3.4 Create the Lambda execution role

```bash
# The role, with a trust policy saying "Lambda may assume me"
aws iam create-role \
  --role-name "$ROLE_NAME" \
  --assume-role-policy-document file://infra/trust-policy.json

# Permission to write CloudWatch Logs
aws iam attach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Permission to touch exactly our bucket prefix and our topic
sed -e "s/REPLACE_BUCKET_NAME/$BUCKET/" \
    -e "s/REPLACE_REGION/$AWS_REGION/" \
    -e "s/REPLACE_ACCOUNT_ID/$ACCOUNT_ID/" \
    infra/lambda-policy.json > /tmp/lambda-policy.json

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name cv-portal-s3-sns \
  --policy-document file:///tmp/lambda-policy.json

export ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)
echo "$ROLE_ARN"
```

Read `/tmp/lambda-policy.json` before moving on. Being able to read a policy document
is a core AWS skill, and this one is small enough to read in full.

> IAM is eventually consistent. If the next step fails with "cannot be assumed",
> wait ten seconds and retry.

### 3.5 Build and push the container image

```bash
aws ecr create-repository --repository-name "$ECR_REPO" \
  --image-scanning-configuration scanOnPush=true

export REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

docker build --platform linux/amd64 -t "$REGISTRY/$ECR_REPO:v1" .
docker push "$REGISTRY/$ECR_REPO:v1"
```

`--platform linux/amd64` is not optional on an Apple Silicon Mac. Without it you build
an arm64 image, Lambda refuses to start it, and the error message does not mention
architecture.

### 3.6 Create the Lambda function

```bash
aws lambda create-function \
  --function-name "$FUNCTION_NAME" \
  --package-type Image \
  --code ImageUri="$REGISTRY/$ECR_REPO:v1" \
  --role "$ROLE_ARN" \
  --timeout 30 \
  --memory-size 512 \
  --environment "Variables={S3_BUCKET=$BUCKET,SNS_TOPIC_ARN=$TOPIC_ARN,ENVIRONMENT=production,DRY_RUN=false,COMPANY_NAME=Averon Automation,CAREERS_EMAIL=$NOTIFY_EMAIL}"
```

Note what is *not* in there: no access key, no secret. The role supplies credentials at
runtime.

> `AWS_REGION` is a reserved Lambda variable — you cannot set it yourself, and you do
> not need to. The runtime provides it and boto3 reads it. That is why `aws_region` in
> `app/config.py` is optional.

Keep log storage bounded:

```bash
aws logs put-retention-policy \
  --log-group-name "/aws/lambda/$FUNCTION_NAME" \
  --retention-in-days 14
```

### 3.7 Put API Gateway in front of it

Quick-create builds the API, the `$default` route, the `AWS_PROXY` integration and an
auto-deploying `$default` stage in one call:

```bash
export API_ID=$(aws apigatewayv2 create-api \
  --name cv-portal-api \
  --protocol-type HTTP \
  --target "arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}" \
  --query ApiId --output text)

# API Gateway still needs explicit permission to invoke the function
aws lambda add-permission \
  --function-name "$FUNCTION_NAME" \
  --statement-id apigateway-invoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*/*"

echo "https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com"
```

Open that URL. You should see the form.

That `add-permission` call is a **resource-based policy** — it lives on the Lambda
function and says which other services may invoke it. It is a different mechanism from
the execution role, and forgetting it produces an opaque `500 Internal Server Error`
with nothing in your application logs.

---

## 4. Deploy

Setup is done. From here on, shipping a change is one command:

```bash
chmod +x deploy.sh
./deploy.sh
```

It builds a timestamped image, pushes it, updates the function, and waits for the
update to finish.

To change configuration without rebuilding:

```bash
aws lambda update-function-configuration \
  --function-name "$FUNCTION_NAME" \
  --environment "Variables={S3_BUCKET=$BUCKET,SNS_TOPIC_ARN=$TOPIC_ARN,MAX_UPLOAD_MB=4,DRY_RUN=false}"
```

> `--environment` **replaces** the whole variable set. Always pass every variable you
> want to keep, or you will silently drop `S3_BUCKET` and wonder why uploads broke.

---

## 5. Verify and debug

```bash
# Follow logs live, then submit the form in your browser
aws logs tail "/aws/lambda/$FUNCTION_NAME" --follow

# Did the file land?
aws s3 ls "s3://$BUCKET/submissions/" --recursive --human-readable

# Inspect one object's metadata
aws s3api head-object --bucket "$BUCKET" \
  --key submissions/2026/09/06/AV-20260906-K7P2Q4-cv.pdf

# Download it
aws s3 cp "s3://$BUCKET/<the-key>" ./downloaded.pdf

# App's own health check
curl "https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/healthz"
```

`/healthz` reports whether the bucket and topic env vars are actually set — the fastest
way to catch a config typo.

---

## 6. The gotchas that will bite you

### Upload size is capped at about 4 MB, not 10

Three limits stack up:

- API Gateway payload: 10 MB
- **Lambda synchronous invoke payload: 6 MB** ← the binding constraint
- API Gateway base64-encodes binary bodies, inflating them by ~37%

So `6 MB ÷ 1.37 ≈ 4.3 MB` of actual file. `MAX_UPLOAD_MB` defaults to **4** for this
reason. A rejected-by-Lambda request never reaches your code, so you cannot even show a
friendly error — the browser just gets a `413`. If you need bigger files, use presigned
uploads (§7).

### Presigned URLs from Lambda expire with the credentials

`generate_presigned_url(ExpiresIn=604800)` looks like it gives a 7-day link. From
Lambda it does not. The signature is made with the role's **temporary STS credentials**,
and the URL dies when those expire — typically within the hour — regardless of
`ExpiresIn`. The link in the SNS email is a convenience for opening the CV right away,
not an archive link. For durable access, have your team use the console or the CLI.

### S3 object metadata must be US-ASCII

`put_object(Metadata=...)` throws on non-ASCII values, so an applicant named `Hashír`
breaks the upload. `app/services/s3.py` percent-encodes metadata values. Same class of
problem in SNS: subjects must be ASCII, single-line and under 100 characters, which is
why `app/services/sns.py` transliterates before truncating.

### Boto3 clients created at import time cannot be mocked

`moto` patches botocore when it starts. A client built during module import — before the
mock is active — bypasses it entirely, and your tests hit real AWS or fail with a
confusing `403`. Both service modules use a cached lazy client with a `reset_client()`
hook for tests. This is exactly the bug that appeared while building this project.

### `python-multipart` is not optional

Without it, FastAPI raises `Form data requires "python-multipart"` and every upload
fails. It is easy to miss because nothing imports it by name.

### REST API needs binary media types configured

If you use a REST API (v1) instead of an HTTP API, set
`binaryMediaTypes` to `multipart/form-data` (or `*/*`) or your PDFs arrive corrupted.
HTTP API v2 handles this for you. This is the single most common reason "the file
uploads but the PDF won't open".

### Set `ROOT_PATH` if your stage has a prefix

`$default` stages serve from the root, so the default empty `ROOT_PATH` is right. If you
deploy to a named stage like `prod`, the URL becomes `.../prod/` and every generated link
and form action breaks. Set `ROOT_PATH=/prod` and FastAPI fixes them all.

### Cold starts are real

First request after idle: 1–3 seconds. Options, in order of how much they cost:
raise memory to 1024 MB (more CPU, faster import, often *cheaper* per request because
duration drops), or configure **provisioned concurrency** (no cold starts, but you pay
hourly whether traffic arrives or not). For a careers page, just accept it.

### Serving static files through Lambda is wasteful

Every CSS request wakes your function. Fine for one stylesheet. Once you have images and
fonts, put them behind CloudFront or serve them from S3 directly.

---

## 7. Where to take it next

Roughly in order of value:

**Presigned uploads, to escape the 4 MB ceiling.** The browser uploads straight to S3
and never routes the file through Lambda. Lambda's only job becomes signing the request:

```python
# in app/services/s3.py
def presigned_post(key: str, content_type: str, max_bytes: int):
    return client().generate_presigned_post(
        Bucket=settings.s3_bucket,
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, max_bytes],
        ],
        ExpiresIn=900,
    )
```

The flow becomes: `POST /apply` validates the text fields and returns the signed policy →
JavaScript posts the file directly to S3 → a second small call confirms and triggers the
SNS publish. The `content-length-range` condition is enforced by S3 itself, so the size
limit holds even if someone bypasses your JavaScript. You will also need a bucket CORS
rule allowing `POST` from your domain.

**SES instead of SNS for anything an applicant sees.** SNS cannot do HTML, attachments,
or a real sender address. SES can: verify a domain, then send a branded "we got your
application" confirmation to the applicant. Keep SNS for the internal alert — the two
serve different jobs.

**DynamoDB for the metadata.** Right now the searchable record of an application lives
only in an email. A DynamoDB table keyed on the reference ID makes the pipeline
queryable: filter by role, by date, by status. It is the natural next service to learn
after these four, and the free tier is generous.

**Infrastructure as code.** Every command in §3 is a resource you will otherwise have to
remember. Rewrite them as an AWS SAM template or Terraform config and the whole stack
becomes one `sam deploy` — reviewable, version-controlled and reproducible. Given the
DevOps direction you are heading, this is the highest-leverage thing on this list.

**CI/CD with GitHub Actions.** On push to `main`: run `pytest`, build the image, push to
ECR, update the function. Use an OIDC role rather than storing access keys in GitHub
secrets — that is the current best practice and worth learning properly.

**Rate limiting.** The honeypot stops naive bots. Add API Gateway throttling
(`--default-route-settings ThrottlingBurstLimit=10,ThrottlingRateLimit=5`) so nobody can
run up your S3 bill with a loop.

**A CloudFront distribution** in front of the API for a custom domain, TLS certificate
and cached static assets.

**Antivirus scanning.** You are accepting arbitrary files from strangers. A separate
Lambda triggered by S3 `ObjectCreated` events can scan with ClamAV and quarantine
anything suspicious. This is the right pattern: the upload path stays fast, scanning
happens asynchronously.

---

## 8. What this costs

Effectively nothing at this scale.

| Service | Free tier | Beyond it |
|---|---|---|
| Lambda | 1M requests + 400,000 GB-s per month, **permanently** | $0.20 per 1M requests |
| API Gateway (HTTP) | 1M requests/month for 12 months | ~$1.00 per 1M |
| S3 | 5 GB + 2,000 PUT for 12 months | ~$0.023/GB/month, $0.005 per 1,000 PUT |
| SNS | 1M publishes; 1,000 email notifications/month | $2.00 per 100,000 email |
| CloudWatch Logs | 5 GB ingestion | ~$0.50/GB |
| ECR | 500 MB for 12 months | $0.10/GB/month |

A thousand applications a month with 500 KB CVs sits inside the free tier on every line.
The realistic surprise is ECR storage if you push a new image daily and never prune, so
set a lifecycle policy on the repository to keep the last ten.

Set a billing alarm regardless. Every AWS learner has one story about a forgotten
resource.

---

## 9. Tear it all down

Deleting things is part of learning them. In this order:

```bash
aws apigatewayv2 delete-api --api-id "$API_ID"
aws lambda delete-function --function-name "$FUNCTION_NAME"
aws ecr delete-repository --repository-name "$ECR_REPO" --force
aws sns delete-topic --topic-arn "$TOPIC_ARN"

# S3 refuses to delete a non-empty bucket. Versioning means "empty" includes
# every old version, which --force does not cover.
aws s3 rm "s3://$BUCKET" --recursive
aws s3api delete-bucket --bucket "$BUCKET"

aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name cv-portal-s3-sns
aws iam detach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name "$ROLE_NAME"

aws logs delete-log-group --log-group-name "/aws/lambda/$FUNCTION_NAME"
```

If the bucket delete fails on a versioned bucket, you need to delete the object versions
and delete markers too — the console's "Empty bucket" button does this for you.

---

## Project layout

```
cv-portal/
├── app/
│   ├── main.py              routes: GET /, POST /apply, GET /submitted, /healthz
│   ├── config.py            settings from environment variables
│   ├── schemas.py           CNIC / phone / email validation and human error copy
│   ├── files.py             size cap, extension allowlist, magic-byte check
│   ├── services/
│   │   ├── s3.py            put_object, key layout, presigned links
│   │   └── sns.py           publish, ASCII-safe subject
│   ├── templates/           base.html, index.html, submitted.html
│   └── static/css/styles.css
├── tests/test_portal.py     19 tests, moto-mocked, no AWS needed
├── infra/                   IAM trust + permission policies, bucket config
├── lambda_handler.py        Mangum adapter — the Lambda entrypoint
├── Dockerfile               public.ecr.aws/lambda/python:3.12 base
├── deploy.sh                build → push → update function (container route)
├── build-zip.sh             build layer.zip + function.zip (console route)
├── CONSOLE-SETUP.md         the same stack, built by hand in the console
└── run-local.sh             uvicorn with reload
```

### Security choices already made for you

Because the form collects CNIC numbers, a few decisions are baked in rather than left
as exercises:

- Bucket is private with Block Public Access on, encrypted at rest, versioned, and
  expires objects after 365 days.
- IAM policy grants two actions on one prefix and one topic. Nothing wider.
- Uploads are checked by extension **and** by magic bytes, so `virus.exe` renamed to
  `cv.pdf` is rejected.
- Filenames are sanitised, killing path traversal — `../../etc/passwd.pdf` cannot escape
  the prefix.
- Size is enforced while streaming, so a large upload is abandoned rather than buffered.
- An explicit consent checkbox is required, and the form states what is kept and for how
  long.
- A honeypot field catches naive bots.
- `docs_url`, `redoc_url` and `openapi_url` are disabled — a public careers form has no
  business exposing an API schema.
- If SNS fails after S3 succeeded, the applicant still gets a success page and the
  failure is logged. Losing a notification is recoverable; losing a CV is not.
