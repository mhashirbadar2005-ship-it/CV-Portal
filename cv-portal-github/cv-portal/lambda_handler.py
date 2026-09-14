"""AWS Lambda entrypoint.

Mangum translates an API Gateway (or Lambda Function URL) event into an ASGI
scope, hands it to FastAPI, and converts the response back. `lifespan="off"`
matters: Lambda has no long-running startup phase, and leaving lifespan on can
hang the first invocation.

Set the function's handler to:  lambda_handler.handler
"""

from mangum import Mangum

from app.main import app

handler = Mangum(app, lifespan="off")
