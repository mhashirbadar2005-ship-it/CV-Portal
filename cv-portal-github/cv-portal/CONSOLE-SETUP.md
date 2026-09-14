# Console setup — clicking it instead of scripting it

README §3 does everything with the AWS CLI. This file does the same thing through the
AWS Console, because clicking through a service once teaches you what its knobs actually
are. Do this first; automate it later.

## Which files you actually need

The application code is identical either way. The console only replaces the
provisioning commands.

| File | Needed? | What you do with it |
|---|---|---|
| `app/` (all of it) | **Yes** | Goes into `function.zip` |
| `lambda_handler.py` | **Yes** | The entrypoint. Handler = `lambda_handler.handler` |
| `build-zip.sh` | **Yes** | Produces the two zips you upload |
| `infra/lambda-policy.json` | **Yes** | Paste into the IAM policy JSON editor |
| `infra/test-event-get.json` | Useful | Paste into the Lambda console Test tab |
| `requirements-dev.txt`, `tests/`, `pytest.ini`, `run-local.sh` | Local only | Never uploaded, but keep them — running `pytest` before each upload saves you a debug cycle |
| `.env.example` | Reference | On Lambda these become console environment variables |
| `Dockerfile`, `deploy.sh` | **Skip** | Only for the container-image route |
| `infra/trust-policy.json` | **Skip** | The console writes the trust policy when it creates the role |
| `infra/bucket-encryption.json`, `infra/lifecycle.json` | **Skip** | These are checkboxes and forms in the S3 console |

So: **two zip files to upload, one JSON policy to paste.** That's it.

Build the zips:

```bash
bash build-zip.sh
# dist/layer.zip     3.4 MB   dependencies
# dist/function.zip   19 KB   your code
```

Two files rather than one because a code change then costs a 19 KB upload instead of
3.4 MB. Want a single file instead? `bash build-zip.sh --single` gives you
`dist/lambda.zip`.

> **On Windows**, run `build-zip.sh` inside WSL or Git Bash. The `--platform
> manylinux2014_x86_64` flags in it exist because `pydantic-core` is a compiled
> extension: install it on Windows without those flags and Lambda fails with
> `Unable to import module 'lambda_handler'`, which tells you nothing about the real
> cause.

---

## Do it in this order

Dependencies run one way: the Lambda function needs the bucket name and topic ARN to
exist, so create those first.

### 1. S3 bucket

**S3 → Create bucket**

- **Bucket name** — globally unique across all of AWS. `averon-cv-submissions-hashir-01`.
- **AWS Region** — `ap-south-1` (Mumbai) is closest to Karachi. Every other resource
  must go in this same region.
- **Object Ownership** — ACLs disabled. This is the modern default; leave it.
- **Block Public Access** — leave all four boxes checked. CVs contain CNIC numbers.
- **Bucket Versioning** — Enable. An accidental overwrite can't then destroy a CV.
- **Default encryption** — SSE-S3 (Amazon S3 managed keys), Bucket Key enabled.
- **Create bucket**

Then add the retention rule that the form promises applicants:

**your bucket → Management → Create lifecycle rule**

- Name: `DeleteSubmissionsAfter12Months`
- Prefix: `submissions/`
- Check *Expire current versions of objects* → **365** days
- Also check *Permanently delete noncurrent versions* → 365 days, since versioning is on

Note the bucket name somewhere. You'll need it twice more.

### 2. SNS topic

**Amazon SNS → Topics → Create topic**

- **Type** — **Standard**. Not FIFO: FIFO topics cannot have email subscriptions at all,
  and the console won't tell you why the email option vanished.
- **Name** — `cv-submissions`
- **Create topic**, then copy the **ARN** — it looks like
  `arn:aws:sns:ap-south-1:123456789012:cv-submissions`

**On the topic page → Create subscription**

- **Protocol** — Email
- **Endpoint** — your real address
- **Create subscription**

**Now open your inbox and click "Confirm subscription".** Refresh the console; the
status must read **Confirmed**. If it still says *Pending confirmation*, no email will
ever arrive and nothing will warn you. This is the single most common failure in this
whole build.

Test it before moving on: topic page → **Publish message** → put anything in Subject and
Message → **Publish message**. If that email doesn't land, fix it now, not after three
more steps.

### 3. Lambda layer (the dependencies)

**Lambda → Layers → Create layer**

- **Name** — `cv-portal-deps`
- **Upload a .zip file** — `dist/layer.zip`
- **Compatible architectures** — `x86_64`
- **Compatible runtimes** — `Python 3.12`
- **Create**

A layer is just a zip that gets unpacked to `/opt` alongside your function. Lambda adds
`/opt/python` to the Python path, which is why `build-zip.sh` puts everything inside a
top-level `python/` directory. Wrong directory name is the usual reason a layer appears
to do nothing.

### 4. Lambda function

**Lambda → Create function → Author from scratch**

- **Function name** — `cv-portal`
- **Runtime** — Python 3.12 (must match the layer)
- **Architecture** — x86_64
- **Change default execution role** → *Create a new role with basic Lambda permissions*
- **Create function**

That role gives CloudWatch Logs access only. Now configure the rest, in this order:

**Code tab → Upload from → .zip file** → `dist/function.zip`

