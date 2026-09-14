"""Tests for the CV portal.

Run with:  pytest -q
Requires:  pip install pytest "moto[s3,sns]"

`moto` stands in for AWS, so these tests never touch a real account and never
cost anything.
"""

import base64
import os

import pytest

os.environ.update(
    AWS_REGION="ap-south-1",
    AWS_DEFAULT_REGION="ap-south-1",
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    AWS_SESSION_TOKEN="testing",
    S3_BUCKET="cv-portal-test",
    SNS_TOPIC_ARN="arn:aws:sns:ap-south-1:123456789012:cv-submissions",
    OPEN_ROLES="Backend Engineer (Python/FastAPI),DevOps & Cloud Engineer",
    MAX_UPLOAD_MB="4",
    DRY_RUN="false",
)

PDF = b"%PDF-1.7\n" + b"A" * 4096

VALID = {
    "full_name": "Hashir Badar",
    "email": "hashir@example.com",
    "cnic": "42101-1234567-8",
    "phone": "0301 2345678",
    "portfolio_url": "github.com/example",
    "note": "Built a few automations.",
    "consent": "true",
}


@pytest.fixture
def aws():
    """A mock S3 bucket and SNS topic, torn down after each test.

    The app caches its boto3 clients, so they are dropped on the way in and out:
    a client built during one mocked session is useless in the next.
    """
    from moto import mock_aws

    from app.services import s3 as storage
    from app.services import sns as notifier

    storage.reset_client()
    notifier.reset_client()

    with mock_aws():
        import boto3

        s3 = boto3.client("s3", region_name="ap-south-1")
        s3.create_bucket(
            Bucket="cv-portal-test",
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )
        sns = boto3.client("sns", region_name="ap-south-1")
        sns.create_topic(Name="cv-submissions")
        yield s3

    storage.reset_client()
    notifier.reset_client()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


# --- pages ------------------------------------------------------------------


def test_form_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Your application" in r.text
    assert 'name="cv"' in r.text


def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# --- happy path -------------------------------------------------------------


def test_submission_lands_in_s3(client, aws):
    r = client.post(
        "/apply",
        data=VALID,
        files={"cv": ("Hashir CV.pdf", PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "ref=AV-" in r.headers["location"]

    objects = aws.list_objects_v2(Bucket="cv-portal-test")["Contents"]
    assert len(objects) == 1
    key = objects[0]["Key"]
    assert key.startswith("submissions/")
    assert key.endswith("Hashir-CV.pdf")  # spaces sanitised

    head = aws.head_object(Bucket="cv-portal-test", Key=key)
    assert head["ContentType"] == "application/pdf"
    assert head["ServerSideEncryption"] == "AES256"
    assert head["Metadata"]["email"] == "hashir@example.com"

    body = aws.get_object(Bucket="cv-portal-test", Key=key)["Body"].read()
    assert body == PDF


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("cnic", "123", "13 digits"),
        ("phone", "12345", "Pakistani mobile"),
        ("email", "not-an-email", "@"),
        ("full_name", "H", "full name"),
    ],
)
def test_bad_field_is_reported(client, aws, field, value, expected):
    r = client.post(
        "/apply",
        data={**VALID, field: value},
        files={"cv": ("cv.pdf", PDF, "application/pdf")},
    )
    assert r.status_code == 422
    assert expected in r.text
    assert "Contents" not in aws.list_objects_v2(Bucket="cv-portal-test")


def test_missing_consent_is_rejected(client, aws):
    payload = {k: v for k, v in VALID.items() if k != "consent"}
    r = client.post("/apply", data=payload, files={"cv": ("cv.pdf", PDF, "application/pdf")})
    assert r.status_code == 422
    assert "permission" in r.text


def test_values_survive_a_failed_submission(client, aws):
    r = client.post(
        "/apply",
        data={**VALID, "cnic": "123"},
        files={"cv": ("cv.pdf", PDF, "application/pdf")},
    )
    assert 'value="Hashir Badar"' in r.text
    assert "hashir@example.com" in r.text


# --- file rules -------------------------------------------------------------


def test_missing_file(client, aws):
    r = client.post("/apply", data=VALID)
    assert r.status_code == 422
    assert "Attach your CV" in r.text


def test_wrong_extension(client, aws):
    r = client.post("/apply", data=VALID, files={"cv": ("virus.exe", b"MZ...", "application/x-msdownload")})
    assert r.status_code == 422
    assert "We accept" in r.text


def test_extension_lies_about_contents(client, aws):
    r = client.post("/apply", data=VALID, files={"cv": ("cv.pdf", b"just text", "application/pdf")})
    assert r.status_code == 422
    assert "match a PDF" in r.text


def test_oversize_file(client, aws):
    big = b"%PDF-" + b"0" * (5 * 1024 * 1024)
    r = client.post("/apply", data=VALID, files={"cv": ("big.pdf", big, "application/pdf")})
    assert r.status_code == 422
    assert "over 4 MB" in r.text


def test_docx_is_accepted(client, aws):
    docx = b"PK\x03\x04" + b"0" * 2048
    r = client.post(
        "/apply",
        data=VALID,
        files={"cv": ("cv.docx", docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        follow_redirects=False,
    )
    assert r.status_code == 303


# --- abuse ------------------------------------------------------------------


def test_honeypot_silently_drops(client, aws):
    r = client.post(
        "/apply",
        data={**VALID, "company_website": "http://spam.example"},
        files={"cv": ("cv.pdf", PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Contents" not in aws.list_objects_v2(Bucket="cv-portal-test")


def test_path_traversal_in_filename(client, aws):
    r = client.post(
        "/apply",
        data=VALID,
        files={"cv": ("../../../etc/passwd.pdf", PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    key = aws.list_objects_v2(Bucket="cv-portal-test")["Contents"][0]["Key"]
    assert ".." not in key
    assert key.count("/") == 4  # submissions/YYYY/MM/DD/file


# --- Lambda wiring ----------------------------------------------------------


def _api_gateway_v2_event(method, path, body=None, content_type=None):
    headers = {
        "host": "abc123.execute-api.ap-south-1.amazonaws.com",
        "x-forwarded-proto": "https",
        "x-forwarded-port": "443",
    }
    if content_type:
        headers["content-type"] = content_type
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": headers,
        "requestContext": {
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1",
                     "sourceIp": "39.55.1.1", "userAgent": "pytest"},
            "domainName": headers["host"],
            "stage": "$default",
            "requestId": "req-1",
            "apiId": "abc123",
        },
        "body": base64.b64encode(body).decode() if body else None,
        "isBase64Encoded": bool(body),
    }


def test_lambda_handler_serves_the_form():
    from lambda_handler import handler

    resp = handler(_api_gateway_v2_event("GET", "/"), None)
    assert resp["statusCode"] == 200


def test_lambda_handler_accepts_a_base64_multipart_upload(aws):
    """The single most fragile part of this stack: API Gateway base64-encodes
    binary request bodies, and the PDF must survive the round trip intact."""
    from lambda_handler import handler

    boundary = "----boundaryTEST"
    chunks = []
    for name, value in VALID.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="cv"; filename="cv.pdf"\r\n'
        f"Content-Type: application/pdf\r\n\r\n".encode()
        + PDF
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)

    event = _api_gateway_v2_event(
        "POST", "/apply", body, f"multipart/form-data; boundary={boundary}"
    )
    resp = handler(event, None)

    assert resp["statusCode"] == 303
    key = aws.list_objects_v2(Bucket="cv-portal-test")["Contents"][0]["Key"]
    assert aws.get_object(Bucket="cv-portal-test", Key=key)["Body"].read() == PDF
