# Define custom function directory
ARG FUNCTION_DIR="/function"

# Use Microsoft's Playwright image as build stage
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy AS build-image

# Include global arg in this stage of the build
ARG FUNCTION_DIR

# Create function directory
RUN mkdir -p ${FUNCTION_DIR}

# Copy requirements and install dependencies
COPY requirements-crawler.txt .
RUN pip install --no-cache-dir --target ${FUNCTION_DIR} -r requirements-crawler.txt

# Copy application code
COPY crawlers/ ${FUNCTION_DIR}/crawlers/
COPY models.py ${FUNCTION_DIR}/models.py

# Multi-stage build: grab a fresh copy of the base image
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy AS crawler

# Include global arg in this stage of the build
ARG FUNCTION_DIR
# Set working directory to function root directory
WORKDIR ${FUNCTION_DIR}

# Keep only Chromium payload to reduce final image size.
RUN rm -rf /ms-playwright/firefox-* /ms-playwright/webkit-*

# Copy in the built dependencies
COPY --from=build-image ${FUNCTION_DIR} ${FUNCTION_DIR}

# Set runtime interface client as default command for the container runtime
ENTRYPOINT [ "python", "-m", "awslambdaric" ]
# Pass the name of the function handler as an argument to the runtime
CMD [ "crawlers.lambda_function.lambda_handler" ]

# ────────────────────────────────────────────────────────
# Stage 3: tmdb (lightweight, no Chrome needed)
# ────────────────────────────────────────────────────────
FROM public.ecr.aws/lambda/python:3.11 AS tmdb

WORKDIR /var/task

COPY requirements-tmdb.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements-tmdb.txt -t .

COPY crawlers/poster_updater.py poster_updater.py
COPY crawlers/supabase_client.py supabase_client.py

CMD ["poster_updater.lambda_handler"]
