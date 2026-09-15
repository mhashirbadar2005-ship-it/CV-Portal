# Container image for AWS Lambda. Chosen over a zip because the dependency tree
# (fastapi + pydantic + boto3) is awkward to pack into a 250 MB unzipped layer,
FROM public.ecr.aws/lambda/python:3.12

# Dependencies first so this layer is cached between code changes.
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r requirements.txt --target "${LAMBDA_TASK_ROOT}"

# Application code
COPY app/ ${LAMBDA_TASK_ROOT}/app/
COPY lambda_handler.py ${LAMBDA_TASK_ROOT}/

# module.function that Lambda invokes
CMD ["lambda_handler.handler"]