**Code tab → Runtime settings → Edit**
- **Handler** — `lambda_handler.handler`

The default is `lambda_function.lambda_handler`, which does not exist in our zip. Leaving
it is a guaranteed failure.

**Code tab → scroll to Layers → Add a layer**
- *Custom layers* → `cv-portal-deps` → Version 1 → **Add**

**Configuration → General configuration → Edit**
- **Memory** — 512 MB
- **Timeout** — 30 sec

The default 3-second timeout is not enough for a cold start plus an S3 write. You will
see a timeout, assume your code is broken, and lose an hour.

**Configuration → Environment variables → Edit** — add:

| Key | Value |
|---|---|
| `S3_BUCKET` | your bucket name from step 1 |
| `SNS_TOPIC_ARN` | the ARN from step 2 |
| `MAX_UPLOAD_MB` | `4` |
| `DRY_RUN` | `false` |
| `ENVIRONMENT` | `production` |
| `COMPANY_NAME` | `Averon Automation` |
| `CAREERS_EMAIL` | your address |

Do **not** add `AWS_REGION` — it's a reserved variable and the console will reject it.
The runtime provides it, and boto3 reads it automatically.

### 5. Permissions for S3 and SNS

The function can currently write logs and nothing else. Grant the two things it needs:

**Configuration → Permissions → click the Role name** (opens IAM in a new tab)

**Add permissions → Create inline policy → JSON tab**

Paste `infra/lambda-policy.json`, replacing the three placeholders with your real values:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "WriteAndReadOwnSubmissions",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/submissions/*"
    },
    {
      "Sid": "NotifyRecruitingTopic",
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "arn:aws:sns:ap-south-1:YOUR-ACCOUNT-ID:cv-submissions"
    }
  ]
}
```

Your account ID is in the top-right account menu. Name the policy
`cv-portal-s3-sns` and **Create policy**.

Read that JSON properly before you save it. Two actions, one bucket prefix, one topic —
this is what least privilege looks like, and it's the habit worth building now. It is
tempting to write `"Action": "s3:*"` and `"Resource": "*"`; a leaked function with that
policy can delete every bucket in your account.

### 6. Test the function before adding API Gateway

**Test tab → Create new event**

- **Event name** — `get-form`
- Paste the contents of `infra/test-event-get.json` (delete the `_comment` line if the
  console objects)
- **Save**, then **Test**

A pass looks like `"statusCode": 200` and a long HTML string in `body`. If it fails, the
message tells you which of the last three steps went wrong:

| Error | Cause |
|---|---|
| `Unable to import module 'lambda_handler'` | Layer not attached, or built on Windows without the platform flags |
| `Unable to import module ... No module named 'fastapi'` | Layer missing or its zip lacks the top-level `python/` folder |
| `Handler 'lambda_handler' missing` | Handler string still on the default |
| `Task timed out after 3.00 seconds` | Timeout not raised to 30 s |

Isolating the function here means that when API Gateway misbehaves later, you already
know the function itself is fine.

### 7. API Gateway

**API Gateway → Create API → HTTP API → Build**

- **Add integration** → **Lambda** → your region → function `cv-portal`
- **API name** — `cv-portal-api`
- **Next**

On the **Configure routes** step the wizard proposes `ANY /cv-portal`. That path is
wrong for us — FastAPI does its own routing, so API Gateway must forward everything.
Set up two routes:

| Method | Path |
|---|---|
| `ANY` | `/` |
| `ANY` | `/{proxy+}` |

`/` catches the form page; `/{proxy+}` is a greedy match catching `/apply`,
`/submitted`, `/static/css/styles.css` and anything you add later. Both point at the
same Lambda integration. Miss the second one and the form renders but submitting it
returns 404.

- **Next** → **Stages**: leave `$default` with auto-deploy → **Next** → **Create**

The console adds the Lambda invoke permission for you here — one step the CLI path makes
you do by hand.

Your URL is the **Invoke URL** on the API's detail page:
`https://<api-id>.execute-api.ap-south-1.amazonaws.com`

Open it. Submit a real CV.

### 8. Confirm the whole chain

- **S3 → your bucket → `submissions/`** — the file, under a dated prefix
- **Your inbox** — the SNS notification with the reference number
- **Lambda → Monitor → View CloudWatch logs** — a `stored submission ref=...` line

Then set log retention, because the default is *never expire* and you pay for that
storage forever:

**CloudWatch → Log groups → `/aws/lambda/cv-portal` → Actions → Edit retention setting →
14 days**

---

## Shipping a change after this

1. Edit the code
2. `pytest`
3. `bash build-zip.sh`
4. **Lambda → Code → Upload from → .zip file** → `dist/function.zip`

19 KB, a few seconds. You only rebuild and re-upload `layer.zip` when a dependency
version in `build-zip.sh` changes.

---

## When to stop clicking

Console work is the right way to learn a service and the wrong way to run one. You have
now made roughly thirty individual choices across five services, and none of them are
written down anywhere except this file. Recreate this stack in a second region and
you'll get it subtly wrong.

The fix is the CLI commands in README §3, and after that an AWS SAM template or
Terraform config, where the whole stack becomes one reviewable file. Do the console pass
once for understanding — then codify it, and never click through it again.
