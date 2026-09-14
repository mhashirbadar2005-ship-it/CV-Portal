"""CV submission portal — FastAPI + Jinja2, deployable to AWS Lambda.

Flow:
    GET  /          form
    POST /apply     validate -> S3 put_object -> SNS publish -> redirect
    GET  /submitted confirmation with the reference number
    GET  /healthz   liveness probe
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.config import settings
from app.files import FileRejected, new_reference, read_cv
from app.schemas import ApplicationForm, errors_by_field
from app.services import s3 as storage
from app.services import sns as notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cv-portal")

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title=f"{settings.company_name} — CV portal",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    root_path=settings.root_path,
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def page_context(request: Request, **extra) -> dict:
    base = {
        "request": request,
        "company": settings.company_name,
        "careers_email": settings.careers_email,
        "max_mb": settings.max_upload_mb,
        "accept": ",".join(f".{e}" for e in settings.extension_list),
        "extensions": settings.extension_list,
        "values": {},
        "errors": {},
    }
    base.update(extra)
    return base


@app.get("/", response_class=HTMLResponse)
async def show_form(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html", context=page_context(request)
    )


@app.post("/apply")
async def submit_application(
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
    cnic: str = Form(""),
    phone: str = Form(""),
    portfolio_url: str = Form(""),
    note: str = Form(""),
    consent: bool = Form(False),
    cv: UploadFile | None = None,
    # Hidden decoy field. Real people never see it, bots fill everything.
    company_website: str = Form(""),
):
    submitted = {
        "full_name": full_name,
        "email": email,
        "cnic": cnic,
        "phone": phone,
        "portfolio_url": portfolio_url,
        "note": note,
        "consent": consent,
    }

    if company_website:
        log.warning("honeypot triggered, dropping submission")
        return RedirectResponse(url=request.url_for("submitted"), status_code=303)

    errors: dict[str, str] = {}

    try:
        form = ApplicationForm(**submitted)
    except ValidationError as exc:
        form = None
        errors = errors_by_field(exc)

    try:
        data, ext, safe_name = await read_cv(cv)
    except FileRejected as exc:
        data = ext = safe_name = None
        errors["cv"] = str(exc)

    if errors or form is None:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=page_context(request, values=submitted, errors=errors),
            status_code=422,
        )

    reference = new_reference()
    key = storage.build_key(reference, safe_name)

    if settings.dry_run:
        log.info("dry run: would store %s (%d bytes) for %s", key, len(data), form.email)
        return RedirectResponse(
            url=f"{request.url_for('submitted')}?ref={reference}", status_code=303
        )

    try:
        storage.upload_cv(
            data,
            key,
            ext,
            meta={
                "reference": reference,
                "applicant": form.full_name,
                "email": form.email,
            },
        )
    except storage.StorageError:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=page_context(
                request,
                values=submitted,
                errors={"form": "We couldn't save your CV just now. Try again in a minute."},
            ),
            status_code=503,
        )

    # The CV is safely stored, so a failed notification must not fail the
    # request for the applicant. Log it loudly and move on.
    try:
        notifier.publish_submission(
            reference=reference,
            form=form,
            s3_key=key,
            download_url=storage.presigned_download_url(key),
            file_name=safe_name,
            file_size=len(data),
        )
    except notifier.NotifyError:
        log.error("stored %s but could not notify the team", reference)

    log.info("stored submission ref=%s key=%s", reference, key)
    return RedirectResponse(url=f"{request.url_for('submitted')}?ref={reference}", status_code=303)


@app.get("/submitted", response_class=HTMLResponse, name="submitted")
async def submitted(request: Request, ref: str = ""):
    return templates.TemplateResponse(
        request=request, name="submitted.html", context=page_context(request, reference=ref)
    )


@app.get("/healthz")
async def healthz():
    return JSONResponse(
        {
            "status": "ok",
            "environment": settings.environment,
            "bucket_configured": bool(settings.s3_bucket),
            "topic_configured": bool(settings.sns_topic_arn),
        }
    )
